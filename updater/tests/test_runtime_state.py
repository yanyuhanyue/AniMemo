from __future__ import annotations

import multiprocessing
import os
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path

from updater.errors import StateError
from updater.runtime_state import RuntimeState


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


def update_runtime_in_process(root, start, results, rounds, changes) -> None:
    errors = []
    for _ in range(rounds):
        start.wait(timeout=10)
        try:
            RuntimeState(Path(root)).update(**changes)
        except BaseException as error:
            errors.append(repr(error))
        start.wait(timeout=10)
    results.put(errors)


class RuntimeStateTests(unittest.TestCase):
    def test_concurrent_updates_preserve_database_and_plugin_contracts(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = RuntimeState(Path(directory) / "state")
            initial = {
                "databaseContract": "animemo-db-v1",
                "configurationContract": "animemo-config-v1",
                "enabledPluginApis": [2],
            }

            for _ in range(32):
                runtime.write(initial)
                start = threading.Barrier(3)
                errors = []

                def update(**changes):
                    try:
                        start.wait(timeout=5)
                        RuntimeState(runtime.root).update(**changes)
                    except BaseException as error:
                        errors.append(error)

                database = threading.Thread(
                    target=update,
                    kwargs={"databaseContract": "animemo-db-v2"},
                )
                plugins = threading.Thread(
                    target=update,
                    kwargs={"enabledPluginApis": [3]},
                )
                database.start()
                plugins.start()
                start.wait(timeout=5)
                database.join(timeout=5)
                plugins.join(timeout=5)

                self.assertFalse(database.is_alive())
                self.assertFalse(plugins.is_alive())
                self.assertEqual(errors, [])
                self.assertEqual(
                    runtime.read(),
                    {
                        "databaseContract": "animemo-db-v2",
                        "configurationContract": "animemo-config-v1",
                        "enabledPluginApis": [3],
                    },
                )

    def test_concurrent_updates_are_atomic_across_processes(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = RuntimeState(Path(directory) / "state")
            initial = {
                "databaseContract": "animemo-db-v1",
                "configurationContract": "animemo-config-v1",
                "enabledPluginApis": [2],
            }
            rounds = 8
            context = multiprocessing.get_context("spawn")
            start = context.Barrier(3)
            results = context.Queue()
            processes = [
                context.Process(
                    target=update_runtime_in_process,
                    args=(runtime.root, start, results, rounds, changes),
                )
                for changes in (
                    {"databaseContract": "animemo-db-v2"},
                    {"enabledPluginApis": [3]},
                )
            ]
            for process in processes:
                process.start()

            try:
                for _ in range(rounds):
                    runtime.write(initial)
                    start.wait(timeout=10)
                    start.wait(timeout=10)
                    self.assertEqual(
                        runtime.read(),
                        {
                            "databaseContract": "animemo-db-v2",
                            "configurationContract": "animemo-config-v1",
                            "enabledPluginApis": [3],
                        },
                    )
            finally:
                for process in processes:
                    process.join(timeout=10)
                    if process.is_alive():
                        process.terminate()
                        process.join(timeout=5)

            self.assertEqual([process.exitcode for process in processes], [0, 0])
            self.assertEqual(results.get(timeout=5), [])
            self.assertEqual(results.get(timeout=5), [])

    def test_runtime_state_rejects_a_linked_state_root(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            outside = root / "outside"
            outside.mkdir()
            link_directory(root / "state", outside)
            runtime = RuntimeState(root / "state")

            with self.assertRaisesRegex(StateError, "directory"):
                runtime.write(
                    {
                        "databaseContract": "animemo-db-v1",
                        "configurationContract": "animemo-config-v1",
                        "enabledPluginApis": [2],
                    }
                )

            self.assertEqual(list(outside.iterdir()), [])

    def test_runtime_state_rejects_a_hard_linked_state_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state_root = root / "state"
            state_root.mkdir()
            outside = root / "outside.json"
            outside.write_text(
                '{"databaseContract":"animemo-db-v1",'
                '"configurationContract":"animemo-config-v1",'
                '"enabledPluginApis":[2]}',
                encoding="utf-8",
            )
            (state_root / "runtime.json").hardlink_to(outside)
            runtime = RuntimeState(state_root)

            with self.assertRaisesRegex(StateError, "file"):
                runtime.read()

    def test_runtime_state_rejects_a_hard_linked_lock_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state_root = root / "state"
            runtime = RuntimeState(state_root)
            runtime.write(
                {
                    "databaseContract": "animemo-db-v1",
                    "configurationContract": "animemo-config-v1",
                    "enabledPluginApis": [2],
                }
            )
            outside = root / "outside.lock"
            outside.write_bytes(b"0")
            runtime.lock_path.unlink()
            runtime.lock_path.hardlink_to(outside)

            with self.assertRaisesRegex(StateError, "lock file"):
                runtime.update(databaseContract="animemo-db-v2")

            self.assertEqual(runtime.read()["databaseContract"], "animemo-db-v1")


if __name__ == "__main__":
    unittest.main()
