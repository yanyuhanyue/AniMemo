"""Concrete canonical-host adapters for Installer Runtime v1.

The domain state machine remains in :mod:`installer.runtime`.  This module owns
only fixed-path OS, Release, Compose, Updater, and protected-file boundaries.
"""

from __future__ import annotations

import base64
import ipaddress
import os
import platform as host_platform
import secrets
import shutil
import socket
import stat
import tempfile
import uuid
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from datetime import datetime, timezone
from pathlib import Path

from durability.canonical import canonical_json_bytes, sha256_identity
from durability.compatibility import (
    ArtifactIdentity,
    CompatibilityOutcome,
    Dimension,
    DimensionAssessment,
    ReasonCode,
)
from durability.doctor import (
    CompatibilityEvidence,
    DoctorRunner,
    DoctorStatus,
    ProbeResult,
)
from durability.instance import (
    APP_ROOT,
    DATA_ROOT,
    INSTANCE_LOCATOR_PATH,
    MANAGED_CONFIG_PATH,
    UPDATER_APP_ROOT,
    UPDATER_RUNTIME_ROOT,
    UPDATER_STATE_ROOT,
    InstanceLocator,
    ListenIdentity,
    LocatorError,
    load_instance_snapshot,
    release_identity_from_manifest,
)
from durability.managed_config import (
    ApplicationConfig,
    DatabaseConfig,
    DirectAccessConfig,
    IntegrationConfig,
    ListenConfig,
    LocalManagedConfigStore,
    ManagedConfig,
    ManagedConfigError,
    RedisConfig,
    TrustedOriginsConfig,
    canonical_public_origin,
    derive_runtime_environment,
)
from durability.platform import (
    REQUIRED_CAPABILITIES,
    DatabasePathEvidence,
    HostCapabilityEvidence,
    PlatformQualification,
    PlatformQualificationError,
    assess_platform,
    parse_platform_qualification,
)
from installer.operations import (
    FreshInstallOperation,
    FreshInstallOperationJournal,
    FreshInstallPhase,
    create_fresh_install_operation,
    fail_fresh_install,
    mark_irreversible_mutation_started,
    mark_mutation_started,
    succeed_fresh_install,
    transition_phase,
)
from release.contract import validate_manifest
from updater import __version__ as updater_version
from updater.commands import CommandRunner
from updater.deployment import HostPaths, ImmutableComposeDeployment
from updater.errors import StateError
from updater.runtime import InitialAdoptionRequest, adopt_initial_release
from updater.runtime_state import RuntimeState
from updater.slots import ReleaseSlots
from updater.source import GitHubReleaseSource, VerifiedReleaseMaterials
from updater.state import OperationStore, UpdateLock

from .restore_production import ProductionRestoreRuntimePort
from .runtime import (
    ConfigPlanEvidence,
    Installer,
    InstallerAdapterError,
    InstallerError,
    InstallOutcome,
    InstallPhase,
    InstallPlan,
    ListenRequest,
    PlatformEvidence,
    ReleaseEvidence,
    ReleaseSelector,
    TargetClass,
    TargetEvidence,
)

_CANONICAL_ROOTS = tuple(
    Path(str(path))
    for path in (
        APP_ROOT,
        DATA_ROOT,
        UPDATER_APP_ROOT,
        UPDATER_STATE_ROOT,
        UPDATER_RUNTIME_ROOT,
    )
)
_PLATFORM_EVIDENCE_MATERIAL = "release/platform-qualification.json"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _manifest_digest(manifest: Mapping[str, object]) -> str:
    return sha256_identity(canonical_json_bytes(dict(manifest)))


def _safe_adapter_error(code: str, *, mutation: bool, recovery: bool):
    raise InstallerAdapterError(
        code,
        mutation_occurred=mutation,
        recovery_required=recovery,
    )


class ProductionReleasePort:
    """Resolve the highest requested candidate and retain its verified bytes."""

    def __init__(
        self,
        *,
        source: GitHubReleaseSource | None = None,
        cache_root: Path | None = None,
    ) -> None:
        self._temporary: tempfile.TemporaryDirectory[str] | None = None
        if source is None:
            if cache_root is None:
                self._temporary = tempfile.TemporaryDirectory(
                    prefix="animemo-installer-release-"
                )
                cache_root = Path(self._temporary.name) / "cache"
            source = GitHubReleaseSource(cache_root)
        self.source = source
        self._materials: dict[str, VerifiedReleaseMaterials] = {}
        self._latest: VerifiedReleaseMaterials | None = None
        self._latest_evidence: ReleaseEvidence | None = None

    def resolve(
        self,
        selector: ReleaseSelector,
        *,
        refresh: bool,
    ) -> ReleaseEvidence:
        if selector.version is not None:
            version = selector.version
        else:
            assert selector.channel is not None
            candidates = self.source.list_releases(selector.channel, refresh=refresh)
            if not candidates:
                raise InstallerError(
                    "INSTALL_RELEASE_NOT_FOUND",
                    outcome=InstallOutcome.VALIDATION_FAILED,
                )
            version = str(candidates[0]["version"])
        try:
            materials = self.source.fetch_verified_materials(
                version,
                updater_version=updater_version,
                refresh=refresh,
            )
            manifest = materials.manifest
            validate_manifest(manifest, updater_version=updater_version)
        except Exception:  # noqa: BLE001 - Release authority boundary is redacted
            raise InstallerError(
                "INSTALL_RELEASE_VERIFICATION_FAILED",
                outcome=InstallOutcome.VALIDATION_FAILED,
            ) from None
        if selector.channel is not None:
            requested = selector.channel
            actual = str(manifest["release"]["channel"])
            if (requested == "stable" and actual != "stable") or (
                requested == "rc" and actual not in {"stable", "rc"}
            ):
                raise InstallerError(
                    "INSTALL_RELEASE_CHANNEL_MISMATCH",
                    outcome=InstallOutcome.VALIDATION_FAILED,
                )
        evidence = ReleaseEvidence(
            version=str(manifest["release"]["version"]),
            channel=str(manifest["release"]["channel"]),
            commit=str(manifest["release"]["commit"]),
            manifest_digest=_manifest_digest(manifest),
            material_identity_digest=materials.identity_digest,
            deployment_identity_digest=str(
                manifest["deployment"]["contractSha256"]
            ),
            deployment_profile=str(manifest["deployment"]["profile"]),
            platform_profile="v1.1-standard-linux-amd64",
        )
        self._materials[evidence.manifest_digest] = materials
        self._latest = materials
        self._latest_evidence = evidence
        return evidence

    def materials_for(self, evidence: ReleaseEvidence) -> VerifiedReleaseMaterials:
        materials = self._materials.get(evidence.manifest_digest)
        if materials is None:
            refreshed = self.resolve(
                ReleaseSelector(version=evidence.version),
                refresh=True,
            )
            if refreshed.as_dict() != evidence.as_dict():
                raise InstallerError(
                    "INSTALL_RELEASE_CHANGED",
                    outcome=InstallOutcome.VALIDATION_FAILED,
                )
            materials = self._materials[evidence.manifest_digest]
        for identity in materials.verified.files:
            materials.material(identity.path)
        return materials

    def latest_materials(self) -> VerifiedReleaseMaterials:
        if self._latest is None:
            raise InstallerError(
                "INSTALL_RELEASE_NOT_RESOLVED",
                outcome=InstallOutcome.ENVIRONMENT_FAILED,
            )
        return self._latest

    def latest_evidence(self) -> ReleaseEvidence:
        if self._latest_evidence is None:
            raise InstallerError(
                "INSTALL_RELEASE_NOT_RESOLVED",
                outcome=InstallOutcome.ENVIRONMENT_FAILED,
            )
        return self._latest_evidence


