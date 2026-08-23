from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path
from unittest import mock

from durability.instance import (
    InstanceSnapshot,
    ListenIdentity,
    instance_locator_digest,
    instance_locator_payload,
    parse_instance_locator,
    release_identity_from_manifest,
)
from durability.managed_config import (
    ListenConfig,
    LocalManagedConfigStore,
    canonical_managed_env_bytes,
    parse_managed_config,
)
from scripts.tests.test_managed_config import (
    CREDENTIAL_KEY,
    DATABASE_SECRET,
    DJANGO_SECRET,
)
from scripts.tests.test_managed_config import (
    payload as config_payload,
)
from updater.configuration import (
    ConfigurationChangeRequest,
    ConfigurationError,
    ConfigurationManager,
    ConfigurationOutcome,
    LocalConfigurationOperationJournal,
)
from updater.tests.test_deployment import manifest


class MemoryJournal:
    def __init__(self) -> None:
        self.operation_id = "a" * 32
        self.states: list[tuple[str, str | None, bool]] = []

    def create(self, plan) -> str:
        self.states.append(("PLANNED", None, False))
        return self.operation_id

    def transition(
        self,
        operation_id,
        state,
        *,
        failure_code=None,
        manual_recovery_required=False,
    ) -> None:
        assert operation_id == self.operation_id
        self.states.append((state, failure_code, manual_recovery_required))


class FakeConfigurationHost:
    def __init__(self, snapshot: InstanceSnapshot, current_manifest) -> None:
        self.current_snapshot = snapshot
        self.manifest = current_manifest
        self.calls: list[object] = []
        self.fail_health_once = False
        self.fail_doctor_once = False
        self.fail_reconcile_after = 0
        self.reconcile_count = 0

    def snapshot(self):
        self.calls.append("snapshot")
        return self.current_snapshot

    def acquire_lock(self):
        self.calls.append("lock")
        return nullcontext()

    def current_manifest(self):
        self.calls.append("manifest")
        return self.manifest

    def validate_listen(self, current, proposed):
        self.calls.append(("validate-listen", current, proposed))

    def refresh_runtime(self, config, snapshot):
        self.calls.append(
            (
                "refresh-runtime",
                config.config_revision,
                snapshot.locator.config_revision,
            )
        )

    def reconcile_application(self, current_manifest):
        assert current_manifest == self.manifest
        self.reconcile_count += 1
        self.calls.append("reconcile-api-web")
        if (
            self.fail_reconcile_after
            and self.reconcile_count >= self.fail_reconcile_after
        ):
            raise OSError("injected reconcile failure")

    def verify_health_and_release(self, current_manifest):
        assert current_manifest == self.manifest
        self.calls.append("health-and-exact-release")
        if self.fail_health_once:
            self.fail_health_once = False
            raise OSError("injected health failure")

    def replace_locator(self, locator, *, expected_digest):
        self.calls.append(("replace-locator", expected_digest))
        if self.current_snapshot.digest != expected_digest:
            raise ConfigurationError("LOCATOR_CONCURRENT_MODIFICATION")
        marker = "2" if self.current_snapshot.digest.endswith("1" * 64) else "3"
        self.current_snapshot = InstanceSnapshot(
            locator=locator,
            digest="sha256:" + marker * 64,
            storage_digest="sha256:" + marker * 64,
        )
        return self.current_snapshot

    def doctor_accept(self, snapshot, config, current_manifest):
        assert snapshot == self.current_snapshot
        assert current_manifest == self.manifest
        self.calls.append(("doctor", config.config_revision))
        if self.fail_doctor_once:
            self.fail_doctor_once = False
            raise OSError("injected Doctor failure")


