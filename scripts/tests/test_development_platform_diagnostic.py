"""Synthetic platform fixture tests; never Candidate or real Guest evidence."""
import base64
import copy
import json
import tempfile
import unittest
from contextlib import ExitStack, nullcontext
from pathlib import Path
from unittest import mock

from installer import apt_diagnostics as apt
from installer.platform_bootstrap import PlatformCommandResult, ProductionPlatformBootstrap
from installer.tests.test_platform_bootstrap import (
    RunnerFixture, SequenceFacts, fresh_base_facts, qualified_existing_facts,
)
from release.candidate import canonical_json_bytes, sha256_bytes
from scripts import development_platform_diagnostic as diagnostic
from scripts.tests import test_candidate_profile_runner as fixtures


class ObservedFixture(RunnerFixture):
    def run(self, argv, *, timeout, environment):
        result = super().run(argv, timeout=timeout, environment=environment)
        if argv[0] != '/usr/bin/apt-get':
            return result
        capture = apt.Capture()
        capture.feed(result.stderr)
        capture.finish()
        process = apt.CapturedProcess(result.returncode, 'EXITED', result.stdout, result.stderr,
            apt.utc_now(), apt.utc_now(), apt.Capture().public(), capture.public(), ())
        return PlatformCommandResult(result.returncode, result.stdout, result.stderr,
            observation=apt.apt_observation(argv, process, '2.8.3'))


class DevelopmentPlatformDiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.loaded = fixtures._loaded(Path('fixture-material-root'))
        self.context = fixtures._context('FRESH_BASE')
        self.context['initial_platform_state'] = dict(docker_present=False,
            runtime_dependencies_present=False, network_allowed=True)
        self.binding = dict(plan_digest='sha256:' + '1' * 64, session_id='2' * 32,
            execution_source_sha='3' * 40, execution_source_tree='4' * 40,
            execution_inventory_digest='sha256:' + '5' * 64,
            verified_candidate_digest=self.loaded.verified_digest,
            material_source_sha=self.loaded.candidate_input['source_sha'],
            material_source_tree=self.loaded.candidate_input['source_tree'],
            qualification_run_id=self.loaded.candidate_input['qualification_run_id'],
            workload_mode='PLATFORM_DIAGNOSTIC')

    def execute(self, *, fail=False, binding=None, profile='FRESH_BASE'):
        command = ObservedFixture(fail_token=' update' if fail else None)
        facts = SequenceFacts(fresh_base_facts(), fresh_base_facts(), qualified_existing_facts(
            installed=('docker.io', 'docker-compose-v2', 'postgresql-client-16')))
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(diagnostic, 'load_verified_candidate', return_value=self.loaded))
            stack.enter_context(mock.patch.object(diagnostic, 'closed_runtime_inventory_digest',
                                                  return_value=self.binding['execution_inventory_digest']))
            stack.enter_context(mock.patch.object(diagnostic, 'inherited_writer', return_value=mock.Mock()))
            stack.enter_context(mock.patch.object(diagnostic, 'SubprocessPlatformCommandRunner', return_value=command))
            stack.enter_context(mock.patch.object(diagnostic, 'ProductionPlatformBootstrap',
                side_effect=lambda runner: ProductionPlatformBootstrap(runner=runner,
                    facts_collector=facts, lock_factory=lambda: nullcontext())))
            result = diagnostic.execute_platform_diagnostic(binding=binding or self.binding,
                profile=profile, context_b64url=base64.urlsafe_b64encode(
                    canonical_json_bytes(self.context)).decode().rstrip('='))
        return result, command

    def validate(self, value):
        return diagnostic.validate_platform_diagnostic_report(value, loaded=self.loaded,
            expected_binding=self.binding, expected_context=self.context)

    def seal(self, value):
        value['report_digest'] = sha256_bytes(canonical_json_bytes(
            {name: item for name, item in value.items() if name != 'report_digest'}))
        return value

    def test_fresh_platform_only_has_four_fixed_apt_operations_and_no_app_authority(self):
        value, commands = self.execute()
        self.assertEqual(value['result'], 'PASS')
        self.assertEqual(len(value['apt_observations']), 4)
        self.assertEqual(self.validate(value), value)
        self.assertFalse(value['candidate_acceptance_authority_granted'])
        self.assertFalse(value['publish_authorized'])
        self.assertNotIn('installer_output', value)
        self.assertNotIn('doctor', json.dumps(value))
        self.assertFalse(any('compose' in command[0] for command in commands.calls))

    def test_apt_failure_remains_fail_with_actual_returncode_and_no_platform_receipt(self):
        value, commands = self.execute(fail=True)
        self.assertEqual(value['result'], 'FAIL')
        self.assertEqual(value['error_code'], 'PLATFORM_BOOTSTRAP_APT_UPDATE_FAILED')
        self.assertEqual(value['apt_observations'][0]['returncode'], 1)
        self.assertEqual(value['apt_observations'][0]['categories'], ['UNKNOWN'])
        self.assertIsNone(value['platform_receipt'])
        self.assertEqual(len([call for call in commands.calls if 'update' in call[0]]), 1)
        self.validate(value)

    def test_report_tampering_cannot_turn_diagnostic_into_candidate_authority(self):
        original, _ = self.execute()
        for field, changed in [('publish_authorized', True), ('candidate_acceptance_authority_granted', True),
                               ('purpose', 'CANDIDATE_ACCEPTANCE'), ('apt_observations', [])]:
            value = self.seal(dict(copy.deepcopy(original), **{field: changed}))
            with self.subTest(field=field), self.assertRaises(diagnostic.runner.ProfileRunnerError):
                self.validate(value)

    def test_unknown_version_and_nonzero_apt_cannot_be_embedded_in_pass(self):
        original, _ = self.execute()
        for key, replacement in [('tool_version', None), ('returncode', 100), ('outcome', 'TIMEOUT')]:
            value = copy.deepcopy(original)
            value['apt_observations'][0][key] = replacement
            with self.subTest(key=key), self.assertRaises(diagnostic.runner.ProfileRunnerError):
                self.validate(self.seal(value))

    def test_different_binding_or_nonfresh_profile_rejected_before_platform(self):
        for binding, profile in [(dict(self.binding, workload_mode='CLEAN_PREACCEPTANCE'), 'FRESH_BASE'),
                                 (self.binding, 'DOCKER_BASE')]:
            with self.subTest(profile=profile), self.assertRaises(diagnostic.runner.ProfileRunnerError):
                self.execute(binding=binding, profile=profile)

    def test_successful_fail_report_transport_returns_zero_without_relabeling_result(self):
        value, _ = self.execute(fail=True)
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / 'report.json'
            with mock.patch.object(diagnostic, 'OUTPUT', output), \
                    mock.patch.object(diagnostic, 'inherited_writer', return_value=mock.Mock()), \
                    mock.patch.object(diagnostic, 'execute_platform_diagnostic', return_value=value):
                code = diagnostic.main(['--profile', 'FRESH_BASE', '--binding', json.dumps(self.binding)])
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(output.read_bytes())['result'], 'FAIL')


if __name__ == '__main__':
    unittest.main()
