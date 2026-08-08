from django.core.management.base import BaseCommand
from django.utils import timezone

from accounts.models import RevokedAccessToken


class Command(BaseCommand):
    help = "删除已过期的 access token 撤销记录。"

    def handle(self, *args, **options):
        deleted, _details = RevokedAccessToken.objects.filter(expires_at__lte=timezone.now()).delete()
        self.stdout.write(self.style.SUCCESS(f"已清理 {deleted} 条过期 access token 撤销记录。"))
