import uuid
import secrets

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q

from .storage_units import BINARY_GIB_BYTES, DECIMAL_GB_BYTES


def default_trusted_poster_hosts():
    return ["lain.bgm.tv", "img.re-anime.cc", "re-anime.cc"]


def site_avatar_upload_to(_instance, filename):
    return f"site/avatar/{uuid.uuid4().hex}-{filename}"


class InstallationState(models.Model):
    class Status(models.TextChoices):
        UNINITIALIZED = "uninitialized", "未初始化"
        INITIALIZING = "initializing", "初始化中"
        INITIALIZED = "initialized", "已初始化"

    status = models.CharField(max_length=16, choices=Status.choices, default=Status.UNINITIALIZED)
    setup_code_hash = models.CharField(max_length=256, blank=True, default="", editable=False)
    setup_code_issued_at = models.DateTimeField(blank=True, null=True, editable=False)
    setup_code_expires_at = models.DateTimeField(blank=True, null=True, editable=False)
    failed_attempts = models.PositiveSmallIntegerField(default=0, editable=False)
    authentication_epoch = models.CharField(
        max_length=64,
        blank=True,
        default="",
        editable=False,
    )
    initialized_at = models.DateTimeField(blank=True, null=True, editable=False)
    initialized_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="initialized_installations",
        editable=False,
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "安装状态"
        verbose_name_plural = "安装状态"

    @classmethod
    def load(cls):
        return cls.objects.get(pk=1)

    @classmethod
    def is_initialized(cls):
        return cls.objects.filter(pk=1, status=cls.Status.INITIALIZED).exists()

    @property
    def accepting_setup(self):
        from django.utils import timezone

        return bool(
            self.status == self.Status.UNINITIALIZED
            and self.setup_code_hash
            and self.setup_code_expires_at
            and self.setup_code_expires_at > timezone.now()
        )

    def save(self, *args, **kwargs):
        self.pk = 1
        if self.status == self.Status.INITIALIZED and not self.authentication_epoch:
            self.authentication_epoch = secrets.token_hex(32)
            update_fields = kwargs.get("update_fields")
            if update_fields is not None:
                kwargs["update_fields"] = {
                    *update_fields,
                    "authentication_epoch",
                }
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        return None

    def __str__(self):
        return self.get_status_display()


