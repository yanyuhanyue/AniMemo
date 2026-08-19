"""Deep orchestration module for the canonical AniMemo Installer Runtime v1.

The public seam deliberately exposes only :meth:`Installer.plan` and
:meth:`Installer.execute`. Host mutation, Release authority, configuration,
platform qualification, operation evidence, and Restore Runtime integration
remain behind narrow ports. No port accepts an arbitrary path, command, image,
Compose project, service, or environment mapping.
"""

from __future__ import annotations

import hmac
import ipaddress
import re
import uuid
from collections.abc import Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from durability.canonical import canonical_json_bytes, sha256_identity
from durability.compatibility import (
    ArtifactIdentity,
    CompatibilityDecision,
    CompatibilityEvaluationError,
    CompatibilityOperation,
    CompatibilityOutcome,
    DimensionAssessment,
    evaluate_compatibility,
)
from updater.transport import ExplicitTransportPolicy

INSTALL_PLAN_IDENTITY = "animemo.install-plan/v1"
INSTALL_RESULT_IDENTITY = "animemo.install-result/v1"
STANDARD_DEPLOYMENT_PROFILE = "v1.1-standard"
STANDARD_PLATFORM_PROFILE = "v1.1-standard-linux-amd64"
_INSTALL_RELEASE_VERSION = re.compile(
    r"^v(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)(?:-rc\.[1-9][0-9]*)?$"
)


class InstallerMode(StrEnum):
    FRESH = "fresh"
    RESTORE_TO_NEW = "restore-to-new"


class InstallTransportSource(StrEnum):
    GITHUB = "github"
    OFFICIAL_MIRROR = "official-mirror"
    LOCAL_BUNDLE = "local-bundle"


def explicit_transport_policy(
    source: InstallTransportSource,
) -> ExplicitTransportPolicy | None:
    if source is InstallTransportSource.GITHUB:
        return ExplicitTransportPolicy.github()
    if source is InstallTransportSource.OFFICIAL_MIRROR:
        return ExplicitTransportPolicy.official_mirror()
    if source is InstallTransportSource.LOCAL_BUNDLE:
        return None
    raise InstallerError(
        "INSTALL_TRANSPORT_SOURCE_INVALID",
        outcome=InstallOutcome.VALIDATION_FAILED,
    )


def transport_policy_identity(source: InstallTransportSource) -> str:
    policy = explicit_transport_policy(source)
    if policy is not None:
        return policy.identity
    return sha256_identity(
        canonical_json_bytes(
            {
                "authority": "blocked-portable-publication-authority",
                "fallback": "forbidden",
                "policyVersion": 1,
                "source": InstallTransportSource.LOCAL_BUNDLE.value,
            }
        )
    ).removeprefix("sha256:")


class RestoreProtectionKind(StrEnum):
    NONE = "none"
    ONE_TIME_KEY_FILE = "one-time-key-file"
    PASSPHRASE_FILE = "passphrase-file"
    PASSPHRASE_FD = "passphrase-fd"


class TargetClass(StrEnum):
    ABSENT = "ABSENT"
    VERIFIED_EMPTY = "VERIFIED_EMPTY"
    ACTIVE = "ACTIVE"
    FOREIGN = "FOREIGN"
    PARTIAL_AMBIGUOUS = "PARTIAL_AMBIGUOUS"
    CORRUPT = "CORRUPT"


class InstallAction(StrEnum):
    INSTALL_FRESH = "INSTALL_FRESH"
    RESTORE_TO_NEW = "RESTORE_TO_NEW"
    NO_CHANGE = "NO_CHANGE"
    UPDATER_HANDOFF = "UPDATER_HANDOFF"


class InstallOutcome(StrEnum):
    SUCCEEDED = "SUCCEEDED"
    NO_CHANGE = "NO_CHANGE"
    UPDATER_HANDOFF = "UPDATER_HANDOFF"
    VALIDATION_FAILED = "VALIDATION_FAILED"
    COMPATIBILITY_BLOCKED = "COMPATIBILITY_BLOCKED"
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"
    ENVIRONMENT_FAILED = "ENVIRONMENT_FAILED"


class InstallPhase(StrEnum):
    PREFLIGHT_VERIFIED = "preflight_verified"
    ROOTS_PREPARING = "roots_preparing"
    CONFIG_STAGING = "config_staging"
    MATERIAL_STAGING = "material_staging"
    SERVICES_PREPARING = "services_preparing"
    DATABASE_MIGRATING = "database_migrating"
    BOOTSTRAPPING = "bootstrapping"
    RUNTIME_STARTING = "runtime_starting"
    VALIDATING = "validating"
    UPDATER_ADOPTING = "updater_adopting"
    DOCTOR = "doctor"
    SUCCEEDED = "succeeded"


class InstallerError(RuntimeError):
    """Stable, non-secret Installer domain error."""

    def __init__(self, code: str, *, outcome: InstallOutcome) -> None:
        self.code = code
        self.outcome = outcome
        super().__init__(code)


class InstallerAdapterError(InstallerError):
    """Stable Adapter failure with explicit mutation/recovery classification."""

    def __init__(
        self,
        code: str,
        *,
        mutation_occurred: bool,
        recovery_required: bool,
    ) -> None:
        self.mutation_occurred = mutation_occurred
        self.recovery_required = recovery_required
        super().__init__(
            code,
            outcome=(
                InstallOutcome.RECOVERY_REQUIRED
                if recovery_required
                else InstallOutcome.ENVIRONMENT_FAILED
            ),
        )


def _canonical_digest(value: str, code: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 71
        or not value.startswith("sha256:")
    ):
        raise InstallerError(code, outcome=InstallOutcome.VALIDATION_FAILED)
    if any(character not in "0123456789abcdef" for character in value[7:]):
        raise InstallerError(code, outcome=InstallOutcome.VALIDATION_FAILED)
    return value


