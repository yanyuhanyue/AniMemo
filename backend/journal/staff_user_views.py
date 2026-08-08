from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.contrib.auth.tokens import default_token_generator
from django.core.exceptions import ValidationError as DjangoValidationError
from django.shortcuts import get_object_or_404
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.models import LoginEvent, StaffProfile
from site_config.models import SiteSettings

from .emails import EmailDeliveryError, send_transactional_email
from .staff_common import _column_data, _entry_data, _journal_data, _require_sensitive_reauthentication, _user_data, _validation_detail
from .staff_services import (
    StaffCapabilityPermission,
    USER_MANAGEMENT_DENIED_DETAIL,
    assert_can_manage_user,
    get_security_profile,
    get_staff_role,
    record_audit,
    record_login_event,
    revoke_user_sessions,
    update_user_password,
)


User = get_user_model()


class StaffUserDetailView(APIView):
    permission_classes = [StaffCapabilityPermission]
    required_capability = "manage_users"

    def get(self, request, pk):
        user = get_object_or_404(User.objects.select_related("journal_settings", "security_profile", "staff_profile"), pk=pk)
        try:
            assert_can_manage_user(request, user, action="view_security")
        except DjangoValidationError as error:
            return Response({"detail": _validation_detail(error)}, status=status.HTTP_403_FORBIDDEN)
        return Response({
            **_user_data(user, request.user),
            "settings": _journal_data(request, user.journal_settings, detail=False) if hasattr(user, "journal_settings") else None,
            "entries": [_entry_data(request, item) for item in user.journal_entries.filter(deleted_at__isnull=True)[:20]],
            "columns": [_column_data(request, item) for item in user.columns.filter(deleted_at__isnull=True)[:20]],
            "login_events": [{
                "id": item.id,
                "event_type": item.event_type,
                "event_display": item.get_event_type_display(),
                "success": item.success,
                "ip_address": item.ip_address,
                "user_agent": item.user_agent,
                "created_at": item.created_at,
            } for item in user.login_events.all()[:20]],
        })


class StaffUserActionView(APIView):
    permission_classes = [StaffCapabilityPermission]
    required_capability = "manage_users"
    throttle_scope = "two_factor"
    account_throttle_scope = "two_factor"
    throttle_account_fields = ("target",)

    def post(self, request, pk, action):
        user = get_object_or_404(User, pk=pk)
        try:
            assert_can_manage_user(request, user, action=action)
        except DjangoValidationError as error:
            return Response({"detail": _validation_detail(error)}, status=status.HTTP_403_FORBIDDEN)
        if action == "force-logout":
            try:
                _require_sensitive_reauthentication(request, user, action)
            except DjangoValidationError as error:
                return Response({"detail": _validation_detail(error)}, status=status.HTTP_403_FORBIDDEN)
            version = revoke_user_sessions(user)
            record_login_event(request, event_type=LoginEvent.EventType.FORCE_LOGOUT, success=True, user=user)
            record_audit(request, action="user.force_logout", target=user, after={"session_version": version})
            return Response({"detail": "该用户的全部登录会话已失效。", "session_version": version})
        if action == "reset-password":
            try:
                _require_sensitive_reauthentication(request, user, action)
            except DjangoValidationError as error:
                return Response({"detail": _validation_detail(error)}, status=status.HTTP_403_FORBIDDEN)
            password = str(request.data.get("password", ""))
            confirm = str(request.data.get("password_confirm", ""))
            if password != confirm:
                return Response({"password_confirm": ["两次输入的密码不一致。"]}, status=status.HTTP_400_BAD_REQUEST)
            try:
                validate_password(password, user=user)
            except DjangoValidationError as error:
                return Response({"password": error.messages}, status=status.HTTP_400_BAD_REQUEST)
            version = update_user_password(user, password)
            record_audit(request, action="user.password_reset", target=user, after={"session_version": version})
            return Response({"detail": "密码已更新，所有设备需要重新登录。", "session_version": version})
        if action == "resend-activation":
            profile = get_security_profile(user)
            if profile.email_verified:
                return Response({"detail": "该邮箱已经完成验证。"}, status=status.HTTP_409_CONFLICT)
            if not user.email:
                return Response({"detail": "该用户没有可用邮箱。"}, status=status.HTTP_400_BAD_REQUEST)
            uid = urlsafe_base64_encode(force_bytes(user.pk))
            token = default_token_generator.make_token(user)
            verify_url = request.build_absolute_uri(f"/api/auth/verify-email/?uid={uid}&token={token}")
            try:
                send_transactional_email(
                    to=user.email,
                    subject=f"激活你的 {SiteSettings.load().site_name} 手账",
                    html=f'<h1>账号激活</h1><p><a href="{verify_url}">点击激活账号</a></p>',
                    text=f"请打开以下链接激活账号：{verify_url}",
                )
            except EmailDeliveryError as error:
                return Response({"detail": str(error)}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
            record_audit(request, action="user.resend_activation", target=user)
            return Response({"detail": "激活邮件已重新发送。"})
        if action == "role":
            if not request.user.is_superuser or user.is_superuser:
                return Response({"detail": USER_MANAGEMENT_DENIED_DETAIL}, status=status.HTTP_403_FORBIDDEN)
            role = request.data.get("role")
            if role not in dict(StaffProfile.Role.choices):
                return Response({"detail": "不支持的后台角色。"}, status=status.HTTP_400_BAD_REQUEST)
            try:
                _require_sensitive_reauthentication(request, user, action, force=True)
            except DjangoValidationError as error:
                return Response({"detail": _validation_detail(error)}, status=status.HTTP_403_FORBIDDEN)
            before = {"is_staff": user.is_staff, "role": get_staff_role(user) if user.is_staff else "user"}
            user.is_staff = True
            user.save(update_fields=["is_staff"])
            profile, _ = StaffProfile.objects.get_or_create(
                user=user,
                defaults={"role": role, "updated_by": request.user},
            )
            profile.role = role
            profile.updated_by = request.user
            profile.save(update_fields=["role", "updated_by", "updated_at"])
            record_audit(request, action="user.role_change", target=user, before=before, after={"is_staff": True, "role": role})
            revoke_user_sessions(user)
            return Response(_user_data(user, request.user))
        return Response({"detail": "不支持的用户操作。"}, status=status.HTTP_404_NOT_FOUND)

