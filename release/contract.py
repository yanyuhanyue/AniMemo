from __future__ import annotations

import copy
import json
import re
from datetime import datetime
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker
from packaging.version import InvalidVersion, Version


REPOSITORY = "yanyuhanyue/AniMemo"
API_REPOSITORY = "ghcr.io/yanyuhanyue/animemo-api"
WEB_REPOSITORY = "ghcr.io/yanyuhanyue/animemo-web"
SCHEMA_PATH = Path(__file__).with_name("release-manifest.schema.json")
STABLE_TAG = re.compile(r"^v(?P<base>(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*))$")
PRERELEASE_TAG = re.compile(
    r"^v(?P<base>(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*))-(?P<channel>beta|rc)\.(?P<sequence>[1-9][0-9]*)$"
)


class ReleaseContractError(ValueError):
    pass


def _version(value: str, *, label: str) -> Version:
    try:
        parsed = Version(value.removeprefix("v"))
    except InvalidVersion as error:
        raise ReleaseContractError(f"Invalid {label}: {value!r}") from error
    return parsed


def _stable_tags(tags: list[str]) -> list[tuple[Version, str]]:
    result = []
    for tag in tags:
        match = STABLE_TAG.fullmatch(tag)
        if match:
            result.append((_version(match.group("base"), label="stable tag"), tag))
        elif tag.startswith("v") and re.match(r"^v[0-9]", tag) and not PRERELEASE_TAG.fullmatch(tag):
            raise ReleaseContractError(f"Invalid AniMemo release tag: {tag!r}")
    return sorted(result)


def assert_tag_absent(tag: str, tags: list[str]) -> None:
    if tag in set(tags):
        raise ReleaseContractError(f"Release tag already exists: {tag}")


def resolve_prerelease(
    *,
    tags: list[str],
    bump: str,
    channel: str,
    target_version_override: str = "",
) -> dict[str, object]:
    if bump not in {"patch", "minor", "major"}:
        raise ReleaseContractError(f"Invalid version bump: {bump!r}")
    if channel not in {"beta", "rc"}:
        raise ReleaseContractError(f"Invalid prerelease channel: {channel!r}")

    stable = _stable_tags(tags)
    if stable:
        if target_version_override:
            raise ReleaseContractError("target-version-override is bootstrap-only after a stable release exists")
        latest = stable[-1][0]
        major, minor, patch = latest.release
        if bump == "patch":
            patch += 1
        elif bump == "minor":
            minor, patch = minor + 1, 0
        else:
            major, minor, patch = major + 1, 0, 0
        base = f"{major}.{minor}.{patch}"
    else:
        if not target_version_override:
            raise ReleaseContractError("Initial release line requires --target-version-override (for example v1.0.0)")
        match = STABLE_TAG.fullmatch(target_version_override)
        if not match:
            raise ReleaseContractError("Bootstrap target-version-override must be a stable vMAJOR.MINOR.PATCH tag")
        base = match.group("base")

    sequences = [
        int(match.group("sequence"))
        for tag in tags
        if (match := PRERELEASE_TAG.fullmatch(tag))
        and match.group("base") == base
        and match.group("channel") == channel
    ]
    sequence = max(sequences, default=0) + 1
    release_tag = f"v{base}-{channel}.{sequence}"
    assert_tag_absent(release_tag, tags)
    return {"targetVersion": f"v{base}", "releaseTag": release_tag, "sequence": sequence}


def _timestamp(value: datetime | str) -> str:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ReleaseContractError("created_at must include a timezone")
        return value.isoformat().replace("+00:00", "Z")
    return value


