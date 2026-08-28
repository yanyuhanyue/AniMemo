from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path, PurePosixPath


class PrivateStoreError(RuntimeError):
    """A stable failure from the protected local-file boundary."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _is_link(path: Path) -> bool:
    return path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction())


class AtomicPrivateFile:
    """Same-directory atomic private-file storage under one fixed root.

    This adapter deliberately exposes only bounded reads and atomic writes.  It
    never follows a link and never accepts a hard-linked target.  Linux adds
    directory fsync; other platforms are useful for contract tests but are not
    thereby qualified as an AniMemo server platform.
    """

    def __init__(
        self,
        root: Path,
        relative_path: str | PurePosixPath,
        *,
        create_parents: bool = False,
        directory_mode: int = 0o700,
    ) -> None:
        self.root = Path(os.path.abspath(root))
        relative = PurePosixPath(relative_path)
        if (
            relative.is_absolute()
            or not relative.parts
            or ".." in relative.parts
            or any(part in {"", "."} for part in relative.parts)
        ):
            raise PrivateStoreError("PRIVATE_PATH_INVALID")
        self.relative = relative
        self.path = self.root.joinpath(*relative.parts)
        self.create_parents = create_parents
        if directory_mode not in {0o700, 0o750}:
            raise PrivateStoreError("PRIVATE_DIRECTORY_MODE_INVALID")
        self.directory_mode = directory_mode

    def _validate_directory(self, path: Path, *, create: bool) -> None:
        if not path.exists():
            if not create:
                raise PrivateStoreError("PRIVATE_DIRECTORY_MISSING")
            try:
                path.mkdir(mode=self.directory_mode)
            except OSError as error:
                raise PrivateStoreError("PRIVATE_DIRECTORY_CREATE_FAILED") from error
        try:
            metadata = path.lstat()
        except OSError as error:
            raise PrivateStoreError("PRIVATE_DIRECTORY_UNAVAILABLE") from error
        if _is_link(path) or not stat.S_ISDIR(metadata.st_mode):
            raise PrivateStoreError("PRIVATE_DIRECTORY_INVALID")
        if os.name != "nt" and stat.S_IMODE(metadata.st_mode) != self.directory_mode:
            raise PrivateStoreError("PRIVATE_DIRECTORY_PERMISSIONS_INVALID")

    def _validate_tree(self) -> None:
        self._validate_directory(self.root, create=False)
        current = self.root
        for part in self.relative.parts[:-1]:
            current /= part
            self._validate_directory(current, create=self.create_parents)

    def _validate_target(self, *, required: bool) -> os.stat_result | None:
        try:
            metadata = self.path.lstat()
        except FileNotFoundError:
            if required:
                raise PrivateStoreError("PRIVATE_FILE_MISSING") from None
            return None
        except OSError as error:
            raise PrivateStoreError("PRIVATE_FILE_UNAVAILABLE") from error
        if (
            _is_link(self.path)
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
        ):
            raise PrivateStoreError("PRIVATE_FILE_INVALID")
        if os.name != "nt" and metadata.st_mode & 0o077:
            raise PrivateStoreError("PRIVATE_FILE_PERMISSIONS_INVALID")
        return metadata

    def exists(self) -> bool:
        self._validate_tree()
        return self._validate_target(required=False) is not None

    def read(self, *, limit: int) -> bytes:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise PrivateStoreError("PRIVATE_READ_LIMIT_INVALID")
        self._validate_tree()
        self._validate_target(required=True)
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(self.path, flags)
        except OSError as error:
            raise PrivateStoreError("PRIVATE_FILE_UNAVAILABLE") from error
        try:
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
                raise PrivateStoreError("PRIVATE_FILE_INVALID")
            with os.fdopen(descriptor, "rb") as handle:
                descriptor = -1
                payload = handle.read(limit + 1)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        if len(payload) > limit:
            raise PrivateStoreError("PRIVATE_FILE_TOO_LARGE")
        return payload

    def write(self, payload: bytes, *, must_not_exist: bool = False) -> None:
        if not isinstance(payload, bytes):
            raise PrivateStoreError("PRIVATE_PAYLOAD_INVALID")
        self._validate_tree()
        existing = self._validate_target(required=False)
        if must_not_exist and existing is not None:
            raise PrivateStoreError("PRIVATE_FILE_EXISTS")
        parent = self.path.parent
        try:
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{self.path.name}.", suffix=".tmp", dir=parent
            )
        except OSError as error:
            raise PrivateStoreError("PRIVATE_TEMP_CREATE_FAILED") from error
        temporary = Path(temporary_name)
        try:
            if _is_link(temporary):
                raise PrivateStoreError("PRIVATE_TEMP_INVALID")
            with os.fdopen(descriptor, "wb") as handle:
                descriptor = -1
                if os.name != "nt":
                    os.fchmod(handle.fileno(), 0o600)
                preserve_owner = getattr(os, "fchown", None)
                if existing is not None and callable(preserve_owner):
                    preserve_owner(
                        handle.fileno(),
                        existing.st_uid,
                        existing.st_gid,
                    )
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            self._validate_tree()
            if must_not_exist and self._validate_target(required=False) is not None:
                raise PrivateStoreError("PRIVATE_FILE_EXISTS")
            os.replace(temporary, self.path)
            if os.name != "nt":
                os.chmod(self.path, 0o600)
                directory_fd = os.open(parent, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            self._validate_target(required=True)
        except PrivateStoreError:
            raise
        except OSError as error:
            raise PrivateStoreError("PRIVATE_ATOMIC_WRITE_FAILED") from error
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


__all__ = ["AtomicPrivateFile", "PrivateStoreError"]
