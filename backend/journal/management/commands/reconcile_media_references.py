import json

from django.core.management.base import BaseCommand, CommandError
from site_config.models import MediaObject

from journal.media_inventory import (
    backfill_all_media_references,
    backfill_media_references,
    inspect_media_references,
)
from journal.media_references import cleanup_media


class Command(BaseCommand):
    help = "只读核验托管封面持有；--apply 逐批幂等回填，保留歧义/缺失数据。"

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true")
        parser.add_argument("--after-entry", type=int, default=0)
        parser.add_argument("--limit", type=int, default=200)
        parser.add_argument("--all-batches", action="store_true")
        parser.add_argument("--retry-cleanup", action="store_true")

    def handle(self, *args, **options):
        cleanup_results = []
        if options["retry_cleanup"]:
            candidates = MediaObject.objects.filter(lifecycle__in=["deleting", "delete_failed"]).order_by("pk").values_list("pk", flat=True)
            for media_id in candidates.iterator():
                cleanup_results.append({"media_id": str(media_id), "reclaimed": cleanup_media(media_id)})
        if options["apply"]:
            apply = backfill_all_media_references if options["all_batches"] else backfill_media_references
            try:
                result = apply(after_entry=options["after_entry"], limit=options["limit"])
            except ValueError as error:
                raise CommandError(str(error)) from None
        else:
            if options["all_batches"]:
                raise CommandError("--all-batches 需要 --apply。")
            result = inspect_media_references()
        if options["retry_cleanup"]:
            result.update(read_only=False, cleanup_results=cleanup_results)
        self.stdout.write(json.dumps(result, ensure_ascii=False, sort_keys=True))
        if result["status"] != "READY":
            raise CommandError("托管封面持有尚未完成核验；缺失、歧义或在途写入仍受保护。")
