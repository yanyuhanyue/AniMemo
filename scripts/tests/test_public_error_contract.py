from __future__ import annotations

import ast
import io
import json
import re
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.config import api_errors as backend_errors  # noqa: E402
from updater import public_errors as updater_errors  # noqa: E402
from updater.__main__ import main as updater_main  # noqa: E402
from updater.public_state import public_operation  # noqa: E402

STRICT_KEYS = {"code", "detail", "correlation_id"}
CORRELATION_ID = re.compile(r"^[0-9a-f]{32}$")
PUBLIC_OPERATION_KEYS = {"id", "kind", "status", "createdAt", "updatedAt", "events"}
PUBLIC_EVENT_KEYS = {"status", "at", "detail"}


def _function(path: str, name: str) -> ast.FunctionDef:
    tree = ast.parse((ROOT / path).read_text(encoding="utf-8"), filename=path)
    candidates = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == name
    ]
    if len(candidates) != 1:
        raise AssertionError(f"expected exactly one {path}:{name}")
    return candidates[0]


def _named_call_count(function: ast.FunctionDef, name: str) -> int:
    return sum(
        1
        for node in ast.walk(function)
        if isinstance(node, ast.Call)
        and (
            (isinstance(node.func, ast.Name) and node.func.id == name)
            or (isinstance(node.func, ast.Attribute) and node.func.attr == name)
        )
    )


