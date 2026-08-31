from __future__ import annotations

import hashlib
import io
import json
import os
import re
import secrets
import shutil
import stat
import tarfile
import tempfile
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Self

from durability.platform import PlatformQualificationError, parse_platform_qualification
from release.formal_windows_pretrust import (
    FORMAL_WINDOWS_PRETRUST_FILES,
    FORMAL_WINDOWS_PRETRUST_PREFIX,
    FormalWindowsPretrustedTrustMaterial,
    FormalWindowsPretrustError,
    inspect_formal_windows_pretrust_in_installer_materials,
)

MAX_MATERIAL_FILES = 512
MAX_MATERIAL_FILE_BYTES = 64 * 1024 * 1024
MAX_MATERIAL_TOTAL_BYTES = 256 * 1024 * 1024
MAX_QUALIFICATION_ARTIFACT_BYTES = 16 * 1024 * 1024 * 1024
MAX_QUALIFICATION_MEMBER_BYTES = 4 * 1024 * 1024 * 1024
INSTALLER_MATERIALS_NAME = "installer-materials.tar"
PLATFORM_QUALIFICATION_MATERIAL = "release/platform-qualification.json"
OFFLINE_RELEASE_VERIFIER_MATERIAL = (
    "release/release_attestation_verifier/offline-release-verifier"
)
INITIAL_TRUST_KIT_PREFIX = "release/release_attestation_verifier/pretrust-v2"
PREPUBLICATION_SCHEMA_VERSION = 3
PREPUBLICATION_MATERIALS_NAME = "prepublication-materials.json"
DEPLOYMENT_CONTRACT_NAME = "deployment-contract.json"
CANDIDATE_QUALIFICATION_ROOT_FILES = frozenset(
    {
        "candidate-input.json",
        "checksums.txt",
        DEPLOYMENT_CONTRACT_NAME,
        INSTALLER_MATERIALS_NAME,
        "platform-qualification.json",
        PREPUBLICATION_MATERIALS_NAME,
        "release-producer-toolchain-receipt.json",
        "release-manifest.json",
        "release-notes.json",
        "release-notes.md",
    }
)
LEGACY_QUALIFICATION_ROOT_FILES = frozenset(
    {
        DEPLOYMENT_CONTRACT_NAME,
        INSTALLER_MATERIALS_NAME,
        "platform-qualification.json",
        PREPUBLICATION_MATERIALS_NAME,
        "release-notes.json",
        "release-notes.md",
    }
)
INITIAL_TRUST_KIT_FILES = frozenset(
    {
        "github-trusted-root.jsonl",
        "github-tuf-root.json",
        "initial-trust-bootstrap.json",
        "offline-release-verifier",
        "sigstore-trusted-root.jsonl",
        "sigstore-tuf-root.json",
        "trust-profile.json",
    }
)

_FIXED_DEPLOYMENT_FILES = (
    "deploy/docker-compose.yml",
    "deploy/install-updater.sh",
    "deploy/updater/animemo",
    "deploy/updater/animemo-updater",
    "deploy/updater/animemo-updater@.service",
    "deploy/updater/animemo-updater.sysusers.conf",
    "deploy/updater/animemo-updater.tmpfiles.conf",
    "scripts/candidate_profile_runner.py",
    "scripts/closed_runtime_inventory.py",
    "scripts/formal_profile_runner.py",
)


class MaterialContractError(ValueError):
    pass


@dataclass(frozen=True)
class MaterialFileIdentity:
    path: str
    sha256: str
    size: int
    mode: int

    def as_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "sha256": self.sha256,
            "size": self.size,
            "mode": format(self.mode, "04o"),
        }


@dataclass(frozen=True)
class MaterialArchiveIdentity:
    sha256: str
    size: int
    files: tuple[MaterialFileIdentity, ...]


@dataclass(frozen=True)
class VerifiedMaterialSet:
    root: Path
    archive_sha256: str
    files: tuple[MaterialFileIdentity, ...]

    def material(self, relative: str) -> Path:
        relative = _validate_relative_path(relative)
        identities = {item.path: item for item in self.files}
        if relative not in identities:
            raise MaterialContractError("Installer material is not declared")
        target = self.root.joinpath(*PurePosixPath(relative).parts)
        try:
            target_stat = target.lstat()
        except OSError as error:
            raise MaterialContractError(
                "Verified installer material is unavailable"
            ) from error
        if (
            target.is_symlink()
            or not stat.S_ISREG(target_stat.st_mode)
            or target_stat.st_nlink != 1
        ):
            raise MaterialContractError(
                "Verified installer material is not a regular file"
            )
        value = read_bounded_release_file(
            target,
            subject="Verified installer material",
            maximum=MAX_MATERIAL_FILE_BYTES,
        )
        identity = identities[relative]
        if len(value) != identity.size or _sha256_bytes(value) != identity.sha256:
            raise MaterialContractError("Verified installer material has changed")
        return target


def _sha256_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _stat_identity(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_nlink,
        metadata.st_size,
    )


