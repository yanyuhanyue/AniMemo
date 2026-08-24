from __future__ import annotations

import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
PERFORMANCE_JOBS = {
    "frontend",
    "backend",
    "isolated-resource-load",
    "isolated-long-operation-capacity",
    "regression-gate",
}
IDENTITY_INPUTS = {
    "candidate_sha",
    "candidate_ref",
    "commit",
    "ref",
    "branch",
    "trusted",
    "allow_untrusted",
}
TRUSTED_MAIN_SCRIPT = """set -euo pipefail
test "$GITHUB_REPOSITORY" = "yanyuhanyue/AniMemo"
test "$GITHUB_REF" = "refs/heads/main"
git fetch --force origin "+refs/heads/main:refs/remotes/origin/main"
test "$(git rev-parse HEAD^{commit})" = "$TRUSTED_SHA"
test "$(git rev-parse origin/main^{commit})" = "$TRUSTED_SHA"
test -z "$(git status --porcelain --untracked-files=no)"
echo "verified=true" >> "$GITHUB_OUTPUT"
"""


def load_yaml(source: str) -> dict[str, object]:
    return yaml.load(source, Loader=yaml.BaseLoader)


def workflow(name: str) -> tuple[dict[str, object], str]:
    source = (ROOT / ".github" / "workflows" / name).read_text(
        encoding="utf-8"
    )
    return load_yaml(source), source


def job_binds_exact_current_main(job: dict[str, object]) -> bool:
    steps = job.get("steps", [])
    if not isinstance(steps, list) or len(steps) < 2:
        return False
    checkout, binding = steps[:2]
    if not isinstance(checkout, dict) or not isinstance(binding, dict):
        return False
    return (
        checkout.get("uses", "").startswith("actions/checkout@")
        and checkout.get("with", {}).get("ref") == "${{ github.sha }}"
        and checkout.get("with", {}).get("persist-credentials") == "false"
        and binding.get("id") == "trusted_main"
        and binding.get("env", {}).get("TRUSTED_SHA") == "${{ github.sha }}"
        and binding.get("shell") == "bash"
        and binding.get("run") == TRUSTED_MAIN_SCRIPT
    )


def classify_performance_boundary(source: str) -> str:
    document = load_yaml(source)
    triggers = document.get("on", {})
    jobs = document.get("jobs", {})
    input_names: set[str] = set()
    for config in triggers.values():
        if isinstance(config, dict):
            inputs = config.get("inputs", {})
            if isinstance(inputs, dict):
                input_names.update(inputs)

    steps = [
        step
        for job in jobs.values()
        if isinstance(job, dict)
        for step in job.get("steps", [])
        if isinstance(step, dict)
    ]
    dynamic_checkout = any(
        step.get("uses", "").startswith("actions/checkout@")
        and "inputs." in step.get("with", {}).get("ref", "")
        for step in steps
    )
    cache_capable = any(
        step.get("uses", "").startswith("docker/setup-buildx-action@")
        or step.get("uses", "").split("@", 1)[0].startswith("actions/cache")
        or any(
            key in {"cache-to", "cache-from"} and "type=gha" in value
            for key, value in step.get("with", {}).items()
        )
        for step in steps
    )
    has_execution = any("run" in step for step in steps)
    untrusted_source = bool(input_names & IDENTITY_INPUTS) or dynamic_checkout
    write_scope_trigger = "workflow_dispatch" in triggers or "workflow_call" in triggers
    if untrusted_source and write_scope_trigger and cache_capable and has_execution:
        return "FAIL_UNTRUSTED_CANDIDATE_IN_CACHE_CAPABLE_DEFAULT_BRANCH_CONTEXT"
    if "pull_request" in triggers and "pull_request_target" not in triggers:
        return "PASS_LOW_TRUST_PULL_REQUEST_CONTEXT"
    code_executing_jobs = [
        job
        for job in jobs.values()
        if isinstance(job, dict)
        and any(
            isinstance(step, dict) and "run" in step
            for step in job.get("steps", [])
        )
    ]
    if (
        triggers == {"workflow_call": {}}
        and not input_names
        and not dynamic_checkout
        and code_executing_jobs
        and all(job_binds_exact_current_main(job) for job in code_executing_jobs)
    ):
        return "PASS_TRUSTED_MAIN_BOUNDARY"
    return "FAIL_UNCLASSIFIED_PERFORMANCE_BOUNDARY"


