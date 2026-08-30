import hashlib
import json
import os
import shutil
import stat
import time
from io import BytesIO
from pathlib import Path, PurePosixPath
from uuid import uuid4
from zipfile import BadZipFile, ZipFile

from django.conf import settings
from packaging.version import InvalidVersion, Version

from installer.safe_archive import (
    SafeArchiveError,
    ZipExtractionLimits,
    archive_member_path,
    read_zip_member,
    validate_zip_archive,
)

from .filesystem_security import (
    PRIVATE_DIRECTORY_MODE,
    PRIVATE_FILE_MODE,
    RUNTIME_DIRECTORY_MODE,
    RUNTIME_FILE_MODE,
    PluginFilesystemSecurityError,
    close_and_unlink_created_file,
    created_file_identity,
    ensure_directory,
    ensure_plugin_layout,
    remove_secure_tree,
    require_created_file_identity,
    secure_file,
    secure_tree,
    validate_directory_chain,
    validate_secure_tree,
    write_descriptor_all,
    write_secure_bytes,
)
from .manifest import ManifestError, validate_manifest


class PluginPackageError(ValueError):
    pass


DEFAULT_MAX_PACKAGE_BYTES = 25 * 1024 * 1024
DEFAULT_MAX_UNCOMPRESSED_BYTES = 100 * 1024 * 1024
DEFAULT_MAX_FILES = 1000
DEFAULT_MAX_COMPRESSION_RATIO = 100
ALLOWED_ROOT_FILES = {"manifest.json", "package-index.json"}
ALLOWED_TOP_DIRS = {"frontend", "backend"}


class PackageHashLock:
    def __init__(self, root, sha256, timeout=10):
        self.root = Path(root)
        self.path = self.root / ".locks" / f"cas-{sha256}.lock"
        self.timeout = timeout
        self.fd = None
        self.token = f"{os.getpid()}:{uuid4().hex}"
        self.identity = None
        self.payload = f"{self.token}\n".encode("ascii")

    def __enter__(self):
        try:
            ensure_directory(
                self.root,
                self.path.parent,
                mode=PRIVATE_DIRECTORY_MODE,
            )
        except PluginFilesystemSecurityError as error:
            raise PluginPackageError("CAS 锁目录不安全。") from error
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
                flags |= getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
                self.fd = os.open(self.path, flags, PRIVATE_FILE_MODE)
            except FileExistsError:
                try:
                    secure_file(self.root, self.path, mode=PRIVATE_FILE_MODE)
                    stale = time.time() - self.path.stat().st_mtime > 300
                except FileNotFoundError:
                    continue
                except PluginFilesystemSecurityError as error:
                    raise PluginPackageError("CAS 锁文件不安全。") from error
                if stale:
                    self.path.unlink(missing_ok=True)
                    continue
                if time.monotonic() >= deadline:
                    raise PluginPackageError("同一 Package 正在执行另一项 CAS 操作。")
                time.sleep(0.05)
                continue
            except OSError as error:
                raise PluginPackageError("CAS 锁文件无法创建。") from error

            try:
                self.identity = created_file_identity(self.fd)
                if hasattr(os, "fchmod") and os.name != "nt":
                    os.fchmod(self.fd, PRIVATE_FILE_MODE)
                write_descriptor_all(self.fd, self.payload)
                os.fsync(self.fd)
                secure_file(self.root, self.path, mode=PRIVATE_FILE_MODE)
                require_created_file_identity(self.path, self.identity)
                return self
            except BaseException as error:
                close_and_unlink_created_file(
                    self.path,
                    self.fd,
                    self.identity,
                )
                self.fd = None
                self.identity = None
                if isinstance(error, (OSError, PluginFilesystemSecurityError)):
                    raise PluginPackageError("CAS 锁文件无法安全初始化。") from error
                raise

    def __exit__(self, exc_type, exc, traceback):
        if self.fd is None:
            return
        close_and_unlink_created_file(
            self.path,
            self.fd,
            self.identity,
            expected_payload=self.payload,
        )
        self.fd = None
        self.identity = None


