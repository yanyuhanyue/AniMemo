from __future__ import annotations

import copy
import hashlib
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from durability.platform import (
    REQUIRED_CAPABILITIES,
    REQUIRED_REHEARSALS,
    canonical_platform_qualification_bytes,
    finalize_platform_qualification,
)
from release.candidate import (
    CandidateContractError,
    _build_verified_candidate_identity,
    _extract_candidate_archive,
    _verify_qualification_intrinsics,
    _verify_runtime,
    aggregate_receipt_digest,
    canonical_json_bytes,
    decode_aggregate_receipt_b64url,
    extract_candidate_oci_archive,
    load_verified_candidate,
    normalize_candidate_oci_layout,
    sha256_bytes,
    validate_aggregate_receipt,
    validate_candidate_input,
    validate_profile_receipt,
    validate_verification_execution_receipt,
    validate_verified_candidate,
    verify_prepublication_candidate,
)
from release.contract import (
    POSTGRES_DIGEST,
    POSTGRES_REPOSITORY,
    REDIS_DIGEST,
    REDIS_REPOSITORY,
)
from release.materials import MaterialContractError, extract_qualification_artifact
from scripts.release_qualification import build_qualification_evidence
from updater.oci import (
    OCI_CONFIG_MEDIA_TYPE,
    OCI_IMAGE_INDEX_MEDIA_TYPE,
    OCI_IMAGE_MANIFEST_MEDIA_TYPE,
    OCI_LAYER_MEDIA_TYPE,
    OCIContractError,
)

SHA = "a" * 40
TREE = "b" * 40
DIGEST = "sha256:" + "c" * 64
RUN_ID = 1234


