import posixpath
import re
from pathlib import PurePosixPath


class MediaStorageError(RuntimeError):
    code = "MEDIA_STORAGE_ERROR"

    def __init__(self, detail=None):
        super().__init__(detail or self.code)
        self.detail = detail or self.code


class MediaStorageSetupRequired(MediaStorageError):
    code = "MEDIA_STORAGE_SETUP_REQUIRED"


class MediaStorageExhausted(MediaStorageError):
    code = "MEDIA_STORAGE_EXHAUSTED"


class MediaStorageOffline(MediaStorageError):
    code = "MEDIA_STORAGE_OFFLINE"


class UnsafeObjectKey(MediaStorageError):
    code = "UNSAFE_MEDIA_OBJECT_KEY"


SAFE_KEY_SEGMENT = re.compile(r"^[^\x00-\x1f\x7f]+$")


def safe_object_key(value):
    value = str(value or "").replace("\\", "/").lstrip("/")
    normalized = posixpath.normpath(value)
    path = PurePosixPath(normalized)
    if (
        not value
        or normalized in {"", "."}
        or normalized.startswith("../")
        or path.is_absolute()
        or ".." in path.parts
        or any(not SAFE_KEY_SEGMENT.fullmatch(part) for part in path.parts)
    ):
        raise UnsafeObjectKey("媒体对象路径不安全。")
    return normalized


def safe_error_summary(error, fallback="存储操作失败，请检查配置和服务状态。"):
    name = error.__class__.__name__
    allowed = {
        "EndpointConnectionError": "无法连接存储端点。",
        "ConnectTimeoutError": "连接存储端点超时。",
        "ReadTimeoutError": "存储端点响应超时。",
        "NoCredentialsError": "存储凭证未配置。",
        "PartialCredentialsError": "存储凭证不完整。",
        "PermissionError": "服务器无权访问本地存储目录。",
        "FileNotFoundError": "本地存储目录不存在。",
    }
    return allowed.get(name, fallback)
