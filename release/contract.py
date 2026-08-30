from __future__ import annotations

import copy
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker
from packaging.version import InvalidVersion, Version

from .dependency_images import AUTHORITY as DEPENDENCY_IMAGE_AUTHORITY
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
POSTGRES_REPOSITORY = DEPENDENCY_IMAGE_AUTHORITY.postgres.repository
POSTGRES_DIGEST = DEPENDENCY_IMAGE_AUTHORITY.postgres.digest
REDIS_REPOSITORY = DEPENDENCY_IMAGE_AUTHORITY.redis.repository
REDIS_DIGEST = DEPENDENCY_IMAGE_AUTHORITY.redis.digest
DEPLOYMENT_PROFILE = "v1.1-instance-scoped"
INSTALLER_MATERIALS_NAME = "installer-materials.tar"
DEPLOYMENT_CONTRACT_PATHS = (
    "deploy/docker-compose.yml",
    "updater/docker-compose.runtime.yml",
)
PRODUCTION_BACKUP_CONTRACT = {
    "schemaVersion": 1,
    "quiescenceMethod": "compose-graceful-writer-stop/v1",
    "writerServices": ["api"],
    "allowedRunningServices": ["postgres", "redis", "web"],
    "privateRegistry": "backup-members.json",
    "updaterStateMembers": [
        "ownership.json",
        "releases/release-slots.json",
        "runtime.json",
    ],
}
SCHEMA_PATH = Path(__file__).with_name("release-manifest.schema.json")
STABLE_TAG = re.compile(
    r"^v(?P<base>(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*))$"
)
PRERELEASE_TAG = re.compile(
    r"^v(?P<base>(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*))-(?P<channel>beta|rc)\.(?P<sequence>[1-9][0-9]*)$"
)
PUBLICATION_RESERVATION_SCHEMA_VERSION = 1
PUBLICATION_RESERVATION_STATUSES = frozenset(
    {
        "ABORTED_PARTIAL_GHCR_TRANSACTION",
        "ABORTED_PARTIAL_DRAFT_RELEASE_TRANSACTION",
        "ABORTED_PARTIAL_RELEASE_TRANSACTION",
    }
)
_PUBLICATION_RESERVATION_FIELDS = {
    "releaseTag",
    "status",
    "reusable",
    "candidateSha",
    "candidateTreeSha",
    "qualificationRunId",
    "publishRunId",
    "api",
    "web",
    "gitTagCreated",
    "githubReleaseCreated",
    "releaseAssetCount",
}
_PUBLICATION_RESERVATION_IMAGE_FIELDS = {
    "repository",
    "digest",
    "tags",
    "attestationVerified",
}


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


def _validate_publication_reservation_image(
    payload: object,
    *,
    role: str,
    release_tag: str,
    candidate_sha: str,
) -> None:
    if not isinstance(payload, dict) or set(payload) != (
        _PUBLICATION_RESERVATION_IMAGE_FIELDS
    ):
        raise ReleaseContractError(
            f"Publication reservation {role} image has an invalid shape"
        )
    expected_repository = API_REPOSITORY if role == "api" else WEB_REPOSITORY
    if payload["repository"] != expected_repository:
        raise ReleaseContractError(
            f"Publication reservation {role} repository is invalid"
        )
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", str(payload["digest"])):
        raise ReleaseContractError(
            f"Publication reservation {role} digest is invalid"
        )
    expected_tags = {release_tag, f"sha-{candidate_sha}"}
    tags = payload["tags"]
    if (
        not isinstance(tags, list)
        or any(not isinstance(tag, str) for tag in tags)
        or len(tags) != 2
        or set(tags) != expected_tags
    ):
        raise ReleaseContractError(
            f"Publication reservation {role} tags are invalid"
        )
    if payload["attestationVerified"] is not True:
        raise ReleaseContractError(
            f"Publication reservation {role} attestation must be verified"
        )


