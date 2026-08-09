import json
from pathlib import Path

from django.test import SimpleTestCase

from integrations.authentication import canonical_hmac_input, sign_hmac_request


class IntegrationHMACVectorTests(SimpleTestCase):
    def test_shared_bridge_vectors(self):
        root = Path(__file__).resolve().parents[3]
        vectors = json.loads((root / "docs" / "integration-protocol-v1-test-vectors.json").read_text(encoding="utf-8"))["vectors"]
        for vector in vectors:
            body = vector["body_utf8"].encode("utf-8")
            self.assertEqual(
                sign_hmac_request(vector["secret"], vector["timestamp"], vector["nonce"], vector["method"], vector["path_with_query"], body),
                vector["expected_signature"],
            )
            self.assertEqual(canonical_hmac_input(vector["timestamp"], vector["nonce"], vector["method"], vector["path_with_query"], body).decode().splitlines()[-1], vector["body_sha256"])
