from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import stat
import tarfile
import tempfile
import unicodedata
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

BLOCKED_PORTABLE_PUBLICATION_AUTHORITY = "BLOCKED_PORTABLE_PUBLICATION_AUTHORITY"
MAX_PORTABLE_FILES = 16384
MAX_PORTABLE_FILE_BYTES = 8 * 1024 * 1024 * 1024
MAX_PORTABLE_TOTAL_BYTES = 32 * 1024 * 1024 * 1024
MAX_PORTABLE_INDEX_BYTES = 4 * 1024 * 1024
MAX_PORTABLE_PATH_LENGTH = 240
MAX_PORTABLE_PATH_DEPTH = 6
MAX_PORTABLE_PATH_COMPONENT_BYTES = 255
MAX_PORTABLE_DIRECTORIES = MAX_PORTABLE_FILES * MAX_PORTABLE_PATH_DEPTH
PORTABLE_STREAM_CHUNK_BYTES = 1024 * 1024
CANONICAL_RELEASE_ASSET_PATHS = (
    "authority/checksums.txt",
    "authority/deployment-contract.json",
    "authority/installer-materials.tar",
    "authority/release-manifest.json",
)
PORTABLE_IMAGE_REPOSITORIES = {
    "api": "ghcr.io/yanyuhanyue/animemo-api",
    "postgres": "docker.io/library/postgres",
    "redis": "docker.io/library/redis",
    "web": "ghcr.io/yanyuhanyue/animemo-web",
}
_RELEASE_TAG = re.compile(
    r"v[0-9]+\.[0-9]+\.[0-9]+(?:-(?:beta|rc)\.(?:[1-9][0-9]*|TEST))?"
)


def portable_release_asset_name(tag: str) -> str:
    """Return the single deterministic GitHub Release transport asset name."""

    if not isinstance(tag, str) or not _RELEASE_TAG.fullmatch(tag):
        raise PortableBundleError("PORTABLE_RELEASE_TAG_INVALID")
    return f"animemo-{tag}-portable.tar"


class PortableBundleError(ValueError):
    """An untrusted portable bundle does not satisfy its closed contract."""


class PortableAuthorityError(PortableBundleError):
    """Portable materials were presented as their own publication authority."""


@dataclass(frozen=True)
class PortableFileIdentity:
    path: str
    sha256: str
    size: int


@dataclass(frozen=True)
class PortableBundle:
    root: Path
    index: dict[str, Any]
    files: tuple[PortableFileIdentity, ...]

    def file(self, relative: str) -> Path:
        relative = _validate_relative_path(relative)
        identities = {item.path: item for item in self.files}
        if relative not in identities:
            raise PortableBundleError("PORTABLE_FILE_NOT_DECLARED")
        target = self.root.joinpath(*PurePosixPath(relative).parts)
        identity = identities[relative]
        actual_digest, actual_size = _hash_regular_file(
            target, max_bytes=MAX_PORTABLE_FILE_BYTES
        )
        if actual_size != identity.size or actual_digest != identity.sha256:
            raise PortableBundleError("PORTABLE_FILE_IDENTITY_CHANGED")
        return target


@dataclass(frozen=True)
class PortableArchiveInspection:
    archive: Path
    index: dict[str, Any]
    files: tuple[PortableFileIdentity, ...]
    archive_sha256: str


def canonical_json_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise PortableBundleError("PORTABLE_JSON_NOT_CANONICALIZABLE") from error


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
        raise PortableBundleError("PORTABLE_DIGEST_INVALID")
    return value


