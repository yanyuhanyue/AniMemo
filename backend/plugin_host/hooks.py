"""Host-owned Hook Registry for Plugin SDK v2."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from threading import RLock
from time import monotonic
from types import SimpleNamespace


logger = logging.getLogger("anime_journal.plugins")
from .hook_contract import ACTION_HOOKS, CLOSED_HOOKS, FILTER_HOOKS, HOOK_DEFINITIONS, SUPPORTED_HOOKS

HOOK_SLOW_WARNING_SECONDS = 0.25
KNOWN_HOOKS = SUPPORTED_HOOKS


class RegistrationHookRejected(RuntimeError):
    """Raised by a closed registration hook when it rejects a request."""


class RegistrationHookUnavailable(RuntimeError):
    """Raised when a closed registration hook cannot evaluate a request."""


def hook_failure_mode(hook_name):
    if hook_name not in KNOWN_HOOKS:
        raise ValueError(f"unknown plugin hook: {hook_name}")
    return "closed" if hook_name in CLOSED_HOOKS else "open"


@dataclass(frozen=True)
class HookRegistration:
    hook_name: str
    callback: object
    plugin_slug: str
    plugin_version: str
    runtime_id: str
    priority: int
    failure_mode: str
    sequence: int


class HookRegistry:
    def __init__(self, runtime_registry=None):
        self._lock = RLock()
        self._registrations: list[HookRegistration] = []
        self._sequence = 0
        self._runtime_registry = runtime_registry

    def bind_runtime_registry(self, registry):
        self._runtime_registry = registry

    def register(self, hook_name, callback, owner, *, priority=100):
        if hook_name not in KNOWN_HOOKS:
            raise ValueError(f"unknown plugin hook: {hook_name}")
        if not callable(callback):
            raise ValueError("plugin hook requires a callable callback")
        try:
            plugin_slug, plugin_version, runtime_id = owner
        except (TypeError, ValueError) as error:
            raise ValueError("plugin hook requires a runtime owner token") from error
        with self._lock:
            self._sequence += 1
            registration = HookRegistration(
                hook_name,
                callback,
                str(plugin_slug),
                str(plugin_version),
                str(runtime_id),
                int(priority),
                hook_failure_mode(hook_name),
                self._sequence,
            )
            self._registrations.append(registration)
        return lambda: self.remove(registration)

    def remove(self, registration):
        with self._lock:
            if registration in self._registrations:
                self._registrations.remove(registration)

    def unregister_owner(self, owner):
        slug, version, runtime_id = (str(value) for value in owner)
        with self._lock:
            self._registrations[:] = [
                item
                for item in self._registrations
                if (item.plugin_slug, item.plugin_version, item.runtime_id) != (slug, version, runtime_id)
            ]

    def unregister_plugin(self, plugin_slug):
        with self._lock:
            self._registrations[:] = [item for item in self._registrations if item.plugin_slug != plugin_slug]

    def reconcile(self):
        if self._runtime_registry is None:
            return []
        return self._runtime_registry.reconcile_all()

    def _snapshot(self, hook_name):
        errors = self.reconcile()
        with self._lock:
            items = sorted(
                (item for item in self._registrations if item.hook_name == hook_name),
                key=lambda item: (item.priority, item.sequence),
            )
        return items, errors

    def run_hook(self, hook_name, context):
        if hook_name not in ACTION_HOOKS:
            raise ValueError(f"{hook_name} is not an action hook")
        items, reconcile_errors = self._snapshot(hook_name)
        if reconcile_errors and hook_failure_mode(hook_name) == "closed":
            raise reconcile_errors[0][1]
        results = []
        for item in items:
            started = monotonic()
            try:
                results.append(item.callback(context))
            except Exception:
                logger.exception("plugin=%s hook=%s failed", item.plugin_slug, hook_name)
                if item.failure_mode == "closed":
                    raise
            finally:
                elapsed = monotonic() - started
                if elapsed > HOOK_SLOW_WARNING_SECONDS:
                    logger.warning("plugin=%s hook=%s slow duration_ms=%.1f", item.plugin_slug, hook_name, elapsed * 1000)
        return results

    def run_filter(self, hook_name, value, context):
        if hook_name not in FILTER_HOOKS:
            raise ValueError(f"{hook_name} is not a filter hook")
        items, reconcile_errors = self._snapshot(hook_name)
        if reconcile_errors and hook_failure_mode(hook_name) == "closed":
            raise reconcile_errors[0][1]
        current = value
        for item in items:
            started = monotonic()
            try:
                next_value = item.callback(current, context)
                if next_value is not None:
                    current = next_value
            except Exception:
                logger.exception("plugin=%s filter=%s failed", item.plugin_slug, hook_name)
                if item.failure_mode == "closed":
                    raise
            finally:
                elapsed = monotonic() - started
                if elapsed > HOOK_SLOW_WARNING_SECONDS:
                    logger.warning("plugin=%s filter=%s slow duration_ms=%.1f", item.plugin_slug, hook_name, elapsed * 1000)
        return current

    def clear(self):
        with self._lock:
            self._registrations.clear()

    def registrations_for(self, plugin_slug):
        with self._lock:
            return tuple(item for item in self._registrations if item.plugin_slug == plugin_slug)


hook_registry = HookRegistry()


def register_hook(hook_name, callback, owner, *, priority=100):
    return hook_registry.register(hook_name, callback, owner, priority=priority)


def unregister_plugin_hooks(plugin_slug):
    hook_registry.unregister_plugin(plugin_slug)


def run_hook(hook_name, context):
    return hook_registry.run_hook(hook_name, context)


def run_filter(hook_name, value, context):
    return hook_registry.run_filter(hook_name, value, context)


def _safe_registration_context(kwargs):
    blocked = {"password", "raw_token", "token", "completion_token", "csrf_token", "jwt", "refresh_token"}
    context = {key: value for key, value in kwargs.items() if key not in blocked}
    request = context.get("request")
    if request is not None:
        request_user = getattr(request, "user", None)
        safe_request_user = SimpleNamespace(
            pk=getattr(request_user, "pk", None),
            id=getattr(request_user, "pk", None),
            username=getattr(request_user, "username", ""),
            email=getattr(request_user, "email", ""),
            is_authenticated=bool(getattr(request_user, "is_authenticated", False)),
            is_staff=getattr(request_user, "is_staff", False),
        )
        context["request"] = SimpleNamespace(
            method=getattr(request, "method", ""),
            path=getattr(request, "path", ""),
            user=safe_request_user,
            META={
                key: request.META.get(key)
                for key in ("REMOTE_ADDR", "HTTP_USER_AGENT")
                if getattr(request, "META", {}).get(key)
            },
        )
    user = context.get("user")
    if user is not None:
        context["user"] = SimpleNamespace(
            pk=getattr(user, "pk", None),
            id=getattr(user, "pk", None),
            username=getattr(user, "username", ""),
            email=getattr(user, "email", ""),
            is_active=getattr(user, "is_active", False),
            is_staff=getattr(user, "is_staff", False),
        )
    return context


def run_registration_hook(hook_name, **kwargs):
    try:
        run_hook(hook_name, _safe_registration_context(kwargs))
    except RegistrationHookRejected:
        raise
    except Exception as error:
        if hook_failure_mode(hook_name) == "closed":
            raise RegistrationHookUnavailable("注册策略服务暂时不可用，请稍后重试。") from error
        logger.exception("registration hook=%s failed open", hook_name)


def clear_hooks_for_tests():
    hook_registry.clear()
