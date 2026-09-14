"""Exercise GitHub's published-only tag endpoint and authenticated draft listing."""

from __future__ import annotations

import argparse
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from release.publication_remote import GitHubAssetAdapter, GitHubDraftAdapter, GitHubPublishAdapter, GitHubResponse
from release.publication_transaction import (
    MutationIntent,
    ObservationClass,
    PublicationTransactionError,
    ResponseClass,
)
from release.test_publication_remote import _response
from scripts.release_publication import _transaction_run


class DraftDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.release = {
            "id": 999,
            "tag_name": "v2.0.0-rc.1",
            "name": "v2.0.0-rc.1",
            "body": "qualified notes\n",
            "draft": True,
            "prerelease": True,
            "assets": [],
        }
        self.calls = []
        self.responses = {
            "repos/owner/repo": _response({"permissions": {"push": True}}),
            "tags/v2.0.0-rc.1": _response({}, 404),
            "?per_page=100&page=1": _response([self.release]),
            "999": _response(self.release),
        }

    def observe(self):
        def request(method, endpoint, payload):
            self.assertEqual(method, "GET")
            self.assertIsNone(payload)
            suffix = endpoint.removeprefix("repos/owner/repo/releases").lstrip("/")
            self.calls.append(suffix)
            return self.responses[suffix]

        adapter = GitHubDraftAdapter(
            repository="owner/repo", tag=self.release["tag_name"],
            title=self.release["name"], body=self.release["body"].encode(),
            prerelease=True, request=request,
        )
        intent = MutationIntent("release-draft", "GITHUB_RELEASE_DRAFT", "key", adapter.identity)
        return adapter.observe(intent).classification

    def test_created_draft_is_found_by_id_without_reposting(self):
        self.assertIs(self.observe(), ObservationClass.SAME)
        self.assertEqual(self.calls[-1], "999")

    def test_second_page_draft_is_found(self):
        self.responses["?per_page=100&page=1"] = _response([
            {"id": number, "tag_name": f"v0.{number}"} for number in range(1, 101)
        ])
        self.responses["?per_page=100&page=2"] = _response([self.release])
        self.assertIs(self.observe(), ObservationClass.SAME)

    def test_only_complete_empty_listing_proves_absence(self):
        self.responses["?per_page=100&page=1"] = _response([])
        self.assertIs(self.observe(), ObservationClass.ABSENT)

    def test_published_release_keeps_fast_path(self):
        self.responses["tags/v2.0.0-rc.1"] = _response({**self.release, "draft": False})
        self.assertIs(self.observe(), ObservationClass.SAME)
        self.assertEqual(self.calls, ["tags/v2.0.0-rc.1"])

    def test_listing_errors_and_ambiguity_are_unknown(self):
        cases = [
            _response({}, 401), _response({}, 403), _response({}, 404), _response({}, 429), _response({}, 500),
            GitHubResponse(200, b'[{'), GitHubResponse(200, b'\xff'),
            _response({}), _response([None]), _response([{"id": True, "tag_name": "x"}]),
            _response([{"id": 0, "tag_name": "x"}]),
            _response([{"id": 1, "tag_name": None}]),
            _response([self.release, self.release]),
            _response([self.release, {**self.release, "id": 998}]),
            _response([{"id": n, "tag_name": "x"} for n in range(1, 102)]),
        ]
        for response in cases:
            with self.subTest(response=response):
                self.responses["?per_page=100&page=1"] = response
                self.assertIs(self.observe(), ObservationClass.UNKNOWN)

    def test_match_does_not_hide_incomplete_later_page(self):
        self.responses["?per_page=100&page=1"] = _response([
            self.release,
            *({"id": n, "tag_name": f"v0.{n}"} for n in range(1, 100)),
        ])
        for response in (_response({}, 403), _response([self.release])):
            with self.subTest(response=response):
                self.responses["?per_page=100&page=2"] = response
                self.assertIs(self.observe(), ObservationClass.UNKNOWN)

    def test_changed_or_disappeared_id_is_unknown(self):
        for response in (
            _response({}, 404), _response({}, 403),
            _response({**self.release, "id": 998}),
            _response({**self.release, "tag_name": "v3.0.0"}),
        ):
            with self.subTest(response=response):
                self.responses["999"] = response
                self.assertIs(self.observe(), ObservationClass.UNKNOWN)

    def test_pagination_limit_is_unknown(self):
        for page in range(1, 101):
            self.responses[f"?per_page=100&page={page}"] = _response([
                {"id": page * 100 + n, "tag_name": "other"} for n in range(100)
            ])
        self.assertIs(self.observe(), ObservationClass.UNKNOWN)

    def test_empty_listing_without_draft_visibility_is_unknown(self):
        self.responses["?per_page=100&page=1"] = _response([])
        for value in ({}, {"permissions": {"push": False}}, {"permissions": {"push": "true"}}):
            with self.subTest(value=value):
                self.responses["repos/owner/repo"] = _response(value)
                self.assertIs(self.observe(), ObservationClass.UNKNOWN)

    def test_next_link_is_validated_and_short_page_does_not_end_listing(self):
        page = _response([self.release])
        self.responses["?per_page=100&page=1"] = GitHubResponse(
            page.status, page.body,
            '<https://api.github.com/repos/owner/repo/releases?per_page=100&page=2>; rel="next"',
        )
        self.responses["?per_page=100&page=2"] = _response([])
        self.assertIs(self.observe(), ObservationClass.SAME)
        for link in (
            '<https://evil.example/steal?per_page=100&page=2>; rel="next"',
            '<https://api.github.com/repos/owner/repo/releases?per_page=100&page=3>; rel="next"',
            '<https://api.github.com/repos/owner/repo/releases?bad>; rel="next"',
            '<https://api.github.com/repos/owner/repo/releases?per_page=100&page=2>; rel="unknown"',
            '<https://api.github.com/repos/owner/repo/releases?per_page=100&page=2>; rel="last"',
        ):
            with self.subTest(link=link):
                self.responses["?per_page=100&page=1"] = GitHubResponse(200,page.body,link)
                self.assertIs(self.observe(), ObservationClass.UNKNOWN)

    def test_reloaded_draft_body_conflict_and_publish_transition(self):
        for changes, expected in (({"body": "changed"}, ObservationClass.DIFFERENT),
                                  ({"draft": False, "immutable": True}, ObservationClass.SAME)):
            with self.subTest(changes=changes):
                self.responses["999"] = _response({**self.release, **changes})
                self.assertIs(self.observe(), expected)

    def test_uncommitted_step_stops_runner_before_next_mutation(self):
        for phase in ("registry", "publication"):
            controller = mock.Mock()
            controller.ledger = {"steps": []}
            controller.advance.return_value = {"steps": [
                {"name": "current", "state": "READY", "committed": False}
            ]}
            runtime = SimpleNamespace(
                registry_steps=("current", "next"), publication_steps=("current", "next")
            )
            with (
                self.subTest(phase=phase),
                mock.patch("scripts.release_publication._transaction_controller", return_value=(controller, runtime)),
                self.assertRaisesRegex(PublicationTransactionError, "TRANSACTION_STEP_NOT_COMMITTED"),
            ):
                _transaction_run(argparse.Namespace(phase=phase))
            controller.advance.assert_called_once_with("current")

    def test_asset_and_publish_use_the_same_draft_discovery(self):
        digest = "sha256:" + "a" * 64
        asset = {"name": "manifest.json", "digest": digest, "size": 7}
        writes = []

        def request(method, endpoint, payload):
            suffix = endpoint.removeprefix("repos/owner/repo/releases").lstrip("/")
            if method == "PATCH":
                writes.append((endpoint, payload))
                return _response(self.release)
            self.assertEqual(method, "GET")
            self.assertIsNone(payload)
            if suffix == "999":
                return _response(self.release)
            return self.responses[suffix]

        self.release["assets"] = [asset]
        adapter = GitHubAssetAdapter(
            repository="owner/repo", tag=self.release["tag_name"],
            path=Path("manifest.json"), expected_digest=digest, expected_size=7,
            request=request,
        )
        intent = MutationIntent("release-asset-01", "GITHUB_RELEASE_ASSET", "key", digest)
        self.assertIs(adapter.observe(intent).classification, ObservationClass.SAME)
        for asset_changes, expected in (({"digest": "sha256:"+"b"*64}, ObservationClass.DIFFERENT),
                                        ({"state": "starter"}, ObservationClass.UNKNOWN)):
            with self.subTest(asset_changes=asset_changes):
                self.release["assets"] = [{**asset, **asset_changes}]
                self.assertIs(adapter.observe(intent).classification, expected)
        self.release["assets"] = [asset,asset]
        self.assertIs(adapter.observe(intent).classification, ObservationClass.UNKNOWN)
        self.release["assets"] = []
        self.assertIs(adapter.observe(intent).classification, ObservationClass.ABSENT)
        self.release["assets"] = [asset]
        publisher = GitHubPublishAdapter(
            repository="owner/repo", tag=self.release["tag_name"], prerelease=True,
            expected_assets={
                "manifest.json": {"sha256": digest, "size": 7},
                "checksums.txt": {"sha256": digest, "size": 7},
            }, request=request,
        )
        intent = MutationIntent("release-publish", "GITHUB_RELEASE_PUBLISH", "key", publisher.identity)
        self.assertIs(publisher.observe(intent).classification, ObservationClass.ABSENT)
        self.assertEqual(writes, [])
        self.release["assets"].append({**asset, "name": "checksums.txt"})
        self.assertIs(publisher.mutate(intent).classification, ResponseClass.ACKNOWLEDGED)
        self.assertEqual(writes, [("repos/owner/repo/releases/999", {
            "draft": False, "prerelease": True, "make_latest": "false",
        })])


if __name__ == "__main__":
    unittest.main()
