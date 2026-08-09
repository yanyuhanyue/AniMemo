import asyncio
import json
import sys
import unittest
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from animemo_bridge.client import AsyncAniMemoClient, BridgeConfig
from animemo_bridge.errors import BridgeAuthError
from animemo_bridge.signing import sign_hmac_request


class ClientTests(unittest.IsolatedAsyncioTestCase):
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
