from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from release import cli


class CandidateVerifierCliTests(unittest.TestCase):
    def _arguments(self, root: Path) -> list[str]:
        metadata = root / "metadata.json"
        metadata.write_text("{}\n", encoding="utf-8")
        archive = root / "candidate.zip"
        archive.write_bytes(b"candidate")
        return [
            "verify-prepublication-candidate",
            "--archive",
            str(archive),
            "--run-metadata",
            str(metadata),
            "--jobs-metadata",
            str(metadata),
            "--artifacts-metadata",
            str(metadata),
            "--containing-artifact-id",
            "99",
            "--containing-artifact-api-digest",
            "sha256:" + "a" * 64,
            "--expected-run-id",
            "1234",
            "--expected-source-sha",
            "b" * 40,
            "--expected-source-tree",
            "c" * 40,
            "--expected-candidate-version",
            "v1.1.0-rc.14",
            "--verified-at",
            "2026-08-25T12:00:00Z",
        ]

    def test_verified_at_is_documented_as_execution_receipt_only(self):
        parser = cli._parser()
        with contextlib.redirect_stdout(io.StringIO()) as output, self.assertRaises(
            SystemExit
        ) as exit_status:
            parser.parse_args(["verify-prepublication-candidate", "--help"])
        self.assertEqual(exit_status.exception.code, 0)
        help_text = output.getvalue().lower()
        self.assertIn("execution receipt timestamp only", help_text)
        self.assertNotIn("--force", help_text)
        self.assertNotIn("--overwrite", help_text)

    def test_command_returns_distinct_identity_and_receipt_digests(self):
        payload = {
            "status": "PASS",
            "candidateInputDigest": "sha256:" + "1" * 64,
            "verifiedCandidateDigest": "sha256:" + "2" * 64,
            "verifiedCandidateIdentityDigest": "sha256:" + "2" * 64,
            "verificationExecutionReceiptDigest": "sha256:" + "3" * 64,
            "verificationExecutionReceiptContentDigest": "sha256:" + "4" * 64,
            "verificationExecutionReceiptExisting": False,
            "existing": False,
        }
        with tempfile.TemporaryDirectory() as temporary, mock.patch(
            "release.cli.verify_prepublication_candidate", return_value=payload
        ) as verifier, contextlib.redirect_stdout(io.StringIO()) as output:
            result = cli.main(self._arguments(Path(temporary)))
        self.assertEqual(result, 0)
        self.assertEqual(json.loads(output.getvalue()), payload)
        self.assertEqual(
            verifier.call_args.kwargs["verified_at"], "2026-08-25T12:00:00Z"
        )
        self.assertNotEqual(
            payload["verifiedCandidateIdentityDigest"],
            payload["verificationExecutionReceiptDigest"],
        )


if __name__ == "__main__":
    unittest.main()
