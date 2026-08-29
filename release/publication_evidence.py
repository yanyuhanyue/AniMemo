from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

from .contract import API_REPOSITORY, REPOSITORY, WEB_REPOSITORY

SCHEMA_PATH = Path(__file__).with_name("github-release-publication.schema.json")
GITHUB_RELEASE_PREDICATE_TYPE = "https://in-toto.io/attestation/release/v0.2"
GITHUB_RELEASE_CERTIFICATE_IDENTITY = "https://dotcom.releases.github.com"
ACTIONS_OIDC_ISSUER = "https://token.actions.githubusercontent.com"
ACTIONS_PREDICATE_TYPE = "https://slsa.dev/provenance/v1"
ACTIONS_SOURCE_REF = "refs/heads/main"
REPOSITORY_ID = "1327429673"
OWNER_ID = "111261350"
_WORKFLOWS = frozenset(
    {
        ".github/workflows/release.yml",
        ".github/workflows/promote-release.yml",
    }
)
_ACTION_SUBJECTS = frozenset(
    {
        API_REPOSITORY,
        WEB_REPOSITORY,
        "release-manifest.json",
        "deployment-contract.json",
        "installer-materials.tar",
    }
)
_SHA256 = re.compile(r"sha256:[0-9a-f]{64}")
_COMMIT = re.compile(r"[0-9a-f]{40}")
_RELEASE_TAG = re.compile(
    r"v[0-9]+\.[0-9]+\.[0-9]+(?:(?:-beta\.[1-9][0-9]*)|(?:-rc\.[1-9][0-9]*))?"
)


class PublicationEvidenceError(ValueError):
    """外部密码学验证后的 claim 不满足冻结的发布权威策略。"""


def _canonical_digest(payload: object) -> str:
    value = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(value).hexdigest()


@dataclass(frozen=True)
class ReleasePublicationAsset:
    name: str
    sha256: str
    size: int


@dataclass(frozen=True)
class ReleaseTransportAsset:
    name: str
    sha256: str
    size: int
    role: str
    authority_role: str


@dataclass(frozen=True)
class GitHubReleasePublication:
    tag: str
    tag_commit: str
    tag_object: str
    draft: bool
    prerelease: bool
    signed_at: str
    assets: tuple[ReleasePublicationAsset, ...]
    transport_assets: tuple[ReleaseTransportAsset, ...]

    @property
    def identity(self) -> str:
        return _canonical_digest(
            {
                "assets": [
                    {"name": item.name, "sha256": item.sha256, "size": item.size}
                    for item in self.assets
                ],
                "transportAssets": [
                    {
                        "authorityRole": item.authority_role,
                        "name": item.name,
                        "role": item.role,
                        "sha256": item.sha256,
                        "size": item.size,
                    }
                    for item in self.transport_assets
                ],
                "certificateIdentity": GITHUB_RELEASE_CERTIFICATE_IDENTITY,
                "draft": self.draft,
                "immutable": True,
                "ownerId": OWNER_ID,
                "predicateType": GITHUB_RELEASE_PREDICATE_TYPE,
                "prerelease": self.prerelease,
                "repository": REPOSITORY,
                "repositoryId": REPOSITORY_ID,
                "signedAt": self.signed_at,
                "tag": self.tag,
                "tagCommit": self.tag_commit,
                "tagObject": self.tag_object,
            }
        )


@dataclass(frozen=True)
class ActionsProvenanceClaim:
    subject_name: str
    subject_digest: str
    repository: str
    workflow: str
    certificate_identity: str
    oidc_issuer: str
    source_commit: str
    source_ref: str
    signer_digest: str
    predicate_type: str

    @property
    def identity(self) -> str:
        return _canonical_digest(self.__dict__)


def _release_schema() -> dict[str, Any]:
    try:
        payload = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PublicationEvidenceError("GitHub Release claim schema 不可用") from error
    if not isinstance(payload, dict):
        raise PublicationEvidenceError("GitHub Release claim schema 无效")
    return payload