class ProductionCompatibilityPort:
    """Collect verified facts; Installer invokes the canonical evaluator."""

    def __init__(self, releases: ProductionReleasePort) -> None:
        self.releases = releases

    def collect(self, release: ReleaseEvidence, platform: PlatformEvidence):
        materials = self.releases.materials_for(release)
        manifest = materials.manifest
        compatible_reasons = {
            Dimension.FORMAT: ReasonCode.FORMAT_SUPPORTED,
            Dimension.INTEGRITY_AUTHENTICATION: ReasonCode.INTEGRITY_AUTHENTICATED,
            Dimension.DEPLOYMENT_CONTRACT: ReasonCode.DEPLOYMENT_CONTRACT_SUPPORTED,
            Dimension.SCHEMA_CONTRACTS: ReasonCode.SCHEMA_CONTRACTS_SUPPORTED,
            Dimension.EXACT_RELEASE_IDENTITY: ReasonCode.RELEASE_IDENTITY_VERIFIED,
            Dimension.PLATFORM_RUNTIME: ReasonCode.PLATFORM_RUNTIME_SUPPORTED,
            Dimension.SUPPORTED_PATH: ReasonCode.DIRECT_PATH_SUPPORTED,
        }
        source = {
            Dimension.FORMAT: {"schemaVersion": manifest["schemaVersion"]},
            Dimension.INTEGRITY_AUTHENTICATION: {
                "manifestDigest": release.manifest_digest,
                "materialIdentityDigest": release.material_identity_digest,
            },
            Dimension.DEPLOYMENT_CONTRACT: {
                "profile": release.deployment_profile,
                "digest": release.deployment_identity_digest,
            },
            Dimension.SCHEMA_CONTRACTS: manifest["compatibility"],
            Dimension.EXACT_RELEASE_IDENTITY: manifest["release"],
            Dimension.PLATFORM_RUNTIME: {
                "qualificationDigest": platform.evidence_digest
            },
            Dimension.SUPPORTED_PATH: {"mode": "fresh-or-restore-to-new"},
        }
        dimensions = tuple(
            DimensionAssessment(
                name=dimension,
                outcome=CompatibilityOutcome.COMPATIBLE,
                reason_code=compatible_reasons[dimension],
                source=source[dimension],
                target={"profile": "v1.1-standard"},
            )
            for dimension in Dimension
        )
        return (
            ArtifactIdentity(
                format_identity="animemo.release-materials",
                format_version=2,
                artifact_id=release.version,
                manifest_digest=release.manifest_digest,
            ),
            dimensions,
        )


def _command_available(runner: CommandRunner, argv: list[str]) -> bool:
    try:
        runner.run(argv, timeout=30)
    except Exception:  # noqa: BLE001 - capability probe returns only a boolean
        return False
    return True


def _filesystem_capabilities() -> dict[str, bool]:
    result = {
        "directory_fsync": False,
        "file_fsync": False,
        "nofollow_regular_file": hasattr(os, "O_NOFOLLOW"),
        "posix_owner_mode": os.name == "posix",
        "same_directory_atomic_replace": False,
        "single_link_file": False,
        "unix_socket_permissions": hasattr(socket, "AF_UNIX"),
    }
    try:
        with tempfile.TemporaryDirectory(prefix="animemo-platform-") as directory:
            root = Path(directory)
            first = root / "first"
            second = root / "second"
            first.write_bytes(b"first")
            with first.open("rb") as handle:
                os.fsync(handle.fileno())
            result["file_fsync"] = True
            result["single_link_file"] = first.stat().st_nlink == 1
            second.write_bytes(b"second")
            os.replace(second, first)
            result["same_directory_atomic_replace"] = first.read_bytes() == b"second"
            if os.name == "posix":
                descriptor = os.open(root, os.O_RDONLY)
                try:
                    os.fsync(descriptor)
                    result["directory_fsync"] = True
                finally:
                    os.close(descriptor)
    except OSError:
        pass
    return result


def collect_host_capabilities(
    qualification: PlatformQualification,
    *,
    runner: CommandRunner | None = None,
) -> HostCapabilityEvidence:
    runner = runner or CommandRunner()
    machine = host_platform.machine().lower()
    architecture = "amd64" if machine in {"x86_64", "amd64"} else machine
    filesystem = _filesystem_capabilities()
    compose_version = _command_available(runner, ["/usr/bin/docker", "compose", "version"])
    compose_help = _command_available(runner, ["/usr/bin/docker", "compose", "up", "--help"])
    docker = _command_available(runner, ["/usr/bin/docker", "info"])
    systemd = _command_available(runner, ["/usr/bin/systemctl", "--version"])
    pg_dump = _command_available(runner, ["/usr/bin/pg_dump", "--version"])
    psql = _command_available(runner, ["/usr/bin/psql", "--version"])
    loopback = False
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", 0))
        loopback = True
    except OSError:
        pass
    capabilities = {
        **filesystem,
        "compose_profiles": compose_version,
        "compose_v2": compose_version,
        "compose_wait": compose_help,
        "docker_daemon": docker,
        "immutable_image_digest": True,
        "loopback_port_binding": loopback,
        "postgres_plain_dump": pg_dump,
        "postgres_psql_restore": psql,
        "systemd_unit_lifecycle": systemd,
    }
    if set(capabilities) != set(REQUIRED_CAPABILITIES):
        raise PlatformQualificationError("PLATFORM_HOST_EVIDENCE_INVALID")
    database_path = DatabasePathEvidence(
        dump_format="plain",
        source_server_major=qualification.database_path.source_server_major,
        pg_dump_major=qualification.database_path.pg_dump_major,
        psql_major=qualification.database_path.psql_major,
        target_server_major=qualification.database_path.target_server_major,
    )
    return HostCapabilityEvidence(
        os=host_platform.system().lower(),
        architecture=architecture,
        profile=qualification.profile,
        capabilities=capabilities,
        database_path=database_path,
    )


