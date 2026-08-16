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
)

SLOTS_SCHEMA_VERSION = 1


class ReleaseSlots:
    """Own CURRENT, PREVIOUS and immutable history as one atomic generation."""

    def __init__(self, root: Path):
        self.root = _absolute(root)
        self.envelope_path = self.root / "release-slots.json"
        # Pre-v1.1 layouts are detected only so they can fail closed.
        self.current_path = self.root / "CURRENT.json"
        self.previous_path = self.root / "PREVIOUS.json"
        self.history_root = self.root / "history"

    @staticmethod
    def _empty() -> dict[str, object]:
        return {
            "schemaVersion": SLOTS_SCHEMA_VERSION,
            "generation": 0,
            "current": None,
            "previous": None,
            "history": [],
        }

    def _validate_storage(self) -> None:
        _ensure_private_directory(self.root, self.root)

    @staticmethod
    def _version(manifest: dict[str, object]) -> str:
        return str(manifest["release"]["version"])

    def _validate_envelope(self, payload: object) -> dict[str, object]:
        if not isinstance(payload, dict) or set(payload) != {
            "schemaVersion",
            "generation",
            "current",
            "previous",
            "history",
        }:
            raise StateError("Release slots envelope has an invalid shape")
        if payload["schemaVersion"] != SLOTS_SCHEMA_VERSION:
            raise StateError("Release slots envelope has an unsupported schema")
        if (
            not isinstance(payload["generation"], int)
            or isinstance(payload["generation"], bool)
            or payload["generation"] < 0
        ):
            raise StateError("Release slots generation is invalid")

        current = payload["current"]
        previous = payload["previous"]
        for label, manifest in (("CURRENT", current), ("PREVIOUS", previous)):
            if manifest is not None:
                try:
                    validate_manifest(manifest)
                except (TypeError, ValueError) as error:
                    raise StateError(f"Invalid release slot: {label}") from error

        history = payload["history"]
        if not isinstance(history, list):
            raise StateError("Release slots history is invalid")
        by_version: dict[str, dict[str, object]] = {}
        for record in history:
            if not isinstance(record, dict) or set(record) != {
                "manifest",
                "deployment",
            }:
                raise StateError("Release slots history record is invalid")
            manifest = record["manifest"]
            deployment = record["deployment"]
            try:
                validate_manifest(manifest)
            except (TypeError, ValueError) as error:
                raise StateError("Release slots history manifest is invalid") from error
            if not isinstance(deployment, dict) or set(deployment) != {"operationId"}:
                raise StateError("Release slots history deployment is invalid")
            operation_id = deployment["operationId"]
            if operation_id is not None and not isinstance(operation_id, str):
                raise StateError("Release slots history operation id is invalid")
            version = self._version(manifest)
            if version in by_version:
                raise StateError(f"Duplicate immutable release history: {version}")
            by_version[version] = manifest

        if current is None:
            if previous is not None or history:
                raise StateError(
                    "Uninitialized release slots contain durable release state"
                )
        else:
            current_version = self._version(current)
            if by_version.get(current_version) != current:
                raise StateError("CURRENT is missing from immutable release history")
            if previous is not None:
                previous_version = self._version(previous)
                if previous == current:
                    raise StateError(
                        "CURRENT and PREVIOUS must identify different releases"
                    )
                if by_version.get(previous_version) != previous:
                    raise StateError(
                        "PREVIOUS is missing from immutable release history"
                    )
        return payload

    def _load_envelope(self) -> dict[str, object] | None:
        try:
            raw = _read_private_text(self.root, self.envelope_path)
        except FileNotFoundError:
            return None
        try:
            payload = json.loads(raw)
        except (OSError, json.JSONDecodeError) as error:
            raise StateError("Release slots envelope is invalid") from error
        return self._validate_envelope(payload)

    def _state(self) -> dict[str, object]:
        self._validate_storage()
        if any(
            path.exists() or path.is_symlink()
            for path in (self.current_path, self.previous_path, self.history_root)
        ):
            raise StateError("Legacy release slot state is unsupported")
        envelope = self._load_envelope()
        if envelope is not None:
            return envelope
        return self._empty()

    def _commit(self, payload: dict[str, object]) -> None:
        self._validate_envelope(payload)
        _atomic_json(self.envelope_path, payload, root=self.root)

    def _record_history(
        self,
        history: list[dict[str, object]],
        manifest: dict[str, object],
        operation_id: str | None,
    ) -> None:
        version = self._version(manifest)
        for record in history:
            if self._version(record["manifest"]) != version:
                continue
            if record["manifest"] != manifest:
                raise StateError(f"Immutable release history conflicts for {version}")
            return
        history.append(
            {
                "manifest": copy.deepcopy(manifest),
                "deployment": {"operationId": operation_id},
            }
        )
        history.sort(
            key=lambda item: (
                item["manifest"]["release"]["createdAt"],
                self._version(item["manifest"]),
            )
        )

    def import_current(self, manifest: dict[str, object]) -> None:
        validate_manifest(manifest)
        state = copy.deepcopy(self._state())
        if state["current"] is not None:
            raise StateError(
                "CURRENT is already initialized; bootstrap import is one-time"
            )
        state["current"] = copy.deepcopy(manifest)
        self._record_history(state["history"], manifest, None)
        state["generation"] += 1
        self._commit(state)

    def promote(self, manifest: dict[str, object], *, operation_id: str) -> None:
        validate_manifest(manifest)
        state = copy.deepcopy(self._state())
        current = state["current"]
        if current == manifest:
            raise StateError("CURRENT already identifies the target release")
        state["previous"] = current
        state["current"] = copy.deepcopy(manifest)
        self._record_history(state["history"], manifest, operation_id)
        state["generation"] += 1
        self._commit(state)

    def restore_previous(self, *, operation_id: str) -> dict[str, object]:
        state = copy.deepcopy(self._state())
        current = state["current"]
        previous = state["previous"]
        if previous is None:
            raise StateError("PREVIOUS is not available")
        state["current"] = previous
        state["previous"] = current
        self._record_history(state["history"], previous, operation_id)
        state["generation"] += 1
        self._commit(state)
        return copy.deepcopy(previous)

    def read(self) -> dict[str, object]:
        state = self._state()
        return {
            "current": copy.deepcopy(state["current"]),
            "previous": copy.deepcopy(state["previous"]),
            "history": copy.deepcopy(state["history"]),
            "generation": state["generation"],
        }
