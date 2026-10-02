"""Synthetic A/B, baseline, zero-capture and cleanup fixtures; no real effects."""
import copy
import hashlib
import json
import threading
import unittest
from contextlib import ExitStack, nullcontext
from dataclasses import replace
from types import SimpleNamespace
from unittest import mock

from scripts import candidate_batch_session as batches
from scripts import guest_batch_scope as scopes
from scripts import local_candidate_development as cli
from scripts import runtime_baseline_only_policy as policy
from scripts.development_plan import from_material_plan
from scripts.guest_console_capture import ConsoleCaptureError
from scripts.guest_sudo_session import ControllerFailure
from scripts.tests import test_runtime_development_boundary as baseline_fixtures
from scripts.tests import test_runtime_target_handoff as target_fixtures


def complete_fixture():
    return {'status': 'FAIL', 'failure_code': 'RUNTIME_BASELINE_DIAGNOSTIC_COMPLETE',
        'source_preserved': True, 'cleanup_errors': [], 'all_profiles_pass': False,
        'candidate_acceptance_authority_granted': False, 'publish_authorized': False,
        'plan': {'runtimeBaselineOnly': True}, 'profile_reports': {},
        'batch_confirmation': {'capture_limit': 0}, 'baseline_authorization_closed': True,
        'private_material_root_released': True, 'private_execution_source_root_released': True,
        'runtime_trust_inputs_released': True,
        'runtime_target_handoff': {name: True for name in
            ('A_approved', 'B_approved', 'entry_spent', 'closed', 'holds_released')},
        'profile_operations': {'RUNTIME_BASE_OFFLINE': {
            'power_state': 'STOPPED', 'clone_disposition': 'QUARANTINED', 'cleanup_errors': [],
            'continuation_authorized': False, 'lease_released': True,
            'session_keys_removed': True, 'known_hosts_removed': True,
            'runtime_lifetime': {'closed': True},
            'runtime_baseline_diagnostic': {'baseline_validated': True, 'baseline_digest': 'sha256:' + 'a' * 64,
                'credential_capture_authorized': False, 'installer_authorized': False}}},
        'credential_session': {'session_capture_attempts': 0, 'session_capture_completed': 0,
            'development_capture_index': 1, 'profiles': {'RUNTIME_BASE_OFFLINE': {
                role: {'delivery_attempts': 0, 'delivery_completed': 0} for role in batches.ROLES}}},
        'profile_results': {'RUNTIME_BASE_OFFLINE': {'status': 'ERROR',
            'failure_code': 'RUNTIME_BASELINE_DIAGNOSTIC_COMPLETE'}}}


