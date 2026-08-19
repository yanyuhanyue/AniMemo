from __future__ import annotations

import copy
import hashlib
import io
import json
import os
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from jsonschema import Draft202012Validator

from release.portable import (
    BLOCKED_PORTABLE_PUBLICATION_AUTHORITY,
    PortableAuthorityError,
    PortableBundleError,
    canonical_json_bytes,
    inspect_portable_archive,
    portable_publication_authority_gate,
    validate_portable_bundle,
)
from updater.oci import (
    OCI_CONFIG_MEDIA_TYPE,
    OCI_IMAGE_INDEX_MEDIA_TYPE,
    OCI_IMAGE_MANIFEST_MEDIA_TYPE,
    OCI_LAYER_MEDIA_TYPE,
    OCIContractError,
    REQUIRED_IMAGE_REPOSITORIES,
    plan_local_image_acquisition,
    verify_oci_image_set,
)


def digest(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def write_json_blob(root: Path, value: object) -> tuple[str, int]:
    payload = canonical_json_bytes(value)
    identity = digest(payload)
    target = root / "blobs" / "sha256" / identity[7:]
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(payload)
    return identity, len(payload)


def write_oci_layout(root: Path, role: str) -> dict[str, object]:
    root.mkdir(parents=True)
    (root / "oci-layout").write_bytes(
        canonical_json_bytes({"imageLayoutVersion": "1.0.0"})
    )
    config_digest, config_size = write_json_blob(
        root,
        {
            "architecture": "amd64",
            "os": "linux",
            "rootfs": {"diff_ids": [], "type": "layers"},
        },
    )
    layer = ("layer:" + role).encode("ascii")
    layer_digest = digest(layer)
    layer_target = root / "blobs" / "sha256" / layer_digest[7:]
    layer_target.write_bytes(layer)
    manifest_digest, manifest_size = write_json_blob(
        root,
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
                    "size": len(layer),
                }
            ],
            "mediaType": OCI_IMAGE_MANIFEST_MEDIA_TYPE,
            "schemaVersion": 2,
        },
    )
    (root / "index.json").write_bytes(
        canonical_json_bytes(
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


class PortableBundleTests(unittest.TestCase):
    def test_portable_index_schema_is_closed_and_cannot_embed_a_trust_root(self):
        schema_path = (
            Path(__file__).resolve().parents[2]
            / "release"
            / "portable-bundle.schema.json"
        )
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
        validator = Draft202012Validator(schema)
        valid = {
            "authorityState": BLOCKED_PORTABLE_PUBLICATION_AUTHORITY,
            "files": [],
            "ociImages": [],
            "profile": "animemo-portable-bundle-v1",
            "schemaVersion": 1,
        }
        self.assertEqual(list(validator.iter_errors(valid)), [])
        for field in ("trustRoot", "publicKey", "verifiedReleaseMaterials"):
            candidate = dict(valid)
            candidate[field] = "self-declared"
            with self.subTest(field=field):
                self.assertNotEqual(list(validator.iter_errors(candidate)), [])

    def test_closed_canonical_bundle_validates_without_creating_release_authority(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = b"immutable release manifest\n"
            target = root / "release" / "release-manifest.json"
            target.parent.mkdir(parents=True)
            target.write_bytes(payload)
            index = {
                "authorityState": BLOCKED_PORTABLE_PUBLICATION_AUTHORITY,
                "files": [
                    {
                        "path": "release/release-manifest.json",
                        "sha256": digest(payload),
                        "size": len(payload),
                    }
                ],
                "ociImages": [],
                "profile": "animemo-portable-bundle-v1",
                "schemaVersion": 1,
            }
            (root / "bundle-index.json").write_bytes(canonical_json_bytes(index))

            bundle = validate_portable_bundle(root)

            self.assertEqual(bundle.index, index)
            self.assertEqual(bundle.files[0].path, "release/release-manifest.json")
            self.assertEqual(
                portable_publication_authority_gate(bundle),
                BLOCKED_PORTABLE_PUBLICATION_AUTHORITY,
            )
            with self.assertRaisesRegex(
                PortableAuthorityError, "SELF_DECLARED_AUTHORITY_FORBIDDEN"
            ):
                portable_publication_authority_gate(
                    bundle, public_key="arbitrary-test-key"
                )
            with self.assertRaisesRegex(
                PortableAuthorityError, "SELF_DECLARED_AUTHORITY_FORBIDDEN"
            ):
                portable_publication_authority_gate(
                    bundle, trust_root={"sha256": "self-declared"}
                )

    def test_parser_rejects_noncanonical_json_and_unbound_oci_role_identity(self):
        base = {
            "authorityState": BLOCKED_PORTABLE_PUBLICATION_AUTHORITY,
            "files": [],
            "ociImages": [],
            "profile": "animemo-portable-bundle-v1",
            "schemaVersion": 1,
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "bundle-index.json").write_text(
                json.dumps(base, indent=2), encoding="utf-8"
            )
            with self.assertRaisesRegex(PortableBundleError, "INDEX_NOT_CANONICAL"):
                validate_portable_bundle(root)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            invalid = copy.deepcopy(base)
            invalid["ociImages"] = [
                {
                    "digest": "sha256:" + "a" * 64,
                    "layoutPath": "oci/api",
                    "platform": "linux/amd64",
                    "repository": "registry.invalid/self-selected",
                    "role": "api",
                }
            ]
            (root / "bundle-index.json").write_bytes(canonical_json_bytes(invalid))
            with self.assertRaisesRegex(PortableBundleError, "OCI_REPOSITORY_MISMATCH"):
                validate_portable_bundle(root)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            duplicate_key = (
                b'{"authorityState":"BLOCKED_PORTABLE_PUBLICATION_AUTHORITY",'
                b'"files":[],"ociImages":[],"profile":"animemo-portable-bundle-v1",'
                b'"schemaVersion":1,"schemaVersion":1}'
            )
            (root / "bundle-index.json").write_bytes(duplicate_key)
            with self.assertRaisesRegex(PortableBundleError, "DUPLICATE_KEY"):
                validate_portable_bundle(root)

    def test_valid_portable_tar_is_inspected_in_memory_without_extraction(self):
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            payload = b"closed portable payload"
            index = {
                "authorityState": BLOCKED_PORTABLE_PUBLICATION_AUTHORITY,
                "files": [
                    {"path": "release/payload", "sha256": digest(payload), "size": len(payload)}
                ],
                "ociImages": [],
                "profile": "animemo-portable-bundle-v1",
                "schemaVersion": 1,
            }
            archive = temporary / "portable.tar"
            with tarfile.open(archive, "w:", format=tarfile.USTAR_FORMAT) as handle:
                for name, value in (
                    ("bundle-index.json", canonical_json_bytes(index)),
                    ("release/payload", payload),
                ):
                    member = tarfile.TarInfo(name)
                    member.size = len(value)
                    handle.addfile(member, io.BytesIO(value))

            inspected = inspect_portable_archive(archive)

            self.assertEqual(inspected.index, index)
            self.assertEqual(inspected.files[0].sha256, digest(payload))
            self.assertEqual(inspected.archive_sha256, digest(archive.read_bytes()))
            self.assertFalse((temporary / "release").exists())

    def test_bundle_is_closed_and_rejects_traversal_duplicates_and_limits(self):
        cases = (
            ([{"path": "../escape", "sha256": digest(b"x"), "size": 1}], "PATH"),
            (
                [
                    {"path": "release/A", "sha256": digest(b"x"), "size": 1},
                    {"path": "release/a", "sha256": digest(b"x"), "size": 1},
                ],
                "CASE_COLLISION",
            ),
        )
        for files, message in cases:
            with self.subTest(message=message), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                index = {
                    "authorityState": BLOCKED_PORTABLE_PUBLICATION_AUTHORITY,
                    "files": files,
                    "ociImages": [],
                    "profile": "animemo-portable-bundle-v1",
                    "schemaVersion": 1,
                }
                (root / "bundle-index.json").write_bytes(canonical_json_bytes(index))
                with self.assertRaisesRegex(PortableBundleError, message):
                    validate_portable_bundle(root)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "unexpected").write_bytes(b"x")
            index = {
                "authorityState": BLOCKED_PORTABLE_PUBLICATION_AUTHORITY,
                "files": [],
                "ociImages": [],
                "profile": "animemo-portable-bundle-v1",
                "schemaVersion": 1,
            }
            (root / "bundle-index.json").write_bytes(canonical_json_bytes(index))
            with self.assertRaisesRegex(PortableBundleError, "LAYOUT_NOT_CLOSED"):
                validate_portable_bundle(root)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "undeclared-empty-directory").mkdir()
            index = {
                "authorityState": BLOCKED_PORTABLE_PUBLICATION_AUTHORITY,
                "files": [],
                "ociImages": [],
                "profile": "animemo-portable-bundle-v1",
                "schemaVersion": 1,
            }
            (root / "bundle-index.json").write_bytes(canonical_json_bytes(index))
            with self.assertRaisesRegex(PortableBundleError, "LAYOUT_NOT_CLOSED"):
                validate_portable_bundle(root)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = b"xx"
            (root / "payload").write_bytes(payload)
            index = {
                "authorityState": BLOCKED_PORTABLE_PUBLICATION_AUTHORITY,
                "files": [
                    {"path": "payload", "sha256": digest(payload), "size": 2}
                ],
                "ociImages": [],
                "profile": "animemo-portable-bundle-v1",
                "schemaVersion": 1,
            }
            (root / "bundle-index.json").write_bytes(canonical_json_bytes(index))
            with self.assertRaisesRegex(PortableBundleError, "FILE_SIZE_LIMIT"):
                validate_portable_bundle(root, max_file_bytes=1)
            with self.assertRaisesRegex(PortableBundleError, "TOTAL_SIZE_LIMIT"):
                validate_portable_bundle(root, max_total_bytes=1)
            with self.assertRaisesRegex(PortableBundleError, "FILE_COUNT_LIMIT"):
                validate_portable_bundle(root, max_files=0)

    def test_archive_inspection_rejects_links_fifo_device_duplicate_and_case_collision(self):
        forbidden_types = (
            tarfile.SYMTYPE,
            tarfile.LNKTYPE,
            tarfile.FIFOTYPE,
            tarfile.CHRTYPE,
            tarfile.BLKTYPE,
        )
        for entry_type in forbidden_types:
            with self.subTest(entry_type=entry_type), tempfile.TemporaryDirectory() as directory:
                archive = Path(directory) / "portable.tar"
                with tarfile.open(archive, "w:", format=tarfile.USTAR_FORMAT) as handle:
                    member = tarfile.TarInfo("payload")
                    member.type = entry_type
                    member.linkname = "target"
                    handle.addfile(member)
                with self.assertRaisesRegex(PortableBundleError, "ENTRY_TYPE_FORBIDDEN"):
                    inspect_portable_archive(archive)

        for names, message in (
            (("same", "same"), "DUPLICATE_PATH"),
            (("Case", "case"), "CASE_COLLISION"),
        ):
            with self.subTest(names=names), tempfile.TemporaryDirectory() as directory:
                archive = Path(directory) / "portable.tar"
                with tarfile.open(archive, "w:", format=tarfile.USTAR_FORMAT) as handle:
                    for name in names:
                        member = tarfile.TarInfo(name)
                        member.size = 1
                        handle.addfile(member, io.BytesIO(b"x"))
                with self.assertRaisesRegex(PortableBundleError, message):
                    inspect_portable_archive(archive)

    @unittest.skipUnless(hasattr(os, "link"), "hard links unavailable")
    def test_extracted_bundle_rejects_hardlinks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = root / "payload"
            payload.write_bytes(b"x")
            alias = root / "alias"
            os.link(payload, alias)
            index = {
                "authorityState": BLOCKED_PORTABLE_PUBLICATION_AUTHORITY,
                "files": [
                    {"path": "alias", "sha256": digest(b"x"), "size": 1},
                    {"path": "payload", "sha256": digest(b"x"), "size": 1},
                ],
                "ociImages": [],
                "profile": "animemo-portable-bundle-v1",
                "schemaVersion": 1,
            }
            (root / "bundle-index.json").write_bytes(canonical_json_bytes(index))
            with self.assertRaisesRegex(PortableBundleError, "HARDLINK_FORBIDDEN"):
                validate_portable_bundle(root)


class OCIImageTests(unittest.TestCase):
    def test_exact_four_role_descriptor_dag_verifies_and_only_builds_a_local_plan(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            images = [
                write_oci_layout(root / "oci" / role, role)
                for role in sorted(REQUIRED_IMAGE_REPOSITORIES)
            ]

            with mock.patch(
                "socket.socket", side_effect=AssertionError("network forbidden")
            ), mock.patch(
                "socket.create_connection",
                side_effect=AssertionError("network forbidden"),
            ):
                verified = verify_oci_image_set(root, images)
                plan = plan_local_image_acquisition(verified)

            self.assertEqual(
                tuple(image.role for image in verified.images),
                tuple(sorted(REQUIRED_IMAGE_REPOSITORIES)),
            )
            self.assertEqual(len(plan.entries), 4)
            for entry in plan.entries:
                self.assertEqual(
                    entry.target_reference,
                    f"{REQUIRED_IMAGE_REPOSITORIES[entry.role]}@{entry.digest}",
                )
                self.assertTrue(entry.layout.is_dir())

    def test_set_rejects_missing_role_repository_platform_and_expected_digest_drift(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            images = [
                write_oci_layout(root / "oci" / role, role)
                for role in sorted(REQUIRED_IMAGE_REPOSITORIES)
            ]
            with self.assertRaisesRegex(OCIContractError, "ROLES_INCOMPLETE"):
                verify_oci_image_set(root, images[:-1])

            wrong_repository = copy.deepcopy(images)
            wrong_repository[0]["repository"] = "registry.invalid/arbitrary"
            with self.assertRaisesRegex(OCIContractError, "REPOSITORY_MISMATCH"):
                verify_oci_image_set(root, wrong_repository)

            wrong_platform = copy.deepcopy(images)
            wrong_platform[0]["platform"] = "linux/arm64"
            with self.assertRaisesRegex(OCIContractError, "PLATFORM_MISMATCH"):
                verify_oci_image_set(root, wrong_platform)

            wrong_digest = copy.deepcopy(images)
            wrong_digest[0]["digest"] = "sha256:" + "f" * 64
            with self.assertRaisesRegex(
                OCIContractError, "EXPECTED_MANIFEST_DIGEST_MISMATCH"
            ):
                verify_oci_image_set(root, wrong_digest)

    def test_descriptor_size_blob_digest_and_closed_dag_are_fail_closed(self):
        def layouts(root: Path) -> list[dict[str, object]]:
            return [
                write_oci_layout(root / "oci" / role, role)
                for role in sorted(REQUIRED_IMAGE_REPOSITORIES)
            ]

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            images = layouts(root)
            api_index_path = root / "oci" / "api" / "index.json"
            api_index = json.loads(api_index_path.read_text(encoding="utf-8"))
            api_index["manifests"][0]["size"] += 1
            api_index_path.write_bytes(canonical_json_bytes(api_index))
            with self.assertRaisesRegex(OCIContractError, "SIZE_MISMATCH"):
                verify_oci_image_set(root, images)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            images = layouts(root)
            layer_digest = digest(b"layer:api")
            layer = root / "oci" / "api" / "blobs" / "sha256" / layer_digest[7:]
            layer.write_bytes(b"tampr:api")
            with self.assertRaisesRegex(OCIContractError, "DIGEST_MISMATCH"):
                verify_oci_image_set(root, images)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            images = layouts(root)
            extra = root / "oci" / "api" / "blobs" / "sha256" / ("e" * 64)
            extra.write_bytes(b"extra")
            with self.assertRaisesRegex(OCIContractError, "DAG_NOT_CLOSED"):
                verify_oci_image_set(root, images)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            images = layouts(root)
            (root / "oci" / "api" / "undeclared-empty-directory").mkdir()
            with self.assertRaisesRegex(OCIContractError, "DAG_NOT_CLOSED"):
                verify_oci_image_set(root, images)

    @unittest.skipUnless(hasattr(os, "link"), "hard links unavailable")
    def test_oci_layout_rejects_hardlinked_blob(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            images = [
                write_oci_layout(root / "oci" / role, role)
                for role in sorted(REQUIRED_IMAGE_REPOSITORIES)
            ]
            layer_digest = digest(b"layer:api")
            layer = root / "oci" / "api" / "blobs" / "sha256" / layer_digest[7:]
            os.link(layer, layer.with_name("e" * 64))
            with self.assertRaisesRegex(OCIContractError, "HARDLINK_FORBIDDEN"):
                verify_oci_image_set(root, images)


if __name__ == "__main__":
    unittest.main()
