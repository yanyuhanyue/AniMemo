from __future__ import annotations

import json
import socket
from pathlib import Path

from .errors import UpdaterError
from .public_errors import validated_public_updater_failure

MAX_RESPONSE_BYTES = 1024 * 1024


class AgentUnavailable(UpdaterError):
    code = "updater_unavailable"


class AgentResponseError(UpdaterError):
    code = "updater_response_error"

    def __init__(self, detail, *, remote_code="updater_error", correlation_id=None):
        super().__init__(detail)
        self.remote_code = remote_code
        self.correlation_id = correlation_id


class UnixAgentClient:
    def __init__(self, socket_path: Path, *, timeout: float = 10.0):
        self.socket_path = socket_path
        self.timeout = timeout

    def request(self, operation: str, params: dict[str, object] | None = None):
        payload = json.dumps({"operation": operation, "params": params or {}}, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
        response = bytearray()
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.settimeout(self.timeout)
                connection.connect(str(self.socket_path))
                connection.sendall(payload)
                while b"\n" not in response:
                    chunk = connection.recv(min(8192, MAX_RESPONSE_BYTES + 1 - len(response)))
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
            raise AgentResponseError(
                "Update service returned an invalid response",
                remote_code="updater_response_error",
            )
        if not decoded["ok"]:
            if set(decoded) != {"ok", "error"}:
                raise AgentResponseError(
                    "Update service returned an invalid response",
                    remote_code="updater_response_error",
                )
            remote = validated_public_updater_failure(decoded.get("error"))
            if remote is None:
                raise AgentResponseError(
                    "Update service returned an invalid response",
                    remote_code="updater_response_error",
                )
            raise AgentResponseError(
                remote["detail"],
                remote_code=remote["code"],
                correlation_id=remote["correlation_id"],
            )
        if set(decoded) != {"ok", "result"}:
            raise AgentResponseError(
                "Update service returned an invalid response",
                remote_code="updater_response_error",
            )
        return decoded["result"]
