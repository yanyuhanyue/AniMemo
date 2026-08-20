from __future__ import annotations

import copy
import hashlib
import inspect
import io
import json
import tarfile
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from release.contract import (
    POSTGRES_DIGEST,
    POSTGRES_REPOSITORY,
    REDIS_DIGEST,
    REDIS_REPOSITORY,
    ReleaseContractError,
    assert_tag_absent,
    build_deployment_contract,
    build_manifest,
    build_provenance_plan,
    deployment_contract_digest,
    previous_stable_tag,
    promote_manifest,
    resolve_prerelease,
    validate_deployment_contract,
    validate_manifest,
)
from scripts.tests.trust_kit_fixture import contract_only_test_pretrust_bytes

COMMIT = "a" * 40
API_DIGEST = "sha256:" + "1" * 64
WEB_DIGEST = "sha256:" + "2" * 64
DEPLOYMENT_FILES = [
    {"path": "deploy/docker-compose.yml", "sha256": "sha256:" + "d" * 64},
    {"path": "updater/docker-compose.runtime.yml", "sha256": "sha256:" + "e" * 64},
]
MATERIAL_BYTES = b"qualified wheel bytes"
MATERIAL_PATH = "wheelhouse/qualified_dependency-1.0-py3-none-any.whl"
MATERIAL_DIGEST = "sha256:" + "f" * 64
MATERIAL_FILES = sorted([
    {
        "path": MATERIAL_PATH,
        "sha256": "sha256:" + hashlib.sha256(MATERIAL_BYTES).hexdigest(),
        "size": len(MATERIAL_BYTES),
        "mode": "0644",
    }
] + [
    {
        "path": path,
        "sha256": "sha256:" + hashlib.sha256(value).hexdigest(),
        "size": len(value),
        "mode": "0755" if path.endswith("/offline-release-verifier") else "0644",
    }
    for path, value in contract_only_test_pretrust_bytes().items()
], key=lambda item: item["path"])
DEPLOYMENT_CONTRACT = {
    "schemaVersion": 2,
    "profile": "v1.1-standard",
    "platform": "linux/amd64",
    "archive": {
        "name": "installer-materials.tar",
        "sha256": MATERIAL_DIGEST,
        "size": 10240,
        "format": "tar",
    },
    "files": DEPLOYMENT_FILES,
    "materials": MATERIAL_FILES,
}
DEPLOYMENT_DIGEST = deployment_contract_digest(
    DEPLOYMENT_CONTRACT
)


def write_material_archive(path: Path) -> None:
    with tarfile.open(path, mode="w:", format=tarfile.USTAR_FORMAT) as archive:
        values = dict(contract_only_test_pretrust_bytes())
        values[MATERIAL_PATH] = MATERIAL_BYTES
        for relative, material in sorted(values.items()):
            member = tarfile.TarInfo(relative)
            member.size = len(material)
            member.mode = 0o755 if relative.endswith("/offline-release-verifier") else 0o644
            member.mtime = 0
            member.uid = 0
            member.gid = 0
            member.uname = ""
            member.gname = ""
            archive.addfile(member, io.BytesIO(material))


def manifest(**overrides):
    values = {
        "version": "v1.0.0-rc.1",
        "channel": "rc",
        "commit": COMMIT,
        "created_at": datetime(2026, 8, 12, tzinfo=timezone.utc),
        "api_digest": API_DIGEST,
        "web_digest": WEB_DIGEST,
        "deployment_contract_sha256": DEPLOYMENT_DIGEST,
        "deployment_files": DEPLOYMENT_FILES,
        "installer_materials_sha256": MATERIAL_DIGEST,
        "minimum_updater_version": "1.0.0",
        "database_contract": "animemo-db-v1",
        "database_accepts": ["animemo-db-v1"],
        "migration_required": False,
        "migration_policy": "none",
        "application_rollback": "safe",
        "configuration_contract": "animemo-config-v1",
        "configuration_accepts": ["animemo-config-v1"],
        "plugin_sdk_apis": [2],
    }
    values.update(overrides)
    return build_manifest(**values)


