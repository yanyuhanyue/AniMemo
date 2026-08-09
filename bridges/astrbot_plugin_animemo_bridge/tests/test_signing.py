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
