from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from release.contract import build_manifest
from updater.executor import UpdateExecutor
from updater.runtime_state import RuntimeState
from updater.slots import ReleaseSlots
from updater.state import OperationStore


def manifest(
    version: str,
    digit: str,
    *,
    migration=False,
    rollback="safe",
    accepts=None,
    contract="animemo-db-v1",
):
    channel = "stable" if "-" not in version else "rc"
    return build_manifest(
        version=version,
        channel=channel,
        commit=digit * 40,
        created_at=datetime(2026, 8, 12, tzinfo=timezone.utc),
        api_digest="sha256:" + digit * 64,
        web_digest="sha256:" + digit * 64,
        minimum_updater_version="1.0.0",
        database_contract=contract,
        database_accepts=accepts or [contract],
        migration_required=migration,
        migration_policy="additive-backward-compatible" if migration else "none",
        application_rollback=rollback,
        configuration_contract="animemo-config-v1",
        configuration_accepts=["animemo-config-v1"],
        plugin_sdk_apis=[2],
        promoted_from=f"{version}-rc.1" if channel == "stable" else None,
    )


class FakeReleaseSource:
    def __init__(self, target):
        self.target = target
        self.verified = []

    def fetch_verified(self, version, updater_version="1.0.0", refresh=False):
        self.verified.append((version, refresh))
        return self.target


class FakeDeployment:
    def __init__(self, *, fail_at=None, enabled_plugin_apis=None):
        self.fail_at = fail_at
        self.failed = False
        self.calls = []
        self.enabled_plugin_apis = enabled_plugin_apis or {2}

    def _call(self, name, *args):
        self.calls.append((name, *args))
        if self.fail_at == name and not self.failed:
            self.failed = True
            raise RuntimeError(f"{name} failed")

    def preflight(self, manifest): self._call("preflight", manifest["release"]["version"])
    def verify_recent_backup(self): self._call("verify_recent_backup")
    def backup_database(self, operation_id): self._call("backup_database", operation_id); return "backup.sql.gz"
    def pull(self, manifest): self._call("pull", manifest["release"]["version"])
    def migrate(self, manifest): self._call("migrate", manifest["release"]["version"])
    def bootstrap(self, manifest): self._call("bootstrap", manifest["release"]["version"])
    def switch(self, manifest): self._call("switch", manifest["release"]["version"])
    def verify_health(self, manifest): self._call("verify_health", manifest["release"]["version"])
    def inspect_enabled_plugin_apis(self, manifest): self._call("inspect_enabled_plugin_apis", manifest["release"]["version"]); return self.enabled_plugin_apis


