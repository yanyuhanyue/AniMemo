"""Local development files with exact ownership of an attempted write.

Production uses StoragePoolStorage and its durable PostgreSQL receipts. These
local claims only compensate handled failures in the development process.
"""
import os

from django.core.files.storage import FileSystemStorage


class DevelopmentFileStorage(FileSystemStorage):
    def _save(self, name, content):
        from .storage import cleanup_local_upload, remember_local_upload

        if self._allow_overwrite:
            raise ValueError("Development uploads require exclusive file creation")
        while True:
            path = self.path(name)
            os.makedirs(os.path.dirname(path), mode=self.directory_permissions_mode or 0o777, exist_ok=True)
            try:
                descriptor = os.open(path, self.OS_OPEN_FLAGS, 0o666)
            except FileExistsError:
                name = self.get_available_name(name)
                continue
            break
        token = remember_local_upload(self, name, os.fstat(descriptor))
        try:
            chunks = iter(content.chunks())
            first = next(chunks, b"")
            with os.fdopen(descriptor, "wb" if isinstance(first, bytes) else "wt") as output:
                descriptor = None
                output.write(first)
                for chunk in chunks:
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            if self.file_permissions_mode is not None:
                os.chmod(path, self.file_permissions_mode)
            self._ensure_location_group_id(path)
        except BaseException:
            if descriptor is not None:
                os.close(descriptor)
            cleanup_local_upload(token)
            raise
        return name.replace("\\", "/")
