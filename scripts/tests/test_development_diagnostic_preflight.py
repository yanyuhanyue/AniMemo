"""No VM or capture: fixed preflight framing, bootstrap ordering and containment."""
from __future__ import annotations

import ast
import base64
from contextlib import ExitStack, nullcontext, redirect_stderr
import hashlib
import json
import os
from pathlib import Path
import shlex
import struct
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest import mock
import zlib

from scripts import candidate_batch_session as batches
from scripts import candidate_diagnostics as diagnostic
from scripts import candidate_guest_session as session
from scripts import candidate_vm_harness as h
from scripts import development_diagnostic_preflight as preflight
from scripts import development_source
from scripts.development_plan import from_material_plan
from scripts import formal_profile_runner
from scripts.tests import test_candidate_vm_harness as plan_fixtures
from scripts.tests import test_candidate_profile_cleanup as cleanup_fixtures

ROOT = Path(__file__).resolve().parents[2]


class DiagnosticPreflightTests(unittest.TestCase):
    def setUp(self):
        self.actual_bootstrap = session.bootstrap_candidate
        fixture = plan_fixtures.CandidateVmHarnessTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        self.plan = from_material_plan(fixture._plan(), execution_source_sha='a' * 40,
            execution_source_tree='b' * 40, execution_inventory_digest='sha256:' + 'c' * 64,
            platform_diagnostic=True, published_subject_digest='sha256:' + 'd' * 64)
        self.profile = self.plan.profiles[0]
        self.source = SimpleNamespace(root=ROOT, inventory_digest=self.plan.execution_inventory_digest)
        self.authority = SimpleNamespace(clone_vmx=fixture.root / 'synthetic.vmx')
        self.guest = object()
        self.verified = SimpleNamespace(authority=self.authority, guest=self.guest)
        self.lease = SimpleNamespace(authority=self.authority, require_open=mock.Mock())
        self.order, self.children = [], []
        self.batch = SimpleNamespace(plan=self.plan, cancelled=threading.Event(),
            _lock=threading.RLock(), _operation=None, _captured_at=None, _clock=time.monotonic,
            check_source=mock.Mock(side_effect=lambda: self.order.append('source-check')),
            capture_after_bootstrap_observation=mock.Mock(side_effect=RuntimeError('CAPTURE_BOUNDARY_REACHED')),
            issue=mock.Mock())
        self.batch.operation = lambda kind, profile: batches.CandidateBatch.operation(self.batch, kind, profile)
        self.provider = SimpleNamespace(_profile_operation_results={self.profile.profile: {}},
            _candidate_diagnostics={}, _active_profile_authority=mock.Mock(return_value=self.authority),
            _ssh_argv=mock.Mock(side_effect=lambda authority, command, **kw: (str(sys.executable), command)),
            _tool_path=mock.Mock(return_value=Path(sys.executable)),
            _running_vmx_paths=mock.Mock(return_value=frozenset({os.path.normcase(str(self.authority.clone_vmx.resolve()))})),
            _observe_guest_connection=mock.Mock(return_value=self.guest), _read_known_host_key=mock.Mock(return_value='sha256:'+'e'*64),
            _ssh_checked=mock.Mock(), _scp_argv=mock.Mock(return_value=('synthetic-scp',)),
            _verify_bootstrap_connection=mock.Mock(return_value=self.verified),
            _remove_known_hosts=mock.Mock(side_effect=lambda authority: self.order.append('hosts-removed')))
        self.provider._run = mock.Mock(side_effect=self.run_transport)
        self.frames_mode = 'COMPLETE'
        self.stack = self.enterContext(ExitStack())
        self.stack.enter_context(mock.patch.object(development_source, 'require_development_source', return_value=self.source))
        self.stack.enter_context(mock.patch.object(session, '_batch', return_value=self.batch))
        actual_read = session._read_receipt
        self.stack.enter_context(mock.patch.object(session, '_read_receipt',
            side_effect=lambda process, **kw: actual_read(process, **{**kw, 'timeout': 0.15})))

    def encoded_frames(self):
        operation = preflight.fixed_command(self.source, self.plan)[1]
        if self.frames_mode == 'WRONG_OPERATION':
            operation = 'sha256:' + 'f' * 64
        with tempfile.TemporaryFile() as stream, tempfile.TemporaryDirectory() as temporary:
            writer = diagnostic.DiagnosticWriter(stream.fileno(), operation)
            writer.stage('RUNTIME_READY')
            writer.stage('RUNNER_STARTED')
            with mock.patch.dict(os.environ, {diagnostic.FD_ENV: str(stream.fileno()), diagnostic.OP_ENV: operation}), \
                    open(os.devnull, 'w') as discarded, redirect_stderr(discarded):
                code = formal_profile_runner.main(['--authority-root', str(Path(temporary) / 'absent'),
                    '--profile', 'FORMAL_FRESH', '--execute'])
            self.assertEqual(code, 2)
            writer.exited('RUNTIME_RUNNER', code)
            stream.seek(0)
            frames = []
            while item := diagnostic.read_frame(stream):
                kind, body = item
                event = json.loads(body) if kind == b'D' else {}
                if self.frames_mode == 'MISSING_END' and event.get('kind') == 'FAULT_END':
                    continue
                if self.frames_mode == 'MISSING_ENVELOPE' and event.get('kind') in {'FAULT_BEGIN', 'FAULT_END'}:
                    continue
                frames.append(kind + struct.pack('!I', len(body)) + body)
        raw = b''.join(frames)
        if self.frames_mode == 'EMPTY':
            raw = b''
        if self.frames_mode == 'TRUNCATED':
            raw = raw[:-2]
        return raw

    def run_transport(self, argv, **kwargs):
        if 'guest_exchange' not in kwargs:
            self.order.append('transfer')
            return SimpleNamespace(returncode=0)
        self.order.append('selftest')
        if self.frames_mode == 'SILENT':
            program = 'import time;time.sleep(30)'
        else:
            program = 'import base64,sys;sys.stdout.buffer.write(base64.b64decode(' + repr(
                base64.b64encode(self.encoded_frames()).decode('ascii')) + '));sys.stdout.flush()'
        callback = kwargs['guest_exchange']
        def exchange(process):
            self.children.append(process)
            callback(process)
        result = h.SubprocessHostCommandRunner().run_guest_exchange(
            (sys.executable, '-I', '-B', '-c', program), environment={}, cwd=Path(sys.executable).parent,
            exchange=exchange, timeout=2)
        self.order.append('selftest-closed')
        return result

    def bootstrap(self):
        return self.actual_bootstrap(self.provider, self.plan, self.profile, self.lease,
                                          'sha256:' + 'e' * 64, self.profile.snapshot_identity)

    def assert_children_closed(self):
        for child in self.children:
            self.assertIsNotNone(child.poll())
            self.assertTrue(child.stdin.closed)
            self.assertTrue(child.stdout.closed)

    def test_valid_selftest_closes_process_and_rechecks_target_before_capture(self):
        def stop_at_capture(_profile):
            self.order.append('capture')
            raise RuntimeError('CAPTURE_BOUNDARY_REACHED')
        self.batch.capture_after_bootstrap_observation.side_effect = stop_at_capture
        with self.assertRaisesRegex(RuntimeError, 'CAPTURE_BOUNDARY_REACHED'):
            self.bootstrap()
        self.assertEqual(self.order[-4:], ['selftest-closed', 'source-check', 'hosts-removed', 'capture'])
        self.assertEqual(self.provider._observe_guest_connection.call_count, 3)
        record = self.provider._profile_operation_results[self.profile.profile]['diagnostic_preflight']
        self.assertEqual(record['result'], 'PASS')
        self.assertEqual(record['sudo_capture_attempts'], 0)
        self.assertEqual(record['diagnostic']['failure_diagnostic']['status'], 'COMPLETE')
        self.batch.issue.assert_not_called()
        self.assertIsNone(self.batch._operation)
        self.assert_children_closed()

    def test_missing_wrong_operation_incomplete_and_broken_pipe_prevent_capture(self):
        for mode in ('EMPTY', 'WRONG_OPERATION', 'MISSING_END', 'MISSING_ENVELOPE', 'TRUNCATED'):
            with self.subTest(mode=mode):
                self.frames_mode = mode
                with self.assertRaises((session.ControllerFailure, h.CandidateHarnessError)):
                    self.bootstrap()
                self.batch.capture_after_bootstrap_observation.assert_not_called()
                self.batch.issue.assert_not_called()
                self.provider._remove_known_hosts.assert_not_called()
                self.assertIsNone(self.batch._operation)
                self.assertNotIn(self.profile.profile, self.provider._candidate_diagnostics)
                self.assert_children_closed()

    def test_silent_child_timeout_and_cancel_reap_without_capture(self):
        for cancel in (False, True):
            with self.subTest(cancel=cancel):
                self.frames_mode = 'SILENT'
                self.batch.cancelled.clear()
                if cancel:
                    self.batch.cancelled.set()
                started = time.monotonic()
                with self.assertRaises(session.WorkloadFailure):
                    self.bootstrap()
                self.assertLess(time.monotonic() - started, 5)
                self.batch.capture_after_bootstrap_observation.assert_not_called()
                self.assertIsNone(self.batch._operation)
                self.assert_children_closed()

    def test_wrong_target_lease_and_closed_lease_prevent_transfer_or_capture(self):
        changes = (
            mock.patch.object(self.lease, 'authority', object()),
            mock.patch.object(self.verified, 'authority', object()),
            mock.patch.object(self.lease, 'require_open', side_effect=session.ControllerFailure('SYNTHETIC_LEASE_CLOSED')),
            mock.patch.object(self.provider._running_vmx_paths, 'return_value', frozenset({'unexpected.vmx'})),
            mock.patch.object(self.provider._observe_guest_connection, 'return_value', object()),
        )
        for change in changes:
            with change, self.assertRaises((session.ControllerFailure, h.CandidateHarnessError)):
                self.bootstrap()
            self.provider._run.assert_not_called()
            self.batch.capture_after_bootstrap_observation.assert_not_called()

    def test_target_change_after_selftest_still_blocks_capture(self):
        self.provider._observe_guest_connection.side_effect = [self.guest, self.guest, object()]
        with self.assertRaisesRegex(h.CandidateHarnessError, 'TARGET_CHANGED'):
            self.bootstrap()
        self.batch.capture_after_bootstrap_observation.assert_not_called()
        self.provider._remove_known_hosts.assert_not_called()
        record = self.provider._profile_operation_results[self.profile.profile]['diagnostic_preflight']
        self.assertEqual(record['result'], 'ERROR')
        self.assertEqual(record['diagnostic']['failure_diagnostic']['status'], 'COMPLETE')
        self.assert_children_closed()

    def test_fixed_command_checks_inventory_before_one_named_entry(self):
        stage, operation, command = preflight.fixed_command(self.source, self.plan)
        argv = shlex.split(command)
        self.assertEqual(argv[:4], ['/usr/bin/python3', '-I', '-B', '-c'])
        syntax = ast.parse(argv[4])
        encoded = next(ast.literal_eval(node.args[0]) for node in ast.walk(syntax)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == 'b64decode')
        program = zlib.decompress(base64.b64decode(encoded)).decode('utf-8')
        syntax = ast.parse(program)
        check = next(i for i, statement in enumerate(syntax.body) if isinstance(statement, ast.Assert))
        entry = next(i for i, statement in enumerate(syntax.body) if isinstance(statement, ast.ImportFrom)
                     and statement.module == 'scripts.development_diagnostic_preflight')
        self.assertLess(check, entry)
        self.assertEqual(stage, '/tmp/animemo-diagnostic-selftest-' + self.plan.session_id)
        self.assertEqual(operation, 'sha256:' + hashlib.sha256(
            ('DIAGNOSTIC_SELFTEST\n' + self.plan.plan_digest).encode('ascii')).hexdigest())
        self.assertIn(repr(self.source.inventory_digest), program)
        self.assertNotIn('sudo', command)
        self.assertNotIn('shell=True', program)
        with self.assertRaises(TypeError):
            preflight.fixed_command(self.source, self.plan, command='arbitrary')

    def test_preflight_timeout_propagates_into_existing_provider_containment(self):
        fixture = cleanup_fixtures.ProfileCleanupTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        # Exercise the real shared failure cleanup with an actual pre-capture
        # hook timeout; transport and VM observations remain synthetic.
        self.frames_mode = 'SILENT'
        fixture.bootstrap.side_effect = lambda *_args: self.bootstrap()
        fixture.running.return_value = True
        batch = mock.Mock(cancelled=threading.Event())
        batch.operation.side_effect = lambda *_: nullcontext()
        fixture.provider._candidate_batch = batch
        with self.assertRaises(h.CandidateHarnessError):
            fixture.execute()
        fixture.assert_no_keys()
        fixture.contain.assert_called_once()
        fixture.release.assert_called_once()
        batch.revoke.assert_called()
        operation = fixture.provider._profile_operation_results[fixture.profile.profile]
        self.assertEqual(operation['power_state'], 'STOPPED')
        self.assertTrue(operation['lease_released'])
        self.assertFalse(operation['continuation_authorized'])
        self.batch.capture_after_bootstrap_observation.assert_not_called()
        self.assert_children_closed()


if __name__ == '__main__':
    unittest.main()
