from __future__ import annotations

import base64
import io
import json
import os
import tempfile
import unittest
import zipfile
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from release.candidate import canonical_json_bytes
from scripts import candidate_profile_runner as runner

DIGEST = "sha256:" + "a" * 64


def _context(profile: str = "RUNTIME_BASE_OFFLINE"):
    return {
        "base_vm_identity": "sha256:" + "1" * 64,
        "clone_identity": "sha256:" + "2" * 64,
        "initial_platform_state": {
            "docker_present": True,
            "runtime_dependencies_present": True,
            "network_allowed": profile != "RUNTIME_BASE_OFFLINE",
        },
        "original_vm_pre_hashes": {"base.vmx": "sha256:" + "3" * 64},
        "profile": profile,
        "snapshot_identity": "sha256:" + "4" * 64,
    }


def _encoded_context(profile: str = "RUNTIME_BASE_OFFLINE") -> str:
    return base64.urlsafe_b64encode(canonical_json_bytes(_context(profile))).decode().rstrip("=")


def _loaded(root: Path):
    images = tuple(
        SimpleNamespace(role=role, digest=DIGEST)
        for role in ("api", "postgres", "redis", "web")
    )
    return SimpleNamespace(
        root=root,
        verified_digest="sha256:" + "5" * 64,
        verified={"candidate_input_sha256": "sha256:" + "6" * 64},
        candidate_input={
            "qualification_run_id": 123,
            "qualification_run_attempt": 1,
            "source_sha": "b" * 40,
            "source_tree": "c" * 40,
            "candidate_version": "v1.1.0-rc.14",
        },
        manifest={},
        images=SimpleNamespace(images=images),
    )


def _installer_output():
    return {
        "platformPlan": {},
        "platformBootstrapReceipt": {
            "planDigest": "sha256:" + "7" * 64,
            "actions": [],
            "result": "PASS",
        },
        "strictPostProvisionQualification": True,
        "installerPlanDigest": "sha256:" + "8" * 64,
        "installerResult": {"outcome": "SUCCEEDED"},
    }


class FakeRunner:
    def __init__(self):
        self.calls = []
        self.runtime_paths = []
        self.runtime_dependency_visible = []

    def run(self, argv, environment):
        self.calls.append((argv, environment))
        python_paths = environment["PYTHONPATH"].split(os.pathsep)
        self.runtime_paths.append(Path(python_paths[0]))
        self.runtime_dependency_visible.append(
            (Path(python_paths[0]) / "offline_dependency" / "__init__.py").is_file()
        )
        return 0, json.dumps(_installer_output()).encode(), b"secret-free"


def _write_test_wheelhouse(root: Path) -> None:
    wheelhouse = root / "installer-root" / "wheelhouse"
    wheelhouse.mkdir(parents=True)
    with zipfile.ZipFile(
        wheelhouse / "offline_dependency-1.0-py3-none-any.whl", "w"
    ) as archive:
        archive.writestr("offline_dependency/__init__.py", b"VERIFIED = True\n")