def package_policy():
    configured = getattr(settings, "configured", False)
    return {
        "max_package_bytes": int(getattr(settings, "PLUGIN_MAX_PACKAGE_BYTES", DEFAULT_MAX_PACKAGE_BYTES)) if configured else DEFAULT_MAX_PACKAGE_BYTES,
        "max_uncompressed_bytes": int(getattr(settings, "PLUGIN_MAX_UNCOMPRESSED_BYTES", DEFAULT_MAX_UNCOMPRESSED_BYTES)) if configured else DEFAULT_MAX_UNCOMPRESSED_BYTES,
        "max_files": int(getattr(settings, "PLUGIN_MAX_FILES", DEFAULT_MAX_FILES)) if configured else DEFAULT_MAX_FILES,
        "max_compression_ratio": int(getattr(settings, "PLUGIN_MAX_COMPRESSION_RATIO", DEFAULT_MAX_COMPRESSION_RATIO)) if configured else DEFAULT_MAX_COMPRESSION_RATIO,
        "allowed_extension": ".ajplugin",
    }


def _archive_limits(policy):
    return ZipExtractionLimits(
        max_archives=1,
        max_members=policy["max_files"],
        max_member_bytes=policy["max_uncompressed_bytes"],
        max_total_bytes=policy["max_uncompressed_bytes"],
        max_compression_ratio=policy["max_compression_ratio"],
    )


def validated_package_members(archive, policy=None):
    policy = policy or package_policy()
    try:
        plans, budget = validate_zip_archive(
            archive,
            member_path=archive_member_path,
            limits=_archive_limits(policy),
        )
    except SafeArchiveError as error:
        raise PluginPackageError("插件包归档边界无效") from error
    if not plans:
        raise PluginPackageError("插件文件数量不合法")
    return tuple(
        (info, PurePosixPath(*parts)) for info, parts in plans
    ), budget


def read_validated_package_member(archive, info, budget):
    try:
        return read_zip_member(archive, info, budget)
    except SafeArchiveError as error:
        raise PluginPackageError("插件包归档内容无效") from error


