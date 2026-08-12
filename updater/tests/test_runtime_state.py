from __future__ import annotations

import os
import subprocess
import tempfile
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


class RuntimeStateTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
