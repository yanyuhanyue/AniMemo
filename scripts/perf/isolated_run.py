"""Run the complete AniMemo Wave 1 load/resource scenario in isolation."""

from __future__ import annotations

import argparse
import dataclasses
import json
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

try:
    from scripts.perf.contract import CONCURRENCY_LEVELS, SUSTAINED_MINUTES
    from scripts.perf.load_harness import (
        AuthenticatedHttpClient,
        EnvironmentCredentials,
        HarnessConfigurationError,
        build_read_scenario,
        run_concurrency_level,
        run_sustained,
        validate_target,
    )
    from scripts.perf.resource_sampler import (
        DockerResourceSampler,
        ResourceConfig,
        ResourceSample,
        evaluate_resource_failures,
        summarize_resources,
    )
except ModuleNotFoundError:  # Support ``python scripts/perf/isolated_run.py``.
    from contract import CONCURRENCY_LEVELS, SUSTAINED_MINUTES
    from load_harness import (
        AuthenticatedHttpClient,
        EnvironmentCredentials,
        HarnessConfigurationError,
        build_read_scenario,
        run_concurrency_level,
        run_sustained,
        validate_target,
    )
    from resource_sampler import (
        DockerResourceSampler,
        ResourceConfig,
        ResourceSample,
        evaluate_resource_failures,
        summarize_resources,
    )


@dataclass(frozen=True)
class VirtualUserIdentity:
    username: str
    access_token: str
    entry_id: int


def virtual_user_client_ip(worker_index: int) -> str:
    if worker_index < 0 or worker_index >= 254:
        raise HarnessConfigurationError("isolated virtual-user index is outside the test address range")
    return f"198.18.0.{worker_index + 1}"


