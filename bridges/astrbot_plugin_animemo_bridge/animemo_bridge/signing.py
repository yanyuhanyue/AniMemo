from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

PROTOCOL_PREFIX = "ANIMEMO-HMAC-V1"


def canonical_json_bytes(value: Any) -> bytes:
    """Encode the exact JSON bytes that will be sent over the wire."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")


def body_sha256(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def canonical_hmac_input(timestamp: str | int, nonce: str, method: str, path_with_query: str, body: bytes) -> bytes:
    return "\n".join(
        (
            PROTOCOL_PREFIX,
            str(timestamp),
            str(nonce),
            str(method).upper(),
            str(path_with_query),
            body_sha256(body),
        )
    ).encode("utf-8")


def sign_hmac_request(secret: str, timestamp: str | int, nonce: str, method: str, path_with_query: str, body: bytes = b"") -> str:
    digest = hmac.new(
        str(secret).encode("utf-8"),
        canonical_hmac_input(timestamp, nonce, method, path_with_query, body),
        hashlib.sha256,
    ).hexdigest()
    return f"v1={digest}"


def request_path_with_query(request) -> str:
    """Return httpx's final encoded path/query, not a re-encoded approximation."""
    raw_path = getattr(request.url, "raw_path", None)
    if raw_path is not None:
        if isinstance(raw_path, bytes):
            return raw_path.decode("ascii")
        return str(raw_path)
    path = str(getattr(request.url, "path", "")) or "/"
    query = getattr(request.url, "query", b"")
    if isinstance(query, bytes):
        query = query.decode("ascii")
    return f"{path}?{query}" if query else path
