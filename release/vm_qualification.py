"""Pre-publish VM boundary classifications for AniMemo v1.1 distribution."""

from __future__ import annotations

from typing import Any

from .notes import CANONICAL_RELEASE_ASSETS


class VMQualificationError(ValueError):
    """A VM result crosses or weakens the frozen qualification boundary."""


VM_ROLES = {
    "Ubuntu 24.04.4 - Docker Base": "CANONICAL_FRESH_INSTALL_BASE",
    "Ubuntu 24.04.4 - AniMemo Runtime Base": "PRIMARY_RUNTIME_QUALIFICATION_BASE",
    "Ubuntu 24.04.4 - Fresh Base - Healthy": "BARE_HOST_BOOTSTRAP_BASE",
}


def validate_pre_publish_qualification(
    *,
    docker_base: str,
    runtime_base: str,
    fresh_base_bootstrap: str,
    docker_reinstalled_on_docker_base: bool,
    docker_reinstalled_on_runtime_base: bool,
    live_public_rc_acceptance: str,
) -> dict[str, Any]:
    if {docker_base, runtime_base, fresh_base_bootstrap} != {"PASS"}:
        raise VMQualificationError("all three bounded pre-publish VM lanes must pass")
    if docker_reinstalled_on_docker_base is not False:
        raise VMQualificationError("Docker Base must not reinstall Docker")
    if docker_reinstalled_on_runtime_base is not False:
        raise VMQualificationError("Runtime Base must not reinstall Docker")
    if live_public_rc_acceptance != "DEFERRED_POST_RC_BY_DESIGN":
        raise VMQualificationError("live public RC acceptance cannot be fabricated pre-publish")
    return {
        "schema": "animemo.vm-pre-publish-qualification/v1",
        "docker_base_role": VM_ROLES["Ubuntu 24.04.4 - Docker Base"],
        "runtime_base_role": VM_ROLES["Ubuntu 24.04.4 - AniMemo Runtime Base"],
        "fresh_base_role": VM_ROLES["Ubuntu 24.04.4 - Fresh Base - Healthy"],
        "docker_base": docker_base,
        "runtime_base": runtime_base,
        "fresh_base_bootstrap": fresh_base_bootstrap,
        "docker_reinstalled_on_docker_base": False,
        "docker_reinstalled_on_runtime_base": False,
        "live_public_rc_acceptance": live_public_rc_acceptance,
        "status": "PASS",
    }


def classify_legacy_release(*, tag: str, observed_assets: set[str]) -> dict[str, Any]:
    if tag != "v1.0.0" or not isinstance(observed_assets, set) or not all(
        isinstance(name, str) for name in observed_assets
    ):
        raise VMQualificationError("legacy release observation is invalid")
    missing = sorted(set(CANONICAL_RELEASE_ASSETS) - observed_assets)
    if missing:
        classification = "LEGACY_RELEASE_NOT_ELIGIBLE_FOR_V1_1_CONTRACT_E2E"
        eligible = False
    else:
        classification = "LEGACY_RELEASE_ELIGIBLE_FOR_V1_1_CONTRACT_E2E"
        eligible = True
    return {
        "schema": "animemo.legacy-release-classification/v1",
        "tag": tag,
        "observed_assets": sorted(observed_assets),
        "required_v1_1_assets": list(CANONICAL_RELEASE_ASSETS),
        "missing_assets": missing,
        "classification": classification,
        "eligible": eligible,
        "installer_defect": False,
        "legacy_release_modified": False,
    }


def classify_github_transport(observation: str) -> dict[str, Any]:
    if observation == "CONNECTION_RESET":
        status = "DEGRADED"
        classification = "GITHUB_PUBLIC_TRANSPORT_ENVIRONMENT_DEGRADED"
    else:
        status = "UNKNOWN"
        classification = "GITHUB_PUBLIC_TRANSPORT_ENVIRONMENT_UNKNOWN"
    return {
        "schema": "animemo.github-transport-classification/v1",
        "observation": observation,
        "status": status,
        "classification": classification,
        "installer_defect": False,
        "qualification_pass": False,
        "requires_gh_auth": False,
        "fallback_attempted": False,
    }
