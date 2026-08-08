from plugin_host.models import PluginDeployment, PluginProject, UserPluginInstallation
from plugin_host.registry import PluginRegistryError, get_plugin, validate_plugin_config


def _scoped_definitions(plugin, scope):
    return [item for item in plugin.get("settings") or [] if item.get("scope") == scope]


def _defaults(definitions):
    return {item["key"]: item.get("default") for item in definitions}


def get_system_settings(plugin_slug):
    plugin = get_plugin(plugin_slug)
    deployment = PluginDeployment.objects.filter(plugin__slug=plugin_slug).first()
    return {**_defaults(_scoped_definitions(plugin, "system")), **(deployment.system_config if deployment else {})}


def get_user_settings(plugin_slug, user):
    if not user or not getattr(user, "is_authenticated", False):
        raise PermissionError("用户插件配置必须绑定已认证用户。")
    plugin = get_plugin(plugin_slug)
    installation = UserPluginInstallation.objects.filter(plugin__slug=plugin_slug, user=user, enabled=True).first()
    if installation is None:
        raise PermissionError("用户未安装或未启用该插件。")
    return {**_defaults(_scoped_definitions(plugin, "user")), **installation.config}


def set_system_setting(plugin_slug, key, value, *, actor=None):
    if not actor or not getattr(actor, "is_superuser", False):
        raise PermissionError("只有超级管理员可以修改系统插件配置。")
    plugin = get_plugin(plugin_slug)
    deployment = PluginDeployment.objects.filter(plugin__slug=plugin_slug).first()
    if deployment is None:
        raise PluginRegistryError("插件尚未部署。")
    definitions = _scoped_definitions(plugin, "system")
    current = dict(deployment.system_config or {})
    current[key] = value
    deployment.system_config = validate_plugin_config({**plugin, "settings": definitions}, current)
    deployment.updated_by = actor
    deployment.save(update_fields=["system_config", "updated_by", "updated_at"])
    return deployment.system_config


def set_user_setting(plugin_slug, key, value, *, user):
    project = PluginProject.objects.filter(slug=plugin_slug).first()
    installation = UserPluginInstallation.objects.filter(plugin=project, user=user, enabled=True).first()
    if installation is None:
        raise PermissionError("只有已安装并启用插件的用户可以修改自己的配置。")
    plugin = get_plugin(plugin_slug)
    definitions = _scoped_definitions(plugin, "user")
    current = dict(installation.config or {})
    current[key] = value
    installation.config = validate_plugin_config({**plugin, "settings": definitions}, current)
    installation.save(update_fields=["config", "updated_at"])
    return installation.config
