from __future__ import annotations

import gzip
import hashlib
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from release.contract import build_manifest
from updater.deployment import HostPaths, ImmutableComposeDeployment
from updater.errors import StateError


class FakeRunner:
    def __init__(self):
        self.calls = []
        self.service_health = {name: "healthy 0" for name in ["postgres", "redis", "api", "web"]}
        self.container_logs = {name: "" for name in ["postgres", "redis", "api", "web"]}
        self.container_errors = {name: "" for name in ["postgres", "redis", "api", "web"]}

    def run(self, argv, **kwargs):
        self.calls.append((tuple(argv), kwargs))
        stdout = ""
        if "ps" in argv and "--services" in argv:
            stdout = "postgres\nredis\napi\nweb\n"
        elif "ps" in argv and "-q" in argv:
            stdout = f"{argv[-1]}-container\n"
        elif argv[:2] == ["/usr/bin/docker", "inspect"]:
            service = str(argv[-1]).removesuffix("-container")
            stdout = self.service_health[service] + "\n"
        elif argv[:2] == ["/usr/bin/docker", "logs"]:
            service = str(argv[-1]).removesuffix("-container")
            stdout = self.container_logs[service]
        elif "list_enabled_plugin_apis" in " ".join(argv):
            stdout = '[2]\n'
        stderr = ""
        if argv[:2] == ["/usr/bin/docker", "logs"]:
            service = str(argv[-1]).removesuffix("-container")
            stderr = self.container_errors[service]
        return type("Result", (), {"stdout": stdout, "stderr": stderr})()

    def write_gzip(self, argv, path, **kwargs):
        self.calls.append((tuple(argv), kwargs))
        path.write_bytes(b"placeholder")
        return {"sha256": "a" * 64, "uncompressedBytes": 128}


def manifest():
    return build_manifest(
        version="v1.0.0",
        channel="stable",
        commit="1" * 40,
        created_at=datetime(2026, 8, 12, tzinfo=timezone.utc),
        api_digest="sha256:" + "a" * 64,
        web_digest="sha256:" + "b" * 64,
        minimum_updater_version="1.0.0",
        database_contract="animemo-db-v1",
        database_accepts=["animemo-db-v1"],
        migration_required=False,
        migration_policy="none",
        application_rollback="safe",
        configuration_contract="animemo-config-v1",
        configuration_accepts=["animemo-config-v1"],
        plugin_sdk_apis=[2],
        promoted_from="v1.0.0-rc.1",
    )


