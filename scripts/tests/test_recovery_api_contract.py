"""HTTP contract regression at the production request boundary."""

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from release.recovery import ExistingRCRecovery
from release.recovery_contract import (
    RecoveryError,
    execution_title,
    policy,
    validate_claim,
)
from release.recovery_pr import read_merge_identity
from release.recovery_remote import GitHubRecoveryRemote, ReadOnlyRecoveryJournal
from scripts.tests.recovery_http_fixture import FixtureHTTP, ProductionFixturePlatform
from scripts.tests.test_source_bound_recovery import MemoryBackend, execution_fixture


class RecoveryAPIContractTests(unittest.TestCase):
    def test_mirror_metadata_transport_sends_explicit_get_version(self):
        import subprocess

        from release.mirror import _gh_json

        with mock.patch(
            "release.mirror.subprocess.run",
            return_value=subprocess.CompletedProcess([], 0, b"{}", b""),
        ) as run:
            _gh_json(("api", "repos/yanyuhanyue/AniMemo/releases/tags/v2.0.0-rc.1"))
        self.assertEqual(
            run.call_args.args[0][1:],
            [
                "api",
                "--method",
                "GET",
                "-H",
                "X-GitHub-Api-Version: 2026-03-10",
                "repos/yanyuhanyue/AniMemo/releases/tags/v2.0.0-rc.1",
            ],
        )

    def execute_read(self, http):
        with tempfile.TemporaryDirectory() as directory, http.installed():
            remote = GitHubRecoveryRemote(output=Path(directory))
            claim = ProductionFixturePlatform(
                remote, Path(directory), http.fixture
            ).execution()
            self.assertTrue(
                any(req["version"] == "2022-11-28" for req in http.requests)
            )
            self.assertTrue(
                any(req["version"] == "2026-03-10" for req in http.requests)
            )
            return claim

    def test_valid_two_hop_source_through_lowest_http_transport(self):
        claim = self.execute_read(FixtureHTTP())
        self.assertEqual(claim["previousTool"]["sha"], policy()["previousTool"]["sha"])
        self.assertEqual(claim["supersededFailure"]["runId"], 34827731807)

    def test_real_two_version_responses_and_no_fallback(self):
        p = policy()
        for version in ("2022-11-28", "2026-03-10"):
            capture = json.loads(
                (
                    Path(__file__).resolve().parents[2]
                    / f"release/fixtures/rc-pr255-api-{version}.json"
                ).read_bytes()
            )
            http = FixtureHTTP()
            url = http.base + "/pulls/255"
            http.values[url] = capture["body"]
            # The failure specifically proves the removed field, independent
            # of the separate selected-version-conflict test below.
            with http.installed():
                if version == "2026-03-10":
                    with self.assertRaisesRegex(RecoveryError, "API_CONTRACT_MISMATCH"):
                        read_merge_identity(255, p["previousTool"]["sourceBranch"])
                else:
                    value = read_merge_identity(255, p["previousTool"]["sourceBranch"])
                    self.assertEqual(
                        value["merge_commit_sha"], p["previousTool"]["sha"]
                    )
            self.assertEqual(len(http.requests), 1)
            self.assertEqual(http.requests[0]["version"], "2022-11-28")

    def test_pr_contract_failures_never_reach_authority(self):
        mutations = [
            lambda pr: pr.pop("merge_commit_sha"),
            *[
                lambda pr, value=value: pr.__setitem__("merge_commit_sha", value)
                for value in (None, False, 15, {}, "a" * 39, "G" * 40)
            ],
            lambda pr: pr.__setitem__("merged", False),
            lambda pr: pr.__setitem__("state", "open"),
            lambda pr: pr.__setitem__("number", 255),
            lambda pr: pr["head"].__setitem__("ref", "other"),
            lambda pr: pr["head"]["repo"].__setitem__("id", 10),
            lambda pr: pr["base"]["repo"].__setitem__("full_name", "fork/AniMemo"),
            lambda pr: pr["base"]["repo"]["owner"].__setitem__("id", 10),
        ]
        for mutate in mutations:
            f = copy.deepcopy(execution_fixture())
            mutate(f["pull_request"])
            with self.subTest(mutate=mutate), self.assertRaises(RecoveryError):
                self.execute_read(FixtureHTTP(f))

    def test_http_status_json_and_version_diagnostics(self):
        for status in (301, 302, 307, 403, 404, 410, 429, 500, 502, 503):
            http = FixtureHTTP()
            http.statuses[http.base + f"/pulls/{policy()['sourcePr']}"] = status
            with (
                self.subTest(status=status),
                self.assertRaisesRegex(RecoveryError, f"PR_HTTP_{status}"),
            ):
                self.execute_read(http)
        http = FixtureHTTP()
        http.raw[http.base + f"/pulls/{policy()['sourcePr']}"] = b"not-json"
        with self.assertRaisesRegex(RecoveryError, "PR_JSON_INVALID"):
            self.execute_read(http)
        http = FixtureHTTP()
        http.headers[http.base + f"/pulls/{policy()['sourcePr']}"] = {
            "X-GitHub-Api-Version-Selected": "2026-03-10"
        }
        with self.assertRaisesRegex(RecoveryError, "API_VERSION_CONFLICT"):
            self.execute_read(http)

    def test_wrong_parent_chain_and_failed_run_binding(self):
        for field, update in (
            ("tool_commit", {"parents": [{"sha": policy()["subject"]["sha"]}]}),
            ("tool_commit", {"parents": [{"sha": "f" * 40}]}),
            (
                "tool_commit",
                {
                    "parents": [
                        {"sha": policy()["previousTool"]["sha"]},
                        {"sha": "f" * 40},
                    ]
                },
            ),
            ("previous_commit", {"parents": [{"sha": "f" * 40}]}),
            ("previous_reviewed", {"tree": {"sha": "f" * 40}}),
            ("superseded_run", {"id": 34827731808}),
            ("superseded_run", {"conclusion": "success"}),
            ("superseded_run", {"run_attempt": 2}),
            ("run", {"display_title": "renamed execute"}),
            ("run", {"run_attempt": 2}),
        ):
            f = copy.deepcopy(execution_fixture())
            f[field].update(update)
            with (
                self.subTest(field=field, update=update),
                self.assertRaises(RecoveryError),
            ):
                self.execute_read(FixtureHTTP(f))
        for title in (execution_title("execute"), "renamed execute"):
            f = execution_fixture()
            f["execution_runs"].append(
                {
                    **f["run"],
                    "id": 900,
                    "display_title": title,
                    "created_at": "2026-09-14T11:59:00Z",
                    "status": "completed",
                    "conclusion": "failure",
                }
            )
            with self.assertRaises(RecoveryError):
                self.execute_read(FixtureHTTP(f))

    def test_inspect_has_no_writable_context_or_journal_channel(self):
        f = execution_fixture()
        f["event"]["inputs"]["operation"] = "inspect"
        f["run"]["display_title"] = execution_title("inspect")
        with tempfile.TemporaryDirectory() as directory, FixtureHTTP(f).installed():
            root = Path(directory)
            remote = GitHubRecoveryRemote(output=root, read_only=True)
            platform = ProductionFixturePlatform(remote, root, f)
            context = platform.execution()
            self.assertEqual(context["writeSteps"], [])
            with self.assertRaises(RecoveryError):
                validate_claim(context)
            with self.assertRaisesRegex(RecoveryError, "READ_ONLY"):
                remote._final_send_check()
            with self.assertRaisesRegex(RecoveryError, "READ_ONLY"):
                ReadOnlyRecoveryJournal(root).append({})
            backend = MemoryBackend()
            with (
                mock.patch.object(
                    backend, "append", side_effect=AssertionError("inspect wrote")
                ),
                mock.patch.object(
                    remote, "upload", side_effect=AssertionError("inspect uploaded")
                ),
                mock.patch.object(
                    remote, "publish", side_effect=AssertionError("inspect published")
                ),
            ):
                engine = ExistingRCRecovery(
                    platform=platform, backend=backend, materials={}, root=root
                )
                with mock.patch.object(
                    engine.precheck, "snapshot", return_value={"syntheticBulk": True}
                ):
                    self.assertEqual(engine.run()["status"], "INSPECTED")
                self.assertIsNone(engine.guard)
                self.assertIsNone(remote.before_send)
                self.assertFalse(hasattr(backend, "before_push"))
                self.assertEqual(remote.write_requests, [])
            self.assertEqual(backend.value["revision"], 30)
