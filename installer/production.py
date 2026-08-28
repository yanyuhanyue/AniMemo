"""Concrete canonical-host adapters for Installer Runtime v1.

The domain state machine remains in :mod:`installer.runtime`.  This module owns
only fixed-path OS, Release, Compose, Updater, and protected-file boundaries.
"""

from __future__ import annotations

import base64
import ipaddress
import json
import os
import platform as host_platform
import secrets
import shutil
import socket
import stat
import tempfile
import time
import uuid
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

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
    DEFAULT_INSTANCE_NAME,
    UPDATER_STATE_BASE,
    InstanceLocator,
    InstanceName,
    InstanceNamespace,
    ListenIdentity,
    LocatorError,
    instance_locator_digest,
    instance_namespace,
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
from durability.ownership import (
    LocalOwnershipReceiptStore,
    OwnershipReceipt,
    create_ownership_receipt,
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
from installer.bootstrap import (
    CandidateBootstrapPrivilegeGate,
    ProductionBootstrapPrivilegeGate,
    verified_prepublication_candidate_capability,
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
from updater.local_bundle import (
    LocalBundleReleaseSource,
    LocalBundleTransportPolicy,
)
from updater.oci import (
    AcquiredRuntimeImage,
    ImageAcquirer,
    ImageAcquisitionReceipt,
)
from updater.runtime import InitialAdoptionRequest, adopt_initial_release
from updater.runtime_state import RuntimeState
from updater.slots import ReleaseSlots
from updater.source import ReleaseResolver, VerifiedReleaseMaterials
from updater.state import OperationStore, UpdateLock
from updater.transport import ExplicitTransportPolicy

from .restore_production import ProductionRestoreRuntimePort
from .runtime import (
    ConfigPlanEvidence,
    Installer,
    InstallerAdapterError,
    InstallerError,
    InstallOutcome,
    InstallPhase,
    InstallPlan,
    InstallRequest,
    InstallTransportSource,
    ListenRequest,
    PlatformEvidence,
    ReleaseEvidence,
    ReleaseSelector,
    TargetClass,
    TargetEvidence,
    explicit_transport_policy,
)

if TYPE_CHECKING:
    from .platform_bootstrap import (
        PlatformBootstrapPlan,
        PlatformBootstrapReceipt,
        ProductionPlatformBootstrap,
    )

_PLATFORM_EVIDENCE_MATERIAL = "release/platform-qualification.json"
_PLATFORM_PROBE_ENVIRONMENT = {
    "HOME": "/nonexistent",
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
    "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
}
_LOCAL_DOCKER_HOST = "unix:///var/run/docker.sock"


class LocalDockerCommandRunner(CommandRunner):
    """Force every production Docker/Compose subprocess onto the local socket."""

    def __init__(self, delegate: CommandRunner | None = None) -> None:
        self._delegate = delegate or CommandRunner()

    @staticmethod
    def _closed_command(
        argv: list[str], env: dict[str, str] | None
    ) -> tuple[list[str], dict[str, str] | None]:
        closed_argv = list(argv)
        if closed_argv[:1] != ["/usr/bin/docker"]:
            return closed_argv, env
        if closed_argv[1:2] != ["--host"]:
            closed_argv[1:1] = ["--host", _LOCAL_DOCKER_HOST]
        closed_env = dict(os.environ if env is None else env)
        for name in tuple(closed_env):
            if name.startswith("DOCKER_"):
                closed_env.pop(name)
        closed_env.update(
            {
                "HOME": "/nonexistent",
                "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
            }
        )
        return closed_argv, closed_env

    def run(
        self,
        argv: list[str],
        *,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
        timeout: int = 300,
    ):
        closed_argv, closed_env = self._closed_command(argv, env)
        return self._delegate.run(
            closed_argv,
            cwd=cwd,
            env=closed_env,
            timeout=timeout,
        )

    def write_gzip(
        self,
        argv: list[str],
        path: Path,
        *,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
        timeout: int = 600,
        root: Path | None = None,
    ) -> dict[str, object]:
        closed_argv, closed_env = self._closed_command(argv, env)
        return self._delegate.write_gzip(
            closed_argv,
            path,
            cwd=cwd,
            env=closed_env,
            timeout=timeout,
            root=root,
        )


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


def _linklike(path: Path) -> bool:
    return path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction())


class ProductionReleasePort:
    """Resolve the highest requested candidate and retain its verified bytes."""

    def __init__(
        self,
        *,
        source: ReleaseResolver | None = None,
        cache_root: Path | None = None,
        transport_source: InstallTransportSource = InstallTransportSource.GITHUB,
        transport_policy: ExplicitTransportPolicy
        | LocalBundleTransportPolicy
        | None = None,
        image_acquirer: ImageAcquirer | None = None,
        local_bundle_payload: Path | None = None,
        local_bundle_release_attestation: Path | None = None,
        offline_verifier=None,
    ) -> None:
        if type(transport_source) is not InstallTransportSource:
            raise InstallerError(
                "INSTALL_TRANSPORT_SOURCE_INVALID",
                outcome=InstallOutcome.VALIDATION_FAILED,
            )
        if transport_source is InstallTransportSource.PREPUBLICATION_CANDIDATE:
            raise InstallerError(
                "INSTALL_TRANSPORT_SOURCE_INVALID",
                outcome=InstallOutcome.VALIDATION_FAILED,
            )
        expected_policy = explicit_transport_policy(transport_source)
        selected_policy = transport_policy or expected_policy
        if (
            type(selected_policy) is not type(expected_policy)
            or selected_policy.source != expected_policy.source
            or selected_policy.identity != expected_policy.identity
            or selected_policy.fallback_allowed is not False
        ):
            raise InstallerError(
                "INSTALL_TRANSPORT_POLICY_INVALID",
                outcome=InstallOutcome.VALIDATION_FAILED,
            )
        if (
            source is None
            and transport_source is InstallTransportSource.LOCAL_BUNDLE
            and (
                not isinstance(local_bundle_payload, Path)
                or not isinstance(local_bundle_release_attestation, Path)
            )
        ):
            raise InstallerError(
                "INSTALL_LOCAL_BUNDLE_INPUT_REQUIRED",
                outcome=InstallOutcome.VALIDATION_FAILED,
            )
        self._temporary: tempfile.TemporaryDirectory[str] | None = None
        if source is None:
            if cache_root is None:
                self._temporary = tempfile.TemporaryDirectory(
                    prefix="animemo-installer-release-"
                )
                cache_root = Path(self._temporary.name) / "cache"
            if transport_source is InstallTransportSource.LOCAL_BUNDLE:
                assert isinstance(local_bundle_payload, Path)
                assert isinstance(local_bundle_release_attestation, Path)
                if offline_verifier is None:
                    try:
                        from updater.offline import (
                            production_offline_release_verifier,
                        )

                        offline_verifier = production_offline_release_verifier()
                    except Exception:  # noqa: BLE001 - closed authority factory
                        raise InstallerError(
                            "INSTALL_OFFLINE_VERIFIER_UNAVAILABLE",
                            outcome=InstallOutcome.VALIDATION_FAILED,
                        ) from None
                try:
                    source = LocalBundleReleaseSource.from_media(
                        payload=local_bundle_payload,
                        release_attestation=local_bundle_release_attestation,
                        cache_root=cache_root,
                        verifier=offline_verifier,
                        updater_version=updater_version,
                    )
                except Exception:  # noqa: BLE001 - offline authority boundary
                    if self._temporary is not None:
                        self._temporary.cleanup()
                        self._temporary = None
                    raise InstallerError(
                        "INSTALL_LOCAL_BUNDLE_VERIFICATION_FAILED",
                        outcome=InstallOutcome.VALIDATION_FAILED,
                    ) from None
            else:
                if (
                    local_bundle_payload is not None
                    or local_bundle_release_attestation is not None
                    or offline_verifier is not None
                ):
                    raise InstallerError(
                        "INSTALL_LOCAL_BUNDLE_INPUT_FORBIDDEN",
                        outcome=InstallOutcome.VALIDATION_FAILED,
                    )
                source = ReleaseResolver(cache_root, policy=selected_policy)
        elif transport_source is InstallTransportSource.LOCAL_BUNDLE:
            if not isinstance(source, LocalBundleReleaseSource):
                raise InstallerError(
                    "INSTALL_LOCAL_BUNDLE_RESOLVER_INVALID",
                    outcome=InstallOutcome.VALIDATION_FAILED,
                )
        elif (
            local_bundle_payload is not None
            or local_bundle_release_attestation is not None
            or offline_verifier is not None
        ):
            raise InstallerError(
                "INSTALL_LOCAL_BUNDLE_INPUT_FORBIDDEN",
                outcome=InstallOutcome.VALIDATION_FAILED,
            )
        source_policy = getattr(source, "transport_policy", selected_policy)
        if (
            type(source_policy) is not type(selected_policy)
            or source_policy.source != selected_policy.source
            or source_policy.identity != selected_policy.identity
            or source_policy.fallback_allowed is not False
        ):
            raise InstallerError(
                "INSTALL_RELEASE_RESOLVER_POLICY_MISMATCH",
                outcome=InstallOutcome.VALIDATION_FAILED,
            )
        self.source = source
        self.transport_source = transport_source
        self.transport_policy = selected_policy
        self.image_acquirer = image_acquirer or ImageAcquirer(
            runner=LocalDockerCommandRunner()
        )
        self._materials: dict[str, VerifiedReleaseMaterials] = {}
        self._image_receipts: dict[str, ImageAcquisitionReceipt] = {}
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
            if actual != requested:
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
            deployment_identity_digest=str(manifest["deployment"]["contractSha256"]),
            deployment_profile=str(manifest["deployment"]["profile"]),
            platform_profile="v1.1-standard-linux-amd64",
            transport_source=self.transport_source,
            transport_policy_identity=self.transport_policy.identity,
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

    def acquire_images(self, evidence: ReleaseEvidence) -> ImageAcquisitionReceipt:
        materials = self.materials_for(evidence)
        try:
            if self.transport_source is InstallTransportSource.LOCAL_BUNDLE:
                receipt = self.source.acquire_images(
                    materials,
                    self.image_acquirer,
                )
            else:
                receipt = self.image_acquirer.acquire(materials, self.transport_policy)
        except InstallerError:
            raise
        except Exception:  # noqa: BLE001 - image acquisition Adapter is redacted
            raise InstallerError(
                "INSTALL_IMAGE_ACQUISITION_FAILED",
                outcome=InstallOutcome.ENVIRONMENT_FAILED,
            ) from None
        expected_roles = ("api", "postgres", "redis", "web")
        local_immutable = self.transport_source is InstallTransportSource.LOCAL_BUNDLE
        if (
            type(receipt) is not ImageAcquisitionReceipt
            or receipt.verified_release_identity != materials.identity_digest
            or receipt.transport_policy_identity != self.transport_policy.identity
            or type(receipt.images) is not tuple
            or tuple(item.role for item in receipt.images) != expected_roles
            or any(
                type(item) is not AcquiredRuntimeImage
                or item.canonical_reference != materials.image(item.role)
                or (
                    item.observed_reference != item.canonical_reference
                    and (
                        not local_immutable
                        or len(item.observed_reference) != 71
                        or not item.observed_reference.startswith("sha256:")
                        or any(
                            character not in "0123456789abcdef"
                            for character in item.observed_reference[7:]
                        )
                    )
                )
                for item in receipt.images
            )
            or not isinstance(receipt.identity, str)
            or len(receipt.identity) != 64
            or any(
                character not in "0123456789abcdef" for character in receipt.identity
            )
        ):
            raise InstallerError(
                "INSTALL_IMAGE_ACQUISITION_RECEIPT_INVALID",
                outcome=InstallOutcome.VALIDATION_FAILED,
            )
        self._image_receipts[evidence.manifest_digest] = receipt
        return receipt

    def distribution_policy_for(
        self, evidence: ReleaseEvidence
    ) -> tuple[str, str, str]:
        self.materials_for(evidence)
        selection_origin = getattr(
            self.transport_policy,
            "selection_origin",
            "explicit-admin-input",
        )
        selection_value = getattr(selection_origin, "value", selection_origin)
        if not isinstance(selection_value, str):
            raise InstallerError(
                "INSTALL_TRANSPORT_POLICY_INVALID",
                outcome=InstallOutcome.VALIDATION_FAILED,
            )
        return (
            self.transport_source.value,
            self.transport_policy.identity,
            selection_value,
        )

    def image_receipt_for(
        self, evidence: ReleaseEvidence
    ) -> ImageAcquisitionReceipt:
        receipt = self._image_receipts.get(evidence.manifest_digest)
        if receipt is None:
            raise InstallerError(
                "INSTALL_IMAGE_ACQUISITION_RECEIPT_UNAVAILABLE",
                outcome=InstallOutcome.ENVIRONMENT_FAILED,
            )
        return receipt

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


class CandidateReleasePort:
    """Serve one verifier-owned Candidate without discovery or fallback."""

    def __init__(
        self,
        loaded,
        *,
        transport_source: InstallTransportSource,
        image_acquirer: ImageAcquirer,
    ) -> None:
        if type(transport_source) is not InstallTransportSource:
            raise InstallerError(
                "INSTALL_CANDIDATE_PROFILE_INVALID",
                outcome=InstallOutcome.VALIDATION_FAILED,
            )
        self._loaded = loaded
        self._verified_digest = loaded.verified_digest
        self.transport_source = transport_source
        self.transport_policy = explicit_transport_policy(transport_source)
        self.image_acquirer = image_acquirer
        self._image_receipts: dict[str, ImageAcquisitionReceipt] = {}
        self._evidence = self._build_evidence()

    def _build_evidence(self) -> ReleaseEvidence:
        manifest = self._loaded.materials.manifest
        validate_manifest(manifest, updater_version=updater_version)
        return ReleaseEvidence(
            version=str(manifest["release"]["version"]),
            channel=str(manifest["release"]["channel"]),
            commit=str(manifest["release"]["commit"]),
            manifest_digest=_manifest_digest(manifest),
            material_identity_digest=self._loaded.materials.identity_digest,
            deployment_identity_digest=str(
                manifest["deployment"]["contractSha256"]
            ),
            deployment_profile=str(manifest["deployment"]["profile"]),
            platform_profile="v1.1-standard-linux-amd64",
            transport_source=self.transport_source,
            transport_policy_identity=self.transport_policy.identity,
        )

    def _reload(self) -> None:
        from release.candidate import load_verified_candidate

        loaded = load_verified_candidate(self._verified_digest)
        if (
            loaded.verified != self._loaded.verified
            or loaded.candidate_input != self._loaded.candidate_input
        ):
            raise InstallerError(
                "INSTALL_CANDIDATE_CHANGED",
                outcome=InstallOutcome.VALIDATION_FAILED,
            )
        self._loaded = loaded

    def resolve(self, selector: ReleaseSelector, *, refresh: bool) -> ReleaseEvidence:
        if refresh:
            self._reload()
        if (
            selector.version is not None
            and selector.version != self._evidence.version
        ) or (
            selector.channel is not None
            and selector.channel != self._evidence.channel
        ):
            raise InstallerError(
                "INSTALL_CANDIDATE_SELECTOR_MISMATCH",
                outcome=InstallOutcome.VALIDATION_FAILED,
            )
        return self._evidence

    def materials_for(self, evidence: ReleaseEvidence) -> VerifiedReleaseMaterials:
        if evidence.as_dict() != self._evidence.as_dict():
            raise InstallerError(
                "INSTALL_CANDIDATE_EVIDENCE_MISMATCH",
                outcome=InstallOutcome.VALIDATION_FAILED,
            )
        for identity in self._loaded.materials.verified.files:
            self._loaded.materials.material(identity.path)
        return self._loaded.materials

    def acquire_images(self, evidence: ReleaseEvidence) -> ImageAcquisitionReceipt:
        materials = self.materials_for(evidence)
        try:
            receipt = self.image_acquirer.acquire_local(
                materials,
                self._loaded.images,
                LocalBundleTransportPolicy(),
            )
        except Exception:  # noqa: BLE001 - local importer boundary is redacted
            raise InstallerError(
                "INSTALL_CANDIDATE_IMAGE_IMPORT_FAILED",
                outcome=InstallOutcome.ENVIRONMENT_FAILED,
            ) from None
        if (
            type(receipt) is not ImageAcquisitionReceipt
            or receipt.verified_release_identity != materials.identity_digest
            or receipt.transport_policy_identity
            != LocalBundleTransportPolicy().identity
            or tuple(item.role for item in receipt.images)
            != ("api", "postgres", "redis", "web")
        ):
            raise InstallerError(
                "INSTALL_CANDIDATE_IMAGE_RECEIPT_INVALID",
                outcome=InstallOutcome.VALIDATION_FAILED,
            )
        self._image_receipts[evidence.manifest_digest] = receipt
        return receipt

    def distribution_policy_for(
        self, evidence: ReleaseEvidence
    ) -> tuple[str, str, str]:
        self.materials_for(evidence)
        policy = LocalBundleTransportPolicy()
        return ("local-bundle", policy.identity, "explicit-admin-input")

    def image_receipt_for(
        self, evidence: ReleaseEvidence
    ) -> ImageAcquisitionReceipt:
        receipt = self._image_receipts.get(evidence.manifest_digest)
        if receipt is None:
            raise InstallerError(
                "INSTALL_CANDIDATE_IMAGE_RECEIPT_UNAVAILABLE",
                outcome=InstallOutcome.ENVIRONMENT_FAILED,
            )
        return receipt

    def latest_materials(self) -> VerifiedReleaseMaterials:
        return self._loaded.materials

    def latest_evidence(self) -> ReleaseEvidence:
        return self._evidence


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
                target={"profile": "v1.1-instance-scoped"},
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
        runner.run(argv, timeout=30, env=dict(_PLATFORM_PROBE_ENVIRONMENT))
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
    runner = runner or LocalDockerCommandRunner()
    machine = host_platform.machine().lower()
    architecture = "amd64" if machine in {"x86_64", "amd64"} else machine
    filesystem = _filesystem_capabilities()
    compose_version = _command_available(
        runner,
        [
            "/usr/bin/docker",
            "--host",
            "unix:///var/run/docker.sock",
            "compose",
            "version",
        ],
    )
    compose_help = _command_available(
        runner,
        [
            "/usr/bin/docker",
            "--host",
            "unix:///var/run/docker.sock",
            "compose",
            "up",
            "--help",
        ],
    )
    docker = _command_available(
        runner,
        ["/usr/bin/docker", "--host", "unix:///var/run/docker.sock", "info"],
    )
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
        namespace: InstanceNamespace | None = None,
    ) -> None:
        self.releases = releases
        self.platform = platform
        self.doctor = doctor
        self.runner = runner or LocalDockerCommandRunner()
        self.namespace = namespace or instance_namespace()

    def _installed_material_exact(self, materials: VerifiedReleaseMaterials) -> bool:
        try:
            for identity in materials.verified.files:
                expected = materials.material(identity.path)
                installed = Path(str(self.namespace.app_root)) / Path(identity.path)
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

    def _external_runtime_present(self) -> bool:
        service_links = (
            Path("/etc/systemd/system/multi-user.target.wants")
            / self.namespace.updater_service,
        )
        for path in service_links:
            try:
                path.lstat()
            except FileNotFoundError:
                continue
            return True
        queries = (
            (
                (
                    "/usr/bin/systemctl",
                    "show",
                    self.namespace.updater_service,
                    "--property=LoadState",
                    "--value",
                ),
                frozenset({"", "not-found"}),
            ),
            *(
                (
                    (
                        "/usr/bin/docker",
                        kind,
                        "ls",
                        "--all" if kind == "container" else "--quiet",
                        *(("--quiet",) if kind == "container" else ()),
                        "--filter",
                        f"label=com.docker.compose.project={self.namespace.compose_project}",
                    ),
                    frozenset({""}),
                )
                for kind in ("container", "network", "volume")
            ),
        )
        for argv, absent_values in queries:
            result = self.runner.run(list(argv))
            if str(result.stdout or "").strip() not in absent_values:
                return True
        return False

    def _casefold_collision(self) -> bool:
        for owned_root in self.namespace.owned_roots:
            root = Path(str(owned_root))
            parent = root.parent
            try:
                metadata = parent.lstat()
            except FileNotFoundError:
                continue
            if _linklike(parent) or not stat.S_ISDIR(metadata.st_mode):
                return True
            try:
                names = tuple(item.name for item in parent.iterdir())
            except OSError:
                return True
            if any(
                candidate != root.name and candidate.casefold() == root.name.casefold()
                for candidate in names
            ):
                return True
        return False

    def inspect(self) -> TargetEvidence:
        if self._casefold_collision():
            return TargetEvidence(
                TargetClass.FOREIGN,
                sha256_identity(b"canonical-instance-casefold-collision"),
            )
        locator_path = Path(str(self.namespace.locator_path))
        if locator_path.exists() or locator_path.is_symlink():
            try:
                snapshot = load_instance_snapshot(instance_name=self.namespace.name)
                config = LocalManagedConfigStore(
                    instance_name=self.namespace.name
                ).read()
                locator = snapshot.locator
                receipt = LocalOwnershipReceiptStore(
                    instance_name=self.namespace.name
                ).read()
                if (
                    receipt.receipt_digest != locator.ownership_receipt_digest
                    or receipt.instance_name != locator.instance_name
                    or receipt.instance_id != locator.instance_id
                    or receipt.compose_project != locator.compose_project
                    or config.instance_id != locator.instance_id
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
                                    derive_runtime_environment(
                                        config,
                                        namespace=instance_namespace(
                                            locator.instance_name
                                        ),
                                        locator_digest=snapshot.digest,
                                    )
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
        updater_state_value = getattr(self.namespace, "updater_state_root", None)
        updater_state_root = (
            Path(str(updater_state_value))
            if updater_state_value is not None
            else None
        )
        for root in tuple(Path(str(path)) for path in self.namespace.owned_roots):
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
            if _linklike(root) or not stat.S_ISDIR(metadata.st_mode):
                return TargetEvidence(
                    TargetClass.FOREIGN,
                    sha256_identity(b"canonical-root-foreign"),
                )
            try:
                entries = tuple(root.iterdir())
            except OSError:
                entries = ()
                unreadable = True
            else:
                unreadable = False
            empty = not entries and not unreadable
            owned_lock_only = False
            if (
                updater_state_root is not None
                and root == updater_state_root
                and len(entries) == 1
            ):
                lock_path = entries[0]
                try:
                    lock_metadata = lock_path.lstat()
                except OSError:
                    lock_metadata = None
                owned_lock_only = bool(
                    lock_path.name == "update.lock"
                    and lock_metadata is not None
                    and not lock_path.is_symlink()
                    and stat.S_ISREG(lock_metadata.st_mode)
                    and lock_metadata.st_nlink == 1
                    and (
                        os.name == "nt"
                        or stat.S_IMODE(lock_metadata.st_mode) == 0o600
                    )
                )
                empty = owned_lock_only
            states.append(
                "owned-lock" if owned_lock_only else ("empty" if empty else "data")
            )
        evidence_digest = sha256_identity(canonical_json_bytes(states))
        if any(item == "data" for item in states):
            return TargetEvidence(TargetClass.PARTIAL_AMBIGUOUS, evidence_digest)
        try:
            external_runtime = self._external_runtime_present()
        except OSError:
            return TargetEvidence(
                TargetClass.CORRUPT,
                sha256_identity(b"canonical-runtime-unreadable"),
            )
        if external_runtime:
            return TargetEvidence(
                TargetClass.PARTIAL_AMBIGUOUS,
                sha256_identity(
                    canonical_json_bytes({"roots": states, "externalRuntime": True})
                ),
            )
        if all(item == "absent" for item in states):
            return TargetEvidence(TargetClass.ABSENT, evidence_digest)
        if all(item in {"absent", "empty", "owned-lock"} for item in states):
            preparation_base_digests: tuple[str, ...] = ()
            if states.count("owned-lock") == 1:
                lock_index = states.index("owned-lock")
                bases: set[str] = set()
                for prior_state in ("absent", "empty"):
                    prior = list(states)
                    prior[lock_index] = prior_state
                    bases.add(sha256_identity(canonical_json_bytes(prior)))
                preparation_base_digests = tuple(sorted(bases))
            return TargetEvidence(
                TargetClass.VERIFIED_EMPTY,
                evidence_digest,
                preparation_base_digests=preparation_base_digests,
            )
        return TargetEvidence(TargetClass.PARTIAL_AMBIGUOUS, evidence_digest)


class ProductionManagedConfigurationPort:
    def __init__(
        self,
        store: LocalManagedConfigStore | None = None,
        *,
        namespace: InstanceNamespace | None = None,
    ) -> None:
        self.namespace = namespace or instance_namespace()
        self.store = store or LocalManagedConfigStore(instance_name=self.namespace.name)
        self._planned: dict[str, ManagedConfig] = {}
        self._planned_evidence: dict[str, ConfigPlanEvidence] = {}
        self._bound_proxy_evidence: dict[str, ConfigPlanEvidence] = {}
        self._pending_proxy_revisions: set[str] = set()
        self._existing: dict[str, bool] = {}

    def _assert_instance_id_unbound(self, instance_id: str) -> None:
        base = Path(str(UPDATER_STATE_BASE))
        try:
            metadata = base.lstat()
        except FileNotFoundError:
            return
        except OSError:
            raise ManagedConfigError("CONFIG_INSTANCE_REGISTRY_UNAVAILABLE") from None
        if _linklike(base) or not stat.S_ISDIR(metadata.st_mode):
            raise ManagedConfigError("CONFIG_INSTANCE_REGISTRY_UNSAFE")
        try:
            entries = tuple(base.iterdir())
        except OSError:
            raise ManagedConfigError("CONFIG_INSTANCE_REGISTRY_UNAVAILABLE") from None
        for entry in entries:
            try:
                entry_metadata = entry.lstat()
                name = InstanceName(entry.name)
            except (OSError, LocatorError):
                raise ManagedConfigError("CONFIG_INSTANCE_REGISTRY_UNSAFE") from None
            if _linklike(entry) or not stat.S_ISDIR(entry_metadata.st_mode):
                raise ManagedConfigError("CONFIG_INSTANCE_REGISTRY_UNSAFE")
            if name == self.namespace.name:
                continue
            locator_path = entry / "instance.json"
            if not locator_path.exists() and not locator_path.is_symlink():
                continue
            try:
                snapshot = load_instance_snapshot(instance_name=name)
            except (LocatorError, OSError):
                raise ManagedConfigError("CONFIG_INSTANCE_REGISTRY_UNSAFE") from None
            if snapshot.locator.instance_id == instance_id:
                raise ManagedConfigError("CONFIG_INSTANCE_ID_COLLISION")

    @staticmethod
    def _assert_listen_available(listen: ListenConfig) -> None:
        address = ipaddress.ip_address(listen.host)
        family = socket.AF_INET6 if address.version == 6 else socket.AF_INET
        try:
            with socket.socket(family, socket.SOCK_STREAM) as probe:
                probe.bind((listen.host, listen.port))
        except OSError:
            raise InstallerError(
                "INSTALL_PORT_CONFLICT",
                outcome=InstallOutcome.VALIDATION_FAILED,
            ) from None

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
        if (
            direct != listen.direct_exposure_accepted
            or insecure != insecure_http_accepted
        ):
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
                trusted_proxy_ips=("127.0.0.1/32",),
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
        self._assert_instance_id_unbound(instance_id)
        exists = (
            self.store.authority_path.exists() or self.store.authority_path.is_symlink()
        )
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
            self._assert_listen_available(config.listen)
        safe_identity = sha256_identity(canonical_json_bytes(config.secret_safe_dict()))
        warnings = tuple(
            warning
            for condition, warning in (
                (not config.listen.is_loopback, "DIRECT_LISTEN_EXPOSURE"),
                (config.public_origin.startswith("http://"), "INSECURE_HTTP_ORIGIN"),
                (not exists, "EXACT_WEB_PROXY_BINDING_PENDING"),
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
        self._planned_evidence[evidence.config_revision] = evidence
        if not exists:
            self._pending_proxy_revisions.add(evidence.config_revision)
        self._existing[evidence.config_revision] = exists
        return evidence

    def revalidate(self, plan: ConfigPlanEvidence) -> None:
        original = self._planned_evidence.get(plan.config_revision)
        bound = self._bound_proxy_evidence.get(plan.config_revision)
        if plan != original and plan != bound:
            raise ManagedConfigError("CONFIG_PLAN_STALE")
        effective = bound or plan
        config = self._planned.get(effective.config_revision)
        if (
            config is None
            or sha256_identity(canonical_json_bytes(config.secret_safe_dict()))
            != effective.non_secret_identity_digest
        ):
            raise ManagedConfigError("CONFIG_PLAN_STALE")
        exists = (
            self.store.authority_path.exists() or self.store.authority_path.is_symlink()
        )
        if exists != self._existing[effective.config_revision]:
            raise ManagedConfigError("CONFIG_PLAN_STALE")
        if exists and self.store.read() != config:
            raise ManagedConfigError("CONFIG_PLAN_STALE")
        if not exists:
            self._assert_listen_available(config.listen)

    def config_for(self, evidence: ConfigPlanEvidence) -> ManagedConfig:
        self.revalidate(evidence)
        return self._planned[evidence.config_revision]

    def bind_exact_web_proxy(
        self,
        evidence: ConfigPlanEvidence,
        trusted_proxy: str,
    ) -> ConfigPlanEvidence:
        """Finalize the execution-bound exact Compose Web proxy before Django runs."""

        original = self._planned_evidence.get(evidence.config_revision)
        if (
            evidence != original
            or evidence.config_revision not in self._pending_proxy_revisions
            or evidence.config_revision in self._bound_proxy_evidence
        ):
            raise ManagedConfigError("CONFIG_PROXY_BINDING_INVALID")
        try:
            network = ipaddress.ip_network(trusted_proxy, strict=True)
        except ValueError:
            raise ManagedConfigError("CONFIG_PROXY_BINDING_INVALID") from None
        address = network.network_address
        if (
            network.version != 4
            or network.prefixlen != 32
            or address.is_unspecified
            or address.is_loopback
            or address.is_multicast
            or address.is_link_local
            or address.is_reserved
        ):
            raise ManagedConfigError("CONFIG_PROXY_BINDING_INVALID")
        current = self.config_for(evidence)
        if current.application.trusted_proxy_ips != ("127.0.0.1/32",):
            raise ManagedConfigError("CONFIG_PROXY_BINDING_INVALID")
        updated = replace(
            current,
            application=replace(
                current.application,
                trusted_proxy_ips=(str(network),),
            ),
        )
        self.store.write(
            updated,
            expected_revision=current.config_revision,
        )
        rebound = ConfigPlanEvidence(
            instance_id=updated.instance_id,
            config_revision=updated.config_revision,
            public_origin=updated.public_origin,
            listen_host=updated.listen.host,
            listen_port=updated.listen.port,
            exposure=evidence.exposure,
            non_secret_identity_digest=sha256_identity(
                canonical_json_bytes(updated.secret_safe_dict())
            ),
            warnings=tuple(
                warning
                for warning in evidence.warnings
                if warning != "EXACT_WEB_PROXY_BINDING_PENDING"
            ),
        )
        self._planned[updated.config_revision] = updated
        self._bound_proxy_evidence[updated.config_revision] = rebound
        self._pending_proxy_revisions.remove(updated.config_revision)
        return rebound

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
        self._existing[evidence.config_revision] = True


_PHASES = {
    phase.value: FreshInstallPhase(phase.value)
    for phase in InstallPhase
    if phase is not InstallPhase.SUCCEEDED
}


class ProductionOperationPort:
    def __init__(
        self,
        *,
        state_root: Path | None = None,
        namespace: InstanceNamespace | None = None,
    ) -> None:
        self.namespace = namespace or instance_namespace()
        selected_state_root = state_root or Path(str(self.namespace.updater_state_root))
        self.journal = FreshInstallOperationJournal(selected_state_root)
        self.lock_path = selected_state_root / "update.lock"
        self.current: FreshInstallOperation | None = None

    def acquire_lock(self, operation_id: str) -> AbstractContextManager[None]:
        del operation_id
        return UpdateLock(self.lock_path)

    def begin(self, plan: InstallPlan) -> None:
        self.journal.require_recovery_clear()
        operation = create_fresh_install_operation(
            operation_id=plan.operation_id,
            instance_name=plan.instance_name,
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
                rollback_succeeded=(False if recovery_required else rollback_succeeded),
            )
        )

    def succeed(self, *, completed_steps: tuple[str, ...]) -> None:
        del completed_steps
        if self.current is None:
            raise StateError("Fresh operation is not initialized")
        self._persist(succeed_fresh_install(self.current, at=_utc_now()))


@dataclass(frozen=True)
class _InstallerDistributionReader:
    expected_instance_id: str
    snapshot: dict[str, object]

    def read_local_snapshot(
        self, locator: InstanceLocator
    ) -> dict[str, object]:
        if locator.instance_id != self.expected_instance_id:
            raise StateError(
                "Installer distribution evidence belongs to another Instance"
            )
        return json.loads(json.dumps(self.snapshot))


class ProductionDoctorAcceptance:
    """Complete, read-only Doctor probe set for the canonical production host."""

    def __init__(
        self,
        *,
        releases: ProductionReleasePort,
        compatibility: ProductionCompatibilityPort,
        runner: CommandRunner | None = None,
        namespace: InstanceNamespace | None = None,
    ) -> None:
        self.releases = releases
        self.compatibility = compatibility
        self.runner = runner or LocalDockerCommandRunner()
        self.namespace = namespace or instance_namespace()

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
        source, policy_identity, selection_origin = (
            self.releases.distribution_policy_for(release)
        )
        receipt = self.releases.image_receipt_for(release)
        images = {
            role: str(manifest["images"][role]["digest"])
            for role in ("api", "postgres", "redis", "web")
        }
        expected_references = {role: materials.image(role) for role in images}
        if (
            receipt.verified_release_identity != release.material_identity_digest
            or receipt.transport_policy_identity != policy_identity
            or {image.role for image in receipt.images} != set(images)
            or any(
                image.canonical_reference != expected_references[image.role]
                for image in receipt.images
            )
        ):
            raise InstallerAdapterError(
                "INSTALL_DOCTOR_DISTRIBUTION_EVIDENCE_INVALID",
                mutation_occurred=True,
                recovery_required=True,
            )
        plan_body = {
            "ociImages": images,
            "policyIdentity": policy_identity,
            "releaseManifestDigest": release.manifest_digest,
            "source": source,
        }
        plan_identity = sha256_identity(canonical_json_bytes(plan_body)).removeprefix(
            "sha256:"
        )
        distribution_reader = _InstallerDistributionReader(
            expected_instance_id=expected_instance_id,
            snapshot={
                "schemaVersion": 1,
                "configuredTransportPolicy": {
                    "fallbackAllowed": False,
                    "identity": policy_identity,
                    "selectionOrigin": selection_origin,
                    "source": source,
                },
                "recentTransportReceipt": {
                    "identity": receipt.identity,
                    "ociImages": images,
                    "planIdentity": plan_identity,
                    "policyIdentity": policy_identity,
                    "releaseManifestDigest": release.manifest_digest,
                    "source": source,
                    "valid": True,
                },
                "verifiedReleaseIdentity": dict(
                    release_identity_from_manifest(manifest)
                ),
                "verifiedOCIIdentity": images,
                "plan": {"identity": plan_identity, **plan_body},
            },
        )
        config_store = LocalManagedConfigStore(instance_name=self.namespace.name)

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
            snapshot = load_instance_snapshot(instance_name=self.namespace.name)
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
                "deploy/updater/animemo-updater@.service"
            ).read_bytes()
            installed = Path(
                "/etc/systemd/system/animemo-updater@.service"
            ).read_bytes()
            result = self.runner.run(
                [
                    "/usr/bin/systemctl",
                    "is-active",
                    self.namespace.updater_service,
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
            with socket.create_connection((probe_host, current.listen.port), timeout=5):
                return True

        def updater_socket() -> bool:
            path = Path(str(self.namespace.updater_socket_path))
            metadata = path.lstat()
            return (
                stat.S_ISSOCK(metadata.st_mode)
                and stat.S_IMODE(metadata.st_mode) == 0o660
            )

        def updater_state() -> bool:
            state_root = Path(str(self.namespace.updater_state_root))
            slots = ReleaseSlots(state_root / "releases").read()
            RuntimeState(state_root).read()
            OperationStore(state_root).require_recovery_clear()
            return slots["current"] == manifest

        def release_consistency() -> bool:
            snapshot = load_instance_snapshot(instance_name=self.namespace.name)
            return (
                dict(snapshot.locator.release_identity)
                == dict(release_identity_from_manifest(manifest))
                and ReleaseSlots(Path(str(self.namespace.release_slots_root))).read()[
                    "current"
                ]
                == manifest
            )

        def plugins() -> bool:
            enabled = deployment.inspect_enabled_plugin_apis(manifest)
            supported = set(manifest["compatibility"]["pluginSdk"]["supportedApis"])
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
                lambda: shutil.disk_usage(Path(str(self.namespace.data_root))).free > 0,
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
                    (
                        deployment.verify_deployment_contract(manifest),
                        deployment.validate_compose(manifest),
                    )
                    is not None
                ),
            ),
            "network.listen": self._guarded(
                "LISTEN_REACHABLE", "LISTEN_UNREACHABLE", listen
            ),
            "identity.public-origin": self._guarded(
                "PUBLIC_ORIGIN_VALID",
                "PUBLIC_ORIGIN_INVALID",
                lambda: (
                    config().public_origin
                    == canonical_public_origin(config().public_origin)
                ),
            ),
            "database.postgresql.connectivity": self._guarded(
                "POSTGRESQL_REACHABLE",
                "POSTGRESQL_UNREACHABLE",
                lambda: deployment.probe_postgres(manifest) is None,
            ),
            "database.schema-compatibility": self._guarded(
                "DATABASE_SCHEMA_COMPATIBLE",
                "DATABASE_SCHEMA_INCOMPATIBLE",
                lambda: (
                    deployment.inspect_runtime_contracts(manifest)["databaseContract"]
                    == manifest["compatibility"]["database"]["contract"]
                ),
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
                lambda: safe_directory(
                    Path(str(self.namespace.data_root / "media")), 0o755
                ),
            ),
            "backup.readiness": self._guarded(
                "BACKUP_READY",
                "BACKUP_NOT_READY",
                lambda: safe_directory(
                    Path(str(self.namespace.data_root / "backups")), 0o770
                ),
            ),
        }
        artifact, dimensions = self.compatibility.collect(release, platform)
        report = DoctorRunner(
            probes=probes,
            compatibility=CompatibilityEvidence(artifact, dimensions),
            distribution_reader=distribution_reader,
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
        namespace: InstanceNamespace | None = None,
    ) -> None:
        self.releases = releases
        self.configuration = configuration
        self.runner = runner or LocalDockerCommandRunner()
        self.doctor_acceptor = doctor_acceptor
        self.namespace = namespace or instance_namespace()
        self._deployment: ImmutableComposeDeployment | None = None
        self._created: set[Path] = set()
        self._ownership: dict[str, OwnershipReceipt] = {}

    def _manifest(self, plan: InstallPlan) -> dict[str, object]:
        return self.releases.materials_for(plan.release).manifest

    def _locator(self, plan: InstallPlan) -> InstanceLocator:
        config = self.configuration.config_for(plan.configuration)
        receipt = self._ownership_receipt(plan)
        return InstanceLocator(
            schema_version=2,
            instance_name=self.namespace.name,
            instance_id=config.instance_id,
            app_root=self.namespace.app_root,
            data_root=self.namespace.data_root,
            updater_state_root=self.namespace.updater_state_root,
            updater_runtime_root=self.namespace.updater_runtime_root,
            deployment_profile="v1.1-instance-scoped",
            compose_project=self.namespace.compose_project,
            updater_service=self.namespace.updater_service,
            updater_socket_path=self.namespace.updater_socket_path,
            listen=ListenIdentity(config.listen.host, config.listen.port),
            public_origin=config.public_origin,
            managed_config_path=self.namespace.managed_config_path,
            config_revision=config.config_revision,
            release_identity=release_identity_from_manifest(self._manifest(plan)),
            ownership_receipt_digest=receipt.receipt_digest,
        )

    def _ownership_receipt(self, plan: InstallPlan) -> OwnershipReceipt:
        receipt = self._ownership.get(plan.operation_id)
        if receipt is None:
            config = self.configuration.config_for(plan.configuration)
            receipt = create_ownership_receipt(
                instance_name=self.namespace.name,
                instance_id=config.instance_id,
                listen_host=config.listen.host,
                listen_port=config.listen.port,
                release_identity=release_identity_from_manifest(self._manifest(plan)),
                created_at=_utc_now(),
            )
            self._ownership[plan.operation_id] = receipt
        return receipt

    def _compose(self, plan: InstallPlan) -> ImmutableComposeDeployment:
        if self._deployment is None:
            config = self.configuration.config_for(plan.configuration)
            self._deployment = ImmutableComposeDeployment(
                HostPaths.initial_adoption(self._locator(plan)),
                runner=self.runner,
                managed_environment=dict(
                    derive_runtime_environment(
                        config,
                        namespace=self.namespace,
                        locator_digest=instance_locator_digest(self._locator(plan)),
                    )
                ),
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
            if _linklike(path) or not stat.S_ISDIR(metadata.st_mode):
                _safe_adapter_error(
                    "INSTALL_ROOT_UNSAFE", mutation=False, recovery=False
                )
            return False
        path.mkdir(mode=mode)
        return True

    @staticmethod
    def _mkdir_shared_base(path: Path, mode: int) -> bool:
        if path.exists() or path.is_symlink():
            metadata = path.lstat()
            if _linklike(path) or not stat.S_ISDIR(metadata.st_mode):
                _safe_adapter_error(
                    "INSTALL_NAMESPACE_BASE_UNSAFE",
                    mutation=False,
                    recovery=False,
                )
            return False
        parent = path.parent
        metadata = parent.lstat()
        if _linklike(parent) or not stat.S_ISDIR(metadata.st_mode):
            _safe_adapter_error(
                "INSTALL_NAMESPACE_BASE_UNSAFE",
                mutation=False,
                recovery=False,
            )
        path.mkdir(mode=mode)
        return True

    def prepare_roots(self, plan: InstallPlan) -> None:
        del plan
        try:
            data_base = Path(str(self.namespace.data_root)).parent
            data_anchor = data_base.parent
            if self._mkdir_shared_base(data_anchor, 0o755):
                self._created.add(data_anchor)
            for base, mode in (
                (Path(str(self.namespace.app_root)).parent, 0o755),
                (data_base, 0o755),
                (Path(str(self.namespace.updater_state_root)).parent, 0o700),
                (Path(str(self.namespace.updater_runtime_root)).parent, 0o750),
            ):
                if self._mkdir_shared_base(base, mode):
                    self._created.add(base)
            for path, mode in (
                (Path(str(self.namespace.data_root)), 0o755),
                (Path(str(self.namespace.data_root / "config")), 0o700),
                (Path(str(self.namespace.data_root / "postgres")), 0o700),
                (Path(str(self.namespace.data_root / "redis")), 0o700),
                (Path(str(self.namespace.data_root / "plugins")), 0o755),
                (Path(str(self.namespace.data_root / "media")), 0o755),
                (Path(str(self.namespace.data_root / "private")), 0o700),
                (Path(str(self.namespace.data_root / "backups")), 0o770),
                (Path(str(self.namespace.data_root / "logs")), 0o755),
                (Path(str(self.namespace.updater_state_root)), 0o700),
                (Path(str(self.namespace.updater_runtime_root)), 0o750),
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
        target = Path(str(self.namespace.app_root))
        if target.exists() or target.is_symlink():
            _safe_adapter_error(
                "INSTALL_APP_ROOT_EXISTS", mutation=False, recovery=False
            )
        staging = target.parent / f".animemo-{plan.operation_id}"
        if staging.exists() or staging.is_symlink():
            _safe_adapter_error(
                "INSTALL_STAGING_EXISTS", mutation=False, recovery=False
            )
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
                [
                    str(self.namespace.app_root / "deploy" / "install-updater.sh"),
                    "--instance",
                    str(self.namespace.name),
                ],
                timeout=600,
            )
            if os.name != "posix":
                raise OSError("POSIX ownership is required")
            import grp
            import pwd

            uid = pwd.getpwnam("animemo-updater").pw_uid
            gid = grp.getgrnam("animemo-api").gr_gid
            os.chown(Path(str(self.namespace.data_root / "config")), uid, gid)
            os.chown(self.configuration.store.authority_path, uid, gid)
            self.configuration.store.rebuild_runtime_env(
                locator_digest=instance_locator_digest(self._locator(plan)),
                expected_revision=plan.configuration.config_revision,
            )
            deployment = self._compose(plan)
            manifest = self._manifest(plan)
            self.releases.acquire_images(plan.release)
            for role, directory in (
                ("postgres", Path(str(self.namespace.data_root / "postgres"))),
                ("redis", Path(str(self.namespace.data_root / "redis"))),
            ):
                image = deployment.image_environment(manifest)[
                    f"ANIMEMO_{role.upper()}_IMAGE"
                ]
                uid_result = self.runner.run(
                    [
                        "/usr/bin/docker",
                        "--host",
                        _LOCAL_DOCKER_HOST,
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
                        "--host",
                        _LOCAL_DOCKER_HOST,
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
                path = Path(str(self.namespace.data_root / relative))
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
            deployment = self._compose(plan)
            manifest = self._manifest(plan)
            deployment.start_application(manifest)
            self.configuration.bind_exact_web_proxy(
                plan.configuration,
                deployment.exact_web_proxy(manifest),
            )
            config = self.configuration.config_for(plan.configuration)
            locator = self._locator(plan)
            locator_digest = instance_locator_digest(locator)
            self.configuration.store.rebuild_runtime_env(
                locator_digest=locator_digest,
                expected_revision=config.config_revision,
            )
            deployment.refresh_binding(
                HostPaths.initial_adoption(locator),
                managed_environment=dict(
                    derive_runtime_environment(
                        config,
                        namespace=self.namespace,
                        locator_digest=locator_digest,
                    )
                ),
            )
            deployment.reconcile_api(manifest)
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

    def _wait_for_updater_socket(self) -> None:
        path = Path(str(self.namespace.updater_socket_path))
        for attempt in range(120):
            try:
                metadata = path.lstat()
            except OSError:
                metadata = None
            if (
                metadata is not None
                and stat.S_ISSOCK(metadata.st_mode)
                and stat.S_IMODE(metadata.st_mode) == 0o660
            ):
                return
            if attempt < 119:
                time.sleep(0.25)
        raise OSError("Updater socket did not become ready")

    def adopt_updater(self, plan: InstallPlan) -> None:
        try:
            receipt = self._ownership_receipt(plan)
            receipt_store = LocalOwnershipReceiptStore(
                instance_name=self.namespace.name
            )
            receipt_store.publish(receipt)
            if os.name != "posix":
                raise OSError("POSIX ownership is required")
            import grp
            import pwd

            os.chown(
                receipt_store.path,
                pwd.getpwnam("animemo-updater").pw_uid,
                grp.getgrnam("animemo-api").gr_gid,
            )

            def reverify_installer_release(version: str) -> dict[str, object]:
                if version != plan.release.version:
                    raise InstallerError(
                        "INSTALL_ADOPTION_RELEASE_MISMATCH",
                        outcome=InstallOutcome.VALIDATION_FAILED,
                    )
                refreshed = self.releases.resolve(
                    ReleaseSelector(version=version),
                    refresh=True,
                )
                if refreshed.as_dict() != plan.release.as_dict():
                    raise InstallerError(
                        "INSTALL_ADOPTION_RELEASE_CHANGED",
                        outcome=InstallOutcome.VALIDATION_FAILED,
                    )
                materials = self.releases.materials_for(refreshed)
                return json.loads(json.dumps(materials.manifest))

            adopt_initial_release(
                InitialAdoptionRequest(
                    locator=self._locator(plan),
                    manifest=self._manifest(plan),
                ),
                verifier=reverify_installer_release,
            )
            self.runner.run(
                [
                    "/usr/bin/systemctl",
                    "enable",
                    "--now",
                    self.namespace.updater_service,
                ],
                timeout=120,
            )
            self._wait_for_updater_socket()
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
            _safe_adapter_error("INSTALL_DOCTOR_FAILED", mutation=True, recovery=True)

    def cleanup_owned_staging(self, plan: InstallPlan) -> None:
        del plan
        application_root = Path(str(self.namespace.app_root))
        for path in sorted(
            self._created, key=lambda item: len(item.parts), reverse=True
        ):
            try:
                if path.is_file() and not path.is_symlink():
                    path.unlink()
                elif (
                    path == application_root
                    and path.is_dir()
                    and not path.is_symlink()
                ):
                    shutil.rmtree(path)
                elif path.is_dir() and not path.is_symlink():
                    path.rmdir()
            except FileNotFoundError:
                continue
            except OSError:
                _safe_adapter_error(
                    "INSTALL_SCOPED_CLEANUP_FAILED", mutation=True, recovery=True
                )


@dataclass(frozen=True)
class VerifiedPlatformBootstrapSession:
    """Exact verified Release and its host-platform plan/executor binding."""

    release: ReleaseEvidence
    plan: PlatformBootstrapPlan
    bootstrap: ProductionPlatformBootstrap


class PlatformBootstrapPrivilegeGate(Protocol):
    def verify_runtime_source(
        self, *, version: str, release_commit: str
    ) -> object: ...

    def consume(self, *, version: str, release_commit: str) -> object: ...


@dataclass(frozen=True)
class ProductionInstallerComposition:
    """Formal Stage0 composition before the canonical Installer interface."""

    runtime: Installer
    releases: ProductionReleasePort | CandidateReleasePort
    platform: ProductionPlatformPort
    bootstrap_privilege_gate: PlatformBootstrapPrivilegeGate | None = None

    def plan_platform(
        self,
        request: InstallRequest,
        verified_at: str,
    ) -> VerifiedPlatformBootstrapSession:
        if (
            request.transport_source is not self.releases.transport_source
            or request.transport_policy_identity
            != self.releases.transport_policy.identity
        ):
            raise InstallerError(
                "INSTALL_RELEASE_TRANSPORT_POLICY_MISMATCH",
                outcome=InstallOutcome.VALIDATION_FAILED,
            )
        release = self.releases.resolve(request.selector, refresh=False)
        if (
            type(self.releases) is not CandidateReleasePort
            and request.transport_source is not InstallTransportSource.LOCAL_BUNDLE
        ):
            from .bootstrap import authorize_online_stage0

            authorize_online_stage0(
                tag=release.version,
                release_commit=release.commit,
                verified_at=verified_at,
            )
        # Online Stage0 has just committed this capability. Offline execution
        # must consume an already provisioned, release-bound protected archive;
        # neither path may import the platform module before its fixed bytes are
        # checked against that archive.
        if self.bootstrap_privilege_gate is None:
            ProductionBootstrapPrivilegeGate().verify_runtime_source(
                version=release.version,
                release_commit=release.commit,
            )
        else:
            self.bootstrap_privilege_gate.verify_runtime_source(
                version=release.version,
                release_commit=release.commit,
            )
        from .platform_bootstrap import ProductionPlatformBootstrap

        bootstrap = ProductionPlatformBootstrap()
        plan = bootstrap.plan(transport_source=request.transport_source)
        return VerifiedPlatformBootstrapSession(
            release=release,
            plan=plan,
            bootstrap=bootstrap,
        )

    def execute_platform(
        self,
        session: VerifiedPlatformBootstrapSession,
        accepted_plan_digest: str,
    ) -> PlatformBootstrapReceipt:
        from .platform_bootstrap import (
            PlatformBootstrapError,
            ProductionPlatformBootstrap,
        )

        if (
            type(session) is not VerifiedPlatformBootstrapSession
            or type(session.bootstrap) is not ProductionPlatformBootstrap
        ):
            raise PlatformBootstrapError("PLATFORM_BOOTSTRAP_PLAN_CHANGED")
        refreshed = self.releases.resolve(
            ReleaseSelector(version=session.release.version),
            refresh=True,
        )
        if refreshed.as_dict() != session.release.as_dict():
            raise PlatformBootstrapError("PLATFORM_BOOTSTRAP_PLAN_CHANGED")
        receipt = session.bootstrap.execute(
            session.plan,
            accepted_plan_digest=accepted_plan_digest,
        )
        try:
            assessment = self.platform.assess(session.release.platform_profile)
        except InstallerError:
            raise PlatformBootstrapError(
                "PLATFORM_BOOTSTRAP_POST_QUALIFICATION_FAILED"
            ) from None
        if not assessment.compatible:
            raise PlatformBootstrapError("PLATFORM_BOOTSTRAP_POST_QUALIFICATION_FAILED")
        return receipt


def build_production_composition(
    *,
    instance_name: InstanceName | str = DEFAULT_INSTANCE_NAME,
    transport_source: InstallTransportSource = InstallTransportSource.GITHUB,
    transport_policy: ExplicitTransportPolicy
    | LocalBundleTransportPolicy
    | None = None,
    local_bundle_payload: Path | None = None,
    local_bundle_release_attestation: Path | None = None,
) -> ProductionInstallerComposition:
    namespace = instance_namespace(instance_name)
    runner = LocalDockerCommandRunner()
    releases = ProductionReleasePort(
        transport_source=transport_source,
        transport_policy=transport_policy,
        local_bundle_payload=local_bundle_payload,
        local_bundle_release_attestation=local_bundle_release_attestation,
        image_acquirer=ImageAcquirer(runner=runner),
    )
    configuration = ProductionManagedConfigurationPort(namespace=namespace)
    compatibility = ProductionCompatibilityPort(releases)
    platform = ProductionPlatformPort(releases)
    doctor = ProductionDoctorAcceptance(
        releases=releases,
        compatibility=compatibility,
        runner=runner,
        namespace=namespace,
    )
    fresh = ProductionFreshInstallPort(
        releases=releases,
        configuration=configuration,
        doctor_acceptor=doctor,
        runner=runner,
        namespace=namespace,
    )
    bootstrap_privilege_gate = ProductionBootstrapPrivilegeGate()
    runtime = Installer(
        releases=releases,
        target=ProductionTargetPort(
            releases=releases,
            platform=platform,
            doctor=doctor,
            runner=runner,
            namespace=namespace,
        ),
        platform=platform,
        compatibility=compatibility,
        configuration=configuration,
        operations=ProductionOperationPort(namespace=namespace),
        fresh=fresh,
        restore=ProductionRestoreRuntimePort(
            releases=releases,
            configuration=configuration,
            fresh=fresh,
        ),
        bootstrap_privilege_gate=bootstrap_privilege_gate,
        namespace=namespace,
    )
    return ProductionInstallerComposition(
        runtime=runtime,
        releases=releases,
        platform=platform,
        bootstrap_privilege_gate=bootstrap_privilege_gate,
    )


def build_candidate_composition(
    verified_candidate_digest: str,
    *,
    profile: str,
    instance_name: InstanceName | str = DEFAULT_INSTANCE_NAME,
) -> ProductionInstallerComposition:
    """Compose the Installer around one local Candidate capability only."""

    if profile not in {
        "ONLINE_FRESH",
        "ONLINE_EXISTING_DOCKER",
        "OFFLINE_VALIDATE_ONLY",
    }:
        raise InstallerError(
            "INSTALL_CANDIDATE_PROFILE_INVALID",
            outcome=InstallOutcome.VALIDATION_FAILED,
        )
    from release.candidate import load_verified_candidate

    try:
        loaded = load_verified_candidate(verified_candidate_digest)
    except Exception:  # noqa: BLE001 - verifier boundary stays redacted
        raise InstallerError(
            "INSTALL_VERIFIED_CANDIDATE_REQUIRED",
            outcome=InstallOutcome.VALIDATION_FAILED,
        ) from None
    namespace = instance_namespace(instance_name)
    runner = LocalDockerCommandRunner()
    transport_source = (
        InstallTransportSource.PREPUBLICATION_CANDIDATE
        if profile == "OFFLINE_VALIDATE_ONLY"
        else InstallTransportSource.GITHUB
    )
    releases = CandidateReleasePort(
        loaded,
        transport_source=transport_source,
        image_acquirer=ImageAcquirer(runner=runner),
    )
    configuration = ProductionManagedConfigurationPort(namespace=namespace)
    compatibility = ProductionCompatibilityPort(releases)
    platform = ProductionPlatformPort(releases)
    doctor = ProductionDoctorAcceptance(
        releases=releases,
        compatibility=compatibility,
        runner=runner,
        namespace=namespace,
    )
    fresh = ProductionFreshInstallPort(
        releases=releases,
        configuration=configuration,
        doctor_acceptor=doctor,
        runner=runner,
        namespace=namespace,
    )
    gate = CandidateBootstrapPrivilegeGate(
        verified_prepublication_candidate_capability(verified_candidate_digest)
    )
    runtime = Installer(
        releases=releases,
        target=ProductionTargetPort(
            releases=releases,
            platform=platform,
            doctor=doctor,
            runner=runner,
            namespace=namespace,
        ),
        platform=platform,
        compatibility=compatibility,
        configuration=configuration,
        operations=ProductionOperationPort(namespace=namespace),
        fresh=fresh,
        restore=ProductionRestoreRuntimePort(
            releases=releases,
            configuration=configuration,
            fresh=fresh,
        ),
        bootstrap_privilege_gate=gate,
        namespace=namespace,
    )
    return ProductionInstallerComposition(
        runtime=runtime,
        releases=releases,
        platform=platform,
        bootstrap_privilege_gate=gate,
    )


def build_runtime(
    *,
    instance_name: InstanceName | str = DEFAULT_INSTANCE_NAME,
    transport_source: InstallTransportSource = InstallTransportSource.GITHUB,
    transport_policy: ExplicitTransportPolicy
    | LocalBundleTransportPolicy
    | None = None,
    local_bundle_payload: Path | None = None,
    local_bundle_release_attestation: Path | None = None,
) -> Installer:
    return build_production_composition(
        instance_name=instance_name,
        transport_source=transport_source,
        transport_policy=transport_policy,
        local_bundle_payload=local_bundle_payload,
        local_bundle_release_attestation=local_bundle_release_attestation,
    ).runtime


def production_configuration_doctor(
    snapshot,
    config: ManagedConfig,
    manifest: dict[str, object],
) -> None:
    """Reacquire exact Release authority and run complete config acceptance."""

    runner = LocalDockerCommandRunner()
    releases = ProductionReleasePort(image_acquirer=ImageAcquirer(runner=runner))
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
        runner=runner,
        managed_environment=dict(
            derive_runtime_environment(
                config,
                namespace=instance_namespace(snapshot.locator.instance_name),
                locator_digest=snapshot.digest,
            )
        ),
    )
    ProductionDoctorAcceptance(
        releases=releases,
        compatibility=compatibility,
        runner=runner,
        namespace=instance_namespace(snapshot.locator.instance_name),
    ).accept_existing(
        expected_instance_id=config.instance_id,
        release=release,
        platform=platform,
        deployment=deployment,
    )


__all__ = [
    "CandidateReleasePort",
    "ProductionCompatibilityPort",
    "ProductionDoctorAcceptance",
    "ProductionFreshInstallPort",
    "ProductionInstallerComposition",
    "ProductionManagedConfigurationPort",
    "ProductionOperationPort",
    "ProductionPlatformPort",
    "ProductionReleasePort",
    "ProductionRestoreRuntimePort",
    "ProductionTargetPort",
    "VerifiedPlatformBootstrapSession",
    "build_candidate_composition",
    "build_production_composition",
    "build_runtime",
    "collect_host_capabilities",
    "production_configuration_doctor",
]
