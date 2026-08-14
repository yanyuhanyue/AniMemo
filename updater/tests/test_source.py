from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from urllib.error import HTTPError

from cramjam import snappy

from release.contract import build_manifest
from updater.errors import CommandFailed, RequestRejected
from updater.source import (
    MAX_GITHUB_JSON_BYTES,
    GitHubPublicRest,
    GitHubReleaseSource,
)


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
    def __init__(
        self,
        manifest,
        *,
        exact_release=None,
        deployment_contract=None,
        attestation_subject_name=None,
        duplicate_checksum=False,
        tamper_checksum=False,
        github_token=None,
        fail_anonymous_download=False,
        omit_asset=None,
        tamper_manifest=False,
        attestation_digest=None,
        fail_attestation=False,
        attestation_repository=None,
        attestation_workflow=None,
        attestation_source_commit=None,
    ):
        self.manifest = manifest
        self.deployment_contract = deployment_contract or {
            "schemaVersion": 1,
            "files": manifest["deployment"]["files"],
        }
        self.calls = []
        self.call_options = []
        self.attestation_subject_name = attestation_subject_name
        self.duplicate_checksum = duplicate_checksum
        self.tamper_checksum = tamper_checksum
        self.github_token = github_token
        self.fail_anonymous_download = fail_anonymous_download
        self.omit_asset = omit_asset
        self.tamper_manifest = tamper_manifest
        self.attestation_digest = attestation_digest
        self.fail_attestation = fail_attestation
        self.attestation_repository = attestation_repository
        self.attestation_workflow = attestation_workflow
        self.attestation_source_commit = attestation_source_commit
        self.exact_release = exact_release or {
            "tag_name": manifest["release"]["version"],
            "draft": False,
            "prerelease": manifest["release"]["channel"] != "stable",
            "published_at": "2026-08-12T01:00:00Z",
        }

    def run(self, argv, **kwargs):
        self.calls.append(tuple(argv))
        self.call_options.append(kwargs)
        if argv[1:3] == ["auth", "token"]:
            if self.github_token is None:
                raise CommandFailed("GitHub Host credential is unavailable")
            return type("Result", (), {"stdout": self.github_token + "\n"})()
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
            if self.fail_anonymous_download and "GH_TOKEN" not in kwargs.get("env", {}):
                raise CommandFailed("anonymous release download failed")
            output = Path(argv[argv.index("--dir") + 1])
            output.mkdir(parents=True, exist_ok=True)
            encoded = (json.dumps(self.manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()
            deployment_encoded = (
                json.dumps(self.deployment_contract, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
            ).encode()
            (output / "release-manifest.json").write_bytes(encoded)
            (output / "deployment-contract.json").write_bytes(deployment_encoded)
            checksums = (
                f"{hashlib.sha256(encoded).hexdigest()}  release-manifest.json\n"
                f"{hashlib.sha256(deployment_encoded).hexdigest()}  deployment-contract.json\n"
            )
            if self.duplicate_checksum:
                checksums += f"{hashlib.sha256(encoded).hexdigest()}  release-manifest.json\n"
            if self.tamper_checksum:
                replacement = "0" if checksums[0] != "0" else "1"
                checksums = replacement + checksums[1:]
            (output / "checksums.txt").write_text(
                checksums,
                encoding="utf-8",
            )
            if self.tamper_manifest:
                (output / "release-manifest.json").write_bytes(encoded + b"\n")
            if self.omit_asset:
                (output / self.omit_asset).unlink()
            return type("Result", (), {"stdout": ""})()
        if argv[1:3] == ["attestation", "verify"]:
            if self.fail_attestation:
                raise CommandFailed("attestation verification failed")
            subject = argv[3]
            is_image = subject.startswith("oci://")
            expected_workflow = (
                ".github/workflows/release.yml"
                if is_image
                else self.manifest["provenance"]["workflow"]
            )
            expected_commit = (
                self.manifest["release"]["commit"]
                if is_image
                else self.manifest["provenance"]["sourceCommit"]
            )
            repository = self.attestation_repository or "yanyuhanyue/AniMemo"
            workflow = self.attestation_workflow or expected_workflow
            source_commit = self.attestation_source_commit or expected_commit

            def option(name):
                return argv[argv.index(name) + 1] if name in argv else None

            expected_identity = (
                f"https://github.com/{repository}/{workflow}@refs/heads/main"
            )
            if (
                option("--repo") != repository
                or option("--signer-workflow") is not None
                or option("--cert-identity") != expected_identity
                or option("--cert-oidc-issuer")
                != "https://token.actions.githubusercontent.com"
                or option("--source-digest") != source_commit
                or option("--source-ref") != "refs/heads/main"
                or option("--signer-digest") != source_commit
                or option("--predicate-type") != "https://slsa.dev/provenance/v1"
                or option("--format") != "json"
                or option("--bundle") is None
            ):
                raise CommandFailed("attestation verification failed")
            if subject.startswith("oci://"):
                name, digest = subject.removeprefix("oci://").split("@", 1)
                digest = digest.removeprefix("sha256:")
            else:
                path = Path(subject)
                name = path.name
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
            return type("Result", (), {"stdout": json.dumps([{
                "verificationResult": {
                    "statement": {
                        "predicateType": "https://slsa.dev/provenance/v1",
                        "subject": [{
                            "name": self.attestation_subject_name or name,
                            "digest": {"sha256": self.attestation_digest or digest},
                        }],
                    },
                },
            }])})()
        return type("Result", (), {"stdout": "Verified"})()


class FakePublicRest:
    def __init__(
        self,
        manifest,
        *,
        exact_release=None,
        tag_commit=None,
        attestations=None,
        configured_token=None,
        release_page=None,
        bundle_payload=None,
    ):
        self.manifest = manifest
        self.calls = []
        self.exact_release = exact_release or {
            "tag_name": manifest["release"]["version"],
            "draft": False,
            "prerelease": manifest["release"]["channel"] != "stable",
            "published_at": manifest["release"]["createdAt"],
            "assets": [
                {"name": "checksums.txt", "state": "uploaded"},
                {"name": "deployment-contract.json", "state": "uploaded"},
                {"name": "release-manifest.json", "state": "uploaded"},
            ],
        }
        self.tag_commit = tag_commit or manifest["release"]["commit"]
        self.attestations = attestations if attestations is not None else [
            {"bundle": {"mediaType": "application/vnd.dev.sigstore.bundle.v0.3+json"}},
        ]
        self.token = configured_token
        self.bundle_payload = bundle_payload or {
            "mediaType": "application/vnd.dev.sigstore.bundle.v0.3+json",
        }
        self.bundle_calls = []
        self.release_page = release_page if release_page is not None else [
            {"tag_name": "v1.0.0-rc.10", "draft": False, "prerelease": True, "published_at": "2026-08-12T01:00:00Z"},
            {"tag_name": "v1.0.0-rc.2", "draft": False, "prerelease": True, "published_at": "2026-08-11T01:00:00Z"},
            {"tag_name": "v1.0.0", "draft": False, "prerelease": False, "published_at": "2026-08-10T01:00:00Z"},
            {"tag_name": "v1.0.1", "draft": False, "prerelease": True, "published_at": "2026-08-12T03:00:00Z"},
            {"tag_name": "v1.1.0-beta.1", "draft": False, "prerelease": True, "published_at": "2026-08-12T02:00:00Z"},
        ]

    def configured_token(self):
        return self.token

    def get_json(self, path, *, label):
        self.calls.append((path, label))
        if path == "/repos/yanyuhanyue/AniMemo/releases?per_page=100&page=1":
            return self.release_page
        if path == f"/repos/yanyuhanyue/AniMemo/releases/tags/{self.manifest['release']['version']}":
            return self.exact_release
        if path == f"/repos/yanyuhanyue/AniMemo/git/ref/tags/{self.manifest['release']['version']}":
            return {"object": {"type": "tag", "sha": "3" * 40}}
        if path == f"/repos/yanyuhanyue/AniMemo/git/tags/{'3' * 40}":
            return {"object": {"type": "commit", "sha": self.tag_commit}}
        if path.startswith("/repos/yanyuhanyue/AniMemo/attestations/sha256:"):
            return {"attestations": self.attestations}
        raise AssertionError(f"unexpected public REST path: {path}")

    def get_attestation_bundle(self, url, *, repository_id):
        self.bundle_calls.append((url, repository_id))
        return self.bundle_payload


class FakeHttpResponse:
    def __init__(self, payload, *, content_type="application/json"):
        self.encoded = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
        self.headers = {"Content-Type": content_type}

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def read(self, limit):
        return self.encoded


class FakeOpener:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.requests = []

    def open(self, request, timeout):
        self.requests.append(request)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


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
    def test_public_rest_does_not_read_a_configured_token_when_anonymous_access_succeeds(self):
        manifest = stable_manifest()
        runner = FakeRunner(manifest, github_token="read-only-token")
        opener = FakeOpener([FakeHttpResponse({"ok": True})])
        rest = GitHubPublicRest(runner=runner, opener=opener)

        self.assertEqual(rest.get_json("/repos/yanyuhanyue/AniMemo", label="Repository"), {"ok": True})

        self.assertEqual(runner.calls, [])
        request = opener.requests[0]
        self.assertEqual(request.full_url, "https://api.github.com/repos/yanyuhanyue/AniMemo")
        self.assertEqual(request.get_header("Accept"), "application/vnd.github+json")
        self.assertEqual(request.get_header("User-agent"), "AniMemo-Updater")
        self.assertEqual(request.get_header("X-github-api-version"), "2026-03-10")
        self.assertIsNone(request.get_header("Authorization"))

    def test_public_rest_uses_the_configured_read_only_token_only_after_anonymous_rate_limit(self):
        manifest = stable_manifest()
        token = "read-only-token"
        runner = FakeRunner(manifest, github_token=token)
        denied = HTTPError(
            "https://api.github.com/repos/yanyuhanyue/AniMemo",
            403,
            "rate limit",
            {},
            BytesIO(b""),
        )
        opener = FakeOpener([denied, FakeHttpResponse({"ok": True})])
        rest = GitHubPublicRest(runner=runner, opener=opener)

        self.assertEqual(rest.get_json("/repos/yanyuhanyue/AniMemo", label="Repository"), {"ok": True})

        self.assertEqual(runner.calls, [("/usr/bin/gh", "auth", "token", "--hostname", "github.com")])
        self.assertIsNone(opener.requests[0].get_header("Authorization"))
        self.assertEqual(opener.requests[1].get_header("Authorization"), f"Bearer {token}")
        self.assertNotIn(token, " ".join(runner.calls[0]))

    def test_public_rest_rejects_redirects_without_reading_a_host_token(self):
        manifest = stable_manifest()
        runner = FakeRunner(manifest, github_token="read-only-token")
        redirect = HTTPError(
            "https://api.github.com/repos/yanyuhanyue/AniMemo",
            302,
            "redirect",
            {"Location": "https://evil.example/repository"},
            BytesIO(b""),
        )
        rest = GitHubPublicRest(runner=runner, opener=FakeOpener([redirect]))

        with self.assertRaisesRegex(RequestRejected, "HTTP 302"):
            rest.get_json("/repos/yanyuhanyue/AniMemo", label="Repository")

        self.assertEqual(runner.calls, [])

    def test_public_rest_downloads_bundle_only_from_fixed_github_storage(self):
        manifest = stable_manifest()
        bundle = {"mediaType": "application/vnd.dev.sigstore.bundle.v0.3+json"}
        opener = FakeOpener([FakeHttpResponse(bundle)])
        rest = GitHubPublicRest(runner=FakeRunner(manifest), opener=opener)
        url = (
            "https://tmaproduction.blob.core.windows.net/attestations/1327429673/"
            "2026/08/14/40771105.json.sn?sig=temporary"
        )

        self.assertEqual(
            rest.get_attestation_bundle(url, repository_id=1327429673),
            bundle,
        )

        request = opener.requests[0]
        self.assertEqual(request.full_url, url)
        self.assertIsNone(request.get_header("Authorization"))

    def test_public_rest_decompresses_a_raw_snappy_attestation_bundle(self):
        manifest = stable_manifest()
        bundle = {"mediaType": "application/vnd.dev.sigstore.bundle.v0.3+json"}
        compressed = bytes(snappy.compress_raw(json.dumps(bundle).encode("utf-8")))
        opener = FakeOpener(
            [FakeHttpResponse(compressed, content_type="application/x-snappy")]
        )
        rest = GitHubPublicRest(runner=FakeRunner(manifest), opener=opener)
        url = (
            "https://tmaproduction.blob.core.windows.net/attestations/1327429673/"
            "2026/08/14/40771105.json.sn?sig=temporary"
        )

        self.assertEqual(
            rest.get_attestation_bundle(url, repository_id=1327429673),
            bundle,
        )

    def test_public_rest_rejects_a_snappy_bundle_beyond_the_json_limit(self):
        manifest = stable_manifest()
        compressed = bytes(snappy.compress_raw(b" " * (MAX_GITHUB_JSON_BYTES + 1)))
        opener = FakeOpener(
            [FakeHttpResponse(compressed, content_type="application/x-snappy")]
        )
        rest = GitHubPublicRest(runner=FakeRunner(manifest), opener=opener)
        url = (
            "https://tmaproduction.blob.core.windows.net/attestations/1327429673/"
            "2026/08/14/40771105.json.sn?sig=temporary"
        )

        with self.assertRaisesRegex(RequestRejected, "response is too large"):
            rest.get_attestation_bundle(url, repository_id=1327429673)

    def test_public_rest_rejects_untrusted_attestation_bundle_urls(self):
        manifest = stable_manifest()
        opener = FakeOpener([])
        rest = GitHubPublicRest(runner=FakeRunner(manifest), opener=opener)
        cases = {
            "scheme": (
                "http://tmaproduction.blob.core.windows.net/attestations/1327429673/"
                "2026/08/14/40771105.json.sn?sig=temporary"
            ),
            "host": (
                "https://evil.example/attestations/1327429673/2026/08/14/"
                "40771105.json.sn?sig=temporary"
            ),
            "repository": (
                "https://tmaproduction.blob.core.windows.net/attestations/999/"
                "2026/08/14/40771105.json.sn?sig=temporary"
            ),
            "userinfo": (
                "https://user@tmaproduction.blob.core.windows.net/attestations/"
                "1327429673/2026/08/14/40771105.json.sn?sig=temporary"
            ),
        }
        for name, url in cases.items():
            with self.subTest(name=name), self.assertRaisesRegex(
                RequestRejected,
                "bundle URL is invalid",
            ):
                rest.get_attestation_bundle(url, repository_id=1327429673)

        self.assertEqual(opener.requests, [])

    def test_release_download_uses_host_credential_only_after_anonymous_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = stable_manifest()
            token = "read-only-token"
            runner = FakeRunner(
                manifest,
                github_token=token,
                fail_anonymous_download=True,
            )
            source = GitHubReleaseSource(
                Path(directory),
                runner=runner,
                rest=FakePublicRest(manifest, configured_token=token),
            )

            self.assertEqual(source.fetch_verified("v1.0.0"), manifest)

            downloads = [
                options["env"]
                for call, options in zip(runner.calls, runner.call_options, strict=True)
                if call[1:3] == ("release", "download")
            ]
            self.assertEqual(len(downloads), 2)
            self.assertNotIn("GH_TOKEN", downloads[0])
            self.assertEqual(downloads[1]["GH_TOKEN"], token)

    def test_public_release_authority_uses_anonymous_rest_and_offline_attestation_bundles(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = stable_manifest()
            runner = FakeRunner(manifest)
            rest = FakePublicRest(manifest)
            source = GitHubReleaseSource(Path(directory), runner=runner, rest=rest)

            releases = source.list_releases("stable", refresh=True)
            fetched = source.fetch_verified("v1.0.0", refresh=True)

            self.assertEqual([item["version"] for item in releases], ["v1.0.0"])
            self.assertEqual(fetched, manifest)
            calls = [" ".join(call) for call in runner.calls]
            self.assertFalse(any(" gh api " in f" {call} " for call in calls))
            self.assertEqual(sum("attestation verify" in call for call in calls), 4)
            self.assertTrue(all("--bundle" in call for call in calls if "attestation verify" in call))
            for call, options in zip(runner.calls, runner.call_options, strict=True):
                if call[1:3] not in {("release", "download"), ("attestation", "verify")}:
                    continue
                environment = options["env"]
                for name in (
                    "GH_TOKEN",
                    "GITHUB_TOKEN",
                    "GH_ENTERPRISE_TOKEN",
                    "GITHUB_ENTERPRISE_TOKEN",
                    "GH_HOST",
                    "DOCKER_AUTH_CONFIG",
                    "REGISTRY_AUTH_FILE",
                ):
                    self.assertNotIn(name, environment)
                self.assertEqual(environment["GH_PROMPT_DISABLED"], "1")
                self.assertNotEqual(environment["GH_CONFIG_DIR"], os.environ.get("GH_CONFIG_DIR"))
            self.assertIn(
                f"/repos/yanyuhanyue/AniMemo/git/ref/tags/{manifest['release']['version']}",
                [path for path, _ in rest.calls],
            )

    def test_channels_are_semver_sorted_and_filtered(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = stable_manifest()
            source = GitHubReleaseSource(
                Path(directory),
                runner=FakeRunner(manifest),
                rest=FakePublicRest(manifest),
            )

            stable = source.list_releases("stable", refresh=True)
            rc = source.list_releases("rc", refresh=True)
            beta = source.list_releases("beta", refresh=True)

            self.assertEqual([item["version"] for item in stable], ["v1.0.0"])
            self.assertEqual([item["version"] for item in rc], ["v1.0.0", "v1.0.0-rc.10", "v1.0.0-rc.2"])
            self.assertEqual([item["version"] for item in beta], ["v1.1.0-beta.1", "v1.0.0", "v1.0.0-rc.10", "v1.0.0-rc.2"])

    def test_release_discovery_rejects_non_object_entries(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = stable_manifest()
            source = GitHubReleaseSource(
                Path(directory),
                runner=FakeRunner(manifest),
                rest=FakePublicRest(manifest, release_page=["invalid-release"]),
            )

            with self.assertRaisesRegex(RequestRejected, "invalid metadata"):
                source.list_releases("stable", refresh=True)

    def test_exact_release_manifest_and_all_attestations_are_verified(self):
        with tempfile.TemporaryDirectory() as directory:
            runner = FakeRunner(stable_manifest())
            source = GitHubReleaseSource(
                Path(directory),
                runner=runner,
                rest=FakePublicRest(stable_manifest()),
            )

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
            expected_identity = (
                "https://github.com/yanyuhanyue/AniMemo/"
                ".github/workflows/promote-release.yml@refs/heads/main"
            )
            self.assertIn(f"--cert-identity {expected_identity}", manifest_call)
            self.assertIn(f"--signer-digest {'2' * 40}", manifest_call)
            self.assertIn("--source-ref refs/heads/main", manifest_call)
            self.assertIn("--predicate-type https://slsa.dev/provenance/v1", manifest_call)
            self.assertTrue(all("--signer-workflow" not in call for call in calls))
            self.assertTrue(all("evil" not in call for call in calls))

    def test_verified_attestation_subject_name_must_match_exactly(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = stable_manifest()
            source = GitHubReleaseSource(
                Path(directory),
                runner=FakeRunner(manifest, attestation_subject_name="evil-release-manifest.json"),
                rest=FakePublicRest(manifest),
            )

            with self.assertRaisesRegex(RequestRejected, "subject"):
                source.fetch_verified("v1.0.0")

    def test_release_tag_must_peel_to_the_manifest_commit(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = stable_manifest()
            source = GitHubReleaseSource(
                Path(directory),
                runner=FakeRunner(manifest),
                rest=FakePublicRest(manifest, tag_commit="4" * 40),
            )

            with self.assertRaisesRegex(RequestRejected, "tag and manifest commit differ"):
                source.fetch_verified("v1.0.0")

    def test_missing_attestation_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = stable_manifest()
            source = GitHubReleaseSource(
                Path(directory),
                runner=FakeRunner(manifest),
                rest=FakePublicRest(manifest, attestations=[]),
            )

            with self.assertRaisesRegex(RequestRejected, "attestation is unavailable"):
                source.fetch_verified("v1.0.0")

    def test_github_issued_bundle_urls_are_downloaded_and_verified_locally(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = stable_manifest()
            bundle_url = (
                "https://tmaproduction.blob.core.windows.net/attestations/1327429673/"
                "2026/08/14/40771105.json.sn?sig=temporary"
            )
            rest = FakePublicRest(
                manifest,
                attestations=[
                    {
                        "repository_id": 1327429673,
                        "bundle_url": bundle_url,
                    },
                ],
            )
            source = GitHubReleaseSource(
                Path(directory),
                runner=FakeRunner(manifest),
                rest=rest,
            )

            self.assertEqual(source.fetch_verified("v1.0.0"), manifest)
            self.assertEqual(
                rest.bundle_calls,
                [(bundle_url, 1327429673)] * 4,
            )

    def test_attestation_repository_workflow_and_commit_are_bound_exactly(self):
        cases = {
            "repository": {"attestation_repository": "evil/AniMemo"},
            "workflow": {"attestation_workflow": ".github/workflows/evil.yml"},
            "commit": {"attestation_source_commit": "f" * 40},
        }
        for name, runner_options in cases.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                manifest = stable_manifest()
                source = GitHubReleaseSource(
                    Path(directory),
                    runner=FakeRunner(manifest, **runner_options),
                    rest=FakePublicRest(manifest),
                )

                with self.assertRaisesRegex(CommandFailed, "attestation verification failed"):
                    source.fetch_verified("v1.0.0")

    def test_release_assets_must_match_the_fixed_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = stable_manifest()
            metadata = dict(FakePublicRest(manifest).exact_release)
            metadata["assets"] = metadata["assets"][:-1]
            source = GitHubReleaseSource(
                Path(directory),
                runner=FakeRunner(manifest),
                rest=FakePublicRest(manifest, exact_release=metadata),
            )

            with self.assertRaisesRegex(RequestRejected, "assets differ"):
                source.fetch_verified("v1.0.0")

    def test_release_asset_metadata_must_be_a_unique_uploaded_object_list(self):
        manifest = stable_manifest()
        valid_assets = FakePublicRest(manifest).exact_release["assets"]
        cases = {
            "not-a-list": "invalid-assets",
            "non-object": [*valid_assets, "invalid-asset"],
            "not-uploaded": [
                *valid_assets,
                {"name": "checksums.txt", "state": "new"},
            ],
        }
        for name, assets in cases.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                metadata = dict(FakePublicRest(manifest).exact_release)
                metadata["assets"] = assets
                source = GitHubReleaseSource(
                    Path(directory),
                    runner=FakeRunner(manifest),
                    rest=FakePublicRest(manifest, exact_release=metadata),
                )

                with self.assertRaisesRegex(RequestRejected, "assets differ"):
                    source.fetch_verified("v1.0.0")

    def test_duplicate_checksum_entries_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = stable_manifest()
            source = GitHubReleaseSource(
                Path(directory),
                runner=FakeRunner(manifest, duplicate_checksum=True),
                rest=FakePublicRest(manifest),
            )

            with self.assertRaisesRegex(RequestRejected, "checksums"):
                source.fetch_verified("v1.0.0")

    def test_tampered_checksum_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = stable_manifest()
            source = GitHubReleaseSource(
                Path(directory),
                runner=FakeRunner(manifest, tamper_checksum=True),
                rest=FakePublicRest(manifest),
            )

            with self.assertRaisesRegex(RequestRejected, "checksum mismatch"):
                source.fetch_verified("v1.0.0")

    def test_missing_downloaded_release_asset_is_rejected_cleanly(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = stable_manifest()
            source = GitHubReleaseSource(
                Path(directory),
                runner=FakeRunner(manifest, omit_asset="release-manifest.json"),
                rest=FakePublicRest(manifest),
            )

            with self.assertRaisesRegex(RequestRejected, "asset"):
                source.fetch_verified("v1.0.0")

    def test_tampered_manifest_is_rejected_by_checksum(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = stable_manifest()
            source = GitHubReleaseSource(
                Path(directory),
                runner=FakeRunner(manifest, tamper_manifest=True),
                rest=FakePublicRest(manifest),
            )

            with self.assertRaisesRegex(RequestRejected, "checksum mismatch"):
                source.fetch_verified("v1.0.0")

    def test_tampered_attestation_is_rejected_by_the_verifier(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = stable_manifest()
            source = GitHubReleaseSource(
                Path(directory),
                runner=FakeRunner(manifest, fail_attestation=True),
                rest=FakePublicRest(manifest),
            )

            with self.assertRaisesRegex(CommandFailed, "attestation verification failed"):
                source.fetch_verified("v1.0.0")

    def test_verified_attestation_subject_digest_must_match_exactly(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = stable_manifest()
            source = GitHubReleaseSource(
                Path(directory),
                runner=FakeRunner(manifest, attestation_digest="f" * 64),
                rest=FakePublicRest(manifest),
            )

            with self.assertRaisesRegex(RequestRejected, "subject"):
                source.fetch_verified("v1.0.0")

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
                rest=FakePublicRest(stable_manifest()),
            )

            with self.assertRaisesRegex(RequestRejected, "differs from the release manifest"):
                source.fetch_verified("v1.0.0")

    def test_release_source_never_accepts_url_or_repository(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = stable_manifest()
            source = GitHubReleaseSource(
                Path(directory),
                runner=FakeRunner(manifest),
                rest=FakePublicRest(manifest),
            )
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
            source.rest = FakePublicRest(stable_manifest(), exact_release=runner.exact_release)

            with self.assertRaisesRegex(RequestRejected, "metadata"):
                source.fetch_verified("v1.0.0")

    def test_release_download_does_not_follow_precreated_cache_asset_links(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = stable_manifest()
            source = GitHubReleaseSource(
                root / "cache",
                runner=FakeRunner(manifest),
                rest=FakePublicRest(manifest),
            )
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
            source.rest = FakePublicRest(stable_manifest())

            with self.assertRaisesRegex(RequestRejected, "cache directory"):
                source.fetch_verified("v1.0.0")

            self.assertEqual(runner.calls, [])
            self.assertEqual(list(outside.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