class VersionResolutionTests(unittest.TestCase):
    def test_initial_release_line_requires_explicit_bootstrap_version(self):
        with self.assertRaisesRegex(ReleaseContractError, "target-version-override"):
            resolve_prerelease(tags=[], bump="patch", channel="rc")

        plan = resolve_prerelease(
            tags=[],
            bump="patch",
            channel="rc",
            target_version_override="v1.0.0",
        )
        self.assertEqual(plan, {"targetVersion": "v1.0.0", "releaseTag": "v1.0.0-rc.1", "sequence": 1})

    def test_existing_release_line_uses_bump_and_next_channel_sequence(self):
        plan = resolve_prerelease(
            tags=["v1.0.0", "v1.0.1-beta.1", "v1.0.1-beta.2", "v1.0.1-rc.10"],
            bump="patch",
            channel="rc",
        )
        self.assertEqual(plan["targetVersion"], "v1.0.1")
        self.assertEqual(plan["releaseTag"], "v1.0.1-rc.11")
        self.assertEqual(plan["sequence"], 11)

    def test_bootstrap_override_is_rejected_after_a_stable_tag_exists(self):
        with self.assertRaisesRegex(ReleaseContractError, "bootstrap"):
            resolve_prerelease(
                tags=["v1.0.0"],
                bump="minor",
                channel="beta",
                target_version_override="v1.1.0",
            )

    def test_invalid_or_existing_target_tag_is_rejected(self):
        with self.assertRaises(ReleaseContractError):
            resolve_prerelease(tags=[], bump="patch", channel="beta", target_version_override="1.0.0")
        with self.assertRaisesRegex(ReleaseContractError, "already exists"):
            assert_tag_absent("v1.0.0-rc.1", ["v1.0.0-rc.1"])

    def test_previous_stable_ignores_prereleases_and_target(self):
        self.assertEqual(
            previous_stable_tag(
                ["v1.0.0", "v1.1.0-beta.1", "v1.1.0", "v1.2.0-rc.1"],
                target="v1.2.0",
            ),
            "v1.1.0",
        )
        self.assertIsNone(previous_stable_tag(["v1.0.0-rc.1"], target="v1.0.0"))


