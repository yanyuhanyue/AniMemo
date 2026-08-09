from __future__ import annotations

from decimal import Decimal

from django.db import transaction
from django.utils import timezone

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

from .canonical import canonical_score, local_pull_capabilities, local_snapshot
from .confirmation import decode_preview_token, issue_preview_token
from .errors import (
    ExternalSyncError,
    external_account_needs_reauthorization,
    no_sync_action,
    sync_action_not_allowed,
    sync_context_changed,
    sync_preview_invalid,
    sync_preview_stale,
    sync_target_not_found,
    sync_value_unsupported,
)
from .planner import plan_collection
from .state import advance_confirmed_baselines, locked_sync_state

ALLOWED_ACTIONS = {
    "uninitialized_equal": frozenset(("accept_equal",)),
    "converged": frozenset(("accept_equal",)),
    "uninitialized": frozenset(("pull_remote",)),
    "remote_changed": frozenset(("pull_remote",)),
    "local_changed": frozenset(("pull_remote",)),
    "conflict": frozenset(("pull_remote",)),
}


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
    account_connection = UserExternalAccountConnection.objects.filter(
        user=user,
        provider=provider.slug,
    ).first()
    if account_connection is None:
        raise sync_target_not_found()
    if account_connection.status != UserExternalAccountConnection.Status.CONNECTED:
        raise external_account_needs_reauthorization()
    return provider, entry, identity, account_connection


def _context_is_current(*, user, entry, identity, account_connection):
    current_entry = JournalEntry.objects.filter(pk=entry.pk).values("user_id", "deleted_at").first()
    current_identity = ExternalMediaIdentity.objects.filter(pk=identity.pk).values(
        "entry_id", "provider", "external_id"
    ).first()
    current_connection = UserExternalAccountConnection.objects.filter(pk=account_connection.pk).values(
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
        and current_connection["external_user_id"] == account_connection.external_user_id
        and ExternalMediaIdentity.objects.filter(
            entry_id=entry.pk,
            provider=identity.provider,
            pk=identity.pk,
        ).exists()
    )


def _credential_and_remote(*, provider, identity, account_connection):
    try:
        token = access_token(account_connection, provider)
    except ExternalAccountError as error:
        if _needs_reauthorization(error):
            raise external_account_needs_reauthorization() from error
        raise
    account_connection.refresh_from_db()
    if account_connection.status != UserExternalAccountConnection.Status.CONNECTED:
        raise external_account_needs_reauthorization()

    try:
        return provider.get_collection(
            token,
            account_connection.external_username,
            identity.external_id,
        )
    except ExternalAccountError as error:
        if _needs_reauthorization(error):
            mark_needs_reauthorization(account_connection)
            raise external_account_needs_reauthorization() from error
        raise


def _collection_plan(*, provider, entry, state, remote_collection):
    try:
        local = local_snapshot(entry)
        remote = provider.collection_sync_snapshot(remote_collection)
        remote_missing = remote is None
        plan = plan_collection(
            baseline=state.baselines if state is not None else {},
            local=local,
            remote=remote or {},
            remote_missing=remote_missing,
            pull_capabilities=local_pull_capabilities(remote or {}, remote_missing=remote_missing),
            push_capabilities=provider.collection_push_capabilities(local),
        )
    except ValueError as error:
        raise sync_value_unsupported(str(error)) from error
    return local, remote, plan


def _preview_payload(*, user, provider, entry, identity, account_connection, state, remote_collection):
    _local, _remote, plan = _collection_plan(
        provider=provider,
        entry=entry,
        state=state,
        remote_collection=remote_collection,
    )
    return {
        "provider": provider.slug,
        "entry_id": entry.pk,
        "identity_id": identity.pk,
        "external_id": identity.external_id,
        "remote_collection_missing": remote_collection is None,
        "sync_state_initialized": bool(state and state.baselines),
        "last_synced_at": state.last_synced_at if state is not None else None,
        "preview_token": issue_preview_token(
            user=user,
            provider=provider,
            entry=entry,
            identity=identity,
            connection=account_connection,
            fingerprints=plan["fingerprints"],
        ),
        **plan,
    }


def preview_collection_sync(*, user, provider_slug, entry_id):
    provider, entry, identity, account_connection = _resolve_context(
        user=user,
        provider_slug=provider_slug,
        entry_id=entry_id,
    )
    remote_collection = _credential_and_remote(
        provider=provider,
        identity=identity,
        account_connection=account_connection,
    )
    if not _context_is_current(
        user=user,
        entry=entry,
        identity=identity,
        account_connection=account_connection,
    ):
        raise sync_context_changed()

    entry.refresh_from_db()
    identity.refresh_from_db()
    account_connection.refresh_from_db()
    state = ExternalCollectionSyncState.objects.filter(
        identity=identity,
        connection=account_connection,
    ).first()
    return _preview_payload(
        user=user,
        provider=provider,
        entry=entry,
        identity=identity,
        account_connection=account_connection,
        state=state,
        remote_collection=remote_collection,
    )


def _assert_token_request(payload, *, user, provider_slug, entry_id):
    if (
        payload["user_id"] != user.pk
        or payload["provider"] != provider_slug
        or payload["entry_id"] != entry_id
    ):
        raise sync_preview_invalid()


