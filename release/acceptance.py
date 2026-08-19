"""Closed RC live-acceptance record and stable Build Once gate."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from collections.abc import Mapping
from typing import Any

SCHEMA = "animemo.rc-live-acceptance/v1"
_SHA256 = re.compile(r"sha256:[0-9a-f]{64}")
_COMMIT = re.compile(r"[0-9a-f]{40}")
_RC_TAG = re.compile(r"v[0-9]+\.[0-9]+\.[0-9]+-rc\.(?:[1-9][0-9]*|TEST)")
_TIMESTAMP = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")
_FIELDS = {
    "schema",
    "identity",
    "rc_tag",
    "rc_commit",
    "release_manifest_identity",
    "deployment_contract_identity",
    "installer_materials_identity",
    "api_digest",
    "web_digest",
    "fresh_base_identity",
    "docker_base_identity",
    "runtime_base_identity",
    "install_path",
    "doctor_result",
    "upgrade_result",
    "accepted_at",
    "operator_identity",
    "tool_identity",
}


class AcceptanceError(ValueError):
    """An RC acceptance record cannot authorize stable promotion."""


def _canonical_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True)
        + "\n"
    ).encode("utf-8")


def _identity(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _digest(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise AcceptanceError(f"{field} must be a SHA-256 identity")
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


def build_rc_live_acceptance(
    *,
    rc_tag: str,
    rc_commit: str,
    release_manifest_identity: str,
    deployment_contract_identity: str,
    installer_materials_identity: str,
    api_digest: str,
    web_digest: str,
    fresh_base_identity: str,
    docker_base_identity: str,
    runtime_base_identity: str,
    install_path: str,
    doctor_result: str,
    upgrade_result: str,
    accepted_at: str,
    operator_identity: str,
    tool_identity: str,
) -> dict[str, Any]:
    if not isinstance(rc_tag, str) or not _RC_TAG.fullmatch(rc_tag):
        raise AcceptanceError("rc_tag is invalid")
    if not isinstance(rc_commit, str) or not _COMMIT.fullmatch(rc_commit):
        raise AcceptanceError("rc_commit is invalid")
    if install_path not in {"github", "official-mirror"}:
        raise AcceptanceError("install_path must be an explicit authorized transport")
    if doctor_result != "PASS":
        raise AcceptanceError("Doctor must pass before RC live acceptance")
    if upgrade_result not in {"PASS", "NOT_APPLICABLE"}:
        raise AcceptanceError("upgrade_result is invalid")
    if not isinstance(accepted_at, str) or not _TIMESTAMP.fullmatch(accepted_at):
        raise AcceptanceError("accepted_at must be an RFC3339 UTC timestamp")
    unsigned: dict[str, Any] = {
        "schema": SCHEMA,
        "rc_tag": rc_tag,
        "rc_commit": rc_commit,
        "release_manifest_identity": _digest(
            release_manifest_identity, "release_manifest_identity"
        ),
        "deployment_contract_identity": _digest(
            deployment_contract_identity, "deployment_contract_identity"
        ),
        "installer_materials_identity": _digest(
            installer_materials_identity, "installer_materials_identity"
        ),
        "api_digest": _digest(api_digest, "api_digest"),
        "web_digest": _digest(web_digest, "web_digest"),
        "fresh_base_identity": _digest(fresh_base_identity, "fresh_base_identity"),
        "docker_base_identity": _digest(docker_base_identity, "docker_base_identity"),
        "runtime_base_identity": _digest(runtime_base_identity, "runtime_base_identity"),
        "install_path": install_path,
        "doctor_result": doctor_result,
        "upgrade_result": upgrade_result,
        "accepted_at": accepted_at,
        "operator_identity": _safe_identity(operator_identity, "operator_identity"),
        "tool_identity": _digest(tool_identity, "tool_identity"),
    }
    return {**unsigned, "identity": _identity(unsigned)}


def validate_rc_live_acceptance(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _FIELDS:
        raise AcceptanceError("RC live acceptance record has unknown or missing fields")
    rebuilt = build_rc_live_acceptance(
        **{field: value[field] for field in _FIELDS - {"schema", "identity"}}
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
