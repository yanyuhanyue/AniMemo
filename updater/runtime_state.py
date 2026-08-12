from __future__ import annotations

import json
from pathlib import Path

from .errors import StateError
from .state import _atomic_json


class RuntimeState:
    """Persist contracts that describe the live host, not merely CURRENT app."""

    def __init__(self, root: Path):
        self.path = root.resolve() / "runtime.json"

    def initialize_from_manifest(self, manifest: dict[str, object]) -> dict[str, object]:
        if self.path.exists():
            return self.read()
        compatibility = manifest["compatibility"]
        payload = {
            "databaseContract": compatibility["database"]["contract"],
            "configurationContract": compatibility["configuration"]["contract"],
            "enabledPluginApis": sorted(compatibility["pluginSdk"]["supportedApis"]),
        }
        self.write(payload)
        return payload

    def read(self) -> dict[str, object]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise StateError("Runtime compatibility state is unavailable") from error
        if (
            not isinstance(payload, dict)
            or set(payload) != {"databaseContract", "configurationContract", "enabledPluginApis"}
            or not isinstance(payload["databaseContract"], str)
            or not isinstance(payload["configurationContract"], str)
            or not isinstance(payload["enabledPluginApis"], list)
            or not all(isinstance(value, int) and value > 0 for value in payload["enabledPluginApis"])
        ):
            raise StateError("Runtime compatibility state is invalid")
        return payload

    def write(self, payload: dict[str, object]) -> None:
        _atomic_json(self.path, payload)

    def update(self, **changes) -> dict[str, object]:
        payload = self.read()
        payload.update(changes)
        self.write(payload)
        return self.read()
