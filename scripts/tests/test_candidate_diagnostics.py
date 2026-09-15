"""Bounded framed transport with real subprocesses and synthetic sentinels."""
from __future__ import annotations

import io
import json
import os
import struct
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from scripts import candidate_diagnostics as d
from scripts import candidate_guest_session as c
from scripts import candidate_vm_harness as h

OPERATION = 'sha256:' + 'a' * 64
SENTINEL = 'synthetic-diagnostic-secret-never-log'


def frame(kind, value):
    raw = json.dumps(value, separators=(',', ':')).encode()
    return kind + struct.pack('!I', len(raw)) + raw


def event(operation, kind, **fields):
    return frame(b'D', dict(schema=d.SCHEMA, operation=operation, kind=kind, **fields))


def business_failure_frames(operation=OPERATION):
    raw = b''.join(event(operation, 'STAGE', stage=stage) for stage in d.STAGES[:12])
    for code in ('PLATFORM_PREPARATION_FAILED', 'PLATFORM_BOOTSTRAP_DOCKER_DAEMON_FAILED',
                 'INSTALLER_EXECUTION_FAILED', 'RUNNER_EXECUTION_FAILED', 'ROOT_EXECUTION_FAILED'):
        raw += event(operation, 'ERROR', code=code)
    for component in ('INSTALLER', 'RUNTIME_RUNNER', 'ROOT', 'SUDO'):
        raw += event(operation, 'EXIT', component=component, exit_code=6 if component == 'INSTALLER' else 2)
    return raw


def business_failure_diagnostic():
    reader = d.DiagnosticReader(OPERATION)
    stream = io.BytesIO(business_failure_frames())
    while item := d.read_frame(stream):
        reader.accept(*item)
    return reader.public()


def successful_frames(operation=OPERATION, padding=None):
    value = {'result': 'PASS', 'synthetic_transport_only': True}
    if padding is not None:
        value['padding'] = padding
    return (b''.join(event(operation, 'STAGE', stage=stage) for stage in d.STAGES if stage != 'HOST_PARSED')
        + frame(b'R', value)
        + b''.join(event(operation, 'EXIT', component=component, exit_code=0) for component in d.COMPONENTS))


