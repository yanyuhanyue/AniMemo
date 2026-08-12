"""Evaluate deterministic AniMemo v1.0 performance regression evidence."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

try:
    from scripts.perf.contract import (
        API_MEASURED_RUNS,
        BROWSER_MEASURED_RUNS,
        CONCURRENCY_LEVELS,
        DEEP_DASHBOARD_PAGE,
        REQUIRED_DATABASE_VENDOR,
        SUSTAINED_MINUTES,
        has_query_scaling_regression,
    )
except ModuleNotFoundError:  # Support ``python scripts/perf/regression_gate.py``.
    from contract import (  # type: ignore
        API_MEASURED_RUNS,
        BROWSER_MEASURED_RUNS,
        CONCURRENCY_LEVELS,
        DEEP_DASHBOARD_PAGE,
        REQUIRED_DATABASE_VENDOR,
        SUSTAINED_MINUTES,
        has_query_scaling_regression,
    )


class RegressionGateError(ValueError):
    pass


def _read(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RegressionGateError(f"cannot read performance evidence {path}: {error}") from error
    if not isinstance(value, dict):
        raise RegressionGateError(f"performance evidence must be an object: {path}")
    return value


def _median(summary: Mapping[str, Any], field: str) -> float:
    value = summary.get(field)
    if not isinstance(value, Mapping) or "median" not in value:
        raise RegressionGateError(f"missing {field}.median")
    return float(value["median"])


def validate_frontend(report: Mapping[str, Any]) -> dict[str, Any]:
    failures: list[str] = []
    journeys = report.get("journeys")
    if not isinstance(journeys, Mapping) or not journeys:
        raise RegressionGateError("frontend evidence has no journeys")
    for name, journey in journeys.items():
        runs = journey.get("runs") if isinstance(journey, Mapping) else None
        measured = [run for run in (runs or []) if run.get("measured")]
        if len(measured) != BROWSER_MEASURED_RUNS:
            failures.append(f"frontend:{name}:measured_runs={len(measured)}")
        for run in measured:
            if run.get("failures"):
                failures.append(f"frontend:{name}:network_failure")
            if run.get("console_errors"):
                failures.append(f"frontend:{name}:console_error")
            requests = run.get("requests") or []
            if any(int(item.get("status") or 0) >= 500 for item in requests):
                failures.append(f"frontend:{name}:http_5xx")
            duplicates = run.get("duplicates") or []
            if any(
                item.get("classification") == "unexplained exact duplicate"
                and " /api/" in str(item.get("key") or "")
                for item in duplicates
            ):
                failures.append(f"frontend:{name}:critical_duplicate_request")

    staff = report.get("staff_polling") or {}
    for field in ("hidden_tab_suppression", "overlap_coalescing", "visible_return_refresh"):
        if staff.get(field) != "PASS":
            failures.append(f"frontend:staff_polling:{field}")

    update = report.get("update_operation_polling") or {}
    if update.get("hidden_tab_suppression") != "PASS":
        failures.append("frontend:update_polling:hidden_tab")
    if update.get("overlap_coalescing") != "PASS":
        failures.append("frontend:update_polling:overlap")
    if int((update.get("visible_slow_response") or {}).get("maximum_in_flight") or 0) > 1:
        failures.append("frontend:update_polling:multiple_in_flight")
    if int((update.get("hidden_tab") or {}).get("requests_after_one_interval") or 0) != 0:
        failures.append("frontend:update_polling:hidden_request")

    deep = report.get("dashboard_page_48") or {}
    expected_pages = list(range(1, DEEP_DASHBOARD_PAGE + 1))
    if deep.get("requested_pages") != expected_pages:
        failures.append("frontend:dashboard_page_48:topology")
    if int(deep.get("page_48_requests") or 0) != 1:
        failures.append("frontend:dashboard_page_48:duplicate_or_missing")
    if deep.get("exact_duplicate_requests"):
        failures.append("frontend:dashboard_page_48:duplicates")

    return {
        "status": "PASS" if not failures else "FAIL",
        "failures": sorted(set(failures)),
        "javascript_bytes": int((report.get("build_inventory") or {}).get("javascript_bytes") or 0),
        "journeys": sorted(journeys),
    }


def validate_backend(reports: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    failures: list[str] = []
    by_dataset: dict[str, Mapping[str, Any]] = {}
    for report in reports:
        database = report.get("database") or {}
        if report.get("mode") != "POSTGRESQL_AUTHORITATIVE":
            failures.append("backend:not_authoritative")
        if database.get("vendor") != REQUIRED_DATABASE_VENDOR or not database.get("authoritative"):
            failures.append("backend:not_postgresql")
        dataset = str((report.get("dataset") or {}).get("dataset") or "").upper()
        if not dataset:
            raise RegressionGateError("backend evidence is missing dataset identity")
        by_dataset[dataset] = report
        for name, probe in (report.get("probes") or {}).items():
            if int((probe.get("query_count") or {}).get("runs") or 0) != API_MEASURED_RUNS:
                failures.append(f"backend:{dataset}:{name}:measured_runs")
            if probe.get("status_codes") != [200]:
                failures.append(f"backend:{dataset}:{name}:http_status")

    if set(by_dataset) != {"SMALL", "MEDIUM", "LARGE"}:
        failures.append("backend:dataset_matrix")
    else:
        small = by_dataset["SMALL"].get("probes") or {}
        large = by_dataset["LARGE"].get("probes") or {}
        for name in sorted(set(small).intersection(large)):
            small_count = int(_median(small[name], "query_count"))
            large_count = int(_median(large[name], "query_count"))
            if has_query_scaling_regression(small_count, large_count):
                failures.append(f"backend:{name}:query_scaling:{small_count}->{large_count}")
            small_duplicates = int(_median(small[name], "duplicate_query_count"))
            large_duplicates = int(_median(large[name], "duplicate_query_count"))
            if has_query_scaling_regression(small_duplicates, large_duplicates):
                failures.append(
                    f"backend:{name}:duplicate_query_scaling:{small_duplicates}->{large_duplicates}"
                )
        deep = large.get("journal_page_48")
        if not deep or deep.get("status_codes") != [200]:
            failures.append("backend:journal_page_48:not_measured")

    return {
        "status": "PASS" if not failures else "FAIL",
        "failures": sorted(set(failures)),
        "datasets": sorted(by_dataset),
    }


def validate_resource(report: Mapping[str, Any]) -> dict[str, Any]:
    failures: list[str] = []
    load = report.get("load") or {}
    resources = report.get("resources") or {}
    virtual_users = report.get("virtual_users") or {}
    failures.extend(f"resource:load:{item}" for item in load.get("hard_failures") or [])
    failures.extend(f"resource:runtime:{item}" for item in resources.get("hard_failures") or [])
    if resources.get("sampling_errors"):
        failures.append("resource:sampling_error")

    required_virtual_users = max(CONCURRENCY_LEVELS)
    if int(virtual_users.get("provided") or 0) < required_virtual_users:
        failures.append("resource:virtual_users:insufficient")
    if int(virtual_users.get("unique_usernames") or 0) < required_virtual_users:
        failures.append("resource:virtual_users:shared_identity")
    if int(virtual_users.get("unique_entry_ids") or 0) < required_virtual_users:
        failures.append("resource:virtual_users:shared_entry")

    runs = load.get("runs") or []
    concurrency = sorted(
        int(run.get("concurrency") or 0)
        for run in runs
        if run.get("mode") == "concurrency"
    )
    if concurrency != list(CONCURRENCY_LEVELS):
        failures.append("resource:concurrency_matrix")
    sustained = [run for run in runs if run.get("mode") == "sustained"]
    if len(sustained) != 1:
        failures.append("resource:sustained_run")
    elif float(sustained[0].get("elapsed_seconds") or 0) < SUSTAINED_MINUTES * 60:
        failures.append("resource:sustained_duration")
    if any(int(run.get("errors") or 0) for run in runs):
        failures.append("resource:request_errors")
    if int((resources.get("summary") or {}).get("samples") or 0) < 4:
        failures.append("resource:insufficient_samples")

    return {
        "status": "PASS" if not failures else "FAIL",
        "failures": sorted(set(failures)),
        "concurrency_levels": concurrency,
        "sustained_seconds": float(sustained[0].get("elapsed_seconds") or 0) if sustained else 0,
        "virtual_users": {
            "provided": int(virtual_users.get("provided") or 0),
            "unique_usernames": int(virtual_users.get("unique_usernames") or 0),
            "unique_entry_ids": int(virtual_users.get("unique_entry_ids") or 0),
        },
    }


def evaluate(
    *,
    frontend: Mapping[str, Any] | None = None,
    backend: Sequence[Mapping[str, Any]] = (),
    resource: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    sections: dict[str, Any] = {}
    if frontend is not None:
        sections["frontend"] = validate_frontend(frontend)
    if backend:
        sections["backend"] = validate_backend(backend)
    if resource is not None:
        sections["resource"] = validate_resource(resource)
    if not sections:
        raise RegressionGateError("at least one performance evidence input is required")
    failed = [name for name, result in sections.items() if result["status"] != "PASS"]
    return {
        "schema_version": "animemo-performance-regression-v1.0",
        "status": "PASS" if not failed else "FAIL",
        "failed_sections": failed,
        "sections": sections,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--frontend", type=Path)
    parser.add_argument("--backend", type=Path, action="append", default=[])
    parser.add_argument("--resource", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    result = evaluate(
        frontend=_read(args.frontend) if args.frontend else None,
        backend=[_read(path) for path in args.backend],
        resource=_read(args.resource) if args.resource else None,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
