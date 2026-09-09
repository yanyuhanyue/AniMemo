"""Poster holdings, owner admission and post-commit object lifecycle.

Lock order: authenticated/service owner, entries by PK, media objects by UUID,
then the existing physical pool reservation. Cleanup never takes an owner lock.
"""

import hashlib
import logging
import re
import uuid
from dataclasses import dataclass

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.files.storage import FileSystemStorage
from django.db import transaction
from rest_framework.exceptions import APIException

from site_config.media_storage.identity import resolve_url_candidates
from site_config.media_storage.common import MediaStorageError
from site_config.media_storage.local import DynamicLocalBackend
from site_config.media_storage.storage import StoragePoolStorage, atomic_media_mutation, bind_local_upload, mark_media_reference_committed
from site_config.models import MediaObject, MediaStorageBackend, MediaWriteReservation, SiteSettings

from .models import Column, JournalEntry, JournalMediaReference, UserSettings


logger = logging.getLogger(__name__)
MEDIA_FIELDS = frozenset({"poster_file", "custom_poster_url", "clear_custom_poster"})
SLOTS = ("poster_file", "custom_poster_url")


class MediaAdmissionError(APIException):
    status_code = 400

    def __init__(self, code="media_hold_unavailable"):
        self.default_code = code
        super().__init__(detail={"code": code}, code=code)


def lock_media_owner(owner_id):
    if not transaction.get_connection().in_atomic_block:
        raise RuntimeError("Media admission requires an outer transaction")
    return get_user_model().objects.select_for_update().only("pk").get(pk=owner_id)


def managed_id(name):
    value = str(name or "")
    if not value.startswith("media-objects/"):
        return None
    try:
        return uuid.UUID(value[len("media-objects/"):])
    except (ValueError, AttributeError):
        return None


def file_name(entry):
    return str(getattr(entry.poster_file, "name", "") or "")


def owner_evidence(media):
    """Only persisted associations establish ownership, never a key or URL."""
    direct = JournalEntry.objects.filter(poster_file=media.reference_name).order_by().values_list("user_id", flat=True)
    avatars = UserSettings.objects.filter(avatar=media.reference_name).order_by().values_list("user_id", flat=True)
    covers = Column.objects.filter(cover=media.reference_name).order_by().values_list("author_id", flat=True)
    references = media.journal_references.order_by().values_list("owner_id", flat=True)
    owners = set(direct.union(avatars, covers, references, all=True))
    if media.upload_owner_id is not None:
        owners.add(media.upload_owner_id)
    return owners


def owns_media(media, owner_id):
    return owner_evidence(media) == {owner_id}


def trustworthy_size(media):
    if media is None or media.size_bytes <= 0 or not re.fullmatch(r"[0-9a-f]{64}", media.sha256 or ""):
        return None
    return media.size_bytes


def verify_new_holding(media):
    """Validate local durable bytes with bounded memory; never fetch a URL.

    R2 currently has no complete persisted version/checksum proof suitable for
    this admission contract. Its existing references remain protected, but a
    new URL holding cannot treat HEAD/Content-Length as proof of complete bytes.
    """
    size = trustworthy_size(media)
    if size is None or media.lifecycle == MediaObject.Lifecycle.DELETING:
        raise MediaAdmissionError()
    if media.storage_backend.backend_type != MediaStorageBackend.BackendType.LOCAL:
        raise MediaAdmissionError()
    digest = hashlib.sha256()
    count = 0
    try:
        path = DynamicLocalBackend(media.storage_backend).path_for(media.object_key)
        with path.open("rb") as source:
            while True:
                chunk = source.read(min(64 * 1024, size + 1 - count))
                if not chunk:
                    break
                count += len(chunk)
                if count > size:
                    raise MediaAdmissionError()
                digest.update(chunk)
    except (OSError, ValueError, MediaStorageError) as error:
        raise MediaAdmissionError() from error
    if count != size or digest.hexdigest() != media.sha256:
        raise MediaAdmissionError()
    return size


