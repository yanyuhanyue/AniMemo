"""Test-only producer fixture for closed Formal acceptance evidence."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from .acceptance import AcceptanceError
from .formal_vm_controller import (
    CANDIDATE_PROFILE_RESULT_KEYS,
    FormalAuthorityRequest,
    FormalExecutionContext,
    FormalProfileObservation,
    FormalVmController,
    VerifiedFormalRcAuthority,
)


def build_test_formal_acceptance(
    *,
    rc_tag: str,
    rc_commit: str,
    rc_tree: str,
    release_manifest_identity: str,
    deployment_contract_identity: str,
    installer_materials_identity: str,
    api_digest: str,
    web_digest: str,
    fresh_base_identity: str,
    docker_base_identity: str,
    runtime_base_identity: str,
    accepted_at: str,
    operator_identity: str,
    observed_at: str | None = None,
    run_id: str = "test-formal-run",
    run_attempt: int = 1,
    correlation_id: str = "test-formal-correlation",
    current_workflow_commit: str = "e" * 40,
    execution_environment: str = "test-private-vm",
    tool_identity: str = "sha256:" + "f" * 64,
) -> dict[str, Any]:
    try:
        request = FormalAuthorityRequest(
            repository="yanyuhanyue/AniMemo",
            rc_tag=rc_tag,
            verified_candidate_digest="sha256:" + "0" * 64,
            source_sha=rc_commit,
            source_tree=rc_tree,
            release_manifest_identity=release_manifest_identity,
            deployment_contract_identity=deployment_contract_identity,
            installer_materials_identity=installer_materials_identity,
            formal_windows_pretrust_kit_identity="sha256:" + "0" * 64,
            offline_release_trust_profile_identity="sha256:" + "1" * 64,
            api_digest=api_digest,
            web_digest=web_digest,
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
    except (RuntimeError, ValueError) as error:
        raise AcceptanceError("test Formal Authority input is invalid") from error
    actions_preflight_digest = "sha256:" + "d" * 64
    publication_execution_identity = "sha256:" + "b" * 64
    publication_signed_claim_identity = "sha256:" + "c" * 64
    publication_preflight_summary = {
        "verifier_digest": "sha256:" + "8" * 64,
        "bundle_digest": "sha256:" + "9" * 64,
        "trusted_root_digest": "sha256:" + "a" * 64,
        "request_digest": "sha256:" + "b" * 64,
        "claim_digest": publication_signed_claim_identity,
    }
    combined_preflight = {
        "schema": "animemo.formal-production-provenance/v1",
        "actions_preflight_digest": actions_preflight_digest,
        "publication_authority_identity": request.publication_identity,
        "publication_execution_receipt_identity": publication_execution_identity,
        "publication_signed_claim_identity": publication_signed_claim_identity,
        "publication_preflight": publication_preflight_summary,
        "formal_windows_pretrust_kit_identity": (
            request.formal_windows_pretrust_kit_identity
        ),
        "offline_release_trust_profile_identity": (
            request.offline_release_trust_profile_identity
        ),
        "pretrusted_profile_identity": "sha256:" + "5" * 64,
        "provenance_verifier_identity": "sha256:" + "8" * 64,
        "github_trusted_root_identity": "sha256:" + "a" * 64,
        "sigstore_trusted_root_identity": "sha256:" + "3" * 64,
        "release_authority_granted": False,
        "publish_authorized": False,
    }
    provenance_preflight_digest = "sha256:" + hashlib.sha256(
        (
            json.dumps(
                combined_preflight,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
    ).hexdigest()
    source_original_hashes = {"source.vmx": "sha256:" + "c" * 64}
    source_base_identity = "sha256:" + hashlib.sha256(
        (
            json.dumps(
                source_original_hashes,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
    ).hexdigest()
    authority = VerifiedFormalRcAuthority.issue(
        request,
        provenance_preflight_digest=provenance_preflight_digest,
        actions_preflight_receipt_digest=actions_preflight_digest,
        provenance_claim_summaries={
            name: {
                "claim_digest": digest,
                "bundle_digest": "sha256:" + "2" * 64,
                "trusted_root_digest": "sha256:" + "3" * 64,
                "request_digest": "sha256:" + "4" * 64,
            }
            for name, digest in request.attestation_claim_identities.items()
        },
        publication_preflight_summary=publication_preflight_summary,
        pretrusted_profile_identity="sha256:" + "5" * 64,
        provenance_verifier_identity="sha256:" + "8" * 64,
        github_trusted_root_identity="sha256:" + "a" * 64,
        sigstore_trusted_root_identity="sha256:" + "3" * 64,
        publication_execution_receipt_identity=publication_execution_identity,
        publication_signed_claim_identity=publication_signed_claim_identity,
        publication_signed_at="2026-08-29T23:59:59Z",
        candidate_aggregate_receipt_digest="sha256:" + "6" * 64,
        candidate_profile_receipt_digests={
            key: "sha256:" + value * 64
            for key, value in zip(
                CANDIDATE_PROFILE_RESULT_KEYS, "789", strict=True
            )
        },
        candidate_plan_digest="sha256:" + "a" * 64,
        candidate_provider_execution_authority_receipt_digest=(
            "sha256:" + "9" * 64
        ),
        candidate_base_vm_identity=source_base_identity,
        candidate_original_vm_hashes=source_original_hashes,
        candidate_snapshot_identities={
            "FRESH_BASE": "sha256:" + "d" * 64,
            "DOCKER_BASE": "sha256:" + "e" * 64,
            "RUNTIME_BASE_OFFLINE": "sha256:" + "f" * 64,
        },
        candidate_source_disk_graph_identity="sha256:" + "1" * 64,
        candidate_snapshot_disk_graph_identities={
            "FRESH_BASE": "sha256:" + "2" * 64,
            "DOCKER_BASE": "sha256:" + "3" * 64,
            "RUNTIME_BASE_OFFLINE": "sha256:" + "4" * 64,
        },
        candidate_source_vm_inventory_identity="sha256:" + "5" * 64,
    )
    snapshots = {
        "FORMAL_FRESH": fresh_base_identity,
        "FORMAL_DOCKER": docker_base_identity,
        "FORMAL_OFFLINE": runtime_base_identity,
    }

    class Verifier:
        def verify(self, _request: FormalAuthorityRequest) -> VerifiedFormalRcAuthority:
            return authority

    class Executor:
        def execute(
            self,
            *,
            authority: VerifiedFormalRcAuthority,
            profile: str,
        ) -> FormalProfileObservation:
            snapshot = snapshots[profile]
            return FormalProfileObservation(
                profile=profile,
                rc_authority_identity=authority.identity,
                transport_source=(
                    "local-bundle" if profile == "FORMAL_OFFLINE" else "github"
                ),
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
                snapshot_identity=snapshot,
                clone_identity=snapshot,
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
                provenance_verifier_identity=(
                    authority.provenance_verifier_identity
                ),
                github_trusted_root_identity=(
                    authority.github_trusted_root_identity
                ),
                sigstore_trusted_root_identity=(
                    authority.sigstore_trusted_root_identity
                ),
                platform_plan_digest="sha256:" + "8" * 64,
                platform_receipt_digest="sha256:" + "9" * 64,
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

    try:
        result = FormalVmController(
            authority_verifier=Verifier(),
            profile_executor=Executor(),
        ).execute(
            request,
            FormalExecutionContext(
                accepted_at=accepted_at,
                observed_at=observed_at or accepted_at,
                operator_identity=operator_identity,
                run_id=run_id,
                run_attempt=run_attempt,
                correlation_id=correlation_id,
                current_workflow_commit=current_workflow_commit,
                execution_environment=execution_environment,
                tool_identity=tool_identity,
            ),
        )
    except (RuntimeError, ValueError) as error:
        raise AcceptanceError("test Formal production failed") from error
    record = result["rcLiveAcceptanceRecord"]
    if not isinstance(record, dict):
        raise AssertionError("test Formal producer did not emit acceptance")
    return record