def _stat_content_state(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def bound_release_directory_io_available() -> bool:
    return all(
        function in os.supports_dir_fd
        for function in (os.open, os.stat, os.unlink)
    ) and (
        bool(getattr(os, "O_DIRECTORY", 0))
        and bool(getattr(os, "O_NOFOLLOW", 0))
    )


def descriptor_relative_release_io_available() -> bool:
    return (
        bound_release_directory_io_available()
        and os.link in os.supports_dir_fd
    )


def open_bound_release_directory(path: Path) -> int:
    if not bound_release_directory_io_available():
        raise NotImplementedError
    absolute = Path(os.path.abspath(path))
    flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(absolute.anchor, flags)
    try:
        for component in absolute.parts[1:]:
            child = os.open(component, flags, dir_fd=descriptor)
            opened = os.fstat(child)
            if not stat.S_ISDIR(opened.st_mode):
                os.close(child)
                raise NotADirectoryError(path)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def bound_release_directory_matches(path: Path, descriptor: int) -> bool:
    try:
        current = Path(path).lstat()
        opened = os.fstat(descriptor)
    except OSError:
        return False
    return (
        not stat.S_ISLNK(current.st_mode)
        and stat.S_ISDIR(current.st_mode)
        and _stat_identity(current)[:3] == _stat_identity(opened)[:3]
    )


@contextmanager
def hold_bound_release_directory(path: Path, *, subject: str) -> Iterator[int]:
    """Hold one directory identity across descriptor-relative member I/O."""

    try:
        descriptor = open_bound_release_directory(path)
    except (NotImplementedError, OSError) as error:
        raise MaterialContractError(f"{subject} directory is unavailable") from error
    try:
        yield descriptor
        if not bound_release_directory_matches(path, descriptor):
            raise MaterialContractError(f"{subject} directory changed while reading")
    finally:
        os.close(descriptor)


def read_bounded_release_member(
    directory_descriptor: int,
    name: str,
    *,
    subject: str,
    maximum: int,
    allow_empty: bool = True,
) -> bytes:
    """Read one canonical leaf relative to an already-held directory handle."""

    if (
        not isinstance(name, str)
        or not name
        or name in {".", ".."}
        or "/" in name
        or "\\" in name
        or "\x00" in name
    ):
        raise MaterialContractError(f"{subject} member name is invalid")
    try:
        before = os.stat(
            name,
            dir_fd=directory_descriptor,
            follow_symlinks=False,
        )
    except OSError as error:
        raise MaterialContractError(f"{subject} is unavailable") from error
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
        or before.st_size > maximum
        or (not allow_empty and before.st_size <= 0)
    ):
        raise MaterialContractError(
            f"{subject} is not a bounded single-link regular file"
        )
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = -1
    try:
        descriptor = os.open(name, flags, dir_fd=directory_descriptor)
        opened = os.fstat(descriptor)
        if _stat_identity(opened) != _stat_identity(before):
            raise MaterialContractError(f"{subject} changed while opening")
        value = bytearray()
        while True:
            chunk = os.read(descriptor, min(1024 * 1024, maximum + 1 - len(value)))
            if not chunk:
                break
            value.extend(chunk)
            if len(value) > maximum:
                raise MaterialContractError(f"{subject} is too large")
        after = os.fstat(descriptor)
        if (
            len(value) != opened.st_size
            or _stat_identity(after) != _stat_identity(opened)
            or _stat_content_state(after) != _stat_content_state(opened)
        ):
            raise MaterialContractError(f"{subject} changed while reading")
        return bytes(value)
    except MaterialContractError:
        raise
    except OSError as error:
        raise MaterialContractError(f"{subject} is unreadable") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _object_identity(metadata: os.stat_result) -> tuple[int, int, int]:
    return metadata.st_dev, metadata.st_ino, metadata.st_mode


def _link_release_file(
    source: str,
    destination: str,
    *,
    parent_descriptor: int,
) -> None:
    os.link(
        source,
        destination,
        src_dir_fd=parent_descriptor,
        dst_dir_fd=parent_descriptor,
        follow_symlinks=False,
    )


