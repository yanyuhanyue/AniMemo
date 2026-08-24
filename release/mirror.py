"""Official Mirror exact-byte replication; GitHub remains release authority."""

from __future__ import annotations

import argparse
import copy
import hashlib
import hmac
import http.client
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

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
_RELEASE_TAG = re.compile(
    r"v(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
    r"(?:-rc\.[1-9][0-9]*)?"
)
_UTC_SECONDS = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z"
)
_ACCOUNT_ID = re.compile(r"[0-9a-f]{32}")
_ACCESS_KEY_ID = re.compile(r"[A-Za-z0-9_-]{16,128}")
_SECRET_ACCESS_KEY = re.compile(r"[A-Za-z0-9/+_=.-]{32,256}")
_EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()

MIRROR_PROVIDER = "CLOUDFLARE_R2"
MIRROR_BUCKET = "animemo-release-mirror"
MIRROR_ORIGIN = "https://download.animemo.cc"
MIRROR_PATH_PREFIX = "yanyuhanyue/AniMemo/releases/download"
MIRROR_PUBLISHER_WORKFLOW = ".github/workflows/release-mirror.yml"
CACHE_CONTROL = "public,max-age=31536000,immutable"
MIRROR_RECEIPT_NAME = "mirror-receipt.json"
MIRROR_RECEIPT_SCHEMA_VERSION = 1
MAX_MIRROR_RECEIPT_BYTES = 256 * 1024
_CONTENT_TYPES = {
    "checksums.txt": "text/plain; charset=utf-8",
    "deployment-contract.json": "application/json",
    "installer-materials.tar": "application/x-tar",
    "release-manifest.json": "application/json",
    MIRROR_RECEIPT_NAME: "application/json",
}


class MirrorError(ValueError):
    """Mirror replication would alter or compete with release authority."""


class MirrorObjectConflict(MirrorError):
    """An immutable mirror key already exists with different bytes."""


class MirrorObjectStore(Protocol):
    def read_to(self, key: str, destination: Path) -> bool: ...

    def put_file_if_absent(
        self,
        key: str,
        source: Path,
        *,
        content_type: str,
        cache_control: str,
    ) -> bool: ...


class MirrorPublicReader(Protocol):
    def read_to(
        self, url: str, destination: Path
    ) -> tuple[int, Mapping[str, str]]: ...

    def first_mib(
        self, url: str
    ) -> tuple[int, Mapping[str, str], bytes]: ...


def _canonical_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True)
        + "\n"
    ).encode("utf-8")


def _identity(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical_bytes(value)).hexdigest()


def mirror_release_assets(tag: str) -> tuple[str, ...]:
    if not isinstance(tag, str) or _RELEASE_TAG.fullmatch(tag) is None:
        raise MirrorError("mirror release tag is outside the closed SemVer contract")
    return (
        "checksums.txt",
        "deployment-contract.json",
        "installer-materials.tar",
        "release-manifest.json",
        portable_release_asset_name(tag),
    )


def mirror_source_release_url_identity(tag: str) -> str:
    mirror_release_assets(tag)
    source = f"https://github.com/yanyuhanyue/AniMemo/releases/tag/{tag}"
    return "sha256:" + hashlib.sha256(source.encode("ascii")).hexdigest()


def _mirror_receipt_assets(tag: str, value: Any) -> list[dict[str, Any]]:
    expected_names = mirror_release_assets(tag)
    if not isinstance(value, list) or len(value) != len(expected_names):
        raise MirrorError("mirror receipt asset inventory is not exact")
    result: list[dict[str, Any]] = []
    for expected_name, item in zip(expected_names, value, strict=True):
        if not isinstance(item, Mapping) or set(item) != {"name", "size", "sha256"}:
            raise MirrorError("mirror receipt asset identity is not closed")
        size = item["size"]
        if (
            item["name"] != expected_name
            or isinstance(size, bool)
            or not isinstance(size, int)
            or size < 1
            or size > MAX_PORTABLE_TOTAL_BYTES
        ):
            raise MirrorError("mirror receipt asset identity is invalid")
        result.append(
            {
                "name": expected_name,
                "size": size,
                "sha256": _digest(item["sha256"], f"assets.{expected_name}.sha256"),
            }
        )
    return result