def inspect_package(payload: bytes) -> dict:
    raw = payload.read() if hasattr(payload, "read") else bytes(payload)
    policy = package_policy()
    if len(raw) > policy["max_package_bytes"]:
        raise PluginPackageError("插件包超过大小限制")
    try:
        archive = ZipFile(BytesIO(raw))
    except BadZipFile as error:
        raise PluginPackageError("不是有效的 .ajplugin ZIP 容器") from error
    with archive:
        members, budget = validated_package_members(archive, policy)
        paths = [path for _, path in members]
        contents = {
            str(path): read_validated_package_member(archive, info, budget)
            for info, path in members
        }
        if PurePosixPath("manifest.json") not in paths:
            raise PluginPackageError("插件包根目录必须包含 manifest.json")
        try:
            manifest = json.loads(contents["manifest.json"].decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise PluginPackageError(f"manifest.json 无法解析: {error}") from error
        try:
            validate_manifest(manifest, directory_name=manifest.get("slug"))
        except ManifestError as error:
            raise PluginPackageError(str(error)) from error
        names = {str(path) for path in paths}
        for path in paths:
            if len(path.parts) == 1 and str(path) not in ALLOWED_ROOT_FILES:
                raise PluginPackageError(f"包内包含未声明的根文件：{path}")
            if len(path.parts) > 1 and path.parts[0] not in ALLOWED_TOP_DIRS:
                raise PluginPackageError(f"包内包含不允许的目录：{path}")
            if path.parts[0] == "frontend" and len(path.parts) > 1 and path.parts[1] != "assets" and path.name not in {"plugin.js", "plugin.css"}:
                raise PluginPackageError(f"前端发布包包含未允许的文件：{path}")
            if path.parts[0] == "backend" and (path.name == "pyproject.toml" or "tests" in path.parts or path.name in {"apps.py", "models.py", "admin.py", "urls.py"} or "migrations" in path.parts):
                raise PluginPackageError(f"后端发布包包含测试或开发文件：{path}")
        runtimes = set(manifest.get("runtimes") or [])
        if "frontend" in runtimes and "frontend/plugin.js" not in names:
            raise PluginPackageError("前端 runtime 缺少 frontend/plugin.js")
        if "backend" in runtimes and "backend/plugin.py" not in names:
            raise PluginPackageError("后端 runtime 缺少 backend/plugin.py")
        if PurePosixPath("package-index.json") not in paths:
            raise PluginPackageError("插件包必须包含 package-index.json")
        try:
            declared_index = json.loads(
                contents["package-index.json"].decode("utf-8")
            )
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise PluginPackageError(f"package-index.json 无法解析: {error}") from error
        if not isinstance(declared_index, dict) or declared_index.get("packageVersion") != 1:
            raise PluginPackageError("package-index.json 版本无效")
        if declared_index.get("pluginId") != manifest.get("id") or declared_index.get("slug") != manifest.get("slug") or declared_index.get("version") != manifest.get("version"):
            raise PluginPackageError("package-index.json 的插件元数据与 manifest 不一致")
        index = []
        for _, path in members:
            if path == PurePosixPath("package-index.json"):
                continue
            content = contents[str(path)]
            digest = hashlib.sha256(content).hexdigest()
            index.append({"path": str(path), "size": len(content), "sha256": digest})
        declared_files = declared_index.get("files")
        if not isinstance(declared_files, list):
            raise PluginPackageError("package-index.json.files 必须是数组")
        if any(
            not isinstance(item, dict)
            or not isinstance(item.get("path"), str)
            or isinstance(item.get("size"), bool)
            or not isinstance(item.get("size"), int)
            or not isinstance(item.get("sha256"), str)
            for item in declared_files
        ):
            raise PluginPackageError("package-index.json.files 包含无效条目")
        normalize = lambda item: (item["path"], item["size"], item["sha256"])
        if sorted(normalize(item) for item in declared_files) != sorted(normalize(item) for item in index):
            raise PluginPackageError("package-index.json 与包内文件的完整性索引不一致")
        return {"manifest": manifest, "files": index, "sha256": hashlib.sha256(raw).hexdigest()}


class LocalPluginPackageStorage:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.packages = self.root / "packages"
        self.runtime = self.root / "runtime"
        self.previews = self.root / "previews"
        self.staging = self.root / "staging"

    def ensure(self):
        try:
            ensure_plugin_layout(self)
        except PluginFilesystemSecurityError as error:
            raise PluginPackageError("插件存储布局不安全。") from error

    def package_path(self, sha256):
        digest = str(sha256 or "").lower()
        if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
            raise PluginPackageError("Package SHA-256 无效")
        return self.packages / "sha256" / digest[:2] / f"{digest}.ajplugin"

    def package_lock(self, sha256, *, timeout=10):
        digest = self.package_path(sha256).stem
        return PackageHashLock(self.root, digest, timeout=timeout)

    def _read_verified_cas_blob(self, sha256, *, inspect=True):
        source = self.package_path(sha256)
        maximum = package_policy()["max_package_bytes"]
        try:
            validate_directory_chain(self.root, source.parent)
            secure_file(self.root, source, mode=PRIVATE_FILE_MODE)
            before = source.lstat()
        except FileNotFoundError:
            raise
        except (OSError, PluginFilesystemSecurityError) as error:
            raise PluginPackageError("CAS Package 无法读取") from error
        if (
            source.is_symlink()
            or not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_size < 1
            or before.st_size > maximum
        ):
            raise PluginPackageError("CAS Package 不是受限单链接普通文件")
        flags = (
            os.O_RDONLY
            | getattr(os, "O_BINARY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        descriptor = -1
        try:
            descriptor = os.open(source, flags)
            opened = os.fstat(descriptor)
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_nlink != 1
                or (
                    opened.st_dev,
                    opened.st_ino,
                    opened.st_mode,
                    opened.st_nlink,
                    opened.st_size,
                )
                != (
                    before.st_dev,
                    before.st_ino,
                    before.st_mode,
                    before.st_nlink,
                    before.st_size,
                )
                or opened.st_mtime_ns != before.st_mtime_ns
            ):
                raise PluginPackageError("CAS Package 在打开期间发生变化")
            with os.fdopen(descriptor, "rb", closefd=True) as stream:
                descriptor = -1
                raw = stream.read(maximum + 1)
                after = os.fstat(stream.fileno())
            if (
                len(raw) != opened.st_size
                or (
                    after.st_dev,
                    after.st_ino,
                    after.st_mode,
                    after.st_nlink,
                    after.st_size,
                    after.st_mtime_ns,
                    after.st_ctime_ns,
                )
                != (
                    opened.st_dev,
                    opened.st_ino,
                    opened.st_mode,
                    opened.st_nlink,
                    opened.st_size,
                    opened.st_mtime_ns,
                    opened.st_ctime_ns,
                )
            ):
                raise PluginPackageError("CAS Package 在读取期间发生变化")
        except PluginPackageError:
            raise
        except OSError as error:
            raise PluginPackageError("CAS Package 无法读取") from error
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        if hashlib.sha256(raw).hexdigest() != source.stem:
            raise PluginPackageError("CAS Package SHA-256 校验失败")
        if inspect:
            inspect_package(raw)
        return raw

    def store_package(self, payload: bytes, *, sha256=None, minimum_free_bytes=0):
        self.ensure()
        raw = payload.read() if hasattr(payload, "read") else bytes(payload)
        digest = hashlib.sha256(raw).hexdigest()
        if sha256 and digest != sha256:
            raise PluginPackageError("Package SHA-256 校验失败")
        destination = self.package_path(digest)
        with self.package_lock(digest):
            try:
                ensure_directory(
                    self.root,
                    destination.parent,
                    mode=PRIVATE_DIRECTORY_MODE,
                )
            except PluginFilesystemSecurityError as error:
                raise PluginPackageError("CAS Package 目录不安全") from error
            if destination.is_file():
                existing = self._read_verified_cas_blob(digest, inspect=False)
                if len(existing) != len(raw) or hashlib.sha256(existing).hexdigest() != digest:
                    raise PluginPackageError("CAS 中存在损坏的同 SHA 文件")
                return destination
            free = shutil.disk_usage(self.root).free
            if free - len(raw) < int(minimum_free_bytes):
                raise PluginPackageError("插件存储空间不足，无法保存 Package。")
            temporary = destination.with_name(f".{digest}.{os.getpid()}.{uuid4().hex}.tmp")
            try:
                write_secure_bytes(
                    self.root,
                    temporary,
                    raw,
                    directory_mode=PRIVATE_DIRECTORY_MODE,
                    file_mode=PRIVATE_FILE_MODE,
                )
                os.replace(temporary, destination)
                secure_file(self.root, destination, mode=PRIVATE_FILE_MODE)
            finally:
                temporary.unlink(missing_ok=True)
        return destination

    @staticmethod
    def _version_key(value):
        try:
            return Version(str(value))
        except InvalidVersion as error:
            raise PluginPackageError(f"发现无效插件版本：{value}") from error

    def retain_versions(self, slug, *, current=None, previous="", keep=2):
        protected = [value for value in (current, previous) if value]
        runtime_directory = self.runtime / slug
        if runtime_directory.is_dir():
            try:
                secure_tree(
                    runtime_directory,
                    directory_mode=RUNTIME_DIRECTORY_MODE,
                    file_mode=RUNTIME_FILE_MODE,
                )
                validate_directory_chain(self.root, runtime_directory)
            except PluginFilesystemSecurityError as error:
                raise PluginPackageError("插件 runtime 目录不安全") from error
        if len(protected) < keep:
            runtime_versions = {
                path.name
                for path in runtime_directory.iterdir()
                if path.is_dir() and not path.name.startswith(".")
            } if runtime_directory.is_dir() else set()
            candidates = sorted(
                runtime_versions,
                key=self._version_key,
                reverse=True,
            )
            protected.extend(value for value in candidates if value not in protected)
        retained = set(protected[:keep])
        if runtime_directory.is_dir():
            for path in runtime_directory.iterdir():
                if path.is_dir() and not path.name.startswith(".") and path.name not in retained:
                    try:
                        remove_secure_tree(self.root, path)
                    except PluginFilesystemSecurityError as error:
                        raise PluginPackageError("插件 runtime 清理失败关闭") from error
        return sorted(retained, key=self._version_key, reverse=True)

    def list_versions(self, slug):
        directory = self.runtime / slug
        if not directory.is_dir():
            return []
        try:
            validate_directory_chain(self.root, directory)
            validate_secure_tree(directory)
        except PluginFilesystemSecurityError as error:
            raise PluginPackageError("插件 runtime 目录不安全") from error
        return sorted((path.name for path in directory.iterdir() if path.is_dir() and not path.name.startswith(".")), key=self._version_key, reverse=True)

    def rollback(self, slug, version, package_sha256):
        raw = self._read_verified_cas_blob(package_sha256)
        destination = self.runtime / slug / version
        staging = self.staging / f"rollback-{slug}-{version}"
        temporary = destination.with_name(f".{version}.rollback-{os.getpid()}")
        try:
            remove_secure_tree(self.root, staging)
            remove_secure_tree(self.root, temporary)
            ensure_directory(
                self.root,
                staging,
                mode=PRIVATE_DIRECTORY_MODE,
            )
        except PluginFilesystemSecurityError as error:
            raise PluginPackageError("插件 rollback staging 不安全") from error
        try:
            with ZipFile(BytesIO(raw)) as archive:
                members, budget = validated_package_members(
                    archive,
                    package_policy(),
                )
                for info, relative in members:
                    target = staging.joinpath(*relative.parts)
                    write_secure_bytes(
                        staging,
                        target,
                        read_validated_package_member(archive, info, budget),
                        directory_mode=RUNTIME_DIRECTORY_MODE,
                        file_mode=RUNTIME_FILE_MODE,
                    )
            secure_tree(
                staging,
                directory_mode=RUNTIME_DIRECTORY_MODE,
                file_mode=RUNTIME_FILE_MODE,
            )
            ensure_directory(
                self.root,
                destination.parent,
                mode=RUNTIME_DIRECTORY_MODE,
            )
            os.replace(staging, temporary)
            secure_tree(
                temporary,
                directory_mode=RUNTIME_DIRECTORY_MODE,
                file_mode=RUNTIME_FILE_MODE,
            )
            remove_secure_tree(self.root, destination)
            os.replace(temporary, destination)
            validate_secure_tree(destination)
            return destination
        except PluginFilesystemSecurityError as error:
            raise PluginPackageError("插件 rollback 文件系统边界失败关闭") from error
        finally:
            for cleanup in (staging, temporary):
                try:
                    remove_secure_tree(self.root, cleanup)
                except PluginFilesystemSecurityError:
                    pass

    def delete_plugin(self, slug):
        try:
            remove_secure_tree(self.root, self.runtime / slug)
            remove_secure_tree(self.root, self.previews / slug)
        except PluginFilesystemSecurityError as error:
            raise PluginPackageError("插件目录删除失败关闭") from error

    def cleanup_staging(self, *, older_than=None):
        if not self.staging.is_dir():
            return 0
        removed = 0
        for path in self.staging.iterdir():
            if path.name == "gc":
                continue
            if older_than is not None:
                try:
                    if path.stat().st_mtime > older_than:
                        continue
                except FileNotFoundError:
                    continue
            if path.is_dir():
                try:
                    secure_tree(
                        path,
                        directory_mode=PRIVATE_DIRECTORY_MODE,
                        file_mode=PRIVATE_FILE_MODE,
                    )
                    remove_secure_tree(self.root, path)
                except PluginFilesystemSecurityError as error:
                    raise PluginPackageError("插件 staging 清理失败关闭") from error
            else:
                try:
                    secure_file(self.root, path, mode=PRIVATE_FILE_MODE)
                    path.unlink(missing_ok=True)
                except PluginFilesystemSecurityError as error:
                    raise PluginPackageError("插件 staging 文件不安全") from error
            removed += 1
        return removed
