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
from release.publication_remote import build_publication_runtime
from release.publication_transaction import (
    DurablePublicationController,
    GitRemoteAppendOnlyJournal,
    MutationResponse,
    PublicationTransactionError,
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


def _transaction_controller(
    args: argparse.Namespace,
) -> tuple[DurablePublicationController, Any]:
    plan = _read_json(args.plan)
    runtime = build_publication_runtime(
        plan,
        source_tree=args.source_tree,
        asset_root=args.asset_root,
        candidate_root=args.candidate_root,
        repository_path=args.repository_path,
        remote=args.remote,
    )
    journal = GitRemoteAppendOnlyJournal(
        args.repository_path,
        remote=args.remote,
    )
    controller = DurablePublicationController.open(
        plan,
        source_tree=args.source_tree,
        intents=runtime.intents,
        journal=journal,
        adapters=runtime.adapters,
    )
    return controller, runtime


def _transaction_reconcile(args: argparse.Namespace) -> dict[str, Any]:
    controller, _runtime = _transaction_controller(args)
    controller.resume_pending()
    ledger = controller.preflight_all()
    return {
        "schema": "animemo.publication-transaction-command/v1",
        "command": "reconcile",
        "operationId": ledger["operationId"],
        "revision": ledger["revision"],
        "finalState": ledger["finalState"],
        "recoveryStatus": ledger["recoveryStatus"],
    }


def _transaction_run(args: argparse.Namespace) -> dict[str, Any]:
    controller, runtime = _transaction_controller(args)
    controller.resume_pending()
    controller.preflight_all()
    names = (
        runtime.registry_steps
        if args.phase == "registry"
        else runtime.publication_steps
    )
    ledger = controller.ledger
    for name in names:
        ledger = controller.advance(name)
    return {
        "schema": "animemo.publication-transaction-command/v1",
        "command": "run",
        "phase": args.phase,
        "operationId": ledger["operationId"],
        "revision": ledger["revision"],
        "finalState": ledger["finalState"],
        "committedSteps": [
            step["name"] for step in ledger["steps"] if step["committed"]
        ],
    }


def _append_github_output(path: Path, name: str, value: str) -> None:
    if name not in {"should_mutate"} or value not in {"true", "false"}:
        raise PublicationTransactionError("TRANSACTION_GITHUB_OUTPUT_INVALID")
    with path.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(f"{name}={value}\n")


def _transaction_begin_external(args: argparse.Namespace) -> dict[str, Any]:
    controller, runtime = _transaction_controller(args)
    if args.step not in runtime.external_steps:
        raise PublicationTransactionError("TRANSACTION_EXTERNAL_STEP_INVALID")
    controller.preflight_all()
    ledger = controller.begin_external(args.step)
    step = next(item for item in ledger["steps"] if item["name"] == args.step)
    should_mutate = step["state"] == "REQUEST_STARTED"
    _append_github_output(
        args.github_output,
        "should_mutate",
        "true" if should_mutate else "false",
    )
    return {
        "schema": "animemo.publication-transaction-command/v1",
        "command": "begin-external",
        "step": args.step,
        "operationId": ledger["operationId"],
        "revision": ledger["revision"],
        "shouldMutate": should_mutate,
    }


def _transaction_reconcile_external(args: argparse.Namespace) -> dict[str, Any]:
    controller, runtime = _transaction_controller(args)
    if args.step not in runtime.external_steps:
        raise PublicationTransactionError("TRANSACTION_EXTERNAL_STEP_INVALID")
    ledger = controller.reconcile_external(
        args.step,
        response=MutationResponse.acknowledged(),
    )
    step = next(item for item in ledger["steps"] if item["name"] == args.step)
    if not step["committed"]:
        raise PublicationTransactionError("TRANSACTION_EXTERNAL_STEP_INCOMPLETE")
    return {
        "schema": "animemo.publication-transaction-command/v1",
        "command": "reconcile-external",
        "step": args.step,
        "operationId": ledger["operationId"],
        "revision": ledger["revision"],
        "committed": True,
    }


def _transaction_finalize(args: argparse.Namespace) -> dict[str, Any]:
    controller, _runtime = _transaction_controller(args)
    ledger = controller.finalize()
    args.receipt.parent.mkdir(parents=True, exist_ok=True)
    args.receipt.write_text(
        json.dumps(ledger, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return {
        "schema": "animemo.publication-transaction-command/v1",
        "command": "finalize",
        "operationId": ledger["operationId"],
        "revision": ledger["revision"],
        "finalState": ledger["finalState"],
        "ledgerIdentity": ledger["ledgerIdentity"],
        "receipt": str(args.receipt),
    }


def _add_transaction_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--source-tree", required=True)
    parser.add_argument("--asset-root", type=Path, required=True)
    parser.add_argument("--candidate-root", type=Path)
    parser.add_argument("--repository-path", type=Path, default=Path("."))
    parser.add_argument("--remote", default="origin")


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
    reconcile = subparsers.add_parser("transaction-reconcile")
    _add_transaction_common(reconcile)
    reconcile.set_defaults(handler=_transaction_reconcile)
    run = subparsers.add_parser("transaction-run")
    _add_transaction_common(run)
    run.add_argument("--phase", choices=("registry", "publication"), required=True)
    run.set_defaults(handler=_transaction_run)
    begin_external = subparsers.add_parser("transaction-begin-external")
    _add_transaction_common(begin_external)
    begin_external.add_argument("--step", required=True)
    begin_external.add_argument("--github-output", type=Path, required=True)
    begin_external.set_defaults(handler=_transaction_begin_external)
    reconcile_external = subparsers.add_parser("transaction-reconcile-external")
    _add_transaction_common(reconcile_external)
    reconcile_external.add_argument("--step", required=True)
    reconcile_external.set_defaults(handler=_transaction_reconcile_external)
    finalize = subparsers.add_parser("transaction-finalize")
    _add_transaction_common(finalize)
    finalize.add_argument("--receipt", type=Path, required=True)
    finalize.set_defaults(handler=_transaction_finalize)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        result = args.handler(args)
    except (
        OSError,
        json.JSONDecodeError,
        PublicationError,
        PublicationTransactionError,
    ) as error:
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
