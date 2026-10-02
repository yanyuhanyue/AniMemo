"""Public evidence and authenticated floor regressions; no fake crypto PASS."""
import copy
import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path

from bootstrap_kit import trust
from release.trust_bootstrap import load_initial_trust_kit
from scripts.tests.trust_kit_fixture import create_test_initial_trust_kit


class TufEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.profile, _ = load_initial_trust_kit(create_test_initial_trust_kit(self.root))
        self.claims = {}
        for domain in ('github', 'sigstore'):
            self.claims[domain] = {field: getattr(self.profile, domain + suffix) for field, suffix in (
                ('tufRootVersion', '_tuf_root_version'), ('timestampVersion', '_tuf_timestamp_version'),
                ('snapshotVersion', '_tuf_snapshot_version'), ('targetsVersion', '_tuf_targets_version'),
                ('tufRootSha256', '_tuf_root_sha256'), ('trustedRootSha256', '_trusted_root_sha256'))}

    def test_unchanged_roles_and_timestamp_only_update_are_allowed(self):
        trust._enforce_profile_floor(self.claims, self.profile)
        self.claims['github']['timestampVersion'] += 1
        trust._enforce_profile_floor(self.claims, self.profile)

    def test_each_domain_role_cannot_rollback(self):
        for domain in self.claims:
            for field in ('tufRootVersion', 'timestampVersion', 'snapshotVersion', 'targetsVersion'):
                claims = copy.deepcopy(self.claims)
                claims[domain][field] -= 1
                with self.subTest(domain=domain, field=field), self.assertRaisesRegex(ValueError, 'VERSION_ROLLBACK'):
                    trust._enforce_profile_floor(claims, self.profile)

    def test_same_version_root_or_target_cannot_change_identity(self):
        for domain in self.claims:
            for field in ('tufRootSha256', 'trustedRootSha256'):
                claims = copy.deepcopy(self.claims)
                claims[domain][field] = 'sha256:'+'0'*64
                with self.subTest(domain=domain, field=field), self.assertRaisesRegex(ValueError, 'SAME_VERSION'):
                    trust._enforce_profile_floor(claims, self.profile)

    def test_public_original_is_exact_and_unverified_until_crypto_closes(self):
        raw = b'{"signed":{"_type":"root"},"signatures":[]}'
        with ExitStack() as holds:
            evidence = trust._ProtocolEvidence(self.root, holds, self.profile, self.profile.verifier_identity)
            observed = evidence.observe_fetch(lambda url, maximum: raw)
            result = observed(trust.original._TRACKS['github']['bootstrap'], 1024)
            self.assertIs(result, raw)
            self.assertEqual((evidence.root/'public-001.json').read_bytes(), raw)
            evidence.finish()
            index = json.loads((evidence.root/'evidence-index.json').read_bytes())
            self.assertEqual(index['status'], 'UNVERIFIED')
            self.assertEqual(index['minimumProfileIdentity'], self.profile.identity)
            self.assertIsNone(index['verificationWindow'])
            self.assertFalse((evidence.root/'actual-go-claim.json').exists())

    def test_public_byte_and_count_limits_do_not_include_profile_copy(self):
        with ExitStack() as holds:
            evidence = trust._ProtocolEvidence(self.root, holds, self.profile, self.profile.verifier_identity)
            self.assertEqual(evidence.public_count, 0)
            self.assertEqual(evidence.total, 0)
            evidence.public_count = 74
            with self.assertRaisesRegex(ValueError, 'EVIDENCE_BUDGET'):
                evidence.observe_fetch(lambda url, maximum: b'{}')('fixed-test', 2)
            self.assertFalse(list(evidence.root.glob('public-*')))

    def test_fetch_failure_keeps_no_raw_exception_or_response_claim(self):
        with ExitStack() as holds:
            evidence = trust._ProtocolEvidence(self.root, holds, None, self.profile.verifier_identity)
            def failure(url, maximum):
                raise ValueError('PRIVATE_SENTINEL')
            with self.assertRaises(ValueError):
                evidence.observe_fetch(failure)('fixed-test', 2)
            evidence.finish()
            self.assertNotIn(b'PRIVATE_SENTINEL', (evidence.root/'evidence-index.json').read_bytes())
            self.assertEqual(evidence.materials[0]['name'], 'evidence-index.json')


if __name__ == '__main__':
    unittest.main()
