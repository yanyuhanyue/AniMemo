import ast
import base64
import copy
import json
import os
import tempfile
import unittest
import zlib
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from installer.development import ASSETS, PACKAGES, expected_service_observation
from scripts import candidate_batch_session as batch
from scripts import candidate_vm_harness as h
from scripts import development_capture_scope as scope
from scripts import development_profile_runner as development
from scripts import development_source
from scripts import local_candidate_development as cli
from scripts.development_plan import execution_source, from_material_plan
from scripts.guest_sudo_session import ControllerFailure, SessionSupervisor
from scripts.tests import test_candidate_batch_session as batch_fixtures
from scripts.tests import test_candidate_profile_runner as runner_fixtures
from scripts.tests import test_guest_sudo_session as session_fixtures


class FakeDevelopmentCommandRunner(runner_fixtures.FakeRunner):
    def __init__(self, proof):
        super().__init__()
        self.proof = proof

    def run(self, argv, environment):
        code, output, errors = super().run(argv, environment)
        value = json.loads(output)
        value['developmentServiceSourceObservation'] = self.proof
        return code, json.dumps(value).encode(), errors


class DevelopmentPlanTests(unittest.TestCase):
    def setUp(self):
        fixture = session_fixtures.GuestSudoSessionTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.fixture = fixture
        self.plan = from_material_plan(fixture.plan, execution_source_sha='a' * 40,
            execution_source_tree='b' * 40, execution_inventory_digest='sha256:' + 'c' * 64)

    def test_execution_source_is_distinct_and_formal_harness_rejects_the_plan(self):
        self.assertEqual(execution_source(self.plan), ('a' * 40, 'b' * 40))
        self.assertEqual(self.plan.source_sha, self.fixture.plan.source_sha)
        self.assertNotEqual(self.plan.plan_digest, self.fixture.plan.plan_digest)
        body = self.plan.identity_body()
        self.assertEqual(body['materialSourceSha'], self.fixture.plan.source_sha)
        self.assertEqual(body['executionSourceSha'], 'a' * 40)
        self.assertFalse(body['candidateAcceptanceAuthorityGranted'])
        with self.assertRaisesRegex(h.CandidateHarnessError, 'PLAN_NOT_ACCEPTED'):
            h._execute_harness_plan(self.plan, accepted_plan_digest=self.plan.plan_digest,
                provider=self.fixture.provider)

    def test_formal_and_development_authorizations_are_not_interchangeable(self):
        with self.assertRaisesRegex(ControllerFailure, 'AUTHORIZATION_INVALID'):
            batch.CandidateBatch(self.fixture.provider, self.plan,
                authorization_id=batch.PR247_REVALIDATION_AUTHORIZATION)
        with self.assertRaisesRegex(ControllerFailure, 'AUTHORIZATION_INVALID'):
            batch.CandidateBatch(self.fixture.provider, self.fixture.plan,
                authorization_id=scope.AUTHORIZATION)
        with self.assertRaisesRegex(ControllerFailure, 'SESSION_PLAN_INVALID'):
            SessionSupervisor(bytearray(session_fixtures.SENTINEL), provider=self.fixture.provider,
                plan=self.plan, profile=self.plan.profiles[0],
                preboot_disk_graph_digest='sha256:' + 'd' * 64,
                preboot_snapshot_identity=self.plan.profiles[0].snapshot_identity)

    def test_fixed_root_program_embeds_current_source_within_protocol_size(self):
        from scripts import development_guest_session as guest
        provider = self.fixture.provider
        provider._candidate_material_authority = SimpleNamespace(tree_inventory_identity='sha256:' + 'd' * 64)
        with mock.patch.object(guest, 'require_development_source', return_value=SimpleNamespace(
                root=Path(guest.__file__).resolve().parents[1])):
            command = guest._root_program(provider, self.plan, self.plan.profiles[0])
        parsed = ast.parse(command)
        encoded = next(ast.literal_eval(node.args[0]) for node in ast.walk(parsed)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == 'b64decode')
        program = zlib.decompress(base64.b64decode(encoded)).decode()
        self.assertLessEqual(len(program.encode('utf-8')), guest.c.MAX_ROOT_PROGRAM_BYTES)
        compile(program, '<development-root-test>', 'exec')
        self.assertIn("scope['run_fixed_development']", program)
        self.assertIn(self.plan.execution_inventory_digest, program)


