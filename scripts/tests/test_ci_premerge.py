from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from scripts.ci_premerge import PreMergeValidationError, validate_snapshot, verify_base_freshness


REPOSITORY = "yanyuhanyue/AniMemo"
BASE_SHA = "a" * 40
HEAD_SHA = "b" * 40


def pull_request_payload(**overrides):
    payload = {
        "number": 62,
        "state": "open",
        "base": {"ref": "main", "sha": BASE_SHA, "repo": {"full_name": REPOSITORY}},
        "head": {
            "ref": "codex/release-producer-workflows",
            "sha": HEAD_SHA,
            "repo": {"full_name": REPOSITORY},
        },
    }
    payload.update(overrides)
    return payload


class PreMergeSnapshotTests(unittest.TestCase):
    def test_valid_snapshot_binds_pr_head_and_current_main(self):
        snapshot = validate_snapshot(
            pull_request_payload(),
            expected_pr_number=62,
            expected_head_sha=HEAD_SHA,
            current_main_sha=BASE_SHA,
            repository=REPOSITORY,
        )

        self.assertEqual(snapshot.pr_number, 62)
        self.assertEqual(snapshot.head_sha, HEAD_SHA)
        self.assertEqual(snapshot.base_sha, BASE_SHA)
        self.assertEqual(snapshot.head_ref, "codex/release-producer-workflows")

    def test_closed_wrong_base_or_moved_head_is_rejected(self):
        cases = (
            (pull_request_payload(state="closed"), "open"),
            (pull_request_payload(base={"ref": "release", "sha": BASE_SHA, "repo": {"full_name": REPOSITORY}}), "base"),
            (pull_request_payload(), "head SHA"),
        )
        for payload, message in cases:
            with self.subTest(message=message):
                expected_head = "c" * 40 if message == "head SHA" else HEAD_SHA
                with self.assertRaisesRegex(PreMergeValidationError, message):
                    validate_snapshot(
                        payload,
                        expected_pr_number=62,
                        expected_head_sha=expected_head,
                        current_main_sha=BASE_SHA,
                        repository=REPOSITORY,
                    )

    def test_stale_base_or_foreign_repository_is_rejected(self):
        with self.assertRaisesRegex(PreMergeValidationError, "current main"):
            validate_snapshot(
                pull_request_payload(),
                expected_pr_number=62,
                expected_head_sha=HEAD_SHA,
                current_main_sha="c" * 40,
                repository=REPOSITORY,
            )

    def test_wrong_pr_number_or_malformed_expected_head_is_rejected(self):
        cases = (
            ({"expected_pr_number": 61, "expected_head_sha": HEAD_SHA}, "number moved"),
            ({"expected_pr_number": 62, "expected_head_sha": "not-a-sha"}, "Invalid expected head SHA"),
        )
        for arguments, message in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(PreMergeValidationError, message):
                    validate_snapshot(
                        pull_request_payload(),
                        current_main_sha=BASE_SHA,
                        repository=REPOSITORY,
                        **arguments,
                    )

        foreign = pull_request_payload(
            head={"ref": "topic", "sha": HEAD_SHA, "repo": {"full_name": "attacker/fork"}}
        )
        with self.assertRaisesRegex(PreMergeValidationError, "head repository"):
            validate_snapshot(
                foreign,
                expected_pr_number=62,
                expected_head_sha=HEAD_SHA,
                current_main_sha=BASE_SHA,
                repository=REPOSITORY,
            )


class BaseFreshnessTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.repo = Path(self.temporary.name)
        self._git("init")
        self._git("config", "user.email", "ci@example.test")
        self._git("config", "user.name", "CI")
        self.base = self._commit("base.txt", "base", "base")
        self.head = self._commit("head.txt", "head", "head")

    def tearDown(self):
        self.temporary.cleanup()

    def _git(self, *args):
        return subprocess.run(["git", *args], cwd=self.repo, check=True, capture_output=True, text=True).stdout.strip()

    def _commit(self, name, content, message):
        (self.repo / name).write_text(content, encoding="utf-8")
        self._git("add", name)
        self._git("commit", "-m", message)
        return self._git("rev-parse", "HEAD")

    def test_head_containing_current_main_is_fresh(self):
        verify_base_freshness(self.repo, base_sha=self.base, head_sha=self.head)

    def test_diverged_head_is_rejected(self):
        self._git("checkout", "--detach", self.base)
        diverged = self._commit("diverged.txt", "diverged", "diverged")

        with self.assertRaisesRegex(PreMergeValidationError, "BASE FRESHNESS"):
            verify_base_freshness(self.repo, base_sha=self.head, head_sha=diverged)


class PreMergeCliTests(unittest.TestCase):
    def test_cli_outputs_bound_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = root / "pr.json"
            payload.write_text(json.dumps(pull_request_payload()), encoding="utf-8")
            completed = subprocess.run(
                [
                    "python",
                    "scripts/ci_premerge.py",
                    "--pr-json", str(payload),
                    "--pr-number", "62",
                    "--expected-head-sha", HEAD_SHA,
                    "--current-main-sha", BASE_SHA,
                    "--repository", REPOSITORY,
                ],
                cwd=Path(__file__).resolve().parents[2],
                capture_output=True,
                text=True,
            )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout)["head_sha"], HEAD_SHA)


if __name__ == "__main__":
    unittest.main()
