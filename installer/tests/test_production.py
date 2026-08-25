from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar
from unittest import mock

from durability.platform import (
    REQUIRED_CAPABILITIES,
    REQUIRED_REHEARSALS,
    HostCapabilityEvidence,
    canonical_platform_qualification_bytes,
    finalize_platform_qualification,
)
from installer.platform_bootstrap import (
    PLATFORM_PACKAGE_POLICY,
    BootstrapHostFacts,
    PlatformBootstrapMode,
    PlatformCommandResult,
    ProductionPlatformBootstrap,
)
from installer.production import (
    LocalDockerCommandRunner,
    ProductionInstallerComposition,
    ProductionPlatformPort,
    ProductionReleasePort,
    ProductionTargetPort,
    build_production_composition,
)
from installer.runtime import (
    Installer,
    InstallerError,
    InstallerMode,
    InstallRequest,
    InstallTransportSource,
    PlatformEvidence,
    ReleaseEvidence,
    ReleaseSelector,
)
from updater.local_bundle import LocalBundleTransportPolicy
from updater.transport import ExplicitTransportPolicy

RC13_REJECTION_FIXTURE = {
    "attempts": [
        {
            "attempt": 1,
            "instanceMutation": 0,
            "manualSameWorkerReadback": "PASS_62658560_BYTES",
            "officialMirrorMaterialAcquisition": "TRANSIENT_TRANSPORT_EXHAUSTED",
            "productRootMutation": 0,
            "stage0GithubAuthority": "PASS",
            "stage0GithubCliVersion": "2.97.0",
            "stage0VerifyBeforeExecute": "PASS",
        },
        {
            "attempt": 2,
            "instanceMutation": 0,
            "officialMirrorMaterialAcquisition": "PASS",
            "outcome": "COMPATIBILITY_BLOCKED",
            "productRootMutation": 0,
            "reasonCode": "PLATFORM_RUNTIME_UNSUPPORTED",
            "stage0GithubAuthority": "PASS",
            "stage0GithubCliVersion": "2.97.0",
            "stage0VerifyBeforeExecute": "PASS",
        },
    ],
    "blockerCode": "FROZEN_BLOCKER_INSTALLER_REQUIRES_PLATFORM_RUNTIME_BEFORE_BOOTSTRAP",
    "cloneAttempts": 2,
    "finalProbe": {
        "architecture": "x86_64",
        "compose": {"available": False, "value": ""},
        "containerCount": 0,
        "docker": {"available": False, "exitCode": 127},
        "githubCli": {
            "exitCode": 0,
            "stderrType": None,
            "stdout": (
                "gh version 2.97.0 (2026-07-31)\n"
                "https://github.com/cli/cli/releases/tag/v2.97.0"
            ),
        },
        "hostname": "animemo-test",
        "images": [],
        "networkCount": 0,
        "pgDump": {"available": False, "value": ""},
        "productRoots": {
            "/data/animemo": False,
            "/data/animemo-instances": False,
            "/opt/animemo": False,
            "/opt/animemo-instances": False,
            "/var/lib/animemo-updater/instances": False,
        },
        "psql": {"available": False, "value": ""},
        "systemd": {"available": True},
        "volumeCount": 0,
    },
    "mainSha": "220c1ec981aff10d5a2aca5e6e984e41a041bf86",
    "mainTree": "498d243f72982a290907f7bb4106cdd5b5e83140",
    "maxCloneAttempts": 2,
    "releaseTag": "v1.1.0-rc.13",
    "result": "FAIL",
    "rootCause": (
        "collect_host_capabilities checks /usr/bin/docker, docker compose, "
        "/usr/bin/pg_dump and /usr/bin/psql before the Fresh Base installer "
        "can install its declared Docker/Compose prerequisites"
    ),
    "schema": "animemo.vm-fresh-report/v1",
    "transportSource": "official-mirror",
}
RC13_REJECTION_FIXTURE_SHA256 = (
    "sha256:4b0cc9f65cd8a53262746677096ca20620d13c89c766ae7a835994e7c559d3b6"
)


def digest(character: str) -> str:
    return "sha256:" + character * 64


