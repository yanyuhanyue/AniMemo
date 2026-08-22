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
import stat
import sys
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO, Self

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


def _stat_identity(metadata: os.stat_result) -> tuple[int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_nlink,
    )


def _stat_content_state(metadata: os.stat_result) -> tuple[int, int, int]:
    return metadata.st_size, metadata.st_mtime_ns, metadata.st_ctime_ns


def _absolute_path(path: Path) -> Path:
    return Path(os.path.abspath(path.expanduser()))


def _descriptor_relative_io_available() -> bool:
    return (
        os.open in os.supports_dir_fd
        and os.stat in os.supports_dir_fd
        and os.unlink in os.supports_dir_fd
        and bool(getattr(os, "O_DIRECTORY", 0))
    )


class BoundEvidenceTree:
    """Bind a DR root and consume every descendant relative to its held handle."""

    def __init__(
        self,
        path: Path,
        *,
        label: str,
        create: bool = False,
        require_empty: bool = False,
    ) -> None:
        self.original = _absolute_path(path)
        self.label = label
        self.create = create
        self.require_empty = require_empty
        self.descriptor = -1

    def __enter__(self) -> Self:
        if not _descriptor_relative_io_available():
            raise BackupError("descriptor-relative DR I/O is required")
        self.descriptor = self._open_root()
        if self.require_empty and os.listdir(self.descriptor):
            self.close()
            raise BackupError(f"{self.label} must be empty: {self.original}")
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        try:
            if exc_type is None:
                self.assert_bound()
        finally:
            self.close()

    def _open_root(self) -> int:
        flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(self.original.anchor, flags)
        try:
            for component in self.original.parts[1:]:
                try:
                    child = os.open(component, flags, dir_fd=descriptor)
                except FileNotFoundError:
                    if not self.create:
                        raise BackupError(
                            f"{self.label} must be a directory: {self.original}"
                        ) from None
                    os.mkdir(component, 0o700, dir_fd=descriptor)
                    child = os.open(component, flags, dir_fd=descriptor)
                except OSError as error:
                    raise BackupError(
                        f"{self.label} must be a directory: {self.original}"
                    ) from error
                opened = os.fstat(child)
                if not stat.S_ISDIR(opened.st_mode):
                    os.close(child)
                    raise BackupError(
                        f"{self.label} must be a directory: {self.original}"
                    )
                os.close(descriptor)
                descriptor = child
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    def close(self) -> None:
        if self.descriptor >= 0:
            os.close(self.descriptor)
            self.descriptor = -1

    def assert_bound(self) -> None:
        try:
            current = self.original.lstat()
        except OSError as error:
            raise BackupError(f"{self.label} root changed during use") from error
        opened = os.fstat(self.descriptor)
        matches = (
            not stat.S_ISLNK(current.st_mode)
            and stat.S_ISDIR(current.st_mode)
            and _stat_identity(current) == _stat_identity(opened)
        )
        if not matches:
            raise BackupError(f"{self.label} root changed during use")

    def _open_directory(
        self,
        relative: tuple[str, ...],
        *,
        create: bool = False,
    ) -> int:
        descriptor = os.dup(self.descriptor)
        flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
        try:
            for component in relative:
                try:
                    child = os.open(component, flags, dir_fd=descriptor)
                except FileNotFoundError:
                    if not create:
                        raise
                    os.mkdir(component, 0o700, dir_fd=descriptor)
                    child = os.open(component, flags, dir_fd=descriptor)
                opened = os.fstat(child)
                if not stat.S_ISDIR(opened.st_mode):
                    os.close(child)
                    raise BackupError(f"{self.label} contains an unsafe directory")
                os.close(descriptor)
                descriptor = child
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    def make_directory(self, relative: tuple[str, ...]) -> None:
        self.assert_bound()
        parent = self._open_directory(relative[:-1], create=True)
        try:
            os.mkdir(relative[-1], 0o700, dir_fd=parent)
        except OSError as error:
            raise BackupError(f"destination directory cannot be created: {relative}") from error
        finally:
            os.close(parent)

    def open_new_file(self, relative: tuple[str, ...]) -> BinaryIO:
        self.assert_bound()
        parent = self._open_directory(relative[:-1], create=True)
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_BINARY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            descriptor = os.open(relative[-1], flags, 0o600, dir_fd=parent)
        except OSError as error:
            raise BackupError(f"destination file cannot be created: {relative}") from error
        finally:
            os.close(parent)
        return os.fdopen(descriptor, "wb", closefd=True)

    @contextmanager
    def open_regular_file(
        self,
        relative: tuple[str, ...],
    ) -> Iterator[BinaryIO]:
        parent = self._open_directory(relative[:-1])
        try:
            before = os.stat(
                relative[-1],
                dir_fd=parent,
                follow_symlinks=False,
            )
            descriptor = os.open(
                relative[-1],
                os.O_RDONLY
                | getattr(os, "O_BINARY", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=parent,
            )
        except OSError as error:
            raise BackupError(f"{self.label} file is unreadable") from error
        finally:
            os.close(parent)
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or _stat_identity(opened) != _stat_identity(before)
            or _stat_content_state(opened) != _stat_content_state(before)
        ):
            os.close(descriptor)
            raise BackupError(f"{self.label} file changed while opening")
        with os.fdopen(descriptor, "rb", closefd=True) as stream:
            yield stream
            after = os.fstat(stream.fileno())
        if (
            _stat_identity(after) != _stat_identity(opened)
            or _stat_content_state(after) != _stat_content_state(opened)
        ):
            raise BackupError(f"{self.label} file changed while reading")

    def read_regular_file(
        self,
        relative: tuple[str, ...],
        *,
        maximum: int,
    ) -> bytes:
        with self.open_regular_file(relative) as stream:
            opened = os.fstat(stream.fileno())
            value = stream.read(maximum + 1)
            after = os.fstat(stream.fileno())
        if (
            len(value) > maximum
            or len(value) != opened.st_size
            or _stat_identity(after) != _stat_identity(opened)
            or _stat_content_state(after) != _stat_content_state(opened)
        ):
            raise BackupError(f"{self.label} file changed while reading")
        return value

    def walk(
        self,
        relative: tuple[str, ...] = (),
    ) -> Iterator[tuple[tuple[str, ...], str, BinaryIO | None]]:
        root_descriptor = self._open_directory(relative)
        try:
            yield from self._walk_descriptor(root_descriptor, relative)
        finally:
            os.close(root_descriptor)

    def _walk_descriptor(
        self,
        descriptor: int,
        prefix: tuple[str, ...],
    ) -> Iterator[tuple[tuple[str, ...], str, BinaryIO | None]]:
        before_directory = os.fstat(descriptor)
        for name in sorted(os.listdir(descriptor)):
            relative = (*prefix, name)
            metadata = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            if stat.S_ISDIR(metadata.st_mode):
                flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
                child = os.open(name, flags, dir_fd=descriptor)
                opened = os.fstat(child)
                if (
                    _stat_identity(opened) != _stat_identity(metadata)
                    or _stat_content_state(opened) != _stat_content_state(metadata)
                ):
                    os.close(child)
                    raise BackupError(f"{self.label} directory changed while opening")
                yield relative, "directory", None
                try:
                    yield from self._walk_descriptor(child, relative)
                    after = os.fstat(child)
                finally:
                    os.close(child)
                if (
                    _stat_identity(after) != _stat_identity(opened)
                    or _stat_content_state(after) != _stat_content_state(opened)
                ):
                    raise BackupError(f"{self.label} directory changed while reading")
            elif stat.S_ISREG(metadata.st_mode) and metadata.st_nlink == 1:
                flags = (
                    os.O_RDONLY
                    | getattr(os, "O_BINARY", 0)
                    | getattr(os, "O_NOFOLLOW", 0)
                )
                child = os.open(name, flags, dir_fd=descriptor)
                opened = os.fstat(child)
                if (
                    _stat_identity(opened) != _stat_identity(metadata)
                    or _stat_content_state(opened) != _stat_content_state(metadata)
                ):
                    os.close(child)
                    raise BackupError(f"{self.label} file changed while opening")
                with os.fdopen(child, "rb", closefd=True) as stream:
                    yield relative, "file", stream
                    after = os.fstat(stream.fileno())
                if (
                    _stat_identity(after) != _stat_identity(opened)
                    or _stat_content_state(after) != _stat_content_state(opened)
                ):
                    raise BackupError(f"{self.label} file changed while reading")
            else:
                raise BackupError(f"{self.label} contains a link or special file")
        after_directory = os.fstat(descriptor)
        if (
            _stat_identity(after_directory) != _stat_identity(before_directory)
            or _stat_content_state(after_directory)
            != _stat_content_state(before_directory)
        ):
            raise BackupError(f"{self.label} directory changed while walking")


