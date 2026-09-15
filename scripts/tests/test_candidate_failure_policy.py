"""Synthetic failure contracts, actual framed readers, and current consumer gates."""
import copy
import io
import unittest
from contextlib import contextmanager, nullcontext
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from release import candidate, metadata_freshness
from release.candidate_failure_policy import business_failure_diagnostic
from release.test_publication_input import _loaded, _receipt
from release.publication_input import build_publish_candidate_plan, PublicationInputError
from scripts import candidate_guest_session as guest, candidate_diagnostics as diagnostics
from scripts.development_plan import DevelopmentHarnessPlan
from scripts.tests.test_candidate_diagnostics import (
    OPERATION, event, frame, business_failure_diagnostic as trusted_diagnostic,
)
from scripts.tests import test_development_platform_diagnostic as platform_fixtures


class FailurePolicyTests(unittest.TestCase):
    def test_missing_stage_conflicting_exit_or_unclassified_failure_is_fatal(self):
        original = trusted_diagnostic()
        self.assertTrue(business_failure_diagnostic(original))
        for component in diagnostics.COMPONENTS:
            for value in (None, 0, -9, 255):
                changed = copy.deepcopy(original)
                changed['exit_codes'][component] = value
                self.assertFalse(business_failure_diagnostic(changed))
        for code in ('TRANSPORT_PROTOCOL_INVALID', 'PLATFORM_BOOTSTRAP_PLAN_CHANGED',
                     'INSTALL_CANDIDATE_EGRESS_ISOLATION_UNVERIFIED', 'APT_DIAGNOSTIC_WRITE_FAILED'):
            changed = copy.deepcopy(original)
            changed['errors'].append(code)
            self.assertFalse(business_failure_diagnostic(changed))
        for stage in ('ROOT_STARTED', 'MATERIAL_VERIFIED', 'PLATFORM_PLANNED'):
            changed = copy.deepcopy(original)
            changed['events'] = [x for x in changed['events'] if x.get('stage') != stage]
            self.assertFalse(business_failure_diagnostic(changed))

    def test_apt_unknown_text_does_not_mean_unknown_delivery_but_security_or_incomplete_output_revokes(self):
        fixture = platform_fixtures.DevelopmentPlatformDiagnosticTests()
        fixture.setUp()
        report, _ = fixture.execute(fail=True)
        original = trusted_diagnostic()
        original['errors'].remove('PLATFORM_BOOTSTRAP_DOCKER_DAEMON_FAILED')
        original['errors'].append('PLATFORM_BOOTSTRAP_APT_UPDATE_FAILED')
        observation = report['apt_observations'][0]
        original['events'].append({'kind': 'APT', 'observation': observation})
        self.assertEqual(observation['categories'], ['UNKNOWN'])
        self.assertTrue(business_failure_diagnostic(original))
        for category in ('SIGNATURE', 'CERTIFICATE', 'SOURCE_IDENTITY', 'DISK', 'LOCK'):
            changed = copy.deepcopy(original)
            changed['events'][-1]['observation']['categories'] = [category]
            changed['events'][-1]['observation']['stderr']['categories'] = [category]
            self.assertFalse(business_failure_diagnostic(changed))
        for change in ({'outcome': 'TIMEOUT'}, {'outcome': 'CANCELLED'},
                       {'outcome': 'LAUNCH_FAILED'}, {'secondary_errors': ['APT_DRAIN_FAILED']}):
            changed = copy.deepcopy(original)
            changed['events'][-1]['observation'].update(change)
            self.assertFalse(business_failure_diagnostic(changed))
        changed = copy.deepcopy(original)
        changed['events'][-1]['observation']['stdout']['missing'] = True
        self.assertFalse(business_failure_diagnostic(changed))
        original['events'][-1]['observation']['stdout']['truncated'] = True
        self.assertTrue(business_failure_diagnostic(original))

    def test_legacy_receipt_remains_readable_but_cannot_authorize_current_consumers(self):
        import tempfile
        loaded = _loaded()
        receipt = _receipt(loaded)
        receipt['schema'], receipt['version'] = 'animemo.prepublication-candidate-acceptance-receipt/v4', 4
        receipt.pop('failure_policy')
        from scripts.tests.candidate_policy_fixture import resign
        resign(receipt)
        candidate.validate_aggregate_receipt(receipt)
        with self.assertRaises(PublicationInputError):
            build_publish_candidate_plan(loaded, receipt)
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'receipt.json'
            path.write_bytes(candidate.canonical_json_bytes(receipt))
            identity = SimpleNamespace(qualification_run_id=receipt['qualification_run_id'],
                candidate_sha=receipt['source_sha'], candidate_tree=receipt['source_tree'],
                candidate_version=receipt['candidate_version'],
                candidate_acceptance_receipt_sha256=candidate.aggregate_receipt_digest(receipt))
            with self.assertRaisesRegex(metadata_freshness.MetadataFreshnessError, 'authority binding differs'):
                metadata_freshness._load_candidate_acceptance_receipt(path, identity=identity,
                    current_time=datetime.now(timezone.utc))


