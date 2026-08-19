from __future__ import annotations

import gzip
import hashlib
import http.client
import ipaddress
import json
import os
import re
import shutil
import stat
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

from durability.instance import (
    APP_ROOT,
    DATA_ROOT,
    MANAGED_CONFIG_PATH,
    UPDATER_RUNTIME_ROOT,
    UPDATER_STATE_ROOT,
    InstanceLocator,
    InstanceSnapshot,
)
from release.contract import (
    API_REPOSITORY,
    DEPLOYMENT_CONTRACT_PATHS,
    POSTGRES_REPOSITORY,
    REDIS_REPOSITORY,
    WEB_REPOSITORY,
)

from .commands import CommandRunner
from .errors import CommandFailed, StateError
from .oci import ImageAcquirer
from .state import _atomic_json, _atomic_text, _read_private_text

MAX_BACKUP_AGE = timedelta(hours=24)
MIN_AVAILABLE_MEMORY = 512 * 1024 * 1024
HEALTH_PATHS = ("/health/", "/", "/login", "/api/schema/", "/api/docs/")
PROCESS_ENV_ALLOWLIST = frozenset(
    {
        "DOCKER_CONFIG",
        "HOME",
        "LANG",
        "LC_ALL",
        "PATH",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "TMPDIR",
    }
)
HTTP_5XX_LOG = re.compile(r'"\s5\d\d(?:\s|$)')
CRITICAL_LOG = re.compile(
    r"(?im)(?:^|\b)(?:traceback \(most recent call last\)|critical|fatal|panic)(?:\b|:)"
)
MIGRATION_PLAN_LINE = re.compile(
    r"^\s*\[(?P<state>[ X])\]\s+(?P<name>[A-Za-z0-9_]+\.[^\s]+)\s*$"
)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class HostPaths:
    app_root: Path
    data_root: Path
    state_root: Path
    managed_config_path: Path
    managed_env_path: Path
    listen_host: str
    listen_port: int
    public_origin: str
    locator_digest: str | None

    @classmethod
    def production(cls, snapshot: InstanceSnapshot):
        if not isinstance(snapshot, InstanceSnapshot):
            raise TypeError(
                "Production Update Agent requires a canonical instance snapshot"
            )
        locator = snapshot.locator
        if (
            locator.app_root != APP_ROOT
            or locator.data_root != DATA_ROOT
            or locator.managed_config_path != MANAGED_CONFIG_PATH
        ):
            raise ValueError("Production Update Agent locator is not canonical")
        return cls(
            app_root=Path(str(APP_ROOT)),
            data_root=Path(str(DATA_ROOT)),
            state_root=Path(str(UPDATER_STATE_ROOT)),
            managed_config_path=Path(str(MANAGED_CONFIG_PATH)),
            managed_env_path=Path(str(UPDATER_RUNTIME_ROOT / "managed.env")),
            listen_host=locator.listen.host,
            listen_port=locator.listen.port,
            public_origin=locator.public_origin,
            locator_digest=snapshot.digest,
        )

    @classmethod
    def initial_adoption(cls, locator: InstanceLocator) -> HostPaths:
        if not isinstance(locator, InstanceLocator):
            raise TypeError("Initial adoption requires a canonical locator")
        if (
            locator.app_root != APP_ROOT
            or locator.data_root != DATA_ROOT
            or locator.managed_config_path != MANAGED_CONFIG_PATH
        ):
            raise ValueError("Initial adoption locator is not canonical")
        return cls(
            app_root=Path(str(APP_ROOT)),
            data_root=Path(str(DATA_ROOT)),
            state_root=Path(str(UPDATER_STATE_ROOT)),
            managed_config_path=Path(str(MANAGED_CONFIG_PATH)),
            managed_env_path=Path(str(UPDATER_RUNTIME_ROOT / "managed.env")),
            listen_host=locator.listen.host,
            listen_port=locator.listen.port,
            public_origin=locator.public_origin,
            locator_digest=None,
        )

    @classmethod
    def testing(cls, *, app: Path, data: Path, state: Path):
        resolved_roots = (app.resolve(), data.resolve(), state.resolve())
        for value in resolved_roots:
            if not value.is_absolute():
                raise ValueError("Test paths must be absolute")
        return cls(
            app_root=resolved_roots[0],
            data_root=resolved_roots[1],
            state_root=resolved_roots[2],
            managed_config_path=resolved_roots[1] / "config" / "animemo.json",
            managed_env_path=resolved_roots[2] / "managed.env",
            listen_host="127.0.0.1",
            listen_port=8088,
            public_origin="https://ci.example.test",
            locator_digest=None,
        )


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
        release_probe=None,
        managed_environment: Mapping[str, str] | None = None,
    ):
        self.paths = paths
        self.runner = runner or CommandRunner()
        self.stable_observations = stable_observations
        self.observation_seconds = observation_seconds
        self.memory_info_path = memory_info_path
        self.http_probe = http_probe or self._http_probe
        self.release_probe = release_probe or self._release_probe
        self.managed_environment = dict(managed_environment or {})
        if any(
            not isinstance(key, str) or not key or not isinstance(value, str)
            for key, value in self.managed_environment.items()
        ):
            raise ValueError("Managed child environment is invalid")
        self.compose_file = self.paths.app_root / "deploy" / "docker-compose.yml"
        self.updater_compose_file = Path(__file__).with_name(
            "docker-compose.runtime.yml"
        )
        self.runtime_env = self.paths.state_root / "runtime-images.env"

    def refresh_binding(
        self,
        paths: HostPaths,
        *,
        managed_environment: Mapping[str, str],
    ) -> None:
        """Refresh one lock-held canonical config/locator snapshot."""

        if not isinstance(paths, HostPaths) or any(
            not isinstance(key, str) or not key or not isinstance(value, str)
            for key, value in managed_environment.items()
        ):
            raise StateError("Canonical runtime binding is invalid")
        self.paths = paths
        self.managed_environment = dict(managed_environment)
        self.compose_file = self.paths.app_root / "deploy" / "docker-compose.yml"
        self.runtime_env = self.paths.state_root / "runtime-images.env"

    def _local_probe_host(self) -> str:
        address = ipaddress.ip_address(self.paths.listen_host)
        if address.is_unspecified:
            return "::1" if address.version == 6 else "127.0.0.1"
        return address.compressed

    def _http_probe(
        self, path: str, *, host: str, port: int, forwarded_proto: str
    ) -> int:
        connection = http.client.HTTPConnection(
            self._local_probe_host(), port, timeout=5
        )
        try:
            connection.request(
                "GET",
                path,
                headers={"Host": host, "X-Forwarded-Proto": forwarded_proto},
            )
            response = connection.getresponse()
            response.read()
            return response.status
        finally:
            connection.close()

    def _release_probe(
        self, *, host: str, port: int, forwarded_proto: str
    ) -> dict[str, object]:
        connection = http.client.HTTPConnection(
            self._local_probe_host(), port, timeout=5
        )
        try:
            connection.request(
                "GET",
                "/health/",
                headers={"Host": host, "X-Forwarded-Proto": forwarded_proto},
            )
            response = connection.getresponse()
            body = response.read()
            if response.status != 200:
                raise StateError(
                    f"AniMemo effective release identity is unavailable: HTTP {response.status}"
                )
            try:
                payload = json.loads(body)
            except json.JSONDecodeError as error:
                raise StateError(
                    "AniMemo effective release identity is invalid"
                ) from error
            if not isinstance(payload, dict):
                raise StateError("AniMemo effective release identity is invalid")
            return payload
        finally:
            connection.close()

    def _public_endpoint(self) -> tuple[str, int, str]:
        parsed = urlparse(self.paths.public_origin)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise StateError(
                "AniMemo Public Origin is unavailable for local health checks"
            )
        return parsed.hostname, self.paths.listen_port, parsed.scheme

    def _container_state(
        self, manifest: dict[str, object], service: str
    ) -> tuple[str, int]:
        container = self._container_id(manifest, service)
        result = self.runner.run(
            [
                "/usr/bin/docker",
                "inspect",
                "--format",
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
            raise StateError(
                f"AniMemo {service} container restart count is invalid"
            ) from error
        return result[0], restarts

    def _container_id(self, manifest: dict[str, object], service: str) -> str:
        container = self._compose(
            manifest, "ps", "-q", service, timeout=30
        ).stdout.strip()
        if not container:
            raise StateError(f"AniMemo {service} container is unavailable")
        return container

    def _inspect_container(self, container: str, template: str) -> str:
        return self.runner.run(
            ["/usr/bin/docker", "inspect", "--format", template, container],
            timeout=30,
        ).stdout.strip()

    def _verify_http_paths(self, paths: tuple[str, ...]) -> None:
        host, port, forwarded_proto = self._public_endpoint()
        for path in paths:
            try:
                status = self.http_probe(
                    path, host=host, port=port, forwarded_proto=forwarded_proto
                )
            except OSError as error:
                raise StateError(f"AniMemo HTTP contract failed for {path}") from error
            if status != 200:
                raise StateError(
                    f"AniMemo HTTP contract failed for {path}: HTTP {status}"
                )

    def _verify_recent_logs(self, manifest: dict[str, object], *, since: str) -> None:
        for service in ["api", "web"]:
            container = self._compose(
                manifest, "ps", "-q", service, timeout=30
            ).stdout.strip()
            if not container:
                raise StateError(f"AniMemo {service} container is unavailable")
            result = self.runner.run(
                ["/usr/bin/docker", "logs", "--since", since, container],
                timeout=30,
            )
            logs = f"{result.stdout}\n{result.stderr}"
            if HTTP_5XX_LOG.search(logs) or CRITICAL_LOG.search(logs):
                raise StateError(
                    f"AniMemo {service} logs failed the stable observation gate"
                )

    @staticmethod
    def image_environment(
        manifest: dict[str, object],
        *,
        live_contracts: dict[str, str] | None = None,
    ) -> dict[str, str]:
        release = manifest["release"]
        compatibility = manifest["compatibility"]
        contracts = (
            {
                "databaseContract": compatibility["database"]["contract"],
                "configurationContract": compatibility["configuration"]["contract"],
            }
            if live_contracts is None
            else live_contracts
        )
        if (
            not isinstance(contracts, dict)
            or set(contracts) != {"databaseContract", "configurationContract"}
            or not all(isinstance(value, str) and value for value in contracts.values())
        ):
            raise StateError("Live runtime contracts are invalid")
        return {
            "ANIMEMO_API_IMAGE": f"{API_REPOSITORY}@{manifest['images']['api']['digest']}",
            "ANIMEMO_WEB_IMAGE": f"{WEB_REPOSITORY}@{manifest['images']['web']['digest']}",
            "ANIMEMO_POSTGRES_IMAGE": (
                f"{POSTGRES_REPOSITORY}@{manifest['images']['postgres']['digest']}"
            ),
            "ANIMEMO_REDIS_IMAGE": (
                f"{REDIS_REPOSITORY}@{manifest['images']['redis']['digest']}"
            ),
            "ANIMEMO_RELEASE_VERSION": release["version"],
            "ANIMEMO_RELEASE_COMMIT": release["commit"],
            "ANIMEMO_RELEASE_CHANNEL": release["channel"],
            "ANIMEMO_DATABASE_CONTRACT": contracts["databaseContract"],
            "ANIMEMO_CONFIGURATION_CONTRACT": contracts["configurationContract"],
        }

    def _environment(
        self,
        manifest: dict[str, object],
        *,
        live_contracts: dict[str, str] | None = None,
    ) -> dict[str, str]:
        return {
            **{
                key: os.environ[key]
                for key in PROCESS_ENV_ALLOWLIST
                if key in os.environ
            },
            **self.managed_environment,
            **self.image_environment(manifest, live_contracts=live_contracts),
        }

    def _compose(
        self,
        manifest: dict[str, object],
        *args: str,
        timeout: int = 300,
        live_contracts: dict[str, str] | None = None,
    ):
        env_files = ["--env-file", str(self.paths.managed_env_path)]
        if self.runtime_env.exists() or self.runtime_env.is_symlink():
            _read_private_text(self.paths.state_root, self.runtime_env)
            env_files.extend(["--env-file", str(self.runtime_env)])
        return self.runner.run(
            [
                "/usr/bin/docker",
                "compose",
                "--project-name",
                "animemo",
                *env_files,
                "-f",
                str(self.compose_file),
                "-f",
                str(self.updater_compose_file),
                *args,
            ],
            cwd=self.paths.app_root,
            env=self._environment(manifest, live_contracts=live_contracts),
            timeout=timeout,
        )

    def verify_deployment_contract(self, manifest: dict[str, object]) -> None:
        declared = manifest.get("deployment", {}).get("files", [])
        if not isinstance(declared, list) or [
            item.get("path") for item in declared if isinstance(item, dict)
        ] != list(DEPLOYMENT_CONTRACT_PATHS):
            raise StateError("AniMemo deployment contract is incomplete or unordered")
        local_files = {
            "deploy/docker-compose.yml": self.compose_file,
            "updater/docker-compose.runtime.yml": self.updater_compose_file,
        }
        for item in declared:
            source = local_files[item["path"]]
            try:
                source_stat = source.lstat()
            except OSError as error:
                raise StateError(
                    f"AniMemo deployment contract file is unavailable: {item['path']}"
                ) from error
            if (
                source.is_symlink()
                or not stat.S_ISREG(source_stat.st_mode)
                or source_stat.st_nlink != 1
            ):
                raise StateError(
                    f"AniMemo deployment contract file must be a private regular file: {item['path']}"
                )
            actual = "sha256:" + _sha256_file(source)
            if actual != item.get("sha256"):
                raise StateError(
                    f"AniMemo deployment contract file differs from the Manifest: {item['path']}"
                )

    def preflight(self, manifest: dict[str, object]) -> None:
        for path in [
            self.compose_file,
            self.updater_compose_file,
            self.paths.managed_config_path,
            self.paths.managed_env_path,
        ]:
            if not path.is_file() or path.is_symlink():
                raise StateError(
                    f"Required fixed AniMemo file is unavailable: {path.name}"
                )
        self.verify_deployment_contract(manifest)
        usage = shutil.disk_usage(self.paths.data_root)
        if usage.free < 2 * 1024 * 1024 * 1024:
            raise StateError("AniMemo data root has less than 2 GiB free")
        try:
            memory_fields = {
                key: value
                for key, value in (
                    line.split(":", 1)
                    for line in self.memory_info_path.read_text(
                        encoding="utf-8"
                    ).splitlines()
                    if ":" in line
                )
            }
            available_kib = int(memory_fields["MemAvailable"].strip().split()[0])
        except (OSError, KeyError, ValueError, IndexError) as error:
            raise StateError(
                "AniMemo host available memory cannot be determined"
            ) from error
        if available_kib * 1024 < MIN_AVAILABLE_MEMORY:
            raise StateError("AniMemo host has less than 512 MiB available memory")
        self.runner.run(
            ["/usr/bin/docker", "version", "--format", "{{.Server.Version}}"],
            timeout=15,
        )
        self._compose(manifest, "config", "--quiet", timeout=30)
        services = set(
            self._compose(
                manifest, "ps", "--services", "--filter", "status=running", timeout=30
            ).stdout.split()
        )
        if not {"postgres", "redis", "api", "web"}.issubset(services):
            raise StateError("AniMemo Compose project is not fully running")
        for service in ["postgres", "redis", "api", "web"]:
            status, _ = self._container_state(manifest, service)
            if status != "healthy":
                raise StateError(f"AniMemo {service} container is not healthy")
        self._verify_http_paths(("/health/", "/"))
        if manifest["compatibility"]["database"]["migration"]["required"]:
            backup_root = self.paths.data_root / "backups"
            if (
                not backup_root.is_dir()
                or backup_root.is_symlink()
                or not os.access(backup_root, os.W_OK)
            ):
                raise StateError(
                    "AniMemo backup root is unavailable for a fresh migration backup"
                )
        else:
            self.verify_recent_backup()

    def verify_recent_backup(self, *, now: datetime | None = None) -> None:
        backup_root = self.paths.data_root / "backups"
        if not backup_root.is_dir() or backup_root.is_symlink():
            raise StateError("AniMemo backup root is unavailable")
        metadata_files = sorted(
            backup_root.glob("*.json"),
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        )
        if not metadata_files:
            raise StateError(
                "No verified AniMemo database backup metadata is available"
            )
        metadata = metadata_files[0]
        metadata_stat = metadata.lstat()
        if (
            metadata.is_symlink()
            or not stat.S_ISREG(metadata_stat.st_mode)
            or metadata_stat.st_nlink != 1
        ):
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
            raise StateError(
                "Latest backup metadata points outside the AniMemo backup root"
            )
        backup_stat = backup.lstat()
        if (
            backup.is_symlink()
            or not stat.S_ISREG(backup_stat.st_mode)
            or backup_stat.st_nlink != 1
        ):
            raise StateError("Latest AniMemo database backup must not be a link")
        digest = _sha256_file(backup)
        if digest != payload.get("compressedSha256"):
            raise StateError("Latest AniMemo database backup checksum is invalid")
        try:
            created_at = datetime.fromisoformat(
                str(payload["createdAt"]).replace("Z", "+00:00")
            )
        except (KeyError, TypeError, ValueError) as error:
            raise StateError(
                "Latest AniMemo database backup timestamp is invalid"
            ) from error
        if created_at.tzinfo is None:
            raise StateError("Latest AniMemo database backup timestamp is invalid")
        current_time = now or datetime.now(timezone.utc)
        age = current_time.astimezone(timezone.utc) - created_at.astimezone(
            timezone.utc
        )
        if age < timedelta(0) or age > MAX_BACKUP_AGE:
            raise StateError("Latest AniMemo database backup is older than 24 hours")
        try:
            with gzip.open(backup, "rb") as handle:
                while handle.read(1024 * 1024):
                    pass
        except (OSError, EOFError) as error:
            raise StateError(
                "Latest AniMemo database backup gzip stream is invalid"
            ) from error

    def backup_database(self, operation_id: str) -> str:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup_root = self.paths.data_root / "backups"
        target = backup_root / f"animemo-pre-{operation_id[:8]}-{stamp}.sql.gz"
        result = self.runner.write_gzip(
            [
                "/usr/bin/docker",
                "compose",
                "--project-name",
                "animemo",
                "--env-file",
                str(self.paths.managed_env_path),
                "-f",
                str(self.compose_file),
                "exec",
                "-T",
                "postgres",
                "sh",
                "-c",
                'exec pg_dump --format=plain --no-owner --no-privileges -U "$POSTGRES_USER" "$POSTGRES_DB"',
            ],
            target,
            cwd=self.paths.app_root,
            timeout=600,
            root=self.paths.data_root,
        )
        compressed_digest = _sha256_file(target)
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
        for image in [
            env["ANIMEMO_API_IMAGE"],
            env["ANIMEMO_WEB_IMAGE"],
            env["ANIMEMO_POSTGRES_IMAGE"],
            env["ANIMEMO_REDIS_IMAGE"],
        ]:
            self.runner.run(["/usr/bin/docker", "pull", image], env=env, timeout=600)

    def pull_verified(self, materials, policy) -> None:
        receipt = ImageAcquirer(
            runner=self.runner,
            environment=self._environment(materials.manifest),
        ).acquire(materials, policy)
        self._record_image_acquisition_receipt(receipt)

    def import_local_verified(self, source, materials, policy) -> None:
        acquire_images = getattr(source, "acquire_images", None)
        if not callable(acquire_images):
            raise StateError("Verified local OCI source is unavailable")
        receipt = acquire_images(
            materials,
            ImageAcquirer(
                runner=self.runner,
                environment=self._environment(materials.manifest),
            ),
        )
        if receipt.transport_policy_identity != policy.identity:
            raise StateError("Local OCI receipt transport binding is invalid")
        self._record_image_acquisition_receipt(receipt)

    def _record_image_acquisition_receipt(self, receipt) -> None:
        _atomic_json(
            self.paths.state_root / "distribution" / "image-acquisition-receipt.json",
            {
                "format": "animemo-image-acquisition-receipt",
                "version": 1,
                "identity": receipt.identity,
                "verifiedReleaseIdentity": receipt.verified_release_identity,
                "transportPolicyIdentity": receipt.transport_policy_identity,
                "images": [
                    {
                        "role": image.role,
                        "canonicalReference": image.canonical_reference,
                        "observedReference": image.observed_reference,
                    }
                    for image in receipt.images
                ],
            },
            root=self.paths.state_root,
        )

    def start_datastores(self, manifest: dict[str, object]) -> None:
        self._compose(
            manifest,
            "up",
            "-d",
            "--wait",
            "--wait-timeout",
            "120",
            "postgres",
            "redis",
            timeout=600,
        )

    def start_application(self, manifest: dict[str, object]) -> None:
        self._compose(
            manifest,
            "up",
            "-d",
            "--no-deps",
            "--wait",
            "--wait-timeout",
            "120",
            "api",
            "web",
            timeout=600,
        )

    def reconcile_application(self, manifest: dict[str, object]) -> None:
        self._compose(
            manifest,
            "up",
            "-d",
            "--no-deps",
            "--force-recreate",
            "--wait",
            "--wait-timeout",
            "120",
            "api",
            "web",
            timeout=600,
        )

    def validate_compose(self, manifest: dict[str, object]) -> None:
        self._compose(manifest, "config", "--quiet", timeout=60)

    def probe_postgres(self, manifest: dict[str, object]) -> None:
        self._compose(
            manifest,
            "exec",
            "-T",
            "postgres",
            "pg_isready",
            "-U",
            self.managed_environment["POSTGRES_USER"],
            "-d",
            self.managed_environment["POSTGRES_DB"],
            timeout=30,
        )

    def probe_redis(self, manifest: dict[str, object]) -> None:
        result = self._compose(
            manifest,
            "exec",
            "-T",
            "redis",
            "redis-cli",
            "ping",
            timeout=30,
        )
        if result.stdout.strip() != "PONG":
            raise StateError("AniMemo Redis connectivity probe failed")

    def probe_api(self, manifest: dict[str, object]) -> None:
        del manifest
        host, _port, forwarded_proto = self._public_endpoint()
        payload = self.release_probe(
            host=host,
            port=self.paths.listen_port,
            forwarded_proto=forwarded_proto,
        )
        if payload.get("status") != "ok":
            raise StateError("AniMemo API health probe failed")

    def probe_web(self, manifest: dict[str, object]) -> None:
        del manifest
        host, _port, forwarded_proto = self._public_endpoint()
        if (
            self.http_probe(
                "/",
                host=host,
                port=self.paths.listen_port,
                forwarded_proto=forwarded_proto,
            )
            != 200
        ):
            raise StateError("AniMemo Web health probe failed")

    def migrate(self, manifest: dict[str, object]) -> None:
        self._compose(manifest, "run", "--rm", "--no-deps", "migration", timeout=600)

    def bootstrap(self, manifest: dict[str, object]) -> None:
        self._compose(manifest, "run", "--rm", "--no-deps", "bootstrap", timeout=600)

    def rotate_restored_authentication_epoch(
        self, manifest: dict[str, object]
    ) -> None:
        self._compose(
            manifest,
            "exec",
            "-T",
            "api",
            "python",
            "manage.py",
            "rotate_authentication_epoch",
            "--confirm-restore",
            timeout=120,
        )

    def apply_restore_secret_disposition(
        self,
        manifest: dict[str, object],
        names: tuple[str, ...],
    ) -> None:
        allowed = {
            "BANGUMI_OAUTH_CLIENT_SECRET",
            "RESEND_API_KEY",
            "TURNSTILE_SECRET",
        }
        if tuple(sorted(set(names))) != names or any(
            name not in allowed for name in names
        ):
            raise StateError("Restore secret disposition is invalid")
        argv = [
            "run",
            "--rm",
            "--no-deps",
            "api",
            "python",
            "manage.py",
            "apply_restore_secret_disposition",
        ]
        for name in names:
            argv.extend(("--clear", name))
        self._compose(manifest, *argv, timeout=120)

    def inspect_restore_integrity(
        self, manifest: dict[str, object]
    ) -> dict[str, bool]:
        result = self._compose(
            manifest,
            "exec",
            "-T",
            "api",
            "python",
            "manage.py",
            "validate_restore_integrity",
            timeout=300,
        )
        try:
            payload = json.loads(result.stdout.strip().splitlines()[-1])
        except (json.JSONDecodeError, IndexError) as error:
            raise StateError(
                "Restore integrity inspection returned invalid data"
            ) from error
        checks = payload.get("checks") if isinstance(payload, dict) else None
        expected = {
            "instance.identity",
            "protection.decryptability",
            "authentication.epoch",
            "durable.write",
            "memory.mi1.external_metadata",
            "memory.mi2.provider_identity",
            "memory.mi3.merge_history",
            "memory.mi4.unsupported_payload",
            "memory.mi5.destructive_ambiguity",
        }
        if (
            not isinstance(checks, dict)
            or set(checks) != expected
            or any(value is not True for value in checks.values())
        ):
            raise StateError("Restore integrity inspection failed")
        return dict(checks)

    def inspect_enabled_plugin_apis(self, manifest: dict[str, object]) -> set[int]:
        result = self._compose(
            manifest,
            "run",
            "--rm",
            "--no-deps",
            "api",
            "python",
            "manage.py",
            "list_enabled_plugin_apis",
            timeout=120,
        )
        try:
            values = json.loads(result.stdout.strip().splitlines()[-1])
        except (json.JSONDecodeError, IndexError) as error:
            raise StateError(
                "Enabled Plugin SDK inspection returned invalid data"
            ) from error
        if not isinstance(values, list) or not all(
            isinstance(value, int) and value > 0 for value in values
        ):
            raise StateError("Enabled Plugin SDK inspection returned invalid data")
        return set(values)

    def inspect_runtime_contracts(self, manifest: dict[str, object]) -> dict[str, str]:
        self._compose(
            manifest,
            "exec",
            "-T",
            "api",
            "python",
            "manage.py",
            "migrate",
            "--check",
            "--noinput",
            timeout=120,
        )
        self._compose(
            manifest,
            "exec",
            "-T",
            "api",
            "python",
            "manage.py",
            "check",
            "--deploy",
            timeout=120,
        )
        host, port, forwarded_proto = self._public_endpoint()
        payload = self.release_probe(
            host=host,
            port=port,
            forwarded_proto=forwarded_proto,
        )
        contracts = payload.get("contracts") if isinstance(payload, dict) else None
        if not isinstance(contracts, dict) or set(contracts) != {
            "database",
            "configuration",
        }:
            raise StateError("Live runtime contract inspection returned invalid data")
        if not all(isinstance(value, str) and value for value in contracts.values()):
            raise StateError("Live runtime contract inspection returned invalid data")
        return {
            "databaseContract": contracts["database"],
            "configurationContract": contracts["configuration"],
        }

    def _migration_snapshot(self, manifest: dict[str, object]) -> dict[str, bool]:
        result = self._compose(
            manifest,
            "run",
            "--rm",
            "--no-deps",
            "api",
            "python",
            "manage.py",
            "showmigrations",
            "--plan",
            timeout=120,
        )
        snapshot: dict[str, bool] = {}
        for line in result.stdout.splitlines():
            match = MIGRATION_PLAN_LINE.fullmatch(line)
            if match is None:
                continue
            name = match.group("name")
            applied = match.group("state") == "X"
            if name in snapshot and snapshot[name] != applied:
                raise StateError(
                    "Database migration inspection returned conflicting state"
                )
            snapshot[name] = applied
        if not snapshot:
            raise StateError("Database migration inspection returned no migrations")
        return snapshot

    def inspect_database_transition(
        self,
        current: dict[str, object],
        target: dict[str, object],
    ) -> str:
        current_snapshot = self._migration_snapshot(current)
        target_snapshot = self._migration_snapshot(target)
        current_names = set(current_snapshot)
        target_names = set(target_snapshot)
        if not current_names.issubset(target_names):
            return "indeterminate"
        if all(target_snapshot.values()):
            return "target"
        target_only = target_names - current_names
        if (
            all(current_snapshot.values())
            and target_only
            and all(not target_snapshot[name] for name in target_only)
        ):
            return "current"
        return "indeterminate"

    def switch(
        self,
        manifest: dict[str, object],
        *,
        live_contracts: dict[str, str] | None = None,
    ) -> None:
        env = self.image_environment(manifest, live_contracts=live_contracts)
        _atomic_text(
            self.runtime_env,
            "".join(f"{key}={value}\n" for key, value in env.items()),
            root=self.paths.state_root,
        )
        self._compose(
            manifest,
            "up",
            "-d",
            "--no-deps",
            "--force-recreate",
            "--wait",
            "--wait-timeout",
            "120",
            "api",
            "web",
            live_contracts=live_contracts,
            timeout=600,
        )

    def verify_health(
        self,
        manifest: dict[str, object],
        *,
        live_contracts: dict[str, str] | None = None,
    ) -> None:
        self.verify_deployment_contract(manifest)
        observation_started = (
            datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        )
        for observation in range(self.stable_observations):
            for service in ["api", "web"]:
                status, restarts = self._container_state(manifest, service)
                if status != "healthy" or restarts != 0:
                    raise CommandFailed(f"{service} failed stable health observation")
            self._verify_http_paths(HEALTH_PATHS)
            self._verify_recent_logs(manifest, since=observation_started)
            self._verify_release_identity(manifest, live_contracts=live_contracts)
            if observation + 1 < self.stable_observations:
                time.sleep(self.observation_seconds)

    def _verify_release_identity(
        self,
        manifest: dict[str, object],
        *,
        live_contracts: dict[str, str] | None = None,
    ) -> None:
        environment = self.image_environment(manifest, live_contracts=live_contracts)
        release = manifest["release"]
        artifact_version = release["promotedFrom"] or release["version"]
        artifact_channel = "rc" if release["promotedFrom"] else release["channel"]
        artifact = {
            "org.opencontainers.image.version": artifact_version,
            "org.opencontainers.image.revision": release["commit"],
            "cc.animemo.release.channel": artifact_channel,
        }
        for service, image_key in (
            ("api", "ANIMEMO_API_IMAGE"),
            ("web", "ANIMEMO_WEB_IMAGE"),
        ):
            container = self._container_id(manifest, service)
            actual_image = self._inspect_container(container, "{{.Config.Image}}")
            if actual_image != environment[image_key]:
                raise StateError(
                    f"AniMemo {service} image identity differs from the Manifest"
                )
            for label, expected in artifact.items():
                actual = self._inspect_container(
                    container,
                    '{{index .Config.Labels "' + label + '"}}',
                )
                if actual != expected:
                    raise StateError(
                        f"AniMemo {service} artifact identity differs from the Manifest"
                    )

        web_container = self._container_id(manifest, "web")
        web_identity = self.runner.run(
            [
                "/usr/bin/docker",
                "exec",
                web_container,
                "/bin/sh",
                "-c",
                (
                    'printf "%s\\n%s\\n%s\\n" "$ANIMEMO_RELEASE_VERSION" '
                    '"$ANIMEMO_RELEASE_COMMIT" "$ANIMEMO_RELEASE_CHANNEL"'
                ),
            ],
            timeout=30,
        ).stdout.splitlines()
        expected_release = {
            "version": release["version"],
            "commit": release["commit"],
            "channel": release["channel"],
        }
        if web_identity != [
            expected_release["version"],
            expected_release["commit"],
            expected_release["channel"],
        ]:
            raise StateError(
                "AniMemo web effective release identity differs from the Manifest"
            )

        host, port, forwarded_proto = self._public_endpoint()
        payload = self.release_probe(
            host=host, port=port, forwarded_proto=forwarded_proto
        )
        expected_contracts = {
            "database": environment["ANIMEMO_DATABASE_CONTRACT"],
            "configuration": environment["ANIMEMO_CONFIGURATION_CONTRACT"],
        }
        if (
            not isinstance(payload, dict)
            or payload.get("status") != "ok"
            or payload.get("release") != expected_release
            or payload.get("contracts") != expected_contracts
        ):
            raise StateError(
                "AniMemo API effective release identity differs from the Manifest"
            )
