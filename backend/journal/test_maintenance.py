from io import StringIO
from types import SimpleNamespace
from unittest.mock import patch

from django.core import management
from django.core.management.base import CommandError
from django.test import SimpleTestCase

from .management.commands.run_maintenance import TASKS


class MaintenanceRunnerTests(SimpleTestCase):
    def test_list_reports_only_allowlisted_tasks(self):
        output = StringIO()
        management.call_command("run_maintenance", list_tasks=True, stdout=output)
        self.assertEqual(output.getvalue().splitlines(), list(TASKS))
        self.assertIn("audit_orphan_media", TASKS)
        self.assertIn("cleanup_integration_events", TASKS)
        self.assertIn("cleanup_watch_history_import_batches", TASKS)
        self.assertNotIn("delete", " ".join(TASKS))

    @patch("journal.management.commands.run_maintenance.call_command")
    def test_runner_does_not_pass_destructive_options(self, call_command):
        management.call_command("run_maintenance", task=["audit_orphan_media"])
        call_command.assert_called_once()
        self.assertEqual(call_command.call_args.args, ("audit_orphan_media",))
        self.assertNotIn("delete", call_command.call_args.kwargs)

    @patch("journal.management.commands.run_maintenance.call_command")
    def test_failures_are_isolated_and_final_status_is_nonzero(self, call_command):
        call_command.side_effect = [RuntimeError("fixture failure"), None]
        output = StringIO()

        with self.assertRaises(CommandError):
            management.call_command(
                "run_maintenance",
                task=["purge_expired_pending_registrations", "purge_expired_revoked_tokens"],
                stdout=output,
            )

        self.assertEqual(call_command.call_count, 2)
        self.assertIn("FAIL purge_expired_pending_registrations", output.getvalue())
        self.assertIn("PASS purge_expired_revoked_tokens", output.getvalue())

    @patch("journal.management.commands.refresh_media_storage_usage.refresh_cloudflare_usage", side_effect=RuntimeError("provider failure"))
    @patch("journal.management.commands.refresh_media_storage_usage.MediaStorageBackend.objects")
    def test_media_usage_failure_returns_nonzero(self, objects, _refresh):
        backend = SimpleNamespace(
            pk=7,
            slug="primary-r2",
            cloudflare_account_ref_id=3,
            bucket_name="anime-journal",
            analytics_token_configured=True,
        )
        objects.select_related.return_value.filter.return_value.order_by.return_value = [backend]
        output = StringIO()

        with self.assertRaises(CommandError):
            management.call_command("refresh_media_storage_usage", stdout=output)

        self.assertIn("FAILED primary-r2", output.getvalue())
        self.assertIn("failed=1", output.getvalue())
