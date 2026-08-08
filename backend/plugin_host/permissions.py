"""Shared permission decisions for metadata, assets, and backend dispatch."""


def _staff_role(user):
    from journal.staff_services import resolve_staff_role

    return resolve_staff_role(user)


def _definitions(manifest):
    return [item for item in (manifest.get("permissions") or []) if isinstance(item, dict) and item.get("code")]


def _has_definition_permission(user, definitions, permission_code=None):
    if not user or not getattr(user, "is_authenticated", False):
        return False
    if getattr(user, "is_superuser", False):
        return True
    role = _staff_role(user)
    if not role or role == "unassigned":
        return False
    selected = [item for item in definitions if permission_code is None or item.get("code") == permission_code]
    return any(role in set(item.get("roles") or []) for item in selected)


def has_plugin_permission(user, plugin_slug, permission_code):
    from .models import PluginInstallation
    from .registry import PluginRegistryError, get_plugin

    installation = PluginInstallation.objects.filter(slug=plugin_slug, enabled=True, healthy=True).first()
    if installation is None:
        return False
    try:
        plugin = get_plugin(plugin_slug)
    except PluginRegistryError:
        return False
    return _has_definition_permission(user, _definitions(plugin.get("manifest") or plugin), permission_code)


def can_access_plugin_frontend(user, installation, manifest):
    if not installation.enabled or not installation.healthy:
        return False
    frontend = manifest.get("frontend") or {}
    exposure = frontend.get("exposure", "public")
    if exposure == "public":
        return True
    if not user or not getattr(user, "is_authenticated", False):
        return False
    if exposure == "authenticated":
        return True
    if exposure != "staff" or not _staff_role(user):
        return False
    definitions = _definitions(manifest)
    return not definitions or _has_definition_permission(user, definitions)


def can_access_plugin_backend(user, plugin_slug, manifest, *, permission_code):
    from .models import PluginInstallation

    installation = PluginInstallation.objects.filter(slug=plugin_slug, enabled=True, healthy=True).first()
    if installation is None:
        return False
    if not user or not getattr(user, "is_authenticated", False):
        return False
    definitions = _definitions(manifest)
    if not permission_code or permission_code not in {item.get("code") for item in definitions}:
        return False
    return _has_definition_permission(user, definitions, permission_code)


def plugin_permissions_for_user(user):
    from .registry import discover_plugins

    if not user or not getattr(user, "is_authenticated", False):
        return []
    permissions = []
    for plugin in discover_plugins():
        for definition in _definitions(plugin.get("manifest") or plugin):
            code = definition.get("code")
            if code and _has_definition_permission(user, [definition], code):
                permissions.append(code)
    return sorted(set(permissions))
