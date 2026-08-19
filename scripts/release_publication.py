"""Workflow adapter for draft/readback/post-publish release verification."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

REPO_IMPORT_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_IMPORT_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_IMPORT_ROOT))

from release.publication import (
    PublicationError,
    declared_publication_assets,
    validate_publication_plan,
    verify_asset_readback,
    verify_post_publish,
)


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise PublicationError(f"expected a JSON object: {path}")
    return value


def _read_assets(
    directory: Path, plan: Mapping[str, Any]
) -> dict[str, Path]:
    if not directory.is_dir():
        raise PublicationError("release readback directory is missing")
    observed = {path.name for path in directory.iterdir() if path.is_file()}
    expected = declared_publication_assets(plan)
    if observed != set(expected):
        raise PublicationError("release readback directory has missing or extra assets")
    return {name: directory / name for name in expected}


def _validate_metadata(
    plan: Mapping[str, Any], metadata: Mapping[str, Any], *, draft: bool
) -> tuple[dict[str, dict[str, Any]], str]:
    required = {"id", "tag", "target", "draft", "prerelease", "body", "assets"}
    if set(metadata) != required:
        raise PublicationError("GitHub release metadata projection is not closed")
    release_id = metadata["id"]
    if isinstance(release_id, bool) or not isinstance(release_id, int) or release_id < 1:
        raise PublicationError("GitHub release id is invalid")
    if (
        metadata["tag"] != plan["tag"]
        or metadata["target"] != plan["commit"]
        or metadata["draft"] is not draft
        or metadata["prerelease"] is not (plan["channel"] != "stable")
        or not isinstance(metadata["body"], str)
    ):
        raise PublicationError("GitHub release metadata differs from publication plan")
    assets = metadata["assets"]
    if not isinstance(assets, list):
        raise PublicationError("GitHub release asset metadata is missing")
    projected: dict[str, dict[str, Any]] = {}
    for item in assets:
        if not isinstance(item, Mapping) or set(item) != {"name", "size", "digest"}:
            raise PublicationError("GitHub release asset metadata is not closed")
        name = item["name"]
        if not isinstance(name, str) or name in projected:
            raise PublicationError("GitHub release asset name is invalid or duplicated")
        digest = item["digest"]
        if digest is None:
            digest = declared_publication_assets(plan).get(name, {}).get("sha256")
        projected[name] = {"sha256": digest, "size": item["size"]}
    body_sha = "sha256:" + hashlib.sha256(metadata["body"].encode("utf-8")).hexdigest()
    return projected, body_sha


def _verify_draft(args: argparse.Namespace) -> dict[str, Any]:
    plan = validate_publication_plan(_read_json(args.plan))
    metadata = _read_json(args.metadata)
    remote, body_sha = _validate_metadata(plan, metadata, draft=True)
    downloaded = _read_assets(args.download_directory, plan)
    verify_asset_readback(plan, remote_assets=remote, downloaded_assets=downloaded)
    notes_sha = "sha256:" + hashlib.sha256(args.notes_file.read_bytes()).hexdigest()
    if notes_sha != plan["release_notes_markdown_sha256"] or body_sha != notes_sha:
        raise PublicationError("GitHub draft body differs from qualified release notes")
    return {
        "schema": "animemo.release-draft-verification/v1",
        "publication_plan_identity": plan["identity"],
        "release_id": metadata["id"],
        "state": "DRAFT_VERIFIED",
        "asset_count": len(downloaded),
        "notes_body_sha256": body_sha,
    }


def _verify_public(args: argparse.Namespace) -> dict[str, Any]:
    plan = validate_publication_plan(_read_json(args.plan))
    metadata = _read_json(args.metadata)
    remote, body_sha = _validate_metadata(plan, metadata, draft=False)
    downloaded = _read_assets(args.download_directory, plan)
    return verify_post_publish(
        plan,
        release={
            "tag": metadata["tag"],
            "target": metadata["target"],
            "draft": metadata["draft"],
            "prerelease": metadata["prerelease"],
            "notes_body_sha256": body_sha,
            "public_unauthenticated_assets": args.public_unauthenticated_assets,
        },
        remote_assets=remote,
        downloaded_assets=downloaded,
        api_digest=args.api_digest,
        web_digest=args.web_digest,
        attestations_verified=args.attestations_verified,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Verify AniMemo release publication")
    subparsers = parser.add_subparsers(dest="command", required=True)
    draft = subparsers.add_parser("verify-draft")
    draft.add_argument("--plan", type=Path, required=True)
    draft.add_argument("--metadata", type=Path, required=True)
    draft.add_argument("--download-directory", type=Path, required=True)
    draft.add_argument("--notes-file", type=Path, required=True)
    draft.set_defaults(handler=_verify_draft)
    public = subparsers.add_parser("verify-post-publish")
    public.add_argument("--plan", type=Path, required=True)
    public.add_argument("--metadata", type=Path, required=True)
    public.add_argument("--download-directory", type=Path, required=True)
    public.add_argument("--api-digest", required=True)
    public.add_argument("--web-digest", required=True)
    public.add_argument("--public-unauthenticated-assets", action="store_true")
    public.add_argument("--attestations-verified", action="store_true")
    public.set_defaults(handler=_verify_public)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        result = args.handler(args)
    except (OSError, json.JSONDecodeError, PublicationError) as error:
        print(
            json.dumps(
                {"code": "release_publication_invalid", "detail": str(error)},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
