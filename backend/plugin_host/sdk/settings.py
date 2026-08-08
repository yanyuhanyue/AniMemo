from plugin_host.models import PluginInstallation
from plugin_host.registry import PluginRegistryError, get_plugin, validate_plugin_config


def get_plugin_settings(plugin_slug):
    plugin = get_plugin(plugin_slug)
    installation = PluginInstallation.objects.filter(slug=plugin_slug).first()
    return dict(installation.config if installation else plugin.get("config") or {})


def get_plugin_setting(plugin_slug, key, default=None):
    return get_plugin_settings(plugin_slug).get(key, default)


def set_plugin_setting(plugin_slug, key, value, *, actor=None):
    if not actor or not getattr(actor, "is_superuser", False):
        raise PermissionError("插件运行时不能写入宿主配置；请由超级管理员在插件中心保存设置。")
    plugin = get_plugin(plugin_slug)
    current = get_plugin_settings(plugin_slug)
    current[key] = value
    normalized = validate_plugin_config(plugin, current)
    installation = PluginInstallation.objects.filter(slug=plugin_slug).first()
    if installation is None:
        raise PluginRegistryError("插件尚未完成安装，无法保存配置。")
    installation.config = normalized
    installation.updated_by = actor
    installation.save(update_fields=["config", "updated_by", "updated_at"])
    return normalized
