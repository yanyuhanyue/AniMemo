"""Synthetic Guest identity plus actual Host holds and child transport tests."""
from __future__ import annotations

import io
import ast
import os
from pathlib import Path
import shlex
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from unittest import mock

from scripts import candidate_batch_session as b, candidate_guest_session as c
from scripts import candidate_vm_harness as h
from scripts.tests import test_guest_sudo_session as session_fixtures
from scripts.tests.test_guest_sudo_session import InputSink, SENTINEL
from scripts.tests.test_candidate_diagnostics import successful_frames, OPERATION


@unittest.skipUnless(os.name == 'nt', 'actual Windows held authority')
class WorkloadAuthorityTests(unittest.TestCase):
    def setUp(self):
        fixture = session_fixtures.GuestSudoSessionTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.fixture = fixture
        bootstrap, material, execution, lease = fixture._production_scope(outer_directory_holds=True)
        bootstrap.close()
        self.provider, self.plan, self.profile = fixture.provider, fixture.plan, fixture.profile
        self.lease, self.material = lease, material
        temporary = tempfile.TemporaryDirectory(dir='E:/')
        self.addCleanup(temporary.cleanup)
        for patcher in (mock.patch.object(b, 'CAPTURE_LEDGERS', {b.AUTHORIZATION: Path(temporary.name) / ('d' * 64)}),
                        mock.patch.object(b, '_check_checkout'), mock.patch.object(c, '_check_checkout'),
                        mock.patch.object(c, '_root_program', return_value='pass')):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.batch = b.CandidateBatch(self.provider, self.plan, authorization_id=b.AUTHORIZATION)
        self.provider._candidate_batch = self.batch
        self.addCleanup(self.batch.close)
        self.secret = bytearray(SENTINEL)
        with self.batch.operation('BOOTSTRAP', self.profile), redirect_stdout(io.StringIO()):
            with mock.patch.object(b, 'WindowsConsoleCapture') as constructor:
                constructor.return_value.capture.return_value = self.secret
                self.batch.capture_after_bootstrap_observation(self.profile)
            use = self.batch.issue(self.profile, lease, b.ROLES[:2])
            bootstrap = c.SessionSupervisor(use, provider=self.provider, plan=self.plan,
                profile=self.profile, lease=lease, preboot_disk_graph_digest=fixture.runtime.disk_graph_digest,
                preboot_snapshot_identity=fixture.runtime.snapshot_identity)
            try:
                bootstrap.bootstrap_rotation()
                self.batch.role_result(self.profile, 'BOOTSTRAP_ROTATION', 'PASS')
                fixture.key_read.return_value = fixture.verified.host_key_digest
                fixture.runner.observation = fixture.verified
                bootstrap.validate_verified_guest()
                self.batch.role_result(self.profile, 'VERIFIED_SUDO', 'PASS')
                self.verified = bootstrap._verified
            finally:
                bootstrap.close()
        self.provider._candidate_connections[self.profile.profile] = c._IssuedConnection(
            execution, self.plan.plan_digest, lease, self.verified)
        self.operation = self.batch.operation('WORKLOAD', self.profile)
        self.operation.__enter__()
        # An expired/revoked fixture must still release its registered operation.
        self.addCleanup(self.operation.__exit__, RuntimeError, RuntimeError(), None)
        self.use = self.batch.issue(self.profile, lease, b.ROLES[2:])
        self.session = c._WorkloadSupervisor(self.use, provider=self.provider, plan=self.plan,
            profile=self.profile, lease=lease, preboot_disk_graph_digest=fixture.runtime.disk_graph_digest,
            preboot_snapshot_identity=fixture.runtime.snapshot_identity)
        self.addCleanup(self.session.close)
        self.frames = successful_frames(c._diagnostic_operation(self.plan, self.profile))
        self.fixture.runner.before_exchange = lambda process: setattr(process, 'stdout',
            io.BytesIO(process.stdout.getvalue() + self.frames))

    def run_workload(self):
        return self.session.execute()

    def test_same_profile_continuation_and_single_delivery_keep_actual_holds(self):
        def before(process):
            process.stdout = io.BytesIO(process.stdout.getvalue() + self.frames)
            with self.assertRaises(OSError):
                self.verified.authority.clone_root.rename(self.verified.authority.clone_root.with_name('replaced'))
        class TracedSink(InputSink):
            def write(self, value):
                self.borrowed = value
                return super().write(value)
        sink = TracedSink()
        def traced(process):
            before(process)
            process.stdin = sink
        self.fixture.runner.before_exchange = traced
        original = c._read_receipt
        def receipt(process, **kwargs):
            self.assertEqual(self.secret, SENTINEL, 'later Profiles still need the batch owner')
            self.assertEqual(sink.borrowed, b'', 'the delivery buffer must be wiped before waiting')
            self.session._expires = 0  # Grant expiry must not invalidate an already delivered fixed job.
            return original(process, **kwargs)
        with mock.patch.object(c, '_read_receipt', side_effect=receipt):
            self.assertEqual(self.run_workload(), {'result': 'PASS', 'synthetic_transport_only': True})
        self.assertEqual(self.session.delivery_attempts, {'CANDIDATE_WORKLOAD': 1})
        self.assertEqual(self.session.delivery_completed, {'CANDIDATE_WORKLOAD': 1})
        self.assertIn(self.verified.guest.host_key_digest, self.provider._accepted_host_key_digests)
        self.assertEqual(self.fixture.runner.processes[-1].stdin.getvalue(), SENTINEL + b'\n')
        with self.assertRaises(c.ControllerFailure):
            self.run_workload()

    def test_changed_guest_rejects_before_any_workload_delivery(self):
        self.fixture.runner.observation = replace(self.fixture.verified, boot_id='f' * 32)
        with self.assertRaisesRegex(c.ControllerFailure, 'IDENTITY_CHANGED'):
            self.run_workload()
        self.assertEqual(self.secret, b'')
        self.assertEqual(self.session.delivery_attempts['CANDIDATE_WORKLOAD'], 0)
        self.assertEqual(self.fixture.runner.processes[-1].stdin.getvalue(), b'')

    def test_cross_profile_or_execution_issued_record_is_not_a_ticket(self):
        self.provider._candidate_connections[self.profile.profile] = replace(
            self.provider._candidate_connections[self.profile.profile], execution=object())
        with self.assertRaisesRegex(c.ControllerFailure, 'AUTHORITY_INVALID'):
            self.run_workload()
        self.assertEqual(self.session.delivery_attempts['CANDIDATE_WORKLOAD'], 0)

    def test_closed_material_blocks_workload_delivery(self):
        self.material._closed = True
        with self.assertRaises((c.ControllerFailure, h.CandidateHarnessError)):
            self.run_workload()
        self.assertEqual(self.secret, b'')
        self.assertEqual(self.session.delivery_attempts['CANDIDATE_WORKLOAD'], 0)

    def test_closed_lease_blocks_workload_delivery(self):
        self.lease._closed = True
        try:
            with self.assertRaises((c.ControllerFailure, h.CandidateHarnessError)):
                self.run_workload()
            self.assertEqual(self.session.delivery_attempts['CANDIDATE_WORKLOAD'], 0)
        finally:
            self.lease._closed = False  # Only release fixture resources; never retry.

    def test_partial_write_is_attempted_once_and_wiped_without_retry(self):
        class ShortSink(InputSink):
            calls = 0
            def write(self, value):
                self.calls += 1
                return super().write(value[:3])
        sink = ShortSink()
        self.fixture.runner.before_exchange = lambda process: setattr(process, 'stdin', sink)
        with self.assertRaisesRegex(c.ControllerFailure, 'SHORT_WRITE'):
            self.run_workload()
        self.assertEqual((sink.calls, sink.getvalue()), (1, SENTINEL[:3]))
        self.assertEqual(self.secret, b'')
        self.assertEqual(self.session.delivery_attempts['CANDIDATE_WORKLOAD'], 1)
        self.assertEqual(self.session.delivery_completed['CANDIDATE_WORKLOAD'], 0)
        with self.assertRaises(c.ControllerFailure):
            self.run_workload()
        self.assertEqual(sink.calls, 1)


