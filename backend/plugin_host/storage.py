from django.db import IntegrityError, transaction

from .models import PluginData, PluginProject


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