def build_mirror_receipt(
    *,
    release_tag: str,
    release_id: int,
    release_commit: str,
    assets: Any,
    publisher_run_id: int,
    published_at: str,
) -> dict[str, Any]:
    asset_inventory = _mirror_receipt_assets(release_tag, assets)
    if (
        isinstance(release_id, bool)
        or not isinstance(release_id, int)
        or release_id < 1
        or not isinstance(release_commit, str)
        or _COMMIT.fullmatch(release_commit) is None
        or isinstance(publisher_run_id, bool)
        or not isinstance(publisher_run_id, int)
        or publisher_run_id < 1
        or not isinstance(published_at, str)
        or _UTC_SECONDS.fullmatch(published_at) is None
    ):
        raise MirrorError("mirror receipt release or publisher identity is invalid")
    unsigned: dict[str, Any] = {
        "schemaVersion": MIRROR_RECEIPT_SCHEMA_VERSION,
        "repository": "yanyuhanyue/AniMemo",
        "releaseTag": release_tag,
        "releaseId": release_id,
        "releaseCommit": release_commit,
        "releaseImmutable": True,
        "assetCount": len(asset_inventory),
        "assets": asset_inventory,
        "sourceReleaseUrlIdentity": mirror_source_release_url_identity(release_tag),
        "mirrorOrigin": MIRROR_ORIGIN,
        "mirrorPrefix": MIRROR_PATH_PREFIX,
        "publisherWorkflow": MIRROR_PUBLISHER_WORKFLOW,
        "publisherRunId": publisher_run_id,
        "publishedAt": published_at,
    }
    return {**unsigned, "receiptDigest": _identity(unsigned)}


def validate_mirror_receipt(value: Any) -> dict[str, Any]:
    required = {
        "schemaVersion",
        "repository",
        "releaseTag",
        "releaseId",
        "releaseCommit",
        "releaseImmutable",
        "assetCount",
        "assets",
        "sourceReleaseUrlIdentity",
        "mirrorOrigin",
        "mirrorPrefix",
        "publisherWorkflow",
        "publisherRunId",
        "publishedAt",
        "receiptDigest",
    }
    if not isinstance(value, Mapping) or set(value) != required:
        raise MirrorError("mirror receipt has unknown or missing fields")
    if (
        value["schemaVersion"] != MIRROR_RECEIPT_SCHEMA_VERSION
        or value["repository"] != "yanyuhanyue/AniMemo"
        or value["releaseImmutable"] is not True
        or value["assetCount"] != 5
        or value["mirrorOrigin"] != MIRROR_ORIGIN
        or value["mirrorPrefix"] != MIRROR_PATH_PREFIX
        or value["publisherWorkflow"] != MIRROR_PUBLISHER_WORKFLOW
        or value["sourceReleaseUrlIdentity"]
        != mirror_source_release_url_identity(value["releaseTag"])
    ):
        raise MirrorError("mirror receipt violates the transport-only contract")
    rebuilt = build_mirror_receipt(
        release_tag=value["releaseTag"],
        release_id=value["releaseId"],
        release_commit=value["releaseCommit"],
        assets=value["assets"],
        publisher_run_id=value["publisherRunId"],
        published_at=value["publishedAt"],
    )
    if dict(value) != rebuilt:
        raise MirrorError("mirror receipt digest or identity differs")
    return copy.deepcopy(rebuilt)


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise MirrorError("mirror receipt contains a duplicate JSON key")
        result[key] = value
    return result


def load_mirror_receipt_bytes(data: bytes) -> dict[str, Any]:
    if not isinstance(data, bytes) or not 1 <= len(data) <= MAX_MIRROR_RECEIPT_BYTES:
        raise MirrorError("mirror receipt byte length is invalid")
    try:
        value = json.loads(
            data.decode("utf-8", errors="strict"),
            object_pairs_hook=_unique_json_object,
            parse_constant=lambda _value: (_ for _ in ()).throw(
                MirrorError("mirror receipt contains a non-finite number")
            ),
        )
    except MirrorError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise MirrorError("mirror receipt is not strict JSON") from error
    validated = validate_mirror_receipt(value)
    if data != _canonical_bytes(validated):
        raise MirrorError("mirror receipt is not canonical JSON")
    return validated


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


def _receipt_release_binding(value: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: copy.deepcopy(value[key])
        for key in (
            "schemaVersion",
            "repository",
            "releaseTag",
            "releaseId",
            "releaseCommit",
            "releaseImmutable",
            "assetCount",
            "assets",
            "sourceReleaseUrlIdentity",
            "mirrorOrigin",
            "mirrorPrefix",
            "publisherWorkflow",
        )
    }


def _asset_content_type(name: str) -> str:
    if name.startswith("animemo-v") and name.endswith("-portable.tar"):
        return "application/x-tar"
    try:
        return _CONTENT_TYPES[name]
    except KeyError as error:
        raise MirrorError("mirror content type is outside the closed contract") from error


def _verified_asset_identity(path: Path, declared: Mapping[str, Any]) -> None:
    digest, size = _hash_regular_file(path, maximum=declared["size"])
    if digest != declared["sha256"] or size != declared["size"]:
        raise MirrorObjectConflict(f"mirror object bytes differ: {declared['name']}")


