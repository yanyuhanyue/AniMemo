"""Acquire an exact published Release evidence set and export one offline sidecar."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

REPO_IMPORT_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_IMPORT_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_IMPORT_ROOT))

from release.acquisition import (
    REQUIRED_ACTIONS_EVIDENCE,
    AttestationAcquisitionError,
    GitHubAttestationAcquirer,
)


def _subjects(values: Sequence[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for value in values:
        name, separator, subject = value.partition("=")
        if not separator or name in result or not subject:
            raise AttestationAcquisitionError("ACTIONS_SUBJECT_ARGUMENT_INVALID")
        result[name] = subject
    if set(result) != set(REQUIRED_ACTIONS_EVIDENCE):
        raise AttestationAcquisitionError("ACTIONS_SUBJECT_SET_INVALID")
    return result


def _workflows(values: Sequence[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for value in values:
        name, separator, workflow = value.partition("=")
        if (
            not separator
            or name in result
            or name not in REQUIRED_ACTIONS_EVIDENCE
            or not workflow
        ):
            raise AttestationAcquisitionError("ACTIONS_WORKFLOW_ARGUMENT_INVALID")
        result[name] = workflow
    return result


def _source_commits(values: Sequence[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for value in values:
        name, separator, commit = value.partition("=")
        if (
            not separator
            or name in result
            or name not in REQUIRED_ACTIONS_EVIDENCE
            or not commit
        ):
            raise AttestationAcquisitionError(
                "ACTIONS_SOURCE_COMMIT_ARGUMENT_INVALID"
            )
        result[name] = commit
    return result


def _run_gh(command: tuple[str, ...]) -> bytes:
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            check=False,
        )
    except OSError as error:
        raise AttestationAcquisitionError("GITHUB_CLI_UNAVAILABLE") from error
    if completed.returncode != 0:
        raise AttestationAcquisitionError("GITHUB_ATTESTATION_ACQUISITION_FAILED")
    return completed.stdout


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Acquire exact-tag GitHub Release evidence for offline verification"
    )
    parser.add_argument("--repository", required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--workflow", required=True)
    parser.add_argument("--payload", type=Path, required=True)
    parser.add_argument(
        "--actions-subject",
        action="append",
        default=[],
        metavar="EVIDENCE_NAME=PATH_OR_OCI_REFERENCE",
    )
    parser.add_argument(
        "--actions-workflow",
        action="append",
        default=[],
        metavar="EVIDENCE_NAME=WORKFLOW_PATH",
    )
    parser.add_argument(
        "--actions-source-commit",
        action="append",
        default=[],
        metavar="EVIDENCE_NAME=COMMIT",
    )
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        envelope = GitHubAttestationAcquirer(runner=_run_gh).acquire_and_export(
            repository=args.repository,
            tag=args.tag,
            commit=args.commit,
            workflow=args.workflow,
            payload=args.payload,
            actions_subjects=_subjects(args.actions_subject),
            destination=args.output,
            actions_workflows=_workflows(args.actions_workflow),
            actions_source_commits=_source_commits(args.actions_source_commit),
        )
    except AttestationAcquisitionError as error:
        print(
            json.dumps(
                {"code": "release_attestation_acquisition_failed", "detail": str(error)},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 2
    print(
        json.dumps(
            {
                "schema": envelope["schema"],
                "tag": envelope["tag"],
                "payload": envelope["payload"],
                "evidence_count": len(envelope["evidence"]),
                "authority_role": envelope["authorityRole"],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
