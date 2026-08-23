from __future__ import annotations

import copy
import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import unittest
import warnings
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import jsonschema

from durability.platform import (
    canonical_platform_qualification_bytes,
    finalize_platform_qualification,
)
from release.acceptance import (
    AcceptanceError,
    build_rc_live_acceptance,
    validate_rc_live_acceptance,
    validate_stable_promotion_acceptance,
    verify_stable_promotion_acceptance,
)
from release.contract import build_deployment_contract
from release.materials import (
    PLATFORM_QUALIFICATION_MATERIAL,
    MaterialContractError,
    build_installer_materials,
    build_prepublication_material_identity,
    descriptor_relative_release_io_available,
    extract_qualification_artifact,
    verify_prepublication_material_identity,
)
from release.mirror import MirrorError, build_mirror_plan, replicate_exact_bytes
from release.publication import (
    PublicationError,
    PublicationTransaction,
    build_publication_plan,
    validate_publication_plan,
    verify_post_publish,
)
from release.vm_qualification import (
    VMQualificationError,
    classify_github_transport,
    classify_legacy_release,
    validate_pre_publish_qualification,
)
from scripts.tests.test_platform_qualification import unsigned_payload
from scripts.tests.trust_kit_fixture import create_test_initial_trust_kit

COMMIT = "b" * 40
API = "sha256:" + "1" * 64
WEB = "sha256:" + "2" * 64
IDENTITY = "sha256:" + "3" * 64
ASSET_BYTES = {
    "release-manifest.json": b"manifest\n",
    "deployment-contract.json": b"deployment\n",
    "installer-materials.tar": b"materials",
    "checksums.txt": b"checksums\n",
}


