"""Fail-closed Release Producer authority decision for Beta and RC channels."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from release.notes import CANONICAL_RELEASE_ASSETS
from release.portable import BLOCKED_PORTABLE_PUBLICATION_AUTHORITY
from release.publication import (
    SCHEMA as PORTABLE_PUBLICATION_SCHEMA,
)
from release.publication import (
    PublicationError,
    validate_publication_plan,
)

_PORTABLE_PUBLICATION_PLAN = Path("release-output/publication-plan.json")
_PORTABLE_BUILD_RECEIPT = Path("release-output/portable-build-receipt.json")

try:
    from scripts.release_qualification import (
        QualificationError,
        build_qualification_evidence,
        read_qualification_evidence,
        resolve_qualification_evidence,
    )
except ModuleNotFoundError:  # pragma: no cover - direct script execution
    from release_qualification import (
        QualificationError,
        build_qualification_evidence,
        read_qualification_evidence,
        resolve_qualification_evidence,
    )


class ReleaseAuthorityError(ValueError):
    pass


def validate_portable_pipeline_authority(
    publication_plan: Mapping[str, Any],
    portable_build_receipt: Mapping[str, Any],
) -> dict[str, Any]:
    """Bind one derived transport asset to a v2 plan without granting authority."""

    try:
        plan = validate_publication_plan(publication_plan)
    except PublicationError as error:
        raise ReleaseAuthorityError(str(error)) from error
    required_receipt = {
        "archive",
        "sha256",
        "files",
        "imageRoles",
        "authorityState",
    }
    if (
        plan.get("schema") != PORTABLE_PUBLICATION_SCHEMA
        or set(plan.get("assets", {})) != set(CANONICAL_RELEASE_ASSETS)
        or not isinstance(plan.get("transport_assets"), Mapping)
        or len(plan["transport_assets"]) != 1
        or not isinstance(portable_build_receipt, Mapping)
        or set(portable_build_receipt) != required_receipt
    ):
        raise ReleaseAuthorityError("portable publication authority inputs are not closed")
    name, declared = next(iter(plan["transport_assets"].items()))
    archive = portable_build_receipt["archive"]
    files = portable_build_receipt["files"]
    if (
        not isinstance(archive, str)
        or Path(archive).name != name
        or portable_build_receipt["sha256"] != declared["sha256"]
        or isinstance(files, bool)
        or not isinstance(files, int)
        or files < 1
        or portable_build_receipt["imageRoles"]
        != ["api", "postgres", "redis", "web"]
        or portable_build_receipt["authorityState"]
        != BLOCKED_PORTABLE_PUBLICATION_AUTHORITY
    ):
        raise ReleaseAuthorityError("portable build receipt differs from publication plan")
    return {
        "schema": "animemo.portable-pipeline-authority/v1",
        "status": "PASS",
        "publication_plan_identity": plan["identity"],
        "portable_sha256": declared["sha256"],
        "canonical_authority_asset_count": 4,
        "declared_transport_asset_count": 1,
        "portable_authority": "FORBIDDEN",
        "build_once": "PASS",
        "image_materialization": "COPY_EXACT_OCI_LAYOUTS_ONLY",
    }


def validate_release_authority(channel: str, needs: Mapping[str, Any]) -> dict[str, str]:
    normalized_channel = str(channel or "").strip().lower()
    if normalized_channel not in {"beta", "rc"}:
        raise ReleaseAuthorityError(f"unsupported release channel: {channel or '<unset>'}")

    required_results = ("preflight", "full-ci", "full-release-gate")
    failures: dict[str, str] = {}
    for name in required_results:
        job = needs.get(name)
        result = job.get("result") if isinstance(job, Mapping) else None
        if result != "success":
            failures[name] = str(result or "missing")

    performance = needs.get("performance")
    performance_result = performance.get("result") if isinstance(performance, Mapping) else None
    expected_performance = "success" if normalized_channel == "rc" else "skipped"
    if performance_result != expected_performance:
        failures["performance"] = str(performance_result or "missing")

    if failures:
        raise ReleaseAuthorityError(json.dumps(failures, sort_keys=True))
    return {"channel": normalized_channel, "status": "PASS"}


def validate_phase_a_authority(
    channel: str,
    needs: Mapping[str, Any],
    *,
    identity: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate all Phase A gates and create immutable qualification evidence."""

    result = validate_release_authority(channel, needs)
    if not identity.get("emit_evidence", False):
        return {**result, "operation": "qualify"}
    try:
        evidence = build_qualification_evidence(
            workflow_ref=str(identity["workflow_ref"]),
            workflow_sha=str(identity["workflow_sha"]),
            run_id=str(identity["run_id"]),
            run_attempt=int(identity["run_attempt"]),
            candidate_sha=str(identity["candidate_sha"]),
            candidate_tree=str(identity["candidate_tree"]),
            upgrade_base_sha=str(identity["upgrade_base_sha"]),
            channel=channel,
            target_version=str(identity["target_version"]),
            release_tag=str(identity["release_tag"]),
            needs=needs,
            current_job_id=str(identity["current_job_id"]),
            candidate_production_receipt_sha256=str(
                identity["candidate_production_receipt_sha256"]
            ),
            producer_job_observation=identity["producer_job_observation"],
            provisional_artifact=identity["provisional_artifact"],
            created_at=str(identity.get("created_at", "1970-01-01T00:00:00Z")),
            event=str(identity.get("event", "workflow_dispatch")),
            release_notes_identity=identity.get("release_notes_identity"),
            release_notes_markdown_sha256=identity.get(
                "release_notes_markdown_sha256"
            ),
        )
    except (KeyError, TypeError, ValueError, QualificationError) as error:
        raise ReleaseAuthorityError(str(error)) from error
    return {**result, "operation": "qualify", "evidence": evidence}


