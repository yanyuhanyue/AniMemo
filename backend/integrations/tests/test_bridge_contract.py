import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

from django.test import SimpleTestCase

from integrations.authentication import canonical_hmac_input, sign_hmac_request


ROOT = Path(__file__).resolve().parents[3]
BRIDGE_SIGNING_PATH = (
    ROOT / "bridges" / "astrbot_plugin_animemo_bridge" / "animemo_bridge" / "signing.py"
)
SPEC = importlib.util.spec_from_file_location("animemo_bridge_contract_signing", BRIDGE_SIGNING_PATH)
bridge_signing = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bridge_signing)


class AstrBotBridgeSigningContractTests(SimpleTestCase):
    def test_server_and_bridge_sign_the_same_canonical_requests(self):
        cases = (
            ("GET", "/api/integrations/v1/events/?after=7&limit=2&wait=0", b""),
            ("POST", "/api/integrations/v1/actions/", b'{"payload":{"title":"\xe8\x91\xac\xe9\x80\x81\xe7\x9a\x84\xe8\x8a\x99\xe8\x8e\x89\xe8\x8e\xb2"}}'),
        )
        for method, path, body in cases:
            with self.subTest(method=method, path=path):
                server_canonical = canonical_hmac_input("1700000000", "contract-nonce", method, path, body)
                bridge_canonical = bridge_signing.canonical_hmac_input(
                    "1700000000", "contract-nonce", method, path, body
                )
                self.assertEqual(server_canonical, bridge_canonical)
                self.assertEqual(
                    sign_hmac_request("contract-secret", "1700000000", "contract-nonce", method, path, body),
                    bridge_signing.sign_hmac_request(
                        "contract-secret", "1700000000", "contract-nonce", method, path, body
                    ),
                )

    def test_bridge_uses_final_encoded_query_and_exact_json_bytes(self):
        body = bridge_signing.canonical_json_bytes({"query": "\u8299\u8389\u83b2", "limit": 2})
        self.assertEqual(body, json.dumps({"query": "\u8299\u8389\u83b2", "limit": 2}, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8"))
        request = SimpleNamespace(
            url=SimpleNamespace(
                raw_path=b"/api/integrations/v1/events/?after=7&query=%E8%8A%99+%E8%8E%89%E8%8E%B2"
            )
        )
        path = bridge_signing.request_path_with_query(request)
        self.assertEqual(
            path,
            "/api/integrations/v1/events/?after=7&query=%E8%8A%99+%E8%8E%89%E8%8E%B2",
        )
        self.assertEqual(
            sign_hmac_request("contract-secret", "1700000000", "contract-nonce", "GET", path, b""),
            bridge_signing.sign_hmac_request(
                "contract-secret", "1700000000", "contract-nonce", "GET", path, b""
            ),
        )
