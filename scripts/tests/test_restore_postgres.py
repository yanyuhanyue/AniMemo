from __future__ import annotations

import contextlib
import importlib.util
import os
import shutil
import tempfile
import unittest
import uuid
from pathlib import Path

from durability import backup, restore
from durability.compatibility import (
    CompatibilityOutcome,
    Dimension,
    DimensionAssessment,
    ReasonCode,
)

PSYCOPG_AVAILABLE = importlib.util.find_spec("psycopg") is not None
if PSYCOPG_AVAILABLE:
    import psycopg
    from psycopg import sql
else:
    psycopg = None
    sql = None


class IntegrationDestination:
    def inspect(self) -> restore.DestinationSnapshot:
        return restore.DestinationSnapshot(
            classification=restore.DestinationClass.EXISTING_EMPTY,
            deployment_profile="v1.1-standard",
            canonical_roots=restore.CANONICAL_ROOTS,
            ownership_verified=True,
            empty_verified=True,
            parent_ready=False,
            evidence_digest="sha256:" + "f" * 64,
        )


class IntegrationRelease:
    def verify(self, manifest):
        return restore.ReleaseEvidence(
            release_identity_digest="sha256:" + "a" * 64,
            deployment_identity_digest="sha256:" + "b" * 64,
        )

    def acquire(self, evidence):
        return object()


class IntegrationUpdater:
    def verify(self, manifest, release_evidence):
        return restore.UpdaterEvidence(
            state_identity_digest="sha256:" + "c" * 64,
            pending_state_preserved=True,
        )

    def stage(self, manifest, evidence, mutation):
        return None


class IntegrationCompatibility:
    def assess(self, manifest, destination, release_evidence, updater_evidence):
        reasons = {
            Dimension.DEPLOYMENT_CONTRACT: ReasonCode.DEPLOYMENT_CONTRACT_SUPPORTED,
            Dimension.SCHEMA_CONTRACTS: ReasonCode.SCHEMA_CONTRACTS_SUPPORTED,
            Dimension.EXACT_RELEASE_IDENTITY: ReasonCode.RELEASE_IDENTITY_VERIFIED,
            Dimension.PLATFORM_RUNTIME: ReasonCode.PLATFORM_RUNTIME_SUPPORTED,
            Dimension.SUPPORTED_PATH: ReasonCode.DIRECT_PATH_SUPPORTED,
        }
        dimensions = tuple(
            DimensionAssessment(
                name=dimension,
                outcome=CompatibilityOutcome.COMPATIBLE,
                reason_code=reason,
                source={"identity": f"source-{dimension.value}"},
                target={"identity": f"target-{dimension.value}"},
            )
            for dimension, reason in reasons.items()
        )
        return restore.RestoreCompatibilityEvidence(dimensions)


class IntegrationMutation:
    def __init__(self) -> None:
        self.published = False
        self.snapshot_root = None
        self.recovery = None

    @contextlib.contextmanager
    def acquire_lock(self, operation_id):
        yield

    def begin(self, plan):
        return None

    def stage_release(self, release_material, evidence):
        return None

    def stage_secret(self, resolution):
        return None

    def prepare_database(self):
        return None

    def restore_filesystem(self, backup_root, member_paths):
        self.snapshot_root = Path(backup_root)

    def apply_upgrade(self, actions):
        raise AssertionError("no upgrade is expected")

    def bootstrap(self):
        return None

    def rebuild_runtime(self):
        return None

    def build_locator(self, instance_id, release_evidence):
        return None

    def rotate_authentication_epoch(self):
        return None

    def publish(self):
        self.published = True

    def record_recovery_required(self, evidence):
        self.recovery = evidence


class IntegrationValidator:
    def validate(self, manifest, plan, mutation):
        return restore.ValidationReport(
            passed_checks=restore.REQUIRED_VALIDATIONS,
            evidence_digest="sha256:" + "d" * 64,
        )


