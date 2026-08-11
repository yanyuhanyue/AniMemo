from django.core.management.base import BaseCommand
from django.utils import timezone

from site_config.models import MediaWriteReservation


class Command(BaseCommand):
    help = "将已过期且仍处于 pending 的媒体写入预留标记为 abandoned；不删除任何远程对象。"

    def handle(self, *args, **options):
        now = timezone.now()
        abandoned, _ = MediaWriteReservation.objects.filter(
            status=MediaWriteReservation.Status.PENDING,
            expires_at__lt=now,
        ).update(
            status=MediaWriteReservation.Status.ABANDONED,
            abandoned_at=now,
        )
        self.stdout.write(
            self.style.SUCCESS(
                f"已标记 {abandoned} 个过期媒体写入预留；未执行远程对象删除。"
            )
        )
