from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar
from unittest.mock import patch

from durability import backup
from durability.backup_production import (
    ContainerPgDumpRunner,
    DockerComposeBackupHost,
    ProductionBackupError,
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


class FakeDeployment:
    def __init__(self) -> None:
        self.paths = SimpleNamespace(
            compose_project="animemo-blue",
            instance_name="blue",
            instance_id="00000000-0000-4000-8000-000000000000",
        )
        self.container = "container-123"
        self.image = "docker.io/library/postgres@sha256:" + "1" * 64
        self.state = "running healthy"
        self.labels = {
            "com.docker.compose.project": "animemo-blue",
            "com.docker.compose.service": "postgres",
            "io.animemo.instance-name": "blue",
            "io.animemo.instance-id": self.paths.instance_id,
            "io.animemo.compose-project": "animemo-blue",
        }

    def _compose(self, manifest, *args, timeout):
        del manifest, args, timeout
        return SimpleNamespace(stdout=self.container + "\n")

    def _inspect_container(self, container, template):
        if container != self.container:
            return ""
        if template == "{{.Config.Image}}":
            return self.image
        if template.startswith("{{.State.Status}}"):
            return self.state
        for label, value in self.labels.items():
            if label in template:
                return value
        return ""


class StubContainerPgDumpRunner(ContainerPgDumpRunner):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.tool_version = "pg_dump (PostgreSQL) 16.4"
        self.identity_row = "animemo\tanimemo\t160004"
        self.commands = []

    def _docker_text(self, argv, *, timeout=30):
        del timeout
        self.commands.append(list(argv))
        if "pg_dump" in argv and "--version" in argv:
            return self.tool_version
        if "psql" in argv:
            return self.identity_row
        raise AssertionError(argv)


def managed_config():
    return ManagedConfig(
        instance_id="00000000-0000-4000-8000-000000000000",
        config_revision="11111111-1111-4111-8111-111111111111",
        listen=ListenConfig("127.0.0.1", 8088),
        public_origin="https://backup.example.test",
        direct_access=DirectAccessConfig(False, False, False),
        trusted_origins=TrustedOriginsConfig((), (), ()),
        database=DatabaseConfig("animemo", "animemo", "database-password"),
        redis=RedisConfig("redis://redis:6379/0"),
        application=ApplicationConfig("django-secret", "credential-key", None, ()),
        integrations=IntegrationConfig("", "", ""),
    )


class FakePopen:
    calls: ClassVar[list[FakePopen]] = []
    returncode = 0
    timeout = False

    def __init__(self, argv, *, stdout, stderr, env, shell):
        self.argv = list(argv)
        self.stdout = stdout
        self.stderr = stderr
        self.env = dict(env)
        self.shell = shell
        self._returncode = None
        type(self).calls.append(self)
        stdout.write(b"-- PostgreSQL database dump\nSELECT 1;\n")

    def wait(self, timeout=None):
        del timeout
        if type(self).timeout and self._returncode is None:
            raise subprocess.TimeoutExpired(self.argv, 1)
        if self._returncode is not None:
            return self._returncode
        self._returncode = type(self).returncode
        return self._returncode

    def poll(self):
        return self._returncode

    def kill(self):
        self._returncode = -9


class ProductionPostgresAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.deployment = FakeDeployment()
        self.manifest = {"images": {"postgres": {"digest": "sha256:" + "1" * 64}}}
        self.runner = StubContainerPgDumpRunner(
            self.deployment, self.manifest, managed_config()
        )
        FakePopen.calls = []
        FakePopen.returncode = 0
        FakePopen.timeout = False

    def test_exact_postgres_container_identity_and_versions_pass(self):
        container, tool = self.runner.verify_identity()
        self.assertEqual(container, "container-123")
        self.assertEqual(tool, "pg_dump (PostgreSQL) 16.4")

    def test_wrong_postgres_digest_fails_closed(self):
        self.deployment.image = "docker.io/library/postgres@sha256:" + "2" * 64
        with self.assertRaisesRegex(
            ProductionBackupError, "BACKUP_POSTGRES_DIGEST_MISMATCH"
        ):
            self.runner.verify_identity()

    def test_wrong_compose_project_label_fails_closed(self):
        self.deployment.labels["com.docker.compose.project"] = "animemo-other"
        with self.assertRaisesRegex(
            ProductionBackupError, "BACKUP_POSTGRES_CONTAINER_INVALID"
        ):
            self.runner.verify_identity()

    def test_stopped_or_unhealthy_postgres_fails_closed(self):
        for state in ("exited healthy", "running unhealthy"):
            with self.subTest(state=state):
                self.deployment.state = state
                with self.assertRaisesRegex(
                    ProductionBackupError, "BACKUP_POSTGRES_NOT_HEALTHY"
                ):
                    self.runner.verify_identity()

    def test_wrong_pg_dump_major_is_compatibility_failure(self):
        self.runner.tool_version = "pg_dump (PostgreSQL) 15.9"
        with self.assertRaisesRegex(ProductionBackupError, "PG_DUMP_MAJOR_UNSUPPORTED"):
            self.runner.verify_identity()

    def test_wrong_server_major_fails_closed(self):
        self.runner.identity_row = "animemo\tanimemo\t150009"
        with self.assertRaisesRegex(
            ProductionBackupError, "BACKUP_POSTGRES_IDENTITY_MISMATCH"
        ):
            self.runner.verify_identity()

    def test_wrong_database_or_user_fails_closed(self):
        for row in ("other\tanimemo\t160004", "animemo\tother\t160004"):
            with self.subTest(row=row):
                self.runner.identity_row = row
                with self.assertRaisesRegex(
                    ProductionBackupError, "BACKUP_POSTGRES_IDENTITY_MISMATCH"
                ):
                    self.runner.verify_identity()

    def test_pg_dump_uses_fixed_no_shell_profile_and_password_only_in_env(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch("durability.backup_production.subprocess.Popen", FakePopen),
        ):
            target = Path(directory) / "database.sql"
            tool = self.runner.run(
                "postgresql://animemo:database-password@postgres:5432/animemo",
                target,
                executable="pg_dump",
                timeout=30,
            )
            call = FakePopen.calls[-1]
            self.assertEqual(tool, "pg_dump (PostgreSQL) 16.4")
            self.assertFalse(call.shell)
            self.assertEqual(
                call.argv[6:10],
                ["--format=plain", "--no-owner", "--no-privileges", "--username"],
            )
            self.assertNotIn("database-password", call.argv)
            self.assertEqual(call.env["PGPASSWORD"], "database-password")
            self.assertGreater(target.stat().st_size, 0)

    def test_nonzero_pg_dump_fails_and_does_not_ignore_stderr(self):
        FakePopen.returncode = 9
        with (
            tempfile.TemporaryDirectory() as directory,
            patch("durability.backup_production.subprocess.Popen", FakePopen),
            self.assertRaisesRegex(backup.BackupError, "PG_DUMP_FAILED"),
        ):
            self.runner.run(
                "postgresql://animemo:database-password@postgres:5432/animemo",
                Path(directory) / "database.sql",
                executable="pg_dump",
                timeout=30,
            )

    def test_pg_dump_timeout_is_stable_and_process_is_killed(self):
        FakePopen.timeout = True
        with (
            tempfile.TemporaryDirectory() as directory,
            patch("durability.backup_production.subprocess.Popen", FakePopen),
        ):
            with self.assertRaisesRegex(backup.BackupError, "PG_DUMP_TIMEOUT"):
                self.runner.run(
                    "postgresql://animemo:database-password@postgres:5432/animemo",
                    Path(directory) / "database.sql",
                    executable="pg_dump",
                    timeout=1,
                )
            self.assertEqual(FakePopen.calls[-1].poll(), -9)

    def test_database_url_must_match_managed_config(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            self.assertRaisesRegex(backup.BackupError, "DATABASE_URL_INVALID"),
        ):
            self.runner.run(
                "postgresql://other:wrong@postgres:5432/other",
                Path(directory) / "database.sql",
                executable="pg_dump",
                timeout=30,
            )

    def test_media_inventory_uses_exact_django_table_names(self):
        self.assertIn("FROM site_mediaobject", DockerComposeBackupHost._MEDIA_SQL)
        self.assertIn(
            "JOIN site_mediastoragebackend", DockerComposeBackupHost._MEDIA_SQL
        )
        self.assertIn(
            "LEFT JOIN site_cloudflarer2account", DockerComposeBackupHost._MEDIA_SQL
        )
        self.assertIn(
            "FROM site_mediawritereservation",
            DockerComposeBackupHost._PENDING_SQL,
        )
        self.assertNotIn("site_config_", DockerComposeBackupHost._MEDIA_SQL)
        self.assertNotIn("site_config_", DockerComposeBackupHost._PENDING_SQL)


@unittest.skipUnless(
    os.environ.get("ANIMEMO_RUN_PRODUCTION_BACKUP_DOCKER_TEST") == "1",
    "requires the Release Gate production-like PostgreSQL container",
)
class RealProductionPostgresBackupTests(unittest.TestCase):
    class Deployment:
        def __init__(self) -> None:
            self.paths = SimpleNamespace(
                compose_project="animemo-release-gate",
                instance_name="release-gate",
                instance_id="12345678-1234-4234-9234-123456789abc",
            )

        def _compose(self, manifest, *args, timeout):
            del manifest, args
            completed = subprocess.run(
                [
                    "/usr/bin/docker",
                    "ps",
                    "--filter",
                    "label=com.docker.compose.project=animemo-release-gate",
                    "--filter",
                    "label=com.docker.compose.service=postgres",
                    "--format",
                    "{{.ID}}",
                ],
                check=True,
                text=True,
                capture_output=True,
                timeout=timeout,
                shell=False,
            )
            return SimpleNamespace(stdout=completed.stdout)

        def _inspect_container(self, container, template):
            return subprocess.run(
                ["/usr/bin/docker", "inspect", "--format", template, container],
                check=True,
                text=True,
                capture_output=True,
                timeout=30,
                shell=False,
            ).stdout.strip()

    def test_real_postgres_16_container_create_and_verify(self):
        config = managed_config()
        config = ManagedConfig(
            **{
                **config.__dict__,
                "instance_id": "12345678-1234-4234-9234-123456789abc",
                "database": DatabaseConfig("animemo", "animemo", "ci-password"),
            }
        )
        digest = (
            "sha256:075f7ba66bc9b3ce7d6b8b635208ff61cd7cf1a67d71ec530eec5d7ae0cbe571"
        )
        runner = ContainerPgDumpRunner(
            self.Deployment(),
            {"images": {"postgres": {"digest": digest}}},
            config,
        )
        self.assertEqual(
            runner.psql_scalar(DockerComposeBackupHost._PENDING_SQL), "0"
        )
        runner.psql(DockerComposeBackupHost._MEDIA_SQL)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            if os.name == "posix":
                os.chmod(root, 0o700)
            sources = {}
            for logical in backup.CANONICAL_FILESYSTEM_ROOTS:
                source = root / "sources" / logical.replace("/", "-")
                source.mkdir(parents=True)
                if os.name == "posix":
                    os.chmod(source, 0o700)
                if logical in {"filesystem/config", "updater-state"}:
                    metadata = source / "metadata.json"
                    metadata.write_bytes(
                        canonical_json_bytes({"logical": logical}) + b"\n"
                    )
                    if os.name == "posix":
                        os.chmod(metadata, 0o600)
                sources[logical] = source
            request = backup.BackupRequest(
                destination_root=root / "backups",
                database_url="postgresql://animemo:ci-password@postgres:5432/animemo",
                source=backup.BackupSourceIdentity(
                    instance_id=config.instance_id,
                    source_locator_digest="sha256:" + "2" * 64,
                    release={"version": "v1.1.0-rc.7", "commit": "a" * 40},
                    deployment_contract={
                        "schemaVersion": 2,
                        "digest": "sha256:" + "3" * 64,
                    },
                    database_contract={"id": "animemo.database/v1", "serverMajor": 16},
                    configuration_contract={"id": "animemo.configuration/v1"},
                    instance_name="release-gate",
                ),
                filesystem_sources=tuple(
                    backup.FilesystemSource(logical_root=name, source=source)
                    for name, source in sources.items()
                ),
                producer={"name": "release-gate-real-production-adapter"},
                platform={"os": "linux", "architecture": "amd64"},
                quiescence={"method": "isolated-release-gate"},
            )
            result = backup.create_backup(request, pg_dump_runner=runner)
            verified = backup.verify_backup(result.path)
            self.assertEqual(verified.backup_id, result.backup_id)
            self.assertGreater(verified.database_uncompressed_bytes, 0)


if __name__ == "__main__":
    unittest.main()
