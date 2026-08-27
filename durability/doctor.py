from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Protocol

from .compatibility import (
    ArtifactIdentity,
    CompatibilityDecision,
    CompatibilityEvaluationError,
    CompatibilityOutcome,
    DimensionAssessment,
    evaluate_compatibility,
)
from .instance import (
    APP_ROOT,
    DATA_ROOT,
    INSTANCE_LOCATOR_PATH,
    UPDATER_APP_ROOT,
    UPDATER_RUNTIME_ROOT,
    UPDATER_STATE_ROOT,
    InstanceLocator,
    LocalReadOnlyHost,
    LocatorError,
    ReadOnlyHost,
    load_instance_locator,
)

DOCTOR_REPORT_FORMAT = "animemo-doctor-report"
DOCTOR_REPORT_VERSION = 1
DOCTOR_MODE = "READ-ONLY"

DISTRIBUTION_CHECK_IDS = (
    "distribution.transport-policy",
    "distribution.transport-receipt",
    "distribution.release-identity",
    "distribution.oci-identity",
    "distribution.plan-receipt-drift",
)

DOCTOR_CHECK_IDS = (
    "instance.locator",
    "filesystem.roots",
    "filesystem.permissions",
    "filesystem.capacity",
    "configuration.required",
    "configuration.alignment",
    "systemd.allowlist",
    "compose.alignment",
    "network.listen",
    "identity.public-origin",
    "database.postgresql.connectivity",
    "database.schema-compatibility",
    "cache.redis.connectivity",
    "cache.redis.persistence-contract",
    "service.api.health",
    "service.web.health",
    "updater.socket",
    "updater.state",
    "release.identity",
    "release.updater-consistency",
    *DISTRIBUTION_CHECK_IDS,
    "plugins.integrity",
    "media.integrity",
    "backup.readiness",
    "compatibility.state",
)

_BUILT_IN_CHECKS = frozenset(
    {
        "instance.locator",
        "filesystem.roots",
        "filesystem.permissions",
        "compatibility.state",
    }
)
_CODE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_LOCAL_IDENTITY = re.compile(r"^[0-9a-f]{64}$")
_SHA256_IDENTITY = re.compile(r"^sha256:[0-9a-f]{64}$")
_DISTRIBUTION_SNAPSHOT_FIELDS = frozenset(
    {
        "schemaVersion",
        "configuredTransportPolicy",
        "recentTransportReceipt",
        "verifiedReleaseIdentity",
        "verifiedOCIIdentity",
        "plan",
    }
)


class DoctorStatus(StrEnum):
    PASS = "PASS"
    WARN = "WARN"
    FAIL = "FAIL"
    SKIPPED = "SKIPPED"


@dataclass(frozen=True)
class ProbeResult:
    status: DoctorStatus
    code: str

    @classmethod
    def passed(cls, code: str) -> ProbeResult:
        return cls(DoctorStatus.PASS, code)

    @classmethod
    def warning(cls, code: str) -> ProbeResult:
        return cls(DoctorStatus.WARN, code)

    @classmethod
    def failed(cls, code: str) -> ProbeResult:
        return cls(DoctorStatus.FAIL, code)

    @classmethod
    def skipped(cls, code: str) -> ProbeResult:
        return cls(DoctorStatus.SKIPPED, code)


@dataclass(frozen=True)
class CompatibilityEvidence:
    artifact: ArtifactIdentity
    dimensions: tuple[DimensionAssessment, ...]


@dataclass(frozen=True)
class CheckDefinition:
    evidence_class: str
    label: str
    remediation: str


