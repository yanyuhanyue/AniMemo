from __future__ import annotations

import json
import re
import secrets
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path
from typing import NoReturn
from uuid import UUID

from durability.canonical import canonical_json_bytes
from durability.private_store import AtomicPrivateFile, PrivateStoreError

OPERATION_FORMAT = "animemo.operation"
OPERATION_SCHEMA_VERSION = 1
FRESH_INSTALL_KIND = "fresh_install"
MAX_OPERATION_BYTES = 1024 * 1024

_ID = re.compile(r"^[0-9a-f]{32}$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_CODE = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")
_TIMESTAMP = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?Z$"
)


class FreshInstallStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED_NO_MUTATION = "failed_no_mutation"
    ROLLED_BACK = "rolled_back"
    MANUAL_RECOVERY_REQUIRED = "manual_recovery_required"
    RECONCILED = "reconciled"


class FreshInstallPhase(StrEnum):
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
    COMPLETE = "complete"


PHASE_ORDER = tuple(FreshInstallPhase)
_TERMINAL = frozenset(
    {
        FreshInstallStatus.SUCCEEDED,
        FreshInstallStatus.FAILED_NO_MUTATION,
        FreshInstallStatus.ROLLED_BACK,
        FreshInstallStatus.MANUAL_RECOVERY_REQUIRED,
        FreshInstallStatus.RECONCILED,
    }
)
_RECORD_FIELDS = frozenset(
    {
        "operationFormat",
        "schemaVersion",
        "id",
        "kind",
        "status",
        "phase",
        "instanceId",
        "planDigest",
        "releaseIdentityDigest",
        "deploymentIdentityDigest",
        "configRevision",
        "mutationOccurred",
        "irreversibleMutationStarted",
        "completedSteps",
        "failedStep",
        "errorCode",
        "targetActive",
        "createdAt",
        "updatedAt",
        "events",
    }
)
_EVENT_FIELDS = frozenset({"status", "phase", "at", "code"})


