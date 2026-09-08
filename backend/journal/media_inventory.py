"""Read-only media inventory and explicitly invoked, idempotent backfill.

Legacy bytes and business fields are never deleted or rewritten. Ambiguous
objects stay outside the cleanup gate; completing one batch is not completion
of the inventory. The same inspector is used by Restore's integrity gate.
"""

from django.db import transaction

from site_config.media_storage.identity import absolute_public_url, resolve_url_candidates, url_identity_digest
from site_config.media_storage.pool import StoragePoolService
from site_config.models import MediaObject, MediaStorageBackend, MediaWriteReservation

from .media_references import (
    MediaAdmissionError, SLOTS, file_name, lock_media_owner, managed_id,
    owner_evidence, poster_usage, trustworthy_size, verify_new_holding,
)
from .models import Column, JournalEntry, JournalMediaReference, UserSettings


def _bytes_issue(media):
    if trustworthy_size(media) is None:
        return "UNKNOWN_USAGE"
    if media.lifecycle == MediaObject.Lifecycle.DELETING:
        return "MISSING_OR_INVALID_BYTES"
    if media.storage_backend.backend_type != MediaStorageBackend.BackendType.LOCAL:
        return "UNVERIFIED_REMOTE_BYTES"
    try:
        verify_new_holding(media)
    except MediaAdmissionError:
        return "MISSING_OR_INVALID_BYTES"
    return None


def expected_holding(entry, slot, byte_issues=None):
    """Return an exact existing business association, or a diagnostic."""
    value = file_name(entry) if slot == "poster_file" else entry.custom_poster_url
    if not value:
        return None, None, []
    if slot == "poster_file":
        media_id = managed_id(value)
        media = MediaObject.objects.filter(pk=media_id).first() if media_id else None
        if media is None:
            return None, "MISSING_MEDIA_ROW", []
        issue = byte_issues.get(media.pk) if byte_issues is not None else _bytes_issue(media)
        # An existing direct field already proves its association. Remote byte
        # health remains unverified; it cannot establish a new URL holding.
        return media, None if issue == "UNVERIFIED_REMOTE_BYTES" else issue, [media.pk]
    candidates = resolve_url_candidates(value)
    if not candidates:
        return None, "LINK_ONLY", []
    ids = [media.pk for media in candidates]
    if len(candidates) != 1:
        return None, "AMBIGUOUS_URL", ids
    media = candidates[0]
    owners = owner_evidence(media)
    if not owners:
        return None, "OWNER_UNPROVEN", ids
    if len(owners) != 1:
        return None, "OWNER_CONFLICT", ids
    if owners != {entry.user_id}:
        return None, "LINK_ONLY", []
    issue = byte_issues.get(media.pk) if byte_issues is not None else _bytes_issue(media)
    return (None, issue, ids) if issue else (media, None, ids)


