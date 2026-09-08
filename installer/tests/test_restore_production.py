from __future__ import annotations

import json
import copy
import os
import tempfile
import unittest
from datetime import datetime, timezone
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from durability import backup, restore, secret_envelope
from durability.canonical import canonical_json_bytes
from durability.compatibility import CompatibilityOutcome, Dimension
from durability.instance import instance_namespace
from durability.platform import (
    REQUIRED_CAPABILITIES, REQUIRED_REHEARSALS,
    canonical_platform_qualification_bytes, finalize_platform_qualification,
)
from durability.managed_config import LocalManagedConfigStore
from installer import restore_production
from installer.operations import RestoreOperationJournal
from installer.production import ProductionManagedConfigurationPort
from installer.restore_production import (
    ProductionRestoreMutation,
    ProductionRestoreRuntimePort,
    _read_protected_file,
)
from installer.runtime import (
    InstallerError,
    InstallOutcome,
    ListenRequest,
    PlatformEvidence,
    ReleaseEvidence,
    RestoreProtectionKind,
    RestoreProtectionRequest,
    TargetClass,
    TargetEvidence,
)
from updater.errors import RecoveryRequired
from updater.state import OperationStore


def digest(character: str) -> str:
    return "sha256:" + character * 64


class _PgDump:
    def run(self, database_url, raw_output, *, executable, timeout):
        del database_url, executable, timeout
        raw_output.write_bytes(b"-- PostgreSQL database dump\nSELECT 1;\n")
        return "pg_dump (PostgreSQL) 16.4"


class _Materials:
    """Unit authority/platform fixtures; no live qualification is asserted."""

    def __init__(self, manifest, root):
        self.manifest = manifest
        self.deployment_contract = {
            "schemaVersion": 2, "profile": "v1.1-instance-scoped", "platform": "linux/amd64",
            "files": [{"path": "deploy/docker-compose.yml", "sha256": digest("d")},
                      {"path": "updater/docker-compose.runtime.yml", "sha256": digest("e")}],
            "materials": [{"path": "deploy/updater/animemo-updater", "sha256": digest("f"), "size": 10, "mode": 0o755}],
        }
        self.qualification = finalize_platform_qualification({
            "schema": "animemo.platform-qualification/v1", "profile": "v1.1-standard-linux-amd64",
            "candidateSha": manifest["release"]["commit"],
            "workflow": {"path": ".github/workflows/platform-qualification.yml", "ref": "refs/heads/main", "sha": manifest["release"]["commit"]},
            "run": {"id": "1", "attempt": 1}, "observedAt": "2026-09-08T00:00:00Z",
            "host": {"os": "linux", "architecture": "amd64", "distributionId": "ubuntu", "distributionVersion": "24.04",
                     "kernel": "unit-fixture", "systemdVersion": "unit-fixture", "dockerVersion": "unit-fixture", "composeVersion": "unit-fixture"},
            "databasePath": {"dumpFormat": "plain", "sourceServerMajor": 16, "pgDumpMajor": 16, "psqlMajor": 16, "targetServerMajor": 16},
            "imageDigests": {role: self.image(role) for role in ("postgres", "redis")},
            "capabilities": {name: True for name in REQUIRED_CAPABILITIES},
            "rehearsals": {name: "PASS" for name in REQUIRED_REHEARSALS},
        })
        root.mkdir(parents=True)
        self.qualification_path = root / "platform-qualification.json"
        self.qualification_path.write_bytes(canonical_platform_qualification_bytes(self.qualification))

    def image(self, role):
        image = self.manifest["images"][role]
        return image["repository"] + "@" + image["digest"]

    def material(self, relative):
        if relative != "release/platform-qualification.json":
            raise AssertionError(relative)
        return self.qualification_path


class _Releases:
    def __init__(self, manifest, root, evidence):
        self.materials = _Materials(manifest, root)
        self.releases = {evidence.version: (evidence, self.materials)}
        self.reads = []

    def materials_for(self, evidence):
        registered, materials = self.releases[evidence.version]
        if registered != evidence:
            raise InstallerError("INSTALL_RELEASE_CHANGED", outcome=InstallOutcome.VALIDATION_FAILED)
        return materials

    def read_exact(self, version, *, refresh):
        self.reads.append((version, refresh))
        if version not in self.releases:
            raise InstallerError("INSTALL_RELEASE_VERIFICATION_FAILED", outcome=InstallOutcome.VALIDATION_FAILED)
        return self.releases[version][0]


