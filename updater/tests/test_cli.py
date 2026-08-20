from __future__ import annotations

import argparse
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from unittest.mock import patch

from release.contract import build_manifest
from updater.__main__ import _listen, main
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
        installer_materials_sha256="sha256:" + "f" * 64,
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

    def reconcile(self, operation_id, confirmation):
        self.calls.append(("reconcile", operation_id, confirmation))
        return {"id": operation_id, "status": "reconciled"}


class HostAgentCliTests(unittest.TestCase):
    def test_configuration_cli_has_fixed_show_validate_plan_and_apply_surface(self):
        with patch("updater.__main__._run_configuration", return_value=0) as run:
            self.assertEqual(main(["config", "show"]), 0)
            self.assertEqual(
                main(
                    [
                        "config",
                        "apply",
                        "--public-origin",
                        "https://new.example",
                        "--listen",
                        "127.0.0.2:18088",
                        "--accept",
                    ]
                ),
                0,
            )
        self.assertEqual(run.call_count, 2)
        applied = run.call_args_list[1].args[0]
        self.assertEqual(applied.listen.host, "127.0.0.2")
        self.assertEqual(applied.listen.port, 18088)
        self.assertTrue(applied.accept)

    def test_configuration_listen_parser_rejects_noncanonical_or_invalid_input(self):
        self.assertEqual(_listen("127.0.0.2:8088").port, 8088)
        for value in ("localhost:8088", "127.0.0.1:0", "127.000.0.1:8088"):
            with self.subTest(value=value), self.assertRaises(
                argparse.ArgumentTypeError
            ):
                _listen(value)

    def test_configuration_apply_requires_acceptance_with_stable_error(self):
        plan = SimpleNamespace(plan_digest="sha256:" + "a" * 64)
        manager = mock.Mock()
        manager.validate.return_value = plan
        errors = io.StringIO()
        with (
            patch(
                "updater.configuration.build_configuration_manager",
                return_value=manager,
            ),
            redirect_stderr(errors),
        ):
            self.assertEqual(
                main(
                    [
                        "config",
                        "apply",
                        "--public-origin",
                        "https://new.example",
                    ]
                ),
                1,
            )
        payload = json.loads(errors.getvalue())
        self.assertEqual(payload["error"]["code"], "CONFIG_PLAN_ACCEPTANCE_REQUIRED")
        manager.apply.assert_not_called()

    def test_configuration_apply_exit_codes_distinguish_recovery_and_failure(self):
        plan = SimpleNamespace(plan_digest="sha256:" + "a" * 64)
        manager = mock.Mock()
        manager.validate.return_value = plan
        output = io.StringIO()

        for manual_recovery_required, outcome, expected in (
            (True, "RECOVERY_REQUIRED", 5),
            (False, "CONFIG_APPLY_FAILED", 6),
        ):
            result = SimpleNamespace(
                manual_recovery_required=manual_recovery_required,
                outcome=SimpleNamespace(value=outcome),
                as_dict=lambda value=outcome: {"outcome": value},
            )
            manager.apply.return_value = result
            with (
                self.subTest(outcome=outcome),
                patch(
                    "updater.configuration.build_configuration_manager",
                    return_value=manager,
                ),
                redirect_stdout(output),
            ):
                self.assertEqual(
                    main(
                        [
                            "config",
                            "apply",
                            "--public-origin",
                            "https://new.example",
                            "--accept",
                        ]
                    ),
                    expected,
                )

    def test_cli_exposes_only_fixed_lifecycle_commands(self):
        runtime = FakeRuntime()
        output = io.StringIO()

        with patch("updater.__main__.production_runtime", return_value=runtime), redirect_stdout(output):
            self.assertEqual(main(["status"]), 0)
            self.assertEqual(main([
                "reconcile",
                "--operation-id", "a" * 32,
                "--confirmation", "RECONCILE " + "a" * 32,
            ]), 0)
            self.assertEqual(main(["serve"]), 0)

        self.assertEqual(runtime.calls, [
            "status",
            ("reconcile", "a" * 32, "RECONCILE " + "a" * 32),
            "serve",
        ])
        documents = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(documents[0]["current"]["version"], "v1.0.0")
        self.assertEqual(documents[1]["status"], "reconciled")

    def test_cli_refuses_custom_production_paths_and_generic_commands(self):
        for argv in [
            ["serve", "--socket", "/tmp/attacker.sock"],
            ["import-current"],
            ["adopt-current", "--manifest", "/tmp/release.json"],
            ["status", "--state-root", "/tmp/state"],
            ["reconcile", "--operation-id", "../escape", "--confirmation", "RECONCILE ../escape"],
            ["run-command", "docker", "ps"],
        ]:
            with self.subTest(argv=argv), self.assertRaises(SystemExit):
                main(argv)

    def test_adopt_current_uses_only_the_fixed_request_boundary(self):
        request = object()
        receipt = mock.Mock()
        receipt.as_dict.return_value = {
            "operationId": "a" * 32,
            "locatorDigest": "sha256:" + "b" * 64,
        }
        output = io.StringIO()
        with (
            patch("updater.__main__.load_initial_adoption_request", return_value=request),
            patch("updater.__main__.adopt_initial_release", return_value=receipt) as adopt,
            redirect_stdout(output),
        ):
            self.assertEqual(main(["adopt-current"]), 0)
        adopt.assert_called_once_with(request)
        self.assertEqual(json.loads(output.getvalue())["operationId"], "a" * 32)

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
