"""Plan and verify the fail-closed GitHub Draft Release transaction."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import stat
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .notes import CANONICAL_RELEASE_ASSETS
from .portable import (
    MAX_PORTABLE_TOTAL_BYTES,
    PORTABLE_STREAM_CHUNK_BYTES,
    portable_release_asset_name,
)

LEGACY_SCHEMA = "animemo.release-publication-plan/v1"
SCHEMA = "animemo.release-publication-plan/v2"
STATES = (
    "NOT_STARTED",
    "TAG_CREATED",
    "DRAFT_CREATED",
    "ASSETS_UPLOADED",
    "DRAFT_VERIFIED",
    "PUBLISHED",
    "FAILED_PARTIAL",
)
_SHA256 = re.compile(r"sha256:[0-9a-f]{64}")
_COMMIT = re.compile(r"[0-9a-f]{40}")
_TAG = re.compile(
    r"v[0-9]+\.[0-9]+\.[0-9]+(?:-(?:beta|rc)\.(?:[1-9][0-9]*|TEST))?"
)
_MAX_METADATA_ASSET_BYTES = 4 * 1024 * 1024
_MAX_INSTALLER_MATERIALS_BYTES = 256 * 1024 * 1024
_MAX_PORTABLE_ARCHIVE_BYTES = MAX_PORTABLE_TOTAL_BYTES + (16 * 1024 * 1024)


class PublicationError(ValueError):
    """The publication transaction cannot preserve authority."""


def _canonical_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True)
        + "\n"
    ).encode("utf-8")


def _identity(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _digest(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise PublicationError(f"{field} must be a SHA-256 identity")
    return value


def _normalize_assets(value: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(value, Mapping) or set(value) != set(CANONICAL_RELEASE_ASSETS):
        raise PublicationError("publication asset inventory is not the frozen canonical set")
    normalized = {}
    for name in CANONICAL_RELEASE_ASSETS:
        item = value[name]
        if not isinstance(item, Mapping) or set(item) != {"sha256", "size"}:
            raise PublicationError(f"publication asset identity is not closed: {name}")
        size = item["size"]
        if (
            isinstance(size, bool)
            or not isinstance(size, int)
            or size < 0
            or size > _asset_size_ceiling(name)
        ):
            raise PublicationError(f"publication asset size is invalid: {name}")
        normalized[name] = {
            "sha256": _digest(item["sha256"], f"{name}.sha256"),
            "size": size,
        }
    return normalized


def _normalize_transport_assets(
    value: Any, *, tag: str
) -> dict[str, dict[str, Any]]:
    expected_name = portable_release_asset_name(tag)
    if not isinstance(value, Mapping) or set(value) != {expected_name}:
        raise PublicationError(
            "publication transport asset inventory is not the declared portable set"
        )
    item = value[expected_name]
    if not isinstance(item, Mapping) or set(item) != {"role", "sha256", "size"}:
        raise PublicationError("publication transport asset identity is not closed")
    size = item["size"]
    if (
        isinstance(size, bool)
        or not isinstance(size, int)
        or size < 1
        or size > _MAX_PORTABLE_ARCHIVE_BYTES
    ):
        raise PublicationError("publication transport asset size is invalid")
    if item["role"] != "PORTABLE_RELEASE_BUNDLE":
        raise PublicationError("publication transport asset role is invalid")
    return {
        expected_name: {
            "role": "PORTABLE_RELEASE_BUNDLE",
            "sha256": _digest(item["sha256"], f"{expected_name}.sha256"),
            "size": size,
        }
    }


def _asset_size_ceiling(name: str) -> int:
    if name == "installer-materials.tar":
        return _MAX_INSTALLER_MATERIALS_BYTES
    if name in CANONICAL_RELEASE_ASSETS:
        return _MAX_METADATA_ASSET_BYTES
    return _MAX_PORTABLE_ARCHIVE_BYTES


def _stream_file_identity(path: Path, *, maximum: int) -> dict[str, Any]:
    try:
        before = path.lstat()
    except OSError as error:
        raise PublicationError("draft asset readback is unavailable") from error
    if (
        path.is_symlink()
        or not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
        or before.st_size < 0
        or before.st_size > maximum
    ):
        raise PublicationError("draft asset readback file boundary is unsafe")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or opened.st_size != before.st_size
            or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
        ):
            os.close(descriptor)
            raise PublicationError("draft asset readback changed before open")
    except PublicationError:
        raise
    except OSError as error:
        raise PublicationError("draft asset readback is unreadable") from error
    hasher = hashlib.sha256()
    consumed = 0
    try:
        with os.fdopen(descriptor, "rb", closefd=True) as stream:
            while chunk := stream.read(PORTABLE_STREAM_CHUNK_BYTES):
                consumed += len(chunk)
                if consumed > maximum:
                    raise PublicationError("draft asset readback exceeds resource limits")
                hasher.update(chunk)
            after = os.fstat(stream.fileno())
    except OSError as error:
        raise PublicationError("draft asset readback is unreadable") from error
    if consumed != before.st_size or after.st_size != before.st_size:
        raise PublicationError("draft asset readback changed while streaming")
    return {"sha256": "sha256:" + hasher.hexdigest(), "size": consumed}


def _readback_identity(value: bytes | Path, *, name: str) -> dict[str, Any]:
    maximum = _asset_size_ceiling(name)
    if isinstance(value, bytes):
        if len(value) > maximum:
            raise PublicationError("draft asset readback exceeds resource limits")
        return {
            "sha256": "sha256:" + hashlib.sha256(value).hexdigest(),
            "size": len(value),
        }
    if isinstance(value, Path):
        return _stream_file_identity(value, maximum=maximum)
    raise PublicationError(f"draft asset readback is not bytes or a file: {name}")


def declared_publication_assets(plan: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Project a validated plan into the exact GitHub Release asset inventory."""

    canonical = _normalize_assets(plan.get("assets"))
    transport = (
        _normalize_transport_assets(plan.get("transport_assets"), tag=plan.get("tag"))
        if "transport_assets" in plan
        else {}
    )
    return {
        **canonical,
        **{
            name: {"sha256": item["sha256"], "size": item["size"]}
            for name, item in transport.items()
        },
    }


