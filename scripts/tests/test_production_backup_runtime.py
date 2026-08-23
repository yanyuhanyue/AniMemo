from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from durability import backup
from durability.backup_production import (
    _REFERENCE_COVERAGE_DIGEST,
    MediaInventory,
    ProductionBackupError,
    ProductionBackupRuntime,
    ProductionBinding,
    ProtectionRequest,
    verify_protected_backup,
)
from durability.canonical import canonical_json_bytes
from durability.managed_config import (
    ApplicationConfig,
    DatabaseConfig,
    DirectAccessConfig,
    IntegrationConfig,
    ListenConfig,
    ManagedConfig,
    RedisConfig,
    TrustedOriginsConfig,
)
from durability.secret_envelope import OneTimeKey, SecretEnvelopeCorruptError
from release.contract import PRODUCTION_BACKUP_CONTRACT
from updater.deployment import HostPaths
from updater.state import OperationStore, UpdateLock


class FakePgDump:
    def __init__(self, *, fail: bool = False, empty: bool = False) -> None:
        self.fail = fail
        self.empty = empty
        self.calls = 0

    def run(self, database_url, raw_output, *, executable, timeout):
        del database_url, executable, timeout
        self.calls += 1
        if self.fail:
            raise backup.BackupError("PG_DUMP_FAILED", "logical dump failed")
        raw_output.write_bytes(
            b"" if self.empty else b"-- PostgreSQL database dump\nSELECT 1;\n"
        )
        return "pg_dump (PostgreSQL) 16.4"


class FakeHost:
    def __init__(self) -> None:
        self.events: list[str] = []
        self.states = {"api": "running"}
        self.fail_restore = False
        self.fail_health = False
        self.pg = FakePgDump()
        self.inventory_counter = 0
        self.inventory = MediaInventory({}, (), "2026-01-01T00:00:00Z")
        self.database_size = 4096

    def validate(self):
        self.events.append("validate")

    def media_inventory(self):
        self.inventory_counter += 1
        return MediaInventory(
            self.inventory.local_references,
            self.inventory.r2_references,
            f"2026-01-{self.inventory_counter:02d}T00:00:00Z",
        )

    def database_size_bytes(self):
        return self.database_size

    def writer_states(self, writers):
        self.events.append("prestate")
        return {writer: self.states[writer] for writer in writers}

    def stop_writers(self, states):
        self.events.append("stop")
        for writer, state in states.items():
            if state == "running":
                self.states[writer] = "stopped"

    def verify_write_barrier(self, writers):
        self.events.append("barrier")
        if any(self.states[writer] == "running" for writer in writers):
            raise ProductionBackupError("BACKUP_WRITE_BARRIER_FAILED", "RECOVERY")

    def restore_writers(self, states):
        self.events.append("restore")
        if self.fail_restore:
            raise ProductionBackupError("BACKUP_WRITER_RESTORE_FAILED", "RECOVERY")
        self.states = dict(states)
        return dict(self.states)

    def verify_health(self, states):
        del states
        self.events.append("health")
        if self.fail_health:
            raise ProductionBackupError("BACKUP_POST_RESUME_HEALTH_FAILED", "RECOVERY")

    def pg_dump_runner(self):
        return self.pg


class FakeAuthority:
    def __init__(self, binding) -> None:
        self.binding = binding
        self.binds = 0

    def bind(self, instance_name):
        self.binds += 1
        if str(instance_name) != str(self.binding.instance_name):
            raise ProductionBackupError("BACKUP_INSTANCE_MISMATCH", "VALIDATION")
        return self.binding


