from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from release.contract import (
    API_REPOSITORY,
    REPOSITORY,
    WEB_REPOSITORY,
    deployment_contract_digest,
    validate_deployment_contract,
    validate_manifest,
)
from release.materials import (
    MaterialFileIdentity,
    VerifiedMaterialSet,
    extract_installer_materials,
)

from .errors import RequestRejected

EXPECTED_RELEASE_ASSETS = frozenset(
    {
        "checksums.txt",
        "deployment-contract.json",
        "installer-materials.tar",
        "release-manifest.json",
    }
)
CHECKSUM_SUBJECTS = frozenset(EXPECTED_RELEASE_ASSETS - {"checksums.txt"})
OIDC_ISSUER = "https://token.actions.githubusercontent.com"
SOURCE_REF = "refs/heads/main"
PREDICATE_TYPE = "https://slsa.dev/provenance/v1"


@dataclass(frozen=True)
class VerifiedReleaseMaterials:
    manifest: dict[str, object]
    deployment_contract: dict[str, object]
    verified: VerifiedMaterialSet
    identity_digest: str
    attestation_execution_receipt: AttestationExecutionReceipt | None = None

    @property
    def root(self) -> Path:
        return self.verified.root

    @property
    def profile(self) -> str:
        return str(self.deployment_contract["profile"])

    def material(self, relative: str) -> Path:
        return self.verified.material(relative)

    def image(self, role: str) -> str:
        images = self.manifest["images"]
        if role not in {"api", "web", "postgres", "redis"}:
            raise RequestRejected("Release image role is invalid")
        image = images[role]
        return f"{image['repository']}@{image['digest']}"


@dataclass(frozen=True)
class ReleaseAssetEvidence:
    name: str
    state: str


@dataclass(frozen=True)
class AttestationEvidence:
    subject_name: str
    subject_digest: str
    repository: str
    workflow: str
    certificate_identity: str
    oidc_issuer: str
    logical_source_commit: str
    source_ref: str
    logical_signer_digest: str
    predicate_type: str


@dataclass(frozen=True)
class AttestationExecutionObservation:
    subject_name: str
    subject_digest: str
    workflow: str
    source_commit: str
    signer_digest: str


@dataclass(frozen=True)
class AttestationExecutionReceipt:
    schema: str
    observations: tuple[AttestationExecutionObservation, ...]
    identity: str


@dataclass(frozen=True)
class AuthorityEvidence:
    repository: str
    version: str
    draft: bool
    prerelease: bool
    tag_commit: str
    assets: tuple[ReleaseAssetEvidence, ...]
    attestations: tuple[AttestationEvidence, ...]


