from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
import uuid
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from unittest import mock

from durability import migration
from durability.compatibility import (
    EVALUATION_ORDER,
    CompatibilityDecision,
    CompatibilityOperation,
    CompatibilityOutcome,
    Dimension,
    DimensionAssessment,
    ReasonCode,
    UpgradeAction,
    evaluate_compatibility,
)
from durability.instance import InstanceLocator, ListenIdentity
from durability.secret_envelope import OneTimeKey, SecretEntry


def digest(value: bytes) -> str:
    return f"sha256:{hashlib.sha256(value).hexdigest()}"


class FakePgDump:
    payload = b"-- PostgreSQL database dump\nSELECT 1;\n"

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, int]] = []

    def run(
        self,
        database_url: str,
        raw_output: Path,
        *,
        executable: str,
        timeout: int,
    ) -> str:
        self.calls.append(
            (
                "DATABASE_URL_PRESENT" if database_url else "DATABASE_URL_MISSING",
                executable,
                timeout,
            )
        )
        raw_output.write_bytes(self.payload)
        return "pg_dump (PostgreSQL) 16.4"


class Probe:
    def __init__(
        self, snapshots: tuple[migration.SourceConsistencySnapshot, ...]
    ) -> None:
        self.snapshots = snapshots
        self.index = 0

    def snapshot(self) -> migration.SourceConsistencySnapshot:
        value = self.snapshots[min(self.index, len(self.snapshots) - 1)]
        self.index += 1
        return value


class ReferenceProbe:
    def __init__(self, inventory: migration.DatabaseReferenceInventory) -> None:
        self.inventory = inventory
        self.calls = 0

    def capture(self) -> migration.DatabaseReferenceInventory:
        self.calls += 1
        return self.inventory


class FakeTarget:
    def __init__(
        self,
        inspection: migration.TargetInspection,
        *,
        fail_at: str | None = None,
        rollback_fails: bool = False,
    ) -> None:
        self.inspection = inspection
        self.fail_at = fail_at
        self.rollback_fails = rollback_fails
        self.events: list[str] = []
        self.opened_secrets = None
        self.recovery_evidence: migration.MigrationRecoveryEvidence | None = None
        self.applied_upgrade_actions: tuple[UpgradeAction, ...] = ()
        self.staged_paths: list[Path] = []

    def inspect(self) -> migration.TargetInspection:
        self.events.append("inspect")
        return self.inspection

    def _event(self, name: str) -> None:
        self.events.append(name)
        if self.fail_at == name:
            raise RuntimeError("callback detail must be redacted")

    def begin(self, *, bundle_id: str, instance_id: str) -> None:
        self._event("begin")

    def stage_database(self, path: Path, metadata: dict[str, object]) -> None:
        self.staged_paths.append(path)
        self._event("database")

    def stage_plugin_package(self, path: Path, metadata: dict[str, object]) -> None:
        self.staged_paths.append(path)
        self._event("plugin")

    def stage_local_media(self, path: Path, metadata: dict[str, object]) -> None:
        self.staged_paths.append(path)
        self._event("media")

    def stage_configuration(self, configuration, secrets) -> None:
        self.opened_secrets = secrets
        self._event("configuration")

    def stage_private_state(self, state: dict[str, object]) -> None:
        self._event("private")

    def stage_updater_state(self, state: dict[str, object]) -> None:
        self._event("updater")

    def apply_upgrade(self, actions: tuple[UpgradeAction, ...]) -> None:
        self.applied_upgrade_actions = actions
        self._event("upgrade")

    def validate_inactive(self, *, bundle_id: str, instance_id: str) -> bool:
        self._event("validate")
        return self.fail_at != "validate-false"

    def publish_inactive(self, *, bundle_id: str, instance_id: str) -> None:
        self._event("publish")

    def rollback(self, *, bundle_id: str) -> None:
        self.events.append("rollback")
        if self.rollback_fails:
            raise RuntimeError("rollback detail must be redacted")

    def record_recovery_required(
        self, evidence: migration.MigrationRecoveryEvidence
    ) -> None:
        self.recovery_evidence = evidence
        self._event("recovery")