class IsolatedPostgreSQLRestoreTests(unittest.TestCase):
    @unittest.skipUnless(
        os.environ.get("ANIMEMO_TEST_DATABASE_URL")
        and os.environ.get("ANIMEMO_RESTORE_TEST_DATABASE_URL")
        and shutil.which("pg_dump")
        and shutil.which("psql")
        and PSYCOPG_AVAILABLE,
        "requires two isolated PostgreSQL URLs, pg_dump, psql and psycopg",
    )
    def test_real_logical_dump_restores_representative_memory_row(self) -> None:
        source_url = os.environ["ANIMEMO_TEST_DATABASE_URL"]
        target_url = os.environ["ANIMEMO_RESTORE_TEST_DATABASE_URL"]
        if source_url == target_url:
            self.fail("source and target database URLs must differ")

        schema_name = f"animemo_restore_{uuid.uuid4().hex}"
        future_payload = b"\x00animemo-future-memory-v2\xff"
        self.addCleanup(self._drop_schema, source_url, schema_name)
        self.addCleanup(self._drop_schema, target_url, schema_name)
        self._assert_empty(source_url, "source")
        self._assert_empty(target_url, "target")

        with psycopg.connect(source_url, autocommit=True) as connection:
            server_major = connection.info.server_version // 10000
            with connection.cursor() as cursor:
                cursor.execute(
                    sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema_name))
                )
                cursor.execute(
                    sql.SQL(
                        "CREATE TABLE {}.memory ("
                        "memory_id text PRIMARY KEY, "
                        "content text NOT NULL, "
                        "external_metadata_id text, "
                        "current_provider_identity text NOT NULL, "
                        "future_payload bytea NOT NULL)"
                    ).format(sql.Identifier(schema_name))
                )
                cursor.execute(
                    sql.SQL(
                        "CREATE TABLE {}.provider_binding_history ("
                        "memory_id text NOT NULL REFERENCES {}.memory(memory_id), "
                        "provider_identity text NOT NULL, "
                        "binding_state text NOT NULL)"
                    ).format(
                        sql.Identifier(schema_name),
                        sql.Identifier(schema_name),
                    )
                )
                cursor.execute(
                    sql.SQL(
                        "CREATE TABLE {}.merge_history ("
                        "merge_id text PRIMARY KEY, "
                        "survivor_memory_id text NOT NULL "
                        "REFERENCES {}.memory(memory_id), "
                        "source_memory_reference text NOT NULL)"
                    ).format(
                        sql.Identifier(schema_name),
                        sql.Identifier(schema_name),
                    )
                )
                cursor.execute(
                    sql.SQL(
                        "INSERT INTO {}.memory VALUES (%s, %s, %s, %s, %s)"
                    ).format(sql.Identifier(schema_name)),
                    (
                        "memory-identity-1",
                        "representative-user-memory",
                        None,
                        "provider-current",
                        future_payload,
                    ),
                )
                cursor.execute(
                    sql.SQL(
                        "INSERT INTO {}.provider_binding_history "
                        "VALUES (%s, %s, %s)"
                    ).format(sql.Identifier(schema_name)),
                    ("memory-identity-1", "provider-previous", "RETAINED"),
                )
                cursor.execute(
                    sql.SQL(
                        "INSERT INTO {}.merge_history VALUES (%s, %s, %s)"
                    ).format(sql.Identifier(schema_name)),
                    (
                        "merge-history-1",
                        "memory-identity-1",
                        "memory-identity-legacy",
                    ),
                )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sources = {}
            for logical_root in sorted(backup.CANONICAL_FILESYSTEM_ROOTS):
                source = root / "sources" / logical_root.replace("/", "-")
                source.mkdir(parents=True)
                sources[logical_root] = source
            (sources["filesystem/config"] / "contract.json").write_bytes(b"{}\n")
            result = backup.create_backup(
                backup.BackupRequest(
                    destination_root=root / "backups",
                    database_url=source_url,
                    source=backup.BackupSourceIdentity(
                        instance_id="11111111-2222-4333-8444-555555555555",
                        source_locator_digest="sha256:" + "1" * 64,
                        release={"version": "1.1.0", "commit": "a" * 40},
                        deployment_contract={
                            "schemaVersion": 1,
                            "digest": "sha256:" + "2" * 64,
                        },
                        database_contract={
                            "id": "animemo.database/v1",
                            "serverMajor": server_major,
                        },
                        configuration_contract={"id": "animemo.configuration/v1"},
                    ),
                    filesystem_sources=tuple(
                        backup.FilesystemSource(
                            logical_root=logical_root,
                            source=source,
                        )
                        for logical_root, source in sources.items()
                    ),
                    producer={"name": "restore-postgres-test", "version": "1"},
                    platform={"os": "isolated-test", "architecture": "test"},
                    quiescence={"method": "coordinator-isolated-postgresql"},
                    pg_dump_timeout=120,
                )
            )
            backup.verify_backup(result.path)
            self._assert_empty(target_url, "target")
            mutation = IntegrationMutation()
            request = restore.RestoreRequest(
                operation_id="22222222-2222-4222-8222-222222222222",
                backup_root=result.path,
                destination=IntegrationDestination(),
                release=IntegrationRelease(),
                updater=IntegrationUpdater(),
                secret_resolver=restore.NoneSecretResolver(),
                compatibility=IntegrationCompatibility(),
                database=restore.SubprocessPostgresRestore(target_url, timeout=120),
                mutation=mutation,
                validator=IntegrationValidator(),
            )
            plan = restore.prepare_restore(request)
            restored = restore.execute_restore(
                request,
                plan,
                accepted_plan_digest=plan.plan_digest,
            )
            self.assertEqual(restored.state, restore.RestoreTerminalState.PUBLISHED)
            self.assertTrue(mutation.published)
            self.assertIsNone(mutation.recovery)
            snapshot_root = mutation.snapshot_root

        self.assertFalse(snapshot_root.exists())

        with (
            psycopg.connect(target_url) as connection,
            connection.cursor() as cursor,
        ):
            cursor.execute(
                sql.SQL(
                    "SELECT m.memory_id, m.content, m.external_metadata_id, "
                    "m.current_provider_identity, b.provider_identity, "
                    "b.binding_state, h.merge_id, h.source_memory_reference, "
                    "m.future_payload "
                    "FROM {}.memory m "
                    "JOIN {}.provider_binding_history b USING (memory_id) "
                    "JOIN {}.merge_history h "
                    "ON h.survivor_memory_id = m.memory_id"
                ).format(
                    sql.Identifier(schema_name),
                    sql.Identifier(schema_name),
                    sql.Identifier(schema_name),
                )
            )
            row = cursor.fetchone()
            self.assertEqual(
                (*row[:-1], bytes(row[-1])),
                (
                    "memory-identity-1",
                    "representative-user-memory",
                    None,
                    "provider-current",
                    "provider-previous",
                    "RETAINED",
                    "merge-history-1",
                    "memory-identity-legacy",
                    future_payload,
                ),
            )

    def _assert_empty(self, database_url: str, label: str) -> None:
        with (
            psycopg.connect(database_url) as connection,
            connection.cursor() as cursor,
        ):
            cursor.execute(restore.SubprocessPostgresRestore._EMPTY_QUERY)
            count = cursor.fetchone()[0]
        if count != 0:
            self.fail(f"{label} database must be initially empty")

    @staticmethod
    def _drop_schema(database_url: str, schema_name: str) -> None:
        with (
            psycopg.connect(database_url, autocommit=True) as connection,
            connection.cursor() as cursor,
        ):
            cursor.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                    sql.Identifier(schema_name)
                )
            )


if __name__ == "__main__":
    unittest.main()
