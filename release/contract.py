from __future__ import annotations

import copy
import hashlib
import json
import re
from datetime import datetime
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker
from packaging.version import InvalidVersion, Version

from .materials import (
    INSTALLER_MATERIALS_NAME as MATERIAL_ARCHIVE_NAME,
)
from .materials import (
    MaterialContractError,
    inspect_installer_materials,
    validate_material_contract,
)

REPOSITORY = "yanyuhanyue/AniMemo"
API_REPOSITORY = "ghcr.io/yanyuhanyue/animemo-api"
WEB_REPOSITORY = "ghcr.io/yanyuhanyue/animemo-web"
POSTGRES_REPOSITORY = "docker.io/library/postgres"
REDIS_REPOSITORY = "docker.io/library/redis"
POSTGRES_DIGEST = (
    "sha256:075f7ba66bc9b3ce7d6b8b635208ff61cd7cf1a67d71ec530eec5d7ae0cbe571"
)
REDIS_DIGEST = "sha256:9702d01c1f10c3ea9f48211b4362e44f154ff02d063e6f7268eba804059f53bf"
DEPLOYMENT_PROFILE = "v1.1-standard"
INSTALLER_MATERIALS_NAME = "installer-materials.tar"
_DEFAULT_MATERIALS_DIGEST = "sha256:" + "0" * 64
DEPLOYMENT_CONTRACT_PATHS = (
    "deploy/docker-compose.yml",
    "updater/docker-compose.runtime.yml",
)
SCHEMA_PATH = Path(__file__).with_name("release-manifest.schema.json")
STABLE_TAG = re.compile(
    r"^v(?P<base>(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*))$"
)
PRERELEASE_TAG = re.compile(
    r"^v(?P<base>(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*))-(?P<channel>beta|rc)\.(?P<sequence>[1-9][0-9]*)$"
)


class ReleaseContractError(ValueError):
    pass