def _canonical_uuid(value: str, code: str) -> str:
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError, TypeError):
        raise InstallerError(code, outcome=InstallOutcome.VALIDATION_FAILED) from None
    if str(parsed) != value:
        raise InstallerError(code, outcome=InstallOutcome.VALIDATION_FAILED)
    return value


@dataclass(frozen=True)
class ReleaseSelector:
    channel: str | None = None
    version: str | None = None

    def __post_init__(self) -> None:
        if (self.channel is None) == (self.version is None):
            raise InstallerError(
                "INSTALL_RELEASE_SELECTOR_INVALID",
                outcome=InstallOutcome.VALIDATION_FAILED,
            )
        if self.channel is not None and self.channel not in {"stable", "rc"}:
            raise InstallerError(
                "INSTALL_RELEASE_CHANNEL_INVALID",
                outcome=InstallOutcome.VALIDATION_FAILED,
            )
        if self.version is not None and not _INSTALL_RELEASE_VERSION.fullmatch(
            self.version
        ):
            raise InstallerError(
                "INSTALL_RELEASE_VERSION_INVALID",
                outcome=InstallOutcome.VALIDATION_FAILED,
            )

    def as_dict(self) -> dict[str, object]:
        return {"channel": self.channel, "version": self.version}


@dataclass(frozen=True)
class ListenRequest:
    host: str = "127.0.0.1"
    port: int = 8088
    direct_exposure_accepted: bool = False

    def __post_init__(self) -> None:
        try:
            address = ipaddress.ip_address(self.host)
        except ValueError:
            raise InstallerError(
                "INSTALL_LISTEN_INVALID",
                outcome=InstallOutcome.VALIDATION_FAILED,
            ) from None
        if (
            address.compressed != self.host
            or address.is_multicast
            or address.is_link_local
            or type(self.port) is not int
            or not 1 <= self.port <= 65535
        ):
            raise InstallerError(
                "INSTALL_LISTEN_INVALID",
                outcome=InstallOutcome.VALIDATION_FAILED,
            )
        if self.direct_exposure_accepted and address.is_loopback:
            raise InstallerError(
                "INSTALL_DIRECT_EXPOSURE_ACCEPTANCE_INVALID",
                outcome=InstallOutcome.VALIDATION_FAILED,
            )


@dataclass(frozen=True)
class RestoreProtectionRequest:
    """Secret-safe description of how Restore acquires envelope protection.

    Secret bytes are deliberately absent.  A production adapter reads the
    protected file or descriptor once, before planning, and retains only a
    redacting Secret Envelope value object for revalidation/execution.
    """

    kind: RestoreProtectionKind
    path: Path | None = None
    fd: int | None = None

    def __post_init__(self) -> None:
        if self.kind in {
            RestoreProtectionKind.ONE_TIME_KEY_FILE,
            RestoreProtectionKind.PASSPHRASE_FILE,
        }:
            if not isinstance(self.path, Path) or self.fd is not None:
                raise InstallerError(
                    "INSTALL_RESTORE_PROTECTION_INPUT_INVALID",
                    outcome=InstallOutcome.VALIDATION_FAILED,
                )
            return
        if self.kind is RestoreProtectionKind.PASSPHRASE_FD:
            if (
                self.path is not None
                or isinstance(self.fd, bool)
                or not isinstance(self.fd, int)
                or self.fd < 0
            ):
                raise InstallerError(
                    "INSTALL_RESTORE_PROTECTION_INPUT_INVALID",
                    outcome=InstallOutcome.VALIDATION_FAILED,
                )
            return
        if self.path is not None or self.fd is not None:
            raise InstallerError(
                "INSTALL_RESTORE_PROTECTION_INPUT_INVALID",
                outcome=InstallOutcome.VALIDATION_FAILED,
            )


@dataclass(frozen=True)
class InstallRequest:
    mode: InstallerMode
    selector: ReleaseSelector
    public_origin: str
    transport_source: InstallTransportSource = InstallTransportSource.GITHUB
    listen: ListenRequest = ListenRequest()
    backup_root: Path | None = None
    restore_protection: RestoreProtectionRequest | None = None
    non_interactive: bool = False
    insecure_http_accepted: bool = False
    transport_policy_identity: str = field(init=False)

    def __post_init__(self) -> None:
        if type(self.transport_source) is not InstallTransportSource:
            raise InstallerError(
                "INSTALL_TRANSPORT_SOURCE_INVALID",
                outcome=InstallOutcome.VALIDATION_FAILED,
            )
        object.__setattr__(
            self,
            "transport_policy_identity",
            transport_policy_identity(self.transport_source),
        )
        if self.mode is InstallerMode.FRESH and (
            self.backup_root is not None or self.restore_protection is not None
        ):
            raise InstallerError(
                "INSTALL_BACKUP_NOT_ALLOWED",
                outcome=InstallOutcome.VALIDATION_FAILED,
            )
        if self.mode is InstallerMode.RESTORE_TO_NEW and self.backup_root is None:
            raise InstallerError(
                "INSTALL_BACKUP_REQUIRED",
                outcome=InstallOutcome.VALIDATION_FAILED,
            )
        if (
            self.mode is InstallerMode.RESTORE_TO_NEW
            and self.restore_protection is None
        ):
            raise InstallerError(
                "INSTALL_RESTORE_PROTECTION_REQUIRED",
                outcome=InstallOutcome.VALIDATION_FAILED,
            )
        if not isinstance(self.public_origin, str) or not self.public_origin:
            raise InstallerError(
                "INSTALL_PUBLIC_ORIGIN_REQUIRED",
                outcome=InstallOutcome.VALIDATION_FAILED,
            )
        if self.public_origin.startswith("http://") != self.insecure_http_accepted:
            raise InstallerError(
                "INSTALL_INSECURE_HTTP_ACCEPTANCE_REQUIRED",
                outcome=InstallOutcome.VALIDATION_FAILED,
            )
        if (
            not ipaddress.ip_address(self.listen.host).is_loopback
            and not self.listen.direct_exposure_accepted
        ):
            raise InstallerError(
                "INSTALL_DIRECT_EXPOSURE_ACCEPTANCE_REQUIRED",
                outcome=InstallOutcome.VALIDATION_FAILED,
            )


