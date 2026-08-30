import json
import re
from pathlib import Path

from django.conf import settings
from django.utils import timezone

from .manifest import ManifestError, validate_manifest
from .public_diagnostics import (
    PLUGIN_RUNTIME_UNAVAILABLE,
    stable_registry_errors,
    stable_runtime_error,
)
from .runtime import RuntimeLoadError, runtime_registry

SLUG_PATTERN = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
SETTING_TYPES = {"text", "textarea", "boolean", "number", "select"}
PLUGIN_SDK_VERSION = "2.0.0"


class PluginRegistryError(ValueError):
    pass


def _safe_entry_path(plugin_directory, relative_path):
    if not relative_path:
        return None
    root = plugin_directory.resolve()
    candidate = (plugin_directory / relative_path).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    return candidate


def _validate_settings(definitions, errors):
    if not isinstance(definitions, list):
        errors.append("settings 必须是数组")
        return []
    normalized = []
    seen = set()
    for definition in definitions:
        if not isinstance(definition, dict):
            errors.append("settings 中存在无效配置项")
            continue
        key = definition.get("key", "")
        field_type = definition.get("type", "")
        if not re.fullmatch(r"[a-z][a-z0-9_]*", key) or key in seen:
            errors.append(f"配置键无效或重复：{key or '未命名'}")
            continue
        if field_type not in SETTING_TYPES or not definition.get("label"):
            errors.append(f"配置 {key} 定义无效")
            continue
        choices = definition.get("choices", [])
        default = definition.get("default")
        if field_type in {"text", "textarea", "select"} and not isinstance(default, str):
            errors.append(f"配置 {key} 的默认值必须是文字")
            continue
        if field_type == "boolean" and not isinstance(default, bool):
            errors.append(f"配置 {key} 的默认值必须是布尔值")
            continue
        if field_type == "number" and (isinstance(default, bool) or not isinstance(default, (int, float))):
            errors.append(f"配置 {key} 的默认值必须是数字")
            continue
        if field_type == "select":
            if not isinstance(choices, list):
                errors.append(f"配置 {key} 的 choices 必须是数组")
                continue
            values = [item.get("value") if isinstance(item, dict) else item for item in choices]
            if default not in values:
                errors.append(f"配置 {key} 的默认值不在 choices 中")
                continue
        seen.add(key)
        normalized.append({
            "key": key,
            "label": str(definition["label"]),
            "description": str(definition.get("description", "")),
            "type": field_type,
            "required": bool(definition.get("required", False)),
            "default": default,
            "min": definition.get("min"),
            "max": definition.get("max"),
            "choices": choices,
            "scope": definition.get("scope"),
        })
    return normalized


def read_runtime_manifest(plugin_directory):
    directory = Path(plugin_directory)
    try:
        manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
        validate_manifest(manifest)
    except FileNotFoundError:
        return {}, ["缺少 manifest.json"]
    except (OSError, json.JSONDecodeError, ManifestError):
        return {}, [PLUGIN_RUNTIME_UNAVAILABLE]
    errors = []
    settings_definitions = _validate_settings(manifest.get("settings", []), errors)
    runtimes = set(manifest.get("runtimes") or [])
    frontend_entry = _safe_entry_path(directory, "frontend/plugin.js")
    style_entry = _safe_entry_path(directory, "frontend/plugin.css")
    backend_entry_value = str((manifest.get("backend") or {}).get("entry") or "")
    backend_entry = _safe_entry_path(directory, backend_entry_value)
    frontend_ready = "frontend" not in runtimes or bool(frontend_entry and frontend_entry.is_file())
    backend_ready = "backend" not in runtimes or bool(backend_entry and backend_entry.is_file())
    if not frontend_ready:
        errors.append("缺少 frontend/plugin.js")
    if not backend_ready:
        errors.append("缺少 backend/plugin.py")
    return {
        **manifest,
        "settings": settings_definitions,
        "sdk_compatible": manifest.get("sdkApi") == 2,
        "compatible": not errors,
        "backend": {
            "enabled": "backend" in runtimes,
            "entry": backend_entry_value,
            "ready": backend_ready,
        },
        "frontend": {
            "enabled": "frontend" in runtimes,
            "entry": "frontend/plugin.js",
            "styleEntry": "frontend/plugin.css" if style_entry and style_entry.is_file() else "",
            "routePrefix": f"/plugins/{manifest['slug']}",
            "exposure": str((manifest.get("frontend") or {}).get("exposure") or "public"),
            "ready": frontend_ready,
        },
    }, errors