def validate_phase_b_authority(
    channel: str,
    needs: Mapping[str, Any],
    *,
    qualification: Mapping[str, Any],
    expected: Mapping[str, Any],
    run_metadata: Mapping[str, Any] | None = None,
    artifact_metadata: Mapping[str, Any] | None = None,
    archive_sha256: str | None = None,
) -> dict[str, Any]:
    """Validate a publish run against previously completed qualification evidence."""

    normalized_channel = str(channel or "").strip().lower()
    if normalized_channel not in {"beta", "rc"}:
        raise ReleaseAuthorityError(f"unsupported release channel: {channel or '<unset>'}")
    # A publish run must not silently re-use a skipped or partial gate result.
    for name, item in needs.items():
        result = item.get("result") if isinstance(item, Mapping) else item
        if result not in {"success", "skipped"}:
            raise ReleaseAuthorityError(json.dumps({name: str(result or "missing")}, sort_keys=True))
    try:
        expected_identity = {**dict(expected), "channel": normalized_channel}
        if run_metadata is None or artifact_metadata is None or not archive_sha256:
            raise QualificationError(
                "qualification run metadata, artifact metadata, and archive digest are required for publish"
            )
        validated = resolve_qualification_evidence(
            qualification_run_id=str(expected_identity.get("qualification_run_id", "")),
            run_metadata=run_metadata,
            artifact=artifact_metadata,
            expected=expected_identity,
            evidence=qualification,
            archive_sha256=archive_sha256,
        )
    except QualificationError as error:
        raise ReleaseAuthorityError(str(error)) from error
    return {"channel": normalized_channel, "status": "PASS", "operation": "publish", "evidence": validated}


def validate_operation(
    operation: str,
    channel: str,
    needs: Mapping[str, Any],
    *,
    identity: Mapping[str, Any] | None = None,
    qualification: Mapping[str, Any] | None = None,
    expected: Mapping[str, Any] | None = None,
    run_metadata: Mapping[str, Any] | None = None,
    artifact_metadata: Mapping[str, Any] | None = None,
    archive_sha256: str | None = None,
) -> dict[str, Any]:
    """Dispatch the explicit qualify/publish authority operation."""

    normalized_operation = str(operation or "").strip().lower()
    if normalized_operation == "qualify":
        if identity is None:
            raise ReleaseAuthorityError("qualification identity is required")
        return validate_phase_a_authority(channel, needs, identity=identity)
    if normalized_operation == "publish":
        if qualification is None:
            raise ReleaseAuthorityError("qualification evidence is required for publish")
        if expected is None:
            raise ReleaseAuthorityError("qualification identity expectations are required for publish")
        return validate_phase_b_authority(
            channel,
            needs,
            qualification=qualification,
            expected=expected,
            run_metadata=run_metadata,
            artifact_metadata=artifact_metadata,
            archive_sha256=archive_sha256,
        )
    raise ReleaseAuthorityError(f"unsupported release operation: {operation or '<unset>'}")


