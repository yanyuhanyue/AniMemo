from __future__ import annotations

import socket
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from durability.instance import (
    InstanceSnapshot,
    parse_instance_locator,
    release_identity_from_manifest,
)
from durability.managed_config import parse_managed_config
from installer import production
from installer.production import (
    ProductionManagedConfigurationPort,
    ProductionReleasePort,
    ProductionTargetPort,
)
from installer.runtime import (
    InstallerAdapterError,
    InstallerError,
    InstallTransportSource,
    ListenRequest,
    PlatformEvidence,
    ReleaseEvidence,
    ReleaseSelector,
    TargetClass,
)
from release.materials import VerifiedMaterialSet
from scripts.tests.test_durability_instance import locator_payload
from scripts.tests.test_managed_config import (
    INSTANCE_ID,
    REVISION,
    encoded,
)
from updater.tests.test_deployment import manifest
from updater.oci import AcquiredRuntimeImage, ImageAcquisitionReceipt
from updater.source import VerifiedReleaseMaterials
from updater.tests.test_source import stable_manifest
from updater.transport import ExplicitTransportPolicy


def digest(character: str) -> str:
    return "sha256:" + character * 64


class ReleaseFixture:
    def __init__(self, root: Path, app_root: Path) -> None:
        self.manifest = manifest()
        release_identity = release_identity_from_manifest(self.manifest)
        self.evidence = ReleaseEvidence(
            version=self.manifest["release"]["version"],
            channel=self.manifest["release"]["channel"],
            commit=self.manifest["release"]["commit"],
            manifest_digest=release_identity["manifestDigest"],
            material_identity_digest=digest("b"),
            deployment_identity_digest=digest("c"),
            deployment_profile="v1.1-standard",
            platform_profile="v1.1-standard-linux-amd64",
        )
        relative = Path("deploy/target-proof.txt")
        installed = app_root / relative
        installed.parent.mkdir(parents=True)
        installed.write_bytes(b"exact installer material\n")
        expected = root / "verified" / relative
        expected.parent.mkdir(parents=True)
        expected.write_bytes(installed.read_bytes())
        identity = SimpleNamespace(
            path=relative.as_posix(),
            size=installed.stat().st_size,
            mode=stat.S_IMODE(installed.stat().st_mode),
        )
        self.materials = SimpleNamespace(
            manifest=self.manifest,
            verified=SimpleNamespace(files=(identity,)),
            material=lambda path: expected
            if path == relative.as_posix()
            else (_ for _ in ()).throw(KeyError(path)),
        )
        self.installed = installed

    def latest_evidence(self):
        return self.evidence

    def materials_for(self, evidence):
        if evidence != self.evidence:
            raise AssertionError("unexpected release evidence")
        return self.materials


class PlatformFixture:
    def assess(self, profile: str) -> PlatformEvidence:
        return PlatformEvidence(
            compatible=profile == "v1.1-standard-linux-amd64",
            profile="v1.1-standard-linux-amd64",
            evidence_digest=digest("d"),
            reason_code="PLATFORM_QUALIFIED",
        )


class DoctorFixture:
    def __init__(self, *, incomplete: bool = False) -> None:
        self.incomplete = incomplete
        self.accepted = 0

    def accept_existing(self, **_kwargs) -> None:
        self.accepted += 1
        if self.incomplete:
            raise InstallerAdapterError(
                "INSTALL_DOCTOR_INCOMPLETE",
                mutation_occurred=False,
                recovery_required=True,
            )


class ConfigStoreFixture:
    def __init__(self, config) -> None:
        self.config = config

    def read(self):
        return self.config


class EmptyRuntimeRunner:
    def __init__(self, *, compose_resource: bool = False) -> None:
        self.compose_resource = compose_resource

    def run(self, argv):
        if argv[:3] == ["/usr/bin/systemctl", "show", "animemo-updater.service"]:
            return SimpleNamespace(stdout="not-found\n")
        if self.compose_resource and argv[1:3] == ["container", "ls"]:
            return SimpleNamespace(stdout="container-id\n")
        return SimpleNamespace(stdout="")


