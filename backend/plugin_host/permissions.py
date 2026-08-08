"""Shared permission decisions for metadata, assets, and backend dispatch."""

from .models import PluginDeployment, PluginProject, UserPluginInstallation


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


def _healthy_deployment(plugin_slug):
    return PluginDeployment.objects.select_related("plugin", "current_version").filter(
        plugin__slug=plugin_slug,
        enabled=True,
        healthy=True,
        current_version__revoked_at__isnull=True,
    ).first()


def is_user_plugin_enabled(user, plugin):
    return bool(
        user
        and getattr(user, "is_authenticated", False)
        and UserPluginInstallation.objects.filter(user=user, plugin=plugin, enabled=True).exists()
    )


def is_user_plugin_enabled_for_id(plugin, user_id):
    return bool(
        user_id
        and plugin
        and plugin.installation_mode == plugin.InstallationMode.USER
        and UserPluginInstallation.objects.filter(user_id=user_id, plugin=plugin, enabled=True).exists()
    )


def enabled_user_plugin(slug, user):
    if not user or not getattr(user, "is_authenticated", False):
        return None
    project = PluginProject.objects.filter(
        slug=slug,
        installation_mode=PluginProject.InstallationMode.USER,
        status=PluginProject.Status.ACTIVE,
    ).first()
    if project is None or not is_user_plugin_enabled(user, project):
        return None
    return project


def has_plugin_permission(user, plugin_slug, permission_code):
    from .registry import PluginRegistryError, get_plugin

    if _healthy_deployment(plugin_slug) is None:
        return False
    try:
        plugin = get_plugin(plugin_slug)
    except PluginRegistryError:
        return False
    return _has_definition_permission(user, _definitions(plugin.get("manifest") or plugin), permission_code)


def can_access_plugin_frontend(user, deployment, manifest):
    if not deployment.enabled or not deployment.healthy:
        return False
    exposure = (manifest.get("frontend") or {}).get("exposure", "user")
    if deployment.plugin.installation_mode == deployment.plugin.InstallationMode.USER:
        return is_user_plugin_enabled(user, deployment.plugin)
    if exposure == "public":
        return True
    if exposure == "authenticated":
        return bool(user and getattr(user, "is_authenticated", False))
    return bool(_staff_role(user) and (not _definitions(manifest) or _has_definition_permission(user, _definitions(manifest))))


def can_access_plugin_backend(user, plugin_slug, manifest, *, access, permission_code=""):
    deployment = _healthy_deployment(plugin_slug)
    if deployment is None or not user or not getattr(user, "is_authenticated", False):
        return False
    if access == "user":
        return is_user_plugin_enabled(user, deployment.plugin)
    if access != "staff":
        return False
    definitions = _definitions(manifest)
    if not permission_code or permission_code not in {item.get("code") for item in definitions}:
        return False
    return _has_definition_permission(user, definitions, permission_code)


def plugin_permissions_for_user(user):
    from .registry import discover_plugins

    if not user or not getattr(user, "is_authenticated", False):
        return []
    result = []
    for plugin in discover_plugins():
        for definition in _definitions(plugin.get("manifest") or plugin):
            code = definition.get("code")
            if code and _has_definition_permission(user, [definition], code):
                result.append(code)
    return sorted(set(result))