class _Fresh:
    namespace = instance_namespace()


class _LauncherMaterials:
    def __init__(self, launcher: Path) -> None:
        self.launcher = launcher

    def material(self, path: str) -> Path:
        if path != "deploy/updater/animemo-updater":
            raise AssertionError("unexpected restore material")
        return self.launcher


class _LauncherReleases:
    def __init__(self, materials: _LauncherMaterials) -> None:
        self.materials = materials

    def materials_for(self, _release):
        return self.materials


class _LauncherFresh:
    def __init__(self, materials: _LauncherMaterials) -> None:
        self.releases = _LauncherReleases(materials)
        self.namespace = instance_namespace()


class ProductionRestoreUpdaterTests(unittest.TestCase):
    def test_stage_uses_the_canonical_installed_updater_launcher(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            expected = root / "verified-launcher"
            expected.write_bytes(b"canonical launcher\n")
            canonical = root / "opt" / "animemo-updater" / "launcher"
            canonical.parent.mkdir(parents=True)
            canonical.write_bytes(expected.read_bytes())
            os.chmod(canonical, 0o755)
            legacy = canonical.with_name("animemo-updater")
            real_path = Path

            def mapped_path(value):
                if str(value) == "/opt/animemo-updater/launcher":
                    return canonical
                if str(value) == "/opt/animemo-updater/animemo-updater":
                    return legacy
                return real_path(value)

            mutation = ProductionRestoreMutation(
                fresh=_LauncherFresh(_LauncherMaterials(expected)),
                configuration=SimpleNamespace(),
                installer_id="a" * 32,
            )
            mutation.installation_plan = SimpleNamespace(release=object())
            with (
                mock.patch.object(restore_production, "Path", side_effect=mapped_path),
                mock.patch.object(
                    restore_production.stat,
                    "S_IMODE",
                    return_value=0o755,
                ),
                mock.patch.object(
                    restore_production,
                    "ReleaseSlots",
                    return_value=SimpleNamespace(
                        read=lambda: {"current": None, "previous": None}
                    ),
                ),
            ):
                mutation.stage_updater()

            self.assertTrue(mutation.adoption_ready)
            self.assertFalse(legacy.exists())


class ProductionRestorePlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.config_root = self.root / "config"
        self.runtime_root = self.root / "runtime"
        self.config_root.mkdir(mode=0o700)
        self.runtime_root.mkdir(mode=0o750)
        store = LocalManagedConfigStore(
            config_root=self.config_root,
            runtime_root=self.runtime_root,
        )
        self.configuration = ProductionManagedConfigurationPort(store)
        self.release = ReleaseEvidence(
            version="v1.1.0-rc.1",
            channel="rc",
            commit="a" * 40,
            manifest_digest=digest("1"),
            material_identity_digest=digest("2"),
            deployment_identity_digest=digest("3"),
            deployment_profile="v1.1-instance-scoped",
            platform_profile="v1.1-standard-linux-amd64",
        )
        self.release_manifest = {
            "release": {
                "version": self.release.version,
                "channel": self.release.channel,
                "commit": self.release.commit,
            },
            "compatibility": {
                "database": {
                    "contract": "animemo-db-v1",
                    "appAccepts": ["animemo-db-v1"],
                    "migration": {"required": False, "policy": "none"},
                },
                "configuration": {
                    "contract": "animemo.configuration/v1",
                    "appAccepts": ["animemo.configuration/v1"],
                },
                "pluginSdk": {"supportedApis": [2]},
            },
            "minimumUpdaterVersion": "1.0.1",
            "images": {
                "postgres": {"repository": "docker.io/library/postgres", "digest": digest("a")},
                "redis": {"repository": "docker.io/library/redis", "digest": digest("b")},
            },
        }
        self.releases = _Releases(self.release_manifest, self.root / "materials", self.release)
        self.port = ProductionRestoreRuntimePort(
            releases=self.releases,
            configuration=self.configuration,
            fresh=_Fresh(),
        )
        self.platform = PlatformEvidence(
            compatible=True,
            profile="v1.1-standard-linux-amd64",
            evidence_digest=self.releases.materials.qualification.evidence_digest,
            reason_code="PLATFORM_QUALIFIED",
        )
        self.target = TargetEvidence(TargetClass.ABSENT, digest("5"))

    def _backup(self, *, external_key=None, entries=()):
        sources = {}
        for logical_root in restore.CANONICAL_BACKUP_ROOTS:
            source = self.root / "sources" / logical_root.replace("/", "-")
            source.mkdir(parents=True)
            if logical_root != "filesystem/private":
                (source / "metadata.json").write_bytes(
                    canonical_json_bytes({"root": logical_root}) + b"\n"
                )
            sources[logical_root] = source

        secret = None
        if external_key is not None:
            def envelope_factory(binding):
                return secret_envelope.create_secret_envelope(
                    external_secret=external_key,
                    artifact_type="backup",
                    artifact_id=binding.artifact_id,
                    artifact_binding_record=binding.artifact_binding_record,
                    source_instance_id=binding.source_instance_id,
                    secret_entries=entries,
                ).to_bytes()

            secret = backup.SecretSource(
                mode="envelope",
                metadata={"suiteId": secret_envelope.SUITE_ID},
                envelope_factory=envelope_factory,
            )
        request = backup.BackupRequest(
            destination_root=self.root / "backups",
            database_url="postgresql://isolated.invalid/source",
            source=backup.BackupSourceIdentity(
                instance_id="11111111-2222-4333-8444-555555555555",
                source_locator_digest=digest("6"),
                release={
                    "version": self.release.version,
                    "commit": self.release.commit,
                },
                deployment_contract={
                    "schemaVersion": 2,
                    "digest": self.release.deployment_identity_digest,
                },
                database_contract={
                    "id": "animemo-db-v1",
                    "serverMajor": 16,
                },
                configuration_contract={"id": "animemo.configuration/v1"},
                plugin_sdk_apis=("animemo.plugin/v2",),
            ),
            filesystem_sources=tuple(
                backup.FilesystemSource(logical_root=name, source=path)
                for name, path in sources.items()
            ),
            secret=secret,
            local_media_references={},
            producer={"name": "installer-test", "version": "1"},
            platform={"os": "linux", "architecture": "amd64"},
            quiescence={"method": "isolated-test"},
        )
        moments = iter(
            (
                datetime(2026, 8, 16, 1, 2, 3, tzinfo=timezone.utc),
                datetime(2026, 8, 16, 1, 2, 4, tzinfo=timezone.utc),
            )
        )
        return backup.create_backup(
            request,
            pg_dump_runner=_PgDump(),
            clock=lambda: next(moments),
        ).path

    def _forward_target(self, label="forward", *, version="v1.1.0-rc.2"):
        target = replace(self.release, version=version, commit="b" * 40,
                         manifest_digest=digest("7"), deployment_identity_digest=digest("8"),
                         material_identity_digest=digest("9"))
        manifest = copy.deepcopy(self.release_manifest)
        manifest["release"] = {"version": target.version, "commit": target.commit, "channel": target.channel}
        manifest["compatibility"]["database"] = {
            "contract": "animemo-db-v2", "appAccepts": ["animemo-db-v1", "animemo-db-v2"],
            "migration": {"required": True, "policy": "additive-backward-compatible"},
        }
        materials = _Materials(manifest, self.root / ("target-materials-" + label))
        self.releases.releases[target.version] = (target, materials)
        platform = replace(self.platform, evidence_digest=materials.qualification.evidence_digest)
        return target, materials, platform

    def _prepare_target(self, artifact, target, platform, *, operation="d" * 32):
        return self.port.prepare(operation_id=operation, backup_root=artifact,
                                 release=target, target=self.target, platform=platform,
                                 protection=RestoreProtectionRequest(RestoreProtectionKind.NONE))

    def test_forward_plan_retains_distinct_source_and_target_identities(self):
        artifact = self._backup()
        original = (artifact / backup.MANIFEST_NAME).read_bytes()
        target, materials, platform = self._forward_target()
        evidence = self._prepare_target(artifact, target, platform)
        plan = self.port._contexts[evidence.operation_id].restore_plan
        self.assertEqual(plan.decision.outcome, CompatibilityOutcome.REQUIRES_UPGRADE)
        exact = next(item for item in plan.decision.evaluated_dimensions if item.name is Dimension.EXACT_RELEASE_IDENTITY)
        self.assertEqual(exact.source["manifestDigest"], self.release.manifest_digest)
        self.assertEqual(exact.target["manifestDigest"], target.manifest_digest)
        self.assertNotEqual(exact.source["deploymentDigest"], exact.target["deploymentDigest"])
        self.assertEqual(len(plan.decision.actions), 1)
        self.assertEqual(plan.decision.actions[0].output_identity["databaseContract"], "animemo-db-v2")
        self.assertTrue(self.releases.reads)
        self.assertTrue(all(refresh for _version, refresh in self.releases.reads))
        self.assertEqual((artifact / backup.MANIFEST_NAME).read_bytes(), original)
        self.port.revalidate(evidence)
        materials.manifest["release"]["commit"] = "c" * 40
        with self.assertRaises(InstallerError):
            self.port.revalidate(evidence)

    def test_forward_restore_rejects_unsupported_transitions_before_mutation(self):
        artifact = self._backup()
        changes = {
            "pg-image": lambda m: m.manifest["images"]["postgres"].update(digest=digest("c")),
            "redis-image": lambda m: m.manifest["images"]["redis"].update(digest=digest("c")),
            "layout-profile": lambda m: m.deployment_contract.update(profile="unsupported-layout"),
            "compose": lambda m: m.deployment_contract["files"][0].update(sha256=digest("c")),
            "launcher": lambda m: m.deployment_contract["materials"][0].update(sha256=digest("c")),
            "db-accepts": lambda m: m.manifest["compatibility"]["database"].update(appAccepts=["animemo-db-v2"]),
            "migration-required": lambda m: m.manifest["compatibility"]["database"]["migration"].update(required=False),
            "migration-none": lambda m: m.manifest["compatibility"]["database"]["migration"].update(policy="none"),
            "migration-breaking": lambda m: m.manifest["compatibility"]["database"]["migration"].update(policy="breaking-blocked"),
            "db-multihop": lambda m: m.manifest["compatibility"]["database"].update(contract="animemo-db-v3"),
            "config": lambda m: m.manifest["compatibility"]["configuration"].update(contract="new-config"),
            "plugin": lambda m: m.manifest["compatibility"]["pluginSdk"].update(supportedApis=[]),
            "qualification-missing": lambda m: m.qualification_path.unlink(),
        }
        for index, (name, change) in enumerate(changes.items(), start=1):
            with self.subTest(name=name):
                target, materials, platform = self._forward_target(name)
                change(materials)
                with self.assertRaises(InstallerError):
                    self._prepare_target(artifact, target, platform, operation=f"{index:032x}")
        target, _materials, platform = self._forward_target("downgrade", version="v1.0.9-rc.1")
        with self.assertRaises(InstallerError):
            self._prepare_target(artifact, target, platform)

    def test_exact_source_unavailable_or_changed_never_falls_back_to_target(self):
        artifact = self._backup()
        target, _materials, platform = self._forward_target()
        original = self.releases.releases.pop(self.release.version)
        with self.assertRaisesRegex(InstallerError, "RESTORE_EXACT_RELEASE_UNAVAILABLE"):
            self._prepare_target(artifact, target, platform)
        self.releases.releases[self.release.version] = (replace(self.release, commit="c" * 40), original[1])
        with self.assertRaisesRegex(InstallerError, "RESTORE_EXACT_RELEASE_UNAVAILABLE"):
            self._prepare_target(artifact, target, platform)

    def test_same_version_different_identity_is_rejected(self):
        artifact = self._backup()
        target, _materials, platform = self._forward_target(version=self.release.version)
        with self.assertRaisesRegex(InstallerError, "RESTORE_EXACT_RELEASE_UNAVAILABLE"):
            self._prepare_target(artifact, target, platform)

    def test_restore_database_path_requires_the_observed_dump_major(self):
        artifact = self._backup()
        target, materials, platform = self._forward_target()
        port = restore_production.ProductionRestoreRelease(self.releases, target)
        checker = restore_production.ProductionRestoreCompatibility(selected=target, platform=platform,
                                                                    manifest=materials.manifest, release_port=port)
        manifest = json.loads((artifact / backup.MANIFEST_NAME).read_bytes())
        self.assertTrue(checker._database_path(manifest, materials)[0])
        for field, value in (("serverMajor", 17), ("toolVersion", "pg_dump (PostgreSQL) 17.1"), ("toolVersion", "unverified tool")):
            changed = copy.deepcopy(manifest)
            changed["database"][field] = value
            self.assertFalse(checker._database_path(changed, materials)[0])

    def test_none_protection_accepts_distinct_target_instance_identity(self) -> None:
        artifact = self._backup()
        evidence = self.port.prepare(
            operation_id="a" * 32,
            backup_root=artifact,
            release=self.release,
            target=self.target,
            platform=self.platform,
            protection=RestoreProtectionRequest(RestoreProtectionKind.NONE),
        )
        target_instance_id = "99999999-8888-4777-8666-555555555555"
        config = self.configuration.plan(
            instance_id=target_instance_id,
            public_origin="https://anime.example",
            listen=ListenRequest(),
            insecure_http_accepted=False,
        )

        bound = self.port.bind_configuration(evidence, config)

        self.assertEqual(
            bound.instance_id,
            target_instance_id,
        )
        self.assertEqual(bound.non_secret_identity_digest, config.non_secret_identity_digest)

    def test_envelope_binding_preserves_cek_and_django_secret(self) -> None:
        key = secret_envelope.OneTimeKey.from_bytes(b"k" * 32)
        cek = "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA="
        django = "d" * 64
        artifact = self._backup(
            external_key=key,
            entries=(
                secret_envelope.SecretEntry.preserve(
                    "CREDENTIAL_ENCRYPTION_KEY", cek.encode()
                ),
                secret_envelope.SecretEntry.preserve(
                    "DJANGO_SECRET_KEY", django.encode()
                ),
            ),
        )
        key_path = self.root / "restore.key"
        key_path.write_bytes(key.export())
        key_path.chmod(0o600)
        evidence = self.port.prepare(
            operation_id="b" * 32,
            backup_root=artifact,
            release=self.release,
            target=self.target,
            platform=self.platform,
            protection=RestoreProtectionRequest(
                RestoreProtectionKind.ONE_TIME_KEY_FILE,
                path=key_path,
            ),
        )
        planned = self.configuration.plan(
            instance_id="99999999-8888-4777-8666-555555555555",
            public_origin="https://anime.example",
            listen=ListenRequest(),
            insecure_http_accepted=False,
        )

        bound = self.port.bind_configuration(evidence, planned)
        config = self.configuration.config_for(bound)

        self.assertEqual(config.application.credential_encryption_key, cek)
        self.assertEqual(config.application.django_secret_key, django)
        rendered = json.dumps(bound.as_dict(), sort_keys=True)
        self.assertNotIn(cek, rendered)
        self.assertNotIn(django, rendered)

    def test_interrupted_restore_blocks_updater_until_manual_recovery(self) -> None:
        state = self.root / "state"
        state.mkdir(mode=0o700)
        operation_id = "c" * 32
        journal = RestoreOperationJournal(state)
        journal.begin(
            operation_id,
            SimpleNamespace(
                operation_id="cccccccc-cccc-4ccc-8ccc-cccccccccccc",
                backup_id="dddddddd-dddd-4ddd-8ddd-dddddddddddd",
                instance_id="11111111-2222-4333-8444-555555555555",
                plan_digest=digest("9"),
            ),
        )
        store = OperationStore(state)

        self.assertEqual(store.recover_incomplete(), [operation_id])
        with self.assertRaises(RecoveryRequired):
            store.require_recovery_clear()

    def test_protected_secret_file_rejects_hard_link(self) -> None:
        source = self.root / "source.key"
        linked = self.root / "linked.key"
        source.write_bytes(b"k" * 32)
        source.chmod(0o600)
        os.link(source, linked)

        with self.assertRaises(InstallerError) as captured:
            _read_protected_file(linked, limit=32)

        self.assertEqual(
            captured.exception.code,
            "INSTALL_RESTORE_PROTECTION_FILE_UNSAFE",
        )

    def test_protected_secret_file_rejects_symbolic_link(self) -> None:
        source = self.root / "source.key"
        linked = self.root / "linked.key"
        source.write_bytes(b"k" * 32)
        source.chmod(0o600)
        try:
            linked.symlink_to(source)
        except OSError as error:
            self.skipTest(f"symbolic links are unavailable: {error}")

        with self.assertRaises(InstallerError) as captured:
            _read_protected_file(linked, limit=32)

        self.assertEqual(
            captured.exception.code,
            "INSTALL_RESTORE_PROTECTION_FILE_UNSAFE",
        )


if __name__ == "__main__":
    unittest.main()
