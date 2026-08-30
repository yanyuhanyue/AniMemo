from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from release.producer_toolchain import (
    DOCKERFILE_PATH,
    LOCK_PATH,
    ProducerToolchainError,
    validate_producer_toolchain_receipt,
)


class ProducerToolchainReceiptTests(unittest.TestCase):
    candidate_sha = "a" * 40

    def test_entrypoint_binds_embedded_inputs_to_the_mounted_workspace(self):
        entrypoint = (
            LOCK_PATH.parents[1] / "scripts" / "release-producer-entrypoint.sh"
        ).read_text(encoding="utf-8")
        self.assertIn('"$GITHUB_WORKSPACE/release/requirements.lock"', entrypoint)
        self.assertIn(
            '"$GITHUB_WORKSPACE/deploy/release-producer.Dockerfile"',
            entrypoint,
        )
        self.assertNotIn("/workspace/release/requirements.lock", entrypoint)

    def test_go_temp_execution_is_scoped_to_a_private_runner_directory(self):
        wrapper = (
            LOCK_PATH.parents[1] / "scripts" / "run-in-release-producer.sh"
        ).read_text(encoding="utf-8")
        entrypoint = (
            LOCK_PATH.parents[1] / "scripts" / "release-producer-entrypoint.sh"
        ).read_text(encoding="utf-8")

        self.assertIn(
            'producer_gotmp="$RUNNER_TEMP/animemo-release-producer-gotmp"',
            wrapper,
        )
        self.assertIn(
            'install -d -m 0700 "$producer_home" "$producer_gotmp"',
            wrapper,
        )
        self.assertIn(
            "--tmpfs /tmp:rw,nosuid,nodev,noexec,mode=1777",
            wrapper,
        )
        self.assertIn('--env "GOTMPDIR=$producer_gotmp"', wrapper)
        self.assertNotIn("|GOTMPDIR|", wrapper)
        self.assertIn(
            'expected_gotmp="$RUNNER_TEMP/animemo-release-producer-gotmp"',
            entrypoint,
        )
        self.assertIn('"$GOTMPDIR" != "$expected_gotmp"', entrypoint)
        self.assertIn('! -d "$GOTMPDIR"', entrypoint)
        self.assertIn('-L "$GOTMPDIR"', entrypoint)
        self.assertIn('! -O "$GOTMPDIR"', entrypoint)
        self.assertIn('stat -c \'%a\' "$GOTMPDIR"', entrypoint)

    @staticmethod
    def _sha256(path: Path) -> str:
        return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()

    def _receipt(self) -> dict[str, object]:
        lock = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
        byte_lock = lock["byteAuthority"]
        return {
            "schemaVersion": "animemo.release-producer-toolchain-receipt.v1",
            "candidateSha": self.candidate_sha,
            "runner": {
                "label": "ubuntu-24.04",
                "os": "Linux",
                "arch": "X64",
                "imageOS": "ubuntu24",
                "imageVersion": "20260820.1.0",
                "observationOnly": True,
            },
            "byteAuthority": {
                "releaseProducer": {
                    "imageId": "sha256:" + "b" * 64,
                    "dockerfileSha256": self._sha256(DOCKERFILE_PATH),
                },
                "python": byte_lock["python"]["hostedRuntimeVersion"],
                "go": "go" + byte_lock["go"]["version"],
                "buildx": byte_lock["buildx"]["version"],
                "buildkit": byte_lock["buildkit"]["version"],
                "buildkitImage": byte_lock["buildkit"]["image"],
                "backendImage": byte_lock["python"]["backendImage"],
                "nodeImage": byte_lock["node"]["image"],
                "npm": byte_lock["npm"],
            },
            "toolchainLockSha256": self._sha256(LOCK_PATH),
        }

    def _write(self, root: Path, value: object) -> Path:
        target = root / "receipt.json"
        target.write_text(json.dumps(value), encoding="utf-8")
        return target

    def test_exact_receipt_is_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            target = self._write(Path(temporary), self._receipt())
            validated = validate_producer_toolchain_receipt(
                target,
                expected_candidate_sha=self.candidate_sha,
            )
            self.assertEqual(validated["candidateSha"], self.candidate_sha)

    def test_authority_and_runner_tamper_fail_closed(self) -> None:
        mutations = (
            ("runner observation", ("runner", "observationOnly"), False),
            ("runner identity", ("runner", "imageVersion"), ""),
            (
                "producer dockerfile",
                ("byteAuthority", "releaseProducer", "dockerfileSha256"),
                "sha256:" + "0" * 64,
            ),
            ("python", ("byteAuthority", "python"), "3.12.11"),
            ("lock", ("toolchainLockSha256",), "sha256:" + "0" * 64),
        )
        for label, path, replacement in mutations:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temporary:
                value = copy.deepcopy(self._receipt())
                cursor = value
                for part in path[:-1]:
                    cursor = cursor[part]
                cursor[path[-1]] = replacement
                target = self._write(Path(temporary), value)
                with self.assertRaises(ProducerToolchainError):
                    validate_producer_toolchain_receipt(
                        target,
                        expected_candidate_sha=self.candidate_sha,
                    )
