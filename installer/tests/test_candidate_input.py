from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest import mock

from installer import cli
from installer.bootstrap import (
    BootstrapAuthorityError,
    VerifiedPrepublicationCandidateCapability,
)
from installer.production import CandidateReleasePort, ProductionInstallerComposition
from installer.runtime import InstallTransportSource, explicit_transport_policy

DIGEST = "sha256:" + "a" * 64


class _Plan:
    mode = SimpleNamespace(value="ONLINE_FRESH")
    plan_digest = "sha256:" + "b" * 64

    def as_dict(self):
        return {"mode": self.mode.value, "planDigest": self.plan_digest}


class _Release:
    def as_dict(self):
        return {"version": "v1.1.0-rc.14", "channel": "rc"}


class _Composition:
    def __init__(self):
        self.plan_calls = 0
        self.execute_calls = 0

    def plan_platform(self, request, verified_at):
        self.plan_calls += 1
        self.request = request
        self.verified_at = verified_at
        return SimpleNamespace(plan=_Plan(), release=_Release())

    def execute_platform(self, *args, **kwargs):
        self.execute_calls += 1
        raise AssertionError("plan-only must not execute")


class _CandidateGate:
    def __init__(self):
        self.verify_calls = 0

    def verify_runtime_source(self, *, version, release_commit):
        self.verify_calls += 1
        self.binding = (version, release_commit)

    def consume(self, *, version, release_commit):
        raise AssertionError("planning must not consume or mutate trust")


class CandidateInstallerCliTests(unittest.TestCase):
    def test_candidate_parser_exposes_digest_but_no_arbitrary_material_path(self):
        help_text = cli._parser().format_help()
        candidate_help = cli._parser()._subparsers._group_actions[0].choices[
            "candidate"
        ].format_help()
        self.assertIn("--verified-candidate-digest", candidate_help)
        self.assertNotIn("candidate-directory", candidate_help)
        self.assertNotIn("bundle-payload", candidate_help)
        self.assertNotIn("source", candidate_help)
        self.assertIn("candidate", help_text)

    def test_candidate_is_plan_only_without_execute(self):
        composition = _Composition()
        output = io.StringIO()
        with mock.patch(
            "installer.production.build_candidate_composition",
            return_value=composition,
        ), redirect_stdout(output):
            code = cli.main(
                [
                    "candidate",
                    "--verified-candidate-digest",
                    DIGEST,
                    "--profile",
                    "ONLINE_FRESH",
                    "--public-origin",
                    "https://candidate.rc14.invalid",
                    "--json",
                ]
            )
        self.assertEqual(code, 0)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["mode"], "PLAN_ONLY")
        self.assertFalse(payload["releaseAuthorityGranted"])
        self.assertEqual(composition.plan_calls, 1)
        self.assertEqual(composition.execute_calls, 0)
        self.assertEqual(composition.request.local_bundle_payload, None)
        self.assertEqual(composition.request.transport_source.value, "github")

    def test_execute_requires_explicit_acceptance_before_any_platform_mutation(self):
        composition = _Composition()
        output = io.StringIO()
        with mock.patch(
            "installer.production.build_candidate_composition",
            return_value=composition,
        ), redirect_stdout(output):
            code = cli.main(
                [
                    "candidate",
                    "--verified-candidate-digest",
                    DIGEST,
                    "--profile",
                    "ONLINE_FRESH",
                    "--public-origin",
                    "https://candidate.rc14.invalid",
                    "--execute",
                    "--json",
                ]
            )
        self.assertEqual(code, cli.EXIT_VALIDATION)
        self.assertEqual(composition.execute_calls, 0)
        self.assertEqual(
            json.loads(output.getvalue())["reasonCode"],
            "CANDIDATE_EXECUTION_ACCEPTANCE_REQUIRED",
        )

    def test_online_candidate_platform_plan_never_discovers_a_release(self):
        args = cli._parser().parse_args(
            [
                "candidate",
                "--verified-candidate-digest",
                DIGEST,
                "--profile",
                "ONLINE_FRESH",
                "--public-origin",
                "https://candidate.rc14.invalid",
            ]
        )
        request = cli._candidate_request(args)
        release = SimpleNamespace(
            version="v1.1.0-rc.14",
            channel="rc",
            commit="a" * 40,
        )
        releases = CandidateReleasePort.__new__(CandidateReleasePort)
        releases.transport_source = InstallTransportSource.GITHUB
        releases.transport_policy = explicit_transport_policy(
            InstallTransportSource.GITHUB
        )
        releases._evidence = release
        gate = _CandidateGate()
        composition = ProductionInstallerComposition(
            runtime=object(),
            releases=releases,
            platform=object(),
            bootstrap_privilege_gate=gate,
        )

        class PlatformBootstrap:
            def plan(self, *, transport_source):
                self.transport_source = transport_source
                return _Plan()

        with mock.patch(
            "installer.bootstrap.authorize_online_stage0",
            side_effect=AssertionError("candidate release discovery is forbidden"),
        ), mock.patch(
            "installer.platform_bootstrap.ProductionPlatformBootstrap",
            PlatformBootstrap,
        ):
            session = composition.plan_platform(
                request, "2026-08-25T12:00:00Z"
            )
        self.assertIs(session.bootstrap.transport_source, InstallTransportSource.GITHUB)
        self.assertEqual(gate.verify_calls, 1)
        self.assertEqual(gate.binding, (release.version, release.commit))

    def test_candidate_capability_cannot_be_forged(self):
        with self.assertRaisesRegex(
            BootstrapAuthorityError, "CANDIDATE_BOOTSTRAP_CAPABILITY_FORGERY"
        ):
            VerifiedPrepublicationCandidateCapability(
                object(),
                candidate_input_digest=DIGEST,
                installer_materials_path=__import__("pathlib").Path("materials.tar"),
                installer_materials_sha256=DIGEST,
                release_commit="a" * 40,
                verified_candidate_digest=DIGEST,
                version="v1.1.0-rc.14",
            )


if __name__ == "__main__":
    unittest.main()
