from __future__ import annotations

import argparse
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
    "instance.locator": CheckDefinition("locator", "Instance locator", "Use the explicit Installer repair path."),
    "filesystem.roots": CheckDefinition("filesystem", "Canonical filesystem roots", "Correct the canonical instance layout explicitly."),
    "filesystem.permissions": CheckDefinition("filesystem", "Filesystem permissions", "Use an explicit permission repair operation."),
    "filesystem.capacity": CheckDefinition("filesystem", "Filesystem capacity", "Provide capacity before running a durable operation."),
    "configuration.required": CheckDefinition("configuration", "Required configuration", "Restore the protected canonical configuration."),
    "configuration.alignment": CheckDefinition("configuration", "Configuration alignment", "Reconcile locator and managed configuration explicitly."),
    "systemd.allowlist": CheckDefinition("runtime", "systemd allowlist", "Inspect the Installer-owned allowlist."),
    "compose.alignment": CheckDefinition("runtime", "Compose alignment", "Inspect the Installer-owned Compose deployment."),
    "network.listen": CheckDefinition("configuration", "Listen identity", "Choose an explicit supported listen identity."),
    "identity.public-origin": CheckDefinition("configuration", "Public Origin", "Set one canonical Public Origin."),
    "database.postgresql.connectivity": CheckDefinition("dependency", "PostgreSQL connectivity", "Inspect the AniMemo-scoped PostgreSQL service."),
    "database.schema-compatibility": CheckDefinition("data-integrity", "Database schema compatibility", "Use an approved compatibility path."),
    "cache.redis.connectivity": CheckDefinition("dependency", "Redis connectivity", "Inspect the AniMemo-scoped Redis service."),
    "cache.redis.persistence-contract": CheckDefinition("runtime", "Redis persistence contract", "Reconcile the rebuildable Redis profile."),
    "service.api.health": CheckDefinition("runtime", "API health", "Inspect the configured loopback API endpoint."),
    "service.web.health": CheckDefinition("runtime", "Web health", "Inspect the configured loopback Web endpoint."),
    "updater.socket": CheckDefinition("runtime", "Updater socket", "Inspect the fixed local Updater socket."),
    "updater.state": CheckDefinition("runtime", "Updater state snapshot", "Use a no-write Updater snapshot reader."),
    "release.identity": CheckDefinition("release", "Release identity", "Revalidate the exact Release Authority identity."),
    "release.updater-consistency": CheckDefinition("release", "Release and Updater consistency", "Reconcile through the approved Updater interface."),
    "plugins.integrity": CheckDefinition("data-integrity", "Plugin integrity", "Repair package/CAS state without deleting plugin data."),
    "media.integrity": CheckDefinition("data-integrity", "Media integrity", "Repair references explicitly without orphan deletion."),
    "backup.readiness": CheckDefinition("data-integrity", "Backup readiness", "Prepare a private canonical backup destination."),
    "compatibility.state": CheckDefinition("data-integrity", "Compatibility state", "Follow the canonical compatibility decision."),
}


class DoctorHost(ReadOnlyHost, Protocol):
    def user_id(self, name: str) -> int: ...

    def group_id(self, name: str) -> int: ...


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
            "compatibility": self.compatibility.as_dict() if self.compatibility else None,
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
        clock: Callable[[], str],
    ) -> None:
        self._host = host or LocalDoctorHost()
        self._probes = dict(probes or {})
        self._compatibility_evidence = compatibility
        self._clock = clock

    def _locator_result(self) -> tuple[ProbeResult, InstanceLocator | None]:
        try:
            updater_uid = self._host.user_id("animemo-updater")
            locator = load_instance_locator(
                self._host, expected_owner_uid=updater_uid
            )
        except OSError:
            return ProbeResult.failed("LOCATOR_OWNER_UNAVAILABLE"), None
        except LocatorError as error:
            return ProbeResult.failed(error.code), None
        return ProbeResult.passed("LOCATOR_VALID"), locator

    def _root_metadata(self) -> dict[PurePosixPath, os.stat_result]:
        roots = (APP_ROOT, DATA_ROOT, UPDATER_APP_ROOT, UPDATER_STATE_ROOT, UPDATER_RUNTIME_ROOT)
        return {root: self._host.lstat(root) for root in roots}

    def _filesystem_roots(self, _locator: InstanceLocator) -> ProbeResult:
        try:
            metadata = self._root_metadata()
        except (OSError, FileNotFoundError):
            return ProbeResult.failed("FILESYSTEM_ROOT_MISSING")
        if any(not stat.S_ISDIR(item.st_mode) or stat.S_ISLNK(item.st_mode) for item in metadata.values()):
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
        if locator_metadata.st_mode & 0o777 != 0o600 or locator_metadata.st_uid != updater_uid:
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
        if not isinstance(result, ProbeResult) or not isinstance(result.status, DoctorStatus):
            return ProbeResult.failed("PROBE_RESULT_INVALID")
        return result

    def run(self) -> DoctorReport:
        checked_at = self._clock()
        locator_result, locator = self._locator_result()
        results: list[DoctorCheck] = [_check("instance.locator", locator_result, checked_at)]
        compatibility_decision: CompatibilityDecision | None = None

        for check_id in DOCTOR_CHECK_IDS[1:]:
            if locator is None:
                result = ProbeResult.skipped("LOCATOR_DEPENDENCY_UNAVAILABLE")
            elif check_id == "compatibility.state":
                result, compatibility_decision = self._compatibility()
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
    lines.extend(f"{check.status.value:7} {check.check_id} [{check.code}]" for check in report.checks)
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    from datetime import UTC, datetime

    args = _parser().parse_args(argv)
    runner = DoctorRunner(clock=lambda: datetime.now(UTC).isoformat().replace("+00:00", "Z"))
    report = runner.run()
    if args.format == "json":
        print(json.dumps(report.as_dict(), ensure_ascii=False, sort_keys=True))
    else:
        print(_human(report))
    return {DoctorStatus.PASS: 0, DoctorStatus.WARN: 1, DoctorStatus.FAIL: 2}[report.overall_status]


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
