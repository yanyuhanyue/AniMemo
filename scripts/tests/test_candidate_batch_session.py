"""Synthetic secret, canonical target/grant checks, and real private ledger races."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout, nullcontext
from dataclasses import replace
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from scripts import candidate_batch_session as b, candidate_guest_session as c
from scripts import guest_sudo_session as guest
from scripts.tests import test_guest_sudo_session as session_fixtures
from scripts.tests.test_guest_sudo_session import SENTINEL, InputSink


class FixedAuthorizationTests(unittest.TestCase):
    def test_pr247_scope_uses_only_its_fixed_ascii_digest_path_without_reserving(self):
        # Only resolve the production path. Every actual reserve uses the
        # isolated mappings in BatchSessionTests; this never touches a ledger.
        self.assertEqual(b.capture_ledger(b.PR247_REVALIDATION_AUTHORIZATION),
            Path('E:/1e0c088ec6cb93149000f3d00a88111230dc38a8ebcf8233340e1fcfa5df086f'))
        for unknown in (None, '', b.PR247_REVALIDATION_AUTHORIZATION + '_NEXT',
                        'ANIMEMO_V2_CANDIDATE_PR247_REVALIDATION_SINGLE_CAPTURE_V2'):
            with self.assertRaisesRegex(guest.ControllerFailure, 'AUTHORIZATION_INVALID'):
                b.capture_ledger(unknown)


class BatchSessionTests(unittest.TestCase):
    def setUp(self):
        fixture = session_fixtures.GuestSudoSessionTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.fixture = fixture
        self.provider, self.plan = fixture.provider, fixture.plan
        self.time = 10.0
        temporary = tempfile.TemporaryDirectory(dir='E:/' if os.name == 'nt' else None)
        self.addCleanup(temporary.cleanup)
        self.ledger = Path(temporary.name) / ('a' * 64)
        self.new_ledger = Path(temporary.name) / ('b' * 64)
        self.legacy_ledger = Path(temporary.name) / ('c' * 64)
        patcher = mock.patch.object(b, 'CAPTURE_LEDGERS', {
            b.AUTHORIZATION: self.legacy_ledger, b.GATEWAY_REPAIR_AUTHORIZATION: self.new_ledger,
            b.PR247_REVALIDATION_AUTHORIZATION: self.ledger})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.batch = b.CandidateBatch(self.provider, self.plan,
            authorization_id=b.PR247_REVALIDATION_AUTHORIZATION, clock=lambda: self.time)
        self.provider._candidate_batch = self.batch
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(b, '_check_checkout').start()
        mock.patch.object(c, '_check_checkout').start()
        self.console = mock.patch.object(b, 'WindowsConsoleCapture').start().return_value
        self.secret = bytearray(SENTINEL)
        self.console.capture.return_value = self.secret
        self.addCleanup(self.batch.close)
        self.lease = SimpleNamespace(require_open=lambda: None)

    def capture(self):
        with self.batch.operation('BOOTSTRAP', self.plan.profiles[0]), redirect_stdout(io.StringIO()):
            self.batch.capture_after_bootstrap_observation(self.plan.profiles[0])

    def target(self, index):
        profile = self.plan.profiles[index]
        authority = self.provider._active_profile_authority(profile, self.plan)
        self.lease = SimpleNamespace(authority=authority, require_open=lambda: None)
        runtime = replace(self.fixture.runtime, clone_root=authority.clone_root, clone_vmx=authority.clone_vmx,
            snapshot_name=profile.snapshot_name, snapshot_identity=profile.snapshot_identity)
        bootstrap = replace(self.fixture.bootstrap,
            boot_id=f'{index + 1:08x}-2222-4333-8444-555555555555',
            nonce=c.h.connection_challenge(profile.connection_nonce, 'guestinfo'))
        verified = replace(bootstrap, host_key_digest='sha256:' + format(index + 10, 'x') * 64)
        self.fixture.runtime_read.return_value = runtime
        self.fixture.running.return_value = frozenset({os.path.normcase(str(authority.clone_vmx.resolve(strict=False)))})
        self.fixture.observe.return_value = bootstrap
        self.fixture.key_read.return_value = bootstrap.host_key_digest
        self.fixture.runner.observation = bootstrap
        return profile, runtime, verified

    def bootstrap(self, index):
        profile, runtime, verified = self.target(index)
        with self.batch.operation('BOOTSTRAP', profile), redirect_stdout(io.StringIO()):
            self.batch.capture_after_bootstrap_observation(profile)
            use = self.batch.issue(profile, self.lease, b.ROLES[:2])
            supervisor = guest.SessionSupervisor(use, provider=self.provider, plan=self.plan,
                profile=profile, lease=self.lease, preboot_disk_graph_digest=runtime.disk_graph_digest,
                preboot_snapshot_identity=runtime.snapshot_identity, clock=lambda: self.time)
            try:
                supervisor.bootstrap_rotation()
                self.batch.role_result(profile, 'BOOTSTRAP_ROTATION', 'PASS')
                self.fixture.key_read.return_value = verified.host_key_digest
                self.fixture.runner.observation = verified
                supervisor.validate_verified_guest()
                self.batch.role_result(profile, 'VERIFIED_SUDO', 'PASS')
            finally:
                supervisor.close()
            self.assertEqual(self.secret, SENTINEL)
            self.provider._candidate_connections[profile.profile] = c._IssuedConnection(
                self.provider._execution, self.plan.plan_digest, self.lease, supervisor._verified)
        return profile, runtime

    def test_one_capture_three_targets_nine_grants_and_early_owner_cleanup(self):
        from scripts.tests.test_candidate_diagnostics import successful_frames
        for index in range(3):
            profile, runtime = self.bootstrap(index)
            operation = c._diagnostic_operation(self.plan, profile)
            self.fixture.runner.before_exchange = lambda process: setattr(process, 'stdout',
                io.BytesIO(process.stdout.getvalue() + successful_frames(operation)))
            with self.batch.operation('WORKLOAD', profile):
                use = self.batch.issue(profile, self.lease, b.ROLES[2:])
                supervisor = c._WorkloadSupervisor(use, provider=self.provider, plan=self.plan,
                    profile=profile, lease=self.lease, preboot_disk_graph_digest=runtime.disk_graph_digest,
                    preboot_snapshot_identity=runtime.snapshot_identity)
                try:
                    with (mock.patch.object(c, '_root_program', return_value='pass'),
                            mock.patch.object(c, 'hold_windows_private_file', side_effect=lambda _: nullcontext())):
                        self.assertEqual(supervisor.execute(), {'result': 'PASS', 'synthetic_transport_only': True})
                    self.batch.role_result(profile, 'CANDIDATE_WORKLOAD', 'PASS')
                finally:
                    supervisor.close()
            self.fixture.runner.before_exchange = None
            self.assertEqual(self.secret, SENTINEL if index < 2 else b'')
        self.console.capture.assert_called_once()
        self.assertEqual(len(self.fixture.runner.processes), 9)
        self.assertEqual(len(self.provider._accepted_host_key_digests), 3)
        for process in self.fixture.runner.processes:
            self.assertEqual(process.stdin.getvalue(), SENTINEL + b'\n')
        self.assertEqual(self.batch.record['secret_state'], 'RELEASED_NO_FURTHER_USES')
        self.batch.close()
        record = json.loads((self.ledger / 'result.json').read_bytes())
        self.assertEqual(record['session_capture_attempts'], 1)
        self.assertEqual(record['session_capture_completed'], 1)
        self.assertNotIn(SENTINEL.decode(), json.dumps(record))
        self.assertEqual(record['secret_state'], 'CLOSED')

    def test_preflight_does_not_reserve_and_capture_cancel_consumes(self):
        self.console.preflight.side_effect = RuntimeError('native preflight unavailable')
        with self.assertRaises(RuntimeError):
            self.capture()
        self.assertFalse(self.ledger.exists())
        self.console.preflight.side_effect = None
        self.console.capture.side_effect = KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            self.capture()
        self.assertTrue(self.ledger.is_dir())
        self.assertEqual(self.batch.record['session_capture_attempts'], 1)
        self.assertEqual(self.batch.record['session_capture_completed'], 0)
        with self.assertRaises(guest.ControllerFailure):
            self.capture()

    def test_two_launchers_race_for_the_same_irreversible_capture(self):
        def reserve():
            try:
                b.reserve_capture(b.PR247_REVALIDATION_AUTHORIZATION)
                return 'RESERVED'
            except guest.ControllerFailure:
                return 'REJECTED'
        with ThreadPoolExecutor(max_workers=2) as pool:
            values = list(pool.map(lambda _: reserve(), range(2)))
        self.assertCountEqual(values, ['RESERVED', 'REJECTED'])

    def test_new_fixed_scope_preserves_consumed_old_scope_and_requires_explicit_selection(self):
        originals = {}
        for authorization, ledger in ((b.AUTHORIZATION, self.legacy_ledger),
                                      (b.GATEWAY_REPAIR_AUTHORIZATION, self.new_ledger)):
            b.reserve_capture(authorization)
            (ledger / 'preserved.json').write_bytes(b'{"consumed":true}\n')
            originals[ledger] = (ledger / 'preserved.json').read_bytes()
            with self.assertRaisesRegex(guest.ControllerFailure, 'ALREADY_ATTEMPTED'):
                b.reserve_capture(authorization)
        for unknown in (None, '', 'arbitrary-new-scope', str(self.new_ledger)):
            with self.assertRaisesRegex(guest.ControllerFailure, 'AUTHORIZATION_INVALID'):
                b.CandidateBatch(self.provider, self.plan, authorization_id=unknown)
        with self.assertRaisesRegex(guest.ControllerFailure, 'AUTHORIZATION_INVALID'):
            b.CandidateBatch(self.provider, self.plan)
        with self.assertRaisesRegex(guest.ControllerFailure, 'AUTHORIZATION_INVALID'):
            b.reserve_capture()
        self.console.capture.assert_not_called()
        self.assertFalse(self.ledger.exists())
        batch = b.CandidateBatch(self.provider, self.plan, authorization_id=b.PR247_REVALIDATION_AUTHORIZATION)
        self.addCleanup(batch.close)
        self.assertFalse(self.ledger.exists())
        self.assertEqual(batch.record['authorization_id'], b.PR247_REVALIDATION_AUTHORIZATION)
        def reserve():
            try:
                b.reserve_capture(b.PR247_REVALIDATION_AUTHORIZATION)
                return 'RESERVED'
            except guest.ControllerFailure:
                return 'REJECTED'
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertCountEqual(list(pool.map(lambda _: reserve(), range(2))), ['RESERVED', 'REJECTED'])
        with batch.operation('BOOTSTRAP', self.plan.profiles[0]):
            with self.assertRaisesRegex(guest.ControllerFailure, 'ALREADY_ATTEMPTED'):
                batch.capture_after_bootstrap_observation(self.plan.profiles[0])
        self.console.capture.assert_not_called()
        for ledger, original in originals.items():
            self.assertEqual((ledger / 'preserved.json').read_bytes(), original)

    def test_new_scope_capture_record_and_restart_after_source_change_share_one_budget(self):
        batch = b.CandidateBatch(self.provider, self.plan, authorization_id=b.PR247_REVALIDATION_AUTHORIZATION)
        self.addCleanup(batch.close)
        with batch.operation('BOOTSTRAP', self.plan.profiles[0]), redirect_stdout(io.StringIO()):
            batch.capture_after_bootstrap_observation(self.plan.profiles[0])
        batch.close()
        original = (self.ledger / 'result.json').read_bytes()
        record = json.loads(original)
        self.assertEqual(record['authorization_id'], b.PR247_REVALIDATION_AUTHORIZATION)
        self.assertEqual(record['session_capture_attempts'], 1)
        self.assertFalse(self.new_ledger.exists())
        changed = replace(self.plan, source_sha='f'*40, qualification_run_id=self.plan.qualification_run_id+1)
        changed = replace(changed, plan_digest=c.h.sha256_bytes(c.h.canonical_json_bytes(changed.identity_body())))
        restarted = b.CandidateBatch(self.provider, changed, authorization_id=b.PR247_REVALIDATION_AUTHORIZATION)
        self.addCleanup(restarted.close)
        with restarted.operation('BOOTSTRAP', changed.profiles[0]):
            with self.assertRaisesRegex(guest.ControllerFailure, 'ALREADY_ATTEMPTED'):
                restarted.capture_after_bootstrap_observation(changed.profiles[0])
        self.console.capture.assert_called_once()
        self.assertEqual((self.ledger / 'result.json').read_bytes(), original)

    def test_idle_and_hard_deadlines_use_monotonic_time_and_close_owner(self):
        self.capture()
        with mock.patch('time.time', return_value=-10**10):
            self.time += b.IDLE_SECONDS - 1
            self.batch.require_live()
            self.time += 1
            with self.assertRaisesRegex(guest.ControllerFailure, 'IDLE_EXPIRED'):
                self.batch.require_live()
        self.assertEqual(self.secret, b'')
        self.assertTrue(self.batch.cancelled.is_set())

    def test_registered_long_work_is_not_idle_but_has_no_hard_deadline_extension(self):
        self.capture()
        for _ in range(2):
            with self.batch.operation('WORKLOAD', self.plan.profiles[0]):
                self.time += 4 * 60 * 60
                self.batch.require_live()
        with self.assertRaisesRegex(guest.ControllerFailure, 'HARD_EXPIRED'):
            with self.batch.operation('WORKLOAD', self.plan.profiles[1]):
                self.time += 4 * 60 * 60
                self.batch.require_live()
        self.assertEqual(self.secret, b'')

    def test_short_write_revokes_whole_batch_and_never_reprompts(self):
        class ShortSink(InputSink):
            def write(self, data):
                return super().write(data[:3])
        self.fixture.runner.before_exchange = lambda process: setattr(process, 'stdin', ShortSink())
        with self.assertRaises(guest.ControllerFailure):
            self.bootstrap(0)
        self.assertTrue(self.batch.cancelled.is_set())
        self.assertEqual(self.secret, b'')
        entry = self.batch.record['profiles']['FRESH_BASE']['BOOTSTRAP_ROTATION']
        self.assertEqual((entry['delivery_attempts'], entry['delivery_completed']), (1, 0))
        with self.assertRaises(guest.ControllerFailure):
            self.bootstrap(1)
        self.console.capture.assert_called_once()

    def test_public_counter_snapshot_cannot_reset_authority(self):
        self.bootstrap(0)
        public = self.batch.record
        public['profiles']['FRESH_BASE']['BOOTSTRAP_ROTATION']['delivery_attempts'] = 0
        public['session_capture_attempts'] = 0
        self.assertEqual(self.batch.record['profiles']['FRESH_BASE']['BOOTSTRAP_ROTATION']['delivery_attempts'], 1)
        self.assertEqual(self.batch.record['session_capture_attempts'], 1)

    def test_changed_source_or_execution_revokes_before_new_use(self):
        self.capture()
        self.provider._execution = object()
        with self.assertRaisesRegex(guest.ControllerFailure, 'SCOPE_CHANGED'):
            self.batch.require_live()
        self.assertEqual(self.secret, b'')

    def test_workload_cannot_skip_bootstrap_or_use_a_raw_buffer(self):
        self.capture()
        with self.assertRaisesRegex(guest.ControllerFailure, 'USE_ORDER_INVALID'):
            with self.batch.operation('WORKLOAD', self.plan.profiles[0]):
                self.batch.issue(self.plan.profiles[0], self.lease, b.ROLES[2:])
        self.assertTrue(self.batch.cancelled.is_set())
        with self.assertRaisesRegex(guest.ControllerFailure, 'BATCH_USE_REQUIRED'):
            c._WorkloadSupervisor(bytearray(SENTINEL))

    def test_next_profile_cannot_inherit_an_unfinished_profile(self):
        self.bootstrap(0)
        with self.assertRaisesRegex(guest.ControllerFailure, 'USE_ORDER_INVALID'):
            self.bootstrap(1)
        self.assertEqual(len(self.fixture.runner.processes), 2)
        self.assertEqual(self.secret, b'')

    def test_operation_deadline_revokes_without_a_new_grant(self):
        self.capture()
        with self.assertRaisesRegex(guest.ControllerFailure, 'OPERATION_EXPIRED'):
            with self.batch.operation('TRANSFER', self.plan.profiles[0]):
                self.time += b.OPERATION_SECONDS['TRANSFER']
                self.assertTrue(self.batch.cancelled.wait(2))
        self.assertEqual(self.secret, b'')
        self.assertEqual(self.batch.record['revocation_code'], 'CANDIDATE_BATCH_OPERATION_EXPIRED')

    def test_human_capture_time_is_not_counted_as_credential_lifetime(self):
        def capture():
            self.time += 20 * 60
            return self.secret
        self.console.capture.side_effect = capture
        self.capture()
        self.batch.require_live()
        self.assertEqual(self.batch._captured_at, self.time)

    def test_source_change_at_delivery_revokes_without_exposing_exception(self):
        self.capture()
        with mock.patch.object(b, '_check_checkout', side_effect=RuntimeError(SENTINEL.decode())):
            with self.assertRaises(guest.ControllerFailure) as caught:
                self.bootstrap(0)
        self.assertNotIn(SENTINEL.decode(), str(caught.exception))
        self.assertEqual(self.secret, b'')
        self.assertEqual(self.batch.record['profiles']['FRESH_BASE']['BOOTSTRAP_ROTATION']['delivery_attempts'], 0)

    def test_unknown_cancellation_text_is_never_written_to_ledger(self):
        self.capture()
        self.batch.revoke(SENTINEL.decode())
        self.batch.close()
        self.assertNotIn(SENTINEL, (self.ledger / 'result.json').read_bytes())

    def test_slow_source_check_cannot_deliver_after_the_grant_deadline(self):
        self.capture()
        def slow_check(*_):
            self.time += 6
        with mock.patch.object(b, '_check_checkout', side_effect=slow_check):
            with self.assertRaises(guest.ControllerFailure):
                self.bootstrap(0)
        self.assertEqual(self.batch.record['profiles']['FRESH_BASE']['BOOTSTRAP_ROTATION']['delivery_attempts'], 0)
        self.assertTrue(all(process.stdin.getvalue() == b'' for process in self.fixture.runner.processes))
        self.assertEqual(self.secret, b'')

    def test_watchdog_can_revoke_while_the_source_check_is_blocked(self):
        self.capture()
        observed = []
        def blocked_check(*_):
            self.time += b.HARD_SECONDS
            observed.append(self.batch.cancelled.wait(2))
        with mock.patch.object(b, '_check_checkout', side_effect=blocked_check):
            with self.assertRaises(guest.ControllerFailure):
                self.bootstrap(0)
        self.assertEqual(observed, [True])
        self.assertEqual(self.batch.record['revocation_code'], 'CANDIDATE_BATCH_HARD_EXPIRED')
        self.assertTrue(all(process.stdin.getvalue() == b'' for process in self.fixture.runner.processes))


if __name__ == '__main__':
    unittest.main()