class ManifestContractTests(unittest.TestCase):
    def test_installer_materials_identity_is_explicit_and_cannot_be_zero(self):
        parameter = inspect.signature(build_manifest).parameters[
            "installer_materials_sha256"
        ]
        self.assertIs(parameter.default, inspect.Parameter.empty)
        with self.assertRaises(ReleaseContractError):
            manifest(installer_materials_sha256="sha256:" + "0" * 64)

    def test_deployment_contract_is_canonical_complete_and_bound_to_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "deploy").mkdir()
            (root / "updater").mkdir()
            (root / "deploy" / "docker-compose.yml").write_text(
                "services: {}\n", encoding="utf-8"
            )
            (root / "updater" / "docker-compose.runtime.yml").write_text(
                "services: {}\n", encoding="utf-8"
            )
            materials = root / "installer-materials.tar"
            write_material_archive(materials)

            contract = build_deployment_contract(
                root, installer_materials=materials
            )
            validate_deployment_contract(
                contract, root=root, installer_materials=materials
            )

        self.assertEqual(
            [item["path"] for item in contract["files"]],
            ["deploy/docker-compose.yml", "updater/docker-compose.runtime.yml"],
        )
        self.assertRegex(deployment_contract_digest(contract), r"^sha256:[0-9a-f]{64}$")

    def test_deployment_contract_rejects_missing_unordered_or_tampered_files(self):
        missing = copy.deepcopy(DEPLOYMENT_CONTRACT)
        missing["files"] = DEPLOYMENT_FILES[:1]
        with self.assertRaisesRegex(ReleaseContractError, "incomplete or unordered"):
            validate_deployment_contract(missing)

        unordered = copy.deepcopy(DEPLOYMENT_CONTRACT)
        unordered["files"] = list(reversed(DEPLOYMENT_FILES))
        with self.assertRaisesRegex(ReleaseContractError, "incomplete or unordered"):
            validate_deployment_contract(unordered)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "deploy").mkdir()
            (root / "updater").mkdir()
            (root / "deploy" / "docker-compose.yml").write_text("original\n", encoding="utf-8")
            (root / "updater" / "docker-compose.runtime.yml").write_text("overlay\n", encoding="utf-8")
            materials = root / "installer-materials.tar"
            write_material_archive(materials)
            contract = build_deployment_contract(
                root, installer_materials=materials
            )
            (root / "deploy" / "docker-compose.yml").write_text("tampered\n", encoding="utf-8")
            with self.assertRaisesRegex(ReleaseContractError, "checksum differs"):
                validate_deployment_contract(contract, root=root)

    def test_v1_release_policy_declares_the_production_baseline_migrations(self):
        policy = json.loads((Path(__file__).parents[2] / "release" / "compatibility.json").read_text(encoding="utf-8"))
        database = policy["database"]

        self.assertEqual(
            database["migration"],
            {"required": True, "policy": "additive-backward-compatible"},
        )
        self.assertEqual(database["applicationRollback"], "conditional")

    def test_valid_manifest_round_trips_through_versioned_schema(self):
        payload = manifest()
        validate_manifest(payload, updater_version="1.0.0")
        self.assertEqual(payload["schemaVersion"], 2)
        self.assertEqual(payload["images"]["api"]["digest"], API_DIGEST)
        self.assertEqual(
            payload["images"]["postgres"],
            {
                "repository": POSTGRES_REPOSITORY,
                "digest": POSTGRES_DIGEST,
                "platform": "linux/amd64",
            },
        )
        self.assertEqual(
            payload["images"]["redis"],
            {
                "repository": REDIS_REPOSITORY,
                "digest": REDIS_DIGEST,
                "platform": "linux/amd64",
            },
        )
        self.assertEqual(payload["deployment"]["profile"], "v1.1-standard")
        self.assertEqual(
            payload["artifacts"],
            {
                "manifest": "release-manifest.json",
                "deploymentContract": "deployment-contract.json",
                "installerMaterials": "installer-materials.tar",
                "checksums": "checksums.txt",
            },
        )
        self.assertEqual(payload["compatibility"]["pluginSdk"]["supportedApis"], [2])
        self.assertEqual(payload["provenance"]["sourceCommit"], COMMIT)
        self.assertNotIn("tag", payload["images"]["api"])

    def test_manifest_rejects_missing_or_mutable_image_identity(self):
        missing = manifest()
        del missing["images"]["api"]["digest"]
        with self.assertRaises(ReleaseContractError):
            validate_manifest(missing)

        mutable = manifest()
        mutable["images"]["api"]["tag"] = "latest"
        with self.assertRaises(ReleaseContractError):
            validate_manifest(mutable)

    def test_manifest_rejects_unbound_deployment_digest(self):
        payload = manifest()
        payload["deployment"]["contractSha256"] = "mutable"
        with self.assertRaisesRegex(ReleaseContractError, "contractSha256"):
            validate_manifest(payload)

    def test_manifest_rejects_release_notes_and_provenance_mismatch(self):
        wrong_notes = manifest()
        wrong_notes["releaseNotes"]["tag"] = "v1.0.0-rc.2"
        with self.assertRaisesRegex(ReleaseContractError, "Release notes tag"):
            validate_manifest(wrong_notes)

        wrong_workflow = manifest()
        wrong_workflow["provenance"]["workflow"] = ".github/workflows/promote-release.yml"
        with self.assertRaisesRegex(ReleaseContractError, "Prerelease provenance"):
            validate_manifest(wrong_workflow)

        wrong_commit = manifest()
        wrong_commit["provenance"]["sourceCommit"] = "f" * 40
        with self.assertRaisesRegex(ReleaseContractError, "Prerelease provenance"):
            validate_manifest(wrong_commit)

    def test_manifest_rejects_invalid_commit_schema_and_compatibility(self):
        invalid_commit = manifest()
        invalid_commit["release"]["commit"] = "abc123"
        with self.assertRaises(ReleaseContractError):
            validate_manifest(invalid_commit)

        unsupported = manifest()
        unsupported["schemaVersion"] = 1
        with self.assertRaisesRegex(ReleaseContractError, "schemaVersion"):
            validate_manifest(unsupported)

        incompatible = manifest()
        incompatible["compatibility"]["database"]["migration"] = {
            "required": True,
            "policy": "none",
        }
        with self.assertRaisesRegex(ReleaseContractError, "migration"):
            validate_manifest(incompatible)

    def test_manifest_rejects_updater_older_than_declared_minimum(self):
        with self.assertRaisesRegex(ReleaseContractError, "updater"):
            validate_manifest(manifest(minimum_updater_version="1.2.0"), updater_version="1.1.9")

    def test_stable_manifest_can_only_promote_exact_rc_artifacts(self):
        rc = manifest()
        promotion_commit = "b" * 40
        stable = promote_manifest(
            rc,
            existing_tags=["v0.9.0"],
            provenance_source_commit=promotion_commit,
        )
        validate_manifest(stable)
        self.assertEqual(stable["release"]["version"], "v1.0.0")
        self.assertEqual(stable["release"]["promotedFrom"], "v1.0.0-rc.1")
        self.assertEqual(stable["release"]["commit"], rc["release"]["commit"])
        self.assertEqual(stable["images"], rc["images"])
        self.assertEqual(stable["deployment"], rc["deployment"])
        self.assertEqual(
            stable["artifacts"]["deploymentContract"],
            rc["artifacts"]["deploymentContract"],
        )
        self.assertEqual(stable["provenance"]["sourceCommit"], promotion_commit)

        wrong_stable_workflow = copy.deepcopy(stable)
        wrong_stable_workflow["provenance"]["workflow"] = ".github/workflows/release.yml"
        with self.assertRaisesRegex(ReleaseContractError, "Stable provenance"):
            validate_manifest(wrong_stable_workflow)

        with self.assertRaisesRegex(ReleaseContractError, "already exists"):
            promote_manifest(rc, existing_tags=["v1.0.0"], provenance_source_commit=promotion_commit)

        beta = manifest(version="v1.0.0-beta.1", channel="beta")
        with self.assertRaisesRegex(ReleaseContractError, "RC"):
            promote_manifest(beta, existing_tags=[], provenance_source_commit=promotion_commit)

    def test_manifest_file_is_canonical_utf8_json(self):
        payload = manifest()
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "release-manifest.json"
            target.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            loaded = json.loads(target.read_text(encoding="utf-8"))
        self.assertEqual(loaded, payload)