class PerformanceTrustBoundaryTests(unittest.TestCase):
    def test_actual_workflow_has_no_manual_or_identity_input_surface(self):
        performance, source = workflow("performance.yml")

        self.assertEqual(performance["on"], {"workflow_call": {}})
        self.assertNotIn("workflow_dispatch", performance["on"])
        self.assertEqual(len(performance["on"]["workflow_call"]), 0)
        self.assertNotIn("candidate_sha", source)
        self.assertNotIn("${{ inputs.", source)
        self.assertEqual(
            performance["concurrency"]["group"],
            "animemo-performance-trusted-${{ github.sha }}",
        )
        self.assertEqual(
            classify_performance_boundary(source), "PASS_TRUSTED_MAIN_BOUNDARY"
        )

    def test_every_code_executing_job_binds_its_own_runner_to_current_main(self):
        performance, _ = workflow("performance.yml")

        self.assertEqual(set(performance["jobs"]), PERFORMANCE_JOBS)
        for name, job in performance["jobs"].items():
            steps = job["steps"]
            checkout_indices = [
                index
                for index, step in enumerate(steps)
                if step.get("uses", "").startswith("actions/checkout@")
            ]
            self.assertEqual(checkout_indices, [0], name)
            checkout = steps[0]
            self.assertEqual(checkout["with"]["ref"], "${{ github.sha }}", name)
            self.assertEqual(checkout["with"]["persist-credentials"], "false", name)

            binding = steps[1]
            self.assertTrue(job_binds_exact_current_main(job), name)
            self.assertEqual(binding["run"], TRUSTED_MAIN_SCRIPT, name)

            for step in steps[2:]:
                condition = step.get("if", "")
                if "always()" in condition or "failure()" in condition:
                    self.assertIn(
                        "steps.trusted_main.outputs.verified == 'true'",
                        condition,
                        f"{name}: {step.get('name', step.get('uses'))}",
                    )

    def test_release_rc_qualification_requires_current_main_and_calls_without_inputs(self):
        release, source = workflow("release.yml")
        performance = release["jobs"]["performance"]

        self.assertEqual(
            performance["if"],
            "${{ inputs.operation == 'qualify' && inputs.channel == 'rc' }}",
        )
        self.assertEqual(performance["uses"], "./.github/workflows/performance.yml")
        self.assertNotIn("with", performance)

        binding = next(
            step
            for step in release["jobs"]["preflight"]["steps"]
            if step.get("id") == "candidate"
        )
        self.assertEqual(binding["env"]["CHANNEL"], "${{ inputs.channel }}")
        run = binding["run"]
        self.assertIn(
            'if [[ "$OPERATION" = "qualify" && "$CHANNEL" = "rc" ]]', run
        )
        self.assertIn('if [[ -n "$REQUESTED_CANDIDATE_SHA" ]]', run)
        self.assertIn("RC_QUALIFICATION_REQUIRES_CURRENT_MAIN", run)
        self.assertIn('test "$GITHUB_REF" = "refs/heads/main"', run)
        self.assertIn('test "$GITHUB_SHA" = "$main_sha"', run)
        self.assertIn('test "$INTENDED_MAIN_SHA" = "$GITHUB_SHA"', run)
        self.assertIn('elif [[ -n "$REQUESTED_CANDIDATE_SHA" ]]', run)
        self.assertIn("candidate_sha", release["on"]["workflow_dispatch"]["inputs"])
        self.assertNotIn("candidate_sha:", source[source.index("  performance:\n") : source.index("  platform-qualification:\n")])

    def test_buildx_and_gha_cache_paths_are_closed_without_profile_changes(self):
        performance, source = workflow("performance.yml")
        buildx_count = 0
        for name, job in performance["jobs"].items():
            for step in job["steps"]:
                action = step.get("uses", "").split("@", 1)[0]
                self.assertFalse(action.startswith("actions/cache"), name)
                for key, value in step.get("with", {}).items():
                    if key in {"cache-to", "cache-from"}:
                        self.assertNotIn("type=gha", value, name)
                if action == "docker/setup-buildx-action":
                    buildx_count += 1
                    self.assertEqual(step["with"]["cache-binary"], "false", name)
                    self.assertEqual(step["with"]["keep-state"], "false", name)
                    self.assertEqual(step["with"]["cleanup"], "true", name)

        self.assertEqual(buildx_count, 2)
        self.assertIn("for dataset in small medium large", source)
        self.assertIn("CONCURRENCY_LEVELS == (1, 5, 10, 20)", source)
        self.assertIn("--sustained-concurrency 5", source)
        self.assertIn("--duration-seconds 1500", source)
        capacity = (ROOT / "scripts" / "perf" / "long_operation_capacity.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("NORMAL_USER_LEVELS = (20, 40, 60)", capacity)
        self.assertIn("LONG_OPERATION_LEVELS = (0, 2, 4, 8)", capacity)

    def test_no_pr_performance_authority_or_premerge_consumer_was_added(self):
        premerge = (ROOT / ".github" / "workflows" / "pre-merge-full.yml").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("performance.yml", premerge)
        self.assertFalse(
            (ROOT / ".github" / "workflows" / "performance-pr.yml").exists()
        )
        for path in (ROOT / ".github" / "workflows").glob("*.yml"):
            source = path.read_text(encoding="utf-8")
            if "pull_request_target:" not in source:
                continue
            self.assertNotIn("performance.yml", source, path.name)
            self.assertNotIn("actions/checkout@", source, path.name)
            self.assertNotIn("candidate_sha", source, path.name)

    def test_security_classifier_rejects_both_candidate_input_shapes(self):
        fixtures = (
            """
on:
  workflow_dispatch:
    inputs:
      candidate_sha: {required: true}
jobs:
  performance:
    steps:
      - uses: actions/checkout@fixed
        with: {ref: '${{ inputs.candidate_sha }}'}
      - uses: docker/setup-buildx-action@fixed
      - run: docker compose build
""",
            """
on:
  workflow_call:
    inputs:
      candidate_sha: {required: true}
jobs:
  performance:
    steps:
      - uses: actions/checkout@fixed
        with: {ref: '${{ inputs.candidate_sha }}'}
      - uses: docker/setup-buildx-action@fixed
      - run: docker compose build
""",
        )
        for fixture in fixtures:
            self.assertEqual(
                classify_performance_boundary(fixture),
                "FAIL_UNTRUSTED_CANDIDATE_IN_CACHE_CAPABLE_DEFAULT_BRANCH_CONTEXT",
            )

    def test_security_classifier_accepts_main_only_and_low_trust_shapes(self):
        trusted = """
on:
  workflow_call: {}
jobs:
  performance:
    steps:
      - uses: actions/checkout@fixed
        with: {ref: '${{ github.sha }}', persist-credentials: false}
      - id: trusted_main
        env: {TRUSTED_SHA: '${{ github.sha }}'}
        shell: bash
        run: |
          set -euo pipefail
          test "$GITHUB_REPOSITORY" = "yanyuhanyue/AniMemo"
          test "$GITHUB_REF" = "refs/heads/main"
          git fetch --force origin "+refs/heads/main:refs/remotes/origin/main"
          test "$(git rev-parse HEAD^{commit})" = "$TRUSTED_SHA"
          test "$(git rev-parse origin/main^{commit})" = "$TRUSTED_SHA"
          test -z "$(git status --porcelain --untracked-files=no)"
          echo "verified=true" >> "$GITHUB_OUTPUT"
      - uses: docker/setup-buildx-action@fixed
        with: {cache-binary: false, keep-state: false, cleanup: true}
      - run: docker compose build
"""
        low_trust = """
on:
  pull_request: {}
jobs:
  smoke:
    steps:
      - uses: actions/checkout@fixed
      - run: python -m unittest
"""
        self.assertEqual(
            classify_performance_boundary(trusted), "PASS_TRUSTED_MAIN_BOUNDARY"
        )
        self.assertEqual(
            classify_performance_boundary(low_trust),
            "PASS_LOW_TRUST_PULL_REQUEST_CONTEXT",
        )

    def test_security_classifier_rejects_missing_main_freshness_guard(self):
        missing_freshness_guard = """
on:
  workflow_call: {}
jobs:
  performance:
    steps:
      - uses: actions/checkout@fixed
        with: {ref: '${{ github.sha }}', persist-credentials: false}
      - id: trusted_main
        env: {TRUSTED_SHA: '${{ github.sha }}'}
        shell: bash
        run: |
          set -euo pipefail
          test "$GITHUB_REPOSITORY" = "yanyuhanyue/AniMemo"
          test "$GITHUB_REF" = "refs/heads/main"
          test "$(git rev-parse HEAD^{commit})" = "$TRUSTED_SHA"
          test -z "$(git status --porcelain --untracked-files=no)"
          echo "verified=true" >> "$GITHUB_OUTPUT"
      - run: docker compose build
"""
        self.assertEqual(
            classify_performance_boundary(missing_freshness_guard),
            "FAIL_UNCLASSIFIED_PERFORMANCE_BOUNDARY",
        )


if __name__ == "__main__":
    unittest.main()
