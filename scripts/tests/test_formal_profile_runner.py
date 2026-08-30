from __future__ import annotations

import base64
import hashlib
import inspect
import json
import pickle
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from installer.production import (
    FormalCandidateBoundOfflineVerifierCapability,
    build_production_composition,
    issue_formal_candidate_bound_offline_verifier,
)
from installer.runtime import InstallerError
from release.candidate import canonical_json_bytes, sha256_bytes
from release.formal_vm_controller import FormalAuthorityRequest
from release.trust_bootstrap import validate_initial_trust_kit
from scripts.formal_profile_runner import (
    FormalProfileRunnerError,
    execute_profile,
)
from scripts.tests.formal_windows_pretrust_fixture import (
    create_test_formal_windows_pretrust_kit,
)
from scripts.tests.trust_kit_fixture import create_test_initial_trust_kit


class FormalProfileRunnerTests(unittest.TestCase):
    def test_generic_production_composition_has_no_verifier_injection(self):
        self.assertNotIn(
            "offline_verifier",
            inspect.signature(build_production_composition).parameters,
        )
        self.assertNotIn(
            "verifier",
            inspect.signature(issue_formal_candidate_bound_offline_verifier).parameters,
        )
        with self.assertRaises(TypeError):
            FormalCandidateBoundOfflineVerifierCapability()

    def test_candidate_bound_guest_trust_ignores_ambient_trust_state(self):
        from release.formal_windows_pretrust import FORMAL_WINDOWS_PRETRUST_PREFIX
        from release.materials import INITIAL_TRUST_KIT_PREFIX

        with tempfile.TemporaryDirectory() as directory:
            scratch = Path(directory)
            source_root = scratch / "source"
            source_root.mkdir()
            initial = create_test_initial_trust_kit(source_root)
            formal = create_test_formal_windows_pretrust_kit(
                scratch / "formal-source",
                source_initial_trust_kit=initial,
            )
            authority_root = scratch / "runtime"
            shutil.copytree(initial, authority_root / INITIAL_TRUST_KIT_PREFIX)
            shutil.copytree(formal, authority_root / FORMAL_WINDOWS_PRETRUST_PREFIX)
            expected = validate_initial_trust_kit(initial).identity
            with (
                mock.patch(
                    "release.formal_windows_pretrust.assert_windows_private_acl"
                ),
                mock.patch(
                    "updater.offline.production_offline_release_verifier",
                    side_effect=AssertionError("ambient trust must be ignored"),
                ),
            ):
                capability = issue_formal_candidate_bound_offline_verifier(
                    authority_root, expected_profile_identity=expected
                )
            with self.assertRaises(TypeError):
                pickle.dumps(capability)
            verifier, temporary = capability._consume()
            try:
                self.assertEqual(verifier._profile.identity, expected)
            finally:
                temporary.cleanup()

    def test_candidate_bound_guest_trust_rejects_profile_mismatch(self):
        from release.formal_windows_pretrust import FORMAL_WINDOWS_PRETRUST_PREFIX
        from release.materials import INITIAL_TRUST_KIT_PREFIX

        with tempfile.TemporaryDirectory() as directory:
            scratch = Path(directory)
            source_root = scratch / "source"
            source_root.mkdir()
            initial = create_test_initial_trust_kit(source_root)
            formal = create_test_formal_windows_pretrust_kit(
                scratch / "formal-source",
                source_initial_trust_kit=initial,
            )
            authority_root = scratch / "runtime"
            shutil.copytree(initial, authority_root / INITIAL_TRUST_KIT_PREFIX)
            shutil.copytree(formal, authority_root / FORMAL_WINDOWS_PRETRUST_PREFIX)
            with (
                mock.patch(
                    "release.formal_windows_pretrust.assert_windows_private_acl"
                ),
                self.assertRaisesRegex(
                    InstallerError,
                    "INSTALL_FORMAL_OFFLINE_CAPABILITY_INVALID",
                ),
            ):
                issue_formal_candidate_bound_offline_verifier(
                    authority_root,
                    expected_profile_identity="sha256:" + "0" * 64,
                )

    @staticmethod
    def authority() -> FormalAuthorityRequest:
        return FormalAuthorityRequest(
            repository="yanyuhanyue/AniMemo",
            rc_tag="v1.1.0-rc.19",
            verified_candidate_digest="sha256:" + "0" * 64,
            source_sha="1" * 40,
            source_tree="2" * 40,
            release_manifest_identity="sha256:" + "3" * 64,
            deployment_contract_identity="sha256:" + "4" * 64,
            installer_materials_identity="sha256:" + "5" * 64,
            formal_windows_pretrust_kit_identity="sha256:" + "0" * 64,
            offline_release_trust_profile_identity="sha256:" + "1" * 64,
            api_digest="sha256:" + "6" * 64,
            web_digest="sha256:" + "7" * 64,
            publication_identity="sha256:" + "8" * 64,
            workflow_identity=(
                "yanyuhanyue/AniMemo/.github/workflows/release.yml@refs/heads/main"
            ),
            attestation_claim_identities={
                "api-image": "sha256:" + "9" * 64,
                "web-image": "sha256:" + "a" * 64,
                "release-manifest": "sha256:" + "b" * 64,
                "deployment-contract": "sha256:" + "c" * 64,
                "installer-materials": "sha256:" + "d" * 64,
            },
        )

    @staticmethod
    def output(authority: FormalAuthorityRequest, profile: str) -> dict[str, object]:
        doctor = {
            "overallStatus": "PASS",
            "checks": [{"status": "PASS", "checkId": "release.identity"}],
        }
        tests = [
            {
                "name": name,
                "result": "PASS",
                "receiptDigest": "sha256:" + character * 64,
            }
            for name, character in zip(
                (
                    "application.journal-crud",
                    "service.api.health",
                    "service.web.health",
                ),
                "123",
                strict=True,
            )
        ]
        offline_binding = None
        if profile == "FORMAL_OFFLINE":
            unsigned_execution = {
                "schema": "animemo.release-execution-receipt/v1",
                "publicationIdentity": authority.publication_identity,
                "publicationExecutionReceiptIdentity": "sha256:" + "e" * 64,
                "signedClaimIdentity": "sha256:" + "f" * 64,
                "signedAt": "2026-08-29T23:59:59Z",
            }
            compact = json.dumps(
                unsigned_execution,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            release_execution = {
                **unsigned_execution,
                "identity": "sha256:" + hashlib.sha256(compact).hexdigest(),
            }
            unsigned_binding = {
                "schema": "animemo.offline-release-authority-binding/v1",
                "version": authority.rc_tag,
                "trustProfileVersion": 1,
                "trustProfileIdentity": "sha256:" + "1" * 64,
                "releaseExecutionReceipt": release_execution,
            }
            offline_binding = {
                **unsigned_binding,
                "identity": "sha256:"
                + hashlib.sha256(
                    json.dumps(
                        unsigned_binding,
                        ensure_ascii=False,
                        allow_nan=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest(),
            }
        return {
            "resolvedRelease": {
                "version": authority.rc_tag,
                "sourceSha": authority.source_sha,
                "releaseManifestIdentity": authority.release_manifest_identity,
                "deploymentContractIdentity": authority.deployment_contract_identity,
                "installerMaterialsIdentity": authority.installer_materials_identity,
                "apiDigest": authority.api_digest,
                "webDigest": authority.web_digest,
                "publicationIdentity": authority.publication_identity,
                "workflowIdentity": authority.workflow_identity,
                "attestationClaimIdentities": dict(
                    authority.attestation_claim_identities
                ),
            },
            "transportSource": (
                "local-bundle" if profile == "FORMAL_OFFLINE" else "github"
            ),
            "platformPlanDigest": "sha256:" + "4" * 64,
            "platformReceiptDigest": "sha256:" + "5" * 64,
            "installerPlanDigest": "sha256:" + "6" * 64,
            "installerExecutionReceiptDigest": "sha256:" + "7" * 64,
            "doctorReport": doctor,
            "doctorReceiptDigest": sha256_bytes(canonical_json_bytes(doctor)),
            "canonicalAcceptanceTests": tests,
            "offlineAuthorityBinding": offline_binding,
            "result": "PASS",
        }

    def stage(self, root: Path, authority: FormalAuthorityRequest) -> tuple[Path, str]:
        identity = sha256_bytes(canonical_json_bytes(authority.identity_body()))
        value = {**authority.identity_body(), "identity": identity}
        (root / "formal-rc-authority.json").write_bytes(canonical_json_bytes(value))
        (root / "formal-publication-preflight.json").write_bytes(
            canonical_json_bytes(
                {
                    "schema": "animemo.formal-publication-preflight/v1",
                    "publication_authority_identity": authority.publication_identity,
                    "publication_execution_receipt_identity": ("sha256:" + "e" * 64),
                    "publication_signed_claim_identity": "sha256:" + "f" * 64,
                    "publication_signed_at": "2026-08-29T23:59:59Z",
                    "formal_windows_pretrust_kit_identity": "sha256:" + "0" * 64,
                    "offline_release_trust_profile_identity": "sha256:" + "1" * 64,
                    "pretrusted_profile_identity": "sha256:" + "1" * 64,
                    "provenance_verifier_identity": "sha256:" + "2" * 64,
                    "github_trusted_root_identity": "sha256:" + "3" * 64,
                    "sigstore_trusted_root_identity": "sha256:" + "4" * 64,
                    "release_authority_granted": False,
                    "publish_authorized": False,
                }
            )
        )
        return root, identity

    @staticmethod
    def context(profile: str, identity: str) -> str:
        value = canonical_json_bytes(
            {"profile": profile, "rc_authority_identity": identity}
        )
        return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")

    def test_actual_production_observation_builds_closed_guest_draft(self):
        authority = self.authority()
        with tempfile.TemporaryDirectory() as directory:
            root, identity = self.stage(Path(directory), authority)

            class Executor:
                def execute(self, *, authority, authority_root, profile):
                    self.authority_root = authority_root
                    return FormalProfileRunnerTests.output(authority, profile)

            receipt = execute_profile(
                authority_root=root,
                profile="FORMAL_FRESH",
                context_b64url=self.context("FORMAL_FRESH", identity),
                executor=Executor(),
            )
        self.assertEqual(
            receipt["schema"], "animemo.formal-profile-observation-draft/v1"
        )
        self.assertEqual(receipt["rc_authority_identity"], identity)
        self.assertEqual(receipt["result"], "PASS")
        self.assertEqual(len(receipt["canonical_acceptance_receipt_digests"]), 3)
        self.assertFalse(receipt["release_authority_granted"])
        self.assertFalse(receipt["publish_authorized"])

    def test_controlled_doctor_rejection_emits_closed_fail_observation(self):
        authority = self.authority()
        with tempfile.TemporaryDirectory() as directory:
            root, identity = self.stage(Path(directory), authority)

            class Executor:
                def execute(self, *, authority, authority_root, profile):
                    del authority_root
                    output = FormalProfileRunnerTests.output(authority, profile)
                    output["doctorReport"]["overallStatus"] = "FAIL"
                    output["doctorReport"]["checks"][0]["status"] = "FAIL"
                    output["doctorReceiptDigest"] = sha256_bytes(
                        canonical_json_bytes(output["doctorReport"])
                    )
                    output["canonicalAcceptanceTests"][0]["result"] = "FAIL"
                    output["result"] = "FAIL"
                    return output

            receipt = execute_profile(
                authority_root=root,
                profile="FORMAL_FRESH",
                context_b64url=self.context("FORMAL_FRESH", identity),
                executor=Executor(),
            )
        self.assertEqual(receipt["result"], "FAIL")
        self.assertEqual(len(receipt["canonical_acceptance_receipt_digests"]), 3)

    def test_controlled_install_rejection_without_doctor_emits_fail_observation(self):
        authority = self.authority()
        with tempfile.TemporaryDirectory() as directory:
            root, identity = self.stage(Path(directory), authority)

            class Executor:
                def execute(self, *, authority, authority_root, profile):
                    del authority_root
                    output = FormalProfileRunnerTests.output(authority, profile)
                    output["doctorReport"] = None
                    output["doctorReceiptDigest"] = None
                    output["canonicalAcceptanceTests"] = []
                    output["result"] = "FAIL"
                    return output

            receipt = execute_profile(
                authority_root=root,
                profile="FORMAL_FRESH",
                context_b64url=self.context("FORMAL_FRESH", identity),
                executor=Executor(),
            )
        self.assertEqual(receipt["result"], "FAIL")
        self.assertIsNone(receipt["doctor_receipt_digest"])
        self.assertEqual(receipt["canonical_acceptance_receipt_digests"], [])

    def test_executor_crash_remains_error_not_controlled_fail(self):
        authority = self.authority()
        with tempfile.TemporaryDirectory() as directory:
            root, identity = self.stage(Path(directory), authority)

            class Executor:
                def execute(self, **_kwargs):
                    raise OSError("fixture crash")

            with self.assertRaisesRegex(
                FormalProfileRunnerError,
                "FORMAL_PROFILE_EXECUTION_FAILED",
            ):
                execute_profile(
                    authority_root=root,
                    profile="FORMAL_FRESH",
                    context_b64url=self.context("FORMAL_FRESH", identity),
                    executor=Executor(),
                )

    def test_plain_pass_input_cannot_replace_actual_observation(self):
        authority = self.authority()
        with tempfile.TemporaryDirectory() as directory:
            root, identity = self.stage(Path(directory), authority)

            class Executor:
                def execute(self, **_kwargs):
                    return {"result": "PASS"}

            with self.assertRaisesRegex(
                FormalProfileRunnerError,
                "FORMAL_PROFILE_PRODUCTION_OBSERVATION_INVALID",
            ):
                execute_profile(
                    authority_root=root,
                    profile="FORMAL_FRESH",
                    context_b64url=self.context("FORMAL_FRESH", identity),
                    executor=Executor(),
                )

    def test_wrong_release_or_transport_is_rejected(self):
        authority = self.authority()
        with tempfile.TemporaryDirectory() as directory:
            root, identity = self.stage(Path(directory), authority)

            class Executor:
                def execute(self, *, authority, profile, **_kwargs):
                    output = FormalProfileRunnerTests.output(authority, profile)
                    output["transportSource"] = "official-mirror"
                    return output

            with self.assertRaisesRegex(
                FormalProfileRunnerError,
                "FORMAL_PROFILE_PRODUCTION_OBSERVATION_MISMATCH",
            ):
                execute_profile(
                    authority_root=root,
                    profile="FORMAL_FRESH",
                    context_b64url=self.context("FORMAL_FRESH", identity),
                    executor=Executor(),
                )

    def test_non_object_doctor_check_is_rejected(self):
        authority = self.authority()
        with tempfile.TemporaryDirectory() as directory:
            root, identity = self.stage(Path(directory), authority)

            class Executor:
                def execute(self, *, authority, profile, **_kwargs):
                    output = FormalProfileRunnerTests.output(authority, profile)
                    output["doctorReport"]["checks"].append("garbage")
                    output["doctorReceiptDigest"] = sha256_bytes(
                        canonical_json_bytes(output["doctorReport"])
                    )
                    return output

            with self.assertRaisesRegex(
                FormalProfileRunnerError,
                "FORMAL_PROFILE_PRODUCTION_OBSERVATION_INVALID",
            ):
                execute_profile(
                    authority_root=root,
                    profile="FORMAL_FRESH",
                    context_b64url=self.context("FORMAL_FRESH", identity),
                    executor=Executor(),
                )

    def test_offline_release_execution_must_match_host_publication(self):
        authority = self.authority()
        with tempfile.TemporaryDirectory() as directory:
            root, identity = self.stage(Path(directory), authority)

            class Executor:
                def execute(self, *, authority, profile, **_kwargs):
                    output = FormalProfileRunnerTests.output(authority, profile)
                    output["offlineAuthorityBinding"]["releaseExecutionReceipt"][
                        "signedClaimIdentity"
                    ] = "sha256:" + "0" * 64
                    return output

            with self.assertRaisesRegex(
                FormalProfileRunnerError,
                "FORMAL_PROFILE_PRODUCTION_OBSERVATION_INVALID",
            ):
                execute_profile(
                    authority_root=root,
                    profile="FORMAL_OFFLINE",
                    context_b64url=self.context("FORMAL_OFFLINE", identity),
                    executor=Executor(),
                )

    def test_offline_resolver_trust_profile_must_match_host_pretrust(self):
        authority = self.authority()
        with tempfile.TemporaryDirectory() as directory:
            root, identity = self.stage(Path(directory), authority)

            class Executor:
                def execute(self, *, authority, profile, **_kwargs):
                    output = FormalProfileRunnerTests.output(authority, profile)
                    binding = output["offlineAuthorityBinding"]
                    binding["trustProfileIdentity"] = "sha256:" + "0" * 64
                    unsigned = dict(binding)
                    unsigned.pop("identity")
                    binding["identity"] = (
                        "sha256:"
                        + hashlib.sha256(
                            json.dumps(
                                unsigned,
                                ensure_ascii=False,
                                allow_nan=False,
                                sort_keys=True,
                                separators=(",", ":"),
                            ).encode("utf-8")
                        ).hexdigest()
                    )
                    return output

            with self.assertRaisesRegex(
                FormalProfileRunnerError,
                "FORMAL_PROFILE_PRODUCTION_OBSERVATION_INVALID",
            ):
                execute_profile(
                    authority_root=root,
                    profile="FORMAL_OFFLINE",
                    context_b64url=self.context("FORMAL_OFFLINE", identity),
                    executor=Executor(),
                )


if __name__ == "__main__":
    unittest.main()
