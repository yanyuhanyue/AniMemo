"""Canonical managed-configuration planning and apply transaction."""

from __future__ import annotations

import ipaddress
import json
import secrets
import socket
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import ClassVar, Protocol
from uuid import uuid4

from durability.canonical import canonical_json_bytes, sha256_identity
from durability.instance import (
    InstanceLocator,
    InstanceSnapshot,
    ListenIdentity,
    LocatorError,
    release_identity_from_manifest,
    replace_instance_locator,
)
from durability.managed_config import (
    DirectAccessConfig,
    ListenConfig,
    LocalManagedConfigStore,
    ManagedConfig,
    ManagedConfigError,
    canonical_managed_config_bytes,
    derive_runtime_environment,
    plan_config_change,
)
from durability.private_store import AtomicPrivateFile, PrivateStoreError

from .deployment import HostPaths, ImmutableComposeDeployment
from .errors import UpdaterError
from .slots import ReleaseSlots
from .state import UpdateLock

CONFIG_OPERATION_SCHEMA = "animemo.managed-config-operation/v1"
MAX_CONFIG_OPERATION_BYTES = 1024 * 1024


class ConfigurationError(UpdaterError):
    """Stable, secret-safe management-configuration failure."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class ConfigurationOutcome(StrEnum):
    VALIDATED = "VALIDATED"
    DRY_RUN = "DRY_RUN"
    NO_CHANGE = "NO_CHANGE"
    APPLIED = "APPLIED"
    CONFIG_APPLY_FAILED = "CONFIG_APPLY_FAILED"
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"


@dataclass(frozen=True)
class ConfigurationChangeRequest:
    public_origin: str | None = None
    listen: ListenConfig | None = None
    accept_direct_exposure: bool = False
    accept_insecure_http: bool = False


@dataclass(frozen=True)
class ConfigurationPlan:
    instance_id: str
    current_revision: str
    next_revision: str
    locator_digest: str
    current_public_origin: str
    next_public_origin: str
    current_listen: ListenConfig
    next_listen: ListenConfig
    changed_fields: tuple[str, ...]
    warnings: tuple[str, ...]
    plan_digest: str
    proposed: ManagedConfig = field(repr=False, compare=False)

    @property
    def no_change(self) -> bool:
        return not self.changed_fields

    def as_dict(
        self, *, outcome: ConfigurationOutcome = ConfigurationOutcome.VALIDATED
    ) -> dict[str, object]:
        return {
            "outcome": outcome.value,
            "planIdentity": "animemo.managed-config-plan/v1",
            "instanceId": self.instance_id,
            "currentRevision": self.current_revision,
            "nextRevision": self.next_revision,
            "locatorDigest": self.locator_digest,
            "currentPublicOrigin": self.current_public_origin,
            "nextPublicOrigin": self.next_public_origin,
            "currentListen": {
                "host": self.current_listen.host,
                "port": self.current_listen.port,
            },
            "nextListen": {
                "host": self.next_listen.host,
                "port": self.next_listen.port,
            },
            "changedFields": list(self.changed_fields),
            "warnings": list(self.warnings),
            "requiredReconcile": ["api", "web"] if not self.no_change else [],
            "requiredVerification": (
                ["localHealth", "exactRelease", "locator", "doctor"]
                if not self.no_change
                else []
            ),
            "planDigest": self.plan_digest,
        }


@dataclass(frozen=True)
class ConfigurationApplyResult:
    outcome: ConfigurationOutcome
    operation_id: str | None
    instance_id: str
    config_revision: str
    locator_digest: str
    plan_digest: str
    reason_code: str | None = None
    warnings: tuple[str, ...] = ()

    @property
    def manual_recovery_required(self) -> bool:
        return self.outcome is ConfigurationOutcome.RECOVERY_REQUIRED

    def as_dict(self) -> dict[str, object]:
        return {
            "outcome": self.outcome.value,
            "operationId": self.operation_id,
            "instanceId": self.instance_id,
            "configRevision": self.config_revision,
            "locatorDigest": self.locator_digest,
            "planDigest": self.plan_digest,
            "reasonCode": self.reason_code,
            "warnings": list(self.warnings),
            "manualRecoveryRequired": self.manual_recovery_required,
        }


class ConfigurationHost(Protocol):
    def snapshot(self) -> InstanceSnapshot: ...

    def acquire_lock(self) -> AbstractContextManager[object]: ...

    def current_manifest(self) -> dict[str, object]: ...

    def validate_listen(
        self, current: ListenConfig, proposed: ListenConfig
    ) -> None: ...

    def refresh_runtime(
        self, config: ManagedConfig, snapshot: InstanceSnapshot
    ) -> None: ...

    def reconcile_application(self, manifest: dict[str, object]) -> None: ...

    def verify_health_and_release(self, manifest: dict[str, object]) -> None: ...

    def replace_locator(
        self, locator: InstanceLocator, *, expected_digest: str
    ) -> InstanceSnapshot: ...

    def doctor_accept(
        self,
        snapshot: InstanceSnapshot,
        config: ManagedConfig,
        manifest: dict[str, object],
    ) -> None: ...


class ConfigurationOperationJournal(Protocol):
    def create(self, plan: ConfigurationPlan) -> str: ...

    def transition(
        self,
        operation_id: str,
        state: str,
        *,
        failure_code: str | None = None,
        manual_recovery_required: bool = False,
    ) -> None: ...


class LocalConfigurationOperationJournal:
    """Private, non-secret recovery evidence for one config transaction."""

    _TRANSITIONS: ClassVar[dict[str, set[str]]] = {
        "PLANNED": {"APPLYING", "CONFIG_APPLY_FAILED"},
        "APPLYING": {
            "VERIFYING",
            "ROLLING_BACK",
            "CONFIG_APPLY_FAILED",
            "RECOVERY_REQUIRED",
        },
        "VERIFYING": {"SUCCEEDED", "ROLLING_BACK", "RECOVERY_REQUIRED"},
        "ROLLING_BACK": {"ROLLED_BACK", "RECOVERY_REQUIRED"},
    }
    _FIELDS: ClassVar[frozenset[str]] = frozenset(
        {
            "schema",
            "operationId",
            "instanceId",
            "planDigest",
            "locatorDigest",
            "fromRevision",
            "toRevision",
            "changedFields",
            "state",
            "failureCode",
            "manualRecoveryRequired",
            "createdAt",
            "updatedAt",
        }
    )

    def __init__(self, state_root: Path = Path("/var/lib/animemo-updater")) -> None:
        self._root = state_root

    def _store(self, operation_id: str) -> AtomicPrivateFile:
        if len(operation_id) != 32 or any(
            character not in "0123456789abcdef" for character in operation_id
        ):
            raise ConfigurationError("CONFIG_OPERATION_ID_INVALID")
        return AtomicPrivateFile(
            self._root,
            f"config-operations/{operation_id}.json",
            create_parents=True,
        )

    @staticmethod
    def _bytes(payload: Mapping[str, object]) -> bytes:
        rendered = canonical_json_bytes(dict(payload)) + b"\n"
        if len(rendered) > MAX_CONFIG_OPERATION_BYTES:
            raise ConfigurationError("CONFIG_OPERATION_TOO_LARGE")
        return rendered

    @staticmethod
    def _now() -> str:
        return datetime.now(UTC).isoformat().replace("+00:00", "Z")

    @staticmethod
    def _loads(raw: bytes) -> object:
        def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
            result: dict[str, object] = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError
                result[key] = value
            return result

        return json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=reject_duplicates,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
        )

    def _read(self, operation_id: str) -> dict[str, object]:
        try:
            raw = self._store(operation_id).read(limit=MAX_CONFIG_OPERATION_BYTES)
            payload = self._loads(raw)
        except (
            PrivateStoreError,
            UnicodeError,
            ValueError,
            json.JSONDecodeError,
        ) as error:
            raise ConfigurationError("CONFIG_OPERATION_UNAVAILABLE") from error
        if (
            not isinstance(payload, dict)
            or frozenset(payload) != self._FIELDS
            or payload.get("schema") != CONFIG_OPERATION_SCHEMA
            or payload.get("operationId") != operation_id
        ):
            raise ConfigurationError("CONFIG_OPERATION_INVALID")
        return payload

    def create(self, plan: ConfigurationPlan) -> str:
        operation_id = secrets.token_hex(16)
        timestamp = self._now()
        payload = {
            "schema": CONFIG_OPERATION_SCHEMA,
            "operationId": operation_id,
            "instanceId": plan.instance_id,
            "planDigest": plan.plan_digest,
            "locatorDigest": plan.locator_digest,
            "fromRevision": plan.current_revision,
            "toRevision": plan.next_revision,
            "changedFields": list(plan.changed_fields),
            "state": "PLANNED",
            "failureCode": None,
            "manualRecoveryRequired": False,
            "createdAt": timestamp,
            "updatedAt": timestamp,
        }
        try:
            self._store(operation_id).write(self._bytes(payload), must_not_exist=True)
        except PrivateStoreError as error:
            raise ConfigurationError("CONFIG_OPERATION_WRITE_FAILED") from error
        return operation_id

    def transition(
        self,
        operation_id: str,
        state: str,
        *,
        failure_code: str | None = None,
        manual_recovery_required: bool = False,
    ) -> None:
        payload = self._read(operation_id)
        current = payload.get("state")
        if state not in self._TRANSITIONS.get(str(current), set()):
            raise ConfigurationError("CONFIG_OPERATION_TRANSITION_INVALID")
        if failure_code is not None and (
            not isinstance(failure_code, str)
            or not failure_code
            or len(failure_code) > 128
        ):
            raise ConfigurationError("CONFIG_OPERATION_FAILURE_CODE_INVALID")
        payload["state"] = state
        payload["failureCode"] = failure_code
        payload["manualRecoveryRequired"] = manual_recovery_required
        payload["updatedAt"] = self._now()
        try:
            self._store(operation_id).write(self._bytes(payload))
        except PrivateStoreError as error:
            raise ConfigurationError("CONFIG_OPERATION_WRITE_FAILED") from error


class LocalConfigurationHost:
    """Production host adapters behind the configuration transaction."""

    def __init__(
        self,
        *,
        registry,
        deployment: ImmutableComposeDeployment,
        slots: ReleaseSlots,
        doctor: Callable[[InstanceSnapshot, ManagedConfig, dict[str, object]], None],
        lock_path: Path = Path("/var/lib/animemo-updater/update.lock"),
    ) -> None:
        self.registry = registry
        self.deployment = deployment
        self.slots = slots
        self.doctor = doctor
        self.lock_path = lock_path

    def snapshot(self) -> InstanceSnapshot:
        return self.registry.snapshot()

    def acquire_lock(self) -> AbstractContextManager[object]:
        return UpdateLock(self.lock_path)

    def current_manifest(self) -> dict[str, object]:
        current = self.slots.read().get("current")
        if not isinstance(current, dict):
            raise ConfigurationError("CONFIG_CURRENT_RELEASE_UNAVAILABLE")
        return current

    def validate_listen(self, current: ListenConfig, proposed: ListenConfig) -> None:
        if current == proposed:
            return
        address = ipaddress.ip_address(proposed.host)
        family = socket.AF_INET6 if address.version == 6 else socket.AF_INET
        bind_host = (
            (address.compressed, proposed.port, 0, 0)
            if family == socket.AF_INET6
            else (address.compressed, proposed.port)
        )
        try:
            with socket.socket(family, socket.SOCK_STREAM) as candidate:
                candidate.bind(bind_host)
        except OSError as error:
            raise ConfigurationError("CONFIG_LISTEN_UNAVAILABLE") from error

    def refresh_runtime(
        self, config: ManagedConfig, snapshot: InstanceSnapshot
    ) -> None:
        self.deployment.refresh_binding(
            HostPaths.production(snapshot),
            managed_environment=dict(derive_runtime_environment(config)),
        )

    def reconcile_application(self, manifest: dict[str, object]) -> None:
        self.deployment.reconcile_application(manifest)

    def verify_health_and_release(self, manifest: dict[str, object]) -> None:
        # verify_health includes container, HTTP, and exact effective release checks.
        self.deployment.verify_health(manifest)

    def replace_locator(
        self, locator: InstanceLocator, *, expected_digest: str
    ) -> InstanceSnapshot:
        return replace_instance_locator(
            locator,
            expected_digest=expected_digest,
            store=self.registry.store,
            owner_uid=self.registry.expected_owner_uid,
            owner_gid=self.registry.expected_owner_gid,
        )

    def doctor_accept(
        self,
        snapshot: InstanceSnapshot,
        config: ManagedConfig,
        manifest: dict[str, object],
    ) -> None:
        self.doctor(snapshot, config, manifest)


class ConfigurationManager:
    """PLAN -> VALIDATE -> APPLY -> VERIFY for non-secret config changes."""

    def __init__(
        self,
        *,
        config_store: LocalManagedConfigStore,
        host: ConfigurationHost,
        journal: ConfigurationOperationJournal,
        revision_factory: Callable[[], str] = lambda: str(uuid4()),
    ) -> None:
        self.config_store = config_store
        self.host = host
        self.journal = journal
        self.revision_factory = revision_factory

    @staticmethod
    def _aligned(config: ManagedConfig, snapshot: InstanceSnapshot) -> bool:
        locator = snapshot.locator
        return (
            config.instance_id == locator.instance_id
            and config.config_revision == locator.config_revision
            and config.listen.host == locator.listen.host
            and config.listen.port == locator.listen.port
            and config.public_origin == locator.public_origin
        )

    @staticmethod
    def _failure_code(error: Exception) -> str:
        code = getattr(error, "code", None)
        if isinstance(code, str) and code and len(code) <= 128:
            return code
        return "CONFIG_APPLY_FAILED"

    @staticmethod
    def _assert_release(
        snapshot: InstanceSnapshot, manifest: dict[str, object]
    ) -> None:
        try:
            identity = release_identity_from_manifest(manifest)
        except LocatorError as error:
            raise ConfigurationError("CONFIG_CURRENT_RELEASE_INVALID") from error
        if dict(identity) != dict(snapshot.locator.release_identity):
            raise ConfigurationError("CONFIG_CURRENT_RELEASE_MISMATCH")

    def _current(self) -> tuple[ManagedConfig, InstanceSnapshot, dict[str, object]]:
        try:
            config = self.config_store.read()
            snapshot = self.host.snapshot()
            manifest = self.host.current_manifest()
        except (ManagedConfigError, LocatorError) as error:
            raise ConfigurationError(
                getattr(error, "code", "CONFIG_STATE_INVALID")
            ) from error
        if not self._aligned(config, snapshot):
            raise ConfigurationError("CONFIG_LOCATOR_MISMATCH")
        self._assert_release(snapshot, manifest)
        return config, snapshot, manifest

    def show(self) -> dict[str, object]:
        config, snapshot, _manifest = self._current()
        projection = config.secret_safe_dict()
        projection.update(
            {
                "trustedOrigins": {
                    "allowedHosts": list(config.trusted_origins.allowed_hosts),
                    "cors": list(config.trusted_origins.cors),
                    "csrf": list(config.trusted_origins.csrf),
                },
                "database": {
                    "name": config.database.name,
                    "user": config.database.user,
                },
                "application": {
                    "mediaPublicOrigin": config.application.media_public_origin,
                    "trustedProxyIps": list(config.application.trusted_proxy_ips),
                },
                "integrations": {
                    "bangumiOAuthClientId": (
                        config.integrations.bangumi_oauth_client_id
                    ),
                },
            }
        )
        return {
            "outcome": "SHOW",
            "configuration": projection,
            "locatorDigest": snapshot.digest,
            "alignment": "VALID",
        }

    @staticmethod
    def _direct_access(
        current: ManagedConfig, request: ConfigurationChangeRequest
    ) -> DirectAccessConfig:
        if not isinstance(request.accept_direct_exposure, bool) or not isinstance(
            request.accept_insecure_http, bool
        ):
            raise ConfigurationError("CONFIG_REQUEST_INVALID")
        listen = current.listen if request.listen is None else request.listen
        origin = (
            current.public_origin
            if request.public_origin is None
            else request.public_origin
        )
        if not isinstance(listen, ListenConfig):
            raise ConfigurationError("CONFIG_LISTEN_INVALID")
        if not isinstance(origin, str):
            raise ConfigurationError("CONFIG_PUBLIC_ORIGIN_INVALID")
        try:
            non_loopback = not listen.is_loopback
        except ValueError as error:
            raise ConfigurationError("CONFIG_LISTEN_INVALID") from error
        insecure_http = origin.startswith("http://")
        if (
            request.listen is not None
            and non_loopback
            and not request.accept_direct_exposure
        ):
            raise ConfigurationError("CONFIG_DIRECT_EXPOSURE_ACCEPTANCE_REQUIRED")
        if (
            request.public_origin is not None
            and insecure_http
            and not request.accept_insecure_http
        ):
            raise ConfigurationError("CONFIG_INSECURE_HTTP_ACCEPTANCE_REQUIRED")
        return DirectAccessConfig(
            allow_non_loopback=non_loopback,
            allow_http=insecure_http,
            warning_acknowledged=non_loopback or insecure_http,
        )

    @staticmethod
    def _plan_digest(change, locator_digest: str) -> str:
        body = {
            "planIdentity": "animemo.managed-config-plan/v1",
            "configPlanDigest": change.plan_digest,
            "instanceId": change.instance_id,
            "currentRevision": change.current_revision,
            "nextRevision": change.next_revision,
            "locatorDigest": locator_digest,
        }
        return sha256_identity(canonical_json_bytes(body))

    def validate(self, request: ConfigurationChangeRequest) -> ConfigurationPlan:
        if not isinstance(request, ConfigurationChangeRequest):
            raise ConfigurationError("CONFIG_REQUEST_INVALID")
        current, snapshot, _manifest = self._current()
        direct = self._direct_access(current, request)
        try:
            change, proposed = plan_config_change(
                current,
                next_revision=self.revision_factory(),
                public_origin=request.public_origin,
                listen=request.listen,
                direct_access=direct,
            )
            # Force complete config and runtime-env validation without exposing bytes.
            canonical_managed_config_bytes(proposed)
            derive_runtime_environment(proposed)
        except ManagedConfigError as error:
            raise ConfigurationError(error.code) from error
        if change.changed_fields:
            self.host.validate_listen(current.listen, proposed.listen)
        return ConfigurationPlan(
            instance_id=change.instance_id,
            current_revision=change.current_revision,
            next_revision=change.next_revision,
            locator_digest=snapshot.digest,
            current_public_origin=change.current_public_origin,
            next_public_origin=change.next_public_origin,
            current_listen=change.current_listen,
            next_listen=change.next_listen,
            changed_fields=change.changed_fields,
            warnings=change.warnings,
            plan_digest=self._plan_digest(change, snapshot.digest),
            proposed=proposed,
        )

    def set_origin(
        self, public_origin: str, *, accept_insecure_http: bool = False
    ) -> ConfigurationPlan:
        return self.validate(
            ConfigurationChangeRequest(
                public_origin=public_origin,
                accept_insecure_http=accept_insecure_http,
            )
        )

    def set_listen(
        self, listen: ListenConfig, *, accept_direct_exposure: bool = False
    ) -> ConfigurationPlan:
        return self.validate(
            ConfigurationChangeRequest(
                listen=listen,
                accept_direct_exposure=accept_direct_exposure,
            )
        )

    def dry_run(self, request: ConfigurationChangeRequest) -> dict[str, object]:
        return self.validate(request).as_dict(outcome=ConfigurationOutcome.DRY_RUN)

    def _assert_fresh(
        self, plan: ConfigurationPlan
    ) -> tuple[ManagedConfig, InstanceSnapshot, dict[str, object]]:
        current, snapshot, manifest = self._current()
        if (
            current.instance_id != plan.instance_id
            or current.config_revision != plan.current_revision
            or snapshot.digest != plan.locator_digest
        ):
            raise ConfigurationError("CONFIG_PLAN_STALE")
        try:
            change, proposed = plan_config_change(
                current,
                next_revision=plan.next_revision,
                public_origin=plan.next_public_origin,
                listen=plan.next_listen,
                direct_access=plan.proposed.direct_access,
            )
        except ManagedConfigError as error:
            raise ConfigurationError(error.code) from error
        if (
            proposed != plan.proposed
            or change.changed_fields != plan.changed_fields
            or self._plan_digest(change, snapshot.digest) != plan.plan_digest
        ):
            raise ConfigurationError("CONFIG_PLAN_INVALID")
        if change.changed_fields:
            self.host.validate_listen(current.listen, proposed.listen)
        return current, snapshot, manifest

    @staticmethod
    def _proposed_locator(
        snapshot: InstanceSnapshot, config: ManagedConfig
    ) -> InstanceLocator:
        return replace(
            snapshot.locator,
            listen=ListenIdentity(config.listen.host, config.listen.port),
            public_origin=config.public_origin,
            config_revision=config.config_revision,
        )

    @staticmethod
    def _pending_snapshot(
        snapshot: InstanceSnapshot, locator: InstanceLocator
    ) -> InstanceSnapshot:
        return replace(snapshot, locator=locator)

    def _rollback(
        self,
        *,
        operation_id: str,
        plan: ConfigurationPlan,
        previous: ManagedConfig,
        previous_snapshot: InstanceSnapshot,
        manifest: dict[str, object],
        failure_code: str,
    ) -> ConfigurationApplyResult:
        try:
            self.journal.transition(
                operation_id, "ROLLING_BACK", failure_code=failure_code
            )
            live_config = self.config_store.read()
            if live_config == plan.proposed:
                self.config_store.write(previous, expected_revision=plan.next_revision)
            elif live_config != previous:
                raise ConfigurationError("CONFIG_ROLLBACK_STATE_DIVERGED")
            self.config_store.rebuild_runtime_env(
                expected_revision=previous.config_revision
            )

            live_snapshot = self.host.snapshot()
            old_locator = previous_snapshot.locator
            new_locator = self._proposed_locator(previous_snapshot, plan.proposed)
            if live_snapshot.locator not in (old_locator, new_locator):
                raise ConfigurationError("CONFIG_ROLLBACK_LOCATOR_DIVERGED")

            pending = self._pending_snapshot(live_snapshot, old_locator)
            self.host.refresh_runtime(previous, pending)
            self.host.reconcile_application(manifest)
            self.host.verify_health_and_release(manifest)
            if live_snapshot.locator == new_locator:
                live_snapshot = self.host.replace_locator(
                    old_locator, expected_digest=live_snapshot.digest
                )
            final_snapshot = self.host.snapshot()
            if final_snapshot.locator != old_locator or not self._aligned(
                previous, final_snapshot
            ):
                raise ConfigurationError("CONFIG_ROLLBACK_VERIFICATION_FAILED")
            self.host.refresh_runtime(previous, final_snapshot)
            self.host.doctor_accept(final_snapshot, previous, manifest)
            self.journal.transition(
                operation_id, "ROLLED_BACK", failure_code=failure_code
            )
            return ConfigurationApplyResult(
                outcome=ConfigurationOutcome.CONFIG_APPLY_FAILED,
                operation_id=operation_id,
                instance_id=plan.instance_id,
                config_revision=previous.config_revision,
                locator_digest=final_snapshot.digest,
                plan_digest=plan.plan_digest,
                reason_code=failure_code,
                warnings=plan.warnings,
            )
        except Exception:  # noqa: BLE001 - any rollback failure requires recovery
            self._mark_recovery_required(operation_id, failure_code)
            try:
                live_snapshot = self.host.snapshot()
                revision = self.config_store.read().config_revision
                locator_digest = live_snapshot.digest
            except Exception:  # noqa: BLE001 - result must remain secret-safe
                revision = plan.next_revision
                locator_digest = plan.locator_digest
            return ConfigurationApplyResult(
                outcome=ConfigurationOutcome.RECOVERY_REQUIRED,
                operation_id=operation_id,
                instance_id=plan.instance_id,
                config_revision=revision,
                locator_digest=locator_digest,
                plan_digest=plan.plan_digest,
                reason_code=failure_code,
                warnings=plan.warnings,
            )

    def _mark_recovery_required(self, operation_id: str, failure_code: str) -> bool:
        try:
            self.journal.transition(
                operation_id,
                "RECOVERY_REQUIRED",
                failure_code=failure_code,
                manual_recovery_required=True,
            )
        except Exception:  # noqa: BLE001 - preserve the primary recovery result
            return False
        return True

    def _mark_apply_failed(self, operation_id: str, failure_code: str) -> bool:
        try:
            self.journal.transition(
                operation_id,
                "CONFIG_APPLY_FAILED",
                failure_code=failure_code,
            )
        except Exception:  # noqa: BLE001 - preserve the primary apply failure
            return False
        return True

    def apply(
        self, plan: ConfigurationPlan, *, accepted_plan_digest: str
    ) -> ConfigurationApplyResult:
        if (
            not isinstance(plan, ConfigurationPlan)
            or accepted_plan_digest != plan.plan_digest
        ):
            raise ConfigurationError("CONFIG_PLAN_ACCEPTANCE_REQUIRED")
        with self.host.acquire_lock():
            previous, previous_snapshot, manifest = self._assert_fresh(plan)
            if plan.no_change:
                return ConfigurationApplyResult(
                    outcome=ConfigurationOutcome.NO_CHANGE,
                    operation_id=None,
                    instance_id=plan.instance_id,
                    config_revision=previous.config_revision,
                    locator_digest=previous_snapshot.digest,
                    plan_digest=plan.plan_digest,
                )

            operation_id = self.journal.create(plan)
            config_write_attempted = False
            try:
                self.journal.transition(operation_id, "APPLYING")
                config_write_attempted = True
                self.config_store.write(
                    plan.proposed, expected_revision=plan.current_revision
                )
                self.config_store.rebuild_runtime_env(
                    expected_revision=plan.next_revision
                )
                proposed_locator = self._proposed_locator(
                    previous_snapshot, plan.proposed
                )
                pending_snapshot = self._pending_snapshot(
                    previous_snapshot, proposed_locator
                )
                self.host.refresh_runtime(plan.proposed, pending_snapshot)
                self.host.reconcile_application(manifest)
                self.journal.transition(operation_id, "VERIFYING")
                self.host.verify_health_and_release(manifest)
                published = self.host.replace_locator(
                    proposed_locator, expected_digest=plan.locator_digest
                )
                self.host.refresh_runtime(plan.proposed, published)
                final_snapshot = self.host.snapshot()
                if final_snapshot != published or not self._aligned(
                    plan.proposed, final_snapshot
                ):
                    raise ConfigurationError("CONFIG_APPLY_VERIFICATION_FAILED")
                self.host.doctor_accept(final_snapshot, plan.proposed, manifest)
                self.journal.transition(operation_id, "SUCCEEDED")
                return ConfigurationApplyResult(
                    outcome=ConfigurationOutcome.APPLIED,
                    operation_id=operation_id,
                    instance_id=plan.instance_id,
                    config_revision=plan.next_revision,
                    locator_digest=published.digest,
                    plan_digest=plan.plan_digest,
                    warnings=plan.warnings,
                )
            except Exception as error:  # noqa: BLE001 - adapters define failure boundary
                failure_code = self._failure_code(error)
                live_config: ManagedConfig | None = None
                if config_write_attempted:
                    try:
                        live_config = self.config_store.read()
                    except Exception:  # noqa: BLE001 - uncertain state enters rollback
                        live_config = None
                if not config_write_attempted or live_config == previous:
                    self._mark_apply_failed(operation_id, failure_code)
                    return ConfigurationApplyResult(
                        outcome=ConfigurationOutcome.CONFIG_APPLY_FAILED,
                        operation_id=operation_id,
                        instance_id=plan.instance_id,
                        config_revision=previous.config_revision,
                        locator_digest=previous_snapshot.digest,
                        plan_digest=plan.plan_digest,
                        reason_code=failure_code,
                        warnings=plan.warnings,
                    )
                return self._rollback(
                    operation_id=operation_id,
                    plan=plan,
                    previous=previous,
                    previous_snapshot=previous_snapshot,
                    manifest=manifest,
                    failure_code=failure_code,
                )


def build_configuration_manager(
    *,
    doctor: Callable[[InstanceSnapshot, ManagedConfig, dict[str, object]], None],
) -> ConfigurationManager:
    """Assemble the fixed production adapters with a complete Doctor gate."""

    if not callable(doctor):
        raise ConfigurationError("CONFIG_DOCTOR_ADAPTER_REQUIRED")
    from .runtime import production_runtime

    runtime = production_runtime()
    if runtime.registry is None:
        raise ConfigurationError("CONFIG_LOCATOR_UNAVAILABLE")
    host = LocalConfigurationHost(
        registry=runtime.registry,
        deployment=runtime.deployment,
        slots=runtime.slots,
        doctor=doctor,
        lock_path=runtime.paths.state_root / "update.lock",
    )
    return ConfigurationManager(
        config_store=LocalManagedConfigStore(),
        host=host,
        journal=LocalConfigurationOperationJournal(runtime.paths.state_root),
    )


__all__ = [
    "CONFIG_OPERATION_SCHEMA",
    "ConfigurationApplyResult",
    "ConfigurationChangeRequest",
    "ConfigurationError",
    "ConfigurationManager",
    "ConfigurationOutcome",
    "ConfigurationPlan",
    "LocalConfigurationHost",
    "LocalConfigurationOperationJournal",
    "build_configuration_manager",
]