class ProductionPlatformPort:
    def __init__(
        self,
        releases: ProductionReleasePort,
        *,
        collector: Callable[[PlatformQualification], HostCapabilityEvidence]
        | None = None,
    ) -> None:
        self.releases = releases
        self.collector = collector or collect_host_capabilities

    def assess(self, profile: str) -> PlatformEvidence:
        try:
            materials = self.releases.latest_materials()
            qualification = parse_platform_qualification(
                materials.material(_PLATFORM_EVIDENCE_MATERIAL).read_bytes()
            )
            manifest = materials.manifest
            if (
                qualification.profile != profile
                or qualification.candidate_sha != manifest["release"]["commit"]
                or qualification.image_digests["postgres"]
                != materials.image("postgres")
                or qualification.image_digests["redis"] != materials.image("redis")
            ):
                raise PlatformQualificationError("PLATFORM_CANDIDATE_MISMATCH")
            assessment = assess_platform(self.collector(qualification), qualification)
        except (OSError, ValueError, PlatformQualificationError):
            raise InstallerError(
                "INSTALL_PLATFORM_EVALUATION_FAILED",
                outcome=InstallOutcome.ENVIRONMENT_FAILED,
            ) from None
        return PlatformEvidence(
            compatible=assessment.outcome is CompatibilityOutcome.COMPATIBLE,
            profile=qualification.profile,
            evidence_digest=qualification.evidence_digest,
            reason_code=assessment.reason_code.value,
        )


class ProductionTargetPort:
    def __init__(
        self,
        *,
        releases: ProductionReleasePort | None = None,
        platform: ProductionPlatformPort | None = None,
        doctor: ProductionDoctorAcceptance | None = None,
        runner: CommandRunner | None = None,
    ) -> None:
        self.releases = releases
        self.platform = platform
        self.doctor = doctor
        self.runner = runner or CommandRunner()

    @staticmethod
    def _installed_material_exact(materials: VerifiedReleaseMaterials) -> bool:
        try:
            for identity in materials.verified.files:
                expected = materials.material(identity.path)
                installed = Path(str(APP_ROOT)) / Path(identity.path)
                metadata = installed.lstat()
                if (
                    installed.is_symlink()
                    or not stat.S_ISREG(metadata.st_mode)
                    or metadata.st_nlink != 1
                    or metadata.st_size != identity.size
                    or stat.S_IMODE(metadata.st_mode) != identity.mode
                    or sha256_identity(installed.read_bytes())
                    != sha256_identity(expected.read_bytes())
                ):
                    return False
        except (OSError, ValueError):
            return False
        return True

    def inspect(self) -> TargetEvidence:
        locator_path = Path(str(INSTANCE_LOCATOR_PATH))
        if locator_path.exists() or locator_path.is_symlink():
            try:
                snapshot = load_instance_snapshot()
                config = LocalManagedConfigStore().read()
                locator = snapshot.locator
                if (
                    config.instance_id != locator.instance_id
                    or config.config_revision != locator.config_revision
                    or config.public_origin != locator.public_origin
                    or config.listen.host != locator.listen.host
                    or config.listen.port != locator.listen.port
                ):
                    raise ManagedConfigError("CONFIG_LOCATOR_MISMATCH")
                evidence = {
                    "locator": snapshot.digest,
                    "configRevision": config.config_revision,
                }
                material_identity_digest = None
                exact_release_running = False
                doctor_complete = False
                if (
                    self.releases is not None
                    and self.platform is not None
                    and self.doctor is not None
                ):
                    selected = self.releases.latest_evidence()
                    materials = self.releases.materials_for(selected)
                    locator_matches = dict(locator.release_identity) == dict(
                        release_identity_from_manifest(materials.manifest)
                    )
                    if locator_matches and self._installed_material_exact(materials):
                        material_identity_digest = selected.material_identity_digest
                        try:
                            platform = self.platform.assess(selected.platform_profile)
                            if not platform.compatible:
                                raise PlatformQualificationError(
                                    "PLATFORM_RUNTIME_UNSUPPORTED"
                                )
                            deployment = ImmutableComposeDeployment(
                                HostPaths.production(snapshot),
                                runner=self.runner,
                                managed_environment=dict(
                                    derive_runtime_environment(config)
                                ),
                            )
                            self.doctor.accept_existing(
                                expected_instance_id=locator.instance_id,
                                release=selected,
                                platform=platform,
                                deployment=deployment,
                            )
                            exact_release_running = True
                            doctor_complete = True
                        except Exception:  # noqa: BLE001 - health is evidence only
                            exact_release_running = False
                            doctor_complete = False
                return TargetEvidence(
                    TargetClass.ACTIVE,
                    sha256_identity(canonical_json_bytes(evidence)),
                    instance_id=locator.instance_id,
                    release_manifest_digest=str(
                        locator.release_identity["manifestDigest"]
                    ),
                    material_identity_digest=material_identity_digest,
                    config_revision=config.config_revision,
                    public_origin=config.public_origin,
                    listen_host=config.listen.host,
                    listen_port=config.listen.port,
                    exact_release_running=exact_release_running,
                    doctor_complete=doctor_complete,
                )
            except (LocatorError, ManagedConfigError, OSError, ValueError):
                return TargetEvidence(
                    TargetClass.CORRUPT,
                    sha256_identity(b"canonical-locator-corrupt"),
                )

        states: list[str] = []
        for root in _CANONICAL_ROOTS:
            try:
                metadata = root.lstat()
            except FileNotFoundError:
                states.append("absent")
                continue
            except OSError:
                return TargetEvidence(
                    TargetClass.CORRUPT,
                    sha256_identity(b"canonical-root-unreadable"),
                )
            if root.is_symlink() or not stat.S_ISDIR(metadata.st_mode):
                return TargetEvidence(
                    TargetClass.FOREIGN,
                    sha256_identity(b"canonical-root-foreign"),
                )
            try:
                empty = next(root.iterdir(), None) is None
            except OSError:
                empty = False
            states.append("empty" if empty else "data")
        evidence_digest = sha256_identity(canonical_json_bytes(states))
        if all(item == "absent" for item in states):
            return TargetEvidence(TargetClass.ABSENT, evidence_digest)
        if all(item in {"absent", "empty"} for item in states):
            return TargetEvidence(TargetClass.VERIFIED_EMPTY, evidence_digest)
        return TargetEvidence(TargetClass.PARTIAL_AMBIGUOUS, evidence_digest)