def _validate_relative_path(value: Any) -> str:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise PortableBundleError("PORTABLE_PATH_INVALID")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise PortableBundleError("PORTABLE_PATH_INVALID")
    if path.as_posix() != value or ":" in value:
        raise PortableBundleError("PORTABLE_PATH_NOT_CANONICAL")
    if unicodedata.normalize("NFC", value) != value:
        raise PortableBundleError("PORTABLE_PATH_NOT_CANONICAL")
    if len(value.encode("utf-8")) > MAX_PORTABLE_PATH_LENGTH:
        raise PortableBundleError("PORTABLE_PATH_LENGTH_LIMIT")
    if len(path.parts) > MAX_PORTABLE_PATH_DEPTH:
        raise PortableBundleError("PORTABLE_PATH_DEPTH_LIMIT")
    for part in path.parts:
        if (
            len(part.encode("utf-8")) > MAX_PORTABLE_PATH_COMPONENT_BYTES
            or part.endswith((" ", "."))
            or any(ord(character) < 32 for character in part)
        ):
            raise PortableBundleError("PORTABLE_PATH_COMPONENT_INVALID")
        stem = part.split(".", 1)[0].casefold()
        if stem in {"con", "prn", "aux", "nul"} or (
            len(stem) == 4 and stem[:3] in {"com", "lpt"} and stem[3] in "123456789"
        ):
            raise PortableBundleError("PORTABLE_PATH_COMPONENT_INVALID")
    return value


def _object_without_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    folded: set[str] = set()
    for key, value in pairs:
        if key in result or key.casefold() in folded:
            raise PortableBundleError("PORTABLE_JSON_DUPLICATE_KEY")
        result[key] = value
        folded.add(key.casefold())
    return result


def _parse_canonical_json(value: bytes) -> dict[str, Any]:
    try:
        parsed = json.loads(
            value.decode("utf-8"),
            object_pairs_hook=_object_without_duplicate_keys,
            parse_constant=lambda constant: (_ for _ in ()).throw(
                PortableBundleError("PORTABLE_JSON_NON_FINITE")
            ),
        )
    except PortableBundleError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PortableBundleError("PORTABLE_JSON_INVALID") from error
    if not isinstance(parsed, dict) or canonical_json_bytes(parsed) != value:
        raise PortableBundleError("PORTABLE_INDEX_NOT_CANONICAL")
    return parsed


def _open_single_link_regular_file(path: Path, *, max_bytes: int):
    try:
        before = path.lstat()
    except OSError as error:
        raise PortableBundleError("PORTABLE_FILE_UNAVAILABLE") from error
    if _is_link_like(path) or not stat.S_ISREG(before.st_mode):
        raise PortableBundleError("PORTABLE_FILE_TYPE_FORBIDDEN")
    if before.st_nlink != 1:
        raise PortableBundleError("PORTABLE_HARDLINK_FORBIDDEN")
    if before.st_size < 0 or before.st_size > max_bytes:
        raise PortableBundleError("PORTABLE_FILE_SIZE_LIMIT")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise PortableBundleError("PORTABLE_FILE_UNREADABLE") from error
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or opened.st_size != before.st_size
            or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
        ):
            raise PortableBundleError("PORTABLE_FILE_CHANGED_DURING_OPEN")
        return os.fdopen(descriptor, "rb", closefd=True), opened.st_size
    except BaseException:
        os.close(descriptor)
        raise


def _read_single_link_regular_file(path: Path, *, max_bytes: int) -> bytes:
    stream, expected_size = _open_single_link_regular_file(path, max_bytes=max_bytes)
    value = bytearray()
    with stream:
        while True:
            remaining = max_bytes + 1 - len(value)
            chunk = stream.read(min(PORTABLE_STREAM_CHUNK_BYTES, remaining))
            if not chunk:
                break
            value.extend(chunk)
            if len(value) > max_bytes:
                raise PortableBundleError("PORTABLE_FILE_SIZE_LIMIT")
        after = os.fstat(stream.fileno())
    if len(value) != expected_size or after.st_size != expected_size:
        raise PortableBundleError("PORTABLE_FILE_CHANGED_DURING_READ")
    return bytes(value)


def _hash_regular_file(path: Path, *, max_bytes: int) -> tuple[str, int]:
    stream, expected_size = _open_single_link_regular_file(path, max_bytes=max_bytes)
    hasher = hashlib.sha256()
    consumed = 0
    with stream:
        while True:
            chunk = stream.read(PORTABLE_STREAM_CHUNK_BYTES)
            if not chunk:
                break
            consumed += len(chunk)
            if consumed > max_bytes:
                raise PortableBundleError("PORTABLE_FILE_SIZE_LIMIT")
            hasher.update(chunk)
        after = os.fstat(stream.fileno())
    if consumed != expected_size or after.st_size != expected_size:
        raise PortableBundleError("PORTABLE_FILE_CHANGED_DURING_READ")
    return "sha256:" + hasher.hexdigest(), consumed


