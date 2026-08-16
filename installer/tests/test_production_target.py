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
    ProductionTargetPort,
)
from installer.runtime import (
    InstallerAdapterError,
    InstallerError,
    ListenRequest,
    PlatformEvidence,
    ReleaseEvidence,
    TargetClass,
)
from scripts.tests.test_durability_instance import locator_payload
from scripts.tests.test_managed_config import (
    INSTANCE_ID,
    REVISION,
    encoded,
)
from updater.tests.test_deployment import manifest


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
