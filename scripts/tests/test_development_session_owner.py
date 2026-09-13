"""One synthetic secret, real Windows scope exclusion, and independent round capabilities."""
import copy
import io
import json
import os
import pickle
import threading
import time
import unittest
from contextlib import nullcontext, redirect_stdout
from dataclasses import replace
from unittest import mock

from scripts import candidate_batch_session as batches
from scripts import candidate_guest_session as guests
from scripts import development_capture_scope as scope
from scripts import development_session_owner as owners
from scripts.tests import test_local_candidate_development as development_fixtures


@unittest.skipUnless(os.name == 'nt', 'real Windows scope locking')
class DevelopmentMemoryOwnerTests(unittest.TestCase):
    def setUp(self):
        setup = development_fixtures.DevelopmentBatchFlowTests()
        setup.setUp()
        self.addCleanup(setup.doCleanups)
        self.fixture = setup.fixture
        self.fixture.batch.close()
        self.material = owners._material(self.fixture.plan)
        for _ in range(6):
            scope.reserve_development_capture(scope.AUTHORIZATION, material_identity=self.material).close()
        self.deadline = time.monotonic() + 3600
        self.utc_deadline = time.time() + 3600
        for name, value in (('_has_additional_capture_grant', True),
                ('scope_deadline', (self.deadline, {'effective_expires_utc_seconds': self.utc_deadline}))):
            patch = mock.patch.object(scope, name, return_value=value)
            patch.start()
            self.addCleanup(patch.stop)
        for _ in range(2):
            scope.reserve_development_capture(scope.AUTHORIZATION, material_identity=self.material).close()
        self.owner = owners.acquire_development_session_owner(material_identity=self.material)
        self.addCleanup(self.owner.dispose)
        self.console = self.fixture.console
        self.secret = self.fixture.secret
        self.original = bytes(self.secret)
        self.batch = None

    def begin(self, number):
        plan = self.fixture.plan
        profiles = tuple(replace(profile, clone_identity='sha256:' + format(number, 'x') * 63 + str(index))
            for index, profile in enumerate(plan.profiles))
        plan = replace(plan, session_id=format(number, 'x') * 32, profiles=profiles)
        plan = replace(plan, plan_digest=batches.h.sha256_bytes(batches.h.canonical_json_bytes(plan.identity_body())))
        batch = batches.CandidateBatch(self.fixture.provider, plan, authorization_id=scope.AUTHORIZATION,
            development_owner=self.owner)
        self.batch = batch
        self.addCleanup(batch.close)
        with batch.operation('BOOTSTRAP', plan.profiles[0]), redirect_stdout(io.StringIO()):
            batch.capture_after_bootstrap_observation(plan.profiles[0])
        return batch

    def completed_failure(self, batch):
        # The producer/Guest protocol tests exercise delivery; this test keeps
        # the resulting closed record to exercise the owner handoff itself.
        profile = batch.plan.profiles[0].profile
        for role, entry in batch._record['profiles'][profile].items():
            entry.update(delivery_attempts=1, delivery_completed=1, target_verified=True, lease_verified=True,
                operation_result='ERROR' if role == 'CANDIDATE_WORKLOAD' else 'PASS')
        batch.revoke('CANDIDATE_SECRET_USE_FAILED')
        batch.close()
        return {'credential_session': batch.record, 'source_preserved': True, 'cleanup_errors': [],
            'profile_results': {name: {'status': 'ERROR' if name == profile else 'NOT_RUN_SHARED_BLOCKER'}
                for name in ('FRESH_BASE', 'DOCKER_BASE', 'RUNTIME_BASE_OFFLINE')},
            'private_material_root_released': True, 'private_execution_source_root_released': True,
            'private_material_root': str(scope.LEDGER.parent / 'absent-material'),
            'private_execution_source_root': str(scope.LEDGER.parent / 'absent-source'),
            'profile_operations': {profile: {'power_state': 'STOPPED', 'clone_disposition': 'QUARANTINED',
                'cleanup_errors': [], 'session_keys_removed': True, 'known_hosts_removed': True, 'lease_released': True}},
            'workload_diagnostics': {profile: {'root_started': True, 'transport_error': None,
                'profile_draft_received': False, 'host_receipt_parse': 'NOT_REACHED',
                'exit_codes': {'INSTALLER': 0, 'RUNTIME_RUNNER': 2, 'ROOT': 2, 'SUDO': 2},
                'errors': ['PROFILE_RECEIPT_INVALID', 'RUNNER_EXECUTION_FAILED', 'ROOT_EXECUTION_FAILED']}},
            'status': 'FAIL', 'all_profiles_pass': False}

    def test_four_rounds_capture_once_and_keep_scope_exclusive_between_rounds(self):
        first_metadata = (scope.LEDGER / 'scope.json').read_bytes()
        for index in range(9, 13):
            batch = self.begin(index)
            use = batch._secret
            self.assertIs(type(use), owners.DevelopmentSecretUse)
            value = use.delivery_value(batch)
            self.assertEqual(value, self.original)
            value[:] = b'\0' * len(value)
            self.assertEqual(batch.record['session_capture_attempts'], int(index == 9))
            report = self.completed_failure(batch)
            self.assertEqual(self.secret, self.original)
            with self.assertRaises(owners.DevelopmentOwnerError):
                use.require_live(batch)
            self.owner.finish_round(report)
            if index < 12:
                self.assertEqual(self.owner.record['state'], 'READY')
                self.assertEqual(self.secret, self.original)
                with self.assertRaises((scope.DevelopmentScopeError, OSError)):
                    scope.acquire_development_scope_owner()
                with self.assertRaises(scope.DevelopmentScopeError):
                    scope.reserve_development_capture(scope.AUTHORIZATION, material_identity=self.material)
        self.console.capture.assert_called_once()
        self.assertEqual(self.secret, b'')
        self.assertEqual(self.owner.record['close_reason'], 'DEVELOPMENT_ROUND_BUDGET_EXHAUSTED')
        self.assertEqual((scope.LEDGER / 'scope.json').read_bytes(), first_metadata)
        self.assertNotIn(self.original.decode(), json.dumps(self.owner.record))
        with self.assertRaises(TypeError):
            pickle.dumps(self.owner)

    def test_persistent_capability_uses_all_nine_existing_verified_delivery_grants(self):
        from scripts.tests.test_candidate_diagnostics import successful_frames
        fixture = self.fixture
        batch = batches.CandidateBatch(fixture.provider, fixture.plan, authorization_id=scope.AUTHORIZATION,
            development_owner=self.owner)
        self.addCleanup(batch.close)
        fixture.batch, fixture.provider._candidate_batch = batch, batch
        for index in range(3):
            profile, runtime = fixture.bootstrap(index)
            operation = guests._diagnostic_operation(fixture.plan, profile)
            fixture.fixture.runner.before_exchange = lambda process, operation=operation: setattr(process, 'stdout',
                io.BytesIO(process.stdout.getvalue() + successful_frames(operation)))
            with batch.operation('WORKLOAD', profile):
                use = batch.issue(profile, fixture.lease, batches.ROLES[2:])
                supervisor = guests._WorkloadSupervisor(use, provider=fixture.provider, plan=fixture.plan,
                    profile=profile, lease=fixture.lease, preboot_disk_graph_digest=runtime.disk_graph_digest,
                    preboot_snapshot_identity=runtime.snapshot_identity)
                try:
                    with (mock.patch.object(guests, '_root_program', return_value='pass'),
                          mock.patch.object(guests, 'hold_windows_private_file', side_effect=lambda _: nullcontext())):
                        self.assertEqual(supervisor.execute()['result'], 'PASS')
                    batch.role_result(profile, 'CANDIDATE_WORKLOAD', 'PASS')
                finally:
                    supervisor.close()
            fixture.fixture.runner.before_exchange = None
        self.console.capture.assert_called_once()
        self.assertEqual(len(fixture.fixture.runner.processes), 9)
        for process in fixture.fixture.runner.processes:
            self.assertEqual(process.stdin.getvalue(), self.original + b'\n')
        self.assertEqual(self.secret, self.original)
        batch.close()
        report = {'credential_session': batch.record, 'source_preserved': True, 'cleanup_errors': [],
            'profile_results': {name: {'status': 'PASS'} for name in ('FRESH_BASE', 'DOCKER_BASE', 'RUNTIME_BASE_OFFLINE')},
            'private_material_root_released': True, 'private_execution_source_root_released': True,
            'private_material_root': str(scope.LEDGER.parent / 'absent-material'),
            'private_execution_source_root': str(scope.LEDGER.parent / 'absent-source'),
            'profile_operations': {profile.profile: {'power_state': 'STOPPED', 'clone_disposition': 'REMOVED',
                'clone_vmx': str(scope.LEDGER.parent / ('removed-' + profile.profile)),
                'cleanup_errors': [], 'session_keys_removed': True, 'known_hosts_removed': True, 'lease_released': True}
                for profile in fixture.plan.profiles}, 'status': 'PASS', 'all_profiles_pass': True}
        self.owner.finish_round(report)
        self.assertEqual(self.secret, b'')
        self.assertEqual(self.owner.record['close_reason'], 'DEVELOPMENT_PREACCEPTANCE_PASSED')

    def test_uncertain_transport_or_cleanup_does_not_retain_secret(self):
        batch = self.begin(9)
        report = self.completed_failure(batch)
        report['workload_diagnostics'][batch.plan.profiles[0].profile]['transport_error'] = 'TRANSPORT_TRUNCATED'
        with self.assertRaises(owners.DevelopmentOwnerError):
            self.owner.finish_round(report)
        self.assertEqual(self.secret, b'')

    def test_later_fatal_revocation_is_not_hidden_by_earlier_generic_failure(self):
        batch = self.begin(9)
        batch.revoke('CANDIDATE_SECRET_USE_FAILED')
        self.assertEqual(self.secret, self.original)
        batch.revoke('CANDIDATE_BATCH_DELIVERY_UNCERTAIN')
        self.assertEqual(self.secret, b'')
        self.assertTrue(self.owner.closed)

    def test_owner_deadline_does_not_reset_on_round_or_idle_wait(self):
        batch = self.begin(9)
        self.owner.finish_round(self.completed_failure(batch))
        self.owner._clock = lambda: self.deadline + 1
        with self.assertRaises(owners.DevelopmentOwnerError):
            self.owner._live()
        self.assertEqual(self.secret, b'')
        self.assertEqual(self.owner.record['close_reason'], 'DEVELOPMENT_SESSION_EXPIRED')

    def test_scope_substitution_and_repeated_clone_are_rejected(self):
        batch = self.begin(9)
        self.owner.finish_round(self.completed_failure(batch))
        with self.assertRaises(owners.DevelopmentOwnerError):
            self.begin(9)
        self.assertEqual(self.batch.record['session_capture_attempts'], 0)
        self.assertEqual(self.batch.record['session_capture_completed'], 0)
        self.assertEqual(self.console.capture.call_count, 1)

    def test_next_profile_authority_failure_before_operation_registration_clears_owner(self):
        batch = self.begin(9)
        report = self.completed_failure(batch)
        for role in report['credential_session']['profiles']['FRESH_BASE'].values():
            role['operation_result'] = 'PASS'
        report['profile_results']['FRESH_BASE']['status'] = 'PASS'
        report['profile_results']['DOCKER_BASE']['status'] = 'ERROR'
        operation = report['profile_operations']['FRESH_BASE']
        operation['clone_disposition'] = 'REMOVED'
        operation['clone_vmx'] = str(scope.LEDGER.parent / 'removed-first-clone')
        with self.assertRaises(owners.DevelopmentOwnerError):
            self.owner.finish_round(report)
        self.assertEqual(self.secret, b'')

    def test_first_capture_cancel_records_one_attempt_and_zero_completion(self):
        self.console.capture.side_effect = KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            self.begin(9)
        self.assertEqual(self.batch.record['session_capture_attempts'], 1)
        self.assertEqual(self.batch.record['session_capture_completed'], 0)

    def test_blocked_cleanup_observation_cannot_delay_owner_wipe(self):
        batch = self.begin(9)
        report = self.completed_failure(batch)
        entered, release = threading.Event(), threading.Event()
        failures = []
        def exists(_path):
            entered.set()
            release.wait(2)
            return False
        def finish():
            try:
                self.owner.finish_round(report)
            except owners.DevelopmentOwnerError as error:
                failures.append(error.code)
        with mock.patch.object(owners.Path, 'exists', exists):
            worker = threading.Thread(target=finish)
            worker.start()
            try:
                self.assertTrue(entered.wait(1))
                self.owner._clock = lambda: self.deadline + 1
                self.assertTrue(self.owner._done.wait(1))
                self.assertEqual(self.secret, b'')
            finally:
                release.set()
                worker.join(timeout=2)
        self.assertFalse(worker.is_alive())
        self.assertTrue(failures)


class DevelopmentFailureClassificationTests(unittest.TestCase):
    def test_only_complete_authenticated_execution_failure_is_retained(self):
        value = {'root_started': True, 'transport_error': None, 'profile_draft_received': False,
            'host_receipt_parse': 'NOT_REACHED', 'errors': ['PROFILE_RECEIPT_INVALID'],
            'exit_codes': {'INSTALLER': 0, 'ROOT': 2, 'RUNTIME_RUNNER': 2, 'SUDO': 2}}
        self.assertTrue(owners.authenticated_execution_failure(value))
        for changes in ({'root_started': False}, {'transport_error': 'TRANSPORT_TRUNCATED'},
                {'profile_draft_received': True}, {'host_receipt_parse': 'REJECTED'},
                {'errors': ['TRANSPORT_PROTOCOL_INVALID']}, {'errors': ['arbitrary-private-text']}):
            self.assertFalse(owners.authenticated_execution_failure({**value, **changes}))
        for component in ('INSTALLER', 'ROOT', 'RUNTIME_RUNNER', 'SUDO'):
            changed = copy.deepcopy(value)
            changed['exit_codes'][component] = None
            self.assertFalse(owners.authenticated_execution_failure(changed))