class ReleaseFixture:
    def __init__(self, source: InstallTransportSource) -> None:
        self.transport_source = source
        self.transport_policy = (
            LocalBundleTransportPolicy()
            if source is InstallTransportSource.LOCAL_BUNDLE
            else ExplicitTransportPolicy.github()
            if source is InstallTransportSource.GITHUB
            else ExplicitTransportPolicy.official_mirror()
        )
        self.evidence = ReleaseEvidence(
            version="v1.1.0-rc.13",
            channel="rc",
            commit="a" * 40,
            manifest_digest=digest("1"),
            material_identity_digest=digest("2"),
            deployment_identity_digest=digest("3"),
            deployment_profile="v1.1-instance-scoped",
            platform_profile="v1.1-standard-linux-amd64",
            transport_source=source,
            transport_policy_identity=self.transport_policy.identity,
        )
        self.refreshes: list[bool] = []

    def resolve(self, selector, *, refresh: bool):
        self.refreshes.append(refresh)
        return self.evidence

    def latest_evidence(self):
        return self.evidence


class PlatformFixture:
    def __init__(self, events: list[str], *, compatible: bool = True) -> None:
        self.events = events
        self.compatible = compatible

    def assess(self, profile: str):
        self.events.append("strict-platform-qualification")
        return PlatformEvidence(
            compatible=self.compatible,
            profile=profile,
            evidence_digest=digest("4"),
            reason_code=(
                "PLATFORM_QUALIFIED"
                if self.compatible
                else "PLATFORM_RUNTIME_UNSUPPORTED"
            ),
        )


class PlatformBootstrapFixture:
    events: ClassVar[list[str]] = []

    def plan(self, *, transport_source):
        self.events.append("platform-plan")
        return SimpleNamespace(
            plan_digest=digest("5"), transport_source=transport_source
        )

    def execute(self, plan, *, accepted_plan_digest):
        self.events.append("platform-execute")
        return SimpleNamespace(plan_digest=accepted_plan_digest)


