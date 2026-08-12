"""Read-only Docker/PostgreSQL/Redis sampler for isolated AniMemo load runs."""

from __future__ import annotations

import argparse
import dataclasses
import json
import re
import subprocess
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any


_SAFE_NAME = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$")
_MEMORY_UNITS = {
    "b": 1,
    "kb": 1000,
    "kib": 1024,
    "mb": 1000**2,
    "mib": 1024**2,
    "gb": 1000**3,
    "gib": 1024**3,
    "tb": 1000**4,
    "tib": 1024**4,
}


class ResourceConfigurationError(ValueError):
    pass


@dataclass(frozen=True)
class ResourceConfig:
    compose_project: str
    postgres_user: str
    postgres_database: str
    api_container: str = ""
    web_container: str = ""
    postgres_container: str = ""
    redis_container: str = ""
    confirm_isolated: bool = False

    def __post_init__(self) -> None:
        for label, value in (
            ("compose_project", self.compose_project),
            ("postgres_user", self.postgres_user),
            ("postgres_database", self.postgres_database),
        ):
            if not _SAFE_NAME.fullmatch(str(value or "")):
                raise ResourceConfigurationError(f"unsafe or missing {label}")
        if not self.confirm_isolated:
            raise ResourceConfigurationError("explicit isolated-environment confirmation is required")
        forbidden = {
            "anime-journal",
            "anime-journal-api",
            "anime-journal-web",
            "anime-journal-postgres",
            "anime-journal-redis",
        }
        supplied = {
            self.compose_project,
            self.api_container,
            self.web_container,
            self.postgres_container,
            self.redis_container,
        }
        if forbidden.intersection(value for value in supplied if value):
            raise ResourceConfigurationError("production AniMemo Compose/container names are forbidden")

    def containers(self) -> dict[str, str]:
        return {
            "api": self.api_container or f"{self.compose_project}-api",
            "web": self.web_container or f"{self.compose_project}-web",
            "postgres": self.postgres_container or f"{self.compose_project}-postgres",
            "redis": self.redis_container or f"{self.compose_project}-redis",
        }


@dataclass(frozen=True)
class ResourceSample:
    timestamp: float
    containers: dict[str, dict[str, float | int]]
    database_connections: int
    database_max_connections: int
    redis_memory_bytes: int
    redis_keys: int

    @classmethod
    def for_test(
        cls,
        *,
        api_memory: int = 0,
        api_cpu: float = 0.0,
        redis_memory: int = 0,
        redis_keys: int = 0,
        db_connections: int = 0,
        max_connections: int = 100,
    ) -> "ResourceSample":
        return cls(
            timestamp=0.0,
            containers={
                "api": {"cpu_percent": api_cpu, "memory_bytes": api_memory},
                "web": {"cpu_percent": 0.0, "memory_bytes": 0},
                "postgres": {"cpu_percent": 0.0, "memory_bytes": 0},
                "redis": {"cpu_percent": 0.0, "memory_bytes": redis_memory},
            },
            database_connections=db_connections,
            database_max_connections=max_connections,
            redis_memory_bytes=redis_memory,
            redis_keys=redis_keys,
        )


def _memory_bytes(value: str) -> int:
    match = re.fullmatch(r"\s*([0-9]+(?:\.[0-9]+)?)\s*([kmgt]?i?b)\s*", value, flags=re.IGNORECASE)
    if not match:
        raise ValueError(f"unrecognized memory value: {value}")
    return int(float(match.group(1)) * _MEMORY_UNITS[match.group(2).lower()])


def parse_docker_stats(payload: str) -> dict[str, dict[str, float | int]]:
    parsed: dict[str, dict[str, float | int]] = {}
    for raw_line in payload.splitlines():
        if not raw_line.strip():
            continue
        item = json.loads(raw_line)
        memory_used = str(item["MemUsage"]).split("/", 1)[0].strip()
        parsed[str(item["Name"])] = {
            "cpu_percent": float(str(item["CPUPerc"]).strip().rstrip("%")),
            "memory_bytes": _memory_bytes(memory_used),
        }
    return parsed