_DEFINITIONS: dict[str, CheckDefinition] = {
    "instance.locator": CheckDefinition(
        "locator", "Instance locator", "Use the explicit Installer repair path."
    ),
    "filesystem.roots": CheckDefinition(
        "filesystem",
        "Canonical filesystem roots",
        "Correct the canonical instance layout explicitly.",
    ),
    "filesystem.permissions": CheckDefinition(
        "filesystem",
        "Filesystem permissions",
        "Use an explicit permission repair operation.",
    ),
    "filesystem.capacity": CheckDefinition(
        "filesystem",
        "Filesystem capacity",
        "Provide capacity before running a durable operation.",
    ),
    "configuration.required": CheckDefinition(
        "configuration",
        "Required configuration",
        "Restore the protected canonical configuration.",
    ),
    "configuration.alignment": CheckDefinition(
        "configuration",
        "Configuration alignment",
        "Reconcile locator and managed configuration explicitly.",
    ),
    "systemd.allowlist": CheckDefinition(
        "runtime", "systemd allowlist", "Inspect the Installer-owned allowlist."
    ),
    "compose.alignment": CheckDefinition(
        "runtime",
        "Compose alignment",
        "Inspect the Installer-owned Compose deployment.",
    ),
    "network.listen": CheckDefinition(
        "configuration",
        "Listen identity",
        "Choose an explicit supported listen identity.",
    ),
    "identity.public-origin": CheckDefinition(
        "configuration", "Public Origin", "Set one canonical Public Origin."
    ),
    "database.postgresql.connectivity": CheckDefinition(
        "dependency",
        "PostgreSQL connectivity",
        "Inspect the AniMemo-scoped PostgreSQL service.",
    ),
    "database.schema-compatibility": CheckDefinition(
        "data-integrity",
        "Database schema compatibility",
        "Use an approved compatibility path.",
    ),
    "cache.redis.connectivity": CheckDefinition(
        "dependency", "Redis connectivity", "Inspect the AniMemo-scoped Redis service."
    ),
    "cache.redis.persistence-contract": CheckDefinition(
        "runtime",
        "Redis persistence contract",
        "Reconcile the rebuildable Redis profile.",
    ),
    "service.api.health": CheckDefinition(
        "runtime", "API health", "Inspect the configured loopback API endpoint."
    ),
    "service.web.health": CheckDefinition(
        "runtime", "Web health", "Inspect the configured loopback Web endpoint."
    ),
    "updater.socket": CheckDefinition(
        "runtime", "Updater socket", "Inspect the fixed local Updater socket."
    ),
    "updater.state": CheckDefinition(
        "runtime", "Updater state snapshot", "Use a no-write Updater snapshot reader."
    ),
    "release.identity": CheckDefinition(
        "release",
        "Release identity",
        "Revalidate the exact Release Authority identity.",
    ),
    "release.updater-consistency": CheckDefinition(
        "release",
        "Release and Updater consistency",
        "Reconcile through the approved Updater interface.",
    ),
    "distribution.transport-policy": CheckDefinition(
        "release",
        "Configured distribution transport policy",
        "Reconcile the persisted transport policy through the approved Updater path.",
    ),
    "distribution.transport-receipt": CheckDefinition(
        "release",
        "Recent distribution transport receipt",
        "Revalidate the recent local transport receipt without reacquiring materials.",
    ),
    "distribution.release-identity": CheckDefinition(
        "release",
        "Verified local release identity",
        "Reconcile the locally verified release with the canonical instance locator.",
    ),
    "distribution.oci-identity": CheckDefinition(
        "release",
        "Verified local OCI identity",
        "Reconcile the four locally verified OCI roles with the release identity.",
    ),
    "distribution.plan-receipt-drift": CheckDefinition(
        "release",
        "Distribution plan and receipt alignment",
        "Recreate an explicit plan through the approved local Updater path.",
    ),
    "plugins.integrity": CheckDefinition(
        "data-integrity",
        "Plugin integrity",
        "Repair package/CAS state without deleting plugin data.",
    ),
    "media.integrity": CheckDefinition(
        "data-integrity",
        "Media integrity",
        "Repair references explicitly without orphan deletion.",
    ),
    "backup.readiness": CheckDefinition(
        "data-integrity",
        "Backup readiness",
        "Prepare a private canonical backup destination.",
    ),
    "compatibility.state": CheckDefinition(
        "data-integrity",
        "Compatibility state",
        "Follow the canonical compatibility decision.",
    ),
}


