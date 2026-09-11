"""Synthetic Guest identity plus actual Host holds and child transport tests."""
from __future__ import annotations

import io
import json
import os
from pathlib import Path
import shlex
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from types import SimpleNamespace
from unittest import mock

from scripts import candidate_guest_session as c
from scripts import candidate_vm_harness as h
from scripts.tests import test_guest_sudo_session as session_fixtures
from scripts.tests.test_guest_sudo_session import InputSink, SENTINEL


class CaptureSlotsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir='E:/' if os.name == 'nt' else None)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / ('a' * 64)
        self.patch = mock.patch.object(c, 'CAPTURE_LEDGER', self.root)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def test_six_fixed_slots_are_independent_and_cannot_be_replayed(self):
        paths = set()
        for profile in h.PROFILES:
            for purpose in c.PURPOSES:
                slot = c._reserve_capture(profile, purpose)
                paths.add(slot)
                with self.assertRaisesRegex(c.ControllerFailure, 'ALREADY_ATTEMPTED'):
                    c._reserve_capture(profile, purpose)
        self.assertEqual(len(paths), 6)
        for wrong in ('NEW_SESSION', '../FRESH_BASE', 'fresh_base'):
            with self.assertRaisesRegex(c.ControllerFailure, 'SCOPE_INVALID'):
                c._reserve_capture(wrong, 'SESSION_BOOTSTRAP')
        self.assertTrue(all(path.is_dir() for path in paths))

    def test_preflight_failure_does_not_consume_but_cancel_does(self):
        provider = SimpleNamespace(_candidate_credential_results={})
        plan = SimpleNamespace(source_sha='a' * 40, source_tree='b' * 40, plan_digest='sha256:'+'c'*64, session_id='d'*32, qualification_run_id=42, candidate_input_digest='sha256:'+'e'*64, verified_candidate_digest='sha256:'+'f'*64)
        profile = SimpleNamespace(profile='FRESH_BASE')
        with mock.patch.object(c, '_check_checkout'), mock.patch.object(c, 'WindowsConsoleCapture') as constructor:
            console = constructor.return_value
            console.preflight.side_effect = c.ConsoleCaptureError('CREDENTIAL_CHANNEL_UNAVAILABLE')
            with self.assertRaises(c.ConsoleCaptureError), c._capture(provider, plan, profile, 'SESSION_BOOTSTRAP'):
                self.fail('channel must not open')
            self.assertFalse(self.root.exists())
            console.preflight.side_effect = None
            console.capture.side_effect = c.ConsoleCaptureError('CREDENTIAL_CAPTURE_CANCELLED')
            with redirect_stdout(io.StringIO()), self.assertRaises(c.ConsoleCaptureError), c._capture(provider, plan, profile, 'SESSION_BOOTSTRAP'):
                self.fail('cancelled capture must not yield')
            with self.assertRaisesRegex(c.ControllerFailure, 'ALREADY_ATTEMPTED'):
                c._reserve_capture('FRESH_BASE', 'SESSION_BOOTSTRAP')
        record = provider._candidate_credential_results['FRESH_BASE']['SESSION_BOOTSTRAP']
        self.assertEqual((record['capture_attempts'], record['capture_completed']), (1, 0))
        self.assertEqual(record['delivery_attempts'], {'BOOTSTRAP_ROTATION': 0, 'VERIFIED_SUDO': 0})
        self.assertEqual(record['secret_cleanup'], 'BEST_EFFORT_COMPLETED')

    def test_secret_is_wiped_and_public_count_record_has_no_secret(self):
        secret = bytearray(SENTINEL)
        provider = SimpleNamespace(_candidate_credential_results={})
        plan = SimpleNamespace(source_sha='a' * 40, source_tree='b' * 40, plan_digest='sha256:'+'c'*64, session_id='d'*32, qualification_run_id=42, candidate_input_digest='sha256:'+'e'*64, verified_candidate_digest='sha256:'+'f'*64)
        profile = SimpleNamespace(profile='DOCKER_BASE')
        with mock.patch.object(c, '_check_checkout'), mock.patch.object(c, 'WindowsConsoleCapture') as constructor, redirect_stdout(io.StringIO()):
            constructor.return_value.capture.return_value = secret
            with c._capture(provider, plan, profile, 'CANDIDATE_WORKLOAD') as (captured, _):
                self.assertIs(captured, secret)
        self.assertEqual(secret, b'')
        for path in self.root.rglob('result.json'):
            self.assertNotIn(SENTINEL, path.read_bytes())


