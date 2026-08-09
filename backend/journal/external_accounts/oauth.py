import hashlib
import secrets
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from journal.models import ExternalAccountAuthorizationState, UserExternalAccountConnection

from .connections import connect_account, ensure_provider_enabled
from .errors import authorization_state_expired, authorization_state_invalid


def _state_digest(value):
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def start_oauth_authorization(*, user, provider_slug):
    provider = ensure_provider_enabled(provider_slug)
    if not provider.oauth_available():
        from .errors import account_not_configured

        raise account_not_configured()
    now = timezone.now()
    ExternalAccountAuthorizationState.objects.filter(expires_at__lt=now).delete()
    raw_state = secrets.token_urlsafe(32)
    ExternalAccountAuthorizationState.objects.create(
        user=user,
        provider=provider.slug,
        state_digest=_state_digest(raw_state),
        expires_at=now + timedelta(seconds=int(settings.EXTERNAL_ACCOUNT_OAUTH_STATE_TTL_SECONDS)),
    )
    return provider.authorization_url(raw_state)


def _consume_authorization_state(provider_slug, raw_state):
    digest = _state_digest(raw_state)
    now = timezone.now()
    with transaction.atomic():
        state = ExternalAccountAuthorizationState.objects.select_for_update().select_related("user").filter(
            provider=provider_slug,
            state_digest=digest,
        ).first()
        if state is None or state.consumed_at is not None:
            raise authorization_state_invalid()
        if state.expires_at <= now:
            raise authorization_state_expired()
        state.consumed_at = now
        state.save(update_fields=["consumed_at"])
        return state.user


def complete_oauth_authorization(*, provider_slug, code, state):
    provider = ensure_provider_enabled(provider_slug)
    if not provider.oauth_available():
        from .errors import account_not_configured

        raise account_not_configured()
    code = str(code or "").strip()
    state = str(state or "").strip()
    if not code or len(code) > 512 or not state or len(state) > 512:
        raise authorization_state_invalid()
    user = _consume_authorization_state(provider.slug, state)
    credentials = provider.exchange_code(code, state)
    return connect_account(
        user=user,
        provider_slug=provider.slug,
        credentials=credentials,
        auth_method=UserExternalAccountConnection.AuthMethod.OAUTH,
        expires_in=credentials["expires_in"],
    )
