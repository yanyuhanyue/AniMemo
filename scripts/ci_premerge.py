#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path


SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")


class PreMergeValidationError(RuntimeError):
    pass


@dataclass(frozen=True)
class PreMergeSnapshot:
    pr_number: int
    head_sha: str
    base_sha: str
    head_ref: str
    repository: str


def _sha(value, *, label):
    normalized = str(value or "").strip().lower()
    if not SHA_PATTERN.fullmatch(normalized):
        raise PreMergeValidationError(f"Invalid {label}: {value!r}")
    return normalized


def validate_snapshot(payload, *, expected_pr_number, expected_head_sha, current_main_sha, repository):
    if not isinstance(payload, dict):
        raise PreMergeValidationError("Pull request payload must be a JSON object")
    try:
        pr_number = int(payload.get("number"))
    except (TypeError, ValueError) as error:
        raise PreMergeValidationError("Pull request number is invalid") from error
    if pr_number != int(expected_pr_number):
        raise PreMergeValidationError(f"Pull request number moved: expected {expected_pr_number}, found {pr_number}")
    if payload.get("state") != "open":
        raise PreMergeValidationError(f"Pull request must remain open; found {payload.get('state')!r}")

    base = payload.get("base") or {}
    head = payload.get("head") or {}
    if base.get("ref") != "main":
        raise PreMergeValidationError(f"Pull request base must remain main; found {base.get('ref')!r}")
    if (base.get("repo") or {}).get("full_name") != repository:
        raise PreMergeValidationError("Pull request base repository does not match the authoritative repository")
    if (head.get("repo") or {}).get("full_name") != repository:
        raise PreMergeValidationError("Pull request head repository must match the authoritative repository")

    expected_head = _sha(expected_head_sha, label="expected head SHA")
    head_sha = _sha(head.get("sha"), label="pull request head SHA")
    if head_sha != expected_head:
        raise PreMergeValidationError(f"Pull request head SHA moved: expected {expected_head}, found {head_sha}")

    current_main = _sha(current_main_sha, label="current main SHA")
    base_sha = _sha(base.get("sha"), label="pull request base SHA")
    if base_sha != current_main:
        raise PreMergeValidationError(
            f"Pull request base is not current main: expected {current_main}, found {base_sha}"
        )
    head_ref = str(head.get("ref") or "").strip()
    if not head_ref:
        raise PreMergeValidationError("Pull request head ref is empty")
    return PreMergeSnapshot(
        pr_number=pr_number,
        head_sha=head_sha,
        base_sha=base_sha,
        head_ref=head_ref,
        repository=repository,
    )


def _commit(repo, sha):
    completed = subprocess.run(
        ["git", "rev-parse", "--verify", f"{sha}^{{commit}}"],
        cwd=repo,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise PreMergeValidationError(f"Unable to resolve commit {sha}: {detail}")
    return completed.stdout.strip()


def verify_base_freshness(repo, *, base_sha, head_sha):
    base = _commit(repo, _sha(base_sha, label="base SHA"))
    head = _commit(repo, _sha(head_sha, label="head SHA"))
    completed = subprocess.run(
        ["git", "merge-base", "--is-ancestor", base, head],
        cwd=repo,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode:
        raise PreMergeValidationError(
            f"PRE-MERGE BASE FRESHNESS: FAIL; candidate {head} does not contain current main {base}"
        )


def _write_outputs(path, snapshot):
    if path is None:
        return
    with path.open("a", encoding="utf-8") as output:
        output.write(f"pr_number={snapshot.pr_number}\n")
        output.write(f"head_sha={snapshot.head_sha}\n")
        output.write(f"base_sha={snapshot.base_sha}\n")
        output.write(f"head_ref={snapshot.head_ref}\n")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Validate an exact AniMemo pre-merge authority snapshot.")
    parser.add_argument("--pr-json", type=Path, required=True)
    parser.add_argument("--pr-number", type=int, required=True)
    parser.add_argument("--expected-head-sha", required=True)
    parser.add_argument("--current-main-sha", required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--repo", type=Path, default=Path("."))
    parser.add_argument("--verify-freshness", action="store_true")
    parser.add_argument("--github-output", type=Path)
    args = parser.parse_args(argv)
    try:
        payload = json.loads(args.pr_json.read_text(encoding="utf-8"))
        snapshot = validate_snapshot(
            payload,
            expected_pr_number=args.pr_number,
            expected_head_sha=args.expected_head_sha,
            current_main_sha=args.current_main_sha,
            repository=args.repository,
        )
        if args.verify_freshness:
            verify_base_freshness(args.repo, base_sha=snapshot.base_sha, head_sha=snapshot.head_sha)
    except (OSError, json.JSONDecodeError, PreMergeValidationError) as error:
        print(json.dumps({"code": "pre_merge_invalid", "detail": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 2
    _write_outputs(args.github_output, snapshot)
    print(json.dumps(asdict(snapshot), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
