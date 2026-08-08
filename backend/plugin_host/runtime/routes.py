from __future__ import annotations

import re
from dataclasses import dataclass


class PluginRouteError(ValueError):
    pass


@dataclass(frozen=True)
class ResolvedPluginRoute:
    handler: object
    access: str
    permission: str
    kwargs: dict


@dataclass(frozen=True)
class _PluginRoute:
    method: str
    path: str
    pattern: re.Pattern
    handler: object
    access: str
    permission: str


class PluginApi:
    """Small host-owned router for one in-process backend plugin."""

    def __init__(self, slug, manifest):
        self.slug = slug
        self._declared_permissions = {
            item.get("code")
            for item in (manifest.get("permissions") or [])
            if isinstance(item, dict) and item.get("code")
        }
        self._routes: list[_PluginRoute] = []

    @staticmethod
    def _compile_path(path):
        normalized = str(path or "").strip("/")
        if not normalized or ".." in normalized.split("/"):
            raise PluginRouteError("Plugin backend route path is invalid.")
        parts = []
        for segment in normalized.split("/"):
            match = re.fullmatch(r"<([a-z][a-z0-9_]*)>", segment)
            if match:
                parts.append(fr"(?P<{match.group(1)}>[^/]+)")
            elif re.fullmatch(r"[A-Za-z0-9._~-]+", segment):
                parts.append(re.escape(segment))
            else:
                raise PluginRouteError("Plugin backend route path is invalid.")
        return normalized, re.compile(r"^" + "/".join(parts) + r"$")

    def route(self, method, path, *, handler, access, permission=""):
        verb = str(method or "").upper()
        if verb not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
            raise PluginRouteError("Plugin backend route method is invalid.")
        if not callable(handler):
            raise PluginRouteError("Plugin backend route handler must be callable.")
        access_mode = str(access or "").strip().lower()
        if access_mode not in {"user", "staff"}:
            raise PluginRouteError("Plugin backend routes must declare access=user or access=staff.")
        required = str(permission or "").strip()
        if access_mode == "staff" and not required:
            raise PluginRouteError("Staff plugin routes must declare a permission.")
        if required and required not in self._declared_permissions:
            raise PluginRouteError(f"Plugin backend route permission is not declared in Manifest: {required}")
        normalized, pattern = self._compile_path(path)
        if any(item.method == verb and item.path == normalized for item in self._routes):
            raise PluginRouteError(f"Duplicate plugin backend route: {verb} {normalized}")
        self._routes.append(_PluginRoute(verb, normalized, pattern, handler, access_mode, required))
        return handler

    def get(self, path, *, handler, access, permission=""):
        return self.route("GET", path, handler=handler, access=access, permission=permission)

    def post(self, path, *, handler, access, permission=""):
        return self.route("POST", path, handler=handler, access=access, permission=permission)

    def put(self, path, *, handler, access, permission=""):
        return self.route("PUT", path, handler=handler, access=access, permission=permission)

    def patch(self, path, *, handler, access, permission=""):
        return self.route("PATCH", path, handler=handler, access=access, permission=permission)

    def delete(self, path, *, handler, access, permission=""):
        return self.route("DELETE", path, handler=handler, access=access, permission=permission)

    def resolve(self, method, path):
        verb = str(method or "").upper()
        normalized = str(path or "").strip("/")
        for route in self._routes:
            if route.method != verb:
                continue
            match = route.pattern.fullmatch(normalized)
            if match:
                return ResolvedPluginRoute(route.handler, route.access, route.permission, match.groupdict())
        return None

    def __bool__(self):
        return bool(self._routes)
