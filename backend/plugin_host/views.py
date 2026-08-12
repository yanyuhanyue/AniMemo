import mimetypes
import re
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core import signing
from django.db.models import Count, Prefetch
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import permissions, status
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView

from journal.staff_services import StaffCapabilityPermission, get_security_profile, record_audit

from .installer import PluginInstallError, PluginPackageInstaller
from .models import PluginDeployment, PluginProject, PluginSubmission, PluginVersion, UserPluginInstallation
from .permissions import can_access_plugin_frontend
from .registry import PluginRegistryError, discover_plugins, get_plugin, read_runtime_manifest, validate_plugin_config
from .package import PluginPackageError, inspect_package
from .runtime import RuntimeLoadError
from .serializers import PluginDeploymentUpdateSerializer
from .services import (
    PluginWorkflowError,
    archive_or_delete_plugin_project,
    create_plugin_project,
    create_frontend_preview,
    install_for_user,
    plugin_upload_policy,
    review_submission,
    submit_version,
    uninstall_for_user,
    update_plugin_project,
    upload_plugin_version,
    withdraw_submission,
)


ASSET_SESSION_SALT = "plugin-asset-session-v2"
PREVIEW_SESSION_SALT = "plugin-preview-session-v3"


def _signed_user_payload(user, **extra):
    return {
        "user_id": user.pk,
        "session_version": get_security_profile(user).session_version,
        **extra,
    }


def _sign_asset_session(user, slug, version):
    return signing.dumps(
        _signed_user_payload(user, slug=slug, version=version),
        salt=ASSET_SESSION_SALT,
        compress=True,
    )


def _sign_preview_session(user, version):
    return signing.dumps(
        _signed_user_payload(
            user,
            version_id=version.pk,
            slug=version.plugin.slug,
            version=version.version,
        ),
        salt=PREVIEW_SESSION_SALT,
        compress=True,
    )


def _load_signed_user(token, *, salt, max_age):
    try:
        payload = signing.loads(token, salt=salt, max_age=max_age)
        user = get_user_model().objects.get(pk=payload.get("user_id"), is_active=True)
        if get_security_profile(user).session_version != int(payload.get("session_version")):
            raise signing.BadSignature
        return user, payload
    except (TypeError, ValueError, signing.BadSignature, get_user_model().DoesNotExist) as error:
        raise Http404 from error


def serialize_marketplace_project(project, *, user=None):
    deployment = getattr(project, "deployment", None)
    current_version = deployment.current_version if deployment else None
    manifest = current_version.manifest_snapshot if current_version else {}
    versions = getattr(project, "marketplace_published_versions", None)
    if versions is None:
        versions = project.versions.filter(
            published_at__isnull=False,
            revoked_at__isnull=True,
        ).order_by("-published_at")
    payload = {
        "id": project.pk,
        "plugin_id": project.plugin_id,
        "slug": project.slug,
        "name": project.name,
        "description": project.description,
        "installation_mode": project.installation_mode,
        "publisher": project.owner.get_username() if project.owner else "Anime Journal",
        "owner": project.owner.get_username() if project.owner else "Anime Journal",
        "published_version": current_version.version if current_version else None,
        "runtime_types": current_version.runtime_types if current_version else [],
        "permissions": [
            {"code": item.get("code"), "name": item.get("name", "")}
            for item in (manifest.get("permissions") or [])
            if isinstance(item, dict) and item.get("code")
        ],
        "dataPolicy": manifest.get("dataPolicy") or {},
        "versions": [
            {
                "version": version.version,
                "runtime_types": version.runtime_types,
                "published_at": version.published_at,
            }
            for version in versions
        ],
        "install_count": (
            project.marketplace_install_count
            if hasattr(project, "marketplace_install_count")
            else project.user_installations.count()
        ),
    }
    if user and getattr(user, "is_authenticated", False):
        installations = getattr(project, "marketplace_user_installations", None)
        if installations is None:
            installation = project.user_installations.filter(user=user).first()
        else:
            installation = installations[0] if installations else None
        payload["installation"] = {
            "enabled": installation.enabled,
            "config": installation.config,
        } if installation else None
    return payload