class UpdateExecutorTests(unittest.TestCase):
    def setup_executor(self, directory, current, target, deployment):
        root = Path(directory)
        slots = ReleaseSlots(root / "releases")
        slots.import_current(current)
        store = OperationStore(root / "state")
        runtime_state = RuntimeState(root / "state")
        runtime_state.initialize_from_manifest(current, enabled_plugin_apis={2})
        executor = UpdateExecutor(
            store=store,
            slots=slots,
            release_source=FakeReleaseSource(target),
            deployment=deployment,
            runtime_state=runtime_state,
            lock_path=root / "state" / "update.lock",
            updater_version="1.0.0",
        )
        return executor, store, slots

    def test_migration_backup_and_switch_are_ordered(self):
        with tempfile.TemporaryDirectory() as directory:
            current = manifest("v1.0.0", "1", accepts=["animemo-db-v1", "animemo-db-v2"])
            target = manifest("v1.1.0", "2", migration=True, rollback="conditional", contract="animemo-db-v2", accepts=["animemo-db-v1", "animemo-db-v2"])
            deployment = FakeDeployment()
            executor, store, slots = self.setup_executor(directory, current, target, deployment)
            operation = store.create("apply_update", {"version": "v1.1.0"})

            executor.apply(operation["id"], target)

            self.assertEqual(store.get(operation["id"])["status"], "succeeded")
            self.assertEqual(
                [call[0] for call in deployment.calls],
                ["preflight", "inspect_enabled_plugin_apis", "backup_database", "pull", "migrate", "bootstrap", "inspect_enabled_plugin_apis", "switch", "verify_health"],
            )
            self.assertEqual(slots.read()["current"]["release"]["version"], "v1.1.0")

    def test_backup_failure_prevents_migration_and_switch(self):
        with tempfile.TemporaryDirectory() as directory:
            current = manifest("v1.0.0", "1", accepts=["animemo-db-v1", "animemo-db-v2"])
            target = manifest("v1.1.0", "2", migration=True, rollback="conditional", contract="animemo-db-v2", accepts=["animemo-db-v1", "animemo-db-v2"])
            deployment = FakeDeployment(fail_at="backup_database")
            executor, store, slots = self.setup_executor(directory, current, target, deployment)
            operation = store.create("apply_update", {"version": "v1.1.0"})

            executor.apply(operation["id"], target)

            self.assertEqual(store.get(operation["id"])["status"], "failed_pre_switch")
            self.assertNotIn("migrate", [call[0] for call in deployment.calls])
            self.assertNotIn("switch", [call[0] for call in deployment.calls])
            self.assertEqual(slots.read()["current"]["release"]["version"], "v1.0.0")

    def test_apply_refetches_and_rejects_a_release_that_differs_from_the_plan(self):
        with tempfile.TemporaryDirectory() as directory:
            current = manifest("v1.0.0", "1")
            planned = manifest("v1.0.1", "2")
            changed = manifest("v1.0.1", "3")
            deployment = FakeDeployment()
            executor, store, slots = self.setup_executor(directory, current, changed, deployment)
            operation = store.create("apply_update", {"version": "v1.0.1"})

            executor.apply(operation["id"], planned)

            self.assertEqual(executor.release_source.verified, [("v1.0.1", True)])
            self.assertEqual(store.get(operation["id"])["status"], "failed_pre_switch")
            self.assertNotIn("pull", [call[0] for call in deployment.calls])
            self.assertNotIn("switch", [call[0] for call in deployment.calls])
            self.assertEqual(slots.read()["current"]["release"]["version"], "v1.0.0")

    def test_health_failure_rolls_back_only_the_application_when_safe(self):
        with tempfile.TemporaryDirectory() as directory:
            current = manifest("v1.0.0", "1", accepts=["animemo-db-v1", "animemo-db-v2"])
            target = manifest("v1.1.0", "2", migration=True, rollback="conditional", contract="animemo-db-v2", accepts=["animemo-db-v1", "animemo-db-v2"])
            deployment = FakeDeployment(fail_at="verify_health")
            executor, store, slots = self.setup_executor(directory, current, target, deployment)
            operation = store.create("apply_update", {"version": "v1.1.0"})

            executor.apply(operation["id"], target)

            self.assertEqual(store.get(operation["id"])["status"], "rolled_back")
            self.assertEqual([call for call in deployment.calls if call[0] == "switch"][-1][1], "v1.0.0")
            self.assertEqual(slots.read()["current"]["release"]["version"], "v1.0.0")

    def test_health_failure_requires_manual_recovery_when_previous_rejects_new_schema(self):
        with tempfile.TemporaryDirectory() as directory:
            current = manifest("v1.0.0", "1")
            target = manifest("v1.1.0", "2", migration=True, rollback="conditional", contract="animemo-db-v2", accepts=["animemo-db-v1", "animemo-db-v2"])
            deployment = FakeDeployment(fail_at="verify_health")
            executor, store, _ = self.setup_executor(directory, current, target, deployment)
            operation = store.create("apply_update", {"version": "v1.1.0"})

            executor.apply(operation["id"], target)

            self.assertEqual(store.get(operation["id"])["status"], "manual_recovery_required")
            self.assertEqual(len([call for call in deployment.calls if call[0] == "switch"]), 1)

    def test_rollback_rechecks_actual_enabled_plugin_apis_before_switch(self):
        with tempfile.TemporaryDirectory() as directory:
            current = manifest("v1.1.0", "2")
            previous = manifest("v1.0.0", "1")
            deployment = FakeDeployment(enabled_plugin_apis={3})
            executor, store, slots = self.setup_executor(directory, current, previous, deployment)
            slots.promote(previous, operation_id="seed")
            slots.restore_previous(operation_id="seed-restore")
            operation = store.create("rollback_previous", {"version": "v1.0.0"})

            executor.rollback(operation["id"], previous)

            self.assertEqual(store.get(operation["id"])["status"], "failed_pre_switch")
            self.assertNotIn("switch", [call[0] for call in deployment.calls])

    def test_rollback_refetches_and_rejects_a_release_that_differs_from_previous(self):
        with tempfile.TemporaryDirectory() as directory:
            current = manifest("v1.1.0", "2")
            previous = manifest("v1.0.0", "1")
            changed = manifest("v1.0.0", "3")
            deployment = FakeDeployment()
            executor, store, slots = self.setup_executor(directory, current, changed, deployment)
            slots.promote(previous, operation_id="seed")
            slots.restore_previous(operation_id="seed-restore")
            operation = store.create("rollback_previous", {"version": "v1.0.0"})

            executor.rollback(operation["id"], previous)

            self.assertEqual(executor.release_source.verified, [("v1.0.0", True)])
            self.assertEqual(store.get(operation["id"])["status"], "failed_pre_switch")
            self.assertNotIn("pull", [call[0] for call in deployment.calls])
            self.assertNotIn("switch", [call[0] for call in deployment.calls])
            self.assertEqual(slots.read()["current"]["release"]["version"], "v1.1.0")


if __name__ == "__main__":
    unittest.main()
