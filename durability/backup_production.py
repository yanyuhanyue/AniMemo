"""Production composition for the canonical AniMemo Backup Format v1 domain.

This module owns host authority, operation locking, Compose quiescence, exact
PostgreSQL-container execution, protected input handling, and redacted operator
receipts.  Artifact semantics remain exclusively in :mod:`durability.backup`.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Protocol

from release.contract import (
    POSTGRES_REPOSITORY,
    PRODUCTION_BACKUP_CONTRACT,
    validate_manifest,
)
from updater.deployment import HostPaths, ImmutableComposeDeployment
from updater.errors import (
    OperationInProgress,
    RecoveryRequired,
    StateError,
    UpdaterError,
)
from updater.runtime import CanonicalInstanceRegistry
from updater.runtime_state import RuntimeState
from updater.slots import ReleaseSlots
from updater.state import TERMINAL_STATES, OperationStore, UpdateLock

from . import backup
from .canonical import canonical_json_bytes, sha256_identity
from .instance import (
    DEFAULT_INSTANCE_NAME,
    InstanceName,
    InstanceSnapshot,
    instance_namespace,
    release_identity_from_manifest,
)
from .managed_config import (
    LocalManagedConfigStore,
    ManagedConfig,
    derive_runtime_environment,
)
from .secret_envelope import (
    MAX_PASSPHRASE_BYTES,
    OneTimeKey,
    Passphrase,
    SecretEntry,
    SecretEnvelopeError,
    create_secret_envelope,
    open_secret_envelope,
)

PRIVATE_REGISTRY_NAME = "backup-members.json"
PRIVATE_REGISTRY_SCHEMA = 1
MAX_PROTECTED_INPUT_BYTES = MAX_PASSPHRASE_BYTES + 2
MIN_DESTINATION_HEADROOM = 64 * 1024 * 1024
_POSTGRES_MAJOR = re.compile(r"\(PostgreSQL\)\s+(?P<major>[0-9]+)\.")
_SAFE_HOST_ENV = frozenset(
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
_SECRET_COVERAGE = (
    "BANGUMI_OAUTH_CLIENT_SECRET",
    "CREDENTIAL_ENCRYPTION_KEY",
    "DJANGO_SECRET_KEY",
    "POSTGRES_PASSWORD",
    "REDIS_URL",
    "RESEND_API_KEY",
)


class ProductionBackupError(RuntimeError):
    """Stable redacted production failure with a CLI exit category."""

    def __init__(self, code: str, category: str = "ENVIRONMENT") -> None:
        self.code = code
        self.category = category
        super().__init__(code)


@dataclass(frozen=True)
class ProtectionRequest:
    kind: str | None
    path: Path | None = None
    fd: int | None = None

    def validate(self, *, creating: bool) -> tuple[object, ...]:
        allowed = {
            "one-time-key",
            "passphrase-file",
            "passphrase-fd",
            "secret-reference",
        }
        if self.kind is None:
            if creating or self.path is not None or self.fd is not None:
                raise ProductionBackupError("BACKUP_PROTECTION_REQUIRED", "VALIDATION")
            return (None,)
        if self.kind not in allowed:
            raise ProductionBackupError("BACKUP_PROTECTION_MODE_INVALID", "VALIDATION")
        if self.kind == "passphrase-fd":
            if self.path is not None or not isinstance(self.fd, int) or self.fd < 0:
                raise ProductionBackupError(
                    "BACKUP_PROTECTION_INPUT_INVALID", "VALIDATION"
                )
            try:
                metadata = os.fstat(self.fd)
            except OSError:
                raise ProductionBackupError(
                    "BACKUP_PROTECTION_UNAVAILABLE", "ENVIRONMENT"
                ) from None
            if stat.S_ISREG(metadata.st_mode) and (
                metadata.st_nlink != 1
                or (os.name == "posix" and stat.S_IMODE(metadata.st_mode) != 0o600)
            ):
                raise ProductionBackupError(
                    "BACKUP_PROTECTION_FILE_UNSAFE", "VALIDATION"
                )
            return (
                self.kind,
                metadata.st_dev,
                metadata.st_ino,
                metadata.st_size,
                metadata.st_mtime_ns,
            )
        if self.fd is not None or self.path is None or not self.path.is_absolute():
            raise ProductionBackupError("BACKUP_PROTECTION_PATH_INVALID", "VALIDATION")
        selected = Path(self.path)
        if self.kind == "one-time-key" and creating:
            _validate_new_private_output(selected)
            parent = selected.parent.stat()
            return (self.kind, os.fspath(selected), parent.st_dev, parent.st_ino)
        metadata = _protected_file_metadata(selected)
        if self.kind == "one-time-key" and metadata.st_size != 32:
            raise ProductionBackupError("BACKUP_PROTECTION_INPUT_INVALID", "VALIDATION")
        if self.kind == "secret-reference":
            encoded = _read_protected_file(selected, limit=1024 * 1024)
            _validate_reference_coverage(encoded)
            content_identity = hashlib.sha256(encoded).hexdigest()
        else:
            content_identity = None
        return (
            self.kind,
            os.fspath(selected),
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_size,
            metadata.st_mtime_ns,
            content_identity,
        )


@dataclass(frozen=True)
class MediaInventory:
    local_references: Mapping[str, str] = field(default_factory=dict)
    r2_references: tuple[backup.R2Reference, ...] = ()
    observed_at: str = "1970-01-01T00:00:00Z"

    @property
    def digest(self) -> str:
        return sha256_identity(
            canonical_json_bytes(
                {
                    "local": dict(sorted(self.local_references.items())),
                    "r2": [
                        {
                            "backendType": item.backend_type,
                            "endpointIdentity": item.endpoint_identity,
                            "bucket": item.bucket,
                            "objectKeys": list(item.object_keys),
                            "coverageClassification": item.coverage_classification,
                        }
                        for item in self.r2_references
                    ],
                }
            )
        )


class BackupHostPort(Protocol):
    def validate(self) -> None: ...

    def media_inventory(self) -> MediaInventory: ...

    def database_size_bytes(self) -> int: ...

    def writer_states(self, writers: tuple[str, ...]) -> Mapping[str, str]: ...

    def stop_writers(self, states: Mapping[str, str]) -> None: ...

    def verify_write_barrier(self, writers: tuple[str, ...]) -> None: ...

    def restore_writers(self, states: Mapping[str, str]) -> Mapping[str, str]: ...

    def verify_health(self, states: Mapping[str, str]) -> None: ...

    def pg_dump_runner(self) -> backup.PgDumpRunner: ...


@dataclass(frozen=True)
class ProductionBinding:
    instance_name: InstanceName
    snapshot: InstanceSnapshot
    config: ManagedConfig
    manifest: Mapping[str, Any]
    paths: HostPaths
    host: BackupHostPort = field(compare=False, repr=False)
    backup_contract: Mapping[str, Any]


class BackupAuthorityPort(Protocol):
    def bind(self, instance_name: InstanceName | str) -> ProductionBinding: ...


@dataclass(frozen=True)
class BackupPlan:
    instance_name: str
    instance_id: str
    source_release: Mapping[str, Any]
    source_locator_digest: str
    destination: str
    member_classes: tuple[str, ...]
    secret_mode: str
    quiescence_method: str
    writer_services: tuple[str, ...]
    estimated_bytes: int
    database_profile: Mapping[str, Any]
    plan_digest: str
    _protection_fingerprint: tuple[object, ...] = field(repr=False, compare=False)
    _media_digest: str = field(repr=False, compare=False)

    def _body(self) -> dict[str, Any]:
        return {
            "instanceName": self.instance_name,
            "instanceId": self.instance_id,
            "sourceRelease": dict(self.source_release),
            "sourceLocatorDigest": self.source_locator_digest,
            "destination": self.destination,
            "memberClasses": list(self.member_classes),
            "secretMode": self.secret_mode,
            "quiescenceMethod": self.quiescence_method,
            "writerServices": list(self.writer_services),
            "estimatedBytes": self.estimated_bytes,
            "databaseProfile": dict(self.database_profile),
        }

    def as_dict(self) -> dict[str, Any]:
        return {**self._body(), "planDigest": self.plan_digest}


@dataclass(frozen=True)
class BackupReceipt:
    backup_id: str
    path: str
    instance_name: str
    instance_id: str
    manifest_digest: str
    checksum_set_digest: str
    plan_digest: str
    outcome: str
    verification_completed_before_resume: bool
    writer_state_restored: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "backupId": self.backup_id,
            "path": self.path,
            "instanceName": self.instance_name,
            "instanceId": self.instance_id,
            "manifestDigest": self.manifest_digest,
            "checksumSetDigest": self.checksum_set_digest,
            "planDigest": self.plan_digest,
            "outcome": self.outcome,
            "verificationCompletedBeforeResume": self.verification_completed_before_resume,
            "writerStateRestored": self.writer_state_restored,
        }


class LocalBackupAuthority:
    """Bind exactly one installed instance without discovery fallbacks."""

    def bind(self, instance_name: InstanceName | str) -> ProductionBinding:
        name = (
            instance_name
            if isinstance(instance_name, InstanceName)
            else InstanceName(instance_name)
        )
        registry = CanonicalInstanceRegistry.production(name)
        snapshot = registry.snapshot()
        namespace = instance_namespace(name)
        config = LocalManagedConfigStore(instance_name=name).read()
        if (
            config.instance_id != snapshot.locator.instance_id
            or config.config_revision != snapshot.locator.config_revision
            or config.listen.host != snapshot.locator.listen.host
            or config.listen.port != snapshot.locator.listen.port
            or config.public_origin != snapshot.locator.public_origin
        ):
            raise ProductionBackupError("BACKUP_CONFIG_LOCATOR_MISMATCH", "VALIDATION")
        slots = ReleaseSlots(Path(str(namespace.release_slots_root))).read()
        manifest = slots.get("current")
        if not isinstance(manifest, dict):
            raise ProductionBackupError(
                "BACKUP_CURRENT_RELEASE_UNAVAILABLE", "ENVIRONMENT"
            )
        try:
            validate_manifest(manifest)
            if dict(release_identity_from_manifest(manifest)) != dict(
                snapshot.locator.release_identity
            ):
                raise ValueError
        except (TypeError, ValueError):
            raise ProductionBackupError(
                "BACKUP_RELEASE_LOCATOR_MISMATCH", "VALIDATION"
            ) from None
        runtime_contracts = RuntimeState(Path(str(namespace.updater_state_root))).read()
        expected_runtime = {
            "databaseContract": manifest["compatibility"]["database"]["contract"],
            "configurationContract": manifest["compatibility"]["configuration"][
                "contract"
            ],
            "enabledPluginApis": runtime_contracts["enabledPluginApis"],
        }
        supported_plugin_apis = set(
            manifest["compatibility"]["pluginSdk"]["supportedApis"]
        )
        if runtime_contracts != expected_runtime or not set(
            runtime_contracts["enabledPluginApis"]
        ).issubset(supported_plugin_apis):
            raise ProductionBackupError("BACKUP_RUNTIME_CONTRACT_INVALID", "VALIDATION")
        raw_contract = manifest.get("deployment", {}).get("backup")
        if raw_contract != PRODUCTION_BACKUP_CONTRACT:
            raise ProductionBackupError(
                "BACKUP_DEPLOYMENT_CONTRACT_MISSING", "COMPATIBILITY"
            )
        paths = HostPaths.production(snapshot)
        environment = dict(
            derive_runtime_environment(
                config,
                namespace=namespace,
                locator_digest=snapshot.digest,
            )
        )
        deployment = ImmutableComposeDeployment(paths, managed_environment=environment)
        host = DockerComposeBackupHost(
            deployment=deployment,
            manifest=manifest,
            config=config,
            contract=raw_contract,
        )
        return ProductionBinding(
            instance_name=name,
            snapshot=snapshot,
            config=config,
            manifest=manifest,
            paths=paths,
            host=host,
            backup_contract=raw_contract,
        )


class DockerComposeBackupHost:
    """Closed instance-scoped Compose and PostgreSQL production adapter."""

    _MEDIA_SQL = (
        "SELECT b.backend_type,COALESCE(b.local_root,''),"
        "COALESCE(b.endpoint_url,''),COALESCE(a.account_id,''),"
        "COALESCE(b.bucket_name,''),m.object_key,COALESCE(m.sha256,'') "
        "FROM site_mediaobject m "
        "JOIN site_mediastoragebackend b ON b.id=m.storage_backend_id "
        "LEFT JOIN site_cloudflarer2account a "
        "ON a.id=b.cloudflare_account_ref_id "
        "ORDER BY b.id,m.object_key"
    )
    _PENDING_SQL = (
        "SELECT count(*) FROM site_mediawritereservation WHERE status='pending'"
    )

    def __init__(
        self,
        *,
        deployment: ImmutableComposeDeployment,
        manifest: Mapping[str, Any],
        config: ManagedConfig,
        contract: Mapping[str, Any],
    ) -> None:
        self.deployment = deployment
        self.manifest = dict(manifest)
        self.config = config
        self.contract = dict(contract)
        self._postgres = ContainerPgDumpRunner(deployment, self.manifest, config)

    def validate(self) -> None:
        try:
            self.deployment.verify_deployment_contract(self.manifest)
            self.deployment.validate_compose(self.manifest)
            live = self.deployment.inspect_runtime_contracts(self.manifest)
            expected = {
                "databaseContract": self.manifest["compatibility"]["database"][
                    "contract"
                ],
                "configurationContract": self.manifest["compatibility"][
                    "configuration"
                ]["contract"],
            }
            if live != expected:
                raise StateError("live runtime contract differs")
            self._postgres.verify_identity()
        except ProductionBackupError:
            raise
        except (KeyError, OSError, TypeError, UpdaterError, ValueError):
            raise ProductionBackupError(
                "BACKUP_LIVE_AUTHORITY_INVALID", "ENVIRONMENT"
            ) from None

    def media_inventory(self) -> MediaInventory:
        observed_at = _utc_now()
        try:
            if self._postgres.psql_scalar(self._PENDING_SQL) != "0":
                raise ProductionBackupError("BACKUP_MEDIA_WRITE_PENDING", "VALIDATION")
            output = self._postgres.psql(self._MEDIA_SQL)
        except ProductionBackupError:
            raise
        except (KeyError, OSError, TypeError, UpdaterError, ValueError):
            raise ProductionBackupError(
                "BACKUP_EXTERNAL_MEDIA_COVERAGE_UNPROVEN", "VALIDATION"
            ) from None
        local: dict[str, str] = {}
        r2: dict[tuple[str, str], list[str]] = {}
        media_root = self.deployment.paths.data_root / "media"
        for line in output.splitlines():
            if not line:
                continue
            fields = line.split("\t")
            if len(fields) != 7 or any("\x00" in value for value in fields):
                raise ProductionBackupError(
                    "BACKUP_EXTERNAL_MEDIA_COVERAGE_UNPROVEN", "VALIDATION"
                )
            backend_type, local_root, endpoint, account, bucket, object_key, digest = (
                fields
            )
            key = _canonical_media_path(object_key)
            if backend_type == "local":
                relative = _canonical_media_path(
                    "/".join(value for value in (local_root, key) if value)
                )
                source = media_root / PurePosixPath(relative)
                if not source.is_file() or source.is_symlink():
                    raise ProductionBackupError(
                        "BACKUP_MEDIA_REFERENCE_MISSING", "VALIDATION"
                    )
                actual = _sha256_regular_file(source)
                if digest and digest != actual:
                    raise ProductionBackupError(
                        "BACKUP_MEDIA_REFERENCE_MISMATCH", "VALIDATION"
                    )
                local[relative] = f"sha256:{actual}"
            elif backend_type == "cloudflare_r2":
                if not endpoint or not account or not bucket:
                    raise ProductionBackupError(
                        "BACKUP_EXTERNAL_MEDIA_COVERAGE_UNPROVEN", "VALIDATION"
                    )
                endpoint_identity = sha256_identity(
                    canonical_json_bytes(
                        {
                            "endpoint": endpoint.rstrip("/").casefold(),
                            "account": account.casefold(),
                        }
                    )
                )
                r2.setdefault((endpoint_identity, bucket.casefold()), []).append(key)
            else:
                raise ProductionBackupError(
                    "BACKUP_EXTERNAL_MEDIA_COVERAGE_UNPROVEN", "VALIDATION"
                )
        references = tuple(
            backup.R2Reference(
                backend_type="r2",
                endpoint_identity=identity,
                bucket=bucket,
                object_keys=tuple(
                    sorted(set(keys), key=lambda value: value.encode("utf-8"))
                ),
                inventory_timestamp=observed_at,
                coverage_classification="AUTHORITATIVE_DATABASE_COMPLETE",
            )
            for (identity, bucket), keys in sorted(r2.items())
        )
        return MediaInventory(local, references, observed_at)

    def database_size_bytes(self) -> int:
        value = self._postgres.psql_scalar(
            "SELECT pg_database_size(current_database())"
        )
        if not value.isdigit() or int(value) <= 0:
            raise ProductionBackupError(
                "BACKUP_DATABASE_SIZE_UNPROVEN", "VALIDATION"
            )
        return int(value)

    def _service_container(self, service: str) -> str | None:
        result = self.deployment._compose(
            self.manifest, "ps", "-a", "-q", service, timeout=30
        ).stdout.strip()
        values = result.splitlines()
        if len(values) > 1:
            raise ProductionBackupError(
                "BACKUP_CONTAINER_IDENTITY_INVALID", "VALIDATION"
            )
        if not values:
            return None
        container = values[0]
        expected = {
            "com.docker.compose.project": self.deployment.paths.compose_project,
            "com.docker.compose.service": service,
            "io.animemo.instance-name": str(self.deployment.paths.instance_name),
            "io.animemo.instance-id": self.deployment.paths.instance_id,
            "io.animemo.compose-project": self.deployment.paths.compose_project,
        }
        for label, value in expected.items():
            actual = self.deployment._inspect_container(
                container, f'{{{{ index .Config.Labels "{label}" }}}}'
            )
            if actual != value:
                raise ProductionBackupError(
                    "BACKUP_CONTAINER_IDENTITY_INVALID", "VALIDATION"
                )
        return container

    def writer_states(self, writers: tuple[str, ...]) -> Mapping[str, str]:
        result: dict[str, str] = {}
        for service in writers:
            container = self._service_container(service)
            if container is None:
                result[service] = "absent"
                continue
            state = self.deployment._inspect_container(container, "{{.State.Status}}")
            if state not in {"running", "exited", "created"}:
                raise ProductionBackupError("BACKUP_WRITER_STATE_INVALID", "VALIDATION")
            result[service] = "running" if state == "running" else "stopped"
        return result

    def stop_writers(self, states: Mapping[str, str]) -> None:
        running = [name for name, state in states.items() if state == "running"]
        if running:
            self.deployment._compose(
                self.manifest,
                "stop",
                "--timeout",
                "30",
                *running,
                timeout=120,
            )

    def verify_write_barrier(self, writers: tuple[str, ...]) -> None:
        running = set(
            self.deployment._compose(
                self.manifest,
                "ps",
                "--status",
                "running",
                "--services",
                timeout=30,
            ).stdout.split()
        )
        if running.intersection(writers):
            raise ProductionBackupError("BACKUP_WRITE_BARRIER_FAILED", "RECOVERY")
        allowed = set(self.contract["allowedRunningServices"])
        if not running.issubset(allowed):
            raise ProductionBackupError("BACKUP_UNDECLARED_WRITER_RUNNING", "RECOVERY")
        if "postgres" not in running:
            raise ProductionBackupError("BACKUP_POSTGRES_NOT_READABLE", "RECOVERY")
        self._postgres.verify_identity()

    def restore_writers(self, states: Mapping[str, str]) -> Mapping[str, str]:
        running = [name for name, state in states.items() if state == "running"]
        if running:
            self.deployment._compose(
                self.manifest,
                "up",
                "-d",
                "--no-deps",
                "--wait",
                "--wait-timeout",
                "120",
                *running,
                timeout=300,
            )
        restored = self.writer_states(tuple(states))
        if dict(restored) != dict(states):
            raise ProductionBackupError("BACKUP_WRITER_RESTORE_FAILED", "RECOVERY")
        return restored

    def verify_health(self, states: Mapping[str, str]) -> None:
        if states.get("api") == "running":
            try:
                self.deployment.verify_health(self.manifest)
            except (OSError, UpdaterError, ValueError):
                raise ProductionBackupError(
                    "BACKUP_POST_RESUME_HEALTH_FAILED", "RECOVERY"
                ) from None

    def pg_dump_runner(self) -> backup.PgDumpRunner:
        return self._postgres


class ContainerPgDumpRunner:
    """Run the fixed pg_dump profile in the exact verified PostgreSQL 16 container."""

    def __init__(
        self,
        deployment: ImmutableComposeDeployment,
        manifest: Mapping[str, Any],
        config: ManagedConfig,
    ) -> None:
        self.deployment = deployment
        self.manifest = dict(manifest)
        self.config = config
        self._container: str | None = None
        self._tool_version: str | None = None

    def _environment(self) -> dict[str, str]:
        environment = {
            key: value
            for key, value in os.environ.items()
            if key.upper() in _SAFE_HOST_ENV
        }
        environment["PGPASSWORD"] = self.config.database.password
        return environment

    def _docker_text(self, argv: Sequence[str], *, timeout: int = 30) -> str:
        try:
            completed = subprocess.run(
                list(argv),
                check=False,
                capture_output=True,
                env=self._environment(),
                timeout=timeout,
                shell=False,
            )
        except subprocess.TimeoutExpired:
            raise ProductionBackupError("PG_DUMP_TIMEOUT", "ENVIRONMENT") from None
        except OSError:
            raise ProductionBackupError(
                "BACKUP_DOCKER_UNAVAILABLE", "ENVIRONMENT"
            ) from None
        if completed.returncode != 0:
            raise ProductionBackupError("BACKUP_POSTGRES_PROBE_FAILED", "ENVIRONMENT")
        try:
            return completed.stdout.decode("utf-8").strip()
        except UnicodeError:
            raise ProductionBackupError(
                "BACKUP_POSTGRES_PROBE_FAILED", "ENVIRONMENT"
            ) from None

    def verify_identity(self) -> tuple[str, str]:
        container = self.deployment._compose(
            self.manifest, "ps", "-q", "postgres", timeout=30
        ).stdout.strip()
        if not container or len(container.splitlines()) != 1:
            raise ProductionBackupError(
                "BACKUP_POSTGRES_CONTAINER_INVALID", "VALIDATION"
            )
        expected_labels = {
            "com.docker.compose.project": self.deployment.paths.compose_project,
            "com.docker.compose.service": "postgres",
            "io.animemo.instance-name": str(self.deployment.paths.instance_name),
            "io.animemo.instance-id": self.deployment.paths.instance_id,
            "io.animemo.compose-project": self.deployment.paths.compose_project,
        }
        for label, expected in expected_labels.items():
            actual = self.deployment._inspect_container(
                container, f'{{{{ index .Config.Labels "{label}" }}}}'
            )
            if actual != expected:
                raise ProductionBackupError(
                    "BACKUP_POSTGRES_CONTAINER_INVALID", "VALIDATION"
                )
        state = self.deployment._inspect_container(
            container,
            "{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{else}}missing{{end}}",
        ).split()
        if state != ["running", "healthy"]:
            raise ProductionBackupError("BACKUP_POSTGRES_NOT_HEALTHY", "ENVIRONMENT")
        expected_image = (
            f"{POSTGRES_REPOSITORY}@{self.manifest['images']['postgres']['digest']}"
        )
        if (
            self.deployment._inspect_container(container, "{{.Config.Image}}")
            != expected_image
        ):
            raise ProductionBackupError("BACKUP_POSTGRES_DIGEST_MISMATCH", "VALIDATION")
        tool = self._docker_text(
            ["/usr/bin/docker", "exec", container, "pg_dump", "--version"]
        )
        match = _POSTGRES_MAJOR.search(tool)
        if match is None or int(match.group("major")) != 16:
            raise ProductionBackupError("PG_DUMP_MAJOR_UNSUPPORTED", "COMPATIBILITY")
        row = self.psql(
            "SELECT current_database(),current_user,current_setting('server_version_num')",
            container=container,
        ).split("\t")
        if (
            len(row) != 3
            or row[0] != self.config.database.name
            or row[1] != self.config.database.user
            or not row[2].isdigit()
            or int(row[2]) // 10000 != 16
        ):
            raise ProductionBackupError(
                "BACKUP_POSTGRES_IDENTITY_MISMATCH", "VALIDATION"
            )
        self._container = container
        self._tool_version = tool
        return container, tool

    def psql(self, sql: str, *, container: str | None = None) -> str:
        if not isinstance(sql, str) or not sql or "\x00" in sql:
            raise ProductionBackupError("BACKUP_DATABASE_QUERY_INVALID", "VALIDATION")
        selected = container or self.verify_identity()[0]
        return self._docker_text(
            [
                "/usr/bin/docker",
                "exec",
                "--env",
                "PGPASSWORD",
                selected,
                "psql",
                "--no-psqlrc",
                "--tuples-only",
                "--no-align",
                "--field-separator=\t",
                "--username",
                self.config.database.user,
                "--dbname",
                self.config.database.name,
                "--command",
                sql,
            ],
            timeout=120,
        )

    def psql_scalar(self, sql: str) -> str:
        value = self.psql(sql)
        if "\n" in value or "\t" in value:
            raise ProductionBackupError("BACKUP_DATABASE_QUERY_INVALID", "VALIDATION")
        return value

    def run(
        self,
        database_url: str,
        raw_output: Path,
        *,
        executable: str,
        timeout: int,
    ) -> str:
        if Path(executable).name.casefold() not in {"pg_dump", "pg_dump.exe"}:
            raise backup.BackupError(
                "PG_DUMP_EXECUTABLE_INVALID", "pg_dump executable is invalid"
            )
        parsed = backup.postgres_connection_environment(database_url)
        expected = {
            "PGDATABASE": self.config.database.name,
            "PGHOST": "postgres",
            "PGPORT": "5432",
            "PGUSER": self.config.database.user,
            "PGPASSWORD": self.config.database.password,
        }
        if any(parsed.get(key) != value for key, value in expected.items()):
            raise backup.BackupError(
                "DATABASE_URL_INVALID", "database authority differs"
            )
        try:
            container, tool = self.verify_identity()
        except ProductionBackupError as error:
            raise backup.BackupError(
                error.code, "PostgreSQL container identity failed"
            ) from None
        descriptor = -1
        process = None
        try:
            descriptor = os.open(
                raw_output,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            with (
                tempfile.TemporaryFile() as stderr_file,
                os.fdopen(descriptor, "wb") as output,
            ):
                descriptor = -1
                process = subprocess.Popen(
                    [
                        "/usr/bin/docker",
                        "exec",
                        "--env",
                        "PGPASSWORD",
                        container,
                        "pg_dump",
                        *backup.PG_DUMP_ARGUMENTS,
                        "--username",
                        self.config.database.user,
                        "--dbname",
                        self.config.database.name,
                    ],
                    stdout=output,
                    stderr=stderr_file,
                    env=self._environment(),
                    shell=False,
                )
                try:
                    return_code = process.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                    raise backup.BackupError(
                        "PG_DUMP_TIMEOUT", "logical database dump timed out"
                    ) from None
                output.flush()
                os.fsync(output.fileno())
            if return_code != 0:
                raise backup.BackupError(
                    "PG_DUMP_FAILED", "logical database dump failed"
                )
            os.chmod(raw_output, 0o600)
            return tool
        except backup.BackupError:
            raise
        except OSError:
            raise backup.BackupError(
                "PG_DUMP_FAILED", "logical database dump failed"
            ) from None
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            if process is not None and process.poll() is None:
                process.kill()
                process.wait()


class ProductionBackupRuntime:
    def __init__(self, authority: BackupAuthorityPort | None = None) -> None:
        self.authority = authority or LocalBackupAuthority()

    def plan(
        self,
        *,
        instance_name: InstanceName | str = DEFAULT_INSTANCE_NAME,
        destination: Path | None,
        protection: ProtectionRequest,
    ) -> BackupPlan:
        binding = self.authority.bind(instance_name)
        return self._plan_for(binding, destination=destination, protection=protection)

    def _plan_for(
        self,
        binding: ProductionBinding,
        *,
        destination: Path | None,
        protection: ProtectionRequest,
    ) -> BackupPlan:
        fingerprint = protection.validate(creating=True)
        binding.host.validate()
        selected = (
            Path(destination)
            if destination is not None
            else binding.paths.data_root / "backups"
        )
        private_members = _registered_private_members(
            binding.paths.data_root / "private"
        )
        estimated = _estimate_sources(binding, private_members)
        selected = _validate_destination(
            binding,
            selected,
            estimated_bytes=estimated,
            explicit=destination is not None,
        )
        inventory = binding.host.media_inventory()
        writers = tuple(binding.backup_contract["writerServices"])
        body = {
            "instanceName": str(binding.instance_name),
            "instanceId": binding.snapshot.locator.instance_id,
            "sourceRelease": dict(binding.manifest["release"]),
            "sourceLocatorDigest": binding.snapshot.digest,
            "destination": os.fspath(selected),
            "memberClasses": sorted(backup.CANONICAL_FILESYSTEM_ROOTS),
            "secretMode": (
                "reference" if protection.kind == "secret-reference" else "envelope"
            ),
            "quiescenceMethod": binding.backup_contract["quiescenceMethod"],
            "writerServices": list(writers),
            "estimatedBytes": estimated,
            "databaseProfile": {
                "adapter": "exact-compose-postgres-container",
                "serverMajor": 16,
                "toolMajor": 16,
                "argv": list(backup.PG_DUMP_ARGUMENTS),
            },
        }
        digest = sha256_identity(canonical_json_bytes(body))
        return BackupPlan(
            instance_name=body["instanceName"],
            instance_id=body["instanceId"],
            source_release=body["sourceRelease"],
            source_locator_digest=body["sourceLocatorDigest"],
            destination=body["destination"],
            member_classes=tuple(body["memberClasses"]),
            secret_mode=body["secretMode"],
            quiescence_method=body["quiescenceMethod"],
            writer_services=tuple(body["writerServices"]),
            estimated_bytes=body["estimatedBytes"],
            database_profile=body["databaseProfile"],
            plan_digest=digest,
            _protection_fingerprint=fingerprint,
            _media_digest=inventory.digest,
        )

    def execute(
        self,
        plan: BackupPlan,
        *,
        protection: ProtectionRequest,
        accepted_plan_digest: str,
    ) -> BackupReceipt:
        if not isinstance(plan, BackupPlan) or accepted_plan_digest != plan.plan_digest:
            raise ProductionBackupError("BACKUP_PLAN_NOT_ACCEPTED", "VALIDATION")
        name = InstanceName(plan.instance_name)
        binding = self.authority.bind(name)
        rebound = self._plan_for(
            binding,
            destination=Path(plan.destination),
            protection=protection,
        )
        _require_same_plan(plan, rebound)
        lock = UpdateLock(binding.paths.state_root / "update.lock")
        try:
            lock.__enter__()
        except OperationInProgress:
            raise ProductionBackupError(
                "BACKUP_OPERATION_ACTIVE", "VALIDATION"
            ) from None
        key_path: Path | None = None
        key_identity: tuple[int, int] | None = None
        key_bound = False
        original_error: Exception | None = None
        states: Mapping[str, str] | None = None
        result: backup.BackupResult | None = None
        verification: backup.BackupVerification | None = None
        try:
            binding = self.authority.bind(name)
            rebound = self._plan_for(
                binding,
                destination=Path(plan.destination),
                protection=protection,
            )
            _require_same_plan(plan, rebound)
            _require_operations_clear(binding.paths.state_root)
            states = binding.host.writer_states(plan.writer_services)
            prestate_digest = _writer_state_digest(states)
            barrier_started = _utc_now()
            try:
                binding.host.stop_writers(states)
                binding.host.verify_write_barrier(plan.writer_services)
                barrier_inventory = binding.host.media_inventory()
                if barrier_inventory.digest != plan._media_digest:
                    raise ProductionBackupError("BACKUP_PLAN_STALE", "VALIDATION")
                private_members = _registered_private_members(
                    binding.paths.data_root / "private"
                )
                with _source_projection(binding, private_members) as sources:
                    (
                        external_secret,
                        secret_source,
                        key_path,
                        key_identity,
                    ) = _prepare_secret_source(binding, protection)
                    quiescence = {
                        "method": plan.quiescence_method,
                        "writerServices": list(plan.writer_services),
                        "startedAt": barrier_started,
                        "completedAt": _utc_now(),
                        "prestateDigest": prestate_digest,
                        "poststateDigest": prestate_digest,
                        "verificationCompletedBeforeResume": True,
                    }
                    request = _backup_request(
                        binding,
                        destination=Path(plan.destination),
                        sources=sources,
                        secret=secret_source,
                        private_members=private_members,
                        inventory=barrier_inventory,
                        quiescence=quiescence,
                    )
                    result = backup.create_backup(
                        request,
                        pg_dump_runner=binding.host.pg_dump_runner(),
                    )
                    key_bound = True
                    verification = backup.verify_backup(result.path)
                    _authenticate_backup_protection(
                        result.path,
                        verification,
                        protection=protection,
                        external_secret=external_secret,
                    )
            except Exception as error:  # noqa: BLE001 - always restore stopped writers
                original_error = error
            try:
                assert states is not None
                restored = binding.host.restore_writers(states)
                binding.host.verify_health(states)
                if _writer_state_digest(restored) != prestate_digest:
                    raise ProductionBackupError(
                        "BACKUP_WRITER_RESTORE_FAILED", "RECOVERY"
                    )
            except Exception:  # noqa: BLE001 - collapse any unsafe recovery outcome
                if key_path is not None and not key_bound:
                    _secure_remove(key_path, expected_identity=key_identity)
                raise ProductionBackupError("RECOVERY_REQUIRED", "RECOVERY") from None
            if original_error is not None:
                if key_path is not None and not key_bound:
                    _secure_remove(key_path, expected_identity=key_identity)
                raise ProductionBackupError(
                    "BACKUP_FAILED_NO_RUNTIME_DAMAGE", "ENVIRONMENT"
                ) from original_error
            assert result is not None and verification is not None
            return BackupReceipt(
                backup_id=result.backup_id,
                path=os.fspath(result.path),
                instance_name=plan.instance_name,
                instance_id=plan.instance_id,
                manifest_digest=verification.manifest_digest,
                checksum_set_digest=verification.checksum_set_digest,
                plan_digest=plan.plan_digest,
                outcome="BACKUP_SUCCEEDED",
                verification_completed_before_resume=True,
                writer_state_restored=True,
            )
        finally:
            lock.__exit__(None, None, None)


def production_backup_runtime() -> ProductionBackupRuntime:
    return ProductionBackupRuntime()


def verify_protected_backup(
    path: Path,
    *,
    protection: ProtectionRequest,
) -> dict[str, Any]:
    verification = backup.verify_backup(path)
    inspected = backup.inspect_backup(path)
    _authenticate_backup_protection(
        Path(path),
        verification,
        protection=protection,
    )
    return {
        **inspected,
        "manifestDigest": verification.manifest_digest,
        "checksumSetDigest": verification.checksum_set_digest,
        "databaseUncompressedBytes": verification.database_uncompressed_bytes,
        "verified": True,
    }


def _authenticate_backup_protection(
    path: Path,
    verification: backup.BackupVerification,
    *,
    protection: ProtectionRequest,
    external_secret: Passphrase | OneTimeKey | None = None,
) -> None:
    manifest = _strict_json_file(
        Path(path) / backup.MANIFEST_NAME, limit=8 * 1024 * 1024
    )
    secrets = manifest.get("secrets")
    if not isinstance(secrets, Mapping) or not isinstance(secrets.get("mode"), str):
        raise ProductionBackupError("BACKUP_MANIFEST_INVALID", "VALIDATION")
    mode = secrets["mode"]
    if mode == "none":
        protection.validate(creating=False)
        if protection.kind is not None:
            raise ProductionBackupError("BACKUP_PROTECTION_MODE_MISMATCH", "VALIDATION")
    elif mode == "envelope":
        if protection.kind not in {"one-time-key", "passphrase-file", "passphrase-fd"}:
            raise ProductionBackupError("BACKUP_PROTECTION_REQUIRED", "VALIDATION")
        external = external_secret or _external_secret(protection)
        source = manifest.get("source")
        binding = manifest.get("artifactBindingRecord")
        if not isinstance(source, Mapping) or not isinstance(binding, Mapping):
            raise ProductionBackupError("BACKUP_MANIFEST_INVALID", "VALIDATION")
        try:
            open_secret_envelope(
                _read_protected_file(
                    Path(path) / "secrets" / "secret-envelope.json",
                    limit=8 * 1024 * 1024,
                ),
                external_secret=external,
                expected_artifact_type="backup",
                expected_artifact_id=verification.backup_id,
                expected_artifact_binding_record=binding,
                expected_source_instance_id=str(source.get("instanceId", "")),
            )
        except (OSError, SecretEnvelopeError) as error:
            code = getattr(error, "code", "BACKUP_PROTECTION_UNAVAILABLE")
            raise ProductionBackupError(code, "VALIDATION") from None
    elif mode == "reference":
        if protection.kind != "secret-reference" or protection.path is None:
            raise ProductionBackupError("BACKUP_PROTECTION_REQUIRED", "VALIDATION")
        supplied = _read_protected_file(protection.path, limit=1024 * 1024)
        _validate_reference_coverage(supplied)
        try:
            embedded = _read_protected_file(
                Path(path) / "secrets" / "secret-reference.json",
                limit=1024 * 1024,
            )
        except (OSError, ProductionBackupError):
            raise ProductionBackupError(
                "BACKUP_PROTECTION_UNAVAILABLE", "ENVIRONMENT"
            ) from None
        if not hmac.compare_digest(supplied, embedded):
            raise ProductionBackupError(
                "BACKUP_PROTECTION_AUTHENTICATION_FAILED", "VALIDATION"
            )
    else:
        raise ProductionBackupError("BACKUP_PROTECTION_MODE_INVALID", "VALIDATION")


def _backup_request(
    binding: ProductionBinding,
    *,
    destination: Path,
    sources: Mapping[str, Path],
    secret: backup.SecretSource,
    private_members: tuple[str, ...],
    inventory: MediaInventory,
    quiescence: Mapping[str, Any],
) -> backup.BackupRequest:
    compatibility = binding.manifest["compatibility"]
    return backup.BackupRequest(
        destination_root=destination,
        database_url=derive_runtime_environment(
            binding.config,
            namespace=instance_namespace(binding.instance_name),
            locator_digest=binding.snapshot.digest,
        )["DATABASE_URL"],
        source=backup.BackupSourceIdentity(
            instance_id=binding.snapshot.locator.instance_id,
            source_locator_digest=binding.snapshot.digest,
            release={
                **dict(binding.manifest["release"]),
                "manifestDigest": binding.snapshot.locator.release_identity[
                    "manifestDigest"
                ],
            },
            deployment_contract={
                "schemaVersion": 2,
                "profile": binding.manifest["deployment"]["profile"],
                "digest": binding.manifest["deployment"]["contractSha256"],
                "backup": dict(binding.backup_contract),
            },
            database_contract={
                "id": compatibility["database"]["contract"],
                "serverMajor": 16,
            },
            configuration_contract={"id": compatibility["configuration"]["contract"]},
            plugin_sdk_apis=tuple(
                f"animemo.plugin/v{value}"
                for value in compatibility["pluginSdk"]["supportedApis"]
            ),
            instance_name=str(binding.instance_name),
        ),
        filesystem_sources=tuple(
            backup.FilesystemSource(logical_root=name, source=source)
            for name, source in sorted(sources.items())
        ),
        secret=secret,
        local_media_references=inventory.local_references,
        r2_references=inventory.r2_references,
        registered_private_members=private_members,
        producer={"name": "animemo-production-backup", "version": "1.1.0"},
        platform={"os": "linux", "architecture": "amd64"},
        quiescence=quiescence,
    )


def _prepare_secret_source(
    binding: ProductionBinding,
    protection: ProtectionRequest,
) -> tuple[
    Passphrase | OneTimeKey | None,
    backup.SecretSource,
    Path | None,
    tuple[int, int] | None,
]:
    if protection.kind == "secret-reference":
        assert protection.path is not None
        encoded = _read_protected_file(protection.path, limit=1024 * 1024)
        _validate_reference_coverage(encoded)
        return (
            None,
            backup.SecretSource(
                mode="reference",
                source=protection.path,
                metadata={"coverage": "REGISTERED_PRODUCTION_SECRETS"},
            ),
            None,
            None,
        )
    key_path = None
    key_identity = None
    if protection.kind == "one-time-key":
        assert protection.path is not None
        external = OneTimeKey.generate()
        key_identity = _write_one_time_key(protection.path, external.export())
        key_path = protection.path
    else:
        external = _external_secret(protection)
    entries = _managed_secret_entries(binding.config)

    def envelope_factory(binding_record: backup.SecretEnvelopeBinding) -> bytes:
        envelope = create_secret_envelope(
            external_secret=external,
            artifact_type="backup",
            artifact_id=binding_record.artifact_id,
            artifact_binding_record=binding_record.artifact_binding_record,
            source_instance_id=binding_record.source_instance_id,
            secret_entries=entries,
        ).to_bytes()
        try:
            open_secret_envelope(
                envelope,
                external_secret=external,
                expected_artifact_type="backup",
                expected_artifact_id=binding_record.artifact_id,
                expected_artifact_binding_record=binding_record.artifact_binding_record,
                expected_source_instance_id=binding_record.source_instance_id,
            )
        except SecretEnvelopeError:
            raise backup.BackupError(
                "BACKUP_SECRET_ENVELOPE_INVALID",
                "secret envelope self-authentication failed",
            ) from None
        return envelope

    return (
        external,
        backup.SecretSource(
            mode="envelope",
            metadata={"suiteId": "argon2id-m65536-t3-p4-aes-256-gcm-v1"},
            envelope_factory=envelope_factory,
        ),
        key_path,
        key_identity,
    )


def _managed_secret_entries(config: ManagedConfig) -> tuple[SecretEntry, ...]:
    values = {
        "CREDENTIAL_ENCRYPTION_KEY": config.application.credential_encryption_key,
        "POSTGRES_PASSWORD": config.database.password,
        "DJANGO_SECRET_KEY": config.application.django_secret_key,
        "REDIS_URL": config.redis.url,
        "BANGUMI_OAUTH_CLIENT_SECRET": config.integrations.bangumi_oauth_client_secret,
        "RESEND_API_KEY": config.integrations.resend_api_key,
    }
    return tuple(
        SecretEntry.preserve(name, value.encode("utf-8"))
        for name, value in values.items()
        if value
    )


def _external_secret(protection: ProtectionRequest) -> Passphrase | OneTimeKey:
    protection.validate(creating=False)
    if protection.kind == "one-time-key":
        assert protection.path is not None
        return OneTimeKey.from_bytes(_read_protected_file(protection.path, limit=32))
    if protection.kind == "passphrase-file":
        assert protection.path is not None
        raw = _read_protected_file(protection.path, limit=MAX_PROTECTED_INPUT_BYTES)
    elif protection.kind == "passphrase-fd":
        assert protection.fd is not None
        raw = _read_protected_fd(protection.fd, limit=MAX_PROTECTED_INPUT_BYTES)
    else:
        raise ProductionBackupError("BACKUP_PROTECTION_MODE_INVALID", "VALIDATION")
    if raw.endswith(b"\r\n"):
        raw = raw[:-2]
    elif raw.endswith(b"\n"):
        raw = raw[:-1]
    try:
        return Passphrase.from_bytes(raw)
    except SecretEnvelopeError as error:
        raise ProductionBackupError(error.code, "VALIDATION") from None


@contextmanager
def _source_projection(
    binding: ProductionBinding,
    private_members: tuple[str, ...],
):
    runtime_root = binding.paths.runtime_root
    if not runtime_root.is_dir() or runtime_root.is_symlink():
        raise ProductionBackupError("BACKUP_RUNTIME_ROOT_UNSAFE", "ENVIRONMENT")
    _reject_linklike_ancestors(runtime_root, "BACKUP_RUNTIME_ROOT_UNSAFE")
    temporary = Path(tempfile.mkdtemp(prefix=".backup-source-", dir=runtime_root))
    os.chmod(temporary, 0o700)
    try:
        config_root = temporary / "config"
        updater_root = temporary / "updater-state"
        config_root.mkdir(mode=0o700)
        updater_root.mkdir(mode=0o700)
        public_config = {
            "schema": binding.config.schema,
            "instanceId": binding.config.instance_id,
            "configRevision": binding.config.config_revision,
            "deploymentProfile": binding.config.deployment_profile,
            "listen": {
                "host": binding.config.listen.host,
                "port": binding.config.listen.port,
            },
            "publicOrigin": binding.config.public_origin,
            "directAccess": {
                "allowNonLoopback": binding.config.direct_access.allow_non_loopback,
                "allowHttp": binding.config.direct_access.allow_http,
                "warningAcknowledged": binding.config.direct_access.warning_acknowledged,
            },
            "trustedOrigins": {
                "allowedHosts": list(binding.config.trusted_origins.allowed_hosts),
                "cors": list(binding.config.trusted_origins.cors),
                "csrf": list(binding.config.trusted_origins.csrf),
            },
            "mediaPublicOrigin": binding.config.application.media_public_origin,
            "bangumiOAuthClientId": binding.config.integrations.bangumi_oauth_client_id,
        }
        _write_private_bytes(
            config_root / "managed-config.public.json",
            canonical_json_bytes(public_config) + b"\n",
        )
        for relative in binding.backup_contract["updaterStateMembers"]:
            source = binding.paths.state_root / PurePosixPath(relative)
            parsed = _strict_json_file(source, limit=8 * 1024 * 1024)
            target = updater_root / PurePosixPath(relative)
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            _write_private_bytes(target, canonical_json_bytes(parsed) + b"\n")
        yield {
            "filesystem/config": config_root,
            "filesystem/plugins/cas": binding.paths.data_root / "plugins" / "cas",
            "filesystem/plugins/durable": binding.paths.data_root
            / "plugins"
            / "durable",
            "filesystem/media": binding.paths.data_root / "media",
            "filesystem/private": binding.paths.data_root / "private",
            "updater-state": updater_root,
        }
    finally:
        shutil.rmtree(temporary, ignore_errors=True)


def _require_operations_clear(state_root: Path) -> None:
    try:
        operations = OperationStore(state_root)
        operations.require_recovery_clear()
        for payload in operations.list():
            status = payload.get("status")
            if payload.get("kind") == "fresh_install":
                if status == "running":
                    raise ProductionBackupError("BACKUP_INSTALLER_ACTIVE", "VALIDATION")
            elif status not in TERMINAL_STATES:
                raise ProductionBackupError("BACKUP_OPERATION_PENDING", "VALIDATION")
        restore_root = state_root / "restore-operations"
        if restore_root.exists():
            if restore_root.is_symlink() or not restore_root.is_dir():
                raise ProductionBackupError(
                    "BACKUP_OPERATION_EVIDENCE_INVALID", "VALIDATION"
                )
            for path in sorted(restore_root.glob("*.json")):
                payload = _strict_json_file(path, limit=1024 * 1024)
                if payload.get("status") in {"running", "manual_recovery_required"}:
                    raise ProductionBackupError("BACKUP_RESTORE_ACTIVE", "VALIDATION")
    except ProductionBackupError:
        raise
    except (RecoveryRequired, StateError, OSError, ValueError):
        raise ProductionBackupError("BACKUP_RECOVERY_BARRIER", "RECOVERY") from None


def _registered_private_members(root: Path) -> tuple[str, ...]:
    if not root.is_dir() or root.is_symlink():
        raise ProductionBackupError("BACKUP_PRIVATE_ROOT_UNSAFE", "VALIDATION")
    _reject_linklike_ancestors(root, "BACKUP_PRIVATE_ROOT_UNSAFE")
    actual = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() or path.is_symlink()
    }
    if not actual:
        return ()
    registry_path = root / PRIVATE_REGISTRY_NAME
    if PRIVATE_REGISTRY_NAME not in actual:
        raise ProductionBackupError("BACKUP_PRIVATE_REGISTRY_MISSING", "VALIDATION")
    payload = _strict_json_file(registry_path, limit=1024 * 1024)
    if set(payload) != {"schemaVersion", "members"} or payload["schemaVersion"] != 1:
        raise ProductionBackupError("BACKUP_PRIVATE_REGISTRY_INVALID", "VALIDATION")
    members = payload.get("members")
    if not isinstance(members, list) or not all(
        isinstance(value, str) and value for value in members
    ):
        raise ProductionBackupError("BACKUP_PRIVATE_REGISTRY_INVALID", "VALIDATION")
    if (
        members != sorted(set(members), key=lambda value: value.encode("utf-8"))
        or PRIVATE_REGISTRY_NAME in members
    ):
        raise ProductionBackupError("BACKUP_PRIVATE_REGISTRY_INVALID", "VALIDATION")
    for relative in members:
        try:
            canonical = PurePosixPath(relative)
            if (
                canonical.is_absolute()
                or canonical.as_posix() != relative
                or any(part in {"", ".", ".."} for part in canonical.parts)
            ):
                raise ValueError
        except (TypeError, ValueError):
            raise ProductionBackupError(
                "BACKUP_PRIVATE_REGISTRY_INVALID", "VALIDATION"
            ) from None
    expected = {PRIVATE_REGISTRY_NAME, *members}
    if actual != expected or any(path.is_symlink() for path in root.rglob("*")):
        raise ProductionBackupError("BACKUP_PRIVATE_MEMBER_UNKNOWN", "VALIDATION")
    return tuple(sorted(expected, key=lambda value: value.encode("utf-8")))


def _estimate_sources(
    binding: ProductionBinding,
    private_members: tuple[str, ...],
) -> int:
    roots = (
        binding.paths.data_root / "plugins" / "cas",
        binding.paths.data_root / "plugins" / "durable",
        binding.paths.data_root / "media",
        binding.paths.data_root / "private",
    )
    total = binding.host.database_size_bytes()
    identities: set[tuple[int, int]] = set()
    for root in roots:
        if not root.is_dir() or root.is_symlink():
            raise ProductionBackupError("BACKUP_SOURCE_UNSAFE", "VALIDATION")
        _reject_linklike_ancestors(root, "BACKUP_SOURCE_UNSAFE")
        for path in root.rglob("*"):
            if path.is_symlink():
                raise ProductionBackupError("BACKUP_SOURCE_UNSAFE", "VALIDATION")
            if path.is_dir():
                continue
            metadata = path.lstat()
            identity = (metadata.st_dev, metadata.st_ino)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != 1
                or identity in identities
            ):
                raise ProductionBackupError("BACKUP_SOURCE_UNSAFE", "VALIDATION")
            identities.add(identity)
            total += metadata.st_size
    if private_members != _registered_private_members(
        binding.paths.data_root / "private"
    ):
        raise ProductionBackupError("BACKUP_PRIVATE_REGISTRY_CHANGED", "VALIDATION")
    for relative in binding.backup_contract["updaterStateMembers"]:
        source = binding.paths.state_root / PurePosixPath(relative)
        metadata = _protected_file_metadata(source)
        total += metadata.st_size
    total += len(canonical_json_bytes(binding.config.secret_safe_dict()))
    return total


def _validate_destination(
    binding: ProductionBinding,
    destination: Path,
    *,
    estimated_bytes: int,
    explicit: bool,
) -> Path:
    if explicit and not destination.is_absolute():
        raise ProductionBackupError("BACKUP_DESTINATION_NOT_ABSOLUTE", "VALIDATION")
    selected = destination.absolute()
    if not selected.parent.is_dir() or _linklike(selected.parent):
        raise ProductionBackupError("BACKUP_DESTINATION_PARENT_UNSAFE", "VALIDATION")
    _reject_linklike_ancestors(
        selected.parent, "BACKUP_DESTINATION_PARENT_UNSAFE"
    )
    if selected.exists() and (_linklike(selected) or not selected.is_dir()):
        raise ProductionBackupError("BACKUP_DESTINATION_INVALID", "VALIDATION")
    canonical_default = binding.paths.data_root / "backups"
    if explicit and selected != canonical_default and os.name == "posix":
        parent_mode = stat.S_IMODE(selected.parent.stat().st_mode)
        if parent_mode & 0o077:
            raise ProductionBackupError(
                "BACKUP_DESTINATION_PARENT_NOT_PRIVATE", "VALIDATION"
            )
    canonical_default = canonical_default.absolute()
    if selected != canonical_default and _paths_overlap(
        selected, binding.paths.data_root
    ):
        raise ProductionBackupError("BACKUP_DESTINATION_OVERLAP", "VALIDATION")
    roots = [
        binding.paths.app_root,
        binding.paths.state_root,
        binding.paths.runtime_root,
    ]
    for root in roots:
        if _paths_overlap(selected, root):
            raise ProductionBackupError("BACKUP_DESTINATION_OVERLAP", "VALIDATION")
    usage = shutil.disk_usage(selected if selected.exists() else selected.parent)
    if usage.free < estimated_bytes * 2 + MIN_DESTINATION_HEADROOM:
        raise ProductionBackupError(
            "BACKUP_DESTINATION_CAPACITY_INSUFFICIENT", "ENVIRONMENT"
        )
    return selected


def _require_same_plan(expected: BackupPlan, actual: BackupPlan) -> None:
    if (
        expected.as_dict() != actual.as_dict()
        or expected.plan_digest
        != sha256_identity(canonical_json_bytes(expected._body()))
        or expected._protection_fingerprint != actual._protection_fingerprint
        or expected._media_digest != actual._media_digest
    ):
        raise ProductionBackupError("BACKUP_PLAN_STALE", "VALIDATION")


def _validate_new_private_output(path: Path) -> None:
    if path.exists() or path.is_symlink():
        raise ProductionBackupError("BACKUP_KEY_OUTPUT_EXISTS", "VALIDATION")
    parent = path.parent
    if not parent.is_dir() or _linklike(parent):
        raise ProductionBackupError("BACKUP_KEY_PARENT_UNSAFE", "VALIDATION")
    _reject_linklike_ancestors(parent, "BACKUP_KEY_PARENT_UNSAFE")
    if os.name == "posix" and stat.S_IMODE(parent.stat().st_mode) & 0o077:
        raise ProductionBackupError("BACKUP_KEY_PARENT_NOT_PRIVATE", "VALIDATION")


def _write_one_time_key(path: Path, value: bytes) -> tuple[int, int]:
    _validate_new_private_output(path)
    descriptor = -1
    created_identity: tuple[int, int] | None = None
    try:
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        opened = os.fstat(descriptor)
        created_identity = (opened.st_dev, opened.st_ino)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(path, 0o600)
        published = path.lstat()
        if (published.st_dev, published.st_ino) != created_identity:
            raise OSError("one-time key path changed")
        _fsync_directory(path.parent)
        return created_identity
    except OSError:
        _secure_remove(path, expected_identity=created_identity)
        raise ProductionBackupError("BACKUP_KEY_OUTPUT_FAILED", "ENVIRONMENT") from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _secure_remove(
    path: Path,
    *,
    expected_identity: tuple[int, int] | None,
) -> None:
    if expected_identity is None:
        return
    descriptor = -1
    try:
        descriptor = os.open(
            path,
            os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
        )
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or (metadata.st_dev, metadata.st_ino) != expected_identity
        ):
            return
        remaining = metadata.st_size
        while remaining:
            written = os.write(descriptor, b"\x00" * min(remaining, 4096))
            if written <= 0:
                raise OSError("one-time key overwrite failed")
            remaining -= written
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        current = path.lstat()
        if (current.st_dev, current.st_ino) != expected_identity:
            return
        path.unlink()
        _fsync_directory(path.parent)
    except OSError:
        return
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _protected_file_metadata(path: Path) -> os.stat_result:
    _reject_linklike_ancestors(path.parent, "BACKUP_PROTECTION_FILE_UNSAFE")
    try:
        metadata = path.lstat()
    except OSError:
        raise ProductionBackupError(
            "BACKUP_PROTECTION_UNAVAILABLE", "ENVIRONMENT"
        ) from None
    if (
        path.is_symlink()
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or (os.name == "posix" and stat.S_IMODE(metadata.st_mode) != 0o600)
        or (os.name == "posix" and metadata.st_uid not in {0, os.geteuid()})
    ):
        raise ProductionBackupError("BACKUP_PROTECTION_FILE_UNSAFE", "VALIDATION")
    return metadata


def _read_protected_file(path: Path, *, limit: int) -> bytes:
    before = _protected_file_metadata(path)
    descriptor = -1
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        opened = os.fstat(descriptor)
        if (
            (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
            or opened.st_nlink != 1
            or not stat.S_ISREG(opened.st_mode)
        ):
            raise ProductionBackupError("BACKUP_PROTECTION_FILE_UNSAFE", "VALIDATION")
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            payload = handle.read(limit + 1)
        after = path.lstat()
        if (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) != (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        ):
            raise ProductionBackupError("BACKUP_PROTECTION_CHANGED", "VALIDATION")
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if len(payload) > limit:
        raise ProductionBackupError("BACKUP_PROTECTION_INPUT_INVALID", "VALIDATION")
    return payload


def _read_protected_fd(fd: int, *, limit: int) -> bytes:
    try:
        duplicate = os.dup(fd)
    except OSError:
        raise ProductionBackupError(
            "BACKUP_PROTECTION_UNAVAILABLE", "ENVIRONMENT"
        ) from None
    try:
        metadata = os.fstat(duplicate)
        if stat.S_ISREG(metadata.st_mode) and (
            metadata.st_nlink != 1
            or (os.name == "posix" and stat.S_IMODE(metadata.st_mode) != 0o600)
        ):
            raise ProductionBackupError("BACKUP_PROTECTION_FILE_UNSAFE", "VALIDATION")
        with os.fdopen(duplicate, "rb") as handle:
            duplicate = -1
            payload = handle.read(limit + 1)
    finally:
        if duplicate >= 0:
            os.close(duplicate)
    if len(payload) > limit:
        raise ProductionBackupError("BACKUP_PROTECTION_INPUT_INVALID", "VALIDATION")
    return payload


def _validate_reference_coverage(encoded: bytes) -> Mapping[str, Any]:
    try:
        payload = json.loads(encoded)
    except (UnicodeError, ValueError, json.JSONDecodeError):
        raise ProductionBackupError(
            "BACKUP_SECRET_REFERENCE_INVALID", "VALIDATION"
        ) from None
    if (
        not isinstance(payload, dict)
        or canonical_json_bytes(payload) + b"\n" != encoded
        or not isinstance(payload.get("provider"), str)
        or not payload["provider"]
        or not isinstance(payload.get("version"), str)
        or not payload["version"]
        or payload.get("coverage") != list(_SECRET_COVERAGE)
    ):
        raise ProductionBackupError("BACKUP_SECRET_REFERENCE_INVALID", "VALIDATION")
    return payload


def _strict_json_file(path: Path, *, limit: int) -> dict[str, Any]:
    raw = _read_protected_file(path, limit=limit)
    try:

        def reject_duplicates(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError
                result[key] = value
            return result

        payload = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=reject_duplicates,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
        )
    except (UnicodeError, ValueError, json.JSONDecodeError):
        raise ProductionBackupError("BACKUP_STATE_JSON_INVALID", "VALIDATION") from None
    if not isinstance(payload, dict):
        raise ProductionBackupError("BACKUP_STATE_JSON_INVALID", "VALIDATION")
    return payload


def _write_private_bytes(path: Path, payload: bytes) -> None:
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(path, 0o600)
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _writer_state_digest(states: Mapping[str, str]) -> str:
    return sha256_identity(canonical_json_bytes(dict(sorted(states.items()))))


def _canonical_media_path(value: str) -> str:
    path = PurePosixPath(value)
    if (
        not value
        or "\\" in value
        or path.is_absolute()
        or path.as_posix() != value
        or any(part in {"", ".", ".."} for part in path.parts)
        or any("\x00" in part for part in path.parts)
    ):
        raise ProductionBackupError("BACKUP_MEDIA_REFERENCE_INVALID", "VALIDATION")
    return value


def _paths_overlap(left: Path, right: Path) -> bool:
    left = left.absolute()
    right = right.absolute()
    try:
        left.relative_to(right)
        return True
    except ValueError:
        pass
    try:
        right.relative_to(left)
        return True
    except ValueError:
        return False


def _linklike(path: Path) -> bool:
    return path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction())


def _reject_linklike_ancestors(path: Path, error_code: str) -> None:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        if _linklike(current):
            raise ProductionBackupError(error_code, "VALIDATION")


def _sha256_regular_file(path: Path) -> str:
    descriptor = -1
    try:
        before = path.lstat()
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or _linklike(path):
            raise ProductionBackupError("BACKUP_MEDIA_REFERENCE_MISSING", "VALIDATION")
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
        ):
            raise ProductionBackupError("BACKUP_MEDIA_REFERENCE_CHANGED", "VALIDATION")
        digest = hashlib.sha256()
        while chunk := os.read(descriptor, 1024 * 1024):
            digest.update(chunk)
        after = os.fstat(descriptor)
        if (
            after.st_size != before.st_size
            or after.st_mtime_ns != before.st_mtime_ns
            or (after.st_dev, after.st_ino) != (before.st_dev, before.st_ino)
        ):
            raise ProductionBackupError("BACKUP_MEDIA_REFERENCE_CHANGED", "VALIDATION")
        return digest.hexdigest()
    except ProductionBackupError:
        raise
    except OSError:
        raise ProductionBackupError(
            "BACKUP_MEDIA_REFERENCE_MISSING", "VALIDATION"
        ) from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


__all__ = [
    "BackupPlan",
    "BackupReceipt",
    "ContainerPgDumpRunner",
    "DockerComposeBackupHost",
    "LocalBackupAuthority",
    "MediaInventory",
    "ProductionBackupError",
    "ProductionBackupRuntime",
    "ProtectionRequest",
    "production_backup_runtime",
    "verify_protected_backup",
]
