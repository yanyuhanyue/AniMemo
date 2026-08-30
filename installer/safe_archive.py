"""Fail-closed ZIP extraction primitives shared by installer runtimes."""

from __future__ import annotations

import os
import re
import shutil
import stat
import struct
import tempfile
import unicodedata
import zlib
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZIP_STORED, BadZipFile, ZipFile, ZipInfo

_LOCAL_FILE_HEADER = struct.Struct("<4s5H3L2H")
_LOCAL_FILE_SIGNATURE = b"PK\x03\x04"
_UTF8_FILENAME_FLAG = 0x800
_UNSAFE_ZIP_FLAGS = 0x1 | 0x8 | 0x20 | 0x40
_ALLOWED_COMPRESSION = frozenset({ZIP_STORED, ZIP_DEFLATED})
_WINDOWS_RESERVED_STEMS = frozenset(
    {"aux", "con", "conin$", "conout$", "nul", "prn"}
    | {f"com{index}" for index in range(1, 10)}
    | {f"lpt{index}" for index in range(1, 10)}
    | {
        f"{prefix}{index}"
        for prefix in ("com", "lpt")
        for index in "\u00b9\u00b2\u00b3"
    }
)


class SafeArchiveError(RuntimeError):
    pass


@dataclass(frozen=True)
class ZipExtractionLimits:
    max_archives: int
    max_members: int
    max_member_bytes: int
    max_total_bytes: int
    max_compression_ratio: int
    chunk_bytes: int = 1024 * 1024

    def validate(self) -> None:
        values = (
            self.max_archives,
            self.max_members,
            self.max_member_bytes,
            self.max_total_bytes,
            self.max_compression_ratio,
            self.chunk_bytes,
        )
        if any(type(value) is not int or value < 1 for value in values):
            raise SafeArchiveError("SAFE_ARCHIVE_LIMITS_INVALID")


WHEEL_RUNTIME_LIMITS = ZipExtractionLimits(
    max_archives=256,
    max_members=100_000,
    max_member_bytes=2 * 1024 * 1024 * 1024,
    max_total_bytes=2 * 1024 * 1024 * 1024,
    max_compression_ratio=100,
)


@dataclass
class ZipExtractionBudget:
    limits: ZipExtractionLimits
    archive_count: int = 0
    member_count: int = 0
    file_count: int = 0
    declared_bytes: int = 0
    declared_compressed_bytes: int = 0
    streamed_bytes: int = 0
    archive_entry_identities: set[str] = field(default_factory=set)
    archive_path_identities: set[str] = field(default_factory=set)
    archive_directory_identities: set[str] = field(default_factory=set)
    archive_path_spellings: dict[str, str] = field(default_factory=dict)
    entry_identities: set[str] = field(default_factory=set)
    path_identities: set[str] = field(default_factory=set)
    directory_identities: set[str] = field(default_factory=set)
    path_spellings: dict[str, str] = field(default_factory=dict)


def _reject() -> None:
    raise SafeArchiveError("SAFE_ARCHIVE_INVALID")


def archive_member_path(value: str) -> tuple[str, ...]:
    if (
        type(value) is not str
        or not value
        or "\\" in value
        or "\x00" in value
        or value.startswith("/")
        or re.match(r"^[A-Za-z]:", value) is not None
    ):
        _reject()
    # ZIP directory entries use exactly one trailing slash.  Stripping an
    # arbitrary run here would silently accept empty path segments such as
    # ``directory//``.
    path_value = value.removesuffix("/")
    raw_parts = path_value.split("/")
    if not raw_parts or any(
        part in {"", ".", ".."}
        or ":" in part
        or part.rstrip(" .") != part
        or part.split(".", 1)[0].casefold() in _WINDOWS_RESERVED_STEMS
        for part in raw_parts
    ):
        _reject()
    normalized = tuple(unicodedata.normalize("NFC", part) for part in raw_parts)
    if any(not part for part in normalized):
        _reject()
    return normalized


def wheel_runtime_member_path(value: str) -> tuple[str, ...] | None:
    parts = archive_member_path(value)
    if len(parts) >= 3 and parts[0].endswith(".data"):
        if parts[1] not in {"purelib", "platlib"}:
            return None
        parts = parts[2:]
    if not parts:
        _reject()
    return parts