@dataclass(frozen=True)
class ReleaseEvidence:
    version: str
    channel: str
    commit: str
    manifest_digest: str
    material_identity_digest: str
    deployment_identity_digest: str
    deployment_profile: str
    platform_profile: str
    transport_source: InstallTransportSource = InstallTransportSource.GITHUB
    transport_policy_identity: str = field(
        default_factory=lambda: ExplicitTransportPolicy.github().identity
    )

    def __post_init__(self) -> None:
        if (
            type(self.transport_source) is not InstallTransportSource
            or self.transport_policy_identity
            != transport_policy_identity(self.transport_source)
        ):
            raise InstallerError(
                "INSTALL_RELEASE_TRANSPORT_POLICY_INVALID",
                outcome=InstallOutcome.VALIDATION_FAILED,
            )
        if self.deployment_profile != STANDARD_DEPLOYMENT_PROFILE:
            raise InstallerError(
                "INSTALL_DEPLOYMENT_PROFILE_UNSUPPORTED",
                outcome=InstallOutcome.COMPATIBILITY_BLOCKED,
            )
        if self.platform_profile != STANDARD_PLATFORM_PROFILE:
            raise InstallerError(
                "INSTALL_PLATFORM_PROFILE_UNSUPPORTED",
                outcome=InstallOutcome.COMPATIBILITY_BLOCKED,
            )
        for value, code in (
            (self.manifest_digest, "INSTALL_MANIFEST_IDENTITY_INVALID"),
            (self.material_identity_digest, "INSTALL_MATERIAL_IDENTITY_INVALID"),
            (self.deployment_identity_digest, "INSTALL_DEPLOYMENT_IDENTITY_INVALID"),
        ):
            _canonical_digest(value, code)
        if (
            not self.version
            or self.channel not in {"stable", "rc"}
            or len(self.commit) != 40
            or any(character not in "0123456789abcdef" for character in self.commit)
        ):
            raise InstallerError(
                "INSTALL_RELEASE_IDENTITY_INVALID",
                outcome=InstallOutcome.VALIDATION_FAILED,
            )

    def as_dict(self) -> dict[str, object]:
        return {
            "version": self.version,
            "channel": self.channel,
            "commit": self.commit,
            "manifestDigest": self.manifest_digest,
            "materialIdentityDigest": self.material_identity_digest,
            "deploymentIdentityDigest": self.deployment_identity_digest,
            "deploymentProfile": self.deployment_profile,
            "platformProfile": self.platform_profile,
            "transportSource": self.transport_source.value,
            "transportPolicyIdentity": self.transport_policy_identity,
        }


@dataclass(frozen=True)
class TargetEvidence:
    classification: TargetClass
    evidence_digest: str
    instance_id: str | None = None
    release_manifest_digest: str | None = None
    material_identity_digest: str | None = None
    config_revision: str | None = None
    public_origin: str | None = None
    listen_host: str | None = None
    listen_port: int | None = None
    exact_release_running: bool = False
    doctor_complete: bool = False

    def __post_init__(self) -> None:
        _canonical_digest(self.evidence_digest, "INSTALL_TARGET_EVIDENCE_INVALID")
        if self.classification is TargetClass.ACTIVE:
            if not self.instance_id or not self.release_manifest_digest:
                raise InstallerError(
                    "INSTALL_ACTIVE_TARGET_INVALID",
                    outcome=InstallOutcome.VALIDATION_FAILED,
                )
            _canonical_uuid(self.instance_id, "INSTALL_ACTIVE_TARGET_INVALID")
            _canonical_digest(
                self.release_manifest_digest,
                "INSTALL_ACTIVE_TARGET_INVALID",
            )
            if self.material_identity_digest is not None:
                _canonical_digest(
                    self.material_identity_digest,
                    "INSTALL_ACTIVE_TARGET_INVALID",
                )

    def as_dict(self) -> dict[str, object]:
        return {
            "classification": self.classification.value,
            "evidenceDigest": self.evidence_digest,
            "instanceId": self.instance_id,
            "releaseManifestDigest": self.release_manifest_digest,
            "materialIdentityDigest": self.material_identity_digest,
            "configRevision": self.config_revision,
            "publicOrigin": self.public_origin,
            "listen": (
                {"host": self.listen_host, "port": self.listen_port}
                if self.listen_host is not None and self.listen_port is not None
                else None
            ),
            "exactReleaseRunning": self.exact_release_running,
            "doctorComplete": self.doctor_complete,
        }


@dataclass(frozen=True)
class PlatformEvidence:
    compatible: bool
    profile: str
    evidence_digest: str
    reason_code: str

    def __post_init__(self) -> None:
        _canonical_digest(self.evidence_digest, "INSTALL_PLATFORM_EVIDENCE_INVALID")
        if self.profile != STANDARD_PLATFORM_PROFILE:
            raise InstallerError(
                "INSTALL_PLATFORM_PROFILE_UNSUPPORTED",
                outcome=InstallOutcome.COMPATIBILITY_BLOCKED,
            )
        if not self.reason_code or len(self.reason_code) > 128:
            raise InstallerError(
                "INSTALL_PLATFORM_EVIDENCE_INVALID",
                outcome=InstallOutcome.VALIDATION_FAILED,
            )

    def as_dict(self) -> dict[str, object]:
        return {
            "compatible": self.compatible,
            "profile": self.profile,
            "evidenceDigest": self.evidence_digest,
            "reasonCode": self.reason_code,
        }