class AtomicReleaseFile:
    """Own a private release file from creation through exclusive publication."""

    def __init__(self, output: Path) -> None:
        self.output = Path(output)
        self.descriptor = -1
        self.parent_descriptor = -1
        self.temporary_name = ""
        self.temporary_path: Path | None = None
        self.created: os.stat_result | None = None
        self.published = False

    def __enter__(self) -> Self:
        try:
            if descriptor_relative_release_io_available():
                self.parent_descriptor = open_bound_release_directory(
                    self.output.parent
                )
                flags = (
                    os.O_RDWR
                    | os.O_CREAT
                    | os.O_EXCL
                    | getattr(os, "O_BINARY", 0)
                    | getattr(os, "O_NOFOLLOW", 0)
                )
                for _attempt in range(32):
                    name = f".{self.output.name}.{secrets.token_hex(16)}.tmp"
                    try:
                        self.descriptor = os.open(
                            name,
                            flags,
                            0o600,
                            dir_fd=self.parent_descriptor,
                        )
                    except FileExistsError:
                        continue
                    self.temporary_name = name
                    break
                else:
                    raise MaterialContractError(
                        "Installer material temporary file cannot be created"
                    )
            else:
                # Windows is a non-authoritative development platform; its
                # open-file semantics prevent the replacement exercised here.
                self.descriptor, temporary_name = tempfile.mkstemp(
                    prefix=f".{self.output.name}.",
                    suffix=".tmp",
                    dir=self.output.parent,
                )
                self.temporary_path = Path(temporary_name)
            self.created = os.fstat(self.descriptor)
            return self
        except BaseException:
            try:
                if self.created is None and self.descriptor >= 0:
                    self.created = os.fstat(self.descriptor)
                self._cleanup_temporary()
            finally:
                self._close_owned_resources()
            raise

    def __exit__(self, exc_type, exc, traceback) -> None:
        try:
            if not self.published:
                self._cleanup_temporary()
        finally:
            self._close_owned_resources()

    def open_stream(self) -> BinaryIO:
        stream = os.fdopen(self.descriptor, "w+b", closefd=True)
        self.descriptor = -1
        return stream

    def publish(self, stream: BinaryIO, completed: os.stat_result) -> None:
        if self.parent_descriptor >= 0:
            self._publish_descriptor_relative(stream, completed)
        else:
            self._publish_windows_fallback(stream, completed)
        self.published = True

    def _publish_descriptor_relative(
        self,
        stream: BinaryIO,
        completed: os.stat_result,
    ) -> None:
        if not bound_release_directory_matches(
            self.output.parent,
            self.parent_descriptor,
        ):
            raise MaterialContractError(
                "Installer material output directory changed before publication"
            )
        current = os.stat(
            self.temporary_name,
            dir_fd=self.parent_descriptor,
            follow_symlinks=False,
        )
        if _object_identity(current) != _object_identity(completed):
            raise MaterialContractError(
                "Installer material temporary pathname changed before publication"
            )
        linked = False
        published: os.stat_result | None = None
        try:
            _link_release_file(
                self.temporary_name,
                self.output.name,
                parent_descriptor=self.parent_descriptor,
            )
            linked = True
            published = os.stat(
                self.output.name,
                dir_fd=self.parent_descriptor,
                follow_symlinks=False,
            )
            if _object_identity(published) != _object_identity(completed):
                raise MaterialContractError(
                    "Installer material publication identity changed"
                )
            os.unlink(self.temporary_name, dir_fd=self.parent_descriptor)
            self.temporary_name = ""
            final = os.stat(
                self.output.name,
                dir_fd=self.parent_descriptor,
                follow_symlinks=False,
            )
            held = os.fstat(stream.fileno())
            if (
                _stat_identity(final) != _stat_identity(held)
                or held.st_size != completed.st_size
                or held.st_mtime_ns != completed.st_mtime_ns
                or not bound_release_directory_matches(
                    self.output.parent,
                    self.parent_descriptor,
                )
            ):
                raise MaterialContractError(
                    "Installer material publication identity changed"
                )
            os.fsync(self.parent_descriptor)
        except BaseException:
            if linked and published is None:
                try:
                    published = os.stat(
                        self.output.name,
                        dir_fd=self.parent_descriptor,
                        follow_symlinks=False,
                    )
                except OSError:
                    pass
            if published is not None:
                self._unlink_if_identity(
                    self.output.name,
                    published,
                )
            raise

    def _publish_windows_fallback(
        self,
        stream: BinaryIO,
        completed: os.stat_result,
    ) -> None:
        assert self.temporary_path is not None
        current = self.temporary_path.lstat()
        if _object_identity(current) != _object_identity(completed):
            raise MaterialContractError(
                "Installer material temporary pathname changed before publication"
            )
        stream.close()
        os.replace(self.temporary_path, self.output)
        self.temporary_path = None
        published = self.output.lstat()
        if (
            _stat_identity(published) != _stat_identity(completed)
            or published.st_mtime_ns != completed.st_mtime_ns
        ):
            try:
                current = self.output.lstat()
                if _object_identity(current) == _object_identity(published):
                    self.output.unlink()
            except OSError:
                pass
            raise MaterialContractError(
                "Installer material publication identity changed"
            )

    def _unlink_if_identity(
        self,
        name: str,
        expected: os.stat_result,
    ) -> None:
        try:
            current = os.stat(
                name,
                dir_fd=self.parent_descriptor,
                follow_symlinks=False,
            )
            if _object_identity(current) == _object_identity(expected):
                os.unlink(name, dir_fd=self.parent_descriptor)
        except OSError:
            pass

    def _cleanup_temporary(self) -> None:
        if self.created is None:
            return
        if self.parent_descriptor >= 0 and self.temporary_name:
            self._unlink_if_identity(self.temporary_name, self.created)
        elif self.temporary_path is not None:
            try:
                current = self.temporary_path.lstat()
                if _object_identity(current) == _object_identity(self.created):
                    self.temporary_path.unlink()
            except OSError:
                pass

    def _close_owned_resources(self) -> None:
        if self.descriptor >= 0:
            os.close(self.descriptor)
            self.descriptor = -1
        if self.parent_descriptor >= 0:
            os.close(self.parent_descriptor)
            self.parent_descriptor = -1


class DuplicateJsonFieldError(ValueError):
    pass


def reject_duplicate_json_keys(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise DuplicateJsonFieldError(f"Duplicate JSON field: {key}")
        result[key] = value
    return result


@contextmanager
def _open_single_link_regular_file(
    source: Path,
    *,
    subject: str,
    maximum: int,
    allow_empty: bool = True,
) -> Iterator[tuple[BinaryIO, os.stat_result]]:
    try:
        before = source.lstat()
    except OSError as error:
        raise MaterialContractError(f"{subject} is unavailable") from error
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
        or before.st_size > maximum
        or (not allow_empty and before.st_size <= 0)
    ):
        raise MaterialContractError(
            f"{subject} is not a bounded single-link regular file"
        )
    descriptor = -1
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(source, flags)
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or (
                opened.st_dev,
                opened.st_ino,
                opened.st_nlink,
                opened.st_size,
            )
            != (
                before.st_dev,
                before.st_ino,
                before.st_nlink,
                before.st_size,
            )
            or opened.st_mtime_ns != before.st_mtime_ns
        ):
            raise MaterialContractError(f"{subject} changed while opening")
        stream = os.fdopen(descriptor, "rb", closefd=True)
        descriptor = -1
        with stream:
            yield stream, opened
            after = os.fstat(stream.fileno())
        if (
            _stat_identity(after) != _stat_identity(opened)
            or _stat_content_state(after) != _stat_content_state(opened)
        ):
            raise MaterialContractError(f"{subject} changed while reading")
    except MaterialContractError:
        raise
    except OSError as error:
        raise MaterialContractError(f"{subject} is unreadable") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _validate_dynamic_material(relative: str, value: bytes) -> None:
    if relative != PLATFORM_QUALIFICATION_MATERIAL:
        return
    try:
        parse_platform_qualification(value)
    except PlatformQualificationError as error:
        raise MaterialContractError(
            "Platform qualification material is invalid"
        ) from error


