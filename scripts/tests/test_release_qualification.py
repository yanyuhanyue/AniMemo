from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from scripts.release_qualification import (
    REQUIRED_GATES,
    QualificationError,
    build_qualification_evidence,
    resolve_qualification_evidence,
    validate_qualification_evidence,
)
from scripts.release_authority import ReleaseAuthorityError, validate_phase_b_authority


class QualificationContractTests(unittest.TestCase):
    def setUp(self):
        self.candidate = "a" * 40
        self.base = "b" * 40
        self.needs = {
            name: {"result": "success" if name != "performance" else "success"}
            for name in REQUIRED_GATES
        }
        self.needs.update(
            {
                "release-authority": {"result": "success"},
                "dry-run": {"result": "success"},
            }
        )
        self.evidence = build_qualification_evidence(
            workflow_ref=".github/workflows/release.yml@refs/heads/rc-candidate",
            workflow_sha=self.candidate,
            run_id="12345",
            run_attempt=2,
            candidate_sha=self.candidate,
            upgrade_base_sha=self.base,
            channel="rc",
            target_version="v1.0.0",
            release_tag="v1.0.0-rc.1",
            needs=self.needs,
        )

    def test_phase_a_artifact_has_immutable_identity_and_canonical_checksum(self):
        self.assertEqual(self.evidence["schema"], "animemo.release-qualification/v1")
        self.assertEqual(self.evidence["repository"], "yanyuhanyue/AniMemo")
        self.assertEqual(self.evidence["workflow"]["path"], ".github/workflows/release.yml")
        self.assertEqual(self.evidence["workflow"]["sha"], self.candidate)
        self.assertEqual(self.evidence["run"]["id"], "12345")
        self.assertEqual(self.evidence["run"]["attempt"], 2)
        self.assertEqual(self.evidence["candidate_sha"], self.candidate)
        self.assertEqual(self.evidence["upgrade_base_sha"], self.base)
        self.assertTrue(self.evidence["artifact_sha256"].startswith("sha256:"))
        validate_qualification_evidence(self.evidence)

    def test_unknown_or_missing_fields_and_tampering_fail_closed(self):
        unknown = copy.deepcopy(self.evidence)
        unknown["trust_override"] = True
        with self.assertRaises(QualificationError):
            validate_qualification_evidence(unknown)

        missing = copy.deepcopy(self.evidence)
        del missing["upgrade_base_sha"]
        with self.assertRaises(QualificationError):
            validate_qualification_evidence(missing)

        tampered = copy.deepcopy(self.evidence)
        tampered["candidate_sha"] = "c" * 40
        with self.assertRaisesRegex(QualificationError, "checksum"):
            validate_qualification_evidence(tampered)

    def test_wrong_gate_result_or_legacy_contract_is_rejected(self):
        wrong_gate = copy.deepcopy(self.evidence)
        wrong_gate["gate_results"]["full-ci"] = "cancelled"
        with self.assertRaises(QualificationError):
            validate_qualification_evidence(wrong_gate)

        wrong_contract = copy.deepcopy(self.evidence)
        wrong_contract["release_graph_contract"] = "animemo.release-gate.jobs/v1"
        with self.assertRaises(QualificationError):
            validate_qualification_evidence(wrong_contract)

    def test_phase_b_requires_same_run_workflow_sha_and_archive_digest(self):
        run = {
            "id": 12345,
            "path": ".github/workflows/release.yml",
            "name": "Release Producer",
            "event": "workflow_dispatch",
            "status": "completed",
            "conclusion": "success",
            "head_sha": self.candidate,
            "run_attempt": 2,
            "repository": {"full_name": "yanyuhanyue/AniMemo"},
        }
        artifact = {
            "name": "release-qualification-12345",
            "expired": False,
            "workflow_run": {"id": 12345},
            "archive_download_url": "https://example.invalid/archive",
            "digest": "sha256:" + "d" * 64,
        }
        expected = {
            "qualification_run_id": "12345",
            "candidate_sha": self.candidate,
            "upgrade_base_sha": self.base,
            "channel": "rc",
            "target_version": "v1.0.0",
            "release_tag": "v1.0.0-rc.1",
            "release_graph_contract": "animemo.release-gate.jobs/v2",
        }
        resolved = resolve_qualification_evidence(
            qualification_run_id="12345",
            run_metadata=run,
            artifact=artifact,
            expected=expected,
            evidence=self.evidence,
            archive_sha256="sha256:" + "d" * 64,
        )
        self.assertEqual(resolved, self.evidence)

        for bad_artifact in (
            {**artifact, "digest": None},
            {**artifact, "workflow_run": {"id": 999}},
            {**artifact, "expired": True},
            {key: value for key, value in artifact.items() if key != "expired"},
        ):
            with self.subTest(bad_artifact=bad_artifact), self.assertRaises(QualificationError):
                resolve_qualification_evidence(
                    qualification_run_id="12345",
                    run_metadata=run,
                    artifact=bad_artifact,
                    expected=expected,
                    evidence=self.evidence,
                    archive_sha256="sha256:" + "d" * 64,
                )

        with self.assertRaisesRegex(QualificationError, "cannot be proven"):
            resolve_qualification_evidence(
                qualification_run_id="12345",
                run_metadata=run,
                artifact=artifact,
                expected=expected,
                evidence=self.evidence,
                archive_sha256=None,
            )

        with self.assertRaisesRegex(QualificationError, "attempt"):
            resolve_qualification_evidence(
                qualification_run_id="12345",
                run_metadata={key: value for key, value in run.items() if key != "run_attempt"},
                artifact=artifact,
                expected=expected,
                evidence=self.evidence,
                archive_sha256="sha256:" + "d" * 64,
            )

    def test_phase_b_rejects_missing_trusted_metadata_instead_of_local_only_fallback(self):
        with self.assertRaisesRegex(ReleaseAuthorityError, "metadata"):
            validate_phase_b_authority(
                "rc",
                {"preflight": {"result": "success"}},
                qualification=self.evidence,
                expected={
                    "qualification_run_id": "12345",
                    "candidate_sha": self.candidate,
                    "upgrade_base_sha": self.base,
                    "target_version": "v1.0.0",
                    "release_tag": "v1.0.0-rc.1",
                },
            )

    def test_phase_b_rejects_each_run_identity_mismatch(self):
        run = {
            "id": 12345,
            "path": ".github/workflows/release.yml",
            "name": "Release Producer",
            "event": "workflow_dispatch",
            "status": "completed",
            "conclusion": "success",
            "head_sha": self.candidate,
            "run_attempt": 2,
            "repository": {"full_name": "yanyuhanyue/AniMemo"},
            "workflow_ref": "yanyuhanyue/AniMemo/.github/workflows/release.yml@refs/heads/rc-candidate",
        }
        artifact = {
            "name": "release-qualification-12345",
            "expired": False,
            "workflow_run": {"id": 12345},
            "archive_download_url": "https://example.invalid/archive",
            "digest": "sha256:" + "d" * 64,
        }
        expected = {
            "qualification_run_id": "12345",
            "candidate_sha": self.candidate,
            "upgrade_base_sha": self.base,
            "target_version": "v1.0.0",
            "release_tag": "v1.0.0-rc.1",
            "release_graph_contract": "animemo.release-gate.jobs/v2",
            "workflow_ref": run["workflow_ref"],
            "workflow_sha": self.candidate,
        }
        cases = (
            ("repository", {"repository": {"full_name": "attacker/Other"}}),
            ("workflow path", {"path": ".github/workflows/other.yml"}),
            ("workflow name", {"name": "Other Workflow"}),
            ("event", {"event": "push"}),
            ("status", {"status": "in_progress"}),
            ("conclusion", {"conclusion": "cancelled"}),
            ("head sha", {"head_sha": "c" * 40}),
            ("attempt", {"run_attempt": 1}),
            ("workflow ref", {"workflow_ref": "yanyuhanyue/AniMemo/.github/workflows/release.yml@refs/heads/main"}),
        )
        for label, changes in cases:
            with self.subTest(label=label):
                candidate = copy.deepcopy(run)
                candidate.update(changes)
                with self.assertRaises(QualificationError):
                    resolve_qualification_evidence(
                        qualification_run_id="12345",
                        run_metadata=candidate,
                        artifact=artifact,
                        expected=expected,
                        evidence=self.evidence,
                        archive_sha256=artifact["digest"],
                    )

    def test_phase_b_rejects_evidence_identity_mismatches_and_fake_passes(self):
        run = {
            "id": 12345,
            "path": ".github/workflows/release.yml",
            "name": "Release Producer",
            "event": "workflow_dispatch",
            "status": "completed",
            "conclusion": "success",
            "head_sha": self.candidate,
            "run_attempt": 2,
            "repository": {"full_name": "yanyuhanyue/AniMemo"},
            "workflow_ref": "yanyuhanyue/AniMemo/.github/workflows/release.yml@refs/heads/rc-candidate",
        }
        artifact = {
            "name": "release-qualification-12345",
            "expired": False,
            "workflow_run": {"id": 12345},
            "archive_download_url": "https://example.invalid/archive",
            "digest": "sha256:" + "d" * 64,
        }
        expected = {
            "qualification_run_id": "12345",
            "candidate_sha": self.candidate,
            "upgrade_base_sha": self.base,
            "channel": "rc",
            "target_version": "v1.0.0",
            "release_tag": "v1.0.0-rc.1",
            "release_graph_contract": "animemo.release-gate.jobs/v2",
            "workflow_ref": run["workflow_ref"],
            "workflow_sha": self.candidate,
        }
        for label, changes in (
            ("wrong base", {"upgrade_base_sha": "c" * 40}),
            ("wrong channel", {"channel": "beta"}),
            ("wrong version", {"target_version": "v2.0.0"}),
            ("wrong tag", {"release_tag": "v1.0.0-rc.2"}),
            ("unknown schema", {"schema": "animemo.release-qualification/v0"}),
            ("missing result", {"qualification_results": {}}),
        ):
            with self.subTest(label=label):
                candidate = copy.deepcopy(self.evidence)
                candidate.update(changes)
                with self.assertRaises(QualificationError):
                    resolve_qualification_evidence(
                        qualification_run_id="12345",
                        run_metadata=run,
                        artifact=artifact,
                        expected=expected,
                        evidence=candidate,
                        archive_sha256=artifact["digest"],
                    )

        with self.assertRaises(QualificationError):
            resolve_qualification_evidence(
                qualification_run_id="12345",
                run_metadata=run,
                artifact={**artifact, "name": "release-qualification-99999"},
                expected=expected,
                evidence=self.evidence,
                archive_sha256=artifact["digest"],
            )

        with self.assertRaises(QualificationError):
            resolve_qualification_evidence(
                qualification_run_id="12345",
                run_metadata=run,
                artifact=artifact,
                expected=expected,
                evidence=self.evidence,
                archive_sha256="sha256:" + "e" * 64,
            )

    def test_read_evidence_rejects_duplicate_json_keys(self):
        from scripts.release_qualification import read_qualification_evidence

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "release-qualification.json"
            path.write_text(
                json.dumps(self.evidence)[:-1]
                + ', "schema": "animemo.release-qualification/v0"}\n',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(QualificationError, "duplicate"):
                read_qualification_evidence(path)


if __name__ == "__main__":
    unittest.main()
