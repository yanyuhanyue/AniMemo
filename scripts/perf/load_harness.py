"""Safe, standard-library AniMemo HTTP load measurement harness.

The harness is intentionally limited to authenticated GET requests against an
explicit isolated target.  It records latency and error evidence without
inventing latency gates.  Production AniMemo hosts are rejected before any
network request is made.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import dataclasses
import http.cookiejar
import json
import os
import random
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    from scripts.perf.contract import (
        CONCURRENCY_LEVELS,
        SUSTAINED_MINUTES,
        nearest_rank,
    )
except ModuleNotFoundError:  # Support ``python scripts/perf/load_harness.py``.
    from contract import CONCURRENCY_LEVELS, SUSTAINED_MINUTES, nearest_rank


PRODUCTION_HOSTS = frozenset({"re-anime.cc", "www.re-anime.cc", "45.207.221.83"})
CONNECTION_EXHAUSTION_MARKERS = (
    "too many connections",
    "connection pool exhausted",
    "remaining connection slots are reserved",
    "cannot assign requested address",
    "resource temporarily unavailable",
)


class HarnessConfigurationError(ValueError):
    """Raised before network traffic when a load target is unsafe or incomplete."""


@dataclass(frozen=True)
class EnvironmentCredentials:
    username: str
    password: str
    otp: str = ""
    recovery_code: str = ""
    challenge: str = ""

    @classmethod
    def from_environment(
        cls,
        *,
        username: str,
        password_environment: str,
        otp_environment: str = "",
        recovery_code_environment: str = "",
        challenge_environment: str = "",
        environment: Mapping[str, str] | None = None,
    ) -> "EnvironmentCredentials":
        values = os.environ if environment is None else environment
        normalized_username = str(username or "").strip()
        password_name = str(password_environment or "").strip()
        if not normalized_username:
            raise HarnessConfigurationError("an explicit isolated-test username is required")
        if not password_name or not values.get(password_name):
            raise HarnessConfigurationError(
                f"isolated-test password environment variable is missing: {password_name or '<unset>'}"
            )
        return cls(
            username=normalized_username,
            password=values[password_name],
            otp=values.get(otp_environment, "") if otp_environment else "",
            recovery_code=values.get(recovery_code_environment, "") if recovery_code_environment else "",
            challenge=values.get(challenge_environment, "") if challenge_environment else "",
        )


@dataclass(frozen=True)
class ReadRequest:
    name: str
    path: str
    expected_statuses: tuple[int, ...] = (200,)
    method: str = "GET"
    auth_scope: str = "user"


@dataclass(frozen=True)
class RequestResult:
    journey: str
    status_code: int | None
    latency_ms: float
    response_bytes: int
    expected_statuses: tuple[int, ...]
    error: str = ""

    @property
    def failed(self) -> bool:
        return bool(self.error) or self.status_code not in self.expected_statuses


@dataclass(frozen=True)
class LoadSummary:
    mode: str
    concurrency: int
    requests: int
    errors: int
    error_rate: float
    http_5xx: int
    transport_errors: int
    p50_ms: float
    p95_ms: float
    elapsed_seconds: float
    throughput_rps: float
    journeys: dict[str, dict[str, float | int]]
    hard_failures: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        payload = dataclasses.asdict(self)
        payload["hard_failures"] = list(self.hard_failures)
        return payload


def validate_target(base_url: str) -> str:
    raw = str(base_url or "").strip()
    parsed = urllib.parse.urlsplit(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise HarnessConfigurationError("base URL must be an explicit http(s) URL")
    if parsed.username or parsed.password:
        raise HarnessConfigurationError("credentials must not be embedded in the base URL")
    hostname = parsed.hostname.rstrip(".").lower()
    if hostname in PRODUCTION_HOSTS or hostname.endswith(".re-anime.cc"):
        raise HarnessConfigurationError("production AniMemo targets are forbidden")
    if parsed.query or parsed.fragment:
        raise HarnessConfigurationError("base URL must not contain query or fragment components")
    normalized_path = parsed.path.rstrip("/")
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, normalized_path, "", ""))


def build_read_scenario(*, entry_id: int, search_term: str, include_staff: bool = False) -> tuple[ReadRequest, ...]:
    if int(entry_id) <= 0:
        raise HarnessConfigurationError("entry_id must identify an isolated seeded entry")
    encoded_search = urllib.parse.urlencode({"search": str(search_term or "").strip()})
    requests = [
        ReadRequest(
            "dashboard",
            "/api/v1/entries/?page=1&page_size=48&priority=1&ordering=-airing_period&include_facets=1",
        ),
        ReadRequest(
            "filter_search",
            f"/api/v1/entries/?page=1&page_size=48&priority=1&ordering=-airing_period&{encoded_search}",
        ),
        ReadRequest("entry_detail", f"/api/v1/entries/{int(entry_id)}/"),
        ReadRequest(
            "watch_history",
            f"/api/v1/entries/{int(entry_id)}/watch-history/?page=1&page_size=48",
        ),
        ReadRequest("plugin_page", "/api/v1/plugins/enabled/"),
    ]
    if include_staff:
        requests.append(ReadRequest("staff_health", "/api/v1/staff/system/health/", auth_scope="staff"))
    return tuple(requests)


def _summarize_journey(results: Sequence[RequestResult]) -> dict[str, float | int]:
    latencies = [item.latency_ms for item in results]
    errors = sum(item.failed for item in results)
    return {
        "requests": len(results),
        "errors": errors,
        "error_rate": round(errors / len(results), 6) if results else 0.0,
        "p50_ms": round(nearest_rank(latencies, 50), 3) if latencies else 0.0,
        "p95_ms": round(nearest_rank(latencies, 95), 3) if latencies else 0.0,
        "response_bytes": sum(item.response_bytes for item in results),
    }


def hard_failure_reasons(results: Sequence[RequestResult]) -> list[str]:
    reasons: list[str] = []
    if any(item.status_code is not None and item.status_code >= 500 for item in results):
        reasons.append("http_5xx")
    if any(item.status_code is not None and item.status_code not in item.expected_statuses for item in results):
        reasons.append("unexpected_http_status")
    transport_text = "\n".join(item.error.lower() for item in results if item.error)
    if any(marker in transport_text for marker in CONNECTION_EXHAUSTION_MARKERS):
        reasons.append("connection_exhaustion")
    return reasons


def summarize_results(
    *,
    mode: str,
    concurrency: int,
    results: Sequence[RequestResult],
    elapsed_seconds: float,
) -> LoadSummary:
    measured = list(results)
    latencies = [item.latency_ms for item in measured]
    errors = sum(item.failed for item in measured)
    grouped: dict[str, list[RequestResult]] = {}
    for item in measured:
        grouped.setdefault(item.journey, []).append(item)
    return LoadSummary(
        mode=mode,
        concurrency=concurrency,
        requests=len(measured),
        errors=errors,
        error_rate=round(errors / len(measured), 6) if measured else 0.0,
        http_5xx=sum(item.status_code is not None and item.status_code >= 500 for item in measured),
        transport_errors=sum(bool(item.error) for item in measured),
        p50_ms=round(nearest_rank(latencies, 50), 3) if latencies else 0.0,
        p95_ms=round(nearest_rank(latencies, 95), 3) if latencies else 0.0,
        elapsed_seconds=round(max(0.0, elapsed_seconds), 3),
        throughput_rps=round(len(measured) / elapsed_seconds, 3) if elapsed_seconds > 0 else 0.0,
        journeys={name: _summarize_journey(items) for name, items in sorted(grouped.items())},
        hard_failures=tuple(hard_failure_reasons(measured)),
    )


def run_concurrency_level(
    *,
    client_factory: Callable[[int], Any],
    requests: Sequence[ReadRequest] | None = None,
    request_factory: Callable[[int], Sequence[ReadRequest]] | None = None,
    concurrency: int,
    iterations_per_user: int = 1,
    clock: Callable[[], float] = time.monotonic,
) -> LoadSummary:
    if concurrency <= 0 or iterations_per_user <= 0:
        raise HarnessConfigurationError("concurrency, iterations, and requests must be positive")
    if (requests is None) == (request_factory is None):
        raise HarnessConfigurationError("provide exactly one of requests or request_factory")

    def worker(worker_index: int) -> list[RequestResult]:
        client = client_factory(worker_index)
        worker_requests = tuple(request_factory(worker_index) if request_factory else requests or ())
        if not worker_requests:
            raise HarnessConfigurationError("each virtual user requires at least one request")
        return [client.get(request) for _ in range(iterations_per_user) for request in worker_requests]

    started = clock()
    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as executor:
        result_groups = list(executor.map(worker, range(concurrency)))
    elapsed = max(0.0, clock() - started)
    return summarize_results(
        mode="concurrency",
        concurrency=concurrency,
        results=[item for group in result_groups for item in group],
        elapsed_seconds=elapsed,
    )


def run_sustained(
    *,
    client_factory: Callable[[int], Any],
    requests: Sequence[ReadRequest] | None = None,
    request_factory: Callable[[int], Sequence[ReadRequest]] | None = None,
    concurrency: int,
    duration_seconds: float,
    think_time_seconds: float = 0.35,
    clock: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
) -> LoadSummary:
    if concurrency <= 0 or duration_seconds <= 0:
        raise HarnessConfigurationError("concurrency, duration, and requests must be positive")
    if (requests is None) == (request_factory is None):
        raise HarnessConfigurationError("provide exactly one of requests or request_factory")
    started = clock()
    deadline = started + duration_seconds

    def worker(worker_index: int) -> list[RequestResult]:
        client = client_factory(worker_index)
        worker_requests = tuple(request_factory(worker_index) if request_factory else requests or ())
        if not worker_requests:
            raise HarnessConfigurationError("each virtual user requires at least one request")
        measured: list[RequestResult] = []
        offset = worker_index % len(worker_requests)
        while clock() < deadline:
            request = worker_requests[(offset + len(measured)) % len(worker_requests)]
            measured.append(client.get(request))
            if think_time_seconds > 0:
                sleeper(think_time_seconds)
        return measured

    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as executor:
        result_groups = list(executor.map(worker, range(concurrency)))
    elapsed = max(0.0, clock() - started)
    return summarize_results(
        mode="sustained",
        concurrency=concurrency,
        results=[item for group in result_groups for item in group],
        elapsed_seconds=elapsed,
    )


class AuthenticatedHttpClient:
    """One virtual user's isolated authenticated HTTP session."""

    def __init__(
        self,
        *,
        base_url: str,
        user_credentials: EnvironmentCredentials,
        staff_credentials: EnvironmentCredentials | None = None,
        timeout_seconds: float = 20.0,
        insecure_tls: bool = False,
        initial_tokens: Mapping[str, str] | None = None,
        token_provider: Callable[[str], str] | None = None,
        token_refresher: Callable[[str, str], str] | None = None,
    ) -> None:
        self.base_url = validate_target(base_url)
        self.user_credentials = user_credentials
        self.staff_credentials = staff_credentials
        self.timeout_seconds = timeout_seconds
        self.insecure_tls = insecure_tls
        self.token_provider = token_provider
        self.token_refresher = token_refresher
        self._tokens: dict[str, str] = {
            str(scope): str(token)
            for scope, token in (initial_tokens or {}).items()
            if str(token).strip()
        }
        self._openers: dict[str, urllib.request.OpenerDirector] = {}
        self._lock = threading.Lock()

    def _opener(self, scope: str) -> urllib.request.OpenerDirector:
        if scope not in self._openers:
            handlers: list[Any] = [urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())]
            if self.insecure_tls:
                context = ssl.create_default_context()
                context.check_hostname = False
                context.verify_mode = ssl.CERT_NONE
                handlers.append(urllib.request.HTTPSHandler(context=context))
            self._openers[scope] = urllib.request.build_opener(*handlers)
        return self._openers[scope]

    def _json_request(
        self,
        scope: str,
        path: str,
        *,
        method: str,
        payload: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> tuple[int, bytes, Mapping[str, str]]:
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        request_headers = {"Accept": "application/json", "User-Agent": "AniMemo-Perf-Harness/1.0"}
        if payload is not None:
            request_headers["Content-Type"] = "application/json"
        request_headers.update(headers or {})
        request = urllib.request.Request(
            urllib.parse.urljoin(f"{self.base_url}/", path.lstrip("/")),
            data=body,
            method=method,
            headers=request_headers,
        )
        try:
            with self._opener(scope).open(request, timeout=self.timeout_seconds) as response:
                return response.status, response.read(), response.headers
        except urllib.error.HTTPError as error:
            return error.code, error.read(), error.headers

    def _authenticate(self, scope: str) -> str:
        credentials = self.staff_credentials if scope == "staff" else self.user_credentials
        if credentials is None:
            raise HarnessConfigurationError("staff journey requested without explicit staff test credentials")
        csrf_status, csrf_body, _headers = self._json_request(scope, "/api/v1/auth/csrf/", method="GET")
        if csrf_status != 200:
            raise RuntimeError(f"CSRF bootstrap failed with HTTP {csrf_status}")
        csrf_token = str(json.loads(csrf_body.decode("utf-8")).get("csrf_token") or "")
        payload: dict[str, str | dict[str, str]] = {
            "username": credentials.username,
            "password": credentials.password,
        }
        if credentials.otp:
            payload["otp"] = credentials.otp
        if credentials.recovery_code:
            payload["recovery_code"] = credentials.recovery_code
        if credentials.challenge:
            payload["challenge"] = {"provider": "turnstile", "token": credentials.challenge}
            payload["cf-turnstile-response"] = credentials.challenge
        login_path = "/api/v1/auth/staff-login/" if scope == "staff" else "/api/v1/token/"
        status_code, body, _headers = self._json_request(
            scope,
            login_path,
            method="POST",
            payload=payload,
            headers={"X-CSRFToken": csrf_token},
        )
        if status_code != 200:
            raise RuntimeError(f"{scope} isolated-test login failed with HTTP {status_code}")
        token = str(json.loads(body.decode("utf-8")).get("access") or "")
        if not token:
            raise RuntimeError(f"{scope} isolated-test login returned no access token")
        self._tokens[scope] = token
        return token

    def authenticate(self, scope: str = "user") -> str:
        """Initialize one isolated credential once before measured traffic."""

        with self._lock:
            return self._tokens.get(scope) or self._authenticate(scope)

    def refresh(self, scope: str = "user") -> str:
        """Rotate an isolated test access token through its refresh cookie."""

        csrf_status, csrf_body, _headers = self._json_request(scope, "/api/v1/auth/csrf/", method="GET")
        if csrf_status != 200:
            raise RuntimeError(f"{scope} CSRF refresh bootstrap failed with HTTP {csrf_status}")
        csrf_token = str(json.loads(csrf_body.decode("utf-8")).get("csrf_token") or "")
        status_code, body, _headers = self._json_request(
            scope,
            "/api/v1/token/refresh/",
            method="POST",
            payload={},
            headers={"X-CSRFToken": csrf_token},
        )
        if status_code != 200:
            raise RuntimeError(f"{scope} isolated-test token refresh failed with HTTP {status_code}")
        token = str(json.loads(body.decode("utf-8")).get("access") or "")
        if not token:
            raise RuntimeError(f"{scope} isolated-test token refresh returned no access token")
        self._tokens[scope] = token
        return token

    def get(self, request: ReadRequest) -> RequestResult:
        started = time.perf_counter()
        try:
            with self._lock:
                token = self._tokens.get(request.auth_scope)
                if not token and self.token_provider:
                    token = self.token_provider(request.auth_scope)
                    self._tokens[request.auth_scope] = token
                if not token:
                    token = self._authenticate(request.auth_scope)
            status_code, body, _headers = self._json_request(
                request.auth_scope,
                request.path,
                method="GET",
                headers={"Authorization": f"Bearer {token}"},
            )
            if status_code == 401:
                if self.token_refresher:
                    token = self.token_refresher(request.auth_scope, token)
                    with self._lock:
                        self._tokens[request.auth_scope] = token
                else:
                    with self._lock:
                        token = self._authenticate(request.auth_scope)
                status_code, body, _headers = self._json_request(
                    request.auth_scope,
                    request.path,
                    method="GET",
                    headers={"Authorization": f"Bearer {token}"},
                )
            return RequestResult(
                journey=request.name,
                status_code=status_code,
                latency_ms=(time.perf_counter() - started) * 1000,
                response_bytes=len(body),
                expected_statuses=request.expected_statuses,
            )
        except Exception as error:  # transport evidence belongs in the report
            return RequestResult(
                journey=request.name,
                status_code=None,
                latency_ms=(time.perf_counter() - started) * 1000,
                response_bytes=0,
                expected_statuses=request.expected_statuses,
                error=f"{error.__class__.__name__}: {error}",
            )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True, help="explicit isolated environment URL")
    parser.add_argument("--username", required=True)
    parser.add_argument("--password-env", required=True)
    parser.add_argument("--otp-env", default="")
    parser.add_argument("--challenge-env", default="")
    parser.add_argument("--staff-username", default="")
    parser.add_argument("--staff-password-env", default="")
    parser.add_argument("--staff-otp-env", default="")
    parser.add_argument("--staff-recovery-code-env", default="")
    parser.add_argument("--staff-challenge-env", default="")
    parser.add_argument("--entry-id", required=True, type=int)
    parser.add_argument("--search-term", default="anime")
    parser.add_argument("--mode", choices=("concurrency", "sustained", "all"), default="all")
    parser.add_argument("--concurrency", type=int, choices=CONCURRENCY_LEVELS, default=5)
    parser.add_argument("--iterations-per-user", type=int, default=2)
    parser.add_argument("--duration-seconds", type=float, default=SUSTAINED_MINUTES * 60)
    parser.add_argument("--think-time-seconds", type=float, default=0.35)
    parser.add_argument("--timeout-seconds", type=float, default=20.0)
    parser.add_argument("--insecure-tls", action="store_true", help="isolated self-signed targets only")
    parser.add_argument(
        "--confirm-isolated",
        action="store_true",
        help="required acknowledgement that the target is disposable/non-production",
    )
    parser.add_argument("--output", required=True, type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if not args.confirm_isolated:
        raise HarnessConfigurationError("--confirm-isolated is required before network traffic")
    base_url = validate_target(args.base_url)
    user_credentials = EnvironmentCredentials.from_environment(
        username=args.username,
        password_environment=args.password_env,
        otp_environment=args.otp_env,
        challenge_environment=args.challenge_env,
    )
    staff_credentials = None
    if args.staff_username or args.staff_password_env:
        staff_credentials = EnvironmentCredentials.from_environment(
            username=args.staff_username,
            password_environment=args.staff_password_env,
            otp_environment=args.staff_otp_env,
            recovery_code_environment=args.staff_recovery_code_env,
            challenge_environment=args.staff_challenge_env,
        )
    scenario = build_read_scenario(
        entry_id=args.entry_id,
        search_term=args.search_term,
        include_staff=staff_credentials is not None,
    )

    bootstrap_client = AuthenticatedHttpClient(
        base_url=base_url,
        user_credentials=user_credentials,
        staff_credentials=staff_credentials,
        timeout_seconds=args.timeout_seconds,
        insecure_tls=args.insecure_tls,
    )
    initial_tokens = {"user": bootstrap_client.authenticate("user")}
    if staff_credentials is not None:
        initial_tokens["staff"] = bootstrap_client.authenticate("staff")
    token_lock = threading.Lock()

    def token_provider(scope: str) -> str:
        with token_lock:
            return initial_tokens[scope]

    def token_refresher(scope: str, rejected_token: str) -> str:
        with token_lock:
            if initial_tokens.get(scope) != rejected_token:
                return initial_tokens[scope]
            initial_tokens[scope] = bootstrap_client.refresh(scope)
            return initial_tokens[scope]

    def client_factory(_worker_index: int) -> AuthenticatedHttpClient:
        return AuthenticatedHttpClient(
            base_url=base_url,
            user_credentials=user_credentials,
            staff_credentials=staff_credentials,
            timeout_seconds=args.timeout_seconds,
            insecure_tls=args.insecure_tls,
            initial_tokens=initial_tokens,
            token_provider=token_provider,
            token_refresher=token_refresher,
        )

    summaries: list[LoadSummary] = []
    if args.mode in {"concurrency", "all"}:
        for level in CONCURRENCY_LEVELS:
            summaries.append(
                run_concurrency_level(
                    client_factory=client_factory,
                    requests=scenario,
                    concurrency=level,
                    iterations_per_user=args.iterations_per_user,
                )
            )
    if args.mode in {"sustained", "all"}:
        summaries.append(
            run_sustained(
                client_factory=client_factory,
                requests=scenario,
                concurrency=args.concurrency,
                duration_seconds=args.duration_seconds,
                think_time_seconds=args.think_time_seconds,
            )
        )

    report = {
        "schema_version": "animemo-performance-load-v1.0",
        "authority": "authoritative only on isolated Ubuntu + PostgreSQL + Redis",
        "target": base_url,
        "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "scenario": [dataclasses.asdict(request) for request in scenario],
        "runs": [summary.to_dict() for summary in summaries],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if any(summary.hard_failures for summary in summaries) else 0


if __name__ == "__main__":
    raise SystemExit(main())
