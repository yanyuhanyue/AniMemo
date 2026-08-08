import hashlib
import json
import os
import shutil
import stat
from io import BytesIO
from pathlib import Path, PurePosixPath
from zipfile import BadZipFile, ZipFile

from packaging.version import InvalidVersion, Version

from .manifest import ManifestError, validate_manifest


class PluginPackageError(ValueError):
    pass


MAX_PACKAGE_BYTES = 25 * 1024 * 1024
MAX_UNCOMPRESSED_BYTES = 100 * 1024 * 1024
MAX_FILES = 1000
MAX_COMPRESSION_RATIO = 100
ALLOWED_ROOT_FILES = {"manifest.json", "package-index.json"}
ALLOWED_TOP_DIRS = {"frontend", "backend"}


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
    if len(payload) > MAX_PACKAGE_BYTES:
        raise PluginPackageError("插件包超过大小限制")
    raw = payload.read() if hasattr(payload, "read") else bytes(payload)
    try:
        archive = ZipFile(BytesIO(raw))
    except BadZipFile as error:
        raise PluginPackageError("不是有效的 .ajplugin ZIP 容器") from error
    with archive:
        infos = [info for info in archive.infolist() if not info.is_dir()]
        if not infos or len(infos) > MAX_FILES:
            raise PluginPackageError("插件文件数量不合法")
        paths = [_safe_member(info.filename, info) for info in infos]
        if len(set(paths)) != len(paths):
            raise PluginPackageError("插件包包含重复路径")
        folded_paths = [str(path).casefold() for path in paths]
        if len(set(folded_paths)) != len(folded_paths):
            raise PluginPackageError("插件包包含大小写冲突路径")
        if sum(info.file_size for info in infos) > MAX_UNCOMPRESSED_BYTES:
            raise PluginPackageError("插件解压体积超过限制")
        for info in infos:
            if info.file_size and info.compress_size and info.file_size / info.compress_size > MAX_COMPRESSION_RATIO:
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
        if "backend" in runtimes and not any(name == "backend" or name.startswith("backend/") for name in names):
            raise PluginPackageError("后端 runtime 缺少 backend/ 目录")
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
        normalize = lambda item: (item.get("path"), item.get("size"), item.get("sha256")) if isinstance(item, dict) else None
        if sorted(normalize(item) for item in declared_files) != sorted(normalize(item) for item in index):
            raise PluginPackageError("package-index.json 与包内文件的完整性索引不一致")
        return {"manifest": manifest, "files": index, "sha256": hashlib.sha256(raw).hexdigest()}


class LocalPluginPackageStorage:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.packages = self.root / "packages"
        self.runtime = self.root / "runtime"
        self.staging = self.root / "staging"

    def ensure(self):
        for path in (self.packages, self.runtime, self.staging):
            path.mkdir(parents=True, exist_ok=True)

    def package_path(self, slug, version):
        return self.packages / slug / f"{version}.ajplugin"

    def store_package(self, slug, version, payload: bytes):
        self.ensure()
        destination = self.package_path(slug, version)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(".tmp")
        temporary.write_bytes(payload)
        os.replace(temporary, destination)
        return destination

    @staticmethod
    def _version_key(value):
        try:
            return Version(str(value))
        except InvalidVersion as error:
            raise PluginPackageError(f"发现无效插件版本：{value}") from error

    def retain_versions(self, slug, *, current=None, previous="", keep=2):
        if not current:
            current = self.list_versions(slug)[0] if self.list_versions(slug) else ""
        protected = [value for value in (current, previous) if value]
        if len(protected) < keep:
            runtime_directory = self.runtime / slug
            runtime_versions = {
                path.name
                for path in runtime_directory.iterdir()
                if path.is_dir() and not path.name.startswith(".")
            } if runtime_directory.is_dir() else set()
            candidates = sorted(
                set(self.list_versions(slug)) | runtime_versions,
                key=self._version_key,
                reverse=True,
            )
            protected.extend(value for value in candidates if value not in protected)
        retained = set(protected[:keep])
        for path in (self.packages / slug).glob("*.ajplugin"):
            if path.stem not in retained:
                path.unlink(missing_ok=True)
        runtime_directory = self.runtime / slug
        if runtime_directory.is_dir():
            for path in runtime_directory.iterdir():
                if path.is_dir() and not path.name.startswith(".") and path.name not in retained:
                    shutil.rmtree(path, ignore_errors=True)
        return sorted(retained, key=self._version_key, reverse=True)

    def list_versions(self, slug):
        directory = self.packages / slug
        if not directory.is_dir():
            return []
        return sorted((path.stem for path in directory.glob("*.ajplugin")), key=self._version_key, reverse=True)

    def rollback(self, slug, version):
        source = self.package_path(slug, version)
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
        shutil.rmtree(self.packages / slug, ignore_errors=True)
        shutil.rmtree(self.runtime / slug, ignore_errors=True)

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
