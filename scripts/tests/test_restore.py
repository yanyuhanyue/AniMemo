from __future__ import annotations

import contextlib
import gzip
import json
import os
import tempfile
import unittest
import uuid
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from durability import backup, restore, secret_envelope
from durability.canonical import canonical_json_bytes
from durability.compatibility import (
    CompatibilityOutcome,
    Dimension,
    DimensionAssessment,
    ReasonCode,
    UpgradeAction,
)
from durability.resource_budget import DurabilityResourceBudget


class FakePgDump:
    def run(self, database_url, raw_output, *, executable, timeout):
        raw_output.write_bytes(
            b"-- PostgreSQL database dump\nCREATE TABLE memory(id integer);\n"
        )
        return "pg_dump (PostgreSQL) 16.4"


class FakeDestination:
    def __init__(self, snapshot: restore.DestinationSnapshot) -> None:
        self.snapshot = snapshot
        self.calls = 0

    def inspect(self) -> restore.DestinationSnapshot:
        self.calls += 1
        return self.snapshot


class FakeRelease:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.verify_calls = 0

    def verify(self, manifest):
        self.verify_calls += 1
        return restore.ReleaseEvidence(
            release_identity_digest="sha256:" + "a" * 64,
            deployment_identity_digest="sha256:" + "b" * 64,
        )

    def acquire(self, evidence):
        self.events.append("release.acquire")
        return object()


class FakeUpdater:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.verify_calls = 0

    def verify(self, manifest, release_evidence):
        self.verify_calls += 1
        return restore.UpdaterEvidence(
            state_identity_digest="sha256:" + "c" * 64,
            pending_state_preserved=True,
        )

    def stage(self, manifest, evidence, mutation):
        self.events.append("updater.stage")


class FakeSecretResolver:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.calls = 0

    def authenticate(self, backup_root, manifest):
        self.calls += 1
        self.events.append("secret.authenticate")
        return restore.SecretResolution(
            mode=manifest["secrets"]["mode"],
            status="RESOLVABLE",
            handle=object(),
        )


def assessment(
    dimension: Dimension,
    outcome: CompatibilityOutcome = CompatibilityOutcome.COMPATIBLE,
) -> DimensionAssessment:
    reasons = {
        (
            Dimension.DEPLOYMENT_CONTRACT,
            CompatibilityOutcome.COMPATIBLE,
        ): ReasonCode.DEPLOYMENT_CONTRACT_SUPPORTED,
        (
            Dimension.DEPLOYMENT_CONTRACT,
            CompatibilityOutcome.CORRUPT,
        ): ReasonCode.DEPLOYMENT_CONTRACT_INVALID,
        (
            Dimension.SCHEMA_CONTRACTS,
            CompatibilityOutcome.COMPATIBLE,
        ): ReasonCode.SCHEMA_CONTRACTS_SUPPORTED,
        (
            Dimension.SCHEMA_CONTRACTS,
            CompatibilityOutcome.REQUIRES_UPGRADE,
        ): ReasonCode.SCHEMA_MIGRATION_REQUIRED,
        (
            Dimension.EXACT_RELEASE_IDENTITY,
            CompatibilityOutcome.COMPATIBLE,
        ): ReasonCode.RELEASE_IDENTITY_VERIFIED,
        (
            Dimension.PLATFORM_RUNTIME,
            CompatibilityOutcome.COMPATIBLE,
        ): ReasonCode.PLATFORM_RUNTIME_SUPPORTED,
        (
            Dimension.PLATFORM_RUNTIME,
            CompatibilityOutcome.UNSUPPORTED,
        ): ReasonCode.PLATFORM_RUNTIME_UNSUPPORTED,
        (
            Dimension.SUPPORTED_PATH,
            CompatibilityOutcome.COMPATIBLE,
        ): ReasonCode.DIRECT_PATH_SUPPORTED,
        (
            Dimension.SUPPORTED_PATH,
            CompatibilityOutcome.REQUIRES_UPGRADE,
        ): ReasonCode.ORDERED_PATH_REQUIRED,
    }
    return DimensionAssessment(
        name=dimension,
        outcome=outcome,
        reason_code=reasons[(dimension, outcome)],
        source={"identity": f"source-{dimension.value}"},
        target={"capability": f"target-{dimension.value}"},
    )


