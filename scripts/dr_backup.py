#!/usr/bin/env python3
"""Create, verify, and restore an isolated AniMemo disaster-recovery set.

The database dump is supplied by the caller (normally ``pg_dump``).  Durable
filesystem roots are copied into a self-describing directory.  Secrets in
``.env.production`` are intentionally excluded; restore requires an explicit
operator-provided environment with the original ``CREDENTIAL_ENCRYPTION_KEY``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 1
MANIFEST_NAME = "dr-manifest.json"
MEMBERS = {
    "database": "database.sql.gz",
    "plugins": "plugins",
    "media": "media",
    "private": "private",
    "updater_state": "updater-state",
}
EXCLUSIONS = {
    "redis": "Redis is a rebuildable cache/queue and is not authoritative recovery state.",
    "logs": "Logs are operational evidence, not required application state.",
    "env": "Production environment files and secrets are never copied into a backup set.",
}


class BackupError(ValueError):
    pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_path(path: Path, *, label: str) -> Path:
    path = path.expanduser()
    if path.is_symlink():
        raise BackupError(f"{label} must not be a symlink: {path}")
    return path.resolve()


def _paths_overlap(first: Path, second: Path) -> bool:
    return first == second or first.is_relative_to(second) or second.is_relative_to(first)


def _copy_tree(source: Path, destination: Path, *, label: str) -> int:
    source = _safe_path(source, label=label)
    if not source.is_dir():
        raise BackupError(f"{label} must be a directory: {source}")
    count = 0
    destination.mkdir(parents=True, exist_ok=False)
    for item in sorted(source.rglob("*")):
        relative = item.relative_to(source)
        target = destination / relative
        if item.is_symlink():
            raise BackupError(f"{label} contains a symlink: {item}")
        if item.is_dir():
            target.mkdir()
            continue
        if not item.is_file():
            raise BackupError(f"{label} contains a non-regular file: {item}")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(item, target)
        count += 1
    return count


def _assert_empty_directory(path: Path, *, label: str) -> Path:
    path = path.expanduser()
    if path.exists() and (path.is_symlink() or not path.is_dir()):
        raise BackupError(f"{label} must be an empty directory: {path}")
    if path.exists() and any(path.iterdir()):
        raise BackupError(f"{label} must be empty: {path}")
    path.mkdir(parents=True, exist_ok=True)
    return path.resolve()


def _write_manifest(root: Path, members: dict[str, dict[str, object]]) -> None:
    payload = {
        "schema": SCHEMA_VERSION,
        "format": "animemo-disaster-recovery-set",
        "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "members": members,
        "exclusions": EXCLUSIONS,
        "restore_requirements": [
            "Fresh target database and filesystem roots.",
            "Original CREDENTIAL_ENCRYPTION_KEY, or an intentional credential re-encryption plan.",
            "Run rotate_authentication_epoch --confirm-restore before serving the restored API.",
        ],
    }
    path = root / MANIFEST_NAME
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)


def create(args: argparse.Namespace) -> int:
    raw_output = Path(args.output).expanduser()
    if raw_output.is_symlink():
        raise BackupError(f"output must not be a symlink: {raw_output}")
    output = raw_output.resolve()
    database = _safe_path(Path(args.database_dump), label="database dump")
    if not database.is_file():
        raise BackupError(f"database dump must be a regular file: {database}")
    sources: dict[str, Path] = {}
    for key in ("plugins", "media", "private", "updater_state"):
        source_arg = getattr(args, key)
        if not source_arg:
            raise BackupError(f"--{key.replace('_', '-')} is required")
        source = _safe_path(Path(source_arg), label=key)
        if not source.is_dir():
            raise BackupError(f"{key} must be a directory: {source}")
        sources[key] = source
    for label, source in {"database dump": database, **sources}.items():
        if _paths_overlap(output, source):
            raise BackupError(f"output must not overlap {label}: {source}")
    output = _assert_empty_directory(raw_output, label="output")
    shutil.copy2(database, output / MEMBERS["database"])
    members: dict[str, dict[str, object]] = {
        "database": {
            "path": MEMBERS["database"],
            "sha256": _sha256(output / MEMBERS["database"]),
            "size_bytes": (output / MEMBERS["database"]).stat().st_size,
        }
    }
    for key in ("plugins", "media", "private", "updater_state"):
        target = output / MEMBERS[key]
        count = _copy_tree(sources[key], target, label=key)
        members[key] = {
            "path": MEMBERS[key],
            "file_count": count,
            "tree_sha256": tree_digest(target),
        }
    _write_manifest(output, members)
    verify_manifest(output)
    print(json.dumps({"status": "PASS", "backup_set": str(output), "members": members}, ensure_ascii=False))
    return 0


def tree_digest(root: Path) -> str:
    root = _safe_path(root, label="backup member")
    if not root.is_dir():
        raise BackupError(f"tree member is not a directory: {root}")
    digest = hashlib.sha256()
    for item in sorted(root.rglob("*")):
        relative = item.relative_to(root).as_posix().encode("utf-8")
        if item.is_symlink():
            raise BackupError(f"tree contains a symlink: {item}")
        if item.is_dir():
            digest.update(b"D\0" + relative + b"\0")
        elif item.is_file():
            digest.update(b"F\0" + relative + b"\0" + str(item.stat().st_size).encode() + b"\0")
            with item.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
        else:
            raise BackupError(f"tree contains a non-regular file: {item}")
    return digest.hexdigest()


def _load_manifest(root: Path) -> dict[str, object]:
    root = _safe_path(root, label="backup set")
    manifest_path = root / MANIFEST_NAME
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise BackupError("backup manifest is not a regular file")
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise BackupError("backup manifest is unreadable") from error
    if (
        not isinstance(payload, dict)
        or payload.get("schema") != SCHEMA_VERSION
        or payload.get("format") != "animemo-disaster-recovery-set"
        or not isinstance(payload.get("members"), dict)
        or set(payload["members"]) != set(MEMBERS)
        or payload.get("exclusions") != EXCLUSIONS
        or not isinstance(payload.get("restore_requirements"), list)
        or not any(
            "rotate_authentication_epoch --confirm-restore" in str(requirement)
            for requirement in payload["restore_requirements"]
        )
    ):
        raise BackupError("backup manifest schema is invalid")
    return payload


def verify_manifest(root: Path) -> None:
    payload = _load_manifest(root)
    for key, expected in payload["members"].items():
        relative = expected.get("path") if isinstance(expected, dict) else None
        if relative != MEMBERS[key] or Path(relative).is_absolute() or ".." in Path(relative).parts:
            raise BackupError(f"invalid backup member path for {key}")
        member = root / relative
        if key == "database":
            if (
                not member.is_file()
                or member.is_symlink()
                or member.stat().st_size != expected.get("size_bytes")
                or _sha256(member) != expected.get("sha256")
            ):
                raise BackupError("database backup member failed integrity verification")
        else:
            digest = tree_digest(member) if member.is_dir() else None
            file_count = sum(1 for item in member.rglob("*") if item.is_file()) if member.is_dir() else None
            if digest != expected.get("tree_sha256") or file_count != expected.get("file_count"):
                raise BackupError(f"backup member failed integrity verification: {key}")


def restore(args: argparse.Namespace) -> int:
    source = _safe_path(Path(args.backup_set), label="backup set")
    verify_manifest(source)
    raw_target = Path(args.target_root).expanduser()
    if raw_target.is_symlink():
        raise BackupError(f"restore target must not be a symlink: {raw_target}")
    target = raw_target.resolve()
    if _paths_overlap(source, target):
        raise BackupError("restore target must not overlap the backup set")
    target = _assert_empty_directory(raw_target, label="restore target")
    for key in ("plugins", "media", "private", "updater_state"):
        _copy_tree(source / MEMBERS[key], target / MEMBERS[key], label=f"restore {key}")
    shutil.copy2(source / MEMBERS["database"], target / MEMBERS["database"])
    # Verify the copied file as well as the source tree before reporting success.
    if _sha256(target / MEMBERS["database"]) != _sha256(source / MEMBERS["database"]):
        raise BackupError("restored database backup member failed integrity verification")
    print(json.dumps({"status": "PASS", "restored_to": str(target), "requires_epoch_rotation": True}, ensure_ascii=False))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    create_parser = subparsers.add_parser("create")
    create_parser.add_argument("--output", required=True)
    create_parser.add_argument("--database-dump", required=True)
    create_parser.add_argument("--plugins", required=True)
    create_parser.add_argument("--media", required=True)
    create_parser.add_argument("--private", required=True)
    create_parser.add_argument("--updater-state", required=True)
    create_parser.set_defaults(handler=create)
    verify_parser = subparsers.add_parser("verify")
    verify_parser.add_argument("backup_set")
    verify_parser.set_defaults(handler=lambda args: (verify_manifest(Path(args.backup_set)), print("PASS"))[1] or 0)
    restore_parser = subparsers.add_parser("restore")
    restore_parser.add_argument("backup_set")
    restore_parser.add_argument("--target-root", required=True)
    restore_parser.set_defaults(handler=restore)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.handler(args) or 0)
    except BackupError as error:
        print(f"DR backup error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
