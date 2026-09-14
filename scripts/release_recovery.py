"""GitHub-only entry point for the explicitly authorised existing RC recovery."""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

from release.candidate import canonical_json_bytes
from release.materials import read_bounded_release_file
from release.publication_transaction import GitRemoteAppendOnlyJournal
from release.recovery import ExistingRCRecovery, RecoveryPlatform
from release.recovery_contract import policy, require
from release.recovery_evidence import collect_evidence
from release.recovery_materials import decode_original_aggregate, prepare_materials
from release.recovery_remote import GitHubRecoveryRemote


def trusted_runner_paths(repository: Path, environment):
    # The checkout source is fixed by __file__, not a supplied directory.
    # Hosted checkout layout: <work>/<repository>/<repository>; the runner's
    # event and temporary directories are siblings of the outer checkout.
    temporary = repository.parent.parent / "_temp"
    event = temporary / "_github_workflow" / "event.json"
    require(
        environment.get("RUNNER_TEMP") == temporary.as_posix()
        and environment.get("GITHUB_EVENT_PATH") == event.as_posix(),
        "RECOVERY_RUNNER_PATHS_INVALID",
    )
    require(
        temporary.is_dir()
        and temporary.resolve() == temporary
        and event.resolve() == event,
        "RECOVERY_RUNNER_PATHS_INVALID",
    )
    return event, temporary / "animemo-existing-rc-recovery"


def main() -> int:
    repository = Path(__file__).resolve().parents[1]
    root = None
    owned = False
    engine = None
    try:
        require(os.environ.get("GITHUB_ACTIONS") == "true", "RECOVERY_HOST_UNTRUSTED")
        event_path, root = trusted_runner_paths(repository, os.environ)
        event = json.loads(
            read_bounded_release_file(
                event_path, maximum=128 * 1024, subject="Recovery platform event"
            )
        )
        root.mkdir(mode=0o700)
        owned = True
        evidence = root / "evidence"
        evidence.mkdir(mode=0o700)
        remote = GitHubRecoveryRemote(output=evidence)
        platform = RecoveryPlatform(remote, repository, os.environ, event)
        # Validate the trusted source before consuming any operator input.
        platform.execution()
        wire = event["inputs"]["candidate_acceptance_receipt_b64url"]
        require(
            isinstance(wire, str) and wire.isascii() and len(wire) <= 48 * 1024,
            "RECOVERY_RECEIPT_WIRE_INVALID",
        )
        aggregate = root / "candidate-acceptance-receipt.json"
        decode_original_aggregate(wire, aggregate)
        materials = prepare_materials(
            remote, root=root, aggregate=aggregate, now=platform.now()
        )
        (evidence / "material-verification.json").write_bytes(
            canonical_json_bytes(materials)
        )
        backend = GitRemoteAppendOnlyJournal(repository)
        os.chdir(root)
        engine = ExistingRCRecovery(
            platform=platform, backend=backend, materials=materials, root=evidence
        )
        execution = engine.run()
        if execution["status"] == "INSPECTED":
            (evidence / "inspection.json").write_bytes(canonical_json_bytes(execution))
            print(json.dumps({"status": "INSPECTED", "remoteMutations": 0}))
            return 0
        material_root = Path(materials["assetRoot"])
        record = collect_evidence(
            remote, material_root=material_root, output=root, execution=execution
        )
        (evidence / "execution-record.json").write_bytes(canonical_json_bytes(record))
        # The original metadata and seven-proof sidecar remain a closed set.
        # Recovery execution facts live in their own artifact.
        from scripts.release_publication_controller import _RELEASE_METADATA_FILES

        metadata = root / "metadata"
        metadata.mkdir(mode=0o700)
        sidecar = (
            f"animemo-{policy()['subject']['release_tag']}-release-attestation.json"
        )
        for name in sorted(_RELEASE_METADATA_FILES | {sidecar}):
            shutil.copyfile(material_root / name, metadata / name)
        print(
            json.dumps(
                {
                    "status": "EVIDENCE_VERIFIED",
                    "releaseId": record["releaseId"],
                    "runId": record["claim"]["runId"],
                    "journalAppendCount": record["journalAppendCount"],
                    "releaseWriteRequests": record["writeRequests"],
                }
            )
        )
        return 0
    except Exception as error:  # noqa: BLE001 - terminal boundary records failure, never resumes writes
        code = getattr(error, "code", None)
        if not isinstance(code, str) or not code.replace("_", "").isalnum():
            code = "RECOVERY_FAILED_CLOSED"
        failure = {
            "status": "FAILED",
            "code": code,
            "recoveryRunMayBeConsumed": os.environ.get("GITHUB_RUN_ID"),
            "releaseState": "UNKNOWN_READ_PLATFORM_BEFORE_ANY_FOLLOWUP",
        }
        if engine is not None:
            failure["writeRequests"] = engine.remote.write_requests
            failure["lastConfirmedJournalHead"] = (
                engine.guard.head if engine.guard else None
            )
            try:
                ledger = engine.backend.load(engine.claim["operationId"])
                if root is not None and ledger is not None:
                    (root / "evidence" / "failure-ledger-readback.json").write_bytes(
                        canonical_json_bytes(ledger)
                    )
                release = engine.remote.draft(published_allowed=True)
                failure["releaseState"] = (
                    "PUBLISHED" if release["draft"] is False else "DRAFT"
                )
            except Exception:  # noqa: BLE001 - failed readback must remain explicitly unknown
                failure["readbackStatus"] = "UNKNOWN"
        if owned and root is not None and (root / "evidence").is_dir():
            (root / "evidence" / "failure.json").write_bytes(
                canonical_json_bytes(failure)
            )
        print(json.dumps(failure), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
