"""Closed, one-operation recovery authority. A hash is a binding, not a permit.

Production authority comes from the operator's explicit one-run dispatch through
the reviewed, protected PR #255 workflow and independently retrieved GitHub facts.
The immutable product subject and the recovery tool deliberately remain separate.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


class RecoveryError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def require(condition: bool, code: str) -> None:
    if not condition:
        raise RecoveryError(code)


def identity(value: Any) -> str:
    encoded = (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def policy() -> dict[str, Any]:
    return json.loads(
        Path(__file__).with_name("rc-recovery-policy.json").read_text(encoding="utf-8")
    )


def instant(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        require(
            parsed.tzinfo is not None and parsed.utcoffset() == timedelta(0),
            "RECOVERY_TIME_INVALID",
        )
        return parsed
    except (ValueError, TypeError, AttributeError):
        raise RecoveryError("RECOVERY_TIME_INVALID") from None


def utc(value: datetime) -> str:
    require(value.tzinfo is not None, "RECOVERY_TIME_INVALID")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def execution_title(mode: str) -> str:
    require(mode in {"inspect", "execute"}, "RECOVERY_MODE_INVALID")
    return f"Existing RC recovery {mode} | {policy()['scope']}"


def validate_execution(
    *,
    environment: Mapping[str, str],
    event: Mapping[str, Any],
    run: Mapping[str, Any],
    workflow: Mapping[str, Any],
    repository: Mapping[str, Any],
    pull_request: Mapping[str, Any],
    main_sha: str,
    checkout_sha: str,
    checkout_tree: str,
    reviewed_tree: str,
    parent_sha: str,
    execution_runs: list[Mapping[str, Any]],
    now: datetime,
) -> dict[str, Any]:
    """Validate live platform facts; callers may not supply a prebuilt permit."""
    p = policy()
    require(environment.get("GITHUB_ACTIONS") == "true", "RECOVERY_HOST_UNTRUSTED")
    require(
        environment.get("GITHUB_EVENT_NAME") == "workflow_dispatch",
        "RECOVERY_EVENT_INVALID",
    )
    require(environment.get("GITHUB_REF") == "refs/heads/main", "RECOVERY_REF_INVALID")
    require(
        event.get("inputs", {}).get("authorization_scope") == p["scope"],
        "RECOVERY_SCOPE_INVALID",
    )
    mode = event.get("inputs", {}).get("operation")
    require(mode in {"inspect", "execute"}, "RECOVERY_MODE_INVALID")
    require(
        set(event.get("inputs", {}))
        == {"operation", "authorization_scope", "candidate_acceptance_receipt_b64url"},
        "RECOVERY_INPUT_SET_INVALID",
    )
    require(
        repository.get("id") == p["repositoryId"]
        and repository.get("owner", {}).get("id") == p["ownerId"]
        and repository.get("full_name") == p["repository"],
        "RECOVERY_REPOSITORY_INVALID",
    )
    for actor in (
        run.get("actor", {}),
        run.get("triggering_actor", {}),
        event.get("sender", {}),
    ):
        require(
            actor.get("id") == p["ownerId"] and actor.get("login") == p["operator"],
            "RECOVERY_OPERATOR_INVALID",
        )
    require(
        run.get("event") == "workflow_dispatch"
        and run.get("head_branch") == "main"
        and type(run.get("run_attempt")) is int
        and run.get("run_attempt") == 1,
        "RECOVERY_RUN_INVALID",
    )
    require(
        run.get("repository", {}).get("id") == p["repositoryId"]
        and run.get("head_repository", {}).get("id") == p["repositoryId"],
        "RECOVERY_RUN_REPOSITORY_INVALID",
    )
    require(
        type(run.get("id")) is int
        and str(run["id"]) == environment.get("GITHUB_RUN_ID")
        and environment.get("GITHUB_RUN_ATTEMPT") == "1",
        "RECOVERY_RUN_BINDING_INVALID",
    )
    require(
        type(run.get("workflow_id")) is int
        and type(workflow.get("id")) is int
        and run.get("workflow_id") == workflow.get("id")
        and workflow.get("path") == p["workflow"]
        and workflow.get("state") == "active",
        "RECOVERY_WORKFLOW_INVALID",
    )
    require(
        run.get("path", "").split("@")[0] == p["workflow"], "RECOVERY_WORKFLOW_INVALID"
    )
    require(
        environment.get("GITHUB_WORKFLOW_REF")
        == f"{p['repository']}/{p['workflow']}@refs/heads/main",
        "RECOVERY_WORKFLOW_REF_INVALID",
    )
    require(
        run.get("display_title") == execution_title(mode), "RECOVERY_RUN_TITLE_INVALID"
    )
    require(
        pull_request.get("number") == p["sourcePr"]
        and pull_request.get("merged") is True
        and pull_request.get("state") == "closed",
        "RECOVERY_TOOL_NOT_MERGED",
    )
    require(
        pull_request.get("head", {}).get("repo", {}).get("id") == p["repositoryId"]
        and pull_request.get("head", {}).get("ref") == p["sourceBranch"],
        "RECOVERY_TOOL_SOURCE_INVALID",
    )
    require(
        pull_request.get("base", {}).get("repo", {}).get("id") == p["repositoryId"]
        and pull_request.get("base", {}).get("ref") == "main",
        "RECOVERY_TOOL_SOURCE_INVALID",
    )
    tool = pull_request.get("merge_commit_sha")
    require(
        isinstance(tool, str) and re.fullmatch(r"[0-9a-f]{40}", tool) is not None,
        "RECOVERY_TOOL_INVALID",
    )
    require(
        tool
        == main_sha
        == checkout_sha
        == run.get("head_sha")
        == environment.get("GITHUB_SHA")
        == environment.get("GITHUB_WORKFLOW_SHA"),
        "RECOVERY_TOOL_DRIFT",
    )
    require(
        parent_sha == p["subject"]["sha"] and checkout_tree == reviewed_tree,
        "RECOVERY_REVIEW_TREE_DRIFT",
    )
    require(
        run.get("status") in {"in_progress", "queued", "waiting"}
        and run.get("conclusion") is None,
        "RECOVERY_RUN_NOT_ACTIVE",
    )
    issued = instant(p["authorizationIssuedAt"])
    run_created = instant(run["created_at"])
    require(
        issued
        <= run_created
        <= now
        < issued + timedelta(seconds=p["authorizationSeconds"]),
        "RECOVERY_AUTHORIZATION_EXPIRED",
    )
    if mode == "execute":
        executions = [
            r
            for r in execution_runs
            if r.get("display_title") == execution_title("execute")
        ]
        require(
            any(r.get("id") == run["id"] for r in executions),
            "RECOVERY_RUN_HISTORY_INCOMPLETE",
        )
        require(
            all(
                r.get("workflow_id") == workflow["id"]
                and r.get("event") == "workflow_dispatch"
                for r in executions
            ),
            "RECOVERY_RUN_HISTORY_INVALID",
        )
        first = min(executions, key=lambda r: (instant(r["created_at"]), r["id"]))
        require(first["id"] == run["id"], "RECOVERY_EXECUTION_ALREADY_CONSUMED")
    binding = {
        "schema": "animemo.source-bound-recovery-claim/v1",
        "scope": p["scope"],
        "authority": "GITHUB_PROTECTED_WORKFLOW_DISPATCH",
        "repositoryId": p["repositoryId"],
        "operatorId": p["ownerId"],
        "sourcePr": p["sourcePr"],
        "subject": p["subject"],
        "toolSha": tool,
        "toolTree": checkout_tree,
        "reviewedHead": pull_request["head"]["sha"],
        "workflow": p["workflow"],
        "workflowId": workflow["id"],
        "runId": run["id"],
        "runAttempt": 1,
        "mode": mode,
        "issuedAt": utc(issued),
        "runCreatedAt": utc(run_created),
        "expiresAt": utc(issued + timedelta(seconds=p["authorizationSeconds"])),
        "initialHead": p["transaction"]["observed_head"],
        "initialRevision": 30,
        "initialLedgerIdentity": p["transaction"]["ledger_identity"],
        "operationId": p["transaction"]["operation_id"],
        "planDigest": p["transaction"]["plan_digest"],
        "draftId": p["transaction"]["draft_id"],
        "writeSteps": p["remainingSteps"],
    }
    binding["nonce"] = identity(
        {"scope": p["scope"], "runId": run["id"], "createdAt": utc(run_created)}
    )
    binding["bindingDigest"] = identity(binding)
    return validate_claim(binding)


def validate_claim(value: Mapping[str, Any]) -> dict[str, Any]:
    p = policy()
    keys = {
        "schema",
        "scope",
        "authority",
        "repositoryId",
        "operatorId",
        "sourcePr",
        "subject",
        "toolSha",
        "toolTree",
        "reviewedHead",
        "workflow",
        "workflowId",
        "runId",
        "runAttempt",
        "mode",
        "issuedAt",
        "runCreatedAt",
        "expiresAt",
        "initialHead",
        "initialRevision",
        "initialLedgerIdentity",
        "operationId",
        "planDigest",
        "draftId",
        "writeSteps",
        "nonce",
        "bindingDigest",
    }
    require(
        isinstance(value, Mapping) and set(value) == keys,
        "RECOVERY_CLAIM_FIELDS_INVALID",
    )
    expected = {
        "schema": "animemo.source-bound-recovery-claim/v1",
        "scope": p["scope"],
        "authority": "GITHUB_PROTECTED_WORKFLOW_DISPATCH",
        "repositoryId": p["repositoryId"],
        "operatorId": p["ownerId"],
        "sourcePr": p["sourcePr"],
        "subject": p["subject"],
        "workflow": p["workflow"],
        "runAttempt": 1,
        "initialHead": p["transaction"]["observed_head"],
        "initialRevision": 30,
        "initialLedgerIdentity": p["transaction"]["ledger_identity"],
        "operationId": p["transaction"]["operation_id"],
        "planDigest": p["transaction"]["plan_digest"],
        "draftId": p["transaction"]["draft_id"],
        "writeSteps": p["remainingSteps"],
    }
    require(
        all(value[k] == v for k, v in expected.items()), "RECOVERY_CLAIM_SCOPE_INVALID"
    )
    require(value["mode"] in {"inspect", "execute"}, "RECOVERY_MODE_INVALID")
    for key in ("toolSha", "toolTree", "reviewedHead"):
        require(
            isinstance(value[key], str)
            and re.fullmatch(r"[0-9a-f]{40}", value[key]) is not None,
            "RECOVERY_TOOL_INVALID",
        )
    for key in (
        "runId",
        "workflowId",
        "runAttempt",
        "initialRevision",
        "repositoryId",
        "operatorId",
        "sourcePr",
        "draftId",
    ):
        require(
            type(value[key]) is int and value[key] > 0, "RECOVERY_CLAIM_TYPE_INVALID"
        )
    issued, expires = instant(value["issuedAt"]), instant(value["expiresAt"])
    require(
        issued == instant(p["authorizationIssuedAt"])
        and issued <= instant(value["runCreatedAt"]) < expires
        and expires - issued == timedelta(seconds=p["authorizationSeconds"]),
        "RECOVERY_CLAIM_TIME_INVALID",
    )
    require(
        value["nonce"]
        == identity(
            {
                "scope": p["scope"],
                "runId": value["runId"],
                "createdAt": utc(instant(value["runCreatedAt"])),
            }
        ),
        "RECOVERY_NONCE_INVALID",
    )
    unsigned = {k: v for k, v in value.items() if k != "bindingDigest"}
    require(
        value["bindingDigest"] == identity(unsigned), "RECOVERY_CLAIM_DIGEST_INVALID"
    )
    return copy.deepcopy(dict(value))


def validate_claim_transition(
    previous: Mapping[str, Any] | None, current: Mapping[str, Any]
) -> None:
    before = previous.get("sourceBoundRecovery") if previous else None
    after = current.get("sourceBoundRecovery")
    if before is not None:
        require(after == before, "RECOVERY_CLAIM_REPLACED")
        for old, new, expected in zip(
            previous["steps"], current["steps"], policy()["steps"]
        ):
            count = expected["initialAttempts"]
            require(
                new["attempts"][:count] == old["attempts"][:count],
                "RECOVERY_ORIGINAL_ATTEMPT_CHANGED",
            )
    elif after is not None:
        validate_claim(after)
        require(
            previous is not None and after["mode"] == "execute",
            "RECOVERY_CLAIM_INITIAL_INVALID",
        )
        p = policy()
        require(
            previous["operationId"] == p["transaction"]["operation_id"]
            and previous["revision"] == 30
            and previous["ledgerIdentity"] == p["transaction"]["ledger_identity"]
            and current["revision"] == 31,
            "RECOVERY_CLAIM_INITIAL_INVALID",
        )
        require(
            current["steps"] == previous["steps"]
            and current["finalState"] == previous["finalState"]
            and current["recoveryStatus"] == previous["recoveryStatus"],
            "RECOVERY_CLAIM_MIXED_WITH_MUTATION",
        )


def validate_bound_ledger(ledger: Mapping[str, Any]) -> None:
    p = policy()
    require(
        ledger["operationId"] == p["transaction"]["operation_id"]
        and ledger["source"]
        == {"sha": p["subject"]["sha"], "tree": p["subject"]["tree"]},
        "RECOVERY_SUBJECT_INVALID",
    )
    require(
        ledger["planDigest"] == p["transaction"]["plan_digest"]
        and ledger["planIdentity"] == p["transaction"]["plan_identity"],
        "RECOVERY_PLAN_INVALID",
    )
    require(
        ledger["repository"] == p["repository"]
        and ledger["channel"] == "rc"
        and ledger["target"]
        == {"tag": p["subject"]["release_tag"], "version": p["subject"]["release_tag"]}
        and ledger["attemptLimit"] == 10,
        "RECOVERY_TRANSACTION_CHANGED",
    )
    require(
        ledger["expected"]
        == {
            "apiDigest": p["plan"]["api_digest"],
            "webDigest": p["plan"]["web_digest"],
            "assets": p["plan"]["assets"],
            "transportAssets": p["plan"]["transport_assets"],
        },
        "RECOVERY_ASSET_PLAN_CHANGED",
    )
    require(
        [
            {k: s[k] for k in ("name", "kind", "remoteKey", "expectedIdentity")}
            for s in ledger["steps"]
        ]
        == [
            {k: s[k] for k in ("name", "kind", "remoteKey", "expectedIdentity")}
            for s in p["steps"]
        ],
        "RECOVERY_STEP_SET_INVALID",
    )
    for step, expected in zip(ledger["steps"], p["steps"]):
        limit = expected["initialAttempts"] + int(
            step["name"].startswith("release-asset-")
            or step["name"] == "release-publish"
        )
        require(
            expected["initialAttempts"] <= len(step["attempts"]) <= limit,
            "RECOVERY_WRITE_ATTEMPT_EXCEEDED",
        )
        if expected["committed"]:
            require(step["committed"] is True, "RECOVERY_COMMITTED_REGRESSION")