class ProvenancePlanTests(unittest.TestCase):
    def test_dry_run_plan_binds_both_images_to_commit_and_workflow(self):
        plan = build_provenance_plan(
            version="v1.0.0-rc.1",
            commit=COMMIT,
            api_digest=API_DIGEST,
            web_digest=WEB_DIGEST,
            created_at="2026-08-12T10:00:00Z",
        )
        self.assertEqual(plan["predicateType"], "https://slsa.dev/provenance/v1")
        self.assertEqual(plan["predicate"]["buildDefinition"]["resolvedDependencies"][0]["digest"]["gitCommit"], COMMIT)
        self.assertEqual(
            plan["subject"],
            [
                {"name": "ghcr.io/yanyuhanyue/animemo-api", "digest": {"sha256": "1" * 64}},
                {"name": "ghcr.io/yanyuhanyue/animemo-web", "digest": {"sha256": "2" * 64}},
            ],
        )

    def test_dry_run_plan_rejects_mutable_or_malformed_identity(self):
        with self.assertRaises(ReleaseContractError):
            build_provenance_plan(
                version="latest",
                commit=COMMIT,
                api_digest=API_DIGEST,
                web_digest=WEB_DIGEST,
                created_at="2026-08-12T10:00:00Z",
            )


if __name__ == "__main__":
    unittest.main()
