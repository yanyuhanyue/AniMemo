from __future__ import annotations

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

                def verify(self, *, payload, sidecar, destination, updater_version):
                    self.calls.append(
                        (
                            payload,
                            sidecar,
                            payload.read_bytes(),
                            sidecar.read_bytes(),
                        )
                    )
                    self.assertions = (destination, updater_version)
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


if __name__ == "__main__":
    unittest.main()