def _assert_token_context(payload, *, identity, account_connection):
    if (
        payload["identity_id"] != identity.pk
        or payload["connection_id"] != account_connection.pk
        or payload["external_id"] != identity.external_id
        or payload["external_user_id"] != account_connection.external_user_id
        or payload["provider"] != identity.provider
        or payload["provider"] != account_connection.provider
    ):
        raise sync_context_changed()


def _locked_context(*, user, payload):
    entry = JournalEntry.objects.select_for_update().filter(
        pk=payload["entry_id"],
        user=user,
        deleted_at__isnull=True,
    ).first()
    if entry is None:
        raise sync_context_changed()
    identity = ExternalMediaIdentity.objects.select_for_update().filter(
        pk=payload["identity_id"],
        entry=entry,
    ).first()
    account_connection = UserExternalAccountConnection.objects.select_for_update().filter(
        pk=payload["connection_id"],
        user=user,
    ).first()
    if identity is None or account_connection is None:
        raise sync_context_changed()
    if account_connection.status != UserExternalAccountConnection.Status.CONNECTED:
        raise external_account_needs_reauthorization()
    _assert_token_context(payload, identity=identity, account_connection=account_connection)
    return entry, identity, account_connection


def _selected_actions(actions):
    selected = {item["field"]: item["action"] for item in actions if item["action"] != "skip"}
    if not selected:
        raise no_sync_action()
    return selected


def _validate_actions(*, selected, plan):
    by_field = {item["field"]: item for item in plan["fields"]}
    for field, action in selected.items():
        field_plan = by_field[field]
        if action not in ALLOWED_ACTIONS.get(field_plan["state"], frozenset()):
            raise sync_action_not_allowed()
        if action == "pull_remote" and not field_plan["pull_supported"]:
            raise sync_action_not_allowed()


def _assign_remote_value(entry, *, field, remote_value):
    if field == "watch_status":
        value = remote_value["value"]
    elif field == "personal_score":
        value = Decimal(remote_value["value"]) if remote_value["present"] else None
    elif field == "review" and remote_value["present"]:
        value = remote_value["value"]
    else:
        raise sync_value_unsupported("远端字段值无法无损写入 AniMemo。")
    model_field = entry._meta.get_field(field)
    setattr(entry, field, model_field.clean(value, entry))


def _assert_fingerprints(payload, plan):
    expected = {
        "baseline": payload["baseline_fingerprint"],
        "local": payload["local_fingerprint"],
        "remote": payload["remote_fingerprint"],
    }
    if plan["fingerprints"] != expected:
        raise sync_preview_stale()


def apply_collection_sync(*, user, provider_slug, entry_id, preview_token, actions):
    payload = decode_preview_token(preview_token)
    _assert_token_request(
        payload,
        user=user,
        provider_slug=provider_slug,
        entry_id=entry_id,
    )
    selected = _selected_actions(actions)

    try:
        provider, entry, identity, account_connection = _resolve_context(
            user=user,
            provider_slug=provider_slug,
            entry_id=entry_id,
        )
    except ExternalSyncError as error:
        if getattr(error, "detail", {}).get("code") == "sync_target_not_found":
            raise sync_context_changed() from error
        raise
    _assert_token_context(payload, identity=identity, account_connection=account_connection)

    # The provider read deliberately finishes before any database row lock is held.
    remote_collection = _credential_and_remote(
        provider=provider,
        identity=identity,
        account_connection=account_connection,
    )
    if not _context_is_current(
        user=user,
        entry=entry,
        identity=identity,
        account_connection=account_connection,
    ):
        raise sync_context_changed()

    with transaction.atomic():
        entry, identity, account_connection = _locked_context(user=user, payload=payload)
        state = locked_sync_state(identity)
        if state is not None and state.connection_id != account_connection.pk:
            raise sync_context_changed()
        _local, remote, plan = _collection_plan(
            provider=provider,
            entry=entry,
            state=state,
            remote_collection=remote_collection,
        )
        _assert_fingerprints(payload, plan)
        _validate_actions(selected=selected, plan=plan)

        by_field = {item["field"]: item for item in plan["fields"]}
        local_updated_fields = []
        for field, action in selected.items():
            if action == "pull_remote":
                _assign_remote_value(entry, field=field, remote_value=by_field[field]["remote"])
                local_updated_fields.append(field)
        if local_updated_fields:
            entry.save(update_fields=[*local_updated_fields, "updated_at"])

        resulting_local = local_snapshot(entry)
        for field in selected:
            if resulting_local[field] != remote[field]:
                raise sync_value_unsupported("同步后的本地字段无法保持与远端语义一致。")

        synced_at = timezone.now()
        state, created = advance_confirmed_baselines(
            identity=identity,
            account_connection=account_connection,
            state=state,
            local=resulting_local,
            remote=remote,
            fields=tuple(selected),
            synced_at=synced_at,
        )

        return {
            "provider": provider.slug,
            "entry_id": entry.pk,
            "applied_fields": list(selected),
            "local_updated_fields": local_updated_fields,
            "baseline_advanced_fields": list(selected),
            "sync_state_initialized": bool(state.baselines),
            "sync_state_created": created,
            "last_synced_at": state.last_synced_at,
            "entry": {
                "watch_status": entry.watch_status,
                "personal_score": (
                    canonical_score(entry.personal_score)
                    if entry.personal_score is not None
                    else None
                ),
                "review": entry.review,
            },
        }
