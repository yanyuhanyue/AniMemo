from __future__ import annotations

import base64
import hashlib
import io
import json
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from installer.bootstrap import BootstrapPrivilegeGate, commit_bootstrap_authorization
from updater.offline import OFFLINE_POLICY_IDENTITY, TrustProfile, _canonical_json_bytes
from updater.trust_lifecycle import (
    ProductionTrustLifecycle,
    TrustLifecycleError,
)


def _digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _kit(archive: Path) -> TrustProfile:
    github_key = "c" * 64
    sigstore_key = "d" * 64
    files = {
        "github-trusted-root.jsonl": b'{"mediaType":"github-root"}\n',
        "sigstore-trusted-root.jsonl": b'{"mediaType":"sigstore-root"}\n',
        "github-tuf-root.json": _canonical_json_bytes({
            "signed": {"version": 1, "roles": {"root": {"keyids": [github_key]}}},
            "signatures": [],
        }),
        "sigstore-tuf-root.json": _canonical_json_bytes({
            "signed": {"version": 1, "roles": {"root": {"keyids": [sigstore_key]}}},
            "signatures": [],
        }),
        "offline-release-verifier": b"qualified-linux-amd64-verifier",
    }
    profile = TrustProfile(
        profile_version=1,
        parent_profile_identity=None,
        repository="yanyuhanyue/AniMemo",
        repository_id="1327429673",
        owner_id="111261350",
        github_release_certificate_identity="https://dotcom.releases.github.com",
        github_trusted_root_sha256=_digest(files["github-trusted-root.jsonl"]),
        sigstore_trusted_root_sha256=_digest(files["sigstore-trusted-root.jsonl"]),
        github_tuf_root_sha256=_digest(files["github-tuf-root.json"]),
        github_tuf_root_version=1,
        github_tuf_timestamp_version=1,
        github_tuf_snapshot_version=1,
        github_tuf_targets_version=1,
        sigstore_tuf_root_sha256=_digest(files["sigstore-tuf-root.json"]),
        sigstore_tuf_root_version=1,
        sigstore_tuf_timestamp_version=1,
        sigstore_tuf_snapshot_version=1,
        sigstore_tuf_targets_version=1,
        verifier_id="github-sigstore-offline",
        minimum_verifier_version="2.97.0",
        revocation_epoch=1,
        revocation_snapshot_sha256="sha256:" + "3" * 64,
        verifier_identity=_digest(files["offline-release-verifier"]),
        policy_identity=OFFLINE_POLICY_IDENTITY,
        activation_sequence=1,
    )
    files["trust-profile.json"] = _canonical_json_bytes(profile.as_bootstrap_record())
    manifest = {
        "schemaVersion": 1,
        "authorityRole": "PRODUCTION_PRETRUST_ONLY",
        "releaseAuthority": "GITHUB_IMMUTABLE_RELEASE",
        "stage0Model": "GITHUB_IMMUTABLE_RELEASE_SIGSTORE_TUF_SINGLE_AUTHORITY",
        "profileIdentity": profile.identity,
        "files": [
            {
                "name": name,
                "mode": "0755" if name == "offline-release-verifier" else "0644",
                "sha256": _digest(data),
                "size": len(data),
            }
            for name, data in sorted(files.items())
        ],
    }
    files["initial-trust-bootstrap.json"] = _canonical_json_bytes(manifest)
    prefix = "release/release_attestation_verifier/pretrust-v2/"
    with tarfile.open(archive, "w", format=tarfile.USTAR_FORMAT) as bundle:
        for name, data in sorted(files.items()):
            info = tarfile.TarInfo(prefix + name)
            info.size = len(data)
            info.mode = 0o755 if name == "offline-release-verifier" else 0o644
            info.mtime = 0
            bundle.addfile(info, io.BytesIO(data))
    return profile