def serialize_developer_project(project):
    versions = project.versions.order_by("-created_at")
    payload = {
        "id": project.pk,
        "plugin_id": project.plugin_id,
        "slug": project.slug,
        "name": project.name,
        "description": project.description,
        "installation_mode": project.installation_mode,
        "status": project.status,
        "owner": project.owner.get_username() if project.owner else "",
        "versions": [
            {
                "id": version.pk,
                "version": version.version,
                "review_status": version.review_status,
                "runtime_types": version.runtime_types,
                "package_sha256": version.package_blob.sha256,
                "published_at": version.published_at,
                "revoked_at": version.revoked_at,
                "created_at": version.created_at,
                "submission": (
                    {
                        "id": version.submissions.first().pk,
                        "status": version.submissions.first().status,
                        "review_note": version.submissions.first().review_note,
                        "security_report": version.submissions.first().security_report,
                    }
                    if version.submissions.first()
                    else None
                ),
            }
            for version in versions
        ],
    }
    deployment = getattr(project, "deployment", None)
    payload["deployment"] = (
        {
            "enabled": deployment.enabled,
            "healthy": deployment.healthy,
            "status": deployment.status,
            "current_version": deployment.current_version.version,
            "previous_version": deployment.previous_version.version if deployment.previous_version else None,
            "disk_bytes": deployment.disk_bytes,
            "last_error": deployment.last_error,
        }
        if deployment
        else None
    )
    payload["install_count"] = project.user_installations.count()
    return payload


class StaffPluginListView(APIView):
    permission_classes = [StaffCapabilityPermission]
    required_capability = "manage_system"

    def get(self, request):
        plugins = discover_plugins()
        return Response({
            "plugins": plugins,
            "can_install": bool(request.user.is_superuser),
            "policy": plugin_upload_policy(),
            "summary": {
                "installed": len(plugins),
                "enabled": sum(plugin["effective_enabled"] for plugin in plugins),
                "attention": sum(not plugin["ready"] for plugin in plugins),
            },
        })


class EnabledPluginListView(APIView):
    permission_classes = [permissions.AllowAny]

    def get(self, request):
        plugins, manifests = [], {}
        deployments = PluginDeployment.objects.select_related("plugin", "current_version").filter(
            enabled=True, healthy=True, current_version__revoked_at__isnull=True,
        )
        for deployment in deployments:
            directory = Path(settings.PLUGIN_ROOT) / "runtime" / deployment.plugin.slug / deployment.current_version.version
            manifest, errors = read_runtime_manifest(directory)
            if errors or not manifest or not can_access_plugin_frontend(request.user, deployment, manifest):
                continue
            version = deployment.current_version.version
            exposure = (manifest.get("frontend") or {}).get("exposure", "public")
            asset_prefix = f"/plugin-assets/{deployment.plugin.slug}/{version}"
            if exposure != "public" or deployment.plugin.installation_mode == PluginProject.InstallationMode.USER:
                if not request.user or not request.user.is_authenticated:
                    continue
                asset_session = _sign_asset_session(request.user, deployment.plugin.slug, version)
                asset_prefix = f"/plugin-assets/session/{asset_session}/{deployment.plugin.slug}/{version}"
            plugins.append({
                "slug": deployment.plugin.slug,
                "route_prefix": f"/plugins/{deployment.plugin.slug}",
                "frontendEntry": f"{asset_prefix}/plugin.js",
                "styleEntry": f"{asset_prefix}/plugin.css" if (manifest.get("frontend") or {}).get("styleEntry") else "",
                "sdkApi": manifest.get("sdkApi", 2),
                "version": version,
                "exposure": exposure,
            })
            manifests[deployment.plugin.slug] = manifest
        return Response({"plugins": plugins, "manifests": manifests})