class ReleaseAuthorityVerifier:
    """Verify already-fetched release bytes at the common authority seam."""

    @staticmethod
    def _verify_asset_envelope(assets: Mapping[str, bytes]) -> None:
        if (
            not isinstance(assets, Mapping)
            or set(assets) != EXPECTED_RELEASE_ASSETS
            or any(type(name) is not str for name in assets)
            or any(type(value) is not bytes for value in assets.values())
        ):
            raise RequestRejected("Release assets differ from the fixed contract")
        try:
            lines = assets["checksums.txt"].decode("utf-8").splitlines()
        except UnicodeDecodeError as error:
            raise RequestRejected("Release checksums are unreadable") from error
        expected: dict[str, str] = {}
        for line in lines:
            digest, separator, name = line.partition("  ")
            if (
                separator != "  "
                or len(digest) != 64
                or name not in CHECKSUM_SUBJECTS
            ):
                raise RequestRejected(
                    "Release checksums contain an unexpected artifact"
                )
            if name in expected:
                raise RequestRejected("Release checksums contain a duplicate artifact")
            expected[name] = digest
        if set(expected) != CHECKSUM_SUBJECTS:
            raise RequestRejected(
                "Release checksums do not cover every release contract asset"
            )
        for name, digest in expected.items():
            if hashlib.sha256(assets[name]).hexdigest() != digest:
                raise RequestRejected(f"Release contract checksum mismatch: {name}")

    @staticmethod
    def _attestation(
        *,
        name: str,
        digest: str,
        workflow: str,
        source_commit: str,
    ) -> AttestationEvidence:
        return AttestationEvidence(
            subject_name=name,
            subject_digest=digest,
            repository=REPOSITORY,
            workflow=workflow,
            certificate_identity=(
                f"https://github.com/{REPOSITORY}/{workflow}@refs/heads/main"
            ),
            oidc_issuer=OIDC_ISSUER,
            logical_source_commit=source_commit,
            source_ref=SOURCE_REF,
            logical_signer_digest=source_commit,
            predicate_type=PREDICATE_TYPE,
        )

    @classmethod
    def _verify_authority(
        cls,
        authority: AuthorityEvidence,
        manifest: dict[str, object],
        assets: Mapping[str, bytes],
    ) -> None:
        if type(authority) is not AuthorityEvidence:
            raise RequestRejected("Release authority evidence type is invalid")
        release = manifest["release"]
        provenance = manifest["provenance"]
        if (
            type(authority.repository) is not str
            or authority.repository != REPOSITORY
            or type(authority.version) is not str
            or authority.version != release["version"]
            or type(authority.draft) is not bool
            or authority.draft is not False
            or type(authority.prerelease) is not bool
            or authority.prerelease != (release["channel"] != "stable")
            or type(authority.tag_commit) is not str
            or authority.tag_commit != release["commit"]
        ):
            raise RequestRejected("Exact GitHub release authority evidence is invalid")
        if type(authority.assets) is not tuple or any(
            type(item) is not ReleaseAssetEvidence
            or type(item.name) is not str
            or type(item.state) is not str
            or item.state != "uploaded"
            for item in authority.assets
        ):
            raise RequestRejected("Release asset authority evidence is invalid")
        evidence_names = [item.name for item in authority.assets]
        if (
            len(evidence_names) != len(EXPECTED_RELEASE_ASSETS)
            or len(evidence_names) != len(set(evidence_names))
            or set(evidence_names) != EXPECTED_RELEASE_ASSETS
        ):
            raise RequestRejected("Release asset authority evidence is invalid")

        release_commit = release["commit"]
        provenance_commit = provenance["sourceCommit"]
        provenance_workflow = provenance["workflow"]
        expected = (
            cls._attestation(
                name=API_REPOSITORY,
                digest=manifest["images"]["api"]["digest"],
                workflow=".github/workflows/release.yml",
                source_commit=release_commit,
            ),
            cls._attestation(
                name=WEB_REPOSITORY,
                digest=manifest["images"]["web"]["digest"],
                workflow=".github/workflows/release.yml",
                source_commit=release_commit,
            ),
            cls._attestation(
                name="release-manifest.json",
                digest="sha256:"
                + hashlib.sha256(assets["release-manifest.json"]).hexdigest(),
                workflow=provenance_workflow,
                source_commit=provenance_commit,
            ),
            cls._attestation(
                name="deployment-contract.json",
                digest="sha256:"
                + hashlib.sha256(assets["deployment-contract.json"]).hexdigest(),
                workflow=provenance_workflow,
                source_commit=provenance_commit,
            ),
            cls._attestation(
                name="installer-materials.tar",
                digest="sha256:"
                + hashlib.sha256(assets["installer-materials.tar"]).hexdigest(),
                workflow=provenance_workflow,
                source_commit=provenance_commit,
            ),
        )
        if (
            type(authority.attestations) is not tuple
            or any(type(item) is not AttestationEvidence for item in authority.attestations)
            or authority.attestations != expected
        ):
            raise RequestRejected("Release attestation authority evidence is invalid")

    def verify(
        self,
        *,
        assets: Mapping[str, bytes],
        authority: AuthorityEvidence,
        destination: Path,
        updater_version: str,
    ) -> VerifiedReleaseMaterials:
        self._verify_asset_envelope(assets)
        try:
            manifest = json.loads(assets["release-manifest.json"].decode("utf-8"))
            deployment_contract = json.loads(
                assets["deployment-contract.json"].decode("utf-8")
            )
            validate_manifest(manifest, updater_version=updater_version)
            with tempfile.TemporaryDirectory(prefix="animemo-release-authority-") as temp:
                archive_path = Path(temp) / "installer-materials.tar"
                with archive_path.open("xb") as handle:
                    handle.write(assets["installer-materials.tar"])
                archive_path.chmod(0o600)
                validate_deployment_contract(
                    deployment_contract,
                    installer_materials=archive_path,
                )
                if (
                    deployment_contract_digest(deployment_contract)
                    != manifest["deployment"]["contractSha256"]
                    or deployment_contract["files"]
                    != manifest["deployment"]["files"]
                    or deployment_contract["profile"]
                    != manifest["deployment"]["profile"]
                    or {
                        key: deployment_contract["archive"][key]
                        for key in ("name", "sha256", "format")
                    }
                    != manifest["deployment"]["installerMaterials"]
                ):
                    raise RequestRejected(
                        "Deployment contract differs from the release manifest"
                    )
                self._verify_authority(authority, manifest, assets)
                material_contract = {
                    "schemaVersion": deployment_contract["schemaVersion"],
                    "profile": deployment_contract["profile"],
                    "platform": deployment_contract["platform"],
                    "archive": deployment_contract["archive"],
                    "materials": deployment_contract["materials"],
                }
                destination = Path(destination).resolve()
                destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                material_files = tuple(
                    MaterialFileIdentity(
                        path=item["path"],
                        sha256=item["sha256"],
                        size=item["size"],
                        mode=int(item["mode"], 8),
                    )
                    for item in deployment_contract["materials"]
                )
                if destination.exists():
                    verified_set = VerifiedMaterialSet(
                        root=destination,
                        archive_sha256=deployment_contract["archive"]["sha256"],
                        files=material_files,
                    )
                    for identity in material_files:
                        verified_set.material(identity.path)
                else:
                    staging = Path(
                        tempfile.mkdtemp(
                            prefix=".authority-materials-",
                            dir=destination.parent,
                        )
                    )
                    staging_root = staging / "root"
                    try:
                        extracted = extract_installer_materials(
                            archive_path,
                            material_contract,
                            staging_root,
                        )
                        os.replace(staging_root, destination)
                        verified_set = VerifiedMaterialSet(
                            root=destination,
                            archive_sha256=extracted.archive_sha256,
                            files=extracted.files,
                        )
                    finally:
                        shutil.rmtree(staging, ignore_errors=True)
        except (KeyError, OSError, UnicodeDecodeError, ValueError) as error:
            raise RequestRejected("Release authority materials are invalid") from error

        identity_payload = {
            "manifest": manifest,
            "deploymentContract": deployment_contract,
        }
        identity_digest = "sha256:" + hashlib.sha256(
            json.dumps(
                identity_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        return VerifiedReleaseMaterials(
            manifest=manifest,
            deployment_contract=deployment_contract,
            verified=verified_set,
            identity_digest=identity_digest,
        )
