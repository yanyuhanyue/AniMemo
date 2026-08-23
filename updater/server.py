from __future__ import annotations

import json
import os
import socket
import stat
from pathlib import Path

from .errors import StateError, UpdaterError
from .redaction import redact

MAX_REQUEST_BYTES = 64 * 1024
MAX_RESPONSE_BYTES = 1024 * 1024


class UnixRpcServer:
    def __init__(self, socket_path: Path, agent, *, socket_mode: int = 0o660):
        self.socket_path = Path(os.path.abspath(socket_path))
        self.agent = agent
        self.socket_mode = socket_mode

    def _response(self, request):
        try:
            return {"ok": True, "result": self.agent.dispatch(request)}
        except UpdaterError as error:
            return {"ok": False, "error": {"code": error.code, "detail": redact(error)}}
        except Exception as error:  # noqa: BLE001 - local RPC never exposes an unframed server exception
            return {"ok": False, "error": {"code": "internal_error", "detail": redact(error)}}

    def _handle(self, connection):
        chunks = bytearray()
        while b"\n" not in chunks:
            chunk = connection.recv(min(8192, MAX_REQUEST_BYTES + 1 - len(chunks)))
            if not chunk:
                break
            chunks.extend(chunk)
            if len(chunks) > MAX_REQUEST_BYTES:
                break
        if len(chunks) > MAX_REQUEST_BYTES:
            response = {"ok": False, "error": {"code": "request_too_large", "detail": "Local RPC request exceeds 64 KiB"}}
        else:
            try:
                request = json.loads(bytes(chunks).split(b"\n", 1)[0].decode("utf-8"))
                response = self._response(request)
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
                response = {"ok": False, "error": {"code": "invalid_json", "detail": "Invalid local RPC request"}}
        encoded = json.dumps(response, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
        if len(encoded) > MAX_RESPONSE_BYTES:
            encoded = json.dumps({
                "ok": False,
                "error": {"code": "response_too_large", "detail": "Local RPC response exceeds 1 MiB"},
            }, separators=(",", ":")).encode("utf-8") + b"\n"
        connection.sendall(encoded)

    def _remove_stale_socket(self) -> None:
        try:
            existing = self.socket_path.lstat()
        except FileNotFoundError:
            return
        if self.socket_path.is_symlink() or not stat.S_ISSOCK(existing.st_mode):
            raise StateError("Updater socket path is occupied by a non-socket file")
        self.socket_path.unlink()

    def _listen(self):
        socket_parent = self.socket_path.parent
        try:
            parent_metadata = socket_parent.lstat()
        except FileNotFoundError:
            socket_parent.mkdir(mode=0o750)
            parent_metadata = socket_parent.lstat()
        if stat.S_ISLNK(parent_metadata.st_mode) or not stat.S_ISDIR(parent_metadata.st_mode):
            raise StateError("Updater socket parent must be a real directory")
        self._remove_stale_socket()
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(self.socket_path))
        os.chmod(self.socket_path, self.socket_mode)
        server.listen(8)
        return server

    def serve_once(self, *, ready=None):
        with self._listen() as server:
            if ready:
                ready.set()
            connection, _ = server.accept()
            with connection:
                self._handle(connection)
        self._remove_stale_socket()

    def serve_forever(self):
        self.agent.recover()
        with self._listen() as server:
            while True:
                connection, _ = server.accept()
                with connection:
                    self._handle(connection)
