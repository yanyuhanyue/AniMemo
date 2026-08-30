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
from installer.platform_bootstrap import _apt_argv
from installer.production import (
    CandidatePlatformCommandObserver,
    CandidateReleasePort,
    LocalDockerCommandRunner,
    ProductionDoctorAcceptance,
    ProductionInstallerComposition,
    build_candidate_composition,
)
from installer.runtime import (
    InstallerError,
    InstallOutcome,
    InstallTransportSource,
    explicit_transport_policy,
)
from updater.oci import AcquiredRuntimeImage, ImageAcquisitionReceipt

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


class _ExecutingComposition:
    def __init__(self):
        self.session = SimpleNamespace(plan=_Plan(), release=_Release())
        self.platform_receipt = SimpleNamespace(
            as_dict=lambda: {"result": "PASS"}
        )
        self.plan = SimpleNamespace(plan_digest="sha256:" + "c" * 64)
        self.result = SimpleNamespace(
            outcome=SimpleNamespace(value="SUCCEEDED"),
            as_dict=lambda: {
                "outcome": "SUCCEEDED",
                "completedSteps": ["runtime.validate", "doctor.accept"],
            },
        )
        self.runtime = SimpleNamespace(
            plan=mock.Mock(return_value=self.plan),
            execute=mock.Mock(return_value=self.result),
        )

    def plan_platform(self, request, verified_at):
        return self.session

    def execute_platform(self, session, accepted_plan_digest):
        return self.platform_receipt

    def candidate_profile_execution_observation(
        self, *, platform_plan, platform_receipt, installer_plan, installer_result
    ):
        self.observation_inputs = (
            platform_plan,
            platform_receipt,
            installer_plan,
            installer_result,
        )
        return {"schema": "production-observation-test"}


class _CandidateGate:
    def __init__(self):
        self.verify_calls = 0

    def verify_runtime_source(self, *, version, release_commit):
        self.verify_calls += 1
        self.binding = (version, release_commit)

    def consume(self, *, version, release_commit):
        raise AssertionError("planning must not consume or mutate trust")


