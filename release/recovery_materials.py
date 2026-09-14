"""Consume the original Qualification and Candidate bytes with canonical readers."""

from __future__ import annotations

import hashlib
import json
import shutil
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any

from .candidate import (
    canonical_json_bytes,
    decode_aggregate_receipt_b64url,
    load_verified_candidate,
    validate_aggregate_receipt,
    verify_prepublication_candidate,
)
from .materials import extract_qualification_artifact, read_bounded_release_file
from .metadata_freshness import validate_qualification_run_metadata
from .portable import build_portable_payload
from .publication import build_publication_plan
from .publication_input import build_publish_candidate_plan
from .qualification_finalization import _verify_phase_b_controller_authority
from .recovery_contract import identity, policy, require, utc
from .recovery_remote import file_digest


def decode_original_aggregate(wire: str, destination: Path) -> dict[str, Any]:
    receipt, raw = decode_aggregate_receipt_b64url(wire)
    require(
        "sha256:" + hashlib.sha256(raw).hexdigest()
        == policy()["subject"]["candidate_aggregate_sha256"],
        "RECOVERY_AGGREGATE_BYTES_MISMATCH",
    )
    validate_aggregate_receipt(receipt)
    require(raw == canonical_json_bytes(receipt), "RECOVERY_AGGREGATE_NONCANONICAL")
    with destination.open("xb") as stream:
        stream.write(raw)
    return receipt


