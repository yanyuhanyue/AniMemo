from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q
from django.db.models.functions import Lower


class PluginProject(models.Model):
    class InstallationMode(models.TextChoices):
        USER = "user", "用户插件"
        SYSTEM = "system", "系统插件"

    class Status(models.TextChoices):
        ACTIVE = "active", "活跃"
        SUSPENDED = "suspended", "已暂停"
        ARCHIVED = "archived", "已归档"

    plugin_id = models.CharField(max_length=160, unique=True)
    slug = models.SlugField(max_length=80, unique=True)
    name = models.CharField(max_length=80)
    description = models.CharField(max_length=240)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="plugin_projects",
    )
    installation_mode = models.CharField(
        max_length=12,
        choices=InstallationMode.choices,
        default=InstallationMode.USER,
    )
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.ACTIVE)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["slug"]

    def __str__(self):
        return self.slug


class PluginPackageBlob(models.Model):
    sha256 = models.CharField(max_length=64, unique=True)
    size_bytes = models.PositiveBigIntegerField()
    storage_path = models.CharField(max_length=255, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["sha256"]


class PluginUploadAttempt(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="plugin_upload_attempts",
    )
    size_bytes = models.PositiveBigIntegerField(default=0)
    accepted = models.BooleanField(default=False)
    outcome = models.CharField(max_length=32, default="received")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [models.Index(fields=("user", "created_at"))]


class PluginVersion(models.Model):
    class ReviewStatus(models.TextChoices):
        DRAFT = "draft", "草稿"
        SUBMITTED = "submitted", "审核中"
        APPROVED = "approved", "已通过"
        REJECTED = "rejected", "已拒绝"
        REVOKED = "revoked", "已撤销"

    plugin = models.ForeignKey(PluginProject, on_delete=models.CASCADE, related_name="versions")
    version = models.CharField(max_length=40)
    package_blob = models.ForeignKey(PluginPackageBlob, on_delete=models.PROTECT, related_name="versions")
    manifest_snapshot = models.JSONField(default=dict)
    runtime_types = models.JSONField(default=list)
    review_status = models.CharField(max_length=16, choices=ReviewStatus.choices, default=ReviewStatus.DRAFT)
    published_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="plugin_versions",
    )

    class Meta:
        ordering = ["plugin__slug", "-created_at"]
        constraints = [
            models.UniqueConstraint(
                models.F("plugin"),
                Lower("version"),
                name="plugin_version_ci_unique",
            ),
        ]

    def save(self, *args, **kwargs):
        if self.pk:
            original = type(self).objects.filter(pk=self.pk).values(
                "plugin_id", "version", "package_blob_id", "manifest_snapshot", "runtime_types"
            ).first()
            immutable = {
                "plugin_id": self.plugin_id,
                "version": self.version,
                "package_blob_id": self.package_blob_id,
                "manifest_snapshot": self.manifest_snapshot,
                "runtime_types": self.runtime_types,
            }
            if original and original != immutable:
                raise ValidationError("PluginVersion 的插件、版本、Package、Manifest 和 Runtime 类型不可修改。")
        return super().save(*args, **kwargs)


class PluginSubmission(models.Model):
    class Status(models.TextChoices):
        SUBMITTED = "submitted", "已提交"
        APPROVED = "approved", "已通过"
        REJECTED = "rejected", "已拒绝"
        WITHDRAWN = "withdrawn", "已撤回"

    plugin_version = models.ForeignKey(PluginVersion, on_delete=models.CASCADE, related_name="submissions")
    submitter = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="plugin_submissions",
    )
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.SUBMITTED)
    submitted_at = models.DateTimeField(auto_now_add=True)
    reviewer = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="reviewed_plugin_submissions",
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    review_note = models.TextField(blank=True, default="")
    security_report = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-submitted_at"]
        constraints = [
            models.UniqueConstraint(
                fields=("plugin_version",),
                condition=Q(status="submitted"),
                name="plugin_submission_one_active_per_version",
            ),
        ]


class PluginDeployment(models.Model):
    class Status(models.TextChoices):
        DEPLOYED = "deployed", "已部署"
        ENABLED = "enabled", "已启用"
        UNHEALTHY = "unhealthy", "异常"
        REVOKED = "revoked", "已撤销"

    plugin = models.OneToOneField(PluginProject, on_delete=models.CASCADE, related_name="deployment")
    current_version = models.ForeignKey(
        PluginVersion,
        on_delete=models.PROTECT,
        related_name="current_deployments",
    )
    previous_version = models.ForeignKey(
        PluginVersion,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="previous_deployments",
    )
    enabled = models.BooleanField(default=True)
    healthy = models.BooleanField(default=False)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.DEPLOYED)
    system_config = models.JSONField(default=dict, blank=True)
    last_error = models.TextField(blank=True, default="")
    disk_bytes = models.PositiveBigIntegerField(default=0)
    rollback_floor = models.CharField(max_length=40, blank=True, default="")
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)

    class Meta:
        ordering = ["plugin__slug"]

    def clean(self):
        if self.current_version_id and self.current_version.plugin_id != self.plugin_id:
            raise ValidationError("当前部署版本必须属于同一个插件项目。")
        if self.previous_version_id and self.previous_version.plugin_id != self.plugin_id:
            raise ValidationError("上一部署版本必须属于同一个插件项目。")


class UserPluginInstallation(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="plugin_installations")
    plugin = models.ForeignKey(PluginProject, on_delete=models.CASCADE, related_name="user_installations")
    enabled = models.BooleanField(default=True)
    config = models.JSONField(default=dict, blank=True)
    installed_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["plugin__slug"]
        constraints = [
            models.UniqueConstraint(fields=("user", "plugin"), name="user_plugin_installation_unique"),
        ]


class PluginData(models.Model):
    plugin = models.ForeignKey(PluginProject, on_delete=models.CASCADE, related_name="data_rows")
    namespace = models.CharField(max_length=120)
    key = models.CharField(max_length=160)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, null=True, blank=True)
    value = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(
                fields=("plugin", "namespace", "updated_at"),
                name="plugin_data_retention_idx",
            ),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=("plugin", "namespace", "key"),
                condition=Q(user__isnull=True),
                name="plugin_data_global_key_unique",
            ),
            models.UniqueConstraint(
                fields=("plugin", "namespace", "key", "user"),
                condition=Q(user__isnull=False),
                name="plugin_data_user_key_unique",
            ),
        ]