class PluginAssetView(APIView):
    permission_classes = [permissions.AllowAny]

    @staticmethod
    def _session_user(asset_session, slug, version):
        user, payload = _load_signed_user(
            asset_session,
            salt=ASSET_SESSION_SALT,
            max_age=int(getattr(settings, "PLUGIN_ASSET_SESSION_SECONDS", 120)),
        )
        if payload.get("slug") != slug or payload.get("version") != version:
            raise Http404
        return user

    def get(self, request, slug, version, asset, asset_session=None):
        if not re.fullmatch(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*", slug or ""):
            raise Http404
        deployment = PluginDeployment.objects.select_related("plugin", "current_version").filter(plugin__slug=slug, enabled=True, healthy=True).first()
        if not deployment or deployment.current_version.version != version or deployment.current_version.revoked_at:
            raise Http404
        runtime_root = Path(settings.PLUGIN_ROOT) / "runtime" / slug / version
        manifest, errors = read_runtime_manifest(runtime_root)
        user = self._session_user(asset_session, slug, version) if asset_session else request.user
        if errors or not can_access_plugin_frontend(user, deployment, manifest):
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
        content_type = {".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8", ".json": "application/json; charset=utf-8"}.get(path.suffix.lower(), mimetypes.guess_type(path.name)[0] or "application/octet-stream")
        response = FileResponse(path.open("rb"), content_type=content_type)
        response["Cache-Control"] = "public, max-age=31536000, immutable" if (manifest.get("frontend") or {}).get("exposure") == "public" else "private, no-store"
        response["Vary"] = "Authorization, Cookie"
        return response


class StaffPluginInstallView(APIView):
    permission_classes = [StaffCapabilityPermission]
    required_capability = "manage_system"
    parser_classes = [MultiPartParser, FormParser]

    def post(self, request):
        if not request.user.is_superuser:
            return Response({"detail": "只有超级管理员可以发布包含服务器代码的插件。"}, status=status.HTTP_403_FORBIDDEN)
        archive = request.FILES.get("archive")
        try:
            if archive is None:
                raise PluginWorkflowError("请选择 .ajplugin 插件包。")
            raw = archive.read()
            inspected = inspect_package(raw)
            archive.seek(0)
            manifest = inspected["manifest"]
            project, _ = PluginProject.objects.get_or_create(
                plugin_id=manifest["id"],
                defaults={"slug": manifest["slug"], "name": manifest["name"], "description": manifest["description"], "installation_mode": manifest["installationMode"], "owner": request.user},
            )
            if project.slug != manifest["slug"]:
                raise PluginWorkflowError("plugin id 已绑定到其他 slug。")
            if project.installation_mode != manifest["installationMode"]:
                raise PluginWorkflowError("Manifest installationMode 与现有插件项目不一致。")
            version, report, _ = upload_plugin_version(project, archive, actor=request.user)
            version.review_status = PluginVersion.ReviewStatus.APPROVED
            version.save(update_fields=["review_status"])
            result = PluginPackageInstaller().publish(version, actor=request.user)
        except (PluginWorkflowError, PluginPackageError, PluginInstallError, RuntimeLoadError, OSError) as error:
            return Response({"detail": str(error)}, status=status.HTTP_400_BAD_REQUEST)
        record_audit(request, action="plugin.staff_publish_upload", target=version, after={"sha256": version.package_blob.sha256, "runtime_types": version.runtime_types})
        return Response({"detail": "插件已发布并部署。", "project": serialize_developer_project(project), "scan": report, **result}, status=status.HTTP_201_CREATED)


class StaffPluginDetailView(APIView):
    permission_classes = [StaffCapabilityPermission]
    required_capability = "manage_system"

    def patch(self, request, slug):
        deployment = get_object_or_404(PluginDeployment.objects.select_related("plugin", "current_version"), plugin__slug=slug)
        before = {"enabled": deployment.enabled, "system_config": deployment.system_config}
        serializer = PluginDeploymentUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            if "config" in serializer.validated_data:
                plugin = get_plugin(slug)
                definitions = [item for item in plugin.get("settings", []) if item.get("scope") == "system"]
                deployment.system_config = validate_plugin_config({**plugin, "settings": definitions}, serializer.validated_data["config"])
                deployment.updated_by = request.user
                deployment.save(update_fields=["system_config", "updated_by", "updated_at"])
            if "enabled" in serializer.validated_data:
                PluginPackageInstaller().set_enabled(slug, serializer.validated_data["enabled"], actor=request.user)
        except (PluginInstallError, PluginRegistryError, RuntimeLoadError) as error:
            return Response({"detail": str(error)}, status=status.HTTP_400_BAD_REQUEST)
        deployment.refresh_from_db()
        record_audit(request, action="plugin.deployment_update", target=deployment.plugin, before=before, after={"enabled": deployment.enabled, "system_config": deployment.system_config})
        return Response(get_plugin(slug))

    def delete(self, request, slug):
        deployment = get_object_or_404(PluginDeployment, plugin__slug=slug)
        deployment.plugin.status = PluginProject.Status.ARCHIVED
        deployment.plugin.save(update_fields=["status", "updated_at"])
        record_audit(request, action="plugin.project_archive", target=deployment.plugin)
        return Response(status=status.HTTP_204_NO_CONTENT)


class StaffPluginRollbackView(APIView):
    permission_classes = [StaffCapabilityPermission]
    required_capability = "manage_system"

    def post(self, request, slug):
        if not request.user.is_superuser:
            return Response({"detail": "只有超级管理员可以回滚插件。"}, status=status.HTTP_403_FORBIDDEN)
        try:
            result = PluginPackageInstaller().rollback(slug, actor=request.user)
        except (PluginInstallError, RuntimeLoadError, OSError) as error:
            return Response({"detail": str(error)}, status=status.HTTP_400_BAD_REQUEST)
        record_audit(request, action="plugin.deployment_rollback", target_type="PluginProject", target_label=slug, after=result)
        return Response({"detail": "插件已回滚。", "plugin": get_plugin(slug), **result})


class StaffPluginCleanupView(APIView):
    permission_classes = [StaffCapabilityPermission]
    required_capability = "manage_system"

    def post(self, request, slug=None):
        try:
            result = PluginPackageInstaller().cleanup(slug)
        except (PluginInstallError, OSError) as error:
            return Response({"detail": str(error)}, status=status.HTTP_400_BAD_REQUEST)
        record_audit(request, action="plugin.runtime_cleanup", target_type="PluginProject", target_label=slug or "all", after=result)
        return Response({"detail": "旧 Runtime 与 staging 已清理。", "result": result})


class MarketplaceView(APIView):
    permission_classes = [permissions.AllowAny]

    def get(self, request):
        published_versions = PluginVersion.objects.filter(
            published_at__isnull=False,
            revoked_at__isnull=True,
        ).order_by("-published_at")
        projects = PluginProject.objects.filter(
            status=PluginProject.Status.ACTIVE,
            installation_mode=PluginProject.InstallationMode.USER,
            deployment__enabled=True,
            deployment__healthy=True,
            deployment__current_version__published_at__isnull=False,
            deployment__current_version__revoked_at__isnull=True,
        ).select_related("owner", "deployment", "deployment__current_version").annotate(
            marketplace_install_count=Count("user_installations", distinct=True),
        ).prefetch_related(
            Prefetch("versions", queryset=published_versions, to_attr="marketplace_published_versions"),
        )
        if request.user and request.user.is_authenticated:
            projects = projects.prefetch_related(
                Prefetch(
                    "user_installations",
                    queryset=UserPluginInstallation.objects.filter(user=request.user),
                    to_attr="marketplace_user_installations",
                ),
            )
        return Response({"plugins": [serialize_marketplace_project(project, user=request.user) for project in projects]})


class MarketplaceDetailView(APIView):
    permission_classes = [permissions.AllowAny]

    def get(self, request, slug):
        project = get_object_or_404(PluginProject, slug=slug, installation_mode=PluginProject.InstallationMode.USER, status=PluginProject.Status.ACTIVE)
        deployment = getattr(project, "deployment", None)
        if not deployment or not deployment.enabled or not deployment.healthy or not deployment.current_version.published_at:
            raise Http404
        return Response(serialize_marketplace_project(project, user=request.user))


class InstalledPluginListView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        installations = UserPluginInstallation.objects.filter(user=request.user).select_related(
            "plugin", "plugin__deployment", "plugin__deployment__current_version",
        ).order_by("plugin__name", "plugin__slug")
        plugins = []
        for installation in installations:
            project = installation.plugin
            deployment = getattr(project, "deployment", None)
            version = deployment.current_version if deployment else None
            manifest = version.manifest_snapshot if version else {}
            plugins.append({
                "id": project.pk,
                "plugin_id": project.plugin_id,
                "slug": project.slug,
                "name": project.name,
                "description": project.description,
                "status": project.status,
                "installation_mode": project.installation_mode,
                "installation": {
                    "enabled": installation.enabled,
                    "config": installation.config,
                    "installed_at": installation.installed_at,
                    "updated_at": installation.updated_at,
                },
                "current_version": version.version if version else None,
                "runtime_types": version.runtime_types if version else [],
                "published": bool(version and version.published_at and not version.revoked_at),
                "available": bool(deployment and deployment.enabled and deployment.healthy and version and not version.revoked_at),
                "settings": [item for item in manifest.get("settings", []) if item.get("scope") == "user"],
            })
        return Response({"plugins": plugins})


class PluginPlatformPolicyView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        return Response({
            "package": plugin_upload_policy(),
            "draft_limit": int(getattr(settings, "PLUGIN_DRAFT_LIMIT", 20)),
            "uploads_per_hour": int(getattr(settings, "PLUGIN_UPLOADS_PER_HOUR", 12)),
        })


class UserPluginInstallationView(APIView):
    permission_classes = [permissions.IsAuthenticated]
    parser_classes = [JSONParser]

    def post(self, request, slug):
        project = get_object_or_404(PluginProject, slug=slug)
        try:
            installation, created = __import__("plugin_host.services", fromlist=["install_for_user"]).install_for_user(project, user=request.user)
        except PluginWorkflowError as error:
            return Response({"detail": str(error)}, status=status.HTTP_400_BAD_REQUEST)
        record_audit(request, action="plugin.user_install", target=project, after={"enabled": True, "created": created})
        return Response({"slug": slug, "enabled": installation.enabled, "created": created}, status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)

    def patch(self, request, slug):
        installation = get_object_or_404(UserPluginInstallation, user=request.user, plugin__slug=slug)
        if "enabled" in request.data:
            installation.enabled = bool(request.data["enabled"])
        if "config" in request.data:
            plugin = get_plugin(slug)
            definitions = [item for item in plugin.get("settings", []) if item.get("scope") == "user"]
            installation.config = validate_plugin_config({**plugin, "settings": definitions}, request.data["config"])
        installation.save()
        record_audit(request, action="plugin.user_installation_update", target=installation, after={"enabled": installation.enabled, "config": installation.config})
        return Response({"slug": slug, "enabled": installation.enabled, "config": installation.config})

    def delete(self, request, slug):
        project = get_object_or_404(PluginProject, slug=slug)
        deleted = __import__("plugin_host.services", fromlist=["uninstall_for_user"]).uninstall_for_user(project, user=request.user, delete_data=bool(request.data.get("delete_data")))
        if deleted:
            record_audit(request, action="plugin.user_uninstall", target=project, metadata={"delete_data": bool(request.data.get("delete_data"))})
        return Response(status=status.HTTP_204_NO_CONTENT if deleted else status.HTTP_404_NOT_FOUND)


class MyPluginProjectView(APIView):
    permission_classes = [permissions.IsAuthenticated]
    parser_classes = [JSONParser]

    def get(self, request):
        projects = PluginProject.objects.filter(owner=request.user).select_related(
            "deployment", "deployment__current_version", "deployment__previous_version",
        ).prefetch_related("versions__package_blob", "versions__submissions", "user_installations")
        return Response({
            "projects": [serialize_developer_project(item) for item in projects],
            "policy": {
                "package": plugin_upload_policy(),
                "draft_limit": int(getattr(settings, "PLUGIN_DRAFT_LIMIT", 20)),
                "uploads_per_hour": int(getattr(settings, "PLUGIN_UPLOADS_PER_HOUR", 12)),
            },
        })

    def post(self, request):
        try:
            project = create_plugin_project(
                actor=request.user,
                plugin_id=request.data.get("plugin_id"),
                slug=request.data.get("slug"),
                name=request.data.get("name"),
                description=request.data.get("description"),
            )
        except PluginWorkflowError as error:
            return Response({"detail": str(error)}, status=status.HTTP_400_BAD_REQUEST)
        record_audit(request, action="plugin.project_create", target=project, after=serialize_developer_project(project))
        return Response(serialize_developer_project(project), status=status.HTTP_201_CREATED)


class MyPluginProjectDetailView(APIView):
    permission_classes = [permissions.IsAuthenticated]
    parser_classes = [JSONParser]

    def get(self, request, project_id):
        project = get_object_or_404(
            PluginProject.objects.select_related("deployment", "deployment__current_version", "deployment__previous_version").prefetch_related(
                "versions__package_blob", "versions__submissions", "user_installations",
            ),
            pk=project_id,
            owner=request.user,
        )
        return Response(serialize_developer_project(project))

    def patch(self, request, project_id):
        project = get_object_or_404(PluginProject, pk=project_id, owner=request.user)
        before = {"name": project.name, "description": project.description}
        try:
            project = update_plugin_project(
                project,
                actor=request.user,
                name=request.data.get("name") if "name" in request.data else None,
                description=request.data.get("description") if "description" in request.data else None,
            )
        except PluginWorkflowError as error:
            return Response({"detail": str(error)}, status=status.HTTP_400_BAD_REQUEST)
        record_audit(request, action="plugin.project_update", target=project, before=before, after={"name": project.name, "description": project.description})
        return Response(serialize_developer_project(project))

    def delete(self, request, project_id):
        project = get_object_or_404(PluginProject, pk=project_id, owner=request.user)
        target_id, target_label = str(project.pk), project.slug
        try:
            result = archive_or_delete_plugin_project(project, actor=request.user)
        except PluginWorkflowError as error:
            return Response({"detail": str(error)}, status=status.HTTP_400_BAD_REQUEST)
        record_audit(
            request,
            action=f"plugin.project_{result}",
            target_type="PluginProject",
            target_id=target_id,
            target_label=target_label,
        )
        return Response({"result": result}, status=status.HTTP_200_OK)


class MyPluginVersionUploadView(APIView):
    permission_classes = [permissions.IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser]

    def post(self, request, project_id):
        project = get_object_or_404(PluginProject, pk=project_id, owner=request.user)
        try:
            version, report, created = upload_plugin_version(project, request.FILES.get("archive"), actor=request.user)
        except (PluginWorkflowError, PluginPackageError) as error:
            return Response({"detail": str(error)}, status=status.HTTP_400_BAD_REQUEST)
        record_audit(request, action="plugin.version_upload", target=version, after={"sha256": version.package_blob.sha256, "created": created, "scan": report})
        return Response({"version": serialize_developer_project(project), "scan": report, "created": created}, status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)


class MyPluginPreviewView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, version_id):
        version = get_object_or_404(PluginVersion, pk=version_id, plugin__owner=request.user)
        try:
            target = create_frontend_preview(version, actor=request.user)
        except PluginWorkflowError as error:
            return Response({"detail": str(error)}, status=status.HTTP_400_BAD_REQUEST)
        preview_session = _sign_preview_session(request.user, version)
        record_audit(request, action="plugin.preview_create", target=version, metadata={"path": str(target)})
        return Response({
            "preview": f"/plugins/preview/{preview_session}",
            "expires_in": int(getattr(settings, "PLUGIN_PREVIEW_SESSION_SECONDS", 600)),
        })


