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

from updater.client import (
    MAX_RESPONSE_BYTES,
    AgentResponseError,
    AgentUnavailable,
    UnixAgentClient,
)
from updater.errors import OperationInProgress, RequestRejected, StateError
from updater.protocol import validate_request
from updater.public_errors import public_updater_failure
from updater.server import MAX_REQUEST_BYTES, UnixRpcServer


class EchoAgent:
    def dispatch(self, request):
        if request["operation"] == "get_status":
            return _valid_status_result()
        raise AssertionError("unexpected operation")


class ValidatingAgent:
    def dispatch(self, request):
        validate_request(request)
        params = request["params"]
        return _valid_plan_result(
            version=params["version"], source=params.get("source", "github")
        )


class _StaticAgent:
    def __init__(self, result):
        self.result = result

    def dispatch(self, _request):
        return self.result


class _FailingAgent:
    def __init__(self, error):
        self.error = error

    def dispatch(self, _request):
        raise self.error


def _valid_identity(version="v1.0.0", channel=None):
    if channel is None:
        channel = "rc" if "-rc." in version else "beta" if "-beta." in version else "stable"
    return {
        "version": version,
        "channel": channel,
        "commit": "1" * 40,
        "apiDigest": "sha256:" + "2" * 64,
        "webDigest": "sha256:" + "3" * 64,
    }


def _valid_compatibility():
    return {
        "allowed": True,
        "decision": "safe_switch",
        "rollbackMode": "safe",
        "migrationRequired": False,
        "migrationPolicy": "none",
        "reasons": [],
    }


def _valid_plan_result(version="v1.0.1", source="github"):
    result = {
        "planId": "a" * 32,
        "expiresAt": "2026-08-30T12:00:00Z",
        "from": _valid_identity(),
        "to": _valid_identity(version),
        "compatibility": _valid_compatibility(),
        "affectedServices": ["api", "web"],
        "databaseRollback": False,
        "source": source,
        "transportPolicyIdentity": "4" * 64,
        "verifiedReleaseIdentity": "sha256:" + "5" * 64,
    }
    if source == "local-bundle":
        result.update(
            {
                "transportIdentity": "sha256:" + "6" * 64,
                "payloadIdentity": "sha256:" + "7" * 64,
                "releaseAttestationIdentity": "sha256:" + "8" * 64,
                "releaseExecutionReceipt": {
                    "schema": "animemo.release-execution-receipt/v1",
                    "publicationIdentity": "sha256:" + "9" * 64,
                    "publicationExecutionReceiptIdentity": "sha256:" + "a" * 64,
                    "signedClaimIdentity": "sha256:" + "b" * 64,
                    "signedAt": "2026-08-30T12:00:00Z",
                    "identity": "sha256:" + "c" * 64,
                },
                "trustProfileVersion": 1,
                "trustProfileIdentity": "sha256:" + "d" * 64,
                "manifestIdentity": "sha256:" + "e" * 64,
                "deploymentContractIdentity": "sha256:" + "f" * 64,
                "apiDigest": "sha256:" + "0" * 64,
                "webDigest": "sha256:" + "1" * 64,
                "postgresDigest": "sha256:" + "2" * 64,
                "redisDigest": "sha256:" + "3" * 64,
            }
        )
    return result


def _valid_operation_result(
    detail="Operation created", *, identifier="b" * 32, kind="apply_update"
):
    return {
        "id": identifier,
        "kind": kind,
        "status": "idle",
        "createdAt": "2026-08-30T12:00:00Z",
        "updatedAt": "2026-08-30T12:00:00Z",
        "events": [
            {
                "status": "idle",
                "at": "2026-08-30T12:00:00Z",
                "detail": detail,
            }
        ],
    }


def _valid_release(version="v1.0.1", channel=None):
    if channel is None:
        channel = "rc" if "-rc." in version else "beta" if "-beta." in version else "stable"
    return {
        "version": version,
        "channel": channel,
        "publishedAt": "2026-08-30T12:00:00Z",
        "compatibility": _valid_compatibility(),
    }


def _event(status, at):
    details = {
        "idle": "Operation created",
        "preflight": "Preflight checks in progress",
        "fetching": "Release acquisition in progress",
        "verifying": "Release verification in progress",
        "succeeded": "Update completed",
    }
    return {"status": status, "at": at, "detail": details[status]}


