#!/usr/bin/env python3
"""Canonical path guards for the isolated disaster-recovery rehearsal."""

from __future__ import annotations

import argparse
import os
import stat
import sys
import tempfile
from pathlib import Path


class PathSafetyError(ValueError):
    pass


def _is_link_or_reparse(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return False
    attributes = getattr(metadata, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return path.is_symlink() or bool(attributes & reparse_flag)


def _absolute_lexical_path(raw: str | Path, *, label: str) -> Path:
    value = os.fspath(raw)
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise PathSafetyError(f"{label} must be an absolute path: {value}")
    if ".." in path.parts:
        raise PathSafetyError(f"{label} must not contain '..': {value}")
    normalized = Path(os.path.abspath(value))
    if os.path.normcase(os.fspath(path)) != os.path.normcase(os.fspath(normalized)):
        raise PathSafetyError(f"{label} must already be canonical: {value}")
    return normalized


def canonical_existing_directory(raw: str | Path, *, label: str) -> Path:
    path = _absolute_lexical_path(raw, label=label)
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        if _is_link_or_reparse(current):
            raise PathSafetyError(f"{label} must not contain a symlink or reparse point: {current}")
        if not current.exists() or not current.is_dir():
            raise PathSafetyError(f"{label} must be an existing directory: {current}")
    resolved = path.resolve(strict=True)
    if os.path.normcase(os.fspath(resolved)) != os.path.normcase(os.fspath(path)):
        raise PathSafetyError(f"{label} must resolve to itself: {path}")
    return resolved


def prepare_temp_root(parent_raw: str | Path, requested_raw: str | Path | None = None) -> Path:
    parent = canonical_existing_directory(parent_raw, label="temporary parent")
    if requested_raw is None:
        candidate = Path(tempfile.mkdtemp(prefix="animemo-dr.", dir=parent))
    else:
        candidate = _absolute_lexical_path(requested_raw, label="DR rehearsal temp root")
        if candidate == parent or candidate.parent != parent:
            raise PathSafetyError(
                f"DR rehearsal temp root must be a direct child of {parent}: {candidate}"
            )
        if os.path.lexists(candidate):
            raise PathSafetyError(f"DR rehearsal temp root already exists: {candidate}")
        candidate.mkdir(mode=0o700)

    canonical = canonical_existing_directory(candidate, label="DR rehearsal temp root")
    if canonical == parent or canonical.parent != parent:
        raise PathSafetyError(
            f"DR rehearsal temp root must remain a direct child of {parent}: {canonical}"
        )
    return canonical


def validate_delete_target(target_raw: str | Path, parent_raw: str | Path) -> Path:
    parent = canonical_existing_directory(parent_raw, label="destructive parent")
    target = canonical_existing_directory(target_raw, label="destructive target")
    if target == parent or target.parent != parent:
        raise PathSafetyError(
            f"destructive target must be a direct child of {parent}: {target}"
        )
    return target


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    canonical = subparsers.add_parser("canonical-directory")
    canonical.add_argument("--path", required=True)

    prepare = subparsers.add_parser("prepare-temp-root")
    prepare.add_argument("--parent", required=True)
    prepare.add_argument("--requested")

    validate = subparsers.add_parser("validate-delete")
    validate.add_argument("--parent", required=True)
    validate.add_argument("--target", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "canonical-directory":
            result = canonical_existing_directory(args.path, label="directory")
        elif args.command == "prepare-temp-root":
            result = prepare_temp_root(args.parent, args.requested)
        else:
            result = validate_delete_target(args.target, args.parent)
    except PathSafetyError as error:
        print(f"DR path safety error: {error}", file=sys.stderr)
        return 2
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
