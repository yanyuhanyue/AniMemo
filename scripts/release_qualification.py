"""Strict qualification evidence for the two-phase Release Producer.

Qualification is deliberately a small, self-contained contract.  The Phase A
run creates one canonical JSON document after all required gates pass.  A later
publish run may consume that document only after independently authenticating
the originating workflow run and comparing every release identity field.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

QUALIFICATION_SCHEMA = "animemo.release-qualification/v1"
QUALIFICATION_SCHEMA_V2 = "animemo.release-qualification/v2"
RELEASE_WORKFLOW_PATH = ".github/workflows/release.yml"
RELEASE_WORKFLOW_NAME = "Release Producer"
RELEASE_GRAPH_CONTRACT = "animemo.release-gate.jobs/v2"
REPOSITORY = "yanyuhanyue/AniMemo"
REQUIRED_GATES = ("preflight", "full-ci", "full-release-gate", "performance")
REQUIRED_QUALIFICATION_RESULTS = (
    "full_ci",
    "full_release_gate",
    "stateful_upgrade",
    "dr_rehearsal",
    "rc_performance",
    "release_authority",
    "read_only_release_dry_run",
    "rc_stable_parity",
)
_SHA = re.compile(r"^[0-9a-f]{40}$")
_TAG = re.compile(r"^v[0-9]+\.[0-9]+\.[0-9]+-(?:beta|rc)\.[1-9][0-9]*$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")


class QualificationError(ValueError):
    """Raised whenever qualification evidence cannot be trusted."""


def _canonical_bytes(payload: Mapping[str, Any]) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise QualificationError(f"duplicate field in qualification evidence: {key}")
        result[key] = value
    return result


def _checksum(payload: Mapping[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(_canonical_bytes(payload)).hexdigest()


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise QualificationError(f"{label} must be a non-empty string")
    return value.strip()


def _sha(value: Any, label: str) -> str:
    value = _text(value, label)
    if not _SHA.fullmatch(value):
        raise QualificationError(f"{label} must be a 40-character lowercase SHA")
    return value


def _run_id(value: Any, label: str) -> str:
    value = _text(value, label)
    if not re.fullmatch(r"[1-9][0-9]*", value):
        raise QualificationError(f"{label} must be a positive decimal run id")
    return value


def _attempt(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise QualificationError("run.attempt must be a positive integer")
    return value


def _require_exact_keys(value: Any, expected: set[str], label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != expected:
        raise QualificationError(f"{label} has unknown or missing fields")
    return value


def _normalize_gate_results(needs: Mapping[str, Any]) -> dict[str, str]:
    results: dict[str, str] = {}
    for name in REQUIRED_GATES:
        item = needs.get(name)
        result = item.get("result") if isinstance(item, Mapping) else None
        if not isinstance(result, str):
            raise QualificationError(f"gate result missing: {name}")
        results[name] = result
    return results


def _result(needs: Mapping[str, Any], name: str) -> str:
    item = needs.get(name)
    value = item.get("result") if isinstance(item, Mapping) else None
    if not isinstance(value, str):
        raise QualificationError(f"qualification result missing: {name}")
    return value


def _normalize_qualification_results(
    needs: Mapping[str, Any], channel: str, supplied: Mapping[str, Any] | None
) -> dict[str, str]:
    if supplied is not None:
        if not isinstance(supplied, Mapping) or set(supplied) != set(REQUIRED_QUALIFICATION_RESULTS):
            raise QualificationError("qualification_results has unknown or missing fields")
        return {name: _text(supplied[name], f"qualification_results.{name}") for name in REQUIRED_QUALIFICATION_RESULTS}

    full_gate = _result(needs, "full-release-gate")
    performance = _result(needs, "performance")
    dry_run = _result(needs, "dry-run")
    authority = _result(needs, "release-authority")
    parity = dry_run if channel == "rc" else "skipped"
    return {
        "full_ci": _result(needs, "full-ci"),
        "full_release_gate": full_gate,
        "stateful_upgrade": full_gate,
        "dr_rehearsal": full_gate,
        "rc_performance": performance,
        "release_authority": authority,
        "read_only_release_dry_run": dry_run,
        "rc_stable_parity": parity,
    }


def build_qualification_evidence(
    *,
    repository: str = REPOSITORY,
    workflow_path: str = RELEASE_WORKFLOW_PATH,
    workflow_name: str = RELEASE_WORKFLOW_NAME,
    workflow_ref: str,
    workflow_sha: str,
    run_id: str,
    run_attempt: int,
    candidate_sha: str,
    upgrade_base_sha: str,
    channel: str,
    target_version: str,
    release_tag: str,
    needs: Mapping[str, Any],
    created_at: str = "1970-01-01T00:00:00Z",
    qualification_results: Mapping[str, Any] | None = None,
    event: str = "workflow_dispatch",
    status: str = "completed",
    conclusion: str = "success",
    release_graph_contract: str = RELEASE_GRAPH_CONTRACT,
    release_notes_identity: str | None = None,
    release_notes_markdown_sha256: str | None = None,
) -> dict[str, Any]:
    """Build and validate a canonical Phase A qualification document."""

    if (release_notes_identity is None) != (release_notes_markdown_sha256 is None):
        raise QualificationError("release note qualification binding must be complete")
    schema = QUALIFICATION_SCHEMA_V2 if release_notes_identity is not None else QUALIFICATION_SCHEMA
    payload: dict[str, Any] = {
        "schema": schema,
        "repository": _text(repository, "repository"),
        "workflow": {
            "name": _text(workflow_name, "workflow.name"),
            "path": _text(workflow_path, "workflow.path"),
            "ref": _text(workflow_ref, "workflow.ref"),
            "sha": _sha(workflow_sha, "workflow.sha"),
        },
        "run": {
            "id": _run_id(run_id, "run.id"),
            "attempt": _attempt(run_attempt),
            "event": _text(event, "run.event"),
            "status": _text(status, "run.status"),
            "conclusion": _text(conclusion, "run.conclusion"),
        },
        "candidate_sha": _sha(candidate_sha, "candidate_sha"),
        "upgrade_base_sha": _sha(upgrade_base_sha, "upgrade_base_sha"),
        "channel": _text(channel, "channel").lower(),
        "target_version": _text(target_version, "target_version"),
        "release_tag": _text(release_tag, "release_tag"),
        "release_graph_contract": _text(release_graph_contract, "release_graph_contract"),
        "gate_results": _normalize_gate_results(needs),
        "created_at": _text(created_at, "created_at"),
        "qualification_results": _normalize_qualification_results(needs, channel, qualification_results),
    }
    if release_notes_identity is not None:
        if not _DIGEST.fullmatch(release_notes_identity) or not _DIGEST.fullmatch(
            release_notes_markdown_sha256 or ""
        ):
            raise QualificationError("release note qualification identities are invalid")
        payload["release_notes"] = {
            "snapshot_identity": release_notes_identity,
            "markdown_sha256": release_notes_markdown_sha256,
        }
    return finalize_qualification_evidence(payload)


def finalize_qualification_evidence(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Add the canonical checksum and validate the complete document."""

    unsigned = copy.deepcopy(dict(payload))
    unsigned.pop("artifact_sha256", None)
    validate_qualification_evidence(unsigned, require_checksum=False)
    result = dict(unsigned)
    result["artifact_sha256"] = _checksum(unsigned)
    validate_qualification_evidence(result)
    return result


