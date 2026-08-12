from __future__ import annotations

import json
import socket
from pathlib import Path

from django.conf import settings


class AgentUnavailable(RuntimeError):
    pass


class AgentResponseError(RuntimeError):
    def __init__(self, detail, *, remote_code="updater_error"):
        super().__init__(detail)
        self.remote_code = remote_code


class UpdateAgentClient:
    """Small Django-side adapter; it exposes no Docker or filesystem control."""

    def __init__(self, socket_path=None, *, timeout=None):
        self.socket_path = Path(socket_path or settings.ANIMEMO_UPDATER_SOCKET)
        self.timeout = float(timeout or settings.ANIMEMO_UPDATER_TIMEOUT_SECONDS)

    def request(self, operation, params=None):
        payload = json.dumps(
            {"operation": operation, "params": params or {}},
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
                    if len(response) > 1024 * 1024:
                        raise AgentUnavailable("AniMemo Update Agent response is too large")
        except (OSError, TimeoutError) as error:
            raise AgentUnavailable("AniMemo Update Agent is unavailable") from error
        try:
            decoded = json.loads(bytes(response).split(b"\n", 1)[0].decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, IndexError) as error:
            raise AgentUnavailable("AniMemo Update Agent returned an invalid response") from error
        if not decoded.get("ok"):
            remote = decoded.get("error") or {}
            raise AgentResponseError(
                str(remote.get("detail") or "Update Agent request failed"),
                remote_code=str(remote.get("code") or "updater_error"),
            )
        return decoded.get("result")