def validate_publication_reservations(payload: object) -> dict[str, object]:
    if not isinstance(payload, dict) or set(payload) != {
        "schemaVersion",
        "reservations",
    }:
        raise ReleaseContractError("Publication reservations have an invalid shape")
    if (
        not isinstance(payload["schemaVersion"], int)
        or isinstance(payload["schemaVersion"], bool)
        or payload["schemaVersion"] != PUBLICATION_RESERVATION_SCHEMA_VERSION
    ):
        raise ReleaseContractError(
            "Publication reservations have an unsupported schemaVersion"
        )
    reservations = payload["reservations"]
    if not isinstance(reservations, list):
        raise ReleaseContractError("Publication reservations must be a list")

    seen_release_tags: set[str] = set()
    for reservation in reservations:
        if not isinstance(reservation, dict) or set(reservation) != (
            _PUBLICATION_RESERVATION_FIELDS
        ):
            raise ReleaseContractError(
                "Publication reservation has an invalid shape"
            )
        release_tag = reservation["releaseTag"]
        if not isinstance(release_tag, str) or not PRERELEASE_TAG.fullmatch(
            release_tag
        ):
            raise ReleaseContractError(
                "Publication reservation releaseTag must be a beta or rc prerelease"
            )
        if release_tag in seen_release_tags:
            raise ReleaseContractError(
                f"Duplicate publication reservation releaseTag: {release_tag}"
            )
        seen_release_tags.add(release_tag)
        if (
            not isinstance(reservation["status"], str)
            or reservation["status"] not in PUBLICATION_RESERVATION_STATUSES
        ):
            raise ReleaseContractError("Publication reservation status is invalid")
        if reservation["reusable"] is not False:
            raise ReleaseContractError(
                "Publication reservation reusable must be false"
            )
        candidate_sha = reservation["candidateSha"]
        if not isinstance(candidate_sha, str) or not re.fullmatch(
            r"[0-9a-f]{40}", candidate_sha
        ):
            raise ReleaseContractError(
                "Publication reservation candidateSha is invalid"
            )
        candidate_tree_sha = reservation["candidateTreeSha"]
        if not isinstance(candidate_tree_sha, str) or not re.fullmatch(
            r"[0-9a-f]{40}", candidate_tree_sha
        ):
            raise ReleaseContractError(
                "Publication reservation candidateTreeSha is invalid"
            )
        for field in ("qualificationRunId", "publishRunId"):
            value = reservation[field]
            if (
                not isinstance(value, int)
                or isinstance(value, bool)
                or value <= 0
            ):
                raise ReleaseContractError(
                    f"Publication reservation {field} must be a positive integer"
                )
        _validate_publication_reservation_image(
            reservation["api"],
            role="api",
            release_tag=release_tag,
            candidate_sha=candidate_sha,
        )
        _validate_publication_reservation_image(
            reservation["web"],
            role="web",
            release_tag=release_tag,
            candidate_sha=candidate_sha,
        )
        for field in ("gitTagCreated", "githubReleaseCreated"):
            if not isinstance(reservation[field], bool):
                raise ReleaseContractError(
                    f"Publication reservation {field} must be boolean"
                )
        asset_count = reservation["releaseAssetCount"]
        if (
            not isinstance(asset_count, int)
            or isinstance(asset_count, bool)
            or asset_count < 0
        ):
            raise ReleaseContractError(
                "Publication reservation releaseAssetCount must be non-negative"
            )
        if (
            reservation["status"] == "ABORTED_PARTIAL_GHCR_TRANSACTION"
            and (
                reservation["gitTagCreated"]
                or reservation["githubReleaseCreated"]
                or asset_count != 0
            )
        ):
            raise ReleaseContractError(
                "Aborted partial GHCR reservation has inconsistent Release state"
            )
        if (
            reservation["status"] == "ABORTED_PARTIAL_DRAFT_RELEASE_TRANSACTION"
            and (
                not reservation["gitTagCreated"]
                or not reservation["githubReleaseCreated"]
                or asset_count != 0
            )
        ):
            raise ReleaseContractError(
                "Aborted partial Draft Release reservation has inconsistent Release state"
            )
        if (
            reservation["status"] == "ABORTED_PARTIAL_RELEASE_TRANSACTION"
            and (
                not reservation["gitTagCreated"]
                or not reservation["githubReleaseCreated"]
                or asset_count <= 0
            )
        ):
            raise ReleaseContractError(
                "Aborted partial Release reservation has inconsistent Release state"
            )
    return payload


