from __future__ import annotations

import json
import re
import socket
from pathlib import Path
from types import MappingProxyType

from django.conf import settings

from .update_public_results import UpdateSuccessResultError, project_update_success

MAX_RESPONSE_BYTES = 1024 * 1024
_CORRELATION_ID = re.compile(r"^[0-9a-f]{32}$")
_REMOTE_ERROR_DETAILS = MappingProxyType({
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


def _validated_remote_failure(value):
    if not isinstance(value, dict) or set(value) != {"code", "detail", "correlation_id"}:
        return None
    code = value.get("code")
    detail = value.get("detail")
    correlation_id = value.get("correlation_id")
    if (
        not isinstance(code, str)
        or code not in _REMOTE_ERROR_DETAILS
        or detail != _REMOTE_ERROR_DETAILS[code]
        or not isinstance(correlation_id, str)
        or _CORRELATION_ID.fullmatch(correlation_id) is None
    ):
        return None
    return {"code": code, "detail": detail, "correlation_id": correlation_id}


class AgentUnavailable(RuntimeError):
    pass


class AgentResponseError(RuntimeError):
    def __init__(self, detail, *, remote_code="updater_error", correlation_id=None):
        super().__init__(detail)
        self.remote_code = remote_code
        self.correlation_id = correlation_id


class UpdateAgentClient:
    """Small Django-side adapter; it exposes no Docker or filesystem control."""

    def __init__(self, socket_path=None, *, timeout=None):
        self.socket_path = Path(socket_path or settings.ANIMEMO_UPDATER_SOCKET)
        self.timeout = float(timeout or settings.ANIMEMO_UPDATER_TIMEOUT_SECONDS)

    def request(self, operation, params=None):
        request_params = {} if params is None else params
        payload = json.dumps(
            {"operation": operation, "params": request_params},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8") + b"\n"
        response = bytearray()
        socket_family = getattr(socket, "AF_UNIX", None)
        if socket_family is None:
            raise AgentUnavailable("AniMemo Update Agent requires Unix Socket support")
        try:
            with socket.socket(socket_family, socket.SOCK_STREAM) as connection:
                connection.settimeout(self.timeout)
                connection.connect(str(self.socket_path))
                connection.sendall(payload)
                while b"\n" not in response:
                    chunk = connection.recv(8192)
                    if not chunk:
                        break
                    response.extend(chunk)
                    if len(response) > MAX_RESPONSE_BYTES:
                        raise AgentUnavailable("AniMemo Update Agent response is too large")
        except (OSError, TimeoutError) as error:
            raise AgentUnavailable("AniMemo Update Agent is unavailable") from error
        try:
            decoded = json.loads(bytes(response).split(b"\n", 1)[0].decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, IndexError) as error:
            raise AgentUnavailable("AniMemo Update Agent returned an invalid response") from error
        if not isinstance(decoded, dict) or not isinstance(decoded.get("ok"), bool):
            raise AgentUnavailable("AniMemo Update Agent returned an invalid response")
        if not decoded["ok"]:
            if set(decoded) != {"ok", "error"}:
                raise AgentUnavailable("AniMemo Update Agent returned an invalid response")
            remote = _validated_remote_failure(decoded.get("error"))
            if remote is None:
                raise AgentUnavailable("AniMemo Update Agent returned an invalid response")
            raise AgentResponseError(
                remote["detail"],
                remote_code=remote["code"],
                correlation_id=remote["correlation_id"],
            )
        if set(decoded) != {"ok", "result"}:
            raise AgentUnavailable("AniMemo Update Agent returned an invalid response")
        try:
            return project_update_success(
                operation, decoded["result"], request_params
            )
        except UpdateSuccessResultError as error:
            raise AgentUnavailable(
                "AniMemo Update Agent returned an invalid response"
            ) from error
