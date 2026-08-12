"""Shared AniMemo v1.0 performance measurement contract.

This module contains measurement policy only.  Product code and benchmarks
import it so every performance workstream uses the same dataset names,
repetition counts, concurrency levels, and finding vocabulary.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from math import ceil
from statistics import median
from typing import Iterable, Mapping, Sequence


@dataclass(frozen=True)
class DatasetShape:
    name: str
    journal_entries: int
    supporting_users: int
    plugins: int
    watch_history_records: int


DATASETS: Mapping[str, DatasetShape] = {
    "small": DatasetShape(
        name="SMALL",
        journal_entries=50,
        supporting_users=10,
        plugins=5,
        watch_history_records=25,
    ),
    "medium": DatasetShape(
        name="MEDIUM",
        journal_entries=1_000,
        supporting_users=50,
        plugins=20,
        watch_history_records=500,
    ),
    "large": DatasetShape(
        name="LARGE",
        journal_entries=10_000,
        supporting_users=100,
        plugins=50,
        watch_history_records=5_000,
    ),
}

API_WARMUP_RUNS = 2
API_MEASURED_RUNS = 10
BROWSER_WARMUP_RUNS = 1
BROWSER_MEASURED_RUNS = 5
CONCURRENCY_LEVELS = (1, 5, 10, 20)
SUSTAINED_MINUTES = 25
DEEP_DASHBOARD_PAGE = 48
REQUIRED_DATABASE_VENDOR = "postgresql"

FINDING_SEVERITIES = frozenset({"PERF0", "PERF1", "PERF2", "PERF3"})
FINDING_DECISIONS = frozenset({"FIX", "DEFER"})
FINDING_REQUIRED_FIELDS = (
    "id",
    "proposed_severity",
    "area",
    "journey",
    "dataset",
    "evidence",
    "before",
    "root_cause",
    "suggested_fix",
    "contract_risk",
    "owner",
)


def nearest_rank(values: Sequence[float], percentile: float) -> float:
    """Return a deterministic nearest-rank percentile for measured samples."""

    if not values:
        raise ValueError("at least one measured value is required")
    if not 0 < percentile <= 100:
        raise ValueError("percentile must be in (0, 100]")
    ordered = sorted(float(value) for value in values)
    return ordered[max(0, ceil(percentile / 100 * len(ordered)) - 1)]


def summarize_samples(values: Iterable[float]) -> dict[str, float | int]:
    measured = [float(value) for value in values]
    if not measured:
        raise ValueError("at least one measured value is required")
    return {
        "runs": len(measured),
        "minimum": min(measured),
        "median": median(measured),
        "p95": nearest_rank(measured, 95),
        "maximum": max(measured),
    }


def has_query_scaling_regression(
    smaller_query_count: int,
    larger_query_count: int,
    *,
    tolerance: int = 5,
) -> bool:
    """Flag query growth that is inconsistent with a bounded hot path."""

    if min(smaller_query_count, larger_query_count, tolerance) < 0:
        raise ValueError("query counts and tolerance must be non-negative")
    return larger_query_count > smaller_query_count + tolerance


def validate_finding(finding: Mapping[str, object]) -> None:
    missing = [field for field in FINDING_REQUIRED_FIELDS if not finding.get(field)]
    if missing:
        raise ValueError(f"performance finding is missing: {', '.join(missing)}")
    if finding["proposed_severity"] not in FINDING_SEVERITIES:
        raise ValueError("invalid proposed performance severity")


def contract_snapshot() -> dict[str, object]:
    return {
        "datasets": {key: asdict(value) for key, value in DATASETS.items()},
        "api": {"warmup_runs": API_WARMUP_RUNS, "measured_runs": API_MEASURED_RUNS},
        "browser": {
            "warmup_runs": BROWSER_WARMUP_RUNS,
            "measured_runs": BROWSER_MEASURED_RUNS,
        },
        "concurrency_levels": list(CONCURRENCY_LEVELS),
        "sustained_minutes": SUSTAINED_MINUTES,
        "deep_dashboard_page": DEEP_DASHBOARD_PAGE,
        "required_database_vendor": REQUIRED_DATABASE_VENDOR,
    }