def _register_path(
    parts: tuple[str, ...],
    *,
    directory: bool,
    budget: ZipExtractionBudget,
) -> None:
    identities: list[str] = []
    for length in range(1, len(parts) + 1):
        spelling = "/".join(parts[:length])
        identity = spelling.casefold()
        existing = budget.path_spellings.get(identity)
        if existing is not None and existing != spelling:
            _reject()
        budget.path_spellings[identity] = spelling
        identities.append(identity)

    identity = identities[-1]
    if identity in budget.entry_identities:
        _reject()
    budget.entry_identities.add(identity)

    directory_parts = identities if directory else identities[:-1]
    if any(
        identity in budget.path_identities for identity in directory_parts
    ):
        _reject()
    budget.directory_identities.update(directory_parts)
    if directory:
        return
    if (
        identity in budget.path_identities
        or identity in budget.directory_identities
    ):
        _reject()
    budget.path_identities.add(identity)


def _register_archive_path(
    parts: tuple[str, ...],
    *,
    directory: bool,
    budget: ZipExtractionBudget,
) -> None:
    """Register every member, including members excluded from publication."""

    identities: list[str] = []
    for length in range(1, len(parts) + 1):
        spelling = "/".join(parts[:length])
        identity = spelling.casefold()
        existing = budget.archive_path_spellings.get(identity)
        if existing is not None and existing != spelling:
            _reject()
        budget.archive_path_spellings[identity] = spelling
        identities.append(identity)

    identity = identities[-1]
    if identity in budget.archive_entry_identities:
        _reject()
    budget.archive_entry_identities.add(identity)

    directory_parts = identities if directory else identities[:-1]
    if any(
        item in budget.archive_path_identities for item in directory_parts
    ):
        _reject()
    budget.archive_directory_identities.update(directory_parts)
    if directory:
        return
    if (
        identity in budget.archive_path_identities
        or identity in budget.archive_directory_identities
    ):
        _reject()
    budget.archive_path_identities.add(identity)


def _validate_member(
    info: ZipInfo,
    archive_parts: tuple[str, ...],
    parts: tuple[str, ...] | None,
    budget: ZipExtractionBudget,
) -> tuple[str, ...] | None:
    budget.member_count += 1
    if budget.member_count > budget.limits.max_members:
        _reject()
    if (
        type(info.filename) is not str
        or type(info.orig_filename) is not str
        or info.orig_filename != info.filename
        or "\x00" in info.orig_filename
        or info.flag_bits & _UNSAFE_ZIP_FLAGS
        or info.compress_type not in _ALLOWED_COMPRESSION
        or info.compress_size < 0
    ):
        _reject()
    mode = info.external_attr >> 16
    file_type = stat.S_IFMT(mode)
    if info.is_dir():
        if (
            file_type not in {0, stat.S_IFDIR}
            or info.file_size != 0
            or info.compress_size != 0
            or info.CRC != 0
        ):
            _reject()
        _register_archive_path(
            archive_parts,
            directory=True,
            budget=budget,
        )
        if parts is not None:
            _register_path(parts, directory=True, budget=budget)
        return None
    if file_type not in {0, stat.S_IFREG}:
        _reject()
    _register_archive_path(
        archive_parts,
        directory=False,
        budget=budget,
    )
    if (
        info.file_size < 0
        or info.file_size > budget.limits.max_member_bytes
        or info.compress_size > budget.limits.max_member_bytes
    ):
        _reject()
    if info.file_size:
        if info.compress_size <= 0:
            _reject()
        if (
            info.file_size / info.compress_size
            > budget.limits.max_compression_ratio
        ):
            _reject()
    budget.file_count += 1
    budget.declared_bytes += info.file_size
    budget.declared_compressed_bytes += info.compress_size
    if (
        budget.file_count > budget.limits.max_members
        or budget.declared_bytes > budget.limits.max_total_bytes
        or budget.declared_compressed_bytes > budget.limits.max_total_bytes
    ):
        _reject()
    if parts is None:
        return None
    _register_path(parts, directory=False, budget=budget)
    return parts


