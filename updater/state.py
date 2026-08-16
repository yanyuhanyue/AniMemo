from __future__ import annotations

import json
import os
import secrets
import stat
import tempfile
import threading
import time
from contextlib import AbstractContextManager
from datetime import datetime, timezone
from pathlib import Path

from .errors import OperationInProgress, RecoveryRequired, StateError
from .redaction import redact

_LOCK_REGISTRY_GUARD = threading.RLock()
_LOCK_OWNERS: dict[str, tuple[int, int]] = {}

TERMINAL_STATES = {
    "succeeded",
    "failed_pre_switch",
    "failed_post_switch",
    "rolled_back",
    "manual_recovery_required",
    "reconciled",
}
TRANSITIONS = {
    "idle": {"preflight", "failed_pre_switch"},
    "preflight": {"fetching", "failed_pre_switch"},
    "fetching": {"verifying", "failed_pre_switch"},
    "verifying": {"backup", "pulling", "adopting", "failed_pre_switch"},
    "backup": {"pulling", "failed_pre_switch"},
    "pulling": {"migrating", "bootstrapping", "switching", "failed_pre_switch"},
    "migrating": {"bootstrapping", "manual_recovery_required"},
    "bootstrapping": {"switching", "manual_recovery_required"},
    "switching": {
        "verifying_health",
        "rolling_back",
        "failed_post_switch",
        "manual_recovery_required",
    },
    "verifying_health": {
        "succeeded",
        "rolling_back",
        "rolled_back",
        "failed_post_switch",
        "manual_recovery_required",
    },
    "rolling_back": {"rolled_back", "manual_recovery_required"},
    "adopting": {"succeeded", "manual_recovery_required"},
    "manual_recovery_required": {"reconciled"},
}
PRE_SWITCH_RECOVERY = {
    "idle",
    "preflight",
    "fetching",
    "verifying",
    "backup",
    "pulling",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _absolute(path: Path) -> Path:
    return Path(os.path.abspath(path))


def _is_directory_link(path: Path) -> bool:
    return path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction())


def _validate_private_directory(
    root: Path, directory: Path, *, create: bool = False
) -> None:
    root = _absolute(root)
    directory = _absolute(directory)
    try:
        relative = directory.relative_to(root)
    except ValueError as error:
        raise StateError("Private state directory escapes its fixed root") from error
    current = root
    for part in (Path(), *relative.parts):
        if part != Path():
            current /= part
        if _is_directory_link(current):
            raise StateError("Private state directory must not be a link")
        if not current.exists():
            if not create:
                return
            current.mkdir(mode=0o700)
        if _is_directory_link(current) or not current.is_dir():
            raise StateError("Private state directory is unavailable")


def _ensure_private_directory(root: Path, directory: Path) -> None:
    _validate_private_directory(root, directory, create=True)


def _atomic_text(path: Path, data: str, *, root: Path | None = None) -> None:
    path = _absolute(path)
    if root is None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    else:
        _ensure_private_directory(root, path.parent)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
        text=True,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            os.chmod(temporary, 0o600)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        for attempt in range(20):
            try:
                os.replace(temporary, path)
                break
            except PermissionError:
                if attempt == 19:
                    raise
                time.sleep(0.005)
        os.chmod(path, 0o600)
        if os.name != "nt":
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)


def _atomic_json(
    path: Path, payload: dict[str, object], *, root: Path | None = None
) -> None:
    _atomic_text(
        path,
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        root=root,
    )


def _read_private_text(root: Path, path: Path) -> str:
    root = _absolute(root)
    path = _absolute(path)
    for attempt in range(20):
        _validate_private_directory(root, path.parent)
        metadata = path.lstat()
        if (
            path.is_symlink()
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
        ):
            raise StateError("Private state file must be a single-link regular file")
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(path, flags)
        try:
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode) or opened.st_nlink > 1:
                raise StateError(
                    "Private state file must be a single-link regular file"
                )
            if opened.st_nlink == 0:
                if attempt == 19:
                    raise StateError(
                        "Private state file changed repeatedly during read"
                    )
                time.sleep(0.005)
                continue
            with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
                descriptor = -1
                return handle.read()
        finally:
            if descriptor >= 0:
                os.close(descriptor)
    raise StateError("Private state file is unavailable")


