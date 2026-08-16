"""AniMemo Restore Runtime v1 domain orchestration.

The runtime consumes only a finalized Backup Format v1 artifact and only a
canonical fresh or verified-empty destination.  Release acquisition, native
Updater state adoption, target publication, and application validation remain
explicit fail-closed ports; this module never fabricates their authority.
"""

from __future__ import annotations

import gzip
import json
import os
import shutil
import stat
import subprocess
import tempfile
import uuid
from collections.abc import Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, ExitStack, contextmanager
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Protocol

from . import backup
from .canonical import canonical_json_bytes, sha256_identity
from .compatibility import (
    EVALUATION_ORDER,
    CompatibilityDecision,
    CompatibilityEvaluationError,
    CompatibilityOutcome,
    Dimension,
    DimensionAssessment,
    ReasonCode,
    UpgradeAction,
    evaluate_compatibility,
)
from .secret_envelope import (
    OneTimeKey,
    Passphrase,
    SecretEnvelopeCorruptError,
    SecretEnvelopeOperationalError,
    SecretEnvelopeUnsupportedError,
    open_secret_envelope,
)

RESTORE_PLAN_IDENTITY = "animemo.restore-plan/v1"
CANONICAL_ROOTS = (
    "/opt/animemo",
    "/data/animemo",
    "/opt/animemo-updater",
    "/var/lib/animemo-updater",
    "/run/animemo-updater",
)
CANONICAL_BACKUP_ROOTS = (
    "filesystem/config",
    "filesystem/plugins/cas",
    "filesystem/plugins/durable",
    "filesystem/media",
    "filesystem/private",
    "updater-state",
)
REQUIRED_VALIDATIONS = (
    "database.usable",
    "database.schema_contract",
    "instance.identity",
    "filesystem.layout",
    "protection.decryptability",
    "service.api.health",
    "service.web.health",
    "updater.state",
    "release.identity",
    "plugins.integrity",
    "media.integrity",
    "runtime.rebuilt",
    "authentication.epoch",
    "durable.write",
    "public_origin.listen",
    "memory.mi1.external_metadata",
    "memory.mi2.provider_identity",
    "memory.mi3.merge_history",
    "memory.mi4.unsupported_payload",
    "memory.mi5.destructive_ambiguity",
)
_EXECUTION_STEPS = (
    "release.acquire",
    "target.begin",
    "release.stage",
    "protection.stage",
    "database.prepare",
    "database.restore",
    "filesystem.restore",
    "updater.stage",
    "upgrade.apply-if-required",
    "bootstrap",
    "runtime.rebuild",
    "locator.build",
    "authentication.rotate",
    "validate",
    "target.publish",
)


class DestinationClass(StrEnum):
    FRESH = "FRESH"
    EXISTING_EMPTY = "EXISTING_EMPTY"
    EXISTING_INSTANCE = "EXISTING_INSTANCE"
    FOREIGN = "FOREIGN"
    PARTIAL_AMBIGUOUS = "PARTIAL_AMBIGUOUS"


class RestoreTerminalState(StrEnum):
    PUBLISHED = "PUBLISHED"
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"


class RestoreError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class RestorePreflightError(RestoreError):
    def __init__(
        self,
        code: str,
        *,
        compatibility_outcome: CompatibilityOutcome | None = None,
    ) -> None:
        self.compatibility_outcome = compatibility_outcome
        super().__init__(code)


class RestoreAdapterError(RestoreError):
    """A port failure containing only a stable non-secret machine code."""


class RestoreRecoveryPersistenceError(RestoreError):
    """Recovery evidence exists but its durable journal could not be written."""

    def __init__(self, recovery_evidence: RecoveryEvidence) -> None:
        self.recovery_evidence = recovery_evidence
        super().__init__("RESTORE_RECOVERY_EVIDENCE_FAILED")


@dataclass(frozen=True)
class DestinationSnapshot:
    classification: DestinationClass
    deployment_profile: str
    canonical_roots: tuple[str, ...]
    ownership_verified: bool
    empty_verified: bool
    parent_ready: bool
    evidence_digest: str

    def as_dict(self) -> dict[str, object]:
        return {
            "classification": self.classification.value,
            "deploymentProfile": self.deployment_profile,
            "canonicalRoots": list(self.canonical_roots),
            "ownershipVerified": self.ownership_verified,
            "emptyVerified": self.empty_verified,
            "parentReady": self.parent_ready,
            "evidenceDigest": self.evidence_digest,
        }


@dataclass(frozen=True)
class ReleaseEvidence:
    release_identity_digest: str
    deployment_identity_digest: str

    def as_dict(self) -> dict[str, object]:
        return {
            "releaseIdentityDigest": self.release_identity_digest,
            "deploymentIdentityDigest": self.deployment_identity_digest,
        }


@dataclass(frozen=True)
class UpdaterEvidence:
    state_identity_digest: str
    pending_state_preserved: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "stateIdentityDigest": self.state_identity_digest,
            "pendingStatePreserved": self.pending_state_preserved,
        }


@dataclass(frozen=True)
class SecretResolution:
    mode: str
    status: str
    handle: object = field(repr=False, compare=False)

    def as_dict(self) -> dict[str, object]:
        return {"mode": self.mode, "status": self.status}


@dataclass(frozen=True)
class RestoreCompatibilityEvidence:
    dimensions: tuple[DimensionAssessment, ...]
    actions: tuple[UpgradeAction, ...] = ()