@dataclass(frozen=True)
class ConfigPlanEvidence:
    instance_id: str
    config_revision: str
    public_origin: str
    listen_host: str
    listen_port: int
    exposure: str
    non_secret_identity_digest: str
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _canonical_uuid(self.instance_id, "INSTALL_CONFIG_EVIDENCE_INVALID")
        _canonical_uuid(self.config_revision, "INSTALL_CONFIG_EVIDENCE_INVALID")
        _canonical_digest(
            self.non_secret_identity_digest,
            "INSTALL_CONFIG_EVIDENCE_INVALID",
        )
        if self.exposure not in {"loopback", "direct"}:
            raise InstallerError(
                "INSTALL_CONFIG_EVIDENCE_INVALID",
                outcome=InstallOutcome.VALIDATION_FAILED,
            )
        if (
            not self.public_origin
            or not self.listen_host
            or type(self.listen_port) is not int
            or not 1 <= self.listen_port <= 65535
        ):
            raise InstallerError(
                "INSTALL_CONFIG_EVIDENCE_INVALID",
                outcome=InstallOutcome.VALIDATION_FAILED,
            )
        if any(not item or len(item) > 128 for item in self.warnings):
            raise InstallerError(
                "INSTALL_CONFIG_EVIDENCE_INVALID",
                outcome=InstallOutcome.VALIDATION_FAILED,
            )

    def as_dict(self) -> dict[str, object]:
        return {
            "instanceId": self.instance_id,
            "configRevision": self.config_revision,
            "publicOrigin": self.public_origin,
            "listen": {"host": self.listen_host, "port": self.listen_port},
            "exposure": self.exposure,
            "nonSecretIdentityDigest": self.non_secret_identity_digest,
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class RestorePlanEvidence:
    operation_id: str
    instance_id: str
    restore_plan_digest: str
    backup_identity_digest: str

    def __post_init__(self) -> None:
        if len(self.operation_id) != 32 or any(
            character not in "0123456789abcdef" for character in self.operation_id
        ):
            raise InstallerError(
                "INSTALL_RESTORE_EVIDENCE_INVALID",
                outcome=InstallOutcome.VALIDATION_FAILED,
            )
        _canonical_uuid(self.instance_id, "INSTALL_RESTORE_EVIDENCE_INVALID")
        _canonical_digest(self.restore_plan_digest, "INSTALL_RESTORE_EVIDENCE_INVALID")
        _canonical_digest(
            self.backup_identity_digest,
            "INSTALL_RESTORE_EVIDENCE_INVALID",
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "operationId": self.operation_id,
            "sourceInstanceId": self.instance_id,
            "restorePlanDigest": self.restore_plan_digest,
            "backupIdentityDigest": self.backup_identity_digest,
        }


@dataclass(frozen=True)
class InstallPlan:
    operation_id: str
    mode: InstallerMode
    action: InstallAction
    selector: ReleaseSelector
    transport_source: InstallTransportSource
    transport_policy_identity: str
    release: ReleaseEvidence
    target: TargetEvidence
    platform: PlatformEvidence
    compatibility: CompatibilityDecision
    configuration: ConfigPlanEvidence
    restore: RestorePlanEvidence | None
    execution_steps: tuple[str, ...]
    warnings: tuple[str, ...]
    plan_digest: str

    def body(self) -> dict[str, object]:
        return {
            "planIdentity": INSTALL_PLAN_IDENTITY,
            "operationId": self.operation_id,
            "mode": self.mode.value,
            "action": self.action.value,
            "selector": self.selector.as_dict(),
            "transportSource": self.transport_source.value,
            "transportPolicyIdentity": self.transport_policy_identity,
            "release": self.release.as_dict(),
            "target": self.target.as_dict(),
            "platform": self.platform.as_dict(),
            "compatibility": self.compatibility.as_dict(),
            "configuration": self.configuration.as_dict(),
            "restore": self.restore.as_dict() if self.restore else None,
            "executionSteps": list(self.execution_steps),
            "warnings": list(self.warnings),
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.body(), "planDigest": self.plan_digest}


@dataclass(frozen=True)
class InstallResult:
    outcome: InstallOutcome
    operation_id: str
    mode: InstallerMode
    instance_id: str
    release: Mapping[str, object]
    state: str
    reason_code: str
    warnings: tuple[str, ...]
    recovery_required: bool
    completed_steps: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "resultIdentity": INSTALL_RESULT_IDENTITY,
            "outcome": self.outcome.value,
            "operationId": self.operation_id,
            "mode": self.mode.value,
            "instanceId": self.instance_id,
            "release": dict(self.release),
            "state": self.state,
            "reasonCode": self.reason_code,
            "warnings": list(self.warnings),
            "recoveryRequired": self.recovery_required,
            "completedSteps": list(self.completed_steps),
        }


class ReleasePort(Protocol):
    def resolve(
        self, selector: ReleaseSelector, *, refresh: bool
    ) -> ReleaseEvidence: ...


class TargetPort(Protocol):
    def inspect(self) -> TargetEvidence: ...


class PlatformPort(Protocol):
    def assess(self, profile: str) -> PlatformEvidence: ...


class CompatibilityEvidencePort(Protocol):
    def collect(
        self,
        release: ReleaseEvidence,
        platform: PlatformEvidence,
    ) -> tuple[ArtifactIdentity, tuple[DimensionAssessment, ...]]: ...


class ManagedConfigurationPort(Protocol):
    def plan(
        self,
        *,
        instance_id: str,
        public_origin: str,
        listen: ListenRequest,
        insecure_http_accepted: bool,
    ) -> ConfigPlanEvidence: ...

    def revalidate(self, plan: ConfigPlanEvidence) -> None: ...


