from django.core.management.base import BaseCommand, CommandError

from site_config.media_storage.usage import refresh_cloudflare_usage
from site_config.models import MediaStorageBackend


class Command(BaseCommand):
    help = "刷新已配置 Cloudflare Analytics 的 R2 存储 usage 快照。"

    def add_arguments(self, parser):
        parser.add_argument("--quiet", action="store_true")

    def handle(self, *args, **options):
        success = failed = skipped = 0
        rows = MediaStorageBackend.objects.select_related("cloudflare_account_ref").filter(
            backend_type=MediaStorageBackend.BackendType.CLOUDFLARE_R2,
            enabled=True,
        ).order_by("priority", "id")
        for backend in rows:
            if not backend.cloudflare_account_ref_id or not backend.bucket_name or not backend.analytics_token_configured:
                skipped += 1
                if not options["quiet"]:
                    self.stdout.write(f"SKIPPED {backend.slug}: analytics not configured")
                continue
            try:
                refresh_cloudflare_usage(backend.pk)
            except Exception as error:
                failed += 1
                if not options["quiet"]:
                    self.stdout.write(f"FAILED {backend.slug}: {error.__class__.__name__}")
                continue
            success += 1
            if not options["quiet"]:
                self.stdout.write(f"SUCCESS {backend.slug}")
        summary = f"summary success={success} failed={failed} skipped={skipped}"
        if failed:
            self.stdout.write(self.style.ERROR(summary))
            raise CommandError(f"媒体存储 usage 刷新失败：{failed} 个后端。")
        self.stdout.write(self.style.SUCCESS(summary))