def build_publication_plan(
    *,
    repository: str,
    channel: str,
    tag: str,
    commit: str,
    qualification_identity: str,
    release_notes_identity: str,
    release_notes_markdown_sha256: str,
    assets: Mapping[str, Mapping[str, Any]],
    api_digest: str,
    web_digest: str,
    transport_assets: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build commands and identities without executing any external mutation."""

    if repository != "yanyuhanyue/AniMemo":
        raise PublicationError("publication repository authority is invalid")
    if channel not in {"beta", "rc", "stable"}:
        raise PublicationError("publication channel is invalid")
    if not isinstance(tag, str) or not _TAG.fullmatch(tag):
        raise PublicationError("publication tag is invalid")
    if channel == "stable":
        if "-" in tag:
            raise PublicationError("stable publication tag must not be a prerelease")
    elif f"-{channel}." not in tag:
        raise PublicationError("publication tag and channel differ")
    if not isinstance(commit, str) or not _COMMIT.fullmatch(commit):
        raise PublicationError("publication commit is invalid")
    normalized_assets = _normalize_assets(assets)
    normalized_transport_assets = (
        _normalize_transport_assets(transport_assets, tag=tag)
        if transport_assets is not None
        else {}
    )
    prerelease = channel != "stable"
    title = f"AniMemo {tag}"
    tag_command = ["git", "tag", "--annotate", tag, commit, "--message", title]
    create = [
        "gh",
        "release",
        "create",
        tag,
        "--repo",
        repository,
        "--verify-tag",
        "--draft",
        "--title",
        title,
        "--notes-file",
        "release-output/release-notes.md",
    ]
    publish = ["gh", "release", "edit", tag, "--repo", repository, "--draft=false"]
    if prerelease:
        create.extend(("--prerelease", "--latest=false"))
        publish.extend(("--prerelease", "--latest=false"))
        build_policy = "BUILD_AND_REHEARSE_EXACT_RC_ONCE"
    else:
        create.append("--latest=false")
        publish.extend(("--prerelease=false", "--latest"))
        build_policy = "REUSE_ACCEPTED_RC_EXACT_DIGESTS"
    commands = {
        "create_tag": tag_command,
        "push_tag": ["git", "push", "origin", f"refs/tags/{tag}"],
        "create_draft": create,
        "upload_assets": [
            "gh",
            "release",
            "upload",
            tag,
            *(f"release-output/{name}" for name in CANONICAL_RELEASE_ASSETS),
            *(f"release-output/{name}" for name in normalized_transport_assets),
            "--repo",
            repository,
        ],
        "publish": publish,
    }
    unsigned: dict[str, Any] = {
        "schema": SCHEMA if transport_assets is not None else LEGACY_SCHEMA,
        "repository": repository,
        "channel": channel,
        "tag": tag,
        "commit": commit,
        "qualification_identity": _digest(qualification_identity, "qualification_identity"),
        "release_notes_identity": _digest(release_notes_identity, "release_notes_identity"),
        "release_notes_markdown_sha256": _digest(
            release_notes_markdown_sha256, "release_notes_markdown_sha256"
        ),
        "assets": normalized_assets,
        "api_digest": _digest(api_digest, "api_digest"),
        "web_digest": _digest(web_digest, "web_digest"),
        "build_policy": build_policy,
        "state_order": list(STATES),
        "partial_failure_policy": "PRESERVE_DRAFT_AND_EVIDENCE_FAIL_CLOSED",
        "external_mutation_mode": "PLAN_ONLY",
        "commands": commands,
    }
    if transport_assets is not None:
        unsigned["transport_assets"] = normalized_transport_assets
    return {**unsigned, "identity": _identity(unsigned)}


def validate_publication_plan(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise PublicationError("publication plan is missing")
    schema = value.get("schema")
    if schema not in {LEGACY_SCHEMA, SCHEMA}:
        raise PublicationError("publication plan schema is unsupported")
    required = {
        "schema",
        "identity",
        "repository",
        "channel",
        "tag",
        "commit",
        "qualification_identity",
        "release_notes_identity",
        "release_notes_markdown_sha256",
        "assets",
        "api_digest",
        "web_digest",
        "build_policy",
        "state_order",
        "partial_failure_policy",
        "external_mutation_mode",
        "commands",
    }
    if schema == SCHEMA:
        required.add("transport_assets")
    if set(value) != required:
        raise PublicationError("publication plan has unknown or missing fields")
    rebuilt = build_publication_plan(
        repository=value["repository"],
        channel=value["channel"],
        tag=value["tag"],
        commit=value["commit"],
        qualification_identity=value["qualification_identity"],
        release_notes_identity=value["release_notes_identity"],
        release_notes_markdown_sha256=value["release_notes_markdown_sha256"],
        assets=value["assets"],
        transport_assets=value.get("transport_assets"),
        api_digest=value["api_digest"],
        web_digest=value["web_digest"],
    )
    if dict(value) != rebuilt:
        raise PublicationError("publication plan identity or derived policy mismatch")
    return copy.deepcopy(rebuilt)


def verify_asset_readback(
    plan: Mapping[str, Any],
    *,
    remote_assets: Mapping[str, Mapping[str, Any]],
    downloaded_assets: Mapping[str, bytes | Path],
) -> None:
    validated = validate_publication_plan(plan)
    expected = declared_publication_assets(validated)
    if not isinstance(remote_assets, Mapping) or set(remote_assets) != set(expected):
        raise PublicationError(
            "draft asset metadata inventory is incomplete or has extras"
        )
    remote: dict[str, dict[str, Any]] = {}
    for name in expected:
        item = remote_assets[name]
        if not isinstance(item, Mapping) or set(item) != {"sha256", "size"}:
            raise PublicationError(f"draft asset metadata is not closed: {name}")
        size = item["size"]
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise PublicationError(f"draft asset metadata size is invalid: {name}")
        remote[name] = {
            "sha256": _digest(item["sha256"], f"{name}.sha256"),
            "size": size,
        }
    if remote != expected:
        raise PublicationError("draft asset metadata differs from qualified publication set")
    if not isinstance(downloaded_assets, Mapping) or set(downloaded_assets) != set(expected):
        raise PublicationError("draft asset readback inventory is incomplete or has extras")
    for name in expected:
        content = downloaded_assets[name]
        actual = _readback_identity(content, name=name)
        if actual != expected[name]:
            raise PublicationError(f"draft asset readback identity mismatch: {name}")


class PublicationTransaction:
    """In-memory state gate used by the workflow-facing publication executor."""

    def __init__(self, plan: Mapping[str, Any]):
        self.plan = validate_publication_plan(plan)
        self.state = "NOT_STARTED"
        self.history = [self.state]

    def _require(self, expected: str) -> None:
        if self.state != expected:
            raise PublicationError(f"publication transition requires {expected}, got {self.state}")

    def _move(self, target: str) -> None:
        self.state = target
        self.history.append(target)

    def _fail_partial(self, error: Exception) -> None:
        self._move("FAILED_PARTIAL")
        raise PublicationError(str(error)) from error

    def record_tag_created(self, *, tag: str, target: str) -> None:
        self._require("NOT_STARTED")
        if tag != self.plan["tag"] or target != self.plan["commit"]:
            raise PublicationError("created tag does not bind the publication candidate")
        self._move("TAG_CREATED")

    def record_draft_created(
        self, *, release_id: int, tag: str, target: str, prerelease: bool
    ) -> None:
        self._require("TAG_CREATED")
        expected_prerelease = self.plan["channel"] != "stable"
        if (
            isinstance(release_id, bool)
            or not isinstance(release_id, int)
            or release_id < 1
            or tag != self.plan["tag"]
            or target != self.plan["commit"]
            or prerelease is not expected_prerelease
        ):
            raise PublicationError("created draft metadata differs from publication plan")
        self._move("DRAFT_CREATED")

    def record_assets_uploaded(self, asset_names: list[str]) -> None:
        self._require("DRAFT_CREATED")
        expected = set(declared_publication_assets(self.plan))
        if len(asset_names) != len(set(asset_names)) or set(asset_names) != expected:
            raise PublicationError("uploaded draft asset inventory is incomplete or ambiguous")
        self._move("ASSETS_UPLOADED")

    def record_draft_verified(
        self,
        *,
        remote_assets: Mapping[str, Mapping[str, Any]],
        downloaded_assets: Mapping[str, bytes | Path],
        notes_body_sha256: str,
    ) -> None:
        self._require("ASSETS_UPLOADED")
        try:
            verify_asset_readback(
                self.plan,
                remote_assets=remote_assets,
                downloaded_assets=downloaded_assets,
            )
            if notes_body_sha256 != self.plan["release_notes_markdown_sha256"]:
                raise PublicationError("draft release notes body identity mismatch")
        except (KeyError, PublicationError, TypeError) as error:
            self._fail_partial(error)
        self._move("DRAFT_VERIFIED")

    def record_published(self, *, tag: str, target: str, prerelease: bool) -> None:
        self._require("DRAFT_VERIFIED")
        if (
            tag != self.plan["tag"]
            or target != self.plan["commit"]
            or prerelease is not (self.plan["channel"] != "stable")
        ):
            self._fail_partial(PublicationError("published release identity mismatch"))
        self._move("PUBLISHED")


def verify_post_publish(
    plan: Mapping[str, Any],
    *,
    release: Mapping[str, Any],
    remote_assets: Mapping[str, Mapping[str, Any]],
    downloaded_assets: Mapping[str, bytes | Path],
    api_digest: str,
    web_digest: str,
    attestations_verified: bool,
) -> dict[str, Any]:
    """Verify public metadata, unauthenticated bytes, OCI and attestations."""

    validated = validate_publication_plan(plan)
    expected_release_fields = {
        "tag",
        "target",
        "draft",
        "prerelease",
        "notes_body_sha256",
        "public_unauthenticated_assets",
    }
    if not isinstance(release, Mapping) or set(release) != expected_release_fields:
        raise PublicationError("post-publish release metadata is not closed")
    if (
        release["tag"] != validated["tag"]
        or release["target"] != validated["commit"]
        or release["draft"] is not False
        or release["prerelease"] is not (validated["channel"] != "stable")
        or release["notes_body_sha256"] != validated["release_notes_markdown_sha256"]
        or release["public_unauthenticated_assets"] is not True
    ):
        raise PublicationError("public release metadata does not match publication authority")
    verify_asset_readback(
        validated, remote_assets=remote_assets, downloaded_assets=downloaded_assets
    )
    if api_digest != validated["api_digest"] or web_digest != validated["web_digest"]:
        raise PublicationError("published OCI digests differ from the publication plan")
    if attestations_verified is not True:
        raise PublicationError("published OCI attestations were not verified")
    unsigned = {
        "schema": "animemo.release-post-publish-verification/v1",
        "publication_plan_identity": validated["identity"],
        "tag": validated["tag"],
        "commit": validated["commit"],
        "api_digest": api_digest,
        "web_digest": web_digest,
        "status": "PASS",
    }
    return {**unsigned, "identity": _identity(unsigned)}
