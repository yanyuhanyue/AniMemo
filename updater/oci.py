from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import stat
import tarfile
import tempfile
import unicodedata
import zlib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Protocol

from .authority import VerifiedReleaseMaterials
from .commands import CommandRunner
from .errors import CommandFailed
from .transport import ExplicitTransportPolicy

OCI_IMAGE_INDEX_MEDIA_TYPE = "application/vnd.oci.image.index.v1+json"
OCI_IMAGE_MANIFEST_MEDIA_TYPE = "application/vnd.oci.image.manifest.v1+json"
OCI_CONFIG_MEDIA_TYPE = "application/vnd.oci.image.config.v1+json"
OCI_LAYER_MEDIA_TYPE = "application/vnd.oci.image.layer.v1.tar"
OCI_LAYER_GZIP_MEDIA_TYPE = "application/vnd.oci.image.layer.v1.tar+gzip"
OCI_LAYER_MEDIA_TYPES = frozenset(
    {
        OCI_LAYER_MEDIA_TYPE,
        OCI_LAYER_GZIP_MEDIA_TYPE,
    }
)
DOCKER_SCHEMA2_MANIFEST_MEDIA_TYPE = (
    "application/vnd.docker.distribution.manifest.v2+json"
)
DOCKER_SCHEMA2_CONFIG_MEDIA_TYPE = "application/vnd.docker.container.image.v1+json"
DOCKER_SCHEMA2_LAYER_GZIP_MEDIA_TYPE = (
    "application/vnd.docker.image.rootfs.diff.tar.gzip"
)
SUPPORTED_MANIFEST_MEDIA_TYPES = frozenset(
    {OCI_IMAGE_MANIFEST_MEDIA_TYPE, DOCKER_SCHEMA2_MANIFEST_MEDIA_TYPE}
)
OBSERVED_OCI_ANNOTATION_KEYS = frozenset(
    {
        "com.docker.official-images.bashbrew.arch",
        "org.opencontainers.image.base.digest",
        "org.opencontainers.image.base.name",
        "org.opencontainers.image.created",
        "org.opencontainers.image.revision",
        "org.opencontainers.image.source",
        "org.opencontainers.image.url",
        "org.opencontainers.image.version",
    }
)
OBSERVED_CRANE_OCI_ANNOTATIONS = {
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
OBSERVED_CRANE_PROFILE_BY_ROLE = {
    "api": "docker-schema2",
    "postgres": "oci-v1",
    "redis": "oci-v1",
    "web": "docker-schema2",
}
OCI_PLATFORM = "linux/amd64"
OCI_REF_NAME_ANNOTATION = "org.opencontainers.image.ref.name"
DERIVED_IMPORT_TAG_PREFIX = "animemo-offline-"
OCI_STREAM_CHUNK_BYTES = 1024 * 1024
MAX_OCI_METADATA_BYTES = 16 * 1024 * 1024
MAX_OCI_BLOB_BYTES = 4 * 1024 * 1024 * 1024
MAX_OCI_LAYERS = 256
MAX_OCI_LAYOUT_FILES = MAX_OCI_LAYERS + 4
MAX_OCI_LAYOUT_DIRECTORIES = 4
MAX_OCI_LAYOUT_TOTAL_BYTES = 16 * 1024 * 1024 * 1024
MAX_OCI_LAYER_UNCOMPRESSED_BYTES = 4 * 1024 * 1024 * 1024
MAX_OCI_IMAGE_UNCOMPRESSED_LAYER_BYTES = 16 * 1024 * 1024 * 1024
MAX_OCI_PATH_BYTES = 1024
MAX_OCI_PATH_DEPTH = 8
MAX_OCI_PATH_COMPONENT_BYTES = 255
OCI_PROCESS_ENV_ALLOWLIST = frozenset(
    {
        "DOCKER_CONFIG",
        "HOME",
        "LANG",
        "LC_ALL",
        "PATH",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "TMPDIR",
    }
)

REQUIRED_IMAGE_REPOSITORIES = {
    "api": "ghcr.io/yanyuhanyue/animemo-api",
    "postgres": "docker.io/library/postgres",
    "redis": "docker.io/library/redis",
    "web": "ghcr.io/yanyuhanyue/animemo-web",
}
DOCKER_DAEMON_OFFICIAL_LIBRARY_DISPLAY_REPOSITORIES = {
    ("postgres", "docker.io/library/postgres"): "postgres",
    ("redis", "docker.io/library/redis"): "redis",
}


class OCIContractError(ValueError):
    """An OCI layout is not the exact immutable image DAG requested."""


@dataclass(frozen=True)
class OCIImageExpectation:
    role: str
    repository: str
    digest: str
    platform: str
    layout_path: str


@dataclass(frozen=True)
class VerifiedOCIImage:
    role: str
    repository: str
    digest: str
    platform: str
    layout: Path
    config_digest: str
    layer_digests: tuple[str, ...]


@dataclass(frozen=True)
class VerifiedOCIImageSet:
    images: tuple[VerifiedOCIImage, ...]

    def image(self, role: str) -> VerifiedOCIImage:
        for image in self.images:
            if image.role == role:
                return image
        raise OCIContractError("OCI_IMAGE_ROLE_NOT_VERIFIED")


class VerifiedOCIImageImporter(Protocol):
    """Runtime boundary that imports only a previously verified local image."""

    def import_verified_image(self, image: VerifiedOCIImage) -> str:
        """Return the exact repository@digest observed after local import."""


@dataclass(frozen=True)
class VerifiedLocalImageImportReceipt:
    images: tuple[str, ...]


class DockerOCIImporter:
    """Load an already verified OCI layout into the local Docker daemon."""

    def __init__(
        self,
        *,
        runner=None,
        environment: dict[str, str] | None = None,
        staging_parent: Path | None = None,
    ) -> None:
        self.runner = runner or CommandRunner()
        self.environment = (
            dict(environment)
            if environment is not None
            else {
                name: os.environ[name]
                for name in OCI_PROCESS_ENV_ALLOWLIST
                if name in os.environ
            }
        )
        self.staging_parent = (
            Path(staging_parent)
            if staging_parent is not None
            else Path(tempfile.gettempdir())
        )

    def import_verified_image(self, image: VerifiedOCIImage) -> str:
        if type(image) is not VerifiedOCIImage:
            raise OCIContractError("OCI_IMAGE_NOT_VERIFIED")
        if (
            image.role not in REQUIRED_IMAGE_REPOSITORIES
            or image.repository != REQUIRED_IMAGE_REPOSITORIES[image.role]
            or image.platform != OCI_PLATFORM
        ):
            raise OCIContractError("OCI_IMAGE_NOT_VERIFIED")
        refreshed = verify_oci_image(
            image.layout,
            OCIImageExpectation(
                role=image.role,
                repository=image.repository,
                digest=image.digest,
                platform=image.platform,
                layout_path=f"oci/{image.role}",
            ),
        )
        if refreshed != image:
            raise OCIContractError("OCI_IMAGE_NOT_VERIFIED")
        try:
            parent_metadata = self.staging_parent.lstat()
        except OSError as error:
            raise OCIContractError("OCI_IMPORT_STAGE_UNAVAILABLE") from error
        if _is_link_like(self.staging_parent) or not stat.S_ISDIR(
            parent_metadata.st_mode
        ):
            raise OCIContractError("OCI_IMPORT_STAGE_INVALID")
        stage = Path(
            tempfile.mkdtemp(prefix=".animemo-oci-import-", dir=self.staging_parent)
        )
        os.chmod(stage, 0o700)
        archive = stage / f"{image.role}.tar"
        expected = f"{image.repository}@{image.digest}"
        try:
            _write_verified_oci_archive(refreshed, archive)
            try:
                self.runner.run(
                    [
                        "/usr/bin/docker",
                        "image",
                        "load",
                        "--input",
                        str(archive),
                    ],
                    env=self.environment,
                    timeout=600,
                )
            except CommandFailed as error:
                raise OCIContractError("OCI_DOCKER_LOCAL_LOAD_FAILED") from error
            try:
                inspection = self.runner.run(
                    [
                        "/usr/bin/docker",
                        "image",
                        "inspect",
                        "--format",
                        "{{json .RepoDigests}}",
                        expected,
                    ],
                    env=self.environment,
                    timeout=60,
                )
            except CommandFailed as error:
                raise OCIContractError("OCI_DOCKER_POST_IMPORT_INSPECT_FAILED") from error
            try:
                observed = json.loads(inspection.stdout)
            except (AttributeError, TypeError, json.JSONDecodeError) as error:
                raise OCIContractError("OCI_DOCKER_POST_IMPORT_IDENTITY_INVALID") from error
            if (
                not isinstance(observed, list)
                or any(type(item) is not str for item in observed)
            ):
                raise OCIContractError("OCI_DOCKER_POST_IMPORT_DIGEST_MISMATCH")
            accepted_readbacks = {expected}
            display_repository = (
                DOCKER_DAEMON_OFFICIAL_LIBRARY_DISPLAY_REPOSITORIES.get(
                    (image.role, image.repository)
                )
            )
            if display_repository is not None:
                accepted_readbacks.add(
                    f"{display_repository}@{image.digest}"
                )
            if not accepted_readbacks.intersection(observed):
                raise OCIContractError("OCI_DOCKER_POST_IMPORT_DIGEST_MISMATCH")
            return expected
        finally:
            shutil.rmtree(stage, ignore_errors=True)


@dataclass(frozen=True)
class LocalImageAcquisitionEntry:
    role: str
    layout: Path
    digest: str
    target_reference: str


@dataclass(frozen=True)
class LocalImageAcquisitionPlan:
    entries: tuple[LocalImageAcquisitionEntry, ...]


@dataclass(frozen=True)
class AcquiredRuntimeImage:
    role: str
    canonical_reference: str
    observed_reference: str


@dataclass(frozen=True)
class ImageAcquisitionReceipt:
    verified_release_identity: str
    transport_policy_identity: str
    images: tuple[AcquiredRuntimeImage, ...]
    identity: str


class ImageAcquirer:
    """Acquire exact release-authorized images and verify runtime readback.

    The release transport policy is identity-bound here, but it never rewrites
    the canonical repository@digest authority.  A future mirror/import adapter
    must terminate at this same exact-digest readback seam.
    """

    def __init__(
        self,
        *,
        runner=None,
        environment: dict[str, str] | None = None,
        local_importer: VerifiedOCIImageImporter | None = None,
    ) -> None:
        self.runner = runner or CommandRunner()
        self.environment = (
            dict(environment)
            if environment is not None
            else {
                name: os.environ[name]
                for name in OCI_PROCESS_ENV_ALLOWLIST
                if name in os.environ
            }
        )
        self.local_importer = local_importer

    def acquire_local(
        self,
        materials: VerifiedReleaseMaterials,
        verified: VerifiedOCIImageSet,
        policy,
    ) -> ImageAcquisitionReceipt:
        """Import a locally verified four-image set without registry access."""

        from .local_bundle import LocalBundleTransportPolicy

        if type(materials) is not VerifiedReleaseMaterials:
            raise OCIContractError("OCI_RELEASE_MATERIALS_NOT_VERIFIED")
        if type(verified) is not VerifiedOCIImageSet:
            raise OCIContractError("OCI_IMAGE_SET_NOT_VERIFIED")
        if type(policy) is not LocalBundleTransportPolicy:
            raise OCIContractError("OCI_LOCAL_TRANSPORT_POLICY_INVALID")
        if policy.source != "local-bundle" or policy.fallback_allowed:
            raise OCIContractError("OCI_LOCAL_TRANSPORT_POLICY_INVALID")
        try:
            _validate_digest(materials.identity_digest)
        except OCIContractError as error:
            raise OCIContractError("OCI_RELEASE_IDENTITY_INVALID") from error
        for image in verified.images:
            expected = f"{image.repository}@{image.digest}"
            try:
                authoritative = materials.image(image.role)
            except Exception as error:
                raise OCIContractError("OCI_RELEASE_IMAGE_IDENTITY_INVALID") from error
            if authoritative != expected:
                raise OCIContractError("OCI_RELEASE_IMAGE_IDENTITY_MISMATCH")
        importer = self.local_importer or DockerOCIImporter(
            runner=self.runner,
            environment=self.environment,
        )
        imported = import_verified_oci_image_set(verified, importer)
        acquired = tuple(
            AcquiredRuntimeImage(
                role=image.role,
                canonical_reference=reference,
                observed_reference=reference,
            )
            for image, reference in zip(
                verified.images, imported.images, strict=True
            )
        )
        identity_document = {
            "images": [
                {
                    "canonical_reference": item.canonical_reference,
                    "observed_reference": item.observed_reference,
                    "role": item.role,
                }
                for item in acquired
            ],
            "receipt_version": 1,
            "transport_policy_identity": policy.identity,
            "verified_release_identity": materials.identity_digest,
        }
        identity = hashlib.sha256(
            json.dumps(
                identity_document,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("ascii")
        ).hexdigest()
        return ImageAcquisitionReceipt(
            verified_release_identity=materials.identity_digest,
            transport_policy_identity=policy.identity,
            images=acquired,
            identity=identity,
        )

    def acquire(
        self,
        materials: VerifiedReleaseMaterials,
        policy: ExplicitTransportPolicy,
    ) -> ImageAcquisitionReceipt:
        if type(materials) is not VerifiedReleaseMaterials:
            raise OCIContractError("OCI_RELEASE_MATERIALS_NOT_VERIFIED")
        if type(policy) is not ExplicitTransportPolicy:
            raise OCIContractError("OCI_TRANSPORT_POLICY_INVALID")
        if (
            not isinstance(materials.identity_digest, str)
            or len(materials.identity_digest) != 71
            or not materials.identity_digest.startswith("sha256:")
            or any(
                character not in "0123456789abcdef"
                for character in materials.identity_digest[7:]
            )
        ):
            raise OCIContractError("OCI_RELEASE_IDENTITY_INVALID")

        acquired: list[AcquiredRuntimeImage] = []
        for role in sorted(REQUIRED_IMAGE_REPOSITORIES):
            reference = materials.image(role)
            expected_repository = REQUIRED_IMAGE_REPOSITORIES[role]
            prefix = f"{expected_repository}@"
            if not reference.startswith(prefix):
                raise OCIContractError("OCI_CANONICAL_REPOSITORY_MISMATCH")
            _validate_digest(reference.removeprefix(prefix))
            try:
                self.runner.run(
                    ["/usr/bin/docker", "pull", reference],
                    env=self.environment,
                    timeout=600,
                )
                inspection = self.runner.run(
                    [
                        "/usr/bin/docker",
                        "image",
                        "inspect",
                        "--format",
                        "{{json .RepoDigests}}",
                        reference,
                    ],
                    env=self.environment,
                    timeout=60,
                )
            except CommandFailed as error:
                raise OCIContractError("OCI_IMAGE_ACQUISITION_FAILED") from error
            try:
                observed = json.loads(inspection.stdout)
            except (AttributeError, TypeError, json.JSONDecodeError) as error:
                raise OCIContractError("OCI_RUNTIME_IDENTITY_UNREADABLE") from error
            if (
                not isinstance(observed, list)
                or any(type(item) is not str for item in observed)
                or reference not in observed
            ):
                raise OCIContractError("OCI_RUNTIME_DIGEST_MISMATCH")
            acquired.append(
                AcquiredRuntimeImage(
                    role=role,
                    canonical_reference=reference,
                    observed_reference=reference,
                )
            )

        identity_document = {
            "images": [
                {
                    "canonical_reference": item.canonical_reference,
                    "observed_reference": item.observed_reference,
                    "role": item.role,
                }
                for item in acquired
            ],
            "receipt_version": 1,
            "transport_policy_identity": policy.identity,
            "verified_release_identity": materials.identity_digest,
        }
        identity = hashlib.sha256(
            json.dumps(
                identity_document,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("ascii")
        ).hexdigest()
        return ImageAcquisitionReceipt(
            verified_release_identity=materials.identity_digest,
            transport_policy_identity=policy.identity,
            images=tuple(acquired),
            identity=identity,
        )


def _digest(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _is_link_like(path: Path) -> bool:
    return path.is_symlink() or bool(getattr(path, "is_junction", lambda: False)())


def _validate_digest(value: Any) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 71
        or not value.startswith("sha256:")
        or any(character not in "0123456789abcdef" for character in value[7:])
    ):
        raise OCIContractError("OCI_DIGEST_INVALID")
    return value


def _validate_size(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise OCIContractError("OCI_DESCRIPTOR_SIZE_INVALID")
    return value


def _validate_relative_path(value: Any) -> str:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise OCIContractError("OCI_LAYOUT_PATH_INVALID")
    parsed = PurePosixPath(value)
    if parsed.is_absolute() or any(part in {"", ".", ".."} for part in parsed.parts):
        raise OCIContractError("OCI_LAYOUT_PATH_INVALID")
    if parsed.as_posix() != value or ":" in value:
        raise OCIContractError("OCI_LAYOUT_PATH_NOT_CANONICAL")
    if (
        unicodedata.normalize("NFC", value) != value
        or len(value.encode("utf-8")) > MAX_OCI_PATH_BYTES
        or len(parsed.parts) > MAX_OCI_PATH_DEPTH
    ):
        raise OCIContractError("OCI_LAYOUT_PATH_NOT_CANONICAL")
    for part in parsed.parts:
        if (
            len(part.encode("utf-8")) > MAX_OCI_PATH_COMPONENT_BYTES
            or part.endswith((" ", "."))
            or any(ord(character) < 32 for character in part)
        ):
            raise OCIContractError("OCI_LAYOUT_PATH_NOT_CANONICAL")
        stem = part.split(".", 1)[0].casefold()
        if stem in {"con", "prn", "aux", "nul"} or (
            len(stem) == 4 and stem[:3] in {"com", "lpt"} and stem[3] in "123456789"
        ):
            raise OCIContractError("OCI_LAYOUT_PATH_NOT_CANONICAL")
    return value


def _pairs_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    folded: set[str] = set()
    for key, value in pairs:
        if key in result or key.casefold() in folded:
            raise OCIContractError("OCI_JSON_DUPLICATE_KEY")
        result[key] = value
        folded.add(key.casefold())
    return result


def _parse_json(value: bytes) -> dict[str, Any]:
    try:
        parsed = json.loads(
            value.decode("utf-8"),
            object_pairs_hook=_pairs_without_duplicates,
            parse_constant=lambda constant: (_ for _ in ()).throw(
                OCIContractError("OCI_JSON_NON_FINITE")
            ),
        )
    except OCIContractError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise OCIContractError("OCI_JSON_INVALID") from error
    if not isinstance(parsed, dict):
        raise OCIContractError("OCI_JSON_OBJECT_REQUIRED")
    return parsed


def _open_regular_file(path: Path, *, max_bytes: int):
    try:
        before = path.lstat()
    except OSError as error:
        raise OCIContractError("OCI_FILE_UNAVAILABLE") from error
    if _is_link_like(path) or not stat.S_ISREG(before.st_mode):
        raise OCIContractError("OCI_FILE_TYPE_FORBIDDEN")
    if before.st_nlink != 1:
        raise OCIContractError("OCI_HARDLINK_FORBIDDEN")
    if before.st_size < 0 or before.st_size > max_bytes:
        raise OCIContractError("OCI_FILE_SIZE_LIMIT")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise OCIContractError("OCI_FILE_UNREADABLE") from error
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or opened.st_size != before.st_size
            or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
        ):
            raise OCIContractError("OCI_FILE_CHANGED_DURING_OPEN")
        return os.fdopen(descriptor, "rb", closefd=True), opened.st_size
    except BaseException:
        os.close(descriptor)
        raise


def _read_regular_file(path: Path, *, max_bytes: int = MAX_OCI_METADATA_BYTES) -> bytes:
    stream, expected_size = _open_regular_file(path, max_bytes=max_bytes)
    value = bytearray()
    with stream:
        while True:
            chunk = stream.read(min(OCI_STREAM_CHUNK_BYTES, max_bytes + 1 - len(value)))
            if not chunk:
                break
            value.extend(chunk)
            if len(value) > max_bytes:
                raise OCIContractError("OCI_FILE_SIZE_LIMIT")
        after = os.fstat(stream.fileno())
    if len(value) != expected_size or after.st_size != expected_size:
        raise OCIContractError("OCI_FILE_CHANGED_DURING_READ")
    return bytes(value)


def _expect_keys(value: dict[str, Any], expected: set[str], code: str) -> None:
    if set(value) != expected:
        raise OCIContractError(code)


def _descriptor(
    value: Any,
    *,
    media_type: str,
    with_platform: bool,
) -> tuple[str, int]:
    if not isinstance(value, dict):
        raise OCIContractError("OCI_DESCRIPTOR_INVALID")
    expected = {"digest", "mediaType", "size"}
    if with_platform:
        expected.add("platform")
    _expect_keys(value, expected, "OCI_DESCRIPTOR_FIELDS_INVALID")
    if value["mediaType"] != media_type:
        raise OCIContractError("OCI_DESCRIPTOR_MEDIA_TYPE_INVALID")
    if with_platform:
        platform = value["platform"]
        if not isinstance(platform, dict):
            raise OCIContractError("OCI_DESCRIPTOR_PLATFORM_INVALID")
        _expect_keys(
            platform,
            {"architecture", "os"},
            "OCI_DESCRIPTOR_PLATFORM_FIELDS_INVALID",
        )
        if platform != {"architecture": "amd64", "os": "linux"}:
            raise OCIContractError("OCI_PLATFORM_MISMATCH")
    return _validate_digest(value["digest"]), _validate_size(value["size"])


def _closed_oci_annotations(value: Any, *, role: str) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) != OBSERVED_OCI_ANNOTATION_KEYS:
        raise OCIContractError("OCI_ANNOTATION_FIELDS_INVALID")
    for item in value.values():
        if (
            type(item) is not str
            or not item
            or any(ord(character) < 32 for character in item)
        ):
            raise OCIContractError("OCI_ANNOTATION_VALUE_INVALID")
        try:
            encoded = item.encode("utf-8")
        except UnicodeEncodeError as error:
            raise OCIContractError("OCI_ANNOTATION_VALUE_INVALID") from error
        if len(encoded) > 4096:
            raise OCIContractError("OCI_ANNOTATION_VALUE_INVALID")
    if value["com.docker.official-images.bashbrew.arch"] != "amd64":
        raise OCIContractError("OCI_ANNOTATION_VALUE_INVALID")
    try:
        _validate_digest(value["org.opencontainers.image.base.digest"])
    except OCIContractError as error:
        raise OCIContractError("OCI_ANNOTATION_VALUE_INVALID") from error
    for name in ("org.opencontainers.image.source", "org.opencontainers.image.url"):
        if not value[name].startswith("https://") or any(
            character.isspace() for character in value[name]
        ):
            raise OCIContractError("OCI_ANNOTATION_VALUE_INVALID")
    if value != OBSERVED_CRANE_OCI_ANNOTATIONS.get(role):
        raise OCIContractError("OCI_ANNOTATION_VALUE_INVALID")
    return value


def _manifest_profile(
    manifest: dict[str, Any], *, role: str
) -> tuple[str, str, frozenset[str]]:
    base_fields = {"config", "layers", "mediaType", "schemaVersion"}
    fields = set(manifest)
    media_type = manifest.get("mediaType")
    if media_type == OCI_IMAGE_MANIFEST_MEDIA_TYPE:
        if fields not in (base_fields, base_fields | {"annotations"}):
            raise OCIContractError("OCI_MANIFEST_FIELDS_INVALID")
        if "annotations" in manifest:
            _closed_oci_annotations(manifest["annotations"], role=role)
        config_media_type = OCI_CONFIG_MEDIA_TYPE
        layer_media_types = OCI_LAYER_MEDIA_TYPES
        profile = "oci-v1"
    elif media_type == DOCKER_SCHEMA2_MANIFEST_MEDIA_TYPE:
        if fields != base_fields:
            raise OCIContractError("OCI_MANIFEST_FIELDS_INVALID")
        config_media_type = DOCKER_SCHEMA2_CONFIG_MEDIA_TYPE
        layer_media_types = frozenset({DOCKER_SCHEMA2_LAYER_GZIP_MEDIA_TYPE})
        profile = "docker-schema2"
    else:
        raise OCIContractError("OCI_MANIFEST_IDENTITY_INVALID")
    if manifest.get("schemaVersion") != 2:
        raise OCIContractError("OCI_MANIFEST_IDENTITY_INVALID")
    return profile, config_media_type, layer_media_types


def _blob(
    layout: Path,
    digest: str,
    size: int,
    *,
    capture: bool = False,
) -> bytes | None:
    digest = _validate_digest(digest)
    if size > MAX_OCI_BLOB_BYTES:
        raise OCIContractError("OCI_BLOB_SIZE_LIMIT")
    target = layout / "blobs" / "sha256" / digest[7:]
    stream, expected_size = _open_regular_file(target, max_bytes=MAX_OCI_BLOB_BYTES)
    if expected_size != size:
        stream.close()
        raise OCIContractError("OCI_DESCRIPTOR_SIZE_MISMATCH")
    hasher = hashlib.sha256()
    captured = bytearray() if capture else None
    consumed = 0
    with stream:
        while True:
            chunk = stream.read(OCI_STREAM_CHUNK_BYTES)
            if not chunk:
                break
            consumed += len(chunk)
            if consumed > size:
                raise OCIContractError("OCI_DESCRIPTOR_SIZE_MISMATCH")
            hasher.update(chunk)
            if captured is not None:
                if consumed > MAX_OCI_METADATA_BYTES:
                    raise OCIContractError("OCI_METADATA_SIZE_LIMIT")
                captured.extend(chunk)
        after = os.fstat(stream.fileno())
    if consumed != size or after.st_size != size:
        raise OCIContractError("OCI_DESCRIPTOR_SIZE_MISMATCH")
    if "sha256:" + hasher.hexdigest() != digest:
        raise OCIContractError("OCI_DESCRIPTOR_DIGEST_MISMATCH")
    return bytes(captured) if captured is not None else None


def _layer_descriptor(
    value: Any, *, allowed_media_types: frozenset[str] = OCI_LAYER_MEDIA_TYPES
) -> tuple[str, int, str]:
    if not isinstance(value, dict):
        raise OCIContractError("OCI_DESCRIPTOR_INVALID")
    media_type = value.get("mediaType")
    if type(media_type) is not str or media_type not in allowed_media_types:
        raise OCIContractError("OCI_DESCRIPTOR_MEDIA_TYPE_INVALID")
    digest, size = _descriptor(
        value,
        media_type=media_type,
        with_platform=False,
    )
    return digest, size, media_type


def _verify_layer_blob(
    layout: Path,
    *,
    digest: str,
    size: int,
    media_type: str,
    diff_id: str,
    remaining_image_bytes: int,
) -> int:
    digest = _validate_digest(digest)
    diff_id = _validate_digest(diff_id)
    if size > MAX_OCI_BLOB_BYTES:
        raise OCIContractError("OCI_BLOB_SIZE_LIMIT")
    target = layout / "blobs" / "sha256" / digest[7:]
    stream, expected_size = _open_regular_file(target, max_bytes=MAX_OCI_BLOB_BYTES)
    if expected_size != size:
        stream.close()
        raise OCIContractError("OCI_DESCRIPTOR_SIZE_MISMATCH")
    stored_hasher = hashlib.sha256()
    diff_hasher = hashlib.sha256()
    stored_bytes = 0
    uncompressed_bytes = 0
    decoder = None
    if media_type in {
        OCI_LAYER_GZIP_MEDIA_TYPE,
        DOCKER_SCHEMA2_LAYER_GZIP_MEDIA_TYPE,
    }:
        decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)

    def consume_uncompressed(value: bytes) -> None:
        nonlocal uncompressed_bytes
        uncompressed_bytes += len(value)
        if uncompressed_bytes > MAX_OCI_LAYER_UNCOMPRESSED_BYTES:
            raise OCIContractError("OCI_LAYER_UNCOMPRESSED_SIZE_LIMIT")
        if uncompressed_bytes > remaining_image_bytes:
            raise OCIContractError("OCI_LAYERS_UNCOMPRESSED_SIZE_LIMIT")
        diff_hasher.update(value)

    with stream:
        while True:
            chunk = stream.read(OCI_STREAM_CHUNK_BYTES)
            if not chunk:
                break
            stored_bytes += len(chunk)
            if stored_bytes > size:
                raise OCIContractError("OCI_DESCRIPTOR_SIZE_MISMATCH")
            stored_hasher.update(chunk)
            if decoder is None:
                consume_uncompressed(chunk)
                continue
            pending = chunk
            while pending:
                previous_size = len(pending)
                try:
                    uncompressed = decoder.decompress(
                        pending,
                        OCI_STREAM_CHUNK_BYTES,
                    )
                except zlib.error as error:
                    raise OCIContractError("OCI_LAYER_GZIP_INVALID") from error
                pending = decoder.unconsumed_tail
                consume_uncompressed(uncompressed)
                if decoder.eof:
                    if decoder.unused_data or pending or stored_bytes != size:
                        raise OCIContractError("OCI_LAYER_GZIP_TRAILING_DATA")
                    break
                if pending and len(pending) == previous_size and not uncompressed:
                    raise OCIContractError("OCI_LAYER_GZIP_INVALID")
        after = os.fstat(stream.fileno())
    if stored_bytes != size or after.st_size != size:
        raise OCIContractError("OCI_DESCRIPTOR_SIZE_MISMATCH")
    if "sha256:" + stored_hasher.hexdigest() != digest:
        raise OCIContractError("OCI_DESCRIPTOR_DIGEST_MISMATCH")
    if decoder is not None and not decoder.eof:
        raise OCIContractError("OCI_LAYER_GZIP_INVALID")
    if "sha256:" + diff_hasher.hexdigest() != diff_id:
        raise OCIContractError("OCI_LAYER_DIFF_ID_MISMATCH")
    return uncompressed_bytes


def _closed_layout(layout: Path) -> tuple[set[str], set[str]]:
    try:
        root_stat = layout.lstat()
    except OSError as error:
        raise OCIContractError("OCI_LAYOUT_UNAVAILABLE") from error
    if _is_link_like(layout) or not stat.S_ISDIR(root_stat.st_mode):
        raise OCIContractError("OCI_LAYOUT_ROOT_INVALID")
    result: set[str] = set()
    directories_seen: set[str] = set()
    folded: set[str] = set()
    total_size = 0
    for current, directories, names in os.walk(layout, topdown=True, followlinks=False):
        current_path = Path(current)
        for name in directories:
            directory = current_path / name
            try:
                metadata = directory.lstat()
            except OSError as error:
                raise OCIContractError("OCI_DIRECTORY_UNAVAILABLE") from error
            if _is_link_like(directory) or not stat.S_ISDIR(metadata.st_mode):
                raise OCIContractError("OCI_DIRECTORY_TYPE_FORBIDDEN")
            relative = directory.relative_to(layout).as_posix()
            directories_seen.add(_validate_relative_path(relative))
            if len(directories_seen) > MAX_OCI_LAYOUT_DIRECTORIES:
                raise OCIContractError("OCI_LAYOUT_DIRECTORY_LIMIT")
        for name in names:
            target = current_path / name
            try:
                metadata = target.lstat()
            except OSError as error:
                raise OCIContractError("OCI_FILE_UNAVAILABLE") from error
            relative = target.relative_to(layout).as_posix()
            relative = _validate_relative_path(relative)
            if relative.casefold() in folded:
                raise OCIContractError("OCI_CASE_COLLISION")
            result.add(relative)
            folded.add(relative.casefold())
            total_size += metadata.st_size
            if len(result) > MAX_OCI_LAYOUT_FILES:
                raise OCIContractError("OCI_LAYOUT_FILE_LIMIT")
            if total_size > MAX_OCI_LAYOUT_TOTAL_BYTES:
                raise OCIContractError("OCI_LAYOUT_TOTAL_SIZE_LIMIT")
    return result, directories_seen


class _HashingReader:
    def __init__(self, stream, *, capture: bool) -> None:
        self.stream = stream
        self.hasher = hashlib.sha256()
        self.consumed = 0
        self.captured = bytearray() if capture else None

    def read(self, size: int = -1) -> bytes:
        chunk = self.stream.read(size)
        self.consumed += len(chunk)
        self.hasher.update(chunk)
        if self.captured is not None:
            if self.consumed > MAX_OCI_METADATA_BYTES:
                raise OCIContractError("OCI_METADATA_SIZE_LIMIT")
            self.captured.extend(chunk)
        return chunk


def _oci_tar_info(path: str, size: int) -> tarfile.TarInfo:
    member = tarfile.TarInfo(path)
    member.size = size
    member.mode = 0o644
    member.uid = 0
    member.gid = 0
    member.uname = ""
    member.gname = ""
    member.mtime = 0
    member.type = tarfile.REGTYPE
    return member


def _write_verified_oci_archive(image: VerifiedOCIImage, archive: Path) -> None:
    frozen_index = _parse_json(
        _read_regular_file(image.layout / "index.json")
    )
    _expect_keys(
        frozen_index,
        {"manifests", "mediaType", "schemaVersion"},
        "OCI_IMPORT_INDEX_FIELDS_INVALID",
    )
    if (
        frozen_index["schemaVersion"] != 2
        or frozen_index["mediaType"] != OCI_IMAGE_INDEX_MEDIA_TYPE
    ):
        raise OCIContractError("OCI_IMPORT_INDEX_IDENTITY_INVALID")
    manifests = frozen_index["manifests"]
    if not isinstance(manifests, list) or len(manifests) != 1:
        raise OCIContractError("OCI_IMPORT_INDEX_MANIFEST_COUNT_INVALID")
    manifest_media_type = (
        manifests[0].get("mediaType") if isinstance(manifests[0], dict) else None
    )
    if manifest_media_type not in SUPPORTED_MANIFEST_MEDIA_TYPES:
        raise OCIContractError("OCI_IMPORT_INDEX_IDENTITY_INVALID")
    manifest_digest, _ = _descriptor(
        manifests[0],
        media_type=manifest_media_type,
        with_platform=True,
    )
    if manifest_digest != image.digest:
        raise OCIContractError("OCI_IMPORT_INDEX_DIGEST_MISMATCH")
    derived_reference = (
        f"{image.repository}:{DERIVED_IMPORT_TAG_PREFIX}{image.digest[7:]}"
    )
    derived_descriptor = dict(manifests[0])
    derived_descriptor["annotations"] = {
        OCI_REF_NAME_ANNOTATION: derived_reference,
    }
    derived_index = json.dumps(
        {
            "manifests": [derived_descriptor],
            "mediaType": OCI_IMAGE_INDEX_MEDIA_TYPE,
            "schemaVersion": 2,
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    blob_paths = {
        f"blobs/sha256/{image.digest[7:]}",
        f"blobs/sha256/{image.config_digest[7:]}",
        *(f"blobs/sha256/{digest[7:]}" for digest in image.layer_digests),
    }
    paths = ["index.json", "oci-layout"] + sorted(blob_paths)
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(archive, flags, 0o600)
    except OSError as error:
        raise OCIContractError("OCI_IMPORT_ARCHIVE_CREATE_FAILED") from error
    metadata: dict[str, bytes] = {}
    try:
        os.chmod(archive, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as output:
            descriptor = -1
            with tarfile.open(
                fileobj=output, mode="w:", format=tarfile.USTAR_FORMAT
            ) as handle:
                for relative in paths:
                    if relative == "index.json":
                        stream, size = io.BytesIO(derived_index), len(derived_index)
                    else:
                        source = image.layout.joinpath(*PurePosixPath(relative).parts)
                        stream, size = _open_regular_file(
                            source, max_bytes=MAX_OCI_BLOB_BYTES
                        )
                    with stream:
                        reader = _HashingReader(
                            stream, capture=relative == "oci-layout"
                        )
                        handle.addfile(_oci_tar_info(relative, size), fileobj=reader)
                        if reader.consumed != size:
                            raise OCIContractError("OCI_IMPORT_ARCHIVE_SIZE_MISMATCH")
                        if relative.startswith("blobs/sha256/"):
                            expected = "sha256:" + relative.rsplit("/", 1)[1]
                            if "sha256:" + reader.hasher.hexdigest() != expected:
                                raise OCIContractError(
                                    "OCI_IMPORT_ARCHIVE_DIGEST_MISMATCH"
                                )
                        elif relative == "oci-layout":
                            assert reader.captured is not None
                            metadata[relative] = bytes(reader.captured)
            output.flush()
            os.fsync(output.fileno())
        if _parse_json(metadata["oci-layout"]) != {"imageLayoutVersion": "1.0.0"}:
            raise OCIContractError("OCI_IMPORT_LAYOUT_VERSION_INVALID")
    except BaseException:
        archive.unlink(missing_ok=True)
        raise
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _expectation(value: Any) -> OCIImageExpectation:
    if not isinstance(value, dict):
        raise OCIContractError("OCI_IMAGE_EXPECTATION_INVALID")
    _expect_keys(
        value,
        {"digest", "layoutPath", "platform", "repository", "role"},
        "OCI_IMAGE_EXPECTATION_FIELDS_INVALID",
    )
    role = value["role"]
    if not isinstance(role, str) or role not in REQUIRED_IMAGE_REPOSITORIES:
        raise OCIContractError("OCI_IMAGE_ROLE_INVALID")
    if value["repository"] != REQUIRED_IMAGE_REPOSITORIES[role]:
        raise OCIContractError("OCI_IMAGE_REPOSITORY_MISMATCH")
    if value["platform"] != OCI_PLATFORM:
        raise OCIContractError("OCI_PLATFORM_MISMATCH")
    layout_path = _validate_relative_path(value["layoutPath"])
    if layout_path != f"oci/{role}":
        raise OCIContractError("OCI_LAYOUT_ROLE_MISMATCH")
    return OCIImageExpectation(
        role=role,
        repository=value["repository"],
        digest=_validate_digest(value["digest"]),
        platform=value["platform"],
        layout_path=layout_path,
    )


def _validated_expectation(value: OCIImageExpectation) -> OCIImageExpectation:
    if type(value) is not OCIImageExpectation:
        raise OCIContractError("OCI_IMAGE_EXPECTATION_INVALID")
    validated = _expectation(
        {
            "digest": value.digest,
            "layoutPath": value.layout_path,
            "platform": value.platform,
            "repository": value.repository,
            "role": value.role,
        }
    )
    if validated != value:
        raise OCIContractError("OCI_IMAGE_EXPECTATION_INVALID")
    return validated


def _atomic_replace_index(path: Path, original: bytes, replacement: bytes) -> None:
    if _read_regular_file(path) != original:
        raise OCIContractError("OCI_INDEX_CHANGED_DURING_NORMALIZATION")
    descriptor = -1
    temporary: Path | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".animemo-index-normalize-", dir=path.parent
        )
        temporary = Path(temporary_name)
        os.chmod(temporary, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            descriptor = -1
            stream.write(replacement)
            stream.flush()
            os.fsync(stream.fileno())
        if _read_regular_file(path) != original:
            raise OCIContractError("OCI_INDEX_CHANGED_DURING_NORMALIZATION")
        os.replace(temporary, path)
        temporary = None
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _bound_crane_layout(
    source_root: Path, layout: Path, expectation: OCIImageExpectation
) -> Path:
    source_root = Path(source_root)
    layout = Path(layout)
    try:
        source_metadata = source_root.lstat()
    except OSError as error:
        raise OCIContractError("OCI_SOURCE_ROOT_UNAVAILABLE") from error
    if _is_link_like(source_root) or not stat.S_ISDIR(source_metadata.st_mode):
        raise OCIContractError("OCI_SOURCE_ROOT_INVALID")
    expected = source_root.joinpath(*PurePosixPath(expectation.layout_path).parts)
    if os.path.normcase(os.path.abspath(layout)) != os.path.normcase(
        os.path.abspath(expected)
    ):
        raise OCIContractError("OCI_LAYOUT_PATH_MISMATCH")
    oci_root = source_root / "oci"
    try:
        oci_metadata = oci_root.lstat()
    except OSError as error:
        raise OCIContractError("OCI_LAYOUT_PARENT_UNAVAILABLE") from error
    if _is_link_like(oci_root) or not stat.S_ISDIR(oci_metadata.st_mode):
        raise OCIContractError("OCI_LAYOUT_PARENT_INVALID")
    return layout


def normalize_crane_oci_layout(
    layout: Path,
    expectation: OCIImageExpectation,
    *,
    source_root: Path,
) -> dict[str, object]:
    """Canonicalize only crane's observed root descriptor wrapper.

    The manifest, config, layers, and authoritative manifest digest remain byte-for-byte
    bound. Both supported whole-image profiles are closed; media types are never
    translated between Docker schema2 and OCI v1.
    """

    expectation = _validated_expectation(expectation)
    layout = _bound_crane_layout(source_root, layout, expectation)
    actual_files, actual_directories = _closed_layout(layout)
    if _parse_json(_read_regular_file(layout / "oci-layout")) != {
        "imageLayoutVersion": "1.0.0"
    }:
        raise OCIContractError("OCI_LAYOUT_VERSION_INVALID")
    index_path = layout / "index.json"
    original = _read_regular_file(index_path)
    index = _parse_json(original)
    _expect_keys(
        index,
        {"manifests", "mediaType", "schemaVersion"},
        "OCI_INDEX_FIELDS_INVALID",
    )
    if index["schemaVersion"] != 2 or index["mediaType"] != OCI_IMAGE_INDEX_MEDIA_TYPE:
        raise OCIContractError("OCI_INDEX_IDENTITY_INVALID")
    manifests = index["manifests"]
    if not isinstance(manifests, list) or len(manifests) != 1:
        raise OCIContractError("OCI_INDEX_MANIFEST_COUNT_INVALID")
    descriptor_value = manifests[0]
    if not isinstance(descriptor_value, dict):
        raise OCIContractError("OCI_DESCRIPTOR_INVALID")
    fields_before = set(descriptor_value)
    canonical_fields = {"digest", "mediaType", "platform", "size"}
    raw_fields = {"artifactType", "digest", "mediaType", "size"}
    raw_annotated_fields = raw_fields | {"annotations"}
    if fields_before == canonical_fields:
        verified, profile = _verify_oci_image_index(
            layout,
            expectation,
            index,
            actual_files=actual_files,
            actual_directories=actual_directories,
        )
        if verified.digest != expectation.digest:
            raise OCIContractError("OCI_EXPECTED_MANIFEST_DIGEST_MISMATCH")
        return {
            "authoritativeDigestRewritten": False,
            "changed": False,
            "digest": expectation.digest,
            "profile": profile,
            "role": expectation.role,
            "rootFieldsAfter": sorted(canonical_fields),
            "rootFieldsBefore": sorted(canonical_fields),
        }
    if fields_before not in (raw_fields, raw_annotated_fields):
        raise OCIContractError("OCI_CRANE_DESCRIPTOR_FIELDS_INVALID")
    media_type = descriptor_value.get("mediaType")
    if media_type not in SUPPORTED_MANIFEST_MEDIA_TYPES:
        raise OCIContractError("OCI_DESCRIPTOR_MEDIA_TYPE_INVALID")
    manifest_digest = _validate_digest(descriptor_value.get("digest"))
    manifest_size = _validate_size(descriptor_value.get("size"))
    if manifest_digest != expectation.digest:
        raise OCIContractError("OCI_EXPECTED_MANIFEST_DIGEST_MISMATCH")
    manifest_bytes = _blob(layout, manifest_digest, manifest_size, capture=True)
    assert manifest_bytes is not None
    manifest = _parse_json(manifest_bytes)
    profile, config_media_type, _ = _manifest_profile(manifest, role=expectation.role)
    if profile != OBSERVED_CRANE_PROFILE_BY_ROLE[expectation.role]:
        raise OCIContractError("OCI_CRANE_ROLE_PROFILE_INVALID")
    if manifest["mediaType"] != media_type:
        raise OCIContractError("OCI_IMAGE_PROFILE_INVALID")
    config_descriptor = manifest.get("config")
    if (
        not isinstance(config_descriptor, dict)
        or config_descriptor.get("mediaType") != config_media_type
    ):
        raise OCIContractError("OCI_IMAGE_PROFILE_INVALID")
    if descriptor_value.get("artifactType") != config_media_type:
        raise OCIContractError("OCI_CRANE_ARTIFACT_TYPE_INVALID")
    if profile == "docker-schema2":
        if fields_before != raw_fields:
            raise OCIContractError("OCI_CRANE_DESCRIPTOR_FIELDS_INVALID")
    else:
        if fields_before != raw_annotated_fields:
            raise OCIContractError("OCI_CRANE_DESCRIPTOR_FIELDS_INVALID")
        root_annotations = _closed_oci_annotations(
            descriptor_value["annotations"], role=expectation.role
        )
        if manifest.get("annotations") != root_annotations:
            raise OCIContractError("OCI_ANNOTATION_BINDING_MISMATCH")
    config_digest, config_size = _descriptor(
        config_descriptor, media_type=config_media_type, with_platform=False
    )
    config_bytes = _blob(layout, config_digest, config_size, capture=True)
    assert config_bytes is not None
    config = _parse_json(config_bytes)
    if config.get("os") != "linux" or config.get("architecture") != "amd64":
        raise OCIContractError("OCI_CONFIG_PLATFORM_MISMATCH")
    canonical_descriptor = {
        "digest": manifest_digest,
        "mediaType": media_type,
        "platform": {"architecture": "amd64", "os": "linux"},
        "size": manifest_size,
    }
    normalized_index = dict(index)
    normalized_index["manifests"] = [canonical_descriptor]
    _verify_oci_image_index(
        layout,
        expectation,
        normalized_index,
        actual_files=actual_files,
        actual_directories=actual_directories,
    )
    replacement = json.dumps(
        normalized_index,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    _atomic_replace_index(index_path, original, replacement)
    verify_oci_image(layout, expectation)
    return {
        "authoritativeDigestRewritten": False,
        "changed": True,
        "digest": expectation.digest,
        "profile": profile,
        "role": expectation.role,
        "rootFieldsAfter": sorted(canonical_fields),
        "rootFieldsBefore": sorted(fields_before),
    }


def _verify_oci_image_index(
    layout: Path,
    expectation: OCIImageExpectation,
    index: dict[str, Any],
    *,
    actual_files: set[str],
    actual_directories: set[str],
) -> tuple[VerifiedOCIImage, str]:
    _expect_keys(
        index,
        {"manifests", "mediaType", "schemaVersion"},
        "OCI_INDEX_FIELDS_INVALID",
    )
    if index["schemaVersion"] != 2 or index["mediaType"] != OCI_IMAGE_INDEX_MEDIA_TYPE:
        raise OCIContractError("OCI_INDEX_IDENTITY_INVALID")
    manifests = index["manifests"]
    if not isinstance(manifests, list) or len(manifests) != 1:
        raise OCIContractError("OCI_INDEX_MANIFEST_COUNT_INVALID")
    root_descriptor = manifests[0]
    root_media_type = (
        root_descriptor.get("mediaType") if isinstance(root_descriptor, dict) else None
    )
    if root_media_type not in SUPPORTED_MANIFEST_MEDIA_TYPES:
        raise OCIContractError("OCI_DESCRIPTOR_MEDIA_TYPE_INVALID")
    manifest_digest, manifest_size = _descriptor(
        root_descriptor, media_type=root_media_type, with_platform=True
    )
    if manifest_digest != expectation.digest:
        raise OCIContractError("OCI_EXPECTED_MANIFEST_DIGEST_MISMATCH")
    manifest_bytes = _blob(layout, manifest_digest, manifest_size, capture=True)
    assert manifest_bytes is not None
    manifest = _parse_json(manifest_bytes)
    profile, config_media_type, layer_media_types = _manifest_profile(
        manifest, role=expectation.role
    )
    if manifest["mediaType"] != root_media_type:
        raise OCIContractError("OCI_IMAGE_PROFILE_INVALID")
    config_descriptor = manifest.get("config")
    if (
        not isinstance(config_descriptor, dict)
        or config_descriptor.get("mediaType") != config_media_type
    ):
        raise OCIContractError("OCI_IMAGE_PROFILE_INVALID")
    config_digest, config_size = _descriptor(
        config_descriptor, media_type=config_media_type, with_platform=False
    )
    config_bytes = _blob(layout, config_digest, config_size, capture=True)
    assert config_bytes is not None
    config = _parse_json(config_bytes)
    if config.get("os") != "linux" or config.get("architecture") != "amd64":
        raise OCIContractError("OCI_CONFIG_PLATFORM_MISMATCH")
    layers = manifest["layers"]
    if not isinstance(layers, list) or len(layers) > MAX_OCI_LAYERS:
        raise OCIContractError("OCI_LAYERS_INVALID")
    rootfs = config.get("rootfs")
    if not isinstance(rootfs, dict) or rootfs.get("type") != "layers":
        raise OCIContractError("OCI_ROOTFS_INVALID")
    diff_ids = rootfs.get("diff_ids")
    if not isinstance(diff_ids, list) or len(diff_ids) != len(layers):
        raise OCIContractError("OCI_ROOTFS_DIFF_IDS_INVALID")
    try:
        validated_diff_ids = tuple(_validate_digest(value) for value in diff_ids)
    except OCIContractError as error:
        raise OCIContractError("OCI_ROOTFS_DIFF_IDS_INVALID") from error
    layer_digests: list[str] = []
    uncompressed_layer_bytes = 0
    for layer, diff_id in zip(layers, validated_diff_ids, strict=True):
        layer_digest, layer_size, layer_media_type = _layer_descriptor(
            layer, allowed_media_types=layer_media_types
        )
        if layer_digest in layer_digests:
            raise OCIContractError("OCI_LAYER_DUPLICATE")
        uncompressed_layer_bytes += _verify_layer_blob(
            layout,
            digest=layer_digest,
            size=layer_size,
            media_type=layer_media_type,
            diff_id=diff_id,
            remaining_image_bytes=(
                MAX_OCI_IMAGE_UNCOMPRESSED_LAYER_BYTES - uncompressed_layer_bytes
            ),
        )
        layer_digests.append(layer_digest)
    expected_files = {
        "index.json",
        "oci-layout",
        f"blobs/sha256/{manifest_digest[7:]}",
        f"blobs/sha256/{config_digest[7:]}",
        *(f"blobs/sha256/{value[7:]}" for value in layer_digests),
    }
    if actual_files != expected_files or actual_directories != {
        "blobs",
        "blobs/sha256",
    }:
        raise OCIContractError("OCI_LAYOUT_DAG_NOT_CLOSED")
    return (
        VerifiedOCIImage(
            role=expectation.role,
            repository=expectation.repository,
            digest=expectation.digest,
            platform=expectation.platform,
            layout=layout,
            config_digest=config_digest,
            layer_digests=tuple(layer_digests),
        ),
        profile,
    )


def verify_oci_image(
    layout: Path, expectation: OCIImageExpectation
) -> VerifiedOCIImage:
    layout = Path(layout)
    actual_files, actual_directories = _closed_layout(layout)
    layout_document = _parse_json(_read_regular_file(layout / "oci-layout"))
    if layout_document != {"imageLayoutVersion": "1.0.0"}:
        raise OCIContractError("OCI_LAYOUT_VERSION_INVALID")
    index = _parse_json(_read_regular_file(layout / "index.json"))
    verified, _ = _verify_oci_image_index(
        layout,
        expectation,
        index,
        actual_files=actual_files,
        actual_directories=actual_directories,
    )
    return verified


def verify_oci_image_set(root: Path, images: Any) -> VerifiedOCIImageSet:
    if not isinstance(images, list):
        raise OCIContractError("OCI_IMAGE_SET_INVALID")
    expectations = tuple(_expectation(image) for image in images)
    roles = [expectation.role for expectation in expectations]
    required = sorted(REQUIRED_IMAGE_REPOSITORIES)
    if roles != required:
        if len(roles) != len(set(roles)):
            raise OCIContractError("OCI_IMAGE_ROLE_DUPLICATE")
        if len({role.casefold() for role in roles}) != len(roles):
            raise OCIContractError("OCI_IMAGE_ROLE_CASE_COLLISION")
        raise OCIContractError("OCI_IMAGE_ROLES_INCOMPLETE_OR_UNORDERED")
    root = Path(root)
    verified = tuple(
        verify_oci_image(
            root.joinpath(*PurePosixPath(expectation.layout_path).parts),
            expectation,
        )
        for expectation in expectations
    )
    return VerifiedOCIImageSet(verified)


def plan_local_image_acquisition(
    verified: VerifiedOCIImageSet,
) -> LocalImageAcquisitionPlan:
    if not isinstance(verified, VerifiedOCIImageSet):
        raise OCIContractError("OCI_IMAGE_SET_NOT_VERIFIED")
    entries = tuple(
        LocalImageAcquisitionEntry(
            role=image.role,
            layout=image.layout,
            digest=image.digest,
            target_reference=f"{image.repository}@{image.digest}",
        )
        for image in verified.images
    )
    return LocalImageAcquisitionPlan(entries)


def import_verified_oci_image_set(
    verified: VerifiedOCIImageSet,
    importer: VerifiedOCIImageImporter,
) -> VerifiedLocalImageImportReceipt:
    """Import exact local layouts without weakening their release identity.

    This seam deliberately owns no registry or tag fallback.  The injected
    importer may touch the local runtime, but acceptance remains bound to the
    already verified canonical repository@digest returned by that importer.
    """

    if type(verified) is not VerifiedOCIImageSet:
        raise OCIContractError("OCI_IMAGE_SET_NOT_VERIFIED")
    if [image.role for image in verified.images] != sorted(REQUIRED_IMAGE_REPOSITORIES):
        raise OCIContractError("OCI_IMAGE_SET_NOT_VERIFIED")
    operation = getattr(importer, "import_verified_image", None)
    if not callable(operation):
        raise OCIContractError("OCI_LOCAL_IMPORTER_INVALID")
    refreshed_images: list[VerifiedOCIImage] = []
    for image in verified.images:
        if (
            image.repository != REQUIRED_IMAGE_REPOSITORIES[image.role]
            or image.platform != OCI_PLATFORM
        ):
            raise OCIContractError("OCI_IMAGE_SET_NOT_VERIFIED")
        refreshed = verify_oci_image(
            image.layout,
            OCIImageExpectation(
                role=image.role,
                repository=image.repository,
                digest=image.digest,
                platform=image.platform,
                layout_path=f"oci/{image.role}",
            ),
        )
        if refreshed != image:
            raise OCIContractError("OCI_IMAGE_SET_NOT_VERIFIED")
        refreshed_images.append(refreshed)

    observed: list[str] = []
    for refreshed in refreshed_images:
        expected = f"{refreshed.repository}@{refreshed.digest}"
        try:
            result = operation(refreshed)
        except OCIContractError:
            raise
        except Exception as error:
            raise OCIContractError("OCI_LOCAL_IMPORT_FAILED") from error
        if type(result) is not str or result != expected:
            raise OCIContractError("OCI_LOCAL_IMPORT_DIGEST_MISMATCH")
        observed.append(result)
    return VerifiedLocalImageImportReceipt(images=tuple(observed))