def _validate_index(index: dict[str, Any]) -> tuple[PortableFileIdentity, ...]:
    if set(index) != {
        "authorityState",
        "files",
        "ociImages",
        "profile",
        "schemaVersion",
    }:
        raise PortableBundleError("PORTABLE_INDEX_FIELDS_INVALID")
    if index["schemaVersion"] != 1:
        raise PortableBundleError("PORTABLE_SCHEMA_VERSION_UNSUPPORTED")
    if index["profile"] != "animemo-portable-bundle-v1":
        raise PortableBundleError("PORTABLE_PROFILE_INVALID")
    if index["authorityState"] != BLOCKED_PORTABLE_PUBLICATION_AUTHORITY:
        raise PortableAuthorityError("PORTABLE_SELF_DECLARED_AUTHORITY_FORBIDDEN")
    _validate_oci_images(index["ociImages"])
    raw_files = index["files"]
    if not isinstance(raw_files, list) or len(raw_files) > MAX_PORTABLE_FILES:
        raise PortableBundleError("PORTABLE_FILE_COUNT_LIMIT")
    identities: list[PortableFileIdentity] = []
    paths: set[str] = set()
    folded: set[str] = set()
    for item in raw_files:
        if not isinstance(item, dict) or set(item) != {"path", "sha256", "size"}:
            raise PortableBundleError("PORTABLE_FILE_IDENTITY_INVALID")
        path = _validate_relative_path(item["path"])
        if path == "bundle-index.json":
            raise PortableBundleError("PORTABLE_INDEX_SELF_REFERENCE_FORBIDDEN")
        if path in paths:
            raise PortableBundleError("PORTABLE_DUPLICATE_PATH")
        if path.casefold() in folded:
            raise PortableBundleError("PORTABLE_CASE_COLLISION")
        size = item["size"]
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise PortableBundleError("PORTABLE_FILE_SIZE_INVALID")
        identities.append(
            PortableFileIdentity(path, _validate_digest(item["sha256"]), size)
        )
        paths.add(path)
        folded.add(path.casefold())
    if [identity.path for identity in identities] != sorted(paths):
        raise PortableBundleError("PORTABLE_FILES_NOT_ORDERED")
    if not set(CANONICAL_RELEASE_ASSET_PATHS).issubset(paths):
        raise PortableBundleError("PORTABLE_CANONICAL_ASSETS_INCOMPLETE")
    _validate_complete_payload_shape(paths, index["ociImages"])
    return tuple(identities)


def _validate_oci_images(value: Any) -> None:
    if not isinstance(value, list):
        raise PortableBundleError("PORTABLE_OCI_IMAGES_INVALID")
    roles: list[str] = []
    for image in value:
        if not isinstance(image, dict) or set(image) != {
            "digest",
            "layoutPath",
            "platform",
            "repository",
            "role",
        }:
            raise PortableBundleError("PORTABLE_OCI_IMAGE_FIELDS_INVALID")
        role = image["role"]
        if not isinstance(role, str) or role not in PORTABLE_IMAGE_REPOSITORIES:
            raise PortableBundleError("PORTABLE_OCI_ROLE_INVALID")
        if image["repository"] != PORTABLE_IMAGE_REPOSITORIES[role]:
            raise PortableBundleError("PORTABLE_OCI_REPOSITORY_MISMATCH")
        if image["platform"] != "linux/amd64":
            raise PortableBundleError("PORTABLE_OCI_PLATFORM_MISMATCH")
        if image["layoutPath"] != f"oci/{role}":
            raise PortableBundleError("PORTABLE_OCI_LAYOUT_ROLE_MISMATCH")
        _validate_digest(image["digest"])
        roles.append(role)
    if len(roles) != len(set(roles)):
        raise PortableBundleError("PORTABLE_OCI_ROLES_DUPLICATE_OR_UNORDERED")
    if roles != sorted(PORTABLE_IMAGE_REPOSITORIES):
        raise PortableBundleError("PORTABLE_OCI_ROLES_INCOMPLETE_OR_UNORDERED")