class RestoreRuntimePort(Protocol):
    def prepare(
        self,
        *,
        operation_id: str,
        backup_root: Path,
        release: ReleaseEvidence,
        target: TargetEvidence,
        platform: PlatformEvidence,
        protection: RestoreProtectionRequest,
    ) -> RestorePlanEvidence: ...

    def bind_configuration(
        self,
        plan: RestorePlanEvidence,
        configuration: ConfigPlanEvidence,
    ) -> ConfigPlanEvidence: ...

    def revalidate(self, plan: RestorePlanEvidence) -> None: ...

    def execute(
        self,
        plan: RestorePlanEvidence,
        *,
        accepted_plan_digest: str,
        installation_plan: InstallPlan,
    ) -> tuple[str, ...]: ...


class OperationPort(Protocol):
    def acquire_lock(self, operation_id: str) -> AbstractContextManager[None]: ...

    def begin(self, plan: InstallPlan) -> None: ...

    def phase(
        self,
        phase: InstallPhase,
        *,
        completed_step: str | None = None,
        mutation_occurred: bool,
        irreversible_mutation_started: bool,
    ) -> None: ...

    def fail(
        self,
        *,
        phase: InstallPhase,
        error_code: str,
        mutation_occurred: bool,
        irreversible_mutation_started: bool,
        recovery_required: bool,
        rollback_succeeded: bool | None,
    ) -> None: ...

    def succeed(self, *, completed_steps: tuple[str, ...]) -> None: ...


class FreshInstallPort(Protocol):
    def prepare_roots(self, plan: InstallPlan) -> None: ...

    def publish_config(self, plan: InstallPlan) -> None: ...

    def stage_release(self, plan: InstallPlan) -> None: ...

    def prepare_services(self, plan: InstallPlan) -> None: ...

    def migrate_database(self, plan: InstallPlan) -> None: ...

    def bootstrap(self, plan: InstallPlan) -> None: ...

    def start_runtime(self, plan: InstallPlan) -> None: ...

    def validate_running_release(self, plan: InstallPlan) -> None: ...

    def adopt_updater(self, plan: InstallPlan) -> None: ...

    def doctor_acceptance(self, plan: InstallPlan) -> None: ...

    def cleanup_owned_staging(self, plan: InstallPlan) -> None: ...


_FRESH_STEPS = (
    "roots.prepare",
    "configuration.publish",
    "release.stage",
    "services.prepare",
    "database.migrate",
    "application.bootstrap",
    "runtime.start",
    "runtime.validate",
    "updater.adopt-and-publish-locator",
    "doctor.accept",
)
_RESTORE_STEPS = ("restore.runtime.execute",)


