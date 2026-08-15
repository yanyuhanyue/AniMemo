from __future__ import annotations

import json
import os
import stat
import unittest
from dataclasses import dataclass
from pathlib import PurePosixPath

from durability.compatibility import (
    EVALUATION_ORDER,
    ArtifactIdentity,
    CompatibilityOutcome,
    Dimension,
    DimensionAssessment,
    ReasonCode,
)
from durability.doctor import (
    DOCTOR_CHECK_IDS,
    CompatibilityEvidence,
    DoctorRunner,
    DoctorStatus,
    ProbeResult,
)
from durability.instance import (
    APP_ROOT,
    BACKUP_ROOT,
    DATA_ROOT,
    INSTANCE_LOCATOR_PATH,
    UPDATER_APP_ROOT,
    UPDATER_RUNTIME_ROOT,
    UPDATER_SOCKET_PATH,
    UPDATER_STATE_ROOT,
    LocatorError,
    parse_instance_locator,
)

CHECKED_AT = "2026-08-15T16:00:00Z"
DIGEST = "sha256:" + "a" * 64


def locator_payload() -> dict[str, object]:
    return {
        "schemaVersion": 1,
        "instanceId": "12345678-1234-5678-9234-567812345678",
        "appRoot": "/opt/animemo",
        "dataRoot": "/data/animemo",
        "deploymentProfile": "v1.1-standard",
        "listen": {"host": "127.0.0.1", "port": 8088},
        "publicOrigin": "https://animemo.example",
        "managedConfigPath": "/data/animemo/config/animemo.json",
        "releaseIdentity": {
            "version": "v1.1.0",
            "channel": "rc",
            "commit": "a" * 40,
            "manifestDigest": DIGEST,
            "apiDigest": DIGEST,
            "webDigest": DIGEST,
        },
    }


def metadata(
    kind: int,
    mode: int,
    *,
    links: int = 1,
    uid: int = 0,
    gid: int = 0,
) -> os.stat_result:
    return os.stat_result((kind | mode, 1, 1, links, uid, gid, 128, 0, 0, 0))


@dataclass(frozen=True)
class Usage:
    total: int
    used: int
    free: int


class FakeReadOnlyHost:
    def __init__(self, *, include_locator: bool = True) -> None:
        self.calls: list[tuple[str, str]] = []
        self.payloads: dict[PurePosixPath, bytes] = {}
        self.metadata: dict[PurePosixPath, os.stat_result] = {
            APP_ROOT: metadata(stat.S_IFDIR, 0o755),
            DATA_ROOT: metadata(stat.S_IFDIR, 0o755),
            UPDATER_APP_ROOT: metadata(stat.S_IFDIR, 0o755),
            UPDATER_STATE_ROOT: metadata(stat.S_IFDIR, 0o700, uid=1000, gid=2000),
            UPDATER_RUNTIME_ROOT: metadata(stat.S_IFDIR, 0o750, uid=1000, gid=2000),
            PurePosixPath("/data/animemo/config/animemo.json"): metadata(
                stat.S_IFREG, 0o600, uid=1000, gid=2000
            ),
            BACKUP_ROOT: metadata(stat.S_IFDIR, 0o770, uid=10001, gid=2000),
            UPDATER_SOCKET_PATH: metadata(stat.S_IFSOCK, 0o660, uid=1000, gid=2000),
        }
        if include_locator:
            self.metadata[INSTANCE_LOCATOR_PATH] = metadata(stat.S_IFREG, 0o600, uid=1000, gid=2000)
            self.payloads[INSTANCE_LOCATOR_PATH] = json.dumps(locator_payload()).encode("utf-8")

    def lstat(self, path: PurePosixPath) -> os.stat_result:
        self.calls.append(("lstat", str(path)))
        try:
            return self.metadata[path]
        except KeyError as error:
            raise FileNotFoundError(str(path)) from error

    def read_bytes(self, path: PurePosixPath, *, limit: int) -> bytes:
        self.calls.append(("read_bytes", str(path)))
        return self.payloads[path][: limit + 1]

    def disk_usage(self, path: PurePosixPath) -> Usage:
        self.calls.append(("disk_usage", str(path)))
        if path not in self.metadata:
            raise FileNotFoundError(str(path))
        return Usage(total=10_000, used=1_000, free=9_000)

    def user_id(self, name: str) -> int:
        self.calls.append(("user_id", name))
        return {"animemo-updater": 1000}[name]

    def group_id(self, name: str) -> int:
        self.calls.append(("group_id", name))
        return {"animemo-api": 2000}[name]


