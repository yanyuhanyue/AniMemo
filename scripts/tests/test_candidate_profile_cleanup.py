"""Provider failure cleanup using disposable synthetic files, without VMs."""
from __future__ import annotations

import io
import json
from contextlib import ExitStack, nullcontext, redirect_stdout, redirect_stderr
from types import SimpleNamespace
import unittest
from unittest import mock

from scripts import candidate_guest_session as session
from scripts import candidate_vm_harness as h
from scripts.tests import test_candidate_vm_harness as fixtures


class ProfileCleanupTests(unittest.TestCase):
    def setUp(self):
        fixture = fixtures.CandidateVmHarnessTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        self.fixture = fixture
        self.plan = fixture._plan()
        self.profile = self.plan.profiles[0]
        self.authority = fixture._temporary_authority(h.ClosedVmwareProvider._profile_authority(self.profile, self.plan))
        self.provider = h.ClosedVmwareProvider(runner=fixtures.RecordingRunner(),
            windows_platform=fixtures.FakeWindowsPlatform(), environment={})
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        def patch(name, **kwargs):
            return self.stack.enter_context(mock.patch.object(self.provider, name, **kwargs))
        patch('_assert_tools')
        patch('inspect_readiness', return_value=fixture.provider.inspect_readiness())
        patch('_hashes', return_value=dict(self.plan.original_vm_hashes))
        patch('_active_profile_authority', return_value=self.authority)
        patch('_acquire_provider_lease', return_value=mock.sentinel.lease)
        self.release = patch('_release_provider_lease')
        self.running = patch('_is_running', return_value=False)
        self.contain = patch('_contain_clone', return_value='STOPPED')
        self.stop = patch('_stop_clone')
        self.remove = patch('_remove_clone')
        self.prepare = patch('_prepare_and_start_profile_clone', side_effect=self.prepared)
        self.bootstrap = self.stack.enter_context(mock.patch.object(session, 'bootstrap_candidate',
            side_effect=session.ControllerFailure('SYNTHETIC_BOOTSTRAP_FAILURE')))
        self.workload = self.stack.enter_context(mock.patch.object(session, 'execute_candidate_workload'))

    def prepared(self, **kwargs):
        self.authority.ssh_root.mkdir(parents=True)
        self.authority.clone_root.mkdir(parents=True)
        for path in (self.authority.identity_file, self.authority.identity_file.with_suffix('.pub'),
                     self.authority.known_hosts_file):
            path.write_bytes(b'synthetic-noncredential-fixture')
        self.authority.clone_vmx.write_bytes(b'synthetic-not-a-virtual-machine')
        return 'sha256:' + 'a' * 64, self.profile.snapshot_identity

    def execute(self):
        return self.provider.execute_profile(plan=self.profile, harness_plan=self.plan,
            candidate_root=self.fixture.root, initial_platform_state=h._initial_platform_state(self.profile.profile))

    def assert_no_keys(self):
        self.assertFalse(self.authority.identity_file.exists())
        self.assertFalse(self.authority.identity_file.with_suffix('.pub').exists())
        self.assertFalse(self.authority.known_hosts_file.exists())

    def test_hold_close_failure_preserves_initial_failure_and_still_clears_keys(self):
        def fail(**kwargs):
            self.prepared(**kwargs)
            def close_failure(): raise RuntimeError('synthetic private close failure')
            kwargs['profile_authority_stack'].callback(close_failure)
            raise h.CandidateHarnessError('CANDIDATE_VM_CLONE_REVERT_FAILED')
        self.prepare.side_effect = fail
        with self.assertRaisesRegex(h.CandidateHarnessError, '^CANDIDATE_VM_CLONE_REVERT_FAILED$'):
            self.execute()
        self.assert_no_keys()
        self.assertTrue(self.authority.clone_vmx.exists())
        operation = self.provider._profile_operation_results[self.profile.profile]
        self.assertEqual(operation['operation_failure_code'], 'CANDIDATE_VM_CLONE_REVERT_FAILED')
        self.assertEqual(operation['cleanup_errors'][0]['step'], 'profile_holds')
        self.assertTrue(operation['lease_released'])

    def test_containment_failure_does_not_skip_key_or_lease_cleanup(self):
        self.running.return_value = True
        self.contain.side_effect = h.CandidateHarnessError('CANDIDATE_VM_STOP_FAILED')
        with self.assertRaisesRegex(h.CandidateHarnessError, '^SYNTHETIC_BOOTSTRAP_FAILURE$'):
            self.execute()
        self.assert_no_keys()
        operation = self.provider._profile_operation_results[self.profile.profile]
        self.assertEqual(operation['cleanup_errors'][0]['step'], 'containment')
        self.assertEqual(operation['clone_disposition'], 'RETAINED_CLEANUP_BLOCKED')
        self.assertTrue(operation['lease_released'])

    def test_uncertain_vmware_start_requires_containment_even_when_list_is_empty(self):
        def fail(**kwargs):
            self.prepared(**kwargs)
            raise h.VmHostCommandError('CANDIDATE_VM_CLONE_START_FAILED',
                {'kind': 'TIMEOUT', 'operation': 'start', 'timeout': True, 'returncode': None})
        self.prepare.side_effect = fail
        self.running.return_value = False
        with self.assertRaisesRegex(h.CandidateHarnessError, '^CANDIDATE_VM_CLONE_START_FAILED$'):
            self.execute()
        self.contain.assert_called_once_with(self.authority.clone_vmx)
        self.assertEqual(self.provider._profile_operation_results[self.profile.profile]['power_state'], 'STOPPED')
        self.assert_no_keys()

    def test_key_cleanup_failure_still_removes_known_hosts_and_releases_lease(self):
        with mock.patch.object(self.provider, '_destroy_session_key',
                side_effect=h.CandidateHarnessError('CANDIDATE_VM_SESSION_KEY_DELETION_FAILED')):
            with self.assertRaisesRegex(h.CandidateHarnessError, '^SYNTHETIC_BOOTSTRAP_FAILURE$'):
                self.execute()
        self.assertFalse(self.authority.known_hosts_file.exists())
        self.release.assert_called_once()
        self.assertTrue(self.authority.clone_vmx.exists())

    def test_late_lease_release_failure_revokes_an_earlier_safe_continuation(self):
        continuation = h.ProfileContinuationReceipt.issue(profile=self.profile.profile,
            session_id=self.plan.session_id, original_vm_hashes=dict(self.plan.original_vm_hashes),
            active_profile_root_count=0, session_private_key_count=0, known_hosts_file_count=0,
            running_vm_count=0, quarantine_present=True, continuation_safe=True)
        self.release.side_effect = h.CandidateHarnessError('CANDIDATE_VM_PROVIDER_SESSION_RELEASE_FAILED')
        with mock.patch.object(self.provider, 'inspect_profile_continuation', return_value=continuation):
            with self.assertRaisesRegex(h.CandidateHarnessError, '^SYNTHETIC_BOOTSTRAP_FAILURE$') as caught:
                self.execute()
        self.assertNotIsInstance(caught.exception, h.CandidateProfileExecutionError)
        self.assertEqual(self.provider._profile_operation_results[self.profile.profile]['cleanup_errors'][-1]['step'], 'lease')

    def test_reported_fail_preserves_stopped_clone_without_success_deletion(self):
        self.bootstrap.side_effect = None
        self.bootstrap.return_value = mock.sentinel.verified
        self.workload.return_value = {'result': 'FAIL'}
        self.assertEqual(self.execute(), {'result': 'FAIL'})
        self.remove.assert_not_called()
        self.stop.assert_called_once()
        self.assertTrue(self.authority.quarantine_root.is_dir())
        self.assertFalse(self.authority.profile_root.exists())
        self.assertEqual(self.provider._profile_operation_results[self.profile.profile]['clone_disposition'], 'QUARANTINED')

    def test_main_retains_failed_work_root_and_preserves_progress_on_poststate_failure(self):
        plan = SimpleNamespace(plan_digest='sha256:' + 'a' * 64, as_dict=lambda: {'syntheticPlan': True})
        output = self.fixture.root / 'result.json'
        def failed(*args, **kwargs):
            self.provider._candidate_acceptance_progress = {'profileReceipts': {'FRESH_BASE': {'synthetic': True}}}
            raise h.CandidateHarnessError('R2_PLUGIN_RESPONSE_TIMEOUT')
        with (mock.patch.object(h, 'ClosedVmwareProvider', return_value=self.provider),
              mock.patch.object(self.provider, 'execution_authority', return_value=nullcontext()) as authority,
              mock.patch.object(h, 'build_harness_plan', return_value=plan),
              mock.patch.object(h, 'execute_harness_plan', side_effect=failed),
              mock.patch.object(h, 'acquire_candidate_material_authority', return_value=nullcontext()),
              mock.patch('scripts.isolated_guest_validation._check_checkout'),
              mock.patch('scripts.guest_console_capture.WindowsConsoleCapture.preflight'),
              redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO())):
            code = h.main(['--execute', '--authorization-id', session.CAPTURE_AUTHORIZATION,
                '--r2-origin-transport', 's3', '--result', str(output),
                '--verified-candidate-digest', self.plan.verified_candidate_digest,
                '--expected-qualification-run-id', str(self.plan.qualification_run_id),
                '--expected-source-sha', self.plan.source_sha, '--expected-source-tree', self.plan.source_tree])
        self.assertEqual(code, 2)
        authority.assert_called_once_with(_retain_controller_data=True)
        result = json.loads(output.read_text(encoding='utf-8'))
        self.assertEqual(result['failure_code'], 'R2_PLUGIN_RESPONSE_TIMEOUT')
        self.assertEqual(result['profileReceipts'], {'FRESH_BASE': {'synthetic': True}})


if __name__ == '__main__':
    unittest.main()