@unittest.skipUnless(os.name == 'nt', 'actual Windows held authority')
class WorkloadAuthorityTests(unittest.TestCase):
    def setUp(self):
        fixture = session_fixtures.GuestSudoSessionTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.fixture = fixture
        bootstrap, material, execution, lease = fixture._production_scope(outer_directory_holds=True)
        bootstrap.bootstrap_rotation()
        fixture.key_read.return_value = fixture.verified.host_key_digest
        fixture.runner.observation = fixture.verified
        bootstrap.validate_verified_guest()
        self.verified = bootstrap._verified
        bootstrap.close()
        self.provider, self.plan, self.profile = fixture.provider, fixture.plan, fixture.profile
        self.lease, self.material = lease, material
        self.provider._candidate_connections[self.profile.profile] = c._IssuedConnection(
            execution, self.plan.plan_digest, lease, self.verified)
        self.secret = bytearray(SENTINEL)
        self.session = c._WorkloadSupervisor(self.secret, provider=self.provider, plan=self.plan,
            profile=self.profile, lease=lease, preboot_disk_graph_digest=fixture.runtime.disk_graph_digest,
            preboot_snapshot_identity=fixture.runtime.snapshot_identity)
        self.addCleanup(self.session.close)
        self.check = mock.patch.object(c, '_check_checkout')
        self.check.start()
        self.addCleanup(self.check.stop)
        self.fixture.runner.before_exchange = lambda process: setattr(process, 'stdout',
            io.BytesIO(process.stdout.getvalue() + b'{"result":"PASS"}\n'))

    def run_workload(self):
        return self.session.execute(c._remote_workload_command('pass'))

    def test_same_profile_continuation_and_single_delivery_keep_actual_holds(self):
        def before(process):
            process.stdout = io.BytesIO(process.stdout.getvalue() + b'{"result":"PASS"}\n')
            with self.assertRaises(OSError):
                self.verified.authority.clone_root.rename(self.verified.authority.clone_root.with_name('replaced'))
        self.fixture.runner.before_exchange = before
        original = c._read_receipt
        def receipt(process, **kwargs):
            self.assertEqual(self.secret, b'', 'secret must be gone before the long receipt wait')
            self.session._expires = 0  # Grant expiry must not invalidate an already delivered fixed job.
            return original(process, **kwargs)
        with mock.patch.object(c, '_read_receipt', side_effect=receipt):
            self.assertEqual(self.run_workload(), {'result': 'PASS'})
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
    def test_real_child_receipt_larger_than_pipe_is_drained_without_deadlock(self):
        program = "import sys,json;sys.stdin.buffer.readline();print(json.dumps({'payload':'x'*300000}))"
        result = []
        secret = bytearray(SENTINEL)
        def exchange(process):
            process.stdin.write(secret + b'\n')
            process.stdin.flush()
            process.stdin.close()
            c.wipe(secret)
            result.append(c._read_receipt(process))
        completed = h.SubprocessHostCommandRunner().run_guest_exchange(
            (sys.executable, '-I', '-B', '-c', program), environment={}, cwd=Path(sys.executable).parent,
            exchange=exchange, timeout=10)
        self.assertEqual(completed.returncode, 0)
        self.assertEqual(secret, b'')
        self.assertEqual(len(result[0]['payload']), 300000)

    def test_oversized_or_truncated_receipt_is_rejected(self):
        for body in (b'', b'{', b'{"a":1,"a":2}', b'x' * (c.MAX_RECEIPT_BYTES + 1)):
            with self.subTest(size=len(body)), self.assertRaises(c.ControllerFailure):
                c._read_receipt(SimpleNamespace(stdout=io.BytesIO(body)))

    def test_remote_program_is_parseable_and_has_one_fixed_sudo_process(self):
        program = shlex.split(c._remote_workload_command('pass'))[-1]
        compile(program, '<synthetic-remote>', 'exec')
        self.assertEqual(program.count('subprocess.Popen('), 1)
        self.assertLess(program.index('password.clear()'), program.index('child.wait()'))
        self.assertIn('bufsize=0', program)
        self.assertNotIn(SENTINEL.decode(), program)


if __name__ == '__main__':
    unittest.main()
