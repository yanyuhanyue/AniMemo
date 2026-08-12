from __future__ import annotations

import gzip
import hashlib
import http.client
import json
import os
import re
import shutil
import stat
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

from release.contract import API_REPOSITORY, WEB_REPOSITORY

from .commands import CommandRunner
from .errors import CommandFailed, StateError
from .state import _atomic_json, _atomic_text

PRODUCTION_APP_ROOT = Path("/opt/1panel/docker/compose/anime-journal/app")
PRODUCTION_DATA_ROOT = Path("/data/anime-journal")
PRODUCTION_STATE_ROOT = Path("/var/lib/animemo-updater")
MAX_BACKUP_AGE = timedelta(hours=24)
MIN_AVAILABLE_MEMORY = 512 * 1024 * 1024
HEALTH_PATHS = ("/health/", "/", "/login", "/api/schema/", "/api/docs/")
HTTP_5XX_LOG = re.compile(r'"\s5\d\d(?:\s|$)')
CRITICAL_LOG = re.compile(r"(?im)(?:^|\b)(?:traceback \(most recent call last\)|critical|fatal|panic)(?:\b|:)")


@dataclass(frozen=True)
class HostPaths:
    app_root: Path
    data_root: Path
    state_root: Path

    @classmethod
    def production(
        cls,
        *,
        app_root: Path = PRODUCTION_APP_ROOT,
        data_root: Path = PRODUCTION_DATA_ROOT,
        state_root: Path = PRODUCTION_STATE_ROOT,
    ):
        resolved = cls(app_root.resolve(), data_root.resolve(), state_root.resolve())
        if resolved != cls(PRODUCTION_APP_ROOT, PRODUCTION_DATA_ROOT, PRODUCTION_STATE_ROOT):
            raise ValueError("Production Update Agent paths are fixed")
        return resolved

    @classmethod
    def testing(cls, *, app: Path, data: Path, state: Path):
        resolved = cls(app.resolve(), data.resolve(), state.resolve())
        for value in resolved.__dict__.values():
            if not value.is_absolute():
                raise ValueError("Test paths must be absolute")
        return resolved


