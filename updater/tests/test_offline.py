from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from release.contract import REPOSITORY
from release.portable import build_portable_payload
from release.publication_evidence import (
    ACTIONS_OIDC_ISSUER,
    GITHUB_RELEASE_CERTIFICATE_IDENTITY,
    GITHUB_RELEASE_PREDICATE_TYPE,
    PublicationEvidenceError,
    close_actions_provenance_claim,
    close_github_release_publication,
)
from updater.errors import RequestRejected
from updater.oci import VerifiedOCIImage, VerifiedOCIImageSet
from updater.offline import (
    ACTIONS_EVIDENCE_NAMES,
    OfflineAuthorityState,
    OfflineReleaseVerifier,
    TrustProfile,
    advance_trust_profile,
    production_offline_release_verifier,
)
from updater.tests.test_oci_offline import write_layout
from updater.tests.test_source import (
    FAKE_DEPLOYMENT_CONTRACT,
    FAKE_MATERIAL_ARCHIVE,
    stable_manifest,
)

_DIGEST_A = "sha256:" + "a" * 64
_DIGEST_B = "sha256:" + "b" * 64
_DIGEST_C = "sha256:" + "c" * 64
_COMMIT = "1" * 40


def _release_claim() -> dict[str, object]:
    return {
        "schemaVersion": 1,
        "predicateType": GITHUB_RELEASE_PREDICATE_TYPE,
        "immutable": True,
        "repository": {
            "name": REPOSITORY,
            "repositoryId": "1327429673",
            "ownerId": "111261350",
        },
        "tag": "v1.0.0",
        "tagCommit": _COMMIT,
        "draft": False,
        "prerelease": False,
        "signedAt": "2026-08-19T00:00:00Z",
        "certificate": {
            "identity": GITHUB_RELEASE_CERTIFICATE_IDENTITY,
            "issuerOrganization": "GitHub, Inc.",
        },
        "assets": [
            {"name": name, "sha256": "sha256:" + character * 64, "size": 1}
            for name, character in zip(
                (
                    "checksums.txt",
                    "deployment-contract.json",
                    "installer-materials.tar",
                    "release-manifest.json",
                ),
                "def0",
                strict=True,
            )
        ],
    }


def _actions_claim() -> dict[str, object]:
    workflow = ".github/workflows/release.yml"
    return {
        "schemaVersion": 1,
        "predicateType": "https://slsa.dev/provenance/v1",
        "subject": {"name": "release-manifest.json", "sha256": _DIGEST_A},
        "repository": {
            "name": REPOSITORY,
            "repositoryId": "1327429673",
            "ownerId": "111261350",
        },
        "workflow": workflow,
        "certificate": {
            "identity": (f"https://github.com/{REPOSITORY}/{workflow}@refs/heads/main"),
            "issuer": ACTIONS_OIDC_ISSUER,
        },
        "source": {"commit": _COMMIT, "ref": "refs/heads/main"},
        "signerDigest": _COMMIT,
    }


