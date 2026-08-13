import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models


def poster_upload_to(instance, filename):
    return f"users/{instance.user_id}/posters/{uuid.uuid4().hex}-{filename}"


def avatar_upload_to(instance, filename):
    return f"users/{instance.user_id}/avatars/{uuid.uuid4().hex}-{filename}"


def column_cover_upload_to(instance, filename):
    return f"users/{instance.author_id}/columns/{uuid.uuid4().hex}-{filename}"


class JournalEntry(models.Model):
    class WatchStatus(models.TextChoices):
        COMPLETED = "completed", "看过"
        WATCHING = "watching", "在看"
        PLANNED = "planned", "想看"
        ON_HOLD = "on_hold", "搁置"
        DROPPED = "dropped", "弃番"

    class Visibility(models.TextChoices):
        PRIVATE = "private", "私人"
        UNLISTED = "unlisted", "链接可见"
        PUBLIC = "public", "公开"

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="journal_entries")
    title = models.CharField(max_length=200)
    japanese_title = models.CharField(max_length=200, blank=True)
    airing_period = models.CharField(max_length=50, blank=True)
    studio = models.CharField(max_length=120, blank=True)
    episodes = models.CharField(max_length=30, blank=True)
    description = models.TextField(blank=True)
    poster_url = models.URLField(max_length=1000, blank=True)
    custom_poster_url = models.URLField(max_length=1000, blank=True)
    poster_file = models.ImageField(upload_to=poster_upload_to, blank=True, null=True)
    baike_url = models.URLField(max_length=1000, blank=True)
    tags = models.JSONField(default=list, blank=True)
    tag_colors = models.JSONField(default=dict, blank=True)
    personal_score = models.DecimalField(
        max_digits=4,
        decimal_places=2,
        blank=True,
        null=True,
        validators=[MinValueValidator(0), MaxValueValidator(10)],
    )
    watch_status = models.CharField(max_length=20, choices=WatchStatus.choices, default=WatchStatus.PLANNED)
    review = models.TextField(blank=True)
    visibility = models.CharField(max_length=20, choices=Visibility.choices, default=Visibility.PRIVATE)
    share_slug = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    deleted_at = models.DateTimeField(blank=True, null=True)
    deleted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="deleted_journal_entries",
    )
    deletion_reason = models.CharField(max_length=300, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at", "-id"]
        indexes = [
            models.Index(fields=["user", "watch_status"]),
            models.Index(fields=["visibility", "updated_at"]),
        ]

    def __str__(self):
        return f"{self.title} · {self.user}"


class ExternalMediaIdentity(models.Model):
    entry = models.ForeignKey(JournalEntry, on_delete=models.CASCADE, related_name="external_identities")
    provider = models.CharField(max_length=50)
    external_id = models.CharField(max_length=200)
    canonical_url = models.URLField(max_length=1000)
    metadata = models.JSONField(default=dict, blank=True)
    metadata_schema_version = models.PositiveSmallIntegerField(default=1)
    is_metadata_source = models.BooleanField(default=False)
    metadata_fetched_at = models.DateTimeField(blank=True, null=True)
    provider_updated_at = models.DateTimeField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["provider", "id"]
        constraints = [
            models.UniqueConstraint(fields=["entry", "provider"], name="journal_ext_entry_provider_uq"),
            models.UniqueConstraint(
                fields=["entry"],
                condition=models.Q(is_metadata_source=True),
                name="journal_ext_one_metadata_source_uq",
            ),
        ]
        indexes = [
            models.Index(fields=["provider", "external_id"], name="journal_ext_provider_id_idx"),
        ]

    def save(self, *args, **kwargs):
        self.provider = str(self.provider or "").strip().lower()
        self.external_id = str(self.external_id or "").strip()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.provider}:{self.external_id} · {self.entry}"