class DiagnosticTests(unittest.TestCase):
    def test_report_counts_are_closed_bounded_integers(self):
        fields = {'commands': 500, 'pull_denied_commands': 80, 'doctor_checks': 29}
        value = dict(schema=d.SCHEMA, operation=OPERATION, kind='REPORT_COUNTS', **fields)
        self.assertEqual(d.validate_event(value, OPERATION), value)
        for changed in ({'commands': True}, {'commands': -1}, {'doctor_checks': SENTINEL},
                        {'pull_denied_commands': d.MAX_RECEIPT_BYTES + 1}, {'extra': SENTINEL}):
            with self.assertRaises(d.DiagnosticError):
                d.validate_event({**value, **changed}, OPERATION)

    def test_runpy_development_entry_failure_keeps_only_known_location(self):
        namespace = {'__name__': '__main__', 'SECRET': SENTINEL}
        exec(compile('def fail():\n raise PermissionError(SECRET)\n',  # noqa: S102 - fixed synthetic traceback
            'X:/private-sentinel/scripts/development_profile_runner.py', 'exec'), namespace)
        writer = d.DiagnosticWriter(0, OPERATION)
        with mock.patch.object(writer, 'event') as emit:
            try:
                namespace['fail']()
            except PermissionError as error:
                writer.fault(error)
        emit.assert_called_once_with('FAULT', module='scripts.development_profile_runner', line=2, category='PermissionError')

    def test_doctor_failure_event_contains_only_closed_check_identifiers(self):
        from durability.doctor import DOCTOR_CHECK_IDS
        self.assertEqual(d.DOCTOR_CHECKS, DOCTOR_CHECK_IDS)
        reader = d.DiagnosticReader(OPERATION)
        reader.accept(b'D', json.dumps(dict(schema=d.SCHEMA, operation=OPERATION,
            kind='DOCTOR', failed_checks=['filesystem.permissions', 'plugins.integrity'])).encode())
        self.assertEqual(reader.public()['events'][0]['failed_checks'],
            ['filesystem.permissions', 'plugins.integrity'])
        for checks in ([], [SENTINEL], ['filesystem.permissions'] * 2, [True], [{}], 'filesystem.permissions'):
            with self.assertRaises(d.DiagnosticError):
                d.validate_event(dict(schema=d.SCHEMA, operation=OPERATION, kind='DOCTOR', failed_checks=checks), OPERATION)

    def test_fault_keeps_command_caller_and_limits_nested_tracebacks(self):
        command = {'__name__': 'updater.commands', 'SECRET': SENTINEL}
        deployment = {'__name__': 'updater.deployment'}
        runtime = {'__name__': 'updater.runtime'}
        exec(compile('def fail():\n raise ValueError(SECRET)\n',
            'X:/private-sentinel/updater/commands.py', 'exec'), command)
        deployment['command'] = command['fail']
        exec(compile('def inspect():\n command()\n',
            'X:/private-sentinel/updater/deployment.py', 'exec'), deployment)
        runtime['inspect'] = deployment['inspect']
        exec(compile('def adopt():\n try:\n  inspect()\n except ValueError as error:\n  raise RuntimeError("private-wrapper") from error\n',
            'X:/private-sentinel/updater/runtime.py', 'exec'), runtime)
        with tempfile.TemporaryFile() as stream:
            writer = d.DiagnosticWriter(stream.fileno(), OPERATION)
            try:
                runtime['adopt']()
            except RuntimeError as error:
                writer.fault(error)
            stream.seek(0)
            reader = d.DiagnosticReader(OPERATION)
            while item := d.read_frame(stream):
                reader.accept(*item)
        faults = reader.public()['events']
        self.assertLessEqual(len(faults), 6)
        self.assertIn(('updater.deployment', 2), {(f['module'], f['line']) for f in faults})
        self.assertIn(('updater.runtime', 3), {(f['module'], f['line']) for f in faults})
        for private in (SENTINEL, 'private-wrapper', 'private-sentinel'):
            self.assertNotIn(private, json.dumps(reader.public()))

    def test_fault_locations_are_bounded_without_exception_text_paths_or_locals(self):
        namespace = {'__name__': 'updater.runtime', 'SECRET': SENTINEL}
        exec(compile('def fail():\n raise ValueError(SECRET)\n',
            'X:/private-sentinel/updater/runtime.py', 'exec'), namespace)
        with tempfile.TemporaryFile() as stream:
            writer = d.DiagnosticWriter(stream.fileno(), OPERATION)
            try:
                namespace['fail']()
            except ValueError as error:
                writer.fault(error)
            stream.seek(0)
            reader = d.DiagnosticReader(OPERATION)
            while item := d.read_frame(stream):
                reader.accept(*item)
        fault = reader.public()['events'][0]
        self.assertEqual({key: fault[key] for key in ('module', 'line', 'category')},
            {'module': 'updater.runtime', 'line': 2, 'category': 'ValueError'})
        self.assertNotIn(SENTINEL, json.dumps(reader.public()))
        self.assertNotIn('private-sentinel', json.dumps(reader.public()))
        for fields in ({'module': SENTINEL, 'line': 2, 'category': 'ValueError'},
                       {'module': 'updater.runtime', 'line': True, 'category': 'ValueError'},
                       {'module': 'updater.runtime', 'line': 2, 'category': SENTINEL}):
            with self.assertRaises(d.DiagnosticError):
                d.validate_event(dict(schema=d.SCHEMA, operation=OPERATION, kind='FAULT', **fields), OPERATION)

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
        with self.assertRaises(c.WorkloadFailure) as caught:
            self.consume(business_failure_frames())
        self.assertFalse(caught.exception.revoke_batch)

    def test_malformed_truncated_large_duplicate_and_wrong_binding_do_not_escape(self):
        hostile = [SENTINEL.encode(), b'D\0\0', b'R' + struct.pack('!I', d.MAX_RECEIPT_BYTES + 1),
            frame(b'D', dict(schema=d.SCHEMA, operation=OPERATION, kind='ERROR', code=SENTINEL)),
            event('sha256:' + 'b' * 64, 'STAGE', stage='ROOT_STARTED'),
            event(OPERATION, 'STAGE', stage='ROOT_STARTED') * 2,
            event(OPERATION, 'STAGE', stage='HOST_PARSED'),
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

    def test_bounded_installer_output_keeps_exit_and_never_returns_stderr(self):
        code, stdout, stderr = d.bounded_process_output([sys.executable, '-I', '-B', '-c',
            "import sys;sys.stdout.write('{}');sys.stderr.write('synthetic-diagnostic-secret-never-log');sys.exit(17)"],
            environment={'SYSTEMROOT': os.environ.get('SYSTEMROOT', 'C:/Windows')}, timeout=5)
        self.assertEqual((code, stdout, stderr), (17, b'{}', b''))

    def test_bounded_installer_output_rejects_limit_and_timeout(self):
        for program, code in (("import sys;sys.stdout.buffer.write(b'x'*10000)", 'TRANSPORT_LIMIT_EXCEEDED'),
                              ('import time;time.sleep(60)', 'WORKLOAD_TIMEOUT')):
            with mock.patch.object(d, 'MAX_RECEIPT_BYTES', 1000), self.assertRaisesRegex(d.DiagnosticError, code):
                d.bounded_process_output([sys.executable, '-I', '-B', '-c', program],
                    environment={'SYSTEMROOT': os.environ.get('SYSTEMROOT', 'C:/Windows')}, timeout=0.25)

    @unittest.skipUnless(os.name == 'posix', 'POSIX owned process group')
    def test_timeout_cancels_descendant_holding_stdout_after_parent_exit(self):
        program = "import subprocess,sys;subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'])"
        with self.assertRaisesRegex(d.DiagnosticError, 'WORKLOAD_TIMEOUT'):
            d.bounded_process_output([sys.executable, '-I', '-B', '-c', program], environment={}, timeout=0.25)

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