@dataclass(frozen=True)
class ValidationReport:
    passed_checks: tuple[str, ...]
    evidence_digest: str


@dataclass(frozen=True)
class RestorePlan:
    operation_id: str
    backup_id: str
    instance_id: str
    checksum_set_digest: str
    artifact_binding_digest: str
    artifact_manifest_digest: str
    destination: DestinationSnapshot
    release: ReleaseEvidence
    updater: UpdaterEvidence
    protection: Mapping[str, object]
    decision: CompatibilityDecision
    member_paths: tuple[str, ...]
    plan_digest: str

    def _body(self) -> dict[str, object]:
        return {
            "planIdentity": RESTORE_PLAN_IDENTITY,
            "operationId": self.operation_id,
            "backupId": self.backup_id,
            "sourceInstanceId": self.instance_id,
            "checksumSetDigest": self.checksum_set_digest,
            "artifactBindingDigest": self.artifact_binding_digest,
            "artifactManifestDigest": self.artifact_manifest_digest,
            "destination": self.destination.as_dict(),
            "release": self.release.as_dict(),
            "updater": self.updater.as_dict(),
            "protection": dict(self.protection),
            "compatibility": self.decision.as_dict(),
            "payloadMembers": list(self.member_paths),
            "executionSteps": list(_EXECUTION_STEPS),
            "requiredValidations": list(REQUIRED_VALIDATIONS),
            "operatorConfirmations": [
                "ACCEPT_EXACT_PLAN_DIGEST",
                "ACCEPT_FORWARD_UPGRADE_ACTIONS"
                if self.decision.outcome is CompatibilityOutcome.REQUIRES_UPGRADE
                else "NO_UPGRADE_ACTIONS",
            ],
        }

    def as_dict(self) -> dict[str, object]:
        result = self._body()
        result["planDigest"] = self.plan_digest
        return result


@dataclass(frozen=True)
class RecoveryEvidence:
    operation_id: str
    backup_id: str
    plan_digest: str
    completed_steps: tuple[str, ...]
    failed_step: str
    error_code: str
    target_active: bool | None = False

    def as_dict(self) -> dict[str, object]:
        return {
            "state": RestoreTerminalState.RECOVERY_REQUIRED.value,
            "operationId": self.operation_id,
            "backupId": self.backup_id,
            "planDigest": self.plan_digest,
            "completedSteps": list(self.completed_steps),
            "failedStep": self.failed_step,
            "errorCode": self.error_code,
            "targetActive": self.target_active,
        }


@dataclass(frozen=True)
class RestoreResult:
    state: RestoreTerminalState
    operation_id: str
    backup_id: str
    plan_digest: str
    completed_steps: tuple[str, ...]
    recovery_evidence: RecoveryEvidence | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "state": self.state.value,
            "operationId": self.operation_id,
            "backupId": self.backup_id,
            "planDigest": self.plan_digest,
            "completedSteps": list(self.completed_steps),
            "recoveryEvidence": self.recovery_evidence.as_dict()
            if self.recovery_evidence
            else None,
        }


class DestinationPort(Protocol):
    def inspect(self) -> DestinationSnapshot: ...


class ReleasePort(Protocol):
    def verify(self, manifest: Mapping[str, object]) -> ReleaseEvidence: ...

    def acquire(self, evidence: ReleaseEvidence) -> object: ...


class UpdaterPort(Protocol):
    def verify(
        self,
        manifest: Mapping[str, object],
        release_evidence: ReleaseEvidence,
    ) -> UpdaterEvidence: ...

    def stage(
        self,
        manifest: Mapping[str, object],
        evidence: UpdaterEvidence,
        mutation: MutationPort,
    ) -> None: ...


class SecretResolver(Protocol):
    def authenticate(
        self,
        backup_root: Path,
        manifest: Mapping[str, object],
    ) -> SecretResolution: ...


class CompatibilityPort(Protocol):
    def assess(
        self,
        manifest: Mapping[str, object],
        destination: DestinationSnapshot,
        release_evidence: ReleaseEvidence,
        updater_evidence: UpdaterEvidence,
    ) -> RestoreCompatibilityEvidence: ...


class DatabasePort(Protocol):
    def restore(self, dump_path: Path) -> None: ...


class MutationPort(Protocol):
    def acquire_lock(self, operation_id: str) -> AbstractContextManager[None]: ...

    def begin(self, plan: RestorePlan) -> None: ...

    def stage_release(
        self, release_material: object, evidence: ReleaseEvidence
    ) -> None: ...

    def stage_secret(self, resolution: SecretResolution) -> None: ...

    def prepare_database(self) -> None: ...

    def restore_filesystem(
        self, backup_root: Path, member_paths: tuple[str, ...]
    ) -> None: ...

    def apply_upgrade(self, actions: tuple[UpgradeAction, ...]) -> None: ...

    def bootstrap(self) -> None: ...

    def rebuild_runtime(self) -> None: ...

    def build_locator(
        self, instance_id: str, release_evidence: ReleaseEvidence
    ) -> None: ...

    def rotate_authentication_epoch(self) -> None: ...

    def publish(self) -> None: ...

    def record_recovery_required(self, evidence: RecoveryEvidence) -> None: ...


class ValidationPort(Protocol):
    def validate(
        self,
        manifest: Mapping[str, object],
        plan: RestorePlan,
        mutation: MutationPort,
    ) -> ValidationReport: ...


