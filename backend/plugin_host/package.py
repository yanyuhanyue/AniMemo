import hashlib
import json
import os
import shutil
import stat
from io import BytesIO
from pathlib import Path, PurePosixPath
from uuid import uuid4
from zipfile import BadZipFile, ZipFile

from django.conf import settings
from packaging.version import InvalidVersion, Version

from .manifest import ManifestError, validate_manifest


class PluginPackageError(ValueError):
    pass


DEFAULT_MAX_PACKAGE_BYTES = 25 * 1024 * 1024
DEFAULT_MAX_UNCOMPRESSED_BYTES = 100 * 1024 * 1024
DEFAULT_MAX_FILES = 1000
DEFAULT_MAX_COMPRESSION_RATIO = 100
ALLOWED_ROOT_FILES = {"manifest.json", "package-index.json"}
ALLOWED_TOP_DIRS = {"frontend", "backend"}


def package_policy():
    configured = getattr(settings, "configured", False)
    return {
        "max_package_bytes": int(getattr(settings, "PLUGIN_MAX_PACKAGE_BYTES", DEFAULT_MAX_PACKAGE_BYTES)) if configured else DEFAULT_MAX_PACKAGE_BYTES,
        "max_uncompressed_bytes": int(getattr(settings, "PLUGIN_MAX_UNCOMPRESSED_BYTES", DEFAULT_MAX_UNCOMPRESSED_BYTES)) if configured else DEFAULT_MAX_UNCOMPRESSED_BYTES,
        "max_files": int(getattr(settings, "PLUGIN_MAX_FILES", DEFAULT_MAX_FILES)) if configured else DEFAULT_MAX_FILES,
        "max_compression_ratio": int(getattr(settings, "PLUGIN_MAX_COMPRESSION_RATIO", DEFAULT_MAX_COMPRESSION_RATIO)) if configured else DEFAULT_MAX_COMPRESSION_RATIO,
        "allowed_extension": ".ajplugin",
    }


def _safe_member(name, info):
    if "\\" in name or "\x00" in name:
        raise PluginPackageError("包内路径不安全")
    path = PurePosixPath(name)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} or ":" in part for part in path.parts):
        raise PluginPackageError("包内路径包含绝对路径或父级跳转")
    mode = info.external_attr >> 16
    if mode and (stat.S_ISLNK(mode) or stat.S_ISDIR(mode)):
        raise PluginPackageError("包内禁止符号链接和异常目录条目")
    return path


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
        infos = [info for info in archive.infolist() if not info.is_dir()]
        if not infos or len(infos) > policy["max_files"]:
            raise PluginPackageError("插件文件数量不合法")
        paths = [_safe_member(info.filename, info) for info in infos]
        if len(set(paths)) != len(paths):
            raise PluginPackageError("插件包包含重复路径")
        folded_paths = [str(path).casefold() for path in paths]
        if len(set(folded_paths)) != len(folded_paths):
            raise PluginPackageError("插件包包含大小写冲突路径")
        if sum(info.file_size for info in infos) > policy["max_uncompressed_bytes"]:
            raise PluginPackageError("插件解压体积超过限制")
        for info in infos:
            if info.file_size and info.compress_size and info.file_size / info.compress_size > policy["max_compression_ratio"]:
                raise PluginPackageError("插件包压缩比异常，疑似解压炸弹")
        if PurePosixPath("manifest.json") not in paths:
            raise PluginPackageError("插件包根目录必须包含 manifest.json")
        try:
            manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
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
            declared_index = json.loads(archive.read("package-index.json").decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise PluginPackageError(f"package-index.json 无法解析: {error}") from error
        if not isinstance(declared_index, dict) or declared_index.get("packageVersion") != 1:
            raise PluginPackageError("package-index.json 版本无效")
        if declared_index.get("pluginId") != manifest.get("id") or declared_index.get("slug") != manifest.get("slug") or declared_index.get("version") != manifest.get("version"):
            raise PluginPackageError("package-index.json 的插件元数据与 manifest 不一致")
        index = []
        for info, path in zip(infos, paths):
            if path == PurePosixPath("package-index.json"):
                continue
            digest = hashlib.sha256(archive.read(info)).hexdigest()
            index.append({"path": str(path), "size": info.file_size, "sha256": digest})
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
        for path in (self.packages / "sha256", self.runtime, self.previews, self.staging, self.root / ".locks"):
            path.mkdir(parents=True, exist_ok=True)

    def package_path(self, sha256):
        digest = str(sha256 or "").lower()
        if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
            raise PluginPackageError("Package SHA-256 无效")
        return self.packages / "sha256" / digest[:2] / f"{digest}.ajplugin"

    def store_package(self, payload: bytes, *, sha256=None):
        self.ensure()
        raw = payload.read() if hasattr(payload, "read") else bytes(payload)
        digest = hashlib.sha256(raw).hexdigest()
        if sha256 and digest != sha256:
            raise PluginPackageError("Package SHA-256 校验失败")
        destination = self.package_path(digest)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.is_file():
            if destination.stat().st_size != len(raw) or hashlib.sha256(destination.read_bytes()).hexdigest() != digest:
                raise PluginPackageError("CAS 中存在损坏的同 SHA 文件")
            return destination
        temporary = destination.with_name(f".{digest}.{os.getpid()}.{uuid4().hex}.tmp")
        try:
            with temporary.open("xb") as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, destination)
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
        if len(protected) < keep:
            runtime_directory = self.runtime / slug
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
        runtime_directory = self.runtime / slug
        if runtime_directory.is_dir():
            for path in runtime_directory.iterdir():
                if path.is_dir() and not path.name.startswith(".") and path.name not in retained:
                    shutil.rmtree(path, ignore_errors=True)
        return sorted(retained, key=self._version_key, reverse=True)

    def list_versions(self, slug):
        directory = self.runtime / slug
        if not directory.is_dir():
            return []
        return sorted((path.name for path in directory.iterdir() if path.is_dir() and not path.name.startswith(".")), key=self._version_key, reverse=True)

    def rollback(self, slug, version, package_sha256):
        source = self.package_path(package_sha256)
        if not source.is_file():
            raise FileNotFoundError(source)
        inspect_package(source.read_bytes())
        destination = self.runtime / slug / version
        staging = self.staging / f"rollback-{slug}-{version}"
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True, exist_ok=False)
        try:
            with ZipFile(source) as archive:
                for info in archive.infolist():
                    if info.is_dir():
                        continue
                    relative = _safe_member(info.filename, info)
                    target = staging.joinpath(*relative.parts)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(archive.read(info))
            destination.parent.mkdir(parents=True, exist_ok=True)
            temporary = destination.with_name(f".{version}.rollback-{os.getpid()}")
            shutil.rmtree(temporary, ignore_errors=True)
            os.replace(staging, temporary)
            shutil.rmtree(destination, ignore_errors=True)
            os.replace(temporary, destination)
            return destination
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    def delete_plugin(self, slug):
        shutil.rmtree(self.runtime / slug, ignore_errors=True)
        shutil.rmtree(self.previews / slug, ignore_errors=True)

    def cleanup_staging(self):
        if not self.staging.is_dir():
            return 0
        removed = 0
        for path in self.staging.iterdir():
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
            else:
                path.unlink(missing_ok=True)
            removed += 1
        return removed
