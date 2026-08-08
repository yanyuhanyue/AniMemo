import uuid

from django.conf import settings
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
