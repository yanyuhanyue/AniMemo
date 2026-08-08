import mimetypes
import re
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core import signing
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404
from rest_framework import permissions, status
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView

from journal.staff_services import StaffCapabilityPermission, get_security_profile, record_audit

from .installer import PluginInstallError, PluginPackageInstaller
from .models import PluginInstallation
from .permissions import can_access_plugin_frontend
from .registry import PluginRegistryError, discover_plugins, get_plugin, read_runtime_manifest, validate_plugin_config
from .runtime import RuntimeLoadError, runtime_registry
from .serializers import PluginInstallationUpdateSerializer


class StaffPluginListView(APIView):
    permission_classes = [StaffCapabilityPermission]
    required_capability = "manage_system"

    def get(self, request):
        plugins = discover_plugins()
        return Response({
            "plugins": plugins,
            "can_install": bool(request.user.is_superuser),
            "summary": {
                "installed": len(plugins),
                "enabled": sum(plugin["effective_enabled"] for plugin in plugins),
                "attention": sum(not plugin["ready"] for plugin in plugins),
            },
        })


class EnabledPluginListView(APIView):
    permission_classes = [permissions.AllowAny]

    def get(self, request):
        plugins = []
        manifests = {}
        for plugin in discover_plugins():
            frontend = plugin.get("frontend") or {}
            installation = PluginInstallation.objects.filter(slug=plugin["slug"]).first()
            manifest = plugin.get("manifest") or {}
            if (
                not installation
                or not plugin.get("effective_enabled")
                or not frontend.get("enabled")
                or not frontend.get("ready")
                or not can_access_plugin_frontend(request.user, installation, manifest)
            ):
                continue
            version = installation.current_version
            exposure = frontend.get("exposure", "public")
            asset_prefix = f"/plugin-assets/{plugin['slug']}/{version}"
            if exposure != "public":
                asset_session = signing.dumps(
                    {
                        "user_id": request.user.pk,
                        "session_version": get_security_profile(request.user).session_version,
                        "slug": plugin["slug"],
                        "version": version,
                    },
                    salt="plugin-asset-session-v2",
                    compress=True,
                )
                asset_prefix = f"/plugin-assets/session/{asset_session}/{plugin['slug']}/{version}"
            plugins.append({
                "slug": plugin["slug"],
                "route_prefix": frontend.get("routePrefix", f"/plugins/{plugin['slug']}"),
                "frontendEntry": f"{asset_prefix}/plugin.js",
                "styleEntry": f"{asset_prefix}/plugin.css" if frontend.get("styleEntry") else "",
                "sdkApi": plugin.get("sdkApi", 2),
                "version": version,
                "exposure": exposure,
            })
            manifests[plugin["slug"]] = {
                "id": plugin.get("id", ""),
                "name": plugin.get("name", plugin["slug"]),
                "version": version,
                "sdkApi": plugin.get("sdkApi", 2),
                "extensions": plugin.get("extensions") or [],
                "runtimes": plugin.get("runtimes") or [],
                "permissions": plugin.get("permissions") or [],
                "hooks": plugin.get("hooks") or [],
                "dataPolicy": plugin.get("data_policy") or {},
            }
        return Response({"plugins": plugins, "manifests": manifests})


