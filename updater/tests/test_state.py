from __future__ import annotations

import json
import multiprocessing
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from updater.errors import OperationInProgress, StateError
from updater.state import OperationStore, UpdateLock


def hold_lock(path: str, ready, release):
    with UpdateLock(Path(path)):
        ready.set()
        release.wait(10)


def link_directory(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        if os.name != "nt":
            raise
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            check=True,
            capture_output=True,
            text=True,
        )


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

    def test_operation_journal_redacts_secrets_at_the_persistence_boundary(self):
        with tempfile.TemporaryDirectory() as directory:
            store = OperationStore(Path(directory))
            operation = store.create("apply_update", {"version": "v1.0.1"})

            stored = store.transition(
                operation["id"],
                "preflight",
                detail="Authorization: Bearer abc.def DB_PASSWORD=hunter2",
            )

            detail = stored["events"][-1]["detail"]
            self.assertNotIn("abc.def", detail)
            self.assertNotIn("hunter2", detail)
            self.assertIn("[REDACTED]", detail)

    def test_operation_store_rejects_an_operations_directory_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state_root = root / "state"
            outside = root / "outside"
            state_root.mkdir()
            outside.mkdir()
            link_directory(state_root / "operations", outside)
            store = OperationStore(state_root)

            with self.assertRaisesRegex(StateError, "directory"):
                store.create("apply_update", {"version": "v1.0.1"})

            self.assertEqual(list(outside.iterdir()), [])

    def test_operation_store_rejects_reading_from_an_operations_directory_link(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state_root = root / "state"
            outside = root / "outside"
            state_root.mkdir()
            outside.mkdir()
            operation_id = "a" * 32
            (outside / f"{operation_id}.json").write_text(
                json.dumps(
                    {
                        "id": operation_id,
                        "kind": "apply_update",
                        "status": "succeeded",
                        "createdAt": "2026-08-12T00:00:00Z",
                        "updatedAt": "2026-08-12T00:00:00Z",
                        "metadata": {},
                        "events": [],
                    }
                ),
                encoding="utf-8",
            )
            link_directory(state_root / "operations", outside)
            store = OperationStore(state_root)

            with self.assertRaisesRegex(StateError, "directory"):
                store.get(operation_id)

    def test_operation_store_rejects_a_hard_linked_journal_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state_root = root / "state"
            operations = state_root / "operations"
            operations.mkdir(parents=True)
            operation_id = "a" * 32
            outside = root / "outside.json"
            outside.write_text(
                json.dumps(
                    {
                        "id": operation_id,
                        "kind": "apply_update",
                        "status": "succeeded",
                        "createdAt": "2026-08-12T00:00:00Z",
                        "updatedAt": "2026-08-12T00:00:00Z",
                        "metadata": {},
                        "events": [],
                    }
                ),
                encoding="utf-8",
            )
            (operations / f"{operation_id}.json").hardlink_to(outside)
            store = OperationStore(state_root)

            with self.assertRaisesRegex(StateError, "file"):
                store.get(operation_id)

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
                with self.assertRaises(OperationInProgress), UpdateLock(Path(lock_path)):
                    pass
            finally:
                release.set()
                process.join(10)
            self.assertEqual(process.exitcode, 0)
            with UpdateLock(Path(lock_path)):
                pass

    def test_global_lock_rejects_a_linked_state_root(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            outside = root / "outside"
            outside.mkdir()
            link_directory(root / "state", outside)

            with self.assertRaisesRegex(StateError, "directory"), UpdateLock(root / "state" / "update.lock"):
                pass

            self.assertEqual(list(outside.iterdir()), [])

    def test_global_lock_rejects_a_hard_linked_lock_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state_root = root / "state"
            state_root.mkdir()
            outside = root / "outside.lock"
            outside.write_bytes(b"DO_NOT_CHANGE\n")
            (state_root / "update.lock").hardlink_to(outside)

            with self.assertRaisesRegex(StateError, "file"), UpdateLock(state_root / "update.lock"):
                pass

            self.assertEqual(outside.read_bytes(), b"DO_NOT_CHANGE\n")


if __name__ == "__main__":
    unittest.main()