class MigrationRuntimeTests(unittest.TestCase):
    maxDiff = None

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.destination = self.root / "migration-bundles"
        self.plugin_bytes = b"AJPLUGIN\x00canonical package"
        self.plugin_source = self.root / "package.ajplugin"
        self.plugin_source.write_bytes(self.plugin_bytes)
        self.media_bytes = b"canonical local media"
        self.media_source = self.root / "poster.webp"
        self.media_source.write_bytes(self.media_bytes)
        self.instance_id = "11111111-2222-4333-8444-555555555555"
        self.bundle_id = uuid.UUID("12345678-1234-4678-9234-567812345678")
        self.external_key = OneTimeKey.from_bytes(b"E" * 32)
        self.r2_identity = migration.R2PhysicalIdentity(
            endpoint="HTTPS://R2.Example.Invalid:443/",
            account_identity="ACCOUNT-1",
            bucket="ANIMEMO-MEDIA",
        )
        self.snapshot = migration.SourceConsistencySnapshot(
            generation="source-generation-7",
            config_generation="config-generation-3",
            quiesced=True,
            writes_blocked=True,
            updater_idle=True,
            database_migration_idle=True,
            plugin_operations_idle=True,
            media_writes_idle=True,
        )

    def locator(self) -> InstanceLocator:
        return InstanceLocator(
            schema_version=1,
            instance_id=self.instance_id,
            app_root=PurePosixPath("/opt/animemo"),
            data_root=PurePosixPath("/data/animemo"),
            deployment_profile="v1.1-standard",
            listen=ListenIdentity("127.0.0.1", 8000),
            public_origin="https://anime.example.invalid",
            managed_config_path=PurePosixPath("/data/animemo/config/animemo.json"),
            config_revision="11111111-1111-4111-8111-111111111111",
            release_identity={
                "version": "v1.1.0",
                "channel": "stable",
                "commit": "a" * 40,
                "manifestDigest": "sha256:" + "1" * 64,
                "apiDigest": "sha256:" + "2" * 64,
                "webDigest": "sha256:" + "3" * 64,
            },
        )

    def configuration(
        self,
        mode: migration.ConfigurationMode = migration.ConfigurationMode.PRESERVE,
    ) -> migration.MigrationConfiguration:
        target_local = {
            "appRoot": "TARGET-LOCAL",
            "dataRoot": "TARGET-LOCAL",
            "managedConfigPath": "TARGET-LOCAL",
            "configRevision": "TARGET-LOCAL",
            "databaseHost": "TARGET-LOCAL",
            "databaseCredential": "TARGET-LOCAL",
            "redisHost": "TARGET-LOCAL",
            "redisCredential": "TARGET-LOCAL",
        }
        if mode is migration.ConfigurationMode.RECONFIGURE:
            return migration.MigrationConfiguration(
                mode=mode,
                non_secret={"featureFlags": ["journal"]},
                dispositions={
                    **target_local,
                    "publicOrigin": "RECONFIGURE",
                    "listen": "RECONFIGURE",
                },
                target_public_origin="https://new.example.invalid",
                target_listen=ListenIdentity("127.0.0.1", 9000),
            )
        if mode is migration.ConfigurationMode.TARGET_LOCAL:
            return migration.MigrationConfiguration(
                mode=mode,
                non_secret={"featureFlags": ["journal"]},
                dispositions={
                    **target_local,
                    "publicOrigin": "PRESERVE",
                    "listen": "TARGET-LOCAL",
                },
                target_listen=ListenIdentity("::1", 9000),
            )
        return migration.MigrationConfiguration(
            mode=mode,
            non_secret={"featureFlags": ["journal"]},
            dispositions={
                **target_local,
                "publicOrigin": "PRESERVE",
                "listen": "PRESERVE",
            },
        )

    def request(
        self,
        *,
        probe: Probe | None = None,
        configuration: migration.MigrationConfiguration | None = None,
        with_r2: bool = True,
        target_r2_identities: dict[str, migration.R2PhysicalIdentity] | None = None,
        private_state: dict[str, object] | None = None,
    ) -> migration.MigrationBundleRequest:
        plugins = (
            migration.PluginPackage(
                project_id="project-1",
                version_id="version-2",
                deployment_id="deployment-3",
                digest=digest(self.plugin_bytes),
                source=self.plugin_source,
                sdk_apis=("animemo.plugin/v2",),
                manifest_snapshot_digest="sha256:" + "4" * 64,
            ),
        )
        local = (
            migration.LocalMediaObject(
                media_id="media-local-1",
                object_key="posters/one.webp",
                digest=digest(self.media_bytes),
                size_bytes=len(self.media_bytes),
                source=self.media_source,
                memory_references=("journal:1", "poster:1"),
            ),
        )
        r2 = (
            (
                migration.R2MediaObject(
                    media_id="media-r2-2",
                    backend_id="r2-primary",
                    object_key="posters/two.webp",
                    digest="sha256:" + "5" * 64,
                    size_bytes=1234,
                    source_identity=self.r2_identity,
                    memory_references=("journal:2", "poster:2"),
                ),
            )
            if with_r2
            else ()
        )
        target_identities = (
            {"r2-primary": self.r2_identity}
            if target_r2_identities is None
            else target_r2_identities
        )
        locator = self.locator()
        reference_inventory = migration.DatabaseReferenceInventory(
            generation="database-references-5",
            plugin_packages=tuple(
                migration.PluginDatabaseReference(
                    project_id=item.project_id,
                    version_id=item.version_id,
                    deployment_id=item.deployment_id,
                    digest=item.digest,
                    sdk_apis=item.sdk_apis,
                    manifest_snapshot_digest=item.manifest_snapshot_digest,
                )
                for item in plugins
            ),
            local_media=tuple(
                migration.LocalMediaDatabaseReference(
                    media_id=item.media_id,
                    object_key=item.object_key,
                    digest=item.digest,
                    size_bytes=item.size_bytes,
                    memory_references=item.memory_references,
                )
                for item in local
            ),
            r2_media=tuple(
                migration.R2MediaDatabaseReference(
                    media_id=item.media_id,
                    backend_id=item.backend_id,
                    object_key=item.object_key,
                    digest=item.digest,
                    size_bytes=item.size_bytes,
                    source_identity=item.source_identity,
                    memory_references=item.memory_references,
                )
                for item in r2
            ),
        )
        return migration.MigrationBundleRequest(
            destination_root=self.destination,
            source_locator=locator,
            source_probe=probe or Probe((self.snapshot,)),
            database_url="postgresql://isolated.invalid/animemo",
            database_server_major=16,
            database_reference_probe=ReferenceProbe(reference_inventory),
            deployment_contract={"id": "animemo.deployment/v1"},
            database_contract={"id": "animemo.database/v1", "schemaVersion": 1},
            configuration_contract={"id": "animemo.configuration/v1"},
            configuration=configuration or self.configuration(),
            private_state=private_state
            or {
                "schemaVersion": 1,
                "instanceLifecycle": "INITIALIZED",
                "allowlistedEntries": ["credential-ciphertext-state"],
                "mergeHistoryReferences": ["merge:old", "merge:new"],
                "unknownFilesCopied": False,
            },
            updater_state={
                "schemaVersion": 1,
                "generation": "updater-9",
                "operationState": "IDLE",
                "current": dict(locator.release_identity),
                "previousHistory": [],
                "completedOperations": [],
                "pendingOperation": None,
                "manualRecoveryRequired": False,
            },
            external_secret=self.external_key,
            secret_entries=(
                SecretEntry.preserve("CREDENTIAL_ENCRYPTION_KEY", b"C" * 32),
                SecretEntry.preserve("DJANGO_SECRET_KEY", b"D" * 32),
            ),
            plugins=plugins,
            local_media=local,
            r2_media=r2,
            target_r2_identities=target_identities,
        )

    def create(
        self,
        *,
        request: migration.MigrationBundleRequest | None = None,
        bundle_id: uuid.UUID | None = None,
    ) -> tuple[migration.MigrationBundleResult, FakePgDump]:
        runner = FakePgDump()
        result = migration.create_migration_bundle(
            request or self.request(),
            pg_dump_runner=runner,
            bundle_id=bundle_id or self.bundle_id,
            clock=lambda: datetime(2026, 8, 16, 1, 2, 3, tzinfo=UTC),
        )
        return result, runner

    def decision(
        self,
        verification: migration.MigrationVerification,
        *,
        outcome: CompatibilityOutcome = CompatibilityOutcome.COMPATIBLE,
    ) -> CompatibilityDecision:
        compatible_reasons = {
            Dimension.FORMAT: ReasonCode.FORMAT_SUPPORTED,
            Dimension.INTEGRITY_AUTHENTICATION: ReasonCode.INTEGRITY_AUTHENTICATED,
            Dimension.DEPLOYMENT_CONTRACT: ReasonCode.DEPLOYMENT_CONTRACT_SUPPORTED,
            Dimension.SCHEMA_CONTRACTS: ReasonCode.SCHEMA_CONTRACTS_SUPPORTED,
            Dimension.EXACT_RELEASE_IDENTITY: ReasonCode.RELEASE_IDENTITY_VERIFIED,
            Dimension.PLATFORM_RUNTIME: ReasonCode.PLATFORM_RUNTIME_SUPPORTED,
            Dimension.SUPPORTED_PATH: ReasonCode.DIRECT_PATH_SUPPORTED,
        }
        dimensions = [
            DimensionAssessment(
                name=name,
                outcome=CompatibilityOutcome.COMPATIBLE,
                reason_code=compatible_reasons[name],
                source={"identity": f"source-{name.value}"},
                target={"capability": f"target-{name.value}"},
            )
            for name in EVALUATION_ORDER
        ]
        actions: tuple[UpgradeAction, ...] = ()
        if outcome is CompatibilityOutcome.UNSUPPORTED:
            dimensions[-1] = DimensionAssessment(
                name=Dimension.SUPPORTED_PATH,
                outcome=outcome,
                reason_code=ReasonCode.SUPPORTED_PATH_UNAVAILABLE,
                source=dimensions[-1].source,
                target=dimensions[-1].target,
            )
        elif outcome is CompatibilityOutcome.REQUIRES_UPGRADE:
            dimensions[-1] = DimensionAssessment(
                name=Dimension.SUPPORTED_PATH,
                outcome=outcome,
                reason_code=ReasonCode.ORDERED_PATH_REQUIRED,
                source=dimensions[-1].source,
                target=dimensions[-1].target,
            )
            actions = (
                UpgradeAction(
                    order=1,
                    kind="APPLY_SCHEMA_MIGRATION",
                    input_identity={"databaseContract": "v1"},
                    output_identity={"databaseContract": "v1-compatible"},
                    required_release_identity=dict(self.locator().release_identity),
                ),
            )
        elif outcome is not CompatibilityOutcome.COMPATIBLE:
            raise AssertionError("test helper outcome is unsupported")
        return evaluate_compatibility(
            CompatibilityOperation.MIGRATION,
            verification.artifact_identity(),
            dimensions,
            actions=actions,
        )

    def inspection(
        self,
        *,
        active_instance_id: str | None = None,
        identities: dict[str, migration.R2PhysicalIdentity] | None = None,
        apis: frozenset[str] = frozenset(("animemo.plugin/v2",)),
        release_identity: dict[str, object] | None | bool = True,
        deployment_contract: dict[str, object] | None | bool = True,
        updater_current: dict[str, object] | None | bool = True,
    ) -> migration.TargetInspection:
        locator = self.locator()
        return migration.TargetInspection(
            canonical_roots=True,
            empty_owned_target=True,
            active_instance_id=active_instance_id,
            release_identity=(
                dict(locator.release_identity)
                if release_identity is True
                else release_identity
            ),
            deployment_contract=(
                {"id": "animemo.deployment/v1"}
                if deployment_contract is True
                else deployment_contract
            ),
            updater_current=(
                dict(locator.release_identity)
                if updater_current is True
                else updater_current
            ),
            target_r2_identities=(
                {"r2-primary": self.r2_identity} if identities is None else identities
            ),
            supported_plugin_sdk_apis=apis,
        )

    def test_happy_create_verify_uses_shared_logical_postgres(self) -> None:
        result, runner = self.create()
        verification = migration.verify_migration_bundle(result.path)

        self.assertEqual(result.bundle_id, str(self.bundle_id))
        self.assertEqual(result.instance_id, self.instance_id)
        self.assertEqual(verification.bundle_id, result.bundle_id)
        self.assertEqual(verification.manifest_digest, result.manifest_digest)
        self.assertEqual(runner.calls, [("DATABASE_URL_PRESENT", "pg_dump", 600)])
        self.assertEqual(
            (result.path / migration.DATABASE_MEMBER).read_bytes()[:2], b"\x1f\x8b"
        )
        self.assertFalse(
            any(
                path.name.startswith(migration.STAGING_PREFIX)
                for path in self.destination.iterdir()
            )
        )
        self.assertEqual(
            verification.manifest["databaseReferences"]["generation"],
            "database-references-5",
        )

    def test_database_capture_resource_limit_has_stable_migration_code(self) -> None:
        with (
            mock.patch.object(FakePgDump, "payload", b"A" * (3 * 1024 * 1024)),
            self.assertRaises(migration.MigrationOperationalError) as raised,
        ):
            self.create()

        self.assertEqual(
            raised.exception.code,
            "MIGRATION_RESOURCE_BOUNDS_EXCEEDED",
        )

    def test_unique_bundle_id_preserves_instance_id(self) -> None:
        first, _ = self.create()
        second_id = uuid.UUID("22345678-1234-4678-9234-567812345678")
        second, _ = self.create(bundle_id=second_id)

        self.assertNotEqual(first.bundle_id, second.bundle_id)
        self.assertEqual(first.instance_id, second.instance_id)
        self.assertEqual(second.instance_id, self.instance_id)

    def test_canonical_members_exclude_runtime_preview_staging_and_locks(self) -> None:
        result, _ = self.create()
        relative = {
            path.relative_to(result.path).as_posix()
            for path in result.path.rglob("*")
            if path.is_file()
        }

        self.assertIn("plugins/manifest.json", relative)
        self.assertTrue(any(path.startswith("plugins/cas/") for path in relative))
        self.assertFalse(any("runtime" in path for path in relative))
        self.assertFalse(any("previews" in path for path in relative))
        self.assertFalse(any("staging" in path for path in relative))
        self.assertFalse(any(".locks" in path for path in relative))

    def test_secret_envelope_round_trip_and_plaintext_absence(self) -> None:
        result, _ = self.create()
        verification = migration.verify_migration_bundle(result.path)
        target = FakeTarget(self.inspection())

        consumed = migration.consume_migration_bundle(
            result.path,
            external_secret=self.external_key,
            compatibility=self.decision(verification),
            target=target,
        )

        self.assertEqual(consumed.state, "READY_FOR_HANDOFF")
        self.assertFalse(consumed.target_active)
        self.assertFalse(consumed.source_deleted)
        self.assertEqual(
            target.opened_secrets.get_secret("DJANGO_SECRET_KEY").reveal(), b"D" * 32
        )
        public_bytes = b"".join(
            (result.path / name).read_bytes()
            for name in (
                migration.MANIFEST_NAME,
                migration.CHECKSUMS_NAME,
                migration.CONFIG_MEMBER,
                migration.PRIVATE_MANIFEST_MEMBER,
                migration.UPDATER_STATE_MEMBER,
            )
        )
        self.assertNotIn(b"C" * 16, public_bytes)
        self.assertNotIn(b"D" * 16, public_bytes)

    def test_wrong_key_and_tamper_fail_before_target_mutation(self) -> None:
        result, _ = self.create()
        verification = migration.verify_migration_bundle(result.path)
        target = FakeTarget(self.inspection())
        with self.assertRaises(migration.MigrationCorruptError):
            migration.consume_migration_bundle(
                result.path,
                external_secret=OneTimeKey.from_bytes(b"W" * 32),
                compatibility=self.decision(verification),
                target=target,
            )
        self.assertEqual(target.events, ["inspect"])

        database = result.path / migration.DATABASE_MEMBER
        database.write_bytes(database.read_bytes() + b"tamper")
        untouched = FakeTarget(self.inspection())
        with self.assertRaises(migration.MigrationCorruptError):
            migration.consume_migration_bundle(
                result.path,
                external_secret=self.external_key,
                compatibility=self.decision(verification),
                target=untouched,
            )
        self.assertEqual(untouched.events, [])

    def test_consume_uses_one_private_snapshot_after_verification(self) -> None:
        result, _ = self.create()
        verification = migration.verify_migration_bundle(result.path)

        class MutatingInspectionTarget(FakeTarget):
            def inspect(self) -> migration.TargetInspection:
                envelope = result.path / migration.ENVELOPE_PATH
                envelope.write_bytes(envelope.read_bytes() + b"transport-race")
                return super().inspect()

        target = MutatingInspectionTarget(self.inspection())
        consumed = migration.consume_migration_bundle(
            result.path,
            external_secret=self.external_key,
            compatibility=self.decision(verification),
            target=target,
        )

        self.assertEqual(consumed.state, "READY_FOR_HANDOFF")
        self.assertTrue(target.staged_paths)
        self.assertTrue(
            all(result.path not in path.parents for path in target.staged_paths)
        )
        with self.assertRaises(migration.MigrationCorruptError):
            migration.verify_migration_bundle(result.path)

    def test_database_reference_inventory_must_exactly_match_payloads(self) -> None:
        request = self.request()
        empty = migration.DatabaseReferenceInventory(
            generation="database-references-6",
            plugin_packages=(),
            local_media=(),
            r2_media=(),
        )
        mismatch = replace(
            request,
            destination_root=self.root / "database-reference-mismatch",
            database_reference_probe=ReferenceProbe(empty),
        )
        with self.assertRaisesRegex(
            migration.MigrationCorruptError,
            "MIGRATION_DATABASE_REFERENCE_MISMATCH",
        ):
            self.create(request=mismatch)

    def test_unavailable_bundle_is_operational_without_compatibility_outcome(
        self,
    ) -> None:
        missing = self.root / "transport-not-present"
        with self.assertRaises(migration.MigrationOperationalError) as captured:
            migration.verify_migration_bundle(missing)
        self.assertNotIsInstance(captured.exception, migration.MigrationCorruptError)
        self.assertIsNone(captured.exception.compatibility_outcome)
        self.assertEqual(str(captured.exception), "MIGRATION_BUNDLE_UNAVAILABLE")

    def test_source_change_aborts_and_cleans_only_owned_staging(self) -> None:
        changed = replace(self.snapshot, generation="source-generation-8")
        foreign = self.destination / "existing-evidence"
        foreign.mkdir(parents=True)
        marker = foreign / "keep"
        marker.write_text("keep", encoding="utf-8")
        request = self.request(probe=Probe((self.snapshot, changed)))

        with self.assertRaisesRegex(
            migration.MigrationOperationalError, "MIGRATION_SOURCE_CHANGED"
        ):
            self.create(request=request)

        self.assertEqual(marker.read_text(encoding="utf-8"), "keep")
        self.assertFalse((self.destination / str(self.bundle_id)).exists())
        self.assertFalse(
            any(
                path.name.startswith(migration.STAGING_PREFIX)
                for path in self.destination.iterdir()
            )
        )

    def test_same_r2_exact_match_succeeds(self) -> None:
        result, _ = self.create()
        verification = migration.verify_migration_bundle(result.path)
        target = FakeTarget(self.inspection())

        migration.consume_migration_bundle(
            result.path,
            external_secret=self.external_key,
            compatibility=self.decision(verification),
            target=target,
        )

        self.assertEqual(target.events[-2:], ["validate", "publish"])
        self.assertNotIn("r2-copy", target.events)

    def test_different_missing_or_indeterminate_r2_is_unsupported(self) -> None:
        cases = (
            {},
            {
                "r2-primary": migration.R2PhysicalIdentity(
                    "https://r2.example.invalid", "account-2", "animemo-media"
                )
            },
            {
                "r2-primary": migration.R2PhysicalIdentity(
                    "not-an-endpoint", "account-1", "animemo-media"
                )
            },
        )
        for index, identities in enumerate(cases):
            with self.subTest(index=index):
                destination = self.root / f"unsupported-{index}"
                request = replace(
                    self.request(target_r2_identities=identities),
                    destination_root=destination,
                )
                with self.assertRaises(migration.MigrationUnsupportedError):
                    self.create(request=request)
                self.assertFalse((destination / str(self.bundle_id)).exists())

    def test_local_media_and_plugin_digest_or_missing_member_is_corrupt(self) -> None:
        bad_plugin = replace(self.request().plugins[0], digest="sha256:" + "0" * 64)
        with self.assertRaises(migration.MigrationCorruptError):
            self.create(request=replace(self.request(), plugins=(bad_plugin,)))

        result, _ = self.create()
        plugin_member = next((result.path / "plugins/cas").iterdir())
        plugin_member.unlink()
        with self.assertRaises(migration.MigrationCorruptError):
            migration.verify_migration_bundle(result.path)

    def test_unknown_member_symlink_and_hardlink_fail_closed(self) -> None:
        result, _ = self.create()
        (result.path / "unknown.bin").write_bytes(b"unknown")
        with self.assertRaises(migration.MigrationCorruptError):
            migration.verify_migration_bundle(result.path)
        (result.path / "unknown.bin").unlink()

        hardlink = result.path / "plugins/cas" / ("f" * 64 + ".ajplugin")
        os.link(result.path / migration.DATABASE_METADATA_MEMBER, hardlink)
        with self.assertRaises(migration.MigrationCorruptError):
            migration.verify_migration_bundle(result.path)
        hardlink.unlink()

        verification = migration.verify_migration_bundle(result.path)
        symlink = result.path / "plugins/cas" / ("e" * 64 + ".ajplugin")
        try:
            symlink.symlink_to(result.path / migration.DATABASE_METADATA_MEMBER)
        except OSError:
            self.skipTest("symlink creation is unavailable")
        with self.assertRaises(migration.MigrationCorruptError):
            migration.verify_migration_bundle(result.path)
        target = FakeTarget(self.inspection())
        with self.assertRaises(migration.MigrationCorruptError):
            migration.consume_migration_bundle(
                result.path,
                external_secret=self.external_key,
                compatibility=self.decision(verification),
                target=target,
            )
        self.assertEqual(target.events, [])

    def test_configuration_mode_matrix_keeps_origin_and_listen_separate(self) -> None:
        modes = (
            migration.ConfigurationMode.PRESERVE,
            migration.ConfigurationMode.RECONFIGURE,
            migration.ConfigurationMode.TARGET_LOCAL,
        )
        for index, mode in enumerate(modes):
            with self.subTest(mode=mode):
                request = replace(
                    self.request(configuration=self.configuration(mode)),
                    destination_root=self.root / f"config-{index}",
                )
                result, _ = self.create(
                    request=request,
                    bundle_id=uuid.UUID(
                        f"{index + 3}2345678-1234-4678-9234-567812345678"
                    ),
                )
                config = json.loads(
                    (result.path / migration.CONFIG_MEMBER).read_bytes()
                )
                self.assertEqual(config["mode"], mode.value)
                if mode is migration.ConfigurationMode.RECONFIGURE:
                    self.assertEqual(
                        config["effectivePublicOrigin"], "https://new.example.invalid"
                    )
                    self.assertEqual(config["effectiveListen"]["port"], 9000)
                if mode is migration.ConfigurationMode.TARGET_LOCAL:
                    self.assertEqual(
                        config["effectivePublicOrigin"],
                        "https://anime.example.invalid",
                    )
                    self.assertEqual(config["effectiveListen"]["host"], "::1")
                    self.assertFalse(config["activationAllowed"])

        invalid = replace(
            self.configuration(migration.ConfigurationMode.TARGET_LOCAL),
            target_listen=ListenIdentity("0.0.0.0", 9000),
        )
        with self.assertRaises(migration.MigrationOperationalError):
            self.create(
                request=replace(
                    self.request(configuration=invalid),
                    destination_root=self.root / "target-local-invalid",
                )
            )

    def test_consume_ordering_records_recovery_after_database_boundary(self) -> None:
        result, _ = self.create()
        verification = migration.verify_migration_bundle(result.path)
        target = FakeTarget(self.inspection(), fail_at="configuration")

        with self.assertRaises(migration.MigrationRecoveryRequiredError) as captured:
            migration.consume_migration_bundle(
                result.path,
                external_secret=self.external_key,
                compatibility=self.decision(verification),
                target=target,
            )

        self.assertEqual(
            target.events,
            [
                "inspect",
                "begin",
                "database",
                "plugin",
                "media",
                "configuration",
                "recovery",
            ],
        )
        self.assertNotIn("rollback", target.events)
        self.assertEqual(captured.exception.evidence.failed_step, "configuration")
        self.assertFalse(captured.exception.evidence.automatic_rollback)
        self.assertFalse(captured.exception.evidence.target_active)

    def test_split_brain_and_compatibility_reject_before_mutation(self) -> None:
        result, _ = self.create()
        verification = migration.verify_migration_bundle(result.path)
        split = FakeTarget(self.inspection(active_instance_id=self.instance_id))
        with self.assertRaisesRegex(
            migration.MigrationOperationalError, "MIGRATION_SPLIT_BRAIN_DETECTED"
        ):
            migration.consume_migration_bundle(
                result.path,
                external_secret=self.external_key,
                compatibility=self.decision(verification),
                target=split,
            )
        self.assertEqual(split.events, ["inspect"])

        incompatible = FakeTarget(self.inspection())
        with self.assertRaises(migration.MigrationUnsupportedError):
            migration.consume_migration_bundle(
                result.path,
                external_secret=self.external_key,
                compatibility=self.decision(
                    verification, outcome=CompatibilityOutcome.UNSUPPORTED
                ),
                target=incompatible,
            )
        self.assertEqual(incompatible.events, [])

        forged = CompatibilityDecision(
            operation=CompatibilityOperation.MIGRATION,
            outcome=CompatibilityOutcome.COMPATIBLE,
            reason_code=ReasonCode.ALL_DIMENSIONS_COMPATIBLE,
            summary="forged empty-dimension decision",
            blocking_dimension=None,
            artifact=verification.artifact_identity(),
            evaluated_dimensions=(),
            actions=(),
        )
        forged_target = FakeTarget(self.inspection())
        with self.assertRaisesRegex(
            migration.MigrationOperationalError,
            "MIGRATION_COMPATIBILITY_DECISION_INVALID",
        ):
            migration.consume_migration_bundle(
                result.path,
                external_secret=self.external_key,
                compatibility=forged,
                target=forged_target,
            )
        self.assertEqual(forged_target.events, [])

        mismatched = replace(
            self.decision(verification),
            artifact=replace(
                verification.artifact_identity(),
                artifact_id="22345678-1234-4678-9234-567812345678",
            ),
        )
        with self.assertRaises(migration.MigrationOperationalError):
            migration.consume_migration_bundle(
                result.path,
                external_secret=self.external_key,
                compatibility=mismatched,
                target=FakeTarget(self.inspection()),
            )

    def test_requires_upgrade_needs_exact_approval_and_adapter_step(self) -> None:
        result, _ = self.create()
        verification = migration.verify_migration_bundle(result.path)
        decision = self.decision(
            verification, outcome=CompatibilityOutcome.REQUIRES_UPGRADE
        )
        unapproved = FakeTarget(self.inspection())
        with self.assertRaisesRegex(
            migration.MigrationOperationalError,
            "MIGRATION_UPGRADE_APPROVAL_REQUIRED",
        ):
            migration.consume_migration_bundle(
                result.path,
                external_secret=self.external_key,
                compatibility=decision,
                target=unapproved,
            )
        self.assertEqual(unapproved.events, [])

        wrong = replace(
            decision.actions[0], output_identity={"databaseContract": "wrong"}
        )
        with self.assertRaisesRegex(
            migration.MigrationOperationalError,
            "MIGRATION_UPGRADE_APPROVAL_MISMATCH",
        ):
            migration.consume_migration_bundle(
                result.path,
                external_secret=self.external_key,
                compatibility=decision,
                target=FakeTarget(self.inspection()),
                approved_upgrade_actions=(wrong,),
            )

        approved = FakeTarget(self.inspection())
        consumed = migration.consume_migration_bundle(
            result.path,
            external_secret=self.external_key,
            compatibility=decision,
            target=approved,
            approved_upgrade_actions=decision.actions,
        )
        self.assertEqual(consumed.state, "READY_FOR_HANDOFF")
        self.assertEqual(approved.applied_upgrade_actions, decision.actions)
        self.assertIn("upgrade", approved.events)

        upgrade_failure = FakeTarget(self.inspection(), fail_at="upgrade")
        with self.assertRaises(migration.MigrationRecoveryRequiredError) as captured:
            migration.consume_migration_bundle(
                result.path,
                external_secret=self.external_key,
                compatibility=decision,
                target=upgrade_failure,
                approved_upgrade_actions=decision.actions,
            )
        self.assertEqual(captured.exception.evidence.failed_step, "upgrade")
        self.assertNotIn("rollback", upgrade_failure.events)

    def test_explicit_activation_handoff_and_target_local_prohibition(self) -> None:
        result, _ = self.create()
        verification = migration.verify_migration_bundle(result.path)
        consumed = migration.consume_migration_bundle(
            result.path,
            external_secret=self.external_key,
            compatibility=self.decision(verification),
            target=FakeTarget(self.inspection()),
        )
        handoff = migration.ActivationHandoff(
            bundle_id=consumed.bundle_id,
            instance_id=consumed.instance_id,
            source_consistency_generation=consumed.source_consistency_generation,
            source_quiesced=True,
            source_writes_blocked=True,
            source_ownership_released=True,
            target_inactive=True,
            target_local_health_passed=True,
            administrator_confirmed=True,
        )
        permit = migration.authorize_activation(consumed, handoff)
        self.assertTrue(permit.target_may_activate)
        self.assertFalse(permit.source_may_resume)
        self.assertFalse(permit.source_may_be_deleted)
        with self.assertRaises(migration.MigrationOperationalError):
            migration.authorize_activation(
                consumed, replace(handoff, administrator_confirmed=False)
            )
        with self.assertRaisesRegex(
            migration.MigrationOperationalError,
            "MIGRATION_TARGET_LOCAL_NOT_ACTIVATABLE",
        ):
            migration.authorize_activation(
                replace(
                    consumed,
                    configuration_mode=migration.ConfigurationMode.TARGET_LOCAL,
                ),
                handoff,
            )

    def test_memory_integrity_fixtures_preserve_and_fail_closed(self) -> None:
        result, _ = self.create()
        manifest = json.loads((result.path / migration.MANIFEST_NAME).read_bytes())
        self.assertEqual(
            manifest["media"]["local"][0]["memoryReferences"],
            ["journal:1", "poster:1"],
        )
        self.assertEqual(
            manifest["privateState"]["mergeHistoryReferences"],
            ["merge:old", "merge:new"],
        )
        self.assertEqual(
            manifest["media"]["unknownOrphanPolicy"], "PRESERVE_NEVER_DELETE"
        )
        self.assertFalse(manifest["media"]["automaticDeletion"])

        unsupported = replace(
            self.request(),
            destination_root=self.root / "unknown-extension",
            private_state={
                "schemaVersion": 1,
                "unknownExtensions": ["animemo.memory/future"],
            },
        )
        with self.assertRaises(migration.MigrationUnsupportedError):
            self.create(request=unsupported)

    def test_preflight_rejects_overlap_updater_mismatch_and_media_ambiguity(
        self,
    ) -> None:
        overlap = replace(self.request(), destination_root=self.root)
        with self.assertRaisesRegex(
            migration.MigrationOperationalError,
            "MIGRATION_DESTINATION_SOURCE_OVERLAP",
        ):
            self.create(request=overlap)
        self.assertEqual(self.plugin_source.read_bytes(), self.plugin_bytes)

        updater = dict(self.request().updater_state)
        updater["current"] = {"commit": "b" * 40}
        mismatch = replace(
            self.request(),
            destination_root=self.root / "updater-mismatch",
            updater_state=updater,
        )
        with self.assertRaisesRegex(
            migration.MigrationOperationalError, "MIGRATION_UPDATER_STATE_INVALID"
        ):
            self.create(request=mismatch)

        updater_extension = dict(self.request().updater_state)
        updater_extension["rawPlan"] = {"state": "must-not-copy"}
        with self.assertRaisesRegex(
            migration.MigrationUnsupportedError,
            "MIGRATION_UPDATER_STATE_EXTENSION_UNSUPPORTED",
        ):
            self.create(
                request=replace(
                    self.request(),
                    destination_root=self.root / "updater-extension",
                    updater_state=updater_extension,
                )
            )

        unsafe_private = dict(self.request().private_state)
        unsafe_private["unknownFilesCopied"] = True
        with self.assertRaisesRegex(
            migration.MigrationOperationalError,
            "MIGRATION_PRIVATE_STATE_INVALID",
        ):
            self.create(
                request=replace(
                    self.request(),
                    destination_root=self.root / "private-invalid",
                    private_state=unsafe_private,
                )
            )

        incomplete_dispositions = migration.MigrationConfiguration(
            mode=migration.ConfigurationMode.PRESERVE,
            non_secret={},
            dispositions={"publicOrigin": "PRESERVE", "listen": "PRESERVE"},
        )
        with self.assertRaisesRegex(
            migration.MigrationOperationalError,
            "MIGRATION_CONFIGURATION_INVALID",
        ):
            self.create(
                request=replace(
                    self.request(),
                    destination_root=self.root / "config-dispositions-missing",
                    configuration=incomplete_dispositions,
                )
            )

        duplicate = replace(
            self.request().local_media[0], media_id="media-local-duplicate"
        )
        ambiguous = replace(
            self.request(),
            destination_root=self.root / "ambiguous-media",
            local_media=(*self.request().local_media, duplicate),
        )
        with self.assertRaisesRegex(
            migration.MigrationCorruptError, "MIGRATION_MEDIA_OWNERSHIP_AMBIGUOUS"
        ):
            self.create(request=ambiguous)

    def test_target_proofs_and_callback_failures_are_redacted(self) -> None:
        result, _ = self.create()
        verification = migration.verify_migration_bundle(result.path)
        decision = self.decision(verification)
        cases = (
            self.inspection(apis=frozenset()),
            self.inspection(identities={}),
            self.inspection(
                identities={
                    "r2-primary": migration.R2PhysicalIdentity(
                        "https://r2.example.invalid", "account-2", "animemo-media"
                    )
                }
            ),
            self.inspection(release_identity={"commit": "b" * 40}),
        )
        for inspection in cases:
            with self.subTest(inspection=inspection):
                target = FakeTarget(inspection)
                with self.assertRaises(migration.MigrationUnsupportedError):
                    migration.consume_migration_bundle(
                        result.path,
                        external_secret=self.external_key,
                        compatibility=decision,
                        target=target,
                    )
                self.assertEqual(target.events, ["inspect"])

        unavailable_authority = FakeTarget(self.inspection(release_identity=None))
        with self.assertRaisesRegex(
            migration.MigrationOperationalError,
            "MIGRATION_TARGET_AUTHORITY_UNAVAILABLE",
        ):
            migration.consume_migration_bundle(
                result.path,
                external_secret=self.external_key,
                compatibility=decision,
                target=unavailable_authority,
            )
        self.assertEqual(unavailable_authority.events, ["inspect"])

        class FailingInspection(FakeTarget):
            def inspect(self) -> migration.TargetInspection:
                raise RuntimeError("credential-like callback detail")

        with self.assertRaisesRegex(
            migration.MigrationOperationalError,
            "MIGRATION_TARGET_INSPECTION_FAILED",
        ) as captured:
            migration.consume_migration_bundle(
                result.path,
                external_secret=self.external_key,
                compatibility=decision,
                target=FailingInspection(self.inspection()),
            )
        self.assertNotIn("credential-like", str(captured.exception))

        rollback_failure = FakeTarget(
            self.inspection(), fail_at="begin", rollback_fails=True
        )
        with self.assertRaises(migration.MigrationRecoveryRequiredError) as captured:
            migration.consume_migration_bundle(
                result.path,
                external_secret=self.external_key,
                compatibility=decision,
                target=rollback_failure,
            )
        self.assertNotIn("callback detail", str(captured.exception))
        self.assertEqual(captured.exception.evidence.failed_step, "rollback")
        self.assertEqual(
            rollback_failure.events, ["inspect", "begin", "rollback", "recovery"]
        )

        rollback_evidence_failure = FakeTarget(
            self.inspection(), fail_at="begin", rollback_fails=True
        )

        def fail_recovery_record(
            evidence: migration.MigrationRecoveryEvidence,
        ) -> None:
            rollback_evidence_failure.recovery_evidence = evidence
            raise RuntimeError("recovery record private detail")

        rollback_evidence_failure.record_recovery_required = fail_recovery_record
        with self.assertRaises(migration.MigrationRecoveryEvidenceError) as captured:
            migration.consume_migration_bundle(
                result.path,
                external_secret=self.external_key,
                compatibility=decision,
                target=rollback_evidence_failure,
            )
        self.assertEqual(captured.exception.evidence.failed_step, "rollback")
        self.assertNotIn("private detail", str(captured.exception))

    def test_post_database_failures_never_auto_rollback(self) -> None:
        result, _ = self.create()
        verification = migration.verify_migration_bundle(result.path)
        decision = self.decision(verification)
        cases = (
            ("database", "database"),
            ("plugin", "plugin"),
            ("validate-false", "validate"),
            ("publish", "publish"),
        )
        for fail_at, expected_step in cases:
            with self.subTest(fail_at=fail_at):
                target = FakeTarget(self.inspection(), fail_at=fail_at)
                with self.assertRaises(
                    migration.MigrationRecoveryRequiredError
                ) as captured:
                    migration.consume_migration_bundle(
                        result.path,
                        external_secret=self.external_key,
                        compatibility=decision,
                        target=target,
                    )
                evidence = captured.exception.evidence
                self.assertEqual(evidence.failed_step, expected_step)
                self.assertEqual(evidence.state, "RECOVERY_REQUIRED")
                self.assertFalse(evidence.target_active)
                self.assertFalse(evidence.source_deleted)
                self.assertFalse(evidence.automatic_rollback)
                self.assertEqual(target.recovery_evidence, evidence)
                self.assertIn("recovery", target.events)
                self.assertNotIn("rollback", target.events)

        evidence_failure = FakeTarget(self.inspection(), fail_at="recovery")
        original_event = evidence_failure._event

        def fail_database_then_recovery(name: str) -> None:
            if name in {"database", "recovery"}:
                evidence_failure.events.append(name)
                raise RuntimeError("recovery adapter private detail")
            original_event(name)

        evidence_failure._event = fail_database_then_recovery
        with self.assertRaises(migration.MigrationRecoveryEvidenceError) as captured:
            migration.consume_migration_bundle(
                result.path,
                external_secret=self.external_key,
                compatibility=decision,
                target=evidence_failure,
            )
        self.assertNotIn("private detail", str(captured.exception))
        self.assertEqual(captured.exception.evidence.failed_step, "database")
        self.assertNotIn("rollback", evidence_failure.events)

    def test_repr_errors_and_reports_are_redacted(self) -> None:
        request = self.request()
        self.assertEqual(repr(request), "<MigrationBundleRequest redacted>")
        self.assertNotIn("postgresql://", repr(request))
        error = migration.MigrationOperationalError("MIGRATION_TEST_FAILURE")
        self.assertEqual(str(error), "MIGRATION_TEST_FAILURE")
        self.assertNotIn("D" * 16, repr(error))

        result, _ = self.create()
        for relative in (
            migration.MANIFEST_NAME,
            migration.CHECKSUMS_NAME,
            migration.CONFIG_MEMBER,
            migration.PRIVATE_MANIFEST_MEMBER,
            migration.UPDATER_STATE_MEMBER,
        ):
            encoded = (result.path / relative).read_bytes()
            self.assertNotIn(b"postgresql://", encoded)
            self.assertNotIn(b"D" * 16, encoded)


if __name__ == "__main__":
    unittest.main()