class DiagnosticTransportTests(unittest.TestCase):
    def setUp(self):
        fixture = platform_fixtures.DevelopmentPlatformDiagnosticTests()
        fixture.setUp()
        self.fixture = fixture
        self.report, _ = fixture.execute(fail=True)
        context, binding, loaded = fixture.context, fixture.binding, fixture.loaded
        self.profile = SimpleNamespace(profile='FRESH_BASE', clone_identity=context['clone_identity'],
            snapshot_identity=context['snapshot_identity'],
            snapshot_disk_graph_identity=context['snapshot_disk_graph_identity'])
        self.plan = DevelopmentHarnessPlan(
            verified_candidate_digest=binding['verified_candidate_digest'], candidate_input_digest='sha256:' + 'a' * 64,
            qualification_run_id=binding['qualification_run_id'], source_sha=binding['material_source_sha'],
            source_tree=binding['material_source_tree'], candidate_version=loaded.candidate_input['candidate_version'],
            source_vm_identity='synthetic', source_vm_digest=context['base_vm_identity'],
            source_vm_inventory_identity=context['source_vm_inventory_identity'],
            source_disk_graph_identity=context['source_disk_graph_identity'],
            original_vm_hashes=context['original_vm_pre_hashes'], profiles=(self.profile,),
            provider_readiness_receipt_digest='sha256:' + 'b' * 64,
            session_id=binding['session_id'], plan_digest=binding['plan_digest'],
            execution_source_sha=binding['execution_source_sha'], execution_source_tree=binding['execution_source_tree'],
            execution_inventory_digest=binding['execution_inventory_digest'], platform_diagnostic=True)
        self.provider = SimpleNamespace(_candidate_diagnostics={},
            _candidate_material_authority=SimpleNamespace(loaded=loaded))

    def frames(self, *, extra_error=None):
        stages = list(diagnostics.STAGES[:9]) + ['PLATFORM_PREPARING', 'PLATFORM_PLANNED',
            'DRAFT_WRITING', 'DRAFT_WRITTEN', 'DRAFT_RETURNED']
        raw = b''.join(event(OPERATION, 'STAGE', stage=stage) for stage in stages)
        for code in ['PLATFORM_PREPARATION_FAILED', self.report['error_code']] + ([extra_error] if extra_error else []):
            raw += event(OPERATION, 'ERROR', code=code)
        raw += frame(b'R', self.report)
        for component in ('RUNTIME_RUNNER', 'ROOT', 'SUDO'):
            raw += event(OPERATION, 'EXIT', component=component, exit_code=0)
        return raw

    def read(self, raw, *, diagnostic=True):
        batch = SimpleNamespace(plan=self.plan, cancelled=SimpleNamespace(is_set=lambda: False))
        return guest._read_receipt(SimpleNamespace(stdout=io.BytesIO(raw)), operation=OPERATION,
            provider=self.provider, profile=self.profile, batch=batch, timeout=1, platform_diagnostic=diagnostic)

    def test_valid_diagnostic_fail_transport_preserves_real_apt_returncode(self):
        result = self.read(self.frames())
        self.assertEqual(result['result'], 'FAIL')
        self.assertEqual(result['apt_observations'][0]['returncode'], 1)
        self.assertIsNone(self.provider._candidate_diagnostics['FRESH_BASE']['exit_codes']['INSTALLER'])

    def test_diagnostic_fail_does_not_relax_candidate_or_extra_protocol_error(self):
        for raw, mode in ((self.frames(), False), (self.frames(extra_error='TRANSPORT_PROTOCOL_INVALID'), True)):
            with self.assertRaises(guest.WorkloadFailure) as caught:
                self.read(raw, diagnostic=mode)
            self.assertTrue(caught.exception.revoke_batch)

    def test_valid_fail_report_returns_with_secret_closed_before_provider_stop(self):
        from scripts import development_guest_session as development
        events, secret = [], bytearray(b'synthetic-diagnostic-owner')
        @contextmanager
        def operation(kind, _profile):
            self.assertTrue(secret)
            yield
            self.assertTrue(secret, 'revoke must follow the completed operation context')
            events.append(kind + '_CLOSED')
        def revoke(_code):
            secret.clear()
            events.append('OWNER_CLOSED')
        batch = mock.Mock()
        batch.operation.side_effect = operation
        batch.revoke.side_effect = revoke
        provider = mock.Mock(_candidate_diagnostics={'FRESH_BASE': {}},
            _candidate_material_authority=SimpleNamespace(loaded=self.fixture.loaded))
        supervisor = mock.Mock()
        supervisor.execute.return_value = self.report
        supervisor.close.side_effect = lambda: events.append('SUPERVISOR_CLOSED')
        with mock.patch.object(development.c, '_batch', return_value=batch), \
                mock.patch.object(development.c, '_continuing_connection'), \
                mock.patch.object(development.c, '_stage_candidate'), \
                mock.patch.object(development, 'require_development_source', return_value=SimpleNamespace(root=Path('synthetic'))), \
                mock.patch.object(development, 'hold_windows_private_file', side_effect=lambda _: nullcontext()), \
                mock.patch.object(development, '_DevelopmentWorkloadSupervisor', return_value=supervisor):
            value = development.execute_development_workload(provider, self.plan, self.profile,
                mock.sentinel.lease, mock.sentinel.disk, mock.sentinel.snapshot, Path('synthetic'),
                self.fixture.context['initial_platform_state'])
        self.assertIs(value, self.report)
        self.assertEqual(value['result'], 'FAIL')
        self.assertEqual(secret, b'')
        self.assertLess(events.index('SUPERVISOR_CLOSED'), events.index('WORKLOAD_CLOSED'))
        self.assertLess(events.index('WORKLOAD_CLOSED'), events.index('OWNER_CLOSED'))
        batch.role_result.assert_called_once_with(self.profile, 'CANDIDATE_WORKLOAD', 'FAIL')
        events.append('PROVIDER_STOP')
        self.assertLess(events.index('OWNER_CLOSED'), events.index('PROVIDER_STOP'))