@unittest.skipUnless(os.name == 'nt', 'actual private synthetic development reservation')
class DevelopmentBatchFlowTests(unittest.TestCase):
    def setUp(self):
        fixture = batch_fixtures.BatchSessionTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.batch.close()
        root = fixture.ledger
        ledger_patch = mock.patch.object(scope, 'LEDGER', root)
        ledger_patch.start()
        self.addCleanup(ledger_patch.stop)
        source_patch = mock.patch.object(development_source, '_check_checkout',
            side_effect=lambda *args: batch._check_checkout(*args))
        source_patch.start()
        self.addCleanup(source_patch.stop)
        base = replace(fixture.plan, candidate_version='v2.0.0-rc.1')
        base = replace(base, plan_digest=h.sha256_bytes(h.canonical_json_bytes(base.identity_body())))
        fixture.plan = from_material_plan(base, execution_source_sha='a' * 40,
            execution_source_tree='b' * 40, execution_inventory_digest='sha256:' + 'c' * 64)
        # The actual held-authority validator runs. Only acquisition/OS inputs
        # are synthetic, so an accidental checkout call under the owner lock
        # remains visible to the watchdog regression below.
        authority = object.__new__(development_source.HeldDevelopmentSource)
        authority._closed = False
        authority._execution = fixture.provider._execution
        authority.source_sha = fixture.plan.execution_source_sha
        authority.source_tree = fixture.plan.execution_source_tree
        authority.inventory_digest = fixture.plan.execution_inventory_digest
        fixture.provider._development_source_authority = authority
        fixture.batch = batch.CandidateBatch(fixture.provider, fixture.plan,
            authorization_id=scope.AUTHORIZATION, clock=lambda: fixture.time)
        fixture.addCleanup(fixture.batch.close)
        fixture.provider._candidate_batch = fixture.batch
        fixture.ledger = root / scope.SLOTS[0]
        self.fixture = fixture

    def test_one_capture_reuses_all_existing_target_and_grant_checks_for_three_profiles(self):
        fixture = self.fixture
        fixture.test_one_capture_three_targets_nine_grants_and_early_owner_cleanup()
        value = json.loads((fixture.ledger / 'result.json').read_bytes())
        self.assertEqual(value['purpose'], 'LOCAL_INSTALLER_DEVELOPMENT')
        self.assertEqual(value['development_capture_index'], 1)
        self.assertEqual(value['binding']['execution_source_sha'], 'a' * 40)

    def test_development_watchdog_clears_secret_while_checkout_is_blocked(self):
        self.fixture.test_watchdog_can_revoke_while_the_source_check_is_blocked()
        self.assertEqual(self.fixture.secret, b'')


class DevelopmentRunnerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        runner_fixtures._write_test_wheelhouse(self.root)
        self.loaded = runner_fixtures._loaded(self.root)
        self.binding = {'plan_digest': 'sha256:' + '1' * 64, 'session_id': '2' * 32,
            'execution_source_sha': '3' * 40, 'execution_source_tree': '4' * 40,
            'execution_inventory_digest': 'sha256:' + '5' * 64,
            'verified_candidate_digest': self.loaded.verified_digest,
            'material_source_sha': self.loaded.candidate_input['source_sha'],
            'material_source_tree': self.loaded.candidate_input['source_tree'],
            'qualification_run_id': self.loaded.candidate_input['qualification_run_id'],
            'workload_mode': 'CLEAN_PREACCEPTANCE'}
        service = self.root / 'service-source'
        for name in PACKAGES:
            path = service / name / '__init__.py'
            path.parent.mkdir(parents=True)
            path.write_text('__version__ = "1.0.0"\n', encoding='utf-8')
        for name in ASSETS:
            path = service / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(name.encode())
        self.proof = expected_service_observation(service, self.binding['execution_inventory_digest'])
        patch = mock.patch.object(development, 'expected_service_observation', return_value=self.proof)
        patch.start()
        self.addCleanup(patch.stop)
        for module in (development, development.runner):
            patch = mock.patch.object(module, 'load_verified_candidate', return_value=self.loaded)
            patch.start()
            self.addCleanup(patch.stop)
        parsed_plan = SimpleNamespace(plan_digest='sha256:' + '7' * 64,
            mode=SimpleNamespace(value='OFFLINE_VALIDATE_ONLY'),
            initial_capabilities=SimpleNamespace(docker_cli_present=True, docker_daemon_healthy=True,
                compose_v2_present=True, pg_dump_major=16, psql_major=16),
            network_policy='DENY_ALL', actions=())
        for name, value in (('parse_platform_bootstrap_plan', parsed_plan),
                            ('parse_platform_bootstrap_receipt', SimpleNamespace(result='PASS', plan_digest=parsed_plan.plan_digest))):
            patch = mock.patch.object(development.runner, name, return_value=value)
            patch.start()
            self.addCleanup(patch.stop)

    def execute(self, command_runner):
        return development.execute_development_profile(binding=self.binding,
            profile='RUNTIME_BASE_OFFLINE', context_b64url=runner_fixtures._encoded_context(),
            command_runner=command_runner)

    def test_current_source_and_baseline_wheels_run_full_observation_validation(self):
        commands = FakeDevelopmentCommandRunner(self.proof)
        report = self.execute(commands)
        self.assertEqual(report['schema'], development.SCHEMA)
        self.assertFalse(report['candidate_acceptance_authority_granted'])
        self.assertFalse(report['publish_authorized'])
        self.assertTrue(all(commands.runtime_dependency_visible))
        self.assertEqual(commands.calls[0][0][4], 'scripts.development_installer_entry')
        paths = commands.calls[0][1]['PYTHONPATH'].split(os.pathsep)
        self.assertEqual(Path(paths[1]), Path(development.__file__).resolve().parents[1])
        self.assertNotEqual(Path(paths[1]), self.root / 'installer-root')
        from release.candidate import (
            CandidateContractError,
            validate_aggregate_receipt,
            validate_profile_receipt,
        )
        for validator in (validate_aggregate_receipt, validate_profile_receipt):
            with self.assertRaises(CandidateContractError):
                validator(report)

    def test_material_mismatch_fails_before_installer_command(self):
        commands = FakeDevelopmentCommandRunner(self.proof)
        self.binding['material_source_sha'] = '9' * 40
        with self.assertRaisesRegex(development.runner.ProfileRunnerError, 'MATERIAL_BINDING_INVALID'):
            self.execute(commands)
        self.assertEqual(commands.calls, [])

    def test_recomputed_report_digest_does_not_hide_business_or_source_failure(self):
        report = self.execute(FakeDevelopmentCommandRunner(self.proof))
        changed = copy.deepcopy(report)
        changed['installer_output']['productionExecutionObservation']['doctorReport']['overallStatus'] = 'FAIL'
        changed['report_digest'] = development.sha256_bytes(development.canonical_json_bytes(
            {k: v for k, v in changed.items() if k != 'report_digest'}))
        with self.assertRaises(development.runner.ProfileRunnerError):
            development.validate_development_report(changed, loaded=self.loaded,
                expected_binding=self.binding, expected_context=runner_fixtures._context(), expected_service_source=self.proof)
        with self.assertRaises(development.runner.ProfileRunnerError):
            development.validate_development_report(report, loaded=self.loaded,
                expected_binding={**self.binding, 'execution_source_sha': '8' * 40},
                expected_context=runner_fixtures._context(), expected_service_source=self.proof)

    def test_old_installed_service_bytes_cannot_pass_current_source_report(self):
        old = copy.deepcopy(self.proof)
        old['package_inventories']['updater'] = 'sha256:' + '9' * 64
        with self.assertRaisesRegex(development.runner.ProfileRunnerError, 'SERVICE_SOURCE_MISMATCH'):
            self.execute(FakeDevelopmentCommandRunner(old))


class DevelopmentEntryTests(unittest.TestCase):
    def test_complete_result_export_rejects_json_type_loss_and_invalid_numbers(self):
        valid={'status':'PASS','profile_reports':{'FRESH_BASE':{'commands':[{'return_code':0}]}}}
        self.assertEqual(json.loads(cli.result_bytes(valid)),valid)
        for invalid in ({'status':'PASS','commands':(1,2)}, {'status':'PASS','number':float('nan')}):
            with self.assertRaisesRegex(cli.h.CandidateHarnessError,'DEVELOPMENT_RESULT_EXPORT_INVALID'):
                cli.result_bytes(invalid)

    def test_missing_unknown_and_plan_only_authorizations_fail_before_provider(self):
        for execute, authorization in ((True, None), (True, batch.PR247_REVALIDATION_AUTHORIZATION),
                                        (True, 'unknown'), (False, scope.AUTHORIZATION)):
            with self.subTest(execute=execute, authorization=authorization), mock.patch.object(cli.h, 'ClosedVmwareProvider') as provider:
                result = cli.run(SimpleNamespace(execute=execute, authorization_id=authorization, result=Path('unused')))
                self.assertEqual(result['status'], 'ERROR')
                self.assertIsNone(result['credential_session'])
                provider.assert_not_called()


if __name__ == '__main__':
    unittest.main()
