import threading

from django.core.files.base import File
from django.core.files.storage import Storage
from django.utils.deconstruct import deconstructible

from .pool import StoragePoolService
from site_config.models import MediaObject


_pending_uploads = threading.local()


def _pending_map():
    if not hasattr(_pending_uploads, "items"):
        _pending_uploads.items = {}
    return _pending_uploads.items


def mark_media_reference_committed(name):
    _pending_map().pop(str(name or ""), None)


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
