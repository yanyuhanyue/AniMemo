from __future__ import annotations

import hashlib
import json
import os
import pickle
import tempfile
import unittest
from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from jsonschema import Draft202012Validator, ValidationError
from jsonschema import validate as validate_schema

import release.formal_vm_controller as formal_provenance
from release.candidate import canonical_json_bytes
from release.formal_acceptance_test_support import build_test_formal_acceptance
from release.formal_vm_controller import (
    CANDIDATE_PROFILE_RESULT_KEYS,
    CandidateFormalContinuation,
    FormalAuthorityRequest,
    FormalExecutionContext,
    FormalProducerError,
    FormalProfileExecutionError,
    FormalProfileObservation,
    FormalProvenanceInput,
    FormalProvenancePlan,
    FormalVmController,
    OfflineActionsProvenancePreflight,
    ProductionFormalAuthorityVerifier,
    ProvenancePreflightError,
    QualifiedCandidateFormalAuthority,
    VerifiedFormalRcAuthority,
    _issue_qualified_candidate_formal_authority,
    close_qualified_candidate_for_formal,
    execute_candidate_controller_for_formal,
    validate_formal_acceptance_bundle,
)


def _candidate_source_evidence() -> dict[str, object]:
    original_vm_hashes = {"source.vmx": "sha256:" + "c" * 64}
    return {
        "candidate_plan_digest": "sha256:" + "a" * 64,
        "candidate_provider_execution_authority_receipt_digest": (
            "sha256:" + "9" * 64
        ),
        "candidate_base_vm_identity": formal_provenance.sha256_bytes(
            canonical_json_bytes(original_vm_hashes)
        ),
        "candidate_original_vm_hashes": original_vm_hashes,
        "candidate_snapshot_identities": {
            "FRESH_BASE": "sha256:" + "d" * 64,
            "DOCKER_BASE": "sha256:" + "e" * 64,
            "RUNTIME_BASE_OFFLINE": "sha256:" + "f" * 64,
        },
        "candidate_source_disk_graph_identity": "sha256:" + "1" * 64,
        "candidate_snapshot_disk_graph_identities": {
            "FRESH_BASE": "sha256:" + "2" * 64,
            "DOCKER_BASE": "sha256:" + "3" * 64,
            "RUNTIME_BASE_OFFLINE": "sha256:" + "4" * 64,
        },
        "candidate_source_vm_inventory_identity": "sha256:" + "5" * 64,
    }


def _qualified_source_evidence() -> dict[str, object]:
    evidence = _candidate_source_evidence()
    return {
        "candidate_plan_digest": evidence["candidate_plan_digest"],
        "candidate_provider_execution_authority_receipt_digest": evidence[
            "candidate_provider_execution_authority_receipt_digest"
        ],
        "base_vm_identity": evidence["candidate_base_vm_identity"],
        "original_vm_hashes": evidence["candidate_original_vm_hashes"],
        "snapshot_identities": evidence["candidate_snapshot_identities"],
        "source_disk_graph_identity": evidence[
            "candidate_source_disk_graph_identity"
        ],
        "snapshot_disk_graph_identities": evidence[
            "candidate_snapshot_disk_graph_identities"
        ],
        "source_vm_inventory_identity": evidence[
            "candidate_source_vm_inventory_identity"
        ],
    }


