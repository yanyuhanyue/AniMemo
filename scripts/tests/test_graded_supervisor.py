"""Real child lifecycle and synthetic secrets through owner/Batch/Supervisor."""
import io
import json
import os
import sys
import unittest
from contextlib import nullcontext, redirect_stdout
from dataclasses import replace
from unittest import mock

from scripts import candidate_batch_session as batches, candidate_guest_session as guest
from scripts import candidate_vm_harness as h
from scripts.tests.test_candidate_diagnostics import business_failure_frames
from scripts.tests import test_development_session_owner as owner_fixtures


@unittest.skipUnless(os.name == 'nt', 'actual Windows child job and private scope')
class GradedSupervisorTests(unittest.TestCase):
    def setUp(self):
        setup = owner_fixtures.DevelopmentMemoryOwnerTests()
        setup.setUp()
        self.addCleanup(setup.doCleanups)
        self.owner, self.secret, self.fixture = setup.owner, setup.secret, setup.fixture
        self.batch = batches.CandidateBatch(self.fixture.provider, self.fixture.plan,
            authorization_id=owner_fixtures.scope.AUTHORIZATION, development_owner=self.owner)
        self.addCleanup(self.batch.close)
        self.fixture.batch = self.fixture.provider._candidate_batch = self.batch
        self.profile, self.runtime = self.fixture.bootstrap(0)
        self.fixture.fixture.observe.return_value = self.fixture.fixture.runner.observation
        self.fixture.provider._profile_operation_results[self.profile.profile] = {}

    def execute(self, exit_code=2, *, short_write=False):
        fixture, batch = self.fixture, self.batch
        operation = guest._diagnostic_operation(fixture.plan, self.profile)
        observation = fixture.fixture.runner.observation
        public = {key: getattr(observation, key) for key in ('machine_id', 'boot_id', 'mac_addresses', 'nonce')}
        program = ("import sys,time;sys.stdout.buffer.write(" + repr(json.dumps(public).encode() + b'\n')
            + ");sys.stdout.buffer.flush();sys.stdin.buffer.readline();sys.stdout.buffer.write("
            + repr(business_failure_frames(operation))
            + ");sys.stdout.buffer.flush();sys.stdout.close();time.sleep(.05);sys.exit(" + str(exit_code) + ")")
        real = h.SubprocessHostCommandRunner()
        seen = []
        def transport(_argv, *, environment, cwd, exchange, timeout):
            def wrapped(process):
                if short_write:
                    original = process.stdin
                    process.stdin = mock.Mock(wraps=original)
                    process.stdin.write.return_value = 0
                exchange(process)
                # Diagnostic EOF alone has not yet granted safe continuation.
                seen.append('EXCHANGE_RETURNED')
                self.assertFalse(fixture.provider._profile_operation_results[self.profile.profile].get('workload_process_completed'))
            result = real.run_guest_exchange([sys.executable, '-I', '-B', '-c', program],
                environment={'SYSTEMROOT': os.environ.get('SYSTEMROOT', 'C:/Windows')},
                cwd=cwd, exchange=wrapped, timeout=10, cancel_event=batch.cancelled)
            seen.append(('WAIT_RETURNED', result.returncode))
            return result
        with self.assertRaises(guest.ControllerFailure) as caught:
            with batch.operation('WORKLOAD', self.profile):
                use = batch.issue(self.profile, fixture.lease, ('CANDIDATE_WORKLOAD',))
                supervisor = guest._WorkloadSupervisor(use, provider=fixture.provider, plan=fixture.plan,
                    profile=self.profile, lease=fixture.lease,
                    preboot_disk_graph_digest=self.runtime.disk_graph_digest,
                    preboot_snapshot_identity=self.runtime.snapshot_identity)
                try:
                    with mock.patch.object(guest, '_root_program', return_value='synthetic'), \
                            mock.patch.object(guest, 'hold_windows_private_file', side_effect=lambda _: nullcontext()), \
                            mock.patch.object(fixture.fixture.runner, 'run_guest_exchange', side_effect=transport):
                        supervisor.execute()
                finally:
                    supervisor.close()
        return caught.exception, seen

    def test_complete_business_failure_waits_for_child_and_preserves_single_owner(self):
        error, seen = self.execute()
        self.assertIs(type(error), guest.WorkloadFailure, str(error))
        self.assertFalse(error.revoke_batch)
        self.assertEqual(seen, ['EXCHANGE_RETURNED', ('WAIT_RETURNED', 2)])
        self.assertFalse(self.owner.closed)
        self.assertFalse(self.batch.cancelled.is_set())
        self.assertTrue(self.secret)
        proof = self.fixture.provider._profile_operation_results[self.profile.profile]
        self.assertTrue(all(proof[key] for key in ('workload_process_completed',
            'workload_delivery_completed', 'workload_identity_rechecked', 'workload_supervisor_closed')))
        self.assertEqual(self.batch.record['profiles'][self.profile.profile]['CANDIDATE_WORKLOAD']['delivery_completed'], 1)
        self.assertEqual(self.owner.record['capture_completed'], 1)

    def test_ssh_exit_conflict_wipes_owner_without_reusing_role(self):
        error, seen = self.execute(exit_code=0)
        self.assertIn('EXIT_CONFLICT', str(error))
        self.assertEqual(seen[-1], ('WAIT_RETURNED', 0))
        self.assertTrue(self.owner.closed)
        self.assertEqual(self.secret, b'')
        self.assertTrue(self.batch.cancelled.is_set())

    def test_short_delivery_wipes_owner_and_closes_owned_child(self):
        error, seen = self.execute(short_write=True)
        self.assertTrue(self.owner.closed)
        self.assertEqual(self.secret, b'')
        self.assertEqual(seen, [])
        role = self.batch.record['profiles'][self.profile.profile]['CANDIDATE_WORKLOAD']
        self.assertEqual((role['delivery_attempts'], role['delivery_completed']), (1, 0))