@dataclass(frozen=True)
class RestoreRequest:
    operation_id: str
    backup_root: Path
    destination: DestinationPort
    release: ReleasePort
    updater: UpdaterPort
    secret_resolver: SecretResolver
    compatibility: CompatibilityPort
    database: DatabasePort
    mutation: MutationPort
    validator: ValidationPort


class EnvelopeSecretResolver:
    def __init__(self, external_secret: Passphrase | OneTimeKey) -> None:
        self._external_secret = external_secret

    def authenticate(
        self,
        backup_root: Path,
        manifest: Mapping[str, object],
    ) -> SecretResolution:
        secrets = manifest.get("secrets")
        if not isinstance(secrets, Mapping) or secrets.get("mode") != "envelope":
            raise RestorePreflightError("RESTORE_PROTECTION_MODE_MISMATCH")
        binding_record = manifest.get("artifactBindingRecord")
        source = manifest.get("source")
        if not isinstance(binding_record, Mapping) or not isinstance(source, Mapping):
            raise RestorePreflightError(
                "RESTORE_BACKUP_IDENTITY_INVALID",
                compatibility_outcome=CompatibilityOutcome.CORRUPT,
            )
        try:
            encoded = (backup_root / "secrets" / "secret-envelope.json").read_bytes()
            opened = open_secret_envelope(
                encoded,
                external_secret=self._external_secret,
                expected_artifact_type="backup",
                expected_artifact_id=str(manifest.get("backupId", "")),
                expected_artifact_binding_record=binding_record,
                expected_source_instance_id=str(source.get("instanceId", "")),
            )
        except SecretEnvelopeUnsupportedError as error:
            raise RestorePreflightError(
                error.code,
                compatibility_outcome=CompatibilityOutcome.UNSUPPORTED,
            ) from None
        except SecretEnvelopeCorruptError as error:
            raise RestorePreflightError(
                error.code,
                compatibility_outcome=CompatibilityOutcome.CORRUPT,
            ) from None
        except SecretEnvelopeOperationalError as error:
            raise RestorePreflightError(error.code) from None
        except OSError:
            raise RestorePreflightError("RESTORE_PROTECTION_UNAVAILABLE") from None
        return SecretResolution("envelope", "AUTHENTICATED", opened)


class NoneSecretResolver:
    def authenticate(
        self,
        backup_root: Path,
        manifest: Mapping[str, object],
    ) -> SecretResolution:
        del backup_root
        secrets = manifest.get("secrets")
        if not isinstance(secrets, Mapping) or secrets.get("mode") != "none":
            raise RestorePreflightError("RESTORE_PROTECTION_MODE_MISMATCH")
        return SecretResolution("none", "NOT_REQUIRED", None)


class ExternalReferencePort(Protocol):
    def resolve(self, reference: Mapping[str, object]) -> object: ...


class ReferenceSecretResolver:
    def __init__(self, resolver: ExternalReferencePort) -> None:
        self._resolver = resolver

    def authenticate(
        self,
        backup_root: Path,
        manifest: Mapping[str, object],
    ) -> SecretResolution:
        secrets = manifest.get("secrets")
        if not isinstance(secrets, Mapping) or secrets.get("mode") != "reference":
            raise RestorePreflightError("RESTORE_PROTECTION_MODE_MISMATCH")
        try:
            encoded = (backup_root / "secrets" / "secret-reference.json").read_bytes()
            reference = json.loads(encoded)
            if not isinstance(reference, Mapping):
                raise TypeError
            handle = self._resolver.resolve(reference)
        except RestoreError:
            raise
        except Exception:  # noqa: BLE001 - external resolver failures are redacted
            raise RestorePreflightError("RESTORE_PROTECTION_UNAVAILABLE") from None
        return SecretResolution("reference", "RESOLVABLE", handle)


@dataclass(frozen=True)
class _PreparedSnapshot:
    plan: RestorePlan
    manifest: Mapping[str, object]
    protection: SecretResolution


def prepare_restore(request: RestoreRequest) -> RestorePlan:
    """Perform VERIFY and COMPATIBILITY PLAN without target mutation."""

    return _prepare_snapshot(request).plan


