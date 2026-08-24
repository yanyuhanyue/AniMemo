from __future__ import annotations

import threading
import unittest
from types import SimpleNamespace

from updater.background import BackgroundOperationManager
from updater.errors import StateError
from updater.runtime import HostAgentRuntime
from updater.protocol import OPERATION_FIELDS


class BackgroundOperationManagerTests(unittest.TestCase):
    def test_wait_observes_target_then_lock_release_then_registry_removal(self):
        manager = BackgroundOperationManager()
        target_started = threading.Event()
        target_release = threading.Event()
        cleanup_started = threading.Event()
        cleanup_release = threading.Event()

        def target():
            target_started.set()
            target_release.wait(2)

        def cleanup():
            cleanup_started.set()
            cleanup_release.wait(2)

        manager.start("operation-1", target, cleanup=cleanup, name="animemo-update-test")
        self.assertTrue(target_started.wait(1))
        self.assertFalse(manager.wait("operation-1", timeout=0.01))
        target_release.set()
        self.assertTrue(cleanup_started.wait(1))
        self.assertFalse(manager.wait("operation-1", timeout=0.01))
        self.assertEqual(manager.active_operation_ids(), ("operation-1",))
        cleanup_release.set()
        self.assertTrue(manager.wait("operation-1", timeout=1))
        self.assertEqual(manager.active_operation_ids(), ())
        self.assertTrue(manager.wait("operation-1", timeout=0))

    def test_close_is_bounded_idempotent_and_rejects_new_workers(self):
        manager = BackgroundOperationManager()
        release = threading.Event()
        rejected_cleanup = []
        manager.start("operation-1", lambda: release.wait(2), name="animemo-update-test")

        with self.assertRaisesRegex(StateError, "BACKGROUND_WORKERS_DID_NOT_STOP_BEFORE_TIMEOUT"):
            manager.close(timeout=0.01)
        with self.assertRaisesRegex(StateError, "BACKGROUND_OPERATION_MANAGER_CLOSED"):
            manager.start(
                "operation-2",
                lambda: None,
                cleanup=lambda: rejected_cleanup.append("released"),
            )
        self.assertEqual(rejected_cleanup, ["released"])

        release.set()
        manager.close(timeout=1)
        manager.close(timeout=0)

    def test_unknown_and_duplicate_operation_ids_fail_closed(self):
        manager = BackgroundOperationManager()
        release = threading.Event()
        manager.start("operation-1", lambda: release.wait(2))
        with self.assertRaisesRegex(StateError, "BACKGROUND_OPERATION_ALREADY_REGISTERED"):
            manager.start("operation-1", lambda: None)
        with self.assertRaisesRegex(StateError, "BACKGROUND_OPERATION_UNKNOWN"):
            manager.wait("missing", timeout=0)
        release.set()
        self.assertTrue(manager.wait("operation-1", timeout=1))

    def test_thread_start_failure_unregisters_and_runs_cleanup_once(self):
        manager = BackgroundOperationManager(thread_factory=lambda **kwargs: BrokenThread())
        cleanup_calls = []

        with self.assertRaisesRegex(RuntimeError, "start failed"):
            manager.start(
                "operation-1",
                lambda: None,
                cleanup=lambda: cleanup_calls.append("released"),
            )

        self.assertEqual(cleanup_calls, ["released"])
        self.assertEqual(manager.active_operation_ids(), ())
        with self.assertRaisesRegex(StateError, "BACKGROUND_OPERATION_UNKNOWN"):
            manager.wait("operation-1", timeout=0)

    def test_worker_threads_are_explicitly_non_daemon(self):
        observed = []

        def factory(**kwargs):
            observed.append(kwargs)
            return threading.Thread(**kwargs)

        manager = BackgroundOperationManager(thread_factory=factory)
        manager.start("operation-1", lambda: None)
        self.assertTrue(manager.wait("operation-1", timeout=1))

        self.assertFalse(observed[0]["daemon"])

    def test_unexpected_worker_failure_has_no_traceback_and_fails_closed(self):
        manager = BackgroundOperationManager()
        manager.start(
            "operation-1",
            lambda: (_ for _ in ()).throw(RuntimeError("unexpected")),
        )

        with self.assertRaisesRegex(StateError, "BACKGROUND_OPERATION_WORKER_FAILED"):
            manager.wait("operation-1", timeout=1)
        with self.assertRaisesRegex(StateError, "BACKGROUND_OPERATION_WORKER_FAILED"):
            manager.close(timeout=0)

    def test_close_cannot_pass_mutation_admission_before_worker_registration(self):
        manager = BackgroundOperationManager()
        worker_release = threading.Event()
        close_returned = threading.Event()

        with manager.mutation_start():
            closer = threading.Thread(
                target=lambda: (manager.close(timeout=2), close_returned.set())
            )
            closer.start()
            self.assertFalse(close_returned.wait(0.05))
            manager.start("operation-1", lambda: worker_release.wait(2))

        self.assertFalse(close_returned.wait(0.05))
        worker_release.set()
        closer.join(2)

        self.assertFalse(closer.is_alive())
        self.assertTrue(close_returned.is_set())

    def test_background_lifecycle_does_not_expand_rpc_protocol(self):
        self.assertNotIn("wait_for_background_operation", OPERATION_FIELDS)
        self.assertNotIn("shutdown", OPERATION_FIELDS)


class BrokenThread:
    def start(self):
        raise RuntimeError("start failed")


class HostAgentRuntimeLifecycleTests(unittest.TestCase):
    def test_serve_forever_closes_agent_after_server_returns(self):
        calls = []
        runtime = object.__new__(HostAgentRuntime)
        runtime.server = SimpleNamespace(serve_forever=lambda: calls.append("serve"))
        runtime.agent = SimpleNamespace(close=lambda **kwargs: calls.append("close"))

        runtime.serve_forever()

        self.assertEqual(calls, ["serve", "close"])

    def test_serve_forever_closes_agent_after_server_failure(self):
        calls = []
        runtime = object.__new__(HostAgentRuntime)

        def fail():
            calls.append("serve")
            raise RuntimeError("server failed")

        runtime.server = SimpleNamespace(serve_forever=fail)
        runtime.agent = SimpleNamespace(close=lambda **kwargs: calls.append("close"))

        with self.assertRaisesRegex(RuntimeError, "server failed"):
            runtime.serve_forever()

        self.assertEqual(calls, ["serve", "close"])


if __name__ == "__main__":
    unittest.main()