class ImmutableComposeDeployment:
    def __init__(
        self,
        paths: HostPaths,
        *,
        runner=None,
        stable_observations: int = 3,
        observation_seconds: int = 5,
        memory_info_path: Path = Path("/proc/meminfo"),
        http_probe=None,
    ):
        self.paths = paths
        self.runner = runner or CommandRunner()
        self.stable_observations = stable_observations
        self.observation_seconds = observation_seconds
        self.memory_info_path = memory_info_path
        self.http_probe = http_probe or self._http_probe
        self.compose_file = self.paths.app_root / "deploy" / "docker-compose.yml"
        self.env_file = self.paths.app_root / ".env.production"
        self.runtime_env = self.paths.state_root / "runtime-images.env"

    def _env_value(self, name: str, default: str = "") -> str:
        for raw_line in self.env_file.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            if key.strip() == name:
                return value.strip().strip("\"'")
        return default

    @staticmethod
    def _http_probe(path: str, *, host: str, port: int, forwarded_proto: str) -> int:
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            connection.request("GET", path, headers={"Host": host, "X-Forwarded-Proto": forwarded_proto})
            response = connection.getresponse()
            response.read()
            return response.status
        finally:
            connection.close()

    def _public_endpoint(self) -> tuple[str, int, str]:
        parsed = urlparse(self._env_value("FRONTEND_URL"))
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise StateError("AniMemo FRONTEND_URL is unavailable for local health checks")
        raw_port = self._env_value("ANIME_JOURNAL_PORT", "8088")
        try:
            port = int(raw_port)
        except ValueError as error:
            raise StateError("AniMemo loopback HTTP port is invalid") from error
        if not 1 <= port <= 65535:
            raise StateError("AniMemo loopback HTTP port is invalid")
        return parsed.hostname, port, parsed.scheme

    def _container_state(self, manifest: dict[str, object], service: str) -> tuple[str, int]:
        container = self._compose(manifest, "ps", "-q", service, timeout=30).stdout.strip()
        if not container:
            raise StateError(f"AniMemo {service} container is unavailable")
        result = self.runner.run(
            [
                "/usr/bin/docker", "inspect", "--format",
                "{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}} {{.RestartCount}}",
                container,
            ],
            timeout=30,
        ).stdout.split()
        if len(result) != 2:
            raise StateError(f"AniMemo {service} container state is invalid")
        try:
            restarts = int(result[1])
        except ValueError as error:
            raise StateError(f"AniMemo {service} container restart count is invalid") from error
        return result[0], restarts

    def _verify_http_paths(self, paths: tuple[str, ...]) -> None:
        host, port, forwarded_proto = self._public_endpoint()
        for path in paths:
            try:
                status = self.http_probe(path, host=host, port=port, forwarded_proto=forwarded_proto)
            except OSError as error:
                raise StateError(f"AniMemo HTTP contract failed for {path}") from error
            if status != 200:
                raise StateError(f"AniMemo HTTP contract failed for {path}: HTTP {status}")

    def _verify_recent_logs(self, manifest: dict[str, object], *, since: str) -> None:
        for service in ["api", "web"]:
            container = self._compose(manifest, "ps", "-q", service, timeout=30).stdout.strip()
            if not container:
                raise StateError(f"AniMemo {service} container is unavailable")
            result = self.runner.run(
                ["/usr/bin/docker", "logs", "--since", since, container],
                timeout=30,
            )
            logs = f"{result.stdout}\n{result.stderr}"
            if HTTP_5XX_LOG.search(logs) or CRITICAL_LOG.search(logs):
                raise StateError(f"AniMemo {service} logs failed the stable observation gate")

    @staticmethod
    def image_environment(manifest: dict[str, object]) -> dict[str, str]:
        return {
            "ANIMEMO_API_IMAGE": f"{API_REPOSITORY}@{manifest['images']['api']['digest']}",
            "ANIMEMO_WEB_IMAGE": f"{WEB_REPOSITORY}@{manifest['images']['web']['digest']}",
        }

    def _environment(self, manifest: dict[str, object]) -> dict[str, str]:
        return {**os.environ, **self.image_environment(manifest)}

    def _compose(self, manifest: dict[str, object], *args: str, timeout: int = 300):
        return self.runner.run(
            [
                "/usr/bin/docker", "compose",
                "--project-name", "anime-journal",
                "--env-file", str(self.env_file),
                "-f", str(self.compose_file),
                *args,
            ],
            cwd=self.paths.app_root,
            env=self._environment(manifest),
            timeout=timeout,
        )

    def preflight(self, manifest: dict[str, object]) -> None:
        for path in [self.compose_file, self.env_file]:
            if not path.is_file() or path.is_symlink():
                raise StateError(f"Required fixed AniMemo file is unavailable: {path.name}")
        usage = shutil.disk_usage(self.paths.data_root)
        if usage.free < 2 * 1024 * 1024 * 1024:
            raise StateError("AniMemo data root has less than 2 GiB free")
        try:
            memory_fields = {
                key: value
                for key, value in (
                    line.split(":", 1)
                    for line in self.memory_info_path.read_text(encoding="utf-8").splitlines()
                    if ":" in line
                )
            }
            available_kib = int(memory_fields["MemAvailable"].strip().split()[0])
        except (OSError, KeyError, ValueError, IndexError) as error:
            raise StateError("AniMemo host available memory cannot be determined") from error
        if available_kib * 1024 < MIN_AVAILABLE_MEMORY:
            raise StateError("AniMemo host has less than 512 MiB available memory")
        self.runner.run(["/usr/bin/docker", "version", "--format", "{{.Server.Version}}"], timeout=15)
        self._compose(manifest, "config", "--quiet", timeout=30)
        services = set(self._compose(manifest, "ps", "--services", "--filter", "status=running", timeout=30).stdout.split())
        if not {"postgres", "redis", "api", "web"}.issubset(services):
            raise StateError("AniMemo Compose project is not fully running")
        for service in ["postgres", "redis", "api", "web"]:
            status, _ = self._container_state(manifest, service)
            if status != "healthy":
                raise StateError(f"AniMemo {service} container is not healthy")
        self._verify_http_paths(("/health/", "/"))
        if manifest["compatibility"]["database"]["migration"]["required"]:
            backup_root = self.paths.data_root / "backups"
            if not backup_root.is_dir() or backup_root.is_symlink() or not os.access(backup_root, os.W_OK):
                raise StateError("AniMemo backup root is unavailable for a fresh migration backup")
        else:
            self.verify_recent_backup()

    def verify_recent_backup(self, *, now: datetime | None = None) -> None:
        backup_root = self.paths.data_root / "backups"
        if not backup_root.is_dir() or backup_root.is_symlink():
            raise StateError("AniMemo backup root is unavailable")
        metadata_files = sorted(backup_root.glob("*.json"), key=lambda item: item.stat().st_mtime, reverse=True)
        if not metadata_files:
            raise StateError("No verified AniMemo database backup metadata is available")
        metadata = metadata_files[0]
        metadata_stat = metadata.lstat()
        if metadata.is_symlink() or not stat.S_ISREG(metadata_stat.st_mode) or metadata_stat.st_nlink != 1:
            raise StateError("Latest backup metadata must not be a link")
        try:
            payload = json.loads(metadata.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise StateError("Latest backup metadata is invalid") from error
        if (
            not isinstance(payload, dict)
            or not isinstance(payload.get("path"), str)
            or not payload["path"]
            or not isinstance(payload.get("compressedSha256"), str)
        ):
            raise StateError("Latest backup metadata is invalid")
        backup = Path(payload["path"])
        if backup.parent.resolve() != backup_root.resolve() or not backup.is_file():
            raise StateError("Latest backup metadata points outside the AniMemo backup root")
        backup_stat = backup.lstat()
        if backup.is_symlink() or not stat.S_ISREG(backup_stat.st_mode) or backup_stat.st_nlink != 1:
            raise StateError("Latest AniMemo database backup must not be a link")
        digest = hashlib.sha256(backup.read_bytes()).hexdigest()
        if digest != payload.get("compressedSha256"):
            raise StateError("Latest AniMemo database backup checksum is invalid")
        try:
            created_at = datetime.fromisoformat(str(payload["createdAt"]).replace("Z", "+00:00"))
        except (KeyError, TypeError, ValueError) as error:
            raise StateError("Latest AniMemo database backup timestamp is invalid") from error
        if created_at.tzinfo is None:
            raise StateError("Latest AniMemo database backup timestamp is invalid")
        current_time = now or datetime.now(timezone.utc)
        age = current_time.astimezone(timezone.utc) - created_at.astimezone(timezone.utc)
        if age < timedelta(0) or age > MAX_BACKUP_AGE:
            raise StateError("Latest AniMemo database backup is older than 24 hours")
        try:
            with gzip.open(backup, "rb") as handle:
                while handle.read(1024 * 1024):
                    pass
        except (OSError, EOFError) as error:
            raise StateError("Latest AniMemo database backup gzip stream is invalid") from error

    def backup_database(self, operation_id: str) -> str:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup_root = self.paths.data_root / "backups"
        target = backup_root / f"animemo-pre-{operation_id[:8]}-{stamp}.sql.gz"
        result = self.runner.write_gzip(
            [
                "/usr/bin/docker", "compose",
                "--project-name", "anime-journal",
                "--env-file", str(self.env_file),
                "-f", str(self.compose_file),
                "exec", "-T", "postgres", "sh", "-c",
                'exec pg_dump --format=plain --no-owner --no-privileges -U "$POSTGRES_USER" "$POSTGRES_DB"',
            ],
            target,
            cwd=self.paths.app_root,
            timeout=600,
            root=self.paths.data_root,
        )
        compressed_digest = hashlib.sha256(target.read_bytes()).hexdigest()
        metadata = {
            "path": str(target),
            "createdAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "operationId": operation_id,
            "compressedSha256": compressed_digest,
            **result,
        }
        _atomic_json(
            target.with_suffix(target.suffix + ".json"),
            metadata,
            root=self.paths.data_root,
        )
        return str(target)

    def pull(self, manifest: dict[str, object]) -> None:
        env = self._environment(manifest)
        for image in [env["ANIMEMO_API_IMAGE"], env["ANIMEMO_WEB_IMAGE"]]:
            self.runner.run(["/usr/bin/docker", "pull", image], env=env, timeout=600)

    def migrate(self, manifest: dict[str, object]) -> None:
        self._compose(manifest, "run", "--rm", "--no-deps", "migration", timeout=600)

    def bootstrap(self, manifest: dict[str, object]) -> None:
        self._compose(manifest, "run", "--rm", "--no-deps", "bootstrap", timeout=600)

    def inspect_enabled_plugin_apis(self, manifest: dict[str, object]) -> set[int]:
        result = self._compose(
            manifest,
            "run", "--rm", "--no-deps", "api",
            "python", "manage.py", "list_enabled_plugin_apis",
            timeout=120,
        )
        try:
            values = json.loads(result.stdout.strip().splitlines()[-1])
        except (json.JSONDecodeError, IndexError) as error:
            raise StateError("Enabled Plugin SDK inspection returned invalid data") from error
        if not isinstance(values, list) or not all(isinstance(value, int) and value > 0 for value in values):
            raise StateError("Enabled Plugin SDK inspection returned invalid data")
        return set(values)

    def switch(self, manifest: dict[str, object]) -> None:
        env = self.image_environment(manifest)
        _atomic_text(
            self.runtime_env,
            f"ANIMEMO_API_IMAGE={env['ANIMEMO_API_IMAGE']}\nANIMEMO_WEB_IMAGE={env['ANIMEMO_WEB_IMAGE']}\n",
            root=self.paths.state_root,
        )
        self._compose(manifest, "up", "-d", "--no-deps", "--force-recreate", "api", "web", timeout=600)

    def verify_health(self, manifest: dict[str, object]) -> None:
        observation_started = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        for observation in range(self.stable_observations):
            for service in ["api", "web"]:
                status, restarts = self._container_state(manifest, service)
                if status != "healthy" or restarts != 0:
                    raise CommandFailed(f"{service} failed stable health observation")
            self._verify_http_paths(HEALTH_PATHS)
            self._verify_recent_logs(manifest, since=observation_started)
            if observation + 1 < self.stable_observations:
                time.sleep(self.observation_seconds)
