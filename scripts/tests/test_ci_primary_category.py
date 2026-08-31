from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


class PrimaryCategoryCliTests(unittest.TestCase):
    def run_cli(self, pull_request: dict[str, object]) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as directory:
            event_path = Path(directory) / "event.json"
            event_path.write_text(
                json.dumps({"pull_request": pull_request}, ensure_ascii=False),
                encoding="utf-8",
            )
            return subprocess.run(
                [
                    "python",
                    "scripts/ci_primary_category.py",
                    "--event-path",
                    str(event_path),
                ],
                cwd=REPOSITORY_ROOT,
                check=False,
                capture_output=True,
                text=True,
            )

    def test_conflicting_primary_labels_return_the_policy_diagnostic(self):
        completed = self.run_cli(
            {
                "number": 198,
                "title": "修复资格平台数据库就绪竞态",
                "head": {"sha": "a" * 40},
                "updated_at": "2026-08-31T07:11:22Z",
                "labels": [
                    {"name": "release/fix"},
                    {"name": "release/deployment"},
                    {"name": "release/ci"},
                ],
            }
        )

        self.assertEqual(completed.returncode, 2)
        diagnostic = json.loads(completed.stderr)
        self.assertEqual(diagnostic["code"], "release_primary_category_conflict")
        self.assertEqual(
            diagnostic["detail"],
            "release_primary_category_conflict: PR #198; "
            "primaryLabels=[release/ci,release/deployment,release/fix]; "
            "exclusionLabels=[]; mergeCommit="
            f"{'a' * 40}; observedUpdatedAt=2026-08-31T07:11:22Z",
        )

    def test_pr_203_fix_word_and_release_paths_do_not_create_a_second_category(self):
        completed = self.run_cli(
            {
                "number": 203,
                "title": "修复发布生产器标准输入转发",
                "head": {"sha": "b" * 40},
                "updated_at": "2026-08-31T09:00:00Z",
                "labels": [
                    {"name": "release/deployment"},
                    {"name": "size/XL"},
                ],
                "audit_changed_paths": [
                    "release/publication.py",
                    "scripts/run-in-release-producer.sh",
                ],
            }
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(
            json.loads(completed.stdout),
            {
                "category": "deployment",
                "decision": "INCLUDED",
                "exclusion_labels": [],
                "primary_labels": ["release/deployment"],
            },
        )

    def test_pr_202_fix_word_and_ci_paths_keep_explicit_ci_category(self):
        completed = self.run_cli(
            {
                "number": 202,
                "title": "修复资格输出步骤边界",
                "head": {"sha": "c" * 40},
                "updated_at": "2026-08-31T08:30:00Z",
                "labels": [
                    {"name": "release/ci"},
                    {"name": "size/L"},
                ],
                "audit_changed_paths": [
                    ".github/workflows/release.yml",
                    "scripts/tests/test_release_workflows.py",
                ],
            }
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(
            json.loads(completed.stdout),
            {
                "category": "ci",
                "decision": "INCLUDED",
                "exclusion_labels": [],
                "primary_labels": ["release/ci"],
            },
        )


if __name__ == "__main__":
    unittest.main()
