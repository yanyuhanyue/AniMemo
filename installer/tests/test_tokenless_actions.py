"""Real fixed Go Actions regression using committed public proof bytes.

The explicit tool fixture is comparison material, not independently provisioned
first trust. No successful verifier is mocked and no network is used.
"""
import base64
import dataclasses
import hashlib
import json
import os
import unittest
from pathlib import Path

from installer.tokenless_stage0 import TokenlessActionsVerifier, TokenlessStage0Error
from updater.offline import PretrustedTrustMaterial


@unittest.skipUnless(os.environ.get('ANIMEMO_TOKENLESS_REAL_FIXTURE'), 'Explicit real tool fixture required')
class TokenlessActionsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture = json.loads(Path(os.environ['ANIMEMO_TOKENLESS_REAL_FIXTURE']).read_bytes())
        assert fixture['authority'] == 'NON_AUTHORITATIVE_LOCAL_COMPONENT_FIXTURE'
        cls.material = PretrustedTrustMaterial.load(Path(fixture['profile_path']).parent)
        cls.scratch = Path(fixture['scratch_parent'])
        source = Path(__file__).resolve().parents[2] / 'release/release_attestation_verifier/testdata/github-actions-public'
        cls.bundle = (source / 'sha256-2588108838c23c9b7e29d70d3a897109bf93b5c52cc4bcf949d5434e51496459.jsonl').read_bytes()
        cls.request = json.loads((source / 'request.json').read_bytes())

    def verify(self, **changes):
        request = self.request
        options = dict(bundle=self.bundle, evidence_name=request['evidenceName'],
            subject_name=request['subject']['name'], subject_sha256=request['subject']['sha256'],
            workflow=request['workflow'], source_commit=request['sourceCommit'])
        options.update(changes)
        return TokenlessActionsVerifier(self.material, scratch_parent=self.scratch).verify(**options)

    def test_real_valid_actions_proof_closes_exact_subject_and_execution(self):
        result = self.verify()
        self.assertEqual(result.subject_digest, self.request['subject']['sha256'])
        self.assertEqual(result.source_commit, self.request['sourceCommit'])
        self.assertEqual(result.signer_digest, result.source_commit)

    def test_valid_signature_with_wrong_expected_workflow_or_commit_is_rejected(self):
        workflow = ('.github/workflows/promote-release.yml' if self.request['workflow'] == '.github/workflows/release.yml'
                    else '.github/workflows/release.yml')
        for change in ({'workflow': workflow}, {'source_commit': 'f'*40}):
            with self.subTest(change=change), self.assertRaises(TokenlessStage0Error):
                self.verify(**change)

    def test_real_corrupted_signature_is_distinct_from_valid_identity_negative(self):
        bundle = json.loads(self.bundle)
        value = bytearray(base64.b64decode(bundle['dsseEnvelope']['signatures'][0]['sig']))
        value[-1] ^= 1
        bundle['dsseEnvelope']['signatures'][0]['sig'] = base64.b64encode(value).decode()
        with self.assertRaises(TokenlessStage0Error):
            self.verify(bundle=json.dumps(bundle).encode())

    def test_github_private_root_cannot_verify_actions_even_when_hash_bound(self):
        material = dataclasses.replace(self.material,
            sigstore_trusted_root_path=self.material.github_trusted_root_path,
            profile=dataclasses.replace(self.material.profile, sigstore_trusted_root_sha256=
                'sha256:' + hashlib.sha256(self.material.github_trusted_root_path.read_bytes()).hexdigest()))
        with self.assertRaises(TokenlessStage0Error):
            TokenlessActionsVerifier(material, scratch_parent=self.scratch).verify(bundle=self.bundle,
                evidence_name=self.request['evidenceName'], subject_name=self.request['subject']['name'],
                subject_sha256=self.request['subject']['sha256'], workflow=self.request['workflow'],
                source_commit=self.request['sourceCommit'])

    def test_slsa_routing_hint_does_not_authorize_tampered_payload(self):
        bundle = json.loads(self.bundle)
        statement = json.loads(base64.b64decode(bundle['dsseEnvelope']['payload']))
        self.assertEqual(statement['predicateType'], 'https://slsa.dev/provenance/v1')
        statement['subject'][0]['name'] = 'changed-without-resigning'
        bundle['dsseEnvelope']['payload'] = base64.b64encode(json.dumps(statement).encode()).decode()
        with self.assertRaises(TokenlessStage0Error):
            self.verify(bundle=json.dumps(bundle).encode())


if __name__ == '__main__':
    unittest.main()
