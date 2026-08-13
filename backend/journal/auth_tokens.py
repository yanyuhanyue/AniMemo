import hmac
import re
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
from site_config.models import InstallationState
from .staff_services import get_security_profile


INSTALLATION_INSTANCE_CLAIM = "ii"
INSTALLATION_BINDING_PATTERN = re.compile(r"[0-9a-f]{64}\Z")


class RefreshReplayError(TokenError):
    def __init__(self, *, user, jti):
        super().__init__("Refresh token 已被使用或撤销。")
        self.user = user
        self.jti = str(jti)


def current_installation_binding():
    try:
        installation = InstallationState.objects.only(
            "status",
            "authentication_epoch",
        ).get(pk=1)
    except InstallationState.DoesNotExist as error:
        raise AuthenticationFailed(
            "登录会话已失效，请重新登录。",
            code="session_revoked",
        ) from error

    binding = installation.authentication_epoch
    if (
        installation.status != InstallationState.Status.INITIALIZED
        or not isinstance(binding, str)
        or not INSTALLATION_BINDING_PATTERN.fullmatch(binding)
    ):
        raise AuthenticationFailed(
            "登录会话已失效，请重新登录。",
            code="session_revoked",
        )
    return binding


def bind_token_to_current_installation(token):
    token[INSTALLATION_INSTANCE_CLAIM] = current_installation_binding()
    return token


def validate_token_installation(token):
    token_binding = token.get(INSTALLATION_INSTANCE_CLAIM)
    current_binding = current_installation_binding()
    if not isinstance(token_binding, str) or not hmac.compare_digest(
        token_binding,
        current_binding,
    ):
        raise AuthenticationFailed(
            "登录会话已失效，请重新登录。",
            code="session_revoked",
        )


def create_refresh_token(user):
    installation_binding = current_installation_binding()
    refresh = RefreshToken.for_user(user)
    refresh["sv"] = get_security_profile(user).session_version
    refresh[INSTALLATION_INSTANCE_CLAIM] = installation_binding
    return refresh


def issue_token_pair(user):
    refresh = create_refresh_token(user)
    user.last_login = timezone.now()
    user.save(update_fields=["last_login"])
    return refresh, refresh.access_token


def revoke_access_token(raw_token):
    raw_token = str(raw_token or "").strip()
    if not raw_token:
        return False
    try:
        token = AccessToken(raw_token)
        validate_token_installation(token)
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
    except (
        AuthenticationFailed,
        TokenError,
        ValueError,
        TypeError,
        OverflowError,
        get_user_model().DoesNotExist,
    ):
        return False


def user_from_refresh(refresh):
    validate_token_installation(refresh)
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


def rotate_refresh(raw_refresh):
    verified = UntypedToken(raw_refresh)
    if verified.get("token_type") != "refresh":
        raise InvalidToken("刷新凭据无效。")
    jti = verified.get("jti")
    if not jti:
        raise InvalidToken("刷新凭据无效。")

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
        return user, access, rotated
