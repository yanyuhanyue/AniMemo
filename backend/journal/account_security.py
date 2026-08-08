from dataclasses import dataclass

from django.contrib.auth import get_user_model
from django.db import transaction
from rest_framework.exceptions import AuthenticationFailed

from .auth_service import verify_staff_second_factor
from .auth_tokens import revoke_current_access_token
from accounts.models import UserSecurityProfile
from plugin_host.sdk import UserHookContext, run_filter, run_hook
from .staff_services import record_audit, revoke_user_sessions


User = get_user_model()


@dataclass
class AccountDeletionError(Exception):
    detail: str
    reason: str
    field: str = "detail"

    def __str__(self):
        return self.detail


def delete_current_account(*, user, current_password, otp="", recovery_code="", request=None):
    with transaction.atomic():
        locked_superusers = []
        if getattr(user, "is_superuser", False):
            # Lock the complete active-superuser set in a stable order first so two self-deletes
            # cannot deadlock while checking the final-admin boundary.
            locked_superusers = list(
                User.objects.select_for_update()
                .filter(is_superuser=True, is_staff=True, is_active=True)
                .order_by("pk")
            )
        try:
            locked_user = User.objects.select_for_update().get(pk=user.pk)
        except User.DoesNotExist as error:
            raise AccountDeletionError("账户已经不存在。", "account_missing") from error

        if not locked_user.check_password(str(current_password or "")):
            raise AccountDeletionError("当前密码不正确。", "password_invalid", "current_password")

        is_staff_account = bool(locked_user.is_staff or locked_user.is_superuser)
        second_factor_method = "none"
        if is_staff_account:
            profile, _created = UserSecurityProfile.objects.select_for_update().get_or_create(user=locked_user)
            if not profile.two_factor_enabled:
                raise AccountDeletionError(
                    "工作人员必须先启用两步验证，才能永久删除账号。",
                    "two_factor_not_enabled",
                    "otp",
                )
            try:
                used_recovery_code, _remaining = verify_staff_second_factor(
                    profile,
                    otp=otp,
                    recovery_code=recovery_code,
                )
            except AuthenticationFailed as error:
                raise AccountDeletionError(
                    "当前密码或二次验证码不正确。",
                    "second_factor_invalid",
                    "otp",
                ) from error
            second_factor_method = "recovery_code" if used_recovery_code else "totp"

        if locked_user.is_superuser:
            active_superuser_ids = [item.pk for item in locked_superusers]
            if locked_user.pk in active_superuser_ids and len(active_superuser_ids) <= 1:
                raise AccountDeletionError(
                    "不能删除最后一个有效超级管理员。",
                    "last_active_superuser",
                )

        snapshot = {
            "user_id": str(locked_user.pk),
            "username": locked_user.get_username(),
            "email": locked_user.email,
            "is_staff": locked_user.is_staff,
            "is_superuser": locked_user.is_superuser,
        }
        hook_context = UserHookContext(
            user_id=locked_user.pk,
            actor_id=getattr(getattr(request, "user", None), "pk", None) if request is not None else None,
            source="account-delete",
        )
        try:
            allowed = run_filter("user.before_delete", True, hook_context)
        except Exception as error:
            raise AccountDeletionError("账户删除策略拒绝本次操作。", "before_delete_hook_failed") from error
        if allowed is False or (
            isinstance(allowed, dict)
            and (allowed.get("allow") is False or allowed.get("deny") is True)
        ):
            raise AccountDeletionError("账户删除策略拒绝本次操作。", "before_delete_hook_denied")

        revoke_current_access_token(request) if request is not None else None
        revoke_user_sessions(locked_user)
        if request is not None:
            record_audit(
                request,
                action="security.account_deleted",
                target=locked_user,
                before=snapshot,
                after={"deleted": True},
                metadata={"second_factor_method": second_factor_method},
            )
        locked_user.delete()
        run_hook("user.after_delete", hook_context)
        return snapshot
