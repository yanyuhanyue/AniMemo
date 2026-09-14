"""R3 authorization and diagnostics through the production HTTP/platform boundary."""

import contextlib
import copy
import hashlib
import http.client
import io
import json
import subprocess
import tempfile
import unittest
import urllib.error
from pathlib import Path
from typing import ClassVar
from unittest import mock

from release.recovery_contract import RecoveryError, execution_title, policy
from release.recovery_remote import GitHubRecoveryRemote
from scripts.tests.recovery_http_fixture import FixtureHTTP, ProductionFixturePlatform
from scripts.tests.test_source_bound_recovery import execution_fixture


class ToolchainDiagnosticsTests(unittest.TestCase):
    def test_direct_consumer_keeps_failed_http_headers_and_strips_binary_safely(self):
        from scripts.release_publication_controller import (
            ControllerReleaseAuthorityError,
            _GitHubReadOnlyObservationBoundary,
        )

        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "synthetic-gh.exe"
            executable.write_bytes(b"synthetic executable identity")
            boundary = _GitHubReadOnlyObservationBoundary(
                gh_executable=str(executable),
                gh_sha256="sha256:"
                + hashlib.sha256(executable.read_bytes()).hexdigest(),
            )
            boundary._recovery_read_diagnostics = True
            for status in (200, 403):
                body = b"original\r\n\r\nX-Github-Request-Id: body-secret"
                raw = (
                    f"HTTP/2.0 {status} Status\nX-Github-Request-Id: safe-123\n\n".encode()
                    + body
                )
                captured = io.StringIO()
                with (
                    mock.patch(
                        "subprocess.run",
                        return_value=subprocess.CompletedProcess(
                            [], 0 if status == 200 else 1, raw, b"private-secret"
                        ),
                    ),
                    contextlib.redirect_stderr(captured),
                ):
                    if status == 200:
                        self.assertEqual(
                            boundary._run(
                                (
                                    "gh",
                                    "api",
                                    "--method",
                                    "GET",
                                    "repos/yanyuhanyue/AniMemo/actions/artifacts/123/zip",
                                )
                            ),
                            body,
                        )
                    else:
                        with self.assertRaises(ControllerReleaseAuthorityError):
                            boundary._gh_json(
                                "repos/yanyuhanyue/AniMemo/git/ref/heads/main"
                            )
                self.assertEqual(boundary._last_recovery_read["status"], status)
                self.assertEqual(boundary._last_recovery_read["requestId"], "safe-123")
                self.assertNotIn("body-secret", captured.getvalue())
                self.assertNotIn("private-secret", captured.getvalue())

    def test_asset_redirect_logs_actual_anonymous_cdn_role_without_url(self):
        from release.publication_remote import _open_github_asset_stream
        from scripts.tests.recovery_http_fixture import Response

        url = "https://api.github.com/repos/yanyuhanyue/AniMemo/releases/assets/1"
        redirect = urllib.error.HTTPError(
            url,
            302,
            "redirect",
            {
                "Location": "https://release-assets.githubusercontent.com/secret-signed-url"
            },
            io.BytesIO(),
        )
        with tempfile.TemporaryDirectory() as directory:
            remote = GitHubRecoveryRemote(output=Path(directory), read_only=True)
            with (
                mock.patch(
                    "urllib.request.OpenerDirector.open",
                    side_effect=[redirect, Response(b"original", headers={})],
                ),
                remote.open_read_stream(
                    lambda: _open_github_asset_stream(
                        url, "secret-token", observe=remote.asset_observer()
                    ),
                    category="AUTHENTICATED_ASSET",
                    role="GITHUB_TOKEN",
                    traced=True,
                ) as stream,
            ):
                self.assertEqual(stream.read(), b"original")
            log = (Path(directory) / "http-read-diagnostics.jsonl").read_text()
            rows = [json.loads(line) for line in log.splitlines()]
            self.assertEqual([r["status"] for r in rows], [None, 302, None, 200])
            self.assertEqual(rows[-1]["credentialRole"], "ANONYMOUS")
            self.assertNotIn("secret-signed-url", log)
            self.assertNotIn("secret-token", log)

    def test_received_http_headers_survive_body_interruption_for_each_role(self):
        class Broken(io.BytesIO):
            status = 200
            headers: ClassVar = {
                "X-GitHub-Request-Id": "safe-123",
                "X-GitHub-Api-Version-Selected": "2026-03-10",
            }

            def read(self, *args):
                raise http.client.IncompleteRead(b"private-secret")

        for endpoint, admin in (
            ("", False),
            ("/immutable-releases", True),
            ("PR", False),
        ):
            with tempfile.TemporaryDirectory() as directory, FixtureHTTP().installed():
                remote = GitHubRecoveryRemote(output=Path(directory), read_only=True)
                with (
                    mock.patch(
                        "urllib.request.OpenerDirector.open", return_value=Broken()
                    ),
                    self.assertRaises(ConnectionError),
                ):
                    if endpoint == "PR":
                        remote.merge_identity(
                            policy()["sourcePr"], policy()["sourceBranch"]
                        )
                    else:
                        remote.get(remote.base + endpoint, administration=admin)
                self.assertEqual(remote.last_read_diagnostic["status"], 200)
                self.assertEqual(remote.last_read_diagnostic["requestId"], "safe-123")
                self.assertEqual(
                    remote.last_read_diagnostic["phase"], "RESPONSE_BODY_ERROR"
                )
                self.assertNotIn(
                    "private-secret", json.dumps(remote.last_read_diagnostic)
                )

    def test_artifact_headers_are_diagnosed_and_binary_bytes_are_unchanged(self):
        raw = b"PK\x03\x04\r\n\r\n\x00synthetic-binary"
        expected = {
            "id": 12,
            "size_in_bytes": len(raw),
            "digest": "sha256:" + hashlib.sha256(raw).hexdigest(),
        }
        for status in (200, 403):
            response = (
                f"HTTP/2.0 {status} Status\nX-Github-Request-Id: safe-123\n\n".encode()
                + raw
            )
            with tempfile.TemporaryDirectory() as directory:
                remote = GitHubRecoveryRemote(output=Path(directory), read_only=True)
                with mock.patch(
                    "subprocess.run",
                    return_value=subprocess.CompletedProcess(
                        [], 0 if status == 200 else 1, response, b"private-secret"
                    ),
                ):
                    if status == 200:
                        remote.download_artifact(
                            expected, Path(directory) / "artifact.zip"
                        )
                        self.assertEqual(
                            (Path(directory) / "artifact.zip").read_bytes(), raw
                        )
                    else:
                        with self.assertRaises(RecoveryError):
                            remote.download_artifact(
                                expected, Path(directory) / "artifact.zip"
                            )
                self.assertEqual(remote.last_read_diagnostic["status"], status)
                self.assertEqual(
                    remote.last_read_diagnostic["endpointCategory"],
                    "QUALIFICATION_ARTIFACT_DOWNLOAD",
                )

    def test_opaque_proof_failure_is_explicitly_unknown_http_status(self):
        with tempfile.TemporaryDirectory() as directory:
            remote = GitHubRecoveryRemote(output=Path(directory), read_only=True)
            with (
                mock.patch(
                    "subprocess.run",
                    return_value=subprocess.CompletedProcess(
                        [], 1, b"", b"private-secret"
                    ),
                ),
                self.assertRaisesRegex(RecoveryError, "ORIGINAL_PROOF_UNKNOWN"),
            ):
                remote.verify_proofs(Path(directory))
            self.assertEqual(remote.last_read_diagnostic["phase"], "COMMAND_RESULT")
            self.assertEqual(
                remote.last_read_diagnostic["endpointCategory"], "ORIGINAL_PROOF_VERIFY"
            )
            self.assertIsNone(remote.last_read_diagnostic["status"])
            self.assertNotIn("private-secret", json.dumps(remote.last_read_diagnostic))

    def run_platform(self, fixture):
        http = FixtureHTTP(fixture)
        with tempfile.TemporaryDirectory() as directory, http.installed():
            remote = GitHubRecoveryRemote(output=Path(directory), read_only=True)
            return ProductionFixturePlatform(
                remote, Path(directory), fixture
            ).execution()

    def test_three_single_parent_hops_are_verified_at_http_boundary(self):
        f = execution_fixture()
        self.assertEqual(self.run_platform(f)["parentTool"], policy()["parentTool"])
        for member, update in (
            (0, {"merge_commit_sha": "f" * 40}),
            (0, {"head": {**f["parent_facts"][0]["head"], "sha": "f" * 40}}),
            (1, {"parents": [{"sha": "f" * 40}]}),
            (
                1,
                {
                    "parents": [
                        {"sha": policy()["previousTool"]["sha"]},
                        {"sha": "f" * 40},
                    ]
                },
            ),
            (2, {"tree": {"sha": "f" * 40}}),
        ):
            changed = copy.deepcopy(f)
            changed["parent_facts"][member].update(update)
            with (
                self.subTest(member=member, update=update),
                self.assertRaises(RecoveryError),
            ):
                self.run_platform(changed)

    def test_execute_is_consumed_across_any_tool_head_or_branch(self):
        for head in (policy()["parentTool"]["sha"], "e" * 40, "f" * 40):
            f = execution_fixture()
            f["execution_runs"].append(
                {
                    **f["run"],
                    "id": 900,
                    "head_sha": head,
                    "head_branch": "different-tool",
                    "status": "completed",
                    "conclusion": "failure",
                    "created_at": "2026-09-14T11:59:00Z",
                }
            )
            with (
                self.subTest(head=head),
                self.assertRaisesRegex(RecoveryError, "ALREADY_CONSUMED"),
            ):
                self.run_platform(f)

    def test_inspection_allowance_shared_and_never_produces_write_steps(self):
        f = execution_fixture()
        f["run"]["display_title"] = execution_title("inspect")
        f["event"]["inputs"]["operation"] = "inspect"
        for i in (800,):
            f["execution_runs"].append({**f["run"], "id": i, "head_sha": "e" * 40})
        self.assertEqual(self.run_platform(f)["writeSteps"], [])
        f["execution_runs"].append({**f["run"], "id": 802})
        with self.assertRaisesRegex(RecoveryError, "INSPECT_ALLOWANCE"):
            self.run_platform(f)

    def test_workflow_definition_name_is_required_but_run_name_can_be_dynamic(self):
        f = execution_fixture()
        f["run"]["name"] = f["run"]["display_title"]
        self.run_platform(f)
        f["workflow"]["name"] = "Different workflow"
        with self.assertRaisesRegex(RecoveryError, "WORKFLOW_INVALID"):
            self.run_platform(f)

    def test_log_failure_preserves_original_http_failure_in_memory(self):
        http = FixtureHTTP()
        http.statuses[http.base] = 403
        with tempfile.TemporaryDirectory() as directory, http.installed():
            remote = GitHubRecoveryRemote(output=Path(directory), read_only=True)
            with (
                mock.patch.object(Path, "open", side_effect=OSError("private-secret")),
                self.assertRaisesRegex(RecoveryError, "REMOTE_READ_UNKNOWN"),
            ):
                remote.get(remote.base)
            self.assertTrue(remote.diagnostics_incomplete)
            self.assertEqual(remote.last_read_diagnostic["status"], 403)
            self.assertNotIn("private-secret", json.dumps(remote.last_read_diagnostic))

    def test_non_pr_selected_version_conflict_and_unsafe_headers_fail_safely(self):
        http = FixtureHTTP()
        http.headers[http.base] = {
            "X-GitHub-Api-Version-Selected": "2022-11-28",
            "X-GitHub-Request-Id": "private-secret\nHeader: value",
        }
        with tempfile.TemporaryDirectory() as directory, http.installed():
            remote = GitHubRecoveryRemote(output=Path(directory), read_only=True)
            with self.assertRaisesRegex(RecoveryError, "API_VERSION_CONFLICT"):
                remote.get(remote.base)
            self.assertIsNone(remote.last_read_diagnostic["requestId"])
            self.assertEqual(remote.last_read_diagnostic["status"], 200)

    def test_pagination_shape_and_count_are_not_empty_success(self):
        for value in ({"total_count": 0}, {"total_count": 1, "workflow_runs": []}):
            http = FixtureHTTP()
            endpoint = "/actions/workflows/902/runs?event=workflow_dispatch"
            http.values[http.base + endpoint + "&per_page=100&page=1"] = value
            with tempfile.TemporaryDirectory() as directory, http.installed():
                remote = GitHubRecoveryRemote(output=Path(directory), read_only=True)
                with self.assertRaisesRegex(RecoveryError, "PAGINATION"):
                    remote.listed(remote.base + endpoint, "workflow_runs")
                self.assertEqual(remote.last_read_diagnostic["status"], 200)
                self.assertEqual(remote.write_requests, [])
