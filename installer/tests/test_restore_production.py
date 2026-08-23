from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from durability import backup, restore, secret_envelope
from durability.canonical import canonical_json_bytes
from durability.instance import instance_namespace
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
    def __init__(self, manifest):
        self.manifest = manifest


class _Releases:
    def __init__(self, manifest):
        self.materials = _Materials(manifest)

    def materials_for(self, evidence):
        del evidence
        return self.materials


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
                    "contract": "animemo.database/v1",
                    "appAccepts": ["animemo.database/v1"],
                    "migration": {"required": False, "policy": "none"},
                },
                "configuration": {
                    "contract": "animemo.configuration/v1",
                    "appAccepts": ["animemo.configuration/v1"],
                },
                "pluginSdk": {"supportedApis": [2]},
            },
        }
        self.port = ProductionRestoreRuntimePort(
            releases=_Releases(self.release_manifest),
            configuration=self.configuration,
            fresh=_Fresh(),
        )
        self.platform = PlatformEvidence(
            compatible=True,
            profile="v1.1-standard-linux-amd64",
            evidence_digest=digest("4"),
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
                    "id": "animemo.database/v1",
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
