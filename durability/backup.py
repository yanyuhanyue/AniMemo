"""Canonical AniMemo instance Backup Format v1 runtime.

This module creates and structurally verifies disaster-recovery artifacts.  It
does not restore an artifact, discover legacy layouts, or copy PostgreSQL data
directories.  Callers must establish the write barrier described by the frozen
Backup Contract before invoking :func:`create_backup`.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import stat
import subprocess
import unicodedata
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Protocol

from .canonical import canonical_json_bytes, sha256_identity

FORMAT = "animemo-instance-backup"
SCHEMA_VERSION = 1
MATRIX_VERSION = "animemo.compatibility/v1"
MANIFEST_NAME = "backup-manifest.json"
CHECKSUMS_NAME = "checksums.sha256"
DATABASE_MEMBER = "database.sql.gz"
STAGING_PREFIX = ".animemo-backup-staging-"
STAGING_STATE_NAME = "staging-state.json"
PG_DUMP_ARGUMENTS = ("--format=plain", "--no-owner", "--no-privileges")

_ALLOWED_FILESYSTEM_ROOTS = frozenset(
    {
        "filesystem/config",
        "filesystem/plugins/cas",
        "filesystem/plugins/durable",
        "filesystem/media",
        "filesystem/private",
        "updater-state",
    }
)
_EXCLUDED_COMPONENTS = (
    "postgresql-physical-data",
    "redis",
    "plugin-runtime",
    "logs",
    "temporary-files",
    "nested-backups",
    "application-binaries",
    "updater-network-cache",
    "runtime-sockets-and-locks",
    "host-credential-stores",
    "docker-images-and-volume-snapshots",
    "proxy-dns-tls-firewall-configuration",
)
_SECRET_KEY_FRAGMENTS = (
    "password",
    "passwd",
    "secret",
    "token",
    "credential",
    "authorization",
    "privatekey",
    "encryptionkey",
    "databaseurl",
)
_FINAL_NAME = re.compile(
    r"^backup-(?P<stamp>[0-9]{8}T[0-9]{6}Z)-(?P<id>[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12})$"
)
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_CHECKSUM_LINE = re.compile(r"^(?P<digest>[0-9a-f]{64})  (?P<path>[^\r\n]+)$")
_MAX_MANIFEST_BYTES = 8 * 1024 * 1024
_MAX_CHECKSUM_BYTES = 64 * 1024 * 1024
_MAX_SECRET_REFERENCE_BYTES = 1024 * 1024
_MAX_NON_SECRET_METADATA_BYTES = 1024 * 1024
_MAX_MEMBER_COUNT = 250_000
_FORBIDDEN_SOURCE_PARTS = frozenset(
    {
        ".docker",
        ".env",
        ".locks",
        "backups",
        "cache",
        "gh",
        "logs",
        "postgres",
        "previews",
        "redis",
        "runtime",
        "setup-code",
        "sockets",
        "staging",
        "temp",
        "tmp",
    }
)


class BackupError(RuntimeError):
    """A stable, redacted Backup Runtime failure."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


class UnsupportedBackupFormat(BackupError):
    """The artifact has a bounded identity that this v1 reader cannot consume."""

    def __init__(self) -> None:
        super().__init__("BACKUP_FORMAT_UNSUPPORTED", "backup format or schema version is unsupported")


@dataclass(frozen=True)
class BackupSourceIdentity:
    instance_id: str
    source_locator_digest: str
    release: Mapping[str, Any]
    deployment_contract: Mapping[str, Any]
    database_contract: Mapping[str, Any]
    configuration_contract: Mapping[str, Any]
    plugin_sdk_apis: tuple[str, ...] = ()


@dataclass(frozen=True)
class FilesystemSource:
    """One explicit canonical source root; arbitrary instance trees are refused."""

    logical_root: str
    source: Path


@dataclass(frozen=True)
class SecretSource:
    """Opaque Envelope bytes or a non-secret external reference.

    Encryption and reference resolution remain owned by their canonical
    runtimes.  Backup never reads plaintext secret payloads.
    """

    mode: str
    source: Path | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    envelope_factory: Callable[[SecretEnvelopeBinding], bytes] | None = field(
        default=None,
        repr=False,
        compare=False,
    )


@dataclass(frozen=True)
class SecretEnvelopeBinding:
    artifact_id: str
    artifact_binding_record: Mapping[str, Any]
    artifact_binding_digest: str
    source_instance_id: str


@dataclass(frozen=True)
class R2Reference:
    backend_type: str
    endpoint_identity: str
    bucket: str
    object_keys: tuple[str, ...]


@dataclass(frozen=True)
class BackupRequest:
    destination_root: Path
    database_url: str = field(repr=False)
    source: BackupSourceIdentity
    filesystem_sources: tuple[FilesystemSource, ...]
    secret: SecretSource | None = None
    local_media_references: Mapping[str, str] = field(default_factory=dict)
    r2_references: tuple[R2Reference, ...] = ()
    producer: Mapping[str, Any] = field(default_factory=dict)
    platform: Mapping[str, Any] = field(default_factory=dict)
    quiescence: Mapping[str, Any] = field(default_factory=dict)
    pg_dump_executable: str = "pg_dump"
    pg_dump_timeout: int = 600


@dataclass(frozen=True)
class BackupResult:
    path: Path
    backup_id: str
    manifest_digest: str
    checksum_set_digest: str
    compatibility_artifact: Mapping[str, Any]

    def as_compatibility_artifact(self) -> Any:
        """Return the canonical evaluator's typed artifact identity."""

        from .compatibility import ArtifactIdentity

        return ArtifactIdentity(
            format_identity=FORMAT,
            format_version=SCHEMA_VERSION,
            artifact_id=self.backup_id,
            manifest_digest=self.manifest_digest,
        )


@dataclass(frozen=True)
class BackupVerification:
    path: Path
    backup_id: str
    manifest_digest: str
    checksum_set_digest: str
    database_uncompressed_bytes: int
    compatibility_artifact: Mapping[str, Any]

    def as_compatibility_artifact(self) -> Any:
        """Return input ready for ``evaluate_compatibility``."""

        from .compatibility import ArtifactIdentity

        return ArtifactIdentity(
            format_identity=FORMAT,
            format_version=SCHEMA_VERSION,
            artifact_id=self.backup_id,
            manifest_digest=self.manifest_digest,
        )


class PgDumpRunner(Protocol):
    def run(
        self,
        database_url: str,
        raw_output: Path,
        *,
        executable: str,
        timeout: int,
    ) -> str: ...


