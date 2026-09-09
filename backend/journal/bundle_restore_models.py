"""Owner-bound portable restore state, separate from provider import sessions."""
import uuid

from django.conf import settings
from django.db import models


class BundleRestoreSession(models.Model):
    class State(models.TextChoices):
        RECEIVING = "receiving"
        VALIDATING = "validating"
        READY = "ready"
        COMMITTING = "committing"
        COMPLETED = "completed"
        CANCELLED = "cancelled"
        FAILED = "failed"
        EXPIRED = "expired"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    # Keep physical cleanup authority when an account is deleted.
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True)
    idempotency_key = models.UUIDField()
    state = models.CharField(max_length=16, choices=State.choices, default=State.RECEIVING)
    generation = models.PositiveIntegerField(default=1)
    expected_bytes = models.PositiveBigIntegerField()
    sha256 = models.CharField(max_length=64)
    schema_version = models.PositiveSmallIntegerField(default=1)
    received_bytes = models.PositiveBigIntegerField(default=0)
    normalized_bytes = models.PositiveBigIntegerField(default=0)
    normalized_sha256 = models.CharField(max_length=64, blank=True)
    validator_version = models.CharField(max_length=64, blank=True)
    normalized_limit = models.PositiveBigIntegerField()
    single_limit = models.PositiveBigIntegerField()
    index_limit = models.PositiveBigIntegerField()
    reserved_bytes = models.PositiveBigIntegerField(default=0)
    physical_bytes = models.PositiveBigIntegerField(default=0)
    preview = models.JSONField(default=dict)
    receipt = models.JSONField(default=dict)
    error_code = models.CharField(max_length=64, blank=True)
    cleanup_pending = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    touched_at = models.DateTimeField(auto_now_add=True)
    operation_expires_at = models.DateTimeField(null=True)
    finished_at = models.DateTimeField(null=True)

    class Meta:
        app_label = "journal"
        constraints = (
            models.UniqueConstraint(fields=["owner", "idempotency_key"], name="bundle_restore_owner_key"),
            models.UniqueConstraint(fields=["owner"], condition=models.Q(
                state__in=["receiving", "validating", "ready", "committing"]
            ), name="bundle_restore_one_open"),
        )
        indexes = (models.Index(fields=["state", "touched_at"], name="bundle_restore_lifecycle"),)


class BundleRestoreChunk(models.Model):
    session = models.ForeignKey(BundleRestoreSession, on_delete=models.CASCADE, related_name="chunks")
    offset = models.PositiveBigIntegerField()
    size = models.PositiveIntegerField()
    sha256 = models.CharField(max_length=64)

    class Meta:
        app_label = "journal"
        constraints = (models.UniqueConstraint(fields=["session", "offset"], name="bundle_restore_chunk_offset"),)


class BundleRestoreAdmission(models.Model):
    """Short database mutex for this feature's disk and operation admission."""
    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)

    class Meta:
        app_label = "journal"
        constraints = (models.CheckConstraint(condition=models.Q(id=1), name="bundle_restore_single_admission"),)
