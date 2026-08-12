import unittest

from scripts.perf.contract import (
    API_MEASURED_RUNS,
    CONCURRENCY_LEVELS,
    DATASETS,
    DEEP_DASHBOARD_PAGE,
    REQUIRED_DATABASE_VENDOR,
    SUSTAINED_MINUTES,
    contract_snapshot,
    has_query_scaling_regression,
    nearest_rank,
    summarize_samples,
    validate_finding,
)


class PerformanceContractTests(unittest.TestCase):
    def test_v1_dataset_and_load_contract_is_fixed(self):
        self.assertEqual(
            [DATASETS[key].journal_entries for key in ("small", "medium", "large")],
            [50, 1_000, 10_000],
        )
        self.assertEqual(CONCURRENCY_LEVELS, (1, 5, 10, 20))
        self.assertEqual(DEEP_DASHBOARD_PAGE, 48)
        self.assertEqual(REQUIRED_DATABASE_VENDOR, "postgresql")
        self.assertGreaterEqual(API_MEASURED_RUNS, 5)
        self.assertGreaterEqual(SUSTAINED_MINUTES, 20)

    def test_sample_summary_uses_median_and_nearest_rank_p95(self):
        samples = [10, 11, 12, 13, 14, 15, 16, 17, 18, 50]
        self.assertEqual(nearest_rank(samples, 95), 50)
        self.assertEqual(
            summarize_samples(samples),
            {"runs": 10, "minimum": 10.0, "median": 14.5, "p95": 50.0, "maximum": 50.0},
        )

    def test_sample_summary_rejects_empty_or_invalid_percentiles(self):
        with self.assertRaises(ValueError):
            summarize_samples([])
        with self.assertRaises(ValueError):
            nearest_rank([1], 0)

    def test_query_scaling_guard_detects_unbounded_growth(self):
        self.assertFalse(has_query_scaling_regression(8, 12))
        self.assertTrue(has_query_scaling_regression(8, 14))

    def test_finding_schema_requires_shared_fields_and_severity(self):
        finding = {
            "id": "PERF-API-001",
            "proposed_severity": "PERF1",
            "area": "API",
            "journey": "Dashboard",
            "dataset": "LARGE",
            "evidence": "query count scales with rows",
            "before": "N=10: 12; N=100: 102",
            "root_cause": "per-row lookup",
            "suggested_fix": "prefetch related rows",
            "contract_risk": "none",
            "owner": "Backend",
        }
        validate_finding(finding)
        with self.assertRaises(ValueError):
            validate_finding({**finding, "owner": ""})
        with self.assertRaises(ValueError):
            validate_finding({**finding, "proposed_severity": "HIGH"})

    def test_contract_snapshot_is_json_serializable_data(self):
        snapshot = contract_snapshot()
        self.assertEqual(snapshot["datasets"]["large"]["journal_entries"], 10_000)
        self.assertEqual(snapshot["concurrency_levels"], [1, 5, 10, 20])


if __name__ == "__main__":
    unittest.main()