COMPATIBLE_REASONS = {
    Dimension.FORMAT: ReasonCode.FORMAT_SUPPORTED,
    Dimension.INTEGRITY_AUTHENTICATION: ReasonCode.INTEGRITY_AUTHENTICATED,
    Dimension.DEPLOYMENT_CONTRACT: ReasonCode.DEPLOYMENT_CONTRACT_SUPPORTED,
    Dimension.SCHEMA_CONTRACTS: ReasonCode.SCHEMA_CONTRACTS_SUPPORTED,
    Dimension.EXACT_RELEASE_IDENTITY: ReasonCode.RELEASE_IDENTITY_VERIFIED,
    Dimension.PLATFORM_RUNTIME: ReasonCode.PLATFORM_RUNTIME_SUPPORTED,
    Dimension.SUPPORTED_PATH: ReasonCode.DIRECT_PATH_SUPPORTED,
}


def compatibility_evidence() -> CompatibilityEvidence:
    artifact = ArtifactIdentity(
        format_identity="animemo-doctor-target",
        format_version=1,
        artifact_id="12345678-1234-5678-9234-567812345678",
        manifest_digest=DIGEST,
    )
    dimensions = tuple(
        DimensionAssessment(
            name=dimension,
            outcome=CompatibilityOutcome.COMPATIBLE,
            reason_code=COMPATIBLE_REASONS[dimension],
            source={"identity": f"source-{dimension.value}"},
            target={"capability": f"target-{dimension.value}"},
        )
        for dimension in EVALUATION_ORDER
    )
    return CompatibilityEvidence(artifact=artifact, dimensions=dimensions)


def passing_probes() -> dict[str, object]:
    built_in = {
        "instance.locator",
        "filesystem.roots",
        "filesystem.permissions",
        "compatibility.state",
    }
    return {
        check_id: (lambda _locator, check_id=check_id: ProbeResult.passed(f"{check_id}.ok"))
        for check_id in DOCTOR_CHECK_IDS
        if check_id not in built_in
    }


class CanonicalLocatorTests(unittest.TestCase):
    def test_only_the_v11_canonical_locator_is_accepted(self):
        locator = parse_instance_locator(locator_payload())

        self.assertEqual(locator.app_root, APP_ROOT)
        self.assertEqual(locator.data_root, DATA_ROOT)
        self.assertEqual(locator.deployment_profile, "v1.1-standard")
        self.assertTrue(locator.listen.is_loopback)

    def test_legacy_roots_profiles_unknown_and_secret_fields_fail_closed(self):
        cases: list[tuple[str, object, str]] = [
            ("appRoot", "/opt/1panel/docker/compose/animemo/app", "LOCATOR_CANONICAL_ROOT_MISMATCH"),
            ("dataRoot", "/data/anime-journal", "LOCATOR_CANONICAL_ROOT_MISMATCH"),
            ("deploymentProfile", "v1.0-compatibility", "LOCATOR_PROFILE_UNSUPPORTED"),
            ("databasePassword", "must-not-appear", "LOCATOR_SECRET_FIELD_FORBIDDEN"),
            ("oldConfigPath", "/legacy/config", "LOCATOR_SCHEMA_INVALID"),
        ]

        for field, value, code in cases:
            payload = locator_payload()
            payload[field] = value
            with self.subTest(field=field):
                with self.assertRaises(LocatorError) as raised:
                    parse_instance_locator(payload)
                self.assertEqual(raised.exception.code, code)
                self.assertNotIn("must-not-appear", str(raised.exception))

        payload = locator_payload()
        payload["releaseIdentity"] = {"nested": {"accessToken": "must-not-appear"}}
        with self.assertRaises(LocatorError) as raised:
            parse_instance_locator(payload)
        self.assertEqual(raised.exception.code, "LOCATOR_SECRET_FIELD_FORBIDDEN")
        self.assertNotIn("must-not-appear", repr(raised.exception))


