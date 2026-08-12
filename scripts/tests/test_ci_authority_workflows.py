from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


class CiAuthorityWorkflowTests(unittest.TestCase):
    def source(self, name):
        return (ROOT / ".github" / "workflows" / name).read_text(encoding="utf-8")

    def job(self, source, name):
        marker = f"  {name}:\n"
        start = source.index(marker)
        next_job = source.find("\n  ", start + len(marker))
        while next_job != -1 and not source[next_job + 3 :].split(":", 1)[0].replace("-", "").isalnum():
            next_job = source.find("\n  ", next_job + 1)
        return source[start:] if next_job == -1 else source[start:next_job]

    def test_pre_merge_workflow_binds_pr_head_base_and_both_full_gates(self):
        source = self.source("pre-merge-full.yml")
        self.assertIn("workflow_dispatch:", source)
        self.assertIn("pr_number:", source)
        self.assertIn("expected_head_sha:", source)
        self.assertIn("uses: ./.github/workflows/ci.yml", source)
        self.assertIn("uses: ./.github/workflows/release-gate.yml", source)
        self.assertIn("candidate_sha: ${{ needs.preflight.outputs.head_sha }}", source)
        self.assertIn("comparison_base_sha: ${{ needs.preflight.outputs.base_sha }}", source)
        self.assertIn("upgrade_base_sha: ${{ needs.preflight.outputs.base_sha }}", source)
        self.assertIn("name: pre-merge-authority", source)
        self.assertIn("context=pre-merge-authority", source)
        self.assertIn("if: ${{ always()", source)

    def test_pre_merge_workflow_uses_trusted_default_branch_and_independent_concurrency(self):
        source = self.source("pre-merge-full.yml")
        self.assertIn("group: pre-merge-full-pr-${{ inputs.pr_number }}", source)
        self.assertIn("cancel-in-progress: false", source)
        self.assertIn('test "$GITHUB_REF" = "refs/heads/$DEFAULT_BRANCH"', source)
        self.assertIn('test "$GITHUB_SHA" = "$current_main_sha"', source)

    def test_pre_merge_workflow_publishes_pending_and_terminal_authority_status(self):
        source = self.source("pre-merge-full.yml")
        self.assertEqual(source.count("-f context=pre-merge-authority"), 2)
        self.assertIn("-f state=pending", source)
        self.assertIn("state=success", source)
        self.assertIn("state=failure", source)
        self.assertIn("Revalidate the exact candidate after all full gates", source)
        self.assertIn("test \"$state\" = success", source)

    def test_ci_and_release_gate_keep_merge_group_and_accept_exact_candidate(self):
        ci = self.source("ci.yml")
        release = self.source("release-gate.yml")
        for source in (ci, release):
            self.assertIn("merge_group:", source)
            self.assertIn("workflow_call:", source)
            self.assertIn("candidate_sha:", source)
            self.assertIn("force_full:", source)
            self.assertIn("ref: ${{ inputs.candidate_sha || github.sha }}", source)
            self.assertIn("inputs.force_full ||", source)
            self.assertIn("inputs.candidate_sha || github.event.pull_request.number || github.ref", source)
        self.assertIn("comparison_base_sha:", ci)
        self.assertIn("upgrade_base_sha:", release)

    def test_every_full_job_is_forced_and_every_animemo_checkout_is_candidate_pinned(self):
        ci = self.source("ci.yml")
        release = self.source("release-gate.yml")

        for name in (
            "fast-fail",
            "frontend",
            "backend",
            "bootstrap-smoke",
            "postgres",
            "plugins",
            "astrbot-bridge",
            "astrbot-runtime",
        ):
            self.assertIn("inputs.force_full ||", self.job(ci, name), name)
        for name in ("updater-isolated", "docker", "stateful-upgrade"):
            self.assertIn("inputs.force_full ||", self.job(release, name), name)

        self.assertEqual(ci.count("ref: ${{ inputs.candidate_sha || github.sha }}"), 9)
        self.assertEqual(release.count("ref: ${{ inputs.candidate_sha || github.sha }}"), 5)
        self.assertIn(
            "repository: AstrBotDevs/AstrBot\n          ref: ${{ matrix.astrbot_ref }}",
            ci,
        )

    def test_pr_fast_has_stable_aggregate_and_main_remains_lightweight(self):
        ci = self.source("ci.yml")
        release = self.source("release-gate.yml")
        self.assertIn("name: pr-fast-gate", ci)
        self.assertIn("github.event_name == 'pull_request'", ci)
        self.assertIn("github.event_name != 'push'", ci)
        self.assertIn("github.event_name != 'push'", release)
        self.assertIn("name: post-merge-sanity", release)


if __name__ == "__main__":
    unittest.main()
