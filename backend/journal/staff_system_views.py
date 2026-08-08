import csv
import io
import json
import zipfile
from datetime import timedelta

import requests
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.serializers.json import DjangoJSONEncoder
from django.db import connection, transaction
from django.http import HttpResponse
from django.utils import timezone
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.models import LoginEvent, StaffProfile, UserSecurityProfile
from site_config.media_storage.pool import StoragePoolService
from site_config.models import SiteSettings
from plugin_host.models import PluginInstallation
from plugin_host.registry import PluginRegistryError, discover_plugins

from .auth_tokens import create_refresh_token, no_store, set_refresh_cookie
from .csv_security import safe_csv_value
from .models import AdminAuditLog, Column, JournalEntry, QuickFilter, UserSettings
from .security import (
    build_totp_uri,
    consume_recovery_code,
    generate_recovery_codes,
    generate_totp_secret,
    hash_recovery_codes,
    verify_totp,
)
from .staff_services import (
    StaffCapabilityPermission,
    get_security_profile,
    record_audit,
    record_login_event,
    revoke_user_sessions,
)


User = get_user_model()


class StaffSystemHealthView(APIView):
    permission_classes = [StaffCapabilityPermission]
    required_capability = "manage_system"

    def get(self, request):
        services = []
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
                cursor.fetchone()
            services.append({"key": "database", "label": "数据库", "status": "healthy", "detail": connection.vendor})
        except Exception as error:
            services.append({"key": "database", "label": "数据库", "status": "down", "detail": str(error)})

        site = SiteSettings.load()
        email_ready = site.email_delivery_enabled and site.resend_api_key_source != "none"
        services.append({"key": "email", "label": "邮件服务", "status": "healthy" if email_ready else "warning", "detail": site.get_email_from() if email_ready else "未配置可用发件密钥"})

        try:
            response = requests.get("https://api.bgm.tv/v0/subjects/1", headers={"User-Agent": settings.BANGUMI_USER_AGENT}, timeout=3)
            response.raise_for_status()
            services.append({"key": "bangumi", "label": "Bangumi", "status": "healthy", "detail": f"HTTP {response.status_code}"})
        except requests.RequestException as error:
            services.append({"key": "bangumi", "label": "Bangumi", "status": "down", "detail": str(error)[:180]})

        if settings.DEBUG:
            services.append({"key": "storage", "label": "媒体存储", "status": "healthy", "detail": str(settings.MEDIA_ROOT)})
        else:
            storage_states = StoragePoolService.list_backends()
            writable = [item for item, state in storage_states if state.writable]
            services.append({
                "key": "storage",
                "label": "媒体存储",
                "status": "healthy" if writable else "warning",
                "detail": f"{len(writable)} 个可写 / {len(storage_states)} 个已配置" if storage_states else "尚未配置媒体存储",
            })
        try:
            plugins = discover_plugins()
            errors = sum(bool(item.get("errors")) for item in plugins)
            services.append({"key": "plugins", "label": "插件系统", "status": "warning" if errors else "healthy", "detail": f"{len(plugins)} 个插件，{errors} 个异常"})
        except (PluginRegistryError, OSError, ValueError) as error:
            services.append({"key": "plugins", "label": "插件系统", "status": "down", "detail": str(error)})
        return Response({"checked_at": timezone.now(), "services": services})


