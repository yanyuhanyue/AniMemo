from __future__ import annotations

import os
import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

from installer import platform_bootstrap as bootstrap
from installer.production import CandidatePlatformCommandObserver


class PlatformProbeCompositionTests(unittest.TestCase):
    def test_real_missing_process_is_preserved_as_launch_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            argv = (str(Path(directory) / "absent-executable"), "--version")
            observer = CandidatePlatformCommandObserver(
                bootstrap.SubprocessPlatformCommandRunner())
            with self.assertRaises(bootstrap.PlatformBootstrapError) as raised:
                observer.run(argv, timeout=5, environment=dict(os.environ))
        self.assertEqual(raised.exception.code, "PLATFORM_BOOTSTRAP_HOST_STATE_INCONSISTENT")
        self.assertIsNone(raised.exception.command_result.returncode)
        self.assertEqual(raised.exception.command_result.outcome, "LAUNCH_FAILED")
        self.assertEqual(observer.completed_commands, ())

    def test_real_timeout_is_not_an_optional_capability_absence(self):
        observer = CandidatePlatformCommandObserver(bootstrap.SubprocessPlatformCommandRunner())
        with self.assertRaises(bootstrap.PlatformBootstrapError) as raised:
            observer.run((sys.executable, "-c", "import time; time.sleep(5)"),
                         timeout=0.05, environment=dict(os.environ))
        self.assertEqual(raised.exception.command_result.outcome, "TIMEOUT")
        self.assertIs(type(raised.exception.command_result.returncode), int)
        self.assertEqual(observer.completed_commands, ())

    def test_apt_noncompletion_retains_operation_evidence_without_completed_record(self):
        for outcome in ("LAUNCH_FAILED", "TIMEOUT", "CANCELLED", "TOOL_VERSION_UNSUPPORTED"):
            with self.subTest(outcome=outcome):
                result = bootstrap.PlatformCommandResult(None, outcome=outcome,
                                                        observation={"outcome": outcome})
                observer = CandidatePlatformCommandObserver(mock.Mock(run=mock.Mock(return_value=result)))
                self.assertIs(observer.run(("/usr/bin/apt-get", "update"),
                                           timeout=5, environment={}), result)
                self.assertEqual(observer.completed_commands, ())

    def test_only_explicit_postgres_absence_skips_process(self):
        observer = CandidatePlatformCommandObserver(bootstrap.SubprocessPlatformCommandRunner())
        with mock.patch.object(Path, "lstat", side_effect=FileNotFoundError):
            for executable in ("/usr/bin/pg_dump", "/usr/bin/psql"):
                self.assertIsNone(bootstrap._optional_postgres_client_major(observer, executable))
        self.assertEqual(observer.completed_commands, ())
        for failure in (PermissionError, NotADirectoryError, OSError):
            with self.subTest(failure=failure), mock.patch.object(Path, "lstat", side_effect=failure):
                with self.assertRaises(bootstrap.PlatformBootstrapError):
                    bootstrap._optional_postgres_client_major(observer, "/usr/bin/pg_dump")

    def test_existing_entry_disappearing_before_launch_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            missing = str(Path(directory) / "absent-after-lstat")
            real = bootstrap.SubprocessPlatformCommandRunner()
            class RoutedRunner:
                def run(self, argv, **kwargs):
                    return real.run((missing, *argv[1:]), **kwargs)
            observer = CandidatePlatformCommandObserver(RoutedRunner())
            with mock.patch.object(Path, "lstat", return_value=Path(directory).stat()):
                with self.assertRaises(bootstrap.PlatformBootstrapError) as raised:
                    bootstrap._optional_postgres_client_major(observer, "/usr/bin/pg_dump")
        self.assertEqual(raised.exception.command_result.outcome, "LAUNCH_FAILED")
        self.assertIsNone(raised.exception.command_result.returncode)

    def test_existing_client_uses_real_bounded_version_process(self):
        real = bootstrap.SubprocessPlatformCommandRunner()
        class RoutedRunner:
            def run(self, argv, **kwargs):
                return real.run((sys.executable, "-c", "print('psql (PostgreSQL) 16.4')"),
                                **kwargs)
        observer = CandidatePlatformCommandObserver(RoutedRunner())
        with mock.patch.object(Path, "lstat", return_value=Path(sys.executable).stat()):
            self.assertEqual(bootstrap._optional_postgres_client_major(observer, "/usr/bin/psql"), 16)
        self.assertEqual(observer.completed_commands[0]["returnCode"], 0)

    def test_collector_with_real_runner_and_observer_accepts_absent_fresh_clients(self):
        calls = []
        real = bootstrap.SubprocessPlatformCommandRunner()
        class RoutedRunner:
            def run(self, argv, **kwargs):
                calls.append(argv)
                if argv[0] in {"/usr/bin/pg_dump", "/usr/bin/psql"}:
                    raise AssertionError("Absent optional executable was launched")
                output = "amd64" if argv[0] == "/usr/bin/dpkg" else ""
                return real.run((sys.executable, "-c", "print(" + repr(output) + ")"),
                                **kwargs)
        observer = CandidatePlatformCommandObserver(RoutedRunner())
        with ExitStack() as patches:
            patches.enter_context(mock.patch.object(bootstrap, "_read_os_release", return_value=("ubuntu", "24.04")))
            patches.enter_context(mock.patch.object(bootstrap, "_apt_sources_evidence", return_value=(True, "sha256:" + "a" * 64)))
            patches.enter_context(mock.patch.object(bootstrap, "_trusted_root_regular", return_value=True))
            patches.enter_context(mock.patch.object(bootstrap, "_trusted_file_identity", return_value=None))
            patches.enter_context(mock.patch.object(bootstrap, "_compose_plugin_identity", return_value=(False, None)))
            patches.enter_context(mock.patch.object(bootstrap, "_docker_daemon_identity", return_value=None))
            patches.enter_context(mock.patch.object(Path, "exists", return_value=False))
            patches.enter_context(mock.patch.object(Path, "is_symlink", return_value=False))
            patches.enter_context(mock.patch.object(Path, "lstat", side_effect=FileNotFoundError))
            facts = bootstrap.collect_bootstrap_host_facts(observer)
        self.assertEqual(facts.architecture, "amd64")
        self.assertIsNone(facts.pg_dump_major)
        self.assertIsNone(facts.psql_major)
        self.assertEqual(len(observer.completed_commands), len(calls))
        self.assertTrue(all(item["returnCode"] == 0 for item in observer.completed_commands))


if __name__ == "__main__":
    unittest.main()
