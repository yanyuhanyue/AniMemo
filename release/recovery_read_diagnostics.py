"""Bounded response clues, never raw permission headers, bodies or URLs."""

import json
import re


def response_clues(response):
    raw = getattr(response, "accepted_permissions", None)
    accepted = None
    known = {
        "contents",
        "actions",
        "packages",
        "attestations",
        "pull_requests",
        "pull-requests",
        "administration",
        "metadata",
    }
    if isinstance(raw, str) and len(raw) <= 256:
        groups = [part.strip() for part in raw.split(";")]
        if groups and len(groups) <= 4:
            valid = True
            for group in groups:
                parts = [p.strip() for p in group.split(",")]
                valid &= bool(parts) and len(parts) <= 8
                for part in parts:
                    name, equals, level = part.partition("=")
                    valid &= (
                        equals == "="
                        and name in known
                        and level in {"read", "write", "admin"}
                    )
            if valid:
                accepted = "; ".join(
                    ", ".join(p.strip() for p in g.split(",")) for g in groups
                )
    result = {"acceptedGitHubPermissions": accepted}
    for name, field in (
        ("rateLimitRemaining", "rate_remaining"),
        ("rateLimitReset", "rate_reset"),
        ("retryAfterSeconds", "retry_after"),
    ):
        value = getattr(response, field, None)
        result[name] = (
            int(value)
            if isinstance(value, str) and re.fullmatch(r"[0-9]{1,12}", value)
            else None
        )
    status = response.status
    result["httpFailureClass"] = (
        "UNAUTHENTICATED"
        if status == 401
        else "FORBIDDEN"
        if status == 403
        else "NOT_FOUND"
        if status == 404
        else "RATE_LIMITED"
        if status == 429
        else "SERVER_ERROR"
        if 500 <= status <= 599
        else "REDIRECT"
        if 300 <= status <= 399
        else "NONE"
        if status == 200
        else "OTHER_HTTP_STATUS"
    )
    return result


def strict_json(data):
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("Duplicate JSON field")
            value[key] = item
        return value

    def invalid(_):
        raise ValueError("Non-finite JSON number")

    return json.loads(data, object_pairs_hook=unique, parse_constant=invalid)