def _authorization(authority_root: Path, archive: Path):
    payload = {
        "schemaVersion": 1,
        "state": "PRIVILEGE_ALLOWED",
        "repository": "yanyuhanyue/AniMemo",
        "tag": "v1.1.0-rc.1",
        "releaseCommit": "1" * 40,
        "releaseAttestationIdentity": "sha256:" + "2" * 64,
        "installerMaterials": {
            "path": str(archive),
            "sha256": _digest(archive.read_bytes()),
            "size": archive.stat().st_size,
        },
        "stage0": {
            "model": "GITHUB_IMMUTABLE_RELEASE_SIGSTORE_TUF_SINGLE_AUTHORITY",
            "carrier": "GH_2_97_0_EXACT_FROM_OFFICIAL_SIGNED_APT",
            "verifierIdentity": "gh:2.97.0",
        },
        "verifiedAt": "2026-08-19T00:00:00Z",
    }
    with mock.patch("installer.bootstrap.BOOTSTRAP_AUTHORITY_ROOT", authority_root):
        commit_bootstrap_authorization(payload)
        return BootstrapPrivilegeGate().consume(
            version="v1.1.0-rc.1",
            release_commit="1" * 40,
        )


def _update_package(
    path: Path,
    current: TrustProfile,
    *,
    revoke_signers: bool = True,
    version: int = 2,
) -> tuple[bytes, bytes, dict]:
    github_successor_key = ("e" if revoke_signers else "c") * 64
    sigstore_successor_key = ("f" if revoke_signers else "d") * 64
    github_root = _canonical_json_bytes({
        "signed": {"version": version, "roles": {"root": {"keyids": [github_successor_key]}}},
        "signatures": [],
    })
    sigstore_root = _canonical_json_bytes({
        "signed": {"version": version, "roles": {"root": {"keyids": [sigstore_successor_key]}}},
        "signatures": [],
    })
    github_trusted = b'{"mediaType":"github-root-v2"}\n'
    sigstore_trusted = b'{"mediaType":"sigstore-root-v2"}\n'

    def track(root: bytes, trusted: bytes) -> dict[str, object]:
        encoded = lambda value: base64.b64encode(value).decode("ascii")
        return {
            "rootChain": [encoded(root)],
            "timestamp": encoded(_canonical_json_bytes({"signed": {"version": version}})),
            "snapshot": encoded(_canonical_json_bytes({"signed": {"version": version}})),
            "targets": encoded(_canonical_json_bytes({"signed": {"version": version}})),
            "trustedRoot": encoded(trusted),
        }

    payload = {
        "schemaVersion": 1,
        "authorityRole": "TRUST_METADATA_ONLY",
        "fromProfileIdentity": current.identity,
        "github": track(github_root, github_trusted),
        "sigstore": track(sigstore_root, sigstore_trusted),
    }
    raw = _canonical_json_bytes(payload)
    path.write_bytes(raw)
    claim = {
        "schemaVersion": 1,
        "authorityRole": "TRUST_METADATA_ONLY",
        "fromProfileIdentity": current.identity,
        "github": {
            "tufRootSha256": _digest(github_root),
            "tufRootVersion": version,
            "timestampVersion": version,
            "snapshotVersion": version,
            "targetsVersion": version,
            "trustedRootSha256": _digest(github_trusted),
            "supersededMaterialIdentities": sorted([
                current.github_trusted_root_sha256,
                current.github_tuf_root_sha256,
            ]),
            "revokedSignerKeyIds": ["c" * 64] if revoke_signers else [],
        },
        "sigstore": {
            "tufRootSha256": _digest(sigstore_root),
            "tufRootVersion": version,
            "timestampVersion": version,
            "snapshotVersion": version,
            "targetsVersion": version,
            "trustedRootSha256": _digest(sigstore_trusted),
            "supersededMaterialIdentities": sorted([
                current.sigstore_trusted_root_sha256,
                current.sigstore_tuf_root_sha256,
            ]),
            "revokedSignerKeyIds": ["d" * 64] if revoke_signers else [],
        },
    }
    return github_root, sigstore_root, claim


class _UpdateVerifier:
    def __init__(self, claim: dict[str, object]) -> None:
        self.claim = claim
        self.calls = 0

    def verify_tuf_update_package(self, *, package: bytes, current_profile: TrustProfile):
        self.calls += 1
        self.input = (package, current_profile.identity)
        return json.loads(json.dumps(self.claim))


