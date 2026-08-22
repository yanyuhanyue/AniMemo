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
from updater import oci as oci_module
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
    OCIImageExpectation,
    VerifiedOCIImageSet,
    import_verified_oci_image_set,
    normalize_crane_oci_layout,
    verify_oci_image,
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


DOCKER_MANIFEST_MEDIA_TYPE = "application/vnd.docker.distribution.manifest.v2+json"
DOCKER_CONFIG_MEDIA_TYPE = "application/vnd.docker.container.image.v1+json"
DOCKER_LAYER_GZIP_MEDIA_TYPE = "application/vnd.docker.image.rootfs.diff.tar.gzip"
OBSERVED_OCI_ANNOTATIONS_BY_ROLE = {
    "postgres": {
        "com.docker.official-images.bashbrew.arch": "amd64",
        "org.opencontainers.image.base.digest": "sha256:79ff19e9084a00eece421b2523fb93e22d730e2c0e525905de047e848e56d95f",
        "org.opencontainers.image.base.name": "alpine:3.24",
        "org.opencontainers.image.created": "2026-08-13T19:16:04Z",
        "org.opencontainers.image.revision": "9d15534160ade17f2b6c455a39ee967c49b1937d",
        "org.opencontainers.image.source": "https://github.com/docker-library/postgres.git#9d15534160ade17f2b6c455a39ee967c49b1937d:16/alpine3.24",
        "org.opencontainers.image.url": "https://hub.docker.com/_/postgres",
        "org.opencontainers.image.version": "16.15-alpine3.24",
    },
    "redis": {
        "com.docker.official-images.bashbrew.arch": "amd64",
        "org.opencontainers.image.base.digest": "sha256:f27cad9117495d32d067133afff942cb2dc745dfe9163e949f6bfe8a6a245339",
        "org.opencontainers.image.base.name": "alpine:3.21",
        "org.opencontainers.image.created": "2026-07-26T04:41:11Z",
        "org.opencontainers.image.revision": "31c68732e7311d95e4c833b8fa50aa561ced577f",
        "org.opencontainers.image.source": "https://github.com/redis/docker-library-redis.git#31c68732e7311d95e4c833b8fa50aa561ced577f:alpine",
        "org.opencontainers.image.url": "https://hub.docker.com/_/redis",
        "org.opencontainers.image.version": "7.4.10-alpine",
    },
}


