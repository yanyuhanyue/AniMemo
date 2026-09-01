"""Fail-closed Qualification v3 evidence for the two-phase Release Producer.

The in-run Finalizer may attest only facts that already exist: completed direct
``needs`` results, a closed Candidate Production Receipt, and an already
uploaded provisional Artifact. The workflow run's completed/success state
remains an external Phase B observation made after that run has ended.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

MAX_QUALIFICATION_ARTIFACT_BYTES = 8 * 1024 * 1024

QUALIFICATION_SCHEMA = "animemo.release-qualification/v3"
RELEASE_WORKFLOW_PATH = ".github/workflows/release.yml"
RELEASE_WORKFLOW_NAME = "Release Producer"
RELEASE_GRAPH_CONTRACT = "animemo.release-gate.jobs/v2"
REPOSITORY = "yanyuhanyue/AniMemo"
QUALIFICATION_FINALIZER_JOB_ID = "qualification-finalizer"
PRODUCER_JOB_ID = "dry-run"
REQUIRED_RESULT_JOB_IDS = (
    "preflight",
    "full-ci",
    "full-release-gate",
    "performance",
    "platform-qualification",
    "release-authority",
    "dry-run",
)
REQUIRED_GATES = ("preflight", "full-ci", "full-release-gate", "performance")
REQUIRED_QUALIFICATION_RESULTS = (
    "full_ci",
    "full_release_gate",
    "stateful_upgrade",
    "dr_rehearsal",
    "rc_performance",
    "release_authority",
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


def _digest(value: Any, label: str) -> str:
    value = _text(value, label)
    if not _DIGEST.fullmatch(value):
        raise QualificationError(f"{label} must be a sha256 digest")
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


def _artifact_id(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise QualificationError("provisional_artifact.id must be a positive integer")
    return value


def _require_exact_keys(value: Any, expected: set[str], label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != expected:
        raise QualificationError(f"{label} has unknown or missing fields")
    return value


def _normalize_completed_results(
    needs: Mapping[str, Any], *, channel: str, current_job_id: str
) -> dict[str, str]:
    current = _text(current_job_id, "current_job_id")
    if current in REQUIRED_RESULT_JOB_IDS:
        raise QualificationError("SelfResultReference: current job cannot be a required result")
    if current != QUALIFICATION_FINALIZER_JOB_ID:
        raise QualificationError("current qualification job identity mismatch")
    if not isinstance(needs, Mapping) or set(needs) != set(REQUIRED_RESULT_JOB_IDS):
        raise QualificationError("IncompleteUpstreamResult: needs has unknown or missing jobs")

    results: dict[str, str] = {}
    for name in REQUIRED_RESULT_JOB_IDS:
        item = needs.get(name)
        result = item.get("result") if isinstance(item, Mapping) else None
        if not isinstance(result, str):
            raise QualificationError(f"IncompleteUpstreamResult: result missing: {name}")
        expected_result = "skipped" if name == "performance" and channel == "beta" else "success"
        if result != expected_result:
            raise QualificationError(f"upstream job {name} did not satisfy qualification policy")
        results[name] = result
    return results


def _qualification_results(results: Mapping[str, str], channel: str) -> dict[str, str]:
    full_gate = results["full-release-gate"]
    return {
        "full_ci": results["full-ci"],
        "full_release_gate": full_gate,
        "stateful_upgrade": full_gate,
        "dr_rehearsal": full_gate,
        "rc_performance": results["performance"],
        "release_authority": results["release-authority"],
        "rc_stable_parity": results[PRODUCER_JOB_ID] if channel == "rc" else "skipped",
    }


def _normalize_producer_observation(
    value: Mapping[str, Any], results: Mapping[str, str]
) -> dict[str, str]:
    observation = _require_exact_keys(value, {"id", "result"}, "producer_job_observation")
    job_id = _text(observation["id"], "producer_job_observation.id")
    result = _text(observation["result"], "producer_job_observation.result")
    if job_id != PRODUCER_JOB_ID or result != results[PRODUCER_JOB_ID]:
        raise QualificationError("producer observation does not match downstream direct needs")
    if result != "success":
        raise QualificationError("producer job did not complete successfully")
    return {"id": job_id, "result": result}


def _normalize_provisional_artifact(
    value: Mapping[str, Any], *, run_id: str
) -> dict[str, Any]:
    artifact = _require_exact_keys(
        value,
        {"id", "name", "api_digest", "archive_sha256"},
        "provisional_artifact",
    )
    name = _text(artifact["name"], "provisional_artifact.name")
    if name != f"candidate-materials-{run_id}":
        raise QualificationError("provisional Artifact name mismatch")
    api_digest = _digest(artifact["api_digest"], "provisional_artifact.api_digest")
    archive_sha256 = _digest(
        artifact["archive_sha256"], "provisional_artifact.archive_sha256"
    )
    if archive_sha256 != api_digest:
        raise QualificationError("provisional Artifact archive digest mismatch")
    return {
        "id": _artifact_id(artifact["id"]),
        "name": name,
        "api_digest": api_digest,
        "archive_sha256": archive_sha256,
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
    candidate_tree: str,
    upgrade_base_sha: str,
    channel: str,
    target_version: str,
    release_tag: str,
    needs: Mapping[str, Any],
    current_job_id: str,
    candidate_production_receipt_sha256: str,
    producer_job_observation: Mapping[str, Any],
    provisional_artifact: Mapping[str, Any],
    created_at: str = "1970-01-01T00:00:00Z",
    event: str = "workflow_dispatch",
    release_graph_contract: str = RELEASE_GRAPH_CONTRACT,
    release_notes_identity: str | None = None,
    release_notes_markdown_sha256: str | None = None,
) -> dict[str, Any]:
    """Build Qualification v3 only from completed downstream-visible facts."""

    normalized_channel = _text(channel, "channel").lower()
    run_identity = _run_id(run_id, "run.id")
    results = _normalize_completed_results(
        needs, channel=normalized_channel, current_job_id=current_job_id
    )
    if release_notes_identity is None or release_notes_markdown_sha256 is None:
        raise QualificationError("release note qualification binding must be complete")
    payload: dict[str, Any] = {
        "schema": QUALIFICATION_SCHEMA,
        "repository": _text(repository, "repository"),
        "workflow": {
            "name": _text(workflow_name, "workflow.name"),
            "path": _text(workflow_path, "workflow.path"),
            "ref": _text(workflow_ref, "workflow.ref"),
            "sha": _sha(workflow_sha, "workflow.sha"),
        },
        "run": {
            "id": run_identity,
            "attempt": _attempt(run_attempt),
            "event": _text(event, "run.event"),
        },
        "candidate_sha": _sha(candidate_sha, "candidate_sha"),
        "candidate_tree": _sha(candidate_tree, "candidate_tree"),
        "upgrade_base_sha": _sha(upgrade_base_sha, "upgrade_base_sha"),
        "channel": normalized_channel,
        "target_version": _text(target_version, "target_version"),
        "release_tag": _text(release_tag, "release_tag"),
        "release_graph_contract": _text(release_graph_contract, "release_graph_contract"),
        "gate_results": {name: results[name] for name in REQUIRED_GATES},
        "qualification_results": _qualification_results(results, normalized_channel),
        "candidate_production_receipt_sha256": _digest(
            candidate_production_receipt_sha256,
            "candidate_production_receipt_sha256",
        ),
        "producer_job_observation": _normalize_producer_observation(
            producer_job_observation, results
        ),
        "provisional_artifact": _normalize_provisional_artifact(
            provisional_artifact, run_id=run_identity
        ),
        "local_finalization_result": "PASS",
        "final_run_state_authority": "EXTERNAL_PHASE_B_REQUIRED",
        "release_notes": {
            "snapshot_identity": _digest(
                release_notes_identity, "release_notes.snapshot_identity"
            ),
            "markdown_sha256": _digest(
                release_notes_markdown_sha256, "release_notes.markdown_sha256"
            ),
        },
        "created_at": _text(created_at, "created_at"),
    }
    return finalize_qualification_evidence(payload)


def finalize_qualification_evidence(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Add the canonical Qualification checksum and validate the document."""

    unsigned = copy.deepcopy(dict(payload))
    unsigned.pop("qualification_sha256", None)
    validate_qualification_evidence(unsigned, require_checksum=False)
    result = dict(unsigned)
    result["qualification_sha256"] = _checksum(unsigned)
    validate_qualification_evidence(result)
    return result