class DoctorHost(ReadOnlyHost, Protocol):
    def user_id(self, name: str) -> int: ...

    def group_id(self, name: str) -> int: ...


class DistributionStateReader(Protocol):
    """Read one already-local, non-secret distribution state snapshot.

    Implementations are a no-write/no-refresh boundary: they may validate existing
    local plan and receipt bytes, but must not resolve, download, mirror, acquire,
    repair, or switch a configured transport source.
    """

    def read_local_snapshot(
        self, locator: InstanceLocator
    ) -> Mapping[str, object]: ...


class LocalDoctorHost(LocalReadOnlyHost):
    def user_id(self, name: str) -> int:
        if os.name == "nt":
            raise OSError("POSIX account lookup unavailable")
        import pwd

        return pwd.getpwnam(name).pw_uid

    def group_id(self, name: str) -> int:
        if os.name == "nt":
            raise OSError("POSIX group lookup unavailable")
        import grp

        return grp.getgrnam(name).gr_gid


@dataclass(frozen=True)
class DoctorCheck:
    check_id: str
    status: DoctorStatus
    code: str
    severity: str
    summary: str
    evidence_class: str
    remediation: str
    checked_at: str

    def as_dict(self) -> dict[str, object]:
        return {
            "checkId": self.check_id,
            "status": self.status.value,
            "code": self.code,
            "severity": self.severity,
            "summary": self.summary,
            "evidenceClass": self.evidence_class,
            "remediation": self.remediation,
            "checkedAt": self.checked_at,
        }


@dataclass(frozen=True)
class DoctorReport:
    checked_at: str
    overall_status: DoctorStatus
    checks: tuple[DoctorCheck, ...]
    instance_id: str | None
    deployment_profile: str | None
    compatibility: CompatibilityDecision | None

    def as_dict(self) -> dict[str, object]:
        return {
            "reportFormat": DOCTOR_REPORT_FORMAT,
            "reportVersion": DOCTOR_REPORT_VERSION,
            "checkedAt": self.checked_at,
            "instanceId": self.instance_id,
            "deploymentProfile": self.deployment_profile,
            "doctorIdentity": {"format": "animemo-doctor-runtime", "version": 1},
            "mode": DOCTOR_MODE,
            "overallStatus": self.overall_status.value,
            "checks": [check.as_dict() for check in self.checks],
            "compatibility": self.compatibility.as_dict()
            if self.compatibility
            else None,
        }


Probe = Callable[[InstanceLocator], ProbeResult]


def _severity(status: DoctorStatus) -> str:
    return {
        DoctorStatus.PASS: "info",
        DoctorStatus.WARN: "warning",
        DoctorStatus.FAIL: "error",
        DoctorStatus.SKIPPED: "error",
    }[status]


def _fixed_summary(check_id: str, status: DoctorStatus) -> str:
    label = _DEFINITIONS[check_id].label
    return {
        DoctorStatus.PASS: f"{label} passed.",
        DoctorStatus.WARN: f"{label} reported a warning.",
        DoctorStatus.FAIL: f"{label} failed.",
        DoctorStatus.SKIPPED: f"{label} was not executed.",
    }[status]


def _safe_code(code: object, fallback: str) -> str:
    rendered = str(code)
    return rendered if _CODE.fullmatch(rendered) else fallback


def _check(check_id: str, result: ProbeResult, checked_at: str) -> DoctorCheck:
    definition = _DEFINITIONS[check_id]
    return DoctorCheck(
        check_id=check_id,
        status=result.status,
        code=_safe_code(result.code, "PROBE_RESULT_INVALID"),
        severity=_severity(result.status),
        summary=_fixed_summary(check_id, result.status),
        evidence_class=definition.evidence_class,
        remediation=definition.remediation,
        checked_at=checked_at,
    )


