"""Public DEV selection and capability rejection; no crypto claim is mocked."""
from __future__ import annotations

import copy
import hashlib
import json
import unittest
from datetime import datetime, timezone

from installer.development_trust import (
    BUILD_FILES,
    EVIDENCE_FILES,
    PURPOSE,
    SCHEMA,
    TRUST_FILES,
    DevelopmentBootstrapPrivilegeGate,
    DevelopmentLocalBundleAuthority,
    DevelopmentTrustError,
    RuntimeTrustInputs,
    consume_runtime_local_bundle,
    validate_runtime_trust_selection,
)
from installer.runtime import (
    InstallTransportSource,
    ReleaseEvidence,
    transport_policy_identity,
)


def selection_fixture():
    binding = {'execution_source_sha': '1' * 40, 'execution_source_tree': '2' * 40,
        'execution_inventory_digest': 'sha256:' + '3' * 64,
        'verified_candidate_digest': 'sha256:' + '4' * 64}
    file = {'sha256': 'sha256:' + '5' * 64, 'size': 12}
    release = ReleaseEvidence(version='v2.0.0-rc.3', channel='rc', commit='6' * 40,
        manifest_digest='sha256:' + '7' * 64, material_identity_digest='sha256:' + '8' * 64,
        deployment_identity_digest='sha256:' + '9' * 64,
        deployment_profile='v1.1-instance-scoped', platform_profile='v1.1-standard-linux-amd64',
        transport_source=InstallTransportSource.LOCAL_BUNDLE,
        transport_policy_identity=transport_policy_identity(InstallTransportSource.LOCAL_BUNDLE))
    value = {'schema': SCHEMA, 'purpose': PURPOSE, 'platform': 'linux/amd64',
        'source': binding, 'profile_identity': 'sha256:' + 'a' * 64,
        'expires_at': '2099-01-01T00:00:00Z',
        'files': {prefix + name: dict(file) for prefix, names in (
            ('trust/', TRUST_FILES), ('evidence/', EVIDENCE_FILES), ('build/', BUILD_FILES)) for name in names},
        'product': {'payload': dict(file), 'release_attestation': dict(file),
            'release': release.as_dict(), 'images': {role: 'sha256:' + 'b' * 64
                for role in ('api', 'web', 'postgres', 'redis')}}}
    return binding, value


def digest(value):
    return 'sha256:' + hashlib.sha256(json.dumps(value, ensure_ascii=False, allow_nan=False,
        sort_keys=True, separators=(',', ':')).encode()).hexdigest()


class RuntimeTrustSelectionTests(unittest.TestCase):
    """These synthetic public descriptors never stand in for verified materials."""

    def setUp(self):
        self.binding, self.value = selection_fixture()

    def validate(self, value=None, **kwargs):
        value = self.value if value is None else value
        return validate_runtime_trust_selection(value, expected_digest=digest(value),
            binding=self.binding, **kwargs)

    def test_public_descriptor_is_data_only(self):
        self.assertIs(self.validate(), self.value)
        with self.assertRaises(DevelopmentTrustError):
            DevelopmentBootstrapPrivilegeGate(self.value)
        with self.assertRaises(DevelopmentTrustError):
            consume_runtime_local_bundle(self.value, service_source=self.value,
                preparation_gate=self.value)

    def test_forged_constructor_tokens_rejected(self):
        for factory in (RuntimeTrustInputs, DevelopmentLocalBundleAuthority):
            with self.subTest(factory=factory.__name__), self.assertRaises(DevelopmentTrustError):
                factory(object())

    def test_windows_platform_cannot_be_claimed_as_linux(self):
        self.value['platform'] = 'windows/amd64'
        with self.assertRaisesRegex(DevelopmentTrustError, 'PLATFORM_INVALID'):
            self.validate()

    def test_test_only_claim_not_a_selection(self):
        for key, value in (('purpose', 'TEST_ONLY'), ('verified', True), ('authorityRole', 'TEST_ONLY')):
            bad = copy.deepcopy(self.value)
            bad[key] = value
            with self.subTest(key=key), self.assertRaises(DevelopmentTrustError):
                self.validate(bad)

    def test_source_changes_do_not_inherit_selection(self):
        self.value['source'] = dict(self.binding, execution_source_sha='c' * 40)
        with self.assertRaisesRegex(DevelopmentTrustError, 'SOURCE_MISMATCH'):
            self.validate()

    def test_package_changes_cannot_keep_approved_digest(self):
        approved = digest(self.value)
        self.value['files']['trust/offline-release-verifier']['sha256'] = 'sha256:' + 'd' * 64
        with self.assertRaisesRegex(DevelopmentTrustError, 'SELECTION_CHANGED'):
            validate_runtime_trust_selection(self.value, expected_digest=approved, binding=self.binding)

    def test_missing_trust_input_rejected(self):
        del self.value['files']['trust/github-tuf-root.json']
        with self.assertRaises(DevelopmentTrustError):
            self.validate()

    def test_unknown_target_or_input_name_rejected(self):
        self.value['files']['/var/lib/animemo/offline-trust/v2/active.json'] = {'sha256': 'sha256:' + 'e' * 64, 'size': 10}
        with self.assertRaises(DevelopmentTrustError):
            self.validate()

    def test_expired_trust_rejected_at_boundary(self):
        self.value['expires_at'] = '2026-09-29T00:00:00Z'
        with self.assertRaisesRegex(DevelopmentTrustError, 'EXPIRED'):
            self.validate(now=datetime(2026, 9, 29, tzinfo=timezone.utc))

    def test_original_candidate_transport_not_relabeled_local_bundle(self):
        self.value['product']['release']['transportSource'] = 'prepublication-candidate'
        with self.assertRaises(DevelopmentTrustError):
            self.validate()


if __name__ == '__main__':
    unittest.main()