class Installer:
    """Canonical Installer Module with a small plan/execute Interface."""

    def __init__(
        self,
        *,
        releases: ReleasePort,
        target: TargetPort,
        platform: PlatformPort,
        compatibility: CompatibilityEvidencePort,
        configuration: ManagedConfigurationPort,
        operations: OperationPort,
        fresh: FreshInstallPort,
        restore: RestoreRuntimePort,
    ) -> None:
        self._releases = releases
        self._target = target
        self._platform = platform
        self._compatibility = compatibility
        self._configuration = configuration
        self._operations = operations
        self._fresh = fresh
        self._restore = restore

    def plan(self, request: InstallRequest) -> InstallPlan:
        """Build a read-only, exact, secret-free operation plan."""

        release = self._resolve(
            request.selector,
            transport_source=request.transport_source,
            transport_policy_identity=request.transport_policy_identity,
            refresh=False,
        )
        platform = self._assess_platform(release.platform_profile)
        if not platform.compatible:
            raise InstallerError(
                platform.reason_code,
                outcome=InstallOutcome.COMPATIBILITY_BLOCKED,
            )
        compatibility = self._evaluate_compatibility(release, platform)
        if compatibility.outcome is not CompatibilityOutcome.COMPATIBLE:
            raise InstallerError(
                compatibility.reason_code.value,
                outcome=InstallOutcome.COMPATIBILITY_BLOCKED,
            )
        target = self._inspect_target()
        operation_id = uuid.uuid4().hex
        restore: RestorePlanEvidence | None = None

        if request.mode is InstallerMode.RESTORE_TO_NEW:
            if target.classification not in {
                TargetClass.ABSENT,
                TargetClass.VERIFIED_EMPTY,
            }:
                self._reject_target(target)
            assert request.backup_root is not None
            restore = self._prepare_restore(
                operation_id=operation_id,
                backup_root=request.backup_root,
                release=release,
                target=target,
                platform=platform,
                protection=request.restore_protection,
            )
            instance_id = restore.instance_id
            action = InstallAction.RESTORE_TO_NEW
            steps = _RESTORE_STEPS
        elif target.classification in {TargetClass.ABSENT, TargetClass.VERIFIED_EMPTY}:
            instance_id = str(uuid.uuid4())
            action = InstallAction.INSTALL_FRESH
            steps = _FRESH_STEPS
        elif target.classification is TargetClass.ACTIVE:
            assert target.instance_id is not None
            instance_id = target.instance_id
            same_release = hmac.compare_digest(
                target.release_manifest_digest or "",
                release.manifest_digest,
            ) and hmac.compare_digest(
                target.material_identity_digest or "",
                release.material_identity_digest,
            )
            action = (
                InstallAction.NO_CHANGE
                if same_release
                else InstallAction.UPDATER_HANDOFF
            )
            steps = ()
        else:
            self._reject_target(target)

        configuration = self._plan_configuration(
            instance_id=instance_id,
            public_origin=request.public_origin,
            listen=request.listen,
            insecure_http_accepted=request.insecure_http_accepted,
        )
        if restore is not None:
            try:
                configuration = self._restore.bind_configuration(
                    restore, configuration
                )
            except InstallerError:
                raise
            except Exception:  # noqa: BLE001 - Restore Adapter is redacted
                raise InstallerError(
                    "INSTALL_RESTORE_CONFIGURATION_BINDING_FAILED",
                    outcome=InstallOutcome.VALIDATION_FAILED,
                ) from None
            if not isinstance(configuration, ConfigPlanEvidence):
                raise InstallerError(
                    "INSTALL_CONFIG_EVIDENCE_INVALID",
                    outcome=InstallOutcome.VALIDATION_FAILED,
                )
        if target.classification is TargetClass.ACTIVE:
            if (
                target.public_origin != configuration.public_origin
                or target.listen_host != configuration.listen_host
                or target.listen_port != configuration.listen_port
            ):
                raise InstallerError(
                    "INSTALL_EXISTING_CONFIGURATION_CONFLICT",
                    outcome=InstallOutcome.VALIDATION_FAILED,
                )
            if action is InstallAction.NO_CHANGE and (
                not target.exact_release_running or not target.doctor_complete
            ):
                raise InstallerError(
                    "INSTALL_EXISTING_INSTANCE_UNHEALTHY",
                    outcome=InstallOutcome.RECOVERY_REQUIRED,
                )

        body = {
            "planIdentity": INSTALL_PLAN_IDENTITY,
            "operationId": operation_id,
            "mode": request.mode.value,
            "action": action.value,
            "selector": request.selector.as_dict(),
            "transportSource": request.transport_source.value,
            "transportPolicyIdentity": request.transport_policy_identity,
            "release": release.as_dict(),
            "target": target.as_dict(),
            "platform": platform.as_dict(),
            "compatibility": compatibility.as_dict(),
            "configuration": configuration.as_dict(),
            "restore": restore.as_dict() if restore else None,
            "executionSteps": list(steps),
            "warnings": list(configuration.warnings),
        }
        plan = InstallPlan(
            operation_id=operation_id,
            mode=request.mode,
            action=action,
            selector=request.selector,
            transport_source=request.transport_source,
            transport_policy_identity=request.transport_policy_identity,
            release=release,
            target=target,
            platform=platform,
            compatibility=compatibility,
            configuration=configuration,
            restore=restore,
            execution_steps=steps,
            warnings=configuration.warnings,
            plan_digest=sha256_identity(canonical_json_bytes(body)),
        )
        if plan.body() != body:
            raise InstallerError(
                "INSTALL_PLAN_INVALID",
                outcome=InstallOutcome.VALIDATION_FAILED,
            )
        return plan

    def execute(self, plan: InstallPlan, *, accepted_plan_digest: str) -> InstallResult:
        """Reverify the accepted plan and execute its fixed action."""

        self._validate_plan(plan, accepted_plan_digest)
        self._revalidate(plan)
        if plan.action is InstallAction.NO_CHANGE:
            return self._result(
                plan,
                InstallOutcome.NO_CHANGE,
                state="healthy",
                reason_code="INSTALL_ALREADY_CURRENT",
            )
        if plan.action is InstallAction.UPDATER_HANDOFF:
            return self._result(
                plan,
                InstallOutcome.UPDATER_HANDOFF,
                state="active",
                reason_code="INSTALL_USE_UPDATER",
            )
        if plan.action is InstallAction.RESTORE_TO_NEW:
            assert plan.restore is not None
            try:
                completed = self._restore.execute(
                    plan.restore,
                    accepted_plan_digest=plan.restore.restore_plan_digest,
                    installation_plan=plan,
                )
            except InstallerError:
                raise
            except Exception:  # noqa: BLE001 - Restore Adapter is a redacted boundary
                raise InstallerAdapterError(
                    "INSTALL_RESTORE_RUNTIME_FAILED",
                    mutation_occurred=True,
                    recovery_required=True,
                ) from None
            return self._result(
                plan,
                InstallOutcome.SUCCEEDED,
                state="published",
                reason_code="INSTALL_RESTORE_SUCCEEDED",
                completed_steps=tuple(completed),
            )
        return self._execute_fresh(plan)

    def _execute_fresh(self, plan: InstallPlan) -> InstallResult:
        completed: list[str] = []
        phase = InstallPhase.PREFLIGHT_VERIFIED
        mutation_occurred = False
        irreversible = False
        with self._operations.acquire_lock(plan.operation_id):
            self._revalidate(plan)
            try:
                self._operations.begin(plan)
                self._operations.phase(
                    phase,
                    mutation_occurred=False,
                    irreversible_mutation_started=False,
                )
                schedule = (
                    (
                        InstallPhase.ROOTS_PREPARING,
                        "roots.prepare",
                        self._fresh.prepare_roots,
                        False,
                    ),
                    (
                        InstallPhase.CONFIG_STAGING,
                        "configuration.publish",
                        self._fresh.publish_config,
                        False,
                    ),
                    (
                        InstallPhase.MATERIAL_STAGING,
                        "release.stage",
                        self._fresh.stage_release,
                        False,
                    ),
                    (
                        InstallPhase.SERVICES_PREPARING,
                        "services.prepare",
                        self._fresh.prepare_services,
                        False,
                    ),
                    (
                        InstallPhase.DATABASE_MIGRATING,
                        "database.migrate",
                        self._fresh.migrate_database,
                        True,
                    ),
                    (
                        InstallPhase.BOOTSTRAPPING,
                        "application.bootstrap",
                        self._fresh.bootstrap,
                        True,
                    ),
                    (
                        InstallPhase.RUNTIME_STARTING,
                        "runtime.start",
                        self._fresh.start_runtime,
                        True,
                    ),
                    (
                        InstallPhase.VALIDATING,
                        "runtime.validate",
                        self._fresh.validate_running_release,
                        True,
                    ),
                    (
                        InstallPhase.UPDATER_ADOPTING,
                        "updater.adopt-and-publish-locator",
                        self._fresh.adopt_updater,
                        True,
                    ),
                    (
                        InstallPhase.DOCTOR,
                        "doctor.accept",
                        self._fresh.doctor_acceptance,
                        True,
                    ),
                )
                for phase, step, operation, crosses_irreversible in schedule:
                    if crosses_irreversible and not irreversible:
                        irreversible = True
                    self._operations.phase(
                        phase,
                        mutation_occurred=mutation_occurred,
                        irreversible_mutation_started=irreversible,
                    )
                    operation(plan)
                    mutation_occurred = True
                    completed.append(step)
                    self._operations.phase(
                        phase,
                        completed_step=step,
                        mutation_occurred=True,
                        irreversible_mutation_started=irreversible,
                    )
                self._operations.succeed(completed_steps=tuple(completed))
            except Exception as error:  # noqa: BLE001 - Adapter failures are normalized
                adapter = error if isinstance(error, InstallerAdapterError) else None
                recovery_required = irreversible or bool(
                    adapter and adapter.recovery_required
                )
                error_code = adapter.code if adapter else "INSTALL_ADAPTER_FAILED"
                mutation_occurred = mutation_occurred or bool(
                    adapter and adapter.mutation_occurred
                )
                rollback_succeeded: bool | None = None
                if mutation_occurred and not recovery_required:
                    try:
                        self._fresh.cleanup_owned_staging(plan)
                        rollback_succeeded = True
                    except Exception:  # noqa: BLE001 - cleanup failure is recovery state
                        recovery_required = True
                        error_code = "INSTALL_SCOPED_CLEANUP_FAILED"
                        rollback_succeeded = False
                try:
                    self._operations.fail(
                        phase=phase,
                        error_code=error_code,
                        mutation_occurred=mutation_occurred,
                        irreversible_mutation_started=irreversible,
                        recovery_required=recovery_required,
                        rollback_succeeded=rollback_succeeded,
                    )
                except Exception:  # noqa: BLE001 - evidence failure supersedes Adapter detail
                    raise InstallerAdapterError(
                        "INSTALL_RECOVERY_EVIDENCE_FAILED",
                        mutation_occurred=mutation_occurred,
                        recovery_required=True,
                    ) from None
                raise InstallerAdapterError(
                    error_code,
                    mutation_occurred=mutation_occurred,
                    recovery_required=recovery_required,
                ) from None
        return self._result(
            plan,
            InstallOutcome.SUCCEEDED,
            state="succeeded",
            reason_code="INSTALL_FRESH_SUCCEEDED",
            completed_steps=tuple(completed),
        )

    def _revalidate(self, plan: InstallPlan) -> None:
        refreshed_release = self._resolve(
            plan.selector,
            transport_source=plan.transport_source,
            transport_policy_identity=plan.transport_policy_identity,
            refresh=True,
        )
        if refreshed_release.as_dict() != plan.release.as_dict():
            raise InstallerError(
                "INSTALL_RELEASE_CHANGED",
                outcome=InstallOutcome.VALIDATION_FAILED,
            )
        current_target = self._inspect_target()
        if current_target.as_dict() != plan.target.as_dict():
            raise InstallerError(
                "INSTALL_TARGET_CHANGED",
                outcome=InstallOutcome.VALIDATION_FAILED,
            )
        platform = self._assess_platform(plan.release.platform_profile)
        if platform.as_dict() != plan.platform.as_dict() or not platform.compatible:
            raise InstallerError(
                "INSTALL_PLATFORM_CHANGED",
                outcome=InstallOutcome.COMPATIBILITY_BLOCKED,
            )
        compatibility = self._evaluate_compatibility(refreshed_release, platform)
        if compatibility.as_dict() != plan.compatibility.as_dict():
            raise InstallerError(
                "INSTALL_COMPATIBILITY_CHANGED",
                outcome=InstallOutcome.COMPATIBILITY_BLOCKED,
            )
        try:
            self._configuration.revalidate(plan.configuration)
            if plan.restore:
                self._restore.revalidate(plan.restore)
        except InstallerError:
            raise
        except Exception:  # noqa: BLE001 - configuration Adapter is a redacted boundary
            raise InstallerError(
                "INSTALL_PLAN_STALE",
                outcome=InstallOutcome.VALIDATION_FAILED,
            ) from None

    @staticmethod
    def _validate_plan(plan: InstallPlan, accepted_plan_digest: str) -> None:
        if not isinstance(plan, InstallPlan) or not hmac.compare_digest(
            accepted_plan_digest,
            plan.plan_digest,
        ):
            raise InstallerError(
                "INSTALL_PLAN_NOT_ACCEPTED",
                outcome=InstallOutcome.VALIDATION_FAILED,
            )
        if not hmac.compare_digest(
            sha256_identity(canonical_json_bytes(plan.body())),
            plan.plan_digest,
        ):
            raise InstallerError(
                "INSTALL_PLAN_INVALID",
                outcome=InstallOutcome.VALIDATION_FAILED,
            )

    @staticmethod
    def _reject_target(target: TargetEvidence) -> None:
        codes = {
            TargetClass.FOREIGN: "INSTALL_FOREIGN_TARGET",
            TargetClass.PARTIAL_AMBIGUOUS: "INSTALL_PARTIAL_TARGET",
            TargetClass.CORRUPT: "INSTALL_CORRUPT_TARGET",
            TargetClass.ACTIVE: "INSTALL_EXISTING_INSTANCE_CONFLICT",
        }
        raise InstallerError(
            codes.get(target.classification, "INSTALL_TARGET_REJECTED"),
            outcome=InstallOutcome.VALIDATION_FAILED,
        )

    def _resolve(
        self,
        selector: ReleaseSelector,
        *,
        transport_source: InstallTransportSource,
        transport_policy_identity: str,
        refresh: bool,
    ) -> ReleaseEvidence:
        try:
            evidence = self._releases.resolve(selector, refresh=refresh)
        except InstallerError:
            raise
        except Exception:  # noqa: BLE001 - Release Adapter is a redacted boundary
            raise InstallerError(
                "INSTALL_RELEASE_VERIFICATION_FAILED",
                outcome=InstallOutcome.VALIDATION_FAILED,
            ) from None
        if not isinstance(evidence, ReleaseEvidence):
            raise InstallerError(
                "INSTALL_RELEASE_EVIDENCE_INVALID",
                outcome=InstallOutcome.VALIDATION_FAILED,
            )
        if (
            evidence.transport_source is not transport_source
            or not hmac.compare_digest(
                evidence.transport_policy_identity,
                transport_policy_identity,
            )
        ):
            raise InstallerError(
                "INSTALL_RELEASE_TRANSPORT_POLICY_MISMATCH",
                outcome=InstallOutcome.VALIDATION_FAILED,
            )
        return evidence

    def _inspect_target(self) -> TargetEvidence:
        try:
            evidence = self._target.inspect()
        except InstallerError:
            raise
        except Exception:  # noqa: BLE001 - target Adapter is a redacted boundary
            raise InstallerError(
                "INSTALL_TARGET_INSPECTION_FAILED",
                outcome=InstallOutcome.ENVIRONMENT_FAILED,
            ) from None
        if not isinstance(evidence, TargetEvidence):
            raise InstallerError(
                "INSTALL_TARGET_EVIDENCE_INVALID",
                outcome=InstallOutcome.VALIDATION_FAILED,
            )
        return evidence

    def _assess_platform(self, profile: str) -> PlatformEvidence:
        try:
            evidence = self._platform.assess(profile)
        except InstallerError:
            raise
        except Exception:  # noqa: BLE001 - platform Adapter is a redacted boundary
            raise InstallerError(
                "INSTALL_PLATFORM_EVALUATION_FAILED",
                outcome=InstallOutcome.ENVIRONMENT_FAILED,
            ) from None
        if not isinstance(evidence, PlatformEvidence):
            raise InstallerError(
                "INSTALL_PLATFORM_EVIDENCE_INVALID",
                outcome=InstallOutcome.VALIDATION_FAILED,
            )
        return evidence

    def _evaluate_compatibility(
        self,
        release: ReleaseEvidence,
        platform: PlatformEvidence,
    ) -> CompatibilityDecision:
        try:
            artifact, dimensions = self._compatibility.collect(release, platform)
            return evaluate_compatibility(
                CompatibilityOperation.INSTALL,
                artifact,
                dimensions,
            )
        except InstallerError:
            raise
        except CompatibilityEvaluationError as error:
            raise InstallerError(
                error.code,
                outcome=InstallOutcome.COMPATIBILITY_BLOCKED,
            ) from None
        except Exception:  # noqa: BLE001 - evidence collection is a redacted boundary
            raise InstallerError(
                "INSTALL_COMPATIBILITY_EVALUATION_FAILED",
                outcome=InstallOutcome.ENVIRONMENT_FAILED,
            ) from None

    def _plan_configuration(
        self,
        *,
        instance_id: str,
        public_origin: str,
        listen: ListenRequest,
        insecure_http_accepted: bool,
    ) -> ConfigPlanEvidence:
        try:
            evidence = self._configuration.plan(
                instance_id=instance_id,
                public_origin=public_origin,
                listen=listen,
                insecure_http_accepted=insecure_http_accepted,
            )
        except InstallerError:
            raise
        except Exception:  # noqa: BLE001 - configuration Adapter is a redacted boundary
            raise InstallerError(
                "INSTALL_CONFIGURATION_INVALID",
                outcome=InstallOutcome.VALIDATION_FAILED,
            ) from None
        if not isinstance(evidence, ConfigPlanEvidence):
            raise InstallerError(
                "INSTALL_CONFIG_EVIDENCE_INVALID",
                outcome=InstallOutcome.VALIDATION_FAILED,
            )
        return evidence

    def _prepare_restore(
        self,
        *,
        operation_id: str,
        backup_root: Path,
        release: ReleaseEvidence,
        target: TargetEvidence,
        platform: PlatformEvidence,
        protection: RestoreProtectionRequest | None,
    ) -> RestorePlanEvidence:
        if protection is None:
            raise InstallerError(
                "INSTALL_RESTORE_PROTECTION_REQUIRED",
                outcome=InstallOutcome.VALIDATION_FAILED,
            )
        try:
            evidence = self._restore.prepare(
                operation_id=operation_id,
                backup_root=backup_root,
                release=release,
                target=target,
                platform=platform,
                protection=protection,
            )
        except InstallerError:
            raise
        except Exception:  # noqa: BLE001 - Restore Adapter is a redacted boundary
            raise InstallerError(
                "INSTALL_RESTORE_PLAN_FAILED",
                outcome=InstallOutcome.VALIDATION_FAILED,
            ) from None
        if not isinstance(evidence, RestorePlanEvidence):
            raise InstallerError(
                "INSTALL_RESTORE_EVIDENCE_INVALID",
                outcome=InstallOutcome.VALIDATION_FAILED,
            )
        return evidence

    @staticmethod
    def _result(
        plan: InstallPlan,
        outcome: InstallOutcome,
        *,
        state: str,
        reason_code: str,
        completed_steps: tuple[str, ...] = (),
    ) -> InstallResult:
        return InstallResult(
            outcome=outcome,
            operation_id=plan.operation_id,
            mode=plan.mode,
            instance_id=plan.configuration.instance_id,
            release=plan.release.as_dict(),
            state=state,
            reason_code=reason_code,
            warnings=plan.warnings,
            recovery_required=outcome is InstallOutcome.RECOVERY_REQUIRED,
            completed_steps=completed_steps,
        )
