"""Single Runtime DEV consent: synthetic secrets/transport, no Guest or native input."""
import io
import importlib.util
import json
import re
import sys
import tempfile
import unittest
from contextlib import ExitStack, contextmanager, nullcontext, redirect_stdout
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from scripts import candidate_batch_session as batches
from scripts import candidate_guest_session as guests
from scripts import candidate_vm_harness as h
from scripts import development_plan as plans
from scripts import development_session_owner as owners
from scripts import guest_batch_scope as scopes
from scripts.tests import test_local_candidate_development as fixtures


@contextmanager
def synthetic_runtime_directory():
    """Keep real consent/owner gates; substitute only private Windows IO."""
    from scripts import development_capture_scope
    original_temporary = tempfile.TemporaryDirectory
    with original_temporary() as directory, ExitStack() as seams:
        root = Path(directory).resolve()
        def checked(path):
            path = Path(path)
            if not path.is_absolute() or not path.resolve().is_relative_to(root):
                raise AssertionError('PRIVATE_BOUNDARY_OUTSIDE_TEMPORARY_ROOT')
            if path.is_symlink() or path.is_junction():
                raise OSError('SYNTHETIC_PRIVATE_BOUNDARY_REBOUND')
            return path
        def temporary(*args, **kwargs):
            kwargs['dir'] = root
            return original_temporary(*args, **kwargs)
        def create(parent, *, name):
            if re.fullmatch('[0-9a-f]{64}', name) is None:
                raise OSError('SYNTHETIC_PRIVATE_NAME_INVALID')
            path = checked(parent) / name
            path.mkdir()
            return path
        @contextmanager
        def hold(path, **kwargs):
            path = checked(path)
            if not path.is_dir():
                raise OSError('SYNTHETIC_PRIVATE_DIRECTORY_REQUIRED')
            yield path
        seams.enter_context(mock.patch.object(tempfile, 'TemporaryDirectory', side_effect=temporary))
        seams.enter_context(mock.patch.object(h, 'VM_WORK_PARENT', root / 'vm-work'))
        for module in (scopes, development_capture_scope):
            seams.enter_context(mock.patch.object(module, 'create_windows_private_named_directory', side_effect=create))
            seams.enter_context(mock.patch.object(module, 'hold_windows_private_path_chain', side_effect=hold))
            seams.enter_context(mock.patch.object(module, 'hold_windows_private_working_directory', side_effect=hold))
        yield str(root)


def synthetic_target_for_owner_tests(plan, authorization_id):
    """Owner budgets are separate from the live binding tests; no native A or E: inputs."""
    import time

    from scripts.runtime_development_boundary import parse_authorization_deadline
    from scripts.runtime_target_handoff import ENTRY_ID, RuntimeTargetHandoff
    target = object.__new__(RuntimeTargetHandoff)
    target._request = {'entry_id': ENTRY_ID}
    target.confirmation = mock.Mock(return_value=({'authorization_id': authorization_id}, 60))
    target.accept_confirmation = mock.Mock()
    target.require_frozen_target = mock.Mock()
    target.require_consent_open = mock.Mock()
    target.require_active_target = mock.Mock()
    target._expires = parse_authorization_deadline(plan.runtime_authorization_deadline)
    target._deadline = time.monotonic() + target._expires - time.time()
    return target


def redigest(plan):
    return replace(plan, plan_digest=h.sha256_bytes(h.canonical_json_bytes(plan.identity_body())))


