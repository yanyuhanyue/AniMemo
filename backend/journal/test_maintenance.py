from io import StringIO
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