def _digest(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _blob(root: Path, value: bytes) -> tuple[str, int]:
    identity = _digest(value)
    target = root / "blobs" / "sha256" / identity[7:]
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(value)
    return identity, len(value)


def _layout(root: Path, role: str) -> str:
    root.mkdir(parents=True)
    (root / "oci-layout").write_bytes(
        canonical_json_bytes({"imageLayoutVersion": "1.0.0"})
    )
    layer = ("layer:" + role).encode()
    layer_digest, layer_size = _blob(root, layer)
    config_digest, config_size = _blob(
        root,
        canonical_json_bytes(
            {
                "architecture": "amd64",
                "os": "linux",
                "rootfs": {"diff_ids": [_digest(layer)], "type": "layers"},
            }
        ),
    )
    manifest_digest, manifest_size = _blob(
        root,
        canonical_json_bytes(
            {
                "config": {
                    "digest": config_digest,
                    "mediaType": OCI_CONFIG_MEDIA_TYPE,
                    "size": config_size,
                },
                "layers": [
                    {
                        "digest": layer_digest,
                        "mediaType": OCI_LAYER_MEDIA_TYPE,
                        "size": layer_size,
                    }
                ],
                "mediaType": OCI_IMAGE_MANIFEST_MEDIA_TYPE,
                "schemaVersion": 2,
            }
        ),
    )
    (root / "index.json").write_bytes(
        canonical_json_bytes(
            {
                "manifests": [
                    {
                        "digest": manifest_digest,
                        "mediaType": OCI_IMAGE_MANIFEST_MEDIA_TYPE,
                        "platform": {"architecture": "amd64", "os": "linux"},
                        "size": manifest_size,
                    }
                ],
                "mediaType": OCI_IMAGE_INDEX_MEDIA_TYPE,
                "schemaVersion": 2,
            }
        )
    )
    return manifest_digest


def _inventory() -> list[dict[str, object]]:
    values = []
    for role in ("api", "postgres", "redis", "web"):
        for name in ("index.json", "oci-layout", "blobs/sha256/" + "1" * 64):
            values.append(
                {
                    "path": f"candidate-runtime/oci/{role}/{name}",
                    "sha256": DIGEST,
                    "size": 1,
                }
            )
    return sorted(values, key=lambda item: str(item["path"]))


def candidate_input() -> dict[str, object]:
    return {
        "schema": "animemo.prepublication-candidate-input/v1",
        "version": 1,
        "purpose": "PREPUBLICATION_CANDIDATE_ACCEPTANCE_ONLY",
        "repository": "yanyuhanyue/AniMemo",
        "qualification_run_id": RUN_ID,
        "qualification_run_attempt": 1,
        "qualification_workflow_identity": {
            "name": "Release Producer",
            "path": ".github/workflows/release.yml",
            "ref": "yanyuhanyue/AniMemo/.github/workflows/release.yml@refs/heads/main",
            "sha": SHA,
        },
        "qualification_artifact_ids": {
            "platform_qualification": 10,
            "release_dry_run": 11,
        },
        "qualification_artifact_api_digests": {
            "platform_qualification": "sha256:" + "1" * 64,
            "release_dry_run": "sha256:" + "2" * 64,
        },
        "source_sha": SHA,
        "source_tree": TREE,
        "target_version": "v1.1.0",
        "candidate_version": "v1.1.0-rc.14",
        "candidate_sequence": 14,
        "release_notes_json_sha256": DIGEST,
        "release_notes_markdown_sha256": DIGEST,
        "release_manifest_sha256": DIGEST,
        "deployment_contract_sha256": DIGEST,
        "installer_materials_sha256": DIGEST,
        "checksums_sha256": DIGEST,
        "api_oci_digest": DIGEST,
        "web_oci_digest": DIGEST,
        "postgres_oci_digest": DIGEST,
        "redis_oci_digest": DIGEST,
        "candidate_runtime_file_inventory": _inventory(),
        "release_authority_granted": False,
        "production_authorized": False,
        "publish_authorized": False,
        "generated_at": "2026-08-25T12:00:00Z",
    }


def verified_candidate_identity() -> dict[str, object]:
    candidate = candidate_input()
    repositories = {
        "api": "ghcr.io/yanyuhanyue/animemo-api",
        "postgres": "docker.io/library/postgres",
        "redis": "docker.io/library/redis",
        "web": "ghcr.io/yanyuhanyue/animemo-web",
    }
    runtime = SimpleNamespace(
        images=tuple(
            SimpleNamespace(
                role=role,
                repository=repositories[role],
                digest=DIGEST,
                platform="linux/amd64",
                config_digest=DIGEST,
                layer_digests=(DIGEST,),
            )
            for role in ("api", "postgres", "redis", "web")
        )
    )
    return _build_verified_candidate_identity(
        candidate=candidate,
        candidate_digest=sha256_bytes(canonical_json_bytes(candidate)),
        containing_artifact_id=99,
        containing_artifact_api_digest=DIGEST,
        archive_digest=DIGEST,
        archive_file_count=22,
        runtime=runtime,
    )


def verification_execution_receipt() -> dict[str, object]:
    identity = verified_candidate_identity()
    value = {
        "schema": (
            "animemo.prepublication-candidate-verification-execution-receipt/v1"
        ),
        "version": 1,
        "purpose": "VERIFICATION_EXECUTION_AUDIT_ONLY",
        "candidate_input_sha256": identity["candidate_input_sha256"],
        "verified_candidate_digest": sha256_bytes(canonical_json_bytes(identity)),
        "repository": identity["repository"],
        "qualification_run_id": identity["qualification_run_id"],
        "qualification_run_attempt": identity["qualification_run_attempt"],
        "source_sha": identity["source_sha"],
        "source_tree": identity["source_tree"],
        "candidate_version": identity["candidate_version"],
        "verifier_contract_version": "2",
        "verified_at": "2026-08-25T04:00:00.000000Z",
        "check_counts": {
            "qualification_artifact_count": 2,
            "containing_artifact_count": 1,
            "oci_image_count": 4,
            "oci_layer_count": 4,
            "runtime_file_count": 12,
            "archive_file_count": 22,
        },
        "environment_classification": "SANITIZED_LOCAL_VERIFIER",
        "result": "PASS",
        "error_code": None,
        "identity_authority_granted": False,
        "release_authority_granted": False,
        "production_authorized": False,
        "publish_authorized": False,
        "receipt_digest": "",
    }
    unsigned = dict(value)
    unsigned.pop("receipt_digest")
    value["receipt_digest"] = sha256_bytes(canonical_json_bytes(unsigned))
    return value


def _write_intrinsic_evidence(root: Path, candidate: dict[str, object]) -> Path:
    workflow = candidate["qualification_workflow_identity"]
    assert isinstance(workflow, dict)
    needs = {
        name: {"result": "success"}
        for name in (
            "preflight",
            "full-ci",
            "full-release-gate",
            "performance",
            "dry-run",
            "release-authority",
        )
    }
    qualification = build_qualification_evidence(
        workflow_ref=str(workflow["ref"]),
        workflow_sha=str(candidate["source_sha"]),
        run_id=str(candidate["qualification_run_id"]),
        run_attempt=1,
        candidate_sha=str(candidate["source_sha"]),
        upgrade_base_sha="d" * 40,
        channel="rc",
        target_version=str(candidate["target_version"]),
        release_tag=str(candidate["candidate_version"]),
        needs=needs,
        created_at="2026-08-25T12:00:00Z",
        release_notes_identity=DIGEST,
        release_notes_markdown_sha256=str(
            candidate["release_notes_markdown_sha256"]
        ),
    )
    qualification_path = root / (
        f"release-qualification-{candidate['qualification_run_id']}.json"
    )
    qualification_path.write_bytes(
        (
            json.dumps(
                qualification,
                ensure_ascii=False,
                allow_nan=False,
                indent=2,
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8")
    )
    (root / "release-notes.json").write_bytes(
        canonical_json_bytes({"identity": DIGEST})
    )

    platform = finalize_platform_qualification(
        {
            "schema": "animemo.platform-qualification/v1",
            "profile": "v1.1-standard-linux-amd64",
            "candidateSha": candidate["source_sha"],
            "workflow": {
                "path": ".github/workflows/release.yml",
                "ref": str(workflow["ref"]).partition("@")[2],
                "sha": candidate["source_sha"],
            },
            "run": {
                "id": str(candidate["qualification_run_id"]),
                "attempt": 1,
            },
            "observedAt": "2026-08-25T12:00:00Z",
            "host": {
                "os": "linux",
                "architecture": "amd64",
                "distributionId": "ubuntu",
                "distributionVersion": "24.04",
                "kernel": "qualified-kernel",
                "systemdVersion": "qualified-systemd",
                "dockerVersion": "qualified-docker",
                "composeVersion": "qualified-compose",
            },
            "databasePath": {
                "dumpFormat": "plain",
                "sourceServerMajor": 16,
                "pgDumpMajor": 16,
                "psqlMajor": 16,
                "targetServerMajor": 16,
            },
            "imageDigests": {
                "postgres": f"{POSTGRES_REPOSITORY}@{POSTGRES_DIGEST}",
                "redis": f"{REDIS_REPOSITORY}@{REDIS_DIGEST}",
            },
            "capabilities": {name: True for name in REQUIRED_CAPABILITIES},
            "rehearsals": {name: "PASS" for name in REQUIRED_REHEARSALS},
        }
    )
    platform_bytes = canonical_platform_qualification_bytes(platform)
    (root / "platform-qualification.json").write_bytes(platform_bytes)
    embedded = root / "installer-root" / "release" / "platform-qualification.json"
    embedded.parent.mkdir(parents=True)
    embedded.write_bytes(platform_bytes)
    return qualification_path


def aggregate_receipt() -> dict[str, object]:
    state = {
        "tag": "ABSENT",
        "github_release": "ABSENT",
        "ghcr": "ABSENT",
        "public_r2": "ABSENT_BY_PUBLIC_READBACK_NON_AUTHORITATIVE",
        "r2_origin": "PROVEN_EMPTY",
    }
    value = {
        "schema": "animemo.prepublication-candidate-acceptance-receipt/v1",
        "version": 1,
        "candidate_input_digest": "sha256:" + "1" * 64,
        "verified_candidate_digest": "sha256:" + "2" * 64,
        "qualification_run_id": RUN_ID,
        "qualification_run_attempt": 1,
        "source_sha": SHA,
        "source_tree": TREE,
        "candidate_version": "v1.1.0-rc.14",
        "r2_origin_prestate_receipt_digest": "sha256:" + "6" * 64,
        "profile_receipts": {
            "fresh_base": "sha256:" + "3" * 64,
            "docker_base": "sha256:" + "4" * 64,
            "runtime_base_offline": "sha256:" + "5" * 64,
        },
        "all_profiles_pass": True,
        "rc14_prestate": dict(state),
        "rc14_poststate": dict(state),
        "repository_mutation_count": 0,
        "publication_mutation_count": 0,
        "shared_host_connection_count": 0,
        "secret_sweep": 0,
        "placeholder_sweep": 0,
        "release_authority_granted": False,
        "publish_authorized": False,
        "completed_at": "2026-08-25T12:00:00Z",
        "result": "PASS",
        "receipt_digest": "",
    }
    unsigned = dict(value)
    unsigned.pop("receipt_digest")
    value["receipt_digest"] = sha256_bytes(canonical_json_bytes(unsigned))
    return value


class CandidateSchemaTests(unittest.TestCase):
    def test_post_repair_loader_rejects_v1_and_receipt_substitution(self):
        identity = verified_candidate_identity()
        legacy = copy.deepcopy(identity)
        legacy.update(
            {
                "schema": "animemo.verified-prepublication-candidate/v1",
                "version": 1,
                "verified_at": "2026-08-25T12:00:00Z",
            }
        )
        substitutions = (legacy, verification_execution_receipt())
        with tempfile.TemporaryDirectory() as temporary:
            for index, value in enumerate(substitutions):
                state = Path(temporary) / str(index)
                candidate_root = state / ("a" * 64)
                candidate_root.mkdir(parents=True)
                encoded = canonical_json_bytes(value)
                (candidate_root / "verified-candidate.json").write_bytes(encoded)
                with self.subTest(schema=value["schema"]), self.assertRaises(
                    CandidateContractError
                ):
                    load_verified_candidate(sha256_bytes(encoded), _state_root=state)

    def test_verified_candidate_oci_manifest_digests_are_cross_bound(self):
        identity = verified_candidate_identity()
        identity["api_oci_digest"] = "sha256:" + "d" * 64
        with self.assertRaisesRegex(
            CandidateContractError, "VERIFIED_CANDIDATE_IDENTITY_BINDING_INVALID"
        ):
            validate_verified_candidate(identity)

    def test_immutable_input_changes_cannot_reuse_an_identity_digest(self):
        baseline = verified_candidate_identity()
        baseline_digest = sha256_bytes(canonical_json_bytes(baseline))
        mutations = {
            "artifact_id": lambda item: item["qualification_artifact_ids"].update(
                {"platform_qualification": 12}
            ),
            "artifact_api_digest": lambda item: item[
                "qualification_artifact_api_digests"
            ].update({"platform_qualification": "sha256:" + "d" * 64}),
            "candidate_input_digest": lambda item: item.update(
                {"candidate_input_sha256": "sha256:" + "d" * 64}
            ),
            "oci_digest": lambda item: (
                item.update({"api_oci_digest": "sha256:" + "d" * 64}),
                item["oci_verification"][0].update(
                    {"digest": "sha256:" + "d" * 64}
                ),
            ),
            "source_sha": lambda item: (
                item.update({"source_sha": "d" * 40}),
                item["qualification_workflow_identity"].update({"sha": "d" * 40}),
            ),
            "source_tree": lambda item: item.update({"source_tree": "d" * 40}),
            "qualification_run_id": lambda item: item.update(
                {"qualification_run_id": RUN_ID + 1}
            ),
            "candidate_version": lambda item: item.update(
                {"candidate_version": "v1.1.0-rc.15", "candidate_sequence": 15}
            ),
        }
        for name, mutate in mutations.items():
            changed = copy.deepcopy(baseline)
            mutate(changed)
            with self.subTest(name=name):
                validate_verified_candidate(changed)
                self.assertNotEqual(
                    sha256_bytes(canonical_json_bytes(changed)), baseline_digest
                )

        invalid_attempt = copy.deepcopy(baseline)
        invalid_attempt["qualification_run_attempt"] = 2
        with self.assertRaises(CandidateContractError):
            validate_verified_candidate(invalid_attempt)

    def test_identity_builder_is_stable_across_hashseed_locale_timezone_and_cwd(self):
        repository = Path(__file__).resolve().parents[1]
        script = r'''
import locale
import json
import os
import sys
import time
from types import SimpleNamespace
from release.candidate import _build_verified_candidate_identity, canonical_json_bytes, sha256_bytes
from release.test_candidate import DIGEST, candidate_input

active_locale = locale.setlocale(
    locale.LC_ALL, os.environ["ANIMEMO_TEST_LOCALE"]
)
if hasattr(time, "tzset"):
    time.tzset()
print(json.dumps({
    "hashSeed": os.environ["PYTHONHASHSEED"],
    "locale": active_locale,
    "timezone": os.environ["TZ"],
}, sort_keys=True), file=sys.stderr)
candidate = candidate_input()
repositories = {
    "api": "ghcr.io/yanyuhanyue/animemo-api",
    "postgres": "docker.io/library/postgres",
    "redis": "docker.io/library/redis",
    "web": "ghcr.io/yanyuhanyue/animemo-web",
}
runtime = SimpleNamespace(images=tuple(
    SimpleNamespace(
        role=role,
        repository=repositories[role],
        digest=DIGEST,
        platform="linux/amd64",
        config_digest=DIGEST,
        layer_digests=(DIGEST,),
    )
    for role in {"web", "api", "redis", "postgres"}
))
identity = _build_verified_candidate_identity(
    candidate=candidate,
    candidate_digest=sha256_bytes(canonical_json_bytes(candidate)),
    containing_artifact_id=99,
    containing_artifact_api_digest=DIGEST,
    archive_digest=DIGEST,
    archive_file_count=22,
    runtime=runtime,
)
sys.stdout.buffer.write(canonical_json_bytes(identity))
'''
        environments = (
            ("0", "UTC", "C"),
            ("1", "Asia/Shanghai", ""),
            ("random", "UTC", "C"),
        )
        outputs = []
        observations = []
        with tempfile.TemporaryDirectory() as temporary:
            temporary_root = Path(temporary)
            for index, (hash_seed, timezone_name, locale_name) in enumerate(
                environments
            ):
                cwd = temporary_root / str(index)
                cwd.mkdir()
                environment = os.environ.copy()
                environment.update(
                    {
                        "ANIMEMO_TEST_LOCALE": locale_name,
                        "PYTHONHASHSEED": hash_seed,
                        "PYTHONPATH": str(repository),
                        "TZ": timezone_name,
                    }
                )
                completed = subprocess.run(
                    [sys.executable, "-B", "-c", script],
                    cwd=cwd,
                    env=environment,
                    check=True,
                    capture_output=True,
                )
                outputs.append(completed.stdout)
                observations.append(json.loads(completed.stderr))
        self.assertEqual(len(set(outputs)), 1)
        self.assertEqual(
            {item["hashSeed"] for item in observations}, {"0", "1", "random"}
        )
        self.assertEqual(
            {item["timezone"] for item in observations}, {"UTC", "Asia/Shanghai"}
        )
        self.assertGreaterEqual(
            len({item["locale"] for item in observations}), 2
        )

    def test_execution_receipt_is_non_authoritative_and_self_bound(self):
        receipt = verification_execution_receipt()
        self.assertEqual(validate_verification_execution_receipt(receipt), receipt)
        for field, value in (
            ("verified_candidate_digest", None),
            ("identity_authority_granted", True),
            ("release_authority_granted", True),
            ("production_authorized", True),
            ("publish_authorized", True),
        ):
            invalid = copy.deepcopy(receipt)
            if value is None:
                invalid.pop(field)
            else:
                invalid[field] = value
            with self.subTest(field=field), self.assertRaises(CandidateContractError):
                validate_verification_execution_receipt(invalid)

        tampered = copy.deepcopy(receipt)
        tampered["verified_candidate_digest"] = "sha256:" + "d" * 64
        with self.assertRaisesRegex(
            CandidateContractError,
            "VERIFICATION_EXECUTION_RECEIPT_DIGEST_MISMATCH",
        ):
            validate_verification_execution_receipt(tampered)

    def test_verified_candidate_v2_excludes_execution_metadata(self):
        identity = verified_candidate_identity()
        self.assertEqual(validate_verified_candidate(identity), identity)
        for field, value in (
            ("verified_at", "2026-08-25T12:00:00Z"),
            ("absolute_path", "/tmp/candidate"),
        ):
            invalid = copy.deepcopy(identity)
            invalid[field] = value
            with self.subTest(field=field), self.assertRaisesRegex(
                CandidateContractError, "VERIFIED_CANDIDATE_SCHEMA_INVALID"
            ):
                validate_verified_candidate(invalid)
        for field in (
            "release_authority_granted",
            "production_authorized",
            "publish_authorized",
        ):
            invalid = copy.deepcopy(identity)
            invalid[field] = True
            with self.subTest(field=field), self.assertRaises(
                CandidateContractError
            ):
                validate_verified_candidate(invalid)

    def test_candidate_input_is_closed_and_cross_schema_substitution_fails(self):
        valid = candidate_input()
        self.assertEqual(validate_candidate_input(valid)["candidate_sequence"], 14)
        for mutation in (
            lambda item: item.pop("source_tree"),
            lambda item: item.update({"unknown": True}),
            lambda item: item.update({"source_sha": ""}),
            lambda item: item.update({"api_oci_digest": "latest"}),
            lambda item: item.update({"candidate_sequence": 13}),
            lambda item: item.update({"schema": "animemo.verified-prepublication-candidate/v1"}),
        ):
            invalid = copy.deepcopy(valid)
            mutation(invalid)
            with self.assertRaises(CandidateContractError):
                validate_candidate_input(invalid)

    def test_aggregate_receipt_is_self_bound_and_canonical_base64url(self):
        receipt = aggregate_receipt()
        self.assertTrue(validate_aggregate_receipt(receipt)["all_profiles_pass"])
        encoded = canonical_json_bytes(receipt)
        import base64

        value = base64.urlsafe_b64encode(encoded).decode().rstrip("=")
        decoded, decoded_bytes = decode_aggregate_receipt_b64url(value)
        self.assertEqual(decoded, receipt)
        self.assertEqual(decoded_bytes, encoded)
        self.assertRegex(aggregate_receipt_digest(receipt), r"^sha256:[0-9a-f]{64}$")
        tampered = copy.deepcopy(receipt)
        tampered["qualification_run_id"] += 1
        with self.assertRaisesRegex(
            CandidateContractError, "CANDIDATE_ACCEPTANCE_RECEIPT_DIGEST_MISMATCH"
        ):
            validate_aggregate_receipt(tampered)

    def test_every_rc_aggregate_requires_r2_prestate_receipt_digest(self):
        for candidate_version in ("v1.1.0-rc.14", "v1.1.0-rc.15"):
            receipt = aggregate_receipt()
            receipt["candidate_version"] = candidate_version
            receipt.pop("r2_origin_prestate_receipt_digest")
            unsigned = dict(receipt)
            unsigned.pop("receipt_digest")
            receipt["receipt_digest"] = sha256_bytes(canonical_json_bytes(unsigned))
            with self.subTest(candidate_version=candidate_version), self.assertRaisesRegex(
                CandidateContractError, "CANDIDATE_ACCEPTANCE_RECEIPT_INVALID"
            ):
                validate_aggregate_receipt(receipt)

    def test_offline_profile_rejects_any_network_apt_or_pull(self):
        receipt = {
            "schema": "animemo.prepublication-candidate-profile-receipt/v1",
            "version": 1,
            "candidate_input_digest": "sha256:" + "1" * 64,
            "verified_candidate_digest": "sha256:" + "2" * 64,
            "qualification_run_id": RUN_ID,
            "qualification_run_attempt": 1,
            "source_sha": SHA,
            "source_tree": TREE,
            "candidate_version": "v1.1.0-rc.14",
            "profile": "RUNTIME_BASE_OFFLINE",
            "base_vm_identity": "sha256:" + "3" * 64,
            "snapshot_identity": "sha256:" + "4" * 64,
            "clone_identity": "sha256:" + "5" * 64,
            "initial_platform_state": {
                "docker_present": True,
                "runtime_dependencies_present": True,
                "network_allowed": False,
            },
            "platform_bootstrap_plan_digest": "sha256:" + "6" * 64,
            "platform_bootstrap_receipt_digest": "sha256:" + "7" * 64,
            "strict_platform_qualification": True,
            "instance_mutation_before_platform_qualification": 0,
            "installer_plan_digest": "sha256:" + "8" * 64,
            "installer_execution_result": "PASS",
            "api_digest": DIGEST,
            "web_digest": DIGEST,
            "postgres_digest": DIGEST,
            "redis_digest": DIGEST,
            "doctor_result": "PASS",
            "canonical_test_results": [{"name": "doctor", "result": "PASS"}],
            "network_request_count": 0,
            "apt_command_count": 0,
            "external_pull_count": 0,
            "original_vm_pre_hashes": {"base.vmx": DIGEST},
            "original_vm_post_hashes": {"base.vmx": DIGEST},
            "release_authority_granted": False,
            "publish_authorized": False,
            "started_at": "2026-08-25T12:00:00Z",
            "completed_at": "2026-08-25T12:01:00Z",
            "result": "PASS",
        }
        self.assertEqual(validate_profile_receipt(receipt)["result"], "PASS")
        receipt["original_vm_pre_hashes"] = {
            "Ubuntu 64 位-Snapshot6.vmsn": DIGEST
        }
        receipt["original_vm_post_hashes"] = dict(
            receipt["original_vm_pre_hashes"]
        )
        self.assertEqual(validate_profile_receipt(receipt)["result"], "PASS")
        receipt["network_request_count"] = 1
        with self.assertRaisesRegex(CandidateContractError, "OFFLINE_NETWORK"):
            validate_profile_receipt(receipt)


class CandidateArchiveTests(unittest.TestCase):
    def test_publish_extraction_rejects_a_legacy_qualification_without_candidate_input(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive_path = root / "legacy.zip"
            entries = {
                "release-qualification-42.json": b"qualification",
                "platform-qualification.json": b"platform",
                "release-notes.json": b"notes",
                "release-notes.md": b"notes markdown",
                "prepublication-materials.json": b"prepublication",
                "installer-materials.tar": b"materials",
                "deployment-contract.json": b"deployment",
            }
            with zipfile.ZipFile(archive_path, mode="w") as archive:
                for name, value in entries.items():
                    archive.writestr(name, value)
            with self.assertRaisesRegex(
                MaterialContractError, "Candidate Input cardinality differs"
            ):
                extract_qualification_artifact(
                    archive_path,
                    root / "extracted",
                    qualification_run_id=42,
                    expected_sha256=_digest(archive_path.read_bytes()),
                    require_candidate_contract=True,
                )

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def _archive(self, extra: tuple[str, bytes, int | None] | None = None) -> Path:
        candidate = candidate_input()
        archive_path = self.root / ("candidate-" + str(len(list(self.root.iterdir()))) + ".zip")
        roots = {
            "candidate-input.json": canonical_json_bytes(candidate),
            "checksums.txt": b"x",
            "deployment-contract.json": b"x",
            "installer-materials.tar": b"x",
            "platform-qualification.json": b"x",
            "prepublication-materials.json": b"x",
            "release-manifest.json": b"x",
            "release-notes.json": b"x",
            "release-notes.md": b"x",
            f"release-qualification-{RUN_ID}.json": b"x",
            **{item["path"]: b"x" for item in candidate["candidate_runtime_file_inventory"]},
        }
        with zipfile.ZipFile(archive_path, "w") as archive:
            for name, value in roots.items():
                archive.writestr(name, value)
            if extra is not None:
                name, value, mode = extra
                info = zipfile.ZipInfo(name)
                if mode is not None:
                    info.external_attr = mode << 16
                archive.writestr(info, value)
        return archive_path

    def test_closed_archive_extracts_without_path_substitution(self):
        destination = self.root / "out"
        candidate, _, count = _extract_candidate_archive(self._archive(), destination)
        self.assertEqual(candidate["qualification_run_id"], RUN_ID)
        self.assertEqual(count, 22)

    def test_zip_slip_absolute_drive_link_duplicate_and_case_collision_fail(self):
        attacks = (
            ("../escape", b"x", None),
            ("/absolute", b"x", None),
            ("C:/drive", b"x", None),
            ("link", b"x", stat.S_IFLNK | 0o777),
            ("CANDIDATE-INPUT.JSON", b"x", None),
        )
        for attack in attacks:
            with self.subTest(path=attack[0]), self.assertRaises(CandidateContractError):
                _extract_candidate_archive(
                    self._archive(attack), self.root / ("out-" + hashlib.sha1(attack[0].encode()).hexdigest())
                )

    def test_duplicate_path_is_rejected_before_overwrite(self):
        archive = self._archive()
        with zipfile.ZipFile(archive, "a") as output:
            output.writestr("candidate-input.json", b"different")
        with self.assertRaisesRegex(CandidateContractError, "DUPLICATE"):
            _extract_candidate_archive(archive, self.root / "duplicate")


class CandidateOciAndAuthorityTests(unittest.TestCase):
    def test_registry_oci_tar_is_safely_extracted_before_digest_verification(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            manifest = _layout(source, "postgres")
            transport = root / "postgres.oci.tar"
            with tarfile.open(transport, "w:") as archive:
                for path in sorted(item for item in source.rglob("*") if item.is_file()):
                    value = path.read_bytes()
                    member = tarfile.TarInfo(path.relative_to(source).as_posix())
                    member.size = len(value)
                    archive.addfile(member, io.BytesIO(value))
            destination = root / "oci" / "postgres"
            result = extract_candidate_oci_archive(
                archive=transport, destination=destination
            )
            self.assertEqual(result["status"], "PASS")
            normalized = normalize_candidate_oci_layout(
                source_root=root,
                layout=destination,
                role="postgres",
                repository="docker.io/library/postgres",
                expected_digest=manifest,
            )
            self.assertEqual(normalized["digest"], manifest)

            unsafe_members = (
                ("../escape", tarfile.REGTYPE, "", "ARCHIVE_PATH_INVALID"),
                ("symlink", tarfile.SYMTYPE, "index.json", "ENTRY_UNSAFE"),
                ("hardlink", tarfile.LNKTYPE, "index.json", "ENTRY_UNSAFE"),
                ("device", tarfile.CHRTYPE, "", "ENTRY_UNSAFE"),
            )
            for index, (name, kind, linkname, code) in enumerate(unsafe_members):
                unsafe = root / f"unsafe-{index}.tar"
                with tarfile.open(unsafe, "w:") as output:
                    member = tarfile.TarInfo(name)
                    member.type = kind
                    member.linkname = linkname
                    if kind == tarfile.REGTYPE:
                        member.size = 1
                        output.addfile(member, io.BytesIO(b"x"))
                    else:
                        output.addfile(member)
                with self.subTest(member=name), self.assertRaisesRegex(
                    CandidateContractError, code
                ):
                    extract_candidate_oci_archive(
                        archive=unsafe,
                        destination=root / f"unsafe-output-{index}",
                    )

    def test_four_complete_oci_layouts_pass_and_incomplete_forms_fail(self):
        repositories = {
            "api": "ghcr.io/yanyuhanyue/animemo-api",
            "postgres": "docker.io/library/postgres",
            "redis": "docker.io/library/redis",
            "web": "ghcr.io/yanyuhanyue/animemo-web",
        }

        def fixture(root: Path) -> tuple[dict[str, object], dict[str, str]]:
            digests = {
                role: _layout(root / "candidate-runtime" / "oci" / role, role)
                for role in repositories
            }
            return (
                {
                    "images": {
                        role: {
                            "digest": digests[role],
                            "platform": "linux/amd64",
                            "repository": repositories[role],
                        }
                        for role in repositories
                    }
                },
                digests,
            )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest, digests = fixture(root)
            self.assertEqual(
                tuple(image.role for image in _verify_runtime(root, manifest).images),
                ("api", "postgres", "redis", "web"),
            )

            api_manifest = json.loads(
                (
                    root
                    / "candidate-runtime"
                    / "oci"
                    / "api"
                    / "blobs"
                    / "sha256"
                    / digests["api"][7:]
                ).read_text()
            )
            api_blobs = root / "candidate-runtime" / "oci" / "api" / "blobs" / "sha256"
            config = api_blobs / api_manifest["config"]["digest"][7:]
            config.unlink()
            with self.assertRaises(CandidateContractError):
                _verify_runtime(root, manifest)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest, digests = fixture(root)
            api_manifest_path = (
                root
                / "candidate-runtime"
                / "oci"
                / "api"
                / "blobs"
                / "sha256"
                / digests["api"][7:]
            )
            api_manifest = json.loads(api_manifest_path.read_text())
            layer = api_manifest_path.parent / api_manifest["layers"][0]["digest"][7:]
            layer.unlink()
            with self.assertRaises(CandidateContractError):
                _verify_runtime(root, manifest)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest, _ = fixture(root)
            manifest["images"]["api"]["digest"] = "sha256:" + "f" * 64
            with self.assertRaises(CandidateContractError):
                _verify_runtime(root, manifest)
            manifest["images"]["api"]["repository"] = (
                "ghcr.io/yanyuhanyue/animemo-api:latest"
            )
            with self.assertRaises(CandidateContractError):
                _verify_runtime(root, manifest)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest, _ = fixture(root)
            api = root / "candidate-runtime" / "oci" / "api"
            shutil.rmtree(api)
            api.mkdir()
            (api / "api.dockerbuild").write_bytes(b"metadata-only")
            with self.assertRaises(CandidateContractError):
                _verify_runtime(root, manifest)

    def test_intrinsic_qualification_and_embedded_platform_are_exactly_bound(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            candidate = candidate_input()
            qualification_path = _write_intrinsic_evidence(root, candidate)

            _verify_qualification_intrinsics(root, candidate)

            qualification = json.loads(qualification_path.read_text(encoding="utf-8"))
            qualification["candidate_sha"] = "e" * 40
            qualification_path.write_bytes(canonical_json_bytes(qualification))
            with self.assertRaisesRegex(
                CandidateContractError, "CANDIDATE_QUALIFICATION_EVIDENCE_INVALID"
            ):
                _verify_qualification_intrinsics(root, candidate)

    def test_verifier_persists_and_reloads_by_exact_verified_digest(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            candidate = candidate_input()
            archive = root / "artifact.zip"
            deployment = {
                "schemaVersion": 2,
                "profile": "animemo-production-v2",
                "platform": "linux/amd64",
                "archive": {},
                "materials": [],
            }
            roots = {
                "candidate-input.json": canonical_json_bytes(candidate),
                "checksums.txt": b"x",
                "deployment-contract.json": canonical_json_bytes(deployment),
                "installer-materials.tar": b"x",
                "platform-qualification.json": b"{}\n",
                "prepublication-materials.json": b"{}\n",
                "release-manifest.json": b"{}\n",
                "release-notes.json": b"{}\n",
                "release-notes.md": b"x",
                f"release-qualification-{RUN_ID}.json": b"{}\n",
                **{
                    item["path"]: b"x"
                    for item in candidate["candidate_runtime_file_inventory"]
                },
            }
            with zipfile.ZipFile(archive, "w") as output:
                for name, value in roots.items():
                    output.writestr(name, value)
            archive_digest = _digest(archive.read_bytes())
            artifacts = {
                "total_count": 3,
                "artifacts": [
                    {
                        "id": 99,
                        "name": f"release-qualification-{RUN_ID}",
                        "expired": False,
                        "digest": archive_digest,
                        "archive_download_url": (
                            "https://api.github.com/repos/yanyuhanyue/AniMemo/"
                            "actions/artifacts/99/zip"
                        ),
                        "workflow_run": {"id": RUN_ID, "head_sha": SHA},
                    },
                    *[
                        {
                            "id": candidate["qualification_artifact_ids"][role],
                            "name": (
                                f"platform-qualification-{RUN_ID}"
                                if role == "platform_qualification"
                                else "release-dry-run-v1.1.0-rc.14"
                            ),
                            "expired": False,
                            "digest": candidate[
                                "qualification_artifact_api_digests"
                            ][role],
                            "workflow_run": {"id": RUN_ID, "head_sha": SHA},
                        }
                        for role in ("platform_qualification", "release_dry_run")
                    ],
                ],
            }
            run = {
                "id": RUN_ID,
                "name": "Release Producer",
                "path": ".github/workflows/release.yml",
                "event": "workflow_dispatch",
                "status": "completed",
                "conclusion": "success",
                "run_attempt": 1,
                "repository": {"full_name": "yanyuhanyue/AniMemo"},
                "head_branch": "main",
                "head_sha": SHA,
            }
            jobs = {
                "total_count": 2,
                "jobs": [
                    {
                        "name": "phase-a-qualification-evidence",
                        "status": "completed",
                        "conclusion": "success",
                    },
                    {
                        "name": "publish-immutable-prerelease",
                        "status": "completed",
                        "conclusion": "skipped",
                    },
                ],
            }
            repositories = {
                "api": "ghcr.io/yanyuhanyue/animemo-api",
                "postgres": "docker.io/library/postgres",
                "redis": "docker.io/library/redis",
                "web": "ghcr.io/yanyuhanyue/animemo-web",
            }
            images = tuple(
                SimpleNamespace(
                    role=role,
                    repository=repositories[role],
                    digest=DIGEST,
                    platform="linux/amd64",
                    config_digest=DIGEST,
                    layer_digests=(DIGEST,),
                )
                for role in ("api", "postgres", "redis", "web")
            )
            runtime = SimpleNamespace(images=images)
            def accept_candidate(value, *, root=None):
                del root
                return dict(value)

            def extract_materials(_archive, _contract, destination):
                destination.mkdir(parents=True)

            with mock.patch(
                "release.candidate.validate_candidate_input",
                side_effect=accept_candidate,
            ), mock.patch(
                "release.candidate._verify_runtime", return_value=runtime
            ), mock.patch(
                "release.candidate.extract_installer_materials",
                side_effect=extract_materials,
            ), mock.patch(
                "release.candidate._verify_qualification_intrinsics",
            ), mock.patch(
                "release.candidate.validate_material_contract",
                return_value=({"sha256": DIGEST}, ()),
            ):
                def invoke(state_root: Path, verified_at: str, mtime_ns: int):
                    os.utime(archive, ns=(mtime_ns, mtime_ns))
                    return verify_prepublication_candidate(
                        archive=archive,
                        run_metadata=run,
                        jobs_metadata=jobs,
                        artifacts_metadata=artifacts,
                        containing_artifact_id=99,
                        containing_artifact_api_digest=archive_digest,
                        expected_run_id=RUN_ID,
                        expected_source_sha=SHA,
                        expected_source_tree=TREE,
                        expected_candidate_version="v1.1.0-rc.14",
                        verified_at=verified_at,
                        _state_root=state_root,
                    )

                states = [root / f"state-{suffix}" for suffix in "abc"]
                times = (
                    "2026-08-25T12:00:00.0000000+08:00",
                    "2026-08-25T12:00:00.000000+08:00",
                    "2026-08-25T12:00:01Z",
                )
                results = [
                    invoke(state_root, verified_at, 1_700_000_000_000_000_000 + index)
                    for index, (state_root, verified_at) in enumerate(
                        zip(states, times, strict=True)
                    )
                ]
                loaded = [
                    load_verified_candidate(
                        result["verifiedCandidateDigest"], _state_root=state_root
                    )
                    for result, state_root in zip(results, states, strict=True)
                ]
                winner_state = root / "state-winner"
                winner = invoke(
                    winner_state, times[0], 1_740_000_000_000_000_000
                )
                winner_root = winner_state / winner["candidateInputDigest"][7:]
                racing_state = root / "state-race"

                def publish_competing_identity(_source, target):
                    shutil.copytree(winner_root, target)
                    raise FileExistsError("simulated concurrent publication")

                with mock.patch(
                    "release.candidate.os.replace",
                    side_effect=publish_competing_identity,
                ):
                    raced = invoke(
                        racing_state,
                        times[2],
                        1_745_000_000_000_000_000,
                    )
                self.assertTrue(raced["existing"])
                self.assertFalse(
                    raced["verificationExecutionReceiptExisting"]
                )
                self.assertTrue(
                    (
                        racing_state
                        / raced["candidateInputDigest"][7:]
                        / "verification-receipts"
                        / raced["verificationExecutionReceiptDigest"][7:]
                        / "verification-execution-receipt.json"
                    ).is_file()
                )
                original_infolist = zipfile.ZipFile.infolist

                def reversed_infolist(candidate_archive):
                    return list(reversed(original_infolist(candidate_archive)))

                with mock.patch.object(
                    zipfile.ZipFile, "infolist", reversed_infolist
                ):
                    permuted = invoke(
                        root / "state-permuted",
                        times[2],
                        1_750_000_000_000_000_000,
                    )
                original_cwd = Path.cwd()
                alternate_cwd = root / "alternate-cwd"
                alternate_cwd.mkdir()
                try:
                    os.chdir(alternate_cwd)
                    alternate_cwd_result = invoke(
                        root / "state-cwd",
                        times[2],
                        1_760_000_000_000_000_000,
                    )
                finally:
                    os.chdir(original_cwd)
                same_receipt_path = (
                    states[0]
                    / results[0]["candidateInputDigest"][7:]
                    / "verification-receipts"
                    / results[0]["verificationExecutionReceiptDigest"][7:]
                    / "verification-execution-receipt.json"
                )
                publication_window_link = (
                    same_receipt_path.parent
                    / ".verification-execution-receipt-publication-window"
                )
                os.link(same_receipt_path, publication_window_link)
                try:
                    same_run = invoke(
                        states[0], times[0], 1_800_000_000_000_000_000
                    )
                finally:
                    publication_window_link.unlink()
                later_run = invoke(states[0], times[2], 1_900_000_000_000_000_000)

                identity_bytes = [
                    (item.root / "verified-candidate.json").read_bytes()
                    for item in loaded
                ]
                receipt_paths = [
                    item.root
                    / "verification-receipts"
                    / result["verificationExecutionReceiptDigest"][7:]
                    / "verification-execution-receipt.json"
                    for item, result in zip(loaded, results, strict=True)
                ]
                receipts = [
                    validate_verification_execution_receipt(
                        json.loads(path.read_text(encoding="utf-8"))
                    )
                    for path in receipt_paths
                ]

                self.assertEqual(len(set(identity_bytes)), 1)
                self.assertEqual(
                    identity_bytes[0], canonical_json_bytes(loaded[0].verified)
                )
                for receipt_path, receipt in zip(
                    receipt_paths, receipts, strict=True
                ):
                    self.assertEqual(
                        receipt_path.read_bytes(), canonical_json_bytes(receipt)
                    )
                self.assertEqual(
                    len({result["verifiedCandidateDigest"] for result in results}),
                    1,
                )
                self.assertEqual(
                    permuted["verifiedCandidateDigest"],
                    results[0]["verifiedCandidateDigest"],
                )
                self.assertEqual(
                    alternate_cwd_result["verifiedCandidateDigest"],
                    results[0]["verifiedCandidateDigest"],
                )
                self.assertNotIn("verified_at", loaded[0].verified)
                self.assertEqual(
                    receipts[0]["verified_candidate_digest"],
                    results[0]["verifiedCandidateDigest"],
                )
                self.assertEqual(receipts[0]["verified_at"], receipts[1]["verified_at"])
                self.assertNotEqual(receipts[0]["verified_at"], receipts[2]["verified_at"])
                self.assertEqual(
                    results[0]["verificationExecutionReceiptDigest"],
                    results[1]["verificationExecutionReceiptDigest"],
                )
                self.assertNotEqual(
                    results[0]["verificationExecutionReceiptDigest"],
                    results[2]["verificationExecutionReceiptDigest"],
                )
                self.assertTrue(same_run["existing"])
                self.assertTrue(same_run["verificationExecutionReceiptExisting"])
                self.assertTrue(later_run["existing"])
                self.assertFalse(later_run["verificationExecutionReceiptExisting"])

                receipt_paths[1].write_bytes(b"{}\n")
                with self.assertRaisesRegex(
                    CandidateContractError,
                    "VERIFICATION_EXECUTION_RECEIPT_OUTPUT_CONFLICT",
                ):
                    invoke(states[1], times[1], 2_000_000_000_000_000_000)

                identity_path = loaded[2].root / "verified-candidate.json"
                identity_path.write_text(
                    json.dumps(loaded[2].verified, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(
                    CandidateContractError, "VERIFIED_CANDIDATE_OUTPUT_CONFLICT"
                ):
                    invoke(states[2], times[2], 2_100_000_000_000_000_000)

                forged = copy.deepcopy(loaded[0].verified)
                forged["source_tree"] = "d" * 40
                forged_bytes = canonical_json_bytes(forged)
                (loaded[0].root / "verified-candidate.json").write_bytes(
                    forged_bytes
                )
                with self.assertRaisesRegex(
                    CandidateContractError,
                    "VERIFIED_CANDIDATE_INPUT_BINDING_MISMATCH",
                ):
                    load_verified_candidate(
                        sha256_bytes(forged_bytes), _state_root=states[0]
                    )

            self.assertEqual(
                loaded[0].verified_digest, results[0]["verifiedCandidateDigest"]
            )
            self.assertEqual(loaded[0].candidate_input, candidate)
            self.assertEqual(
                loaded[0].root.name, results[0]["candidateInputDigest"][7:]
            )
            self.assertFalse(results[0]["existing"])

    def test_complete_layout_passes_and_missing_config_fails(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary)
            layout = source / "oci" / "api"
            manifest = _layout(layout, "api")
            result = normalize_candidate_oci_layout(
                source_root=source,
                layout=layout,
                role="api",
                repository="ghcr.io/yanyuhanyue/animemo-api",
                expected_digest=manifest,
            )
            self.assertFalse(result["changed"])
            config = json.loads(
                (layout / "blobs" / "sha256" / manifest[7:]).read_text()
            )["config"]["digest"]
            (layout / "blobs" / "sha256" / config[7:]).unlink()
            with self.assertRaises(OCIContractError):
                normalize_candidate_oci_layout(
                    source_root=source,
                    layout=layout,
                    role="api",
                    repository="ghcr.io/yanyuhanyue/animemo-api",
                    expected_digest=manifest,
                )

    def test_buildx_directory_descriptor_is_closed_without_rewriting_dag(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary)
            layout = source / "oci" / "api"
            manifest = _layout(layout, "api")
            (layout / "ingest").mkdir()
            index_path = layout / "index.json"
            index = json.loads(index_path.read_text())
            index["manifests"][0]["annotations"] = {
                "org.opencontainers.image.created": "2026-08-25T17:09:53Z",
                "org.opencontainers.image.ref.name": "animemo-release-api:candidate",
            }
            index_path.write_bytes(canonical_json_bytes(index))
            blobs_before = {
                path.name: _digest(path.read_bytes())
                for path in (layout / "blobs" / "sha256").iterdir()
            }

            result = normalize_candidate_oci_layout(
                source_root=source,
                layout=layout,
                role="api",
                repository="ghcr.io/yanyuhanyue/animemo-api",
                expected_digest=manifest,
            )

            self.assertTrue(result["changed"])
            self.assertTrue(result["ingestDirectoryRemoved"])
            self.assertFalse((layout / "ingest").exists())
            self.assertEqual(result["digest"], manifest)
            self.assertEqual(
                set(json.loads(index_path.read_text())["manifests"][0]),
                {"digest", "mediaType", "platform", "size"},
            )
            self.assertEqual(
                blobs_before,
                {
                    path.name: _digest(path.read_bytes())
                    for path in (layout / "blobs" / "sha256").iterdir()
                },
            )

    def test_buildx_nonempty_ingest_directory_is_rejected_without_mutation(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary)
            layout = source / "oci" / "api"
            manifest = _layout(layout, "api")
            ingest = layout / "ingest"
            ingest.mkdir()
            marker = ingest / "active"
            marker.write_text("not exporter scratch")
            index_path = layout / "index.json"
            original_index = index_path.read_bytes()

            with self.assertRaisesRegex(
                CandidateContractError, "CANDIDATE_OCI_INGEST_NOT_EMPTY"
            ):
                normalize_candidate_oci_layout(
                    source_root=source,
                    layout=layout,
                    role="api",
                    repository="ghcr.io/yanyuhanyue/animemo-api",
                    expected_digest=manifest,
                )

            self.assertEqual(index_path.read_bytes(), original_index)
            self.assertEqual(marker.read_text(), "not exporter scratch")

    def test_buildx_ingest_regular_file_is_rejected_without_mutation(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary)
            layout = source / "oci" / "api"
            manifest = _layout(layout, "api")
            ingest = layout / "ingest"
            ingest.write_text("not a directory")
            index_path = layout / "index.json"
            original_index = index_path.read_bytes()

            with self.assertRaisesRegex(
                CandidateContractError, "CANDIDATE_OCI_INGEST_INVALID"
            ):
                normalize_candidate_oci_layout(
                    source_root=source,
                    layout=layout,
                    role="api",
                    repository="ghcr.io/yanyuhanyue/animemo-api",
                    expected_digest=manifest,
                )

            self.assertEqual(index_path.read_bytes(), original_index)
            self.assertEqual(ingest.read_text(), "not a directory")

    def test_buildx_ingest_cleanup_rolls_back_when_dag_is_invalid(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary)
            layout = source / "oci" / "api"
            manifest = _layout(layout, "api")
            ingest = layout / "ingest"
            ingest.mkdir()
            index_path = layout / "index.json"
            index = json.loads(index_path.read_text())
            index["manifests"][0]["annotations"] = {
                "org.opencontainers.image.ref.name": "animemo-release-api:candidate"
            }
            original_index = canonical_json_bytes(index)
            index_path.write_bytes(original_index)
            config = json.loads(
                (layout / "blobs" / "sha256" / manifest[7:]).read_text()
            )["config"]["digest"]
            (layout / "blobs" / "sha256" / config[7:]).unlink()

            with self.assertRaises(OCIContractError):
                normalize_candidate_oci_layout(
                    source_root=source,
                    layout=layout,
                    role="api",
                    repository="ghcr.io/yanyuhanyue/animemo-api",
                    expected_digest=manifest,
                )

            self.assertEqual(index_path.read_bytes(), original_index)
            self.assertTrue(ingest.is_dir())
            self.assertEqual(list(ingest.iterdir()), [])

    def test_buildx_directory_descriptor_rejects_platform_drift(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary)
            layout = source / "oci" / "api"
            manifest = _layout(layout, "api")
            index_path = layout / "index.json"
            index = json.loads(index_path.read_text())
            index["manifests"][0]["annotations"] = {
                "org.opencontainers.image.ref.name": "animemo-release-api:candidate"
            }
            index["manifests"][0]["platform"] = {
                "architecture": "arm64",
                "os": "linux",
            }
            encoded = canonical_json_bytes(index)
            index_path.write_bytes(encoded)

            with self.assertRaisesRegex(
                CandidateContractError, "CANDIDATE_OCI_DESCRIPTOR_INVALID"
            ):
                normalize_candidate_oci_layout(
                    source_root=source,
                    layout=layout,
                    role="api",
                    repository="ghcr.io/yanyuhanyue/animemo-api",
                    expected_digest=manifest,
                )
            self.assertEqual(index_path.read_bytes(), encoded)

    def test_containing_artifact_requires_exact_id_and_api_digest(self):
        with tempfile.TemporaryDirectory() as temporary:
            archive = Path(temporary) / "artifact.zip"
            archive.write_bytes(b"not-a-zip")
            digest = _digest(archive.read_bytes())
            metadata = {
                "total_count": 1,
                "artifacts": [{
                    "id": 99, "name": f"release-qualification-{RUN_ID}",
                    "expired": False, "digest": digest,
                    "archive_download_url": "https://api.github.com/repos/yanyuhanyue/AniMemo/actions/artifacts/99/zip",
                    "workflow_run": {"id": RUN_ID, "head_sha": SHA},
                }],
            }
            run = {
                "id": RUN_ID, "name": "Release Producer",
                "path": ".github/workflows/release.yml", "event": "workflow_dispatch",
                "status": "completed", "conclusion": "success", "run_attempt": 1,
                "repository": {"full_name": "yanyuhanyue/AniMemo"},
                "head_branch": "main", "head_sha": SHA,
            }
            jobs = {"total_count": 2, "jobs": [
                {"name": "phase-a-qualification-evidence", "status": "completed", "conclusion": "success"},
                {"name": "publish-immutable-prerelease", "status": "completed", "conclusion": "skipped"},
            ]}
            with self.assertRaisesRegex(CandidateContractError, "CONTAINING_ARTIFACT_MISMATCH"):
                verify_prepublication_candidate(
                    archive=archive, run_metadata=run, jobs_metadata=jobs,
                    artifacts_metadata=metadata, containing_artifact_id=100,
                    containing_artifact_api_digest=digest, expected_run_id=RUN_ID,
                    expected_source_sha=SHA, expected_source_tree=TREE,
                    expected_candidate_version="v1.1.0-rc.14",
                    verified_at="2026-08-25T12:00:00Z", _state_root=Path(temporary) / "state",
                )

if __name__ == "__main__":
    unittest.main()