def _valid_status_result():
    return {
        "updaterVersion": "1.1.0",
        "current": _valid_identity(),
        "previous": None,
        "previousCompatibility": None,
        "runtime": {
            "databaseContract": "animemo-db-v1",
            "configurationContract": "animemo-config-v1",
            "enabledPluginApis": [2],
        },
        "recoveryBlock": None,
        "operation": None,
        "history": [],
    }


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
    def assert_public_failure(self, value, expected_code):
        self.assertEqual(set(value), {"code", "detail", "correlation_id"})
        self.assertEqual(value["code"], expected_code)
        self.assertRegex(value["correlation_id"], r"^[0-9a-f]{32}$")
        self.assertIsInstance(value["detail"], str)

    def _serve_payload(self, path, ready, payload):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
            server.bind(str(path))
            server.listen(1)
            ready.set()
            connection, _ = server.accept()
            with connection:
                connection.recv(4096)
                connection.sendall(
                    json.dumps(payload, separators=(",", ":")).encode("utf-8")
                    + b"\n"
                )

    def test_server_error_contract_never_exposes_exception_text(self):
        sentinels = (
            "TOKEN_CANARY_7f4e",
            "https://objects.invalid/file?signature=SIGNED_URL_CANARY",
            "Authorization: Bearer HEADER_CANARY",
            "SELECT secret_column FROM internal_table",
            r"C:\\private\\operator\\runtime.py",
            "/srv/private/operator/runtime.py",
            "ssh private-host -- command-canary",
            "R2 provider SDK canary",
            "postgresql://operator:password@db.internal/app",
            "username=private-operator",
            "stderr=process-canary Traceback (most recent call last)",
        )
        secret_text = " | ".join(sentinels)
        cases = (
            (RequestRejected(secret_text), "request_rejected"),
            (RuntimeError(secret_text), "internal_error"),
        )

        for error, expected_code in cases:
            with self.subTest(expected_code=expected_code):
                response = UnixRpcServer(
                    Path("/run/animemo-updater.sock"),
                    _FailingAgent(error),
                )._response({"operation": "get_status", "params": {}})
                self.assertEqual(set(response), {"ok", "error"})
                self.assertFalse(response["ok"])
                self.assert_public_failure(response["error"], expected_code)
                serialized = json.dumps(response)
                for sentinel in sentinels:
                    self.assertNotIn(sentinel, serialized)

    def test_server_rejects_hostile_success_result_before_serialization(self):
        canaries = (
            "Traceback (most recent call last)",
            r"C:\\private\\operator\\runtime.py",
            "SELECT secret_column FROM internal_table",
        )

        class HostileAgent:
            def dispatch(self, _request):
                return {**_valid_plan_result(), "events": list(canaries)}

        response = UnixRpcServer(
            Path("/run/animemo-updater.sock"), HostileAgent()
        )._response(
            {"operation": "plan_update", "params": {"version": "v1.0.1"}}
        )

        self.assertFalse(response["ok"])
        self.assert_public_failure(response["error"], "internal_error")
        serialized = json.dumps(response)
        for canary in canaries:
            self.assertNotIn(canary, serialized)

        detail_canary = "Traceback SELECT secret FROM private_table"

        class HostileEventAgent:
            def dispatch(self, _request):
                return _valid_operation_result(detail_canary)

        event_response = UnixRpcServer(
            Path("/run/animemo-updater.sock"), HostileEventAgent()
        )._response(
            {
                "operation": "get_operation",
                "params": {"operationId": "b" * 32},
            }
        )
        self.assertFalse(event_response["ok"])
        self.assertNotIn(detail_canary, json.dumps(event_response))

    def test_server_binds_success_results_to_the_validated_request(self):
        requested_id = "b" * 32
        other_id = "c" * 32
        cases = (
            (
                "list channel",
                {"operation": "list_releases", "params": {"channel": "stable"}},
                {"channel": "beta", "releases": []},
            ),
            (
                "nested list channel",
                {"operation": "list_releases", "params": {"channel": "stable"}},
                {
                    "channel": "stable",
                    "releases": [_valid_release("v1.0.1-beta.1")],
                },
            ),
            (
                "check channel",
                {"operation": "check_update", "params": {"channel": "stable"}},
                {
                    "channel": "rc",
                    "currentVersion": "v1.0.0",
                    "latest": _valid_release("v1.0.1-rc.1"),
                },
            ),
            (
                "plan version",
                {"operation": "plan_update", "params": {"version": "v1.0.1"}},
                _valid_plan_result("v1.0.2"),
            ),
            (
                "plan source",
                {"operation": "plan_update", "params": {"version": "v1.0.1"}},
                _valid_plan_result("v1.0.1", "official-mirror"),
            ),
            (
                "operation id",
                {
                    "operation": "get_operation",
                    "params": {"operationId": requested_id},
                },
                _valid_operation_result(identifier=other_id),
            ),
            (
                "log operation id",
                {
                    "operation": "get_logs",
                    "params": {"operationId": requested_id, "limit": 10},
                },
                {
                    "operationId": other_id,
                    "events": [_event("idle", "2026-08-30T12:00:00Z")],
                },
            ),
            (
                "apply kind",
                {
                    "operation": "apply_update",
                    "params": {
                        "planId": "a" * 32,
                        "confirmation": "APPLY v1.0.1",
                    },
                },
                {
                    "planId": "a" * 32,
                    "operation": _valid_operation_result(
                        kind="rollback_previous"
                    )
                },
            ),
            (
                "rollback kind",
                {
                    "operation": "rollback_previous",
                    "params": {"confirmation": "ROLLBACK PREVIOUS"},
                },
                {"operation": _valid_operation_result(kind="apply_update")},
            ),
            (
                "invalid mutation state",
                {
                    "operation": "apply_update",
                    "params": {
                        "planId": "a" * 32,
                        "confirmation": "APPLY v1.0.1",
                    },
                },
                {
                    "planId": "a" * 32,
                    "operation": {
                        "id": "",
                        "kind": "apply_update",
                        "status": "invalid_operation_state",
                        "createdAt": "",
                        "updatedAt": "",
                        "events": [
                            {
                                "status": "invalid_operation_state",
                                "at": "",
                                "detail": "Operation state is unavailable",
                            }
                        ],
                    }
                },
            ),
            (
                "apply plan replay",
                {
                    "operation": "apply_update",
                    "params": {
                        "planId": "a" * 32,
                        "confirmation": "APPLY v1.0.1",
                    },
                },
                {
                    "planId": "c" * 32,
                    "operation": _valid_operation_result(),
                },
            ),
            (
                "null check missing channel",
                {"operation": "check_update", "params": {"channel": "stable"}},
                {"currentVersion": "v1.0.0", "latest": None},
            ),
            (
                "null check channel replay",
                {"operation": "check_update", "params": {"channel": "stable"}},
                {"channel": "rc", "currentVersion": "v1.0.0", "latest": None},
            ),
        )
        for label, request, result in cases:
            with self.subTest(label=label):
                response = UnixRpcServer(
                    Path("/run/animemo-updater.sock"), _StaticAgent(result)
                )._response(request)
                self.assertFalse(response["ok"])
                self.assert_public_failure(response["error"], "internal_error")

        null_check = UnixRpcServer(
            Path("/run/animemo-updater.sock"),
            _StaticAgent(
                {
                    "channel": "stable",
                    "currentVersion": "v1.0.0",
                    "latest": None,
                }
            ),
        )._response(
            {"operation": "check_update", "params": {"channel": "stable"}}
        )
        self.assertTrue(null_check["ok"])
        self.assertEqual(null_check["result"]["channel"], "stable")

        apply = UnixRpcServer(
            Path("/run/animemo-updater.sock"),
            _StaticAgent(
                {
                    "planId": "a" * 32,
                    "operation": _valid_operation_result(),
                }
            ),
        )._response(
            {
                "operation": "apply_update",
                "params": {
                    "planId": "a" * 32,
                    "confirmation": "APPLY v1.0.1",
                },
            }
        )
        self.assertTrue(apply["ok"])
        self.assertEqual(apply["result"]["planId"], "a" * 32)

    def test_server_rejects_hostile_log_cardinality_order_and_transitions(self):
        operation_id = "b" * 32
        request = {
            "operation": "get_logs",
            "params": {"operationId": operation_id, "limit": 2},
        }
        cases = (
            ("empty", []),
            (
                "invalid-state timestamp",
                [
                    {
                        "status": "invalid_operation_state",
                        "at": "2026-08-30T12:00:00Z",
                        "detail": "Operation state is unavailable",
                    }
                ],
            ),
            (
                "over requested limit",
                [
                    _event("idle", "2026-08-30T12:00:00Z"),
                    _event("preflight", "2026-08-30T12:00:01Z"),
                    _event("fetching", "2026-08-30T12:00:02Z"),
                ],
            ),
            (
                "time reversal",
                [
                    _event("fetching", "2026-08-30T12:00:02Z"),
                    _event("verifying", "2026-08-30T12:00:01Z"),
                ],
            ),
            (
                "illegal transition",
                [
                    _event("idle", "2026-08-30T12:00:00Z"),
                    _event("succeeded", "2026-08-30T12:00:01Z"),
                ],
            ),
        )
        for label, events in cases:
            with self.subTest(label=label):
                response = UnixRpcServer(
                    Path("/run/animemo-updater.sock"),
                    _StaticAgent({"operationId": operation_id, "events": events}),
                )._response(request)
                self.assertFalse(response["ok"])
                self.assert_public_failure(response["error"], "internal_error")

        valid = UnixRpcServer(
            Path("/run/animemo-updater.sock"),
            _StaticAgent(
                {
                    "operationId": operation_id,
                    "events": [
                        _event("fetching", "2026-08-30T12:00:01Z"),
                        _event("verifying", "2026-08-30T12:00:02Z"),
                    ],
                }
            ),
        )._response(request)
        self.assertTrue(valid["ok"])

    def test_unknown_error_code_falls_back_to_internal_error(self):
        error = RequestRejected("never public")
        error.code = "attacker_selected_code"

        response = UnixRpcServer(
            Path("/run/animemo-updater.sock"), _FailingAgent(error)
        )._response({"operation": "get_status", "params": {}})

        self.assert_public_failure(response["error"], "internal_error")

    def test_client_accepts_only_exact_known_error_contract(self):
        if not hasattr(socket, "AF_UNIX"):
            self.skipTest("AF_UNIX unavailable")
        valid = public_updater_failure("request_rejected")
        tampered_cases = (
            {**valid, "detail": "SELECT private FROM internal_table"},
            {**valid, "code": "attacker_selected_code"},
            {**valid, "correlation_id": "incoming-request-id"},
            {**valid, "traceback": "private traceback"},
        )
        for index, remote in enumerate(tampered_cases):
            with self.subTest(index=index), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "updater.sock"
                ready = threading.Event()
                thread = threading.Thread(
                    target=self._serve_payload,
                    args=(path, ready, {"ok": False, "error": remote}),
                    daemon=True,
                )
                thread.start()
                self.assertTrue(ready.wait(5))
                with self.assertRaises(AgentResponseError) as raised:
                    UnixAgentClient(path).request("get_status")
                thread.join(5)
                self.assertEqual(
                    raised.exception.remote_code,
                    "updater_response_error",
                )
                self.assertNotIn("private", str(raised.exception).lower())

    def test_client_preserves_only_validated_remote_code_and_correlation(self):
        if not hasattr(socket, "AF_UNIX"):
            self.skipTest("AF_UNIX unavailable")
        remote = public_updater_failure("request_rejected")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "updater.sock"
            ready = threading.Event()
            thread = threading.Thread(
                target=self._serve_payload,
                args=(path, ready, {"ok": False, "error": remote}),
                daemon=True,
            )
            thread.start()
            self.assertTrue(ready.wait(5))
            with self.assertRaises(AgentResponseError) as raised:
                UnixAgentClient(path).request("get_status")
            thread.join(5)

        self.assertEqual(raised.exception.remote_code, "request_rejected")
        self.assertEqual(raised.exception.correlation_id, remote["correlation_id"])
        self.assertEqual(str(raised.exception), remote["detail"])

    def test_client_rejects_extra_failure_envelope_fields(self):
        if not hasattr(socket, "AF_UNIX"):
            self.skipTest("AF_UNIX unavailable")
        remote = public_updater_failure("request_rejected")
        payload = {
            "ok": False,
            "error": remote,
            "traceback": "PRIVATE_OUTER_ENVELOPE_CANARY",
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "updater.sock"
            ready = threading.Event()
            thread = threading.Thread(
                target=self._serve_payload,
                args=(path, ready, payload),
                daemon=True,
            )
            thread.start()
            self.assertTrue(ready.wait(5))
            with self.assertRaises(AgentResponseError) as raised:
                UnixAgentClient(path).request("get_status")
            thread.join(5)

        self.assertEqual(raised.exception.remote_code, "updater_response_error")
        self.assertNotIn("PRIVATE_OUTER", str(raised.exception))

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
            self.assertEqual(
                accepted["result"],
                _valid_plan_result("v1.0.0", "local-bundle"),
            )
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
            self.assertEqual(response, _valid_status_result())
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
            self.assert_public_failure(response["error"], "request_too_large")

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