class BaselineOnlyScopeTests(unittest.TestCase):
    def setUp(self):
        self.fixture = f = target_fixtures.RuntimeTargetHandoffTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        f.plan = from_material_plan(f.base, execution_source_sha=f.plan.execution_source_sha,
            execution_source_tree=f.plan.execution_source_tree,
            execution_inventory_digest=f.plan.execution_inventory_digest,
            runtime_offline_only=True, runtime_baseline_only=True,
            runtime_authorization_deadline=f.plan.runtime_authorization_deadline,
            runtime_trust_selection_digest=f.plan.runtime_trust_selection_digest,
            runtime_retention_policy='STOP_AND_RETAIN')
        f.args.runtime_baseline_only, f.args.runtime_ui_retry, f.args.result_only = True, False, True
        self.enterContext(mock.patch.object(policy, 'ENTRY_ID', 'ANIMEMO_TEST_ONLY_BASELINE_V1'))
        self.enterContext(mock.patch.object(policy, 'PERMIT_ID', 'ANIMEMO_TEST_ONLY_BASELINE_RUN_V1'))
        f.args.authorization_id = policy.PERMIT_ID
        previous = {'failure_code': 'RUNTIME_BASELINE_HARD_PREREQUISITE_MISSING',
            'credential_session': {'session_capture_attempts': 0},
            'runtime_target_handoff': {name: True for name in
                ('A_approved', 'B_approved', 'entry_spent', 'closed', 'holds_released')}}
        self.receipt = f.root / 'prior-v2.json'
        raw = json.dumps(previous).encode()
        self.receipt.write_bytes(raw)
        for name, value in (('PREVIOUS_REPORT', self.receipt), ('PREVIOUS_REPORT_BYTES', len(raw)),
                ('PREVIOUS_REPORT_SHA256', hashlib.sha256(raw).hexdigest()), ('ENTRY_ROOT', f.entry)):
            self.enterContext(mock.patch.object(policy, name, value))
        self.enterContext(mock.patch.object(scopes, 'hold_windows_private_working_directory',
            side_effect=lambda *args, **kwargs: nullcontext()))

    def test_mode_is_bound_in_plan_and_cannot_select_other_profiles(self):
        f = self.fixture
        self.assertTrue(f.plan.identity_body()['runtimeBaselineOnly'])
        self.assertEqual(f.plan.identity_body()['sudoCaptureLimit'], 0)
        self.assertNotEqual(f.plan.plan_digest, target_fixtures.redigest(replace(f.plan, runtime_baseline_only=False)).plan_digest)
        with self.assertRaisesRegex(Exception, 'DEVELOPMENT_PLAN_BINDING_INVALID'):
            from_material_plan(f.base, execution_source_sha='a' * 40, execution_source_tree='b' * 40,
                execution_inventory_digest='sha256:' + 'a' * 64, runtime_baseline_only=True)

    def test_new_policy_cannot_reuse_v2_or_read_receipt_for_wrong_scope(self):
        f = self.fixture
        result = policy.request_diagnostic(f.args)
        self.assertEqual(result['capture_limit'], 0)
        self.assertEqual(result['round_limit'], 1)
        self.assertNotIn('UI_RETRY', result['entry_id'])
        for name, value in (('authorization_id', 'ANIMEMO_TEST_ONLY_OTHER_MODE_V1'),
                            ('runtime_ui_retry', True), ('result_only', False)):
            args = SimpleNamespace(**{**vars(f.args), name: value})
            with self.subTest(name=name), mock.patch.object(policy.Path, 'open',
                    side_effect=AssertionError('scope drift must precede receipt IO')):
                with self.assertRaisesRegex(ControllerFailure, 'FIXED_SCOPE_REQUIRED'):
                    policy.request_diagnostic(args)

    def test_changed_public_receipt_fails_before_A(self):
        self.receipt.write_bytes(self.receipt.read_bytes() + b' ')
        with self.assertRaisesRegex(ControllerFailure, 'RECEIPT_CHANGED'):
            self.fixture.preparation()
        self.assertFalse(self.fixture.entry.exists())
        self.assertEqual(self.fixture.events, [])

    def test_A_cancel_leaves_no_entry_or_provider(self):
        def cancel():
            raise ConsoleCaptureError('BATCH_CONFIRMATION_CANCELLED')
        with self.assertRaisesRegex(ConsoleCaptureError, 'CANCELLED'):
            self.fixture.preparation(cancel)
        self.assertFalse(self.fixture.entry.exists())
        self.assertEqual(self.fixture.events, ['A_NATIVE'])

    def test_B_cancel_retains_spent_A_entry_without_execution(self):
        f = self.fixture
        target = f.bound()
        def cancel():
            raise ConsoleCaptureError('BATCH_CONFIRMATION_CANCELLED')
        with self.assertRaisesRegex(ConsoleCaptureError, 'CANCELLED'):
            f.batch_confirmation(target, cancel)
        self.assertTrue(f.entry.exists())
        self.assertTrue(target.record['entry_spent'])
        self.assertFalse(target.record['B_approved'])
        f.provider.execute_development_profile.assert_not_called()

    def test_real_A_B_scope_reservation_and_batch_refuse_all_credential_paths(self):
        f = self.fixture
        target = f.bound()
        authorization = f.batch_confirmation(target)
        self.assertEqual(authorization.body['capture_limit'], 0)
        self.assertTrue(authorization.body['runtime_target_binding']['runtime_baseline_only'])
        self.assertFalse(authorization.body['runtime_target_binding']['installer_authorized'])
        with self.assertRaisesRegex(ControllerFailure, 'LOCAL_BATCH_AUTHORIZATION_INVALID'):
            authorization.consume_capture()
        def create(parent, *, name):
            path = parent / name
            path.mkdir()
            return path
        with mock.patch.object(scopes, 'create_windows_private_named_directory', side_effect=create):
            batch = batches.CandidateBatch(f.provider, f.plan,
                authorization_id=f.args.authorization_id, local_authorization=authorization)
        self.addCleanup(batch.close)
        f.provider._candidate_batch = batch
        self.assertEqual(batch.record['development_capture_index'], 1)
        for action in (lambda: batch.capture_after_bootstrap_observation(f.plan.profiles[0]),
                       lambda: batch.issue(f.plan.profiles[0], object(), batches.ROLES[:2])):
            with self.assertRaisesRegex(ControllerFailure, 'CREDENTIALS_FORBIDDEN'):
                action()
        with self.assertRaisesRegex(ControllerFailure, 'LOCAL_BATCH_AUTHORIZATION_INVALID'):
            authorization.reserve_round(f.plan)
        self.assertEqual(batch.record['session_capture_attempts'], 0)
        self.assertFalse((authorization.root / 'capture-attempt.json').exists())
        self.assertTrue(f.entry.exists())

    def test_removing_bound_baseline_mode_cannot_change_B_target(self):
        f = self.fixture
        target = f.bound()
        f.plan = target_fixtures.redigest(replace(f.plan, runtime_baseline_only=False))
        with self.assertRaisesRegex(ControllerFailure, 'RUNTIME_TARGET_CONTEXT_CHANGED'):
            target.require_binding(f.plan)

    def test_B_prompt_and_persisted_targets_agree_on_zero_capture(self):
        f = self.fixture
        target = f.bound()
        authorization = f.batch_confirmation(target)
        prompt = json.loads(f.batch_prompt[f.batch_prompt.index('{'):].split('\nProfiles:', 1)[0])
        self.assertIn('Zero password captures', f.batch_prompt)
        self.assertEqual(prompt['capture_limit'], 0)
        authorization.close()
        target.close({'status': 'SYNTHETIC_TEST_CLOSED'})
        completion = json.loads((f.entry / 'completion.json').read_text())
        targets = (target.record['target'], authorization.body['runtime_target_binding'],
            prompt['runtime_target_binding'], json.loads((f.entry / 'target-binding.json').read_text()),
            json.loads((f.batch_root / 'scope.json').read_text())['runtime_target_binding'],
            completion['handoff']['target'])
        for value in targets:
            self.assertEqual(value['capture_limit'], 0)
            self.assertFalse(value['credential_capture_authorized'])
            self.assertFalse(value['installer_authorized'])


