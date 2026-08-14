import logging

import requests

from site_config.turnstile import resolve_turnstile_config


logger = logging.getLogger(__name__)
TURNSTILE_VERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"


def verify_turnstile(token, *, remote_ip=""):
    """Redeem one DB-configured Turnstile token and fail closed."""
    config = resolve_turnstile_config()
    if not config.enabled:
        return True

    if not config.ready:
        return False

    secret = config.secret
    response_token = str(token or "").strip()
    if not secret or not response_token:
        return False

    try:
        response = requests.post(
            TURNSTILE_VERIFY_URL,
            data={
                "secret": secret,
                "response": response_token,
                "remoteip": str(remote_ip or ""),
            },
            timeout=(3, 8),
        )
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError, TypeError):
        logger.warning("Turnstile siteverify request failed", exc_info=True)
        return False

    return isinstance(payload, dict) and payload.get("success") is True
