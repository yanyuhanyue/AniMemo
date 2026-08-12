from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
COMMIT = "b" * 40
API_DIGEST = "sha256:" + "3" * 64
WEB_DIGEST = "sha256:" + "4" * 64


class ReleaseCliTests(unittest.TestCase):
    def run_cli(self, *arguments, expected=0):
        completed = subprocess.run(
            [sys.executable, "-m", "release.cli", *map(str, arguments)],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, expected, completed.stderr or completed.stdout)
        return completed

    def test_resolve_version_emits_json_and_github_outputs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tags = root / "tags.txt"
            outputs = root / "github-output.txt"
            tags.write_text("v1.0.0\nv1.0.1-beta.1\n", encoding="utf-8")
            completed = self.run_cli(
                "resolve-version",
                "--tags-file", tags,
                "--bump", "patch",
                "--channel", "beta",
                "--github-output", outputs,
            )
            payload = json.loads(completed.stdout)
            self.assertEqual(payload["releaseTag"], "v1.0.1-beta.2")
            self.assertIn("release_tag=v1.0.1-beta.2", outputs.read_text(encoding="utf-8"))

    def test_generate_validate_and_checksum_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "release-manifest.json"
            checksums = root / "checksums.txt"
            self.run_cli(
                "generate-manifest",
                "--version", "v1.0.0-rc.1",
                "--channel", "rc",
                "--commit", COMMIT,
                "--created-at", "2026-08-12T10:00:00Z",
                "--api-digest", API_DIGEST,
                "--web-digest", WEB_DIGEST,
                "--compatibility-file", ROOT / "release" / "compatibility.json",
                "--output", target,
            )
            self.run_cli("validate-manifest", "--manifest", target, "--updater-version", "1.0.0")
            self.run_cli("write-checksums", "--output", checksums, target)
            expected = hashlib.sha256(target.read_bytes()).hexdigest()
            self.assertEqual(checksums.read_text(encoding="utf-8"), f"{expected}  release-manifest.json\n")

    def test_cli_errors_are_machine_readable_and_nonzero(self):
        completed = self.run_cli(
            "resolve-version",
            "--tags-file", ROOT / "release" / "missing-tags.txt",
            "--bump", "patch",
            "--channel", "stable",
            expected=2,
        )
        payload = json.loads(completed.stderr)
        self.assertEqual(payload["code"], "release_contract_invalid")
        self.assertIn("detail", payload)


if __name__ == "__main__":
    unittest.main()
