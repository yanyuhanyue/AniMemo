from __future__ import annotations

import hashlib
import importlib
import inspect
import json
import stat
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

import yaml

ROOT = Path(__file__).resolve().parents[1]
SHA = "a" * 40
TREE = "b" * 40
RUN_ID = 1234


def _digest(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _canonical(value: object) -> bytes:
    return (
        json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def _request(output: Path) -> dict[str, object]:
    required = (
        "preflight",
        "full-ci",
        "full-release-gate",
        "performance",
        "platform-qualification",
        "release-authority",
        "dry-run",
    )
    return {
        "repository": "yanyuhanyue/AniMemo",
        "workflow": {
            "name": "Release Producer",
            "path": ".github/workflows/release.yml",
            "ref": "yanyuhanyue/AniMemo/.github/workflows/release.yml@refs/heads/main",
            "sha": SHA,
        },
        "run": {"id": str(RUN_ID), "attempt": 1, "event": "workflow_dispatch"},
        "current_job_id": "qualification-finalizer",
        "required_result_jobs": list(required),
        "needs": {name: {"result": "success"} for name in required},
        "candidate_sha": SHA,
        "candidate_tree": TREE,
        "upgrade_base_sha": "c" * 40,
        "channel": "rc",
        "target_version": "v1.1.0",
        "release_tag": "v1.1.0-rc.19",
        "created_at": "2026-09-01T10:00:00Z",
        "platform_artifact": {
            "id": 10,
            "api_digest": "sha256:" + "d" * 64,
        },
        "output_directory": str(output),
    }


def _metadata(archive: Path, *, name: str | None = None) -> dict[str, object]:
    encoded = archive.read_bytes()
    return {
        "id": 11,
        "name": name or f"candidate-materials-{RUN_ID}",
        "expired": False,
        "size_in_bytes": len(encoded),
        "digest": _digest(encoded),
        "workflow_run": {"id": RUN_ID, "head_sha": SHA},
    }


def _final_metadata(archive: Path) -> dict[str, object]:
    value = _metadata(archive, name=f"release-qualification-{RUN_ID}")
    value["id"] = 99
    return value


def _observed(archive: Path, *, artifacts: list[dict[str, object]] | None = None) -> dict[str, object]:
    selected = _metadata(archive)
    listing = artifacts if artifacts is not None else [selected]
    return {
        "total_count": len(listing),
        "artifacts": listing,
        "archive_path": str(archive),
    }


def _bound_request(output: Path, archive: Path) -> dict[str, object]:
    request = _request(output)
    artifact = _metadata(archive)
    request["provisional_artifact"] = {
        "id": artifact["id"],
        "name": artifact["name"],
        "api_digest": artifact["digest"],
    }
    return request


def _zip(path: Path, entries: list[tuple[str, bytes, int | None]]) -> Path:
    with zipfile.ZipFile(path, mode="w") as archive:
        for name, encoded, mode in entries:
            info = zipfile.ZipInfo(name)
            if mode is not None:
                info.external_attr = mode << 16
            archive.writestr(info, encoded)
    return path


class QualificationFinalizerRedContractTests(unittest.TestCase):
    def _api(self):
        return importlib.import_module("release.qualification_finalization")

    def test_public_surface_has_only_the_two_preagreed_deep_entries(self):
        module = self._api()
        self.assertEqual(
            module.__all__,
            ["finalize_qualification", "verify_uploaded_qualification"],
        )
        self.assertEqual(
            list(inspect.signature(module.finalize_qualification).parameters),
            ["request", "observed_archive"],
        )
        self.assertEqual(
            list(inspect.signature(module.verify_uploaded_qualification).parameters),
            ["request", "observed_archive"],
        )

    def test_exact_production_finalizer_direct_needs_include_completed_producer(self):
        workflow = yaml.safe_load(
            (ROOT / ".github" / "workflows" / "release.yml").read_text(
                encoding="utf-8"
            )
        )
        finalizer = workflow["jobs"]["qualification-finalizer"]
        direct_needs = set(finalizer["needs"])
        self.assertEqual(
            direct_needs,
            {
                "preflight",
                "full-ci",
                "full-release-gate",
                "performance",
                "platform-qualification",
                "release-authority",
                "dry-run",
            },
        )
        self.assertNotIn("qualification-finalizer", direct_needs)

    def test_missing_upstream_result_fails_closed(self):
        module = self._api()
        with tempfile.TemporaryDirectory() as temporary:
            request = _request(Path(temporary) / "out")
            request["needs"].pop("dry-run")
            with self.assertRaisesRegex(ValueError, "IncompleteUpstreamResult"):
                module.finalize_qualification(request, {})

    def test_current_finalizer_result_reference_fails_closed(self):
        module = self._api()
        with tempfile.TemporaryDirectory() as temporary:
            request = _request(Path(temporary) / "out")
            request["required_result_jobs"].append("qualification-finalizer")
            request["needs"]["qualification-finalizer"] = {"result": "success"}
            with self.assertRaisesRegex(ValueError, "SelfResultReference"):
                module.finalize_qualification(request, {})

    def test_workflow_and_run_request_identity_are_closed_objects(self):
        module = self._api()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for parent in ("workflow", "run"):
                request = _request(root / f"out-{parent}")
                request[parent]["untrusted_extension"] = "ignored-before-repair"
                with self.subTest(parent=parent), self.assertRaisesRegex(
                    ValueError, "ReceiptIdentityMismatch"
                ):
                    module.finalize_qualification(request, {})

    def test_provisional_artifact_cardinality_is_exact(self):
        module = self._api()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = _zip(root / "provisional.zip", [("payload.bin", b"x", None)])
            request = _bound_request(root / "out", archive)
            for artifacts in ([], [_metadata(archive), _metadata(archive)]):
                with self.subTest(count=len(artifacts)), self.assertRaisesRegex(
                    ValueError, "ArtifactCardinalityError"
                ):
                    module.finalize_qualification(
                        request, _observed(archive, artifacts=artifacts)
                    )

    def test_artifact_listing_must_be_complete_and_bounded(self):
        module = self._api()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = _zip(root / "provisional.zip", [("payload.bin", b"x", None)])
            request = _bound_request(root / "out", archive)
            artifact = _metadata(archive)
            for total_count in (None, 2, 101):
                observed = {
                    "total_count": total_count,
                    "artifacts": [artifact],
                    "archive_path": str(archive),
                }
                with self.subTest(total_count=total_count), self.assertRaisesRegex(
                    ValueError, "ArtifactCardinalityError"
                ):
                    module.finalize_qualification(request, observed)

    def test_provisional_metadata_is_exactly_bound(self):
        module = self._api()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = _zip(root / "provisional.zip", [("payload.bin", b"x", None)])
            request = _bound_request(root / "out", archive)
            wrong = _metadata(archive, name="candidate-materials-wrong")
            with self.assertRaisesRegex(ValueError, "ArtifactIdentityMismatch"):
                module.finalize_qualification(
                    request, _observed(archive, artifacts=[wrong])
                )

    def test_provisional_archive_digest_must_match_api(self):
        module = self._api()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = _zip(root / "provisional.zip", [("payload.bin", b"x", None)])
            request = _bound_request(root / "out", archive)
            expected = _metadata(archive)
            archive.write_bytes(archive.read_bytes() + b"tampered")
            with self.assertRaisesRegex(ValueError, "ArchiveDigestMismatch"):
                module.finalize_qualification(
                    request,
                    {
                        "total_count": 1,
                        "artifacts": [expected],
                        "archive_path": str(archive),
                    },
                )

    def test_archive_rejects_unsafe_duplicate_and_case_collision_members(self):
        module = self._api()
        cases = (
            [("../escape", b"x", None)],
            [("/absolute", b"x", None)],
            [("link", b"x", stat.S_IFLNK | 0o777)],
            [("same", b"x", None), ("same", b"y", None)],
            [("Name", b"x", None), ("name", b"y", None)],
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for index, entries in enumerate(cases):
                archive = _zip(root / f"attack-{index}.zip", list(entries))
                code = (
                    "DuplicateArchiveMember"
                    if len(entries) > 1
                    else "UnsafeArchiveMember"
                )
                with self.subTest(index=index), self.assertRaisesRegex(ValueError, code):
                    module.finalize_qualification(
                        _bound_request(root / f"out-{index}", archive),
                        _observed(archive),
                    )

    def test_receipt_member_inventory_mismatch_fails_closed(self):
        module = self._api()
        from release.materials import build_candidate_production_receipt

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            payload_root = root / "payload"
            payload_root.mkdir()
            (payload_root / "payload.bin").write_bytes(b"before")
            identity = {
                "repository": "yanyuhanyue/AniMemo",
                "workflow_ref": "yanyuhanyue/AniMemo/.github/workflows/release.yml@refs/heads/main",
                "workflow_sha": SHA,
                "run_id": str(RUN_ID),
                "run_attempt": 1,
                "event": "workflow_dispatch",
                "candidate_sha": SHA,
                "candidate_tree": TREE,
                "target_version": "v1.1.0",
                "release_tag": "v1.1.0-rc.19",
                "channel": "rc",
            }
            receipt = build_candidate_production_receipt(
                root=payload_root, identity=identity
            )
            receipt_bytes = _canonical(receipt)
            archive = _zip(
                root / "receipt-mismatch.zip",
                [
                    ("candidate-production-receipt.json", receipt_bytes, None),
                    ("payload.bin", b"after", None),
                ],
            )
            request = _bound_request(root / "out", archive)
            request["candidate_production_receipt_sha256"] = _digest(receipt_bytes)
            with self.assertRaisesRegex(ValueError, "ByteSetMismatch"):
                module.finalize_qualification(request, _observed(archive))

    def test_finalizer_copies_every_receipted_member_without_rebuild(self):
        module = self._api()
        from release.materials import build_candidate_production_receipt

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            provisional = root / "provisional"
            provisional.mkdir()
            note_identity = "sha256:" + "d" * 64
            original = {
                "release-notes.json": _canonical({"identity": note_identity}),
                "release-notes.md": b"# frozen notes\n",
                "nested/candidate.bin": b"candidate-byte-set\x00\xff",
            }
            for name, encoded in original.items():
                target = provisional / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(encoded)
            request = _request(root / "final")
            identity = {
                "repository": request["repository"],
                "workflow_ref": request["workflow"]["ref"],
                "workflow_sha": request["workflow"]["sha"],
                "run_id": request["run"]["id"],
                "run_attempt": request["run"]["attempt"],
                "event": request["run"]["event"],
                "candidate_sha": request["candidate_sha"],
                "candidate_tree": request["candidate_tree"],
                "target_version": request["target_version"],
                "release_tag": request["release_tag"],
                "channel": request["channel"],
            }
            receipt = build_candidate_production_receipt(
                root=provisional, identity=identity
            )
            receipt_bytes = _canonical(receipt)
            original["candidate-production-receipt.json"] = receipt_bytes
            archive = _zip(
                root / "candidate-materials.zip",
                [(name, encoded, None) for name, encoded in original.items()],
            )
            request = _bound_request(root / "final", archive)
            request["candidate_production_receipt_sha256"] = _digest(receipt_bytes)

            def write_candidate(**arguments):
                value = {"schema": "test-candidate-input"}
                arguments["output"].write_bytes(_canonical(value))
                return value

            def write_qualification(path, payload):
                path.write_bytes(_canonical(payload))

            evidence = {
                "schema": "animemo.release-qualification/v3",
                "qualification_sha256": "sha256:" + "e" * 64,
            }
            with (
                mock.patch.object(
                    module, "build_candidate_input", side_effect=write_candidate
                ),
                mock.patch.object(
                    module, "build_qualification_evidence", return_value=evidence
                ),
                mock.patch.object(
                    module, "write_qualification_evidence", side_effect=write_qualification
                ),
                mock.patch.object(
                    module, "validate_qualification_evidence", return_value=evidence
                ),
            ):
                result = module.finalize_qualification(
                    request, _observed(archive)
                )

            self.assertEqual(result["candidateBytesRebuildCount"], 0)
            for name, encoded in original.items():
                self.assertEqual((root / "final" / name).read_bytes(), encoded)

    def test_finalizer_source_has_no_candidate_rebuild_surface(self):
        module = self._api()
        source = inspect.getsource(module)
        for marker in (
            "docker build",
            "docker/build-push-action",
            "build_installer_materials",
            "generate_manifest",
            "normalize_candidate_oci_layout",
        ):
            self.assertNotIn(marker, source)

    def test_controller_binds_only_the_uploaded_final_artifact_readback(self):
        module = self._api()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            receipt_bytes = _canonical({"schema": "receipt-fixture"})
            qualification = {
                "schema": "animemo.release-qualification/v3",
                "candidate_production_receipt_sha256": _digest(receipt_bytes),
            }
            archive = _zip(
                root / "release-qualification.zip",
                [
                    ("candidate-production-receipt.json", receipt_bytes, None),
                    (
                        f"release-qualification-{RUN_ID}.json",
                        _canonical(qualification),
                        None,
                    ),
                ],
            )
            artifact = _final_metadata(archive)
            request = _request(root / "controller")
            request.update(
                {
                    "current_job_id": "controller-authority",
                    "required_result_jobs": ["qualification-finalizer"],
                    "needs": {
                        "qualification-finalizer": {"result": "success"}
                    },
                    "final_artifact": {
                        "id": artifact["id"],
                        "name": artifact["name"],
                        "api_digest": artifact["digest"],
                    },
                }
            )

            def build_controller(**arguments):
                output = arguments["output"]
                output.mkdir()
                (output / "candidate-input.json").write_bytes(b"candidate\n")
                (output / "verified-candidate.json").write_bytes(b"verified\n")
                return {"status": "PASS"}

            with (
                mock.patch.object(
                    module,
                    "build_prepublication_controller_authority",
                    side_effect=build_controller,
                ),
                mock.patch.object(
                    module,
                    "validate_qualification_evidence",
                    return_value=qualification,
                ),
            ):
                result = module.verify_uploaded_qualification(
                    request,
                    {
                        "total_count": 1,
                        "artifacts": [artifact],
                        "archive_path": str(archive),
                    },
                )

            self.assertEqual(result["finalArtifactId"], artifact["id"])
            authority = json.loads(
                (root / "controller" / "controller-authority.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(
                authority["finalizer_job_observation"],
                {"job_id": "qualification-finalizer", "result": "success"},
            )
            self.assertEqual(authority["final_artifact"]["id"], artifact["id"])
            self.assertNotIn("status", authority["run"])
            self.assertNotIn("conclusion", authority["run"])

    def test_controller_rejects_self_consistent_archive_for_other_source_or_attempt(self):
        module = self._api()
        from scripts.release_qualification import build_qualification_evidence

        required = (
            "preflight",
            "full-ci",
            "full-release-gate",
            "performance",
            "platform-qualification",
            "release-authority",
            "dry-run",
        )
        needs = {name: {"result": "success"} for name in required}
        receipt_bytes = _canonical({"schema": "receipt-fixture"})

        def build_controller(**arguments):
            output = arguments["output"]
            output.mkdir()
            (output / "candidate-input.json").write_bytes(b"candidate\n")
            (output / "verified-candidate.json").write_bytes(b"verified\n")
            return {"status": "PASS"}

        cases = (
            ("source", "e" * 40, 1, "qualification candidate_sha mismatch"),
            ("attempt", SHA, 2, "qualification run attempt mismatch"),
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for label, evidence_sha, evidence_attempt, error in cases:
                evidence = build_qualification_evidence(
                    workflow_ref=(
                        "yanyuhanyue/AniMemo/.github/workflows/"
                        "release.yml@refs/heads/main"
                    ),
                    workflow_sha=evidence_sha,
                    run_id=str(RUN_ID),
                    run_attempt=evidence_attempt,
                    candidate_sha=evidence_sha,
                    candidate_tree=TREE,
                    upgrade_base_sha="c" * 40,
                    channel="rc",
                    target_version="v1.1.0",
                    release_tag="v1.1.0-rc.19",
                    needs=needs,
                    current_job_id="qualification-finalizer",
                    candidate_production_receipt_sha256=_digest(receipt_bytes),
                    producer_job_observation={"id": "dry-run", "result": "success"},
                    provisional_artifact={
                        "id": 11,
                        "name": f"candidate-materials-{RUN_ID}",
                        "api_digest": "sha256:" + "d" * 64,
                        "archive_sha256": "sha256:" + "d" * 64,
                    },
                    created_at="2026-09-01T10:00:00Z",
                    event="workflow_dispatch",
                    release_notes_identity="sha256:" + "a" * 64,
                    release_notes_markdown_sha256="sha256:" + "b" * 64,
                )
                archive = _zip(
                    root / f"final-{label}.zip",
                    [
                        ("candidate-production-receipt.json", receipt_bytes, None),
                        (
                            f"release-qualification-{RUN_ID}.json",
                            _canonical(evidence),
                            None,
                        ),
                    ],
                )
                artifact = _final_metadata(archive)
                request = _request(root / f"controller-{label}")
                request.update(
                    {
                        "current_job_id": "controller-authority",
                        "required_result_jobs": ["qualification-finalizer"],
                        "needs": {
                            "qualification-finalizer": {"result": "success"}
                        },
                        "final_artifact": {
                            "id": artifact["id"],
                            "name": artifact["name"],
                            "api_digest": artifact["digest"],
                        },
                    }
                )
                with mock.patch.object(
                    module,
                    "build_prepublication_controller_authority",
                    side_effect=build_controller,
                ), self.subTest(label=label), self.assertRaisesRegex(ValueError, error):
                    module.verify_uploaded_qualification(
                        request,
                        {
                            "total_count": 1,
                            "artifacts": [artifact],
                            "archive_path": str(archive),
                        },
                    )

    def test_controller_rejects_final_artifact_output_identity_mismatch(self):
        module = self._api()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = _zip(root / "final.zip", [("payload", b"x", None)])
            artifact = _final_metadata(archive)
            request = _request(root / "controller")
            request.update(
                {
                    "current_job_id": "controller-authority",
                    "required_result_jobs": ["qualification-finalizer"],
                    "needs": {
                        "qualification-finalizer": {"result": "success"}
                    },
                    "final_artifact": {
                        "id": 100,
                        "name": artifact["name"],
                        "api_digest": artifact["digest"],
                    },
                }
            )
            with self.assertRaisesRegex(ValueError, "ArtifactIdentityMismatch"):
                module.verify_uploaded_qualification(
                    request,
                    {
                        "total_count": 1,
                        "artifacts": [artifact],
                        "archive_path": str(archive),
                    },
                )

    def test_phase_b_verifies_controller_authority_bytes_against_final_archive(self):
        module = self._api()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            qualification = root / "qualification"
            verified = root / "verified"
            qualification.mkdir()
            verified.mkdir()
            receipt_bytes = _canonical({"schema": "receipt-fixture"})
            qualification_bytes = _canonical({"schema": "qualification-fixture"})
            candidate_bytes = b"candidate-input\n"
            verified_bytes = b"verified-candidate\n"
            (qualification / "candidate-production-receipt.json").write_bytes(
                receipt_bytes
            )
            (qualification / f"release-qualification-{RUN_ID}.json").write_bytes(
                qualification_bytes
            )
            (qualification / "candidate-input.json").write_bytes(candidate_bytes)
            (verified / "candidate-input.json").write_bytes(candidate_bytes)
            (verified / "verified-candidate.json").write_bytes(verified_bytes)

            final_archive = _zip(root / "final.zip", [("payload", b"final", None)])
            final_artifact = _final_metadata(final_archive)
            authority = {
                "schema": "animemo.qualification-controller-authority/v1",
                "version": 1,
                "authority": "FINAL_ARTIFACT_REMOTE_READBACK_VERIFIED",
                "repository": "yanyuhanyue/AniMemo",
                "workflow": _request(root / "unused")["workflow"],
                "run": _request(root / "unused")["run"],
                "source_sha": SHA,
                "finalizer_job_observation": {
                    "job_id": "qualification-finalizer",
                    "result": "success",
                },
                "final_artifact": {
                    "id": final_artifact["id"],
                    "name": final_artifact["name"],
                    "size_in_bytes": final_artifact["size_in_bytes"],
                    "api_digest": final_artifact["digest"],
                    "archive_sha256": final_artifact["digest"],
                    "run_id": str(RUN_ID),
                    "head_sha": SHA,
                },
                "qualification_sha256": _digest(qualification_bytes),
                "candidate_production_receipt_sha256": _digest(receipt_bytes),
                "candidate_input_sha256": _digest(candidate_bytes),
                "verified_candidate_sha256": _digest(verified_bytes),
                "final_run_state_authority": "EXTERNAL_PHASE_B_REQUIRED",
            }
            controller_archive = _zip(
                root / "controller.zip",
                [
                    ("candidate-input.json", candidate_bytes, None),
                    ("verified-candidate.json", verified_bytes, None),
                    ("controller-authority.json", _canonical(authority), None),
                ],
            )
            controller_artifact = _metadata(
                controller_archive, name=f"controller-authority-{RUN_ID}"
            )
            controller_artifact["id"] = 100
            request = _request(root / "phase-b-result.json")
            request["final_artifact"] = {
                "id": final_artifact["id"],
                "name": final_artifact["name"],
                "api_digest": final_artifact["digest"],
            }
            request["controller_artifact"] = {
                "id": controller_artifact["id"],
                "name": controller_artifact["name"],
                "api_digest": controller_artifact["digest"],
            }
            observed = {
                "total_count": 2,
                "artifacts": [final_artifact, controller_artifact],
                "qualification_archive_path": str(final_archive),
                "controller_archive_path": str(controller_archive),
            }
            with mock.patch.object(
                module,
                "_extract_archive",
                wraps=module._extract_archive,
            ) as extract:
                result = module._verify_phase_b_controller_authority(
                    request, observed, qualification, verified
                )
            self.assertEqual(result["status"], "PASS")
            extract.assert_called_once_with(
                controller_archive,
                mock.ANY,
                max_member_count=3,
                max_member_bytes=8 * 1024 * 1024,
                max_expanded_bytes=24 * 1024 * 1024,
            )

            authority["candidate_input_sha256"] = "sha256:" + "f" * 64
            controller_archive = _zip(
                root / "controller-tampered.zip",
                [
                    ("candidate-input.json", candidate_bytes, None),
                    ("verified-candidate.json", verified_bytes, None),
                    ("controller-authority.json", _canonical(authority), None),
                ],
            )
            controller_artifact = _metadata(
                controller_archive, name=f"controller-authority-{RUN_ID}"
            )
            controller_artifact["id"] = 100
            request["controller_artifact"]["api_digest"] = controller_artifact[
                "digest"
            ]
            observed.update(
                {
                    "artifacts": [final_artifact, controller_artifact],
                    "controller_archive_path": str(controller_archive),
                }
            )
            with self.assertRaisesRegex(ValueError, "ControllerAuthorityMismatch"):
                module._verify_phase_b_controller_authority(
                    request, observed, qualification, verified
                )

    def test_controller_archive_member_limit_rejects_before_writing(self):
        module = self._api()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = _zip(
                root / "controller-bomb.zip",
                [("candidate-input.json", b"\0" * (8 * 1024 * 1024 + 1), None)],
            )
            self.assertLess(archive.stat().st_size, 16 * 1024 * 1024)
            destination = root / "extracted"
            destination.mkdir()
            with self.assertRaisesRegex(ValueError, "UnsafeArchiveMember"):
                module._extract_archive(
                    archive,
                    destination,
                    max_member_count=3,
                    max_member_bytes=8 * 1024 * 1024,
                    max_expanded_bytes=24 * 1024 * 1024,
                )
            self.assertEqual(list(destination.iterdir()), [])

    def test_phase_b_cli_routes_both_archives_and_writes_canonical_result(self):
        module = self._api()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            request_path = root / "request.json"
            metadata_path = root / "artifacts.json"
            final_archive = root / "final.zip"
            controller_archive = root / "controller.zip"
            qualification = root / "qualification"
            verified = root / "verified"
            output = root / "result.json"
            request_path.write_bytes(_canonical(_request(root / "unused")))
            metadata_path.write_bytes(
                _canonical({"total_count": 0, "artifacts": []})
            )
            final_archive.write_bytes(b"final")
            controller_archive.write_bytes(b"controller")
            qualification.mkdir()
            verified.mkdir()
            result = {"schema": "phase-b-fixture", "status": "PASS"}
            with mock.patch.object(
                module,
                "_verify_phase_b_controller_authority",
                return_value=result,
            ) as verify:
                exit_code = module._main(
                    [
                        "phase-b",
                        "--request",
                        str(request_path),
                        "--artifacts-metadata",
                        str(metadata_path),
                        "--archive",
                        str(final_archive),
                        "--controller-archive",
                        str(controller_archive),
                        "--qualification-directory",
                        str(qualification),
                        "--verified-candidate-directory",
                        str(verified),
                        "--output",
                        str(output),
                    ]
                )
            self.assertEqual(exit_code, 0)
            self.assertEqual(output.read_bytes(), _canonical(result))
            self.assertEqual(verify.call_args.args[2:], (qualification, verified))
            self.assertEqual(
                verify.call_args.args[1]["controller_archive_path"],
                str(controller_archive),
            )


class CandidateProductionReceiptRedContractTests(unittest.TestCase):
    def test_receipt_closes_exact_member_inventory_excluding_itself(self):
        from release.materials import (
            build_candidate_production_receipt,
            validate_candidate_production_receipt,
        )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "nested").mkdir()
            (root / "a.txt").write_bytes(b"a")
            (root / "nested" / "b.bin").write_bytes(b"bb")
            identity = {
                "repository": "yanyuhanyue/AniMemo",
                "workflow_ref": "yanyuhanyue/AniMemo/.github/workflows/release.yml@refs/heads/main",
                "workflow_sha": SHA,
                "run_id": str(RUN_ID),
                "run_attempt": 1,
                "event": "workflow_dispatch",
                "candidate_sha": SHA,
                "candidate_tree": TREE,
                "target_version": "v1.1.0",
                "release_tag": "v1.1.0-rc.19",
                "channel": "rc",
            }
            receipt = build_candidate_production_receipt(root=root, identity=identity)
            self.assertEqual(
                receipt["no_rebuild_policy"],
                "REBUILD_FORBIDDEN_BYTE_EXACT_COPY_REQUIRED",
            )
            self.assertEqual(
                [item["path"] for item in receipt["member_inventory"]],
                ["a.txt", "nested/b.bin"],
            )
            validate_candidate_production_receipt(receipt, root=root, identity=identity)
            receipt["no_rebuild_policy"] = "REBUILD_ALLOWED"
            with self.assertRaisesRegex(ValueError, "no-rebuild policy"):
                validate_candidate_production_receipt(
                    receipt, root=root, identity=identity
                )

    def test_receipt_detects_post_close_byte_mutation(self):
        from release.materials import (
            build_candidate_production_receipt,
            validate_candidate_production_receipt,
        )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "payload.bin"
            target.write_bytes(b"before")
            identity = {
                "repository": "yanyuhanyue/AniMemo",
                "workflow_ref": "yanyuhanyue/AniMemo/.github/workflows/release.yml@refs/heads/main",
                "workflow_sha": SHA,
                "run_id": str(RUN_ID),
                "run_attempt": 1,
                "event": "workflow_dispatch",
                "candidate_sha": SHA,
                "candidate_tree": TREE,
                "target_version": "v1.1.0",
                "release_tag": "v1.1.0-rc.19",
                "channel": "rc",
            }
            receipt = build_candidate_production_receipt(root=root, identity=identity)
            target.write_bytes(b"after")
            with self.assertRaisesRegex(ValueError, "ByteSetMismatch"):
                validate_candidate_production_receipt(receipt, root=root, identity=identity)


if __name__ == "__main__":
    unittest.main()
