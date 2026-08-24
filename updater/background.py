from __future__ import annotations

import threading
import time
from collections.abc import Callable

from .errors import StateError


class BackgroundOperationManager:
    """Own mutation workers until their target and lock cleanup are complete."""

    def __init__(self, *, thread_factory=threading.Thread) -> None:
        self._condition = threading.Condition(threading.RLock())
        self._thread_factory = thread_factory
        self._active: dict[str, object] = {}
        self._completed: dict[str, bool] = {}
        self._closing = False

    def require_open(self) -> None:
        with self._condition:
            if self._closing:
                raise StateError("BACKGROUND_OPERATION_MANAGER_CLOSED")

    def start(
        self,
        operation_id: str,
        target: Callable[..., object],
        *args: object,
        cleanup: Callable[[], object] | None = None,
        name: str | None = None,
        **kwargs: object,
    ) -> None:
        def cleanup_failed_start() -> None:
            if cleanup is not None:
                try:
                    cleanup()
                except BaseException:
                    pass

        if not isinstance(operation_id, str) or not operation_id:
            cleanup_failed_start()
            raise StateError("BACKGROUND_OPERATION_ID_INVALID")
        if not callable(target) or (cleanup is not None and not callable(cleanup)):
            cleanup_failed_start()
            raise StateError("BACKGROUND_OPERATION_TARGET_INVALID")

        def run_owned_worker() -> None:
            failed = False
            try:
                target(*args, **kwargs)
            except BaseException:  # worker failures are observed by wait, never leaked
                failed = True
            finally:
                if cleanup is not None:
                    try:
                        cleanup()
                    except BaseException:
                        failed = True
                with self._condition:
                    self._active.pop(operation_id, None)
                    self._completed[operation_id] = failed
                    self._condition.notify_all()

        try:
            worker = self._thread_factory(
                target=run_owned_worker,
                name=name or f"animemo-background-{operation_id[:8]}",
                daemon=False,
            )
        except BaseException:
            cleanup_failed_start()
            raise
        with self._condition:
            if self._closing:
                rejection = StateError("BACKGROUND_OPERATION_MANAGER_CLOSED")
            elif operation_id in self._active or operation_id in self._completed:
                rejection = StateError("BACKGROUND_OPERATION_ALREADY_REGISTERED")
            else:
                rejection = None
                self._active[operation_id] = worker
        if rejection is not None:
            cleanup_failed_start()
            raise rejection
        try:
            worker.start()
        except BaseException:
            with self._condition:
                self._active.pop(operation_id, None)
                self._condition.notify_all()
            cleanup_failed_start()
            raise

    def wait(self, operation_id: str, timeout: float | None) -> bool:
        deadline = None if timeout is None else time.monotonic() + max(0.0, timeout)
        with self._condition:
            if operation_id not in self._active and operation_id not in self._completed:
                raise StateError("BACKGROUND_OPERATION_UNKNOWN")
            while operation_id in self._active:
                remaining = (
                    None if deadline is None else max(0.0, deadline - time.monotonic())
                )
                if remaining == 0.0:
                    return False
                self._condition.wait(remaining)
            failed = self._completed[operation_id]
        if failed:
            raise StateError("BACKGROUND_OPERATION_WORKER_FAILED")
        return True

    def active_operation_ids(self) -> tuple[str, ...]:
        with self._condition:
            return tuple(sorted(self._active))

    def close(self, timeout: float | None) -> None:
        deadline = None if timeout is None else time.monotonic() + max(0.0, timeout)
        with self._condition:
            self._closing = True
            while self._active:
                remaining = (
                    None if deadline is None else max(0.0, deadline - time.monotonic())
                )
                if remaining == 0.0:
                    raise StateError(
                        "BACKGROUND_WORKERS_DID_NOT_STOP_BEFORE_TIMEOUT"
                    )
                self._condition.wait(remaining)
            if any(self._completed.values()):
                raise StateError("BACKGROUND_OPERATION_WORKER_FAILED")
