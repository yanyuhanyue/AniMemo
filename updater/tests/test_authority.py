from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from release.contract import API_REPOSITORY, REPOSITORY, WEB_REPOSITORY
from updater.authority import (
    AttestationEvidence,
    AuthorityEvidence,
    ReleaseAssetEvidence,
    ReleaseAuthorityVerifier,
)
from updater.errors import RequestRejected
from updater.tests.test_source import (
    FAKE_DEPLOYMENT_CONTRACT,
    FAKE_MATERIAL_ARCHIVE,
    stable_manifest,
)


EXPECTED_IDENTITY = (
    "sha256:692640e41ceb2c77276d75eb84c317b8344552a3f841ee4ede34f6125216f295"
)
MATERIAL_PATH = "wheelhouse/qualified_dependency-1.0-py3-none-any.whl"


def _json_bytes(payload: object) -> bytes:
    return (
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")


def _fixture() -> tuple[dict[str, bytes], AuthorityEvidence]:
    manifest = stable_manifest()
    manifest_bytes = _json_bytes(manifest)
    deployment_bytes = _json_bytes(FAKE_DEPLOYMENT_CONTRACT)
    checksums = (
        f"{hashlib.sha256(manifest_bytes).hexdigest()}  release-manifest.json\n"
        f"{hashlib.sha256(deployment_bytes).hexdigest()}  deployment-contract.json\n"
        f"{hashlib.sha256(FAKE_MATERIAL_ARCHIVE).hexdigest()}  installer-materials.tar\n"
    ).encode("utf-8")
    assets = {
        "release-manifest.json": manifest_bytes,
        "deployment-contract.json": deployment_bytes,
        "installer-materials.tar": FAKE_MATERIAL_ARCHIVE,
        "checksums.txt": checksums,
    }
    workflow = manifest["provenance"]["workflow"]
    provenance_commit = manifest["provenance"]["sourceCommit"]
    release_commit = manifest["release"]["commit"]
    common = {
        "repository": REPOSITORY,
        "oidc_issuer": "https://token.actions.githubusercontent.com",
        "source_ref": "refs/heads/main",
        "predicate_type": "https://slsa.dev/provenance/v1",
    }

    def attestation(
        name: str, digest: str, producer_workflow: str, source_commit: str
    ) -> AttestationEvidence:
        return AttestationEvidence(
            subject_name=name,
            subject_digest=digest,
            workflow=producer_workflow,
            certificate_identity=(
                f"https://github.com/{REPOSITORY}/{producer_workflow}@refs/heads/main"
            ),
            source_commit=source_commit,
            signer_digest=source_commit,
            **common,
        )

    evidence = AuthorityEvidence(
        repository=REPOSITORY,
        version="v1.0.0",
        draft=False,
        prerelease=False,
        tag_commit=release_commit,
        assets=tuple(
            ReleaseAssetEvidence(name=name, state="uploaded")
            for name in sorted(assets)
        ),
        attestations=(
            attestation(
                API_REPOSITORY,
                manifest["images"]["api"]["digest"],
                ".github/workflows/release.yml",
                release_commit,
            ),
            attestation(
                WEB_REPOSITORY,
                manifest["images"]["web"]["digest"],
                ".github/workflows/release.yml",
                release_commit,
            ),
            attestation(
                "release-manifest.json",
                "sha256:" + hashlib.sha256(manifest_bytes).hexdigest(),
                workflow,
                provenance_commit,
            ),
            attestation(
                "deployment-contract.json",
                "sha256:" + hashlib.sha256(deployment_bytes).hexdigest(),
                workflow,
                provenance_commit,
            ),
            attestation(
                "installer-materials.tar",
                "sha256:" + hashlib.sha256(FAKE_MATERIAL_ARCHIVE).hexdigest(),
                workflow,
                provenance_commit,
            ),
        ),
    )
    return assets, evidence


def _replace_checked_asset(
    assets: dict[str, bytes], name: str, replacement: bytes
) -> None:
    assets[name] = replacement
    rewritten = []
    for line in assets["checksums.txt"].decode("utf-8").splitlines():
        _, _, subject = line.partition("  ")
        digest = hashlib.sha256(assets[subject]).hexdigest()
        rewritten.append(f"{digest}  {subject}\n")
    assets["checksums.txt"] = "".join(rewritten).encode("utf-8")


class ReleaseAuthorityVerifierTests(unittest.TestCase):
    def test_same_golden_bytes_produce_the_existing_verified_identity(self) -> None:
        assets, authority = _fixture()
        verifier = ReleaseAuthorityVerifier()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = verifier.verify(
                assets=assets,
                authority=authority,
                destination=root / "first",
                updater_version="1.0.0",
            )
            second = verifier.verify(
                assets=assets,
                authority=authority,
                destination=root / "second",
                updater_version="1.0.0",
            )

            self.assertEqual(first.identity_digest, EXPECTED_IDENTITY)
            self.assertEqual(second.identity_digest, EXPECTED_IDENTITY)
            self.assertEqual(first.manifest, second.manifest)
            self.assertEqual(first.deployment_contract, second.deployment_contract)
            self.assertEqual(first.material(MATERIAL_PATH).read_bytes(), b"qualified wheel bytes")
            self.assertEqual(second.material(MATERIAL_PATH).read_bytes(), b"qualified wheel bytes")

    def test_asset_set_and_checksum_envelope_are_closed(self) -> None:
        assets, authority = _fixture()

        def extra_asset(candidate: dict[str, bytes]) -> None:
            candidate["unexpected.bin"] = b"unexpected"

        def missing_asset(candidate: dict[str, bytes]) -> None:
            del candidate["checksums.txt"]

        def checksum_mismatch(candidate: dict[str, bytes]) -> None:
            candidate["release-manifest.json"] += b"\n"

        def duplicate_checksum(candidate: dict[str, bytes]) -> None:
            candidate["checksums.txt"] += candidate["checksums.txt"].splitlines(
                keepends=True
            )[0]

        def incomplete_checksums(candidate: dict[str, bytes]) -> None:
            candidate["checksums.txt"] = b"".join(
                candidate["checksums.txt"].splitlines(keepends=True)[:2]
            )

        mutations = {
            "extra_asset": extra_asset,
            "missing_asset": missing_asset,
            "checksum_mismatch": checksum_mismatch,
            "duplicate_checksum": duplicate_checksum,
            "incomplete_checksums": incomplete_checksums,
        }
        verifier = ReleaseAuthorityVerifier()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for index, (label, mutate) in enumerate(mutations.items()):
                with self.subTest(label=label):
                    candidate = dict(assets)
                    mutate(candidate)
                    with self.assertRaises(RequestRejected):
                        verifier.verify(
                            assets=candidate,
                            authority=authority,
                            destination=root / str(index),
                            updater_version="1.0.0",
                        )

    def test_manifest_and_deployment_contract_must_cross_bind(self) -> None:
        assets, authority = _fixture()
        manifest = json.loads(assets["release-manifest.json"])
        manifest["deployment"]["contractSha256"] = "sha256:" + "0" * 64
        _replace_checked_asset(
            assets,
            "release-manifest.json",
            _json_bytes(manifest),
        )

        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(RequestRejected):
                ReleaseAuthorityVerifier().verify(
                    assets=assets,
                    authority=authority,
                    destination=Path(temporary) / "materials",
                    updater_version="1.0.0",
                )

    def test_authority_evidence_is_an_exact_closed_binding(self) -> None:
        assets, authority = _fixture()

        def change_attestation(field: str, value: str) -> AuthorityEvidence:
            attestations = list(authority.attestations)
            attestations[0] = replace(attestations[0], **{field: value})
            return replace(authority, attestations=tuple(attestations))

        cases: dict[str, object] = {
            "wrong_type": object(),
            "wrong_repository": replace(authority, repository="other/repository"),
            "wrong_version": replace(authority, version="v1.0.1"),
            "draft": replace(authority, draft=True),
            "wrong_prerelease": replace(authority, prerelease=True),
            "wrong_tag_commit": replace(authority, tag_commit="9" * 40),
            "missing_release_asset": replace(authority, assets=authority.assets[:-1]),
            "non_uploaded_release_asset": replace(
                authority,
                assets=(replace(authority.assets[0], state="open"),)
                + authority.assets[1:],
            ),
            "missing_attestation": replace(
                authority, attestations=authority.attestations[:-1]
            ),
            "wrong_subject_digest": change_attestation(
                "subject_digest", "sha256:" + "f" * 64
            ),
            "wrong_attestation_repository": change_attestation(
                "repository", "other/repository"
            ),
            "wrong_workflow": change_attestation(
                "workflow", ".github/workflows/promote-release.yml"
            ),
            "wrong_certificate_identity": change_attestation(
                "certificate_identity", "https://example.invalid/identity"
            ),
            "wrong_oidc_issuer": change_attestation(
                "oidc_issuer", "https://example.invalid/issuer"
            ),
            "wrong_source_commit": change_attestation("source_commit", "8" * 40),
            "wrong_source_ref": change_attestation("source_ref", "refs/tags/v1.0.0"),
            "wrong_signer_digest": change_attestation("signer_digest", "7" * 40),
            "wrong_predicate": change_attestation(
                "predicate_type", "https://example.invalid/predicate"
            ),
        }
        verifier = ReleaseAuthorityVerifier()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for index, (label, candidate) in enumerate(cases.items()):
                with self.subTest(label=label):
                    with self.assertRaises(RequestRejected):
                        verifier.verify(
                            assets=assets,
                            authority=candidate,  # type: ignore[arg-type]
                            destination=root / str(index),
                            updater_version="1.0.0",
                        )
                    self.assertFalse((root / str(index)).exists())

    def test_manifest_deployment_and_material_failures_publish_no_materials(
        self,
    ) -> None:
        assets, authority = _fixture()

        def invalid_manifest(candidate: dict[str, bytes]) -> None:
            manifest = json.loads(candidate["release-manifest.json"])
            manifest["unexpected"] = True
            _replace_checked_asset(
                candidate, "release-manifest.json", _json_bytes(manifest)
            )

        def invalid_deployment(candidate: dict[str, bytes]) -> None:
            deployment = json.loads(candidate["deployment-contract.json"])
            deployment["unexpected"] = True
            _replace_checked_asset(
                candidate, "deployment-contract.json", _json_bytes(deployment)
            )

        def invalid_materials(candidate: dict[str, bytes]) -> None:
            _replace_checked_asset(
                candidate,
                "installer-materials.tar",
                candidate["installer-materials.tar"] + b"corrupt",
            )

        mutations = {
            "manifest_schema": invalid_manifest,
            "deployment_contract": invalid_deployment,
            "installer_materials": invalid_materials,
        }
        verifier = ReleaseAuthorityVerifier()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for index, (label, mutate) in enumerate(mutations.items()):
                with self.subTest(label=label):
                    candidate = dict(assets)
                    mutate(candidate)
                    destination = root / str(index)
                    with self.assertRaises(RequestRejected):
                        verifier.verify(
                            assets=candidate,
                            authority=authority,
                            destination=destination,
                            updater_version="1.0.0",
                        )
                    self.assertFalse(destination.exists())


if __name__ == "__main__":
    unittest.main()
