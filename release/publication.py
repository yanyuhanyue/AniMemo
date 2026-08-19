"""Plan and verify the fail-closed GitHub Draft Release transaction."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from collections.abc import Mapping
from typing import Any

from .notes import CANONICAL_RELEASE_ASSETS


SCHEMA = "animemo.release-publication-plan/v1"
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
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise PublicationError(f"publication asset size is invalid: {name}")
        normalized[name] = {
            "sha256": _digest(item["sha256"], f"{name}.sha256"),
            "size": size,
        }
    return normalized


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
            "--repo",
            repository,
        ],
        "publish": publish,
    }
    unsigned: dict[str, Any] = {
        "schema": SCHEMA,
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
    return {**unsigned, "identity": _identity(unsigned)}


def validate_publication_plan(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise PublicationError("publication plan is missing")
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
    if set(value) != required or value.get("schema") != SCHEMA:
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
    downloaded_assets: Mapping[str, bytes],
) -> None:
    validated = validate_publication_plan(plan)
    remote = _normalize_assets(remote_assets)
    if remote != validated["assets"]:
        raise PublicationError("draft asset metadata differs from qualified publication set")
    if not isinstance(downloaded_assets, Mapping) or set(downloaded_assets) != set(
        CANONICAL_RELEASE_ASSETS
    ):
        raise PublicationError("draft asset readback inventory is incomplete or has extras")
    for name in CANONICAL_RELEASE_ASSETS:
        content = downloaded_assets[name]
        if not isinstance(content, bytes):
            raise PublicationError(f"draft asset readback is not bytes: {name}")
        actual = {
            "sha256": "sha256:" + hashlib.sha256(content).hexdigest(),
            "size": len(content),
        }
        if actual != validated["assets"][name]:
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
        if len(asset_names) != len(set(asset_names)) or set(asset_names) != set(
            CANONICAL_RELEASE_ASSETS
        ):
            raise PublicationError("uploaded draft asset inventory is incomplete or ambiguous")
        self._move("ASSETS_UPLOADED")

    def record_draft_verified(
        self,
        *,
        remote_assets: Mapping[str, Mapping[str, Any]],
        downloaded_assets: Mapping[str, bytes],
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
        except Exception as error:
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
    downloaded_assets: Mapping[str, bytes],
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