def _walk_closed_layout(root: Path) -> tuple[set[str], set[str]]:
    files: set[str] = set()
    directories_seen: set[str] = set()
    folded: set[str] = set()
    for current, directories, names in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        for name in directories:
            directory = current_path / name
            try:
                metadata = directory.lstat()
            except OSError as error:
                raise PortableBundleError("PORTABLE_DIRECTORY_UNAVAILABLE") from error
            if _is_link_like(directory) or not stat.S_ISDIR(metadata.st_mode):
                raise PortableBundleError("PORTABLE_DIRECTORY_TYPE_FORBIDDEN")
            relative = directory.relative_to(root).as_posix()
            relative = _validate_relative_path(relative)
            if relative.casefold() in folded:
                raise PortableBundleError("PORTABLE_CASE_COLLISION")
            directories_seen.add(relative)
            folded.add(relative.casefold())
            if len(directories_seen) > MAX_PORTABLE_DIRECTORIES:
                raise PortableBundleError("PORTABLE_DIRECTORY_COUNT_LIMIT")
        for name in names:
            relative = (current_path / name).relative_to(root).as_posix()
            relative = _validate_relative_path(relative)
            if relative.casefold() in folded:
                raise PortableBundleError("PORTABLE_CASE_COLLISION")
            files.add(relative)
            folded.add(relative.casefold())
            if len(files) > MAX_PORTABLE_FILES + 1:
                raise PortableBundleError("PORTABLE_FILE_COUNT_LIMIT")
    return files, directories_seen


def _required_parent_directories(paths: set[str]) -> set[str]:
    result: set[str] = set()
    for path in paths:
        parts = PurePosixPath(path).parts[:-1]
        for end in range(1, len(parts) + 1):
            result.add(PurePosixPath(*parts[:end]).as_posix())
    return result


def _validate_complete_payload_shape(paths: set[str], oci_images: Any) -> None:
    _validate_oci_images(oci_images)
    roles = [image["role"] for image in oci_images]
    if roles != sorted(PORTABLE_IMAGE_REPOSITORIES):
        raise PortableBundleError("PORTABLE_OCI_ROLES_INCOMPLETE_OR_UNORDERED")
    canonical_assets = set(CANONICAL_RELEASE_ASSET_PATHS)
    if not canonical_assets.issubset(paths):
        raise PortableBundleError("PORTABLE_CANONICAL_ASSETS_INCOMPLETE")
    allowed_prefixes = tuple(f"oci/{role}/" for role in roles)
    for path in paths:
        if path not in canonical_assets and not path.startswith(allowed_prefixes):
            raise PortableBundleError("PORTABLE_PAYLOAD_PATH_UNDECLARED")
    for role in roles:
        required = {f"oci/{role}/index.json", f"oci/{role}/oci-layout"}
        if not required.issubset(paths):
            raise PortableBundleError("PORTABLE_OCI_LAYOUT_INCOMPLETE")


def _deterministic_tar_info(path: str, size: int) -> tarfile.TarInfo:
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


