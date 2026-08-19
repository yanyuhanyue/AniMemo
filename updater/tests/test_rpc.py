from __future__ import annotations

import json
import os
import socket
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from updater.client import MAX_RESPONSE_BYTES, AgentUnavailable, UnixAgentClient
from updater.errors import StateError
from updater.protocol import validate_request
from updater.server import MAX_REQUEST_BYTES, UnixRpcServer


class EchoAgent:
    def dispatch(self, request):
        return {"echo": request["operation"]}


class ValidatingAgent:
    def dispatch(self, request):
        return validate_request(request)


class UnixRpcTests(unittest.TestCase):
    def test_local_bundle_blocker_code_is_preserved_over_rpc(self):
        with tempfile.TemporaryDirectory() as directory:
            server = UnixRpcServer(
                Path(directory) / "updater.sock",
                ValidatingAgent(),
            )

            response = server._response(
                {
                    "operation": "plan_update",
                    "params": {"version": "v1.0.0", "source": "local-bundle"},
                }
            )

            self.assertFalse(response["ok"])
            self.assertEqual(
                response["error"]["code"],
                "BLOCKED_PORTABLE_PUBLICATION_AUTHORITY",
            )

    def test_server_refuses_to_delete_a_regular_file_at_the_socket_path(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "updater.sock"
            path.write_text("DO_NOT_DELETE\n", encoding="utf-8")
            server = UnixRpcServer(path, EchoAgent())
            fake_socket = SimpleNamespace(
                AF_UNIX=1,
                SOCK_STREAM=1,
                socket=lambda *_args: (_ for _ in ()).throw(AssertionError("socket must not be opened")),
            )

            with patch("updater.server.socket", fake_socket), self.assertRaisesRegex(StateError, "socket path"):
                server.serve_once()

            self.assertEqual(path.read_text(encoding="utf-8"), "DO_NOT_DELETE\n")

    def test_one_json_request_round_trips_over_unix_socket(self):
        if not hasattr(socket, "AF_UNIX"):
            self.skipTest("AF_UNIX unavailable")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "updater.sock"
            server = UnixRpcServer(path, EchoAgent())
            ready = threading.Event()
            thread = threading.Thread(target=server.serve_once, kwargs={"ready": ready}, daemon=True)
            thread.start()
            self.assertTrue(ready.wait(5))
            if os.name != "nt":
                self.assertEqual(path.stat().st_mode & 0o777, 0o660)

            response = UnixAgentClient(path).request("get_status")

            thread.join(5)
            self.assertEqual(response, {"echo": "get_status"})
            self.assertFalse(thread.is_alive())

    def test_oversized_request_is_rejected_without_dispatch(self):
        if not hasattr(socket, "AF_UNIX"):
            self.skipTest("AF_UNIX unavailable")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "updater.sock"
            agent = EchoAgent()
            server = UnixRpcServer(path, agent)
            ready = threading.Event()
            thread = threading.Thread(target=server.serve_once, kwargs={"ready": ready}, daemon=True)
            thread.start()
            self.assertTrue(ready.wait(5))
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.connect(str(path))
                connection.sendall(b"x" * (MAX_REQUEST_BYTES + 1) + b"\n")
                response = json.loads(connection.recv(4096).split(b"\n", 1)[0])
            thread.join(5)
            self.assertEqual(response["error"]["code"], "request_too_large")

    def test_client_rejects_oversized_agent_response(self):
        if not hasattr(socket, "AF_UNIX"):
            self.skipTest("AF_UNIX unavailable")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "updater.sock"
            ready = threading.Event()

            def serve():
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
                    server.bind(str(path))
                    server.listen(1)
                    ready.set()
                    connection, _ = server.accept()
                    with connection:
                        connection.recv(4096)
                        connection.sendall(b"x" * (MAX_RESPONSE_BYTES + 1) + b"\n")

            thread = threading.Thread(target=serve, daemon=True)
            thread.start()
            self.assertTrue(ready.wait(5))
            with self.assertRaisesRegex(AgentUnavailable, "too large"):
                UnixAgentClient(path).request("get_status")
            thread.join(5)


if __name__ == "__main__":
    unittest.main()