class ProductionManagedConfigurationPort:
    def __init__(self, store: LocalManagedConfigStore | None = None) -> None:
        self.store = store or LocalManagedConfigStore()
        self._planned: dict[str, ManagedConfig] = {}
        self._existing: dict[str, bool] = {}

    @staticmethod
    def _fresh_config(
        *,
        instance_id: str,
        revision: str,
        public_origin: str,
        listen: ListenRequest,
        insecure_http_accepted: bool,
    ) -> ManagedConfig:
        origin = canonical_public_origin(public_origin)
        direct = not ipaddress.ip_address(listen.host).is_loopback
        insecure = origin.startswith("http://")
        if direct != listen.direct_exposure_accepted or insecure != insecure_http_accepted:
            raise ManagedConfigError("CONFIG_DIRECT_ACCESS_ACK_REQUIRED")
        return ManagedConfig(
            instance_id=instance_id,
            config_revision=revision,
            listen=ListenConfig(listen.host, listen.port),
            public_origin=origin,
            direct_access=DirectAccessConfig(
                allow_non_loopback=direct,
                allow_http=insecure,
                warning_acknowledged=direct or insecure,
            ),
            trusted_origins=TrustedOriginsConfig((), (), ()),
            database=DatabaseConfig(
                "animemo",
                "animemo",
                secrets.token_urlsafe(48),
            ),
            redis=RedisConfig("redis://redis:6379/0"),
            application=ApplicationConfig(
                django_secret_key=secrets.token_urlsafe(64),
                credential_encryption_key=base64.urlsafe_b64encode(
                    secrets.token_bytes(32)
                ).decode("ascii"),
            ),
            integrations=IntegrationConfig("", "", ""),
        )

    def plan(
        self,
        *,
        instance_id: str,
        public_origin: str,
        listen: ListenRequest,
        insecure_http_accepted: bool,
    ) -> ConfigPlanEvidence:
        exists = self.store.authority_path.exists() or self.store.authority_path.is_symlink()
        if exists:
            config = self.store.read()
            if (
                config.instance_id != instance_id
                or config.public_origin != canonical_public_origin(public_origin)
                or config.listen != ListenConfig(listen.host, listen.port)
            ):
                raise ManagedConfigError("CONFIG_EXISTING_CONFLICT")
        else:
            config = self._fresh_config(
                instance_id=instance_id,
                revision=str(uuid.uuid4()),
                public_origin=public_origin,
                listen=listen,
                insecure_http_accepted=insecure_http_accepted,
            )
        safe_identity = sha256_identity(
            canonical_json_bytes(config.secret_safe_dict())
        )
        warnings = tuple(
            warning
            for condition, warning in (
                (not config.listen.is_loopback, "DIRECT_LISTEN_EXPOSURE"),
                (config.public_origin.startswith("http://"), "INSECURE_HTTP_ORIGIN"),
            )
            if condition
        )
        evidence = ConfigPlanEvidence(
            instance_id=config.instance_id,
            config_revision=config.config_revision,
            public_origin=config.public_origin,
            listen_host=config.listen.host,
            listen_port=config.listen.port,
            exposure="loopback" if config.listen.is_loopback else "direct",
            non_secret_identity_digest=safe_identity,
            warnings=warnings,
        )
        self._planned[evidence.config_revision] = config
        self._existing[evidence.config_revision] = exists
        return evidence

    def revalidate(self, plan: ConfigPlanEvidence) -> None:
        config = self._planned.get(plan.config_revision)
        if config is None or sha256_identity(
            canonical_json_bytes(config.secret_safe_dict())
        ) != plan.non_secret_identity_digest:
            raise ManagedConfigError("CONFIG_PLAN_STALE")
        exists = self.store.authority_path.exists() or self.store.authority_path.is_symlink()
        if exists != self._existing[plan.config_revision]:
            raise ManagedConfigError("CONFIG_PLAN_STALE")
        if exists and self.store.read() != config:
            raise ManagedConfigError("CONFIG_PLAN_STALE")

    def config_for(self, evidence: ConfigPlanEvidence) -> ManagedConfig:
        self.revalidate(evidence)
        return self._planned[evidence.config_revision]

    def replace_planned_config(
        self,
        evidence: ConfigPlanEvidence,
        config: ManagedConfig,
    ) -> ConfigPlanEvidence:
        """Bind authenticated Restore secrets without exposing their values."""

        current = self.config_for(evidence)
        if (
            config.instance_id != current.instance_id
            or config.config_revision != current.config_revision
            or config.public_origin != current.public_origin
            or config.listen != current.listen
            or config.database != current.database
            or config.redis != current.redis
        ):
            raise ManagedConfigError("CONFIG_RESTORE_BINDING_INVALID")
        updated = ConfigPlanEvidence(
            instance_id=config.instance_id,
            config_revision=config.config_revision,
            public_origin=config.public_origin,
            listen_host=config.listen.host,
            listen_port=config.listen.port,
            exposure=evidence.exposure,
            non_secret_identity_digest=sha256_identity(
                canonical_json_bytes(config.secret_safe_dict())
            ),
            warnings=evidence.warnings,
        )
        self._planned[config.config_revision] = config
        return updated

    def publish(self, evidence: ConfigPlanEvidence) -> None:
        config = self.config_for(evidence)
        if self._existing[evidence.config_revision]:
            return
        self.store.write(config, expected_revision=None, must_not_exist=True)


