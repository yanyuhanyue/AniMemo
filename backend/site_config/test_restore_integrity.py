import io
import json
import os
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase

from .management.commands.validate_restore_integrity import Command
from .models import InstallationState


class RestoreIntegrityCommandTests(TestCase):
    def test_actual_integrity_command_uses_application_model_table_and_preserves_epoch(self):
        state, _ = InstallationState.objects.get_or_create(pk=1)
        state.status = InstallationState.Status.INITIALIZED
        state.save()
        epoch = state.authentication_epoch
        output = io.StringIO()
        with patch.dict(os.environ, {"ANIMEMO_INSTANCE_ID": "11111111-2222-4333-8444-555555555555"}):
            call_command("validate_restore_integrity", stdout=output)
        checks = json.loads(output.getvalue())["checks"]
        self.assertEqual(len(checks), 9)
        self.assertTrue(all(checks.values()))
        self.assertTrue(checks["durable.write"])
        state.refresh_from_db()
        self.assertEqual(state.authentication_epoch, epoch)

    def test_durable_write_rejects_a_missing_installation_row(self):
        InstallationState.objects.all().delete()
        self.assertFalse(Command._durable_write())
