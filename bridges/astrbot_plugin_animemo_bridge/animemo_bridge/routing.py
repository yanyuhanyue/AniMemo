from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .identity import MessageIdentity, route_key


class RouteStore:
    def __init__(self, path: str | os.PathLike, *, backup_corrupt=True):
        self.path = Path(path)
        self.backup_corrupt = backup_corrupt
        self.state = {"version": 1, "routes": {}}
        self.load()

    def load(self):
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict) or payload.get("version") != 1 or not isinstance(payload.get("routes"), dict):
                raise ValueError("invalid route state")
            self.state = payload
        except FileNotFoundError:
            self.state = {"version": 1, "routes": {}}
        except (OSError, ValueError, json.JSONDecodeError):
            if self.backup_corrupt and self.path.exists():
                backup = self.path.with_name(f"{self.path.name}.corrupt-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}")
                try:
                    self.path.replace(backup)
                except OSError:
                    pass
            self.state = {"version": 1, "routes": {}}
        return self.state

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=f".{self.path.name}.", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(self.state, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, self.path)
        finally:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)

    def save_private(self, identity: MessageIdentity):
        if not identity.is_private or not identity.umo or not identity.external_user_id:
            return False
        self.state["routes"][route_key(identity.platform, identity.external_user_id)] = {
            "umo": identity.umo,
            "display_name": identity.display_name,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        self.save()
        return True

    def get(self, platform, external_user_id):
        return self.state["routes"].get(route_key(platform, external_user_id))

    def count(self):
        return len(self.state["routes"])

    def masked_routes(self):
        result = []
        for key, value in sorted(self.state["routes"].items()):
            platform, _, external = key.partition(":")
            digest = hashlib.sha256(external.encode("utf-8")).hexdigest()[:10]
            result.append({"platform": platform, "external_user_id": f"…{digest}", "updated_at": value.get("updated_at", "")})
        return result