def write_crane_layout(
    root: Path,
    role: str,
    *,
    profile: str,
    config_os: str = "linux",
    config_architecture: str = "amd64",
    config_media_type: str | None = None,
    layer_media_type: str | None = None,
) -> dict[str, str]:
    root.mkdir(parents=True)
    (root / "oci-layout").write_bytes(canonical({"imageLayoutVersion": "1.0.0"}))
    docker = profile == "docker"
    manifest_media_type = (
        DOCKER_MANIFEST_MEDIA_TYPE if docker else OCI_IMAGE_MANIFEST_MEDIA_TYPE
    )
    config_media_type = config_media_type or (
        DOCKER_CONFIG_MEDIA_TYPE if docker else OCI_CONFIG_MEDIA_TYPE
    )
    layer_media_type = layer_media_type or (
        DOCKER_LAYER_GZIP_MEDIA_TYPE
        if docker
        else "application/vnd.oci.image.layer.v1.tar+gzip"
    )
    uncompressed_layer = ("external-layer:" + role).encode("ascii")
    stored_layer = gzip.compress(uncompressed_layer, mtime=0)
    layer_digest, layer_size = write_blob(root, stored_layer)
    config_digest, config_size = write_blob(
        root,
        canonical(
            {
                "architecture": config_architecture,
                "os": config_os,
                "rootfs": {"diff_ids": [digest(uncompressed_layer)], "type": "layers"},
            }
        ),
    )
    manifest = {
        "config": {
            "digest": config_digest,
            "mediaType": config_media_type,
            "size": config_size,
        },
        "layers": [
            {
                "digest": layer_digest,
                "mediaType": layer_media_type,
                "size": layer_size,
            }
        ],
        "mediaType": manifest_media_type,
        "schemaVersion": 2,
    }
    if not docker:
        manifest["annotations"] = dict(OBSERVED_OCI_ANNOTATIONS_BY_ROLE[role])
    manifest_digest, manifest_size = write_blob(root, canonical(manifest))
    root_descriptor = {
        "artifactType": config_media_type,
        "digest": manifest_digest,
        "mediaType": manifest_media_type,
        "size": manifest_size,
    }
    if not docker:
        root_descriptor["annotations"] = dict(OBSERVED_OCI_ANNOTATIONS_BY_ROLE[role])
    (root / "index.json").write_bytes(
        canonical(
            {
                "manifests": [root_descriptor],
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


def crane_expectation(value: dict[str, str]) -> OCIImageExpectation:
    return OCIImageExpectation(
        role=value["role"],
        repository=value["repository"],
        digest=value["digest"],
        platform=value["platform"],
        layout_path=value["layoutPath"],
    )


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
    def test_crane_root_adapter_accepts_only_closed_docker_and_oci_profiles(self):
        for role, profile in (
            ("api", "docker"),
            ("web", "docker"),
            ("postgres", "oci"),
            ("redis", "oci"),
        ):
            with self.subTest(role=role), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                expectation = write_crane_layout(
                    root / "oci" / role, role, profile=profile
                )
                layout = root / "oci" / role
                before = json.loads((layout / "index.json").read_text(encoding="utf-8"))
                authoritative_digest = before["manifests"][0]["digest"]

                expected_image = crane_expectation(expectation)
                receipt = normalize_crane_oci_layout(
                    layout, expected_image, source_root=root
                )

                after_bytes = (layout / "index.json").read_bytes()
                after = json.loads(after_bytes)
                self.assertEqual(
                    set(after["manifests"][0]),
                    {"digest", "mediaType", "platform", "size"},
                )
                self.assertEqual(after["manifests"][0]["digest"], authoritative_digest)
                self.assertEqual(
                    after["manifests"][0]["platform"],
                    {"architecture": "amd64", "os": "linux"},
                )
                self.assertEqual(
                    receipt["profile"],
                    "docker-schema2" if profile == "docker" else "oci-v1",
                )
                self.assertFalse(receipt["authoritativeDigestRewritten"])
                self.assertTrue(receipt["changed"])
                self.assertEqual(
                    verify_oci_image(layout, expected_image).digest,
                    authoritative_digest,
                )

                repeated = normalize_crane_oci_layout(
                    layout,
                    expected_image,
                    source_root=root,
                )
                self.assertFalse(repeated["changed"])
                self.assertEqual((layout / "index.json").read_bytes(), after_bytes)

    def test_crane_root_adapter_rejects_unobserved_role_profile_pair(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            expectation = write_crane_layout(
                root / "oci" / "postgres", "postgres", profile="docker"
            )
            with self.assertRaisesRegex(
                OCIContractError, "OCI_CRANE_ROLE_PROFILE_INVALID"
            ):
                normalize_crane_oci_layout(
                    root / "oci" / "postgres",
                    crane_expectation(expectation),
                    source_root=root,
                )

    def test_crane_root_adapter_rejects_unobserved_descriptor_surfaces(self):
        mutations = (
            ("urls", ["https://example.invalid/blob"]),
            ("data", "opaque"),
            ("subject", {"digest": "sha256:" + "a" * 64}),
            ("unknown", "value"),
        )
        for field, value in mutations:
            with self.subTest(field=field), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                expectation = write_crane_layout(
                    root / "oci" / "api", "api", profile="docker"
                )
                layout = root / "oci" / "api"
                index_path = layout / "index.json"
                index = json.loads(index_path.read_text(encoding="utf-8"))
                index["manifests"][0][field] = value
                index_path.write_bytes(canonical(index))
                with self.assertRaisesRegex(
                    OCIContractError, "OCI_CRANE_DESCRIPTOR_FIELDS_INVALID"
                ):
                    normalize_crane_oci_layout(
                        layout,
                        crane_expectation(expectation),
                        source_root=root,
                    )

    def test_crane_root_adapter_rejects_artifact_annotation_and_profile_drift(self):
        cases = (
            "artifact",
            "annotation",
            "annotation-value",
            "surrogate",
            "mixed-profile",
            "platform",
        )
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                annotated = case in {"annotation", "annotation-value", "surrogate"}
                profile = "oci" if annotated else "docker"
                role = "postgres" if annotated else "api"
                expectation = write_crane_layout(
                    root / "oci" / role,
                    role,
                    profile=profile,
                    config_architecture="arm64" if case == "platform" else "amd64",
                    config_media_type=OCI_CONFIG_MEDIA_TYPE
                    if case == "mixed-profile"
                    else None,
                )
                layout = root / "oci" / role
                index_path = layout / "index.json"
                index = json.loads(index_path.read_text(encoding="utf-8"))
                if case == "artifact":
                    index["manifests"][0]["artifactType"] = "application/octet-stream"
                elif case == "annotation":
                    index["manifests"][0]["annotations"]["org.example.untrusted"] = (
                        "value"
                    )
                elif case == "annotation-value":
                    index["manifests"][0]["annotations"][
                        "org.opencontainers.image.version"
                    ] = "unobserved"
                elif case == "surrogate":
                    index["manifests"][0]["annotations"][
                        "org.opencontainers.image.version"
                    ] = "\ud800"
                index_path.write_bytes(canonical(index))
                code = {
                    "artifact": "OCI_CRANE_ARTIFACT_TYPE_INVALID",
                    "annotation": "OCI_ANNOTATION_FIELDS_INVALID",
                    "annotation-value": "OCI_ANNOTATION_VALUE_INVALID",
                    "surrogate": "OCI_ANNOTATION_VALUE_INVALID",
                    "mixed-profile": "OCI_IMAGE_PROFILE_INVALID",
                    "platform": "OCI_CONFIG_PLATFORM_MISMATCH",
                }[case]
                with self.assertRaisesRegex(OCIContractError, code):
                    normalize_crane_oci_layout(
                        layout,
                        crane_expectation(expectation),
                        source_root=root,
                    )

    def test_crane_root_adapter_rejects_invalid_identity_and_authority_shapes(self):
        cases = (
            ("missing-digest", "OCI_CRANE_DESCRIPTOR_FIELDS_INVALID"),
            ("invalid-digest", "OCI_DIGEST_INVALID"),
            ("negative-size", "OCI_DESCRIPTOR_SIZE_INVALID"),
            ("size-mismatch", "OCI_DESCRIPTOR_SIZE_MISMATCH"),
            ("multiple-roots", "OCI_INDEX_MANIFEST_COUNT_INVALID"),
            ("root-is-index", "OCI_DESCRIPTOR_MEDIA_TYPE_INVALID"),
            ("unknown-index-field", "OCI_INDEX_FIELDS_INVALID"),
        )
        for case, code in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                expectation = write_crane_layout(
                    root / "oci" / "api", "api", profile="docker"
                )
                layout = root / "oci" / "api"
                index_path = layout / "index.json"
                index = json.loads(index_path.read_text(encoding="utf-8"))
                descriptor = index["manifests"][0]
                if case == "missing-digest":
                    del descriptor["digest"]
                elif case == "invalid-digest":
                    descriptor["digest"] = "sha256:invalid"
                elif case == "negative-size":
                    descriptor["size"] = -1
                elif case == "size-mismatch":
                    descriptor["size"] += 1
                elif case == "multiple-roots":
                    index["manifests"].append(dict(descriptor))
                elif case == "root-is-index":
                    descriptor["mediaType"] = OCI_IMAGE_INDEX_MEDIA_TYPE
                else:
                    index["unknown"] = True
                index_path.write_bytes(canonical(index))
                with self.assertRaisesRegex(OCIContractError, code):
                    normalize_crane_oci_layout(
                        layout,
                        crane_expectation(expectation),
                        source_root=root,
                    )

    def test_crane_root_adapter_rejects_schema1_and_expected_digest_drift(self):
        for case in ("schema1", "manifest-array", "expected-digest"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                expectation = write_crane_layout(
                    root / "oci" / "api", "api", profile="docker"
                )
                layout = root / "oci" / "api"
                index_path = layout / "index.json"
                index = json.loads(index_path.read_text(encoding="utf-8"))
                if case == "schema1":
                    old_digest = index["manifests"][0]["digest"]
                    manifest_path = layout / "blobs" / "sha256" / old_digest[7:]
                    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                    manifest["schemaVersion"] = 1
                    new_digest, new_size = write_blob(layout, canonical(manifest))
                    index["manifests"][0]["digest"] = new_digest
                    index["manifests"][0]["size"] = new_size
                    expectation["digest"] = new_digest
                    index_path.write_bytes(canonical(index))
                    code = "OCI_MANIFEST_IDENTITY_INVALID"
                elif case == "manifest-array":
                    new_digest, new_size = write_blob(layout, b"[]")
                    index["manifests"][0]["digest"] = new_digest
                    index["manifests"][0]["size"] = new_size
                    expectation["digest"] = new_digest
                    index_path.write_bytes(canonical(index))
                    code = "OCI_JSON_OBJECT_REQUIRED"
                else:
                    expectation["digest"] = "sha256:" + "f" * 64
                    code = "OCI_EXPECTED_MANIFEST_DIGEST_MISMATCH"
                with self.assertRaisesRegex(OCIContractError, code):
                    normalize_crane_oci_layout(
                        layout,
                        crane_expectation(expectation),
                        source_root=root,
                    )

    def test_crane_root_adapter_rejects_manifest_config_and_layer_blob_digest_drift(
        self,
    ):
        for subject in ("manifest", "config", "layer"):
            with (
                self.subTest(subject=subject),
                tempfile.TemporaryDirectory() as directory,
            ):
                root = Path(directory)
                expectation = write_crane_layout(
                    root / "oci" / "api", "api", profile="docker"
                )
                layout = root / "oci" / "api"
                index = json.loads((layout / "index.json").read_text(encoding="utf-8"))
                manifest_digest = index["manifests"][0]["digest"]
                manifest_path = layout / "blobs" / "sha256" / manifest_digest[7:]
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                if subject == "manifest":
                    blob_digest = manifest_digest
                elif subject == "config":
                    blob_digest = manifest["config"]["digest"]
                else:
                    blob_digest = manifest["layers"][0]["digest"]
                blob_path = layout / "blobs" / "sha256" / blob_digest[7:]
                if subject == "layer":
                    replacement = gzip.compress(b"tampered-layer:api", mtime=0)
                    self.assertEqual(len(replacement), blob_path.stat().st_size)
                    blob_path.write_bytes(replacement)
                else:
                    value = bytearray(blob_path.read_bytes())
                    value[0] ^= 1
                    blob_path.write_bytes(value)
                with self.assertRaisesRegex(
                    OCIContractError, "OCI_DESCRIPTOR_DIGEST_MISMATCH"
                ):
                    normalize_crane_oci_layout(
                        layout,
                        crane_expectation(expectation),
                        source_root=root,
                    )

    def test_crane_root_adapter_rejects_wrong_canonical_platform(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            expectation = write_crane_layout(
                root / "oci" / "api", "api", profile="docker"
            )
            layout = root / "oci" / "api"
            normalize_crane_oci_layout(
                layout,
                crane_expectation(expectation),
                source_root=root,
            )
            index_path = layout / "index.json"
            index = json.loads(index_path.read_text(encoding="utf-8"))
            index["manifests"][0]["platform"]["architecture"] = "arm64"
            index_path.write_bytes(canonical(index))

            with self.assertRaisesRegex(OCIContractError, "OCI_PLATFORM_MISMATCH"):
                normalize_crane_oci_layout(
                    layout,
                    crane_expectation(expectation),
                    source_root=root,
                )

    def test_crane_root_adapter_binds_layout_to_source_root_and_role(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            expectation = write_crane_layout(
                root / "oci" / "api", "api", profile="docker"
            )
            layout = root / "oci" / "api"
            original_index = (layout / "index.json").read_bytes()
            unrelated_root = root / "unrelated"
            unrelated_root.mkdir()

            with self.assertRaisesRegex(OCIContractError, "OCI_LAYOUT_PATH_MISMATCH"):
                normalize_crane_oci_layout(
                    layout,
                    crane_expectation(expectation),
                    source_root=unrelated_root,
                )
            self.assertEqual((layout / "index.json").read_bytes(), original_index)

    def test_crane_root_adapter_rejects_index_links_duplicate_keys_and_read_drift(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            expectation = write_crane_layout(
                root / "oci" / "api", "api", profile="docker"
            )
            layout = root / "oci" / "api"
            index_path = layout / "index.json"
            external = root / "external-index.json"
            index_path.replace(external)
            try:
                os.symlink(external, index_path)
            except OSError:
                pass
            else:
                with self.assertRaisesRegex(
                    OCIContractError, "OCI_FILE_TYPE_FORBIDDEN"
                ):
                    normalize_crane_oci_layout(
                        layout,
                        crane_expectation(expectation),
                        source_root=root,
                    )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            expectation = write_crane_layout(
                root / "oci" / "api", "api", profile="docker"
            )
            layout = root / "oci" / "api"
            index_path = layout / "index.json"
            os.link(index_path, root / "external-hardlink.json")
            with self.assertRaisesRegex(OCIContractError, "OCI_HARDLINK_FORBIDDEN"):
                normalize_crane_oci_layout(
                    layout,
                    crane_expectation(expectation),
                    source_root=root,
                )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            layout = root / "oci" / "api"
            write_crane_layout(layout, "api", profile="docker")
            index_path = layout / "index.json"
            value = index_path.read_text(encoding="utf-8")
            index_path.write_text(
                value.replace(
                    '"schemaVersion":2', '"schemaVersion":2,"schemaVersion":2'
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(OCIContractError, "OCI_JSON_DUPLICATE_KEY"):
                oci_module._read_regular_file(index_path)
                oci_module._parse_json(index_path.read_bytes())

        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "metadata.json"
            target.write_bytes(b"{}")
            real_fstat = os.fstat
            calls = 0

            def changed_after_read(descriptor):
                nonlocal calls
                calls += 1
                observed = real_fstat(descriptor)
                if calls == 2:
                    return SimpleNamespace(st_size=observed.st_size + 1)
                return observed

            with (
                mock.patch("updater.oci.os.fstat", side_effect=changed_after_read),
                self.assertRaisesRegex(
                    OCIContractError, "OCI_FILE_CHANGED_DURING_READ"
                ),
            ):
                oci_module._read_regular_file(target)

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
