from __future__ import annotations

import re

from .errors import RequestRejected

RELEASE_VERSION = re.compile(
    r"^v(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)(?:-(?:beta|rc)\.[1-9][0-9]*)?$"
)
IDENTIFIER = re.compile(r"^[0-9a-f]{32}$")
CHANNELS = {"stable", "rc", "beta"}
TRANSPORT_SOURCES = {"github", "official-mirror"}


class BlockedPortablePublicationAuthority(RequestRejected):
    code = "BLOCKED_PORTABLE_PUBLICATION_AUTHORITY"

OPERATION_FIELDS = {
    "get_status": {},
    "list_releases": {"channel": str, "refresh": bool},
    "check_update": {"channel": str},
    "plan_update": {"version": str, "source": str},
    "apply_update": {"planId": str, "confirmation": str},
    "rollback_previous": {"confirmation": str},
    "get_operation": {"operationId": str},
    "get_logs": {"operationId": str, "limit": int},
}
REQUIRED_FIELDS = {
    "list_releases": {"channel"},
    "check_update": {"channel"},
    "plan_update": {"version"},
    "apply_update": {"planId", "confirmation"},
    "rollback_previous": {"confirmation"},
    "get_operation": {"operationId"},
    "get_logs": {"operationId"},
}


def _reject(detail: str) -> None:
    raise RequestRejected(detail)


def validate_request(request: object) -> dict[str, object]:
    if not isinstance(request, dict) or set(request) != {"operation", "params"}:
        _reject("Request must contain only operation and params")
    operation = request.get("operation")
    params = request.get("params")
    if operation not in OPERATION_FIELDS:
        _reject("Operation is not allowed")
    if not isinstance(params, dict):
        _reject("params must be an object")

    allowed = OPERATION_FIELDS[operation]
    if not set(params).issubset(allowed):
        _reject("Operation contains a forbidden parameter")
    if not REQUIRED_FIELDS.get(operation, set()).issubset(params):
        _reject("Operation is missing a required parameter")
    for name, value in params.items():
        expected = allowed[name]
        if expected is int and isinstance(value, bool):
            _reject(f"{name} has an invalid type")
        if not isinstance(value, expected):
            _reject(f"{name} has an invalid type")

    if "channel" in params and params["channel"] not in CHANNELS:
        _reject("Invalid release channel")
    if "version" in params and not RELEASE_VERSION.fullmatch(params["version"]):
        _reject("Invalid immutable release version")
    if operation == "plan_update" and params.get("source") == "local-bundle":
        raise BlockedPortablePublicationAuthority(
            "Portable local bundle publication authority is not frozen"
        )
    if "source" in params and params["source"] not in TRANSPORT_SOURCES:
        _reject("Invalid release transport source")
    for name in ("planId", "operationId"):
        if name in params and not IDENTIFIER.fullmatch(params[name]):
            _reject(f"Invalid {name}")
    if operation == "apply_update" and not re.fullmatch(
        r"APPLY v(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)(?:-(?:beta|rc)\.[1-9][0-9]*)?",
        params["confirmation"],
    ):
        _reject("Invalid apply confirmation")
    if operation == "rollback_previous" and params["confirmation"] != "ROLLBACK PREVIOUS":
        _reject("Invalid rollback confirmation")
    if "limit" in params and not 1 <= params["limit"] <= 1000:
        _reject("Log limit must be between 1 and 1000")
    return request
