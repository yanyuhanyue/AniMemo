"""Fixed retry policy against synthetic public receipts and temp-only entries."""
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from scripts import runtime_ui_retry_policy as policy
from scripts.guest_sudo_session import ControllerFailure


class RetryPolicyTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix='animemo-retry-synthetic-')))
        self.receipt = self.root / 'previous.json'
        self.enterContext(mock.patch.multiple(policy,
            PREVIOUS_ENTRY_ID='ANIMEMO_TEST_ONLY_PREVIOUS_V1',
            RETRY_ENTRY_ID='ANIMEMO_TEST_ONLY_RETRY_V2',
            RETRY_PERMIT_ID='ANIMEMO_TEST_ONLY_RETRY_RUN_V2',
            RETRY_DEADLINE='2030-01-01T17:00:00Z',
            RETRY_ENTRY_ROOT=self.root / hashlib.sha256(b'ANIMEMO_TEST_ONLY_RETRY_V2').hexdigest()))
        self.report = {'failure_code': 'BATCH_CONFIRMATION_CANCELLED',
            'runtime_target_handoff_stage': 'TARGET_BOUND_B_NOT_APPROVED', 'credential_session': None,
            'profile_results': {'RUNTIME_BASE_OFFLINE': {'failure_code': None, 'status': 'NOT_RUN'}},
            'runtime_target_handoff': {'entry_id': policy.PREVIOUS_ENTRY_ID, 'A_approved': True,
                'B_approved': False, 'entry_spent': True, 'closed': True, 'holds_released': True}}
        self.enterContext(mock.patch.object(policy, 'PREVIOUS_REPORT', self.receipt))
        self.args = SimpleNamespace(runtime_ui_retry=True, authorization_id=policy.RETRY_PERMIT_ID,
            runtime_authorization_deadline=policy.RETRY_DEADLINE, result_only=True)
        self.bind_receipt()

    def bind_receipt(self):
        raw = json.dumps(self.report).encode()
        self.receipt.write_bytes(raw)
        self.enterContext(mock.patch.object(policy, 'PREVIOUS_REPORT_BYTES', len(raw)))
        self.enterContext(mock.patch.object(policy, 'PREVIOUS_REPORT_SHA256', hashlib.sha256(raw).hexdigest()))

    def test_original_route_does_not_read_receipt_or_recover_entry(self):
        self.args.runtime_ui_retry = False
        with mock.patch.object(Path, 'open', side_effect=AssertionError('no retry IO')):
            self.assertIsNone(policy.request_retry(self.args))

    def test_fixed_id_is_independent_of_session_source_and_label(self):
        result = policy.request_retry(self.args)
        self.assertNotEqual(result['retry_entry_id'], policy.PREVIOUS_ENTRY_ID)
        self.assertEqual(Path(result['retry_entry_root']).name,
                         hashlib.sha256(policy.RETRY_ENTRY_ID.encode()).hexdigest())
        self.args.execution_source_sha = 'f' * 40
        self.args.session_id = 'e' * 32
        self.assertEqual(policy.request_retry(self.args), result)
        self.assertEqual(result['round_limit'], 1)
        self.assertEqual(result['capture_limit'], 1)

    def test_scope_drift_fails_before_any_receipt_read(self):
        for key, value in (('authorization_id', policy.RETRY_PERMIT_ID + '_NEXT'),
                           ('result_only', False)):
            with self.subTest(key=key):
                args = SimpleNamespace(**{**vars(self.args), key: value})
                with mock.patch.object(Path, 'open', side_effect=AssertionError('no scope drift IO')):
                    with self.assertRaisesRegex(ControllerFailure, 'FIXED_SCOPE_REQUIRED'):
                        policy.request_retry(args)

    def test_new_declared_window_needs_fresh_consent_without_changing_old_window(self):
        self.args.runtime_authorization_deadline = '2026-10-02T17:00:00Z'
        proposed = policy.request_retry(self.args)
        self.assertEqual(proposed['authorization_deadline'], self.args.runtime_authorization_deadline)
        self.assertTrue(proposed['new_window_requires_explicit_user_approval'])
        self.assertTrue(proposed['requires_fresh_native_A_and_B'])
        self.assertEqual(policy.public_policy()['authorization_deadline'], policy.RETRY_DEADLINE)
        self.args.runtime_authorization_deadline = 'tomorrow'
        from scripts.runtime_development_boundary import RuntimeBoundaryError
        with mock.patch.object(Path, 'open', side_effect=AssertionError('no invalid deadline IO')):
            with self.assertRaisesRegex(RuntimeBoundaryError, 'DEADLINE_INVALID'):
                policy.request_retry(self.args)

    def test_changed_previous_receipt_fails_closed(self):
        self.receipt.write_bytes(self.receipt.read_bytes() + b' ')
        with self.assertRaisesRegex(ControllerFailure, 'RECEIPT_CHANGED'):
            policy.request_retry(self.args)

    def test_prior_guest_action_or_open_holds_cannot_qualify(self):
        for kind in ('B', 'capture', 'runtime', 'holds'):
            with self.subTest(kind=kind):
                if kind == 'B':
                    self.report['runtime_target_handoff']['B_approved'] = True
                elif kind == 'capture':
                    self.report['credential_session'] = {'synthetic': True}
                elif kind == 'runtime':
                    self.report['profile_results']['RUNTIME_BASE_OFFLINE']['status'] = 'PASS'
                else:
                    self.report['runtime_target_handoff']['holds_released'] = False
                self.bind_receipt()
                with self.assertRaisesRegex(ControllerFailure, 'NOT_ELIGIBLE'):
                    policy.request_retry(self.args)

    def test_new_entry_is_exclusive_and_old_spent_entry_is_untouched(self):
        from scripts.tests.test_runtime_target_handoff import RuntimeTargetHandoffTests, redigest
        from scripts import runtime_target_handoff as target
        from dataclasses import replace
        fixture = RuntimeTargetHandoffTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        old_root = fixture.entry
        old_root.mkdir()
        (old_root / 'attempt.json').write_bytes(b'SYNTHETIC_OLD_SPENT_UNCHANGED')
        previous = (old_root / 'attempt.json').read_bytes()
        fixture.entry = fixture.root / 'new-fixed-retry'
        fixture.args.runtime_ui_retry = True
        fixture.args.result_only = True
        fixture.args.authorization_id = policy.RETRY_PERMIT_ID
        fixture.args.runtime_authorization_deadline = policy.RETRY_DEADLINE
        fixture.plan = redigest(replace(fixture.plan, runtime_authorization_deadline=policy.RETRY_DEADLINE))
        with mock.patch.object(policy, 'RETRY_ENTRY_ROOT', fixture.entry), \
                mock.patch.object(target.time, 'time', return_value=1790859600):  # 2026-10-01 13:00 UTC
            value, _ = fixture.preparation()
            self.assertEqual(value.record['entry_id'], policy.RETRY_ENTRY_ID)
            value.bind(fixture.provider, fixture.plan)
            self.assertEqual(value.record['target']['entry_id'], policy.RETRY_ENTRY_ID)
            value.close({'status': 'SYNTHETIC_B_CANCELLED'})
            with mock.patch.object(target, 'WindowsConsoleCapture') as console, \
                    mock.patch.object(target, 'create_windows_private_named_directory',
                        side_effect=lambda parent, name: (parent / name).mkdir()):
                with self.assertRaises(FileExistsError):
                    target.begin_preparation(fixture.args)
                console.return_value.confirm_batch.assert_called_once()
        self.assertEqual((old_root / 'attempt.json').read_bytes(), previous)
        self.assertTrue((fixture.entry / 'attempt.json').exists())
        self.assertTrue((fixture.entry / 'completion.json').exists())