class MyPluginSubmitView(APIView):
    permission_classes = [permissions.IsAuthenticated]
    parser_classes = [JSONParser]

    def post(self, request, version_id):
        version = get_object_or_404(PluginVersion, pk=version_id, plugin__owner=request.user)
        try:
            submission = submit_version(version, actor=request.user)
        except PluginWorkflowError as error:
            return Response({"detail": str(error)}, status=status.HTTP_400_BAD_REQUEST)
        record_audit(request, action="plugin.version_submit", target=version, after={"submission_id": submission.pk, "security_report": submission.security_report})
        return Response({"id": submission.pk, "status": submission.status}, status=status.HTTP_201_CREATED)


class MyPluginSubmissionWithdrawView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, submission_id):
        submission = get_object_or_404(
            PluginSubmission.objects.select_related("plugin_version__plugin"),
            pk=submission_id,
            plugin_version__plugin__owner=request.user,
        )
        try:
            submission = withdraw_submission(submission, actor=request.user)
        except PluginWorkflowError as error:
            return Response({"detail": str(error)}, status=status.HTTP_400_BAD_REQUEST)
        record_audit(request, action="plugin.submission_withdraw", target=submission.plugin_version, after={"submission_id": submission.pk})
        return Response({"id": submission.pk, "status": submission.status})