def build_manifest(
    *,
    version: str,
    channel: str,
    commit: str,
    created_at: datetime | str,
    api_digest: str,
    web_digest: str,
    minimum_updater_version: str,
    database_contract: str,
    database_accepts: list[str],
    migration_required: bool,
    migration_policy: str,
    application_rollback: str,
    configuration_contract: str,
    configuration_accepts: list[str],
    plugin_sdk_apis: list[int],
    promoted_from: str | None = None,
    provenance_workflow: str = ".github/workflows/release.yml",
) -> dict[str, object]:
    payload = {
        "schemaVersion": 1,
        "release": {
            "version": version,
            "channel": channel,
            "commit": commit,
            "createdAt": _timestamp(created_at),
            "promotedFrom": promoted_from,
        },
        "images": {
            "api": {"repository": API_REPOSITORY, "digest": api_digest, "platform": "linux/amd64"},
            "web": {"repository": WEB_REPOSITORY, "digest": web_digest, "platform": "linux/amd64"},
        },
        "minimumUpdaterVersion": minimum_updater_version,
        "compatibility": {
            "database": {
                "contract": database_contract,
                "appAccepts": database_accepts,
                "migration": {"required": migration_required, "policy": migration_policy},
                "applicationRollback": application_rollback,
            },
            "configuration": {"contract": configuration_contract, "appAccepts": configuration_accepts},
            "pluginSdk": {
                "manifestSchema": 2,
                "supportedApis": plugin_sdk_apis,
                "runtime": "trusted-in-process",
            },
        },
        "releaseNotes": {"repository": REPOSITORY, "tag": version},
        "provenance": {
            "repository": REPOSITORY,
            "issuer": "github-actions",
            "predicateType": "https://slsa.dev/provenance/v1",
            "workflow": provenance_workflow,
        },
        "artifacts": {"manifest": "release-manifest.json", "checksums": "checksums.txt"},
    }
    validate_manifest(payload)
    return payload


def _schema() -> dict[str, object]:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def validate_manifest(payload: dict[str, object], *, updater_version: str | None = None) -> None:
    validator = Draft202012Validator(_schema(), format_checker=FormatChecker())
    errors = sorted(validator.iter_errors(payload), key=lambda item: list(item.absolute_path))
    if errors:
        error = errors[0]
        location = ".".join(str(part) for part in error.absolute_path) or "manifest"
        raise ReleaseContractError(f"Invalid release manifest at {location}: {error.message}")

    release = payload["release"]
    version = release["version"]
    channel = release["channel"]
    prerelease = PRERELEASE_TAG.fullmatch(version)
    stable = STABLE_TAG.fullmatch(version)
    if channel == "stable":
        if not stable or not release["promotedFrom"]:
            raise ReleaseContractError("Stable manifests must identify an RC promotion source")
        promoted = PRERELEASE_TAG.fullmatch(release["promotedFrom"])
        if not promoted or promoted.group("channel") != "rc" or promoted.group("base") != stable.group("base"):
            raise ReleaseContractError("Stable promotedFrom must be an RC for the exact stable version")
    elif not prerelease or prerelease.group("channel") != channel or release["promotedFrom"] is not None:
        raise ReleaseContractError("Prerelease version, channel, and promotedFrom are inconsistent")

    database = payload["compatibility"]["database"]
    migration = database["migration"]
    if database["contract"] not in database["appAccepts"]:
        raise ReleaseContractError("Target application must accept its declared database contract")
    if migration["required"] != (migration["policy"] != "none"):
        raise ReleaseContractError("Database migration required flag and migration policy are inconsistent")
    if migration["policy"] == "none" and database["applicationRollback"] != "safe":
        raise ReleaseContractError("No-migration releases must permit safe application rollback")
    if migration["policy"] == "breaking-blocked" and database["applicationRollback"] != "blocked":
        raise ReleaseContractError("Breaking-blocked migrations must block application rollback")

    configuration = payload["compatibility"]["configuration"]
    if configuration["contract"] not in configuration["appAccepts"]:
        raise ReleaseContractError("Target application must accept its declared configuration contract")

    if updater_version is not None and _version(updater_version, label="updater version") < _version(
        payload["minimumUpdaterVersion"], label="minimum updater version"
    ):
        raise ReleaseContractError(
            f"Installed updater {updater_version} is older than required updater {payload['minimumUpdaterVersion']}"
        )


def promote_manifest(rc_manifest: dict[str, object], *, existing_tags: list[str], created_at: datetime | str | None = None) -> dict[str, object]:
    validate_manifest(rc_manifest)
    rc_release = rc_manifest["release"]
    match = PRERELEASE_TAG.fullmatch(rc_release["version"])
    if rc_release["channel"] != "rc" or not match:
        raise ReleaseContractError("Only a valid RC manifest can be promoted")
    stable_tag = f"v{match.group('base')}"
    assert_tag_absent(stable_tag, existing_tags)

    promoted = copy.deepcopy(rc_manifest)
    promoted["release"].update(
        {
            "version": stable_tag,
            "channel": "stable",
            "createdAt": _timestamp(created_at or rc_release["createdAt"]),
            "promotedFrom": rc_release["version"],
        }
    )
    promoted["releaseNotes"]["tag"] = stable_tag
    promoted["provenance"]["workflow"] = ".github/workflows/promote-release.yml"
    validate_manifest(promoted)
    return promoted
