import json

from django.contrib.auth import get_user_model
from django.contrib.sessions.models import Session
from django.core.exceptions import ValidationError
from django.core.serializers.json import DjangoJSONEncoder
from django.db import transaction
from django.utils import timezone
from rest_framework.permissions import BasePermission
from rest_framework_simplejwt.token_blacklist.models import BlacklistedToken, OutstandingToken

from accounts.models import LoginEvent, RevokedAccessToken, StaffProfile, UserSecurityProfile
from .models import AdminAuditLog
from .network import client_ip


ROLE_CAPABILITIES = {
    StaffProfile.Role.UNASSIGNED: set(),
    StaffProfile.Role.REVIEWER: {"view_dashboard", "moderate_content", "view_audit"},
    StaffProfile.Role.USER_MANAGER: {"view_dashboard", "manage_users", "view_audit"},
    StaffProfile.Role.OPERATOR: {"view_dashboard", "moderate_content", "manage_system", "backup_data", "view_audit"},
    StaffProfile.Role.ADMINISTRATOR: {"view_dashboard", "moderate_content", "manage_users", "manage_system", "backup_data", "view_audit"},
}
ALL_CAPABILITIES = sorted({item for capabilities in ROLE_CAPABILITIES.values() for item in capabilities})
USER_MANAGEMENT_DENIED_DETAIL = "无权操作该管理员账号。"
HIGH_RISK_USER_ACTIONS = {"reset-password", "force-logout", "permissions", "role", "security"}


def resolve_staff_role(user):
    """Resolve staff identity from the account flag and assigned profile."""
    if not user or not getattr(user, "is_authenticated", False):
        return None
    if getattr(user, "is_superuser", False):
        return "superuser"
    if not getattr(user, "is_staff", False):
        return None
    role = StaffProfile.objects.filter(user_id=user.pk).values_list("role", flat=True).first()
    if not role or role == StaffProfile.Role.UNASSIGNED:
        return None
    return role


def staff_capabilities(user):
    if not user or not getattr(user, "is_authenticated", False):
        return []
    if getattr(user, "is_superuser", False):
        return list(ALL_CAPABILITIES)
    role = resolve_staff_role(user)
    if not role:
        return []
    return sorted(ROLE_CAPABILITIES.get(role, set()))


def has_staff_capability(user, capability):
    return bool(
        user
        and getattr(user, "is_authenticated", False)
        and (getattr(user, "is_superuser", False) or capability in staff_capabilities(user))
    )


def can_manage_user(actor, target, *, action=None):
    """Central server-side boundary for all staff account operations."""
    if not actor or not actor.is_authenticated or not has_staff_capability(actor, "manage_users"):
        return False
    if actor.pk == target.pk:
        return False
    if actor.is_superuser:
        return True
    return not target.is_staff and not target.is_superuser


def assert_can_manage_user(request, target, *, action):
    if can_manage_user(request.user, target, action=action):
        return True
    record_audit(
        request,
        action="user.management_denied",
        target=target,
        metadata={"requested_action": action, "reason": "hierarchy"},
    )
    raise ValidationError(USER_MANAGEMENT_DENIED_DETAIL)


def ensure_not_last_active_superuser(target, changes):
    """Prevent concurrent requests from disabling the final usable superuser."""
    if not target.is_superuser:
        return
    demoting = changes.get("is_staff") is False or changes.get("is_active") is False
    if not demoting:
        return
    user_model = get_user_model()
    active_ids = list(
        user_model.objects.select_for_update()
        .filter(is_superuser=True, is_staff=True, is_active=True)
        .order_by("pk")
        .values_list("pk", flat=True)
    )
    if target.pk in active_ids and len(active_ids) <= 1:
        raise ValidationError("不能移除或停用最后一个有效超级管理员。")


class StaffCapabilityPermission(BasePermission):
    def has_permission(self, request, view):
        user = request.user
        return bool(
            user
            and user.is_authenticated
            and has_staff_capability(user, getattr(view, "required_capability", ""))
        )


def get_security_profile(user):
    return UserSecurityProfile.objects.get_or_create(user=user)[0]


def record_login_event(request, *, event_type, success, user=None, account=""):
    return LoginEvent.objects.create(
        user=user,
        account=str(account or (user.get_username() if user else ""))[:254],
        event_type=event_type,
        success=success,
        ip_address=client_ip(request),
        user_agent=request.META.get("HTTP_USER_AGENT", "")[:500],
    )


def record_audit(request, *, action, target=None, target_type="", target_id="", target_label="", before=None, after=None, metadata=None):
    def json_safe(value):
        return json.loads(json.dumps(value or {}, cls=DjangoJSONEncoder))

    if target is not None:
        target_type = target_type or target.__class__.__name__
        target_id = target_id or str(getattr(target, "pk", ""))
        target_label = target_label or str(target)
    return AdminAuditLog.objects.create(
        actor=request.user if getattr(request, "user", None) and request.user.is_authenticated else None,
        action=action,
        target_type=target_type,
        target_id=str(target_id or ""),
        target_label=str(target_label or "")[:300],
        before=json_safe(before),
        after=json_safe(after),
        metadata=json_safe(metadata),
        ip_address=client_ip(request),
        user_agent=request.META.get("HTTP_USER_AGENT", "")[:500],
    )


def revoke_user_sessions(user):
    profile = get_security_profile(user)
    profile.session_version += 1
    profile.save(update_fields=["session_version", "updated_at"])
    for token in OutstandingToken.objects.filter(user=user):
        BlacklistedToken.objects.get_or_create(token=token)
    RevokedAccessToken.objects.filter(user=user).delete()
    for session in Session.objects.filter(expire_date__gte=timezone.now()):
        try:
            if str(session.get_decoded().get("_auth_user_id")) == str(user.pk):
                session.delete()
        except Exception:
            continue
    return profile.session_version


def update_user_password(user, password):
    with transaction.atomic():
        user.set_password(password)
        user.save(update_fields=["password"])
        return revoke_user_sessions(user)
