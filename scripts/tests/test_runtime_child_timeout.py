"""Fixed public worker; no credentials, sockets, VM or system effects."""
import os
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path

from scripts.candidate_vm_harness import SubprocessHostCommandRunner


class RuntimeChildTimeoutTests(unittest.TestCase):
    def test_deadline_reaps_fixed_worker(self):
        started = time.monotonic()
        with self.assertRaises(subprocess.TimeoutExpired):
            SubprocessHostCommandRunner().run(
                [sys.executable, '-I', '-B', '-c', 'import time;time.sleep(15)'],
                environment={'SYSTEMROOT': os.environ.get('SYSTEMROOT', 'C:/Windows')},
                cwd=Path.cwd(), timeout=0.15, cancel_event=threading.Event())
        self.assertLess(time.monotonic() - started, 3)
