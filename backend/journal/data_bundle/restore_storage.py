"""Private, bounded files for one portable restore session.

Database reservations cover the configured raw, normalized and index budgets.
Actual bytes are measured independently; deletion failure never releases a
reservation. Callers hold the session row lock for every filesystem mutation.
"""
import hashlib
import os
import re
import stat
from pathlib import Path

from django.conf import settings
from plugin_host.filesystem_security import (
    PluginFilesystemSecurityError,
    ensure_directory,
    validate_directory_chain,
)


class RestoreStorageError(ValueError):
    pass


NORMALIZED_NAMES = frozenset({"normalizing.jsonl", "normalized.jsonl", "identities.sqlite3"})
CHUNK_NAME = re.compile(r"chunk-[0-9a-f]{16}$")


def _stat(path, *, directory=False):
    value = path.lstat()
    if stat.S_ISLNK(value.st_mode) or getattr(value, "st_file_attributes", 0) & 0x400:
        raise RestoreStorageError("staging_link_rejected")
    if directory:
        if not stat.S_ISDIR(value.st_mode):
            raise RestoreStorageError("staging_directory_required")
    elif not stat.S_ISREG(value.st_mode) or value.st_nlink != 1:
        raise RestoreStorageError("staging_regular_private_file_required")
    return value


def session_directory(session_id, *, create=False):
    root = Path(settings.BUNDLE_RESTORE_ROOT)
    if not root.is_absolute() or ".." in root.parts:
        raise RestoreStorageError("staging_root_invalid")
    for parent in reversed([root, *root.parents]):
        if parent.exists() or parent.is_symlink():
            _stat(parent, directory=True)
    if create:
        try:
            # Reuse the installed POSIX/Windows owner and DACL authority.
            ensure_directory(root, root)
        except PluginFilesystemSecurityError as error:
            raise RestoreStorageError("staging_root_permissions") from error
    if not root.exists():
        raise FileNotFoundError(root)
    root_stat = _stat(root, directory=True)
    if os.name == "posix" and (root_stat.st_mode & 0o077 or root_stat.st_uid != os.geteuid()):
        raise RestoreStorageError("staging_root_permissions")
    path = root / str(session_id)
    if create:
        try:
            ensure_directory(root, path)
        except PluginFilesystemSecurityError as error:
            raise RestoreStorageError("staging_session_permissions") from error
    value = _stat(path, directory=True)
    try:
        validate_directory_chain(root, path)
    except PluginFilesystemSecurityError as error:
        raise RestoreStorageError("staging_directory_permissions") from error
    if os.name == "posix" and (value.st_mode & 0o077 or value.st_uid != os.geteuid()):
        raise RestoreStorageError("staging_session_permissions")
    return path


def open_private(path, *, create=False):
    previous = None if create else _stat(path)
    flags = (os.O_WRONLY | os.O_CREAT | os.O_EXCL) if create else os.O_RDONLY
    descriptor = os.open(path, flags | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0), 0o600)
    try:
        current = os.fstat(descriptor)
        if not stat.S_ISREG(current.st_mode) or current.st_nlink != 1:
            raise RestoreStorageError("staging_file_identity")
        if previous is not None and (previous.st_dev, previous.st_ino) != (current.st_dev, current.st_ino):
            raise RestoreStorageError("staging_file_changed")
        if os.name == "posix" and (current.st_mode & 0o077 or current.st_uid != os.geteuid()):
            raise RestoreStorageError("staging_file_permissions")
        return os.fdopen(descriptor, "wb" if create else "rb")
    except BaseException:
        os.close(descriptor)
        raise


def file_digest(path):
    digest = hashlib.sha256()
    size = 0
    with open_private(path) as source:
        for data in iter(lambda: source.read(65536), b""):
            digest.update(data)
            size += len(data)
    return size, digest.hexdigest()


def chunk_path(directory, offset):
    return directory / f"chunk-{offset:016x}"