def load_virtual_user_identities(path: Path, *, required_count: int) -> tuple[VirtualUserIdentity, ...]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise HarnessConfigurationError(f"unable to read isolated identities file: {error}") from error
    raw_identities = payload.get("identities") if isinstance(payload, dict) else None
    if not isinstance(raw_identities, list):
        raise HarnessConfigurationError("isolated identities file must contain an identities list")

    identities = []
    for item in raw_identities:
        if not isinstance(item, dict):
            raise HarnessConfigurationError("each isolated identity must be an object")
        username = str(item.get("username") or "").strip()
        access_token = str(item.get("access_token") or "").strip()
        try:
            entry_id = int(item.get("entry_id"))
        except (TypeError, ValueError) as error:
            raise HarnessConfigurationError("each isolated identity requires a positive entry_id") from error
        if not username or not access_token or entry_id <= 0:
            raise HarnessConfigurationError("each isolated identity requires username, access_token, and entry_id")
        identities.append(VirtualUserIdentity(username, access_token, entry_id))

    if len(identities) < required_count:
        raise HarnessConfigurationError(
            f"isolated identities file provides {len(identities)} users; {required_count} are required"
        )
    if len({item.username for item in identities}) != len(identities):
        raise HarnessConfigurationError("isolated virtual-user usernames must be unique")
    if len({item.access_token for item in identities}) != len(identities):
        raise HarnessConfigurationError("isolated virtual-user access tokens must be unique")
    if len({item.entry_id for item in identities}) != len(identities):
        raise HarnessConfigurationError("isolated virtual-user entry ids must be unique")
    return tuple(identities)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--confirm-isolated", action="store_true", required=True)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--identities-file", type=Path, required=True)
    parser.add_argument("--staff-username", default="")
    parser.add_argument("--staff-password-env", default="")
    parser.add_argument("--staff-otp-env", default="")
    parser.add_argument("--staff-challenge-env", default="")
    parser.add_argument("--search-term", default="anime")
    parser.add_argument("--iterations-per-user", type=int, default=2)
    parser.add_argument("--sustained-concurrency", type=int, choices=CONCURRENCY_LEVELS, default=5)
    parser.add_argument("--duration-seconds", type=float, default=SUSTAINED_MINUTES * 60)
    parser.add_argument("--think-time-seconds", type=float, default=0.35)
    parser.add_argument("--timeout-seconds", type=float, default=20.0)
    parser.add_argument("--insecure-tls", action="store_true")
    parser.add_argument("--compose-project", required=True)
    parser.add_argument("--postgres-user", required=True)
    parser.add_argument("--postgres-database", required=True)
    parser.add_argument("--api-container", default="")
    parser.add_argument("--web-container", default="")
    parser.add_argument("--postgres-container", default="")
    parser.add_argument("--redis-container", default="")
    parser.add_argument("--resource-interval-seconds", type=float, default=15.0)
    parser.add_argument("--api-memory-growth-limit-mib", type=int, default=512)
    parser.add_argument("--redis-memory-growth-limit-mib", type=int, default=256)
    parser.add_argument("--redis-key-growth-limit", type=int, default=50_000)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def _resource_worker(
    sampler: DockerResourceSampler,
    *,
    interval_seconds: float,
    stop_event: threading.Event,
    samples: list[ResourceSample],
    errors: list[str],
) -> None:
    try:
        while True:
            samples.append(sampler.sample())
            if stop_event.wait(interval_seconds):
                break
        samples.append(sampler.sample())
    except Exception as error:
        errors.append(f"{error.__class__.__name__}: {error}")
        stop_event.set()


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    base_url = validate_target(args.base_url)
    if args.duration_seconds <= 0 or args.resource_interval_seconds <= 0:
        raise HarnessConfigurationError("duration and resource interval must be positive")

    identities = load_virtual_user_identities(
        args.identities_file,
        required_count=max(CONCURRENCY_LEVELS),
    )
    staff_credentials = None
    if args.staff_username or args.staff_password_env:
        staff_credentials = EnvironmentCredentials.from_environment(
            username=args.staff_username,
            password_environment=args.staff_password_env,
            otp_environment=args.staff_otp_env,
            challenge_environment=args.staff_challenge_env,
        )

    bootstrap_client = AuthenticatedHttpClient(
        base_url=base_url,
        user_credentials=EnvironmentCredentials(identities[0].username, "preissued-token-only"),
        staff_credentials=staff_credentials,
        timeout_seconds=args.timeout_seconds,
        insecure_tls=args.insecure_tls,
        client_ip="198.18.255.254",
    )
    shared_tokens = {}
    if staff_credentials is not None:
        shared_tokens["staff"] = bootstrap_client.authenticate("staff")
    token_lock = threading.Lock()

    def token_provider(scope: str) -> str:
        with token_lock:
            return shared_tokens[scope]

    def token_refresher(scope: str, rejected_token: str) -> str:
        with token_lock:
            if shared_tokens.get(scope) != rejected_token:
                return shared_tokens[scope]
            shared_tokens[scope] = bootstrap_client.refresh(scope)
            return shared_tokens[scope]

    def client_factory(worker_index: int) -> AuthenticatedHttpClient:
        identity = identities[worker_index]
        return AuthenticatedHttpClient(
            base_url=base_url,
            user_credentials=EnvironmentCredentials(identity.username, "preissued-token-only"),
            staff_credentials=staff_credentials,
            timeout_seconds=args.timeout_seconds,
            insecure_tls=args.insecure_tls,
            client_ip=virtual_user_client_ip(worker_index),
            initial_tokens={"user": identity.access_token, **shared_tokens},
            token_provider=token_provider,
            token_refresher=token_refresher,
        )

    def request_factory(worker_index: int):
        return build_read_scenario(
            entry_id=identities[worker_index].entry_id,
            search_term=args.search_term,
            include_staff=staff_credentials is not None,
        )

    resource_sampler = DockerResourceSampler(
        ResourceConfig(
            compose_project=args.compose_project,
            postgres_user=args.postgres_user,
            postgres_database=args.postgres_database,
            api_container=args.api_container,
            web_container=args.web_container,
            postgres_container=args.postgres_container,
            redis_container=args.redis_container,
            confirm_isolated=True,
        )
    )
    resource_samples: list[ResourceSample] = []
    resource_errors: list[str] = []
    stop_event = threading.Event()
    sampler_thread = threading.Thread(
        target=_resource_worker,
        kwargs={
            "sampler": resource_sampler,
            "interval_seconds": args.resource_interval_seconds,
            "stop_event": stop_event,
            "samples": resource_samples,
            "errors": resource_errors,
        },
        name="animemo-resource-sampler",
        daemon=True,
    )

    load_summaries = []
    started = time.monotonic()
    sampler_thread.start()
    try:
        for concurrency in CONCURRENCY_LEVELS:
            load_summaries.append(
                run_concurrency_level(
                    client_factory=client_factory,
                    request_factory=request_factory,
                    concurrency=concurrency,
                    iterations_per_user=args.iterations_per_user,
                )
            )
        load_summaries.append(
            run_sustained(
                client_factory=client_factory,
                request_factory=request_factory,
                concurrency=args.sustained_concurrency,
                duration_seconds=args.duration_seconds,
                think_time_seconds=args.think_time_seconds,
            )
        )
    finally:
        stop_event.set()
        sampler_thread.join(timeout=max(30.0, args.resource_interval_seconds + 10.0))
    elapsed_seconds = time.monotonic() - started

    resource_failures = evaluate_resource_failures(
        resource_samples,
        memory_growth_limit_bytes=args.api_memory_growth_limit_mib * 1024 * 1024,
        redis_memory_growth_limit_bytes=args.redis_memory_growth_limit_mib * 1024 * 1024,
        redis_key_growth_limit=args.redis_key_growth_limit,
    )
    if resource_errors or sampler_thread.is_alive():
        resource_failures.append("resource_sampling_incomplete")

    report: dict[str, Any] = {
        "schema_version": "animemo-performance-isolated-v1.0",
        "authority": "authoritative only on isolated Ubuntu + PostgreSQL + Redis",
        "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "target": base_url,
        "elapsed_seconds": round(elapsed_seconds, 3),
        "contract": {
            "concurrency_levels": list(CONCURRENCY_LEVELS),
            "sustained_minutes": SUSTAINED_MINUTES,
            "configured_duration_seconds": args.duration_seconds,
        },
        "virtual_users": {
            "provided": len(identities),
            "unique_usernames": len({identity.username for identity in identities}),
            "unique_entry_ids": len({identity.entry_id for identity in identities}),
        },
        "scenario": [dataclasses.asdict(request) for request in request_factory(0)],
        "load": {
            "runs": [summary.to_dict() for summary in load_summaries],
            "hard_failures": sorted({reason for summary in load_summaries for reason in summary.hard_failures}),
        },
        "resources": {
            "summary": summarize_resources(resource_samples),
            "hard_failures": sorted(set(resource_failures)),
            "sampling_errors": resource_errors,
            "samples": [dataclasses.asdict(sample) for sample in resource_samples],
        },
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if report["load"]["hard_failures"] or report["resources"]["hard_failures"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
