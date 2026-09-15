"""Exercise the real production report across the strict Runner boundary."""
import json
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from durability.doctor import DOCTOR_CHECK_IDS, DoctorCheck, DoctorReport, DoctorStatus
from installer.platform_bootstrap import ProductionPlatformBootstrap
from installer.production import (
    CandidatePlatformCommandObserver,
    CandidateReleasePort,
    LocalDockerCommandRunner,
    ProductionDoctorAcceptance,
    ProductionInstallerComposition,
)
from installer.runtime import InstallOutcome, InstallTransportSource
from installer.tests.test_platform_bootstrap import (
    RunnerFixture,
    SequenceFacts,
    fresh_base_facts,
    qualified_existing_facts,
)
from scripts import candidate_diagnostics as diagnostics
from scripts import candidate_profile_runner as runner
from scripts import development_profile_runner as development
from scripts.tests.test_candidate_profile_runner import _context, _identity, _loaded
from updater.oci import (
    REQUIRED_IMAGE_REPOSITORIES,
    AcquiredRuntimeImage,
    ImageAcquisitionReceipt,
)


def production_output(profile):
    loaded = _loaded(Path('.'))
    final = qualified_existing_facts(installed=('docker.io', 'docker-compose-v2', 'postgresql-client-16'))
    initial = {
        'FRESH_BASE': fresh_base_facts(),
        'DOCKER_BASE': qualified_existing_facts(compose=False, postgres_major=None),
        'RUNTIME_BASE_OFFLINE': final,
    }[profile]
    platform_observer = CandidatePlatformCommandObserver(RunnerFixture())
    bootstrap = ProductionPlatformBootstrap(
        facts_collector=SequenceFacts(initial, initial, final), runner=platform_observer,
        clock=lambda: '2026-08-25T12:00:00Z', lock_factory=lambda: nullcontext())
    plan = bootstrap.plan(transport_source=(InstallTransportSource.LOCAL_BUNDLE
        if profile == 'RUNTIME_BASE_OFFLINE' else InstallTransportSource.GITHUB))
    receipt = bootstrap.execute(plan, accepted_plan_digest=plan.plan_digest)
    doctor = ProductionDoctorAcceptance(releases=mock.Mock(), compatibility=mock.Mock())
    doctor._latest_report = DoctorReport(
        checked_at='2026-08-25T12:00:30Z', overall_status=DoctorStatus.PASS,
        checks=tuple(DoctorCheck(name, DoctorStatus.PASS, 'CHECK_PASSED', 'info',
            'Check passed', 'runtime', '', '2026-08-25T12:00:30Z') for name in DOCTOR_CHECK_IDS),
        instance_id='12345678-1234-4678-9234-567812345678', deployment_profile='animemo-standard-v1',
        compatibility=SimpleNamespace(as_dict=lambda: {'overallStatus': 'COMPATIBLE'}))
    acceptance = []
    for name, adapter in (
        ('application.journal-crud', 'django-domain-service-transaction-rollback'),
        ('service.api.health', 'immutable-compose-api-health'),
        ('service.web.health', 'immutable-compose-web-health')):
        body = {'name': name, 'result': 'PASS',
            'evidence': {'adapter': adapter, 'observationDigest': _identity({'name': name})}}
        acceptance.append({**body, 'receiptDigest': _identity(body)})
    doctor._latest_canonical_acceptance = tuple(acceptance)
    images = ImageAcquisitionReceipt(loaded.materials.identity_digest, '2' * 64,
        tuple(AcquiredRuntimeImage(item.role, REQUIRED_IMAGE_REPOSITORIES[item.role] + '@' + item.digest,
            REQUIRED_IMAGE_REPOSITORIES[item.role] + '@' + item.digest) for item in loaded.images.images), '3' * 64)
    releases = CandidateReleasePort.__new__(CandidateReleasePort)
    releases.image_receipt_for = lambda _: images
    delegate = mock.Mock()
    delegate.run.side_effect = [SimpleNamespace(returncode=0, stdout='true'),
        SimpleNamespace(returncode=0, stdout='AF_NETLINK AF_UNIX'),
        *[SimpleNamespace(returncode=0, stdout=json.dumps([
            image.canonical_reference.removeprefix('docker.io/library/')])) for image in images.images]]
    composition = ProductionInstallerComposition(runtime=object(), releases=releases, platform=object(),
        candidate_doctor=doctor, candidate_platform_observer=platform_observer,
        candidate_command_runner=LocalDockerCommandRunner(delegate))
    result = {'outcome': 'SUCCEEDED', 'completedSteps': ['roots.prepare', 'configuration.publish',
        'release.stage', 'services.prepare', 'database.migrate', 'application.bootstrap',
        'runtime.start', 'runtime.validate', 'updater.adopt-and-publish-locator', 'doctor.accept']}
    output = {'platformPlan': plan.as_dict(), 'platformBootstrapReceipt': receipt.as_dict(),
        'strictPostProvisionQualification': True, 'installerPlanDigest': 'sha256:' + '8' * 64,
        'installerResult': result}
    output['productionExecutionObservation'] = composition.candidate_profile_execution_observation(
        platform_plan=plan, platform_receipt=receipt,
        installer_plan=SimpleNamespace(plan_digest=output['installerPlanDigest'], release=object()),
        installer_result=SimpleNamespace(outcome=InstallOutcome.SUCCEEDED, as_dict=lambda: result))
    context = _context(profile)
    context['initial_platform_state'] = {'docker_present': initial.docker_cli_present and initial.docker_daemon_healthy,
        'runtime_dependencies_present': initial.compose_v2_present and initial.pg_dump_major is not None and initial.psql_major is not None,
        'network_allowed': plan.network_policy != 'DENY_ALL'}
    return loaded, context, output