def _prepare_snapshot(request: RestoreRequest) -> _PreparedSnapshot:
    operation_id = _canonical_uuid(request.operation_id, "RESTORE_OPERATION_ID_INVALID")
    backup_root = Path(request.backup_root)
    try:
        if not backup_root.exists():
            raise RestorePreflightError("RESTORE_BACKUP_UNAVAILABLE")
        verification = backup.verify_backup(backup_root)
    except backup.UnsupportedBackupFormat:
        raise RestorePreflightError(
            "RESTORE_FORMAT_UNSUPPORTED",
            compatibility_outcome=CompatibilityOutcome.UNSUPPORTED,
        ) from None
    except RestorePreflightError:
        raise
    except backup.BackupError as error:
        if error.code == "BACKUP_IO_FAILED":
            raise RestorePreflightError("RESTORE_BACKUP_UNAVAILABLE") from None
        raise RestorePreflightError(
            "RESTORE_BACKUP_CORRUPT",
            compatibility_outcome=CompatibilityOutcome.CORRUPT,
        ) from None
    except OSError:
        raise RestorePreflightError("RESTORE_BACKUP_UNAVAILABLE") from None
    manifest = _load_verified_manifest(backup_root)
    destination = _inspect_destination(request.destination)
    protection = request.secret_resolver.authenticate(backup_root, manifest)
    _validate_protection(manifest, protection)
    try:
        release_evidence = request.release.verify(manifest)
        _validate_release_evidence(release_evidence)
        updater_evidence = request.updater.verify(manifest, release_evidence)
        _validate_updater_evidence(updater_evidence)
        supplied = request.compatibility.assess(
            manifest,
            destination,
            release_evidence,
            updater_evidence,
        )
    except RestorePreflightError:
        raise
    except Exception:  # noqa: BLE001 - fail-closed authority adapter boundary
        raise RestorePreflightError("RESTORE_AUTHORITY_EVALUATION_FAILED") from None
    if not isinstance(supplied, RestoreCompatibilityEvidence):
        raise RestorePreflightError("RESTORE_COMPATIBILITY_EVIDENCE_INVALID")
    expected_dimensions = EVALUATION_ORDER[2:]
    if tuple(item.name for item in supplied.dimensions) != expected_dimensions:
        raise RestorePreflightError("RESTORE_COMPATIBILITY_EVIDENCE_INVALID")
    format_dimension = DimensionAssessment(
        name=Dimension.FORMAT,
        outcome=CompatibilityOutcome.COMPATIBLE,
        reason_code=ReasonCode.FORMAT_SUPPORTED,
        source={
            "format": backup.FORMAT,
            "schemaVersion": backup.SCHEMA_VERSION,
            "backupId": verification.backup_id,
        },
        target={"format": backup.FORMAT, "schemaVersion": backup.SCHEMA_VERSION},
    )
    integrity_dimension = DimensionAssessment(
        name=Dimension.INTEGRITY_AUTHENTICATION,
        outcome=CompatibilityOutcome.COMPATIBLE,
        reason_code=ReasonCode.INTEGRITY_AUTHENTICATED,
        source={
            "checksumSetDigest": verification.checksum_set_digest,
            "artifactBindingDigest": str(manifest.get("artifactBindingDigest", "")),
            "protectionMode": protection.mode,
        },
        target={"verificationStatus": "STRUCTURALLY_VERIFIED"},
    )
    try:
        decision = evaluate_compatibility(
            "restore",
            verification.as_compatibility_artifact(),
            (format_dimension, integrity_dimension, *supplied.dimensions),
            actions=supplied.actions,
        )
    except CompatibilityEvaluationError:
        raise RestorePreflightError("RESTORE_COMPATIBILITY_EVIDENCE_INVALID") from None
    source = manifest.get("source")
    if not isinstance(source, Mapping):
        raise RestorePreflightError(
            "RESTORE_BACKUP_IDENTITY_INVALID",
            compatibility_outcome=CompatibilityOutcome.CORRUPT,
        )
    instance_id = _canonical_uuid(
        source.get("instanceId"),
        "RESTORE_BACKUP_IDENTITY_INVALID",
    )
    binding_digest = _canonical_digest(
        manifest.get("artifactBindingDigest"),
        "RESTORE_BACKUP_IDENTITY_INVALID",
    )
    member_paths = _payload_members(manifest)
    body = {
        "planIdentity": RESTORE_PLAN_IDENTITY,
        "operationId": operation_id,
        "backupId": verification.backup_id,
        "sourceInstanceId": instance_id,
        "checksumSetDigest": verification.checksum_set_digest,
        "artifactBindingDigest": binding_digest,
        "artifactManifestDigest": verification.manifest_digest,
        "destination": destination.as_dict(),
        "release": release_evidence.as_dict(),
        "updater": updater_evidence.as_dict(),
        "protection": protection.as_dict(),
        "compatibility": decision.as_dict(),
        "payloadMembers": list(member_paths),
        "executionSteps": list(_EXECUTION_STEPS),
        "requiredValidations": list(REQUIRED_VALIDATIONS),
        "operatorConfirmations": [
            "ACCEPT_EXACT_PLAN_DIGEST",
            "ACCEPT_FORWARD_UPGRADE_ACTIONS"
            if decision.outcome is CompatibilityOutcome.REQUIRES_UPGRADE
            else "NO_UPGRADE_ACTIONS",
        ],
    }
    plan_digest = sha256_identity(canonical_json_bytes(body))
    plan = RestorePlan(
        operation_id=operation_id,
        backup_id=verification.backup_id,
        instance_id=instance_id,
        checksum_set_digest=verification.checksum_set_digest,
        artifact_binding_digest=binding_digest,
        artifact_manifest_digest=verification.manifest_digest,
        destination=destination,
        release=release_evidence,
        updater=updater_evidence,
        protection=protection.as_dict(),
        decision=decision,
        member_paths=member_paths,
        plan_digest=plan_digest,
    )
    if plan._body() != body:
        raise RestorePreflightError("RESTORE_PLAN_INVALID")
    return _PreparedSnapshot(plan, manifest, protection)


def execute_restore(
    request: RestoreRequest,
    plan: RestorePlan,
    *,
    accepted_plan_digest: str,
    accept_upgrade: bool = False,
) -> RestoreResult:
    """Reverify the exact plan, then run RESTORE and VALIDATE."""

    if not isinstance(plan, RestorePlan) or accepted_plan_digest != plan.plan_digest:
        raise RestorePreflightError("RESTORE_PLAN_NOT_ACCEPTED")
    if sha256_identity(canonical_json_bytes(plan._body())) != plan.plan_digest:
        raise RestorePreflightError("RESTORE_PLAN_INVALID")
    if plan.decision.outcome in {
        CompatibilityOutcome.UNSUPPORTED,
        CompatibilityOutcome.CORRUPT,
    }:
        raise RestorePreflightError(
            "RESTORE_COMPATIBILITY_REJECTED",
            compatibility_outcome=plan.decision.outcome,
        )
    if (
        plan.decision.outcome is CompatibilityOutcome.REQUIRES_UPGRADE
        and not accept_upgrade
    ):
        raise RestorePreflightError("RESTORE_UPGRADE_NOT_ACCEPTED")
    with _operation_backup_snapshot(
        Path(request.backup_root), plan.operation_id
    ) as snapshot_root:
        snapshot_request = replace(request, backup_root=snapshot_root)
        return _execute_verified_snapshot(snapshot_request, plan)