class CandidateProfileRunnerTests(unittest.TestCase):
    def test_installer_command_is_fixed_argv_without_release_discovery(self):
        argv = runner.installer_argv(
            verified_candidate_digest=DIGEST,
            profile="FRESH_BASE",
            public_origin="https://candidate.rc14.invalid",
        )
        self.assertEqual(argv[1:4], ("-m", "installer", "candidate"))
        self.assertIn("--verified-candidate-digest", argv)
        self.assertNotIn("--source", argv)
        self.assertNotIn("--bundle-payload", argv)
        self.assertNotIn("latest", argv)
        with self.assertRaisesRegex(runner.ProfileRunnerError, "ORIGIN_INVALID"):
            runner.installer_argv(
                verified_candidate_digest=DIGEST,
                profile="FRESH_BASE",
                public_origin="https://user@example.invalid/",
            )

    def test_context_is_closed_and_rejects_policy_override(self):
        self.assertEqual(
            runner._decode_context(_encoded_context())["profile"],
            "RUNTIME_BASE_OFFLINE",
        )
        invalid = _context()
        invalid["network_policy_override"] = True
        encoded = base64.urlsafe_b64encode(canonical_json_bytes(invalid)).decode().rstrip("=")
        with self.assertRaisesRegex(runner.ProfileRunnerError, "CONTEXT_INVALID"):
            runner._decode_context(encoded)
        with self.assertRaisesRegex(runner.ProfileRunnerError, "RESULT_INVALID"):
            runner._result_json(b'{"outcome":"SUCCEEDED","outcome":"FAILED"}')

    def test_offline_execution_receipt_has_zero_network_apt_and_pull(self):
        with tempfile.TemporaryDirectory() as temporary:
            loaded = _loaded(Path(temporary))
            _write_test_wheelhouse(loaded.root)
            fake = FakeRunner()
            parsed_plan = SimpleNamespace(
                plan_digest="sha256:" + "7" * 64,
                mode=SimpleNamespace(value="OFFLINE_VALIDATE_ONLY"),
                initial_capabilities=SimpleNamespace(
                    docker_cli_present=True,
                    docker_daemon_healthy=True,
                    compose_v2_present=True,
                    pg_dump_major=16,
                    psql_major=16,
                ),
                network_policy="DENY_ALL",
                actions=(),
            )
            parsed_receipt = SimpleNamespace(
                result="PASS",
                plan_digest=parsed_plan.plan_digest,
            )
            with mock.patch(
                "scripts.candidate_profile_runner.load_verified_candidate",
                return_value=loaded,
            ), mock.patch(
                "scripts.candidate_profile_runner.parse_platform_bootstrap_plan",
                return_value=parsed_plan,
            ), mock.patch(
                "scripts.candidate_profile_runner.parse_platform_bootstrap_receipt",
                return_value=parsed_receipt,
            ):
                receipt = runner.execute_profile(
                    verified_candidate_digest=loaded.verified_digest,
                    profile="RUNTIME_BASE_OFFLINE",
                    public_origin="https://candidate.rc14.invalid",
                    context_b64url=_encoded_context(),
                    runner=fake,
                )
        self.assertEqual(receipt["result"], "PASS")
        self.assertEqual(receipt["network_request_count"], 0)
        self.assertEqual(receipt["apt_command_count"], 0)
        self.assertEqual(receipt["external_pull_count"], 0)
        self.assertFalse(receipt["release_authority_granted"])
        self.assertEqual(len(fake.calls), 1)
        python_paths = fake.calls[0][1]["PYTHONPATH"].split(os.pathsep)
        self.assertEqual(len(python_paths), 2)
        self.assertEqual(
            python_paths[1], str(loaded.root / "installer-root")
        )
        self.assertEqual(fake.calls[0][1]["PYTHONSAFEPATH"], "1")
        self.assertEqual(fake.runtime_dependency_visible, [True])
        self.assertFalse(fake.runtime_paths[0].exists())

    def test_missing_wheelhouse_stops_before_installer(self):
        with tempfile.TemporaryDirectory() as temporary:
            loaded = _loaded(Path(temporary))
            fake = FakeRunner()
            with mock.patch(
                "scripts.candidate_profile_runner.load_verified_candidate",
                return_value=loaded,
            ), self.assertRaisesRegex(
                runner.ProfileRunnerError, "CANDIDATE_PROFILE_RUNTIME_INVALID"
            ):
                runner.execute_profile(
                    verified_candidate_digest=loaded.verified_digest,
                    profile="RUNTIME_BASE_OFFLINE",
                    public_origin="https://candidate.rc14.invalid",
                    context_b64url=_encoded_context(),
                    runner=fake,
                )
        self.assertEqual(fake.calls, [])

    def test_profile_mismatch_stops_before_installer(self):
        fake = FakeRunner()
        with self.assertRaisesRegex(runner.ProfileRunnerError, "CONTEXT_MISMATCH"):
            runner.execute_profile(
                verified_candidate_digest=DIGEST,
                profile="FRESH_BASE",
                public_origin="https://candidate.rc14.invalid",
                context_b64url=_encoded_context(),
                runner=fake,
            )
        self.assertEqual(fake.calls, [])

    def test_cli_defaults_to_plan_only(self):
        output = io.StringIO()
        with redirect_stdout(output):
            code = runner.main(
                [
                    "--verified-candidate-digest",
                    DIGEST,
                    "--profile",
                    "FRESH_BASE",
                    "--public-origin",
                    "https://candidate.rc14.invalid",
                ]
            )
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue())["mode"], "PLAN_ONLY")


if __name__ == "__main__":
    unittest.main()