def _header(headers: Mapping[str, str], name: str) -> str | None:
    expected = name.lower()
    for key, value in headers.items():
        if key.lower() == expected:
            return value
    return None


class OfficialReleaseMirrorPublisher:
    """Publish a complete immutable Release path through a narrow storage seam."""

    def __init__(
        self,
        *,
        store: MirrorObjectStore,
        public_reader: MirrorPublicReader,
    ) -> None:
        self._store = store
        self._public_reader = public_reader

    def _verify_public_asset(
        self,
        *,
        source: Path,
        declared: Mapping[str, Any],
        key: str,
        scratch: Path,
        label: str,
        verify_range: bool,
    ) -> None:
        public_path = scratch / f"public-{label}"
        url = f"{MIRROR_ORIGIN}/{key}"
        status, headers = self._public_reader.read_to(url, public_path)
        if (
            status != 200
            or _header(headers, "Content-Length") != str(declared["size"])
            or _header(headers, "Accept-Ranges") != "bytes"
            or _header(headers, "Content-Type")
            != _asset_content_type(declared["name"])
            or _header(headers, "Cache-Control") != CACHE_CONTROL
            or _header(headers, "Content-Encoding") is not None
            or _header(headers, "Access-Control-Allow-Origin") == "*"
        ):
            raise MirrorError("mirror public full-object readback failed")
        _verified_asset_identity(public_path, declared)
        if not verify_range:
            return
        range_status, range_headers, first_mib = self._public_reader.first_mib(url)
        with source.open("rb") as stream:
            expected_first_mib = stream.read(1024 * 1024)
        if (
            range_status != 206
            or _header(range_headers, "Accept-Ranges") != "bytes"
            or first_mib != expected_first_mib
        ):
            raise MirrorError("mirror public first-MiB Range readback failed")

    def publish(
        self,
        *,
        receipt: Mapping[str, Any],
        asset_directory: Path,
    ) -> dict[str, Any]:
        requested_receipt = validate_mirror_receipt(receipt)
        directory = _safe_directory(Path(asset_directory), label="mirror asset")
        expected_names = mirror_release_assets(requested_receipt["releaseTag"])
        try:
            observed_names = {item.name for item in directory.iterdir()}
        except OSError as error:
            raise MirrorError("mirror asset directory is unreadable") from error
        if observed_names != set(expected_names):
            raise MirrorError("mirror asset directory has missing or extra entries")

        prefix = (
            f"{MIRROR_PATH_PREFIX}/{requested_receipt['releaseTag']}"
        )
        marker_key = f"{prefix}/{MIRROR_RECEIPT_NAME}"
        uploaded = 0
        existing_equal = 0

        with tempfile.TemporaryDirectory(prefix="animemo-mirror-publish-") as temporary:
            scratch = Path(temporary)
            existing_marker_path = scratch / "existing-marker.json"
            marker_exists = self._store.read_to(marker_key, existing_marker_path)
            active_receipt = requested_receipt
            if marker_exists:
                existing_receipt = load_mirror_receipt_bytes(
                    existing_marker_path.read_bytes()
                )
                if _receipt_release_binding(existing_receipt) != _receipt_release_binding(
                    requested_receipt
                ):
                    raise MirrorObjectConflict(
                        "existing mirror marker belongs to different release bytes"
                    )
                active_receipt = existing_receipt

            for index, declared in enumerate(active_receipt["assets"]):
                name = declared["name"]
                source = directory / name
                _verified_asset_identity(source, declared)
                key = f"{prefix}/{name}"
                readback = scratch / f"authenticated-{index}"
                if self._store.read_to(key, readback):
                    _verified_asset_identity(readback, declared)
                    existing_equal += 1
                else:
                    if marker_exists:
                        raise MirrorObjectConflict(
                            "ready marker exists before the complete asset set"
                        )
                    created = self._store.put_file_if_absent(
                        key,
                        source,
                        content_type=_asset_content_type(name),
                        cache_control=CACHE_CONTROL,
                    )
                    if created:
                        uploaded += 1
                    if readback.exists():
                        readback.unlink()
                    if not self._store.read_to(key, readback):
                        raise MirrorError("mirror object is absent after conditional write")
                    _verified_asset_identity(readback, declared)
                    if not created:
                        existing_equal += 1

                self._verify_public_asset(
                    source=source,
                    declared=declared,
                    key=key,
                    scratch=scratch,
                    label=f"before-marker-{index}",
                    verify_range=True,
                )

            marker_bytes = _canonical_bytes(active_receipt)
            marker_source = scratch / "mirror-receipt.json"
            marker_source.write_bytes(marker_bytes)
            if marker_exists:
                existing_equal += 1
            else:
                created = self._store.put_file_if_absent(
                    marker_key,
                    marker_source,
                    content_type=_asset_content_type(MIRROR_RECEIPT_NAME),
                    cache_control=CACHE_CONTROL,
                )
                marker_readback = scratch / "authenticated-marker.json"
                if not self._store.read_to(marker_key, marker_readback):
                    raise MirrorError("mirror marker is absent after conditional write")
                raced_receipt = load_mirror_receipt_bytes(marker_readback.read_bytes())
                if _receipt_release_binding(raced_receipt) != _receipt_release_binding(
                    active_receipt
                ):
                    raise MirrorObjectConflict(
                        "concurrent mirror marker belongs to different release bytes"
                    )
                active_receipt = raced_receipt
                marker_bytes = marker_readback.read_bytes()
                if created:
                    uploaded += 1
                else:
                    existing_equal += 1

            public_marker = scratch / "public-marker.json"
            marker_status, marker_headers = self._public_reader.read_to(
                f"{MIRROR_ORIGIN}/{marker_key}", public_marker
            )
            if (
                marker_status != 200
                or _header(marker_headers, "Content-Length")
                != str(len(marker_bytes))
                or _header(marker_headers, "Content-Type") != "application/json"
                or _header(marker_headers, "Cache-Control") != CACHE_CONTROL
                or _header(marker_headers, "Content-Encoding") is not None
                or _header(marker_headers, "Access-Control-Allow-Origin") == "*"
                or public_marker.read_bytes() != marker_bytes
            ):
                raise MirrorError("mirror public completeness marker readback failed")
            load_mirror_receipt_bytes(public_marker.read_bytes())
            for index, declared in enumerate(active_receipt["assets"]):
                name = declared["name"]
                self._verify_public_asset(
                    source=directory / name,
                    declared=declared,
                    key=f"{prefix}/{name}",
                    scratch=scratch,
                    label=f"after-marker-{index}",
                    verify_range=False,
                )

        return {
            "schemaVersion": 1,
            "role": "NON_AUTHORITY_TRANSPORT_VERIFICATION_EVIDENCE",
            "repository": "yanyuhanyue/AniMemo",
            "releaseTag": active_receipt["releaseTag"],
            "releaseId": active_receipt["releaseId"],
            "releaseCommit": active_receipt["releaseCommit"],
            "mirrorOrigin": MIRROR_ORIGIN,
            "mirrorPrefix": MIRROR_PATH_PREFIX,
            "assetCount": 5,
            "uploadedObjectCount": uploaded,
            "existingEqualObjectCount": existing_equal,
            "objectOverwriteCount": 0,
            "existingMismatchCount": 0,
            "rangeStatus": "PASS",
            "publicReadback": "PASS",
            "receiptDigest": active_receipt["receiptDigest"],
        }