def build_portable_payload(
    source_root: Path,
    archive: Path,
    oci_images: Any,
    *,
    max_files: int = MAX_PORTABLE_FILES,
    max_file_bytes: int = MAX_PORTABLE_FILE_BYTES,
    max_total_bytes: int = MAX_PORTABLE_TOTAL_BYTES,
) -> PortableArchiveInspection:
    """Build a deterministic, uncompressed USTAR transport payload."""

    source_root = Path(source_root)
    archive = Path(archive)
    try:
        source_metadata = source_root.lstat()
        parent_metadata = archive.parent.lstat()
    except OSError as error:
        raise PortableBundleError("PORTABLE_BUILD_PATH_UNAVAILABLE") from error
    if (
        _is_link_like(source_root)
        or not stat.S_ISDIR(source_metadata.st_mode)
        or _is_link_like(archive.parent)
        or not stat.S_ISDIR(parent_metadata.st_mode)
    ):
        raise PortableBundleError("PORTABLE_BUILD_PATH_INVALID")
    actual_files, directories = _walk_closed_layout(source_root)
    if "bundle-index.json" in actual_files:
        raise PortableBundleError("PORTABLE_INDEX_SOURCE_FORBIDDEN")
    if len(actual_files) > max_files:
        raise PortableBundleError("PORTABLE_FILE_COUNT_LIMIT")
    if directories != _required_parent_directories(actual_files):
        raise PortableBundleError("PORTABLE_LAYOUT_NOT_CLOSED")
    _validate_complete_payload_shape(actual_files, oci_images)

    identities: list[PortableFileIdentity] = []
    total = 0
    for relative in sorted(actual_files):
        identity, size = _hash_regular_file(
            source_root.joinpath(*PurePosixPath(relative).parts),
            max_bytes=max_file_bytes,
        )
        total += size
        if total > max_total_bytes:
            raise PortableBundleError("PORTABLE_TOTAL_SIZE_LIMIT")
        identities.append(PortableFileIdentity(relative, identity, size))
    index = {
        "authorityState": BLOCKED_PORTABLE_PUBLICATION_AUTHORITY,
        "files": [
            {"path": item.path, "sha256": item.sha256, "size": item.size}
            for item in identities
        ],
        "ociImages": oci_images,
        "profile": "animemo-portable-bundle-v1",
        "schemaVersion": 1,
    }
    _validate_index(index)
    index_bytes = canonical_json_bytes(index)
    if (
        len(index_bytes) > min(max_file_bytes, MAX_PORTABLE_INDEX_BYTES)
        or total + len(index_bytes) > max_total_bytes
    ):
        raise PortableBundleError("PORTABLE_TOTAL_SIZE_LIMIT")

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{archive.name}.", suffix=".tmp", dir=archive.parent
    )
    temporary = Path(temporary_name)
    try:
        os.chmod(temporary, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as output:
            try:
                with tarfile.open(
                    fileobj=output, mode="w:", format=tarfile.USTAR_FORMAT
                ) as handle:
                    handle.addfile(
                        _deterministic_tar_info("bundle-index.json", len(index_bytes)),
                        fileobj=io.BytesIO(index_bytes),
                    )
                    for identity in identities:
                        target = source_root.joinpath(
                            *PurePosixPath(identity.path).parts
                        )
                        stream, opened_size = _open_single_link_regular_file(
                            target, max_bytes=max_file_bytes
                        )
                        if opened_size != identity.size:
                            stream.close()
                            raise PortableBundleError(
                                "PORTABLE_FILE_CHANGED_DURING_BUILD"
                            )
                        with stream:
                            handle.addfile(
                                _deterministic_tar_info(identity.path, identity.size),
                                fileobj=stream,
                            )
            except (OSError, tarfile.TarError, ValueError) as error:
                if isinstance(error, PortableBundleError):
                    raise
                raise PortableBundleError("PORTABLE_ARCHIVE_BUILD_FAILED") from error
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, archive)
        inspection = inspect_portable_archive(
            archive,
            max_files=max_files,
            max_file_bytes=max_file_bytes,
            max_total_bytes=max_total_bytes,
        )
        if inspection.index != index:
            raise PortableBundleError("PORTABLE_ARCHIVE_BUILD_IDENTITY_MISMATCH")
        return inspection
    except BaseException:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        try:
            archive.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def validate_portable_bundle(
    root: Path,
    *,
    max_files: int = MAX_PORTABLE_FILES,
    max_file_bytes: int = MAX_PORTABLE_FILE_BYTES,
    max_total_bytes: int = MAX_PORTABLE_TOTAL_BYTES,
) -> PortableBundle:
    root = Path(root)
    try:
        metadata = root.lstat()
    except OSError as error:
        raise PortableBundleError("PORTABLE_ROOT_UNAVAILABLE") from error
    if _is_link_like(root) or not stat.S_ISDIR(metadata.st_mode):
        raise PortableBundleError("PORTABLE_ROOT_INVALID")
    index_bytes = _read_single_link_regular_file(
        root / "bundle-index.json",
        max_bytes=min(max_file_bytes, MAX_PORTABLE_INDEX_BYTES),
    )
    index = _parse_canonical_json(index_bytes)
    files = _validate_index(index)
    if len(files) > max_files:
        raise PortableBundleError("PORTABLE_FILE_COUNT_LIMIT")
    actual, directories = _walk_closed_layout(root)
    declared = {item.path for item in files} | {"bundle-index.json"}
    if actual != declared or directories != _required_parent_directories(declared):
        raise PortableBundleError("PORTABLE_LAYOUT_NOT_CLOSED")
    total = len(index_bytes)
    if total > max_total_bytes:
        raise PortableBundleError("PORTABLE_TOTAL_SIZE_LIMIT")
    for identity in files:
        actual_digest, actual_size = _hash_regular_file(
            root.joinpath(*PurePosixPath(identity.path).parts),
            max_bytes=max_file_bytes,
        )
        total += actual_size
        if total > max_total_bytes:
            raise PortableBundleError("PORTABLE_TOTAL_SIZE_LIMIT")
        if actual_size != identity.size or actual_digest != identity.sha256:
            raise PortableBundleError("PORTABLE_FILE_IDENTITY_MISMATCH")
    return PortableBundle(root=root, index=index, files=files)


