"""Routing chooses a domain; only the real verifier can accept its bytes."""
import base64
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from updater.errors import RequestRejected
from updater.source import GitHubReleaseSource


def bundle(predicate):
    return {'dsseEnvelope': {'payload': base64.b64encode(json.dumps(
        {'predicateType': predicate}).encode()).decode(), 'signatures': []}}


class AnonymousProofRoutingTests(unittest.TestCase):
    def route(self, bundles):
        value = object.__new__(GitHubReleaseSource)
        value.anonymous_installation = True
        value.rest = SimpleNamespace(get_json=lambda *a, **k:
            {'attestations': [{'bundle': b} for b in bundles]})
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'proof.jsonl'
            value._write_attestation_bundle('sha256:'+'a'*64, path)
            return json.loads(path.read_bytes())

    def test_platform_and_actions_in_either_order_select_only_actions(self):
        actions = bundle('https://slsa.dev/provenance/v1')
        platform = bundle('https://in-toto.io/attestation/release/v0.2')
        for values in ([actions, platform], [platform, actions]):
            self.assertEqual(self.route(values), actions)

    def test_multiple_actions_are_rejected_even_when_identical(self):
        actions = bundle('https://slsa.dev/provenance/v1')
        with self.assertRaises(RequestRejected):
            self.route([actions, actions])

    def test_platform_only_cannot_replace_actions(self):
        with self.assertRaises(RequestRejected):
            self.route([bundle('https://in-toto.io/attestation/release/v0.2')])

    def test_unknown_domain_and_malformed_payload_fail_closed(self):
        actions = bundle('https://slsa.dev/provenance/v1')
        bad = [bundle('https://example.invalid/proof'), {},
            {'dsseEnvelope': {'payload': '%%%'}},
            {'dsseEnvelope': {'payload': base64.b64encode(b'{"predicateType":"a","predicateType":"b"}').decode()}}]
        for item in bad:
            with self.subTest(item=item), self.assertRaises(RequestRejected):
                self.route([actions, item])

    def test_untrusted_hint_does_not_create_signature_or_claim(self):
        actions = bundle('https://slsa.dev/provenance/v1')
        selected = self.route([actions])
        self.assertEqual(selected, actions)
        self.assertEqual(selected['dsseEnvelope']['signatures'], [])


if __name__ == '__main__':
    unittest.main()
