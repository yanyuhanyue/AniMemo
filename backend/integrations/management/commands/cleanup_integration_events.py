from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from integrations.models import IntegrationEvent


class Command(BaseCommand):
    help = "清理超过保留期的集成事件。"

    def handle(self, *args, **options):
        now = timezone.now()
        acked_cutoff = now - timedelta(
            seconds=int(getattr(settings, "INTEGRATION_ACKED_EVENT_RETENTION_SECONDS", 86400))
        )
        unacked_cutoff = now - timedelta(
            seconds=int(getattr(settings, "INTEGRATION_UNACKED_EVENT_RETENTION_SECONDS", 604800))
        )
        acked, _ = IntegrationEvent.objects.filter(
            acked_at__isnull=False,
            acked_at__lt=acked_cutoff,
        ).delete()
        unacked, _ = IntegrationEvent.objects.filter(
            acked_at__isnull=True,
            created_at__lt=unacked_cutoff,
        ).delete()
        self.stdout.write(self.style.SUCCESS(f"deleted acked={acked} unacked={unacked}"))
