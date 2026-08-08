from django.apps import AppConfig


class JournalConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "journal"
    verbose_name = "番剧手账"

    def ready(self):
        from . import signals  # noqa: F401
