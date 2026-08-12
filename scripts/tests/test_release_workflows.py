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
