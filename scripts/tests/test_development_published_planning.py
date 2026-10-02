"""Synthetic local DEV contracts; these tests grant no published acceptance."""
import copy
import base64
from dataclasses import replace
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from contextlib import ExitStack
from unittest import mock

from release.candidate import canonical_json_bytes, sha256_bytes
from scripts import development_published_planning as planning
from scripts import development_published_observer as observer
from scripts.development_guest_session import development_binding
from scripts.development_plan import from_material_plan
from scripts.tests import test_guest_sudo_session as guest_fixtures
from scripts.tests import test_candidate_profile_runner as materials


class PublishedPlanningTests(unittest.TestCase):
    def setUp(self):
        fixture = guest_fixtures.GuestSudoSessionTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.base = fixture.plan
        self.plan = from_material_plan(self.base, execution_source_sha='a' * 40,
            execution_source_tree='b' * 40, execution_inventory_digest='sha256:' + 'c' * 64,
            platform_diagnostic=True, published_subject_digest='sha256:' + 'd' * 64)

    def test_fixed_fresh_mode_and_subject_are_in_confirmed_plan_digest(self):
        from scripts import guest_batch_scope as scopes
        from scripts.development_profile_runner import validate_binding, execute_development_profile, runner
        self.assertEqual([p.profile for p in self.plan.profiles], ['FRESH_BASE'])
        self.assertEqual(self.plan.identity_body()['developmentMode'], planning.MODE)
        self.assertEqual(self.plan.identity_body()['publishedSubjectDigest'], 'sha256:' + 'd' * 64)
        scopes._require_plan('LOCAL_INSTALLER_DEVELOPMENT', self.plan)
        for purpose in ('FORMAL_POSTPUBLICATION', 'CANDIDATE_ACCEPTANCE'):
            with self.assertRaises(scopes.ControllerFailure):
                scopes._require_plan(purpose, self.plan)
        binding = development_binding(self.plan)
        validate_binding(binding)
        with self.assertRaisesRegex(runner.ProfileRunnerError, 'BINDING_INVALID'):
            execute_development_profile(binding=binding, profile='FRESH_BASE', context_b64url='')
        for value in (None, {}, 'CANDIDATE_ACCEPTANCE', True):
            with self.subTest(value=value), self.assertRaises(runner.ProfileRunnerError):
                validate_binding({**binding, 'published_subject_digest': value})

    def test_published_subject_cannot_select_full_development_workload(self):
        from scripts.candidate_vm_harness import CandidateHarnessError
        with self.assertRaises(CandidateHarnessError):
            from_material_plan(self.base, execution_source_sha='a' * 40,
                execution_source_tree='b' * 40, execution_inventory_digest='sha256:' + 'c' * 64,
                published_subject_digest='sha256:' + 'd' * 64)

    def test_published_planning_refuses_additional_rounds_before_confirmation(self):
        from scripts import guest_batch_scope as scopes
        with mock.patch.object(scopes.WindowsConsoleCapture, 'confirm_batch') as confirm:
            with self.assertRaises(scopes.ControllerFailure):
                scopes.confirm_local_batch(authorization_id='ANIMEMO_SYNTHETIC_PUBLISHED_PLAN_V1',
                    purpose='LOCAL_INSTALLER_DEVELOPMENT', plan=self.plan, round_limit=2)
        confirm.assert_not_called()

    def test_published_execute_requires_new_native_confirmation_before_provider(self):
        from scripts import local_candidate_development as entry
        from scripts.guest_batch_scope import DEVELOPMENT_AUTHORIZATION
        for authorization in (entry.AUTHORIZATION, DEVELOPMENT_AUTHORIZATION,
                              'ANIMEMO_SYNTHETIC_PUBLISHED_PLAN_V1'):
            options = SimpleNamespace(execute=True, confirm_batch=False, published_platform_plan=True,
                authorization_id=authorization, result=Path('unused-result.json'),
                execution_source_sha='a' * 40, execution_source_tree='b' * 40,
                material_source_sha=self.base.source_sha, material_source_tree=self.base.source_tree,
                platform_diagnostic=False, asset_root=Path('unused-assets'), sidecar=Path('unused-sidecar'),
                sidecar_digest='sha256:' + 'd' * 64, release_id=1,
                windows_gh=Path('unused-gh.exe'), linux_gh_package=Path('unused-gh.deb'))
            with self.subTest(authorization=authorization), \
                    mock.patch.object(entry, '_check_checkout'), \
                    mock.patch.object(entry, 'require_material_compatibility'), \
                    mock.patch.object(entry.WindowsConsoleCapture, 'preflight') as preflight, \
                    mock.patch.object(entry.h, 'ClosedVmwareProvider', side_effect=AssertionError('provider reached')) as provider, \
                    mock.patch.object(entry.CandidateBatch, 'capture_after_bootstrap_observation') as capture:
                report = entry.run(options)
            self.assertEqual(report['failure_code'], 'DEVELOPMENT_LOCAL_CONFIRMATION_REQUIRED')
            self.assertIsNone(report['credential_session'])
            provider.assert_not_called()
            preflight.assert_not_called()
            capture.assert_not_called()

    def test_published_failure_cannot_self_assert_complete_without_fault_envelope(self):
        from scripts import candidate_diagnostics as diagnostics
        from scripts import candidate_guest_session as guest
        loaded, binding, _context, value = self.failure_report()
        plan = replace(self.plan, source_sha=loaded.candidate_input['source_sha'],
            source_tree=loaded.candidate_input['source_tree'], qualification_run_id=loaded.candidate_input['qualification_run_id'],
            verified_candidate_digest=loaded.verified_digest, candidate_version=loaded.candidate_input['candidate_version'],
            published_subject_digest=binding['published_subject_digest'])
        plan = replace(plan, plan_digest=sha256_bytes(canonical_json_bytes(plan.identity_body())))
        profile = plan.profiles[0]
        value.update(binding=development_binding(plan), diagnostic_status='COMPLETE',
            context=guest._profile_context(plan, profile, guest.h._initial_platform_state(profile.profile)))
        value['report_digest'] = sha256_bytes(canonical_json_bytes(
            {k: v for k, v in value.items() if k != 'report_digest'}))
        planning.validate_published_planning_report(value, loaded=loaded,
            expected_binding=value['binding'], expected_context=value['context'])
        for envelope in ('MISSING_BOTH', 'MISSING_END'):
            with self.subTest(envelope=envelope), tempfile.TemporaryFile() as stream:
                operation = 'sha256:' + 'f' * 64
                writer = diagnostics.DiagnosticWriter(stream.fileno(), operation)
                for stage in ('ROOT_STARTED', 'RUNTIME_READY', 'RUNNER_STARTED', 'PLATFORM_PREPARING'):
                    writer.stage(stage)
                writer.error('RUNNER_EXECUTION_FAILED')
                if envelope == 'MISSING_END':
                    writer.event('FAULT_BEGIN')
                writer.stage('DRAFT_WRITTEN')
                writer.stage('DRAFT_RETURNED')
                writer.frame(b'R', canonical_json_bytes(value))
                for component in ('RUNTIME_RUNNER', 'ROOT', 'SUDO'):
                    writer.exited(component, 0)
                stream.seek(0)
                encoded = base64.b64encode(stream.read()).decode('ascii')
                # Real subprocess stdout, frame parser, reader finalization,
                # and supervisor projection; no mocked diagnostic authority.
                process = subprocess.Popen([sys.executable, '-I', '-B', '-c',
                    'import base64,sys;sys.stdout.buffer.write(base64.b64decode(' + repr(encoded) + '))'],
                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
                provider = SimpleNamespace(_candidate_diagnostics={},
                    _candidate_material_authority=SimpleNamespace(loaded=loaded))
                batch = SimpleNamespace(plan=plan, cancelled=threading.Event())
                try:
                    with self.assertRaises(guest.WorkloadFailure) as raised:
                        guest._read_receipt(process, operation=operation, provider=provider,
                            profile=profile, batch=batch, platform_diagnostic=True, timeout=5)
                    self.assertTrue(raised.exception.revoke_batch)
                    observed = provider._candidate_diagnostics[profile.profile]
                    self.assertIn(observed['failure_diagnostic']['status'], {'UNAVAILABLE', 'INCOMPLETE'})
                    self.assertIsNone(observed['exit_codes']['INSTALLER'])
                finally:
                    if process.poll() is None:
                        process.kill()
                    process.wait(timeout=5)
                    process.stdout.close()

    def failure_report(self):
        loaded = materials._loaded(Path('synthetic-material-root'))
        loaded.candidate_input['installer_materials_sha256'] = 'sha256:' + 'e' * 64
        binding = development_binding(self.plan)
        binding.update(material_source_sha=loaded.candidate_input['source_sha'],
            material_source_tree=loaded.candidate_input['source_tree'],
            qualification_run_id=loaded.candidate_input['qualification_run_id'],
            verified_candidate_digest=loaded.verified_digest)
        subject = dict(schema=planning.SUBJECT_SCHEMA, purpose='NON_AUTHORITATIVE_DEVELOPMENT',
            operation=planning.MODE, profile='FRESH_BASE', version=loaded.candidate_input['candidate_version'],
            release_id=1, material_source_sha=binding['material_source_sha'],
            material_source_tree=binding['material_source_tree'], qualification_run_id=binding['qualification_run_id'],
            verified_candidate_digest=loaded.verified_digest,
            archive_sha256=loaded.candidate_input['installer_materials_sha256'])
        binding['published_subject_digest'] = sha256_bytes(canonical_json_bytes(subject))
        context = materials._context('FRESH_BASE')
        value = dict(schema=planning.SCHEMA, purpose='NON_AUTHORITATIVE_DEVELOPMENT', operation=planning.MODE,
            result='FAIL', binding=binding, context=context, subject=subject, platform_plan=None,
            failure_code=planning.FAILURE, diagnostic_status='NO_LOCATION', started_at='2026-09-20T01:00:00Z',
            completed_at='2026-09-20T01:00:01Z', platform_apply_executions=0, installer_executions=0,
            formal_authority_granted=False, candidate_acceptance_authority_granted=False, publish_authorized=False)
        value['report_digest'] = sha256_bytes(canonical_json_bytes(value))
        return loaded, binding, context, value

    def test_failure_report_has_separate_schema_and_rejects_formal_consumers(self):
        from release.formal_vm_controller import (
            validate_formal_profile_receipt, validate_formal_aggregate_receipt, FormalProducerError,
        )
        from release.acceptance import validate_rc_live_acceptance
        from installer.platform_bootstrap import ProductionPlatformBootstrap
        from installer.runtime import InstallTransportSource
        from installer.tests.test_platform_bootstrap import RunnerFixture, fresh_base_facts
        loaded, binding, context, value = self.failure_report()
        self.assertIs(planning.validate_published_planning_report(value, loaded=loaded,
            expected_binding=binding, expected_context=context), value)
        for result in ('PASS', 'FAIL'):
            candidate = copy.deepcopy(value)
            if result == 'PASS':
                bootstrap = ProductionPlatformBootstrap(runner=RunnerFixture(), facts_collector=fresh_base_facts)
                plan = bootstrap.plan(transport_source=InstallTransportSource.GITHUB)
                candidate.update(result='PASS', failure_code=None, diagnostic_status='NOT_REQUIRED',
                                 platform_plan=plan.as_dict())
                candidate['report_digest'] = sha256_bytes(canonical_json_bytes(
                    {k: v for k, v in candidate.items() if k != 'report_digest'}))
                planning.validate_published_planning_report(candidate, loaded=loaded,
                    expected_binding=binding, expected_context=context)
            for consumer in (validate_formal_profile_receipt, validate_formal_aggregate_receipt,
                             validate_rc_live_acceptance):
                with self.subTest(result=result, consumer=consumer.__name__), self.assertRaises((ValueError, FormalProducerError)):
                    consumer(candidate)

    def test_tampered_identity_execution_count_and_private_error_are_rejected(self):
        loaded, binding, context, original = self.failure_report()
        for key, change in (('formal_authority_granted', True), ('installer_executions', 1),
                ('platform_apply_executions', True), ('failure_code', 'private-sentinel'),
                ('diagnostic_status', 'full traceback'), ('platform_plan', {})):
            value = copy.deepcopy(original)
            value[key] = change
            value['report_digest'] = sha256_bytes(canonical_json_bytes(
                {k: v for k, v in value.items() if k != 'report_digest'}))
            with self.subTest(key=key), self.assertRaises(ValueError):
                planning.validate_published_planning_report(value, loaded=loaded,
                    expected_binding=binding, expected_context=context)

    def test_real_production_planning_converts_low_level_read_failure_without_apply(self):
        from updater.source import AnonymousGitHubRest
        from installer.production import ProductionInstallerComposition
        from installer.runtime import Installer, InstallerError
        from installer import production
        diagnostic = mock.Mock()
        subject = {'version': 'v2.0.0-rc.3'}
        # Only module location (no Guest here) and the bottom HTTP boundary are
        # synthetic. Production composition, release parsing/translation and
        # plan_platform itself remain the real implementations.
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        with mock.patch.object(observer, '_module_origin'), \
                mock.patch.object(production.tempfile, 'TemporaryDirectory', return_value=temporary), \
                mock.patch.object(AnonymousGitHubRest, 'get_json', side_effect=OSError('private-sentinel')) as read, \
                mock.patch('bootstrap_kit.http_supervisor.OwnedProcess', side_effect=AssertionError('real child forbidden')), \
                mock.patch('socket.create_connection', side_effect=AssertionError('real network forbidden')), \
                mock.patch.object(ProductionInstallerComposition, 'execute_platform') as apply, \
                mock.patch.object(Installer, 'execute') as install:
            with self.assertRaises(InstallerError):
                observer.observe(subject, diagnostic)
        read.assert_called()
        apply.assert_not_called()
        install.assert_not_called()
        diagnostic.stage.assert_called_once_with('PLATFORM_PREPARING')

    def test_observer_rejects_module_from_dev_tree(self):
        with mock.patch.dict(observer.sys.modules, {'installer.production': SimpleNamespace(
                __file__=str(Path(__file__).resolve()))}):
            with self.assertRaisesRegex(ValueError, 'CONTEXT_INVALID'):
                observer._module_origin('installer.production')

    def test_observer_cancellation_is_not_a_development_failure_report(self):
        from scripts import candidate_diagnostics as diagnostic
        with tempfile.TemporaryFile() as stream, mock.patch.dict(observer.os.environ, {
                diagnostic.FD_ENV: str(stream.fileno()), diagnostic.OP_ENV: 'sha256:' + 'f' * 64}), \
                mock.patch.object(observer, '_require', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                observer.main(['--profile', 'FRESH_BASE', '--binding', '{}'])
            self.assertEqual(stream.tell(), 0)

    def prepared_inputs(self, *, readback_release=1, replace_archive=False):
        from installer import formal_bootstrap
        from release import formal_input_readback
        from scripts import published_formal_entry
        stack = self.enterContext(ExitStack())
        root = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        execution, readback = root / 'execution', root / 'readback'
        execution.mkdir()
        readback.mkdir()
        archive, sidecar, gh = b'original-qualified-archive', b'original-sidecar', b'fixed-gh-deb'
        sidecar_path, gh_path = root / 'sidecar.json', root / 'gh.deb'
        sidecar_path.write_bytes(sidecar)
        gh_path.write_bytes(gh)
        product_path = root / 'original-product.py'
        product_path.write_bytes(b'# original published module\n')
        product_digest = sha256_bytes(product_path.read_bytes())
        loaded = materials._loaded(root / 'candidate')
        loaded.candidate_input.update(installer_materials_sha256=sha256_bytes(archive),
            release_manifest_sha256='sha256:' + '1' * 64, deployment_contract_sha256='sha256:' + '2' * 64)
        loaded.materials = SimpleNamespace(verified=SimpleNamespace(files=(SimpleNamespace(
            path='installer/production.py', size=product_path.stat().st_size, sha256=product_digest),)),
            material=lambda name: product_path)

        def observed_readback(**kwargs):
            # Synthetic transport fixture only: real factory copy/hash checks
            # still run and no authority or PASS is supplied to an executor.
            (readback / 'installer-materials.tar').write_bytes(b'portable-role-confusion' if replace_archive else archive)
            (readback / 'release-attestation.sigstore.json').write_bytes(sidecar)
            (readback / 'published-release-readback.json').write_bytes(canonical_json_bytes({'id': readback_release}))
        stack.enter_context(mock.patch.object(formal_input_readback, 'read_published_inputs', side_effect=observed_readback))
        stack.enter_context(mock.patch.object(published_formal_entry, '_PinnedWindowsGh'))
        stack.enter_context(mock.patch.object(formal_bootstrap, 'GH_DEB_SHA256', sha256_bytes(gh).removeprefix('sha256:')))
        return dict(loaded=loaded, execution_root=execution, readback_root=readback, asset_root=root,
            sidecar=sidecar_path, sidecar_digest=sha256_bytes(sidecar), release_id=1,
            windows_gh=root / 'unused-synthetic-gh.exe', linux_gh_package=gh_path)

    def test_prepared_product_is_distinct_and_all_held_input_digests_are_actual(self):
        options = self.prepared_inputs()
        additions, subject_digest = planning.prepare_published_planning_inputs(**options)
        for name, digest in additions.items():
            self.assertEqual(sha256_bytes((options['execution_root'] / name).read_bytes()), digest)
        subject = options['execution_root'] / 'published-planning/subject.json'
        self.assertEqual(sha256_bytes(subject.read_bytes()), subject_digest)
        self.assertFalse((options['execution_root'] / 'installer/production.py').exists())
        self.assertTrue((options['execution_root'] / 'published-planning/published-product/installer/production.py').is_file())

    def test_release_sidecar_archive_role_and_oversized_archive_fail_closed(self):
        from release.materials import MaterialContractError
        for difference in ('release', 'sidecar', 'archive-role', 'archive-size'):
            options = self.prepared_inputs(readback_release=2 if difference == 'release' else 1,
                                           replace_archive=difference == 'archive-role')
            if difference == 'sidecar':
                options['sidecar_digest'] = 'sha256:' + 'f' * 64
            with self.subTest(difference=difference), \
                    mock.patch.object(planning, 'MAX_PUBLISHED_ARCHIVE_BYTES', 4 if difference == 'archive-size'
                                      else planning.MAX_PUBLISHED_ARCHIVE_BYTES), \
                    self.assertRaises((ValueError, MaterialContractError)):
                planning.prepare_published_planning_inputs(**options)


if __name__ == '__main__':
    unittest.main()