def _execute_verified_snapshot(
    request: RestoreRequest,
    plan: RestorePlan,
) -> RestoreResult:
    """Execute exclusively from one copied, verified operation snapshot."""

    prepared = _prepare_snapshot(request)
    if prepared.plan.plan_digest != plan.plan_digest:
        raise RestorePreflightError("RESTORE_PLAN_STALE")
    try:
        release_material = request.release.acquire(plan.release)
    except Exception:  # noqa: BLE001 - release adapter failures are redacted
        raise RestorePreflightError("RESTORE_RELEASE_ACQUISITION_FAILED") from None
    completed: list[str] = ["release.acquire"]
    failed_step = "lock.acquire"
    mutation_started = False
    with ExitStack() as lock_stack:
        try:
            lock_stack.enter_context(request.mutation.acquire_lock(plan.operation_id))
        except Exception:  # noqa: BLE001 - lock adapters are an operational boundary
            raise RestorePreflightError("RESTORE_LOCK_ACQUISITION_FAILED") from None
        current_destination = _inspect_destination(request.destination)
        if current_destination.as_dict() != plan.destination.as_dict():
            raise RestorePreflightError("RESTORE_DESTINATION_CHANGED")
        try:
            failed_step = "target.begin"
            mutation_started = True
            request.mutation.begin(plan)
            completed.append(failed_step)

            failed_step = "release.stage"
            request.mutation.stage_release(release_material, plan.release)
            completed.append(failed_step)

            failed_step = "protection.stage"
            request.mutation.stage_secret(prepared.protection)
            completed.append(failed_step)

            failed_step = "database.prepare"
            request.mutation.prepare_database()
            completed.append(failed_step)

            failed_step = "database.restore"
            request.database.restore(Path(request.backup_root) / backup.DATABASE_MEMBER)
            completed.append(failed_step)

            failed_step = "filesystem.restore"
            request.mutation.restore_filesystem(
                Path(request.backup_root),
                plan.member_paths,
            )
            completed.append(failed_step)

            failed_step = "updater.stage"
            request.updater.stage(prepared.manifest, plan.updater, request.mutation)
            completed.append(failed_step)

            if plan.decision.actions:
                failed_step = "upgrade.apply"
                request.mutation.apply_upgrade(plan.decision.actions)
                completed.append(failed_step)

            failed_step = "bootstrap"
            request.mutation.bootstrap()
            completed.append(failed_step)

            failed_step = "runtime.rebuild"
            request.mutation.rebuild_runtime()
            completed.append(failed_step)

            failed_step = "locator.build"
            request.mutation.build_locator(plan.instance_id, plan.release)
            completed.append(failed_step)

            failed_step = "authentication.rotate"
            request.mutation.rotate_authentication_epoch()
            completed.append(failed_step)

            failed_step = "validate"
            validation = request.validator.validate(
                prepared.manifest,
                plan,
                request.mutation,
            )
            _require_complete_validation(validation)
            completed.append(failed_step)

            failed_step = "target.publish"
            request.mutation.publish()
            completed.append(failed_step)
        except Exception as error:  # noqa: BLE001 - mutation ports must yield recovery evidence
            if not mutation_started:
                raise RestorePreflightError("RESTORE_PRE_MUTATION_FAILED") from None
            code = (
                error.code
                if isinstance(error, RestoreError)
                else _step_error_code(failed_step)
            )
            recovery = RecoveryEvidence(
                operation_id=plan.operation_id,
                backup_id=plan.backup_id,
                plan_digest=plan.plan_digest,
                completed_steps=tuple(completed),
                failed_step=failed_step,
                error_code=code,
                target_active=None if failed_step == "target.publish" else False,
            )
            try:
                request.mutation.record_recovery_required(recovery)
            except Exception:  # noqa: BLE001 - never disclose journal adapter internals
                raise RestoreRecoveryPersistenceError(recovery) from None
            return RestoreResult(
                state=RestoreTerminalState.RECOVERY_REQUIRED,
                operation_id=plan.operation_id,
                backup_id=plan.backup_id,
                plan_digest=plan.plan_digest,
                completed_steps=tuple(completed),
                recovery_evidence=recovery,
            )
    return RestoreResult(
        state=RestoreTerminalState.PUBLISHED,
        operation_id=plan.operation_id,
        backup_id=plan.backup_id,
        plan_digest=plan.plan_digest,
        completed_steps=tuple(completed),
    )


@contextmanager
def _operation_backup_snapshot(
    source_root: Path,
    operation_id: str,
) -> Iterator[Path]:
    """Copy Backup bytes into a private lifetime-bound operation directory."""

    with tempfile.TemporaryDirectory(
        prefix=f".animemo-restore-{operation_id}-"
    ) as directory:
        workspace = Path(directory)
        source = Path(source_root).absolute()
        snapshot_root = workspace / source.name
        try:
            os.chmod(workspace, 0o700)
            if _is_link_or_reparse(source):
                raise RestorePreflightError(
                    "RESTORE_BACKUP_CORRUPT",
                    compatibility_outcome=CompatibilityOutcome.CORRUPT,
                )
            shutil.copytree(
                source,
                snapshot_root,
                symlinks=True,
                copy_function=_copy_snapshot_regular,
            )
            os.chmod(snapshot_root, 0o700)
        except RestoreError:
            raise
        except OSError:
            raise RestorePreflightError("RESTORE_BACKUP_SNAPSHOT_FAILED") from None
        yield snapshot_root


