import logging
import threading
from contextlib import contextmanager

from django.core.files.base import File
from django.core.files.storage import Storage
from django.db import DatabaseError, InterfaceError, transaction
from django.utils.deconstruct import deconstructible

from site_config.models import MediaObject

from .pool import StoragePoolService


logger = logging.getLogger(__name__)
_pending_uploads = threading.local()


def _pending_map():
    if not hasattr(_pending_uploads, "items"):
        _pending_uploads.items = {}
    return _pending_uploads.items


def mark_media_reference_committed(name):
    reference = str(name or "")
    if reference in _pending_map():
        transaction.on_commit(lambda: _pending_map().pop(reference, None))


@contextmanager
def atomic_media_mutation():
    """Keep this mutation's uploads recoverable through its outer transaction."""
    previous_references = set(_pending_map())
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


def cleanup_uncommitted_media_reference(name):
    reference = str(name or "")
    pending = _pending_map().pop(reference, None)
    if not pending:
        return
    media_id, backend, object_key, size_bytes, adapter = pending
    try:
        adapter.delete(object_key)
    except Exception:
        pass
    try:
        MediaObject.objects.filter(pk=media_id).delete()
    except Exception:
        pass


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
