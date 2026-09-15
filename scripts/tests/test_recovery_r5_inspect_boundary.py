"""Production R5 transport and execution boundaries with synthetic HTTP only."""

import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import yaml

from release.recovery import RecoveryAdapter, RecoveryPlatform
from release.recovery_contract import RecoveryError, execution_title, policy
from release.recovery_remote import (
    GitHubRecoveryRemote,
    GuardedRecoveryJournal,
    ReadOnlyRecoveryJournal,
)
from scripts.tests.recovery_http_fixture import FixtureHTTP, ProductionFixturePlatform
from scripts.tests.test_source_bound_recovery import execution_fixture


def draft_http(fixture=None):
    http = FixtureHTTP(fixture)
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
    return http


class R5InspectionBoundaryTests(unittest.TestCase):
    def test_by_tag_200_still_requires_complete_unique_release_collection(self):
        for state in ("valid", "missing", "duplicate", "malformed", "negative_id"):
            http = draft_http()
            draft = http.values[http.base + "/releases/388147631"]
            http.statuses[http.base + "/releases/tags/v2.0.0-rc.1"] = 200
            http.values[http.base + "/releases/tags/v2.0.0-rc.1"] = draft
            collection = http.base + "/releases?per_page=100&page=1"
            http.values[collection] = (
                [draft]
                if state == "valid"
                else []
                if state == "missing"
                else [draft, {**draft, "id": 123}]
            )
            if state == "malformed":
                http.values[collection] = [draft, {"id": 123}]
            if state == "negative_id":
                http.values[collection] = [draft, {"id": -1, "tag_name": "unrelated"}]
            with (
                self.subTest(state=state),
                tempfile.TemporaryDirectory() as directory,
                http.installed(),
            ):
                remote = GitHubRecoveryRemote(output=Path(directory), read_only=True)
                platform = ProductionFixturePlatform(remote, Path(directory))
                if state == "valid":
                    self.assertEqual(
                        platform.initial_draft_reads()["canonicalDiscovery"],
                        "COMPLETE_UNIQUE",
                    )
                else:
                    with self.assertRaisesRegex(
                        RecoveryError, "INITIAL_DISCOVERY_CONFLICT"
                    ):
                        platform.initial_draft_reads()
                self.assertEqual(sum(r["url"] == collection for r in http.requests), 1)

    def test_native_token_reads_all_fixed_draft_surfaces_before_materials(self):
        http = draft_http()
        roles = []

        def opened(request, *args, **kwargs):
            expected = (
                "synthetic-admin-read"
                if request.full_url.endswith(
                    ("/immutable-releases", "/branches/main/protection")
                )
                else "synthetic-test-token"
            )
            self.assertEqual(request.get_header("Authorization"), "Bearer " + expected)
            roles.append(expected)
            return http.open(request, *args, **kwargs)

        with tempfile.TemporaryDirectory() as directory, http.installed():
            remote = GitHubRecoveryRemote(output=Path(directory), read_only=True)
            platform = ProductionFixturePlatform(remote, Path(directory))
            with mock.patch("urllib.request.OpenerDirector.open", side_effect=opened):
                platform.execution()
                result = platform.initial_draft_reads()
            self.assertFalse(result["fullInspectionPassed"])
            self.assertEqual(result["status"], "FIXED_DRAFT_READS_VERIFIED")
            self.assertIsNone(result["platformReportedJobPermissions"])
            self.assertEqual(remote.write_requests, [])
            self.assertIn("synthetic-admin-read", roles)
            self.assertIn("synthetic-test-token", roles)

    def test_denials_keep_real_permission_clues_without_retries_or_secrets(self):
        for status in (401, 403, 429, 500, 503):
            http = draft_http()
            url = http.base + "/releases/373784357"
            http.statuses[url] = status
            http.headers[url] = {
                "X-GitHub-Api-Version-Selected": "2026-03-10",
                "X-GitHub-Request-Id": "safe-123",
                "X-Accepted-GitHub-Permissions": "contents=write",
                "X-RateLimit-Remaining": "0",
                "Retry-After": "45",
                "X-GitHub-SSO": "private-secret-url",
            }
            with (
                self.subTest(status=status),
                tempfile.TemporaryDirectory() as directory,
                http.installed(),
            ):
                remote = GitHubRecoveryRemote(output=Path(directory), read_only=True)
                with self.assertRaises(RecoveryError):
                    ProductionFixturePlatform(remote, Path(directory)).execution()
                last = remote.last_read_diagnostic
                self.assertEqual(last["status"], status)
                self.assertEqual(last["credentialRole"], "GITHUB_TOKEN")
                self.assertEqual(last["acceptedGitHubPermissions"], "contents=write")
                self.assertEqual(last["retryAfterSeconds"], 45)
                self.assertEqual(sum(r["url"] == url for r in http.requests), 1)
                self.assertNotIn("private-secret", json.dumps(last))
                self.assertNotIn("synthetic-test-token", json.dumps(last))

    def test_unsafe_permission_headers_and_duplicate_json_are_rejected(self):
        http = draft_http()
        url = http.base + "/releases/373784357"
        http.headers[url] = {
            "X-Accepted-GitHub-Permissions": "contents=write; secret=private-secret",
            "Retry-After": "https://private-secret/",
        }
        with tempfile.TemporaryDirectory() as directory, http.installed():
            remote = GitHubRecoveryRemote(output=Path(directory), read_only=True)
            remote.get(remote.base + "/releases/373784357")
            self.assertIsNone(remote.last_read_diagnostic["acceptedGitHubPermissions"])
            self.assertIsNone(remote.last_read_diagnostic["retryAfterSeconds"])
            http.raw[url] = b'{"id":373784357,"id":388147631}'
            with self.assertRaisesRegex(RecoveryError, "REMOTE_JSON_INVALID"):
                remote.get(remote.base + "/releases/373784357")
            http.raw[http.base + "/releases?per_page=100&page=1"] = (
                b'[{"id":388147631,"id":1}]'
            )
            with self.assertRaisesRegex(RecoveryError, "REMOTE_JSON_INVALID"):
                remote.readonly_request(
                    "GET", remote.base + "/releases?per_page=100&page=1", None
                )

    def test_native_draft_surface_does_not_send_unapproved_reads(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch("release.recovery_remote.github_request") as send,
        ):
            remote = GitHubRecoveryRemote(output=Path(directory), read_only=True)
            for suffix in (
                "/releases/123",
                "/releases?per_page=100&page=101",
                "/releases?per_page=100&page=1&x=1",
                "/releases/373784357/assets",
                "/releases/assets/123",
            ):
                with (
                    self.subTest(suffix=suffix),
                    self.assertRaisesRegex(RecoveryError, "DRAFT_READ_TARGET_INVALID"),
                ):
                    remote.get(remote.base + suffix)
            with self.assertRaisesRegex(RecoveryError, "ADMIN_TARGET_INVALID"):
                remote.get(remote.base + "/releases/373784357", administration=True)
            send.assert_not_called()

    def test_write_capable_fake_transports_cannot_write_during_inspect(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch("subprocess.run") as command,
            mock.patch("release.recovery_remote.github_request") as send,
            mock.patch("http.client.HTTPSConnection") as upload,
        ):
            remote = GitHubRecoveryRemote(
                output=Path(directory), read_only=True, before_send=lambda: None
            )
            for call in (
                lambda: remote.upload(Path(directory) / "checksums.txt"),
                remote.publish,
                lambda: remote.readonly_request("POST", remote.base + "/releases", {}),
                lambda: remote.readonly_request(
                    "DELETE", remote.base + "/releases/388147631", None
                ),
            ):
                with self.assertRaises(RecoveryError):
                    call()
            readonly = ReadOnlyRecoveryJournal(Path(directory))
            with self.assertRaisesRegex(RecoveryError, "READ_ONLY"):
                readonly.append({})
            backend = mock.Mock()
            guard = GuardedRecoveryJournal(
                backend,
                {"mode": "inspect", "bindingDigest": "synthetic"},
                head_reader=lambda: "",
                clock=lambda: None,
                before_write=lambda: None,
            )
            with self.assertRaises(RecoveryError):
                guard.claim_once()
            with self.assertRaises(RecoveryError):
                guard.append({})
            backend.append.assert_not_called()
            engine = SimpleNamespace(
                claim={"mode": "inspect"}, remote=remote, precheck=mock.Mock()
            )
            with self.assertRaises(RecoveryError):
                RecoveryAdapter(engine, None).mutate(None)
            for argv in (
                ("gh", "api", "--method", "POST", remote.base + "/releases"),
                ("gh", "attestation", "sign", "checksums.txt"),
                ("docker", "push", "image"),
                ("python", "-m", "release.mirror"),
            ):
                with self.assertRaisesRegex(RecoveryError, "PROOF_COMMAND_INVALID"):
                    remote.run_read_command(
                        argv, category="ORIGINAL_PROOF_VERIFY", timeout=1
                    )
            platform = RecoveryPlatform(remote, Path(directory), {}, {})
            with self.assertRaisesRegex(RecoveryError, "GIT_READONLY_BOUNDARY"):
                platform.git("push", "origin", "HEAD")
            command.assert_not_called()
            send.assert_not_called()
            upload.assert_not_called()

    def test_exact_r4_hop_and_failed_r4_inspect_are_required(self):
        for change in ("parent", "tree", "missing_inspect", "attempt", "head"):
            fixture = copy.deepcopy(execution_fixture())
            if change == "parent":
                fixture["draft_read_facts"][1]["parents"] = [
                    {"sha": policy()["parentTool"]["sha"]}
                ]
            elif change == "tree":
                fixture["draft_read_facts"][2]["tree"]["sha"] = "f" * 40
            elif change == "missing_inspect":
                fixture["execution_runs"] = [
                    r for r in fixture["execution_runs"] if r["id"] != 34857289912
                ]
            else:
                run = next(
                    r for r in fixture["execution_runs"] if r["id"] == 34857289912
                )
                run["run_attempt" if change == "attempt" else "head_sha"] = (
                    2 if change == "attempt" else "f" * 40
                )
            with (
                self.subTest(change=change),
                tempfile.TemporaryDirectory() as directory,
                FixtureHTTP(fixture).installed(),
            ):
                remote = GitHubRecoveryRemote(output=Path(directory), read_only=True)
                with self.assertRaises(RecoveryError):
                    ProductionFixturePlatform(
                        remote, Path(directory), fixture
                    ).execution()

    def test_only_inspect_job_receives_the_explicit_permission_exception(self):
        root = Path(__file__).resolve().parents[2]
        w = yaml.safe_load(
            (root / ".github/workflows/release-recovery.yml").read_text(
                encoding="utf-8"
            )
        )
        reads = {
            "contents": "read",
            "actions": "read",
            "packages": "read",
            "attestations": "read",
            "pull-requests": "read",
        }
        self.assertEqual(w["permissions"], reads)
        self.assertEqual(
            w["jobs"]["inspect"]["permissions"], {**reads, "contents": "write"}
        )
        self.assertEqual(
            w["jobs"]["recover"]["permissions"], {**reads, "contents": "write"}
        )
        self.assertEqual(set(w.get("on", w.get(True))), {"workflow_dispatch"})
        steps = w["jobs"]["inspect"]["steps"]
        self.assertFalse(steps[0]["with"]["persist-credentials"])
        for name, operation in (("inspect", "inspect"), ("recover", "execute")):
            job = w["jobs"][name]
            expected = " && ".join(
                (
                    "github.event_name == 'workflow_dispatch'",
                    "github.ref == 'refs/heads/main'",
                    "github.repository == 'yanyuhanyue/AniMemo'",
                    "github.actor_id == '111261350'",
                    "inputs.authorization_scope == 'ANIMEMO_V2_EXISTING_RC_SOURCE_BOUND_RECOVERY_API_FIX_V2'",
                    f"inputs.operation == '{operation}'",
                )
            )
            self.assertEqual(job["if"], expected)
            self.assertEqual(
                job["steps"][0]["with"]["ref"], "${{ github.workflow_sha }}"
            )
        self.assertFalse(any("credential" in s.get("run", "") for s in steps))
        for s in steps:
            if "uses" in s and not s["uses"].startswith("./"):
                self.assertRegex(s["uses"], r"@[0-9a-f]{40}$")
            self.assertNotIn("${{ inputs.", s.get("run", ""))
        self.assertIn(
            "python -m pip install --require-hashes -r release/requirements.lock",
            [s.get("run") for s in steps],
        )


if __name__ == "__main__":
    unittest.main()