def parse_postgres_connections(payload: str) -> dict[str, int]:
    values = [part.strip() for part in payload.strip().split("|")]
    if len(values) != 2:
        raise ValueError("PostgreSQL connection sample must contain current|max")
    return {"connections": int(values[0]), "max_connections": int(values[1])}


def parse_redis_info(info_payload: str, dbsize_payload: str) -> dict[str, int]:
    values: dict[str, str] = {}
    for line in info_payload.splitlines():
        if ":" in line and not line.startswith("#"):
            key, value = line.split(":", 1)
            values[key.strip()] = value.strip()
    if "used_memory" not in values:
        raise ValueError("Redis INFO memory did not include used_memory")
    return {"used_memory_bytes": int(values["used_memory"]), "keys": int(dbsize_payload.strip())}


def _default_run_command(command: Sequence[str]) -> str:
    completed = subprocess.run(command, check=True, capture_output=True, text=True, timeout=30)
    return completed.stdout


class DockerResourceSampler:
    def __init__(
        self,
        config: ResourceConfig,
        *,
        run_command: Callable[[Sequence[str]], str] = _default_run_command,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.config = config
        self.run_command = run_command
        self.clock = clock

    def sample(self) -> ResourceSample:
        containers = self.config.containers()
        stats_payload = self.run_command(
            ["docker", "stats", "--no-stream", "--format", "{{ json . }}", *containers.values()]
        )
        raw_stats = parse_docker_stats(stats_payload)
        normalized: dict[str, dict[str, float | int]] = {}
        for service, configured_name in containers.items():
            candidates = [
                metrics
                for name, metrics in raw_stats.items()
                if name == configured_name or name.startswith(f"{configured_name}-") or name.startswith(f"{configured_name}_")
            ]
            if len(candidates) != 1:
                raise RuntimeError(f"expected exactly one isolated {service} container for {configured_name}")
            normalized[service] = candidates[0]

        postgres_payload = self.run_command(
            [
                "docker",
                "exec",
                containers["postgres"],
                "psql",
                "-X",
                "-A",
                "-t",
                "-U",
                self.config.postgres_user,
                "-d",
                self.config.postgres_database,
                "-c",
                "SELECT (SELECT count(*)::int FROM pg_stat_activity), current_setting('max_connections')::int;",
            ]
        )
        redis_info = self.run_command(
            ["docker", "exec", containers["redis"], "redis-cli", "--raw", "INFO", "memory"]
        )
        redis_size = self.run_command(
            ["docker", "exec", containers["redis"], "redis-cli", "--raw", "DBSIZE"]
        )
        postgres = parse_postgres_connections(postgres_payload)
        redis = parse_redis_info(redis_info, redis_size)
        return ResourceSample(
            timestamp=self.clock(),
            containers=normalized,
            database_connections=postgres["connections"],
            database_max_connections=postgres["max_connections"],
            redis_memory_bytes=redis["used_memory_bytes"],
            redis_keys=redis["keys"],
        )


def _is_sustained_growth(values: Sequence[int], growth_limit: int) -> bool:
    if len(values) < 4:
        return False
    return values[-1] - values[0] >= growth_limit and all(next_value >= value for value, next_value in zip(values, values[1:]))


def evaluate_resource_failures(
    samples: Sequence[ResourceSample],
    *,
    memory_growth_limit_bytes: int,
    redis_memory_growth_limit_bytes: int,
    redis_key_growth_limit: int,
) -> list[str]:
    if not samples:
        return []
    reasons: list[str] = []
    if any(
        sample.database_max_connections > 0
        and sample.database_connections >= sample.database_max_connections
        for sample in samples
    ):
        reasons.append("database_connection_exhaustion")
    api_memory = [int(sample.containers["api"]["memory_bytes"]) for sample in samples]
    redis_memory = [sample.redis_memory_bytes for sample in samples]
    redis_keys = [sample.redis_keys for sample in samples]
    if _is_sustained_growth(api_memory, memory_growth_limit_bytes):
        reasons.append("api_memory_runaway_growth")
    if _is_sustained_growth(redis_memory, redis_memory_growth_limit_bytes):
        reasons.append("redis_memory_runaway_growth")
    if _is_sustained_growth(redis_keys, redis_key_growth_limit):
        reasons.append("redis_key_runaway_growth")
    return reasons


def summarize_resources(samples: Sequence[ResourceSample]) -> dict[str, Any]:
    if not samples:
        return {"samples": 0, "containers": {}, "database": {}, "redis": {}}
    container_summary: dict[str, dict[str, float | int]] = {}
    for service in samples[0].containers:
        memories = [int(sample.containers[service]["memory_bytes"]) for sample in samples]
        cpus = [float(sample.containers[service]["cpu_percent"]) for sample in samples]
        container_summary[service] = {
            "start_memory_bytes": memories[0],
            "end_memory_bytes": memories[-1],
            "memory_growth_bytes": memories[-1] - memories[0],
            "peak_memory_bytes": max(memories),
            "peak_cpu_percent": max(cpus),
        }
    db_connections = [sample.database_connections for sample in samples]
    redis_memory = [sample.redis_memory_bytes for sample in samples]
    redis_keys = [sample.redis_keys for sample in samples]
    return {
        "samples": len(samples),
        "started_at_epoch": samples[0].timestamp,
        "ended_at_epoch": samples[-1].timestamp,
        "containers": container_summary,
        "database": {
            "start_connections": db_connections[0],
            "end_connections": db_connections[-1],
            "peak_connections": max(db_connections),
            "max_connections": max(sample.database_max_connections for sample in samples),
        },
        "redis": {
            "start_memory_bytes": redis_memory[0],
            "end_memory_bytes": redis_memory[-1],
            "memory_growth_bytes": redis_memory[-1] - redis_memory[0],
            "peak_memory_bytes": max(redis_memory),
            "start_keys": redis_keys[0],
            "end_keys": redis_keys[-1],
            "key_growth": redis_keys[-1] - redis_keys[0],
            "peak_keys": max(redis_keys),
        },
    }


def collect_samples(
    sampler: DockerResourceSampler,
    *,
    duration_seconds: float,
    interval_seconds: float,
    clock: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
) -> list[ResourceSample]:
    if duration_seconds <= 0 or interval_seconds <= 0:
        raise ResourceConfigurationError("duration and interval must be positive")
    started = clock()
    deadline = started + duration_seconds
    samples = [sampler.sample()]
    while clock() < deadline:
        sleeper(interval_seconds)
        samples.append(sampler.sample())
    return samples


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compose-project", required=True)
    parser.add_argument("--confirm-isolated", action="store_true", required=True)
    parser.add_argument("--postgres-user", required=True)
    parser.add_argument("--postgres-database", required=True)
    parser.add_argument("--api-container", default="")
    parser.add_argument("--web-container", default="")
    parser.add_argument("--postgres-container", default="")
    parser.add_argument("--redis-container", default="")
    parser.add_argument("--duration-seconds", type=float, default=25 * 60)
    parser.add_argument("--interval-seconds", type=float, default=15)
    parser.add_argument("--api-memory-growth-limit-mib", type=int, default=512)
    parser.add_argument("--redis-memory-growth-limit-mib", type=int, default=256)
    parser.add_argument("--redis-key-growth-limit", type=int, default=50_000)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    sampler = DockerResourceSampler(
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
    samples = collect_samples(
        sampler,
        duration_seconds=args.duration_seconds,
        interval_seconds=args.interval_seconds,
    )
    failures = evaluate_resource_failures(
        samples,
        memory_growth_limit_bytes=args.api_memory_growth_limit_mib * 1024 * 1024,
        redis_memory_growth_limit_bytes=args.redis_memory_growth_limit_mib * 1024 * 1024,
        redis_key_growth_limit=args.redis_key_growth_limit,
    )
    report = {
        "schema_version": "animemo-performance-resources-v1.0",
        "authority": "authoritative only on isolated Ubuntu + PostgreSQL + Redis",
        "config": {
            **dataclasses.asdict(sampler.config),
            "duration_seconds": args.duration_seconds,
            "interval_seconds": args.interval_seconds,
        },
        "summary": summarize_resources(samples),
        "hard_failures": failures,
        "samples": [dataclasses.asdict(sample) for sample in samples],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
