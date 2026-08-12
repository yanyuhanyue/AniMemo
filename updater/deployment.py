from __future__ import annotations

import hashlib
import gzip
import json
import os
import shutil
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from release.contract import API_REPOSITORY, WEB_REPOSITORY

from .commands import CommandRunner
from .errors import CommandFailed, StateError
from .state import _atomic_json


PRODUCTION_APP_ROOT = Path("/opt/1panel/docker/compose/anime-journal/app")
PRODUCTION_DATA_ROOT = Path("/data/anime-journal")
PRODUCTION_STATE_ROOT = Path("/var/lib/animemo-updater")
MAX_BACKUP_AGE = timedelta(hours=24)


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
    def __init__(self, paths: HostPaths, *, runner=None, stable_observations: int = 3, observation_seconds: int = 5):
        self.paths = paths
        self.runner = runner or CommandRunner()
        self.stable_observations = stable_observations
        self.observation_seconds = observation_seconds
        self.compose_file = self.paths.app_root / "deploy" / "docker-compose.yml"
        self.env_file = self.paths.app_root / ".env.production"
        self.runtime_env = self.paths.state_root / "runtime-images.env"

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
        self.runner.run(["/usr/bin/docker", "version", "--format", "{{.Server.Version}}"], timeout=15)
        self._compose(manifest, "config", "--quiet", timeout=30)
        services = set(self._compose(manifest, "ps", "--services", "--filter", "status=running", timeout=30).stdout.split())
        if not {"postgres", "redis", "api", "web"}.issubset(services):
            raise StateError("AniMemo Compose project is not fully running")

    def verify_recent_backup(self, *, now: datetime | None = None) -> None:
        metadata_files = sorted((self.paths.data_root / "backups").glob("*.json"), key=lambda item: item.stat().st_mtime, reverse=True)
        if not metadata_files:
            raise StateError("No verified AniMemo database backup metadata is available")
        payload = json.loads(metadata_files[0].read_text(encoding="utf-8"))
        backup = Path(payload["path"])
        if backup.parent.resolve() != (self.paths.data_root / "backups").resolve() or not backup.is_file():
            raise StateError("Latest backup metadata points outside the AniMemo backup root")
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
        )
        compressed_digest = hashlib.sha256(target.read_bytes()).hexdigest()
        metadata = {
            "path": str(target),
            "createdAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "operationId": operation_id,
            "compressedSha256": compressed_digest,
            **result,
        }
        _atomic_json(target.with_suffix(target.suffix + ".json"), metadata)
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
        self.paths.state_root.mkdir(parents=True, exist_ok=True)
        env = self.image_environment(manifest)
        self.runtime_env.write_text(
            f"ANIMEMO_API_IMAGE={env['ANIMEMO_API_IMAGE']}\nANIMEMO_WEB_IMAGE={env['ANIMEMO_WEB_IMAGE']}\n",
            encoding="utf-8",
            newline="\n",
        )
        os.chmod(self.runtime_env, 0o600)
        self._compose(manifest, "up", "-d", "--no-deps", "--force-recreate", "api", "web", timeout=600)

    def verify_health(self, manifest: dict[str, object]) -> None:
        for observation in range(self.stable_observations):
            for service in ["api", "web"]:
                container = self._compose(manifest, "ps", "-q", service, timeout=30).stdout.strip()
                if not container:
                    raise CommandFailed(f"{service} container is unavailable")
                result = self.runner.run(
                    [
                        "/usr/bin/docker", "inspect", "--format",
                        "{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}} {{.RestartCount}}",
                        container,
                    ],
                    timeout=30,
                ).stdout.split()
                if result != ["healthy", "0"]:
                    raise CommandFailed(f"{service} failed stable health observation")
            if observation + 1 < self.stable_observations:
                time.sleep(self.observation_seconds)