class DoctorBasicRuntimeTests(unittest.TestCase):
    def test_complete_read_only_report_uses_stable_schema_and_check_ids(self):
        host = FakeReadOnlyHost()
        report = DoctorRunner(
            host=host,
            probes=passing_probes(),
            compatibility=compatibility_evidence(),
            clock=lambda: CHECKED_AT,
        ).run()

        rendered = report.as_dict()
        self.assertEqual(rendered["reportFormat"], "animemo-doctor-report")
        self.assertEqual(rendered["reportVersion"], 1)
        self.assertEqual(rendered["mode"], "READ-ONLY")
        self.assertEqual(rendered["overallStatus"], "PASS")
        self.assertEqual([item["checkId"] for item in rendered["checks"]], list(DOCTOR_CHECK_IDS))
        self.assertEqual({item["status"] for item in rendered["checks"]}, {"PASS"})
        self.assertEqual(rendered["compatibility"]["overallStatus"], "COMPATIBLE")
        self.assertEqual(
            {call[0] for call in host.calls},
            {"lstat", "read_bytes", "user_id", "group_id"},
        )

    def test_missing_locator_does_not_scan_legacy_paths_or_call_dependent_probes(self):
        host = FakeReadOnlyHost(include_locator=False)
        called: list[str] = []
        probes = {
            check_id: (lambda _locator, check_id=check_id: called.append(check_id))
            for check_id in DOCTOR_CHECK_IDS
        }

        report = DoctorRunner(host=host, probes=probes, clock=lambda: CHECKED_AT).run()

        self.assertEqual(report.overall_status, DoctorStatus.FAIL)
        self.assertEqual(called, [])
        inspected = {path for operation, path in host.calls if operation == "lstat"}
        self.assertEqual(inspected, {str(INSTANCE_LOCATOR_PATH)})
        self.assertFalse(any("1panel" in path or "anime-journal" in path for path in inspected))

    def test_probe_exception_is_redacted_and_does_not_abort_independent_checks(self):
        marker = "must-not-appear"
        probes = passing_probes()

        def failing_probe(_locator):
            raise RuntimeError(f"credential={marker}")

        probes["database.postgresql.connectivity"] = failing_probe
        report = DoctorRunner(
            host=FakeReadOnlyHost(),
            probes=probes,
            compatibility=compatibility_evidence(),
            clock=lambda: CHECKED_AT,
        ).run()
        rendered = json.dumps(report.as_dict(), ensure_ascii=False)

        self.assertNotIn(marker, rendered)
        self.assertEqual(report.overall_status, DoctorStatus.FAIL)
        by_id = {check.check_id: check for check in report.checks}
        self.assertEqual(by_id["database.postgresql.connectivity"].status, DoctorStatus.SKIPPED)
        self.assertEqual(by_id["service.web.health"].status, DoctorStatus.PASS)

    def test_owner_mismatch_fails_without_repairing_metadata(self):
        host = FakeReadOnlyHost()
        host.metadata[APP_ROOT] = metadata(stat.S_IFDIR, 0o755, uid=99, gid=0)
        report = DoctorRunner(
            host=host,
            probes=passing_probes(),
            compatibility=compatibility_evidence(),
            clock=lambda: CHECKED_AT,
        ).run()
        by_id = {check.check_id: check for check in report.checks}

        self.assertEqual(by_id["filesystem.permissions"].status, DoctorStatus.FAIL)
        self.assertEqual(report.overall_status, DoctorStatus.FAIL)
        self.assertFalse(hasattr(host, "chmod"))

    def test_locator_owner_mismatch_blocks_all_dependent_probes(self):
        host = FakeReadOnlyHost()
        host.metadata[INSTANCE_LOCATOR_PATH] = metadata(
            stat.S_IFREG, 0o600, uid=99, gid=2000
        )
        called: list[str] = []
        probes = {
            check_id: (lambda _locator, check_id=check_id: called.append(check_id))
            for check_id in DOCTOR_CHECK_IDS
        }

        report = DoctorRunner(
            host=host,
            probes=probes,
            compatibility=compatibility_evidence(),
            clock=lambda: CHECKED_AT,
        ).run()
        by_id = {check.check_id: check for check in report.checks}

        self.assertEqual(by_id["instance.locator"].code, "LOCATOR_OWNER_INVALID")
        self.assertEqual(by_id["instance.locator"].status, DoctorStatus.FAIL)
        self.assertEqual(called, [])
        self.assertNotIn(("read_bytes", str(INSTANCE_LOCATOR_PATH)), host.calls)

    def test_duplicate_locator_fields_fail_before_dependent_probes(self):
        host = FakeReadOnlyHost()
        host.payloads[INSTANCE_LOCATOR_PATH] = (
            b'{"schemaVersion":1,"schemaVersion":1}'
        )

        report = DoctorRunner(host=host, clock=lambda: CHECKED_AT).run()
        by_id = {check.check_id: check for check in report.checks}

        self.assertEqual(by_id["instance.locator"].code, "LOCATOR_CONTENT_INVALID")
        self.assertEqual(report.overall_status, DoctorStatus.FAIL)

    def test_compatibility_state_is_mapped_by_the_canonical_engine(self):
        evidence = compatibility_evidence()
        dimensions = list(evidence.dimensions)
        dimensions[1] = DimensionAssessment(
            name=Dimension.INTEGRITY_AUTHENTICATION,
            outcome=CompatibilityOutcome.CORRUPT,
            reason_code=ReasonCode.AUTHENTICATION_FAILED,
            source={"identity": "source-integrity"},
            target={"capability": "target-integrity"},
        )
        report = DoctorRunner(
            host=FakeReadOnlyHost(),
            probes=passing_probes(),
            compatibility=CompatibilityEvidence(evidence.artifact, tuple(dimensions)),
            clock=lambda: CHECKED_AT,
        ).run()
        by_id = {check.check_id: check for check in report.checks}

        self.assertEqual(report.compatibility.outcome, CompatibilityOutcome.CORRUPT)
        self.assertEqual(by_id["compatibility.state"].status, DoctorStatus.FAIL)
        self.assertEqual(report.overall_status, DoctorStatus.FAIL)

    def test_only_four_check_statuses_exist(self):
        self.assertEqual(
            tuple(status.value for status in DoctorStatus),
            ("PASS", "WARN", "FAIL", "SKIPPED"),
        )
        self.assertNotIn("UNKNOWN", {status.value for status in DoctorStatus})


if __name__ == "__main__":
    unittest.main()