@dataclass(frozen=True)
class PosterUsage:
    known_bytes: int
    unknown_slots: tuple

    @property
    def status(self):
        return "UNKNOWN_USAGE" if self.unknown_slots else "KNOWN"


def _entry_slot_sizes(entry, references=None, url_cache=None, *, read_source=None):
    references = references if references is not None else {ref.slot: ref for ref in entry.media_references.select_related("media")}
    url_cache = url_cache if url_cache is not None else {}
    sizes = {}
    for slot in SLOTS:
        value = file_name(entry) if slot == "poster_file" else entry.custom_poster_url
        if not value:
            sizes[slot] = 0
            continue
        ref = references.get(slot)
        if ref is not None:
            valid = ref.owner_id == entry.user_id and ref.value == value
            if slot == "poster_file":
                valid = valid and ref.media_id == managed_id(value)
            sizes[slot] = trustworthy_size(ref.media) if valid else None
            continue
        if slot == "poster_file":
            if read_source is None:
                media = MediaObject.objects.filter(pk=managed_id(value)).first() if managed_id(value) else None
            else:
                media = read_source.media_for_id(managed_id(value))
            if media is None and isinstance(entry.poster_file.storage, FileSystemStorage):
                try:
                    size = entry.poster_file.size
                    sizes[slot] = size if size > 0 else None
                except (OSError, ValueError):
                    sizes[slot] = None
            else:
                sizes[slot] = trustworthy_size(media)
            continue
        if value not in url_cache:
            url_cache[value] = (resolve_url_candidates(value) if read_source is None
                                else read_source.resolve_candidates(value))
        candidates = url_cache[value]
        owned = [media for media in candidates if
                 (owns_media(media, entry.user_id) if read_source is None
                  else read_source.owners_for(media) == {entry.user_id})]
        sizes[slot] = trustworthy_size(owned[0]) if len(candidates) == 1 and len(owned) == 1 else (None if owned else 0)
    return sizes


def poster_usage(owner_id):
    total, unknown, url_cache = 0, [], {}
    entries = (JournalEntry.objects.filter(user_id=owner_id)
               .only("id", "user_id", "poster_file", "custom_poster_url")
               .prefetch_related("media_references__media").order_by("pk"))
    for entry in entries.iterator(chunk_size=256):
        refs = {ref.slot: ref for ref in entry.media_references.all()}
        for slot, size in _entry_slot_sizes(entry, refs, url_cache).items():
            if size is None:
                unknown.append((entry.pk, slot))
            else:
                total += size
    return PosterUsage(total, tuple(unknown))


def _admit(before, old_sizes, new_sizes):
    removed = sum(size for size in old_sizes.values() if size is not None)
    added = sum(size for size in new_sizes.values() if size is not None)
    if any(size is None for size in new_sizes.values()):
        # Unchanged unknown slots can be retained, but never invented or priced
        # as zero. Safe changes to another slot still use the known net delta.
        for slot, size in new_sizes.items():
            if size is None and old_sizes.get(slot) is not None:
                raise MediaAdmissionError("media_usage_unknown")
    delta = added - removed
    if before.unknown_slots and delta > 0:
        raise MediaAdmissionError("media_usage_unknown")
    after = before.known_bytes + delta
    quota = settings.POSTER_STORAGE_QUOTA_BYTES
    if delta > 0 and after > quota:
        raise MediaAdmissionError("poster_quota_exceeded")


def _changed_media(instance, previous, update_fields):
    if update_fields is not None and not (set(update_fields) & set(SLOTS)):
        return False
    for slot in SLOTS:
        if update_fields is not None and slot not in update_fields:
            continue
        value = file_name(instance) if slot == "poster_file" else instance.custom_poster_url
        old_value = (file_name(previous) if slot == "poster_file" else previous.custom_poster_url) if previous else ""
        if value != old_value or (slot == "poster_file" and instance.poster_file and not instance.poster_file._committed):
            return True
    return False


