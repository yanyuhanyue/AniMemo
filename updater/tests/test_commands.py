from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from updater.commands import CommandRunner
from updater.errors import StateError


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


class CommandRunnerTests(unittest.TestCase):
    def test_write_gzip_does_not_follow_a_precreated_temporary_hard_link(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            destination = root / "backups" / "database.sql.gz"
            destination.parent.mkdir()
            outside = root / "outside.txt"
            outside.write_bytes(b"DO_NOT_CHANGE\n")
            predictable = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
            predictable.hardlink_to(outside)

            CommandRunner().write_gzip(
                [sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'SELECT 1;\\n')"],
                destination,
            )

            self.assertEqual(outside.read_bytes(), b"DO_NOT_CHANGE\n")

    def test_write_gzip_rejects_a_destination_directory_link(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data_root = root / "data"
            outside = root / "outside"
            data_root.mkdir()
            outside.mkdir()
            link_directory(data_root / "backups", outside)

            with self.assertRaisesRegex(StateError, "directory"):
                CommandRunner().write_gzip(
                    [sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'SELECT 1;\\n')"],
                    data_root / "backups" / "database.sql.gz",
                    root=data_root,
                )

            self.assertEqual(list(outside.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
