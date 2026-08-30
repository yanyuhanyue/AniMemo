"""Build a closed Python import runtime directly from verified wheels."""

from __future__ import annotations

import argparse
import stat
import sys
from pathlib import Path

from installer.safe_archive import (
    WHEEL_RUNTIME_LIMITS,
    SafeArchiveError,
    extract_wheel_runtime,
)


class OfflineRuntimeError(RuntimeError):
    pass


def _reject() -> None:
    raise OfflineRuntimeError("OFFLINE_PYTHON_RUNTIME_INVALID")


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
        or len(wheels) > WHEEL_RUNTIME_LIMITS.max_archives
        or any(
            wheel.is_symlink() or not wheel.is_file() or wheel.suffix != ".whl"
            for wheel in wheels
        )
    ):
        _reject()

    try:
        extract_wheel_runtime(wheels, target)
    except SafeArchiveError as error:
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