class SingleRuntimePlanTests(unittest.TestCase):
    def setUp(self):
        fixture = fixtures.DevelopmentPlanTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.base = redigest(replace(fixture.fixture.plan, candidate_version='v2.0.0-rc.3'))
        self.inputs = {'execution_source_sha': 'a' * 40, 'execution_source_tree': 'b' * 40,
            'execution_inventory_digest': 'sha256:' + 'c' * 64}
        self.default = plans.from_material_plan(self.base, **self.inputs)
        self.runtime_inputs = {'runtime_authorization_deadline': '2099-01-01T00:00:00Z',
            'runtime_trust_selection_digest': 'sha256:' + 'd' * 64,
            'runtime_retention_policy': 'STOP_AND_RETAIN'}
        self.plan = plans.from_material_plan(self.base, **self.inputs, runtime_offline_only=True,
                                            **self.runtime_inputs)

    def test_explicit_selection_preserves_original_identity_and_default_three(self):
        self.assertEqual(self.plan.profiles, self.base.profiles[2:])
        self.assertEqual(self.plan.session_id, self.base.session_id)
        self.assertEqual(scopes.material_identity(self.plan), scopes.material_identity(self.base))
        self.assertEqual(self.default.profiles, self.base.profiles)
        self.assertNotIn('runtimeOfflineOnly', self.default.identity_body())
        self.assertNotEqual(self.default.plan_digest, self.plan.plan_digest)
        self.assertTrue(self.plan.identity_body()['runtimeOfflineOnly'])
        self.assertEqual(self.plan.identity_body()['developmentMode'], 'CLEAN_PREACCEPTANCE')
        scopes._require_plan('LOCAL_INSTALLER_DEVELOPMENT', self.plan)

    def test_conflicts_and_non_boolean_selection_are_rejected(self):
        for flags in ({'platform_diagnostic': True}, {'published_subject_digest': 'sha256:' + 'd' * 64},
                      {'userspace_probe_digest': 'sha256:' + 'd' * 64}):
            with self.subTest(flags=flags), self.assertRaises(h.CandidateHarnessError):
                plans.from_material_plan(self.base, **self.inputs, runtime_offline_only=True, **flags)
        for value in (1, 'true', None):
            with self.subTest(value=value), self.assertRaises(h.CandidateHarnessError):
                plans.from_material_plan(self.base, **self.inputs, runtime_offline_only=value)

    def test_selection_digest_and_purpose_tampering_are_rejected(self):
        candidates = [replace(self.plan, runtime_offline_only=False),
            redigest(replace(self.plan, profiles=self.base.profiles[:1])),
            redigest(replace(self.plan, profiles=self.base.profiles)),
            redigest(replace(self.plan, platform_diagnostic=True)),
            replace(self.plan, runtime_offline_only=1)]
        for plan in candidates:
            with self.subTest(plan=plan.plan_digest), self.assertRaises(scopes.ControllerFailure):
                scopes._require_plan('LOCAL_INSTALLER_DEVELOPMENT', plan)
        for purpose in ('CANDIDATE_ACCEPTANCE', 'FORMAL_POSTPUBLICATION'):
            with self.subTest(purpose=purpose), self.assertRaises(scopes.ControllerFailure):
                scopes._require_plan(purpose, self.plan)
        with self.assertRaises(scopes.ControllerFailure):
            scopes._require_plan('CANDIDATE_ACCEPTANCE', redigest(replace(self.base, profiles=self.base.profiles[2:])))

    def test_single_confirmation_rejects_multiple_rounds_before_ui(self):
        with mock.patch.object(scopes.WindowsConsoleCapture, 'confirm_batch') as confirm:
            with self.assertRaises(scopes.ControllerFailure):
                scopes.confirm_local_batch(authorization_id='ANIMEMO_SYNTHETIC_SINGLE_RUNTIME_V1',
                    purpose='LOCAL_INSTALLER_DEVELOPMENT', plan=self.plan, round_limit=2)
            confirm.assert_not_called()

    def test_guest_binding_cannot_select_online_profile_or_other_mode(self):
        from scripts import development_profile_runner as runner
        from scripts.development_guest_session import development_binding
        binding = development_binding(self.plan)
        runner.validate_binding(binding, profile='RUNTIME_BASE_OFFLINE')
        for profile in ('FRESH_BASE', 'DOCKER_BASE'):
            with self.subTest(profile=profile), mock.patch.object(runner, 'load_verified_candidate') as load:
                with self.assertRaises(runner.runner.ProfileRunnerError):
                    runner.execute_development_profile(binding=binding, profile=profile, context_b64url='')
                load.assert_not_called()
        for field, value in (('runtime_offline_only', False), ('runtime_offline_only', 1),
                             ('workload_mode', 'PLATFORM_DIAGNOSTIC')):
            with self.subTest(field=field, value=value), self.assertRaises(runner.runner.ProfileRunnerError):
                runner.validate_binding({**binding, field: value}, profile='RUNTIME_BASE_OFFLINE')

    def test_root_rejects_profile_context_substitution_before_any_write(self):
        from scripts import development_workload_root as root
        from scripts.development_guest_session import development_binding
        binding = development_binding(self.plan)
        with (mock.patch.object(root.os, 'geteuid', return_value=0, create=True),
              mock.patch.object(root.os, 'umask') as write,
              mock.patch.object(root, '_seal_development_tree') as seal):
            for profile, context in (('FRESH_BASE', {'profile': 'RUNTIME_BASE_OFFLINE'}),
                                     ('RUNTIME_BASE_OFFLINE', {'profile': 'DOCKER_BASE'})):
                with self.subTest(profile=profile), self.assertRaisesRegex(ValueError, 'ROOT_SCOPE_INVALID|RUNTIME_BOUNDARY_REQUIRED'):
                    root.run_fixed_development(profile=profile, input_digest=self.plan.candidate_input_digest,
                        material_inventory_digest='sha256:' + 'd' * 64,
                        execution_inventory_digest=self.plan.execution_inventory_digest,
                        binding=binding, context=context, diagnostic=mock.Mock())
            write.assert_not_called()
            seal.assert_not_called()

    def test_installer_rejects_online_profiles_before_acquiring_source(self):
        from scripts.development_guest_session import development_binding
        with mock.patch.dict(sys.modules, {'installer.production': None}):
            spec = importlib.util.spec_from_file_location('synthetic_installer_entry',
                Path(__file__).resolve().parents[1] / 'development_installer_entry.py')
            entry = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(entry)
            with (mock.patch.object(entry, 'inherited_writer', return_value=mock.Mock()),
                  mock.patch.object(entry, 'acquire_development_service_source') as acquire,
                  redirect_stdout(io.StringIO())):
                for profile in ('ONLINE_FRESH', 'ONLINE_EXISTING_DOCKER'):
                    self.assertEqual(entry.main(['--binding', json.dumps(development_binding(self.plan)),
                        '--profile', profile, '--public-origin', 'https://candidate.invalid']), 5)
                acquire.assert_not_called()

    def test_plan_only_never_confirms_captures_or_executes(self):
        from scripts import development_guest_session as guest
        from scripts import local_candidate_development as entry
        root = Path(self.enterContext(synthetic_runtime_directory()))
        provider = mock.MagicMock()
        provider._execution.root = root / 'provider'
        provider._execution.work_root = root / 'work'
        provider._profile_operation_results = {}
        provider._candidate_diagnostics = {}
        provider._host_lifecycle_observations = []
        provider.execution_authority.return_value = nullcontext()
        provider.bind_candidate_material_authority.return_value = nullcontext()
        material = SimpleNamespace(loaded=SimpleNamespace(root=root / 'material' / 'root'))
        source = SimpleNamespace(root=root / 'source' / 'root',
            inventory_digest=self.plan.execution_inventory_digest, published_subject_digest=None)
        options = SimpleNamespace(execute=False, confirm_batch=False, authorization_id=None, result=None,
            execution_source_sha=self.plan.execution_source_sha, execution_source_tree=self.plan.execution_source_tree,
            material_source_sha=self.plan.source_sha, material_source_tree=self.plan.source_tree,
            verified_candidate_digest=self.plan.verified_candidate_digest,
            qualification_run_id=self.plan.qualification_run_id, runtime_offline_only=True)
        options.runtime_authorization_deadline = self.plan.runtime_authorization_deadline
        options.runtime_trust_selection_digest = self.plan.runtime_trust_selection_digest
        options.runtime_trust_inputs = root / 'synthetic-trust-inputs'
        options.runtime_portable = root / 'synthetic-portable.tar'
        options.runtime_release_attestation = root / 'synthetic-attestation.json'
        with (mock.patch.object(entry, '_check_checkout'),
              mock.patch.object(entry, 'require_material_compatibility'),
              mock.patch.object(entry.h, 'ClosedVmwareProvider', return_value=provider) as create_provider,
              mock.patch.object(entry.h, 'acquire_candidate_material_authority', return_value=nullcontext(material)),
              mock.patch.object(entry, 'acquire_development_source', return_value=nullcontext(source)),
              mock.patch.object(entry.h, 'build_harness_plan', return_value=self.base),
              mock.patch('installer.development_trust.read_runtime_trust_selection', return_value=mock.Mock()),
              mock.patch.object(guest, 'preflight_development_workload_commands', return_value={}),
              mock.patch.object(scopes, 'confirm_local_batch') as confirm,
              mock.patch.object(entry, 'WindowsConsoleCapture') as console,
              mock.patch.object(entry, 'CandidateBatch') as batch):
            report = entry.run(options)
        self.assertEqual(report['status'], 'SCOPE_ONLY', report.get('failure_code'))
        self.assertNotIn('plan', report)
        self.assertEqual(report['profile_results'], {})
        self.assertFalse(report['private_inputs_verified'])
        create_provider.assert_not_called()
        confirm.assert_not_called()
        console.assert_not_called()
        batch.assert_not_called()
        provider.execute_development_profile.assert_not_called()