def _merged_config(definitions, stored_config):
    stored = stored_config if isinstance(stored_config, dict) else {}
    return {item["key"]: stored.get(item["key"], item.get("default")) for item in definitions}


def _serialize_plugin(directory, deployment):
    manifest, errors = read_runtime_manifest(directory)
    if not manifest:
        return _serialize_missing_plugin(deployment, errors)
    ready = not errors and manifest.get("compatible", False) and deployment.healthy
    active = False
    slug = deployment.plugin.slug
    version = deployment.current_version.version
    if deployment.enabled and ready:
        try:
            runtime_registry.ensure_current(slug)
            active = runtime_registry.is_active(slug, version)
        except RuntimeLoadError:
            errors.append(PLUGIN_RUNTIME_UNAVAILABLE)
            ready = False
    public_errors = stable_registry_errors(errors)
    enabled = bool(deployment.enabled)
    return {
        "id": manifest.get("id", ""),
        "slug": slug,
        "name": manifest.get("name", deployment.plugin.name),
        "version": version,
        "previous_version": deployment.previous_version.version if deployment.previous_version else "",
        "description": manifest.get("description", ""),
        "author": manifest.get("author") or {},
        "license": manifest.get("license", ""),
        "app_compatibility": manifest.get("appCompatibility") or {},
        "sdk_api": manifest.get("sdkApi"),
        "sdk_compatible": bool(manifest.get("sdk_compatible", True)),
        "compatible": bool(manifest.get("compatible", False)),
        "backend": manifest.get("backend") or {"enabled": False, "ready": True},
        "frontend": manifest.get("frontend") or {"enabled": False, "ready": True},
        "permissions": manifest.get("permissions") or [],
        "extensions": manifest.get("extensions") or [],
        "runtimes": manifest.get("runtimes") or [],
        "sdkApi": manifest.get("sdkApi"),
        "frontendEntry": "frontend/plugin.js",
        "styleEntry": (manifest.get("frontend") or {}).get("styleEntry", ""),
        "hooks": manifest.get("hooks") or [],
        "data_policy": manifest.get("dataPolicy") or {},
        "settings": manifest.get("settings") or [],
        "config": _merged_config(
            [item for item in (manifest.get("settings") or []) if item.get("scope") == "system"],
            deployment.system_config,
        ),
        "enabled": enabled,
        "healthy": deployment.healthy,
        "effective_enabled": enabled and ready and active,
        "status": "loaded" if enabled and ready and active else ("deployed" if ready else "failed"),
        "discovered": True,
        "installed": True,
        "loaded": active,
        "failed": bool(public_errors),
        "diagnostics": {
            "manifest": not bool(public_errors),
            "sdk_compatible": bool(manifest.get("sdk_compatible", True)),
            "frontend_entry": (manifest.get("frontend") or {}).get("ready", True),
            "backend_runtime": (manifest.get("backend") or {}).get("ready", True),
            "loaded": active,
            "last_error": public_errors[0] if public_errors else stable_runtime_error(deployment.last_error),
        },
        "ready": ready,
        "errors": public_errors,
        "updated_at": deployment.updated_at,
        "updated_by": deployment.updated_by.get_username() if deployment.updated_by else "",
        "discovered_at": timezone.now(),
        "manifest": manifest,
    }


