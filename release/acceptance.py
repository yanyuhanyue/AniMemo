"""Closed RC live-acceptance Authority and stable Build Once gate."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

SCHEMA = "animemo.rc-live-acceptance/v2"
EXECUTION_SCHEMA = "animemo.rc-live-acceptance-execution-receipt/v1"
_SHA256 = re.compile(r"sha256:[0-9a-f]{64}")
_COMMIT = re.compile(r"[0-9a-f]{40}")
_RC_TAG = re.compile(
    r"v(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\."
    r"(?:0|[1-9][0-9]*)-rc\.[1-9][0-9]*"
)
_FORMAL_PROFILE_NAMES = {"fresh_base", "docker_base", "runtime_base_offline"}
_AUTHORITY_FIELDS = {
    "schema",
    "rc_tag",
    "rc_commit",
    "rc_tree",
    "release_manifest_identity",
    "deployment_contract_identity",
    "installer_materials_identity",
    "formal_windows_pretrust_kit_identity",
    "offline_release_trust_profile_identity",
    "api_digest",
    "web_digest",
    "formal_profile_authority_identities",
    "formal_authority_identity",
    "formal_rc_authority_identity",
    "formal_producer_contract_identity",
    "formal_profile_transports",
    "doctor_result",
    "upgrade_result",
}
_EXECUTION_FIELDS = {
    "schema",
    "identity",
    "acceptance_identity",
    "accepted_at",
    "observed_at",
    "operator_identity",
    "run_id",
    "run_attempt",
    "correlation_id",
    "current_workflow_commit",
    "execution_environment",
    "tool_identity",
    "formal_record_input_digest",
    "formal_profile_receipt_digests",
    "formal_aggregate_receipt_digest",
    "formal_execution_receipt_digest",
    "publication_execution_receipt_identity",
    "publication_signed_claim_identity",
    "publication_signed_at",
}
_FIELDS = _AUTHORITY_FIELDS | {"identity", "execution_receipt", "formal_evidence"}
_STABLE_PROMOTION_FIELDS = {
    "schema",
    "identity",
    "acceptance_identity",
    "rc_tag",
    "stable_commit",
    "stable_api_digest",
    "stable_web_digest",
    "rebuild_allowed",
    "status",
}


class AcceptanceError(ValueError):
    """An RC acceptance record cannot authorize stable promotion."""


def _canonical_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def _identity(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _digest(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise AcceptanceError(f"{field} must be a SHA-256 identity")
    return value


def _git_identity(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _COMMIT.fullmatch(value):
        raise AcceptanceError(f"{field} must be an exact Git object identity")
    return value


def _safe_identity(value: Any, field: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > 200
        or any(ord(character) < 32 for character in value)
    ):
        raise AcceptanceError(f"{field} must be a bounded safe identity")
    return value


def _optional_safe_identity(value: Any, field: str) -> str | None:
    if value is None:
        return None
    return _safe_identity(value, field)


def _canonical_timestamp(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise AcceptanceError(f"{field} must be an RFC3339 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise AcceptanceError(f"{field} must be an RFC3339 timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise AcceptanceError(f"{field} must include a timezone")
    if parsed.microsecond != 0:
        raise AcceptanceError(f"{field} must use fixed whole-second precision")
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _formal_profile_identities(value: Any) -> dict[str, str]:
    if not isinstance(value, Mapping) or set(value) != _FORMAL_PROFILE_NAMES:
        raise AcceptanceError("formal profile Authority identities are not closed")
    return {
        name: _digest(value[name], f"formal_profile_authority_identities.{name}")
        for name in sorted(_FORMAL_PROFILE_NAMES)
    }


def _formal_profile_transports(value: Any) -> dict[str, str]:
    expected = {
        "fresh_base": "github",
        "docker_base": "github",
        "runtime_base_offline": "local-bundle",
    }
    if not isinstance(value, Mapping) or dict(value) != expected:
        raise AcceptanceError("formal profile transports are not closed")
    return expected


def _validated_formal_evidence(value: Any) -> dict[str, Any]:
    try:
        from .formal_vm_controller import validate_formal_acceptance_bundle

        closed = validate_formal_acceptance_bundle(value)
    except (ImportError, RuntimeError, ValueError) as error:
        raise AcceptanceError("formal acceptance evidence is invalid") from error
    return {
        key: copy.deepcopy(closed[key])
        for key in (
            "rcLiveAcceptanceInput",
            "profileReceipts",
            "aggregateReceipt",
            "executionReceipt",
        )
    }


def _build_from_formal_evidence(formal_evidence: dict[str, Any]) -> dict[str, Any]:
    acceptance_input = formal_evidence["rcLiveAcceptanceInput"]
    profiles = formal_evidence["profileReceipts"]
    aggregate = formal_evidence["aggregateReceipt"]
    formal_execution = formal_evidence["executionReceipt"]
    rc_tag = acceptance_input["rc_tag"]
    rc_commit = acceptance_input["source_sha"]
    rc_tree = acceptance_input["source_tree"]
    release_manifest_identity = acceptance_input["release_manifest_identity"]
    deployment_contract_identity = acceptance_input["deployment_contract_identity"]
    installer_materials_identity = acceptance_input["installer_materials_identity"]
    api_digest = acceptance_input["api_digest"]
    web_digest = acceptance_input["web_digest"]
    formal_profile_authority_identities = {
        "fresh_base": profiles["FORMAL_FRESH"]["profile_authority_identity"],
        "docker_base": profiles["FORMAL_DOCKER"]["profile_authority_identity"],
        "runtime_base_offline": profiles["FORMAL_OFFLINE"][
            "profile_authority_identity"
        ],
    }
    formal_authority_identity = aggregate["formal_authority_identity"]
    formal_profile_transports = {
        "fresh_base": profiles["FORMAL_FRESH"]["transport_source"],
        "docker_base": profiles["FORMAL_DOCKER"]["transport_source"],
        "runtime_base_offline": profiles["FORMAL_OFFLINE"]["transport_source"],
    }
    doctor_result = "PASS"
    upgrade_result = "NOT_APPLICABLE"
    accepted_at = formal_execution["accepted_at"]
    observed_at = formal_execution["observed_at"]
    operator_identity = formal_execution["operator_identity"]
    tool_identity = formal_execution["tool_identity"]
    run_id = formal_execution["run_id"]
    run_attempt = formal_execution["run_attempt"]
    correlation_id = formal_execution["correlation_id"]
    current_workflow_commit = formal_execution["current_workflow_commit"]
    execution_environment = formal_execution["execution_environment"]

    if not isinstance(rc_tag, str) or not _RC_TAG.fullmatch(rc_tag):
        raise AcceptanceError("rc_tag is invalid")
    if doctor_result != "PASS":
        raise AcceptanceError("Doctor must pass before RC live acceptance")
    if upgrade_result not in {"PASS", "NOT_APPLICABLE"}:
        raise AcceptanceError("upgrade_result is invalid")
    if run_attempt is not None and (type(run_attempt) is not int or run_attempt < 1):
        raise AcceptanceError("run_attempt must be a positive integer or null")

    authority: dict[str, Any] = {
        "schema": SCHEMA,
        "rc_tag": rc_tag,
        "rc_commit": _git_identity(rc_commit, "rc_commit"),
        "rc_tree": _git_identity(rc_tree, "rc_tree"),
        "release_manifest_identity": _digest(
            release_manifest_identity, "release_manifest_identity"
        ),
        "deployment_contract_identity": _digest(
            deployment_contract_identity, "deployment_contract_identity"
        ),
        "installer_materials_identity": _digest(
            installer_materials_identity, "installer_materials_identity"
        ),
        "formal_windows_pretrust_kit_identity": _digest(
            acceptance_input["formal_windows_pretrust_kit_identity"],
            "formal_windows_pretrust_kit_identity",
        ),
        "offline_release_trust_profile_identity": _digest(
            acceptance_input["offline_release_trust_profile_identity"],
            "offline_release_trust_profile_identity",
        ),
        "api_digest": _digest(api_digest, "api_digest"),
        "web_digest": _digest(web_digest, "web_digest"),
        "formal_profile_authority_identities": _formal_profile_identities(
            formal_profile_authority_identities
        ),
        "formal_authority_identity": _digest(
            formal_authority_identity, "formal_authority_identity"
        ),
        "formal_rc_authority_identity": _digest(
            acceptance_input["rc_authority_identity"],
            "formal_rc_authority_identity",
        ),
        "formal_producer_contract_identity": _digest(
            acceptance_input["producer_contract_identity"],
            "formal_producer_contract_identity",
        ),
        "formal_profile_transports": _formal_profile_transports(
            formal_profile_transports
        ),
        "doctor_result": doctor_result,
        "upgrade_result": upgrade_result,
    }
    authority_identity = _identity(authority)
    canonical_accepted_at = _canonical_timestamp(accepted_at, "accepted_at")
    receipt_unsigned: dict[str, Any] = {
        "schema": EXECUTION_SCHEMA,
        "acceptance_identity": authority_identity,
        "accepted_at": canonical_accepted_at,
        "observed_at": _canonical_timestamp(observed_at, "observed_at"),
        "operator_identity": _safe_identity(operator_identity, "operator_identity"),
        "run_id": _optional_safe_identity(run_id, "run_id"),
        "run_attempt": run_attempt,
        "correlation_id": _optional_safe_identity(correlation_id, "correlation_id"),
        "current_workflow_commit": (
            None
            if current_workflow_commit is None
            else _git_identity(current_workflow_commit, "current_workflow_commit")
        ),
        "execution_environment": _optional_safe_identity(
            execution_environment, "execution_environment"
        ),
        "tool_identity": _digest(tool_identity, "tool_identity"),
        "formal_record_input_digest": _digest(
            acceptance_input["record_input_digest"],
            "formal_record_input_digest",
        ),
        "formal_profile_receipt_digests": {
            name: _digest(value, f"formal_profile_receipt_digests.{name}")
            for name, value in sorted(
                acceptance_input["formal_profile_receipt_digests"].items()
            )
        },
        "formal_aggregate_receipt_digest": _digest(
            aggregate["receipt_digest"], "formal_aggregate_receipt_digest"
        ),
        "formal_execution_receipt_digest": _digest(
            formal_execution["receipt_digest"],
            "formal_execution_receipt_digest",
        ),
        "publication_execution_receipt_identity": _digest(
            profiles["FORMAL_FRESH"][
                "publication_execution_receipt_identity"
            ],
            "publication_execution_receipt_identity",
        ),
        "publication_signed_claim_identity": _digest(
            profiles["FORMAL_FRESH"]["publication_signed_claim_identity"],
            "publication_signed_claim_identity",
        ),
        "publication_signed_at": _canonical_timestamp(
            profiles["FORMAL_FRESH"]["publication_signed_at"],
            "publication_signed_at",
        ),
    }
    receipt = {**receipt_unsigned, "identity": _identity(receipt_unsigned)}
    return {
        **authority,
        "identity": authority_identity,
        "formal_evidence": copy.deepcopy(formal_evidence),
        "execution_receipt": receipt,
    }


def build_rc_live_acceptance(*, formal_evidence: Mapping[str, Any]) -> dict[str, Any]:
    """Build acceptance only from the closed production Formal evidence bundle."""

    return _build_from_formal_evidence(_validated_formal_evidence(formal_evidence))


def validate_rc_live_acceptance(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _FIELDS:
        raise AcceptanceError("RC live acceptance record has unknown or missing fields")
    receipt = value.get("execution_receipt")
    if not isinstance(receipt, Mapping) or set(receipt) != _EXECUTION_FIELDS:
        raise AcceptanceError("RC live acceptance execution receipt is not closed")
    rebuilt = build_rc_live_acceptance(
        formal_evidence=value.get("formal_evidence"),
    )
    if value.get("schema") != SCHEMA or dict(value) != rebuilt:
        raise AcceptanceError("RC live acceptance record identity mismatch")
    return copy.deepcopy(rebuilt)


def verify_stable_promotion_acceptance(
    record: Mapping[str, Any] | None,
    *,
    expected: Mapping[str, str],
    stable_commit: str,
    stable_api_digest: str,
    stable_web_digest: str,
) -> dict[str, Any]:
    accepted = validate_rc_live_acceptance(record)
    fields = {
        "rc_tag",
        "rc_commit",
        "release_manifest_identity",
        "deployment_contract_identity",
        "installer_materials_identity",
        "api_digest",
        "web_digest",
    }
    if not isinstance(expected, Mapping) or set(expected) != fields:
        raise AcceptanceError("stable promotion expected identity tuple is not closed")
    for field in fields:
        if accepted[field] != expected[field]:
            raise AcceptanceError(f"acceptance record does not bind expected {field}")
    if stable_commit != accepted["rc_commit"]:
        raise AcceptanceError("Stable commit differs from accepted RC commit")
    if stable_api_digest != accepted["api_digest"]:
        raise AcceptanceError("Stable API digest differs from accepted RC digest")
    if stable_web_digest != accepted["web_digest"]:
        raise AcceptanceError("Stable Web digest differs from accepted RC digest")
    unsigned = {
        "schema": "animemo.stable-promotion-acceptance/v1",
        "acceptance_identity": accepted["identity"],
        "rc_tag": accepted["rc_tag"],
        "stable_commit": stable_commit,
        "stable_api_digest": stable_api_digest,
        "stable_web_digest": stable_web_digest,
        "rebuild_allowed": False,
        "status": "AUTHORIZED",
    }
    return {**unsigned, "identity": _identity(unsigned)}


def validate_stable_promotion_acceptance(
    value: Mapping[str, Any] | None,
    *,
    acceptance: Mapping[str, Any],
) -> dict[str, Any]:
    accepted = validate_rc_live_acceptance(acceptance)
    if not isinstance(value, Mapping) or set(value) != _STABLE_PROMOTION_FIELDS:
        raise AcceptanceError(
            "Stable promotion acceptance has unknown or missing fields"
        )
    expected = verify_stable_promotion_acceptance(
        accepted,
        expected={
            field: accepted[field]
            for field in {
                "rc_tag",
                "rc_commit",
                "release_manifest_identity",
                "deployment_contract_identity",
                "installer_materials_identity",
                "api_digest",
                "web_digest",
            }
        },
        stable_commit=value.get("stable_commit"),
        stable_api_digest=value.get("stable_api_digest"),
        stable_web_digest=value.get("stable_web_digest"),
    )
    if dict(value) != expected:
        raise AcceptanceError("Stable promotion acceptance identity mismatch")
    return copy.deepcopy(expected)
