from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from updater.oci import (
    OCI_CONFIG_MEDIA_TYPE,
    OCI_IMAGE_INDEX_MEDIA_TYPE,
    OCI_IMAGE_MANIFEST_MEDIA_TYPE,
    OCI_LAYER_MEDIA_TYPE,
    REQUIRED_IMAGE_REPOSITORIES,
    OCIContractError,
    import_verified_oci_image_set,
    verify_oci_image_set,
)


def digest(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def canonical(value: object) -> bytes:
    return json.dumps(value, separators=(",", ":"), sort_keys=True).encode("ascii")


def write_blob(root: Path, value: bytes) -> tuple[str, int]:
    identity = digest(value)
    target = root / "blobs" / "sha256" / identity[7:]
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(value)
    return identity, len(value)


def write_layout(root: Path, role: str) -> dict[str, object]:
    root.mkdir(parents=True)
    (root / "oci-layout").write_bytes(canonical({"imageLayoutVersion": "1.0.0"}))
    config_digest, config_size = write_blob(
        root,
        canonical(
            {
                "architecture": "amd64",
                "os": "linux",
                "rootfs": {"diff_ids": [], "type": "layers"},
            }
        ),
    )
    layer_digest, layer_size = write_blob(root, ("layer:" + role).encode("ascii"))
    manifest_digest, manifest_size = write_blob(
        root,
        canonical(
            {
                "config": {
                    "digest": config_digest,
                    "mediaType": OCI_CONFIG_MEDIA_TYPE,
                    "size": config_size,
                },
                "layers": [
                    {
                        "digest": layer_digest,
                        "mediaType": OCI_LAYER_MEDIA_TYPE,
                        "size": layer_size,
                    }
                ],
                "mediaType": OCI_IMAGE_MANIFEST_MEDIA_TYPE,
                "schemaVersion": 2,
            }
        ),
    )
    (root / "index.json").write_bytes(
        canonical(
            {
                "manifests": [
                    {
                        "digest": manifest_digest,
                        "mediaType": OCI_IMAGE_MANIFEST_MEDIA_TYPE,
                        "platform": {"architecture": "amd64", "os": "linux"},
                        "size": manifest_size,
                    }
                ],
                "mediaType": OCI_IMAGE_INDEX_MEDIA_TYPE,
                "schemaVersion": 2,
            }
        )
    )
    return {
        "digest": manifest_digest,
        "layoutPath": f"oci/{role}",
        "platform": "linux/amd64",
        "repository": REQUIRED_IMAGE_REPOSITORIES[role],
        "role": role,
    }


class ExactImporter:
    def __init__(self) -> None:
        self.references: list[str] = []

    def import_verified_image(self, image) -> str:
        reference = f"{image.repository}@{image.digest}"
        self.references.append(reference)
        return reference


class OCIOfflineTests(unittest.TestCase):
    def test_verification_streams_blobs_without_path_read_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            images = [
                write_layout(root / "oci" / role, role)
                for role in sorted(REQUIRED_IMAGE_REPOSITORIES)
            ]

            original = Path.read_bytes

            def forbidden_read_bytes(path: Path) -> bytes:
                raise AssertionError(f"non-streaming read forbidden: {path}")

            Path.read_bytes = forbidden_read_bytes
            try:
                verified = verify_oci_image_set(root, images)
            finally:
                Path.read_bytes = original

            self.assertEqual(len(verified.images), 4)

    def test_local_import_seam_requires_verified_exact_digest_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            images = [
                write_layout(root / "oci" / role, role)
                for role in sorted(REQUIRED_IMAGE_REPOSITORIES)
            ]
            verified = verify_oci_image_set(root, images)
            importer = ExactImporter()

            receipt = import_verified_oci_image_set(verified, importer)

            self.assertEqual(len(receipt.images), 4)
            self.assertEqual(tuple(importer.references), receipt.images)
            with self.assertRaisesRegex(OCIContractError, "IMAGE_SET_NOT_VERIFIED"):
                import_verified_oci_image_set(images, importer)

            class TagImporter:
                def import_verified_image(self, image) -> str:
                    return f"{image.repository}:latest"

            with self.assertRaisesRegex(
                OCIContractError, "LOCAL_IMPORT_DIGEST_MISMATCH"
            ):
                import_verified_oci_image_set(verified, TagImporter())

    def test_local_import_revalidates_layout_immediately_before_runtime_boundary(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            images = [
                write_layout(root / "oci" / role, role)
                for role in sorted(REQUIRED_IMAGE_REPOSITORIES)
            ]
            verified = verify_oci_image_set(root, images)
            layer = root / "oci" / "api" / "blobs" / "sha256" / digest(b"layer:api")[7:]
            layer.write_bytes(b"tampr:api")
            importer = ExactImporter()

            with self.assertRaisesRegex(OCIContractError, "DIGEST_MISMATCH"):
                import_verified_oci_image_set(verified, importer)

            self.assertEqual(importer.references, [])


if __name__ == "__main__":
    unittest.main()
