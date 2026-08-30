from __future__ import annotations

import importlib.util
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from types import ModuleType
from uuid import uuid4

from django.conf import settings

from plugin_host.filesystem_security import (
    PluginFilesystemSecurityError,
    validate_directory_chain,
    validate_secure_tree,
)
from plugin_host.hooks import HookRegistry, hook_registry
from plugin_host.manifest import ManifestError, validate_manifest

from .context import PluginContext


class RuntimeLoadError(RuntimeError):
    pass


class RuntimeUnavailable(RuntimeLoadError):
    pass


@dataclass
class RuntimeCandidate:
    slug: str
    version: str
    root: Path
    manifest: dict
    context: PluginContext
    plugin: object | None
    module_namespace: str = ""
    active: bool = False

    def activate(self):
        if self.active:
            return
        self.context.activate()
        try:
            activate = getattr(self.plugin, "activate", None)
            if callable(activate):
                activate()
        except Exception:
            self.context.deactivate()
            raise
        self.active = True

    def deactivate(self):
        if not self.active:
            return
        try:
            deactivate = getattr(self.plugin, "deactivate", None)
            if callable(deactivate):
                deactivate()
        finally:
            self.context.deactivate()
            self.active = False

    def dispose(self):
        self.deactivate()
        if self.module_namespace:
            for name in tuple(sys.modules):
                if name == self.module_namespace or name.startswith(f"{self.module_namespace}."):
                    sys.modules.pop(name, None)