def main(argv: Sequence[str] | None = None) -> int:
    del argv
    operation = os.getenv("OPERATION", "qualify").strip().lower()
    if operation == "portable":
        try:
            plan = json.loads(_PORTABLE_PUBLICATION_PLAN.read_text(encoding="utf-8"))
            receipt = json.loads(_PORTABLE_BUILD_RECEIPT.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ReleaseAuthorityError("portable authority input is unreadable") from error
        result = validate_portable_pipeline_authority(plan, receipt)
        print(json.dumps(result, sort_keys=True))
        return 0
    if operation == "produce":
        from release.materials import build_candidate_production_receipt

        root = Path("release-qualification")
        identity = {
            "repository": os.getenv("GITHUB_REPOSITORY", "yanyuhanyue/AniMemo"),
            "workflow_ref": os.getenv("WORKFLOW_REF", os.getenv("GITHUB_WORKFLOW_REF", "")),
            "workflow_sha": os.getenv("WORKFLOW_SHA", os.getenv("GITHUB_SHA", "")),
            "run_id": os.getenv("RUN_ID", os.getenv("GITHUB_RUN_ID", "")),
            "run_attempt": int(
                os.getenv("RUN_ATTEMPT", os.getenv("GITHUB_RUN_ATTEMPT", "1"))
            ),
            "event": os.getenv(
                "EVENT_NAME", os.getenv("GITHUB_EVENT_NAME", "workflow_dispatch")
            ),
            "candidate_sha": os.getenv("CANDIDATE_SHA", ""),
            "candidate_tree": os.getenv("CANDIDATE_TREE", ""),
            "target_version": os.getenv("TARGET_VERSION", ""),
            "release_tag": os.getenv("RELEASE_TAG", ""),
            "channel": os.getenv("CHANNEL", ""),
        }
        receipt = build_candidate_production_receipt(root=root, identity=identity)
        encoded = (
            json.dumps(
                receipt,
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8")
        path = root / "candidate-production-receipt.json"
        path.write_bytes(encoded)
        print(
            json.dumps(
                {
                    "operation": "produce",
                    "receipt_path": path.as_posix(),
                    "schema": receipt.get("schema"),
                },
                sort_keys=True,
            )
        )
        return 0
    channel = os.getenv("CHANNEL", "")
    raw_needs = os.getenv("NEEDS_JSON", "")
    try:
        needs = json.loads(raw_needs)
    except json.JSONDecodeError as error:
        raise ReleaseAuthorityError(f"invalid NEEDS_JSON: {error}") from error
    if not isinstance(needs, dict):
        raise ReleaseAuthorityError("NEEDS_JSON must be an object")
    if operation == "qualify":
        identity = {
            "workflow_ref": os.getenv("WORKFLOW_REF", ""),
            "workflow_sha": os.getenv("WORKFLOW_SHA", os.getenv("GITHUB_SHA", "")),
            "run_id": os.getenv("RUN_ID", os.getenv("GITHUB_RUN_ID", "")),
            "run_attempt": os.getenv("RUN_ATTEMPT", os.getenv("GITHUB_RUN_ATTEMPT", "1")),
            "candidate_sha": os.getenv("CANDIDATE_SHA", ""),
            "candidate_tree": os.getenv("CANDIDATE_TREE", ""),
            "upgrade_base_sha": os.getenv("UPGRADE_BASE_SHA", ""),
            "target_version": os.getenv("TARGET_VERSION", ""),
            "release_tag": os.getenv("RELEASE_TAG", ""),
            "created_at": os.getenv("CREATED_AT", "1970-01-01T00:00:00Z"),
            "emit_evidence": bool(os.getenv("QUALIFICATION_ARTIFACT_PATH", "")),
            "event": os.getenv("EVENT_NAME", os.getenv("GITHUB_EVENT_NAME", "workflow_dispatch")),
            "release_notes_identity": os.getenv("RELEASE_NOTES_IDENTITY", "") or None,
            "release_notes_markdown_sha256": os.getenv(
                "RELEASE_NOTES_MARKDOWN_SHA256", ""
            )
            or None,
        }
        result = validate_phase_a_authority(channel, needs, identity=identity)
        artifact_path = os.getenv("QUALIFICATION_ARTIFACT_PATH", "")
        if artifact_path:
            Path(artifact_path).write_text(
                json.dumps(result["evidence"], ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
    elif operation == "publish":
        artifact_path = os.getenv("QUALIFICATION_ARTIFACT_PATH", "")
        if not artifact_path:
            raise ReleaseAuthorityError("QUALIFICATION_ARTIFACT_PATH is required for publish")
        qualification = read_qualification_evidence(Path(artifact_path))
        expected = {
            "qualification_run_id": os.getenv("QUALIFICATION_RUN_ID", ""),
            "candidate_sha": os.getenv("CANDIDATE_SHA", ""),
            "upgrade_base_sha": os.getenv("UPGRADE_BASE_SHA", ""),
            "channel": channel,
            "target_version": os.getenv("TARGET_VERSION", ""),
            "release_tag": os.getenv("RELEASE_TAG", ""),
            "workflow_ref": os.getenv("QUALIFICATION_WORKFLOW_REF", ""),
            "workflow_sha": os.getenv("QUALIFICATION_WORKFLOW_SHA", ""),
            "release_graph_contract": os.getenv("RELEASE_GRAPH_CONTRACT", "animemo.release-gate.jobs/v2"),
            "release_notes_identity": os.getenv("RELEASE_NOTES_IDENTITY", ""),
            "release_notes_markdown_sha256": os.getenv(
                "RELEASE_NOTES_MARKDOWN_SHA256", ""
            ),
        }
        expected = {key: value for key, value in expected.items() if value != ""}
        def _json_env(name: str) -> Mapping[str, Any] | None:
            raw = os.getenv(name, "")
            if not raw:
                return None
            parsed = json.loads(raw)
            if not isinstance(parsed, Mapping):
                raise ReleaseAuthorityError(f"{name} must be an object")
            return parsed

        result = validate_phase_b_authority(
            channel,
            needs,
            qualification=qualification,
            expected=expected,
            run_metadata=_json_env("QUALIFICATION_RUN_METADATA"),
            artifact_metadata=_json_env("QUALIFICATION_ARTIFACT_METADATA"),
            archive_sha256=os.getenv("QUALIFICATION_ARCHIVE_SHA256", "") or None,
        )
    else:
        raise ReleaseAuthorityError(f"unsupported release operation: {operation or '<unset>'}")
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
