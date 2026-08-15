from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import TypeAlias

JsonScalar: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = JsonScalar | Mapping[str, "JsonValue"] | Sequence["JsonValue"]


def canonical_json_bytes(value: JsonValue) -> bytes:
    """Serialize one contract value into deterministic UTF-8 JSON bytes."""

    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def sha256_identity(value: bytes) -> str:
    """Return the lowercase, algorithm-qualified SHA-256 identity."""

    return f"sha256:{hashlib.sha256(value).hexdigest()}"