class MyPluginPreviewSessionView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, preview_session):
        user, payload = _load_signed_user(
            preview_session,
            salt=PREVIEW_SESSION_SALT,
            max_age=int(getattr(settings, "PLUGIN_PREVIEW_SESSION_SECONDS", 600)),
        )
        if request.user.pk != user.pk:
            raise Http404
        version = get_object_or_404(
            PluginVersion.objects.select_related("plugin"),
            pk=payload.get("version_id"),
            plugin__owner=user,
        )
        if payload.get("slug") != version.plugin.slug or payload.get("version") != version.version:
            raise Http404
        if version.review_status not in {PluginVersion.ReviewStatus.DRAFT, PluginVersion.ReviewStatus.REJECTED}:
            raise Http404
        if set(version.runtime_types or []) != {"frontend"}:
            raise Http404
        root = Path(settings.PLUGIN_ROOT) / "previews" / version.plugin.slug / version.version / "frontend"
        if not (root / "plugin.js").is_file():
            raise Http404
        frontend = version.manifest_snapshot.get("frontend") or {}
        prefix = f"/plugin-previews/session/{preview_session}/{version.plugin.slug}/{version.version}"
        return Response({
            "slug": version.plugin.slug,
            "version": version.version,
            "manifest": version.manifest_snapshot,
            "frontendEntry": f"{prefix}/plugin.js",
            "styleEntry": f"{prefix}/plugin.css" if frontend.get("styleEntry") and (root / "plugin.css").is_file() else "",
        })


