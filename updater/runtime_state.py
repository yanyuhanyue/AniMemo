from __future__ import annotations

import json
from pathlib import Path

from .errors import StateError
from .state import (
    _absolute,
    _atomic_json,
    _read_private_text,
    _validate_private_directory,
)


class RuntimeState:
    """Persist contracts that describe the live host, not merely CURRENT app."""

    def __init__(self, root: Path):
        self.root = _absolute(root)
        self.path = self.root / "runtime.json"

    def initialize_from_manifest(
        self,
        manifest: dict[str, object],
        *,
        enabled_plugin_apis: set[int],
    ) -> dict[str, object]:
        if self.path.exists():
            return self.read()
        compatibility = manifest["compatibility"]
        supported_apis = set(compatibility["pluginSdk"]["supportedApis"])
        if (
            not all(isinstance(value, int) and value > 0 for value in enabled_plugin_apis)
            or not enabled_plugin_apis.issubset(supported_apis)
        ):
            raise StateError("Enabled Plugin SDK APIs are invalid for CURRENT")
        payload = {
            "databaseContract": compatibility["database"]["contract"],
            "configurationContract": compatibility["configuration"]["contract"],
            "enabledPluginApis": sorted(enabled_plugin_apis),
        }
        self.write(payload)
        return payload

    def read(self) -> dict[str, object]:
        _validate_private_directory(self.root, self.root)
        try:
            payload = json.loads(_read_private_text(self.root, self.path))
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
        _atomic_json(self.path, payload, root=self.root)

    def update(self, **changes) -> dict[str, object]:
        payload = self.read()
        payload.update(changes)
        self.write(payload)
        return self.read()
