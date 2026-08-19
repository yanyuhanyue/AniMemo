"""Official Mirror exact-byte replication; GitHub remains release authority."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from collections.abc import Callable, Mapping
from typing import Any

from .notes import CANONICAL_RELEASE_ASSETS


SCHEMA = "animemo.release-mirror-plan/v1"
_SHA256 = re.compile(r"sha256:[0-9a-f]{64}")
_COMMIT = re.compile(r"[0-9a-f]{40}")


class MirrorError(ValueError):
    """Mirror replication would alter or compete with release authority."""


def _canonical_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True)
        + "\n"
    ).encode("utf-8")


def _identity(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _digest(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise MirrorError(f"{field} must be a SHA-256 identity")
    return value


def _assets(value: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(value, Mapping) or set(value) != set(CANONICAL_RELEASE_ASSETS):
        raise MirrorError("mirror asset inventory differs from authority inventory")
    result = {}
    for name in CANONICAL_RELEASE_ASSETS:
        item = value[name]
        if not isinstance(item, Mapping) or set(item) != {"sha256", "size"}:
            raise MirrorError("mirror asset identity is not closed")
        size = item["size"]
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise MirrorError("mirror asset size is invalid")
        result[name] = {"sha256": _digest(item["sha256"], name), "size": size}
    return result


def build_mirror_plan(
    *,
    authority: str,
    repository: str,
    tag: str,
    commit: str,
    release_identity: str,
    assets: Mapping[str, Mapping[str, Any]],
    api_digest: str,
    web_digest: str,
) -> dict[str, Any]:
    if authority != "GITHUB_RELEASE" or repository != "yanyuhanyue/AniMemo":
        raise MirrorError("Official Mirror cannot select or replace release authority")
    if not isinstance(tag, str) or not tag.startswith("v"):
        raise MirrorError("mirror tag is invalid")
    if not isinstance(commit, str) or not _COMMIT.fullmatch(commit):
        raise MirrorError("mirror commit is invalid")
    unsigned: dict[str, Any] = {
        "schema": SCHEMA,
        "authority": authority,
        "role": "TRANSPORT_ONLY",
        "repository": repository,
        "tag": tag,
        "commit": commit,
        "release_identity": _digest(release_identity, "release_identity"),
        "assets": _assets(assets),
        "api_digest": _digest(api_digest, "api_digest"),
        "web_digest": _digest(web_digest, "web_digest"),
        "version_selection": "FORBIDDEN",
        "transformation": "FORBIDDEN",
        "fallback_policy": "FORBIDDEN",
    }
    return {**unsigned, "identity": _identity(unsigned)}


def validate_mirror_plan(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise MirrorError("mirror plan is missing")
    required = {
        "schema",
        "identity",
        "authority",
        "role",
        "repository",
        "tag",
        "commit",
        "release_identity",
        "assets",
        "api_digest",
        "web_digest",
        "version_selection",
        "transformation",
        "fallback_policy",
    }
    if set(value) != required:
        raise MirrorError("mirror plan has unknown or missing fields")
    rebuilt = build_mirror_plan(
        authority=value["authority"],
        repository=value["repository"],
        tag=value["tag"],
        commit=value["commit"],
        release_identity=value["release_identity"],
        assets=value["assets"],
        api_digest=value["api_digest"],
        web_digest=value["web_digest"],
    )
    if dict(value) != rebuilt:
        raise MirrorError("mirror plan identity mismatch")
    return copy.deepcopy(rebuilt)


def replicate_exact_bytes(
    plan: Mapping[str, Any],
    *,
    fetched: Mapping[str, bytes],
    write: Callable[[str, bytes], None],
    readback: Callable[[str], bytes],
) -> dict[str, Any]:
    """Verify authority bytes before and after a transport-only write."""

    validated = validate_mirror_plan(plan)
    if not isinstance(fetched, Mapping) or set(fetched) != set(CANONICAL_RELEASE_ASSETS):
        raise MirrorError("fetched authority asset inventory is incomplete or has extras")
    for name in CANONICAL_RELEASE_ASSETS:
        content = fetched[name]
        if not isinstance(content, bytes):
            raise MirrorError(f"authority asset is not bytes: {name}")
        actual = {
            "sha256": "sha256:" + hashlib.sha256(content).hexdigest(),
            "size": len(content),
        }
        if actual != validated["assets"][name]:
            raise MirrorError(f"fetched authority asset identity mismatch: {name}")
        write(name, content)
        mirrored = readback(name)
        if not isinstance(mirrored, bytes) or mirrored != content:
            raise MirrorError(f"mirror transformed or failed to preserve asset bytes: {name}")
    unsigned = {
        "schema": "animemo.release-mirror-receipt/v1",
        "plan_identity": validated["identity"],
        "authority": "GITHUB_RELEASE",
        "role": "TRANSPORT_ONLY",
        "tag": validated["tag"],
        "commit": validated["commit"],
        "api_digest": validated["api_digest"],
        "web_digest": validated["web_digest"],
        "asset_count": len(CANONICAL_RELEASE_ASSETS),
        "status": "PASS",
    }
    return {**unsigned, "identity": _identity(unsigned)}
