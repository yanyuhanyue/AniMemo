"""Production Restore-to-New adapters for Installer Runtime v1.

The orchestration and backup state machine remain owned by
``durability.restore``.  This module binds its ports to the canonical host,
verified Installer materials, managed configuration, Compose, and the one
Updater initial-adoption interface.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import socket
import stat
import subprocess
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import BinaryIO

from durability import backup, restore
from durability.canonical import canonical_json_bytes, sha256_identity
from durability.compatibility import (
    CompatibilityOutcome,
    Dimension,
    DimensionAssessment,
    ReasonCode,
    UpgradeAction,
)
from durability.instance import InstanceNamespace, instance_locator_digest
from durability.managed_config import (
    derive_runtime_environment,
)
from durability.restore import (
    DestinationClass,
    DestinationSnapshot,
    EnvelopeSecretResolver,
    NoneSecretResolver,
    ProcessResult,
    RecoveryEvidence,
    RestoreAdapterError,
    RestoreCompatibilityEvidence,
    RestorePlan,
    RestorePreflightError,
    RestoreRequest,
    RestoreTerminalState,
    SecretResolution,
    SubprocessPostgresRestore,
    UpdaterEvidence,
    ValidationReport,
    execute_restore,
    prepare_restore,
)
from durability.secret_envelope import (
    MAX_PASSPHRASE_BYTES,
    OneTimeKey,
    OpenedSecretPayload,
    Passphrase,
    SecretEnvelopeError,
)
from installer.operations import RestoreOperationJournal
from installer.runtime import (
    ConfigPlanEvidence,
    InstallerAdapterError,
    InstallerError,
    InstallOutcome,
    InstallPlan,
    PlatformEvidence,
    ReleaseEvidence,
    RestorePlanEvidence,
    RestoreProtectionKind,
    RestoreProtectionRequest,
    TargetClass,
    TargetEvidence,
)
from updater.deployment import ImmutableComposeDeployment
from updater.runtime import InitialAdoptionRequest
from updater.slots import ReleaseSlots
from updater.state import UpdateLock

_SAFE_SOURCE_MODES = frozenset({0o600, 0o640, 0o644, 0o700, 0o750, 0o755})


def _stable_restore_error(code: str) -> RestoreAdapterError:
    return RestoreAdapterError(code)


def _manifest_members(manifest: Mapping[str, object]) -> list[Mapping[str, object]]:
    filesystem = manifest.get("filesystem")
    members = filesystem.get("members") if isinstance(filesystem, Mapping) else None
    if not isinstance(members, list) or any(
        not isinstance(item, Mapping) for item in members
    ):
        raise RestorePreflightError("RESTORE_MEMBER_METADATA_INVALID")
    return members


def _safe_path(path: Path) -> None:
    absolute = Path(path)
    if not absolute.is_absolute():
        raise InstallerError(
            "INSTALL_RESTORE_PROTECTION_PATH_INVALID",
            outcome=InstallOutcome.VALIDATION_FAILED,
        )
    for parent in (absolute.parent, *absolute.parents):
        try:
            metadata = parent.lstat()
        except OSError:
            raise InstallerError(
                "INSTALL_RESTORE_PROTECTION_UNAVAILABLE",
                outcome=InstallOutcome.ENVIRONMENT_FAILED,
            ) from None
        is_link = parent.is_symlink() or (
            hasattr(parent, "is_junction") and parent.is_junction()
        )
        if is_link or not stat.S_ISDIR(metadata.st_mode):
            raise InstallerError(
                "INSTALL_RESTORE_PROTECTION_PATH_UNSAFE",
                outcome=InstallOutcome.VALIDATION_FAILED,
            )
        if parent == parent.parent:
            break


def _read_protected_file(path: Path, *, limit: int) -> bytes:
    _safe_path(path)
    try:
        before = path.lstat()
    except OSError:
        raise InstallerError(
            "INSTALL_RESTORE_PROTECTION_UNAVAILABLE",
            outcome=InstallOutcome.ENVIRONMENT_FAILED,
        ) from None
    is_link = path.is_symlink() or (
        hasattr(path, "is_junction") and path.is_junction()
    )
    if (
        is_link
        or not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
        or (os.name == "posix" and stat.S_IMODE(before.st_mode) != 0o600)
        or (os.name == "posix" and before.st_uid not in {0, os.geteuid()})
    ):
        raise InstallerError(
            "INSTALL_RESTORE_PROTECTION_FILE_UNSAFE",
            outcome=InstallOutcome.VALIDATION_FAILED,
        )
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError:
        raise InstallerError(
            "INSTALL_RESTORE_PROTECTION_UNAVAILABLE",
            outcome=InstallOutcome.ENVIRONMENT_FAILED,
        ) from None
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or (metadata.st_dev, metadata.st_ino) != (before.st_dev, before.st_ino)
            or (os.name == "posix" and stat.S_IMODE(metadata.st_mode) != 0o600)
            or (
                os.name == "posix"
                and metadata.st_uid not in {0, os.geteuid()}
            )
        ):
            raise InstallerError(
                "INSTALL_RESTORE_PROTECTION_FILE_UNSAFE",
                outcome=InstallOutcome.VALIDATION_FAILED,
            )
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            payload = handle.read(limit + 1)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if len(payload) > limit:
        raise InstallerError(
            "INSTALL_RESTORE_PROTECTION_INPUT_INVALID",
            outcome=InstallOutcome.VALIDATION_FAILED,
        )
    return payload


def _read_secret_fd(fd: int, *, limit: int) -> bytes:
    try:
        descriptor = os.dup(fd)
    except OSError:
        raise InstallerError(
            "INSTALL_RESTORE_PROTECTION_UNAVAILABLE",
            outcome=InstallOutcome.ENVIRONMENT_FAILED,
        ) from None
    try:
        metadata = os.fstat(descriptor)
        if stat.S_ISREG(metadata.st_mode) and (
            metadata.st_nlink != 1
            or (os.name == "posix" and metadata.st_mode & 0o077)
        ):
            raise InstallerError(
                "INSTALL_RESTORE_PROTECTION_FILE_UNSAFE",
                outcome=InstallOutcome.VALIDATION_FAILED,
            )
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            payload = handle.read(limit + 1)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if len(payload) > limit:
        raise InstallerError(
            "INSTALL_RESTORE_PROTECTION_INPUT_INVALID",
            outcome=InstallOutcome.VALIDATION_FAILED,
        )
    return payload


def _passphrase_bytes(value: bytes) -> bytes:
    if value.endswith(b"\r\n"):
        return value[:-2]
    if value.endswith(b"\n"):
        return value[:-1]
    return value


def _secret_resolver(request: RestoreProtectionRequest):
    try:
        if request.kind is RestoreProtectionKind.NONE:
            return NoneSecretResolver()
        if request.kind is RestoreProtectionKind.ONE_TIME_KEY_FILE:
            assert request.path is not None
            value = _read_protected_file(request.path, limit=32)
            return EnvelopeSecretResolver(OneTimeKey.from_bytes(value))
        if request.kind is RestoreProtectionKind.PASSPHRASE_FILE:
            assert request.path is not None
            value = _read_protected_file(
                request.path, limit=MAX_PASSPHRASE_BYTES + 2
            )
            return EnvelopeSecretResolver(
                Passphrase.from_bytes(_passphrase_bytes(value))
            )
        assert request.fd is not None
        value = _read_secret_fd(request.fd, limit=MAX_PASSPHRASE_BYTES + 2)
        return EnvelopeSecretResolver(Passphrase.from_bytes(_passphrase_bytes(value)))
    except InstallerError:
        raise
    except SecretEnvelopeError as error:
        raise InstallerError(
            error.code,
            outcome=InstallOutcome.VALIDATION_FAILED,
        ) from None


class ProductionRestoreDestination:
    def __init__(self, target: TargetEvidence, namespace: InstanceNamespace) -> None:
        self.target = target
        self.namespace = namespace
        if target.classification is TargetClass.ABSENT:
            classification = DestinationClass.FRESH
            empty_verified = False
            parent_ready = True
        elif target.classification is TargetClass.VERIFIED_EMPTY:
            classification = DestinationClass.EXISTING_EMPTY
            empty_verified = True
            parent_ready = False
        else:
            classification = DestinationClass.PARTIAL_AMBIGUOUS
            empty_verified = False
            parent_ready = False
        self.snapshot = DestinationSnapshot(
            classification=classification,
            instance_name=namespace.name,
            deployment_profile="v1.1-instance-scoped",
            canonical_roots=restore.canonical_roots_for(namespace.name),
            ownership_verified=classification
            in {DestinationClass.FRESH, DestinationClass.EXISTING_EMPTY},
            empty_verified=empty_verified,
            parent_ready=parent_ready,
            evidence_digest=target.evidence_digest,
        )

    def inspect(self) -> DestinationSnapshot:
        # UpdateLock creates one private lock file before Restore's locked
        # destination recheck.  It is operation evidence, not an active instance.
        state_root = Path(str(self.namespace.updater_state_root))
        if state_root.exists():
            try:
                names = {item.name for item in state_root.iterdir()}
            except OSError:
                raise RestorePreflightError("RESTORE_DESTINATION_UNAVAILABLE") from None
            allowed = {"update.lock"}
            if names - allowed:
                # Before target.begin no other state is allowed.  After begin the
                # destination is not inspected again by Restore Runtime.
                raise RestorePreflightError("RESTORE_DESTINATION_CHANGED")
        return self.snapshot


class ProductionRestoreRelease:
    def __init__(self, releases, selected: ReleaseEvidence) -> None:
        self.releases = releases
        self.selected = selected

    def verify(self, manifest: Mapping[str, object]) -> restore.ReleaseEvidence:
        source = manifest.get("source")
        if not isinstance(source, Mapping):
            raise RestorePreflightError("RESTORE_RELEASE_IDENTITY_INVALID")
        release = source.get("release")
        deployment = source.get("deploymentContract")
        if not isinstance(release, Mapping) or not isinstance(deployment, Mapping):
            raise RestorePreflightError("RESTORE_RELEASE_IDENTITY_INVALID")
        selected_manifest = self.releases.materials_for(self.selected).manifest
        if (
            release.get("version") != self.selected.version
            or release.get("commit") != self.selected.commit
            or deployment.get("digest") != self.selected.deployment_identity_digest
            or selected_manifest["release"]["version"] != self.selected.version
            or selected_manifest["release"]["commit"] != self.selected.commit
        ):
            raise RestorePreflightError("RESTORE_EXACT_RELEASE_UNAVAILABLE")
        return restore.ReleaseEvidence(
            release_identity_digest=self.selected.manifest_digest,
            deployment_identity_digest=self.selected.deployment_identity_digest,
        )

    def acquire(self, evidence: restore.ReleaseEvidence):
        if evidence != self.verify_for_selected():
            raise RestorePreflightError("RESTORE_RELEASE_CHANGED")
        return self.releases.materials_for(self.selected)

    def verify_for_selected(self) -> restore.ReleaseEvidence:
        return restore.ReleaseEvidence(
            release_identity_digest=self.selected.manifest_digest,
            deployment_identity_digest=self.selected.deployment_identity_digest,
        )


class ProductionRestoreUpdater:
    def __init__(self, selected: ReleaseEvidence) -> None:
        self.selected = selected

    def verify(
        self,
        manifest: Mapping[str, object],
        release_evidence: restore.ReleaseEvidence,
    ) -> UpdaterEvidence:
        if release_evidence.release_identity_digest != self.selected.manifest_digest:
            raise RestorePreflightError("RESTORE_UPDATER_RELEASE_MISMATCH")
        members = _manifest_members(manifest)
        state_members = [
            item
            for item in members
            if isinstance(item, Mapping)
            and str(item.get("path", "")).startswith("updater-state/")
        ]
        if not state_members:
            raise RestorePreflightError("RESTORE_UPDATER_STATE_INVALID")
        return UpdaterEvidence(
            state_identity_digest=sha256_identity(
                canonical_json_bytes(state_members)
            ),
            pending_state_preserved=True,
        )

    def stage(self, manifest, evidence, mutation) -> None:
        if not isinstance(mutation, ProductionRestoreMutation):
            raise RestoreAdapterError("RESTORE_UPDATER_ADAPTER_INVALID")
        expected = self.verify(manifest, mutation.restore_plan.release)
        if expected != evidence:
            raise RestoreAdapterError("RESTORE_UPDATER_STATE_CHANGED")
        mutation.stage_updater()


class ProductionRestoreCompatibility:
    def __init__(
        self,
        *,
        selected: ReleaseEvidence,
        platform: PlatformEvidence,
        manifest: Mapping[str, object],
    ) -> None:
        self.selected = selected
        self.platform = platform
        self.release_manifest = manifest

    @staticmethod
    def _assessment(
        dimension: Dimension,
        outcome: CompatibilityOutcome,
        reason: ReasonCode,
        source: Mapping[str, object],
        target: Mapping[str, object],
    ) -> DimensionAssessment:
        return DimensionAssessment(dimension, outcome, reason, source, target)

    def assess(
        self,
        manifest: Mapping[str, object],
        destination: DestinationSnapshot,
        release_evidence: restore.ReleaseEvidence,
        updater_evidence: UpdaterEvidence,
    ) -> RestoreCompatibilityEvidence:
        del updater_evidence
        source = manifest.get("source")
        if not isinstance(source, Mapping):
            raise RestorePreflightError("RESTORE_COMPATIBILITY_EVIDENCE_INVALID")
        source_db = source.get("databaseContract")
        source_config = source.get("configurationContract")
        source_deployment = source.get("deploymentContract")
        compatibility = self.release_manifest["compatibility"]
        target_db = compatibility["database"]
        target_config = compatibility["configuration"]
        if not all(
            isinstance(item, Mapping)
            for item in (source_db, source_config, source_deployment)
        ):
            raise RestorePreflightError("RESTORE_COMPATIBILITY_EVIDENCE_INVALID")
        db_id = source_db.get("id")
        config_id = source_config.get("id")
        db_target = target_db["contract"]
        config_target = target_config["contract"]
        actions: tuple[UpgradeAction, ...] = ()
        if db_id == db_target:
            schema_outcome = CompatibilityOutcome.COMPATIBLE
            schema_reason = ReasonCode.SCHEMA_CONTRACTS_SUPPORTED
        elif (
            db_id in target_db["appAccepts"]
            and target_db["migration"]["required"] is True
            and target_db["migration"]["policy"] == "forward-only"
        ):
            schema_outcome = CompatibilityOutcome.REQUIRES_UPGRADE
            schema_reason = ReasonCode.SCHEMA_MIGRATION_REQUIRED
            actions = (
                UpgradeAction(
                    order=1,
                    kind="APPLY_FORWARD_MIGRATION",
                    input_identity={"databaseContract": db_id},
                    output_identity={"databaseContract": db_target},
                    required_release_identity={
                        "manifestDigest": self.selected.manifest_digest
                    },
                ),
            )
        else:
            schema_outcome = CompatibilityOutcome.UNSUPPORTED
            schema_reason = ReasonCode.SCHEMA_CONTRACT_UNSUPPORTED
        if config_id not in target_config["appAccepts"]:
            schema_outcome = CompatibilityOutcome.UNSUPPORTED
            schema_reason = ReasonCode.SCHEMA_CONTRACT_UNSUPPORTED
            actions = ()
        dimensions = (
            self._assessment(
                Dimension.DEPLOYMENT_CONTRACT,
                CompatibilityOutcome.COMPATIBLE,
                ReasonCode.DEPLOYMENT_CONTRACT_SUPPORTED,
                {"digest": source_deployment.get("digest")},
                {"digest": release_evidence.deployment_identity_digest},
            ),
            self._assessment(
                Dimension.SCHEMA_CONTRACTS,
                schema_outcome,
                schema_reason,
                {"database": db_id, "configuration": config_id},
                {"database": db_target, "configuration": config_target},
            ),
            self._assessment(
                Dimension.EXACT_RELEASE_IDENTITY,
                CompatibilityOutcome.COMPATIBLE,
                ReasonCode.RELEASE_IDENTITY_VERIFIED,
                {"manifestDigest": release_evidence.release_identity_digest},
                {"manifestDigest": self.selected.manifest_digest},
            ),
            self._assessment(
                Dimension.PLATFORM_RUNTIME,
                CompatibilityOutcome.COMPATIBLE,
                ReasonCode.PLATFORM_RUNTIME_SUPPORTED,
                {"profile": self.platform.profile},
                {"evidenceDigest": self.platform.evidence_digest},
            ),
            self._assessment(
                Dimension.SUPPORTED_PATH,
                CompatibilityOutcome.COMPATIBLE,
                ReasonCode.DIRECT_PATH_SUPPORTED,
                {"mode": "restore-to-new"},
                {"destination": destination.classification.value},
            ),
        )
        return RestoreCompatibilityEvidence(dimensions, actions)


class DockerPostgresProcessRunner:
    """Feed psql through fixed Compose stdin without putting secrets in argv."""

    def __init__(
        self,
        deployment: ImmutableComposeDeployment,
        manifest: dict[str, object],
    ) -> None:
        self.deployment = deployment
        self.manifest = manifest

    def run(
        self,
        argv: Sequence[str],
        *,
        stdin: BinaryIO | None,
        env: Mapping[str, str],
        timeout: int,
    ) -> ProcessResult:
        del env
        if not argv or Path(argv[0]).name.casefold() not in {"psql", "psql.exe"}:
            raise RestoreAdapterError("DATABASE_TOOL_INVALID")
        env_files = [
            "--env-file",
            str(self.deployment.paths.managed_env_path),
        ]
        command = [
            "/usr/bin/docker",
            "compose",
            "--project-name",
            self.deployment.paths.compose_project,
            *env_files,
            "-f",
            str(self.deployment.compose_file),
            "-f",
            str(self.deployment.updater_compose_file),
            "exec",
            "-T",
            "postgres",
            "sh",
            "-c",
            'exec psql --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" "$@"',
            "restore-psql",
            *argv[1:],
        ]
        try:
            completed = subprocess.run(
                command,
                cwd=self.deployment.paths.app_root,
                env=self.deployment._environment(self.manifest),
                stdin=stdin,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=timeout,
                shell=False,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            raise RestoreAdapterError("DATABASE_IMPORT_FAILED") from None
        return ProcessResult(completed.returncode, completed.stdout)


class ProductionRestoreDatabase:
    def __init__(self, mutation: ProductionRestoreMutation) -> None:
        self.mutation = mutation

    def restore(self, dump_path: Path) -> None:
        plan = self.mutation.installation_plan
        if plan is None:
            raise RestoreAdapterError("RESTORE_CONFIGURATION_NOT_BOUND")
        config = self.mutation.configuration.config_for(plan.configuration)
        deployment = self.mutation.fresh.deployment_for(plan)
        manifest = self.mutation.fresh.manifest_for(plan)
        database_url = derive_runtime_environment(config)["DATABASE_URL"]
        SubprocessPostgresRestore(
            database_url,
            runner=DockerPostgresProcessRunner(deployment, manifest),
        ).restore(dump_path)


class ProductionRestoreMutation:
    def __init__(self, *, fresh, configuration, installer_id: str) -> None:
        self.fresh = fresh
        self.namespace = fresh.namespace
        self.configuration = configuration
        self.installer_id = installer_id
        self.installation_plan: InstallPlan | None = None
        self.restore_plan: RestorePlan | None = None
        self.reconfigure_names: tuple[str, ...] = ()
        self.journal = RestoreOperationJournal(
            Path(str(self.namespace.updater_state_root))
        )
        self.locator = None
        self.adoption_ready = False

    def bind(
        self,
        installation_plan: InstallPlan,
        restore_plan: RestorePlan,
        reconfigure_names: tuple[str, ...],
    ) -> None:
        if installation_plan.configuration.instance_id == restore_plan.instance_id:
            raise RestoreAdapterError("RESTORE_TARGET_INSTANCE_ID_REUSED")
        self.installation_plan = installation_plan
        self.restore_plan = restore_plan
        self.reconfigure_names = reconfigure_names

    def _plan(self) -> InstallPlan:
        if self.installation_plan is None:
            raise RestoreAdapterError("RESTORE_CONFIGURATION_NOT_BOUND")
        return self.installation_plan

    def acquire_lock(self, operation_id: str):
        if self.restore_plan is not None and operation_id != self.restore_plan.operation_id:
            raise RestoreAdapterError("RESTORE_OPERATION_ID_CHANGED")
        return UpdateLock(Path(str(self.namespace.update_lock_path)))

    def begin(self, plan: RestorePlan) -> None:
        self.restore_plan = plan
        self.journal.begin(self.installer_id, plan)
        try:
            self.fresh.prepare_roots(self._plan())
        except InstallerAdapterError as error:
            raise _stable_restore_error(error.code) from None

    def stage_release(self, release_material, evidence) -> None:
        del evidence
        expected = self.fresh.releases.materials_for(self._plan().release)
        if release_material is not expected:
            raise RestoreAdapterError("RESTORE_RELEASE_MATERIAL_CHANGED")
        try:
            self.fresh.stage_release(self._plan())
        except InstallerAdapterError as error:
            raise _stable_restore_error(error.code) from None

    def stage_secret(self, resolution: SecretResolution) -> None:
        if self.restore_plan is None or resolution.as_dict() != self.restore_plan.protection:
            raise RestoreAdapterError("RESTORE_PROTECTION_CHANGED")
        try:
            self.fresh.publish_config(self._plan())
        except InstallerAdapterError as error:
            raise _stable_restore_error(error.code) from None

    def prepare_database(self) -> None:
        try:
            self.fresh.prepare_services(self._plan())
        except InstallerAdapterError as error:
            raise _stable_restore_error(error.code) from None

    def restore_filesystem(
        self, backup_root: Path, member_paths: tuple[str, ...]
    ) -> None:
        self._plan()
        staging = Path(
            str(self.namespace.data_root / f".restore-{self.installer_id}")
        )
        try:
            restore.LocalFilesystemStager().stage(
                backup_root,
                staging,
                member_paths,
            )
            manifest = json.loads((backup_root / backup.MANIFEST_NAME).read_bytes())
            members = _manifest_members(manifest)
            modes = {
                item["path"]: item.get("sourceMode")
                for item in members
                if isinstance(item, dict) and isinstance(item.get("path"), str)
            }
            archive_root = Path(
                str(
                    self.namespace.data_root
                    / "private"
                    / "restore-source"
                    / self.installer_id
                )
            )
            mappings = (
                (
                    "filesystem/plugins/cas/",
                    Path(str(self.namespace.data_root / "plugins" / "cas")),
                ),
                (
                    "filesystem/plugins/durable/",
                    Path(str(self.namespace.data_root / "plugins" / "durable")),
                ),
                (
                    "filesystem/media/",
                    Path(str(self.namespace.data_root / "media")),
                ),
                (
                    "filesystem/private/",
                    Path(str(self.namespace.data_root / "private")),
                ),
                ("filesystem/config/", archive_root / "config"),
                ("updater-state/", archive_root / "updater-state"),
            )
            for logical in member_paths:
                destination_root = None
                relative = None
                for prefix, root in mappings:
                    if logical.startswith(prefix):
                        destination_root = root
                        relative = logical[len(prefix) :]
                        break
                if destination_root is None or relative is None or not relative:
                    raise RestoreAdapterError("FILESYSTEM_MEMBER_UNSUPPORTED")
                source = staging / PurePosixPath(logical)
                destination = destination_root / PurePosixPath(relative)
                destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                if destination.exists() or destination.is_symlink():
                    raise RestoreAdapterError("FILESYSTEM_TARGET_NOT_EMPTY")
                raw_mode = modes.get(logical)
                try:
                    source_mode = int(str(raw_mode), 8)
                except (TypeError, ValueError):
                    raise RestoreAdapterError(
                        "FILESYSTEM_MEMBER_METADATA_INVALID"
                    ) from None
                if source_mode not in _SAFE_SOURCE_MODES:
                    raise RestoreAdapterError("FILESYSTEM_MEMBER_MODE_UNSAFE")
                os.replace(source, destination)
                os.chmod(destination, source_mode)
                if os.name == "posix":
                    os.chown(destination, 10001, 10001)
            shutil.rmtree(staging)
        except RestoreAdapterError:
            raise
        except (OSError, ValueError, json.JSONDecodeError):
            raise RestoreAdapterError("FILESYSTEM_RESTORE_FAILED") from None

    def stage_updater(self) -> None:
        plan = self._plan()
        launcher = Path("/opt/animemo-updater/launcher")
        expected = self.fresh.releases.materials_for(plan.release).material(
            "deploy/updater/animemo-updater"
        )
        try:
            if (
                launcher.read_bytes() != expected.read_bytes()
                or stat.S_IMODE(launcher.lstat().st_mode) != 0o755
            ):
                raise OSError
            slots = ReleaseSlots(Path(str(self.namespace.release_slots_root))).read()
            if slots["current"] is not None or slots["previous"] is not None:
                raise OSError
        except OSError:
            raise RestoreAdapterError("RESTORE_UPDATER_STAGING_FAILED") from None
        self.adoption_ready = True

    def apply_upgrade(self, actions: tuple[UpgradeAction, ...]) -> None:
        if tuple(action.order for action in actions) != tuple(
            range(1, len(actions) + 1)
        ):
            raise RestoreAdapterError("RESTORE_UPGRADE_ACTION_INVALID")
        try:
            self.fresh.deployment_for(self._plan()).migrate(
                self.fresh.manifest_for(self._plan())
            )
        except Exception:  # noqa: BLE001 - fixed deployment adapter boundary
            raise RestoreAdapterError("RESTORE_UPGRADE_APPLY_FAILED") from None

    def bootstrap(self) -> None:
        plan = self._plan()
        try:
            deployment = self.fresh.deployment_for(plan)
            if self.reconfigure_names:
                deployment.apply_restore_secret_disposition(
                    self.fresh.manifest_for(plan),
                    self.reconfigure_names,
                )
            self.fresh.bootstrap(plan)
        except Exception:  # noqa: BLE001 - fixed deployment adapter boundary
            raise RestoreAdapterError("RESTORE_BOOTSTRAP_FAILED") from None

    def rebuild_runtime(self) -> None:
        try:
            self.fresh.start_runtime(self._plan())
        except InstallerAdapterError as error:
            raise _stable_restore_error(error.code) from None

    def build_locator(
        self,
        instance_id: str,
        release_evidence: restore.ReleaseEvidence,
    ) -> None:
        plan = self._plan()
        if (
            plan.restore is None
            or instance_id != plan.restore.instance_id
            or release_evidence.release_identity_digest
            != plan.release.manifest_digest
        ):
            raise RestoreAdapterError("RESTORE_LOCATOR_IDENTITY_INVALID")
        self.locator = self.fresh.locator_for(plan)

    def rotate_authentication_epoch(self) -> None:
        plan = self._plan()
        try:
            self.fresh.deployment_for(plan).rotate_restored_authentication_epoch(
                self.fresh.manifest_for(plan)
            )
        except Exception:  # noqa: BLE001 - fixed deployment adapter boundary
            raise RestoreAdapterError("RESTORE_AUTHENTICATION_ROTATION_FAILED") from None

    def publish(self) -> None:
        if self.locator is None or not self.adoption_ready or self.restore_plan is None:
            raise RestoreAdapterError("RESTORE_PUBLICATION_NOT_READY")
        try:
            self.fresh.adopt_updater(self._plan())
            self.journal.published(
                self.installer_id,
                self.restore_plan,
                completed_steps=restore.REQUIRED_VALIDATIONS,
            )
        except InstallerAdapterError as error:
            raise _stable_restore_error(error.code) from None

    def record_recovery_required(self, evidence: RecoveryEvidence) -> None:
        self.journal.recovery(self.installer_id, evidence)


class ProductionRestoreValidation:
    def __init__(self, mutation: ProductionRestoreMutation) -> None:
        self.mutation = mutation

    @staticmethod
    def _digest(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return "sha256:" + digest.hexdigest()

    def _filesystem_layout(self) -> None:
        required = {
            Path(str(self.mutation.namespace.data_root / "config")): 0o700,
            Path(str(self.mutation.namespace.data_root / "postgres")): 0o700,
            Path(str(self.mutation.namespace.data_root / "redis")): 0o700,
            Path(str(self.mutation.namespace.data_root / "plugins")): 0o755,
            Path(str(self.mutation.namespace.data_root / "media")): 0o755,
            Path(str(self.mutation.namespace.data_root / "private")): 0o700,
        }
        for path, mode in required.items():
            metadata = path.lstat()
            if (
                path.is_symlink()
                or not stat.S_ISDIR(metadata.st_mode)
                or stat.S_IMODE(metadata.st_mode) != mode
            ):
                raise RestoreAdapterError("RESTORE_FILESYSTEM_LAYOUT_INVALID")

    def _payload_integrity(self, manifest: Mapping[str, object], prefix: str) -> None:
        try:
            members = _manifest_members(manifest)
        except RestorePreflightError:
            raise RestoreAdapterError("RESTORE_MEMBER_METADATA_INVALID") from None
        for item in members:
            if not isinstance(item, Mapping):
                raise RestoreAdapterError("RESTORE_MEMBER_METADATA_INVALID")
            logical = item.get("path")
            if not isinstance(logical, str) or not logical.startswith(prefix):
                continue
            relative = logical[len(prefix) :]
            if prefix == "filesystem/media/":
                target = Path(
                    str(self.mutation.namespace.data_root / "media")
                ) / PurePosixPath(relative)
            else:
                suffix = logical[len("filesystem/plugins/") :]
                target = Path(
                    str(self.mutation.namespace.data_root / "plugins")
                ) / PurePosixPath(suffix)
            if self._digest(target) != item.get("sha256"):
                raise RestoreAdapterError("RESTORE_PAYLOAD_INTEGRITY_FAILED")

    def validate(
        self,
        manifest: Mapping[str, object],
        plan: RestorePlan,
        mutation,
    ) -> ValidationReport:
        if mutation is not self.mutation:
            raise RestoreAdapterError("RESTORE_VALIDATOR_MUTATION_MISMATCH")
        install = mutation._plan()
        deployment = mutation.fresh.deployment_for(install)
        release_manifest = mutation.fresh.manifest_for(install)
        passed: set[str] = set()

        deployment.probe_postgres(release_manifest)
        passed.add("database.usable")
        contracts = deployment.inspect_runtime_contracts(release_manifest)
        if (
            contracts["databaseContract"]
            != release_manifest["compatibility"]["database"]["contract"]
        ):
            raise RestoreAdapterError("RESTORE_DATABASE_SCHEMA_INVALID")
        passed.add("database.schema_contract")

        config = mutation.configuration.config_for(install.configuration)
        if (
            config.instance_id == plan.instance_id
            or mutation.locator is None
            or mutation.locator.instance_id != config.instance_id
        ):
            raise RestoreAdapterError("RESTORE_INSTANCE_IDENTITY_INVALID")
        passed.add("instance.identity")
        self._filesystem_layout()
        passed.add("filesystem.layout")

        deployment.probe_api(release_manifest)
        passed.add("service.api.health")
        deployment.probe_web(release_manifest)
        passed.add("service.web.health")
        if not mutation.adoption_ready or mutation.locator is None:
            raise RestoreAdapterError("RESTORE_UPDATER_STATE_INVALID")
        InitialAdoptionRequest(locator=mutation.locator, manifest=release_manifest)
        passed.add("updater.state")
        deployment.verify_health(release_manifest)
        passed.add("release.identity")

        enabled = deployment.inspect_enabled_plugin_apis(release_manifest)
        supported = set(
            release_manifest["compatibility"]["pluginSdk"]["supportedApis"]
        )
        if not enabled.issubset(supported):
            raise RestoreAdapterError("RESTORE_PLUGIN_INTEGRITY_FAILED")
        self._payload_integrity(manifest, "filesystem/plugins/")
        passed.add("plugins.integrity")
        self._payload_integrity(manifest, "filesystem/media/")
        passed.add("media.integrity")

        mutation.configuration.store.rebuild_runtime_env(
            locator_digest=instance_locator_digest(mutation.locator),
            expected_revision=config.config_revision
        )
        deployment.validate_compose(release_manifest)
        passed.add("runtime.rebuilt")

        address = config.listen.host
        if address in {"0.0.0.0", "::"}:
            address = "127.0.0.1" if address == "0.0.0.0" else "::1"
        with socket.create_connection((address, config.listen.port), timeout=5):
            pass
        if (
            config.public_origin != install.configuration.public_origin
            or config.listen.host != install.configuration.listen_host
            or config.listen.port != install.configuration.listen_port
        ):
            raise RestoreAdapterError("RESTORE_PUBLIC_ORIGIN_LISTEN_INVALID")
        passed.add("public_origin.listen")

        app_checks = deployment.inspect_restore_integrity(release_manifest)
        passed.update(app_checks)
        if passed != set(restore.REQUIRED_VALIDATIONS):
            raise RestoreAdapterError("RESTORE_VALIDATION_INCOMPLETE")
        ordered = tuple(
            check for check in restore.REQUIRED_VALIDATIONS if check in passed
        )
        return ValidationReport(
            passed_checks=ordered,
            evidence_digest=sha256_identity(
                canonical_json_bytes(
                    {
                        "restorePlanDigest": plan.plan_digest,
                        "checks": list(ordered),
                        "releaseManifestDigest": install.release.manifest_digest,
                        "configRevision": install.configuration.config_revision,
                    }
                )
            ),
        )


@dataclass
class _RestoreContext:
    installer_id: str
    request: RestoreRequest
    restore_plan: RestorePlan
    evidence: RestorePlanEvidence
    mutation: ProductionRestoreMutation
    resolver: object
    backup_root: Path
    reconfigure_names: tuple[str, ...] = ()
    installation_plan: InstallPlan | None = None


class ProductionRestoreRuntimePort:
    def __init__(self, *, releases, configuration, fresh) -> None:
        self.releases = releases
        self.configuration = configuration
        self.fresh = fresh
        self._contexts: dict[str, _RestoreContext] = {}

    @staticmethod
    def _manifest(backup_root: Path) -> Mapping[str, object]:
        backup.verify_backup(backup_root)
        try:
            payload = json.loads((backup_root / backup.MANIFEST_NAME).read_bytes())
        except (OSError, ValueError, json.JSONDecodeError):
            raise InstallerError(
                "INSTALL_RESTORE_BACKUP_INVALID",
                outcome=InstallOutcome.VALIDATION_FAILED,
            ) from None
        if not isinstance(payload, Mapping):
            raise InstallerError(
                "INSTALL_RESTORE_BACKUP_INVALID",
                outcome=InstallOutcome.VALIDATION_FAILED,
            )
        return payload

    def prepare(
        self,
        *,
        operation_id: str,
        backup_root: Path,
        release: ReleaseEvidence,
        target: TargetEvidence,
        platform: PlatformEvidence,
        protection: RestoreProtectionRequest,
    ) -> RestorePlanEvidence:
        if operation_id in self._contexts:
            raise InstallerError(
                "INSTALL_RESTORE_OPERATION_DUPLICATE",
                outcome=InstallOutcome.VALIDATION_FAILED,
            )
        resolver = _secret_resolver(protection)
        self._manifest(backup_root)
        mutation = ProductionRestoreMutation(
            fresh=self.fresh,
            configuration=self.configuration,
            installer_id=operation_id,
        )
        release_port = ProductionRestoreRelease(self.releases, release)
        updater = ProductionRestoreUpdater(release)
        request = RestoreRequest(
            operation_id=str(uuid.UUID(hex=operation_id)),
            backup_root=backup_root,
            destination=ProductionRestoreDestination(target, self.fresh.namespace),
            release=release_port,
            updater=updater,
            secret_resolver=resolver,
            compatibility=ProductionRestoreCompatibility(
                selected=release,
                platform=platform,
                manifest=self.releases.materials_for(release).manifest,
            ),
            database=ProductionRestoreDatabase(mutation),
            mutation=mutation,
            validator=ProductionRestoreValidation(mutation),
        )
        try:
            restore_plan = prepare_restore(request)
        except restore.RestoreError as error:
            raise InstallerError(
                error.code,
                outcome=InstallOutcome.VALIDATION_FAILED,
            ) from None
        evidence = RestorePlanEvidence(
            operation_id=operation_id,
            instance_id=restore_plan.instance_id,
            restore_plan_digest=restore_plan.plan_digest,
            backup_identity_digest=restore_plan.artifact_manifest_digest,
        )
        self._contexts[operation_id] = _RestoreContext(
            installer_id=operation_id,
            request=request,
            restore_plan=restore_plan,
            evidence=evidence,
            mutation=mutation,
            resolver=resolver,
            backup_root=backup_root,
        )
        return evidence

    @staticmethod
    def _secret_text(entry) -> str:
        try:
            return entry.reveal().decode("utf-8")
        except (UnicodeError, SecretEnvelopeError):
            raise InstallerError(
                "INSTALL_RESTORE_SECRET_INVALID",
                outcome=InstallOutcome.VALIDATION_FAILED,
            ) from None

    def bind_configuration(
        self,
        plan: RestorePlanEvidence,
        configuration: ConfigPlanEvidence,
    ) -> ConfigPlanEvidence:
        context = self._contexts.get(plan.operation_id)
        if context is None or context.evidence != plan:
            raise InstallerError(
                "INSTALL_RESTORE_PLAN_STALE",
                outcome=InstallOutcome.VALIDATION_FAILED,
            )
        config = self.configuration.config_for(configuration)
        manifest = self._manifest(context.backup_root)
        resolution = context.resolver.authenticate(context.backup_root, manifest)
        reconfigure: list[str] = []
        if resolution.mode == "envelope":
            opened = resolution.handle
            if (
                not isinstance(opened, OpenedSecretPayload)
                or opened.source_instance_id != plan.instance_id
            ):
                raise InstallerError(
                    "INSTALL_RESTORE_SECRET_IDENTITY_INVALID",
                    outcome=InstallOutcome.VALIDATION_FAILED,
                )
            preserved = {
                entry.name: self._secret_text(entry)
                for entry in opened.entries
                if entry.handling == "PRESERVE"
            }
            reconfigure = sorted(
                entry.name
                for entry in opened.entries
                if entry.handling == "RECONFIGURE"
            )
            credential = preserved.get("CREDENTIAL_ENCRYPTION_KEY")
            if credential is None:
                raise InstallerError(
                    "INSTALL_RESTORE_CREDENTIAL_KEY_MISSING",
                    outcome=InstallOutcome.VALIDATION_FAILED,
                )
            try:
                decoded = base64.urlsafe_b64decode(credential.encode("ascii"))
            except (UnicodeEncodeError, ValueError):
                decoded = b""
            if len(decoded) != 32:
                raw = opened.get_secret("CREDENTIAL_ENCRYPTION_KEY").reveal()
                if len(raw) != 32:
                    raise InstallerError(
                        "INSTALL_RESTORE_CREDENTIAL_KEY_INVALID",
                        outcome=InstallOutcome.VALIDATION_FAILED,
                    )
                credential = base64.urlsafe_b64encode(raw).decode("ascii")
            application = replace(
                config.application,
                credential_encryption_key=credential,
                django_secret_key=preserved.get(
                    "DJANGO_SECRET_KEY", config.application.django_secret_key
                ),
            )
            integrations = replace(
                config.integrations,
                bangumi_oauth_client_secret=preserved.get(
                    "BANGUMI_OAUTH_CLIENT_SECRET",
                    config.integrations.bangumi_oauth_client_secret,
                ),
                resend_api_key=preserved.get(
                    "RESEND_API_KEY", config.integrations.resend_api_key
                ),
            )
            config = replace(
                config,
                application=application,
                integrations=integrations,
            )
        updated = self.configuration.replace_planned_config(configuration, config)
        context.reconfigure_names = tuple(
            name
            for name in reconfigure
            if name
            in {
                "BANGUMI_OAUTH_CLIENT_SECRET",
                "RESEND_API_KEY",
                "TURNSTILE_SECRET",
            }
        )
        return updated

    def revalidate(self, plan: RestorePlanEvidence) -> None:
        context = self._contexts.get(plan.operation_id)
        if context is None or context.evidence != plan:
            raise InstallerError(
                "INSTALL_RESTORE_PLAN_STALE",
                outcome=InstallOutcome.VALIDATION_FAILED,
            )
        try:
            current = prepare_restore(context.request)
        except restore.RestoreError as error:
            raise InstallerError(
                error.code,
                outcome=InstallOutcome.VALIDATION_FAILED,
            ) from None
        if current.plan_digest != plan.restore_plan_digest:
            raise InstallerError(
                "INSTALL_RESTORE_PLAN_STALE",
                outcome=InstallOutcome.VALIDATION_FAILED,
            )

    def execute(
        self,
        plan: RestorePlanEvidence,
        *,
        accepted_plan_digest: str,
        installation_plan: InstallPlan,
    ) -> tuple[str, ...]:
        context = self._contexts.get(plan.operation_id)
        if (
            context is None
            or context.evidence != plan
            or accepted_plan_digest != plan.restore_plan_digest
            or installation_plan.restore != plan
            or installation_plan.configuration.instance_id == plan.instance_id
        ):
            raise InstallerError(
                "INSTALL_RESTORE_PLAN_NOT_ACCEPTED",
                outcome=InstallOutcome.VALIDATION_FAILED,
            )
        context.installation_plan = installation_plan
        context.mutation.bind(
            installation_plan,
            context.restore_plan,
            context.reconfigure_names,
        )
        try:
            result = execute_restore(
                context.request,
                context.restore_plan,
                accepted_plan_digest=accepted_plan_digest,
                accept_upgrade=context.restore_plan.decision.outcome
                is CompatibilityOutcome.REQUIRES_UPGRADE,
            )
        except restore.RestoreError as error:
            raise InstallerAdapterError(
                error.code,
                mutation_occurred=True,
                recovery_required=True,
            ) from None
        if result.state is RestoreTerminalState.RECOVERY_REQUIRED:
            code = (
                result.recovery_evidence.error_code
                if result.recovery_evidence is not None
                else "RESTORE_RECOVERY_REQUIRED"
            )
            raise InstallerAdapterError(
                code,
                mutation_occurred=True,
                recovery_required=True,
            )
        try:
            self.fresh.doctor_acceptance(installation_plan)
            context.mutation.journal.doctor_succeeded(
                context.installer_id,
                context.restore_plan,
                completed_steps=restore.REQUIRED_VALIDATIONS,
            )
        except Exception:  # noqa: BLE001 - Doctor is a redacted adapter boundary
            try:
                context.mutation.journal.doctor_failed(
                    context.installer_id,
                    context.restore_plan,
                    completed_steps=restore.REQUIRED_VALIDATIONS,
                )
            except Exception:  # noqa: BLE001 - evidence failure supersedes detail
                raise InstallerAdapterError(
                    "INSTALL_RECOVERY_EVIDENCE_FAILED",
                    mutation_occurred=True,
                    recovery_required=True,
                ) from None
            raise InstallerAdapterError(
                "INSTALL_DOCTOR_FAILED",
                mutation_occurred=True,
                recovery_required=True,
            ) from None
        return (*result.completed_steps, "doctor.accept")


__all__ = ["ProductionRestoreRuntimePort", "RestoreOperationJournal"]
