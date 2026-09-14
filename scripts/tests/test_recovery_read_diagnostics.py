"""Local diagnostic delta: real HTTP boundary failures remain read-only."""

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from release.recovery_contract import RecoveryError, execution_title, policy
from release.recovery_remote import GitHubRecoveryRemote
from scripts.release_publication_controller import (
    ControllerReleaseAuthorityError,
    _GitHubReadOnlyObservationBoundary,
)
from scripts.tests.recovery_http_fixture import FixtureHTTP, ProductionFixturePlatform


class RecoveryReadDiagnosticsTests(unittest.TestCase):
    def test_failed_pr_read_replaces_previous_success_diagnostic(self):
        http = FixtureHTTP()
        http.statuses[http.base + f"/pulls/{policy()['sourcePr']}"] = 403
        with tempfile.TemporaryDirectory() as directory, http.installed():
            remote = GitHubRecoveryRemote(output=Path(directory), read_only=True)
            remote.get(remote.base)
            self.assertEqual(remote.last_read_diagnostic["status"], 200)
            with self.assertRaises(RecoveryError):
                remote.merge_identity(policy()["sourcePr"], policy()["sourceBranch"])
            self.assertEqual(
                remote.last_read_diagnostic["endpointCategory"], "PR_MERGE_IDENTITY"
            )
            self.assertEqual(remote.last_read_diagnostic["status"], 403)
            self.assertEqual(remote.last_read_diagnostic["sequence"], 2)

    def test_actual_http_failure_identifies_endpoint_role_and_status_without_body(self):
        for suffix, role, category in (
            ("/branches/main/protection", "ADMIN_READ", "BRANCH_PROTECTION"),
            ("/immutable-releases", "ADMIN_READ", "IMMUTABLE_SETTING"),
            ("/releases/373784357", "GITHUB_TOKEN", "ORDINARY_DRAFT"),
        ):
            for status in (403, 404, 503):
                http = FixtureHTTP()
                http.statuses[http.base + suffix] = status
                http.raw[http.base + suffix] = b'{"message":"private-body-secret"}'
                with tempfile.TemporaryDirectory() as directory, http.installed():
                    root = Path(directory)
                    remote = GitHubRecoveryRemote(output=root, read_only=True)
                    with self.assertRaises(RecoveryError):
                        ProductionFixturePlatform(remote, root).execution()
                    last = remote.last_read_diagnostic
                    self.assertEqual(
                        (
                            last["endpointCategory"],
                            last["credentialRole"],
                            last["status"],
                        ),
                        (category, role, status),
                    )
                    diagnostic = (root / "http-read-diagnostics.jsonl").read_text()
                    self.assertNotIn("private-body-secret", diagnostic)
                    self.assertNotIn("synthetic-test-token", diagnostic)
                    self.assertNotIn("Authorization", diagnostic)
                    self.assertEqual(remote.write_requests, [])

    def test_no_response_is_distinct_from_an_http_failure(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch(
                "release.recovery_remote.github_request",
                side_effect=ConnectionError("private-secret"),
            ),
        ):
            remote = GitHubRecoveryRemote(output=Path(directory), read_only=True)
            with self.assertRaises(ConnectionError):
                remote.get(remote.base)
            self.assertEqual(remote.last_read_diagnostic["phase"], "NO_RESPONSE")
            self.assertIsNone(remote.last_read_diagnostic["status"])
            self.assertNotIn(
                "private-secret",
                (Path(directory) / "http-read-diagnostics.jsonl").read_text(),
            )

    def test_real_run_name_shape_is_not_a_workflow_definition_name(self):
        actual = json.loads(
            (
                Path(__file__).resolve().parents[2]
                / "release/fixtures/rc-inspect-run-response.json"
            ).read_bytes()
        )["body"]
        self.assertEqual(actual["name"], actual["display_title"])
        self.assertNotEqual(actual["name"], "Existing RC Recovery")
        # Explicitly synthetic successful execute, preserving the observed API shape.
        run = {
            **actual,
            "name": execution_title("execute"),
            "display_title": execution_title("execute"),
            "status": "completed",
            "conclusion": "success",
        }
        workflow = {
            "id": run["workflow_id"],
            "name": "Existing RC Recovery",
            "path": policy()["workflow"],
            "state": "active",
        }
        boundary = object.__new__(_GitHubReadOnlyObservationBoundary)
        with mock.patch.object(boundary, "_gh_json", return_value=workflow):
            boundary._validate_recovery_workflow(run)
            boundary._validate_run(
                run,
                run_id=run["id"],
                name=None,
                path=policy()["workflow"],
                head=run["head_sha"],
                events=frozenset({"workflow_dispatch"}),
                head_branches=frozenset({"main"}),
            )
            for update in (
                {"workflow_id": 1},
                {"display_title": execution_title("inspect")},
            ):
                with self.assertRaises(ControllerReleaseAuthorityError):
                    boundary._validate_recovery_workflow(
                        {**copy.deepcopy(run), **update}
                    )
