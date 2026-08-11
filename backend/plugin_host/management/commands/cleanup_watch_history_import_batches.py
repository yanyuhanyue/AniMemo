from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from plugin_host.models import PluginData


class Command(BaseCommand):
    help = "清理超过保留期的 Watch History Importer 批次。"

    def handle(self, *args, **options):
        cutoff = timezone.now() - timedelta(
            seconds=int(
                getattr(
                    settings,
                    "WATCH_HISTORY_IMPORT_BATCH_RETENTION_SECONDS",
                    604800,
                )
            )
        )
        deleted, _ = PluginData.objects.filter(
            plugin__slug="watch-history-importer",
            namespace="batches",
            updated_at__lt=cutoff,
        ).delete()
        self.stdout.write(self.style.SUCCESS(f"deleted batches={deleted}"))