def frozen_prepublication_fixture(directory: Path):
    source = directory / "source"
    for relative in (
        "deploy/docker-compose.yml",
        "deploy/install-updater.sh",
        "deploy/updater/animemo",
        "deploy/updater/animemo-updater",
        "deploy/updater/animemo-updater@.service",
        "deploy/updater/animemo-updater.sysusers.conf",
        "deploy/updater/animemo-updater.tmpfiles.conf",
        "updater/docker-compose.runtime.yml",
    ):
        target = source / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(relative + "\n", encoding="utf-8", newline="\n")
    for package in ("durability", "release", "updater", "installer"):
        package_root = source / package
        package_root.mkdir(parents=True, exist_ok=True)
        (package_root / "__init__.py").write_text("", encoding="utf-8")
    (source / PLATFORM_QUALIFICATION_MATERIAL).write_bytes(
        canonical_platform_qualification_bytes(
            finalize_platform_qualification(unsigned_payload())
        )
    )
    trust_kit = create_test_initial_trust_kit(directory)
    verifier = (
        source
        / "release"
        / "release_attestation_verifier"
        / "offline-release-verifier"
    )
    verifier.parent.mkdir(parents=True, exist_ok=True)
    verifier.write_bytes((trust_kit / "offline-release-verifier").read_bytes())
    wheelhouse = directory / "wheelhouse"
    wheelhouse.mkdir()
    (wheelhouse / "qualified_dependency-1.0-py3-none-any.whl").write_bytes(
        b"qualified wheel bytes"
    )
    archive = directory / "installer-materials.tar"
    build_installer_materials(
        source,
        wheelhouse=wheelhouse,
        output=archive,
        initial_trust_kit=trust_kit,
    )
    deployment_contract = directory / "deployment-contract.json"
    deployment_contract.write_text(
        json.dumps(
            build_deployment_contract(source, installer_materials=archive),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return archive, deployment_contract


def rewrite_material_archive(source: Path, destination: Path, mutate):
    entries = []
    with tarfile.open(source, mode="r:") as archive:
        for member in archive.getmembers():
            extracted = archive.extractfile(member) if member.isfile() else None
            entries.append((member, extracted.read() if extracted is not None else b""))
    entries = mutate(entries)
    with tarfile.open(destination, mode="w:", format=tarfile.USTAR_FORMAT) as archive:
        for member, content in entries:
            archive.addfile(member, io.BytesIO(content) if member.isfile() else None)
    return destination


def asset_identities():
    return {
        name: {
            "sha256": "sha256:" + hashlib.sha256(content).hexdigest(),
            "size": len(content),
        }
        for name, content in ASSET_BYTES.items()
    }


def publication_plan(channel="rc", tag="v1.1.0-rc.TEST"):
    return build_publication_plan(
        repository="yanyuhanyue/AniMemo",
        channel=channel,
        tag=tag,
        commit=COMMIT,
        qualification_identity=IDENTITY,
        release_notes_identity="sha256:" + "4" * 64,
        release_notes_markdown_sha256="sha256:" + "5" * 64,
        assets=asset_identities(),
        api_digest=API,
        web_digest=WEB,
    )


def acceptance_record(**changes):
    fields = {
        "rc_tag": "v1.1.0-rc.1",
        "rc_commit": COMMIT,
        "release_manifest_identity": asset_identities()["release-manifest.json"]["sha256"],
        "deployment_contract_identity": asset_identities()["deployment-contract.json"]["sha256"],
        "installer_materials_identity": asset_identities()["installer-materials.tar"]["sha256"],
        "api_digest": API,
        "web_digest": WEB,
        "fresh_base_identity": "sha256:" + "6" * 64,
        "docker_base_identity": "sha256:" + "7" * 64,
        "runtime_base_identity": "sha256:" + "8" * 64,
        "install_path": "github",
        "doctor_result": "PASS",
        "upgrade_result": "NOT_APPLICABLE",
        "accepted_at": "2026-08-19T12:34:56Z",
        "operator_identity": "github:maintainer-review/v1",
        "tool_identity": "sha256:" + "9" * 64,
    }
    fields.update(changes)
    return build_rc_live_acceptance(**fields)


class DraftPublicationTests(unittest.TestCase):
    def test_rc_draft_upload_readback_verify_then_publish(self):
        plan = publication_plan()
        commands = plan["commands"]
        self.assertEqual(commands["create_tag"][-1], "v1.1.0-rc.TEST")
        self.assertEqual(
            commands["create_draft"][commands["create_draft"].index("--title") + 1],
            "v1.1.0-rc.TEST",
        )
        self.assertNotIn("AniMemo", commands["create_tag"][-1])
        self.assertIn("--draft", commands["create_draft"])
        self.assertIn("--prerelease", commands["create_draft"])
        self.assertIn("--notes-file", commands["create_draft"])
        self.assertNotIn("--generate-notes", commands["create_draft"])
        self.assertEqual(plan["external_mutation_mode"], "PLAN_ONLY")
        self.assertEqual(validate_publication_plan(copy.deepcopy(plan)), plan)

        transaction = PublicationTransaction(plan)
        transaction.record_tag_created(tag="v1.1.0-rc.TEST", target=COMMIT)
        transaction.record_draft_created(
            release_id=123, tag="v1.1.0-rc.TEST", target=COMMIT, prerelease=True
        )
        transaction.record_assets_uploaded(list(ASSET_BYTES))
        transaction.record_draft_verified(
            remote_assets=asset_identities(),
            downloaded_assets=ASSET_BYTES,
            notes_body_sha256="sha256:" + "5" * 64,
        )
        transaction.record_published(
            tag="v1.1.0-rc.TEST", target=COMMIT, prerelease=True
        )
        self.assertEqual(transaction.state, "PUBLISHED")
        self.assertEqual(
            transaction.history,
            [
                "NOT_STARTED",
                "TAG_CREATED",
                "DRAFT_CREATED",
                "ASSETS_UPLOADED",
                "DRAFT_VERIFIED",
                "PUBLISHED",
            ],
        )

    def test_missing_extra_digest_mismatch_and_invalid_transition_fail_closed(self):
        for mutation in ("missing", "extra", "digest", "bytes"):
            transaction = PublicationTransaction(publication_plan())
            transaction.record_tag_created(tag="v1.1.0-rc.TEST", target=COMMIT)
            transaction.record_draft_created(
                release_id=1, tag="v1.1.0-rc.TEST", target=COMMIT, prerelease=True
            )
            transaction.record_assets_uploaded(list(ASSET_BYTES))
            remote = copy.deepcopy(asset_identities())
            downloaded = dict(ASSET_BYTES)
            if mutation == "missing":
                remote.pop("checksums.txt")
            elif mutation == "extra":
                remote["unexpected.bin"] = {"sha256": "sha256:" + "0" * 64, "size": 0}
            elif mutation == "digest":
                remote["checksums.txt"]["sha256"] = "sha256:" + "0" * 64
            else:
                downloaded["checksums.txt"] = b"tamper"
            with self.subTest(mutation=mutation), self.assertRaises(PublicationError):
                transaction.record_draft_verified(
                    remote_assets=remote,
                    downloaded_assets=downloaded,
                    notes_body_sha256="sha256:" + "5" * 64,
                )
            self.assertEqual(transaction.state, "FAILED_PARTIAL")

        with self.assertRaises(PublicationError):
            PublicationTransaction(publication_plan()).record_published(
                tag="v1.1.0-rc.TEST", target=COMMIT, prerelease=True
            )

    def test_stable_plan_reuses_rc_commit_and_digests_without_build_commands(self):
        plan = publication_plan(channel="stable", tag="v1.1.0")
        commands = plan["commands"]
        self.assertEqual(commands["create_tag"][-1], "v1.1.0")
        self.assertEqual(
            commands["create_draft"][commands["create_draft"].index("--title") + 1],
            "v1.1.0",
        )
        flattened = " ".join(word for command in plan["commands"].values() for word in command)
        self.assertNotIn("docker build", flattened)
        self.assertNotIn("build-push-action", flattened)
        self.assertIn("--latest", plan["commands"]["publish"])
        self.assertEqual(plan["build_policy"], "REUSE_ACCEPTED_RC_EXACT_DIGESTS")

    def test_post_publish_verification_binds_public_assets_notes_and_oci(self):
        plan = publication_plan()
        result = verify_post_publish(
            plan,
            release={
                "tag": "v1.1.0-rc.TEST",
                "target": COMMIT,
                "draft": False,
                "prerelease": True,
                "notes_body_sha256": "sha256:" + "5" * 64,
                "public_unauthenticated_assets": True,
            },
            remote_assets=asset_identities(),
            downloaded_assets=ASSET_BYTES,
            api_digest=API,
            web_digest=WEB,
            attestations_verified=True,
        )
        self.assertEqual(result["status"], "PASS")
        with self.assertRaises(PublicationError):
            verify_post_publish(
                plan,
                release={
                    "tag": "v1.1.0-rc.TEST",
                    "target": COMMIT,
                    "draft": False,
                    "prerelease": True,
                    "notes_body_sha256": "sha256:" + "5" * 64,
                    "public_unauthenticated_assets": False,
                },
                remote_assets=asset_identities(),
                downloaded_assets=ASSET_BYTES,
                api_digest=API,
                web_digest=WEB,
                attestations_verified=True,
            )


class AcceptanceAndPromotionTests(unittest.TestCase):
    def test_acceptance_record_is_closed_self_identifying_and_authorizes_exact_stable(self):
        record = acceptance_record()
        validate_rc_live_acceptance(record)
        result = verify_stable_promotion_acceptance(
            record,
            expected={
                "rc_tag": "v1.1.0-rc.1",
                "rc_commit": COMMIT,
                "release_manifest_identity": asset_identities()["release-manifest.json"]["sha256"],
                "deployment_contract_identity": asset_identities()["deployment-contract.json"]["sha256"],
                "installer_materials_identity": asset_identities()["installer-materials.tar"]["sha256"],
                "api_digest": API,
                "web_digest": WEB,
            },
            stable_commit=COMMIT,
            stable_api_digest=API,
            stable_web_digest=WEB,
        )
        self.assertEqual(result["status"], "AUTHORIZED")
        self.assertFalse(result["rebuild_allowed"])
        schema = json.loads(
            (Path(__file__).with_name("rc-live-acceptance.schema.json")).read_text(
                encoding="utf-8"
            )
        )
        jsonschema.Draft202012Validator.check_schema(schema)
        jsonschema.validate(record, schema)

    def test_acceptance_rejects_test_only_and_noncanonical_rc_tags(self):
        for rc_tag in (
            "v1.1.0-rc.TEST",
            "v01.1.0-rc.1",
            "v1.01.0-rc.1",
            "v1.1.00-rc.1",
            "v1.1.0-rc.01",
            "v1.1.0-rc.0",
        ):
            with self.subTest(rc_tag=rc_tag), self.assertRaises(AcceptanceError):
                acceptance_record(rc_tag=rc_tag)

    def test_stable_promotion_receipt_is_closed_and_bound_to_live_acceptance(self):
        record = acceptance_record()
        receipt = verify_stable_promotion_acceptance(
            record,
            expected={
                "rc_tag": record["rc_tag"],
                "rc_commit": record["rc_commit"],
                "release_manifest_identity": record["release_manifest_identity"],
                "deployment_contract_identity": record["deployment_contract_identity"],
                "installer_materials_identity": record["installer_materials_identity"],
                "api_digest": record["api_digest"],
                "web_digest": record["web_digest"],
            },
            stable_commit=record["rc_commit"],
            stable_api_digest=record["api_digest"],
            stable_web_digest=record["web_digest"],
        )
        self.assertEqual(
            validate_stable_promotion_acceptance(receipt, acceptance=record),
            receipt,
        )
        for mutate in (
            lambda value: value.__setitem__("status", "AUTHORIZED_BYPASS"),
            lambda value: value.__setitem__("stable_commit", "a" * 40),
            lambda value: value.__setitem__("unexpected", True),
        ):
            tampered = copy.deepcopy(receipt)
            mutate(tampered)
            with self.subTest(tampered=tampered), self.assertRaises(AcceptanceError):
                validate_stable_promotion_acceptance(tampered, acceptance=record)

    def test_wrong_rc_digest_tamper_and_missing_acceptance_are_rejected(self):
        record = acceptance_record()
        expected = {
            "rc_tag": "v1.1.0-rc.1",
            "rc_commit": COMMIT,
            "release_manifest_identity": asset_identities()["release-manifest.json"]["sha256"],
            "deployment_contract_identity": asset_identities()["deployment-contract.json"]["sha256"],
            "installer_materials_identity": asset_identities()["installer-materials.tar"]["sha256"],
            "api_digest": API,
            "web_digest": WEB,
        }
        with self.assertRaises(AcceptanceError):
            verify_stable_promotion_acceptance(
                record,
                expected={**expected, "api_digest": "sha256:" + "0" * 64},
                stable_commit=COMMIT,
                stable_api_digest=API,
                stable_web_digest=WEB,
            )
        with self.assertRaises(AcceptanceError):
            verify_stable_promotion_acceptance(
                record,
                expected=expected,
                stable_commit="a" * 40,
                stable_api_digest=API,
                stable_web_digest=WEB,
            )
        tampered = copy.deepcopy(record)
        tampered["doctor_result"] = "FAIL"
        with self.assertRaises(AcceptanceError):
            validate_rc_live_acceptance(tampered)
        with self.assertRaises(AcceptanceError):
            validate_rc_live_acceptance(None)


class MirrorTests(unittest.TestCase):
    def test_mirror_copies_exact_authority_bytes_and_preserves_oci_identity(self):
        plan = build_mirror_plan(
            authority="GITHUB_RELEASE",
            repository="yanyuhanyue/AniMemo",
            tag="v1.1.0-rc.TEST",
            commit=COMMIT,
            release_identity=IDENTITY,
            assets=asset_identities(),
            api_digest=API,
            web_digest=WEB,
        )
        written = {}

        def sink(name, content):
            written[name] = bytes(content)

        receipt = replicate_exact_bytes(
            plan,
            fetched=ASSET_BYTES,
            write=sink,
            readback=lambda name: written[name],
        )
        self.assertEqual(receipt["status"], "PASS")
        self.assertEqual(receipt["authority"], "GITHUB_RELEASE")
        self.assertEqual(receipt["role"], "TRANSPORT_ONLY")
        self.assertEqual(receipt["api_digest"], API)

    def test_mirror_cannot_select_version_transform_bytes_or_fallback(self):
        with self.assertRaises(MirrorError):
            build_mirror_plan(
                authority="OFFICIAL_MIRROR",
                repository="yanyuhanyue/AniMemo",
                tag="v1.1.0-rc.TEST",
                commit=COMMIT,
                release_identity=IDENTITY,
                assets=asset_identities(),
                api_digest=API,
                web_digest=WEB,
            )
        plan = build_mirror_plan(
            authority="GITHUB_RELEASE",
            repository="yanyuhanyue/AniMemo",
            tag="v1.1.0-rc.TEST",
            commit=COMMIT,
            release_identity=IDENTITY,
            assets=asset_identities(),
            api_digest=API,
            web_digest=WEB,
        )
        with self.assertRaises(MirrorError):
            replicate_exact_bytes(
                plan,
                fetched=ASSET_BYTES,
                write=lambda name, content: None,
                readback=lambda name: b"transformed",
            )
        self.assertEqual(plan["fallback_policy"], "FORBIDDEN")
        self.assertEqual(plan["version_selection"], "FORBIDDEN")


class VMQualificationBoundaryTests(unittest.TestCase):
    def test_pre_publish_contract_freezes_three_non_interchangeable_roles(self):
        result = validate_pre_publish_qualification(
            docker_base="PASS",
            runtime_base="PASS",
            fresh_base_bootstrap="PASS",
            docker_reinstalled_on_docker_base=False,
            docker_reinstalled_on_runtime_base=False,
            live_public_rc_acceptance="DEFERRED_POST_RC_BY_DESIGN",
        )
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["docker_base_role"], "CANONICAL_FRESH_INSTALL_BASE")
        self.assertEqual(result["runtime_base_role"], "PRIMARY_RUNTIME_QUALIFICATION_BASE")
        self.assertEqual(result["fresh_base_role"], "BARE_HOST_BOOTSTRAP_BASE")

    def test_legacy_release_and_transport_degradation_are_not_installer_defects(self):
        legacy = classify_legacy_release(
            tag="v1.0.0",
            observed_assets={
                "release-manifest.json",
                "deployment-contract.json",
                "checksums.txt",
            },
        )
        self.assertEqual(
            legacy["classification"],
            "LEGACY_RELEASE_NOT_ELIGIBLE_FOR_V1_1_CONTRACT_E2E",
        )
        self.assertFalse(legacy["installer_defect"])
        transport = classify_github_transport("CONNECTION_RESET")
        self.assertEqual(
            transport["classification"],
            "GITHUB_PUBLIC_TRANSPORT_ENVIRONMENT_DEGRADED",
        )
        self.assertFalse(transport["installer_defect"])
        self.assertFalse(transport["qualification_pass"])

    def test_pre_publish_does_not_accept_docker_reinstallation_or_fake_live_acceptance(self):
        with self.assertRaises(VMQualificationError):
            validate_pre_publish_qualification(
                docker_base="PASS",
                runtime_base="PASS",
                fresh_base_bootstrap="PASS",
                docker_reinstalled_on_docker_base=True,
                docker_reinstalled_on_runtime_base=False,
                live_public_rc_acceptance="PASS",
            )


class FrozenPrepublicationMaterialTests(unittest.TestCase):
    @staticmethod
    def _changed_stat(metadata, **changes):
        values = {
            name: getattr(metadata, name)
            for name in (
                "st_dev",
                "st_ino",
                "st_mode",
                "st_nlink",
                "st_size",
                "st_mtime_ns",
                "st_ctime_ns",
            )
        }
        values.update(changes)
        return SimpleNamespace(**values)

    def test_builder_writes_and_hashes_the_held_temporary_descriptor(self):
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            real_tar_open = tarfile.open
            write_calls = []

            def require_file_object(*args, **kwargs):
                if kwargs.get("mode") == "w:":
                    write_calls.append(kwargs.get("fileobj"))
                    self.assertFalse(args)
                    self.assertIsNotNone(kwargs.get("fileobj"))
                return real_tar_open(*args, **kwargs)

            with mock.patch(
                "release.materials.tarfile.open",
                side_effect=require_file_object,
            ):
                archive, _contract = frozen_prepublication_fixture(temporary)

            self.assertTrue(archive.is_file())
            self.assertEqual(len(write_calls), 1)

    def test_builder_rejects_temporary_path_replacement_without_touching_victim(self):
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            source = temporary / "source"
            wheelhouse = temporary / "wheelhouse"
            trust_kit = temporary / "trust-kit"
            output = temporary / "installer-materials.tar"
            victim = temporary / "victim"
            victim.write_bytes(b"do not touch")
            real_tar_open = tarfile.open

            def replace_temporary_path(*args, **kwargs):
                if kwargs.get("mode") == "w:":
                    candidates = list(
                        temporary.glob(".installer-materials.tar.*.tmp")
                    )
                    self.assertEqual(len(candidates), 1)
                    try:
                        candidates[0].unlink()
                    except PermissionError:
                        self.skipTest("open files cannot be unlinked on this platform")
                    try:
                        candidates[0].symlink_to(victim)
                    except (OSError, NotImplementedError):
                        self.skipTest("symlinks unavailable")
                return real_tar_open(*args, **kwargs)

            # The attack happens before source enumeration, so only the temporary
            # descriptor lifecycle is under test here.
            with (
                mock.patch(
                    "release.materials.tarfile.open",
                    side_effect=replace_temporary_path,
                ),
                mock.patch(
                    "release.materials._profile_paths",
                    return_value=[],
                ),
                self.assertRaisesRegex(
                    MaterialContractError,
                    "temporary file identity changed",
                ),
            ):
                build_installer_materials(
                    source,
                    wheelhouse=wheelhouse,
                    output=output,
                    initial_trust_kit=trust_kit,
                )

            self.assertEqual(victim.read_bytes(), b"do not touch")
            self.assertFalse(output.exists())

    @unittest.skipUnless(
        descriptor_relative_release_io_available(),
        "descriptor-relative release publication unavailable",
    )
    def test_builder_removes_wrong_output_when_temp_is_swapped_at_publish(self):
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            output = temporary / "installer-materials.tar"
            real_link = os.link

            def replace_before_link(
                source,
                destination,
                *,
                parent_descriptor,
            ):
                os.unlink(source, dir_fd=parent_descriptor)
                attacker = os.open(
                    source,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                    dir_fd=parent_descriptor,
                )
                try:
                    os.write(attacker, b"attacker")
                finally:
                    os.close(attacker)
                return real_link(
                    source,
                    destination,
                    src_dir_fd=parent_descriptor,
                    dst_dir_fd=parent_descriptor,
                    follow_symlinks=False,
                )

            with (
                mock.patch(
                    "release.materials._profile_paths",
                    return_value=[],
                ),
                mock.patch(
                    "release.materials._link_release_file",
                    side_effect=replace_before_link,
                ),
                self.assertRaisesRegex(
                    MaterialContractError,
                    "publication identity changed",
                ),
            ):
                build_installer_materials(
                    temporary / "source",
                    wheelhouse=temporary / "wheelhouse",
                    output=output,
                    initial_trust_kit=temporary / "trust-kit",
                )

            self.assertFalse(output.exists())

    def test_exact_frozen_material_identity_verifies(self):
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            archive, deployment_contract = frozen_prepublication_fixture(temporary)
            payload = build_prepublication_material_identity(
                installer_materials=archive,
                deployment_contract=deployment_contract,
                candidate_sha=COMMIT,
                candidate_tree_sha="c" * 40,
            )

            result = verify_prepublication_material_identity(
                payload,
                installer_materials=archive,
                deployment_contract=deployment_contract,
                expected_candidate_sha=COMMIT,
                expected_candidate_tree_sha="c" * 40,
            )

            self.assertEqual(
                (payload["schemaVersion"], result["status"]),
                (2, "PASS"),
            )
            self.assertEqual(
                (
                    payload["installerMaterials"]["memberManifestSha256"],
                    payload["wheelhouse"]["aggregateSha256"],
                    payload["pretrust"]["aggregateSha256"],
                ),
                (
                    "sha256:77235727fcd7e23aeccf34f8c45134ac2ac5cbf909a211469df60335ab25b38f",
                    "sha256:ca794441aa84a156fc47d0cf2efc2d04aef61517925e5dcccbbcc181ec98b93a",
                    "sha256:bf605057d25869d1a0616a574547c5d2106fe1a4aa013ea7af09dfe03b6e2502",
                ),
            )

    def test_source_replacement_during_open_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            archive, deployment_contract = frozen_prepublication_fixture(temporary)
            real_fstat = os.fstat

            def replaced_identity(descriptor):
                metadata = real_fstat(descriptor)
                return self._changed_stat(metadata, st_ino=metadata.st_ino + 1)

            with mock.patch(
                "release.materials.os.fstat", replaced_identity
            ), self.assertRaisesRegex(MaterialContractError, "changed while opening"):
                build_prepublication_material_identity(
                    installer_materials=archive,
                    deployment_contract=deployment_contract,
                    candidate_sha=COMMIT,
                    candidate_tree_sha="c" * 40,
                )

    def test_source_mutation_during_read_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            archive, deployment_contract = frozen_prepublication_fixture(temporary)
            real_fstat = os.fstat
            calls = 0

            def changed_after_read(descriptor):
                nonlocal calls
                calls += 1
                metadata = real_fstat(descriptor)
                if calls == 2:
                    return self._changed_stat(
                        metadata,
                        st_mtime_ns=metadata.st_mtime_ns + 1,
                    )
                return metadata

            with mock.patch(
                "release.materials.os.fstat", changed_after_read
            ), self.assertRaisesRegex(MaterialContractError, "changed while reading"):
                build_prepublication_material_identity(
                    installer_materials=archive,
                    deployment_contract=deployment_contract,
                    candidate_sha=COMMIT,
                    candidate_tree_sha="c" * 40,
                )

    def test_archive_byte_and_member_set_tampering_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            archive, deployment_contract = frozen_prepublication_fixture(temporary)
            payload = build_prepublication_material_identity(
                installer_materials=archive,
                deployment_contract=deployment_contract,
                candidate_sha=COMMIT,
                candidate_tree_sha="c" * 40,
            )

            byte_tamper = temporary / "byte-tamper.tar"
            byte_tamper.write_bytes(archive.read_bytes() + b"tamper")

            def content_tamper(entries):
                return [
                    (
                        member,
                        (b"X" + content[1:])
                        if member.name.startswith("wheelhouse/")
                        else content,
                    )
                    for member, content in entries
                ]

            def remove_member(entries):
                return [
                    item
                    for item in entries
                    if not item[0].name.startswith("wheelhouse/")
                ]

            def add_member(entries):
                member = tarfile.TarInfo("zz-added-material")
                member.size = len(b"added")
                member.mode = 0o644
                member.mtime = 0
                member.uid = member.gid = 0
                member.uname = member.gname = ""
                return [*entries, (member, b"added")]

            variants = {
                "archive-byte-tamper": byte_tamper,
                "member-content-tamper": rewrite_material_archive(
                    archive, temporary / "content-tamper.tar", content_tamper
                ),
                "member-removed": rewrite_material_archive(
                    archive, temporary / "removed.tar", remove_member
                ),
                "member-added": rewrite_material_archive(
                    archive, temporary / "added.tar", add_member
                ),
                "member-reordered": rewrite_material_archive(
                    archive,
                    temporary / "reordered.tar",
                    lambda entries: list(reversed(entries)),
                ),
            }
            for label, candidate in variants.items():
                with self.subTest(label=label), self.assertRaises(
                    MaterialContractError
                ):
                    verify_prepublication_material_identity(
                        payload,
                        installer_materials=candidate,
                        deployment_contract=deployment_contract,
                        expected_candidate_sha=COMMIT,
                        expected_candidate_tree_sha="c" * 40,
                    )

    def test_every_declared_prepublication_identity_is_enforced(self):
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            archive, deployment_contract = frozen_prepublication_fixture(temporary)
            payload = build_prepublication_material_identity(
                installer_materials=archive,
                deployment_contract=deployment_contract,
                candidate_sha=COMMIT,
                candidate_tree_sha="c" * 40,
            )
            mutations = {
                "archive-sha": lambda item: item["installerMaterials"].__setitem__(
                    "sha256", "sha256:" + "0" * 64
                ),
                "archive-size": lambda item: item["installerMaterials"].__setitem__(
                    "size", item["installerMaterials"]["size"] + 1
                ),
                "archive-member-count": lambda item: item[
                    "installerMaterials"
                ].__setitem__(
                    "memberCount", item["installerMaterials"]["memberCount"] + 1
                ),
                "member-manifest": lambda item: item[
                    "installerMaterials"
                ].__setitem__("memberManifestSha256", "sha256:" + "0" * 64),
                "wheelhouse-aggregate": lambda item: item["wheelhouse"].__setitem__(
                    "aggregateSha256", "sha256:" + "0" * 64
                ),
                "pretrust-aggregate": lambda item: item["pretrust"].__setitem__(
                    "aggregateSha256", "sha256:" + "0" * 64
                ),
                "initial-trust-bootstrap": lambda item: item[
                    "initialTrustBootstrap"
                ].__setitem__("sha256", "sha256:" + "0" * 64),
                "offline-verifier": lambda item: item["offlineVerifier"].__setitem__(
                    "sha256", "sha256:" + "0" * 64
                ),
                "platform-qualification": lambda item: item[
                    "platformQualification"
                ].__setitem__("sha256", "sha256:" + "0" * 64),
                "deployment-contract": lambda item: item[
                    "deploymentContract"
                ].__setitem__("sha256", "sha256:" + "0" * 64),
            }
            for label, mutate in mutations.items():
                changed = copy.deepcopy(payload)
                mutate(changed)
                with self.subTest(label=label), self.assertRaisesRegex(
                    MaterialContractError, "identity differs"
                ):
                    verify_prepublication_material_identity(
                        changed,
                        installer_materials=archive,
                        deployment_contract=deployment_contract,
                        expected_candidate_sha=COMMIT,
                        expected_candidate_tree_sha="c" * 40,
                    )

    def test_candidate_schema_and_closed_shape_tampering_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            archive, deployment_contract = frozen_prepublication_fixture(temporary)
            payload = build_prepublication_material_identity(
                installer_materials=archive,
                deployment_contract=deployment_contract,
                candidate_sha=COMMIT,
                candidate_tree_sha="c" * 40,
            )
            mutations = {
                "candidate-sha": lambda item: item.__setitem__(
                    "candidateSha", "a" * 40
                ),
                "candidate-tree": lambda item: item.__setitem__(
                    "candidateTreeSha", "d" * 40
                ),
                "unknown-schema": lambda item: item.__setitem__(
                    "schemaVersion", 99
                ),
                "missing-field": lambda item: item.pop("deploymentContract"),
                "extra-field": lambda item: item.__setitem__("unexpected", True),
            }
            for label, mutate in mutations.items():
                changed = copy.deepcopy(payload)
                mutate(changed)
                with self.subTest(label=label), self.assertRaises(
                    MaterialContractError
                ):
                    verify_prepublication_material_identity(
                        changed,
                        installer_materials=archive,
                        deployment_contract=deployment_contract,
                        expected_candidate_sha=COMMIT,
                        expected_candidate_tree_sha="c" * 40,
                    )

    def test_deployment_contract_tamper_and_wrong_tar_binding_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            archive, deployment_contract = frozen_prepublication_fixture(temporary)
            payload = build_prepublication_material_identity(
                installer_materials=archive,
                deployment_contract=deployment_contract,
                candidate_sha=COMMIT,
                candidate_tree_sha="c" * 40,
            )
            whitespace_tamper = temporary / "contract-whitespace-tamper.json"
            whitespace_tamper.write_bytes(deployment_contract.read_bytes() + b"\n")
            wrong_binding = temporary / "contract-wrong-binding.json"
            changed = json.loads(deployment_contract.read_text(encoding="utf-8"))
            changed["archive"]["sha256"] = "sha256:" + "0" * 64
            wrong_binding.write_text(json.dumps(changed), encoding="utf-8")
            wrong_file_binding = temporary / "contract-wrong-file-binding.json"
            changed = json.loads(deployment_contract.read_text(encoding="utf-8"))
            changed["files"][0]["sha256"] = "sha256:" + "0" * 64
            wrong_file_binding.write_text(json.dumps(changed), encoding="utf-8")
            duplicate_field = temporary / "contract-duplicate-field.json"
            contract_text = deployment_contract.read_text(encoding="utf-8")
            duplicate_field.write_text(
                contract_text.replace(
                    "{\n",
                    '{\n  "schemaVersion": 2,\n',
                    1,
                ),
                encoding="utf-8",
                newline="\n",
            )

            for label, candidate in {
                "byte-tamper": whitespace_tamper,
                "wrong-tar-binding": wrong_binding,
                "wrong-file-binding": wrong_file_binding,
                "duplicate-field": duplicate_field,
            }.items():
                with self.subTest(label=label), self.assertRaises(
                    MaterialContractError
                ):
                    verify_prepublication_material_identity(
                        payload,
                        installer_materials=archive,
                        deployment_contract=candidate,
                        expected_candidate_sha=COMMIT,
                        expected_candidate_tree_sha="c" * 40,
                    )

    def test_malformed_traversal_and_link_archives_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            archive, deployment_contract = frozen_prepublication_fixture(temporary)
            payload = build_prepublication_material_identity(
                installer_materials=archive,
                deployment_contract=deployment_contract,
                candidate_sha=COMMIT,
                candidate_tree_sha="c" * 40,
            )

            def append_traversal(entries):
                member = tarfile.TarInfo("../outside")
                member.size = 1
                member.mode = 0o644
                member.mtime = 0
                member.uid = member.gid = 0
                member.uname = member.gname = ""
                return [*entries, (member, b"x")]

            def append_symlink(entries):
                member = tarfile.TarInfo("zz-symlink")
                member.type = tarfile.SYMTYPE
                member.linkname = "outside"
                member.mode = 0o644
                member.mtime = 0
                member.uid = member.gid = 0
                member.uname = member.gname = ""
                return [*entries, (member, b"")]

            def append_absolute_path(entries):
                member = tarfile.TarInfo("/outside")
                member.size = 1
                member.mode = 0o644
                member.mtime = 0
                member.uid = member.gid = 0
                member.uname = member.gname = ""
                return [*entries, (member, b"x")]

            def append_hardlink(entries):
                member = tarfile.TarInfo("zz-hardlink")
                member.type = tarfile.LNKTYPE
                member.linkname = entries[0][0].name
                member.mode = 0o644
                member.mtime = 0
                member.uid = member.gid = 0
                member.uname = member.gname = ""
                return [*entries, (member, b"")]

            def append_special_file(entries):
                member = tarfile.TarInfo("zz-special")
                member.type = tarfile.CHRTYPE
                member.mode = 0o644
                member.mtime = 0
                member.uid = member.gid = 0
                member.uname = member.gname = ""
                member.devmajor = member.devminor = 0
                return [*entries, (member, b"")]

            def append_duplicate(entries):
                member, content = entries[0]
                return [*entries, (copy.copy(member), content)]

            for label, mutate in {
                "traversal": append_traversal,
                "symlink": append_symlink,
                "absolute-path": append_absolute_path,
                "hardlink": append_hardlink,
                "special-file": append_special_file,
                "duplicate-member": append_duplicate,
            }.items():
                malformed = rewrite_material_archive(
                    archive, temporary / f"{label}.tar", mutate
                )
                with self.subTest(label=label), self.assertRaises(
                    MaterialContractError
                ):
                    verify_prepublication_material_identity(
                        payload,
                        installer_materials=malformed,
                        deployment_contract=deployment_contract,
                        expected_candidate_sha=COMMIT,
                        expected_candidate_tree_sha="c" * 40,
                    )

    def test_pretrust_verifier_must_equal_top_level_verifier(self):
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            archive, _deployment_contract = frozen_prepublication_fixture(temporary)

            def tamper_pretrust_verifier(entries):
                return [
                    (
                        member,
                        (b"X" + content[1:])
                        if member.name.endswith(
                            "pretrust-v2/offline-release-verifier"
                        )
                        else content,
                    )
                    for member, content in entries
                ]

            changed_archive = rewrite_material_archive(
                archive,
                temporary / "pretrust-verifier-tamper.tar",
                tamper_pretrust_verifier,
            )
            changed_contract = temporary / "changed-deployment-contract.json"
            changed_contract.write_text(
                json.dumps(
                    build_deployment_contract(
                        temporary / "source",
                        installer_materials=changed_archive,
                    ),
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                MaterialContractError,
                "Top-level and pretrust offline verifier identities differ",
            ):
                build_prepublication_material_identity(
                    installer_materials=changed_archive,
                    deployment_contract=changed_contract,
                    candidate_sha=COMMIT,
                    candidate_tree_sha="c" * 40,
                )

    def test_qualification_artifact_rejects_substitution_and_unsafe_zip_shapes(self):
        expected = {
            "release-qualification-42.json": b"qualification",
            "platform-qualification.json": b"platform",
            "release-notes.json": b"notes",
            "release-notes.md": b"notes markdown",
            "prepublication-materials.json": b"prepublication",
            "installer-materials.tar": b"materials",
            "deployment-contract.json": b"deployment",
        }

        def make_zip(path, entries):
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", UserWarning)
                with zipfile.ZipFile(path, mode="w") as archive:
                    for name, content in entries:
                        archive.writestr(name, content)

        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            valid = temporary / "valid.zip"
            make_zip(valid, list(expected.items()))
            valid_digest = "sha256:" + hashlib.sha256(valid.read_bytes()).hexdigest()
            result = extract_qualification_artifact(
                valid,
                temporary / "valid",
                qualification_run_id=42,
                expected_sha256=valid_digest,
            )
            self.assertEqual(result["fileCount"], 7)

            with self.assertRaisesRegex(
                MaterialContractError, "digest differs"
            ):
                extract_qualification_artifact(
                    valid,
                    temporary / "wrong-digest",
                    qualification_run_id=42,
                    expected_sha256="sha256:" + "0" * 64,
                )

            variants = {
                "wrong-run": [
                    *[(name, value) for name, value in expected.items() if not name.startswith("release-qualification-")],
                    ("release-qualification-41.json", b"qualification"),
                ],
                "unexpected-extra": [*expected.items(), ("unexpected", b"x")],
                "path-traversal": [*expected.items(), ("../outside", b"x")],
                "duplicate": [*expected.items(), ("release-notes.md", b"duplicate")],
            }
            for label, entries in variants.items():
                candidate = temporary / f"{label}.zip"
                make_zip(candidate, entries)
                with self.subTest(label=label), self.assertRaises(
                    MaterialContractError
                ):
                    extract_qualification_artifact(
                        candidate,
                        temporary / f"extract-{label}",
                        qualification_run_id=42,
                        expected_sha256="sha256:"
                        + hashlib.sha256(candidate.read_bytes()).hexdigest(),
                    )

    def test_qualification_artifact_mutation_on_open_handle_fails_closed(self):
        expected = {
            "release-qualification-42.json": b"qualification",
            "platform-qualification.json": b"platform",
            "release-notes.json": b"notes",
            "release-notes.md": b"notes markdown",
            "prepublication-materials.json": b"prepublication",
            "installer-materials.tar": b"materials",
            "deployment-contract.json": b"deployment",
        }
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            candidate = temporary / "candidate.zip"
            with zipfile.ZipFile(candidate, mode="w") as archive:
                for name, content in expected.items():
                    archive.writestr(name, content)
            expected_sha256 = "sha256:" + hashlib.sha256(
                candidate.read_bytes()
            ).hexdigest()
            real_fstat = os.fstat
            calls = 0

            def changed_after_extraction(descriptor):
                nonlocal calls
                calls += 1
                metadata = real_fstat(descriptor)
                if calls == 2:
                    return self._changed_stat(
                        metadata, st_ctime_ns=metadata.st_ctime_ns + 1
                    )
                return metadata

            with mock.patch(
                "release.materials.os.fstat", changed_after_extraction
            ), self.assertRaisesRegex(MaterialContractError, "changed while reading"):
                extract_qualification_artifact(
                    candidate,
                    temporary / "extracted",
                    qualification_run_id=42,
                    expected_sha256=expected_sha256,
                )
            self.assertFalse((temporary / "extracted").exists())

    def test_cli_builds_and_verifies_frozen_material_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            archive, deployment_contract = frozen_prepublication_fixture(temporary)
            identity = temporary / "prepublication-materials.json"
            build = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "release.cli",
                    "build-prepublication-materials",
                    "--installer-materials",
                    str(archive),
                    "--deployment-contract",
                    str(deployment_contract),
                    "--candidate-sha",
                    COMMIT,
                    "--candidate-tree-sha",
                    "c" * 40,
                    "--output",
                    str(identity),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            verify = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "release.cli",
                    "verify-prepublication-materials",
                    "--prepublication",
                    str(identity),
                    "--installer-materials",
                    str(archive),
                    "--deployment-contract",
                    str(deployment_contract),
                    "--expected-candidate-sha",
                    COMMIT,
                    "--expected-candidate-tree-sha",
                    "c" * 40,
                ],
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertEqual(
                (build.returncode, verify.returncode),
                (0, 0),
                msg=build.stderr + verify.stderr,
            )

            duplicated = identity.read_text(encoding="utf-8").replace(
                '"schemaVersion": 2,',
                '"schemaVersion": 2,\n  "schemaVersion": 2,',
                1,
            )
            identity.write_text(duplicated, encoding="utf-8")
            rejected = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "release.cli",
                    "verify-prepublication-materials",
                    "--prepublication",
                    str(identity),
                    "--installer-materials",
                    str(archive),
                    "--deployment-contract",
                    str(deployment_contract),
                    "--expected-candidate-sha",
                    COMMIT,
                    "--expected-candidate-tree-sha",
                    "c" * 40,
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(rejected.returncode, 0)
            self.assertIn("Duplicate JSON field", rejected.stderr)


if __name__ == "__main__":
    unittest.main()