def _validated_object_key(key: str) -> str:
    if not isinstance(key, str) or not key.startswith(MIRROR_PATH_PREFIX + "/"):
        raise MirrorError("R2 object key is outside the fixed mirror prefix")
    suffix = key.removeprefix(MIRROR_PATH_PREFIX + "/")
    parts = suffix.split("/")
    if len(parts) != 2:
        raise MirrorError("R2 object key is outside the exact-tag layout")
    tag, name = parts
    allowed = {*mirror_release_assets(tag), MIRROR_RECEIPT_NAME}
    if name not in allowed:
        raise MirrorError("R2 object key is outside the exact asset contract")
    return key


def _sigv4_hmac(key: bytes, value: str) -> bytes:
    return hmac.new(key, value.encode("utf-8"), hashlib.sha256).digest()


class R2S3ObjectStore:
    """Pinned standard-library SigV4 adapter for one bucket and closed keys."""

    def __init__(
        self,
        *,
        account_id: str,
        access_key_id: str,
        secret_access_key: str,
        timeout_seconds: int = 900,
    ) -> None:
        if (
            not isinstance(account_id, str)
            or _ACCOUNT_ID.fullmatch(account_id) is None
            or not isinstance(access_key_id, str)
            or _ACCESS_KEY_ID.fullmatch(access_key_id) is None
            or not isinstance(secret_access_key, str)
            or _SECRET_ACCESS_KEY.fullmatch(secret_access_key) is None
            or isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, int)
            or not 1 <= timeout_seconds <= 900
        ):
            raise MirrorError("R2 bucket-only credentials are unavailable or invalid")
        self._host = f"{account_id}.r2.cloudflarestorage.com"
        self._access_key_id = access_key_id
        self._secret_access_key = secret_access_key
        self._timeout_seconds = timeout_seconds

    @classmethod
    def from_environment(cls) -> R2S3ObjectStore:
        names = (
            "ANIMEMO_RELEASE_MIRROR_ACCOUNT_ID",
            "ANIMEMO_RELEASE_MIRROR_ACCESS_KEY_ID",
            "ANIMEMO_RELEASE_MIRROR_SECRET_ACCESS_KEY",
        )
        values = [os.environ.get(name) for name in names]
        if any(value is None for value in values):
            raise MirrorError("R2 bucket-only credentials are unavailable")
        return cls(
            account_id=values[0],
            access_key_id=values[1],
            secret_access_key=values[2],
        )

    @staticmethod
    def _canonical_path(key: str) -> str:
        _validated_object_key(key)
        segments = (MIRROR_BUCKET, *key.split("/"))
        return "/" + "/".join(quote(segment, safe="-_.~") for segment in segments)

    def _signed_headers(
        self,
        *,
        method: str,
        canonical_path: str,
        payload_sha256: str,
        extra_headers: Mapping[str, str],
    ) -> dict[str, str]:
        now = datetime.now(timezone.utc)
        amz_date = now.strftime("%Y%m%dT%H%M%SZ")
        date = now.strftime("%Y%m%d")
        headers = {
            "host": self._host,
            "x-amz-content-sha256": payload_sha256,
            "x-amz-date": amz_date,
            **{key.lower(): value.strip() for key, value in extra_headers.items()},
        }
        signed_names = ";".join(sorted(headers))
        canonical_headers = "".join(
            f"{name}:{' '.join(headers[name].split())}\n" for name in sorted(headers)
        )
        canonical_request = (
            f"{method}\n{canonical_path}\n\n{canonical_headers}\n"
            f"{signed_names}\n{payload_sha256}"
        )
        scope = f"{date}/auto/s3/aws4_request"
        string_to_sign = "\n".join(
            (
                "AWS4-HMAC-SHA256",
                amz_date,
                scope,
                hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
            )
        )
        date_key = _sigv4_hmac(
            ("AWS4" + self._secret_access_key).encode("utf-8"), date
        )
        region_key = _sigv4_hmac(date_key, "auto")
        service_key = _sigv4_hmac(region_key, "s3")
        signing_key = _sigv4_hmac(service_key, "aws4_request")
        signature = hmac.new(
            signing_key, string_to_sign.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        return {
            **headers,
            "authorization": (
                "AWS4-HMAC-SHA256 "
                f"Credential={self._access_key_id}/{scope},"
                f"SignedHeaders={signed_names},Signature={signature}"
            ),
        }

    def _request(
        self,
        *,
        method: str,
        key: str,
        payload_sha256: str,
        extra_headers: Mapping[str, str],
        source: Path | None = None,
    ) -> tuple[http.client.HTTPSConnection, http.client.HTTPResponse]:
        path = self._canonical_path(key)
        headers = self._signed_headers(
            method=method,
            canonical_path=path,
            payload_sha256=payload_sha256,
            extra_headers=extra_headers,
        )
        connection = http.client.HTTPSConnection(
            self._host, timeout=self._timeout_seconds
        )
        try:
            connection.putrequest(
                method,
                path,
                skip_host=True,
                skip_accept_encoding=True,
            )
            for name, value in headers.items():
                connection.putheader(name, value)
            connection.endheaders()
            if source is not None:
                with source.open("rb") as stream:
                    while chunk := stream.read(PORTABLE_STREAM_CHUNK_BYTES):
                        connection.send(chunk)
            response = connection.getresponse()
            return connection, response
        except (OSError, http.client.HTTPException) as error:
            connection.close()
            raise MirrorError("R2 authenticated object request failed") from error

    def read_to(self, key: str, destination: Path) -> bool:
        _validated_object_key(key)
        connection, response = self._request(
            method="GET",
            key=key,
            payload_sha256=_EMPTY_SHA256,
            extra_headers={},
        )
        try:
            if response.status == 404:
                response.read(4096)
                return False
            if response.status != 200:
                response.read(4096)
                raise MirrorError("R2 authenticated object read failed")
            declared = response.getheader("Content-Length")
            try:
                expected_size = int(declared) if declared is not None else None
            except ValueError as error:
                raise MirrorError("R2 authenticated object length is invalid") from error
            if expected_size is None or not 1 <= expected_size <= MAX_PORTABLE_TOTAL_BYTES:
                raise MirrorError("R2 authenticated object length is invalid")
            consumed = 0
            descriptor = os.open(
                destination,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
                0o600,
            )
            try:
                with os.fdopen(descriptor, "wb", closefd=True) as output:
                    while chunk := response.read(PORTABLE_STREAM_CHUNK_BYTES):
                        consumed += len(chunk)
                        if consumed > expected_size:
                            raise MirrorError("R2 authenticated object exceeded its length")
                        output.write(chunk)
                    output.flush()
                    os.fsync(output.fileno())
            except BaseException:
                destination.unlink(missing_ok=True)
                raise
            if consumed != expected_size:
                destination.unlink(missing_ok=True)
                raise MirrorError("R2 authenticated object length changed")
            return True
        finally:
            connection.close()

    def put_file_if_absent(
        self,
        key: str,
        source: Path,
        *,
        content_type: str,
        cache_control: str,
    ) -> bool:
        _validated_object_key(key)
        if content_type != _asset_content_type(source.name) or cache_control != CACHE_CONTROL:
            raise MirrorError("R2 object metadata is outside the immutable contract")
        digest, size = _hash_regular_file(source, maximum=MAX_PORTABLE_TOTAL_BYTES)
        connection, response = self._request(
            method="PUT",
            key=key,
            payload_sha256=digest.removeprefix("sha256:"),
            extra_headers={
                "cache-control": cache_control,
                "content-length": str(size),
                "content-type": content_type,
                "if-none-match": "*",
            },
            source=source,
        )
        try:
            response.read(4096)
            if response.status in {200, 201}:
                return True
            if response.status == 412:
                return False
            raise MirrorError("R2 conditional object write failed")
        finally:
            connection.close()


class _RejectPublicRedirects(HTTPRedirectHandler):
    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        del request, file_pointer, code, message, headers, new_url


class OfficialMirrorPublicReader:
    def __init__(self, *, opener=None, timeout_seconds: int = 900) -> None:
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, int)
            or not 1 <= timeout_seconds <= 900
        ):
            raise MirrorError("Official Mirror public timeout is invalid")
        self._opener = opener or build_opener(_RejectPublicRedirects())
        self._timeout_seconds = timeout_seconds

    @staticmethod
    def _request(url: str, *, range_header: str | None = None) -> Request:
        parsed = urlsplit(url)
        origin = urlsplit(MIRROR_ORIGIN)
        if (
            parsed.scheme != "https"
            or parsed.netloc != origin.netloc
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or not parsed.path.startswith("/" + MIRROR_PATH_PREFIX + "/")
        ):
            raise MirrorError("Official Mirror public URL escaped the fixed origin")
        _validated_object_key(parsed.path.removeprefix("/"))
        headers = {"Accept": "application/octet-stream", "User-Agent": "AniMemo-Mirror-Publisher"}
        if range_header is not None:
            headers["Range"] = range_header
        return Request(url, headers=headers, method="GET")

    def read_to(self, url: str, destination: Path) -> tuple[int, Mapping[str, str]]:
        request = self._request(url)
        try:
            response_context = self._opener.open(
                request, timeout=self._timeout_seconds
            )
        except HTTPError as error:
            return error.code, dict(error.headers.items()) if error.headers else {}
        except (TimeoutError, URLError, OSError) as error:
            raise MirrorError("Official Mirror public readback failed") from error
        with response_context as response:
            if response.geturl() != url:
                raise MirrorError("Official Mirror public redirect was rejected")
            status = getattr(response, "status", 200)
            consumed = 0
            descriptor = os.open(
                destination,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
                0o600,
            )
            try:
                with os.fdopen(descriptor, "wb", closefd=True) as output:
                    while chunk := response.read(PORTABLE_STREAM_CHUNK_BYTES):
                        consumed += len(chunk)
                        if consumed > MAX_PORTABLE_TOTAL_BYTES:
                            raise MirrorError("Official Mirror public object is too large")
                        output.write(chunk)
                    output.flush()
                    os.fsync(output.fileno())
            except BaseException:
                destination.unlink(missing_ok=True)
                raise
            return status, dict(response.headers.items())

    def first_mib(self, url: str) -> tuple[int, Mapping[str, str], bytes]:
        request = self._request(url, range_header="bytes=0-1048575")
        try:
            with self._opener.open(request, timeout=self._timeout_seconds) as response:
                if response.geturl() != url:
                    raise MirrorError("Official Mirror public redirect was rejected")
                body = response.read(1024 * 1024 + 1)
                if len(body) > 1024 * 1024:
                    raise MirrorError("Official Mirror Range response is too large")
                return getattr(response, "status", 200), dict(response.headers.items()), body
        except HTTPError as error:
            body = error.read(1024 * 1024 + 1)
            return error.code, dict(error.headers.items()) if error.headers else {}, body
        except (TimeoutError, URLError, OSError) as error:
            raise MirrorError("Official Mirror public Range readback failed") from error


