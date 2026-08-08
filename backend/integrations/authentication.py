import hashlib
import hmac
import re
from dataclasses import dataclass
from time import time

from django.conf import settings
from django.core.cache import cache
from django.utils import timezone
from rest_framework.authentication import BaseAuthentication
from rest_framework.exceptions import AuthenticationFailed

from .crypto import IntegrationSecretError
from .models import IntegrationConnection


SIGNATURE_RE = re.compile(r"^v1=([0-9a-f]{64})$")
TIMESTAMP_RE = re.compile(r"^[0-9]{1,16}$")
NONCE_RE = re.compile(r"^[A-Za-z0-9._~-]{8,128}$")


def canonical_hmac_input(timestamp, nonce, method, path_with_query, body):
    body_digest = hashlib.sha256(body).hexdigest()
    return "\n".join(
        (
            "ANIMEMO-HMAC-V1",
            str(timestamp),
            str(nonce),
            str(method).upper(),
            str(path_with_query),
            body_digest,
        )
    ).encode("utf-8")


def sign_hmac_request(secret, timestamp, nonce, method, path_with_query, body=b""):
    digest = hmac.new(
        str(secret).encode("utf-8"),
        canonical_hmac_input(timestamp, nonce, method, path_with_query, body),
        hashlib.sha256,
    ).hexdigest()
    return f"v1={digest}"


@dataclass(frozen=True)
class IntegrationPrincipal:
    connection_id: object
    is_authenticated: bool = True
    is_active: bool = True


class IntegrationHMACAuthentication(BaseAuthentication):
    keyword = "AniMemo-HMAC-V1"

    def authenticate_header(self, request):
        return self.keyword

    def authenticate(self, request):
        key_id = request.headers.get("X-AniMemo-Key-Id", "")
        timestamp_raw = request.headers.get("X-AniMemo-Timestamp", "")
        nonce = request.headers.get("X-AniMemo-Nonce", "")
        signature_raw = request.headers.get("X-AniMemo-Signature", "")
        signature_match = SIGNATURE_RE.fullmatch(signature_raw)
        if not key_id or not TIMESTAMP_RE.fullmatch(timestamp_raw) or not NONCE_RE.fullmatch(nonce) or not signature_match:
            raise AuthenticationFailed("集成请求认证失败。")

        try:
            connection = IntegrationConnection.objects.get(key_id=key_id, enabled=True)
        except IntegrationConnection.DoesNotExist as error:
            raise AuthenticationFailed("集成请求认证失败。") from error

        timestamp = int(timestamp_raw)
        tolerance = int(getattr(settings, "INTEGRATION_HMAC_TIMESTAMP_TOLERANCE_SECONDS", 300))
        if abs(int(time()) - timestamp) > tolerance:
            raise AuthenticationFailed("集成请求时间戳已失效。")

        try:
            secret = connection.get_secret()
        except IntegrationSecretError as error:
            raise AuthenticationFailed("集成请求认证失败。") from error
        expected = sign_hmac_request(
            secret,
            timestamp_raw,
            nonce,
            request.method,
            request.get_full_path(),
            request.body,
        )
        if not hmac.compare_digest(expected, signature_raw):
            raise AuthenticationFailed("集成请求认证失败。")

        nonce_digest = hashlib.sha256(nonce.encode("ascii")).hexdigest()
        nonce_key = f"integration:hmac:nonce:{connection.pk}:{nonce_digest}"
        nonce_ttl = int(getattr(settings, "INTEGRATION_HMAC_NONCE_TTL_SECONDS", 660))
        if not cache.add(nonce_key, "1", timeout=nonce_ttl):
            raise AuthenticationFailed("集成请求 nonce 已被使用。")

        now = timezone.now()
        IntegrationConnection.objects.filter(pk=connection.pk).update(last_seen_at=now)
        connection.last_seen_at = now
        return IntegrationPrincipal(connection.pk), connection
