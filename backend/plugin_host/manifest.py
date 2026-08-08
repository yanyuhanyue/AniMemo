import json
import re
from pathlib import Path

from .hook_contract import SUPPORTED_HOOKS


SLUG_RE = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
PLUGIN_ID_RE = re.compile(r"^[a-z0-9]+(?:[.-][a-z0-9]+)+$")
SEMVER_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-[0-9A-Za-z.-]+)?$")
ROLES = {"reviewer", "user_manager", "operator", "administrator"}
EXTENSIONS = {
    "frontend.page", "frontend.navigation", "backend.api", "settings",
    "hooks", "storage", "catalog.importer", "catalog.metadata",
}
HOOKS = SUPPORTED_HOOKS
POLICY_KEYS = {"storesPersonalData", "usesExternalNetwork", "acceptsFileUploads", "retainsDataOnDisable"}


class ManifestError(ValueError):
    pass


def load_manifest(plugin_dir: str | Path) -> dict:
    directory = Path(plugin_dir)
    try:
        manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise ManifestError("缺少 manifest.json") from error
    except (OSError, json.JSONDecodeError) as error:
        raise ManifestError(f"manifest.json 无法读取：{error}") from error
    validate_manifest(manifest, directory_name=directory.name)
    return manifest


def validate_manifest(manifest: dict, *, directory_name: str | None = None) -> dict:
    if not isinstance(manifest, dict):
        raise ManifestError("Manifest 必须是对象")
    if manifest.get("schemaVersion") != 2 or manifest.get("sdkApi") != 2:
        raise ManifestError("仅支持 Manifest Schema v2 / SDK API v2")
    slug = manifest.get("slug", "")
    if not SLUG_RE.fullmatch(slug):
        raise ManifestError("slug 必须为 kebab-case")
    if directory_name and directory_name != slug:
        raise ManifestError("目录名必须与 slug 一致")
    for key in ("id", "name", "version", "description", "license"):
        if not isinstance(manifest.get(key), str) or not manifest[key].strip():
            raise ManifestError(f"{key} 不能为空")
    if not PLUGIN_ID_RE.fullmatch(manifest["id"]):
        raise ManifestError("id 必须使用反向域名或等价的稳定命名空间")
    author = manifest.get("author")
    if not isinstance(author, dict) or not isinstance(author.get("name"), str) or not author["name"].strip():
        raise ManifestError("author.name 不能为空")
    if manifest.get("installationMode") not in {"user", "system"}:
        raise ManifestError("installationMode 必须是 user 或 system")
    if not SEMVER_RE.fullmatch(manifest["version"]):
        raise ManifestError("version 必须符合 SemVer")
    runtimes = manifest.get("runtimes")
    if not isinstance(runtimes, list) or len(runtimes) != len(set(runtimes)) or not set(runtimes) <= {"frontend", "backend"}:
        raise ManifestError("runtimes 只能声明 frontend/backend")
    extensions = manifest.get("extensions")
    if not isinstance(extensions, list) or not set(extensions) <= EXTENSIONS:
        raise ManifestError("extensions 包含未知扩展")
    if "frontend" in runtimes:
        # Runtime packages expose one stable browser entry; the package inspector
        # checks that the file is present in the archive/directory.
        frontend_entry = manifest.get("frontendEntry", "frontend/plugin.js")
        if frontend_entry != "frontend/plugin.js":
            raise ManifestError("前端插件必须使用固定 frontend/plugin.js 入口")
        frontend_policy = manifest.get("frontend") or {}
        if not isinstance(frontend_policy, dict) or frontend_policy.get("exposure", "public") not in {"public", "authenticated", "staff"}:
            raise ManifestError("frontend.exposure 必须是 public、authenticated 或 staff")
    unsupported_backend_fields = {
        "backendPackage", "backendEntrypoint", "backendUrls", "djangoApp", "migrations",
    }
    if unsupported_backend_fields & set(manifest):
        raise ManifestError("Unsupported extended backend capability")
    if "background.task" in set(manifest.get("extensions") or []):
        raise ManifestError("Unsupported extended backend capability")
    backend = manifest.get("backend")
    if "backend" in runtimes:
        if not isinstance(backend, dict) or set(backend) != {"entry"}:
            raise ManifestError("backend runtime 必须只声明 entry")
        entry = backend.get("entry")
        if entry != "backend/plugin.py":
            raise ManifestError("backend.entry 必须使用固定 backend/plugin.py 入口")
        if "backend.api" not in set(extensions):
            raise ManifestError("backend runtime 必须声明 backend.api 扩展")
    elif backend is not None:
        raise ManifestError("未启用 backend runtime 时不能声明 backend")
    for permission in manifest.get("permissions", []):
        if not isinstance(permission, dict) or not str(permission.get("code", "")).startswith(f"{slug}."):
            raise ManifestError("权限代码必须使用插件命名空间")
        if not isinstance(permission.get("roles"), list) or not set(permission["roles"]) <= ROLES:
            raise ManifestError("权限 roles 只能使用 StaffProfile 角色")
    settings_definitions = manifest.get("settings", [])
    if not isinstance(settings_definitions, list):
        raise ManifestError("settings 必须是数组")
    setting_keys = set()
    for definition in settings_definitions:
        if not isinstance(definition, dict):
            raise ManifestError("settings definition 必须是对象")
        key = definition.get("key")
        if not isinstance(key, str) or not re.fullmatch(r"^[a-z][a-z0-9_]*$", key):
            raise ManifestError("settings.key 无效")
        if key in setting_keys:
            raise ManifestError("settings.key 不能重复")
        setting_keys.add(key)
        if definition.get("scope") not in {"user", "system"}:
            raise ManifestError("每个 settings definition 必须声明 scope=user 或 scope=system")
    hooks = manifest.get("hooks", [])
    if not isinstance(hooks, list) or not set(hooks) <= HOOKS:
        raise ManifestError("hooks 包含未知 Hook")
    policy = manifest.get("dataPolicy")
    if not isinstance(policy, dict) or set(POLICY_KEYS) - set(policy) or any(not isinstance(policy[k], bool) for k in POLICY_KEYS):
        raise ManifestError("dataPolicy 必须完整声明四项布尔策略")
    return manifest
