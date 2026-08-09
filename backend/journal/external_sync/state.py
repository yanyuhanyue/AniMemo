from __future__ import annotations

from copy import deepcopy

from django.db import connection

from journal.models import ExternalCollectionSyncState

from .canonical import validate_baselines


def locked_sync_state(identity):
    if not connection.in_atomic_block:
        raise RuntimeError("sync state locks require an active transaction")
    return ExternalCollectionSyncState.objects.select_for_update().filter(identity=identity).first()


def advance_confirmed_baselines(
    *,
    identity,
    account_connection,
    state,
    local,
    remote,
    fields,
    synced_at,
):
    if not connection.in_atomic_block:
        raise RuntimeError("sync state mutations require an active transaction")
    if not fields:
        raise ValueError("at least one confirmed field is required")

    baselines = deepcopy(state.baselines if state is not None else {})
    for field in fields:
        if local[field] != remote[field]:
            raise ValueError("baseline can advance only for confirmed equal values")
        baselines[field] = deepcopy(local[field])
    validate_baselines(baselines)

    if state is None:
        state = ExternalCollectionSyncState.objects.create(
            identity=identity,
            connection=account_connection,
            baselines=baselines,
            last_synced_at=synced_at,
        )
        return state, True

    if state.connection_id != account_connection.pk:
        raise ValueError("sync state connection changed")
    state.baselines = baselines
    state.last_synced_at = synced_at
    state.save(update_fields=["baselines", "last_synced_at", "updated_at"])
    return state, False