class ProductionInstallerCompositionTests(unittest.TestCase):
    def test_formal_composition_closes_all_docker_process_boundaries(self) -> None:
        composition = build_production_composition()
        shared = composition.releases.image_acquirer.runner
        default_release = ProductionReleasePort(
            source=SimpleNamespace(transport_policy=ExplicitTransportPolicy.github())
        )

        self.assertIsInstance(shared, LocalDockerCommandRunner)
        self.assertIs(composition.runtime._target.runner, shared)
        self.assertIs(composition.runtime._fresh.runner, shared)
        self.assertIs(composition.runtime._fresh.doctor_acceptor.runner, shared)
        self.assertIsInstance(
            default_release.image_acquirer.runner,
            LocalDockerCommandRunner,
        )
        assert composition.releases._temporary is not None
        composition.releases._temporary.cleanup()
        composition.releases._temporary = None

    def test_local_docker_runner_closes_run_and_streaming_backup(self) -> None:
        class RecordingRunner:
            def __init__(self) -> None:
                self.calls: list[
                    tuple[str, tuple[str, ...], dict[str, str] | None]
                ] = []

            def run(self, argv, *, env=None, **kwargs):
                del kwargs
                self.calls.append(("run", tuple(argv), env))
                return SimpleNamespace(stdout="")

            def write_gzip(self, argv, path, *, env=None, **kwargs):
                del path, kwargs
                self.calls.append(("write_gzip", tuple(argv), env))
                return {"sha256": "0" * 64, "uncompressedBytes": 0}

        delegate = RecordingRunner()
        runner = LocalDockerCommandRunner(delegate)  # type: ignore[arg-type]
        hostile = {
            "ANIMEMO_API_IMAGE": "example.invalid/api@sha256:" + "1" * 64,
            "DOCKER_CONFIG": "/tmp/attacker-config",
            "DOCKER_CONTEXT": "remote-attacker",
            "DOCKER_HOST": "tcp://attacker.invalid:2375",
            "HOME": "/tmp/attacker-home",
            "PATH": "/tmp/attacker-bin:/usr/bin",
        }

        for argv in (
            ["/usr/bin/docker", "pull", "example.invalid/image@sha256:" + "2" * 64],
            ["/usr/bin/docker", "image", "inspect", "candidate"],
            ["/usr/bin/docker", "image", "load", "--input", "/tmp/image.tar"],
            ["/usr/bin/docker", "compose", "version"],
        ):
            runner.run(argv, env=hostile)
        runner.write_gzip(
            ["/usr/bin/docker", "compose", "exec", "postgres", "pg_dump"],
            Path("C:/unused/backup.sql.gz"),
            env=hostile,
        )

        self.assertEqual(
            [kind for kind, _, _ in delegate.calls],
            ["run", "run", "run", "run", "write_gzip"],
        )
        for _, argv, environment in delegate.calls:
            self.assertEqual(argv[1:3], ("--host", "unix:///var/run/docker.sock"))
            self.assertIsNotNone(environment)
            assert environment is not None
            self.assertEqual(environment["HOME"], "/nonexistent")
            self.assertEqual(environment["PATH"], "/usr/sbin:/usr/bin:/sbin:/bin")
            self.assertEqual(
                environment["ANIMEMO_API_IMAGE"], hostile["ANIMEMO_API_IMAGE"]
            )
            self.assertFalse(any(name.startswith("DOCKER_") for name in environment))

    def test_pre_instance_runtime_scan_pins_every_docker_query_to_local_socket(
        self,
    ) -> None:
        class RecordingRunner:
            def __init__(self) -> None:
                self.calls: list[tuple[str, ...]] = []

            def run(self, argv, **kwargs):
                del kwargs
                self.calls.append(tuple(argv))
                return SimpleNamespace(stdout="")

        runner = RecordingRunner()
        target = ProductionTargetPort(
            runner=LocalDockerCommandRunner(runner)  # type: ignore[arg-type]
        )
        target.namespace = SimpleNamespace(
            updater_service="animemo-updater@default.service",
            compose_project="animemo-default",
        )

        self.assertFalse(target._external_runtime_present())

        docker_calls = [call for call in runner.calls if call[0] == "/usr/bin/docker"]
        self.assertEqual(len(docker_calls), 3)
        self.assertTrue(
            all(
                call[1:3] == ("--host", "unix:///var/run/docker.sock")
                for call in docker_calls
            )
        )

    def test_rc13_fresh_fixture_is_rejected_before_bootstrap_and_plans_afterward(
        self,
    ) -> None:
        from installer.tests.test_runtime import (
            BootstrapGateFake,
            CompatibilityFake,
            ConfigurationFake,
            FreshFake,
            OperationFake,
            RestoreFake,
            TargetFake,
        )

        events: list[str] = []
        fixture_bytes = (
            json.dumps(
                RC13_REJECTION_FIXTURE,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode()
        self.assertEqual(
            "sha256:" + hashlib.sha256(fixture_bytes).hexdigest(),
            RC13_REJECTION_FIXTURE_SHA256,
        )
        releases = ReleaseFixture(InstallTransportSource.OFFICIAL_MIRROR)
        releases.evidence = replace(
            releases.evidence,
            commit=RC13_REJECTION_FIXTURE["mainSha"],
        )
        platform = PlatformFixture(events, compatible=False)
        operations = OperationFake()
        fresh = FreshFake()
        runtime = Installer(
            releases=releases,
            target=TargetFake(),
            platform=platform,
            compatibility=CompatibilityFake(),
            configuration=ConfigurationFake(),
            operations=operations,
            fresh=fresh,
            restore=RestoreFake(),
            bootstrap_privilege_gate=BootstrapGateFake(),
        )
        composition = ProductionInstallerComposition(
            runtime=runtime,
            releases=releases,
            platform=platform,
        )
        request = InstallRequest(
            mode=InstallerMode.FRESH,
            selector=ReleaseSelector(version="v1.1.0-rc.13"),
            public_origin="https://anime.example",
            transport_source=InstallTransportSource.OFFICIAL_MIRROR,
        )

        with self.assertRaises(InstallerError) as raised:
            runtime.plan(request)
        self.assertEqual(
            raised.exception.code,
            RC13_REJECTION_FIXTURE["attempts"][1]["reasonCode"],
        )
        self.assertEqual(
            RC13_REJECTION_FIXTURE["mainTree"],
            "498d243f72982a290907f7bb4106cdd5b5e83140",
        )
        self.assertTrue(
            all(
                attempt["instanceMutation"] == 0 and attempt["productRootMutation"] == 0
                for attempt in RC13_REJECTION_FIXTURE["attempts"]
            )
        )
        self.assertTrue(
            all(
                RC13_REJECTION_FIXTURE["finalProbe"][name] == 0
                for name in ("containerCount", "networkCount", "volumeCount")
            )
        )
        self.assertFalse(
            any(RC13_REJECTION_FIXTURE["finalProbe"]["productRoots"].values())
        )
        self.assertEqual(operations.events, [])
        self.assertEqual(fresh.calls, [])

        class ClosingPlatformBootstrap(PlatformBootstrapFixture):
            def execute(self, plan, *, accepted_plan_digest):
                receipt = super().execute(
                    plan,
                    accepted_plan_digest=accepted_plan_digest,
                )
                platform.compatible = True
                return receipt

        PlatformBootstrapFixture.events = events
        ClosingPlatformBootstrap.events = events
        gate = SimpleNamespace(verify_runtime_source=lambda **kwargs: None)
        with (
            mock.patch("installer.bootstrap.authorize_online_stage0"),
            mock.patch(
                "installer.production.ProductionBootstrapPrivilegeGate",
                return_value=gate,
            ),
            mock.patch(
                "installer.platform_bootstrap.ProductionPlatformBootstrap",
                ClosingPlatformBootstrap,
            ),
        ):
            session = composition.plan_platform(request, "2026-08-25T04:30:00Z")
            composition.execute_platform(session, session.plan.plan_digest)

        plan = runtime.plan(request)
        self.assertEqual(plan.release.version, "v1.1.0-rc.13")
        self.assertEqual(plan.platform.reason_code, "PLATFORM_QUALIFIED")
        self.assertEqual(operations.events, [])
        self.assertEqual(fresh.calls, [])

    def test_exact_rc13_report_drives_real_bootstrap_and_strict_plan_closure(
        self,
    ) -> None:
        from installer.tests.test_runtime import (
            BootstrapGateFake,
            CompatibilityFake,
            ConfigurationFake,
            FreshFake,
            OperationFake,
            RestoreFake,
            TargetFake,
        )

        probe = RC13_REJECTION_FIXTURE["finalProbe"]
        self.assertFalse(probe["docker"]["available"])
        self.assertFalse(probe["compose"]["available"])
        self.assertFalse(probe["pgDump"]["available"])
        self.assertFalse(probe["psql"]["available"])
        self.assertTrue(probe["systemd"]["available"])

        initial = BootstrapHostFacts(
            distribution_id="ubuntu",
            distribution_major="24.04",
            architecture="amd64",
            effective_uid=0,
            apt_available=True,
            apt_sources_trusted=True,
            apt_sources_identity="sha256:" + "a" * 64,
            systemd_available=True,
            docker_cli_present=False,
            docker_cli_available=False,
            docker_cli_trusted=True,
            docker_cli_identity=None,
            docker_service_active=False,
            docker_daemon_healthy=False,
            docker_daemon_identity=None,
            docker_socket_present=False,
            docker_socket_local=False,
            docker_socket_identity=None,
            compose_v2_present=False,
            compose_v2_available=False,
            compose_v2_identity=None,
            docker_config_identity="ABSENT",
            pg_dump_major=None,
            psql_major=None,
            installed_policy_packages=(),
        )
        final = replace(
            initial,
            docker_cli_present=True,
            docker_cli_available=True,
            docker_cli_identity="sha256:" + "b" * 64,
            docker_service_active=True,
            docker_daemon_healthy=True,
            docker_daemon_identity="sha256:" + "c" * 64,
            docker_socket_present=True,
            docker_socket_local=True,
            docker_socket_identity="sha256:" + "d" * 64,
            compose_v2_present=True,
            compose_v2_available=True,
            compose_v2_identity="sha256:" + "e" * 64,
            pg_dump_major=16,
            psql_major=16,
            installed_policy_packages=tuple(
                PLATFORM_PACKAGE_POLICY.body()["packageNames"]
            ),
        )

        class Facts:
            def __init__(self) -> None:
                self.calls = 0

            def __call__(self):
                self.calls += 1
                return initial if self.calls < 3 else final

        class TransactionRunner:
            def run(self, argv, *, timeout, environment):
                del timeout, environment
                if argv[0] == "/usr/bin/apt-cache" and argv[-2] == "policy":
                    package = argv[-1]
                    return PlatformCommandResult(
                        0,
                        stdout=(
                            f"{package}:\n"
                            "  Installed: (none)\n"
                            "  Candidate: 1.0-1ubuntu1\n"
                            "  Version table:\n"
                            "     1.0-1ubuntu1 500\n"
                            "        500 http://archive.ubuntu.com/ubuntu "
                            "noble/main amd64 Packages\n"
                        ).encode(),
                    )
                return PlatformCommandResult(0)

        bootstrap = ProductionPlatformBootstrap(
            facts_collector=Facts(),
            runner=TransactionRunner(),
            clock=lambda: "2026-08-25T04:30:00Z",
            lock_factory=lambda: nullcontext(),
        )
        bootstrap_plan = bootstrap.plan(
            transport_source=InstallTransportSource.OFFICIAL_MIRROR
        )
        self.assertEqual(bootstrap_plan.mode, PlatformBootstrapMode.ONLINE_FRESH)
        receipt = bootstrap.execute(
            bootstrap_plan,
            accepted_plan_digest=bootstrap_plan.plan_digest,
        )
        self.assertEqual(receipt.result, "PASS")

        image_digests = {
            "postgres": "docker.io/library/postgres@sha256:" + "1" * 64,
            "redis": "docker.io/library/redis@sha256:" + "2" * 64,
        }
        qualification = finalize_platform_qualification(
            {
                "schema": "animemo.platform-qualification/v1",
                "profile": "v1.1-standard-linux-amd64",
                "candidateSha": RC13_REJECTION_FIXTURE["mainSha"],
                "workflow": {
                    "path": ".github/workflows/platform-qualification.yml",
                    "ref": "refs/heads/main",
                    "sha": RC13_REJECTION_FIXTURE["mainSha"],
                },
                "run": {"id": "32700000000", "attempt": 1},
                "observedAt": "2026-08-25T00:00:00Z",
                "host": {
                    "os": "linux",
                    "architecture": "amd64",
                    "distributionId": "ubuntu",
                    "distributionVersion": "24.04",
                    "kernel": "fixture",
                    "systemdVersion": "fixture",
                    "dockerVersion": "fixture",
                    "composeVersion": "fixture",
                },
                "databasePath": {
                    "dumpFormat": "plain",
                    "sourceServerMajor": 16,
                    "pgDumpMajor": 16,
                    "psqlMajor": 16,
                    "targetServerMajor": 16,
                },
                "imageDigests": image_digests,
                "capabilities": {name: True for name in REQUIRED_CAPABILITIES},
                "rehearsals": {name: "PASS" for name in REQUIRED_REHEARSALS},
            }
        )
        with tempfile.TemporaryDirectory() as directory:
            qualification_path = Path(directory) / "platform-qualification.json"
            qualification_path.write_bytes(
                canonical_platform_qualification_bytes(qualification)
            )

            class Materials:
                manifest: ClassVar[dict[str, object]] = {
                    "release": {"commit": RC13_REJECTION_FIXTURE["mainSha"]}
                }

                def material(self, name):
                    self.last_name = name
                    return qualification_path

                def image(self, role):
                    return image_digests[role]

            class MaterialReleases:
                def latest_materials(self):
                    return Materials()

            strict_platform = ProductionPlatformPort(
                MaterialReleases(),  # type: ignore[arg-type]
                collector=lambda observed: HostCapabilityEvidence(
                    os="linux",
                    architecture="amd64",
                    profile=observed.profile,
                    capabilities={name: True for name in REQUIRED_CAPABILITIES},
                    database_path=observed.database_path,
                ),
            )
            self.assertTrue(strict_platform.assess(qualification.profile).compatible)

            releases = ReleaseFixture(InstallTransportSource.OFFICIAL_MIRROR)
            releases.evidence = replace(
                releases.evidence,
                commit=RC13_REJECTION_FIXTURE["mainSha"],
            )
            operations = OperationFake()
            fresh = FreshFake()
            runtime = Installer(
                releases=releases,
                target=TargetFake(),
                platform=strict_platform,
                compatibility=CompatibilityFake(),
                configuration=ConfigurationFake(),
                operations=operations,
                fresh=fresh,
                restore=RestoreFake(),
                bootstrap_privilege_gate=BootstrapGateFake(),
            )
            install_plan = runtime.plan(
                InstallRequest(
                    mode=InstallerMode.FRESH,
                    selector=ReleaseSelector(version="v1.1.0-rc.13"),
                    public_origin="https://anime.example",
                    transport_source=InstallTransportSource.OFFICIAL_MIRROR,
                )
            )

        self.assertEqual(install_plan.release.version, "v1.1.0-rc.13")
        self.assertTrue(install_plan.platform.compatible)
        self.assertEqual(operations.events, [])
        self.assertEqual(fresh.calls, [])

    def test_offline_platform_plan_also_requires_verified_protected_source(
        self,
    ) -> None:
        events: list[str] = []
        releases = ReleaseFixture(InstallTransportSource.LOCAL_BUNDLE)
        composition = ProductionInstallerComposition(
            runtime=object(),
            releases=releases,
            platform=PlatformFixture(events),
        )
        offline_root = Path(tempfile.gettempdir()).resolve() / "animemo-offline-fixture"
        request = InstallRequest(
            mode=InstallerMode.FRESH,
            selector=ReleaseSelector(version="v1.1.0-rc.13"),
            public_origin="https://anime.example",
            transport_source=InstallTransportSource.LOCAL_BUNDLE,
            local_bundle_payload=offline_root / "payload.tar",
            local_bundle_release_attestation=offline_root / "attestation.json",
        )
        PlatformBootstrapFixture.events = events
        gate = SimpleNamespace(
            verify_runtime_source=lambda **kwargs: events.append(
                "verified-runtime-source"
            )
        )
        with (
            mock.patch("installer.bootstrap.authorize_online_stage0") as authorize,
            mock.patch(
                "installer.production.ProductionBootstrapPrivilegeGate",
                return_value=gate,
            ),
            mock.patch(
                "installer.platform_bootstrap.ProductionPlatformBootstrap",
                PlatformBootstrapFixture,
            ),
        ):
            composition.plan_platform(request, "2026-08-25T04:30:00Z")

        authorize.assert_not_called()
        self.assertEqual(events, ["verified-runtime-source", "platform-plan"])

    def test_verified_release_and_runtime_source_precede_platform_plan(self) -> None:
        events: list[str] = []
        releases = ReleaseFixture(InstallTransportSource.OFFICIAL_MIRROR)
        platform = PlatformFixture(events)
        composition = ProductionInstallerComposition(
            runtime=object(),
            releases=releases,
            platform=platform,
        )
        request = InstallRequest(
            mode=InstallerMode.FRESH,
            selector=ReleaseSelector(version="v1.1.0-rc.13"),
            public_origin="https://anime.example",
            transport_source=InstallTransportSource.OFFICIAL_MIRROR,
        )
        PlatformBootstrapFixture.events = events
        gate = SimpleNamespace(
            verify_runtime_source=lambda **kwargs: events.append(
                "verified-runtime-source"
            )
        )

        def authorize(**kwargs):
            events.append("exact-release-and-asset-verified")

        with (
            mock.patch(
                "installer.bootstrap.authorize_online_stage0", side_effect=authorize
            ),
            mock.patch(
                "installer.production.ProductionBootstrapPrivilegeGate",
                return_value=gate,
            ),
            mock.patch(
                "installer.platform_bootstrap.ProductionPlatformBootstrap",
                PlatformBootstrapFixture,
            ),
        ):
            session = composition.plan_platform(
                request,
                "2026-08-25T04:30:00Z",
            )

        self.assertEqual(
            events,
            [
                "exact-release-and-asset-verified",
                "verified-runtime-source",
                "platform-plan",
            ],
        )
        self.assertEqual(releases.refreshes, [False])
        self.assertEqual(session.release, releases.evidence)

    def test_platform_execute_reverifies_release_then_strictly_qualifies(self) -> None:
        events: list[str] = []

        class OrderedReleaseFixture(ReleaseFixture):
            def resolve(self, selector, *, refresh: bool):
                events.append("release-refresh" if refresh else "release-resolve")
                return super().resolve(selector, refresh=refresh)

        releases = OrderedReleaseFixture(InstallTransportSource.GITHUB)
        composition = ProductionInstallerComposition(
            runtime=object(),
            releases=releases,
            platform=PlatformFixture(events),
        )
        request = InstallRequest(
            mode=InstallerMode.FRESH,
            selector=ReleaseSelector(version="v1.1.0-rc.13"),
            public_origin="https://anime.example",
            transport_source=InstallTransportSource.GITHUB,
        )
        PlatformBootstrapFixture.events = events
        gate = SimpleNamespace(verify_runtime_source=lambda **kwargs: None)
        with (
            mock.patch("installer.bootstrap.authorize_online_stage0"),
            mock.patch(
                "installer.production.ProductionBootstrapPrivilegeGate",
                return_value=gate,
            ),
            mock.patch(
                "installer.platform_bootstrap.ProductionPlatformBootstrap",
                PlatformBootstrapFixture,
            ),
        ):
            session = composition.plan_platform(request, "2026-08-25T04:30:00Z")
            events.clear()
            composition.execute_platform(session, session.plan.plan_digest)

        self.assertEqual(
            events,
            [
                "release-refresh",
                "platform-execute",
                "strict-platform-qualification",
            ],
        )


if __name__ == "__main__":
    unittest.main()
