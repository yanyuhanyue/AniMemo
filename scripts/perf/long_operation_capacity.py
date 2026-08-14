"""Measure AniMemo normal traffic while isolated provider calls occupy workers.

The runner sends real HTTP requests to a disposable Compose candidate. The
provider-latency route is enabled only in that isolated environment and sleeps
locally; it never contacts Bangumi or any other external service.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import dataclasses
import json
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    from scripts.perf.contract import nearest_rank
    from scripts.perf.isolated_run import (
        VirtualUserIdentity,
        load_virtual_user_identities,
    )
    from scripts.perf.load_harness import HarnessConfigurationError, validate_target
    from scripts.perf.resource_sampler import (
        DockerResourceSampler,
        ResourceConfig,
        ResourceSample,
        summarize_resources,
    )
except ModuleNotFoundError:  # Support ``python scripts/perf/long_operation_capacity.py``.
    from contract import nearest_rank
    from isolated_run import VirtualUserIdentity, load_virtual_user_identities
    from load_harness import HarnessConfigurationError, validate_target
    from resource_sampler import (
        DockerResourceSampler,
        ResourceConfig,
        ResourceSample,
        summarize_resources,
    )


NORMAL_USER_LEVELS = (20, 40, 60)
LONG_OPERATION_LEVELS = (0, 2, 4, 8)
PROVIDER_PATH = "/api/v1/_isolated/capacity/provider-latency/"
GUNICORN_WORKERS = 2
GUNICORN_THREADS = 4
USER_THROTTLE_RATE_PER_MINUTE = 300
WARMUP_ITERATIONS_PER_USER = 1
NORMAL_SCENARIO = (
    ("dashboard", "/api/v1/entries/?page=1&page_size=48&include_facets=1"),
    ("entry_detail", "/api/v1/entries/{entry_id}/"),
    ("watch_history", "/api/v1/entries/{entry_id}/watch-history/?page=1&page_size=48"),
    ("plugin_page", "/api/v1/plugins/enabled/"),
)


def maximum_matrix_requests_per_identity(iterations_per_user: int) -> int:
    if iterations_per_user <= 0:
        raise HarnessConfigurationError("iterations per user must be positive")
    measured_normal_requests = (
        len(NORMAL_USER_LEVELS)
        * len(LONG_OPERATION_LEVELS)
        * len(NORMAL_SCENARIO)
        * iterations_per_user
    )
    warmup_normal_requests = (
        len(NORMAL_USER_LEVELS)
        * len(LONG_OPERATION_LEVELS)
        * len(NORMAL_SCENARIO)
        * WARMUP_ITERATIONS_PER_USER
    )
    long_requests = len(NORMAL_USER_LEVELS) * sum(
        1 for level in LONG_OPERATION_LEVELS if level > 0
    )
    return measured_normal_requests + warmup_normal_requests + long_requests


MAX_MATRIX_REQUESTS_PER_IDENTITY = maximum_matrix_requests_per_identity(2)


@dataclass(frozen=True)
class HttpResult:
    kind: str
    journey: str
    status_code: int | None
    latency_ms: float
    response_bytes: int
    error: str = ""
    timed_out: bool = False
    payload: dict[str, Any] | None = None


class InFlightTracker:
    def __init__(self) -> None:
        self.active = 0
        self.peak = 0
        self.lock = threading.Lock()

    def enter(self) -> None:
        with self.lock:
            self.active += 1
            self.peak = max(self.peak, self.active)

    def leave(self) -> None:
        with self.lock:
            self.active -= 1


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        return None


_NO_REDIRECT_OPENER = urllib.request.build_opener(_RejectRedirects())


def _http_request(
    *,
    base_url: str,
    token: str,
    path: str,
    method: str,
    kind: str,
    journey: str,
    timeout_seconds: float,
) -> HttpResult:
    request = urllib.request.Request(
        urllib.parse.urljoin(f"{base_url}/", path.lstrip("/")),
        data=b"{}" if method == "POST" else None,
        method=method,
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "AniMemo-Isolated-Capacity/1.0",
        },
    )
    started = time.perf_counter()
    try:
        with _NO_REDIRECT_OPENER.open(request, timeout=timeout_seconds) as response:
            body = response.read()
            try:
                payload = json.loads(body.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeError):
                payload = None
            return HttpResult(
                kind,
                journey,
                response.status,
                (time.perf_counter() - started) * 1000,
                len(body),
                payload=payload if isinstance(payload, dict) else None,
            )
    except urllib.error.HTTPError as error:
        try:
            body = error.read()
        except OSError:
            body = b""
        return HttpResult(kind, journey, error.code, (time.perf_counter() - started) * 1000, len(body))
    except Exception as error:  # noqa: BLE001 - transport failures are capacity evidence.
        message = f"{error.__class__.__name__}: {error}"
        return HttpResult(
            kind,
            journey,
            None,
            (time.perf_counter() - started) * 1000,
            0,
            error=message,
            timed_out=isinstance(error, TimeoutError) or "timed out" in message.lower(),
        )


def _latency_summary(results: Sequence[HttpResult]) -> dict[str, float | int]:
    latencies = [item.latency_ms for item in results]
    return {
        "requests": len(results),
        "p50_ms": round(nearest_rank(latencies, 50), 3) if latencies else 0.0,
        "p95_ms": round(nearest_rank(latencies, 95), 3) if latencies else 0.0,
        "p99_ms": round(nearest_rank(latencies, 99), 3) if latencies else 0.0,
        "maximum_ms": round(max(latencies), 3) if latencies else 0.0,
    }


def _resource_worker(
    sampler: DockerResourceSampler,
    *,
    interval_seconds: float,
    stop_event: threading.Event,
    ready_event: threading.Event,
    samples: list[ResourceSample],
    errors: list[str],
    lock: threading.Lock,
    load_started: threading.Event,
    load_finished: threading.Event,
    active_window_samples: list[int],
) -> None:
    try:
        initial_sample = sampler.sample()
        with lock:
            samples.append(initial_sample)
        ready_event.set()
        while not load_started.wait(0.05):
            if stop_event.is_set():
                return
        while not load_finished.is_set():
            sample = sampler.sample()
            with lock:
                samples.append(sample)
                if not load_finished.is_set():
                    active_window_samples[0] += 1
            if stop_event.wait(interval_seconds):
                break
        final_sample = sampler.sample()
        with lock:
            samples.append(final_sample)
    except Exception as error:  # noqa: BLE001 - sampler failures must fail the evidence cell.
        with lock:
            errors.append(f"{error.__class__.__name__}: {error}")
        ready_event.set()
        stop_event.set()


def inspect_gunicorn_capacity(
    api_container: str,
    *,
    run_command: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict[str, Any]:
    completed = run_command(
        ["docker", "inspect", "--format", "{{json .Config.Cmd}}", api_container],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    command = json.loads(completed.stdout)
    if not isinstance(command, list):
        raise HarnessConfigurationError("isolated API container command is not a JSON list")

    def option_value(name: str) -> int:
        try:
            return int(command[command.index(name) + 1])
        except (ValueError, IndexError) as error:
            raise HarnessConfigurationError(f"isolated Gunicorn command is missing {name}") from error

    workers = option_value("--workers")
    threads = option_value("--threads")
    if workers != GUNICORN_WORKERS or threads != GUNICORN_THREADS:
        raise HarnessConfigurationError(
            f"capacity evidence requires Gunicorn {GUNICORN_WORKERS}x{GUNICORN_THREADS}; got {workers}x{threads}"
        )
    return {
        "command": command,
        "workers": workers,
        "threads_per_worker": threads,
        "worker_capacity": workers * threads,
    }


def run_capacity_cell(
    *,
    base_url: str,
    identities: Sequence[VirtualUserIdentity],
    normal_users: int,
    long_operations: int,
    iterations_per_user: int,
    timeout_seconds: float,
    worker_capacity: int,
    resource_sampler: DockerResourceSampler | None = None,
    resource_interval_seconds: float = 0.5,
    request_function: Callable[..., HttpResult] = _http_request,
) -> dict[str, Any]:
    if normal_users not in NORMAL_USER_LEVELS or long_operations not in LONG_OPERATION_LEVELS:
        raise HarnessConfigurationError("capacity cell is outside the required matrix")
    if len(identities) < normal_users or iterations_per_user <= 0:
        raise HarnessConfigurationError("capacity cell requires enough identities and positive iterations")
    if timeout_seconds <= 0 or resource_interval_seconds <= 0 or worker_capacity <= 0:
        raise HarnessConfigurationError("timeouts, resource interval, and worker capacity must be positive")

    participants = normal_users + long_operations
    load_started = threading.Event()
    load_finished = threading.Event()
    barrier = threading.Barrier(participants + 1, action=load_started.set)
    tracker = InFlightTracker()
    resource_samples: list[ResourceSample] = []
    resource_errors: list[str] = []
    active_window_samples = [0]
    resource_lock = threading.Lock()
    resource_stop = threading.Event()
    resource_ready = threading.Event()
    sampler_thread = None
    if resource_sampler is not None:
        sampler_thread = threading.Thread(
            target=_resource_worker,
            kwargs={
                "sampler": resource_sampler,
                "interval_seconds": resource_interval_seconds,
                "stop_event": resource_stop,
                "ready_event": resource_ready,
                "samples": resource_samples,
                "errors": resource_errors,
                "lock": resource_lock,
                "load_started": load_started,
                "load_finished": load_finished,
                "active_window_samples": active_window_samples,
            },
            name=f"capacity-resource-{normal_users}-{long_operations}",
            daemon=True,
        )
        sampler_thread.start()
        if not resource_ready.wait(timeout=130):
            raise RuntimeError("resource sampler did not complete its initial sample")
        with resource_lock:
            initial_resource_errors = list(resource_errors)
        if initial_resource_errors:
            raise RuntimeError(f"resource sampler failed before capacity cell: {initial_resource_errors}")

    def tracked_request(**kwargs) -> HttpResult:
        tracker.enter()
        try:
            return request_function(**kwargs)
        finally:
            tracker.leave()

    def normal_worker(worker_index: int) -> list[HttpResult]:
        identity = identities[worker_index]
        scenario = tuple(
            (name, path.format(entry_id=identity.entry_id))
            for name, path in NORMAL_SCENARIO
        )
        try:
            barrier.wait(timeout=30)
            return [
                tracked_request(
                    base_url=base_url,
                    token=identity.access_token,
                    path=path,
                    method="GET",
                    kind="normal",
                    journey=name,
                    timeout_seconds=timeout_seconds,
                )
                for _ in range(iterations_per_user)
                for name, path in scenario
            ]
        except Exception as error:
            message = f"{error.__class__.__name__}: {error}"
            return [HttpResult("normal", "worker_failure", None, 0.0, 0, error=message)]

    def long_worker(worker_index: int) -> HttpResult:
        identity = identities[worker_index % normal_users]
        try:
            barrier.wait(timeout=30)
            return tracked_request(
                base_url=base_url,
                token=identity.access_token,
                path=PROVIDER_PATH,
                method="POST",
                kind="long_operation",
                journey="provider_latency",
                timeout_seconds=timeout_seconds,
            )
        except Exception as error:
            message = f"{error.__class__.__name__}: {error}"
            return HttpResult("long_operation", "worker_failure", None, 0.0, 0, error=message)

    started = time.monotonic()
    normal_elapsed_seconds = 0.0
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, participants)) as executor:
            normal_futures = [executor.submit(normal_worker, index) for index in range(normal_users)]
            long_futures = [executor.submit(long_worker, index) for index in range(long_operations)]
            try:
                barrier.wait(timeout=30)
            except threading.BrokenBarrierError:
                barrier.abort()
            normal_results = [item for future in normal_futures for item in future.result()]
            normal_elapsed_seconds = max(0.0, time.monotonic() - started)
            long_results = [future.result() for future in long_futures]
    finally:
        load_started.set()
        load_finished.set()
        resource_stop.set()
        if sampler_thread is not None:
            sampler_thread.join(timeout=max(130.0, resource_interval_seconds + 125.0))
    elapsed_seconds = max(0.0, time.monotonic() - started)
    with resource_lock:
        resource_samples_snapshot = list(resource_samples)
        resource_errors_snapshot = list(resource_errors)
    all_results = [*normal_results, *long_results]
    status_counts = Counter(str(item.status_code) if item.status_code is not None else "transport_error" for item in all_results)
    journeys = {
        journey: _latency_summary([item for item in normal_results if item.journey == journey])
        for journey, _path in NORMAL_SCENARIO
    }
    hard_failures = []
    if any(item.status_code is not None and item.status_code >= 500 for item in all_results):
        hard_failures.append("http_5xx")
    if any(item.status_code == 429 for item in all_results):
        hard_failures.append("http_429")
    if any(
        item.status_code is not None and item.status_code not in {200, 429}
        for item in all_results
    ):
        hard_failures.append("unexpected_http_status")
    if any(item.timed_out for item in all_results):
        hard_failures.append("request_timeout")
    if any(item.error and not item.timed_out for item in all_results):
        hard_failures.append("transport_error")
    if resource_errors_snapshot or (sampler_thread is not None and sampler_thread.is_alive()):
        hard_failures.append("resource_sampling_incomplete")
    if resource_sampler is not None and active_window_samples[0] < 1:
        hard_failures.append("resource_sampling_no_active_window")
    if any(
        item.payload is None
        or item.payload.get("provider") != "fake-bangumi-provider"
        or item.payload.get("network") != "disabled"
        for item in long_results
        if item.status_code == 200
    ):
        hard_failures.append("invalid_provider_stub_response")

    peak_in_flight = tracker.peak
    return {
        "normal_users": normal_users,
        "long_operations": long_operations,
        "iterations_per_user": iterations_per_user,
        "elapsed_seconds": round(elapsed_seconds, 3),
        "requests": len(all_results),
        "throughput_rps": round(len(all_results) / elapsed_seconds, 3) if elapsed_seconds else 0.0,
        "normal": {
            **_latency_summary(normal_results),
            "elapsed_seconds": round(normal_elapsed_seconds, 3),
            "throughput_rps": (
                round(len(normal_results) / normal_elapsed_seconds, 3)
                if normal_elapsed_seconds
                else 0.0
            ),
            "journeys": journeys,
        },
        "long_operation": {
            **_latency_summary(long_results),
            "throughput_rps": round(len(long_results) / elapsed_seconds, 3) if elapsed_seconds else 0.0,
            "reported_provider_latency_ms": sorted({
                item.payload.get("latency_ms")
                for item in long_results
                if item.payload and item.payload.get("latency_ms") is not None
            }),
        },
        "status_counts": dict(sorted(status_counts.items())),
        "timeouts": sum(item.timed_out for item in all_results),
        "http_5xx": sum(item.status_code is not None and item.status_code >= 500 for item in all_results),
        "http_429": sum(item.status_code == 429 for item in all_results),
        "transport_errors": sum(bool(item.error) for item in all_results),
        "capacity_pressure": {
            "configured_capacity": worker_capacity,
            "peak_client_in_flight": peak_in_flight,
            "offered_load_ratio": round(peak_in_flight / worker_capacity, 3),
            "client_queue_pressure": max(0, peak_in_flight - worker_capacity),
            "deterministic_long_operation_occupancy": round(long_operations / worker_capacity, 3),
            "note": "client offered load plus configured long-operation occupancy; not direct server worker telemetry",
        },
        "resources": {
            "source": "docker_stats_postgresql_redis" if resource_sampler is not None else "not_sampled",
            "active_window_samples": active_window_samples[0],
            "active_window_proven": resource_sampler is None or active_window_samples[0] > 0,
            "summary": summarize_resources(resource_samples_snapshot),
            "sampling_errors": resource_errors_snapshot,
            "samples": [dataclasses.asdict(sample) for sample in resource_samples_snapshot],
        },
        "hard_failures": sorted(set(hard_failures)),
    }


def attach_baseline_deltas(matrix: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    baselines = {
        int(cell["normal_users"]): cell
        for cell in matrix
        if int(cell["long_operations"]) == 0
    }
    annotated = []
    for source in matrix:
        cell = dict(source)
        baseline = baselines[int(cell["normal_users"])]
        normal = cell["normal"]
        baseline_normal = baseline["normal"]
        baseline_p95 = float(baseline_normal["p95_ms"])
        baseline_p99 = float(baseline_normal["p99_ms"])
        baseline_throughput = float(baseline_normal["throughput_rps"])
        deltas = {
            "normal_p50_ms": round(normal["p50_ms"] - baseline_normal["p50_ms"], 3),
            "normal_p95_ms": round(normal["p95_ms"] - baseline_normal["p95_ms"], 3),
            "normal_p99_ms": round(normal["p99_ms"] - baseline_normal["p99_ms"], 3),
            "normal_throughput_rps": round(
                normal["throughput_rps"] - baseline_normal["throughput_rps"],
                3,
            ),
            "normal_p95_percent": (
                round((normal["p95_ms"] / baseline_p95 - 1) * 100, 3)
                if baseline_p95
                else 0.0
            ),
            "normal_p99_percent": (
                round((normal["p99_ms"] / baseline_p99 - 1) * 100, 3)
                if baseline_p99
                else 0.0
            ),
            "normal_throughput_percent": (
                round((normal["throughput_rps"] / baseline_throughput - 1) * 100, 3)
                if baseline_throughput
                else 0.0
            ),
        }
        cell["baseline_delta"] = deltas
        cell["worker_saturation_evidence"] = {
            "configured_long_operation_occupancy": cell["capacity_pressure"][
                "deterministic_long_operation_occupancy"
            ],
            "baseline_delta": deltas,
            "direct_worker_telemetry": False,
            "decision": "manual_review_required",
            "note": "judge saturation from measured deltas, errors, resources, and the configured 2x4 worker capacity",
        }
        annotated.append(cell)
    return annotated


def capacity_execution_plan() -> tuple[dict[str, int | str], ...]:
    plan = []
    for normal_users in NORMAL_USER_LEVELS:
        for long_operations in LONG_OPERATION_LEVELS:
            plan.append({
                "phase": "warmup",
                "normal_users": normal_users,
                "long_operations": 0,
                "target_long_operations": long_operations,
                "iterations_per_user": WARMUP_ITERATIONS_PER_USER,
            })
            plan.append({
                "phase": "measured",
                "normal_users": normal_users,
                "long_operations": long_operations,
                "target_long_operations": long_operations,
                "iterations_per_user": 0,
            })
    return tuple(plan)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--confirm-isolated", action="store_true", required=True)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--identities-file", type=Path, required=True)
    parser.add_argument("--compose-project", required=True)
    parser.add_argument("--postgres-user", required=True)
    parser.add_argument("--postgres-database", required=True)
    parser.add_argument("--api-container", default="")
    parser.add_argument("--web-container", default="")
    parser.add_argument("--postgres-container", default="")
    parser.add_argument("--redis-container", default="")
    parser.add_argument("--iterations-per-user", type=int, default=2)
    parser.add_argument("--timeout-seconds", type=float, default=20.0)
    parser.add_argument("--resource-interval-seconds", type=float, default=0.5)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    base_url = validate_target(args.base_url)
    identities = load_virtual_user_identities(args.identities_file, required_count=max(NORMAL_USER_LEVELS))
    resource_config = ResourceConfig(
        compose_project=args.compose_project,
        postgres_user=args.postgres_user,
        postgres_database=args.postgres_database,
        api_container=args.api_container,
        web_container=args.web_container,
        postgres_container=args.postgres_container,
        redis_container=args.redis_container,
        confirm_isolated=True,
    )
    gunicorn = inspect_gunicorn_capacity(resource_config.containers()["api"])
    sampler = DockerResourceSampler(resource_config)
    warmups = []
    raw_matrix = []
    for step in capacity_execution_plan():
        is_warmup = step["phase"] == "warmup"
        cell = run_capacity_cell(
            base_url=base_url,
            identities=identities,
            normal_users=int(step["normal_users"]),
            long_operations=int(step["long_operations"]),
            iterations_per_user=(
                int(step["iterations_per_user"])
                if is_warmup
                else args.iterations_per_user
            ),
            timeout_seconds=args.timeout_seconds,
            worker_capacity=gunicorn["worker_capacity"],
            resource_sampler=None if is_warmup else sampler,
            resource_interval_seconds=args.resource_interval_seconds,
        )
        if is_warmup:
            warmups.append({
                "normal_users": step["normal_users"],
                "target_long_operations": step["target_long_operations"],
                "iterations_per_user": step["iterations_per_user"],
                "requests": cell["requests"],
                "status_counts": cell["status_counts"],
                "hard_failures": cell["hard_failures"],
            })
        else:
            raw_matrix.append(cell)
    matrix = attach_baseline_deltas(raw_matrix)
    report = {
        "schema_version": "animemo-isolated-long-operation-capacity-v2",
        "authority": "real HTTP against disposable Compose candidate; never production",
        "target": base_url,
        "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "provider": {
            "name": "fake-bangumi-provider",
            "endpoint": PROVIDER_PATH,
            "network": "disabled",
        },
        "gunicorn": gunicorn,
        "matrix_contract": {
            "normal_users": list(NORMAL_USER_LEVELS),
            "long_operations": list(LONG_OPERATION_LEVELS),
            "identities": len(identities),
            "barrier_synchronized": True,
            "iterations_per_user": args.iterations_per_user,
        },
        "warmup_contract": {
            "strategy": "normal-only warm-up immediately before every measured cell",
            "counted_in_matrix": False,
            "iterations_per_user": WARMUP_ITERATIONS_PER_USER,
            "warmups": warmups,
        },
        "throttle_budget": {
            "configured_user_rate": f"{USER_THROTTLE_RATE_PER_MINUTE}/min",
            "maximum_requests_per_identity": maximum_matrix_requests_per_identity(args.iterations_per_user),
            "within_configured_rate": (
                maximum_matrix_requests_per_identity(args.iterations_per_user)
                < USER_THROTTLE_RATE_PER_MINUTE
            ),
            "note": "distinct preissued identities prevent cross-user buckets; any observed 429 remains measured evidence",
        },
        "matrix": matrix,
        "hard_failures": sorted({
            reason
            for cell in [*matrix, *warmups]
            for reason in cell["hard_failures"]
        }),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if report["hard_failures"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
