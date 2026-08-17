"""Shared implementation safety bounds for durability artifact I/O.

These limits do not change the identity or serialized semantics of Backup
Format v1 or Migration Bundle v1.  Producers and consumers enforce the same
provider-neutral byte budgets while streaming so declared metadata is never
trusted as the sole resource boundary.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from typing import BinaryIO, Final

COPY_CHUNK_BYTES: Final = 1024 * 1024


class ResourceLimitReason(StrEnum):
    COMPRESSED_MEMBER_BYTES = "COMPRESSED_MEMBER_BYTES"
    UNCOMPRESSED_DATABASE_BYTES = "UNCOMPRESSED_DATABASE_BYTES"
    FILESYSTEM_MEMBER_BYTES = "FILESYSTEM_MEMBER_BYTES"
    TOTAL_COPY_BYTES = "TOTAL_COPY_BYTES"
    COMPRESSION_RATIO = "COMPRESSION_RATIO"
    DECLARED_SIZE_MISMATCH = "DECLARED_SIZE_MISMATCH"


class ResourceLimitExceeded(RuntimeError):
    """A stable, value-free resource budget failure."""

    def __init__(self, reason: ResourceLimitReason) -> None:
        self.reason = reason
        super().__init__(reason.value)


@dataclass(frozen=True)
class DurabilityResourceBudget:
    maximum_compressed_member_bytes: int
    maximum_uncompressed_database_bytes: int
    maximum_filesystem_member_bytes: int
    maximum_total_copied_bytes: int
    maximum_compression_ratio: int

    def __post_init__(self) -> None:
        values = (
            self.maximum_compressed_member_bytes,
            self.maximum_uncompressed_database_bytes,
            self.maximum_filesystem_member_bytes,
            self.maximum_total_copied_bytes,
            self.maximum_compression_ratio,
        )
        if any(isinstance(value, bool) or not isinstance(value, int) or value <= 0 for value in values):
            raise ValueError("durability resource limits must be positive integers")


DEFAULT_RESOURCE_BUDGET: Final = DurabilityResourceBudget(
    maximum_compressed_member_bytes=8 * 1024 * 1024 * 1024,
    maximum_uncompressed_database_bytes=64 * 1024 * 1024 * 1024,
    maximum_filesystem_member_bytes=8 * 1024 * 1024 * 1024,
    maximum_total_copied_bytes=512 * 1024 * 1024 * 1024,
    maximum_compression_ratio=1_000,
)


@dataclass
class CopyByteCounter:
    maximum_bytes: int
    copied: int = 0

    def __post_init__(self) -> None:
        if (
            isinstance(self.maximum_bytes, bool)
            or not isinstance(self.maximum_bytes, int)
            or self.maximum_bytes <= 0
        ):
            raise ValueError("copy limit must be a positive integer")

    def consume(self, count: int) -> None:
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise ValueError("copy count must be a non-negative integer")
        if count > self.maximum_bytes - self.copied:
            raise ResourceLimitExceeded(ResourceLimitReason.TOTAL_COPY_BYTES)
        self.copied += count


def _write_all(target: BinaryIO, chunk: bytes) -> None:
    remaining = memoryview(chunk)
    while remaining:
        written = target.write(remaining)
        if (
            isinstance(written, bool)
            or not isinstance(written, int)
            or written <= 0
            or written > len(remaining)
        ):
            raise OSError("TARGET_WRITE_INCOMPLETE")
        remaining = remaining[written:]


def preflight_copy_sizes(
    sizes: Iterable[int],
    *,
    maximum_member_bytes: int,
    maximum_total_bytes: int,
    member_reason: ResourceLimitReason = ResourceLimitReason.FILESYSTEM_MEMBER_BYTES,
) -> int:
    """Validate declared/stat sizes before copying and return their total."""

    total = 0
    for size in sizes:
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise ResourceLimitExceeded(ResourceLimitReason.DECLARED_SIZE_MISMATCH)
        if size > maximum_member_bytes:
            raise ResourceLimitExceeded(member_reason)
        if size > maximum_total_bytes - total:
            raise ResourceLimitExceeded(ResourceLimitReason.TOTAL_COPY_BYTES)
        total += size
    return total


def bounded_copy(
    source: BinaryIO,
    target: BinaryIO,
    *,
    counter: CopyByteCounter,
    maximum_member_bytes: int,
    expected_size: int | None = None,
    chunk_bytes: int = COPY_CHUNK_BYTES,
    member_reason: ResourceLimitReason = ResourceLimitReason.FILESYSTEM_MEMBER_BYTES,
) -> int:
    """Copy one stream while enforcing declared, member, and aggregate bytes."""

    if expected_size is not None:
        preflight_copy_sizes(
            (expected_size,),
            maximum_member_bytes=maximum_member_bytes,
            maximum_total_bytes=counter.maximum_bytes - counter.copied,
            member_reason=member_reason,
        )
    if isinstance(chunk_bytes, bool) or not isinstance(chunk_bytes, int) or chunk_bytes <= 0:
        raise ValueError("copy chunk size must be a positive integer")

    copied = 0
    while True:
        remaining_declared = (
            None if expected_size is None else expected_size - copied
        )
        read_size = chunk_bytes
        if remaining_declared is not None:
            read_size = min(read_size, max(0, remaining_declared) + 1)
        chunk = source.read(read_size)
        if not chunk:
            break
        copied += len(chunk)
        if expected_size is not None and copied > expected_size:
            raise ResourceLimitExceeded(ResourceLimitReason.DECLARED_SIZE_MISMATCH)
        if copied > maximum_member_bytes:
            raise ResourceLimitExceeded(member_reason)
        counter.consume(len(chunk))
        _write_all(target, chunk)

    if expected_size is not None and copied != expected_size:
        raise ResourceLimitExceeded(ResourceLimitReason.DECLARED_SIZE_MISMATCH)
    return copied


@dataclass
class DatabaseExpansionGuard:
    compressed_bytes: int
    budget: DurabilityResourceBudget = DEFAULT_RESOURCE_BUDGET
    uncompressed_bytes: int = 0

    def __post_init__(self) -> None:
        if (
            isinstance(self.compressed_bytes, bool)
            or not isinstance(self.compressed_bytes, int)
            or self.compressed_bytes <= 0
            or self.compressed_bytes > self.budget.maximum_compressed_member_bytes
        ):
            raise ResourceLimitExceeded(ResourceLimitReason.COMPRESSED_MEMBER_BYTES)

    def consume(self, count: int) -> None:
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise ValueError("database byte count must be a non-negative integer")
        self.uncompressed_bytes += count
        if self.uncompressed_bytes > self.budget.maximum_uncompressed_database_bytes:
            raise ResourceLimitExceeded(
                ResourceLimitReason.UNCOMPRESSED_DATABASE_BYTES
            )
        if (
            self.uncompressed_bytes
            > self.compressed_bytes * self.budget.maximum_compression_ratio
        ):
            raise ResourceLimitExceeded(ResourceLimitReason.COMPRESSION_RATIO)
