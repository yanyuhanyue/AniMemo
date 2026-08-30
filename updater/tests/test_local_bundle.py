from __future__ import annotations

import hashlib
import json
import socket
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from release.materials import VerifiedMaterialSet
from updater.authority import VerifiedReleaseMaterials
from updater.local_bundle import (
    LocalBundleError,
    LocalBundleReleaseSource,
    LocalBundleTransport,
)
from updater.oci import VerifiedOCIImage, VerifiedOCIImageSet
from updater.tests.test_source import stable_manifest


def digest(character: str) -> str:
    return "sha256:" + character * 64


def images(root: Path, manifest: dict[str, object]) -> VerifiedOCIImageSet:
    return VerifiedOCIImageSet(
        tuple(
            VerifiedOCIImage(
                role=role,
                repository=manifest["images"][role]["repository"],
                digest=manifest["images"][role]["digest"],
                platform="linux/amd64",
                layout=root / "oci" / role,
                config_digest=digest(str(index + 1)),
                layer_digests=(digest(chr(ord("a") + index)),),
            )
            for index, role in enumerate(("api", "postgres", "redis", "web"))
        )
    )


class LocalBundleTests(unittest.TestCase):
    def test_source_accepts_the_persistent_production_verifier_type(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = root / "payload.tar"
            sidecar = root / "proof.json"
            payload.write_bytes(b"portable payload")
            sidecar.write_bytes(b"immutable release proof")
            manifest = stable_manifest()
            materials = VerifiedReleaseMaterials(
                manifest=manifest,
                deployment_contract={},
                verified=VerifiedMaterialSet(
                    root=root / "materials",
                    archive_sha256=digest("f"),
                    files=(),
                ),
                identity_digest=digest("e"),
            )
            fake_offline = types.ModuleType("updater.offline")

            class VerifiedPortableRelease:
                def __init__(self) -> None:
                    self.materials = materials
                    self.images = images(root, manifest)
                    self.payload_sha256 = digest("0")
                    self.authority_evidence = object()

            class OfflineReleaseVerifier:
                pass

            class PersistentOfflineReleaseVerifier:
                def verify(self, **_kwargs):
                    value = VerifiedPortableRelease()
                    value.payload_sha256 = "sha256:" + __import__("hashlib").sha256(
                        b"portable payload"
                    ).hexdigest()
                    return value

            fake_offline.VerifiedPortableRelease = VerifiedPortableRelease
            fake_offline.OfflineReleaseVerifier = OfflineReleaseVerifier
            fake_offline.PersistentOfflineReleaseVerifier = (
                PersistentOfflineReleaseVerifier
            )

            with mock.patch.dict(sys.modules, {"updater.offline": fake_offline}):
                source = LocalBundleReleaseSource.from_media(
                    payload=payload,
                    release_attestation=sidecar,
                    cache_root=root / "cache",
                    verifier=PersistentOfflineReleaseVerifier(),
                    updater_version="1.1.0",
                )

            self.assertIs(
                source.fetch_verified_materials(
                    "v1.0.0", updater_version="1.1.0"
                ),
                materials,
            )
    def test_source_verifies_private_staged_bytes_and_never_rereads_original_media(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media = root / "media"
            cache = root / "cache"
            media.mkdir()
            payload = media / "payload.tar"
            sidecar = media / "release-attestation.sigstore.json"
            payload.write_bytes(b"portable payload")
            sidecar.write_bytes(b"immutable release proof")
            manifest = stable_manifest()
            materials = VerifiedReleaseMaterials(
                manifest=manifest,
                deployment_contract={},
                verified=VerifiedMaterialSet(
                    root=root / "materials",
                    archive_sha256=digest("f"),
                    files=(),
                ),
                identity_digest=digest("e"),
            )

            fake_offline = types.ModuleType("updater.offline")

            class VerifiedPortableRelease:
                def __init__(self) -> None:
                    self.materials = materials
                    self.images = images(root, manifest)
                    self.payload_sha256 = "sha256:0d6fd443e2c97b4515030b925865149ecfc6102c1a9b044049c5818f3d570702"
                    self.authority_evidence = object()

            class OfflineReleaseVerifier:
                def __init__(self) -> None:
                    self.calls: list[tuple[Path, Path, bytes, bytes]] = []

                def verify(
                    self,
                    *,
                    payload,
                    sidecar,
                    destination,
                    updater_version,
                    expected_rollback_version=None,
                ):
                    self.assertions = (
                        destination,
                        updater_version,
                        expected_rollback_version,
                    )
                    self.calls.append(
                        (
                            payload,
                            sidecar,
                            payload.read_bytes(),
                            sidecar.read_bytes(),
                        )
                    )
                    return VerifiedPortableRelease()

            fake_offline.VerifiedPortableRelease = VerifiedPortableRelease
            fake_offline.OfflineReleaseVerifier = OfflineReleaseVerifier
            verifier = OfflineReleaseVerifier()
            with mock.patch.dict(sys.modules, {"updater.offline": fake_offline}):
                source = LocalBundleReleaseSource.from_media(
                    payload=payload,
                    release_attestation=sidecar,
                    cache_root=cache,
                    verifier=verifier,
                    updater_version="1.1.0",
                )
                payload.write_bytes(b"changed untrusted media")
                sidecar.write_bytes(b"changed untrusted proof")

                refreshed = source.fetch_verified_materials(
                    "v1.0.0",
                    updater_version="1.1.0",
                    refresh=True,
                )

            self.assertIs(refreshed, materials)
            self.assertEqual(len(verifier.calls), 2)
            for (
                staged_payload,
                staged_sidecar,
                payload_bytes,
                sidecar_bytes,
            ) in verifier.calls:
                self.assertNotEqual(staged_payload, payload)
                self.assertNotEqual(staged_sidecar, sidecar)
                self.assertEqual(payload_bytes, b"portable payload")
                self.assertEqual(sidecar_bytes, b"immutable release proof")

    def test_transport_rejects_links_and_non_regular_proof_material(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            staging = root / "staging"
            staging.mkdir(mode=0o700)
            payload = root / "payload.tar"
            payload.write_bytes(b"payload")
            target = root / "proof.json"
            target.write_bytes(b"proof")
            directory = root / "proof-directory"
            directory.mkdir()
            with self.assertRaisesRegex(LocalBundleError, "LOCAL_BUNDLE_PATH_UNSAFE"):
                LocalBundleTransport().acquire(
                    payload=payload,
                    release_attestation=directory,
                    private_staging=staging,
                )
            link = root / "proof-link.json"
            try:
                link.symlink_to(target)
            except OSError:
                self.skipTest("symbolic links unavailable")

            with self.assertRaisesRegex(LocalBundleError, "LOCAL_BUNDLE_PATH_UNSAFE"):
                LocalBundleTransport().acquire(
                    payload=payload,
                    release_attestation=link,
                    private_staging=staging,
                )

    def test_transport_has_no_network_or_fallback_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = root / "payload.tar"
            sidecar = root / "proof.json"
            staging = root / "staging"
            payload.write_bytes(b"payload")
            sidecar.write_bytes(b"proof")
            staging.mkdir(mode=0o700)
            transport = LocalBundleTransport()

            with (
                mock.patch.object(
                    socket,
                    "socket",
                    side_effect=AssertionError("network forbidden"),
                ),
                mock.patch.object(
                    socket,
                    "create_connection",
                    side_effect=AssertionError("network forbidden"),
                ),
            ):
                acquired = transport.acquire(
                    payload=payload,
                    release_attestation=sidecar,
                    private_staging=staging,
                )

            self.assertFalse(transport.policy.fallback_allowed)
            self.assertEqual(transport.policy.source, "local-bundle")
            self.assertEqual(
                acquired.material("portable-payload").read_bytes(), b"payload"
            )

    def test_structural_lookalike_without_offline_verifier_proof_is_rejected(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = root / "payload.tar"
            sidecar = root / "proof.json"
            payload.write_bytes(b"payload")
            sidecar.write_bytes(b"proof")

            fake_offline = types.ModuleType("updater.offline")

            class VerifiedPortableRelease:
                pass

            class OfflineReleaseVerifier:
                def verify(self, **_kwargs):
                    return types.SimpleNamespace(
                        materials=object(),
                        images=object(),
                        payload_sha256=digest("a"),
                        authority_evidence=object(),
                    )

            fake_offline.VerifiedPortableRelease = VerifiedPortableRelease
            fake_offline.OfflineReleaseVerifier = OfflineReleaseVerifier
            with (
                mock.patch.dict(sys.modules, {"updater.offline": fake_offline}),
                self.assertRaisesRegex(
                    LocalBundleError,
                    "LOCAL_BUNDLE_IMMUTABLE_PROOF_REQUIRED",
                ),
            ):
                LocalBundleReleaseSource.from_media(
                    payload=payload,
                    release_attestation=sidecar,
                    cache_root=root / "cache",
                    verifier=OfflineReleaseVerifier(),
                    updater_version="1.1.0",
                )

    def test_private_staging_can_be_reopened_by_exact_transport_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = root / "payload.tar"
            sidecar = root / "proof.json"
            payload.write_bytes(b"portable payload")
            sidecar.write_bytes(b"release proof")
            cache = root / "cache"
            manifest = stable_manifest()
            materials = VerifiedReleaseMaterials(
                manifest=manifest,
                deployment_contract={},
                verified=VerifiedMaterialSet(
                    root=root / "materials",
                    archive_sha256=digest("f"),
                    files=(),
                ),
                identity_digest=digest("e"),
            )

            fake_offline = types.ModuleType("updater.offline")

            class VerifiedPortableRelease:
                def __init__(self) -> None:
                    self.materials = materials
                    self.images = images(root, manifest)
                    self.payload_sha256 = (
                        "sha256:0d6fd443e2c97b4515030b925865149ecfc6102c1a9b044049c5818f3d570702"
                    )
                    self.authority_evidence = object()
                    self.release_attestation_identity = (
                        "sha256:"
                        + __import__("hashlib").sha256(b"release proof").hexdigest()
                    )
                    self.trust_profile_version = 1
                    self.trust_profile_identity = digest("4")
                    unsigned = {
                        "schema": "animemo.release-execution-receipt/v1",
                        "publicationIdentity": digest("5"),
                        "publicationExecutionReceiptIdentity": digest("6"),
                        "signedClaimIdentity": digest("7"),
                        "signedAt": "2026-08-30T00:00:00Z",
                    }
                    self.release_execution_receipt = {
                        **unsigned,
                        "identity": "sha256:"
                        + hashlib.sha256(
                            json.dumps(
                                unsigned,
                                ensure_ascii=False,
                                sort_keys=True,
                                separators=(",", ":"),
                            ).encode("utf-8")
                        ).hexdigest(),
                    }

            class OfflineReleaseVerifier:
                def verify(self, **_kwargs):
                    return VerifiedPortableRelease()

            fake_offline.VerifiedPortableRelease = VerifiedPortableRelease
            fake_offline.OfflineReleaseVerifier = OfflineReleaseVerifier
            with mock.patch.dict(sys.modules, {"updater.offline": fake_offline}):
                first = LocalBundleReleaseSource.from_media(
                    payload=payload,
                    release_attestation=sidecar,
                    cache_root=cache,
                    verifier=OfflineReleaseVerifier(),
                    updater_version="1.1.0",
                )
                payload.unlink()
                sidecar.unlink()
                reopened = LocalBundleReleaseSource.from_staged(
                    cache_root=cache,
                    transport_identity="sha256:" + first.receipt.identity,
                    verifier=OfflineReleaseVerifier(),
                    updater_version="1.1.0",
                )

            self.assertEqual(reopened.receipt, first.receipt)
            self.assertIs(
                reopened.fetch_verified_materials(
                    "v1.0.0", updater_version="1.1.0"
                ),
                materials,
            )
            binding = reopened.release_binding("v1.0.0")
            self.assertEqual(
                binding["transportIdentity"],
                "sha256:" + first.receipt.identity,
            )
            self.assertEqual(binding["trustProfileVersion"], 1)

    def test_reopen_rejects_tampered_private_transport_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = root / "payload.tar"
            sidecar = root / "proof.json"
            cache_root = root / "cache"
            cache_root.mkdir(mode=0o700)
            payload.write_bytes(b"payload")
            sidecar.write_bytes(b"proof")
            acquired = LocalBundleTransport().acquire(
                payload=payload,
                release_attestation=sidecar,
                private_staging=cache_root / "transport",
            )
            receipt_path = acquired.root / "transport-receipt.json"
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            receipt["identity"] = "0" * 64
            receipt_path.write_text(json.dumps(receipt), encoding="utf-8")

            fake_offline = types.ModuleType("updater.offline")
            fake_offline.VerifiedPortableRelease = type(
                "VerifiedPortableRelease", (), {}
            )
            fake_offline.OfflineReleaseVerifier = type(
                "OfflineReleaseVerifier", (), {"verify": lambda self, **kwargs: None}
            )
            with (
                mock.patch.dict(sys.modules, {"updater.offline": fake_offline}),
                self.assertRaisesRegex(
                    LocalBundleError, "LOCAL_BUNDLE_RECEIPT_INVALID"
                ),
            ):
                LocalBundleReleaseSource.from_staged(
                    cache_root=cache_root,
                    transport_identity="sha256:" + acquired.receipt.identity,
                    verifier=fake_offline.OfflineReleaseVerifier(),
                    updater_version="1.1.0",
                )

    def test_reopen_rejects_duplicate_transport_receipt_keys(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = root / "payload.tar"
            sidecar = root / "proof.json"
            cache_root = root / "cache"
            cache_root.mkdir(mode=0o700)
            payload.write_bytes(b"payload")
            sidecar.write_bytes(b"proof")
            acquired = LocalBundleTransport().acquire(
                payload=payload,
                release_attestation=sidecar,
                private_staging=cache_root / "transport",
            )
            receipt_path = acquired.root / "transport-receipt.json"
            encoded = receipt_path.read_text(encoding="ascii")
            receipt_path.write_text(
                '{"schema":"animemo.local-bundle-transport-receipt/v1",'
                + encoded[1:],
                encoding="ascii",
            )

            fake_offline = types.ModuleType("updater.offline")
            fake_offline.VerifiedPortableRelease = type(
                "VerifiedPortableRelease", (), {}
            )
            fake_offline.OfflineReleaseVerifier = type(
                "OfflineReleaseVerifier", (), {"verify": lambda self, **kwargs: None}
            )
            with (
                mock.patch.dict(sys.modules, {"updater.offline": fake_offline}),
                self.assertRaisesRegex(
                    LocalBundleError, "LOCAL_BUNDLE_RECEIPT_INVALID"
                ),
            ):
                LocalBundleReleaseSource.from_staged(
                    cache_root=cache_root,
                    transport_identity="sha256:" + acquired.receipt.identity,
                    verifier=fake_offline.OfflineReleaseVerifier(),
                    updater_version="1.1.0",
                )


if __name__ == "__main__":
    unittest.main()
