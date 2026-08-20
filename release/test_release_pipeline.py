from __future__ import annotations

import copy
import hashlib
import json
import unittest
from pathlib import Path

import jsonschema

from release.acceptance import (
    AcceptanceError,
    build_rc_live_acceptance,
    validate_rc_live_acceptance,
    validate_stable_promotion_acceptance,
    verify_stable_promotion_acceptance,
)
from release.mirror import MirrorError, build_mirror_plan, replicate_exact_bytes
from release.publication import (
    PublicationError,
    PublicationTransaction,
    build_publication_plan,
    verify_post_publish,
)
from release.vm_qualification import (
    VMQualificationError,
    classify_github_transport,
    classify_legacy_release,
    validate_pre_publish_qualification,
)

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
        self.assertIn("--draft", commands["create_draft"])
        self.assertIn("--prerelease", commands["create_draft"])
        self.assertIn("--notes-file", commands["create_draft"])
        self.assertNotIn("--generate-notes", commands["create_draft"])
        self.assertEqual(plan["external_mutation_mode"], "PLAN_ONLY")

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


if __name__ == "__main__":
    unittest.main()
