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
        candidate_ref = (
            "ref: ${{ inputs.candidate_sha || (github.event_name == 'pull_request' && "
            "github.event.pull_request.head.sha || github.sha) }}"
        )
        for source in (ci, release):
            self.assertIn("merge_group:", source)
            self.assertIn("workflow_call:", source)
            self.assertIn("candidate_sha:", source)
            self.assertIn("force_full:", source)
            self.assertIn(candidate_ref, source)
            self.assertNotIn("ref: ${{ inputs.candidate_sha || github.sha }}", source)
            self.assertIn("CI_FORCE_FULL: ${{ inputs.force_full || false }}", source)
            self.assertIn("inputs.candidate_sha || github.event.pull_request.number || github.ref", source)
        self.assertIn("comparison_base_sha:", ci)
        self.assertIn("upgrade_base_sha:", release)

    def test_classifiers_write_only_to_the_runner_output_file(self):
        expected = (
            'run: python scripts/ci_classify.py --base "$CI_BASE_SHA" '
            '--head "$CI_HEAD_SHA" --github-output "$GITHUB_OUTPUT"'
        )
        for name in ("ci.yml", "release-gate.yml"):
            invocations = [
                line.strip()
                for line in self.source(name).splitlines()
                if "scripts/ci_classify.py" in line and "--github-output" in line
            ]
            with self.subTest(workflow=name):
                self.assertEqual(invocations, [expected])

    def test_pre_merge_passes_candidate_public_origin_to_trusted_reusable_gates(self):
        source = self.source("pre-merge-full.yml")
        self.assertIn("public_origin: https://ci.example.test", source)
        self.assertEqual(source.count("public_origin: https://ci.example.test"), 2)

        ci = self.source("ci.yml")
        release = self.source("release-gate.yml")
        self.assertIn("public_origin:", ci)
        self.assertIn("public_origin:", release)
        self.assertIn("ANIMEMO_PUBLIC_ORIGIN: ${{ inputs.public_origin || 'http://localhost:5173' }}", ci)
        self.assertIn("ANIMEMO_PUBLIC_ORIGIN=${{ inputs.public_origin || 'https://ci.example.test' }}", release)
        self.assertNotIn("ANIME_JOURNAL_PORT", release)
        self.assertNotIn("ANIME_JOURNAL_DATA_ROOT", release)

    def test_full_selection_flows_only_through_classifier_gates_and_checkouts_are_pinned(self):
        ci = self.source("ci.yml")
        release = self.source("release-gate.yml")

        ci_selectors = {
            "frontend": "run_frontend",
            "backend": "run_backend",
            "bootstrap-smoke": "run_bootstrap",
            "postgres": "run_postgres",
            "plugins": "run_plugins",
            "astrbot-bridge": "run_bridge",
            "astrbot-runtime": "run_runtime",
        }
        for name, selector in ci_selectors.items():
            job = self.job(ci, name)
            self.assertNotIn("inputs.force_full ||", job, name)
            self.assertIn(f"needs.classify.outputs.{selector} == 'true'", job, name)
        self.assertNotIn("inputs.force_full ||", self.job(ci, "fast-fail"))
        self.assertIn(
            ".execution.profile != 'DOCS_ONLY'", self.job(ci, "fast-fail")
        )

        release_selectors = {
            "updater-isolated": "run_release_updater",
            "docker": "run_release_docker",
            "stateful-upgrade": "run_release_stateful",
        }
        for name, selector in release_selectors.items():
            job = self.job(release, name)
            self.assertNotIn("inputs.force_full ||", job, name)
            self.assertIn(f"needs.classify.outputs.{selector} == 'true'", job, name)
        dr = self.job(release, "dr-rehearsal")
        self.assertNotIn("inputs.force_full ||", dr)
        self.assertIn(".gates.run_release_dr == true", dr)

        candidate_ref = (
            "ref: ${{ inputs.candidate_sha || (github.event_name == 'pull_request' && "
            "github.event.pull_request.head.sha || github.sha) }}"
        )
        self.assertEqual(ci.count(candidate_ref), 11)
        self.assertEqual(release.count(candidate_ref), 7)
        self.assertIn(
            "repository: AstrBotDevs/AstrBot\n          ref: ${{ matrix.astrbot_ref }}",
            ci,
        )

    def test_pr_fast_has_stable_aggregate_and_main_remains_lightweight(self):
        ci = self.source("ci.yml")
        release = self.source("release-gate.yml")
        self.assertIn("name: pr-fast-gate", ci)
        self.assertIn("name: ci-selection-authority", ci)
        self.assertIn("needs: selection-authority", self.job(ci, "pr-fast-gate"))
        self.assertIn("!inputs.force_full", self.job(ci, "pr-fast-gate"))
        self.assertIn("name: release-gate-authority", release)
        self.assertIn("github.event_name == 'pull_request'", ci)
        self.assertIn("github.event_name != 'push'", ci)
        self.assertIn("github.event_name != 'push'", release)
        self.assertIn("name: post-merge-sanity", release)

    def test_plugin_immutability_gate_uses_the_checked_out_candidate_head(self):
        plugins = self.job(self.source("ci.yml"), "plugins")

        self.assertEqual(plugins.count('--head "$CANDIDATE_SHA"'), 2)
        self.assertIn('--base "$COMPARISON_BASE_SHA" --head "$CANDIDATE_SHA"', plugins)
        self.assertNotIn("--head-root", plugins)
        self.assertNotIn("--repo", plugins)

    def test_selection_authorities_validate_actual_job_results(self):
        ci = self.source("ci.yml")
        release = self.source("release-gate.yml")

        ci_authority = self.job(ci, "selection-authority")
        self.assertIn("if: ${{ always() }}", ci_authority)
        self.assertIn("NEEDS_JSON: ${{ toJSON(needs) }}", ci_authority)
        self.assertIn("--workflow ci", ci_authority)
        self.assertIn('--event-name "${{ github.event_name }}"', ci_authority)
        for dependency in (
            "classify",
            "fast-fail",
            "docs-only",
            "frontend",
            "backend",
            "bootstrap-smoke",
            "postgres",
            "plugins",
            "astrbot-bridge",
            "astrbot-runtime",
        ):
            self.assertIn(dependency, ci_authority)

        release_authority = self.job(release, "selection-authority")
        self.assertIn("if: ${{ always() }}", release_authority)
        self.assertIn("NEEDS_JSON: ${{ toJSON(needs) }}", release_authority)
        self.assertIn("--workflow release", release_authority)
        self.assertIn(
            "--release-graph-contract animemo.release-gate.jobs/v2",
            release_authority,
        )
        self.assertNotIn("--release-graph-contract", ci_authority)
        for dependency in (
            "classify",
            "post-merge-sanity",
            "updater-isolated",
            "docker",
            "stateful-upgrade",
            "dr-rehearsal",
        ):
            self.assertIn(dependency, release_authority)

    def test_release_jobs_use_distinct_component_selectors(self):
        release = self.source("release-gate.yml")
        self.assertIn(
            "needs.classify.outputs.run_release_updater == 'true'",
            self.job(release, "updater-isolated"),
        )
        self.assertIn(
            "needs.classify.outputs.run_release_docker == 'true'",
            self.job(release, "docker"),
        )
        self.assertIn(
            "needs.classify.outputs.run_release_stateful == 'true'",
            self.job(release, "stateful-upgrade"),
        )
        self.assertIn(
            "fromJSON(needs.classify.outputs.classification_json).gates.run_release_dr == true",
            self.job(release, "dr-rehearsal"),
        )
        self.assertNotIn(
            "run_release_stateful == 'true'", self.job(release, "dr-rehearsal")
        )

    def test_contract_validation_reuses_the_old_main_compatible_docs_slot(self):
        ci = self.source("ci.yml")
        contract = self.job(ci, "docs-only")
        classify = self.job(ci, "classify")

        self.assertIn("name: docs-or-contract-validation", contract)
        self.assertIn("CONTRACT_VALIDATION_ONLY", contract)
        self.assertIn("scripts.tests.test_recovery_migration_contracts", contract)
        self.assertIn("python scripts/ci_classify.py --self-test", contract)
        self.assertIn("python -m compileall -q", contract)
        self.assertIn("git diff --check", contract)
        self.assertIn("Validate CI authority contracts", self.job(ci, "fast-fail"))
        self.assertIn(".signals.ci", self.job(ci, "fast-fail"))
        self.assertNotIn("execution_profile:", classify)
        self.assertNotIn("run_contract_validation:", classify)
        self.assertNotIn("run_release_dr:", classify)

    def test_release_gate_uses_only_the_canonical_explicit_job_compose_contract(self):
        release = self.source("release-gate.yml")

        self.assertIn("ANIMEMO_API_IMAGE=animemo-api:release-gate", release)
        self.assertIn("ANIMEMO_WEB_IMAGE=animemo-web:release-gate", release)
        self.assertIn("test -f deploy/docker-compose.build.yml", release)
        self.assertNotIn('if [[ -f deploy/docker-compose.build.yml ]]; then', release)
        self.assertIn(
            "COMPOSE_FILE=deploy/docker-compose.yml:deploy/docker-compose.build.yml",
            release,
        )
        self.assertIn("docker compose --env-file .ci-runtime.env build api web", release)
        self.assertIn(
            "docker compose --env-file .ci-runtime.env up -d --wait --wait-timeout 120 postgres redis",
            release,
        )
        self.assertIn(
            "docker compose --env-file .ci-runtime.env run --rm --no-deps migration",
            release,
        )
        self.assertIn(
            "docker compose --env-file .ci-runtime.env run --rm --no-deps bootstrap",
            release,
        )
        self.assertIn(
            "docker compose --env-file .ci-runtime.env up -d --no-deps api web",
            release,
        )
        self.assertNotIn("EXPLICIT_RELEASE_JOBS", release)
        self.assertNotIn(".env.production", release)

    def test_performance_backend_uses_an_explicit_isolated_frontend_origin(self):
        performance = self.source("performance.yml")
        self.assertIn("FRONTEND_URL: http://perf.example.test:8088", performance)


if __name__ == "__main__":
    unittest.main()
