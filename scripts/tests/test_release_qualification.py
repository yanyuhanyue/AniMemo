from __future__ import annotations

import copy
import hashlib
import inspect
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from scripts.release_authority import (
    ReleaseAuthorityError,
    _read_fixed_qualification_artifact,
    validate_phase_a_authority,
    validate_phase_b_authority,
)
from scripts.release_authority import main as release_authority_main
from scripts.release_qualification import (
    REQUIRED_RESULT_JOB_IDS,
    QualificationError,
    build_qualification_evidence,
    read_qualification_evidence,
    resolve_qualification_evidence,
    validate_qualification_evidence,
)


class QualificationV3ContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.candidate = "a" * 40
        self.base = "b" * 40
        self.tree = "c" * 40
        self.receipt_digest = "sha256:" + "1" * 64
        self.notes_identity = "sha256:" + "2" * 64
        self.notes_markdown = "sha256:" + "3" * 64
        self.provisional_digest = "sha256:" + "4" * 64
        self.workflow_ref = (
            "yanyuhanyue/AniMemo/.github/workflows/release.yml@refs/heads/rc-candidate"
        )
        self.needs = {name: {"result": "success"} for name in REQUIRED_RESULT_JOB_IDS}
        self.arguments = {
            "workflow_ref": self.workflow_ref,
            "workflow_sha": self.candidate,
            "run_id": "12345",
            "run_attempt": 2,
            "candidate_sha": self.candidate,
            "candidate_tree": self.tree,
            "upgrade_base_sha": self.base,
            "channel": "rc",
            "target_version": "v1.0.0",
            "release_tag": "v1.0.0-rc.1",
            "needs": self.needs,
            "current_job_id": "qualification-finalizer",
            "candidate_production_receipt_sha256": self.receipt_digest,
            "producer_job_observation": {"id": "dry-run", "result": "success"},
            "provisional_artifact": {
                "id": 99,
                "name": "candidate-materials-12345",
                "api_digest": self.provisional_digest,
                "archive_sha256": self.provisional_digest,
            },
            "created_at": "2026-09-01T10:00:00Z",
            "release_notes_identity": self.notes_identity,
            "release_notes_markdown_sha256": self.notes_markdown,
        }
        self.evidence = build_qualification_evidence(**self.arguments)

    def _run_metadata(self) -> dict[str, object]:
        return {
            "id": 12345,
            "path": ".github/workflows/release.yml",
            "name": "Release Producer",
            "event": "workflow_dispatch",
            "status": "completed",
            "conclusion": "success",
            "head_sha": self.candidate,
            "run_attempt": 2,
            "repository": {"full_name": "yanyuhanyue/AniMemo"},
            "workflow_ref": self.workflow_ref,
        }

    def _artifact_metadata(self) -> dict[str, object]:
        return {
            "name": "release-qualification-12345",
            "expired": False,
            "workflow_run": {"id": 12345},
            "archive_download_url": "https://example.invalid/archive",
            "digest": "sha256:" + "5" * 64,
        }

    def _expected(self) -> dict[str, object]:
        return {
            "qualification_run_id": "12345",
            "candidate_sha": self.candidate,
            "candidate_tree": self.tree,
            "upgrade_base_sha": self.base,
            "channel": "rc",
            "target_version": "v1.0.0",
            "release_tag": "v1.0.0-rc.1",
            "release_graph_contract": "animemo.release-gate.jobs/v2",
            "workflow_ref": self.workflow_ref,
            "workflow_sha": self.candidate,
            "release_notes_identity": self.notes_identity,
            "release_notes_markdown_sha256": self.notes_markdown,
            "candidate_production_receipt_sha256": self.receipt_digest,
        }

    def test_v3_has_closed_identity_without_current_or_future_state(self):
        self.assertEqual(self.evidence["schema"], "animemo.release-qualification/v3")
        self.assertEqual(
            self.evidence["run"],
            {"id": "12345", "attempt": 2, "event": "workflow_dispatch"},
        )
        self.assertEqual(self.evidence["candidate_tree"], self.tree)
        self.assertEqual(
            self.evidence["producer_job_observation"],
            {"id": "dry-run", "result": "success"},
        )
        self.assertEqual(
            self.evidence["candidate_production_receipt_sha256"], self.receipt_digest
        )
        self.assertEqual(self.evidence["local_finalization_result"], "PASS")
        self.assertEqual(
            self.evidence["final_run_state_authority"], "EXTERNAL_PHASE_B_REQUIRED"
        )
        self.assertNotIn("read_only_release_dry_run", self.evidence["qualification_results"])
        self.assertNotIn("artifact_sha256", self.evidence)
        self.assertTrue(self.evidence["qualification_sha256"].startswith("sha256:"))
        validate_qualification_evidence(self.evidence)

    def test_builder_signature_has_no_result_or_future_state_bypass(self):
        parameters = inspect.signature(build_qualification_evidence).parameters
        for unsafe in ("qualification_results", "status", "conclusion"):
            self.assertNotIn(unsafe, parameters)
        authority_source = Path(inspect.getsourcefile(validate_phase_a_authority) or "").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("QUALIFICATION_RESULTS_JSON", authority_source)

    def test_required_result_jobs_are_the_exact_frozen_downstream_needs(self):
        self.assertEqual(
            REQUIRED_RESULT_JOB_IDS,
            (
                "preflight",
                "full-ci",
                "full-release-gate",
                "performance",
                "platform-qualification",
                "release-authority",
                "dry-run",
            ),
        )

    def test_builder_rejects_missing_extra_or_current_job_results(self):
        missing = copy.deepcopy(self.arguments)
        missing["needs"] = {key: value for key, value in self.needs.items() if key != "dry-run"}
        with self.assertRaisesRegex(QualificationError, "IncompleteUpstreamResult"):
            build_qualification_evidence(**missing)

        extra = copy.deepcopy(self.arguments)
        extra["needs"]["qualification-finalizer"] = {"result": "success"}
        with self.assertRaisesRegex(QualificationError, "IncompleteUpstreamResult"):
            build_qualification_evidence(**extra)

        self_result = copy.deepcopy(self.arguments)
        self_result["current_job_id"] = "dry-run"
        with self.assertRaisesRegex(QualificationError, "SelfResultReference"):
            build_qualification_evidence(**self_result)

        wrong_job = copy.deepcopy(self.arguments)
        wrong_job["current_job_id"] = "some-other-job"
        with self.assertRaisesRegex(QualificationError, "job identity"):
            build_qualification_evidence(**wrong_job)

    def test_builder_rejects_fake_or_non_success_producer_observation(self):
        for observation in (
            {"id": "qualification-finalizer", "result": "success"},
            {"id": "dry-run", "result": "failure"},
            {"id": "dry-run", "result": "success", "self": True},
        ):
            arguments = copy.deepcopy(self.arguments)
            arguments["producer_job_observation"] = observation
            with self.subTest(observation=observation), self.assertRaises(QualificationError):
                build_qualification_evidence(**arguments)

    def test_provisional_artifact_is_prior_and_exactly_bound(self):
        self.assertEqual(
            set(self.evidence["provisional_artifact"]),
            {"id", "name", "api_digest", "archive_sha256"},
        )
        for changes in (
            {"name": "release-qualification-12345"},
            {"api_digest": "sha256:" + "6" * 64},
            {"id": 0},
        ):
            arguments = copy.deepcopy(self.arguments)
            arguments["provisional_artifact"].update(changes)
            with self.subTest(changes=changes), self.assertRaises(QualificationError):
                build_qualification_evidence(**arguments)

    def test_unknown_future_or_final_artifact_self_claims_fail_closed(self):
        cases = (
            ("run status", ("run", "status", "completed")),
            ("run conclusion", ("run", "conclusion", "success")),
            ("finalizer result", (None, "finalizer_result", "success")),
            ("final Artifact id", (None, "final_artifact_id", 123)),
            ("final Artifact digest", (None, "final_artifact_api_digest", "sha256:" + "7" * 64)),
        )
        for label, (parent, key, value) in cases:
            candidate = copy.deepcopy(self.evidence)
            target = candidate if parent is None else candidate[parent]
            target[key] = value
            with self.subTest(label=label), self.assertRaises(QualificationError):
                validate_qualification_evidence(candidate)

    def test_beta_performance_and_parity_are_skipped_but_all_other_needs_succeed(self):
        arguments = copy.deepcopy(self.arguments)
        arguments["channel"] = "beta"
        arguments["release_tag"] = "v1.0.0-beta.1"
        arguments["needs"]["performance"]["result"] = "skipped"
        evidence = build_qualification_evidence(**arguments)
        self.assertEqual(evidence["gate_results"]["performance"], "skipped")
        self.assertEqual(evidence["qualification_results"]["rc_performance"], "skipped")
        self.assertEqual(evidence["qualification_results"]["rc_stable_parity"], "skipped")

    def test_unknown_missing_and_checksum_tampering_fail_closed(self):
        unknown = copy.deepcopy(self.evidence)
        unknown["trust_override"] = True
        with self.assertRaises(QualificationError):
            validate_qualification_evidence(unknown)

        missing = copy.deepcopy(self.evidence)
        del missing["candidate_tree"]
        with self.assertRaises(QualificationError):
            validate_qualification_evidence(missing)

        tampered = copy.deepcopy(self.evidence)
        tampered["candidate_sha"] = "d" * 40
        with self.assertRaisesRegex(QualificationError, "checksum"):
            validate_qualification_evidence(tampered)

    def test_v1_and_v2_are_not_accepted_or_upgraded(self):
        for schema in ("animemo.release-qualification/v1", "animemo.release-qualification/v2"):
            candidate = copy.deepcopy(self.evidence)
            candidate["schema"] = schema
            with self.subTest(schema=schema), self.assertRaisesRegex(
                QualificationError, "unsupported"
            ):
                validate_qualification_evidence(candidate)

    def test_phase_a_api_builds_v3_without_supplied_result_bypass(self):
        identity = {
            "emit_evidence": True,
            "workflow_ref": self.workflow_ref,
            "workflow_sha": self.candidate,
            "run_id": "12345",
            "run_attempt": 2,
            "candidate_sha": self.candidate,
            "candidate_tree": self.tree,
            "upgrade_base_sha": self.base,
            "target_version": "v1.0.0",
            "release_tag": "v1.0.0-rc.1",
            "current_job_id": "qualification-finalizer",
            "candidate_production_receipt_sha256": self.receipt_digest,
            "producer_job_observation": {"id": "dry-run", "result": "success"},
            "provisional_artifact": self.arguments["provisional_artifact"],
            "created_at": "2026-09-01T10:00:00Z",
            "release_notes_identity": self.notes_identity,
            "release_notes_markdown_sha256": self.notes_markdown,
        }
        result = validate_phase_a_authority("rc", self.needs, identity=identity)
        self.assertEqual(result["evidence"]["schema"], "animemo.release-qualification/v3")

    def test_phase_b_requires_prior_completed_success_metadata_and_archive_digest(self):
        artifact = self._artifact_metadata()
        resolved = resolve_qualification_evidence(
            qualification_run_id="12345",
            run_metadata=self._run_metadata(),
            artifact=artifact,
            expected=self._expected(),
            evidence=self.evidence,
            archive_sha256=artifact["digest"],
        )
        self.assertEqual(resolved, self.evidence)

        for status, conclusion in (("in_progress", None), ("completed", "failure")):
            metadata = self._run_metadata()
            metadata.update({"status": status, "conclusion": conclusion})
            with self.subTest(status=status), self.assertRaisesRegex(
                QualificationError, "not successful"
            ):
                resolve_qualification_evidence(
                    qualification_run_id="12345",
                    run_metadata=metadata,
                    artifact=artifact,
                    expected=self._expected(),
                    evidence=self.evidence,
                    archive_sha256=artifact["digest"],
                )

    def test_phase_b_rejects_identity_attempt_artifact_and_digest_mismatches(self):
        artifact = self._artifact_metadata()
        cases = (
            ("repository", {"repository": {"full_name": "attacker/Other"}}),
            ("workflow path", {"path": ".github/workflows/other.yml"}),
            ("workflow name", {"name": "Other"}),
            ("event", {"event": "push"}),
            ("head sha", {"head_sha": "d" * 40}),
            ("attempt", {"run_attempt": 1}),
            ("workflow ref", {"workflow_ref": "refs/heads/main"}),
        )
        for label, changes in cases:
            metadata = self._run_metadata()
            metadata.update(changes)
            with self.subTest(label=label), self.assertRaises(QualificationError):
                resolve_qualification_evidence(
                    qualification_run_id="12345",
                    run_metadata=metadata,
                    artifact=artifact,
                    expected=self._expected(),
                    evidence=self.evidence,
                    archive_sha256=artifact["digest"],
                )

        for bad_artifact in (
            {**artifact, "name": "release-qualification-999"},
            {**artifact, "expired": True},
            {**artifact, "workflow_run": {"id": 999}},
            {**artifact, "digest": None},
        ):
            with self.subTest(artifact=bad_artifact), self.assertRaises(QualificationError):
                resolve_qualification_evidence(
                    qualification_run_id="12345",
                    run_metadata=self._run_metadata(),
                    artifact=bad_artifact,
                    expected=self._expected(),
                    evidence=self.evidence,
                    archive_sha256="sha256:" + "5" * 64,
                )

        with self.assertRaisesRegex(QualificationError, "cannot be proven"):
            resolve_qualification_evidence(
                qualification_run_id="12345",
                run_metadata=self._run_metadata(),
                artifact=artifact,
                expected=self._expected(),
                evidence=self.evidence,
                archive_sha256=None,
            )

    def test_phase_b_authority_has_no_local_only_fallback(self):
        with self.assertRaisesRegex(ReleaseAuthorityError, "metadata"):
            validate_phase_b_authority(
                "rc",
                {"preflight": {"result": "success"}},
                qualification=self.evidence,
                expected=self._expected(),
            )

    def test_read_evidence_accepts_only_bounded_bytes(self):
        encoded = json.dumps(self.evidence).encode("utf-8")

        class BytesSubclass(bytes):
            pass

        self.assertEqual(read_qualification_evidence(encoded), self.evidence)
        hostile_inputs = {
            "absolute path": Path("C:/outside/release-qualification-12345.json"),
            "parent traversal": Path("../release-qualification-12345.json"),
            "oversized bytes": b"x" * (8 * 1024 * 1024 + 1),
            "bytes subclass": BytesSubclass(encoded),
        }
        for label, hostile_input in hostile_inputs.items():
            with self.subTest(label=label), self.assertRaisesRegex(
                QualificationError, "bytes|size"
            ):
                read_qualification_evidence(hostile_input)  # type: ignore[arg-type]

    def test_publish_main_reads_fixed_file_and_binds_controller_digest(self):
        encoded = json.dumps(self.evidence).encode("utf-8")
        evidence_sha256 = "sha256:" + hashlib.sha256(encoded).hexdigest()
        artifact = self._artifact_metadata()
        environment = {
            "OPERATION": "publish",
            "CHANNEL": "rc",
            "NEEDS_JSON": json.dumps(self.needs),
            "QUALIFICATION_RUN_ID": "12345",
            "CANDIDATE_SHA": self.candidate,
            "UPGRADE_BASE_SHA": self.base,
            "TARGET_VERSION": "v1.0.0",
            "RELEASE_TAG": "v1.0.0-rc.1",
            "QUALIFICATION_WORKFLOW_REF": self.workflow_ref,
            "QUALIFICATION_WORKFLOW_SHA": self.candidate,
            "RELEASE_GRAPH_CONTRACT": "animemo.release-gate.jobs/v2",
            "RELEASE_NOTES_IDENTITY": self.notes_identity,
            "RELEASE_NOTES_MARKDOWN_SHA256": self.notes_markdown,
            "QUALIFICATION_RUN_METADATA": json.dumps(self._run_metadata()),
            "QUALIFICATION_ARTIFACT_METADATA": json.dumps(artifact),
            "QUALIFICATION_ARCHIVE_SHA256": str(artifact["digest"]),
            "QUALIFICATION_EVIDENCE_SHA256": evidence_sha256,
        }
        output = io.StringIO()
        previous = Path.cwd()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "qualification-evidence.json").write_bytes(encoded)
            try:
                os.chdir(root)
                with mock.patch.dict(
                    "os.environ", environment, clear=True
                ), redirect_stdout(output):
                    self.assertEqual(release_authority_main(), 0)
                with mock.patch.dict(
                    "os.environ",
                    {
                        **environment,
                        "QUALIFICATION_EVIDENCE_SHA256": "sha256:" + "0" * 64,
                    },
                    clear=True,
                ), self.assertRaisesRegex(ReleaseAuthorityError, "digest mismatch"):
                    release_authority_main()
            finally:
                os.chdir(previous)
        self.assertEqual(json.loads(output.getvalue())["status"], "PASS")

    def test_fixed_reader_rejects_post_read_hard_link_state(self):
        encoded = json.dumps(self.evidence).encode("utf-8")
        expected = "sha256:" + hashlib.sha256(encoded).hexdigest()
        previous = Path.cwd()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "qualification-evidence.json"
            target.write_bytes(encoded)
            metadata = target.stat()

            def observed(nlink: int) -> mock.Mock:
                return mock.Mock(
                    st_mode=metadata.st_mode,
                    st_nlink=nlink,
                    st_size=metadata.st_size,
                    st_dev=metadata.st_dev,
                    st_ino=metadata.st_ino,
                    st_mtime_ns=metadata.st_mtime_ns,
                )

            try:
                os.chdir(root)
                with mock.patch(
                    "scripts.release_authority.os.fstat",
                    side_effect=(observed(1), observed(2)),
                ), self.assertRaisesRegex(
                    ReleaseAuthorityError, "changed while being read"
                ):
                    _read_fixed_qualification_artifact(expected)
            finally:
                os.chdir(previous)

    def test_read_evidence_rejects_duplicate_json_keys(self):
        encoded = (
            json.dumps(self.evidence)[:-1]
            + ', "schema": "animemo.release-qualification/v1"}\n'
        ).encode("utf-8")
        with self.assertRaisesRegex(QualificationError, "duplicate"):
            read_qualification_evidence(encoded)


if __name__ == "__main__":
    unittest.main()
