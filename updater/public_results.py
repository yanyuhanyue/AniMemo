from __future__ import annotations

import re
from datetime import datetime, timezone
from itertools import pairwise

from .public_state import public_event, public_operation
from .state import TRANSITIONS as _STATE_TRANSITIONS

_IDENTIFIER = re.compile(r"^[0-9a-f]{32}$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_POLICY_IDENTITY = re.compile(r"^[0-9a-f]{64}$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_VERSION = re.compile(
    r"^v(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
    r"(?:-(?:beta|rc)\.[1-9][0-9]*)?$"
)
_UPDATER_VERSION = re.compile(
    r"^(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
    r"(?:[+-][0-9A-Za-z.-]+)?$"
)
_CONTRACT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

_CHANNELS = frozenset({"stable", "rc", "beta"})
_SOURCES = frozenset({"github", "official-mirror", "local-bundle"})
_DECISIONS = frozenset({"safe_switch", "application_rollback", "blocked", "unsafe_downgrade"})
_ROLLBACK_MODES = frozenset({"safe", "application", "blocked"})
_MIGRATION_POLICIES = frozenset(
    {"none", "additive-backward-compatible", "breaking-blocked", "unknown"}
)
_REASONS = frozenset(
    {
        "database_contract_not_accepted",
        "configuration_contract_not_accepted",
        "enabled_plugin_sdk_not_supported",
        "breaking_migration_blocked",
        "Release compatibility could not be evaluated",
    }
)
_OPERATION_KINDS = frozenset({"apply_update", "rollback_previous", "initial_adoption"})
_OPERATION_STATUSES = frozenset(
    {
        "idle",
        "preflight",
        "fetching",
        "verifying",
        "backup",
        "pulling",
        "migrating",
        "bootstrapping",
        "switching",
        "verifying_health",
        "rolling_back",
        "adopting",
        "succeeded",
        "failed_pre_switch",
        "failed_post_switch",
        "rolled_back",
        "manual_recovery_required",
        "reconciled",
        "invalid_operation_state",
    }
)
_TRANSITIONS = {
    status: frozenset(targets) for status, targets in _STATE_TRANSITIONS.items()
}
_PUBLIC_EVENT_DETAILS = frozenset(
    public_event({"status": status, "at": ""})["detail"]
    for status in _OPERATION_STATUSES
)

_PLAN_COMMON_FIELDS = frozenset(
    {
        "planId",
        "expiresAt",
        "from",
        "to",
        "compatibility",
        "affectedServices",
        "databaseRollback",
        "source",
        "transportPolicyIdentity",
        "verifiedReleaseIdentity",
    }
)
_PLAN_LOCAL_FIELDS = frozenset(
    {
        "transportIdentity",
        "payloadIdentity",
        "releaseAttestationIdentity",
        "releaseExecutionReceipt",
        "trustProfileVersion",
        "trustProfileIdentity",
        "manifestIdentity",
        "deploymentContractIdentity",
        "apiDigest",
        "webDigest",
        "postgresDigest",
        "redisDigest",
    }
)


class PublicResultError(ValueError):
    pass


def _reject() -> None:
    raise PublicResultError("Updater success result is invalid")


def _exact_dict(value: object, fields: set[str] | frozenset[str]) -> dict[str, object]:
    if type(value) is not dict or set(value) != set(fields):
        _reject()
    return value


def _canonical_timestamp(value: object) -> str:
    if type(value) is not str:
        _reject()
    try:
        parsed = datetime.fromisoformat(value.removesuffix("Z") + "+00:00")
    except ValueError:
        _reject()
    if not value.endswith("Z") or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        _reject()
    if parsed.isoformat().replace("+00:00", "Z") != value:
        _reject()
    return value


def _string(value: object, pattern: re.Pattern[str]) -> str:
    if type(value) is not str or pattern.fullmatch(value) is None:
        _reject()
    return value


def _params(value: object) -> dict[str, object]:
    if type(value) is not dict:
        _reject()
    return value


def _version_channel(version: str) -> str:
    if "-rc." in version:
        return "rc"
    if "-beta." in version:
        return "beta"
    return "stable"


def _identity(value: object) -> dict[str, str]:
    item = _exact_dict(value, {"version", "channel", "commit", "apiDigest", "webDigest"})
    version = _string(item["version"], _VERSION)
    channel = item["channel"]
    if channel not in _CHANNELS or channel != _version_channel(version):
        _reject()
    return {
        "version": version,
        "channel": channel,
        "commit": _string(item["commit"], _COMMIT),
        "apiDigest": _string(item["apiDigest"], _DIGEST),
        "webDigest": _string(item["webDigest"], _DIGEST),
    }


def _compatibility(value: object) -> dict[str, object]:
    item = _exact_dict(
        value,
        {
            "allowed",
            "decision",
            "rollbackMode",
            "migrationRequired",
            "migrationPolicy",
            "reasons",
        },
    )
    if (
        type(item["allowed"]) is not bool
        or item["decision"] not in _DECISIONS
        or item["rollbackMode"] not in _ROLLBACK_MODES
        or type(item["migrationRequired"]) is not bool
        or item["migrationPolicy"] not in _MIGRATION_POLICIES
        or type(item["reasons"]) is not list
        or len(item["reasons"]) > 16
        or any(reason not in _REASONS for reason in item["reasons"])
    ):
        _reject()
    return {
        "allowed": item["allowed"],
        "decision": item["decision"],
        "rollbackMode": item["rollbackMode"],
        "migrationRequired": item["migrationRequired"],
        "migrationPolicy": item["migrationPolicy"],
        "reasons": list(item["reasons"]),
    }


def _operation(value: object) -> dict[str, object]:
    item = _exact_dict(value, {"id", "kind", "status", "createdAt", "updatedAt", "events"})
    if public_operation(item) != item:
        _reject()
    if (
        item["kind"] not in _OPERATION_KINDS
        and item["kind"] != "unknown_operation"
    ) or item["status"] not in _OPERATION_STATUSES:
        _reject()
    identifier = item["id"]
    created_at = item["createdAt"]
    updated_at = item["updatedAt"]
    if item["status"] == "invalid_operation_state":
        if identifier != "" and (type(identifier) is not str or _IDENTIFIER.fullmatch(identifier) is None):
            _reject()
        if created_at not in {""} or updated_at not in {""}:
            _reject()
    else:
        identifier = _string(identifier, _IDENTIFIER)
        created_at = _canonical_timestamp(created_at)
        updated_at = _canonical_timestamp(updated_at)
    if type(item["events"]) is not list or not 1 <= len(item["events"]) <= 1000:
        _reject()
    events = []
    for candidate in item["events"]:
        event = _exact_dict(candidate, {"status", "at", "detail"})
        if event["status"] not in _OPERATION_STATUSES or public_event(event) != event:
            _reject()
        events.append(dict(event))
    if item["status"] != events[-1]["status"]:
        _reject()
    return {
        "id": identifier,
        "kind": item["kind"],
        "status": item["status"],
        "createdAt": created_at,
        "updatedAt": updated_at,
        "events": events,
    }


def _release(value: object) -> dict[str, object]:
    item = _exact_dict(value, {"version", "channel", "publishedAt", "compatibility"})
    version = _string(item["version"], _VERSION)
    if (
        item["channel"] not in _CHANNELS
        or item["channel"] != _version_channel(version)
    ):
        _reject()
    return {
        "version": version,
        "channel": item["channel"],
        "publishedAt": _canonical_timestamp(item["publishedAt"]),
        "compatibility": _compatibility(item["compatibility"]),
    }


def _execution_receipt(value: object) -> dict[str, object]:
    fields = {
        "schema",
        "publicationIdentity",
        "publicationExecutionReceiptIdentity",
        "signedClaimIdentity",
        "signedAt",
        "identity",
    }
    item = _exact_dict(value, fields)
    if item["schema"] != "animemo.release-execution-receipt/v1":
        _reject()
    result = {"schema": item["schema"]}
    for field in (
        "publicationIdentity",
        "publicationExecutionReceiptIdentity",
        "signedClaimIdentity",
    ):
        result[field] = _string(item[field], _DIGEST)
    result["signedAt"] = _canonical_timestamp(item["signedAt"])
    result["identity"] = _string(item["identity"], _DIGEST)
    return result


def public_plan(value: object) -> dict[str, object]:
    if type(value) is not dict or value.get("source") not in _SOURCES:
        _reject()
    source = value["source"]
    fields = _PLAN_COMMON_FIELDS | (_PLAN_LOCAL_FIELDS if source == "local-bundle" else frozenset())
    item = _exact_dict(value, fields)
    if (
        item["affectedServices"] != ["api", "web"]
        or item["databaseRollback"] is not False
    ):
        _reject()
    result: dict[str, object] = {
        "planId": _string(item["planId"], _IDENTIFIER),
        "expiresAt": _canonical_timestamp(item["expiresAt"]),
        "from": _identity(item["from"]),
        "to": _identity(item["to"]),
        "compatibility": _compatibility(item["compatibility"]),
        "affectedServices": ["api", "web"],
        "databaseRollback": False,
        "source": source,
        "transportPolicyIdentity": _string(
            item["transportPolicyIdentity"], _POLICY_IDENTITY
        ),
        "verifiedReleaseIdentity": _string(item["verifiedReleaseIdentity"], _DIGEST),
    }
    if source == "local-bundle":
        for field in sorted(
            _PLAN_LOCAL_FIELDS - {"releaseExecutionReceipt", "trustProfileVersion"}
        ):
            result[field] = _string(item[field], _DIGEST)
        if type(item["trustProfileVersion"]) is not int or item["trustProfileVersion"] < 1:
            _reject()
        result["trustProfileVersion"] = item["trustProfileVersion"]
        result["releaseExecutionReceipt"] = _execution_receipt(item["releaseExecutionReceipt"])
    return result


def _status(value: object) -> dict[str, object]:
    fields = {
        "updaterVersion",
        "current",
        "previous",
        "previousCompatibility",
        "runtime",
        "recoveryBlock",
        "operation",
        "history",
    }
    item = _exact_dict(value, fields)
    runtime = _exact_dict(
        item["runtime"], {"databaseContract", "configurationContract", "enabledPluginApis"}
    )
    enabled = runtime["enabledPluginApis"]
    if (
        type(enabled) is not list
        or enabled != sorted(set(enabled))
        or any(type(api) is not int or api < 1 for api in enabled)
    ):
        _reject()
    previous = None if item["previous"] is None else _identity(item["previous"])
    previous_compatibility = (
        None
        if item["previousCompatibility"] is None
        else _compatibility(item["previousCompatibility"])
    )
    if (previous is None) != (previous_compatibility is None):
        _reject()
    recovery = item["recoveryBlock"]
    if recovery is not None:
        recovery = _exact_dict(recovery, {"required", "operationId", "since", "detail"})
        if recovery["required"] is not True:
            _reject()
        operation_id = recovery["operationId"]
        since = recovery["since"]
        detail = recovery["detail"]
        if operation_id != "" and (type(operation_id) is not str or _IDENTIFIER.fullmatch(operation_id) is None):
            _reject()
        if since != "":
            since = _canonical_timestamp(since)
        if detail not in _PUBLIC_EVENT_DETAILS:
            _reject()
        recovery = {
            "required": True,
            "operationId": operation_id,
            "since": since,
            "detail": detail,
        }
    history = item["history"]
    if type(history) is not list or len(history) > 1000:
        _reject()
    projected_history = []
    for candidate in history:
        row = _exact_dict(
            candidate,
            {"version", "channel", "commit", "apiDigest", "webDigest", "deployment", "compatibility"},
        )
        deployment = _exact_dict(row["deployment"], {"operationId"})
        deployment_id = deployment["operationId"]
        if deployment_id is not None:
            deployment_id = _string(deployment_id, _IDENTIFIER)
        projected_history.append(
            {
                **_identity({key: row[key] for key in ("version", "channel", "commit", "apiDigest", "webDigest")}),
                "deployment": {"operationId": deployment_id},
                "compatibility": _compatibility(row["compatibility"]),
            }
        )
    return {
        "updaterVersion": _string(item["updaterVersion"], _UPDATER_VERSION),
        "current": _identity(item["current"]),
        "previous": previous,
        "previousCompatibility": previous_compatibility,
        "runtime": {
            "databaseContract": _string(runtime["databaseContract"], _CONTRACT),
            "configurationContract": _string(runtime["configurationContract"], _CONTRACT),
            "enabledPluginApis": list(enabled),
        },
        "recoveryBlock": recovery,
        "operation": None if item["operation"] is None else _operation(item["operation"]),
        "history": projected_history,
    }


def _event_sequence(value: object, *, maximum: int) -> list[dict[str, object]]:
    if type(value) is not list or not 1 <= len(value) <= maximum:
        _reject()
    events = []
    for candidate in value:
        event = _exact_dict(candidate, {"status", "at", "detail"})
        if event["status"] not in _OPERATION_STATUSES or public_event(event) != event:
            _reject()
        events.append(dict(event))
    if any(event["status"] == "invalid_operation_state" for event in events):
        if events != [
            {
                "status": "invalid_operation_state",
                "at": "",
                "detail": "Operation state is unavailable",
            }
        ]:
            _reject()
        return events
    times = [
        datetime.fromisoformat(event["at"][:-1] + "+00:00") for event in events
    ]
    if any(current > following for current, following in pairwise(times)):
        _reject()
    statuses = [event["status"] for event in events]
    if any(
        following not in _TRANSITIONS.get(current, frozenset())
        for current, following in pairwise(statuses)
    ):
        _reject()
    return events


def public_result(
    operation: object, value: object, request_params: object
) -> dict[str, object]:
    params = _params(request_params)
    if operation == "get_status":
        return _status(value)
    if operation == "list_releases":
        item = _exact_dict(value, {"channel", "releases"})
        requested_channel = params.get("channel")
        if (
            requested_channel not in _CHANNELS
            or item["channel"] != requested_channel
            or type(item["releases"]) is not list
        ):
            _reject()
        releases = [_release(row) for row in item["releases"]]
        if any(release["channel"] != requested_channel for release in releases):
            _reject()
        return {"channel": item["channel"], "releases": releases}
    if operation == "check_update":
        item = _exact_dict(value, {"channel", "currentVersion", "latest"})
        requested_channel = params.get("channel")
        if (
            requested_channel not in _CHANNELS
            or item["channel"] != requested_channel
        ):
            _reject()
        latest = None if item["latest"] is None else _release(item["latest"])
        if latest is not None and latest["channel"] != requested_channel:
            _reject()
        return {
            "channel": item["channel"],
            "currentVersion": _string(item["currentVersion"], _VERSION),
            "latest": latest,
        }
    if operation == "plan_update":
        requested_version = params.get("version")
        requested_source = params.get("source", "github")
        if (
            type(requested_version) is not str
            or _VERSION.fullmatch(requested_version) is None
            or requested_source not in _SOURCES
        ):
            _reject()
        plan = public_plan(value)
        if (
            plan["to"]["version"] != requested_version
            or plan["source"] != requested_source
        ):
            _reject()
        return plan
    if operation == "apply_update":
        item = _exact_dict(value, {"planId", "operation"})
        projected = _operation(item["operation"])
        requested_plan_id = params.get("planId")
        if (
            type(requested_plan_id) is not str
            or _IDENTIFIER.fullmatch(requested_plan_id) is None
            or item["planId"] != requested_plan_id
            or projected["kind"] != operation
            or projected["status"] == "invalid_operation_state"
        ):
            _reject()
        return {"planId": item["planId"], "operation": projected}
    if operation == "rollback_previous":
        item = _exact_dict(value, {"operation"})
        projected = _operation(item["operation"])
        if (
            projected["kind"] != operation
            or projected["status"] == "invalid_operation_state"
        ):
            _reject()
        return {"operation": projected}
    if operation == "get_operation":
        requested_id = params.get("operationId")
        if type(requested_id) is not str or _IDENTIFIER.fullmatch(requested_id) is None:
            _reject()
        projected = _operation(value)
        if projected["id"] != requested_id:
            _reject()
        return projected
    if operation == "get_logs":
        item = _exact_dict(value, {"operationId", "events"})
        requested_id = params.get("operationId")
        requested_limit = params.get("limit", 100)
        if (
            type(requested_id) is not str
            or _IDENTIFIER.fullmatch(requested_id) is None
            or item["operationId"] != requested_id
            or type(requested_limit) is not int
            or type(requested_limit) is bool
            or not 1 <= requested_limit <= 1000
        ):
            _reject()
        return {
            "operationId": item["operationId"],
            "events": _event_sequence(item["events"], maximum=requested_limit),
        }
    _reject()
