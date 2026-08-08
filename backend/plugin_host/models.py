from django.conf import settings
from django.db import models
from django.db.models import Q


class PluginInstallation(models.Model):
    class Status(models.TextChoices):
        DEPLOYED = "deployed", "已部署"
        ENABLED = "enabled", "已启用"
        UNHEALTHY = "unhealthy", "异常"

    slug = models.SlugField(max_length=80, unique=True)
    plugin_id = models.CharField(max_length=160, unique=True)
    current_version = models.CharField(max_length=40)
    previous_version = models.CharField(max_length=40, blank=True, default="")
    enabled = models.BooleanField(default=False)
    config = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DEPLOYED)
    healthy = models.BooleanField(default=False)
    last_error = models.TextField(blank=True, default="")
    disk_bytes = models.PositiveBigIntegerField(default=0)
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["slug"]


class PluginData(models.Model):
    plugin_slug = models.SlugField(max_length=80)
    namespace = models.CharField(max_length=120)
    key = models.CharField(max_length=160)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, null=True, blank=True)
    value = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("plugin_slug", "namespace", "key"),
                condition=Q(user__isnull=True),
                name="plugin_data_global_key_unique",
            ),
            models.UniqueConstraint(
                fields=("plugin_slug", "namespace", "key", "user"),
                condition=Q(user__isnull=False),
                name="plugin_data_user_key_unique",
            ),
        ]
