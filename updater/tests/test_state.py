from __future__ import annotations

import json
import multiprocessing
import os
import stat
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

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
    def test_recovery_target_and_pending_contract_transition_are_immutable(self):
        with tempfile.TemporaryDirectory() as directory:
            store = OperationStore(Path(directory) / "state")
            operation = store.create("apply_update", {"version": "v1.1.0"})
            target = {
                "release": {"version": "v1.1.0", "commit": "2" * 40},
                "compatibility": {
                    "database": {"contract": "animemo-db-v2"},
                    "configuration": {"contract": "animemo-config-v2"},
                },
            }

            store.bind_recovery_target(operation["id"], target)
            store.bind_recovery_target(operation["id"], target)
            store.mark_contract_transition_pending(
                operation["id"],
                "database",
                before="animemo-db-v1",
                after="animemo-db-v2",
            )

            pending = store.get(operation["id"])["recovery"]["pendingContractTransitions"]
            self.assertEqual(
                pending,
                {"database": {"before": "animemo-db-v1", "after": "animemo-db-v2"}},
            )
            with self.assertRaisesRegex(StateError, "different"):
                store.bind_recovery_target(
                    operation["id"],
                    {**target, "release": {"version": "v1.1.0", "commit": "3" * 40}},
                )
            with self.assertRaisesRegex(StateError, "already bound"):
                store.mark_contract_transition_pending(
                    operation["id"],
                    "database",
                    before="animemo-db-v1",
                    after="animemo-db-v3",
                )

            store.resolve_contract_transition(operation["id"], "database")
            self.assertEqual(
                store.get(operation["id"])["recovery"]["pendingContractTransitions"],
                {},
            )

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

    def test_operation_store_reads_inode_unlinked_by_concurrent_atomic_replace(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = OperationStore(root / "state")
            operation = store.create("apply_update", {"version": "v1.0.1"})
            real_fstat = os.fstat
            observed_unlinked_inode = False

            def fstat_after_atomic_replace(descriptor):
                nonlocal observed_unlinked_inode
                metadata = real_fstat(descriptor)
                if observed_unlinked_inode:
                    return metadata
                observed_unlinked_inode = True
                values = list(metadata)
                values[3] = 0  # st_nlink: the opened old inode was unlinked by os.replace
                return os.stat_result(values)

            with mock.patch("updater.state.os.fstat", side_effect=fstat_after_atomic_replace):
                restored = store.get(operation["id"])

            self.assertTrue(observed_unlinked_inode)
            self.assertEqual(restored["id"], operation["id"])
            self.assertEqual(restored["status"], "idle")

    def test_operation_store_retries_preopen_inode_unlinked_by_atomic_replace(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = OperationStore(root / "state")
            operation = store.create("apply_update", {"version": "v1.0.1"})
            real_lstat = Path.lstat
            observed_unlinked_inode = False
            lstat_calls = 0

            def lstat_during_atomic_replace(path):
                nonlocal observed_unlinked_inode, lstat_calls
                metadata = real_lstat(path)
                if path.name != f"{operation['id']}.json":
                    return metadata
                lstat_calls += 1
                if not observed_unlinked_inode:
                    observed_unlinked_inode = True
                    values = list(metadata)
                    values[3] = 0
                    return os.stat_result(values)
                return metadata

            with mock.patch.object(
                Path,
                "lstat",
                autospec=True,
                side_effect=lstat_during_atomic_replace,
            ):
                restored = store.get(operation["id"])

            self.assertTrue(observed_unlinked_inode)
            self.assertGreaterEqual(lstat_calls, 2)
            self.assertEqual(restored["id"], operation["id"])

    def test_operation_store_retries_lstat_open_inode_substitution(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = OperationStore(root / "state")
            operation = store.create("apply_update", {"version": "v1.0.1"})
            real_fstat = os.fstat
            substituted = False
            fstat_calls = 0

            def fstat_from_different_inode(descriptor):
                nonlocal substituted, fstat_calls
                metadata = real_fstat(descriptor)
                fstat_calls += 1
                if substituted:
                    return metadata
                substituted = True
                values = list(metadata)
                values[1] += 1
                return os.stat_result(values)

            with mock.patch("updater.state.os.fstat", side_effect=fstat_from_different_inode):
                restored = store.get(operation["id"])

            self.assertTrue(substituted)
            self.assertGreaterEqual(fstat_calls, 2)
            self.assertEqual(restored["id"], operation["id"])

    def test_operation_store_fails_stably_after_repeated_namespace_replacement(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = OperationStore(root / "state")
            operation = store.create("apply_update", {"version": "v1.0.1"})
            real_lstat = Path.lstat

            def always_unlinked(path):
                metadata = real_lstat(path)
                if path.name != f"{operation['id']}.json":
                    return metadata
                values = list(metadata)
                values[3] = 0
                return os.stat_result(values)

            with mock.patch.object(
                Path,
                "lstat",
                autospec=True,
                side_effect=always_unlinked,
            ):
                with self.assertRaisesRegex(
                    StateError,
                    "PRIVATE_STATE_CHANGED_REPEATEDLY_DURING_READ",
                ):
                    store.get(operation["id"])

    def test_operation_store_retries_transient_missing_and_permission_windows(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = OperationStore(root / "state")
            operation = store.create("apply_update", {"version": "v1.0.1"})
            real_lstat = Path.lstat
            failures = [FileNotFoundError("replace window"), PermissionError("replace window")]

            def transient_errors(path):
                if path.name == f"{operation['id']}.json" and failures:
                    raise failures.pop(0)
                return real_lstat(path)

            with mock.patch.object(
                Path,
                "lstat",
                autospec=True,
                side_effect=transient_errors,
            ):
                restored = store.get(operation["id"])

            self.assertEqual(failures, [])
            self.assertEqual(restored["id"], operation["id"])

    def test_operation_store_rejects_a_symlinked_journal_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state_root = root / "state"
            operations = state_root / "operations"
            operations.mkdir(parents=True)
            operation_id = "a" * 32
            outside = root / "outside.json"
            outside.write_text("{}\n", encoding="utf-8")
            try:
                (operations / f"{operation_id}.json").symlink_to(outside)
            except OSError as error:
                self.skipTest(f"File symlinks are unavailable: {error}")

            with self.assertRaisesRegex(StateError, "single-link regular file"):
                OperationStore(state_root).get(operation_id)

    def test_operation_store_rejects_a_directory_in_place_of_a_journal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            operation_id = "a" * 32
            journal = root / "state" / "operations" / f"{operation_id}.json"
            journal.mkdir(parents=True)

            with self.assertRaisesRegex(StateError, "single-link regular file"):
                OperationStore(root / "state").get(operation_id)

    @unittest.skipIf(os.name == "nt", "POSIX FIFO semantics are unavailable")
    def test_operation_store_rejects_a_fifo_journal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            operation_id = "a" * 32
            journal = root / "state" / "operations" / f"{operation_id}.json"
            journal.parent.mkdir(parents=True)
            os.mkfifo(journal)

            with self.assertRaisesRegex(StateError, "single-link regular file"):
                OperationStore(root / "state").get(operation_id)

    def test_operation_store_does_not_mask_corrupt_json(self):
        with tempfile.TemporaryDirectory() as directory:
            store = OperationStore(Path(directory) / "state")
            operation = store.create("apply_update", {"version": "v1.0.1"})
            path = store.operations / f"{operation['id']}.json"
            path.write_text('{"partial":', encoding="utf-8")

            with self.assertRaisesRegex(StateError, "Operation state is unavailable"):
                store.get(operation["id"])

    def test_operation_store_concurrent_readers_never_observe_mixed_generation(self):
        with tempfile.TemporaryDirectory() as directory:
            store = OperationStore(Path(directory) / "state")
            operation = store.create("apply_update", {"version": "v1.0.1"})
            barrier = threading.Barrier(5)
            stopped = threading.Event()
            errors = []
            observed = []

            def read_until_complete():
                try:
                    barrier.wait(timeout=5)
                    while not stopped.is_set():
                        payload = store.get(operation["id"])
                        observed.append(payload["status"])
                except BaseException as error:
                    errors.append(error)

            readers = [threading.Thread(target=read_until_complete) for _ in range(4)]
            for reader in readers:
                reader.start()
            barrier.wait(timeout=5)
            for status in (
                "preflight",
                "fetching",
                "verifying",
                "pulling",
                "migrating",
                "bootstrapping",
                "switching",
                "verifying_health",
                "succeeded",
            ):
                store.transition(operation["id"], status)
            stopped.set()
            for reader in readers:
                reader.join(timeout=5)

            self.assertTrue(all(not reader.is_alive() for reader in readers))
            self.assertEqual(errors, [])
            self.assertTrue(observed)
            self.assertEqual(store.get(operation["id"])["status"], "succeeded")

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

    def test_manual_recovery_is_a_durable_global_block_until_reconciled(self):
        with tempfile.TemporaryDirectory() as directory:
            store = OperationStore(Path(directory))
            operation = store.create("apply_update", {"version": "v1.0.1"})
            for status in [
                "preflight", "fetching", "verifying", "pulling", "migrating",
                "manual_recovery_required",
            ]:
                store.transition(operation["id"], status)

            self.assertEqual(store.recovery_block()["id"], operation["id"])
            store.transition(operation["id"], "reconciled", detail="live state verified")

            self.assertIsNone(store.recovery_block())
            self.assertEqual(store.get(operation["id"])["status"], "reconciled")

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

    def test_global_lock_is_reentrant_only_for_the_owning_thread(self):
        with tempfile.TemporaryDirectory() as directory:
            lock_path = Path(directory) / "update.lock"
            with UpdateLock(lock_path), UpdateLock(
                lock_path, allow_reentrant=True
            ):
                self.assertTrue(lock_path.is_file())
            with UpdateLock(lock_path):
                pass

    def test_global_lock_creates_a_fully_missing_private_state_root(self):
        with tempfile.TemporaryDirectory() as directory:
            existing_parent = Path(directory) / "var" / "lib"
            existing_parent.mkdir(parents=True)
            state_root = (
                existing_parent / "animemo-updater" / "instances" / "default"
            )
            lock_path = state_root / "update.lock"

            with UpdateLock(lock_path):
                self.assertTrue(lock_path.is_file())

            self.assertTrue(state_root.is_dir())
            if os.name != "nt":
                for path in (
                    existing_parent / "animemo-updater",
                    existing_parent / "animemo-updater" / "instances",
                    state_root,
                ):
                    self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700)

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