class PluginAssetView(APIView):
    permission_classes = [permissions.AllowAny]

    @staticmethod
    def _session_user(asset_session, slug, version):
        try:
            payload = signing.loads(
                asset_session,
                salt="plugin-asset-session-v2",
                max_age=int(getattr(settings, "PLUGIN_ASSET_SESSION_SECONDS", 120)),
            )
            if payload.get("slug") != slug or payload.get("version") != version:
                raise signing.BadSignature
            user = get_user_model().objects.get(pk=payload.get("user_id"), is_active=True)
            if get_security_profile(user).session_version != int(payload.get("session_version")):
                raise signing.BadSignature
            return user
        except (TypeError, ValueError, signing.BadSignature, get_user_model().DoesNotExist) as error:
            raise Http404 from error

    def get(self, request, slug, version, asset, asset_session=None):
        if not re.fullmatch(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*", slug or "") or not re.fullmatch(r"[0-9A-Za-z][0-9A-Za-z.-]{0,39}", version or ""):
            raise Http404
        installation = PluginInstallation.objects.filter(slug=slug).first()
        if not installation or installation.current_version != version:
            raise Http404
        runtime_root = Path(settings.PLUGIN_ROOT) / "runtime" / slug / version
        manifest, errors = read_runtime_manifest(runtime_root)
        user = self._session_user(asset_session, slug, version) if asset_session else request.user
        if errors or not can_access_plugin_frontend(user, installation, manifest):
            raise Http404
        relative = Path(*str(asset or "").split("/"))
        if not relative.parts or any(part in {"", ".", ".."} or part.startswith(".") for part in relative.parts) or relative.is_absolute() or len(relative.parts) > 8:
            raise Http404
        if relative.name not in {"plugin.js", "plugin.css"} and "assets" not in relative.parts:
            raise Http404
        frontend_root = (runtime_root / "frontend").resolve()
        path = (frontend_root / relative).resolve()
        try:
            path.relative_to(frontend_root)
        except ValueError:
            raise Http404
        if not path.is_file() or path.is_symlink():
            raise Http404
        content_type = {
            ".js": "text/javascript; charset=utf-8", ".mjs": "text/javascript; charset=utf-8",
            ".css": "text/css; charset=utf-8", ".json": "application/json; charset=utf-8",
            ".svg": "image/svg+xml", ".png": "image/png", ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg", ".webp": "image/webp", ".woff": "font/woff", ".woff2": "font/woff2",
        }.get(path.suffix.lower(), mimetypes.guess_type(path.name)[0] or "application/octet-stream")
        response = FileResponse(path.open("rb"), content_type=content_type)
        exposure = (manifest.get("frontend") or {}).get("exposure", "public")
        response["Cache-Control"] = "public, max-age=31536000, immutable" if exposure == "public" else "private, no-store"
        response["Vary"] = "Authorization, Cookie"
        return response


class StaffPluginInstallView(APIView):
    permission_classes = [StaffCapabilityPermission]
    required_capability = "manage_system"
    parser_classes = [MultiPartParser, FormParser]

    def post(self, request):
        if not request.user.is_superuser:
            return Response({"code": "plugin_install_forbidden", "detail": "只有超级管理员可以安装或升级插件代码。"}, status=status.HTTP_403_FORBIDDEN)
        replace = str(request.data.get("replace", "")).lower() in {"1", "true", "yes", "on"}
        try:
            result = PluginPackageInstaller().install(request.FILES.get("archive"), replace=replace, actor=request.user)
            plugin = get_plugin(result["slug"])
        except (PluginInstallError, PluginRegistryError, RuntimeLoadError, OSError) as error:
            return Response({"code": "plugin_install_failed", "detail": str(error)}, status=status.HTTP_400_BAD_REQUEST)
        installation = PluginInstallation.objects.get(slug=result["slug"])
        record_audit(
            request,
            action="plugin.upgrade" if result["replaced"] else "plugin.install",
            target=installation,
            after={"current_version": result["version"], "previous_version": result["previous_version"], "enabled": installation.enabled},
        )
        detail = f"{plugin['name']} 已升级并立即切换 Runtime。" if result["replaced"] else f"{plugin['name']} 已安装，当前保持停用。"
        return Response({"detail": detail, "plugin": plugin, **result}, status=status.HTTP_200_OK if result["replaced"] else status.HTTP_201_CREATED)


class StaffPluginDetailView(APIView):
    permission_classes = [StaffCapabilityPermission]
    required_capability = "manage_system"

    def patch(self, request, slug):
        serializer = PluginInstallationUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        installation = get_object_or_404(PluginInstallation, slug=slug)
        before = {"enabled": installation.enabled, "config": installation.config}
        try:
            plugin = get_plugin(slug)
            if "config" in serializer.validated_data:
                installation.config = validate_plugin_config(plugin, serializer.validated_data["config"])
                installation.updated_by = request.user
                installation.save(update_fields=["config", "updated_by", "updated_at"])
            if "enabled" in serializer.validated_data and serializer.validated_data["enabled"] != installation.enabled:
                PluginPackageInstaller().set_enabled(slug, serializer.validated_data["enabled"], actor=request.user)
        except (PluginInstallError, PluginRegistryError, RuntimeLoadError) as error:
            return Response({"code": "plugin_invalid", "detail": str(error)}, status=status.HTTP_400_BAD_REQUEST)
        installation.refresh_from_db()
        record_audit(request, action="plugin.enable" if installation.enabled else "plugin.disable", target=installation, before=before, after={"enabled": installation.enabled, "config": installation.config})
        return Response(get_plugin(slug))

    def delete(self, request, slug):
        if not request.user.is_superuser:
            return Response({"detail": "只有超级管理员可以卸载插件。"}, status=status.HTTP_403_FORBIDDEN)
        try:
            snapshot = PluginPackageInstaller().uninstall(slug)
        except (PluginInstallError, PluginInstallation.DoesNotExist) as error:
            return Response({"detail": str(error)}, status=status.HTTP_400_BAD_REQUEST)
        record_audit(request, action="plugin.uninstall", target_type="PluginInstallation", target_id=slug, target_label=slug, before=snapshot)
        return Response(status=status.HTTP_204_NO_CONTENT)


class StaffPluginRollbackView(APIView):
    permission_classes = [StaffCapabilityPermission]
    required_capability = "manage_system"

    def post(self, request, slug):
        if not request.user.is_superuser:
            return Response({"detail": "只有超级管理员可以回滚插件。"}, status=status.HTTP_403_FORBIDDEN)
        installation = get_object_or_404(PluginInstallation, slug=slug)
        before = {"current_version": installation.current_version, "previous_version": installation.previous_version}
        try:
            result = PluginPackageInstaller().rollback(slug, actor=request.user)
        except (PluginInstallError, RuntimeLoadError, OSError) as error:
            return Response({"detail": str(error)}, status=status.HTTP_400_BAD_REQUEST)
        installation.refresh_from_db()
        record_audit(request, action="plugin.rollback", target=installation, before=before, after=result)
        return Response({"detail": "插件已回滚并立即切换前后端 Runtime。", "plugin": get_plugin(slug)})


class StaffPluginCleanupView(APIView):
    permission_classes = [StaffCapabilityPermission]
    required_capability = "manage_system"

    def post(self, request, slug=None):
        try:
            result = PluginPackageInstaller().cleanup(slug)
        except (PluginInstallError, OSError) as error:
            return Response({"detail": str(error)}, status=status.HTTP_400_BAD_REQUEST)
        record_audit(request, action="plugin.cleanup", target_type="PluginInstallation", target_id=slug or "all", target_label=slug or "all", after=result)
        return Response({"detail": "旧插件版本与 staging 已清理。", "result": result})