class CandidateInstallerCliTests(unittest.TestCase):
    @mock.patch(
        "installer.production.verified_prepublication_candidate_capability",
        return_value=object(),
    )
    @mock.patch("installer.production.CandidateBootstrapPrivilegeGate")
    @mock.patch("installer.production.CandidateReleasePort")
    @mock.patch("release.candidate.load_verified_candidate", return_value=object())
    def test_offline_candidate_composition_uses_closed_prepublication_transport(
        self,
        _load_candidate,
        candidate_release_port,
        _candidate_gate,
        _candidate_capability,
    ):
        build_candidate_composition(DIGEST, profile="OFFLINE_VALIDATE_ONLY")

        self.assertIs(
            candidate_release_port.call_args.kwargs["transport_source"],
            InstallTransportSource.PREPUBLICATION_CANDIDATE,
        )

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

    def test_offline_candidate_uses_closed_prepublication_transport(self):
        args = cli._parser().parse_args(
            [
                "candidate",
                "--verified-candidate-digest",
                DIGEST,
                "--profile",
                "OFFLINE_VALIDATE_ONLY",
                "--public-origin",
                "https://candidate.rc14.invalid",
            ]
        )

        request = cli._candidate_request(args)

        self.assertIs(
            request.transport_source,
            InstallTransportSource.PREPUBLICATION_CANDIDATE,
        )
        self.assertEqual(request.selector.channel, "rc")
        self.assertIsNone(request.local_bundle_payload)
        self.assertIsNone(request.local_bundle_release_attestation)
        public_install_help = cli._parser()._subparsers._group_actions[0].choices[
            "install"
        ].format_help()
        self.assertNotIn("prepublication-candidate", public_install_help)

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

    def test_execute_exports_observation_from_the_production_composition(self):
        composition = _ExecutingComposition()
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
                    "--accept",
                    "--json",
                ]
            )

        self.assertEqual(code, cli.EXIT_SUCCESS)
        self.assertEqual(
            json.loads(output.getvalue())["productionExecutionObservation"],
            {"schema": "production-observation-test"},
        )
        self.assertEqual(
            composition.observation_inputs,
            (
                composition.session.plan,
                composition.platform_receipt,
                composition.plan,
                composition.result,
            ),
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

    def test_real_candidate_composition_builds_execution_bound_observation(self):
        releases = CandidateReleasePort.__new__(CandidateReleasePort)
        image_receipt = ImageAcquisitionReceipt(
            verified_release_identity="sha256:" + "1" * 64,
            transport_policy_identity="2" * 64,
            images=tuple(
                AcquiredRuntimeImage(
                    role=role,
                    canonical_reference=f"example.invalid/{role}@sha256:" + "3" * 64,
                    observed_reference=f"example.invalid/{role}@sha256:" + "3" * 64,
                )
                for role in ("api", "postgres", "redis", "web")
            ),
            identity="4" * 64,
        )
        releases.image_receipt_for = lambda _release: image_receipt
        doctor = ProductionDoctorAcceptance(
            releases=mock.Mock(), compatibility=mock.Mock()
        )
        doctor._latest_report = SimpleNamespace(
            as_dict=lambda: {
                "doctorIdentity": {
                    "format": "animemo-doctor-runtime",
                    "version": 1,
                },
                "overallStatus": "PASS",
            }
        )
        doctor._latest_canonical_acceptance = tuple(
            {
                "evidence": {
                    "adapter": adapter,
                    "observationDigest": "sha256:" + "5" * 64,
                },
                "name": name,
                "receiptDigest": "sha256:" + digit * 64,
                "result": "PASS",
            }
            for name, adapter, digit in (
                (
                    "application.journal-crud",
                    "django-domain-service-transaction-rollback",
                    "6",
                ),
                ("service.api.health", "immutable-compose-api-health", "7"),
                ("service.web.health", "immutable-compose-web-health", "8"),
            )
        )
        command_delegate = mock.Mock()
        egress_readbacks = [
            SimpleNamespace(returncode=0, stdout="true"),
            SimpleNamespace(
                returncode=0,
                stdout="AF_UNIX AF_NETLINK\n",
            ),
        ]
        command_delegate.run.side_effect = [
            *egress_readbacks,
            *[
                SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps([image.canonical_reference]),
                )
                for image in image_receipt.images
            ],
        ]
        platform_delegate = mock.Mock()
        platform_delegate.run.return_value = SimpleNamespace(returncode=0)
        platform_observer = CandidatePlatformCommandObserver(platform_delegate)
        composition = ProductionInstallerComposition(
            runtime=object(),
            releases=releases,
            platform=object(),
            candidate_doctor=doctor,
            candidate_platform_observer=platform_observer,
            candidate_command_runner=LocalDockerCommandRunner(command_delegate),
        )
        completed_steps = ["runtime.validate", "doctor.accept"]
        result = SimpleNamespace(
            outcome=InstallOutcome.SUCCEEDED,
            as_dict=lambda: {
                "outcome": "SUCCEEDED",
                "completedSteps": completed_steps,
            },
        )

        observation = composition.candidate_profile_execution_observation(
            platform_plan=SimpleNamespace(
                actions=(),
                mode=SimpleNamespace(value="OFFLINE_VALIDATE_ONLY"),
                network_policy="DENY_ALL",
                plan_digest="sha256:" + "9" * 64,
            ),
            platform_receipt=SimpleNamespace(result="PASS"),
            installer_plan=SimpleNamespace(
                plan_digest="sha256:" + "a" * 64,
                release=object(),
            ),
            installer_result=result,
        )

        self.assertEqual(observation["networkObservation"]["result"], "PASS")
        self.assertEqual(
            len(observation["networkObservation"]["completedCommands"]), 6
        )
        self.assertTrue(
            observation["networkObservation"]["egressIsolation"][
                "containerNetworkInternal"
            ]
        )
        self.assertEqual(
            observation["imageRuntimeReadbackReceipt"]["result"], "PASS"
        )
        self.assertEqual(observation["externalPullObservation"]["inventory"], [])
        self.assertEqual(len(observation["canonicalAcceptanceTests"]), 3)
        self.assertNotEqual(
            observation["doctorExecutionIdentity"],
            observation["doctorReceiptDigest"],
        )

        for name, readbacks in (
            (
                "container-network-not-internal",
                [
                    SimpleNamespace(returncode=0, stdout="false"),
                    SimpleNamespace(returncode=0, stdout="AF_UNIX AF_NETLINK\n"),
                ],
            ),
            (
                "service-allows-inet",
                [
                    SimpleNamespace(returncode=0, stdout="true"),
                    SimpleNamespace(returncode=0, stdout="AF_UNIX AF_INET\n"),
                ],
            ),
        ):
            command_delegate.run.side_effect = readbacks
            before = len(composition.candidate_command_runner._completed_commands)
            with self.subTest(name=name), self.assertRaisesRegex(
                InstallerError,
                "INSTALL_CANDIDATE_EGRESS_ISOLATION_UNVERIFIED",
            ):
                composition.candidate_profile_execution_observation(
                    platform_plan=SimpleNamespace(
                        actions=(),
                        mode=SimpleNamespace(value="OFFLINE_VALIDATE_ONLY"),
                        network_policy="DENY_ALL",
                        plan_digest="sha256:" + "9" * 64,
                    ),
                    platform_receipt=SimpleNamespace(result="PASS"),
                    installer_plan=SimpleNamespace(
                        plan_digest="sha256:" + "a" * 64,
                        release=object(),
                    ),
                    installer_result=result,
                )
            del composition.candidate_command_runner._completed_commands[before:]

        platform_observer.run(
            ("/usr/bin/curl", "https://hidden.invalid"),
            timeout=30,
            environment={"PATH": "/usr/bin"},
        )
        command_delegate.run.side_effect = [
            *egress_readbacks,
            *[
                SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps([image.canonical_reference]),
                )
                for image in image_receipt.images
            ],
        ]
        with self.assertRaisesRegex(
            InstallerError, "INSTALL_CANDIDATE_COMMAND_OBSERVATION_FAILED"
        ):
            composition.candidate_profile_execution_observation(
                platform_plan=SimpleNamespace(
                    actions=(),
                    mode=SimpleNamespace(value="OFFLINE_VALIDATE_ONLY"),
                    network_policy="DENY_ALL",
                    plan_digest="sha256:" + "9" * 64,
                ),
                platform_receipt=SimpleNamespace(result="PASS"),
                installer_plan=SimpleNamespace(
                    plan_digest="sha256:" + "a" * 64,
                    release=object(),
                ),
                installer_result=result,
            )

        platform_observer._completed_commands.pop()
        command_delegate.run.side_effect = [
            SimpleNamespace(returncode=0),
            *[
                SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps([image.canonical_reference]),
                )
                for image in image_receipt.images
            ],
        ]
        composition.candidate_command_runner.run(
            ["/usr/bin/docker", "compose", "up", "-d"]
        )
        with self.assertRaisesRegex(
            InstallerError, "INSTALL_CANDIDATE_COMMAND_OBSERVATION_FAILED"
        ):
            composition.candidate_profile_execution_observation(
                platform_plan=SimpleNamespace(
                    actions=(),
                    mode=SimpleNamespace(value="OFFLINE_VALIDATE_ONLY"),
                    network_policy="DENY_ALL",
                    plan_digest="sha256:" + "9" * 64,
                ),
                platform_receipt=SimpleNamespace(result="PASS"),
                installer_plan=SimpleNamespace(
                    plan_digest="sha256:" + "a" * 64,
                    release=object(),
                ),
                installer_result=result,
            )

        composition.candidate_command_runner._completed_commands.pop()
        platform_observer.run(
            _apt_argv("update"),
            timeout=30,
            environment={"PATH": "/usr/bin"},
        )
        platform_observer.run(
            _apt_argv("update"),
            timeout=30,
            environment={"PATH": "/usr/bin"},
        )
        command_delegate.run.side_effect = [
            *egress_readbacks,
            *[
                SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps([image.canonical_reference]),
                )
                for image in image_receipt.images
            ],
        ]
        with self.assertRaisesRegex(
            InstallerError, "INSTALL_CANDIDATE_NETWORK_OBSERVATION_FAILED"
        ):
            composition.candidate_profile_execution_observation(
                platform_plan=SimpleNamespace(
                    actions=(
                        SimpleNamespace(
                            kind=SimpleNamespace(value="APT_UPDATE"),
                            packages=(),
                        ),
                    ),
                    mode=SimpleNamespace(value="ONLINE_FRESH"),
                    network_policy="APT_UBUNTU_ARCHIVE_ONLY",
                    plan_digest="sha256:" + "9" * 64,
                ),
                platform_receipt=SimpleNamespace(result="PASS"),
                installer_plan=SimpleNamespace(
                    plan_digest="sha256:" + "a" * 64,
                    release=object(),
                ),
                installer_result=result,
            )

        platform_observer._completed_commands.clear()
        packages = ("docker.io",)
        platform_delegate.run.side_effect = [
            SimpleNamespace(returncode=124),
            SimpleNamespace(returncode=0),
        ]
        platform_observer.run(
            _apt_argv("install", packages),
            timeout=30,
            environment={"PATH": "/usr/bin"},
        )
        platform_observer.run(
            _apt_argv("install", packages),
            timeout=30,
            environment={"PATH": "/usr/bin"},
        )
        command_delegate.run.side_effect = [
            *egress_readbacks,
            *[
                SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps([image.canonical_reference]),
                )
                for image in image_receipt.images
            ],
        ]
        retry_observation = composition.candidate_profile_execution_observation(
            platform_plan=SimpleNamespace(
                actions=(
                    SimpleNamespace(
                        kind=SimpleNamespace(value="INSTALL_DOCKER"),
                        packages=packages,
                    ),
                ),
                mode=SimpleNamespace(value="ONLINE_FRESH"),
                network_policy="APT_UBUNTU_ARCHIVE_ONLY",
                plan_digest="sha256:" + "9" * 64,
            ),
            platform_receipt=SimpleNamespace(result="PASS"),
            installer_plan=SimpleNamespace(
                plan_digest="sha256:" + "a" * 64,
                release=object(),
            ),
            installer_result=result,
        )
        self.assertEqual(
            retry_observation["networkObservation"][
                "retryableNetworkCommandDigests"
            ],
            retry_observation["networkObservation"][
                "expectedNetworkCommandDigests"
            ],
        )

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
