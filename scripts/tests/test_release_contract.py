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
    validate_publication_reservations,
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
    "profile": "v1.1-instance-scoped",
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


def publication_reservation(
    *,
    release_tag: str = "v1.1.0-rc.1",
    candidate_sha: str = "a" * 40,
) -> dict[str, object]:
    sha_tag = f"sha-{candidate_sha}"
    return {
        "releaseTag": release_tag,
        "status": "ABORTED_PARTIAL_GHCR_TRANSACTION",
        "reusable": False,
        "candidateSha": candidate_sha,
        "candidateTreeSha": "b" * 40,
        "qualificationRunId": 1,
        "publishRunId": 2,
        "api": {
            "repository": "ghcr.io/yanyuhanyue/animemo-api",
            "digest": "sha256:" + "1" * 64,
            "tags": [release_tag, sha_tag],
            "attestationVerified": True,
        },
        "web": {
            "repository": "ghcr.io/yanyuhanyue/animemo-web",
            "digest": "sha256:" + "2" * 64,
            "tags": [release_tag, sha_tag],
            "attestationVerified": True,
        },
        "gitTagCreated": False,
        "githubReleaseCreated": False,
        "releaseAssetCount": 0,
    }


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

    def test_resolver_unions_actual_tags_and_non_reusable_reservations(self):
        empty = {"schemaVersion": 1, "reservations": []}
        self.assertEqual(
            resolve_prerelease(
                tags=["v1.0.0"],
                bump="minor",
                channel="rc",
                publication_reservations=empty,
            )["releaseTag"],
            "v1.1.0-rc.1",
        )
        rc1 = {
            "schemaVersion": 1,
            "reservations": [publication_reservation()],
        }
        plan = resolve_prerelease(
            tags=["v1.0.0"],
            bump="minor",
            channel="rc",
            publication_reservations=rc1,
        )
        self.assertEqual(
            plan,
            {"targetVersion": "v1.1.0", "releaseTag": "v1.1.0-rc.2", "sequence": 2},
        )
        deduplicated = resolve_prerelease(
            tags=["v1.0.0", "v1.1.0-rc.1"],
            bump="minor",
            channel="rc",
            publication_reservations=rc1,
        )
        self.assertEqual(deduplicated["sequence"], 2)
        self.assertEqual(
            resolve_prerelease(
                tags=["v1.0.0"],
                bump="minor",
                channel="beta",
                publication_reservations=rc1,
            )["sequence"],
            1,
        )

        beta1 = {
            "schemaVersion": 1,
            "reservations": [
                publication_reservation(release_tag="v1.1.0-beta.1")
            ],
        }
        self.assertEqual(
            resolve_prerelease(
                tags=["v1.0.0"],
                bump="minor",
                channel="rc",
                publication_reservations=beta1,
            )["sequence"],
            1,
        )
        other_target = {
            "schemaVersion": 1,
            "reservations": [
                publication_reservation(release_tag="v2.0.0-rc.1")
            ],
        }
        self.assertEqual(
            resolve_prerelease(
                tags=["v1.0.0"],
                bump="minor",
                channel="rc",
                publication_reservations=other_target,
            )["sequence"],
            1,
        )

    def test_current_incident_reservation_is_valid_and_stable_baseline_is_actual(self):
        root = Path(__file__).parents[2]
        payload = json.loads(
            (root / "release" / "publication-reservations.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertIs(validate_publication_reservations(payload), payload)
        self.assertEqual(payload["schemaVersion"], 1)
        self.assertEqual(len(payload["reservations"]), 5)
        self.assertEqual(
            [item["releaseTag"] for item in payload["reservations"]],
            [
                "v1.1.0-rc.1",
                "v1.1.0-rc.2",
                "v1.1.0-rc.3",
                "v1.1.0-rc.4",
                "v1.1.0-rc.5",
            ],
        )
        self.assertTrue(
            all(
                item["status"] == "ABORTED_PARTIAL_GHCR_TRANSACTION"
                and item["reusable"] is False
                and item["gitTagCreated"] is False
                and item["githubReleaseCreated"] is False
                and item["releaseAssetCount"] == 0
                for item in payload["reservations"][:3]
            )
        )
        partial_release = payload["reservations"][3]
        self.assertEqual(
            (
                partial_release["status"],
                partial_release["reusable"],
                partial_release["gitTagCreated"],
                partial_release["githubReleaseCreated"],
                partial_release["releaseAssetCount"],
            ),
            ("ABORTED_PARTIAL_RELEASE_TRANSACTION", False, True, True, 5),
        )
        partial_draft = payload["reservations"][4]
        self.assertEqual(
            (
                partial_draft["status"],
                partial_draft["reusable"],
                partial_draft["gitTagCreated"],
                partial_draft["githubReleaseCreated"],
                partial_draft["releaseAssetCount"],
            ),
            (
                "ABORTED_PARTIAL_DRAFT_RELEASE_TRANSACTION",
                False,
                True,
                True,
                0,
            ),
        )
        identities = {
            item["releaseTag"]: {
                "candidateSha": item["candidateSha"],
                "candidateTreeSha": item["candidateTreeSha"],
                "qualificationRunId": item["qualificationRunId"],
                "publishRunId": item["publishRunId"],
                "apiDigest": item["api"]["digest"],
                "webDigest": item["web"]["digest"],
                "apiTags": item["api"]["tags"],
                "webTags": item["web"]["tags"],
            }
            for item in payload["reservations"]
        }
        self.assertEqual(
            identities,
            {
                "v1.1.0-rc.1": {
                    "candidateSha": "ebe1763271a0e367ac2ce1c64d1b94beef36aaf7",
                    "candidateTreeSha": "fe4e718667d5799d71ea21a52749863da971a501",
                    "qualificationRunId": 32554141297,
                    "publishRunId": 32555774478,
                    "apiDigest": "sha256:3331277a905902388afe430b92370f55b6d0425c663a2ae7470b6d678e579a5a",
                    "webDigest": "sha256:86b99658c1ea71c0a407ef4cddbd5349d8c159195ebedcb3055d9c3c34d5824a",
                    "apiTags": [
                        "v1.1.0-rc.1",
                        "sha-ebe1763271a0e367ac2ce1c64d1b94beef36aaf7",
                    ],
                    "webTags": [
                        "v1.1.0-rc.1",
                        "sha-ebe1763271a0e367ac2ce1c64d1b94beef36aaf7",
                    ],
                },
                "v1.1.0-rc.2": {
                    "candidateSha": "548cb47fac2d8c9e8e9b457084486df4cb8e5d26",
                    "candidateTreeSha": "0fd798d2835f8b5d6ce275db1c8990823c52b9b0",
                    "qualificationRunId": 32561512396,
                    "publishRunId": 32563131412,
                    "apiDigest": "sha256:1d0b3a521c341a4ea4fb7cf0887b35fbdddffc202caaa7cb7eaac877befec765",
                    "webDigest": "sha256:6695edab18801f02ed64700af1a361af6abd65fa11fc35a5cf92281258b164cc",
                    "apiTags": [
                        "v1.1.0-rc.2",
                        "sha-548cb47fac2d8c9e8e9b457084486df4cb8e5d26",
                    ],
                    "webTags": [
                        "v1.1.0-rc.2",
                        "sha-548cb47fac2d8c9e8e9b457084486df4cb8e5d26",
                    ],
                },
                "v1.1.0-rc.3": {
                    "candidateSha": "33eae910d38494e358d8d6ab0f196ab2c6399a6e",
                    "candidateTreeSha": "13fe7a99d314e56af33eea2362fd52a50514cc0a",
                    "qualificationRunId": 32572123477,
                    "publishRunId": 32574129439,
                    "apiDigest": "sha256:1b7c3e0bcac28628f25c6419961e5ec37aaaac7eb0ff6d26c7bc8b70c6ccea61",
                    "webDigest": "sha256:cad63b93e3c88ad6430101342679c76c25767a229ed704f989dd1b251cf53dd8",
                    "apiTags": [
                        "v1.1.0-rc.3",
                        "sha-33eae910d38494e358d8d6ab0f196ab2c6399a6e",
                    ],
                    "webTags": [
                        "v1.1.0-rc.3",
                        "sha-33eae910d38494e358d8d6ab0f196ab2c6399a6e",
                    ],
                },
                "v1.1.0-rc.4": {
                    "candidateSha": "48f23bd51e7c68970fe54d309a5f15c989dc5b8d",
                    "candidateTreeSha": "190e845a39780f926179038dc1ceadd525e6d8d9",
                    "qualificationRunId": 32582830554,
                    "publishRunId": 32584743079,
                    "apiDigest": "sha256:5787ddcd248d883827811a30c0b417c44b45ca03439e16fdd178d3edffc7a652",
                    "webDigest": "sha256:21a170dc1485051e67bfa53981d6a8f09703f1bcab326311a3acb2ff377fe483",
                    "apiTags": [
                        "v1.1.0-rc.4",
                        "sha-48f23bd51e7c68970fe54d309a5f15c989dc5b8d",
                    ],
                    "webTags": [
                        "v1.1.0-rc.4",
                        "sha-48f23bd51e7c68970fe54d309a5f15c989dc5b8d",
                    ],
                },
                "v1.1.0-rc.5": {
                    "candidateSha": "db34a3fb0e44ff3143cce8c196e6f7d0ec6baa71",
                    "candidateTreeSha": "7828eed68a83dd309c5b39ffa024c8df4a56782c",
                    "qualificationRunId": 32586531664,
                    "publishRunId": 32588261236,
                    "apiDigest": "sha256:38c813bebd9d438f3b1cdcdf5893ed2a0db2b266c89785f43f063c9146fdca2c",
                    "webDigest": "sha256:e38136993d85582ffb6c2755724000ff568fd312b7959c193772c8ed551590e9",
                    "apiTags": [
                        "v1.1.0-rc.5",
                        "sha-db34a3fb0e44ff3143cce8c196e6f7d0ec6baa71",
                    ],
                    "webTags": [
                        "v1.1.0-rc.5",
                        "sha-db34a3fb0e44ff3143cce8c196e6f7d0ec6baa71",
                    ],
                },
            },
        )
        plan = resolve_prerelease(
            tags=["v1.0.0"],
            bump="minor",
            channel="rc",
            publication_reservations=payload,
        )
        self.assertEqual(
            plan,
            {"targetVersion": "v1.1.0", "releaseTag": "v1.1.0-rc.6", "sequence": 6},
        )
        self.assertEqual(previous_stable_tag(["v1.0.0"], target="v1.1.0"), "v1.0.0")

    def test_publication_reservation_validation_fails_closed(self):
        valid = {
            "schemaVersion": 1,
            "reservations": [publication_reservation()],
        }

        def mutation(path, value):
            payload = copy.deepcopy(valid)
            target = payload
            for key in path[:-1]:
                target = target[key]
            target[path[-1]] = value
            return payload

        invalid = {
            "unknown top-level field": {**copy.deepcopy(valid), "extra": True},
            "unknown reservation field": mutation(
                ["reservations", 0, "extra"], True
            ),
            "stable releaseTag": mutation(
                ["reservations", 0, "releaseTag"], "v1.1.0"
            ),
            "reusable reservation": mutation(
                ["reservations", 0, "reusable"], True
            ),
            "unknown status": mutation(
                ["reservations", 0, "status"], "UNKNOWN"
            ),
            "malformed candidate SHA": mutation(
                ["reservations", 0, "candidateSha"], "A" * 40
            ),
            "malformed tree SHA": mutation(
                ["reservations", 0, "candidateTreeSha"], "bad"
            ),
            "malformed digest": mutation(
                ["reservations", 0, "api", "digest"], "sha256:bad"
            ),
            "mismatched sha tag": mutation(
                ["reservations", 0, "api", "tags"],
                ["v1.1.0-rc.1", "sha-" + "c" * 40],
            ),
            "wrong repository": mutation(
                ["reservations", 0, "web", "repository"],
                "ghcr.io/yanyuhanyue/other",
            ),
        }
        duplicate = copy.deepcopy(valid)
        duplicate["reservations"].append(copy.deepcopy(duplicate["reservations"][0]))
        invalid["duplicate releaseTag"] = duplicate
        for label, payload in invalid.items():
            with self.subTest(label=label), self.assertRaises(ReleaseContractError):
                validate_publication_reservations(payload)

    def test_partial_release_reservation_requires_tag_release_and_assets(self):
        reservation = publication_reservation()
        reservation.update(
            {
                "status": "ABORTED_PARTIAL_RELEASE_TRANSACTION",
                "gitTagCreated": True,
                "githubReleaseCreated": True,
                "releaseAssetCount": 5,
            }
        )
        valid = {"schemaVersion": 1, "reservations": [reservation]}
        self.assertIs(validate_publication_reservations(valid), valid)
        for field, value in (
            ("gitTagCreated", False),
            ("githubReleaseCreated", False),
            ("releaseAssetCount", 0),
        ):
            invalid = copy.deepcopy(valid)
            invalid["reservations"][0][field] = value
            with self.subTest(field=field), self.assertRaises(ReleaseContractError):
                validate_publication_reservations(invalid)

    def test_partial_draft_release_reservation_requires_tag_release_and_zero_assets(self):
        reservation = publication_reservation()
        reservation.update(
            {
                "status": "ABORTED_PARTIAL_DRAFT_RELEASE_TRANSACTION",
                "gitTagCreated": True,
                "githubReleaseCreated": True,
                "releaseAssetCount": 0,
            }
        )
        valid = {"schemaVersion": 1, "reservations": [reservation]}
        self.assertIs(validate_publication_reservations(valid), valid)
        for field, value in (
            ("gitTagCreated", False),
            ("githubReleaseCreated", False),
            ("releaseAssetCount", 1),
        ):
            invalid = copy.deepcopy(valid)
            invalid["reservations"][0][field] = value
            with self.subTest(field=field), self.assertRaises(ReleaseContractError):
                validate_publication_reservations(invalid)


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
        self.assertEqual(
            payload["deployment"]["profile"], "v1.1-instance-scoped"
        )
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
    def test_release_contract_closes_trusted_freshness_ttl_and_local_authority(self):
        root = Path(__file__).resolve().parents[2]
        contract = (root / "docs" / "release-contract-v1.md").read_text(
            encoding="utf-8"
        )
        for term in (
            "Phase A `Qualification`",
            "Phase F `Release Metadata Freshness`",
            "Phase B `Publish`",
            "至少相隔 60 秒",
            "不得超过 15 分钟",
            "METADATA_FRESHNESS_EXPIRED",
            "NON_AUTHORITY_DIAGNOSTIC",
            "TRANSPORT_AND_QUALIFICATION_EVIDENCE",
            "GitHub Immutable Release",
        ):
            self.assertIn(term, contract)
        self.assertIn("不得回退到 Qualification 或任何本地结果", contract)

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
