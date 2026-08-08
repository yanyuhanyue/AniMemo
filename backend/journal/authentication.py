from rest_framework.exceptions import AuthenticationFailed
from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework_simplejwt.exceptions import InvalidToken
from django.utils import timezone

from accounts.models import RevokedAccessToken
from .staff_services import get_security_profile


class SessionVersionJWTAuthentication(JWTAuthentication):
    def get_user(self, validated_token):
        user = super().get_user(validated_token)
        if "sv" not in validated_token:
            raise AuthenticationFailed("登录会话已失效，请重新登录。", code="session_revoked")
        if not validated_token.get("jti"):
            raise InvalidToken("Token has no jti.")
        try:
            token_version = int(validated_token["sv"])
        except (TypeError, ValueError) as error:
            raise AuthenticationFailed("登录会话已失效，请重新登录。", code="session_revoked") from error
        if token_version != get_security_profile(user).session_version:
            raise AuthenticationFailed("登录会话已失效，请重新登录。", code="session_revoked")
        if RevokedAccessToken.objects.filter(
            jti=str(validated_token["jti"]),
            expires_at__gt=timezone.now(),
        ).exists():
            raise InvalidToken("Token has been revoked.")
        return user