class ProductionTargetPortTests(unittest.TestCase):
    def test_local_bundle_is_blocked_at_the_production_authority_gate(self) -> None:
        with self.assertRaisesRegex(
            InstallerError,
            "BLOCKED_PORTABLE_PUBLICATION_AUTHORITY",
        ):
            ProductionReleasePort(
                transport_source=InstallTransportSource.LOCAL_BUNDLE,
            )

    def test_injected_resolver_must_use_the_selected_policy(self) -> None:
        resolver = SimpleNamespace(
            transport_policy=ExplicitTransportPolicy.official_mirror()
        )
        with self.assertRaisesRegex(
            InstallerError,
            "INSTALL_RELEASE_RESOLVER_POLICY_MISMATCH",
        ):
            ProductionReleasePort(source=resolver)  # type: ignore[arg-type]

    def test_release_and_image_acquisition_use_the_same_explicit_policy(self) -> None:
        policy = ExplicitTransportPolicy.official_mirror()
        release_manifest = stable_manifest()
        with tempfile.TemporaryDirectory() as temporary:
            materials = VerifiedReleaseMaterials(
                manifest=release_manifest,
                deployment_contract={},
                verified=VerifiedMaterialSet(
                    root=Path(temporary),
                    archive_sha256=digest("a"),
                    files=(),
                ),
                identity_digest=digest("b"),
            )

            class Resolver:
                transport_policy = policy

                def fetch_verified_materials(self, version, **_kwargs):
                    if version != "v1.0.0":
                        raise AssertionError("unexpected release")
                    return materials

            class Acquirer:
                def __init__(self) -> None:
                    self.calls = []

                def acquire(self, candidate, candidate_policy):
                    self.calls.append((candidate, candidate_policy))
                    return ImageAcquisitionReceipt(
                        verified_release_identity=candidate.identity_digest,
                        transport_policy_identity=candidate_policy.identity,
                        images=tuple(
                            AcquiredRuntimeImage(
                                role=role,
                                canonical_reference=candidate.image(role),
                                observed_reference=candidate.image(role),
                            )
                            for role in ("api", "postgres", "redis", "web")
                        ),
                        identity="c" * 64,
                    )

            acquirer = Acquirer()
            releases = ProductionReleasePort(
                source=Resolver(),
                transport_source=InstallTransportSource.OFFICIAL_MIRROR,
                transport_policy=policy,
                image_acquirer=acquirer,  # type: ignore[arg-type]
            )
            evidence = releases.resolve(
                ReleaseSelector(version="v1.0.0"),
                refresh=False,
            )
            receipt = releases.acquire_images(evidence)

        self.assertEqual(acquirer.calls, [(materials, policy)])
        self.assertEqual(receipt.verified_release_identity, materials.identity_digest)
        self.assertEqual(receipt.transport_policy_identity, policy.identity)

    def test_launcher_without_locator_is_partial_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            roots = tuple(root / name for name in ("app", "data", "updater", "state", "run"))
            launcher = root / "animemo-updater"
            launcher.write_bytes(b"partial launcher")
            with (
                mock.patch.object(production, "_CANONICAL_ROOTS", roots),
                mock.patch.object(
                    production,
                    "_CANONICAL_EXTERNAL_ARTIFACTS",
                    (launcher,),
                ),
            ):
                evidence = ProductionTargetPort(
                    runner=EmptyRuntimeRunner()
                ).inspect()

        self.assertEqual(evidence.classification, TargetClass.PARTIAL_AMBIGUOUS)

    def test_compose_resources_without_locator_are_partial_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            roots = tuple(root / name for name in ("app", "data", "updater", "state", "run"))
            with (
                mock.patch.object(production, "_CANONICAL_ROOTS", roots),
                mock.patch.object(
                    production, "_CANONICAL_EXTERNAL_ARTIFACTS", ()
                ),
            ):
                evidence = ProductionTargetPort(
                    runner=EmptyRuntimeRunner(compose_resource=True)
                ).inspect()

        self.assertEqual(evidence.classification, TargetClass.PARTIAL_AMBIGUOUS)

    def inspect_target(
        self,
        root: Path,
        *,
        tamper_material: bool = False,
        doctor_incomplete: bool = False,
    ):
        app_root = root / "app"
        releases = ReleaseFixture(root, app_root)
        if tamper_material:
            releases.installed.write_bytes(b"different installed material\n")
        config = parse_managed_config(encoded())
        payload = locator_payload()
        payload["instanceId"] = INSTANCE_ID
        payload["configRevision"] = REVISION
        payload["releaseIdentity"] = dict(
            release_identity_from_manifest(releases.manifest)
        )
        locator = parse_instance_locator(payload)
        snapshot = InstanceSnapshot(
            locator=locator,
            digest=digest("e"),
            storage_digest=digest("f"),
        )
        locator_marker = root / "instance.json"
        locator_marker.write_text("published", encoding="utf-8")
        doctor = DoctorFixture(incomplete=doctor_incomplete)
        target = ProductionTargetPort(
            releases=releases,
            platform=PlatformFixture(),
            doctor=doctor,
        )
        with (
            mock.patch.object(production, "APP_ROOT", app_root),
            mock.patch.object(production, "INSTANCE_LOCATOR_PATH", locator_marker),
            mock.patch.object(
                production, "load_instance_snapshot", return_value=snapshot
            ),
            mock.patch.object(
                production,
                "LocalManagedConfigStore",
                return_value=ConfigStoreFixture(config),
            ),
        ):
            evidence = target.inspect()
        return evidence, releases, doctor

    def test_exact_healthy_same_version_is_proven_safe_for_no_change(self):
        with tempfile.TemporaryDirectory() as directory:
            evidence, releases, doctor = self.inspect_target(Path(directory))

        self.assertEqual(evidence.classification, TargetClass.ACTIVE)
        self.assertEqual(
            evidence.release_manifest_digest,
            releases.evidence.manifest_digest,
        )
        self.assertEqual(
            evidence.material_identity_digest,
            releases.evidence.material_identity_digest,
        )
        self.assertTrue(evidence.exact_release_running)
        self.assertTrue(evidence.doctor_complete)
        self.assertEqual(doctor.accepted, 1)

    def test_installed_material_mismatch_cannot_be_treated_as_no_change(self):
        with tempfile.TemporaryDirectory() as directory:
            evidence, _, doctor = self.inspect_target(
                Path(directory), tamper_material=True
            )

        self.assertEqual(evidence.classification, TargetClass.ACTIVE)
        self.assertIsNone(evidence.material_identity_digest)
        self.assertFalse(evidence.exact_release_running)
        self.assertFalse(evidence.doctor_complete)
        self.assertEqual(doctor.accepted, 0)

    def test_incomplete_required_doctor_checks_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            evidence, releases, doctor = self.inspect_target(
                Path(directory), doctor_incomplete=True
            )

        self.assertEqual(evidence.classification, TargetClass.ACTIVE)
        self.assertEqual(
            evidence.material_identity_digest,
            releases.evidence.material_identity_digest,
        )
        self.assertFalse(evidence.exact_release_running)
        self.assertFalse(evidence.doctor_complete)
        self.assertEqual(doctor.accepted, 1)


