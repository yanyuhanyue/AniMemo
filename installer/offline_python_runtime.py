"""Build a closed Python import runtime directly from verified wheels."""

from __future__ import annotations

import argparse
import re
import shutil
import stat
import sys
import zipfile
from pathlib import Path, PurePosixPath


MAX_WHEEL_COUNT = 256
MAX_RUNTIME_FILES = 100_000
MAX_RUNTIME_BYTES = 2 * 1024 * 1024 * 1024


class OfflineRuntimeError(RuntimeError):
    pass


def _reject() -> None:
    raise OfflineRuntimeError("OFFLINE_PYTHON_RUNTIME_INVALID")


def _member_path(value: str) -> tuple[str, ...] | None:
    if (
        not value
        or "\\" in value
        or "\x00" in value
        or value.startswith("/")
        or re.match(r"^[A-Za-z]:", value) is not None
    ):
        _reject()
    raw_parts = value.rstrip("/").split("/")
    if not raw_parts or any(part in {"", ".", ".."} for part in raw_parts):
        _reject()
    parts = PurePosixPath(*raw_parts).parts
    if len(parts) >= 3 and parts[0].endswith(".data"):
        if parts[1] not in {"purelib", "platlib"}:
            return None
        parts = parts[2:]
    if not parts:
        _reject()
    return parts


def install_wheel_runtime(wheelhouse: Path, target: Path) -> None:
    wheelhouse = wheelhouse.absolute()
    target = target.absolute()
    try:
        wheelhouse_metadata = wheelhouse.lstat()
        wheels = sorted(wheelhouse.iterdir(), key=lambda item: item.name)
        target_parent = target.parent
        parent_metadata = target_parent.lstat()
    except OSError as error:
        raise OfflineRuntimeError("OFFLINE_PYTHON_RUNTIME_INVALID") from error
    if (
        wheelhouse.is_symlink()
        or not stat.S_ISDIR(wheelhouse_metadata.st_mode)
        or target.exists()
        or target.is_symlink()
        or target_parent.is_symlink()
        or not stat.S_ISDIR(parent_metadata.st_mode)
        or not wheels
        or len(wheels) > MAX_WHEEL_COUNT
        or any(
            wheel.is_symlink() or not wheel.is_file() or wheel.suffix != ".whl"
            for wheel in wheels
        )
    ):
        _reject()

    seen: set[str] = set()
    file_count = 0
    total_bytes = 0
    try:
        target.mkdir(mode=0o755)
        for wheel in wheels:
            with zipfile.ZipFile(wheel) as archive:
                for member in archive.infolist():
                    parts = _member_path(member.filename)
                    if parts is None:
                        continue
                    mode = member.external_attr >> 16
                    file_type = stat.S_IFMT(mode)
                    if member.is_dir():
                        if file_type not in {0, stat.S_IFDIR}:
                            _reject()
                        continue
                    if file_type not in {0, stat.S_IFREG}:
                        _reject()
                    folded = "/".join(parts).casefold()
                    file_count += 1
                    total_bytes += member.file_size
                    if (
                        folded in seen
                        or file_count > MAX_RUNTIME_FILES
                        or total_bytes > MAX_RUNTIME_BYTES
                    ):
                        _reject()
                    seen.add(folded)
                    destination = target.joinpath(*parts)
                    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
                    with archive.open(member) as source, destination.open("xb") as output:
                        shutil.copyfileobj(source, output)
                    if destination.stat().st_size != member.file_size:
                        _reject()
                    destination.chmod(0o644)
        if not seen:
            _reject()
        for directory in sorted(
            (item for item in target.rglob("*") if item.is_dir()),
            key=lambda item: len(item.parts),
            reverse=True,
        ):
            directory.chmod(0o755)
        target.chmod(0o755)
    except (OSError, RuntimeError, zipfile.BadZipFile) as error:
        shutil.rmtree(target, ignore_errors=True)
        if isinstance(error, OfflineRuntimeError):
            raise
        raise OfflineRuntimeError("OFFLINE_PYTHON_RUNTIME_INVALID") from error


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheelhouse", type=Path, required=True)
    parser.add_argument("--target", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        install_wheel_runtime(args.wheelhouse, args.target)
    except OfflineRuntimeError as error:
        print(str(error), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
