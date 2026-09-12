"""Real descendant/pipe cancellation and suspended Windows assignment failures."""
from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from scripts import candidate_child_process as child, candidate_vm_harness as h


class ChildProcessTests(unittest.TestCase):
    def test_cancellation_terminates_descendant_after_parent_exits(self):
        cancelled = threading.Event()
        program = "import subprocess,sys;subprocess.Popen([sys.executable,'-c','import time;time.sleep(15)'])"
        timer = threading.Timer(0.3, cancelled.set)
        timer.start()
        self.addCleanup(timer.cancel)
        started = time.monotonic()
        with self.assertRaisesRegex(h.CandidateHarnessError, 'BATCH_REVOKED'):
            h.SubprocessHostCommandRunner().run([sys.executable, '-I', '-B', '-c', program],
                environment={'SYSTEMROOT': os.environ.get('SYSTEMROOT', 'C:/Windows')},
                cwd=Path.cwd(), timeout=5, cancel_event=cancelled)
        self.assertLess(time.monotonic() - started, 3)

    def test_interactive_exchange_cleanup_terminates_descendants(self):
        program = ("import subprocess,sys;subprocess.Popen([sys.executable,'-c','import time;time.sleep(15)']);"
            "print('PUBLIC_READY',flush=True);sys.stdin.buffer.readline()")
        started = time.monotonic()
        def exchange(process):
            self.assertEqual(process.stdout.readline().rstrip(b'\r\n'), b'PUBLIC_READY')
            raise h.CandidateHarnessError('SYNTHETIC_EXCHANGE_FAILURE')
        with self.assertRaisesRegex(h.CandidateHarnessError, 'SYNTHETIC_EXCHANGE_FAILURE'):
            h.SubprocessHostCommandRunner().run_guest_exchange([sys.executable, '-I', '-B', '-c', program],
                environment={'SYSTEMROOT': os.environ.get('SYSTEMROOT', 'C:/Windows')},
                cwd=Path.cwd(), timeout=5, exchange=exchange)
        self.assertLess(time.monotonic() - started, 3)

    @unittest.skipUnless(os.name == 'nt', 'real Windows suspended process')
    def test_failed_job_assignment_never_runs_the_suspended_child(self):
        with tempfile.TemporaryDirectory() as temporary:
            marker = Path(temporary) / 'must-not-run'
            program = 'from pathlib import Path;Path(' + repr(str(marker)) + ").write_bytes(b'public')"
            with mock.patch.object(child._WindowsJob, 'assign_and_resume', side_effect=child.ChildProcessError()):
                with self.assertRaises(child.ChildProcessError):
                    child.OwnedChild([sys.executable, '-I', '-B', '-c', program],
                        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            self.assertFalse(marker.exists())

    @unittest.skipUnless(os.name == 'nt', 'Windows vmrun command containment selection')
    def test_direct_command_containment_does_not_create_a_kill_job(self):
        with mock.patch.object(child, '_WindowsJob', side_effect=AssertionError('VM must use canonical containment')):
            with child.OwnedChild([sys.executable, '-I', '-B', '-c', 'raise SystemExit(0)'],
                    tree=False, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as owned:
                self.assertEqual(owned.process.wait(timeout=5), 0)

    def test_execute_without_current_authorization_or_result_cannot_reach_capture(self):
        from scripts.candidate_batch_session import AUTHORIZATION
        common = ['--execute', '--verified-candidate-digest', 'sha256:' + 'a' * 64,
            '--expected-qualification-run-id', '1', '--expected-source-sha', 'b' * 40,
            '--expected-source-tree', 'c' * 40, '--accept-plan-digest', 'sha256:' + 'd' * 64]
        for extra in ([], ['--authorization-id', AUTHORIZATION]):
            with (mock.patch('scripts.candidate_batch_session.CandidateBatch') as constructor,
                    mock.patch('scripts.guest_console_capture.WindowsConsoleCapture.preflight') as console,
                    redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO())):
                self.assertEqual(h.main(common + extra), 2)
                constructor.assert_not_called()
                console.assert_not_called()


if __name__ == '__main__':
    unittest.main()