def prepare_materials(
    remote, *, root: Path, aggregate: Path, now: datetime
) -> dict[str, Any]:
    p = policy()
    subject = p["subject"]
    qid = subject["qualification_run_id"]
    run = remote.get(f"{remote.base}/actions/runs/{qid}")
    jobs = remote.listed(f"{remote.base}/actions/runs/{qid}/jobs", "jobs")
    artifacts = remote.listed(
        f"{remote.base}/actions/runs/{qid}/artifacts", "artifacts"
    )
    selected = validate_qualification_run_metadata(
        run_metadata=run,
        jobs_metadata=jobs,
        artifacts_metadata=artifacts,
        expected_run_id=qid,
        expected_sha=subject["sha"],
    )
    require(
        selected["artifactId"] == p["qualificationArtifacts"]["final_artifact"]["id"],
        "RECOVERY_QUALIFICATION_ID_MISMATCH",
    )
    archives = {}
    observations = {}
    for role, expected in p["qualificationArtifacts"].items():
        matches = [a for a in artifacts["artifacts"] if a["id"] == expected["id"]]
        require(len(matches) == 1, "RECOVERY_QUALIFICATION_ARTIFACT_MISSING")
        metadata = matches[0]
        require(
            all(metadata[k] == v for k, v in expected.items())
            and metadata.get("expired") is False,
            "RECOVERY_QUALIFICATION_ARTIFACT_CHANGED",
        )
        path = root / (role + ".zip")
        remote.download_artifact(metadata, path)
        require(
            file_digest(path) == (expected["digest"], expected["size_in_bytes"]),
            "RECOVERY_ARCHIVE_BYTES_INVALID",
        )
        archives[role], observations[role] = path, metadata
    extracted = root / "qualification"
    extract_qualification_artifact(
        archives["final_artifact"],
        extracted,
        qualification_run_id=qid,
        expected_sha256=observations["final_artifact"]["digest"],
        require_candidate_contract=True,
    )
    state = root / "verified-candidates"
    verified = verify_prepublication_candidate(
        archive=archives["final_artifact"],
        run_metadata=run,
        jobs_metadata=jobs,
        artifacts_metadata=artifacts,
        containing_artifact_id=observations["final_artifact"]["id"],
        containing_artifact_api_digest=observations["final_artifact"]["digest"],
        expected_run_id=qid,
        expected_source_sha=subject["sha"],
        expected_source_tree=subject["tree"],
        expected_candidate_version=subject["release_tag"],
        verified_at=utc(now),
        _state_root=state,
    )
    require(
        verified["candidateInputDigest"] == subject["candidate_input_sha256"]
        and verified["verifiedCandidateDigest"] == subject["verified_candidate_sha256"],
        "RECOVERY_CANDIDATE_IDENTITY_MISMATCH",
    )
    loaded = load_verified_candidate(
        verified["verifiedCandidateDigest"], _state_root=state
    )
    phase_b = _verify_phase_b_controller_authority(
        p["phaseBRequest"],
        {
            "total_count": artifacts["total_count"],
            "artifacts": artifacts["artifacts"],
            "qualification_archive_path": str(archives["final_artifact"]),
            "controller_archive_path": str(archives["controller_artifact"]),
        },
        extracted,
        loaded.root,
    )
    require(phase_b["status"] == "PASS", "RECOVERY_CONTROLLER_AUTHORITY_INVALID")
    with zipfile.ZipFile(archives["platform_artifact"]) as archive:
        require(
            archive.namelist() == ["platform-qualification.json"],
            "RECOVERY_PLATFORM_ARCHIVE_INVALID",
        )
        require(
            archive.read("platform-qualification.json")
            == (loaded.root / "platform-qualification.json").read_bytes(),
            "RECOVERY_PLATFORM_BYTES_INVALID",
        )
    receipt_bytes = read_bounded_release_file(
        aggregate, subject="Candidate aggregate", maximum=384 * 1024
    )
    require(
        "sha256:" + hashlib.sha256(receipt_bytes).hexdigest()
        == subject["candidate_aggregate_sha256"],
        "RECOVERY_AGGREGATE_BYTES_MISMATCH",
    )
    receipt = validate_aggregate_receipt(json.loads(receipt_bytes))
    candidate_plan = build_publish_candidate_plan(loaded, receipt)
    output = root / "release-output"
    output.mkdir(mode=0o700)
    for name in (*p["plan"]["assets"], "release-notes.json", "release-notes.md"):
        shutil.copyfile(loaded.root / name, output / name)
    for name in ("candidate-input.json", "verified-candidate.json"):
        shutil.copyfile(loaded.root / name, output / name)
    shutil.copyfile(aggregate, output / "candidate-acceptance-receipt.json")
    shutil.copyfile(
        loaded.root / f"release-qualification-{qid}.json",
        output / "release-qualification.json",
    )
    for name, expected in p["plan"]["assets"].items():
        require(
            file_digest(output / name) == (expected["sha256"], expected["size"]),
            "RECOVERY_ASSET_BYTES_INVALID",
        )
    transport_source = root / "portable-source"
    transport_source.mkdir(mode=0o700)
    (transport_source / "authority").mkdir(mode=0o700)
    for name in p["plan"]["assets"]:
        shutil.copyfile(output / name, transport_source / "authority" / name)
    shutil.copytree(loaded.root / "candidate-runtime" / "oci", transport_source / "oci")
    images = [
        {
            "role": role,
            "repository": value["repository"],
            "digest": value["digest"],
            "layoutPath": "oci/" + role,
            "platform": "linux/amd64",
        }
        for role, value in sorted(loaded.manifest["images"].items())
    ]
    portable_name, portable_expected = next(iter(p["plan"]["transport_assets"].items()))
    portable = build_portable_payload(transport_source, output / portable_name, images)
    require(
        (portable.archive_sha256, portable.archive_size)
        == (portable_expected["sha256"], portable_expected["size"]),
        "RECOVERY_PORTABLE_BYTES_MISMATCH",
    )
    qualification = json.loads(
        (loaded.root / f"release-qualification-{qid}.json").read_text(encoding="utf-8")
    )
    notes = json.loads((output / "release-notes.json").read_text(encoding="utf-8"))
    plan = build_publication_plan(
        repository=p["repository"],
        channel="rc",
        tag=subject["release_tag"],
        commit=subject["sha"],
        qualification_identity=qualification["qualification_sha256"],
        release_notes_identity=notes["identity"],
        release_notes_markdown_sha256=file_digest(output / "release-notes.md")[0],
        assets=p["plan"]["assets"],
        transport_assets=p["plan"]["transport_assets"],
        api_digest=p["plan"]["api_digest"],
        web_digest=p["plan"]["web_digest"],
    )
    require(plan == p["plan"], "RECOVERY_PUBLICATION_PLAN_MISMATCH")
    from scripts.release_authority import validate_portable_pipeline_authority

    build_receipt = {
        "archive": str(Path("release-output") / portable_name),
        "sha256": portable.archive_sha256,
        "files": len(portable.files),
        "imageRoles": [i["role"] for i in images],
        "authorityState": portable.index["authorityState"],
    }
    for name, value in (
        ("portable-build-receipt.json", build_receipt),
        (
            "portable-pipeline-authority.json",
            validate_portable_pipeline_authority(plan, build_receipt),
        ),
    ):
        (output / name).write_bytes(canonical_json_bytes(value))
    for name, value in (
        ("publication-plan.json", plan),
        ("publish-candidate-plan.json", candidate_plan),
        ("phase-b.json", phase_b),
        ("qualification-run.json", run),
        ("qualification-artifacts.json", artifacts),
    ):
        (output / name).write_bytes(canonical_json_bytes(value))
    return {
        "plan": plan,
        "candidateRoot": str(loaded.root),
        "assetRoot": str(output),
        "candidatePlan": candidate_plan,
        "archiveIdentities": {
            role: {
                "id": item["id"],
                "digest": item["digest"],
                "size": item["size_in_bytes"],
            }
            for role, item in observations.items()
        },
        "subject": subject,
        "runtimeRebuildCount": 0,
        "materialBinding": identity({"subject": subject, "plan": plan}),
    }