def resolve_prerelease(
    *,
    tags: list[str],
    bump: str,
    channel: str,
    target_version_override: str = "",
    publication_reservations: object | None = None,
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

    occupied_prerelease_tags = set(tags)
    if publication_reservations is not None:
        validated_reservations = validate_publication_reservations(
            publication_reservations
        )
        occupied_prerelease_tags.update(
            reservation["releaseTag"]
            for reservation in validated_reservations["reservations"]
        )

    sequences = [
        int(match.group("sequence"))
        for tag in occupied_prerelease_tags
        if (match := PRERELEASE_TAG.fullmatch(tag))
        and match.group("base") == base
        and match.group("channel") == channel
    ]
    sequence = max(sequences, default=0) + 1
    release_tag = f"v{base}-{channel}.{sequence}"
    assert_tag_absent(release_tag, list(occupied_prerelease_tags))
    return {
        "targetVersion": f"v{base}",
        "releaseTag": release_tag,
        "sequence": sequence,
    }


def _timestamp(value: datetime | str) -> str:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value and value == value.strip():
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as error:
            raise ReleaseContractError("timestamp must be valid RFC3339") from error
    else:
        raise ReleaseContractError("timestamp must be valid RFC3339")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ReleaseContractError("timestamp must include a timezone")
    if parsed.microsecond != 0:
        raise ReleaseContractError("timestamp must use fixed whole-second precision")
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


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
    installer_materials_sha256: str,
    promoted_from: str | None = None,
    provenance_workflow: str | None = None,
    provenance_source_commit: str | None = None,
) -> dict[str, object]:
    materials_digest = _digest_hex(
        installer_materials_sha256,
        label="installer materials",
    )
    if materials_digest == "0" * 64:
        raise ReleaseContractError("Installer materials digest must not be the zero digest")
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
                "repository": DEPENDENCY_IMAGE_AUTHORITY.postgres.repository,
                "digest": DEPENDENCY_IMAGE_AUTHORITY.postgres.digest,
                "platform": DEPENDENCY_IMAGE_AUTHORITY.postgres.platform,
            },
            "redis": {
                "repository": DEPENDENCY_IMAGE_AUTHORITY.redis.repository,
                "digest": DEPENDENCY_IMAGE_AUTHORITY.redis.digest,
                "platform": DEPENDENCY_IMAGE_AUTHORITY.redis.platform,
            },
        },
        "deployment": {
            "profile": DEPLOYMENT_PROFILE,
            "contractSha256": deployment_contract_sha256,
            "files": copy.deepcopy(deployment_files),
            "backup": copy.deepcopy(PRODUCTION_BACKUP_CONTRACT),
            "installerMaterials": {
                "name": INSTALLER_MATERIALS_NAME,
                "sha256": f"sha256:{materials_digest}",
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

    for role in DEPENDENCY_IMAGE_AUTHORITY.roles:
        image = DEPENDENCY_IMAGE_AUTHORITY.image(role)
        expected = {
            "repository": image.repository,
            "digest": image.digest,
            "platform": image.platform,
        }
        if payload["images"][role] != expected:
            raise ReleaseContractError(
                f"Manifest {role} differs from the canonical dependency image authority"
            )

    release = payload["release"]
    if release["createdAt"] != _timestamp(release["createdAt"]):
        raise ReleaseContractError("Release createdAt must be canonical UTC Z")
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
        if (
            provenance["workflow"] != ".github/workflows/promote-release.yml"
            or provenance["sourceCommit"] != release["commit"]
        ):
            raise ReleaseContractError(
                "Stable provenance must use the promotion workflow and frozen RC commit"
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
    if "backup" in deployment and deployment["backup"] != PRODUCTION_BACKUP_CONTRACT:
        raise ReleaseContractError("Production Backup deployment contract is invalid")

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
    provenance_source_commit: str | None = None,
    created_at: datetime | str | None = None,
) -> dict[str, object]:
    validate_manifest(rc_manifest)
    rc_release = rc_manifest["release"]
    match = PRERELEASE_TAG.fullmatch(rc_release["version"])
    if rc_release["channel"] != "rc" or not match:
        raise ReleaseContractError("Only a valid RC manifest can be promoted")
    stable_tag = f"v{match.group('base')}"
    assert_tag_absent(stable_tag, existing_tags)
    frozen_created_at = _timestamp(rc_release["createdAt"])
    if created_at is not None and _timestamp(created_at) != frozen_created_at:
        raise ReleaseContractError(
            "Stable createdAt must derive exactly from the frozen RC manifest"
        )
    if (
        provenance_source_commit is not None
        and provenance_source_commit != rc_release["commit"]
    ):
        raise ReleaseContractError(
            "Promotion workflow commit is execution metadata, not Stable Authority"
        )

    promoted = copy.deepcopy(rc_manifest)
    promoted["release"].update(
        {
            "version": stable_tag,
            "channel": "stable",
            "createdAt": frozen_created_at,
            "promotedFrom": rc_release["version"],
        }
    )
    promoted["releaseNotes"]["tag"] = stable_tag
    promoted["provenance"]["workflow"] = ".github/workflows/promote-release.yml"
    promoted["provenance"]["sourceCommit"] = rc_release["commit"]
    validate_manifest(promoted)
    return promoted
