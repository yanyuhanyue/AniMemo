import logging
import os
import threading
import uuid
from contextlib import contextmanager

from django.core.files.base import File
from django.core.files.storage import FileSystemStorage, Storage
from django.db import DatabaseError, InterfaceError, transaction
from django.utils.deconstruct import deconstructible

from site_config.models import MediaObject

from .pool import StoragePoolService
from .receipts import cleanup_rolled_back_upload


logger = logging.getLogger(__name__)
_pending_uploads = threading.local()


def _pending_map():
    if not hasattr(_pending_uploads, "items"):
        _pending_uploads.items = {}
    return _pending_uploads.items


def _local_uploads():
    if not hasattr(_pending_uploads, "local_files"):
        _pending_uploads.local_files = {}
    return _pending_uploads.local_files


def remember_local_upload(storage, name, stat):
    token = uuid.uuid4().hex
    _local_uploads()[token] = {
        "storage": storage, "name": name, "path": storage.path(name),
        "identity": (stat.st_dev, stat.st_ino), "binding": None,
    }
    return token


def bind_local_upload(instance, field_name, *, uploading):
    field = getattr(instance, field_name)
    if not uploading or not field or not field._committed or not isinstance(field.storage, FileSystemStorage):
        return
    path = field.storage.path(field.name)
    token = next((key for key, value in _local_uploads().items() if value["path"] == path), None)
    if token is None:
        # Explicit legacy FileSystemStorage also returns an exclusively created
        # final name after success. The default development subclass additionally
        # records ownership before its first write, covering partial failures.
        if getattr(field.storage, "_allow_overwrite", False):
            return
        token = remember_local_upload(field.storage, field.name, os.stat(path))
    _local_uploads()[token]["binding"] = (instance._meta.label, instance.pk, field_name)
    transaction.on_commit(lambda: _local_uploads().pop(token, None))


def cleanup_local_upload(token):
    pending = _local_uploads().get(token)
    if pending is None:
        return
    from journal.models import Column, JournalEntry, UserSettings
    from site_config.models import SiteSettings

    try:
        referenced = any(model.objects.filter(**{field: pending["name"]}).exists() for model, field in (
            (JournalEntry, "poster_file"), (UserSettings, "avatar"),
            (Column, "cover"), (SiteSettings, "site_avatar"),
        ))
        if referenced:
            if not transaction.get_connection().in_atomic_block:
                _local_uploads().pop(token, None)
            return
        try:
            current = os.stat(pending["path"], follow_symlinks=False)
        except FileNotFoundError:
            _local_uploads().pop(token, None)
            return
        if (current.st_dev, current.st_ino) != pending["identity"]:
            logger.warning("development_upload_identity_changed")
            return
        pending["storage"].delete(pending["name"])
        _local_uploads().pop(token, None)
    except (DatabaseError, InterfaceError, OSError) as error:
        logger.warning("development_upload_cleanup_deferred", extra={"animemo_exception_class": type(error).__name__})


def mark_media_reference_committed(name):
    reference = str(name or "")
    if reference in _pending_map():
        transaction.on_commit(lambda: _pending_map().pop(reference, None))


@contextmanager
def atomic_media_mutation():
    """Keep this mutation's uploads recoverable through its outer transaction."""
    previous_references = set(_pending_map())
    previous_local = set(_local_uploads())
    try:
        with transaction.atomic():
            yield
    finally:
        # Run after atomic has committed or rolled back, including exceptions
        # raised by on_commit callbacks. An earlier callback can prevent the
        # pending marker from running even though the media row has committed.
        for reference in set(_pending_map()) - previous_references:
            media_id = _pending_map()[reference][0]
            try:
                row_exists = MediaObject.objects.filter(pk=media_id).exists()
            except (DatabaseError, InterfaceError) as error:
                # A surrounding broken transaction must unwind before its
                # owning media scope can establish that this upload rolled back.
                logger.warning(
                    "media_rollback_cleanup_deferred",
                    extra={"animemo_exception_class": type(error).__name__},
                )
                continue
            if not row_exists:
                cleanup_uncommitted_media_reference(reference)
            elif not transaction.get_connection().in_atomic_block:
                _pending_map().pop(reference, None)
        for token in set(_local_uploads()) - previous_local:
            cleanup_local_upload(token)


def cleanup_uncommitted_media_reference(name):
    reference = str(name or "")
    pending = _pending_map().get(reference)
    if not pending:
        return
    media_id, backend, object_key, size_bytes, adapter, reservation = pending
    cleanup_rolled_back_upload(reservation, adapter)
    _pending_map().pop(reference, None)


def save_model_image(instance, field_name, save, *, owner_id=None, update_fields=None):
    """Own a model save's physical upload until its business row commits.

    Callers adding later writes must wrap their complete operation in
    atomic_media_mutation as well (serializers and Admin do this).
    """
    field = getattr(instance, field_name)
    if update_fields is not None and field_name not in update_fields:
        return save()
    uploading = bool(field and not field._committed)
    previous = type(instance).objects.filter(pk=instance.pk).first() if instance.pk else None
    old_name = str(getattr(getattr(previous, field_name, None), "name", "") or "")
    if not uploading and str(field or "") == old_name:
        return save()
    with atomic_media_mutation():
        if owner_id is not None:
            from django.contrib.auth import get_user_model

            get_user_model().objects.select_for_update().only("pk").get(pk=owner_id)
        if previous is not None:
            previous = type(instance).objects.select_for_update().get(pk=previous.pk)
            old_name = str(getattr(getattr(previous, field_name), "name", "") or "")
        try:
            result = save()
        finally:
            bind_local_upload(instance, field_name, uploading=uploading)
        name = str(getattr(instance, field_name).name or "")
        if uploading and owner_id is not None:
            media = StoragePoolService.resolve_reference(name)
            if media is not None:
                media.upload_owner_id = owner_id
                media.save(update_fields=["upload_owner"])
        mark_media_reference_committed(name)
        if old_name and old_name != name:
            transaction.on_commit(lambda: StoragePoolService.delete_reference(old_name))
        return result


@deconstructible
class StoragePoolStorage(Storage):
    """Django storage facade backed by MediaObject identity records."""

    def _open(self, name, mode="rb"):
        if mode not in {"r", "rb"}:
            raise ValueError("媒体存储只支持只读打开。")
        return File(StoragePoolService.open_reference(name), name=name)

    def _save(self, name, content):
        try:
            content.seek(0)
        except (AttributeError, OSError):
            pass
        data = content.read()
        media = StoragePoolService.create_media(
            name,
            data,
            content_type=getattr(content, "content_type", "application/octet-stream"),
        )
        _pending_map()[media.reference_name] = (
            media.pk,
            media.storage_backend,
            media.object_key,
            media.size_bytes,
            media._storage_adapter,
            media._write_reservation,
        )
        return media.reference_name

    def delete(self, name):
        StoragePoolService.delete_reference(name)

    def exists(self, name):
        media = StoragePoolService.resolve_reference(name)
        if media is None:
            return False
        return StoragePoolService.adapter_for(media.storage_backend).exists(media.object_key)

    def size(self, name):
        media = StoragePoolService.resolve_reference(name)
        if media is None:
            raise FileNotFoundError(name)
        return media.size_bytes

    def url(self, name):
        return StoragePoolService.url_for_reference(name)

    def get_available_name(self, name, max_length=None):
        # upload_to already uses UUID names; MediaObject identity guarantees the
        # reference itself is unique even when source filenames collide.
        return name