def _paths_overlap(first: Path, second: Path) -> bool:
    return first == second or first.is_relative_to(second) or second.is_relative_to(first)


def _copy_bound_file(
    source: BinaryIO,
    destination: BinaryIO,
) -> tuple[str, int]:
    opened = os.fstat(source.fileno())
    digest = hashlib.sha256()
    size = 0
    for chunk in iter(lambda: source.read(1024 * 1024), b""):
        destination.write(chunk)
        digest.update(chunk)
        size += len(chunk)
    destination.flush()
    os.fsync(destination.fileno())
    after = os.fstat(source.fileno())
    if (
        size != opened.st_size
        or _stat_identity(after) != _stat_identity(opened)
        or _stat_content_state(after) != _stat_content_state(opened)
    ):
        raise BackupError("source file changed while copying")
    return digest.hexdigest(), size


def _copy_bound_tree(
    source: BoundEvidenceTree,
    destination: BoundEvidenceTree,
    *,
    source_prefix: tuple[str, ...] = (),
    target_prefix: tuple[str, ...],
) -> int:
    source.assert_bound()
    destination.assert_bound()
    if target_prefix:
        destination.make_directory(target_prefix)
    count = 0
    for relative, kind, stream in source.walk(source_prefix):
        target = (*target_prefix, *relative[len(source_prefix) :])
        if kind == "directory":
            destination.make_directory(target)
        else:
            assert stream is not None
            with destination.open_new_file(target) as output:
                _copy_bound_file(stream, output)
            count += 1
    source.assert_bound()
    destination.assert_bound()
    return count


