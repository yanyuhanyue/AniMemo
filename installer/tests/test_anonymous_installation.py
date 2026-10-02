"""Current anonymous client and installation selection, without cryptographic shortcuts."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from urllib.request import Request

from bootstrap_kit import http_supervisor as supervisor
from bootstrap_kit.http_protocol import HttpFailure
from bootstrap_kit.safe_files import (
    create_private_directory,
    directory_identity,
    remove_owned_directory,
)
from bootstrap_kit.tests.test_http_supervisor import (
    ROOT,
    loopback_program,
    test_command,
)
from installer import production
from installer.runtime import (
    InstallerError,
    InstallTransportSource,
    explicit_transport_policy,
)
from installer.tests.test_anonymous_release_transport import (
    Network,
    Response,
    Result,
    wire_server,
)
from updater import source
from updater.errors import RequestRejected

CANARY = "NONSECRET_ANONYMOUS_INSTALL_CANARY"
PATH = "/repos/yanyuhanyue/AniMemo/releases/tags/v2.0.0-rc.3"


class AnonymousInstallationTests(unittest.TestCase):
    def test_injected_supervised_result_selects_fixed_path_without_accounts(self):
        network = Network([Response(body=b"{}")])
        with (
            mock.patch(
                "os.environ",
                {"GH_TOKEN": CANARY, "GITHUB_TOKEN": CANARY, "HTTPS_PROXY": CANARY},
            ),
            mock.patch.object(
                source.GitHubPublicRest,
                "configured_token",
                side_effect=AssertionError("account lookup"),
            ),
            mock.patch.object(
                source.CommandRunner,
                "run",
                side_effect=AssertionError("credential helper"),
            ),
        ):
            client = source.AnonymousGitHubRest(http_client=network, private_root=ROOT)
            self.assertIsNone(client.configured_token)
            self.assertEqual(client.get_json(PATH, label=CANARY), {})
        self.assertEqual(network.requests[0].target()[0], "api.github.com")
        self.assertEqual(network.requests[0].fields, {"path": PATH})
        self.assertTrue(network.connections[0].closed)

    def test_supervisor_errors_keep_status_cleanup_and_no_fallback(self):
        for status in (401, 403, 404, 429, 500, 503, 302):
            network = Network(
                [
                    HttpFailure(
                        "HTTP_" + str(status),
                        http_status=status,
                        cleanup=("BOOTSTRAP_HTTP_JOB_CLOSE_FAILED",),
                    )
                ]
            )
            with self.assertRaises(RequestRejected) as caught:
                source.AnonymousGitHubRest(http_client=network).get_json(
                    PATH, label=CANARY
                )
            self.assertEqual(caught.exception.http_status, status)
            self.assertEqual(
                caught.exception.secondary_errors, ("BOOTSTRAP_HTTP_JOB_CLOSE_FAILED",)
            )
            self.assertNotIn(CANARY, str(caught.exception))
            self.assertEqual(len(network.requests), 1)

    def test_duplicate_json_nonfinite_truncation_and_decoder_failure_are_fixed(self):
        for body in (b'{"a":1,"a":2}', b'{"x":NaN}', b"\xff", b"{"):
            with (
                self.subTest(size=len(body)),
                self.assertRaisesRegex(
                    RequestRejected, "BOOTSTRAP_ANONYMOUS_JSON_INVALID"
                ),
            ):
                source.AnonymousGitHubRest(
                    http_client=Network([Response(body=body)])
                ).get_json(PATH, label=CANARY)
        # This verifies the exception projection only. Python versions differ
        # in which nested input causes their native decoder to hit its limit.
        with (
            mock.patch.object(source.json, "loads", side_effect=RecursionError(CANARY)),
            self.assertRaisesRegex(RequestRejected, "BOOTSTRAP_ANONYMOUS_JSON_INVALID"),
        ):
            source.AnonymousGitHubRest._decode_json(b"{}", label=CANARY)

    def test_metadata_cannot_consume_snappy(self):
        network = Network(
            [Response(body=b"{}", headers=[("Content-Type", "application/x-snappy")])]
        )
        with self.assertRaisesRegex(RequestRejected, "CONTENT_TYPE_INVALID"):
            source.AnonymousGitHubRest(http_client=network).get_json(PATH, label=CANARY)
        self.assertTrue(network.connections[0].closed)

    def test_invalid_path_never_reaches_worker(self):
        for path in (
            "https://attacker.invalid/",
            "/repos/other/repo/releases",
            PATH + "?token=" + CANARY,
            PATH + "#fragment",
        ):
            network = Network([])
            with self.assertRaises((RequestRejected, HttpFailure)):
                source.AnonymousGitHubRest(http_client=network).get_json(
                    path, label=CANARY
                )
            self.assertEqual(network.requests, [])

    def test_actions_envelope_cannot_smuggle_credentials_or_post(self):
        url = "https://tmaproduction.blob.core.windows.net/attestations/1327429673/2026/09/20/123.json.sn?sig=NONSECRET"
        for request in (
            Request(url, headers={"Authorization": "Bearer " + CANARY}),
            Request(url, headers={"Cookie": CANARY}),
            Request(url, method="POST"),
        ):
            client = mock.Mock()
            client.fetch.return_value = Result(Response(body=b"{}"))
            with self.assertRaises((RequestRejected, HttpFailure)):
                source.AnonymousGitHubRest(http_client=client)._open_bytes(
                    request, label=CANARY
                )
            client.fetch.assert_not_called()

    def test_real_supervised_metadata_wire_errors(self):
        responses = [
            (Response(body=b"{}"), None),
            (
                Response(
                    body=b"{}",
                    headers=[
                        ("Content-Type", "application/json"),
                        ("Content-Length", "2"),
                        ("content-length", "2"),
                    ],
                ),
                "HEADERS_INVALID",
            ),
            (
                Response(
                    body=b"{}",
                    headers=[
                        ("Content-Type", "application/json"),
                        ("Content-Length", "3"),
                    ],
                ),
                "TRUNCATED",
            ),
            (Response(body=b"{}", status=206), "HTTP_206"),
        ]
        for response, error in responses:
            private = create_private_directory(ROOT, prefix="animemo-rest-wire-")
            identity = directory_identity(private)
            try:
                with (
                    wire_server([response]) as (port, calls),
                    mock.patch.object(
                        supervisor, "_command", test_command(loopback_program(port))
                    ),
                ):
                    client = source.AnonymousGitHubRest(private_root=private)
                    if error:
                        with self.assertRaisesRegex(RequestRejected, error):
                            client.get_json(PATH, label=CANARY)
                    else:
                        self.assertEqual(client.get_json(PATH, label=CANARY), {})
                    self.assertEqual(len(calls), 1)
                    self.assertFalse(
                        {"authorization", "cookie"}
                        & {k.lower() for k, v in calls[0][1]}
                    )
            finally:
                remove_owned_directory(private, identity)

    def test_anonymous_and_management_and_transport_policy_caches_are_separate(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            management = source.GitHubReleaseSource(root)
            github = production.ProductionReleasePort(cache_root=root)
            mirror = production.ProductionReleasePort(
                cache_root=root, transport_source=InstallTransportSource.OFFICIAL_MIRROR
            )
            self.assertEqual(management.cache_root, root)
            self.assertEqual(
                github.source.cache_root, root / "anonymous-installer/github"
            )
            self.assertEqual(
                mirror.source.cache_root, root / "anonymous-installer/official-mirror"
            )
            self.assertIs(type(management.rest), source.GitHubPublicRest)
            self.assertIs(type(github.source.rest), source.AnonymousGitHubRest)
            self.assertIs(type(mirror.source.rest), source.AnonymousGitHubRest)
            self.assertIs(
                type(github.source.transport_source),
                source.AnonymousGitHubTransportSource,
            )
            self.assertEqual(
                mirror.source.transport_source.transport_id.value, "official-mirror"
            )
            self.assertIsNot(github.source._verified_cache, management._verified_cache)
            self.assertIsNot(
                github.source._verified_cache, mirror.source._verified_cache
            )

    def test_formal_fresh_and_docker_reach_real_anonymous_production_construction(self):
        from scripts import formal_profile_runner as formal

        actual_build = production.build_production_composition
        constructions = []

        def build(**kwargs):
            composition = actual_build(**kwargs)
            constructions.append(composition)
            self.addCleanup(composition.releases._temporary.cleanup)
            return composition

        authority = SimpleNamespace(rc_tag="v2.0.0-rc.3")
        with (
            mock.patch.object(
                production, "build_production_composition", side_effect=build
            ),
            mock.patch.object(
                source.AnonymousGitHubRest,
                "get_json",
                side_effect=RequestRejected("SYNTHETIC_NETWORK_REJECTION"),
            ),
            mock.patch.object(
                source.GitHubPublicRest,
                "configured_token",
                side_effect=AssertionError("account lookup"),
            ),
            mock.patch.object(
                production.ProductionInstallerComposition, "execute_platform"
            ) as apply,
        ):
            for profile in ("FORMAL_FRESH", "FORMAL_DOCKER"):
                with self.subTest(profile=profile), self.assertRaises(InstallerError):
                    formal._production_executor_output(
                        authority=authority,
                        authority_root=Path("/unused"),
                        profile=profile,
                    )
        self.assertEqual(len(constructions), 2)
        for composition in constructions:
            self.assertIs(
                type(composition.releases.source.rest), source.AnonymousGitHubRest
            )
            self.assertEqual(
                composition.releases.transport_source, InstallTransportSource.GITHUB
            )
        apply.assert_not_called()

    def test_explicit_offline_failure_never_constructs_anonymous_or_online_fallback(
        self,
    ):
        with (
            tempfile.TemporaryDirectory() as temporary,
            mock.patch.object(
                source,
                "AnonymousGitHubRest",
                side_effect=AssertionError("online fallback"),
            ) as online,
            mock.patch.object(
                production.LocalBundleReleaseSource,
                "from_media",
                side_effect=RequestRejected("SYNTHETIC_OFFLINE_REJECTION"),
            ) as offline,
        ):
            root = Path(temporary)
            with self.assertRaisesRegex(
                InstallerError, "INSTALL_LOCAL_BUNDLE_VERIFICATION_FAILED"
            ):
                production.ProductionReleasePort(
                    cache_root=root,
                    transport_source=InstallTransportSource.LOCAL_BUNDLE,
                    transport_policy=explicit_transport_policy(
                        InstallTransportSource.LOCAL_BUNDLE
                    ),
                    local_bundle_payload=root / "portable.tar",
                    local_bundle_release_attestation=root / "sidecar.json",
                    offline_verifier=object(),
                )
            offline.assert_called_once()
            online.assert_not_called()
