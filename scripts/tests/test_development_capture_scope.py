"""The development allowance is exercised only in disposable synthetic ledgers."""
import hashlib
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

    def test_https_clock_accepts_only_fresh_fixed_origin_headers_without_credentials(self):
        from email.message import Message
        from types import SimpleNamespace

        for kind in ('valid', 'cached', 'duplicate-age', 'redirected', 'duplicate', 'missing', 'http-error'):
            with self.subTest(kind=kind):
                headers = Message()
                if kind != 'missing':
                    headers.add_header('Date', 'Sun, 13 Sep 2026 01:00:00 GMT')
                if kind == 'duplicate':
                    headers.add_header('Date', 'Sun, 13 Sep 2026 01:00:00 GMT')
                if kind == 'cached':
                    headers.add_header('Age', '5')
                if kind == 'duplicate-age':
                    headers.add_header('Age', '0')
                    headers.add_header('Age', '20')
                response = SimpleNamespace(headers=headers, status=403 if kind == 'http-error' else 200)
                captured = []
                def open_request(request, timeout):
                    captured.append(request)
                    self.assertEqual(timeout, 10)
                    self.assertTrue(request.full_url.startswith('https://api.github.com/rate_limit?animemo_clock_nonce='))
                    self.assertNotIn('Authorization', request.headers)
                    response.geturl = lambda: 'https://other.invalid/' if kind == 'redirected' else request.full_url
                    context = mock.MagicMock()
                    context.__enter__.return_value = response
                    return context
                with mock.patch('urllib.request.build_opener', return_value=SimpleNamespace(open=open_request)) as build:
                    if kind == 'valid':
                        self.assertEqual(d._github_utc_upper_bound(), 1789261201)
                    else:
                        with self.assertRaisesRegex(d.DevelopmentScopeError, 'CLOCK_AUTHORITY_UNAVAILABLE'):
                            d._github_utc_upper_bound()
                self.assertEqual(len(captured), 1)
                self.assertEqual(build.call_args.args[0].proxies, {})
                self.assertIsNone(build.call_args.args[1].redirect_request())


