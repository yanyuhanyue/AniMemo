from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from release.contract import build_manifest
from updater.errors import RequestRejected
from updater.source import GitHubReleaseSource


def link_directory(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        if os.name != "nt":
            raise
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            check=True,
            capture_output=True,
            text=True,
        )


class FakeRunner:
    def __init__(self, manifest, *, exact_release=None, deployment_contract=None):
        self.manifest = manifest
        self.deployment_contract = deployment_contract or {
            "schemaVersion": 1,
            "files": manifest["deployment"]["files"],
        }
        self.calls = []
        self.exact_release = exact_release or {
            "tag_name": manifest["release"]["version"],
            "draft": False,
            "prerelease": manifest["release"]["channel"] != "stable",
            "published_at": "2026-08-12T01:00:00Z",
        }

    def run(self, argv, **kwargs):
        self.calls.append(tuple(argv))
        if argv[1:3] == ["api", "repos/yanyuhanyue/AniMemo/releases"]:
            return type("Result", (), {"stdout": json.dumps([
                {"tag_name": "v1.0.0-rc.10", "draft": False, "prerelease": True, "published_at": "2026-08-12T01:00:00Z"},
                {"tag_name": "v1.0.0-rc.2", "draft": False, "prerelease": True, "published_at": "2026-08-11T01:00:00Z"},
                {"tag_name": "v1.0.0", "draft": False, "prerelease": False, "published_at": "2026-08-10T01:00:00Z"},
                {"tag_name": "v1.0.1", "draft": False, "prerelease": True, "published_at": "2026-08-12T03:00:00Z"},
                {"tag_name": "v1.1.0-beta.1", "draft": False, "prerelease": True, "published_at": "2026-08-12T02:00:00Z"},
            ])})()
        if argv[1:3] == ["api", f"repos/yanyuhanyue/AniMemo/releases/tags/{self.manifest['release']['version']}"]:
            return type("Result", (), {"stdout": json.dumps(self.exact_release)})()
        if argv[1:3] == ["release", "download"]:
            output = Path(argv[argv.index("--dir") + 1])
            output.mkdir(parents=True, exist_ok=True)
            encoded = (json.dumps(self.manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()
            deployment_encoded = (
                json.dumps(self.deployment_contract, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
            ).encode()
            (output / "release-manifest.json").write_bytes(encoded)
            (output / "deployment-contract.json").write_bytes(deployment_encoded)
            (output / "checksums.txt").write_text(
                f"{hashlib.sha256(encoded).hexdigest()}  release-manifest.json\n"
                f"{hashlib.sha256(deployment_encoded).hexdigest()}  deployment-contract.json\n",
                encoding="utf-8",
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
        deployment_contract_sha256="sha256:0be5fdf5f87275755e06a2e2b6523c24e16d6aa1db48d8d58e8cfea969b674df",
        deployment_files=[
            {"path": "deploy/docker-compose.yml", "sha256": "sha256:" + "d" * 64},
            {"path": "updater/docker-compose.runtime.yml", "sha256": "sha256:" + "e" * 64},
        ],
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
            self.assertEqual(sum("attestation verify" in call for call in calls), 4)
            image_calls = [call for call in calls if "attestation verify oci://" in call]
            manifest_call = next(call for call in calls if "attestation verify" in call and "release-manifest.json" in call)
            deployment_call = next(
                call for call in calls
                if "attestation verify" in call and "deployment-contract.json" in call
            )
            self.assertTrue(all(f"--source-digest {'1' * 40}" in call for call in image_calls))
            self.assertIn(f"--source-digest {stable_manifest()['provenance']['sourceCommit']}", manifest_call)
            self.assertIn(
                f"--source-digest {stable_manifest()['provenance']['sourceCommit']}",
                deployment_call,
            )
            self.assertTrue(all("evil" not in call for call in calls))

    def test_tampered_deployment_contract_is_rejected_even_with_matching_checksum(self):
        with tempfile.TemporaryDirectory() as directory:
            changed = {
                "schemaVersion": 1,
                "files": [
                    {"path": "deploy/docker-compose.yml", "sha256": "sha256:" + "f" * 64},
                    {"path": "updater/docker-compose.runtime.yml", "sha256": "sha256:" + "e" * 64},
                ],
            }
            source = GitHubReleaseSource(
                Path(directory),
                runner=FakeRunner(stable_manifest(), deployment_contract=changed),
            )

            with self.assertRaisesRegex(RequestRejected, "differs from the release manifest"):
                source.fetch_verified("v1.0.0")

    def test_release_source_never_accepts_url_or_repository(self):
        with tempfile.TemporaryDirectory() as directory:
            source = GitHubReleaseSource(Path(directory), runner=FakeRunner(stable_manifest()))
            for version in ["https://evil.example/release", "evil/repo:v1", "v1.0.0;id"]:
                with self.subTest(version=version), self.assertRaises(RequestRejected):
                    source.fetch_verified(version)

    def test_exact_release_metadata_must_match_the_manifest_channel(self):
        with tempfile.TemporaryDirectory() as directory:
            runner = FakeRunner(
                stable_manifest(),
                exact_release={
                    "tag_name": "v1.0.0",
                    "draft": False,
                    "prerelease": True,
                    "published_at": "2026-08-12T01:00:00Z",
                },
            )
            source = GitHubReleaseSource(Path(directory), runner=runner)

            with self.assertRaisesRegex(RequestRejected, "metadata"):
                source.fetch_verified("v1.0.0")

    def test_release_download_does_not_follow_precreated_cache_asset_links(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = GitHubReleaseSource(root / "cache", runner=FakeRunner(stable_manifest()))
            destination = source.cache_root / "v1.0.0"
            destination.mkdir(parents=True)
            outside = root / "outside.json"
            outside.write_text("DO_NOT_CHANGE\n", encoding="utf-8")
            (destination / "release-manifest.json").hardlink_to(outside)

            source.fetch_verified("v1.0.0")

            self.assertEqual(outside.read_text(encoding="utf-8"), "DO_NOT_CHANGE\n")

    def test_release_download_rejects_a_linked_cache_root(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            outside = root / "outside"
            outside.mkdir()
            link_directory(root / "cache", outside)
            runner = FakeRunner(stable_manifest())
            source = GitHubReleaseSource(root / "cache", runner=runner)

            with self.assertRaisesRegex(RequestRejected, "cache directory"):
                source.fetch_verified("v1.0.0")

            self.assertEqual(runner.calls, [])
            self.assertEqual(list(outside.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