class ProductionTrustLifecycleTests(unittest.TestCase):
    def test_tuf_successor_is_verified_derived_atomic_and_replay_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            authority_root = root / "authority"
            authority_root.mkdir()
            archive = authority_root / "installer-materials.tar"
            current = _kit(archive)
            authorization = _authorization(authority_root, archive)
            lifecycle = ProductionTrustLifecycle._for_test(root / "state")
            lifecycle.provision_initial(authorization)

            package = root / "trust-update.json"
            _, _, claim = _update_package(package, current)
            verifier = _UpdateVerifier(claim)
            lifecycle = ProductionTrustLifecycle._for_test(
                root / "state",
                verifier_factory=lambda _material: verifier,
            )
            first = lifecycle.import_update(package)
            second = lifecycle.import_update(package)
            active = lifecycle.load_active()

            self.assertEqual(first.commit_identity, second.commit_identity)
            self.assertEqual(active.profile.profile_version, 2)
            self.assertEqual(active.profile.parent_profile_identity, current.identity)
            self.assertEqual(active.profile.github_tuf_root_version, 2)
            self.assertEqual(active.profile.sigstore_tuf_root_version, 2)
            self.assertEqual(
                active.superseded_material_identities,
                frozenset(
                    {
                        current.github_trusted_root_sha256,
                        current.github_tuf_root_sha256,
                        current.sigstore_trusted_root_sha256,
                        current.sigstore_tuf_root_sha256,
                    }
                ),
            )
            self.assertEqual(
                active.revoked_signer_key_ids,
                frozenset({"c" * 64, "d" * 64}),
            )
            self.assertEqual(active.profile.revocation_epoch, 2)
            self.assertEqual(
                lifecycle.load_profile_lineage(active),
                frozenset({current.identity, active.profile.identity}),
            )
            self.assertEqual(verifier.calls, 1)

    def test_plain_tuf_supersession_does_not_claim_signer_revocation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            authority_root = root / "authority"
            authority_root.mkdir()
            archive = authority_root / "installer-materials.tar"
            current = _kit(archive)
            authorization = _authorization(authority_root, archive)
            lifecycle = ProductionTrustLifecycle._for_test(root / "state")
            lifecycle.provision_initial(authorization)
            package = root / "trust-update.json"
            _, _, claim = _update_package(
                package,
                current,
                revoke_signers=False,
            )
            lifecycle = ProductionTrustLifecycle._for_test(
                root / "state",
                verifier_factory=lambda _material: _UpdateVerifier(claim),
            )

            lifecycle.import_update(package)
            active = lifecycle.load_active()

            self.assertEqual(active.revoked_signer_key_ids, frozenset())
            self.assertEqual(
                active.profile.revocation_epoch,
                current.revocation_epoch,
            )
            self.assertNotEqual(active.superseded_material_identities, frozenset())

    def test_revoked_signer_reintroduction_is_rejected_before_any_state_write(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            authority_root = root / "authority"
            authority_root.mkdir()
            archive = authority_root / "installer-materials.tar"
            current = _kit(archive)
            authorization = _authorization(authority_root, archive)
            lifecycle = ProductionTrustLifecycle._for_test(root / "state")
            lifecycle.provision_initial(authorization)

            first_package = root / "trust-update-v2.json"
            _, _, first_claim = _update_package(first_package, current)
            lifecycle = ProductionTrustLifecycle._for_test(
                root / "state",
                verifier_factory=lambda _material: _UpdateVerifier(first_claim),
            )
            lifecycle.import_update(first_package)
            active = lifecycle.load_active()

            second_package = root / "trust-update-v3.json"
            _, _, second_claim = _update_package(
                second_package,
                active.profile,
                revoke_signers=False,
                version=3,
            )
            lifecycle = ProductionTrustLifecycle._for_test(
                root / "state",
                verifier_factory=lambda _material: _UpdateVerifier(second_claim),
            )
            state_path = root / "state" / "active-state.json"
            state_before = state_path.read_bytes()
            generations_before = {
                item.name for item in (root / "state" / "generations").iterdir()
            }

            with self.assertRaisesRegex(
                TrustLifecycleError,
                "TRUST_UPDATE_REVOKED_SIGNER_REINTRODUCED",
            ):
                lifecycle.import_update(second_package)

            self.assertEqual(state_path.read_bytes(), state_before)
            self.assertEqual(
                {item.name for item in (root / "state" / "generations").iterdir()},
                generations_before,
            )
            self.assertEqual(lifecycle.load_active().profile.identity, active.profile.identity)

    def test_superseded_material_reintroduction_is_rejected_before_any_state_write(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            authority_root = root / "authority"
            authority_root.mkdir()
            archive = authority_root / "installer-materials.tar"
            current = _kit(archive)
            authorization = _authorization(authority_root, archive)
            lifecycle = ProductionTrustLifecycle._for_test(root / "state")
            lifecycle.provision_initial(authorization)

            package = root / "trust-update.json"
            github_root, _, claim = _update_package(
                package,
                current,
                revoke_signers=False,
            )
            claim["github"]["supersededMaterialIdentities"] = sorted(
                set(claim["github"]["supersededMaterialIdentities"])
                | {_digest(github_root)}
            )
            lifecycle = ProductionTrustLifecycle._for_test(
                root / "state",
                verifier_factory=lambda _material: _UpdateVerifier(claim),
            )
            state_path = root / "state" / "active-state.json"
            state_before = state_path.read_bytes()
            generations_before = {
                item.name for item in (root / "state" / "generations").iterdir()
            }

            with self.assertRaisesRegex(
                TrustLifecycleError,
                "TRUST_UPDATE_SUPERSEDED_MATERIAL_REINTRODUCED",
            ):
                lifecycle.import_update(package)

            self.assertEqual(state_path.read_bytes(), state_before)
            self.assertEqual(
                {item.name for item in (root / "state" / "generations").iterdir()},
                generations_before,
            )
            self.assertEqual(lifecycle.load_active().profile.identity, current.identity)

    def test_tuf_rollback_claim_is_rejected_without_state_change(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            authority_root = root / "authority"
            authority_root.mkdir()
            archive = authority_root / "installer-materials.tar"
            current = _kit(archive)
            authorization = _authorization(authority_root, archive)
            lifecycle = ProductionTrustLifecycle._for_test(root / "state")
            lifecycle.provision_initial(authorization)
            package = root / "trust-update.json"
            _, _, claim = _update_package(package, current)
            claim["github"]["timestampVersion"] = 0
            lifecycle = ProductionTrustLifecycle._for_test(
                root / "state",
                verifier_factory=lambda _material: _UpdateVerifier(claim),
            )

            with self.assertRaisesRegex(
                TrustLifecycleError,
                "TRUST_UPDATE_CLAIM_INVALID",
            ):
                lifecycle.import_update(package)
            self.assertEqual(lifecycle.load_active().profile.identity, current.identity)

    def test_initial_provisioning_is_atomic_closed_and_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            authority_root = root / "authority"
            authority_root.mkdir()
            archive = authority_root / "installer-materials.tar"
            profile = _kit(archive)
            authorization = _authorization(authority_root, archive)
            lifecycle = ProductionTrustLifecycle._for_test(root / "state")

            first = lifecycle.provision_initial(authorization)
            second = lifecycle.provision_initial(authorization)
            active = lifecycle.load_active()

            self.assertEqual(first.commit_identity, second.commit_identity)
            self.assertEqual(active.profile.identity, profile.identity)
            self.assertEqual(active.profile.parent_profile_identity, None)
            self.assertEqual(active.generation, 1)
            self.assertEqual(
                {item.name for item in active.material.root.iterdir()},
                {
                    "github-trusted-root.jsonl",
                    "github-tuf-root.json",
                    "offline-release-verifier",
                    "sigstore-trusted-root.jsonl",
                    "sigstore-tuf-root.json",
                    "trust-profile.json",
                },
            )

    def test_bundle_root_without_authorized_capability_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lifecycle = ProductionTrustLifecycle._for_test(Path(directory) / "state")
            with self.assertRaisesRegex(
                TrustLifecycleError,
                "TRUST_INITIAL_BOOTSTRAP_AUTHORIZATION_INVALID",
            ):
                lifecycle.provision_initial(object())

    def test_second_different_initial_generation_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            authority_root = root / "authority"
            authority_root.mkdir()
            archive = authority_root / "installer-materials.tar"
            _kit(archive)
            authorization = _authorization(authority_root, archive)
            lifecycle = ProductionTrustLifecycle._for_test(root / "state")
            lifecycle.provision_initial(authorization)

            active = json.loads((root / "state" / "active-state.json").read_text())
            active["profileIdentity"] = "sha256:" + "9" * 64
            (root / "state" / "active-state.json").write_bytes(
                _canonical_json_bytes(active)
            )
            with self.assertRaisesRegex(
                TrustLifecycleError,
                "TRUST_ACTIVE_STATE_INVALID",
            ):
                lifecycle.load_active()


if __name__ == "__main__":
    unittest.main()
