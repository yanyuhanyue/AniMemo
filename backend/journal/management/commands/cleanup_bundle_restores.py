import json

from django.core.management.base import BaseCommand, CommandError

from journal.data_bundle.restore import BundleRestoreError, cleanup_restores


class Command(BaseCommand):
    help = "Expire inactive portable restores and clean bounded private staging; retain terminal receipts for 7 days."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=25)
        parser.add_argument("--max-files", type=int, default=100)

    def handle(self, *args, **options):
        try:
            result = cleanup_restores(limit=options["limit"], max_files=options["max_files"])
        except BundleRestoreError as error:
            raise CommandError(error.code) from error
        self.stdout.write(json.dumps(result, sort_keys=True))
