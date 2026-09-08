import json

from django.core.management.base import BaseCommand, CommandError

from journal.media_inventory import backfill_media_references, inspect_media_references
from journal.models import JournalEntry
from journal.media_references import cleanup_media
from site_config.models import MediaObject


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
            result = backfill_media_references(after_entry=options["after_entry"], limit=options["limit"])
            if options["all_batches"]:
                # Bound this invocation using the remaining snapshot. Concurrent
                # writes already maintain their own references; any unresolved
                # or unexpected extra work still fails the final READY gate.
                remaining = JournalEntry.objects.filter(pk__gt=result["next_after_entry"]).count()
                for _ in range(remaining // options["limit"] + 1):
                    if not result["more"]:
                        break
                    cursor = result["next_after_entry"]
                    result = backfill_media_references(after_entry=cursor, limit=options["limit"])
                    if result["more"] and result["next_after_entry"] <= cursor:
                        raise CommandError("持有回填游标未前进。")
        else:
            if options["all_batches"]:
                raise CommandError("--all-batches 需要 --apply。")
            result = inspect_media_references()
        if options["retry_cleanup"]:
            result.update(read_only=False, cleanup_results=cleanup_results)
        self.stdout.write(json.dumps(result, ensure_ascii=False, sort_keys=True))
        if result["status"] != "READY":
            raise CommandError("托管封面持有尚未完成核验；缺失、歧义或在途写入仍受保护。")
