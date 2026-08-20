"""Official Mirror exact-byte replication; GitHub remains release authority."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import stat
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from .acquisition import release_attestation_sidecar_name
from .notes import CANONICAL_RELEASE_ASSETS
from .portable import (
    MAX_PORTABLE_TOTAL_BYTES,
    PORTABLE_STREAM_CHUNK_BYTES,
    portable_release_asset_name,
)

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


def _offline_pair_asset(
    value: Any, *, expected_name: str, label: str
) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != {"name", "sha256", "size"}:
        raise MirrorError(f"{label} identity is not closed")
    size = value["size"]
    if (
        value["name"] != expected_name
        or isinstance(size, bool)
        or not isinstance(size, int)
        or size < 1
    ):
        raise MirrorError(f"{label} identity is invalid")
    return {
        "name": expected_name,
        "sha256": _digest(value["sha256"], f"{label}.sha256"),
        "size": size,
    }


def build_offline_pair_mirror_plan(
    *,
    authority: str,
    repository: str,
    tag: str,
    commit: str,
    release_identity: str,
    payload: Mapping[str, Any],
    release_attestation: Mapping[str, Any],
) -> dict[str, Any]:
    """Plan an exact two-file transport copy without creating mirror authority."""

    if authority != "GITHUB_RELEASE" or repository != "yanyuhanyue/AniMemo":
        raise MirrorError("Official Mirror cannot select or replace release authority")
    if not isinstance(commit, str) or not _COMMIT.fullmatch(commit):
        raise MirrorError("mirror commit is invalid")
    payload_name = portable_release_asset_name(tag)
    sidecar_name = release_attestation_sidecar_name(tag)
    unsigned = {
        "schema": "animemo.release-mirror-offline-pair-plan/v1",
        "authority": "GITHUB_RELEASE",
        "role": "TRANSPORT_ONLY",
        "repository": repository,
        "tag": tag,
        "commit": commit,
        "release_identity": _digest(release_identity, "release_identity"),
        "payload": _offline_pair_asset(
            payload, expected_name=payload_name, label="portable payload"
        ),
        "release_attestation": _offline_pair_asset(
            release_attestation,
            expected_name=sidecar_name,
            label="release attestation",
        ),
        "payload_source": "GITHUB_RELEASE_DECLARED_TRANSPORT_ASSET",
        "attestation_source": "GITHUB_POST_PUBLISH_ATTESTATION_EXPORT",
        "version_selection": "FORBIDDEN",
        "transformation": "FORBIDDEN",
        "fallback_policy": "FORBIDDEN",
    }
    return {**unsigned, "identity": _identity(unsigned)}


def validate_offline_pair_mirror_plan(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise MirrorError("offline pair mirror plan is missing")
    required = {
        "schema",
        "identity",
        "authority",
        "role",
        "repository",
        "tag",
        "commit",
        "release_identity",
        "payload",
        "release_attestation",
        "payload_source",
        "attestation_source",
        "version_selection",
        "transformation",
        "fallback_policy",
    }
    if set(value) != required:
        raise MirrorError("offline pair mirror plan has unknown or missing fields")
    rebuilt = build_offline_pair_mirror_plan(
        authority=value["authority"],
        repository=value["repository"],
        tag=value["tag"],
        commit=value["commit"],
        release_identity=value["release_identity"],
        payload=value["payload"],
        release_attestation=value["release_attestation"],
    )
    if dict(value) != rebuilt:
        raise MirrorError("offline pair mirror plan identity mismatch")
    return copy.deepcopy(rebuilt)


def replicate_offline_pair_exact_bytes(
    plan: Mapping[str, Any],
    *,
    fetched: Mapping[str, bytes],
    write: Callable[[str, bytes], None],
    readback: Callable[[str], bytes],
) -> dict[str, Any]:
    validated = validate_offline_pair_mirror_plan(plan)
    expected = {
        validated["payload"]["name"]: validated["payload"],
        validated["release_attestation"]["name"]: validated[
            "release_attestation"
        ],
    }
    if not isinstance(fetched, Mapping) or set(fetched) != set(expected):
        raise MirrorError("offline pair fetched inventory has missing or extra assets")
    for name, declared in expected.items():
        content = fetched[name]
        if not isinstance(content, bytes):
            raise MirrorError(f"offline pair asset is not bytes: {name}")
        actual = {
            "sha256": "sha256:" + hashlib.sha256(content).hexdigest(),
            "size": len(content),
        }
        if actual != {"sha256": declared["sha256"], "size": declared["size"]}:
            raise MirrorError(f"offline pair authority identity mismatch: {name}")
        write(name, content)
        mirrored = readback(name)
        if not isinstance(mirrored, bytes) or mirrored != content:
            raise MirrorError(f"offline pair mirror changed bytes: {name}")
    unsigned = {
        "schema": "animemo.release-mirror-offline-pair-receipt/v1",
        "plan_identity": validated["identity"],
        "authority": "GITHUB_RELEASE",
        "role": "TRANSPORT_ONLY",
        "tag": validated["tag"],
        "commit": validated["commit"],
        "asset_count": 2,
        "fallback_count": 0,
        "status": "PASS",
    }
    return {**unsigned, "identity": _identity(unsigned)}


def _safe_directory(path: Path, *, label: str) -> Path:
    path = Path(path)
    try:
        metadata = path.lstat()
    except OSError as error:
        raise MirrorError(f"{label} directory is unavailable") from error
    if path.is_symlink() or not stat.S_ISDIR(metadata.st_mode):
        raise MirrorError(f"{label} directory is unsafe")
    return path


def _hash_regular_file(path: Path, *, maximum: int) -> tuple[str, int]:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise MirrorError("mirror source asset is unavailable") from error
    if (
        path.is_symlink()
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or metadata.st_size < 1
        or metadata.st_size > maximum
    ):
        raise MirrorError("mirror source asset is unsafe or exceeds resource limits")
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY
            | getattr(os, "O_BINARY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or opened.st_dev != metadata.st_dev
            or opened.st_ino != metadata.st_ino
            or opened.st_size != metadata.st_size
        ):
            os.close(descriptor)
            raise MirrorError("mirror source asset changed before open")
    except MirrorError:
        raise
    except OSError as error:
        raise MirrorError("mirror source asset is unreadable") from error
    hasher = hashlib.sha256()
    consumed = 0
    try:
        with os.fdopen(descriptor, "rb", closefd=True) as stream:
            while chunk := stream.read(PORTABLE_STREAM_CHUNK_BYTES):
                consumed += len(chunk)
                if consumed > maximum:
                    raise MirrorError("mirror source asset exceeds resource limits")
                hasher.update(chunk)
    except OSError as error:
        raise MirrorError("mirror source asset is unreadable") from error
    if consumed != metadata.st_size:
        raise MirrorError("mirror source asset changed during hashing")
    return "sha256:" + hasher.hexdigest(), consumed


def _stream_exact_file(source: Path, destination: Path, *, maximum: int) -> None:
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        source_metadata = source.lstat()
        source_descriptor = os.open(
            source,
            os.O_RDONLY
            | getattr(os, "O_BINARY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        opened = os.fstat(source_descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or opened.st_dev != source_metadata.st_dev
            or opened.st_ino != source_metadata.st_ino
            or opened.st_size != source_metadata.st_size
            or opened.st_size > maximum
        ):
            os.close(source_descriptor)
            raise MirrorError("mirror source asset changed before copy")
        descriptor = os.open(destination, flags, 0o600)
        source_stream = os.fdopen(source_descriptor, "rb", closefd=True)
    except MirrorError:
        raise
    except OSError as error:
        if "source_descriptor" in locals():
            try:
                os.close(source_descriptor)
            except OSError:
                pass
        raise MirrorError("mirror exclusive copy boundary failed") from error
    consumed = 0
    try:
        with source_stream, os.fdopen(descriptor, "wb", closefd=True) as output:
            while chunk := source_stream.read(PORTABLE_STREAM_CHUNK_BYTES):
                consumed += len(chunk)
                if consumed > maximum:
                    raise MirrorError("mirror source asset exceeds resource limits")
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
    except OSError as error:
        try:
            destination.unlink(missing_ok=True)
        except OSError:
            pass
        raise MirrorError("mirror exact byte copy failed") from error
    except BaseException:
        try:
            destination.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    if consumed != source_metadata.st_size:
        try:
            destination.unlink(missing_ok=True)
        except OSError:
            pass
        raise MirrorError("mirror source asset changed during copy")


def build_offline_pair_mirror_plan_from_files(
    *,
    authority: str,
    repository: str,
    tag: str,
    commit: str,
    release_identity: str,
    payload: Path,
    release_attestation: Path,
) -> dict[str, Any]:
    payload = Path(payload)
    sidecar = Path(release_attestation)
    if payload.name != portable_release_asset_name(tag):
        raise MirrorError("portable payload name does not match exact release tag")
    if sidecar.name != release_attestation_sidecar_name(tag):
        raise MirrorError("release attestation name does not match exact release tag")
    payload_digest, payload_size = _hash_regular_file(
        payload, maximum=MAX_PORTABLE_TOTAL_BYTES
    )
    sidecar_digest, sidecar_size = _hash_regular_file(
        sidecar, maximum=64 * 1024 * 1024
    )
    return build_offline_pair_mirror_plan(
        authority=authority,
        repository=repository,
        tag=tag,
        commit=commit,
        release_identity=release_identity,
        payload={
            "name": payload.name,
            "sha256": payload_digest,
            "size": payload_size,
        },
        release_attestation={
            "name": sidecar.name,
            "sha256": sidecar_digest,
            "size": sidecar_size,
        },
    )


def replicate_offline_pair_files(
    plan: Mapping[str, Any],
    *,
    source_directory: Path,
    destination_directory: Path,
) -> dict[str, Any]:
    """Stream an exact payload/sidecar pair through an empty per-release directory."""

    validated = validate_offline_pair_mirror_plan(plan)
    source = _safe_directory(source_directory, label="mirror source")
    destination = _safe_directory(destination_directory, label="mirror destination")
    expected = {
        validated["payload"]["name"]: (
            validated["payload"],
            MAX_PORTABLE_TOTAL_BYTES,
        ),
        validated["release_attestation"]["name"]: (
            validated["release_attestation"],
            64 * 1024 * 1024,
        ),
    }
    if {item.name for item in source.iterdir()} != set(expected):
        raise MirrorError("offline pair source inventory has missing or extra assets")
    if any(destination.iterdir()):
        raise MirrorError("offline pair destination must be an empty exclusive boundary")
    created: list[Path] = []
    try:
        for name, (declared, maximum) in expected.items():
            source_path = source / name
            source_identity = _hash_regular_file(source_path, maximum=maximum)
            if source_identity != (declared["sha256"], declared["size"]):
                raise MirrorError(f"offline pair authority identity mismatch: {name}")
            target = destination / name
            _stream_exact_file(source_path, target, maximum=maximum)
            created.append(target)
            if _hash_regular_file(target, maximum=maximum) != source_identity:
                raise MirrorError(f"offline pair mirror changed bytes: {name}")
    except BaseException:
        for target in created:
            try:
                target.unlink(missing_ok=True)
            except OSError:
                pass
        raise
    unsigned = {
        "schema": "animemo.release-mirror-offline-pair-receipt/v1",
        "plan_identity": validated["identity"],
        "authority": "GITHUB_RELEASE",
        "role": "TRANSPORT_ONLY",
        "tag": validated["tag"],
        "commit": validated["commit"],
        "asset_count": 2,
        "network_attempt": 0,
        "fallback_count": 0,
        "status": "PASS",
    }
    return {**unsigned, "identity": _identity(unsigned)}