def close_github_release_publication(
    payload: Mapping[str, object],
) -> GitHubReleasePublication:
    """关闭已由外部实现验签的 GitHub Release normalized claim。"""

    if not isinstance(payload, Mapping):
        raise PublicationEvidenceError("GitHub Release claim 必须是对象")
    errors = sorted(
        Draft202012Validator(
            _release_schema(), format_checker=FormatChecker()
        ).iter_errors(dict(payload)),
        key=lambda item: list(item.absolute_path),
    )
    if errors:
        location = ".".join(str(item) for item in errors[0].absolute_path)
        raise PublicationEvidenceError(
            f"GitHub Release claim 不满足 closed schema：{location or 'root'}"
        )
    try:
        tag = payload["tag"]
        prerelease = payload["prerelease"]
        if bool("-" in tag) != prerelease:  # type: ignore[operator]
            raise PublicationEvidenceError("Release tag 与 prerelease 状态不一致")
        assets = tuple(
            ReleasePublicationAsset(
                name=item["name"],
                sha256=item["sha256"],
                size=item["size"],
            )
            for item in payload["assets"]  # type: ignore[union-attr]
        )
        transport_assets = tuple(
            ReleaseTransportAsset(
                name=item["name"],
                sha256=item["sha256"],
                size=item["size"],
                role=item["role"],
                authority_role=item["authorityRole"],
            )
            for item in payload["transportAssets"]  # type: ignore[union-attr]
        )
        expected_transport_name = f"animemo-{tag}-portable.tar"
        if (
            len(transport_assets) != 1
            or transport_assets[0].name != expected_transport_name
        ):
            raise PublicationEvidenceError(
                "GitHub Release transport asset 与 release tag 不一致"
            )
        return GitHubReleasePublication(
            tag=tag,  # type: ignore[arg-type]
            tag_commit=payload["tagCommit"],  # type: ignore[arg-type]
            tag_object=payload["tagObject"],  # type: ignore[arg-type]
            draft=payload["draft"],  # type: ignore[arg-type]
            prerelease=prerelease,  # type: ignore[arg-type]
            signed_at=payload["signedAt"],  # type: ignore[arg-type]
            assets=assets,
            transport_assets=transport_assets,
        )
    except (KeyError, TypeError) as error:
        raise PublicationEvidenceError("GitHub Release claim 结构无效") from error


def _closed_object(value: object, expected: set[str], label: str) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != expected:
        raise PublicationEvidenceError(f"{label} 字段集合未关闭")
    return value


def close_actions_provenance_claim(
    payload: Mapping[str, object],
) -> ActionsProvenanceClaim:
    """关闭已由外部实现验签的 GitHub Actions normalized claim。"""

    if not isinstance(payload, dict) or set(payload) != {
        "schemaVersion",
        "predicateType",
        "subject",
        "repository",
        "workflow",
        "certificate",
        "source",
        "signerDigest",
    }:
        raise PublicationEvidenceError("Actions provenance claim 字段集合未关闭")
    subject = _closed_object(payload["subject"], {"name", "sha256"}, "subject")
    repository = _closed_object(
        payload["repository"], {"name", "repositoryId", "ownerId"}, "repository"
    )
    certificate = _closed_object(
        payload["certificate"], {"identity", "issuer"}, "certificate"
    )
    source = _closed_object(payload["source"], {"commit", "ref"}, "source")
    workflow = payload["workflow"]
    source_commit = source["commit"]
    expected_identity = (
        f"https://github.com/{REPOSITORY}/{workflow}@{ACTIONS_SOURCE_REF}"
    )
    if (
        payload["schemaVersion"] != 1
        or payload["predicateType"] != ACTIONS_PREDICATE_TYPE
        or subject["name"] not in _ACTION_SUBJECTS
        or not isinstance(subject["sha256"], str)
        or not _SHA256.fullmatch(subject["sha256"])
        or repository
        != {
            "name": REPOSITORY,
            "repositoryId": REPOSITORY_ID,
            "ownerId": OWNER_ID,
        }
        or workflow not in _WORKFLOWS
        or certificate != {"identity": expected_identity, "issuer": ACTIONS_OIDC_ISSUER}
        or not isinstance(source_commit, str)
        or not _COMMIT.fullmatch(source_commit)
        or source["ref"] != ACTIONS_SOURCE_REF
        or payload["signerDigest"] != source_commit
    ):
        raise PublicationEvidenceError("Actions provenance claim 身份或绑定无效")
    return ActionsProvenanceClaim(
        subject_name=subject["name"],  # type: ignore[arg-type]
        subject_digest=subject["sha256"],
        repository=REPOSITORY,
        workflow=workflow,  # type: ignore[arg-type]
        certificate_identity=expected_identity,
        oidc_issuer=ACTIONS_OIDC_ISSUER,
        source_commit=source_commit,
        source_ref=ACTIONS_SOURCE_REF,
        signer_digest=source_commit,
        predicate_type=ACTIONS_PREDICATE_TYPE,
    )
