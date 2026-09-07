#!/usr/bin/env python3
"""Check committed whitespace using the exact event base/head, never the worktree."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path

EVENTS = frozenset({"pull_request", "push", "merge_group", "workflow_call", "workflow_dispatch"})
ZERO_SHA = "0" * 40


class CommittedTextError(RuntimeError):
    pass


def _sha(value: str, name: str, *, allow_zero: bool = False) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-fA-F]{40}", value):
        raise CommittedTextError(f"{name} must be an exact 40-character commit SHA")
    value = value.lower()
    if value == ZERO_SHA and not allow_zero:
        raise CommittedTextError(f"{name} must be a non-zero commit SHA")
    return value


def _git(repo: Path, *arguments: str, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *arguments], cwd=repo, input=stdin,
        check=False, capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=120,
    )


def _commit_present(repo: Path, sha: str) -> bool:
    result = _git(repo, "rev-parse", "--verify", f"{sha}^{{commit}}")
    return result.returncode == 0 and result.stdout.strip() == sha


def _fetch(repo: Path, *shas: str, unshallow: bool = False) -> None:
    result = _git(
        repo, "fetch", "--no-tags", "--no-recurse-submodules", "--no-write-fetch-head",
        *(["--unshallow"] if unshallow else []), "origin", *shas,
    )
    if result.returncode:
        # Git transport diagnostics can contain credential-bearing remote URLs.
        raise CommittedTextError("Unable to fetch the exact trusted commits from origin")


def _ensure_commit(repo: Path, sha: str, *, fetch_missing: bool) -> None:
    if _commit_present(repo, sha):
        return
    if fetch_missing:
        _fetch(repo, sha)
    if not _commit_present(repo, sha):
        raise CommittedTextError(f"Required commit object is unavailable: {sha}")


def _ancestor(repo: Path, ancestor: str, descendant: str) -> bool:
    result = _git(repo, "merge-base", "--is-ancestor", ancestor, descendant)
    if result.returncode not in {0, 1}:
        raise CommittedTextError("Unable to establish the committed-text diff direction")
    return result.returncode == 0


def check_committed_text(
    *, repo: Path, event_name: str, base: str, head: str, fetch_missing: bool = False,
) -> dict[str, str]:
    if event_name not in EVENTS:
        raise CommittedTextError("Committed-text event type is unsupported")
    base = _sha(base, "base", allow_zero=event_name == "push")
    head = _sha(head, "head")
    _ensure_commit(repo, head, fetch_missing=fetch_missing)
    checkout = _git(repo, "rev-parse", "--verify", "HEAD^{commit}")
    if checkout.returncode or checkout.stdout.strip() != head:
        raise CommittedTextError("Trusted head differs from the checked-out commit")

    if base == ZERO_SHA:
        # An initial push can introduce several commits, including the root.
        # Compare its complete resulting tree, not just HEAD's last parent.
        empty = _git(repo, "hash-object", "-w", "-t", "tree", "--stdin", stdin="")
        if empty.returncode:
            raise CommittedTextError("Unable to create the initial-push empty tree")
        diff_base = _sha(empty.stdout.strip(), "empty tree")
        scope = "initial-push-entire-tree"
    else:
        _ensure_commit(repo, base, fetch_missing=fetch_missing)
        shallow = _git(repo, "rev-parse", "--is-shallow-repository")
        if shallow.returncode:
            raise CommittedTextError("Unable to inspect the repository history")
        if fetch_missing and shallow.stdout.strip() == "true":
            _fetch(repo, base, head, unshallow=True)
        if base != head and _ancestor(repo, head, base):
            raise CommittedTextError("Trusted base/head have the wrong diff direction")
        if event_name == "push":
            if not _ancestor(repo, base, head):
                raise CommittedTextError("Push base must be an observed ancestor of head")
            diff_base, scope = base, "push-before-to-head"
        else:
            merge_base = _git(repo, "merge-base", "--all", base, head)
            ancestors = merge_base.stdout.splitlines()
            if merge_base.returncode or len(ancestors) != 1:
                raise CommittedTextError("A unique base/head merge-base is unavailable")
            diff_base = _sha(ancestors[0], "merge-base")
            scope = "classifier-merge-base-to-head"

    checked = _git(
        repo, "-c", "core.whitespace=blank-at-eol,blank-at-eof,space-before-tab",
        "diff", "--check", "--no-ext-diff", "--no-textconv", "--no-renames",
        diff_base, head, "--",
    )
    if checked.returncode:
        raise CommittedTextError(
            f"Committed text failed for {diff_base} -> {head}:\n"
            + (checked.stdout + checked.stderr).strip()
        )
    return {
        "status": "PASS", "event": event_name, "requested_base": base,
        "diff_base": diff_base, "head": head, "scope": scope,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event-name", required=True, choices=sorted(EVENTS))
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", required=True)
    parser.add_argument("--fetch-missing", action="store_true")
    arguments = parser.parse_args(argv)
    try:
        result = check_committed_text(
            repo=Path.cwd(), event_name=arguments.event_name,
            base=arguments.base, head=arguments.head, fetch_missing=arguments.fetch_missing,
        )
    except (CommittedTextError, OSError, subprocess.SubprocessError) as error:
        parser.exit(1, f"Committed-text scope/check failed: {error}\n")
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