_PHASES = {
    phase.value: FreshInstallPhase(phase.value)
    for phase in InstallPhase
    if phase is not InstallPhase.SUCCEEDED
}


class ProductionOperationPort:
    def __init__(
        self,
        *,
        state_root: Path = Path(str(UPDATER_STATE_ROOT)),
    ) -> None:
        self.journal = FreshInstallOperationJournal(state_root)
        self.lock_path = state_root / "update.lock"
        self.current: FreshInstallOperation | None = None

    def acquire_lock(self, operation_id: str) -> AbstractContextManager[None]:
        del operation_id
        return UpdateLock(self.lock_path)

    def begin(self, plan: InstallPlan) -> None:
        self.journal.require_recovery_clear()
        operation = create_fresh_install_operation(
            operation_id=plan.operation_id,
            instance_id=plan.configuration.instance_id,
            plan_digest=plan.plan_digest,
            release_identity_digest=plan.release.material_identity_digest,
            deployment_identity_digest=plan.release.deployment_identity_digest,
            config_revision=plan.configuration.config_revision,
            at=_utc_now(),
        )
        self.journal.create(operation)
        self.current = operation

    def _persist(self, updated: FreshInstallOperation) -> None:
        if self.current is None:
            raise StateError("Fresh operation is not initialized")
        self.journal.persist(self.current, updated)
        self.current = updated

    def phase(
        self,
        phase: InstallPhase,
        *,
        completed_step: str | None = None,
        mutation_occurred: bool,
        irreversible_mutation_started: bool,
    ) -> None:
        del completed_step
        if self.current is None:
            raise StateError("Fresh operation is not initialized")
        target = _PHASES[phase.value]
        if self.current.phase is not target:
            self._persist(transition_phase(self.current, target, at=_utc_now()))
        if mutation_occurred and not self.current.mutation_occurred:
            self._persist(mark_mutation_started(self.current, at=_utc_now()))
        if (
            irreversible_mutation_started
            and not self.current.irreversible_mutation_started
        ):
            self._persist(
                mark_irreversible_mutation_started(self.current, at=_utc_now())
            )

    def fail(
        self,
        *,
        phase: InstallPhase,
        error_code: str,
        mutation_occurred: bool,
        irreversible_mutation_started: bool,
        recovery_required: bool,
        rollback_succeeded: bool | None,
    ) -> None:
        self.phase(
            phase,
            mutation_occurred=mutation_occurred,
            irreversible_mutation_started=irreversible_mutation_started,
        )
        assert self.current is not None
        self._persist(
            fail_fresh_install(
                self.current,
                error_code=error_code,
                at=_utc_now(),
                rollback_succeeded=(
                    False if recovery_required else rollback_succeeded
                ),
            )
        )

    def succeed(self, *, completed_steps: tuple[str, ...]) -> None:
        del completed_steps
        if self.current is None:
            raise StateError("Fresh operation is not initialized")
        self._persist(succeed_fresh_install(self.current, at=_utc_now()))


