from __future__ import annotations

import json
import os
import secrets
import time
from contextlib import AbstractContextManager
from datetime import datetime, timezone
from pathlib import Path

from .errors import OperationInProgress, StateError


TERMINAL_STATES = {
    "succeeded",
    "failed_pre_switch",
    "failed_post_switch",
    "rolled_back",
    "manual_recovery_required",
}
TRANSITIONS = {
    "idle": {"preflight", "failed_pre_switch"},
    "preflight": {"fetching", "failed_pre_switch"},
    "fetching": {"verifying", "failed_pre_switch"},
    "verifying": {"backup", "pulling", "failed_pre_switch"},
    "backup": {"pulling", "failed_pre_switch"},
    "pulling": {"migrating", "switching", "failed_pre_switch"},
    "migrating": {"switching", "manual_recovery_required"},
    "switching": {"verifying_health", "rolling_back", "failed_post_switch", "manual_recovery_required"},
    "verifying_health": {"succeeded", "rolling_back", "rolled_back", "failed_post_switch", "manual_recovery_required"},
    "rolling_back": {"rolled_back", "manual_recovery_required"},
}
PRE_SWITCH_RECOVERY = {"idle", "preflight", "fetching", "verifying", "backup", "pulling"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _atomic_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    data = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
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


class OperationStore:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.operations = self.root / "operations"

    def _path(self, operation_id: str) -> Path:
        if len(operation_id) != 32 or any(character not in "0123456789abcdef" for character in operation_id):
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
            "events": [{"status": "idle", "at": timestamp, "detail": "operation created"}],
        }
        _atomic_json(self._path(operation_id), payload)
        return payload

    def get(self, operation_id: str) -> dict[str, object]:
        path = self._path(operation_id)
        error = None
        for attempt in range(20):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                break
            except (FileNotFoundError, PermissionError, json.JSONDecodeError) as current_error:
                error = current_error
                if attempt == 19:
                    raise StateError(f"Operation state is unavailable: {operation_id}") from error
                time.sleep(0.005)
        if not isinstance(payload, dict) or payload.get("id") != operation_id:
            raise StateError(f"Operation state is invalid: {operation_id}")
        return payload

    def transition(self, operation_id: str, status: str, *, detail: str = "") -> dict[str, object]:
        payload = self.get(operation_id)
        current = payload["status"]
        if status not in TRANSITIONS.get(current, set()):
            raise StateError(f"Invalid operation transition: {current} -> {status}")
        timestamp = _now()
        payload["status"] = status
        payload["updatedAt"] = timestamp
        payload["events"].append({"status": status, "at": timestamp, "detail": detail})
        _atomic_json(self._path(operation_id), payload)
        return payload

    def list(self) -> list[dict[str, object]]:
        if not self.operations.exists():
            return []
        result = []
        for path in sorted(self.operations.glob("*.json")):
            result.append(self.get(path.stem))
        return result

    def recover_incomplete(self) -> list[str]:
        recovered = []
        for payload in self.list():
            status = payload["status"]
            if status in TERMINAL_STATES:
                continue
            if status in PRE_SWITCH_RECOVERY:
                self.transition(payload["id"], "failed_pre_switch", detail="agent restarted before application switch")
            else:
                self.transition(payload["id"], "manual_recovery_required", detail="agent restarted after migration or switch began")
            recovered.append(payload["id"])
        return recovered


class UpdateLock(AbstractContextManager):
    def __init__(self, path: Path):
        self.path = path.resolve()
        self.handle = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.handle = self.path.open("a+b")
        os.chmod(self.path, 0o600)
        try:
            if os.name == "nt":
                import msvcrt

                self.handle.seek(0)
                if self.handle.read(1) == b"":
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
            raise OperationInProgress("Another AniMemo update operation is active") from error
        return self

    def __exit__(self, exc_type, exc, traceback):
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
        return False
