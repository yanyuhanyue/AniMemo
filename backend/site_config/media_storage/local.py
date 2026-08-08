import io
import os
import shutil
import tempfile
import uuid
from pathlib import Path
from pathlib import PurePosixPath

from django.conf import settings

from .common import MediaStorageOffline, UnsafeObjectKey, safe_error_summary, safe_object_key


PUBLIC_DIRECTORY_MODE = 0o755
PUBLIC_FILE_MODE = 0o644


def validate_storage_subpath(value):
    """Validate the admin-facing subdirectory below MEDIA_LOCAL_STORAGE_ROOT."""
    value = str(value or "").strip()
    if not value:
        return ""
    path = PurePosixPath(value)
    if (
        "\\" in value
        or value.startswith("/")
        or value.endswith("/")
        or "//" in value
        or (len(value) >= 2 and value[1] == ":")
        or path.is_absolute()
        or any(part in {".", ".."} for part in path.parts)
    ):
        raise UnsafeObjectKey("Local 存储子路径必须位于固定根目录内。")
    if any(not part or any(ord(char) < 32 for char in part) for part in path.parts):
        raise UnsafeObjectKey("Local 存储子路径包含非法字符。")
    return "/".join(path.parts)


def approved_local_root(backend):
    approved = Path(settings.MEDIA_LOCAL_STORAGE_ROOT).resolve()
    configured = validate_storage_subpath(backend.local_root)
    candidate = (approved / Path(*configured.split("/"))).resolve() if configured else approved
    try:
        candidate.relative_to(approved)
    except ValueError as error:
        raise UnsafeObjectKey("本地存储目录不在 MEDIA_LOCAL_STORAGE_ROOT 内。") from error
    return candidate


class DynamicLocalBackend:
    def __init__(self, backend):
        self.backend = backend

    def root(self, *, create=False):
        root = approved_local_root(self.backend)
        if create:
            try:
                root.mkdir(mode=PUBLIC_DIRECTORY_MODE, parents=True, exist_ok=True)
                os.chmod(root, PUBLIC_DIRECTORY_MODE)
            except OSError as error:
                raise MediaStorageOffline(safe_error_summary(error)) from error
        return root

    @staticmethod
    def ensure_public_directories(root, directory):
        try:
            relative = directory.relative_to(root)
        except ValueError as error:
            raise UnsafeObjectKey("本地媒体对象路径越界。") from error
        current = root
        os.chmod(current, PUBLIC_DIRECTORY_MODE)
        for part in relative.parts:
            current = current / part
            current.mkdir(mode=PUBLIC_DIRECTORY_MODE, exist_ok=True)
            os.chmod(current, PUBLIC_DIRECTORY_MODE)

    def path_for(self, key, *, create_parent=False):
        root = self.root(create=create_parent)
        target = (root / Path(*safe_object_key(key).split("/"))).resolve()
        try:
            target.relative_to(root)
        except ValueError as error:
            raise UnsafeObjectKey("本地媒体对象路径越界。") from error
        if create_parent:
            try:
                self.ensure_public_directories(root, target.parent)
            except OSError as error:
                raise MediaStorageOffline(safe_error_summary(error)) from error
        return target

    def write(self, key, content, *, content_type="application/octet-stream"):
        temporary = None
        target = None
        replaced = False
        try:
            target = self.path_for(key, create_parent=True)
            descriptor, temporary = tempfile.mkstemp(prefix=".upload-", dir=target.parent)
            with os.fdopen(descriptor, "wb") as output:
                if hasattr(os, "fchmod"):
                    os.fchmod(output.fileno(), PUBLIC_FILE_MODE)
                output.write(content)
                output.flush()
                os.fsync(output.fileno())
            os.chmod(temporary, PUBLIC_FILE_MODE)
            os.replace(temporary, target)
            temporary = None
            replaced = True
            os.chmod(target, PUBLIC_FILE_MODE)
        except OSError as error:
            cleanup_path = target if replaced else temporary
            if cleanup_path:
                try:
                    os.unlink(cleanup_path)
                except OSError:
                    pass
            raise MediaStorageOffline(safe_error_summary(error)) from error

    def open(self, key):
        try:
            return io.BytesIO(self.path_for(key).read_bytes())
        except OSError as error:
            raise MediaStorageOffline(safe_error_summary(error)) from error

    def exists(self, key):
        try:
            return self.path_for(key).is_file()
        except OSError as error:
            raise MediaStorageOffline(safe_error_summary(error)) from error

    def delete(self, key):
        try:
            self.path_for(key).unlink()
        except FileNotFoundError:
            pass
        except OSError as error:
            raise MediaStorageOffline(safe_error_summary(error)) from error

    def url(self, key):
        parts = [self.backend.local_public_base_url.rstrip("/")]
        subpath = validate_storage_subpath(self.backend.local_root)
        if subpath:
            parts.append(subpath)
        parts.append(safe_object_key(key))
        return "/".join(parts)

    def disk_usage(self):
        root = self.root(create=True)
        try:
            return shutil.disk_usage(root)
        except OSError as error:
            raise MediaStorageOffline(safe_error_summary(error)) from error

    def test_connection(self):
        key = f"site/healthchecks/{uuid.uuid4().hex}"
        try:
            self.write(key, b"anime-journal-local-healthcheck", content_type="text/plain")
            if self.open(key).read() != b"anime-journal-local-healthcheck":
                raise MediaStorageOffline("本地存储读写校验失败。")
            self.disk_usage()
            return "Local media read/write connection OK"
        finally:
            try:
                self.delete(key)
            except MediaStorageOffline:
                pass
