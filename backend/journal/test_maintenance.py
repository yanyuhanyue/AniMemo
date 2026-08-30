import subprocess
import sys
import traceback
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
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

    @patch("journal.management.commands.run_maintenance.subprocess.run")
    def test_runner_uses_only_the_fixed_isolated_command(self, run):
        run.return_value = subprocess.CompletedProcess((), 0)
        management.call_command("run_maintenance", task=["audit_orphan_media"])
        run.assert_called_once()
        command = run.call_args.args[0]
        self.assertEqual(command[0], sys.executable)
        self.assertEqual(command[1], "-B")
        self.assertEqual(command[-2:], ("audit_orphan_media", "--no-color"))
        self.assertNotIn("delete", command)
        self.assertIs(run.call_args.kwargs["stdin"], subprocess.DEVNULL)
        self.assertIs(run.call_args.kwargs["stdout"], subprocess.DEVNULL)
        self.assertIs(run.call_args.kwargs["stderr"], subprocess.DEVNULL)
        self.assertTrue(run.call_args.kwargs["close_fds"])
        self.assertFalse(run.call_args.kwargs["check"])

    @patch("journal.management.commands.run_maintenance.subprocess.run")
    def test_failures_are_isolated_and_final_status_is_nonzero(self, run):
        run.side_effect = [
            subprocess.CompletedProcess((), 1),
            subprocess.CompletedProcess((), 0),
        ]
        output = StringIO()

        with self.assertRaises(CommandError):
            management.call_command(
                "run_maintenance",
                task=["purge_expired_pending_registrations", "purge_expired_revoked_tokens"],
                stdout=output,
            )

        self.assertEqual(run.call_count, 2)
        self.assertIn("FAIL purge_expired_pending_registrations", output.getvalue())
        self.assertIn("PASS purge_expired_revoked_tokens", output.getvalue())

    def test_isolated_child_cannot_emit_prebound_log_native_or_descendant_output(self):
        sentinels = (
            r"C:\Users\hostile-user\private.sql",
            "SELECT secret FROM auth_user",
            "maintenance-secret-sentinel",
            "hostile-user",
        )
        output = StringIO()
        errors = StringIO()
        payload = " ".join(sentinels)
        with TemporaryDirectory() as directory:
            hostile_manage = Path(directory) / "manage.py"
            hostile_manage.write_text(
                "import logging, os, subprocess, sys\n"
                f"payload = {payload!r}\n"
                "bound = os.fdopen(os.dup(2), 'w', closefd=True)\n"
                "logger = logging.getLogger('hostile-maintenance')\n"
                "logger.addHandler(logging.StreamHandler(bound))\n"
                "logger.error(payload)\n"
                "bound.flush()\n"
                "os.write(1, payload.encode())\n"
                "os.write(2, payload.encode())\n"
                "subprocess.run([sys.executable, '-c', "
                "'import os; os.write(1, b\\\"descendant-output\\\"); "
                "os.write(2, b\\\"descendant-error\\\")'], check=False)\n"
                "raise SystemExit(1)\n",
                encoding="utf-8",
            )
            with (
                patch(
                    "journal.management.commands.run_maintenance._TASK_COMMANDS",
                    {
                        "audit_orphan_media": (
                            sys.executable,
                            "-B",
                            str(hostile_manage),
                            "audit_orphan_media",
                            "--no-color",
                        )
                    },
                ),
                self.assertRaises(CommandError) as raised,
            ):
                management.call_command(
                    "run_maintenance",
                    task=["audit_orphan_media"],
                    stdout=output,
                    stderr=errors,
                )

        public_text = "\n".join(
            (
                output.getvalue(),
                errors.getvalue(),
                repr(raised.exception),
                "".join(traceback.format_exception(raised.exception)),
            )
        )
        for sentinel in sentinels:
            self.assertNotIn(sentinel, public_text)
        self.assertNotIn("descendant-output", public_text)
        self.assertNotIn("descendant-error", public_text)
        self.assertIn("code=maintenance_task_failed", output.getvalue())
        self.assertRegex(output.getvalue(), r"correlation_id=[0-9a-f]{32}")
        self.assertIsNone(raised.exception.__cause__)

    @patch("journal.management.commands.run_maintenance.subprocess.run")
    def test_successful_child_reports_only_the_parent_status(self, run):
        run.return_value = subprocess.CompletedProcess((), 0)
        output = StringIO()
        management.call_command(
            "run_maintenance",
            task=["audit_orphan_media"],
            stdout=output,
        )

        self.assertEqual(output.getvalue().strip(), "PASS audit_orphan_media")

    @patch("journal.management.commands.refresh_media_storage_usage.refresh_cloudflare_usage", side_effect=RuntimeError("provider failure"))
    @patch("journal.management.commands.refresh_media_storage_usage.MediaStorageBackend.objects")
    def test_media_usage_failure_returns_nonzero(self, objects, _refresh):
        backend = SimpleNamespace(
            pk=7,
            slug="primary-r2",
            cloudflare_account_ref_id=3,
            bucket_name="animemo",
            analytics_token_configured=True,
        )
        objects.select_related.return_value.filter.return_value.order_by.return_value = [backend]
        output = StringIO()

        with self.assertRaises(CommandError):
            management.call_command("refresh_media_storage_usage", stdout=output)

        self.assertIn("FAILED primary-r2", output.getvalue())
        self.assertIn("failed=1", output.getvalue())
