from __future__ import annotations

import asyncio
import os
import random
import time
from dataclasses import dataclass
from uuid import uuid4

import httpx

from .errors import (
    BridgeActionError,
    BridgeAuthError,
    BridgeConnectionError,
    BridgeEventError,
    BridgePairingError,
    BridgeProtocolError,
    BridgeRateLimitError,
    PairingResultUnknown,
)
from .signing import canonical_json_bytes, request_path_with_query, sign_hmac_request

PAIRING_REQUEST_MAX_BYTES = 8 * 1024
ACTION_REQUEST_MAX_BYTES = 256 * 1024
ACK_REQUEST_MAX_BYTES = 16 * 1024


@dataclass(frozen=True)
class BridgeConfig:
    base_url: str
    key_id: str
    secret: str
    timeout_seconds: float = 35.0
    verify_tls: bool = True

    @classmethod
    def from_values(cls, base_url: str | None, key_id: str | None, secret: str | None, **kwargs):
        base_url = os.getenv("ANIMEMO_BASE_URL", base_url or "").strip().rstrip("/")
        key_id = os.getenv("ANIMEMO_INTEGRATION_KEY_ID", key_id or "").strip()
        secret = os.getenv("ANIMEMO_INTEGRATION_SECRET", secret or "").strip()
        if not base_url or not key_id or not secret:
            raise ValueError("AniMemo base URL、key id 和 secret 均为必填。")
        return cls(base_url, key_id, secret, **kwargs)


class AsyncAniMemoClient:
    """Small provider-neutral async client; all requests are signed from final bytes/URL."""

    def __init__(self, config: BridgeConfig | None = None, *, transport=None, client=None, sleep=asyncio.sleep, random_fn=None):
        self.config = config
        self._sleep = sleep
        self._random = random_fn or random.random
        self._client = client or (
            httpx.AsyncClient(
                base_url=config.base_url,
                timeout=httpx.Timeout(config.timeout_seconds),
                verify=config.verify_tls,
                headers={"Accept": "application/json"},
                transport=transport,
            )
            if config
            else None
        )
        self._owns_client = client is None

    @property
    def configured(self):
        return self.config is not None and bool(self.config.base_url and self.config.key_id and self.config.secret)

    async def aclose(self):
        if self._client is not None and self._owns_client:
            await self._client.aclose()

    def _headers(self, request):
        timestamp = str(int(time.time()))
        nonce = uuid4().hex
        body = request.content or b""
        request.headers["Content-Type"] = "application/json"
        request.headers["X-AniMemo-Key-Id"] = self.config.key_id
        request.headers["X-AniMemo-Timestamp"] = timestamp
        request.headers["X-AniMemo-Nonce"] = nonce
        request.headers["X-AniMemo-Signature"] = sign_hmac_request(
            self.config.secret, timestamp, nonce, request.method, request_path_with_query(request), body
        )

    async def _request_once(self, method, path, *, params=None, json_body=None, max_body_bytes=None):
        if not self.configured:
            raise BridgeAuthError("AniMemo 凭证尚未配置。")
        body = canonical_json_bytes(json_body) if json_body is not None else b""
        if max_body_bytes is not None and len(body) > max_body_bytes:
            raise BridgeProtocolError("AniMemo 请求体超过协议允许的大小。")
        request = self._client.build_request(method, path, params=params, content=body)
        self._headers(request)
        try:
            response = await self._client.send(request)
        except httpx.TransportError as error:
            raise BridgeConnectionError("AniMemo 连接失败。") from error
        if response.status_code == 401:
            raise BridgeAuthError("AniMemo HMAC 认证失败。")
        if response.status_code == 429:
            retry_after = response.headers.get("Retry-After")
            raise BridgeRateLimitError(f"AniMemo 请求受限，请稍后重试。retry_after={retry_after or 'unknown'}")
        if 500 <= response.status_code < 600:
            raise BridgeConnectionError(f"AniMemo 服务暂时不可用（HTTP {response.status_code}）。")
        try:
            payload = response.json() if response.content else {}
        except ValueError as error:
            raise BridgeProtocolError("AniMemo 返回了无效 JSON。") from error
        if not isinstance(payload, dict):
            raise BridgeProtocolError("AniMemo 返回结构不是 JSON 对象。")
        if response.status_code >= 400:
            detail = str(payload.get("detail") or payload.get("code") or "AniMemo 请求失败")
            error_type = BridgeActionError if path.rstrip("/").endswith("/actions") else BridgePairingError if "pair/consume" in path else BridgeEventError
            raise error_type(detail)
        return payload

    async def request(self, method, path, *, params=None, json_body=None, retries=2, pairing=False, max_body_bytes=None):
        delay = 1.0
        for attempt in range(retries + 1):
            try:
                return await self._request_once(
                    method,
                    path,
                    params=params,
                    json_body=json_body,
                    max_body_bytes=max_body_bytes,
                )
            except PairingResultUnknown:
                raise
            except BridgeAuthError:
                raise
            except BridgePairingError:
                raise
            except (BridgeConnectionError, BridgeRateLimitError) as error:
                if pairing:
                    raise PairingResultUnknown("配对请求结果未知，请在 AniMemo 绑定页面确认；如未成功请生成新的配对码。") from error
                if attempt >= retries:
                    raise
                await self._sleep(delay + self._random() * 0.25)
                delay = min(delay * 2, 30.0)

    async def pair(self, code, platform, external_user_id, display_name=""):
        return await self.request(
            "POST", "/api/integrations/v1/pair/consume/",
            json_body={"code": code, "platform": platform, "external_user_id": external_user_id, "display_name": display_name},
            retries=0, pairing=True,
            max_body_bytes=PAIRING_REQUEST_MAX_BYTES,
        )

    async def action(self, request_id, platform, external_user_id, action, payload=None):
        return await self.request(
            "POST", "/api/integrations/v1/actions/",
            json_body={"request_id": request_id, "platform": platform, "external_user_id": external_user_id, "action": action, "payload": payload or {}},
            retries=2,
            max_body_bytes=ACTION_REQUEST_MAX_BYTES,
        )

    async def events(self, *, after=0, limit=50, wait=20):
        return await self.request(
            "GET", "/api/integrations/v1/events/", params={"after": after, "limit": limit, "wait": min(int(wait), 25)}, retries=3
        )

    async def ack(self, event_ids):
        return await self.request(
            "POST",
            "/api/integrations/v1/events/ack/",
            json_body={"event_ids": list(event_ids)},
            retries=3,
            max_body_bytes=ACK_REQUEST_MAX_BYTES,
        )

    async def ping(self):
        return await self.events(after=0, limit=1, wait=0)
