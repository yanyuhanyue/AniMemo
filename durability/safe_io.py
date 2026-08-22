"""Durability-local protected file snapshot primitives."""

from __future__ import annotations

import errno
import os
import stat
from enum import Enum
from pathlib import Path

_NOFOLLOW_LINK_ERRNOS = frozenset(
    {errno.ELOOP, getattr(errno, "EMLINK", errno.ELOOP)}
)


class SafeReadReason(Enum):
    """Value-free reasons for a protected file snapshot failure."""

    MISSING = "MISSING"
    UNAVAILABLE = "UNAVAILABLE"
    UNSAFE = "UNSAFE"
    CHANGED = "CHANGED"
    BOUNDS = "BOUNDS"


class SafeReadError(Exception):
    """A protected file could not be captured as one bounded snapshot."""

    def __init__(self, reason: SafeReadReason) -> None:
        self.reason = reason
        super().__init__(reason.value)


def read_single_link_regular_file(path: Path, *, maximum: int) -> bytes:
    """Read one regular, singly linked file through one identity-bound handle."""

    if maximum < 0:
        raise SafeReadError(SafeReadReason.BOUNDS)
    try:
        item_stat = path.lstat()
    except FileNotFoundError:
        raise SafeReadError(SafeReadReason.MISSING) from None
    except OSError:
        raise SafeReadError(SafeReadReason.UNAVAILABLE) from None
    if _is_unsafe_file(item_stat):
        raise SafeReadError(SafeReadReason.UNSAFE)
    if item_stat.st_size > maximum:
        raise SafeReadError(SafeReadReason.BOUNDS)

    descriptor = -1
    try:
        try:
            descriptor = os.open(
                path,
                os.O_RDONLY
                | getattr(os, "O_BINARY", 0)
                | getattr(os, "O_NOFOLLOW", 0),
            )
        except FileNotFoundError:
            raise SafeReadError(SafeReadReason.CHANGED) from None
        except OSError as error:
            if error.errno in _NOFOLLOW_LINK_ERRNOS:
                raise SafeReadError(SafeReadReason.CHANGED) from None
            raise SafeReadError(SafeReadReason.UNAVAILABLE) from None

        try:
            opened_stat = os.fstat(descriptor)
        except OSError:
            raise SafeReadError(SafeReadReason.UNAVAILABLE) from None
        if _is_unsafe_file(opened_stat):
            raise SafeReadError(SafeReadReason.CHANGED)
        if opened_stat.st_size > maximum:
            raise SafeReadError(SafeReadReason.BOUNDS)
        if _path_binding_state(opened_stat) != _path_binding_state(item_stat):
            raise SafeReadError(SafeReadReason.CHANGED)

        payload = bytearray()
        while len(payload) <= maximum:
            try:
                chunk = os.read(
                    descriptor,
                    min(1024 * 1024, maximum + 1 - len(payload)),
                )
            except OSError:
                raise SafeReadError(SafeReadReason.UNAVAILABLE) from None
            if not chunk:
                break
            payload.extend(chunk)
        if len(payload) > maximum:
            raise SafeReadError(SafeReadReason.BOUNDS)

        try:
            after_stat = os.fstat(descriptor)
        except OSError:
            raise SafeReadError(SafeReadReason.UNAVAILABLE) from None
        if (
            _content_state(after_stat) != _content_state(opened_stat)
            or len(payload) != opened_stat.st_size
        ):
            raise SafeReadError(SafeReadReason.CHANGED)
        return bytes(payload)
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _is_unsafe_file(metadata: os.stat_result) -> bool:
    attributes = getattr(metadata, "st_file_attributes", 0)
    return (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or stat.S_ISLNK(metadata.st_mode)
        or bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
    )


def _path_binding_state(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_nlink,
        metadata.st_size,
        metadata.st_mtime_ns,
    )


def _content_state(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_nlink,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )
