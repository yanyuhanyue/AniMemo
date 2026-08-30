from __future__ import annotations

import re
from datetime import datetime, timezone
from itertools import pairwise

_ID = re.compile(r"^[0-9a-f]{32}$")
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
_EVENT_DETAILS = {
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
    "invalid_operation_state": "Operation state is unavailable",
}
_KINDS = frozenset({"apply_update", "rollback_previous", "initial_adoption", "unknown_operation"})
_TRANSITIONS = {
    "idle": {"preflight", "failed_pre_switch"},
    "preflight": {"fetching", "failed_pre_switch"},
    "fetching": {"verifying", "failed_pre_switch"},
    "verifying": {"backup", "pulling", "adopting", "failed_pre_switch"},
    "backup": {"pulling", "failed_pre_switch"},
    "pulling": {"migrating", "bootstrapping", "switching", "failed_pre_switch"},
    "migrating": {"bootstrapping", "manual_recovery_required"},
    "bootstrapping": {"switching", "manual_recovery_required"},
    "switching": {
        "verifying_health",
        "rolling_back",
        "failed_post_switch",
        "manual_recovery_required",
    },
    "verifying_health": {
        "succeeded",
        "rolling_back",
        "rolled_back",
        "failed_post_switch",
        "manual_recovery_required",
    },
    "rolling_back": {"rolled_back", "manual_recovery_required"},
    "adopting": {"succeeded", "manual_recovery_required"},
    "manual_recovery_required": {"reconciled"},
}

_PLAN_COMMON = frozenset(
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
_PLAN_LOCAL = frozenset(
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


class UpdateSuccessResultError(ValueError):
    pass


def _reject() -> None:
    raise UpdateSuccessResultError("Update service returned an invalid success result")


def _dict(value, fields):
    if type(value) is not dict or set(value) != set(fields):
        _reject()
    return value


def _string(value, pattern):
    if type(value) is not str or pattern.fullmatch(value) is None:
        _reject()
    return value


def _params(value):
    if type(value) is not dict:
        _reject()
    return value


def _version_channel(version):
    if "-rc." in version:
        return "rc"
    if "-beta." in version:
        return "beta"
    return "stable"


def _timestamp(value):
    if type(value) is not str or not value.endswith("Z"):
        _reject()
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        _reject()
    if (
        parsed.utcoffset() != timezone.utc.utcoffset(parsed)
        or parsed.isoformat().replace("+00:00", "Z") != value
    ):
        _reject()
    return value


def _identity(value):
    item = _dict(value, {"version", "channel", "commit", "apiDigest", "webDigest"})
    version = _string(item["version"], _VERSION)
    if (
        item["channel"] not in _CHANNELS
        or item["channel"] != _version_channel(version)
    ):
        _reject()
    return {
        "version": version,
        "channel": item["channel"],
        "commit": _string(item["commit"], _COMMIT),
        "apiDigest": _string(item["apiDigest"], _DIGEST),
        "webDigest": _string(item["webDigest"], _DIGEST),
    }


def _compatibility(value):
    fields = {
        "allowed",
        "decision",
        "rollbackMode",
        "migrationRequired",
        "migrationPolicy",
        "reasons",
    }
    item = _dict(value, fields)
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
    return {field: list(item[field]) if field == "reasons" else item[field] for field in fields}


def _event(value):
    item = _dict(value, {"status", "at", "detail"})
    status = item["status"]
    if status not in _EVENT_DETAILS or item["detail"] != _EVENT_DETAILS[status]:
        _reject()
    if status == "invalid_operation_state":
        if item["at"] != "":
            _reject()
        at = ""
    else:
        at = _timestamp(item["at"])
    return {"status": status, "at": at, "detail": item["detail"]}


def _operation(value):
    item = _dict(value, {"id", "kind", "status", "createdAt", "updatedAt", "events"})
    status = item["status"]
    if item["kind"] not in _KINDS or status not in _EVENT_DETAILS:
        _reject()
    if status == "invalid_operation_state":
        identifier = item["id"]
        if identifier != "" and (type(identifier) is not str or _ID.fullmatch(identifier) is None):
            _reject()
        if item["createdAt"] != "" or item["updatedAt"] != "":
            _reject()
        created_at = updated_at = ""
    else:
        identifier = _string(item["id"], _ID)
        created_at = _timestamp(item["createdAt"])
        updated_at = _timestamp(item["updatedAt"])
    if type(item["events"]) is not list or not 1 <= len(item["events"]) <= 1000:
        _reject()
    events = [_event(event) for event in item["events"]]
    if events[-1]["status"] != status:
        _reject()
    if status == "invalid_operation_state":
        if events != [
            {
                "status": "invalid_operation_state",
                "at": "",
                "detail": "Operation state is unavailable",
            }
        ]:
            _reject()
    else:
        if item["kind"] == "unknown_operation" or events[0]["status"] != "idle":
            _reject()
        if created_at != events[0]["at"] or updated_at != events[-1]["at"]:
            _reject()
        times = [datetime.fromisoformat(event["at"][:-1] + "+00:00") for event in events]
        if any(current > following for current, following in pairwise(times)):
            _reject()
        statuses = [event["status"] for event in events]
        if any(
            following not in _TRANSITIONS.get(current, set())
            for current, following in pairwise(statuses)
        ):
            _reject()
    return {
        "id": identifier,
        "kind": item["kind"],
        "status": status,
        "createdAt": created_at,
        "updatedAt": updated_at,
        "events": events,
    }


def _release(value):
    item = _dict(value, {"version", "channel", "publishedAt", "compatibility"})
    version = _string(item["version"], _VERSION)
    if (
        item["channel"] not in _CHANNELS
        or item["channel"] != _version_channel(version)
    ):
        _reject()
    return {
        "version": version,
        "channel": item["channel"],
        "publishedAt": _timestamp(item["publishedAt"]),
        "compatibility": _compatibility(item["compatibility"]),
    }


def _receipt(value):
    fields = {
        "schema",
        "publicationIdentity",
        "publicationExecutionReceiptIdentity",
        "signedClaimIdentity",
        "signedAt",
        "identity",
    }
    item = _dict(value, fields)
    if item["schema"] != "animemo.release-execution-receipt/v1":
        _reject()
    return {
        "schema": item["schema"],
        "publicationIdentity": _string(item["publicationIdentity"], _DIGEST),
        "publicationExecutionReceiptIdentity": _string(item["publicationExecutionReceiptIdentity"], _DIGEST),
        "signedClaimIdentity": _string(item["signedClaimIdentity"], _DIGEST),
        "signedAt": _timestamp(item["signedAt"]),
        "identity": _string(item["identity"], _DIGEST),
    }


def project_update_plan(value):
    if type(value) is not dict or value.get("source") not in _SOURCES:
        _reject()
    source = value["source"]
    item = _dict(value, _PLAN_COMMON | (_PLAN_LOCAL if source == "local-bundle" else frozenset()))
    if item["affectedServices"] != ["api", "web"] or item["databaseRollback"] is not False:
        _reject()
    result = {
        "planId": _string(item["planId"], _ID),
        "expiresAt": _timestamp(item["expiresAt"]),
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
            _PLAN_LOCAL - {"releaseExecutionReceipt", "trustProfileVersion"}
        ):
            result[field] = _string(item[field], _DIGEST)
        if type(item["trustProfileVersion"]) is not int or item["trustProfileVersion"] < 1:
            _reject()
        result["trustProfileVersion"] = item["trustProfileVersion"]
        result["releaseExecutionReceipt"] = _receipt(item["releaseExecutionReceipt"])
    return result


def _status(value):
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
    item = _dict(value, fields)
    runtime = _dict(item["runtime"], {"databaseContract", "configurationContract", "enabledPluginApis"})
    apis = runtime["enabledPluginApis"]
    if type(apis) is not list or apis != sorted(set(apis)) or any(type(api) is not int or api < 1 for api in apis):
        _reject()
    previous = None if item["previous"] is None else _identity(item["previous"])
    previous_compatibility = None if item["previousCompatibility"] is None else _compatibility(item["previousCompatibility"])
    if (previous is None) != (previous_compatibility is None):
        _reject()
    recovery = item["recoveryBlock"]
    if recovery is not None:
        recovery = _dict(recovery, {"required", "operationId", "since", "detail"})
        if recovery["required"] is not True or recovery["detail"] not in set(_EVENT_DETAILS.values()):
            _reject()
        operation_id = recovery["operationId"]
        if operation_id != "":
            operation_id = _string(operation_id, _ID)
        since = recovery["since"]
        if since != "":
            since = _timestamp(since)
        recovery = {"required": True, "operationId": operation_id, "since": since, "detail": recovery["detail"]}
    history = item["history"]
    if type(history) is not list or len(history) > 1000:
        _reject()
    projected_history = []
    for candidate in history:
        row = _dict(candidate, {"version", "channel", "commit", "apiDigest", "webDigest", "deployment", "compatibility"})
        deployment = _dict(row["deployment"], {"operationId"})
        operation_id = deployment["operationId"]
        if operation_id is not None:
            operation_id = _string(operation_id, _ID)
        projected_history.append(
            {
                **_identity({key: row[key] for key in ("version", "channel", "commit", "apiDigest", "webDigest")}),
                "deployment": {"operationId": operation_id},
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
            "enabledPluginApis": list(apis),
        },
        "recoveryBlock": recovery,
        "operation": None if item["operation"] is None else _operation(item["operation"]),
        "history": projected_history,
    }


def _event_sequence(value, *, maximum):
    if type(value) is not list or not 1 <= len(value) <= maximum:
        _reject()
    events = [_event(event) for event in value]
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
        following not in _TRANSITIONS.get(current, set())
        for current, following in pairwise(statuses)
    ):
        _reject()
    return events


def project_update_success(operation, value, request_params):
    params = _params(request_params)
    if operation == "get_status":
        return _status(value)
    if operation == "list_releases":
        item = _dict(value, {"channel", "releases"})
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
        item = _dict(value, {"channel", "currentVersion", "latest"})
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
        plan = project_update_plan(value)
        if (
            plan["to"]["version"] != requested_version
            or plan["source"] != requested_source
        ):
            _reject()
        return plan
    if operation == "apply_update":
        item = _dict(value, {"planId", "operation"})
        requested_plan_id = params.get("planId")
        if (
            type(requested_plan_id) is not str
            or _ID.fullmatch(requested_plan_id) is None
            or item["planId"] != requested_plan_id
        ):
            _reject()
        projected = _operation(item["operation"])
        if (
            projected["kind"] != operation
            or projected["status"] == "invalid_operation_state"
        ):
            _reject()
        return {"planId": item["planId"], "operation": projected}
    if operation == "rollback_previous":
        item = _dict(value, {"operation"})
        projected = _operation(item["operation"])
        if (
            projected["kind"] != operation
            or projected["status"] == "invalid_operation_state"
        ):
            _reject()
        return {"operation": projected}
    if operation == "get_operation":
        requested_id = params.get("operationId")
        if type(requested_id) is not str or _ID.fullmatch(requested_id) is None:
            _reject()
        projected = _operation(value)
        if projected["id"] != requested_id:
            _reject()
        return projected
    if operation == "get_logs":
        item = _dict(value, {"operationId", "events"})
        requested_id = params.get("operationId")
        requested_limit = params.get("limit", 100)
        if (
            type(requested_id) is not str
            or _ID.fullmatch(requested_id) is None
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
