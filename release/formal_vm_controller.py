"""Closed offline verifier for the five Formal provenance evidence roles.

This Wave A component emits a non-authoritative preflight receipt only.  It
deliberately exposes no VM provider, clone capability, release authority, or
publication authority.  Wave C owns the production composition that combines
this verified evidence set with exact immutable RC authority before any clone.
"""

from __future__ import annotations

import ctypes
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
from collections.abc import Callable, Mapping, Sequence
from contextlib import ExitStack, nullcontext
from ctypes import wintypes
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from jsonschema import Draft202012Validator

from .candidate import (
    CandidateContractError,
    LoadedVerifiedCandidate,
    aggregate_receipt_digest,
    canonical_json_bytes,
    reject_duplicate_json_keys,
    sha256_bytes,
    validate_aggregate_receipt,
    validate_profile_receipt,
)
from .formal_windows_pretrust import (
    FORMAL_WINDOWS_PRETRUST_FILES,
    FORMAL_WINDOWS_PRETRUST_PREFIX,
    FormalWindowsPretrustedTrustMaterial,
    FormalWindowsPretrustError,
    HeldWindowsPrivatePathAuthority,
    assert_windows_private_acl,
    create_windows_private_directory,
    hold_windows_private_descendant_path,
    hold_windows_private_path_chain,
    hold_windows_private_snapshot,
    inspect_formal_windows_pretrust_in_installer_materials,
)
from .publication_evidence import (
    PublicationEvidenceError,
    close_github_release_publication,
)

PREFLIGHT_SCHEMA = "animemo.formal-provenance-preflight/v1"
REQUIRED_EVIDENCE = frozenset(
    {
        "api-image",
        "web-image",
        "release-manifest",
        "deployment-contract",
        "installer-materials",
    }
)
EXPECTED_SUBJECT_BY_EVIDENCE = {
    "api-image": "ghcr.io/yanyuhanyue/animemo-api",
    "web-image": "ghcr.io/yanyuhanyue/animemo-web",
    "release-manifest": "release-manifest.json",
    "deployment-contract": "deployment-contract.json",
    "installer-materials": "installer-materials.tar",
}
EXPECTED_REPOSITORY = {
    "name": "yanyuhanyue/AniMemo",
    "repositoryId": "1327429673",
    "ownerId": "111261350",
}
SHA256_IDENTITY = re.compile(r"^sha256:[0-9a-f]{64}$")
COMMIT_IDENTITY = re.compile(r"^[0-9a-f]{40}$")
MAX_VERIFIER_BYTES = 128 * 1024 * 1024
MAX_INPUT_BYTES = 16 * 1024 * 1024
MAX_CLAIM_BYTES = 1024 * 1024
FORMAL_PROFILES = ("FORMAL_FRESH", "FORMAL_DOCKER", "FORMAL_OFFLINE")
FORMAL_PROFILE_RESULT_KEYS = {
    "FORMAL_FRESH": "formal_fresh",
    "FORMAL_DOCKER": "formal_docker",
    "FORMAL_OFFLINE": "formal_offline",
}
CANDIDATE_PROFILE_RESULT_KEYS = (
    "fresh_base",
    "docker_base",
    "runtime_base_offline",
)
FORMAL_PROFILE_SCHEMA = "animemo.formal-profile-receipt/v1"
FORMAL_AGGREGATE_SCHEMA = "animemo.formal-acceptance-receipt/v1"
FORMAL_EXECUTION_SCHEMA = "animemo.formal-execution-receipt/v1"
FORMAL_ACCEPTANCE_INPUT_SCHEMA = "animemo.formal-rc-live-acceptance-input/v1"
FORMAL_AUTHORITY_SCHEMA = "animemo.formal-rc-authority/v1"
FORMAL_PROFILE_AUTHORITY_SCHEMA = "animemo.formal-profile-authority/v1"
FORMAL_PRODUCER_CONTRACT_IDENTITY = sha256_bytes(
    canonical_json_bytes(
        {
            "schema": "animemo.formal-production-contract/v1",
            "profiles": list(FORMAL_PROFILES),
            "profileReceiptSchema": FORMAL_PROFILE_SCHEMA,
            "aggregateReceiptSchema": FORMAL_AGGREGATE_SCHEMA,
            "executionReceiptSchema": FORMAL_EXECUTION_SCHEMA,
            "acceptanceInputSchema": FORMAL_ACCEPTANCE_INPUT_SCHEMA,
            "provenanceBeforeClone": True,
            "syntheticEvidenceAccepted": False,
        }
    )
)
_FORMAL_WORKFLOW_IDENTITY = (
    "yanyuhanyue/AniMemo/.github/workflows/release.yml@refs/heads/main"
)
_FORMAL_RC_TAG = re.compile(
    r"^v(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\."
    r"(?:0|[1-9][0-9]*)-rc\.[1-9][0-9]*$"
)
_FORMAL_CODE = re.compile(r"^FORMAL_[A-Z0-9_]{1,120}$")
_CANONICAL_UTC_SECONDS = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$"
)


class ProvenancePreflightError(RuntimeError):
    """Stable fail-closed error for the pre-clone provenance boundary."""


