from __future__ import annotations

import io
import json
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from scripts.offline_authority_state import main
from scripts.tests.trust_kit_fixture import (
    create_test_initial_trust_kit,
    simulated_test_root_ownership,
)
from updater.offline import PretrustedTrustMaterial


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


class OfflineAuthorityStateMigrationCliTests(unittest.TestCase):
    @staticmethod
    def _trust_root(root: Path) -> tuple[Path, object]:
        trust = create_test_initial_trust_kit(root)
        (trust / "initial-trust-bootstrap.json").unlink()
        (trust / "offline-release-verifier").chmod(0o755)
        with simulated_test_root_ownership():
            return trust, PretrustedTrustMaterial.load(trust).profile

    def _run(self, state: Path, trust: Path) -> subprocess.CompletedProcess[str]:
        argv = [
            "migrate-pristine-v1",
            "--state",
            str(state),
            "--trust-material-root",
            str(trust),
        ]
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            simulated_test_root_ownership(),
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            returncode = main(argv)
        return subprocess.CompletedProcess(
            args=argv,
            returncode=returncode,
            stdout=stdout.getvalue(),
            stderr=stderr.getvalue(),
        )

    def test_cli_atomically_migrates_only_pristine_v1(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            trust, profile = self._trust_root(root)
            state = root / "authority-state.json"
            state.write_bytes(
                _canonical(
                    {
                        "acceptedPublicationIdentities": [],
                        "activeProfileIdentity": profile.identity,
                        "activeProfileVersion": profile.profile_version,
                        "generation": 0,
                        "highestReleaseVersion": None,
                        "revokedEvidenceIdentities": [],
                        "schemaVersion": 1,
                    }
                )
            )

            completed = self._run(state, trust)

            self.assertEqual(completed.returncode, 0, completed.stderr)
            migrated = json.loads(state.read_text(encoding="utf-8"))
            self.assertEqual(migrated["schemaVersion"], 2)
            self.assertEqual(migrated["acceptedAuthorityIdentities"], [])
            self.assertFalse(state.with_suffix(".json.migration.new").exists())
            self.assertFalse(state.with_suffix(".json.migration.lock").exists())

    def test_cli_rejects_history_bearing_v1_without_reinterpreting_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            trust, profile = self._trust_root(root)
            state = root / "authority-state.json"
            original = _canonical(
                {
                    "acceptedPublicationIdentities": ["sha256:" + "a" * 64],
                    "activeProfileIdentity": profile.identity,
                    "activeProfileVersion": profile.profile_version,
                    "generation": 1,
                    "highestReleaseVersion": "v1.0.0",
                    "revokedEvidenceIdentities": [],
                    "schemaVersion": 1,
                }
            )
            state.write_bytes(original)

            completed = self._run(state, trust)

            self.assertEqual(completed.returncode, 2)
            self.assertEqual(
                json.loads(completed.stderr)["code"],
                "offline_authority_state_migration_rejected",
            )
            self.assertEqual(state.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
