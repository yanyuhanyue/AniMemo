from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Protocol

from packaging.version import InvalidVersion, Version

from release.portable import (
    PortableBundleError,
    inspect_portable_archive,
    stage_portable_payload,
    validate_portable_bundle,
)
from release.publication_evidence import (
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
    verifier_id: str
    minimum_verifier_version: str
    revocation_epoch: int
    revocation_snapshot_sha256: str

    def __post_init__(self) -> None:
        if type(self.profile_version) is not int or self.profile_version < 1:
            raise ValueError("信任 profile 版本无效")
        if self.profile_version == 1:
            if self.parent_profile_identity is not None:
                raise ValueError("首个信任 profile 不得声明父身份")
        else:
            _require_digest(
                self.parent_profile_identity,
                label="父信任 profile 身份",  # type: ignore[arg-type]
            )
        for value, label in (
            (self.github_trusted_root_sha256, "GitHub 信任根"),
            (self.sigstore_trusted_root_sha256, "Sigstore 信任根"),
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

    @property
    def identity(self) -> str:
        return _canonical_digest(
            {
                "schema": "animemo.offline-trust-profile/v1",
                "profileVersion": self.profile_version,
                "parentProfileIdentity": self.parent_profile_identity,
                "repository": self.repository,
                "repositoryId": self.repository_id,
                "ownerId": self.owner_id,
                "githubReleaseCertificateIdentity": (
                    self.github_release_certificate_identity
                ),
                "githubTrustedRootSha256": self.github_trusted_root_sha256,
                "sigstoreTrustedRootSha256": self.sigstore_trusted_root_sha256,
                "verifierId": self.verifier_id,
                "minimumVerifierVersion": self.minimum_verifier_version,
                "revocationEpoch": self.revocation_epoch,
                "revocationSnapshotSha256": self.revocation_snapshot_sha256,
            }
        )


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


class ExternalEvidenceVerifier(Protocol):
    """冻结的外部密码学验证器；AniMemo 只消费其 closed claims。"""

    verifier_id: str
    verifier_version: str

    def verify_github_release(
        self, *, bundle: bytes, trust_profile: TrustProfile
    ) -> Mapping[str, object]: ...

    def verify_actions_provenance(
        self,
        *,
        bundle: bytes,
        evidence_name: str,
        trust_profile: TrustProfile,
    ) -> Mapping[str, object]: ...

    def verify_trust_update(
        self,
        *,
        bundle: bytes,
        current_profile: TrustProfile,
        successor_profile: TrustProfile,
    ) -> Mapping[str, object]: ...


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
    authenticity_status: str = "AUTHENTIC_AS_OF_SIGNED_EVIDENCE"
    revocation_status: str = "OFFLINE_FUTURE_REVOCATION_UNKNOWN"


class OfflineReleaseVerifier:
    def __init__(
        self,
        *,
        trust_profile: TrustProfile | None,
        external_verifier: ExternalEvidenceVerifier | None = None,
        production_blocked_reason: str | None = None,
        oci_verifier: Callable[[Path, object], VerifiedOCIImageSet] = (
            verify_oci_image_set
        ),
    ) -> None:
        self._profile = trust_profile
        self._external = external_verifier
        self._production_blocked_reason = production_blocked_reason
        self._oci_verifier = oci_verifier

    def verify(
        self,
        *,
        payload: Path,
        sidecar: Path,
        destination: Path,
        updater_version: str,
        state: OfflineAuthorityState | None = None,
    ) -> VerifiedPortableRelease:
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
            external_release = self._external.verify_github_release(
                bundle=sidecar_bundle,
                trust_profile=self._profile,
            )
            publication = close_github_release_publication(external_release)
        except (PortableBundleError, PublicationEvidenceError) as error:
            raise RequestRejected("Portable 发布权威证明无效") from error
        except Exception as error:
            raise RequestRejected("GitHub Immutable Release 外部验证失败") from error

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
            _bind_release_assets(publication.assets, assets)
            claims = tuple(
                self._verified_actions_claim(sidecar_bundle, evidence_name)
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
            )
        except RequestRejected:
            raise
        except (OSError, PortableBundleError, OCIContractError, ValueError) as error:
            raise RequestRejected("Portable release 编排验证失败") from error
        finally:
            if created_staging is not None:
                shutil.rmtree(created_staging, ignore_errors=True)

    def _verified_actions_claim(
        self, sidecar_bundle: bytes, evidence_name: str
    ) -> ActionsProvenanceClaim:
        assert self._external is not None and self._profile is not None
        try:
            normalized = self._external.verify_actions_provenance(
                bundle=sidecar_bundle,
                evidence_name=evidence_name,
                trust_profile=self._profile,
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


def production_offline_release_verifier() -> OfflineReleaseVerifier:
    """返回 fail-closed 生产门，直至官方 proof/profile/verifier 均冻结。"""

    return OfflineReleaseVerifier(
        trust_profile=None,
        external_verifier=None,
        production_blocked_reason=(
            "生产离线发布验证器尚未冻结：GitHub Immutable Releases 当前未启用，"
            "不存在可验证的正式 release proof"
        ),
    )
