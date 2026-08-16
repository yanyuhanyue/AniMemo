"""Canonical-only AniMemo Migration Bundle v1 runtime.

The module creates and verifies migration artifacts and consumes them into an
inactive target staging boundary.  Activation is a separate, pure authorization
step; no API in this module deletes or resumes the source instance.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import math
import os
import re
import shutil
import stat
import tempfile
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from functools import partial
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any, Final, NoReturn, Protocol, cast
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from durability.backup import (
    DATABASE_MEMBER,
    PG_DUMP_ARGUMENTS,
    BackupError,
    PgDumpRunner,
    capture_logical_postgres,
)
from durability.canonical import canonical_json_bytes, sha256_identity
from durability.compatibility import (
    MATRIX_IDENTITY,
    ArtifactIdentity,
    CompatibilityDecision,
    CompatibilityEvaluationError,
    CompatibilityOperation,
    CompatibilityOutcome,
    UpgradeAction,
    evaluate_compatibility,
)
from durability.instance import (
    APP_ROOT,
    DATA_ROOT,
    MANAGED_CONFIG_ROOT,
    InstanceLocator,
    ListenIdentity,
    LocatorError,
    instance_locator_payload,
    parse_instance_locator,
)
from durability.resource_budget import (
    DEFAULT_RESOURCE_BUDGET,
    CopyByteCounter,
    DatabaseExpansionGuard,
    ResourceLimitExceeded,
    ResourceLimitReason,
    bounded_copy,
    preflight_copy_sizes,
)
from durability.secret_envelope import (
    ENVELOPE_FORMAT,
    ENVELOPE_IDENTITY,
    ENVELOPE_PATH,
    ENVELOPE_SCHEMA_VERSION,
    SUITE_ID,
    OneTimeKey,
    OpenedSecretPayload,
    Passphrase,
    SecretEntry,
    SecretEnvelope,
    SecretEnvelopeCorruptError,
    SecretEnvelopeError,
    SecretEnvelopeUnsupportedError,
    create_secret_envelope,
    open_secret_envelope,
)

FORMAT: Final = "animemo-migration-bundle"
FORMAT_VERSION: Final = 1
MANIFEST_NAME: Final = "manifest.json"
CHECKSUMS_NAME: Final = "checksums.sha256"
DATABASE_METADATA_MEMBER: Final = "database.metadata.json"
PLUGIN_MANIFEST_MEMBER: Final = "plugins/manifest.json"
MEDIA_MANIFEST_MEMBER: Final = "media/manifest.json"
CONFIG_MEMBER: Final = "config/non-secret.json"
PRIVATE_MANIFEST_MEMBER: Final = "private/manifest.json"
UPDATER_STATE_MEMBER: Final = "updater/state.json"
STAGING_PREFIX: Final = ".animemo-migration-staging-"

MAX_MANIFEST_BYTES: Final = 4 * 1024 * 1024
MAX_JSON_MEMBER_BYTES: Final = 4 * 1024 * 1024
MAX_CHECKSUM_BYTES: Final = 4 * 1024 * 1024
MAX_MEMBER_COUNT: Final = 10_000
MAX_MEMBER_BYTES: Final = DEFAULT_RESOURCE_BUDGET.maximum_filesystem_member_bytes
MAX_COMPRESSED_MEMBER_BYTES: Final = (
    DEFAULT_RESOURCE_BUDGET.maximum_compressed_member_bytes
)
MAX_DATABASE_UNCOMPRESSED_BYTES: Final = (
    DEFAULT_RESOURCE_BUDGET.maximum_uncompressed_database_bytes
)
MAX_TOTAL_COPIED_BYTES: Final = DEFAULT_RESOURCE_BUDGET.maximum_total_copied_bytes
MAX_COMPRESSION_RATIO: Final = DEFAULT_RESOURCE_BUDGET.maximum_compression_ratio
MAX_IDENTITY_MEMBERS: Final = 20_000
MAX_IDENTITY_DEPTH: Final = 12
MAX_IDENTITY_STRING: Final = 8_192

_SHA256_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z"
)
_CHECKSUM_LINE_RE = re.compile(r"([0-9a-f]{64})  ([^\r\n]+)\Z")
_CORE_MEMBERS = frozenset(
    {
        DATABASE_MEMBER,
        DATABASE_METADATA_MEMBER,
        PLUGIN_MANIFEST_MEMBER,
        MEDIA_MANIFEST_MEMBER,
        CONFIG_MEMBER,
        ENVELOPE_PATH,
        PRIVATE_MANIFEST_MEMBER,
        UPDATER_STATE_MEMBER,
    }
)
_MANAGED_PREFIXES = ("plugins/cas/", "media/local/")
_SENSITIVE_KEY_PARTS = (
    "authorization",
    "cookie",
    "credential",
    "encryptionkey",
    "password",
    "privatekey",
    "secret",
    "setupcode",
    "token",
)
_TARGET_LOCAL_DISPOSITIONS: Final = {
    "appRoot": "TARGET-LOCAL",
    "dataRoot": "TARGET-LOCAL",
    "managedConfigPath": "TARGET-LOCAL",
    "configRevision": "TARGET-LOCAL",
    "databaseHost": "TARGET-LOCAL",
    "databaseCredential": "TARGET-LOCAL",
    "redisHost": "TARGET-LOCAL",
    "redisCredential": "TARGET-LOCAL",
}
_PRIVATE_STATE_FIELDS: Final = frozenset(
    {
        "schemaVersion",
        "instanceLifecycle",
        "allowlistedEntries",
        "unknownFilesCopied",
        "mergeHistoryReferences",
    }
)
_UPDATER_STATE_FIELDS: Final = frozenset(
    {
        "schemaVersion",
        "generation",
        "operationState",
        "current",
        "previousHistory",
        "completedOperations",
        "pendingOperation",
        "manualRecoveryRequired",
    }
)


class MigrationError(RuntimeError):
    """Stable, non-secret migration failure."""

    compatibility_outcome: str | None = None

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class MigrationUnsupportedError(MigrationError):
    compatibility_outcome = "UNSUPPORTED"


class MigrationCorruptError(MigrationError):
    compatibility_outcome = "CORRUPT"


class MigrationOperationalError(MigrationError):
    """Operational failure for which no compatibility decision is valid."""


class MigrationRecoveryRequiredError(MigrationOperationalError):
    """Target has crossed the database boundary and requires explicit recovery."""

    def __init__(self, evidence: MigrationRecoveryEvidence):
        self.evidence = evidence
        super().__init__("MIGRATION_TARGET_RECOVERY_REQUIRED")


class MigrationRecoveryEvidenceError(MigrationOperationalError):
    """Durable recovery recording failed; redacted evidence remains available."""

    def __init__(self, evidence: MigrationRecoveryEvidence):
        self.evidence = evidence
        super().__init__("MIGRATION_RECOVERY_EVIDENCE_FAILED")


class ConfigurationMode(str, Enum):
    PRESERVE = "PRESERVE"
    RECONFIGURE = "RECONFIGURE"
    TARGET_LOCAL = "TARGET-LOCAL"


@dataclass(frozen=True)
class SourceConsistencySnapshot:
    generation: str
    config_generation: str
    quiesced: bool
    writes_blocked: bool
    updater_idle: bool
    database_migration_idle: bool
    plugin_operations_idle: bool
    media_writes_idle: bool


class SourceConsistencyProbe(Protocol):
    def snapshot(self) -> SourceConsistencySnapshot: ...


@dataclass(frozen=True)
class MigrationConfiguration:
    mode: ConfigurationMode
    non_secret: Mapping[str, object]
    dispositions: Mapping[str, str]
    target_public_origin: str | None = None
    target_listen: ListenIdentity | None = None


@dataclass(frozen=True)
class PluginPackage:
    project_id: str
    version_id: str
    deployment_id: str
    digest: str
    source: Path
    sdk_apis: tuple[str, ...]
    manifest_snapshot_digest: str


@dataclass(frozen=True)
class LocalMediaObject:
    media_id: str
    object_key: str
    digest: str
    size_bytes: int
    source: Path
    memory_references: tuple[str, ...]


@dataclass(frozen=True)
class R2PhysicalIdentity:
    endpoint: str
    account_identity: str
    bucket: str

    def canonical(self) -> dict[str, str]:
        return _r2_identity_json(self)


@dataclass(frozen=True)
class R2MediaObject:
    media_id: str
    backend_id: str
    object_key: str
    digest: str
    size_bytes: int
    source_identity: R2PhysicalIdentity
    memory_references: tuple[str, ...]


@dataclass(frozen=True)
class PluginDatabaseReference:
    project_id: str
    version_id: str
    deployment_id: str
    digest: str
    sdk_apis: tuple[str, ...]
    manifest_snapshot_digest: str


@dataclass(frozen=True)
class LocalMediaDatabaseReference:
    media_id: str
    object_key: str
    digest: str
    size_bytes: int
    memory_references: tuple[str, ...]


@dataclass(frozen=True)
class R2MediaDatabaseReference:
    media_id: str
    backend_id: str
    object_key: str
    digest: str
    size_bytes: int
    source_identity: R2PhysicalIdentity
    memory_references: tuple[str, ...]


@dataclass(frozen=True)
class DatabaseReferenceInventory:
    generation: str
    plugin_packages: tuple[PluginDatabaseReference, ...]
    local_media: tuple[LocalMediaDatabaseReference, ...]
    r2_media: tuple[R2MediaDatabaseReference, ...]


class DatabaseReferenceProbe(Protocol):
    def capture(self) -> DatabaseReferenceInventory: ...


@dataclass(frozen=True, repr=False)
class MigrationBundleRequest:
    destination_root: Path
    source_locator: InstanceLocator
    source_probe: SourceConsistencyProbe
    database_url: str
    database_server_major: int
    database_reference_probe: DatabaseReferenceProbe
    deployment_contract: Mapping[str, object]
    database_contract: Mapping[str, object]
    configuration_contract: Mapping[str, object]
    configuration: MigrationConfiguration
    private_state: Mapping[str, object]
    updater_state: Mapping[str, object]
    external_secret: Passphrase | OneTimeKey
    secret_entries: tuple[SecretEntry, ...]
    plugins: tuple[PluginPackage, ...] = ()
    local_media: tuple[LocalMediaObject, ...] = ()
    r2_media: tuple[R2MediaObject, ...] = ()
    target_r2_identities: Mapping[str, R2PhysicalIdentity] = MappingProxyType({})
    pg_dump_executable: str = "pg_dump"
    pg_dump_timeout: int = 600

    def __repr__(self) -> str:
        return "<MigrationBundleRequest redacted>"


@dataclass(frozen=True)
class MigrationBundleResult:
    path: Path
    bundle_id: str
    instance_id: str
    manifest_digest: str
    artifact_binding_digest: str
    state: str = "READY_FOR_HANDOFF"


@dataclass(frozen=True)
class MigrationVerification:
    path: Path
    bundle_id: str
    instance_id: str
    manifest_digest: str
    artifact_binding_digest: str
    source_consistency_generation: str
    configuration_mode: ConfigurationMode
    manifest: Mapping[str, object]

    def artifact_identity(self) -> ArtifactIdentity:
        return ArtifactIdentity(
            format_identity=FORMAT,
            format_version=FORMAT_VERSION,
            artifact_id=self.bundle_id,
            manifest_digest=self.manifest_digest,
        )


@dataclass(frozen=True)
class TargetInspection:
    canonical_roots: bool
    empty_owned_target: bool
    active_instance_id: str | None
    release_identity: Mapping[str, object] | None
    deployment_contract: Mapping[str, object] | None
    updater_current: Mapping[str, object] | None
    target_r2_identities: Mapping[str, R2PhysicalIdentity]
    supported_plugin_sdk_apis: frozenset[str]


class MigrationTargetWriter(Protocol):
    def inspect(self) -> TargetInspection: ...

    def begin(self, *, bundle_id: str, instance_id: str) -> None: ...

    def stage_database(self, path: Path, metadata: Mapping[str, object]) -> None: ...

    def stage_plugin_package(
        self, path: Path, metadata: Mapping[str, object]
    ) -> None: ...

    def stage_local_media(self, path: Path, metadata: Mapping[str, object]) -> None: ...

    def stage_configuration(
        self,
        configuration: Mapping[str, object],
        secrets: OpenedSecretPayload,
    ) -> None: ...

    def stage_private_state(self, state: Mapping[str, object]) -> None: ...

    def stage_updater_state(self, state: Mapping[str, object]) -> None: ...

    def apply_upgrade(self, actions: tuple[UpgradeAction, ...]) -> None: ...

    def validate_inactive(self, *, bundle_id: str, instance_id: str) -> bool: ...

    def publish_inactive(self, *, bundle_id: str, instance_id: str) -> None: ...

    def rollback(self, *, bundle_id: str) -> None: ...

    def record_recovery_required(self, evidence: MigrationRecoveryEvidence) -> None: ...


@dataclass(frozen=True)
class MigrationRecoveryEvidence:
    bundle_id: str
    instance_id: str
    completed_steps: tuple[str, ...]
    failed_step: str
    error_code: str
    state: str = "RECOVERY_REQUIRED"
    target_active: bool = False
    source_deleted: bool = False
    automatic_rollback: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "state": self.state,
            "bundleId": self.bundle_id,
            "instanceId": self.instance_id,
            "completedSteps": list(self.completed_steps),
            "failedStep": self.failed_step,
            "errorCode": self.error_code,
            "targetActive": self.target_active,
            "sourceDeleted": self.source_deleted,
            "automaticRollback": self.automatic_rollback,
        }


@dataclass(frozen=True)
class ConsumedMigration:
    bundle_id: str
    instance_id: str
    source_consistency_generation: str
    configuration_mode: ConfigurationMode
    state: str = "READY_FOR_HANDOFF"
    target_active: bool = False
    source_deleted: bool = False


@dataclass(frozen=True)
class ActivationHandoff:
    bundle_id: str
    instance_id: str
    source_consistency_generation: str
    source_quiesced: bool
    source_writes_blocked: bool
    source_ownership_released: bool
    target_inactive: bool
    target_local_health_passed: bool
    administrator_confirmed: bool


@dataclass(frozen=True)
class ActivationPermit:
    bundle_id: str
    instance_id: str
    source_consistency_generation: str
    target_may_activate: bool = True
    source_may_resume: bool = False
    source_may_be_deleted: bool = False


def create_migration_bundle(
    request: MigrationBundleRequest,
    *,
    pg_dump_runner: PgDumpRunner | None = None,
    bundle_id: UUID | None = None,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> MigrationBundleResult:
    """Create one verified bundle while leaving the source quiesced."""

    configuration = _validate_request(request)
    private_state = _validate_private_state(request.private_state, producer=True)
    updater_state = _validate_updater_state(
        request.updater_state,
        request.source_locator.release_identity,
        producer=True,
    )
    initial = _source_snapshot(request.source_probe)
    identifier = bundle_id or uuid4()
    bundle_text = str(identifier)
    if not _is_uuid(bundle_text):
        raise MigrationOperationalError("MIGRATION_BUNDLE_ID_INVALID")
    created_at = _canonical_time(clock())
    destination = _prepare_destination_root(request.destination_root)
    final_path = destination / bundle_text
    if final_path.exists() or final_path.is_symlink():
        raise MigrationOperationalError("MIGRATION_DESTINATION_CONFLICT")
    staging = destination / f"{STAGING_PREFIX}{bundle_text}-{uuid4()}"
    try:
        staging.mkdir(mode=0o700)
        if os.name != "nt":
            os.chmod(staging, 0o700)
        try:
            database = capture_logical_postgres(
                request.database_url,
                staging,
                server_major=request.database_server_major,
                runner=pg_dump_runner,
                executable=request.pg_dump_executable,
                timeout=request.pg_dump_timeout,
            )
        except BackupError as error:
            if error.code == "BACKUP_RESOURCE_BOUNDS_EXCEEDED":
                raise MigrationOperationalError(
                    "MIGRATION_RESOURCE_BOUNDS_EXCEEDED"
                ) from None
            raise MigrationOperationalError(
                "MIGRATION_DATABASE_CAPTURE_FAILED"
            ) from None

        copy_counter = CopyByteCounter(MAX_TOTAL_COPIED_BYTES)
        try:
            copy_counter.consume(database["compressedBytes"])
        except ResourceLimitExceeded:
            raise MigrationOperationalError(
                "MIGRATION_RESOURCE_BOUNDS_EXCEEDED"
            ) from None

        database_metadata = {
            key: value for key, value in database.items() if key != "member"
        }
        _write_json(staging / DATABASE_METADATA_MEMBER, database_metadata)
        database_references = _capture_database_references(
            request.database_reference_probe
        )
        plugin_manifest = _capture_plugins(
            staging,
            request.plugins,
            copy_counter=copy_counter,
        )
        _write_json(staging / PLUGIN_MANIFEST_MEMBER, plugin_manifest)
        media_manifest = _capture_media(
            staging,
            request.local_media,
            request.r2_media,
            request.target_r2_identities,
            copy_counter=copy_counter,
        )
        _cross_check_database_references(
            database_references,
            plugin_manifest,
            media_manifest,
        )
        _write_json(staging / MEDIA_MANIFEST_MEMBER, media_manifest)
        _write_json(staging / CONFIG_MEMBER, configuration)
        _write_json(
            staging / PRIVATE_MANIFEST_MEMBER,
            private_state,
        )
        _write_json(
            staging / UPDATER_STATE_MEMBER,
            updater_state,
        )

        payload_records = _payload_records(staging)
        source = _source_json(request.source_locator)
        binding_record = {
            "format": FORMAT,
            "formatVersion": FORMAT_VERSION,
            "bundleId": bundle_text,
            "instanceId": request.source_locator.instance_id,
            "createdAt": created_at,
            "source": source,
            "deploymentContract": _safe_non_secret(request.deployment_contract),
            "databaseContract": _safe_non_secret(request.database_contract),
            "configurationContract": _safe_non_secret(request.configuration_contract),
            "database": database_metadata,
            "databaseReferences": database_references,
            "payloadMembers": payload_records,
            "plugins": plugin_manifest,
            "media": media_manifest,
            "configuration": configuration,
            "privateState": private_state,
            "updaterState": updater_state,
            "sourceConsistency": _snapshot_json(initial),
            "secretProfileIdentity": ENVELOPE_IDENTITY,
            "activationProfile": "explicit-source-target-handoff/v1",
        }
        artifact_binding_digest = sha256_identity(canonical_json_bytes(binding_record))
        try:
            envelope = create_secret_envelope(
                external_secret=request.external_secret,
                artifact_type="migration-bundle",
                artifact_id=bundle_text,
                artifact_binding_record=binding_record,
                source_instance_id=request.source_locator.instance_id,
                secret_entries=request.secret_entries,
            )
        except SecretEnvelopeUnsupportedError as error:
            raise MigrationUnsupportedError(error.code) from None
        except SecretEnvelopeError:
            raise MigrationOperationalError(
                "MIGRATION_SECRET_ENVELOPE_FAILED"
            ) from None
        envelope_bytes = envelope.to_bytes()
        _write_private_bytes(staging / ENVELOPE_PATH, envelope_bytes)

        records = _payload_records(staging)
        checksum_bytes = _checksum_bytes(records)
        _write_private_bytes(staging / CHECKSUMS_NAME, checksum_bytes)
        envelope_header = _strict_json_object(
            envelope_bytes,
            maximum=MAX_JSON_MEMBER_BYTES,
            code="MIGRATION_ENVELOPE_STRUCTURE_CORRUPT",
        )
        manifest = {
            "format": FORMAT,
            "formatVersion": FORMAT_VERSION,
            "bundleId": bundle_text,
            "createdAt": created_at,
            "instanceId": request.source_locator.instance_id,
            "lifecycle": "FINALIZED",
            "source": source,
            "deploymentContract": _safe_non_secret(request.deployment_contract),
            "databaseContract": _safe_non_secret(request.database_contract),
            "configurationContract": _safe_non_secret(request.configuration_contract),
            "database": database_metadata,
            "databaseReferences": database_references,
            "configuration": configuration,
            "plugins": plugin_manifest,
            "media": media_manifest,
            "privateState": private_state,
            "updaterState": updater_state,
            "sourceConsistency": _snapshot_json(initial),
            "artifactBindingRecord": binding_record,
            "artifactBindingDigest": artifact_binding_digest,
            "secretEnvelope": {
                "format": envelope_header["format"],
                "schemaVersion": envelope_header["schemaVersion"],
                "mode": envelope_header["mode"],
                "suiteId": envelope_header["suiteId"],
                "binding": envelope_header["binding"],
                "path": ENVELOPE_PATH,
                "sha256": sha256_identity(envelope_bytes),
                "sizeBytes": len(envelope_bytes),
                "requiredSecretProfileSatisfied": True,
            },
            "members": records,
            "checksumSetDigest": sha256_identity(checksum_bytes),
            "compatibility": {
                "matrixVersion": MATRIX_IDENTITY,
                "requiredOutcome": "COMPATIBLE",
            },
            "activation": {
                "state": "READY_FOR_HANDOFF",
                "sourceOwnership": "QUIESCED",
                "targetOwnership": "INACTIVE",
                "sourceDeletion": "NEVER_AUTOMATIC",
            },
        }
        _write_json(staging / MANIFEST_NAME, manifest)
        verification = _verify_bundle(staging, staging_allowed=True)
        final_snapshot = _source_snapshot(request.source_probe)
        if final_snapshot != initial:
            raise MigrationOperationalError("MIGRATION_SOURCE_CHANGED")
        _fsync_directory(staging)
        os.replace(staging, final_path)
        _fsync_directory(destination)
        return MigrationBundleResult(
            path=final_path,
            bundle_id=bundle_text,
            instance_id=request.source_locator.instance_id,
            manifest_digest=verification.manifest_digest,
            artifact_binding_digest=artifact_binding_digest,
        )
    except MigrationError:
        _cleanup_owned_staging(destination, staging)
        raise
    except Exception:  # noqa: BLE001 - all operational details stay redacted
        _cleanup_owned_staging(destination, staging)
        raise MigrationOperationalError("MIGRATION_CREATE_FAILED") from None


def verify_migration_bundle(path: Path) -> MigrationVerification:
    """Verify a finalized bundle without external secret or target mutation."""

    try:
        return _verify_bundle(Path(path), staging_allowed=False)
    except MigrationError:
        raise
    except Exception:  # noqa: BLE001 - host I/O details never become compatibility
        raise MigrationOperationalError("MIGRATION_BUNDLE_UNAVAILABLE") from None


def consume_migration_bundle(
    path: Path,
    *,
    external_secret: Passphrase | OneTimeKey,
    compatibility: CompatibilityDecision,
    target: MigrationTargetWriter,
    approved_upgrade_actions: Sequence[UpgradeAction] = (),
) -> ConsumedMigration:
    """Consume one private snapshot into an inactive target staging boundary."""

    source = Path(path).absolute()
    try:
        with tempfile.TemporaryDirectory(
            prefix="animemo-migration-consume-",
            ignore_cleanup_errors=True,
        ) as workspace_text:
            workspace = Path(workspace_text)
            os.chmod(workspace, 0o700)
            snapshot = workspace / source.name
            _copy_finalized_bundle_snapshot(source, snapshot)
            return _consume_migration_snapshot(
                snapshot,
                external_secret=external_secret,
                compatibility=compatibility,
                target=target,
                approved_upgrade_actions=approved_upgrade_actions,
            )
    except MigrationError:
        raise
    except Exception:  # noqa: BLE001 - snapshot host details remain redacted
        raise MigrationOperationalError("MIGRATION_SNAPSHOT_FAILED") from None


def _consume_migration_snapshot(
    snapshot: Path,
    *,
    external_secret: Passphrase | OneTimeKey,
    compatibility: CompatibilityDecision,
    target: MigrationTargetWriter,
    approved_upgrade_actions: Sequence[UpgradeAction],
) -> ConsumedMigration:
    verification = verify_migration_bundle(snapshot)
    upgrade_actions = _require_compatible(
        verification, compatibility, approved_upgrade_actions
    )

    manifest = verification.manifest
    try:
        inspection = target.inspect()
    except Exception:  # noqa: BLE001 - target callback errors are redacted
        raise MigrationOperationalError("MIGRATION_TARGET_INSPECTION_FAILED") from None
    _validate_target_inspection(
        verification, manifest, inspection, upgrade_actions=upgrade_actions
    )

    try:
        envelope_bytes = _read_regular_file(
            verification.path / ENVELOPE_PATH, maximum=MAX_JSON_MEMBER_BYTES
        )
        secrets = open_secret_envelope(
            SecretEnvelope.from_bytes(envelope_bytes),
            external_secret=external_secret,
            expected_artifact_type="migration-bundle",
            expected_artifact_id=verification.bundle_id,
            expected_artifact_binding_record=cast(
                Mapping[str, object], manifest["artifactBindingRecord"]
            ),
            expected_source_instance_id=verification.instance_id,
        )
    except SecretEnvelopeUnsupportedError as error:
        raise MigrationUnsupportedError(error.code) from None
    except SecretEnvelopeCorruptError as error:
        raise MigrationCorruptError(error.code) from None
    except SecretEnvelopeError as error:
        raise MigrationOperationalError(error.code) from None

    plugin_manifest = _read_json_member(verification.path, PLUGIN_MANIFEST_MEMBER)
    media_manifest = _read_json_member(verification.path, MEDIA_MANIFEST_MEMBER)
    configuration = _read_json_member(verification.path, CONFIG_MEMBER)
    private_state = _read_json_member(verification.path, PRIVATE_MANIFEST_MEMBER)
    updater_state = _read_json_member(verification.path, UPDATER_STATE_MEMBER)
    database_metadata = _read_json_member(verification.path, DATABASE_METADATA_MEMBER)

    completed: list[str] = []
    failed_step = "begin"
    database_boundary_crossed = False
    try:
        target.begin(
            bundle_id=verification.bundle_id, instance_id=verification.instance_id
        )
        completed.append("begin")
        failed_step = "database"
        database_boundary_crossed = True
        target.stage_database(verification.path / DATABASE_MEMBER, database_metadata)
        completed.append("database")
        for package in cast(
            Sequence[Mapping[str, object]], plugin_manifest["packages"]
        ):
            failed_step = "plugin"
            target.stage_plugin_package(
                verification.path / cast(str, package["casPath"]), package
            )
        if plugin_manifest["packages"]:
            completed.append("plugin")
        for item in cast(Sequence[Mapping[str, object]], media_manifest["local"]):
            failed_step = "media"
            target.stage_local_media(
                verification.path / cast(str, item["memberPath"]), item
            )
        if media_manifest["local"]:
            completed.append("media")
        failed_step = "configuration"
        target.stage_configuration(configuration, secrets)
        completed.append("configuration")
        failed_step = "private"
        target.stage_private_state(private_state)
        completed.append("private")
        failed_step = "updater"
        target.stage_updater_state(updater_state)
        completed.append("updater")
        if upgrade_actions:
            failed_step = "upgrade"
            target.apply_upgrade(upgrade_actions)
            completed.append("upgrade")
        failed_step = "validate"
        if not target.validate_inactive(
            bundle_id=verification.bundle_id,
            instance_id=verification.instance_id,
        ):
            raise MigrationOperationalError("MIGRATION_TARGET_VALIDATION_FAILED")
        completed.append("validate")
        failed_step = "publish"
        target.publish_inactive(
            bundle_id=verification.bundle_id,
            instance_id=verification.instance_id,
        )
        completed.append("publish-inactive")
    except Exception as error:  # noqa: BLE001 - target failures become recovery evidence
        if not database_boundary_crossed:
            try:
                target.rollback(bundle_id=verification.bundle_id)
            except Exception:  # noqa: BLE001 - target callback errors are redacted
                evidence = MigrationRecoveryEvidence(
                    bundle_id=verification.bundle_id,
                    instance_id=verification.instance_id,
                    completed_steps=tuple(completed),
                    failed_step="rollback",
                    error_code="MIGRATION_TARGET_ROLLBACK_FAILED",
                )
                try:
                    target.record_recovery_required(evidence)
                except Exception:  # noqa: BLE001 - evidence remains on exception
                    raise MigrationRecoveryEvidenceError(evidence) from None
                raise MigrationRecoveryRequiredError(evidence) from None
            raise MigrationOperationalError("MIGRATION_TARGET_BEGIN_FAILED") from None
        error_code = (
            "MIGRATION_TARGET_VALIDATION_FAILED"
            if failed_step == "validate"
            and isinstance(error, MigrationOperationalError)
            and error.code == "MIGRATION_TARGET_VALIDATION_FAILED"
            else f"MIGRATION_TARGET_{failed_step.upper()}_FAILED"
        )
        evidence = MigrationRecoveryEvidence(
            bundle_id=verification.bundle_id,
            instance_id=verification.instance_id,
            completed_steps=tuple(completed),
            failed_step=failed_step,
            error_code=error_code,
        )
        try:
            target.record_recovery_required(evidence)
        except Exception:  # noqa: BLE001 - evidence adapter details are redacted
            raise MigrationRecoveryEvidenceError(evidence) from None
        raise MigrationRecoveryRequiredError(evidence) from None

    return ConsumedMigration(
        bundle_id=verification.bundle_id,
        instance_id=verification.instance_id,
        source_consistency_generation=verification.source_consistency_generation,
        configuration_mode=verification.configuration_mode,
    )


def authorize_activation(
    consumed: ConsumedMigration, handoff: ActivationHandoff
) -> ActivationPermit:
    """Authorize, but never perform, the source-to-target ownership handoff."""

    if not isinstance(consumed, ConsumedMigration) or not isinstance(
        handoff, ActivationHandoff
    ):
        raise MigrationOperationalError("MIGRATION_HANDOFF_NOT_AUTHORIZED")
    if consumed.configuration_mode is ConfigurationMode.TARGET_LOCAL:
        raise MigrationOperationalError("MIGRATION_TARGET_LOCAL_NOT_ACTIVATABLE")
    if (
        consumed.state != "READY_FOR_HANDOFF"
        or consumed.target_active
        or consumed.source_deleted
        or handoff.bundle_id != consumed.bundle_id
        or handoff.instance_id != consumed.instance_id
        or handoff.source_consistency_generation
        != consumed.source_consistency_generation
        or not handoff.source_quiesced
        or not handoff.source_writes_blocked
        or not handoff.source_ownership_released
        or not handoff.target_inactive
        or not handoff.target_local_health_passed
        or not handoff.administrator_confirmed
    ):
        raise MigrationOperationalError("MIGRATION_HANDOFF_NOT_AUTHORIZED")
    return ActivationPermit(
        bundle_id=consumed.bundle_id,
        instance_id=consumed.instance_id,
        source_consistency_generation=consumed.source_consistency_generation,
    )


def _validate_request(request: MigrationBundleRequest) -> dict[str, object]:
    if not isinstance(request, MigrationBundleRequest):
        raise MigrationOperationalError("MIGRATION_REQUEST_INVALID")
    _source_json(request.source_locator)
    if not isinstance(request.database_url, str) or not request.database_url:
        raise MigrationOperationalError("MIGRATION_DATABASE_SOURCE_INVALID")
    if (
        isinstance(request.database_server_major, bool)
        or not isinstance(request.database_server_major, int)
        or request.database_server_major <= 0
        or isinstance(request.pg_dump_timeout, bool)
        or not isinstance(request.pg_dump_timeout, int)
        or request.pg_dump_timeout <= 0
        or not isinstance(request.pg_dump_executable, str)
        or not request.pg_dump_executable
        or "\x00" in request.pg_dump_executable
    ):
        raise MigrationOperationalError("MIGRATION_DATABASE_SOURCE_INVALID")
    for value in (
        request.deployment_contract,
        request.database_contract,
        request.configuration_contract,
    ):
        _safe_non_secret(value)
        _reject_unknown_extensions(value)
    if not all(
        isinstance(value, Mapping) and value
        for value in (
            request.deployment_contract,
            request.database_contract,
            request.configuration_contract,
        )
    ):
        raise MigrationOperationalError("MIGRATION_CONTRACT_IDENTITY_INVALID")
    if (
        not isinstance(request.plugins, tuple)
        or not isinstance(request.local_media, tuple)
        or not isinstance(request.r2_media, tuple)
    ):
        raise MigrationOperationalError("MIGRATION_REQUEST_INVALID")
    if not isinstance(request.target_r2_identities, Mapping):
        raise MigrationUnsupportedError("MIGRATION_R2_IDENTITY_INDETERMINATE")
    if request.database_reference_probe is None:
        raise MigrationOperationalError("MIGRATION_DATABASE_REFERENCE_PROBE_MISSING")
    destination = Path(request.destination_root).expanduser().absolute()
    for item in (*request.plugins, *request.local_media):
        if isinstance(item, (PluginPackage, LocalMediaObject)) and _paths_overlap(
            destination, Path(item.source).expanduser().absolute()
        ):
            raise MigrationOperationalError("MIGRATION_DESTINATION_SOURCE_OVERLAP")
    return _configuration_json(request.source_locator, request.configuration)


def _validate_private_state(value: object, *, producer: bool) -> dict[str, object]:
    if not isinstance(value, Mapping):
        _state_invalid("MIGRATION_PRIVATE_STATE_INVALID", producer)
    fields = set(value)
    if fields - _PRIVATE_STATE_FIELDS:
        raise MigrationUnsupportedError("MIGRATION_PRIVATE_STATE_EXTENSION_UNSUPPORTED")
    if fields != _PRIVATE_STATE_FIELDS:
        _state_invalid("MIGRATION_PRIVATE_STATE_INVALID", producer)
    try:
        normalized = _safe_non_secret(value)
    except MigrationError:
        _state_invalid("MIGRATION_PRIVATE_STATE_INVALID", producer)
    assert isinstance(normalized, dict)
    if (
        normalized.get("schemaVersion") != 1
        or normalized.get("instanceLifecycle") not in {"INITIALIZED", "UNINITIALIZED"}
        or normalized.get("unknownFilesCopied") is not False
    ):
        _state_invalid("MIGRATION_PRIVATE_STATE_INVALID", producer)
    try:
        normalized["allowlistedEntries"] = _sorted_unique_strings(
            cast(Sequence[object], normalized["allowlistedEntries"]),
            code="MIGRATION_PRIVATE_STATE_INVALID",
            require_canonical=True,
        )
        normalized["mergeHistoryReferences"] = _ordered_unique_strings(
            cast(Sequence[object], normalized["mergeHistoryReferences"]),
            code="MIGRATION_PRIVATE_STATE_INVALID",
        )
    except (KeyError, MigrationError):
        _state_invalid("MIGRATION_PRIVATE_STATE_INVALID", producer)
    return normalized


def _validate_updater_state(
    value: object,
    release_identity: Mapping[str, object],
    *,
    producer: bool,
) -> dict[str, object]:
    if not isinstance(value, Mapping):
        _state_invalid("MIGRATION_UPDATER_STATE_INVALID", producer)
    fields = set(value)
    if fields - _UPDATER_STATE_FIELDS:
        raise MigrationUnsupportedError("MIGRATION_UPDATER_STATE_EXTENSION_UNSUPPORTED")
    if fields != _UPDATER_STATE_FIELDS:
        _state_invalid("MIGRATION_UPDATER_STATE_INVALID", producer)
    try:
        normalized = _safe_non_secret(value)
        release = _safe_non_secret(release_identity)
    except MigrationError:
        _state_invalid("MIGRATION_UPDATER_STATE_INVALID", producer)
    assert isinstance(normalized, dict)
    if (
        normalized.get("schemaVersion") != 1
        or normalized.get("operationState") != "IDLE"
        or normalized.get("current") != release
        or normalized.get("pendingOperation") is not None
        or normalized.get("manualRecoveryRequired") is not False
        or not isinstance(normalized.get("previousHistory"), list)
        or not isinstance(normalized.get("completedOperations"), list)
    ):
        _state_invalid("MIGRATION_UPDATER_STATE_INVALID", producer)
    generation = normalized.get("generation")
    if (
        not isinstance(generation, str)
        or not generation
        or len(generation.encode("utf-8")) > 256
        or _string_is_secret(generation)
    ):
        _state_invalid("MIGRATION_UPDATER_STATE_INVALID", producer)
    if any(
        not isinstance(item, Mapping)
        for item in (
            *cast(list[object], normalized["previousHistory"]),
            *cast(list[object], normalized["completedOperations"]),
        )
    ):
        _state_invalid("MIGRATION_UPDATER_STATE_INVALID", producer)
    _validate_updater_history(
        cast(list[object], normalized["previousHistory"]),
        cast(list[object], normalized["completedOperations"]),
        cast(Mapping[str, object], release),
        producer=producer,
    )
    return normalized


def _validate_updater_history(
    previous_history: Sequence[object],
    completed_operations: Sequence[object],
    current_release: Mapping[str, object],
    *,
    producer: bool,
) -> None:
    previous_evidence: set[str] = set()
    for item in previous_history:
        if not isinstance(item, Mapping) or set(item) != {
            "releaseIdentity",
            "evidenceDigest",
        }:
            _state_invalid("MIGRATION_UPDATER_STATE_INVALID", producer)
        release = item.get("releaseIdentity")
        evidence = item.get("evidenceDigest")
        try:
            if (
                not isinstance(release, Mapping)
                or set(release) != set(current_release)
                or _safe_non_secret(release) != release
            ):
                _state_invalid("MIGRATION_UPDATER_STATE_INVALID", producer)
            digest = _require_sha256(evidence, "MIGRATION_UPDATER_STATE_INVALID")
        except MigrationError:
            _state_invalid("MIGRATION_UPDATER_STATE_INVALID", producer)
        if digest in previous_evidence:
            _state_invalid("MIGRATION_UPDATER_STATE_INVALID", producer)
        previous_evidence.add(digest)

    operation_ids: set[str] = set()
    for item in completed_operations:
        if not isinstance(item, Mapping) or set(item) != {
            "operationId",
            "operationType",
            "completedAt",
            "evidenceDigest",
        }:
            _state_invalid("MIGRATION_UPDATER_STATE_INVALID", producer)
        operation_id = item.get("operationId")
        if (
            not _is_uuid(operation_id)
            or operation_id in operation_ids
            or item.get("operationType") not in {"BOOTSTRAP", "ROLLBACK", "UPDATE"}
        ):
            _state_invalid("MIGRATION_UPDATER_STATE_INVALID", producer)
        try:
            _parse_canonical_time(item.get("completedAt"))
            _require_sha256(
                item.get("evidenceDigest"), "MIGRATION_UPDATER_STATE_INVALID"
            )
        except MigrationError:
            _state_invalid("MIGRATION_UPDATER_STATE_INVALID", producer)
        operation_ids.add(cast(str, operation_id))


def _state_invalid(code: str, producer: bool) -> NoReturn:
    if producer:
        raise MigrationOperationalError(code)
    raise MigrationCorruptError(code)


def _configuration_json(
    locator: InstanceLocator, configuration: MigrationConfiguration
) -> dict[str, object]:
    if not isinstance(configuration, MigrationConfiguration) or not isinstance(
        configuration.mode, ConfigurationMode
    ):
        raise MigrationOperationalError("MIGRATION_CONFIGURATION_MODE_INVALID")
    non_secret = _safe_non_secret(configuration.non_secret)
    if not isinstance(non_secret, dict):
        raise MigrationOperationalError("MIGRATION_CONFIGURATION_INVALID")
    dispositions: dict[str, str] = {}
    if not isinstance(configuration.dispositions, Mapping):
        raise MigrationOperationalError("MIGRATION_CONFIGURATION_INVALID")
    for key, value in configuration.dispositions.items():
        if (
            not isinstance(key, str)
            or not key
            or len(key.encode("utf-8")) > 256
            or value not in {item.value for item in ConfigurationMode}
        ):
            raise MigrationOperationalError("MIGRATION_CONFIGURATION_INVALID")
        dispositions[key] = value

    source_origin = _canonical_public_origin(locator.public_origin)
    source_listen = _listen_json(locator.listen)
    if "publicOrigin" in non_secret and non_secret["publicOrigin"] != source_origin:
        raise MigrationOperationalError("MIGRATION_CONFIGURATION_SOURCE_MISMATCH")
    if "listen" in non_secret and non_secret["listen"] != source_listen:
        raise MigrationOperationalError("MIGRATION_CONFIGURATION_SOURCE_MISMATCH")

    mode = configuration.mode
    target_origin = configuration.target_public_origin
    target_listen = configuration.target_listen
    if mode is ConfigurationMode.PRESERVE:
        if target_origin is not None or target_listen is not None:
            raise MigrationOperationalError("MIGRATION_CONFIGURATION_INVALID")
        effective_origin = source_origin
        effective_listen = source_listen
        expected = {"publicOrigin": "PRESERVE", "listen": "PRESERVE"}
    elif mode is ConfigurationMode.RECONFIGURE:
        effective_origin = (
            source_origin
            if target_origin is None
            else _canonical_public_origin(target_origin)
        )
        effective_listen = (
            source_listen if target_listen is None else _listen_json(target_listen)
        )
        expected = {
            "publicOrigin": "RECONFIGURE" if target_origin is not None else "PRESERVE",
            "listen": "RECONFIGURE" if target_listen is not None else "PRESERVE",
        }
    else:
        if target_origin is not None or target_listen is None:
            raise MigrationOperationalError("MIGRATION_TARGET_LOCAL_INVALID")
        effective_origin = source_origin
        effective_listen = _listen_json(target_listen)
        if not target_listen.is_loopback:
            raise MigrationOperationalError("MIGRATION_TARGET_LOCAL_INVALID")
        expected = {"publicOrigin": "PRESERVE", "listen": "TARGET-LOCAL"}
    required_dispositions = {**_TARGET_LOCAL_DISPOSITIONS, **expected}
    if dispositions != required_dispositions:
        raise MigrationOperationalError("MIGRATION_CONFIGURATION_INVALID")
    return {
        "mode": mode.value,
        "nonSecret": non_secret,
        "dispositions": {
            key: dispositions[key]
            for key in sorted(dispositions, key=lambda item: item.encode("utf-8"))
        },
        "sourcePublicOrigin": source_origin,
        "effectivePublicOrigin": effective_origin,
        "sourceListen": source_listen,
        "effectiveListen": effective_listen,
        "targetHostPaths": {
            "appRoot": str(APP_ROOT),
            "dataRoot": str(DATA_ROOT),
            "managedConfigRoot": str(MANAGED_CONFIG_ROOT),
        },
        "publicEdgeMutation": "OPERATOR_MANAGED",
        "activationAllowed": mode is not ConfigurationMode.TARGET_LOCAL,
    }


def _source_snapshot(probe: SourceConsistencyProbe) -> SourceConsistencySnapshot:
    try:
        snapshot = probe.snapshot()
    except Exception:  # noqa: BLE001 - probe callback errors are redacted
        raise MigrationOperationalError("MIGRATION_SOURCE_PROBE_FAILED") from None
    if not isinstance(snapshot, SourceConsistencySnapshot):
        raise MigrationOperationalError("MIGRATION_SOURCE_STATE_INVALID")
    if any(
        not isinstance(value, bool)
        for value in (
            snapshot.quiesced,
            snapshot.writes_blocked,
            snapshot.updater_idle,
            snapshot.database_migration_idle,
            snapshot.plugin_operations_idle,
            snapshot.media_writes_idle,
        )
    ):
        raise MigrationOperationalError("MIGRATION_SOURCE_STATE_INVALID")
    for generation in (snapshot.generation, snapshot.config_generation):
        if (
            not isinstance(generation, str)
            or not generation
            or len(generation.encode("utf-8")) > 256
            or _string_is_secret(generation)
        ):
            raise MigrationOperationalError("MIGRATION_SOURCE_STATE_INVALID")
    if not snapshot.quiesced or not snapshot.writes_blocked:
        raise MigrationOperationalError("MIGRATION_SOURCE_NOT_QUIESCED")
    if not all(
        (
            snapshot.updater_idle,
            snapshot.database_migration_idle,
            snapshot.plugin_operations_idle,
            snapshot.media_writes_idle,
        )
    ):
        raise MigrationOperationalError("MIGRATION_SOURCE_OPERATION_ACTIVE")
    return snapshot


def _source_json(locator: InstanceLocator) -> dict[str, object]:
    if not isinstance(locator, InstanceLocator):
        raise MigrationUnsupportedError("MIGRATION_SOURCE_LOCATOR_UNSUPPORTED")
    try:
        payload = instance_locator_payload(locator)
        canonical = parse_instance_locator(payload)
    except LocatorError as error:
        raise MigrationUnsupportedError(
            "MIGRATION_SOURCE_LOCATOR_UNSUPPORTED"
        ) from error
    if canonical != locator:
        raise MigrationUnsupportedError("MIGRATION_SOURCE_LOCATOR_UNSUPPORTED")
    return payload


def _snapshot_json(snapshot: SourceConsistencySnapshot) -> dict[str, object]:
    return {
        "generation": snapshot.generation,
        "configGeneration": snapshot.config_generation,
        "quiesced": snapshot.quiesced,
        "writesBlocked": snapshot.writes_blocked,
        "updaterIdle": snapshot.updater_idle,
        "databaseMigrationIdle": snapshot.database_migration_idle,
        "pluginOperationsIdle": snapshot.plugin_operations_idle,
        "mediaWritesIdle": snapshot.media_writes_idle,
    }


def _canonical_time(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise MigrationOperationalError("MIGRATION_CLOCK_INVALID")
    return value.astimezone(UTC).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def _is_uuid(value: object) -> bool:
    if not isinstance(value, str) or _UUID_RE.fullmatch(value) is None:
        return False
    try:
        return str(UUID(value)) == value
    except (ValueError, AttributeError, TypeError):
        return False


def _safe_non_secret(
    value: object,
    *,
    depth: int = 0,
    counter: list[int] | None = None,
) -> object:
    if counter is None:
        counter = [0]
    counter[0] += 1
    if depth > MAX_IDENTITY_DEPTH or counter[0] > MAX_IDENTITY_MEMBERS:
        raise MigrationOperationalError("MIGRATION_METADATA_INVALID")
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise MigrationOperationalError("MIGRATION_METADATA_INVALID")
        return value
    if isinstance(value, str):
        if len(value.encode("utf-8")) > MAX_IDENTITY_STRING or _string_is_secret(value):
            raise MigrationOperationalError("MIGRATION_SECRET_METADATA_FORBIDDEN")
        return value
    if isinstance(value, Mapping):
        if len(value) > MAX_IDENTITY_MEMBERS:
            raise MigrationOperationalError("MIGRATION_METADATA_INVALID")
        normalized: dict[str, object] = {}
        if any(not isinstance(key, str) or not key for key in value):
            raise MigrationOperationalError("MIGRATION_METADATA_INVALID")
        for key in sorted(value, key=lambda item: item.encode("utf-8")):
            if _field_is_secret(key, value[key]):
                raise MigrationOperationalError("MIGRATION_SECRET_METADATA_FORBIDDEN")
            normalized[key] = _safe_non_secret(
                value[key], depth=depth + 1, counter=counter
            )
        return normalized
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        if len(value) > MAX_IDENTITY_MEMBERS:
            raise MigrationOperationalError("MIGRATION_METADATA_INVALID")
        return [
            _safe_non_secret(item, depth=depth + 1, counter=counter) for item in value
        ]
    raise MigrationOperationalError("MIGRATION_METADATA_INVALID")


def _field_is_secret(key: str, value: object) -> bool:
    normalized = "".join(character for character in key.lower() if character.isalnum())
    if not any(part in normalized for part in _SENSITIVE_KEY_PARTS):
        return False
    return not (
        normalized.endswith(
            (
                "configured",
                "disposition",
                "identity",
                "path",
                "present",
                "required",
                "status",
            )
        )
        and isinstance(value, (str, bool, int, type(None)))
    )


def _string_is_secret(value: str) -> bool:
    lowered = value.lower()
    return (
        lowered.startswith("bearer ")
        or "-----begin private key-----" in lowered
        or bool(re.search(r"://[^/@\s:]+:[^/@\s]+@", value))
        or bool(re.match(r"^[A-Z][A-Z0-9_]+\s*=", value))
    )


def _reject_unknown_extensions(value: object) -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if key in {"unknownExtensions", "unsupportedExtensions"} and child not in (
                None,
                [],
                {},
                (),
            ):
                raise MigrationUnsupportedError("MIGRATION_EXTENSION_UNSUPPORTED")
            _reject_unknown_extensions(child)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for child in value:
            _reject_unknown_extensions(child)


def _canonical_public_origin(value: object) -> str:
    if not isinstance(value, str):
        raise MigrationOperationalError("MIGRATION_PUBLIC_ORIGIN_INVALID")
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise MigrationOperationalError("MIGRATION_PUBLIC_ORIGIN_INVALID")
    return f"{parsed.scheme}://{parsed.netloc}"


def _listen_json(value: object) -> dict[str, object]:
    if not isinstance(value, ListenIdentity):
        raise MigrationOperationalError("MIGRATION_LISTEN_INVALID")
    if (
        not isinstance(value.host, str)
        or not value.host
        or any(character in value.host for character in "\r\n\x00")
        or isinstance(value.port, bool)
        or not isinstance(value.port, int)
        or not 1 <= value.port <= 65535
    ):
        raise MigrationOperationalError("MIGRATION_LISTEN_INVALID")
    return {"host": value.host, "port": value.port}


def _prepare_destination_root(path: Path) -> Path:
    raw = Path(path).expanduser().absolute()
    if _path_has_link_component(raw) or (raw.exists() and not raw.is_dir()):
        raise MigrationOperationalError("MIGRATION_DESTINATION_INVALID")
    try:
        raw.mkdir(parents=True, exist_ok=True)
        if _path_has_link_component(raw) or not raw.is_dir():
            raise MigrationOperationalError("MIGRATION_DESTINATION_INVALID")
        os.chmod(raw, 0o700)
        return raw.resolve(strict=True)
    except MigrationError:
        raise
    except OSError:
        raise MigrationOperationalError("MIGRATION_DESTINATION_INVALID") from None


def _copy_finalized_bundle_snapshot(source: Path, snapshot: Path) -> None:
    try:
        metadata = source.lstat()
    except OSError:
        raise MigrationOperationalError("MIGRATION_BUNDLE_UNAVAILABLE") from None
    if not stat.S_ISDIR(metadata.st_mode) or _is_link_or_reparse(source):
        raise MigrationCorruptError("MIGRATION_BUNDLE_ROOT_UNSAFE")
    try:
        _enumerate_bundle_files(source)
        copy_counter = CopyByteCounter(MAX_TOTAL_COPIED_BYTES)
        shutil.copytree(
            source,
            snapshot,
            symlinks=True,
            copy_function=partial(
                _copy_bundle_snapshot_regular,
                copy_counter=copy_counter,
            ),
        )
    except MigrationError:
        raise
    except OSError:
        raise MigrationOperationalError("MIGRATION_SNAPSHOT_FAILED") from None


def _copy_bundle_snapshot_regular(
    source: str,
    destination: str,
    *,
    copy_counter: CopyByteCounter,
) -> str:
    source_path = Path(source)
    destination_path = Path(destination)
    before = source_path.lstat()
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
        or _is_link_or_reparse(source_path)
        or _is_sparse(before)
    ):
        raise MigrationCorruptError("MIGRATION_MEMBER_UNSAFE")
    source_fd = os.open(source_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    destination_fd = -1
    try:
        opened = os.fstat(source_fd)
        if not stat.S_ISREG(opened.st_mode) or (
            before.st_dev,
            before.st_ino,
        ) != (opened.st_dev, opened.st_ino):
            raise MigrationOperationalError("MIGRATION_SOURCE_CHANGED")
        destination_fd = os.open(
            destination_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        with (
            os.fdopen(source_fd, "rb") as input_stream,
            os.fdopen(destination_fd, "wb") as output_stream,
        ):
            source_fd = -1
            destination_fd = -1
            try:
                bounded_copy(
                    input_stream,
                    output_stream,
                    counter=copy_counter,
                    maximum_member_bytes=(
                        MAX_COMPRESSED_MEMBER_BYTES
                        if source_path.name == DATABASE_MEMBER
                        else MAX_MEMBER_BYTES
                    ),
                    expected_size=opened.st_size,
                    member_reason=(
                        ResourceLimitReason.COMPRESSED_MEMBER_BYTES
                        if source_path.name == DATABASE_MEMBER
                        else ResourceLimitReason.FILESYSTEM_MEMBER_BYTES
                    ),
                )
            except ResourceLimitExceeded as error:
                if error.reason is ResourceLimitReason.DECLARED_SIZE_MISMATCH:
                    raise MigrationOperationalError(
                        "MIGRATION_SOURCE_CHANGED"
                    ) from None
                raise MigrationCorruptError(
                    "MIGRATION_RESOURCE_BOUNDS_EXCEEDED"
                ) from None
        os.chmod(destination_path, 0o600)
    finally:
        if source_fd >= 0:
            os.close(source_fd)
        if destination_fd >= 0:
            os.close(destination_fd)
    return str(destination_path)


def _cleanup_owned_staging(destination: Path, staging: Path) -> None:
    if staging.parent != destination or not staging.name.startswith(STAGING_PREFIX):
        return
    try:
        if _is_link_or_reparse(staging):
            staging.unlink(missing_ok=True)
        elif staging.is_dir():
            shutil.rmtree(staging)
    except OSError:
        return


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _make_private_directory(path: Path) -> None:
    pending: list[Path] = []
    cursor = path
    while not cursor.exists():
        pending.append(cursor)
        cursor = cursor.parent
    if _is_link_or_reparse(cursor) or not cursor.is_dir():
        raise MigrationOperationalError("MIGRATION_STAGING_UNSAFE")
    for directory in reversed(pending):
        directory.mkdir()
        if _is_link_or_reparse(directory) or not directory.is_dir():
            raise MigrationOperationalError("MIGRATION_STAGING_UNSAFE")
        os.chmod(directory, 0o700)


def _write_private_bytes(path: Path, data: bytes) -> None:
    _make_private_directory(path.parent)
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


def _write_json(path: Path, value: object) -> None:
    try:
        encoded = canonical_json_bytes(cast(Any, value)) + b"\n"
    except (TypeError, ValueError, UnicodeError):
        raise MigrationOperationalError("MIGRATION_METADATA_INVALID") from None
    _write_private_bytes(path, encoded)


def _read_regular_file(path: Path, *, maximum: int) -> bytes:
    try:
        item_stat = path.lstat()
        if (
            not stat.S_ISREG(item_stat.st_mode)
            or item_stat.st_nlink != 1
            or _is_link_or_reparse(path)
            or item_stat.st_size > maximum
        ):
            raise MigrationCorruptError("MIGRATION_MEMBER_UNSAFE")
        with path.open("rb") as stream:
            return stream.read(maximum + 1)
    except MigrationError:
        raise
    except FileNotFoundError:
        raise MigrationCorruptError("MIGRATION_MEMBER_MISSING") from None
    except OSError:
        raise MigrationOperationalError("MIGRATION_MEMBER_UNAVAILABLE") from None


def _capture_database_references(
    probe: DatabaseReferenceProbe,
) -> dict[str, object]:
    try:
        inventory = probe.capture()
    except Exception:  # noqa: BLE001 - database probe details are redacted
        raise MigrationOperationalError(
            "MIGRATION_DATABASE_REFERENCE_PROBE_FAILED"
        ) from None
    if not isinstance(inventory, DatabaseReferenceInventory):
        raise MigrationOperationalError(
            "MIGRATION_DATABASE_REFERENCE_INVENTORY_INVALID"
        )
    generation = inventory.generation
    if (
        not isinstance(generation, str)
        or not generation
        or len(generation.encode("utf-8")) > 256
        or _string_is_secret(generation)
    ):
        raise MigrationOperationalError(
            "MIGRATION_DATABASE_REFERENCE_INVENTORY_INVALID"
        )

    plugins: list[dict[str, object]] = []
    for item in sorted(
        inventory.plugin_packages,
        key=lambda value: (
            (
                value.project_id.encode("utf-8"),
                value.version_id.encode("utf-8"),
                value.deployment_id.encode("utf-8"),
            )
            if isinstance(value, PluginDatabaseReference)
            else (b"", b"", b"")
        ),
    ):
        if not isinstance(item, PluginDatabaseReference):
            raise MigrationOperationalError(
                "MIGRATION_DATABASE_REFERENCE_INVENTORY_INVALID"
            )
        plugins.append(
            {
                "projectId": _safe_identifier(item.project_id),
                "versionId": _safe_identifier(item.version_id),
                "deploymentId": _safe_identifier(item.deployment_id),
                "digest": _require_sha256(
                    item.digest, "MIGRATION_DATABASE_REFERENCE_INVENTORY_INVALID"
                ),
                "sdkApis": _sorted_unique_strings(
                    item.sdk_apis,
                    code="MIGRATION_DATABASE_REFERENCE_INVENTORY_INVALID",
                ),
                "manifestSnapshotDigest": _require_sha256(
                    item.manifest_snapshot_digest,
                    "MIGRATION_DATABASE_REFERENCE_INVENTORY_INVALID",
                ),
            }
        )

    local: list[dict[str, object]] = []
    for item in sorted(
        inventory.local_media,
        key=lambda value: (
            value.media_id.encode("utf-8")
            if isinstance(value, LocalMediaDatabaseReference)
            else b""
        ),
    ):
        if not isinstance(item, LocalMediaDatabaseReference):
            raise MigrationOperationalError(
                "MIGRATION_DATABASE_REFERENCE_INVENTORY_INVALID"
            )
        local.append(_database_local_reference_json(item))

    remote: list[dict[str, object]] = []
    for item in sorted(
        inventory.r2_media,
        key=lambda value: (
            value.media_id.encode("utf-8")
            if isinstance(value, R2MediaDatabaseReference)
            else b""
        ),
    ):
        if not isinstance(item, R2MediaDatabaseReference):
            raise MigrationOperationalError(
                "MIGRATION_DATABASE_REFERENCE_INVENTORY_INVALID"
            )
        record = _database_media_reference_common(item)
        record["backendId"] = _safe_identifier(item.backend_id)
        record["physicalIdentity"] = _r2_identity_json(item.source_identity)
        remote.append(record)
    result = {
        "generation": generation,
        "plugins": plugins,
        "localMedia": local,
        "r2Media": remote,
    }
    _require_unique_reference_identities(result)
    return result


def _database_local_reference_json(
    item: LocalMediaDatabaseReference,
) -> dict[str, object]:
    return _database_media_reference_common(item)


def _database_media_reference_common(
    item: LocalMediaDatabaseReference | R2MediaDatabaseReference,
) -> dict[str, object]:
    size = item.size_bytes
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        raise MigrationOperationalError(
            "MIGRATION_DATABASE_REFERENCE_INVENTORY_INVALID"
        )
    return {
        "mediaId": _safe_identifier(item.media_id),
        "objectKey": _canonical_object_key(item.object_key),
        "digest": _require_sha256(
            item.digest, "MIGRATION_DATABASE_REFERENCE_INVENTORY_INVALID"
        ),
        "sizeBytes": size,
        "memoryReferences": _sorted_unique_strings(
            item.memory_references,
            code="MIGRATION_DATABASE_REFERENCE_INVENTORY_INVALID",
        ),
    }


def _require_unique_reference_identities(value: Mapping[str, object]) -> None:
    plugins = cast(Sequence[Mapping[str, object]], value["plugins"])
    plugin_ids = {
        (item["projectId"], item["versionId"], item["deploymentId"]) for item in plugins
    }
    local = cast(Sequence[Mapping[str, object]], value["localMedia"])
    remote = cast(Sequence[Mapping[str, object]], value["r2Media"])
    media_ids = [item["mediaId"] for item in (*local, *remote)]
    object_ids = [("local", item["objectKey"]) for item in local] + [
        (item["backendId"], item["objectKey"]) for item in remote
    ]
    if (
        len(plugin_ids) != len(plugins)
        or len(set(media_ids)) != len(media_ids)
        or len(set(object_ids)) != len(object_ids)
    ):
        raise MigrationCorruptError("MIGRATION_DATABASE_REFERENCE_AMBIGUOUS")


def _cross_check_database_references(
    references: Mapping[str, object],
    plugin_manifest: Mapping[str, object],
    media_manifest: Mapping[str, object],
) -> None:
    expected_plugins = [
        {
            key: package[key]
            for key in (
                "projectId",
                "versionId",
                "deploymentId",
                "digest",
                "sdkApis",
                "manifestSnapshotDigest",
            )
        }
        for package in cast(Sequence[Mapping[str, object]], plugin_manifest["packages"])
    ]
    expected_local = [
        {
            key: item[key]
            for key in (
                "mediaId",
                "objectKey",
                "digest",
                "sizeBytes",
                "memoryReferences",
            )
        }
        for item in cast(Sequence[Mapping[str, object]], media_manifest["local"])
    ]
    expected_remote = [
        {
            key: item[key]
            for key in (
                "mediaId",
                "objectKey",
                "digest",
                "sizeBytes",
                "memoryReferences",
                "backendId",
                "physicalIdentity",
            )
        }
        for item in cast(Sequence[Mapping[str, object]], media_manifest["r2"])
    ]
    if (
        references.get("plugins") != expected_plugins
        or references.get("localMedia") != expected_local
        or references.get("r2Media") != expected_remote
    ):
        raise MigrationCorruptError("MIGRATION_DATABASE_REFERENCE_MISMATCH")


def _capture_plugins(
    staging: Path,
    packages: Sequence[PluginPackage],
    *,
    copy_counter: CopyByteCounter,
) -> dict[str, object]:
    records: list[dict[str, object]] = []
    identities: set[tuple[str, str, str]] = set()
    copied: set[str] = set()
    for package in sorted(
        packages,
        key=lambda item: (
            (
                item.project_id.encode("utf-8"),
                item.version_id.encode("utf-8"),
                item.deployment_id.encode("utf-8"),
            )
            if isinstance(item, PluginPackage)
            else (b"", b"", b"")
        ),
    ):
        if not isinstance(package, PluginPackage):
            raise MigrationOperationalError("MIGRATION_PLUGIN_INVENTORY_INVALID")
        identity = (
            _safe_identifier(package.project_id),
            _safe_identifier(package.version_id),
            _safe_identifier(package.deployment_id),
        )
        if identity in identities:
            raise MigrationCorruptError("MIGRATION_PLUGIN_IDENTITY_DUPLICATED")
        identities.add(identity)
        _require_sha256(package.digest, "MIGRATION_PLUGIN_DIGEST_INVALID")
        _require_sha256(
            package.manifest_snapshot_digest,
            "MIGRATION_PLUGIN_MANIFEST_IDENTITY_INVALID",
        )
        sdk_apis = _sorted_unique_strings(
            package.sdk_apis, code="MIGRATION_PLUGIN_SDK_INVALID"
        )
        member = f"plugins/cas/{package.digest.removeprefix('sha256:')}.ajplugin"
        if member not in copied:
            _copy_verified_source(
                Path(package.source),
                staging / PurePosixPath(member),
                package.digest,
                copy_counter=copy_counter,
            )
            copied.add(member)
        size = (staging / PurePosixPath(member)).stat().st_size
        records.append(
            {
                "projectId": identity[0],
                "versionId": identity[1],
                "deploymentId": identity[2],
                "digest": package.digest,
                "casPath": member,
                "sizeBytes": size,
                "sdkApis": sdk_apis,
                "manifestSnapshotDigest": package.manifest_snapshot_digest,
            }
        )
    return {
        "packages": records,
        "runtimeIncluded": False,
        "previewsIncluded": False,
        "stagingIncluded": False,
        "locksIncluded": False,
        "pluginDataDeletion": "NEVER_AUTOMATIC",
    }


def _capture_media(
    staging: Path,
    local_media: Sequence[LocalMediaObject],
    r2_media: Sequence[R2MediaObject],
    target_identities: Mapping[str, R2PhysicalIdentity],
    *,
    copy_counter: CopyByteCounter,
) -> dict[str, object]:
    local_records: list[dict[str, object]] = []
    remote_records: list[dict[str, object]] = []
    seen_media: set[str] = set()
    seen_objects: set[tuple[str, str]] = set()
    copied: set[str] = set()
    for item in sorted(
        local_media,
        key=lambda value: (
            value.media_id.encode("utf-8")
            if isinstance(value, LocalMediaObject)
            else b""
        ),
    ):
        if not isinstance(item, LocalMediaObject):
            raise MigrationOperationalError("MIGRATION_MEDIA_INVENTORY_INVALID")
        media_id = _safe_identifier(item.media_id)
        if media_id in seen_media:
            raise MigrationCorruptError("MIGRATION_MEDIA_IDENTITY_DUPLICATED")
        seen_media.add(media_id)
        object_key = _canonical_object_key(item.object_key)
        object_identity = ("local", object_key)
        if object_identity in seen_objects:
            raise MigrationCorruptError("MIGRATION_MEDIA_OWNERSHIP_AMBIGUOUS")
        seen_objects.add(object_identity)
        _require_sha256(item.digest, "MIGRATION_MEDIA_DIGEST_INVALID")
        if (
            isinstance(item.size_bytes, bool)
            or not isinstance(item.size_bytes, int)
            or item.size_bytes < 0
        ):
            raise MigrationOperationalError("MIGRATION_MEDIA_SIZE_INVALID")
        member = f"media/local/{item.digest.removeprefix('sha256:')}"
        if member not in copied:
            _copy_verified_source(
                Path(item.source),
                staging / PurePosixPath(member),
                item.digest,
                expected_size=item.size_bytes,
                copy_counter=copy_counter,
            )
            copied.add(member)
        elif (staging / PurePosixPath(member)).stat().st_size != item.size_bytes:
            raise MigrationCorruptError("MIGRATION_MEDIA_IDENTITY_MISMATCH")
        local_records.append(
            {
                "mediaId": media_id,
                "objectKey": object_key,
                "digest": item.digest,
                "sizeBytes": item.size_bytes,
                "memberPath": member,
                "memoryReferences": _sorted_unique_strings(
                    item.memory_references, code="MIGRATION_MEMORY_REFERENCE_INVALID"
                ),
                "strategy": "LOCAL_INCLUDED",
            }
        )
    for item in sorted(
        r2_media,
        key=lambda value: (
            value.media_id.encode("utf-8") if isinstance(value, R2MediaObject) else b""
        ),
    ):
        if not isinstance(item, R2MediaObject):
            raise MigrationOperationalError("MIGRATION_MEDIA_INVENTORY_INVALID")
        media_id = _safe_identifier(item.media_id)
        backend_id = _safe_identifier(item.backend_id)
        if media_id in seen_media:
            raise MigrationCorruptError("MIGRATION_MEDIA_IDENTITY_DUPLICATED")
        seen_media.add(media_id)
        source_identity = _r2_identity_json(item.source_identity)
        target_identity = target_identities.get(backend_id)
        if target_identity is None:
            raise MigrationUnsupportedError("MIGRATION_R2_IDENTITY_INDETERMINATE")
        if _r2_identity_json(target_identity) != source_identity:
            raise MigrationUnsupportedError("MIGRATION_R2_TRANSFER_REQUIRED")
        _require_sha256(item.digest, "MIGRATION_MEDIA_DIGEST_INVALID")
        if (
            isinstance(item.size_bytes, bool)
            or not isinstance(item.size_bytes, int)
            or item.size_bytes < 0
        ):
            raise MigrationOperationalError("MIGRATION_MEDIA_SIZE_INVALID")
        object_key = _canonical_object_key(item.object_key)
        object_identity = (backend_id, object_key)
        if object_identity in seen_objects:
            raise MigrationCorruptError("MIGRATION_MEDIA_OWNERSHIP_AMBIGUOUS")
        seen_objects.add(object_identity)
        remote_records.append(
            {
                "mediaId": media_id,
                "backendId": backend_id,
                "objectKey": object_key,
                "digest": item.digest,
                "sizeBytes": item.size_bytes,
                "physicalIdentity": source_identity,
                "memoryReferences": _sorted_unique_strings(
                    item.memory_references, code="MIGRATION_MEMORY_REFERENCE_INVALID"
                ),
                "strategy": "SAME_R2",
                "remoteBytesCopied": False,
            }
        )
    return {
        "local": local_records,
        "r2": remote_records,
        "unknownOrphanPolicy": "PRESERVE_NEVER_DELETE",
        "automaticDeletion": False,
    }


def _copy_verified_source(
    source: Path,
    target: Path,
    expected_digest: str,
    *,
    expected_size: int | None = None,
    copy_counter: CopyByteCounter,
) -> None:
    try:
        before = source.lstat()
    except OSError:
        raise MigrationCorruptError("MIGRATION_SOURCE_MEMBER_MISSING") from None
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
        or _path_has_link_component(source)
        or _is_sparse(before)
    ):
        raise MigrationCorruptError("MIGRATION_SOURCE_MEMBER_UNSAFE")
    if expected_size is not None and before.st_size != expected_size:
        raise MigrationCorruptError("MIGRATION_SOURCE_MEMBER_SIZE_MISMATCH")
    try:
        preflight_copy_sizes(
            (before.st_size,),
            maximum_member_bytes=MAX_MEMBER_BYTES,
            maximum_total_bytes=(copy_counter.maximum_bytes - copy_counter.copied),
        )
    except ResourceLimitExceeded:
        raise MigrationOperationalError("MIGRATION_RESOURCE_BOUNDS_EXCEEDED") from None
    _make_private_directory(target.parent)
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    digest = hashlib.sha256()
    copied = 0
    try:
        with source.open("rb") as input_stream, os.fdopen(descriptor, "wb") as output:
            descriptor = -1

            class _DigestingTarget:
                def write(self, chunk: bytes) -> int:
                    digest.update(chunk)
                    return output.write(chunk)

            try:
                copied = bounded_copy(
                    input_stream,
                    cast(Any, _DigestingTarget()),
                    counter=copy_counter,
                    maximum_member_bytes=MAX_MEMBER_BYTES,
                    expected_size=before.st_size,
                )
            except ResourceLimitExceeded as error:
                if error.reason is ResourceLimitReason.DECLARED_SIZE_MISMATCH:
                    raise MigrationOperationalError(
                        "MIGRATION_SOURCE_CHANGED"
                    ) from None
                raise MigrationOperationalError(
                    "MIGRATION_RESOURCE_BOUNDS_EXCEEDED"
                ) from None
            output.flush()
            os.fsync(output.fileno())
        after = source.lstat()
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise MigrationOperationalError("MIGRATION_SOURCE_CHANGED")
        if f"sha256:{digest.hexdigest()}" != expected_digest:
            raise MigrationCorruptError("MIGRATION_SOURCE_MEMBER_DIGEST_MISMATCH")
        if expected_size is not None and copied != expected_size:
            raise MigrationCorruptError("MIGRATION_SOURCE_MEMBER_SIZE_MISMATCH")
        os.chmod(target, 0o600)
    except Exception:
        target.unlink(missing_ok=True)
        raise
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _payload_records(root: Path) -> list[dict[str, object]]:
    files = _enumerate_bundle_files(root)
    records = []
    for relative in sorted(files, key=lambda item: item.encode("utf-8")):
        if relative in {MANIFEST_NAME, CHECKSUMS_NAME}:
            continue
        path = files[relative]
        records.append(
            {
                "path": relative,
                "sha256": _sha256_file(path),
                "sizeBytes": path.stat().st_size,
            }
        )
    if len(records) > MAX_MEMBER_COUNT:
        raise MigrationCorruptError("MIGRATION_MEMBER_COUNT_EXCEEDED")
    return records


def _checksum_bytes(records: Sequence[Mapping[str, object]]) -> bytes:
    lines: list[str] = []
    previous: bytes | None = None
    for record in sorted(
        records, key=lambda item: str(item.get("path", "")).encode("utf-8")
    ):
        if set(record) != {"path", "sha256", "sizeBytes"}:
            raise MigrationCorruptError("MIGRATION_MEMBER_RECORD_INVALID")
        relative = _canonical_relative_path(record["path"])
        _validate_member_path(relative)
        digest = record["sha256"]
        _require_sha256(digest, "MIGRATION_MEMBER_RECORD_INVALID")
        size = record["sizeBytes"]
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise MigrationCorruptError("MIGRATION_MEMBER_RECORD_INVALID")
        ordering = relative.encode("utf-8")
        if previous is not None and ordering <= previous:
            raise MigrationCorruptError("MIGRATION_MEMBER_RECORD_INVALID")
        lines.append(f"{digest.removeprefix('sha256:')}  {relative}\n")
        previous = ordering
    return "".join(lines).encode("utf-8")


def _strict_json_object(encoded: bytes, *, maximum: int, code: str) -> dict[str, Any]:
    if len(encoded) > maximum:
        raise MigrationCorruptError(code)

    def reject_constant(_: str) -> None:
        raise ValueError

    def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
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
    except (UnicodeError, ValueError, TypeError, json.JSONDecodeError):
        raise MigrationCorruptError(code) from None
    if not isinstance(parsed, dict):
        raise MigrationCorruptError(code)
    return parsed


def _read_json_member(root: Path, relative: str) -> dict[str, Any]:
    encoded = _read_regular_file(
        root / PurePosixPath(relative), maximum=MAX_JSON_MEMBER_BYTES
    )
    parsed = _strict_json_object(
        encoded, maximum=MAX_JSON_MEMBER_BYTES, code="MIGRATION_JSON_MEMBER_CORRUPT"
    )
    if canonical_json_bytes(parsed) + b"\n" != encoded:
        raise MigrationCorruptError("MIGRATION_JSON_MEMBER_NONCANONICAL")
    return parsed


def _verify_bundle(root: Path, *, staging_allowed: bool) -> MigrationVerification:
    path = Path(root).absolute()
    try:
        root_stat = path.lstat()
    except OSError:
        raise MigrationOperationalError("MIGRATION_BUNDLE_UNAVAILABLE") from None
    if (
        not stat.S_ISDIR(root_stat.st_mode)
        or _path_has_link_component(path)
        or (os.name != "nt" and stat.S_IMODE(root_stat.st_mode) & 0o077)
    ):
        raise MigrationCorruptError("MIGRATION_BUNDLE_ROOT_UNSAFE")
    if path.name.startswith(STAGING_PREFIX) and not staging_allowed:
        raise MigrationCorruptError("MIGRATION_BUNDLE_NOT_FINALIZED")

    files = _enumerate_bundle_files(path)
    if MANIFEST_NAME not in files or CHECKSUMS_NAME not in files:
        raise MigrationCorruptError("MIGRATION_CONTROL_MEMBER_MISSING")
    manifest_bytes = _read_regular_file(
        files[MANIFEST_NAME], maximum=MAX_MANIFEST_BYTES
    )
    manifest = _strict_json_object(
        manifest_bytes,
        maximum=MAX_MANIFEST_BYTES,
        code="MIGRATION_MANIFEST_CORRUPT",
    )
    if canonical_json_bytes(manifest) + b"\n" != manifest_bytes:
        raise MigrationCorruptError("MIGRATION_MANIFEST_NONCANONICAL")
    format_identity = manifest.get("format")
    format_version = manifest.get("formatVersion")
    if (
        isinstance(format_identity, str)
        and isinstance(format_version, int)
        and not isinstance(format_version, bool)
        and (format_identity != FORMAT or format_version != FORMAT_VERSION)
    ):
        raise MigrationUnsupportedError("MIGRATION_FORMAT_UNSUPPORTED")
    if format_identity != FORMAT or format_version != FORMAT_VERSION:
        raise MigrationCorruptError("MIGRATION_FORMAT_IDENTITY_CORRUPT")
    expected_fields = {
        "format",
        "formatVersion",
        "bundleId",
        "createdAt",
        "instanceId",
        "lifecycle",
        "source",
        "deploymentContract",
        "databaseContract",
        "configurationContract",
        "database",
        "databaseReferences",
        "configuration",
        "plugins",
        "media",
        "privateState",
        "updaterState",
        "sourceConsistency",
        "artifactBindingRecord",
        "artifactBindingDigest",
        "secretEnvelope",
        "members",
        "checksumSetDigest",
        "compatibility",
        "activation",
    }
    if set(manifest) != expected_fields:
        raise MigrationCorruptError("MIGRATION_MANIFEST_SHAPE_CORRUPT")
    bundle_id = manifest["bundleId"]
    instance_id = manifest["instanceId"]
    if not _is_uuid(bundle_id) or not _is_uuid(instance_id):
        raise MigrationCorruptError("MIGRATION_IDENTITY_CORRUPT")
    if not staging_allowed and path.name != bundle_id:
        raise MigrationCorruptError("MIGRATION_BUNDLE_IDENTITY_MISMATCH")
    if manifest["lifecycle"] != "FINALIZED":
        raise MigrationCorruptError("MIGRATION_BUNDLE_NOT_FINALIZED")
    _parse_canonical_time(manifest["createdAt"])

    source = manifest["source"]
    if not isinstance(source, Mapping):
        raise MigrationCorruptError("MIGRATION_SOURCE_LOCATOR_CORRUPT")
    try:
        parsed_locator = parse_instance_locator(cast(Mapping[str, Any], source))
        canonical_source = _source_json(parsed_locator)
    except (LocatorError, MigrationOperationalError, MigrationUnsupportedError):
        raise MigrationCorruptError("MIGRATION_SOURCE_LOCATOR_CORRUPT") from None
    if source != canonical_source or source.get("instanceId") != instance_id:
        raise MigrationCorruptError("MIGRATION_SOURCE_LOCATOR_CORRUPT")

    for key in (
        "deploymentContract",
        "databaseContract",
        "configurationContract",
        "databaseReferences",
        "privateState",
        "updaterState",
    ):
        try:
            if _safe_non_secret(manifest[key]) != manifest[key]:
                raise MigrationCorruptError("MIGRATION_METADATA_CORRUPT")
            _reject_unknown_extensions(manifest[key])
        except MigrationUnsupportedError:
            raise
        except MigrationError:
            raise MigrationCorruptError("MIGRATION_METADATA_CORRUPT") from None
    if not all(
        isinstance(manifest[key], Mapping) and manifest[key]
        for key in (
            "deploymentContract",
            "databaseContract",
            "configurationContract",
        )
    ):
        raise MigrationCorruptError("MIGRATION_CONTRACT_IDENTITY_CORRUPT")
    source_release = cast(Mapping[str, object], source["releaseIdentity"])
    if (
        _validate_private_state(manifest["privateState"], producer=False)
        != manifest["privateState"]
    ):
        raise MigrationCorruptError("MIGRATION_PRIVATE_STATE_INVALID")
    if (
        _validate_updater_state(
            manifest["updaterState"], source_release, producer=False
        )
        != manifest["updaterState"]
    ):
        raise MigrationCorruptError("MIGRATION_UPDATER_STATE_INVALID")

    records = _payload_records(path)
    members = manifest["members"]
    if not isinstance(members, list) or members != records:
        raise MigrationCorruptError("MIGRATION_MEMBER_INVENTORY_CORRUPT")
    member_paths = {cast(str, record["path"]) for record in records}
    if not _CORE_MEMBERS.issubset(member_paths):
        raise MigrationCorruptError("MIGRATION_REQUIRED_MEMBER_MISSING")
    checksum_bytes = _read_regular_file(
        files[CHECKSUMS_NAME], maximum=MAX_CHECKSUM_BYTES
    )
    if checksum_bytes != _checksum_bytes(records):
        raise MigrationCorruptError("MIGRATION_CHECKSUM_SET_CORRUPT")
    if manifest["checksumSetDigest"] != sha256_identity(checksum_bytes):
        raise MigrationCorruptError("MIGRATION_CHECKSUM_SET_CORRUPT")

    database = _read_json_member(path, DATABASE_METADATA_MEMBER)
    plugin_manifest = _read_json_member(path, PLUGIN_MANIFEST_MEMBER)
    media_manifest = _read_json_member(path, MEDIA_MANIFEST_MEMBER)
    configuration = _read_json_member(path, CONFIG_MEMBER)
    private_state = _read_json_member(path, PRIVATE_MANIFEST_MEMBER)
    updater_member = _read_json_member(path, UPDATER_STATE_MEMBER)
    if (
        manifest["database"] != database
        or manifest["plugins"] != plugin_manifest
        or manifest["media"] != media_manifest
        or manifest["configuration"] != configuration
        or manifest["privateState"] != private_state
        or manifest["updaterState"] != updater_member
    ):
        raise MigrationCorruptError("MIGRATION_CROSS_MEMBER_BINDING_FAILED")
    _verify_database(path, database)
    _verify_plugins(plugin_manifest, records)
    _verify_media(media_manifest, records)
    _verify_database_references(
        manifest["databaseReferences"], plugin_manifest, media_manifest
    )
    configuration_mode = _verify_configuration(configuration)
    consistency_generation = _verify_source_consistency(manifest["sourceConsistency"])

    binding_record = manifest["artifactBindingRecord"]
    expected_binding_record = {
        "format": FORMAT,
        "formatVersion": FORMAT_VERSION,
        "bundleId": bundle_id,
        "instanceId": instance_id,
        "createdAt": manifest["createdAt"],
        "source": source,
        "deploymentContract": manifest["deploymentContract"],
        "databaseContract": manifest["databaseContract"],
        "configurationContract": manifest["configurationContract"],
        "database": database,
        "databaseReferences": manifest["databaseReferences"],
        "payloadMembers": [
            record for record in records if record["path"] != ENVELOPE_PATH
        ],
        "plugins": plugin_manifest,
        "media": media_manifest,
        "configuration": configuration,
        "privateState": private_state,
        "updaterState": updater_member,
        "sourceConsistency": manifest["sourceConsistency"],
        "secretProfileIdentity": ENVELOPE_IDENTITY,
        "activationProfile": "explicit-source-target-handoff/v1",
    }
    if binding_record != expected_binding_record:
        raise MigrationCorruptError("MIGRATION_ARTIFACT_BINDING_CORRUPT")
    artifact_binding_digest = sha256_identity(
        canonical_json_bytes(cast(Mapping[str, object], binding_record))
    )
    if manifest["artifactBindingDigest"] != artifact_binding_digest:
        raise MigrationCorruptError("MIGRATION_ARTIFACT_BINDING_CORRUPT")
    _verify_envelope(
        path,
        manifest["secretEnvelope"],
        bundle_id=bundle_id,
        binding_digest=artifact_binding_digest,
        records=records,
    )
    if manifest["compatibility"] != {
        "matrixVersion": MATRIX_IDENTITY,
        "requiredOutcome": "COMPATIBLE",
    }:
        raise MigrationCorruptError("MIGRATION_COMPATIBILITY_METADATA_CORRUPT")
    if manifest["activation"] != {
        "state": "READY_FOR_HANDOFF",
        "sourceOwnership": "QUIESCED",
        "targetOwnership": "INACTIVE",
        "sourceDeletion": "NEVER_AUTOMATIC",
    }:
        raise MigrationCorruptError("MIGRATION_ACTIVATION_METADATA_CORRUPT")

    return MigrationVerification(
        path=path,
        bundle_id=bundle_id,
        instance_id=instance_id,
        manifest_digest=sha256_identity(manifest_bytes),
        artifact_binding_digest=artifact_binding_digest,
        source_consistency_generation=consistency_generation,
        configuration_mode=configuration_mode,
        manifest=MappingProxyType(manifest),
    )


def _verify_database(root: Path, database: Mapping[str, object]) -> None:
    if (
        set(database)
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
        raise MigrationCorruptError("MIGRATION_DATABASE_METADATA_CORRUPT")
    profile = database.get("dumpProfile")
    if profile != {
        "format": "plain",
        "argv": list(PG_DUMP_ARGUMENTS),
        "compression": "gzip",
        "gzipMtime": 0,
    }:
        raise MigrationCorruptError("MIGRATION_DATABASE_PROFILE_CORRUPT")
    if (
        isinstance(database.get("serverMajor"), bool)
        or not isinstance(database.get("serverMajor"), int)
        or cast(int, database["serverMajor"]) <= 0
        or not isinstance(database.get("toolVersion"), str)
        or not cast(str, database["toolVersion"]).startswith("pg_dump ")
    ):
        raise MigrationCorruptError("MIGRATION_DATABASE_METADATA_CORRUPT")
    for field in ("uncompressedSha256", "compressedSha256"):
        _require_sha256(database.get(field), "MIGRATION_DATABASE_METADATA_CORRUPT")
    for field in ("uncompressedBytes", "compressedBytes"):
        value = database.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise MigrationCorruptError("MIGRATION_DATABASE_METADATA_CORRUPT")
    member = root / DATABASE_MEMBER
    compressed_size = member.stat().st_size
    if database["compressedBytes"] != compressed_size or database[
        "compressedSha256"
    ] != _sha256_file(member):
        raise MigrationCorruptError("MIGRATION_DATABASE_COMPRESSED_CORRUPT")
    digest = hashlib.sha256()
    try:
        expansion = DatabaseExpansionGuard(
            compressed_bytes=compressed_size,
            budget=DEFAULT_RESOURCE_BUDGET,
        )
        with gzip.open(member, "rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                expansion.consume(len(chunk))
                digest.update(chunk)
    except ResourceLimitExceeded:
        raise MigrationCorruptError("MIGRATION_DATABASE_BOUNDS_EXCEEDED") from None
    except MigrationError:
        raise
    except (gzip.BadGzipFile, EOFError):
        raise MigrationCorruptError("MIGRATION_DATABASE_GZIP_CORRUPT") from None
    except OSError:
        raise MigrationOperationalError("MIGRATION_DATABASE_UNAVAILABLE") from None
    uncompressed_size = expansion.uncompressed_bytes
    if (
        not uncompressed_size
        or database["uncompressedBytes"] != uncompressed_size
        or database["uncompressedSha256"] != f"sha256:{digest.hexdigest()}"
    ):
        raise MigrationCorruptError("MIGRATION_DATABASE_UNCOMPRESSED_CORRUPT")


def _verify_database_references(
    value: object,
    plugin_manifest: Mapping[str, object],
    media_manifest: Mapping[str, object],
) -> None:
    if not isinstance(value, Mapping) or set(value) != {
        "generation",
        "plugins",
        "localMedia",
        "r2Media",
    }:
        raise MigrationCorruptError("MIGRATION_DATABASE_REFERENCE_INVENTORY_CORRUPT")
    generation = value.get("generation")
    if (
        not isinstance(generation, str)
        or not generation
        or len(generation.encode("utf-8")) > 256
        or _string_is_secret(generation)
        or any(
            not isinstance(value.get(field), list)
            for field in ("plugins", "localMedia", "r2Media")
        )
    ):
        raise MigrationCorruptError("MIGRATION_DATABASE_REFERENCE_INVENTORY_CORRUPT")
    try:
        _require_unique_reference_identities(value)
        _cross_check_database_references(value, plugin_manifest, media_manifest)
    except MigrationError:
        raise MigrationCorruptError("MIGRATION_DATABASE_REFERENCE_MISMATCH") from None


def _verify_plugins(
    manifest: Mapping[str, object], records: Sequence[Mapping[str, object]]
) -> None:
    if (
        set(manifest)
        != {
            "packages",
            "runtimeIncluded",
            "previewsIncluded",
            "stagingIncluded",
            "locksIncluded",
            "pluginDataDeletion",
        }
        or any(
            manifest.get(field) is not False
            for field in (
                "runtimeIncluded",
                "previewsIncluded",
                "stagingIncluded",
                "locksIncluded",
            )
        )
        or manifest.get("pluginDataDeletion") != "NEVER_AUTOMATIC"
    ):
        raise MigrationCorruptError("MIGRATION_PLUGIN_MANIFEST_CORRUPT")
    packages = manifest.get("packages")
    if not isinstance(packages, list):
        raise MigrationCorruptError("MIGRATION_PLUGIN_MANIFEST_CORRUPT")
    member_records = {record["path"]: record for record in records}
    declared: set[str] = set()
    identities: set[tuple[object, object, object]] = set()
    ordering: list[tuple[bytes, bytes, bytes]] = []
    for package in packages:
        if not isinstance(package, Mapping) or set(package) != {
            "projectId",
            "versionId",
            "deploymentId",
            "digest",
            "casPath",
            "sizeBytes",
            "sdkApis",
            "manifestSnapshotDigest",
        }:
            raise MigrationCorruptError("MIGRATION_PLUGIN_MANIFEST_CORRUPT")
        try:
            identity = tuple(
                _safe_identifier(cast(str, package[field]))
                for field in ("projectId", "versionId", "deploymentId")
            )
        except MigrationError:
            raise MigrationCorruptError("MIGRATION_PLUGIN_MANIFEST_CORRUPT") from None
        if identity in identities:
            raise MigrationCorruptError("MIGRATION_PLUGIN_MANIFEST_CORRUPT")
        identities.add(identity)
        ordering.append(
            cast(
                tuple[bytes, bytes, bytes],
                tuple(item.encode("utf-8") for item in identity),
            )
        )
        _require_sha256(package["digest"], "MIGRATION_PLUGIN_MANIFEST_CORRUPT")
        _require_sha256(
            package["manifestSnapshotDigest"], "MIGRATION_PLUGIN_MANIFEST_CORRUPT"
        )
        try:
            _sorted_unique_strings(
                cast(Sequence[object], package["sdkApis"]),
                code="MIGRATION_PLUGIN_MANIFEST_CORRUPT",
                require_canonical=True,
            )
        except MigrationError:
            raise MigrationCorruptError("MIGRATION_PLUGIN_MANIFEST_CORRUPT") from None
        expected_path = f"plugins/cas/{cast(str, package['digest']).removeprefix('sha256:')}.ajplugin"
        if package["casPath"] != expected_path or expected_path not in member_records:
            raise MigrationCorruptError("MIGRATION_PLUGIN_MEMBER_CORRUPT")
        member = member_records[expected_path]
        if (
            package["digest"] != member["sha256"]
            or package["sizeBytes"] != member["sizeBytes"]
        ):
            raise MigrationCorruptError("MIGRATION_PLUGIN_MEMBER_CORRUPT")
        declared.add(expected_path)
    if ordering != sorted(ordering) or declared != {
        cast(str, record["path"])
        for record in records
        if cast(str, record["path"]).startswith("plugins/cas/")
    }:
        raise MigrationCorruptError("MIGRATION_PLUGIN_MANIFEST_CORRUPT")


def _verify_media(
    manifest: Mapping[str, object], records: Sequence[Mapping[str, object]]
) -> None:
    if (
        set(manifest)
        != {
            "local",
            "r2",
            "unknownOrphanPolicy",
            "automaticDeletion",
        }
        or manifest.get("unknownOrphanPolicy") != "PRESERVE_NEVER_DELETE"
        or manifest.get("automaticDeletion") is not False
    ):
        raise MigrationCorruptError("MIGRATION_MEDIA_MANIFEST_CORRUPT")
    local = manifest.get("local")
    remote = manifest.get("r2")
    if not isinstance(local, list) or not isinstance(remote, list):
        raise MigrationCorruptError("MIGRATION_MEDIA_MANIFEST_CORRUPT")
    member_records = {record["path"]: record for record in records}
    declared: set[str] = set()
    media_ids: set[str] = set()
    object_identities: set[tuple[str, str]] = set()
    previous: bytes | None = None
    for item in local:
        if (
            not isinstance(item, Mapping)
            or set(item)
            != {
                "mediaId",
                "objectKey",
                "digest",
                "sizeBytes",
                "memberPath",
                "memoryReferences",
                "strategy",
            }
            or item.get("strategy") != "LOCAL_INCLUDED"
        ):
            raise MigrationCorruptError("MIGRATION_MEDIA_MANIFEST_CORRUPT")
        media_id = _verified_media_common(item, media_ids)
        object_identity = ("local", cast(str, item["objectKey"]))
        if object_identity in object_identities:
            raise MigrationCorruptError("MIGRATION_MEDIA_OWNERSHIP_AMBIGUOUS")
        object_identities.add(object_identity)
        if previous is not None and media_id.encode("utf-8") <= previous:
            raise MigrationCorruptError("MIGRATION_MEDIA_MANIFEST_CORRUPT")
        previous = media_id.encode("utf-8")
        expected_path = (
            f"media/local/{cast(str, item['digest']).removeprefix('sha256:')}"
        )
        if item["memberPath"] != expected_path or expected_path not in member_records:
            raise MigrationCorruptError("MIGRATION_MEDIA_MEMBER_CORRUPT")
        member = member_records[expected_path]
        if (
            item["digest"] != member["sha256"]
            or item["sizeBytes"] != member["sizeBytes"]
        ):
            raise MigrationCorruptError("MIGRATION_MEDIA_MEMBER_CORRUPT")
        declared.add(expected_path)
    previous = None
    for item in remote:
        if (
            not isinstance(item, Mapping)
            or set(item)
            != {
                "mediaId",
                "backendId",
                "objectKey",
                "digest",
                "sizeBytes",
                "physicalIdentity",
                "memoryReferences",
                "strategy",
                "remoteBytesCopied",
            }
            or item.get("strategy") != "SAME_R2"
            or item.get("remoteBytesCopied") is not False
        ):
            raise MigrationCorruptError("MIGRATION_MEDIA_MANIFEST_CORRUPT")
        media_id = _verified_media_common(item, media_ids)
        object_identity = (
            cast(str, item["backendId"]),
            cast(str, item["objectKey"]),
        )
        if object_identity in object_identities:
            raise MigrationCorruptError("MIGRATION_MEDIA_OWNERSHIP_AMBIGUOUS")
        object_identities.add(object_identity)
        if previous is not None and media_id.encode("utf-8") <= previous:
            raise MigrationCorruptError("MIGRATION_MEDIA_MANIFEST_CORRUPT")
        previous = media_id.encode("utf-8")
        try:
            _safe_identifier(item["backendId"])
            identity = item["physicalIdentity"]
            if (
                not isinstance(identity, Mapping)
                or _r2_identity_json(
                    R2PhysicalIdentity(
                        endpoint=cast(str, identity.get("endpoint")),
                        account_identity=cast(str, identity.get("accountIdentity")),
                        bucket=cast(str, identity.get("bucket")),
                    )
                )
                != identity
            ):
                raise MigrationCorruptError("MIGRATION_R2_IDENTITY_CORRUPT")
        except MigrationError:
            raise MigrationCorruptError("MIGRATION_R2_IDENTITY_CORRUPT") from None
    actual = {
        cast(str, record["path"])
        for record in records
        if cast(str, record["path"]).startswith("media/local/")
    }
    if declared != actual:
        raise MigrationCorruptError("MIGRATION_MEDIA_MANIFEST_CORRUPT")


def _verified_media_common(item: Mapping[str, object], media_ids: set[str]) -> str:
    try:
        media_id = _safe_identifier(item["mediaId"])
        _canonical_object_key(item["objectKey"])
        _require_sha256(item["digest"], "MIGRATION_MEDIA_MANIFEST_CORRUPT")
        size = item["sizeBytes"]
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise MigrationCorruptError("MIGRATION_MEDIA_MANIFEST_CORRUPT")
        _sorted_unique_strings(
            cast(Sequence[object], item["memoryReferences"]),
            code="MIGRATION_MEDIA_MANIFEST_CORRUPT",
            require_canonical=True,
        )
    except (KeyError, TypeError, MigrationError):
        raise MigrationCorruptError("MIGRATION_MEDIA_MANIFEST_CORRUPT") from None
    if media_id in media_ids:
        raise MigrationCorruptError("MIGRATION_MEDIA_MANIFEST_CORRUPT")
    media_ids.add(media_id)
    return media_id


def _verify_configuration(value: Mapping[str, object]) -> ConfigurationMode:
    if set(value) != {
        "mode",
        "nonSecret",
        "dispositions",
        "sourcePublicOrigin",
        "effectivePublicOrigin",
        "sourceListen",
        "effectiveListen",
        "targetHostPaths",
        "publicEdgeMutation",
        "activationAllowed",
    }:
        raise MigrationCorruptError("MIGRATION_CONFIGURATION_CORRUPT")
    try:
        mode = ConfigurationMode(cast(str, value["mode"]))
        if _safe_non_secret(value["nonSecret"]) != value["nonSecret"]:
            raise MigrationCorruptError("MIGRATION_CONFIGURATION_CORRUPT")
        source_origin = _canonical_public_origin(value["sourcePublicOrigin"])
        effective_origin = _canonical_public_origin(value["effectivePublicOrigin"])
        source_listen = _listen_from_json(value["sourceListen"])
        effective_listen = _listen_from_json(value["effectiveListen"])
    except (ValueError, MigrationError):
        raise MigrationCorruptError("MIGRATION_CONFIGURATION_CORRUPT") from None
    dispositions = value["dispositions"]
    if not isinstance(dispositions, Mapping) or any(
        not isinstance(key, str)
        or disposition not in {item.value for item in ConfigurationMode}
        for key, disposition in dispositions.items()
    ):
        raise MigrationCorruptError("MIGRATION_CONFIGURATION_CORRUPT")
    if value["publicEdgeMutation"] != "OPERATOR_MANAGED":
        raise MigrationCorruptError("MIGRATION_CONFIGURATION_CORRUPT")
    if value["targetHostPaths"] != {
        "appRoot": str(APP_ROOT),
        "dataRoot": str(DATA_ROOT),
        "managedConfigRoot": str(MANAGED_CONFIG_ROOT),
    }:
        raise MigrationCorruptError("MIGRATION_CONFIGURATION_CORRUPT")
    if mode is ConfigurationMode.PRESERVE and (
        effective_origin != source_origin
        or effective_listen != source_listen
        or value["activationAllowed"] is not True
        or dispositions.get("publicOrigin") != "PRESERVE"
        or dispositions.get("listen") != "PRESERVE"
    ):
        raise MigrationCorruptError("MIGRATION_CONFIGURATION_CORRUPT")
    if mode is ConfigurationMode.TARGET_LOCAL and (
        effective_origin != source_origin
        or not effective_listen.is_loopback
        or value["activationAllowed"] is not False
        or dispositions.get("publicOrigin") != "PRESERVE"
        or dispositions.get("listen") != "TARGET-LOCAL"
    ):
        raise MigrationCorruptError("MIGRATION_CONFIGURATION_CORRUPT")
    if mode is ConfigurationMode.RECONFIGURE and value["activationAllowed"] is not True:
        raise MigrationCorruptError("MIGRATION_CONFIGURATION_CORRUPT")
    if mode is ConfigurationMode.RECONFIGURE:
        public_disposition = dispositions.get("publicOrigin")
        listen_disposition = dispositions.get("listen")
        if (
            public_disposition not in {"PRESERVE", "RECONFIGURE"}
            or listen_disposition not in {"PRESERVE", "RECONFIGURE"}
            or (public_disposition == "PRESERVE" and effective_origin != source_origin)
            or (listen_disposition == "PRESERVE" and effective_listen != source_listen)
        ):
            raise MigrationCorruptError("MIGRATION_CONFIGURATION_CORRUPT")
    expected_dispositions = {
        **_TARGET_LOCAL_DISPOSITIONS,
        "publicOrigin": cast(str, dispositions.get("publicOrigin")),
        "listen": cast(str, dispositions.get("listen")),
    }
    if dispositions != expected_dispositions:
        raise MigrationCorruptError("MIGRATION_CONFIGURATION_CORRUPT")
    return mode


def _verify_source_consistency(value: object) -> str:
    if not isinstance(value, Mapping) or set(value) != {
        "generation",
        "configGeneration",
        "quiesced",
        "writesBlocked",
        "updaterIdle",
        "databaseMigrationIdle",
        "pluginOperationsIdle",
        "mediaWritesIdle",
    }:
        raise MigrationCorruptError("MIGRATION_SOURCE_CONSISTENCY_CORRUPT")
    generation = value.get("generation")
    config_generation = value.get("configGeneration")
    if (
        not isinstance(generation, str)
        or not generation
        or len(generation.encode("utf-8")) > 256
        or _string_is_secret(generation)
        or not isinstance(config_generation, str)
        or not config_generation
        or len(config_generation.encode("utf-8")) > 256
        or _string_is_secret(config_generation)
        or not all(
            value.get(field) is True
            for field in (
                "quiesced",
                "writesBlocked",
                "updaterIdle",
                "databaseMigrationIdle",
                "pluginOperationsIdle",
                "mediaWritesIdle",
            )
        )
    ):
        raise MigrationCorruptError("MIGRATION_SOURCE_CONSISTENCY_CORRUPT")
    return generation


def _verify_envelope(
    root: Path,
    metadata: object,
    *,
    bundle_id: str,
    binding_digest: str,
    records: Sequence[Mapping[str, object]],
) -> None:
    if not isinstance(metadata, Mapping) or set(metadata) != {
        "format",
        "schemaVersion",
        "mode",
        "suiteId",
        "binding",
        "path",
        "sha256",
        "sizeBytes",
        "requiredSecretProfileSatisfied",
    }:
        raise MigrationCorruptError("MIGRATION_ENVELOPE_METADATA_CORRUPT")
    if (
        metadata.get("format") != ENVELOPE_FORMAT
        or metadata.get("schemaVersion") != ENVELOPE_SCHEMA_VERSION
        or metadata.get("suiteId") != SUITE_ID
    ):
        raise MigrationUnsupportedError("MIGRATION_ENVELOPE_UNSUPPORTED")
    envelope_bytes = _read_regular_file(
        root / ENVELOPE_PATH, maximum=MAX_JSON_MEMBER_BYTES
    )
    envelope = _strict_json_object(
        envelope_bytes,
        maximum=MAX_JSON_MEMBER_BYTES,
        code="MIGRATION_ENVELOPE_STRUCTURE_CORRUPT",
    )
    if canonical_json_bytes(envelope) != envelope_bytes or set(envelope) != {
        "aead",
        "binding",
        "ciphertext",
        "ciphertextEncoding",
        "format",
        "kdf",
        "mode",
        "schemaVersion",
        "suiteId",
    }:
        raise MigrationCorruptError("MIGRATION_ENVELOPE_STRUCTURE_CORRUPT")
    expected_binding = {
        "artifactBindingDigest": binding_digest,
        "artifactId": bundle_id,
        "artifactType": "migration-bundle",
    }
    if (
        envelope.get("format") != ENVELOPE_FORMAT
        or envelope.get("schemaVersion") != ENVELOPE_SCHEMA_VERSION
        or envelope.get("suiteId") != SUITE_ID
        or envelope.get("mode") not in {"passphrase", "one-time-key"}
        or envelope.get("binding") != expected_binding
        or envelope.get("ciphertextEncoding") != "base64url"
        or not isinstance(envelope.get("aead"), Mapping)
        or not isinstance(envelope.get("kdf"), Mapping)
        or not isinstance(envelope.get("ciphertext"), str)
    ):
        raise MigrationCorruptError("MIGRATION_ENVELOPE_STRUCTURE_CORRUPT")
    record = next((item for item in records if item.get("path") == ENVELOPE_PATH), None)
    if record is None or metadata != {
        "format": ENVELOPE_FORMAT,
        "schemaVersion": ENVELOPE_SCHEMA_VERSION,
        "mode": envelope["mode"],
        "suiteId": SUITE_ID,
        "binding": expected_binding,
        "path": ENVELOPE_PATH,
        "sha256": record["sha256"],
        "sizeBytes": record["sizeBytes"],
        "requiredSecretProfileSatisfied": True,
    }:
        raise MigrationCorruptError("MIGRATION_ENVELOPE_METADATA_CORRUPT")


def _require_compatible(
    verification: MigrationVerification,
    decision: CompatibilityDecision,
    approved_upgrade_actions: Sequence[UpgradeAction],
) -> tuple[UpgradeAction, ...]:
    if not isinstance(decision, CompatibilityDecision):
        raise MigrationOperationalError("MIGRATION_COMPATIBILITY_DECISION_INVALID")
    try:
        canonical_decision = evaluate_compatibility(
            decision.operation,
            decision.artifact,
            decision.evaluated_dimensions,
            actions=decision.actions,
        )
    except CompatibilityEvaluationError:
        raise MigrationOperationalError(
            "MIGRATION_COMPATIBILITY_DECISION_INVALID"
        ) from None
    if canonical_decision != decision:
        raise MigrationOperationalError("MIGRATION_COMPATIBILITY_DECISION_INVALID")
    if decision.operation is not CompatibilityOperation.MIGRATION:
        raise MigrationOperationalError("MIGRATION_COMPATIBILITY_OPERATION_MISMATCH")
    expected = verification.artifact_identity()
    if decision.artifact != expected:
        raise MigrationOperationalError("MIGRATION_COMPATIBILITY_ARTIFACT_MISMATCH")
    if decision.outcome is CompatibilityOutcome.CORRUPT:
        raise MigrationCorruptError("MIGRATION_COMPATIBILITY_REJECTED")
    if decision.outcome is CompatibilityOutcome.UNSUPPORTED:
        raise MigrationUnsupportedError("MIGRATION_COMPATIBILITY_REJECTED")
    if isinstance(approved_upgrade_actions, (str, bytes, bytearray)) or not isinstance(
        approved_upgrade_actions, Sequence
    ):
        raise MigrationOperationalError("MIGRATION_UPGRADE_APPROVAL_INVALID")
    approved = tuple(approved_upgrade_actions)
    if any(not isinstance(action, UpgradeAction) for action in approved):
        raise MigrationOperationalError("MIGRATION_UPGRADE_APPROVAL_INVALID")
    if decision.outcome is CompatibilityOutcome.REQUIRES_UPGRADE:
        if not approved:
            raise MigrationOperationalError("MIGRATION_UPGRADE_APPROVAL_REQUIRED")
        if approved != decision.actions:
            raise MigrationOperationalError("MIGRATION_UPGRADE_APPROVAL_MISMATCH")
        return approved
    if decision.outcome is not CompatibilityOutcome.COMPATIBLE:
        raise MigrationOperationalError("MIGRATION_COMPATIBILITY_DECISION_INVALID")
    if decision.actions or approved:
        raise MigrationOperationalError("MIGRATION_UPGRADE_APPROVAL_INVALID")
    return ()


def _validate_target_inspection(
    verification: MigrationVerification,
    manifest: Mapping[str, object],
    inspection: TargetInspection,
    *,
    upgrade_actions: tuple[UpgradeAction, ...],
) -> None:
    if not isinstance(inspection, TargetInspection):
        raise MigrationOperationalError("MIGRATION_TARGET_INSPECTION_INVALID")
    if inspection.active_instance_id == verification.instance_id:
        raise MigrationOperationalError("MIGRATION_SPLIT_BRAIN_DETECTED")
    if inspection.active_instance_id is not None:
        raise MigrationOperationalError("MIGRATION_TARGET_NOT_EMPTY")
    if not inspection.canonical_roots:
        raise MigrationUnsupportedError("MIGRATION_TARGET_ROOTS_UNSUPPORTED")
    if not inspection.empty_owned_target:
        raise MigrationOperationalError("MIGRATION_TARGET_NOT_EMPTY")
    authority = (
        inspection.release_identity,
        inspection.deployment_contract,
        inspection.updater_current,
    )
    if any(not isinstance(value, Mapping) for value in authority):
        raise MigrationOperationalError("MIGRATION_TARGET_AUTHORITY_UNAVAILABLE")
    try:
        release_identity = _safe_non_secret(inspection.release_identity)
        deployment_contract = _safe_non_secret(inspection.deployment_contract)
        updater_current = _safe_non_secret(inspection.updater_current)
    except MigrationError:
        raise MigrationOperationalError("MIGRATION_TARGET_AUTHORITY_INVALID") from None
    source = manifest.get("source")
    if not isinstance(source, Mapping) or not isinstance(
        source.get("releaseIdentity"), Mapping
    ):
        raise MigrationCorruptError("MIGRATION_SOURCE_LOCATOR_CORRUPT")
    expected_release: object = source["releaseIdentity"]
    if upgrade_actions:
        expected_release = upgrade_actions[-1].required_release_identity
    if (
        release_identity != expected_release
        or deployment_contract != manifest.get("deploymentContract")
        or updater_current != expected_release
    ):
        raise MigrationUnsupportedError("MIGRATION_TARGET_AUTHORITY_MISMATCH")
    plugins = manifest.get("plugins")
    if not isinstance(plugins, Mapping) or not isinstance(
        plugins.get("packages"), list
    ):
        raise MigrationCorruptError("MIGRATION_PLUGIN_MANIFEST_CORRUPT")
    required_apis = {
        api
        for package in cast(Sequence[Mapping[str, object]], plugins["packages"])
        for api in cast(Sequence[str], package["sdkApis"])
    }
    if not required_apis.issubset(inspection.supported_plugin_sdk_apis):
        raise MigrationUnsupportedError("MIGRATION_PLUGIN_SDK_UNSUPPORTED")
    media = manifest.get("media")
    if not isinstance(media, Mapping) or not isinstance(media.get("r2"), list):
        raise MigrationCorruptError("MIGRATION_MEDIA_MANIFEST_CORRUPT")
    for item in cast(Sequence[Mapping[str, object]], media["r2"]):
        backend_id = cast(str, item["backendId"])
        target_identity = inspection.target_r2_identities.get(backend_id)
        if target_identity is None:
            raise MigrationUnsupportedError("MIGRATION_R2_IDENTITY_INDETERMINATE")
        if _r2_identity_json(target_identity) != item["physicalIdentity"]:
            raise MigrationUnsupportedError("MIGRATION_R2_TRANSFER_REQUIRED")


def _parse_canonical_time(value: object) -> datetime:
    if not isinstance(value, str):
        raise MigrationCorruptError("MIGRATION_TIMESTAMP_CORRUPT")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError:
        raise MigrationCorruptError("MIGRATION_TIMESTAMP_CORRUPT") from None
    if _canonical_time(parsed) != value:
        raise MigrationCorruptError("MIGRATION_TIMESTAMP_CORRUPT")
    return parsed


def _parse_checksum_bytes(value: bytes) -> dict[str, str]:
    try:
        text = value.decode("utf-8")
    except UnicodeError:
        raise MigrationCorruptError("MIGRATION_CHECKSUM_SET_CORRUPT") from None
    if not text or not text.endswith("\n") or "\r" in text:
        raise MigrationCorruptError("MIGRATION_CHECKSUM_SET_CORRUPT")
    result: dict[str, str] = {}
    previous: bytes | None = None
    for line in text.splitlines():
        match = _CHECKSUM_LINE_RE.fullmatch(line)
        if match is None:
            raise MigrationCorruptError("MIGRATION_CHECKSUM_SET_CORRUPT")
        relative = _canonical_relative_path(match.group(2))
        _validate_member_path(relative)
        ordering = relative.encode("utf-8")
        if previous is not None and ordering <= previous:
            raise MigrationCorruptError("MIGRATION_CHECKSUM_SET_CORRUPT")
        result[relative] = f"sha256:{match.group(1)}"
        previous = ordering
    if len(result) > MAX_MEMBER_COUNT:
        raise MigrationCorruptError("MIGRATION_CHECKSUM_SET_CORRUPT")
    return result


def _enumerate_bundle_files(root: Path) -> dict[str, Path]:
    files: dict[str, Path] = {}
    normalized: set[str] = set()
    copy_counter = CopyByteCounter(MAX_TOTAL_COPIED_BYTES)
    try:
        items = root.rglob("*")
        for item in items:
            relative = item.relative_to(root).as_posix()
            if _is_link_or_reparse(item):
                raise MigrationCorruptError("MIGRATION_MEMBER_LINK_FORBIDDEN")
            item_stat = item.lstat()
            if stat.S_ISDIR(item_stat.st_mode):
                if not _directory_allowed(relative):
                    raise MigrationCorruptError("MIGRATION_MEMBER_UNEXPECTED")
                if os.name != "nt" and stat.S_IMODE(item_stat.st_mode) & 0o077:
                    raise MigrationCorruptError("MIGRATION_MEMBER_PERMISSIONS_INVALID")
                continue
            maximum_member_bytes = (
                MAX_COMPRESSED_MEMBER_BYTES
                if relative == DATABASE_MEMBER
                else MAX_MEMBER_BYTES
            )
            if (
                not stat.S_ISREG(item_stat.st_mode)
                or item_stat.st_nlink != 1
                or _is_sparse(item_stat)
            ):
                raise MigrationCorruptError("MIGRATION_MEMBER_UNSAFE")
            try:
                preflight_copy_sizes(
                    (item_stat.st_size,),
                    maximum_member_bytes=maximum_member_bytes,
                    maximum_total_bytes=(
                        copy_counter.maximum_bytes - copy_counter.copied
                    ),
                    member_reason=(
                        ResourceLimitReason.COMPRESSED_MEMBER_BYTES
                        if relative == DATABASE_MEMBER
                        else ResourceLimitReason.FILESYSTEM_MEMBER_BYTES
                    ),
                )
                copy_counter.consume(item_stat.st_size)
            except ResourceLimitExceeded:
                raise MigrationCorruptError(
                    "MIGRATION_RESOURCE_BOUNDS_EXCEEDED"
                ) from None
            if os.name != "nt" and stat.S_IMODE(item_stat.st_mode) & 0o077:
                raise MigrationCorruptError("MIGRATION_MEMBER_PERMISSIONS_INVALID")
            canonical = _canonical_relative_path(relative)
            _validate_member_path(canonical, controls=True)
            collision = unicodedata.normalize("NFC", canonical).casefold()
            if collision in normalized:
                raise MigrationCorruptError("MIGRATION_MEMBER_PATH_COLLISION")
            normalized.add(collision)
            files[canonical] = item
            if len(files) > MAX_MEMBER_COUNT + 2:
                raise MigrationCorruptError("MIGRATION_MEMBER_COUNT_EXCEEDED")
    except MigrationError:
        raise
    except OSError:
        raise MigrationOperationalError("MIGRATION_BUNDLE_UNAVAILABLE") from None
    return files


def _canonical_relative_path(value: object) -> str:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise MigrationCorruptError("MIGRATION_MEMBER_PATH_INVALID")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise MigrationCorruptError("MIGRATION_MEMBER_PATH_INVALID")
    canonical = path.as_posix()
    if (
        canonical != value
        or len(canonical.encode("utf-8")) > 4096
        or any(len(part.encode("utf-8")) > 255 for part in path.parts)
    ):
        raise MigrationCorruptError("MIGRATION_MEMBER_PATH_INVALID")
    return canonical


def _validate_member_path(relative: str, *, controls: bool = False) -> None:
    if controls and relative in {MANIFEST_NAME, CHECKSUMS_NAME}:
        return
    if relative in _CORE_MEMBERS:
        return
    if relative.startswith("plugins/cas/") and re.fullmatch(
        r"plugins/cas/[0-9a-f]{64}\.ajplugin", relative
    ):
        return
    if relative.startswith("media/local/") and re.fullmatch(
        r"media/local/[0-9a-f]{64}", relative
    ):
        return
    raise MigrationCorruptError("MIGRATION_MEMBER_UNEXPECTED")


def _directory_allowed(relative: str) -> bool:
    return relative in {
        "plugins",
        "plugins/cas",
        "media",
        "media/local",
        "config",
        "secrets",
        "private",
        "updater",
    }


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def _is_link_or_reparse(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return False
    if stat.S_ISLNK(metadata.st_mode):
        return True
    attributes = getattr(metadata, "st_file_attributes", 0)
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def _path_has_link_component(path: Path) -> bool:
    candidate = Path(path).absolute()
    while True:
        if _is_link_or_reparse(candidate):
            return True
        if candidate.parent == candidate:
            return False
        candidate = candidate.parent


def _is_sparse(metadata: os.stat_result) -> bool:
    blocks = getattr(metadata, "st_blocks", None)
    return (
        isinstance(blocks, int)
        and metadata.st_size > 0
        and blocks * 512 < metadata.st_size
    )


def _paths_overlap(first: Path, second: Path) -> bool:
    return first == second or first in second.parents or second in first.parents


def _require_sha256(value: object, code: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise MigrationCorruptError(code)
    return value


def _safe_identifier(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value.encode("utf-8")) > 256
        or any(character in value for character in "\r\n\x00/\\")
        or _string_is_secret(value)
    ):
        raise MigrationOperationalError("MIGRATION_IDENTIFIER_INVALID")
    return value


def _sorted_unique_strings(
    values: Sequence[object],
    *,
    code: str,
    require_canonical: bool = False,
) -> list[str]:
    if isinstance(values, (str, bytes, bytearray)) or not isinstance(values, Sequence):
        raise MigrationOperationalError(code)
    result: list[str] = []
    for value in values:
        if (
            not isinstance(value, str)
            or not value
            or len(value.encode("utf-8")) > 512
            or any(character in value for character in "\r\n\x00")
            or _string_is_secret(value)
        ):
            raise MigrationOperationalError(code)
        result.append(value)
    canonical = sorted(set(result), key=lambda item: item.encode("utf-8"))
    if len(canonical) != len(result) or (require_canonical and result != canonical):
        raise MigrationOperationalError(code)
    return canonical


def _ordered_unique_strings(values: Sequence[object], *, code: str) -> list[str]:
    if isinstance(values, (str, bytes, bytearray)) or not isinstance(values, Sequence):
        raise MigrationOperationalError(code)
    result: list[str] = []
    for value in values:
        if (
            not isinstance(value, str)
            or not value
            or len(value.encode("utf-8")) > 512
            or any(character in value for character in "\r\n\x00")
            or _string_is_secret(value)
            or value in result
        ):
            raise MigrationOperationalError(code)
        result.append(value)
    return result


def _canonical_object_key(value: object) -> str:
    try:
        return _canonical_relative_path(value)
    except MigrationError:
        raise MigrationOperationalError("MIGRATION_OBJECT_KEY_INVALID") from None


def _r2_identity_json(identity: object) -> dict[str, str]:
    if not isinstance(identity, R2PhysicalIdentity):
        raise MigrationUnsupportedError("MIGRATION_R2_IDENTITY_INDETERMINATE")
    if not all(
        isinstance(value, str) and value.strip()
        for value in (identity.endpoint, identity.account_identity, identity.bucket)
    ):
        raise MigrationUnsupportedError("MIGRATION_R2_IDENTITY_INDETERMINATE")
    try:
        parsed = urlsplit(identity.endpoint.strip())
        port = parsed.port
    except ValueError:
        raise MigrationUnsupportedError("MIGRATION_R2_IDENTITY_INDETERMINATE") from None
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise MigrationUnsupportedError("MIGRATION_R2_IDENTITY_INDETERMINATE")
    scheme = parsed.scheme.lower()
    host = parsed.hostname.lower()
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    default_port = (scheme == "https" and port == 443) or (
        scheme == "http" and port == 80
    )
    endpoint = f"{scheme}://{host}{'' if port is None or default_port else f':{port}'}"
    account = identity.account_identity.strip().lower()
    bucket = identity.bucket.strip().lower()
    if (
        len(account.encode("utf-8")) > 256
        or len(bucket.encode("utf-8")) > 256
        or any(character in account + bucket for character in "\r\n\x00/\\")
    ):
        raise MigrationUnsupportedError("MIGRATION_R2_IDENTITY_INDETERMINATE")
    return {
        "backendType": "r2",
        "endpoint": endpoint,
        "accountIdentity": account,
        "bucket": bucket,
    }


def _listen_from_json(value: object) -> ListenIdentity:
    if not isinstance(value, Mapping) or set(value) != {"host", "port"}:
        raise MigrationOperationalError("MIGRATION_LISTEN_INVALID")
    listen = ListenIdentity(
        host=cast(str, value["host"]), port=cast(int, value["port"])
    )
    _listen_json(listen)
    return listen
