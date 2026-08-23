from __future__ import annotations

import hashlib
import json
import os
import stat
import unittest
from dataclasses import dataclass
from pathlib import PurePosixPath
from unittest import mock

from durability.compatibility import (
    EVALUATION_ORDER,
    ArtifactIdentity,
    CompatibilityOutcome,
    Dimension,
    DimensionAssessment,
    ReasonCode,
)
from durability.doctor import (
    DISTRIBUTION_CHECK_IDS,
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
    SecureFileSnapshot,
    parse_instance_locator,
)

CHECKED_AT = "2026-08-15T16:00:00Z"
DIGEST = "sha256:" + "a" * 64


def transport_policy_identity() -> str:
    payload = json.dumps(
        {
            "fallback": "forbidden",
            "policy_version": 1,
            "selection_origin": "persisted-instance-policy",
            "source": "github",
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    return hashlib.sha256(payload).hexdigest()


IDENTITY = transport_policy_identity()


def locator_payload() -> dict[str, object]:
    return {
        "schemaVersion": 2,
        "instanceName": "default",
        "instanceId": "12345678-1234-5678-9234-567812345678",
        "appRoot": "/opt/animemo-instances/default",
        "dataRoot": "/data/animemo-instances/default",
        "updaterStateRoot": "/var/lib/animemo-updater/instances/default",
        "updaterRuntimeRoot": "/run/animemo-updater/default",
        "deploymentProfile": "v1.1-instance-scoped",
        "composeProject": "animemo-default",
        "updaterService": "animemo-updater@default.service",
        "updaterSocketPath": "/run/animemo-updater/default/updater.sock",
        "listen": {"host": "127.0.0.1", "port": 8088},
        "publicOrigin": "https://animemo.example",
        "managedConfigPath": "/data/animemo-instances/default/config/animemo.json",
        "configRevision": "11111111-1111-4111-8111-111111111111",
        "releaseIdentity": {
            "version": "v1.1.0-rc.1",
            "channel": "rc",
            "commit": "a" * 40,
            "manifestDigest": DIGEST,
            "apiDigest": DIGEST,
            "webDigest": DIGEST,
        },
        "ownershipReceiptDigest": DIGEST,
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
            self.metadata[INSTANCE_LOCATOR_PATH] = metadata(
                stat.S_IFREG, 0o600, uid=1000, gid=2000
            )
            self.payloads[INSTANCE_LOCATOR_PATH] = json.dumps(locator_payload()).encode(
                "utf-8"
            )

    def lstat(self, path: PurePosixPath) -> os.stat_result:
        self.calls.append(("lstat", str(path)))
        try:
            return self.metadata[path]
        except KeyError as error:
            raise FileNotFoundError(str(path)) from error

    def read_bytes(self, path: PurePosixPath, *, limit: int) -> bytes:
        self.calls.append(("read_bytes", str(path)))
        return self.payloads[path][: limit + 1]

    def read_secure_bytes(
        self,
        path: PurePosixPath,
        *,
        limit: int,
        expected_owner_uid: int | None = None,
        expected_owner_gid: int | None = None,
        required_mode: int = 0o600,
    ) -> SecureFileSnapshot:
        self.calls.append(("read_secure_bytes", str(path)))
        metadata_value = self.lstat(path)
        if (
            expected_owner_uid is not None
            and metadata_value.st_uid != expected_owner_uid
        ):
            raise LocatorError("LOCATOR_OWNER_INVALID")
        if (
            expected_owner_gid is not None
            and metadata_value.st_gid != expected_owner_gid
        ):
            raise LocatorError("LOCATOR_GROUP_INVALID")
        if stat.S_IMODE(metadata_value.st_mode) != required_mode:
            raise LocatorError("LOCATOR_PERMISSIONS_INVALID")
        return SecureFileSnapshot(self.payloads[path][: limit + 1], metadata_value)

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
        *DISTRIBUTION_CHECK_IDS,
    }
    return {
        check_id: (
            lambda _locator, check_id=check_id: ProbeResult.passed(f"{check_id}.ok")
        )
        for check_id in DOCTOR_CHECK_IDS
        if check_id not in built_in
    }


def distribution_snapshot() -> dict[str, object]:
    images = {
        "api": DIGEST,
        "postgres": DIGEST,
        "redis": DIGEST,
        "web": DIGEST,
    }
    return {
        "schemaVersion": 1,
        "configuredTransportPolicy": {
            "fallbackAllowed": False,
            "identity": IDENTITY,
            "selectionOrigin": "persisted-instance-policy",
            "source": "github",
        },
        "recentTransportReceipt": {
            "identity": "c" * 64,
            "ociImages": images,
            "planIdentity": "d" * 64,
            "policyIdentity": IDENTITY,
            "releaseManifestDigest": DIGEST,
            "source": "github",
            "valid": True,
        },
        "verifiedReleaseIdentity": dict(locator_payload()["releaseIdentity"]),
        "verifiedOCIIdentity": images,
        "plan": {
            "identity": "d" * 64,
            "ociImages": images,
            "policyIdentity": IDENTITY,
            "releaseManifestDigest": DIGEST,
            "source": "github",
        },
    }


class NetworkSentinel:
    def __init__(self) -> None:
        self.call_count = 0

    def call(self) -> None:
        self.call_count += 1
        raise AssertionError("Doctor must not use a distribution network adapter")


class FakeDistributionReader:
    def __init__(self, snapshot: dict[str, object], network: NetworkSentinel) -> None:
        self.snapshot = snapshot
        self.network = network
        self.calls: list[str] = []

    def read_local_snapshot(self, locator) -> dict[str, object]:
        self.calls.append(locator.instance_id)
        return self.snapshot


class CanonicalLocatorTests(unittest.TestCase):
    def test_only_the_v11_canonical_locator_is_accepted(self):
        locator = parse_instance_locator(locator_payload())

        self.assertEqual(locator.app_root, APP_ROOT)
        self.assertEqual(locator.data_root, DATA_ROOT)
        self.assertEqual(locator.deployment_profile, "v1.1-instance-scoped")
        self.assertTrue(locator.listen.is_loopback)

    def test_legacy_roots_profiles_unknown_and_secret_fields_fail_closed(self):
        cases: list[tuple[str, object, str]] = [
            (
                "appRoot",
                "/opt/1panel/docker/compose/animemo/app",
                "LOCATOR_CANONICAL_ROOT_MISMATCH",
            ),
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
    def test_distribution_transport_policy_uses_only_the_closed_local_snapshot(self):
        network = NetworkSentinel()
        reader = FakeDistributionReader(distribution_snapshot(), network)
        with mock.patch(
            "socket.socket", side_effect=AssertionError("network forbidden")
        ) as socket_guard, mock.patch(
            "socket.create_connection",
            side_effect=AssertionError("network forbidden"),
        ) as connection_guard, mock.patch(
            "socket.getaddrinfo", side_effect=AssertionError("DNS forbidden")
        ) as dns_guard:
            report = DoctorRunner(
                host=FakeReadOnlyHost(),
                probes=passing_probes(),
                compatibility=compatibility_evidence(),
                distribution_reader=reader,
                clock=lambda: CHECKED_AT,
            ).run()

        by_id = {check.check_id: check for check in report.checks}
        self.assertEqual(
            DISTRIBUTION_CHECK_IDS,
            (
                "distribution.transport-policy",
                "distribution.transport-receipt",
                "distribution.release-identity",
                "distribution.oci-identity",
                "distribution.plan-receipt-drift",
            ),
        )
        self.assertEqual(
            by_id["distribution.transport-policy"].status, DoctorStatus.PASS
        )
        self.assertEqual(
            by_id["distribution.transport-policy"].code,
            "DISTRIBUTION_TRANSPORT_POLICY_VALID",
        )
        self.assertEqual(reader.calls, [locator_payload()["instanceId"]])
        self.assertEqual(network.call_count, 0)
        self.assertEqual(socket_guard.call_count, 0)
        self.assertEqual(connection_guard.call_count, 0)
        self.assertEqual(dns_guard.call_count, 0)

    def test_distribution_snapshot_failure_is_redacted_and_does_not_call_network(self):
        marker = "credential=must-not-appear"
        network = NetworkSentinel()

        class BrokenLocalReader:
            def read_local_snapshot(self, _locator):
                raise RuntimeError(marker)

        report = DoctorRunner(
            host=FakeReadOnlyHost(),
            probes=passing_probes(),
            compatibility=compatibility_evidence(),
            distribution_reader=BrokenLocalReader(),
            clock=lambda: CHECKED_AT,
        ).run()
        by_id = {check.check_id: check for check in report.checks}

        self.assertNotIn(marker, json.dumps(report.as_dict(), ensure_ascii=False))
        self.assertEqual(
            {
                by_id[check_id].code
                for check_id in DISTRIBUTION_CHECK_IDS
            },
            {"DISTRIBUTION_SNAPSHOT_UNAVAILABLE"},
        )
        self.assertEqual(
            {by_id[check_id].status for check_id in DISTRIBUTION_CHECK_IDS},
            {DoctorStatus.SKIPPED},
        )
        self.assertEqual(by_id["service.api.health"].status, DoctorStatus.PASS)
        self.assertEqual(network.call_count, 0)

    def test_distribution_snapshot_schema_is_closed_and_secret_safe(self):
        marker = "must-not-appear"
        snapshot = distribution_snapshot()
        snapshot["credential"] = marker
        report = DoctorRunner(
            host=FakeReadOnlyHost(),
            probes=passing_probes(),
            compatibility=compatibility_evidence(),
            distribution_reader=FakeDistributionReader(
                snapshot, NetworkSentinel()
            ),
            clock=lambda: CHECKED_AT,
        ).run()
        by_id = {check.check_id: check for check in report.checks}

        self.assertNotIn(marker, json.dumps(report.as_dict(), ensure_ascii=False))
        self.assertEqual(
            {by_id[check_id].code for check_id in DISTRIBUTION_CHECK_IDS},
            {"DISTRIBUTION_SNAPSHOT_INVALID"},
        )
        self.assertEqual(
            {by_id[check_id].status for check_id in DISTRIBUTION_CHECK_IDS},
            {DoctorStatus.FAIL},
        )

    def test_malformed_distribution_subsection_does_not_abort_other_checks(self):
        marker = "credential=must-not-appear"
        snapshot = distribution_snapshot()
        snapshot["recentTransportReceipt"] = {
            **snapshot["recentTransportReceipt"],
            "ociImages": marker,
        }

        report = DoctorRunner(
            host=FakeReadOnlyHost(),
            probes=passing_probes(),
            compatibility=compatibility_evidence(),
            distribution_reader=FakeDistributionReader(
                snapshot, NetworkSentinel()
            ),
            clock=lambda: CHECKED_AT,
        ).run()
        by_id = {check.check_id: check for check in report.checks}

        self.assertNotIn(marker, json.dumps(report.as_dict(), ensure_ascii=False))
        self.assertEqual(
            by_id["distribution.transport-receipt"].status, DoctorStatus.FAIL
        )
        self.assertEqual(
            by_id["distribution.plan-receipt-drift"].status, DoctorStatus.FAIL
        )
        self.assertEqual(
            by_id["distribution.transport-policy"].status, DoctorStatus.PASS
        )
        self.assertEqual(by_id["service.web.health"].status, DoctorStatus.PASS)

    def test_recent_transport_receipt_is_validated_against_the_local_policy(self):
        network = NetworkSentinel()
        valid_reader = FakeDistributionReader(distribution_snapshot(), network)
        valid = DoctorRunner(
            host=FakeReadOnlyHost(),
            probes=passing_probes(),
            compatibility=compatibility_evidence(),
            distribution_reader=valid_reader,
            clock=lambda: CHECKED_AT,
        ).run()
        valid_check = {
            check.check_id: check for check in valid.checks
        }["distribution.transport-receipt"]
        self.assertEqual(valid_check.status, DoctorStatus.PASS)
        self.assertEqual(valid_check.code, "DISTRIBUTION_TRANSPORT_RECEIPT_VALID")

        invalid_snapshot = distribution_snapshot()
        invalid_snapshot["recentTransportReceipt"] = {
            **invalid_snapshot["recentTransportReceipt"],
            "policyIdentity": "e" * 64,
        }
        invalid = DoctorRunner(
            host=FakeReadOnlyHost(),
            probes=passing_probes(),
            compatibility=compatibility_evidence(),
            distribution_reader=FakeDistributionReader(invalid_snapshot, network),
            clock=lambda: CHECKED_AT,
        ).run()
        invalid_check = {
            check.check_id: check for check in invalid.checks
        }["distribution.transport-receipt"]
        self.assertEqual(invalid_check.status, DoctorStatus.FAIL)
        self.assertEqual(
            invalid_check.code, "DISTRIBUTION_TRANSPORT_RECEIPT_POLICY_MISMATCH"
        )

        unverified_snapshot = distribution_snapshot()
        unverified_snapshot["recentTransportReceipt"] = {
            **unverified_snapshot["recentTransportReceipt"],
            "valid": False,
        }
        unverified = DoctorRunner(
            host=FakeReadOnlyHost(),
            probes=passing_probes(),
            compatibility=compatibility_evidence(),
            distribution_reader=FakeDistributionReader(unverified_snapshot, network),
            clock=lambda: CHECKED_AT,
        ).run()
        unverified_check = {check.check_id: check for check in unverified.checks}[
            "distribution.transport-receipt"
        ]
        self.assertEqual(unverified_check.status, DoctorStatus.FAIL)
        self.assertEqual(
            unverified_check.code, "DISTRIBUTION_TRANSPORT_RECEIPT_INVALID"
        )
        self.assertEqual(network.call_count, 0)

    def test_verified_release_identity_must_equal_the_canonical_locator_identity(self):
        network = NetworkSentinel()
        valid = DoctorRunner(
            host=FakeReadOnlyHost(),
            probes=passing_probes(),
            compatibility=compatibility_evidence(),
            distribution_reader=FakeDistributionReader(
                distribution_snapshot(), network
            ),
            clock=lambda: CHECKED_AT,
        ).run()
        valid_check = {check.check_id: check for check in valid.checks}[
            "distribution.release-identity"
        ]
        self.assertEqual(valid_check.status, DoctorStatus.PASS)
        self.assertEqual(valid_check.code, "DISTRIBUTION_RELEASE_IDENTITY_VERIFIED")

        drifted_snapshot = distribution_snapshot()
        drifted_snapshot["verifiedReleaseIdentity"] = {
            **drifted_snapshot["verifiedReleaseIdentity"],
            "manifestDigest": "sha256:" + "f" * 64,
        }
        drifted = DoctorRunner(
            host=FakeReadOnlyHost(),
            probes=passing_probes(),
            compatibility=compatibility_evidence(),
            distribution_reader=FakeDistributionReader(drifted_snapshot, network),
            clock=lambda: CHECKED_AT,
        ).run()
        drifted_check = {check.check_id: check for check in drifted.checks}[
            "distribution.release-identity"
        ]
        self.assertEqual(drifted_check.status, DoctorStatus.FAIL)
        self.assertEqual(
            drifted_check.code, "DISTRIBUTION_RELEASE_IDENTITY_MISMATCH"
        )
        self.assertEqual(network.call_count, 0)

    def test_verified_oci_identity_requires_four_roles_and_locator_api_web_binding(self):
        network = NetworkSentinel()
        valid = DoctorRunner(
            host=FakeReadOnlyHost(),
            probes=passing_probes(),
            compatibility=compatibility_evidence(),
            distribution_reader=FakeDistributionReader(
                distribution_snapshot(), network
            ),
            clock=lambda: CHECKED_AT,
        ).run()
        valid_check = {check.check_id: check for check in valid.checks}[
            "distribution.oci-identity"
        ]
        self.assertEqual(valid_check.status, DoctorStatus.PASS)
        self.assertEqual(valid_check.code, "DISTRIBUTION_OCI_IDENTITY_VERIFIED")

        drifted_snapshot = distribution_snapshot()
        drifted_snapshot["verifiedOCIIdentity"] = {
            **drifted_snapshot["verifiedOCIIdentity"],
            "web": "sha256:" + "f" * 64,
        }
        drifted = DoctorRunner(
            host=FakeReadOnlyHost(),
            probes=passing_probes(),
            compatibility=compatibility_evidence(),
            distribution_reader=FakeDistributionReader(drifted_snapshot, network),
            clock=lambda: CHECKED_AT,
        ).run()
        drifted_check = {check.check_id: check for check in drifted.checks}[
            "distribution.oci-identity"
        ]
        self.assertEqual(drifted_check.status, DoctorStatus.FAIL)
        self.assertEqual(drifted_check.code, "DISTRIBUTION_OCI_IDENTITY_MISMATCH")
        self.assertEqual(network.call_count, 0)

    def test_distribution_plan_and_receipt_must_bind_the_same_verified_identities(self):
        network = NetworkSentinel()
        valid = DoctorRunner(
            host=FakeReadOnlyHost(),
            probes=passing_probes(),
            compatibility=compatibility_evidence(),
            distribution_reader=FakeDistributionReader(
                distribution_snapshot(), network
            ),
            clock=lambda: CHECKED_AT,
        ).run()
        valid_check = {check.check_id: check for check in valid.checks}[
            "distribution.plan-receipt-drift"
        ]
        self.assertEqual(valid_check.status, DoctorStatus.PASS)
        self.assertEqual(valid_check.code, "DISTRIBUTION_PLAN_RECEIPT_ALIGNED")

        drifted_snapshot = distribution_snapshot()
        drifted_snapshot["recentTransportReceipt"] = {
            **drifted_snapshot["recentTransportReceipt"],
            "planIdentity": "e" * 64,
        }
        drifted = DoctorRunner(
            host=FakeReadOnlyHost(),
            probes=passing_probes(),
            compatibility=compatibility_evidence(),
            distribution_reader=FakeDistributionReader(drifted_snapshot, network),
            clock=lambda: CHECKED_AT,
        ).run()
        drifted_check = {check.check_id: check for check in drifted.checks}[
            "distribution.plan-receipt-drift"
        ]
        self.assertEqual(drifted_check.status, DoctorStatus.FAIL)
        self.assertEqual(drifted_check.code, "DISTRIBUTION_PLAN_RECEIPT_DRIFT")
        self.assertEqual(network.call_count, 0)

    def test_complete_read_only_report_uses_stable_schema_and_check_ids(self):
        host = FakeReadOnlyHost()
        network = NetworkSentinel()
        report = DoctorRunner(
            host=host,
            probes=passing_probes(),
            compatibility=compatibility_evidence(),
            distribution_reader=FakeDistributionReader(
                distribution_snapshot(), network
            ),
            clock=lambda: CHECKED_AT,
        ).run()

        rendered = report.as_dict()
        self.assertEqual(rendered["reportFormat"], "animemo-doctor-report")
        self.assertEqual(rendered["reportVersion"], 1)
        self.assertEqual(rendered["mode"], "READ-ONLY")
        self.assertEqual(rendered["overallStatus"], "PASS")
        self.assertEqual(
            [item["checkId"] for item in rendered["checks"]], list(DOCTOR_CHECK_IDS)
        )
        self.assertEqual({item["status"] for item in rendered["checks"]}, {"PASS"})
        self.assertEqual(rendered["compatibility"]["overallStatus"], "COMPATIBLE")
        self.assertEqual(
            {call[0] for call in host.calls},
            {"lstat", "read_secure_bytes", "user_id", "group_id"},
        )
        self.assertEqual(network.call_count, 0)

    def test_missing_locator_does_not_scan_legacy_paths_or_call_dependent_probes(self):
        host = FakeReadOnlyHost(include_locator=False)
        called: list[str] = []
        network = NetworkSentinel()
        distribution_reader = FakeDistributionReader(
            distribution_snapshot(), network
        )
        probes = {
            check_id: (lambda _locator, check_id=check_id: called.append(check_id))
            for check_id in DOCTOR_CHECK_IDS
        }

        report = DoctorRunner(
            host=host,
            probes=probes,
            distribution_reader=distribution_reader,
            clock=lambda: CHECKED_AT,
        ).run()

        self.assertEqual(report.overall_status, DoctorStatus.FAIL)
        self.assertEqual(called, [])
        inspected = {
            path
            for operation, path in host.calls
            if operation in {"lstat", "read_secure_bytes"}
        }
        self.assertEqual(inspected, {str(INSTANCE_LOCATOR_PATH)})
        self.assertFalse(
            any("1panel" in path or "anime-journal" in path for path in inspected)
        )
        self.assertEqual(distribution_reader.calls, [])
        self.assertEqual(network.call_count, 0)

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
        self.assertEqual(
            by_id["database.postgresql.connectivity"].status, DoctorStatus.SKIPPED
        )
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
        host.payloads[INSTANCE_LOCATOR_PATH] = b'{"schemaVersion":1,"schemaVersion":1}'

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