def _strict_json_file(path: Path, *, label: str) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
        value = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=_unique_json_object,
            parse_constant=lambda _value: (_ for _ in ()).throw(
                MirrorError(f"{label} contains a non-finite number")
            ),
        )
    except MirrorError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise MirrorError(f"{label} is unreadable or invalid") from error
    if not isinstance(value, dict):
        raise MirrorError(f"{label} is not a JSON object")
    return value


def _gh_environment() -> dict[str, str]:
    allowed = (
        "GH_TOKEN",
        "GITHUB_TOKEN",
        "GH_CONFIG_DIR",
        "HOME",
        "PATH",
        "RUNNER_TEMP",
        "LANG",
        "LC_ALL",
        "SYSTEMROOT",
    )
    environment = {
        key: os.environ[key]
        for key in allowed
        if os.environ.get(key)
    }
    environment["GH_PROMPT_DISABLED"] = "1"
    return environment


def _run_gh(arguments: tuple[str, ...], *, cwd: Path | None = None) -> bytes:
    executable = "/usr/bin/gh" if os.name == "posix" else "gh"
    try:
        result = subprocess.run(
            [executable, *arguments],
            cwd=cwd,
            env=_gh_environment(),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=False,
            shell=False,
            timeout=900,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise MirrorError("GitHub Release authority command failed") from error
    if result.returncode != 0 or len(result.stdout) > 16 * 1024 * 1024:
        raise MirrorError("GitHub Release authority command failed")
    return result.stdout


def _gh_json(arguments: tuple[str, ...]) -> dict[str, Any]:
    output = _run_gh(arguments)
    try:
        value = json.loads(
            output.decode("utf-8", errors="strict"),
            object_pairs_hook=_unique_json_object,
        )
    except MirrorError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise MirrorError("GitHub Release authority returned invalid JSON") from error
    if not isinstance(value, dict):
        raise MirrorError("GitHub Release authority returned invalid metadata")
    return value


def _verify_checksums(directory: Path) -> None:
    expected_names = (
        "release-manifest.json",
        "deployment-contract.json",
        "installer-materials.tar",
    )
    try:
        lines = (directory / "checksums.txt").read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise MirrorError("Release checksums are unreadable") from error
    expected: dict[str, str] = {}
    for line in lines:
        digest, separator, name = line.partition("  ")
        if (
            separator != "  "
            or re.fullmatch(r"[0-9a-f]{64}", digest) is None
            or name not in expected_names
            or name in expected
        ):
            raise MirrorError("Release checksums differ from the closed contract")
        expected[name] = digest
    if set(expected) != set(expected_names):
        raise MirrorError("Release checksums do not cover the exact contract")
    for name in expected_names:
        digest, _size = _hash_regular_file(
            directory / name, maximum=MAX_PORTABLE_TOTAL_BYTES
        )
        if digest.removeprefix("sha256:") != expected[name]:
            raise MirrorError("Release checksum verification failed")


def _verify_github_release_authority(
    tag: str,
    directory: Path,
) -> tuple[dict[str, Any], str]:
    from .contract import (
        API_REPOSITORY,
        REPOSITORY,
        WEB_REPOSITORY,
        deployment_contract_digest,
        validate_deployment_contract,
        validate_manifest,
    )
    from .portable import inspect_portable_archive

    if REPOSITORY != "yanyuhanyue/AniMemo" or _RELEASE_TAG.fullmatch(tag) is None:
        raise MirrorError("Mirror publisher accepts only the fixed repository and tag")
    metadata = _gh_json(("api", f"repos/{REPOSITORY}/releases/tags/{tag}"))
    expected_names = mirror_release_assets(tag)
    raw_assets = metadata.get("assets")
    if (
        metadata.get("tag_name") != tag
        or metadata.get("name") != tag
        or metadata.get("draft") is not False
        or metadata.get("immutable") is not True
        or not isinstance(metadata.get("id"), int)
        or not isinstance(raw_assets, list)
    ):
        raise MirrorError("GitHub Immutable Release metadata is invalid")
    inventory: dict[str, dict[str, Any]] = {}
    for item in raw_assets:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("name"), str)
            or item.get("state") != "uploaded"
            or isinstance(item.get("size"), bool)
            or not isinstance(item.get("size"), int)
            or item["size"] < 1
            or not isinstance(item.get("digest"), str)
            or _SHA256.fullmatch(item["digest"]) is None
            or item["name"] in inventory
        ):
            raise MirrorError("GitHub Release asset inventory is invalid")
        inventory[item["name"]] = item
    if set(inventory) != set(expected_names) or len(inventory) != 5:
        raise MirrorError("GitHub Release asset inventory is not the exact five-set")

    tag_ref = _gh_json(("api", f"repos/{REPOSITORY}/git/ref/tags/{tag}"))
    target = tag_ref.get("object")
    if not isinstance(target, dict) or target.get("type") != "tag":
        raise MirrorError("Release tag is not an annotated tag")
    tag_object = _gh_json(("api", f"repos/{REPOSITORY}/git/tags/{target.get('sha')}"))
    peeled = tag_object.get("object")
    if (
        tag_object.get("tag") != tag
        or tag_object.get("message") != tag + "\n"
        or not isinstance(peeled, dict)
        or peeled.get("type") != "commit"
        or not isinstance(peeled.get("sha"), str)
        or _COMMIT.fullmatch(peeled["sha"]) is None
    ):
        raise MirrorError("Annotated Release tag subject or empty body is invalid")
    commit = peeled["sha"]

    _run_gh(("release", "verify", tag, "--repo", REPOSITORY))
    for name in expected_names:
        _run_gh(
            (
                "release",
                "download",
                tag,
                "--repo",
                REPOSITORY,
                "--pattern",
                name,
                "--dir",
                str(directory),
            )
        )
        path = directory / name
        digest, size = _hash_regular_file(path, maximum=MAX_PORTABLE_TOTAL_BYTES)
        if digest != inventory[name]["digest"] or size != inventory[name]["size"]:
            raise MirrorError("Downloaded GitHub Release bytes differ from metadata")
        _run_gh(("release", "verify-asset", tag, str(path), "--repo", REPOSITORY))

    if {item.name for item in directory.iterdir()} != set(expected_names):
        raise MirrorError("GitHub Release download directory is not closed")
    _verify_checksums(directory)
    manifest = _strict_json_file(
        directory / "release-manifest.json", label="Release Manifest"
    )
    deployment = _strict_json_file(
        directory / "deployment-contract.json", label="Deployment Contract"
    )
    try:
        validate_manifest(manifest, updater_version="1.0.0")
        validate_deployment_contract(
            deployment,
            installer_materials=directory / "installer-materials.tar",
        )
        inspection = inspect_portable_archive(directory / expected_names[-1])
    except ValueError as error:
        raise MirrorError("Release contract or Portable validation failed") from error
    if (
        manifest["release"]["version"] != tag
        or manifest["release"]["commit"] != commit
        or deployment_contract_digest(deployment)
        != manifest["deployment"]["contractSha256"]
        or inspection.archive_sha256 != inventory[expected_names[-1]]["digest"]
        or inspection.archive_size != inventory[expected_names[-1]]["size"]
    ):
        raise MirrorError("Release authority documents differ from the exact tag")

    signer = f"{REPOSITORY}/.github/workflows/release.yml"
    subjects = (
        f"oci://{API_REPOSITORY}@{manifest['images']['api']['digest']}",
        f"oci://{WEB_REPOSITORY}@{manifest['images']['web']['digest']}",
        str(directory / "release-manifest.json"),
        str(directory / "deployment-contract.json"),
        str(directory / "installer-materials.tar"),
    )
    for subject in subjects:
        _run_gh(
            (
                "attestation",
                "verify",
                subject,
                "--repo",
                REPOSITORY,
                "--signer-workflow",
                signer,
                "--source-digest",
                commit,
            )
        )
    return metadata, commit


