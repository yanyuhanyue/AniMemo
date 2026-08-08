import uuid

from django.conf import settings
from django.db import models

from .crypto import decrypt_connection_secret, encrypt_connection_secret


class IntegrationConnection(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    provider = models.SlugField(max_length=64)
    instance_id = models.CharField(max_length=128)
    name = models.CharField(max_length=160)
    key_id = models.CharField(max_length=64, unique=True)
    encrypted_secret = models.TextField()
    enabled = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    last_seen_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["provider", "instance_id"]
        constraints = [
            models.UniqueConstraint(
                fields=("provider", "instance_id"),
                name="integration_provider_instance_uniq",
            ),
        ]

    def __str__(self):
        return f"{self.provider}:{self.instance_id}"

    def set_secret(self, secret):
        self.encrypted_secret = encrypt_connection_secret(secret)

    def get_secret(self):
        return decrypt_connection_secret(self.encrypted_secret)


class ExternalIdentityBinding(models.Model):
    connection = models.ForeignKey(
        IntegrationConnection,
        on_delete=models.CASCADE,
        related_name="bindings",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="external_identity_bindings",
    )
    platform = models.CharField(max_length=64)
    external_user_id = models.CharField(max_length=255)
    display_name = models.CharField(max_length=160, blank=True, default="")
    enabled = models.BooleanField(default=True)
    allow_group_delivery = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    verified_at = models.DateTimeField()

    class Meta:
        ordering = ["connection__provider", "platform", "external_user_id"]
        constraints = [
            models.UniqueConstraint(
                fields=("connection", "platform", "external_user_id"),
                name="integration_external_identity_uniq",
            ),
        ]


class IntegrationPairingCode(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    connection = models.ForeignKey(
        IntegrationConnection,
        on_delete=models.CASCADE,
        related_name="pairing_codes",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="integration_pairing_codes",
    )
    code_lookup = models.CharField(max_length=64)
    code_hash = models.CharField(max_length=256)
    expires_at = models.DateTimeField()
    consumed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=("connection", "code_lookup"),
                name="integration_pairing_lookup_uniq",
            ),
        ]
        indexes = [
            models.Index(
                fields=("connection", "consumed_at", "expires_at"),
                name="integration_pairing_active_idx",
            ),
        ]


class IntegrationEvent(models.Model):
    class RouteType(models.TextChoices):
        PRIVATE = "private", "私聊"

    connection = models.ForeignKey(
        IntegrationConnection,
        on_delete=models.CASCADE,
        related_name="events",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="integration_events",
    )
    platform = models.CharField(max_length=64)
    external_user_id = models.CharField(max_length=255)
    plugin_slug = models.SlugField(max_length=80)
    event_name = models.CharField(max_length=80)
    payload = models.JSONField(default=dict)
    route_type = models.CharField(
        max_length=16,
        choices=RouteType.choices,
        default=RouteType.PRIVATE,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    acked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["id"]
        indexes = [
            models.Index(
                fields=("connection", "id"),
                name="integration_event_cursor_idx",
            ),
            models.Index(
                fields=("acked_at", "created_at"),
                name="integration_event_cleanup_idx",
            ),
        ]


class IntegrationActionReceipt(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "处理中"
        COMPLETED = "completed", "已完成"
        FAILED = "failed", "失败"

    connection = models.ForeignKey(
        IntegrationConnection,
        on_delete=models.CASCADE,
        related_name="action_receipts",
    )
    request_id = models.CharField(max_length=128)
    action = models.CharField(max_length=180)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    response_status = models.PositiveSmallIntegerField(null=True, blank=True)
    response_payload = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=("connection", "request_id"),
                name="integration_action_request_uniq",
            ),
        ]
