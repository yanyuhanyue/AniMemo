#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path


class RefResolutionError(RuntimeError):
    pass


@dataclass(frozen=True)
class ResolvedRefs:
    base: str
    head: str
    source: str


def _git_commit(repo, ref):
    completed = subprocess.run(
        ["git", "rev-parse", "--verify", f"{ref}^{{commit}}"],
        cwd=repo,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise RefResolutionError(f"Unable to resolve Git commit {ref!r}: {detail}")
    return completed.stdout.strip()


def _load_event(env):
    path = str(env.get("GITHUB_EVENT_PATH") or "").strip()
    if not path:
        return {}
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RefResolutionError(f"Unable to read GitHub event payload: {error}") from error


def _is_zero_sha(value):
    value = str(value or "").strip()
    return bool(value) and set(value) == {"0"}


def resolve_refs(*, repo=".", explicit_base="", explicit_head="", env=None, event=None):
    env = dict(os.environ if env is None else env)
    event = _load_event(env) if event is None else event
    event_name = str(env.get("GITHUB_EVENT_NAME") or "").strip()

    head_candidate = str(explicit_head or env.get("GITHUB_SHA") or "HEAD").strip()
    head = _git_commit(repo, head_candidate)

    base_candidate = str(explicit_base or "").strip()
    source = "explicit --base"
    if not base_candidate and event_name == "pull_request":
        base_candidate = str(event.get("pull_request", {}).get("base", {}).get("sha") or "").strip()
        source = "github.event.pull_request.base.sha"
    elif not base_candidate and event_name == "push":
        before = str(event.get("before") or "").strip()
        if before and not _is_zero_sha(before):
            base_candidate = before
            source = "github.event.before"
        else:
            base_candidate = f"{head}^"
            source = "push fallback HEAD^ (before was empty or all-zero)"
    elif not base_candidate and event_name == "workflow_dispatch":
        inputs = event.get("inputs") or {}
        manual = str(inputs.get("upgrade_base_sha") or env.get("INPUT_UPGRADE_BASE_SHA") or "").strip()
        if manual:
            base_candidate = manual
            source = "workflow_dispatch upgrade_base_sha"
        else:
            base_candidate = f"{head}^"
            source = "workflow_dispatch fallback HEAD^"
    elif not base_candidate:
        base_candidate = f"{head}^"
        source = "local fallback HEAD^"

    if not base_candidate:
        raise RefResolutionError("No upgrade base ref could be resolved.")
    base = _git_commit(repo, base_candidate)
    return ResolvedRefs(base=base, head=head, source=source)


def main():
    parser = argparse.ArgumentParser(description="Resolve audited base/head commits for CI release gates.")
    parser.add_argument("--repo", default=".")
    parser.add_argument("--base", default="")
    parser.add_argument("--head", default="")
    parser.add_argument("--github-output", type=Path)
    args = parser.parse_args()
    try:
        refs = resolve_refs(repo=args.repo, explicit_base=args.base, explicit_head=args.head)
    except RefResolutionError as error:
        parser.exit(1, f"CI ref resolution failed: {error}\n")

    print(f"Upgrade Base SHA: {refs.base}")
    print(f"Upgrade Head SHA: {refs.head}")
    print(f"Base resolution: {refs.source}")
    if args.github_output:
        with args.github_output.open("a", encoding="utf-8") as output:
            output.write(f"base={refs.base}\nhead={refs.head}\nsource={refs.source}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
