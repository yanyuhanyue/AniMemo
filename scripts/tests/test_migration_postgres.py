from __future__ import annotations

import importlib.util
import os
import shutil
import tempfile
import unittest
import uuid
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from durability import migration, restore
from durability.compatibility import (
    EVALUATION_ORDER,
    CompatibilityOperation,
    CompatibilityOutcome,
    Dimension,
    DimensionAssessment,
    ReasonCode,
    evaluate_compatibility,
)
from durability.instance import InstanceLocator, ListenIdentity
from durability.secret_envelope import OneTimeKey, SecretEntry

PSYCOPG_AVAILABLE = importlib.util.find_spec("psycopg") is not None
if PSYCOPG_AVAILABLE:
    import psycopg
    from psycopg import sql
else:
    psycopg = None
    sql = None


class StableProbe:
    def __init__(self) -> None:
        self.value = migration.SourceConsistencySnapshot(
            generation="real-postgres-generation-1",
            config_generation="real-config-generation-1",
            quiesced=True,
            writes_blocked=True,
            updater_idle=True,
            database_migration_idle=True,
            plugin_operations_idle=True,
            media_writes_idle=True,
        )

    def snapshot(self) -> migration.SourceConsistencySnapshot:
        return self.value


class PostgreSQLReferenceProbe:
    def __init__(self, database_url: str, schema_name: str) -> None:
        self.database_url = database_url
        self.schema_name = schema_name

    def capture(self) -> migration.DatabaseReferenceInventory:
        with (
            psycopg.connect(self.database_url) as connection,
            connection.cursor() as cursor,
        ):
            cursor.execute(
                sql.SQL("SELECT count(*) FROM {}.memory_nodes").format(
                    sql.Identifier(self.schema_name)
                )
            )
            if cursor.fetchone()[0] != 2:
                raise AssertionError("quiesced database reference graph changed")
        return migration.DatabaseReferenceInventory(
            generation="real-database-references-1",
            plugin_packages=(),
            local_media=(),
            r2_media=(),
        )


class PostgreSQLMigrationTarget:
    """Inactive test target using the same bounded restore adapter as Restore v1."""

    def __init__(
        self,
        database_url: str,
        *,
        schema_name: str,
        expected_graph: dict[str, list[tuple[object, ...]]],
        release_identity: dict[str, object],
        deployment_contract: dict[str, object],
    ) -> None:
        self.database_url = database_url
        self.schema_name = schema_name
        self.expected_graph = expected_graph
        self.release_identity = release_identity
        self.deployment_contract = deployment_contract
        self.events: list[str] = []
        self.published_inactive = False
        self.recovery_evidence: migration.MigrationRecoveryEvidence | None = None

    def inspect(self) -> migration.TargetInspection:
        self.events.append("inspect")
        return migration.TargetInspection(
            canonical_roots=True,
            empty_owned_target=True,
            active_instance_id=None,
            release_identity=self.release_identity,
            deployment_contract=self.deployment_contract,
            updater_current=self.release_identity,
            target_r2_identities={},
            supported_plugin_sdk_apis=frozenset(),
        )

    def begin(self, *, bundle_id: str, instance_id: str) -> None:
        self.events.append("begin")

    def stage_database(self, path: Path, metadata) -> None:
        self.events.append("database")
        restore.SubprocessPostgresRestore(
            self.database_url,
            timeout=120,
        ).restore(path)

    def stage_plugin_package(self, path: Path, metadata) -> None:
        raise AssertionError("the isolated fixture has no plugin packages")

    def stage_local_media(self, path: Path, metadata) -> None:
        raise AssertionError("the isolated fixture has no local media")

    def stage_configuration(self, configuration, secrets) -> None:
        secrets.get_secret("CREDENTIAL_ENCRYPTION_KEY")
        self.events.append("configuration")

    def stage_private_state(self, state) -> None:
        self.events.append("private")

    def stage_updater_state(self, state) -> None:
        self.events.append("updater")

    def apply_upgrade(self, actions) -> None:
        self.events.append("upgrade")

    def validate_inactive(self, *, bundle_id: str, instance_id: str) -> bool:
        self.events.append("validate")
        graph = IsolatedPostgreSQLMigrationTests._read_graph(
            self.database_url, self.schema_name
        )
        expected_instance = self.expected_graph["memory"][0][0]
        return graph == self.expected_graph and instance_id == expected_instance

    def publish_inactive(self, *, bundle_id: str, instance_id: str) -> None:
        self.events.append("publish-inactive")
        self.published_inactive = True

    def rollback(self, *, bundle_id: str) -> None:
        self.events.append("rollback")

    def record_recovery_required(
        self, evidence: migration.MigrationRecoveryEvidence
    ) -> None:
        self.recovery_evidence = evidence
        self.events.append("recovery")