def inspect_media_references():
    """Never writes DB/files, changes ownership, or calls a remote adapter."""
    objects, byte_issues = {}, {}
    for media in MediaObject.objects.select_related("storage_backend").order_by("pk"):
        issue = _bytes_issue(media)
        byte_issues[media.pk] = issue
        objects[media.pk] = {
            "media_id": str(media.pk), "storage_backend_id": str(media.storage_backend_id),
            "object_key": media.object_key, "size_bytes": trustworthy_size(media),
            "upload_owner_id": media.upload_owner_id,
            "inventory_complete": media.reference_inventory_complete,
            "lifecycle": media.lifecycle,
            "byte_verification": issue or "VERIFIED_LOCAL_BYTES",
            "issues": [issue] if issue and issue != "UNVERIFIED_REMOTE_BYTES" else [],
        }
    expected = {}
    slots = []
    for entry in JournalEntry.objects.all().order_by("pk").iterator(chunk_size=200):
        for slot in SLOTS:
            value = file_name(entry) if slot == "poster_file" else entry.custom_poster_url
            if not value:
                continue
            media, issue, candidates = expected_holding(entry, slot, byte_issues)
            row = {"entry_id": entry.pk, "owner_id": entry.user_id, "slot": slot,
                   "value": value, "soft_deleted": entry.deleted_at is not None,
                   "media_id": str(media.pk) if media else None,
                   "issue": issue, "candidate_ids": [str(pk) for pk in candidates]}
            slots.append(row)
            if media:
                expected[(entry.pk, slot)] = (entry.user_id, media.pk, value)
            if issue and issue != "LINK_ONLY":
                for media_id in candidates:
                    objects[media_id]["issues"].append(issue)
    actual = {}
    for ref in JournalMediaReference.objects.all().order_by("pk"):
        key = (ref.entry_id, ref.slot)
        actual[key] = (ref.owner_id, ref.media_id, ref.value)
        if expected.get(key) != actual[key] and ref.media_id in objects:
            objects[ref.media_id]["issues"].append("REFERENCE_MISMATCH")
    for key, value in expected.items():
        if actual.get(key) != value:
            objects[value[1]]["issues"].append("REFERENCE_MISSING" if key not in actual else "REFERENCE_MISMATCH")
    # Other existing image roles are not charged, but missing role bytes or a
    # malformed identity must remain visible in Restore/inventory diagnostics.
    roles = []
    from site_config.models import SiteSettings

    for model, field in ((UserSettings, "avatar"), (Column, "cover"), (SiteSettings, "site_avatar")):
        for pk, value in model.objects.exclude(**{field: ""}).exclude(**{field: None}).values_list("pk", field):
            media_id = managed_id(value)
            issue = "MISSING_MEDIA_ROW" if media_id not in objects else None
            roles.append({"model": model._meta.label, "pk": pk, "field": field, "media_id": str(media_id) if media_id else None, "issue": issue})
    object_rows = list(objects.values())
    for row in object_rows:
        row["issues"] = sorted(set(row["issues"]))
    unresolved_writes = list(MediaWriteReservation.objects.filter(status__in=["pending", "cleanup_ready", "cleanup_failed"]).order_by("pk").values("id", "status", "size_bytes", "cleanup_error"))
    for row in unresolved_writes:
        row["id"] = str(row["id"])
    issues = [row for row in slots if row["issue"] not in {None, "LINK_ONLY"}]
    ready = not issues and not any(row["issues"] or not row["inventory_complete"] for row in object_rows) and not any(row["issue"] for row in roles) and not unresolved_writes
    return {
        "status": "READY" if ready else "BLOCKED", "read_only": True,
        "objects": object_rows, "slots": slots, "other_roles": roles,
        "unresolved_writes": unresolved_writes,
        "usage": [{"owner_id": owner_id, "status": (usage := poster_usage(owner_id)).status,
                   "known_bytes": usage.known_bytes, "unknown_slots": list(usage.unknown_slots)}
                  for owner_id in JournalEntry.objects.order_by("user_id").values_list("user_id", flat=True).distinct()],
    }


