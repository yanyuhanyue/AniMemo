from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db.models import Q
from django.utils import timezone

from accounts.models import PendingRegistration


class Command(BaseCommand):
    help = "清理过期或已消费超过 30 天的邮箱预注册记录。"

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **options):
        now = timezone.now()
        consumed_cutoff = now - timedelta(days=30)
        candidates = PendingRegistration.objects.filter(
            Q(consumed_at__isnull=True, expires_at__lt=now)
            | Q(consumed_at__isnull=False, consumed_at__lt=consumed_cutoff)
        )
        count = candidates.count()
        if options["dry_run"]:
            self.stdout.write(f"发现 {count} 条可清理的预注册记录（dry-run，未删除）。")
            return
        candidates.delete()
        self.stdout.write(self.style.SUCCESS(f"已清理 {count} 条过期预注册记录。"))
