import logging

import requests
from django.conf import settings
from rest_framework import status
from rest_framework.response import Response

from .network import client_ip


logger = logging.getLogger(__name__)
TURNSTILE_VERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"


def verify_turnstile(request, token):
    """Redeem a browser token and fail closed when Turnstile is enabled."""
    if not getattr(settings, "TURNSTILE_ENABLED", True):
        return True

    secret = str(getattr(settings, "TURNSTILE_SECRET", "") or "").strip()
    response_token = str(token or "").strip()
    if not secret or not response_token:
        return False

    try:
        response = requests.post(
            TURNSTILE_VERIFY_URL,
            data={
                "secret": secret,
                "response": response_token,
                "remoteip": client_ip(request) or "",
            },
            timeout=(3, 8),
        )
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError, TypeError):
        logger.warning("Turnstile siteverify request failed", exc_info=True)
        return False

    return isinstance(payload, dict) and payload.get("success") is True


def require_turnstile(request):
    if verify_turnstile(request, request.data.get("cf-turnstile-response")):
        return None
    return Response({"code": "turnstile_failed", "detail": "安全验证失败，请完成验证后重试。"}, status=status.HTTP_403_FORBIDDEN)
