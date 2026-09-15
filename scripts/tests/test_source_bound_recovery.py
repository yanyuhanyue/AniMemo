from __future__ import annotations

import copy
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from release.publication_remote import GitHubResponse
from release.publication_transaction import (
    DurablePublicationController,
    PublicationTransactionError,
    _next_snapshot,
    _validate_recovery_transition,
    validate_ledger,
)
from release.recovery import RecoveryPlatform, RecoveryPrecheck
from release.recovery_contract import (
    RecoveryError,
    execution_title,
    identity,
    policy,
    validate_claim,
    validate_claim_transition,
    validate_execution,
)
from release.recovery_remote import GitHubRecoveryRemote, GuardedRecoveryJournal


def execution_fixture():
    p = policy()
    tool, tree, head = "a" * 40, "b" * 40, "c" * 40
    created = "2026-09-14T12:00:00Z"
    repo = {
        "id": p["repositoryId"],
        "full_name": p["repository"],
        "owner": {"id": p["ownerId"]},
    }
    actor = {"id": p["ownerId"], "login": p["operator"]}
    run = {
        "id": 901,
        "workflow_id": 902,
        "path": p["workflow"],
        "event": "workflow_dispatch",
        "head_branch": "main",
        "head_sha": tool,
        "repository": repo,
        "head_repository": repo,
        "run_attempt": 1,
        "actor": actor,
        "triggering_actor": actor,
        "created_at": created,
        "status": "in_progress",
        "conclusion": None,
        "display_title": execution_title("execute"),
    }
    event = {
        "inputs": {
            "operation": "execute",
            "authorization_scope": p["scope"],
            "candidate_acceptance_receipt_b64url": "fixture",
        },
        "sender": actor,
    }
    env = {
        "GITHUB_ACTIONS": "true",
        "GITHUB_EVENT_NAME": "workflow_dispatch",
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_RUN_ID": "901",
        "GITHUB_RUN_ATTEMPT": "1",
        "GITHUB_SHA": tool,
        "GITHUB_WORKFLOW_SHA": tool,
        "GITHUB_WORKFLOW_REF": f"{p['repository']}/{p['workflow']}@refs/heads/main",
    }
    pr = {
        "number": p["sourcePr"],
        "merged": True,
        "state": "closed",
        "merge_commit_sha": tool,
        "head": {"sha": head, "ref": p["sourceBranch"], "repo": repo},
        "base": {"ref": "main", "repo": repo},
    }
    old = p["previousTool"]
    previous_pr = json.loads(
        (
            Path(__file__).resolve().parents[2]
            / "release/fixtures/rc-pr255-api-2022-11-28.json"
        ).read_bytes()
    )["body"]
    superseded = {
        **run,
        "id": p["supersededFailure"]["runId"],
        "head_sha": old["sha"],
        "created_at": p["supersededFailure"]["createdAt"],
        "status": "completed",
        "conclusion": "failure",
        "display_title": "Existing RC recovery execute | "
        + p["supersededFailure"]["scope"],
    }
    return {
        "environment": env,
        "event": event,
        "run": run,
        "workflow": {
            "id": 902,
            "path": p["workflow"],
            "name": "Existing RC Recovery",
            "state": "active",
        },
        "repository": repo,
        "pull_request": pr,
        "main_sha": tool,
        "checkout_sha": tool,
        "checkout_tree": tree,
        "reviewed_tree": tree,
        "parent_sha": p["draftReadTool"]["sha"],
        "tool_commit": {
            "sha": tool,
            "tree": {"sha": tree},
            "parents": [{"sha": p["draftReadTool"]["sha"]}],
        },
        "parent_facts": (
            {
                **copy.deepcopy(pr),
                "number": p["parentTool"]["sourcePr"],
                "merge_commit_sha": p["parentTool"]["sha"],
                "head": {
                    "sha": p["parentTool"]["reviewedHead"],
                    "ref": p["parentTool"]["sourceBranch"],
                    "repo": repo,
                },
            },
            {
                "sha": p["parentTool"]["sha"],
                "tree": {"sha": p["parentTool"]["tree"]},
                "parents": [{"sha": old["sha"]}],
            },
            {
                "sha": p["parentTool"]["reviewedHead"],
                "tree": {"sha": p["parentTool"]["tree"]},
            },
        ),
        "diagnostic_facts": (
            {
                **copy.deepcopy(pr),
                "number": p["diagnosticTool"]["sourcePr"],
                "merge_commit_sha": p["diagnosticTool"]["sha"],
                "head": {
                    "sha": p["diagnosticTool"]["reviewedHead"],
                    "ref": p["diagnosticTool"]["sourceBranch"],
                    "repo": repo,
                },
            },
            {
                "sha": p["diagnosticTool"]["sha"],
                "tree": {"sha": p["diagnosticTool"]["tree"]},
                "parents": [{"sha": p["parentTool"]["sha"]}],
            },
            {
                "sha": p["diagnosticTool"]["reviewedHead"],
                "tree": {"sha": p["diagnosticTool"]["tree"]},
            },
        ),
        "draft_read_facts": (
            {
                **copy.deepcopy(pr),
                "number": p["draftReadTool"]["sourcePr"],
                "merge_commit_sha": p["draftReadTool"]["sha"],
                "head": {
                    "sha": p["draftReadTool"]["reviewedHead"],
                    "ref": p["draftReadTool"]["sourceBranch"],
                    "repo": repo,
                },
            },
            {
                "sha": p["draftReadTool"]["sha"],
                "tree": {"sha": p["draftReadTool"]["tree"]},
                "parents": [{"sha": p["diagnosticTool"]["sha"]}],
            },
            {
                "sha": p["draftReadTool"]["reviewedHead"],
                "tree": {"sha": p["draftReadTool"]["tree"]},
            },
        ),
        "previous_pr": previous_pr,
        "previous_commit": {
            "sha": old["sha"],
            "tree": {"sha": old["tree"]},
            "parents": [{"sha": p["subject"]["sha"]}],
        },
        "previous_reviewed": {"sha": old["reviewedHead"], "tree": {"sha": old["tree"]}},
        "superseded_run": superseded,
        "execution_runs": [
            superseded,
            run,
            {
                **run,
                "id": p["diagnosticTool"]["inspectionRun"],
                "head_sha": p["diagnosticTool"]["sha"],
                "display_title": execution_title("inspect"),
                "status": "completed",
                "conclusion": "failure",
                "created_at": "2026-09-14T11:58:00Z",
            },
            {
                **run,
                "id": p["draftReadTool"]["inspectionRun"],
                "head_sha": p["draftReadTool"]["sha"],
                "display_title": execution_title("inspect"),
                "status": "completed",
                "conclusion": "failure",
                "created_at": "2026-09-14T11:59:00Z",
            },
        ],
        "now": datetime(2026, 9, 14, 12, 1, tzinfo=timezone.utc),
    }


