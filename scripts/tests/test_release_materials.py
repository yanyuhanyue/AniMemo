from __future__ import annotations

import hashlib
import io
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from durability.platform import (
    canonical_platform_qualification_bytes,
    finalize_platform_qualification,
)
from release import materials
from release.contract import build_deployment_contract, validate_deployment_contract
from release.formal_windows_pretrust import (
    FORMAL_WINDOWS_PRETRUST_FILES,
    FORMAL_WINDOWS_PRETRUST_PREFIX,
    inspect_formal_windows_pretrust_in_installer_materials,
)
from release.materials import (
    INITIAL_TRUST_KIT_PREFIX,
    PLATFORM_QUALIFICATION_MATERIAL,
    MaterialContractError,
    _validate_dynamic_material,
    build_installer_materials,
    extract_installer_materials,
)
from scripts.tests.formal_windows_pretrust_fixture import (
    create_test_formal_windows_pretrust_kit,
)
from scripts.tests.test_platform_qualification import unsigned_payload
from scripts.tests.trust_kit_fixture import create_test_initial_trust_kit

ROOT = Path(__file__).resolve().parents[2]


class InstallerMaterialsTests(unittest.TestCase):
    def test_bound_directory_io_requires_nofollow_without_global_listdir_downgrade(
        self,
    ) -> None:
        supported_dir_fd = {materials.os.open, materials.os.stat, materials.os.unlink}
        with (
            mock.patch.object(materials.os, "supports_dir_fd", supported_dir_fd),
            mock.patch.object(materials.os, "O_DIRECTORY", 1, create=True),
            mock.patch.object(
                materials.os,
                "supports_fd",
                {materials.os.listdir},
            ),
            mock.patch.object(materials.os, "O_NOFOLLOW", 0, create=True),
        ):
            self.assertFalse(materials.bound_release_directory_io_available())
        with (
            mock.patch.object(materials.os, "supports_dir_fd", supported_dir_fd),
            mock.patch.object(materials.os, "O_DIRECTORY", 1, create=True),
            mock.patch.object(materials.os, "supports_fd", set()),
            mock.patch.object(materials.os, "O_NOFOLLOW", 1, create=True),
        ):
            self.assertTrue(materials.bound_release_directory_io_available())

    @staticmethod
    def contract(identity):
        return {
            "schemaVersion": 2,
            "profile": "v1.1-instance-scoped",
            "platform": "linux/amd64",
            "archive": {
                "name": "installer-materials.tar",
                "sha256": identity.sha256,
                "size": identity.size,
                "format": "tar",
            },
            "materials": [item.as_dict() for item in identity.files],
        }

    def test_builder_produces_a_deterministic_uncompressed_offline_material_archive(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            wheelhouse = temporary / "wheelhouse"
            wheelhouse.mkdir()
            (wheelhouse / "qualified_dependency-1.0-py3-none-any.whl").write_bytes(
                b"qualified wheel bytes"
            )
            first = temporary / "first.tar"
            second = temporary / "second.tar"
            trust_kit = create_test_initial_trust_kit(temporary)
            formal_windows_kit = create_test_formal_windows_pretrust_kit(
                temporary, source_initial_trust_kit=trust_kit
            )

            first_identity = build_installer_materials(
                ROOT,
                wheelhouse=wheelhouse,
                output=first,
                initial_trust_kit=trust_kit,
                formal_windows_pretrust_kit=formal_windows_kit,
            )
            second_identity = build_installer_materials(
                ROOT,
                wheelhouse=wheelhouse,
                output=second,
                initial_trust_kit=trust_kit,
                formal_windows_pretrust_kit=formal_windows_kit,
            )

            self.assertEqual(first.read_bytes(), second.read_bytes())
            self.assertEqual(
                first_identity.sha256,
                "sha256:" + hashlib.sha256(first.read_bytes()).hexdigest(),
            )
            self.assertEqual(first_identity, second_identity)
            with tarfile.open(first, mode="r:") as archive:
                members = archive.getmembers()
            names = [member.name for member in members]
            self.assertEqual(names, sorted(names))
            self.assertIn("deploy/docker-compose.yml", names)
            self.assertIn("deploy/updater/animemo", names)
            self.assertIn("deploy/updater/animemo-updater", names)
            launcher = next(
                member
                for member in members
                if member.name == "deploy/updater/animemo-updater"
            )
            self.assertEqual(launcher.mode, 0o755)
            operator = next(
                member for member in members if member.name == "deploy/updater/animemo"
            )
            self.assertEqual(operator.mode, 0o755)
            guest_pretrust_verifier = next(
                member
                for member in members
                if member.name
                == f"{INITIAL_TRUST_KIT_PREFIX}/offline-release-verifier"
            )
            self.assertEqual(guest_pretrust_verifier.mode, 0o755)
            self.assertIn("durability/backup_cli.py", names)
            self.assertIn("durability/backup_production.py", names)
            self.assertIn("durability/managed_config.py", names)
            self.assertIn("release/contract.py", names)
            self.assertIn("scripts/candidate_profile_runner.py", names)
            self.assertIn("scripts/closed_runtime_inventory.py", names)
            self.assertIn("scripts/formal_profile_runner.py", names)
            inventory_program = next(
                item
                for item in first_identity.files
                if item.path == "scripts/closed_runtime_inventory.py"
            )
            inventory_bytes = (ROOT / inventory_program.path).read_bytes()
            self.assertEqual(
                inventory_program.sha256,
                "sha256:" + hashlib.sha256(inventory_bytes).hexdigest(),
            )
            self.assertEqual(inventory_program.size, len(inventory_bytes))
            self.assertEqual(inventory_program.mode, 0o644)
            self.assertIn("updater/source.py", names)
            self.assertEqual(
                {
                    name.removeprefix(FORMAL_WINDOWS_PRETRUST_PREFIX + "/")
                    for name in names
                    if name.startswith(FORMAL_WINDOWS_PRETRUST_PREFIX + "/")
                },
                FORMAL_WINDOWS_PRETRUST_FILES,
            )
            binding = inspect_formal_windows_pretrust_in_installer_materials(
                first
            )
            self.assertEqual(binding.installer_materials_sha256, first_identity.sha256)
            self.assertNotEqual(
                binding.windows_host_verifier_identity,
                binding.linux_guest_verifier_identity,
            )
            self.assertIn("wheelhouse/qualified_dependency-1.0-py3-none-any.whl", names)
            self.assertTrue(all(member.isfile() for member in members))
            self.assertTrue(
                all(
                    member.mtime == 0
                    and member.uid == 0
                    and member.gid == 0
                    and member.uname == ""
                    and member.gname == ""
                    for member in members
                )
            )

    def test_production_material_builder_requires_initial_trust_kit(self):
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            wheelhouse = temporary / "wheelhouse"
            wheelhouse.mkdir()
            (wheelhouse / "qualified_dependency-1.0-py3-none-any.whl").write_bytes(
                b"qualified wheel bytes"
            )

            with self.assertRaisesRegex(
                MaterialContractError,
                "Initial pretrust kit is required",
            ):
                build_installer_materials(
                    ROOT,
                    wheelhouse=wheelhouse,
                    output=temporary / "installer-materials.tar",
                )

    def test_production_material_builder_requires_formal_windows_pretrust(self):
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            wheelhouse = temporary / "wheelhouse"
            wheelhouse.mkdir()
            (wheelhouse / "qualified_dependency-1.0-py3-none-any.whl").write_bytes(
                b"qualified wheel bytes"
            )
            trust_kit = create_test_initial_trust_kit(temporary)

            with self.assertRaisesRegex(
                MaterialContractError,
                "Formal Windows pretrust kit is required",
            ):
                build_installer_materials(
                    ROOT,
                    wheelhouse=wheelhouse,
                    output=temporary / "installer-materials.tar",
                    initial_trust_kit=trust_kit,
                )

    def test_staged_platform_qualification_must_be_canonical_evidence(self):
        _validate_dynamic_material("release/ordinary.json", b"{}")
        with self.assertRaisesRegex(
            MaterialContractError, "Platform qualification material is invalid"
        ):
            _validate_dynamic_material(PLATFORM_QUALIFICATION_MATERIAL, b"{}")

    def test_offline_release_verifier_is_packaged_as_an_executable(self):
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            source_root = temporary / "source"
            fixed = (
                "deploy/docker-compose.yml",
                "deploy/install-updater.sh",
                "deploy/updater/animemo",
                "deploy/updater/animemo-updater",
                "deploy/updater/animemo-updater@.service",
                "deploy/updater/animemo-updater.sysusers.conf",
                "deploy/updater/animemo-updater.tmpfiles.conf",
                "scripts/candidate_profile_runner.py",
                "scripts/closed_runtime_inventory.py",
                "scripts/formal_profile_runner.py",
            )
            for relative in fixed:
                target = source_root / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(relative + "\n", encoding="utf-8")
            for package in ("durability", "release", "updater", "installer"):
                package_root = source_root / package
                package_root.mkdir(parents=True, exist_ok=True)
                (package_root / "__init__.py").write_text("", encoding="utf-8")
            verifier = (
                source_root
                / "release"
                / "release_attestation_verifier"
                / "offline-release-verifier"
            )
            verifier.parent.mkdir(parents=True)
            verifier.write_bytes(b"qualified linux verifier")
            wheelhouse = temporary / "wheelhouse"
            wheelhouse.mkdir()
            (wheelhouse / "qualified_dependency-1.0-py3-none-any.whl").write_bytes(
                b"qualified wheel bytes"
            )

            initial_trust_kit = create_test_initial_trust_kit(temporary)
            formal_windows_kit = create_test_formal_windows_pretrust_kit(
                temporary, source_initial_trust_kit=initial_trust_kit
            )
            identity = build_installer_materials(
                source_root,
                wheelhouse=wheelhouse,
                output=temporary / "installer-materials.tar",
                initial_trust_kit=initial_trust_kit,
                formal_windows_pretrust_kit=formal_windows_kit,
            )

            packaged = next(
                item
                for item in identity.files
                if item.path
                == "release/release_attestation_verifier/offline-release-verifier"
            )
            self.assertEqual(packaged.mode, 0o755)

    def test_valid_staged_platform_qualification_is_bound_into_the_archive(self):
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            source_root = temporary / "source"
            fixed = (
                "deploy/docker-compose.yml",
                "deploy/install-updater.sh",
                "deploy/updater/animemo",
                "deploy/updater/animemo-updater",
                "deploy/updater/animemo-updater@.service",
                "deploy/updater/animemo-updater.sysusers.conf",
                "deploy/updater/animemo-updater.tmpfiles.conf",
                "scripts/candidate_profile_runner.py",
                "scripts/closed_runtime_inventory.py",
                "scripts/formal_profile_runner.py",
            )
            for relative in fixed:
                target = source_root / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(relative + "\n", encoding="utf-8")
            for package in ("durability", "release", "updater", "installer"):
                package_root = source_root / package
                package_root.mkdir(parents=True, exist_ok=True)
                (package_root / "__init__.py").write_text("", encoding="utf-8")
            evidence = canonical_platform_qualification_bytes(
                finalize_platform_qualification(unsigned_payload())
            )
            (source_root / PLATFORM_QUALIFICATION_MATERIAL).write_bytes(evidence)
            wheelhouse = temporary / "wheelhouse"
            wheelhouse.mkdir()
            (wheelhouse / "qualified_dependency-1.0-py3-none-any.whl").write_bytes(
                b"qualified wheel bytes"
            )

            initial_trust_kit = create_test_initial_trust_kit(temporary)
            formal_windows_kit = create_test_formal_windows_pretrust_kit(
                temporary, source_initial_trust_kit=initial_trust_kit
            )
            identity = build_installer_materials(
                source_root,
                wheelhouse=wheelhouse,
                output=temporary / "installer-materials.tar",
                initial_trust_kit=initial_trust_kit,
                formal_windows_pretrust_kit=formal_windows_kit,
            )

            platform_material = next(
                item
                for item in identity.files
                if item.path == PLATFORM_QUALIFICATION_MATERIAL
            )
            self.assertEqual(platform_material.size, len(evidence))
            self.assertEqual(
                platform_material.sha256,
                "sha256:" + hashlib.sha256(evidence).hexdigest(),
            )

    def test_exact_extraction_returns_a_durable_verified_material_set(self):
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            wheelhouse = temporary / "wheelhouse"
            wheelhouse.mkdir()
            wheel = wheelhouse / "qualified_dependency-1.0-py3-none-any.whl"
            wheel.write_bytes(b"qualified wheel bytes")
            archive = temporary / "installer-materials.tar"
            initial_trust_kit = create_test_initial_trust_kit(temporary)
            formal_windows_kit = create_test_formal_windows_pretrust_kit(
                temporary, source_initial_trust_kit=initial_trust_kit
            )
            identity = build_installer_materials(
                ROOT,
                wheelhouse=wheelhouse,
                output=archive,
                initial_trust_kit=initial_trust_kit,
                formal_windows_pretrust_kit=formal_windows_kit,
            )
            destination = temporary / "verified"

            verified = extract_installer_materials(
                archive, self.contract(identity), destination
            )

            material = verified.material(
                "wheelhouse/qualified_dependency-1.0-py3-none-any.whl"
            )
            self.assertEqual(material.read_bytes(), b"qualified wheel bytes")
            self.assertTrue(material.is_relative_to(destination))
            self.assertTrue(destination.is_dir())

    def test_extraction_rejects_link_entries_and_removes_owned_staging(self):
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            trust_kit = create_test_initial_trust_kit(temporary)
            formal_windows_kit = create_test_formal_windows_pretrust_kit(
                temporary, source_initial_trust_kit=trust_kit
            )
            archive = temporary / "installer-materials.tar"
            fixture_sources = [
                (f"{INITIAL_TRUST_KIT_PREFIX}/{source.name}", source)
                for source in trust_kit.iterdir()
            ] + [
                (f"{FORMAL_WINDOWS_PRETRUST_PREFIX}/{source.name}", source)
                for source in formal_windows_kit.iterdir()
            ]
            with tarfile.open(archive, mode="w:", format=tarfile.USTAR_FORMAT) as tar:
                fixture_materials = []
                for relative, source in sorted(fixture_sources):
                    value = source.read_bytes()
                    member = tarfile.TarInfo(relative)
                    member.size = len(value)
                    member.mode = (
                        0o755
                        if source.name
                        in {"formal-release-verifier.exe", "offline-release-verifier"}
                        else 0o644
                    )
                    member.mtime = 0
                    member.uid = 0
                    member.gid = 0
                    member.uname = ""
                    member.gname = ""
                    tar.addfile(member, io.BytesIO(value))
                    fixture_materials.append(
                        {
                            "path": relative,
                            "sha256": "sha256:" + hashlib.sha256(value).hexdigest(),
                            "size": len(value),
                            "mode": format(member.mode, "04o"),
                        }
                    )
                member = tarfile.TarInfo("zz-payload")
                member.type = tarfile.SYMTYPE
                member.linkname = "outside"
                member.mode = 0o644
                member.mtime = 0
                tar.addfile(member)
            payload = archive.read_bytes()
            contract = {
                "schemaVersion": 2,
                "profile": "v1.1-instance-scoped",
                "platform": "linux/amd64",
                "archive": {
                    "name": "installer-materials.tar",
                    "sha256": "sha256:" + hashlib.sha256(payload).hexdigest(),
                    "size": len(payload),
                    "format": "tar",
                },
                "materials": [
                    *fixture_materials,
                    {
                        "path": "zz-payload",
                        "sha256": "sha256:" + hashlib.sha256(b"").hexdigest(),
                        "size": 0,
                        "mode": "0644",
                    }
                ],
            }
            destination = temporary / "verified"

            with self.assertRaisesRegex(MaterialContractError, "entry differs"):
                extract_installer_materials(archive, contract, destination)

            self.assertFalse(destination.exists())

    def test_deployment_contract_v2_binds_the_profile_archive_and_every_material(self):
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            wheelhouse = temporary / "wheelhouse"
            wheelhouse.mkdir()
            (wheelhouse / "qualified_dependency-1.0-py3-none-any.whl").write_bytes(
                b"qualified wheel bytes"
            )
            archive = temporary / "installer-materials.tar"
            initial_trust_kit = create_test_initial_trust_kit(temporary)
            formal_windows_kit = create_test_formal_windows_pretrust_kit(
                temporary, source_initial_trust_kit=initial_trust_kit
            )
            identity = build_installer_materials(
                ROOT,
                wheelhouse=wheelhouse,
                output=archive,
                initial_trust_kit=initial_trust_kit,
                formal_windows_pretrust_kit=formal_windows_kit,
            )

            contract = build_deployment_contract(ROOT, installer_materials=archive)
            validate_deployment_contract(
                contract, root=ROOT, installer_materials=archive
            )

            self.assertEqual(contract["schemaVersion"], 2)
            self.assertEqual(contract["profile"], "v1.1-instance-scoped")
            self.assertEqual(contract["platform"], "linux/amd64")
            self.assertEqual(contract["archive"]["sha256"], identity.sha256)
            self.assertEqual(contract["archive"]["format"], "tar")
            self.assertEqual(
                contract["materials"], [item.as_dict() for item in identity.files]
            )


if __name__ == "__main__":
    unittest.main()