def _member_data_offset(archive: ZipFile, info: ZipInfo) -> int:
    source = archive.fp
    if source is None or info.header_offset < 0:
        _reject()
    source.seek(info.header_offset)
    encoded_header = source.read(_LOCAL_FILE_HEADER.size)
    if len(encoded_header) != _LOCAL_FILE_HEADER.size:
        _reject()
    header = _LOCAL_FILE_HEADER.unpack(encoded_header)
    flags = header[2]
    compression = header[3]
    checksum = header[6]
    compressed_size = header[7]
    uncompressed_size = header[8]
    name_size = header[9]
    extra_size = header[10]
    if (
        header[0] != _LOCAL_FILE_SIGNATURE
        or flags != info.flag_bits
        or flags & _UNSAFE_ZIP_FLAGS
        or compression != info.compress_type
        or compression not in _ALLOWED_COMPRESSION
        or checksum != info.CRC
        or compressed_size != info.compress_size
        or uncompressed_size != info.file_size
    ):
        _reject()
    encoded_name = source.read(name_size)
    if len(encoded_name) != name_size:
        _reject()
    try:
        name = encoded_name.decode(
            "utf-8"
            if flags & _UTF8_FILENAME_FLAG
            else archive.metadata_encoding or "cp437"
        )
    except UnicodeDecodeError:
        _reject()
    if name != info.orig_filename:
        _reject()
    data_offset = info.header_offset + _LOCAL_FILE_HEADER.size + name_size + extra_size
    end_offset = getattr(info, "_end_offset", None)
    if (
        type(end_offset) is not int
        or data_offset < info.header_offset
        or info.compress_size > end_offset - data_offset
    ):
        _reject()
    return data_offset


def _output_limit(
    info: ZipInfo,
    member_bytes: int,
    budget: ZipExtractionBudget,
) -> int:
    return max(
        1,
        min(
            budget.limits.chunk_bytes,
            info.file_size - member_bytes + 1,
            budget.limits.max_member_bytes - member_bytes + 1,
            budget.limits.max_total_bytes - budget.streamed_bytes + 1,
        ),
    )


def copy_zip_member(
    archive: ZipFile,
    info: ZipInfo,
    output,
    budget: ZipExtractionBudget,
) -> None:
    try:
        _copy_zip_member(archive, info, output, budget)
    except zlib.error as error:
        raise SafeArchiveError("SAFE_ARCHIVE_INVALID") from error


def _copy_zip_member(
    archive: ZipFile,
    info: ZipInfo,
    output,
    budget: ZipExtractionBudget,
) -> None:
    member_bytes = 0
    checksum = 0

    def emit(chunk: bytes) -> None:
        nonlocal checksum, member_bytes
        if not chunk:
            return
        member_bytes += len(chunk)
        budget.streamed_bytes += len(chunk)
        if (
            member_bytes > info.file_size
            or member_bytes > budget.limits.max_member_bytes
            or budget.streamed_bytes > budget.limits.max_total_bytes
        ):
            _reject()
        checksum = zlib.crc32(chunk, checksum)
        if output.write(chunk) != len(chunk):
            _reject()

    source = archive.fp
    if source is None:
        _reject()
    source.seek(_member_data_offset(archive, info))
    compressed_remaining = info.compress_size
    if info.compress_type == ZIP_STORED:
        if info.compress_size != info.file_size:
            _reject()
        while compressed_remaining:
            chunk = source.read(
                min(budget.limits.chunk_bytes, compressed_remaining)
            )
            if not chunk:
                _reject()
            compressed_remaining -= len(chunk)
            emit(chunk)
    else:
        decompressor = zlib.decompressobj(-zlib.MAX_WBITS)
        pending = b""
        while compressed_remaining or pending:
            if not pending:
                pending = source.read(
                    min(budget.limits.chunk_bytes, compressed_remaining)
                )
                if not pending:
                    _reject()
                compressed_remaining -= len(pending)
            before = len(pending)
            chunk = decompressor.decompress(
                pending,
                _output_limit(info, member_bytes, budget),
            )
            pending = decompressor.unconsumed_tail
            if decompressor.unused_data:
                _reject()
            emit(chunk)
            if decompressor.eof:
                if pending or compressed_remaining:
                    _reject()
                break
            if not chunk and len(pending) >= before:
                _reject()
        if not decompressor.eof:
            _reject()
    if (
        compressed_remaining
        or member_bytes != info.file_size
        or (checksum & 0xFFFFFFFF) != info.CRC
    ):
        _reject()


