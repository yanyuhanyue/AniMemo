from django.core.management.base import BaseCommand
from django.utils import timezone

from journal.models import ExternalAccountAuthorizationState, ExternalImportSession


class Command(BaseCommand):
    help = "删除已过期的外部账号 OAuth state 与导入快照。"

    def handle(self, *args, **options):
        now = timezone.now()
        oauth_count, _ = ExternalAccountAuthorizationState.objects.filter(expires_at__lt=now).delete()
        import_count, _ = ExternalImportSession.objects.filter(expires_at__lt=now).delete()
        self.stdout.write(
            self.style.SUCCESS(
                f"External account session cleanup: oauth={oauth_count}, imports={import_count}"
            )
        )