def _hash_ustar_archive(path: Path, *, max_bytes: int) -> tuple[str, int]:
    stream, expected_size = _open_single_link_regular_file(path, max_bytes=max_bytes)
    hasher = hashlib.sha256()
    first = b""
    tail = b""
    consumed = 0
    with stream:
        while True:
            chunk = stream.read(PORTABLE_STREAM_CHUNK_BYTES)
            if not chunk:
                break
            if not first:
                first = chunk[:512]
            consumed += len(chunk)
            hasher.update(chunk)
            tail = (tail + chunk)[-1024:]
        after = os.fstat(stream.fileno())
    if consumed != expected_size or after.st_size != expected_size:
        raise PortableBundleError("PORTABLE_FILE_CHANGED_DURING_READ")
    if (
        consumed < 1536
        or consumed % 512 != 0
        or len(first) < 512
        or first[257:263] not in {b"ustar\x00", b"ustar "}
        or tail != b"\x00" * 1024
    ):
        raise PortableBundleError("PORTABLE_USTAR_REQUIRED")
    return "sha256:" + hasher.hexdigest(), consumed


def _open_private_stage_file(root: Path, relative: str):
    target = root.joinpath(*PurePosixPath(relative).parts)
    parent = target.parent
    parent.mkdir(parents=True, exist_ok=True)
    current = root
    for part in PurePosixPath(relative).parts[:-1]:
        current = current / part
        os.chmod(current, 0o700)
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(target, flags, 0o600)
    except OSError as error:
        raise PortableBundleError("PORTABLE_STAGE_FILE_CREATE_FAILED") from error
    return os.fdopen(descriptor, "wb", closefd=True)


