from __future__ import annotations

import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def workflow(name):
    # PyYAML parses the YAML 1.1 word `on` as bool; BaseLoader preserves keys.
    return yaml.load((ROOT / ".github" / "workflows" / name).read_text(encoding="utf-8"), Loader=yaml.BaseLoader)


class ReleaseWorkflowContractTests(unittest.TestCase):
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

    def test_release_workflow_is_manual_and_never_builds_stable(self):
        release = workflow("release.yml")
        self.assertEqual(set(release["on"]), {"workflow_dispatch"})
        inputs = release["on"]["workflow_dispatch"]["inputs"]
        self.assertEqual(inputs["channel"]["options"], ["beta", "rc"])
        self.assertIn("dry_run", inputs)
        self.assertIn("upgrade_base_sha", inputs)
        self.assertIn("target_version_override", inputs)
        self.assertEqual(release["jobs"]["full-ci"]["uses"], "./.github/workflows/ci.yml")
        self.assertEqual(release["jobs"]["full-release-gate"]["uses"], "./.github/workflows/release-gate.yml")
        self.assertEqual(release["jobs"]["full-ci"]["with"]["candidate_sha"], "${{ github.sha }}")
        self.assertEqual(release["jobs"]["full-ci"]["with"]["comparison_base_sha"], "${{ inputs.upgrade_base_sha }}")
        self.assertTrue(release["jobs"]["full-ci"]["with"]["force_full"])
        self.assertEqual(release["jobs"]["full-release-gate"]["with"]["candidate_sha"], "${{ github.sha }}")
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
        performance_nginx = (ROOT / "deploy" / "nginx.performance.conf").read_text(encoding="utf-8")
        self.assertIn("x_animemo_perf_client", performance_nginx)
        self.assertIn("198\\.18\\.0\\.", performance_nginx)
        self.assertIn("proxy_set_header X-Forwarded-For $anime_perf_client_ip", performance_nginx)
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

    def test_release_performance_is_rc_only_but_beta_dependencies_remain_live(self):
        release = workflow("release.yml")
        performance = release["jobs"]["performance"]
        self.assertEqual(performance["uses"], "./.github/workflows/performance.yml")
        self.assertEqual(performance["needs"], "preflight")
        self.assertEqual(performance["if"], "${{ inputs.channel == 'rc' }}")
        self.assertEqual(performance["with"]["candidate_sha"], "${{ github.sha }}")

        source = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
        authority = release["jobs"]["release-authority"]
        self.assertEqual(authority["needs"], ["preflight", "full-ci", "full-release-gate", "performance"])
        self.assertEqual(authority["if"], "${{ always() }}")
        authority_source = source[source.index("  release-authority:\n") : source.index("  dry-run:\n")]
        self.assertIn("toJSON(needs)", authority_source)
        self.assertIn("ref: ${{ github.sha }}", authority_source)
        self.assertIn("python scripts/release_authority.py", authority_source)
        for job_name in ("dry-run", "publish"):
            job = release["jobs"][job_name]
            self.assertEqual(job["needs"], ["preflight", "release-authority"])
            self.assertNotIn("performance", job["needs"])
        self.assertNotIn("performance.yml", (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))
        self.assertNotIn("performance.yml", (ROOT / ".github" / "workflows" / "release-gate.yml").read_text(encoding="utf-8"))
        self.assertIn("needs: [preflight, full-ci, full-release-gate, performance]", authority_source)

    def test_dry_run_is_read_only_and_publish_permissions_are_minimal(self):
        release = workflow("release.yml")
        dry_permissions = release["jobs"]["dry-run"]["permissions"]
        self.assertEqual(dry_permissions, {"contents": "read"})
        publish_permissions = release["jobs"]["publish"]["permissions"]
        self.assertEqual(publish_permissions["contents"], "write")
        self.assertEqual(publish_permissions["packages"], "write")
        self.assertEqual(publish_permissions["id-token"], "write")
        self.assertEqual(publish_permissions["attestations"], "write")
        self.assertNotIn("write-all", str(release))

    def test_release_images_receive_the_same_runtime_identity_as_the_manifest(self):
        source = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
        self.assertGreaterEqual(source.count("ANIMEMO_VERSION=${{ needs.preflight.outputs.release_tag }}"), 4)
        self.assertGreaterEqual(source.count("ANIMEMO_COMMIT=${{ github.sha }}"), 4)
        self.assertIn("VITE_TURNSTILE_SITE_KEY=1x00000000000000000000AA", source)
        self.assertIn("promote-manifest", source)

    def test_stable_notes_start_at_previous_stable_when_one_exists(self):
        source = (ROOT / ".github" / "workflows" / "promote-release.yml").read_text(encoding="utf-8")
        self.assertIn("previous-stable", source)
        self.assertIn("--notes-start-tag", source)

    def test_stable_promotion_has_no_image_build_and_requires_acceptance(self):
        promotion_path = ROOT / ".github" / "workflows" / "promote-release.yml"
        source = promotion_path.read_text(encoding="utf-8")
        promotion = yaml.load(source, Loader=yaml.BaseLoader)
        self.assertEqual(set(promotion["on"]), {"workflow_dispatch"})
        self.assertIn("acceptance_confirmation", promotion["on"]["workflow_dispatch"]["inputs"])
        self.assertIn("dry_run", promotion["on"]["workflow_dispatch"]["inputs"])
        self.assertNotIn("docker/build-push-action", source)
        self.assertNotIn("docker build", source)
        self.assertIn("RC_COMMIT == STABLE_COMMIT", source)
        self.assertIn("RC_API_DIGEST == STABLE_API_DIGEST", source)
        self.assertIn("RC_WEB_DIGEST == STABLE_WEB_DIGEST", source)


if __name__ == "__main__":
    unittest.main()
