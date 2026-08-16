from __future__ import annotations

import gzip
import hashlib
import importlib.util
import json
import os
import tempfile
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

if importlib.util.find_spec("cryptography") is None:
    raise unittest.SkipTest("requires durability/requirements.txt")

from durability import backup, secret_envelope
from durability.canonical import canonical_json_bytes


class FakePgDump:
    def __init__(
        self, payload: bytes = b"-- PostgreSQL database dump\nSELECT 1;\n"
    ) -> None:
        self.payload = payload
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


class FailingPgDump:
    def run(
        self, database_url: str, raw_output: Path, *, executable: str, timeout: int
    ) -> str:
        raise backup.BackupError("PG_DUMP_FAILED", "logical database dump failed")


class BackupRuntimeTests(unittest.TestCase):
    maxDiff = None

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.destination = self.root / "backups"
        self.sources: dict[str, Path] = {}
        for logical_root in (
            "filesystem/config",
            "filesystem/plugins/cas",
            "filesystem/plugins/durable",
            "filesystem/media",
            "filesystem/private",
            "updater-state",
        ):
            source = self.root / "sources" / logical_root.replace("/", "-")
            source.mkdir(parents=True)
            if logical_root in {"filesystem/config", "updater-state"}:
                (source / "metadata.json").write_bytes(
                    canonical_json_bytes({"profile": logical_root}) + b"\n"
                )
            elif logical_root != "filesystem/private":
                (source / "payload.bin").write_bytes(f"payload:{logical_root}".encode())
            self.sources[logical_root] = source
        (self.sources["filesystem/media"] / "poster.jpg").write_bytes(b"poster")
        self.secret_reference = self.root / "secret-reference.json"
        self.secret_reference.write_bytes(b'{"provider":"test-kms","version":"v1"}\n')
        self.backup_id = uuid.UUID("12345678-1234-5678-9234-567812345678")
        self.started = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
        self.completed = datetime(2026, 1, 2, 3, 4, 6, tzinfo=timezone.utc)

    def request(self) -> backup.BackupRequest:
        poster = self.sources["filesystem/media"] / "poster.jpg"
        return backup.BackupRequest(
            destination_root=self.destination,
            database_url="postgresql://isolated-test.invalid/animemo",
            source=backup.BackupSourceIdentity(
                instance_id="11111111-2222-4333-8444-555555555555",
                source_locator_digest="sha256:" + "1" * 64,
                release={
                    "version": "1.1.0",
                    "channel": "stable",
                    "commit": "a" * 40,
                    "manifestDigest": "sha256:" + "2" * 64,
                    "apiImageDigest": "sha256:" + "3" * 64,
                    "webImageDigest": "sha256:" + "4" * 64,
                },
                deployment_contract={
                    "schemaVersion": 1,
                    "digest": "sha256:" + "5" * 64,
                },
                database_contract={"id": "animemo.database/v1", "serverMajor": 16},
                configuration_contract={"id": "animemo.configuration/v1"},
                plugin_sdk_apis=("animemo.plugin/v2",),
            ),
            filesystem_sources=tuple(
                backup.FilesystemSource(logical_root=name, source=source)
                for name, source in self.sources.items()
            ),
            secret=backup.SecretSource(mode="reference", source=self.secret_reference),
            local_media_references={
                "poster.jpg": "sha256:"
                + hashlib.sha256(poster.read_bytes()).hexdigest(),
            },
            r2_references=(
                backup.R2Reference(
                    backend_type="r2",
                    endpoint_identity="account.example.invalid",
                    bucket="animemo-media",
                    object_keys=("posters/one.webp",),
                ),
            ),
            producer={"name": "animemo-durability", "version": "1.1.0"},
            platform={"os": "linux", "architecture": "amd64"},
            quiescence={"method": "isolated-test-write-barrier"},
        )

    def create(self, *, runner: object | None = None) -> backup.BackupResult:
        moments = iter((self.started, self.completed))
        return backup.create_backup(
            self.request(),
            pg_dump_runner=runner or FakePgDump(),
            backup_id=self.backup_id,
            clock=lambda: next(moments),
        )

    def test_create_verify_and_discover_canonical_v1(self) -> None:
        runner = FakePgDump()
        result = self.create(runner=runner)

        self.assertEqual(result.backup_id, str(self.backup_id))
        self.assertEqual(
            result.path.name,
            "backup-20260102T030405Z-12345678-1234-5678-9234-567812345678",
        )
        self.assertEqual(runner.calls, [("DATABASE_URL_PRESENT", "pg_dump", 600)])
        self.assertEqual(
            backup.list_finalized_backups(self.destination), (result.path,)
        )
        self.assertFalse(
            any(
                path.name.startswith(backup.STAGING_PREFIX)
                for path in self.destination.iterdir()
            )
        )

        verification = backup.verify_backup(result.path)
        artifact_identity = verification.as_compatibility_artifact()
        self.assertEqual(artifact_identity.format_identity, backup.FORMAT)
        self.assertEqual(artifact_identity.format_version, backup.SCHEMA_VERSION)
        self.assertEqual(artifact_identity.artifact_id, str(self.backup_id))
        self.assertEqual(
            artifact_identity.manifest_digest, verification.manifest_digest
        )
        self.assertEqual(verification.backup_id, str(self.backup_id))
        self.assertEqual(verification.compatibility_artifact["format"], backup.FORMAT)
        self.assertEqual(verification.compatibility_artifact["schemaVersion"], 1)
        self.assertEqual(
            verification.compatibility_artifact["artifactId"], str(self.backup_id)
        )

        self.assertEqual(
            verification.compatibility_artifact["manifestDigest"],
            result.manifest_digest,
        )

        manifest_bytes = (result.path / backup.MANIFEST_NAME).read_bytes()
        manifest = json.loads(manifest_bytes)
        self.assertEqual(manifest["format"], "animemo-instance-backup")
        self.assertEqual(manifest["schemaVersion"], 1)
        self.assertEqual(manifest["lifecycle"], "FINALIZED")
        self.assertEqual(
            manifest["database"]["dumpProfile"]["argv"], list(backup.PG_DUMP_ARGUMENTS)
        )
        self.assertEqual(
            manifest["database"]["toolVersion"], "pg_dump (PostgreSQL) 16.4"
        )
        self.assertEqual(manifest["secrets"]["mode"], "reference")
        self.assertNotIn("postgresql://", manifest_bytes.decode("utf-8"))
        self.assertEqual(
            (result.path / "secrets" / "secret-reference.json").read_bytes(),
            self.secret_reference.read_bytes(),
        )
        with gzip.open(result.path / backup.DATABASE_MEMBER, "rb") as stream:
            self.assertEqual(stream.read(), runner.payload)

    def test_shared_logical_postgres_capture_is_artifact_neutral(self) -> None:
        staging = self.root / "shared-postgres-staging"
        staging.mkdir(mode=0o700)
        runner = FakePgDump()

        captured = backup.capture_logical_postgres(
            "postgresql://isolated-test.invalid/animemo",
            staging,
            server_major=16,
            runner=runner,
        )

        self.assertEqual(runner.calls, [("DATABASE_URL_PRESENT", "pg_dump", 600)])
        self.assertEqual(captured["path"], backup.DATABASE_MEMBER)
        self.assertEqual(captured["serverMajor"], 16)
        self.assertEqual(
            gzip.decompress((staging / backup.DATABASE_MEMBER).read_bytes()),
            runner.payload,
        )
        self.assertFalse((staging / ".database.sql.raw").exists())

    def test_checksums_are_sorted_complete_and_reproducible(self) -> None:
        first = self.create()
        checksum_bytes = (first.path / backup.CHECKSUMS_NAME).read_bytes()
        lines = checksum_bytes.decode("utf-8").splitlines()
        paths = [line.split("  ", 1)[1] for line in lines]
        self.assertEqual(paths, sorted(paths, key=lambda value: value.encode("utf-8")))
        self.assertIn(backup.DATABASE_MEMBER, paths)
        self.assertIn("secrets/secret-reference.json", paths)
        self.assertNotIn(backup.MANIFEST_NAME, paths)
        self.assertNotIn(backup.CHECKSUMS_NAME, paths)

        second_root = self.root / "second"
        request = self.request()
        request = backup.BackupRequest(
            **{**request.__dict__, "destination_root": second_root}
        )
        moments = iter((self.started, self.completed))
        second = backup.create_backup(
            request,
            pg_dump_runner=FakePgDump(),
            backup_id=self.backup_id,
            clock=lambda: next(moments),
        )
        self.assertEqual(
            checksum_bytes, (second.path / backup.CHECKSUMS_NAME).read_bytes()
        )
        self.assertEqual(
            (first.path / backup.MANIFEST_NAME).read_bytes(),
            (second.path / backup.MANIFEST_NAME).read_bytes(),
        )

    def test_media_and_r2_coverage_never_copies_unknown_remote_objects(self) -> None:
        result = self.create()
        manifest = json.loads((result.path / backup.MANIFEST_NAME).read_bytes())
        local = manifest["media"]["local"]
        self.assertEqual(local["mode"], "captured")
        self.assertEqual(local["referenced"][0]["path"], "poster.jpg")
        self.assertTrue(
            any(
                item["path"] == "payload.bin" for item in local["preservedUnreferenced"]
            )
        )
        self.assertEqual(
            manifest["media"]["external"][0]["coverage"], "reference-dependent"
        )
        self.assertEqual(
            manifest["media"]["external"][0]["objectKeys"], ["posters/one.webp"]
        )
        self.assertFalse((result.path / "filesystem" / "media" / "posters").exists())
        self.assertEqual(
            manifest["media"]["unknownOrphanPolicy"], "PRESERVE_NEVER_DELETE"
        )

    def test_envelope_is_created_after_and_bound_to_canonical_artifact_record(
        self,
    ) -> None:
        external_secret = secret_envelope.OneTimeKey.from_bytes(b"e" * 32)
        request = self.request()

        def create_envelope(binding: backup.SecretEnvelopeBinding) -> bytes:
            return secret_envelope.create_secret_envelope(
                external_secret=external_secret,
                artifact_type="backup",
                artifact_id=binding.artifact_id,
                artifact_binding_record=binding.artifact_binding_record,
                source_instance_id=binding.source_instance_id,
                secret_entries=(
                    secret_envelope.SecretEntry.preserve(
                        "CREDENTIAL_ENCRYPTION_KEY", b"fake-cek"
                    ),
                ),
            ).to_bytes()

        request = backup.BackupRequest(
            **{
                **request.__dict__,
                "secret": backup.SecretSource(
                    mode="envelope",
                    metadata={"suiteId": secret_envelope.SUITE_ID},
                    envelope_factory=create_envelope,
                ),
            }
        )
        moments = iter((self.started, self.completed))
        result = backup.create_backup(
            request,
            pg_dump_runner=FakePgDump(),
            backup_id=self.backup_id,
            clock=lambda: next(moments),
        )
        manifest = json.loads((result.path / backup.MANIFEST_NAME).read_bytes())
        encoded = (result.path / secret_envelope.ENVELOPE_PATH).read_bytes()
        opened = secret_envelope.open_secret_envelope(
            encoded,
            external_secret=external_secret,
            expected_artifact_type="backup",
            expected_artifact_id=str(self.backup_id),
            expected_artifact_binding_record=manifest["artifactBindingRecord"],
            expected_source_instance_id=request.source.instance_id,
        )
        self.assertEqual(
            opened.artifact_binding_digest, manifest["artifactBindingDigest"]
        )
        backup.verify_backup(result.path)

    def test_verify_rejects_missing_database_and_checksum_mismatch(self) -> None:
        result = self.create()
        (result.path / backup.DATABASE_MEMBER).unlink()
        with self.assertRaisesRegex(backup.BackupError, "BACKUP_MEMBER_MISSING"):
            backup.verify_backup(result.path)

        other_root = self.root / "other"
        request = self.request()
        request = backup.BackupRequest(
            **{**request.__dict__, "destination_root": other_root}
        )
        moments = iter((self.started, self.completed))
        other = backup.create_backup(
            request,
            pg_dump_runner=FakePgDump(),
            backup_id=self.backup_id,
            clock=lambda: next(moments),
        )
        (other.path / "filesystem" / "media" / "poster.jpg").write_bytes(b"tampered")
        with self.assertRaisesRegex(backup.BackupError, "BACKUP_CHECKSUM_MISMATCH"):
            backup.verify_backup(other.path)

    def test_verify_rejects_partial_staging_and_invalid_or_unsupported_manifest(
        self,
    ) -> None:
        self.destination.mkdir()
        staging = (
            self.destination
            / f"{backup.STAGING_PREFIX}aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        )
        staging.mkdir(mode=0o700)
        os.chmod(staging, 0o700)
        (staging / backup.STAGING_STATE_NAME).write_text(
            '{"lifecycle":"STAGING"}\n', encoding="utf-8"
        )
        self.assertEqual(backup.list_finalized_backups(self.destination), ())
        with self.assertRaisesRegex(backup.BackupError, "BACKUP_NOT_FINALIZED"):
            backup.verify_backup(staging)

        result = self.create()
        manifest_path = result.path / backup.MANIFEST_NAME
        manifest_path.write_text("{not json", encoding="utf-8")
        with self.assertRaisesRegex(backup.BackupError, "BACKUP_MANIFEST_INVALID"):
            backup.verify_backup(result.path)

        unsupported_root = self.root / "unsupported"
        request = self.request()
        request = backup.BackupRequest(
            **{**request.__dict__, "destination_root": unsupported_root}
        )
        moments = iter((self.started, self.completed))
        unsupported = backup.create_backup(
            request,
            pg_dump_runner=FakePgDump(),
            backup_id=self.backup_id,
            clock=lambda: next(moments),
        )
        manifest_path = unsupported.path / backup.MANIFEST_NAME
        manifest = json.loads(manifest_path.read_bytes())
        manifest["schemaVersion"] = 2
        manifest_path.write_bytes(canonical_json_bytes(manifest) + b"\n")
        with self.assertRaises(backup.UnsupportedBackupFormat):
            backup.verify_backup(unsupported.path)

    def test_pg_dump_failure_is_fail_closed_and_not_discoverable(self) -> None:
        with self.assertRaisesRegex(backup.BackupError, "PG_DUMP_FAILED"):
            self.create(runner=FailingPgDump())
        self.assertEqual(backup.list_finalized_backups(self.destination), ())
        self.assertTrue(
            any(
                path.name.startswith(backup.STAGING_PREFIX)
                for path in self.destination.iterdir()
            )
        )

    def test_database_url_is_split_into_libpq_environment_not_argv(self) -> None:
        database_url = (
            "postgresql://animemo:not-a-real-secret@postgres.example:5433/animemo"
            "?sslmode=require&connect_timeout=7"
        )
        with mock.patch.dict(
            os.environ,
            {
                "ANIMEMO_UNRELATED_SECRET": "must-not-reach-database-process",
                "PGSERVICE": "must-not-be-inherited",
            },
        ):
            environment = backup.postgres_connection_environment(database_url)

        self.assertEqual(environment["PGHOST"], "postgres.example")
        self.assertEqual(environment["PGPORT"], "5433")
        self.assertEqual(environment["PGUSER"], "animemo")
        self.assertEqual(environment["PGDATABASE"], "animemo")
        self.assertEqual(environment["PGSSLMODE"], "require")
        self.assertEqual(environment["PGCONNECT_TIMEOUT"], "7")
        self.assertIn("PGPASSWORD", environment)
        self.assertNotIn(database_url, environment.values())
        self.assertNotIn("ANIMEMO_UNRELATED_SECRET", environment)
        self.assertNotIn("PGSERVICE", environment)

    def test_empty_pg_dump_is_rejected(self) -> None:
        with self.assertRaisesRegex(backup.BackupError, "PG_DUMP_EMPTY"):
            self.create(runner=FakePgDump(b""))
        self.assertEqual(backup.list_finalized_backups(self.destination), ())

    def test_filesystem_read_failure_is_fail_closed(self) -> None:
        with (
            mock.patch(
                "durability.backup._copy_regular_file",
                side_effect=OSError("read denied"),
            ),
            self.assertRaisesRegex(backup.BackupError, "BACKUP_IO_FAILED"),
        ):
            self.create()
        self.assertEqual(backup.list_finalized_backups(self.destination), ())

    def test_manifest_write_failure_is_fail_closed(self) -> None:
        with (
            mock.patch(
                "durability.backup._write_manifest", side_effect=OSError("write denied")
            ),
            self.assertRaisesRegex(backup.BackupError, "BACKUP_IO_FAILED"),
        ):
            self.create()
        self.assertEqual(backup.list_finalized_backups(self.destination), ())

    def test_finalize_interruption_leaves_only_undiscoverable_staging(self) -> None:
        with (
            mock.patch(
                "durability.backup._atomic_finalize",
                side_effect=OSError("rename interrupted"),
            ),
            self.assertRaisesRegex(backup.BackupError, "BACKUP_FINALIZE_FAILED"),
        ):
            self.create()
        self.assertEqual(backup.list_finalized_backups(self.destination), ())
        staging = tuple(
            path
            for path in self.destination.iterdir()
            if path.name.startswith(backup.STAGING_PREFIX)
        )
        self.assertEqual(len(staging), 1)
        with self.assertRaisesRegex(backup.BackupError, "BACKUP_NOT_FINALIZED"):
            backup.verify_backup(staging[0])

    def test_allowlist_rejects_runtime_pgdata_redis_logs_temp_and_nested_backup(
        self,
    ) -> None:
        for forbidden in (
            "filesystem/plugins/runtime",
            "filesystem/postgres",
            "filesystem/redis",
            "filesystem/logs",
            "filesystem/temp",
            "filesystem/backups",
            "application",
        ):
            with self.subTest(forbidden=forbidden):
                source = self.root / forbidden.replace("/", "-")
                source.mkdir(exist_ok=True)
                request = self.request()
                request = backup.BackupRequest(
                    **{
                        **request.__dict__,
                        "filesystem_sources": request.filesystem_sources
                        + (
                            backup.FilesystemSource(
                                logical_root=forbidden, source=source
                            ),
                        ),
                    }
                )
                with self.assertRaisesRegex(
                    backup.BackupError, "BACKUP_SOURCE_NOT_ALLOWED"
                ):
                    backup.create_backup(request, pg_dump_runner=FakePgDump())

        nested_backup = self.sources["filesystem/media"] / "backups"
        nested_backup.mkdir()
        (nested_backup / "old.sql.gz").write_bytes(b"not allowed")
        with self.assertRaisesRegex(backup.BackupError, "BACKUP_SOURCE_NOT_ALLOWED"):
            self.create()

    def test_source_tree_rejects_symlinks(self) -> None:
        link = self.sources["filesystem/media"] / "link"
        try:
            link.symlink_to(self.root / "outside")
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable")
        with self.assertRaisesRegex(backup.BackupError, "BACKUP_UNSAFE_SOURCE"):
            self.create()

    def test_sensitive_metadata_and_secret_reference_are_rejected(self) -> None:
        request = self.request()
        request = backup.BackupRequest(
            **{
                **request.__dict__,
                "producer": {"token": "must-not-enter-manifest"},
            }
        )
        with self.assertRaisesRegex(backup.BackupError, "BACKUP_SECRET_METADATA"):
            backup.create_backup(request, pg_dump_runner=FakePgDump())

        config_secret = self.sources["filesystem/config"] / "secret.json"
        config_secret.write_bytes(
            canonical_json_bytes({"databasePassword": "must-not-enter-backup"}) + b"\n"
        )
        with self.assertRaisesRegex(backup.BackupError, "BACKUP_SOURCE_NOT_ALLOWED"):
            self.create()
        config_secret.unlink()

        private_unknown = self.sources["filesystem/private"] / "unknown.bin"
        private_unknown.write_bytes(b"unclassified-private-state")
        with self.assertRaisesRegex(backup.BackupError, "BACKUP_SOURCE_NOT_ALLOWED"):
            self.create()
        private_unknown.unlink()

        request = self.request()
        request = backup.BackupRequest(
            **{
                **request.__dict__,
                "r2_references": (
                    backup.R2Reference(
                        backend_type="r2",
                        endpoint_identity="https://user:password@example.invalid",
                        bucket="animemo-media",
                        object_keys=("posters/one.webp",),
                    ),
                ),
            }
        )
        with self.assertRaisesRegex(backup.BackupError, "BACKUP_SECRET_METADATA"):
            backup.create_backup(request, pg_dump_runner=FakePgDump())

        self.secret_reference.write_bytes(b'{"provider":"test","token":"plaintext"}\n')
        with self.assertRaisesRegex(
            backup.BackupError, "BACKUP_SECRET_REFERENCE_INVALID"
        ):
            self.create()

    def test_all_canonical_roots_are_explicitly_required(self) -> None:
        request = self.request()
        request = backup.BackupRequest(
            **{
                **request.__dict__,
                "filesystem_sources": request.filesystem_sources[:-1],
            }
        )
        with self.assertRaisesRegex(backup.BackupError, "BACKUP_SOURCE_INCOMPLETE"):
            backup.create_backup(request, pg_dump_runner=FakePgDump())

    def test_verify_rejects_manifest_binding_inconsistency(self) -> None:
        result = self.create()
        manifest_path = result.path / backup.MANIFEST_NAME
        manifest = json.loads(manifest_path.read_bytes())
        manifest["source"]["instanceId"] = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        manifest_path.write_bytes(canonical_json_bytes(manifest) + b"\n")
        with self.assertRaisesRegex(backup.BackupError, "BACKUP_BINDING_MISMATCH"):
            backup.verify_backup(result.path)

    def test_claimed_v1_manifest_rejects_additional_fields(self) -> None:
        result = self.create()
        manifest_path = result.path / backup.MANIFEST_NAME
        manifest = json.loads(manifest_path.read_bytes())
        manifest["futurePrototypeField"] = True
        manifest_path.write_bytes(canonical_json_bytes(manifest) + b"\n")
        with self.assertRaisesRegex(backup.BackupError, "BACKUP_MANIFEST_INVALID"):
            backup.verify_backup(result.path)

    def test_verify_never_restores_or_mutates_payload(self) -> None:
        result = self.create()
        before = {
            path.relative_to(result.path).as_posix(): path.read_bytes()
            for path in result.path.rglob("*")
            if path.is_file()
        }
        backup.verify_backup(result.path)
        after = {
            path.relative_to(result.path).as_posix(): path.read_bytes()
            for path in result.path.rglob("*")
            if path.is_file()
        }
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
