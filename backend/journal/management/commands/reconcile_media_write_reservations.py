from django.core.management.base import BaseCommand
from django.utils import timezone

from site_config.models import MediaWriteReservation


class Command(BaseCommand):
    help = "报告在途媒体写入；可显式重试已确认回滚的清理，不按超时丢弃占用。"

    def add_arguments(self, parser):
        parser.add_argument("--retry-cleanup", action="store_true")

    def handle(self, *args, **options):
        now = timezone.now()
        expired = MediaWriteReservation.objects.filter(
            status=MediaWriteReservation.Status.PENDING,
            expires_at__lt=now,
        ).count()
        retried = 0
        if options["retry_cleanup"]:
            from site_config.media_storage.receipts import retry_receipt_cleanup

            ids = list(MediaWriteReservation.objects.filter(status__in=["cleanup_ready", "cleanup_failed"]).values_list("pk", flat=True))
            for reservation_id in ids:
                retried += int(retry_receipt_cleanup(reservation_id))
        self.stdout.write(f"保留 {expired} 个超时但结果未明的写入预留；成功清理 {retried} 个已确认回滚的写入。")
