from __future__ import annotations

import hashlib
import json
import os
import stat
import tarfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any


BLOCKED_PORTABLE_PUBLICATION_AUTHORITY = (
    "BLOCKED_PORTABLE_PUBLICATION_AUTHORITY"
)
MAX_PORTABLE_FILES = 4096
MAX_PORTABLE_FILE_BYTES = 4 * 1024 * 1024 * 1024
MAX_PORTABLE_TOTAL_BYTES = 16 * 1024 * 1024 * 1024
PORTABLE_IMAGE_REPOSITORIES = {
    "api": "ghcr.io/yanyuhanyue/animemo-api",
    "postgres": "docker.io/library/postgres",
    "redis": "docker.io/library/redis",
    "web": "ghcr.io/yanyuhanyue/animemo-web",
}


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
        value = _read_single_link_regular_file(
            target, max_bytes=MAX_PORTABLE_FILE_BYTES
        )
        identity = identities[relative]
        if len(value) != identity.size or _digest(value) != identity.sha256:
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
    if path.as_posix() != value or ":" in path.parts[0]:
        raise PortableBundleError("PORTABLE_PATH_NOT_CANONICAL")
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


def _read_single_link_regular_file(path: Path, *, max_bytes: int) -> bytes:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise PortableBundleError("PORTABLE_FILE_UNAVAILABLE") from error
    if path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
        raise PortableBundleError("PORTABLE_FILE_TYPE_FORBIDDEN")
    if metadata.st_nlink != 1:
        raise PortableBundleError("PORTABLE_HARDLINK_FORBIDDEN")
    if metadata.st_size > max_bytes:
        raise PortableBundleError("PORTABLE_FILE_SIZE_LIMIT")
    try:
        value = path.read_bytes()
    except OSError as error:
        raise PortableBundleError("PORTABLE_FILE_UNREADABLE") from error
    if len(value) != metadata.st_size:
        raise PortableBundleError("PORTABLE_FILE_CHANGED_DURING_READ")
    return value


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
    return tuple(identities)


def _validate_oci_images(value: Any) -> None:
    if not isinstance(value, list) or len(value) > len(PORTABLE_IMAGE_REPOSITORIES):
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
    if roles != sorted(roles) or len(roles) != len(set(roles)):
        raise PortableBundleError("PORTABLE_OCI_ROLES_DUPLICATE_OR_UNORDERED")


def _walk_closed_layout(root: Path) -> tuple[set[str], set[str]]:
    files: set[str] = set()
    directories_seen: set[str] = set()
    for current, directories, names in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        for name in directories:
            directory = current_path / name
            try:
                metadata = directory.lstat()
            except OSError as error:
                raise PortableBundleError("PORTABLE_DIRECTORY_UNAVAILABLE") from error
            if directory.is_symlink() or not stat.S_ISDIR(metadata.st_mode):
                raise PortableBundleError("PORTABLE_DIRECTORY_TYPE_FORBIDDEN")
            relative = directory.relative_to(root).as_posix()
            directories_seen.add(_validate_relative_path(relative))
        for name in names:
            relative = (current_path / name).relative_to(root).as_posix()
            files.add(_validate_relative_path(relative))
    return files, directories_seen


def _required_parent_directories(paths: set[str]) -> set[str]:
    result: set[str] = set()
    for path in paths:
        parts = PurePosixPath(path).parts[:-1]
        for end in range(1, len(parts) + 1):
            result.add(PurePosixPath(*parts[:end]).as_posix())
    return result


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
    if root.is_symlink() or not stat.S_ISDIR(metadata.st_mode):
        raise PortableBundleError("PORTABLE_ROOT_INVALID")
    index_bytes = _read_single_link_regular_file(
        root / "bundle-index.json", max_bytes=max_file_bytes
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
        value = _read_single_link_regular_file(
            root.joinpath(*PurePosixPath(identity.path).parts),
            max_bytes=max_file_bytes,
        )
        total += len(value)
        if total > max_total_bytes:
            raise PortableBundleError("PORTABLE_TOTAL_SIZE_LIMIT")
        if len(value) != identity.size or _digest(value) != identity.sha256:
            raise PortableBundleError("PORTABLE_FILE_IDENTITY_MISMATCH")
    return PortableBundle(root=root, index=index, files=files)


def inspect_portable_archive(
    archive: Path,
    *,
    max_files: int = MAX_PORTABLE_FILES,
    max_file_bytes: int = MAX_PORTABLE_FILE_BYTES,
    max_total_bytes: int = MAX_PORTABLE_TOTAL_BYTES,
) -> PortableArchiveInspection:
    archive = Path(archive)
    archive_bytes = _read_single_link_regular_file(
        archive, max_bytes=max_total_bytes + (16 * 1024 * 1024)
    )
    entries: dict[str, bytes] = {}
    folded: set[str] = set()
    total = 0
    try:
        with tarfile.open(archive, mode="r:*") as handle:
            members = handle.getmembers()
            if len(members) > max_files + 1:
                raise PortableBundleError("PORTABLE_FILE_COUNT_LIMIT")
            for member in members:
                path = _validate_relative_path(member.name)
                if path in entries:
                    raise PortableBundleError("PORTABLE_DUPLICATE_PATH")
                if path.casefold() in folded:
                    raise PortableBundleError("PORTABLE_CASE_COLLISION")
                if not member.isfile():
                    raise PortableBundleError("PORTABLE_ENTRY_TYPE_FORBIDDEN")
                if member.size < 0 or member.size > max_file_bytes:
                    raise PortableBundleError("PORTABLE_FILE_SIZE_LIMIT")
                total += member.size
                if total > max_total_bytes:
                    raise PortableBundleError("PORTABLE_TOTAL_SIZE_LIMIT")
                extracted = handle.extractfile(member)
                if extracted is None:
                    raise PortableBundleError("PORTABLE_ARCHIVE_ENTRY_UNREADABLE")
                value = extracted.read(max_file_bytes + 1)
                if len(value) != member.size:
                    raise PortableBundleError("PORTABLE_ARCHIVE_ENTRY_SIZE_MISMATCH")
                entries[path] = value
                folded.add(path.casefold())
    except PortableBundleError:
        raise
    except (OSError, tarfile.TarError) as error:
        raise PortableBundleError("PORTABLE_ARCHIVE_INVALID") from error
    if "bundle-index.json" not in entries:
        raise PortableBundleError("PORTABLE_INDEX_MISSING")
    index = _parse_canonical_json(entries["bundle-index.json"])
    files = _validate_index(index)
    declared = {item.path for item in files} | {"bundle-index.json"}
    if set(entries) != declared:
        raise PortableBundleError("PORTABLE_LAYOUT_NOT_CLOSED")
    for identity in files:
        value = entries[identity.path]
        if len(value) != identity.size or _digest(value) != identity.sha256:
            raise PortableBundleError("PORTABLE_FILE_IDENTITY_MISMATCH")
    return PortableArchiveInspection(
        archive=archive,
        index=index,
        files=files,
        archive_sha256=_digest(archive_bytes),
    )


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
