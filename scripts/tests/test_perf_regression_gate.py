import unittest

from scripts.perf.regression_gate import evaluate


def sample_summary(value, runs=10):
    return {"runs": runs, "minimum": value, "median": value, "p95": value, "maximum": value}


def frontend_report(*, update_pass=True):
    run = {
        "measured": True,
        "failures": [],
        "console_errors": [],
        "requests": [{"status": 200}],
        "duplicates": [],
    }
    return {
        "build_inventory": {"javascript_bytes": 1_100_000},
        "journeys": {"dashboard": {"runs": [dict(run) for _ in range(5)]}},
        "staff_polling": {
            "hidden_tab_suppression": "PASS",
            "overlap_coalescing": "PASS",
            "visible_return_refresh": "PASS",
        },
        "update_operation_polling": {
            "hidden_tab_suppression": "PASS" if update_pass else "FAIL",
            "overlap_coalescing": "PASS" if update_pass else "FAIL",
            "visible_slow_response": {"maximum_in_flight": 1 if update_pass else 2},
            "hidden_tab": {"requests_after_one_interval": 0 if update_pass else 1},
        },
        "dashboard_page_48": {
            "requested_pages": list(range(1, 49)),
            "page_48_requests": 1,
            "exact_duplicate_requests": [],
        },
    }


def backend_report(dataset, count):
    probes = {
        "journal_page_1": {
            "status_codes": [200],
            "query_count": sample_summary(6),
            "duplicate_query_count": sample_summary(0),
        },
        "plugin_marketplace": {
            "status_codes": [200],
            "query_count": sample_summary(count),
            "duplicate_query_count": sample_summary(max(0, count - 2)),
        },
    }
    if dataset == "LARGE":
        probes["journal_page_48"] = {
            "status_codes": [200],
            "query_count": sample_summary(6),
            "duplicate_query_count": sample_summary(0),
        }
    return {
        "mode": "POSTGRESQL_AUTHORITATIVE",
        "database": {"vendor": "postgresql", "authoritative": True},
        "dataset": {"dataset": dataset},
        "probes": probes,
    }


def resource_report(*, errors=0, unique_users=20):
    runs = [
        {"mode": "concurrency", "concurrency": level, "errors": errors}
        for level in (1, 5, 10, 20)
    ]
    runs.append({"mode": "sustained", "concurrency": 5, "errors": errors, "elapsed_seconds": 1500})
    return {
        "virtual_users": {
            "provided": 20,
            "unique_usernames": unique_users,
            "unique_entry_ids": unique_users,
        },
        "load": {"runs": runs, "hard_failures": []},
        "resources": {"hard_failures": [], "sampling_errors": [], "summary": {"samples": 100}},
    }


class PerformanceRegressionGateTests(unittest.TestCase):
    def test_complete_deterministic_matrix_passes(self):
        result = evaluate(
            frontend=frontend_report(),
            backend=[backend_report("SMALL", 2), backend_report("MEDIUM", 2), backend_report("LARGE", 2)],
            resource=resource_report(),
        )
        self.assertEqual(result["status"], "PASS")

    def test_frontend_update_polling_regression_is_red_capable(self):
        result = evaluate(frontend=frontend_report(update_pass=False))
        self.assertEqual(result["status"], "FAIL")
        self.assertIn("frontend", result["failed_sections"])

    def test_backend_query_scaling_regression_is_red_capable(self):
        result = evaluate(
            backend=[backend_report("SMALL", 2), backend_report("MEDIUM", 20), backend_report("LARGE", 50)]
        )
        self.assertEqual(result["status"], "FAIL")
        self.assertTrue(any("query_scaling" in item for item in result["sections"]["backend"]["failures"]))

    def test_resource_request_errors_are_red_capable(self):
        result = evaluate(resource=resource_report(errors=1))
        self.assertEqual(result["status"], "FAIL")
        self.assertIn("resource:request_errors", result["sections"]["resource"]["failures"])

    def test_resource_shared_virtual_identity_is_red_capable(self):
        result = evaluate(resource=resource_report(unique_users=1))

        self.assertEqual(result["status"], "FAIL")
        self.assertIn(
            "resource:virtual_users:shared_identity",
            result["sections"]["resource"]["failures"],
        )
        self.assertIn(
            "resource:virtual_users:shared_entry",
            result["sections"]["resource"]["failures"],
        )


if __name__ == "__main__":
    unittest.main()
