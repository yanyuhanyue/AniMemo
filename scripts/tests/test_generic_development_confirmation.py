"""Synthetic consent only; exercise production scope budgets and native binding."""
from pathlib import Path
from unittest import TestCase, mock

from scripts import guest_batch_scope as scopes
from scripts.tests.test_local_candidate_development import DevelopmentPlanTests
from scripts.tests.formal_windows_pretrust_fixture import private_windows_test_directory


class GenericDevelopmentConfirmationTests(TestCase):
    def setUp(self):
        fixture = DevelopmentPlanTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.plan = fixture.plan
        self.root = Path(self.enterContext(private_windows_test_directory())) / ('d' * 64)

    def test_generic_label_has_one_round_and_cannot_reopen_uncaptured_scope(self):
        label = 'ANIMEMO_SYNTHETIC_GENERIC_DEVELOPMENT_V1'
        with mock.patch.object(scopes, 'authorization_root', return_value=self.root), \
                mock.patch.object(scopes.WindowsConsoleCapture, 'confirm_batch') as confirm:
            scope = scopes.confirm_local_batch(authorization_id=label,
                purpose='LOCAL_INSTALLER_DEVELOPMENT', plan=self.plan)
            self.addCleanup(scope.close)
            self.assertEqual(scope.round_limit, 1)
            self.assertEqual(scope.body['authorization_id'], label)
            self.assertEqual(scope.body['capture_limit'], 1)
            self.assertEqual(scope.body['failure_policy'], 'animemo.graded-profile-failure/v1')
            self.assertIn('Profile cleanup', confirm.call_args.args[0])
            scope.reserve_round(self.plan).close()
            with self.assertRaises(scopes.ControllerFailure):
                scope.reserve_round(self.plan)
            scope.close()
            with self.assertRaises(scopes.ControllerFailure):
                scopes.confirm_local_batch(authorization_id=label,
                    purpose='LOCAL_INSTALLER_DEVELOPMENT', plan=self.plan)
            confirm.assert_called_once()
            self.assertFalse((self.root / 'capture-attempt.json').exists())

    def test_invalid_label_or_budget_fails_before_native_consent(self):
        with mock.patch.object(scopes.WindowsConsoleCapture, 'confirm_batch') as confirm:
            for label in ('../escape', '', scopes.RETIRED_FORMAL_AUTHORIZATION):
                with self.subTest(label=label), self.assertRaises(scopes.ControllerFailure):
                    scopes.confirm_local_batch(authorization_id=label,
                        purpose='LOCAL_INSTALLER_DEVELOPMENT', plan=self.plan)
            for limit in (0, -1, 13, True, '1'):
                with self.subTest(limit=limit), self.assertRaises(scopes.ControllerFailure):
                    scopes.confirm_local_batch(authorization_id='ANIMEMO_SYNTHETIC_GENERIC_V1',
                        purpose='LOCAL_INSTALLER_DEVELOPMENT', plan=self.plan, round_limit=limit)
            confirm.assert_not_called()

    def test_candidate_and_formal_cannot_request_development_round_budget(self):
        with mock.patch.object(scopes.WindowsConsoleCapture, 'confirm_batch') as confirm:
            for purpose in ('CANDIDATE_ACCEPTANCE', 'FORMAL_POSTPUBLICATION'):
                with self.subTest(purpose=purpose), self.assertRaises(scopes.ControllerFailure):
                    scopes.confirm_local_batch(authorization_id='ANIMEMO_SYNTHETIC_GENERIC_V1',
                        purpose=purpose, plan=self.plan, round_limit=2)
            confirm.assert_not_called()

    def test_platform_diagnostic_plan_only_grants_one_fresh_profile(self):
        from scripts.development_plan import from_material_plan
        from scripts.tests.test_guest_sudo_session import GuestSudoSessionTests
        fixture = GuestSudoSessionTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        diagnostic = from_material_plan(fixture.plan, execution_source_sha='a' * 40,
            execution_source_tree='b' * 40, execution_inventory_digest='sha256:' + 'c' * 64,
            platform_diagnostic=True)
        self.assertEqual(tuple(p.profile for p in diagnostic.profiles), ('FRESH_BASE',))
        self.assertEqual(diagnostic.identity_body()['developmentMode'], 'PLATFORM_DIAGNOSTIC')
        self.assertFalse(diagnostic.identity_body()['candidateAcceptanceAuthorityGranted'])
        with mock.patch.object(scopes, 'authorization_root', return_value=self.root), \
                mock.patch.object(scopes.WindowsConsoleCapture, 'confirm_batch'):
            scope = scopes.confirm_local_batch(authorization_id='ANIMEMO_SYNTHETIC_DIAGNOSTIC_V1',
                purpose='LOCAL_INSTALLER_DEVELOPMENT', plan=diagnostic)
            self.addCleanup(scope.close)
            self.assertEqual(len(scope.body['initial_plan']['profiles']), 1)

    def test_diagnostic_binding_cannot_enter_full_installer(self):
        from scripts.development_guest_session import development_binding
        from scripts import development_profile_runner as runner
        binding = development_binding(self.plan)
        binding['workload_mode'] = 'PLATFORM_DIAGNOSTIC'
        with mock.patch.object(runner, 'load_verified_candidate') as load:
            with self.assertRaisesRegex(runner.runner.ProfileRunnerError, 'BINDING_INVALID'):
                runner.execute_development_profile(binding=binding, profile='FRESH_BASE', context_b64url='')
            load.assert_not_called()
        for invalid in (None, {}, 'CANDIDATE_ACCEPTANCE', True):
            with self.subTest(mode=invalid), self.assertRaises(runner.runner.ProfileRunnerError):
                runner.validate_binding({**binding, 'workload_mode': invalid})

    def test_owner_final_validation_failure_keeps_primary_result_and_wipes_secret(self):
        import json
        from scripts import local_candidate_development as entry
        from scripts import development_session_owner as owners
        with mock.patch.object(scopes, 'authorization_root', return_value=self.root), \
                mock.patch.object(scopes.WindowsConsoleCapture, 'confirm_batch'):
            scope = scopes.confirm_local_batch(authorization_id='ANIMEMO_SYNTHETIC_OWNER_CLEANUP_V1',
                purpose='LOCAL_INSTALLER_DEVELOPMENT', plan=self.plan)
        owner = owners.acquire_confirmed_development_owner(authorization=scope,
            material_identity=owners._material(self.plan))
        self.addCleanup(owner.dispose)
        secret = bytearray(b'synthetic-cleanup-regression')
        owner._secret, owner._attempts, owner._completed = secret, 1, 1
        # Incomplete cleanup evidence must fail closed without replacing APT's
        # original failure or preventing the caller from writing the result.
        report = {'status':'FAIL', 'all_profiles_pass':False,
            'failure_code':'PLATFORM_BOOTSTRAP_APT_UPDATE_FAILED',
            'workload_diagnostics':{'FRESH_BASE':{'returncode':100}}, 'cleanup_errors':[]}
        entry._close_owned_session(report, owner)
        self.assertEqual(report['status'], 'ERROR')
        self.assertEqual(report['failure_code'], 'PLATFORM_BOOTSTRAP_APT_UPDATE_FAILED')
        self.assertEqual(report['workload_diagnostics']['FRESH_BASE']['returncode'], 100)
        self.assertTrue(report['cleanup_errors'])
        self.assertEqual(report['memory_owner']['state'], 'CLOSED')
        self.assertEqual(secret, b'')
        self.assertEqual(json.loads(entry.result_bytes(report)), report)

    def test_workload_construction_failure_precedes_native_confirmation_and_capture(self):
        from contextlib import nullcontext
        from types import SimpleNamespace
        from scripts import local_candidate_development as entry, development_guest_session as guest
        provider = mock.MagicMock()
        provider._execution.root = self.root / 'provider'
        provider._execution.work_root = self.root / 'work'
        provider._profile_operation_results = {}
        provider._candidate_diagnostics = {}
        provider._host_lifecycle_observations = []
        provider.execution_authority.return_value = nullcontext()
        provider.bind_candidate_material_authority.return_value = nullcontext()
        material = SimpleNamespace(loaded=SimpleNamespace(root=self.root / 'material' / 'root'))
        source = SimpleNamespace(root=self.root / 'source' / 'root',
            inventory_digest=self.plan.execution_inventory_digest)
        base = self.plan.__dict__.copy()
        for field in ('execution_source_sha', 'execution_source_tree', 'execution_inventory_digest', 'platform_diagnostic'):
            base.pop(field)
        base['candidate_version'] = 'v2.0.0-rc.2'
        base_plan = entry.h.CandidateHarnessPlan(**base)
        from dataclasses import replace
        base_plan = replace(base_plan, plan_digest=entry.h.sha256_bytes(entry.h.canonical_json_bytes(base_plan.identity_body())))
        options = SimpleNamespace(execute=True, confirm_batch=True,
            authorization_id='ANIMEMO_SYNTHETIC_COMMAND_PREFLIGHT_V1', result=self.root / 'result.json',
            execution_source_sha=self.plan.execution_source_sha, execution_source_tree=self.plan.execution_source_tree,
            material_source_sha=self.plan.source_sha, material_source_tree=self.plan.source_tree,
            verified_candidate_digest=self.plan.verified_candidate_digest,
            qualification_run_id=self.plan.qualification_run_id, platform_diagnostic=False)
        with (mock.patch.object(entry, '_check_checkout'),
              mock.patch.object(entry, 'require_material_compatibility'),
              mock.patch.object(entry.WindowsConsoleCapture, 'preflight'),
              mock.patch.object(entry.h, 'ClosedVmwareProvider', return_value=provider),
              mock.patch.object(entry.h, 'acquire_candidate_material_authority', return_value=nullcontext(material)),
              mock.patch.object(entry, 'acquire_development_source', return_value=nullcontext(source)),
              mock.patch.object(entry.h, 'build_harness_plan', return_value=base_plan),
              mock.patch.object(guest, 'preflight_development_workload_commands',
                  side_effect=scopes.ControllerFailure('CANDIDATE_WORKLOAD_COMMAND_LIMIT_EXCEEDED')) as preflight,
              mock.patch.object(scopes, 'confirm_local_batch') as confirm,
              mock.patch.object(entry.CandidateBatch, 'capture_after_bootstrap_observation') as capture):
            report = entry.run(options)
        self.assertEqual(report['failure_code'], 'CANDIDATE_WORKLOAD_COMMAND_LIMIT_EXCEEDED')
        self.assertEqual(report['status'], 'ERROR')
        self.assertIsNone(report['credential_session'])
        preflight.assert_called_once()
        confirm.assert_not_called()
        capture.assert_not_called()
        provider.execute_development_profile.assert_not_called()
