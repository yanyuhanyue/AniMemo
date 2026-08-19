"""Collect read-only GitHub PR metadata and freeze AniMemo release notes."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

REPO_IMPORT_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_IMPORT_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_IMPORT_ROOT))

from release.notes import (
    CANONICAL_RELEASE_ASSETS,
    ReleaseNotesError,
    build_release_notes,
    render_release_notes,
)


_COMMIT = re.compile(r"[0-9a-f]{40}")


class SnapshotCollectionError(ValueError):
    """GitHub metadata cannot form one deterministic qualified snapshot."""


def _normalize_pr(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise SnapshotCollectionError("associated PR metadata must be an object")
    number = value.get("number")
    title = value.get("title")
    labels = value.get("labels")
    head = value.get("head")
    merge_commit = value.get("merge_commit_sha")
    if isinstance(number, bool) or not isinstance(number, int) or number < 1:
        raise SnapshotCollectionError("associated PR number is invalid")
    if (
        not isinstance(title, str)
        or not title
        or title != title.strip()
        or any(ord(character) < 32 for character in title)
    ):
        raise SnapshotCollectionError("associated PR title is invalid")
    if not isinstance(labels, list):
        raise SnapshotCollectionError("associated PR labels are missing")
    names = []
    for label in labels:
        if not isinstance(label, Mapping) or set(label) < {"name"}:
            raise SnapshotCollectionError("associated PR label is invalid")
        name = label["name"]
        if not isinstance(name, str) or not name or name != name.strip():
            raise SnapshotCollectionError("associated PR label name is invalid")
        names.append(name)
    if len(names) != len(set(names)):
        raise SnapshotCollectionError("associated PR has duplicate labels")
    if isinstance(merge_commit, str) and _COMMIT.fullmatch(merge_commit):
        source = merge_commit
    elif isinstance(head, Mapping) and isinstance(head.get("sha"), str) and _COMMIT.fullmatch(
        head["sha"]
    ):
        source = head["sha"]
    else:
        raise SnapshotCollectionError("associated PR source identity is missing")
    return {
        "number": number,
        "title": title,
        "source_identity": source,
        "labels": sorted(names),
    }


def _query_with_gh(repository: str, commit: str) -> list[dict[str, Any]]:
    completed = subprocess.run(
        [
            "gh",
            "api",
            "--method",
            "GET",
            "-H",
            "Accept: application/vnd.github+json",
            f"repos/{repository}/commits/{commit}/pulls?per_page=100",
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if completed.returncode != 0:
        raise SnapshotCollectionError(
            f"unable to query associated PR metadata for commit {commit}"
        )
    try:
        value = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise SnapshotCollectionError("GitHub PR metadata is not valid JSON") from error
    if not isinstance(value, list):
        raise SnapshotCollectionError("GitHub PR metadata response must be an array")
    return value


def collect_pull_metadata(
    *,
    repository: str,
    commits: Sequence[str],
    query: Callable[[str, str], list[dict[str, Any]]] = _query_with_gh,
) -> list[dict[str, Any]]:
    """Collect one closed PR record per associated PR over an exact commit range."""

    if repository != "yanyuhanyue/AniMemo":
        raise SnapshotCollectionError("release note repository authority is invalid")
    population: dict[int, dict[str, Any]] = {}
    for commit in commits:
        if not isinstance(commit, str) or not _COMMIT.fullmatch(commit):
            raise SnapshotCollectionError("release note commit range contains an invalid SHA")
        response = query(repository, commit)
        if not isinstance(response, list) or not response:
            raise SnapshotCollectionError(
                f"commit has no associated merged PR classification authority: {commit}"
            )
        for raw in response:
            normalized = _normalize_pr(raw)
            existing = population.get(normalized["number"])
            if existing is not None and existing != normalized:
                raise SnapshotCollectionError(
                    f"associated PR metadata changed within snapshot: {normalized['number']}"
                )
            population[normalized["number"]] = normalized
    return [population[number] for number in sorted(population)]


def _run_git(*arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if completed.returncode != 0:
        raise SnapshotCollectionError("unable to prove exact release note Git range")
    return completed.stdout


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Freeze deterministic release note metadata")
    parser.add_argument("--repository", required=True)
    parser.add_argument("--range-start", required=True)
    parser.add_argument("--candidate-sha", required=True)
    parser.add_argument("--previous-stable", default="")
    parser.add_argument("--release-tag", required=True)
    parser.add_argument("--target-version", required=True)
    parser.add_argument("--channel", required=True)
    parser.add_argument("--minimum-updater-version", default="1.0.0")
    parser.add_argument("--supported-os", action="append", default=[])
    parser.add_argument(
        "--docker-requirement", default="Docker Engine 27+ with Compose v2"
    )
    parser.add_argument("--output-input", type=Path, required=True)
    parser.add_argument("--output-json", type=Path, required=True)
    parser.add_argument("--output-markdown", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if not _COMMIT.fullmatch(args.range_start) or not _COMMIT.fullmatch(args.candidate_sha):
            raise SnapshotCollectionError("release note Git boundary is invalid")
        _run_git("merge-base", "--is-ancestor", args.range_start, args.candidate_sha)
        commits = [
            line
            for line in _run_git(
                "rev-list", "--reverse", f"{args.range_start}..{args.candidate_sha}"
            ).splitlines()
            if line
        ]
        pulls = collect_pull_metadata(
            repository=args.repository, commits=commits, query=_query_with_gh
        )
        note_input = {
            "context": {
                "candidate_sha": args.candidate_sha,
                "comparison_base_sha": args.range_start,
                "previous_stable": args.previous_stable,
                "release_tag": args.release_tag,
                "target_version": args.target_version,
                "channel": args.channel,
                "minimum_updater_version": args.minimum_updater_version,
                "supported_os": sorted(args.supported_os or ["Ubuntu 24.04 LTS"]),
                "docker_requirement": args.docker_requirement,
                "release_assets": list(CANONICAL_RELEASE_ASSETS),
            },
            "pulls": pulls,
        }
        snapshot = build_release_notes(
            context=note_input["context"], pulls=note_input["pulls"]
        )
        markdown = render_release_notes(snapshot)
        _write_json(args.output_input, note_input)
        _write_json(args.output_json, snapshot)
        args.output_markdown.parent.mkdir(parents=True, exist_ok=True)
        args.output_markdown.write_text(markdown, encoding="utf-8", newline="\n")
    except (OSError, ReleaseNotesError, SnapshotCollectionError) as error:
        print(
            json.dumps(
                {"code": "release_notes_snapshot_invalid", "detail": str(error)},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 2
    print(json.dumps(snapshot, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