class FakeCompatibility:
    def __init__(self, outcome=CompatibilityOutcome.COMPATIBLE) -> None:
        dimensions = [
            assessment(Dimension.DEPLOYMENT_CONTRACT),
            assessment(Dimension.SCHEMA_CONTRACTS),
            assessment(Dimension.EXACT_RELEASE_IDENTITY),
            assessment(Dimension.PLATFORM_RUNTIME),
            assessment(Dimension.SUPPORTED_PATH),
        ]
        actions = ()
        if outcome is CompatibilityOutcome.REQUIRES_UPGRADE:
            dimensions[1] = assessment(
                Dimension.SCHEMA_CONTRACTS,
                CompatibilityOutcome.REQUIRES_UPGRADE,
            )
            dimensions[-1] = assessment(
                Dimension.SUPPORTED_PATH,
                CompatibilityOutcome.REQUIRES_UPGRADE,
            )
            actions = (
                UpgradeAction(
                    order=1,
                    kind="APPLY_FORWARD_MIGRATION",
                    input_identity={"databaseContract": "animemo.database/v1"},
                    output_identity={"databaseContract": "animemo.database/v2"},
                    required_release_identity={"manifestDigest": "sha256:" + "d" * 64},
                ),
            )
        elif outcome is CompatibilityOutcome.UNSUPPORTED:
            dimensions[3] = assessment(
                Dimension.PLATFORM_RUNTIME,
                CompatibilityOutcome.UNSUPPORTED,
            )
        elif outcome is CompatibilityOutcome.CORRUPT:
            dimensions[0] = assessment(
                Dimension.DEPLOYMENT_CONTRACT,
                CompatibilityOutcome.CORRUPT,
            )
        self.evidence = restore.RestoreCompatibilityEvidence(tuple(dimensions), actions)

    def assess(self, manifest, destination, release_evidence, updater_evidence):
        return self.evidence


class FakeDatabase:
    def __init__(self, events: list[str], fail: bool = False) -> None:
        self.events = events
        self.fail = fail

    def restore(self, dump_path):
        self.events.append("database.restore")
        if self.fail:
            raise restore.RestoreAdapterError("DATABASE_IMPORT_FAILED")


class FakeMutation:
    def __init__(
        self,
        events: list[str],
        fail_at: str | None = None,
        recovery_record_fail: bool = False,
    ) -> None:
        self.events = events
        self.fail_at = fail_at
        self.recovery_record_fail = recovery_record_fail
        self.published = False
        self.recovery: restore.RecoveryEvidence | None = None
        self.locator_instance_id: str | None = None

    @contextlib.contextmanager
    def acquire_lock(self, operation_id):
        self.events.append("lock.acquire")
        if self.fail_at == "lock.acquire":
            raise restore.RestoreAdapterError("LOCK_ACQUISITION_FAILED")
        yield
        self.events.append("lock.release")

    def _call(self, name):
        self.events.append(name)
        if self.fail_at == name:
            raise restore.RestoreAdapterError(
                f"{name.upper().replace('.', '_')}_FAILED"
            )

    def begin(self, plan):
        self._call("target.begin")

    def stage_release(self, release_material, evidence):
        self._call("release.stage")

    def stage_secret(self, resolution):
        self._call("secret.stage")

    def prepare_database(self):
        self._call("database.prepare")

    def restore_filesystem(self, backup_root, member_paths):
        self._call("filesystem.restore")

    def apply_upgrade(self, actions):
        self._call("upgrade.apply")

    def bootstrap(self):
        self._call("bootstrap")

    def rebuild_runtime(self):
        self._call("runtime.rebuild")

    def build_locator(self, instance_id, release_evidence):
        self.locator_instance_id = instance_id
        self._call("locator.build")

    def rotate_authentication_epoch(self):
        self._call("authentication.rotate")

    def publish(self):
        self._call("target.publish")
        self.published = True

    def record_recovery_required(self, evidence):
        self.events.append("recovery.record")
        self.recovery = evidence
        if self.recovery_record_fail:
            raise RuntimeError("redacted recovery journal failure")


class FakeValidator:
    def __init__(self, events: list[str], missing: str | None = None) -> None:
        self.events = events
        self.missing = missing

    def validate(self, manifest, plan, mutation):
        self.events.append("validate")
        passed = tuple(
            check for check in restore.REQUIRED_VALIDATIONS if check != self.missing
        )
        return restore.ValidationReport(
            passed_checks=passed,
            evidence_digest="sha256:" + "e" * 64,
        )


class RestoreRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.backup_root, self.external_secret = self.make_backup()
        self.events: list[str] = []

    def make_backup(self):
        sources = {}
        for logical_root in restore.CANONICAL_BACKUP_ROOTS:
            source = self.root / "sources" / logical_root.replace("/", "-")
            source.mkdir(parents=True)
            if logical_root in {"filesystem/config", "updater-state"}:
                (source / "metadata.json").write_bytes(
                    canonical_json_bytes({"contract": logical_root}) + b"\n"
                )
            elif logical_root != "filesystem/private":
                (source / "payload.bin").write_bytes(logical_root.encode())
            sources[logical_root] = source
        (sources["filesystem/media"] / "poster.jpg").write_bytes(b"poster")
        external_secret = secret_envelope.OneTimeKey.from_bytes(b"k" * 32)

        def envelope_factory(binding):
            return secret_envelope.create_secret_envelope(
                external_secret=external_secret,
                artifact_type="backup",
                artifact_id=binding.artifact_id,
                artifact_binding_record=binding.artifact_binding_record,
                source_instance_id=binding.source_instance_id,
                secret_entries=(
                    secret_envelope.SecretEntry.preserve(
                        "CREDENTIAL_ENCRYPTION_KEY", b"fake-restore-cek"
                    ),
                ),
            ).to_bytes()

        request = backup.BackupRequest(
            destination_root=self.root / "backups",
            database_url="postgresql://isolated.invalid/source",
            source=backup.BackupSourceIdentity(
                instance_id="11111111-2222-4333-8444-555555555555",
                source_locator_digest="sha256:" + "1" * 64,
                release={"version": "1.1.0", "commit": "a" * 40},
                deployment_contract={
                    "schemaVersion": 1,
                    "digest": "sha256:" + "2" * 64,
                },
                database_contract={"id": "animemo.database/v1", "serverMajor": 16},
                configuration_contract={"id": "animemo.configuration/v1"},
                plugin_sdk_apis=("animemo.plugin/v2",),
            ),
            filesystem_sources=tuple(
                backup.FilesystemSource(logical_root=name, source=path)
                for name, path in sources.items()
            ),
            secret=backup.SecretSource(
                mode="envelope",
                metadata={"suiteId": secret_envelope.SUITE_ID},
                envelope_factory=envelope_factory,
            ),
            local_media_references={},
            producer={"name": "restore-test", "version": "1"},
            platform={"os": "linux", "architecture": "amd64"},
            quiescence={"method": "isolated-test"},
        )
        moments = iter(
            (
                datetime(2026, 8, 16, 1, 2, 3, tzinfo=timezone.utc),
                datetime(2026, 8, 16, 1, 2, 4, tzinfo=timezone.utc),
            )
        )
        result = backup.create_backup(
            request,
            pg_dump_runner=FakePgDump(),
            backup_id=uuid.UUID("12345678-1234-5678-9234-567812345678"),
            clock=lambda: next(moments),
        )
        return result.path, external_secret

    def snapshot(self, kind=restore.DestinationClass.FRESH):
        return restore.DestinationSnapshot(
            classification=kind,
            deployment_profile="v1.1-standard",
            canonical_roots=restore.CANONICAL_ROOTS,
            ownership_verified=True,
            empty_verified=kind is restore.DestinationClass.EXISTING_EMPTY,
            parent_ready=kind is restore.DestinationClass.FRESH,
            evidence_digest="sha256:" + "f" * 64,
        )

    def request(
        self,
        *,
        kind=restore.DestinationClass.FRESH,
        compatibility=CompatibilityOutcome.COMPATIBLE,
        wrong_key=False,
        database_fail=False,
        mutation_fail=None,
        recovery_record_fail=False,
        validation_missing=None,
    ):
        destination = FakeDestination(self.snapshot(kind))
        release = FakeRelease(self.events)
        updater = FakeUpdater(self.events)
        mutation = FakeMutation(
            self.events,
            fail_at=mutation_fail,
            recovery_record_fail=recovery_record_fail,
        )
        validator = FakeValidator(self.events, missing=validation_missing)
        key = (
            secret_envelope.OneTimeKey.from_bytes(b"w" * 32)
            if wrong_key
            else self.external_secret
        )
        request = restore.RestoreRequest(
            operation_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            backup_root=self.backup_root,
            destination=destination,
            release=release,
            updater=updater,
            secret_resolver=restore.EnvelopeSecretResolver(key),
            compatibility=FakeCompatibility(compatibility),
            database=FakeDatabase(self.events, fail=database_fail),
            mutation=mutation,
            validator=validator,
        )
        return request, destination, release, updater, mutation

    def execute(self, request):
        plan = restore.prepare_restore(request)
        result = restore.execute_restore(
            request,
            plan,
            accepted_plan_digest=plan.plan_digest,
            accept_upgrade=plan.decision.outcome
            is CompatibilityOutcome.REQUIRES_UPGRADE,
        )
        return plan, result

    def test_valid_fresh_restore_is_ordered_and_published(self):
        request, destination, release, updater, mutation = self.request()
        original = {
            path.relative_to(self.backup_root).as_posix(): path.read_bytes()
            for path in self.backup_root.rglob("*")
            if path.is_file()
        }
        plan, result = self.execute(request)
        self.assertEqual(plan.decision.outcome, CompatibilityOutcome.COMPATIBLE)
        self.assertEqual(result.state, restore.RestoreTerminalState.PUBLISHED)
        self.assertTrue(mutation.published)
        self.assertEqual(
            self.events,
            [
                "release.acquire",
                "lock.acquire",
                "target.begin",
                "release.stage",
                "secret.stage",
                "database.prepare",
                "database.restore",
                "filesystem.restore",
                "updater.stage",
                "bootstrap",
                "runtime.rebuild",
                "locator.build",
                "authentication.rotate",
                "validate",
                "target.publish",
                "lock.release",
            ],
        )
        self.assertGreaterEqual(destination.calls, 3)
        self.assertGreaterEqual(release.verify_calls, 2)
        self.assertGreaterEqual(updater.verify_calls, 2)
        self.assertEqual(
            mutation.locator_instance_id,
            "11111111-2222-4333-8444-555555555555",
        )
        after = {
            path.relative_to(self.backup_root).as_posix(): path.read_bytes()
            for path in self.backup_root.rglob("*")
            if path.is_file()
        }
        self.assertEqual(original, after)

    def test_execution_uses_verified_private_snapshot_after_source_swap(self):
        request, _, _, _, _ = self.request()
        plan = restore.prepare_restore(request)
        original_database = self.backup_root / backup.DATABASE_MEMBER
        original_poster = (
            self.backup_root / "filesystem" / "media" / "poster.jpg"
        )
        delegate = restore.EnvelopeSecretResolver(self.external_secret)

        class SwappingResolver:
            def __init__(self):
                self.snapshot_root = None

            def authenticate(self, backup_root, manifest):
                resolution = delegate.authenticate(backup_root, manifest)
                self.snapshot_root = Path(backup_root)
                original_database.write_bytes(b"swapped-after-verification")
                original_poster.write_bytes(b"swapped-after-verification")
                return resolution

        class CapturingDatabase(FakeDatabase):
            def __init__(self, events):
                super().__init__(events)
                self.sql = None
                self.snapshot_root = None

            def restore(self, dump_path):
                self.events.append("database.restore")
                self.snapshot_root = Path(dump_path).parent
                with gzip.open(dump_path, "rb") as stream:
                    self.sql = stream.read()

        class CapturingMutation(FakeMutation):
            def __init__(self, events):
                super().__init__(events)
                self.snapshot_root = None
                self.poster = None

            def restore_filesystem(self, backup_root, member_paths):
                self._call("filesystem.restore")
                self.snapshot_root = Path(backup_root)
                self.poster = (
                    self.snapshot_root / "filesystem" / "media" / "poster.jpg"
                ).read_bytes()

        resolver = SwappingResolver()
        database = CapturingDatabase(self.events)
        mutation = CapturingMutation(self.events)
        execution_request = replace(
            request,
            secret_resolver=resolver,
            database=database,
            mutation=mutation,
        )
        result = restore.execute_restore(
            execution_request,
            plan,
            accepted_plan_digest=plan.plan_digest,
        )

        self.assertEqual(result.state, restore.RestoreTerminalState.PUBLISHED)
        self.assertNotEqual(resolver.snapshot_root, self.backup_root)
        self.assertEqual(database.snapshot_root, resolver.snapshot_root)
        self.assertEqual(mutation.snapshot_root, resolver.snapshot_root)
        self.assertIn(b"CREATE TABLE memory", database.sql)
        self.assertEqual(mutation.poster, b"poster")
        self.assertEqual(original_database.read_bytes(), b"swapped-after-verification")
        self.assertEqual(original_poster.read_bytes(), b"swapped-after-verification")
        self.assertFalse(resolver.snapshot_root.exists())

    def test_snapshot_preserves_swapped_member_link_for_reverification(self):
        request, _, _, _, mutation = self.request()
        plan = restore.prepare_restore(request)
        member = self.backup_root / "filesystem" / "media" / "poster.jpg"
        outside = self.root / "outside-poster.jpg"
        outside.write_bytes(member.read_bytes())
        member.unlink()
        try:
            member.symlink_to(outside)
        except OSError:
            self.skipTest("file symlinks are unavailable")

        with self.assertRaises(restore.RestorePreflightError) as raised:
            restore.execute_restore(
                request,
                plan,
                accepted_plan_digest=plan.plan_digest,
            )
        self.assertEqual(raised.exception.code, "RESTORE_BACKUP_CORRUPT")
        self.assertEqual(
            raised.exception.compatibility_outcome,
            CompatibilityOutcome.CORRUPT,
        )
        self.assertFalse(mutation.published)

    def test_existing_empty_target_is_supported(self):
        request, _, _, _, mutation = self.request(
            kind=restore.DestinationClass.EXISTING_EMPTY
        )
        _, result = self.execute(request)
        self.assertEqual(result.state, restore.RestoreTerminalState.PUBLISHED)
        self.assertTrue(mutation.published)

    def test_active_foreign_and_partial_targets_fail_before_mutation(self):
        for kind in (
            restore.DestinationClass.EXISTING_INSTANCE,
            restore.DestinationClass.FOREIGN,
            restore.DestinationClass.PARTIAL_AMBIGUOUS,
        ):
            with self.subTest(kind=kind):
                self.events.clear()
                request, _, release, updater, mutation = self.request(kind=kind)
                with self.assertRaisesRegex(
                    restore.RestorePreflightError, "RESTORE_DESTINATION_REJECTED"
                ):
                    restore.prepare_restore(request)
                self.assertEqual(self.events, [])
                self.assertEqual(release.verify_calls, 0)
                self.assertEqual(updater.verify_calls, 0)
                self.assertFalse(mutation.published)

    def test_checksum_corruption_fails_before_mutation(self):
        (self.backup_root / backup.DATABASE_MEMBER).write_bytes(b"corrupt")
        request, _, _, _, mutation = self.request()
        with self.assertRaises(restore.RestorePreflightError) as raised:
            restore.prepare_restore(request)
        self.assertEqual(
            raised.exception.compatibility_outcome, CompatibilityOutcome.CORRUPT
        )
        self.assertEqual(self.events, [])
        self.assertFalse(mutation.published)

    def test_resource_bomb_fails_before_destination_or_mutation(self):
        request, destination, release, updater, mutation = self.request()
        budget = DurabilityResourceBudget(
            maximum_compressed_member_bytes=1024 * 1024,
            maximum_uncompressed_database_bytes=16,
            maximum_filesystem_member_bytes=1024 * 1024,
            maximum_total_copied_bytes=16 * 1024 * 1024,
            maximum_compression_ratio=1_000,
        )
        with (
            mock.patch.object(backup, "_RESOURCE_BUDGET", budget),
            self.assertRaises(restore.RestorePreflightError) as raised,
        ):
            restore.prepare_restore(request)

        self.assertEqual(
            raised.exception.code,
            "RESTORE_RESOURCE_BOUNDS_EXCEEDED",
        )
        self.assertEqual(
            raised.exception.compatibility_outcome,
            CompatibilityOutcome.CORRUPT,
        )
        self.assertEqual(destination.calls, 0)
        self.assertEqual(release.verify_calls, 0)
        self.assertEqual(updater.verify_calls, 0)
        self.assertEqual(self.events, [])
        self.assertFalse(mutation.published)

    def test_resource_failure_after_mutation_records_recovery_required(self):
        request, _, _, _, mutation = self.request()
        plan = restore.prepare_restore(request)

        class BoundsDatabase:
            def restore(self, dump_path):
                raise restore.RestoreAdapterError(
                    "DATABASE_RESOURCE_BOUNDS_EXCEEDED"
                )

        result = restore.execute_restore(
            replace(request, database=BoundsDatabase()),
            plan,
            accepted_plan_digest=plan.plan_digest,
        )

        self.assertEqual(result.state, restore.RestoreTerminalState.RECOVERY_REQUIRED)
        self.assertIsNotNone(result.recovery_evidence)
        self.assertEqual(
            result.recovery_evidence.error_code,
            "DATABASE_RESOURCE_BOUNDS_EXCEEDED",
        )
        self.assertFalse(mutation.published)

    def test_unreadable_backup_is_operational_not_corrupt(self):
        request, _, _, _, mutation = self.request()
        with (
            mock.patch.object(backup, "verify_backup", side_effect=OSError),
            self.assertRaises(restore.RestorePreflightError) as raised,
        ):
            restore.prepare_restore(request)
        self.assertEqual(raised.exception.code, "RESTORE_BACKUP_UNAVAILABLE")
        self.assertIsNone(raised.exception.compatibility_outcome)
        self.assertFalse(mutation.published)

    def test_unsupported_format_fails_before_mutation(self):
        manifest_path = self.backup_root / backup.MANIFEST_NAME
        manifest = json.loads(manifest_path.read_bytes())
        manifest["schemaVersion"] = 2
        manifest_path.write_bytes(canonical_json_bytes(manifest) + b"\n")
        request, _, _, _, _ = self.request()
        with self.assertRaises(restore.RestorePreflightError) as raised:
            restore.prepare_restore(request)
        self.assertEqual(
            raised.exception.compatibility_outcome, CompatibilityOutcome.UNSUPPORTED
        )

    def test_requires_upgrade_demands_exact_explicit_acceptance(self):
        request, _, _, _, mutation = self.request(
            compatibility=CompatibilityOutcome.REQUIRES_UPGRADE
        )
        plan = restore.prepare_restore(request)
        self.assertEqual(plan.decision.outcome, CompatibilityOutcome.REQUIRES_UPGRADE)
        with self.assertRaisesRegex(
            restore.RestorePreflightError, "RESTORE_UPGRADE_NOT_ACCEPTED"
        ):
            restore.execute_restore(
                request,
                plan,
                accepted_plan_digest=plan.plan_digest,
                accept_upgrade=False,
            )
        self.assertEqual(self.events, [])
        result = restore.execute_restore(
            request,
            plan,
            accepted_plan_digest=plan.plan_digest,
            accept_upgrade=True,
        )
        self.assertEqual(result.state, restore.RestoreTerminalState.PUBLISHED)
        self.assertIn("upgrade.apply", self.events)
        self.assertTrue(mutation.published)

    def test_unsupported_and_corrupt_decisions_never_mutate(self):
        for outcome in (CompatibilityOutcome.UNSUPPORTED, CompatibilityOutcome.CORRUPT):
            with self.subTest(outcome=outcome):
                self.events.clear()
                request, _, _, _, mutation = self.request(compatibility=outcome)
                plan = restore.prepare_restore(request)
                with self.assertRaisesRegex(
                    restore.RestorePreflightError, "RESTORE_COMPATIBILITY_REJECTED"
                ):
                    restore.execute_restore(
                        request,
                        plan,
                        accepted_plan_digest=plan.plan_digest,
                    )
                self.assertEqual(self.events, [])
                self.assertFalse(mutation.published)

    def test_wrong_envelope_key_and_tamper_fail_before_mutation(self):
        request, _, _, _, mutation = self.request(wrong_key=True)
        with self.assertRaises(restore.RestorePreflightError) as raised:
            restore.prepare_restore(request)
        self.assertEqual(
            raised.exception.compatibility_outcome, CompatibilityOutcome.CORRUPT
        )
        self.assertEqual(self.events, [])
        self.assertFalse(mutation.published)

        envelope = self.backup_root / secret_envelope.ENVELOPE_PATH
        encoded = bytearray(envelope.read_bytes())
        encoded[-1] = ord("A") if encoded[-1] != ord("A") else ord("B")
        envelope.write_bytes(bytes(encoded))
        request, _, _, _, mutation = self.request()
        with self.assertRaises(restore.RestorePreflightError) as raised:
            restore.prepare_restore(request)
        self.assertEqual(
            raised.exception.compatibility_outcome, CompatibilityOutcome.CORRUPT
        )
        self.assertEqual(self.events, [])
        self.assertFalse(mutation.published)

    def test_database_filesystem_mid_validation_and_publish_failures_require_recovery(
        self,
    ):
        cases = (
            ({"database_fail": True}, "database.restore", False),
            (
                {"mutation_fail": "filesystem.restore"},
                "filesystem.restore",
                False,
            ),
            ({"mutation_fail": "bootstrap"}, "bootstrap", False),
            ({"validation_missing": "service.api.health"}, "validate", False),
            ({"mutation_fail": "target.publish"}, "target.publish", None),
        )
        for options, expected_event, target_active in cases:
            with self.subTest(options=options):
                self.events.clear()
                request, _, _, _, mutation = self.request(**options)
                plan = restore.prepare_restore(request)
                result = restore.execute_restore(
                    request,
                    plan,
                    accepted_plan_digest=plan.plan_digest,
                )
                self.assertEqual(
                    result.state, restore.RestoreTerminalState.RECOVERY_REQUIRED
                )
                self.assertIn(expected_event, self.events)
                self.assertIn("recovery.record", self.events)
                self.assertFalse(mutation.published)
                self.assertIsNotNone(mutation.recovery)
                self.assertEqual(mutation.recovery.plan_digest, plan.plan_digest)
                self.assertIs(mutation.recovery.target_active, target_active)

    def test_begin_failure_is_conservatively_recovery_required(self):
        request, _, _, _, mutation = self.request(mutation_fail="target.begin")
        plan = restore.prepare_restore(request)
        result = restore.execute_restore(
            request,
            plan,
            accepted_plan_digest=plan.plan_digest,
        )
        self.assertEqual(result.state, restore.RestoreTerminalState.RECOVERY_REQUIRED)
        self.assertEqual(result.recovery_evidence.failed_step, "target.begin")
        self.assertEqual(result.recovery_evidence.completed_steps, ("release.acquire",))
        self.assertIs(result.recovery_evidence.target_active, False)
        self.assertIs(mutation.recovery, result.recovery_evidence)

    def test_recovery_journal_failure_keeps_built_evidence_attached(self):
        request, _, _, _, mutation = self.request(
            mutation_fail="database.prepare",
            recovery_record_fail=True,
        )
        plan = restore.prepare_restore(request)
        with self.assertRaises(
            restore.RestoreRecoveryPersistenceError
        ) as raised:
            restore.execute_restore(
                request,
                plan,
                accepted_plan_digest=plan.plan_digest,
            )
        self.assertEqual(raised.exception.code, "RESTORE_RECOVERY_EVIDENCE_FAILED")
        self.assertIs(raised.exception.recovery_evidence, mutation.recovery)
        self.assertEqual(
            raised.exception.recovery_evidence.failed_step,
            "database.prepare",
        )

    def test_lock_acquisition_failure_has_stable_operational_code(self):
        request, _, _, _, mutation = self.request(mutation_fail="lock.acquire")
        plan = restore.prepare_restore(request)
        with self.assertRaises(restore.RestorePreflightError) as raised:
            restore.execute_restore(
                request,
                plan,
                accepted_plan_digest=plan.plan_digest,
            )
        self.assertEqual(raised.exception.code, "RESTORE_LOCK_ACQUISITION_FAILED")
        self.assertIsNone(raised.exception.compatibility_outcome)
        self.assertIsNone(mutation.recovery)

    def test_plan_or_destination_swap_is_rejected_before_mutation(self):
        request, destination, _, _, mutation = self.request()
        plan = restore.prepare_restore(request)
        with self.assertRaisesRegex(
            restore.RestorePreflightError, "RESTORE_PLAN_NOT_ACCEPTED"
        ):
            restore.execute_restore(
                request,
                plan,
                accepted_plan_digest="sha256:" + "0" * 64,
            )
        destination.snapshot = replace(
            destination.snapshot,
            evidence_digest="sha256:" + "9" * 64,
        )
        with self.assertRaisesRegex(
            restore.RestorePreflightError, "RESTORE_PLAN_STALE"
        ):
            restore.execute_restore(
                request,
                plan,
                accepted_plan_digest=plan.plan_digest,
            )
        self.assertEqual(self.events, [])
        self.assertFalse(mutation.published)

    def test_each_memory_integrity_invariant_is_a_publish_gate(self):
        for check in (
            "memory.mi1.external_metadata",
            "memory.mi2.provider_identity",
            "memory.mi3.merge_history",
            "memory.mi4.unsupported_payload",
            "memory.mi5.destructive_ambiguity",
        ):
            with self.subTest(check=check):
                self.events.clear()
                request, _, _, _, mutation = self.request(validation_missing=check)
                plan = restore.prepare_restore(request)
                result = restore.execute_restore(
                    request,
                    plan,
                    accepted_plan_digest=plan.plan_digest,
                )
                self.assertEqual(
                    result.state, restore.RestoreTerminalState.RECOVERY_REQUIRED
                )
                self.assertFalse(mutation.published)