@unittest.skipUnless(os.name == 'nt', 'real Windows private directory and process locking')
class DevelopmentCaptureTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir='E:/')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / ('a' * 64)
        patch = mock.patch.object(d, 'LEDGER', self.root)
        patch.start()
        self.addCleanup(patch.stop)
        patch = mock.patch.object(d, '_github_utc_upper_bound',
            side_effect=d.DevelopmentScopeError('DEVELOPMENT_CLOCK_AUTHORITY_UNAVAILABLE'))
        self.clock_authority = patch.start()
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
            self.assertLessEqual(reservation.deadline, first.deadline + 0.1)
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
                with self.assertRaisesRegex(d.DevelopmentScopeError, 'SCOPE_EXPIRED|CLOCK_AUTHORITY_UNAVAILABLE'):
                    self.reserve()
        self.assertFalse((self.root / d.SLOTS[1]).exists())

    def test_reboot_uses_next_spent_slot_and_original_utc_deadline_without_metadata_edit(self):
        with mock.patch.object(d.time, 'monotonic', return_value=40000), mock.patch.object(d.time, 'time', return_value=100000):
            first = self.reserve()
            first.close()
        metadata = (self.root / 'scope.json').read_bytes()
        # The previous result is absent, exactly as after an interrupted owner.
        self.clock_authority.side_effect = None
        self.clock_authority.return_value = 140001
        with mock.patch.object(d.time, 'monotonic', return_value=100), mock.patch.object(d.time, 'time', return_value=140000):
            second = self.reserve()
            self.assertEqual(second.index, 2)
            self.assertEqual(second.deadline, 3299)
            self.assertEqual(second.time_observation['original_expires_utc_seconds'], 143200)
            self.assertEqual(second.time_observation['authority'], 'GITHUB_HTTPS_DATE')
            second.close()
        self.assertEqual((self.root / 'scope.json').read_bytes(), metadata)
        self.assertEqual({p.name for p in self.root.iterdir() if p.is_dir()}, set(d.SLOTS[:2]))

    def test_reboot_cannot_hide_wall_clock_rollback_or_extend_expired_utc_budget(self):
        with mock.patch.object(d.time, 'monotonic', return_value=40000), mock.patch.object(d.time, 'time', return_value=100000):
            self.reserve().close()
        self.clock_authority.side_effect = None
        for local_utc, trusted_utc, code in ((140000, 144000, 'CLOCK_AUTHORITY_MISMATCH'),
                                          (143199, 143201, 'SCOPE_EXPIRED')):
            self.clock_authority.return_value = trusted_utc
            with mock.patch.object(d.time, 'monotonic', return_value=100), mock.patch.object(d.time, 'time', return_value=local_utc):
                with self.assertRaisesRegex(d.DevelopmentScopeError, code):
                    self.reserve()
            self.assertFalse((self.root / d.SLOTS[1]).exists())

    def test_reboot_with_larger_new_uptime_still_clamps_to_original_utc_deadline(self):
        self.clock_authority.side_effect = None
        self.clock_authority.return_value = 140001
        deadline, observation = d.scope_deadline({'created_monotonic': 10, 'created_utc_seconds': 100000},
            monotonic_now=1000, utc_now=140000)
        self.assertEqual(deadline, 4199)
        self.assertTrue(observation['clock_epoch_changed'])

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

    def prepare_confirmed_extension(self):
        with mock.patch.object(d.time, 'monotonic', return_value=40000), mock.patch.object(d.time, 'time', return_value=100000):
            for _ in range(3):
                self.reserve().close()
        raw = (self.root / 'scope.json').read_bytes()
        for name, value in (('EXTENSION_SCOPE_SHA256', hashlib.sha256(raw).hexdigest()),
                            ('EXTENSION_START_UTC', 145000), ('EXTENSION_EXPIRES_UTC', 188200)):
            patch = mock.patch.object(d, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        self.clock_authority.side_effect = None
        self.clock_authority.return_value = 145010
        return raw

    def test_confirmed_extension_keeps_original_metadata_and_only_remaining_slots(self):
        raw = self.prepare_confirmed_extension()
        with mock.patch.object(d.time, 'monotonic', return_value=100), mock.patch.object(d.time, 'time', return_value=145009):
            for index in (4, 5, 6):
                reservation = self.reserve()
                self.assertEqual(reservation.index, index)
                self.assertEqual(reservation.deadline, 43290)
                self.assertEqual(reservation.time_observation['original_expires_utc_seconds'], 143200)
                self.assertEqual(reservation.time_observation['effective_expires_utc_seconds'], 188200)
                reservation.close()
            with self.assertRaisesRegex(d.DevelopmentScopeError, 'BUDGET_EXHAUSTED'):
                self.reserve()
        self.assertEqual((self.root / 'scope.json').read_bytes(), raw)
        self.assertEqual({p.name for p in self.root.iterdir() if p.is_dir()}, set(d.SLOTS))
        self.assertTrue(all(not tuple((self.root / slot).iterdir()) for slot in d.SLOTS[:3]))

    def test_extension_rejects_expiry_rollback_time_failure_and_different_base(self):
        raw = self.prepare_confirmed_extension()
        for local, trusted, code in ((188200, 188200, 'SCOPE_EXPIRED'),
                                     (188199, 188201, 'SCOPE_EXPIRED'),
                                     (144999, 145001, 'SCOPE_EXPIRED'),
                                     (145009, 145100, 'CLOCK_AUTHORITY_MISMATCH'),
                                     (145009, None, 'CLOCK_AUTHORITY_UNAVAILABLE')):
            self.clock_authority.side_effect = d.DevelopmentScopeError('DEVELOPMENT_CLOCK_AUTHORITY_UNAVAILABLE') if trusted is None else None
            self.clock_authority.return_value = trusted
            with mock.patch.object(d.time, 'monotonic', return_value=100), mock.patch.object(d.time, 'time', return_value=local):
                with self.assertRaisesRegex(d.DevelopmentScopeError, code):
                    self.reserve()
            self.assertFalse((self.root / d.SLOTS[3]).exists())
        with mock.patch.object(d, 'EXTENSION_SCOPE_SHA256', 'f' * 64):
            with self.assertRaisesRegex(d.DevelopmentScopeError, 'SCOPE_EXPIRED'):
                d.scope_deadline(json.loads(raw), monotonic_now=100, utc_now=145009)
        self.assertEqual((self.root / 'scope.json').read_bytes(), raw)

    def test_extension_does_not_restore_missing_spent_slots(self):
        self.prepare_confirmed_extension()
        # Disposable synthetic ledger only: losing the third spent sentinel
        # must not let the fixed extension recapture that attempt.
        (self.root / d.SLOTS[2]).rmdir()
        with mock.patch.object(d.time, 'monotonic', return_value=100), mock.patch.object(d.time, 'time', return_value=145009):
            with self.assertRaisesRegex(d.DevelopmentScopeError, 'SCOPE_INVALID'):
                self.reserve()
        self.assertFalse((self.root / d.SLOTS[2]).exists())


if __name__ == '__main__':
    unittest.main()