def _validate_relative_path(value: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise MaterialContractError("Installer material path is invalid")
    parsed = PurePosixPath(value)
    if parsed.is_absolute() or any(part in {"", ".", ".."} for part in parsed.parts):
        raise MaterialContractError("Installer material path is invalid")
    canonical = parsed.as_posix()
    if canonical != value:
        raise MaterialContractError("Installer material path is not canonical")
    return canonical


def _source_bytes(root: Path, relative: str) -> bytes:
    relative = _validate_relative_path(relative)
    source = root.joinpath(*PurePosixPath(relative).parts)
    return _read_single_link_regular_file(source, relative)


def _read_single_link_regular_file(source: Path, relative: str) -> bytes:
    return read_bounded_release_file(
        source,
        subject=f"Installer material source {relative}",
        maximum=MAX_MATERIAL_FILE_BYTES,
    )


def read_bounded_release_file(
    source: Path,
    *,
    subject: str,
    maximum: int,
    allow_empty: bool = True,
) -> bytes:
    with _open_single_link_regular_file(
        source,
        subject=subject,
        maximum=maximum,
        allow_empty=allow_empty,
    ) as (stream, opened):
        value = bytearray()
        while True:
            remaining = maximum + 1 - len(value)
            chunk = stream.read(min(1024 * 1024, remaining))
            if not chunk:
                break
            value.extend(chunk)
            if len(value) > maximum:
                raise MaterialContractError(f"{subject} is too large")
        if len(value) != opened.st_size:
            raise MaterialContractError(f"{subject} changed while reading")
    return bytes(value)


def _direct_source_bytes(source: Path, relative: str) -> bytes:
    return _read_single_link_regular_file(source, relative)


def _profile_paths(
    root: Path,
    wheelhouse: Path,
    initial_trust_kit: Path,
    formal_windows_pretrust_kit: Path,
) -> list[tuple[str, Path]]:
    result = [(relative, root) for relative in _FIXED_DEPLOYMENT_FILES]
    for package in ("durability", "release", "updater", "installer"):
        package_root = root / package
        if package == "installer" and not package_root.exists():
            continue
        if not package_root.is_dir() or package_root.is_symlink():
            raise MaterialContractError(
                f"Installer material package is unavailable: {package}"
            )
        for source in package_root.rglob("*"):
            relative_parts = source.relative_to(root).parts
            if "__pycache__" in relative_parts or "tests" in relative_parts:
                continue
            if source.is_file() or source.is_symlink():
                result.append((source.relative_to(root).as_posix(), root))

    if not wheelhouse.is_dir() or wheelhouse.is_symlink():
        raise MaterialContractError("Offline wheelhouse is unavailable")
    wheels = sorted(wheelhouse.iterdir(), key=lambda item: item.name)
    if not wheels or any(item.suffix != ".whl" for item in wheels):
        raise MaterialContractError("Offline wheelhouse must contain only wheel files")
    result.extend((f"wheelhouse/{wheel.name}", wheel) for wheel in wheels)

    from release.trust_bootstrap import validate_initial_trust_kit

    try:
        validate_initial_trust_kit(initial_trust_kit)
    except ValueError as error:
        raise MaterialContractError("Initial pretrust kit is invalid") from error
    result.extend(
        (
            f"{INITIAL_TRUST_KIT_PREFIX}/{name}",
            initial_trust_kit / name,
        )
        for name in sorted(INITIAL_TRUST_KIT_FILES)
    )

    try:
        FormalWindowsPretrustedTrustMaterial.load(formal_windows_pretrust_kit)
    except (OSError, FormalWindowsPretrustError) as error:
        raise MaterialContractError(
            "Formal Windows pretrust kit is invalid"
        ) from error
    result.extend(
        (
            f"{FORMAL_WINDOWS_PRETRUST_PREFIX}/{name}",
            formal_windows_pretrust_kit / name,
        )
        for name in sorted(FORMAL_WINDOWS_PRETRUST_FILES)
    )

    paths = [relative for relative, _ in result]
    if len(paths) > MAX_MATERIAL_FILES or len(paths) != len(set(paths)):
        raise MaterialContractError(
            "Installer material profile is duplicate or too large"
        )
    return sorted(result, key=lambda item: item[0])


def _mode_for(relative: str) -> int:
    return (
        0o755
        if relative.endswith(".sh")
        or relative == "deploy/updater/animemo"
        or relative == "deploy/updater/animemo-updater"
        or relative == OFFLINE_RELEASE_VERIFIER_MATERIAL
        or relative
        == f"{INITIAL_TRUST_KIT_PREFIX}/offline-release-verifier"
        or relative
        in {
            f"{FORMAL_WINDOWS_PRETRUST_PREFIX}/formal-release-verifier.exe",
            f"{FORMAL_WINDOWS_PRETRUST_PREFIX}/offline-release-verifier",
        }
        else 0o644
    )


def build_installer_materials(
    root: Path,
    *,
    wheelhouse: Path,
    output: Path,
    initial_trust_kit: Path | None = None,
    formal_windows_pretrust_kit: Path | None = None,
) -> MaterialArchiveIdentity:
    root = root.resolve()
    wheelhouse = wheelhouse.resolve()
    output = output.resolve()
    if initial_trust_kit is None:
        raise MaterialContractError("Initial pretrust kit is required")
    initial_trust_kit = initial_trust_kit.resolve()
    if formal_windows_pretrust_kit is None:
        raise MaterialContractError("Formal Windows pretrust kit is required")
    formal_windows_pretrust_kit = formal_windows_pretrust_kit.resolve()
    files: list[MaterialFileIdentity] = []
    total_bytes = 0
    output.parent.mkdir(parents=True, exist_ok=True)
    with AtomicReleaseFile(output) as atomic:
        assert atomic.created is not None
        with atomic.open_stream() as stream:
            with tarfile.open(
                fileobj=stream,
                mode="w:",
                format=tarfile.USTAR_FORMAT,
            ) as archive:
                for relative, source_root in _profile_paths(
                    root,
                    wheelhouse,
                    initial_trust_kit,
                    formal_windows_pretrust_kit,
                ):
                    value = (
                        _direct_source_bytes(source_root, relative)
                        if source_root.is_file()
                        else _source_bytes(source_root, relative)
                    )
                    _validate_dynamic_material(relative, value)
                    total_bytes += len(value)
                    if total_bytes > MAX_MATERIAL_TOTAL_BYTES:
                        raise MaterialContractError(
                            "Installer material profile exceeds its byte ceiling"
                        )
                    mode = _mode_for(relative)
                    identity = MaterialFileIdentity(
                        path=relative,
                        sha256=_sha256_bytes(value),
                        size=len(value),
                        mode=mode,
                    )
                    files.append(identity)
                    member = tarfile.TarInfo(relative)
                    member.size = len(value)
                    member.mode = mode
                    member.mtime = 0
                    member.uid = 0
                    member.gid = 0
                    member.uname = ""
                    member.gname = ""
                    archive.addfile(member, io.BytesIO(value))
            stream.flush()
            os.fsync(stream.fileno())
            completed = os.fstat(stream.fileno())
            if (
                _object_identity(completed) != _object_identity(atomic.created)
                or completed.st_nlink != 1
            ):
                raise MaterialContractError(
                    "Installer material temporary file identity changed"
                )
            stream.seek(0)
            archive_digest = hashlib.sha256()
            archive_size = 0
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                archive_size += len(chunk)
                archive_digest.update(chunk)
            after_hash = os.fstat(stream.fileno())
            if (
                archive_size != completed.st_size
                or _stat_identity(after_hash) != _stat_identity(completed)
                or _stat_content_state(after_hash) != _stat_content_state(completed)
            ):
                raise MaterialContractError(
                    "Installer material temporary file changed while hashing"
                )
            archive_sha256 = "sha256:" + archive_digest.hexdigest()
            atomic.publish(stream, completed)
        return MaterialArchiveIdentity(
            sha256=archive_sha256,
            size=archive_size,
            files=tuple(files),
        )


def inspect_installer_materials(archive_path: Path) -> MaterialArchiveIdentity:
    files: list[MaterialFileIdentity] = []
    total = 0
    try:
        with _open_single_link_regular_file(
            archive_path,
            subject="Installer material archive",
            maximum=MAX_MATERIAL_TOTAL_BYTES + MAX_MATERIAL_FILES * 2048,
            allow_empty=False,
        ) as (stream, archive_stat):
            archive_digest = hashlib.sha256()
            hashed_size = 0
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                hashed_size += len(chunk)
                archive_digest.update(chunk)
            if hashed_size != archive_stat.st_size:
                raise MaterialContractError(
                    "Installer material archive changed while hashing"
                )
            stream.seek(0)
            with tarfile.open(fileobj=stream, mode="r:") as archive:
                members = archive.getmembers()
                if not members or len(members) > MAX_MATERIAL_FILES:
                    raise MaterialContractError(
                        "Installer material archive has an invalid file count"
                    )
                for member in members:
                    relative = _validate_relative_path(member.name)
                    if (
                        not member.isfile()
                        or member.size < 0
                        or member.size > MAX_MATERIAL_FILE_BYTES
                        or stat.S_IMODE(member.mode) not in {0o644, 0o755}
                        or member.mtime != 0
                        or member.uid != 0
                        or member.gid != 0
                        or member.uname != ""
                        or member.gname != ""
                    ):
                        raise MaterialContractError(
                            "Installer material archive entry is invalid"
                        )
                    source = archive.extractfile(member)
                    if source is None:
                        raise MaterialContractError(
                            "Installer material archive entry is unreadable"
                        )
                    value = source.read(MAX_MATERIAL_FILE_BYTES + 1)
                    if len(value) != member.size:
                        raise MaterialContractError(
                            "Installer material archive entry size differs"
                        )
                    _validate_dynamic_material(relative, value)
                    total += len(value)
                    if total > MAX_MATERIAL_TOTAL_BYTES:
                        raise MaterialContractError(
                            "Installer material archive exceeds its byte ceiling"
                        )
                    files.append(
                        MaterialFileIdentity(
                            path=relative,
                            sha256=_sha256_bytes(value),
                            size=len(value),
                            mode=stat.S_IMODE(member.mode),
                        )
                    )
    except (OSError, tarfile.TarError) as error:
        raise MaterialContractError(
            "Installer material archive is not an uncompressed tar"
        ) from error
    paths = [item.path for item in files]
    if paths != sorted(paths) or len(paths) != len(set(paths)):
        raise MaterialContractError(
            "Installer material archive is duplicate or unordered"
        )
    return MaterialArchiveIdentity(
        sha256="sha256:" + archive_digest.hexdigest(),
        size=archive_stat.st_size,
        files=tuple(files),
    )


def _canonical_member_aggregate(
    files: tuple[MaterialFileIdentity, ...],
) -> str:
    ordered = sorted(files, key=lambda item: item.path)
    serialized = json.dumps(
        [item.as_dict() for item in ordered],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return _sha256_bytes(serialized)


def _required_material_identity(
    files: tuple[MaterialFileIdentity, ...], path: str
) -> MaterialFileIdentity:
    matches = [item for item in files if item.path == path]
    if len(matches) != 1:
        raise MaterialContractError(
            f"Installer material profile lacks exact required material: {path}"
        )
    return matches[0]


def _bound_material(identity: MaterialFileIdentity) -> dict[str, object]:
    return {
        "path": identity.path,
        "sha256": identity.sha256,
        "size": identity.size,
    }


def _validate_git_identity(value: str, *, label: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{40}", value):
        raise MaterialContractError(f"{label} is invalid")
    return value


def build_prepublication_material_identity(
    *,
    installer_materials: Path,
    deployment_contract: Path,
    candidate_sha: str,
    candidate_tree_sha: str,
) -> dict[str, object]:
    """Bind exact qualified prepublication bytes through a closed v3 contract."""
    candidate_sha = _validate_git_identity(candidate_sha, label="Candidate SHA")
    candidate_tree_sha = _validate_git_identity(
        candidate_tree_sha, label="Candidate tree SHA"
    )
    archive = inspect_installer_materials(installer_materials)
    try:
        formal_windows = inspect_formal_windows_pretrust_in_installer_materials(
            installer_materials
        )
    except FormalWindowsPretrustError as error:
        raise MaterialContractError(
            "Formal Windows pretrust installer binding is invalid"
        ) from error
    contract_bytes = _direct_source_bytes(
        deployment_contract, DEPLOYMENT_CONTRACT_NAME
    )
    try:
        contract = json.loads(
            contract_bytes,
            object_pairs_hook=reject_duplicate_json_keys,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, DuplicateJsonFieldError) as error:
        raise MaterialContractError("Deployment contract is not valid JSON") from error
    from .contract import ReleaseContractError, validate_deployment_contract

    try:
        validate_deployment_contract(contract)
    except ReleaseContractError as error:
        raise MaterialContractError(str(error)) from error
    if contract["archive"] != {
        "name": INSTALLER_MATERIALS_NAME,
        "sha256": archive.sha256,
        "size": archive.size,
        "format": "tar",
    } or contract["materials"] != [item.as_dict() for item in archive.files]:
        raise MaterialContractError(
            "Deployment contract installer materials identity differs"
        )
    archive_files = {item.path: item for item in archive.files}
    for declared in contract["files"]:
        archived = archive_files.get(declared["path"])
        if archived is None or archived.sha256 != declared["sha256"]:
            raise MaterialContractError(
                "Deployment contract file identity differs from installer materials"
            )

    platform = _required_material_identity(
        archive.files, PLATFORM_QUALIFICATION_MATERIAL
    )
    initial_trust = _required_material_identity(
        archive.files,
        f"{INITIAL_TRUST_KIT_PREFIX}/initial-trust-bootstrap.json",
    )
    verifier = _required_material_identity(
        archive.files, OFFLINE_RELEASE_VERIFIER_MATERIAL
    )
    pretrust_verifier = _required_material_identity(
        archive.files, f"{INITIAL_TRUST_KIT_PREFIX}/offline-release-verifier"
    )
    if (
        verifier.sha256 != pretrust_verifier.sha256
        or verifier.size != pretrust_verifier.size
    ):
        raise MaterialContractError(
            "Top-level and pretrust offline verifier identities differ"
        )
    wheels = tuple(
        item for item in archive.files if item.path.startswith("wheelhouse/")
    )
    pretrust = tuple(
        item
        for item in archive.files
        if item.path.startswith(f"{INITIAL_TRUST_KIT_PREFIX}/")
    )
    expected_pretrust = {
        f"{INITIAL_TRUST_KIT_PREFIX}/{name}" for name in INITIAL_TRUST_KIT_FILES
    }
    if not wheels or {item.path for item in pretrust} != expected_pretrust:
        raise MaterialContractError(
            "Installer material profile has an invalid wheelhouse or pretrust set"
        )
    return {
        "schemaVersion": PREPUBLICATION_SCHEMA_VERSION,
        "candidateSha": candidate_sha,
        "candidateTreeSha": candidate_tree_sha,
        "installerMaterials": {
            "name": INSTALLER_MATERIALS_NAME,
            "sha256": archive.sha256,
            "size": archive.size,
            "memberCount": len(archive.files),
            "memberManifestSha256": _canonical_member_aggregate(archive.files),
        },
        "deploymentContract": {
            "name": DEPLOYMENT_CONTRACT_NAME,
            "sha256": _sha256_bytes(contract_bytes),
            "size": len(contract_bytes),
        },
        "platformQualification": _bound_material(platform),
        "initialTrustBootstrap": _bound_material(initial_trust),
        "offlineVerifier": _bound_material(verifier),
        "formalWindowsPretrust": formal_windows.as_prepublication_record(),
        "wheelhouse": {
            "memberCount": len(wheels),
            "aggregateSha256": _canonical_member_aggregate(wheels),
        },
        "pretrust": {
            "memberCount": len(pretrust),
            "aggregateSha256": _canonical_member_aggregate(pretrust),
        },
    }


def verify_prepublication_material_identity(
    payload: object,
    *,
    installer_materials: Path,
    deployment_contract: Path,
    expected_candidate_sha: str,
    expected_candidate_tree_sha: str,
) -> dict[str, object]:
    """Recompute every frozen v2 binding and fail closed on any mismatch."""
    if not isinstance(payload, dict):
        raise MaterialContractError(
            "Prepublication material identity must be a JSON object"
        )
    if payload.get("schemaVersion") != PREPUBLICATION_SCHEMA_VERSION:
        raise MaterialContractError(
            "Prepublication material identity schema is unsupported"
        )
    expected = build_prepublication_material_identity(
        installer_materials=installer_materials,
        deployment_contract=deployment_contract,
        candidate_sha=expected_candidate_sha,
        candidate_tree_sha=expected_candidate_tree_sha,
    )
    if payload != expected:
        raise MaterialContractError("Frozen prepublication material identity differs")
    return {
        "status": "PASS",
        "schemaVersion": PREPUBLICATION_SCHEMA_VERSION,
        "candidateSha": expected_candidate_sha,
        "candidateTreeSha": expected_candidate_tree_sha,
        "installerMaterialsSha256": expected["installerMaterials"]["sha256"],
        "memberManifestSha256": expected["installerMaterials"][
            "memberManifestSha256"
        ],
        "wheelhouseAggregateSha256": expected["wheelhouse"]["aggregateSha256"],
        "pretrustAggregateSha256": expected["pretrust"]["aggregateSha256"],
        "formalWindowsPretrustKitIdentity": expected[
            "formalWindowsPretrust"
        ]["kitIdentity"],
        "offlineReleaseTrustProfileIdentity": expected[
            "formalWindowsPretrust"
        ]["sourceProfileIdentity"],
        "deploymentContractSha256": expected["deploymentContract"]["sha256"],
        "fallbackPolicy": "FORBIDDEN",
        "actionsArtifactRole": "TRANSPORT_AND_QUALIFICATION_EVIDENCE",
        "releaseAuthority": "GITHUB_IMMUTABLE_RELEASE",
    }


def extract_qualification_artifact(
    archive_path: Path,
    destination: Path,
    *,
    qualification_run_id: int,
    expected_sha256: str,
    require_candidate_contract: bool = False,
) -> dict[str, object]:
    """Extract the closed Candidate qualification transport without ZIP trust."""
    if (
        not isinstance(qualification_run_id, int)
        or isinstance(qualification_run_id, bool)
        or qualification_run_id <= 0
    ):
        raise MaterialContractError("Qualification run ID is invalid")
    if not isinstance(expected_sha256, str) or not re.fullmatch(
        r"sha256:[0-9a-f]{64}", expected_sha256
    ):
        raise MaterialContractError("Qualification artifact digest is invalid")
    legacy_expected = set(LEGACY_QUALIFICATION_ROOT_FILES)
    legacy_expected.add(f"release-qualification-{qualification_run_id}.json")
    if destination.exists() or destination.is_symlink():
        raise MaterialContractError(
            "Qualification artifact destination must not exist"
        )
    try:
        with _open_single_link_regular_file(
            archive_path,
            subject="Qualification artifact",
            maximum=MAX_QUALIFICATION_ARTIFACT_BYTES,
            allow_empty=False,
        ) as (stream, archive_stat):
            digest = hashlib.sha256()
            hashed_size = 0
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                hashed_size += len(chunk)
                digest.update(chunk)
            if (
                hashed_size != archive_stat.st_size
                or "sha256:" + digest.hexdigest() != expected_sha256
            ):
                raise MaterialContractError(
                    "Qualification artifact digest differs"
                )
            stream.seek(0)
            with zipfile.ZipFile(stream, mode="r") as archive:
                entries = archive.infolist()
                names = [entry.filename for entry in entries]
                for entry in entries:
                    _validate_relative_path(entry.filename)
                    unix_mode = entry.external_attr >> 16
                    file_type = stat.S_IFMT(unix_mode)
                    if (
                        entry.is_dir()
                        or entry.flag_bits & 0x1
                        or file_type not in {0, stat.S_IFREG}
                        or entry.file_size < 0
                        or entry.file_size > MAX_QUALIFICATION_MEMBER_BYTES
                    ):
                        raise MaterialContractError(
                            "Qualification artifact ZIP entry is invalid"
                        )
                candidate_entries = [
                    entry for entry in entries if entry.filename == "candidate-input.json"
                ]
                if len(candidate_entries) > 1 or (
                    require_candidate_contract and len(candidate_entries) != 1
                ):
                    raise MaterialContractError(
                        "Qualification Candidate Input cardinality differs"
                    )
                if candidate_entries:
                    candidate_entry = candidate_entries[0]
                    if candidate_entry.file_size > MAX_MATERIAL_FILE_BYTES:
                        raise MaterialContractError(
                            "Qualification Candidate Input is too large"
                        )
                    try:
                        candidate_value = json.loads(
                            archive.read(candidate_entry),
                            object_pairs_hook=reject_duplicate_json_keys,
                        )
                        from .candidate import validate_candidate_input

                        candidate = validate_candidate_input(candidate_value)
                    except Exception as error:
                        raise MaterialContractError(
                            "Qualification Candidate Input is invalid"
                        ) from error
                    runtime = {
                        item["path"]
                        for item in candidate["candidate_runtime_file_inventory"]
                    }
                    expected = set(CANDIDATE_QUALIFICATION_ROOT_FILES)
                    expected.add(f"release-qualification-{qualification_run_id}.json")
                    expected.update(runtime)
                else:
                    expected = legacy_expected
                if (
                    len(entries) != len(expected)
                    or len(names) != len(set(names))
                    or set(names) != expected
                    or sum(entry.file_size for entry in entries)
                    > MAX_QUALIFICATION_ARTIFACT_BYTES
                ):
                    raise MaterialContractError(
                        "Qualification artifact file set differs"
                    )
                destination.mkdir(parents=True, mode=0o700)
                identities = []
                for entry in sorted(entries, key=lambda item: item.filename):
                    target = destination / entry.filename
                    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    member_digest = hashlib.sha256()
                    written = 0
                    with archive.open(entry, mode="r") as source, target.open(
                        "xb"
                    ) as output:
                        while chunk := source.read(1024 * 1024):
                            written += len(chunk)
                            if written > entry.file_size:
                                raise MaterialContractError(
                                    "Qualification artifact ZIP entry exceeds its size"
                                )
                            member_digest.update(chunk)
                            output.write(chunk)
                    if written != entry.file_size:
                        raise MaterialContractError(
                            "Qualification artifact ZIP entry size differs"
                        )
                    target.chmod(0o600)
                    identities.append(
                        {
                            "name": entry.filename,
                            "sha256": "sha256:" + member_digest.hexdigest(),
                            "size": written,
                        }
                    )
    except (OSError, zipfile.BadZipFile, RuntimeError) as error:
        shutil.rmtree(destination, ignore_errors=True)
        raise MaterialContractError(
            "Qualification artifact is not a valid ZIP"
        ) from error
    except BaseException:
        shutil.rmtree(destination, ignore_errors=True)
        raise
    return {
        "status": "PASS",
        "fileCount": len(identities),
        "files": identities,
        "role": "TRANSPORT_AND_QUALIFICATION_EVIDENCE",
        "releaseAuthority": "GITHUB_IMMUTABLE_RELEASE",
    }


def _parse_material_contract(
    payload: object,
) -> tuple[dict[str, object], tuple[MaterialFileIdentity, ...]]:
    if not isinstance(payload, dict) or set(payload) != {
        "schemaVersion",
        "profile",
        "platform",
        "archive",
        "materials",
    }:
        raise MaterialContractError("Installer material contract has an invalid shape")
    if (
        payload["schemaVersion"] != 2
        or payload["profile"] != "v1.1-instance-scoped"
        or payload["platform"] != "linux/amd64"
    ):
        raise MaterialContractError(
            "Installer material contract has an unsupported profile"
        )
    archive = payload["archive"]
    if (
        not isinstance(archive, dict)
        or set(archive) != {"name", "sha256", "size", "format"}
        or archive["name"] != INSTALLER_MATERIALS_NAME
        or archive["format"] != "tar"
        or not isinstance(archive["sha256"], str)
        or not re.fullmatch(r"sha256:[0-9a-f]{64}", archive["sha256"])
        or not isinstance(archive["size"], int)
        or isinstance(archive["size"], bool)
        or archive["size"] <= 0
        or archive["size"] > MAX_MATERIAL_TOTAL_BYTES + MAX_MATERIAL_FILES * 2048
    ):
        raise MaterialContractError("Installer material archive identity is invalid")
    raw_materials = payload["materials"]
    if (
        not isinstance(raw_materials, list)
        or not raw_materials
        or len(raw_materials) > MAX_MATERIAL_FILES
    ):
        raise MaterialContractError("Installer material file list is invalid")
    materials: list[MaterialFileIdentity] = []
    total = 0
    for item in raw_materials:
        if (
            not isinstance(item, dict)
            or set(item) != {"path", "sha256", "size", "mode"}
            or not isinstance(item.get("sha256"), str)
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", item["sha256"])
            or not isinstance(item.get("size"), int)
            or isinstance(item.get("size"), bool)
            or item["size"] < 0
            or item["size"] > MAX_MATERIAL_FILE_BYTES
            or item.get("mode") not in {"0644", "0755"}
        ):
            raise MaterialContractError("Installer material file identity is invalid")
        relative = _validate_relative_path(item.get("path"))
        total += item["size"]
        if total > MAX_MATERIAL_TOTAL_BYTES:
            raise MaterialContractError(
                "Installer material file list exceeds its byte ceiling"
            )
        materials.append(
            MaterialFileIdentity(
                path=relative,
                sha256=item["sha256"],
                size=item["size"],
                mode=int(item["mode"], 8),
            )
        )
    paths = [item.path for item in materials]
    if paths != sorted(paths) or len(paths) != len(set(paths)):
        raise MaterialContractError(
            "Installer material file list is duplicate or unordered"
        )
    required_pretrust = {
        f"{INITIAL_TRUST_KIT_PREFIX}/{name}" for name in INITIAL_TRUST_KIT_FILES
    }
    if not required_pretrust.issubset(paths):
        raise MaterialContractError("Installer material profile lacks initial pretrust")
    required_formal_windows_pretrust = {
        f"{FORMAL_WINDOWS_PRETRUST_PREFIX}/{name}"
        for name in FORMAL_WINDOWS_PRETRUST_FILES
    }
    if not required_formal_windows_pretrust.issubset(paths):
        raise MaterialContractError(
            "Installer material profile lacks Formal Windows pretrust"
        )
    return archive, tuple(materials)


def validate_material_contract(
    payload: object,
) -> tuple[dict[str, object], tuple[MaterialFileIdentity, ...]]:
    """Validate the exact material profile through its public contract seam."""
    return _parse_material_contract(payload)


def extract_installer_materials(
    archive_path: Path,
    contract: object,
    destination: Path,
) -> VerifiedMaterialSet:
    archive_identity, materials = _parse_material_contract(contract)
    try:
        archive_stat = archive_path.lstat()
    except OSError as error:
        raise MaterialContractError(
            "Installer material archive is unavailable"
        ) from error
    if (
        archive_path.is_symlink()
        or not stat.S_ISREG(archive_stat.st_mode)
        or archive_stat.st_nlink != 1
        or archive_stat.st_size != archive_identity["size"]
    ):
        raise MaterialContractError("Installer material archive identity differs")
    if _sha256_file(archive_path) != archive_identity["sha256"]:
        raise MaterialContractError("Installer material archive checksum differs")
    if destination.exists() or destination.is_symlink():
        raise MaterialContractError("Installer material destination must not exist")
    destination.mkdir(parents=True, mode=0o700)
    try:
        try:
            with tarfile.open(archive_path, mode="r:") as archive:
                members = archive.getmembers()
                if len(members) != len(materials):
                    raise MaterialContractError(
                        "Installer material archive file set differs"
                    )
                for member, identity in zip(members, materials, strict=True):
                    if (
                        member.name != identity.path
                        or not member.isfile()
                        or member.size != identity.size
                        or stat.S_IMODE(member.mode) != identity.mode
                        or member.mtime != 0
                        or member.uid != 0
                        or member.gid != 0
                        or member.uname != ""
                        or member.gname != ""
                    ):
                        raise MaterialContractError(
                            "Installer material archive entry differs"
                        )
                    target = destination.joinpath(*PurePosixPath(identity.path).parts)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    source = archive.extractfile(member)
                    if source is None:
                        raise MaterialContractError(
                            "Installer material archive entry is unreadable"
                        )
                    digest = hashlib.sha256()
                    written = 0
                    with target.open("xb") as handle:
                        while chunk := source.read(1024 * 1024):
                            written += len(chunk)
                            if written > identity.size:
                                raise MaterialContractError(
                                    "Installer material archive entry exceeds its size"
                                )
                            digest.update(chunk)
                            handle.write(chunk)
                    if (
                        written != identity.size
                        or "sha256:" + digest.hexdigest() != identity.sha256
                    ):
                        raise MaterialContractError(
                            "Installer material archive entry checksum differs"
                        )
                    target.chmod(identity.mode)
        except (OSError, tarfile.TarError) as error:
            raise MaterialContractError(
                "Installer material archive is not an uncompressed tar"
            ) from error
        return VerifiedMaterialSet(
            root=destination,
            archive_sha256=archive_identity["sha256"],
            files=materials,
        )
    except BaseException:
        shutil.rmtree(destination, ignore_errors=True)
        raise
