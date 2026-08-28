from __future__ import annotations

import json
import os
import socket
import tempfile
import threading
import unittest
from contextlib import AbstractContextManager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from updater.client import MAX_RESPONSE_BYTES, AgentUnavailable, UnixAgentClient
from updater.errors import OperationInProgress, StateError
from updater.protocol import validate_request
from updater.server import MAX_REQUEST_BYTES, UnixRpcServer


class EchoAgent:
    def dispatch(self, request):
        return {"echo": request["operation"]}


class ValidatingAgent:
    def dispatch(self, request):
        return validate_request(request)


class _StopServing(RuntimeError):
    pass


class _ScriptedListener:
    def __init__(self, events):
        self.events = events

    def accept(self):
        self.events.append("accept")
        raise _StopServing


class _ScriptedListenerContext(AbstractContextManager):
    def __init__(self, listener, events):
        self.listener = listener
        self.events = events

    def __enter__(self):
        self.events.append("listen")
        return self.listener

    def __exit__(self, exc_type, exc, traceback):
        return False


class _ContendedRecoveryAgent:
    def __init__(self, events):
        self.events = events
        self.attempts = 0

    def recover(self):
        self.attempts += 1
        self.events.append("recover")
        if self.attempts == 1:
            raise OperationInProgress("installer still owns the global lock")
        return []


class UnixRpcTests(unittest.TestCase):
    def test_forever_server_listens_before_retrying_contended_recovery(self):
        events = []
        agent = _ContendedRecoveryAgent(events)
        server = UnixRpcServer(Path("/run/animemo-updater.sock"), agent)
        listener = _ScriptedListener(events)
        context = _ScriptedListenerContext(listener, events)

        with (
            patch.object(server, "_listen", return_value=context),
            patch(
                "updater.server.time.sleep",
                side_effect=lambda _seconds: events.append("sleep"),
            ),
            self.assertRaises(_StopServing),
        ):
            server.serve_forever()

        self.assertEqual(
            events,
            ["listen", "recover", "sleep", "recover", "accept"],
        )

    def test_local_bundle_pair_is_accepted_and_missing_pair_is_rejected_over_rpc(self):
        with tempfile.TemporaryDirectory() as directory:
            server = UnixRpcServer(
                Path(directory) / "updater.sock",
                ValidatingAgent(),
            )

            request = {
                "operation": "plan_update",
                "params": {
                    "version": "v1.0.0",
                    "source": "local-bundle",
                    "bundlePayload": "/media/payload.tar",
                    "releaseAttestation": "/media/release-attestation.json",
                },
            }
            accepted = server._response(request)
            rejected = server._response(
                {
                    "operation": "plan_update",
                    "params": {"version": "v1.0.0", "source": "local-bundle"},
                }
            )

            self.assertTrue(accepted["ok"])
            self.assertEqual(accepted["result"], request)
            self.assertFalse(rejected["ok"])
            self.assertEqual(rejected["error"]["code"], "request_rejected")

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

    def test_socket_parent_symlink_is_rejected_without_writing_outside(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            outside = root / "outside"
            outside.mkdir()
            socket_parent = root / "instance"
            try:
                socket_parent.symlink_to(outside, target_is_directory=True)
            except OSError as error:
                self.skipTest(f"directory symlinks unavailable: {error}")
            server = UnixRpcServer(socket_parent / "updater.sock", EchoAgent())
            fake_socket = SimpleNamespace(
                AF_UNIX=1,
                SOCK_STREAM=1,
                socket=lambda *_args: (_ for _ in ()).throw(AssertionError("socket must not be opened")),
            )

            with patch("updater.server.socket", fake_socket), self.assertRaisesRegex(StateError, "parent"):
                server.serve_once()

            self.assertFalse((outside / "updater.sock").exists())

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
