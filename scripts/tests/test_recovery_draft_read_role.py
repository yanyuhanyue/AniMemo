"""Native job token serves fixed draft GETs after the R4 ADMIN_READ denial."""

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from release.recovery_contract import RecoveryError, policy
from release.recovery_remote import GitHubRecoveryRemote
from scripts.tests.recovery_http_fixture import FixtureHTTP, ProductionFixturePlatform
from scripts.tests.test_source_bound_recovery import execution_fixture


class DraftReadRoleTests(unittest.TestCase):
    def test_production_draft_discovery_and_ordinary_get_use_existing_approved_role(
        self,
    ):
        http = FixtureHTTP()
        draft = json.loads(
            (
                Path(__file__).resolve().parents[2]
                / "release/fixtures/rc-original-draft-response.json"
            ).read_bytes()
        )
        http.values.update(
            {
                http.base + "/releases/tags/v2.0.0-rc.1": {},
                http.base + "/releases?per_page=100&page=1": [draft],
                http.base + "/releases/388147631": draft,
                http.base + "/releases/388147631/assets?per_page=100&page=1": [],
            }
        )
        http.statuses[http.base + "/releases/tags/v2.0.0-rc.1"] = 404
        checked = []

        def open_request(request, *args, **kwargs):
            if "/releases" in request.full_url:
                self.assertEqual(
                    dict(request.header_items())["Authorization"],
                    "Bearer synthetic-test-token",
                )
                checked.append(request.full_url)
            return http.open(request, *args, **kwargs)

        with tempfile.TemporaryDirectory() as directory, http.installed():
            remote = GitHubRecoveryRemote(output=Path(directory), read_only=True)
            with mock.patch(
                "urllib.request.OpenerDirector.open", side_effect=open_request
            ):
                ProductionFixturePlatform(remote, Path(directory)).execution()
                self.assertEqual(remote.draft()["id"], 388147631)
            self.assertEqual(
                checked,
                [
                    http.base + suffix
                    for suffix in (
                        "/releases/373784357",
                        "/releases/tags/v2.0.0-rc.1",
                        "/releases?per_page=100&page=1",
                        "/releases/388147631",
                        "/releases/388147631",
                        "/releases/388147631/assets?per_page=100&page=1",
                    )
                ],
            )  # Discovery refresh and recovery identity validation each read the ID.
            self.assertEqual(
                remote.last_read_diagnostic["credentialRole"], "GITHUB_TOKEN"
            )
            self.assertEqual(remote.write_requests, [])

    def test_approved_role_denial_never_falls_back_or_retries(self):
        http = FixtureHTTP()
        url = http.base + "/releases/373784357"
        http.statuses[url] = 403
        with tempfile.TemporaryDirectory() as directory, http.installed():
            remote = GitHubRecoveryRemote(output=Path(directory), read_only=True)
            with self.assertRaisesRegex(RecoveryError, "REMOTE_READ_UNKNOWN"):
                ProductionFixturePlatform(remote, Path(directory)).execution()
            self.assertEqual(sum(r["url"] == url for r in http.requests), 1)
            self.assertEqual(remote.last_read_diagnostic["status"], 403)
            self.assertEqual(
                remote.last_read_diagnostic["credentialRole"], "GITHUB_TOKEN"
            )
            with self.assertRaisesRegex(RecoveryError, "READ_ONLY"):
                remote._final_send_check()

    def test_read_credential_route_rejects_unapproved_ids_queries_and_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            remote = GitHubRecoveryRemote(output=Path(directory), read_only=True)
            for suffix in (
                "/releases/123",
                "/releases?per_page=100&page=1&extra=x",
                "/releases?per_page=100&page=101",
                "/releases/388147631?extra=x",
                "/releases/373784357/assets?per_page=100&page=1",
            ):
                with (
                    self.subTest(suffix=suffix),
                    self.assertRaisesRegex(RecoveryError, "ADMIN_TARGET_INVALID"),
                ):
                    remote._get_response(remote.base + suffix, administration=True)
            for method in ("POST", "PATCH", "DELETE"):
                with self.assertRaisesRegex(RecoveryError, "READONLY_BOUNDARY"):
                    remote.readonly_request(method, remote.base + "/releases", {})

    def test_exact_r3_hop_and_failed_inspection_are_required(self):
        for change in (
            "parent",
            "reviewed",
            "missing_inspect",
            "inspect_success",
            "inspect_attempt",
        ):
            fixture = copy.deepcopy(execution_fixture())
            if change == "parent":
                fixture["diagnostic_facts"][1]["parents"] = [
                    {"sha": policy()["previousTool"]["sha"]}
                ]
            elif change == "reviewed":
                fixture["diagnostic_facts"][2]["tree"]["sha"] = "f" * 40
            elif change == "missing_inspect":
                fixture["execution_runs"] = [
                    r
                    for r in fixture["execution_runs"]
                    if r["id"] != policy()["diagnosticTool"]["inspectionRun"]
                ]
            else:
                run = next(
                    r
                    for r in fixture["execution_runs"]
                    if r["id"] == policy()["diagnosticTool"]["inspectionRun"]
                )
                run["conclusion" if change == "inspect_success" else "run_attempt"] = (
                    "success" if change == "inspect_success" else 2
                )
            with (
                tempfile.TemporaryDirectory() as directory,
                FixtureHTTP(fixture).installed(),
            ):
                remote = GitHubRecoveryRemote(output=Path(directory), read_only=True)
                with self.subTest(change=change), self.assertRaises(RecoveryError):
                    ProductionFixturePlatform(
                        remote, Path(directory), fixture
                    ).execution()