class ExternalProviderConfiguration(models.Model):
    provider = models.CharField(max_length=50, unique=True)
    enabled = models.BooleanField(blank=True, null=True)
    client_id = models.CharField(max_length=255, blank=True, default="")
    encrypted_client_secret = models.TextField(blank=True, default="")
    credential_key_version = models.CharField(max_length=16, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["provider"]

    def save(self, *args, **kwargs):
        self.provider = str(self.provider or "").strip().lower()
        self.client_id = str(self.client_id or "").strip()
        super().save(*args, **kwargs)

    def __str__(self):
        return self.provider


class UserExternalAccountConnection(models.Model):
    class AuthMethod(models.TextChoices):
        OAUTH = "oauth", "OAuth"
        PERSONAL_ACCESS_TOKEN = "personal_access_token", "Personal Access Token"

    class Status(models.TextChoices):
        CONNECTED = "connected", "已连接"
        NEEDS_REAUTHORIZATION = "needs_reauthorization", "需要重新授权"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="external_account_connections",
    )
    provider = models.CharField(max_length=50)
    auth_method = models.CharField(max_length=32, choices=AuthMethod.choices)
    external_user_id = models.CharField(max_length=200)
    external_username = models.CharField(max_length=200)
    display_name = models.CharField(max_length=200, blank=True)
    credential_ciphertext = models.TextField()
    credential_key_version = models.CharField(max_length=16, default="v1")
    metadata = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=32, choices=Status.choices, default=Status.CONNECTED)
    connected_at = models.DateTimeField()
    verified_at = models.DateTimeField(blank=True, null=True)
    last_used_at = models.DateTimeField(blank=True, null=True)
    expires_at = models.DateTimeField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["provider", "id"]
        constraints = [
            models.UniqueConstraint(fields=["user", "provider"], name="journal_extacct_user_provider_uq"),
            models.UniqueConstraint(fields=["provider", "external_user_id"], name="journal_extacct_provider_user_uq"),
        ]
        indexes = [
            models.Index(fields=["provider", "status"], name="journal_extacct_status_idx"),
        ]

    def save(self, *args, **kwargs):
        self.provider = str(self.provider or "").strip().lower()
        self.external_user_id = str(self.external_user_id or "").strip()
        self.external_username = str(self.external_username or "").strip()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.provider}:{self.external_username} · {self.user}"


class ExternalCollectionSyncState(models.Model):
    identity = models.OneToOneField(
        ExternalMediaIdentity,
        on_delete=models.CASCADE,
        related_name="collection_sync_state",
    )
    connection = models.ForeignKey(
        UserExternalAccountConnection,
        on_delete=models.CASCADE,
        related_name="collection_sync_states",
    )
    schema_version = models.PositiveSmallIntegerField(default=1)
    baselines = models.JSONField(default=dict, blank=True)
    last_synced_at = models.DateTimeField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["identity_id"]
        indexes = [
            models.Index(fields=["connection", "updated_at"], name="journal_extsync_conn_idx"),
        ]

    def clean(self):
        from .external_sync.canonical import validate_baselines

        errors = {}
        if self.schema_version != 1:
            errors["schema_version"] = "Unsupported collection sync baseline schema version."
        try:
            validate_baselines(self.baselines)
        except ValueError as error:
            errors["baselines"] = str(error)
        if self.identity_id and self.connection_id:
            identity = self.identity
            connection = self.connection
            if identity.entry.user_id != connection.user_id:
                errors["connection"] = "Sync identity and connection must have the same owner."
            if identity.provider != connection.provider:
                errors["connection"] = "Sync identity and connection must use the same provider."
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.identity} · {self.connection}"


class ExternalAccountAuthorizationState(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="external_account_authorization_states",
    )
    provider = models.CharField(max_length=50)
    state_digest = models.CharField(max_length=64, unique=True)
    expires_at = models.DateTimeField()
    consumed_at = models.DateTimeField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=["provider", "expires_at"], name="journal_extauth_expiry_idx"),
        ]


class ExternalImportSession(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="external_import_sessions",
    )
    provider = models.CharField(max_length=50)
    snapshot = models.JSONField(default=list)
    snapshot_schema_version = models.PositiveSmallIntegerField(default=1)
    result = models.JSONField(default=dict, blank=True)
    expires_at = models.DateTimeField()
    applied_at = models.DateTimeField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["user", "provider", "expires_at"], name="journal_extimport_exp_idx"),
        ]