def _serialize_missing_plugin(deployment, errors=None):
    errors = stable_registry_errors(errors or [PLUGIN_RUNTIME_UNAVAILABLE])
    return {
        "id": deployment.plugin.plugin_id,
        "slug": deployment.plugin.slug,
        "name": deployment.plugin.name,
        "version": deployment.current_version.version,
        "previous_version": deployment.previous_version.version if deployment.previous_version else "",
        "backend": {"enabled": False, "ready": False},
        "frontend": {"enabled": False, "ready": False},
        "permissions": [], "extensions": [], "runtimes": [], "hooks": [], "settings": [], "config": {},
        "data_policy": {}, "sdkApi": 2, "sdk_api": 2, "sdk_compatible": False, "compatible": False,
        "enabled": deployment.enabled, "healthy": False, "effective_enabled": False,
        "status": "failed", "discovered": False, "installed": True, "loaded": False, "failed": True,
        "ready": False, "errors": errors,
        "diagnostics": {"manifest": False, "loaded": False, "last_error": errors[0]},
        "updated_at": deployment.updated_at,
        "updated_by": deployment.updated_by.get_username() if deployment.updated_by else "",
        "discovered_at": timezone.now(),
        "manifest": {},
    }


def discover_plugins():
    from .models import PluginDeployment

    plugins = []
    deployments = PluginDeployment.objects.select_related("plugin", "current_version", "previous_version", "updated_by")
    for deployment in deployments:
        directory = Path(settings.PLUGIN_ROOT) / "runtime" / deployment.plugin.slug / deployment.current_version.version
        plugins.append(_serialize_plugin(directory, deployment) if directory.is_dir() else _serialize_missing_plugin(deployment))
    return plugins


def get_plugin(slug):
    if not SLUG_PATTERN.fullmatch(slug or ""):
        raise PluginRegistryError("插件标识无效。")
    from .models import PluginDeployment

    deployment = PluginDeployment.objects.select_related("plugin", "current_version", "previous_version", "updated_by").filter(plugin__slug=slug).first()
    if deployment is None:
        raise PluginRegistryError("插件尚未部署。")
    directory = Path(settings.PLUGIN_ROOT) / "runtime" / slug / deployment.current_version.version
    if not directory.is_dir():
        raise PluginRegistryError("当前版本 Runtime 目录不存在。")
    return _serialize_plugin(directory, deployment)


def validate_plugin_config(plugin, config):
    if not isinstance(config, dict):
        raise PluginRegistryError("插件配置必须是对象。")
    definitions = {item["key"]: item for item in plugin["settings"]}
    unknown = sorted(set(config) - set(definitions))
    if unknown:
        raise PluginRegistryError(f"包含未知配置：{', '.join(unknown)}")
    normalized = {}
    for key, definition in definitions.items():
        value = config.get(key, definition.get("default"))
        field_type = definition["type"]
        if definition["required"] and (value is None or value == ""):
            raise PluginRegistryError(f"{definition['label']}不能为空。")
        if field_type in {"text", "textarea", "select"} and value is not None and not isinstance(value, str):
            raise PluginRegistryError(f"{definition['label']}必须是文字。")
        if field_type == "boolean" and not isinstance(value, bool):
            raise PluginRegistryError(f"{definition['label']}必须是布尔值。")
        if field_type == "number" and (isinstance(value, bool) or not isinstance(value, (int, float))):
            raise PluginRegistryError(f"{definition['label']}必须是数字。")
        if field_type == "number" and definition.get("min") is not None and value < definition["min"]:
            raise PluginRegistryError(f"{definition['label']}不能小于 {definition['min']}。")
        if field_type == "number" and definition.get("max") is not None and value > definition["max"]:
            raise PluginRegistryError(f"{definition['label']}不能大于 {definition['max']}。")
        if field_type == "select":
            choices = [item.get("value") if isinstance(item, dict) else item for item in definition.get("choices", [])]
            if value not in choices:
                raise PluginRegistryError(f"{definition['label']}不是有效选项。")
        normalized[key] = value
    return normalized