def _json_bytes(payload: object) -> bytes:
    return (
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode()


def _portable_fixture(root: Path):
    content_root = root / "content"
    manifest = stable_manifest()
    image_claims = []
    for role in ("api", "postgres", "redis", "web"):
        image = write_layout(content_root / "oci" / role, role)
        image["digest"] = manifest["images"][role]["digest"]
        image_claims.append(image)

    manifest_bytes = _json_bytes(manifest)
    deployment_bytes = _json_bytes(FAKE_DEPLOYMENT_CONTRACT)
    checksums = (
        f"{hashlib.sha256(manifest_bytes).hexdigest()}  release-manifest.json\n"
        f"{hashlib.sha256(deployment_bytes).hexdigest()}  deployment-contract.json\n"
        f"{hashlib.sha256(FAKE_MATERIAL_ARCHIVE).hexdigest()}  installer-materials.tar\n"
    ).encode()
    assets = {
        "checksums.txt": checksums,
        "deployment-contract.json": deployment_bytes,
        "installer-materials.tar": FAKE_MATERIAL_ARCHIVE,
        "release-manifest.json": manifest_bytes,
    }
    for name, value in assets.items():
        target = content_root / "authority" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(value)

    action_claims = {}
    release_commit = manifest["release"]["commit"]
    provenance_commit = manifest["provenance"]["sourceCommit"]
    provenance_workflow = manifest["provenance"]["workflow"]
    subjects = {
        "api": (
            manifest["images"]["api"]["repository"],
            manifest["images"]["api"]["digest"],
            ".github/workflows/release.yml",
            release_commit,
        ),
        "web": (
            manifest["images"]["web"]["repository"],
            manifest["images"]["web"]["digest"],
            ".github/workflows/release.yml",
            release_commit,
        ),
        "manifest": (
            "release-manifest.json",
            "sha256:" + hashlib.sha256(manifest_bytes).hexdigest(),
            provenance_workflow,
            provenance_commit,
        ),
        "deployment": (
            "deployment-contract.json",
            "sha256:" + hashlib.sha256(deployment_bytes).hexdigest(),
            provenance_workflow,
            provenance_commit,
        ),
        "materials": (
            "installer-materials.tar",
            "sha256:" + hashlib.sha256(FAKE_MATERIAL_ARCHIVE).hexdigest(),
            provenance_workflow,
            provenance_commit,
        ),
    }
    for label, evidence_name in ACTIONS_EVIDENCE_NAMES.items():
        subject_name, subject_digest, workflow, commit = subjects[label]
        claim = _actions_claim()
        claim["subject"] = {"name": subject_name, "sha256": subject_digest}
        claim["workflow"] = workflow
        claim["certificate"]["identity"] = (
            f"https://github.com/{REPOSITORY}/{workflow}@refs/heads/main"
        )
        claim["source"] = {"commit": commit, "ref": "refs/heads/main"}
        claim["signerDigest"] = commit
        action_claims[evidence_name] = claim

    archive = root / "portable.tar"
    build_portable_payload(content_root, archive, image_claims)

    release_claim = _release_claim()
    release_claim["tagCommit"] = release_commit
    release_claim["assets"] = [
        {
            "name": name,
            "sha256": "sha256:" + hashlib.sha256(value).hexdigest(),
            "size": len(value),
        }
        for name, value in assets.items()
    ]
    sidecar = root / "release-attestation.sigstore.json"
    sidecar.write_bytes(b"official-github-immutable-release-bundle")
    return archive, sidecar, release_claim, action_claims


def _profile() -> TrustProfile:
    return TrustProfile(
        profile_version=1,
        parent_profile_identity=None,
        repository=REPOSITORY,
        repository_id="1327429673",
        owner_id="111261350",
        github_release_certificate_identity=("https://dotcom.releases.github.com"),
        github_trusted_root_sha256=_DIGEST_A,
        sigstore_trusted_root_sha256=_DIGEST_B,
        verifier_id="github-sigstore-offline",
        minimum_verifier_version="1.0.0",
        revocation_epoch=1,
        revocation_snapshot_sha256=_DIGEST_C,
    )


class _TrustUpdateVerifier:
    verifier_id = "github-sigstore-offline"
    verifier_version = "1.0.0"

    def __init__(self, claim: dict[str, object]) -> None:
        self.claim = claim

    def verify_trust_update(self, *, bundle, current_profile, successor_profile):
        self.call = (bundle, current_profile, successor_profile)
        return deepcopy(self.claim)


class _PublicationVerifier:
    verifier_id = "github-sigstore-offline"
    verifier_version = "1.1.0"

    def __init__(self, release_claim, action_claims) -> None:
        self.release_claim = release_claim
        self.action_claims = action_claims

    def verify_github_release(self, *, bundle, trust_profile):
        self.release_call = (bundle, trust_profile.identity)
        return deepcopy(self.release_claim)

    def verify_actions_provenance(self, *, bundle, evidence_name, trust_profile):
        self.action_calls = getattr(self, "action_calls", []) + [
            (bundle, evidence_name, trust_profile.identity)
        ]
        return deepcopy(self.action_claims[evidence_name])


def _qualified_oci_verifier(root: Path, images) -> VerifiedOCIImageSet:
    return VerifiedOCIImageSet(
        tuple(
            VerifiedOCIImage(
                role=item["role"],
                repository=item["repository"],
                digest=item["digest"],
                platform=item["platform"],
                layout=root.joinpath(*item["layoutPath"].split("/")),
                config_digest=_DIGEST_B,
                layer_digests=(_DIGEST_C,),
            )
            for item in images
        )
    )


def _successor(profile: TrustProfile) -> TrustProfile:
    return TrustProfile(
        profile_version=2,
        parent_profile_identity=profile.identity,
        repository=profile.repository,
        repository_id=profile.repository_id,
        owner_id=profile.owner_id,
        github_release_certificate_identity=(
            profile.github_release_certificate_identity
        ),
        github_trusted_root_sha256="sha256:" + "4" * 64,
        sigstore_trusted_root_sha256="sha256:" + "5" * 64,
        verifier_id=profile.verifier_id,
        minimum_verifier_version="1.1.0",
        revocation_epoch=2,
        revocation_snapshot_sha256="sha256:" + "6" * 64,
    )


class OfflineProductionGateTests(unittest.TestCase):
    def test_missing_official_immutable_release_proof_fails_closed(self) -> None:
        profile = _profile()
        verifier = OfflineReleaseVerifier(trust_profile=profile)
        state = OfflineAuthorityState.initial(profile)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            payload = root / "payload.tar"
            payload.write_bytes("尚未验证的 portable payload".encode())
            with self.assertRaisesRegex(
                RequestRejected,
                "官方 GitHub Immutable Release 证明缺失",
            ):
                verifier.verify(
                    payload=payload,
                    sidecar=root / "missing-release-attestation.sigstore.json",
                    destination=root / "materials",
                    updater_version="1.0.0",
                    state=state,
                )


class ClosedPublicationClaimTests(unittest.TestCase):
    def test_github_release_claim_is_closed_and_identity_is_deterministic(self) -> None:
        first = close_github_release_publication(_release_claim())
        second = close_github_release_publication(deepcopy(_release_claim()))

        self.assertEqual(first, second)
        self.assertEqual(first.identity, second.identity)
        self.assertEqual(first.tag_commit, _COMMIT)
        self.assertEqual(len(first.assets), 4)

        for mutation in (
            lambda value: value.update({"unexpectedTrustRoot": _DIGEST_A}),
            lambda value: value.update({"immutable": False}),
            lambda value: value["repository"].update({"name": "attacker/fork"}),
            lambda value: value["assets"].append(deepcopy(value["assets"][0])),
        ):
            changed = deepcopy(_release_claim())
            mutation(changed)
            with self.assertRaises(PublicationEvidenceError):
                close_github_release_publication(changed)

    def test_actions_claim_is_closed_and_binds_repository_workflow_and_source(
        self,
    ) -> None:
        closed = close_actions_provenance_claim(_actions_claim())

        self.assertEqual(closed.source_commit, _COMMIT)
        self.assertEqual(closed.oidc_issuer, ACTIONS_OIDC_ISSUER)
        self.assertEqual(closed.subject_name, "release-manifest.json")

        for key, value in (
            ("workflow", ".github/workflows/lookalike.yml"),
            ("signerDigest", "2" * 40),
        ):
            changed = deepcopy(_actions_claim())
            changed[key] = value
            with self.assertRaises(PublicationEvidenceError):
                close_actions_provenance_claim(changed)

    def test_production_factory_is_explicitly_blocked_without_frozen_verifier(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            payload = root / "payload.tar"
            sidecar = root / "release-attestation.sigstore.json"
            payload.write_bytes("尚未验证的 portable payload".encode())
            sidecar.write_bytes("尚未验证的 immutable release proof".encode())

            with self.assertRaisesRegex(
                RequestRejected,
                "生产离线发布验证器尚未冻结",
            ):
                production_offline_release_verifier().verify(
                    payload=payload,
                    sidecar=sidecar,
                    destination=root / "materials",
                    updater_version="1.0.0",
                )


class OfflineDurableStateTests(unittest.TestCase):
    def test_replay_and_version_downgrade_are_rejected(self) -> None:
        profile = _profile()
        state = OfflineAuthorityState.initial(profile).accept_publication(
            profile=profile,
            publication_identity=_DIGEST_A,
            release_version="v1.0.0",
        )

        with self.assertRaisesRegex(RequestRejected, "发布证明重放"):
            state.accept_publication(
                profile=profile,
                publication_identity=_DIGEST_A,
                release_version="v1.0.0",
            )
        with self.assertRaisesRegex(RequestRejected, "发布版本降级或重放"):
            state.accept_publication(
                profile=profile,
                publication_identity=_DIGEST_B,
                release_version="v0.9.0",
            )

    def test_rotation_and_revocation_require_exact_external_claim(self) -> None:
        current = _profile()
        successor = _successor(current)
        state = OfflineAuthorityState.initial(current).accept_publication(
            profile=current,
            publication_identity=_DIGEST_A,
            release_version="v1.0.0",
        )
        claim = {
            "schemaVersion": 1,
            "fromProfileIdentity": current.identity,
            "toProfileIdentity": successor.identity,
            "fromVersion": 1,
            "toVersion": 2,
            "revocationEpoch": 2,
            "revocationSnapshotSha256": successor.revocation_snapshot_sha256,
            "revokedEvidenceIdentities": [_DIGEST_A],
        }
        verifier = _TrustUpdateVerifier(claim)

        rotated = advance_trust_profile(
            current_profile=current,
            successor_profile=successor,
            state=state,
            external_verifier=verifier,
            update_bundle="已验证的 trust update".encode(),
        )

        self.assertEqual(rotated.active_profile_identity, successor.identity)
        self.assertEqual(rotated.active_profile_version, 2)
        self.assertIn(_DIGEST_A, rotated.revoked_evidence_identities)
        self.assertGreater(rotated.generation, state.generation)

        changed = deepcopy(claim)
        changed["fromProfileIdentity"] = _DIGEST_B
        with self.assertRaisesRegex(RequestRejected, "信任更新 claim 绑定无效"):
            advance_trust_profile(
                current_profile=current,
                successor_profile=successor,
                state=state,
                external_verifier=_TrustUpdateVerifier(changed),
                update_bundle="错误绑定的 trust update".encode(),
            )


class OfflineOrchestrationTests(unittest.TestCase):
    def test_verified_official_claims_flow_through_common_authority_and_oci_seams(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            payload, sidecar, release_claim, action_claims = _portable_fixture(root)
            profile = _profile()
            external = _PublicationVerifier(release_claim, action_claims)

            verified = OfflineReleaseVerifier(
                trust_profile=profile,
                external_verifier=external,
                oci_verifier=_qualified_oci_verifier,
            ).verify(
                payload=payload,
                sidecar=sidecar,
                destination=root / "verified-materials",
                updater_version="1.0.0",
                state=OfflineAuthorityState.initial(profile),
            )

            self.assertEqual(
                verified.materials.manifest["release"]["version"], "v1.0.0"
            )
            self.assertEqual(len(verified.images.images), 4)
            self.assertEqual(len(external.action_calls), 5)
            self.assertEqual(verified.next_state.highest_release_version, "v1.0.0")
            self.assertEqual(
                verified.payload_sha256,
                "sha256:" + hashlib.sha256(payload.read_bytes()).hexdigest(),
            )
            self.assertTrue(
                all(image.layout.exists() for image in verified.images.images)
            )


if __name__ == "__main__":
    unittest.main()
