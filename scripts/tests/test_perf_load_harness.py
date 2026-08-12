import threading
import unittest

from scripts.perf.load_harness import (
    AuthenticatedHttpClient,
    EnvironmentCredentials,
    HarnessConfigurationError,
    RequestResult,
    build_read_scenario,
    hard_failure_reasons,
    run_concurrency_level,
    run_sustained,
    summarize_results,
    validate_target,
)


class _FixedClient:
    def get(self, request):
        return RequestResult(
            journey=request.name,
            status_code=200,
            latency_ms=25.0,
            response_bytes=128,
            expected_statuses=request.expected_statuses,
        )


class _ManualClock:
    def __init__(self):
        self.value = 0.0
        self.lock = threading.Lock()

    def __call__(self):
        with self.lock:
            current = self.value
            self.value += 1.0
            return current


class PerformanceLoadHarnessTests(unittest.TestCase):
    def test_target_validation_rejects_production_and_unsafe_urls(self):
        for url in (
            "https://re-anime.cc",
            "https://www.re-anime.cc/api",
            "http://45.207.221.83:8088",
            "https://user:password@example.test",
            "ftp://localhost",
            "localhost:8088",
        ):
            with self.subTest(url=url), self.assertRaises(HarnessConfigurationError):
                validate_target(url)

        self.assertEqual(validate_target("http://127.0.0.1:8088/"), "http://127.0.0.1:8088")
        self.assertEqual(validate_target("https://animemo-perf.example.test"), "https://animemo-perf.example.test")

    def test_credentials_are_explicit_and_secrets_come_from_environment(self):
        credentials = EnvironmentCredentials.from_environment(
            username="perf-user",
            password_environment="ANIMEMO_PERF_PASSWORD",
            otp_environment="ANIMEMO_PERF_OTP",
            environment={
                "ANIMEMO_PERF_PASSWORD": "test-password",
                "ANIMEMO_PERF_OTP": "123456",
            },
        )
        self.assertEqual(credentials.username, "perf-user")
        self.assertEqual(credentials.password, "test-password")
        self.assertEqual(credentials.otp, "123456")

        with self.assertRaises(HarnessConfigurationError):
            EnvironmentCredentials.from_environment(
                username="perf-user",
                password_environment="MISSING_TEST_PASSWORD",
                environment={},
            )

    def test_preissued_token_avoids_measured_login_requests(self):
        client = AuthenticatedHttpClient(
            base_url="https://animemo-perf.example.test",
            user_credentials=EnvironmentCredentials("perf-user", "test-password"),
            initial_tokens={"user": "preissued-test-token"},
        )
        client._json_request = lambda scope, path, **kwargs: (200, b"{}", {})

        result = client.get(build_read_scenario(entry_id=1, search_term="test")[0])

        self.assertEqual(result.status_code, 200)
        self.assertEqual(client._tokens["user"], "preissued-test-token")

    def test_read_scenario_covers_required_journeys_without_product_writes(self):
        requests = build_read_scenario(entry_id=42, search_term="星际牛仔", include_staff=True)

        self.assertEqual(
            [request.name for request in requests],
            [
                "dashboard",
                "filter_search",
                "entry_detail",
                "watch_history",
                "plugin_page",
                "staff_health",
            ],
        )
        self.assertTrue(all(request.method == "GET" for request in requests))
        self.assertIn("include_facets=1", requests[0].path)
        self.assertIn("search=", requests[1].path)
        self.assertEqual(requests[2].path, "/api/v1/entries/42/")
        self.assertEqual(requests[3].path, "/api/v1/entries/42/watch-history/?page=1&page_size=48")
        self.assertEqual(requests[4].path, "/api/v1/plugins/enabled/")
        self.assertEqual(requests[5].auth_scope, "staff")

    def test_summary_records_errors_percentiles_elapsed_and_throughput(self):
        results = [
            RequestResult("dashboard", 200, 10.0, 100, (200,)),
            RequestResult("dashboard", 200, 20.0, 100, (200,)),
            RequestResult("filter_search", 429, 30.0, 40, (200,)),
            RequestResult("entry_detail", 503, 40.0, 20, (200,)),
            RequestResult("plugin_page", None, 50.0, 0, (200,), error="timed out"),
        ]

        summary = summarize_results(
            mode="concurrency",
            concurrency=5,
            results=results,
            elapsed_seconds=2.0,
        )

        self.assertEqual(summary.requests, 5)
        self.assertEqual(summary.errors, 3)
        self.assertEqual(summary.http_5xx, 1)
        self.assertEqual(summary.transport_errors, 1)
        self.assertEqual(summary.p50_ms, 30.0)
        self.assertEqual(summary.p95_ms, 50.0)
        self.assertEqual(summary.throughput_rps, 2.5)

    def test_hard_failures_are_contract_based_and_never_latency_percentages(self):
        slow_but_successful = [RequestResult("dashboard", 200, 60_000.0, 1, (200,))]
        self.assertEqual(hard_failure_reasons(slow_but_successful), [])

        failures = hard_failure_reasons(
            [
                RequestResult("dashboard", 500, 5.0, 1, (200,)),
                RequestResult("filter_search", 429, 5.0, 1, (200,)),
                RequestResult("entry_detail", None, 5.0, 0, (200,), error="timed out"),
            ]
        )
        self.assertIn("http_5xx", failures)
        self.assertIn("unexpected_http_status", failures)
        self.assertNotIn("latency", " ".join(failures))
        self.assertNotIn("transport_error", failures)

    def test_concurrency_level_counts_each_virtual_user_journey(self):
        requests = build_read_scenario(entry_id=7, search_term="test", include_staff=False)[:2]
        summary = run_concurrency_level(
            client_factory=_FixedClient,
            requests=requests,
            concurrency=5,
            iterations_per_user=2,
        )

        self.assertEqual(summary.mode, "concurrency")
        self.assertEqual(summary.concurrency, 5)
        self.assertEqual(summary.requests, 20)
        self.assertEqual(summary.errors, 0)

    def test_sustained_runner_obeys_duration_boundary_without_changing_scenario(self):
        requests = build_read_scenario(entry_id=7, search_term="test", include_staff=False)[:2]
        summary = run_sustained(
            client_factory=_FixedClient,
            requests=requests,
            concurrency=1,
            duration_seconds=4.0,
            think_time_seconds=0,
            clock=_ManualClock(),
            sleeper=lambda _seconds: None,
        )

        self.assertEqual(summary.mode, "sustained")
        self.assertEqual(summary.concurrency, 1)
        self.assertGreaterEqual(summary.requests, 2)
        self.assertEqual(summary.errors, 0)


if __name__ == "__main__":
    unittest.main()