class ProductionDoctorAcceptance:
    """Complete, read-only Doctor probe set for the canonical production host."""

    def __init__(
        self,
        *,
        releases: ProductionReleasePort,
        compatibility: ProductionCompatibilityPort,
        runner: CommandRunner | None = None,
    ) -> None:
        self.releases = releases
        self.compatibility = compatibility
        self.runner = runner or CommandRunner()

    @staticmethod
    def _guarded(
        passed_code: str,
        failed_code: str,
        check: Callable[[], bool | None],
    ) -> Callable[[InstanceLocator], ProbeResult]:
        def probe(_locator: InstanceLocator) -> ProbeResult:
            try:
                result = check()
            except Exception:  # noqa: BLE001 - Doctor probes return stable failure only
                return ProbeResult.failed(failed_code)
            return (
                ProbeResult.passed(passed_code)
                if result is not False
                else ProbeResult.failed(failed_code)
            )

        return probe

    def __call__(
        self,
        plan: InstallPlan,
        deployment: ImmutableComposeDeployment,
    ) -> None:
        self._accept(
            expected_instance_id=plan.configuration.instance_id,
            release=plan.release,
            platform=plan.platform,
            deployment=deployment,
        )

    def accept_existing(
        self,
        *,
        expected_instance_id: str,
        release: ReleaseEvidence,
        platform: PlatformEvidence,
        deployment: ImmutableComposeDeployment,
    ) -> None:
        self._accept(
            expected_instance_id=expected_instance_id,
            release=release,
            platform=platform,
            deployment=deployment,
        )

    def _accept(
        self,
        *,
        expected_instance_id: str,
        release: ReleaseEvidence,
        platform: PlatformEvidence,
        deployment: ImmutableComposeDeployment,
    ) -> None:
        materials = self.releases.materials_for(release)
        manifest = materials.manifest
        config_store = LocalManagedConfigStore()

        def config() -> ManagedConfig:
            return config_store.read()

        def config_required() -> bool:
            current = config()
            metadata = config_store.authority_path.lstat()
            if os.name != "posix":
                return False
            import grp
            import pwd

            return (
                current.instance_id == expected_instance_id
                and stat.S_ISREG(metadata.st_mode)
                and metadata.st_nlink == 1
                and stat.S_IMODE(metadata.st_mode) == 0o600
                and metadata.st_uid == pwd.getpwnam("animemo-updater").pw_uid
                and metadata.st_gid == grp.getgrnam("animemo-api").gr_gid
            )

        def alignment() -> bool:
            snapshot = load_instance_snapshot()
            current = config()
            locator = snapshot.locator
            return (
                locator.instance_id == current.instance_id
                and locator.config_revision == current.config_revision
                and locator.public_origin == current.public_origin
                and locator.listen.host == current.listen.host
                and locator.listen.port == current.listen.port
            )

        def systemd_allowlist() -> bool:
            expected = materials.material(
                "deploy/updater/animemo-updater.service"
            ).read_bytes()
            installed = Path(
                "/etc/systemd/system/animemo-updater.service"
            ).read_bytes()
            result = self.runner.run(
                [
                    "/usr/bin/systemctl",
                    "is-active",
                    "animemo-updater.service",
                ],
                timeout=30,
            )
            return installed == expected and result.stdout.strip() == "active"

        def listen() -> bool:
            current = config()
            host = current.listen.host
            address = ipaddress.ip_address(host)
            probe_host = (
                "127.0.0.1"
                if address.is_unspecified and address.version == 4
                else "::1"
                if address.is_unspecified
                else address.compressed
            )
            with socket.create_connection(
                (probe_host, current.listen.port), timeout=5
            ):
                return True

        def updater_socket() -> bool:
            path = Path(str(UPDATER_RUNTIME_ROOT / "updater.sock"))
            metadata = path.lstat()
            return stat.S_ISSOCK(metadata.st_mode) and stat.S_IMODE(
                metadata.st_mode
            ) == 0o660

        def updater_state() -> bool:
            state_root = Path(str(UPDATER_STATE_ROOT))
            slots = ReleaseSlots(state_root / "releases").read()
            RuntimeState(state_root).read()
            OperationStore(state_root).require_recovery_clear()
            return slots["current"] == manifest

        def release_consistency() -> bool:
            snapshot = load_instance_snapshot()
            return (
                dict(snapshot.locator.release_identity)
                == dict(release_identity_from_manifest(manifest))
                and ReleaseSlots(Path(str(UPDATER_STATE_ROOT / "releases"))).read()[
                    "current"
                ]
                == manifest
            )

        def plugins() -> bool:
            enabled = deployment.inspect_enabled_plugin_apis(manifest)
            supported = set(
                manifest["compatibility"]["pluginSdk"]["supportedApis"]
            )
            return enabled.issubset(supported)

        def safe_directory(path: Path, mode: int) -> bool:
            metadata = path.lstat()
            return (
                not path.is_symlink()
                and stat.S_ISDIR(metadata.st_mode)
                and stat.S_IMODE(metadata.st_mode) == mode
            )

        probes = {
            "filesystem.capacity": self._guarded(
                "FILESYSTEM_CAPACITY_AVAILABLE",
                "FILESYSTEM_CAPACITY_UNAVAILABLE",
                lambda: shutil.disk_usage(Path(str(DATA_ROOT))).free > 0,
            ),
            "configuration.required": self._guarded(
                "CONFIGURATION_REQUIRED_VALID",
                "CONFIGURATION_REQUIRED_INVALID",
                config_required,
            ),
            "configuration.alignment": self._guarded(
                "CONFIGURATION_ALIGNED",
                "CONFIGURATION_ALIGNMENT_FAILED",
                alignment,
            ),
            "systemd.allowlist": self._guarded(
                "SYSTEMD_ALLOWLIST_VALID",
                "SYSTEMD_ALLOWLIST_INVALID",
                systemd_allowlist,
            ),
            "compose.alignment": self._guarded(
                "COMPOSE_ALIGNED",
                "COMPOSE_ALIGNMENT_FAILED",
                lambda: (
                    deployment.verify_deployment_contract(manifest),
                    deployment.validate_compose(manifest),
                )
                is not None,
            ),
            "network.listen": self._guarded(
                "LISTEN_REACHABLE", "LISTEN_UNREACHABLE", listen
            ),
            "identity.public-origin": self._guarded(
                "PUBLIC_ORIGIN_VALID",
                "PUBLIC_ORIGIN_INVALID",
                lambda: config().public_origin
                == canonical_public_origin(config().public_origin),
            ),
            "database.postgresql.connectivity": self._guarded(
                "POSTGRESQL_REACHABLE",
                "POSTGRESQL_UNREACHABLE",
                lambda: deployment.probe_postgres(manifest) is None,
            ),
            "database.schema-compatibility": self._guarded(
                "DATABASE_SCHEMA_COMPATIBLE",
                "DATABASE_SCHEMA_INCOMPATIBLE",
                lambda: deployment.inspect_runtime_contracts(manifest)[
                    "databaseContract"
                ]
                == manifest["compatibility"]["database"]["contract"],
            ),
            "cache.redis.connectivity": self._guarded(
                "REDIS_REACHABLE",
                "REDIS_UNREACHABLE",
                lambda: deployment.probe_redis(manifest) is None,
            ),
            "cache.redis.persistence-contract": self._guarded(
                "REDIS_PERSISTENCE_VALID",
                "REDIS_PERSISTENCE_INVALID",
                lambda: deployment.validate_compose(manifest) is None,
            ),
            "service.api.health": self._guarded(
                "API_HEALTHY",
                "API_UNHEALTHY",
                lambda: deployment.probe_api(manifest) is None,
            ),
            "service.web.health": self._guarded(
                "WEB_HEALTHY",
                "WEB_UNHEALTHY",
                lambda: deployment.probe_web(manifest) is None,
            ),
            "updater.socket": self._guarded(
                "UPDATER_SOCKET_VALID",
                "UPDATER_SOCKET_INVALID",
                updater_socket,
            ),
            "updater.state": self._guarded(
                "UPDATER_STATE_VALID", "UPDATER_STATE_INVALID", updater_state
            ),
            "release.identity": self._guarded(
                "RELEASE_IDENTITY_VALID",
                "RELEASE_IDENTITY_INVALID",
                lambda: deployment.verify_health(manifest) is None,
            ),
            "release.updater-consistency": self._guarded(
                "RELEASE_UPDATER_CONSISTENT",
                "RELEASE_UPDATER_INCONSISTENT",
                release_consistency,
            ),
            "plugins.integrity": self._guarded(
                "PLUGINS_INTEGRITY_VALID", "PLUGINS_INTEGRITY_INVALID", plugins
            ),
            "media.integrity": self._guarded(
                "MEDIA_INTEGRITY_VALID",
                "MEDIA_INTEGRITY_INVALID",
                lambda: safe_directory(Path(str(DATA_ROOT / "media")), 0o755),
            ),
            "backup.readiness": self._guarded(
                "BACKUP_READY",
                "BACKUP_NOT_READY",
                lambda: safe_directory(Path(str(DATA_ROOT / "backups")), 0o770),
            ),
        }
        artifact, dimensions = self.compatibility.collect(release, platform)
        report = DoctorRunner(
            probes=probes,
            compatibility=CompatibilityEvidence(artifact, dimensions),
            clock=_utc_now,
        ).run()
        if report.overall_status is not DoctorStatus.PASS or any(
            check.status is not DoctorStatus.PASS for check in report.checks
        ):
            raise InstallerAdapterError(
                "INSTALL_DOCTOR_INCOMPLETE",
                mutation_occurred=True,
                recovery_required=True,
            )


