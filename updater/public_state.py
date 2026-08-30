from __future__ import annotations

import re
from datetime import datetime, timezone
from types import MappingProxyType

from .state import TRANSITIONS as _STATE_TRANSITIONS

_IDENTIFIER = re.compile(r"^[0-9a-f]{32}$")
_TIMESTAMP = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]{1,9})?Z$"
)

_KINDS = frozenset({"apply_update", "rollback_previous", "initial_adoption"})
_EVENT_DETAILS = MappingProxyType(
    {
        "idle": "Operation created",
        "preflight": "Preflight checks in progress",
        "fetching": "Release acquisition in progress",
        "verifying": "Release verification in progress",
        "backup": "Backup in progress",
        "pulling": "Image acquisition in progress",
        "migrating": "Database migration in progress",
        "bootstrapping": "Application bootstrap in progress",
        "switching": "Application switch in progress",
        "verifying_health": "Health verification in progress",
        "rolling_back": "Application rollback in progress",
        "adopting": "Initial release adoption in progress",
        "succeeded": "Update completed",
        "failed_pre_switch": "Operation failed before application switch",
        "failed_post_switch": "Operation failed after application switch",
        "rolled_back": "Application rollback completed",
        "manual_recovery_required": "Manual recovery is required",
        "reconciled": "Manual recovery was reconciled",
    }
)
_INVALID_STATE = "invalid_operation_state"
_INVALID_DETAIL = "Operation state is unavailable"

_TRANSITIONS = MappingProxyType(
    {status: frozenset(targets) for status, targets in _STATE_TRANSITIONS.items()}
)


def _identifier(value: object) -> str:
    return value if isinstance(value, str) and _IDENTIFIER.fullmatch(value) else ""


def _parsed_timestamp(value: object) -> tuple[str, datetime] | None:
    if not isinstance(value, str) or _TIMESTAMP.fullmatch(value) is None:
        return None
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        return None
    canonical = parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if canonical != value:
        return None
    return value, parsed


def _timestamp(value: object) -> str:
    parsed = _parsed_timestamp(value)
    return parsed[0] if parsed is not None else ""


def _invalid_operation(value: object) -> dict[str, object]:
    operation = value if isinstance(value, dict) else {}
    candidate_kind = operation.get("kind")
    kind = (
        candidate_kind
        if isinstance(candidate_kind, str) and candidate_kind in _KINDS
        else "unknown_operation"
    )
    return {
        "id": _identifier(operation.get("id")),
        "kind": kind,
        "status": _INVALID_STATE,
        "createdAt": "",
        "updatedAt": "",
        "events": [
            {
                "status": _INVALID_STATE,
                "at": "",
                "detail": _INVALID_DETAIL,
            }
        ],
    }


def public_event(value: object) -> dict[str, str]:
    event = value if isinstance(value, dict) else {}
    candidate_status = event.get("status")
    if isinstance(candidate_status, str) and candidate_status in _EVENT_DETAILS:
        public_status = candidate_status
        detail = _EVENT_DETAILS[candidate_status]
    else:
        public_status = _INVALID_STATE
        detail = _INVALID_DETAIL
    return {
        "status": public_status,
        "at": _timestamp(event.get("at")),
        "detail": detail,
    }


def public_operation(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        return _invalid_operation(value)
    operation = value
    identifier = _identifier(operation.get("id"))
    candidate_kind = operation.get("kind")
    kind = (
        candidate_kind
        if isinstance(candidate_kind, str) and candidate_kind in _KINDS
        else "unknown_operation"
    )
    created = _parsed_timestamp(operation.get("createdAt"))
    updated = _parsed_timestamp(operation.get("updatedAt"))
    events = operation.get("events")
    candidate_status = operation.get("status")
    if (
        not identifier
        or kind == "unknown_operation"
        or created is None
        or updated is None
        or not isinstance(events, list)
        or not events
        or not isinstance(candidate_status, str)
        or candidate_status not in _EVENT_DETAILS
    ):
        return _invalid_operation(operation)

    projected_events: list[dict[str, str]] = []
    parsed_event_times: list[datetime] = []
    event_statuses: list[str] = []
    for event in events:
        if not isinstance(event, dict):
            return _invalid_operation(operation)
        event_status = event.get("status")
        event_time = _parsed_timestamp(event.get("at"))
        if (
            not isinstance(event_status, str)
            or event_status not in _EVENT_DETAILS
            or event_time is None
        ):
            return _invalid_operation(operation)
        event_statuses.append(event_status)
        parsed_event_times.append(event_time[1])
        projected_events.append(public_event(event))

    valid_sequence = (
        event_statuses[0] == "idle"
        and event_statuses[-1] == candidate_status
        and created[1] == parsed_event_times[0]
        and updated[1] == parsed_event_times[-1]
        and all(
            current <= following
            for current, following in zip(parsed_event_times, parsed_event_times[1:])
        )
        and all(
            following in _TRANSITIONS.get(current, frozenset())
            for current, following in zip(event_statuses, event_statuses[1:])
        )
    )
    if not valid_sequence:
        return _invalid_operation(operation)

    return {
        "id": identifier,
        "kind": kind,
        "status": candidate_status,
        "createdAt": created[0],
        "updatedAt": updated[0],
        "events": projected_events[-1000:],
    }