class SingleRuntimeOwnerTests(unittest.TestCase):
    def setUp(self):
        temporary_root = Path(self.enterContext(synthetic_runtime_directory()))
        setup = fixtures.DevelopmentBatchFlowTests()
        setup.setUp()
        self.addCleanup(setup.doCleanups)
        self.fixture = setup.fixture
        self.fixture.batch.close()
        self.original_plan = self.fixture.plan
        self.plan = redigest(replace(self.original_plan, runtime_offline_only=True,
            runtime_authorization_deadline='2099-01-01T00:00:00Z',
            runtime_trust_selection_digest='sha256:' + 'e' * 64,
            runtime_retention_policy='STOP_AND_RETAIN',
            profiles=self.original_plan.profiles[2:]))
        self.fixture.plan = self.plan
        self.root = temporary_root / ('f' * 64)
        self.label = 'ANIMEMO_SYNTHETIC_SINGLE_RUNTIME_V1'
        with mock.patch.object(scopes, 'authorization_root', return_value=self.root), \
                mock.patch.object(scopes.WindowsConsoleCapture, 'confirm_batch') as confirm:
            self.authorization = scopes.confirm_local_batch(authorization_id=self.label,
                purpose='LOCAL_INSTALLER_DEVELOPMENT', plan=self.plan,
                runtime_handoff=synthetic_target_for_owner_tests(self.plan, self.label))
            confirm.assert_called_once()
        self.owner = owners.acquire_confirmed_development_owner(authorization=self.authorization,
            material_identity=owners._material(self.plan))
        self.addCleanup(self.owner.dispose)
        self.secret = self.fixture.secret
        from scripts import runtime_development_boundary as boundary
        # These tests exercise secret-role/owner closure. W1 tests separately run
        # the real baseline consumer against explicit OS observations.
        self.enterContext(mock.patch.object(boundary, 'run_before_capture',
            side_effect=lambda provider, plan, *args: boundary.require_confirmed_runtime_boundary(provider, plan)))

    def begin(self):
        batch = batches.CandidateBatch(self.fixture.provider, self.plan, authorization_id=self.label,
            development_owner=self.owner)
        self.addCleanup(batch.close)
        self.fixture.batch = self.fixture.provider._candidate_batch = batch
        from scripts import runtime_development_boundary as boundary
        self.fixture.provider._profile_operation_results[self.plan.profiles[0].profile] = {}
        lifetime = boundary.start_runtime_lifetime(self.fixture.provider, self.plan, self.plan.profiles[0])
        self.fixture.provider._runtime_confirmed_boundary = boundary.ConfirmedRuntimeBoundary(
            boundary._CONFIRM_ISSUER, provider=self.fixture.provider, plan=self.plan,
            baseline={'fixture': 'SYNTHETIC_ONLY'}, body={'fixture': 'SYNTHETIC_ONLY'}, lifetime=lifetime)
        return batch

    def report(self, batch):
        name = 'RUNTIME_BASE_OFFLINE'
        vmx = self.root / 'retained-vm.vmx'
        vmx.write_bytes(b'scsi0:0.fileName = "disk.vmdk"\n')
        (self.root / 'disk.vmdk').write_bytes(b'# Disk DescriptorFile\ncreateType="twoGbMaxExtentFlat"\n'
            b'parentCID=ffffffff\nRW 100 FLAT "disk-flat.vmdk" 0\n')
        (self.root / 'disk-flat.vmdk').write_bytes(b'SYNTHETIC_DATA_NOT_A_BOOTABLE_DISK')
        from scripts.runtime_development_retention import (
            _retained_disk_graph,
            _vmx_identity,
        )
        stop = {'schema': 'animemo.runtime-development-instance-stop/v1',
            'sessionId': self.plan.session_id, 'parentPlanDigest': self.plan.plan_digest,
            'installerPlanDigest': 'sha256:' + '1' * 64,
            'instanceName': 'default', 'composeProject': 'animemo-default',
            'updaterService': 'animemo-updater@default.service', 'containers': [],
            'serviceStopped': True, 'listenerClosed': True,
            'dataRetention': {'state': 'RETAINED', 'identity': {
                'path': '/data/animemo-instances/default', 'device': 1, 'inode': 1}},
            'result': 'PASS', 'failures': []}
        stop['receiptDigest'] = h.sha256_bytes(h.canonical_json_bytes(stop))
        retained = {'schema': 'animemo.runtime-development-retained-clone/v1',
            'parentPlanDigest': self.plan.plan_digest, 'sessionId': self.plan.session_id,
            'cloneIdentity': self.plan.profiles[0].clone_identity,
            'retentionPolicy': 'STOP_AND_RETAIN', 'powerState': 'STOPPED',
            'vmxIdentity': _vmx_identity(vmx), 'diskGraph': _retained_disk_graph(vmx),
            'instanceStopDigest': stop['receiptDigest']}
        retained['receiptDigest'] = h.sha256_bytes(h.canonical_json_bytes(retained))
        return {'plan': batch.plan.as_dict(), 'credential_session': batch.record,
            'failure_policy': 'animemo.graded-profile-failure/v1', 'source_preserved': True,
            'cleanup_errors': [], 'private_material_root_released': True,
            'private_execution_source_root_released': True,
            'private_material_root': str(self.root / 'absent-material'),
            'private_execution_source_root': str(self.root / 'absent-source'),
            'profile_results': {name: {'status': 'PASS'}},
            'profile_operations': {name: {'power_state': 'STOPPED', 'clone_disposition': 'RETAINED_STOPPED',
                'clone_vmx': str(vmx), 'clone_identity': self.plan.profiles[0].clone_identity,
                'instance_stop': stop, 'retention_receipt': retained, 'cleanup_errors': [],
                'session_keys_removed': True, 'known_hosts_removed': True, 'lease_released': True}},
            'status': 'PASS', 'all_profiles_pass': True}

    def completed_synthetic_record(self):
        batch = self.begin()
        with batch.operation('BOOTSTRAP', self.plan.profiles[0]), redirect_stdout(io.StringIO()):
            batch.capture_after_bootstrap_observation(self.plan.profiles[0])
        for entry in batch._record['profiles']['RUNTIME_BASE_OFFLINE'].values():
            entry.update(delivery_attempts=1, delivery_completed=1, target_verified=True,
                lease_verified=True, operation_result='PASS')
        batch.close()
        return self.report(batch)

    def test_three_real_protocol_deliveries_release_capability_then_owner_wipes(self):
        from scripts.tests.test_candidate_diagnostics import successful_frames
        batch = self.begin()
        fixture = self.fixture
        profile, runtime = fixture.bootstrap(0)
        operation = guests._diagnostic_operation(self.plan, profile)
        fixture.fixture.runner.before_exchange = lambda process: setattr(process, 'stdout',
            io.BytesIO(process.stdout.getvalue() + successful_frames(operation)))
        with batch.operation('WORKLOAD', profile):
            use = batch.issue(profile, fixture.lease, batches.ROLES[2:])
            supervisor = guests._WorkloadSupervisor(use, provider=fixture.provider, plan=self.plan,
                profile=profile, lease=fixture.lease, preboot_disk_graph_digest=runtime.disk_graph_digest,
                preboot_snapshot_identity=runtime.snapshot_identity)
            try:
                with mock.patch.object(guests, '_root_program', return_value='pass'), \
                        mock.patch.object(guests, 'hold_windows_private_file', side_effect=lambda _: nullcontext()):
                    self.assertEqual(supervisor.execute()['result'], 'PASS')
                batch.role_result(profile, 'CANDIDATE_WORKLOAD', 'PASS')
            finally:
                supervisor.close()
        self.assertEqual(len(fixture.fixture.runner.processes), 3)
        self.assertEqual(batch.record['secret_state'], 'ROUND_CAPABILITY_RELEASED_NO_FURTHER_USES')
        self.assertTrue(self.secret)
        batch.close()
        self.owner.finish_round(self.report(batch))
        self.assertEqual(self.secret, b'')
        self.assertTrue(self.owner.closed)
        fixture.console.capture.assert_called_once()
        with self.assertRaises(scopes.ControllerFailure):
            self.authorization.reserve_round(self.plan)

    def reject_report(self, mutate):
        report = self.completed_synthetic_record()
        mutate(report)
        with self.assertRaises(owners.DevelopmentOwnerError):
            self.owner.finish_round(report)
        self.assertEqual(self.secret, b'')
        self.assertTrue(self.owner.closed)

    def test_changed_binding_wipes_owner(self):
        self.reject_report(lambda r: r['credential_session']['binding'].update(plan_digest='sha256:' + 'f' * 64))

    def test_cross_session_report_wipes_owner(self):
        self.reject_report(lambda r: r['credential_session']['binding'].update(session_id='f' * 32))

    def test_missing_role_wipes_owner(self):
        self.reject_report(lambda r: r['credential_session']['profiles']['RUNTIME_BASE_OFFLINE'].pop('VERIFIED_SUDO'))

    def test_changed_plan_wipes_owner(self):
        self.reject_report(lambda r: r['plan'].update(runtimeOfflineOnly=False))

    def test_added_profile_wipes_owner(self):
        self.reject_report(lambda r: r['profile_results'].update(FRESH_BASE={'status': 'PASS'}))

    def test_incomplete_cleanup_wipes_owner(self):
        self.reject_report(lambda r: r['profile_operations']['RUNTIME_BASE_OFFLINE'].update(lease_released=False))

    def test_missing_stop_receipt_cannot_be_replaced_with_vm_stop(self):
        self.reject_report(lambda r: r['profile_operations']['RUNTIME_BASE_OFFLINE'].pop('instance_stop'))

    def test_retained_clone_changed_blocks_owner_success(self):
        self.reject_report(lambda r: Path(r['profile_operations']['RUNTIME_BASE_OFFLINE']['clone_vmx']).write_bytes(b'changed'))

    def test_vmx_without_retained_disk_blocks_owner_success(self):
        self.reject_report(lambda r: (Path(r['profile_operations']['RUNTIME_BASE_OFFLINE']['clone_vmx']).parent
            / 'disk-flat.vmdk').unlink())

    def test_closed_owner_cannot_turn_unvalidated_runtime_report_into_pass(self):
        from scripts.local_candidate_development import _close_owned_session
        report = self.completed_synthetic_record()
        self.owner.close('DEVELOPMENT_SESSION_EXPIRED')
        _close_owned_session(report, self.owner)
        self.assertEqual(report['status'], 'ERROR')
        self.assertFalse(report['all_profiles_pass'])
        self.assertIn('DEVELOPMENT_OWNER_CLOSED_BEFORE_RUNTIME_VALIDATION', report['cleanup_errors'])

    def test_changed_identity_with_recomputed_digest_is_not_confirmed(self):
        for plan in (self.original_plan, redigest(replace(self.plan, execution_source_tree='f' * 40)),
                     redigest(replace(self.plan, profiles=(replace(self.plan.profiles[0],
                         snapshot_identity='sha256:' + 'f' * 64),)))):
            with self.subTest(digest=plan.plan_digest), self.assertRaises(scopes.ControllerFailure):
                self.authorization.reserve_round(plan)
        self.assertEqual(self.authorization._reservations, [])
        self.fixture.console.capture.assert_not_called()

    def test_legacy_ledger_or_missing_owner_cannot_run_single(self):
        from scripts import development_capture_scope
        with self.assertRaisesRegex(scopes.ControllerFailure, 'LOCAL_CONFIRMATION_REQUIRED'):
            batches.CandidateBatch(self.fixture.provider, self.plan,
                authorization_id=development_capture_scope.AUTHORIZATION)
        with self.assertRaisesRegex(scopes.ControllerFailure, 'SESSION_OWNER_REQUIRED'):
            batches.CandidateBatch(self.fixture.provider, self.plan, authorization_id=self.label,
                local_authorization=self.authorization)
        self.assertEqual(self.authorization._reservations, [])
        self.fixture.console.capture.assert_not_called()

    def test_removing_selection_flag_cannot_shrink_legacy_batch(self):
        from scripts import development_capture_scope
        unmarked = redigest(replace(self.plan, runtime_offline_only=False))
        with mock.patch.object(development_capture_scope, 'reserve_development_capture') as reserve:
            with self.assertRaisesRegex(scopes.ControllerFailure, 'LOCAL_BATCH_AUTHORIZATION_INVALID'):
                batches.CandidateBatch(self.fixture.provider, unmarked,
                    authorization_id=development_capture_scope.AUTHORIZATION)
            reserve.assert_not_called()
        self.fixture.console.capture.assert_not_called()

    def test_owner_confirmed_for_three_profiles_cannot_reserve_single(self):
        other_root = self.root.parent / ('e' * 64)
        with mock.patch.object(scopes, 'authorization_root', return_value=other_root), \
                mock.patch.object(scopes.WindowsConsoleCapture, 'confirm_batch'):
            other = scopes.confirm_local_batch(authorization_id=self.label,
                purpose='LOCAL_INSTALLER_DEVELOPMENT', plan=self.original_plan)
        owner = owners.acquire_confirmed_development_owner(authorization=other,
            material_identity=owners._material(self.original_plan))
        self.addCleanup(owner.dispose)
        with self.assertRaises(scopes.ControllerFailure):
            batches.CandidateBatch(self.fixture.provider, self.plan, authorization_id=self.label,
                development_owner=owner)
        self.assertEqual(other._reservations, [])
        self.assertEqual(self.authorization._reservations, [])
        self.fixture.console.capture.assert_not_called()