def save_entry_with_media(instance, save, save_kwargs):
    update_fields = save_kwargs.get("update_fields")
    previous = JournalEntry.objects.filter(pk=instance.pk).first() if instance.pk else None
    if previous and previous.user_id != instance.user_id:
        raise MediaAdmissionError("media_owner_change_denied")
    if not _changed_media(instance, previous, update_fields):
        return save()
    if not instance.user_id:
        raise MediaAdmissionError("media_owner_change_denied")
    # The model is also used by Admin and authorized server services. Perform
    # the same bounded sanitizer before any final logical-byte admission.
    from .signals import sanitize_entry_poster

    sanitize_entry_poster(JournalEntry, instance)
    with atomic_media_mutation():
        lock_media_owner(instance.user_id)
        previous = JournalEntry.objects.select_for_update().filter(pk=instance.pk).first() if instance.pk else None
        if previous and previous.user_id != instance.user_id:
            raise MediaAdmissionError("media_owner_change_denied")
        if update_fields is not None and previous:
            for slot in SLOTS:
                if slot not in update_fields:
                    setattr(instance, slot, getattr(previous, slot))
        before = poster_usage(instance.user_id)
        old_refs = {ref.slot: ref for ref in previous.media_references.select_related("media")} if previous else {}
        old_sizes = _entry_slot_sizes(previous, old_refs) if previous else dict.fromkeys(SLOTS, 0)
        targets = {}
        new_sizes = {}
        uploading = bool(instance.poster_file and not instance.poster_file._committed)
        if uploading:
            storage = instance.poster_file.storage
            if not isinstance(storage, (StoragePoolStorage, FileSystemStorage)) or getattr(storage, "_allow_overwrite", False):
                raise MediaAdmissionError()
            new_sizes["poster_file"] = instance.poster_file.size
        elif instance.poster_file:
            media_id = managed_id(file_name(instance))
            target = MediaObject.objects.filter(pk=media_id).first() if media_id else None
            if target is None and previous and file_name(previous) == file_name(instance):
                new_sizes["poster_file"] = old_sizes["poster_file"]
            elif target is None:
                raise MediaAdmissionError()
            else:
                targets["poster_file"] = target
                new_sizes["poster_file"] = trustworthy_size(target)
        else:
            new_sizes["poster_file"] = 0
        if instance.custom_poster_url:
            unchanged_ref = old_refs.get("custom_poster_url") if previous and previous.custom_poster_url == instance.custom_poster_url else None
            if unchanged_ref:
                targets["custom_poster_url"] = unchanged_ref.media
            else:
                candidates = resolve_url_candidates(instance.custom_poster_url)
                owned = [media for media in candidates if owns_media(media, instance.user_id)]
                if owned and len(candidates) != 1:
                    raise MediaAdmissionError()
                if owned:
                    targets["custom_poster_url"] = owned[0]
            new_sizes["custom_poster_url"] = trustworthy_size(targets.get("custom_poster_url")) if "custom_poster_url" in targets else 0
        else:
            new_sizes["custom_poster_url"] = 0
        ids = {ref.media_id for ref in old_refs.values()} | {media.pk for media in targets.values()}
        locked = {media.pk: media for media in MediaObject.objects.select_for_update(of=("self",)).filter(pk__in=ids).order_by("pk")}
        for slot, target in list(targets.items()):
            target = locked.get(target.pk)
            if target is None:
                raise MediaAdmissionError()
            prior = old_refs.get(slot)
            unchanged = prior is not None and prior.media_id == target.pk and prior.owner_id == instance.user_id
            if not unchanged and not owns_media(target, instance.user_id):
                raise MediaAdmissionError()
            if not unchanged:
                new_sizes[slot] = verify_new_holding(target)
            elif target.lifecycle == MediaObject.Lifecycle.DELETING:
                raise MediaAdmissionError()
            if target.lifecycle == MediaObject.Lifecycle.DELETE_FAILED:
                target.lifecycle = MediaObject.Lifecycle.ACTIVE
                target.cleanup_error = ""
                target.save(update_fields=["lifecycle", "cleanup_error"])
            # Persist the proven owner while its original business association
            # still exists. A later failed final cleanup must not erase the
            # ownership proof and silently downgrade a retry to LINK_ONLY.
            if target.upload_owner_id is None:
                target.upload_owner_id = instance.user_id
                target.save(update_fields=["upload_owner"])
            targets[slot] = target
        _admit(before, old_sizes, new_sizes)
        try:
            result = save()
        finally:
            bind_local_upload(instance, "poster_file", uploading=uploading)
        if uploading and isinstance(instance.poster_file.storage, StoragePoolStorage):
            target = MediaObject.objects.get(pk=managed_id(file_name(instance)))
            target.upload_owner_id = instance.user_id
            target.save(update_fields=["upload_owner"])
            targets["poster_file"] = target
        for slot in SLOTS:
            target = targets.get(slot)
            if target is None:
                instance.media_references.filter(slot=slot).delete()
            else:
                JournalMediaReference.objects.update_or_create(
                    entry=instance, slot=slot,
                    defaults={"owner_id": instance.user_id, "media": target,
                              "value": file_name(instance) if slot == "poster_file" else instance.custom_poster_url},
                )
        # update_or_create replacement is an UPDATE, so schedule old IDs too.
        for media_id in {ref.media_id for ref in old_refs.values()}:
            schedule_media_cleanup(media_id)
        if previous and file_name(previous) != file_name(instance):
            old_id = managed_id(file_name(previous))
            if old_id:
                schedule_media_cleanup(old_id)
        mark_media_reference_committed(file_name(instance))
        return result


