from __future__ import annotations

import copy
import hashlib
import json
import shutil
import tempfile
import unittest
import zipfile
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from release.candidate import canonical_json_bytes, sha256_bytes
from release.materials import (
    MaterialContractError,
    build_candidate_production_receipt,
    extract_qualification_artifact,
)
from release.metadata_freshness import (
    ARTIFACT_FILES,
    WORKFLOW_PATH,
    FreshnessExpectation,
    FreshnessRunIdentity,
    MetadataFreshnessError,
    _receipt_digest,
    _validate_receipt,
    collect_metadata_freshness,
    extract_metadata_freshness_artifact,
    validate_freshness_run_metadata,
    validate_qualification_run_metadata,
    verify_metadata_freshness_artifact,
)
from release.notes import build_release_notes, render_release_notes
from release.release_notes_preflight import (
    build_preflight_manifest,
)
from release.release_notes_preflight import (
    canonical_json_bytes as release_notes_json_bytes,
)
from scripts.release_qualification import (
    REQUIRED_RESULT_JOB_IDS,
    build_qualification_evidence,
)

CANDIDATE = "e65f9beb0b5a19be2b4562206b38bb6d00adff7e"
TREE = "b21598e5352654985a17146b3272775df0694fbe"
BASE = "225c47e858d56e449869c32ebb7102107c151d61"
QUALIFICATION_RUN_ID = 32635898412
QUALIFICATION_ARTIFACT_ID = 9492671353
FRESHNESS_RUN_ID = 789


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()


def _normalized_pull(title: str = "修复发布门禁", *, number: int = 45) -> dict[str, object]:
    return {
        "number": number,
        "title": title,
        "source_identity": "f" * 40,
        "labels": ["release/fix"],
        "observed_updated_at": "2026-08-23T11:58:00Z",
    }


def _run_metadata(
    *, run_id: int, name: str, path: str, head_sha: str = CANDIDATE
) -> dict[str, object]:
    return {
        "id": run_id,
        "name": name,
        "path": path,
        "event": "workflow_dispatch",
        "status": "completed",
        "conclusion": "success",
        "run_attempt": 1,
        "repository": {"full_name": "yanyuhanyue/AniMemo"},
        "head_branch": "main",
        "head_sha": head_sha,
    }


def _artifact_listing(
    *,
    run_id: int,
    artifact_id: int,
    name: str,
    digest: str,
    include_controller: bool = False,
) -> dict[str, object]:
    artifacts = [
        {
            "id": artifact_id,
            "name": name,
            "expired": False,
            "digest": digest,
            "archive_download_url": (
                "https://api.github.com/repos/yanyuhanyue/AniMemo/"
                f"actions/artifacts/{artifact_id}/zip"
            ),
            "workflow_run": {"id": run_id, "head_sha": CANDIDATE},
        }
    ]
    if include_controller:
        controller_id = artifact_id + 1
        artifacts.append(
            {
                "id": controller_id,
                "name": f"controller-authority-{run_id}",
                "expired": False,
                "digest": "sha256:" + "f" * 64,
                "archive_download_url": (
                    "https://api.github.com/repos/yanyuhanyue/AniMemo/"
                    f"actions/artifacts/{controller_id}/zip"
                ),
                "workflow_run": {"id": run_id, "head_sha": CANDIDATE},
            }
        )
    return {"total_count": len(artifacts), "artifacts": artifacts}


def _qualification_jobs() -> dict[str, object]:
    return {
        "total_count": 4,
        "jobs": [
            {
                "name": "candidate-byte-producer",
                "status": "completed",
                "conclusion": "success",
                "run_id": QUALIFICATION_RUN_ID,
                "head_sha": CANDIDATE,
            },
            {
                "name": "qualification-finalizer",
                "status": "completed",
                "conclusion": "success",
                "run_id": QUALIFICATION_RUN_ID,
                "head_sha": CANDIDATE,
            },
            {
                "name": "controller-authority",
                "status": "completed",
                "conclusion": "success",
                "run_id": QUALIFICATION_RUN_ID,
                "head_sha": CANDIDATE,
            },
            {
                "name": "publish-immutable-prerelease",
                "status": "completed",
                "conclusion": "skipped",
                "run_id": QUALIFICATION_RUN_ID,
                "head_sha": CANDIDATE,
            },
        ],
    }


def _context() -> dict[str, object]:
    return {
        "candidate_sha": CANDIDATE,
        "comparison_base_sha": BASE,
        "previous_stable": "v1.0.0",
        "release_tag": "v1.1.0-rc.9",
        "target_version": "v1.1.0",
        "channel": "rc",
        "minimum_updater_version": "v1.0.0",
        "supported_os": ["Ubuntu 24.04 LTS"],
        "docker_requirement": "Docker Engine 28+",
        "release_assets": [
            "release-manifest.json",
            "deployment-contract.json",
            "installer-materials.tar",
            "checksums.txt",
        ],
    }