class ImmutableComposeDeploymentTests(unittest.TestCase):
    def make(self, directory, *, http_statuses=None):
        root = Path(directory)
        app = root / "app"
        data = root / "data"
        state = root / "state"
        memory_info = root / "meminfo"
        (app / "deploy").mkdir(parents=True)
        (app / "deploy" / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
        (app / ".env.production").write_text(
            "POSTGRES_USER=anime_journal\nPOSTGRES_DB=anime_journal\nFRONTEND_URL=https://ci.example.test\n",
            encoding="utf-8",
        )
        for name in ["backups", "plugins", "logs", "media"]:
            (data / name).mkdir(parents=True, exist_ok=True)
        memory_info.write_text("MemAvailable:    2097152 kB\n", encoding="utf-8")
        runner = FakeRunner()
        statuses = http_statuses or {}
        probes = []

        def http_probe(path, *, host, port, forwarded_proto):
            probes.append((path, host, port, forwarded_proto))
            return statuses.get(path, 200)

        deployment = ImmutableComposeDeployment(
            HostPaths.testing(app=app, data=data, state=state),
            runner=runner,
            stable_observations=1,
            memory_info_path=memory_info,
            http_probe=http_probe,
        )
        return deployment, runner, probes

    def write_backup(self, deployment, *, created_at=None, content=b"SELECT 1;\n", compressed=None):
        backup_root = deployment.paths.data_root / "backups"
        backup = backup_root / "verified.sql.gz"
        if compressed is None:
            with gzip.open(backup, "wb") as handle:
                handle.write(content)
        else:
            backup.write_bytes(compressed)
        metadata = {
            "path": str(backup),
            "createdAt": (created_at or datetime.now(timezone.utc)).isoformat().replace("+00:00", "Z"),
            "compressedSha256": hashlib.sha256(backup.read_bytes()).hexdigest(),
        }
        (backup_root / "verified.sql.gz.json").write_text(json.dumps(metadata), encoding="utf-8")

    def test_pull_and_switch_use_only_exact_digest_api_and_web(self):
        with tempfile.TemporaryDirectory() as directory:
            deployment, runner, _ = self.make(directory)
            target = manifest()

            deployment.pull(target)
            deployment.switch(target)

            commands = [call[0] for call in runner.calls]
            self.assertIn(("/usr/bin/docker", "pull", "ghcr.io/yanyuhanyue/animemo-api@" + target["images"]["api"]["digest"]), commands)
            self.assertIn(("/usr/bin/docker", "pull", "ghcr.io/yanyuhanyue/animemo-web@" + target["images"]["web"]["digest"]), commands)
            switch = commands[-1]
            self.assertEqual(switch[-6:], ("up", "-d", "--no-deps", "--force-recreate", "api", "web"))
            self.assertNotIn("postgres", switch)
            self.assertNotIn("redis", switch)

    def test_switch_atomically_replaces_a_linked_runtime_env_without_mutating_the_source(self):
        with tempfile.TemporaryDirectory() as directory:
            deployment, _, _ = self.make(directory)
            target = manifest()
            outside = Path(directory) / "outside.env"
            outside.write_text("DO_NOT_CHANGE=1\n", encoding="utf-8")
            deployment.paths.state_root.mkdir(parents=True, exist_ok=True)
            try:
                deployment.runtime_env.symlink_to(outside)
            except OSError:
                deployment.runtime_env.hardlink_to(outside)

            deployment.switch(target)

            self.assertEqual(outside.read_text(encoding="utf-8"), "DO_NOT_CHANGE=1\n")
            self.assertFalse(deployment.runtime_env.is_symlink())
            self.assertEqual(
                deployment.runtime_env.read_text(encoding="utf-8"),
                "ANIMEMO_API_IMAGE=ghcr.io/yanyuhanyue/animemo-api@"
                + target["images"]["api"]["digest"]
                + "\nANIMEMO_WEB_IMAGE=ghcr.io/yanyuhanyue/animemo-web@"
                + target["images"]["web"]["digest"]
                + "\n",
            )

    def test_switch_does_not_follow_a_precreated_atomic_temporary_link(self):
        with tempfile.TemporaryDirectory() as directory:
            deployment, _, _ = self.make(directory)
            outside = Path(directory) / "outside.tmp"
            outside.write_text("DO_NOT_CHANGE=1\n", encoding="utf-8")
            deployment.paths.state_root.mkdir(parents=True, exist_ok=True)
            predictable = deployment.runtime_env.with_name(
                f".{deployment.runtime_env.name}.{os.getpid()}.tmp"
            )
            predictable.hardlink_to(outside)

            deployment.switch(manifest())

            self.assertEqual(outside.read_text(encoding="utf-8"), "DO_NOT_CHANGE=1\n")

    def test_migration_and_bootstrap_use_target_api_image_jobs(self):
        with tempfile.TemporaryDirectory() as directory:
            deployment, runner, _ = self.make(directory)
            target = manifest()

            deployment.migrate(target)
            deployment.bootstrap(target)

            commands = [call[0] for call in runner.calls]
            self.assertEqual(commands[0][-4:], ("run", "--rm", "--no-deps", "migration"))
            self.assertEqual(commands[1][-4:], ("run", "--rm", "--no-deps", "bootstrap"))
            for _, kwargs in runner.calls:
                self.assertEqual(kwargs["env"]["ANIMEMO_API_IMAGE"], "ghcr.io/yanyuhanyue/animemo-api@" + target["images"]["api"]["digest"])

    def test_custom_or_symlink_escape_paths_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaises(ValueError):
                HostPaths.production(app_root=root / "attacker")

    def test_recent_backup_requires_fresh_metadata_and_valid_gzip_stream(self):
        with tempfile.TemporaryDirectory() as directory:
            deployment, _, _ = self.make(directory)
            self.write_backup(deployment)
            deployment.verify_recent_backup()

            self.write_backup(deployment, created_at=datetime.now(timezone.utc) - timedelta(hours=25))
            with self.assertRaisesRegex(Exception, "older than"):
                deployment.verify_recent_backup()

            self.write_backup(deployment, compressed=b"not-gzip")
            with self.assertRaisesRegex(Exception, "gzip"):
                deployment.verify_recent_backup()

    def test_recent_backup_rejects_linked_metadata_and_backup_files(self):
        with tempfile.TemporaryDirectory() as directory:
            deployment, _, _ = self.make(directory)
            backup_root = deployment.paths.data_root / "backups"
            outside_backup = Path(directory) / "outside.sql.gz"
            with gzip.open(outside_backup, "wb") as handle:
                handle.write(b"SELECT 1;\n")
            linked_backup = backup_root / "linked.sql.gz"
            linked_backup.hardlink_to(outside_backup)
            metadata_payload = {
                "path": str(linked_backup),
                "createdAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "compressedSha256": hashlib.sha256(outside_backup.read_bytes()).hexdigest(),
            }
            metadata = backup_root / "linked.sql.gz.json"
            metadata.write_text(json.dumps(metadata_payload), encoding="utf-8")

            with self.assertRaisesRegex(Exception, "link"):
                deployment.verify_recent_backup()

            linked_backup.unlink()
            metadata.unlink()
            outside_metadata = Path(directory) / "outside.json"
            outside_metadata.write_text(json.dumps(metadata_payload), encoding="utf-8")
            metadata.hardlink_to(outside_metadata)

            with self.assertRaisesRegex(Exception, "link"):
                deployment.verify_recent_backup()

    def test_recent_backup_reports_invalid_metadata_as_state_error(self):
        with tempfile.TemporaryDirectory() as directory:
            deployment, _, _ = self.make(directory)
            metadata = deployment.paths.data_root / "backups" / "latest.json"
            metadata.write_text("{not-json", encoding="utf-8")

            with self.assertRaisesRegex(StateError, "metadata is invalid"):
                deployment.verify_recent_backup()

    def test_recent_backup_rejects_metadata_without_a_backup_path(self):
        with tempfile.TemporaryDirectory() as directory:
            deployment, _, _ = self.make(directory)
            metadata = deployment.paths.data_root / "backups" / "latest.json"
            metadata.write_text(
                json.dumps(
                    {
                        "createdAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                        "compressedSha256": "0" * 64,
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(StateError, "metadata is invalid"):
                deployment.verify_recent_backup()

    def test_preflight_blocks_when_available_memory_is_below_the_host_floor(self):
        with tempfile.TemporaryDirectory() as directory:
            deployment, _, _ = self.make(directory)
            deployment.memory_info_path.write_text("MemAvailable:    262144 kB\n", encoding="utf-8")

            with self.assertRaisesRegex(Exception, "memory"):
                deployment.preflight(manifest())

    def test_preflight_requires_all_current_services_to_be_healthy(self):
        with tempfile.TemporaryDirectory() as directory:
            deployment, runner, _ = self.make(directory)
            runner.service_health["redis"] = "unhealthy 0"

            with self.assertRaisesRegex(Exception, "redis"):
                deployment.preflight(manifest())

    def test_preflight_requires_current_api_and_web_http_contracts(self):
        with tempfile.TemporaryDirectory() as directory:
            deployment, _, probes = self.make(directory, http_statuses={"/health/": 503})

            with self.assertRaisesRegex(Exception, "/health/"):
                deployment.preflight(manifest())
            self.assertIn(("/health/", "ci.example.test", 8088, "https"), probes)

    def test_preflight_requires_backup_availability_before_release_work(self):
        with tempfile.TemporaryDirectory() as directory:
            deployment, _, _ = self.make(directory)

            with self.assertRaisesRegex(Exception, "backup"):
                deployment.preflight(manifest())

            self.write_backup(deployment)
            deployment.preflight(manifest())

    def test_stable_window_checks_all_public_contract_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            deployment, _, probes = self.make(directory)

            deployment.verify_health(manifest())

            self.assertEqual(
                [probe[0] for probe in probes],
                ["/health/", "/", "/login", "/api/schema/", "/api/docs/"],
            )

    def test_stable_window_rejects_http_5xx_and_critical_logs(self):
        for log_line in [
            '127.0.0.1 - - "GET / HTTP/1.1" 502 173',
            "Traceback (most recent call last):",
        ]:
            with self.subTest(log_line=log_line), tempfile.TemporaryDirectory() as directory:
                deployment, runner, _ = self.make(directory)
                runner.container_logs["web"] = log_line

                with self.assertRaisesRegex(Exception, "logs"):
                    deployment.verify_health(manifest())

    def test_stable_window_scans_container_stderr(self):
        with tempfile.TemporaryDirectory() as directory:
            deployment, runner, _ = self.make(directory)
            runner.container_errors["api"] = "Traceback (most recent call last):"

            with self.assertRaisesRegex(Exception, "logs"):
                deployment.verify_health(manifest())


if __name__ == "__main__":
    unittest.main()