def initial_ledger():
    return json.loads(
        (
            Path(__file__).resolve().parents[2]
            / "release/fixtures/rc-recovery-initial-ledger.json"
        ).read_text(encoding="utf-8")
    )


class MemoryBackend:
    def __init__(self):
        self.value = initial_ledger()
        self.head = policy()["transaction"]["observed_head"]
        self.last_written_head = None
        self.before_append = None

    def load(self, operation_id):
        if operation_id != self.value["operationId"]:
            return None
        return copy.deepcopy(self.value)

    def _remote_head(self, operation_id):
        return self.head

    def append(self, value):
        if self.before_append:
            self.before_append()
        if (
            value["previousLedgerIdentity"] != self.value["ledgerIdentity"]
            or value["revision"] != self.value["revision"] + 1
        ):
            raise PublicationTransactionError("TRANSACTION_JOURNAL_APPEND_CONFLICT")
        _validate_recovery_transition(self.value, value)
        self.value = validate_ledger(value)
        self.head = identity(self.value)[7:47]
        self.last_written_head = self.head
        return copy.deepcopy(self.value)


class SourceBoundAuthorityTests(unittest.TestCase):
    def test_original_subject_and_new_merged_tool_are_distinct(self):
        fixture = execution_fixture()
        claim = validate_execution(**fixture)
        self.assertNotEqual(claim["toolSha"], claim["subject"]["sha"])
        self.assertEqual(claim["issuedAt"], "2026-09-14T09:55:15.462000Z")
        self.assertEqual(claim["expiresAt"], "2026-09-15T09:55:15.462000Z")
        self.assertEqual(claim["draftId"], 388147631)

    def test_untrusted_or_changed_platform_context_is_rejected(self):
        changes = [
            ("environment", "GITHUB_ACTIONS", "false"),
            ("environment", "GITHUB_REF", "refs/pull/255/merge"),
            ("environment", "GITHUB_WORKFLOW_SHA", "f" * 40),
            ("environment", "GITHUB_RUN_ATTEMPT", "2"),
            ("run", "event", "pull_request"),
            ("run", "run_attempt", 2),
            ("run", "run_attempt", True),
            ("run", "actor", {"id": 123, "login": "attacker"}),
            ("run", "head_repository", {"id": 123}),
            ("run", "head_sha", policy()["subject"]["sha"]),
            ("pull_request", "merged", False),
            ("workflow", "path", ".github/workflows/release.yml"),
        ]
        for group, key, value in changes:
            with self.subTest(group=group, key=key, value=value):
                fixture = execution_fixture()
                fixture[group] = {**fixture[group], key: value}
                with self.assertRaises(RecoveryError):
                    validate_execution(**fixture)
        for field, value in (
            ("main_sha", "f" * 40),
            ("checkout_tree", "f" * 40),
            ("parent_sha", "f" * 40),
            ("execution_runs", []),
        ):
            with self.subTest(field=field):
                fixture = execution_fixture()
                fixture[field] = value
                with self.assertRaises(RecoveryError):
                    validate_execution(**fixture)

    def test_first_failed_execution_consumes_opportunity_even_without_claim(self):
        fixture = execution_fixture()
        previous = {
            **fixture["run"],
            "id": 900,
            "created_at": "2026-09-14T11:59:00Z",
            "status": "completed",
            "conclusion": "failure",
        }
        fixture["execution_runs"] += [previous]
        with self.assertRaisesRegex(RecoveryError, "ALREADY_CONSUMED"):
            validate_execution(**fixture)

    def test_expiry_future_clock_nonce_and_unapproved_fields(self):
        for now in (
            datetime(2026, 9, 14, 11, 59, tzinfo=timezone.utc),
            datetime(2026, 9, 15, 12, tzinfo=timezone.utc),
        ):
            fixture = execution_fixture()
            fixture["now"] = now
            with self.assertRaises(RecoveryError):
                validate_execution(**fixture)
        claim = validate_execution(**execution_fixture())
        for update in (
            {"runId": 903},
            {"nonce": "sha256:" + "0" * 64},
            {"signature": "self-signed"},
            {"authorized": True},
            {"draftId": 373784357},
            {"expiresAt": "2026-09-16T12:00:00Z"},
        ):
            with self.subTest(update=update):
                altered = {**claim, **update}
                altered["bindingDigest"] = identity(
                    {k: v for k, v in altered.items() if k != "bindingDigest"}
                )
                with self.assertRaises(RecoveryError):
                    validate_claim(altered)

    def test_atomic_claim_cursor_and_second_claim_rejection(self):
        fixture = execution_fixture()
        claim = validate_execution(**fixture)
        backend = MemoryBackend()
        before_write = mock.Mock()
        make = lambda: GuardedRecoveryJournal(
            backend,
            claim,
            head_reader=lambda: backend.head,
            clock=lambda: fixture["now"],
            before_write=before_write,
        )
        first, competitor = make(), make()
        first.claim_once()
        self.assertEqual(first.cursor["revision"], 31)
        with self.assertRaises(RecoveryError):
            competitor.claim_once()
        next_value = _next_snapshot(first.cursor)
        first.append(next_value)
        self.assertEqual(first.cursor["revision"], 32)
        backend.head = "f" * 40
        with self.assertRaisesRegex(RecoveryError, "EXTERNAL_JOURNAL_CHANGE"):
            first.load(claim["operationId"])

    def test_claim_cannot_be_removed_replaced_or_reissue_original_attempts(self):
        claim = validate_execution(**execution_fixture())
        previous = initial_ledger()
        current = {**previous, "sourceBoundRecovery": claim}
        current = _next_snapshot(current)
        validate_claim_transition(previous, current)
        for mutate in (
            lambda x: x.pop("sourceBoundRecovery"),
            lambda x: x["sourceBoundRecovery"].__setitem__("runId", 903),
            lambda x: x["steps"][10]["attempts"][0]["readback"].__setitem__(
                "classification", "SAME"
            ),
        ):
            value = copy.deepcopy(current)
            mutate(value)
            with self.assertRaises(RecoveryError):
                validate_claim_transition(current, value)

    def test_ordinary_controller_cannot_write_claimed_transaction(self):
        fixture = execution_fixture()
        claim = validate_execution(**fixture)
        backend = MemoryBackend()
        guard = GuardedRecoveryJournal(
            backend,
            claim,
            head_reader=lambda: backend.head,
            clock=lambda: fixture["now"],
            before_write=lambda: None,
        )
        guard.claim_once()
        with self.assertRaisesRegex(
            PublicationTransactionError, "RECOVERY_ENTRY_REQUIRED"
        ):
            DurablePublicationController(
                initial=initial_ledger(),
                journal=backend,
                adapters={s["name"]: mock.Mock() for s in initial_ledger()["steps"]},
            )

    def test_precheck_refresh_does_not_extend_one_run_authorization(self):
        fixture = execution_fixture()
        claim = validate_execution(**fixture)
        now = [fixture["now"]]
        platform = mock.Mock()
        platform.now.side_effect = lambda: now[0]
        platform.validate_active.side_effect = lambda _claim: now[0]
        platform.remote.draft.return_value = {"id": 388147631}
        platform.tag.return_value = {"object": policy()["tagObject"]}
        with tempfile.TemporaryDirectory() as directory:
            precheck = RecoveryPrecheck(
                platform, claim, {}, MemoryBackend(), root=Path(directory)
            )
            with mock.patch.object(precheck, "snapshot", return_value={"same": True}):
                first = precheck.refresh()
                now[0] += timedelta(seconds=900)
                precheck.before_write()
                self.assertNotEqual(first["identity"], precheck.current["identity"])
                self.assertEqual(claim["expiresAt"], "2026-09-15T09:55:15.462000Z")
                self.assertEqual(
                    precheck.current["claimBinding"], claim["bindingDigest"]
                )

    def test_fixed_id_upload_is_single_request_and_never_redirects(self):
        p = policy()
        contents = "".join(
            p["plan"]["assets"][name]["sha256"][7:] + "  " + name + "\n"
            for name in (
                "release-manifest.json",
                "deployment-contract.json",
                "installer-materials.tar",
            )
        ).encode()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "checksums.txt"
            path.write_bytes(contents)
            remote = GitHubRecoveryRemote(output=root, before_send=lambda: None)
            release = {
                "id": 388147631,
                "assets": [],
                "upload_url": "https://uploads.github.com/repos/yanyuhanyue/AniMemo/releases/388147631/assets{?name,label}",
            }
            connection = mock.Mock()
            connection.getresponse.return_value.status = 302
            with (
                mock.patch.object(remote, "draft", return_value=release),
                mock.patch.dict("os.environ", {"GH_TOKEN": "fixture"}),
                mock.patch(
                    "release.recovery_remote.http.client.HTTPSConnection",
                    return_value=connection,
                ) as ctor,
            ):
                result = remote.upload(path)
            self.assertEqual(result.classification.value, "AMBIGUOUS")
            ctor.assert_called_once_with("uploads.github.com", timeout=300)
            connection.request.assert_called_once()
            self.assertEqual(
                connection.request.call_args.args[:2],
                (
                    "POST",
                    "/repos/yanyuhanyue/AniMemo/releases/388147631/assets?name=checksums.txt",
                ),
            )
            self.assertEqual(len(remote.write_requests), 1)

    def test_clock_rollback_and_expired_bulk_observation_are_rejected(self):
        now = execution_fixture()["now"]
        platform = RecoveryPlatform(
            mock.Mock(),
            Path.cwd(),
            {},
            {},
            clock=mock.Mock(side_effect=[now, now - timedelta(seconds=1)]),
        )
        platform.now()
        with self.assertRaisesRegex(RecoveryError, "CLOCK_ROLLBACK"):
            platform.now()
        claim = validate_execution(**execution_fixture())
        fake = mock.Mock()
        fake.now.side_effect = [now, now + timedelta(seconds=900)]
        with tempfile.TemporaryDirectory() as directory:
            precheck = RecoveryPrecheck(
                fake, claim, {}, MemoryBackend(), root=Path(directory)
            )
            with (
                mock.patch.object(precheck, "snapshot", return_value={}),
                self.assertRaisesRegex(RecoveryError, "PRECHECK_EXPIRED"),
            ):
                precheck.refresh()

    def test_wrong_upload_authority_fails_before_any_request(self):
        p = policy()
        contents = "".join(
            p["plan"]["assets"][n]["sha256"][7:] + "  " + n + "\n"
            for n in (
                "release-manifest.json",
                "deployment-contract.json",
                "installer-materials.tar",
            )
        ).encode()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "checksums.txt"
            path.write_bytes(contents)
            for url in (
                "https://example.invalid/upload",
                "https://uploads.github.com/repos/yanyuhanyue/AniMemo/releases/373784357/assets{?name,label}",
                "https://uploads.github.com.evil.invalid/upload",
            ):
                remote = GitHubRecoveryRemote(output=root)
                with (
                    mock.patch.object(
                        remote,
                        "draft",
                        return_value={"id": 388147631, "assets": [], "upload_url": url},
                    ),
                    mock.patch(
                        "release.recovery_remote.http.client.HTTPSConnection"
                    ) as connection,
                ):
                    with self.assertRaisesRegex(RecoveryError, "UPLOAD_URL_INVALID"):
                        remote.upload(path)
                    connection.assert_not_called()
                    self.assertEqual(remote.write_requests, [])

    def test_authenticated_asset_bytes_reject_truncation_and_corruption(self):
        import hashlib
        import io

        expected = {
            "size": 4,
            "sha256": "sha256:" + hashlib.sha256(b"good").hexdigest(),
        }
        GitHubRecoveryRemote._verify_stream(io.BytesIO(b"good"), expected)
        for raw in (b"bad!", b"goo", b"goodextra"):
            with self.assertRaisesRegex(RecoveryError, "BYTES_CONFLICT"):
                GitHubRecoveryRemote._verify_stream(io.BytesIO(raw), expected)

    def test_pagination_rejects_count_drift_and_duplicate_ids(self):
        with tempfile.TemporaryDirectory() as directory:
            remote = GitHubRecoveryRemote(output=Path(directory))
            for responses in (
                [{"total_count": 2, "items": [{"id": 1}]}],
                [{"total_count": 2, "items": [{"id": 1}, {"id": 1}]}],
                [{"total_count": True, "items": [{"id": 1}]}],
            ):
                with (
                    mock.patch.object(
                        remote,
                        "get_response",
                        side_effect=[
                            GitHubResponse(200, json.dumps(value).encode())
                            for value in responses
                        ],
                    ),
                    self.assertRaises(RecoveryError),
                ):
                    remote.listed(remote.base + "/test", "items")

    def test_assets_short_page_follows_only_valid_same_endpoint_links(self):
        from release.publication_remote import GitHubResponse

        with tempfile.TemporaryDirectory() as directory:
            remote = GitHubRecoveryRemote(output=Path(directory))
            endpoint = remote.base + "/releases/388147631/assets"
            link = (
                f'<https://api.github.com/{endpoint}?per_page=100&page=2>; rel="next"'
            )
            with mock.patch.object(
                remote,
                "get_response",
                side_effect=[
                    GitHubResponse(200, b'[{"id":1}]', link),
                    GitHubResponse(200, b'[{"id":2}]'),
                ],
            ) as get:
                self.assertEqual(remote.listed(endpoint), [{"id": 1}, {"id": 2}])
                self.assertEqual(get.call_count, 2)
            for bad in (
                link.replace("api.github.com", "evil.invalid"),
                link.replace('rel="next"', 'rel="last"'),
                link.replace("/388147631/", "/373784357/"),
            ):
                with (
                    mock.patch.object(
                        remote,
                        "get_response",
                        return_value=GitHubResponse(200, b'[{"id":1}]', bad),
                    ),
                    self.assertRaises(RecoveryError),
                ):
                    remote.listed(endpoint)
                self.assertEqual(remote.write_requests, [])

    def test_final_send_guard_stops_expired_upload_publish_and_journal_push(self):
        from release.publication_transaction import (
            GitCommandResult,
            GitRemoteAppendOnlyJournal,
        )

        p = policy()
        fixture = execution_fixture()
        claim = validate_execution(**fixture)
        now = [fixture["now"]]
        platform = mock.Mock()
        platform.now.side_effect = lambda: now[0]
        contents = "".join(
            p["plan"]["assets"][n]["sha256"][7:] + "  " + n + "\n"
            for n in (
                "release-manifest.json",
                "deployment-contract.json",
                "installer-materials.tar",
            )
        ).encode()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "checksums.txt"
            path.write_bytes(contents)
            precheck = RecoveryPrecheck(platform, claim, {}, MemoryBackend(), root=root)
            precheck.current = {
                "claimBinding": claim["bindingDigest"],
                "observedAt": "2026-09-14T12:00:00Z",
                "expiresAt": "2026-09-14T12:15:00Z",
            }

            def slow_draft(**_kwargs):
                now[0] = datetime(2026, 9, 14, 12, 15, tzinfo=timezone.utc)
                return {
                    "id": 388147631,
                    "assets": [],
                    "upload_url": "https://uploads.github.com/repos/yanyuhanyue/AniMemo/releases/388147631/assets{?name,label}",
                }

            remote = GitHubRecoveryRemote(
                output=root, before_send=precheck.final_send_check
            )
            with (
                mock.patch.object(remote, "draft", side_effect=slow_draft),
                mock.patch.dict("os.environ", {"GH_TOKEN": "fixture"}),
                mock.patch(
                    "release.recovery_remote.http.client.HTTPSConnection"
                ) as connection,
                self.assertRaisesRegex(RecoveryError, "PRECHECK_EXPIRED"),
            ):
                remote.upload(path)
            connection.assert_not_called()
            self.assertEqual(remote.write_requests, [])
            with (
                mock.patch.object(
                    remote, "draft", return_value={"id": 388147631, "assets": [{}] * 5}
                ),
                mock.patch("release.recovery_remote.github_request") as request,
                self.assertRaisesRegex(RecoveryError, "PRECHECK_EXPIRED"),
            ):
                remote.publish()
            request.assert_not_called()
            self.assertEqual(remote.write_requests, [])
            backend = GitRemoteAppendOnlyJournal(root)
            backend.before_push = precheck.final_send_check
            initial = initial_ledger()
            value = _next_snapshot({**initial, "sourceBoundRecovery": claim})
            commands = []

            def git(*args, **_kwargs):
                commands.append(args)
                return GitCommandResult(
                    0,
                    (
                        (claim["initialHead"] if args[0] == "rev-parse" else "f" * 40)
                        + "\n"
                    ).encode(),
                    b"",
                )

            with (
                mock.patch.object(backend, "load", return_value=initial),
                mock.patch.object(
                    backend, "_remote_head", return_value=claim["initialHead"]
                ),
                mock.patch.object(backend, "_git", side_effect=git),
                self.assertRaisesRegex(RecoveryError, "PRECHECK_EXPIRED"),
            ):
                backend.append(value)
            self.assertFalse(any(command[0] == "push" for command in commands))

    def test_dispatch_never_restarts_fixed_user_authorization_window(self):
        fixture = execution_fixture()
        fixture["run"]["created_at"] = "2026-09-15T10:00:00Z"
        fixture["now"] = datetime(2026, 9, 15, 10, 1, tzinfo=timezone.utc)
        with self.assertRaisesRegex(RecoveryError, "AUTHORIZATION_EXPIRED"):
            validate_execution(**fixture)

    def test_actual_workflow_has_only_existing_release_permissions_and_fixed_cli(self):
        import yaml

        root = Path(__file__).resolve().parents[2]
        workflow = yaml.safe_load(
            (root / ".github/workflows/release-recovery.yml").read_text(
                encoding="utf-8"
            )
        )
        trigger = workflow.get("on", workflow.get(True))
        self.assertEqual(set(trigger), {"workflow_dispatch"})
        self.assertEqual(
            trigger["workflow_dispatch"]["inputs"]["operation"]["default"], "inspect"
        )
        self.assertFalse(workflow["concurrency"]["cancel-in-progress"])
        self.assertEqual(
            workflow["jobs"]["recover"]["permissions"],
            {
                "contents": "write",
                "actions": "read",
                "packages": "read",
                "attestations": "read",
                "pull-requests": "read",
            },
        )
        for job in workflow["jobs"].values():
            commands = [step.get("run", "") for step in job["steps"]]
            self.assertIn("python -m scripts.release_recovery", commands)
            self.assertIn("bash scripts/mask-candidate-receipt.sh", commands)
            self.assertFalse(any("${{ inputs." in command for command in commands))
            self.assertFalse(job["steps"][0]["with"]["persist-credentials"])

    def test_actual_cli_rejects_local_execution_before_any_transport(self):
        import contextlib
        import io

        from scripts import release_recovery

        with (
            mock.patch.dict("os.environ", {"GITHUB_ACTIONS": "false"}),
            mock.patch.object(release_recovery, "GitHubRecoveryRemote") as remote,
            contextlib.redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(release_recovery.main(), 2)
            remote.assert_not_called()

    def test_runner_paths_are_derived_from_checkout_and_reject_environment_redirection(
        self,
    ):
        from scripts.release_recovery import trusted_runner_paths

        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory).resolve()
            repo = work / "AniMemo" / "AniMemo"
            repo.mkdir(parents=True)
            temporary = work / "_temp"
            (temporary / "_github_workflow").mkdir(parents=True)
            event = temporary / "_github_workflow" / "event.json"
            event.write_bytes(b"{}")
            environment = {
                "RUNNER_TEMP": temporary.as_posix(),
                "GITHUB_EVENT_PATH": event.as_posix(),
            }
            self.assertEqual(
                trusted_runner_paths(repo, environment),
                (event, temporary / "animemo-existing-rc-recovery"),
            )
            for update in (
                {"RUNNER_TEMP": "/etc"},
                {"GITHUB_EVENT_PATH": "/etc/passwd"},
                {"RUNNER_TEMP": temporary.as_posix() + "/../_temp"},
            ):
                with (
                    self.subTest(update=update),
                    self.assertRaisesRegex(RecoveryError, "RUNNER_PATHS_INVALID"),
                ):
                    trusted_runner_paths(repo, {**environment, **update})

    def test_proof_commands_require_complete_fixed_arguments(self):
        from release.recovery_evidence import fixed_proof_runner

        p = policy()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            runner = fixed_proof_runner(root)
            valid = (
                "gh",
                "release",
                "verify-asset",
                p["subject"]["release_tag"],
                str(root / next(iter(p["plan"]["transport_assets"]))),
                "--repo",
                p["repository"],
                "--format",
                "json",
            )
            with mock.patch(
                "release.recovery_evidence.subprocess.run",
                return_value=mock.Mock(returncode=0, stdout=b"[]"),
            ) as execute:
                self.assertEqual(runner(valid), b"[]")
                self.assertEqual(execute.call_args.args[0], valid)
                execute.reset_mock()
                for command in (
                    valid + ("--insecure",),
                    ("evil", *valid[1:]),
                    (*valid[:4], "/tmp/other", *valid[5:]),
                    (
                        "gh",
                        "attestation",
                        "verify",
                        "oci://evil.invalid/image",
                        "--repo",
                        p["repository"],
                    ),
                ):
                    with (
                        self.subTest(command=command),
                        self.assertRaisesRegex(RecoveryError, "PROOF_COMMAND_INVALID"),
                    ):
                        runner(command)
                execute.assert_not_called()

    def test_cli_existing_output_is_preserved_before_transport(self):
        import contextlib
        import io

        from scripts import release_recovery

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            event = root / "event.json"
            event.write_bytes(b"{}")
            output = root / "existing"
            (output / "evidence").mkdir(parents=True)
            sentinel = output / "evidence" / "failure.json"
            sentinel.write_bytes(b"previous evidence")
            with (
                mock.patch.dict(os.environ, {"GITHUB_ACTIONS": "true"}),
                mock.patch.object(
                    release_recovery,
                    "trusted_runner_paths",
                    return_value=(event, output),
                ),
                mock.patch.object(release_recovery, "GitHubRecoveryRemote") as remote,
                contextlib.redirect_stderr(io.StringIO()),
            ):
                self.assertEqual(release_recovery.main(), 2)
            remote.assert_not_called()
            self.assertEqual(sentinel.read_bytes(), b"previous evidence")

    @unittest.skipUnless(
        os.environ.get("GITHUB_ACTIONS") == "true" and sys.platform == "linux",
        "Actual hosted runner layout is verified on Linux CI",
    )
    def test_actual_hosted_runner_paths_match_reviewed_entry(self):
        import os

        from scripts.release_recovery import trusted_runner_paths

        event, output = trusted_runner_paths(
            Path(__file__).resolve().parents[2], os.environ
        )
        self.assertEqual(event.name, "event.json")
        self.assertEqual(output.name, "animemo-existing-rc-recovery")


if __name__ == "__main__":
    unittest.main()