def publish_release_mirror(tag: str) -> dict[str, Any]:
    if os.environ.get("GITHUB_REPOSITORY") != "yanyuhanyue/AniMemo":
        raise MirrorError("Mirror publisher is outside the fixed repository")
    workflow_ref = os.environ.get("GITHUB_WORKFLOW_REF", "")
    if workflow_ref != (
        "yanyuhanyue/AniMemo/.github/workflows/release-mirror.yml@refs/heads/main"
    ):
        raise MirrorError("Mirror publisher is not using the trusted default-branch definition")
    try:
        run_id = int(os.environ["GITHUB_RUN_ID"])
    except (KeyError, TypeError, ValueError) as error:
        raise MirrorError("Mirror publisher run identity is invalid") from error
    with tempfile.TemporaryDirectory(prefix="animemo-release-mirror-") as temporary:
        directory = Path(temporary)
        metadata, commit = _verify_github_release_authority(tag, directory)
        assets = [
            {
                "name": name,
                "size": next(
                    item["size"] for item in metadata["assets"] if item["name"] == name
                ),
                "sha256": next(
                    item["digest"] for item in metadata["assets"] if item["name"] == name
                ),
            }
            for name in mirror_release_assets(tag)
        ]
        receipt = build_mirror_receipt(
            release_tag=tag,
            release_id=metadata["id"],
            release_commit=commit,
            assets=assets,
            publisher_run_id=run_id,
            published_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
        publisher = OfficialReleaseMirrorPublisher(
            store=R2S3ObjectStore.from_environment(),
            public_reader=OfficialMirrorPublicReader(),
        )
        return publisher.publish(receipt=receipt, asset_directory=directory)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m release.mirror")
    parser.add_argument("--release-tag", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        evidence = publish_release_mirror(arguments.release_tag)
    except MirrorError as error:
        print(
            json.dumps(
                {"status": "FAIL", "reasonCode": type(error).__name__},
                separators=(",", ":"),
                sort_keys=True,
            )
        )
        return 1
    print(json.dumps(evidence, separators=(",", ":"), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
