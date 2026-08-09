from __future__ import annotations

from django.conf import settings
from django.core import signing

from .errors import sync_preview_expired, sync_preview_invalid

CONFIRMATION_SCHEMA_VERSION = 1
CONFIRMATION_SALT = "journal.external_sync.confirmation.v1"
CONFIRMATION_KEYS = frozenset(
    (
        "schema_version",
        "user_id",
        "provider",
        "entry_id",
        "identity_id",
        "connection_id",
        "external_id",
        "external_user_id",
        "baseline_fingerprint",
        "local_fingerprint",
        "remote_fingerprint",
    )
)


def issue_preview_token(*, user, provider, entry, identity, connection, fingerprints):
    payload = {
        "schema_version": CONFIRMATION_SCHEMA_VERSION,
        "user_id": user.pk,
        "provider": provider.slug,
        "entry_id": entry.pk,
        "identity_id": identity.pk,
        "connection_id": connection.pk,
        "external_id": identity.external_id,
        "external_user_id": connection.external_user_id,
        "baseline_fingerprint": fingerprints["baseline"],
        "local_fingerprint": fingerprints["local"],
        "remote_fingerprint": fingerprints["remote"],
    }
    return signing.dumps(payload, salt=CONFIRMATION_SALT, compress=False)


def decode_preview_token(token):
    try:
        payload = signing.loads(
            token,
            salt=CONFIRMATION_SALT,
            max_age=settings.EXTERNAL_SYNC_CONFIRMATION_MAX_AGE_SECONDS,
        )
    except signing.SignatureExpired as error:
        raise sync_preview_expired() from error
    except (signing.BadSignature, TypeError, ValueError) as error:
        raise sync_preview_invalid() from error
    if (
        not isinstance(payload, dict)
        or set(payload) != CONFIRMATION_KEYS
        or payload.get("schema_version") != CONFIRMATION_SCHEMA_VERSION
    ):
        raise sync_preview_invalid()
    return payload
