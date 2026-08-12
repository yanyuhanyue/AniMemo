from __future__ import annotations

import copy
import json
from pathlib import Path

from release.contract import validate_manifest

from .errors import StateError
from .state import (
    _absolute,
    _atomic_json,
    _ensure_private_directory,
    _read_private_text,
    _validate_private_directory,
)


class ReleaseSlots:
    """Own the durable CURRENT, PREVIOUS, and immutable history manifests."""

    def __init__(self, root: Path):
        self.root = _absolute(root)
        self.current_path = self.root / "CURRENT.json"
        self.previous_path = self.root / "PREVIOUS.json"
        self.history_root = self.root / "history"

    def _validate_storage(self) -> None:
        _ensure_private_directory(self.root, self.history_root)

    def _load(self, path: Path) -> dict[str, object] | None:
        try:
            raw = _read_private_text(self.root, path)
        except FileNotFoundError:
            return None
        try:
            payload = json.loads(raw)
            validate_manifest(payload)
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as error:
            raise StateError(f"Invalid release slot: {path.name}") from error
        return payload

    def _history_path(self, manifest: dict[str, object]) -> Path:
        version = str(manifest["release"]["version"])
        return self.history_root / f"{version}.json"

    def _record_history(self, manifest: dict[str, object], operation_id: str | None) -> None:
        path = self._history_path(manifest)
        if path.exists():
            try:
                existing = json.loads(_read_private_text(self.root, path))
                existing_manifest = existing["manifest"]
                validate_manifest(existing_manifest)
            except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
                raise StateError(f"Invalid immutable release history: {path.name}") from error
            if existing_manifest != manifest:
                raise StateError(f"Immutable release history conflicts for {manifest['release']['version']}")
            return
        _atomic_json(
            path,
            {"manifest": copy.deepcopy(manifest), "deployment": {"operationId": operation_id}},
            root=self.root,
        )

    @staticmethod
    def _strip_metadata(manifest: dict[str, object] | None) -> dict[str, object] | None:
        if manifest is None:
            return None
        result = copy.deepcopy(manifest)
        result.pop("_deployment", None)
        return result

    def import_current(self, manifest: dict[str, object]) -> None:
        validate_manifest(manifest)
        self._validate_storage()
        current = self._load(self.current_path)
        if current is not None:
            raise StateError("CURRENT is already initialized; bootstrap import is one-time")
        _atomic_json(self.current_path, manifest, root=self.root)
        self._record_history(manifest, None)

    def promote(self, manifest: dict[str, object], *, operation_id: str) -> None:
        validate_manifest(manifest)
        self._validate_storage()
        current = self._load(self.current_path)
        if current is not None:
            _atomic_json(self.previous_path, current, root=self.root)
        _atomic_json(self.current_path, manifest, root=self.root)
        self._record_history(manifest, operation_id)

    def restore_previous(self, *, operation_id: str) -> dict[str, object]:
        self._validate_storage()
        current = self._load(self.current_path)
        previous = self._load(self.previous_path)
        if previous is None:
            raise StateError("PREVIOUS is not available")
        if current is not None:
            _atomic_json(self.previous_path, current, root=self.root)
        _atomic_json(self.current_path, previous, root=self.root)
        self._record_history(previous, operation_id)
        return previous

    def read(self) -> dict[str, object]:
        _validate_private_directory(self.root, self.root)
        _validate_private_directory(self.root, self.history_root)
        history = []
        if self.history_root.exists():
            for path in sorted(self.history_root.glob("*.json")):
                payload = json.loads(_read_private_text(self.root, path))
                manifest = payload["manifest"]
                validate_manifest(manifest)
                history.append({"manifest": manifest, "deployment": payload.get("deployment", {})})
        history.sort(key=lambda item: item["manifest"]["release"]["createdAt"])
        return {
            "current": self._strip_metadata(self._load(self.current_path)),
            "previous": self._strip_metadata(self._load(self.previous_path)),
            "history": history,
        }
