from dataclasses import dataclass

from django.contrib.auth import authenticate, get_user_model
from django.db import transaction
from rest_framework.exceptions import AuthenticationFailed

from accounts.models import UserSecurityProfile
from .security import consume_recovery_code, verify_totp
from .staff_services import get_security_profile


User = get_user_model()


@dataclass
class AuthenticationResult:
    user: object
    account: str
    used_recovery_code: bool = False
    remaining_recovery_codes: int | None = None
    second_factor_verified: bool = False


def verify_staff_second_factor(profile, *, otp="", recovery_code=""):
    """Verify exactly one staff second factor against a locked profile when consuming a code."""
    normalized_otp = str(otp or "").strip()
    normalized_recovery = str(recovery_code or "").strip().upper()
    if normalized_otp and normalized_recovery:
        raise AuthenticationFailed({
            "detail": "只能选择一种二次验证方式。",
            "two_factor_required": True,
        }, code="two_factor_required")
    if not profile.two_factor_enabled or not (normalized_otp or normalized_recovery):
        raise AuthenticationFailed({
            "detail": "用户名、密码或验证码不正确。",
            "two_factor_required": True,
        }, code="two_factor_required")
    if normalized_recovery:
        if not consume_recovery_code(profile, normalized_recovery, save=False):
            raise AuthenticationFailed({
                "detail": "用户名、密码或验证码不正确。",
                "two_factor_required": True,
            }, code="two_factor_required")
        profile.save(update_fields=["recovery_code_hashes", "updated_at"])
        return True, len(profile.recovery_code_hashes or [])
    if not verify_totp(profile.get_totp_secret(), normalized_otp):
        raise AuthenticationFailed({
            "detail": "用户名、密码或验证码不正确。",
            "two_factor_required": True,
        }, code="two_factor_required")
    return False, len(profile.recovery_code_hashes or [])


def _resolve_account(account):
    normalized = str(account or "").strip()
    username = normalized
    matched = None
    if "@" in normalized:
        matched = User.objects.filter(email__iexact=normalized).first()
        if matched:
            username = matched.get_username()
    else:
        matched = User.objects.filter(username__iexact=normalized).first()
        if matched:
            username = matched.get_username()
    return normalized, username, matched


def authenticate_with_second_factor(*, request, username, password, otp="", recovery_code="", staff_only=False):
    account, resolved_username, _matched = _resolve_account(username)
    user = authenticate(request, username=resolved_username, password=str(password or ""))
    if user is None or not user.is_active:
        raise AuthenticationFailed("用户名、密码或验证码不正确。")
    if staff_only and not (user.is_staff or user.is_superuser):
        raise AuthenticationFailed("用户名、密码或验证码不正确。")

    profile = get_security_profile(user)
    if not profile.two_factor_enabled:
        return AuthenticationResult(user=user, account=account)

    if str(recovery_code or "").strip():
        with transaction.atomic():
            locked_profile = UserSecurityProfile.objects.select_for_update().get(pk=profile.pk)
            used_recovery_code, remaining = verify_staff_second_factor(
                locked_profile,
                otp=otp,
                recovery_code=recovery_code,
            )
        return AuthenticationResult(
            user=user,
            account=account,
            used_recovery_code=used_recovery_code,
            remaining_recovery_codes=remaining,
            second_factor_verified=True,
        )

    used_recovery_code, remaining = verify_staff_second_factor(profile, otp=otp, recovery_code=recovery_code)
    return AuthenticationResult(
        user=user,
        account=account,
        used_recovery_code=used_recovery_code,
        remaining_recovery_codes=remaining,
        second_factor_verified=True,
    )
