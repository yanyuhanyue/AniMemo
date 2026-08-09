import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from animemo_bridge.signing import canonical_hmac_input, sign_hmac_request


class SigningTests(unittest.TestCase):
    def test_golden_vectors(self):
        vectors = json.loads((ROOT / "docs" / "integration-protocol-v1-test-vectors.json").read_text(encoding="utf-8"))["vectors"]
        for vector in vectors:
            body = vector["body_utf8"].encode("utf-8")
            self.assertEqual(sign_hmac_request(vector["secret"], vector["timestamp"], vector["nonce"], vector["method"], vector["path_with_query"], body), vector["expected_signature"])
            self.assertEqual(canonical_hmac_input(vector["timestamp"], vector["nonce"], vector["method"], vector["path_with_query"], body).decode().splitlines()[-1], vector["body_sha256"])

    def test_secret_body_method_path_and_query_changes_change_signature(self):
        base = sign_hmac_request("secret", "1700000000", "nonce", "POST", "/api/test/?a=1", b"{}")
        variants = (
            sign_hmac_request("other-secret", "1700000000", "nonce", "POST", "/api/test/?a=1", b"{}"),
            sign_hmac_request("secret", "1700000000", "nonce", "POST", "/api/test/?a=1", b'{"x":1}'),
            sign_hmac_request("secret", "1700000000", "nonce", "GET", "/api/test/?a=1", b"{}"),
            sign_hmac_request("secret", "1700000000", "nonce", "POST", "/api/test/?a=2", b"{}"),
        )
        for variant in variants:
            self.assertNotEqual(base, variant)

    def test_same_inputs_are_deterministic_and_nonce_is_unique_input(self):
        first = sign_hmac_request("secret", "1700000000", "nonce", "GET", "/api/test/", b"")
        second = sign_hmac_request("secret", "1700000000", "nonce", "GET", "/api/test/", b"")
        changed_nonce = sign_hmac_request("secret", "1700000000", "other", "GET", "/api/test/", b"")
        self.assertEqual(first, second)
        self.assertNotEqual(first, changed_nonce)