def validate_qualification_evidence(
    payload: Mapping[str, Any],
    *,
    require_checksum: bool = True,
    expected: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate strict schema, immutable identity, gate status, and checksum."""

    required = {
        "schema",
        "repository",
        "workflow",
        "run",
        "candidate_sha",
        "upgrade_base_sha",
        "channel",
        "target_version",
        "release_tag",
        "release_graph_contract",
        "gate_results",
        "created_at",
        "qualification_results",
        "artifact_sha256",
    }
    if not isinstance(payload, Mapping):
        raise QualificationError("qualification evidence must be an object")
    keys = set(payload)
    schema = payload.get("schema")
    if schema == QUALIFICATION_SCHEMA_V2:
        required = required | {"release_notes"}
    elif schema != QUALIFICATION_SCHEMA:
        raise QualificationError("unsupported qualification schema")
    if require_checksum:
        if keys != required:
            raise QualificationError("qualification evidence has unknown or missing fields")
    elif keys - (required - {"artifact_sha256"}):
        raise QualificationError("qualification evidence has unknown fields")

    if payload.get("repository") != REPOSITORY:
        raise QualificationError("qualification repository mismatch")
    workflow = _require_exact_keys(payload.get("workflow"), {"name", "path", "ref", "sha"}, "workflow")
    if workflow["name"] != RELEASE_WORKFLOW_NAME or workflow["path"] != RELEASE_WORKFLOW_PATH:
        raise QualificationError("qualification workflow mismatch")
    _text(workflow["ref"], "workflow.ref")
    _sha(workflow["sha"], "workflow.sha")

    run = _require_exact_keys(
        payload.get("run"), {"id", "attempt", "event", "status", "conclusion"}, "run"
    )
    _run_id(run["id"], "run.id")
    _attempt(run["attempt"])
    if run["event"] != "workflow_dispatch":
        raise QualificationError("qualification run must use workflow_dispatch")
    if run["status"] != "completed" or run["conclusion"] != "success":
        raise QualificationError("qualification run must complete successfully")

    candidate = _sha(payload.get("candidate_sha"), "candidate_sha")
    base = _sha(payload.get("upgrade_base_sha"), "upgrade_base_sha")
    if candidate == base:
        raise QualificationError("candidate and upgrade base must differ")
    channel = payload.get("channel")
    if channel not in {"beta", "rc"}:
        raise QualificationError("qualification channel must be beta or rc")
    target = _text(payload.get("target_version"), "target_version")
    if not re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+", target):
        raise QualificationError("target_version must be a stable semantic version")
    tag = _text(payload.get("release_tag"), "release_tag")
    if not _TAG.fullmatch(tag) or not tag.startswith(target + "-"):
        raise QualificationError("release_tag is inconsistent with target_version")
    if payload.get("release_graph_contract") != RELEASE_GRAPH_CONTRACT:
        raise QualificationError("release graph contract mismatch")
    created_at = _text(payload.get("created_at"), "created_at")
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z", created_at):
        raise QualificationError("created_at must be an RFC3339 UTC timestamp")

    gates = payload.get("gate_results")
    if not isinstance(gates, Mapping) or set(gates) != set(REQUIRED_GATES):
        raise QualificationError("gate_results has unknown or missing fields")
    for name in REQUIRED_GATES:
        result = gates[name]
        if not isinstance(result, str):
            raise QualificationError(f"gate result is invalid: {name}")
        expected_result = "success" if name != "performance" or channel == "rc" else "skipped"
        if result != expected_result:
            raise QualificationError(f"gate {name} did not satisfy qualification policy")

    qualification_results = payload.get("qualification_results")
    if not isinstance(qualification_results, Mapping) or set(qualification_results) != set(
        REQUIRED_QUALIFICATION_RESULTS
    ):
        raise QualificationError("qualification_results has unknown or missing fields")
    for name in REQUIRED_QUALIFICATION_RESULTS:
        result = qualification_results[name]
        expected_result = "success"
        if name in {"rc_performance", "rc_stable_parity"} and channel == "beta":
            expected_result = "skipped"
        if result != expected_result:
            raise QualificationError(f"qualification result {name} did not succeed")

    if schema == QUALIFICATION_SCHEMA_V2:
        notes = _require_exact_keys(
            payload.get("release_notes"),
            {"snapshot_identity", "markdown_sha256"},
            "release_notes",
        )
        if not _DIGEST.fullmatch(str(notes["snapshot_identity"])):
            raise QualificationError("release note snapshot identity is invalid")
        if not _DIGEST.fullmatch(str(notes["markdown_sha256"])):
            raise QualificationError("release note markdown identity is invalid")

    if require_checksum:
        checksum = payload.get("artifact_sha256")
        if not isinstance(checksum, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", checksum):
            raise QualificationError("artifact_sha256 is invalid")
        unsigned = copy.deepcopy(dict(payload))
        unsigned.pop("artifact_sha256", None)
        if checksum != _checksum(unsigned):
            raise QualificationError("qualification artifact checksum mismatch")

    if expected:
        for field in (
            "repository",
            "candidate_sha",
            "upgrade_base_sha",
            "channel",
            "target_version",
            "release_tag",
            "release_graph_contract",
        ):
            if field in expected and payload[field] != expected[field]:
                raise QualificationError(f"qualification {field} mismatch")
        if "qualification_run_id" in expected and run["id"] != str(expected["qualification_run_id"]):
            raise QualificationError("qualification run id mismatch")
        if "workflow_ref" in expected and workflow["ref"] != expected["workflow_ref"]:
            raise QualificationError("qualification workflow ref mismatch")
        if "workflow_sha" in expected and workflow["sha"] != expected["workflow_sha"]:
            raise QualificationError("qualification workflow sha mismatch")
        if "release_notes_identity" in expected:
            notes = payload.get("release_notes")
            if not isinstance(notes, Mapping) or notes.get("snapshot_identity") != expected[
                "release_notes_identity"
            ]:
                raise QualificationError("qualification release notes snapshot mismatch")
        if "release_notes_markdown_sha256" in expected:
            notes = payload.get("release_notes")
            if not isinstance(notes, Mapping) or notes.get("markdown_sha256") != expected[
                "release_notes_markdown_sha256"
            ]:
                raise QualificationError("qualification release notes markdown mismatch")
    return dict(payload)


def resolve_qualification_evidence(
    *,
    qualification_run_id: str,
    run_metadata: Mapping[str, Any],
    artifact: Mapping[str, Any],
    expected: Mapping[str, Any],
    evidence: Mapping[str, Any] | None = None,
    archive_sha256: str | None = None,
) -> dict[str, Any]:
    """Resolve Phase B evidence from already queried run and artifact metadata.

    The caller must fetch these records from GitHub.  This function intentionally
    refuses to infer identity from the caller's event or from a mutable tag.
    """

    requested_id = _run_id(qualification_run_id, "qualification_run_id")
    if str(run_metadata.get("id", "")) != requested_id:
        raise QualificationError("queried qualification run id mismatch")
    run_repository = run_metadata.get("repository")
    if not isinstance(run_repository, Mapping) or run_repository.get("full_name") != REPOSITORY:
        raise QualificationError("qualification run repository mismatch")
    if run_metadata.get("path") != RELEASE_WORKFLOW_PATH or run_metadata.get("name") != RELEASE_WORKFLOW_NAME:
        raise QualificationError("qualification run workflow mismatch")
    if run_metadata.get("event") != "workflow_dispatch":
        raise QualificationError("qualification run event mismatch")
    if run_metadata.get("status") != "completed" or run_metadata.get("conclusion") != "success":
        raise QualificationError("qualification run is not successful")
    if "candidate_sha" in expected and run_metadata.get("head_sha") != expected["candidate_sha"]:
        raise QualificationError("qualification run candidate SHA mismatch")
    if artifact.get("name") != f"release-qualification-{requested_id}":
        raise QualificationError("qualification artifact name mismatch")
    if artifact.get("expired") is not False:
        raise QualificationError("qualification artifact is expired")
    artifact_run = artifact.get("workflow_run")
    if not isinstance(artifact_run, Mapping) or str(artifact_run.get("id", "")) != requested_id:
        raise QualificationError("qualification artifact is not attached to requested run")
    if not artifact.get("archive_download_url"):
        raise QualificationError("qualification artifact download identity is missing")
    metadata_digest = artifact.get("digest")
    if not isinstance(metadata_digest, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", metadata_digest):
        raise QualificationError("qualification artifact archive digest is missing")
    if archive_sha256 is None or archive_sha256 != metadata_digest:
        raise QualificationError("qualification artifact archive digest cannot be proven")
    if evidence is None:
        evidence = expected.get("evidence")
    if not isinstance(evidence, Mapping):
        raise QualificationError("qualification artifact contents are missing")
    workflow = evidence.get("workflow")
    if not isinstance(workflow, Mapping):
        raise QualificationError("qualification workflow identity is missing")
    if run_metadata.get("head_sha") != workflow.get("sha"):
        raise QualificationError("qualification workflow SHA cannot be derived from run metadata")
    metadata_workflow_ref = run_metadata.get("workflow_ref")
    if metadata_workflow_ref is not None and metadata_workflow_ref != workflow.get("ref"):
        raise QualificationError("qualification workflow ref mismatch")
    validated = validate_qualification_evidence(
        evidence,
        expected={**dict(expected), "qualification_run_id": requested_id},
    )
    if "run_attempt" not in run_metadata:
        raise QualificationError("qualification run attempt is missing")
    metadata_attempt = run_metadata["run_attempt"]
    if isinstance(metadata_attempt, bool) or not isinstance(metadata_attempt, int) or metadata_attempt < 1:
        raise QualificationError("qualification run attempt is invalid")
    if metadata_attempt != validated["run"]["attempt"]:
        raise QualificationError("qualification run attempt mismatch")
    return validated


def write_qualification_evidence(path: Path, payload: Mapping[str, Any]) -> None:
    validated = validate_qualification_evidence(payload)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_canonical_bytes(validated))


def read_qualification_evidence(path: Path, *, expected: Mapping[str, Any] | None = None) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_reject_duplicate_keys)
    except (OSError, json.JSONDecodeError) as error:
        raise QualificationError(f"unable to read qualification artifact: {path}") from error
    return validate_qualification_evidence(payload, expected=expected)