@unittest.skipUnless(os.name == 'nt', 'actual Windows confirmed private scope')
class DiagnosticOwnerClosureTests(unittest.TestCase):
    def test_single_fresh_pass_finishes_its_confirmed_scope_and_wipes_owner(self):
        from scripts.tests import test_guest_batch_scope as fixtures
        from release.candidate_failure_policy import FAILURE_POLICY
        setup = fixtures.ConfirmedOwnerIntegrationTests()
        setup.setUp()
        self.addCleanup(setup.doCleanups)
        plan = replace(setup.plan, platform_diagnostic=True, profiles=setup.plan.profiles[:1], plan_digest='')
        plan = replace(plan, plan_digest=h.sha256_bytes(h.canonical_json_bytes(plan.identity_body())))
        # This is the synthetic native-consent fixture, never a persisted user scope.
        setup.authorization._body.update(initial_plan=plan.as_dict(), initial_plan_digest=plan.plan_digest)
        setup.fixture.plan = plan
        batch = batches.CandidateBatch(setup.fixture.provider, plan,
            authorization_id=setup.authorization.authorization_id, development_owner=setup.owner)
        self.addCleanup(batch.close)
        with batch.operation('BOOTSTRAP', plan.profiles[0]), redirect_stdout(io.StringIO()):
            batch.capture_after_bootstrap_observation(plan.profiles[0])
        roles = batch._record['profiles']['FRESH_BASE']
        self.assertEqual(set(batch._record['profiles']), {'FRESH_BASE'})
        for item in roles.values():
            item.update(delivery_attempts=1, delivery_completed=1, operation_result='PASS',
                target_verified=True, lease_verified=True)
        batch.close()
        report = dict(failure_policy=FAILURE_POLICY, credential_session=batch.record,
            source_preserved=True, cleanup_errors=[], private_material_root_released=True,
            private_execution_source_root_released=True,
            private_material_root=str(setup.root/'absent-material'),
            private_execution_source_root=str(setup.root/'absent-source'),
            profile_results={'FRESH_BASE': {'status': 'PASS'}}, status='PASS', all_profiles_pass=True,
            profile_operations={'FRESH_BASE': dict(power_state='STOPPED', clone_disposition='REMOVED',
                clone_vmx=str(setup.root/'removed-vm'), cleanup_errors=[], session_keys_removed=True,
                known_hosts_removed=True, lease_released=True)})
        setup.owner.finish_round(report)
        self.assertTrue(setup.owner.closed)
        self.assertEqual(setup.secret, b'')
        self.assertEqual(setup.owner.record['close_reason'], 'DEVELOPMENT_PLATFORM_DIAGNOSTIC_COMPLETED')
        self.assertEqual(setup.owner.record['capture_attempts'], 1)


class SharedBlockerTests(unittest.TestCase):
    def test_source_observation_failure_wipes_batch_before_origin_poststate(self):
        from types import SimpleNamespace
        from scripts.tests import test_candidate_batch_session as batch_fixtures
        from scripts.tests import test_candidate_vm_harness as fixtures
        setup = batch_fixtures.BatchSessionTests()
        setup.setUp()
        self.addCleanup(setup.doCleanups)
        setup.capture()
        provider, plan = setup.provider, setup.plan
        original = h.verify_candidate_r2_origin_from_environment
        checked = []
        def observe(**kwargs):
            if kwargs['observation_role'] == 'POSTSTATE':
                self.assertEqual(setup.secret, b'')
                self.assertTrue(setup.batch.cancelled.is_set())
                checked.append('POSTSTATE_AFTER_WIPE')
            return original(**kwargs)
        with mock.patch.object(provider, 'execute_profile', return_value={}) as execute, \
                mock.patch.object(provider, 'inspect_original_hashes', side_effect=[OSError('synthetic'), dict(plan.original_vm_hashes)]), \
                mock.patch.object(provider, 'inspect_candidate_external_state', return_value=h.EXPECTED_CANDIDATE_EXTERNAL_STATE), \
                mock.patch.object(h, 'verify_candidate_r2_origin_from_environment', side_effect=observe), \
                mock.patch('release.r2_prestate.R2_ACCOUNT_ID_SHA256', h.sha256_bytes(fixtures.ACCOUNT_ID.encode())):
            result = h._execute_harness_plan(plan, accepted_plan_digest=plan.plan_digest,
                provider=provider, environment=fixtures._r2_environment(), r2_client=fixtures.R2Client(),
                _loaded_candidate=SimpleNamespace(root=setup.ledger.parent, verified_digest=plan.verified_candidate_digest))
        self.assertEqual(checked, ['POSTSTATE_AFTER_WIPE'])
        execute.assert_called_once()
        self.assertEqual([x['status'] for x in result['profileResults'].values()],
            ['ERROR', 'NOT_RUN_SHARED_BLOCKER', 'NOT_RUN_SHARED_BLOCKER'])
        self.assertEqual(result['aggregateReceipt']['result'], 'FAIL')