def _copy_snapshot_regular(source: str, destination: str) -> str:
    """Copy one regular member without following a link introduced by a swap."""

    source_path = Path(source)
    destination_path = Path(destination)
    before = source_path.lstat()
    if _is_link_or_reparse(source_path):
        if stat.S_ISLNK(before.st_mode):
            os.symlink(
                os.readlink(source_path),
                destination_path,
                target_is_directory=False,
            )
            return str(destination_path)
        raise RestorePreflightError(
            "RESTORE_BACKUP_CORRUPT",
            compatibility_outcome=CompatibilityOutcome.CORRUPT,
        )
    if not stat.S_ISREG(before.st_mode):
        raise RestorePreflightError(
            "RESTORE_BACKUP_CORRUPT",
            compatibility_outcome=CompatibilityOutcome.CORRUPT,
        )
    source_fd = os.open(source_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    destination_fd = -1
    try:
        opened = os.fstat(source_fd)
        if not stat.S_ISREG(opened.st_mode) or (
            before.st_dev,
            before.st_ino,
        ) != (opened.st_dev, opened.st_ino):
            raise RestorePreflightError("RESTORE_BACKUP_SNAPSHOT_CHANGED")
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
            shutil.copyfileobj(input_stream, output_stream, length=1024 * 1024)
        os.chmod(destination_path, 0o600)
    finally:
        if source_fd >= 0:
            os.close(source_fd)
        if destination_fd >= 0:
            os.close(destination_fd)
    return str(destination_path)


def _inspect_destination(port: DestinationPort) -> DestinationSnapshot:
    try:
        snapshot = port.inspect()
    except Exception:  # noqa: BLE001 - destination probe is an external adapter
        raise RestorePreflightError("RESTORE_DESTINATION_EVALUATION_FAILED") from None
    if not isinstance(snapshot, DestinationSnapshot):
        raise RestorePreflightError("RESTORE_DESTINATION_EVALUATION_FAILED")
    if snapshot.classification not in {
        DestinationClass.FRESH,
        DestinationClass.EXISTING_EMPTY,
    }:
        raise RestorePreflightError("RESTORE_DESTINATION_REJECTED")
    if snapshot.deployment_profile != "v1.1-standard":
        raise RestorePreflightError("RESTORE_DESTINATION_REJECTED")
    if snapshot.canonical_roots != CANONICAL_ROOTS:
        raise RestorePreflightError("RESTORE_DESTINATION_REJECTED")
    _canonical_digest(snapshot.evidence_digest, "RESTORE_DESTINATION_EVALUATION_FAILED")
    if not snapshot.ownership_verified:
        raise RestorePreflightError("RESTORE_DESTINATION_REJECTED")
    if snapshot.classification is DestinationClass.FRESH and not snapshot.parent_ready:
        raise RestorePreflightError("RESTORE_DESTINATION_REJECTED")
    if (
        snapshot.classification is DestinationClass.EXISTING_EMPTY
        and not snapshot.empty_verified
    ):
        raise RestorePreflightError("RESTORE_DESTINATION_REJECTED")
    return snapshot


def _validate_release_evidence(evidence: object) -> None:
    if not isinstance(evidence, ReleaseEvidence):
        raise RestorePreflightError("RESTORE_RELEASE_EVIDENCE_INVALID")
    _canonical_digest(
        evidence.release_identity_digest,
        "RESTORE_RELEASE_EVIDENCE_INVALID",
    )
    _canonical_digest(
        evidence.deployment_identity_digest,
        "RESTORE_RELEASE_EVIDENCE_INVALID",
    )


def _validate_updater_evidence(evidence: object) -> None:
    if not isinstance(evidence, UpdaterEvidence):
        raise RestorePreflightError("RESTORE_UPDATER_EVIDENCE_INVALID")
    _canonical_digest(
        evidence.state_identity_digest, "RESTORE_UPDATER_EVIDENCE_INVALID"
    )
    if evidence.pending_state_preserved is not True:
        raise RestorePreflightError("RESTORE_UPDATER_PENDING_STATE_UNSAFE")


def _validate_protection(
    manifest: Mapping[str, object],
    resolution: SecretResolution,
) -> None:
    secrets = manifest.get("secrets")
    if not isinstance(resolution, SecretResolution) or not isinstance(secrets, Mapping):
        raise RestorePreflightError("RESTORE_PROTECTION_EVIDENCE_INVALID")
    expected_mode = secrets.get("mode")
    expected_status = {
        "none": "NOT_REQUIRED",
        "envelope": "AUTHENTICATED",
        "reference": "RESOLVABLE",
    }.get(expected_mode)
    if resolution.mode != expected_mode or resolution.status != expected_status:
        raise RestorePreflightError("RESTORE_PROTECTION_EVIDENCE_INVALID")


def _load_verified_manifest(root: Path) -> Mapping[str, object]:
    try:
        encoded = (root / backup.MANIFEST_NAME).read_bytes()
        manifest = json.loads(encoded)
    except OSError:
        raise RestorePreflightError("RESTORE_BACKUP_UNAVAILABLE") from None
    except (UnicodeError, json.JSONDecodeError):
        raise RestorePreflightError(
            "RESTORE_BACKUP_CORRUPT",
            compatibility_outcome=CompatibilityOutcome.CORRUPT,
        ) from None
    if not isinstance(manifest, Mapping):
        raise RestorePreflightError(
            "RESTORE_BACKUP_CORRUPT",
            compatibility_outcome=CompatibilityOutcome.CORRUPT,
        )
    return manifest


def _payload_members(manifest: Mapping[str, object]) -> tuple[str, ...]:
    filesystem = manifest.get("filesystem")
    records = filesystem.get("members") if isinstance(filesystem, Mapping) else None
    if not isinstance(records, Sequence) or isinstance(records, (str, bytes)):
        raise RestorePreflightError(
            "RESTORE_BACKUP_CORRUPT",
            compatibility_outcome=CompatibilityOutcome.CORRUPT,
        )
    paths = []
    for record in records:
        path = record.get("path") if isinstance(record, Mapping) else None
        if not isinstance(path, str):
            raise RestorePreflightError(
                "RESTORE_BACKUP_CORRUPT",
                compatibility_outcome=CompatibilityOutcome.CORRUPT,
            )
        if any(path.startswith(f"{root}/") for root in CANONICAL_BACKUP_ROOTS):
            paths.append(path)
    if len(paths) != len(set(paths)):
        raise RestorePreflightError(
            "RESTORE_BACKUP_CORRUPT",
            compatibility_outcome=CompatibilityOutcome.CORRUPT,
        )
    return tuple(sorted(paths, key=lambda value: value.encode("utf-8")))


def _require_complete_validation(report: object) -> None:
    if not isinstance(report, ValidationReport):
        raise RestoreAdapterError("RESTORE_VALIDATION_FAILED")
    _canonical_digest(report.evidence_digest, "RESTORE_VALIDATION_FAILED")
    if report.passed_checks != REQUIRED_VALIDATIONS:
        raise RestoreAdapterError("RESTORE_VALIDATION_FAILED")


def _canonical_uuid(value: object, code: str) -> str:
    try:
        parsed = str(uuid.UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        raise RestorePreflightError(code) from None
    if parsed != value:
        raise RestorePreflightError(code)
    return parsed


def _canonical_digest(value: object, code: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 71
        or not value.startswith("sha256:")
        or any(character not in "0123456789abcdef" for character in value[7:])
    ):
        raise RestorePreflightError(code)
    return value


def _step_error_code(step: str) -> str:
    normalized = step.upper().replace(".", "_").replace("-", "_")
    return f"RESTORE_{normalized}_FAILED"


class LocalFilesystemStager:
    """Copy only already-verified Backup payload members into private staging."""

    def stage(
        self,
        backup_root: Path,
        staging_root: Path,
        member_paths: Sequence[str],
    ) -> None:
        try:
            source_root = Path(backup_root).resolve()
            raw_staging = Path(staging_root).absolute()
            if any(
                _is_link_or_reparse(candidate)
                for candidate in (raw_staging, *raw_staging.parents)
            ):
                raise RestoreAdapterError("FILESYSTEM_STAGING_UNSAFE")
            if raw_staging.exists():
                raise RestoreAdapterError("FILESYSTEM_STAGING_NOT_EMPTY")
            staging = raw_staging.resolve()
            if (
                source_root == staging
                or source_root in staging.parents
                or staging in source_root.parents
            ):
                raise RestoreAdapterError("FILESYSTEM_STAGING_OVERLAP")
            raw_staging.mkdir(parents=True, mode=0o700)
            if _is_link_or_reparse(raw_staging):
                raise RestoreAdapterError("FILESYSTEM_STAGING_UNSAFE")
            os.chmod(raw_staging, 0o700)
            seen: set[str] = set()
            for raw_path in member_paths:
                relative = _restore_member_path(raw_path)
                if relative in seen:
                    raise RestoreAdapterError("FILESYSTEM_MEMBER_DUPLICATE")
                seen.add(relative)
                source = source_root / PurePosixPath(relative)
                source_stat = source.lstat()
                if source.is_symlink() or not stat.S_ISREG(source_stat.st_mode):
                    raise RestoreAdapterError("FILESYSTEM_MEMBER_UNSAFE")
                target = raw_staging / PurePosixPath(relative)
                target.parent.mkdir(parents=True, exist_ok=True)
                os.chmod(target.parent, 0o700)
                with (
                    source.open("rb") as source_stream,
                    target.open("xb") as target_stream,
                ):
                    shutil.copyfileobj(source_stream, target_stream, length=1024 * 1024)
                    target_stream.flush()
                    os.fsync(target_stream.fileno())
                os.chmod(target, 0o600)
        except RestoreError:
            raise
        except OSError:
            raise RestoreAdapterError("FILESYSTEM_STAGE_FAILED") from None


def _is_link_or_reparse(path: Path) -> bool:
    try:
        item_stat = path.lstat()
    except FileNotFoundError:
        return False
    if stat.S_ISLNK(item_stat.st_mode):
        return True
    attributes = getattr(item_stat, "st_file_attributes", 0)
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def _restore_member_path(value: object) -> str:
    if not isinstance(value, str) or "\\" in value or "\x00" in value:
        raise RestoreAdapterError("FILESYSTEM_MEMBER_UNSAFE")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise RestoreAdapterError("FILESYSTEM_MEMBER_UNSAFE")
    normalized = path.as_posix()
    if normalized != value or not any(
        normalized.startswith(f"{root}/") for root in CANONICAL_BACKUP_ROOTS
    ):
        raise RestoreAdapterError("FILESYSTEM_MEMBER_UNSAFE")
    return normalized


@dataclass(frozen=True)
class ProcessResult:
    returncode: int
    stdout: bytes


class ProcessRunner(Protocol):
    def run(
        self,
        argv: Sequence[str],
        *,
        stdin: BinaryIO | None,
        env: Mapping[str, str],
        timeout: int,
    ) -> ProcessResult: ...


class _SubprocessRunner:
    def run(
        self,
        argv: Sequence[str],
        *,
        stdin: BinaryIO | None,
        env: Mapping[str, str],
        timeout: int,
    ) -> ProcessResult:
        command = list(argv)
        resolved_executable = shutil.which(command[0], path=env.get("PATH"))
        if resolved_executable is None:
            raise RestoreAdapterError("DATABASE_IMPORT_FAILED")
        command[0] = resolved_executable
        try:
            completed = subprocess.run(
                command,
                check=False,
                stdin=stdin,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env=dict(env),
                timeout=timeout,
                shell=False,
            )
        except (OSError, subprocess.SubprocessError):
            raise RestoreAdapterError("DATABASE_IMPORT_FAILED") from None
        return ProcessResult(completed.returncode, completed.stdout)


class SubprocessPostgresRestore:
    """Restore a verified gzip/plain logical dump into one empty staged database."""

    _EMPTY_QUERY = (
        "SELECT count(*) FROM pg_catalog.pg_class c "
        "JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace "
        "WHERE n.nspname NOT IN ('pg_catalog','information_schema') "
        "AND n.nspname NOT LIKE 'pg_toast%' "
        "AND c.relkind IN ('r','p','v','m','S','f');"
    )

    def __init__(
        self,
        database_url: str,
        *,
        executable: str = "psql",
        timeout: int = 600,
        runner: ProcessRunner | None = None,
    ) -> None:
        if (
            not isinstance(database_url, str)
            or not database_url
            or "\x00" in database_url
        ):
            raise RestoreAdapterError("DATABASE_TARGET_INVALID")
        try:
            database_environment = backup.postgres_connection_environment(database_url)
        except backup.BackupError:
            raise RestoreAdapterError("DATABASE_TARGET_INVALID") from None
        if Path(executable).name.casefold() not in {"psql", "psql.exe"}:
            raise RestoreAdapterError("DATABASE_TOOL_INVALID")
        if isinstance(timeout, bool) or not isinstance(timeout, int) or timeout <= 0:
            raise RestoreAdapterError("DATABASE_TIMEOUT_INVALID")
        self._database_environment = database_environment
        self._executable = executable
        self._timeout = timeout
        self._runner = runner or _SubprocessRunner()

    def restore(self, dump_path: Path) -> None:
        environment = dict(self._database_environment)
        empty = self._runner.run(
            (
                self._executable,
                "--no-psqlrc",
                "--tuples-only",
                "--no-align",
                "--set=ON_ERROR_STOP=1",
                "--command",
                self._EMPTY_QUERY,
            ),
            stdin=None,
            env=environment,
            timeout=min(self._timeout, 60),
        )
        if empty.returncode != 0 or empty.stdout.strip() != b"0":
            raise RestoreAdapterError("DATABASE_TARGET_NOT_EMPTY")
        try:
            with tempfile.TemporaryFile(mode="w+b") as sql:
                uncompressed = 0
                with gzip.open(Path(dump_path), "rb") as compressed:
                    for chunk in iter(lambda: compressed.read(1024 * 1024), b""):
                        sql.write(chunk)
                        uncompressed += len(chunk)
                if uncompressed == 0:
                    raise RestoreAdapterError("DATABASE_DUMP_EMPTY")
                sql.flush()
                sql.seek(0)
                imported = self._runner.run(
                    (
                        self._executable,
                        "--no-psqlrc",
                        "--set=ON_ERROR_STOP=1",
                        "--single-transaction",
                    ),
                    stdin=sql,
                    env=environment,
                    timeout=self._timeout,
                )
        except RestoreError:
            raise
        except (OSError, EOFError):
            raise RestoreAdapterError("DATABASE_DUMP_INVALID") from None
        if imported.returncode != 0:
            raise RestoreAdapterError("DATABASE_IMPORT_FAILED")


__all__ = [
    "CANONICAL_BACKUP_ROOTS",
    "CANONICAL_ROOTS",
    "REQUIRED_VALIDATIONS",
    "DatabasePort",
    "DestinationClass",
    "DestinationPort",
    "DestinationSnapshot",
    "EnvelopeSecretResolver",
    "ExternalReferencePort",
    "LocalFilesystemStager",
    "MutationPort",
    "NoneSecretResolver",
    "ProcessResult",
    "ReferenceSecretResolver",
    "ReleaseEvidence",
    "ReleasePort",
    "RestoreAdapterError",
    "RestoreCompatibilityEvidence",
    "RestoreError",
    "RestorePlan",
    "RestorePreflightError",
    "RestoreRecoveryPersistenceError",
    "RestoreRequest",
    "RestoreResult",
    "RestoreTerminalState",
    "SecretResolution",
    "SubprocessPostgresRestore",
    "UpdaterEvidence",
    "UpdaterPort",
    "ValidationPort",
    "ValidationReport",
    "execute_restore",
    "prepare_restore",
]