class ProductionFreshInstallPort:
    def __init__(
        self,
        *,
        releases: ProductionReleasePort,
        configuration: ProductionManagedConfigurationPort,
        runner: CommandRunner | None = None,
        doctor_acceptor: Callable[[InstallPlan, ImmutableComposeDeployment], None]
        | None = None,
    ) -> None:
        self.releases = releases
        self.configuration = configuration
        self.runner = runner or CommandRunner()
        self.doctor_acceptor = doctor_acceptor
        self._deployment: ImmutableComposeDeployment | None = None
        self._created: set[Path] = set()

    def _manifest(self, plan: InstallPlan) -> dict[str, object]:
        return self.releases.materials_for(plan.release).manifest

    def _locator(self, plan: InstallPlan) -> InstanceLocator:
        config = self.configuration.config_for(plan.configuration)
        return InstanceLocator(
            schema_version=1,
            instance_id=config.instance_id,
            app_root=APP_ROOT,
            data_root=DATA_ROOT,
            deployment_profile="v1.1-standard",
            listen=ListenIdentity(config.listen.host, config.listen.port),
            public_origin=config.public_origin,
            managed_config_path=MANAGED_CONFIG_PATH,
            config_revision=config.config_revision,
            release_identity=release_identity_from_manifest(self._manifest(plan)),
        )

    def _compose(self, plan: InstallPlan) -> ImmutableComposeDeployment:
        if self._deployment is None:
            config = self.configuration.config_for(plan.configuration)
            self._deployment = ImmutableComposeDeployment(
                HostPaths.initial_adoption(self._locator(plan)),
                runner=self.runner,
                managed_environment=dict(derive_runtime_environment(config)),
            )
        return self._deployment

    def manifest_for(self, plan: InstallPlan) -> dict[str, object]:
        return self._manifest(plan)

    def locator_for(self, plan: InstallPlan) -> InstanceLocator:
        return self._locator(plan)

    def deployment_for(self, plan: InstallPlan) -> ImmutableComposeDeployment:
        return self._compose(plan)

    @staticmethod
    def _mkdir(path: Path, mode: int) -> bool:
        if path.exists() or path.is_symlink():
            metadata = path.lstat()
            if path.is_symlink() or not stat.S_ISDIR(metadata.st_mode):
                _safe_adapter_error(
                    "INSTALL_ROOT_UNSAFE", mutation=False, recovery=False
                )
            return False
        path.mkdir(mode=mode)
        return True

    def prepare_roots(self, plan: InstallPlan) -> None:
        del plan
        try:
            for path, mode in (
                (Path(str(DATA_ROOT)), 0o755),
                (Path(str(DATA_ROOT / "config")), 0o700),
                (Path(str(DATA_ROOT / "postgres")), 0o700),
                (Path(str(DATA_ROOT / "redis")), 0o700),
                (Path(str(DATA_ROOT / "plugins")), 0o755),
                (Path(str(DATA_ROOT / "media")), 0o755),
                (Path(str(DATA_ROOT / "private")), 0o700),
                (Path(str(DATA_ROOT / "backups")), 0o770),
                (Path(str(DATA_ROOT / "logs")), 0o755),
            ):
                if self._mkdir(path, mode):
                    self._created.add(path)
        except InstallerAdapterError:
            raise
        except OSError:
            _safe_adapter_error(
                "INSTALL_ROOT_PREPARATION_FAILED", mutation=True, recovery=False
            )

    def publish_config(self, plan: InstallPlan) -> None:
        try:
            self.configuration.publish(plan.configuration)
            self._created.add(self.configuration.store.authority_path)
        except (OSError, ManagedConfigError):
            _safe_adapter_error(
                "INSTALL_CONFIG_PUBLICATION_FAILED", mutation=True, recovery=False
            )

    def stage_release(self, plan: InstallPlan) -> None:
        materials = self.releases.materials_for(plan.release)
        target = Path(str(APP_ROOT))
        if target.exists() or target.is_symlink():
            _safe_adapter_error("INSTALL_APP_ROOT_EXISTS", mutation=False, recovery=False)
        staging = target.parent / f".animemo-{plan.operation_id}"
        if staging.exists() or staging.is_symlink():
            _safe_adapter_error("INSTALL_STAGING_EXISTS", mutation=False, recovery=False)
        try:
            staging.mkdir(mode=0o755)
            for identity in materials.verified.files:
                source = materials.material(identity.path)
                destination = staging.joinpath(*Path(identity.path).parts)
                destination.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
                with source.open("rb") as reader, destination.open("xb") as writer:
                    shutil.copyfileobj(reader, writer, 1024 * 1024)
                    writer.flush()
                    os.fsync(writer.fileno())
                destination.chmod(identity.mode)
            os.replace(staging, target)
            self._created.add(target)
        except OSError:
            shutil.rmtree(staging, ignore_errors=True)
            _safe_adapter_error(
                "INSTALL_RELEASE_STAGING_FAILED", mutation=True, recovery=False
            )

    def prepare_services(self, plan: InstallPlan) -> None:
        try:
            self.runner.run(
                [str(Path(str(APP_ROOT)) / "deploy" / "install-updater.sh")],
                timeout=600,
            )
            if os.name != "posix":
                raise OSError("POSIX ownership is required")
            import grp
            import pwd

            uid = pwd.getpwnam("animemo-updater").pw_uid
            gid = grp.getgrnam("animemo-api").gr_gid
            os.chown(Path(str(DATA_ROOT / "config")), uid, gid)
            os.chown(self.configuration.store.authority_path, uid, gid)
            self.configuration.store.rebuild_runtime_env(
                expected_revision=plan.configuration.config_revision
            )
            deployment = self._compose(plan)
            manifest = self._manifest(plan)
            deployment.pull(manifest)
            for role, directory in (
                ("postgres", Path(str(DATA_ROOT / "postgres"))),
                ("redis", Path(str(DATA_ROOT / "redis"))),
            ):
                image = deployment.image_environment(manifest)[
                    f"ANIMEMO_{role.upper()}_IMAGE"
                ]
                uid_result = self.runner.run(
                    [
                        "/usr/bin/docker",
                        "run",
                        "--rm",
                        "--network",
                        "none",
                        "--entrypoint",
                        "id",
                        image,
                        "-u",
                    ],
                    timeout=60,
                )
                gid_result = self.runner.run(
                    [
                        "/usr/bin/docker",
                        "run",
                        "--rm",
                        "--network",
                        "none",
                        "--entrypoint",
                        "id",
                        image,
                        "-g",
                    ],
                    timeout=60,
                )
                os.chown(directory, int(uid_result.stdout), int(gid_result.stdout))
            for relative, mode in (
                ("plugins", 0o755),
                ("media", 0o755),
                ("logs", 0o755),
                ("private", 0o700),
                ("backups", 0o770),
            ):
                path = Path(str(DATA_ROOT / relative))
                os.chown(path, 10001, 10001)
                os.chmod(path, mode)
            deployment.start_datastores(manifest)
        except Exception:  # noqa: BLE001 - fixed host Adapter boundary
            _safe_adapter_error(
                "INSTALL_SERVICE_PREPARATION_FAILED", mutation=True, recovery=False
            )

    def migrate_database(self, plan: InstallPlan) -> None:
        try:
            self._compose(plan).migrate(self._manifest(plan))
        except Exception:  # noqa: BLE001 - fixed Compose Adapter boundary
            _safe_adapter_error(
                "INSTALL_DATABASE_MIGRATION_FAILED", mutation=True, recovery=True
            )

    def bootstrap(self, plan: InstallPlan) -> None:
        try:
            self._compose(plan).bootstrap(self._manifest(plan))
        except Exception:  # noqa: BLE001 - fixed Compose Adapter boundary
            _safe_adapter_error(
                "INSTALL_BOOTSTRAP_FAILED", mutation=True, recovery=True
            )

    def start_runtime(self, plan: InstallPlan) -> None:
        try:
            self._compose(plan).start_application(self._manifest(plan))
        except Exception:  # noqa: BLE001 - fixed Compose Adapter boundary
            _safe_adapter_error(
                "INSTALL_RUNTIME_START_FAILED", mutation=True, recovery=True
            )

    def validate_running_release(self, plan: InstallPlan) -> None:
        try:
            self._compose(plan).verify_health(self._manifest(plan))
        except Exception:  # noqa: BLE001 - fixed health Adapter boundary
            _safe_adapter_error(
                "INSTALL_RUNNING_RELEASE_INVALID", mutation=True, recovery=True
            )

    def adopt_updater(self, plan: InstallPlan) -> None:
        try:
            adopt_initial_release(
                InitialAdoptionRequest(
                    locator=self._locator(plan),
                    manifest=self._manifest(plan),
                )
            )
            self.runner.run(
                [
                    "/usr/bin/systemctl",
                    "enable",
                    "--now",
                    "animemo-updater.service",
                ],
                timeout=120,
            )
        except Exception:  # noqa: BLE001 - fixed Updater Adapter boundary
            _safe_adapter_error(
                "INSTALL_UPDATER_ADOPTION_FAILED", mutation=True, recovery=True
            )

    def doctor_acceptance(self, plan: InstallPlan) -> None:
        if self.doctor_acceptor is None:
            _safe_adapter_error(
                "INSTALL_DOCTOR_ADAPTER_UNAVAILABLE", mutation=True, recovery=True
            )
        try:
            self.doctor_acceptor(plan, self._compose(plan))
        except InstallerAdapterError:
            raise
        except Exception:  # noqa: BLE001 - complete Doctor Adapter boundary
            _safe_adapter_error(
                "INSTALL_DOCTOR_FAILED", mutation=True, recovery=True
            )

    def cleanup_owned_staging(self, plan: InstallPlan) -> None:
        del plan
        for path in sorted(self._created, key=lambda item: len(item.parts), reverse=True):
            try:
                if path.is_file() and not path.is_symlink():
                    path.unlink()
                elif path.is_dir() and not path.is_symlink():
                    path.rmdir()
            except FileNotFoundError:
                continue
            except OSError:
                _safe_adapter_error(
                    "INSTALL_SCOPED_CLEANUP_FAILED", mutation=True, recovery=True
                )


