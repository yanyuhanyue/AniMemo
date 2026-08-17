import asyncio
import json
import sys
import unittest
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from animemo_bridge.client import AsyncAniMemoClient, BridgeConfig
from animemo_bridge.errors import (
    BridgeActionError,
    BridgeAuthError,
    BridgeProtocolError,
    PairingResultUnknown,
)
from animemo_bridge.signing import sign_hmac_request


class ClientTests(unittest.IsolatedAsyncioTestCase):
    def test_bridge_config_rejects_non_https_service_urls(self):
        for base_url in ("http://animemo.example", "http://127.0.0.1:8000"):
            with self.subTest(base_url=base_url), self.assertRaisesRegex(ValueError, "HTTPS"):
                BridgeConfig.from_values(base_url, "key", "secret")

    def test_bridge_config_rejects_credentials_embedded_in_service_url(self):
        with self.assertRaisesRegex(ValueError, "凭证"):
            BridgeConfig.from_values("https://operator:password@animemo.example", "key", "secret")

    def test_bridge_config_requires_a_canonical_service_origin(self):
        for base_url in (
            "https://animemo.example/api",
            "https://animemo.example/?tenant=one",
            "https://animemo.example/#status",
            "https://animemo.example:invalid",
        ):
            with self.subTest(base_url=base_url), self.assertRaises(ValueError):
                BridgeConfig.from_values(base_url, "key", "secret")

        config = BridgeConfig.from_values("https://animemo.example/", "key", "secret")
        self.assertEqual(config.base_url, "https://animemo.example")

    async def test_query_and_body_are_signed_as_sent(self):
        seen = []

        async def handler(request):
            body = await request.aread()
            seen.append((request, body))
            timestamp = request.headers["X-AniMemo-Timestamp"]
            expected = sign_hmac_request("secret", timestamp, request.headers["X-AniMemo-Nonce"], request.method, request.url.raw_path.decode("ascii"), body)
            self.assertEqual(request.headers["X-AniMemo-Signature"], expected)
            return httpx.Response(200, json={"events": [], "next_cursor": 7})

        transport = httpx.MockTransport(handler)
        client = AsyncAniMemoClient(BridgeConfig("https://example.test", "key", "secret"), transport=transport)
        result = await client.events(after=7, limit=2, wait=0)
        self.assertEqual(result["next_cursor"], 7)
        self.assertEqual(seen[0][0].url.raw_path.decode("ascii"), "/api/integrations/v1/events/?after=7&limit=2&wait=0")
        await client.aclose()

    async def test_action_retry_reuses_request_id_and_exact_body(self):
        calls = []

        async def handler(request):
            body = await request.aread()
            calls.append(json.loads(body))
            if len(calls) == 1:
                raise httpx.ReadTimeout("temporary")
            return httpx.Response(200, json={"ok": True})

        transport = httpx.MockTransport(handler)
        client = AsyncAniMemoClient(BridgeConfig("https://example.test", "key", "secret"), transport=transport, sleep=lambda _x: asyncio.sleep(0))
        result = await client.action("req-1", "qq", "42", "watch-history-importer.history-get", {"entry_id": 1})
        self.assertEqual(result, {"ok": True})
        self.assertEqual(calls, [calls[0], calls[1]])
        self.assertEqual(calls[0]["request_id"], calls[1]["request_id"])
        await client.aclose()

    async def test_auth_failure_is_not_retried(self):
        attempts = 0

        async def handler(_request):
            nonlocal attempts
            attempts += 1
            return httpx.Response(401, json={"code": "auth_failed"})

        client = AsyncAniMemoClient(BridgeConfig("https://example.test", "key", "secret"), transport=httpx.MockTransport(handler))
        with self.assertRaises(BridgeAuthError):
            await client.ping()
        self.assertEqual(attempts, 1)
        await client.aclose()

    async def test_server_502_is_retried(self):
        attempts = 0

        async def handler(_request):
            nonlocal attempts
            attempts += 1
            return httpx.Response(502 if attempts == 1 else 200, json={"ok": True})

        client = AsyncAniMemoClient(
            BridgeConfig("https://example.test", "key", "secret"),
            transport=httpx.MockTransport(handler),
            sleep=lambda _delay: asyncio.sleep(0),
        )
        self.assertEqual(await client.action("req-502", "qq", "42", "watch-history-importer.history-get", {}), {"ok": True})
        self.assertEqual(attempts, 2)
        await client.aclose()

    async def test_bad_request_is_not_retried(self):
        attempts = 0

        async def handler(_request):
            nonlocal attempts
            attempts += 1
            return httpx.Response(400, json={"detail": "bad request"})

        client = AsyncAniMemoClient(BridgeConfig("https://example.test", "key", "secret"), transport=httpx.MockTransport(handler))
        with self.assertRaises(BridgeActionError):
            await client.action("req-400", "qq", "42", "watch-history-importer.history-get", {})
        self.assertEqual(attempts, 1)
        await client.aclose()

    async def test_pair_timeout_is_ambiguous(self):
        async def handler(_request):
            raise httpx.ReadTimeout("temporary")

        client = AsyncAniMemoClient(BridgeConfig("https://example.test", "key", "secret"), transport=httpx.MockTransport(handler))
        with self.assertRaises(PairingResultUnknown):
            await client.pair("ABC", "qq", "42")
        await client.aclose()

    async def test_remote_protocol_error_is_retried(self):
        attempts = 0

        async def handler(_request):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise httpx.RemoteProtocolError("temporary")
            return httpx.Response(200, json={"ok": True})

        client = AsyncAniMemoClient(
            BridgeConfig("https://example.test", "key", "secret"),
            transport=httpx.MockTransport(handler),
            sleep=lambda _delay: asyncio.sleep(0),
        )
        self.assertEqual(
            await client.action("req-protocol", "qq", "42", "watch-history-importer.history-get", {}),
            {"ok": True},
        )
        self.assertEqual(attempts, 2)
        await client.aclose()

    async def test_invalid_json_is_protocol_error(self):
        async def handler(_request):
            return httpx.Response(200, content=b"not-json")

        client = AsyncAniMemoClient(BridgeConfig("https://example.test", "key", "secret"), transport=httpx.MockTransport(handler))
        with self.assertRaises(BridgeProtocolError):
            await client.ping()
        await client.aclose()

    async def test_oversized_action_is_rejected_before_network(self):
        attempts = 0

        async def handler(_request):
            nonlocal attempts
            attempts += 1
            return httpx.Response(200, json={"ok": True})

        client = AsyncAniMemoClient(BridgeConfig("https://example.test", "key", "secret"), transport=httpx.MockTransport(handler))
        with self.assertRaises(BridgeProtocolError):
            await client.action("req-large", "qq", "42", "watch-history-importer.history-add", {"text": "x" * (256 * 1024)})
        self.assertEqual(attempts, 0)
        await client.aclose()

    async def test_oversized_pairing_and_ack_are_rejected_before_network(self):
        attempts = 0

        async def handler(_request):
            nonlocal attempts
            attempts += 1
            return httpx.Response(200, json={"ok": True})

        client = AsyncAniMemoClient(BridgeConfig("https://example.test", "key", "secret"), transport=httpx.MockTransport(handler))
        with self.assertRaises(BridgeProtocolError):
            await client.pair("ABC", "qq", "42", "x" * (8 * 1024))
        with self.assertRaises(BridgeProtocolError):
            await client.ack(range(5000))
        self.assertEqual(attempts, 0)
        await client.aclose()
