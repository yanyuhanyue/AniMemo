import json
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.utils import timezone

from .models import PluginData, PluginProject


class PluginStorageLimitError(ValueError):
    pass


class PluginStorage:
    def __init__(self, plugin_slug, *, user=None, namespace="default"):
        self.plugin = plugin_slug if isinstance(plugin_slug, PluginProject) else PluginProject.objects.get(slug=plugin_slug)
        self.user = user
        self.namespace = namespace

    def get(self, key, default=None):
        row = PluginData.objects.filter(
            plugin=self.plugin,
            namespace=self.namespace,
            key=key,
            user=self.user,
        ).first()
        return default if row is None else row.value

    @transaction.atomic
    def set(self, key, value):
        return self._set_locked(key, value)

    @transaction.atomic
    def set_bounded(
        self,
        key,
        value,
        *,
        max_value_bytes,
        max_rows,
        retention_seconds,
    ):
        max_value_bytes = int(max_value_bytes)
        max_rows = int(max_rows)
        retention_seconds = int(retention_seconds)
        if min(max_value_bytes, max_rows, retention_seconds) < 1:
            raise ValueError("Plugin storage bounds must be positive integers.")
        try:
            encoded = json.dumps(
                value,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        except (TypeError, ValueError) as error:
            raise ValueError("Plugin storage value must be JSON serializable.") from error
        if len(encoded) > max_value_bytes:
            raise PluginStorageLimitError(
                f"Plugin storage value exceeds {max_value_bytes} bytes."
            )

        if self.user is None:
            PluginProject.objects.select_for_update().get(pk=self.plugin.pk)
        else:
            get_user_model().objects.select_for_update().get(pk=self.user.pk)

        rows = self.collection()
        cutoff = timezone.now() - timedelta(seconds=retention_seconds)
        rows.filter(updated_at__lt=cutoff).exclude(key=key).delete()
        overflow = list(
            rows.exclude(key=key)
            .order_by("-updated_at", "-pk")
            .values_list("pk", flat=True)[max_rows - 1 :]
        )
        if overflow:
            rows.filter(pk__in=overflow).delete()
        return self._set_locked(key, value)

    def _set_locked(self, key, value):
        lookup = {
            "plugin": self.plugin,
            "namespace": self.namespace,
            "key": key,
            "user": self.user,
        }
        row = PluginData.objects.select_for_update().filter(**lookup).first()
        if row is not None:
            row.value = value
            row.save(update_fields=["value", "updated_at"])
            return row.value
        try:
            with transaction.atomic():
                row = PluginData.objects.create(**lookup, value=value)
        except IntegrityError:
            row = PluginData.objects.select_for_update().get(**lookup)
            row.value = value
            row.save(update_fields=["value", "updated_at"])
        return row.value

    def delete(self, key):
        return PluginData.objects.filter(
            plugin=self.plugin,
            namespace=self.namespace,
            key=key,
            user=self.user,
        ).delete()[0]

    def collection(self):
        return PluginData.objects.filter(
            plugin=self.plugin,
            namespace=self.namespace,
            user=self.user,
        ).order_by("key")
