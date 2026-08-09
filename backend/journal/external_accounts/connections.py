from __future__ import annotations

from datetime import timedelta

from config.credentials import CredentialCipher
from django.db import IntegrityError, transaction
from django.utils import timezone

from journal.external_media.services import lock_identity_owner
from journal.models import UserExternalAccountConnection

from .credentials import ExternalAccountCredentialError, decrypt_credentials, encrypt_credentials
from .errors import (
    ExternalAccountError,
    account_already_connected,
    account_identity_mismatch,
    account_not_configured,
    account_not_connected,
    account_token_invalid,
)
from .registry import get_account_provider, iter_account_providers


def provider_capability(provider_slug):
    return dict(get_account_provider(provider_slug).capabilities())


def serialize_connection(connection):
    if connection is None:
        return None
    return {
        "provider": connection.provider,
        "connected": True,
        "auth_method": str(connection.auth_method),
        "external_user_id": connection.external_user_id,
        "username": connection.external_username,
        "display_name": connection.display_name,
        "avatar_url": str(connection.metadata.get("avatar_url") or "") if isinstance(connection.metadata, dict) else "",
        "status": str(connection.status),
        "connected_at": connection.connected_at,
        "verified_at": connection.verified_at,
        "last_used_at": connection.last_used_at,
        "expires_at": connection.expires_at,
    }


def list_account_providers(user):
    connections = {
        connection.provider: connection
        for connection in UserExternalAccountConnection.objects.filter(user=user)
    }
    result = []
    for provider in iter_account_providers():
        capability = dict(provider.capabilities())
        capability["connection"] = serialize_connection(connections.get(provider.slug))
        result.append(capability)
    return result


def ensure_provider_enabled(provider_slug):
    provider = get_account_provider(provider_slug)
    if not provider.enabled():
        raise account_not_configured()
    return provider


def _expires_at(expires_in):
    if not expires_in:
        return None
    return timezone.now() + timedelta(seconds=int(expires_in))


def connect_account(*, user, provider_slug, credentials, auth_method, expires_in=None):
    provider = ensure_provider_enabled(provider_slug)
    access_token = str(credentials.get("access_token") or "").strip()
    profile = provider.verify_account(access_token)
    ciphertext = encrypt_credentials(credentials)
    now = timezone.now()
    try:
        with transaction.atomic():
            locked_user = lock_identity_owner(user)
            connection = UserExternalAccountConnection.objects.select_for_update().filter(
                user=locked_user,
                provider=provider.slug,
            ).first()
            if connection is not None and connection.external_user_id != profile["external_user_id"]:
                raise account_identity_mismatch()
            if connection is None:
                connection = UserExternalAccountConnection(
                    user=locked_user,
                    provider=provider.slug,
                    external_user_id=profile["external_user_id"],
                    connected_at=now,
                )
            connection.auth_method = auth_method
            connection.external_username = profile["external_username"]
            connection.display_name = profile["display_name"]
            connection.credential_ciphertext = ciphertext
            connection.credential_key_version = CredentialCipher.version
            connection.metadata = profile["metadata"]
            connection.status = UserExternalAccountConnection.Status.CONNECTED
            connection.verified_at = now
            connection.last_used_at = now
            connection.expires_at = _expires_at(expires_in)
            connection.save()
            return connection
    except IntegrityError as error:
        raise account_already_connected() from error


def connect_personal_access_token(*, user, provider_slug, access_token):
    token = str(access_token or "").strip()
    return connect_account(
        user=user,
        provider_slug=provider_slug,
        credentials={"access_token": token, "token_type": "Bearer"},
        auth_method=UserExternalAccountConnection.AuthMethod.PERSONAL_ACCESS_TOKEN,
    )


def get_connection(*, user, provider_slug, for_update=False):
    provider = get_account_provider(provider_slug)
    queryset = UserExternalAccountConnection.objects
    if for_update:
        queryset = queryset.select_for_update()
    connection = queryset.filter(user=user, provider=provider.slug).first()
    if connection is None:
        raise account_not_connected()
    return connection


def mark_needs_reauthorization(connection):
    UserExternalAccountConnection.objects.filter(pk=connection.pk).update(
        status=UserExternalAccountConnection.Status.NEEDS_REAUTHORIZATION,
        updated_at=timezone.now(),
    )


def access_token(connection, provider):
    try:
        credentials = decrypt_credentials(connection.credential_ciphertext)
    except ExternalAccountCredentialError as error:
        mark_needs_reauthorization(connection)
        raise account_token_invalid() from error
    refresh_token = credentials.get("refresh_token")
    should_refresh = (
        connection.auth_method == UserExternalAccountConnection.AuthMethod.OAUTH
        and connection.expires_at is not None
        and connection.expires_at <= timezone.now() + timedelta(seconds=30)
    )
    if not should_refresh:
        return credentials["access_token"]
    if not refresh_token or not provider.oauth_available():
        mark_needs_reauthorization(connection)
        raise account_token_invalid()
    try:
        refreshed = provider.refresh_oauth_token(refresh_token)
        profile = provider.verify_account(refreshed["access_token"])
    except ExternalAccountError:
        mark_needs_reauthorization(connection)
        raise
    if profile["external_user_id"] != connection.external_user_id:
        mark_needs_reauthorization(connection)
        raise account_identity_mismatch()
    encrypted = encrypt_credentials(refreshed)
    with transaction.atomic():
        current = UserExternalAccountConnection.objects.select_for_update().get(pk=connection.pk)
        if current.external_user_id != profile["external_user_id"]:
            raise account_identity_mismatch()
        current.credential_ciphertext = encrypted
        current.credential_key_version = CredentialCipher.version
        current.expires_at = _expires_at(refreshed["expires_in"])
        current.external_username = profile["external_username"]
        current.display_name = profile["display_name"]
        current.metadata = profile["metadata"]
        current.status = UserExternalAccountConnection.Status.CONNECTED
        current.verified_at = timezone.now()
        current.last_used_at = current.verified_at
        current.save()
        connection.expires_at = current.expires_at
    return refreshed["access_token"]


def verify_connection(*, user, provider_slug):
    provider = ensure_provider_enabled(provider_slug)
    connection = get_connection(user=user, provider_slug=provider_slug)
    token = access_token(connection, provider)
    try:
        profile = provider.verify_account(token)
    except ExternalAccountError as error:
        if error.detail.get("code") == "external_account_token_invalid":
            mark_needs_reauthorization(connection)
        raise
    if profile["external_user_id"] != connection.external_user_id:
        mark_needs_reauthorization(connection)
        raise account_identity_mismatch()
    now = timezone.now()
    UserExternalAccountConnection.objects.filter(pk=connection.pk).update(
        external_username=profile["external_username"],
        display_name=profile["display_name"],
        metadata=profile["metadata"],
        status=UserExternalAccountConnection.Status.CONNECTED,
        verified_at=now,
        last_used_at=now,
        updated_at=now,
    )
    connection.refresh_from_db()
    return connection


def disconnect_account(*, user, provider_slug):
    with transaction.atomic():
        connection = get_connection(user=user, provider_slug=provider_slug, for_update=True)
        connection.delete()


def connection_token_for_use(*, user, provider_slug):
    connection = get_connection(user=user, provider_slug=provider_slug)
    provider = ensure_provider_enabled(provider_slug)
    return connection, provider, access_token(connection, provider)
