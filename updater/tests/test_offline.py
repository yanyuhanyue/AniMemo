from __future__ import annotations

import base64
import hashlib
import json
import subprocess
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest import mock

from release.contract import REPOSITORY
from release.portable import build_portable_payload, canonical_json_bytes
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
    OFFLINE_POLICY_IDENTITY,
    OfflineAuthorityState,
    OfflineReleaseVerifier,
    PersistentOfflineReleaseVerifier,
    PretrustedTrustMaterial,
    SigstoreGoEvidenceVerifier,
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
        "transportAssets": [
            {
                "authorityRole": "TRANSPORT_ONLY",
                "name": "animemo-v1.0.0-portable.tar",
                "role": "PORTABLE_RELEASE_BUNDLE",
                "sha256": _DIGEST_A,
                "size": 1,
            }
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

    archive = root / "animemo-v1.0.0-portable.tar"
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
    release_claim["transportAssets"] = [
        {
            "authorityRole": "TRANSPORT_ONLY",
            "name": archive.name,
            "role": "PORTABLE_RELEASE_BUNDLE",
            "sha256": "sha256:" + hashlib.sha256(archive.read_bytes()).hexdigest(),
            "size": archive.stat().st_size,
        }
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
        github_tuf_root_sha256=_DIGEST_A,
        github_tuf_root_version=1,
        github_tuf_timestamp_version=1,
        github_tuf_snapshot_version=1,
        github_tuf_targets_version=1,
        sigstore_tuf_root_sha256=_DIGEST_B,
        sigstore_tuf_root_version=1,
        sigstore_tuf_timestamp_version=1,
        sigstore_tuf_snapshot_version=1,
        sigstore_tuf_targets_version=1,
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

    def verify_github_release(
        self,
        *,
        bundle,
        trust_profile,
        tag=None,
        tag_commit=None,
        expected_subjects=None,
    ):
        self.release_call = (bundle, trust_profile.identity)
        return deepcopy(self.release_claim)

    def verify_actions_provenance(
        self,
        *,
        bundle,
        evidence_name,
        trust_profile,
        subject_name=None,
        subject_sha256=None,
        workflow=None,
        source_commit=None,
    ):
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
        github_tuf_root_sha256="sha256:" + "7" * 64,
        github_tuf_root_version=2,
        github_tuf_timestamp_version=2,
        github_tuf_snapshot_version=2,
        github_tuf_targets_version=2,
        sigstore_tuf_root_sha256="sha256:" + "8" * 64,
        sigstore_tuf_root_version=2,
        sigstore_tuf_timestamp_version=2,
        sigstore_tuf_snapshot_version=2,
        sigstore_tuf_targets_version=2,
        verifier_id=profile.verifier_id,
        minimum_verifier_version="1.1.0",
        revocation_epoch=2,
        revocation_snapshot_sha256="sha256:" + "6" * 64,
    )


def _pretrusted_material_fixture(root: Path) -> TrustProfile:
    github_root = b"production GitHub TUF-derived trusted root\n"
    sigstore_root = b"production Sigstore TUF-derived trusted root\n"
    github_tuf_root = b'{"signed":{"version":1},"signatures":[]}\n'
    sigstore_tuf_root = b'{"signed":{"version":1},"signatures":[]}\n'
    verifier = b"frozen sigstore-go release verifier binary"
    profile = TrustProfile(
        profile_version=1,
        parent_profile_identity=None,
        repository=REPOSITORY,
        repository_id="1327429673",
        owner_id="111261350",
        github_release_certificate_identity=(
            "https://dotcom.releases.github.com"
        ),
        github_trusted_root_sha256=(
            "sha256:" + hashlib.sha256(github_root).hexdigest()
        ),
        sigstore_trusted_root_sha256=(
            "sha256:" + hashlib.sha256(sigstore_root).hexdigest()
        ),
        github_tuf_root_sha256=(
            "sha256:" + hashlib.sha256(github_tuf_root).hexdigest()
        ),
        github_tuf_root_version=1,
        github_tuf_timestamp_version=1,
        github_tuf_snapshot_version=1,
        github_tuf_targets_version=1,
        sigstore_tuf_root_sha256=(
            "sha256:" + hashlib.sha256(sigstore_tuf_root).hexdigest()
        ),
        sigstore_tuf_root_version=1,
        sigstore_tuf_timestamp_version=1,
        sigstore_tuf_snapshot_version=1,
        sigstore_tuf_targets_version=1,
        verifier_id="github-sigstore-offline",
        minimum_verifier_version="2.97.0",
        revocation_epoch=1,
        revocation_snapshot_sha256=_DIGEST_C,
        verifier_identity="sha256:" + hashlib.sha256(verifier).hexdigest(),
        policy_identity=OFFLINE_POLICY_IDENTITY,
        activation_sequence=1,
    )
    root.mkdir()
    (root / "trust-profile.json").write_bytes(
        canonical_json_bytes(profile.as_bootstrap_record())
    )
    (root / "github-trusted-root.jsonl").write_bytes(github_root)
    (root / "sigstore-trusted-root.jsonl").write_bytes(sigstore_root)
    (root / "github-tuf-root.json").write_bytes(github_tuf_root)
    (root / "sigstore-tuf-root.json").write_bytes(sigstore_tuf_root)
    verifier_path = root / "offline-release-verifier"
    verifier_path.write_bytes(verifier)
    verifier_path.chmod(0o755)
    return profile


def _attestation_sidecar_fixture() -> bytes:
    bundle_set = canonical_json_bytes(
        [{"attestation": {"bundle": {"mediaType": "test-only-bundle"}}}]
    )
    record = {
        "encoding": "base64",
        "mediaType": "application/vnd.dev.sigstore.bundle-set+json",
        "sha256": "sha256:" + hashlib.sha256(bundle_set).hexdigest(),
        "size": len(bundle_set),
        "value": base64.b64encode(bundle_set).decode("ascii"),
    }
    return canonical_json_bytes(
        {
            "authorityRole": "TRANSPORT_ONLY",
            "commit": _COMMIT,
            "evidence": {
                name: deepcopy(record)
                for name in (
                    "api-image",
                    "deployment-contract",
                    "github-release",
                    "installer-materials",
                    "portable-asset",
                    "release-manifest",
                    "web-image",
                )
            },
            "offlineCryptographicVerificationRequired": True,
            "payload": {
                "name": "animemo-v1.0.0-portable.tar",
                "sha256": _DIGEST_A,
                "size": 1,
            },
            "repository": REPOSITORY,
            "resigned": False,
            "schema": "animemo.github-attestation-sidecar/v1",
            "selectionPolicy": "EXACT_EXPLICIT_TAG_NO_FALLBACK",
            "tag": "v1.0.0",
            "workflow": ".github/workflows/release.yml",
        }
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
        self.assertEqual(len(first.transport_assets), 1)
        self.assertEqual(first.transport_assets[0].authority_role, "TRANSPORT_ONLY")

        for mutation in (
            lambda value: value.update({"unexpectedTrustRoot": _DIGEST_A}),
            lambda value: value.update({"immutable": False}),
            lambda value: value["repository"].update({"name": "attacker/fork"}),
            lambda value: value["assets"].append(deepcopy(value["assets"][0])),
            lambda value: value.update({"transportAssets": []}),
            lambda value: value["transportAssets"].append(
                deepcopy(value["transportAssets"][0])
            ),
            lambda value: value["transportAssets"][0].update(
                {"name": "animemo-v1.0.0-lookalike-portable.tar"}
            ),
            lambda value: value["transportAssets"][0].update(
                {"authorityRole": "AUTHORITY"}
            ),
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


class PretrustedTrustMaterialTests(unittest.TestCase):
    def test_closed_pretrusted_material_loads_with_exact_profile_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "pretrusted-v1"
            expected = _pretrusted_material_fixture(root)

            material = PretrustedTrustMaterial.load(root)

            self.assertEqual(material.profile.identity, expected.identity)
            self.assertEqual(material.profile.profile_version, 1)
            self.assertEqual(material.profile.policy_identity, OFFLINE_POLICY_IDENTITY)
            self.assertEqual(material.verifier_path, root / "offline-release-verifier")

    def test_activated_successor_profile_reloads_with_exact_lineage(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "pretrusted-v1"
            current = _pretrusted_material_fixture(root)
            activated = TrustProfile(
                profile_version=2,
                parent_profile_identity=current.identity,
                repository=current.repository,
                repository_id=current.repository_id,
                owner_id=current.owner_id,
                github_release_certificate_identity=(
                    current.github_release_certificate_identity
                ),
                github_trusted_root_sha256=current.github_trusted_root_sha256,
                sigstore_trusted_root_sha256=(
                    current.sigstore_trusted_root_sha256
                ),
                github_tuf_root_sha256=current.github_tuf_root_sha256,
                github_tuf_root_version=current.github_tuf_root_version + 1,
                github_tuf_timestamp_version=current.github_tuf_timestamp_version + 1,
                github_tuf_snapshot_version=current.github_tuf_snapshot_version + 1,
                github_tuf_targets_version=current.github_tuf_targets_version + 1,
                sigstore_tuf_root_sha256=current.sigstore_tuf_root_sha256,
                sigstore_tuf_root_version=current.sigstore_tuf_root_version + 1,
                sigstore_tuf_timestamp_version=current.sigstore_tuf_timestamp_version + 1,
                sigstore_tuf_snapshot_version=current.sigstore_tuf_snapshot_version + 1,
                sigstore_tuf_targets_version=current.sigstore_tuf_targets_version + 1,
                verifier_id=current.verifier_id,
                minimum_verifier_version=current.minimum_verifier_version,
                revocation_epoch=2,
                revocation_snapshot_sha256=_DIGEST_C,
                verifier_identity=current.verifier_identity,
                policy_identity=current.policy_identity,
                activation_sequence=2,
            )
            (root / "trust-profile.json").write_bytes(
                canonical_json_bytes(activated.as_bootstrap_record())
            )

            material = PretrustedTrustMaterial.load(root)

            self.assertEqual(material.profile.identity, activated.identity)
            self.assertEqual(material.profile.profile_version, 2)
            self.assertEqual(material.profile.parent_profile_identity, current.identity)

    def test_unbound_successor_profile_cannot_be_loaded(self) -> None:
        current = _profile()
        bound = _successor(current)
        record = bound.as_bootstrap_record()
        record["parentProfileIdentity"] = None

        with self.assertRaisesRegex(RequestRejected, "预置信任 profile 身份字段无效"):
            TrustProfile.from_bootstrap_record(record)

    def test_tampered_or_open_ended_pretrusted_store_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "pretrusted-v1"
            _pretrusted_material_fixture(root)
            (root / "github-trusted-root.jsonl").write_bytes(b"tampered root")
            with self.assertRaisesRegex(RequestRejected, "预置信任材料身份不一致"):
                PretrustedTrustMaterial.load(root)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "pretrusted-v1"
            _pretrusted_material_fixture(root)
            (root / "bundle-supplied-root.json").write_bytes(b"untrusted")
            with self.assertRaisesRegex(RequestRejected, "预置信任目录未关闭"):
                PretrustedTrustMaterial.load(root)


class SigstoreGoEvidenceVerifierTests(unittest.TestCase):
    def test_tuf_update_uses_pretrusted_roots_and_closed_current_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "pretrusted-v1"
            profile = _pretrusted_material_fixture(root)
            material = PretrustedTrustMaterial.load(root)
            calls = []
            claim = {
                "authorityRole": "TRUST_METADATA_ONLY",
                "fromProfileIdentity": profile.identity,
                "github": {"tufRootVersion": 2},
                "schemaVersion": 1,
                "sigstore": {"tufRootVersion": 2},
            }

            def runner(command, **kwargs):
                request_path = Path(command[command.index("--request") + 1])
                calls.append((command, kwargs, json.loads(request_path.read_text())))
                return subprocess.CompletedProcess(
                    command,
                    0,
                    stdout=canonical_json_bytes(claim) + b"\n",
                    stderr=b"",
                )

            package = canonical_json_bytes(
                {
                    "authorityRole": "TRUST_METADATA_ONLY",
                    "fromProfileIdentity": profile.identity,
                    "github": {},
                    "schemaVersion": 1,
                    "sigstore": {},
                }
            )
            observed = SigstoreGoEvidenceVerifier(
                material,
                runner=runner,
            ).verify_tuf_update_package(
                package=package,
                current_profile=profile,
            )

            self.assertEqual(observed, claim)
            command, kwargs, request = calls[0]
            self.assertEqual(command[0], str(material.verifier_path))
            self.assertIn(str(material.github_tuf_root_path), command)
            self.assertIn(str(material.sigstore_tuf_root_path), command)
            self.assertEqual(request["fromProfileIdentity"], profile.identity)
            self.assertEqual(
                request["github"]["tufRootSha256"],
                profile.github_tuf_root_sha256,
            )
            self.assertEqual(set(kwargs["env"]) - {"SystemRoot"}, {"LANG", "LC_ALL"})

    def test_tuf_update_rejects_noncanonical_package_before_execution(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "pretrusted-v1"
            profile = _pretrusted_material_fixture(root)
            material = PretrustedTrustMaterial.load(root)
            verifier = SigstoreGoEvidenceVerifier(
                material,
                runner=mock.Mock(),
            )

            with self.assertRaisesRegex(RequestRejected, "canonical JSON"):
                verifier.verify_tuf_update_package(
                    package=b'{"schemaVersion": 1}\n',
                    current_profile=profile,
                )
            verifier._runner.assert_not_called()

    def test_structured_argv_uses_only_pretrusted_root_and_closed_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "pretrusted-v1"
            profile = _pretrusted_material_fixture(root)
            material = PretrustedTrustMaterial.load(root)
            calls = []

            def runner(command, **kwargs):
                calls.append((command, kwargs))
                return subprocess.CompletedProcess(
                    command,
                    0,
                    stdout=canonical_json_bytes(_release_claim()) + b"\n",
                    stderr=b"",
                )

            verifier = SigstoreGoEvidenceVerifier(material, runner=runner)
            claim = verifier.verify_github_release(
                bundle=_attestation_sidecar_fixture(),
                trust_profile=profile,
                tag="v1.0.0",
                tag_commit=_COMMIT,
                expected_subjects=[
                    {"name": "checksums.txt", "sha256": _DIGEST_A, "size": 1}
                ],
            )

            self.assertEqual(claim, _release_claim())
            command, kwargs = calls[0]
            self.assertIsInstance(command, tuple)
            self.assertEqual(command[0], str(material.verifier_path))
            self.assertNotIn("shell", kwargs)
            self.assertEqual(set(kwargs["env"]) - {"SystemRoot"}, {"LANG", "LC_ALL"})
            self.assertIn(str(material.github_trusted_root_path), command)

    def test_nonzero_or_stderr_external_result_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "pretrusted-v1"
            profile = _pretrusted_material_fixture(root)
            material = PretrustedTrustMaterial.load(root)

            def runner(command, **kwargs):
                return subprocess.CompletedProcess(
                    command, 1, stdout=b"", stderr="拒绝".encode()
                )

            verifier = SigstoreGoEvidenceVerifier(material, runner=runner)
            with self.assertRaisesRegex(RequestRejected, "外部验证器拒绝"):
                verifier.verify_github_release(
                    bundle=_attestation_sidecar_fixture(),
                    trust_profile=profile,
                    tag="v1.0.0",
                    tag_commit=_COMMIT,
                    expected_subjects=[
                        {"name": "checksums.txt", "sha256": _DIGEST_A, "size": 1}
                    ],
                )

class OfflineOrchestrationTests(unittest.TestCase):
    def test_persistent_state_migrates_only_across_verified_profile_lineage(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            current = _profile()
            successor = _successor(current)
            state_path = root / "offline-authority.json"
            state_path.write_bytes(
                canonical_json_bytes(OfflineAuthorityState.initial(current).as_record())
            )
            inner = mock.Mock()
            verifier = PersistentOfflineReleaseVerifier(
                inner=inner,
                profile=successor,
                state_path=state_path,
                profile_lineage=frozenset({current.identity, successor.identity}),
            )

            migrated = verifier._load_state()

            self.assertEqual(migrated.active_profile_identity, successor.identity)
            self.assertEqual(migrated.active_profile_version, successor.profile_version)
            self.assertEqual(
                json.loads(state_path.read_text())["activeProfileIdentity"],
                successor.identity,
            )

            unrelated = PersistentOfflineReleaseVerifier(
                inner=inner,
                profile=successor,
                state_path=state_path,
                profile_lineage=frozenset({successor.identity}),
            )
            state_path.write_bytes(
                canonical_json_bytes(OfflineAuthorityState.initial(current).as_record())
            )
            with self.assertRaisesRegex(RequestRejected, "profile 不一致"):
                unrelated._load_state()

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
            self.assertIn(
                verified.publication_identity,
                verified.next_state.accepted_publication_identities,
            )
            self.assertEqual(
                verified.release_attestation_identity,
                "sha256:4843cd2f1e501c0096158c10ac21a9495384f6ee39297a5d1d687fb34ea4c445",
            )
            self.assertEqual(verified.trust_profile_version, 1)
            self.assertEqual(verified.trust_profile_identity, profile.identity)
            self.assertRegex(
                verified.actions_evidence_identity,
                r"^sha256:[0-9a-f]{64}$",
            )
            self.assertEqual(
                verified.payload_sha256,
                "sha256:" + hashlib.sha256(payload.read_bytes()).hexdigest(),
            )
            self.assertTrue(
                all(image.layout.exists() for image in verified.images.images)
            )

    def test_persistent_verification_is_idempotent_without_reaccepting_release(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            payload, sidecar, release_claim, action_claims = _portable_fixture(root)
            profile = _profile()
            inner = OfflineReleaseVerifier(
                trust_profile=profile,
                external_verifier=_PublicationVerifier(release_claim, action_claims),
                oci_verifier=_qualified_oci_verifier,
                idempotent_reverification=True,
            )
            verifier = PersistentOfflineReleaseVerifier(
                inner=inner,
                profile=profile,
                state_path=root / "state" / "offline-authority.json",
            )

            first = verifier.verify(
                payload=payload,
                sidecar=sidecar,
                destination=root / "first-materials",
                updater_version="1.0.0",
            )
            second = verifier.verify(
                payload=payload,
                sidecar=sidecar,
                destination=root / "second-materials",
                updater_version="1.0.0",
            )

            self.assertEqual(first.next_state.generation, 1)
            self.assertEqual(second.next_state.generation, 1)
            self.assertEqual(first.publication_identity, second.publication_identity)

    def test_previously_accepted_release_requires_exact_rollback_version(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            payload, sidecar, release_claim, action_claims = _portable_fixture(root)
            profile = _profile()
            verifier = OfflineReleaseVerifier(
                trust_profile=profile,
                external_verifier=_PublicationVerifier(release_claim, action_claims),
                oci_verifier=_qualified_oci_verifier,
                idempotent_reverification=True,
            )
            accepted = verifier.verify(
                payload=payload,
                sidecar=sidecar,
                destination=root / "accepted-materials",
                updater_version="1.0.0",
                state=OfflineAuthorityState.initial(profile),
            )
            higher = accepted.next_state.accept_publication(
                profile=profile,
                publication_identity="sha256:" + "9" * 64,
                release_version="v1.0.1",
            )

            with self.assertRaisesRegex(RequestRejected, "重放"):
                verifier.verify(
                    payload=payload,
                    sidecar=sidecar,
                    destination=root / "regular-materials",
                    updater_version="1.0.0",
                    state=higher,
                )
            rolled_back = verifier.verify(
                payload=payload,
                sidecar=sidecar,
                destination=root / "rollback-materials",
                updater_version="1.0.0",
                state=higher,
                expected_rollback_version="v1.0.0",
            )

            self.assertEqual(rolled_back.next_state, higher)
            with self.assertRaisesRegex(RequestRejected, "重放"):
                verifier.verify(
                    payload=payload,
                    sidecar=sidecar,
                    destination=root / "wrong-rollback-materials",
                    updater_version="1.0.0",
                    state=higher,
                    expected_rollback_version="v0.9.0",
                )


if __name__ == "__main__":
    unittest.main()