def _canonical_json(payload: dict[str, object]) -> bytes:
    return (
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def validate_deployment_contract(
    payload: object,
    *,
    root: Path | None = None,
    installer_materials: Path | None = None,
) -> dict[str, object]:
    if not isinstance(payload, dict) or set(payload) != {
        "schemaVersion",
        "profile",
        "platform",
        "archive",
        "files",
        "materials",
    }:
        raise ReleaseContractError("Deployment contract has an invalid shape")
    if (
        payload["schemaVersion"] != 2
        or payload["profile"] != DEPLOYMENT_PROFILE
        or payload["platform"] != "linux/amd64"
        or not isinstance(payload["files"], list)
    ):
        raise ReleaseContractError("Deployment contract has an unsupported schema")
    files = payload["files"]
    if [item.get("path") for item in files if isinstance(item, dict)] != list(
        DEPLOYMENT_CONTRACT_PATHS
    ):
        raise ReleaseContractError(
            "Deployment contract files are incomplete or unordered"
        )
    for item in files:
        if (
            not isinstance(item, dict)
            or set(item) != {"path", "sha256"}
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", str(item["sha256"]))
        ):
            raise ReleaseContractError("Deployment contract file identity is invalid")
        if root is not None:
            source = root / item["path"]
            if source.is_symlink() or not source.is_file():
                raise ReleaseContractError(
                    f"Deployment contract source is unavailable: {item['path']}"
                )
            if _file_sha256(source) != item["sha256"]:
                raise ReleaseContractError(
                    f"Deployment contract source checksum differs: {item['path']}"
                )
    material_contract = {
        "schemaVersion": payload["schemaVersion"],
        "profile": payload["profile"],
        "platform": payload["platform"],
        "archive": payload["archive"],
        "materials": payload["materials"],
    }
    try:
        validate_material_contract(material_contract)
        if installer_materials is not None:
            identity = inspect_installer_materials(installer_materials)
            if payload["archive"] != {
                "name": MATERIAL_ARCHIVE_NAME,
                "sha256": identity.sha256,
                "size": identity.size,
                "format": "tar",
            } or payload["materials"] != [item.as_dict() for item in identity.files]:
                raise ReleaseContractError(
                    "Deployment contract installer materials identity differs"
                )
    except MaterialContractError as error:
        raise ReleaseContractError(str(error)) from error
    return payload


def build_deployment_contract(
    root: Path,
    *,
    installer_materials: Path,
) -> dict[str, object]:
    root = root.resolve()
    files: list[dict[str, str]] = []
    for relative in DEPLOYMENT_CONTRACT_PATHS:
        source = root / relative
        if source.is_symlink() or not source.is_file():
            raise ReleaseContractError(
                f"Deployment contract source is unavailable: {relative}"
            )
        files.append({"path": relative, "sha256": _file_sha256(source)})
    try:
        identity = inspect_installer_materials(installer_materials)
    except MaterialContractError as error:
        raise ReleaseContractError(str(error)) from error
    payload = {
        "schemaVersion": 2,
        "profile": DEPLOYMENT_PROFILE,
        "platform": "linux/amd64",
        "archive": {
            "name": MATERIAL_ARCHIVE_NAME,
            "sha256": identity.sha256,
            "size": identity.size,
            "format": "tar",
        },
        "files": files,
        "materials": [item.as_dict() for item in identity.files],
    }
    return validate_deployment_contract(
        payload, root=root, installer_materials=installer_materials
    )


def deployment_contract_digest(payload: dict[str, object]) -> str:
    validate_deployment_contract(payload)
    return "sha256:" + hashlib.sha256(_canonical_json(payload)).hexdigest()


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
        elif (
            tag.startswith("v")
            and re.match(r"^v[0-9]", tag)
            and not PRERELEASE_TAG.fullmatch(tag)
        ):
            raise ReleaseContractError(f"Invalid AniMemo release tag: {tag!r}")
    return sorted(result)


def assert_tag_absent(tag: str, tags: list[str]) -> None:
    if tag in set(tags):
        raise ReleaseContractError(f"Release tag already exists: {tag}")


def previous_stable_tag(tags: list[str], *, target: str) -> str | None:
    target_match = STABLE_TAG.fullmatch(target)
    if not target_match:
        raise ReleaseContractError(f"Invalid Stable target: {target!r}")
    target_version = _version(target_match.group("base"), label="Stable target")
    candidates = [
        (version, tag)
        for version, tag in _stable_tags(tags)
        if version < target_version
    ]
    return candidates[-1][1] if candidates else None


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
            raise ReleaseContractError(
                "target-version-override is bootstrap-only after a stable release exists"
            )
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
            raise ReleaseContractError(
                "Initial release line requires --target-version-override (for example v1.0.0)"
            )
        match = STABLE_TAG.fullmatch(target_version_override)
        if not match:
            raise ReleaseContractError(
                "Bootstrap target-version-override must be a stable vMAJOR.MINOR.PATCH tag"
            )
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
    return {
        "targetVersion": f"v{base}",
        "releaseTag": release_tag,
        "sequence": sequence,
    }


def _timestamp(value: datetime | str) -> str:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ReleaseContractError("created_at must include a timezone")
        return value.isoformat().replace("+00:00", "Z")
    return value


def _digest_hex(value: str, *, label: str) -> str:
    match = re.fullmatch(r"sha256:([0-9a-f]{64})", value)
    if not match:
        raise ReleaseContractError(f"Invalid immutable {label} digest: {value!r}")
    return match.group(1)


def build_provenance_plan(
    *,
    version: str,
    commit: str,
    api_digest: str,
    web_digest: str,
    created_at: datetime | str,
) -> dict[str, object]:
    """Build the unsigned SLSA subject plan used by read-only release dry-runs.

    Real publish jobs replace this proof-of-input step with GitHub's OIDC-backed
    ``actions/attest`` signature.  Keeping the dry-run unsigned is what lets the
    job operate with only ``contents: read``.
    """

    if not (STABLE_TAG.fullmatch(version) or PRERELEASE_TAG.fullmatch(version)):
        raise ReleaseContractError(f"Invalid immutable release version: {version!r}")
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ReleaseContractError(f"Invalid release commit: {commit!r}")
    created = _timestamp(created_at)
    try:
        parsed_created = datetime.fromisoformat(created.replace("Z", "+00:00"))
    except ValueError as error:
        raise ReleaseContractError(
            f"Invalid provenance timestamp: {created!r}"
        ) from error
    if parsed_created.tzinfo is None:
        raise ReleaseContractError("Provenance timestamp must include a timezone")

    return {
        "_type": "https://in-toto.io/Statement/v1",
        "subject": [
            {
                "name": API_REPOSITORY,
                "digest": {"sha256": _digest_hex(api_digest, label="API image")},
            },
            {
                "name": WEB_REPOSITORY,
                "digest": {"sha256": _digest_hex(web_digest, label="Web image")},
            },
        ],
        "predicateType": "https://slsa.dev/provenance/v1",
        "predicate": {
            "buildDefinition": {
                "buildType": f"https://github.com/{REPOSITORY}/.github/workflows/release.yml@refs/heads/main",
                "externalParameters": {"releaseVersion": version},
                "internalParameters": {"dryRun": True, "signed": False},
                "resolvedDependencies": [
                    {
                        "uri": f"git+https://github.com/{REPOSITORY}.git@{commit}",
                        "digest": {"gitCommit": commit},
                    }
                ],
            },
            "runDetails": {
                "builder": {"id": "https://github.com/actions/runner"},
                "metadata": {
                    "invocationId": f"dry-run:{commit}:{version}",
                    "startedOn": created,
                    "finishedOn": created,
                },
            },
        },
    }


def build_manifest(
    *,
    version: str,
    channel: str,
    commit: str,
    created_at: datetime | str,
    api_digest: str,
    web_digest: str,
    deployment_contract_sha256: str,
    deployment_files: list[dict[str, str]],
    minimum_updater_version: str,
    database_contract: str,
    database_accepts: list[str],
    migration_required: bool,
    migration_policy: str,
    application_rollback: str,
    configuration_contract: str,
    configuration_accepts: list[str],
    plugin_sdk_apis: list[int],
    installer_materials_sha256: str = _DEFAULT_MATERIALS_DIGEST,
    promoted_from: str | None = None,
    provenance_workflow: str | None = None,
    provenance_source_commit: str | None = None,
) -> dict[str, object]:
    source_commit = provenance_source_commit or commit
    workflow = provenance_workflow or (
        ".github/workflows/promote-release.yml"
        if channel == "stable"
        else ".github/workflows/release.yml"
    )
    payload = {
        "schemaVersion": 2,
        "release": {
            "version": version,
            "channel": channel,
            "commit": commit,
            "createdAt": _timestamp(created_at),
            "promotedFrom": promoted_from,
        },
        "images": {
            "api": {
                "repository": API_REPOSITORY,
                "digest": api_digest,
                "platform": "linux/amd64",
            },
            "web": {
                "repository": WEB_REPOSITORY,
                "digest": web_digest,
                "platform": "linux/amd64",
            },
            "postgres": {
                "repository": POSTGRES_REPOSITORY,
                "digest": POSTGRES_DIGEST,
                "platform": "linux/amd64",
            },
            "redis": {
                "repository": REDIS_REPOSITORY,
                "digest": REDIS_DIGEST,
                "platform": "linux/amd64",
            },
        },
        "deployment": {
            "profile": DEPLOYMENT_PROFILE,
            "contractSha256": deployment_contract_sha256,
            "files": copy.deepcopy(deployment_files),
            "installerMaterials": {
                "name": INSTALLER_MATERIALS_NAME,
                "sha256": installer_materials_sha256,
                "format": "tar",
            },
        },
        "minimumUpdaterVersion": minimum_updater_version,
        "compatibility": {
            "database": {
                "contract": database_contract,
                "appAccepts": database_accepts,
                "migration": {
                    "required": migration_required,
                    "policy": migration_policy,
                },
                "applicationRollback": application_rollback,
            },
            "configuration": {
                "contract": configuration_contract,
                "appAccepts": configuration_accepts,
            },
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
            "workflow": workflow,
            "sourceCommit": source_commit,
        },
        "artifacts": {
            "manifest": "release-manifest.json",
            "deploymentContract": "deployment-contract.json",
            "installerMaterials": INSTALLER_MATERIALS_NAME,
            "checksums": "checksums.txt",
        },
    }
    validate_manifest(payload)
    return payload


def _schema() -> dict[str, object]:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def validate_manifest(
    payload: dict[str, object], *, updater_version: str | None = None
) -> None:
    validator = Draft202012Validator(_schema(), format_checker=FormatChecker())
    errors = sorted(
        validator.iter_errors(payload), key=lambda item: list(item.absolute_path)
    )
    if errors:
        error = errors[0]
        location = ".".join(str(part) for part in error.absolute_path) or "manifest"
        raise ReleaseContractError(
            f"Invalid release manifest at {location}: {error.message}"
        )

    release = payload["release"]
    version = release["version"]
    channel = release["channel"]
    prerelease = PRERELEASE_TAG.fullmatch(version)
    stable = STABLE_TAG.fullmatch(version)
    if channel == "stable":
        if not stable or not release["promotedFrom"]:
            raise ReleaseContractError(
                "Stable manifests must identify an RC promotion source"
            )
        promoted = PRERELEASE_TAG.fullmatch(release["promotedFrom"])
        if (
            not promoted
            or promoted.group("channel") != "rc"
            or promoted.group("base") != stable.group("base")
        ):
            raise ReleaseContractError(
                "Stable promotedFrom must be an RC for the exact stable version"
            )
    elif (
        not prerelease
        or prerelease.group("channel") != channel
        or release["promotedFrom"] is not None
    ):
        raise ReleaseContractError(
            "Prerelease version, channel, and promotedFrom are inconsistent"
        )

    if payload["releaseNotes"]["tag"] != version:
        raise ReleaseContractError(
            "Release notes tag must equal the immutable release version"
        )
    provenance = payload["provenance"]
    if channel == "stable":
        if provenance["workflow"] != ".github/workflows/promote-release.yml":
            raise ReleaseContractError(
                "Stable provenance must use the promotion workflow"
            )
    elif (
        provenance["workflow"] != ".github/workflows/release.yml"
        or provenance["sourceCommit"] != release["commit"]
    ):
        raise ReleaseContractError(
            "Prerelease provenance must bind the producer workflow and release commit"
        )

    deployment = payload["deployment"]
    if [item.get("path") for item in deployment["files"]] != list(
        DEPLOYMENT_CONTRACT_PATHS
    ):
        raise ReleaseContractError(
            "Deployment contract files are incomplete or unordered"
        )

    database = payload["compatibility"]["database"]
    migration = database["migration"]
    if database["contract"] not in database["appAccepts"]:
        raise ReleaseContractError(
            "Target application must accept its declared database contract"
        )
    if migration["required"] != (migration["policy"] != "none"):
        raise ReleaseContractError(
            "Database migration required flag and migration policy are inconsistent"
        )
    if migration["policy"] == "none" and database["applicationRollback"] != "safe":
        raise ReleaseContractError(
            "No-migration releases must permit safe application rollback"
        )
    if (
        migration["policy"] == "breaking-blocked"
        and database["applicationRollback"] != "blocked"
    ):
        raise ReleaseContractError(
            "Breaking-blocked migrations must block application rollback"
        )

    configuration = payload["compatibility"]["configuration"]
    if configuration["contract"] not in configuration["appAccepts"]:
        raise ReleaseContractError(
            "Target application must accept its declared configuration contract"
        )

    if updater_version is not None and _version(
        updater_version, label="updater version"
    ) < _version(payload["minimumUpdaterVersion"], label="minimum updater version"):
        raise ReleaseContractError(
            f"Installed updater {updater_version} is older than required updater {payload['minimumUpdaterVersion']}"
        )


def promote_manifest(
    rc_manifest: dict[str, object],
    *,
    existing_tags: list[str],
    provenance_source_commit: str,
    created_at: datetime | str | None = None,
) -> dict[str, object]:
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
    promoted["provenance"]["sourceCommit"] = provenance_source_commit
    validate_manifest(promoted)
    return promoted
