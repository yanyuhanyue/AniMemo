import json
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from scripts.perf.isolated_run import VirtualUserIdentity
from scripts.perf.load_harness import HarnessConfigurationError
from scripts.perf.long_operation_capacity import (
    LONG_OPERATION_LEVELS,
    MAX_MATRIX_REQUESTS_PER_IDENTITY,
    NORMAL_USER_LEVELS,
    USER_THROTTLE_RATE_PER_MINUTE,
    HttpResult,
    _http_request,
    attach_baseline_deltas,
    capacity_execution_plan,
    inspect_gunicorn_capacity,
    maximum_matrix_requests_per_identity,
    run_capacity_cell,
)
from scripts.perf.resource_sampler import ResourceSample


def identities(count=60):
    return tuple(
        VirtualUserIdentity(f"user-{index}", f"token-{index}", 1_000 + index)
        for index in range(count)
    )


class LongOperationCapacityTests(unittest.TestCase):
    def test_required_matrix_is_exactly_twelve_cells(self):
        self.assertEqual(NORMAL_USER_LEVELS, (20, 40, 60))
        self.assertEqual(LONG_OPERATION_LEVELS, (0, 2, 4, 8))
        self.assertEqual(
            {(users, operations) for users in NORMAL_USER_LEVELS for operations in LONG_OPERATION_LEVELS},
            {
                (20, 0), (20, 2), (20, 4), (20, 8),
                (40, 0), (40, 2), (40, 4), (40, 8),
                (60, 0), (60, 2), (60, 4), (60, 8),
            },
        )

    def test_cell_barrier_sends_real_scenario_shape_and_captures_metrics(self):
        calls = []
        lock = threading.Lock()

        def request_function(**kwargs):
            with lock:
                calls.append(kwargs)
            return HttpResult(
                kwargs["kind"],
                kwargs["journey"],
                200,
                25.0 if kwargs["kind"] == "normal" else 1_200.0,
                64,
                payload=(
                    {"provider": "fake-bangumi-provider", "network": "disabled", "latency_ms": 1200}
                    if kwargs["kind"] == "long_operation"
                    else None
                ),
            )

        result = run_capacity_cell(
            base_url="http://127.0.0.1:8088",
            identities=identities(),
            normal_users=20,
            long_operations=8,
            iterations_per_user=1,
            timeout_seconds=5,
            worker_capacity=8,
            request_function=request_function,
        )

        self.assertEqual(result["requests"], 88)
        self.assertEqual(result["normal"]["requests"], 80)
        self.assertEqual(result["long_operation"]["requests"], 8)
        self.assertEqual(result["long_operation"]["p95_ms"], 1_200.0)
        self.assertEqual(result["http_5xx"], 0)
        self.assertEqual(result["http_429"], 0)
        self.assertEqual(result["timeouts"], 0)
        self.assertEqual(result["resources"]["source"], "not_sampled")
        self.assertGreaterEqual(result["capacity_pressure"]["peak_client_in_flight"], 1)
        self.assertNotIn("saturation_proxy", result["capacity_pressure"])
        self.assertFalse("worker_saturation" in result)
        self.assertEqual(result["capacity_pressure"]["deterministic_long_operation_occupancy"], 1.0)
        self.assertTrue(any(call["method"] == "POST" and "provider-latency" in call["path"] for call in calls))
        self.assertTrue(any(call["method"] == "GET" and "/entries/" in call["path"] for call in calls))
        self.assertEqual(len({call["token"] for call in calls if call["kind"] == "normal"}), 20)

    def test_cell_records_429_5xx_timeout_and_transport_statuses(self):
        status_by_journey = {
            "dashboard": 429,
            "entry_detail": 503,
            "watch_history": None,
            "plugin_page": 200,
        }

        def request_function(**kwargs):
            status = status_by_journey.get(kwargs["journey"], 200)
            if status is None:
                return HttpResult(kwargs["kind"], kwargs["journey"], None, 50.0, 0, "TimeoutError: timed out", True)
            return HttpResult(kwargs["kind"], kwargs["journey"], status, 20.0, 1)

        result = run_capacity_cell(
            base_url="http://127.0.0.1:8088",
            identities=identities(),
            normal_users=20,
            long_operations=0,
            iterations_per_user=1,
            timeout_seconds=5,
            worker_capacity=8,
            request_function=request_function,
        )

        self.assertEqual(result["http_429"], 20)
        self.assertEqual(result["http_5xx"], 20)
        self.assertEqual(result["timeouts"], 20)
        self.assertEqual(result["status_counts"]["transport_error"], 20)
        self.assertEqual(
            result["hard_failures"],
            ["http_429", "http_5xx", "request_timeout", "unexpected_http_status"],
        )

    def test_http_client_rejects_redirect_without_forwarding_authorization(self):
        received_authorization = []

        class DestinationHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                received_authorization.append(self.headers.get("Authorization"))
                self.send_response(200)
                self.end_headers()

            def log_message(self, *_args):
                pass

        destination = ThreadingHTTPServer(("127.0.0.1", 0), DestinationHandler)

        class RedirectHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(302)
                self.send_header(
                    "Location",
                    f"http://127.0.0.1:{destination.server_port}/escaped",
                )
                self.end_headers()

            def log_message(self, *_args):
                pass

        redirect = ThreadingHTTPServer(("127.0.0.1", 0), RedirectHandler)
        destination_thread = threading.Thread(target=destination.serve_forever, daemon=True)
        redirect_thread = threading.Thread(target=redirect.serve_forever, daemon=True)
        destination_thread.start()
        redirect_thread.start()
        try:
            result = _http_request(
                base_url=f"http://127.0.0.1:{redirect.server_port}",
                token="must-not-leave-origin",
                path="/redirect",
                method="GET",
                kind="normal",
                journey="redirect_probe",
                timeout_seconds=2,
            )
        finally:
            redirect.shutdown()
            destination.shutdown()
            redirect.server_close()
            destination.server_close()
            redirect_thread.join(timeout=2)
            destination_thread.join(timeout=2)

        self.assertEqual(result.status_code, 302)
        self.assertEqual(received_authorization, [])

    def test_resource_sample_must_complete_inside_active_load_window(self):
        active_sample_complete = threading.Event()

        class ActiveWindowSampler:
            def __init__(self):
                self.calls = 0

            def sample(self):
                self.calls += 1
                if self.calls == 2:
                    active_sample_complete.set()
                return ResourceSample.for_test(api_cpu=25.0, db_connections=4)

        def request_function(**kwargs):
            active_sample_complete.wait(timeout=2)
            return HttpResult(kwargs["kind"], kwargs["journey"], 200, 5.0, 1)

        result = run_capacity_cell(
            base_url="http://127.0.0.1:8088",
            identities=identities(),
            normal_users=20,
            long_operations=0,
            iterations_per_user=1,
            timeout_seconds=5,
            worker_capacity=8,
            resource_sampler=ActiveWindowSampler(),
            resource_interval_seconds=0.01,
            request_function=request_function,
        )

        self.assertGreaterEqual(result["resources"]["active_window_samples"], 1)
        self.assertTrue(result["resources"]["active_window_proven"])
        self.assertNotIn("resource_sampling_no_active_window", result["hard_failures"])

    def test_resource_sample_after_load_is_rejected_as_missing_active_evidence(self):
        all_requests_seen = threading.Event()
        request_count = 0
        request_lock = threading.Lock()

        class LateSampler:
            def __init__(self):
                self.calls = 0

            def sample(self):
                self.calls += 1
                if self.calls == 2:
                    all_requests_seen.wait(timeout=2)
                    time.sleep(0.05)
                return ResourceSample.for_test(api_cpu=25.0, db_connections=4)

        def request_function(**kwargs):
            nonlocal request_count
            with request_lock:
                request_count += 1
                if request_count == 80:
                    all_requests_seen.set()
            return HttpResult(kwargs["kind"], kwargs["journey"], 200, 5.0, 1)

        result = run_capacity_cell(
            base_url="http://127.0.0.1:8088",
            identities=identities(),
            normal_users=20,
            long_operations=0,
            iterations_per_user=1,
            timeout_seconds=5,
            worker_capacity=8,
            resource_sampler=LateSampler(),
            resource_interval_seconds=0.01,
            request_function=request_function,
        )

        self.assertEqual(result["resources"]["active_window_samples"], 0)
        self.assertFalse(result["resources"]["active_window_proven"])
        self.assertIn("resource_sampling_no_active_window", result["hard_failures"])

    def test_baseline_deltas_leave_saturation_decision_for_evidence_review(self):
        baseline = {
            "normal_users": 20,
            "long_operations": 0,
            "normal": {"p50_ms": 10.0, "p95_ms": 20.0, "p99_ms": 30.0, "throughput_rps": 100.0},
            "capacity_pressure": {"deterministic_long_operation_occupancy": 0.0},
        }
        stressed = {
            "normal_users": 20,
            "long_operations": 8,
            "normal": {"p50_ms": 15.0, "p95_ms": 40.0, "p99_ms": 60.0, "throughput_rps": 70.0},
            "capacity_pressure": {"deterministic_long_operation_occupancy": 1.0},
        }

        annotated = attach_baseline_deltas([baseline, stressed])

        self.assertEqual(annotated[1]["baseline_delta"]["normal_p95_ms"], 20.0)
        self.assertEqual(annotated[1]["baseline_delta"]["normal_throughput_rps"], -30.0)
        self.assertEqual(annotated[1]["baseline_delta"]["normal_p95_percent"], 100.0)
        self.assertEqual(annotated[1]["baseline_delta"]["normal_throughput_percent"], -30.0)
        evidence = annotated[1]["worker_saturation_evidence"]
        self.assertEqual(evidence["decision"], "manual_review_required")
        self.assertFalse(evidence["direct_worker_telemetry"])

    def test_full_matrix_stays_below_explicit_per_identity_throttle_budget(self):
        self.assertEqual(MAX_MATRIX_REQUESTS_PER_IDENTITY, 153)
        self.assertEqual(maximum_matrix_requests_per_identity(1), 105)
        self.assertLess(MAX_MATRIX_REQUESTS_PER_IDENTITY, USER_THROTTLE_RATE_PER_MINUTE)

    def test_each_measured_cell_has_an_immediately_preceding_unmeasured_warmup(self):
        plan = capacity_execution_plan()
        self.assertEqual(len(plan), 24)
        for index in range(0, len(plan), 2):
            warmup = plan[index]
            measured = plan[index + 1]
            self.assertEqual(warmup["phase"], "warmup")
            self.assertEqual(measured["phase"], "measured")
            self.assertEqual(warmup["normal_users"], measured["normal_users"])
            self.assertEqual(warmup["target_long_operations"], measured["long_operations"])
            self.assertEqual(warmup["long_operations"], 0)
            self.assertEqual(warmup["iterations_per_user"], 1)

    def test_inspect_requires_real_gunicorn_two_by_four(self):
        def successful_run(*_args, **_kwargs):
            return subprocess.CompletedProcess(
                [],
                0,
                stdout=json.dumps([
                    "gunicorn", "config.wsgi:application", "--workers", "2", "--threads", "4"
                ]),
            )

        evidence = inspect_gunicorn_capacity("isolated-api", run_command=successful_run)
        self.assertEqual(evidence["worker_capacity"], 8)

        def wrong_run(*_args, **_kwargs):
            return subprocess.CompletedProcess(
                [],
                0,
                stdout=json.dumps(["gunicorn", "config.wsgi:application", "--workers", "1", "--threads", "4"]),
            )

        with self.assertRaisesRegex(HarnessConfigurationError, "requires Gunicorn 2x4"):
            inspect_gunicorn_capacity("isolated-api", run_command=wrong_run)

    def test_cli_requires_real_target_identities_and_compose_scope(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "capacity.json"
            completed = subprocess.run(
                [
                    "python",
                    "scripts/perf/long_operation_capacity.py",
                    "--confirm-isolated",
                    "--base-url",
                    "https://animemo.cc",
                    "--identities-file",
                    str(Path(directory) / "missing.json"),
                    "--compose-project",
                    "animemo-capacity-test",
                    "--postgres-user",
                    "animemo",
                    "--postgres-database",
                    "animemo",
                    "--output",
                    str(output),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("production AniMemo targets are forbidden", completed.stderr)
            self.assertFalse(output.exists())

    def test_registered_performance_workflow_runs_capacity_job_and_never_targets_production(self):
        workflow = Path(".github/workflows/performance.yml").read_text(encoding="utf-8")
        for required in (
            "workflow_dispatch:",
            "workflow_call:",
            "ref: ${{ inputs.candidate_sha }}",
            "COMPOSE_PROJECT_NAME: animemo-capacity-${{ github.run_id }}-${{ github.run_attempt }}",
            "ANIMEMO_ISOLATED_CAPACITY_PROBE=true",
            "ANIMEMO_ISOLATED_PROVIDER_LATENCY_MS=1200",
            "--count 60",
            "scripts/perf/long_operation_capacity.py",
            "down -v --remove-orphans",
            "long-operation-capacity.json",
            "needs: [frontend, backend, isolated-resource-load, isolated-long-operation-capacity]",
            "name: performance-long-operation-capacity",
        ):
            self.assertIn(required, workflow)
        self.assertNotIn("ssh ", workflow.lower())
        self.assertNotIn("https://animemo.cc", workflow)
        self.assertNotIn("api.bgm.tv", workflow)
        self.assertNotIn("BANGUMI_OAUTH_CLIENT_SECRET", workflow)


if __name__ == "__main__":
    unittest.main()
