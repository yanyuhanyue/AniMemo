from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Protocol

from packaging.version import InvalidVersion, Version

from release.acquisition import (
    AttestationAcquisitionError,
    validate_attestation_sidecar,
)
from release.portable import (
    PortableBundleError,
    inspect_portable_archive,
    stage_portable_payload,
    validate_portable_bundle,
)
from release.publication_evidence import (
    ACTIONS_OIDC_ISSUER,
    ACTIONS_PREDICATE_TYPE,
    ACTIONS_SOURCE_REF,
    GITHUB_RELEASE_CERTIFICATE_IDENTITY,
    GITHUB_RELEASE_PREDICATE_TYPE,
    OWNER_ID,
    REPOSITORY_ID,
    ActionsProvenanceClaim,
    PublicationEvidenceError,
    close_actions_provenance_claim,
    close_github_release_publication,
)

from .authority import (
    EXPECTED_RELEASE_ASSETS,
    AttestationEvidence,
    AuthorityEvidence,
    ReleaseAssetEvidence,
    ReleaseAuthorityVerifier,
    VerifiedReleaseMaterials,
)
from .errors import RequestRejected
from .oci import OCIContractError, VerifiedOCIImageSet, verify_oci_image_set

_SHA256 = re.compile(r"sha256:[0-9a-f]{64}")
_MAX_SIDECAR_BYTES = 64 * 1024 * 1024
ACTIONS_EVIDENCE_NAMES = {
    "api": "api-image",
    "web": "web-image",
    "manifest": "release-manifest",
    "deployment": "deployment-contract",
    "materials": "installer-materials",
}


def _canonical_digest(payload: object) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


_OFFLINE_POLICY_DOCUMENT = {
    "actions": {
        "evidenceNames": list(ACTIONS_EVIDENCE_NAMES.values()),
        "oidcIssuer": ACTIONS_OIDC_ISSUER,
        "predicateType": ACTIONS_PREDICATE_TYPE,
        "runnerEnvironment": "github-hosted",
        "sourceRef": ACTIONS_SOURCE_REF,
        "workflows": [
            ".github/workflows/promote-release.yml",
            ".github/workflows/release.yml",
        ],
    },
    "githubRelease": {
        "certificateIdentity": GITHUB_RELEASE_CERTIFICATE_IDENTITY,
        "predicateType": GITHUB_RELEASE_PREDICATE_TYPE,
    },
    "model": "GITHUB_SIGSTORE_PORTABLE_EVIDENCE",
    "ownerId": OWNER_ID,
    "repository": "yanyuhanyue/AniMemo",
    "repositoryId": REPOSITORY_ID,
    "schemaVersion": 1,
}
OFFLINE_POLICY_IDENTITY = _canonical_digest(_OFFLINE_POLICY_DOCUMENT)


def _require_digest(value: str, *, label: str) -> None:
    if type(value) is not str or not _SHA256.fullmatch(value):
        raise ValueError(f"{label} 必须是规范 sha256 身份")