class ConfigurationManagerTests(unittest.TestCase):
    def make_runtime(self, root: Path):
        target = manifest()
        raw = config_payload()
        raw["instanceId"] = "abcdefab-1234-5678-9234-567812345678"
        raw["configRevision"] = "11111111-1111-4111-8111-111111111111"
        config = parse_managed_config(json.dumps(raw).encode("utf-8"))

        config_root = root / "config"
        runtime_root = root / "runtime"
        config_root.mkdir(mode=0o700)
        runtime_root.mkdir(mode=0o750)
        store = LocalManagedConfigStore(
            config_root=config_root,
            runtime_root=runtime_root,
        )
        store.write(config, expected_revision=None, must_not_exist=True)
        store.rebuild_runtime_env(
            locator_digest="sha256:" + "1" * 64,
            expected_revision=config.config_revision,
        )

        locator = parse_instance_locator(
            {
                "schemaVersion": 2,
                "instanceName": "default",
                "instanceId": config.instance_id,
                "appRoot": "/opt/animemo-instances/default",
                "dataRoot": "/data/animemo-instances/default",
                "updaterStateRoot": "/var/lib/animemo-updater/instances/default",
                "updaterRuntimeRoot": "/run/animemo-updater/default",
                "deploymentProfile": "v1.1-instance-scoped",
                "composeProject": "animemo-default",
                "updaterService": "animemo-updater@default.service",
                "updaterSocketPath": "/run/animemo-updater/default/updater.sock",
                "listen": {"host": config.listen.host, "port": config.listen.port},
                "publicOrigin": config.public_origin,
                "managedConfigPath": "/data/animemo-instances/default/config/animemo.json",
                "configRevision": config.config_revision,
                "releaseIdentity": dict(release_identity_from_manifest(target)),
                "ownershipReceiptDigest": "sha256:" + "9" * 64,
            }
        )
        snapshot = InstanceSnapshot(
            locator=locator,
            digest="sha256:" + "1" * 64,
            storage_digest="sha256:" + "1" * 64,
        )
        host = FakeConfigurationHost(snapshot, target)
        journal = MemoryJournal()
        revisions = iter(
            (
                "22222222-2222-4222-8222-222222222222",
                "33333333-3333-4333-8333-333333333333",
                "44444444-4444-4444-8444-444444444444",
            )
        )
        manager = ConfigurationManager(
            config_store=store,
            host=host,
            journal=journal,
            revision_factory=lambda: next(revisions),
        )
        return manager, store, host, journal, config

    def test_show_is_aligned_and_discloses_only_secret_status(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager, _store, _host, _journal, _config = self.make_runtime(
                Path(directory)
            )

            rendered = json.dumps(manager.show(), ensure_ascii=False, sort_keys=True)

            self.assertNotIn(DJANGO_SECRET, rendered)
            self.assertNotIn(DATABASE_SECRET, rendered)
            self.assertNotIn(CREDENTIAL_KEY, rendered)
            shown = manager.show()["configuration"]
            statuses = shown["secretStatus"]
            self.assertTrue(
                set(statuses.values()).issubset({"configured", "missing", "invalid"})
            )
            self.assertEqual(shown["database"], {"name": "animemo", "user": "animemo"})
            self.assertEqual(
                shown["trustedOrigins"]["allowedHosts"],
                ["assets.animemo.example"],
            )

    def test_validate_setters_and_dry_run_are_non_mutating(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager, store, host, journal, current = self.make_runtime(Path(directory))
            before_authority = store.authority_path.read_bytes()
            before_env = store.runtime_env_path.read_bytes()

            origin = manager.set_origin("https://new.example")
            listen = manager.set_listen(ListenConfig("127.0.0.2", 18088))
            dry_run = manager.dry_run(
                ConfigurationChangeRequest(public_origin="https://dry.example")
            )

            self.assertEqual(origin.changed_fields, ("publicOrigin",))
            self.assertEqual(listen.changed_fields, ("listen",))
            self.assertEqual(dry_run["outcome"], "DRY_RUN")
            self.assertEqual(dry_run["requiredReconcile"], ["api", "web"])
            self.assertEqual(store.authority_path.read_bytes(), before_authority)
            self.assertEqual(store.runtime_env_path.read_bytes(), before_env)
            self.assertEqual(store.read(), current)
            self.assertEqual(journal.states, [])
            self.assertNotIn("reconcile-api-web", host.calls)

    def test_direct_access_requires_explicit_acceptance_and_emits_warnings(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager, *_ = self.make_runtime(Path(directory))
            with self.assertRaisesRegex(
                ConfigurationError, "CONFIG_DIRECT_EXPOSURE_ACCEPTANCE_REQUIRED"
            ):
                manager.set_listen(ListenConfig("0.0.0.0", 8088))
            with self.assertRaisesRegex(
                ConfigurationError, "CONFIG_INSECURE_HTTP_ACCEPTANCE_REQUIRED"
            ):
                manager.set_origin("http://direct.example")

            exposed = manager.set_listen(
                ListenConfig("0.0.0.0", 8088), accept_direct_exposure=True
            )
            insecure = manager.set_origin(
                "http://direct.example", accept_insecure_http=True
            )
            self.assertIn("DIRECT_NETWORK_EXPOSURE", exposed.warnings)
            self.assertIn("HTTP_WITHOUT_TLS", insecure.warnings)

    def test_invalid_typed_request_fails_with_secret_safe_code(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager, *_ = self.make_runtime(Path(directory))
            with self.assertRaisesRegex(ConfigurationError, "CONFIG_LISTEN_INVALID"):
                manager.set_listen(ListenConfig("not-an-ip", 8088))
            with self.assertRaisesRegex(ConfigurationError, "CONFIG_REQUEST_INVALID"):
                manager.validate(
                    ConfigurationChangeRequest(
                        public_origin="https://new.example",
                        accept_insecure_http=1,  # type: ignore[arg-type]
                    )
                )

    def test_apply_refreshes_env_reconciles_only_application_and_verifies_all_gates(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager, store, host, journal, previous = self.make_runtime(Path(directory))
            plan = manager.validate(
                ConfigurationChangeRequest(
                    public_origin="https://new.example",
                    listen=ListenConfig("127.0.0.2", 18088),
                )
            )

            result = manager.apply(plan, accepted_plan_digest=plan.plan_digest)

            self.assertEqual(result.outcome, ConfigurationOutcome.APPLIED)
            self.assertEqual(store.read(), plan.proposed)
            self.assertEqual(
                store.runtime_env_path.read_bytes(),
                canonical_managed_env_bytes(
                    plan.proposed,
                    locator_digest=instance_locator_digest(
                        host.current_snapshot.locator
                    ),
                ),
            )
            self.assertEqual(
                host.current_snapshot.locator.config_revision, plan.next_revision
            )
            self.assertEqual(
                host.current_snapshot.locator.public_origin, "https://new.example"
            )
            self.assertEqual(
                host.current_snapshot.locator.listen,
                ListenIdentity("127.0.0.2", 18088),
            )
            self.assertEqual(host.calls.count("reconcile-api-web"), 1)
            self.assertIn("health-and-exact-release", host.calls)
            self.assertIn(("doctor", plan.next_revision), host.calls)
            self.assertEqual(
                [state for state, _code, _manual in journal.states],
                ["PLANNED", "APPLYING", "VERIFYING", "SUCCEEDED"],
            )
            self.assertEqual(plan.proposed.database, previous.database)
            self.assertEqual(plan.proposed.redis, previous.redis)
            self.assertEqual(plan.proposed.application, previous.application)
            self.assertEqual(plan.proposed.integrations, previous.integrations)

    def test_wrong_acceptance_and_stale_locator_fail_before_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager, store, host, journal, current = self.make_runtime(Path(directory))
            plan = manager.set_origin("https://new.example")

            with self.assertRaisesRegex(
                ConfigurationError, "CONFIG_PLAN_ACCEPTANCE_REQUIRED"
            ):
                manager.apply(plan, accepted_plan_digest="sha256:" + "f" * 64)

            host.current_snapshot = replace(
                host.current_snapshot, digest="sha256:" + "9" * 64
            )
            with self.assertRaisesRegex(ConfigurationError, "CONFIG_PLAN_STALE"):
                manager.apply(plan, accepted_plan_digest=plan.plan_digest)
            self.assertEqual(store.read(), current)
            self.assertEqual(journal.states, [])

    def test_health_failure_rolls_back_config_env_runtime_and_locator(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager, store, host, journal, previous = self.make_runtime(Path(directory))
            old_snapshot = host.current_snapshot
            plan = manager.set_origin("https://new.example")
            host.fail_health_once = True

            result = manager.apply(plan, accepted_plan_digest=plan.plan_digest)

            self.assertEqual(result.outcome, ConfigurationOutcome.CONFIG_APPLY_FAILED)
            self.assertEqual(store.read(), previous)
            self.assertEqual(
                store.runtime_env_path.read_bytes(),
                canonical_managed_env_bytes(
                    previous, locator_digest=old_snapshot.digest
                ),
            )
            self.assertEqual(host.current_snapshot, old_snapshot)
            self.assertEqual(host.calls.count("reconcile-api-web"), 2)
            self.assertEqual(
                [state for state, _code, _manual in journal.states],
                ["PLANNED", "APPLYING", "VERIFYING", "ROLLING_BACK", "ROLLED_BACK"],
            )

    def test_config_write_failure_distinguishes_pre_replace_and_post_replace(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager, store, host, journal, previous = self.make_runtime(Path(directory))
            plan = manager.set_origin("https://new.example")
            with mock.patch.object(
                store, "write", side_effect=OSError("injected pre-replace failure")
            ):
                result = manager.apply(plan, accepted_plan_digest=plan.plan_digest)

            self.assertEqual(result.outcome, ConfigurationOutcome.CONFIG_APPLY_FAILED)
            self.assertEqual(store.read(), previous)
            self.assertNotIn("reconcile-api-web", host.calls)
            self.assertEqual(
                [state for state, _code, _manual in journal.states],
                ["PLANNED", "APPLYING", "CONFIG_APPLY_FAILED"],
            )

        with tempfile.TemporaryDirectory() as directory:
            manager, store, host, journal, previous = self.make_runtime(Path(directory))
            plan = manager.set_origin("https://new.example")
            original_write = store.write
            attempts = 0

            def fail_after_replace(*args, **kwargs):
                nonlocal attempts
                attempts += 1
                original_write(*args, **kwargs)
                if attempts == 1:
                    raise OSError("injected post-replace failure")

            with mock.patch.object(store, "write", side_effect=fail_after_replace):
                result = manager.apply(plan, accepted_plan_digest=plan.plan_digest)

            self.assertEqual(result.outcome, ConfigurationOutcome.CONFIG_APPLY_FAILED)
            self.assertEqual(store.read(), previous)
            self.assertEqual(host.calls.count("reconcile-api-web"), 1)
            self.assertEqual(
                [state for state, _code, _manual in journal.states],
                ["PLANNED", "APPLYING", "ROLLING_BACK", "ROLLED_BACK"],
            )

    def test_doctor_failure_after_locator_publication_is_fully_rolled_back(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager, store, host, _journal, previous = self.make_runtime(
                Path(directory)
            )
            old_locator_payload = instance_locator_payload(
                host.current_snapshot.locator
            )
            plan = manager.set_origin("https://new.example")
            host.fail_doctor_once = True

            result = manager.apply(plan, accepted_plan_digest=plan.plan_digest)

            self.assertEqual(result.outcome, ConfigurationOutcome.CONFIG_APPLY_FAILED)
            self.assertEqual(store.read(), previous)
            self.assertEqual(
                instance_locator_payload(host.current_snapshot.locator),
                old_locator_payload,
            )
            replace_calls = [
                call
                for call in host.calls
                if isinstance(call, tuple) and call[0] == "replace-locator"
            ]
            self.assertEqual(len(replace_calls), 2)

    def test_rollback_failure_returns_recovery_required(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager, store, host, journal, _previous = self.make_runtime(
                Path(directory)
            )
            plan = manager.set_origin("https://new.example")
            host.fail_health_once = True
            host.fail_reconcile_after = 2

            result = manager.apply(plan, accepted_plan_digest=plan.plan_digest)

            self.assertEqual(result.outcome, ConfigurationOutcome.RECOVERY_REQUIRED)
            self.assertTrue(result.manual_recovery_required)
            self.assertIn(
                ("RECOVERY_REQUIRED", "CONFIG_APPLY_FAILED", True), journal.states
            )
            self.assertEqual(store.read().config_revision, plan.current_revision)

    def test_no_change_does_not_rotate_revision_or_create_operation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager, store, _host, journal, current = self.make_runtime(Path(directory))
            before = store.authority_path.read_bytes()
            plan = manager.set_origin(current.public_origin)

            result = manager.apply(plan, accepted_plan_digest=plan.plan_digest)

            self.assertEqual(result.outcome, ConfigurationOutcome.NO_CHANGE)
            self.assertEqual(result.config_revision, current.config_revision)
            self.assertEqual(store.authority_path.read_bytes(), before)
            self.assertEqual(journal.states, [])


class LocalConfigurationOperationJournalTests(unittest.TestCase):
    def test_operation_evidence_is_private_and_contains_no_secret(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            root.chmod(0o700)
            manager_root = root / "manager"
            manager_root.mkdir(mode=0o700)
            manager, _store, _host, _journal, _config = (
                ConfigurationManagerTests().make_runtime(manager_root)
            )
            plan = manager.set_origin("https://new.example")
            journal = LocalConfigurationOperationJournal(root)

            operation_id = journal.create(plan)
            journal.transition(operation_id, "APPLYING")
            journal.transition(operation_id, "VERIFYING")
            journal.transition(operation_id, "SUCCEEDED")

            path = root / "config-operations" / f"{operation_id}.json"
            rendered = path.read_text(encoding="utf-8")
            self.assertNotIn(DJANGO_SECRET, rendered)
            self.assertNotIn(DATABASE_SECRET, rendered)
            self.assertNotIn(CREDENTIAL_KEY, rendered)
            self.assertEqual(json.loads(rendered)["state"], "SUCCEEDED")


if __name__ == "__main__":
    unittest.main()
