from __future__ import annotations

import hashlib
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from release.portable import BLOCKED_PORTABLE_PUBLICATION_AUTHORITY
from release.publication import build_publication_plan
from scripts.release_authority import (
    ReleaseAuthorityError,
    main,
    validate_portable_pipeline_authority,
    validate_release_authority,
)


def needs(*, preflight="success", full_ci="success", release_gate="success", performance="skipped"):
    return {
        "preflight": {"result": preflight},
        "full-ci": {"result": full_ci},
        "full-release-gate": {"result": release_gate},
        "performance": {"result": performance},
    }


class ReleaseAuthorityTests(unittest.TestCase):
    def test_portable_pipeline_authority_binds_declared_asset_without_expanding_canonical_four(self):
        portable = b"portable"
        name = "animemo-v1.1.0-rc.TEST-portable.tar"
        plan = build_publication_plan(
            repository="yanyuhanyue/AniMemo",
            channel="rc",
            tag="v1.1.0-rc.TEST",
            commit="a" * 40,
            qualification_identity="sha256:" + "1" * 64,
            release_notes_identity="sha256:" + "2" * 64,
            release_notes_markdown_sha256="sha256:" + "3" * 64,
            assets={
                asset: {"sha256": "sha256:" + str(index) * 64, "size": index}
                for index, asset in enumerate(
                    (
                        "checksums.txt",
                        "deployment-contract.json",
                        "installer-materials.tar",
                        "release-manifest.json",
                    ),
                    1,
                )
            },
            api_digest="sha256:" + "5" * 64,
            web_digest="sha256:" + "6" * 64,
            transport_assets={
                name: {
                    "role": "PORTABLE_RELEASE_BUNDLE",
                    "sha256": "sha256:" + hashlib.sha256(portable).hexdigest(),
                    "size": len(portable),
                }
            },
        )
        receipt = {
            "archive": f"release-output/{name}",
            "sha256": "sha256:" + hashlib.sha256(portable).hexdigest(),
            "files": 12,
            "imageRoles": ["api", "postgres", "redis", "web"],
            "authorityState": BLOCKED_PORTABLE_PUBLICATION_AUTHORITY,
        }

        result = validate_portable_pipeline_authority(plan, receipt)

        self.assertEqual(result["canonical_authority_asset_count"], 4)
        self.assertEqual(result["declared_transport_asset_count"], 1)
        self.assertEqual(result["build_once"], "PASS")
        changed = dict(receipt)
        changed["sha256"] = "sha256:" + "f" * 64
        with self.assertRaises(ReleaseAuthorityError):
            validate_portable_pipeline_authority(plan, changed)

    def test_portable_operation_reads_only_the_closed_release_output_inputs(self):
        def inputs(payload: bytes, tag: str) -> tuple[dict, dict]:
            name = f"animemo-{tag}-portable.tar"
            plan = build_publication_plan(
                repository="yanyuhanyue/AniMemo",
                channel="rc",
                tag=tag,
                commit="a" * 40,
                qualification_identity="sha256:" + "1" * 64,
                release_notes_identity="sha256:" + "2" * 64,
                release_notes_markdown_sha256="sha256:" + "3" * 64,
                assets={
                    asset: {"sha256": "sha256:" + str(index) * 64, "size": index}
                    for index, asset in enumerate(
                        (
                            "checksums.txt",
                            "deployment-contract.json",
                            "installer-materials.tar",
                            "release-manifest.json",
                        ),
                        1,
                    )
                },
                api_digest="sha256:" + "5" * 64,
                web_digest="sha256:" + "6" * 64,
                transport_assets={
                    name: {
                        "role": "PORTABLE_RELEASE_BUNDLE",
                        "sha256": "sha256:" + hashlib.sha256(payload).hexdigest(),
                        "size": len(payload),
                    }
                },
            )
            receipt = {
                "archive": f"release-output/{name}",
                "sha256": "sha256:" + hashlib.sha256(payload).hexdigest(),
                "files": 12,
                "imageRoles": ["api", "postgres", "redis", "web"],
                "authorityState": BLOCKED_PORTABLE_PUBLICATION_AUTHORITY,
            }
            return plan, receipt

        canonical_plan, canonical_receipt = inputs(b"canonical", "v1.1.0-rc.1")
        attacker_plan, attacker_receipt = inputs(b"attacker", "v1.1.0-rc.2")
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            release_output = root_path / "release-output"
            release_output.mkdir()
            (release_output / "publication-plan.json").write_text(
                json.dumps(canonical_plan), encoding="utf-8"
            )
            (release_output / "portable-build-receipt.json").write_text(
                json.dumps(canonical_receipt), encoding="utf-8"
            )
            attacker_root = root_path / "attacker"
            attacker_root.mkdir()
            attacker_plan_path = attacker_root / "publication-plan.json"
            attacker_receipt_path = attacker_root / "portable-build-receipt.json"
            attacker_plan_path.write_text(json.dumps(attacker_plan), encoding="utf-8")
            attacker_receipt_path.write_text(json.dumps(attacker_receipt), encoding="utf-8")

            previous_cwd = Path.cwd()
            output = io.StringIO()
            try:
                os.chdir(root_path)
                with mock.patch.dict(
                    os.environ,
                    {
                        "OPERATION": "portable",
                        "PUBLICATION_PLAN_PATH": str(attacker_plan_path),
                        "PORTABLE_BUILD_RECEIPT_PATH": str(attacker_receipt_path),
                    },
                    clear=False,
                ), redirect_stdout(output):
                    self.assertEqual(main(), 0)
            finally:
                os.chdir(previous_cwd)

        result = json.loads(output.getvalue())
        self.assertEqual(result["portable_sha256"], canonical_receipt["sha256"])

    def test_release_workflow_publishes_and_packages_only_candidate_accepted_oci(self):
        workflow = (
            Path(__file__).resolve().parents[2] / ".github" / "workflows" / "release.yml"
        ).read_text(encoding="utf-8")

        self.assertNotIn("Materialize exact OCI layouts without rebuilding", workflow)
        self.assertIn("Publish only the Candidate-accepted OCI layouts", workflow)
        self.assertIn("Assemble the portable transport from accepted OCI layouts", workflow)
        self.assertIn(
            'cp -a "$ANIMEMO_ACCEPTED_CANDIDATE_ROOT/candidate-runtime/oci"',
            workflow,
        )
        self.assertIn("python -m release.cli build-portable", workflow)
        self.assertIn("--portable \"release-output/$PORTABLE_ASSET\"", workflow)
        self.assertIn("release-output/$PORTABLE_ASSET", workflow)
        self.assertIn("python scripts/acquire_release_attestation.py", workflow)
        self.assertIn("animemo-$RELEASE_TAG-release-attestation.json", workflow)
        self.assertLess(
            workflow.index("Publish only the Candidate-accepted OCI layouts"),
            workflow.index("Assemble the portable transport from accepted OCI layouts"),
        )
        self.assertLess(
            workflow.index("Assemble the portable transport from accepted OCI layouts"),
            workflow.index("python -m release.cli build-portable"),
        )
        self.assertLess(
            workflow.index("python -m release.cli build-portable"),
            workflow.index("Create an unpublished GitHub Draft Pre-release"),
        )
        self.assertGreater(
            workflow.index("python scripts/acquire_release_attestation.py"),
            workflow.index("Publish only the fully verified Draft Pre-release"),
        )
        self.assertIn(
            '--payload "$RUNNER_TEMP/public-readback/$PORTABLE_ASSET"', workflow
        )

    def test_stable_workflow_reuses_rc_oci_layouts_and_exports_a_new_stable_sidecar(self):
        workflow = (
            Path(__file__).resolve().parents[2]
            / ".github"
            / "workflows"
            / "promote-release.yml"
        ).read_text(encoding="utf-8")

        self.assertIn("python -m release.cli promote-portable", workflow)
        self.assertIn("--rc-payload \"rc-assets/$RC_PORTABLE_ASSET\"", workflow)
        self.assertIn("--portable \"promotion-output/$STABLE_PORTABLE_ASSET\"", workflow)
        self.assertIn("python scripts/acquire_release_attestation.py", workflow)
        for name in (
            "release-manifest",
            "deployment-contract",
            "installer-materials",
        ):
            self.assertIn(
                f"--actions-source-commit {name}=$GITHUB_SHA", workflow
            )
        self.assertIn(
            '--payload "$RUNNER_TEMP/stable-public-readback/$STABLE_PORTABLE_ASSET"',
            workflow,
        )
        self.assertNotIn("docker/build-push-action", workflow)
    def test_beta_accepts_only_an_intentionally_skipped_performance_gate(self):
        self.assertEqual(validate_release_authority("beta", needs()), {"channel": "beta", "status": "PASS"})
        for result in ("success", "failure", "cancelled"):
            with self.subTest(result=result), self.assertRaises(ReleaseAuthorityError):
                validate_release_authority("beta", needs(performance=result))

    def test_rc_requires_the_performance_gate_to_succeed(self):
        self.assertEqual(
            validate_release_authority("rc", needs(performance="success")),
            {"channel": "rc", "status": "PASS"},
        )
        for result in ("skipped", "failure", "cancelled"):
            with self.subTest(result=result), self.assertRaises(ReleaseAuthorityError):
                validate_release_authority("rc", needs(performance=result))

    def test_existing_release_gates_always_fail_closed(self):
        cases = (
            needs(preflight="failure"),
            needs(full_ci="failure"),
            needs(release_gate="cancelled"),
        )
        for state in cases:
            with self.subTest(state=state), self.assertRaises(ReleaseAuthorityError):
                validate_release_authority("beta", state)

    def test_unknown_channel_or_missing_result_is_rejected(self):
        with self.assertRaises(ReleaseAuthorityError):
            validate_release_authority("stable", needs())
        malformed = needs()
        del malformed["full-ci"]
        with self.assertRaises(ReleaseAuthorityError):
            validate_release_authority("beta", malformed)


if __name__ == "__main__":
    unittest.main()