class WorkloadTransportTests(unittest.TestCase):
    def test_remote_program_is_parseable_and_has_one_fixed_sudo_process(self):
        program = shlex.split(c._remote_workload_command('pass', OPERATION))[-1]
        compile(program, '<synthetic-remote>', 'exec')
        self.assertEqual(program.count('subprocess.Popen('), 1)
        self.assertLess(program.index('password.clear()'), program.index('child.wait()'))
        self.assertIn('bufsize=0', program)
        self.assertNotIn(SENTINEL.decode(), program)

    def test_all_candidate_sudo_executables_are_absolute_and_ignore_guest_path(self):
        from scripts import guest_sudo_session as bootstrap
        programs = [c._remote_workload_command('pass', OPERATION)]
        programs += [bootstrap._remote_command(role, 'ssh-ed25519 YWJj alias')
                     for role in ('BOOTSTRAP_ROTATION', 'VERIFIED_SUDO')]
        for command in programs:
            tree = ast.parse(shlex.split(command)[-1])
            sudo_calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute) and node.func.attr in {'run', 'Popen'}
                and node.args and isinstance(node.args[0], ast.List)
                and any(isinstance(item, ast.Constant) and item.value == '-S' for item in node.args[0].elts)]
            self.assertEqual(len(sudo_calls), 1)
            self.assertEqual(sudo_calls[0].args[0].elts[0].value, '/usr/bin/sudo')


if __name__ == '__main__':
    unittest.main()
