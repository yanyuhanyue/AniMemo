from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import sys
from pathlib import Path

MAXIMUM_PATHS = 20_000
MAXIMUM_TOTAL_BYTES = 20 * 1024 * 1024 * 1024
_FIXED_GUEST_AUTHORITY_ROOTS = (
    Path("/var/lib/animemo/prepublication-candidates/v2"),
    Path("/var/lib/animemo/formal-authority"),
)


def closed_runtime_total_bytes(current: int, next_file_size: int) -> int:
    if (
        type(current) is not int
        or type(next_file_size) is not int
        or current < 0
        or next_file_size < 0
        or next_file_size > MAXIMUM_TOTAL_BYTES - current
    ):
        raise ValueError("closed runtime inventory exceeds its byte ceiling")
    return current + next_file_size


def closed_runtime_inventory_digest(root: Path) -> str:
    boundary = Path(root)
    if _descriptor_inventory_available():
        items = _descriptor_inventory(boundary)
    else:
        items = _fallback_inventory(boundary)
    encoded = (
        json.dumps(
            items,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _descriptor_inventory_available() -> bool:
    return (
        os.open in os.supports_dir_fd
        and os.stat in os.supports_dir_fd
        and os.listdir in os.supports_fd
        and bool(getattr(os, "O_DIRECTORY", 0))
        and bool(getattr(os, "O_NOFOLLOW", 0))
    )


def _directory_state(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_nlink,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _file_state(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_nlink,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _open_directory_chain(root: Path) -> int:
    absolute = Path(os.path.abspath(root))
    if not absolute.is_absolute():
        raise SystemExit(40)
    flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(absolute.anchor, flags)
    try:
        for component in absolute.parts[1:]:
            child = os.open(component, flags, dir_fd=descriptor)
            metadata = os.fstat(child)
            if not stat.S_ISDIR(metadata.st_mode):
                os.close(child)
                raise SystemExit(40)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _descriptor_inventory(root: Path) -> list[dict[str, object]]:
    descriptor = _open_directory_chain(root)
    items: list[dict[str, object]] = []
    total = 0
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
    file_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)

    def visit(directory: int, prefix: str) -> None:
        nonlocal total
        before = os.fstat(directory)
        names_before = sorted(os.listdir(directory))
        for name in names_before:
            if not name or name in {".", ".."} or "/" in name or "\x00" in name:
                raise SystemExit(42)
            relative = prefix + name
            metadata = os.stat(name, dir_fd=directory, follow_symlinks=False)
            if stat.S_ISLNK(metadata.st_mode):
                raise SystemExit(42)
            if len(items) >= MAXIMUM_PATHS:
                raise SystemExit(41)
            if stat.S_ISDIR(metadata.st_mode):
                child = os.open(name, directory_flags, dir_fd=directory)
                try:
                    opened = os.fstat(child)
                    if _directory_state(opened) != _directory_state(metadata):
                        raise SystemExit(42)
                    items.append({"path": relative + "/", "type": "directory"})
                    visit(child, relative + "/")
                finally:
                    os.close(child)
                continue
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise SystemExit(43)
            try:
                total = closed_runtime_total_bytes(total, metadata.st_size)
            except ValueError:
                raise SystemExit(44)
            file_descriptor = os.open(name, file_flags, dir_fd=directory)
            try:
                opened = os.fstat(file_descriptor)
                if _file_state(opened) != _file_state(metadata):
                    raise SystemExit(43)
                digest = hashlib.sha256()
                read_size = 0
                while block := os.read(
                    file_descriptor,
                    min(1024 * 1024, opened.st_size + 1 - read_size),
                ):
                    read_size += len(block)
                    if read_size > opened.st_size:
                        raise SystemExit(43)
                    digest.update(block)
                if (
                    read_size != opened.st_size
                    or _file_state(os.fstat(file_descriptor)) != _file_state(opened)
                ):
                    raise SystemExit(43)
            finally:
                os.close(file_descriptor)
            items.append(
                {
                    "path": relative,
                    "type": "file",
                    "size": metadata.st_size,
                    "sha256": "sha256:" + digest.hexdigest(),
                }
            )
        names_after = sorted(os.listdir(directory))
        after = os.fstat(directory)
        if names_after != names_before or _directory_state(after) != _directory_state(
            before
        ):
            raise SystemExit(42)

    try:
        visit(descriptor, "")
        if not items:
            raise SystemExit(41)
        return items
    finally:
        os.close(descriptor)


def _fallback_inventory(root: Path) -> list[dict[str, object]]:
    """Non-authoritative compatibility path for direct trusted API callers."""

    try:
        boundary_metadata = root.lstat()
    except OSError:
        raise SystemExit(40)
    if root.is_symlink() or not stat.S_ISDIR(boundary_metadata.st_mode):
        raise SystemExit(40)
    paths = sorted(root.rglob("*"), key=lambda item: item.as_posix())
    if not paths or len(paths) > MAXIMUM_PATHS:
        raise SystemExit(41)
    items: list[dict[str, object]] = []
    total = 0
    for path in paths:
        metadata = path.lstat()
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            raise SystemExit(42)
        if path.is_dir():
            items.append({"path": relative + "/", "type": "directory"})
            continue
        if not path.is_file() or metadata.st_nlink != 1:
            raise SystemExit(43)
        try:
            total = closed_runtime_total_bytes(total, metadata.st_size)
        except ValueError:
            raise SystemExit(44)
        digest = hashlib.sha256()
        read_size = 0
        with path.open("rb") as handle:
            while block := handle.read(
                min(1024 * 1024, metadata.st_size + 1 - read_size)
            ):
                read_size += len(block)
                if read_size > metadata.st_size:
                    raise SystemExit(43)
                digest.update(block)
        if read_size != metadata.st_size or _file_state(path.lstat()) != _file_state(
            metadata
        ):
            raise SystemExit(43)
        items.append(
            {
                "path": relative,
                "type": "file",
                "size": metadata.st_size,
                "sha256": "sha256:" + digest.hexdigest(),
            }
        )
    return items


def closed_runtime_cli_root(value: str) -> Path:
    """Map one canonical Guest authority path onto a fixed production root."""

    if not isinstance(value, str) or "\\" in value or "\x00" in value:
        raise ValueError("closed runtime inventory root is invalid")
    leaf = os.path.basename(value)
    if re.fullmatch(r"[0-9a-f]{64}", leaf) is None:
        raise ValueError("closed runtime inventory identity is invalid")
    for fixed_root in _FIXED_GUEST_AUTHORITY_ROOTS:
        if value != fixed_root.as_posix() + "/" + leaf:
            continue
        try:
            root_metadata = fixed_root.lstat()
            entries = tuple(fixed_root.iterdir())
        except OSError as error:
            raise ValueError(
                "closed runtime inventory fixed authority root is unavailable"
            ) from error
        if fixed_root.is_symlink() or not stat.S_ISDIR(root_metadata.st_mode):
            raise ValueError("closed runtime inventory fixed authority root is invalid")
        for entry in entries:
            if entry.name != leaf:
                continue
            try:
                metadata = entry.lstat()
            except OSError as error:
                raise ValueError(
                    "closed runtime inventory authority is unavailable"
                ) from error
            if entry.is_symlink() or not stat.S_ISDIR(metadata.st_mode):
                raise ValueError("closed runtime inventory authority is invalid")
            return entry
        raise ValueError("closed runtime inventory authority does not exist")
    raise ValueError("closed runtime inventory root is outside fixed authority roots")


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if len(arguments) != 1:
        return 64
    if not _descriptor_inventory_available():
        return 66
    try:
        root = closed_runtime_cli_root(arguments[0])
    except ValueError:
        return 65
    print(closed_runtime_inventory_digest(root))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