def validate_zip_archive(
    archive: ZipFile,
    *,
    member_path: Callable[[str], tuple[str, ...] | None],
    limits: ZipExtractionLimits,
    budget: ZipExtractionBudget | None = None,
) -> tuple[tuple[tuple[ZipInfo, tuple[str, ...]], ...], ZipExtractionBudget]:
    limits.validate()
    if budget is None:
        budget = ZipExtractionBudget(limits=limits)
    if budget.limits != limits:
        _reject()
    budget.archive_count += 1
    if budget.archive_count > limits.max_archives:
        _reject()
    plans: list[tuple[ZipInfo, tuple[str, ...]]] = []
    for info in archive.infolist():
        archive_parts = archive_member_path(info.filename)
        parts = member_path(info.filename)
        validated = _validate_member(info, archive_parts, parts, budget)
        if validated is not None:
            plans.append((info, validated))
    return tuple(plans), budget


def read_zip_member(
    archive: ZipFile,
    info: ZipInfo,
    budget: ZipExtractionBudget,
) -> bytes:
    output = BytesIO()
    copy_zip_member(archive, info, output, budget)
    return output.getvalue()


def extract_zip_archives(
    sources: Iterable[Path],
    destination: Path,
    *,
    member_path: Callable[[str], tuple[str, ...] | None],
    limits: ZipExtractionLimits,
    file_mode: int = 0o644,
    directory_mode: int = 0o755,
) -> None:
    """Extract archives into an atomically published, previously absent tree."""

    limits.validate()
    archives = tuple(Path(source) for source in sources)
    destination = Path(destination).absolute()
    parent = destination.parent
    if (
        not archives
        or len(archives) > limits.max_archives
        or destination.exists()
        or destination.is_symlink()
    ):
        _reject()
    try:
        parent_metadata = parent.lstat()
    except OSError as error:
        raise SafeArchiveError("SAFE_ARCHIVE_INVALID") from error
    if parent.is_symlink() or not stat.S_ISDIR(parent_metadata.st_mode):
        _reject()

    staging: Path | None = None
    budget = ZipExtractionBudget(limits=limits)
    try:
        staging = Path(
            tempfile.mkdtemp(
                prefix=f".{destination.name}.extract-",
                dir=parent,
            )
        )
        staging.chmod(directory_mode)
        for source_path in archives:
            with ZipFile(source_path, mode="r") as archive:
                plans, budget = validate_zip_archive(
                    archive,
                    member_path=member_path,
                    limits=limits,
                    budget=budget,
                )
                for info, parts in plans:
                    target = staging.joinpath(*parts)
                    target.parent.mkdir(
                        parents=True,
                        exist_ok=True,
                        mode=directory_mode,
                    )
                    with target.open("xb") as output:
                        copy_zip_member(archive, info, output, budget)
                    if target.stat().st_size != info.file_size:
                        _reject()
                    target.chmod(file_mode)
        if not budget.path_identities:
            _reject()
        for directory in sorted(
            (item for item in staging.rglob("*") if item.is_dir()),
            key=lambda item: len(item.parts),
            reverse=True,
        ):
            directory.chmod(directory_mode)
        if destination.exists() or destination.is_symlink():
            _reject()
        os.rename(staging, destination)
        staging = None
    except SafeArchiveError:
        raise
    except (BadZipFile, EOFError, OSError, RuntimeError, ValueError) as error:
        raise SafeArchiveError("SAFE_ARCHIVE_INVALID") from error
    finally:
        if staging is not None:
            shutil.rmtree(staging, ignore_errors=True)


def extract_wheel_runtime(sources: Iterable[Path], destination: Path) -> None:
    extract_zip_archives(
        sources,
        destination,
        member_path=wheel_runtime_member_path,
        limits=WHEEL_RUNTIME_LIMITS,
    )