class OperationStore:
    def __init__(self, root: Path):
        self.root = _absolute(root)
        self.operations = self.root / "operations"

    def _path(self, operation_id: str) -> Path:
        if len(operation_id) != 32 or any(
            character not in "0123456789abcdef" for character in operation_id
        ):
            raise StateError("Invalid operation id")
        return self.operations / f"{operation_id}.json"

    def create(self, kind: str, metadata: dict[str, object]) -> dict[str, object]:
        operation_id = secrets.token_hex(16)
        timestamp = _now()
        payload = {
            "id": operation_id,
            "kind": kind,
            "status": "idle",
            "createdAt": timestamp,
            "updatedAt": timestamp,
            "metadata": metadata,
            "events": [
                {"status": "idle", "at": timestamp, "detail": "operation created"}
            ],
        }
        _atomic_json(self._path(operation_id), payload, root=self.root)
        return payload

    def get(self, operation_id: str) -> dict[str, object]:
        _validate_private_directory(self.root, self.operations)
        path = self._path(operation_id)
        error = None
        for attempt in range(20):
            try:
                payload = json.loads(_read_private_text(self.root, path))
                break
            except (
                FileNotFoundError,
                PermissionError,
                json.JSONDecodeError,
            ) as current_error:
                error = current_error
                if attempt == 19:
                    raise StateError(
                        f"Operation state is unavailable: {operation_id}"
                    ) from error
                time.sleep(0.005)
        if not isinstance(payload, dict) or payload.get("id") != operation_id:
            raise StateError(f"Operation state is invalid: {operation_id}")
        return payload

    def transition(
        self, operation_id: str, status: str, *, detail: str = ""
    ) -> dict[str, object]:
        payload = self.get(operation_id)
        current = payload["status"]
        if status not in TRANSITIONS.get(current, set()):
            raise StateError(f"Invalid operation transition: {current} -> {status}")
        timestamp = _now()
        payload["status"] = status
        payload["updatedAt"] = timestamp
        payload["events"].append(
            {"status": status, "at": timestamp, "detail": redact(detail)}
        )
        _atomic_json(self._path(operation_id), payload, root=self.root)
        return payload

    def bind_recovery_target(
        self,
        operation_id: str,
        manifest: dict[str, object],
    ) -> dict[str, object]:
        payload = self.get(operation_id)
        if payload.get("kind") not in {
            "apply_update",
            "initial_adoption",
        } or not isinstance(manifest, dict):
            raise StateError("Recovery target is invalid for this operation")
        target = json.loads(json.dumps(manifest))
        recovery = payload.setdefault(
            "recovery",
            {"targetManifest": target, "pendingContractTransitions": {}},
        )
        if (
            not isinstance(recovery, dict)
            or recovery.get("targetManifest") != target
            or not isinstance(recovery.get("pendingContractTransitions"), dict)
        ):
            raise StateError("Recovery target is already bound to different state")
        payload["updatedAt"] = _now()
        _atomic_json(self._path(operation_id), payload, root=self.root)
        return payload

    def mark_contract_transition_pending(
        self,
        operation_id: str,
        kind: str,
        *,
        before: str,
        after: str,
    ) -> dict[str, object]:
        if kind not in {"database", "configuration"}:
            raise StateError("Recovery contract kind is invalid")
        if not all(isinstance(value, str) and value for value in (before, after)):
            raise StateError("Recovery contract transition is invalid")
        payload = self.get(operation_id)
        recovery = payload.get("recovery")
        if not isinstance(recovery, dict) or not isinstance(
            recovery.get("targetManifest"),
            dict,
        ):
            raise StateError("Recovery target is not bound")
        pending = recovery.get("pendingContractTransitions")
        if not isinstance(pending, dict):
            raise StateError("Recovery contract state is invalid")
        transition = {"before": before, "after": after}
        if kind in pending and pending[kind] != transition:
            raise StateError("Recovery contract transition is already bound")
        pending[kind] = transition
        payload["updatedAt"] = _now()
        _atomic_json(self._path(operation_id), payload, root=self.root)
        return payload

    def resolve_contract_transition(
        self,
        operation_id: str,
        kind: str,
    ) -> dict[str, object]:
        if kind not in {"database", "configuration"}:
            raise StateError("Recovery contract kind is invalid")
        payload = self.get(operation_id)
        recovery = payload.get("recovery")
        pending = (
            recovery.get("pendingContractTransitions")
            if isinstance(recovery, dict)
            else None
        )
        if not isinstance(pending, dict) or kind not in pending:
            raise StateError("Recovery contract transition is not pending")
        del pending[kind]
        payload["updatedAt"] = _now()
        _atomic_json(self._path(operation_id), payload, root=self.root)
        return payload

    def list(self) -> list[dict[str, object]]:
        _validate_private_directory(self.root, self.operations)
        if not self.operations.exists():
            return []
        result = []
        for path in sorted(self.operations.glob("*.json")):
            result.append(self.get(path.stem))
        return result

    def recovery_block(self) -> dict[str, object] | None:
        blocked = [
            payload
            for payload in self.list()
            if payload["status"] == "manual_recovery_required"
        ]
        if not blocked:
            return None
        return max(blocked, key=lambda payload: (payload["updatedAt"], payload["id"]))

    def require_recovery_clear(self) -> None:
        blocked = self.recovery_block()
        if blocked is not None:
            raise RecoveryRequired(
                f"Manual recovery operation {blocked['id']} must be reconciled on the host"
            )
        from installer.operations import RestoreOperationJournal

        restore_block = RestoreOperationJournal(self.root).recovery_block()
        if restore_block is not None:
            raise RecoveryRequired(
                "A Restore operation requires manual recovery on the host"
            )

    def recover_incomplete(self) -> list[str]:
        recovered = []
        for payload in self.list():
            if payload.get("operationFormat") == "animemo.operation":
                if payload.get("kind") != "fresh_install":
                    raise StateError("Lifecycle operation kind is unsupported")
                from installer.operations import (
                    FreshInstallStatus,
                    fail_fresh_install,
                    parse_fresh_install_operation,
                )

                current = parse_fresh_install_operation(payload)
                if current.status is not FreshInstallStatus.RUNNING:
                    continue
                updated = fail_fresh_install(
                    current,
                    error_code="FRESH_INSTALL_INTERRUPTED",
                    at=_now(),
                    rollback_succeeded=None,
                )
                _atomic_json(
                    self._path(current.operation_id),
                    updated.as_dict(),
                    root=self.root,
                )
                recovered.append(current.operation_id)
                continue
            status = payload["status"]
            if status in TERMINAL_STATES:
                continue
            if status in PRE_SWITCH_RECOVERY:
                self.transition(
                    payload["id"],
                    "failed_pre_switch",
                    detail="agent restarted before application switch",
                )
            else:
                self.transition(
                    payload["id"],
                    "manual_recovery_required",
                    detail="agent restarted after migration or switch began",
                )
            recovered.append(payload["id"])
        from installer.operations import RestoreOperationJournal

        recovered.extend(RestoreOperationJournal(self.root).recover_incomplete())
        return recovered