class FreshInstallOperationError(RuntimeError):
    """A stable, secret-safe operation journal failure."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class FreshInstallRecoveryRequired(FreshInstallOperationError):
    def __init__(self, operation_id: str) -> None:
        self.operation_id = operation_id
        super().__init__("FRESH_INSTALL_RECOVERY_REQUIRED")


@dataclass(frozen=True)
class OperationEvent:
    status: FreshInstallStatus
    phase: FreshInstallPhase
    at: str
    code: str

    def as_dict(self) -> dict[str, object]:
        return {
            "status": self.status.value,
            "phase": self.phase.value,
            "at": self.at,
            "code": self.code,
        }


@dataclass(frozen=True)
class FreshInstallOperation:
    operation_id: str
    status: FreshInstallStatus
    phase: FreshInstallPhase
    instance_id: str
    plan_digest: str
    release_identity_digest: str
    deployment_identity_digest: str
    config_revision: str
    mutation_occurred: bool
    irreversible_mutation_started: bool
    completed_steps: tuple[str, ...]
    failed_step: str | None
    error_code: str | None
    target_active: bool | None
    created_at: str
    updated_at: str
    events: tuple[OperationEvent, ...]

    @property
    def recovery_required(self) -> bool:
        return self.status is FreshInstallStatus.MANUAL_RECOVERY_REQUIRED

    def as_dict(self) -> dict[str, object]:
        return {
            "operationFormat": OPERATION_FORMAT,
            "schemaVersion": OPERATION_SCHEMA_VERSION,
            "id": self.operation_id,
            "kind": FRESH_INSTALL_KIND,
            "status": self.status.value,
            "phase": self.phase.value,
            "instanceId": self.instance_id,
            "planDigest": self.plan_digest,
            "releaseIdentityDigest": self.release_identity_digest,
            "deploymentIdentityDigest": self.deployment_identity_digest,
            "configRevision": self.config_revision,
            "mutationOccurred": self.mutation_occurred,
            "irreversibleMutationStarted": self.irreversible_mutation_started,
            "completedSteps": list(self.completed_steps),
            "failedStep": self.failed_step,
            "errorCode": self.error_code,
            "targetActive": self.target_active,
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
            "events": [event.as_dict() for event in self.events],
        }

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.as_dict()) + b"\n"


def _fail(code: str) -> NoReturn:
    raise FreshInstallOperationError(code)


def _timestamp(value: object) -> str:
    if not isinstance(value, str) or not _TIMESTAMP.fullmatch(value):
        _fail("FRESH_OPERATION_TIME_INVALID")
    return value


def _uuid(value: object, code: str) -> str:
    try:
        rendered = str(UUID(str(value)))
    except (TypeError, ValueError, AttributeError):
        _fail(code)
    if rendered != value:
        _fail(code)
    return rendered


def _digest(value: object, code: str) -> str:
    if not isinstance(value, str) or not _DIGEST.fullmatch(value):
        _fail(code)
    return value


def _event(value: object) -> OperationEvent:
    if not isinstance(value, dict) or frozenset(value) != _EVENT_FIELDS:
        _fail("FRESH_OPERATION_EVENT_INVALID")
    try:
        status = FreshInstallStatus(value["status"])
        phase = FreshInstallPhase(value["phase"])
    except (TypeError, ValueError):
        _fail("FRESH_OPERATION_EVENT_INVALID")
    code = value["code"]
    if not isinstance(code, str) or not _CODE.fullmatch(code):
        _fail("FRESH_OPERATION_EVENT_INVALID")
    return OperationEvent(status, phase, _timestamp(value["at"]), code)


def parse_fresh_install_operation(payload: object) -> FreshInstallOperation:
    if not isinstance(payload, dict) or frozenset(payload) != _RECORD_FIELDS:
        _fail("FRESH_OPERATION_SCHEMA_INVALID")
    if payload["operationFormat"] != OPERATION_FORMAT:
        _fail("FRESH_OPERATION_FORMAT_UNSUPPORTED")
    if payload["schemaVersion"] != OPERATION_SCHEMA_VERSION:
        _fail("FRESH_OPERATION_SCHEMA_UNSUPPORTED")
    if payload["kind"] != FRESH_INSTALL_KIND:
        _fail("FRESH_OPERATION_KIND_UNSUPPORTED")
    operation_id = payload["id"]
    if not isinstance(operation_id, str) or not _ID.fullmatch(operation_id):
        _fail("FRESH_OPERATION_ID_INVALID")
    try:
        status = FreshInstallStatus(payload["status"])
        phase = FreshInstallPhase(payload["phase"])
    except (TypeError, ValueError):
        _fail("FRESH_OPERATION_STATE_INVALID")
    mutation = payload["mutationOccurred"]
    irreversible = payload["irreversibleMutationStarted"]
    if not isinstance(mutation, bool) or not isinstance(irreversible, bool):
        _fail("FRESH_OPERATION_STATE_INVALID")
    if irreversible and not mutation:
        _fail("FRESH_OPERATION_STATE_INVALID")
    steps = payload["completedSteps"]
    if not isinstance(steps, list) or len(steps) > len(PHASE_ORDER):
        _fail("FRESH_OPERATION_STEPS_INVALID")
    if any(not isinstance(step, str) for step in steps):
        _fail("FRESH_OPERATION_STEPS_INVALID")
    expected_prefix = [phase.value for phase in PHASE_ORDER[: len(steps)]]
    if steps != expected_prefix or len(set(steps)) != len(steps):
        _fail("FRESH_OPERATION_STEPS_INVALID")
    failed_step = payload["failedStep"]
    if failed_step is not None:
        try:
            FreshInstallPhase(failed_step)
        except (TypeError, ValueError):
            _fail("FRESH_OPERATION_STATE_INVALID")
    error_code = payload["errorCode"]
    if error_code is not None and (
        not isinstance(error_code, str) or not _CODE.fullmatch(error_code)
    ):
        _fail("FRESH_OPERATION_ERROR_INVALID")
    target_active = payload["targetActive"]
    if target_active is not None and not isinstance(target_active, bool):
        _fail("FRESH_OPERATION_STATE_INVALID")
    events_raw = payload["events"]
    if not isinstance(events_raw, list) or not 1 <= len(events_raw) <= 64:
        _fail("FRESH_OPERATION_EVENT_INVALID")
    events = tuple(_event(item) for item in events_raw)
    if events[-1].status is not status or events[-1].phase is not phase:
        _fail("FRESH_OPERATION_EVENT_INVALID")
    created_at = _timestamp(payload["createdAt"])
    updated_at = _timestamp(payload["updatedAt"])
    if events[0].at != created_at or events[-1].at != updated_at:
        _fail("FRESH_OPERATION_EVENT_INVALID")
    if status in _TERMINAL and status is not FreshInstallStatus.RECONCILED:
        if status is FreshInstallStatus.SUCCEEDED:
            if phase is not FreshInstallPhase.COMPLETE or error_code is not None:
                _fail("FRESH_OPERATION_STATE_INVALID")
        elif error_code is None or failed_step is None:
            _fail("FRESH_OPERATION_STATE_INVALID")
    if status is FreshInstallStatus.MANUAL_RECOVERY_REQUIRED and not mutation:
        _fail("FRESH_OPERATION_STATE_INVALID")
    if status is FreshInstallStatus.FAILED_NO_MUTATION and mutation:
        _fail("FRESH_OPERATION_STATE_INVALID")
    return FreshInstallOperation(
        operation_id=operation_id,
        status=status,
        phase=phase,
        instance_id=_uuid(payload["instanceId"], "FRESH_OPERATION_INSTANCE_INVALID"),
        plan_digest=_digest(payload["planDigest"], "FRESH_OPERATION_PLAN_INVALID"),
        release_identity_digest=_digest(
            payload["releaseIdentityDigest"], "FRESH_OPERATION_RELEASE_INVALID"
        ),
        deployment_identity_digest=_digest(
            payload["deploymentIdentityDigest"],
            "FRESH_OPERATION_DEPLOYMENT_INVALID",
        ),
        config_revision=_uuid(
            payload["configRevision"], "FRESH_OPERATION_CONFIG_INVALID"
        ),
        mutation_occurred=mutation,
        irreversible_mutation_started=irreversible,
        completed_steps=tuple(steps),
        failed_step=failed_step,
        error_code=error_code,
        target_active=target_active,
        created_at=created_at,
        updated_at=updated_at,
        events=events,
    )


def parse_fresh_install_operation_bytes(raw: bytes) -> FreshInstallOperation:
    if not isinstance(raw, bytes) or len(raw) > MAX_OPERATION_BYTES:
        _fail("FRESH_OPERATION_SIZE_INVALID")

    def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError
            result[key] = value
        return result

    try:
        payload = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=reject_duplicates,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError()),
        )
    except (UnicodeError, ValueError, json.JSONDecodeError):
        _fail("FRESH_OPERATION_CONTENT_INVALID")
    return parse_fresh_install_operation(payload)


def create_fresh_install_operation(
    *,
    instance_id: str,
    plan_digest: str,
    release_identity_digest: str,
    deployment_identity_digest: str,
    config_revision: str,
    at: str,
    operation_id: str | None = None,
) -> FreshInstallOperation:
    identifier = operation_id or secrets.token_hex(16)
    if not _ID.fullmatch(identifier):
        _fail("FRESH_OPERATION_ID_INVALID")
    timestamp = _timestamp(at)
    operation = FreshInstallOperation(
        operation_id=identifier,
        status=FreshInstallStatus.RUNNING,
        phase=FreshInstallPhase.PREFLIGHT_VERIFIED,
        instance_id=_uuid(instance_id, "FRESH_OPERATION_INSTANCE_INVALID"),
        plan_digest=_digest(plan_digest, "FRESH_OPERATION_PLAN_INVALID"),
        release_identity_digest=_digest(
            release_identity_digest, "FRESH_OPERATION_RELEASE_INVALID"
        ),
        deployment_identity_digest=_digest(
            deployment_identity_digest, "FRESH_OPERATION_DEPLOYMENT_INVALID"
        ),
        config_revision=_uuid(config_revision, "FRESH_OPERATION_CONFIG_INVALID"),
        mutation_occurred=False,
        irreversible_mutation_started=False,
        completed_steps=(),
        failed_step=None,
        error_code=None,
        target_active=False,
        created_at=timestamp,
        updated_at=timestamp,
        events=(
            OperationEvent(
                FreshInstallStatus.RUNNING,
                FreshInstallPhase.PREFLIGHT_VERIFIED,
                timestamp,
                "FRESH_INSTALL_CREATED",
            ),
        ),
    )
    return parse_fresh_install_operation(operation.as_dict())


def transition_phase(
    operation: FreshInstallOperation,
    next_phase: FreshInstallPhase,
    *,
    at: str,
) -> FreshInstallOperation:
    if operation.status is not FreshInstallStatus.RUNNING:
        _fail("FRESH_OPERATION_TERMINAL")
    current_index = PHASE_ORDER.index(operation.phase)
    if (
        current_index + 1 >= len(PHASE_ORDER)
        or PHASE_ORDER[current_index + 1] is not next_phase
    ):
        _fail("FRESH_OPERATION_TRANSITION_INVALID")
    timestamp = _timestamp(at)
    result = replace(
        operation,
        phase=next_phase,
        completed_steps=(*operation.completed_steps, operation.phase.value),
        updated_at=timestamp,
        events=(
            *operation.events,
            OperationEvent(
                FreshInstallStatus.RUNNING,
                next_phase,
                timestamp,
                "FRESH_INSTALL_PHASE_ENTERED",
            ),
        ),
    )
    return parse_fresh_install_operation(result.as_dict())


def mark_mutation_started(
    operation: FreshInstallOperation, *, at: str
) -> FreshInstallOperation:
    if operation.status is not FreshInstallStatus.RUNNING:
        _fail("FRESH_OPERATION_TERMINAL")
    if operation.mutation_occurred:
        return operation
    timestamp = _timestamp(at)
    result = replace(
        operation,
        mutation_occurred=True,
        updated_at=timestamp,
        events=(
            *operation.events,
            OperationEvent(
                operation.status,
                operation.phase,
                timestamp,
                "FRESH_INSTALL_MUTATION_STARTED",
            ),
        ),
    )
    return parse_fresh_install_operation(result.as_dict())


def mark_irreversible_mutation_started(
    operation: FreshInstallOperation, *, at: str
) -> FreshInstallOperation:
    if (
        operation.status is not FreshInstallStatus.RUNNING
        or operation.phase is not FreshInstallPhase.DATABASE_MIGRATING
        or not operation.mutation_occurred
    ):
        _fail("FRESH_OPERATION_IRREVERSIBLE_BOUNDARY_INVALID")
    if operation.irreversible_mutation_started:
        return operation
    timestamp = _timestamp(at)
    result = replace(
        operation,
        irreversible_mutation_started=True,
        updated_at=timestamp,
        events=(
            *operation.events,
            OperationEvent(
                operation.status,
                operation.phase,
                timestamp,
                "FRESH_INSTALL_IRREVERSIBLE_MUTATION_STARTED",
            ),
        ),
    )
    return parse_fresh_install_operation(result.as_dict())


def fail_fresh_install(
    operation: FreshInstallOperation,
    *,
    error_code: str,
    at: str,
    rollback_succeeded: bool | None = None,
    target_active: bool | None = False,
) -> FreshInstallOperation:
    if operation.status is not FreshInstallStatus.RUNNING:
        _fail("FRESH_OPERATION_TERMINAL")
    if not _CODE.fullmatch(error_code):
        _fail("FRESH_OPERATION_ERROR_INVALID")
    if not operation.mutation_occurred:
        status = FreshInstallStatus.FAILED_NO_MUTATION
    elif operation.irreversible_mutation_started or rollback_succeeded is not True:
        status = FreshInstallStatus.MANUAL_RECOVERY_REQUIRED
    else:
        status = FreshInstallStatus.ROLLED_BACK
    timestamp = _timestamp(at)
    result = replace(
        operation,
        status=status,
        failed_step=operation.phase.value,
        error_code=error_code,
        target_active=target_active,
        updated_at=timestamp,
        events=(
            *operation.events,
            OperationEvent(status, operation.phase, timestamp, error_code),
        ),
    )
    return parse_fresh_install_operation(result.as_dict())


def succeed_fresh_install(
    operation: FreshInstallOperation, *, at: str
) -> FreshInstallOperation:
    if (
        operation.status is not FreshInstallStatus.RUNNING
        or operation.phase is not FreshInstallPhase.DOCTOR
    ):
        _fail("FRESH_OPERATION_TRANSITION_INVALID")
    timestamp = _timestamp(at)
    result = replace(
        operation,
        status=FreshInstallStatus.SUCCEEDED,
        phase=FreshInstallPhase.COMPLETE,
        completed_steps=(*operation.completed_steps, operation.phase.value),
        target_active=True,
        updated_at=timestamp,
        events=(
            *operation.events,
            OperationEvent(
                FreshInstallStatus.SUCCEEDED,
                FreshInstallPhase.COMPLETE,
                timestamp,
                "FRESH_INSTALL_SUCCEEDED",
            ),
        ),
    )
    return parse_fresh_install_operation(result.as_dict())


def reconcile_fresh_install(
    operation: FreshInstallOperation, *, at: str
) -> FreshInstallOperation:
    if operation.status is not FreshInstallStatus.MANUAL_RECOVERY_REQUIRED:
        _fail("FRESH_OPERATION_RECONCILE_INVALID")
    timestamp = _timestamp(at)
    result = replace(
        operation,
        status=FreshInstallStatus.RECONCILED,
        updated_at=timestamp,
        events=(
            *operation.events,
            OperationEvent(
                FreshInstallStatus.RECONCILED,
                operation.phase,
                timestamp,
                "FRESH_INSTALL_RECONCILED",
            ),
        ),
    )
    return parse_fresh_install_operation(result.as_dict())


class FreshInstallOperationJournal:
    """Versioned Fresh records in the shared durable operations directory."""

    def __init__(self, state_root: Path = Path("/var/lib/animemo-updater")) -> None:
        self.state_root = state_root

    def _file(self, operation_id: str) -> AtomicPrivateFile:
        if not isinstance(operation_id, str) or not _ID.fullmatch(operation_id):
            _fail("FRESH_OPERATION_ID_INVALID")
        return AtomicPrivateFile(
            self.state_root,
            f"operations/{operation_id}.json",
            create_parents=True,
        )

    def create(self, operation: FreshInstallOperation) -> None:
        try:
            self._file(operation.operation_id).write(
                operation.canonical_bytes(), must_not_exist=True
            )
        except PrivateStoreError as error:
            raise FreshInstallOperationError(error.code) from None

    def load(self, operation_id: str) -> FreshInstallOperation:
        try:
            raw = self._file(operation_id).read(limit=MAX_OPERATION_BYTES)
        except PrivateStoreError as error:
            raise FreshInstallOperationError(error.code) from None
        return parse_fresh_install_operation_bytes(raw)

    def persist(
        self,
        previous: FreshInstallOperation,
        updated: FreshInstallOperation,
    ) -> None:
        if previous.operation_id != updated.operation_id:
            _fail("FRESH_OPERATION_ID_MISMATCH")
        current = self.load(previous.operation_id)
        if current.canonical_bytes() != previous.canonical_bytes():
            _fail("FRESH_OPERATION_STALE")
        try:
            self._file(updated.operation_id).write(updated.canonical_bytes())
        except PrivateStoreError as error:
            raise FreshInstallOperationError(error.code) from None

    def recovery_block(self) -> FreshInstallOperation | None:
        operations_path = self.state_root / "operations"
        if not operations_path.exists():
            return None
        if operations_path.is_symlink() or (
            hasattr(operations_path, "is_junction") and operations_path.is_junction()
        ):
            _fail("FRESH_OPERATION_DIRECTORY_INVALID")
        blocked: list[FreshInstallOperation] = []
        for path in sorted(operations_path.glob("*.json")):
            try:
                store = self._file(path.stem)
                raw = store.read(limit=MAX_OPERATION_BYTES)
                outer = json.loads(raw)
            except PrivateStoreError as error:
                raise FreshInstallOperationError(error.code) from None
            except (ValueError, json.JSONDecodeError):
                _fail("FRESH_OPERATION_CONTENT_INVALID")
            if not isinstance(outer, dict) or outer.get("kind") != FRESH_INSTALL_KIND:
                continue
            operation = parse_fresh_install_operation_bytes(raw)
            if operation.recovery_required:
                blocked.append(operation)
        if not blocked:
            return None
        return max(blocked, key=lambda item: (item.updated_at, item.operation_id))

    def require_recovery_clear(self) -> None:
        blocked = self.recovery_block()
        if blocked is not None:
            raise FreshInstallRecoveryRequired(blocked.operation_id)


__all__ = [
    "FRESH_INSTALL_KIND",
    "OPERATION_FORMAT",
    "OPERATION_SCHEMA_VERSION",
    "PHASE_ORDER",
    "FreshInstallOperation",
    "FreshInstallOperationError",
    "FreshInstallOperationJournal",
    "FreshInstallPhase",
    "FreshInstallRecoveryRequired",
    "FreshInstallStatus",
    "create_fresh_install_operation",
    "fail_fresh_install",
    "mark_irreversible_mutation_started",
    "mark_mutation_started",
    "parse_fresh_install_operation",
    "parse_fresh_install_operation_bytes",
    "reconcile_fresh_install",
    "succeed_fresh_install",
    "transition_phase",
]
