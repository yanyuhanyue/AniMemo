"""Deterministic, qualification-owned release note snapshots and rendering."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from typing import Any

SCHEMA = "animemo.release-notes/v1"
CONFIG_SCHEMA = "animemo.release-notes.configuration/v1"
CANONICAL_RELEASE_ASSETS = (
    "release-manifest.json",
    "deployment-contract.json",
    "installer-materials.tar",
    "checksums.txt",
)

_PRIMARY_LABELS = {
    "release/feature": "feature",
    "release/fix": "fix",
    "release/improvement": "improvement",
    "release/ui": "ui",
    "release/performance": "performance",
    "release/refactor": "refactor",
    "release/deployment": "deployment",
    "release/ci": "ci",
    "release/dependencies": "dependencies",
    "release/security": "security",
    "release/breaking": "breaking",
    "release/docs": "docs",
}
_CATEGORY_ORDER = (
    "feature",
    "fix",
    "improvement",
    "ui",
    "performance",
    "refactor",
    "deployment",
    "ci",
    "dependencies",
    "security",
    "breaking",
    "docs",
    "internal",
    "skip",
)
_CONTEXT_FIELDS = {
    "candidate_sha",
    "comparison_base_sha",
    "previous_stable",
    "release_tag",
    "target_version",
    "channel",
    "minimum_updater_version",
    "supported_os",
    "docker_requirement",
    "release_assets",
}
_PULL_FIELDS = {
    "number",
    "title",
    "source_identity",
    "labels",
    "category",
    "decision",
}
_TOP_FIELDS = {
    "schema",
    "identity",
    "context",
    "configuration",
    "pulls",
    "category_counts",
}
_HEX40 = re.compile(r"[0-9a-f]{40}")
_IDENTITY = re.compile(r"(?:sha256:[0-9a-f]{64}|[0-9a-f]{40})")
_STABLE = re.compile(r"v[0-9]+\.[0-9]+\.[0-9]+")
_TAG = re.compile(
    r"v[0-9]+\.[0-9]+\.[0-9]+(?:-(?:beta|rc)\.(?:[1-9][0-9]*|TEST))?"
)


class ReleaseNotesError(ValueError):
    """A release note snapshot cannot satisfy the closed contract."""


def _canonical_bytes(value: Any) -> bytes:
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


def _identity(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _configuration() -> dict[str, Any]:
    unsigned: dict[str, Any] = {
        "schema": CONFIG_SCHEMA,
        "label_namespace": "release/",
        "primary_labels": dict(sorted(_PRIMARY_LABELS.items())),
        "exclusion_labels": {
            "release/internal": "EXCLUDED_INTERNAL",
            "skip-changelog": "EXCLUDED_SKIP",
        },
        "category_order": list(_CATEGORY_ORDER),
        "renderer": "animemo.release-notes.renderer/v1",
        "unclassified_policy": "FAIL_QUALIFICATION",
        "conflicting_primary_policy": "FAIL_QUALIFICATION",
    }
    return {**unsigned, "identity": _identity(unsigned)}


def configuration() -> dict[str, Any]:
    """Return the immutable release-note classification configuration."""

    return copy.deepcopy(_configuration())


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ReleaseNotesError(f"{field} must be a non-empty canonical string")
    if any(ord(character) < 32 for character in value):
        raise ReleaseNotesError(f"{field} contains a control character")
    return value


def _validate_context(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _CONTEXT_FIELDS:
        raise ReleaseNotesError("release note context has unknown or missing fields")
    result = dict(value)
    for field in ("candidate_sha", "comparison_base_sha"):
        if not isinstance(result[field], str) or not _HEX40.fullmatch(result[field]):
            raise ReleaseNotesError(f"{field} must be a lowercase Git commit SHA")
    previous = result["previous_stable"]
    if not isinstance(previous, str) or (previous and not _STABLE.fullmatch(previous)):
        raise ReleaseNotesError("previous_stable must be empty or a stable tag")
    target = result["target_version"]
    tag = result["release_tag"]
    channel = result["channel"]
    if not isinstance(target, str) or not _STABLE.fullmatch(target):
        raise ReleaseNotesError("target_version must be a stable semantic version")
    if not isinstance(tag, str) or not _TAG.fullmatch(tag):
        raise ReleaseNotesError("release_tag is invalid")
    if channel not in {"beta", "rc", "stable"}:
        raise ReleaseNotesError("channel is invalid")
    if channel == "stable":
        if tag != target:
            raise ReleaseNotesError("stable release_tag must equal target_version")
    elif not tag.startswith(target + f"-{channel}."):
        raise ReleaseNotesError("prerelease tag does not match target and channel")
    _text(result["minimum_updater_version"], "minimum_updater_version")
    _text(result["docker_requirement"], "docker_requirement")
    operating_systems = result["supported_os"]
    if not isinstance(operating_systems, list) or not operating_systems:
        raise ReleaseNotesError("supported_os must be a non-empty list")
    if operating_systems != sorted(set(operating_systems)):
        raise ReleaseNotesError("supported_os must be unique and sorted")
    for item in operating_systems:
        _text(item, "supported_os item")
    assets = result["release_assets"]
    if assets != list(CANONICAL_RELEASE_ASSETS):
        raise ReleaseNotesError("release_assets must be the frozen canonical asset set")
    return copy.deepcopy(result)


def _classify(labels: list[str]) -> tuple[str, str]:
    if len(labels) != len(set(labels)) or labels != sorted(labels):
        raise ReleaseNotesError("PR labels must be unique and sorted")
    if "skip-changelog" in labels:
        return "skip", "EXCLUDED_SKIP"
    primaries = [label for label in labels if label in _PRIMARY_LABELS]
    internal = "release/internal" in labels
    if internal:
        if primaries:
            raise ReleaseNotesError("release/internal conflicts with a primary category")
        return "internal", "EXCLUDED_INTERNAL"
    if not primaries:
        raise ReleaseNotesError("user-visible PR is unclassified")
    if len(primaries) != 1:
        raise ReleaseNotesError("PR has conflicting primary release categories")
    return _PRIMARY_LABELS[primaries[0]], "INCLUDED"


def _normalize_pull(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ReleaseNotesError("PR metadata must be an object")
    required = {"number", "title", "source_identity", "labels"}
    if set(value) != required:
        raise ReleaseNotesError("PR metadata has unknown or missing fields")
    number = value["number"]
    if isinstance(number, bool) or not isinstance(number, int) or number < 1:
        raise ReleaseNotesError("PR number must be a positive integer")
    title = _text(value["title"], "PR title")
    source = value["source_identity"]
    if not isinstance(source, str) or not _IDENTITY.fullmatch(source):
        raise ReleaseNotesError("PR source_identity is invalid")
    labels_value = value["labels"]
    if not isinstance(labels_value, list) or not all(
        isinstance(label, str) and label and label == label.strip()
        for label in labels_value
    ):
        raise ReleaseNotesError("PR labels must be canonical strings")
    labels = sorted(labels_value)
    category, decision = _classify(labels)
    return {
        "number": number,
        "title": title,
        "source_identity": source,
        "labels": labels,
        "category": category,
        "decision": decision,
    }


def build_release_notes(
    *, context: Mapping[str, Any], pulls: Iterable[Mapping[str, Any]]
) -> dict[str, Any]:
    """Build the canonical qualification snapshot from closed PR metadata."""

    normalized_context = _validate_context(context)
    normalized_pulls = [_normalize_pull(value) for value in pulls]
    normalized_pulls.sort(key=lambda value: value["number"])
    numbers = [value["number"] for value in normalized_pulls]
    if len(numbers) != len(set(numbers)):
        raise ReleaseNotesError("duplicate PR number in release note population")
    counts = {
        category: sum(
            1
            for value in normalized_pulls
            if value["decision"] == "INCLUDED" and value["category"] == category
        )
        for category in _CATEGORY_ORDER
    }
    unsigned: dict[str, Any] = {
        "schema": SCHEMA,
        "context": normalized_context,
        "configuration": _configuration(),
        "pulls": normalized_pulls,
        "category_counts": counts,
    }
    artifact = {**unsigned, "identity": _identity(unsigned)}
    validate_release_notes(artifact)
    return artifact


def validate_release_notes(value: Any) -> dict[str, Any]:
    """Validate a frozen snapshot including its complete identity."""

    if not isinstance(value, Mapping) or set(value) != _TOP_FIELDS:
        raise ReleaseNotesError("release note snapshot has unknown or missing fields")
    if value.get("schema") != SCHEMA:
        raise ReleaseNotesError("release note snapshot schema is invalid")
    if value.get("configuration") != _configuration():
        raise ReleaseNotesError("release note configuration identity is invalid")
    _validate_context(value.get("context"))
    pulls = value.get("pulls")
    if not isinstance(pulls, list):
        raise ReleaseNotesError("release note pulls must be a list")
    normalized = []
    for item in pulls:
        if not isinstance(item, Mapping) or set(item) != _PULL_FIELDS:
            raise ReleaseNotesError("release note PR projection is not closed")
        rebuilt = _normalize_pull(
            {field: item[field] for field in ("number", "title", "source_identity", "labels")}
        )
        if dict(item) != rebuilt:
            raise ReleaseNotesError("release note PR classification is inconsistent")
        normalized.append(rebuilt)
    if normalized != sorted(normalized, key=lambda item: item["number"]):
        raise ReleaseNotesError("release note PR population is not sorted")
    numbers = [item["number"] for item in normalized]
    if len(numbers) != len(set(numbers)):
        raise ReleaseNotesError("release note PR population contains duplicates")
    counts = value.get("category_counts")
    expected_counts = {
        category: sum(
            1
            for item in normalized
            if item["decision"] == "INCLUDED" and item["category"] == category
        )
        for category in _CATEGORY_ORDER
    }
    if counts != expected_counts:
        raise ReleaseNotesError("release note category counts are inconsistent")
    unsigned = copy.deepcopy(dict(value))
    identity = unsigned.pop("identity", None)
    if identity != _identity(unsigned):
        raise ReleaseNotesError("release note snapshot identity mismatch")
    return copy.deepcopy(dict(value))


def promote_release_notes(
    value: Mapping[str, Any], *, stable_tag: str
) -> dict[str, Any]:
    """Derive Stable notes from the exact accepted RC PR metadata population."""

    rc = validate_release_notes(value)
    if rc["context"]["channel"] != "rc":
        raise ReleaseNotesError("only an RC release note snapshot can be promoted")
    if not isinstance(stable_tag, str) or not _STABLE.fullmatch(stable_tag):
        raise ReleaseNotesError("stable release note tag is invalid")
    if stable_tag != rc["context"]["target_version"]:
        raise ReleaseNotesError("stable release note tag differs from RC target version")
    stable_context = copy.deepcopy(rc["context"])
    stable_context["release_tag"] = stable_tag
    stable_context["channel"] = "stable"
    pulls = [
        {
            field: item[field]
            for field in ("number", "title", "source_identity", "labels")
        }
        for item in rc["pulls"]
    ]
    return build_release_notes(context=stable_context, pulls=pulls)


def _escape_markdown(value: str) -> str:
    return re.sub(r"([\\`*_\[\]<>#&])", r"\\\1", value)


def _items(snapshot: Mapping[str, Any], categories: set[str]) -> list[str]:
    return [
        f"- {_escape_markdown(item['title'])} (#{item['number']})"
        for item in snapshot["pulls"]
        if item["decision"] == "INCLUDED" and item["category"] in categories
    ]


def _section(title: str, items: list[str]) -> list[str]:
    return [title, "", *(items or ["- 无"]), ""]


def render_release_notes(value: Mapping[str, Any]) -> str:
    """Render a frozen snapshot without GitHub or language-model input."""

    snapshot = validate_release_notes(value)
    context = snapshot["context"]
    channel_line = {
        "beta": "Beta 预览版本；用于验证，不代表 Stable 发布。",
        "rc": "RC 候选版本；发布后仍须通过 Fresh Base live acceptance。",
        "stable": "Stable 版本；由已验收 RC 的同一提交与 OCI 摘要提升。",
    }[context["channel"]]
    lines = [f"# AniMemo {context['release_tag']}", "", f"> {channel_line}", ""]
    lines.extend(_section("## ✨ 新增功能 (Features)", _items(snapshot, {"feature"})))
    lines.extend(_section("## 🐛 Bug 修复 (Bug Fixes)", _items(snapshot, {"fix"})))
    lines.extend(
        _section(
            "## 💡 功能与体验优化 (Improvements)",
            _items(snapshot, {"improvement", "ui"}),
        )
    )
    lines.extend(
        _section(
            "## 🚀 性能与工程改进 (Performance & Engineering)",
            _items(snapshot, {"performance", "refactor", "deployment", "ci", "dependencies"}),
        )
    )
    lines.extend(_section("## 📝 文档 (Documentation)", _items(snapshot, {"docs"})))
    if context["previous_stable"]:
        upgrade = [
            f"- 支持从 {context['previous_stable']} 升级到 {context['target_version']}。",
            f"- 最低 Updater 版本：{context['minimum_updater_version']}。",
            "- 升级前应完成备份，并按 Doctor 与安全切换结果决定提交或回滚。",
        ]
    else:
        upgrade = [
            "- 这是首个 Stable 发行基线，没有可声明的历史 Stable 升级起点。",
            f"- 最低 Updater 版本：{context['minimum_updater_version']}。",
        ]
    lines.extend(_section("## 🔄 升级 (Upgrade)", upgrade))
    lines.extend(
        _section(
            "## 📦 安装 (Installation)",
            [
                "- `install.animemo.cc` 仅提供引导与安装体验，GitHub Release 是唯一发行权威。",
                "- 可显式选择 GitHub 或 Official Mirror 运输；不会静默跨源回退。",
                "- Portable/local-bundle 仅在离线信任引导获得独立授权后启用。",
            ],
        )
    )
    security_items = _items(snapshot, {"security"})
    security_items.append(
        "- 本说明不声明新的 Heavy Security 认证；首次公开 Pre-RC 前仍需完成 Security Delta。"
    )
    lines.extend(_section("## 🛡️ 安全 (Security)", security_items))
    lines.extend(_section("## ⚠️ Breaking Changes", _items(snapshot, {"breaking"})))
    lines.extend(
        _section(
            "## 📋 部署环境",
            [
                *(f"- 支持：{item}" for item in context["supported_os"]),
                f"- 容器运行要求：{context['docker_requirement']}。",
            ],
        )
    )
    lines.extend(
        _section(
            "## 📦 Release Assets",
            [f"- `{name}`" for name in context["release_assets"]],
        )
    )
    return "\n".join(lines).rstrip() + "\n"