class ProductionConfigurationPreflightTests(unittest.TestCase):
    def test_config_publication_becomes_the_expected_operation_state(self) -> None:
        with (
            tempfile.TemporaryDirectory() as config_directory,
            tempfile.TemporaryDirectory() as runtime_directory,
        ):
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as available:
                available.bind(("127.0.0.1", 0))
                port = available.getsockname()[1]
            store = production.LocalManagedConfigStore(
                config_root=Path(config_directory),
                runtime_root=Path(runtime_directory),
            )
            adapter = ProductionManagedConfigurationPort(store)
            plan = adapter.plan(
                instance_id=INSTANCE_ID,
                public_origin="https://anime.example",
                listen=ListenRequest("127.0.0.1", port),
                insecure_http_accepted=False,
            )

            adapter.publish(plan)

            self.assertEqual(adapter.config_for(plan).instance_id, INSTANCE_ID)

    def test_requested_port_collision_fails_during_plan(self) -> None:
        with (
            tempfile.TemporaryDirectory() as config_directory,
            tempfile.TemporaryDirectory() as runtime_directory,
            socket.socket(socket.AF_INET, socket.SOCK_STREAM) as occupied,
        ):
            occupied.bind(("127.0.0.1", 0))
            occupied.listen()
            port = occupied.getsockname()[1]
            store = production.LocalManagedConfigStore(
                config_root=Path(config_directory),
                runtime_root=Path(runtime_directory),
            )
            adapter = ProductionManagedConfigurationPort(store)

            with self.assertRaisesRegex(InstallerError, "INSTALL_PORT_CONFLICT"):
                adapter.plan(
                    instance_id=INSTANCE_ID,
                    public_origin="https://anime.example",
                    listen=ListenRequest("127.0.0.1", port),
                    insecure_http_accepted=False,
                )

    def test_port_is_rechecked_at_execution_boundary(self) -> None:
        with (
            tempfile.TemporaryDirectory() as config_directory,
            tempfile.TemporaryDirectory() as runtime_directory,
            socket.socket(socket.AF_INET, socket.SOCK_STREAM) as reservation,
        ):
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
            reservation.close()
            store = production.LocalManagedConfigStore(
                config_root=Path(config_directory),
                runtime_root=Path(runtime_directory),
            )
            adapter = ProductionManagedConfigurationPort(store)
            plan = adapter.plan(
                instance_id=INSTANCE_ID,
                public_origin="https://anime.example",
                listen=ListenRequest("127.0.0.1", port),
                insecure_http_accepted=False,
            )
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as occupied:
                occupied.bind(("127.0.0.1", port))
                occupied.listen()
                with self.assertRaisesRegex(
                    InstallerError, "INSTALL_PORT_CONFLICT"
                ):
                    adapter.revalidate(plan)


if __name__ == "__main__":
    unittest.main()
