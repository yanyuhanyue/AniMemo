"""Canonical transport-only publication plan for an accepted Candidate.

Qualification and Candidate verification own every authority-bearing byte.  This
module has one interface: turn an already loaded, cryptographically closed
Candidate plus its acceptance receipt into an immutable, non-mutating Publish
plan.  Registry mutation and reconciliation remain separate concerns.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .candidate import (
    LoadedVerifiedCandidate,
    aggregate_receipt_digest,
    canonical_json_bytes,
    sha256_bytes,
    validate_aggregate_receipt,
    validate_candidate_input,
    validate_verified_candidate,
)

PUBLISH_CANDIDATE_PLAN_SCHEMA = "animemo.publish-candidate-plan/v1"
PUBLISH_CANDIDATE_BYTE_MISMATCH = "PUBLISH_CANDIDATE_BYTE_MISMATCH"
PUBLISH_IMAGE_ROLES = ("api", "web")


class PublicationInputError(ValueError):
    """Stable fail-closed error at the accepted-Candidate publication seam."""

    def __init__(self, code: str = PUBLISH_CANDIDATE_BYTE_MISMATCH) -> None:
        super().__init__(code)
        self.code = code


def _reject() -> None:
    raise PublicationInputError()


def _build_plan(
    loaded: LoadedVerifiedCandidate,
    acceptance_receipt: Mapping[str, Any],
) -> dict[str, Any]:
    candidate = validate_candidate_input(dict(loaded.candidate_input))
    verified = validate_verified_candidate(dict(loaded.verified))
    receipt = validate_aggregate_receipt(dict(acceptance_receipt))

    candidate_digest = sha256_bytes(canonical_json_bytes(candidate))
    verified_digest = sha256_bytes(canonical_json_bytes(verified))
    if (
        loaded.verified_digest != verified_digest
        or verified["candidate_input_sha256"] != candidate_digest
        or receipt["candidate_input_digest"] != candidate_digest
        or receipt["verified_candidate_digest"] != verified_digest
        or receipt["qualification_run_id"] != candidate["qualification_run_id"]
        or receipt["qualification_run_attempt"]
        != candidate["qualification_run_attempt"]
        or receipt["source_sha"] != candidate["source_sha"]
        or receipt["source_tree"] != candidate["source_tree"]
        or receipt["candidate_version"] != candidate["candidate_version"]
        or receipt["all_profiles_pass"] is not True
        or receipt["result"] != "PASS"
    ):
        _reject()

    verified_images = {item["role"]: item for item in verified["oci_verification"]}
    loaded_images = {item.role: item for item in loaded.images.images}
    if set(verified_images) != set(loaded_images):
        _reject()
    images: dict[str, dict[str, str]] = {}
    for role in PUBLISH_IMAGE_ROLES:
        observed = loaded_images.get(role)
        authority = verified_images.get(role)
        if observed is None or authority is None:
            _reject()
        try:
            layout_path = observed.layout.relative_to(loaded.root).as_posix()
        except (TypeError, ValueError):
            _reject()
        expected_layout = f"candidate-runtime/oci/{role}"
        if (
            layout_path != expected_layout
            or observed.repository != authority["repository"]
            or observed.digest != authority["digest"]
            or observed.platform != authority["platform"]
            or observed.config_digest != authority["config_digest"]
            or list(observed.layer_digests) != authority["layer_digests"]
            or observed.digest != candidate[f"{role}_oci_digest"]
        ):
            _reject()
        images[role] = {
            "digest": observed.digest,
            "layout_path": expected_layout,
            "platform": observed.platform,
            "repository": observed.repository,
        }

    inventory = candidate["candidate_runtime_file_inventory"]
    if inventory != verified["runtime_file_inventory"]:
        _reject()
    plan: dict[str, Any] = {
        "schema": PUBLISH_CANDIDATE_PLAN_SCHEMA,
        "version": 1,
        "repository": verified["repository"],
        "qualification_run_id": candidate["qualification_run_id"],
        "qualification_run_attempt": candidate["qualification_run_attempt"],
        "source_sha": candidate["source_sha"],
        "source_tree": candidate["source_tree"],
        "candidate_version": candidate["candidate_version"],
        "candidate_input_digest": candidate_digest,
        "verified_candidate_digest": verified_digest,
        "candidate_acceptance_receipt_digest": aggregate_receipt_digest(receipt),
        "release_manifest_digest": candidate["release_manifest_sha256"],
        "producer_toolchain_receipt_digest": candidate[
            "producer_toolchain_receipt_sha256"
        ],
        "candidate_runtime_inventory_digest": sha256_bytes(
            canonical_json_bytes(inventory)
        ),
        "paths": {
            "candidate_input": "candidate-input.json",
            "verified_candidate": "verified-candidate.json",
            "candidate_acceptance_receipt": "candidate-acceptance-receipt.json",
            "release_manifest": "release-manifest.json",
            "producer_toolchain_receipt": (
                "release-producer-toolchain-receipt.json"
            ),
            "checksums": "checksums.txt",
            "deployment_contract": "deployment-contract.json",
            "installer_materials": "installer-materials.tar",
            "candidate_runtime": "candidate-runtime",
        },
        "images": images,
        "publish_rebuild_count": 0,
        "manifest_generation_count": 0,
        "mutation_authorized": False,
        "plan_digest": "",
    }
    unsigned = dict(plan)
    unsigned.pop("plan_digest")
    plan["plan_digest"] = sha256_bytes(canonical_json_bytes(unsigned))
    return plan


def build_publish_candidate_plan(
    loaded: LoadedVerifiedCandidate,
    acceptance_receipt: Mapping[str, Any],
) -> dict[str, Any]:
    """Return one canonical no-mutation plan or one stable mismatch error."""

    if type(loaded) is not LoadedVerifiedCandidate or not isinstance(
        acceptance_receipt, Mapping
    ):
        _reject()
    try:
        return _build_plan(loaded, acceptance_receipt)
    except PublicationInputError:
        raise
    except (KeyError, TypeError, ValueError, OSError):
        raise PublicationInputError() from None


__all__ = [
    "PUBLISH_CANDIDATE_BYTE_MISMATCH",
    "PUBLISH_CANDIDATE_PLAN_SCHEMA",
    "PublicationInputError",
    "build_publish_candidate_plan",
]
