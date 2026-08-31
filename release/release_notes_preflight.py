"""Closed, run-scoped authority for frozen Release Notes metadata."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from collections.abc import Mapping
from typing import Any

from release.notes import (
    build_release_notes,
    render_release_notes,
    validate_release_notes,
)

SCHEMA = "animemo.release-notes-preflight/v1"
FILE_NAMES = (
    "release-notes-input.json",
    "release-notes.json",
    "release-notes.md",
    "release-notes-readback.json",
)
BINDING_FIELDS = {
    "repository",
    "run_id",
    "run_attempt",
    "head_sha",
    "head_tree",
    "comparison_base_sha",
    "previous_stable",
    "release_tag",
    "target_version",
    "channel",
}
TOP_FIELDS = {
    "schema",
    "binding",
    "files",
    "population",
    "release_notes_identity",
    "configuration_identity",
    "identity",
}
SHA40 = re.compile(r"[0-9a-f]{40}")
SHA256 = re.compile(r"sha256:[0-9a-f]{64}")
TAG = re.compile(r"v[0-9]+\.[0-9]+\.[0-9]+-(?:beta|rc)\.[1-9][0-9]*")
STABLE = re.compile(r"v[0-9]+\.[0-9]+\.[0-9]+")


class ReleaseNotesPreflightError(ValueError):
    """The frozen Release Notes artifact is incomplete or has drifted."""


def canonical_json_bytes(value: Any) -> bytes:
    """Return the canonical JSON representation used by all identities."""

    return (
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def _digest_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _identity(value: Any) -> str:
    return _digest_bytes(canonical_json_bytes(value))


def _json_file(files: Mapping[str, bytes], name: str) -> Any:
    value = files.get(name)
    if not isinstance(value, bytes):
        raise ReleaseNotesPreflightError(f"{name} must be supplied as bytes")
    try:
        parsed = json.loads(value)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ReleaseNotesPreflightError(f"{name} is not valid UTF-8 JSON") from error
    if value != canonical_json_bytes(parsed):
        raise ReleaseNotesPreflightError(f"{name} is not canonical JSON")
    return parsed


def _validate_binding(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != BINDING_FIELDS:
        raise ReleaseNotesPreflightError("release preflight binding is not closed")
    result = dict(value)
    if result["repository"] != "yanyuhanyue/AniMemo":
        raise ReleaseNotesPreflightError("release preflight repository is invalid")
    for field in ("run_id", "run_attempt"):
        if isinstance(result[field], bool) or not isinstance(result[field], int) or result[field] < 1:
            raise ReleaseNotesPreflightError(f"release preflight {field} is invalid")
    for field in ("head_sha", "head_tree", "comparison_base_sha"):
        if not isinstance(result[field], str) or not SHA40.fullmatch(result[field]):
            raise ReleaseNotesPreflightError(f"release preflight {field} is invalid")
    previous = result["previous_stable"]
    if not isinstance(previous, str) or (previous and not STABLE.fullmatch(previous)):
        raise ReleaseNotesPreflightError("release preflight previous_stable is invalid")
    if not isinstance(result["release_tag"], str) or not TAG.fullmatch(result["release_tag"]):
        raise ReleaseNotesPreflightError("release preflight release_tag is invalid")
    if not isinstance(result["target_version"], str) or not STABLE.fullmatch(result["target_version"]):
        raise ReleaseNotesPreflightError("release preflight target_version is invalid")
    if result["channel"] not in {"beta", "rc"}:
        raise ReleaseNotesPreflightError("release preflight channel is invalid")
    if not result["release_tag"].startswith(
        result["target_version"] + f"-{result['channel']}."
    ):
        raise ReleaseNotesPreflightError("release preflight release identity is inconsistent")
    return result


def _population(note_input: Any) -> tuple[list[dict[str, Any]], str, str]:
    if not isinstance(note_input, Mapping) or set(note_input) != {"context", "pulls"}:
        raise ReleaseNotesPreflightError("release note input is not closed")
    pulls = note_input["pulls"]
    if not isinstance(pulls, list):
        raise ReleaseNotesPreflightError("release note population must be a list")
    normalized: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    for pull in pulls:
        if not isinstance(pull, Mapping):
            raise ReleaseNotesPreflightError("release note population entry is invalid")
        observed_updated_at = pull.get("observed_updated_at")
        if not isinstance(observed_updated_at, str) or not observed_updated_at:
            raise ReleaseNotesPreflightError(
                "release note population lacks observed_updated_at"
            )
        normalized.append(copy.deepcopy(dict(pull)))
        events.append(
            {
                "labels": copy.deepcopy(pull.get("labels")),
                "number": pull.get("number"),
                "observed_updated_at": observed_updated_at,
            }
        )
    normalized.sort(key=lambda item: item.get("number"))
    events.sort(key=lambda item: item.get("number"))
    return normalized, _identity(normalized), _identity(events)


def _validate_readback(
    value: Any, *, population_digest: str, event_digest: str
) -> dict[str, Any]:
    expected = {
        "schema": "animemo.release-notes-readback/v1",
        "readback_count": 2,
        "population_digest": population_digest,
        "event_digest": event_digest,
    }
    if value != expected:
        raise ReleaseNotesPreflightError(
            "release preflight double-readback receipt is invalid"
        )
    return expected


def _validate_notes_binding(notes: Mapping[str, Any], binding: Mapping[str, Any]) -> None:
    context = notes["context"]
    expected_context = {
        "candidate_sha": binding["head_sha"],
        "comparison_base_sha": binding["comparison_base_sha"],
        "previous_stable": binding["previous_stable"],
        "release_tag": binding["release_tag"],
        "target_version": binding["target_version"],
        "channel": binding["channel"],
    }
    for field, expected in expected_context.items():
        if context.get(field) != expected:
            raise ReleaseNotesPreflightError(
                f"release notes context differs from binding: {field}"
            )


def build_preflight_manifest(
    *, binding: Mapping[str, Any], files: Mapping[str, bytes]
) -> dict[str, Any]:
    """Build and self-verify one immutable Release Notes preflight manifest."""

    normalized_binding = _validate_binding(binding)
    if set(files) != set(FILE_NAMES):
        raise ReleaseNotesPreflightError("release preflight file set is not closed")
    note_input = _json_file(files, "release-notes-input.json")
    notes = validate_release_notes(_json_file(files, "release-notes.json"))
    population, population_digest, event_digest = _population(note_input)
    readback = _validate_readback(
        _json_file(files, "release-notes-readback.json"),
        population_digest=population_digest,
        event_digest=event_digest,
    )
    rebuilt = build_release_notes(
        context=note_input["context"], pulls=note_input["pulls"]
    )
    if rebuilt != notes:
        raise ReleaseNotesPreflightError("release notes differ from frozen input")
    if files["release-notes.md"] != render_release_notes(notes).encode("utf-8"):
        raise ReleaseNotesPreflightError("release notes Markdown differs from snapshot")
    _validate_notes_binding(notes, normalized_binding)
    unsigned = {
        "schema": SCHEMA,
        "binding": normalized_binding,
        "files": {
            name: {"sha256": _digest_bytes(files[name]), "size": len(files[name])}
            for name in FILE_NAMES
        },
        "population": {
            "count": len(population),
            "digest": population_digest,
            "event_digest": event_digest,
            "readback_count": readback["readback_count"],
        },
        "release_notes_identity": notes["identity"],
        "configuration_identity": notes["configuration"]["identity"],
    }
    manifest = {**unsigned, "identity": _identity(unsigned)}
    verify_preflight_manifest(manifest, files=files, expected_binding=binding)
    return manifest


def verify_preflight_manifest(
    manifest: Any,
    *,
    files: Mapping[str, bytes],
    expected_binding: Mapping[str, Any],
) -> dict[str, Any]:
    """Verify closed files and exact run/source/boundary binding."""

    if not isinstance(manifest, Mapping) or set(manifest) != TOP_FIELDS:
        raise ReleaseNotesPreflightError("release preflight manifest is not closed")
    if manifest.get("schema") != SCHEMA:
        raise ReleaseNotesPreflightError("release preflight schema is invalid")
    binding = _validate_binding(manifest.get("binding"))
    expected = _validate_binding(expected_binding)
    if binding != expected:
        changed = sorted(
            field for field in BINDING_FIELDS if binding.get(field) != expected.get(field)
        )
        raise ReleaseNotesPreflightError(
            "release_preflight_binding_mismatch: " + ",".join(changed)
        )
    if set(files) != set(FILE_NAMES):
        raise ReleaseNotesPreflightError("release preflight file set is not closed")
    declared_files = manifest.get("files")
    expected_files = {
        name: {"sha256": _digest_bytes(files[name]), "size": len(files[name])}
        for name in FILE_NAMES
    }
    if declared_files != expected_files:
        raise ReleaseNotesPreflightError("release preflight file digest mismatch")
    note_input = _json_file(files, "release-notes-input.json")
    notes = validate_release_notes(_json_file(files, "release-notes.json"))
    population, population_digest, event_digest = _population(note_input)
    readback = _validate_readback(
        _json_file(files, "release-notes-readback.json"),
        population_digest=population_digest,
        event_digest=event_digest,
    )
    if manifest.get("population") != {
        "count": len(population),
        "digest": population_digest,
        "event_digest": event_digest,
        "readback_count": readback["readback_count"],
    }:
        raise ReleaseNotesPreflightError("release preflight population digest mismatch")
    rebuilt = build_release_notes(
        context=note_input["context"], pulls=note_input["pulls"]
    )
    if rebuilt != notes:
        raise ReleaseNotesPreflightError("release notes differ from frozen input")
    if files["release-notes.md"] != render_release_notes(notes).encode("utf-8"):
        raise ReleaseNotesPreflightError("release notes Markdown differs from snapshot")
    _validate_notes_binding(notes, binding)
    if manifest.get("release_notes_identity") != notes["identity"]:
        raise ReleaseNotesPreflightError("release notes identity mismatch")
    if manifest.get("configuration_identity") != notes["configuration"]["identity"]:
        raise ReleaseNotesPreflightError("release notes configuration identity mismatch")
    unsigned = copy.deepcopy(dict(manifest))
    identity = unsigned.pop("identity", None)
    if not isinstance(identity, str) or not SHA256.fullmatch(identity):
        raise ReleaseNotesPreflightError("release preflight identity is invalid")
    if identity != _identity(unsigned):
        raise ReleaseNotesPreflightError("release preflight identity mismatch")
    return copy.deepcopy(dict(manifest))