class IsolatedPostgreSQLMigrationTests(unittest.TestCase):
    @unittest.skipUnless(
        os.environ.get("ANIMEMO_TEST_DATABASE_URL")
        and os.environ.get("ANIMEMO_RESTORE_TEST_DATABASE_URL")
        and shutil.which("pg_dump")
        and shutil.which("psql")
        and PSYCOPG_AVAILABLE,
        "requires two isolated PostgreSQL URLs, pg_dump, psql and psycopg",
    )
    def test_real_bundle_moves_exact_memory_and_instance_into_inactive_target(
        self,
    ) -> None:
        source_url = os.environ["ANIMEMO_TEST_DATABASE_URL"]
        target_url = os.environ["ANIMEMO_RESTORE_TEST_DATABASE_URL"]
        if source_url == target_url:
            self.fail("source and target database URLs must differ")
        self._assert_empty_database(source_url)
        self._assert_empty_database(target_url)

        schema_name = f"animemo_migration_{uuid.uuid4().hex}"
        instance_id = "11111111-2222-4333-8444-555555555555"
        expected_graph: dict[str, list[tuple[object, ...]]] = {
            "memory": [
                (
                    instance_id,
                    "memory-identity-1",
                    "representative-user-memory",
                    None,
                    b"\x00future-memory-v9\xff",
                ),
                (
                    instance_id,
                    "memory-identity-2",
                    "merged-history-memory",
                    None,
                    b"\x01opaque-extension",
                ),
            ],
            "provider": [
                (
                    "memory-identity-1",
                    "provider:stable-binding",
                    "provider:historical-binding",
                )
            ],
            "merge": [
                (
                    "memory-identity-2",
                    "memory-identity-1",
                    "history:merge-reference-1",
                )
            ],
        }
        self.addCleanup(self._drop_schema, source_url, schema_name)
        self.addCleanup(self._drop_schema, target_url, schema_name)

        with psycopg.connect(source_url, autocommit=True) as connection:
            server_major = connection.info.server_version // 10000
            with connection.cursor() as cursor:
                cursor.execute(
                    sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema_name))
                )
                cursor.execute(
                    sql.SQL(
                        "CREATE TABLE {}.memory_nodes ("
                        "instance_id text NOT NULL, "
                        "memory_id text PRIMARY KEY, "
                        "content text NOT NULL, "
                        "external_metadata text NULL, "
                        "unsupported_payload bytea NOT NULL)"
                    ).format(sql.Identifier(schema_name))
                )
                cursor.executemany(
                    sql.SQL(
                        "INSERT INTO {}.memory_nodes VALUES (%s, %s, %s, %s, %s)"
                    ).format(sql.Identifier(schema_name)),
                    expected_graph["memory"],
                )
                cursor.execute(
                    sql.SQL(
                        "CREATE TABLE {}.provider_binding_history ("
                        "memory_id text NOT NULL, provider_identity text NOT NULL, "
                        "previous_identity text NOT NULL)"
                    ).format(sql.Identifier(schema_name))
                )
                cursor.executemany(
                    sql.SQL(
                        "INSERT INTO {}.provider_binding_history VALUES (%s, %s, %s)"
                    ).format(sql.Identifier(schema_name)),
                    expected_graph["provider"],
                )
                cursor.execute(
                    sql.SQL(
                        "CREATE TABLE {}.merge_history ("
                        "merged_memory_id text NOT NULL, source_memory_id text NOT NULL, "
                        "history_reference text NOT NULL)"
                    ).format(sql.Identifier(schema_name))
                )
                cursor.executemany(
                    sql.SQL("INSERT INTO {}.merge_history VALUES (%s, %s, %s)").format(
                        sql.Identifier(schema_name)
                    ),
                    expected_graph["merge"],
                )

        external_key = OneTimeKey.from_bytes(b"E" * 32)
        bundle_id = uuid.UUID("12345678-1234-4678-9234-567812345678")
        release_identity = {
            "version": "1.1.0",
            "channel": "stable",
            "commit": "a" * 40,
            "manifestDigest": "sha256:" + "1" * 64,
            "apiDigest": "sha256:" + "2" * 64,
            "webDigest": "sha256:" + "3" * 64,
        }
        deployment_contract = {"id": "animemo.deployment/v1"}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = migration.create_migration_bundle(
                migration.MigrationBundleRequest(
                    destination_root=root / "migration-bundles",
                    source_locator=InstanceLocator(
                        schema_version=1,
                        instance_id=instance_id,
                        app_root=PurePosixPath("/opt/animemo"),
                        data_root=PurePosixPath("/data/animemo"),
                        deployment_profile="v1.1-standard",
                        listen=ListenIdentity("127.0.0.1", 8000),
                        public_origin="https://isolated.example.invalid",
                        managed_config_path=PurePosixPath(
                            "/data/animemo/config/animemo.json"
                        ),
                        config_revision="11111111-1111-4111-8111-111111111111",
                        release_identity=release_identity,
                    ),
                    source_probe=StableProbe(),
                    database_url=source_url,
                    database_server_major=server_major,
                    database_reference_probe=PostgreSQLReferenceProbe(
                        source_url, schema_name
                    ),
                    deployment_contract=deployment_contract,
                    database_contract={
                        "id": "animemo.database/v1",
                        "serverMajor": server_major,
                    },
                    configuration_contract={"id": "animemo.configuration/v1"},
                    configuration=migration.MigrationConfiguration(
                        mode=migration.ConfigurationMode.PRESERVE,
                        non_secret={"profile": "isolated-postgresql"},
                        dispositions={
                            "appRoot": "TARGET-LOCAL",
                            "dataRoot": "TARGET-LOCAL",
                            "managedConfigPath": "TARGET-LOCAL",
                            "configRevision": "TARGET-LOCAL",
                            "databaseHost": "TARGET-LOCAL",
                            "databaseCredential": "TARGET-LOCAL",
                            "redisHost": "TARGET-LOCAL",
                            "redisCredential": "TARGET-LOCAL",
                            "publicOrigin": "PRESERVE",
                            "listen": "PRESERVE",
                        },
                    ),
                    private_state={
                        "schemaVersion": 1,
                        "instanceLifecycle": "INITIALIZED",
                        "allowlistedEntries": [],
                        "mergeHistoryReferences": ["history:merge-reference-1"],
                        "unknownFilesCopied": False,
                    },
                    updater_state={
                        "schemaVersion": 1,
                        "generation": "updater-real-1",
                        "operationState": "IDLE",
                        "current": release_identity,
                        "previousHistory": [],
                        "completedOperations": [],
                        "pendingOperation": None,
                        "manualRecoveryRequired": False,
                    },
                    external_secret=external_key,
                    secret_entries=(
                        SecretEntry.preserve("CREDENTIAL_ENCRYPTION_KEY", b"C" * 32),
                    ),
                ),
                bundle_id=bundle_id,
                clock=lambda: datetime(2026, 8, 16, 1, 2, 3, tzinfo=UTC),
            )
            verification = migration.verify_migration_bundle(result.path)
            compatible_reasons = {
                Dimension.FORMAT: ReasonCode.FORMAT_SUPPORTED,
                Dimension.INTEGRITY_AUTHENTICATION: ReasonCode.INTEGRITY_AUTHENTICATED,
                Dimension.DEPLOYMENT_CONTRACT: ReasonCode.DEPLOYMENT_CONTRACT_SUPPORTED,
                Dimension.SCHEMA_CONTRACTS: ReasonCode.SCHEMA_CONTRACTS_SUPPORTED,
                Dimension.EXACT_RELEASE_IDENTITY: ReasonCode.RELEASE_IDENTITY_VERIFIED,
                Dimension.PLATFORM_RUNTIME: ReasonCode.PLATFORM_RUNTIME_SUPPORTED,
                Dimension.SUPPORTED_PATH: ReasonCode.DIRECT_PATH_SUPPORTED,
            }
            decision = evaluate_compatibility(
                CompatibilityOperation.MIGRATION,
                verification.artifact_identity(),
                [
                    DimensionAssessment(
                        name=name,
                        outcome=CompatibilityOutcome.COMPATIBLE,
                        reason_code=compatible_reasons[name],
                        source={"identity": f"source-{name.value}"},
                        target={"capability": f"target-{name.value}"},
                    )
                    for name in EVALUATION_ORDER
                ],
            )
            target = PostgreSQLMigrationTarget(
                target_url,
                schema_name=schema_name,
                expected_graph=expected_graph,
                release_identity=release_identity,
                deployment_contract=deployment_contract,
            )
            consumed = migration.consume_migration_bundle(
                result.path,
                external_secret=external_key,
                compatibility=decision,
                target=target,
            )

        self.assertEqual(consumed.instance_id, instance_id)
        self.assertEqual(consumed.state, "READY_FOR_HANDOFF")
        self.assertFalse(consumed.target_active)
        self.assertTrue(target.published_inactive)
        self.assertEqual(
            target.events,
            [
                "inspect",
                "begin",
                "database",
                "configuration",
                "private",
                "updater",
                "validate",
                "publish-inactive",
            ],
        )
        self.assertEqual(self._read_graph(source_url, schema_name), expected_graph)
        self.assertEqual(self._read_graph(target_url, schema_name), expected_graph)

    @staticmethod
    def _assert_empty_database(database_url: str) -> None:
        query = (
            "SELECT count(*) FROM pg_catalog.pg_class c "
            "JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace "
            "WHERE n.nspname NOT IN ('pg_catalog','information_schema') "
            "AND n.nspname NOT LIKE 'pg_toast%' "
            "AND c.relkind IN ('r','p','v','m','S','f')"
        )
        with (
            psycopg.connect(database_url) as connection,
            connection.cursor() as cursor,
        ):
            cursor.execute(query)
            if cursor.fetchone()[0] != 0:
                raise AssertionError("isolated PostgreSQL database must be empty")

    @staticmethod
    def _read_graph(
        database_url: str, schema_name: str
    ) -> dict[str, list[tuple[object, ...]]]:
        with (
            psycopg.connect(database_url) as connection,
            connection.cursor() as cursor,
        ):
            cursor.execute(
                sql.SQL(
                    "SELECT instance_id, memory_id, content, external_metadata, "
                    "unsupported_payload FROM {}.memory_nodes ORDER BY memory_id"
                ).format(sql.Identifier(schema_name))
            )
            memory = cursor.fetchall()
            cursor.execute(
                sql.SQL(
                    "SELECT memory_id, provider_identity, previous_identity "
                    "FROM {}.provider_binding_history ORDER BY memory_id"
                ).format(sql.Identifier(schema_name))
            )
            provider = cursor.fetchall()
            cursor.execute(
                sql.SQL(
                    "SELECT merged_memory_id, source_memory_id, history_reference "
                    "FROM {}.merge_history ORDER BY merged_memory_id"
                ).format(sql.Identifier(schema_name))
            )
            merge = cursor.fetchall()
            return {"memory": memory, "provider": provider, "merge": merge}

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
