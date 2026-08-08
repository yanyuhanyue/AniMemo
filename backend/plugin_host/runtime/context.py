from __future__ import annotations

from dataclasses import dataclass, field
from uuid import uuid4

import requests

from plugin_host.hooks import KNOWN_HOOKS, hook_failure_mode
from plugin_host.storage import PluginStorage

from .routes import PluginApi


class ExternalNetworkDenied(PermissionError):
    pass


@dataclass
class PluginContext:
    slug: str
    version: str
    root: object
    manifest: dict
    hook_registry: object
    runtime_id: str = field(default_factory=lambda: uuid4().hex)
    _hooks: list[tuple] = field(default_factory=list)
    _hook_disposers: list[object] = field(default_factory=list)

    def __post_init__(self):
        self.api = PluginApi(self.slug, self.manifest)

    @property
    def hook_owner(self):
        return self.slug, self.version, self.runtime_id

    @property
    def settings(self):
        from plugin_host.sdk.settings import get_plugin_settings

        return get_plugin_settings(self.slug)

    def storage(self, *, user=None, namespace="default"):
        return PluginStorage(self.slug, user=user, namespace=namespace)

    def register_hook(self, hook_name, callback, *, priority=100, failure_mode=None):
        if hook_name not in KNOWN_HOOKS:
            raise ValueError(f"unknown plugin hook: {hook_name}")
        if hook_name not in set(self.manifest.get("hooks") or []):
            raise ValueError(f"Plugin hook is not declared in Manifest: {hook_name}")
        host_mode = hook_failure_mode(hook_name)
        if failure_mode is not None and str(failure_mode).lower() != host_mode:
            raise ValueError(f"Hook failure policy is controlled by the Host: {hook_name}={host_mode}")
        self._hooks.append((hook_name, callback, priority))

    def request_json(self, method, url, **kwargs):
        policy = self.manifest.get("dataPolicy") or {}
        if not policy.get("usesExternalNetwork", False):
            raise ExternalNetworkDenied(f"{self.slug} 未声明外部网络权限。")
        response = requests.request(method, url, timeout=kwargs.pop("timeout", 8), **kwargs)
        response.raise_for_status()
        return response.json()

    def activate(self):
        self._hook_disposers = [
            self.hook_registry.register(
                hook_name,
                callback,
                self.hook_owner,
                priority=priority,
            )
            for hook_name, callback, priority in self._hooks
        ]

    def deactivate(self):
        for dispose in reversed(self._hook_disposers):
            dispose()
        self._hook_disposers.clear()
