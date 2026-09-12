"""The development allowance is exercised only in disposable synthetic ledgers."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from scripts import development_capture_scope as d

MATERIAL = {'verified_candidate_digest': 'sha256:' + '1' * 64,
            'candidate_input_digest': 'sha256:' + '2' * 64,
            'source_sha': '3' * 40, 'source_tree': '4' * 40,
            'qualification_run_id': 9, 'qualification_run_attempt': 1,
            'candidate_version': 'v2.0.0-rc.1'}


class DevelopmentAuthorizationTests(unittest.TestCase):
    def test_fixed_scope_is_separate_from_candidate_and_rejects_missing_or_unknown(self):
        from scripts.candidate_batch_session import CAPTURE_LEDGERS
        self.assertNotIn(d.AUTHORIZATION, CAPTURE_LEDGERS)
        self.assertEqual(d.LEDGER, Path('E:/4d9046d98ee16d687ac9d8945a42a97e32bd87b7ea264d950d48356db9951cc8'))
        for value in (None, '', d.AUTHORIZATION + '_NEXT', *CAPTURE_LEDGERS):
            with self.assertRaisesRegex(d.DevelopmentScopeError, 'AUTHORIZATION_INVALID'):
                d.reserve_development_capture(value, material_identity=MATERIAL)


@unittest.skipUnless(os.name == 'nt', 'real Windows private directory and process locking')
class DevelopmentCaptureTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir='E:/')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / ('a' * 64)
        patch = mock.patch.object(d, 'LEDGER', self.root)
        patch.start()
        self.addCleanup(patch.stop)

    def reserve(self):
        value = d.reserve_development_capture(d.AUTHORIZATION, material_identity=MATERIAL)
        self.addCleanup(value.close)
        return value

    def test_one_live_owner_six_spent_attempts_including_missing_results(self):
        first = self.reserve()
        self.assertEqual(first.index, 1)
        # The existing Windows directory hold may reject the second owner
        # before it reaches the byte lock; either path must fail closed.
        with self.assertRaisesRegex(d.DevelopmentScopeError, 'SCOPE_(BUSY|UNAVAILABLE)'):
            self.reserve()
        first.close()
        initial_metadata = (self.root / 'scope.json').read_bytes()
        paths = [first.path]
        for expected in range(2, 7):
            reservation = self.reserve()
            self.assertEqual(reservation.index, expected)
            self.assertEqual(reservation.deadline, first.deadline)
            paths.append(reservation.path)
            reservation.close()
        self.assertEqual(len(set(paths)), 6)
        self.assertTrue(all(path.is_dir() for path in paths))
        self.assertEqual((self.root / 'scope.json').read_bytes(), initial_metadata)
        with self.assertRaisesRegex(d.DevelopmentScopeError, 'BUDGET_EXHAUSTED'):
            self.reserve()

    def test_expiry_and_clock_rollback_do_not_open_another_capture(self):
        first = self.reserve()
        first.close()
        metadata = json.loads((self.root / 'scope.json').read_bytes())
        for monotonic_now, utc_now in (
            (metadata['created_monotonic'] + d.SCOPE_SECONDS, metadata['created_utc_seconds'] + d.SCOPE_SECONDS),
            (metadata['created_monotonic'] - 1, metadata['created_utc_seconds']),
            (metadata['created_monotonic'], metadata['created_utc_seconds'] - 1),
        ):
            with mock.patch.object(d.time, 'monotonic', return_value=monotonic_now), mock.patch.object(d.time, 'time', return_value=utc_now):
                with self.assertRaisesRegex(d.DevelopmentScopeError, 'SCOPE_EXPIRED'):
                    self.reserve()
        self.assertFalse((self.root / d.SLOTS[1]).exists())

    def test_existing_empty_ledger_is_not_reinitialized(self):
        self.root.mkdir()
        with self.assertRaises(d.DevelopmentScopeError):
            self.reserve()
        self.assertFalse((self.root / 'scope.json').exists())
        self.assertFalse((self.root / d.SLOTS[0]).exists())

    def test_wrong_purpose_is_rejected_without_repairing_metadata(self):
        first = self.reserve()
        first.close()
        path = self.root / 'scope.json'
        value = json.loads(path.read_bytes())
        value['purpose'] = 'CANDIDATE_ACCEPTANCE'
        raw = json.dumps(value).encode()
        path.write_bytes(raw)
        with self.assertRaisesRegex(d.DevelopmentScopeError, 'SCOPE_INVALID'):
            self.reserve()
        self.assertEqual(path.read_bytes(), raw)
        self.assertFalse((self.root / d.SLOTS[1]).exists())

    def test_material_source_qualification_or_candidate_change_cannot_use_next_round(self):
        first = self.reserve()
        first.close()
        for key, value in (('source_sha', '5' * 40), ('source_tree', '6' * 40),
                           ('qualification_run_id', 10), ('verified_candidate_digest', 'sha256:' + '7' * 64)):
            with self.assertRaisesRegex(d.DevelopmentScopeError, 'MATERIAL_CHANGED'):
                d.reserve_development_capture(d.AUTHORIZATION, material_identity={**MATERIAL, key: value})
        self.assertFalse((self.root / d.SLOTS[1]).exists())

    def test_reservation_cannot_be_reused_after_close(self):
        first = self.reserve()
        first.require_open()
        first.close()
        with self.assertRaisesRegex(d.DevelopmentScopeError, 'SCOPE_CLOSED'):
            first.require_open()


if __name__ == '__main__':
    unittest.main()
