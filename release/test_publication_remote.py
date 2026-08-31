from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from release.publication import build_publication_plan
from release.publication_remote import (
    AttestationAdapter,
    CommandResult,
    GitHubAssetAdapter,
    GitHubDraftAdapter,
    GitHubPublishAdapter,
    GitHubResponse,
    RegistryAdapter,
    build_publication_runtime,
    github_request,
)
from release.publication_transaction import MutationIntent, ObservationClass


DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64
COMMIT = "1" * 40
TREE = "2" * 40


def _digest(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


def _response(value: object, status: int = 200) -> GitHubResponse:
    return GitHubResponse(
        status,
        json.dumps(value, separators=(",", ":"), sort_keys=True).encode("utf-8"),
    )


class PublicationRemoteTests(unittest.TestCase):
    def test_general_github_request_never_follows_authenticated_redirects(self):
        requests: list[object] = []

        class RedirectOpener:
            def open(self, request, timeout: int):
                requests.append(request)
                raise urllib.error.HTTPError(
                    request.full_url,
                    302,
                    "Found",
                    {"Location": "https://malicious.example/collect"},
                    None,
                )

        with (
            mock.patch.dict("os.environ", {"GH_TOKEN": "fixture"}, clear=True),
            mock.patch(
                "release.publication_remote.urllib.request.build_opener",
                return_value=RedirectOpener(),
            ) as build_opener,
        ):
            response = github_request("GET", "repos/yanyuhanyue/AniMemo", None)
        self.assertEqual(response.status, 302)
        self.assertEqual(len(requests), 1)
        self.assertIn("Authorization", dict(requests[0].header_items()))
        build_opener.assert_called_once()

    def test_registry_readback_classifies_same_absent_different_and_unknown(self):
        intent = MutationIntent("registry-api-version", "REGISTRY_PUSH", "key", DIGEST_A)
        cases = (
            (CommandResult(0, (DIGEST_A + "\n").encode(), b""), ObservationClass.SAME),
            (CommandResult(1, b"", b"MANIFEST UNKNOWN"), ObservationClass.ABSENT),
            (CommandResult(0, (DIGEST_B + "\n").encode(), b""), ObservationClass.DIFFERENT),
            (CommandResult(1, b"", b"authentication transport failure"), ObservationClass.UNKNOWN),
        )
        for result, expected in cases:
            with self.subTest(expected=expected):
                adapter = RegistryAdapter(
                    target="ghcr.io/yanyuhanyue/animemo-api:v1.1.0-rc.19",
                    expected_digest=DIGEST_A,
                    source_layout=Path("candidate-runtime/oci/api"),
                    source_role="api",
                    source_repository="ghcr.io/yanyuhanyue/animemo-api",
                    run=lambda _argv, _timeout, result=result: result,
                )
                verified = SimpleNamespace(
                    digest=DIGEST_A,
                    repository="ghcr.io/yanyuhanyue/animemo-api",
                    role="api",
                    platform="linux/amd64",
                )
                with mock.patch(
                    "release.publication_remote.verify_oci_image", return_value=verified
                ):
                    self.assertIs(adapter.observe(intent).classification, expected)

    def test_candidate_registry_preflight_rejects_unverified_local_layout(self):
        with tempfile.TemporaryDirectory() as directory:
            calls: list[tuple[str, ...]] = []
            adapter = RegistryAdapter(
                target="ghcr.io/yanyuhanyue/animemo-api:v1.1.0-rc.19",
                expected_digest=DIGEST_A,
                source_layout=Path(directory),
                source_role="api",
                source_repository="ghcr.io/yanyuhanyue/animemo-api",
                run=lambda argv, _timeout: (
                    calls.append(argv) or CommandResult(1, b"", b"manifest unknown")
                ),
            )
            intent = MutationIntent(
                "registry-api-version", "REGISTRY_PUSH", "key", DIGEST_A
            )
            self.assertIs(adapter.observe(intent).classification, ObservationClass.DIFFERENT)
            self.assertEqual(calls, [])

    def test_stable_registry_preflight_requires_exact_digest_source_before_mutation(self):
        calls: list[str] = []

        def run(argv: tuple[str, ...], _timeout: int) -> CommandResult:
            calls.append(argv[-1])
            if argv[-1].endswith(":v1.1.0"):
                return CommandResult(1, b"", b"manifest unknown")
            return CommandResult(0, (DIGEST_B + "\n").encode(), b"")

        adapter = RegistryAdapter(
            target="ghcr.io/yanyuhanyue/animemo-api:v1.1.0",
            expected_digest=DIGEST_A,
            source_reference="ghcr.io/yanyuhanyue/animemo-api@" + DIGEST_A,
            run=run,
        )
        intent = MutationIntent("registry-api-stable", "REGISTRY_TAG", "key", DIGEST_A)
        observed = adapter.observe(intent)
        self.assertIs(observed.classification, ObservationClass.DIFFERENT)
        self.assertEqual(len(calls), 2)

    def test_asset_redirect_strips_authorization_before_cross_origin_download(self):
        content = b"release asset"
        release = {
            "assets": [
                {
                    "name": "release-manifest.json",
                    "digest": None,
                    "size": len(content),
                    "url": "https://api.github.com/repos/yanyuhanyue/AniMemo/releases/assets/7",
                }
            ]
        }
        requests: list[object] = []

        class Response:
            def __init__(self, value: bytes) -> None:
                self.value = value
                self.offset = 0

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, size: int) -> bytes:
                chunk = self.value[self.offset : self.offset + size]
                self.offset += len(chunk)
                return chunk

        class RedirectOpener:
            def open(self, request, timeout: int):
                requests.append(request)
                raise urllib.error.HTTPError(
                    request.full_url,
                    302,
                    "Found",
                    {
                        "Location": "https://release-assets.githubusercontent.com/signed/object?token=opaque"
                    },
                    None,
                )

        class DownloadOpener:
            def open(self, request, timeout: int):
                requests.append(request)
                return Response(content)

        adapter = GitHubAssetAdapter(
            repository="yanyuhanyue/AniMemo",
            tag="v1.1.0-rc.19",
            path=Path("release-manifest.json"),
            expected_digest=_digest(content),
            expected_size=len(content),
            request=lambda _method, _endpoint, _payload: _response(release),
        )
        intent = MutationIntent(
            "release-asset-01", "GITHUB_RELEASE_ASSET", "key", _digest(content)
        )
        with (
            mock.patch.dict("os.environ", {"GH_TOKEN": "hidden-test-token"}, clear=True),
            mock.patch(
                "release.publication_remote.urllib.request.build_opener",
                side_effect=[RedirectOpener(), DownloadOpener()],
            ),
        ):
            self.assertIs(adapter.observe(intent).classification, ObservationClass.SAME)
        first_headers = dict(requests[0].header_items())
        second_headers = dict(requests[1].header_items())
        self.assertIn("Authorization", first_headers)
        self.assertNotIn("Authorization", second_headers)

    def test_draft_identity_is_monotonic_after_publish(self):
        body = "qualified notes\n"
        release = {
            "tag_name": "v1.1.0-rc.19",
            "name": "v1.1.0-rc.19",
            "body": body,
            "draft": False,
            "prerelease": True,
            "immutable": True,
            "assets": [{"name": "already-uploaded"}],
        }
        adapter = GitHubDraftAdapter(
            repository="yanyuhanyue/AniMemo",
            tag="v1.1.0-rc.19",
            title="v1.1.0-rc.19",
            body=body.encode(),
            prerelease=True,
            request=lambda _method, _endpoint, _payload: _response(release),
        )
        intent = MutationIntent(
            "release-draft", "GITHUB_RELEASE_DRAFT", "key", adapter.identity
        )
        self.assertIs(adapter.observe(intent).classification, ObservationClass.SAME)

    def test_stable_publish_requires_exact_latest_pointer(self):
        assets = {"release-manifest.json": {"sha256": DIGEST_A, "size": 7}}
        release = {
            "id": 7,
            "tag_name": "v1.1.0",
            "draft": False,
            "prerelease": False,
            "immutable": True,
            "assets": [
                {
                    "name": "release-manifest.json",
                    "digest": DIGEST_A,
                    "size": 7,
                }
            ],
        }

        def requester(latest_tag: str):
            def request(_method: str, endpoint: str, _payload: object) -> GitHubResponse:
                return _response(
                    {"tag_name": latest_tag} if endpoint.endswith("/latest") else release
                )

            return request

        missing = GitHubPublishAdapter(
            repository="yanyuhanyue/AniMemo",
            tag="v1.1.0",
            prerelease=False,
            expected_assets=assets,
            request=requester("v1.0.0"),
        )
        exact = GitHubPublishAdapter(
            repository="yanyuhanyue/AniMemo",
            tag="v1.1.0",
            prerelease=False,
            expected_assets=assets,
            request=requester("v1.1.0"),
        )
        missing_intent = MutationIntent(
            "release-publish", "GITHUB_RELEASE_PUBLISH", "key", missing.identity
        )
        exact_intent = MutationIntent(
            "release-publish", "GITHUB_RELEASE_PUBLISH", "key", exact.identity
        )
        self.assertIs(missing.observe(missing_intent).classification, ObservationClass.ABSENT)
        self.assertIs(exact.observe(exact_intent).classification, ObservationClass.SAME)

    def test_publish_refuses_a_draft_with_an_extra_asset(self):
        assets = {"release-manifest.json": {"sha256": DIGEST_A, "size": 7}}
        release = {
            "id": 7,
            "tag_name": "v1.1.0-rc.19",
            "draft": True,
            "prerelease": True,
            "immutable": False,
            "assets": [
                {"name": "release-manifest.json", "digest": DIGEST_A, "size": 7},
                {"name": "extra.bin", "digest": DIGEST_B, "size": 1},
            ],
        }
        adapter = GitHubPublishAdapter(
            repository="yanyuhanyue/AniMemo",
            tag="v1.1.0-rc.19",
            prerelease=True,
            expected_assets=assets,
            request=lambda _method, _endpoint, _payload: _response(release),
        )
        intent = MutationIntent(
            "release-publish", "GITHUB_RELEASE_PUBLISH", "key", adapter.identity
        )
        self.assertIs(adapter.observe(intent).classification, ObservationClass.DIFFERENT)

    def test_publish_allows_an_exact_partial_draft_asset_subset_to_resume(self):
        assets = {
            "release-manifest.json": {"sha256": DIGEST_A, "size": 7},
            "checksums.txt": {"sha256": DIGEST_B, "size": 9},
        }
        release = {
            "id": 7,
            "tag_name": "v1.1.0-rc.19",
            "draft": True,
            "prerelease": True,
            "immutable": False,
            "assets": [
                {"name": "release-manifest.json", "digest": DIGEST_A, "size": 7}
            ],
        }
        adapter = GitHubPublishAdapter(
            repository="yanyuhanyue/AniMemo",
            tag="v1.1.0-rc.19",
            prerelease=True,
            expected_assets=assets,
            request=lambda _method, _endpoint, _payload: _response(release),
        )
        intent = MutationIntent(
            "release-publish", "GITHUB_RELEASE_PUBLISH", "key", adapter.identity
        )
        self.assertIs(adapter.observe(intent).classification, ObservationClass.ABSENT)

    def test_attestation_observer_ignores_nonmatching_additive_bundles(self):
        locator = "oci://ghcr.io/yanyuhanyue/animemo-api@" + DIGEST_A

        def adapter(result: CommandResult, attestations: list[object]) -> AttestationAdapter:
            return AttestationAdapter(
                repository="yanyuhanyue/AniMemo",
                workflow="release.yml",
                source_sha=COMMIT,
                subjects=((locator, DIGEST_A),),
                run=lambda _argv, _timeout: result,
                request=lambda _method, _endpoint, _payload: _response(
                    {"attestations": attestations}
                ),
            )

        verified = adapter(CommandResult(0, b"", b""), [])
        absent = adapter(CommandResult(1, b"", b""), [])
        other = adapter(CommandResult(1, b"", b""), [{"bundle": "other-signer"}])
        for value, expected in (
            (verified, ObservationClass.SAME),
            (absent, ObservationClass.ABSENT),
            (other, ObservationClass.ABSENT),
        ):
            intent = MutationIntent(
                "attestation-api", "ATTESTATION", "key", value.identity
            )
            self.assertIs(value.observe(intent).classification, expected)

    def test_file_attestation_preflight_rejects_local_subject_digest_drift(self):
        with tempfile.TemporaryDirectory() as directory:
            subject = Path(directory) / "release-manifest.json"
            subject.write_bytes(b"changed after plan")
            calls: list[tuple[str, ...]] = []
            adapter = AttestationAdapter(
                repository="yanyuhanyue/AniMemo",
                workflow="release.yml",
                source_sha=COMMIT,
                subjects=((str(subject), DIGEST_A),),
                run=lambda argv, _timeout: (
                    calls.append(argv) or CommandResult(1, b"", b"")
                ),
                request=lambda _method, _endpoint, _payload: _response(
                    {"attestations": [{"bundle": "other-signer"}]}
                ),
            )
            intent = MutationIntent(
                "attestation-file-01", "ATTESTATION", "key", adapter.identity
            )
            self.assertIs(
                adapter.observe(intent).classification,
                ObservationClass.DIFFERENT,
            )
            self.assertEqual(calls, [])

    def test_file_attestation_treats_other_signer_bundle_as_exact_intent_absent(self):
        with tempfile.TemporaryDirectory() as directory:
            subject = Path(directory) / "deployment-contract.json"
            content = b"accepted RC bytes"
            subject.write_bytes(content)
            adapter = AttestationAdapter(
                repository="yanyuhanyue/AniMemo",
                workflow="promote-release.yml",
                source_sha=COMMIT,
                subjects=((str(subject), _digest(content)),),
                run=lambda _argv, _timeout: CommandResult(1, b"", b"not matched"),
                request=lambda _method, _endpoint, _payload: _response(
                    {"attestations": [{"bundle": "release.yml signer"}]}
                ),
            )
            intent = MutationIntent(
                "attestation-file-02", "ATTESTATION", "key", adapter.identity
            )
            self.assertIs(adapter.observe(intent).classification, ObservationClass.ABSENT)

    def test_runtime_closes_rc_and_stable_mutation_order_from_one_final_plan(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            notes = b"qualified notes\n"
            (root / "release-notes.md").write_bytes(notes)
            contents = {
                "release-manifest.json": b"manifest",
                "deployment-contract.json": b"contract",
                "installer-materials.tar": b"installer",
            }
            for name, content in contents.items():
                (root / name).write_bytes(content)
            checksum_content = "".join(
                f"{_digest(content).removeprefix('sha256:')}  {name}\n"
                for name, content in contents.items()
            ).encode()
            (root / "checksums.txt").write_bytes(checksum_content)
            portable = root / "animemo-v1.1.0-rc.19-portable.tar"
            portable.write_bytes(b"portable")
            assets = {
                name: {"sha256": _digest((root / name).read_bytes()), "size": (root / name).stat().st_size}
                for name in (
                    "release-manifest.json",
                    "deployment-contract.json",
                    "installer-materials.tar",
                    "checksums.txt",
                )
            }
            plan = build_publication_plan(
                repository="yanyuhanyue/AniMemo",
                channel="rc",
                tag="v1.1.0-rc.19",
                commit=COMMIT,
                qualification_identity=DIGEST_A,
                release_notes_identity=DIGEST_B,
                release_notes_markdown_sha256=_digest(notes),
                assets=assets,
                transport_assets={
                    portable.name: {
                        "role": "PORTABLE_RELEASE_BUNDLE",
                        "sha256": _digest(portable.read_bytes()),
                        "size": portable.stat().st_size,
                    }
                },
                api_digest=DIGEST_A,
                web_digest=DIGEST_B,
            )
            candidate = root / "candidate"
            runtime = build_publication_runtime(
                plan,
                source_tree=TREE,
                asset_root=root,
                candidate_root=candidate,
                repository_path=root,
                request=lambda _method, _endpoint, _payload: GitHubResponse(404, b""),
                run=lambda _argv, _timeout: CommandResult(1, b"", b"not found"),
            )
            self.assertEqual(
                runtime.registry_steps,
                (
                    "registry-api-version",
                    "registry-api-source",
                    "registry-web-version",
                    "registry-web-source",
                ),
            )
            self.assertEqual(
                runtime.external_steps,
                (
                    "attestation-api",
                    "attestation-web",
                    "attestation-file-01",
                    "attestation-file-02",
                    "attestation-file-03",
                ),
            )
            self.assertEqual(runtime.publication_steps[0:2], ("git-tag", "release-draft"))
            self.assertEqual(runtime.publication_steps[-1], "release-publish")
            self.assertEqual(len(runtime.publication_steps), 8)
            self.assertEqual(len(runtime.intents), 17)


if __name__ == "__main__":
    unittest.main()