def validate_qualification_evidence(
    payload: Mapping[str, Any],
    *,
    require_checksum: bool = True,
    expected: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate the v3 closed schema and its external identity bindings."""

    required = {
        "schema",
        "repository",
        "workflow",
        "run",
        "candidate_sha",
        "candidate_tree",
        "upgrade_base_sha",
        "channel",
        "target_version",
        "release_tag",
        "release_graph_contract",
        "gate_results",
        "qualification_results",
        "candidate_production_receipt_sha256",
        "producer_job_observation",
        "provisional_artifact",
        "local_finalization_result",
        "final_run_state_authority",
        "release_notes",
        "created_at",
        "qualification_sha256",
    }
    if not isinstance(payload, Mapping):
        raise QualificationError("qualification evidence must be an object")
    if payload.get("schema") != QUALIFICATION_SCHEMA:
        raise QualificationError("unsupported qualification schema")
    keys = set(payload)
    if require_checksum:
        if keys != required:
            raise QualificationError("qualification evidence has unknown or missing fields")
    elif keys != required - {"qualification_sha256"}:
        raise QualificationError("qualification evidence has unknown or missing fields")

    if payload.get("repository") != REPOSITORY:
        raise QualificationError("qualification repository mismatch")
    workflow = _require_exact_keys(
        payload.get("workflow"), {"name", "path", "ref", "sha"}, "workflow"
    )
    if workflow["name"] != RELEASE_WORKFLOW_NAME or workflow["path"] != RELEASE_WORKFLOW_PATH:
        raise QualificationError("qualification workflow mismatch")
    _text(workflow["ref"], "workflow.ref")
    _sha(workflow["sha"], "workflow.sha")

    run = _require_exact_keys(payload.get("run"), {"id", "attempt", "event"}, "run")
    _run_id(run["id"], "run.id")
    _attempt(run["attempt"])
    if run["event"] != "workflow_dispatch":
        raise QualificationError("qualification run must use workflow_dispatch")

    candidate = _sha(payload.get("candidate_sha"), "candidate_sha")
    _sha(payload.get("candidate_tree"), "candidate_tree")
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

    gates = _require_exact_keys(payload.get("gate_results"), set(REQUIRED_GATES), "gate_results")
    for name in REQUIRED_GATES:
        expected_result = "skipped" if name == "performance" and channel == "beta" else "success"
        if gates[name] != expected_result:
            raise QualificationError(f"gate {name} did not satisfy qualification policy")

    qualification_results = _require_exact_keys(
        payload.get("qualification_results"),
        set(REQUIRED_QUALIFICATION_RESULTS),
        "qualification_results",
    )
    for name in REQUIRED_QUALIFICATION_RESULTS:
        expected_result = "skipped" if name in {"rc_performance", "rc_stable_parity"} and channel == "beta" else "success"
        if qualification_results[name] != expected_result:
            raise QualificationError(f"qualification result {name} did not succeed")

    _digest(
        payload.get("candidate_production_receipt_sha256"),
        "candidate_production_receipt_sha256",
    )
    producer = _require_exact_keys(
        payload.get("producer_job_observation"),
        {"id", "result"},
        "producer_job_observation",
    )
    if producer != {"id": PRODUCER_JOB_ID, "result": "success"}:
        raise QualificationError("producer observation is invalid")
    _normalize_provisional_artifact(payload.get("provisional_artifact"), run_id=run["id"])
    if payload.get("local_finalization_result") != "PASS":
        raise QualificationError("local finalization did not pass")
    if payload.get("final_run_state_authority") != "EXTERNAL_PHASE_B_REQUIRED":
        raise QualificationError("FutureRunStateClaim: final run state authority is invalid")

    notes = _require_exact_keys(
        payload.get("release_notes"),
        {"snapshot_identity", "markdown_sha256"},
        "release_notes",
    )
    _digest(notes["snapshot_identity"], "release_notes.snapshot_identity")
    _digest(notes["markdown_sha256"], "release_notes.markdown_sha256")

    if require_checksum:
        checksum = _digest(payload.get("qualification_sha256"), "qualification_sha256")
        unsigned = copy.deepcopy(dict(payload))
        unsigned.pop("qualification_sha256", None)
        if checksum != _checksum(unsigned):
            raise QualificationError("qualification checksum mismatch")

    if expected:
        for field in (
            "repository",
            "candidate_sha",
            "candidate_tree",
            "upgrade_base_sha",
            "channel",
            "target_version",
            "release_tag",
            "release_graph_contract",
            "candidate_production_receipt_sha256",
        ):
            if field in expected and payload[field] != expected[field]:
                raise QualificationError(f"qualification {field} mismatch")
        if "qualification_run_id" in expected and run["id"] != str(expected["qualification_run_id"]):
            raise QualificationError("qualification run id mismatch")
        if (
            "qualification_run_attempt" in expected
            and run["attempt"] != expected["qualification_run_attempt"]
        ):
            raise QualificationError("qualification run attempt mismatch")
        if "workflow_ref" in expected and workflow["ref"] != expected["workflow_ref"]:
            raise QualificationError("qualification workflow ref mismatch")
        if "workflow_sha" in expected and workflow["sha"] != expected["workflow_sha"]:
            raise QualificationError("qualification workflow sha mismatch")
        if "created_at" in expected and payload["created_at"] != expected["created_at"]:
            raise QualificationError("qualification created_at mismatch")
        if "release_notes_identity" in expected and notes["snapshot_identity"] != expected["release_notes_identity"]:
            raise QualificationError("qualification release notes snapshot mismatch")
        if (
            "release_notes_markdown_sha256" in expected
            and notes["markdown_sha256"] != expected["release_notes_markdown_sha256"]
        ):
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
    """Resolve Phase B evidence from a prior completed run and final Artifact."""

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
    if not isinstance(metadata_digest, str) or not _DIGEST.fullmatch(metadata_digest):
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


def read_qualification_evidence(
    value: bytes, *, expected: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    if type(value) is not bytes:
        raise QualificationError("qualification artifact input must be bytes")
    if not value or len(value) > MAX_QUALIFICATION_ARTIFACT_BYTES:
        raise QualificationError("qualification artifact input exceeds its size authority")
    try:
        payload = json.loads(
            value.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise QualificationError("unable to decode qualification artifact") from error
    return validate_qualification_evidence(payload, expected=expected)