class FormalVmControllerTests(unittest.TestCase):
    def test_production_verifier_runner_closes_cwd_path_and_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            executable = root / (
                "formal-verifier.exe" if os.name == "nt" else "formal-verifier"
            )
            executable.write_bytes(b"closed verifier fixture")
            executable.chmod(0o700)
            command = (str(executable), "--mode", "github-release")
            completed = SimpleNamespace(returncode=0, stdout=b"{}\n", stderr=b"")
            with mock.patch.object(
                formal_provenance.subprocess,
                "run",
                return_value=completed,
            ) as run:
                self.assertEqual(
                    formal_provenance._production_verifier_runner(command),
                    b"{}\n",
                )
            kwargs = run.call_args.kwargs
            self.assertEqual(kwargs["cwd"], executable.parent)
            self.assertIs(kwargs["stdin"], formal_provenance.subprocess.DEVNULL)
            self.assertFalse(kwargs["shell"])
            self.assertEqual(set(kwargs["env"]), {"PATH", "LANG", "LC_ALL"})
            path_entries = kwargs["env"]["PATH"].split(os.pathsep)
            self.assertEqual(path_entries[0], str(executable.parent))
            self.assertNotIn(".", path_entries)
            self.assertNotIn(r"C:\bin", path_entries)
            self.assertNotEqual(kwargs["env"]["PATH"], os.defpath)

    def test_qualified_candidate_authority_cannot_be_constructed_or_pickled(self):
        with self.assertRaises(TypeError):
            QualifiedCandidateFormalAuthority()

    def test_qualified_candidate_owns_and_releases_held_material_authority(self):
        from scripts.candidate_vm_harness import HeldCandidateMaterialAuthority

        with tempfile.TemporaryDirectory() as directory:
            loaded = SimpleNamespace(root=Path(directory) / "candidate")
            stack = mock.Mock()
            material = object.__new__(HeldCandidateMaterialAuthority)
            material._closed = False
            material._identity = "sha256:" + "1" * 64
            material._loaded = loaded
            material._root = Path(directory) / "already-removed-private-root"
            material._stack = stack
            material._tree_inventory_identity = "sha256:" + "2" * 64
            qualified = _issue_qualified_candidate_formal_authority(
                loaded=loaded,
                candidate_material_authority=material,
                candidate_aggregate_receipt_digest="sha256:" + "6" * 64,
                candidate_profile_receipt_digests={
                    key: "sha256:" + value * 64
                    for key, value in zip(
                        CANDIDATE_PROFILE_RESULT_KEYS, "789", strict=True
                    )
                },
                **_qualified_source_evidence(),
                formal_windows_pretrust_root=Path(directory),
            )
            self.assertIs(qualified.loaded, loaded)
            qualified.close()
            qualified.close()
            self.assertTrue(material._closed)
            stack.close.assert_called_once_with()
            with self.assertRaisesRegex(
                FormalProducerError, "FORMAL_QUALIFIED_CANDIDATE_INVALID"
            ):
                _ = qualified.loaded

    def test_candidate_continuation_is_opaque_one_use_and_not_json_input(self):
        with self.assertRaisesRegex(
            FormalProducerError, "FORMAL_CANDIDATE_CONTINUATION_INVALID"
        ):
            close_qualified_candidate_for_formal(
                "sha256:" + "1" * 64, {"status": "PASS"}
            )
        plan = SimpleNamespace(
            plan_digest="sha256:" + "1" * 64,
            as_dict=lambda: {"planDigest": "sha256:" + "1" * 64},
        )

        class Provider:
            def execution_authority(self):
                return nullcontext(SimpleNamespace(result="PASS"))

            def inspect_execution_authority(self):
                return SimpleNamespace(result="PASS")

        provider = Provider()
        material_authority = SimpleNamespace(
            loaded=SimpleNamespace(root=Path("private-candidate") / "digest"),
            close=mock.Mock(),
        )
        with (
            mock.patch(
                "scripts.candidate_vm_harness.ClosedVmwareProvider", Provider
            ),
            mock.patch(
                "scripts.candidate_vm_harness.acquire_candidate_material_authority",
                return_value=material_authority,
            ) as acquire_material,
            mock.patch(
                "scripts.candidate_vm_harness.build_harness_plan",
                return_value=plan,
            ) as build_plan,
            mock.patch(
                "scripts.candidate_vm_harness.execute_harness_plan",
                return_value={"status": "PASS"},
            ) as execute_plan,
        ):
            capability = execute_candidate_controller_for_formal(
                verified_candidate_digest="sha256:" + "1" * 64,
                expected_qualification_run_id=1,
                expected_source_sha="1" * 40,
                expected_source_tree="2" * 40,
                provider=provider,
                authorize_plan=lambda value: value["planDigest"],
            )
        acquire_material.assert_called_once()
        self.assertEqual(
            build_plan.call_args.kwargs["_state_root"],
            Path("private-candidate"),
        )
        self.assertIs(
            build_plan.call_args.kwargs["_candidate_material_authority"],
            material_authority,
        )
        self.assertEqual(
            execute_plan.call_args.kwargs["_state_root"],
            Path("private-candidate"),
        )
        self.assertIs(
            execute_plan.call_args.kwargs["_candidate_material_authority"],
            material_authority,
        )
        self.assertIsInstance(capability, CandidateFormalContinuation)
        with self.assertRaises(TypeError):
            pickle.dumps(capability)
        with self.assertRaisesRegex(
            FormalProducerError, "FORMAL_CANDIDATE_PLAN_INVALID"
        ):
            close_qualified_candidate_for_formal("sha256:" + "1" * 64, capability)
        with self.assertRaisesRegex(
            FormalProducerError, "FORMAL_CANDIDATE_CONTINUATION_INVALID"
        ):
            close_qualified_candidate_for_formal("sha256:" + "1" * 64, capability)

    def test_wave_c_controller_adds_producer_without_publication_authority(self):
        self.assertTrue(hasattr(formal_provenance, "FormalVmController"))
        self.assertFalse(
            hasattr(formal_provenance, "ProvenanceAuthorizedCloneCapability")
        )
        self.assertFalse(hasattr(formal_provenance, "execute_production_formal_vm"))

    def test_mismatched_external_pretrust_kit_runs_no_verifier(self):
        request = self._authority_request()
        calls: list[tuple[str, ...]] = []
        profile = SimpleNamespace(
            identity="sha256:" + "a" * 64,
            source_profile_identity=(request.offline_release_trust_profile_identity),
            verifier_identity="sha256:" + "b" * 64,
            linux_guest_verifier_identity="sha256:" + "c" * 64,
            github_trusted_root_sha256="sha256:" + "d" * 64,
            sigstore_trusted_root_sha256="sha256:" + "e" * 64,
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            material = SimpleNamespace(
                root=root,
                identity="sha256:" + "f" * 64,
                profile=profile,
                verifier_path=root / "formal-release-verifier.exe",
                github_trusted_root_path=root / "github-trusted-root.jsonl",
                sigstore_trusted_root_path=root / "sigstore-trusted-root.jsonl",
            )
            binding = SimpleNamespace(
                installer_materials_sha256=(request.installer_materials_identity),
                kit_identity="sha256:" + "9" * 64,
                profile_identity=profile.identity,
                source_profile_identity=profile.source_profile_identity,
                windows_host_verifier_identity=profile.verifier_identity,
                linux_guest_verifier_identity=(profile.linux_guest_verifier_identity),
                github_trusted_root_sha256=(profile.github_trusted_root_sha256),
                sigstore_trusted_root_sha256=(profile.sigstore_trusted_root_sha256),
            )
            candidate = {
                "repository": request.repository,
                "candidate_version": request.rc_tag,
                "source_sha": request.source_sha,
                "source_tree": request.source_tree,
                "release_manifest_sha256": request.release_manifest_identity,
                "deployment_contract_sha256": (request.deployment_contract_identity),
                "installer_materials_sha256": (request.installer_materials_identity),
                "api_oci_digest": request.api_digest,
                "web_oci_digest": request.web_digest,
            }
            qualified = _issue_qualified_candidate_formal_authority(
                loaded=SimpleNamespace(
                    root=root,
                    verified_digest=request.verified_candidate_digest,
                    candidate_input=candidate,
                ),
                candidate_aggregate_receipt_digest=("sha256:" + "6" * 64),
                candidate_profile_receipt_digests={
                    key: "sha256:" + value * 64
                    for key, value in zip(
                        CANDIDATE_PROFILE_RESULT_KEYS, "789", strict=True
                    )
                },
                **_qualified_source_evidence(),
                formal_windows_pretrust_root=root,
            )
            with (
                mock.patch.object(
                    formal_provenance.FormalWindowsPretrustedTrustMaterial,
                    "load",
                    return_value=material,
                ),
                mock.patch.object(
                    formal_provenance,
                    "assert_windows_private_acl",
                ),
                mock.patch.object(
                    formal_provenance,
                    "hold_windows_private_path_chain",
                    side_effect=lambda *_args, **_kwargs: nullcontext(root),
                ),
                mock.patch.object(
                    formal_provenance,
                    "hold_windows_private_snapshot",
                    side_effect=lambda *_args, **_kwargs: nullcontext(root),
                ),
                mock.patch.object(
                    formal_provenance,
                    "inspect_formal_windows_pretrust_in_installer_materials",
                    return_value=binding,
                ),
            ):
                verifier = ProductionFormalAuthorityVerifier(
                    FormalProvenancePlan(
                        verifier=None,
                        inputs=(),
                        private_work_root=root,
                        qualified_candidate=qualified,
                    ),
                    runner=lambda command: calls.append(command) or b"{}\n",
                )
                with self.assertRaisesRegex(
                    FormalProducerError,
                    "FORMAL_PRETRUSTED_MATERIAL_AUTHORITY_MISMATCH",
                ):
                    verifier.verify(request)
        self.assertEqual(calls, [])

    @staticmethod
    def _authority_request() -> FormalAuthorityRequest:
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
    def _execution_context() -> FormalExecutionContext:
        return FormalExecutionContext(
            accepted_at="2026-08-30T01:02:03Z",
            observed_at="2026-08-30T01:01:59Z",
            operator_identity="formal-reviewer",
            run_id="formal-run-1",
            run_attempt=1,
            correlation_id="formal-correlation-1",
            current_workflow_commit="e" * 40,
            execution_environment="windows-vmware-private",
            tool_identity="sha256:" + "f" * 64,
        )

    @staticmethod
    def _provenance_summaries(
        request: FormalAuthorityRequest,
    ) -> dict[str, dict[str, str]]:
        return {
            name: {
                "claim_digest": digest,
                "bundle_digest": "sha256:" + "d" * 64,
                "trusted_root_digest": "sha256:" + "e" * 64,
                "request_digest": "sha256:" + "f" * 64,
            }
            for name, digest in request.attestation_claim_identities.items()
        }

    @staticmethod
    def _combined_preflight_digest(request: FormalAuthorityRequest) -> str:
        publication_summary = FormalVmControllerTests._publication_summary()
        return (
            "sha256:"
            + hashlib.sha256(
                canonical_json_bytes(
                    {
                        "schema": "animemo.formal-production-provenance/v1",
                        "actions_preflight_digest": "sha256:" + "d" * 64,
                        "publication_authority_identity": request.publication_identity,
                        "publication_execution_receipt_identity": "sha256:" + "b" * 64,
                        "publication_signed_claim_identity": "sha256:" + "c" * 64,
                        "publication_preflight": publication_summary,
                        "formal_windows_pretrust_kit_identity": (
                            request.formal_windows_pretrust_kit_identity
                        ),
                        "offline_release_trust_profile_identity": (
                            request.offline_release_trust_profile_identity
                        ),
                        "pretrusted_profile_identity": "sha256:" + "5" * 64,
                        "provenance_verifier_identity": "sha256:" + "8" * 64,
                        "github_trusted_root_identity": "sha256:" + "a" * 64,
                        "sigstore_trusted_root_identity": "sha256:" + "e" * 64,
                        "release_authority_granted": False,
                        "publish_authorized": False,
                    }
                )
            ).hexdigest()
        )

    @staticmethod
    def _publication_summary() -> dict[str, str]:
        return {
            "verifier_digest": "sha256:" + "8" * 64,
            "bundle_digest": "sha256:" + "9" * 64,
            "trusted_root_digest": "sha256:" + "a" * 64,
            "request_digest": "sha256:" + "b" * 64,
            "claim_digest": "sha256:" + "c" * 64,
        }

    @staticmethod
    def _candidate_evidence() -> dict[str, object]:
        return {
            "candidate_aggregate_receipt_digest": "sha256:" + "6" * 64,
            "candidate_profile_receipt_digests": {
                key: "sha256:" + value * 64
                for key, value in zip(CANDIDATE_PROFILE_RESULT_KEYS, "789", strict=True)
            },
            **_candidate_source_evidence(),
        }

    @staticmethod
    def _observation(
        authority: VerifiedFormalRcAuthority,
        profile: str,
    ) -> FormalProfileObservation:
        transport = "local-bundle" if profile == "FORMAL_OFFLINE" else "github"
        profile_hex = {
            "FORMAL_FRESH": "8",
            "FORMAL_DOCKER": "9",
            "FORMAL_OFFLINE": "a",
        }[profile]
        return FormalProfileObservation(
            profile=profile,
            rc_authority_identity=authority.identity,
            transport_source=transport,
            resolved_version=authority.rc_tag,
            resolved_source_sha=authority.source_sha,
            resolved_manifest_identity=authority.release_manifest_identity,
            resolved_deployment_contract_identity=(
                authority.deployment_contract_identity
            ),
            resolved_installer_materials_identity=(
                authority.installer_materials_identity
            ),
            resolved_api_digest=authority.api_digest,
            resolved_web_digest=authority.web_digest,
            resolved_publication_identity=authority.publication_identity,
            resolved_workflow_identity=authority.workflow_identity,
            resolved_attestation_claim_identities=(
                authority.attestation_claim_identities
            ),
            base_vm_identity="sha256:" + "0" * 64,
            snapshot_identity="sha256:" + profile_hex * 64,
            clone_identity="sha256:" + profile_hex * 64,
            provider_execution_authority_receipt_digest=(
                "sha256:" + "1" * 64
            ),
            publication_execution_receipt_identity=(
                authority.publication_execution_receipt_identity
            ),
            publication_signed_claim_identity=(
                authority.publication_signed_claim_identity
            ),
            publication_signed_at=authority.publication_signed_at,
            formal_windows_pretrust_kit_identity=(
                authority.formal_windows_pretrust_kit_identity
            ),
            offline_release_trust_profile_identity=(
                authority.offline_release_trust_profile_identity
            ),
            pretrusted_profile_identity=authority.pretrusted_profile_identity,
            provenance_verifier_identity=authority.provenance_verifier_identity,
            github_trusted_root_identity=authority.github_trusted_root_identity,
            sigstore_trusted_root_identity=(authority.sigstore_trusted_root_identity),
            platform_plan_digest="sha256:" + "b" * 64,
            platform_receipt_digest="sha256:" + "c" * 64,
            installer_plan_digest="sha256:" + "1" * 64,
            installer_execution_receipt_digest="sha256:" + "2" * 64,
            doctor_receipt_digest="sha256:" + "3" * 64,
            canonical_acceptance_receipt_digests=(
                "sha256:" + "4" * 64,
                "sha256:" + "5" * 64,
                "sha256:" + "6" * 64,
            ),
            continuation_receipt_digest="sha256:" + "7" * 64,
            result="PASS",
        )

    def test_formal_controller_rejects_provenance_before_any_profile_execution(self):
        events: list[str] = []

        class RejectingVerifier:
            def verify(self, request):
                del request
                events.append("provenance")
                raise ProvenancePreflightError("FORMAL_PROVENANCE_VERIFICATION_FAILED")

        class Executor:
            def execute(self, *, authority, profile):
                del authority, profile
                events.append("profile")
                raise AssertionError("profile execution must remain unreachable")

        with self.assertRaisesRegex(
            ProvenancePreflightError,
            "FORMAL_PROVENANCE_VERIFICATION_FAILED",
        ):
            FormalVmController(
                authority_verifier=RejectingVerifier(),
                profile_executor=Executor(),
            ).execute(self._authority_request(), self._execution_context())
        self.assertEqual(events, ["provenance"])

    def test_formal_controller_runs_all_profiles_and_emits_schema_valid_receipts(self):
        request = self._authority_request()
        authority = VerifiedFormalRcAuthority.issue(
            request,
            provenance_preflight_digest=self._combined_preflight_digest(request),
            actions_preflight_receipt_digest="sha256:" + "d" * 64,
            provenance_claim_summaries=self._provenance_summaries(request),
            publication_preflight_summary=self._publication_summary(),
            pretrusted_profile_identity="sha256:" + "5" * 64,
            provenance_verifier_identity="sha256:" + "8" * 64,
            github_trusted_root_identity="sha256:" + "a" * 64,
            sigstore_trusted_root_identity="sha256:" + "e" * 64,
            publication_execution_receipt_identity="sha256:" + "b" * 64,
            publication_signed_claim_identity="sha256:" + "c" * 64,
            publication_signed_at="2026-08-29T23:59:59Z",
            candidate_aggregate_receipt_digest="sha256:" + "6" * 64,
            candidate_profile_receipt_digests={
                key: "sha256:" + value * 64
                for key, value in zip(CANDIDATE_PROFILE_RESULT_KEYS, "789", strict=True)
            },
            **_candidate_source_evidence(),
        )
        events: list[str] = []

        class Verifier:
            def verify(self, observed_request):
                self.assert_request = observed_request
                events.append("provenance")
                return authority

        class Executor:
            def execute(self, *, authority: VerifiedFormalRcAuthority, profile: str):
                events.append(profile)
                return FormalVmControllerTests._observation(authority, profile)

        result = FormalVmController(
            authority_verifier=Verifier(),
            profile_executor=Executor(),
        ).execute(request, self._execution_context())

        self.assertEqual(
            events,
            ["provenance", "FORMAL_FRESH", "FORMAL_DOCKER", "FORMAL_OFFLINE"],
        )
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(
            set(result["profileReceipts"]),
            {"FORMAL_FRESH", "FORMAL_DOCKER", "FORMAL_OFFLINE"},
        )
        self.assertIsNotNone(result["rcLiveAcceptanceInput"])
        self.assertFalse(result["aggregateReceipt"]["release_authority_granted"])
        self.assertFalse(result["aggregateReceipt"]["publish_authorized"])
        bundle = validate_formal_acceptance_bundle(
            {
                "rcLiveAcceptanceInput": result["rcLiveAcceptanceInput"],
                "profileReceipts": result["profileReceipts"],
                "aggregateReceipt": result["aggregateReceipt"],
                "executionReceipt": result["executionReceipt"],
            }
        )
        self.assertEqual(
            set(bundle),
            {
                "rcLiveAcceptanceInput",
                "profileReceipts",
                "aggregateReceipt",
                "executionReceipt",
            },
        )
        for name, receipt in result["profileReceipts"].items():
            schema = json.loads(
                Path("release/formal-profile-receipt.schema.json").read_text(
                    encoding="utf-8"
                )
            )
            validate_schema(receipt, schema)
            self.assertEqual(receipt["profile"], name)
        validate_schema(
            result["aggregateReceipt"],
            json.loads(
                Path("release/formal-acceptance-receipt.schema.json").read_text(
                    encoding="utf-8"
                )
            ),
        )

    def test_standalone_and_embedded_formal_pass_schemas_have_exact_parity(self):
        record = build_test_formal_acceptance(
            rc_tag="v1.1.0-rc.19",
            rc_commit="1" * 40,
            rc_tree="2" * 40,
            release_manifest_identity="sha256:" + "3" * 64,
            deployment_contract_identity="sha256:" + "4" * 64,
            installer_materials_identity="sha256:" + "5" * 64,
            api_digest="sha256:" + "6" * 64,
            web_digest="sha256:" + "7" * 64,
            fresh_base_identity="sha256:" + "8" * 64,
            docker_base_identity="sha256:" + "9" * 64,
            runtime_base_identity="sha256:" + "a" * 64,
            accepted_at="2026-08-30T01:02:03Z",
            observed_at="2026-08-30T01:01:59Z",
            operator_identity="formal-schema-parity",
        )
        evidence = record["formal_evidence"]
        main_schema = json.loads(
            Path("release/rc-live-acceptance.schema.json").read_text(
                encoding="utf-8"
            )
        )
        surfaces = (
            (
                "release/formal-rc-live-acceptance-input.schema.json",
                "formalAcceptanceInput",
                evidence["rcLiveAcceptanceInput"],
            ),
            (
                "release/formal-profile-receipt.schema.json",
                "formalProfileReceipt",
                evidence["profileReceipts"]["FORMAL_FRESH"],
            ),
            (
                "release/formal-acceptance-receipt.schema.json",
                "formalAggregateReceipt",
                evidence["aggregateReceipt"],
            ),
            (
                "release/formal-execution-receipt.schema.json",
                "formalExecutionReceipt",
                evidence["executionReceipt"],
            ),
        )
        for path, definition, sample in surfaces:
            with self.subTest(definition=definition):
                standalone = json.loads(Path(path).read_text(encoding="utf-8"))
                embedded = main_schema["$defs"][definition]
                embedded_schema = {
                    "$schema": main_schema["$schema"],
                    "$ref": f"#/$defs/{definition}",
                    "$defs": main_schema["$defs"],
                }
                self.assertEqual(
                    set(standalone["required"]), set(embedded["required"])
                )
                self.assertEqual(
                    set(standalone["properties"]), set(embedded["properties"])
                )
                self.assertEqual(
                    standalone["additionalProperties"],
                    embedded["additionalProperties"],
                )
                validate_schema(sample, standalone)
                validate_schema(sample, embedded_schema)
                standalone_validator = Draft202012Validator(standalone)
                embedded_validator = Draft202012Validator(embedded_schema)

                def object_paths(
                    value: object,
                    prefix: tuple[object, ...] = (),
                ):
                    if type(value) is dict:
                        for key, child in value.items():
                            path = (*prefix, key)
                            yield path
                            yield from object_paths(child, path)
                    elif type(value) is list:
                        for index, child in enumerate(value):
                            yield from object_paths(child, (*prefix, index))

                def remove_path(value: object, path: tuple[object, ...]):
                    mutated = json.loads(json.dumps(value))
                    parent = mutated
                    for segment in path[:-1]:
                        parent = parent[segment]
                    del parent[path[-1]]
                    return mutated

                def replace_path(
                    value: object,
                    path: tuple[object, ...],
                    replacement: object,
                ):
                    mutated = json.loads(json.dumps(value))
                    parent = mutated
                    for segment in path[:-1]:
                        parent = parent[segment]
                    parent[path[-1]] = replacement
                    return mutated

                for path_to_value in object_paths(sample):
                    removed = remove_path(sample, path_to_value)
                    self.assertEqual(
                        standalone_validator.is_valid(removed),
                        embedded_validator.is_valid(removed),
                        (definition, "remove", path_to_value),
                    )
                    replaced = replace_path(sample, path_to_value, None)
                    self.assertEqual(
                        standalone_validator.is_valid(replaced),
                        embedded_validator.is_valid(replaced),
                        (definition, "replace", path_to_value),
                    )
                for field in standalone["required"]:
                    missing = json.loads(json.dumps(sample))
                    missing.pop(field)
                    with self.assertRaises(ValidationError):
                        validate_schema(missing, standalone)
                    with self.assertRaises(ValidationError):
                        validate_schema(missing, embedded_schema)
                identity_fields = {
                    field
                    for field in standalone["required"]
                    if "identity" in field
                    or "source" in field
                    or "candidate" in field
                }
                for field in identity_fields:
                    invalid = json.loads(json.dumps(sample))
                    invalid[field] = None
                    with self.assertRaises(ValidationError):
                        validate_schema(invalid, standalone)
                    with self.assertRaises(ValidationError):
                        validate_schema(invalid, embedded_schema)

    def test_formal_bundle_rejects_self_asserted_profile_digest(self):
        request = self._authority_request()
        authority = VerifiedFormalRcAuthority.issue(
            request,
            provenance_preflight_digest=self._combined_preflight_digest(request),
            actions_preflight_receipt_digest="sha256:" + "d" * 64,
            provenance_claim_summaries=self._provenance_summaries(request),
            publication_preflight_summary=self._publication_summary(),
            pretrusted_profile_identity="sha256:" + "5" * 64,
            provenance_verifier_identity="sha256:" + "8" * 64,
            github_trusted_root_identity="sha256:" + "a" * 64,
            sigstore_trusted_root_identity="sha256:" + "e" * 64,
            publication_execution_receipt_identity="sha256:" + "b" * 64,
            publication_signed_claim_identity="sha256:" + "c" * 64,
            publication_signed_at="2026-08-29T23:59:59Z",
            **self._candidate_evidence(),
        )

        class Verifier:
            def verify(self, _request):
                return authority

        class Executor:
            def execute(self, *, authority, profile):
                return FormalVmControllerTests._observation(authority, profile)

        result = FormalVmController(
            authority_verifier=Verifier(), profile_executor=Executor()
        ).execute(request, self._execution_context())
        tampered = json.loads(json.dumps(result["rcLiveAcceptanceInput"]))
        tampered["formal_profile_receipt_digests"]["formal_fresh"] = (
            "sha256:" + "0" * 64
        )
        unsigned = dict(tampered)
        unsigned.pop("record_input_digest")
        tampered["record_input_digest"] = (
            "sha256:" + hashlib.sha256(canonical_json_bytes(unsigned)).hexdigest()
        )
        with self.assertRaisesRegex(
            formal_provenance.FormalProducerError,
            "FORMAL_ACCEPTANCE_BUNDLE_BINDING_MISMATCH",
        ):
            validate_formal_acceptance_bundle(
                {
                    "rcLiveAcceptanceInput": tampered,
                    "profileReceipts": result["profileReceipts"],
                    "aggregateReceipt": result["aggregateReceipt"],
                    "executionReceipt": result["executionReceipt"],
                }
            )
        validate_schema(
            result["executionReceipt"],
            json.loads(
                Path("release/formal-execution-receipt.schema.json").read_text(
                    encoding="utf-8"
                )
            ),
        )

    def test_formal_bundle_rejects_mixed_input_and_profile_authorities(self):
        request = self._authority_request()
        authority = VerifiedFormalRcAuthority.issue(
            request,
            provenance_preflight_digest=self._combined_preflight_digest(request),
            actions_preflight_receipt_digest="sha256:" + "d" * 64,
            provenance_claim_summaries=self._provenance_summaries(request),
            publication_preflight_summary=self._publication_summary(),
            pretrusted_profile_identity="sha256:" + "5" * 64,
            provenance_verifier_identity="sha256:" + "8" * 64,
            github_trusted_root_identity="sha256:" + "a" * 64,
            sigstore_trusted_root_identity="sha256:" + "e" * 64,
            publication_execution_receipt_identity="sha256:" + "b" * 64,
            publication_signed_claim_identity="sha256:" + "c" * 64,
            publication_signed_at="2026-08-29T23:59:59Z",
            **self._candidate_evidence(),
        )

        class Verifier:
            def verify(self, _request):
                return authority

        class Executor:
            def execute(self, *, authority, profile):
                return FormalVmControllerTests._observation(authority, profile)

        result = FormalVmController(
            authority_verifier=Verifier(), profile_executor=Executor()
        ).execute(request, self._execution_context())
        mixed_input = json.loads(json.dumps(result["rcLiveAcceptanceInput"]))
        mixed_input["source_sha"] = "f" * 40
        replacement = FormalAuthorityRequest(
            repository=mixed_input["repository"],
            rc_tag=mixed_input["rc_tag"],
            verified_candidate_digest=mixed_input["verified_candidate_digest"],
            source_sha=mixed_input["source_sha"],
            source_tree=mixed_input["source_tree"],
            release_manifest_identity=mixed_input["release_manifest_identity"],
            deployment_contract_identity=mixed_input["deployment_contract_identity"],
            installer_materials_identity=mixed_input["installer_materials_identity"],
            formal_windows_pretrust_kit_identity=mixed_input[
                "formal_windows_pretrust_kit_identity"
            ],
            offline_release_trust_profile_identity=mixed_input[
                "offline_release_trust_profile_identity"
            ],
            api_digest=mixed_input["api_digest"],
            web_digest=mixed_input["web_digest"],
            publication_identity=mixed_input["publication_identity"],
            workflow_identity=mixed_input["workflow_identity"],
            attestation_claim_identities=mixed_input["attestation_claim_identities"],
        )
        mixed_input["rc_authority_identity"] = (
            "sha256:"
            + hashlib.sha256(
                canonical_json_bytes(replacement.identity_body())
            ).hexdigest()
        )
        unsigned = dict(mixed_input)
        unsigned.pop("record_input_digest")
        mixed_input["record_input_digest"] = (
            "sha256:" + hashlib.sha256(canonical_json_bytes(unsigned)).hexdigest()
        )
        with self.assertRaisesRegex(
            formal_provenance.FormalProducerError,
            "FORMAL_ACCEPTANCE_BUNDLE_BINDING_MISMATCH",
        ):
            validate_formal_acceptance_bundle(
                {
                    "rcLiveAcceptanceInput": mixed_input,
                    "profileReceipts": result["profileReceipts"],
                    "aggregateReceipt": result["aggregateReceipt"],
                    "executionReceipt": result["executionReceipt"],
                }
            )

    def test_formal_bundle_rejects_swapped_profile_keys(self):
        request = self._authority_request()
        authority = VerifiedFormalRcAuthority.issue(
            request,
            provenance_preflight_digest=self._combined_preflight_digest(request),
            actions_preflight_receipt_digest="sha256:" + "d" * 64,
            provenance_claim_summaries=self._provenance_summaries(request),
            publication_preflight_summary=self._publication_summary(),
            pretrusted_profile_identity="sha256:" + "5" * 64,
            provenance_verifier_identity="sha256:" + "8" * 64,
            github_trusted_root_identity="sha256:" + "a" * 64,
            sigstore_trusted_root_identity="sha256:" + "e" * 64,
            publication_execution_receipt_identity="sha256:" + "b" * 64,
            publication_signed_claim_identity="sha256:" + "c" * 64,
            publication_signed_at="2026-08-29T23:59:59Z",
            **self._candidate_evidence(),
        )

        class Verifier:
            def verify(self, _request):
                return authority

        class Executor:
            def execute(self, *, authority, profile):
                return FormalVmControllerTests._observation(authority, profile)

        result = FormalVmController(
            authority_verifier=Verifier(), profile_executor=Executor()
        ).execute(request, self._execution_context())
        profiles = dict(result["profileReceipts"])
        profiles["FORMAL_FRESH"], profiles["FORMAL_DOCKER"] = (
            profiles["FORMAL_DOCKER"],
            profiles["FORMAL_FRESH"],
        )
        with self.assertRaisesRegex(
            formal_provenance.FormalProducerError,
            "FORMAL_ACCEPTANCE_PROFILE_SET_INVALID",
        ):
            validate_formal_acceptance_bundle(
                {
                    "rcLiveAcceptanceInput": result["rcLiveAcceptanceInput"],
                    "profileReceipts": profiles,
                    "aggregateReceipt": result["aggregateReceipt"],
                    "executionReceipt": result["executionReceipt"],
                }
            )
        validate_schema(
            result["rcLiveAcceptanceInput"],
            json.loads(
                Path("release/formal-rc-live-acceptance-input.schema.json").read_text(
                    encoding="utf-8"
                )
            ),
        )

    def test_execution_metadata_does_not_change_formal_authority(self):
        request = self._authority_request()
        authority = VerifiedFormalRcAuthority.issue(
            request,
            provenance_preflight_digest=self._combined_preflight_digest(request),
            actions_preflight_receipt_digest="sha256:" + "d" * 64,
            provenance_claim_summaries=self._provenance_summaries(request),
            publication_preflight_summary=self._publication_summary(),
            pretrusted_profile_identity="sha256:" + "5" * 64,
            provenance_verifier_identity="sha256:" + "8" * 64,
            github_trusted_root_identity="sha256:" + "a" * 64,
            sigstore_trusted_root_identity="sha256:" + "e" * 64,
            publication_execution_receipt_identity="sha256:" + "b" * 64,
            publication_signed_claim_identity="sha256:" + "c" * 64,
            publication_signed_at="2026-08-29T23:59:59Z",
            **self._candidate_evidence(),
        )

        class Verifier:
            def verify(self, _request):
                return authority

        class Executor:
            def execute(self, *, authority, profile):
                return FormalVmControllerTests._observation(authority, profile)

        controller = FormalVmController(
            authority_verifier=Verifier(), profile_executor=Executor()
        )
        first = controller.execute(request, self._execution_context())
        second = controller.execute(
            request,
            FormalExecutionContext(
                accepted_at="2026-08-31T11:12:13Z",
                observed_at="2026-08-31T11:12:11Z",
                operator_identity="different-display-name",
                run_id="formal-run-2",
                run_attempt=2,
                correlation_id="formal-correlation-2",
                current_workflow_commit="d" * 40,
                execution_environment="different-host",
                tool_identity="sha256:" + "c" * 64,
            ),
        )
        self.assertEqual(
            first["aggregateReceipt"]["formal_authority_identity"],
            second["aggregateReceipt"]["formal_authority_identity"],
        )
        self.assertEqual(
            {
                key: value["profile_authority_identity"]
                for key, value in first["profileReceipts"].items()
            },
            {
                key: value["profile_authority_identity"]
                for key, value in second["profileReceipts"].items()
            },
        )
        self.assertNotEqual(
            first["executionReceipt"]["receipt_digest"],
            second["executionReceipt"]["receipt_digest"],
        )

    def test_formal_authority_determinism_matrix_closes_ambient_and_logical_axes(
        self,
    ):
        request = self._authority_request()

        def identity(candidate: FormalAuthorityRequest) -> str:
            return formal_provenance.sha256_bytes(
                canonical_json_bytes(candidate.identity_body())
            )

        baseline = identity(request)
        reversed_claims = dict(
            reversed(tuple(request.attestation_claim_identities.items()))
        )
        reordered = replace(
            request,
            attestation_claim_identities=reversed_claims,
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ambient = root / "ambient"
            ambient.mkdir()
            marker = ambient / "mtime-only"
            marker.write_bytes(b"not authority input")
            os.utime(marker, (1_700_000_000, 1_700_000_123))
            original_cwd = Path.cwd()
            try:
                os.chdir(ambient)
                with mock.patch.dict(
                    os.environ,
                    {
                        "TZ": "Pacific/Kiritimati",
                        "LANG": "tr_TR.UTF-8",
                        "LC_ALL": "tr_TR.UTF-8",
                        "PYTHONHASHSEED": "8675309",
                    },
                    clear=False,
                ):
                    ambient_identity = identity(reordered)
            finally:
                os.chdir(original_cwd)
        self.assertEqual(baseline, ambient_identity)

        logical_mutations = (
            replace(request, verified_candidate_digest="sha256:" + "f" * 64),
            replace(request, source_sha="f" * 40),
            replace(request, source_tree="e" * 40),
            replace(request, installer_materials_identity="sha256:" + "e" * 64),
            replace(request, publication_identity="sha256:" + "f" * 64),
            replace(
                request,
                attestation_claim_identities={
                    **request.attestation_claim_identities,
                    "installer-materials": "sha256:" + "e" * 64,
                },
            ),
        )
        for mutation in logical_mutations:
            with self.subTest(mutation=mutation.identity_body()):
                self.assertNotEqual(baseline, identity(mutation))
        with self.assertRaisesRegex(
            FormalProducerError, "FORMAL_RC_AUTHORITY_INVALID"
        ):
            replace(request, source_sha="not-a-git-object")
        with self.assertRaisesRegex(
            FormalProducerError, "FORMAL_EXECUTION_TIME_INVALID"
        ):
            replace(
                self._execution_context(),
                accepted_at="2026-08-30T09:02:03+08:00",
            )

    def test_controlled_profile_rejection_is_fail_not_execution_error(self):
        request = self._authority_request()
        authority = VerifiedFormalRcAuthority.issue(
            request,
            provenance_preflight_digest=self._combined_preflight_digest(request),
            actions_preflight_receipt_digest="sha256:" + "d" * 64,
            provenance_claim_summaries=self._provenance_summaries(request),
            publication_preflight_summary=self._publication_summary(),
            pretrusted_profile_identity="sha256:" + "5" * 64,
            provenance_verifier_identity="sha256:" + "8" * 64,
            github_trusted_root_identity="sha256:" + "a" * 64,
            sigstore_trusted_root_identity="sha256:" + "e" * 64,
            publication_execution_receipt_identity="sha256:" + "b" * 64,
            publication_signed_claim_identity="sha256:" + "c" * 64,
            publication_signed_at="2026-08-29T23:59:59Z",
            **self._candidate_evidence(),
        )

        class Verifier:
            def verify(self, _request):
                return authority

        class Executor:
            def execute(self, *, authority, profile):
                observation = FormalVmControllerTests._observation(
                    authority, profile
                )
                return (
                    replace(observation, result="FAIL")
                    if profile == "FORMAL_DOCKER"
                    else observation
                )

        result = FormalVmController(
            authority_verifier=Verifier(), profile_executor=Executor()
        ).execute(request, self._execution_context())
        docker = result["aggregateReceipt"]["profile_results"]["formal_docker"]
        self.assertEqual(docker["status"], "FAIL")
        self.assertEqual(
            docker["failure_code"], "FORMAL_PROFILE_ACCEPTANCE_FAILED"
        )
        self.assertRegex(docker["receipt_digest"], r"^sha256:[0-9a-f]{64}$")
        self.assertEqual(result["executionReceipt"]["result"], "FAIL")
        self.assertIsNone(result["rcLiveAcceptanceInput"])
        self.assertIsNone(result["rcLiveAcceptanceRecord"])
    def test_profile_local_failure_keeps_independent_diagnostics_and_no_acceptance(
        self,
    ):
        request = self._authority_request()
        authority = VerifiedFormalRcAuthority.issue(
            request,
            provenance_preflight_digest=self._combined_preflight_digest(request),
            actions_preflight_receipt_digest="sha256:" + "d" * 64,
            provenance_claim_summaries=self._provenance_summaries(request),
            publication_preflight_summary=self._publication_summary(),
            pretrusted_profile_identity="sha256:" + "5" * 64,
            provenance_verifier_identity="sha256:" + "8" * 64,
            github_trusted_root_identity="sha256:" + "a" * 64,
            sigstore_trusted_root_identity="sha256:" + "e" * 64,
            publication_execution_receipt_identity="sha256:" + "b" * 64,
            publication_signed_claim_identity="sha256:" + "c" * 64,
            publication_signed_at="2026-08-29T23:59:59Z",
            **self._candidate_evidence(),
        )
        called: list[str] = []

        class Verifier:
            def verify(self, _request):
                return authority

        class Executor:
            def execute(self, *, authority, profile):
                called.append(profile)
                if profile == "FORMAL_DOCKER":
                    raise FormalProfileExecutionError(
                        "FORMAL_PROFILE_DOCTOR_FAILED",
                        continuation_safe=True,
                        continuation_receipt_digest="sha256:" + "b" * 64,
                    )
                return FormalVmControllerTests._observation(authority, profile)

        result = FormalVmController(
            authority_verifier=Verifier(), profile_executor=Executor()
        ).execute(request, self._execution_context())
        self.assertEqual(called, ["FORMAL_FRESH", "FORMAL_DOCKER", "FORMAL_OFFLINE"])
        self.assertEqual(result["status"], "FAIL")
        self.assertIsNone(result["rcLiveAcceptanceInput"])
        self.assertEqual(
            result["aggregateReceipt"]["profile_results"]["formal_docker"],
            {
                "status": "ERROR",
                "failure_code": "FORMAL_PROFILE_DOCTOR_FAILED",
                "receipt_digest": None,
                "continuation_receipt_digest": "sha256:" + "b" * 64,
            },
        )

    def test_offline_preflight_closes_all_five_production_verifier_calls(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            verifier = root / ("verifier.exe" if os.name == "nt" else "verifier")
            verifier.write_bytes(b"production verifier fixture")
            verifier.chmod(0o700)
            subject_names = {
                "api-image": "ghcr.io/yanyuhanyue/animemo-api",
                "web-image": "ghcr.io/yanyuhanyue/animemo-web",
                "release-manifest": "release-manifest.json",
                "deployment-contract": "deployment-contract.json",
                "installer-materials": "installer-materials.tar",
            }
            claims = {
                name: {
                    "schemaVersion": 1,
                    "subject": {
                        "name": subject_names[name],
                        "sha256": "sha256:" + character * 64,
                    },
                    "repository": {
                        "name": "yanyuhanyue/AniMemo",
                        "repositoryId": "1327429673",
                        "ownerId": "111261350",
                    },
                    "workflow": ".github/workflows/release.yml",
                    "source": {
                        "commit": character * 40,
                        "ref": "refs/heads/main",
                    },
                    "signerDigest": character * 40,
                }
                for name, character in zip(
                    (
                        "api-image",
                        "web-image",
                        "release-manifest",
                        "deployment-contract",
                        "installer-materials",
                    ),
                    "12345",
                    strict=True,
                )
            }
            inputs = []
            originals = {}
            initial_bytes = {}
            for name, character in reversed(tuple(zip(claims, "12345", strict=True))):
                bundle = root / f"{name}.bundle.json"
                trusted_root = root / f"{name}.root.json"
                request = root / f"{name}.request.json"
                bundle.write_bytes(
                    canonical_json_bytes({"name": name, "kind": "bundle"})
                )
                trusted_root.write_bytes(
                    canonical_json_bytes({"name": name, "kind": "root"})
                )
                request.write_bytes(
                    canonical_json_bytes(
                        {
                            "schemaVersion": 1,
                            "mode": "actions-provenance",
                            "evidenceName": name,
                            "subject": {
                                "name": subject_names[name],
                                "sha256": "sha256:" + character * 64,
                                "size": 0,
                            },
                            "workflow": ".github/workflows/release.yml",
                            "sourceCommit": character * 40,
                        }
                    )
                )
                inputs.append(
                    FormalProvenanceInput(name, bundle, trusted_root, request)
                )
                originals[name] = {
                    "bundle": bundle,
                    "trusted-root": trusted_root,
                    "request": request,
                }
                initial_bytes[name] = {
                    key: path.read_bytes() for key, path in originals[name].items()
                }
            calls: list[tuple[str, ...]] = []
            originals_replaced = False

            def runner(command: tuple[str, ...]) -> bytes:
                nonlocal originals_replaced
                calls.append(command)
                request = Path(command[command.index("--request") + 1])
                name = request.name.removesuffix(".request.json")
                for argument, key in (
                    ("--bundle", "bundle"),
                    ("--trusted-root", "trusted-root"),
                    ("--request", "request"),
                ):
                    snapshot = Path(command[command.index(argument) + 1])
                    self.assertNotEqual(snapshot, originals[name][key])
                    self.assertEqual(snapshot.read_bytes(), initial_bytes[name][key])
                self.assertNotEqual(Path(command[0]), verifier)
                self.assertEqual(
                    Path(command[0]).read_bytes(), b"production verifier fixture"
                )
                if not originals_replaced:
                    verifier.write_bytes(b"replaced verifier")
                    for paths in originals.values():
                        for path in paths.values():
                            path.write_bytes(b"replaced original input")
                    originals_replaced = True
                return canonical_json_bytes(claims[name])

            receipt = OfflineActionsProvenancePreflight(
                FormalProvenancePlan(verifier=verifier, inputs=tuple(inputs)),
                runner=runner,
            ).verify()

            self.assertEqual(len(calls), 5)
            schema = json.loads(
                Path(
                    "release/formal-provenance-preflight-receipt.schema.json"
                ).read_text(encoding="utf-8")
            )
            validate_schema(receipt, schema)
            self.assertFalse(receipt["clone_authorized"])
            self.assertFalse(receipt["release_authority_granted"])
            self.assertFalse(receipt["publish_authorized"])
            self.assertEqual(
                [item["evidence_name"] for item in receipt["claims"]],
                sorted(claims),
            )
            self.assertRegex(receipt["preflight_digest"], r"^sha256:[0-9a-f]{64}$")
            for command in calls:
                self.assertIn("--bundle", command)
                self.assertIn("--trusted-root", command)
                self.assertIn("--request", command)
            self.assertEqual(
                {item["request_digest"] for item in receipt["claims"]},
                {
                    "sha256:" + hashlib.sha256(values["request"]).hexdigest()
                    for values in initial_bytes.values()
                },
            )
            self.assertEqual(
                {item["bundle_digest"] for item in receipt["claims"]},
                {
                    "sha256:" + hashlib.sha256(values["bundle"]).hexdigest()
                    for values in initial_bytes.values()
                },
            )
            self.assertEqual(
                {item["trusted_root_digest"] for item in receipt["claims"]},
                {
                    "sha256:" + hashlib.sha256(values["trusted-root"]).hexdigest()
                    for values in initial_bytes.values()
                },
            )
            self.assertEqual(
                receipt["verifier_digest"],
                "sha256:" + hashlib.sha256(b"production verifier fixture").hexdigest(),
            )

    def test_same_api_provenance_cannot_impersonate_all_required_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            verifier = root / ("verifier.exe" if os.name == "nt" else "verifier")
            verifier.write_bytes(b"production verifier fixture")
            verifier.chmod(0o700)
            bundle = root / "api.bundle.json"
            trusted_root = root / "trusted-root.json"
            request = root / "api.request.json"
            bundle.write_bytes(b"api provenance bundle")
            trusted_root.write_bytes(b"trusted root")
            request.write_bytes(
                canonical_json_bytes(
                    {
                        "schemaVersion": 1,
                        "mode": "actions-provenance",
                        "evidenceName": "api-image",
                        "subject": {
                            "name": "ghcr.io/yanyuhanyue/animemo-api",
                            "sha256": "sha256:" + "1" * 64,
                            "size": 0,
                        },
                        "workflow": ".github/workflows/release.yml",
                        "sourceCommit": "1" * 40,
                    }
                )
            )
            inputs = tuple(
                FormalProvenanceInput(name, bundle, trusted_root, request)
                for name in (
                    "api-image",
                    "web-image",
                    "release-manifest",
                    "deployment-contract",
                    "installer-materials",
                )
            )
            api_claim = canonical_json_bytes(
                {
                    "schemaVersion": 1,
                    "subject": {
                        "name": "ghcr.io/yanyuhanyue/animemo-api",
                        "sha256": "sha256:" + "1" * 64,
                    },
                }
            )

            with self.assertRaisesRegex(
                ProvenancePreflightError,
                "FORMAL_PROVENANCE_EVIDENCE_BINDING_INVALID",
            ):
                OfflineActionsProvenancePreflight(
                    FormalProvenancePlan(verifier=verifier, inputs=inputs),
                    runner=lambda _command: api_claim,
                ).verify()


if __name__ == "__main__":
    unittest.main()