class StaffPluginReviewQueueView(APIView):
    permission_classes = [StaffCapabilityPermission]
    required_capability = "manage_system"

    def get(self, request):
        rows = PluginSubmission.objects.filter(status=PluginSubmission.Status.SUBMITTED).select_related("plugin_version__plugin", "submitter")
        approved = PluginVersion.objects.filter(
            review_status=PluginVersion.ReviewStatus.APPROVED,
            published_at__isnull=True,
            revoked_at__isnull=True,
        ).select_related("plugin").prefetch_related(
            Prefetch("submissions", to_attr="review_submissions"),
        )
        deployments = PluginDeployment.objects.select_related(
            "plugin", "current_version", "previous_version",
        ).annotate(review_install_count=Count("plugin__user_installations", distinct=True))
        market_versions = PluginVersion.objects.filter(
            published_at__isnull=False,
            revoked_at__isnull=True,
            plugin__installation_mode=PluginProject.InstallationMode.USER,
        ).select_related("plugin").annotate(
            review_install_count=Count("plugin__user_installations", distinct=True),
        ).prefetch_related(
            Prefetch("submissions", to_attr="review_submissions"),
        )
        return Response({
            "submissions": [{
                "id": row.pk, "version_id": row.plugin_version_id,
                "project": row.plugin_version.plugin.slug, "version": row.plugin_version.version,
                "runtime_types": row.plugin_version.runtime_types,
                "submitter": row.submitter.get_username() if row.submitter else "",
                "security_report": row.security_report,
            } for row in rows],
            "approved_versions": [{
                "id": version.pk, "project": version.plugin.slug, "version": version.version,
                "runtime_types": version.runtime_types,
                "security_report": version.review_submissions[0].security_report if version.review_submissions else {},
            } for version in approved],
            "deployments": [{
                "slug": deployment.plugin.slug,
                "name": deployment.plugin.name,
                "version_id": deployment.current_version_id,
                "version": deployment.current_version.version,
                "previous_version": deployment.previous_version.version if deployment.previous_version else None,
                "enabled": deployment.enabled,
                "healthy": deployment.healthy,
                "status": deployment.status,
                "published": bool(deployment.current_version.published_at),
                "revoked": bool(deployment.current_version.revoked_at),
                "install_count": deployment.review_install_count,
                "disk_bytes": deployment.disk_bytes,
                "last_error": deployment.last_error,
            } for deployment in deployments],
            "marketplace_versions": [{
                "id": version.pk,
                "project": version.plugin.slug,
                "name": version.plugin.name,
                "version": version.version,
                "runtime_types": version.runtime_types,
                "published_at": version.published_at,
                "install_count": version.review_install_count,
                "security_report": version.review_submissions[0].security_report if version.review_submissions else {},
            } for version in market_versions],
        })