class DoctorRunner:
    """Strictly read-only structural diagnostics for one canonical instance."""

    def __init__(
        self,
        *,
        host: DoctorHost | None = None,
        probes: Mapping[str, Probe] | None = None,
        compatibility: CompatibilityEvidence | None = None,
        distribution_reader: DistributionStateReader | None = None,
        clock: Callable[[], str],
    ) -> None:
        self._host = host or LocalDoctorHost()
        self._probes = dict(probes or {})
        self._compatibility_evidence = compatibility
        self._distribution_reader = distribution_reader
        self._clock = clock

    def _locator_result(self) -> tuple[ProbeResult, InstanceLocator | None]:
        try:
            updater_uid = self._host.user_id("animemo-updater")
            locator = load_instance_locator(self._host, expected_owner_uid=updater_uid)
        except OSError:
            return ProbeResult.failed("LOCATOR_OWNER_UNAVAILABLE"), None
        except LocatorError as error:
            return ProbeResult.failed(error.code), None
        return ProbeResult.passed("LOCATOR_VALID"), locator

    def _root_metadata(self) -> dict[PurePosixPath, os.stat_result]:
        roots = (
            APP_ROOT,
            DATA_ROOT,
            UPDATER_APP_ROOT,
            UPDATER_STATE_ROOT,
            UPDATER_RUNTIME_ROOT,
        )
        return {root: self._host.lstat(root) for root in roots}

    def _filesystem_roots(self, _locator: InstanceLocator) -> ProbeResult:
        try:
            metadata = self._root_metadata()
        except (OSError, FileNotFoundError):
            return ProbeResult.failed("FILESYSTEM_ROOT_MISSING")
        if any(
            not stat.S_ISDIR(item.st_mode) or stat.S_ISLNK(item.st_mode)
            for item in metadata.values()
        ):
            return ProbeResult.failed("FILESYSTEM_ROOT_INVALID")
        return ProbeResult.passed("FILESYSTEM_ROOTS_VALID")

    def _filesystem_permissions(self, _locator: InstanceLocator) -> ProbeResult:
        try:
            updater_uid = self._host.user_id("animemo-updater")
            api_gid = self._host.group_id("animemo-api")
            metadata = self._root_metadata()
            locator_metadata = self._host.lstat(INSTANCE_LOCATOR_PATH)
        except (OSError, FileNotFoundError):
            return ProbeResult.failed("FILESYSTEM_PERMISSIONS_UNAVAILABLE")
        expected = {
            APP_ROOT: (0o755, 0, 0),
            DATA_ROOT: (0o755, 0, 0),
            UPDATER_APP_ROOT: (0o755, 0, 0),
            UPDATER_STATE_ROOT: (0o700, updater_uid, api_gid),
            UPDATER_RUNTIME_ROOT: (0o750, updater_uid, api_gid),
        }
        if any(
            (item.st_mode & 0o777, item.st_uid, item.st_gid) != expected[path]
            for path, item in metadata.items()
        ):
            return ProbeResult.failed("FILESYSTEM_PERMISSIONS_INVALID")
        if (
            locator_metadata.st_mode & 0o777 != 0o600
            or locator_metadata.st_uid != updater_uid
        ):
            return ProbeResult.failed("FILESYSTEM_PERMISSIONS_INVALID")
        return ProbeResult.passed("FILESYSTEM_PERMISSIONS_VALID")

    def _compatibility(self) -> tuple[ProbeResult, CompatibilityDecision | None]:
        if self._compatibility_evidence is None:
            return ProbeResult.skipped("COMPATIBILITY_EVIDENCE_UNAVAILABLE"), None
        try:
            decision = evaluate_compatibility(
                "doctor",
                self._compatibility_evidence.artifact,
                self._compatibility_evidence.dimensions,
            )
        except CompatibilityEvaluationError:
            return ProbeResult.failed("COMPATIBILITY_EVALUATION_FAILED"), None
        status = {
            CompatibilityOutcome.COMPATIBLE: DoctorStatus.PASS,
            CompatibilityOutcome.REQUIRES_UPGRADE: DoctorStatus.WARN,
            CompatibilityOutcome.UNSUPPORTED: DoctorStatus.FAIL,
            CompatibilityOutcome.CORRUPT: DoctorStatus.FAIL,
        }[decision.outcome]
        return ProbeResult(status, f"COMPATIBILITY_{decision.outcome.value}"), decision

    def _run_builtin(self, check_id: str, locator: InstanceLocator) -> ProbeResult:
        functions = {
            "filesystem.roots": self._filesystem_roots,
            "filesystem.permissions": self._filesystem_permissions,
        }
        return functions[check_id](locator)

    def _run_probe(self, check_id: str, locator: InstanceLocator) -> ProbeResult:
        probe = self._probes.get(check_id)
        if probe is None:
            return ProbeResult.skipped("PROBE_UNAVAILABLE")
        try:
            result = probe(locator)
        except Exception:  # noqa: BLE001 - independent probes must not abort the report
            return ProbeResult.skipped("PROBE_UNAVAILABLE")
        if not isinstance(result, ProbeResult) or not isinstance(
            result.status, DoctorStatus
        ):
            return ProbeResult.failed("PROBE_RESULT_INVALID")
        return result

    def _distribution_snapshot(
        self, locator: InstanceLocator
    ) -> tuple[Mapping[str, object] | None, ProbeResult | None]:
        if self._distribution_reader is None:
            return None, ProbeResult.skipped("DISTRIBUTION_SNAPSHOT_UNAVAILABLE")
        try:
            snapshot = self._distribution_reader.read_local_snapshot(locator)
        except Exception:  # noqa: BLE001 - never expose local reader details
            return None, ProbeResult.skipped("DISTRIBUTION_SNAPSHOT_UNAVAILABLE")
        if (
            not isinstance(snapshot, Mapping)
            or set(snapshot) != _DISTRIBUTION_SNAPSHOT_FIELDS
            or snapshot.get("schemaVersion") != 1
            or isinstance(snapshot.get("schemaVersion"), bool)
        ):
            return None, ProbeResult.failed("DISTRIBUTION_SNAPSHOT_INVALID")
        return snapshot, None

    @staticmethod
    def _distribution_transport_policy(
        snapshot: Mapping[str, object]
    ) -> ProbeResult:
        policy = snapshot.get("configuredTransportPolicy")
        if not isinstance(policy, Mapping) or set(policy) != {
            "fallbackAllowed",
            "identity",
            "selectionOrigin",
            "source",
        }:
            return ProbeResult.failed("DISTRIBUTION_TRANSPORT_POLICY_INVALID")
        source = policy.get("source")
        selection_origin = policy.get("selectionOrigin")
        identity = policy.get("identity")
        if (
            source not in {"github", "official-mirror", "local-bundle"}
            or selection_origin
            not in {"explicit-admin-input", "persisted-instance-policy"}
            or type(policy.get("fallbackAllowed")) is not bool
            or policy.get("fallbackAllowed") is not False
            or not isinstance(identity, str)
            or _LOCAL_IDENTITY.fullmatch(identity) is None
        ):
            return ProbeResult.failed("DISTRIBUTION_TRANSPORT_POLICY_INVALID")
        policy_document = (
            {
                "authority": "github-release-attestation-sidecar",
                "fallback": "forbidden",
                "policyVersion": 1,
                "source": "local-bundle",
            }
            if source == "local-bundle"
            else {
                "fallback": "forbidden",
                "policy_version": 1,
                "selection_origin": selection_origin,
                "source": source,
            }
        )
        canonical = json.dumps(
            policy_document,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
        if hashlib.sha256(canonical).hexdigest() != identity:
            return ProbeResult.failed("DISTRIBUTION_TRANSPORT_POLICY_IDENTITY_INVALID")
        return ProbeResult.passed("DISTRIBUTION_TRANSPORT_POLICY_VALID")

    @staticmethod
    def _distribution_oci_identity(value: object) -> bool:
        return (
            isinstance(value, Mapping)
            and set(value) == {"api", "postgres", "redis", "web"}
            and all(
                isinstance(identity, str)
                and _SHA256_IDENTITY.fullmatch(identity) is not None
                for identity in value.values()
            )
        )

    @classmethod
    def _distribution_transport_receipt(
        cls, snapshot: Mapping[str, object]
    ) -> ProbeResult:
        receipt = snapshot.get("recentTransportReceipt")
        if receipt is None:
            return ProbeResult.skipped("DISTRIBUTION_TRANSPORT_RECEIPT_UNAVAILABLE")
        if not isinstance(receipt, Mapping) or set(receipt) != {
            "identity",
            "ociImages",
            "planIdentity",
            "policyIdentity",
            "releaseManifestDigest",
            "source",
            "valid",
        }:
            return ProbeResult.failed("DISTRIBUTION_TRANSPORT_RECEIPT_INVALID")
        if (
            type(receipt.get("valid")) is not bool
            or not isinstance(receipt.get("identity"), str)
            or _LOCAL_IDENTITY.fullmatch(receipt["identity"]) is None
            or not isinstance(receipt.get("planIdentity"), str)
            or _LOCAL_IDENTITY.fullmatch(receipt["planIdentity"]) is None
            or not isinstance(receipt.get("policyIdentity"), str)
            or _LOCAL_IDENTITY.fullmatch(receipt["policyIdentity"]) is None
            or receipt.get("source")
            not in {"github", "official-mirror", "local-bundle"}
            or not isinstance(receipt.get("releaseManifestDigest"), str)
            or _SHA256_IDENTITY.fullmatch(receipt["releaseManifestDigest"]) is None
            or not cls._distribution_oci_identity(receipt.get("ociImages"))
        ):
            return ProbeResult.failed("DISTRIBUTION_TRANSPORT_RECEIPT_INVALID")
        if receipt["valid"] is not True:
            return ProbeResult.failed("DISTRIBUTION_TRANSPORT_RECEIPT_INVALID")
        policy = snapshot.get("configuredTransportPolicy")
        if not isinstance(policy, Mapping) or receipt["policyIdentity"] != policy.get(
            "identity"
        ):
            return ProbeResult.failed(
                "DISTRIBUTION_TRANSPORT_RECEIPT_POLICY_MISMATCH"
            )
        if receipt.get("source") != policy.get("source"):
            return ProbeResult.failed(
                "DISTRIBUTION_TRANSPORT_RECEIPT_SOURCE_MISMATCH"
            )
        return ProbeResult.passed("DISTRIBUTION_TRANSPORT_RECEIPT_VALID")

    @staticmethod
    def _distribution_release_identity(
        snapshot: Mapping[str, object], locator: InstanceLocator
    ) -> ProbeResult:
        identity = snapshot.get("verifiedReleaseIdentity")
        expected = dict(locator.release_identity)
        if not isinstance(identity, Mapping) or set(identity) != set(expected):
            return ProbeResult.failed("DISTRIBUTION_RELEASE_IDENTITY_INVALID")
        if dict(identity) != expected:
            return ProbeResult.failed("DISTRIBUTION_RELEASE_IDENTITY_MISMATCH")
        return ProbeResult.passed("DISTRIBUTION_RELEASE_IDENTITY_VERIFIED")

    @classmethod
    def _verified_distribution_oci_identity(
        cls, snapshot: Mapping[str, object], locator: InstanceLocator
    ) -> ProbeResult:
        images = snapshot.get("verifiedOCIIdentity")
        if not cls._distribution_oci_identity(images):
            return ProbeResult.failed("DISTRIBUTION_OCI_IDENTITY_INVALID")
        if not isinstance(images, Mapping):
            return ProbeResult.failed("DISTRIBUTION_OCI_IDENTITY_INVALID")
        if (
            images["api"] != locator.release_identity["apiDigest"]
            or images["web"] != locator.release_identity["webDigest"]
        ):
            return ProbeResult.failed("DISTRIBUTION_OCI_IDENTITY_MISMATCH")
        return ProbeResult.passed("DISTRIBUTION_OCI_IDENTITY_VERIFIED")

    @classmethod
    def _distribution_plan_receipt_drift(
        cls, snapshot: Mapping[str, object]
    ) -> ProbeResult:
        plan = snapshot.get("plan")
        receipt = snapshot.get("recentTransportReceipt")
        if plan is None or receipt is None:
            return ProbeResult.skipped("DISTRIBUTION_PLAN_OR_RECEIPT_UNAVAILABLE")
        if not isinstance(plan, Mapping) or set(plan) != {
            "identity",
            "ociImages",
            "policyIdentity",
            "releaseManifestDigest",
            "source",
        }:
            return ProbeResult.failed("DISTRIBUTION_PLAN_INVALID")
        if not isinstance(receipt, Mapping) or set(receipt) != {
            "identity",
            "ociImages",
            "planIdentity",
            "policyIdentity",
            "releaseManifestDigest",
            "source",
            "valid",
        }:
            return ProbeResult.failed("DISTRIBUTION_TRANSPORT_RECEIPT_INVALID")
        if (
            type(receipt.get("valid")) is not bool
            or not isinstance(receipt.get("identity"), str)
            or _LOCAL_IDENTITY.fullmatch(receipt["identity"]) is None
            or not isinstance(receipt.get("planIdentity"), str)
            or _LOCAL_IDENTITY.fullmatch(receipt["planIdentity"]) is None
            or not isinstance(receipt.get("policyIdentity"), str)
            or _LOCAL_IDENTITY.fullmatch(receipt["policyIdentity"]) is None
            or receipt.get("source")
            not in {"github", "official-mirror", "local-bundle"}
            or not isinstance(receipt.get("releaseManifestDigest"), str)
            or _SHA256_IDENTITY.fullmatch(receipt["releaseManifestDigest"]) is None
            or not cls._distribution_oci_identity(receipt.get("ociImages"))
        ):
            return ProbeResult.failed("DISTRIBUTION_TRANSPORT_RECEIPT_INVALID")
        if (
            not isinstance(plan.get("identity"), str)
            or _LOCAL_IDENTITY.fullmatch(plan["identity"]) is None
            or not isinstance(plan.get("policyIdentity"), str)
            or _LOCAL_IDENTITY.fullmatch(plan["policyIdentity"]) is None
            or plan.get("source")
            not in {"github", "official-mirror", "local-bundle"}
            or not isinstance(plan.get("releaseManifestDigest"), str)
            or _SHA256_IDENTITY.fullmatch(plan["releaseManifestDigest"]) is None
            or not cls._distribution_oci_identity(plan.get("ociImages"))
        ):
            return ProbeResult.failed("DISTRIBUTION_PLAN_INVALID")
        verified_release = snapshot.get("verifiedReleaseIdentity")
        verified_oci = snapshot.get("verifiedOCIIdentity")
        if not isinstance(verified_release, Mapping) or not cls._distribution_oci_identity(
            verified_oci
        ):
            return ProbeResult.failed("DISTRIBUTION_VERIFIED_IDENTITY_INVALID")
        aligned = (
            receipt.get("valid") is True
            and plan["identity"] == receipt.get("planIdentity")
            and plan["policyIdentity"] == receipt.get("policyIdentity")
            and plan["source"] == receipt.get("source")
            and plan["releaseManifestDigest"]
            == receipt.get("releaseManifestDigest")
            == verified_release.get("manifestDigest")
            and dict(plan["ociImages"])
            == dict(receipt.get("ociImages", {}))
            == dict(verified_oci)
        )
        if not aligned:
            return ProbeResult.failed("DISTRIBUTION_PLAN_RECEIPT_DRIFT")
        return ProbeResult.passed("DISTRIBUTION_PLAN_RECEIPT_ALIGNED")

    def _distribution_result(
        self,
        check_id: str,
        snapshot: Mapping[str, object] | None,
        snapshot_error: ProbeResult | None,
        locator: InstanceLocator,
    ) -> ProbeResult:
        if snapshot_error is not None:
            return snapshot_error
        if snapshot is None:
            return ProbeResult.failed("DISTRIBUTION_SNAPSHOT_INVALID")
        if check_id == "distribution.release-identity":
            return self._distribution_release_identity(snapshot, locator)
        if check_id == "distribution.oci-identity":
            return self._verified_distribution_oci_identity(snapshot, locator)
        if check_id == "distribution.plan-receipt-drift":
            return self._distribution_plan_receipt_drift(snapshot)
        functions = {
            "distribution.transport-policy": self._distribution_transport_policy,
            "distribution.transport-receipt": self._distribution_transport_receipt,
        }
        return functions[check_id](snapshot)

    def run(self) -> DoctorReport:
        checked_at = self._clock()
        locator_result, locator = self._locator_result()
        results: list[DoctorCheck] = [
            _check("instance.locator", locator_result, checked_at)
        ]
        compatibility_decision: CompatibilityDecision | None = None
        distribution_snapshot: Mapping[str, object] | None = None
        distribution_error: ProbeResult | None = None
        if locator is not None:
            distribution_snapshot, distribution_error = self._distribution_snapshot(
                locator
            )

        for check_id in DOCTOR_CHECK_IDS[1:]:
            if locator is None:
                result = ProbeResult.skipped("LOCATOR_DEPENDENCY_UNAVAILABLE")
            elif check_id == "compatibility.state":
                result, compatibility_decision = self._compatibility()
            elif check_id in DISTRIBUTION_CHECK_IDS:
                result = self._distribution_result(
                    check_id, distribution_snapshot, distribution_error, locator
                )
            elif check_id in _BUILT_IN_CHECKS:
                try:
                    result = self._run_builtin(check_id, locator)
                except Exception:  # noqa: BLE001 - isolate one check without exposing details
                    result = ProbeResult.failed("CHECK_EXECUTION_FAILED")
            else:
                result = self._run_probe(check_id, locator)
            results.append(_check(check_id, result, checked_at))

        statuses = {result.status for result in results}
        if DoctorStatus.FAIL in statuses or DoctorStatus.SKIPPED in statuses:
            overall = DoctorStatus.FAIL
        elif DoctorStatus.WARN in statuses:
            overall = DoctorStatus.WARN
        else:
            overall = DoctorStatus.PASS
        return DoctorReport(
            checked_at=checked_at,
            overall_status=overall,
            checks=tuple(results),
            instance_id=locator.instance_id if locator else None,
            deployment_profile=locator.deployment_profile if locator else None,
            compatibility=compatibility_decision,
        )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m durability.doctor")
    parser.add_argument("--format", choices=("json", "human"), default="json")
    return parser


def _human(report: DoctorReport) -> str:
    lines = [f"AniMemo Doctor Basic: {report.overall_status.value}"]
    lines.extend(
        f"{check.status.value:7} {check.check_id} [{check.code}]"
        for check in report.checks
    )
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    from datetime import UTC, datetime

    args = _parser().parse_args(argv)
    runner = DoctorRunner(
        clock=lambda: datetime.now(UTC).isoformat().replace("+00:00", "Z")
    )
    report = runner.run()
    if args.format == "json":
        print(json.dumps(report.as_dict(), ensure_ascii=False, sort_keys=True))
    else:
        print(_human(report))
    return {DoctorStatus.PASS: 0, DoctorStatus.WARN: 1, DoctorStatus.FAIL: 2}[
        report.overall_status
    ]


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
