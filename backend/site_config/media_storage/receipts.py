"""Durable accounting for the existing physical media write reservation.

PostgreSQL commits the physical receipt independently of a caller's business
transaction. No owner quota reservation, lease or background worker is used.
SQLite development mode can only compensate after its outer writer unwinds.
"""

from contextlib import contextmanager

from django.db import connections, transaction
from django.utils import timezone

from site_config.models import MediaObject, MediaWriteReservation


OCCUPYING_STATUSES = ("pending", "cleanup_ready", "cleanup_failed")


@contextmanager
def physical_transaction():
    original = connections["default"]
    independent = original.copy(alias="default") if original.vendor == "postgresql" else None
    try:
        if independent is not None:
            connections["default"] = independent
        with transaction.atomic():
            yield
    finally:
        if independent is not None:
            connections["default"] = original
            independent.close()


def cleanup_rolled_back_upload(reservation, adapter):
    """Only the upload's completed/failed caller may prove it is no longer live."""
    with physical_transaction():
        current, _ = MediaWriteReservation.objects.get_or_create(
            pk=reservation.pk,
            defaults={
                "storage_backend_id": reservation.storage_backend_id,
                "object_key": reservation.object_key, "size_bytes": reservation.size_bytes,
                "sha256": reservation.sha256, "content_type": reservation.content_type,
                "expires_at": reservation.expires_at,
            },
        )
        current = MediaWriteReservation.objects.select_for_update().get(pk=current.pk)
        if current.status in {"finalized", "abandoned"} or MediaObject.objects.filter(pk=current.pk).exists():
            return
        current.status = MediaWriteReservation.Status.CLEANUP_READY
        current.save(update_fields=["status"])
    retry_receipt_cleanup(current.pk, adapter=adapter)


def retry_receipt_cleanup(reservation_id, *, adapter=None):
    from .pool import StoragePoolService

    # Receipt I/O is serialized against finalization. Pending/unknown writers
    # are deliberately excluded, even when their old expires_at is in the past.
    with physical_transaction():
        current = MediaWriteReservation.objects.select_for_update(of=("self",)).get(pk=reservation_id)
        if current.status not in {"cleanup_ready", "cleanup_failed"}:
            return False
        if MediaObject.objects.filter(pk=current.pk).exists():
            return False
        try:
            selected = adapter or StoragePoolService.adapter_for(current.storage_backend)
            from .local import DynamicLocalBackend

            if isinstance(selected, DynamicLocalBackend):
                selected.delete_owned_write(current.object_key, current.pk)
            else:
                selected.delete(current.object_key)
        except Exception as error:
            current.status = MediaWriteReservation.Status.CLEANUP_FAILED
            current.cleanup_error = type(error).__name__[:120]
        else:
            current.status = MediaWriteReservation.Status.ABANDONED
            current.abandoned_at = timezone.now()
            current.cleanup_error = ""
        current.save(update_fields=["status", "abandoned_at", "cleanup_error"])
        return current.status == "abandoned"