class StaffBackupView(APIView):
    permission_classes = [StaffCapabilityPermission]
    required_capability = "backup_data"

    def get(self, request):
        export_format = request.query_params.get("export_format", "zip")
        kind = request.query_params.get("kind", "all")
        datasets = {
            "users": list(User.objects.values("id", "username", "email", "is_active", "is_staff", "is_superuser", "last_login", "date_joined")),
            "entries": list(JournalEntry.objects.values()),
            "columns": [{**item, "entries": list(Column.objects.get(pk=item["id"]).entries.values_list("id", flat=True))} for item in Column.objects.values()],
            "user_settings": list(UserSettings.objects.values()),
            "quick_filters": list(QuickFilter.objects.values()),
            "staff_roles": list(StaffProfile.objects.values("user_id", "role", "updated_at")),
            "plugin_installations": list(PluginInstallation.objects.values("plugin_id", "slug", "current_version", "enabled", "healthy", "config", "updated_at")),
            "audit": list(AdminAuditLog.objects.values())[:10000],
        }
        record_audit(request, action="system.backup_export", target_type="system", target_label=kind, metadata={"format": export_format})
        stamp = timezone.localtime().strftime("%Y%m%d-%H%M%S")
        if export_format == "csv":
            rows = datasets.get(kind)
            if rows is None:
                return Response({"detail": "CSV 导出必须选择具体数据类型。"}, status=status.HTTP_400_BAD_REQUEST)
            output = io.StringIO()
            fields = sorted({key for row in rows for key in row}) if rows else ["empty"]
            writer = csv.DictWriter(output, fieldnames=fields)
            writer.writeheader()
            for row in rows:
                writer.writerow({field: safe_csv_value(row.get(field)) for field in fields})
            response = HttpResponse(output.getvalue(), content_type="text/csv; charset=utf-8")
            response["Content-Disposition"] = f'attachment; filename="anime-journal-{kind}-{stamp}.csv"'
            return no_store(response)

        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("manifest.json", json.dumps({"version": settings.ANIME_JOURNAL_VERSION, "created_at": timezone.now(), "scope": "safe-admin-backup", "contains_passwords": False}, cls=DjangoJSONEncoder, ensure_ascii=False, indent=2))
            selected = datasets if kind == "all" else {kind: datasets.get(kind, [])}
            for name, rows in selected.items():
                archive.writestr(f"{name}.json", json.dumps(rows, cls=DjangoJSONEncoder, ensure_ascii=False, indent=2))
        response = HttpResponse(buffer.getvalue(), content_type="application/zip")
        response["Content-Disposition"] = f'attachment; filename="anime-journal-backup-{stamp}.zip"'
        return no_store(response)


