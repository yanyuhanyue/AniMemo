"""DEV data loading and negative TUF seams; no cryptographic PASS is mocked."""
import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from bootstrap_kit import trust
from bootstrap_kit.safe_files import remove_owned_directory
from release import trust_bootstrap as original
from scripts.tests.trust_kit_fixture import create_test_initial_trust_kit
from updater.offline import PretrustedTrustMaterial


class LocalTrustTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        fixture = create_test_initial_trust_kit(self.root)
        self.local = self.root/'local-six-file-dto'
        self.local.mkdir()
        for name in trust._FILES:
            shutil.copyfile(fixture/name, self.local/name)

    def test_local_six_file_dto_uses_original_profile_without_production_loader(self):
        with mock.patch.object(original, 'validate_initial_trust_kit', side_effect=AssertionError('production loader used')):
            material = trust.load_local_trust(self.local)
        self.assertIs(type(material), PretrustedTrustMaterial)
        self.assertEqual(material.profile.verifier_identity,
                         original._digest((self.local/'offline-release-verifier').read_bytes()))
        self.assertNotIn('initial-trust-bootstrap.json', {item.name for item in self.local.iterdir()})
        self.assertFalse(hasattr(material, 'production_authority_granted'))

    def test_extra_missing_or_changed_root_fails_before_any_process(self):
        with mock.patch('installer.apt_diagnostics.subprocess.Popen') as launch:
            extra = self.local/'unreviewed.json'
            extra.write_bytes(b'{}')
            with self.assertRaisesRegex(ValueError, 'MEMBER_SET'):
                trust.load_local_trust(self.local)
            extra.unlink()
            root = self.local/'github-trusted-root.jsonl'
            original_bytes = root.read_bytes()
            root.unlink()
            with self.assertRaisesRegex(ValueError, 'MEMBER_SET'):
                trust.load_local_trust(self.local)
            root.write_bytes(original_bytes+b' ')
            with self.assertRaisesRegex(ValueError, 'BYTES_CHANGED'):
                trust.load_local_trust(self.local)
            launch.assert_not_called()

    def test_profile_duplicate_keys_and_mutated_identity_are_rejected(self):
        path = self.local/'trust-profile.json'
        value = json.loads(path.read_bytes())
        for data in (b'{"profileVersion":1,"profileVersion":2}',
                     original._canonical_json_bytes({**value, 'profileIdentity': 'sha256:'+'0'*64})):
            path.write_bytes(data)
            with self.assertRaises(ValueError):
                trust.load_local_trust(self.local)

    def test_refresh_existing_output_rejects_before_file_or_network_operations(self):
        client = mock.Mock()
        with mock.patch.object(trust, 'capture_process') as launch, self.assertRaisesRegex(ValueError, 'OUTPUT_EXISTS'):
            trust.refresh_trust(verifier=self.root/'missing', output=self.local,
                                client=client, deadline=time.monotonic()+60)
        launch.assert_not_called()
        client.fetch.assert_not_called()

    def test_real_capture_launch_failure_keeps_exe_suffix_and_leaves_no_output(self):
        client = mock.Mock()
        observed = []
        def refused_launch(argv, **kwargs):
            executable = Path(argv[0])
            observed.append(executable)
            self.assertEqual(executable.suffix, '.exe' if os.name == 'nt' else '')
            self.assertEqual(argv[1:], ['--version'])
            self.assertNotIn('GH_TOKEN', kwargs['env'])
            self.assertNotIn('HTTPS_PROXY', kwargs['env'])
            if os.name == 'nt':
                with self.assertRaises(PermissionError):
                    executable.write_bytes(b'changed')
            raise OSError('NONSECRET_LAUNCH_DETAIL')
        with mock.patch('installer.apt_diagnostics.subprocess.Popen', side_effect=refused_launch), \
                self.assertRaisesRegex(trust.KitTrustError, 'PROCESS_FAILED') as caught:
            trust.refresh_trust(verifier=self.local/'offline-release-verifier', output=self.root/'refreshed',
                                client=client, deadline=time.monotonic()+60)
        self.assertNotIn('NONSECRET_LAUNCH_DETAIL', str(caught.exception))
        self.assertEqual(len(observed), 1)
        client.fetch.assert_not_called()
        self.assertFalse((self.root/'refreshed').exists())
        self.assertFalse(list(self.root.glob('tuf-verifier-*')))

    def test_real_capture_cancellation_and_timeout_preserve_fixed_primary(self):
        for failure, code in ((KeyboardInterrupt(), 'CANCELLED'),
                              (subprocess.TimeoutExpired('fixed', 1), 'TIMEOUT')):
            with mock.patch('installer.apt_diagnostics.subprocess.Popen', side_effect=failure), \
                    self.assertRaisesRegex(trust.KitTrustError, code):
                trust.refresh_trust(verifier=self.local/'offline-release-verifier', output=self.root/'refreshed',
                                    client=mock.Mock(), deadline=time.monotonic()+60)
            self.assertFalse(list(self.root.glob('tuf-verifier-*')))

    def test_uncertain_cleanup_retains_inputs_without_overwriting_primary_timeout(self):
        # Negative capture result only: no verifier or signature success exists.
        result = SimpleNamespace(outcome='TIMEOUT', returncode=None,
                                 secondary_errors=('PROCESS_CLEANUP_FAILED',))
        before = set(trust._RETAINED_WORK)
        try:
            with mock.patch.object(trust, 'capture_process', return_value=result), \
                    self.assertRaisesRegex(trust.KitTrustError, 'TIMEOUT') as caught:
                trust.refresh_trust(verifier=self.local/'offline-release-verifier', output=self.root/'refreshed',
                                    client=mock.Mock(), deadline=time.monotonic()+60)
            self.assertEqual(caught.exception.secondary_errors, ('BOOTSTRAP_KIT_TUF_PROCESS_CLEANUP_FAILED',))
            retained = set(trust._RETAINED_WORK)-before
            self.assertEqual(len(retained), 1)
            root, _holds = trust._RETAINED_WORK[next(iter(retained))]
            if os.name == 'nt':
                with self.assertRaises(PermissionError):
                    (root/'verifier.exe').unlink()
        finally:
            # No process existed in this injected negative; explicit fixture teardown.
            for identity in set(trust._RETAINED_WORK)-before:
                root, holds = trust._RETAINED_WORK.pop(identity)
                holds.close()
                remove_owned_directory(root, identity)

    def test_original_acquirer_rejects_unpinned_bootstrap_before_tuf_chain(self):
        calls = []
        def unsigned_bytes(url, maximum):
            calls.append(url)
            return b'{"signed":{"version":3},"signatures":[]}'
        with self.assertRaises(original.TrustBootstrapError):
            original._acquire_track('github', fetcher=unsigned_bytes)
        self.assertEqual(calls, [original._TRACKS['github']['bootstrap']])

    def test_fetcher_rejects_foreign_url_and_expired_deadline_before_client(self):
        client = mock.Mock()
        with self.assertRaises(ValueError):
            trust.supervised_tuf_fetcher(client, deadline=time.monotonic()+30)('https://unknown.invalid/root.json', 100)
        with self.assertRaisesRegex(ValueError, 'TIMEOUT'):
            trust.supervised_tuf_fetcher(client, deadline=time.monotonic()-1)(original._TRACKS['github']['bootstrap'], 100)
        client.fetch.assert_not_called()


if __name__ == '__main__':
    unittest.main()