class SubprocessPgDumpRunner:
    """Invoke only the frozen logical pg_dump command profile, without a shell."""

    def run(
        self,
        database_url: str,
        raw_output: Path,
        *,
        executable: str,
        timeout: int,
    ) -> str:
        environment = os.environ.copy()
        environment["PGDATABASE"] = database_url
        try:
            version = subprocess.run(
                [executable, "--version"],
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=min(timeout, 30),
                shell=False,
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise BackupError("PG_DUMP_VERSION_FAILED", "pg_dump version probe failed") from error
        if version.returncode != 0:
            raise BackupError("PG_DUMP_VERSION_FAILED", "pg_dump version probe failed")
        tool_version = version.stdout.decode("utf-8", "replace").strip()
        if not tool_version.startswith("pg_dump ") or len(tool_version) > 200:
            raise BackupError("PG_DUMP_VERSION_INVALID", "pg_dump returned invalid version metadata")

        descriptor = -1
        try:
            descriptor = os.open(raw_output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                descriptor = -1
                completed = subprocess.run(
                    [executable, *PG_DUMP_ARGUMENTS],
                    check=False,
                    stdout=stream,
                    stderr=subprocess.DEVNULL,
                    env=environment,
                    timeout=timeout,
                    shell=False,
                )
                stream.flush()
                os.fsync(stream.fileno())
        except subprocess.TimeoutExpired as error:
            raise BackupError("PG_DUMP_TIMEOUT", "logical database dump timed out") from error
        except OSError as error:
            raise BackupError("PG_DUMP_FAILED", "logical database dump could not start") from error
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        if completed.returncode != 0:
            raise BackupError("PG_DUMP_FAILED", "logical database dump failed")
        return tool_version


def create_backup(
    request: BackupRequest,
    *,
    pg_dump_runner: PgDumpRunner | None = None,
    backup_id: uuid.UUID | None = None,
    clock: Callable[[], datetime] | None = None,
) -> BackupResult:
    """Create, verify, then atomically publish one Backup Format v1 artifact."""

    _validate_request(request)
    source_files = _preflight_sources(request)
    clock = clock or (lambda: datetime.now(UTC))
    started_at = _require_utc(clock(), field_name="startedAt")
    identifier = backup_id or uuid.uuid4()
    _require_uuid(str(identifier), field_name="backupId")
    stamp = started_at.strftime("%Y%m%dT%H%M%SZ")
    final_name = f"backup-{stamp}-{identifier}"

    destination = _prepare_destination_root(request.destination_root)
    staging = destination / f"{STAGING_PREFIX}{identifier}"
    final_path = destination / final_name
    if staging.exists() or final_path.exists():
        raise BackupError("BACKUP_DESTINATION_EXISTS", "backup identity already exists at destination")
    _make_private_directory(staging)
    _write_private_bytes(
        staging / STAGING_STATE_NAME,
        canonical_json_bytes({"lifecycle": "STAGING", "format": FORMAT, "schemaVersion": SCHEMA_VERSION}) + b"\n",
    )

    try:
        database = _create_database_member(
            request,
            staging,
            pg_dump_runner or SubprocessPgDumpRunner(),
        )
        member_records = _copy_payload_members(request, source_files, staging)
        member_records.append(database["member"])
        member_records.sort(key=lambda item: item["path"].encode("utf-8"))

        media = _build_media_metadata(request, member_records)
        source_identity = _source_identity_json(request.source)
        binding_record = {
            "format": FORMAT,
            "schemaVersion": SCHEMA_VERSION,
            "backupId": str(identifier),
            "startedAt": _utc_string(started_at),
            "source": source_identity,
            "database": {
                key: database[key]
                for key in (
                    "path",
                    "dumpProfile",
                    "serverMajor",
                    "toolVersion",
                    "uncompressedSha256",
                    "uncompressedBytes",
                    "compressedSha256",
                    "compressedBytes",
                )
            },
            "payloadMembers": [
                item for item in member_records if not item["path"].startswith("secrets/")
            ],
            "media": media,
            "secretDisposition": {
                "mode": request.secret.mode if request.secret is not None else "none",
                "path": _secret_member_path(request.secret.mode) if request.secret is not None else None,
                "profileIdentity": _secret_profile_identity(request.secret),
            },
        }
        artifact_binding_digest = sha256_identity(canonical_json_bytes(binding_record))
        secret_record = _copy_secret_member(
            request.secret,
            staging,
            binding=SecretEnvelopeBinding(
                artifact_id=str(identifier),
                artifact_binding_record=binding_record,
                artifact_binding_digest=artifact_binding_digest,
                source_instance_id=request.source.instance_id,
            ),
        )
        if secret_record is not None:
            member_records.append(secret_record)
            member_records.sort(key=lambda item: item["path"].encode("utf-8"))
        checksum_bytes = _checksum_bytes(member_records)
        _write_private_bytes(staging / CHECKSUMS_NAME, checksum_bytes)
        checksum_set_digest = sha256_identity(checksum_bytes)
        staging_state = staging / STAGING_STATE_NAME
        staging_state.unlink()

        completed_at = _require_utc(clock(), field_name="completedAt")
        if completed_at < started_at:
            raise BackupError("BACKUP_TIME_INVALID", "completedAt precedes startedAt")
        manifest = _build_manifest(
            request=request,
            identifier=identifier,
            started_at=started_at,
            completed_at=completed_at,
            source_identity=source_identity,
            database=database,
            member_records=member_records,
            media=media,
            checksum_bytes=checksum_bytes,
            checksum_set_digest=checksum_set_digest,
            binding_record=binding_record,
            artifact_binding_digest=artifact_binding_digest,
            lifecycle="STAGING",
        )
        _write_manifest(staging, manifest)
        _verify_tree(staging, allow_staging=True, expected_lifecycle="STAGING")

        final_manifest = dict(manifest)
        final_manifest["lifecycle"] = "FINALIZED"
        final_manifest["verification"] = {
            "status": "STRUCTURALLY_VERIFIED",
            "verifiedAt": _utc_string(completed_at),
            "restoreRehearsed": False,
        }
        _write_manifest(staging, final_manifest)
        verification = _verify_tree(staging, allow_staging=True, expected_lifecycle="FINALIZED")
        try:
            _atomic_finalize(staging, final_path)
        except OSError as error:
            raise BackupError("BACKUP_FINALIZE_FAILED", "atomic backup publication failed") from error
        return BackupResult(
            path=final_path,
            backup_id=str(identifier),
            manifest_digest=verification.manifest_digest,
            checksum_set_digest=verification.checksum_set_digest,
            compatibility_artifact=verification.compatibility_artifact,
        )
    except BackupError:
        _mark_incomplete(staging)
        raise
    except OSError as error:
        _mark_incomplete(staging)
        raise BackupError("BACKUP_IO_FAILED", "backup filesystem operation failed") from error


def verify_backup(path: Path) -> BackupVerification:
    """Read-only structural verification.  This function never restores data."""

    return _verify_tree(Path(path), allow_staging=False, expected_lifecycle="FINALIZED")


def list_finalized_backups(destination_root: Path) -> tuple[Path, ...]:
    """Return structurally valid, finalized artifacts; hidden staging is ignored."""

    root = Path(destination_root)
    if not root.exists():
        return ()
    if _is_link_or_reparse(root) or not root.is_dir():
        raise BackupError("BACKUP_DESTINATION_INVALID", "backup destination is not a safe directory")
    found: list[Path] = []
    for candidate in sorted(root.iterdir(), key=lambda item: item.name.encode("utf-8")):
        if not candidate.is_dir() or _FINAL_NAME.fullmatch(candidate.name) is None:
            continue
        try:
            verify_backup(candidate)
        except BackupError:
            continue
        found.append(candidate)
    return tuple(found)


def _validate_request(request: BackupRequest) -> None:
    if not isinstance(request.database_url, str) or not request.database_url or "\x00" in request.database_url:
        raise BackupError("DATABASE_URL_INVALID", "authoritative database location is missing or invalid")
    if not request.pg_dump_executable or "\x00" in request.pg_dump_executable:
        raise BackupError("PG_DUMP_EXECUTABLE_INVALID", "pg_dump executable is invalid")
    if Path(request.pg_dump_executable).name.casefold() not in {"pg_dump", "pg_dump.exe"}:
        raise BackupError("PG_DUMP_EXECUTABLE_INVALID", "only the pg_dump executable is allowed")
    if not isinstance(request.pg_dump_timeout, int) or request.pg_dump_timeout <= 0:
        raise BackupError("PG_DUMP_TIMEOUT_INVALID", "pg_dump timeout must be positive")
    _require_uuid(request.source.instance_id, field_name="instanceId")
    _require_sha256(request.source.source_locator_digest, field_name="sourceLocatorDigest")
    for value, label in (
        (request.source.release, "release"),
        (request.source.deployment_contract, "deploymentContract"),
        (request.source.database_contract, "databaseContract"),
        (request.source.configuration_contract, "configurationContract"),
        (request.producer, "producer"),
        (request.platform, "platform"),
        (request.quiescence, "quiescence"),
    ):
        _validate_non_secret_json(value, label=label)
    if not request.source.release or not request.source.deployment_contract:
        raise BackupError("BACKUP_SOURCE_IDENTITY_INVALID", "release and deployment identities are required")
    if not request.source.database_contract or not request.source.configuration_contract:
        raise BackupError("BACKUP_SOURCE_IDENTITY_INVALID", "database and configuration contracts are required")
    if not request.quiescence.get("method"):
        raise BackupError("BACKUP_QUIESCENCE_MISSING", "an established quiescence method is required")
    if len(set(request.source.plugin_sdk_apis)) != len(request.source.plugin_sdk_apis):
        raise BackupError("BACKUP_PLUGIN_IDENTITY_INVALID", "enabled Plugin SDK identities are duplicated")
    if not all(isinstance(item, str) and item for item in request.source.plugin_sdk_apis):
        raise BackupError("BACKUP_PLUGIN_IDENTITY_INVALID", "enabled Plugin SDK identity is invalid")
    roots = [item.logical_root for item in request.filesystem_sources]
    if len(roots) != len(set(roots)):
        raise BackupError("BACKUP_SOURCE_DUPLICATE", "filesystem source roots are duplicated")
    for item in request.filesystem_sources:
        if item.logical_root not in _ALLOWED_FILESYSTEM_ROOTS:
            raise BackupError("BACKUP_SOURCE_NOT_ALLOWED", "filesystem source is outside the canonical allowlist")
    if set(roots) != set(_ALLOWED_FILESYSTEM_ROOTS):
        raise BackupError("BACKUP_SOURCE_INCOMPLETE", "all canonical filesystem roots must be explicit")
    if request.secret is not None:
        if request.secret.mode not in {"envelope", "reference"}:
            raise BackupError("BACKUP_SECRET_MODE_INVALID", "secret mode must be envelope or reference")
        _validate_non_secret_json(request.secret.metadata, label="secretMetadata")
        if request.secret.mode == "reference":
            if request.secret.source is None or request.secret.envelope_factory is not None:
                raise BackupError("BACKUP_SECRET_MODE_INVALID", "reference mode requires exactly one reference file")
        elif (request.secret.source is None) == (request.secret.envelope_factory is None):
            raise BackupError(
                "BACKUP_SECRET_MODE_INVALID",
                "envelope mode requires exactly one envelope file or post-binding factory",
            )
    for relative, digest in request.local_media_references.items():
        _canonical_relative_path(relative)
        _require_sha256(digest, field_name="localMediaReference")
    for reference in request.r2_references:
        if reference.backend_type != "r2" or not reference.endpoint_identity or not reference.bucket:
            raise BackupError("BACKUP_R2_REFERENCE_INVALID", "R2 physical identity is incomplete")
        if len(reference.object_keys) != len(set(reference.object_keys)):
            raise BackupError("BACKUP_R2_REFERENCE_INVALID", "R2 object keys are duplicated")
        if not all(isinstance(key, str) and key and "\x00" not in key for key in reference.object_keys):
            raise BackupError("BACKUP_R2_REFERENCE_INVALID", "R2 object key is invalid")
        _validate_non_secret_json(
            {
                "backendType": reference.backend_type,
                "endpointIdentity": reference.endpoint_identity,
                "bucket": reference.bucket,
                "objectKeys": reference.object_keys,
            },
            label="r2Reference",
        )
    physical_identities = [
        (reference.endpoint_identity, reference.bucket) for reference in request.r2_references
    ]
    if len(physical_identities) != len(set(physical_identities)):
        raise BackupError("BACKUP_R2_REFERENCE_INVALID", "R2 physical identities are duplicated")


def _preflight_sources(request: BackupRequest) -> Mapping[str, tuple[tuple[str, Path, int], ...]]:
    destination = _absolute_without_link(request.destination_root)
    source_files: dict[str, tuple[tuple[str, Path, int], ...]] = {}
    identities: set[tuple[int, int]] = set()
    for filesystem_source in request.filesystem_sources:
        source = Path(filesystem_source.source)
        absolute = _safe_source_directory(source)
        if _paths_overlap(destination, absolute):
            raise BackupError("BACKUP_SOURCE_OVERLAP", "backup destination overlaps a source root")
        entries = _enumerate_source_files(absolute)
        if filesystem_source.logical_root == "filesystem/private" and entries:
            raise BackupError(
                "BACKUP_SOURCE_NOT_ALLOWED",
                "no private member is registered for Backup Format v1",
            )
        for relative, file_path, _ in entries:
            _validate_source_relative(filesystem_source.logical_root, relative)
            if filesystem_source.logical_root in {"filesystem/config", "updater-state"}:
                _validate_non_secret_metadata_file(file_path)
            file_stat = file_path.lstat()
            identity = (file_stat.st_dev, file_stat.st_ino)
            if identity in identities:
                raise BackupError("BACKUP_UNSAFE_SOURCE", "hard-linked or repeated source member is not allowed")
            identities.add(identity)
        source_files[filesystem_source.logical_root] = entries
    if request.secret is not None and request.secret.source is not None:
        secret_path = _safe_source_file(Path(request.secret.source))
        if _paths_overlap(destination, secret_path):
            raise BackupError("BACKUP_SOURCE_OVERLAP", "backup destination overlaps the secret source")
        secret_stat = secret_path.lstat()
        identity = (secret_stat.st_dev, secret_stat.st_ino)
        if identity in identities or secret_stat.st_nlink != 1:
            raise BackupError("BACKUP_UNSAFE_SOURCE", "secret member must not be hard linked")
        if request.secret.mode == "reference":
            _validate_secret_reference(secret_path)
    media_entries = {
        relative: path
        for relative, path, _ in source_files.get("filesystem/media", ())
    }
    for relative, expected_digest in request.local_media_references.items():
        source_path = media_entries.get(relative)
        if source_path is None:
            raise BackupError("BACKUP_MEDIA_REFERENCE_MISSING", "database-referenced local media is missing")
        if f"sha256:{_sha256_file(source_path)}" != expected_digest:
            raise BackupError("BACKUP_MEDIA_REFERENCE_MISMATCH", "database-referenced local media failed identity verification")
    return source_files


def _create_database_member(
    request: BackupRequest,
    staging: Path,
    runner: PgDumpRunner,
) -> dict[str, Any]:
    raw = staging / ".database.sql.raw"
    compressed = staging / DATABASE_MEMBER
    try:
        tool_version = runner.run(
            request.database_url,
            raw,
            executable=request.pg_dump_executable,
            timeout=request.pg_dump_timeout,
        )
        if not raw.is_file() or _is_link_or_reparse(raw):
            raise BackupError("PG_DUMP_MISSING", "logical database dump was not produced")
        raw_stat = raw.lstat()
        if not stat.S_ISREG(raw_stat.st_mode) or raw_stat.st_nlink != 1:
            raise BackupError("PG_DUMP_UNSAFE", "logical database dump staging member is unsafe")
        if raw_stat.st_size == 0:
            raise BackupError("PG_DUMP_EMPTY", "logical database dump is empty")
        uncompressed_digest = _sha256_file(raw)
        descriptor = os.open(compressed, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as target:
                descriptor = -1
                with raw.open("rb") as source, gzip.GzipFile(fileobj=target, mode="wb", mtime=0) as gzip_stream:
                    for chunk in iter(lambda: source.read(1024 * 1024), b""):
                        gzip_stream.write(chunk)
                target.flush()
                os.fsync(target.fileno())
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        os.chmod(compressed, 0o600)
        compressed_bytes = compressed.stat().st_size
        compressed_digest = _sha256_file(compressed)
        server_major = request.source.database_contract.get("serverMajor")
        if isinstance(server_major, bool) or not isinstance(server_major, int) or server_major <= 0:
            raise BackupError("BACKUP_DATABASE_METADATA_INVALID", "PostgreSQL server major is required")
        if (
            not isinstance(tool_version, str)
            or not tool_version.startswith("pg_dump ")
            or len(tool_version) > 200
            or any(character in tool_version for character in "\r\n\x00")
        ):
            raise BackupError("PG_DUMP_VERSION_INVALID", "pg_dump returned invalid version metadata")
        member = {
            "path": DATABASE_MEMBER,
            "sha256": f"sha256:{compressed_digest}",
            "sizeBytes": compressed_bytes,
            "sourceMode": "logical-postgresql",
        }
        return {
            "path": DATABASE_MEMBER,
            "dumpProfile": {
                "format": "plain",
                "argv": list(PG_DUMP_ARGUMENTS),
                "compression": "gzip",
                "gzipMtime": 0,
            },
            "serverMajor": server_major,
            "toolVersion": tool_version,
            "uncompressedSha256": f"sha256:{uncompressed_digest}",
            "uncompressedBytes": raw_stat.st_size,
            "compressedSha256": f"sha256:{compressed_digest}",
            "compressedBytes": compressed_bytes,
            "member": member,
        }
    finally:
        raw.unlink(missing_ok=True)


def _copy_payload_members(
    request: BackupRequest,
    source_files: Mapping[str, tuple[tuple[str, Path, int], ...]],
    staging: Path,
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for filesystem_source in sorted(request.filesystem_sources, key=lambda item: item.logical_root.encode("utf-8")):
        root_target = staging / PurePosixPath(filesystem_source.logical_root)
        _make_private_directory(root_target)
        for relative, source, source_mode in source_files[filesystem_source.logical_root]:
            logical_path = f"{filesystem_source.logical_root}/{relative}"
            target = staging / PurePosixPath(logical_path)
            _make_private_directory(target.parent)
            _copy_regular_file(source, target)
            records.append(
                {
                    "path": logical_path,
                    "sha256": f"sha256:{_sha256_file(target)}",
                    "sizeBytes": target.stat().st_size,
                    "sourceMode": f"{source_mode:04o}",
                }
            )
    return records


def _copy_secret_member(
    secret: SecretSource | None,
    staging: Path,
    *,
    binding: SecretEnvelopeBinding,
) -> dict[str, Any] | None:
    if secret is None:
        return None
    logical_path = _secret_member_path(secret.mode)
    target = staging / PurePosixPath(logical_path)
    _make_private_directory(target.parent)
    if secret.envelope_factory is not None:
        try:
            envelope_bytes = secret.envelope_factory(binding)
        except BackupError:
            raise
        except Exception:  # noqa: BLE001 - callback failures are normalized and never rendered
            raise BackupError(
                "BACKUP_SECRET_ENVELOPE_FAILED", "secret envelope creation failed"
            ) from None
        if not isinstance(envelope_bytes, bytes):
            raise BackupError("BACKUP_SECRET_ENVELOPE_INVALID", "secret envelope factory returned invalid bytes")
        _validate_envelope_binding(envelope_bytes, binding)
        _write_private_bytes(target, envelope_bytes)
    else:
        if secret.source is None:  # guarded by request validation
            raise BackupError("BACKUP_SECRET_MODE_INVALID", "secret source is missing")
        source = _safe_source_file(Path(secret.source))
        if secret.mode == "envelope":
            envelope_bytes = _read_regular_file(source, maximum=_MAX_MANIFEST_BYTES)
            _validate_envelope_binding(envelope_bytes, binding)
        else:
            _validate_secret_reference(source)
        _copy_regular_file(source, target)
    return {
        "path": logical_path,
        "sha256": f"sha256:{_sha256_file(target)}",
        "sizeBytes": target.stat().st_size,
        "sourceMode": "protected-secret-artifact",
    }


def _build_media_metadata(
    request: BackupRequest,
    member_records: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    prefix = "filesystem/media/"
    local_records = {
        str(record["path"])[len(prefix) :]: record
        for record in member_records
        if str(record["path"]).startswith(prefix)
    }
    referenced = []
    for path, digest in sorted(request.local_media_references.items(), key=lambda item: item[0].encode("utf-8")):
        record = local_records[path]
        referenced.append({"path": path, "sha256": digest, "sizeBytes": record["sizeBytes"]})
    preserved = [
        {"path": path, "sha256": record["sha256"], "sizeBytes": record["sizeBytes"]}
        for path, record in sorted(local_records.items(), key=lambda item: item[0].encode("utf-8"))
        if path not in request.local_media_references
    ]
    external = [
        {
            "backendType": item.backend_type,
            "endpointIdentity": item.endpoint_identity,
            "bucket": item.bucket,
            "objectKeys": sorted(item.object_keys, key=lambda value: value.encode("utf-8")),
            "coverage": "reference-dependent",
            "remoteBytesCopied": False,
        }
        for item in sorted(
            request.r2_references,
            key=lambda value: (value.endpoint_identity.encode("utf-8"), value.bucket.encode("utf-8")),
        )
    ]
    return {
        "local": {
            "mode": "captured",
            "referenced": referenced,
            "preservedUnreferenced": preserved,
        },
        "external": external,
        "unknownOrphanPolicy": "PRESERVE_NEVER_DELETE",
    }


def _verify_media_metadata(
    media: Any,
    manifest_members: Mapping[str, Mapping[str, Any]],
) -> None:
    if (
        not isinstance(media, dict)
        or set(media) != {"local", "external", "unknownOrphanPolicy"}
        or media.get("unknownOrphanPolicy") != "PRESERVE_NEVER_DELETE"
    ):
        raise BackupError("BACKUP_MANIFEST_INVALID", "media coverage metadata is invalid")
    local = media.get("local")
    external = media.get("external")
    if (
        not isinstance(local, dict)
        or set(local) != {"mode", "referenced", "preservedUnreferenced"}
        or local.get("mode") != "captured"
        or not isinstance(external, list)
    ):
        raise BackupError("BACKUP_MANIFEST_INVALID", "media coverage metadata is invalid")
    referenced = local.get("referenced")
    preserved = local.get("preservedUnreferenced")
    if not isinstance(referenced, list) or not isinstance(preserved, list):
        raise BackupError("BACKUP_MANIFEST_INVALID", "local media coverage is invalid")
    prefix = "filesystem/media/"
    local_members = {
        path[len(prefix) :]: record
        for path, record in manifest_members.items()
        if path.startswith(prefix)
    }
    declared: set[str] = set()
    for raw_record in [*referenced, *preserved]:
        if not isinstance(raw_record, dict) or set(raw_record) != {"path", "sha256", "sizeBytes"}:
            raise BackupError("BACKUP_MANIFEST_INVALID", "local media record is invalid")
        path = raw_record.get("path")
        if not isinstance(path, str) or path in declared or path not in local_members:
            raise BackupError("BACKUP_MANIFEST_INVALID", "local media record is invalid")
        declared.add(path)
        member = local_members[path]
        if raw_record.get("sha256") != member.get("sha256") or raw_record.get("sizeBytes") != member.get("sizeBytes"):
            raise BackupError("BACKUP_MANIFEST_INVALID", "local media identity is invalid")
    if declared != set(local_members):
        raise BackupError("BACKUP_MANIFEST_INVALID", "local media coverage is incomplete")
    physical_identities: set[tuple[str, str]] = set()
    for reference in external:
        if not isinstance(reference, dict) or set(reference) != {
            "backendType",
            "endpointIdentity",
            "bucket",
            "objectKeys",
            "coverage",
            "remoteBytesCopied",
        }:
            raise BackupError("BACKUP_MANIFEST_INVALID", "external media reference is invalid")
        if (
            reference.get("backendType") != "r2"
            or not isinstance(reference.get("endpointIdentity"), str)
            or not reference["endpointIdentity"]
            or not isinstance(reference.get("bucket"), str)
            or not reference["bucket"]
            or reference.get("coverage") != "reference-dependent"
            or reference.get("remoteBytesCopied") is not False
        ):
            raise BackupError("BACKUP_MANIFEST_INVALID", "external media reference is invalid")
        identity = (reference["endpointIdentity"], reference["bucket"])
        if identity in physical_identities:
            raise BackupError("BACKUP_MANIFEST_INVALID", "external media identity is duplicated")
        physical_identities.add(identity)
        keys = reference.get("objectKeys")
        if not isinstance(keys, list) or not all(
            isinstance(key, str) and key and "\x00" not in key for key in keys
        ):
            raise BackupError("BACKUP_MANIFEST_INVALID", "external media object keys are invalid")
        if keys != sorted(keys, key=lambda value: value.encode("utf-8")) or len(keys) != len(set(keys)):
            raise BackupError("BACKUP_MANIFEST_INVALID", "external media object keys are invalid")


def _build_manifest(
    *,
    request: BackupRequest,
    identifier: uuid.UUID,
    started_at: datetime,
    completed_at: datetime,
    source_identity: Mapping[str, Any],
    database: Mapping[str, Any],
    member_records: Sequence[Mapping[str, Any]],
    media: Mapping[str, Any],
    checksum_bytes: bytes,
    checksum_set_digest: str,
    binding_record: Mapping[str, Any],
    artifact_binding_digest: str,
    lifecycle: str,
) -> dict[str, Any]:
    roots = sorted((source.logical_root for source in request.filesystem_sources), key=lambda item: item.encode("utf-8"))
    plugin_records = [record for record in member_records if str(record["path"]).startswith("filesystem/plugins/")]
    included = ["database"]
    included.extend(roots)
    if request.secret is not None:
        included.append(f"secrets/{request.secret.mode}")
    compatibility = {
        "matrixVersion": MATRIX_VERSION,
        "operation": "restore",
        "artifact": {
            "format": FORMAT,
            "schemaVersion": SCHEMA_VERSION,
            "artifactId": str(identifier),
            "checksumSetDigest": checksum_set_digest,
            "artifactBindingDigest": artifact_binding_digest,
        },
    }
    return {
        "format": FORMAT,
        "schemaVersion": SCHEMA_VERSION,
        "backupId": str(identifier),
        "startedAt": _utc_string(started_at),
        "createdAt": _utc_string(completed_at),
        "completedAt": _utc_string(completed_at),
        "lifecycle": lifecycle,
        "artifactBindingRecord": binding_record,
        "artifactBindingDigest": artifact_binding_digest,
        "source": source_identity,
        "database": {
            key: database[key]
            for key in (
                "path",
                "dumpProfile",
                "serverMajor",
                "toolVersion",
                "uncompressedSha256",
                "uncompressedBytes",
                "compressedSha256",
                "compressedBytes",
            )
        },
        "filesystem": {
            "allowlist": sorted(_ALLOWED_FILESYSTEM_ROOTS, key=lambda item: item.encode("utf-8")),
            "includedRoots": roots,
            "members": list(member_records),
        },
        "plugins": {
            "sdkApis": sorted(request.source.plugin_sdk_apis),
            "durableAndCasMembers": plugin_records,
            "runtimeIncluded": False,
        },
        "media": media,
        "secrets": {
            "mode": request.secret.mode if request.secret is not None else "none",
            "path": _secret_member_path(request.secret.mode) if request.secret is not None else None,
            "metadata": dict(request.secret.metadata) if request.secret is not None else {},
            "plaintextIncluded": False,
        },
        "includedComponents": sorted(included, key=lambda item: item.encode("utf-8")),
        "excludedComponents": list(_EXCLUDED_COMPONENTS),
        "compatibility": compatibility,
        "checksums": {
            "path": CHECKSUMS_NAME,
            "sha256": sha256_identity(checksum_bytes),
            "memberCount": len(member_records),
        },
        "quiescence": dict(request.quiescence),
        "producer": dict(request.producer),
        "platform": dict(request.platform),
        "verification": {
            "status": "PENDING" if lifecycle == "STAGING" else "STRUCTURALLY_VERIFIED",
            "restoreRehearsed": False,
        },
        "restorePrerequisites": [
            "Verify exact Release Authority before obtaining application or deployment bytes.",
            "Resolve or authenticate the declared secret mode before restore mutation.",
            "Validate reference-dependent external media without deleting unknown objects.",
        ],
        "memoryIntegrity": ["MI-1", "MI-2", "MI-3", "MI-4", "MI-5"],
    }


def _write_manifest(root: Path, manifest: Mapping[str, Any]) -> None:
    temporary = root / f".{MANIFEST_NAME}.tmp"
    data = canonical_json_bytes(manifest) + b"\n"
    _write_private_bytes(temporary, data)
    try:
        os.replace(temporary, root / MANIFEST_NAME)
        os.chmod(root / MANIFEST_NAME, 0o600)
        _fsync_directory(root)
    finally:
        temporary.unlink(missing_ok=True)


def _verify_tree(
    path: Path,
    *,
    allow_staging: bool,
    expected_lifecycle: str,
) -> BackupVerification:
    root = Path(path)
    if _is_link_or_reparse(root) or not root.is_dir():
        raise BackupError("BACKUP_ROOT_INVALID", "backup root is not a safe directory")
    _require_private_mode(root, directory=True)
    if root.name.startswith(STAGING_PREFIX) and not allow_staging:
        raise BackupError("BACKUP_NOT_FINALIZED", "staging artifacts are not published backups")
    final_match = _FINAL_NAME.fullmatch(root.name)
    if not allow_staging and final_match is None:
        raise BackupError("BACKUP_NAME_INVALID", "published backup directory name is invalid")
    manifest_path = root / MANIFEST_NAME
    if _is_link_or_reparse(manifest_path) or not manifest_path.is_file():
        raise BackupError("BACKUP_MANIFEST_INVALID", "backup manifest is missing or unsafe")
    manifest_bytes = _read_regular_file(manifest_path, maximum=_MAX_MANIFEST_BYTES)
    manifest = _strict_json_object(manifest_bytes, code="BACKUP_MANIFEST_INVALID")
    format_name = manifest.get("format")
    schema_version = manifest.get("schemaVersion")
    if (
        isinstance(format_name, str)
        and isinstance(schema_version, int)
        and not isinstance(schema_version, bool)
        and (
        format_name != FORMAT or schema_version != SCHEMA_VERSION
        )
    ):
        raise UnsupportedBackupFormat()
    if format_name != FORMAT or schema_version != SCHEMA_VERSION or isinstance(schema_version, bool):
        raise BackupError("BACKUP_MANIFEST_INVALID", "backup format identity is malformed")
    if canonical_json_bytes(manifest) + b"\n" != manifest_bytes:
        raise BackupError("BACKUP_MANIFEST_INVALID", "backup manifest is not canonical JSON")
    required = {
        "backupId",
        "startedAt",
        "createdAt",
        "completedAt",
        "lifecycle",
        "artifactBindingRecord",
        "artifactBindingDigest",
        "source",
        "database",
        "filesystem",
        "plugins",
        "media",
        "secrets",
        "includedComponents",
        "excludedComponents",
        "compatibility",
        "checksums",
        "quiescence",
        "producer",
        "platform",
        "verification",
        "memoryIntegrity",
        "restorePrerequisites",
    }
    if set(manifest) != required | {"format", "schemaVersion"}:
        raise BackupError("BACKUP_MANIFEST_INVALID", "backup manifest fields are invalid")
    if manifest["lifecycle"] != expected_lifecycle:
        raise BackupError("BACKUP_NOT_FINALIZED", "backup lifecycle is not finalized")
    backup_id = _require_uuid(manifest["backupId"], field_name="backupId")
    if final_match is not None:
        if final_match.group("id") != backup_id:
            raise BackupError("BACKUP_IDENTITY_MISMATCH", "backup directory and manifest identities differ")
        expected_stamp = _parse_utc(manifest["startedAt"], field_name="startedAt").strftime("%Y%m%dT%H%M%SZ")
        if final_match.group("stamp") != expected_stamp:
            raise BackupError("BACKUP_IDENTITY_MISMATCH", "backup directory timestamp and manifest differ")
    _parse_utc(manifest["completedAt"], field_name="completedAt")
    if manifest.get("createdAt") != manifest["completedAt"]:
        raise BackupError("BACKUP_MANIFEST_INVALID", "createdAt and completedAt are inconsistent")
    if manifest["excludedComponents"] != list(_EXCLUDED_COMPONENTS):
        raise BackupError("BACKUP_MANIFEST_INVALID", "backup exclusions do not match Format v1")
    if manifest["memoryIntegrity"] != ["MI-1", "MI-2", "MI-3", "MI-4", "MI-5"]:
        raise BackupError("BACKUP_MANIFEST_INVALID", "memory integrity declarations are incomplete")
    binding_record = manifest["artifactBindingRecord"]
    if not isinstance(binding_record, dict):
        raise BackupError("BACKUP_MANIFEST_INVALID", "artifact binding record is invalid")
    expected_binding = sha256_identity(canonical_json_bytes(binding_record))
    if manifest["artifactBindingDigest"] != expected_binding:
        raise BackupError("BACKUP_BINDING_MISMATCH", "artifact binding digest is invalid")
    for value, label in (
        (manifest["source"], "source"),
        (manifest["producer"], "producer"),
        (manifest["platform"], "platform"),
        (manifest["quiescence"], "quiescence"),
    ):
        _validate_non_secret_json(value, label=label)
    source_metadata = manifest["source"]
    if not isinstance(source_metadata, dict) or set(source_metadata) != {
        "instanceId",
        "sourceLocatorDigest",
        "release",
        "deploymentContract",
        "databaseContract",
        "configurationContract",
        "pluginSdkApis",
    }:
        raise BackupError("BACKUP_MANIFEST_INVALID", "source identity is invalid")
    _require_uuid(source_metadata.get("instanceId"), field_name="instanceId")
    _require_sha256(source_metadata.get("sourceLocatorDigest"), field_name="sourceLocatorDigest")

    checksums_path = root / CHECKSUMS_NAME
    if _is_link_or_reparse(checksums_path) or not checksums_path.is_file():
        raise BackupError("BACKUP_CHECKSUMS_INVALID", "checksum set is missing or unsafe")
    checksum_bytes = _read_regular_file(checksums_path, maximum=_MAX_CHECKSUM_BYTES)
    checksum_set_digest = sha256_identity(checksum_bytes)
    checksums_metadata = manifest["checksums"]
    if (
        not isinstance(checksums_metadata, dict)
        or set(checksums_metadata) != {"path", "sha256", "memberCount"}
        or checksums_metadata.get("path") != CHECKSUMS_NAME
    ):
        raise BackupError("BACKUP_CHECKSUMS_INVALID", "checksum metadata is invalid")
    if checksums_metadata.get("sha256") != checksum_set_digest:
        raise BackupError("BACKUP_CHECKSUMS_INVALID", "checksum set digest is invalid")
    expected_members = _parse_checksum_bytes(checksum_bytes)
    if checksums_metadata.get("memberCount") != len(expected_members):
        raise BackupError("BACKUP_CHECKSUMS_INVALID", "checksum member count is invalid")
    members = manifest.get("filesystem", {}).get("members") if isinstance(manifest.get("filesystem"), dict) else None
    if not isinstance(members, list) or len(members) != len(expected_members):
        raise BackupError("BACKUP_MANIFEST_INVALID", "manifest member inventory is invalid")
    manifest_members: dict[str, Mapping[str, Any]] = {}
    for record in members:
        if not isinstance(record, dict) or not isinstance(record.get("path"), str):
            raise BackupError("BACKUP_MANIFEST_INVALID", "manifest member record is invalid")
        member_path = _canonical_relative_path(record["path"])
        _validate_artifact_member_path(member_path)
        size_bytes = record.get("sizeBytes")
        if isinstance(size_bytes, bool) or not isinstance(size_bytes, int) or size_bytes < 0:
            raise BackupError("BACKUP_MANIFEST_INVALID", "manifest member size is invalid")
        if member_path in manifest_members:
            raise BackupError("BACKUP_MANIFEST_INVALID", "manifest member path is duplicated")
        manifest_members[member_path] = record
    if set(manifest_members) != set(expected_members):
        raise BackupError("BACKUP_MANIFEST_INVALID", "manifest and checksum inventories differ")
    expected_binding_record = {
        "format": FORMAT,
        "schemaVersion": SCHEMA_VERSION,
        "backupId": backup_id,
        "startedAt": manifest["startedAt"],
        "source": manifest["source"],
        "database": manifest["database"],
        "payloadMembers": [
            manifest_members[path]
            for path in sorted(manifest_members, key=lambda value: value.encode("utf-8"))
            if not path.startswith("secrets/")
        ],
        "media": manifest["media"],
        "secretDisposition": {
            "mode": manifest["secrets"].get("mode") if isinstance(manifest["secrets"], dict) else None,
            "path": manifest["secrets"].get("path") if isinstance(manifest["secrets"], dict) else None,
            "profileIdentity": _secret_profile_identity_from_mode(
                manifest["secrets"].get("mode")
                if isinstance(manifest["secrets"], dict)
                else None
            ),
        },
    }
    if binding_record != expected_binding_record:
        raise BackupError("BACKUP_BINDING_MISMATCH", "artifact binding record and manifest differ")
    filesystem = manifest["filesystem"]
    expected_roots = sorted(_ALLOWED_FILESYSTEM_ROOTS, key=lambda item: item.encode("utf-8"))
    if (
        not isinstance(filesystem, dict)
        or filesystem.get("allowlist") != expected_roots
        or filesystem.get("includedRoots") != expected_roots
    ):
        raise BackupError("BACKUP_MANIFEST_INVALID", "filesystem allowlist is invalid")

    actual_members = _enumerate_artifact_files(root)
    expected_actual = set(expected_members) | {MANIFEST_NAME, CHECKSUMS_NAME}
    if actual_members != expected_actual:
        missing = set(expected_members) - actual_members
        if missing:
            raise BackupError("BACKUP_MEMBER_MISSING", "a required backup member is missing")
        raise BackupError("BACKUP_MEMBER_UNEXPECTED", "backup contains an unexpected member")
    for relative, expected_digest in expected_members.items():
        member_path = root / PurePosixPath(relative)
        actual_digest = _sha256_file(member_path)
        if actual_digest != expected_digest:
            raise BackupError("BACKUP_CHECKSUM_MISMATCH", "backup member checksum failed")
        record = manifest_members[relative]
        if record.get("sha256") != f"sha256:{expected_digest}" or record.get("sizeBytes") != member_path.stat().st_size:
            raise BackupError("BACKUP_MANIFEST_INVALID", "backup member metadata is inconsistent")

    database = manifest["database"]
    if (
        not isinstance(database, dict)
        or set(database)
        != {
            "path",
            "dumpProfile",
            "serverMajor",
            "toolVersion",
            "uncompressedSha256",
            "uncompressedBytes",
            "compressedSha256",
            "compressedBytes",
        }
        or database.get("path") != DATABASE_MEMBER
    ):
        raise BackupError("BACKUP_DATABASE_METADATA_INVALID", "database metadata is invalid")
    profile = database.get("dumpProfile")
    if (
        not isinstance(profile, dict)
        or set(profile) != {"format", "argv", "compression", "gzipMtime"}
        or profile.get("argv") != list(PG_DUMP_ARGUMENTS)
        or profile.get("format") != "plain"
        or profile.get("compression") != "gzip"
        or profile.get("gzipMtime") != 0
    ):
        raise BackupError("BACKUP_DATABASE_METADATA_INVALID", "database dump profile is not Format v1")
    if (
        isinstance(database.get("serverMajor"), bool)
        or not isinstance(database.get("serverMajor"), int)
        or not isinstance(database.get("toolVersion"), str)
        or not database["toolVersion"].startswith("pg_dump ")
    ):
        raise BackupError("BACKUP_DATABASE_METADATA_INVALID", "database version metadata is invalid")
    database_path = root / DATABASE_MEMBER
    uncompressed_digest = hashlib.sha256()
    uncompressed_bytes = 0
    try:
        with gzip.open(database_path, "rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                uncompressed_digest.update(chunk)
                uncompressed_bytes += len(chunk)
    except (OSError, EOFError) as error:
        raise BackupError("BACKUP_DATABASE_GZIP_INVALID", "database gzip stream is invalid") from error
    if uncompressed_bytes == 0:
        raise BackupError("BACKUP_DATABASE_EMPTY", "database dump is empty")
    if database.get("uncompressedBytes") != uncompressed_bytes or database.get("uncompressedSha256") != f"sha256:{uncompressed_digest.hexdigest()}":
        raise BackupError("BACKUP_DATABASE_METADATA_INVALID", "database uncompressed identity is inconsistent")
    if database.get("compressedBytes") != database_path.stat().st_size or database.get("compressedSha256") != f"sha256:{_sha256_file(database_path)}":
        raise BackupError("BACKUP_DATABASE_METADATA_INVALID", "database compressed identity is inconsistent")

    secrets = manifest["secrets"]
    if (
        not isinstance(secrets, dict)
        or set(secrets) != {"mode", "path", "metadata", "plaintextIncluded"}
        or secrets.get("mode") not in {"none", "envelope", "reference"}
    ):
        raise BackupError("BACKUP_MANIFEST_INVALID", "secret disposition is invalid")
    secret_paths = {path for path in expected_members if path.startswith("secrets/")}
    expected_secret_path = None if secrets["mode"] == "none" else _secret_member_path(secrets["mode"])
    if secret_paths != ({expected_secret_path} if expected_secret_path is not None else set()):
        raise BackupError("BACKUP_MANIFEST_INVALID", "secret member and disposition differ")
    if secrets.get("path") != expected_secret_path or secrets.get("plaintextIncluded") is not False:
        raise BackupError("BACKUP_MANIFEST_INVALID", "secret member metadata is invalid")
    if secrets["mode"] == "reference":
        _validate_secret_reference(root / str(expected_secret_path))
    elif secrets["mode"] == "envelope":
        _validate_envelope_binding(
            _read_regular_file(root / str(expected_secret_path), maximum=_MAX_MANIFEST_BYTES),
            SecretEnvelopeBinding(
                artifact_id=backup_id,
                artifact_binding_record=binding_record,
                artifact_binding_digest=expected_binding,
                source_instance_id=str(manifest["source"].get("instanceId", "")),
            ),
        )
    expected_included = ["database", *expected_roots]
    if secrets["mode"] != "none":
        expected_included.append(f"secrets/{secrets['mode']}")
    if manifest["includedComponents"] != sorted(expected_included, key=lambda item: item.encode("utf-8")):
        raise BackupError("BACKUP_MANIFEST_INVALID", "included component inventory is invalid")
    plugins = manifest["plugins"]
    expected_plugin_records = [
        manifest_members[path]
        for path in sorted(manifest_members, key=lambda value: value.encode("utf-8"))
        if path.startswith("filesystem/plugins/")
    ]
    if (
        not isinstance(plugins, dict)
        or plugins.get("sdkApis") != source_metadata.get("pluginSdkApis")
        or plugins.get("durableAndCasMembers") != expected_plugin_records
        or plugins.get("runtimeIncluded") is not False
    ):
        raise BackupError("BACKUP_MANIFEST_INVALID", "plugin durable metadata is invalid")
    _verify_media_metadata(manifest["media"], manifest_members)
    verification_metadata = manifest["verification"]
    if expected_lifecycle == "FINALIZED" and (
        not isinstance(verification_metadata, dict)
        or set(verification_metadata) != {"status", "restoreRehearsed", "verifiedAt"}
        or verification_metadata.get("status") != "STRUCTURALLY_VERIFIED"
        or verification_metadata.get("restoreRehearsed") is not False
        or verification_metadata.get("verifiedAt") != manifest["completedAt"]
    ):
        raise BackupError("BACKUP_MANIFEST_INVALID", "verification metadata is invalid")
    if expected_lifecycle == "STAGING" and (
        not isinstance(verification_metadata, dict)
        or set(verification_metadata) != {"status", "restoreRehearsed"}
        or verification_metadata.get("status") != "PENDING"
        or verification_metadata.get("restoreRehearsed") is not False
    ):
        raise BackupError("BACKUP_MANIFEST_INVALID", "verification metadata is invalid")

    compatibility = manifest["compatibility"]
    expected_compatibility = {
        "matrixVersion": MATRIX_VERSION,
        "operation": "restore",
        "artifact": {
            "format": FORMAT,
            "schemaVersion": SCHEMA_VERSION,
            "artifactId": backup_id,
            "checksumSetDigest": checksum_set_digest,
            "artifactBindingDigest": expected_binding,
        },
    }
    if compatibility != expected_compatibility:
        raise BackupError("BACKUP_COMPATIBILITY_INVALID", "canonical compatibility metadata is inconsistent")
    manifest_digest = sha256_identity(manifest_bytes)
    compatibility_artifact = {
        "format": FORMAT,
        "schemaVersion": SCHEMA_VERSION,
        "artifactId": backup_id,
        "manifestDigest": manifest_digest,
        "checksumSetDigest": checksum_set_digest,
        "artifactBindingDigest": expected_binding,
    }
    return BackupVerification(
        path=root,
        backup_id=backup_id,
        manifest_digest=manifest_digest,
        checksum_set_digest=checksum_set_digest,
        database_uncompressed_bytes=uncompressed_bytes,
        compatibility_artifact=compatibility_artifact,
    )


def _parse_checksum_bytes(data: bytes) -> dict[str, str]:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise BackupError("BACKUP_CHECKSUMS_INVALID", "checksum set is not UTF-8") from error
    if not text.endswith("\n") or "\r" in text:
        raise BackupError("BACKUP_CHECKSUMS_INVALID", "checksum set must use canonical LF records")
    records: dict[str, str] = {}
    normalized: set[str] = set()
    previous: bytes | None = None
    for line in text.splitlines():
        match = _CHECKSUM_LINE.fullmatch(line)
        if match is None:
            raise BackupError("BACKUP_CHECKSUMS_INVALID", "checksum record is malformed")
        relative = _canonical_relative_path(match.group("path"))
        _validate_artifact_member_path(relative)
        ordering = relative.encode("utf-8")
        if previous is not None and ordering <= previous:
            raise BackupError("BACKUP_CHECKSUMS_INVALID", "checksum records are not uniquely sorted")
        collision = unicodedata.normalize("NFC", relative).casefold()
        if collision in normalized:
            raise BackupError("BACKUP_CHECKSUMS_INVALID", "checksum paths have a normalization collision")
        normalized.add(collision)
        records[relative] = match.group("digest")
        previous = ordering
        if len(records) > _MAX_MEMBER_COUNT:
            raise BackupError("BACKUP_CHECKSUMS_INVALID", "checksum member count exceeds the v1 bound")
    if not records:
        raise BackupError("BACKUP_CHECKSUMS_INVALID", "checksum set is empty")
    return records


def _checksum_bytes(records: Sequence[Mapping[str, Any]]) -> bytes:
    lines = []
    for record in sorted(records, key=lambda item: str(item["path"]).encode("utf-8")):
        relative = _canonical_relative_path(str(record["path"]))
        digest = str(record["sha256"])
        _require_sha256(digest, field_name="memberSha256")
        lines.append(f"{digest.removeprefix('sha256:')}  {relative}\n")
    return "".join(lines).encode("utf-8")


def _enumerate_artifact_files(root: Path) -> set[str]:
    found: set[str] = set()
    normalized: set[str] = set()
    for item in root.rglob("*"):
        if _is_link_or_reparse(item):
            raise BackupError("BACKUP_UNSAFE_MEMBER", "backup contains a link or reparse point")
        relative = item.relative_to(root).as_posix()
        if item.is_dir():
            _require_private_mode(item, directory=True)
            if not _artifact_directory_allowed(relative):
                raise BackupError("BACKUP_MEMBER_UNEXPECTED", "backup contains an unexpected directory")
            continue
        item_stat = item.lstat()
        if not stat.S_ISREG(item_stat.st_mode) or item_stat.st_nlink != 1:
            raise BackupError("BACKUP_UNSAFE_MEMBER", "backup contains a non-regular or hard-linked member")
        _require_private_mode(item, directory=False)
        canonical = _canonical_relative_path(relative)
        collision = unicodedata.normalize("NFC", canonical).casefold()
        if collision in normalized:
            raise BackupError("BACKUP_UNSAFE_MEMBER", "backup paths have a normalization collision")
        normalized.add(collision)
        found.add(canonical)
    return found


def _enumerate_source_files(root: Path) -> tuple[tuple[str, Path, int], ...]:
    result = []
    normalized: set[str] = set()
    for item in sorted(root.rglob("*"), key=lambda path: path.relative_to(root).as_posix().encode("utf-8")):
        if _is_link_or_reparse(item):
            raise BackupError("BACKUP_UNSAFE_SOURCE", "source contains a link or reparse point")
        relative = item.relative_to(root).as_posix()
        if item.is_dir():
            continue
        item_stat = item.lstat()
        if not stat.S_ISREG(item_stat.st_mode) or item_stat.st_nlink != 1:
            raise BackupError("BACKUP_UNSAFE_SOURCE", "source contains a non-regular or hard-linked member")
        canonical = _canonical_relative_path(relative)
        collision = unicodedata.normalize("NFC", canonical).casefold()
        if collision in normalized:
            raise BackupError("BACKUP_UNSAFE_SOURCE", "source paths have a normalization collision")
        normalized.add(collision)
        result.append((canonical, item, stat.S_IMODE(item_stat.st_mode)))
    return tuple(result)


def _safe_source_directory(path: Path) -> Path:
    if _is_link_or_reparse(path) or not path.is_dir():
        raise BackupError("BACKUP_UNSAFE_SOURCE", "filesystem source is not a safe directory")
    return path.resolve()


def _safe_source_file(path: Path) -> Path:
    if _is_link_or_reparse(path) or not path.is_file():
        raise BackupError("BACKUP_UNSAFE_SOURCE", "source member is not a safe regular file")
    item_stat = path.lstat()
    if not stat.S_ISREG(item_stat.st_mode) or item_stat.st_nlink != 1:
        raise BackupError("BACKUP_UNSAFE_SOURCE", "source member must be regular and singly linked")
    return path.resolve()


def _copy_regular_file(source: Path, target: Path) -> None:
    source_stat = source.lstat()
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    source_fd = os.open(source, flags)
    target_fd = -1
    try:
        opened_stat = os.fstat(source_fd)
        if not stat.S_ISREG(opened_stat.st_mode) or opened_stat.st_nlink != 1:
            raise BackupError("BACKUP_UNSAFE_SOURCE", "source member changed during copy")
        if (source_stat.st_dev, source_stat.st_ino) != (opened_stat.st_dev, opened_stat.st_ino):
            raise BackupError("BACKUP_UNSAFE_SOURCE", "source member changed during copy")
        target_fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(source_fd, "rb") as input_stream, os.fdopen(target_fd, "wb") as output_stream:
            source_fd = -1
            target_fd = -1
            for chunk in iter(lambda: input_stream.read(1024 * 1024), b""):
                output_stream.write(chunk)
            output_stream.flush()
            os.fsync(output_stream.fileno())
        after_stat = source.lstat()
        if (
            (source_stat.st_dev, source_stat.st_ino, source_stat.st_size, source_stat.st_mtime_ns)
            != (after_stat.st_dev, after_stat.st_ino, after_stat.st_size, after_stat.st_mtime_ns)
        ):
            raise BackupError("BACKUP_SOURCE_CHANGED", "source member changed during backup")
        os.chmod(target, 0o600)
    finally:
        if source_fd >= 0:
            os.close(source_fd)
        if target_fd >= 0:
            os.close(target_fd)


def _read_regular_file(path: Path, *, maximum: int) -> bytes:
    item_stat = path.lstat()
    if not stat.S_ISREG(item_stat.st_mode) or item_stat.st_nlink != 1 or _is_link_or_reparse(path):
        raise BackupError("BACKUP_UNSAFE_MEMBER", "member is not a safe regular file")
    if item_stat.st_size > maximum:
        raise BackupError("BACKUP_BOUNDS_EXCEEDED", "bounded metadata member is too large")
    with path.open("rb") as stream:
        return stream.read()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _prepare_destination_root(path: Path) -> Path:
    raw = Path(path)
    if raw.exists() and (_is_link_or_reparse(raw) or not raw.is_dir()):
        raise BackupError("BACKUP_DESTINATION_INVALID", "backup destination is not a safe directory")
    raw.mkdir(parents=True, exist_ok=True)
    os.chmod(raw, 0o700)
    return raw.resolve()


def _absolute_without_link(path: Path) -> Path:
    raw = Path(path).expanduser()
    if raw.exists() and _is_link_or_reparse(raw):
        raise BackupError("BACKUP_DESTINATION_INVALID", "backup destination must not be a link")
    return raw.absolute()


def _make_private_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    if _is_link_or_reparse(path) or not path.is_dir():
        raise BackupError("BACKUP_UNSAFE_MEMBER", "backup directory is unsafe")
    os.chmod(path, 0o700)


def _write_private_bytes(path: Path, data: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    os.chmod(path, 0o600)


def _atomic_finalize(staging: Path, final_path: Path) -> None:
    os.replace(staging, final_path)
    try:
        _fsync_directory(final_path.parent)
    except OSError:
        try:
            os.replace(final_path, staging)
        finally:
            raise


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _require_private_mode(path: Path, *, directory: bool) -> None:
    if os.name == "nt":
        return
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        kind = "directory" if directory else "file"
        raise BackupError("BACKUP_PERMISSIONS_INVALID", f"backup {kind} is group/world accessible")


def _is_link_or_reparse(path: Path) -> bool:
    try:
        item_stat = path.lstat()
    except FileNotFoundError:
        return False
    if stat.S_ISLNK(item_stat.st_mode):
        return True
    attributes = getattr(item_stat, "st_file_attributes", 0)
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def _paths_overlap(first: Path, second: Path) -> bool:
    return first == second or first in second.parents or second in first.parents


def _canonical_relative_path(value: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise BackupError("BACKUP_PATH_INVALID", "backup member path is invalid")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise BackupError("BACKUP_PATH_INVALID", "backup member path escapes the artifact")
    canonical = path.as_posix()
    if canonical != value:
        raise BackupError("BACKUP_PATH_INVALID", "backup member path is not canonical POSIX")
    if len(canonical.encode("utf-8")) > 4096 or any(len(part.encode("utf-8")) > 255 for part in path.parts):
        raise BackupError("BACKUP_PATH_INVALID", "backup member path exceeds the v1 bound")
    return canonical


def _validate_artifact_member_path(relative: str) -> None:
    if relative == DATABASE_MEMBER or relative in {
        "secrets/secret-envelope.json",
        "secrets/secret-reference.json",
    }:
        return
    if any(relative.startswith(f"{root}/") for root in _ALLOWED_FILESYSTEM_ROOTS):
        return
    raise BackupError("BACKUP_PATH_INVALID", "backup member is outside the Format v1 allowlist")


def _artifact_directory_allowed(relative: str) -> bool:
    allowed = {"filesystem", "filesystem/plugins", "secrets"}
    for root in _ALLOWED_FILESYSTEM_ROOTS:
        parts = PurePosixPath(root).parts
        allowed.update("/".join(parts[:index]) for index in range(1, len(parts) + 1))
    return relative in allowed or any(relative.startswith(f"{root}/") for root in _ALLOWED_FILESYSTEM_ROOTS)


def _validate_source_relative(logical_root: str, relative: str) -> None:
    parts = tuple(part.casefold() for part in PurePosixPath(relative).parts)
    if any(
        part in _FORBIDDEN_SOURCE_PARTS
        or part.startswith(".env")
        or part.endswith((".lock", ".sock", ".tmp"))
        for part in parts
    ):
        raise BackupError("BACKUP_SOURCE_NOT_ALLOWED", "source contains a contract-excluded member")
    if logical_root == "filesystem/plugins/cas" and not parts:
        raise BackupError("BACKUP_SOURCE_NOT_ALLOWED", "plugin CAS source path is invalid")


def _validate_secret_reference(path: Path) -> None:
    try:
        encoded = _read_regular_file(path, maximum=_MAX_SECRET_REFERENCE_BYTES)
        parsed = _strict_json_object(encoded, code="BACKUP_SECRET_REFERENCE_INVALID")
    except BackupError as error:
        if error.code == "BACKUP_SECRET_REFERENCE_INVALID":
            raise
        raise BackupError("BACKUP_SECRET_REFERENCE_INVALID", "secret reference is invalid") from error
    if canonical_json_bytes(parsed) + b"\n" != encoded:
        raise BackupError("BACKUP_SECRET_REFERENCE_INVALID", "secret reference is not canonical JSON")
    if not isinstance(parsed.get("provider"), str) or not parsed["provider"]:
        raise BackupError("BACKUP_SECRET_REFERENCE_INVALID", "secret reference provider is missing")
    if not isinstance(parsed.get("version"), str) or not parsed["version"]:
        raise BackupError("BACKUP_SECRET_REFERENCE_INVALID", "secret reference version is missing")
    try:
        _validate_non_secret_json(parsed, label="secretReference")
    except BackupError as error:
        raise BackupError("BACKUP_SECRET_REFERENCE_INVALID", "secret reference contains forbidden data") from error


def _validate_non_secret_metadata_file(path: Path) -> None:
    try:
        encoded = _read_regular_file(path, maximum=_MAX_NON_SECRET_METADATA_BYTES)
        parsed = _strict_json_object(encoded, code="BACKUP_SOURCE_NOT_ALLOWED")
        if canonical_json_bytes(parsed) + b"\n" != encoded:
            raise BackupError(
                "BACKUP_SOURCE_NOT_ALLOWED", "metadata source is not canonical JSON"
            )
        _validate_non_secret_json(parsed, label="filesystemMetadata")
    except BackupError as error:
        if error.code == "BACKUP_SOURCE_NOT_ALLOWED":
            raise
        raise BackupError(
            "BACKUP_SOURCE_NOT_ALLOWED", "metadata source is not safely classifiable"
        ) from None


def _validate_envelope_binding(encoded: bytes, binding: SecretEnvelopeBinding) -> None:
    from .secret_envelope import ENVELOPE_FORMAT, ENVELOPE_SCHEMA_VERSION, SUITE_ID

    calculated_binding_digest = sha256_identity(
        canonical_json_bytes(binding.artifact_binding_record)
    )
    if binding.artifact_binding_digest != calculated_binding_digest:
        raise BackupError(
            "BACKUP_SECRET_ENVELOPE_INVALID",
            "secret envelope binding record and digest differ",
        )

    parsed = _strict_json_object(encoded, code="BACKUP_SECRET_ENVELOPE_INVALID")
    if canonical_json_bytes(parsed) != encoded:
        raise BackupError("BACKUP_SECRET_ENVELOPE_INVALID", "secret envelope is not canonical JSON")
    required = {
        "aead",
        "binding",
        "ciphertext",
        "ciphertextEncoding",
        "format",
        "kdf",
        "mode",
        "schemaVersion",
        "suiteId",
    }
    if set(parsed) != required:
        raise BackupError("BACKUP_SECRET_ENVELOPE_INVALID", "secret envelope structure is invalid")
    if (
        parsed.get("format") != ENVELOPE_FORMAT
        or parsed.get("schemaVersion") != ENVELOPE_SCHEMA_VERSION
        or parsed.get("suiteId") != SUITE_ID
    ):
        raise BackupError("BACKUP_SECRET_ENVELOPE_INVALID", "secret envelope identity is invalid")
    envelope_binding = parsed.get("binding")
    if envelope_binding != {
        "artifactBindingDigest": calculated_binding_digest,
        "artifactId": binding.artifact_id,
        "artifactType": "backup",
    }:
        raise BackupError("BACKUP_SECRET_ENVELOPE_INVALID", "secret envelope artifact binding is invalid")


def _strict_json_object(encoded: bytes, *, code: str) -> dict[str, Any]:
    def reject_constant(_: str) -> None:
        raise ValueError

    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError
            result[key] = value
        return result

    try:
        parsed = json.loads(
            encoded.decode("utf-8"),
            object_pairs_hook=reject_duplicates,
            parse_constant=reject_constant,
        )
    except (UnicodeError, ValueError, TypeError, json.JSONDecodeError) as error:
        raise BackupError(code, "JSON structure is invalid") from error
    if not isinstance(parsed, dict):
        raise BackupError(code, "JSON structure is invalid")
    return parsed


def _mark_incomplete(staging: Path) -> None:
    try:
        if not staging.is_dir() or (staging / STAGING_STATE_NAME).exists():
            return
        _write_private_bytes(
            staging / STAGING_STATE_NAME,
            canonical_json_bytes(
                {"format": FORMAT, "schemaVersion": SCHEMA_VERSION, "lifecycle": "INCOMPLETE"}
            )
            + b"\n",
        )
    except (BackupError, OSError):
        # Preserve the original stable failure.  The hidden staging prefix still
        # prevents publication even when the filesystem cannot write a marker.
        return


def _source_identity_json(source: BackupSourceIdentity) -> dict[str, Any]:
    return {
        "instanceId": source.instance_id,
        "sourceLocatorDigest": source.source_locator_digest,
        "release": dict(source.release),
        "deploymentContract": dict(source.deployment_contract),
        "databaseContract": dict(source.database_contract),
        "configurationContract": dict(source.configuration_contract),
        "pluginSdkApis": sorted(source.plugin_sdk_apis),
    }


def _validate_non_secret_json(value: Any, *, label: str, path: tuple[str, ...] = ()) -> None:
    try:
        canonical_json_bytes(value)
    except (TypeError, ValueError) as error:
        raise BackupError("BACKUP_METADATA_INVALID", f"{label} is not canonical JSON") from error
    if isinstance(value, Mapping):
        for key, child in value.items():
            if not isinstance(key, str):
                raise BackupError("BACKUP_METADATA_INVALID", f"{label} contains a non-string key")
            folded = re.sub(r"[^a-z0-9]", "", key.casefold())
            if any(fragment in folded for fragment in _SECRET_KEY_FRAGMENTS):
                raise BackupError("BACKUP_SECRET_METADATA", f"{label} contains a forbidden secret-bearing field")
            _validate_non_secret_json(child, label=label, path=(*path, key))
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for index, child in enumerate(value):
            _validate_non_secret_json(child, label=label, path=(*path, str(index)))
    elif isinstance(value, str):
        lowered = value.casefold()
        if (
            lowered.startswith("bearer ")
            or "-----begin private key-----" in lowered
            or re.search(r"://[^/@\s:]+:[^/@\s]+@", value)
            or re.match(r"^[A-Z][A-Z0-9_]+\s*=", value)
        ):
            raise BackupError("BACKUP_SECRET_METADATA", f"{label} contains secret-like data")


def _require_uuid(value: Any, *, field_name: str) -> str:
    try:
        parsed = uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError) as error:
        raise BackupError("BACKUP_IDENTITY_INVALID", f"{field_name} is not a canonical UUID") from error
    canonical = str(parsed)
    if value != canonical:
        raise BackupError("BACKUP_IDENTITY_INVALID", f"{field_name} is not a canonical UUID")
    return canonical


def _require_sha256(value: Any, *, field_name: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise BackupError("BACKUP_IDENTITY_INVALID", f"{field_name} is not a canonical SHA-256 identity")
    return value


def _require_utc(value: datetime, *, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
        raise BackupError("BACKUP_TIME_INVALID", f"{field_name} must be an aware UTC timestamp")
    return value


def _utc_string(value: datetime) -> str:
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_utc(value: Any, *, field_name: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise BackupError("BACKUP_TIME_INVALID", f"{field_name} is not canonical UTC")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as error:
        raise BackupError("BACKUP_TIME_INVALID", f"{field_name} is not canonical UTC") from error
    if _utc_string(parsed) != value:
        raise BackupError("BACKUP_TIME_INVALID", f"{field_name} is not canonical UTC")
    return parsed


def _secret_member_path(mode: str) -> str:
    if mode == "envelope":
        return "secrets/secret-envelope.json"
    if mode == "reference":
        return "secrets/secret-reference.json"
    raise BackupError("BACKUP_SECRET_MODE_INVALID", "secret mode is invalid")


def _secret_profile_identity(secret: SecretSource | None) -> str:
    return _secret_profile_identity_from_mode(secret.mode if secret is not None else "none")


def _secret_profile_identity_from_mode(mode: object) -> str:
    if mode == "envelope":
        from .secret_envelope import ENVELOPE_IDENTITY

        return ENVELOPE_IDENTITY
    if mode == "reference":
        return "animemo.secret-reference"
    if mode == "none":
        return "none"
    raise BackupError("BACKUP_SECRET_MODE_INVALID", "secret mode is invalid")
