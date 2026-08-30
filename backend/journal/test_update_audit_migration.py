import io
import json
import zipfile
from importlib import import_module

from django.apps import apps
from django.contrib.auth import get_user_model
from django.urls import reverse
from rest_framework.test import APITestCase

from journal.models import AdminAuditLog

User = get_user_model()


def _identity(version):
    return {
        "version": version,
        "channel": "stable",
        "commit": "1" * 40,
        "apiDigest": "sha256:" + "2" * 64,
        "webDigest": "sha256:" + "3" * 64,
    }


def _legacy_plan(marker):
    return {
        "planId": "a" * 32,
        "expiresAt": "2026-08-30T12:00:00Z",
        "from": {**_identity("v1.0.0"), "path": marker},
        "to": _identity("v1.0.1"),
        "compatibility": {
            "allowed": True,
            "decision": "safe_switch",
            "rollbackMode": "safe",
            "migrationRequired": False,
            "migrationPolicy": "none",
            "reasons": [],
            "traceback": marker,
        },
        "affectedServices": ["api", "web"],
        "databaseRollback": False,
        "source": "github",
        "transportPolicyIdentity": "4" * 64,
        "verifiedReleaseIdentity": "sha256:" + "5" * 64,
        "events": [{"detail": marker}],
        "sql": marker,
    }


class UpdateAuditMigrationTests(APITestCase):
    def test_legacy_update_diagnostics_are_removed_from_database_view_and_backup(self):
        marker = "Traceback SELECT private FROM internal_table C:\\private\\runtime.py"
        plan = AdminAuditLog.objects.create(
            action="system.update_plan", after=_legacy_plan(marker)
        )
        apply = AdminAuditLog.objects.create(
            action="system.update_apply",
            metadata={"operation_id": "b" * 32, "detail": marker, "events": [marker]},
        )
        rollback = AdminAuditLog.objects.create(
            action="system.update_rollback",
            metadata={"operation_id": "invalid", "traceback": marker},
        )
        unrelated = AdminAuditLog.objects.create(
            action="journal.review",
            after={"public_status": "approved"},
            metadata={"reason": "business-value"},
        )

        migration = import_module(
            "journal.migrations.0007_redact_update_audit_results"
        )
        migration.redact_update_audits(apps, None)

        plan.refresh_from_db()
        apply.refresh_from_db()
        rollback.refresh_from_db()
        unrelated.refresh_from_db()
        self.assertNotIn(marker, str(plan.after))
        self.assertEqual(set(plan.after), set(_legacy_plan(marker)) - {"events", "sql"})
        self.assertNotIn("path", plan.after["from"])
        self.assertNotIn("traceback", plan.after["compatibility"])
        self.assertEqual(apply.metadata, {"operation_id": "b" * 32})
        self.assertEqual(rollback.metadata, {})
        self.assertEqual(unrelated.after, {"public_status": "approved"})
        self.assertEqual(unrelated.metadata, {"reason": "business-value"})

        admin = User.objects.create_superuser(
            username="update-audit-admin",
            email="update-audit@example.com",
            password="StrongPass123!",
        )
        self.client.force_authenticate(admin)
        audit_view = self.client.get(
            reverse("staff-resource-list", kwargs={"kind": "audit"})
        )
        backup = self.client.get(
            reverse("staff-system-backup"),
            {"export_format": "zip", "kind": "audit"},
        )

        self.assertEqual(audit_view.status_code, 200)
        self.assertNotIn(marker, str(audit_view.data))
        self.assertEqual(backup.status_code, 200)
        with zipfile.ZipFile(io.BytesIO(backup.content)) as archive:
            exported = json.loads(archive.read("audit.json"))
        self.assertNotIn(marker, str(exported))