def _consume_portable_archive(
    archive: Path,
    *,
    stage_root: Path | None,
    max_files: int,
    max_file_bytes: int,
    max_total_bytes: int,
) -> PortableArchiveInspection:
    archive_identity, _ = _hash_ustar_archive(
        archive, max_bytes=max_total_bytes + (16 * 1024 * 1024)
    )
    stream, _ = _open_single_link_regular_file(
        archive, max_bytes=max_total_bytes + (16 * 1024 * 1024)
    )
    entries: dict[str, tuple[str, int]] = {}
    folded: set[str] = set()
    index: dict[str, Any] | None = None
    total = 0
    count = 0
    try:
        with (
            stream,
            tarfile.open(
                fileobj=stream, mode="r|", format=tarfile.USTAR_FORMAT
            ) as handle,
        ):
            for member in handle:
                count += 1
                if count > max_files + 1:
                    raise PortableBundleError("PORTABLE_FILE_COUNT_LIMIT")
                path = _validate_relative_path(member.name)
                if path in entries:
                    raise PortableBundleError("PORTABLE_DUPLICATE_PATH")
                if path.casefold() in folded:
                    raise PortableBundleError("PORTABLE_CASE_COLLISION")
                if (
                    not member.isfile()
                    or member.type != tarfile.REGTYPE
                    or member.pax_headers
                ):
                    raise PortableBundleError("PORTABLE_ENTRY_TYPE_FORBIDDEN")
                if (
                    member.mode != 0o644
                    or member.uid != 0
                    or member.gid != 0
                    or member.uname != ""
                    or member.gname != ""
                    or member.mtime != 0
                ):
                    raise PortableBundleError("PORTABLE_USTAR_HEADER_NOT_CANONICAL")
                if member.size < 0 or member.size > max_file_bytes:
                    raise PortableBundleError("PORTABLE_FILE_SIZE_LIMIT")
                if (
                    path == "bundle-index.json"
                    and member.size > MAX_PORTABLE_INDEX_BYTES
                ):
                    raise PortableBundleError("PORTABLE_INDEX_SIZE_LIMIT")
                total += member.size
                if total > max_total_bytes:
                    raise PortableBundleError("PORTABLE_TOTAL_SIZE_LIMIT")
                if (
                    stage_root is not None
                    and index is None
                    and path != "bundle-index.json"
                ):
                    raise PortableBundleError("PORTABLE_INDEX_MUST_BE_FIRST")
                extracted = handle.extractfile(member)
                if extracted is None:
                    raise PortableBundleError("PORTABLE_ARCHIVE_ENTRY_UNREADABLE")
                output = (
                    _open_private_stage_file(stage_root, path)
                    if stage_root is not None
                    else None
                )
                captured = bytearray() if path == "bundle-index.json" else None
                hasher = hashlib.sha256()
                consumed = 0
                try:
                    while consumed < member.size:
                        chunk = extracted.read(
                            min(PORTABLE_STREAM_CHUNK_BYTES, member.size - consumed)
                        )
                        if not chunk:
                            break
                        consumed += len(chunk)
                        hasher.update(chunk)
                        if output is not None:
                            output.write(chunk)
                        if captured is not None:
                            captured.extend(chunk)
                    if consumed != member.size:
                        raise PortableBundleError(
                            "PORTABLE_ARCHIVE_ENTRY_SIZE_MISMATCH"
                        )
                    if output is not None:
                        output.flush()
                        os.fsync(output.fileno())
                finally:
                    if output is not None:
                        output.close()
                entries[path] = ("sha256:" + hasher.hexdigest(), consumed)
                folded.add(path.casefold())
                if captured is not None:
                    index = _parse_canonical_json(bytes(captured))
    except PortableBundleError:
        raise
    except (OSError, tarfile.TarError) as error:
        raise PortableBundleError("PORTABLE_ARCHIVE_INVALID") from error
    if index is None:
        raise PortableBundleError("PORTABLE_INDEX_MISSING")
    files = _validate_index(index)
    declared = {item.path for item in files} | {"bundle-index.json"}
    if set(entries) != declared:
        raise PortableBundleError("PORTABLE_LAYOUT_NOT_CLOSED")
    if list(entries) != ["bundle-index.json"] + sorted(item.path for item in files):
        raise PortableBundleError("PORTABLE_ARCHIVE_ORDER_INVALID")
    for identity in files:
        actual_digest, actual_size = entries[identity.path]
        if actual_size != identity.size or actual_digest != identity.sha256:
            raise PortableBundleError("PORTABLE_FILE_IDENTITY_MISMATCH")
    return PortableArchiveInspection(
        archive=archive,
        index=index,
        files=files,
        archive_sha256=archive_identity,
    )


def inspect_portable_archive(
    archive: Path,
    *,
    max_files: int = MAX_PORTABLE_FILES,
    max_file_bytes: int = MAX_PORTABLE_FILE_BYTES,
    max_total_bytes: int = MAX_PORTABLE_TOTAL_BYTES,
) -> PortableArchiveInspection:
    return _consume_portable_archive(
        Path(archive),
        stage_root=None,
        max_files=max_files,
        max_file_bytes=max_file_bytes,
        max_total_bytes=max_total_bytes,
    )


