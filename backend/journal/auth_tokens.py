import hashlib
from datetime import datetime, timezone as dt_timezone

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework_simplejwt.exceptions import AuthenticationFailed, InvalidToken
from rest_framework_simplejwt.settings import api_settings
from rest_framework_simplejwt.token_blacklist.models import BlacklistedToken, OutstandingToken
from rest_framework_simplejwt.tokens import AccessToken, RefreshToken, UntypedToken
from rest_framework_simplejwt.exceptions import TokenError

from accounts.models import RevokedAccessToken
from .staff_services import get_security_profile, record_audit


class RefreshReplayError(TokenError):
    def __init__(self, *, user, jti):
        super().__init__("Refresh token 已被使用或撤销。")
        self.user = user
        self.jti = str(jti)


def create_refresh_token(user):
    refresh = RefreshToken.for_user(user)
    refresh["sv"] = get_security_profile(user).session_version
    return refresh


def issue_token_pair(user):
    refresh = create_refresh_token(user)
    user.last_login = timezone.now()
    user.save(update_fields=["last_login"])
    return refresh, refresh.access_token


def revoke_current_access_token(request):
    header = str(request.META.get("HTTP_AUTHORIZATION") or "")
    if not header.lower().startswith("bearer "):
        return False
    raw_token = header[7:].strip()
    if not raw_token:
        return False
    try:
        token = AccessToken(raw_token)
        jti = token.get("jti")
        user_id = token.get(api_settings.USER_ID_CLAIM)
        exp = token.get("exp")
        if not jti or user_id is None or not exp:
            return False
        user = get_user_model().objects.get(pk=user_id)
        expires_at = datetime.fromtimestamp(int(exp), tz=dt_timezone.utc)
        if expires_at <= timezone.now():
            return False
        RevokedAccessToken.objects.get_or_create(
            jti=str(jti),
            defaults={"user": user, "expires_at": expires_at},
        )
        return True
    except (TokenError, ValueError, TypeError, OverflowError, get_user_model().DoesNotExist):
        return False


def refresh_cookie_options():
    return {
        "path": settings.REFRESH_COOKIE_PATH,
        "domain": settings.REFRESH_COOKIE_DOMAIN,
        "samesite": settings.REFRESH_COOKIE_SAMESITE,
        "secure": settings.REFRESH_COOKIE_SECURE,
    }


def set_refresh_cookie(response, refresh):
    response.set_cookie(
        key=settings.REFRESH_COOKIE_NAME,
        value=str(refresh),
        httponly=True,
        max_age=int(settings.SIMPLE_JWT["REFRESH_TOKEN_LIFETIME"].total_seconds()),
        **refresh_cookie_options(),
    )
    return response


def clear_refresh_cookie(response):
    options = refresh_cookie_options()
    options.pop("secure", None)
    response.delete_cookie(
        key=settings.REFRESH_COOKIE_NAME,
        **options,
    )
    return response


def no_store(response):
    response["Cache-Control"] = "no-store"
    response["Pragma"] = "no-cache"
    response["X-Content-Type-Options"] = "nosniff"
    return response


def user_from_refresh(refresh):
    user_id = refresh.get(api_settings.USER_ID_CLAIM)
    if user_id is None:
        raise InvalidToken("刷新凭据无效。")
    try:
        user = get_user_model().objects.get(**{api_settings.USER_ID_FIELD: user_id}, is_active=True)
    except get_user_model().DoesNotExist as error:
        raise AuthenticationFailed("登录会话已失效，请重新登录。", code="session_revoked") from error
    token_version = refresh.get("sv")
    if token_version is None or int(token_version) != get_security_profile(user).session_version:
        raise AuthenticationFailed("登录会话已失效，请重新登录。", code="session_revoked")
    return user


def rotate_refresh(raw_refresh, *, request=None):
    verified = UntypedToken(raw_refresh)
    if verified.get("token_type") != "refresh":
        raise InvalidToken("刷新凭据无效。")
    jti = verified.get("jti")
    if not jti:
        raise InvalidToken("刷新凭据无效。")

    try:
        with transaction.atomic():
            try:
                outstanding = (
                    OutstandingToken.objects.select_for_update()
                    .get(jti=str(jti))
                )
            except OutstandingToken.DoesNotExist as error:
                raise InvalidToken("刷新凭据无效。") from error

            refresh = RefreshToken(raw_refresh, verify=False)
            user = user_from_refresh(refresh)
            if outstanding.user_id != user.pk:
                raise InvalidToken("刷新凭据无效。")
            if BlacklistedToken.objects.filter(token=outstanding).exists():
                raise RefreshReplayError(user=user, jti=jti)

            rotated = None
            if settings.SIMPLE_JWT.get("ROTATE_REFRESH_TOKENS"):
                if settings.SIMPLE_JWT.get("BLACKLIST_AFTER_ROTATION"):
                    try:
                        with transaction.atomic():
                            BlacklistedToken.objects.create(token=outstanding)
                    except IntegrityError as error:
                        raise RefreshReplayError(user=user, jti=jti) from error
                rotated = create_refresh_token(user)
                access = rotated.access_token
            else:
                access = refresh.access_token
            result = (user, access, rotated)
    except RefreshReplayError as error:
        if request is not None:
            record_audit(
                request,
                action="security.refresh_replay_rejected",
                target=error.user,
                metadata={"token_jti_hash": hashlib.sha256(error.jti.encode("utf-8")).hexdigest()[:16]},
            )
        raise
    return result