class CandidateProductionReportTests(unittest.TestCase):
    def test_all_profiles_production_reports_pass_real_runner_validation(self):
        for profile in runner.PROFILES:
            with self.subTest(profile=profile):
                loaded, context, output = production_output(profile)
                draft = runner.build_profile_receipt(loaded=loaded, profile=profile, context=context,
                    installer_output=json.loads(json.dumps(output)), started_at='2026-08-25T12:00:00Z',
                    completed_at='2026-08-25T12:01:00Z')
                self.assertEqual(draft['result'], 'PASS')

    def test_runner_failure_exposes_only_closed_code_locations_and_counts(self):
        loaded, context, output = production_output('FRESH_BASE')
        output['productionExecutionObservation']['doctorReceiptDigest'] = 'sha256:' + 'f' * 64
        sentinel = 'synthetic-private-report-value'
        output['productionExecutionObservation']['doctorReport']['checks'][0]['summary'] = sentinel
        binding = {'workload_mode': 'CLEAN_PREACCEPTANCE',
            'plan_digest': 'sha256:' + '1' * 64, 'session_id': '2' * 32,
            'execution_source_sha': '3' * 40, 'execution_source_tree': '4' * 40,
            'execution_inventory_digest': 'sha256:' + '5' * 64,
            'verified_candidate_digest': loaded.verified_digest,
            'material_source_sha': loaded.candidate_input['source_sha'],
            'material_source_tree': loaded.candidate_input['source_tree'],
            'qualification_run_id': loaded.candidate_input['qualification_run_id']}
        source = {'execution_inventory_digest': binding['execution_inventory_digest']}
        output['developmentServiceSourceObservation'] = source
        operation = 'sha256:' + 'a' * 64
        with tempfile.TemporaryDirectory() as temporary, tempfile.TemporaryFile() as stream:
            writer = diagnostics.DiagnosticWriter(stream.fileno(), operation)
            destination = Path(temporary) / 'report.json'
            with (mock.patch.object(development, 'inherited_writer', return_value=writer),
                  mock.patch.object(development, 'load_verified_candidate', return_value=loaded),
                  mock.patch.object(development, 'expected_service_observation', return_value=source),
                  mock.patch.object(development, 'OUTPUT', destination),
                  mock.patch.object(runner, '_execute_profile_workload', return_value=(loaded, context, output,
                      '2026-08-25T12:00:00Z', '2026-08-25T12:01:00Z'))):
                self.assertEqual(development.main(['--profile', 'FRESH_BASE', '--binding', json.dumps(binding)]), 2)
            self.assertFalse(destination.exists())
            stream.seek(0)
            reader = diagnostics.DiagnosticReader(operation)
            while frame := diagnostics.read_frame(stream):
                reader.accept(*frame)
        public = reader.public()
        self.assertIn('CANDIDATE_PROFILE_DOCTOR_RECEIPT_MISMATCH', public['errors'])
        self.assertTrue(any(event['kind'] == 'FAULT' and event['module'] == 'scripts.candidate_profile_runner'
            for event in public['events']))
        counts = next(event for event in public['events'] if event['kind'] == 'REPORT_COUNTS')
        self.assertEqual(counts['doctor_checks'], len(DOCTOR_CHECK_IDS))
        self.assertNotIn(sentinel, json.dumps(public))
