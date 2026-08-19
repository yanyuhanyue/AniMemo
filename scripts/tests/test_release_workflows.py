from __future__ import annotations

import re
import unittest
from pathlib import Path

import yaml
from yaml.constructor import ConstructorError

ROOT = Path(__file__).resolve().parents[2]

HARDENED_WORKFLOWS = (
    "ci.yml",
    "dr-rehearsal.yml",
    "performance.yml",
    "pre-merge-full.yml",
    "promote-release.yml",
    "release-gate.yml",
    "release.yml",
)

PINNED_RELEASE_ACTIONS = {
    "actions/checkout": "3d3c42e5aac5ba805825da76410c181273ba90b1",
    "actions/setup-python": "5fda3b95a4ea91299a34e894583c3862153e4b97",
    "actions/download-artifact": "d3f86a106a0bac45b974a628896c90dbdf5c8093",
}


class UniqueKeyLoader(yaml.BaseLoader):
    pass


def _construct_unique_mapping(loader, node, deep=False):
    mapping = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"found duplicate key ({key})",
                key_node.start_mark,
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


def workflow(name):
    # PyYAML parses the YAML 1.1 word `on` as bool; BaseLoader preserves keys.
    return yaml.load(
        (ROOT / ".github" / "workflows" / name).read_text(encoding="utf-8"),
        Loader=UniqueKeyLoader,
    )


