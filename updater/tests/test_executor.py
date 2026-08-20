from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from release.contract import build_manifest
from updater.errors import StateError
from updater.executor import UpdateExecutor
from updater.runtime_state import RuntimeState
from updater.slots import ReleaseSlots
from updater.state import OperationStore
from updater.transport import ExplicitTransportPolicy


def manifest(
    version: str,
    digit: str,
    *,
    migration=False,
    rollback="safe",
    accepts=None,
    contract="animemo-db-v1",
    configuration_contract="animemo-config-v1",
    configuration_accepts=None,
):
    channel = "stable" if "-" not in version else "rc"
    return build_manifest(
        version=version,
        channel=channel,
        commit=digit * 40,
        created_at=datetime(2026, 8, 12, tzinfo=timezone.utc),
        api_digest="sha256:" + digit * 64,
        web_digest="sha256:" + digit * 64,
        deployment_contract_sha256="sha256:0be5fdf5f87275755e06a2e2b6523c24e16d6aa1db48d8d58e8cfea969b674df",
        installer_materials_sha256="sha256:" + "f" * 64,
        deployment_files=[
            {"path": "deploy/docker-compose.yml", "sha256": "sha256:" + "d" * 64},
            {"path": "updater/docker-compose.runtime.yml", "sha256": "sha256:" + "e" * 64},
        ],
        minimum_updater_version="1.0.0",
        database_contract=contract,
        database_accepts=accepts or [contract],
        migration_required=migration,
        migration_policy="additive-backward-compatible" if migration else "none",
        application_rollback=rollback,
        configuration_contract=configuration_contract,
        configuration_accepts=configuration_accepts or [configuration_contract],
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
    def __init__(
        self,
        *,
        fail_at=None,
        enabled_plugin_apis=None,
        plugin_api_snapshots=None,
        runtime_contracts=None,
        database_transition="target",
    ):
        self.fail_at = fail_at
        self.failed = False
        self.calls = []
        self.enabled_plugin_apis = enabled_plugin_apis or {2}
        self.plugin_api_snapshots = list(plugin_api_snapshots or [])
        self.switch_contracts = []
        self.health_contracts = []
        self.database_transition = database_transition
        self.runtime_contracts = runtime_contracts or {
            "databaseContract": "animemo-db-v1",
            "configurationContract": "animemo-config-v1",
        }

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
    def switch(self, manifest, *, live_contracts=None):
        self._call("switch", manifest["release"]["version"])
        self.switch_contracts.append(dict(live_contracts or {}))
        if live_contracts is not None:
            self.runtime_contracts = dict(live_contracts)
    def verify_health(self, manifest, *, live_contracts=None):
        self._call("verify_health", manifest["release"]["version"])
        self.health_contracts.append(dict(live_contracts or {}))
    def inspect_runtime_contracts(self, manifest):
        self._call("inspect_runtime_contracts", manifest["release"]["version"])
        return dict(self.runtime_contracts)
    def inspect_database_transition(self, current, target):
        self._call(
            "inspect_database_transition",
            current["release"]["version"],
            target["release"]["version"],
        )
        return self.database_transition
    def inspect_enabled_plugin_apis(self, manifest):
        self._call("inspect_enabled_plugin_apis", manifest["release"]["version"])
        if self.plugin_api_snapshots:
            return self.plugin_api_snapshots.pop(0)
        return self.enabled_plugin_apis


class StatefulRuntimeBinding:
    def __init__(self, *, failed_replacements: int = 0):
        self.failed_replacements = failed_replacements
        self.refreshes = 0
        self.release_version = None

    def refresh(self):
        self.refreshes += 1

    def replace_release(self, manifest):
        if self.failed_replacements:
            self.failed_replacements -= 1
            raise StateError("LOCATOR_CONCURRENT_MODIFICATION")
        self.release_version = manifest["release"]["version"]


class UpdateExecutorTests(unittest.TestCase):
    def setup_executor(
        self,
        directory,
        current,
        target,
        deployment,
        *,
        runtime_binding=None,
    ):
        root = Path(directory)
        slots = ReleaseSlots(root / "releases")
        slots.import_current(current)
        store = OperationStore(root / "state")
        runtime_state = RuntimeState(root / "state")
        runtime_state.initialize_from_manifest(current, enabled_plugin_apis={2})
        runtime_binding = runtime_binding or mock.Mock()
        executor = UpdateExecutor(
            store=store,
            slots=slots,
            release_source=FakeReleaseSource(target),
            deployment=deployment,
            runtime_state=runtime_state,
            runtime_binding=runtime_binding,
            lock_path=root / "state" / "update.lock",
            updater_version="1.0.0",
        )
        return executor, store, slots

    @staticmethod
    def enter_manual_recovery(store):
        operation = store.create("rollback_previous", {"version": "v1.0.0"})
        for status in [
            "preflight", "fetching", "verifying", "pulling", "switching",
            "manual_recovery_required",
        ]:
            store.transition(operation["id"], status)
        return operation

    def test_host_reconciliation_clears_the_block_only_after_live_verification(self):
        with tempfile.TemporaryDirectory() as directory:
            current = manifest("v1.0.0", "1")
            deployment = FakeDeployment()
            executor, store, _ = self.setup_executor(directory, current, current, deployment)
            operation = self.enter_manual_recovery(store)

            reconciled = executor.reconcile(operation["id"])

            self.assertEqual(reconciled["status"], "reconciled")
            self.assertIsNone(store.recovery_block())
            self.assertEqual(
                [call[0] for call in deployment.calls],
                [
                    "inspect_enabled_plugin_apis",
                    "switch",
                    "verify_health",
                    "inspect_runtime_contracts",
                    "inspect_enabled_plugin_apis",
                ],
            )

    def test_failed_live_reconciliation_preserves_the_manual_recovery_block(self):
        with tempfile.TemporaryDirectory() as directory:
            current = manifest("v1.0.0", "1")
            deployment = FakeDeployment(fail_at="inspect_runtime_contracts")
            executor, store, _ = self.setup_executor(directory, current, current, deployment)
            operation = self.enter_manual_recovery(store)

            with self.assertRaisesRegex(RuntimeError, "inspect_runtime_contracts failed"):
                executor.reconcile(operation["id"])

            self.assertEqual(store.recovery_block()["id"], operation["id"])
            self.assertEqual(store.get(operation["id"])["status"], "manual_recovery_required")

    def test_apply_reconciliation_without_an_exact_recovery_receipt_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            current = manifest("v1.0.0", "1")
            deployment = FakeDeployment()
            executor, store, _ = self.setup_executor(directory, current, current, deployment)
            operation = store.create("apply_update", {"version": "v1.0.1"})
            for transition in [
                "preflight",
                "fetching",
                "verifying",
                "pulling",
                "migrating",
                "manual_recovery_required",
            ]:
                store.transition(operation["id"], transition)

            with self.assertRaisesRegex(StateError, "receipt is missing"):
                executor.reconcile(operation["id"])

            self.assertEqual(store.recovery_block()["id"], operation["id"])
            self.assertEqual(deployment.calls, [])

    def test_reconciliation_never_downgrades_the_durable_runtime_contracts(self):
        with tempfile.TemporaryDirectory() as directory:
            current = manifest(
                "v1.0.0", "1",
                accepts=["animemo-db-v1", "animemo-db-v2"],
                configuration_accepts=["animemo-config-v1", "animemo-config-v2"],
            )
            deployment = FakeDeployment(
                runtime_contracts={
                    "databaseContract": "animemo-db-v1",
                    "configurationContract": "animemo-config-v1",
                },
            )
            executor, store, _ = self.setup_executor(directory, current, current, deployment)
            executor.runtime_state.write({
                "databaseContract": "animemo-db-v2",
                "configurationContract": "animemo-config-v2",
                "enabledPluginApis": [2],
            })
            operation = self.enter_manual_recovery(store)

            reconciled = executor.reconcile(operation["id"])

            self.assertEqual(reconciled["status"], "reconciled")
            self.assertEqual(
                deployment.switch_contracts,
                [{
                    "databaseContract": "animemo-db-v2",
                    "configurationContract": "animemo-config-v2",
                }],
            )
            self.assertEqual(
                executor.runtime_state.read(),
                {
                    "databaseContract": "animemo-db-v2",
                    "configurationContract": "animemo-config-v2",
                    "enabledPluginApis": [2],
                },
            )

    def test_reconciliation_recovers_a_committed_migration_without_replaying_it(self):
        with tempfile.TemporaryDirectory() as directory:
            current = manifest(
                "v1.0.0", "1",
                accepts=["animemo-db-v1", "animemo-db-v2"],
            )
            target = manifest(
                "v1.1.0", "2",
                migration=True,
                rollback="conditional",
                contract="animemo-db-v2",
                accepts=["animemo-db-v1", "animemo-db-v2"],
            )
            deployment = FakeDeployment(
                fail_at="migrate",
                database_transition="target",
            )
            executor, store, _ = self.setup_executor(directory, current, target, deployment)
            operation = store.create("apply_update", {"version": "v1.1.0"})

            executor.apply(operation["id"], target)
            self.assertEqual(store.get(operation["id"])["status"], "manual_recovery_required")
            self.assertEqual(executor.runtime_state.read()["databaseContract"], "animemo-db-v1")

            reconciled = executor.reconcile(operation["id"])

            self.assertEqual(reconciled["status"], "reconciled")
            self.assertEqual(executor.runtime_state.read()["databaseContract"], "animemo-db-v2")
            self.assertEqual(
                [call[0] for call in deployment.calls].count("migrate"),
                1,
            )
            self.assertIn(
                "inspect_database_transition",
                [call[0] for call in deployment.calls],
            )

    def test_reconciliation_replays_only_idempotent_bootstrap_after_uncertain_exit(self):
        with tempfile.TemporaryDirectory() as directory:
            current = manifest(
                "v1.0.0", "1",
                configuration_accepts=["animemo-config-v1", "animemo-config-v2"],
            )
            target = manifest(
                "v1.0.1", "2",
                configuration_contract="animemo-config-v2",
                configuration_accepts=["animemo-config-v1", "animemo-config-v2"],
            )
            deployment = FakeDeployment(fail_at="bootstrap")
            executor, store, _ = self.setup_executor(directory, current, target, deployment)
            operation = store.create("apply_update", {"version": "v1.0.1"})

            executor.apply(operation["id"], target)
            self.assertEqual(store.get(operation["id"])["status"], "manual_recovery_required")
            self.assertEqual(
                executor.runtime_state.read()["configurationContract"],
                "animemo-config-v1",
            )

            reconciled = executor.reconcile(operation["id"])

            self.assertEqual(reconciled["status"], "reconciled")
            self.assertEqual(
                executor.runtime_state.read()["configurationContract"],
                "animemo-config-v2",
            )
            self.assertEqual(
                [call[0] for call in deployment.calls].count("bootstrap"),
                2,
            )

    def test_indeterminate_partial_migration_keeps_manual_recovery_blocked(self):
        with tempfile.TemporaryDirectory() as directory:
            current = manifest(
                "v1.0.0", "1",
                accepts=["animemo-db-v1", "animemo-db-v2"],
            )
            target = manifest(
                "v1.1.0", "2",
                migration=True,
                rollback="conditional",
                contract="animemo-db-v2",
                accepts=["animemo-db-v1", "animemo-db-v2"],
            )
            deployment = FakeDeployment(
                fail_at="migrate",
                database_transition="indeterminate",
            )
            executor, store, _ = self.setup_executor(directory, current, target, deployment)
            operation = store.create("apply_update", {"version": "v1.1.0"})
            executor.apply(operation["id"], target)

            with self.assertRaisesRegex(StateError, "indeterminate"):
                executor.reconcile(operation["id"])

            self.assertEqual(store.recovery_block()["id"], operation["id"])
            self.assertEqual(executor.runtime_state.read()["databaseContract"], "animemo-db-v1")

    def test_migration_backup_and_switch_are_ordered(self):
        with tempfile.TemporaryDirectory() as directory:
            current = manifest("v1.0.0", "1", accepts=["animemo-db-v1", "animemo-db-v2"])
            target = manifest(
                "v1.1.0", "2",
                migration=True,
                rollback="conditional",
                contract="animemo-db-v2",
                accepts=["animemo-db-v1", "animemo-db-v2"],
                configuration_contract="animemo-config-v2",
                configuration_accepts=["animemo-config-v1", "animemo-config-v2"],
            )
            deployment = FakeDeployment()
            executor, store, slots = self.setup_executor(directory, current, target, deployment)
            operation = store.create("apply_update", {"version": "v1.1.0"})

            executor.apply(operation["id"], target)

            self.assertEqual(store.get(operation["id"])["status"], "succeeded")
            self.assertEqual(
                [call[0] for call in deployment.calls],
                ["preflight", "inspect_enabled_plugin_apis", "backup_database", "verify_recent_backup", "pull", "migrate", "bootstrap", "inspect_enabled_plugin_apis", "switch", "verify_health"],
            )
            self.assertEqual(slots.read()["current"]["release"]["version"], "v1.1.0")
            self.assertEqual(
                executor.runtime_state.read(),
                {
                    "databaseContract": "animemo-db-v2",
                    "configurationContract": "animemo-config-v2",
                    "enabledPluginApis": [2],
                },
            )

    def test_modern_release_source_binds_verified_materials_and_policy_to_image_acquisition(self):
        class ModernSource:
            def __init__(self, target):
                self.target = target
                self.transport_policy = ExplicitTransportPolicy.official_mirror()

            def fetch_verified_materials(self, version, updater_version="1.0.0", refresh=False):
                del version, updater_version, refresh
                return SimpleNamespace(manifest=self.target)

        class ModernDeployment(FakeDeployment):
            def pull(self, manifest):
                raise AssertionError("legacy manifest-only pull must not run")

            def pull_verified(self, materials, policy):
                self._call(
                    "pull_verified",
                    materials.manifest["release"]["version"],
                    policy.identity,
                )

        with tempfile.TemporaryDirectory() as directory:
            current = manifest("v1.0.0", "1")
            target = manifest("v1.0.1", "2")
            deployment = ModernDeployment()
            executor, store, _ = self.setup_executor(
                directory, current, target, deployment
            )
            executor.release_source = ModernSource(target)
            operation = store.create("apply_update", {"version": "v1.0.1"})

            executor.apply(operation["id"], target)

            self.assertEqual(store.get(operation["id"])["status"], "succeeded")
            self.assertEqual(
                [call[0] for call in deployment.calls].count("pull_verified"), 1
            )
            self.assertNotIn("pull", [call[0] for call in deployment.calls])

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

    def test_unreadable_fresh_backup_prevents_pull_migration_and_switch(self):
        with tempfile.TemporaryDirectory() as directory:
            current = manifest("v1.0.0", "1", accepts=["animemo-db-v1", "animemo-db-v2"])
            target = manifest(
                "v1.1.0", "2",
                migration=True,
                rollback="conditional",
                contract="animemo-db-v2",
                accepts=["animemo-db-v1", "animemo-db-v2"],
                configuration_contract="animemo-config-v2",
                configuration_accepts=["animemo-config-v1", "animemo-config-v2"],
            )
            deployment = FakeDeployment(fail_at="verify_recent_backup")
            executor, store, slots = self.setup_executor(directory, current, target, deployment)
            operation = store.create("apply_update", {"version": "v1.1.0"})

            executor.apply(operation["id"], target)

            self.assertEqual(store.get(operation["id"])["status"], "failed_pre_switch")
            self.assertEqual(
                [call[0] for call in deployment.calls],
                ["preflight", "inspect_enabled_plugin_apis", "backup_database", "verify_recent_backup"],
            )
            self.assertEqual(slots.read()["current"]["release"]["version"], "v1.0.0")

    def test_bootstrap_cannot_introduce_an_unsupported_enabled_plugin_api(self):
        with tempfile.TemporaryDirectory() as directory:
            current = manifest("v1.0.0", "1")
            target = manifest("v1.0.1", "2")
            deployment = FakeDeployment(plugin_api_snapshots=[{2}, {2, 3}])
            executor, store, slots = self.setup_executor(directory, current, target, deployment)
            operation = store.create("apply_update", {"version": "v1.0.1"})

            executor.apply(operation["id"], target)

            self.assertEqual(store.get(operation["id"])["status"], "manual_recovery_required")
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
            current = manifest(
                "v1.0.0", "1",
                accepts=["animemo-db-v1", "animemo-db-v2"],
                configuration_accepts=["animemo-config-v1", "animemo-config-v2"],
            )
            target = manifest(
                "v1.1.0", "2",
                migration=True,
                rollback="conditional",
                contract="animemo-db-v2",
                accepts=["animemo-db-v1", "animemo-db-v2"],
                configuration_contract="animemo-config-v2",
                configuration_accepts=["animemo-config-v1", "animemo-config-v2"],
            )
            deployment = FakeDeployment(fail_at="verify_health")
            executor, store, slots = self.setup_executor(directory, current, target, deployment)
            operation = store.create("apply_update", {"version": "v1.1.0"})

            executor.apply(operation["id"], target)

            self.assertEqual(store.get(operation["id"])["status"], "rolled_back")
            self.assertEqual([call for call in deployment.calls if call[0] == "switch"][-1][1], "v1.0.0")
            self.assertEqual(slots.read()["current"]["release"]["version"], "v1.0.0")
            self.assertEqual(executor.runtime_state.read()["databaseContract"], "animemo-db-v2")
            self.assertEqual(executor.runtime_state.read()["configurationContract"], "animemo-config-v2")

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

    def test_successful_application_rollback_commits_live_runtime_contracts(self):
        with tempfile.TemporaryDirectory() as directory:
            current = manifest(
                "v1.1.0", "2",
                contract="animemo-db-v2",
                configuration_contract="animemo-config-v2",
            )
            previous = manifest(
                "v1.0.0", "1",
                accepts=["animemo-db-v1", "animemo-db-v2"],
                configuration_contract="animemo-config-v1",
                configuration_accepts=["animemo-config-v1", "animemo-config-v2"],
            )
            deployment = FakeDeployment()
            executor, store, slots = self.setup_executor(directory, current, previous, deployment)
            slots.promote(previous, operation_id="seed")
            slots.restore_previous(operation_id="seed-restore")
            executor.runtime_state.write({
                "databaseContract": "animemo-db-v2",
                "configurationContract": "animemo-config-v2",
                "enabledPluginApis": [2],
            })
            operation = store.create("rollback_previous", {"version": "v1.0.0"})

            executor.rollback(operation["id"], previous)

            self.assertEqual(store.get(operation["id"])["status"], "rolled_back")
            self.assertEqual(
                executor.runtime_state.read(),
                {
                    "databaseContract": "animemo-db-v2",
                    "configurationContract": "animemo-config-v2",
                    "enabledPluginApis": [2],
                },
            )

    def test_apply_commits_the_target_release_to_the_canonical_binding(self):
        with tempfile.TemporaryDirectory() as directory:
            current = manifest("v1.0.0", "1")
            target = manifest("v1.0.1", "2")
            binding = StatefulRuntimeBinding()
            executor, store, slots = self.setup_executor(
                directory,
                current,
                target,
                FakeDeployment(),
                runtime_binding=binding,
            )
            operation = store.create("apply_update", {"version": "v1.0.1"})

            result = executor.apply(operation["id"], target)

            self.assertEqual(result["status"], "succeeded")
            self.assertEqual(slots.read()["current"], target)
            self.assertEqual(binding.release_version, "v1.0.1")
            self.assertEqual(binding.refreshes, 1)

    def test_rollback_commits_previous_to_the_canonical_binding(self):
        with tempfile.TemporaryDirectory() as directory:
            current = manifest("v1.1.0", "2")
            previous = manifest("v1.0.0", "1")
            binding = StatefulRuntimeBinding()
            executor, store, slots = self.setup_executor(
                directory,
                current,
                previous,
                FakeDeployment(),
                runtime_binding=binding,
            )
            slots.promote(previous, operation_id="seed")
            slots.restore_previous(operation_id="seed-restore")
            operation = store.create(
                "rollback_previous", {"version": "v1.0.0"}
            )

            result = executor.rollback(operation["id"], previous)

            self.assertEqual(result["status"], "rolled_back")
            self.assertEqual(slots.read()["current"], previous)
            self.assertEqual(binding.release_version, "v1.0.0")
            self.assertEqual(binding.refreshes, 1)

    def test_apply_locator_cas_failure_blocks_until_reconcile_repairs_binding(self):
        with tempfile.TemporaryDirectory() as directory:
            current = manifest("v1.0.0", "1")
            target = manifest("v1.0.1", "2")
            binding = StatefulRuntimeBinding(failed_replacements=1)
            executor, store, slots = self.setup_executor(
                directory,
                current,
                target,
                FakeDeployment(),
                runtime_binding=binding,
            )
            operation = store.create("apply_update", {"version": "v1.0.1"})

            failed = executor.apply(operation["id"], target)

            self.assertEqual(failed["status"], "manual_recovery_required")
            self.assertEqual(slots.read()["current"], target)
            self.assertIsNone(binding.release_version)
            self.assertEqual(store.recovery_block()["id"], operation["id"])

            repaired = executor.reconcile(operation["id"])

            self.assertEqual(repaired["status"], "reconciled")
            self.assertEqual(binding.release_version, "v1.0.1")
            self.assertIsNone(store.recovery_block())

    def test_rollback_locator_cas_failure_blocks_until_reconcile_repairs_binding(self):
        with tempfile.TemporaryDirectory() as directory:
            current = manifest("v1.1.0", "2")
            previous = manifest("v1.0.0", "1")
            binding = StatefulRuntimeBinding(failed_replacements=1)
            executor, store, slots = self.setup_executor(
                directory,
                current,
                previous,
                FakeDeployment(),
                runtime_binding=binding,
            )
            slots.promote(previous, operation_id="seed")
            slots.restore_previous(operation_id="seed-restore")
            operation = store.create(
                "rollback_previous", {"version": "v1.0.0"}
            )

            failed = executor.rollback(operation["id"], previous)

            self.assertEqual(failed["status"], "manual_recovery_required")
            self.assertEqual(slots.read()["current"], previous)
            self.assertIsNone(binding.release_version)
            self.assertEqual(store.recovery_block()["id"], operation["id"])

            repaired = executor.reconcile(operation["id"])

            self.assertEqual(repaired["status"], "reconciled")
            self.assertEqual(binding.release_version, "v1.0.0")
            self.assertIsNone(store.recovery_block())


if __name__ == "__main__":
    unittest.main()