class LocalFilesystemStagerTests(unittest.TestCase):
    def test_stages_only_explicit_verified_payload_members_with_private_modes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            backup_root = root / "backup"
            source = backup_root / "filesystem" / "media" / "poster.jpg"
            source.parent.mkdir(parents=True)
            source.write_bytes(b"poster")
            staging = root / "staging"
            restore.LocalFilesystemStager().stage(
                backup_root,
                staging,
                ("filesystem/media/poster.jpg",),
            )
            self.assertEqual(
                (staging / "filesystem" / "media" / "poster.jpg").read_bytes(),
                b"poster",
            )
            self.assertFalse((staging / "database.sql.gz").exists())

    def test_rejects_staging_beneath_a_linked_parent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            backup_root = root / "backup"
            backup_root.mkdir()
            real_parent = root / "real-parent"
            real_parent.mkdir()
            linked_parent = root / "linked-parent"
            try:
                linked_parent.symlink_to(real_parent, target_is_directory=True)
            except OSError:
                self.skipTest("directory symlinks are unavailable")
            with self.assertRaisesRegex(
                restore.RestoreAdapterError, "FILESYSTEM_STAGING_UNSAFE"
            ):
                restore.LocalFilesystemStager().stage(
                    backup_root,
                    linked_parent / "nested" / "staging",
                    (),
                )
            self.assertFalse((real_parent / "nested").exists())

    def test_member_and_total_copy_budgets_fail_before_staging(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            backup_root = root / "backup"
            first = backup_root / "filesystem" / "media" / "first.bin"
            second = backup_root / "filesystem" / "media" / "second.bin"
            first.parent.mkdir(parents=True)
            first.write_bytes(b"a" * 6)
            second.write_bytes(b"b" * 6)

            member_budget = DurabilityResourceBudget(
                maximum_compressed_member_bytes=32,
                maximum_uncompressed_database_bytes=64,
                maximum_filesystem_member_bytes=5,
                maximum_total_copied_bytes=20,
                maximum_compression_ratio=4,
            )
            member_staging = root / "member-staging"
            with (
                mock.patch.object(restore, "_RESOURCE_BUDGET", member_budget),
                self.assertRaisesRegex(
                    restore.RestoreAdapterError,
                    "FILESYSTEM_RESOURCE_BOUNDS_EXCEEDED",
                ),
            ):
                restore.LocalFilesystemStager().stage(
                    backup_root,
                    member_staging,
                    ("filesystem/media/first.bin",),
                )
            self.assertFalse(member_staging.exists())

            total_budget = DurabilityResourceBudget(
                maximum_compressed_member_bytes=32,
                maximum_uncompressed_database_bytes=64,
                maximum_filesystem_member_bytes=10,
                maximum_total_copied_bytes=10,
                maximum_compression_ratio=4,
            )
            total_staging = root / "total-staging"
            with (
                mock.patch.object(restore, "_RESOURCE_BUDGET", total_budget),
                self.assertRaisesRegex(
                    restore.RestoreAdapterError,
                    "FILESYSTEM_RESOURCE_BOUNDS_EXCEEDED",
                ),
            ):
                restore.LocalFilesystemStager().stage(
                    backup_root,
                    total_staging,
                    (
                        "filesystem/media/first.bin",
                        "filesystem/media/second.bin",
                    ),
                )
            self.assertFalse(total_staging.exists())


class PostgresRestoreAdapterTests(unittest.TestCase):
    def test_uses_fail_on_error_logical_psql_without_credentials_in_argv(self):
        class Runner:
            def __init__(self):
                self.calls = []

            def run(self, argv, *, stdin, env, timeout):
                pg_environment = {
                    name: value for name, value in env.items() if name.startswith("PG")
                }
                self.calls.append((tuple(argv), stdin, pg_environment, timeout))
                if "--tuples-only" in argv:
                    return restore.ProcessResult(0, b"0\n")
                return restore.ProcessResult(0, b"")

        runner = Runner()
        adapter = restore.SubprocessPostgresRestore(
            "postgresql://test-user@isolated.invalid/target",
            runner=runner,
        )
        with tempfile.TemporaryDirectory() as directory:
            dump = Path(directory) / "database.sql.gz"
            with (
                dump.open("wb") as raw,
                gzip.GzipFile(filename="", fileobj=raw, mode="wb", mtime=0) as stream,
            ):
                stream.write(b"CREATE TABLE restored(id integer);\n")
            with mock.patch.dict(
                os.environ,
                {
                    "PGHOST": "inherited.invalid",
                    "PGPASSWORD": "must-not-be-inherited",
                    "PGSERVICE": "inherited-service",
                },
            ):
                adapter.restore(dump)
        self.assertEqual(len(runner.calls), 2)
        empty_argv, _, empty_env, _ = runner.calls[0]
        import_argv, _, import_env, _ = runner.calls[1]
        self.assertIn("--tuples-only", empty_argv)
        self.assertIn("--set=ON_ERROR_STOP=1", import_argv)
        self.assertIn("--single-transaction", import_argv)
        self.assertNotIn("postgresql://", " ".join((*empty_argv, *import_argv)))
        self.assertEqual(empty_env["PGDATABASE"], "target")
        self.assertEqual(import_env["PGDATABASE"], "target")
        self.assertEqual(empty_env["PGHOST"], "isolated.invalid")
        self.assertEqual(import_env["PGHOST"], "isolated.invalid")
        self.assertEqual(empty_env["PGUSER"], "test-user")
        self.assertEqual(import_env["PGUSER"], "test-user")
        for inherited in ("PGPASSWORD", "PGSERVICE"):
            self.assertNotIn(inherited, empty_env)
            self.assertNotIn(inherited, import_env)

    def test_compression_bomb_is_rejected_before_database_import(self):
        class Runner:
            def __init__(self):
                self.calls = []

            def run(self, argv, *, stdin, env, timeout):
                self.calls.append(tuple(argv))
                if "--tuples-only" in argv:
                    return restore.ProcessResult(0, b"0\n")
                return restore.ProcessResult(0, b"")

        runner = Runner()
        adapter = restore.SubprocessPostgresRestore(
            "postgresql://test-user@isolated.invalid/target",
            runner=runner,
        )
        budget = DurabilityResourceBudget(
            maximum_compressed_member_bytes=1024 * 1024,
            maximum_uncompressed_database_bytes=2 * 1024 * 1024,
            maximum_filesystem_member_bytes=1024 * 1024,
            maximum_total_copied_bytes=4 * 1024 * 1024,
            maximum_compression_ratio=2,
        )
        with tempfile.TemporaryDirectory() as directory:
            dump = Path(directory) / "database.sql.gz"
            with (
                dump.open("wb") as raw,
                gzip.GzipFile(filename="", fileobj=raw, mode="wb", mtime=0) as stream,
            ):
                stream.write(b"z" * (1024 * 1024))
            with (
                mock.patch.object(restore, "_RESOURCE_BUDGET", budget),
                self.assertRaisesRegex(
                    restore.RestoreAdapterError,
                    "DATABASE_RESOURCE_BOUNDS_EXCEEDED",
                ),
            ):
                adapter.restore(dump)

        self.assertEqual(len(runner.calls), 1)


if __name__ == "__main__":
    unittest.main()
