#!/usr/bin/env python3
"""Create or verify one run-scoped frozen Release Notes preflight artifact."""

from __future__ import annotations

import argparse
import json
import stat
import sys
from pathlib import Path

REPO_IMPORT_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_IMPORT_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_IMPORT_ROOT))

from release.release_notes_preflight import (
    FILE_NAMES,
    ReleaseNotesPreflightError,
    build_preflight_manifest,
    canonical_json_bytes,
    verify_preflight_manifest,
)


def _binding(args: argparse.Namespace) -> dict[str, object]:
    return {
        "repository": args.repository,
        "run_id": args.run_id,
        "run_attempt": args.run_attempt,
        "head_sha": args.head_sha,
        "head_tree": args.head_tree,
        "comparison_base_sha": args.comparison_base_sha,
        "previous_stable": args.previous_stable,
        "release_tag": args.release_tag,
        "target_version": args.target_version,
        "channel": args.channel,
    }


def _files(root: Path, *, include_manifest: bool) -> dict[str, bytes]:
    expected = set(FILE_NAMES)
    if include_manifest:
        expected.add("release-notes-preflight.json")
    actual: set[str] = set()
    for path in root.iterdir():
        metadata = path.lstat()
        if path.is_symlink() or not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise ReleaseNotesPreflightError(
                "release preflight root contains a non-regular entry"
            )
        actual.add(path.name)
    if actual != expected:
        raise ReleaseNotesPreflightError("release preflight root file set is not closed")
    return {name: (root / name).read_bytes() for name in FILE_NAMES}


def _add_binding(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--repository", required=True)
    parser.add_argument("--run-id", required=True, type=int)
    parser.add_argument("--run-attempt", required=True, type=int)
    parser.add_argument("--head-sha", required=True)
    parser.add_argument("--head-tree", required=True)
    parser.add_argument("--comparison-base-sha", required=True)
    parser.add_argument("--previous-stable", default="")
    parser.add_argument("--release-tag", required=True)
    parser.add_argument("--target-version", required=True)
    parser.add_argument("--channel", required=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("create", "verify"):
        subparser = subparsers.add_parser(command)
        subparser.add_argument("--root", type=Path, required=True)
        _add_binding(subparser)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        files = _files(args.root, include_manifest=args.command == "verify")
        binding = _binding(args)
        manifest_path = args.root / "release-notes-preflight.json"
        if args.command == "create":
            manifest = build_preflight_manifest(binding=binding, files=files)
            manifest_path.write_bytes(canonical_json_bytes(manifest))
        else:
            raw = manifest_path.read_bytes()
            manifest = json.loads(raw)
            if raw != canonical_json_bytes(manifest):
                raise ReleaseNotesPreflightError(
                    "release-notes-preflight.json is not canonical JSON"
                )
            manifest = verify_preflight_manifest(
                manifest,
                files=files,
                expected_binding=binding,
            )
    except (OSError, json.JSONDecodeError, ReleaseNotesPreflightError) as error:
        print(
            json.dumps(
                {"code": "release_notes_preflight_invalid", "detail": str(error)},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 2
    print(
        json.dumps(
            {
                "identity": manifest["identity"],
                "population": manifest["population"],
                "release_notes_identity": manifest["release_notes_identity"],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
