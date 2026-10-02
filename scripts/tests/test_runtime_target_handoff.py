"""Synthetic A/B input and holders only; real providers and E: IO are forbidden."""
import io
import json
import pickle
import socket
import subprocess
import tempfile
import time
import unittest
from contextlib import ExitStack, contextmanager, nullcontext, redirect_stdout
from dataclasses import replace
from pathlib import Path, PureWindowsPath
from types import SimpleNamespace
from unittest import mock

from installer import development_trust as trust
from scripts import candidate_vm_harness as h
from scripts import development_guest_session as guest
from scripts import development_session_owner as owners
from scripts import development_source
from scripts import guest_batch_scope as scopes
from scripts import local_candidate_development as cli
from scripts import runtime_target_handoff as target
from scripts.development_plan import from_material_plan
from scripts.guest_console_capture import ConsoleCaptureError, WindowsConsoleCapture
from scripts.guest_sudo_session import ControllerFailure


def digest(number):
    return 'sha256:' + f'{number:064x}'


def redigest(plan):
    return replace(plan, plan_digest=h.sha256_bytes(h.canonical_json_bytes(plan.identity_body())))


@contextmanager
def synthetic_provider_constructor(provider):
    """Replace only the CLI factory; never mutate the real type's __new__."""
    facade = SimpleNamespace(**vars(h))
    facade.ClosedVmwareProvider = mock.Mock(return_value=provider)
    with mock.patch.object(cli, 'h', facade):
        yield


class RuntimeTargetHandoffTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.events = []
        self.entry = self.root / ('a' * 64)
        self.batch_root = self.root / ('b' * 64)
        session = '1' * 32
        profiles = tuple(h.CandidateProfilePlan(name, 'OFFLINE_EXISTING_DOCKER',
            h.SNAPSHOT_ALLOWLIST[name], digest(index + 1), digest(index + 4), digest(index + 7),
            digest(10), session, '2' * 32, 'synthetic-host-alias') for index, name in enumerate(h.PROFILES))
        self.base = redigest(h.CandidateHarnessPlan(digest(11), digest(12), 1,
            'c' * 40, 'd' * 40, 'v2.0.0-rc.3', h.SOURCE_VM_IDENTITY,
            digest(13), digest(14), digest(15), {'Ubuntu 64 位.vmx': digest(16)},
            profiles, digest(10), session, ''))
        self.plan = from_material_plan(self.base, execution_source_sha='a' * 40,
            execution_source_tree='b' * 40, execution_inventory_digest=digest(17),
            runtime_offline_only=True, runtime_authorization_deadline='2098-01-01T00:00:00Z',
            runtime_trust_selection_digest=digest(18), runtime_retention_policy='STOP_AND_RETAIN')
        self.source = SimpleNamespace(root=self.root / 'source' / 'root',
            inventory_digest=self.plan.execution_inventory_digest, published_subject_digest=None)
        self.inputs = object.__new__(trust.RuntimeTrustInputs)
        self.inputs._raw, self.inputs.digest = b'synthetic-public-selection', digest(18)
        self.inputs._closed, self.inputs._holds = False, ExitStack()
        self.inputs.selection = {'expires_at': '2099-01-01T00:00:00Z', 'files': {},
            'product': {'payload': {'sha256': digest(19), 'size': 1},
                'release_attestation': {'sha256': digest(20), 'size': 1}}}
        self.provider = object.__new__(h.ClosedVmwareProvider)
        self.execution = SimpleNamespace(root=PureWindowsPath('E:/animemo-provider-execution-' + '3' * 32),
            work_root=PureWindowsPath('E:/animemo-provider-execution-' + '3' * 32 + '/private-work-' + '4' * 32))
        self.provider._execution = self.execution
        self.source._execution, self.source._closed = self.execution, False
        self.provider._development_source_authority = self.source
        self.provider._require_active_execution_authority = mock.Mock()
        self.provider._active_profile_authority = mock.Mock(side_effect=lambda profile, plan: SimpleNamespace(
            clone_vmx=self.provider._execution.work_root / plan.session_id / profile.profile.lower() / 'vm' / 'Ubuntu 64 位.vmx'))
        self.provider._runtime_development_material = self.inputs
        self.provider._profile_operation_results = {}
        self.provider._candidate_diagnostics = {}
        self.provider._host_lifecycle_observations = []
        self.provider.bind_candidate_material_authority = lambda material: nullcontext()
        self.provider.execute_development_profile = mock.Mock(side_effect=lambda **kw:
            self.events.append('VM_EXECUTION_REQUESTED_SYNTHETICALLY') or {'result': 'FAIL'})
        self.provider.inspect_original_hashes = mock.Mock(return_value=dict(self.plan.original_vm_hashes))
        self.args = SimpleNamespace(execute=True, confirm_batch=True, authorization_id='ANIMEMO_SYNTHETIC_TARGET_HANDOFF_V1',
            runtime_offline_only=True, runtime_target_handoff=True, result=self.root / 'report.json',
            execution_source_sha=self.plan.execution_source_sha, execution_source_tree=self.plan.execution_source_tree,
            material_source_sha=self.plan.source_sha, material_source_tree=self.plan.source_tree,
            verified_candidate_digest=self.plan.verified_candidate_digest, qualification_run_id=1,
            runtime_authorization_deadline=self.plan.runtime_authorization_deadline,
            runtime_trust_selection_digest=self.plan.runtime_trust_selection_digest,
            runtime_execution_inventory_digest=self.plan.execution_inventory_digest,
            runtime_guest_inventory_digest=self.inputs.guest_inventory_digest,
            runtime_trust_expiry=self.inputs.selection['expires_at'],
            runtime_trust_inputs=Path('E:/synthetic/trust'), runtime_portable=Path('E:/synthetic/portable.tar'),
            runtime_release_attestation=Path('E:/synthetic/attestation.json'))
        # Guards apply to every case. Explicit per-test synthetic seams below
        # never execute the original OS/native/private implementations.
        for owner, name in ((subprocess, 'Popen'), (socket, 'socket'),
                (h.ClosedVmwareProvider, '__init__'), (h.ClosedVmwareProvider, 'execution_authority'),
                (WindowsConsoleCapture, 'capture'), (WindowsConsoleCapture, 'confirm_batch'),
                (WindowsConsoleCapture, 'preflight'), (trust, 'read_runtime_trust_selection'),
                (target, 'create_windows_private_named_directory'), (target, 'hold_windows_private_path_chain'),
                (scopes, 'create_windows_private_named_directory'), (scopes, 'hold_windows_private_path_chain')):
            self.enterContext(mock.patch.object(owner, name, side_effect=AssertionError('FORBIDDEN_REAL_EFFECT:' + name)))
        original_open = Path.open
        def guarded_open(path, *args, **kwargs):
            if PureWindowsPath(str(path)).drive.upper() == 'E:':
                raise AssertionError('FORBIDDEN_E_DRIVE_IO')
            return original_open(path, *args, **kwargs)
        self.enterContext(mock.patch.object(Path, 'open', guarded_open))
        self.enterContext(mock.patch.object(target, 'ENTRY_ID', 'ANIMEMO_TEST_ONLY_TARGET_V1'))
        self.enterContext(mock.patch.object(target, 'ENTRY_ROOT', self.entry))
        self.enterContext(mock.patch.object(scopes, 'authorization_root', return_value=self.batch_root))
        self.enterContext(mock.patch.object(development_source, 'require_development_source', return_value=self.source))
        self.enterContext(mock.patch.object(trust.RuntimeTrustInputs, 'verify_current', autospec=True))

    def preparation(self, callback=None):
        def confirm(summary, **options):
            self.events.append('A_NATIVE')
            self.assertFalse(self.entry.exists())
            self.assertIn('private byte snapshot', summary)
            self.assertIn('id_ed25519', summary)
            self.assertIn('No clone/revert/boot', summary)
            self.assertGreater(options['timeout_seconds'], 0)
            self.assertLessEqual(options['timeout_seconds'], 60)
            if callback:
                callback()
        console = SimpleNamespace(confirm_batch=mock.Mock(side_effect=confirm))
        def create(parent, *, name):
            self.events.append('ENTRY_RESERVED')
            self.assertIn('A_NATIVE', self.events)
            path = parent / name
            path.mkdir()
            return path
        with mock.patch.object(target, 'WindowsConsoleCapture', return_value=console), \
                mock.patch.object(target, 'create_windows_private_named_directory', side_effect=create), \
                mock.patch.object(target, 'hold_windows_private_path_chain', side_effect=lambda *a, **k: nullcontext()):
            value = target.begin_preparation(self.args)
        self.addCleanup(value.close, {'status': 'SYNTHETIC_TEST_CLOSED'})
        return value, console

    def bound(self):
        value, _ = self.preparation()
        value.bind(self.provider, self.plan)
        return value

    def batch_confirmation(self, value, callback=None, holder=None):
        def confirm(summary, **options):
            self.events.append('B_NATIVE')
            self.batch_prompt = summary
            self.assertIn(value.record['target']['clone_vmx'], summary.replace('\\\\', '\\'))
            self.assertIn(self.plan.execution_source_sha, summary)
            self.assertIn(self.plan.execution_source_tree, summary)
            self.assertIn(self.plan.execution_inventory_digest, summary)
            self.assertIn(self.plan.session_id, summary)
            self.assertIn(self.args.runtime_trust_expiry, summary)
            self.assertLessEqual(len(summary), 12000)
            self.assertGreater(options['timeout_seconds'], 0)
            self.assertLessEqual(options['timeout_seconds'], 60)
            self.assertFalse(self.batch_root.exists())
            self.provider.execute_development_profile.assert_not_called()
            if callback:
                callback()
        console = SimpleNamespace(confirm_batch=mock.Mock(side_effect=confirm))
        def create(parent, *, name):
            path = parent / name
            path.mkdir()
            return path
        with mock.patch.object(scopes, 'WindowsConsoleCapture', return_value=console), \
                mock.patch.object(scopes, 'create_windows_private_named_directory', side_effect=create), \
                mock.patch.object(scopes, 'hold_windows_private_path_chain', side_effect=holder or (lambda *a, **k: nullcontext())):
            authorization = scopes.confirm_local_batch(authorization_id=self.args.authorization_id,
                purpose='LOCAL_INSTALLER_DEVELOPMENT', plan=self.plan, runtime_handoff=value)
        self.addCleanup(authorization.close)
        return authorization

    def test_A_rejection_has_no_entry_private_effect_or_provider(self):
        def cancel():
            raise ConsoleCaptureError('BATCH_CONFIRMATION_CANCELLED')
        with self.assertRaisesRegex(ConsoleCaptureError, 'CANCELLED'):
            self.preparation(cancel)
        self.assertEqual(self.events, ['A_NATIVE'])
        self.assertFalse(self.entry.exists())

    def test_fixed_entry_is_spent_even_when_plan_or_permit_changes(self):
        value, _ = self.preparation()
        value.close({'status': 'CANCELLED'})
        original = (self.entry / 'attempt.json').read_bytes()
        self.args.authorization_id = 'ANIMEMO_SYNTHETIC_CHANGED_LABEL_V1'
        self.args.execution_source_sha = 'e' * 40
        # The second native consent cannot restore this fixed task attempt.
        with mock.patch.object(target, 'WindowsConsoleCapture') as console, \
                mock.patch.object(target, 'create_windows_private_named_directory', side_effect=FileExistsError):
            with self.assertRaises(FileExistsError):
                target.begin_preparation(self.args)
            console.return_value.confirm_batch.assert_called_once()
        self.assertEqual((self.entry / 'attempt.json').read_bytes(), original)

    def test_spent_old_permit_fails_after_A_before_provider(self):
        self.batch_root.mkdir()
        with self.assertRaisesRegex(ControllerFailure, 'PERMIT_ALREADY_SPENT'):
            self.preparation()
        self.assertTrue((self.entry / 'attempt.json').is_file())
        self.provider.execute_development_profile.assert_not_called()

    def test_bound_target_is_exact_and_nonserializable(self):
        value = self.bound()
        body = value.require_binding(self.plan)
        expected = self.execution.work_root / self.plan.session_id / 'runtime_base_offline/vm/Ubuntu 64 位.vmx'
        self.assertEqual(body['clone_vmx'], str(expected))
        self.assertEqual(body['selection_digest'], self.inputs.digest)
        self.assertFalse(body['publish_authorized'])
        with self.assertRaises(TypeError):
            pickle.dumps(value)
        with self.assertRaisesRegex(ControllerFailure, 'ALREADY_BOUND'):
            value.bind(self.provider, self.plan)

    def test_wrong_requested_inventory_selection_guest_digest_or_expiry_rejects_binding(self):
        value, _ = self.preparation()
        for key in ('runtime_execution_inventory_digest', 'runtime_trust_selection_digest',
                'runtime_guest_inventory_digest', 'runtime_trust_expiry', 'execution_source_sha'):
            original = value._request[key]
            value._request[key] = digest(99) if 'digest' in key else 'changed'
            with self.subTest(key=key), self.assertRaisesRegex(ControllerFailure, 'REQUEST_CHANGED'):
                value.bind(self.provider, self.plan)
            value._request[key] = original
        self.assertFalse((self.entry / 'target-binding.json').exists())

    def test_target_context_plan_holders_and_clone_cannot_change(self):
        value = self.bound()
        with self.assertRaisesRegex(ControllerFailure, 'CONTEXT_CHANGED'):
            value.require_binding(replace(self.plan))
        self.provider._execution = SimpleNamespace(**vars(self.execution))
        with self.assertRaisesRegex(ControllerFailure, 'CONTEXT_CHANGED'):
            value.require_binding(self.plan)
        self.provider._execution = self.execution
        self.provider._active_profile_authority.side_effect = lambda *a: SimpleNamespace(clone_vmx=PureWindowsPath('E:/other.vmx'))
        with self.assertRaisesRegex(ControllerFailure, 'CLONE_PATH_CHANGED'):
            value.require_binding(self.plan)
        self.provider.execute_development_profile.assert_not_called()

    def test_direct_or_restored_Runtime_confirmation_cannot_bypass_handoff(self):
        for value in (None, self.plan.as_dict(), SimpleNamespace()):
            with self.subTest(value=type(value).__name__), self.assertRaises(ControllerFailure):
                scopes.confirm_local_batch(authorization_id=self.args.authorization_id,
                    purpose='LOCAL_INSTALLER_DEVELOPMENT', plan=self.plan, runtime_handoff=value)
        self.assertFalse(self.batch_root.exists())

    def test_B_cancellation_or_context_change_does_not_issue_scope_or_restore_attempt(self):
        value = self.bound()
        def change_context():
            self.provider._execution = SimpleNamespace(**vars(self.execution))
        with self.assertRaisesRegex(ControllerFailure, 'CONTEXT_CHANGED'):
            self.batch_confirmation(value, change_context)
        self.assertFalse(self.batch_root.exists())
        self.assertFalse(value.record['B_approved'])
        self.assertTrue(value.record['B_confirmation_attempted'])
        self.provider._execution = self.execution
        with self.assertRaisesRegex(ControllerFailure, 'ALREADY_CONSUMED'):
            value.confirmation(self.plan)

    def test_B_scope_contains_target_digest_and_keeps_existing_capture_limit(self):
        value = self.bound()
        authorization = self.batch_confirmation(value)
        self.assertEqual(authorization.body['runtime_target_binding_digest'],
            h.sha256_bytes(h.canonical_json_bytes(value.record['target'])))
        self.assertEqual(authorization.round_limit, 1)
        self.assertEqual(authorization.body['capture_limit'], 1)
        self.assertEqual(authorization.body['runtime_target_binding']['capture_limit'], 1)
        self.assertFalse(authorization._capture_consumed)
        value.require_execution_binding(self.provider, self.plan)
        with self.assertRaisesRegex(ControllerFailure, 'ALREADY_CONSUMED'):
            value.confirmation(self.plan)

    def test_B_scope_rejects_other_provider_or_closed_handoff_before_first_round(self):
        from scripts.candidate_batch_session import CandidateBatch
        value = self.bound()
        authorization = self.batch_confirmation(value)
        other = object.__new__(h.ClosedVmwareProvider)
        other._execution = SimpleNamespace(**vars(self.execution))
        owner = SimpleNamespace(_authorization=authorization, closed=False)
        with self.assertRaisesRegex(ControllerFailure, 'CONTEXT_CHANGED'):
            CandidateBatch(other, self.plan, authorization_id=self.args.authorization_id, development_owner=owner)
        value.close({'status': 'CANCELLED'})
        with self.assertRaisesRegex(ControllerFailure, 'EXPIRED_OR_CLOSED'):
            CandidateBatch(self.provider, self.plan, authorization_id=self.args.authorization_id, development_owner=owner)
        self.assertEqual(authorization._reservations, [])
        self.assertFalse(authorization._capture_consumed)

    def test_lock_safe_target_check_does_not_rehash_media_or_call_provider(self):
        value = self.bound()
        authorization = self.batch_confirmation(value)
        with mock.patch.object(trust.RuntimeTrustInputs, 'verify_current', side_effect=AssertionError('NO_IO_UNDER_LOCK')), \
                mock.patch.object(self.provider, '_active_profile_authority', side_effect=AssertionError('NO_PROVIDER_UNDER_LOCK')):
            authorization.require_runtime_target(self.provider, self.plan)
            authorization.require_open()
        self.assertEqual(authorization.deadline, min(authorization.deadline, value._deadline))

    def test_B_scope_constructor_failure_releases_holders_without_restoring_entry(self):
        value = self.bound()
        @contextmanager
        def holder(*args, **kwargs):
            try:
                yield
            finally:
                self.events.append('B_SCOPE_HOLDS_RELEASED')
        with mock.patch.object(target.RuntimeTargetHandoff, 'expiry_limits',
                new_callable=mock.PropertyMock, side_effect=ControllerFailure('SYNTHETIC_EXPIRY_RACE')), \
                self.assertRaisesRegex(ControllerFailure, 'SYNTHETIC_EXPIRY_RACE'):
            self.batch_confirmation(value, holder=holder)
        self.assertIn('B_SCOPE_HOLDS_RELEASED', self.events)
        self.assertTrue(self.entry.exists())
        self.assertTrue(self.batch_root.exists())
        self.provider.execute_development_profile.assert_not_called()

    def test_real_owner_finalizes_synthetic_success_after_provider_and_inputs_close(self):
        value = self.bound()
        authorization = self.batch_confirmation(value)
        owner = owners.acquire_confirmed_development_owner(authorization=authorization,
            material_identity=owners._material(self.plan))
        self.addCleanup(owner.dispose)
        secret = bytearray(b'SYNTHETIC_MEMORY_ONLY_NEVER_A_PASSWORD')
        owner._secret, owner._attempts, owner._completed = secret, 1, 1
        owner._index, owner._state = 1, 'CLEANUP_PENDING'
        owner._pending, owner._pending_plan = self.plan.session_id, self.plan.as_dict()
        roles = {name: {'delivery_attempts': 1, 'delivery_completed': 1, 'target_verified': True,
            'lease_verified': True, 'operation_result': 'PASS'}
            for name in ('BOOTSTRAP_ROTATION', 'VERIFIED_SUDO', 'CANDIDATE_WORKLOAD')}
        session = {'binding': {'session_id': self.plan.session_id},
            'development_owner_id': owner._id, 'development_capture_index': 1,
            'profiles': {'RUNTIME_BASE_OFFLINE': roles}}
        owner._pending_record = session
        report = {'status': 'PASS', 'all_profiles_pass': True, 'plan': self.plan.as_dict(),
            'credential_session': session, 'failure_policy': 'animemo.graded-profile-failure/v1',
            'source_preserved': True, 'cleanup_errors': [],
            'private_material_root_released': True, 'private_execution_source_root_released': True,
            'private_material_root': str(self.root / 'absent-material'),
            'private_execution_source_root': str(self.root / 'absent-source'),
            'profile_results': {'RUNTIME_BASE_OFFLINE': {'status': 'PASS'}},
            'profile_operations': {'RUNTIME_BASE_OFFLINE': {'power_state': 'STOPPED',
                'clone_disposition': 'RETAINED_STOPPED', 'clone_vmx': value.record['target']['clone_vmx'],
                'cleanup_errors': [], 'session_keys_removed': True, 'known_hosts_removed': True,
                'lease_released': True, 'retention_receipt': {'receiptDigest': digest(30)}}}}
        self.provider._execution, self.source._closed, self.inputs._closed = None, True, True
        with self.assertRaises(ControllerFailure):
            authorization.require_runtime_target(self.provider, self.plan)
        with self.assertRaises(ControllerFailure):
            authorization.reserve_round(self.plan)
        # Retained clone validation has its own tests. Here the real owner
        # finalizer and consent clock run after synthetic context release.
        with mock.patch('scripts.runtime_development_retention.validate_retained_operation'):
            cli._close_owned_session(report, owner)
        self.assertEqual(report['status'], 'PASS', report.get('cleanup_errors'))
        self.assertEqual(owner.record['close_reason'], 'DEVELOPMENT_PREACCEPTANCE_PASSED')
        self.assertEqual(owner.record['runtime_round_verified']['plan_digest'], self.plan.plan_digest)
        self.assertEqual(secret, b'')

    def test_wall_and_monotonic_expiry_and_closed_context_cannot_authorize_B(self):
        value = self.bound()
        for key, expired in (('_expires', time.time() - 1), ('_deadline', time.monotonic() - 1)):
            original = getattr(value, key)
            setattr(value, key, expired)
            with self.subTest(clock=key), self.assertRaisesRegex(ControllerFailure, 'EXPIRED_OR_CLOSED'):
                value.confirmation(self.plan)
            setattr(value, key, original)
        value.close({'status': 'CANCELLED'})
        self.assertTrue(value.record['holds_released'])
        with self.assertRaises(ControllerFailure):
            value.require_binding(self.plan)
        self.assertTrue((self.entry / 'attempt.json').is_file())

    def test_cancel_close_keeps_prepared_user_files_and_reports_hold_failure(self):
        prepared = self.root / 'operator-prepared-original'
        prepared.write_bytes(b'unchanged-user-data')
        value = self.bound()
        value._holds.callback(lambda: (_ for _ in ()).throw(OSError('synthetic hold close failed')))
        with self.assertRaises(OSError):
            value.close({'status': 'CANCELLED'})
        self.assertTrue(value.record['closed'])
        self.assertFalse(value.record['holds_released'])
        self.assertEqual(prepared.read_bytes(), b'unchanged-user-data')

    def test_nonexecute_Runtime_is_scope_only_without_provider_or_private_reads(self):
        self.args.execute, self.args.confirm_batch, self.args.authorization_id = False, False, None
        with mock.patch.object(cli, '_check_checkout'), mock.patch.object(cli, 'require_material_compatibility'):
            report = cli.run(self.args)
        self.assertEqual(report['status'], 'SCOPE_ONLY', report.get('failure_code'))
        self.assertFalse(report['private_inputs_verified'])
        self.assertNotIn('plan', report)
        self.assertFalse(self.entry.exists())

    def test_missing_handoff_and_external_owner_or_sink_fail_before_provider(self):
        self.args.runtime_target_handoff = False
        self.assertEqual(cli.run(self.args)['failure_code'], 'RUNTIME_TARGET_HANDOFF_REQUIRED')
        self.args.runtime_target_handoff = True
        for keyword in ({'confirmed_owner_sink': []}, {'session_owner': SimpleNamespace()}):
            report = cli.run(self.args, **keyword)
            self.assertEqual(report['status'], 'ERROR')
        self.assertFalse(self.entry.exists())

    def test_scope_command_uses_no_private_effects_or_native_UI(self):
        with mock.patch.object(target, 'scope_report', return_value={'status': 'SCOPE_ONLY'}), \
                redirect_stdout(io.StringIO()) as output:
            self.assertEqual(cli.main(['--runtime-target-handoff-scope']), 0)
        self.assertEqual(json.loads(output.getvalue()), {'status': 'SCOPE_ONLY'})

    def test_cli_A_rejection_never_creates_a_provider(self):
        fake = SimpleNamespace(confirm_batch=mock.Mock(side_effect=ConsoleCaptureError('BATCH_CONFIRMATION_CANCELLED')))
        with mock.patch.object(cli, '_check_checkout'), mock.patch.object(cli, 'require_material_compatibility'), \
                mock.patch.object(cli, 'WindowsConsoleCapture'), mock.patch.object(target, 'WindowsConsoleCapture', return_value=fake):
            report = cli.run(self.args)
        self.assertEqual(report['failure_code'], 'BATCH_CONFIRMATION_CANCELLED')
        self.assertFalse(self.entry.exists())
        self.assertIsNone(report['credential_session'])

    def test_full_cli_keeps_context_through_B_and_cancel_closes_without_VM(self):
        value, _ = self.preparation()
        @contextmanager
        def provider_context(**kwargs):
            self.assertTrue(kwargs['_retain_controller_data'])
            self.events.append('PROVIDER_ENTERED')
            try:
                yield
            finally:
                self.events.append('PROVIDER_CLOSED')
                self.provider._execution = None
        self.provider.execution_authority = provider_context
        material = SimpleNamespace(loaded=SimpleNamespace(root=self.root / 'material' / 'root'))
        def cancel_B(summary, **kwargs):
            self.assertIs(self.provider._execution, self.execution)
            self.assertIn(self.plan.session_id, summary)
            self.events.append('B_CANCELLED')
            raise ConsoleCaptureError('BATCH_CONFIRMATION_CANCELLED')
        with mock.patch.object(cli, '_check_checkout'), mock.patch.object(cli, 'require_material_compatibility'), \
                mock.patch.object(cli, 'WindowsConsoleCapture'), mock.patch.object(target, 'begin_preparation', return_value=value), \
                mock.patch.object(h, 'acquire_candidate_material_authority', return_value=nullcontext(material)), \
                mock.patch.object(cli, 'acquire_development_source', return_value=nullcontext(self.source)), \
                mock.patch.object(h, 'build_harness_plan', return_value=self.base), \
                mock.patch.object(trust, 'read_runtime_trust_selection', return_value=self.inputs), \
                mock.patch.object(guest, 'preflight_development_workload_commands', return_value={}), \
                mock.patch.object(scopes, 'WindowsConsoleCapture', return_value=SimpleNamespace(confirm_batch=cancel_B)), \
                synthetic_provider_constructor(self.provider):
            report = cli.run(self.args)
        self.assertEqual(report['failure_code'], 'BATCH_CONFIRMATION_CANCELLED')
        self.assertLess(self.events.index('PROVIDER_ENTERED'), self.events.index('B_CANCELLED'))
        self.assertLess(self.events.index('B_CANCELLED'), self.events.index('PROVIDER_CLOSED'))
        self.provider.execute_development_profile.assert_not_called()
        self.assertTrue(report['runtime_target_handoff']['holds_released'])
        self.assertTrue(report['runtime_trust_inputs_released'])
        self.assertFalse(report['runtime_target_handoff']['B_approved'])
        self.assertTrue(self.entry.exists())

    def test_fake_provider_constructs_after_cli_handoff_in_same_process(self):
        self.test_full_cli_keeps_context_through_B_and_cancel_closes_without_VM()
        from scripts.tests.test_candidate_vm_harness import (
            FakeWindowsPlatform,
            RecordingRunner,
        )
        # The class initializer guard remains mocked: allocation with the real
        # keyword-only constructor is the regression under test, not OS setup.
        with mock.patch.object(h.ClosedVmwareProvider, '__init__', return_value=None) as initialize:
            runner, platform = RecordingRunner(), FakeWindowsPlatform()
            provider = h.ClosedVmwareProvider(runner=runner, windows_platform=platform, environment={})
        self.assertIs(type(provider), h.ClosedVmwareProvider)
        initialize.assert_called_once_with(runner=runner, windows_platform=platform, environment={})
        self.assertEqual(runner.calls, [])

    def test_cleanup_diagnostic_exception_still_closes_owner_then_handoff(self):
        value, _ = self.preparation()
        value._holds.callback(lambda: self.events.append('HANDOFF_HOLDS_RELEASED'))
        @contextmanager
        def provider_context(**kwargs):
            try:
                yield
            finally:
                self.provider._execution = None
        self.provider.execution_authority = provider_context
        material = SimpleNamespace(loaded=SimpleNamespace(root=self.root / 'material' / 'root'))
        owner = SimpleNamespace(record={'state': 'OPEN'})
        owner.finish_round = mock.Mock()
        def make_owner(*, authorization, material_identity):
            def dispose():
                authorization.close()
                owner.record['state'] = 'CLOSED'
                self.events.append('OWNER_DISPOSED')
            owner.dispose = mock.Mock(side_effect=dispose)
            return owner
        def create(parent, *, name):
            path = parent / name
            path.mkdir()
            return path
        original_exists = Path.exists
        def exists(path):
            if path == self.source.root.parent:
                raise OSError('synthetic cleanup diagnostic failure')
            return original_exists(path)
        batch = SimpleNamespace(close=mock.Mock(), revoke=mock.Mock(), record={'profiles': {},
            'session_capture_attempts': 0, 'session_capture_completed': 0})
        with mock.patch.object(cli, '_check_checkout'), mock.patch.object(cli, 'require_material_compatibility'), \
                mock.patch.object(cli, 'WindowsConsoleCapture'), mock.patch.object(target, 'begin_preparation', return_value=value), \
                mock.patch.object(h, 'acquire_candidate_material_authority', return_value=nullcontext(material)), \
                mock.patch.object(cli, 'acquire_development_source', return_value=nullcontext(self.source)), \
                mock.patch.object(h, 'build_harness_plan', return_value=self.base), \
                mock.patch.object(trust, 'read_runtime_trust_selection', return_value=self.inputs), \
                mock.patch.object(guest, 'preflight_development_workload_commands', return_value={}), \
                mock.patch.object(scopes, 'WindowsConsoleCapture'), \
                mock.patch.object(scopes, 'create_windows_private_named_directory', side_effect=create), \
                mock.patch.object(scopes, 'hold_windows_private_path_chain', side_effect=lambda *a, **k: nullcontext()), \
                mock.patch.object(owners, 'acquire_confirmed_development_owner', side_effect=make_owner), \
                mock.patch.object(cli, 'CandidateBatch', return_value=batch), mock.patch.object(Path, 'exists', exists), \
                synthetic_provider_constructor(self.provider):
            report = cli.run(self.args)
        self.assertEqual(report['status'], 'ERROR')
        self.assertIn('DEVELOPMENT_CLEANUP_DIAGNOSTIC_FAILED', report['cleanup_errors'])
        owner.dispose.assert_called_once()
        self.assertLess(self.events.index('OWNER_DISPOSED'), self.events.index('HANDOFF_HOLDS_RELEASED'))
        self.assertTrue(report['runtime_target_handoff']['holds_released'])
        self.assertTrue(self.entry.exists())


if __name__ == '__main__':
    unittest.main()
