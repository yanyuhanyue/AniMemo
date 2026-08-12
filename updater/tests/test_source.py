from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from release.contract import build_manifest
from updater.errors import RequestRejected
from updater.source import GitHubReleaseSource


class FakeRunner:
    def __init__(self, manifest):
        self.manifest = manifest
        self.calls = []

    def run(self, argv, **kwargs):
        self.calls.append(tuple(argv))
        if argv[1:3] == ["api", "repos/yanyuhanyue/AniMemo/releases"]:
            return type("Result", (), {"stdout": json.dumps([
                {"tag_name": "v1.0.0-rc.10", "draft": False, "prerelease": True, "published_at": "2026-08-12T01:00:00Z"},
                {"tag_name": "v1.0.0-rc.2", "draft": False, "prerelease": True, "published_at": "2026-08-11T01:00:00Z"},
                {"tag_name": "v1.0.0", "draft": False, "prerelease": False, "published_at": "2026-08-10T01:00:00Z"},
                {"tag_name": "v1.1.0-beta.1", "draft": False, "prerelease": True, "published_at": "2026-08-12T02:00:00Z"},
            ])})()
        if argv[1:3] == ["release", "download"]:
            output = Path(argv[argv.index("--dir") + 1])
            output.mkdir(parents=True, exist_ok=True)
            encoded = (json.dumps(self.manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()
            (output / "release-manifest.json").write_bytes(encoded)
            (output / "checksums.txt").write_text(
                f"{hashlib.sha256(encoded).hexdigest()}  release-manifest.json\n", encoding="utf-8"
            )
            return type("Result", (), {"stdout": ""})()
        return type("Result", (), {"stdout": "Verified"})()


def stable_manifest():
    return build_manifest(
        version="v1.0.0",
        channel="stable",
        commit="1" * 40,
        created_at=datetime(2026, 8, 12, tzinfo=timezone.utc),
        api_digest="sha256:" + "a" * 64,
        web_digest="sha256:" + "b" * 64,
        minimum_updater_version="1.0.0",
        database_contract="animemo-db-v1",
        database_accepts=["animemo-db-v1"],
        migration_required=False,
        migration_policy="none",
        application_rollback="safe",
        configuration_contract="animemo-config-v1",
        configuration_accepts=["animemo-config-v1"],
        plugin_sdk_apis=[2],
        promoted_from="v1.0.0-rc.1",
        provenance_workflow=".github/workflows/promote-release.yml",
        provenance_source_commit="2" * 40,
    )


class GitHubReleaseSourceTests(unittest.TestCase):
    def test_channels_are_semver_sorted_and_filtered(self):
        with tempfile.TemporaryDirectory() as directory:
            source = GitHubReleaseSource(Path(directory), runner=FakeRunner(stable_manifest()))

            stable = source.list_releases("stable", refresh=True)
            rc = source.list_releases("rc", refresh=True)
            beta = source.list_releases("beta", refresh=True)

            self.assertEqual([item["version"] for item in stable], ["v1.0.0"])
            self.assertEqual([item["version"] for item in rc], ["v1.0.0", "v1.0.0-rc.10", "v1.0.0-rc.2"])
            self.assertEqual([item["version"] for item in beta], ["v1.1.0-beta.1", "v1.0.0", "v1.0.0-rc.10", "v1.0.0-rc.2"])

    def test_exact_release_manifest_and_all_attestations_are_verified(self):
        with tempfile.TemporaryDirectory() as directory:
            runner = FakeRunner(stable_manifest())
            source = GitHubReleaseSource(Path(directory), runner=runner)

            fetched = source.fetch_verified("v1.0.0")

            self.assertEqual(fetched, stable_manifest())
            calls = [" ".join(call) for call in runner.calls]
            self.assertTrue(any("release download v1.0.0 --repo yanyuhanyue/AniMemo" in call for call in calls))
            self.assertEqual(sum("attestation verify" in call for call in calls), 3)
            image_calls = [call for call in calls if "attestation verify oci://" in call]
            manifest_call = next(call for call in calls if "attestation verify" in call and "release-manifest.json" in call)
            self.assertTrue(all(f"--source-digest {'1' * 40}" in call for call in image_calls))
            self.assertIn(f"--source-digest {stable_manifest()['provenance']['sourceCommit']}", manifest_call)
            self.assertTrue(all("evil" not in call for call in calls))

    def test_release_source_never_accepts_url_or_repository(self):
        with tempfile.TemporaryDirectory() as directory:
            source = GitHubReleaseSource(Path(directory), runner=FakeRunner(stable_manifest()))
            for version in ["https://evil.example/release", "evil/repo:v1", "v1.0.0;id"]:
                with self.subTest(version=version), self.assertRaises(RequestRejected):
                    source.fetch_verified(version)


if __name__ == "__main__":
    unittest.main()