def _copy_tree(source: Path, destination: Path, *, label: str) -> int:
    with (
        BoundEvidenceTree(source, label=label) as bound_source,
        BoundEvidenceTree(
            destination,
            label="destination",
            create=True,
            require_empty=True,
        ) as bound_destination,
    ):
        return _copy_bound_tree(
            bound_source,
            bound_destination,
            target_prefix=(),
        )


def _write_manifest(
    root: BoundEvidenceTree,
    members: dict[str, dict[str, object]],
) -> None:
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
    root.assert_bound()
    with root.open_new_file((MANIFEST_NAME,)) as output:
        output.write(
            (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode(
                "utf-8"
            )
        )
        output.flush()
        os.fsync(output.fileno())


def create(args: argparse.Namespace) -> int:
    output_path = _absolute_path(Path(args.output))
    database_path = _absolute_path(Path(args.database_dump))
    source_paths: dict[str, Path] = {}
    for key in ("plugins", "media", "private", "updater_state"):
        source_arg = getattr(args, key)
        if not source_arg:
            raise BackupError(f"--{key.replace('_', '-')} is required")
        source_paths[key] = _absolute_path(Path(source_arg))
    for label, source in {
        "database dump": database_path,
        **source_paths,
    }.items():
        if _paths_overlap(output_path, source):
            raise BackupError(f"output must not overlap {label}: {source}")

    with ExitStack() as stack:
        database_parent = stack.enter_context(
            BoundEvidenceTree(database_path.parent, label="database dump parent")
        )
        sources = {
            key: stack.enter_context(BoundEvidenceTree(path, label=key))
            for key, path in source_paths.items()
        }
        database_parent.assert_bound()
        for source in sources.values():
            source.assert_bound()
        output = stack.enter_context(
            BoundEvidenceTree(
                output_path,
                label="output",
                create=True,
                require_empty=True,
            )
        )
        output.assert_bound()
        with (
            database_parent.open_regular_file((database_path.name,)) as source,
            output.open_new_file((MEMBERS["database"],)) as destination,
        ):
            database_digest, database_size = _copy_bound_file(source, destination)
        members: dict[str, dict[str, object]] = {
            "database": {
                "path": MEMBERS["database"],
                "sha256": database_digest,
                "size_bytes": database_size,
            }
        }
        for key in ("plugins", "media", "private", "updater_state"):
            count = _copy_bound_tree(
                sources[key],
                output,
                target_prefix=(MEMBERS[key],),
            )
            digest, _file_count = _tree_summary(output, (MEMBERS[key],))
            members[key] = {
                "path": MEMBERS[key],
                "file_count": count,
                "tree_sha256": digest,
            }
        _write_manifest(output, members)
        _verify_manifest_bound(output)
        output.assert_bound()
    print(
        json.dumps(
            {
                "status": "PASS",
                "backup_set": str(output_path),
                "members": members,
            },
            ensure_ascii=False,
        )
    )
    return 0


def _tree_summary(
    root: BoundEvidenceTree,
    relative: tuple[str, ...] = (),
) -> tuple[str, int]:
    digest = hashlib.sha256()
    file_count = 0
    for path, kind, stream in root.walk(relative):
        local = path[len(relative) :]
        encoded = Path(*local).as_posix().encode("utf-8")
        if kind == "directory":
            digest.update(b"D\0" + encoded + b"\0")
        else:
            assert stream is not None
            opened = os.fstat(stream.fileno())
            digest.update(
                b"F\0"
                + encoded
                + b"\0"
                + str(opened.st_size).encode()
                + b"\0"
            )
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
            file_count += 1
    return digest.hexdigest(), file_count


def _load_manifest_bound(root: BoundEvidenceTree) -> dict[str, object]:
    try:
        manifest = root.read_regular_file(
            (MANIFEST_NAME,),
            maximum=1024 * 1024,
        )
        payload = json.loads(manifest.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
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


def _verify_manifest_member(
    root: BoundEvidenceTree,
    key: str,
    expected: object,
    *,
    restored: bool = False,
) -> None:
    relative = expected.get("path") if isinstance(expected, dict) else None
    if (
        relative != MEMBERS[key]
        or Path(relative).is_absolute()
        or ".." in Path(relative).parts
    ):
        raise BackupError(f"invalid backup member path for {key}")
    member = (relative,)
    prefix = "restored " if restored else ""
    if key == "database":
        with root.open_regular_file(member) as stream:
            digest = hashlib.sha256()
            size = 0
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
                size += len(chunk)
        if size != expected.get("size_bytes") or digest.hexdigest() != expected.get(
            "sha256"
        ):
            raise BackupError(
                f"{prefix}database backup member failed integrity verification"
            )
        return
    try:
        digest, file_count = _tree_summary(root, member)
    except (BackupError, FileNotFoundError):
        digest = None
        file_count = None
    if (
        digest != expected.get("tree_sha256")
        or file_count != expected.get("file_count")
    ):
        raise BackupError(
            f"{prefix}backup member failed integrity verification: {key}"
        )


def _verify_manifest_bound(root: BoundEvidenceTree) -> dict[str, object]:
    payload = _load_manifest_bound(root)
    for key, expected in payload["members"].items():
        _verify_manifest_member(root, key, expected)
    return payload


def verify_manifest(root: Path) -> dict[str, object]:
    with BoundEvidenceTree(root, label="backup set") as bound:
        return _verify_manifest_bound(bound)


def restore(args: argparse.Namespace) -> int:
    source_path = _absolute_path(Path(args.backup_set))
    target_path = _absolute_path(Path(args.target_root))
    if _paths_overlap(source_path, target_path):
        raise BackupError("restore target must not overlap the backup set")
    with (
        BoundEvidenceTree(source_path, label="backup set") as source,
        BoundEvidenceTree(
            target_path,
            label="restore target",
            create=True,
            require_empty=True,
        ) as target,
    ):
        manifest = _verify_manifest_bound(source)
        source.assert_bound()
        target.assert_bound()
        for key in ("plugins", "media", "private", "updater_state"):
            _copy_bound_tree(
                source,
                target,
                source_prefix=(MEMBERS[key],),
                target_prefix=(MEMBERS[key],),
            )
        with (
            source.open_regular_file((MEMBERS["database"],)) as database,
            target.open_new_file((MEMBERS["database"],)) as output,
        ):
            _copy_bound_file(database, output)
        # Bind every copied byte to the frozen manifest, not to mutable paths.
        for key, expected in manifest["members"].items():
            _verify_manifest_member(target, key, expected, restored=True)
        source.assert_bound()
        target.assert_bound()
    print(
        json.dumps(
            {
                "status": "PASS",
                "restored_to": str(target_path),
                "requires_epoch_rotation": True,
            },
            ensure_ascii=False,
        )
    )
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
