from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from durability.platform import parse_platform_qualification
from scripts import ci_smoke_installer_linux as smoke


class LinuxInstallerSmokeTests(unittest.TestCase):
    def test_schema_fixture_is_explicitly_synthetic_and_parses_normally(self):
        payload = (smoke.ROOT / smoke.PLATFORM_FIXTURE).read_bytes()
        qualification = parse_platform_qualification(payload)
        self.assertEqual(qualification.candidate_sha, "a" * 40)
        self.assertEqual(qualification.as_dict()["host"]["distributionId"], "NON_AUTHORITATIVE_SCHEMA_FIXTURE")
        source = (smoke.ROOT / "scripts/ci_smoke_installer_linux.py").read_text(encoding="utf-8")
        self.assertNotIn("scripts.platform_qualification", source)
        self.assertNotIn("release-output", source)
        self.assertIn('"qualification_or_release_authority_granted": False', source)

    def test_go_build_uses_locked_real_compiler_and_only_owned_writable_mount(self):
        authority = json.loads((smoke.ROOT / "release/producer-toolchain.lock.json").read_bytes())["byteAuthority"]
        with mock.patch.object(os, "getuid", return_value=1001, create=True), mock.patch.object(os, "getgid", return_value=1001, create=True):
            command = smoke.go_build_command(Path("/checkout"), Path("/owned/verifiers"), authority)
        self.assertEqual(command[:3], ["docker", "--host", "unix:///var/run/docker.sock"])
        self.assertIn(authority["releaseProducer"]["goBase"], command)
        self.assertIn("type=bind,src=/checkout/release/release_attestation_verifier,dst=/source,readonly", [part.replace("\\", "/") for part in command])
        self.assertIn("type=bind,src=/owned/verifiers,dst=/output", [part.replace("\\", "/") for part in command])
        self.assertIn("--read-only", command)
        self.assertIn("go mod verify", smoke._GO_BUILD)
        self.assertIn("GOOS=linux go build -mod=readonly", smoke._GO_BUILD)
        self.assertIn("GOOS=windows go build -mod=readonly", smoke._GO_BUILD)
        authority["releaseProducer"]["goBase"] = "golang:latest"
        with self.assertRaisesRegex(ValueError, "locked Go image digest"):
            smoke.go_build_command(Path("/checkout"), Path("/owned/verifiers"), authority)

    def test_stage_copies_current_fixed_helpers_without_inheriting_untracked_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "checkout"
            root.mkdir()
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            names = (*smoke._FIXED_DEPLOYMENT_FILES, *(name + "/__init__.py" for name in ("durability", "release", "updater", "installer")), smoke.PLATFORM_FIXTURE)
            for relative in names:
                target = root / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(relative, encoding="utf-8")
            subprocess.run(["git", "add", "--all"], cwd=root, check=True, capture_output=True)
            (root / "release/untracked.py").write_text("ambient", encoding="utf-8")
            destination = Path(temporary) / "stage"
            smoke.stage_source(root, destination)
            self.assertEqual((destination / "scripts/release_qualification.py").read_text(encoding="utf-8"), "scripts/release_qualification.py")
            self.assertFalse((destination / "release/untracked.py").exists())
            self.assertEqual((destination / "release/platform-qualification.json").read_text(encoding="utf-8"), smoke.PLATFORM_FIXTURE)

    def test_command_environment_removes_ambient_python_and_docker_settings(self):
        with mock.patch.dict(os.environ, {"PYTHONPATH": "ambient", "DOCKER_HOST": "tcp://remote", "PIP_INDEX_URL": "https://credentials.invalid"}):
            clean = smoke.environment(Path("/owned"))
        self.assertNotIn("PYTHONPATH", clean)
        self.assertNotIn("DOCKER_HOST", clean)
        self.assertNotIn("PIP_INDEX_URL", clean)
        self.assertEqual(clean["PIP_CONFIG_FILE"], os.devnull)


if __name__ == "__main__":
    unittest.main()
