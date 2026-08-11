from django.conf import settings
from rest_framework import status
from rest_framework.response import Response

from .anti_abuse import challenge_from_payload, verify_anti_abuse_challenge
from .network import client_ip


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


def require_anti_abuse_challenge(request):
    challenge = challenge_from_payload(request.data)
    if verify_anti_abuse_challenge(challenge, remote_ip=client_ip(request) or ""):
        return None
    return Response(
        {"code": "turnstile_failed", "detail": "安全验证失败，请完成验证后重试。"},
        status=status.HTTP_403_FORBIDDEN,
    )