class BaselineOnlyTerminationTests(unittest.TestCase):
    def test_public_checkout_refuses_live_operator_routes_before_any_receipt_io(self):
        from scripts import runtime_ui_retry_policy as retry
        from pathlib import Path
        routes = ((policy.request_diagnostic, SimpleNamespace(runtime_baseline_only=True)),
            (retry.request_retry, SimpleNamespace(runtime_ui_retry=True)),
            (target_fixtures.target._request, SimpleNamespace()))
        with mock.patch.object(Path, 'open', side_effect=AssertionError('public checkout must not read operator files')):
            for route, args in routes:
                with self.subTest(route=route.__name__):
                    with self.assertRaisesRegex(ControllerFailure, 'OPERATOR_BINDING_REQUIRED'):
                        route(args)
        self.assertFalse(retry.public_policy()['runtime_execution_available'])

    def test_real_handoff_cleanup_reports_final_outcome_and_nonfinal_receipt(self):
        for failure in (None, 'receipt_write', 'holder_close'):
            with self.subTest(failure=failure), ExitStack() as seams:
                scope = BaselineOnlyScopeTests()
                scope.setUp()
                seams.callback(scope.doCleanups)
                f = scope.fixture
                target = f.bound()
                authorization = f.batch_confirmation(target)
                if failure == 'receipt_write':
                    seams.enter_context(mock.patch.object(target_fixtures.target, '_write',
                        side_effect=OSError('synthetic receipt write failure')))
                elif failure == 'holder_close':
                    def fail_close():
                        raise OSError('synthetic holder release failure')
                    target._holds.callback(fail_close)
                def execute(args, result, cleanup, **kwargs):
                    result.update(complete_fixture())
                    cleanup.callback(cli._finish_handoff, result, target)
                    cleanup.callback(cli._close_baseline_authorization, result, authorization)
                with mock.patch.object(cli, '_run', side_effect=execute):
                    report = cli.run(SimpleNamespace(runtime_baseline_only=True))
                self.assertFalse(report['all_profiles_pass'])
                self.assertFalse(report['publish_authorized'])
                self.assertEqual(report['runtime_target_handoff']['holds_released'], failure != 'holder_close')
                if failure is None:
                    self.assertEqual(report['status'], 'DIAGNOSTIC_COMPLETE')
                    self.assertNotIn('failure_code', report)
                else:
                    self.assertEqual(report['status'], 'ERROR')
                    self.assertEqual(report['failure_code'], 'RUNTIME_BASELINE_ONLY_COMPLETION_UNVERIFIED')
                    self.assertIn('RUNTIME_TARGET_HANDOFF_CLEANUP_FAILED', report['cleanup_errors'])
                if failure != 'receipt_write':
                    receipt = json.loads((f.entry / 'completion.json').read_text())
                    self.assertEqual(receipt['phase'], 'BEFORE_HOLD_RELEASE')
                    self.assertFalse(receipt['final_result'])
                    self.assertFalse(receipt['handoff']['holds_released'])

    def test_actual_baseline_success_rechecks_identity_then_unwinds_before_capture_or_workload(self):
        helper, record = baseline_fixtures.RuntimeBaselineTests(), {}
        events, failure = helper._run_baseline_capture(baseline_only=True, record_sink=record,
            expected_failure='RUNTIME_BASELINE_DIAGNOSTIC_COMPLETE')
        self.assertEqual(events, ['fixed-observer'])
        self.assertIsNone(failure)
        diagnostic = record['profile_operations']['RUNTIME_BASE_OFFLINE']['runtime_baseline_diagnostic']
        self.assertTrue(diagnostic['baseline_validated'])
        self.assertFalse(diagnostic['credential_capture_authorized'])
        self.assertFalse(diagnostic['installer_authorized'])

    def test_actual_missing_prerequisite_keeps_failure_and_diagnostics_with_zero_capture(self):
        helper = baseline_fixtures.RuntimeBaselineTests()
        events, diagnostic = helper._run_baseline_capture(baseline_only=True,
            baseline_mutation=lambda value: value.update(hard_missing=['DOCKER_SERVICE_INACTIVE'],
                                                         docker_service_active=False),
            expected_failure='RUNTIME_BASELINE_HARD_PREREQUISITE_MISSING')
        self.assertEqual(events, ['fixed-observer'])
        self.assertEqual(diagnostic['hard_missing'], ['DOCKER_SERVICE_INACTIVE'])

    def test_wrapper_classifies_completion_only_after_exitstack_cleanup(self):
        value = complete_fixture()
        value['baseline_authorization_closed'] = False
        def execute(args, result, cleanup, **kwargs):
            result.update(copy.deepcopy(value))
            cleanup.callback(result.update, baseline_authorization_closed=True)
        with mock.patch.object(cli, '_run', side_effect=execute):
            report = cli.run(SimpleNamespace(runtime_baseline_only=True))
        self.assertEqual(report['status'], 'DIAGNOSTIC_COMPLETE')
        self.assertFalse(report['all_profiles_pass'])
        self.assertFalse(report['publish_authorized'])
        self.assertNotIn('failure_code', report)

    def test_incomplete_stop_cleanup_capture_or_validation_cannot_claim_completion(self):
        mutations = (
            lambda value: value.update(private_execution_source_root_released=False),
            lambda value: value.update(baseline_authorization_closed=False),
            lambda value: value['runtime_target_handoff'].update(holds_released=False),
            lambda value: value['batch_confirmation'].update(capture_limit=1),
            lambda value: value['credential_session'].update(session_capture_attempts=1),
            lambda value: value['credential_session'].update(development_capture_index=0),
            lambda value: value['profile_operations']['RUNTIME_BASE_OFFLINE'].update(power_state='RUNNING'),
            lambda value: value['profile_operations']['RUNTIME_BASE_OFFLINE'].update(lease_released=False),
            lambda value: value['profile_operations']['RUNTIME_BASE_OFFLINE']['runtime_baseline_diagnostic'].update(
                baseline_validated=False),
            lambda value: value['profile_operations']['RUNTIME_BASE_OFFLINE']['runtime_lifetime'].update(closed=False),
            lambda value: value['credential_session']['profiles']['RUNTIME_BASE_OFFLINE'][batches.ROLES[0]].update(
                delivery_completed=1),
            lambda value: value.update(profile_reports={'RUNTIME_BASE_OFFLINE': {'result': 'PASS'}}),
        )
        for index, mutate in enumerate(mutations):
            value = complete_fixture()
            mutate(value)
            with self.subTest(index=index):
                cli._finish_baseline_diagnostic(value)
                self.assertEqual(value['status'], 'ERROR')
                self.assertEqual(value['failure_code'], 'RUNTIME_BASELINE_ONLY_COMPLETION_UNVERIFIED')
                self.assertIn('RUNTIME_BASELINE_ONLY_COMPLETION_UNVERIFIED', value['cleanup_errors'])
                self.assertFalse(value['all_profiles_pass'])

    def test_failed_baseline_is_never_promoted_by_diagnostic_mode(self):
        value = complete_fixture()
        value['failure_code'] = 'RUNTIME_BASELINE_HARD_PREREQUISITE_MISSING'
        cli._finish_baseline_diagnostic(value)
        self.assertEqual(value['status'], 'FAIL')
        self.assertEqual(value['failure_code'], 'RUNTIME_BASELINE_HARD_PREREQUISITE_MISSING')
        self.assertFalse(value['all_profiles_pass'])

    def test_zero_capture_scope_cannot_be_changed_to_a_privileged_owner(self):
        batch = object.__new__(batches.CandidateBatch)
        batch._lock = threading.RLock()
        batch.plan = SimpleNamespace(runtime_baseline_only=True)
        with mock.patch.object(batches, 'WindowsConsoleCapture',
                side_effect=AssertionError('native capture forbidden')):
            with self.assertRaisesRegex(ControllerFailure, 'CREDENTIALS_FORBIDDEN'):
                batch.capture_after_bootstrap_observation(object())
            with self.assertRaisesRegex(ControllerFailure, 'CREDENTIALS_FORBIDDEN'):
                batch.issue(object(), object(), batches.ROLES)


if __name__ == '__main__':
    unittest.main()