def persist_chunk(directory, offset, data):
    path = chunk_path(directory, offset)
    expected = (len(data), hashlib.sha256(data).hexdigest())
    try:
        with open_private(path, create=True) as target:
            target.write(data)
            target.flush()
            os.fsync(target.fileno())
    except FileExistsError:
        # A crash may have persisted the immutable file before its DB receipt.
        # Adopt only the identical retry; never overwrite unreceipted bytes.
        if file_digest(path) != expected:
            raise RestoreStorageError("chunk_file_conflict") from None
    return expected[1]


def inventory(directory, *, max_files):
    members = []
    with os.scandir(directory) as entries:
        for entry in entries:
            if len(members) >= max_files:
                raise RestoreStorageError("staging_inventory_budget")
            if entry.name not in NORMALIZED_NAMES and not CHUNK_NAME.fullmatch(entry.name):
                raise RestoreStorageError("staging_unknown_member")
            path = directory / entry.name
            members.append((path, _stat(path).st_size))
    return members


def physical_size(session):
    try:
        directory = session_directory(session.pk)
    except FileNotFoundError:
        return 0
    max_files = (session.expected_bytes + settings.BUNDLE_RESTORE_CHUNK_BYTES - 1) // settings.BUNDLE_RESTORE_CHUNK_BYTES + 3
    members = inventory(directory, max_files=max_files)
    size = sum(value for _, value in members)
    if size > session.reserved_bytes:
        raise RestoreStorageError("staging_reservation_mismatch")
    return size


def discard_normalized(session):
    directory = session_directory(session.pk, create=True)
    for name in NORMALIZED_NAMES:
        path = directory / name
        try:
            _stat(path)
        except FileNotFoundError:
            continue
        path.unlink()
    return directory


def cleanup_files(session, *, max_files):
    """Delete only recognized session members, at most max_files per call."""
    try:
        directory = session_directory(session.pk)
    except FileNotFoundError:
        return True
    limit = (session.expected_bytes + settings.BUNDLE_RESTORE_CHUNK_BYTES - 1) // settings.BUNDLE_RESTORE_CHUNK_BYTES + 3
    members = inventory(directory, max_files=limit)
    for path, _size in members[:max_files]:
        _stat(path)
        path.unlink()
    if len(members) > max_files:
        return False
    directory.rmdir()
    return True


class ChunkReader:
    """One bounded reader over immutable chunks, verifying every receipt."""
    def __init__(self, session):
        self.session = session
        self.directory = session_directory(session.pk)
        self.rows = iter(session.chunks.order_by("offset").iterator(chunk_size=50))
        self.file = None
        self.row = None
        self.chunk_hash = None
        self.chunk_read = 0
        self.total = 0
        self.digest = hashlib.sha256()
        self.finished = False

    def _next(self):
        if self.file is not None:
            self.file.close()
            self.file = None
            if self.chunk_read != self.row.size or self.chunk_hash.hexdigest() != self.row.sha256:
                raise RestoreStorageError("chunk_receipt_mismatch")
        self.row = next(self.rows, None)
        if self.row is None:
            if self.total != self.session.expected_bytes or self.digest.hexdigest() != self.session.sha256:
                raise RestoreStorageError("raw_digest_mismatch")
            self.finished = True
            return
        if self.row.offset != self.total:
            raise RestoreStorageError("chunk_missing")
        self.file = open_private(chunk_path(self.directory, self.row.offset))
        if os.fstat(self.file.fileno()).st_size != self.row.size:
            raise RestoreStorageError("chunk_size_mismatch")
        self.chunk_hash = hashlib.sha256()
        self.chunk_read = 0

    def read(self, size):
        if not 0 <= size <= 65536:
            raise RestoreStorageError("unbounded_read_rejected")
        output = bytearray()
        while len(output) < size and not self.finished:
            if self.file is None:
                self._next()
                if self.finished:
                    break
            data = self.file.read(size - len(output))
            if not data:
                self._next()
                continue
            self.total += len(data)
            self.chunk_read += len(data)
            if self.total > self.session.expected_bytes:
                raise RestoreStorageError("raw_size_mismatch")
            self.chunk_hash.update(data)
            self.digest.update(data)
            output.extend(data)
        return bytes(output)

    def close(self):
        if self.file is not None:
            self.file.close()
        close = getattr(self.rows, "close", None)
        if close:
            close()
