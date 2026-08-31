#!/usr/bin/env python3
"""Validate the pull-request primary category at the PR Fast boundary."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_IMPORT_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_IMPORT_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_IMPORT_ROOT))

from release.primary_category import PrimaryCategoryError, validate_primary_category


def _labels(payload: object) -> list[str]:
    if not isinstance(payload, dict) or not isinstance(payload.get("labels"), list):
        raise TypeError("pull request labels are missing")
    result: list[str] = []
    for value in payload["labels"]:
        name = value.get("name") if isinstance(value, dict) else value
        if not isinstance(name, str) or not name or name != name.strip():
            raise ValueError("pull request label is invalid")
        result.append(name)
    if len(result) != len(set(result)):
        raise ValueError("pull request labels contain duplicates")
    return sorted(result)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event-path", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        event = json.loads(args.event_path.read_text(encoding="utf-8"))
        payload = event.get("pull_request") if isinstance(event, dict) else None
        if not isinstance(payload, dict):
            raise TypeError("pull request event payload is missing")
        number = payload.get("number")
        head = payload.get("head")
        updated_at = payload.get("updated_at")
        if isinstance(number, bool) or not isinstance(number, int) or number < 1:
            raise ValueError("pull request number is invalid")
        head_sha = head.get("sha") if isinstance(head, dict) else None
        if not isinstance(head_sha, str):
            raise TypeError("pull request head SHA is missing")
        if not isinstance(updated_at, str) or not updated_at:
            raise ValueError("pull request updated_at is missing")
        decision = validate_primary_category(
            number=number,
            labels=_labels(payload),
            merge_commit=head_sha,
            observed_updated_at=updated_at,
        )
    except PrimaryCategoryError as error:
        print(
            json.dumps(
                {"code": error.code, "detail": str(error)},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 2
    except (OSError, TypeError, json.JSONDecodeError, ValueError) as error:
        print(
            json.dumps(
                {"code": "pr_fast_primary_category_invalid", "detail": str(error)},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 2
    print(
        json.dumps(
            {
                "category": decision.category,
                "decision": decision.decision,
                "exclusion_labels": decision.exclusion_labels,
                "primary_labels": decision.primary_labels,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
