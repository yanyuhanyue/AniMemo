"""Synthetic future R2 facts enter only at HTTP transport and Git read boundaries.

The two PR255 payloads are actual captured API responses. No claim, PR parser,
request builder, remote reader, or platform execution method is replaced.
"""

import io
import json
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from release.recovery import RecoveryPlatform
from release.recovery_contract import policy
from scripts.tests.test_source_bound_recovery import execution_fixture


class Response(io.BytesIO):
    def __init__(self, body, status=200, headers=None):
        super().__init__(body)
        self.status = status
        self.headers = headers or {}


class FixtureHTTP:
    def __init__(self, fixture=None):
        self.fixture = f = fixture or execution_fixture()
        self.requests = []
        self.statuses, self.headers, self.raw = {}, {}, {}
        p = policy()
        b = "https://api.github.com/repos/" + p["repository"]
        self.base = b
        old = p["previousTool"]
        self.values = {
            b: f["repository"],
            b + "/actions/runs/901": f["run"],
            b + "/actions/workflows/release-recovery.yml": f["workflow"],
            b + f"/pulls/{p['sourcePr']}": f["pull_request"],
            b + "/pulls/255": f["previous_pr"],
            b + "/pulls/257": f["diagnostic_facts"][0],
            b + "/git/commits/" + p["diagnosticTool"]["sha"]: f["diagnostic_facts"][1],
            b + "/git/commits/" + p["diagnosticTool"]["reviewedHead"]: f[
                "diagnostic_facts"
            ][2],
            b + "/pulls/256": f["parent_facts"][0],
            b + "/git/commits/" + p["parentTool"]["sha"]: f["parent_facts"][1],
            b + "/git/commits/" + p["parentTool"]["reviewedHead"]: f["parent_facts"][2],
            b + "/git/ref/heads/main": {"object": {"sha": f["main_sha"]}},
            b + "/git/commits/" + f["checkout_sha"]: f["tool_commit"],
            b + "/git/commits/" + f["pull_request"]["head"]["sha"]: {
                "sha": f["pull_request"]["head"]["sha"],
                "tree": {"sha": f["reviewed_tree"]},
            },
            b + "/git/commits/" + old["sha"]: f["previous_commit"],
            b + "/git/commits/" + old["reviewedHead"]: f["previous_reviewed"],
            b + f"/actions/runs/{p['supersededFailure']['runId']}": f["superseded_run"],
            b
            + "/actions/workflows/902/runs?event=workflow_dispatch&per_page=100&page=1": {
                "total_count": len(f["execution_runs"]),
                "workflow_runs": f["execution_runs"],
            },
            b + "/immutable-releases": {"enabled": True},
            b + "/branches/main/protection": json.loads(
                (
                    Path(__file__).resolve().parents[2]
                    / "release/fixtures/rc-recovery-protection.json"
                ).read_bytes()
            ),
            b + "/contents/.github/workflows/release-drafter.yml?ref=main": {
                "sha": p["drafterWorkflowBlob"]
            },
            b + "/contents/.github/release-drafter.yml?ref=main": {
                "sha": p["drafterConfigBlob"]
            },
            b + f"/releases/{p['ordinaryDraft']}": {
                "id": p["ordinaryDraft"],
                "draft": True,
                "prerelease": False,
                "published_at": None,
                "assets": [],
            },
            b
            + f"/actions/runs/{p['subject']['qualification_run_id']}/artifacts?per_page=100&page=1": {
                "total_count": 3,
                "artifacts": [
                    {**a, "expired": False}
                    for a in p["qualificationArtifacts"].values()
                ],
            },
            b + "/git/ref/tags/" + p["subject"]["release_tag"]: {
                "object": {
                    "type": "tag",
                    "sha": p["tagObject"],
                    "url": b + "/git/tags/" + p["tagObject"],
                }
            },
            b + "/git/tags/" + p["tagObject"]: {
                "sha": p["tagObject"],
                "tag": p["subject"]["release_tag"],
                "message": p["subject"]["release_tag"] + "\n",
                "object": {"type": "commit", "sha": p["subject"]["sha"]},
            },
        }
        for state in ("queued", "in_progress", "waiting", "requested", "pending"):
            self.values[b + f"/actions/runs?status={state}&per_page=100&page=1"] = {
                "total_count": 0,
                "workflow_runs": [],
            }

    def open(self, request, *args, **kwargs):
        url = request.full_url
        headers = {k.lower(): v for k, v in request.header_items()}
        version = "2022-11-28" if "/pulls/" in url else "2026-03-10"
        assert request.get_method() == "GET" and request.data is None
        assert headers["x-github-api-version"] == version
        # Never retain authentication headers, even for synthetic tokens.
        self.requests.append({"url": url, "method": "GET", "version": version})
        body = (
            self.raw[url] if url in self.raw else json.dumps(self.values[url]).encode()
        )
        return Response(
            body,
            self.statuses.get(url, 200),
            self.headers.get(url, {"X-GitHub-Api-Version-Selected": version}),
        )

    @contextmanager
    def installed(self):
        with (
            mock.patch.dict(
                "os.environ",
                {
                    "GH_TOKEN": "synthetic-test-token",
                    "ANIMEMO_RELEASE_ADMIN_READ_TOKEN": "synthetic-admin-read",
                },
            ),
            mock.patch("urllib.request.OpenerDirector.open", side_effect=self.open),
        ):
            yield self


class ProductionFixturePlatform(RecoveryPlatform):
    """Only Git command outputs and registry bytes are synthetic here."""

    def __init__(self, remote, root, fixture=None):
        self.fixture = f = fixture or execution_fixture()
        super().__init__(
            remote, root, f["environment"], f["event"], clock=lambda: f["now"]
        )

    def git(self, *args):
        f = self.fixture
        return {
            ("status", "--porcelain", "--untracked-files=all"): "",
            ("rev-parse", "HEAD"): f["checkout_sha"],
            ("rev-parse", "HEAD^{tree}"): f["checkout_tree"],
            ("show", "-s", "--format=%P", "HEAD"): f["parent_sha"],
        }[args]

    def registry(self):
        return {
            s["name"]: s["expectedIdentity"]
            for s in policy()["steps"]
            if s["name"].startswith("registry-")
        }
