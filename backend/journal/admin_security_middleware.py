from urllib.parse import quote

from django.conf import settings
from django.contrib.auth import logout as session_logout
from django.shortcuts import redirect
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from datetime import timedelta

from .staff_services import get_security_profile


STAFF_2FA_USER_KEY = "staff_2fa_verified_user_id"
STAFF_2FA_VERSION_KEY = "staff_2fa_verified_session_version"
STAFF_2FA_AT_KEY = "staff_2fa_verified_at"


def mark_staff_second_factor_verified(request, user, profile=None):
    profile = profile or get_security_profile(user)
    request.session[STAFF_2FA_USER_KEY] = str(user.pk)
    request.session[STAFF_2FA_VERSION_KEY] = profile.session_version
    request.session[STAFF_2FA_AT_KEY] = timezone.now().isoformat()
    request.session.modified = True


def clear_staff_second_factor(request):
    for key in (STAFF_2FA_USER_KEY, STAFF_2FA_VERSION_KEY, STAFF_2FA_AT_KEY):
        request.session.pop(key, None)
    request.session.modified = True


def staff_login_redirect(request, *, next_path=None):
    target = str(next_path or request.get_full_path() or "/admin/")
    if not target.startswith("/admin/") and target != "/admin":
        target = "/admin/"
    login_url = f"{settings.FRONTEND_URL}{settings.ADMIN_LOGIN_PATH}"
    return redirect(f"{login_url}?next={quote(target, safe='/')}")


class AdminSecondFactorMiddleware:
    """Require a project-issued, version-bound second-factor session for Django Admin."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        path = request.path_info or ""
        if not (path == "/admin" or path.startswith("/admin/")):
            return self.get_response(request)

        if path.rstrip("/") == "/admin/logout":
            clear_staff_second_factor(request)
            session_logout(request)
            return redirect(f"{settings.FRONTEND_URL}{settings.ADMIN_LOGIN_PATH}")

        user = request.user
        if not user.is_authenticated:
            return staff_login_redirect(request)
        if not user.is_active or not user.is_staff:
            session_logout(request)
            return staff_login_redirect(request)

        profile = get_security_profile(user)
        verified_user_id = request.session.get(STAFF_2FA_USER_KEY)
        verified_version = request.session.get(STAFF_2FA_VERSION_KEY)
        verified_at_raw = request.session.get(STAFF_2FA_AT_KEY)
        verified_at = parse_datetime(str(verified_at_raw or ""))
        if verified_at and timezone.is_naive(verified_at):
            verified_at = timezone.make_aware(verified_at, timezone.get_current_timezone())
        verified_at_valid = bool(
            verified_at
            and timezone.now() - verified_at <= timedelta(seconds=settings.ADMIN_2FA_SESSION_MAX_AGE)
        )
        valid = (
            profile.two_factor_enabled
            and str(verified_user_id) == str(user.pk)
            and str(verified_version) == str(profile.session_version)
            and verified_at_valid
        )
        if not valid:
            clear_staff_second_factor(request)
            session_logout(request)
            return staff_login_redirect(request)

        if path.rstrip("/") == "/admin/login":
            return redirect("admin:index")
        return self.get_response(request)