def stage_portable_payload(
    archive: Path,
    staging_parent: Path,
    *,
    max_files: int = MAX_PORTABLE_FILES,
    max_file_bytes: int = MAX_PORTABLE_FILE_BYTES,
    max_total_bytes: int = MAX_PORTABLE_TOTAL_BYTES,
) -> PortableBundle:
    """Stream a payload into an isolated private directory and validate it."""

    staging_parent = Path(staging_parent)
    try:
        parent_metadata = staging_parent.lstat()
    except OSError as error:
        raise PortableBundleError("PORTABLE_STAGE_PARENT_UNAVAILABLE") from error
    if _is_link_like(staging_parent) or not stat.S_ISDIR(parent_metadata.st_mode):
        raise PortableBundleError("PORTABLE_STAGE_PARENT_INVALID")
    root = Path(tempfile.mkdtemp(prefix=".animemo-portable-", dir=staging_parent))
    os.chmod(root, 0o700)
    try:
        _consume_portable_archive(
            Path(archive),
            stage_root=root,
            max_files=max_files,
            max_file_bytes=max_file_bytes,
            max_total_bytes=max_total_bytes,
        )
        return validate_portable_bundle(
            root,
            max_files=max_files,
            max_file_bytes=max_file_bytes,
            max_total_bytes=max_total_bytes,
        )
    except BaseException:
        shutil.rmtree(root, ignore_errors=True)
        raise


def portable_publication_authority_gate(
    bundle: PortableBundle,
    *,
    trust_root: object | None = None,
    public_key: object | None = None,
) -> str:
    if not isinstance(bundle, PortableBundle):
        raise PortableBundleError("PORTABLE_BUNDLE_NOT_VALIDATED")
    if trust_root is not None or public_key is not None:
        raise PortableAuthorityError("PORTABLE_SELF_DECLARED_AUTHORITY_FORBIDDEN")
    return BLOCKED_PORTABLE_PUBLICATION_AUTHORITY


def _copy_verified_regular_file(source: Path, destination: Path) -> None:
    stream, size = _open_single_link_regular_file(
        source, max_bytes=MAX_PORTABLE_FILE_BYTES
    )
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(destination, flags, 0o600)
    except OSError as error:
        stream.close()
        raise PortableBundleError("PORTABLE_PROMOTION_COPY_FAILED") from error
    consumed = 0
    try:
        with stream, os.fdopen(descriptor, "wb", closefd=True) as output:
            while chunk := stream.read(PORTABLE_STREAM_CHUNK_BYTES):
                consumed += len(chunk)
                if consumed > size:
                    raise PortableBundleError("PORTABLE_PROMOTION_SOURCE_CHANGED")
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
    except OSError as error:
        raise PortableBundleError("PORTABLE_PROMOTION_COPY_FAILED") from error
    if consumed != size:
        raise PortableBundleError("PORTABLE_PROMOTION_SOURCE_CHANGED")


def promote_portable_payload(
    rc_archive: Path,
    *,
    authority_directory: Path,
    archive: Path,
) -> PortableArchiveInspection:
    """Re-envelope accepted RC OCI bytes with the Stable canonical four.

    This does not rebuild, pull, or import any image and never copies a
    post-publish attestation sidecar into the deterministic payload.
    """

    rc_archive = Path(rc_archive)
    authority_directory = Path(authority_directory)
    archive = Path(archive)
    try:
        authority_metadata = authority_directory.lstat()
        archive_parent_metadata = archive.parent.lstat()
    except OSError as error:
        raise PortableBundleError("PORTABLE_PROMOTION_PATH_UNAVAILABLE") from error
    if (
        _is_link_like(authority_directory)
        or not stat.S_ISDIR(authority_metadata.st_mode)
        or _is_link_like(archive.parent)
        or not stat.S_ISDIR(archive_parent_metadata.st_mode)
    ):
        raise PortableBundleError("PORTABLE_PROMOTION_PATH_INVALID")
    source = Path(
        tempfile.mkdtemp(prefix=".animemo-portable-promotion-", dir=archive.parent)
    )
    staged: PortableBundle | None = None
    try:
        staged = stage_portable_payload(rc_archive, archive.parent)
        for relative in CANONICAL_RELEASE_ASSET_PATHS:
            name = PurePosixPath(relative).name
            _copy_verified_regular_file(
                authority_directory / name,
                source.joinpath(*PurePosixPath(relative).parts),
            )
        for identity in staged.files:
            if not identity.path.startswith("oci/"):
                continue
            _copy_verified_regular_file(
                staged.file(identity.path),
                source.joinpath(*PurePosixPath(identity.path).parts),
            )
        return build_portable_payload(
            source,
            archive,
            staged.index["ociImages"],
        )
    finally:
        shutil.rmtree(source, ignore_errors=True)
        if staged is not None:
            shutil.rmtree(staged.root, ignore_errors=True)
