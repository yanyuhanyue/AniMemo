from __future__ import annotations

import gzip
import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from release.contract import build_manifest
from updater.deployment import HostPaths, ImmutableComposeDeployment


class FakeRunner:
    def __init__(self):
        self.calls = []

    def run(self, argv, **kwargs):
        self.calls.append((tuple(argv), kwargs))
        stdout = ""
        if "ps" in argv and "--services" in argv:
            stdout = "postgres\nredis\napi\nweb\n"
        elif argv[:2] == ["/usr/bin/docker", "inspect"]:
            stdout = "healthy 0\n"
        elif "list_enabled_plugin_apis" in " ".join(argv):
            stdout = '[2]\n'
        return type("Result", (), {"stdout": stdout})()

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
    def make(self, directory):
        root = Path(directory)
        app = root / "app"
        data = root / "data"
        state = root / "state"
        (app / "deploy").mkdir(parents=True)
        (app / "deploy" / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
        (app / ".env.production").write_text(
            "POSTGRES_USER=anime_journal\nPOSTGRES_DB=anime_journal\nFRONTEND_URL=https://ci.example.test\n",
            encoding="utf-8",
        )
        for name in ["backups", "plugins", "logs", "media"]:
            (data / name).mkdir(parents=True, exist_ok=True)
        runner = FakeRunner()
        deployment = ImmutableComposeDeployment(HostPaths.testing(app=app, data=data, state=state), runner=runner, stable_observations=1)
        return deployment, runner

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
            deployment, runner = self.make(directory)
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

    def test_migration_and_bootstrap_use_target_api_image_jobs(self):
        with tempfile.TemporaryDirectory() as directory:
            deployment, runner = self.make(directory)
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
            deployment, _ = self.make(directory)
            self.write_backup(deployment)
            deployment.verify_recent_backup()

            self.write_backup(deployment, created_at=datetime.now(timezone.utc) - timedelta(hours=25))
            with self.assertRaisesRegex(Exception, "older than"):
                deployment.verify_recent_backup()

            self.write_backup(deployment, compressed=b"not-gzip")
            with self.assertRaisesRegex(Exception, "gzip"):
                deployment.verify_recent_backup()


if __name__ == "__main__":
    unittest.main()
