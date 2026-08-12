"""Fail-closed Release Producer authority decision for Beta and RC channels."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence
from typing import Any


class ReleaseAuthorityError(ValueError):
    pass


def validate_release_authority(channel: str, needs: Mapping[str, Any]) -> dict[str, str]:
    normalized_channel = str(channel or "").strip().lower()
    if normalized_channel not in {"beta", "rc"}:
        raise ReleaseAuthorityError(f"unsupported release channel: {channel or '<unset>'}")

    required_results = ("preflight", "full-ci", "full-release-gate")
    failures: dict[str, str] = {}
    for name in required_results:
        job = needs.get(name)
        result = job.get("result") if isinstance(job, Mapping) else None
        if result != "success":
            failures[name] = str(result or "missing")

    performance = needs.get("performance")
    performance_result = performance.get("result") if isinstance(performance, Mapping) else None
    expected_performance = "success" if normalized_channel == "rc" else "skipped"
    if performance_result != expected_performance:
        failures["performance"] = str(performance_result or "missing")

    if failures:
        raise ReleaseAuthorityError(json.dumps(failures, sort_keys=True))
    return {"channel": normalized_channel, "status": "PASS"}


def main(argv: Sequence[str] | None = None) -> int:
    del argv
    channel = os.getenv("CHANNEL", "")
    raw_needs = os.getenv("NEEDS_JSON", "")
    try:
        needs = json.loads(raw_needs)
    except json.JSONDecodeError as error:
        raise ReleaseAuthorityError(f"invalid NEEDS_JSON: {error}") from error
    if not isinstance(needs, dict):
        raise ReleaseAuthorityError("NEEDS_JSON must be an object")
    result = validate_release_authority(channel, needs)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
