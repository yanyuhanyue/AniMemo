from __future__ import annotations

import json
import multiprocessing
import os
import tempfile
import unittest
from pathlib import Path

from updater.errors import OperationInProgress, StateError
from updater.state import OperationStore, UpdateLock


def hold_lock(path: str, ready, release):
    with UpdateLock(Path(path)):
        ready.set()
        release.wait(10)


class OperationStateTests(unittest.TestCase):
    def test_operation_transitions_are_persisted_atomically(self):
        with tempfile.TemporaryDirectory() as directory:
            store = OperationStore(Path(directory))
            operation = store.create("apply_update", {"version": "v1.0.1"})
            store.transition(operation["id"], "preflight", detail="checking host")
            store.transition(operation["id"], "fetching", detail="fetching release")

            restored = store.get(operation["id"])

            self.assertEqual(restored["status"], "fetching")
            self.assertEqual([event["status"] for event in restored["events"]], ["idle", "preflight", "fetching"])
            if os.name != "nt":
                self.assertEqual((Path(directory) / "operations" / f"{operation['id']}.json").stat().st_mode & 0o777, 0o600)
            json.loads((Path(directory) / "operations" / f"{operation['id']}.json").read_text(encoding="utf-8"))

    def test_invalid_transition_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            store = OperationStore(Path(directory))
            operation = store.create("apply_update", {})

            with self.assertRaises(StateError):
                store.transition(operation["id"], "switching")

    def test_crash_recovery_never_repeats_migration(self):
        with tempfile.TemporaryDirectory() as directory:
            store = OperationStore(Path(directory))
            safe = store.create("apply_update", {})
            store.transition(safe["id"], "preflight")
            risky = store.create("apply_update", {})
            for status in ["preflight", "fetching", "verifying", "backup", "pulling", "migrating"]:
                store.transition(risky["id"], status)

            recovered = store.recover_incomplete()

            self.assertEqual(store.get(safe["id"])["status"], "failed_pre_switch")
            self.assertEqual(store.get(risky["id"])["status"], "manual_recovery_required")
            self.assertEqual(set(recovered), {safe["id"], risky["id"]})

    def test_global_lock_is_cross_process_and_crash_aware(self):
        with tempfile.TemporaryDirectory() as directory:
            lock_path = str(Path(directory) / "update.lock")
            context = multiprocessing.get_context("spawn")
            ready = context.Event()
            release = context.Event()
            process = context.Process(target=hold_lock, args=(lock_path, ready, release))
            process.start()
            self.assertTrue(ready.wait(10))
            try:
                with self.assertRaises(OperationInProgress):
                    with UpdateLock(Path(lock_path)):
                        pass
            finally:
                release.set()
                process.join(10)
            self.assertEqual(process.exitcode, 0)
            with UpdateLock(Path(lock_path)):
                pass


if __name__ == "__main__":
    unittest.main()
