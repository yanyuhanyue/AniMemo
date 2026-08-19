from __future__ import annotations

import gzip
import hashlib
import json
import os
import tarfile
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from release.materials import VerifiedMaterialSet
from updater.authority import VerifiedReleaseMaterials
from updater.local_bundle import LocalBundleTransportPolicy
from updater.oci import (
    OCI_CONFIG_MEDIA_TYPE,
    OCI_IMAGE_INDEX_MEDIA_TYPE,
    OCI_IMAGE_MANIFEST_MEDIA_TYPE,
    OCI_LAYER_MEDIA_TYPE,
    REQUIRED_IMAGE_REPOSITORIES,
    DockerOCIImporter,
    ImageAcquirer,
    OCIContractError,
    VerifiedOCIImageSet,
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


def write_layout(
    root: Path,
    role: str,
    *,
    layer_media_type: str = OCI_LAYER_MEDIA_TYPE,
    gzip_trailing: bytes = b"",
    stored_layer_override: bytes | None = None,
    diff_id_override: str | None = None,
) -> dict[str, object]:
    root.mkdir(parents=True)
    (root / "oci-layout").write_bytes(canonical({"imageLayoutVersion": "1.0.0"}))
    uncompressed_layer = ("layer:" + role).encode("ascii")
    config_digest, config_size = write_blob(
        root,
        canonical(
            {
                "architecture": "amd64",
                "os": "linux",
                "rootfs": {
                    "diff_ids": [diff_id_override or digest(uncompressed_layer)],
                    "type": "layers",
                },
            }
        ),
    )
    stored_layer = uncompressed_layer
    if layer_media_type.endswith("+gzip"):
        stored_layer = gzip.compress(uncompressed_layer, mtime=0) + gzip_trailing
    if stored_layer_override is not None:
        stored_layer = stored_layer_override
    layer_digest, layer_size = write_blob(root, stored_layer)
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
                        "mediaType": layer_media_type,
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


class RecordingDockerRunner:
    def __init__(self, *, observed=None) -> None:
        self.calls: list[list[str]] = []
        self.archived_indexes: list[bytes] = []
        self.observed = observed or (lambda reference: [reference])

    def run(self, argv, *, env, timeout):
        self.calls.append(list(argv))
        if argv[:3] == ["/usr/bin/docker", "image", "load"]:
            self.archived_indexes.append(self.read_oci_archive_index(Path(argv[4])))
            return SimpleNamespace(stdout="Loaded local OCI image\n")
        if argv[:3] == ["/usr/bin/docker", "image", "inspect"]:
            return SimpleNamespace(stdout=json.dumps(self.observed(argv[-1])))
        raise AssertionError(f"unexpected Docker argv: {argv}")

    @staticmethod
    def read_oci_archive_index(path: Path) -> bytes:
        with tarfile.open(path, "r:") as handle:
            members = handle.getmembers()
            names = [member.name for member in members]
            if names != ["index.json", "oci-layout"] + sorted(names[2:]):
                raise AssertionError(f"non-canonical OCI archive: {names}")
            for member in members:
                if not member.isfile() or member.mode != 0o644 or member.mtime != 0:
                    raise AssertionError(f"non-canonical OCI member: {member.name}")
            index_stream = handle.extractfile("index.json")
            if index_stream is None:
                raise AssertionError("OCI archive index is not a regular file")
            return index_stream.read()


class OCIOfflineTests(unittest.TestCase):
    def test_image_acquirer_exposes_exact_local_bundle_receipt_seam(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            images = [
                write_layout(root / "oci" / role, role)
                for role in sorted(REQUIRED_IMAGE_REPOSITORIES)
            ]
            verified_images = verify_oci_image_set(root, images)
            identity = "sha256:" + "a" * 64
            materials = VerifiedReleaseMaterials(
                manifest={
                    "images": {
                        image.role: {
                            "digest": image.digest,
                            "repository": image.repository,
                        }
                        for image in verified_images.images
                    }
                },
                deployment_contract={"profile": "animemo-production-v1"},
                verified=VerifiedMaterialSet(
                    root=root,
                    archive_sha256="sha256:" + "b" * 64,
                    files=(),
                ),
                identity_digest=identity,
            )
            runner = RecordingDockerRunner()
            acquirer = ImageAcquirer(runner=runner, environment={})

            receipt = acquirer.acquire_local(
                materials,
                verified_images,
                LocalBundleTransportPolicy(),
            )

            self.assertEqual(receipt.verified_release_identity, identity)
            self.assertEqual(len(receipt.images), 4)
            for image in receipt.images:
                self.assertEqual(image.canonical_reference, image.observed_reference)
                self.assertIn("@sha256:", image.canonical_reference)
            self.assertEqual(len(runner.calls), 8)

    def test_production_docker_importer_loads_exact_four_without_network_or_tag_commands(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            images = [
                write_layout(root / "oci" / role, role)
                for role in sorted(REQUIRED_IMAGE_REPOSITORIES)
            ]
            verified = verify_oci_image_set(root, images)
            runner = RecordingDockerRunner()
            importer = DockerOCIImporter(runner=runner, environment={})

            receipt = import_verified_oci_image_set(verified, importer)

            expected = tuple(
                f"{image.repository}@{image.digest}" for image in verified.images
            )
            self.assertEqual(receipt.images, expected)
            self.assertEqual(len(runner.calls), 8)
            for argv in runner.calls:
                self.assertIs(type(argv), list)
                self.assertEqual(argv[0], "/usr/bin/docker")
                self.assertNotIn(argv[2], {"pull", "tag", "login", "push"})
                self.assertFalse(
                    any("http://" in part or "https://" in part for part in argv)
                )

    def test_import_archive_has_deterministic_digest_derived_ref_name_only(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            expectations = [
                write_layout(root / "oci" / role, role)
                for role in sorted(REQUIRED_IMAGE_REPOSITORIES)
            ]
            frozen_index = root / "oci" / "api" / "index.json"
            frozen_before = frozen_index.read_bytes()
            image = verify_oci_image_set(root, expectations).image("api")
            runner = RecordingDockerRunner()
            importer = DockerOCIImporter(runner=runner, environment={})

            importer.import_verified_image(image)
            importer.import_verified_image(image)

            self.assertEqual(len(runner.archived_indexes), 2)
            self.assertEqual(runner.archived_indexes[0], runner.archived_indexes[1])
            archived_index = json.loads(runner.archived_indexes[0])
            self.assertEqual(
                archived_index["manifests"][0]["annotations"],
                {
                    "org.opencontainers.image.ref.name": (
                        f"{image.repository}:animemo-offline-{image.digest[7:]}"
                    )
                },
            )
            self.assertEqual(frozen_index.read_bytes(), frozen_before)
            self.assertNotIn(
                "annotations", json.loads(frozen_before)["manifests"][0]
            )
            self.assertNotIn("tag", [argv[2] for argv in runner.calls])

    def test_gzip_layers_are_diff_id_verified_before_local_docker_load(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            expectations = [
                write_layout(
                    root / "oci" / role,
                    role,
                    layer_media_type="application/vnd.oci.image.layer.v1.tar+gzip",
                )
                for role in sorted(REQUIRED_IMAGE_REPOSITORIES)
            ]
            verified = verify_oci_image_set(root, expectations)
            runner = RecordingDockerRunner()

            receipt = import_verified_oci_image_set(
                verified,
                DockerOCIImporter(runner=runner, environment={}),
            )

            self.assertEqual(len(receipt.images), 4)
            self.assertEqual(
                [argv[2] for argv in runner.calls],
                ["load", "inspect"] * 4,
            )

    def test_gzip_layer_rejects_trailing_invalid_diff_id_and_resource_exhaustion(self):
        gzip_media_type = "application/vnd.oci.image.layer.v1.tar+gzip"

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            expectations = [
                write_layout(
                    root / "oci" / role,
                    role,
                    layer_media_type=gzip_media_type,
                    gzip_trailing=b"trailing",
                )
                for role in sorted(REQUIRED_IMAGE_REPOSITORIES)
            ]
            with self.assertRaisesRegex(OCIContractError, "GZIP_TRAILING_DATA"):
                verify_oci_image_set(root, expectations)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            expectations = [
                write_layout(
                    root / "oci" / role,
                    role,
                    layer_media_type=gzip_media_type,
                    stored_layer_override=b"not-a-gzip-stream",
                )
                for role in sorted(REQUIRED_IMAGE_REPOSITORIES)
            ]
            with self.assertRaisesRegex(OCIContractError, "GZIP_INVALID"):
                verify_oci_image_set(root, expectations)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            expectations = [
                write_layout(
                    root / "oci" / role,
                    role,
                    layer_media_type=gzip_media_type,
                    diff_id_override="sha256:" + "f" * 64,
                )
                for role in sorted(REQUIRED_IMAGE_REPOSITORIES)
            ]
            with self.assertRaisesRegex(OCIContractError, "DIFF_ID_MISMATCH"):
                verify_oci_image_set(root, expectations)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            expectations = [
                write_layout(
                    root / "oci" / role,
                    role,
                    layer_media_type=gzip_media_type,
                )
                for role in sorted(REQUIRED_IMAGE_REPOSITORIES)
            ]
            with (
                mock.patch("updater.oci.MAX_OCI_LAYER_UNCOMPRESSED_BYTES", 4),
                self.assertRaisesRegex(
                    OCIContractError, "LAYER_UNCOMPRESSED_SIZE_LIMIT"
                ),
            ):
                verify_oci_image_set(root, expectations)
            with (
                mock.patch(
                    "updater.oci.MAX_OCI_IMAGE_UNCOMPRESSED_LAYER_BYTES", 4
                ),
                self.assertRaisesRegex(
                    OCIContractError, "LAYERS_UNCOMPRESSED_SIZE_LIMIT"
                ),
            ):
                verify_oci_image_set(root, expectations)

    def test_unqualified_zstd_layer_remains_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            expectations = [
                write_layout(
                    root / "oci" / role,
                    role,
                    layer_media_type="application/vnd.oci.image.layer.v1.tar+zstd",
                )
                for role in sorted(REQUIRED_IMAGE_REPOSITORIES)
            ]

            with self.assertRaisesRegex(OCIContractError, "MEDIA_TYPE_INVALID"):
                verify_oci_image_set(root, expectations)

    def test_input_ref_name_annotation_is_rejected_before_any_docker_call(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            expectations = [
                write_layout(root / "oci" / role, role)
                for role in sorted(REQUIRED_IMAGE_REPOSITORIES)
            ]
            verified = verify_oci_image_set(root, expectations)
            frozen_index = root / "oci" / "api" / "index.json"
            attacker_index = json.loads(frozen_index.read_bytes())
            attacker_index["manifests"][0]["annotations"] = {
                "org.opencontainers.image.ref.name": "attacker.invalid/override:latest"
            }
            frozen_index.write_bytes(canonical(attacker_index))
            runner = RecordingDockerRunner()

            with self.assertRaisesRegex(
                OCIContractError, "OCI_DESCRIPTOR_FIELDS_INVALID"
            ):
                import_verified_oci_image_set(
                    verified,
                    DockerOCIImporter(runner=runner, environment={}),
                )

            self.assertEqual(runner.calls, [])

    def test_all_four_layouts_are_reverified_before_first_active_import(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            images = [
                write_layout(root / "oci" / role, role)
                for role in sorted(REQUIRED_IMAGE_REPOSITORIES)
            ]
            verified = verify_oci_image_set(root, images)
            final_role = max(REQUIRED_IMAGE_REPOSITORIES)
            layer = (
                root
                / "oci"
                / final_role
                / "blobs"
                / "sha256"
                / digest(("layer:" + final_role).encode("ascii"))[7:]
            )
            layer.write_bytes(("tampr:" + final_role).encode("ascii"))
            runner = RecordingDockerRunner()

            with self.assertRaisesRegex(OCIContractError, "DIGEST_MISMATCH"):
                import_verified_oci_image_set(
                    verified,
                    DockerOCIImporter(runner=runner, environment={}),
                )

            self.assertEqual(runner.calls, [])

    def test_production_importer_rejects_tag_only_and_wrong_digest_readback(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            images = [
                write_layout(root / "oci" / role, role)
                for role in sorted(REQUIRED_IMAGE_REPOSITORIES)
            ]
            image = verify_oci_image_set(root, images).images[0]
            bad_results = (
                [f"{image.repository}:latest"],
                [f"{image.repository}@sha256:{'f' * 64}"],
                [],
            )
            for observed in bad_results:
                with self.subTest(observed=observed):
                    runner = RecordingDockerRunner(
                        observed=lambda reference, value=observed: value
                    )
                    importer = DockerOCIImporter(runner=runner, environment={})
                    with self.assertRaisesRegex(
                        OCIContractError, "POST_IMPORT_DIGEST_MISMATCH"
                    ):
                        importer.import_verified_image(image)
                    self.assertEqual(
                        [argv[2] for argv in runner.calls], ["load", "inspect"]
                    )

    def test_official_library_display_alias_preserves_canonical_reference(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            expectations = [
                write_layout(root / "oci" / role, role)
                for role in sorted(REQUIRED_IMAGE_REPOSITORIES)
            ]
            postgres = verify_oci_image_set(root, expectations).image("postgres")
            canonical_reference = f"{postgres.repository}@{postgres.digest}"
            runner = RecordingDockerRunner(
                observed=lambda reference: [f"postgres@{postgres.digest}"]
            )

            observed_reference = DockerOCIImporter(
                runner=runner, environment={}
            ).import_verified_image(postgres)

            self.assertEqual(observed_reference, canonical_reference)
            self.assertEqual(runner.calls[-1][-1], canonical_reference)

    def test_official_library_display_alias_is_exact_repository_and_digest_bound(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            expectations = [
                write_layout(root / "oci" / role, role)
                for role in sorted(REQUIRED_IMAGE_REPOSITORIES)
            ]
            verified = verify_oci_image_set(root, expectations)
            postgres = verified.image("postgres")
            api = verified.image("api")
            invalid_readbacks = (
                (postgres, f"postgres@sha256:{'f' * 64}"),
                (postgres, f"redis@{postgres.digest}"),
                (postgres, f"library/postgres@{postgres.digest}"),
                (postgres, f"attacker/postgres@{postgres.digest}"),
                (api, f"animemo-api@{api.digest}"),
            )
            for image, readback in invalid_readbacks:
                with self.subTest(role=image.role, readback=readback):
                    runner = RecordingDockerRunner(
                        observed=lambda reference, value=readback: [value]
                    )
                    with self.assertRaisesRegex(
                        OCIContractError, "POST_IMPORT_DIGEST_MISMATCH"
                    ):
                        DockerOCIImporter(
                            runner=runner, environment={}
                        ).import_verified_image(image)

    def test_official_library_display_alias_keeps_local_receipt_canonical(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            expectations = [
                write_layout(root / "oci" / role, role)
                for role in sorted(REQUIRED_IMAGE_REPOSITORIES)
            ]
            verified = verify_oci_image_set(root, expectations)
            materials = VerifiedReleaseMaterials(
                manifest={
                    "images": {
                        image.role: {
                            "digest": image.digest,
                            "repository": image.repository,
                        }
                        for image in verified.images
                    }
                },
                deployment_contract={"profile": "animemo-production-v1"},
                verified=VerifiedMaterialSet(
                    root=root,
                    archive_sha256="sha256:" + "b" * 64,
                    files=(),
                ),
                identity_digest="sha256:" + "a" * 64,
            )

            def daemon_readback(reference: str) -> list[str]:
                repository, image_digest = reference.split("@", 1)
                if repository in {
                    "docker.io/library/postgres",
                    "docker.io/library/redis",
                }:
                    return [f"{repository.rsplit('/', 1)[1]}@{image_digest}"]
                return [reference]

            receipt = ImageAcquirer(
                runner=RecordingDockerRunner(observed=daemon_readback),
                environment={},
            ).acquire_local(materials, verified, LocalBundleTransportPolicy())

            self.assertEqual(
                tuple(item.canonical_reference for item in receipt.images),
                tuple(
                    f"{image.repository}@{image.digest}" for image in verified.images
                ),
            )
            self.assertEqual(
                tuple(item.observed_reference for item in receipt.images),
                tuple(item.canonical_reference for item in receipt.images),
            )

    def test_local_acquirer_rejects_partial_or_release_identity_drift_before_docker(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            images = [
                write_layout(root / "oci" / role, role)
                for role in sorted(REQUIRED_IMAGE_REPOSITORIES)
            ]
            verified = verify_oci_image_set(root, images)

            def materials(repository_override: str | None = None):
                return VerifiedReleaseMaterials(
                    manifest={
                        "images": {
                            image.role: {
                                "digest": image.digest,
                                "repository": (
                                    repository_override
                                    if repository_override is not None
                                    and image.role == "api"
                                    else image.repository
                                ),
                            }
                            for image in verified.images
                        }
                    },
                    deployment_contract={"profile": "animemo-production-v1"},
                    verified=VerifiedMaterialSet(
                        root=root,
                        archive_sha256="sha256:" + "b" * 64,
                        files=(),
                    ),
                    identity_digest="sha256:" + "a" * 64,
                )

            runner = RecordingDockerRunner()
            acquirer = ImageAcquirer(runner=runner, environment={})
            with self.assertRaisesRegex(OCIContractError, "IMAGE_SET_NOT_VERIFIED"):
                acquirer.acquire_local(
                    materials(),
                    VerifiedOCIImageSet(verified.images[:-1]),
                    LocalBundleTransportPolicy(),
                )
            with self.assertRaisesRegex(
                OCIContractError, "RELEASE_IMAGE_IDENTITY_MISMATCH"
            ):
                acquirer.acquire_local(
                    materials("registry.invalid/animemo-api"),
                    verified,
                    LocalBundleTransportPolicy(),
                )
            self.assertEqual(runner.calls, [])

    def test_missing_extra_manifest_traversal_and_symlink_never_reach_docker(self):
        def fresh(root: Path):
            images = [
                write_layout(root / "oci" / role, role)
                for role in sorted(REQUIRED_IMAGE_REPOSITORIES)
            ]
            return images

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            images = fresh(root)
            missing = (
                root
                / "oci"
                / "api"
                / "blobs"
                / "sha256"
                / digest(b"layer:api")[7:]
            )
            missing.unlink()
            with self.assertRaisesRegex(OCIContractError, "FILE_UNAVAILABLE"):
                verify_oci_image_set(root, images)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            images = fresh(root)
            index_path = root / "oci" / "api" / "index.json"
            index = json.loads(index_path.read_text(encoding="utf-8"))
            index["manifests"].append(dict(index["manifests"][0]))
            index_path.write_bytes(canonical(index))
            with self.assertRaisesRegex(OCIContractError, "MANIFEST_COUNT_INVALID"):
                verify_oci_image_set(root, images)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            images = fresh(root)
            images[0]["layoutPath"] = "../oci/api"
            with self.assertRaisesRegex(OCIContractError, "LAYOUT_PATH_INVALID"):
                verify_oci_image_set(root, images)

        if hasattr(os, "symlink"):
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                images = fresh(root)
                layer = (
                    root
                    / "oci"
                    / "api"
                    / "blobs"
                    / "sha256"
                    / digest(b"layer:api")[7:]
                )
                target = root / "symlink-target"
                target.write_bytes(b"layer:api")
                layer.unlink()
                try:
                    os.symlink(target, layer)
                except OSError:
                    return
                with self.assertRaisesRegex(OCIContractError, "FILE_TYPE_FORBIDDEN"):
                    verify_oci_image_set(root, images)

    def test_rehashed_arm64_config_is_rejected_before_import(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            images = [
                write_layout(root / "oci" / role, role)
                for role in sorted(REQUIRED_IMAGE_REPOSITORIES)
            ]
            layout = root / "oci" / "api"
            index_path = layout / "index.json"
            index = json.loads(index_path.read_text(encoding="utf-8"))
            old_manifest_digest = index["manifests"][0]["digest"]
            old_manifest_path = layout / "blobs" / "sha256" / old_manifest_digest[7:]
            manifest = json.loads(old_manifest_path.read_text(encoding="utf-8"))
            old_config_digest = manifest["config"]["digest"]
            old_config_path = layout / "blobs" / "sha256" / old_config_digest[7:]
            config = json.loads(old_config_path.read_text(encoding="utf-8"))
            config["architecture"] = "arm64"
            new_config_digest, new_config_size = write_blob(layout, canonical(config))
            old_config_path.unlink()
            manifest["config"]["digest"] = new_config_digest
            manifest["config"]["size"] = new_config_size
            new_manifest_digest, new_manifest_size = write_blob(
                layout, canonical(manifest)
            )
            old_manifest_path.unlink()
            index["manifests"][0]["digest"] = new_manifest_digest
            index["manifests"][0]["size"] = new_manifest_size
            index_path.write_bytes(canonical(index))
            images[0]["digest"] = new_manifest_digest

            with self.assertRaisesRegex(OCIContractError, "CONFIG_PLATFORM_MISMATCH"):
                verify_oci_image_set(root, images)

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
