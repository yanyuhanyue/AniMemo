from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from .authority import VerifiedReleaseMaterials
from .commands import CommandRunner
from .errors import CommandFailed
from .transport import ExplicitTransportPolicy


OCI_IMAGE_INDEX_MEDIA_TYPE = "application/vnd.oci.image.index.v1+json"
OCI_IMAGE_MANIFEST_MEDIA_TYPE = "application/vnd.oci.image.manifest.v1+json"
OCI_CONFIG_MEDIA_TYPE = "application/vnd.oci.image.config.v1+json"
OCI_LAYER_MEDIA_TYPE = "application/vnd.oci.image.layer.v1.tar"
OCI_PLATFORM = "linux/amd64"
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

    def __init__(self, *, runner=None, environment: dict[str, str] | None = None) -> None:
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
    if parsed.as_posix() != value or ":" in parsed.parts[0]:
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


def _read_regular_file(path: Path) -> bytes:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise OCIContractError("OCI_FILE_UNAVAILABLE") from error
    if path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
        raise OCIContractError("OCI_FILE_TYPE_FORBIDDEN")
    if metadata.st_nlink != 1:
        raise OCIContractError("OCI_HARDLINK_FORBIDDEN")
    try:
        value = path.read_bytes()
    except OSError as error:
        raise OCIContractError("OCI_FILE_UNREADABLE") from error
    if len(value) != metadata.st_size:
        raise OCIContractError("OCI_FILE_CHANGED_DURING_READ")
    return value


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


def _blob(layout: Path, digest: str, size: int) -> bytes:
    digest = _validate_digest(digest)
    target = layout / "blobs" / "sha256" / digest[7:]
    value = _read_regular_file(target)
    if len(value) != size:
        raise OCIContractError("OCI_DESCRIPTOR_SIZE_MISMATCH")
    if _digest(value) != digest:
        raise OCIContractError("OCI_DESCRIPTOR_DIGEST_MISMATCH")
    return value


def _closed_layout(layout: Path) -> tuple[set[str], set[str]]:
    try:
        root_stat = layout.lstat()
    except OSError as error:
        raise OCIContractError("OCI_LAYOUT_UNAVAILABLE") from error
    if layout.is_symlink() or not stat.S_ISDIR(root_stat.st_mode):
        raise OCIContractError("OCI_LAYOUT_ROOT_INVALID")
    result: set[str] = set()
    directories_seen: set[str] = set()
    folded: set[str] = set()
    for current, directories, names in os.walk(layout, topdown=True, followlinks=False):
        current_path = Path(current)
        for name in directories:
            directory = current_path / name
            try:
                metadata = directory.lstat()
            except OSError as error:
                raise OCIContractError("OCI_DIRECTORY_UNAVAILABLE") from error
            if directory.is_symlink() or not stat.S_ISDIR(metadata.st_mode):
                raise OCIContractError("OCI_DIRECTORY_TYPE_FORBIDDEN")
            relative = directory.relative_to(layout).as_posix()
            directories_seen.add(_validate_relative_path(relative))
        for name in names:
            relative = (current_path / name).relative_to(layout).as_posix()
            relative = _validate_relative_path(relative)
            if relative.casefold() in folded:
                raise OCIContractError("OCI_CASE_COLLISION")
            result.add(relative)
            folded.add(relative.casefold())
    return result, directories_seen


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


def verify_oci_image(layout: Path, expectation: OCIImageExpectation) -> VerifiedOCIImage:
    layout = Path(layout)
    actual_files, actual_directories = _closed_layout(layout)
    layout_document = _parse_json(_read_regular_file(layout / "oci-layout"))
    if layout_document != {"imageLayoutVersion": "1.0.0"}:
        raise OCIContractError("OCI_LAYOUT_VERSION_INVALID")
    index = _parse_json(_read_regular_file(layout / "index.json"))
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
    manifest_digest, manifest_size = _descriptor(
        manifests[0], media_type=OCI_IMAGE_MANIFEST_MEDIA_TYPE, with_platform=True
    )
    if manifest_digest != expectation.digest:
        raise OCIContractError("OCI_EXPECTED_MANIFEST_DIGEST_MISMATCH")
    manifest_bytes = _blob(layout, manifest_digest, manifest_size)
    manifest = _parse_json(manifest_bytes)
    _expect_keys(
        manifest,
        {"config", "layers", "mediaType", "schemaVersion"},
        "OCI_MANIFEST_FIELDS_INVALID",
    )
    if (
        manifest["schemaVersion"] != 2
        or manifest["mediaType"] != OCI_IMAGE_MANIFEST_MEDIA_TYPE
    ):
        raise OCIContractError("OCI_MANIFEST_IDENTITY_INVALID")
    config_digest, config_size = _descriptor(
        manifest["config"], media_type=OCI_CONFIG_MEDIA_TYPE, with_platform=False
    )
    config = _parse_json(_blob(layout, config_digest, config_size))
    if config.get("os") != "linux" or config.get("architecture") != "amd64":
        raise OCIContractError("OCI_CONFIG_PLATFORM_MISMATCH")
    layers = manifest["layers"]
    if not isinstance(layers, list):
        raise OCIContractError("OCI_LAYERS_INVALID")
    layer_digests: list[str] = []
    for layer in layers:
        layer_digest, layer_size = _descriptor(
            layer, media_type=OCI_LAYER_MEDIA_TYPE, with_platform=False
        )
        if layer_digest in layer_digests:
            raise OCIContractError("OCI_LAYER_DUPLICATE")
        _blob(layout, layer_digest, layer_size)
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
    return VerifiedOCIImage(
        role=expectation.role,
        repository=expectation.repository,
        digest=expectation.digest,
        platform=expectation.platform,
        layout=layout,
        config_digest=config_digest,
        layer_digests=tuple(layer_digests),
    )


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