class PublicErrorContractGate(unittest.TestCase):
    def assert_strict_failure(self, value):
        self.assertEqual(set(value), STRICT_KEYS)
        self.assertIsInstance(value["code"], str)
        self.assertIsInstance(value["detail"], str)
        self.assertRegex(value["correlation_id"], CORRELATION_ID)

    def test_backend_factory_discards_prose_and_client_correlation(self):
        class Request:
            headers = {
                "X-Request-ID": "client-selected-request-id",
                "X-AniMemo-Correlation-ID": "client-selected-correlation-id",
            }

        request = Request()
        first = backend_errors.public_failure(
            request=request,
            candidate_code="unknown_private_error",
            status_code=500,
        )
        second = backend_errors.public_failure(
            request=request,
            candidate_code="internal_error",
            status_code=500,
        )

        self.assert_strict_failure(first)
        self.assertEqual(first, second)
        self.assertEqual(first["code"], "internal_error")
        self.assertNotIn("client-selected", repr(first))

    def test_backend_code_and_status_must_match_exactly(self):
        exact = backend_errors.public_failure(
            request=None,
            candidate_code="request_rejected",
            status_code=500,
        )
        padded = backend_errors.public_failure(
            request=None,
            candidate_code=" internal_error ",
            status_code=500,
        )

        self.assert_strict_failure(exact)
        self.assert_strict_failure(padded)
        self.assertEqual(exact["code"], "internal_error")
        self.assertEqual(padded["code"], "internal_error")

    def test_updater_factory_never_stringifies_candidate_objects(self):
        class HostileCandidate:
            def __str__(self):
                raise AssertionError("candidate prose must never be read")

        failure = updater_errors.public_updater_failure(HostileCandidate())

        self.assert_strict_failure(failure)
        self.assertEqual(failure["code"], "internal_error")

    def test_django_error_renderer_and_handler_are_mandatory(self):
        settings = (ROOT / "backend" / "config" / "settings.py").read_text(
            encoding="utf-8"
        )
        renderer = (
            ROOT / "backend" / "config" / "api_renderers.py"
        ).read_text(encoding="utf-8")
        handler = (
            ROOT / "backend" / "config" / "rest_exceptions.py"
        ).read_text(encoding="utf-8")

        self.assertIn(
            '"EXCEPTION_HANDLER": "config.rest_exceptions.exception_handler"',
            settings,
        )
        self.assertIn(
            '"config.api_renderers.CanonicalJSONRenderer"',
            settings,
        )
        self.assertIn("response.status_code >= 400", renderer)
        self.assertIn("canonicalize_payload", renderer)
        self.assertIn('candidate_code="internal_error"', handler)
        self.assertNotIn("return None", handler)

    def test_django_url_error_handlers_close_non_drf_api_failures(self):
        urls = (ROOT / "backend" / "config" / "urls.py").read_text(
            encoding="utf-8"
        )
        for status, function_name in (
            (400, "api_bad_request"),
            (403, "api_permission_denied"),
            (404, "api_page_not_found"),
            (500, "api_server_error"),
        ):
            with self.subTest(status=status):
                self.assertIn(f"handler{status} = {function_name}", urls)
                function = _function("backend/config/urls.py", function_name)
                self.assertEqual(
                    _named_call_count(function, "_api_failure_response"),
                    1,
                )

        boundary = _function("backend/config/urls.py", "_api_failure_response")
        self.assertEqual(_named_call_count(boundary, "public_failure"), 1)
        self.assertIn('response["X-AniMemo-Correlation-ID"]', urls)
        api_matcher = _function("backend/config/urls.py", "_is_api_request")
        matcher_source = ast.unparse(api_matcher)
        self.assertIn("path_info.startswith('/api/')", matcher_source)

    def test_critical_external_and_durable_paths_do_not_render_exception_text(self):
        paths = (
            "backend/journal/auth_views.py",
            "backend/journal/import_export_views.py",
            "backend/journal/external_accounts/imports.py",
            "backend/journal/external_accounts/views.py",
            "backend/journal/image_security.py",
            "backend/journal/public_views.py",
            "backend/journal/staff_system_views.py",
            "backend/journal/staff_update_views.py",
            "backend/integrations/services.py",
            "backend/integrations/views.py",
            "backend/plugin_host/services.py",
            "backend/plugin_host/views.py",
            "backend/plugin_host/hooks.py",
            "backend/plugin_host/sdk/logging.py",
            "backend/plugin_host/registry.py",
            "backend/plugin_host/runtime/dispatch.py",
            "plugins/watch-history-importer/backend/plugin.py",
            "updater/__main__.py",
            "updater/agent.py",
            "updater/client.py",
            "updater/executor.py",
            "updater/server.py",
        )
        forbidden = (
            re.compile(r"\b(?:str|redact)\((?:error|exc|exception)\)"),
            re.compile(r"\bdetail\s*=\s*f[^\n]*(?:error|exc|exception)"),
            re.compile(r"\bdetail\s*=\s*str\((?:error|exc|exception)\)"),
        )
        violations = []
        for relative in paths:
            source = (ROOT / relative).read_text(encoding="utf-8")
            for pattern in forbidden:
                for match in pattern.finditer(source):
                    line = source.count("\n", 0, match.start()) + 1
                    violations.append(f"{relative}:{line}: {match.group(0)}")

        self.assertEqual(violations, [])

    def test_sensitive_logging_boundaries_are_closed_and_traceback_free(self):
        paths = (
            "backend/plugin_host/hooks.py",
            "backend/journal/image_security.py",
            "backend/plugin_host/sdk/logging.py",
        )
        violations = []
        for relative in paths:
            tree = ast.parse(
                (ROOT / relative).read_text(encoding="utf-8"),
                filename=relative,
            )
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                if isinstance(node.func, ast.Attribute) and node.func.attr == "exception":
                    violations.append(f"{relative}:{node.lineno}: logger.exception")
                for keyword in node.keywords:
                    if keyword.arg in {"exc_info", "stack_info"}:
                        violations.append(
                            f"{relative}:{node.lineno}: {keyword.arg} forwarding"
                        )

        self.assertEqual(violations, [])

        process = _function("backend/plugin_host/sdk/logging.py", "process")
        process_inputs = {
            node.id
            for node in ast.walk(process)
            if isinstance(node, ast.Name)
            and isinstance(node.ctx, ast.Load)
            and node.id in {"msg", "kwargs"}
        }
        self.assertEqual(process_inputs, set())

        returns = [node for node in ast.walk(process) if isinstance(node, ast.Return)]
        self.assertEqual(len(returns), 1)
        returned = returns[0].value
        self.assertIsInstance(returned, ast.Tuple)
        self.assertEqual(len(returned.elts), 2)
        self.assertIsInstance(returned.elts[0], ast.Name)
        self.assertEqual(returned.elts[0].id, "_EVENT")
        self.assertIsInstance(returned.elts[1], ast.Dict)
        self.assertEqual(
            [key.value for key in returned.elts[1].keys],
            ["extra"],
        )
        closed_extra = returned.elts[1].values[0]
        self.assertIsInstance(closed_extra, ast.Dict)
        self.assertEqual(
            [key.value for key in closed_extra.keys],
            ["animemo_stage", "plugin"],
        )

        adapter_log = _function("backend/plugin_host/sdk/logging.py", "log")
        loaded_inputs = [
            node.id
            for node in ast.walk(adapter_log)
            if isinstance(node, ast.Name)
            and isinstance(node.ctx, ast.Load)
            and node.id in {"args", "kwargs"}
        ]
        self.assertNotIn("args", loaded_inputs)
        self.assertEqual(loaded_inputs.count("kwargs"), 1)

        sink_calls = [
            node
            for node in ast.walk(adapter_log)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "log"
            and isinstance(node.func.value, ast.Attribute)
            and node.func.value.attr == "logger"
        ]
        self.assertEqual(len(sink_calls), 1)
        sink = sink_calls[0]
        self.assertFalse(any(isinstance(argument, ast.Starred) for argument in sink.args))
        self.assertEqual(len(sink.args), 2)
        self.assertEqual(len(sink.keywords), 1)
        self.assertIsNone(sink.keywords[0].arg)
        self.assertIsInstance(sink.keywords[0].value, ast.Name)
        self.assertEqual(sink.keywords[0].value.id, "safe_kwargs")

    def test_success_and_replay_paths_do_not_read_exception_detail(self):
        paths = (
            "backend/journal/import_export_views.py",
            "backend/journal/external_accounts/imports.py",
            "backend/journal/external_accounts/views.py",
            "backend/integrations/services.py",
            "backend/integrations/views.py",
            "backend/plugin_host/services.py",
            "backend/plugin_host/registry.py",
            "backend/plugin_host/runtime/dispatch.py",
            "plugins/watch-history-importer/backend/plugin.py",
        )
        violations = []
        pattern = re.compile(
            r"\b(?:error|exc|exception)\.(?:detail|message|args|stderr|stdout)\b"
        )
        for relative in paths:
            source = (ROOT / relative).read_text(encoding="utf-8")
            for match in pattern.finditer(source):
                line = source.count("\n", 0, match.start()) + 1
                violations.append(f"{relative}:{line}: {match.group(0)}")

        self.assertEqual(violations, [])

    def test_partial_success_and_redirects_use_strict_public_projection(self):
        required_calls = {
            ("backend/journal/import_export_views.py", "_partial_import_failure"): 1,
            ("backend/journal/import_export_views.py", "_public_data_bundle_preview"): 2,
            ("backend/journal/external_accounts/views.py", "_public_import_result"): 1,
            ("backend/journal/external_accounts/views.py", "_oauth_callback_failure"): 1,
        }
        for (path, function_name), expected in required_calls.items():
            with self.subTest(path=path, function=function_name):
                self.assertEqual(
                    _named_call_count(_function(path, function_name), "public_failure"),
                    expected,
                )

        import_source = (
            ROOT / "backend/journal/import_export_views.py"
        ).read_text(encoding="utf-8")
        persistence_source = (
            ROOT / "backend/journal/external_accounts/imports.py"
        ).read_text(encoding="utf-8")
        self.assertNotIn("serializer.errors", import_source)
        self.assertNotRegex(
            persistence_source,
            r"\b(?:error|exc|exception)\.(?:detail|message|args|stderr|stdout)\b",
        )

    def test_every_updater_operation_boundary_uses_the_closed_projection(self):
        required_calls = {
            ("updater/agent.py", "_status"): 2,
            ("updater/agent.py", "_apply_while_open"): 2,
            ("updater/agent.py", "_rollback_previous_while_open"): 2,
            ("updater/agent.py", "dispatch"): 2,
            ("updater/runtime.py", "reconcile"): 1,
            ("updater/runtime.py", "_reconcile_initial_adoption"): 1,
        }
        for (path, function_name), minimum in required_calls.items():
            with self.subTest(path=path, function=function_name):
                function = _function(path, function_name)
                self.assertGreaterEqual(
                    _named_call_count(function, "public_operation"),
                    minimum,
                )

        dispatch = _function("updater/agent.py", "dispatch")
        self.assertEqual(_named_call_count(dispatch, "public_event"), 0)

    def test_legacy_persistence_is_closed_at_queries_mutations_reconcile_and_cli(self):
        canary = (
            r"C:\\private\\operator\\runtime.py SELECT secret FROM private_table "
            "stderr=PRIVATE_CANARY Traceback username=private-operator"
        )
        raw = {
            "id": "a" * 32,
            "kind": "apply_update",
            "status": "manual_recovery_required",
            "createdAt": "2026-08-30T10:00:00Z",
            "updatedAt": canary,
            "metadata": {"private": canary},
            "recovery": {"private": canary},
            "events": [
                {
                    "status": "manual_recovery_required",
                    "at": canary,
                    "detail": canary,
                    "traceback": canary,
                }
            ],
        }
        projected = public_operation(raw)
        self.assertEqual(set(projected), PUBLIC_OPERATION_KEYS)
        self.assertEqual(projected["status"], "invalid_operation_state")
        self.assertTrue(
            all(set(event) == PUBLIC_EVENT_KEYS for event in projected["events"])
        )

        boundaries = {
            "status": {"operation": projected},
            "get_operation": projected,
            "get_logs": {
                "operationId": projected["id"],
                "events": projected["events"],
            },
            "apply": {"operation": projected},
            "rollback": {"operation": projected},
            "reconcile": projected,
        }
        serialized = json.dumps(boundaries, ensure_ascii=False)
        for sentinel in (
            "private",
            "SELECT secret",
            "PRIVATE_CANARY",
            "Traceback",
            "username",
            "metadata",
            "recovery",
        ):
            self.assertNotIn(sentinel, serialized)

        class Runtime:
            def status(self):
                return boundaries["status"]

            def reconcile(self, _operation_id, _confirmation):
                return boundaries["reconcile"]

        output = io.StringIO()
        with patch("updater.__main__.production_runtime", return_value=Runtime()), redirect_stdout(output):
            self.assertEqual(updater_main(["status"]), 0)
            self.assertEqual(
                updater_main(
                    [
                        "reconcile",
                        "--operation-id",
                        "a" * 32,
                        "--confirmation",
                        "RECONCILE " + "a" * 32,
                    ]
                ),
                0,
            )
        cli_output = output.getvalue()
        self.assertNotIn("private", cli_output.lower())
        self.assertNotIn("metadata", cli_output)
        self.assertNotIn("recovery", cli_output)


if __name__ == "__main__":
    unittest.main()
