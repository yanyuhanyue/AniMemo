from __future__ import annotations

import io
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from updater.commands import CommandRunner
from updater.errors import (
    CommandExited,
    CommandFailed,
    CommandStartFailed,
    CommandTimedOut,
    StateError,
)


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
    def test_successful_command_uses_a_fixed_argv_without_a_shell(self):
        completed = subprocess.CompletedProcess(["agent"], 0, "ok", "")
        with patch("updater.commands.subprocess.run", return_value=completed) as run:
            result = CommandRunner().run(["agent", "status"], timeout=17)

        self.assertIs(result, completed)
        self.assertEqual(run.call_args.args[0], ["agent", "status"])
        self.assertFalse(run.call_args.kwargs["shell"])
        self.assertEqual(run.call_args.kwargs["timeout"], 17)

    def test_nonzero_exit_is_structured_and_diagnostics_are_redacted(self):
        failure = subprocess.CalledProcessError(
            23,
            ["agent", "status"],
            output="Authorization: Bearer stdout-secret\n",
            stderr="\x1b[31mhttps://user:proxy-secret@example.test\x00",
        )
        with (
            patch("updater.commands.subprocess.run", side_effect=failure),
            self.assertRaises(CommandExited) as raised,
        ):
            CommandRunner().run(["agent", "status"])

        error = raised.exception
        self.assertIsInstance(error, CommandFailed)
        self.assertEqual(error.code, "agent_command_exit_failed")
        self.assertEqual(error.executable, "agent")
        self.assertEqual(error.return_code, 23)
        self.assertNotIn("stdout-secret", str(error))
        self.assertNotIn("proxy-secret", str(error))
        self.assertNotIn("\x1b", str(error))
        self.assertNotIn("\x00", str(error))

    def test_timeout_is_structured_before_the_generic_subprocess_error(self):
        failure = subprocess.TimeoutExpired(
            ["agent", "status"],
            77,
            output=b"GH_TOKEN=timeout-secret\n",
            stderr=b"Basic stderr-secret\n",
        )
        with (
            patch("updater.commands.subprocess.run", side_effect=failure),
            self.assertRaises(CommandTimedOut) as raised,
        ):
            CommandRunner().run(["agent", "status"], timeout=77)

        error = raised.exception
        self.assertIsInstance(error, CommandFailed)
        self.assertEqual(error.code, "agent_command_timeout")
        self.assertEqual(error.timeout_seconds, 77)
        self.assertNotIn("timeout-secret", str(error))
        self.assertNotIn("stderr-secret", str(error))
        self.assertNotIn("status", str(error), "full argv must not enter diagnostics")

    def test_start_failure_has_a_stable_non_path_error_class(self):
        failure = FileNotFoundError(2, "missing", "/private/operator/path/agent")
        with (
            patch("updater.commands.subprocess.run", side_effect=failure),
            self.assertRaises(CommandStartFailed) as raised,
        ):
            CommandRunner().run(["agent", "status"])

        error = raised.exception
        self.assertIsInstance(error, CommandFailed)
        self.assertEqual(error.code, "agent_command_start_failed")
        self.assertEqual(error.failure_class, "command_not_found")
        self.assertNotIn("/private/operator/path", str(error))

    def test_diagnostics_are_bounded_to_4096_characters_per_stream(self):
        failure = subprocess.CalledProcessError(
            1,
            ["agent"],
            output="x" * 10_000,
            stderr="y" * 10_000,
        )
        with (
            patch("updater.commands.subprocess.run", side_effect=failure),
            self.assertRaises(CommandExited) as raised,
        ):
            CommandRunner().run(["agent"])

        self.assertLessEqual(len(raised.exception.stdout), 4096)
        self.assertLessEqual(len(raised.exception.stderr), 4096)

    def test_write_gzip_does_not_follow_a_precreated_temporary_hard_link(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            destination = root / "backups" / "database.sql.gz"
            destination.parent.mkdir()
            outside = root / "outside.txt"
            outside.write_bytes(b"DO_NOT_CHANGE\n")
            predictable = destination.with_name(
                f".{destination.name}.{os.getpid()}.tmp"
            )
            predictable.hardlink_to(outside)

            CommandRunner().write_gzip(
                [
                    sys.executable,
                    "-c",
                    "import sys; sys.stdout.buffer.write(b'SELECT 1;\\n')",
                ],
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
                    [
                        sys.executable,
                        "-c",
                        "import sys; sys.stdout.buffer.write(b'SELECT 1;\\n')",
                    ],
                    data_root / "backups" / "database.sql.gz",
                    root=data_root,
                )

            self.assertEqual(list(outside.iterdir()), [])

    def test_write_gzip_preserves_backup_flow_with_structured_failures(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            missing = FileNotFoundError(2, "missing", "/private/backup/tool")
            with (
                patch("updater.commands.subprocess.Popen", side_effect=missing),
                self.assertRaises(CommandStartFailed) as start_failure,
            ):
                CommandRunner().write_gzip(
                    ["backup-tool", "dump"],
                    root / "start.sql.gz",
                )
            self.assertEqual(start_failure.exception.failure_class, "command_not_found")
            self.assertNotIn("/private/backup", str(start_failure.exception))

            process = MagicMock()
            process.stdout = io.BytesIO(b"")
            process.stderr = io.BytesIO(b"Basic backup-secret\x00\x1b[31m")
            process.wait.return_value = 23
            process.poll.return_value = 23
            with (
                patch("updater.commands.subprocess.Popen", return_value=process),
                self.assertRaises(CommandExited) as exit_failure,
            ):
                CommandRunner().write_gzip(
                    ["backup-tool", "dump"],
                    root / "exit.sql.gz",
                )
            self.assertEqual(exit_failure.exception.return_code, 23)
            self.assertNotIn("backup-secret", str(exit_failure.exception))
            self.assertNotIn("\x00", str(exit_failure.exception))
            self.assertNotIn("\x1b", str(exit_failure.exception))

            process = MagicMock()
            process.stdout = io.BytesIO(b"")
            process.stderr = io.BytesIO(b"Bearer timeout-secret")
            process.wait.side_effect = [
                subprocess.TimeoutExpired(["backup-tool", "dump"], 19),
                0,
            ]
            process.poll.return_value = -9
            with (
                patch("updater.commands.subprocess.Popen", return_value=process),
                self.assertRaises(CommandTimedOut) as timeout_failure,
            ):
                CommandRunner().write_gzip(
                    ["backup-tool", "dump"],
                    root / "timeout.sql.gz",
                    timeout=19,
                )
            self.assertEqual(timeout_failure.exception.timeout_seconds, 19)
            self.assertNotIn("timeout-secret", str(timeout_failure.exception))
            process.kill.assert_called_once_with()

    def test_write_gzip_timeout_covers_live_stdout_and_full_stderr_pipes(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "blocked.sql.gz"
            started = time.monotonic()
            with self.assertRaises(CommandTimedOut) as raised:
                CommandRunner().write_gzip(
                    [
                        sys.executable,
                        "-c",
                        (
                            "import sys,time; "
                            "sys.stderr.buffer.write(b'x' * 1000000); "
                            "sys.stderr.flush(); time.sleep(30)"
                        ),
                    ],
                    destination,
                    timeout=1,
                )

            self.assertEqual(raised.exception.timeout_seconds, 1)
            self.assertLess(time.monotonic() - started, 10)
            self.assertFalse(destination.exists())


if __name__ == "__main__":
    unittest.main()
