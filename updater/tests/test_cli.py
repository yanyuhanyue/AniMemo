from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from release.contract import build_manifest
from updater.__main__ import main
from updater.errors import StateError
from updater.runtime import HostAgentRuntime


def manifest():
    return build_manifest(
        version="v1.0.0",
        channel="stable",
        commit="1" * 40,
        created_at=datetime(2026, 8, 12, tzinfo=timezone.utc),
        api_digest="sha256:" + "a" * 64,
        web_digest="sha256:" + "b" * 64,
        deployment_contract_sha256="sha256:0be5fdf5f87275755e06a2e2b6523c24e16d6aa1db48d8d58e8cfea969b674df",
        deployment_files=[
            {"path": "deploy/docker-compose.yml", "sha256": "sha256:" + "d" * 64},
            {"path": "updater/docker-compose.runtime.yml", "sha256": "sha256:" + "e" * 64},
        ],
        minimum_updater_version="1.0.0",
        database_contract="animemo-db-v1",
        database_accepts=["animemo-db-v1"],
        migration_required=False,
        migration_policy="none",
        application_rollback="safe",
        configuration_contract="animemo-config-v1",
        configuration_accepts=["animemo-config-v1"],
        plugin_sdk_apis=[1, 2],
        promoted_from="v1.0.0-rc.1",
    )


class FakeRuntime:
    def __init__(self):
        self.calls = []

    def serve_forever(self):
        self.calls.append("serve")

    def status(self):
        self.calls.append("status")
        return {"updaterVersion": "1.0.0", "current": {"version": "v1.0.0"}}

    def import_current(self):
        self.calls.append("import-current")
        return {"version": "v1.0.0"}

    def reconcile(self, operation_id, confirmation):
        self.calls.append(("reconcile", operation_id, confirmation))
        return {"id": operation_id, "status": "reconciled"}


class HostAgentCliTests(unittest.TestCase):
    def test_cli_exposes_only_fixed_lifecycle_commands(self):
        runtime = FakeRuntime()
        output = io.StringIO()

        with patch("updater.__main__.production_runtime", return_value=runtime), redirect_stdout(output):
            self.assertEqual(main(["status"]), 0)
            self.assertEqual(main(["import-current"]), 0)
            self.assertEqual(main([
                "reconcile",
                "--operation-id", "a" * 32,
                "--confirmation", "RECONCILE " + "a" * 32,
            ]), 0)
            self.assertEqual(main(["serve"]), 0)

        self.assertEqual(runtime.calls, [
            "status",
            "import-current",
            ("reconcile", "a" * 32, "RECONCILE " + "a" * 32),
            "serve",
        ])
        documents = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(documents[0]["current"]["version"], "v1.0.0")
        self.assertEqual(documents[1], {"version": "v1.0.0"})
        self.assertEqual(documents[2]["status"], "reconciled")

    def test_cli_refuses_custom_production_paths_and_generic_commands(self):
        for argv in [
            ["serve", "--socket", "/tmp/attacker.sock"],
            ["import-current", "--manifest", "/tmp/release.json"],
            ["status", "--state-root", "/tmp/state"],
            ["reconcile", "--operation-id", "../escape", "--confirmation", "RECONCILE ../escape"],
            ["run-command", "docker", "ps"],
        ]:
            with self.subTest(argv=argv), self.assertRaises(SystemExit):
                main(argv)

    def test_import_current_validates_fixed_manifest_and_is_one_time(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = root / "app"
            data = root / "data"
            state = root / "state"
            socket_path = root / "run" / "updater.sock"
            bootstrap_manifest = state / "bootstrap" / "release-manifest.json"
            app.mkdir()
            data.mkdir()
            bootstrap_manifest.parent.mkdir(parents=True)
            bootstrap_manifest.write_text(json.dumps(manifest()), encoding="utf-8")
            runtime = HostAgentRuntime.testing(
                app_root=app,
                data_root=data,
                state_root=state,
                socket_path=socket_path,
                bootstrap_manifest=bootstrap_manifest,
            )

            with patch.object(runtime.deployment, "inspect_enabled_plugin_apis", return_value={2}):
                identity = runtime.import_current()

            self.assertEqual(identity["version"], "v1.0.0")
            self.assertEqual(runtime.slots.read()["current"], manifest())
            self.assertEqual(runtime.runtime_state.read()["databaseContract"], "animemo-db-v1")
            self.assertEqual(runtime.runtime_state.read()["enabledPluginApis"], [2])
            with self.assertRaisesRegex(StateError, "already initialized"):
                runtime.import_current()

    def test_import_current_rejects_invalid_manifest_without_partial_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = root / "app"
            data = root / "data"
            state = root / "state"
            socket_path = root / "run" / "updater.sock"
            bootstrap_manifest = state / "bootstrap" / "release-manifest.json"
            app.mkdir()
            data.mkdir()
            bootstrap_manifest.parent.mkdir(parents=True)
            bootstrap_manifest.write_text('{"schemaVersion": 1}', encoding="utf-8")
            runtime = HostAgentRuntime.testing(
                app_root=app,
                data_root=data,
                state_root=state,
                socket_path=socket_path,
                bootstrap_manifest=bootstrap_manifest,
            )

            with self.assertRaisesRegex(StateError, "failed validation"):
                runtime.import_current()

            self.assertIsNone(runtime.slots.read()["current"])
            self.assertFalse(runtime.runtime_state.path.exists())

    def test_reconcile_requires_exact_host_confirmation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = HostAgentRuntime.testing(
                app_root=root / "app",
                data_root=root / "data",
                state_root=root / "state",
                socket_path=root / "run" / "updater.sock",
                bootstrap_manifest=root / "state" / "bootstrap" / "release-manifest.json",
            )

            with self.assertRaisesRegex(StateError, "confirmation"):
                runtime.reconcile("a" * 32, "RECONCILE WRONG")


if __name__ == "__main__":
    unittest.main()