class StaffPluginReviewActionView(APIView):
    permission_classes = [StaffCapabilityPermission]
    required_capability = "manage_system"
    parser_classes = [JSONParser]

    def post(self, request, submission_id):
        submission = get_object_or_404(PluginSubmission, pk=submission_id)
        try:
            result = review_submission(submission, actor=request.user, approve=bool(request.data.get("approve")), note=str(request.data.get("note", "")))
        except PluginWorkflowError as error:
            return Response({"detail": str(error)}, status=status.HTTP_400_BAD_REQUEST)
        record_audit(request, action="plugin.review_approve" if result.status == PluginSubmission.Status.APPROVED else "plugin.review_reject", target=result.plugin_version, after={"submission_id": result.pk, "note": result.review_note})
        return Response({"id": result.pk, "status": result.status, "review_note": result.review_note})


class StaffPluginPublishView(APIView):
    permission_classes = [StaffCapabilityPermission]
    required_capability = "manage_system"

    def post(self, request, version_id):
        version = get_object_or_404(PluginVersion, pk=version_id)
        if "backend" in set(version.runtime_types or []) and not request.user.is_superuser:
            return Response({"detail": "Backend Runtime 发布必须由超级管理员执行。"}, status=status.HTTP_403_FORBIDDEN)
        try:
            result = PluginPackageInstaller().publish(version, actor=request.user)
        except (PluginInstallError, RuntimeLoadError) as error:
            return Response({"detail": str(error)}, status=status.HTTP_400_BAD_REQUEST)
        record_audit(request, action="plugin.version_publish", target=version, after=result)
        return Response(result)


