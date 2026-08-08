from rest_framework.permissions import BasePermission

from plugin_host.permissions import has_plugin_permission


class PluginPermissionRequired(BasePermission):
    permission_code = ""

    def has_permission(self, request, view):
        return has_plugin_permission(
            request.user,
            getattr(view, "plugin_slug", ""),
            getattr(view, "plugin_permission", self.permission_code),
        )