class StaffTwoFactorView(APIView):
    permission_classes = [StaffCapabilityPermission]
    required_capability = "view_audit"
    throttle_scope = "two_factor"
    account_throttle_scope = "two_factor"

    @staticmethod
    def _response(user, payload, *, status_code=status.HTTP_200_OK, rotate_session=False):
        response = Response(payload, status=status_code)
        if rotate_session:
            refresh = create_refresh_token(user)
            response.data["access"] = str(refresh.access_token)
            set_refresh_cookie(response, refresh)
        return no_store(response)

    @staticmethod
    def _invalid():
        return no_store(Response({"detail": "当前密码或验证码不正确。"}, status=status.HTTP_400_BAD_REQUEST))

    def get(self, request):
        profile = get_security_profile(request.user)
        expires_at = profile.pending_totp_created_at + timedelta(minutes=10) if profile.pending_totp_created_at else None
        return self._response(request.user, {
            "enabled": profile.two_factor_enabled,
            "has_pending_secret": bool(profile.pending_totp_secret_encrypted),
            "pending_expires_at": expires_at,
            "recovery_codes_remaining": len(profile.recovery_code_hashes or []),
        })

    def post(self, request):
        profile = get_security_profile(request.user)
        action = request.data.get("action")
        if action == "begin":
            if not request.user.check_password(str(request.data.get("password", ""))):
                return self._invalid()
            if profile.two_factor_enabled and not verify_totp(profile.get_totp_secret(), request.data.get("current_code")):
                return self._invalid()
            secret = generate_totp_secret()
            profile.set_pending_totp_secret(secret)
            profile.pending_totp_created_at = timezone.now()
            profile.save(update_fields=["pending_totp_secret_encrypted", "pending_totp_created_at", "updated_at"])
            return self._response(request.user, {
                "secret": secret,
                "otpauth_uri": build_totp_uri(secret, request.user.email or request.user.username, SiteSettings.load().site_name),
                "expires_in": 600,
            })
        if action == "confirm":
            with transaction.atomic():
                profile = UserSecurityProfile.objects.select_for_update().get(pk=profile.pk)
                if (
                    not profile.pending_totp_created_at
                    or timezone.now() - profile.pending_totp_created_at > timedelta(minutes=10)
                ):
                    profile.clear_pending_totp()
                    profile.save(update_fields=["pending_totp_secret_encrypted", "pending_totp_created_at", "updated_at"])
                    return self._response(request.user, {"detail": "二维码已过期，请重新生成。"}, status_code=status.HTTP_400_BAD_REQUEST)
                pending_secret = profile.get_pending_totp_secret()
                if not verify_totp(pending_secret, request.data.get("code")):
                    return self._response(request.user, {"detail": "验证码不正确。"}, status_code=status.HTTP_400_BAD_REQUEST)
                recovery_codes = generate_recovery_codes()
                profile.set_totp_secret(pending_secret)
                profile.two_factor_enabled = True
                profile.recovery_code_hashes = hash_recovery_codes(recovery_codes)
                profile.clear_pending_totp()
                profile.save(update_fields=[
                    "totp_secret_encrypted", "two_factor_enabled", "recovery_code_hashes",
                    "pending_totp_secret_encrypted", "pending_totp_created_at", "updated_at",
                ])
                revoke_user_sessions(request.user)
            record_login_event(request, event_type=LoginEvent.EventType.TWO_FACTOR_ENABLED, success=True, user=request.user)
            record_audit(request, action="security.two_factor_enabled", target=request.user)
            return self._response(request.user, {
                "detail": "两步验证已启用。",
                "enabled": True,
                "recovery_codes": recovery_codes,
            }, rotate_session=True)
        if action == "disable":
            if not request.user.check_password(str(request.data.get("password", ""))):
                return self._invalid()
            with transaction.atomic():
                profile = UserSecurityProfile.objects.select_for_update().get(pk=profile.pk)
                code = request.data.get("code")
                valid_totp = verify_totp(profile.get_totp_secret(), code)
                valid_recovery = False
                if not valid_totp:
                    valid_recovery = consume_recovery_code(profile, request.data.get("recovery_code"), save=False)
                if profile.two_factor_enabled and not (valid_totp or valid_recovery):
                    return self._invalid()
                profile.two_factor_enabled = False
                profile.totp_secret_encrypted = ""
                profile.recovery_code_hashes = []
                profile.clear_pending_totp()
                profile.save(update_fields=[
                    "two_factor_enabled", "totp_secret_encrypted", "recovery_code_hashes",
                    "pending_totp_secret_encrypted", "pending_totp_created_at", "updated_at",
                ])
                revoke_user_sessions(request.user)
            record_login_event(request, event_type=LoginEvent.EventType.TWO_FACTOR_DISABLED, success=True, user=request.user)
            record_audit(request, action="security.two_factor_disabled", target=request.user)
            return self._response(request.user, {"detail": "两步验证已关闭。", "enabled": False}, rotate_session=True)
        if action == "regenerate":
            if (
                not profile.two_factor_enabled
                or not request.user.check_password(str(request.data.get("password", "")))
                or not verify_totp(profile.get_totp_secret(), request.data.get("code"))
            ):
                return self._invalid()
            with transaction.atomic():
                profile = UserSecurityProfile.objects.select_for_update().get(pk=profile.pk)
                recovery_codes = generate_recovery_codes()
                profile.recovery_code_hashes = hash_recovery_codes(recovery_codes)
                profile.save(update_fields=["recovery_code_hashes", "updated_at"])
                revoke_user_sessions(request.user)
            record_audit(request, action="security.recovery_codes_regenerated", target=request.user)
            return self._response(request.user, {
                "detail": "恢复码已重新生成，旧恢复码已失效。",
                "recovery_codes": recovery_codes,
            }, rotate_session=True)
        return self._response(request.user, {"detail": "不支持的两步验证操作。"}, status_code=status.HTTP_400_BAD_REQUEST)