class RuntimeRegistry:
    """The only in-process registry for backend plugin runtimes."""

    def __init__(self):
        self._locks_guard = RLock()
        self._plugin_locks: dict[str, RLock] = {}
        self._active: dict[str, RuntimeCandidate] = {}
        self.hooks = HookRegistry()
        self.hooks.bind_runtime_registry(self)

    def lock_for(self, slug):
        key = str(slug or "")
        with self._locks_guard:
            return self._plugin_locks.setdefault(key, RLock())

    def plugin_lock(self, slug):
        return self.lock_for(slug)

    @staticmethod
    def runtime_path(slug, version):
        return Path(settings.PLUGIN_ROOT) / "runtime" / slug / version

    def load_candidate(self, root, *, expected_slug=None, expected_version=None):
        candidate_root = Path(root).absolute()
        try:
            candidate_root = validate_directory_chain(Path(settings.PLUGIN_ROOT), candidate_root)
            validate_secure_tree(candidate_root)
        except PluginFilesystemSecurityError as error:
            raise RuntimeLoadError(str(error)) from error
        # The complete chain has just been proven contained, non-link, owned,
        # and non-writable by other principals; resolving it again would reopen
        # the path authority after validation.
        directory = candidate_root  # lgtm[py/path-injection]
        try:
            manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
            validate_manifest(manifest)
        except (OSError, json.JSONDecodeError, ManifestError) as error:
            raise RuntimeLoadError(str(error)) from error
        slug = manifest["slug"]
        version = manifest["version"]
        if expected_slug and slug != expected_slug:
            raise RuntimeLoadError("Runtime slug 与安装记录不一致。")
        if expected_version and version != expected_version:
            raise RuntimeLoadError("Runtime version 与安装记录不一致。")

        context = PluginContext(slug=slug, version=version, root=directory, manifest=manifest, hook_registry=self.hooks)
        if "backend" not in set(manifest.get("runtimes") or []):
            return RuntimeCandidate(slug, version, directory, manifest, context, None)

        entry_value = str((manifest.get("backend") or {}).get("entry") or "")
        entry = (directory / entry_value).resolve()
        try:
            entry.relative_to(directory)
        except ValueError as error:
            raise RuntimeLoadError("Backend entry 越过插件目录边界。") from error
        if not entry.is_file() or entry.is_symlink():
            raise RuntimeLoadError("Backend runtime entry 不存在。")

        namespace = "_animemo_runtime_{}_{}_{}".format(
            re.sub(r"[^a-z0-9_]", "_", slug),
            re.sub(r"[^a-z0-9_]", "_", version.lower()),
            uuid4().hex,
        )
        package = ModuleType(namespace)
        package.__path__ = [str(entry.parent)]
        package.__package__ = namespace
        sys.modules[namespace] = package
        module_name = f"{namespace}.entry"
        try:
            spec = importlib.util.spec_from_file_location(module_name, entry)
            if spec is None or spec.loader is None:
                raise RuntimeLoadError("Backend runtime entry 无法创建导入规范。")
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            spec.loader.exec_module(module)
            factory = getattr(module, "create_plugin", None)
            if not callable(factory):
                raise RuntimeLoadError("Backend runtime 必须导出 create_plugin(host)。")
            plugin = factory(context)
            if plugin is None:
                raise RuntimeLoadError("Backend plugin factory 未返回 runtime object。")
            if not context.api and not context.integrations:
                raise RuntimeLoadError("Backend plugin 必须注册 API handler 或 Integration action。")
            health_check = getattr(plugin, "health_check", None)
            health = health_check() if callable(health_check) else True
            if health is False or (isinstance(health, dict) and health.get("status") not in {None, "ok", "healthy"}):
                raise RuntimeLoadError("Backend runtime health check 未通过。")
        except Exception as error:
            for name in tuple(sys.modules):
                if name == namespace or name.startswith(f"{namespace}."):
                    sys.modules.pop(name, None)
            if isinstance(error, RuntimeLoadError):
                raise
            raise RuntimeLoadError(f"Backend runtime 加载失败：{error}") from error
        return RuntimeCandidate(slug, version, directory, manifest, context, plugin, namespace)

    def load_installed_candidate(self, slug, version):
        return self.load_candidate(
            self.runtime_path(slug, version),
            expected_slug=slug,
            expected_version=version,
        )

    def active_candidate(self, slug):
        with self.lock_for(slug):
            return self._active.get(slug)

    def active_version(self, slug):
        candidate = self.active_candidate(slug)
        return candidate.version if candidate and candidate.active else None

    def is_active(self, slug, version):
        candidate = self.active_candidate(slug)
        return bool(candidate and candidate.active and candidate.version == version)

    def activate_candidate_locked(self, candidate):
        current = self._active.get(candidate.slug)
        if current is candidate:
            candidate.activate()
            return current
        candidate.activate()
        self._active[candidate.slug] = candidate
        if current is not None:
            current.deactivate()
        return current

    def restore_candidate_locked(self, slug, previous):
        current = self._active.pop(slug, None)
        if current is not None and current is not previous:
            current.dispose()
        if previous is not None:
            previous.activate()
            self._active[slug] = previous

    def finalize_previous(self, previous):
        if previous is not None:
            previous.dispose()

    def _unload_locked(self, slug):
        candidate = self._active.pop(slug, None)
        if candidate is not None:
            candidate.dispose()

    def ensure_current(self, slug):
        from plugin_host.models import PluginDeployment

        slug = str(slug or "")
        with self.lock_for(slug):
            deployment = PluginDeployment.objects.select_related("current_version").filter(plugin__slug=slug).first()
            if deployment is None or not deployment.enabled or not deployment.healthy or not deployment.current_version_id:
                self._unload_locked(slug)
                raise RuntimeUnavailable("插件不存在、已停用或当前不健康。")
            version = deployment.current_version.version
            current = self._active.get(slug)
            if current and current.version == version and current.active:
                return current
            try:
                candidate = self.load_installed_candidate(slug, version)
            except Exception:
                self._unload_locked(slug)
                raise
            previous = self.activate_candidate_locked(candidate)
            self.finalize_previous(previous)
        return candidate

    def reconcile_all(self):
        from plugin_host.models import PluginDeployment

        desired = set(
            PluginDeployment.objects.filter(enabled=True, healthy=True).values_list("plugin__slug", flat=True)
        )
        errors = []
        for slug in sorted(desired):
            try:
                self.ensure_current(slug)
            except RuntimeLoadError as error:
                errors.append((slug, error))
        for slug in tuple(self._active):
            if slug not in desired:
                self.unload(slug)
        return errors

    def assert_invariant(self, slug):
        from plugin_host.models import PluginDeployment

        candidate = self.ensure_current(slug)
        deployment = PluginDeployment.objects.select_related("current_version").get(plugin__slug=slug)
        if candidate.version != deployment.current_version.version:
            raise AssertionError("Plugin runtime version does not match PluginDeployment.current_version")
        return candidate.version

    def unload(self, slug):
        with self.lock_for(slug):
            self._unload_locked(slug)

    def clear(self):
        for slug in tuple(self._active):
            self.unload(slug)


runtime_registry = RuntimeRegistry()
runtime_registry.hooks = hook_registry
hook_registry.bind_runtime_registry(runtime_registry)
