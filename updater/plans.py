from __future__ import annotations

import hashlib
import json
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path

from release.contract import validate_manifest

from .errors import RequestRejected, StateError
from .state import (
    _absolute,
    _atomic_json,
    _read_private_text,
    _validate_private_directory,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _manifest_hash(manifest: dict[str, object]) -> str:
    encoded = json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class PlanStore:
    def __init__(self, root: Path, *, ttl_seconds: int = 900):
        self.state_root = _absolute(root)
        self.root = self.state_root / "plans"
        self.ttl_seconds = ttl_seconds

    def _path(self, plan_id: str) -> Path:
        if len(plan_id) != 32 or any(character not in "0123456789abcdef" for character in plan_id):
            raise RequestRejected("Invalid update plan id")
        return self.root / f"{plan_id}.json"

    def create(self, manifest: dict[str, object], plan: dict[str, object]) -> dict[str, object]:
        validate_manifest(manifest)
        created = _now()
        payload = {
            "id": secrets.token_hex(16),
            "createdAt": created.isoformat().replace("+00:00", "Z"),
            "expiresAt": (created + timedelta(seconds=self.ttl_seconds)).isoformat().replace("+00:00", "Z"),
            "consumedAt": None,
            "manifestHash": _manifest_hash(manifest),
            "manifest": manifest,
            "plan": plan,
        }
        _atomic_json(self._path(payload["id"]), payload, root=self.state_root)
        return payload
    def get(self, plan_id: str) -> dict[str, object]:
        _validate_private_directory(self.state_root, self.root)
        try:
            payload = json.loads(_read_private_text(self.state_root, self._path(plan_id)))
            validate_manifest(payload["manifest"])
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise RequestRejected("Update plan is unavailable") from error
        if payload.get("id") != plan_id or payload.get("manifestHash") != _manifest_hash(payload["manifest"]):
            raise StateError("Update plan manifest binding is invalid")
        expires = datetime.fromisoformat(str(payload["expiresAt"]).replace("Z", "+00:00"))
        if expires <= _now():
            raise RequestRejected("Update plan has expired")
        return payload

    def consume(self, plan_id: str) -> dict[str, object]:
        payload = self.get(plan_id)
        if payload.get("consumedAt"):
            raise RequestRejected("Update plan has already been consumed")
        payload["consumedAt"] = _now().isoformat().replace("+00:00", "Z")
        _atomic_json(self._path(plan_id), payload, root=self.state_root)
        return payload