class FormalProducerError(RuntimeError):
    """Stable, secret-free producer failure."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class FormalProfileExecutionError(FormalProducerError):
    """A classified profile failure with an explicit continuation decision."""

    def __init__(
        self,
        code: str,
        *,
        continuation_safe: bool,
        continuation_receipt_digest: str,
    ) -> None:
        if (
            _FORMAL_CODE.fullmatch(code) is None
            or type(continuation_safe) is not bool
            or SHA256_IDENTITY.fullmatch(continuation_receipt_digest) is None
        ):
            raise FormalProducerError("FORMAL_PROFILE_FAILURE_INVALID")
        self.continuation_safe = continuation_safe
        self.continuation_receipt_digest = continuation_receipt_digest
        super().__init__(code)


def _formal_reject(code: str) -> None:
    raise FormalProducerError(code)


def _is_digest(value: object) -> bool:
    return type(value) is str and SHA256_IDENTITY.fullmatch(value) is not None


def _closed_text(value: object, *, maximum: int = 256) -> bool:
    return (
        type(value) is str
        and 0 < len(value) <= maximum
        and all(
            ord(character) >= 0x20 and ord(character) != 0x7F for character in value
        )
    )


def _canonical_utc_seconds(value: object) -> str:
    if type(value) is not str or _CANONICAL_UTC_SECONDS.fullmatch(value) is None:
        _formal_reject("FORMAL_EXECUTION_TIME_INVALID")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError as error:
        raise FormalProducerError("FORMAL_EXECUTION_TIME_INVALID") from error
    if parsed.strftime("%Y-%m-%dT%H:%M:%SZ") != value:
        _formal_reject("FORMAL_EXECUTION_TIME_INVALID")
    return value


def _validate_attestation_identities(value: object) -> dict[str, str]:
    if (
        type(value) is not dict
        or set(value) != REQUIRED_EVIDENCE
        or any(
            type(name) is not str or not _is_digest(digest)
            for name, digest in value.items()
        )
    ):
        _formal_reject("FORMAL_RC_AUTHORITY_INVALID")
    return dict(sorted(value.items()))


@dataclass(frozen=True)
class FormalAuthorityRequest:
    repository: str
    rc_tag: str
    verified_candidate_digest: str
    source_sha: str
    source_tree: str
    release_manifest_identity: str
    deployment_contract_identity: str
    installer_materials_identity: str
    formal_windows_pretrust_kit_identity: str
    offline_release_trust_profile_identity: str
    api_digest: str
    web_digest: str
    publication_identity: str
    workflow_identity: str
    attestation_claim_identities: Mapping[str, str]

    def __post_init__(self) -> None:
        if (
            self.repository != EXPECTED_REPOSITORY["name"]
            or _FORMAL_RC_TAG.fullmatch(self.rc_tag) is None
            or COMMIT_IDENTITY.fullmatch(self.source_sha) is None
            or COMMIT_IDENTITY.fullmatch(self.source_tree) is None
            or self.workflow_identity != _FORMAL_WORKFLOW_IDENTITY
            or any(
                not _is_digest(value)
                for value in (
                    self.release_manifest_identity,
                    self.verified_candidate_digest,
                    self.deployment_contract_identity,
                    self.installer_materials_identity,
                    self.formal_windows_pretrust_kit_identity,
                    self.offline_release_trust_profile_identity,
                    self.api_digest,
                    self.web_digest,
                    self.publication_identity,
                )
            )
        ):
            _formal_reject("FORMAL_RC_AUTHORITY_INVALID")
        object.__setattr__(
            self,
            "attestation_claim_identities",
            _validate_attestation_identities(
                dict(self.attestation_claim_identities)
                if isinstance(self.attestation_claim_identities, Mapping)
                else self.attestation_claim_identities
            ),
        )

    def identity_body(self) -> dict[str, object]:
        return {
            "schema": FORMAL_AUTHORITY_SCHEMA,
            "repository": self.repository,
            "rc_tag": self.rc_tag,
            "verified_candidate_digest": self.verified_candidate_digest,
            "source_sha": self.source_sha,
            "source_tree": self.source_tree,
            "release_manifest_identity": self.release_manifest_identity,
            "deployment_contract_identity": self.deployment_contract_identity,
            "installer_materials_identity": self.installer_materials_identity,
            "formal_windows_pretrust_kit_identity": (
                self.formal_windows_pretrust_kit_identity
            ),
            "offline_release_trust_profile_identity": (
                self.offline_release_trust_profile_identity
            ),
            "api_digest": self.api_digest,
            "web_digest": self.web_digest,
            "publication_identity": self.publication_identity,
            "workflow_identity": self.workflow_identity,
            "attestation_claim_identities": dict(self.attestation_claim_identities),
            "release_authority_granted": False,
            "publish_authorized": False,
        }


@dataclass(frozen=True)
class _QualifiedCandidateFormalRecord:
    loaded: LoadedVerifiedCandidate
    candidate_material_authority: object | None
    candidate_plan_digest: str
    candidate_provider_execution_authority_receipt_digest: str
    candidate_material_authority_identity: str
    candidate_material_tree_inventory_identity: str
    candidate_aggregate_receipt_digest: str
    candidate_profile_receipt_digests: Mapping[str, str]
    base_vm_identity: str
    original_vm_hashes: Mapping[str, str]
    snapshot_identities: Mapping[str, str]
    source_disk_graph_identity: str
    snapshot_disk_graph_identities: Mapping[str, str]
    source_vm_inventory_identity: str
    candidate_source_vm_authority_identity: str
    formal_windows_pretrust_root: Path


_QUALIFIED_CANDIDATE_FORMAL_RECORDS: dict[object, _QualifiedCandidateFormalRecord] = {}


class QualifiedCandidateFormalAuthority:
    """Opaque Candidate-PASS authority; only the close function can issue it."""

    __slots__ = ("__token",)

    def __init__(self) -> None:
        raise TypeError("Qualified Candidate Formal authority不能直接构造")

    def __reduce__(self):
        raise TypeError("Qualified Candidate Formal authority不可序列化")

    def _record(self) -> _QualifiedCandidateFormalRecord:
        try:
            return _QUALIFIED_CANDIDATE_FORMAL_RECORDS[self.__token]
        except (AttributeError, KeyError) as error:
            raise FormalProducerError("FORMAL_QUALIFIED_CANDIDATE_INVALID") from error

    def close(self) -> None:
        """Invalidate this in-memory authority exactly once."""

        token = getattr(self, "_QualifiedCandidateFormalAuthority__token", None)
        if token is None:
            return
        record = _QUALIFIED_CANDIDATE_FORMAL_RECORDS.get(token)
        if record is not None and record.candidate_material_authority is not None:
            try:
                record.candidate_material_authority.close()
            except Exception as error:
                raise FormalProducerError(
                    "FORMAL_CANDIDATE_MATERIAL_AUTHORITY_RELEASE_FAILED"
                ) from error
        _QUALIFIED_CANDIDATE_FORMAL_RECORDS.pop(token, None)
        self.__token = None

    @property
    def loaded(self) -> LoadedVerifiedCandidate:
        return self._record().loaded

    @property
    def candidate_aggregate_receipt_digest(self) -> str:
        return self._record().candidate_aggregate_receipt_digest

    @property
    def candidate_profile_receipt_digests(self) -> Mapping[str, str]:
        return dict(self._record().candidate_profile_receipt_digests)

    @property
    def candidate_plan_digest(self) -> str:
        return self._record().candidate_plan_digest

    @property
    def candidate_provider_execution_authority_receipt_digest(self) -> str:
        return self._record().candidate_provider_execution_authority_receipt_digest

    @property
    def candidate_material_authority_identity(self) -> str:
        return self._record().candidate_material_authority_identity

    @property
    def candidate_material_tree_inventory_identity(self) -> str:
        return self._record().candidate_material_tree_inventory_identity

    @property
    def base_vm_identity(self) -> str:
        return self._record().base_vm_identity

    @property
    def original_vm_hashes(self) -> Mapping[str, str]:
        return dict(self._record().original_vm_hashes)

    @property
    def snapshot_identities(self) -> Mapping[str, str]:
        return dict(self._record().snapshot_identities)

    @property
    def source_disk_graph_identity(self) -> str:
        return self._record().source_disk_graph_identity

    @property
    def snapshot_disk_graph_identities(self) -> Mapping[str, str]:
        return dict(self._record().snapshot_disk_graph_identities)

    @property
    def source_vm_inventory_identity(self) -> str:
        return self._record().source_vm_inventory_identity

    @property
    def candidate_source_vm_authority_identity(self) -> str:
        return self._record().candidate_source_vm_authority_identity

    @property
    def formal_windows_pretrust_root(self) -> Path:
        return self._record().formal_windows_pretrust_root

    @property
    def installer_materials(self) -> Path:
        return self.loaded.root / "installer-materials.tar"

    def issue_request(
        self,
        *,
        publication_identity: str,
        attestation_claim_identities: Mapping[str, str],
    ) -> FormalAuthorityRequest:
        candidate = self.loaded.candidate_input
        formal_binding = inspect_formal_windows_pretrust_in_installer_materials(
            self.installer_materials
        )
        return FormalAuthorityRequest(
            repository=candidate["repository"],
            rc_tag=candidate["candidate_version"],
            verified_candidate_digest=self.loaded.verified_digest,
            source_sha=candidate["source_sha"],
            source_tree=candidate["source_tree"],
            release_manifest_identity=candidate["release_manifest_sha256"],
            deployment_contract_identity=candidate["deployment_contract_sha256"],
            installer_materials_identity=candidate["installer_materials_sha256"],
            formal_windows_pretrust_kit_identity=(formal_binding.kit_identity),
            offline_release_trust_profile_identity=(
                formal_binding.source_profile_identity
            ),
            api_digest=candidate["api_oci_digest"],
            web_digest=candidate["web_oci_digest"],
            publication_identity=publication_identity,
            workflow_identity=_FORMAL_WORKFLOW_IDENTITY,
            attestation_claim_identities=attestation_claim_identities,
        )


def _issue_qualified_candidate_formal_authority(
    *,
    loaded: LoadedVerifiedCandidate,
    candidate_material_authority: object | None = None,
    candidate_plan_digest: str,
    candidate_provider_execution_authority_receipt_digest: str,
    candidate_aggregate_receipt_digest: str,
    candidate_profile_receipt_digests: Mapping[str, str],
    base_vm_identity: str,
    original_vm_hashes: Mapping[str, str],
    snapshot_identities: Mapping[str, str],
    source_disk_graph_identity: str,
    snapshot_disk_graph_identities: Mapping[str, str],
    source_vm_inventory_identity: str,
    formal_windows_pretrust_root: Path,
) -> QualifiedCandidateFormalAuthority:
    if candidate_material_authority is not None:
        from scripts.candidate_vm_harness import HeldCandidateMaterialAuthority

        if (
            type(candidate_material_authority) is not HeldCandidateMaterialAuthority
            or candidate_material_authority.loaded is not loaded
        ):
            _formal_reject("FORMAL_CANDIDATE_MATERIAL_AUTHORITY_INVALID")
        candidate_material_authority_identity = candidate_material_authority.identity
        candidate_material_tree_inventory_identity = (
            candidate_material_authority.tree_inventory_identity
        )
    else:
        candidate_material_authority_identity = "sha256:" + "0" * 64
        candidate_material_tree_inventory_identity = "sha256:" + "0" * 64
    if not _is_digest(candidate_plan_digest) or not _is_digest(
        candidate_provider_execution_authority_receipt_digest
    ):
        _formal_reject("FORMAL_CANDIDATE_SOURCE_AUTHORITY_INVALID")
    candidate_source_vm_authority_identity = _candidate_source_vm_authority_identity(
        base_vm_identity=base_vm_identity,
        original_vm_hashes=original_vm_hashes,
        snapshot_identities=snapshot_identities,
        source_disk_graph_identity=source_disk_graph_identity,
        snapshot_disk_graph_identities=snapshot_disk_graph_identities,
        source_vm_inventory_identity=source_vm_inventory_identity,
    )
    token = object()
    authority = object.__new__(QualifiedCandidateFormalAuthority)
    authority._QualifiedCandidateFormalAuthority__token = token
    _QUALIFIED_CANDIDATE_FORMAL_RECORDS[token] = _QualifiedCandidateFormalRecord(
        loaded=loaded,
        candidate_material_authority=candidate_material_authority,
        candidate_plan_digest=candidate_plan_digest,
        candidate_provider_execution_authority_receipt_digest=(
            candidate_provider_execution_authority_receipt_digest
        ),
        candidate_material_authority_identity=(
            candidate_material_authority_identity
        ),
        candidate_material_tree_inventory_identity=(
            candidate_material_tree_inventory_identity
        ),
        candidate_aggregate_receipt_digest=(candidate_aggregate_receipt_digest),
        candidate_profile_receipt_digests=dict(candidate_profile_receipt_digests),
        base_vm_identity=base_vm_identity,
        original_vm_hashes=dict(original_vm_hashes),
        snapshot_identities=dict(snapshot_identities),
        source_disk_graph_identity=source_disk_graph_identity,
        snapshot_disk_graph_identities=dict(snapshot_disk_graph_identities),
        source_vm_inventory_identity=source_vm_inventory_identity,
        candidate_source_vm_authority_identity=(
            candidate_source_vm_authority_identity
        ),
        formal_windows_pretrust_root=Path(formal_windows_pretrust_root),
    )
    return authority


def _candidate_source_vm_authority_identity(
    *,
    base_vm_identity: str,
    original_vm_hashes: Mapping[str, str],
    snapshot_identities: Mapping[str, str],
    source_disk_graph_identity: str,
    snapshot_disk_graph_identities: Mapping[str, str],
    source_vm_inventory_identity: str,
) -> str:
    profiles = {"FRESH_BASE", "DOCKER_BASE", "RUNTIME_BASE_OFFLINE"}
    original = dict(original_vm_hashes)
    snapshots = dict(snapshot_identities)
    snapshot_graphs = dict(snapshot_disk_graph_identities)
    if (
        not _is_digest(base_vm_identity)
        or not _is_digest(source_disk_graph_identity)
        or not _is_digest(source_vm_inventory_identity)
        or not original
        or any(
            type(name) is not str
            or not name
            or not _is_digest(digest)
            for name, digest in original.items()
        )
        or set(snapshots) != profiles
        or set(snapshot_graphs) != profiles
        or any(not _is_digest(value) for value in snapshots.values())
        or any(not _is_digest(value) for value in snapshot_graphs.values())
        or base_vm_identity
        != sha256_bytes(canonical_json_bytes(dict(sorted(original.items()))))
    ):
        _formal_reject("FORMAL_CANDIDATE_SOURCE_AUTHORITY_INVALID")
    body = {
        "schema": "animemo.candidate-source-vm-authority/v1",
        "baseVmIdentity": base_vm_identity,
        "originalVmHashes": dict(sorted(original.items())),
        "snapshotIdentities": {key: snapshots[key] for key in sorted(snapshots)},
        "sourceDiskGraphIdentity": source_disk_graph_identity,
        "snapshotDiskGraphIdentities": {
            key: snapshot_graphs[key] for key in sorted(snapshot_graphs)
        },
        "sourceVmInventoryIdentity": source_vm_inventory_identity,
    }
    return sha256_bytes(canonical_json_bytes(body))


_CANDIDATE_CONTINUATION_RESULTS: dict[object, tuple[object, object, object]] = {}


class CandidateFormalContinuation:
    """Opaque, one-use in-process result from the Candidate controller."""

    __slots__ = ("__token",)

    def __init__(self) -> None:
        raise TypeError("Candidate Formal continuation不能直接构造")

    def __reduce__(self):
        raise TypeError("Candidate Formal continuation不可序列化")

    def _consume(self) -> object:
        try:
            token = self.__token
            value = _CANDIDATE_CONTINUATION_RESULTS.pop(token)
        except (AttributeError, KeyError) as error:
            raise FormalProducerError(
                "FORMAL_CANDIDATE_CONTINUATION_INVALID"
            ) from error
        self.__token = None
        return value


def execute_candidate_controller_for_formal(
    *,
    verified_candidate_digest: str,
    expected_qualification_run_id: int,
    expected_source_sha: str,
    expected_source_tree: str,
    provider: object,
    authorize_plan: Callable[[Mapping[str, object]], str],
    environment: Mapping[str, str] | None = None,
    r2_client: object = None,
    _state_root: Path | None = None,
    _private_material_parent: Path | None = None,
    _parent_path_authority: HeldWindowsPrivatePathAuthority | None = None,
) -> CandidateFormalContinuation:
    """Build, authorize, and run Candidate inside one provider authority.

    No serialized receipt or operator-built plan can enter this parent seam.
    The callback sees the exact freshly-built canonical plan and can only return
    its digest; it cannot replace the plan object passed to execution.
    """

    from scripts.candidate_vm_harness import (
        ClosedVmwareProvider,
        acquire_candidate_material_authority,
        build_harness_plan,
        execute_harness_plan,
    )

    if type(provider) is not ClosedVmwareProvider or not callable(authorize_plan):
        _formal_reject("FORMAL_CANDIDATE_PROVIDER_CAPABILITY_INVALID")
    material_authority = None
    try:
        with provider.execution_authority():
            material_authority = acquire_candidate_material_authority(
                verified_candidate_digest,
                provider=provider,
                _state_root=_state_root,
                private_parent=_private_material_parent,
                _parent_path_authority=_parent_path_authority,
            )
            private_state_root = material_authority.loaded.root.parent
            plan = build_harness_plan(
                verified_candidate_digest=verified_candidate_digest,
                expected_qualification_run_id=expected_qualification_run_id,
                expected_source_sha=expected_source_sha,
                expected_source_tree=expected_source_tree,
                provider=provider,
                _state_root=private_state_root,
                _candidate_material_authority=material_authority,
            )
            accepted_plan_digest = authorize_plan(plan.as_dict())
            if accepted_plan_digest != plan.plan_digest:
                _formal_reject("FORMAL_CANDIDATE_PLAN_NOT_AUTHORIZED")
            candidate_controller_result = execute_harness_plan(
                plan,
                accepted_plan_digest=accepted_plan_digest,
                provider=provider,
                environment=environment,
                r2_client=r2_client,
                _state_root=private_state_root,
                _candidate_material_authority=material_authority,
            )
            provider_execution_receipt = provider.inspect_execution_authority()
    except BaseException:
        if material_authority is not None:
            material_authority.close()
        raise
    token = object()
    continuation = object.__new__(CandidateFormalContinuation)
    continuation._CandidateFormalContinuation__token = token
    _CANDIDATE_CONTINUATION_RESULTS[token] = (
        plan,
        candidate_controller_result,
        provider_execution_receipt,
        material_authority,
    )
    return continuation


def close_qualified_candidate_for_formal(
    expected_verified_candidate_digest: str,
    candidate_continuation: CandidateFormalContinuation,
) -> QualifiedCandidateFormalAuthority:
    """Close Candidate PASS observations before any Formal provenance/VM work."""

    material_authority = None
    try:
        if type(candidate_continuation) is not CandidateFormalContinuation:
            _formal_reject("FORMAL_CANDIDATE_CONTINUATION_INVALID")
        (
            candidate_plan,
            candidate_acceptance,
            provider_execution_receipt,
            material_authority,
        ) = candidate_continuation._consume()
        from scripts.candidate_vm_harness import (
            CandidateHarnessError,
            CandidateHarnessPlan,
            HeldCandidateMaterialAuthority,
            ProviderExecutionAuthorityReceipt,
        )

        if (
            type(candidate_plan) is not CandidateHarnessPlan
            or candidate_plan.plan_digest
            != sha256_bytes(canonical_json_bytes(candidate_plan.identity_body()))
            or type(provider_execution_receipt)
            is not ProviderExecutionAuthorityReceipt
            or provider_execution_receipt.result != "PASS"
            or provider_execution_receipt.source_vm_inventory_identity
            != candidate_plan.source_vm_inventory_identity
            or type(material_authority) is not HeldCandidateMaterialAuthority
            or provider_execution_receipt.candidate_material_authority_identity
            != material_authority.identity
            or provider_execution_receipt.candidate_material_tree_inventory_identity
            != material_authority.tree_inventory_identity
        ):
            _formal_reject("FORMAL_CANDIDATE_PLAN_INVALID")
        loaded = material_authority.loaded
        if loaded.verified_digest != expected_verified_candidate_digest:
            _formal_reject("FORMAL_CANDIDATE_MATERIAL_AUTHORITY_INVALID")
        if type(candidate_acceptance) is not dict:
            _formal_reject("FORMAL_CANDIDATE_ACCEPTANCE_INVALID")
        aggregate = validate_aggregate_receipt(
            candidate_acceptance.get("aggregateReceipt")
        )
        profile_values = candidate_acceptance.get("profileReceipts")
        if type(profile_values) is not dict or set(profile_values) != {
            "FRESH_BASE",
            "DOCKER_BASE",
            "RUNTIME_BASE_OFFLINE",
        }:
            _formal_reject("FORMAL_CANDIDATE_ACCEPTANCE_INVALID")
        profiles = {
            profile: validate_profile_receipt(profile_values[profile])
            for profile in (
                "FRESH_BASE",
                "DOCKER_BASE",
                "RUNTIME_BASE_OFFLINE",
            )
        }
        candidate = loaded.candidate_input
        common = {
            "candidate_input_digest": loaded.verified["candidate_input_sha256"],
            "verified_candidate_digest": loaded.verified_digest,
            "qualification_run_id": candidate["qualification_run_id"],
            "qualification_run_attempt": 1,
            "source_sha": candidate["source_sha"],
            "source_tree": candidate["source_tree"],
            "candidate_version": candidate["candidate_version"],
        }
        result_keys = {
            "FRESH_BASE": "fresh_base",
            "DOCKER_BASE": "docker_base",
            "RUNTIME_BASE_OFFLINE": "runtime_base_offline",
        }
        plan_profiles = {item.profile: item for item in candidate_plan.profiles}
        if set(plan_profiles) != set(result_keys):
            _formal_reject("FORMAL_CANDIDATE_PLAN_INVALID")
        if (
            candidate_plan.verified_candidate_digest != loaded.verified_digest
            or candidate_plan.candidate_input_digest
            != loaded.verified["candidate_input_sha256"]
            or candidate_plan.qualification_run_id
            != candidate["qualification_run_id"]
            or candidate_plan.source_sha != candidate["source_sha"]
            or candidate_plan.source_tree != candidate["source_tree"]
            or candidate_plan.candidate_version != candidate["candidate_version"]
        ):
            _formal_reject("FORMAL_CANDIDATE_PLAN_INVALID")
        profile_digests: dict[str, str] = {}
        for profile, result_key in result_keys.items():
            receipt = profiles[profile]
            plan_profile = plan_profiles[profile]
            digest = sha256_bytes(canonical_json_bytes(receipt))
            if (
                receipt["profile"] != profile
                or receipt["result"] != "PASS"
                or any(receipt[key] != value for key, value in common.items())
                or receipt["base_vm_identity"] != candidate_plan.source_vm_digest
                or receipt["snapshot_identity"] != plan_profile.snapshot_identity
                or receipt["source_disk_graph_identity"]
                != candidate_plan.source_disk_graph_identity
                or receipt["snapshot_disk_graph_identity"]
                != plan_profile.snapshot_disk_graph_identity
                or receipt["source_vm_inventory_identity"]
                != candidate_plan.source_vm_inventory_identity
                or receipt["original_vm_pre_hashes"]
                != dict(candidate_plan.original_vm_hashes)
                or receipt["original_vm_post_hashes"]
                != dict(candidate_plan.original_vm_hashes)
                or aggregate["profile_results"][result_key]
                != {
                    "status": "PASS",
                    "failure_code": None,
                    "receipt_digest": digest,
                }
            ):
                _formal_reject("FORMAL_CANDIDATE_ACCEPTANCE_INVALID")
            profile_digests[result_key] = digest
        if (
            candidate_acceptance.get("status") != "PASS"
            or aggregate["result"] != "PASS"
            or aggregate["all_profiles_pass"] is not True
            or any(aggregate[key] != value for key, value in common.items())
            or aggregate["base_vm_identity"] != candidate_plan.source_vm_digest
            or aggregate["source_vm_inventory_identity"]
            != candidate_plan.source_vm_inventory_identity
            or aggregate["source_disk_graph_identity"]
            != candidate_plan.source_disk_graph_identity
            or aggregate["original_vm_hashes"]
            != dict(candidate_plan.original_vm_hashes)
            or aggregate["snapshot_identities"]
            != {
                item.profile: item.snapshot_identity
                for item in candidate_plan.profiles
            }
            or aggregate["snapshot_disk_graph_identities"]
            != {
                item.profile: item.snapshot_disk_graph_identity
                for item in candidate_plan.profiles
            }
        ):
            _formal_reject("FORMAL_CANDIDATE_ACCEPTANCE_INVALID")
        aggregate_digest = aggregate_receipt_digest(aggregate)
        if candidate_acceptance.get("aggregateReceiptSha256") not in {
            None,
            aggregate_digest,
        }:
            _formal_reject("FORMAL_CANDIDATE_ACCEPTANCE_INVALID")
        profile_root = loaded.materials.material(
            f"{FORMAL_WINDOWS_PRETRUST_PREFIX}/formal-windows-trust-profile.json"
        ).parent
    except FormalProducerError:
        if material_authority is not None:
            material_authority.close()
        raise
    except (
        CandidateContractError,
        CandidateHarnessError,
        FormalWindowsPretrustError,
        OSError,
    ) as error:
        if material_authority is not None:
            material_authority.close()
        raise FormalProducerError("FORMAL_CANDIDATE_ACCEPTANCE_INVALID") from error
    except BaseException:
        # The continuation has transferred ownership of the held Candidate
        # material to this function.  No validation-time exception, including
        # process-control exceptions, may strand its Windows handles.
        if material_authority is not None:
            material_authority.close()
        raise
    try:
        return _issue_qualified_candidate_formal_authority(
            loaded=loaded,
            candidate_material_authority=material_authority,
            candidate_plan_digest=candidate_plan.plan_digest,
            candidate_provider_execution_authority_receipt_digest=(
                provider_execution_receipt.receipt_digest
            ),
            candidate_aggregate_receipt_digest=aggregate_digest,
            candidate_profile_receipt_digests=profile_digests,
            base_vm_identity=candidate_plan.source_vm_digest,
            original_vm_hashes=candidate_plan.original_vm_hashes,
            snapshot_identities={
                item.profile: item.snapshot_identity
                for item in candidate_plan.profiles
            },
            source_disk_graph_identity=candidate_plan.source_disk_graph_identity,
            snapshot_disk_graph_identities={
                item.profile: item.snapshot_disk_graph_identity
                for item in candidate_plan.profiles
            },
            source_vm_inventory_identity=(
                candidate_plan.source_vm_inventory_identity
            ),
            formal_windows_pretrust_root=profile_root,
        )
    except BaseException:
        material_authority.close()
        raise


@dataclass(frozen=True)
class VerifiedFormalRcAuthority:
    repository: str
    rc_tag: str
    verified_candidate_digest: str
    source_sha: str
    source_tree: str
    release_manifest_identity: str
    deployment_contract_identity: str
    installer_materials_identity: str
    formal_windows_pretrust_kit_identity: str
    offline_release_trust_profile_identity: str
    api_digest: str
    web_digest: str
    publication_identity: str
    workflow_identity: str
    attestation_claim_identities: Mapping[str, str]
    provenance_preflight_digest: str
    actions_preflight_receipt_digest: str
    provenance_claim_summaries: Mapping[str, Mapping[str, str]]
    publication_preflight_summary: Mapping[str, str]
    pretrusted_profile_identity: str
    provenance_verifier_identity: str
    github_trusted_root_identity: str
    sigstore_trusted_root_identity: str
    publication_execution_receipt_identity: str
    publication_signed_claim_identity: str
    publication_signed_at: str
    candidate_aggregate_receipt_digest: str
    candidate_profile_receipt_digests: Mapping[str, str]
    candidate_plan_digest: str
    candidate_provider_execution_authority_receipt_digest: str
    candidate_material_authority_identity: str
    candidate_material_tree_inventory_identity: str
    candidate_base_vm_identity: str
    candidate_original_vm_hashes: Mapping[str, str]
    candidate_snapshot_identities: Mapping[str, str]
    candidate_source_disk_graph_identity: str
    candidate_snapshot_disk_graph_identities: Mapping[str, str]
    candidate_source_vm_inventory_identity: str
    candidate_source_vm_authority_identity: str
    identity: str

    @classmethod
    def issue(
        cls,
        request: FormalAuthorityRequest,
        *,
        provenance_preflight_digest: str,
        actions_preflight_receipt_digest: str,
        provenance_claim_summaries: Mapping[str, Mapping[str, str]],
        publication_preflight_summary: Mapping[str, str],
        pretrusted_profile_identity: str,
        provenance_verifier_identity: str,
        github_trusted_root_identity: str,
        sigstore_trusted_root_identity: str,
        publication_execution_receipt_identity: str,
        publication_signed_claim_identity: str,
        publication_signed_at: str,
        candidate_aggregate_receipt_digest: str,
        candidate_profile_receipt_digests: Mapping[str, str],
        candidate_plan_digest: str,
        candidate_provider_execution_authority_receipt_digest: str,
        candidate_base_vm_identity: str,
        candidate_original_vm_hashes: Mapping[str, str],
        candidate_snapshot_identities: Mapping[str, str],
        candidate_source_disk_graph_identity: str,
        candidate_snapshot_disk_graph_identities: Mapping[str, str],
        candidate_source_vm_inventory_identity: str,
        candidate_material_authority_identity: str = "sha256:" + "0" * 64,
        candidate_material_tree_inventory_identity: str = "sha256:" + "0" * 64,
    ) -> VerifiedFormalRcAuthority:
        if (
            type(request) is not FormalAuthorityRequest
            or not _is_digest(provenance_preflight_digest)
            or not _is_digest(actions_preflight_receipt_digest)
            or not _is_digest(publication_execution_receipt_identity)
            or not _is_digest(publication_signed_claim_identity)
            or not _is_digest(pretrusted_profile_identity)
            or not _is_digest(provenance_verifier_identity)
            or not _is_digest(github_trusted_root_identity)
            or not _is_digest(sigstore_trusted_root_identity)
            or not _is_digest(candidate_aggregate_receipt_digest)
            or not _is_digest(candidate_plan_digest)
            or not _is_digest(
                candidate_provider_execution_authority_receipt_digest
            )
            or not _is_digest(candidate_material_authority_identity)
            or not _is_digest(candidate_material_tree_inventory_identity)
        ):
            _formal_reject("FORMAL_RC_AUTHORITY_INVALID")
        closed_publication_summary = dict(publication_preflight_summary)
        if set(closed_publication_summary) != {
            "verifier_digest",
            "bundle_digest",
            "trusted_root_digest",
            "request_digest",
            "claim_digest",
        } or any(
            not _is_digest(value) for value in closed_publication_summary.values()
        ):
            _formal_reject("FORMAL_RC_AUTHORITY_INVALID")
        closed_summaries = {
            name: dict(summary) for name, summary in provenance_claim_summaries.items()
        }
        if set(closed_summaries) != REQUIRED_EVIDENCE or any(
            set(summary)
            != {
                "claim_digest",
                "bundle_digest",
                "trusted_root_digest",
                "request_digest",
            }
            or any(not _is_digest(value) for value in summary.values())
            for summary in closed_summaries.values()
        ):
            _formal_reject("FORMAL_RC_AUTHORITY_INVALID")
        _canonical_utc_seconds(publication_signed_at)
        closed_candidate_profile_receipts = dict(candidate_profile_receipt_digests)
        if tuple(sorted(closed_candidate_profile_receipts)) != tuple(
            sorted(CANDIDATE_PROFILE_RESULT_KEYS)
        ) or any(
            not _is_digest(value)
            for value in closed_candidate_profile_receipts.values()
        ):
            _formal_reject("FORMAL_CANDIDATE_ACCEPTANCE_INVALID")
        candidate_source_vm_authority_identity = (
            _candidate_source_vm_authority_identity(
                base_vm_identity=candidate_base_vm_identity,
                original_vm_hashes=candidate_original_vm_hashes,
                snapshot_identities=candidate_snapshot_identities,
                source_disk_graph_identity=candidate_source_disk_graph_identity,
                snapshot_disk_graph_identities=(
                    candidate_snapshot_disk_graph_identities
                ),
                source_vm_inventory_identity=(
                    candidate_source_vm_inventory_identity
                ),
            )
        )
        body = request.identity_body()
        return cls(
            repository=request.repository,
            rc_tag=request.rc_tag,
            verified_candidate_digest=request.verified_candidate_digest,
            source_sha=request.source_sha,
            source_tree=request.source_tree,
            release_manifest_identity=request.release_manifest_identity,
            deployment_contract_identity=request.deployment_contract_identity,
            installer_materials_identity=request.installer_materials_identity,
            formal_windows_pretrust_kit_identity=(
                request.formal_windows_pretrust_kit_identity
            ),
            offline_release_trust_profile_identity=(
                request.offline_release_trust_profile_identity
            ),
            api_digest=request.api_digest,
            web_digest=request.web_digest,
            publication_identity=request.publication_identity,
            workflow_identity=request.workflow_identity,
            attestation_claim_identities=dict(request.attestation_claim_identities),
            provenance_preflight_digest=provenance_preflight_digest,
            actions_preflight_receipt_digest=actions_preflight_receipt_digest,
            provenance_claim_summaries=closed_summaries,
            publication_preflight_summary=closed_publication_summary,
            pretrusted_profile_identity=pretrusted_profile_identity,
            provenance_verifier_identity=provenance_verifier_identity,
            github_trusted_root_identity=github_trusted_root_identity,
            sigstore_trusted_root_identity=sigstore_trusted_root_identity,
            publication_execution_receipt_identity=(
                publication_execution_receipt_identity
            ),
            publication_signed_claim_identity=publication_signed_claim_identity,
            publication_signed_at=publication_signed_at,
            candidate_aggregate_receipt_digest=(candidate_aggregate_receipt_digest),
            candidate_profile_receipt_digests=(closed_candidate_profile_receipts),
            candidate_plan_digest=candidate_plan_digest,
            candidate_provider_execution_authority_receipt_digest=(
                candidate_provider_execution_authority_receipt_digest
            ),
            candidate_material_authority_identity=(
                candidate_material_authority_identity
            ),
            candidate_material_tree_inventory_identity=(
                candidate_material_tree_inventory_identity
            ),
            candidate_base_vm_identity=candidate_base_vm_identity,
            candidate_original_vm_hashes=dict(candidate_original_vm_hashes),
            candidate_snapshot_identities=dict(candidate_snapshot_identities),
            candidate_source_disk_graph_identity=(
                candidate_source_disk_graph_identity
            ),
            candidate_snapshot_disk_graph_identities=dict(
                candidate_snapshot_disk_graph_identities
            ),
            candidate_source_vm_inventory_identity=(
                candidate_source_vm_inventory_identity
            ),
            candidate_source_vm_authority_identity=(
                candidate_source_vm_authority_identity
            ),
            identity=sha256_bytes(canonical_json_bytes(body)),
        )

    def identity_body(self) -> dict[str, object]:
        request = FormalAuthorityRequest(
            repository=self.repository,
            rc_tag=self.rc_tag,
            verified_candidate_digest=self.verified_candidate_digest,
            source_sha=self.source_sha,
            source_tree=self.source_tree,
            release_manifest_identity=self.release_manifest_identity,
            deployment_contract_identity=self.deployment_contract_identity,
            installer_materials_identity=self.installer_materials_identity,
            formal_windows_pretrust_kit_identity=(
                self.formal_windows_pretrust_kit_identity
            ),
            offline_release_trust_profile_identity=(
                self.offline_release_trust_profile_identity
            ),
            api_digest=self.api_digest,
            web_digest=self.web_digest,
            publication_identity=self.publication_identity,
            workflow_identity=self.workflow_identity,
            attestation_claim_identities=self.attestation_claim_identities,
        )
        return request.identity_body()


@dataclass(frozen=True)
class FormalExecutionContext:
    accepted_at: str
    observed_at: str
    operator_identity: str
    run_id: str
    run_attempt: int
    correlation_id: str
    current_workflow_commit: str
    execution_environment: str
    tool_identity: str

    def __post_init__(self) -> None:
        _canonical_utc_seconds(self.accepted_at)
        _canonical_utc_seconds(self.observed_at)
        if (
            not _closed_text(self.operator_identity, maximum=200)
            or not _closed_text(self.run_id)
            or type(self.run_attempt) is not int
            or self.run_attempt < 1
            or not _closed_text(self.correlation_id)
            or COMMIT_IDENTITY.fullmatch(self.current_workflow_commit) is None
            or not _closed_text(self.execution_environment)
            or not _is_digest(self.tool_identity)
        ):
            _formal_reject("FORMAL_EXECUTION_CONTEXT_INVALID")


@dataclass(frozen=True)
class FormalProfileObservation:
    """Actual guest/host observation returned by the profile executor boundary."""

    profile: str
    rc_authority_identity: str
    transport_source: str
    resolved_version: str
    resolved_source_sha: str
    resolved_manifest_identity: str
    resolved_deployment_contract_identity: str
    resolved_installer_materials_identity: str
    resolved_api_digest: str
    resolved_web_digest: str
    resolved_publication_identity: str
    resolved_workflow_identity: str
    resolved_attestation_claim_identities: Mapping[str, str]
    base_vm_identity: str
    snapshot_identity: str
    clone_identity: str
    provider_execution_authority_receipt_digest: str
    publication_execution_receipt_identity: str
    publication_signed_claim_identity: str
    publication_signed_at: str
    formal_windows_pretrust_kit_identity: str
    offline_release_trust_profile_identity: str
    pretrusted_profile_identity: str
    provenance_verifier_identity: str
    github_trusted_root_identity: str
    sigstore_trusted_root_identity: str
    platform_plan_digest: str
    platform_receipt_digest: str
    installer_plan_digest: str
    installer_execution_receipt_digest: str
    doctor_receipt_digest: str | None
    canonical_acceptance_receipt_digests: tuple[str, ...]
    continuation_receipt_digest: str
    result: str


class FormalAuthorityVerifier(Protocol):
    def verify(self, request: FormalAuthorityRequest) -> VerifiedFormalRcAuthority: ...


class FormalProfileExecutor(Protocol):
    def execute(
        self,
        *,
        authority: VerifiedFormalRcAuthority,
        profile: str,
    ) -> FormalProfileObservation: ...


@dataclass(frozen=True)
class FormalProvenanceInput:
    evidence_name: str
    bundle: Path
    trusted_root: Path | None
    request: Path


@dataclass(frozen=True)
class FormalProvenancePlan:
    verifier: Path | None
    inputs: tuple[FormalProvenanceInput, ...]
    publication: FormalProvenanceInput | None = None
    pretrusted_trust_material_root: Path | None = None
    installer_materials: Path | None = None
    private_work_root: Path | None = None
    candidate_aggregate_receipt_digest: str | None = None
    candidate_profile_receipt_digests: Mapping[str, str] | None = None
    qualified_candidate: QualifiedCandidateFormalAuthority | None = None


VerifierRunner = Callable[[tuple[str, ...]], bytes]


def _reject(code: str) -> None:
    raise ProvenancePreflightError(code)


def _read_bound_file(
    path: Path, *, maximum: int, executable: bool = False
) -> tuple[Path, bytes]:
    candidate = Path(path)
    try:
        metadata = candidate.lstat()
    except OSError as error:
        raise ProvenancePreflightError("FORMAL_PROVENANCE_INPUT_UNAVAILABLE") from error
    if (
        candidate.is_symlink()
        or bool(getattr(candidate, "is_junction", lambda: False)())
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or metadata.st_size < 1
        or metadata.st_size > maximum
        or (executable and os.name == "posix" and metadata.st_mode & 0o111 == 0)
    ):
        _reject("FORMAL_PROVENANCE_INPUT_UNSAFE")
    try:
        resolved = candidate.resolve(strict=True)
        with candidate.open("rb") as handle:
            opened = os.fstat(handle.fileno())
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_nlink != 1
                or (metadata.st_dev, metadata.st_ino) != (opened.st_dev, opened.st_ino)
            ):
                _reject("FORMAL_PROVENANCE_INPUT_REBOUND")
            value = handle.read(maximum + 1)
            after = os.fstat(handle.fileno())
    except ProvenancePreflightError:
        raise
    except OSError as error:
        raise ProvenancePreflightError("FORMAL_PROVENANCE_INPUT_UNAVAILABLE") from error
    if (
        len(value) < 1
        or len(value) > maximum
        or (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)
        != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    ):
        _reject("FORMAL_PROVENANCE_INPUT_REBOUND")
    return resolved, value


def _write_private_snapshot(
    root: Path,
    name: str,
    value: bytes,
    *,
    executable: bool = False,
    enforce_windows_acl: bool = False,
) -> Path:
    if enforce_windows_acl:
        try:
            assert_windows_private_acl(Path(root).resolve(strict=True))
        except (OSError, FormalWindowsPretrustError) as error:
            raise ProvenancePreflightError(
                "FORMAL_PROVENANCE_SNAPSHOT_AUTHORITY_INVALID"
            ) from error
    path = root / name
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    mode = 0o700 if executable else 0o600
    try:
        descriptor = os.open(path, flags, mode)
        with os.fdopen(descriptor, "wb", closefd=True) as output:
            output.write(value)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(path, mode)
        resolved, observed = _read_bound_file(
            path,
            maximum=MAX_VERIFIER_BYTES if executable else MAX_INPUT_BYTES,
            executable=executable,
        )
    except ProvenancePreflightError:
        raise
    except OSError as error:
        raise ProvenancePreflightError(
            "FORMAL_PROVENANCE_SNAPSHOT_UNAVAILABLE"
        ) from error
    if observed != value:
        _reject("FORMAL_PROVENANCE_SNAPSHOT_REBOUND")
    if enforce_windows_acl:
        try:
            assert_windows_private_acl(resolved)
        except (OSError, FormalWindowsPretrustError) as error:
            raise ProvenancePreflightError(
                "FORMAL_PROVENANCE_SNAPSHOT_AUTHORITY_INVALID"
            ) from error
    return resolved


def _assert_snapshot_bytes(
    path: Path,
    expected: bytes,
    *,
    executable: bool = False,
    enforce_windows_acl: bool = False,
) -> None:
    if enforce_windows_acl:
        try:
            assert_windows_private_acl(path.resolve(strict=True))
        except (OSError, FormalWindowsPretrustError) as error:
            raise ProvenancePreflightError(
                "FORMAL_PROVENANCE_SNAPSHOT_AUTHORITY_INVALID"
            ) from error
    _, observed = _read_bound_file(
        path,
        maximum=MAX_VERIFIER_BYTES if executable else MAX_INPUT_BYTES,
        executable=executable,
    )
    if observed != expected:
        _reject("FORMAL_PROVENANCE_SNAPSHOT_REBOUND")


def _production_verifier_runner(command: tuple[str, ...]) -> bytes:
    if not command:
        raise ProvenancePreflightError(
            "FORMAL_PROVENANCE_VERIFIER_UNAVAILABLE"
        )
    try:
        executable = Path(command[0]).resolve(strict=True)
    except (OSError, ValueError) as error:
        raise ProvenancePreflightError(
            "FORMAL_PROVENANCE_VERIFIER_UNAVAILABLE"
        ) from error
    if not executable.is_absolute() or executable != Path(command[0]):
        raise ProvenancePreflightError(
            "FORMAL_PROVENANCE_VERIFIER_UNAVAILABLE"
        )
    path_entries = [str(executable.parent)]
    if os.name == "nt":
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetSystemDirectoryW.argtypes = (
            wintypes.LPWSTR,
            wintypes.UINT,
        )
        kernel32.GetSystemDirectoryW.restype = wintypes.UINT
        buffer = ctypes.create_unicode_buffer(32768)
        length = kernel32.GetSystemDirectoryW(buffer, len(buffer))
        if length == 0 or length >= len(buffer):
            raise ProvenancePreflightError(
                "FORMAL_PROVENANCE_VERIFIER_UNAVAILABLE"
            )
        path_entries.append(buffer.value)
    environment = {
        "PATH": os.pathsep.join(path_entries),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
    }
    try:
        completed = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=False,
            shell=False,
            cwd=executable.parent,
            timeout=60,
            env=environment,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise ProvenancePreflightError(
            "FORMAL_PROVENANCE_VERIFIER_UNAVAILABLE"
        ) from error
    if completed.returncode != 0:
        _reject("FORMAL_PROVENANCE_VERIFICATION_FAILED")
    if len(completed.stdout) < 2 or len(completed.stdout) > MAX_CLAIM_BYTES:
        _reject("FORMAL_PROVENANCE_CLAIM_INVALID")
    return completed.stdout


def _closed_claim(value: bytes) -> Mapping[str, object]:
    try:
        decoded = value.decode("utf-8")
        claim = json.loads(decoded, object_pairs_hook=reject_duplicate_json_keys)
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as error:
        raise ProvenancePreflightError("FORMAL_PROVENANCE_CLAIM_INVALID") from error
    if (
        not isinstance(claim, dict)
        or claim.get("schemaVersion") != 1
        or canonical_json_bytes(claim) != value
    ):
        _reject("FORMAL_PROVENANCE_CLAIM_INVALID")
    return claim


def _closed_actions_request(value: bytes, evidence_name: str) -> Mapping[str, object]:
    try:
        request = json.loads(
            value.decode("utf-8"), object_pairs_hook=reject_duplicate_json_keys
        )
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as error:
        raise ProvenancePreflightError(
            "FORMAL_PROVENANCE_EVIDENCE_BINDING_INVALID"
        ) from error
    expected_subject = EXPECTED_SUBJECT_BY_EVIDENCE.get(evidence_name)
    if (
        not isinstance(request, dict)
        or set(request)
        != {
            "schemaVersion",
            "mode",
            "evidenceName",
            "subject",
            "workflow",
            "sourceCommit",
        }
        or request.get("schemaVersion") != 1
        or request.get("mode") != "actions-provenance"
        or request.get("evidenceName") != evidence_name
        or request.get("workflow")
        not in {
            ".github/workflows/release.yml",
            ".github/workflows/promote-release.yml",
        }
        or not isinstance(request.get("sourceCommit"), str)
        or not COMMIT_IDENTITY.fullmatch(request["sourceCommit"])
        or not isinstance(request.get("subject"), dict)
        or set(request["subject"]) != {"name", "sha256", "size"}
        or request["subject"].get("name") != expected_subject
        or not isinstance(request["subject"].get("sha256"), str)
        or not SHA256_IDENTITY.fullmatch(request["subject"]["sha256"])
        or type(request["subject"].get("size")) is not int
        or request["subject"]["size"] < 0
    ):
        _reject("FORMAL_PROVENANCE_EVIDENCE_BINDING_INVALID")
    return request


def _bind_actions_claim(
    claim: Mapping[str, object],
    request: Mapping[str, object],
    evidence_name: str,
) -> Mapping[str, object]:
    subject = request["subject"]
    expected_claim_subject = {
        "name": subject["name"],
        "sha256": subject["sha256"],
    }
    source = claim.get("source")
    if (
        request["evidenceName"] != evidence_name
        or claim.get("subject") != expected_claim_subject
        or claim.get("repository") != EXPECTED_REPOSITORY
        or claim.get("workflow") != request["workflow"]
        or not isinstance(source, dict)
        or source.get("commit") != request["sourceCommit"]
        or source.get("ref") != "refs/heads/main"
        or claim.get("signerDigest") != request["sourceCommit"]
    ):
        _reject("FORMAL_PROVENANCE_EVIDENCE_BINDING_INVALID")
    return {
        "evidence_name": evidence_name,
        "subject": expected_claim_subject,
        "workflow": request["workflow"],
        "source_commit": request["sourceCommit"],
    }


class OfflineActionsProvenancePreflight:
    """Verify all Formal evidence roles without granting clone authority."""

    def __init__(
        self,
        plan: FormalProvenancePlan,
        *,
        runner: VerifierRunner = _production_verifier_runner,
    ) -> None:
        self._plan = plan
        self._runner = runner

    def verify(self) -> Mapping[str, object]:
        names = [item.evidence_name for item in self._plan.inputs]
        if (
            len(names) != len(REQUIRED_EVIDENCE)
            or set(names) != REQUIRED_EVIDENCE
            or self._plan.verifier is None
            or any(item.trusted_root is None for item in self._plan.inputs)
        ):
            _reject("FORMAL_PROVENANCE_EVIDENCE_SET_INVALID")
        _, verifier_bytes = _read_bound_file(
            self._plan.verifier, maximum=MAX_VERIFIER_BYTES, executable=True
        )
        claims: list[dict[str, object]] = []
        enforce_acl = self._plan.private_work_root is not None
        if enforce_acl:
            try:
                snapshot_root = create_windows_private_directory(
                    Path(self._plan.private_work_root),
                    prefix="animemo-formal-provenance",
                )
            except (OSError, FormalWindowsPretrustError) as error:
                raise ProvenancePreflightError(
                    "FORMAL_PROVENANCE_SNAPSHOT_AUTHORITY_INVALID"
                ) from error
        else:
            snapshot_root = Path(tempfile.mkdtemp(prefix="animemo-formal-provenance-"))
            os.chmod(snapshot_root, 0o700)
        try:
            verifier_name = "verifier.exe" if os.name == "nt" else "verifier"
            verifier = _write_private_snapshot(
                snapshot_root,
                verifier_name,
                verifier_bytes,
                executable=True,
                enforce_windows_acl=enforce_acl,
            )
            bound_inputs = []
            for item in sorted(
                self._plan.inputs, key=lambda value: value.evidence_name
            ):
                _, bundle_bytes = _read_bound_file(item.bundle, maximum=MAX_INPUT_BYTES)
                _, trusted_root_bytes = _read_bound_file(
                    item.trusted_root, maximum=MAX_INPUT_BYTES
                )
                _, request_bytes = _read_bound_file(
                    item.request, maximum=MAX_INPUT_BYTES
                )
                closed_request = _closed_actions_request(
                    request_bytes, item.evidence_name
                )
                bound_inputs.append(
                    {
                        "evidence_name": item.evidence_name,
                        "bundle": _write_private_snapshot(
                            snapshot_root,
                            f"{item.evidence_name}.bundle.json",
                            bundle_bytes,
                            enforce_windows_acl=enforce_acl,
                        ),
                        "bundle_bytes": bundle_bytes,
                        "trusted_root": _write_private_snapshot(
                            snapshot_root,
                            f"{item.evidence_name}.root.json",
                            trusted_root_bytes,
                            enforce_windows_acl=enforce_acl,
                        ),
                        "trusted_root_bytes": trusted_root_bytes,
                        "request": _write_private_snapshot(
                            snapshot_root,
                            f"{item.evidence_name}.request.json",
                            request_bytes,
                            enforce_windows_acl=enforce_acl,
                        ),
                        "request_bytes": request_bytes,
                        "closed_request": closed_request,
                    }
                )
            relative_files = tuple(
                sorted(
                    path.name
                    for path in (
                        verifier,
                        *(
                            item[key]
                            for item in bound_inputs
                            for key in ("bundle", "trusted_root", "request")
                        ),
                    )
                )
            )
            snapshot_hold = (
                hold_windows_private_snapshot(
                    snapshot_root, relative_files=relative_files
                )
                if enforce_acl
                else nullcontext(snapshot_root)
            )
            with snapshot_hold:
                for bound in bound_inputs:
                    command = (
                        str(verifier),
                        "--bundle",
                        str(bound["bundle"]),
                        "--trusted-root",
                        str(bound["trusted_root"]),
                        "--request",
                        str(bound["request"]),
                    )
                    try:
                        claim = _closed_claim(self._runner(command))
                        _assert_snapshot_bytes(
                            verifier,
                            verifier_bytes,
                            executable=True,
                            enforce_windows_acl=enforce_acl,
                        )
                        _assert_snapshot_bytes(
                            bound["bundle"],
                            bound["bundle_bytes"],
                            enforce_windows_acl=enforce_acl,
                        )
                        _assert_snapshot_bytes(
                            bound["trusted_root"],
                            bound["trusted_root_bytes"],
                            enforce_windows_acl=enforce_acl,
                        )
                        _assert_snapshot_bytes(
                            bound["request"],
                            bound["request_bytes"],
                            enforce_windows_acl=enforce_acl,
                        )
                        binding = _bind_actions_claim(
                            claim,
                            bound["closed_request"],
                            bound["evidence_name"],
                        )
                    except ProvenancePreflightError:
                        raise
                    except Exception as error:
                        raise ProvenancePreflightError(
                            "FORMAL_PROVENANCE_VERIFIER_UNAVAILABLE"
                        ) from error
                    claims.append(
                        {
                            **binding,
                            "bundle_digest": sha256_bytes(bound["bundle_bytes"]),
                            "trusted_root_digest": sha256_bytes(
                                bound["trusted_root_bytes"]
                            ),
                            "request_digest": sha256_bytes(bound["request_bytes"]),
                            "claim_digest": sha256_bytes(canonical_json_bytes(claim)),
                        }
                    )
        finally:
            shutil.rmtree(snapshot_root, ignore_errors=True)
        unsigned: dict[str, object] = {
            "schema": PREFLIGHT_SCHEMA,
            "verifier_digest": sha256_bytes(verifier_bytes),
            "claims": claims,
            "clone_authorized": False,
            "release_authority_granted": False,
            "publish_authorized": False,
        }
        return {
            **unsigned,
            "preflight_digest": sha256_bytes(canonical_json_bytes(unsigned)),
        }


def _schema(name: str) -> dict[str, Any]:
    path = Path(__file__).with_name(name)
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=reject_duplicate_json_keys,
        )
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as error:
        raise FormalProducerError("FORMAL_SCHEMA_UNAVAILABLE") from error
    if type(value) is not dict:
        _formal_reject("FORMAL_SCHEMA_INVALID")
    return value


def _validate_formal_schema(
    value: object,
    name: str,
    *,
    code: str,
) -> dict[str, Any]:
    errors = sorted(
        Draft202012Validator(_schema(name)).iter_errors(value),
        key=lambda item: tuple(str(part) for part in item.absolute_path),
    )
    if errors or type(value) is not dict:
        _formal_reject(code)
    return dict(value)


def validate_formal_profile_receipt(value: object) -> dict[str, Any]:
    receipt = _validate_formal_schema(
        value,
        "formal-profile-receipt.schema.json",
        code="FORMAL_PROFILE_RECEIPT_INVALID",
    )
    unsigned = dict(receipt)
    receipt_digest = unsigned.pop("receipt_digest")
    if receipt_digest != sha256_bytes(canonical_json_bytes(unsigned)):
        _formal_reject("FORMAL_PROFILE_RECEIPT_DIGEST_MISMATCH")
    authority_body = {
        "schema": FORMAL_PROFILE_AUTHORITY_SCHEMA,
        "rc_authority_identity": receipt["rc_authority_identity"],
        "profile": receipt["profile"],
        "transport_source": receipt["transport_source"],
        "resolved_release": receipt["resolved_release"],
        "canonical_acceptance_receipt_digests": receipt[
            "canonical_acceptance_receipt_digests"
        ],
        "result": receipt["result"],
        "release_authority_granted": False,
        "publish_authorized": False,
    }
    if receipt["profile_authority_identity"] != sha256_bytes(
        canonical_json_bytes(authority_body)
    ):
        _formal_reject("FORMAL_PROFILE_AUTHORITY_IDENTITY_MISMATCH")
    execution_body = {
        "schema": "animemo.formal-profile-execution/v1",
        "profile": receipt["profile"],
        "base_vm_identity": receipt["base_vm_identity"],
        "snapshot_identity": receipt["snapshot_identity"],
        "clone_identity": receipt["clone_identity"],
        "provider_execution_authority_receipt_digest": receipt[
            "provider_execution_authority_receipt_digest"
        ],
        "publication_execution_receipt_identity": receipt[
            "publication_execution_receipt_identity"
        ],
        "publication_signed_claim_identity": receipt[
            "publication_signed_claim_identity"
        ],
        "publication_signed_at": receipt["publication_signed_at"],
        "formal_windows_pretrust_kit_identity": receipt[
            "formal_windows_pretrust_kit_identity"
        ],
        "offline_release_trust_profile_identity": receipt[
            "offline_release_trust_profile_identity"
        ],
        "pretrusted_profile_identity": receipt["pretrusted_profile_identity"],
        "provenance_verifier_identity": receipt["provenance_verifier_identity"],
        "github_trusted_root_identity": receipt["github_trusted_root_identity"],
        "sigstore_trusted_root_identity": receipt["sigstore_trusted_root_identity"],
        "platform_plan_digest": receipt["platform_plan_digest"],
        "platform_receipt_digest": receipt["platform_receipt_digest"],
        "installer_plan_digest": receipt["installer_plan_digest"],
        "installer_execution_receipt_digest": receipt[
            "installer_execution_receipt_digest"
        ],
        "doctor_receipt_digest": receipt["doctor_receipt_digest"],
        "continuation_receipt_digest": receipt["continuation_receipt_digest"],
        "result": receipt["result"],
    }
    if receipt["execution_receipt_digest"] != sha256_bytes(
        canonical_json_bytes(execution_body)
    ):
        _formal_reject("FORMAL_PROFILE_EXECUTION_DIGEST_MISMATCH")
    return receipt


def validate_formal_profile_status_receipt(value: object) -> dict[str, Any]:
    receipt = _validate_formal_schema(
        value,
        "formal-profile-status-receipt.schema.json",
        code="FORMAL_PROFILE_STATUS_RECEIPT_INVALID",
    )
    unsigned = dict(receipt)
    receipt_digest = unsigned.pop("receipt_digest")
    if receipt_digest != sha256_bytes(canonical_json_bytes(unsigned)):
        _formal_reject("FORMAL_PROFILE_STATUS_RECEIPT_DIGEST_MISMATCH")
    return receipt


def _build_formal_profile_status_receipt(
    *,
    rc_authority_identity: str,
    profile: str,
    result: Mapping[str, str | None],
) -> dict[str, Any]:
    unsigned = {
        "schema": "animemo.formal-profile-status-receipt/v1",
        "version": 1,
        "rc_authority_identity": rc_authority_identity,
        "profile": profile,
        "status": result["status"],
        "failure_code": result["failure_code"],
        "continuation_receipt_digest": result[
            "continuation_receipt_digest"
        ],
        "release_authority_granted": False,
        "publish_authorized": False,
    }
    return validate_formal_profile_status_receipt(
        {
            **unsigned,
            "receipt_digest": sha256_bytes(canonical_json_bytes(unsigned)),
        }
    )


def validate_formal_aggregate_receipt(value: object) -> dict[str, Any]:
    receipt = _validate_formal_schema(
        value,
        "formal-acceptance-receipt.schema.json",
        code="FORMAL_ACCEPTANCE_RECEIPT_INVALID",
    )
    unsigned = dict(receipt)
    receipt_digest = unsigned.pop("receipt_digest")
    if receipt_digest != sha256_bytes(canonical_json_bytes(unsigned)):
        _formal_reject("FORMAL_ACCEPTANCE_RECEIPT_DIGEST_MISMATCH")
    authority_body = {
        "schema": "animemo.formal-acceptance-authority/v1",
        "rc_authority_identity": receipt["rc_authority_identity"],
        "profile_authority_identities": receipt["profile_authority_identities"],
        "all_profiles_pass": receipt["all_profiles_pass"],
        "result": receipt["result"],
        "release_authority_granted": False,
        "publish_authorized": False,
    }
    if receipt["formal_authority_identity"] != sha256_bytes(
        canonical_json_bytes(authority_body)
    ):
        _formal_reject("FORMAL_AUTHORITY_IDENTITY_MISMATCH")
    statuses = [
        receipt["profile_results"][FORMAL_PROFILE_RESULT_KEYS[profile]]["status"]
        for profile in FORMAL_PROFILES
    ]
    all_pass = statuses == ["PASS", "PASS", "PASS"]
    if receipt["all_profiles_pass"] is not all_pass or receipt["result"] != (
        "PASS" if all_pass else "FAIL"
    ):
        _formal_reject("FORMAL_ACCEPTANCE_RESULT_MISMATCH")
    return receipt


def validate_formal_execution_receipt(value: object) -> dict[str, Any]:
    receipt = _validate_formal_schema(
        value,
        "formal-execution-receipt.schema.json",
        code="FORMAL_EXECUTION_RECEIPT_INVALID",
    )
    _canonical_utc_seconds(receipt["accepted_at"])
    _canonical_utc_seconds(receipt["observed_at"])
    unsigned = dict(receipt)
    receipt_digest = unsigned.pop("receipt_digest")
    if receipt_digest != sha256_bytes(canonical_json_bytes(unsigned)):
        _formal_reject("FORMAL_EXECUTION_RECEIPT_DIGEST_MISMATCH")
    return receipt


def validate_formal_acceptance_bundle(value: object) -> dict[str, Any]:
    """Validate the non-circular Formal evidence embedded by acceptance v2.

    The returned four-key bundle is derived only after every schema, digest,
    profile cardinality, RC binding, and aggregate cross-reference is closed.
    No precomputed PASS or arbitrary Formal digest is accepted.
    """

    if type(value) is not dict or set(value) != {
        "rcLiveAcceptanceInput",
        "profileReceipts",
        "aggregateReceipt",
        "executionReceipt",
    }:
        _formal_reject("FORMAL_ACCEPTANCE_BUNDLE_INVALID")
    acceptance_input = _validate_formal_schema(
        value["rcLiveAcceptanceInput"],
        "formal-rc-live-acceptance-input.schema.json",
        code="FORMAL_ACCEPTANCE_INPUT_INVALID",
    )
    unsigned_input = dict(acceptance_input)
    input_digest = unsigned_input.pop("record_input_digest")
    if input_digest != sha256_bytes(canonical_json_bytes(unsigned_input)):
        _formal_reject("FORMAL_ACCEPTANCE_INPUT_DIGEST_MISMATCH")
    if (
        acceptance_input["producer_contract_identity"]
        != FORMAL_PRODUCER_CONTRACT_IDENTITY
    ):
        _formal_reject("FORMAL_PRODUCER_CONTRACT_MISMATCH")
    input_authority = FormalAuthorityRequest(
        repository=acceptance_input["repository"],
        rc_tag=acceptance_input["rc_tag"],
        verified_candidate_digest=acceptance_input["verified_candidate_digest"],
        source_sha=acceptance_input["source_sha"],
        source_tree=acceptance_input["source_tree"],
        release_manifest_identity=acceptance_input["release_manifest_identity"],
        deployment_contract_identity=acceptance_input["deployment_contract_identity"],
        installer_materials_identity=acceptance_input["installer_materials_identity"],
        formal_windows_pretrust_kit_identity=acceptance_input[
            "formal_windows_pretrust_kit_identity"
        ],
        offline_release_trust_profile_identity=acceptance_input[
            "offline_release_trust_profile_identity"
        ],
        api_digest=acceptance_input["api_digest"],
        web_digest=acceptance_input["web_digest"],
        publication_identity=acceptance_input["publication_identity"],
        workflow_identity=acceptance_input["workflow_identity"],
        attestation_claim_identities=acceptance_input["attestation_claim_identities"],
    )
    if acceptance_input["rc_authority_identity"] != sha256_bytes(
        canonical_json_bytes(input_authority.identity_body())
    ):
        _formal_reject("FORMAL_RC_AUTHORITY_BINDING_MISMATCH")
    profile_values = value["profileReceipts"]
    if type(profile_values) is not dict or set(profile_values) != set(FORMAL_PROFILES):
        _formal_reject("FORMAL_ACCEPTANCE_PROFILE_SET_INVALID")
    profiles = {
        profile: validate_formal_profile_receipt(profile_values[profile])
        for profile in FORMAL_PROFILES
    }
    if any(profiles[profile]["profile"] != profile for profile in FORMAL_PROFILES):
        _formal_reject("FORMAL_ACCEPTANCE_PROFILE_SET_INVALID")
    aggregate = validate_formal_aggregate_receipt(value["aggregateReceipt"])
    execution = validate_formal_execution_receipt(value["executionReceipt"])
    rc_authorities = {receipt["rc_authority_identity"] for receipt in profiles.values()}
    expected_authorities = {
        "formal_fresh": profiles["FORMAL_FRESH"]["profile_authority_identity"],
        "formal_docker": profiles["FORMAL_DOCKER"]["profile_authority_identity"],
        "formal_offline": profiles["FORMAL_OFFLINE"]["profile_authority_identity"],
    }
    expected_receipts = {
        "formal_fresh": profiles["FORMAL_FRESH"]["receipt_digest"],
        "formal_docker": profiles["FORMAL_DOCKER"]["receipt_digest"],
        "formal_offline": profiles["FORMAL_OFFLINE"]["receipt_digest"],
    }
    expected_executions = {
        "formal_fresh": profiles["FORMAL_FRESH"]["execution_receipt_digest"],
        "formal_docker": profiles["FORMAL_DOCKER"]["execution_receipt_digest"],
        "formal_offline": profiles["FORMAL_OFFLINE"]["execution_receipt_digest"],
    }
    expected_release = {
        "version": acceptance_input["rc_tag"],
        "source_sha": acceptance_input["source_sha"],
        "release_manifest_identity": acceptance_input["release_manifest_identity"],
        "deployment_contract_identity": acceptance_input[
            "deployment_contract_identity"
        ],
        "installer_materials_identity": acceptance_input[
            "installer_materials_identity"
        ],
        "api_digest": acceptance_input["api_digest"],
        "web_digest": acceptance_input["web_digest"],
        "publication_identity": acceptance_input["publication_identity"],
        "workflow_identity": acceptance_input["workflow_identity"],
        "attestation_claim_identities": acceptance_input[
            "attestation_claim_identities"
        ],
    }
    publication_executions = {
        receipt["publication_execution_receipt_identity"]
        for receipt in profiles.values()
    }
    publication_claims = {
        receipt["publication_signed_claim_identity"] for receipt in profiles.values()
    }
    publication_signed_times = {
        receipt["publication_signed_at"] for receipt in profiles.values()
    }
    expected_claim_digests = {
        name: summary["claim_digest"]
        for name, summary in execution["provenance_claim_summaries"].items()
    }
    expected_combined_preflight = sha256_bytes(
        canonical_json_bytes(
            {
                "schema": "animemo.formal-production-provenance/v1",
                "actions_preflight_digest": execution[
                    "actions_preflight_receipt_digest"
                ],
                "publication_authority_identity": acceptance_input[
                    "publication_identity"
                ],
                "publication_execution_receipt_identity": next(
                    iter(publication_executions), ""
                ),
                "publication_signed_claim_identity": next(iter(publication_claims), ""),
                "publication_preflight": execution["publication_preflight_summary"],
                "formal_windows_pretrust_kit_identity": execution[
                    "formal_windows_pretrust_kit_identity"
                ],
                "offline_release_trust_profile_identity": execution[
                    "offline_release_trust_profile_identity"
                ],
                "pretrusted_profile_identity": execution["pretrusted_profile_identity"],
                "provenance_verifier_identity": execution[
                    "provenance_verifier_identity"
                ],
                "github_trusted_root_identity": execution[
                    "github_trusted_root_identity"
                ],
                "sigstore_trusted_root_identity": execution[
                    "sigstore_trusted_root_identity"
                ],
                "release_authority_granted": False,
                "publish_authorized": False,
            }
        )
    )
    if (
        rc_authorities != {acceptance_input["rc_authority_identity"]}
        or any(
            receipt["resolved_release"] != expected_release
            for receipt in profiles.values()
        )
        or len(publication_executions) != 1
        or len(publication_claims) != 1
        or len(publication_signed_times) != 1
        or any(
            receipt["pretrusted_profile_identity"]
            != execution["pretrusted_profile_identity"]
            or receipt["formal_windows_pretrust_kit_identity"]
            != execution["formal_windows_pretrust_kit_identity"]
            or receipt["offline_release_trust_profile_identity"]
            != execution["offline_release_trust_profile_identity"]
            or receipt["provenance_verifier_identity"]
            != execution["provenance_verifier_identity"]
            or receipt["github_trusted_root_identity"]
            != execution["github_trusted_root_identity"]
            or receipt["sigstore_trusted_root_identity"]
            != execution["sigstore_trusted_root_identity"]
            for receipt in profiles.values()
        )
        or {
            receipt["provider_execution_authority_receipt_digest"]
            for receipt in profiles.values()
        }
        != {execution["formal_provider_execution_authority_receipt_digest"]}
        or expected_claim_digests != acceptance_input["attestation_claim_identities"]
        or execution["formal_windows_pretrust_kit_identity"]
        != acceptance_input["formal_windows_pretrust_kit_identity"]
        or execution["offline_release_trust_profile_identity"]
        != acceptance_input["offline_release_trust_profile_identity"]
        or execution["verified_candidate_digest"]
        != acceptance_input["verified_candidate_digest"]
        or execution["publication_preflight_summary"]["claim_digest"]
        != next(iter(publication_claims), "")
        or execution["publication_preflight_summary"]["verifier_digest"]
        != execution["provenance_verifier_identity"]
        or execution["publication_preflight_summary"]["trusted_root_digest"]
        != execution["github_trusted_root_identity"]
        or any(
            summary["trusted_root_digest"]
            != execution["sigstore_trusted_root_identity"]
            for summary in execution["provenance_claim_summaries"].values()
        )
        or execution["provenance_preflight_digest"] != expected_combined_preflight
        or aggregate["rc_authority_identity"]
        != acceptance_input["rc_authority_identity"]
        or aggregate["profile_authority_identities"] != expected_authorities
        or acceptance_input["profile_authority_identities"] != expected_authorities
        or acceptance_input["formal_profile_receipt_digests"] != expected_receipts
        or any(
            aggregate["profile_results"][key]
            != {
                "status": "PASS",
                "failure_code": None,
                "receipt_digest": expected_receipts[key],
                "continuation_receipt_digest": None,
            }
            for key in expected_receipts
        )
        or aggregate["all_profiles_pass"] is not True
        or aggregate["result"] != "PASS"
        or aggregate["formal_authority_identity"]
        != acceptance_input["formal_authority_identity"]
        or aggregate["formal_execution_receipt_digest"] != execution["receipt_digest"]
        or acceptance_input["formal_execution_receipt_digest"]
        != execution["receipt_digest"]
        or acceptance_input["formal_aggregate_receipt_digest"]
        != aggregate["receipt_digest"]
        or execution["formal_authority_identity"]
        != aggregate["formal_authority_identity"]
        or execution["profile_execution_receipt_digests"] != expected_executions
        or execution["profile_results"] != aggregate["profile_results"]
        or execution["result"] != "PASS"
    ):
        _formal_reject("FORMAL_ACCEPTANCE_BUNDLE_BINDING_MISMATCH")
    return {
        "rcLiveAcceptanceInput": acceptance_input,
        "profileReceipts": profiles,
        "aggregateReceipt": aggregate,
        "executionReceipt": execution,
    }


class ProductionFormalAuthorityVerifier:
    """Production provenance-first adapter for exact immutable RC authority."""

    def __init__(
        self,
        plan: FormalProvenancePlan,
        *,
        runner: VerifierRunner = _production_verifier_runner,
        _parent_path_authority: HeldWindowsPrivatePathAuthority | None = None,
    ) -> None:
        if type(plan.qualified_candidate) is not QualifiedCandidateFormalAuthority:
            _formal_reject("FORMAL_QUALIFIED_CANDIDATE_REQUIRED")
        if plan.private_work_root is None:
            _formal_reject("FORMAL_PRIVATE_WORK_ROOT_REQUIRED")
        qualified_candidate = plan.qualified_candidate
        if (
            plan.verifier is not None
            or plan.pretrusted_trust_material_root is not None
            or plan.installer_materials is not None
            or plan.candidate_aggregate_receipt_digest is not None
            or plan.candidate_profile_receipt_digests is not None
            or any(item.trusted_root is not None for item in plan.inputs)
            or (
                plan.publication is not None
                and plan.publication.trusted_root is not None
            )
        ):
            _formal_reject("FORMAL_PRETRUSTED_MATERIAL_REBOUND")
        try:
            material = FormalWindowsPretrustedTrustMaterial.load(
                qualified_candidate.formal_windows_pretrust_root
            )
            private_work_root = Path(plan.private_work_root).resolve(strict=True)
            assert_windows_private_acl(private_work_root)
        except (OSError, FormalWindowsPretrustError, ValueError) as error:
            raise FormalProducerError("FORMAL_PRETRUSTED_MATERIAL_INVALID") from error
        closed_actions = tuple(
            FormalProvenanceInput(
                evidence_name=item.evidence_name,
                bundle=item.bundle,
                trusted_root=material.sigstore_trusted_root_path,
                request=item.request,
            )
            for item in plan.inputs
        )
        publication = plan.publication
        self._publication_input = (
            None
            if publication is None
            else FormalProvenanceInput(
                evidence_name=publication.evidence_name,
                bundle=publication.bundle,
                trusted_root=material.github_trusted_root_path,
                request=publication.request,
            )
        )
        self._preflight = OfflineActionsProvenancePreflight(
            FormalProvenancePlan(
                verifier=material.verifier_path,
                inputs=closed_actions,
                private_work_root=private_work_root,
            ),
            runner=runner,
        )
        self._verifier = material.verifier_path
        self._trust_profile_identity = material.profile.identity
        self._verifier_identity = material.profile.verifier_identity
        self._github_root_identity = material.profile.github_trusted_root_sha256
        self._sigstore_root_identity = material.profile.sigstore_trusted_root_sha256
        self._runner = runner
        self._material = material
        self._installer_materials = qualified_candidate.installer_materials
        self._private_work_root = private_work_root
        self._parent_path_authority = _parent_path_authority
        self._qualified_candidate = qualified_candidate
        self._candidate_aggregate_receipt_digest = (
            qualified_candidate.candidate_aggregate_receipt_digest
        )
        self._candidate_profile_receipt_digests = dict(
            qualified_candidate.candidate_profile_receipt_digests
        )
        self._candidate_plan_digest = qualified_candidate.candidate_plan_digest
        self._candidate_base_vm_identity = qualified_candidate.base_vm_identity
        self._candidate_original_vm_hashes = dict(
            qualified_candidate.original_vm_hashes
        )
        self._candidate_snapshot_identities = dict(
            qualified_candidate.snapshot_identities
        )
        self._candidate_source_disk_graph_identity = (
            qualified_candidate.source_disk_graph_identity
        )
        self._candidate_snapshot_disk_graph_identities = dict(
            qualified_candidate.snapshot_disk_graph_identities
        )
        self._candidate_source_vm_inventory_identity = (
            qualified_candidate.source_vm_inventory_identity
        )

    def _bind_pretrusted_authority(self, request: FormalAuthorityRequest) -> None:
        try:
            expected_request = self._qualified_candidate.issue_request(
                publication_identity=request.publication_identity,
                attestation_claim_identities=(request.attestation_claim_identities),
            )
            material_binding = inspect_formal_windows_pretrust_in_installer_materials(
                self._installer_materials
            )
        except (OSError, FormalWindowsPretrustError, ValueError) as error:
            raise FormalProducerError(
                "FORMAL_INSTALLER_MATERIALS_PRETRUST_INVALID"
            ) from error
        if (
            expected_request.identity_body() != request.identity_body()
            or request.verified_candidate_digest
            != self._qualified_candidate.loaded.verified_digest
            or material_binding.installer_materials_sha256
            != request.installer_materials_identity
            or material_binding.kit_identity
            != request.formal_windows_pretrust_kit_identity
            or material_binding.kit_identity != self._material.identity
            or material_binding.profile_identity != self._trust_profile_identity
            or material_binding.source_profile_identity
            != request.offline_release_trust_profile_identity
            or material_binding.source_profile_identity
            != self._material.profile.source_profile_identity
            or material_binding.windows_host_verifier_identity
            != self._verifier_identity
            or material_binding.linux_guest_verifier_identity
            != self._material.profile.linux_guest_verifier_identity
            or material_binding.github_trusted_root_sha256 != self._github_root_identity
            or material_binding.sigstore_trusted_root_sha256
            != self._sigstore_root_identity
        ):
            _formal_reject("FORMAL_PRETRUSTED_MATERIAL_AUTHORITY_MISMATCH")

    def _close_publication(
        self, request: FormalAuthorityRequest
    ) -> tuple[Any, dict[str, str]]:
        publication_input = self._publication_input
        if (
            publication_input is None
            or publication_input.evidence_name != "github-release"
        ):
            _formal_reject("FORMAL_PUBLICATION_CLAIM_REQUIRED")
        try:
            _, verifier_bytes = _read_bound_file(
                self._verifier,
                maximum=MAX_VERIFIER_BYTES,
                executable=True,
            )
            _, bundle_bytes = _read_bound_file(
                publication_input.bundle, maximum=MAX_INPUT_BYTES
            )
            _, trusted_root_bytes = _read_bound_file(
                publication_input.trusted_root, maximum=MAX_INPUT_BYTES
            )
            _, request_bytes = _read_bound_file(
                publication_input.request, maximum=MAX_INPUT_BYTES
            )
            if (
                sha256_bytes(verifier_bytes) != self._verifier_identity
                or sha256_bytes(trusted_root_bytes) != self._github_root_identity
            ):
                _formal_reject("FORMAL_PRETRUSTED_MATERIAL_REBOUND")
            closed_request = json.loads(
                request_bytes.decode("utf-8"),
                object_pairs_hook=reject_duplicate_json_keys,
            )
            if (
                type(closed_request) is not dict
                or canonical_json_bytes(closed_request) != request_bytes
                or set(closed_request)
                != {
                    "schemaVersion",
                    "mode",
                    "repository",
                    "repositoryId",
                    "ownerId",
                    "tag",
                    "tagCommit",
                    "tagObject",
                    "expectedSubjects",
                }
                or closed_request["schemaVersion"] != 1
                or closed_request["mode"] != "github-release"
                or closed_request["repository"] != request.repository
                or closed_request["repositoryId"] != EXPECTED_REPOSITORY["repositoryId"]
                or closed_request["ownerId"] != EXPECTED_REPOSITORY["ownerId"]
                or closed_request["tag"] != request.rc_tag
                or closed_request["tagCommit"] != request.source_sha
                or COMMIT_IDENTITY.fullmatch(closed_request["tagObject"] or "") is None
                or type(closed_request["expectedSubjects"]) is not list
            ):
                _formal_reject("FORMAL_PUBLICATION_REQUEST_INVALID")
            try:
                snapshot_root = create_windows_private_directory(
                    self._private_work_root,
                    prefix="animemo-formal-publication",
                )
            except (OSError, FormalWindowsPretrustError) as error:
                raise FormalProducerError(
                    "FORMAL_PUBLICATION_SNAPSHOT_INVALID"
                ) from error
            try:
                verifier_name = "verifier.exe" if os.name == "nt" else "verifier"
                verifier = _write_private_snapshot(
                    snapshot_root,
                    verifier_name,
                    verifier_bytes,
                    executable=True,
                    enforce_windows_acl=True,
                )
                bundle = _write_private_snapshot(
                    snapshot_root,
                    "github-release.bundle.json",
                    bundle_bytes,
                    enforce_windows_acl=True,
                )
                trusted_root = _write_private_snapshot(
                    snapshot_root,
                    "github-release.trusted-root.json",
                    trusted_root_bytes,
                    enforce_windows_acl=True,
                )
                request_path = _write_private_snapshot(
                    snapshot_root,
                    "github-release.request.json",
                    request_bytes,
                    enforce_windows_acl=True,
                )
                with hold_windows_private_snapshot(
                    snapshot_root,
                    relative_files=tuple(
                        sorted(
                            item.name
                            for item in (
                                verifier,
                                bundle,
                                trusted_root,
                                request_path,
                            )
                        )
                    ),
                ):
                    claim_bytes = self._runner(
                        (
                            str(verifier),
                            "--bundle",
                            str(bundle),
                            "--trusted-root",
                            str(trusted_root),
                            "--request",
                            str(request_path),
                        )
                    )
                    _assert_snapshot_bytes(
                        verifier,
                        verifier_bytes,
                        executable=True,
                        enforce_windows_acl=True,
                    )
                    _assert_snapshot_bytes(
                        bundle, bundle_bytes, enforce_windows_acl=True
                    )
                    _assert_snapshot_bytes(
                        trusted_root,
                        trusted_root_bytes,
                        enforce_windows_acl=True,
                    )
                    _assert_snapshot_bytes(
                        request_path,
                        request_bytes,
                        enforce_windows_acl=True,
                    )
            finally:
                shutil.rmtree(snapshot_root, ignore_errors=True)
            payload = _closed_claim(claim_bytes)
            publication = close_github_release_publication(payload)
        except FormalProducerError:
            raise
        except (
            PublicationEvidenceError,
            ProvenancePreflightError,
            UnicodeError,
            ValueError,
            json.JSONDecodeError,
        ) as error:
            raise FormalProducerError("FORMAL_PUBLICATION_CLAIM_INVALID") from error
        assets = {item.name: item for item in publication.assets}
        transport_assets = {item.name: item for item in publication.transport_assets}
        request_subjects = closed_request["expectedSubjects"]
        expected_subjects = {
            item["name"]: {
                "sha256": item["sha256"],
                "size": item["size"],
            }
            for item in request_subjects
            if type(item) is dict and set(item) == {"name", "sha256", "size"}
        }
        observed_subjects = {
            item.name: {"sha256": item.sha256, "size": item.size}
            for item in (*publication.assets, *publication.transport_assets)
        }
        if (
            publication.tag != request.rc_tag
            or publication.tag_commit != request.source_sha
            or publication.identity != request.publication_identity
            or publication.tag_object != closed_request["tagObject"]
            or len(expected_subjects) != 5
            or expected_subjects != observed_subjects
            or set(assets)
            != {
                "checksums.txt",
                "deployment-contract.json",
                "installer-materials.tar",
                "release-manifest.json",
            }
            or assets["deployment-contract.json"].sha256
            != request.deployment_contract_identity
            or assets["installer-materials.tar"].sha256
            != request.installer_materials_identity
            or assets["release-manifest.json"].sha256
            != request.release_manifest_identity
            or set(transport_assets) != {f"animemo-{request.rc_tag}-portable.tar"}
        ):
            _formal_reject("FORMAL_PUBLICATION_AUTHORITY_BINDING_MISMATCH")
        return publication, {
            "verifier_digest": sha256_bytes(verifier_bytes),
            "bundle_digest": sha256_bytes(bundle_bytes),
            "trusted_root_digest": sha256_bytes(trusted_root_bytes),
            "request_digest": sha256_bytes(request_bytes),
            "claim_digest": publication.signed_claim_identity,
        }

    def verify(self, request: FormalAuthorityRequest) -> VerifiedFormalRcAuthority:
        if type(request) is not FormalAuthorityRequest:
            _formal_reject("FORMAL_RC_AUTHORITY_INVALID")
        try:
            with ExitStack() as stack:
                if self._parent_path_authority is None:
                    stack.enter_context(
                        hold_windows_private_path_chain(
                            self._private_work_root,
                            allow_leaf_child_writes=True,
                        )
                    )
                    stack.enter_context(
                        hold_windows_private_path_chain(
                            self._material.root,
                            allow_leaf_child_writes=False,
                        )
                    )
                else:
                    stack.enter_context(
                        hold_windows_private_descendant_path(
                            self._parent_path_authority,
                            self._private_work_root,
                            allow_leaf_child_writes=True,
                        )
                    )
                    # The material root is owned by the still-live opaque
                    # Candidate material capability; reopening its ancestors
                    # would conflict with the same no-share-delete authority.
                stack.enter_context(
                    hold_windows_private_snapshot(
                        self._material.root,
                        relative_files=tuple(sorted(FORMAL_WINDOWS_PRETRUST_FILES)),
                        root_already_held=True,
                    )
                )
                return self._verify_held(request)
        except FormalProducerError:
            raise
        except (OSError, FormalWindowsPretrustError) as error:
            raise FormalProducerError(
                "FORMAL_PRETRUSTED_EXECUTION_AUTHORITY_INVALID"
            ) from error

    def _verify_held(
        self, request: FormalAuthorityRequest
    ) -> VerifiedFormalRcAuthority:
        self._bind_pretrusted_authority(request)
        publication, publication_summary = self._close_publication(request)
        receipt = self._preflight.verify()
        errors = sorted(
            Draft202012Validator(
                _schema("formal-provenance-preflight-receipt.schema.json")
            ).iter_errors(receipt),
            key=lambda item: tuple(str(part) for part in item.absolute_path),
        )
        if errors:
            _formal_reject("FORMAL_PROVENANCE_RECEIPT_INVALID")
        if publication_summary["verifier_digest"] != receipt["verifier_digest"]:
            _formal_reject("FORMAL_PROVENANCE_VERIFIER_REBOUND")
        if receipt["verifier_digest"] != self._verifier_identity or any(
            claim["trusted_root_digest"] != self._sigstore_root_identity
            for claim in receipt["claims"]
        ):
            _formal_reject("FORMAL_PRETRUSTED_MATERIAL_REBOUND")
        expected_subjects = {
            "api-image": ("ghcr.io/yanyuhanyue/animemo-api", request.api_digest),
            "web-image": ("ghcr.io/yanyuhanyue/animemo-web", request.web_digest),
            "release-manifest": (
                "release-manifest.json",
                request.release_manifest_identity,
            ),
            "deployment-contract": (
                "deployment-contract.json",
                request.deployment_contract_identity,
            ),
            "installer-materials": (
                "installer-materials.tar",
                request.installer_materials_identity,
            ),
        }
        claims = {item["evidence_name"]: item for item in receipt["claims"]}
        if set(claims) != REQUIRED_EVIDENCE:
            _formal_reject("FORMAL_PROVENANCE_EVIDENCE_SET_INVALID")
        for name, (subject_name, subject_digest) in expected_subjects.items():
            claim = claims[name]
            if (
                claim["subject"] != {"name": subject_name, "sha256": subject_digest}
                or claim["source_commit"] != request.source_sha
                or claim["workflow"] != ".github/workflows/release.yml"
                or claim["claim_digest"] != request.attestation_claim_identities[name]
            ):
                _formal_reject("FORMAL_RC_AUTHORITY_BINDING_MISMATCH")
        combined_preflight_digest = sha256_bytes(
            canonical_json_bytes(
                {
                    "schema": "animemo.formal-production-provenance/v1",
                    "actions_preflight_digest": receipt["preflight_digest"],
                    "publication_authority_identity": publication.identity,
                    "publication_execution_receipt_identity": (
                        publication.execution_receipt_identity
                    ),
                    "publication_signed_claim_identity": (
                        publication.signed_claim_identity
                    ),
                    "publication_preflight": publication_summary,
                    "formal_windows_pretrust_kit_identity": (
                        request.formal_windows_pretrust_kit_identity
                    ),
                    "offline_release_trust_profile_identity": (
                        request.offline_release_trust_profile_identity
                    ),
                    "pretrusted_profile_identity": self._trust_profile_identity,
                    "provenance_verifier_identity": self._verifier_identity,
                    "github_trusted_root_identity": self._github_root_identity,
                    "sigstore_trusted_root_identity": self._sigstore_root_identity,
                    "release_authority_granted": False,
                    "publish_authorized": False,
                }
            )
        )
        return VerifiedFormalRcAuthority.issue(
            request,
            provenance_preflight_digest=combined_preflight_digest,
            actions_preflight_receipt_digest=receipt["preflight_digest"],
            provenance_claim_summaries={
                name: {
                    field: claims[name][field]
                    for field in (
                        "claim_digest",
                        "bundle_digest",
                        "trusted_root_digest",
                        "request_digest",
                    )
                }
                for name in sorted(claims)
            },
            publication_preflight_summary=publication_summary,
            pretrusted_profile_identity=self._trust_profile_identity,
            provenance_verifier_identity=self._verifier_identity,
            github_trusted_root_identity=self._github_root_identity,
            sigstore_trusted_root_identity=self._sigstore_root_identity,
            publication_execution_receipt_identity=(
                publication.execution_receipt_identity
            ),
            publication_signed_claim_identity=publication.signed_claim_identity,
            publication_signed_at=publication.signed_at,
            candidate_aggregate_receipt_digest=(
                self._candidate_aggregate_receipt_digest
            ),
            candidate_profile_receipt_digests=(self._candidate_profile_receipt_digests),
            candidate_plan_digest=self._candidate_plan_digest,
            candidate_provider_execution_authority_receipt_digest=(
                self._qualified_candidate
                .candidate_provider_execution_authority_receipt_digest
            ),
            candidate_material_authority_identity=(
                self._qualified_candidate.candidate_material_authority_identity
            ),
            candidate_material_tree_inventory_identity=(
                self._qualified_candidate
                .candidate_material_tree_inventory_identity
            ),
            candidate_base_vm_identity=self._candidate_base_vm_identity,
            candidate_original_vm_hashes=self._candidate_original_vm_hashes,
            candidate_snapshot_identities=self._candidate_snapshot_identities,
            candidate_source_disk_graph_identity=(
                self._candidate_source_disk_graph_identity
            ),
            candidate_snapshot_disk_graph_identities=(
                self._candidate_snapshot_disk_graph_identities
            ),
            candidate_source_vm_inventory_identity=(
                self._candidate_source_vm_inventory_identity
            ),
        )


def _validate_verified_authority(
    authority: object,
    request: FormalAuthorityRequest,
) -> VerifiedFormalRcAuthority:
    if (
        type(authority) is not VerifiedFormalRcAuthority
        or authority.identity_body() != request.identity_body()
        or authority.identity
        != sha256_bytes(canonical_json_bytes(request.identity_body()))
        or not _is_digest(authority.provenance_preflight_digest)
        or not _is_digest(authority.actions_preflight_receipt_digest)
        or set(authority.provenance_claim_summaries) != REQUIRED_EVIDENCE
        or any(
            set(summary)
            != {
                "claim_digest",
                "bundle_digest",
                "trusted_root_digest",
                "request_digest",
            }
            or any(not _is_digest(value) for value in summary.values())
            for summary in authority.provenance_claim_summaries.values()
        )
        or set(authority.publication_preflight_summary)
        != {
            "verifier_digest",
            "bundle_digest",
            "trusted_root_digest",
            "request_digest",
            "claim_digest",
        }
        or any(
            not _is_digest(value)
            for value in authority.publication_preflight_summary.values()
        )
        or any(
            not _is_digest(value)
            for value in (
                authority.pretrusted_profile_identity,
                authority.provenance_verifier_identity,
                authority.github_trusted_root_identity,
                authority.sigstore_trusted_root_identity,
            )
        )
        or not _is_digest(authority.publication_execution_receipt_identity)
        or not _is_digest(authority.publication_signed_claim_identity)
        or not _is_digest(authority.candidate_aggregate_receipt_digest)
        or set(authority.candidate_profile_receipt_digests)
        != set(CANDIDATE_PROFILE_RESULT_KEYS)
        or any(
            not _is_digest(value)
            for value in authority.candidate_profile_receipt_digests.values()
        )
        or not _is_digest(authority.candidate_plan_digest)
        or not _is_digest(
            authority.candidate_provider_execution_authority_receipt_digest
        )
        or not _is_digest(authority.candidate_material_authority_identity)
        or not _is_digest(
            authority.candidate_material_tree_inventory_identity
        )
        or authority.candidate_source_vm_authority_identity
        != _candidate_source_vm_authority_identity(
            base_vm_identity=authority.candidate_base_vm_identity,
            original_vm_hashes=authority.candidate_original_vm_hashes,
            snapshot_identities=authority.candidate_snapshot_identities,
            source_disk_graph_identity=(
                authority.candidate_source_disk_graph_identity
            ),
            snapshot_disk_graph_identities=(
                authority.candidate_snapshot_disk_graph_identities
            ),
            source_vm_inventory_identity=(
                authority.candidate_source_vm_inventory_identity
            ),
        )
        or _canonical_utc_seconds(authority.publication_signed_at)
        != authority.publication_signed_at
    ):
        _formal_reject("FORMAL_RC_AUTHORITY_BINDING_MISMATCH")
    return authority


def _validate_profile_observation(
    value: object,
    *,
    authority: VerifiedFormalRcAuthority,
    profile: str,
) -> FormalProfileObservation:
    if type(value) is not FormalProfileObservation:
        _formal_reject("FORMAL_PROFILE_OBSERVATION_INVALID")
    observation = value
    expected_transport = "local-bundle" if profile == "FORMAL_OFFLINE" else "github"
    if (
        observation.profile != profile
        or observation.rc_authority_identity != authority.identity
        or observation.transport_source != expected_transport
        or observation.resolved_version != authority.rc_tag
        or observation.resolved_source_sha != authority.source_sha
        or observation.resolved_manifest_identity != authority.release_manifest_identity
        or observation.resolved_deployment_contract_identity
        != authority.deployment_contract_identity
        or observation.resolved_installer_materials_identity
        != authority.installer_materials_identity
        or observation.resolved_api_digest != authority.api_digest
        or observation.resolved_web_digest != authority.web_digest
        or observation.resolved_publication_identity != authority.publication_identity
        or observation.resolved_workflow_identity != authority.workflow_identity
        or dict(observation.resolved_attestation_claim_identities)
        != dict(authority.attestation_claim_identities)
        or observation.publication_execution_receipt_identity
        != authority.publication_execution_receipt_identity
        or observation.publication_signed_claim_identity
        != authority.publication_signed_claim_identity
        or observation.publication_signed_at != authority.publication_signed_at
        or observation.formal_windows_pretrust_kit_identity
        != authority.formal_windows_pretrust_kit_identity
        or observation.offline_release_trust_profile_identity
        != authority.offline_release_trust_profile_identity
        or observation.pretrusted_profile_identity
        != authority.pretrusted_profile_identity
        or observation.provenance_verifier_identity
        != authority.provenance_verifier_identity
        or observation.github_trusted_root_identity
        != authority.github_trusted_root_identity
        or observation.sigstore_trusted_root_identity
        != authority.sigstore_trusted_root_identity
        or observation.result not in {"PASS", "FAIL"}
        or (
            observation.result == "PASS"
            and (
                len(observation.canonical_acceptance_receipt_digests) != 3
                or observation.doctor_receipt_digest is None
            )
        )
        or len(observation.canonical_acceptance_receipt_digests) > 3
        or len(set(observation.canonical_acceptance_receipt_digests))
        != len(observation.canonical_acceptance_receipt_digests)
        or any(
            not _is_digest(value)
            for value in (
                observation.base_vm_identity,
                observation.snapshot_identity,
                observation.clone_identity,
                observation.provider_execution_authority_receipt_digest,
                observation.publication_execution_receipt_identity,
                observation.publication_signed_claim_identity,
                observation.formal_windows_pretrust_kit_identity,
                observation.offline_release_trust_profile_identity,
                observation.pretrusted_profile_identity,
                observation.provenance_verifier_identity,
                observation.github_trusted_root_identity,
                observation.sigstore_trusted_root_identity,
                observation.platform_plan_digest,
                observation.platform_receipt_digest,
                observation.installer_plan_digest,
                observation.installer_execution_receipt_digest,
                observation.continuation_receipt_digest,
                *observation.canonical_acceptance_receipt_digests,
            )
        )
        or (
            observation.doctor_receipt_digest is not None
            and not _is_digest(observation.doctor_receipt_digest)
        )
    ):
        _formal_reject("FORMAL_PROFILE_OBSERVATION_BINDING_MISMATCH")
    return observation


def _build_profile_receipt(
    observation: FormalProfileObservation,
) -> dict[str, Any]:
    resolved_release = {
        "version": observation.resolved_version,
        "source_sha": observation.resolved_source_sha,
        "release_manifest_identity": observation.resolved_manifest_identity,
        "deployment_contract_identity": (
            observation.resolved_deployment_contract_identity
        ),
        "installer_materials_identity": (
            observation.resolved_installer_materials_identity
        ),
        "api_digest": observation.resolved_api_digest,
        "web_digest": observation.resolved_web_digest,
        "publication_identity": observation.resolved_publication_identity,
        "workflow_identity": observation.resolved_workflow_identity,
        "attestation_claim_identities": dict(
            observation.resolved_attestation_claim_identities
        ),
    }
    authority_body = {
        "schema": FORMAL_PROFILE_AUTHORITY_SCHEMA,
        "rc_authority_identity": observation.rc_authority_identity,
        "profile": observation.profile,
        "transport_source": observation.transport_source,
        "resolved_release": resolved_release,
        "canonical_acceptance_receipt_digests": list(
            observation.canonical_acceptance_receipt_digests
        ),
        "result": observation.result,
        "release_authority_granted": False,
        "publish_authorized": False,
    }
    execution_body = {
        "schema": "animemo.formal-profile-execution/v1",
        "profile": observation.profile,
        "base_vm_identity": observation.base_vm_identity,
        "snapshot_identity": observation.snapshot_identity,
        "clone_identity": observation.clone_identity,
        "provider_execution_authority_receipt_digest": (
            observation.provider_execution_authority_receipt_digest
        ),
        "publication_execution_receipt_identity": (
            observation.publication_execution_receipt_identity
        ),
        "publication_signed_claim_identity": (
            observation.publication_signed_claim_identity
        ),
        "publication_signed_at": observation.publication_signed_at,
        "formal_windows_pretrust_kit_identity": (
            observation.formal_windows_pretrust_kit_identity
        ),
        "offline_release_trust_profile_identity": (
            observation.offline_release_trust_profile_identity
        ),
        "pretrusted_profile_identity": observation.pretrusted_profile_identity,
        "provenance_verifier_identity": (observation.provenance_verifier_identity),
        "github_trusted_root_identity": (observation.github_trusted_root_identity),
        "sigstore_trusted_root_identity": (observation.sigstore_trusted_root_identity),
        "platform_plan_digest": observation.platform_plan_digest,
        "platform_receipt_digest": observation.platform_receipt_digest,
        "installer_plan_digest": observation.installer_plan_digest,
        "installer_execution_receipt_digest": (
            observation.installer_execution_receipt_digest
        ),
        "doctor_receipt_digest": observation.doctor_receipt_digest,
        "continuation_receipt_digest": observation.continuation_receipt_digest,
        "result": observation.result,
    }
    unsigned: dict[str, Any] = {
        "schema": FORMAL_PROFILE_SCHEMA,
        "version": 1,
        "rc_authority_identity": observation.rc_authority_identity,
        "profile": observation.profile,
        "transport_source": observation.transport_source,
        "resolved_release": resolved_release,
        "base_vm_identity": observation.base_vm_identity,
        "snapshot_identity": observation.snapshot_identity,
        "clone_identity": observation.clone_identity,
        "provider_execution_authority_receipt_digest": (
            observation.provider_execution_authority_receipt_digest
        ),
        "publication_execution_receipt_identity": (
            observation.publication_execution_receipt_identity
        ),
        "publication_signed_claim_identity": (
            observation.publication_signed_claim_identity
        ),
        "publication_signed_at": observation.publication_signed_at,
        "formal_windows_pretrust_kit_identity": (
            observation.formal_windows_pretrust_kit_identity
        ),
        "offline_release_trust_profile_identity": (
            observation.offline_release_trust_profile_identity
        ),
        "pretrusted_profile_identity": observation.pretrusted_profile_identity,
        "provenance_verifier_identity": (observation.provenance_verifier_identity),
        "github_trusted_root_identity": (observation.github_trusted_root_identity),
        "sigstore_trusted_root_identity": (observation.sigstore_trusted_root_identity),
        "platform_plan_digest": observation.platform_plan_digest,
        "platform_receipt_digest": observation.platform_receipt_digest,
        "installer_plan_digest": observation.installer_plan_digest,
        "installer_execution_receipt_digest": (
            observation.installer_execution_receipt_digest
        ),
        "doctor_receipt_digest": observation.doctor_receipt_digest,
        "canonical_acceptance_receipt_digests": list(
            observation.canonical_acceptance_receipt_digests
        ),
        "continuation_receipt_digest": observation.continuation_receipt_digest,
        "profile_authority_identity": sha256_bytes(
            canonical_json_bytes(authority_body)
        ),
        "execution_receipt_digest": sha256_bytes(canonical_json_bytes(execution_body)),
        "release_authority_granted": False,
        "publish_authorized": False,
        "result": observation.result,
    }
    return validate_formal_profile_receipt(
        {
            **unsigned,
            "receipt_digest": sha256_bytes(canonical_json_bytes(unsigned)),
        }
    )


def _profile_result(
    status: str,
    *,
    failure_code: str | None = None,
    receipt_digest: str | None = None,
    continuation_receipt_digest: str | None = None,
) -> dict[str, str | None]:
    return {
        "status": status,
        "failure_code": failure_code,
        "receipt_digest": receipt_digest,
        "continuation_receipt_digest": continuation_receipt_digest,
    }


class FormalVmController:
    """Provenance-first production coordinator for all Formal profiles."""

    def __init__(
        self,
        *,
        authority_verifier: FormalAuthorityVerifier,
        profile_executor: FormalProfileExecutor,
    ) -> None:
        self._authority_verifier = authority_verifier
        self._profile_executor = profile_executor

    def execute(
        self,
        request: FormalAuthorityRequest,
        execution: FormalExecutionContext,
    ) -> dict[str, Any]:
        if (
            type(request) is not FormalAuthorityRequest
            or type(execution) is not FormalExecutionContext
        ):
            _formal_reject("FORMAL_CONTROLLER_INPUT_INVALID")
        # This call deliberately precedes every provider/profile interaction.
        authority = _validate_verified_authority(
            self._authority_verifier.verify(request), request
        )
        receipts: dict[str, dict[str, Any]] = {}
        results: dict[str, dict[str, str | None]] = {}
        shared_blocker: str | None = None
        for profile in FORMAL_PROFILES:
            key = FORMAL_PROFILE_RESULT_KEYS[profile]
            if shared_blocker is not None:
                results[key] = _profile_result(
                    "NOT_RUN_SHARED_BLOCKER", failure_code=shared_blocker
                )
                continue
            try:
                observation = _validate_profile_observation(
                    self._profile_executor.execute(
                        authority=authority,
                        profile=profile,
                    ),
                    authority=authority,
                    profile=profile,
                )
                receipt = _build_profile_receipt(observation)
            except FormalProfileExecutionError as error:
                results[key] = _profile_result(
                    "ERROR",
                    failure_code=error.code,
                    continuation_receipt_digest=(error.continuation_receipt_digest),
                )
                if not error.continuation_safe:
                    shared_blocker = error.code
                continue
            except FormalProducerError as error:
                shared_blocker = error.code
                results[key] = _profile_result("ERROR", failure_code=shared_blocker)
                continue
            except Exception:  # noqa: BLE001 - producer must classify unknown failure
                shared_blocker = "FORMAL_PROFILE_UNCLASSIFIED_ERROR"
                results[key] = _profile_result("ERROR", failure_code=shared_blocker)
                continue
            receipts[profile] = receipt
            if receipt["result"] == "PASS":
                results[key] = _profile_result(
                    "PASS", receipt_digest=receipt["receipt_digest"]
                )
            else:
                results[key] = _profile_result(
                    "FAIL",
                    failure_code="FORMAL_PROFILE_ACCEPTANCE_FAILED",
                    receipt_digest=receipt["receipt_digest"],
                )
        all_profiles_pass = all(
            results[FORMAL_PROFILE_RESULT_KEYS[profile]]["status"] == "PASS"
            for profile in FORMAL_PROFILES
        )
        profile_status_receipts = {
            profile: _build_formal_profile_status_receipt(
                rc_authority_identity=authority.identity,
                profile=profile,
                result=results[FORMAL_PROFILE_RESULT_KEYS[profile]],
            )
            for profile in FORMAL_PROFILES
            if profile not in receipts
        }
        profile_authorities = {
            FORMAL_PROFILE_RESULT_KEYS[profile]: (
                receipts[profile]["profile_authority_identity"]
                if profile in receipts
                else None
            )
            for profile in FORMAL_PROFILES
        }
        aggregate_authority_body = {
            "schema": "animemo.formal-acceptance-authority/v1",
            "rc_authority_identity": authority.identity,
            "profile_authority_identities": profile_authorities,
            "all_profiles_pass": all_profiles_pass,
            "result": "PASS" if all_profiles_pass else "FAIL",
            "release_authority_granted": False,
            "publish_authorized": False,
        }
        formal_authority_identity = sha256_bytes(
            canonical_json_bytes(aggregate_authority_body)
        )
        provider_execution_digests = {
            receipt["provider_execution_authority_receipt_digest"]
            for receipt in receipts.values()
        }
        if len(provider_execution_digests) > 1:
            _formal_reject("FORMAL_VM_EXECUTION_AUTHORITY_REBOUND")
        formal_provider_execution_authority_receipt_digest = (
            next(iter(provider_execution_digests))
            if provider_execution_digests
            else None
        )
        execution_unsigned: dict[str, Any] = {
            "schema": FORMAL_EXECUTION_SCHEMA,
            "version": 1,
            "formal_authority_identity": formal_authority_identity,
            "verified_candidate_digest": (authority.verified_candidate_digest),
            "candidate_aggregate_receipt_digest": (
                authority.candidate_aggregate_receipt_digest
            ),
            "candidate_profile_receipt_digests": dict(
                authority.candidate_profile_receipt_digests
            ),
            "candidate_source_vm_authority_identity": (
                authority.candidate_source_vm_authority_identity
            ),
            "candidate_provider_execution_authority_receipt_digest": (
                authority.candidate_provider_execution_authority_receipt_digest
            ),
            "candidate_material_authority_identity": (
                authority.candidate_material_authority_identity
            ),
            "candidate_material_tree_inventory_identity": (
                authority.candidate_material_tree_inventory_identity
            ),
            "formal_provider_execution_authority_receipt_digest": (
                formal_provider_execution_authority_receipt_digest
            ),
            "profile_execution_receipt_digests": {
                FORMAL_PROFILE_RESULT_KEYS[profile]: (
                    receipts[profile]["execution_receipt_digest"]
                    if profile in receipts
                    else None
                )
                for profile in FORMAL_PROFILES
            },
            "profile_results": results,
            "provenance_preflight_digest": authority.provenance_preflight_digest,
            "actions_preflight_receipt_digest": (
                authority.actions_preflight_receipt_digest
            ),
            "provenance_claim_summaries": {
                name: dict(summary)
                for name, summary in sorted(
                    authority.provenance_claim_summaries.items()
                )
            },
            "publication_preflight_summary": dict(
                authority.publication_preflight_summary
            ),
            "formal_windows_pretrust_kit_identity": (
                authority.formal_windows_pretrust_kit_identity
            ),
            "offline_release_trust_profile_identity": (
                authority.offline_release_trust_profile_identity
            ),
            "pretrusted_profile_identity": authority.pretrusted_profile_identity,
            "provenance_verifier_identity": (authority.provenance_verifier_identity),
            "github_trusted_root_identity": (authority.github_trusted_root_identity),
            "sigstore_trusted_root_identity": (
                authority.sigstore_trusted_root_identity
            ),
            "accepted_at": execution.accepted_at,
            "observed_at": execution.observed_at,
            "operator_identity": execution.operator_identity,
            "run_id": execution.run_id,
            "run_attempt": execution.run_attempt,
            "correlation_id": execution.correlation_id,
            "current_workflow_commit": execution.current_workflow_commit,
            "execution_environment": execution.execution_environment,
            "tool_identity": execution.tool_identity,
            "result": "PASS" if all_profiles_pass else "FAIL",
            "release_authority_granted": False,
            "publish_authorized": False,
        }
        execution_receipt = validate_formal_execution_receipt(
            {
                **execution_unsigned,
                "receipt_digest": sha256_bytes(
                    canonical_json_bytes(execution_unsigned)
                ),
            }
        )
        aggregate_unsigned: dict[str, Any] = {
            "schema": FORMAL_AGGREGATE_SCHEMA,
            "version": 1,
            "rc_authority_identity": authority.identity,
            "profile_results": results,
            "profile_authority_identities": profile_authorities,
            "all_profiles_pass": all_profiles_pass,
            "formal_authority_identity": formal_authority_identity,
            "formal_execution_receipt_digest": execution_receipt["receipt_digest"],
            "release_authority_granted": False,
            "publish_authorized": False,
            "result": "PASS" if all_profiles_pass else "FAIL",
        }
        aggregate = validate_formal_aggregate_receipt(
            {
                **aggregate_unsigned,
                "receipt_digest": sha256_bytes(
                    canonical_json_bytes(aggregate_unsigned)
                ),
            }
        )
        acceptance_input: dict[str, Any] | None = None
        acceptance_record: dict[str, Any] | None = None
        if all_profiles_pass:
            acceptance_unsigned = {
                "schema": FORMAL_ACCEPTANCE_INPUT_SCHEMA,
                "version": 1,
                "producer_contract_identity": FORMAL_PRODUCER_CONTRACT_IDENTITY,
                "rc_authority_identity": authority.identity,
                "formal_authority_identity": formal_authority_identity,
                "repository": authority.repository,
                "rc_tag": authority.rc_tag,
                "verified_candidate_digest": (authority.verified_candidate_digest),
                "source_sha": authority.source_sha,
                "source_tree": authority.source_tree,
                "release_manifest_identity": authority.release_manifest_identity,
                "deployment_contract_identity": (
                    authority.deployment_contract_identity
                ),
                "installer_materials_identity": (
                    authority.installer_materials_identity
                ),
                "formal_windows_pretrust_kit_identity": (
                    authority.formal_windows_pretrust_kit_identity
                ),
                "offline_release_trust_profile_identity": (
                    authority.offline_release_trust_profile_identity
                ),
                "api_digest": authority.api_digest,
                "web_digest": authority.web_digest,
                "publication_identity": authority.publication_identity,
                "workflow_identity": authority.workflow_identity,
                "attestation_claim_identities": dict(
                    authority.attestation_claim_identities
                ),
                "profile_authority_identities": profile_authorities,
                "formal_profile_receipt_digests": {
                    FORMAL_PROFILE_RESULT_KEYS[profile]: receipts[profile][
                        "receipt_digest"
                    ]
                    for profile in FORMAL_PROFILES
                },
                "formal_aggregate_receipt_digest": aggregate["receipt_digest"],
                "formal_execution_receipt_digest": execution_receipt["receipt_digest"],
                "release_authority_granted": False,
                "publish_authorized": False,
            }
            acceptance_input = _validate_formal_schema(
                {
                    **acceptance_unsigned,
                    "record_input_digest": sha256_bytes(
                        canonical_json_bytes(acceptance_unsigned)
                    ),
                },
                "formal-rc-live-acceptance-input.schema.json",
                code="FORMAL_ACCEPTANCE_INPUT_INVALID",
            )
            from .acceptance import (
                build_rc_live_acceptance,
                validate_rc_live_acceptance,
            )

            closed_bundle = validate_formal_acceptance_bundle(
                {
                    "rcLiveAcceptanceInput": acceptance_input,
                    "profileReceipts": receipts,
                    "aggregateReceipt": aggregate,
                    "executionReceipt": execution_receipt,
                }
            )
            formal_evidence = {
                key: closed_bundle[key]
                for key in (
                    "rcLiveAcceptanceInput",
                    "profileReceipts",
                    "aggregateReceipt",
                    "executionReceipt",
                )
            }
            acceptance_record = validate_rc_live_acceptance(
                build_rc_live_acceptance(formal_evidence=formal_evidence)
            )
        return {
            "status": aggregate["result"],
            "profileReceipts": receipts,
            "profileStatusReceipts": profile_status_receipts,
            "aggregateReceipt": aggregate,
            "executionReceipt": execution_receipt,
            "rcLiveAcceptanceInput": acceptance_input,
            "rcLiveAcceptanceRecord": acceptance_record,
        }


__all__: Sequence[str] = (
    "FORMAL_PRODUCER_CONTRACT_IDENTITY",
    "FORMAL_PROFILES",
    "FormalAuthorityRequest",
    "FormalExecutionContext",
    "FormalProducerError",
    "FormalProfileExecutionError",
    "FormalProfileObservation",
    "FormalProvenanceInput",
    "FormalProvenancePlan",
    "FormalVmController",
    "OfflineActionsProvenancePreflight",
    "ProductionFormalAuthorityVerifier",
    "ProvenancePreflightError",
    "VerifiedFormalRcAuthority",
    "validate_formal_acceptance_bundle",
    "validate_formal_aggregate_receipt",
    "validate_formal_execution_receipt",
    "validate_formal_profile_receipt",
)