class ProductionBackupRuntimeTests(unittest.TestCase):
    maxDiff = None

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.app = self.root / "app"
        self.data = self.root / "data"
        self.state = self.root / "state"
        for path in (
            self.app,
            self.data / "plugins" / "cas",
            self.data / "plugins" / "durable",
            self.data / "media",
            self.data / "private",
            self.data / "backups",
            self.state / "releases",
            self.state / "runtime",
        ):
            path.mkdir(parents=True, exist_ok=True)
            if os.name == "posix":
                os.chmod(path, 0o700)
        self.keys = self.root / "keys"
        self.keys.mkdir()
        if os.name == "posix":
            os.chmod(self.keys, 0o700)
        for relative, payload in (
            ("ownership.json", {"identity": "ownership"}),
            ("releases/release-slots.json", {"identity": "slots"}),
            ("runtime.json", {"identity": "runtime"}),
        ):
            target = self.state / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(canonical_json_bytes(payload) + b"\n")
            if os.name == "posix":
                os.chmod(target, 0o600)
        self.config = ManagedConfig(
            instance_id="00000000-0000-4000-8000-000000000000",
            config_revision="11111111-1111-4111-8111-111111111111",
            listen=ListenConfig("127.0.0.1", 8088),
            public_origin="https://backup.example.test",
            direct_access=DirectAccessConfig(False, False, False),
            trusted_origins=TrustedOriginsConfig((), (), ()),
            database=DatabaseConfig("animemo", "animemo", "database-password"),
            redis=RedisConfig("redis://redis:6379/0"),
            application=ApplicationConfig(
                "django-secret-value",
                "credential-encryption-value",
                None,
                (),
            ),
            integrations=IntegrationConfig(
                "client-id", "oauth-secret", "resend-secret"
            ),
        )
        self.host = FakeHost()
        paths = HostPaths.testing(
            app=self.app,
            data=self.data,
            state=self.state,
            instance_name="blue",
        )
        release_identity = {
            "manifestDigest": "sha256:" + "9" * 64,
        }
        snapshot = SimpleNamespace(
            digest="sha256:" + "8" * 64,
            locator=SimpleNamespace(
                instance_id=self.config.instance_id,
                release_identity=release_identity,
            ),
        )
        manifest = {
            "release": {
                "version": "v1.1.0-rc.7",
                "channel": "rc",
                "commit": "a" * 40,
                "createdAt": "2026-01-01T00:00:00Z",
                "promotedFrom": None,
            },
            "deployment": {
                "profile": "v1.1-instance-scoped",
                "contractSha256": "sha256:" + "7" * 64,
                "backup": PRODUCTION_BACKUP_CONTRACT,
            },
            "compatibility": {
                "database": {"contract": "animemo.database/v1"},
                "configuration": {"contract": "animemo.configuration/v1"},
                "pluginSdk": {"supportedApis": [2]},
            },
        }
        self.binding = ProductionBinding(
            instance_name=paths.instance_name,
            snapshot=snapshot,
            config=self.config,
            manifest=manifest,
            paths=paths,
            host=self.host,
            backup_contract=PRODUCTION_BACKUP_CONTRACT,
        )
        self.authority = FakeAuthority(self.binding)
        self.runtime = ProductionBackupRuntime(self.authority)

    def key_protection(self, name="backup.key"):
        return ProtectionRequest("one-time-key", path=self.keys / name)

    def passphrase_protection(self):
        path = self.keys / "passphrase"
        path.write_bytes(b"correct horse battery staple\n")
        if os.name == "posix":
            os.chmod(path, 0o600)
        return ProtectionRequest("passphrase-file", path=path)

    def reference_protection(self):
        path = self.keys / "reference.json"
        public_reference = canonical_json_bytes(
            {
                "provider": "operator-secret-store",
                "version": "v1",
                "coverageDigest": _REFERENCE_COVERAGE_DIGEST,
            }
        ) + b"\n"
        path.write_bytes(public_reference)
        if os.name == "posix":
            os.chmod(path, 0o600)
        return ProtectionRequest("secret-reference", path=path)

    def create(self, protection=None):
        protection = protection or self.key_protection()
        plan = self.runtime.plan(
            instance_name="blue", destination=None, protection=protection
        )
        receipt = self.runtime.execute(
            plan,
            protection=protection,
            accepted_plan_digest=plan.plan_digest,
        )
        return plan, receipt, protection

    def test_plan_is_closed_and_digest_is_self_verifying(self):
        plan = self.runtime.plan(
            instance_name="blue", destination=None, protection=self.key_protection()
        )
        self.assertEqual(
            set(plan.as_dict()),
            {
                "instanceName",
                "instanceId",
                "sourceRelease",
                "sourceLocatorDigest",
                "destination",
                "memberClasses",
                "secretMode",
                "quiescenceMethod",
                "writerServices",
                "estimatedBytes",
                "databaseProfile",
                "planDigest",
            },
        )
        self.assertEqual(plan.instance_name, "blue")
        self.assertEqual(plan.writer_services, ("api",))
        self.assertEqual(plan.database_profile["serverMajor"], 16)
        self.assertGreaterEqual(plan.estimated_bytes, self.host.database_size)

    def test_inventory_timestamp_does_not_make_same_authority_plan_stale(self):
        protection = self.key_protection()
        first = self.runtime.plan(
            instance_name="blue", destination=None, protection=protection
        )
        second = self.runtime.plan(
            instance_name="blue", destination=None, protection=protection
        )
        self.assertEqual(first.plan_digest, second.plan_digest)
        self.assertEqual(first._media_digest, second._media_digest)

    def test_execute_rebinds_authority_before_and_after_lock(self):
        self.create()
        self.assertGreaterEqual(self.authority.binds, 3)

    def test_wrong_accepted_digest_fails_before_mutation(self):
        protection = self.key_protection()
        plan = self.runtime.plan(
            instance_name="blue", destination=None, protection=protection
        )
        with self.assertRaisesRegex(ProductionBackupError, "BACKUP_PLAN_NOT_ACCEPTED"):
            self.runtime.execute(
                plan, protection=protection, accepted_plan_digest="sha256:" + "0" * 64
            )
        self.assertNotIn("stop", self.host.events)

    def test_instance_mismatch_fails_closed(self):
        with self.assertRaisesRegex(ProductionBackupError, "BACKUP_INSTANCE_MISMATCH"):
            self.runtime.plan(
                instance_name="green",
                destination=None,
                protection=self.key_protection(),
            )

    def test_shared_operation_lock_conflict_fails_before_writer_stop(self):
        protection = self.key_protection()
        plan = self.runtime.plan(
            instance_name="blue", destination=None, protection=protection
        )
        with (
            UpdateLock(self.state / "update.lock"),
            self.assertRaisesRegex(ProductionBackupError, "BACKUP_OPERATION_ACTIVE"),
        ):
            self.runtime.execute(
                plan,
                protection=protection,
                accepted_plan_digest=plan.plan_digest,
            )
        self.assertNotIn("stop", self.host.events)

    def test_pending_updater_operation_blocks_backup(self):
        OperationStore(self.state).create("apply_update", {})
        protection = self.key_protection()
        plan = self.runtime.plan(
            instance_name="blue", destination=None, protection=protection
        )
        with self.assertRaisesRegex(ProductionBackupError, "BACKUP_OPERATION_PENDING"):
            self.runtime.execute(
                plan, protection=protection, accepted_plan_digest=plan.plan_digest
            )

    def test_writer_stop_verify_backup_restore_health_order(self):
        self.create()
        relevant = [
            event
            for event in self.host.events
            if event in {"prestate", "stop", "barrier", "restore", "health"}
        ]
        self.assertEqual(relevant, ["prestate", "stop", "barrier", "restore", "health"])

    def test_original_stopped_writer_prestate_is_restored(self):
        self.host.states["api"] = "stopped"
        self.create()
        self.assertEqual(self.host.states, {"api": "stopped"})

    def test_backup_failure_restores_writer_and_reports_no_runtime_damage(self):
        self.host.pg.fail = True
        protection = self.key_protection()
        plan = self.runtime.plan(
            instance_name="blue", destination=None, protection=protection
        )
        with self.assertRaisesRegex(
            ProductionBackupError, "BACKUP_FAILED_NO_RUNTIME_DAMAGE"
        ):
            self.runtime.execute(
                plan, protection=protection, accepted_plan_digest=plan.plan_digest
            )
        self.assertEqual(self.host.states, {"api": "running"})
        self.assertFalse(protection.path.exists())

    def test_writer_restore_failure_is_recovery_required(self):
        self.host.pg.fail = True
        self.host.fail_restore = True
        protection = self.key_protection()
        plan = self.runtime.plan(
            instance_name="blue", destination=None, protection=protection
        )
        with self.assertRaisesRegex(ProductionBackupError, "RECOVERY_REQUIRED"):
            self.runtime.execute(
                plan, protection=protection, accepted_plan_digest=plan.plan_digest
            )

    def test_one_time_key_is_new_private_and_separate(self):
        _plan, receipt, protection = self.create()
        self.assertEqual(protection.path.stat().st_size, 32)
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(protection.path.stat().st_mode), 0o600)
        self.assertFalse((Path(receipt.path) / protection.path.name).exists())

    def test_one_time_key_file_preserves_newline_bytes_exactly(self):
        material = b"\n" + b"x" * 31
        with patch(
            "durability.backup_production.OneTimeKey.generate",
            return_value=OneTimeKey.from_bytes(material),
        ):
            _plan, _receipt, protection = self.create()
        self.assertEqual(protection.path.read_bytes(), material)

    def test_existing_one_time_key_is_never_overwritten(self):
        path = self.keys / "existing.key"
        path.write_bytes(b"existing")
        with self.assertRaisesRegex(ProductionBackupError, "BACKUP_KEY_OUTPUT_EXISTS"):
            self.runtime.plan(
                instance_name="blue",
                destination=None,
                protection=ProtectionRequest("one-time-key", path=path),
            )
        self.assertEqual(path.read_bytes(), b"existing")

    def test_failed_backup_never_deletes_replacement_at_one_time_key_path(self):
        protection = self.key_protection()
        plan = self.runtime.plan(
            instance_name="blue", destination=None, protection=protection
        )

        def replace_key_then_fail(*_args, **_kwargs):
            assert protection.path is not None
            protection.path.unlink()
            protection.path.write_bytes(b"operator-replacement")
            if os.name == "posix":
                os.chmod(protection.path, 0o600)
            raise backup.BackupError("PG_DUMP_FAILED", "forced failure")

        with (
            patch(
                "durability.backup_production.backup.create_backup",
                side_effect=replace_key_then_fail,
            ),
            self.assertRaisesRegex(
                ProductionBackupError, "BACKUP_FAILED_NO_RUNTIME_DAMAGE"
            ),
        ):
            self.runtime.execute(
                plan,
                protection=protection,
                accepted_plan_digest=plan.plan_digest,
            )
        assert protection.path is not None
        self.assertEqual(protection.path.read_bytes(), b"operator-replacement")

    def test_created_backup_is_automatically_verified_before_resume(self):
        plan, receipt, _protection = self.create()
        verification = backup.verify_backup(Path(receipt.path))
        self.assertEqual(verification.backup_id, receipt.backup_id)
        self.assertTrue(receipt.verification_completed_before_resume)
        manifest = json.loads((Path(receipt.path) / backup.MANIFEST_NAME).read_bytes())
        self.assertTrue(manifest["quiescence"]["verificationCompletedBeforeResume"])
        self.assertEqual(manifest["quiescence"]["writerServices"], ["api"])
        self.assertEqual(manifest["schemaVersion"], 1)
        self.assertEqual(plan.protection_mode, "envelope")

    def test_create_authenticates_secret_envelope_before_writer_resume(self):
        with (
            patch(
                "durability.backup_production.open_secret_envelope",
                side_effect=SecretEnvelopeCorruptError(
                    "SECRET_ENVELOPE_AUTHENTICATION_FAILED"
                ),
            ),
            self.assertRaisesRegex(
                ProductionBackupError, "BACKUP_FAILED_NO_RUNTIME_DAMAGE"
            ),
        ):
            self.create()
        self.assertLess(
            self.host.events.index("stop"), self.host.events.index("restore")
        )
        self.assertEqual(self.host.states, {"api": "running"})
        self.assertEqual(backup.list_finalized_backups(self.data / "backups"), ())

    def test_manifest_binds_source_instance_name_and_exact_release(self):
        _plan, receipt, _protection = self.create()
        inspected = backup.inspect_backup(Path(receipt.path))
        self.assertEqual(inspected["sourceInstance"]["name"], "blue")
        self.assertEqual(inspected["releaseIdentity"]["version"], "v1.1.0-rc.7")
        self.assertFalse(inspected["verified"])

    def test_full_verify_authenticates_one_time_key_and_rejects_wrong_key(self):
        _plan, receipt, protection = self.create()
        verified = verify_protected_backup(Path(receipt.path), protection=protection)
        self.assertTrue(verified["verified"])
        wrong = self.keys / "wrong.key"
        wrong.write_bytes(b"x" * 32)
        if os.name == "posix":
            os.chmod(wrong, 0o600)
        with self.assertRaises(ProductionBackupError):
            verify_protected_backup(
                Path(receipt.path),
                protection=ProtectionRequest("one-time-key", path=wrong),
            )

    def test_passphrase_create_and_full_verify(self):
        protection = self.passphrase_protection()
        _plan, receipt, _ = self.create(protection)
        self.assertTrue(
            verify_protected_backup(Path(receipt.path), protection=protection)[
                "verified"
            ]
        )

    def test_reference_create_and_full_verify(self):
        protection = self.reference_protection()
        reference = json.loads(protection.path.read_bytes())
        self.assertEqual(
            set(reference), {"provider", "version", "coverageDigest"}
        )
        self.assertRegex(reference["coverageDigest"], r"^sha256:[0-9a-f]{64}$")
        self.assertNotIn(b"POSTGRES_PASSWORD", protection.path.read_bytes())
        plan, receipt, _ = self.create(protection)
        self.assertEqual(plan.protection_mode, "reference")
        self.assertTrue(
            verify_protected_backup(Path(receipt.path), protection=protection)[
                "verified"
            ]
        )

    def test_reference_without_complete_registered_coverage_fails(self):
        path = self.keys / "bad-reference.json"
        path.write_bytes(
            canonical_json_bytes({"provider": "store", "version": "v1", "coverage": []})
            + b"\n"
        )
        if os.name == "posix":
            os.chmod(path, 0o600)
        with self.assertRaisesRegex(
            ProductionBackupError, "BACKUP_SECRET_REFERENCE_INVALID"
        ):
            self.runtime.plan(
                instance_name="blue",
                destination=None,
                protection=ProtectionRequest("secret-reference", path=path),
            )

    def test_private_members_require_closed_registry(self):
        (self.data / "private" / "memo.bin").write_bytes(b"private-state")
        with self.assertRaisesRegex(
            ProductionBackupError, "BACKUP_PRIVATE_REGISTRY_MISSING"
        ):
            self.runtime.plan(
                instance_name="blue", destination=None, protection=self.key_protection()
            )

    def test_registered_private_member_round_trips(self):
        private = self.data / "private"
        (private / "memo.bin").write_bytes(b"private-state")
        (private / "backup-members.json").write_bytes(
            canonical_json_bytes({"schemaVersion": 1, "members": ["memo.bin"]}) + b"\n"
        )
        if os.name == "posix":
            os.chmod(private / "backup-members.json", 0o600)
        _plan, receipt, _ = self.create()
        self.assertEqual(
            (Path(receipt.path) / "filesystem" / "private" / "memo.bin").read_bytes(),
            b"private-state",
        )

    def test_unknown_private_member_fails_even_with_registry(self):
        private = self.data / "private"
        (private / "memo.bin").write_bytes(b"private-state")
        (private / "unknown.bin").write_bytes(b"unknown")
        (private / "backup-members.json").write_bytes(
            canonical_json_bytes({"schemaVersion": 1, "members": ["memo.bin"]}) + b"\n"
        )
        if os.name == "posix":
            os.chmod(private / "backup-members.json", 0o600)
        with self.assertRaisesRegex(
            ProductionBackupError, "BACKUP_PRIVATE_MEMBER_UNKNOWN"
        ):
            self.runtime.plan(
                instance_name="blue", destination=None, protection=self.key_protection()
            )

    def test_relative_external_destination_fails(self):
        with self.assertRaisesRegex(
            ProductionBackupError, "BACKUP_DESTINATION_NOT_ABSOLUTE"
        ):
            self.runtime.plan(
                instance_name="blue",
                destination=Path("relative"),
                protection=self.key_protection(),
            )

    def test_destination_inside_source_tree_fails(self):
        with self.assertRaisesRegex(
            ProductionBackupError, "BACKUP_DESTINATION_OVERLAP"
        ):
            self.runtime.plan(
                instance_name="blue",
                destination=self.data / "media" / "backups",
                protection=self.key_protection(),
            )

    def test_destination_with_symlink_ancestor_fails_closed(self):
        external = self.root / "external"
        nested = external / "nested"
        nested.mkdir(parents=True)
        link = self.root / "destination-link"
        try:
            link.symlink_to(external, target_is_directory=True)
        except OSError:
            self.skipTest("symlinks unavailable")
        if os.name == "posix":
            os.chmod(external, 0o700)
            os.chmod(nested, 0o700)
        with self.assertRaisesRegex(
            ProductionBackupError, "BACKUP_DESTINATION_PARENT_UNSAFE"
        ):
            self.runtime.plan(
                instance_name="blue",
                destination=link / "nested" / "backups",
                protection=self.key_protection(),
            )

    @unittest.skipUnless(hasattr(os, "link"), "hard links unavailable")
    def test_source_hardlink_fails_closed(self):
        original = self.data / "media" / "original.bin"
        alias = self.data / "media" / "alias.bin"
        original.write_bytes(b"media")
        try:
            os.link(original, alias)
        except OSError as error:
            self.skipTest(f"hard links unavailable: {error}")

        with self.assertRaisesRegex(ProductionBackupError, "BACKUP_SOURCE_UNSAFE"):
            self.runtime.plan(
                instance_name="blue",
                destination=None,
                protection=self.key_protection(),
            )

    def test_r2_authoritative_coverage_passes_and_unproven_coverage_fails(self):
        self.host.inventory = MediaInventory(
            {},
            (
                backup.R2Reference(
                    backend_type="r2",
                    endpoint_identity="sha256:" + "4" * 64,
                    bucket="animemo-media",
                    object_keys=("media/object.bin",),
                    inventory_timestamp="2026-01-01T00:00:00Z",
                    coverage_classification="AUTHORITATIVE_DATABASE_COMPLETE",
                ),
            ),
            "2026-01-01T00:00:00Z",
        )
        _plan, receipt, _protection = self.create()
        self.assertEqual(receipt.outcome, "BACKUP_SUCCEEDED")

        self.host.inventory = MediaInventory(
            {},
            (
                backup.R2Reference(
                    backend_type="r2",
                    endpoint_identity="sha256:" + "5" * 64,
                    bucket="animemo-media",
                    object_keys=("media/object.bin",),
                    inventory_timestamp="2026-01-01T00:00:00Z",
                    coverage_classification="UNPROVEN",
                ),
            ),
            "2026-01-01T00:00:00Z",
        )
        protection = self.key_protection("unproven.key")
        plan = self.runtime.plan(
            instance_name="blue", destination=None, protection=protection
        )
        with self.assertRaisesRegex(
            ProductionBackupError, "BACKUP_FAILED_NO_RUNTIME_DAMAGE"
        ):
            self.runtime.execute(
                plan,
                protection=protection,
                accepted_plan_digest=plan.plan_digest,
            )

    def test_updater_projection_excludes_active_lock_and_runtime(self):
        (self.state / "unrelated.lock").write_text("lock", encoding="utf-8")
        _plan, receipt, _ = self.create()
        members = {
            path.relative_to(Path(receipt.path)).as_posix()
            for path in Path(receipt.path).rglob("*")
            if path.is_file()
        }
        self.assertIn("updater-state/runtime.json", members)
        self.assertNotIn("updater-state/unrelated.lock", members)

    def test_managed_plaintext_secrets_are_absent_from_artifact(self):
        _plan, receipt, _ = self.create()
        combined = b"\n".join(
            path.read_bytes()
            for path in Path(receipt.path).rglob("*")
            if path.is_file()
        )
        for secret in (
            b"database-password",
            b"django-secret-value",
            b"credential-encryption-value",
            b"oauth-secret",
            b"resend-secret",
        ):
            self.assertNotIn(secret, combined)

    def test_other_instance_roots_are_not_mutated(self):
        other = self.root / "other-instance"
        other.mkdir()
        sentinel = other / "sentinel"
        sentinel.write_bytes(b"unchanged")
        self.create()
        self.assertEqual(sentinel.read_bytes(), b"unchanged")


if __name__ == "__main__":
    unittest.main()