def backfill_media_references(*, after_entry=0, limit=200):
    if after_entry < 0 or not 1 <= limit <= 1000:
        raise ValueError("Backfill requires after_entry >= 0 and 1 <= limit <= 1000")
    entry_ids = list(JournalEntry.objects.filter(pk__gt=after_entry).order_by("pk").values_list("pk", flat=True)[:limit])
    for entry_id in entry_ids:
        owner_id = JournalEntry.objects.filter(pk=entry_id).values_list("user_id", flat=True).first()
        if owner_id is None:
            continue
        with transaction.atomic():
            lock_media_owner(owner_id)
            entry = JournalEntry.objects.select_for_update().filter(pk=entry_id, user_id=owner_id).first()
            if entry is None:
                continue
            proposals = {}
            ids = set()
            for slot in SLOTS:
                media, issue, candidates = expected_holding(entry, slot)
                ids.update(candidates)
                # Direct ImageField identity is already a persisted association;
                # retain its protection even when its size/bytes need repair.
                if media is not None and (issue is None or slot == "poster_file"):
                    proposals[slot] = media.pk
            locked = {media.pk: media for media in MediaObject.objects.select_for_update(of=("self",)).filter(pk__in=ids).order_by("pk")}
            for slot, media_id in proposals.items():
                media = locked.get(media_id)
                if media is None or media.lifecycle == MediaObject.Lifecycle.DELETING:
                    continue
                # Re-evaluate after lifecycle serialization; an earlier read is
                # never sufficient to establish a custom URL holding.
                verified, issue, _candidates = expected_holding(entry, slot)
                if verified is None or verified.pk != media_id or (issue and slot != "poster_file"):
                    continue
                value = file_name(entry) if slot == "poster_file" else entry.custom_poster_url
                JournalMediaReference.objects.get_or_create(
                    entry=entry, slot=slot,
                    defaults={"owner_id": owner_id, "media": media, "value": value},
                )
                if media.upload_owner_id is None and owner_evidence(media) == {owner_id}:
                    media.upload_owner_id = owner_id
                    media.save(update_fields=["upload_owner"])
                if not media.public_url_snapshot:
                    # Always build from server configuration, never adopt the
                    # historical client URL as a new authority statement.
                    url = absolute_public_url(StoragePoolService.adapter_for(media.storage_backend).url(media.object_key))
                    media.public_url_snapshot = url
                    media.public_url_identity = url_identity_digest(url)
                    media.save(update_fields=["public_url_snapshot", "public_url_identity"])
    last = entry_ids[-1] if entry_ids else after_entry
    more = JournalEntry.objects.filter(pk__gt=last).exists()
    inventory = inspect_media_references()
    candidate_entries_by_media = {}
    for slot in inventory["slots"]:
        for media_id in slot["candidate_ids"]:
            candidate_entries_by_media.setdefault(media_id, set()).add(slot["entry_id"])
    # Recheck each object under its lifecycle lock before opening its cleanup
    # gate. New writes take the same object lock and maintain references.
    for row in inventory["objects"]:
        with transaction.atomic():
            media = MediaObject.objects.select_for_update(of=("self",)).filter(pk=row["media_id"]).first()
            if media is None:
                continue
            # A committed cleanup claim has already passed this gate and denies
            # new attachments. Re-running backfill must not erase the proof
            # needed to finish an interrupted deletion.
            if media.lifecycle == MediaObject.Lifecycle.DELETING:
                continue
            current = inspect_object_references(media, candidate_entries_by_media.get(str(media.pk), ()))
            complete = not more and not current
            media.reference_inventory_complete = complete
            media.inventory_error = current[0] if current else ("BACKFILL_INCOMPLETE" if more else "")
            media.save(update_fields=["reference_inventory_complete", "inventory_error"])
    result = inspect_media_references()
    result.update(read_only=False, processed_entries=len(entry_ids), next_after_entry=last, more=more)
    if more:
        result["status"] = "INCOMPLETE"
    return result


def inspect_object_references(media, candidate_entries=()):
    """Lifecycle-lock-local check; read business rows without reversing locks."""
    issues = []
    bytes_issue = _bytes_issue(media)
    if bytes_issue and bytes_issue != "UNVERIFIED_REMOTE_BYTES":
        issues.append(bytes_issue)
    actual = {(ref.entry_id, ref.slot): ref for ref in media.journal_references.select_related("entry")}
    # The initial full inventory finds legacy URL candidates. Current direct
    # fields and held rows cover writes committed since that snapshot; a new
    # owner holding cannot pass this same object lock without creating its row.
    entry_ids = set(candidate_entries) | {entry_id for entry_id, _slot in actual}
    entry_ids.update(JournalEntry.objects.filter(poster_file=media.reference_name).values_list("pk", flat=True))
    for entry in JournalEntry.objects.filter(pk__in=entry_ids).order_by("pk"):
        for slot in SLOTS:
            value = file_name(entry) if slot == "poster_file" else entry.custom_poster_url
            if not value:
                continue
            target, issue, ids = expected_holding(entry, slot, {media.pk: bytes_issue})
            if media.pk not in ids:
                continue
            if issue and issue != "LINK_ONLY":
                issues.append(issue)
            if target is not None and target.pk == media.pk:
                ref = actual.pop((entry.pk, slot), None)
                if ref is None:
                    issues.append("REFERENCE_MISSING")
                elif (ref.owner_id, ref.value) != (entry.user_id, value):
                    issues.append("REFERENCE_MISMATCH")
    if actual:
        issues.append("REFERENCE_MISMATCH")
    return sorted(set(issues))