class ReleaseWorkflowContractTests(unittest.TestCase):
    def test_release_workflows_pin_every_external_action_to_a_commit(self):
        for name in ("release.yml", "promote-release.yml"):
            source = (ROOT / ".github" / "workflows" / name).read_text(
                encoding="utf-8"
            )
            references = re.findall(
                r"uses:\s+([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)@([^\s#]+)",
                source,
            )
            self.assertTrue(references)
            for action, reference in references:
                with self.subTest(workflow=name, action=action):
                    self.assertRegex(reference, r"^[0-9a-f]{40}$")

    def test_core_github_actions_are_v7_and_checkout_credentials_are_explicit(self):
        credentialed_checkouts = set()
        checkout_count = 0

        for name in HARDENED_WORKFLOWS:
            document = workflow(name)
            for job_name, job in document["jobs"].items():
                for step in job.get("steps", []):
                    action = step.get("uses", "")
                    if action.startswith("actions/checkout@"):
                        checkout_count += 1
                        expected = (
                            f"actions/checkout@{PINNED_RELEASE_ACTIONS['actions/checkout']}"
                            if name in {"release.yml", "promote-release.yml"}
                            else "actions/checkout@v7"
                        )
                        self.assertEqual(action, expected)
                        settings = step.get("with", {})
                        self.assertIn("persist-credentials", settings)
                        if settings["persist-credentials"] == "true":
                            credentialed_checkouts.add((name, job_name))
                        else:
                            self.assertEqual(settings["persist-credentials"], "false")
                    elif action.startswith("actions/setup-node@"):
                        self.assertEqual(action, "actions/setup-node@v7")
                    elif action.startswith("actions/setup-python@"):
                        expected = (
                            f"actions/setup-python@{PINNED_RELEASE_ACTIONS['actions/setup-python']}"
                            if name in {"release.yml", "promote-release.yml"}
                            else "actions/setup-python@v7"
                        )
                        self.assertEqual(action, expected)

        self.assertGreater(checkout_count, 0)
        self.assertEqual(
            credentialed_checkouts,
            {
                ("promote-release.yml", "publish"),
                ("release.yml", "publish"),
            },
        )

    def test_dependabot_groups_only_minor_patch_version_updates(self):
        dependabot = yaml.load(
            (ROOT / ".github" / "dependabot.yml").read_text(encoding="utf-8"),
            Loader=UniqueKeyLoader,
        )
        updates = {
            update["package-ecosystem"]: update for update in dependabot["updates"]
        }

        self.assertEqual(
            set(updates["npm"]["groups"]),
            {"frontend-production", "frontend-development"},
        )
        self.assertEqual(set(updates["pip"]["groups"]), {"backend-minor-patch"})
        self.assertEqual(
            set(updates["github-actions"]["groups"]),
            {"github-actions-minor-patch"},
        )

        for update in updates.values():
            for group in update.get("groups", {}).values():
                self.assertEqual(group["applies-to"], "version-updates")
                self.assertEqual(set(group["update-types"]), {"minor", "patch"})
                self.assertNotIn("major", group["update-types"])

    def test_security_policy_uses_private_reporting_for_preproduction(self):
        source = (ROOT / ".github" / "SECURITY.md").read_text(encoding="utf-8")

        self.assertIn("pre-production", source)
        self.assertIn("GitHub Private Vulnerability Reporting", source)
        self.assertIn("不要在公开 Issue", source)
        self.assertIn("tokens", source)
        self.assertIn("private user data", source)
        self.assertIn("exploit details", source)
        self.assertNotIn("v1.0 currently supported", source)

    def test_candidate_workflows_never_save_dependency_caches(self):
        for name in ("ci.yml", "performance.yml", "release.yml"):
            with self.subTest(workflow=name):
                document = workflow(name)
                setup_steps = [
                    step
                    for job in document["jobs"].values()
                    for step in job.get("steps", [])
                    if step.get("uses", "").startswith(
                        ("actions/setup-node@", "actions/setup-python@")
                    )
                ]
                self.assertTrue(setup_steps)
                for step in setup_steps:
                    settings = step.get("with", {})
                    self.assertNotIn("cache", settings)
                    self.assertNotIn("cache-dependency-path", settings)

    def test_dr_rehearsal_has_no_cache_artifact_secret_or_write_authority(self):
        document = workflow("dr-rehearsal.yml")
        source = (ROOT / ".github" / "workflows" / "dr-rehearsal.yml").read_text(
            encoding="utf-8"
        )

        self.assertEqual(document["permissions"], {"contents": "read"})
        self.assertNotIn("actions/cache@", source)
        self.assertNotIn("cache:", source)
        self.assertNotIn("actions/upload-artifact@", source)
        self.assertNotIn("secrets.", source)

    def test_astrbot_packaging_uses_only_the_canonical_dist_output(self):
        source = (ROOT / ".github" / "workflows" / "ci.yml").read_text(
            encoding="utf-8"
        )

        self.assertNotIn("package-astrbot-bridge.py --output", source)
        self.assertNotIn(
            "${{ runner.temp }}/astrbot_plugin_animemo_bridge-0.1.3.zip",
            source,
        )
        self.assertGreaterEqual(
            source.count("dist/astrbot_plugin_animemo_bridge-0.1.3.zip"),
            2,
        )
        self.assertNotIn("ASTRBOT_ROOT:", source)

    def test_qualification_evidence_paths_are_runner_scoped_and_validated(self):
        source = (ROOT / ".github" / "workflows" / "release.yml").read_text(
            encoding="utf-8"
        )
        publish = source[
            source.index("      - name: Download and verify Phase A qualification evidence") :
            source.index("      - name: Stage the validated platform and Release Notes")
        ]
        stage = source[
            source.index("      - name: Stage the validated platform and Release Notes") :
            source.index("      - uses: actions/upload-artifact@", source.index("      - name: Stage the validated platform and Release Notes"))
        ]

        run_id_guard = '[[ "$QUALIFICATION_RUN_ID" =~ ^[1-9][0-9]*$ ]]'
        evidence_path = (
            'evidence_file="$RUNNER_TEMP/qualification/'
            'release-qualification-$QUALIFICATION_RUN_ID.json"'
        )
        authority_path = (
            "QUALIFICATION_ARTIFACT_PATH: ${{ runner.temp }}/qualification/"
            "release-qualification-${{ inputs.qualification_run_id }}.json"
        )
        self.assertIn(
            "QUALIFICATION_RUN_ID: ${{ inputs.qualification_run_id }}",
            stage,
        )
        self.assertIn(run_id_guard, publish)
        self.assertIn(evidence_path, publish)
        self.assertIn(authority_path, publish)
        self.assertLess(publish.index(run_id_guard), publish.index(evidence_path))
        self.assertLess(publish.index(evidence_path), publish.index(authority_path))
        self.assertIn(
            "QUALIFICATION_ARTIFACT_PATH: ${{ runner.temp }}/"
            "release-qualification-${{ github.run_id }}.json",
            source,
        )
        self.assertEqual(source.count("QUALIFICATION_ARTIFACT_PATH:"), 2)

    def test_all_workflows_reject_duplicate_mapping_keys(self):
        for path in sorted((ROOT / ".github" / "workflows").glob("*.yml")):
            with self.subTest(workflow=path.name):
                yaml.load(path.read_text(encoding="utf-8"), Loader=UniqueKeyLoader)

    def test_ci_and_release_gate_are_reusable_full_gates(self):
        ci = workflow("ci.yml")
        gate = workflow("release-gate.yml")
        self.assertIn("workflow_call", ci["on"])
        self.assertIn("workflow_call", gate["on"])
        self.assertIn("candidate_sha", ci["on"]["workflow_call"]["inputs"])
        self.assertIn("comparison_base_sha", ci["on"]["workflow_call"]["inputs"])
        self.assertIn("force_full", ci["on"]["workflow_call"]["inputs"])
        self.assertIn("candidate_sha", gate["on"]["workflow_call"]["inputs"])
        self.assertIn("upgrade_base_sha", gate["on"]["workflow_call"]["inputs"])
        self.assertIn("force_full", gate["on"]["workflow_call"]["inputs"])
        ci_source = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        gate_source = (ROOT / ".github" / "workflows" / "release-gate.yml").read_text(encoding="utf-8")
        self.assertIn("inputs.force_full && 'workflow_call'", ci_source)
        self.assertIn("inputs.force_full && 'workflow_call'", gate_source)
        self.assertIn("--base \"${{ inputs.upgrade_base_sha || '' }}\"", gate_source)
        self.assertEqual(
            gate_source.count(
                "--release-graph-contract animemo.release-gate.jobs/v2"
            ),
            1,
        )
        self.assertNotIn("--release-graph-contract", ci_source)

    def test_ci_and_release_gate_publish_complete_classifier_contract(self):
        expected_outputs = {
            "schema_version",
            "risk_level",
            "risk_rank",
            "execution_force_full",
            "classification_json",
            "docs_only",
            "mixed",
            "run_frontend",
            "run_backend",
            "run_bootstrap",
            "run_plugins",
            "run_bridge",
            "run_postgres",
            "run_runtime",
            "run_release_full",
            "run_release_updater",
            "run_release_docker",
            "run_release_stateful",
            "full_gate",
            "critical_gate",
        }
        for name in ("ci.yml", "release-gate.yml"):
            with self.subTest(workflow=name):
                outputs = set(workflow(name)["jobs"]["classify"]["outputs"])
                self.assertEqual(outputs, expected_outputs)

    def test_selection_authority_jobs_are_always_run_and_exhaustive(self):
        ci = workflow("ci.yml")
        gate = workflow("release-gate.yml")

        ci_authority = ci["jobs"]["selection-authority"]
        self.assertEqual(ci_authority["if"], "${{ always() }}")
        self.assertEqual(
            set(ci_authority["needs"]),
            {
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
            },
        )
        self.assertEqual(
            ci["jobs"]["pr-fast-gate"]["needs"],
            "selection-authority",
        )

        release_authority = gate["jobs"]["selection-authority"]
        self.assertEqual(release_authority["if"], "${{ always() }}")
        self.assertEqual(
            set(release_authority["needs"]),
            {
                "classify",
                "post-merge-sanity",
                "updater-isolated",
                "docker",
                "stateful-upgrade",
                "dr-rehearsal",
            },
        )

    def test_release_workflow_is_manual_and_never_builds_stable(self):
        release = workflow("release.yml")
        self.assertEqual(set(release["on"]), {"workflow_dispatch"})
        inputs = release["on"]["workflow_dispatch"]["inputs"]
        self.assertEqual(inputs["channel"]["options"], ["beta", "rc"])
        self.assertEqual(inputs["operation"]["options"], ["qualify", "publish"])
        self.assertNotIn("dry_run", inputs)
        self.assertIn("candidate_sha", inputs)
        self.assertEqual(inputs["candidate_sha"]["required"], "false")
        self.assertIn("upgrade_base_sha", inputs)
        self.assertIn("target_version_override", inputs)
        self.assertEqual(release["jobs"]["full-ci"]["uses"], "./.github/workflows/ci.yml")
        self.assertEqual(release["jobs"]["full-release-gate"]["uses"], "./.github/workflows/release-gate.yml")
        self.assertEqual(
            release["jobs"]["full-ci"]["with"]["candidate_sha"],
            "${{ needs.preflight.outputs.candidate_sha }}",
        )
        self.assertEqual(release["jobs"]["full-ci"]["with"]["comparison_base_sha"], "${{ inputs.upgrade_base_sha }}")
        self.assertTrue(release["jobs"]["full-ci"]["with"]["force_full"])
        self.assertEqual(
            release["jobs"]["full-release-gate"]["with"]["candidate_sha"],
            "${{ needs.preflight.outputs.candidate_sha }}",
        )
        self.assertEqual(release["jobs"]["full-release-gate"]["with"]["upgrade_base_sha"], "${{ inputs.upgrade_base_sha }}")
        self.assertTrue(release["jobs"]["full-release-gate"]["with"]["force_full"])

    def test_performance_workflow_is_manual_or_reusable_and_pins_exact_candidate(self):
        performance = workflow("performance.yml")
        self.assertEqual(set(performance["on"]), {"workflow_dispatch", "workflow_call"})
        for trigger in ("workflow_dispatch", "workflow_call"):
            self.assertIn("candidate_sha", performance["on"][trigger]["inputs"])
            self.assertEqual(performance["on"][trigger]["inputs"]["candidate_sha"]["required"], "true")
            self.assertEqual(performance["on"][trigger]["inputs"]["candidate_sha"]["type"], "string")

        source = (ROOT / ".github" / "workflows" / "performance.yml").read_text(encoding="utf-8")
        self.assertNotIn("pull_request:", source)
        self.assertNotIn("merge_group:", source)
        self.assertGreaterEqual(source.count("ref: ${{ inputs.candidate_sha }}"), 3)
        self.assertIn("services:", source)
        self.assertIn("postgres:", source)
        self.assertIn("redis:", source)
        self.assertIn("POSTGRESQL_AUTHORITATIVE", source)
        self.assertIn("for dataset in small medium large", source)
        self.assertIn('--dataset "$dataset"', source)
        self.assertIn("CONCURRENCY_LEVELS", source)
        self.assertIn("--duration-seconds 1500", source)
        self.assertIn("provision_performance_load_identities", source)
        self.assertIn("--identities-file", source)
        self.assertNotIn("--username perf-v1-owner", source)
        self.assertIn("$RUNNER_TEMP/animemo-performance-identities-", source)
        self.assertIn('chmod 600 "$identities_file"', source)
        self.assertNotIn('tee artifacts/seed.json', source)
        self.assertIn("SESSION_COOKIE_SECURE=false", source)
        self.assertIn("CSRF_COOKIE_SECURE=false", source)
        self.assertIn("REFRESH_COOKIE_SECURE=false", source)
        self.assertIn("ALLOW_INSECURE_PRODUCTION_COOKIES=true", source)
        self.assertGreaterEqual(source.count("actions/upload-artifact@v4"), 4)
        self.assertIn("scripts/perf/regression_gate.py", source)
        self.assertIn("Require every performance evidence producer to succeed", source)
        self.assertIn("toJSON(needs)", source)
        self.assertIn('job["result"] != "success"', source)
        self.assertNotIn("inputs.candidate_sha || github.sha", source)
        self.assertIn('CANDIDATE_SHA: ${{ inputs.candidate_sha }}', source)
        self.assertIn('test "$(git rev-parse HEAD)" = "$CANDIDATE_SHA"', source)
        self.assertIn("isolated-long-operation-capacity:", source)
        self.assertIn("name: performance-long-operation-capacity", source)
        self.assertIn("ANIMEMO_ISOLATED_CAPACITY_PROBE=true", source)
        self.assertIn("ANIMEMO_ISOLATED_PROVIDER_LATENCY_MS=1200", source)
        self.assertIn("THROTTLE_USER_RATE=300/min", source)
        self.assertIn("--count 60", source)
        self.assertIn("scripts/perf/long_operation_capacity.py", source)
        capacity = source[
            source.index("  isolated-long-operation-capacity:") : source.index(
                "  regression-gate:"
            )
        ]
        self.assertIn("--iterations-per-user 4", capacity)
        self.assertIn(
            "needs: [frontend, backend, isolated-resource-load, isolated-long-operation-capacity]",
            source,
        )
        self.assertIn("name: performance-long-operation-capacity", source)
        self.assertIn("path: artifacts/capacity", source)

    def test_hosted_browser_gates_avoid_apt_and_launch_the_downloaded_chromium(self):
        for name in ("ci.yml", "performance.yml"):
            with self.subTest(workflow=name):
                source = (ROOT / ".github" / "workflows" / name).read_text(
                    encoding="utf-8"
                )
                self.assertNotIn("playwright install-deps", source)
                self.assertNotIn("--with-deps", source)
                self.assertIn("playwright install chromium", source)
                self.assertIn("chromium.launch", source)
                self.assertIn("browser.close()", source)
                self.assertIn("BROWSER_RUNTIME_VERIFICATION", source)
                self.assertLess(
                    source.index("playwright install chromium"),
                    source.index("chromium.launch"),
                )
                self.assertLess(
                    source.index("chromium.launch"),
                    source.index("browser.close()"),
                )

    def test_fresh_docker_gates_complete_the_real_one_time_setup_api(self):
        release_gate = (ROOT / ".github" / "workflows" / "release-gate.yml").read_text(encoding="utf-8")
        performance = (ROOT / ".github" / "workflows" / "performance.yml").read_text(encoding="utf-8")

        for source in (release_gate, performance):
            self.assertIn("scripts/ci_first_run.py", source)
            self.assertIn("--confirm-isolated", source)
            self.assertIn("--code-stdin", source)
            self.assertIn("sudo cat", source)
            self.assertIn(".example.test", source)
        self.assertIn("RELEASE_GATE_DATA_ROOT", release_gate)
        self.assertIn("PERF_DATA_ROOT", performance)
        self.assertIn("CSRF_COOKIE_SECURE=false", release_gate)
        self.assertNotIn("private/setup-code | tee", release_gate)
        self.assertNotIn("private/setup-code | tee", performance)

    def test_release_performance_is_rc_only_but_beta_dependencies_remain_live(self):
        release = workflow("release.yml")
        performance = release["jobs"]["performance"]
        self.assertEqual(performance["uses"], "./.github/workflows/performance.yml")
        self.assertEqual(performance["needs"], "preflight")
        self.assertEqual(performance["if"], "${{ inputs.operation == 'qualify' && inputs.channel == 'rc' }}")
        self.assertEqual(
            performance["with"]["candidate_sha"],
            "${{ needs.preflight.outputs.candidate_sha }}",
        )

        source = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
        promote_source = (ROOT / ".github" / "workflows" / "promote-release.yml").read_text(encoding="utf-8")
        authority = release["jobs"]["release-authority"]
        self.assertEqual(
            authority["needs"],
            [
                "preflight",
                "full-ci",
                "full-release-gate",
                "performance",
                "platform-qualification",
            ],
        )
        self.assertEqual(authority["if"], "${{ always() }}")
        authority_source = source[source.index("  release-authority:\n") : source.index("  dry-run:\n")]
        self.assertIn("toJSON(needs)", authority_source)
        self.assertIn("ref: ${{ needs.preflight.outputs.candidate_sha }}", authority_source)
        self.assertIn("python -m scripts.release_authority", authority_source)
        self.assertEqual(source.count("python -m scripts.release_authority"), 4)
        self.assertNotIn("python scripts/release_authority.py", source)
        self.assertEqual(promote_source.count("python -m scripts.release_authority"), 1)
        self.assertNotIn("python scripts/release_authority.py", promote_source)
        self.assertEqual(
            release["jobs"]["dry-run"]["needs"],
            ["preflight", "release-authority", "platform-qualification"],
        )
        self.assertEqual(
            release["jobs"]["publish"]["needs"],
            ["preflight", "release-authority"],
        )
        for job_name in ("dry-run", "publish"):
            self.assertNotIn("performance", release["jobs"][job_name]["needs"])
        self.assertNotIn("performance.yml", (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))
        self.assertNotIn("performance.yml", (ROOT / ".github" / "workflows" / "release-gate.yml").read_text(encoding="utf-8"))
        self.assertIn(
            "needs: [preflight, full-ci, full-release-gate, performance, platform-qualification]",
            authority_source,
        )
        self.assertEqual(authority["permissions"], {"contents": "read", "actions": "read"})
        self.assertIn("run_attempt=\"$(jq -r '.run_attempt // empty'", authority_source)
        self.assertIn('[[ "$run_attempt" =~ ^[1-9][0-9]*$ ]]', authority_source)

    def test_phase_b_publish_scheduling_is_skip_safe_and_fail_closed(self):
        release = workflow("release.yml")
        publish = release["jobs"]["publish"]
        condition = publish["if"]
        expected_condition = (
            "${{ !cancelled() && inputs.operation == 'publish' "
            "&& needs.preflight.result == 'success' "
            "&& needs.release-authority.result == 'success' }}"
        )

        self.assertEqual(publish["needs"], ["preflight", "release-authority"])
        self.assertEqual(condition, expected_condition)
        for required_guard in (
            "!cancelled()",
            "inputs.operation == 'publish'",
            "needs.preflight.result == 'success'",
            "needs.release-authority.result == 'success'",
        ):
            self.assertIn(required_guard, condition)
        self.assertNotIn("always()", condition)

        cases = (
            ("qualify", False, "success", "success", False),
            ("publish", False, "success", "success", True),
            ("publish", False, "failure", "success", False),
            ("publish", False, "success", "failure", False),
            ("publish", False, "success", "skipped", False),
            ("publish", True, "success", "success", False),
        )
        for operation, cancelled, preflight, authority, expected in cases:
            with self.subTest(
                operation=operation,
                cancelled=cancelled,
                preflight=preflight,
                authority=authority,
            ):
                eligible = (
                    not cancelled
                    and operation == "publish"
                    and preflight == "success"
                    and authority == "success"
                )
                self.assertEqual(eligible, expected)

    def test_dry_run_is_read_only_and_publish_permissions_are_minimal(self):
        release = workflow("release.yml")
        dry_permissions = release["jobs"]["dry-run"]["permissions"]
        self.assertEqual(
            dry_permissions, {"contents": "read", "pull-requests": "read"}
        )
        publish_permissions = release["jobs"]["publish"]["permissions"]
        self.assertEqual(publish_permissions["contents"], "write")
        self.assertEqual(publish_permissions["packages"], "write")
        self.assertEqual(publish_permissions["id-token"], "write")
        self.assertEqual(publish_permissions["attestations"], "write")
        self.assertNotIn("write-all", str(release))

    def test_candidate_override_is_dry_run_only_and_fail_closed(self):
        release = workflow("release.yml")
        source = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
        preflight = source[source.index("  preflight:\n") : source.index("  full-ci:\n")]
        publish = source[source.index("  publish:\n") :]

        for guard in (
            'test "$(git rev-parse HEAD)" = "$GITHUB_SHA"',
            'test "$INTENDED_MAIN_SHA" = "$main_sha"',
            'elif [[ -n "$REQUESTED_CANDIDATE_SHA" ]]',
            'test "$REQUESTED_CANDIDATE_SHA" = "$GITHUB_SHA"',
            'test "$(git rev-parse HEAD)" = "$REQUESTED_CANDIDATE_SHA"',
            'test -z "$REQUESTED_CANDIDATE_SHA"',
            'test "$GITHUB_REF" = "refs/heads/main"',
            'test "$GITHUB_SHA" = "$main_sha"',
            'test "$INTENDED_MAIN_SHA" = "$GITHUB_SHA"',
            '[[ "$candidate_sha" =~ ^[0-9a-f]{40}$ ]]',
            'git merge-base --is-ancestor "$UPGRADE_BASE_SHA" "$candidate_sha"',
        ):
            self.assertIn(guard, preflight)
        self.assertNotIn("DRY_RUN", preflight)
        self.assertNotIn("ref", release["jobs"]["preflight"]["steps"][0]["with"])
        self.assertIn('ref: ${{ steps.candidate.outputs.candidate_sha }}', preflight)
        for job_name in ("full-ci", "full-release-gate", "performance"):
            uses = release["jobs"][job_name]["uses"]
            self.assertTrue(uses.startswith("./.github/workflows/"))
            self.assertNotIn("@main", uses)
        self.assertNotIn("inputs.candidate_sha", publish)
        self.assertIn("ref: main", publish)
        self.assertIn('test "$(git rev-parse origin/main)" = "$GITHUB_SHA"', publish)
        self.assertIn('test "$INTENDED_MAIN_SHA" = "$GITHUB_SHA"', publish)

    def test_candidate_dry_run_uses_exact_candidate_and_has_no_external_mutation(self):
        source = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
        dry_run = source[source.index("  dry-run:\n") : source.index("  publish:\n")]

        self.assertIn("ref: ${{ needs.preflight.outputs.candidate_sha }}", dry_run)
        self.assertIn('test "$(git rev-parse HEAD)" = "$CANDIDATE_SHA"', dry_run)
        self.assertEqual(dry_run.count("ANIMEMO_COMMIT=${{ needs.preflight.outputs.candidate_sha }}"), 2)
        self.assertNotIn("ANIMEMO_COMMIT=${{ github.sha }}", dry_run)
        self.assertIn('--commit "${{ needs.preflight.outputs.candidate_sha }}"', dry_run)
        self.assertIn('RC_COMMIT == STABLE_COMMIT', dry_run)
        self.assertIn('RC_API_DIGEST == STABLE_API_DIGEST', dry_run)
        self.assertIn('RC_WEB_DIGEST == STABLE_WEB_DIGEST', dry_run)
        self.assertIn('RC_DEPLOYMENT == STABLE_DEPLOYMENT', dry_run)
        self.assertIn(".artifacts.deploymentContract", dry_run)
        for mutation in (
            "docker push",
            "git push",
            "git tag --annotate",
            "gh release create",
            "actions/attest",
            "docker/login-action",
        ):
            self.assertNotIn(mutation, dry_run)

    def test_release_notes_start_at_previous_stable_or_the_bootstrap_baseline(self):
        source = (ROOT / ".github" / "workflows" / "release.yml").read_text(
            encoding="utf-8"
        )
        dry_run = source[source.index("  dry-run:\n") : source.index("  publish:\n")]

        self.assertIn("UPGRADE_BASE_SHA: ${{ inputs.upgrade_base_sha }}", dry_run)
        self.assertIn('release_notes_base="$UPGRADE_BASE_SHA"', dry_run)
        self.assertIn('if [[ -n "$PREVIOUS_STABLE" ]]', dry_run)
        self.assertIn(
            'release_notes_base="$(git rev-parse "$PREVIOUS_STABLE^{commit}")"',
            dry_run,
        )
        self.assertIn(
            'git merge-base --is-ancestor "$release_notes_base" "$CANDIDATE_SHA"',
            dry_run,
        )
        self.assertIn('--range-start "$release_notes_base"', dry_run)
        self.assertNotIn('--range-start "$UPGRADE_BASE_SHA"', dry_run)
        self.assertNotIn('test -n "$PREVIOUS_STABLE"', dry_run)

    def test_release_images_receive_the_same_runtime_identity_as_the_manifest(self):
        source = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
        self.assertGreaterEqual(source.count("ANIMEMO_VERSION=${{ needs.preflight.outputs.release_tag }}"), 4)
        self.assertEqual(source.count("ANIMEMO_COMMIT=${{ needs.preflight.outputs.candidate_sha }}"), 2)
        self.assertEqual(source.count("ANIMEMO_COMMIT=${{ github.sha }}"), 2)
        self.assertNotIn("VITE_TURNSTILE_SITE_KEY", source)
        self.assertIn("promote-manifest", source)
        self.assertEqual(source.count("scripts/rehearse-release-images.sh"), 2)
        self.assertIn("Start and accept the exact images before any external publication", source)
        self.assertIn("Publish only the already rehearsed images", source)
        self.assertNotIn("Build and publish API image once", source)
        publish_section = source.index("  publish:\n")
        rehearse = source.index("Start and accept the exact images before any external publication", publish_section)
        publish = source.index("Publish only the already rehearsed images", publish_section)
        self.assertLess(rehearse, publish)
        self.assertNotIn("push: true", source[publish_section:publish])

    def test_immutable_release_setting_is_checked_before_any_rc_publication(self):
        source = (ROOT / ".github" / "workflows" / "release.yml").read_text(
            encoding="utf-8"
        )
        publish = source[source.index("  publish:\n") :]
        setting_gate = (
            'test "$(gh api "repos/$GITHUB_REPOSITORY/immutable-releases" '
            '--jq \'.enabled\')" = "true"'
        )

        self.assertIn(setting_gate, publish)
        self.assertLess(publish.index(setting_gate), publish.index("docker/login-action"))
        self.assertLess(publish.index(setting_gate), publish.index("docker push"))
        self.assertLess(publish.index(setting_gate), publish.index("git push origin"))
        self.assertLess(
            publish.index("build-initial-trust-kit"),
            publish.index("docker/login-action"),
        )
        self.assertLess(
            publish.index("build-initial-trust-kit"),
            publish.index("docker push"),
        )

    def test_release_contract_assets_and_real_upgrade_delta_are_fail_closed(self):
        release = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
        promotion = (ROOT / ".github" / "workflows" / "promote-release.yml").read_text(encoding="utf-8")
        gate = (ROOT / "scripts" / "stateful-upgrade-gate.sh").read_text(encoding="utf-8")

        self.assertEqual(release.count("generate-deployment-contract"), 2)
        self.assertEqual(release.count("build-installer-materials"), 2)
        self.assertGreaterEqual(release.count("-r durability/requirements.txt"), 2)
        self.assertGreaterEqual(release.count("installer-materials.tar"), 10)
        self.assertGreaterEqual(release.count("deployment-contract.json"), 8)
        self.assertGreaterEqual(promotion.count("deployment-contract.json"), 7)
        self.assertGreaterEqual(promotion.count("installer-materials.tar"), 7)
        self.assertIn(
            "cp --no-clobber rc-assets/installer-materials.tar promotion-output/installer-materials.tar",
            promotion,
        )
        self.assertNotIn("build-installer-materials", promotion)
        self.assertIn("POSTGRES_IMAGE: docker.io/library/postgres@sha256:075f7ba66bc9b3ce7d6b8b635208ff61cd7cf1a67d71ec530eec5d7ae0cbe571", release)
        self.assertIn("REDIS_IMAGE: docker.io/library/redis@sha256:9702d01c1f10c3ea9f48211b4362e44f154ff02d063e6f7268eba804059f53bf", release)
        self.assertIn('test "$UPGRADE_BASE_SHA" != "$candidate_sha"', release)
        self.assertIn('if [[ "$BASE_SHA" == "$HEAD_SHA" ]]', gate)
        release_gate = (ROOT / ".github" / "workflows" / "release-gate.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("timeout-minutes: 40", release_gate)
        self.assertIn('merge-base --is-ancestor "$BASE_SHA" "$HEAD_SHA"', gate)

    def test_release_verifier_is_built_offline_from_a_pinned_go_toolchain(self):
        release = (ROOT / ".github" / "workflows" / "release.yml").read_text(
            encoding="utf-8"
        )
        self.assertEqual(
            release.count(
                "actions/setup-go@924ae3a1cded613372ab5595356fb5720e22ba16"
            ),
            2,
        )
        self.assertEqual(release.count("go-version: '1.25.8'"), 2)
        self.assertEqual(
            release.count("release/release_attestation_verifier/go.sum"), 2
        )
        self.assertEqual(release.count("go mod download"), 2)
        self.assertEqual(release.count("GOPROXY=off GOSUMDB=off go mod verify"), 2)
        self.assertEqual(
            release.count("GOPROXY=off GOSUMDB=off go test ./..."), 2
        )
        self.assertEqual(
            release.count(
                "CGO_ENABLED=0 GOPROXY=off GOSUMDB=off go build "
                "-mod=readonly -trimpath -o offline-release-verifier ."
            ),
            2,
        )
        self.assertEqual(
            release.count(
                "test -x release/release_attestation_verifier/offline-release-verifier"
            ),
            2,
        )
        self.assertEqual(release.count("build-initial-trust-kit"), 2)
        self.assertNotIn(
            "--output release/release_attestation_verifier/pretrust-v2",
            release,
        )
        self.assertGreaterEqual(release.count("$RUNNER_TEMP/animemo-pretrust-v2"), 8)
        self.assertEqual(
            release.count(
                '--initial-trust-kit "$RUNNER_TEMP/animemo-pretrust-v2"'
            ),
            2,
        )

    def test_publish_rebinds_exact_qualified_prepublication_materials(self):
        release = (ROOT / ".github" / "workflows" / "release.yml").read_text(
            encoding="utf-8"
        )
        dry_run_identity = release.index(
            "> release-output/prepublication-materials.json"
        )
        qualification_copy = release.index(
            "cp release-dry-run-input/prepublication-materials.json"
        )
        publish_rebuild = release.index(
            "Rebuild and bind exact qualified prepublication materials before mutation"
        )
        immutable_recheck = release.index(
            "Recheck immutable release identity immediately before publishing"
        )
        docker_login = release.index("docker/login-action@", publish_rebuild)
        self.assertLess(dry_run_identity, qualification_copy)
        self.assertLess(publish_rebuild, immutable_recheck)
        self.assertLess(publish_rebuild, docker_login)
        self.assertIn("initialTrustBootstrapSha256", release)
        self.assertIn("installerMaterialsSha256", release)
        self.assertIn('test "$actual_trust" =', release)
        self.assertIn('test "$actual_materials" =', release)

    def test_every_github_authority_job_gates_the_exact_cli_security_version(self):
        release = (ROOT / ".github" / "workflows" / "release.yml").read_text(
            encoding="utf-8"
        )
        promotion = (
            ROOT / ".github" / "workflows" / "promote-release.yml"
        ).read_text(encoding="utf-8")
        gate = "Gate exact GitHub CLI security baseline"

        self.assertIn("GH_REQUIRED_VERSION: 2.97.0", release)
        self.assertIn("GH_REQUIRED_VERSION: 2.97.0", promotion)
        self.assertEqual(release.count(gate), 4)
        self.assertEqual(promotion.count(gate), 2)

    def test_platform_qualification_is_hosted_scoped_and_injected_exactly(self):
        release = workflow("release.yml")
        source = (ROOT / ".github" / "workflows" / "release.yml").read_text(
            encoding="utf-8"
        )
        promotion = (ROOT / ".github" / "workflows" / "promote-release.yml").read_text(
            encoding="utf-8"
        )
        job = release["jobs"]["platform-qualification"]

        self.assertEqual(job["runs-on"], "ubuntu-latest")
        self.assertEqual(job["if"], "${{ inputs.operation == 'qualify' }}")
        self.assertEqual(
            job["needs"], ["preflight", "full-ci", "full-release-gate"]
        )
        qualification = source[
            source.index("  platform-qualification:\n") : source.index(
                "  release-authority:\n"
            )
        ]
        for exact_identity_guard in (
            'test "$GITHUB_ACTIONS" = "true"',
            'test "$RUNNER_OS" = "Linux"',
            'test "$RUNNER_ARCH" = "X64"',
            'test "$GITHUB_SHA" = "$CANDIDATE_SHA"',
            'test "$GITHUB_WORKFLOW_SHA" = "$CANDIDATE_SHA"',
            'test "$(git rev-parse HEAD)" = "$CANDIDATE_SHA"',
        ):
            self.assertIn(exact_identity_guard, qualification)
        for real_rehearsal in (
            "installer.tests.test_runtime",
            "from installer.production import build_runtime",
            "deploy/docker-compose.yml config --quiet",
            "scripts.tests.test_restore_postgres",
            "scripts.tests.test_migration_postgres",
            "updater.tests.test_adoption",
            "updater.tests.test_linux_e2e",
            "scripts.tests.test_durability_doctor",
        ):
            self.assertIn(real_rehearsal, qualification)
        for marker in (
            "fresh_install",
            "logical_restore",
            "logical_migration",
            "updater_handoff",
            "doctor_complete",
        ):
            self.assertIn(f'$REHEARSAL_DIR/{marker}', qualification)
        for observed_capability in (
            "postgresql-client-16",
            "platform_qualification.py collect",
            '--postgres-image "$POSTGRES_IMAGE"',
            '--redis-image "$REDIS_IMAGE"',
            '--source-database-url "$ANIMEMO_TEST_DATABASE_URL"',
            '--target-database-url "$ANIMEMO_RESTORE_TEST_DATABASE_URL"',
            '--rehearsal-directory "$REHEARSAL_DIR"',
            "platform-qualification-${{ github.run_id }}",
        ):
            self.assertIn(observed_capability, qualification)

        self.assertEqual(
            source.count("release/platform-qualification.json | cmp - release/platform-qualification.json"),
            2,
        )
        self.assertGreaterEqual(
            source.count("platform_qualification.py verify"), 4
        )
        self.assertIn("path: release-qualification/", source)
        self.assertIn(
            "cp platform-qualification-input/platform-qualification.json", source
        )
        self.assertIn("release-qualification/platform-qualification.json", source)
        self.assertIn(
            "validated-platform-qualification-${{ github.run_id }}", source
        )
        self.assertIn(
            "cp --no-clobber rc-assets/installer-materials.tar promotion-output/installer-materials.tar",
            promotion,
        )
        self.assertNotIn("platform_qualification.py collect", promotion)
        self.assertNotIn("build-installer-materials", promotion)

    def test_exact_image_rehearsal_is_runner_scoped_and_read_only(self):
        source = (ROOT / "scripts" / "rehearse-release-images.sh").read_text(encoding="utf-8")

        self.assertIn('--confirm-isolated', source)
        self.assertIn('${GITHUB_ACTIONS:-}', source)
        self.assertIn('$RUNNER_TEMP/animemo-release-images.', source)
        self.assertIn('down -v --remove-orphans', source)
        self.assertIn('scripts/ci_first_run.py', source)
        self.assertIn('--code-stdin', source)
        self.assertNotIn('docker push', source)
        self.assertNotIn('docker system prune', source)
        self.assertNotIn('docker builder prune', source)

    def test_exact_image_rehearsal_closes_the_required_configuration_revision(self):
        source = (ROOT / "scripts" / "rehearse-release-images.sh").read_text(
            encoding="utf-8"
        )
        revision = "33333333-3333-4333-8333-333333333333"

        self.assertEqual(source.count("ANIMEMO_CONFIG_REVISION"), 2)
        self.assertIn(f"ANIMEMO_CONFIG_REVISION={revision}", source)
        self.assertIn('export ANIMEMO_CONFIG_REVISION="33333333-3333-4333-8333-333333333333"', source)
        self.assertLess(
            source.index(f"ANIMEMO_CONFIG_REVISION={revision}"),
            source.index('"${COMPOSE[@]}" config --quiet'),
        )

    def test_exact_image_rehearsal_closes_every_required_compose_variable(self):
        source = (ROOT / "scripts" / "rehearse-release-images.sh").read_text(
            encoding="utf-8"
        )
        compose_sources = (
            ROOT / "deploy" / "docker-compose.yml",
            ROOT / "updater" / "docker-compose.runtime.yml",
            ROOT / "deploy" / "docker-compose.upgrade-gate.yml",
        )
        required = set()
        for path in compose_sources:
            required.update(
                re.findall(
                    r"\$\{([A-Z0-9_]+):\?[^}]+\}",
                    path.read_text(encoding="utf-8"),
                )
            )
        env_file = re.search(
            r'cat > "\$ENV_FILE" <<EOF\n(?P<body>.*?)\nEOF',
            source,
            re.DOTALL,
        )
        self.assertIsNotNone(env_file)
        bindings = set(
            re.findall(r"^([A-Z0-9_]+)=", env_file.group("body"), re.MULTILINE)
        )

        self.assertSetEqual(required - bindings, set())

    def test_exact_image_rehearsal_trusts_only_the_runtime_web_proxy(self):
        source = (ROOT / "scripts" / "rehearse-release-images.sh").read_text(encoding="utf-8")

        self.assertIn("TRUSTED_PROXY_IPS=127.0.0.1/32", source)
        self.assertIn(".NetworkSettings.Networks", source)
        self.assertIn('print(f"{address}/32")', source)
        self.assertIn("TRUSTED_PROXY_CIDR", source)
        self.assertIn("--force-recreate --wait --wait-timeout 120 api", source)
        self.assertIn("AdminAuditLog.objects.get(action='installation.initialized').ip_address", source)
        self.assertIn("recorded_ip == proxy_ip", source)
        self.assertNotIn("TRUSTED_PROXY_IPS=172.16.0.0/12", source)

    def test_stable_notes_derive_from_the_frozen_rc_snapshot(self):
        source = (ROOT / ".github" / "workflows" / "promote-release.yml").read_text(encoding="utf-8")
        self.assertIn("previous-stable", source)
        self.assertIn("promote-release-notes", source)
        self.assertIn("--rc-notes promotion-output/rc-release-notes.json", source)
        self.assertIn("--notes-file promotion-output/release-notes.md", source)
        self.assertNotIn("--generate-notes", source)

    def test_stable_promotion_has_no_image_build_and_requires_acceptance(self):
        promotion_path = ROOT / ".github" / "workflows" / "promote-release.yml"
        source = promotion_path.read_text(encoding="utf-8")
        promotion = yaml.load(source, Loader=yaml.BaseLoader)
        self.assertEqual(set(promotion["on"]), {"workflow_dispatch"})
        self.assertNotIn("acceptance_confirmation", promotion["on"]["workflow_dispatch"]["inputs"])
        self.assertIn("dry_run", promotion["on"]["workflow_dispatch"]["inputs"])
        self.assertIn('acceptance_path="release/acceptance-records/$RC_TAG.json"', source)
        self.assertIn("git ls-files --error-unmatch", source)
        self.assertIn("scripts/rc_live_acceptance.py", source)
        self.assertNotIn("docker/build-push-action", source)
        self.assertNotIn("docker build", source)
        self.assertIn("RC_COMMIT == STABLE_COMMIT", source)
        self.assertIn("RC_API_DIGEST == STABLE_API_DIGEST", source)
        self.assertIn("RC_WEB_DIGEST == STABLE_WEB_DIGEST", source)

    def test_stable_promotion_dry_run_checks_out_before_downloading_artifact(self):
        promotion = workflow("promote-release.yml")
        steps = promotion["jobs"]["dry-run"]["steps"]
        checkout = next(
            index
            for index, step in enumerate(steps)
            if step.get("uses")
            == f"actions/checkout@{PINNED_RELEASE_ACTIONS['actions/checkout']}"
        )
        download = next(
            index
            for index, step in enumerate(steps)
            if step.get("uses")
            == f"actions/download-artifact@{PINNED_RELEASE_ACTIONS['actions/download-artifact']}"
        )

        self.assertLess(checkout, download)

    def test_stable_promotion_revalidates_authority_before_external_mutation(self):
        promotion = workflow("promote-release.yml")
        publish = promotion["jobs"]["publish"]
        self.assertEqual(publish["steps"][0]["with"]["ref"], "${{ github.sha }}")

        source = (ROOT / ".github" / "workflows" / "promote-release.yml").read_text(encoding="utf-8")
        publish_source = source[source.index("  publish:\n") :]
        before_first_mutation = publish_source[: publish_source.index("crane tag")]
        for guard in (
            'test "$(git rev-parse origin/main)" = "$GITHUB_SHA"',
            '! git ls-remote --exit-code --tags origin "refs/tags/$STABLE_TAG"',
            '! gh release view "$STABLE_TAG" --repo "$GITHUB_REPOSITORY"',
        ):
            self.assertIn(guard, before_first_mutation)

    def test_rc_publication_is_draft_upload_verify_publish_with_qualified_notes(self):
        release = workflow("release.yml")
        names = [step.get("name", "") for step in release["jobs"]["publish"]["steps"]]
        expected = (
            "Generate the closed publication plan without mutation",
            "Create the immutable annotated RC tag",
            "Create an unpublished GitHub Draft Pre-release",
            "Upload and read back the complete Draft asset set",
            "Publish only the fully verified Draft Pre-release",
            "Verify the public RC without authenticated asset transport",
        )
        indices = [names.index(name) for name in expected]
        self.assertEqual(indices, sorted(indices))
        source = (ROOT / ".github" / "workflows" / "release.yml").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("--generate-notes", source)
        self.assertIn("release_notes.snapshot_identity", source)
        self.assertIn("RELEASE_NOTES_MARKDOWN_SHA256", source)
        self.assertIn("release-notes.md", source)
        self.assertIn("env -u GH_TOKEN curl", source)
        self.assertLess(
            source.index('gh release create "$RELEASE_TAG"'),
            source.index('gh release upload "$RELEASE_TAG"'),
        )
        self.assertLess(
            source.index('gh release upload "$RELEASE_TAG"'),
            source.index('gh release edit "$RELEASE_TAG"'),
        )

    def test_stable_publication_uses_the_same_draft_transaction_and_never_rebuilds(self):
        source = (ROOT / ".github" / "workflows" / "promote-release.yml").read_text(
            encoding="utf-8"
        )
        publish_source = source[source.index("  publish:\n") :]
        self.assertIn("Create an unpublished Stable Draft Release", source)
        self.assertIn("Upload and read back the complete Stable Draft asset set", source)
        self.assertIn("Publish only the fully verified Stable Draft as Latest", source)
        self.assertIn("Verify the public Stable release without authenticated asset transport", source)
        self.assertNotIn("docker/build-push-action", source)
        self.assertNotIn("docker build", source)
        self.assertIn("plan-stable-publication-files", source)
        self.assertNotIn(
            'test "$(git rev-parse origin/main)" = "$GITHUB_SHA"',
            publish_source[publish_source.index("crane tag") :],
        )


if __name__ == "__main__":
    unittest.main()