class SiteSettings(models.Model):
    site_name = models.CharField(max_length=120, default="AniMemo")
    homepage_title = models.CharField(max_length=160, default="AniMemo · 我的动漫记忆库")
    homepage_owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, blank=True, null=True, related_name="homepage_site_settings")
    site_avatar = models.ImageField(upload_to=site_avatar_upload_to, blank=True, null=True)
    homepage_description = models.CharField(
        max_length=320,
        default="把想看、在看与看完的作品收进同一条记忆轨迹，随时回望每一次与动画相遇的时刻。",
    )
    universe_description = models.CharField(max_length=320, default="穿过各位同好们的观看轨道，发现真实同步、持续生长的私人番剧宇宙。")
    social_handle = models.CharField(max_length=80, default="X: @ANIMEMO")
    registration_enabled = models.BooleanField(default=True)
    email_delivery_enabled = models.BooleanField(default=True)
    email_sender_name = models.CharField(max_length=120, blank=True, default="")
    email_sender_address = models.EmailField(blank=True, default="")
    trusted_poster_hosts = models.JSONField(default=default_trusted_poster_hosts, blank=True)
    resend_api_key_encrypted = models.TextField(blank=True, default="", editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "站点设置"
        verbose_name_plural = "站点设置"

    @classmethod
    def load(cls):
        return cls.objects.get_or_create(pk=1)[0]

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        return None

    def set_resend_api_key(self, value):
        from journal.secrets import encrypt_secret

        self.resend_api_key_encrypted = encrypt_secret(value.strip())

    def get_resend_api_key(self):
        if self.resend_api_key_encrypted:
            from journal.secrets import decrypt_secret

            return decrypt_secret(self.resend_api_key_encrypted)
        return settings.RESEND_API_KEY

    @property
    def resend_api_key_source(self):
        if self.resend_api_key_encrypted:
            return "database"
        if settings.RESEND_API_KEY:
            return "environment"
        return "none"

    def get_email_from(self):
        if self.email_sender_address:
            name = self.email_sender_name.strip() or self.site_name
            return f"{name} <{self.email_sender_address}>"
        return settings.RESEND_FROM_EMAIL

    def __str__(self):
        return self.site_name


class CloudflareR2Account(models.Model):
    account_id = models.CharField(max_length=64, unique=True)
    name = models.CharField(max_length=120)
    warning_bytes = models.PositiveBigIntegerField(blank=True, null=True)
    write_limit_bytes = models.PositiveBigIntegerField(blank=True, null=True)
    encrypted_analytics_token = models.TextField(blank=True, default="", editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Cloudflare R2 账户"
        verbose_name_plural = "Cloudflare R2 账户"

    def save(self, *args, **kwargs):
        self.account_id = str(self.account_id or "").strip().lower()
        if self.warning_bytes is not None and self.write_limit_bytes is not None:
            if self.warning_bytes <= 0 or self.write_limit_bytes <= self.warning_bytes:
                raise ValueError("Cloudflare Account 阈值必须满足 0 < warning_bytes < write_limit_bytes。")
        super().save(*args, **kwargs)

    def set_analytics_token(self, value):
        from config.credentials import CredentialCipher
        self.encrypted_analytics_token = CredentialCipher.encrypt(str(value or "").strip())

    def get_analytics_token(self):
        from config.credentials import CredentialCipher
        return CredentialCipher.decrypt(self.encrypted_analytics_token)

    @property
    def analytics_token_configured(self):
        return bool(self.encrypted_analytics_token)

    def __str__(self):
        return self.name or self.account_id


class MediaStorageBackend(models.Model):
    class BackendType(models.TextChoices):
        CLOUDFLARE_R2 = "cloudflare_r2", "Cloudflare R2"
        LOCAL = "local", "VPS 本地存储"

    slug = models.SlugField(max_length=64, unique=True)
    name = models.CharField(max_length=120)
    backend_type = models.CharField(max_length=24, choices=BackendType.choices)
    enabled = models.BooleanField(default=True)
    accept_new_writes = models.BooleanField(default=True)
    priority = models.PositiveIntegerField(default=100)
    warning_bytes = models.PositiveBigIntegerField(default=8 * DECIMAL_GB_BYTES)
    write_limit_bytes = models.PositiveBigIntegerField(default=9 * DECIMAL_GB_BYTES)
    config_version = models.PositiveBigIntegerField(default=1)

    bucket_name = models.CharField(max_length=63, blank=True, default="")
    endpoint_url = models.URLField(max_length=300, blank=True, default="")
    public_base_url = models.URLField(max_length=300, blank=True, default="")
    region = models.CharField(max_length=32, blank=True, default="auto")
    encrypted_access_key_id = models.TextField(blank=True, default="", editable=False)
    encrypted_secret_access_key = models.TextField(blank=True, default="", editable=False)
    cloudflare_account_ref = models.ForeignKey(
        CloudflareR2Account,
        on_delete=models.PROTECT,
        blank=True,
        null=True,
        related_name="storage_backends",
        db_column="cloudflare_account_ref_id",
    )
    usage_payload_bytes = models.PositiveBigIntegerField(default=0)
    usage_metadata_bytes = models.PositiveBigIntegerField(default=0)
    usage_object_count = models.PositiveBigIntegerField(default=0)
    usage_refreshed_at = models.DateTimeField(blank=True, null=True)

    local_root = models.CharField(max_length=240, blank=True, default="")
    local_public_base_url = models.URLField(max_length=300, blank=True, default="")
    min_free_warning_bytes = models.PositiveBigIntegerField(default=15 * BINARY_GIB_BYTES)
    min_free_block_bytes = models.PositiveBigIntegerField(default=10 * BINARY_GIB_BYTES)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["priority", "id"]
        verbose_name = "媒体存储后端"
        verbose_name_plural = "媒体存储后端"
        constraints = [
            models.UniqueConstraint(
                fields=["cloudflare_account_ref", "bucket_name"],
                condition=Q(
                    backend_type="cloudflare_r2",
                    cloudflare_account_ref__isnull=False,
                    bucket_name__gt="",
                ),
                name="unique_r2_account_bucket_identity",
            ),
            models.UniqueConstraint(
                fields=["endpoint_url", "bucket_name"],
                condition=Q(
                    backend_type="cloudflare_r2",
                    endpoint_url__gt="",
                    bucket_name__gt="",
                ),
                name="unique_r2_endpoint_bucket_identity",
            ),
            models.UniqueConstraint(
                fields=["local_root"],
                condition=Q(backend_type="local"),
                name="unique_local_media_storage_root",
            ),
        ]

    def physical_identity(self):
        if self.backend_type == self.BackendType.CLOUDFLARE_R2:
            return (
                self.backend_type,
                str(self.bucket_name or "").strip().lower(),
                str(self.endpoint_url or "").strip().rstrip("/").lower(),
                self.cloudflare_account_ref_id,
            )
        if self.backend_type == self.BackendType.LOCAL:
            return (self.backend_type, str(self.local_root or ""))
        return (self.backend_type,)

    def save(self, *args, **kwargs):
        self.bucket_name = str(self.bucket_name or "").strip().lower()
        self.endpoint_url = str(self.endpoint_url or "").strip().rstrip("/").lower()
        if self.backend_type == self.BackendType.LOCAL:
            from .media_storage.local import validate_storage_subpath

            self.local_root = validate_storage_subpath(self.local_root)
        if self.warning_bytes <= 0 or self.write_limit_bytes <= self.warning_bytes:
            raise ValueError("媒体阈值必须满足 0 < warning_bytes < write_limit_bytes。")
        if self.backend_type == self.BackendType.LOCAL and self.min_free_warning_bytes <= self.min_free_block_bytes:
            raise ValueError("本地磁盘警告剩余空间必须大于阻止写入剩余空间。")
        update_fields = kwargs.get("update_fields")
        physical_fields = {"backend_type", "bucket_name", "endpoint_url", "cloudflare_account_ref", "local_root"}
        checks_physical_identity = update_fields is None or bool(physical_fields.intersection(update_fields))
        if self.pk and checks_physical_identity:
            from django.db import transaction

            with transaction.atomic():
                previous = type(self).objects.select_for_update().filter(pk=self.pk).first()
                has_active_reservation = MediaWriteReservation.objects.filter(
                    storage_backend_id=self.pk,
                    status=MediaWriteReservation.Status.PENDING,
                ).exists()
                if previous and previous.physical_identity() != self.physical_identity() and (
                    previous.media_objects.exists() or has_active_reservation
                ):
                    raise ValidationError({
                        "code": "STORAGE_PHYSICAL_IDENTITY_LOCKED",
                        "detail": "Storage contains media objects or an active media write reservation, so its physical location cannot be changed.",
                    })
                super().save(*args, **kwargs)
            return
        super().save(*args, **kwargs)

    def set_access_key_id(self, value):
        from config.credentials import CredentialCipher
        self.encrypted_access_key_id = CredentialCipher.encrypt(value.strip())

    def get_access_key_id(self):
        from config.credentials import CredentialCipher
        return CredentialCipher.decrypt(self.encrypted_access_key_id)

    def set_secret_access_key(self, value):
        from config.credentials import CredentialCipher
        self.encrypted_secret_access_key = CredentialCipher.encrypt(value.strip())

    def get_secret_access_key(self):
        from config.credentials import CredentialCipher
        return CredentialCipher.decrypt(self.encrypted_secret_access_key)

    def get_analytics_token(self):
        if self.cloudflare_account_ref_id and self.cloudflare_account_ref:
            return self.cloudflare_account_ref.get_analytics_token()
        return ""

    @property
    def access_key_configured(self):
        return bool(self.encrypted_access_key_id)

    @property
    def secret_key_configured(self):
        return bool(self.encrypted_secret_access_key)

    @property
    def analytics_token_configured(self):
        return bool(self.cloudflare_account_ref_id and self.cloudflare_account_ref and self.cloudflare_account_ref.analytics_token_configured)

    @property
    def snapshot_bytes(self):
        return int(self.usage_payload_bytes or 0) + int(self.usage_metadata_bytes or 0)

    def __str__(self):
        return self.name


class MediaStoragePoolSettings(models.Model):
    preferred_write_backend = models.ForeignKey(
        MediaStorageBackend,
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="preferred_by_pools",
    )
    updated_at = models.DateTimeField(auto_now=True)

    @classmethod
    def load(cls):
        return cls.objects.get_or_create(pk=1)[0]

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    def __str__(self):
        return "媒体存储池"


class MediaObject(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    storage_backend = models.ForeignKey(MediaStorageBackend, on_delete=models.PROTECT, related_name="media_objects")
    object_key = models.CharField(max_length=500)
    size_bytes = models.PositiveBigIntegerField(default=0)
    content_type = models.CharField(max_length=120, blank=True, default="")
    sha256 = models.CharField(max_length=64, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["storage_backend", "object_key"], name="unique_media_object_location"),
        ]
        indexes = [models.Index(fields=["storage_backend", "created_at"])]

    @property
    def reference_name(self):
        return f"media-objects/{self.pk}"

    def __str__(self):
        return f"{self.storage_backend.slug}:{self.object_key}"


class MediaWriteReservation(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "写入中"
        FINALIZED = "finalized", "已完成"
        ABANDONED = "abandoned", "已放弃"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    storage_backend = models.ForeignKey(
        MediaStorageBackend,
        on_delete=models.PROTECT,
        related_name="media_write_reservations",
    )
    object_key = models.CharField(max_length=500)
    size_bytes = models.PositiveBigIntegerField(default=0)
    content_type = models.CharField(max_length=120, blank=True, default="")
    sha256 = models.CharField(max_length=64, blank=True, default="")
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    finalized_at = models.DateTimeField(blank=True, null=True)
    abandoned_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["status", "expires_at"], name="media_reservation_gc_idx"),
            models.Index(fields=["storage_backend", "status"], name="media_reservation_backend_idx"),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["storage_backend", "object_key"],
                condition=Q(status="pending"),
                name="unique_pending_media_reservation",
            ),
        ]

    def __str__(self):
        return f"{self.storage_backend.slug}:{self.object_key} ({self.status})"


class TagDefinition(models.Model):
    class Color(models.TextChoices):
        PINK = "pink", "粉色"; ROSE = "rose", "玫红"; BLUE = "blue", "蓝色"; EMERALD = "emerald", "翡翠"; AMBER = "amber", "琥珀"; ORANGE = "orange", "橙色"; INDIGO = "indigo", "靛蓝"; VIOLET = "violet", "堇紫"; FUCHSIA = "fuchsia", "洋红"; YELLOW = "yellow", "黄色"; PURPLE = "purple", "紫色"; CYAN = "cyan", "青色"; LIME = "lime", "青柠"; SKY = "sky", "天蓝"; SLATE = "slate", "灰色"

    name = models.CharField(max_length=40, unique=True)
    color = models.CharField(max_length=16, choices=Color.choices, default=Color.SLATE)
    is_quick_preset = models.BooleanField(default=False)
    sort_order = models.PositiveSmallIntegerField(default=0)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, blank=True, null=True, related_name="created_tag_definitions")
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, blank=True, null=True, related_name="updated_tag_definitions")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["sort_order", "id"]
        verbose_name = "公共标签"
        verbose_name_plural = "公共标签"

    def __str__(self):
        return self.name
