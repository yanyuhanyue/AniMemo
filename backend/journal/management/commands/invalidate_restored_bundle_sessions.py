"""Invalidate staging references only on a verified, offline Restore target."""
import json
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import F
from django.utils import timezone

from journal.data_bundle.restore import OPEN
from journal.models import BundleRestoreAdmission, BundleRestoreSession


class Command(BaseCommand):
    help = "Restore-only, before API start: invalidate copied session staging when the new target staging root is empty."

    def handle(self, *args, **options):
        root = Path(settings.BUNDLE_RESTORE_ROOT)
        from plugin_host.filesystem_security import (
            PluginFilesystemSecurityError,
            ensure_directory,
            validate_directory_chain,
        )

        try:
            ensure_directory(root, root)
            validate_directory_chain(root, root)
            if next(root.iterdir(), None) is not None:
                raise CommandError("Restore target staging must be empty; existing bytes and reservations were preserved.")
        except (OSError, PluginFilesystemSecurityError) as error:
            raise CommandError("Restore target staging could not be verified.") from error
        # The verified Restore controller owns the offline target. This is never
        # invoked by normal upgrades or by an HTTP endpoint.
        with transaction.atomic():
            BundleRestoreAdmission.objects.select_for_update().get(pk=1)
            invalidated = BundleRestoreSession.objects.filter(state__in=OPEN).update(
                state="expired", generation=F("generation") + 1, finished_at=timezone.now(),
                operation_expires_at=None, error_code="bundle_restore_expired",
            )
            measured_empty = BundleRestoreSession.objects.update(
                reserved_bytes=0, physical_bytes=0, cleanup_pending=False,
            )
        self.stdout.write(json.dumps({"invalidated": invalidated, "empty_staging_reconciled": measured_empty,
                                      "completed_receipts_preserved": True}, sort_keys=True))
