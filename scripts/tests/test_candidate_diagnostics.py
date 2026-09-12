"""Bounded framed transport with real subprocesses and synthetic sentinels."""
from __future__ import annotations

import io
import json
import os
from pathlib import Path
import struct
import sys
import threading
from types import SimpleNamespace
import unittest

from scripts import candidate_diagnostics as d, candidate_guest_session as c, candidate_vm_harness as h

OPERATION = 'sha256:' + 'a' * 64
SENTINEL = 'synthetic-diagnostic-secret-never-log'


def frame(kind, value):
    raw = json.dumps(value, separators=(',', ':')).encode()
    return kind + struct.pack('!I', len(raw)) + raw


def event(operation, kind, **fields):
    return frame(b'D', dict(schema=d.SCHEMA, operation=operation, kind=kind, **fields))


def successful_frames(operation=OPERATION, padding=None):
    value = {'result': 'PASS', 'synthetic_transport_only': True}
    if padding is not None:
        value['padding'] = padding
    return (b''.join(event(operation, 'STAGE', stage=stage) for stage in d.STAGES if stage != 'HOST_PARSED')
        + frame(b'R', value)
        + b''.join(event(operation, 'EXIT', component=component, exit_code=0) for component in d.COMPONENTS))


class DiagnosticTests(unittest.TestCase):
    def consume(self, raw):
        self.provider = SimpleNamespace(_candidate_diagnostics={})
        return c._read_receipt(SimpleNamespace(stdout=io.BytesIO(raw)), operation=OPERATION,
            provider=self.provider, profile=SimpleNamespace(profile='FRESH_BASE'), timeout=5)

    def test_receipt_and_diagnostics_remain_separate(self):
        receipt = self.consume(successful_frames())
        self.assertTrue(receipt['synthetic_transport_only'])
        diagnostic = self.provider._candidate_diagnostics['FRESH_BASE']
        self.assertTrue(diagnostic['root_started'])
        self.assertEqual(diagnostic['exit_codes'], {item: 0 for item in d.COMPONENTS})
        self.assertNotIn('result', diagnostic)

    def test_missing_root_marker_is_unknown_not_password_failure(self):
        raw = event(OPERATION, 'STAGE', stage='SSH_OBSERVED') + event(OPERATION, 'EXIT', component='SUDO', exit_code=1)
        with self.assertRaisesRegex(c.WorkloadFailure, 'UNKNOWN_BEFORE_ROOT_START') as caught:
            self.consume(raw)
        self.assertTrue(caught.exception.revoke_batch)
        self.assertIsNone(self.provider._candidate_diagnostics['FRESH_BASE']['exit_codes']['ROOT'])

    def test_runtime_failure_is_shared_and_keeps_actual_exit_codes(self):
        raw = b''.join(event(OPERATION, 'STAGE', stage=stage) for stage in d.STAGES[:6])
        raw += event(OPERATION, 'ERROR', code='RUNTIME_INITIALIZATION_FAILED')
        raw += b''.join(event(OPERATION, 'EXIT', component=component, exit_code=2) for component in ('RUNTIME_RUNNER', 'ROOT', 'SUDO'))
        with self.assertRaises(c.WorkloadFailure) as caught:
            self.consume(raw)
        self.assertTrue(caught.exception.revoke_batch)
        self.assertIsNone(self.provider._candidate_diagnostics['FRESH_BASE']['exit_codes']['INSTALLER'])

    def test_trusted_installer_failure_may_continue_after_external_cleanup(self):
        raw = b''.join(event(OPERATION, 'STAGE', stage=stage) for stage in d.STAGES[:11])
        raw += event(OPERATION, 'ERROR', code='PLATFORM_PREPARATION_FAILED')
        raw += event(OPERATION, 'ERROR', code='INSTALLER_EXECUTION_FAILED')
        raw += b''.join(event(OPERATION, 'EXIT', component=component, exit_code=2) for component in d.COMPONENTS)
        with self.assertRaises(c.WorkloadFailure) as caught:
            self.consume(raw)
        self.assertFalse(caught.exception.revoke_batch)

    def test_malformed_truncated_large_duplicate_and_wrong_binding_do_not_escape(self):
        hostile = [SENTINEL.encode(), b'D\0\0', b'R' + struct.pack('!I', d.MAX_RECEIPT_BYTES + 1),
            frame(b'D', dict(schema=d.SCHEMA, operation=OPERATION, kind='ERROR', code=SENTINEL)),
            event('sha256:' + 'b' * 64, 'STAGE', stage='ROOT_STARTED'),
            event(OPERATION, 'STAGE', stage='ROOT_STARTED') * 2,
            event(OPERATION, 'STAGE', stage='RUNTIME_READY') + event(OPERATION, 'STAGE', stage='ROOT_STARTED')]
        for body in hostile:
            with self.subTest(size=len(body)), self.assertRaises(c.WorkloadFailure) as caught:
                self.consume(body)
            self.assertTrue(caught.exception.revoke_batch)
            self.assertNotIn(SENTINEL, str(caught.exception))
            self.assertNotIn(SENTINEL, json.dumps(self.provider._candidate_diagnostics))

    def test_real_child_large_frame_drains_without_pipe_deadlock(self):
        program = ('import sys;sys.path.insert(0,' + repr(str(Path.cwd()))
            + ");from scripts.tests.test_candidate_diagnostics import successful_frames;"
            + "sys.stdout.buffer.write(successful_frames(padding='x'*100000));sys.stdout.buffer.flush()")
        observed = {}
        provider = SimpleNamespace(_candidate_diagnostics={})
        def exchange(process):
            process.stdin.close()
            observed['receipt'] = c._read_receipt(process, operation=OPERATION, provider=provider,
                profile=SimpleNamespace(profile='FRESH_BASE'), timeout=10)
        completed = h.SubprocessHostCommandRunner().run_guest_exchange([sys.executable, '-I', '-B', '-c', program],
            environment={'SYSTEMROOT': os.environ.get('SYSTEMROOT', 'C:/Windows')}, cwd=Path.cwd(), exchange=exchange, timeout=15)
        self.assertEqual(completed.returncode, 0)
        self.assertEqual(len(observed['receipt']['padding']), 100000)

    def test_revocation_interrupts_a_real_inflight_child(self):
        cancelled = threading.Event()
        timer = threading.Timer(0.2, cancelled.set)
        timer.start()
        self.addCleanup(timer.cancel)
        with self.assertRaisesRegex(h.CandidateHarnessError, 'BATCH_REVOKED'):
            h.SubprocessHostCommandRunner().run([sys.executable, '-I', '-B', '-c', 'import time;time.sleep(60)'],
                environment={'SYSTEMROOT': os.environ.get('SYSTEMROOT', 'C:/Windows')}, cwd=Path.cwd(), timeout=10, cancel_event=cancelled)


if __name__ == '__main__':
    unittest.main()