class UpdateLock(AbstractContextManager):
    def __init__(self, path: Path, *, allow_reentrant: bool = False):
        absolute = _absolute(path)
        self.root = absolute.parent
        self.path = absolute
        self.handle = None
        self._registry_key = os.fspath(absolute)
        self._reentrant = False
        self._allow_reentrant = allow_reentrant

    def __enter__(self):
        thread_id = threading.get_ident()
        with _LOCK_REGISTRY_GUARD:
            owner = _LOCK_OWNERS.get(self._registry_key)
            if (
                self._allow_reentrant
                and owner is not None
                and owner[0] == thread_id
            ):
                _LOCK_OWNERS[self._registry_key] = (thread_id, owner[1] + 1)
                self._reentrant = True
                return self
        _ensure_private_directory(self.root, self.root)
        try:
            existing = self.path.lstat()
        except FileNotFoundError:
            existing = None
        if existing is not None and (
            self.path.is_symlink()
            or not stat.S_ISREG(existing.st_mode)
            or existing.st_nlink != 1
        ):
            raise StateError("Update lock file must be a private regular file")
        flags = os.O_RDWR | os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(self.path, flags, 0o600)
        except OSError as error:
            raise StateError("Update lock file is unavailable") from error
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
            os.close(descriptor)
            raise StateError("Update lock file must be a private regular file")
        self.handle = os.fdopen(descriptor, "r+b")
        os.chmod(self.path, 0o600)
        try:
            if os.name == "nt":
                import msvcrt

                if opened.st_size == 0:
                    self.handle.seek(0)
                    self.handle.write(b"0")
                    self.handle.flush()
                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError) as error:
            self.handle.close()
            self.handle = None
            raise OperationInProgress(
                "Another AniMemo update operation is active"
            ) from error
        with _LOCK_REGISTRY_GUARD:
            if self._registry_key in _LOCK_OWNERS:
                self.handle.close()
                self.handle = None
                raise OperationInProgress(
                    "Another AniMemo update operation is active"
                )
            _LOCK_OWNERS[self._registry_key] = (thread_id, 1)
        return self

    def __exit__(self, exc_type, exc, traceback):
        if self._reentrant:
            with _LOCK_REGISTRY_GUARD:
                owner = _LOCK_OWNERS.get(self._registry_key)
                if owner is not None and owner[0] == threading.get_ident():
                    if owner[1] <= 1:
                        _LOCK_OWNERS.pop(self._registry_key, None)
                    else:
                        _LOCK_OWNERS[self._registry_key] = (
                            owner[0],
                            owner[1] - 1,
                        )
            self._reentrant = False
            return False
        if self.handle is None:
            return False
        if os.name == "nt":
            import msvcrt

            self.handle.seek(0)
            msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        self.handle.close()
        self.handle = None
        with _LOCK_REGISTRY_GUARD:
            _LOCK_OWNERS.pop(self._registry_key, None)
        return False