class FakeClock:
    def __init__(self, *, advance_sleep: bool = True) -> None:
        self.current = datetime(2026, 8, 23, 12, 0, tzinfo=timezone.utc)
        self.elapsed = 0.0
        self.advance_sleep = advance_sleep

    def now(self) -> datetime:
        return self.current

    def monotonic(self) -> float:
        return self.elapsed

    def sleep(self, seconds: float) -> None:
        if self.advance_sleep:
            self.current += timedelta(seconds=seconds)
            self.elapsed += seconds


class MetadataFreshnessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.repository = self.root / "repository"
        self.repository.mkdir()
        self.qualification = self.root / "qualification"
        self.qualification.mkdir()
        self.output = self.root / "freshness"
        notes_input = {"context": _context(), "pulls": [_normalized_pull()]}
        self.notes = build_release_notes(
            context=notes_input["context"], pulls=notes_input["pulls"]
        )
        markdown = render_release_notes(self.notes).encode()
        population_digest = sha256_bytes(
            release_notes_json_bytes(notes_input["pulls"])
        )
        events = [
            {
                "labels": pull["labels"],
                "number": pull["number"],
                "observed_updated_at": pull["observed_updated_at"],
            }
            for pull in notes_input["pulls"]
        ]
        readback = {
            "schema": "animemo.release-notes-readback/v1",
            "readback_count": 2,
            "population_digest": population_digest,
            "event_digest": sha256_bytes(release_notes_json_bytes(events)),
        }
        frozen_files = {
            "release-notes-input.json": release_notes_json_bytes(notes_input),
            "release-notes.json": release_notes_json_bytes(self.notes),
            "release-notes.md": markdown,
            "release-notes-readback.json": release_notes_json_bytes(readback),
        }
        preflight = build_preflight_manifest(
            binding={
                "repository": "yanyuhanyue/AniMemo",
                "run_id": QUALIFICATION_RUN_ID,
                "run_attempt": 1,
                "head_sha": CANDIDATE,
                "head_tree": TREE,
                "comparison_base_sha": BASE,
                "previous_stable": "v1.0.0",
                "release_tag": "v1.1.0-rc.9",
                "target_version": "v1.1.0",
                "channel": "rc",
            },
            files=frozen_files,
        )
        needs = {
            name: {"result": "success"} for name in REQUIRED_RESULT_JOB_IDS
        }
        provisional_digest = "sha256:" + "7" * 64
        evidence = build_qualification_evidence(
            workflow_ref=(
                "yanyuhanyue/AniMemo/.github/workflows/release.yml@refs/heads/main"
            ),
            workflow_sha=CANDIDATE,
            run_id=str(QUALIFICATION_RUN_ID),
            run_attempt=1,
            candidate_sha=CANDIDATE,
            candidate_tree=TREE,
            upgrade_base_sha=BASE,
            channel="rc",
            target_version="v1.1.0",
            release_tag="v1.1.0-rc.9",
            needs=needs,
            current_job_id="qualification-finalizer",
            candidate_production_receipt_sha256="sha256:" + "6" * 64,
            producer_job_observation={"id": "dry-run", "result": "success"},
            provisional_artifact={
                "id": 7001,
                "name": f"candidate-materials-{QUALIFICATION_RUN_ID}",
                "api_digest": provisional_digest,
                "archive_sha256": provisional_digest,
            },
            release_notes_identity=self.notes["identity"],
            release_notes_markdown_sha256=(
                "sha256:" + hashlib.sha256(markdown).hexdigest()
            ),
        )
        files = {
            f"release-qualification-{QUALIFICATION_RUN_ID}.json": _json_bytes(evidence),
            "platform-qualification.json": b"{}\n",
            **frozen_files,
            "release-notes-preflight.json": release_notes_json_bytes(preflight),
            "prepublication-materials.json": _json_bytes(
                {
                    "schemaVersion": 2,
                    "candidateSha": CANDIDATE,
                    "candidateTreeSha": TREE,
                }
            ),
            "installer-materials.tar": b"installer",
            "deployment-contract.json": b"{}\n",
        }
        for name, value in files.items():
            (self.qualification / name).write_bytes(value)
        original_vm_hashes = {"source.vmx": "sha256:" + "8" * 64}
        aggregate = {
            "schema": "animemo.prepublication-candidate-acceptance-receipt/v3",
            "version": 3,
            "candidate_input_digest": "sha256:" + "1" * 64,
            "verified_candidate_digest": "sha256:" + "2" * 64,
            "qualification_run_id": QUALIFICATION_RUN_ID,
            "qualification_run_attempt": 1,
            "source_sha": CANDIDATE,
            "source_tree": TREE,
            "candidate_version": "v1.1.0-rc.9",
            "r2_origin_prestate_receipt_digest": "sha256:" + "6" * 64,
            "r2_origin_poststate_receipt_digest": "sha256:" + "7" * 64,
            "r2_origin_prestate_observation_id": (
                "12345678-1234-4678-9234-567812345678"
            ),
            "r2_origin_poststate_observation_id": (
                "87654321-4321-4765-8abc-876543210fed"
            ),
            "base_vm_identity": sha256_bytes(
                canonical_json_bytes(original_vm_hashes)
            ),
            "source_vm_inventory_identity": "sha256:" + "9" * 64,
            "source_disk_graph_identity": "sha256:" + "a" * 64,
            "original_vm_hashes": original_vm_hashes,
            "snapshot_identities": {
                "FRESH_BASE": "sha256:" + "b" * 64,
                "DOCKER_BASE": "sha256:" + "c" * 64,
                "RUNTIME_BASE_OFFLINE": "sha256:" + "d" * 64,
            },
            "snapshot_disk_graph_identities": {
                "FRESH_BASE": "sha256:" + "e" * 64,
                "DOCKER_BASE": "sha256:" + "f" * 64,
                "RUNTIME_BASE_OFFLINE": "sha256:" + "0" * 64,
            },
            "profile_results": {
                "fresh_base": {
                    "status": "PASS", "failure_code": None,
                    "receipt_digest": "sha256:" + "3" * 64,
                },
                "docker_base": {
                    "status": "PASS", "failure_code": None,
                    "receipt_digest": "sha256:" + "4" * 64,
                },
                "runtime_base_offline": {
                    "status": "PASS", "failure_code": None,
                    "receipt_digest": "sha256:" + "5" * 64,
                },
            },
            "all_profiles_pass": True,
            "candidate_prestate": {
                "tag": "ABSENT", "github_release": "ABSENT", "ghcr": "ABSENT",
                "public_r2": "ABSENT_BY_PUBLIC_READBACK_NON_AUTHORITATIVE",
                "r2_origin": "PROVEN_EMPTY",
            },
            "candidate_poststate": {
                "tag": "ABSENT", "github_release": "ABSENT", "ghcr": "ABSENT",
                "public_r2": "ABSENT_BY_PUBLIC_READBACK_NON_AUTHORITATIVE",
                "r2_origin": "PROVEN_EMPTY",
            },
            "repository_mutation_count": 0,
            "publication_mutation_count": 0,
            "shared_host_connection_count": 0,
            "secret_sweep": 0,
            "placeholder_sweep": 0,
            "release_authority_granted": False,
            "publish_authorized": False,
            "completed_at": "2026-08-23T11:59:00Z",
            "result": "PASS",
            "receipt_digest": "",
        }
        unsigned = dict(aggregate)
        unsigned.pop("receipt_digest")
        aggregate["receipt_digest"] = sha256_bytes(canonical_json_bytes(unsigned))
        self.candidate_receipt = self.root / "candidate-acceptance-receipt.json"
        self.candidate_receipt.write_bytes(canonical_json_bytes(aggregate))
        self.candidate_receipt_sha256 = sha256_bytes(
            self.candidate_receipt.read_bytes()
        )
        self.identity = FreshnessRunIdentity(
            workflow_run_id=FRESHNESS_RUN_ID,
            workflow_attempt=1,
            workflow_path=WORKFLOW_PATH,
            workflow_sha=CANDIDATE,
            candidate_sha=CANDIDATE,
            candidate_tree=TREE,
            qualification_run_id=QUALIFICATION_RUN_ID,
            qualification_artifact_id=QUALIFICATION_ARTIFACT_ID,
            candidate_acceptance_receipt_sha256=self.candidate_receipt_sha256,
            candidate_version="v1.1.0-rc.9",
        )
        self.expectation = FreshnessExpectation(
            workflow_run_id=FRESHNESS_RUN_ID,
            candidate_sha=CANDIDATE,
            candidate_tree=TREE,
            qualification_run_id=QUALIFICATION_RUN_ID,
            qualification_artifact_id=QUALIFICATION_ARTIFACT_ID,
            candidate_acceptance_receipt_sha256=self.candidate_receipt_sha256,
            candidate_version="v1.1.0-rc.9",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _collect(self, clock: FakeClock | None = None) -> FakeClock:
        clock = clock or FakeClock()
        collect_metadata_freshness(
            repository_root=self.repository,
            qualification_directory=self.qualification,
            output_directory=self.output,
            identity=self.identity,
            candidate_acceptance_receipt=self.candidate_receipt,
            clock=clock,
        )
        return clock

    def _build_full_qualification_archive(
        self,
        *,
        wrong_receipt_binding: bool = False,
        mutate_closed_member: bool = False,
    ) -> tuple[Path, str]:
        """Build the real final-archive shape consumed before ten-file projection."""

        from release.test_candidate import candidate_input

        staging = self.root / (
            "full-qualification-"
            + str(int(wrong_receipt_binding))
            + "-"
            + str(int(mutate_closed_member))
        )
        staging.mkdir()
        evidence_name = f"release-qualification-{QUALIFICATION_RUN_ID}.json"
        for source in self.qualification.iterdir():
            if source.name != evidence_name:
                shutil.copyfile(source, staging / source.name)
        for name in (
            "checksums.txt",
            "release-manifest.json",
            "release-producer-toolchain-receipt.json",
        ):
            (staging / name).write_bytes((name + "\n").encode())

        candidate = candidate_input()
        candidate["qualification_run_id"] = QUALIFICATION_RUN_ID
        candidate["qualification_workflow_identity"]["sha"] = CANDIDATE
        candidate["source_sha"] = CANDIDATE
        candidate["source_tree"] = TREE
        candidate["candidate_version"] = "v1.1.0-rc.9"
        candidate["candidate_sequence"] = 9
        for item in candidate["candidate_runtime_file_inventory"]:
            target = staging / str(item["path"])
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"x")

        receipt_identity = {
            "repository": "yanyuhanyue/AniMemo",
            "workflow_ref": (
                "yanyuhanyue/AniMemo/.github/workflows/release.yml@refs/heads/main"
            ),
            "workflow_sha": CANDIDATE,
            "run_id": str(QUALIFICATION_RUN_ID),
            "run_attempt": 1,
            "event": "workflow_dispatch",
            "candidate_sha": CANDIDATE,
            "candidate_tree": TREE,
            "target_version": "v1.1.0",
            "release_tag": "v1.1.0-rc.9",
            "channel": "rc",
        }
        production_receipt = build_candidate_production_receipt(
            root=staging,
            identity=receipt_identity,
        )
        receipt_bytes = canonical_json_bytes(production_receipt)
        (staging / "candidate-production-receipt.json").write_bytes(receipt_bytes)

        needs = {
            name: {"result": "success"} for name in REQUIRED_RESULT_JOB_IDS
        }
        provisional_digest = "sha256:" + "7" * 64
        receipt_digest = sha256_bytes(receipt_bytes)
        if wrong_receipt_binding:
            receipt_digest = "sha256:" + "8" * 64
        qualification = build_qualification_evidence(
            workflow_ref=receipt_identity["workflow_ref"],
            workflow_sha=CANDIDATE,
            run_id=str(QUALIFICATION_RUN_ID),
            run_attempt=1,
            candidate_sha=CANDIDATE,
            candidate_tree=TREE,
            upgrade_base_sha=BASE,
            channel="rc",
            target_version="v1.1.0",
            release_tag="v1.1.0-rc.9",
            needs=needs,
            current_job_id="qualification-finalizer",
            candidate_production_receipt_sha256=receipt_digest,
            producer_job_observation={"id": "dry-run", "result": "success"},
            provisional_artifact={
                "id": 7001,
                "name": f"candidate-materials-{QUALIFICATION_RUN_ID}",
                "api_digest": provisional_digest,
                "archive_sha256": provisional_digest,
            },
            created_at="2026-08-23T12:00:00Z",
            release_notes_identity=self.notes["identity"],
            release_notes_markdown_sha256=sha256_bytes(
                (staging / "release-notes.md").read_bytes()
            ),
        )
        (staging / evidence_name).write_bytes(_json_bytes(qualification))
        (staging / "candidate-input.json").write_bytes(canonical_json_bytes(candidate))
        if mutate_closed_member:
            (staging / "checksums.txt").write_bytes(b"changed-after-close\n")

        archive_path = staging.with_suffix(".zip")
        with zipfile.ZipFile(archive_path, mode="w") as archive:
            for source in sorted(path for path in staging.rglob("*") if path.is_file()):
                archive.write(source, arcname=source.relative_to(staging).as_posix())
        return archive_path, sha256_bytes(archive_path.read_bytes())

    def _project_freshness_inputs(self, extracted: Path, destination: Path) -> None:
        destination.mkdir()
        for name in (
            f"release-qualification-{QUALIFICATION_RUN_ID}.json",
            "platform-qualification.json",
            "release-notes-input.json",
            "release-notes.json",
            "release-notes.md",
            "release-notes-readback.json",
            "release-notes-preflight.json",
            "prepublication-materials.json",
            "installer-materials.tar",
            "deployment-contract.json",
        ):
            shutil.copyfile(extracted / name, destination / name)

    def test_module_has_no_live_pull_request_query_surface(self) -> None:
        source = (Path(__file__).with_name("metadata_freshness.py")).read_text(
            encoding="utf-8"
        )

        for forbidden in (
            "GitHubAssociatedPullSource",
            "AssociatedPullSource",
            "ENDPOINT_TEMPLATE",
            "commits/{sha}/pulls",
            "_git_commit_range",
            "_complete_snapshot",
            "_collect_with_retry",
            "MAX_COMPLETE_ATTEMPTS",
        ):
            self.assertNotIn(forbidden, source)

    def test_collector_reads_only_frozen_qualification_bytes_without_sleep(self) -> None:
        clock = FakeClock(advance_sleep=False)

        collect_metadata_freshness(
            repository_root=self.repository,
            qualification_directory=self.qualification,
            output_directory=self.output,
            identity=self.identity,
            candidate_acceptance_receipt=self.candidate_receipt,
            clock=clock,
        )

        self.assertEqual(clock.elapsed, 0.0)
        for source_name, frozen_name in (
            ("release-notes-input.json", "snapshot-a-input.json"),
            ("release-notes.json", "snapshot-a.json"),
            ("release-notes.md", "snapshot-a.md"),
            ("release-notes-input.json", "snapshot-b-input.json"),
            ("release-notes.json", "snapshot-b.json"),
            ("release-notes.md", "snapshot-b.md"),
        ):
            self.assertEqual(
                (self.qualification / source_name).read_bytes(),
                (self.output / frozen_name).read_bytes(),
            )
        self.assertEqual(
            json.loads((self.output / "request-diagnostics.json").read_bytes()),
            {
                "schemaVersion": 1,
                "source": "QUALIFICATION_FROZEN_PREFLIGHT",
                "requests": [],
            },
        )

    def test_full_final_archive_is_verified_before_ten_file_projection(self) -> None:
        archive, digest = self._build_full_qualification_archive()
        extracted = self.root / "full-extracted"
        result = extract_qualification_artifact(
            archive,
            extracted,
            qualification_run_id=QUALIFICATION_RUN_ID,
            expected_sha256=digest,
            require_candidate_contract=True,
        )
        self.assertEqual(result["status"], "PASS")
        self.assertIn("candidate-production-receipt.json", {path.name for path in extracted.iterdir()})

        projection = self.root / "freshness-projection"
        self._project_freshness_inputs(extracted, projection)
        self.assertEqual(len(list(projection.iterdir())), 10)
        collect_metadata_freshness(
            repository_root=self.repository,
            qualification_directory=projection,
            output_directory=self.output,
            identity=self.identity,
            candidate_acceptance_receipt=self.candidate_receipt,
            clock=FakeClock(),
        )
        self.assertEqual({path.name for path in self.output.iterdir()}, ARTIFACT_FILES)

    def test_full_archive_rejects_receipt_digest_and_closed_inventory_tampering(self) -> None:
        for label, options in (
            ("receipt binding", {"wrong_receipt_binding": True}),
            ("closed inventory", {"mutate_closed_member": True}),
        ):
            archive, digest = self._build_full_qualification_archive(**options)
            with self.subTest(label=label), self.assertRaises(MaterialContractError):
                extract_qualification_artifact(
                    archive,
                    self.root / ("rejected-" + label.replace(" ", "-")),
                    qualification_run_id=QUALIFICATION_RUN_ID,
                    expected_sha256=digest,
                    require_candidate_contract=True,
                )

    def test_projection_rejects_legacy_schema_and_embedded_future_run_state(self) -> None:
        evidence_path = self.qualification / (
            f"release-qualification-{QUALIFICATION_RUN_ID}.json"
        )
        original = json.loads(evidence_path.read_bytes())
        cases = []
        legacy = copy.deepcopy(original)
        legacy["schema"] = "animemo.release-qualification/v2"
        cases.append(legacy)
        future_state = copy.deepcopy(original)
        future_state["run"]["status"] = "completed"
        future_state["run"]["conclusion"] = "success"
        cases.append(future_state)
        for evidence in cases:
            with self.subTest(schema=evidence["schema"]):
                evidence_path.write_bytes(_json_bytes(evidence))
                with self.assertRaisesRegex(
                    MetadataFreshnessError, "qualification evidence binding differs"
                ):
                    self._collect()
        evidence_path.write_bytes(_json_bytes(original))

    def test_verifier_binds_the_single_frozen_preflight_authority(self) -> None:
        clock = FakeClock(advance_sleep=False)
        collect_metadata_freshness(
            repository_root=self.repository,
            qualification_directory=self.qualification,
            output_directory=self.output,
            identity=self.identity,
            candidate_acceptance_receipt=self.candidate_receipt,
            clock=clock,
        )

        manifest = json.loads(
            (self.qualification / "release-notes-preflight.json").read_bytes()
        )
        result = verify_metadata_freshness_artifact(
            artifact_directory=self.output,
            qualification_directory=self.qualification,
            expectation=self.expectation,
            verified_at=clock.now() + timedelta(minutes=1),
        )

        self.assertEqual(result["preflightIdentity"], manifest["identity"])
        self.assertEqual(
            result["populationDigest"], manifest["population"]["digest"]
        )
        self.assertEqual(result["eventDigest"], manifest["population"]["event_digest"])
        self.assertEqual(result["releaseNotesAuthorityProducerCount"], 1)
        self.assertEqual(result["livePrLabelQueryCount"], 0)

    def test_frozen_preflight_run_head_tree_and_boundary_are_fail_closed(self) -> None:
        original = json.loads(
            (self.qualification / "release-notes-preflight.json").read_bytes()
        )
        for field, replacement in (
            ("run_id", QUALIFICATION_RUN_ID + 1),
            ("head_sha", "1" * 40),
            ("head_tree", "2" * 40),
            ("comparison_base_sha", "3" * 40),
        ):
            with self.subTest(field=field):
                manifest = copy.deepcopy(original)
                manifest["binding"][field] = replacement
                unsigned = copy.deepcopy(manifest)
                unsigned.pop("identity")
                manifest["identity"] = sha256_bytes(
                    release_notes_json_bytes(unsigned)
                )
                (self.qualification / "release-notes-preflight.json").write_bytes(
                    release_notes_json_bytes(manifest)
                )
                with self.assertRaisesRegex(
                    MetadataFreshnessError,
                    "preflight differs",
                ):
                    collect_metadata_freshness(
                        repository_root=self.repository,
                        qualification_directory=self.qualification,
                        output_directory=self.output,
                        identity=self.identity,
                        candidate_acceptance_receipt=self.candidate_receipt,
                        clock=FakeClock(),
                    )

    def test_two_frozen_byte_readbacks_pass_and_verify(self) -> None:
        clock = self._collect()
        self.assertEqual({path.name for path in self.output.iterdir()}, ARTIFACT_FILES)
        result = verify_metadata_freshness_artifact(
            artifact_directory=self.output,
            qualification_directory=self.qualification,
            expectation=self.expectation,
            verified_at=clock.now() + timedelta(minutes=1),
        )
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["snapshotIntervalSeconds"], 0)

    def test_valid_overall_fail_aggregate_cannot_start_freshness(self) -> None:
        receipt = json.loads(self.candidate_receipt.read_bytes())
        receipt["profile_results"]["fresh_base"] = {
            "status": "FAIL",
            "failure_code": "CANDIDATE_PROFILE_REPORTED_FAILURE",
            "receipt_digest": "sha256:" + "8" * 64,
        }
        receipt["all_profiles_pass"] = False
        receipt["result"] = "FAIL"
        unsigned = dict(receipt)
        unsigned.pop("receipt_digest")
        receipt["receipt_digest"] = sha256_bytes(canonical_json_bytes(unsigned))
        self.candidate_receipt.write_bytes(canonical_json_bytes(receipt))
        digest = sha256_bytes(self.candidate_receipt.read_bytes())
        self.identity = replace(
            self.identity,
            candidate_acceptance_receipt_sha256=digest,
        )

        with self.assertRaisesRegex(
            MetadataFreshnessError,
            "authority binding differs",
        ):
            self._collect()

    def test_historical_qualification_passes_old_main_and_not_a_new_main(self) -> None:
        clock = self._collect()
        self.assertEqual(
            verify_metadata_freshness_artifact(
                artifact_directory=self.output,
                qualification_directory=self.qualification,
                expectation=self.expectation,
                verified_at=clock.now(),
            )["candidateSha"],
            "e65f9beb0b5a19be2b4562206b38bb6d00adff7e",
        )
        with self.assertRaises(MetadataFreshnessError):
            verify_metadata_freshness_artifact(
                artifact_directory=self.output,
                qualification_directory=self.qualification,
                expectation=FreshnessExpectation(
                    workflow_run_id=FRESHNESS_RUN_ID,
                    candidate_sha="2" * 40,
                    candidate_tree="3" * 40,
                    qualification_run_id=QUALIFICATION_RUN_ID,
                    qualification_artifact_id=QUALIFICATION_ARTIFACT_ID,
                    candidate_acceptance_receipt_sha256=self.candidate_receipt_sha256,
                    candidate_version="v1.1.0-rc.9",
                ),
                verified_at=clock.now(),
            )

    def test_closed_receipt_rejects_unknown_conflict_unclassified_and_duplicate(self) -> None:
        self._collect()
        receipt = json.loads((self.output / "metadata-freshness.json").read_text())
        cases = []
        unknown = copy.deepcopy(receipt)
        unknown["override"] = True
        cases.append(unknown)
        for field in ("conflicts", "unclassified", "duplicates"):
            changed = copy.deepcopy(receipt)
            changed[field] = 1
            changed["receiptDigest"] = _receipt_digest(changed)
            cases.append(changed)
        for value in cases:
            with self.subTest(keys=set(value)), self.assertRaises(
                MetadataFreshnessError
            ):
                _validate_receipt(value)

    def test_duplicate_json_key_and_markdown_tamper_fail(self) -> None:
        self._collect()
        receipt_path = self.output / "metadata-freshness.json"
        original_receipt = receipt_path.read_bytes()
        receipt_path.write_text('{"schemaVersion":1,"schemaVersion":1}\n')
        with self.assertRaisesRegex(MetadataFreshnessError, "strict JSON"):
            verify_metadata_freshness_artifact(
                artifact_directory=self.output,
                qualification_directory=self.qualification,
                expectation=self.expectation,
            )
        receipt_path.write_bytes(original_receipt)
        (self.output / "snapshot-b.md").write_text("# tampered\n")
        with self.assertRaisesRegex(MetadataFreshnessError, "Markdown differs"):
            verify_metadata_freshness_artifact(
                artifact_directory=self.output,
                qualification_directory=self.qualification,
                expectation=self.expectation,
            )

    def test_stale_freshness_fails_with_exact_code(self) -> None:
        clock = self._collect()
        with self.assertRaises(MetadataFreshnessError) as raised:
            verify_metadata_freshness_artifact(
                artifact_directory=self.output,
                qualification_directory=self.qualification,
                expectation=self.expectation,
                verified_at=clock.now() + timedelta(minutes=16),
            )
        self.assertEqual(raised.exception.code, "METADATA_FRESHNESS_EXPIRED")

    def test_wrong_run_head_or_qualification_binding_fails(self) -> None:
        self._collect()
        expectations = (
            FreshnessExpectation(
                FRESHNESS_RUN_ID, "2" * 40, TREE, QUALIFICATION_RUN_ID,
                QUALIFICATION_ARTIFACT_ID, self.candidate_receipt_sha256,
                "v1.1.0-rc.9"
            ),
            FreshnessExpectation(
                FRESHNESS_RUN_ID, CANDIDATE, TREE, 999,
                QUALIFICATION_ARTIFACT_ID, self.candidate_receipt_sha256,
                "v1.1.0-rc.9"
            ),
        )
        for expectation in expectations:
            with self.subTest(expectation=expectation), self.assertRaises(
                MetadataFreshnessError
            ):
                verify_metadata_freshness_artifact(
                    artifact_directory=self.output,
                    qualification_directory=self.qualification,
                    expectation=expectation,
                )

    def test_full_trusted_transaction_unlocks_publish_only_after_verification(
        self,
    ) -> None:
        mutation_unlocked = False
        qualification_archive = self.root / "qualification.zip"
        with zipfile.ZipFile(qualification_archive, "w") as archive:
            for path in sorted(self.qualification.iterdir()):
                archive.write(path, arcname=path.name)
        qualification_digest = (
            "sha256:" + hashlib.sha256(qualification_archive.read_bytes()).hexdigest()
        )
        qualification_artifact = validate_qualification_run_metadata(
            run_metadata=_run_metadata(
                run_id=QUALIFICATION_RUN_ID,
                name="Release Producer",
                path=".github/workflows/release.yml",
            ),
            jobs_metadata=_qualification_jobs(),
            artifacts_metadata=_artifact_listing(
                run_id=QUALIFICATION_RUN_ID,
                artifact_id=QUALIFICATION_ARTIFACT_ID,
                name=f"release-qualification-{QUALIFICATION_RUN_ID}",
                digest=qualification_digest,
                include_controller=True,
            ),
            expected_run_id=QUALIFICATION_RUN_ID,
            expected_sha=CANDIDATE,
        )
        self.assertFalse(mutation_unlocked)
        qualification_consumer = self.root / "qualification-consumer"
        shutil.copytree(self.qualification, qualification_consumer)

        clock = FakeClock()
        collect_metadata_freshness(
            repository_root=self.repository,
            qualification_directory=qualification_consumer,
            output_directory=self.output,
            identity=self.identity,
            candidate_acceptance_receipt=self.candidate_receipt,
            clock=clock,
        )
        self.assertFalse(mutation_unlocked)
        freshness_archive = self.root / "freshness.zip"
        with zipfile.ZipFile(freshness_archive, "w") as archive:
            for path in sorted(self.output.iterdir()):
                archive.write(path, arcname=path.name)
        freshness_digest = (
            "sha256:" + hashlib.sha256(freshness_archive.read_bytes()).hexdigest()
        )
        freshness_artifact = validate_freshness_run_metadata(
            run_metadata=_run_metadata(
                run_id=FRESHNESS_RUN_ID,
                name="Release Metadata Freshness",
                path=".github/workflows/release-metadata-freshness.yml",
            ),
            artifacts_metadata=_artifact_listing(
                run_id=FRESHNESS_RUN_ID,
                artifact_id=900002,
                name=f"release-metadata-freshness-{FRESHNESS_RUN_ID}",
                digest=freshness_digest,
            ),
            expected_run_id=FRESHNESS_RUN_ID,
            expected_sha=CANDIDATE,
        )
        freshness_consumer = self.root / "freshness-consumer"
        extract_metadata_freshness_artifact(
            freshness_archive,
            freshness_consumer,
            expected_sha256=str(freshness_artifact["digest"]),
        )
        verification = verify_metadata_freshness_artifact(
            artifact_directory=freshness_consumer,
            qualification_directory=qualification_consumer,
            expectation=self.expectation,
            verified_at=clock.now() + timedelta(minutes=1),
        )
        mutation_unlocked = verification["status"] == "PASS"
        self.assertTrue(mutation_unlocked)
        self.assertEqual(qualification_artifact["artifactId"], 9492671353)
        self.assertEqual(verification["qualificationRunId"], 32635898412)

    def test_run_and_artifact_authority_metadata_fail_closed(self) -> None:
        qualification_run = _run_metadata(
            run_id=QUALIFICATION_RUN_ID,
            name="Release Producer",
            path=".github/workflows/release.yml",
        )
        qualification_artifacts = _artifact_listing(
            run_id=QUALIFICATION_RUN_ID,
            artifact_id=QUALIFICATION_ARTIFACT_ID,
            name=f"release-qualification-{QUALIFICATION_RUN_ID}",
            digest="sha256:" + "1" * 64,
            include_controller=True,
        )
        cases: list[tuple[object, object, object]] = []
        wrong_workflow = copy.deepcopy(qualification_run)
        wrong_workflow["name"] = "Untrusted Producer"
        cases.append((wrong_workflow, _qualification_jobs(), qualification_artifacts))
        failed_run = copy.deepcopy(qualification_run)
        failed_run["conclusion"] = "failure"
        cases.append((failed_run, _qualification_jobs(), qualification_artifacts))
        in_progress_run = copy.deepcopy(qualification_run)
        in_progress_run["status"] = "in_progress"
        in_progress_run["conclusion"] = None
        cases.append((in_progress_run, _qualification_jobs(), qualification_artifacts))
        for index in range(4):
            failed_jobs = _qualification_jobs()
            failed_jobs["jobs"][index]["conclusion"] = "failure"
            cases.append((qualification_run, failed_jobs, qualification_artifacts))
            in_progress_jobs = _qualification_jobs()
            in_progress_jobs["jobs"][index]["status"] = "in_progress"
            in_progress_jobs["jobs"][index]["conclusion"] = None
            cases.append((qualification_run, in_progress_jobs, qualification_artifacts))
        duplicate_finalizer = _qualification_jobs()
        duplicate_finalizer["jobs"].append(copy.deepcopy(duplicate_finalizer["jobs"][1]))
        duplicate_finalizer["total_count"] += 1
        cases.append((qualification_run, duplicate_finalizer, qualification_artifacts))
        wrong_head = copy.deepcopy(qualification_run)
        wrong_head["head_sha"] = "2" * 40
        cases.append((wrong_head, _qualification_jobs(), qualification_artifacts))
        expired = copy.deepcopy(qualification_artifacts)
        expired["artifacts"][0]["expired"] = True
        cases.append((qualification_run, _qualification_jobs(), expired))
        malformed_digest = copy.deepcopy(qualification_artifacts)
        malformed_digest["artifacts"][0]["digest"] = "sha256:not-a-digest"
        cases.append((qualification_run, _qualification_jobs(), malformed_digest))
        wrong_artifact_head = copy.deepcopy(qualification_artifacts)
        wrong_artifact_head["artifacts"][0]["workflow_run"]["head_sha"] = "4" * 40
        cases.append((qualification_run, _qualification_jobs(), wrong_artifact_head))
        duplicate = copy.deepcopy(qualification_artifacts)
        duplicate["artifacts"].append(copy.deepcopy(duplicate["artifacts"][0]))
        cases.append((qualification_run, _qualification_jobs(), duplicate))
        missing_controller = copy.deepcopy(qualification_artifacts)
        missing_controller["artifacts"] = [
            artifact
            for artifact in missing_controller["artifacts"]
            if artifact["name"] != f"controller-authority-{QUALIFICATION_RUN_ID}"
        ]
        missing_controller["total_count"] = len(missing_controller["artifacts"])
        cases.append((qualification_run, _qualification_jobs(), missing_controller))
        for run, jobs, artifacts in cases:
            with self.subTest(run=run, jobs=jobs, artifacts=artifacts), self.assertRaises(
                MetadataFreshnessError
            ):
                validate_qualification_run_metadata(
                    run_metadata=run,
                    jobs_metadata=jobs,
                    artifacts_metadata=artifacts,
                    expected_run_id=QUALIFICATION_RUN_ID,
                    expected_sha=CANDIDATE,
                )
        freshness_artifacts = _artifact_listing(
            run_id=FRESHNESS_RUN_ID,
            artifact_id=900002,
            name=f"release-metadata-freshness-{FRESHNESS_RUN_ID}",
            digest="sha256:" + "2" * 64,
        )
        for name, head_sha in (
            ("Untrusted Freshness", CANDIDATE),
            ("Release Metadata Freshness", "3" * 40),
        ):
            with self.subTest(name=name, head_sha=head_sha), self.assertRaises(
                MetadataFreshnessError
            ):
                validate_freshness_run_metadata(
                    run_metadata=_run_metadata(
                        run_id=FRESHNESS_RUN_ID,
                        name=name,
                        path=".github/workflows/release-metadata-freshness.yml",
                        head_sha=head_sha,
                    ),
                    artifacts_metadata=freshness_artifacts,
                    expected_run_id=FRESHNESS_RUN_ID,
                    expected_sha=CANDIDATE,
                )

    def test_exact_zip_extract_and_extra_or_missing_file_rejection(self) -> None:
        self._collect()
        archive = self.root / "freshness.zip"
        with zipfile.ZipFile(archive, "w") as output:
            for path in self.output.iterdir():
                output.write(path, arcname=path.name)
        digest = "sha256:" + hashlib.sha256(archive.read_bytes()).hexdigest()
        extracted = self.root / "extracted"
        self.assertEqual(
            extract_metadata_freshness_artifact(
                archive, extracted, expected_sha256=digest
            )["fileCount"],
            10,
        )
        for excluded in ("snapshot-a.md", None):
            with self.subTest(excluded=excluded):
                broken = self.root / f"broken-{excluded or 'extra'}.zip"
                with zipfile.ZipFile(broken, "w") as output:
                    for path in self.output.iterdir():
                        if path.name != excluded:
                            output.write(path, arcname=path.name)
                    if excluded is None:
                        output.writestr("extra.txt", "no")
                broken_digest = "sha256:" + hashlib.sha256(broken.read_bytes()).hexdigest()
                with self.assertRaises(MetadataFreshnessError):
                    extract_metadata_freshness_artifact(
                        broken,
                        self.root / f"extract-{excluded or 'extra'}",
                        expected_sha256=broken_digest,
                    )


if __name__ == "__main__":
    unittest.main()