class WatchHistoryRecord(models.Model):
    entry = models.ForeignKey(
        JournalEntry,
        on_delete=models.CASCADE,
        related_name="watch_history_records",
    )
    watched_on = models.DateField()
    watched_label = models.CharField(max_length=80)
    brush_number = models.PositiveSmallIntegerField(blank=True, null=True)
    brush_label = models.CharField(max_length=20, default="首刷")
    episode_start = models.PositiveSmallIntegerField(blank=True, null=True)
    episode_end = models.PositiveSmallIntegerField(blank=True, null=True)
    notes = models.JSONField(default=list, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    sequence = models.PositiveIntegerField()
    semantic_key = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["sequence", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["entry", "semantic_key"],
                name="journal_watch_entry_semantic_uq",
            ),
            models.UniqueConstraint(
                fields=["entry", "sequence"],
                name="journal_watch_entry_sequence_uq",
            ),
            models.CheckConstraint(
                condition=models.Q(episode_start__isnull=True)
                | models.Q(episode_end__isnull=True)
                | models.Q(episode_end__gte=models.F("episode_start")),
                name="journal_watch_episode_range_ck",
            ),
        ]
        indexes = [
            models.Index(
                fields=["entry", "watched_on"],
                name="journal_watch_entry_date_idx",
            ),
        ]

    def save(self, *args, **kwargs):
        from .watch_history.validation import semantic_digest_from_values

        self.semantic_key = semantic_digest_from_values(
            self.watched_on,
            self.brush_label,
            self.episode_start,
            self.episode_end,
        )
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.entry} · {self.watched_on} · {self.brush_label}"


class UserSettings(models.Model):
    class PublicStatus(models.TextChoices):
        PRIVATE = "private", "未公开"
        PENDING = "pending", "待审核"
        APPROVED = "approved", "已公开"

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="journal_settings")
    nickname = models.CharField(max_length=80, blank=True)
    avatar = models.ImageField(upload_to=avatar_upload_to, blank=True, null=True)
    showcase_subtitle = models.CharField(max_length=240, blank=True, default="把每一次与动画相遇认真收藏。")
    accent = models.CharField(max_length=20, default="#4ecdc4")
    theme = models.CharField(max_length=30, default="memphis-pop")
    default_view = models.CharField(max_length=10, default="list")
    public_status = models.CharField(max_length=16, choices=PublicStatus.choices, default=PublicStatus.PRIVATE)
    allow_sharing = models.BooleanField(default=False)
    public_review_reason = models.CharField(max_length=500, blank=True, default="")
    public_reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="reviewed_public_journals",
    )
    public_reviewed_at = models.DateTimeField(blank=True, null=True)
    public_slug = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.nickname or self.user.get_username()


class QuickFilter(models.Model):
    class MatchMode(models.TextChoices):
        ANY = "any", "任一匹配"
        ALL = "all", "全部匹配"

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="quick_filters")
    name = models.CharField(max_length=80)
    tags = models.JSONField(default=list, blank=True)
    title_keywords = models.JSONField(default=list, blank=True)
    match_mode = models.CharField(max_length=10, choices=MatchMode.choices, default=MatchMode.ANY)
    color = models.CharField(max_length=20, default="#ffe66d")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["id"]
        unique_together = [("user", "name")]

    def __str__(self):
        return self.name


class Column(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", "草稿"
        PENDING = "pending", "待审核"
        APPROVED = "approved", "已通过"
        REJECTED = "rejected", "未通过"
        REMOVAL_REQUESTED = "removal_requested", "申请下架"

    author = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="columns")
    title = models.CharField(max_length=200)
    slug = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    summary = models.CharField(max_length=400, blank=True)
    cover = models.ImageField(upload_to=column_cover_upload_to, blank=True, null=True)
    body = models.TextField()
    status = models.CharField(max_length=24, choices=Status.choices, default=Status.DRAFT)
    featured = models.BooleanField(default=False)
    moderation_reason = models.CharField(max_length=500, blank=True, default="")
    moderated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="moderated_columns",
    )
    moderated_at = models.DateTimeField(blank=True, null=True)
    deleted_at = models.DateTimeField(blank=True, null=True)
    deleted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="deleted_columns",
    )
    deletion_reason = models.CharField(max_length=300, blank=True, default="")
    entries = models.ManyToManyField(JournalEntry, blank=True, related_name="columns")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    published_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        ordering = ["-published_at", "-updated_at"]
        indexes = [models.Index(fields=["status", "featured"])]

    def __str__(self):
        return self.title


class AdminAuditLog(models.Model):
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, blank=True, null=True, related_name="admin_audit_logs")
    action = models.CharField(max_length=80)
    target_type = models.CharField(max_length=80, blank=True, default="")
    target_id = models.CharField(max_length=80, blank=True, default="")
    target_label = models.CharField(max_length=300, blank=True, default="")
    before = models.JSONField(default=dict, blank=True)
    after = models.JSONField(default=dict, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    ip_address = models.GenericIPAddressField(blank=True, null=True)
    user_agent = models.CharField(max_length=500, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [models.Index(fields=["action", "created_at"]), models.Index(fields=["target_type", "target_id"])]

    def __str__(self):
        return f"{self.actor or 'system'} · {self.action} · {self.target_label}"
