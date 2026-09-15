from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

from updater.errors import RequestRejected
from updater.source import GitHubReleaseSource

FIXTURE = Path(__file__).with_name('fixtures') / 'gh-2.97.0-verified-manifest.json'
SOURCE = '715e997e1b8ef552376148fee62b08d7c0c35364'
DIGEST = 'sha256:d31e5d4373856f435bb6d8b3e4e9ca0b96095665727b76344fc97a9edb01fb3d'
WORKFLOW = '.github/workflows/release.yml'


class GhVerificationOutputTests(unittest.TestCase):
    def parse(self, raw, **kwargs):
        return GitHubReleaseSource._verify_attestation_result(
            raw, 'release-manifest.json', DIGEST,
            expected_workflow=WORKFLOW, expected_source_commit=kwargs.get('source', SOURCE))

    def test_actual_fixed_gh_output(self):
        self.assertEqual(self.parse(FIXTURE.read_text(encoding='utf-8')), (SOURCE, SOURCE))

    def test_each_identity_field_must_match(self):
        original = json.loads(FIXTURE.read_bytes())
        for field in ('sourceRepositoryDigest', 'buildSignerDigest', 'buildConfigDigest',
                      'sourceRepositoryURI', 'sourceRepositoryIdentifier',
                      'sourceRepositoryOwnerURI', 'sourceRepositoryOwnerIdentifier',
                      'issuer', 'subjectAlternativeName', 'buildSignerURI', 'buildConfigURI',
                      'sourceRepositoryRef'):
            for replacement in (None, 1, [], '0' * 40):
                with self.subTest(field=field, replacement=replacement):
                    changed = copy.deepcopy(original)
                    changed[0]['verificationResult']['signature']['certificate'][field] = replacement
                    with self.assertRaises(RequestRejected):
                        self.parse(json.dumps(changed))
            changed = copy.deepcopy(original)
            del changed[0]['verificationResult']['signature']['certificate'][field]
            with self.subTest(field=field, missing=True), self.assertRaises(RequestRejected):
                self.parse(json.dumps(changed))

    def test_nested_and_conflicting_certificate_formats_are_rejected(self):
        original = json.loads(FIXTURE.read_bytes())
        for nested_only in (True, False):
            changed = copy.deepcopy(original)
            cert = changed[0]['verificationResult']['signature']['certificate']
            cert['extensions'] = {key: cert.pop(key) if nested_only else cert[key]
                for key in ('sourceRepositoryDigest', 'buildSignerDigest', 'buildConfigDigest')}
            with self.subTest(nested_only=nested_only), self.assertRaises(RequestRejected):
                self.parse(json.dumps(changed))

    def test_subject_and_unique_match_are_bound(self):
        original = json.loads(FIXTURE.read_bytes())
        values = [original + original]
        for key, value in [('name', 'other.json'), ('digest', {'sha256': '0'*64})]:
            changed = copy.deepcopy(original)
            changed[0]['verificationResult']['statement']['subject'][0][key] = value
            values.append(changed)
        for value in values:
            with self.subTest(value=value), self.assertRaises(RequestRejected):
                self.parse(json.dumps(value))
        with self.assertRaises(RequestRejected):
            self.parse(json.dumps(original), source='0'*40)

    def test_invalid_json_and_duplicate_keys_are_rejected(self):
        raw = FIXTURE.read_text(encoding='utf-8')
        duplicated = raw.replace('"sourceRepositoryDigest":', '"sourceRepositoryDigest": "'+SOURCE+'", "sourceRepositoryDigest":', 1)
        for value in ('', '[]', '{}', raw[:-20], raw+raw, duplicated):
            with self.subTest(value=value[:30]), self.assertRaises(RequestRejected):
                self.parse(value)


if __name__ == '__main__':
    unittest.main()
