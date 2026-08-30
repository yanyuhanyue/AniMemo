"""Run one closed Candidate profile inside a disposable VM.

The host harness supplies identity context through one canonical, bounded
base64url environment value.  No VM path, shell fragment, package list, or
transport override is accepted by this program.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.parse
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from durability.canonical import canonical_json_bytes as canonical_identity_bytes
from installer.platform_bootstrap import (
    PlatformBootstrapError,
    _apt_argv,
    parse_platform_bootstrap_plan,
    parse_platform_bootstrap_receipt,
)
from installer.safe_archive import (
    WHEEL_RUNTIME_LIMITS,
    SafeArchiveError,
    extract_wheel_runtime,
)
from release.candidate import (
    CandidateContractError,
    apt_network_sequence_matches,
    canonical_json_bytes,
    load_verified_candidate,
    sha256_bytes,
    validate_profile_receipt,
)
from release.materials import reject_duplicate_json_keys

PROFILES = ("FRESH_BASE", "DOCKER_BASE", "RUNTIME_BASE_OFFLINE")
INSTALLER_PROFILES = {
    "FRESH_BASE": "ONLINE_FRESH",
    "DOCKER_BASE": "ONLINE_EXISTING_DOCKER",
    "RUNTIME_BASE_OFFLINE": "OFFLINE_VALIDATE_ONLY",
}
CONTEXT_ENV = "ANIMEMO_CANDIDATE_PROFILE_CONTEXT_B64URL"
RECEIPT_OUTPUT = Path(
    "/var/lib/animemo/candidate-acceptance/profile-receipt-draft.json"
)
MAX_CONTEXT_BYTES = 64 * 1024
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_HEX_IDENTITY = re.compile(r"[0-9a-f]{64}\Z")
_STEP = re.compile(r"[a-z0-9][a-z0-9.-]{0,127}\Z")
_COMMAND_OBSERVER_CONTRACT = {
    "boundaries": ["PLATFORM", "RUNTIME"],
    "externalPullDispositions": [
        "EXPLICIT_NEVER",
        "FORBIDDEN_DETECTED",
        "NOT_APPLICABLE",
    ],
    "networkClassification": "APT_NETWORK",
    "localClassifications": ["LOCAL_DOCKER_SOCKET", "LOCAL_ONLY"],
    "unknownClassification": "UNKNOWN_NETWORK_CAPABILITY",
    "version": 1,
}
_COMMAND_OBSERVER_IDENTITY = sha256_bytes(
    canonical_identity_bytes(_COMMAND_OBSERVER_CONTRACT)
)


class ProfileRunnerError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class CommandRunner(Protocol):
    def run(
        self, argv: tuple[str, ...], environment: Mapping[str, str]
    ) -> tuple[int, bytes, bytes]: ...


class SubprocessCommandRunner:
    def run(
        self, argv: tuple[str, ...], environment: Mapping[str, str]
    ) -> tuple[int, bytes, bytes]:
        completed = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            env=dict(environment),
            shell=False,
            timeout=4 * 60 * 60,
            check=False,
        )
        return completed.returncode, completed.stdout, completed.stderr


@contextmanager
def _verified_wheel_runtime(installer_root: Path) -> Iterator[Path]:
    wheelhouse = installer_root / "wheelhouse"
    try:
        wheels = sorted(wheelhouse.iterdir(), key=lambda item: item.name)
    except OSError as error:
        raise ProfileRunnerError("CANDIDATE_PROFILE_RUNTIME_INVALID") from error
    if (
        not wheels
        or len(wheels) > WHEEL_RUNTIME_LIMITS.max_archives
        or any(
            wheel.is_symlink() or not wheel.is_file() or wheel.suffix != ".whl"
            for wheel in wheels
        )
    ):
        raise ProfileRunnerError("CANDIDATE_PROFILE_RUNTIME_INVALID")

    with tempfile.TemporaryDirectory(
        prefix="animemo-candidate-python-"
    ) as temporary:
        runtime = Path(temporary) / "runtime"
        try:
            extract_wheel_runtime(wheels, runtime)
        except SafeArchiveError as error:
            raise ProfileRunnerError("CANDIDATE_PROFILE_RUNTIME_INVALID") from error
        yield runtime


def _decode_context(value: str) -> dict[str, Any]:
    if (
        not value
        or "=" in value
        or len(value) > MAX_CONTEXT_BYTES * 2
        or re.fullmatch(r"[A-Za-z0-9_-]+", value) is None
    ):
        raise ProfileRunnerError("CANDIDATE_PROFILE_CONTEXT_INVALID")
    try:
        decoded = base64.urlsafe_b64decode(
            (value + "=" * (-len(value) % 4)).encode("ascii")
        )
    except (ValueError, UnicodeEncodeError) as error:
        raise ProfileRunnerError("CANDIDATE_PROFILE_CONTEXT_INVALID") from error
    if (
        not decoded
        or len(decoded) > MAX_CONTEXT_BYTES
        or base64.urlsafe_b64encode(decoded).decode("ascii").rstrip("=") != value
    ):
        raise ProfileRunnerError("CANDIDATE_PROFILE_CONTEXT_INVALID")
    try:
        context = json.loads(
            decoded.decode("utf-8"),
            object_pairs_hook=reject_duplicate_json_keys,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise ProfileRunnerError("CANDIDATE_PROFILE_CONTEXT_INVALID") from error
    fields = {
        "base_vm_identity",
        "clone_identity",
        "initial_platform_state",
        "original_vm_pre_hashes",
        "profile",
        "source_disk_graph_identity",
        "source_vm_inventory_identity",
        "snapshot_disk_graph_identity",
        "snapshot_identity",
    }
    if type(context) is not dict or set(context) != fields:
        raise ProfileRunnerError("CANDIDATE_PROFILE_CONTEXT_INVALID")
    for field in (
        "base_vm_identity",
        "clone_identity",
        "source_disk_graph_identity",
        "source_vm_inventory_identity",
        "snapshot_disk_graph_identity",
        "snapshot_identity",
    ):
        if type(context[field]) is not str or not _DIGEST.fullmatch(context[field]):
            raise ProfileRunnerError("CANDIDATE_PROFILE_CONTEXT_INVALID")
    hashes = context["original_vm_pre_hashes"]
    if (
        type(hashes) is not dict
        or not hashes
        or any(
            type(name) is not str
            or not name
            or type(digest) is not str
            or not _DIGEST.fullmatch(digest)
            for name, digest in hashes.items()
        )
    ):
        raise ProfileRunnerError("CANDIDATE_PROFILE_CONTEXT_INVALID")
    state = context["initial_platform_state"]
    if type(state) is not dict or set(state) != {
        "docker_present",
        "network_allowed",
        "runtime_dependencies_present",
    } or any(type(item) is not bool for item in state.values()):
        raise ProfileRunnerError("CANDIDATE_PROFILE_CONTEXT_INVALID")
    if context["profile"] not in PROFILES:
        raise ProfileRunnerError("CANDIDATE_PROFILE_CONTEXT_INVALID")
    if canonical_json_bytes(context) != decoded:
        raise ProfileRunnerError("CANDIDATE_PROFILE_CONTEXT_NON_CANONICAL")
    return context


def installer_argv(
    *, verified_candidate_digest: str, profile: str, public_origin: str
) -> tuple[str, ...]:
    if not _DIGEST.fullmatch(verified_candidate_digest) or profile not in PROFILES:
        raise ProfileRunnerError("CANDIDATE_PROFILE_INPUT_INVALID")
    try:
        origin = urllib.parse.urlsplit(public_origin)
        port = origin.port
    except (TypeError, ValueError):
        raise ProfileRunnerError("CANDIDATE_PROFILE_ORIGIN_INVALID") from None
    if (
        type(public_origin) is not str
        or len(public_origin) > 2048
        or origin.scheme != "https"
        or not origin.hostname
        or origin.username is not None
        or origin.password is not None
        or origin.query
        or origin.fragment
        or origin.path not in {"", "/"}
        or port not in {None, 443}
    ):
        raise ProfileRunnerError("CANDIDATE_PROFILE_ORIGIN_INVALID")
    return (
        sys.executable,
        "-P",
        "-B",
        "-m",
        "installer",
        "candidate",
        "--verified-candidate-digest",
        verified_candidate_digest,
        "--profile",
        INSTALLER_PROFILES[profile],
        "--public-origin",
        public_origin,
        "--execute",
        "--accept",
        "--json",
    )


def _result_json(stdout: bytes) -> dict[str, Any]:
    if not stdout or len(stdout) > 8 * 1024 * 1024:
        raise ProfileRunnerError("CANDIDATE_INSTALLER_RESULT_INVALID")
    try:
        value = json.loads(stdout, object_pairs_hook=reject_duplicate_json_keys)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise ProfileRunnerError("CANDIDATE_INSTALLER_RESULT_INVALID") from error
    if type(value) is not dict:
        raise ProfileRunnerError("CANDIDATE_INSTALLER_RESULT_INVALID")
    return value


def _closed_mapping(
    value: object, fields: set[str], *, code: str
) -> dict[str, Any]:
    if type(value) is not dict or set(value) != fields:
        raise ProfileRunnerError(code)
    return value


def _production_execution_observation(
    *,
    loaded,
    parsed_plan,
    installer_result: Mapping[str, Any],
    value: object,
) -> dict[str, Any]:
    code = "CANDIDATE_PROFILE_EXECUTION_OBSERVATION_INVALID"
    observation = _closed_mapping(
        value,
        {
            "canonicalAcceptanceTests",
            "completedSteps",
            "doctorExecutionIdentity",
            "doctorReceiptDigest",
            "doctorReport",
            "externalPullObservation",
            "imageAcquisitionReceipt",
            "imageAcquisitionReceiptDigest",
            "imageRuntimeReadbackReceipt",
            "imageRuntimeReadbackReceiptDigest",
            "networkObservation",
            "schema",
        },
        code=code,
    )
    if (
        observation["schema"]
        != "animemo.candidate-profile-production-execution-observation/v1"
    ):
        raise ProfileRunnerError(code)

    doctor = _closed_mapping(
        observation["doctorReport"],
        {
            "checkedAt",
            "checks",
            "compatibility",
            "deploymentProfile",
            "doctorIdentity",
            "instanceId",
            "mode",
            "overallStatus",
            "reportFormat",
            "reportVersion",
        },
        code=code,
    )
    doctor_identity = _closed_mapping(
        doctor["doctorIdentity"], {"format", "version"}, code=code
    )
    if (
        doctor["reportFormat"] != "animemo-doctor-report"
        or doctor["reportVersion"] != 1
        or doctor_identity
        != {"format": "animemo-doctor-runtime", "version": 1}
        or doctor["mode"] != "READ-ONLY"
        or type(doctor["checkedAt"]) is not str
        or type(doctor["instanceId"]) is not str
        or type(doctor["deploymentProfile"]) is not str
        or type(doctor["compatibility"]) is not dict
    ):
        raise ProfileRunnerError(code)
    doctor_digest = sha256_bytes(canonical_identity_bytes(doctor))
    if observation["doctorReceiptDigest"] != doctor_digest:
        raise ProfileRunnerError("CANDIDATE_PROFILE_DOCTOR_RECEIPT_MISMATCH")
    checks = doctor["checks"]
    if type(checks) is not list or not checks or len(checks) > 128:
        raise ProfileRunnerError(code)
    seen_checks: set[str] = set()
    for item in checks:
        check = _closed_mapping(
            item,
            {
                "checkId",
                "checkedAt",
                "code",
                "evidenceClass",
                "remediation",
                "severity",
                "status",
                "summary",
            },
            code=code,
        )
        name = check["checkId"]
        if (
            type(name) is not str
            or not _STEP.fullmatch(name)
            or name in seen_checks
            or check["status"] not in {"PASS", "WARN", "FAIL", "SKIPPED"}
            or any(type(check[field]) is not str for field in set(check) - {"status"})
        ):
            raise ProfileRunnerError(code)
        seen_checks.add(name)
        if check["status"] != "PASS":
            raise ProfileRunnerError("CANDIDATE_PROFILE_DOCTOR_FAILED")
    if doctor["overallStatus"] != "PASS":
        raise ProfileRunnerError("CANDIDATE_PROFILE_DOCTOR_FAILED")

    expected_acceptance = {
        "application.journal-crud": "django-domain-service-transaction-rollback",
        "service.api.health": "immutable-compose-api-health",
        "service.web.health": "immutable-compose-web-health",
    }
    acceptance = observation["canonicalAcceptanceTests"]
    if type(acceptance) is not list or len(acceptance) != len(expected_acceptance):
        raise ProfileRunnerError("CANDIDATE_PROFILE_CANONICAL_TEST_MISMATCH")
    canonical_tests: list[dict[str, str]] = []
    for acceptance_value, (expected_name, expected_adapter) in zip(
        acceptance, expected_acceptance.items(), strict=True
    ):
        item = _closed_mapping(
            acceptance_value,
            {"evidence", "name", "receiptDigest", "result"},
            code=code,
        )
        evidence = _closed_mapping(
            item["evidence"], {"adapter", "observationDigest"}, code=code
        )
        body = {
            "evidence": evidence,
            "name": item["name"],
            "result": item["result"],
        }
        if (
            item["name"] != expected_name
            or item["result"] != "PASS"
            or evidence["adapter"] != expected_adapter
            or type(evidence["observationDigest"]) is not str
            or not _DIGEST.fullmatch(evidence["observationDigest"])
            or item["receiptDigest"] != sha256_bytes(canonical_identity_bytes(body))
        ):
            raise ProfileRunnerError("CANDIDATE_PROFILE_CANONICAL_TEST_MISMATCH")
        canonical_tests.append(
            {
                "name": item["name"],
                "result": item["result"],
                "receiptDigest": item["receiptDigest"],
            }
        )

    completed_steps = observation["completedSteps"]
    if (
        type(completed_steps) is not list
        or not completed_steps
        or len(completed_steps) > 128
        or any(type(step) is not str or not _STEP.fullmatch(step) for step in completed_steps)
        or len(set(completed_steps)) != len(completed_steps)
        or completed_steps[-1] != "doctor.accept"
        or installer_result.get("completedSteps") != completed_steps
    ):
        raise ProfileRunnerError("CANDIDATE_PROFILE_COMPLETED_STEPS_MISMATCH")

    installer_result_digest = sha256_bytes(canonical_identity_bytes(installer_result))
    expected_doctor_execution_identity = sha256_bytes(
        canonical_identity_bytes(
            {
                "canonicalAcceptanceReceiptDigests": [
                    item["receiptDigest"] for item in canonical_tests
                ],
                "completedSteps": completed_steps,
                "doctorReceiptDigest": doctor_digest,
                "installerExecutionReceiptDigest": installer_result_digest,
            }
        )
    )
    if observation["doctorExecutionIdentity"] != expected_doctor_execution_identity:
        raise ProfileRunnerError("CANDIDATE_PROFILE_DOCTOR_EXECUTION_MISMATCH")

    network = _closed_mapping(
        observation["networkObservation"],
        {
            "authority",
            "completedCommandInventoryDigest",
            "completedCommands",
            "destinationAuthority",
            "egressIsolation",
            "expectedNetworkCommandDigests",
            "observerIdentities",
            "platformPlanDigest",
            "policy",
            "retryableNetworkCommandDigests",
            "result",
        },
        code=code,
    )
    egress = _closed_mapping(
        network["egressIsolation"],
        {
            "authority",
            "containerNetwork",
            "containerNetworkInternal",
            "receiptDigest",
            "service",
            "serviceAddressFamilies",
        },
        code=code,
    )
    egress_body = dict(egress)
    receipt_digest = egress_body.pop("receiptDigest")
    if (
        egress["authority"] != "OS_ENFORCED_CANDIDATE_EGRESS_ISOLATION"
        or type(egress["containerNetwork"]) is not str
        or re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,127}", egress["containerNetwork"])
        is None
        or egress["containerNetworkInternal"] is not True
        or type(egress["service"]) is not str
        or re.fullmatch(r"[a-z0-9][a-z0-9@_.-]{0,127}", egress["service"])
        is None
        or egress["serviceAddressFamilies"] != ["AF_UNIX", "AF_NETLINK"]
        or receipt_digest != sha256_bytes(canonical_identity_bytes(egress_body))
    ):
        raise ProfileRunnerError("CANDIDATE_PROFILE_EGRESS_ISOLATION_INVALID")
    expected_network_digests: list[str] = []
    retryable_network_digests: list[str] = []
    for action in parsed_plan.actions:
        if action.kind.value == "APT_UPDATE":
            expected_network_digests.append(
                sha256_bytes(canonical_identity_bytes(list(_apt_argv("update"))))
            )
        elif action.kind.value in {
            "INSTALL_DOCKER",
            "INSTALL_COMPOSE",
            "INSTALL_POSTGRES_CLIENT",
        }:
            install_digest = sha256_bytes(
                canonical_identity_bytes(list(_apt_argv("install", action.packages)))
            )
            if parsed_plan.mode.value == "ONLINE_EXISTING_DOCKER":
                expected_network_digests.append(
                    sha256_bytes(
                        canonical_identity_bytes(
                            list(_apt_argv("simulate", action.packages))
                        )
                    )
                )
            expected_network_digests.append(install_digest)
            retryable_network_digests.append(install_digest)
    completed_commands = network["completedCommands"]
    if type(completed_commands) is not list or len(completed_commands) > 512:
        raise ProfileRunnerError("CANDIDATE_PROFILE_NETWORK_OBSERVATION_INVALID")
    normalized_commands: list[dict[str, Any]] = []
    for command_value in completed_commands:
        command = _closed_mapping(
            command_value,
            {
                "argvDigest",
                "boundary",
                "classification",
                "externalPullDisposition",
                "operation",
                "returnCode",
            },
            code=code,
        )
        if (
            type(command["argvDigest"]) is not str
            or not _DIGEST.fullmatch(command["argvDigest"])
            or command["boundary"] not in {"PLATFORM", "RUNTIME"}
            or command["classification"]
            not in {"APT_NETWORK", "LOCAL_DOCKER_SOCKET", "LOCAL_ONLY"}
            or command["externalPullDisposition"]
            not in {"EXPLICIT_NEVER", "NOT_APPLICABLE"}
            or type(command["operation"]) is not str
            or not _STEP.fullmatch(command["operation"])
            or type(command["returnCode"]) is not int
            or command["classification"] == "APT_NETWORK"
            and (
                command["boundary"] != "PLATFORM"
                or command["operation"] != "apt-get"
                or command["argvDigest"] not in expected_network_digests
                or command["returnCode"] not in {0, 124}
                or command["returnCode"] == 124
                and command["argvDigest"] not in retryable_network_digests
            )
            or command["operation"]
            in {"docker-compose-run", "docker-compose-up", "docker-run"}
            and command["externalPullDisposition"] != "EXPLICIT_NEVER"
            or command["operation"]
            not in {"docker-compose-run", "docker-compose-up", "docker-run"}
            and command["externalPullDisposition"] != "NOT_APPLICABLE"
        ):
            raise ProfileRunnerError("CANDIDATE_PROFILE_NETWORK_OBSERVATION_INVALID")
        normalized_commands.append(
            {
                "argv_digest": command["argvDigest"],
                "boundary": command["boundary"],
                "classification": command["classification"],
                "external_pull_disposition": command["externalPullDisposition"],
                "operation": command["operation"],
                "return_code": command["returnCode"],
            }
        )
    observer_identities = _closed_mapping(
        network["observerIdentities"], {"platform", "runtime"}, code=code
    )
    observed_network_commands = [
        (command["argv_digest"], command["return_code"])
        for command in normalized_commands
        if command["classification"] == "APT_NETWORK"
    ]
    if (
        network["authority"]
        != "PRODUCTION_EXECUTION_WITH_OS_EGRESS_ISOLATION"
        or network["completedCommandInventoryDigest"]
        != sha256_bytes(canonical_identity_bytes(completed_commands))
        or observer_identities
        != {
            "platform": _COMMAND_OBSERVER_IDENTITY,
            "runtime": _COMMAND_OBSERVER_IDENTITY,
        }
        or network["platformPlanDigest"] != parsed_plan.plan_digest
        or network["policy"] != parsed_plan.network_policy
        or network["destinationAuthority"]
        != (
            "NONE"
            if parsed_plan.network_policy == "DENY_ALL"
            else "UBUNTU_ARCHIVE_VERIFIED_APT_SOURCES"
        )
        or network["result"] != "PASS"
        or network["expectedNetworkCommandDigests"]
        != expected_network_digests
        or network["retryableNetworkCommandDigests"]
        != retryable_network_digests
        or not apt_network_sequence_matches(
            observed_network_commands,
            expected_digests=expected_network_digests,
            retryable_digests=retryable_network_digests,
        )
        or parsed_plan.network_policy == "DENY_ALL"
        and observed_network_commands
    ):
        raise ProfileRunnerError("CANDIDATE_PROFILE_NETWORK_OBSERVATION_INVALID")

    pulls = _closed_mapping(
        observation["externalPullObservation"],
        {
            "authority",
            "inventory",
            "observedCount",
            "observerIdentity",
            "pullDeniedCommandDigests",
            "result",
            "runtimeCommandInventoryDigest",
        },
        code=code,
    )
    inventory = pulls["inventory"]
    runtime_commands = [
        command
        for command in completed_commands
        if command["boundary"] == "RUNTIME"
    ]
    pull_denied_digests = sorted(
        command["argvDigest"]
        for command in runtime_commands
        if command["externalPullDisposition"] == "EXPLICIT_NEVER"
    )
    if (
        pulls["authority"] != "PRODUCTION_EXECUTION_COMMAND_BOUNDARY"
        or type(inventory) is not list
        or type(pulls["observedCount"]) is not int
        or pulls["observedCount"] != len(inventory)
        or pulls["observerIdentity"] != _COMMAND_OBSERVER_IDENTITY
        or pulls["pullDeniedCommandDigests"] != pull_denied_digests
        or pulls["runtimeCommandInventoryDigest"]
        != sha256_bytes(canonical_identity_bytes(runtime_commands))
        or pulls["result"] != "PASS"
    ):
        raise ProfileRunnerError(code)
    if inventory:
        raise ProfileRunnerError("CANDIDATE_PROFILE_EXTERNAL_PULL_ACTIVITY")
    normalized_runtime_commands = [
        command
        for command in normalized_commands
        if command["boundary"] == "RUNTIME"
    ]

    image_receipt = _closed_mapping(
        observation["imageAcquisitionReceipt"],
        {
            "identity",
            "images",
            "transportPolicyIdentity",
            "verifiedReleaseIdentity",
        },
        code=code,
    )
    if (
        type(image_receipt["identity"]) is not str
        or not _HEX_IDENTITY.fullmatch(image_receipt["identity"])
        or type(image_receipt["transportPolicyIdentity"]) is not str
        or not _HEX_IDENTITY.fullmatch(image_receipt["transportPolicyIdentity"])
        or image_receipt["verifiedReleaseIdentity"]
        != loaded.materials.identity_digest
        or observation["imageAcquisitionReceiptDigest"]
        != sha256_bytes(canonical_identity_bytes(image_receipt))
    ):
        raise ProfileRunnerError("CANDIDATE_PROFILE_IMAGE_OBSERVATION_MISMATCH")
    observed_images = image_receipt["images"]
    expected_images = {item.role: item.digest for item in loaded.images.images}
    if type(observed_images) is not list or len(observed_images) != len(expected_images):
        raise ProfileRunnerError("CANDIDATE_PROFILE_IMAGE_OBSERVATION_MISMATCH")
    observed_roles: set[str] = set()
    for observed_image_value in observed_images:
        image = _closed_mapping(
            observed_image_value,
            {"canonicalReference", "observedReference", "role"},
            code=code,
        )
        role = image["role"]
        expected_digest = expected_images.get(role)
        if (
            type(role) is not str
            or role in observed_roles
            or expected_digest is None
            or type(image["canonicalReference"]) is not str
            or image["canonicalReference"] != image["observedReference"]
            or not image["canonicalReference"].endswith(f"@{expected_digest}")
        ):
            raise ProfileRunnerError("CANDIDATE_PROFILE_IMAGE_OBSERVATION_MISMATCH")
        observed_roles.add(role)
    if observed_roles != set(expected_images):
        raise ProfileRunnerError("CANDIDATE_PROFILE_IMAGE_OBSERVATION_MISMATCH")
    runtime_readback = _closed_mapping(
        observation["imageRuntimeReadbackReceipt"],
        {"images", "result"},
        code=code,
    )
    if (
        runtime_readback["result"] != "PASS"
        or runtime_readback["images"] != observed_images
        or observation["imageRuntimeReadbackReceiptDigest"]
        != sha256_bytes(canonical_identity_bytes(runtime_readback))
    ):
        raise ProfileRunnerError("CANDIDATE_PROFILE_IMAGE_OBSERVATION_MISMATCH")
    return {
        "canonical_tests": canonical_tests,
        "completed_steps": completed_steps,
        "doctor_execution_identity": expected_doctor_execution_identity,
        "doctor_receipt_digest": doctor_digest,
        "external_pull_observation": {
            "authority": pulls["authority"],
            "inventory": list(inventory),
            "observed_count": pulls["observedCount"],
            "observer_identity": pulls["observerIdentity"],
            "pull_denied_command_digests": pulls["pullDeniedCommandDigests"],
            "result": pulls["result"],
            "runtime_command_inventory_digest": sha256_bytes(
                canonical_json_bytes(normalized_runtime_commands)
            ),
        },
        "image_acquisition_receipt_digest": observation[
            "imageAcquisitionReceiptDigest"
        ],
        "image_runtime_readback_receipt_digest": observation[
            "imageRuntimeReadbackReceiptDigest"
        ],
        "network_observation": {
            "authority": network["authority"],
            "completed_command_inventory_digest": sha256_bytes(
                canonical_json_bytes(normalized_commands)
            ),
            "completed_commands": normalized_commands,
            "destination_authority": network["destinationAuthority"],
            "egress_isolation": {
                "authority": egress["authority"],
                "container_network": egress["containerNetwork"],
                "container_network_internal": egress[
                    "containerNetworkInternal"
                ],
                "receipt_digest": receipt_digest,
                "service": egress["service"],
                "service_address_families": egress[
                    "serviceAddressFamilies"
                ],
            },
            "expected_network_command_digests": network[
                "expectedNetworkCommandDigests"
            ],
            "observer_identities": observer_identities,
            "platform_plan_digest": network["platformPlanDigest"],
            "policy": network["policy"],
            "retryable_network_command_digests": network[
                "retryableNetworkCommandDigests"
            ],
            "result": network["result"],
        },
    }


def build_profile_receipt(
    *,
    loaded,
    profile: str,
    context: Mapping[str, Any],
    installer_output: Mapping[str, Any],
    started_at: str,
    completed_at: str,
) -> dict[str, Any]:
    platform_plan_value = installer_output.get("platformPlan")
    platform = installer_output.get("platformBootstrapReceipt")
    result = installer_output.get("installerResult")
    if (
        type(platform_plan_value) is not dict
        or type(platform) is not dict
        or type(result) is not dict
        or installer_output.get("strictPostProvisionQualification") is not True
    ):
        raise ProfileRunnerError("CANDIDATE_INSTALLER_RESULT_INVALID")
    plan_digest = installer_output.get("installerPlanDigest")
    if type(plan_digest) is not str or not _DIGEST.fullmatch(plan_digest):
        raise ProfileRunnerError("CANDIDATE_INSTALLER_RESULT_INVALID")
    try:
        parsed_plan = parse_platform_bootstrap_plan(
            canonical_json_bytes(platform_plan_value)
        )
        parsed_receipt = parse_platform_bootstrap_receipt(
            canonical_json_bytes(platform),
            plan=parsed_plan,
        )
    except PlatformBootstrapError as error:
        raise ProfileRunnerError("CANDIDATE_INSTALLER_RESULT_INVALID") from error
    platform_plan = parsed_plan.plan_digest
    if (
        parsed_plan.mode.value != INSTALLER_PROFILES[profile]
        or parsed_receipt.result != "PASS"
        or parsed_receipt.plan_digest != platform_plan
    ):
        raise ProfileRunnerError("CANDIDATE_INSTALLER_RESULT_INVALID")
    succeeded = result.get("outcome") == "SUCCEEDED"
    images = {item.role: item.digest for item in loaded.images.images}
    facts = parsed_plan.initial_capabilities
    expected_initial_state = {
        "docker_present": facts.docker_cli_present
        and facts.docker_daemon_healthy,
        "runtime_dependencies_present": facts.compose_v2_present
        and facts.pg_dump_major is not None
        and facts.psql_major is not None,
        "network_allowed": parsed_plan.network_policy != "DENY_ALL",
    }
    if dict(context["initial_platform_state"]) != expected_initial_state:
        raise ProfileRunnerError("CANDIDATE_PROFILE_PLATFORM_STATE_MISMATCH")
    execution = _production_execution_observation(
        loaded=loaded,
        parsed_plan=parsed_plan,
        installer_result=result,
        value=installer_output.get("productionExecutionObservation"),
    )
    draft = {
        "schema": "animemo.prepublication-candidate-profile-receipt-draft/v1",
        "version": 1,
        "candidate_input_digest": loaded.verified["candidate_input_sha256"],
        "verified_candidate_digest": loaded.verified_digest,
        "qualification_run_id": loaded.candidate_input["qualification_run_id"],
        "qualification_run_attempt": loaded.candidate_input[
            "qualification_run_attempt"
        ],
        "source_sha": loaded.candidate_input["source_sha"],
        "source_tree": loaded.candidate_input["source_tree"],
        "candidate_version": loaded.candidate_input["candidate_version"],
        "profile": profile,
        "base_vm_identity": context["base_vm_identity"],
        "snapshot_identity": context["snapshot_identity"],
        "clone_identity": context["clone_identity"],
        "source_disk_graph_identity": context["source_disk_graph_identity"],
        "snapshot_disk_graph_identity": context[
            "snapshot_disk_graph_identity"
        ],
        "source_vm_inventory_identity": context[
            "source_vm_inventory_identity"
        ],
        "initial_platform_state": context["initial_platform_state"],
        "platform_bootstrap_plan_digest": platform_plan,
        "platform_bootstrap_receipt_digest": sha256_bytes(
            canonical_json_bytes(platform)
        ),
        "strict_platform_qualification": True,
        "instance_mutation_before_platform_qualification": 0,
        "installer_plan_digest": plan_digest,
        "installer_execution_receipt_digest": sha256_bytes(
            canonical_identity_bytes(result)
        ),
        "installer_execution_result": "PASS" if succeeded else "FAIL",
        "api_digest": images["api"],
        "web_digest": images["web"],
        "postgres_digest": images["postgres"],
        "redis_digest": images["redis"],
        "doctor_execution_identity": execution["doctor_execution_identity"],
        "doctor_receipt_digest": execution["doctor_receipt_digest"],
        "canonical_acceptance_tests": execution["canonical_tests"],
        "completed_steps": execution["completed_steps"],
        "network_observation": execution["network_observation"],
        "external_pull_observation": execution["external_pull_observation"],
        "image_acquisition_receipt_digest": execution[
            "image_acquisition_receipt_digest"
        ],
        "image_runtime_readback_receipt_digest": execution[
            "image_runtime_readback_receipt_digest"
        ],
        "release_authority_granted": False,
        "publish_authorized": False,
        "started_at": started_at,
        "completed_at": completed_at,
        "result": "PASS" if succeeded else "FAIL",
    }
    try:
        validation_only = {
            **draft,
            "schema": "animemo.prepublication-candidate-profile-receipt/v1",
            "original_vm_pre_hashes": dict(context["original_vm_pre_hashes"]),
            "original_vm_post_hashes": dict(context["original_vm_pre_hashes"]),
        }
        validate_profile_receipt(validation_only)
    except CandidateContractError as error:
        raise ProfileRunnerError(error.code) from error
    return draft


def execute_profile(
    *,
    verified_candidate_digest: str,
    profile: str,
    public_origin: str,
    context_b64url: str,
    runner: CommandRunner | None = None,
) -> dict[str, Any]:
    context = _decode_context(context_b64url)
    if context["profile"] != profile:
        raise ProfileRunnerError("CANDIDATE_PROFILE_CONTEXT_MISMATCH")
    try:
        loaded = load_verified_candidate(verified_candidate_digest)
    except CandidateContractError as error:
        raise ProfileRunnerError(error.code) from error
    started = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    installer_root = loaded.root / "installer-root"
    with _verified_wheel_runtime(installer_root) as runtime:
        environment = {
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
            "PYTHONPATH": os.pathsep.join((str(runtime), str(installer_root))),
            "PYTHONSAFEPATH": "1",
        }
        return_code, stdout, _ = (runner or SubprocessCommandRunner()).run(
            installer_argv(
                verified_candidate_digest=verified_candidate_digest,
                profile=profile,
                public_origin=public_origin,
            ),
            environment,
        )
    output = _result_json(stdout)
    if return_code != 0:
        raise ProfileRunnerError("CANDIDATE_INSTALLER_EXECUTION_FAILED")
    completed = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    return build_profile_receipt(
        loaded=loaded,
        profile=profile,
        context=context,
        installer_output=output,
        started_at=started,
        completed_at=completed,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="AniMemo Candidate profile runner")
    parser.add_argument("--verified-candidate-digest", required=True)
    parser.add_argument("--profile", choices=PROFILES, required=True)
    parser.add_argument("--public-origin", required=True)
    parser.add_argument("--execute", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        command = installer_argv(
            verified_candidate_digest=args.verified_candidate_digest,
            profile=args.profile,
            public_origin=args.public_origin,
        )
        if not args.execute:
            print(
                json.dumps(
                    {
                        "mode": "PLAN_ONLY",
                        "profile": args.profile,
                        "command": list(command[:-3]),
                        "releaseAuthorityGranted": False,
                        "publishAuthorized": False,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
            return 0
        context = os.environ.get(CONTEXT_ENV, "")
        receipt = execute_profile(
            verified_candidate_digest=args.verified_candidate_digest,
            profile=args.profile,
            public_origin=args.public_origin,
            context_b64url=context,
        )
        if RECEIPT_OUTPUT.exists() or RECEIPT_OUTPUT.is_symlink():
            raise ProfileRunnerError("CANDIDATE_PROFILE_RECEIPT_OUTPUT_EXISTS")
        RECEIPT_OUTPUT.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with RECEIPT_OUTPUT.open("xb") as output:
            output.write(canonical_json_bytes(receipt))
            output.flush()
            os.fsync(output.fileno())
        os.chmod(RECEIPT_OUTPUT, 0o600)
        print(
            json.dumps(
                {
                    "status": receipt["result"],
                    "receiptDraft": str(RECEIPT_OUTPUT),
                    "receiptDraftDigest": sha256_bytes(
                        canonical_json_bytes(receipt)
                    ),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0
    except ProfileRunnerError as error:
        print(json.dumps({"code": error.code}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