def has_live_references(media):
    name = media.reference_name
    return (
        media.journal_references.exists()
        or JournalEntry.objects.filter(poster_file=name).exists()
        or UserSettings.objects.filter(avatar=name).exists()
        or Column.objects.filter(cover=name).exists()
        or SiteSettings.objects.filter(site_avatar=name).exists()
        or MediaWriteReservation.objects.filter(pk=media.pk, status__in=["pending", "cleanup_ready", "cleanup_failed"]).exists()
    )


def cleanup_media(media_id):
    """Claim deletion durably, then do I/O; every new holding locks this row."""
    from site_config.media_storage.pool import StoragePoolService

    with transaction.atomic():
        media = MediaObject.objects.select_for_update(of=("self",)).filter(pk=media_id).first()
        if media is None or not media.reference_inventory_complete or has_live_references(media):
            return False
        # A prior process can stop after committing DELETING or after deleting
        # bytes. Repeating deletion is safe: this state rejects every new hold,
        # and the finalized physical receipt permanently prevents key reuse.
        # No elapsed-time or guessed liveness assertion releases its protection.
        media.lifecycle = MediaObject.Lifecycle.DELETING
        media.cleanup_error = ""
        media.save(update_fields=["lifecycle", "cleanup_error"])
    with transaction.atomic():
        current = MediaObject.objects.select_for_update(of=("self",)).filter(pk=media_id).first()
        if current is None or current.lifecycle != MediaObject.Lifecycle.DELETING or not current.reference_inventory_complete or has_live_references(current):
            return False
        # Serialize actual I/O as well as the claim. Otherwise one retry could
        # fail and reopen admission while another retry is still deleting bytes.
        # DELETING was committed above, so a process exit here remains resumable.
        try:
            StoragePoolService.adapter_for(current.storage_backend).delete(current.object_key)
        except Exception as error:
            current.lifecycle = MediaObject.Lifecycle.DELETE_FAILED
            current.cleanup_error = type(error).__name__[:120]
            current.save(update_fields=["lifecycle", "cleanup_error"])
            return False
        current.delete()
        return True


def schedule_media_cleanup(media_id):
    def after_commit():
        try:
            cleanup_media(media_id)
        except Exception as error:
            logger.warning("media_cleanup_deferred", extra={"animemo_exception_class": type(error).__name__})

    transaction.on_commit(after_commit)
