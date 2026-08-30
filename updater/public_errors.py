from __future__ import annotations

import re
import secrets
from types import MappingProxyType
from typing import TypedDict


class PublicUpdaterFailure(TypedDict):
    code: str
    detail: str
    correlation_id: str


_CORRELATION_ID = re.compile(r"^[0-9a-f]{32}$")

_PUBLIC_MESSAGES = MappingProxyType({
    "updater_error": "Update request failed",
    "request_rejected": "Update request was rejected",
    "update_in_progress": "Another update operation is in progress",
    "manual_recovery_required": "Manual recovery is required",
    "invalid_operation_state": "Update operation state is invalid",
    "incompatible_release": "Release is incompatible with this installation",
    "agent_command_failed": "Update command failed",
    "agent_command_timeout": "Update command timed out",
    "agent_command_exit_failed": "Update command failed",
    "agent_command_start_failed": "Update command could not start",
    "request_too_large": "Local RPC request is too large",
    "invalid_json": "Local RPC request is invalid",
    "response_too_large": "Local RPC response is too large",
    "updater_unavailable": "Update service is unavailable",
    "updater_response_error": "Update service returned an invalid response",
    "internal_error": "Internal update service error",
    "CONFIG_APPLY_VERIFICATION_FAILED": "Configuration request failed",
    "CONFIG_CURRENT_RELEASE_INVALID": "Configuration request failed",
    "CONFIG_CURRENT_RELEASE_MISMATCH": "Configuration request failed",
    "CONFIG_CURRENT_RELEASE_UNAVAILABLE": "Configuration request failed",
    "CONFIG_DIRECT_EXPOSURE_ACCEPTANCE_REQUIRED": "Configuration request failed",
    "CONFIG_DOCTOR_ADAPTER_REQUIRED": "Configuration request failed",
    "CONFIG_INSECURE_HTTP_ACCEPTANCE_REQUIRED": "Configuration request failed",
    "CONFIG_LISTEN_INVALID": "Configuration request failed",
    "CONFIG_LISTEN_UNAVAILABLE": "Configuration request failed",
    "CONFIG_LOCATOR_MISMATCH": "Configuration request failed",
    "CONFIG_LOCATOR_UNAVAILABLE": "Configuration request failed",
    "CONFIG_OPERATION_FAILURE_CODE_INVALID": "Configuration request failed",
    "CONFIG_OPERATION_ID_INVALID": "Configuration request failed",
    "CONFIG_OPERATION_INVALID": "Configuration request failed",
    "CONFIG_OPERATION_TOO_LARGE": "Configuration request failed",
    "CONFIG_OPERATION_TRANSITION_INVALID": "Configuration request failed",
    "CONFIG_OPERATION_UNAVAILABLE": "Configuration request failed",
    "CONFIG_OPERATION_WRITE_FAILED": "Configuration request failed",
    "CONFIG_PLAN_ACCEPTANCE_REQUIRED": "Configuration request failed",
    "CONFIG_PLAN_INVALID": "Configuration request failed",
    "CONFIG_PLAN_STALE": "Configuration request failed",
    "CONFIG_PUBLIC_ORIGIN_INVALID": "Configuration request failed",
    "CONFIG_REQUEST_INVALID": "Configuration request failed",
    "CONFIG_ROLLBACK_LOCATOR_DIVERGED": "Configuration request failed",
    "CONFIG_ROLLBACK_STATE_DIVERGED": "Configuration request failed",
    "CONFIG_ROLLBACK_VERIFICATION_FAILED": "Configuration request failed",
})


def public_updater_failure(candidate_code: object | None) -> PublicUpdaterFailure:
    code = candidate_code if isinstance(candidate_code, str) else "internal_error"
    if code not in _PUBLIC_MESSAGES:
        code = "internal_error"
    return {
        "code": code,
        "detail": _PUBLIC_MESSAGES[code],
        "correlation_id": secrets.token_hex(16),
    }


def validated_public_updater_failure(value: object) -> PublicUpdaterFailure | None:
    if not isinstance(value, dict) or set(value) != {
        "code",
        "detail",
        "correlation_id",
    }:
        return None
    code = value.get("code")
    detail = value.get("detail")
    correlation_id = value.get("correlation_id")
    if (
        not isinstance(code, str)
        or code not in _PUBLIC_MESSAGES
        or detail != _PUBLIC_MESSAGES[code]
        or not isinstance(correlation_id, str)
        or _CORRELATION_ID.fullmatch(correlation_id) is None
    ):
        return None
    return {
        "code": code,
        "detail": detail,
        "correlation_id": correlation_id,
    }
