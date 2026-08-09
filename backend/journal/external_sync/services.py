from __future__ import annotations

from journal.external_accounts.connections import (
    access_token,
    mark_needs_reauthorization,
)
from journal.external_accounts.errors import ExternalAccountError
from journal.external_accounts.registry import get_account_provider
from journal.models import (
    ExternalCollectionSyncState,
    ExternalMediaIdentity,
    JournalEntry,
    UserExternalAccountConnection,
)

from .canonical import local_snapshot
from .errors import (
    external_account_needs_reauthorization,
    sync_context_changed,
    sync_target_not_found,
    sync_value_unsupported,
)
from .planner import plan_collection


def _needs_reauthorization(error):
    return isinstance(error, ExternalAccountError) and error.detail.get("code") == "external_account_token_invalid"


def _resolve_context(*, user, provider_slug, entry_id):
    provider = get_account_provider(provider_slug)
    entry = JournalEntry.objects.filter(pk=entry_id, user=user, deleted_at__isnull=True).first()
    if entry is None:
        raise sync_target_not_found()
    identity = ExternalMediaIdentity.objects.filter(entry=entry, provider=provider.slug).first()
    if identity is None:
        raise sync_target_not_found()
    connection = UserExternalAccountConnection.objects.filter(user=user, provider=provider.slug).first()
    if connection is None:
        raise sync_target_not_found()
    if connection.status != UserExternalAccountConnection.Status.CONNECTED:
        raise external_account_needs_reauthorization()
    return provider, entry, identity, connection


def _context_is_current(*, user, entry, identity, connection):
    current_entry = JournalEntry.objects.filter(pk=entry.pk).values("user_id", "deleted_at").first()
    current_identity = ExternalMediaIdentity.objects.filter(pk=identity.pk).values(
        "entry_id", "provider", "external_id"
    ).first()
    current_connection = UserExternalAccountConnection.objects.filter(pk=connection.pk).values(
        "user_id", "provider", "external_user_id", "status"
    ).first()
    if current_connection and current_connection["status"] != UserExternalAccountConnection.Status.CONNECTED:
        raise external_account_needs_reauthorization()
    expected_user_id = user.pk
    return bool(
        current_entry
        and current_entry["user_id"] == expected_user_id
        and current_entry["deleted_at"] is None
        and current_identity
        and current_identity["entry_id"] == entry.pk
        and current_identity["provider"] == identity.provider
        and current_identity["external_id"] == identity.external_id
        and current_connection
        and current_connection["user_id"] == expected_user_id
        and current_connection["provider"] == identity.provider
        and current_connection["external_user_id"] == connection.external_user_id
        and ExternalMediaIdentity.objects.filter(
            entry_id=entry.pk,
            provider=identity.provider,
            pk=identity.pk,
        ).exists()
    )


def preview_collection_sync(*, user, provider_slug, entry_id):
    provider, entry, identity, connection = _resolve_context(
        user=user,
        provider_slug=provider_slug,
        entry_id=entry_id,
    )
    try:
        token = access_token(connection, provider)
    except ExternalAccountError as error:
        if _needs_reauthorization(error):
            raise external_account_needs_reauthorization() from error
        raise
    connection.refresh_from_db()
    if connection.status != UserExternalAccountConnection.Status.CONNECTED:
        raise external_account_needs_reauthorization()

    try:
        remote_collection = provider.get_collection(
            token,
            connection.external_username,
            identity.external_id,
        )
    except ExternalAccountError as error:
        if _needs_reauthorization(error):
            mark_needs_reauthorization(connection)
            raise external_account_needs_reauthorization() from error
        raise

    if not _context_is_current(
        user=user,
        entry=entry,
        identity=identity,
        connection=connection,
    ):
        raise sync_context_changed()

    entry.refresh_from_db()
    identity.refresh_from_db()
    connection.refresh_from_db()
    try:
        local = local_snapshot(entry)
        remote = provider.collection_sync_snapshot(remote_collection)
    except ValueError as error:
        raise sync_value_unsupported(str(error)) from error
    state = ExternalCollectionSyncState.objects.filter(
        identity=identity,
        connection=connection,
    ).first()
    baseline = state.baselines if state is not None else {}
    plan = plan_collection(
        baseline=baseline,
        local=local,
        remote=remote or {},
        remote_missing=remote is None,
        push_capabilities=provider.collection_push_capabilities(local),
    )
    return {
        "provider": provider.slug,
        "entry_id": entry.pk,
        "identity_id": identity.pk,
        "external_id": identity.external_id,
        "sync_state_initialized": bool(state and state.baselines),
        "last_synced_at": state.last_synced_at if state is not None else None,
        **plan,
    }