class StaffPluginUnpublishView(APIView):
    permission_classes = [StaffCapabilityPermission]
    required_capability = "manage_system"

    def post(self, request, version_id):
        version = get_object_or_404(PluginVersion, pk=version_id)
        version.published_at = None
        version.save(update_fields=["published_at"])
        record_audit(request, action="plugin.version_unpublish", target=version)
        return Response({"detail": "版本已从市场隐藏。"})


class StaffPluginRevokeView(APIView):
    permission_classes = [StaffCapabilityPermission]
    required_capability = "manage_system"

    def post(self, request, version_id):
        version = get_object_or_404(PluginVersion, pk=version_id)
        if not request.user.is_superuser:
            return Response({"detail": "撤销运行时代码必须由超级管理员执行。"}, status=status.HTTP_403_FORBIDDEN)
        try:
            PluginPackageInstaller().revoke(version, actor=request.user)
        except PluginWorkflowError as error:
            return Response({"detail": str(error)}, status=status.HTTP_400_BAD_REQUEST)
        record_audit(request, action="plugin.version_revoke", target=version)
        return Response({"detail": "版本已撤销，当前 Runtime 已停用。"})


class PluginPreviewAssetView(APIView):
    permission_classes = [permissions.AllowAny]

    def get(self, request, slug, version, asset, preview_session):
        user, payload = _load_signed_user(
            preview_session,
            salt=PREVIEW_SESSION_SALT,
            max_age=int(getattr(settings, "PLUGIN_PREVIEW_SESSION_SECONDS", 600)),
        )
        version_row = get_object_or_404(
            PluginVersion.objects.select_related("plugin"),
            pk=payload.get("version_id"),
            plugin__slug=slug,
            plugin__owner=user,
            version=version,
        )
        if version_row.review_status not in {PluginVersion.ReviewStatus.DRAFT, PluginVersion.ReviewStatus.REJECTED}:
            raise Http404
        if set(version_row.runtime_types or []) != {"frontend"}:
            raise Http404
        path = (Path(settings.PLUGIN_ROOT) / "previews" / slug / version / "frontend" / asset).resolve()
        root = (Path(settings.PLUGIN_ROOT) / "previews" / slug / version / "frontend").resolve()
        try:
            path.relative_to(root)
        except ValueError:
            raise Http404
        if not path.is_file() or path.is_symlink():
            raise Http404
        response = FileResponse(path.open("rb"), content_type=mimetypes.guess_type(path.name)[0] or "application/octet-stream")
        response["Cache-Control"] = "private, no-store"
        response["Vary"] = "Cookie"
        return response