def build_runtime() -> Installer:
    releases = ProductionReleasePort()
    configuration = ProductionManagedConfigurationPort()
    compatibility = ProductionCompatibilityPort(releases)
    platform = ProductionPlatformPort(releases)
    doctor = ProductionDoctorAcceptance(
        releases=releases,
        compatibility=compatibility,
    )
    fresh = ProductionFreshInstallPort(
        releases=releases,
        configuration=configuration,
        doctor_acceptor=doctor,
    )
    return Installer(
        releases=releases,
        target=ProductionTargetPort(
            releases=releases,
            platform=platform,
            doctor=doctor,
        ),
        platform=platform,
        compatibility=compatibility,
        configuration=configuration,
        operations=ProductionOperationPort(),
        fresh=fresh,
        restore=ProductionRestoreRuntimePort(
            releases=releases,
            configuration=configuration,
            fresh=fresh,
        ),
    )


def production_configuration_doctor(
    snapshot,
    config: ManagedConfig,
    manifest: dict[str, object],
) -> None:
    """Reacquire exact Release authority and run complete config acceptance."""

    releases = ProductionReleasePort()
    release = releases.resolve(
        ReleaseSelector(version=str(manifest["release"]["version"])),
        refresh=True,
    )
    materials = releases.materials_for(release)
    if materials.manifest != manifest:
        raise InstallerAdapterError(
            "CONFIG_CURRENT_RELEASE_CHANGED",
            mutation_occurred=True,
            recovery_required=True,
        )
    compatibility = ProductionCompatibilityPort(releases)
    platform_adapter = ProductionPlatformPort(releases)
    platform = platform_adapter.assess(release.platform_profile)
    if not platform.compatible:
        raise InstallerAdapterError(
            "CONFIG_PLATFORM_NOT_QUALIFIED",
            mutation_occurred=True,
            recovery_required=True,
        )
    deployment = ImmutableComposeDeployment(
        HostPaths.production(snapshot),
        managed_environment=dict(derive_runtime_environment(config)),
    )
    ProductionDoctorAcceptance(
        releases=releases,
        compatibility=compatibility,
    ).accept_existing(
        expected_instance_id=config.instance_id,
        release=release,
        platform=platform,
        deployment=deployment,
    )


__all__ = [
    "ProductionCompatibilityPort",
    "ProductionDoctorAcceptance",
    "ProductionFreshInstallPort",
    "ProductionManagedConfigurationPort",
    "ProductionOperationPort",
    "ProductionPlatformPort",
    "ProductionReleasePort",
    "ProductionRestoreRuntimePort",
    "ProductionTargetPort",
    "build_runtime",
    "collect_host_capabilities",
    "production_configuration_doctor",
]