@dataclass(frozen=True)
class TrustProfile:
    """由安装介质预置的离线信任材料，不得从发布 bundle 自举。"""

    profile_version: int
    parent_profile_identity: str | None
    repository: str
    repository_id: str
    owner_id: str
    github_release_certificate_identity: str
    github_trusted_root_sha256: str
    sigstore_trusted_root_sha256: str
    github_tuf_root_sha256: str
    github_tuf_root_version: int
    github_tuf_timestamp_version: int
    github_tuf_snapshot_version: int
    github_tuf_targets_version: int
    sigstore_tuf_root_sha256: str
    sigstore_tuf_root_version: int
    sigstore_tuf_timestamp_version: int
    sigstore_tuf_snapshot_version: int
    sigstore_tuf_targets_version: int
    verifier_id: str
    minimum_verifier_version: str
    revocation_epoch: int
    revocation_snapshot_sha256: str
    verifier_identity: str | None = None
    policy_identity: str | None = None
    activation_sequence: int | None = None

    def __post_init__(self) -> None:
        if type(self.profile_version) is not int or self.profile_version < 1:
            raise ValueError("信任 profile 版本无效")
        if self.profile_version == 1:
            if self.parent_profile_identity is not None:
                raise ValueError("首个信任 profile 不得声明父身份")
        elif self.parent_profile_identity is None:
            raise ValueError("后继信任 profile 必须声明父身份")
        else:
            _require_digest(
                self.parent_profile_identity,
                label="父信任 profile 身份",
            )
        for value, label in (
            (self.github_trusted_root_sha256, "GitHub 信任根"),
            (self.sigstore_trusted_root_sha256, "Sigstore 信任根"),
            (self.github_tuf_root_sha256, "GitHub TUF bootstrap root"),
            (self.sigstore_tuf_root_sha256, "Sigstore TUF bootstrap root"),
            (self.revocation_snapshot_sha256, "撤销快照"),
        ):
            _require_digest(value, label=label)
        if any(
            type(value) is not str or not value
            for value in (
                self.repository,
                self.repository_id,
                self.owner_id,
                self.github_release_certificate_identity,
                self.verifier_id,
                self.minimum_verifier_version,
            )
        ):
            raise ValueError("信任 profile 的固定身份字段无效")
        try:
            Version(self.minimum_verifier_version)
        except InvalidVersion as error:
            raise ValueError("外部验证器最低版本无效") from error
        if type(self.revocation_epoch) is not int or self.revocation_epoch < 0:
            raise ValueError("撤销 epoch 无效")
        if (
            any(
                type(value) is not int or value < 1
                for value in (
                    self.github_tuf_root_version,
                    self.github_tuf_timestamp_version,
                    self.github_tuf_snapshot_version,
                    self.github_tuf_targets_version,
                    self.sigstore_tuf_root_version,
                    self.sigstore_tuf_timestamp_version,
                    self.sigstore_tuf_snapshot_version,
                    self.sigstore_tuf_targets_version,
                )
            )
        ):
            raise ValueError("TUF bootstrap root 版本无效")
        if self.verifier_identity is None:
            object.__setattr__(
                self,
                "verifier_identity",
                _canonical_digest(
                    {
                        "id": self.verifier_id,
                        "minimumVersion": self.minimum_verifier_version,
                    }
                ),
            )
        if self.policy_identity is None:
            object.__setattr__(self, "policy_identity", OFFLINE_POLICY_IDENTITY)
        if self.activation_sequence is None:
            object.__setattr__(
                self, "activation_sequence", self.profile_version
            )
        _require_digest(self.verifier_identity, label="外部验证器身份")  # type: ignore[arg-type]
        _require_digest(self.policy_identity, label="离线权威策略身份")  # type: ignore[arg-type]
        if (
            type(self.activation_sequence) is not int
            or self.activation_sequence < 1
        ):
            raise ValueError("信任 profile 激活序列无效")

    @property
    def identity(self) -> str:
        return _canonical_digest(
            {
                "activationSequence": self.activation_sequence,
                "githubReleaseCertificateIdentity": (
                    self.github_release_certificate_identity
                ),
                "githubRootIdentity": self.github_trusted_root_sha256,
                "githubTufRootIdentity": self.github_tuf_root_sha256,
                "githubTufRootVersion": self.github_tuf_root_version,
                "githubTufSnapshotVersion": self.github_tuf_snapshot_version,
                "githubTufTargetsVersion": self.github_tuf_targets_version,
                "githubTufTimestampVersion": self.github_tuf_timestamp_version,
                "minimumVerifierVersion": self.minimum_verifier_version,
                "ownerId": self.owner_id,
                "parentProfileIdentity": self.parent_profile_identity,
                "profileVersion": self.profile_version,
                "policyIdentity": self.policy_identity,
                "repository": self.repository,
                "repositoryId": self.repository_id,
                "revocationEpoch": self.revocation_epoch,
                "revocationSnapshotIdentity": self.revocation_snapshot_sha256,
                "schemaVersion": 2,
                "sigstoreRootIdentity": self.sigstore_trusted_root_sha256,
                "sigstoreTufRootIdentity": self.sigstore_tuf_root_sha256,
                "sigstoreTufRootVersion": self.sigstore_tuf_root_version,
                "sigstoreTufSnapshotVersion": self.sigstore_tuf_snapshot_version,
                "sigstoreTufTargetsVersion": self.sigstore_tuf_targets_version,
                "sigstoreTufTimestampVersion": self.sigstore_tuf_timestamp_version,
                "verifierId": self.verifier_id,
                "verifierIdentity": self.verifier_identity,
            }
        )

    def as_bootstrap_record(self) -> dict[str, object]:
        return {
            "activationSequence": self.activation_sequence,
            "githubReleaseCertificateIdentity": (
                self.github_release_certificate_identity
            ),
            "githubRootIdentity": self.github_trusted_root_sha256,
            "githubTufRootIdentity": self.github_tuf_root_sha256,
            "githubTufRootVersion": self.github_tuf_root_version,
            "githubTufSnapshotVersion": self.github_tuf_snapshot_version,
            "githubTufTargetsVersion": self.github_tuf_targets_version,
            "githubTufTimestampVersion": self.github_tuf_timestamp_version,
            "minimumVerifierVersion": self.minimum_verifier_version,
            "ownerId": self.owner_id,
            "parentProfileIdentity": self.parent_profile_identity,
            "policyIdentity": self.policy_identity,
            "profileIdentity": self.identity,
            "profileVersion": self.profile_version,
            "repository": self.repository,
            "repositoryId": self.repository_id,
            "revocationEpoch": self.revocation_epoch,
            "revocationSnapshotIdentity": self.revocation_snapshot_sha256,
            "schemaVersion": 2,
            "sigstoreRootIdentity": self.sigstore_trusted_root_sha256,
            "sigstoreTufRootIdentity": self.sigstore_tuf_root_sha256,
            "sigstoreTufRootVersion": self.sigstore_tuf_root_version,
            "sigstoreTufSnapshotVersion": self.sigstore_tuf_snapshot_version,
            "sigstoreTufTargetsVersion": self.sigstore_tuf_targets_version,
            "sigstoreTufTimestampVersion": self.sigstore_tuf_timestamp_version,
            "verifierId": self.verifier_id,
            "verifierIdentity": self.verifier_identity,
        }

    @classmethod
    def from_bootstrap_record(cls, record: Mapping[str, object]) -> TrustProfile:
        expected = {
            "activationSequence",
            "githubReleaseCertificateIdentity",
            "githubRootIdentity",
            "githubTufRootIdentity",
            "githubTufRootVersion",
            "githubTufSnapshotVersion",
            "githubTufTargetsVersion",
            "githubTufTimestampVersion",
            "minimumVerifierVersion",
            "ownerId",
            "parentProfileIdentity",
            "policyIdentity",
            "profileIdentity",
            "profileVersion",
            "repository",
            "repositoryId",
            "revocationEpoch",
            "revocationSnapshotIdentity",
            "schemaVersion",
            "sigstoreRootIdentity",
            "sigstoreTufRootIdentity",
            "sigstoreTufRootVersion",
            "sigstoreTufSnapshotVersion",
            "sigstoreTufTargetsVersion",
            "sigstoreTufTimestampVersion",
            "verifierId",
            "verifierIdentity",
        }
        if not isinstance(record, dict) or set(record) != expected:
            raise RequestRejected("预置信任 profile 字段集合未关闭")
        if (
            record["schemaVersion"] != 2
            or type(record["profileVersion"]) is not int
            or record["profileVersion"] < 1  # type: ignore[operator]
            or type(record["activationSequence"]) is not int
            or record["activationSequence"] < 1  # type: ignore[operator]
            or record["policyIdentity"] != OFFLINE_POLICY_IDENTITY
            or record["repository"] != "yanyuhanyue/AniMemo"
            or record["repositoryId"] != REPOSITORY_ID
            or record["ownerId"] != OWNER_ID
            or record["githubReleaseCertificateIdentity"]
            != GITHUB_RELEASE_CERTIFICATE_IDENTITY
            or record["verifierId"] != "github-sigstore-offline"
        ):
            raise RequestRejected("预置信任 profile 策略或版本无效")
        try:
            profile = cls(
                profile_version=record["profileVersion"],  # type: ignore[arg-type]
                parent_profile_identity=record["parentProfileIdentity"],  # type: ignore[arg-type]
                repository=record["repository"],  # type: ignore[arg-type]
                repository_id=record["repositoryId"],  # type: ignore[arg-type]
                owner_id=record["ownerId"],  # type: ignore[arg-type]
                github_release_certificate_identity=record[
                    "githubReleaseCertificateIdentity"
                ],  # type: ignore[arg-type]
                github_trusted_root_sha256=record["githubRootIdentity"],  # type: ignore[arg-type]
                sigstore_trusted_root_sha256=record["sigstoreRootIdentity"],  # type: ignore[arg-type]
                github_tuf_root_sha256=record["githubTufRootIdentity"],  # type: ignore[arg-type]
                github_tuf_root_version=record["githubTufRootVersion"],  # type: ignore[arg-type]
                github_tuf_timestamp_version=record["githubTufTimestampVersion"],  # type: ignore[arg-type]
                github_tuf_snapshot_version=record["githubTufSnapshotVersion"],  # type: ignore[arg-type]
                github_tuf_targets_version=record["githubTufTargetsVersion"],  # type: ignore[arg-type]
                sigstore_tuf_root_sha256=record["sigstoreTufRootIdentity"],  # type: ignore[arg-type]
                sigstore_tuf_root_version=record["sigstoreTufRootVersion"],  # type: ignore[arg-type]
                sigstore_tuf_timestamp_version=record["sigstoreTufTimestampVersion"],  # type: ignore[arg-type]
                sigstore_tuf_snapshot_version=record["sigstoreTufSnapshotVersion"],  # type: ignore[arg-type]
                sigstore_tuf_targets_version=record["sigstoreTufTargetsVersion"],  # type: ignore[arg-type]
                verifier_id=record["verifierId"],  # type: ignore[arg-type]
                minimum_verifier_version=record["minimumVerifierVersion"],  # type: ignore[arg-type]
                revocation_epoch=record["revocationEpoch"],  # type: ignore[arg-type]
                revocation_snapshot_sha256=record[
                    "revocationSnapshotIdentity"
                ],  # type: ignore[arg-type]
                verifier_identity=record["verifierIdentity"],  # type: ignore[arg-type]
                policy_identity=record["policyIdentity"],  # type: ignore[arg-type]
                activation_sequence=record["activationSequence"],  # type: ignore[arg-type]
            )
        except ValueError as error:
            raise RequestRejected("预置信任 profile 身份字段无效") from error
        if record["profileIdentity"] != profile.identity:
            raise RequestRejected("预置信任 profile identity 不一致")
        return profile


@dataclass(frozen=True)
class PretrustedTrustMaterial:
    root: Path
    profile: TrustProfile
    github_trusted_root_path: Path
    sigstore_trusted_root_path: Path
    github_tuf_root_path: Path
    sigstore_tuf_root_path: Path
    verifier_path: Path

    @classmethod
    def load(cls, root: Path) -> PretrustedTrustMaterial:
        root = Path(root)
        try:
            root_metadata = root.lstat()
        except OSError as error:
            raise RequestRejected("预置信任目录不可用") from error
        if root.is_symlink() or not stat.S_ISDIR(root_metadata.st_mode):
            raise RequestRejected("预置信任目录类型无效")
        if os.name != "nt" and (
            root_metadata.st_uid != 0 or root_metadata.st_mode & 0o022
        ):
            raise RequestRejected("生产预置信任目录不是 root 独占写入")
        expected = {
            "github-trusted-root.jsonl",
            "github-tuf-root.json",
            "offline-release-verifier",
            "sigstore-trusted-root.jsonl",
            "sigstore-tuf-root.json",
            "trust-profile.json",
        }
        try:
            observed = {item.name for item in root.iterdir()}
        except OSError as error:
            raise RequestRejected("预置信任目录不可读") from error
        if observed != expected:
            raise RequestRejected("预置信任目录未关闭")
        profile_bytes = _read_pretrusted_file(
            root / "trust-profile.json", max_bytes=64 * 1024
        )
        try:
            record = json.loads(profile_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RequestRejected("预置信任 profile 不可解析") from error
        if (
            not isinstance(record, dict)
            or json.dumps(
                record,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            != profile_bytes
        ):
            raise RequestRejected("预置信任 profile 不是 canonical JSON")
        profile = TrustProfile.from_bootstrap_record(record)
        github_path = root / "github-trusted-root.jsonl"
        sigstore_path = root / "sigstore-trusted-root.jsonl"
        verifier_path = root / "offline-release-verifier"
        github_tuf_path = root / "github-tuf-root.json"
        sigstore_tuf_path = root / "sigstore-tuf-root.json"
        identities = {
            "github": "sha256:"
            + hashlib.sha256(
                _read_pretrusted_file(github_path, max_bytes=16 * 1024 * 1024)
            ).hexdigest(),
            "sigstore": "sha256:"
            + hashlib.sha256(
                _read_pretrusted_file(sigstore_path, max_bytes=16 * 1024 * 1024)
            ).hexdigest(),
            "verifier": "sha256:"
            + hashlib.sha256(
                _read_pretrusted_file(verifier_path, max_bytes=256 * 1024 * 1024)
            ).hexdigest(),
            "githubTuf": "sha256:"
            + hashlib.sha256(
                _read_pretrusted_file(github_tuf_path, max_bytes=16 * 1024 * 1024)
            ).hexdigest(),
            "sigstoreTuf": "sha256:"
            + hashlib.sha256(
                _read_pretrusted_file(sigstore_tuf_path, max_bytes=16 * 1024 * 1024)
            ).hexdigest(),
        }
        if identities != {
            "github": profile.github_trusted_root_sha256,
            "sigstore": profile.sigstore_trusted_root_sha256,
            "verifier": profile.verifier_identity,
            "githubTuf": profile.github_tuf_root_sha256,
            "sigstoreTuf": profile.sigstore_tuf_root_sha256,
        }:
            raise RequestRejected("预置信任材料身份不一致")
        if os.name != "nt":
            for path in (
                root / "trust-profile.json",
                github_path,
                sigstore_path,
                github_tuf_path,
                sigstore_tuf_path,
                verifier_path,
            ):
                metadata = path.lstat()
                if metadata.st_uid != 0 or metadata.st_mode & 0o022:
                    raise RequestRejected("生产预置信任材料不是 root 独占写入")
            if not os.access(verifier_path, os.X_OK):
                raise RequestRejected("预置外部验证器不可执行")
        return cls(
            root=root,
            profile=profile,
            github_trusted_root_path=github_path,
            sigstore_trusted_root_path=sigstore_path,
            github_tuf_root_path=github_tuf_path,
            sigstore_tuf_root_path=sigstore_tuf_path,
            verifier_path=verifier_path,
        )


def _read_pretrusted_file(path: Path, *, max_bytes: int) -> bytes:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise RequestRejected("预置信任材料不可用") from error
    if (
        path.is_symlink()
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or metadata.st_size < 1
        or metadata.st_size > max_bytes
    ):
        raise RequestRejected("预置信任材料文件类型或大小无效")
    try:
        value = path.read_bytes()
    except OSError as error:
        raise RequestRejected("预置信任材料不可读") from error
    if len(value) != metadata.st_size:
        raise RequestRejected("预置信任材料读取期间发生变化")
    return value


@dataclass(frozen=True)
class OfflineAuthorityState:
    schema_version: int
    generation: int
    active_profile_version: int
    active_profile_identity: str
    highest_release_version: str | None
    accepted_publication_identities: frozenset[str]
    revoked_evidence_identities: frozenset[str]

    @classmethod
    def initial(cls, profile: TrustProfile) -> OfflineAuthorityState:
        if type(profile) is not TrustProfile:
            raise ValueError("初始状态必须绑定预置信任 profile")
        return cls(
            schema_version=1,
            generation=0,
            active_profile_version=profile.profile_version,
            active_profile_identity=profile.identity,
            highest_release_version=None,
            accepted_publication_identities=frozenset(),
            revoked_evidence_identities=frozenset(),
        )

    def accept_publication(
        self,
        *,
        profile: TrustProfile,
        publication_identity: str,
        release_version: str,
    ) -> OfflineAuthorityState:
        _require_digest(publication_identity, label="publication 身份")
        if (
            self.active_profile_version != profile.profile_version
            or self.active_profile_identity != profile.identity
        ):
            raise RequestRejected("离线状态与预置信任 profile 不一致")
        if publication_identity in self.revoked_evidence_identities:
            raise RequestRejected("发布证明已被已知撤销状态拒绝")
        if publication_identity in self.accepted_publication_identities:
            raise RequestRejected("发布证明重放被拒绝")
        try:
            candidate = Version(release_version.removeprefix("v"))
            highest = (
                Version(self.highest_release_version.removeprefix("v"))
                if self.highest_release_version is not None
                else None
            )
        except InvalidVersion as error:
            raise RequestRejected("发布版本身份无效") from error
        if highest is not None and candidate <= highest:
            raise RequestRejected("发布版本降级或重放被拒绝")
        return replace(
            self,
            generation=self.generation + 1,
            highest_release_version=release_version,
            accepted_publication_identities=(
                self.accepted_publication_identities | {publication_identity}
            ),
        )

    def as_record(self) -> dict[str, object]:
        return {
            "acceptedPublicationIdentities": sorted(
                self.accepted_publication_identities
            ),
            "activeProfileIdentity": self.active_profile_identity,
            "activeProfileVersion": self.active_profile_version,
            "generation": self.generation,
            "highestReleaseVersion": self.highest_release_version,
            "revokedEvidenceIdentities": sorted(self.revoked_evidence_identities),
            "schemaVersion": self.schema_version,
        }

    @classmethod
    def from_record(cls, record: Mapping[str, object]) -> OfflineAuthorityState:
        expected = {
            "acceptedPublicationIdentities",
            "activeProfileIdentity",
            "activeProfileVersion",
            "generation",
            "highestReleaseVersion",
            "revokedEvidenceIdentities",
            "schemaVersion",
        }
        if not isinstance(record, dict) or set(record) != expected:
            raise RequestRejected("离线权威耐久状态字段集合未关闭")
        accepted = record["acceptedPublicationIdentities"]
        revoked = record["revokedEvidenceIdentities"]
        if (
            record["schemaVersion"] != 1
            or type(record["generation"]) is not int
            or record["generation"] < 0  # type: ignore[operator]
            or type(record["activeProfileVersion"]) is not int
            or record["activeProfileVersion"] < 1  # type: ignore[operator]
            or type(record["activeProfileIdentity"]) is not str
            or not _SHA256.fullmatch(record["activeProfileIdentity"])
            or (
                record["highestReleaseVersion"] is not None
                and type(record["highestReleaseVersion"]) is not str
            )
            or not isinstance(accepted, list)
            or accepted != sorted(set(accepted))
            or any(type(item) is not str or not _SHA256.fullmatch(item) for item in accepted)
            or not isinstance(revoked, list)
            or revoked != sorted(set(revoked))
            or any(type(item) is not str or not _SHA256.fullmatch(item) for item in revoked)
        ):
            raise RequestRejected("离线权威耐久状态无效")
        return cls(
            schema_version=1,
            generation=record["generation"],  # type: ignore[arg-type]
            active_profile_version=record["activeProfileVersion"],  # type: ignore[arg-type]
            active_profile_identity=record["activeProfileIdentity"],  # type: ignore[arg-type]
            highest_release_version=record["highestReleaseVersion"],  # type: ignore[arg-type]
            accepted_publication_identities=frozenset(accepted),
            revoked_evidence_identities=frozenset(revoked),
        )


class ExternalEvidenceVerifier(Protocol):
    """冻结的外部密码学验证器；AniMemo 只消费其 closed claims。"""

    verifier_id: str
    verifier_version: str

    def verify_github_release(
        self,
        *,
        bundle: bytes,
        trust_profile: TrustProfile,
        tag: str | None = None,
        tag_commit: str | None = None,
        expected_subjects: Sequence[Mapping[str, object]] | None = None,
    ) -> Mapping[str, object]: ...

    def verify_actions_provenance(
        self,
        *,
        bundle: bytes,
        evidence_name: str,
        trust_profile: TrustProfile,
        subject_name: str | None = None,
        subject_sha256: str | None = None,
        workflow: str | None = None,
        source_commit: str | None = None,
    ) -> Mapping[str, object]: ...

    def verify_trust_update(
        self,
        *,
        bundle: bytes,
        current_profile: TrustProfile,
        successor_profile: TrustProfile,
    ) -> Mapping[str, object]: ...


def _canonical_json_bytes(payload: object) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


class SigstoreGoEvidenceVerifier:
    """受预置 binary hash 约束的 network-free Sigstore 外部验证器 adapter。"""

    verifier_id = "github-sigstore-offline"
    verifier_version = "2.97.0"

    def __init__(
        self,
        material: PretrustedTrustMaterial,
        *,
        runner: Callable[..., subprocess.CompletedProcess[bytes]] = subprocess.run,
    ) -> None:
        if type(material) is not PretrustedTrustMaterial:
            raise ValueError("外部验证器必须绑定预置信任材料")
        self._material = material
        self._runner = runner

    def verify_github_release(
        self,
        *,
        bundle: bytes,
        trust_profile: TrustProfile,
        tag: str | None = None,
        tag_commit: str | None = None,
        expected_subjects: Sequence[Mapping[str, object]] | None = None,
    ) -> Mapping[str, object]:
        if (
            tag is None
            or tag_commit is None
            or expected_subjects is None
            or trust_profile.identity != self._material.profile.identity
        ):
            raise RequestRejected("GitHub Release 外部验证输入不完整")
        return self._invoke(
            bundle=bundle,
            evidence_name="github-release",
            trusted_root=self._material.github_trusted_root_path,
            request={
                "expectedSubjects": [dict(item) for item in expected_subjects],
                "mode": "github-release",
                "ownerId": OWNER_ID,
                "repository": "yanyuhanyue/AniMemo",
                "repositoryId": REPOSITORY_ID,
                "schemaVersion": 1,
                "tag": tag,
                "tagCommit": tag_commit,
            },
        )

    def verify_actions_provenance(
        self,
        *,
        bundle: bytes,
        evidence_name: str,
        trust_profile: TrustProfile,
        subject_name: str | None = None,
        subject_sha256: str | None = None,
        workflow: str | None = None,
        source_commit: str | None = None,
    ) -> Mapping[str, object]:
        if (
            subject_name is None
            or subject_sha256 is None
            or workflow is None
            or source_commit is None
            or trust_profile.identity != self._material.profile.identity
        ):
            raise RequestRejected("Actions provenance 外部验证输入不完整")
        return self._invoke(
            bundle=bundle,
            evidence_name=evidence_name,
            trusted_root=self._material.sigstore_trusted_root_path,
            request={
                "evidenceName": evidence_name,
                "mode": "actions-provenance",
                "schemaVersion": 1,
                "sourceCommit": source_commit,
                "subject": {
                    "name": subject_name,
                    "sha256": subject_sha256,
                    "size": 0,
                },
                "workflow": workflow,
            },
        )

    def verify_trust_update(
        self,
        *,
        bundle: bytes,
        current_profile: TrustProfile,
        successor_profile: TrustProfile,
    ) -> Mapping[str, object]:
        raise RequestRejected("生产信任轮换验证器尚未在本 profile 激活")

    def verify_tuf_update_package(
        self,
        *,
        package: bytes,
        current_profile: TrustProfile,
    ) -> Mapping[str, object]:
        """使用同一冻结二进制验证两条官方 TUF 连续更新链。"""

        if (
            type(package) is not bytes
            or not package
            or len(package) > 64 * 1024 * 1024
            or type(current_profile) is not TrustProfile
            or current_profile.identity != self._material.profile.identity
        ):
            raise RequestRejected("TUF 信任更新外部验证输入不完整")
        try:
            decoded = json.loads(package.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RequestRejected("TUF 信任更新包不可解析") from error
        if _canonical_json_bytes(decoded) != package:
            raise RequestRejected("TUF 信任更新包不是 canonical JSON")
        request = {
            "authorityRole": "TRUST_METADATA_ONLY",
            "fromProfileIdentity": current_profile.identity,
            "github": {
                "snapshotVersion": current_profile.github_tuf_snapshot_version,
                "targetsVersion": current_profile.github_tuf_targets_version,
                "timestampVersion": current_profile.github_tuf_timestamp_version,
                "trustedRootSha256": current_profile.github_trusted_root_sha256,
                "tufRootSha256": current_profile.github_tuf_root_sha256,
                "tufRootVersion": current_profile.github_tuf_root_version,
            },
            "mode": "tuf-trust-update",
            "schemaVersion": 1,
            "sigstore": {
                "snapshotVersion": current_profile.sigstore_tuf_snapshot_version,
                "targetsVersion": current_profile.sigstore_tuf_targets_version,
                "timestampVersion": current_profile.sigstore_tuf_timestamp_version,
                "trustedRootSha256": current_profile.sigstore_trusted_root_sha256,
                "tufRootSha256": current_profile.sigstore_tuf_root_sha256,
                "tufRootVersion": current_profile.sigstore_tuf_root_version,
            },
        }
        with tempfile.TemporaryDirectory(prefix="animemo-tuf-update-") as temp:
            root = Path(temp)
            package_path = root / "trust-update.json"
            request_path = root / "request.json"
            package_path.write_bytes(package)
            request_path.write_bytes(_canonical_json_bytes(request))
            return self._execute_closed(
                (
                    str(self._material.verifier_path),
                    "--trust-update",
                    str(package_path),
                    "--github-tuf-root",
                    str(self._material.github_tuf_root_path),
                    "--sigstore-tuf-root",
                    str(self._material.sigstore_tuf_root_path),
                    "--request",
                    str(request_path),
                ),
                rejected_message="冻结的外部验证器拒绝 TUF 更新",
            )

    def _invoke(
        self,
        *,
        bundle: bytes,
        evidence_name: str,
        trusted_root: Path,
        request: Mapping[str, object],
    ) -> Mapping[str, object]:
        sigstore_bundle = _extract_sigstore_bundle(bundle, evidence_name)
        with tempfile.TemporaryDirectory(prefix="animemo-offline-verify-") as temp:
            root = Path(temp)
            bundle_path = root / "bundle.json"
            request_path = root / "request.json"
            bundle_path.write_bytes(_canonical_json_bytes(sigstore_bundle))
            request_path.write_bytes(_canonical_json_bytes(dict(request)))
            command = (
                str(self._material.verifier_path),
                "--bundle",
                str(bundle_path),
                "--trusted-root",
                str(trusted_root),
                "--request",
                str(request_path),
            )
            return self._execute_closed(
                command,
                rejected_message="冻结的外部验证器拒绝证明",
            )

    def _execute_closed(
        self,
        command: tuple[str, ...],
        *,
        rejected_message: str,
    ) -> Mapping[str, object]:
        environment = {"LANG": "C", "LC_ALL": "C"}
        if os.name == "nt" and "SystemRoot" in os.environ:
            environment["SystemRoot"] = os.environ["SystemRoot"]
        try:
            completed = self._runner(
                command,
                check=False,
                capture_output=True,
                env=environment,
                timeout=60,
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise RequestRejected("冻结的外部验证器执行失败") from error
        if completed.returncode != 0:
            raise RequestRejected(rejected_message)
        if completed.stderr or not completed.stdout or len(completed.stdout) > 1024 * 1024:
            raise RequestRejected("冻结的外部验证器输出通道未关闭")
        try:
            normalized = json.loads(completed.stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RequestRejected("冻结的外部验证器输出不可解析") from error
        if not isinstance(normalized, dict):
            raise RequestRejected("冻结的外部验证器输出不是对象")
        if completed.stdout != _canonical_json_bytes(normalized) + b"\n":
            raise RequestRejected("冻结的外部验证器输出不是 canonical JSON")
        return normalized


def _extract_sigstore_bundle(sidecar: bytes, evidence_name: str) -> Mapping[str, object]:
    try:
        envelope = validate_attestation_sidecar(sidecar)
        record = envelope["evidence"][evidence_name]
        decoded = base64.b64decode(record["value"], validate=True)
        bundle_set = json.loads(decoded.decode("utf-8"))
    except (
        AttestationAcquisitionError,
        KeyError,
        TypeError,
        ValueError,
        binascii.Error,
        UnicodeDecodeError,
        json.JSONDecodeError,
    ) as error:
        raise RequestRejected("官方 attestation sidecar 证据不可提取") from error
    if (
        not isinstance(bundle_set, list)
        or len(bundle_set) != 1
        or not isinstance(bundle_set[0], dict)
        or set(bundle_set[0]) != {"attestation"}
        or not isinstance(bundle_set[0]["attestation"], dict)
    ):
        raise RequestRejected("官方 attestation bundle 集合不唯一")
    attestation = bundle_set[0]["attestation"]
    if "bundle" not in attestation or not isinstance(attestation["bundle"], dict):
        raise RequestRejected("官方 attestation 不含本地 Sigstore bundle")
    return attestation["bundle"]


def _verifier_is_qualified(
    verifier: ExternalEvidenceVerifier, profile: TrustProfile
) -> bool:
    try:
        return verifier.verifier_id == profile.verifier_id and Version(
            verifier.verifier_version
        ) >= Version(profile.minimum_verifier_version)
    except (AttributeError, InvalidVersion):
        return False


def advance_trust_profile(
    *,
    current_profile: TrustProfile,
    successor_profile: TrustProfile,
    state: OfflineAuthorityState,
    external_verifier: ExternalEvidenceVerifier,
    update_bundle: bytes,
) -> OfflineAuthorityState:
    """经外部验证的连续更新推进 profile，并单调合并已知撤销。"""

    if (
        type(current_profile) is not TrustProfile
        or type(successor_profile) is not TrustProfile
        or type(state) is not OfflineAuthorityState
        or type(update_bundle) is not bytes
        or not update_bundle
    ):
        raise RequestRejected("信任更新输入无效")
    if (
        state.active_profile_version != current_profile.profile_version
        or state.active_profile_identity != current_profile.identity
    ):
        raise RequestRejected("信任更新起点与耐久状态不一致")
    if (
        successor_profile.profile_version != current_profile.profile_version + 1
        or successor_profile.parent_profile_identity != current_profile.identity
        or successor_profile.repository != current_profile.repository
        or successor_profile.repository_id != current_profile.repository_id
        or successor_profile.owner_id != current_profile.owner_id
        or successor_profile.github_release_certificate_identity
        != current_profile.github_release_certificate_identity
        or successor_profile.verifier_id != current_profile.verifier_id
        or successor_profile.github_tuf_root_version
        < current_profile.github_tuf_root_version
        or successor_profile.github_tuf_timestamp_version
        < current_profile.github_tuf_timestamp_version
        or successor_profile.github_tuf_snapshot_version
        < current_profile.github_tuf_snapshot_version
        or successor_profile.github_tuf_targets_version
        < current_profile.github_tuf_targets_version
        or successor_profile.sigstore_tuf_root_version
        < current_profile.sigstore_tuf_root_version
        or successor_profile.sigstore_tuf_timestamp_version
        < current_profile.sigstore_tuf_timestamp_version
        or successor_profile.sigstore_tuf_snapshot_version
        < current_profile.sigstore_tuf_snapshot_version
        or successor_profile.sigstore_tuf_targets_version
        < current_profile.sigstore_tuf_targets_version
        or successor_profile.revocation_epoch <= current_profile.revocation_epoch
    ):
        raise RequestRejected("后继信任 profile 不是精确连续更新")
    if not _verifier_is_qualified(external_verifier, current_profile):
        raise RequestRejected("信任更新外部验证器身份或版本不合格")
    try:
        claim = external_verifier.verify_trust_update(
            bundle=update_bundle,
            current_profile=current_profile,
            successor_profile=successor_profile,
        )
    except Exception as error:
        raise RequestRejected("信任更新的外部密码学验证失败") from error
    expected_keys = {
        "schemaVersion",
        "fromProfileIdentity",
        "toProfileIdentity",
        "fromVersion",
        "toVersion",
        "revocationEpoch",
        "revocationSnapshotSha256",
        "revokedEvidenceIdentities",
    }
    revoked = (
        claim.get("revokedEvidenceIdentities") if isinstance(claim, Mapping) else None
    )
    if (
        not isinstance(claim, Mapping)
        or set(claim) != expected_keys
        or claim.get("schemaVersion") != 1
        or claim.get("fromProfileIdentity") != current_profile.identity
        or claim.get("toProfileIdentity") != successor_profile.identity
        or claim.get("fromVersion") != current_profile.profile_version
        or claim.get("toVersion") != successor_profile.profile_version
        or claim.get("revocationEpoch") != successor_profile.revocation_epoch
        or claim.get("revocationSnapshotSha256")
        != successor_profile.revocation_snapshot_sha256
        or not isinstance(revoked, list)
        or any(type(item) is not str or not _SHA256.fullmatch(item) for item in revoked)
        or revoked != sorted(set(revoked))
    ):
        raise RequestRejected("信任更新 claim 绑定无效")
    return replace(
        state,
        generation=state.generation + 1,
        active_profile_version=successor_profile.profile_version,
        active_profile_identity=successor_profile.identity,
        revoked_evidence_identities=(
            state.revoked_evidence_identities | frozenset(revoked)
        ),
    )


@dataclass(frozen=True)
class VerifiedPortableRelease:
    """仅可由完整离线权威验证路径产生的运输无关结果。"""

    materials: VerifiedReleaseMaterials
    images: VerifiedOCIImageSet
    payload_sha256: str
    authority_evidence: AuthorityEvidence
    next_state: OfflineAuthorityState
    publication_identity: str
    release_attestation_identity: str
    actions_evidence_identity: str
    trust_profile_version: int
    trust_profile_identity: str
    authenticity_status: str = "AUTHENTIC_AS_OF_SIGNED_EVIDENCE"
    revocation_status: str = "OFFLINE_FUTURE_REVOCATION_UNKNOWN"


class OfflineReleaseVerifier:
    def __init__(
        self,
        *,
        trust_profile: TrustProfile | None,
        external_verifier: ExternalEvidenceVerifier | None = None,
        production_blocked_reason: str | None = None,
        idempotent_reverification: bool = False,
        oci_verifier: Callable[[Path, object], VerifiedOCIImageSet] = (
            verify_oci_image_set
        ),
    ) -> None:
        self._profile = trust_profile
        self._external = external_verifier
        self._production_blocked_reason = production_blocked_reason
        self._idempotent_reverification = idempotent_reverification
        self._oci_verifier = oci_verifier

    def verify(
        self,
        *,
        payload: Path,
        sidecar: Path,
        destination: Path,
        updater_version: str,
        state: OfflineAuthorityState | None = None,
        expected_rollback_version: str | None = None,
    ) -> VerifiedPortableRelease:
        if expected_rollback_version is not None:
            try:
                Version(expected_rollback_version.removeprefix("v"))
            except (AttributeError, InvalidVersion) as error:
                raise RequestRejected("PREVIOUS 回滚版本身份无效") from error
        sidecar_bundle = _read_official_sidecar(sidecar)
        if self._production_blocked_reason is not None:
            raise RequestRejected(self._production_blocked_reason)
        if self._profile is None:
            raise RequestRejected("正式预置信任 profile 未冻结")
        if self._external is None:
            raise RequestRejected("冻结的外部离线密码学验证器不可用")
        if not _verifier_is_qualified(self._external, self._profile):
            raise RequestRejected("冻结的外部验证器身份或版本不合格")
        if state is None:
            state = OfflineAuthorityState.initial(self._profile)
        if (
            type(state) is not OfflineAuthorityState
            or state.active_profile_version != self._profile.profile_version
            or state.active_profile_identity != self._profile.identity
        ):
            raise RequestRejected("离线耐久状态与预置信任 profile 不一致")

        payload = Path(payload)
        destination = Path(destination)
        try:
            inspection = inspect_portable_archive(payload)
        except (PortableBundleError, PublicationEvidenceError) as error:
            raise RequestRejected("Portable 发布权威证明无效") from error

        destination_parent = destination.resolve().parent
        destination_parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        portable_root = destination_parent / (
            ".animemo-offline-payload-" + inspection.archive_sha256[7:]
        )
        created_staging: Path | None = None
        try:
            if portable_root.exists():
                bundle = validate_portable_bundle(portable_root)
            else:
                bundle = stage_portable_payload(payload, destination_parent)
                created_staging = bundle.root
                staged_inspection = inspect_portable_archive(payload)
                if (
                    staged_inspection.archive_sha256 != inspection.archive_sha256
                    or staged_inspection.index != bundle.index
                ):
                    raise RequestRejected("Portable payload 在私有暂存期间发生变化")

            assets = {
                name: bundle.file(f"authority/{name}").read_bytes()
                for name in sorted(EXPECTED_RELEASE_ASSETS)
            }
            authority_inputs = _manifest_authority_inputs(assets)
            expected_subjects = [
                {
                    "name": name,
                    "sha256": "sha256:" + hashlib.sha256(value).hexdigest(),
                    "size": len(value),
                }
                for name, value in sorted(assets.items())
            ] + [
                {
                    "name": payload.name,
                    "sha256": inspection.archive_sha256,
                    "size": payload.stat().st_size,
                }
            ]
            try:
                external_release = self._external.verify_github_release(
                    bundle=sidecar_bundle,
                    trust_profile=self._profile,
                    tag=authority_inputs["tag"],
                    tag_commit=authority_inputs["tag_commit"],
                    expected_subjects=expected_subjects,
                )
                publication = close_github_release_publication(external_release)
            except PublicationEvidenceError as error:
                raise RequestRejected("Portable 发布权威证明无效") from error
            except Exception as error:
                raise RequestRejected("GitHub Immutable Release 外部验证失败") from error
            _bind_release_assets(publication.assets, assets)
            _bind_release_transport(
                publication.transport_assets,
                payload=payload,
                payload_sha256=inspection.archive_sha256,
            )
            claims = tuple(
                self._verified_actions_claim(
                    sidecar_bundle,
                    evidence_name,
                    authority_inputs["actions"][evidence_name],
                )
                for evidence_name in ACTIONS_EVIDENCE_NAMES.values()
            )
            authority = AuthorityEvidence(
                repository=self._profile.repository,
                version=publication.tag,
                draft=publication.draft,
                prerelease=publication.prerelease,
                tag_commit=publication.tag_commit,
                assets=tuple(
                    ReleaseAssetEvidence(name=name, state="uploaded")
                    for name in sorted(EXPECTED_RELEASE_ASSETS)
                ),
                attestations=tuple(_authority_attestation(item) for item in claims),
            )
            _bind_oci_to_release_manifest(bundle.index, assets)
            images = self._oci_verifier(bundle.root, bundle.index["ociImages"])
            materials = ReleaseAuthorityVerifier().verify(
                assets=assets,
                authority=authority,
                destination=destination,
                updater_version=updater_version,
            )
            if (
                self._idempotent_reverification
                and publication.identity in state.accepted_publication_identities
                and (
                    state.highest_release_version == publication.tag
                    or expected_rollback_version == publication.tag
                )
            ):
                next_state = state
            else:
                next_state = state.accept_publication(
                    profile=self._profile,
                    publication_identity=publication.identity,
                    release_version=publication.tag,
                )

            if created_staging is not None:
                os.replace(created_staging, portable_root)
                created_staging = None
                images = self._oci_verifier(portable_root, bundle.index["ociImages"])
            return VerifiedPortableRelease(
                materials=materials,
                images=images,
                payload_sha256=inspection.archive_sha256,
                authority_evidence=authority,
                next_state=next_state,
                publication_identity=publication.identity,
                release_attestation_identity=(
                    "sha256:" + hashlib.sha256(sidecar_bundle).hexdigest()
                ),
                actions_evidence_identity=_canonical_digest(
                    [claim.identity for claim in claims]
                ),
                trust_profile_version=self._profile.profile_version,
                trust_profile_identity=self._profile.identity,
            )
        except RequestRejected:
            raise
        except (OSError, PortableBundleError, OCIContractError, ValueError) as error:
            raise RequestRejected("Portable release 编排验证失败") from error
        finally:
            if created_staging is not None:
                shutil.rmtree(created_staging, ignore_errors=True)

    def _verified_actions_claim(
        self,
        sidecar_bundle: bytes,
        evidence_name: str,
        expected: Mapping[str, str],
    ) -> ActionsProvenanceClaim:
        assert self._external is not None and self._profile is not None
        try:
            normalized = self._external.verify_actions_provenance(
                bundle=sidecar_bundle,
                evidence_name=evidence_name,
                trust_profile=self._profile,
                subject_name=expected["subject_name"],
                subject_sha256=expected["subject_sha256"],
                workflow=expected["workflow"],
                source_commit=expected["source_commit"],
            )
            return close_actions_provenance_claim(normalized)
        except (OSError, PortableBundleError, PublicationEvidenceError) as error:
            raise RequestRejected("GitHub Actions provenance 证明无效") from error
        except Exception as error:
            raise RequestRejected("GitHub Actions provenance 外部验证失败") from error


def _read_official_sidecar(sidecar: Path) -> bytes:
    sidecar = Path(sidecar)
    try:
        metadata = sidecar.lstat()
    except OSError as error:
        raise RequestRejected("官方 GitHub Immutable Release 证明缺失") from error
    if (
        sidecar.is_symlink()
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or metadata.st_size < 1
        or metadata.st_size > _MAX_SIDECAR_BYTES
    ):
        raise RequestRejected("官方 GitHub Immutable Release 证明缺失或文件类型不安全")
    try:
        value = sidecar.read_bytes()
    except OSError as error:
        raise RequestRejected("官方 GitHub Immutable Release 证明不可读") from error
    if len(value) != metadata.st_size:
        raise RequestRejected("官方 GitHub Immutable Release 证明读取期间发生变化")
    return value


def _bind_release_assets(claims: Sequence[object], assets: Mapping[str, bytes]) -> None:
    observed = {
        item.name: (item.sha256, item.size)  # type: ignore[attr-defined]
        for item in claims
    }
    expected = {
        name: ("sha256:" + hashlib.sha256(value).hexdigest(), len(value))
        for name, value in assets.items()
    }
    if observed != expected:
        raise RequestRejected("Immutable Release asset claim 与 canonical bytes 不一致")


def _bind_release_transport(
    claims: Sequence[object], *, payload: Path, payload_sha256: str
) -> None:
    try:
        metadata = payload.lstat()
    except OSError as error:
        raise RequestRejected("Portable transport asset 不可读取") from error
    expected = {
        payload.name: (
            payload_sha256,
            metadata.st_size,
            "PORTABLE_RELEASE_BUNDLE",
            "TRANSPORT_ONLY",
        )
    }
    observed = {
        item.name: (  # type: ignore[attr-defined]
            item.sha256,  # type: ignore[attr-defined]
            item.size,  # type: ignore[attr-defined]
            item.role,  # type: ignore[attr-defined]
            item.authority_role,  # type: ignore[attr-defined]
        )
        for item in claims
    }
    if observed != expected:
        raise RequestRejected(
            "Immutable Release transport claim 与 portable payload 不一致"
        )


def _manifest_authority_inputs(
    assets: Mapping[str, bytes],
) -> dict[str, object]:
    try:
        manifest = json.loads(assets["release-manifest.json"].decode("utf-8"))
        release = manifest["release"]
        provenance = manifest["provenance"]
        images = manifest["images"]
        tag = release["version"]
        tag_commit = release["commit"]
        provenance_workflow = provenance["workflow"]
        provenance_commit = provenance["sourceCommit"]
        action_inputs = {
            "api-image": {
                "subject_name": "ghcr.io/yanyuhanyue/animemo-api",
                "subject_sha256": images["api"]["digest"],
                "workflow": ".github/workflows/release.yml",
                "source_commit": tag_commit,
            },
            "web-image": {
                "subject_name": "ghcr.io/yanyuhanyue/animemo-web",
                "subject_sha256": images["web"]["digest"],
                "workflow": ".github/workflows/release.yml",
                "source_commit": tag_commit,
            },
        }
        for evidence_name, asset_name in (
            ("release-manifest", "release-manifest.json"),
            ("deployment-contract", "deployment-contract.json"),
            ("installer-materials", "installer-materials.tar"),
        ):
            action_inputs[evidence_name] = {
                "subject_name": asset_name,
                "subject_sha256": (
                    "sha256:" + hashlib.sha256(assets[asset_name]).hexdigest()
                ),
                "workflow": provenance_workflow,
                "source_commit": provenance_commit,
            }
    except (KeyError, TypeError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RequestRejected("release manifest 权威输入不可读取") from error
    if (
        type(tag) is not str
        or type(tag_commit) is not str
        or not re.fullmatch(
            r"v[0-9]+\.[0-9]+\.[0-9]+(?:-(?:beta|rc)\.[1-9][0-9]*)?", tag
        )
        or not re.fullmatch(r"[0-9a-f]{40}", tag_commit)
        or set(action_inputs) != set(ACTIONS_EVIDENCE_NAMES.values())
        or any(
            set(item) != {"subject_name", "subject_sha256", "workflow", "source_commit"}
            or type(item["subject_name"]) is not str
            or type(item["subject_sha256"]) is not str
            or not _SHA256.fullmatch(item["subject_sha256"])
            or item["workflow"] not in {
                ".github/workflows/release.yml",
                ".github/workflows/promote-release.yml",
            }
            or type(item["source_commit"]) is not str
            or not re.fullmatch(r"[0-9a-f]{40}", item["source_commit"])
            for item in action_inputs.values()
        )
    ):
        raise RequestRejected("release manifest 权威输入未关闭")
    return {"tag": tag, "tag_commit": tag_commit, "actions": action_inputs}


def _authority_attestation(claim: ActionsProvenanceClaim) -> AttestationEvidence:
    return AttestationEvidence(
        subject_name=claim.subject_name,
        subject_digest=claim.subject_digest,
        repository=claim.repository,
        workflow=claim.workflow,
        certificate_identity=claim.certificate_identity,
        oidc_issuer=claim.oidc_issuer,
        source_commit=claim.source_commit,
        source_ref=claim.source_ref,
        signer_digest=claim.signer_digest,
        predicate_type=claim.predicate_type,
    )


def _bind_oci_to_release_manifest(
    index: Mapping[str, object], assets: Mapping[str, bytes]
) -> None:
    try:
        manifest = json.loads(assets["release-manifest.json"].decode("utf-8"))
        images = index["ociImages"]
        if not isinstance(images, list):
            raise TypeError
        expected = {
            role: (
                manifest["images"][role]["repository"],
                manifest["images"][role]["digest"],
            )
            for role in ("api", "postgres", "redis", "web")
        }
        observed = {
            item["role"]: (item["repository"], item["digest"])
            for item in images
            if isinstance(item, dict)
        }
    except (KeyError, TypeError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RequestRejected("OCI transport 与 release manifest 绑定不可读") from error
    if observed != expected:
        raise RequestRejected("OCI transport 与 release manifest 精确身份不一致")


def production_offline_release_verifier(
) -> OfflineReleaseVerifier | PersistentOfflineReleaseVerifier:
    """从固定系统路径装载生产信任；缺任一材料时保持 fail closed。"""

    try:
        from .trust_lifecycle import (
            TRUST_STATE_ROOT,
            ProductionTrustLifecycle,
            TrustLifecycleError,
        )

        lifecycle = ProductionTrustLifecycle.production()
        active = lifecycle.load_active()
        material = active.material
        profile_lineage = lifecycle.load_profile_lineage(active)
        external = SigstoreGoEvidenceVerifier(material)
    except (RequestRejected, TrustLifecycleError, ValueError):
        return OfflineReleaseVerifier(
            trust_profile=None,
            external_verifier=None,
            production_blocked_reason=(
                "生产离线发布验证器尚未冻结或不可用：预置信任 profile、官方 TUF roots "
                "或冻结 verifier 未完整安装"
            ),
        )
    inner = OfflineReleaseVerifier(
        trust_profile=material.profile,
        external_verifier=external,
        idempotent_reverification=True,
    )
    return PersistentOfflineReleaseVerifier(
        inner=inner,
        profile=material.profile,
        state_path=TRUST_STATE_ROOT / "release-authority-state.json",
        profile_lineage=profile_lineage,
    )


class PersistentOfflineReleaseVerifier:
    """串行、原子持久化离线接受状态；验证重入不重复激活。"""

    def __init__(
        self,
        *,
        inner: OfflineReleaseVerifier,
        profile: TrustProfile,
        state_path: Path,
        profile_lineage: frozenset[str] | None = None,
    ) -> None:
        self._inner = inner
        self._profile = profile
        self._state_path = Path(state_path)
        self._profile_lineage = (
            frozenset({profile.identity})
            if profile_lineage is None
            else frozenset(profile_lineage)
        )
        if profile.identity not in self._profile_lineage:
            raise ValueError("离线权威 profile lineage 未包含活动 profile")

    def verify(
        self,
        *,
        payload: Path,
        sidecar: Path,
        destination: Path,
        updater_version: str,
        state: OfflineAuthorityState | None = None,
        expected_rollback_version: str | None = None,
    ) -> VerifiedPortableRelease:
        if state is not None:
            raise RequestRejected("生产持久化验证器禁止调用方注入权威状态")
        parent = self._state_path.parent
        try:
            parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            metadata = parent.lstat()
        except OSError as error:
            raise RequestRejected("离线权威状态目录不可用") from error
        if parent.is_symlink() or not stat.S_ISDIR(metadata.st_mode):
            raise RequestRejected("离线权威状态目录类型无效")
        lock_path = self._state_path.with_suffix(self._state_path.suffix + ".lock")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        try:
            lock_descriptor = os.open(lock_path, flags, 0o600)
        except OSError as error:
            raise RequestRejected("离线权威状态被占用或需要崩溃恢复") from error
        try:
            os.close(lock_descriptor)
            durable = self._load_state()
            verified = self._inner.verify(
                payload=payload,
                sidecar=sidecar,
                destination=destination,
                updater_version=updater_version,
                state=durable,
                expected_rollback_version=expected_rollback_version,
            )
            if verified.next_state != durable:
                self._store_state(verified.next_state)
            return verified
        finally:
            try:
                lock_path.unlink(missing_ok=True)
            except OSError:
                pass

    def _load_state(self) -> OfflineAuthorityState:
        if not self._state_path.exists():
            return OfflineAuthorityState.initial(self._profile)
        value = _read_pretrusted_file(self._state_path, max_bytes=1024 * 1024)
        try:
            record = json.loads(value.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RequestRejected("离线权威耐久状态不可解析") from error
        if _canonical_json_bytes(record) != value:
            raise RequestRejected("离线权威耐久状态不是 canonical JSON")
        state = OfflineAuthorityState.from_record(record)
        if (
            state.active_profile_version != self._profile.profile_version
            or state.active_profile_identity != self._profile.identity
        ):
            if (
                state.active_profile_identity not in self._profile_lineage
                or state.active_profile_version >= self._profile.profile_version
            ):
                raise RequestRejected("离线权威耐久状态与预置信任 profile 不一致")
            state = replace(
                state,
                generation=state.generation + 1,
                active_profile_version=self._profile.profile_version,
                active_profile_identity=self._profile.identity,
            )
            self._store_state(state)
        return state

    def _store_state(self, state: OfflineAuthorityState) -> None:
        value = _canonical_json_bytes(state.as_record())
        temporary = self._state_path.with_suffix(self._state_path.suffix + ".new")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        try:
            descriptor = os.open(temporary, flags, 0o600)
            with os.fdopen(descriptor, "wb", closefd=True) as output:
                output.write(value)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self._state_path)
            if os.name != "nt":
                directory = os.open(self._state_path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
        except OSError as error:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            raise RequestRejected("离线权威耐久状态提交失败") from error
