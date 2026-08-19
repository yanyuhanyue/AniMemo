from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path

from durability.compatibility import (
    ArtifactIdentity,
    CompatibilityOutcome,
    Dimension,
    DimensionAssessment,
    ReasonCode,
)
from installer.runtime import (
    ConfigPlanEvidence,
    InstallAction,
    Installer,
    InstallerAdapterError,
    InstallerError,
    InstallerMode,
    InstallOutcome,
    InstallPhase,
    InstallRequest,
    InstallTransportSource,
    ListenRequest,
    PlatformEvidence,
    ReleaseEvidence,
    ReleaseSelector,
    RestorePlanEvidence,
    RestoreProtectionKind,
    RestoreProtectionRequest,
    TargetClass,
    TargetEvidence,
)
from updater.local_bundle import LOCAL_BUNDLE_POLICY_IDENTITY
from updater.transport import ExplicitTransportPolicy


def digest(character: str) -> str:
    return "sha256:" + character * 64


INSTANCE_ID = "12345678-1234-4234-9234-123456789abc"
CONFIG_REVISION = "22345678-1234-4234-9234-123456789abc"


class ReleaseFake:
    def __init__(self) -> None:
        self.evidence = ReleaseEvidence(
            version="v1.1.0-rc.1",
            channel="rc",
            commit="a" * 40,
            manifest_digest=digest("1"),
            material_identity_digest=digest("2"),
            deployment_identity_digest=digest("3"),
            deployment_profile="v1.1-standard",
            platform_profile="v1.1-standard-linux-amd64",
        )
        self.calls: list[bool] = []

    def resolve(self, selector, *, refresh: bool):
        self.calls.append(refresh)
        return self.evidence


class BootstrapGateFake:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.fail = False

    def consume(self, *, version: str, release_commit: str) -> object:
        self.calls.append((version, release_commit))
        if self.fail:
            raise RuntimeError("bootstrap authority unavailable")
        return object()


class TargetFake:
    def __init__(self, evidence: TargetEvidence | None = None) -> None:
        self.evidence = evidence or TargetEvidence(TargetClass.ABSENT, digest("4"))
        self.calls = 0

    def inspect(self):
        self.calls += 1
        return self.evidence


class PlatformFake:
    def __init__(self) -> None:
        self.evidence = PlatformEvidence(
            compatible=True,
            profile="v1.1-standard-linux-amd64",
            evidence_digest=digest("5"),
            reason_code="PLATFORM_QUALIFIED",
        )
        self.calls = 0

    def assess(self, profile: str):
        self.calls += 1
        return self.evidence


class CompatibilityFake:
    def __init__(self) -> None:
        self.calls = 0

    def collect(self, release, platform):
        self.calls += 1
        reasons = {
            Dimension.FORMAT: ReasonCode.FORMAT_SUPPORTED,
            Dimension.INTEGRITY_AUTHENTICATION: ReasonCode.INTEGRITY_AUTHENTICATED,
            Dimension.DEPLOYMENT_CONTRACT: ReasonCode.DEPLOYMENT_CONTRACT_SUPPORTED,
            Dimension.SCHEMA_CONTRACTS: ReasonCode.SCHEMA_CONTRACTS_SUPPORTED,
            Dimension.EXACT_RELEASE_IDENTITY: ReasonCode.RELEASE_IDENTITY_VERIFIED,
            Dimension.PLATFORM_RUNTIME: ReasonCode.PLATFORM_RUNTIME_SUPPORTED,
            Dimension.SUPPORTED_PATH: ReasonCode.DIRECT_PATH_SUPPORTED,
        }
        dimensions = tuple(
            DimensionAssessment(
                name=dimension,
                outcome=CompatibilityOutcome.COMPATIBLE,
                reason_code=reasons[dimension],
                source={"identity": release.manifest_digest},
                target={"profile": platform.profile},
            )
            for dimension in Dimension
        )
        return (
            ArtifactIdentity(
                format_identity="animemo.release-materials",
                format_version=2,
                artifact_id=release.version,
                manifest_digest=release.manifest_digest,
            ),
            dimensions,
        )


class ConfigurationFake:
    def __init__(self) -> None:
        self.calls = 0
        self.revalidations = 0

    def plan(
        self,
        *,
        instance_id: str,
        public_origin: str,
        listen: ListenRequest,
        insecure_http_accepted: bool,
    ):
        self.calls += 1
        self.asserted_http = insecure_http_accepted
        exposure = "loopback" if listen.host.startswith("127.") else "direct"
        warnings = () if exposure == "loopback" else ("DIRECT_LISTEN_EXPOSURE",)
        return ConfigPlanEvidence(
            instance_id=instance_id,
            config_revision=CONFIG_REVISION,
            public_origin=public_origin,
            listen_host=listen.host,
            listen_port=listen.port,
            exposure=exposure,
            non_secret_identity_digest=digest("6"),
            warnings=warnings,
        )

    def revalidate(self, plan):
        self.revalidations += 1


class OperationFake:
    def __init__(self) -> None:
        self.events: list[tuple] = []

    def acquire_lock(self, operation_id: str):
        self.events.append(("lock", operation_id))
        return nullcontext()

    def begin(self, plan):
        self.events.append(("begin", plan.operation_id))

    def phase(
        self,
        phase,
        *,
        completed_step=None,
        mutation_occurred,
        irreversible_mutation_started,
    ):
        self.events.append(
            (
                "phase",
                phase.value,
                completed_step,
                mutation_occurred,
                irreversible_mutation_started,
            )
        )

    def fail(self, **kwargs):
        self.events.append(("fail", kwargs))

    def succeed(self, *, completed_steps):
        self.events.append(("succeed", completed_steps))


class FreshFake:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.failure: str | None = None

    def _call(self, name: str) -> None:
        self.calls.append(name)
        if self.failure == name:
            raise InstallerAdapterError(
                f"{name.upper()}_FAILED",
                mutation_occurred=True,
                recovery_required=False,
            )

    def prepare_roots(self, plan):
        self._call("roots")

    def publish_config(self, plan):
        self._call("config")

    def stage_release(self, plan):
        self._call("release")

    def prepare_services(self, plan):
        self._call("services")

    def migrate_database(self, plan):
        self._call("migration")

    def bootstrap(self, plan):
        self._call("bootstrap")

    def start_runtime(self, plan):
        self._call("runtime")

    def validate_running_release(self, plan):
        self._call("validate")

    def adopt_updater(self, plan):
        self._call("adopt")

    def doctor_acceptance(self, plan):
        self._call("doctor")

    def cleanup_owned_staging(self, plan):
        self.calls.append("cleanup")


class RestoreFake:
    def __init__(self) -> None:
        self.prepared = 0
        self.revalidated = 0
        self.executed = 0
        self.bound = 0

    def prepare(
        self,
        *,
        operation_id,
        backup_root,
        release,
        target,
        platform,
        protection,
    ):
        del backup_root, release, target, platform, protection
        self.prepared += 1
        return RestorePlanEvidence(
            operation_id=operation_id,
            instance_id=INSTANCE_ID,
            restore_plan_digest=digest("7"),
            backup_identity_digest=digest("8"),
        )

    def revalidate(self, plan):
        self.revalidated += 1

    def bind_configuration(self, plan, configuration):
        del plan
        self.bound += 1
        return configuration

    def execute(self, plan, *, accepted_plan_digest, installation_plan):
        del installation_plan
        self.executed += 1
        if accepted_plan_digest != plan.restore_plan_digest:
            raise AssertionError("wrong Restore plan acceptance")
        return ("restore.runtime.execute", "doctor.accept")


class InstallerRuntimeTests(unittest.TestCase):
    def test_exact_release_selector_accepts_only_stable_or_rc(self) -> None:
        self.assertEqual(ReleaseSelector(version="v1.1.0").version, "v1.1.0")
        self.assertEqual(
            ReleaseSelector(version="v1.1.0-rc.2").version,
            "v1.1.0-rc.2",
        )
        for version in (
            "v1.1.0-beta.1",
            "v1.1",
            "latest",
            "v01.1.0",
            "v1.1.0-rc.0",
        ):
            with (
                self.subTest(version=version),
                self.assertRaisesRegex(
                    InstallerError, "INSTALL_RELEASE_VERSION_INVALID"
                ),
            ):
                ReleaseSelector(version=version)

    def test_local_bundle_requires_exact_version_payload_and_release_attestation(
        self,
    ) -> None:
        payload = Path("C:/offline/payload.tar")
        sidecar = Path("C:/offline/release-attestation.sigstore.json")
        request = InstallRequest(
            mode=InstallerMode.FRESH,
            selector=ReleaseSelector(version="v1.1.0"),
            public_origin="https://anime.example",
            transport_source=InstallTransportSource.LOCAL_BUNDLE,
            local_bundle_payload=payload,
            local_bundle_release_attestation=sidecar,
        )

        self.assertEqual(request.local_bundle_payload, payload)
        self.assertEqual(request.local_bundle_release_attestation, sidecar)
        self.assertEqual(
            request.transport_policy_identity,
            LOCAL_BUNDLE_POLICY_IDENTITY,
        )
        for selector, candidate_payload, candidate_sidecar in (
            (ReleaseSelector(channel="rc"), payload, sidecar),
            (ReleaseSelector(version="v1.1.0"), None, sidecar),
            (ReleaseSelector(version="v1.1.0"), payload, None),
        ):
            with (
                self.subTest(selector=selector),
                self.assertRaisesRegex(
                    InstallerError,
                    "INSTALL_LOCAL_BUNDLE_INPUT_REQUIRED",
                ),
            ):
                InstallRequest(
                    mode=InstallerMode.FRESH,
                    selector=selector,
                    public_origin="https://anime.example",
                    transport_source=InstallTransportSource.LOCAL_BUNDLE,
                    local_bundle_payload=candidate_payload,
                    local_bundle_release_attestation=candidate_sidecar,
                )

        with self.assertRaisesRegex(
            InstallerError,
            "INSTALL_LOCAL_BUNDLE_INPUT_FORBIDDEN",
        ):
            InstallRequest(
                mode=InstallerMode.FRESH,
                selector=ReleaseSelector(version="v1.1.0"),
                public_origin="https://anime.example",
                local_bundle_payload=payload,
                local_bundle_release_attestation=sidecar,
            )

    def setUp(self) -> None:
        self.releases = ReleaseFake()
        self.target = TargetFake()
        self.platform = PlatformFake()
        self.compatibility = CompatibilityFake()
        self.configuration = ConfigurationFake()
        self.operations = OperationFake()
        self.fresh = FreshFake()
        self.restore = RestoreFake()
        self.bootstrap_gate = BootstrapGateFake()
        self.runtime = Installer(
            releases=self.releases,
            target=self.target,
            platform=self.platform,
            compatibility=self.compatibility,
            configuration=self.configuration,
            operations=self.operations,
            fresh=self.fresh,
            restore=self.restore,
            bootstrap_privilege_gate=self.bootstrap_gate,
        )

    def request(self, **changes) -> InstallRequest:
        values = {
            "mode": InstallerMode.FRESH,
            "selector": ReleaseSelector(channel="rc"),
            "public_origin": "https://anime.example",
        }
        values.update(changes)
        return InstallRequest(**values)

    def test_fresh_plan_is_read_only_and_secret_free(self) -> None:
        plan = self.runtime.plan(self.request())

        self.assertEqual(plan.action, InstallAction.INSTALL_FRESH)
        self.assertEqual(self.fresh.calls, [])
        self.assertEqual(self.operations.events, [])
        rendered = json.dumps(plan.as_dict(), sort_keys=True)
        self.assertNotIn("example-private-value", rendered)
        self.assertNotIn('"secrets"', rendered.casefold())
        self.assertNotIn('"credentials"', rendered.casefold())
        self.assertNotIn('"password"', rendered.casefold())

    def test_bootstrap_privilege_gate_blocks_before_first_mutation(self) -> None:
        plan = self.runtime.plan(self.request())
        self.bootstrap_gate.fail = True

        with self.assertRaisesRegex(
            InstallerError,
            "INSTALL_BOOTSTRAP_AUTHORITY_REQUIRED",
        ):
            self.runtime.execute(plan, accepted_plan_digest=plan.plan_digest)

        self.assertEqual(self.fresh.calls, [])
        self.assertEqual(self.operations.events, [])

    def test_plan_binds_one_explicit_transport_policy_through_execution(self) -> None:
        policy = ExplicitTransportPolicy.official_mirror()
        self.releases.evidence = replace(
            self.releases.evidence,
            transport_source=InstallTransportSource.OFFICIAL_MIRROR,
            transport_policy_identity=policy.identity,
        )
        plan = self.runtime.plan(
            self.request(
                transport_source=InstallTransportSource.OFFICIAL_MIRROR,
            )
        )

        self.assertIs(
            plan.transport_source,
            InstallTransportSource.OFFICIAL_MIRROR,
        )
        self.assertEqual(plan.transport_policy_identity, policy.identity)
        self.assertEqual(plan.body()["transportSource"], "official-mirror")
        self.assertEqual(plan.body()["transportPolicyIdentity"], policy.identity)

        self.runtime.execute(plan, accepted_plan_digest=plan.plan_digest)
        self.assertEqual(self.releases.calls, [False, True, True])

    def test_release_transport_policy_mismatch_stops_before_target_inspection(
        self,
    ) -> None:
        policy = ExplicitTransportPolicy.official_mirror()
        self.releases.evidence = replace(
            self.releases.evidence,
            transport_source=InstallTransportSource.OFFICIAL_MIRROR,
            transport_policy_identity=policy.identity,
        )

        with self.assertRaisesRegex(
            InstallerError,
            "INSTALL_RELEASE_TRANSPORT_POLICY_MISMATCH",
        ):
            self.runtime.plan(self.request())

        self.assertEqual(self.target.calls, 0)
        self.assertEqual(self.operations.events, [])
        self.assertEqual(self.fresh.calls, [])

    def test_fresh_execute_reverifies_and_adoption_publishes_before_doctor(
        self,
    ) -> None:
        plan = self.runtime.plan(self.request())
        result = self.runtime.execute(plan, accepted_plan_digest=plan.plan_digest)

        self.assertEqual(result.outcome, InstallOutcome.SUCCEEDED)
        self.assertEqual(
            self.fresh.calls,
            [
                "roots",
                "config",
                "release",
                "services",
                "migration",
                "bootstrap",
                "runtime",
                "validate",
                "adopt",
                "doctor",
            ],
        )
        self.assertLess(
            self.fresh.calls.index("adopt"), self.fresh.calls.index("doctor")
        )
        migration_markers = [
            event
            for event in self.operations.events
            if event[:2] == ("phase", InstallPhase.DATABASE_MIGRATING.value)
        ]
        self.assertTrue(
            migration_markers[0][4], "irreversible flag must be durable first"
        )
        self.assertEqual(self.releases.calls, [False, True, True])

    def test_wrong_plan_digest_causes_zero_mutation(self) -> None:
        plan = self.runtime.plan(self.request())

        with self.assertRaisesRegex(InstallerError, "INSTALL_PLAN_NOT_ACCEPTED"):
            self.runtime.execute(plan, accepted_plan_digest=digest("f"))

        self.assertEqual(self.operations.events, [])
        self.assertEqual(self.fresh.calls, [])

    def test_release_change_causes_zero_mutation(self) -> None:
        plan = self.runtime.plan(self.request())
        self.releases.evidence = replace(
            self.releases.evidence,
            manifest_digest=digest("9"),
        )

        with self.assertRaisesRegex(InstallerError, "INSTALL_RELEASE_CHANGED"):
            self.runtime.execute(plan, accepted_plan_digest=plan.plan_digest)

        self.assertEqual(self.operations.events, [])
        self.assertEqual(self.fresh.calls, [])

    def test_same_exact_healthy_instance_is_no_change(self) -> None:
        release = self.releases.evidence
        self.target.evidence = TargetEvidence(
            TargetClass.ACTIVE,
            digest("4"),
            instance_id=INSTANCE_ID,
            release_manifest_digest=release.manifest_digest,
            material_identity_digest=release.material_identity_digest,
            config_revision=CONFIG_REVISION,
            public_origin="https://anime.example",
            listen_host="127.0.0.1",
            listen_port=8088,
            exact_release_running=True,
            doctor_complete=True,
        )
        plan = self.runtime.plan(self.request())

        result = self.runtime.execute(plan, accepted_plan_digest=plan.plan_digest)

        self.assertEqual(plan.action, InstallAction.NO_CHANGE)
        self.assertEqual(result.outcome, InstallOutcome.NO_CHANGE)
        self.assertEqual(self.operations.events, [])

    def test_different_release_is_updater_handoff(self) -> None:
        self.target.evidence = TargetEvidence(
            TargetClass.ACTIVE,
            digest("4"),
            instance_id=INSTANCE_ID,
            release_manifest_digest=digest("a"),
            material_identity_digest=digest("b"),
            config_revision=CONFIG_REVISION,
            public_origin="https://anime.example",
            listen_host="127.0.0.1",
            listen_port=8088,
            exact_release_running=True,
            doctor_complete=True,
        )
        plan = self.runtime.plan(self.request())

        result = self.runtime.execute(plan, accepted_plan_digest=plan.plan_digest)

        self.assertEqual(plan.action, InstallAction.UPDATER_HANDOFF)
        self.assertEqual(result.outcome, InstallOutcome.UPDATER_HANDOFF)
        self.assertEqual(self.operations.events, [])

    def test_partial_and_foreign_targets_fail_closed(self) -> None:
        for classification, code in (
            (TargetClass.PARTIAL_AMBIGUOUS, "INSTALL_PARTIAL_TARGET"),
            (TargetClass.FOREIGN, "INSTALL_FOREIGN_TARGET"),
            (TargetClass.CORRUPT, "INSTALL_CORRUPT_TARGET"),
        ):
            with self.subTest(classification=classification):
                self.target.evidence = TargetEvidence(classification, digest("4"))
                with self.assertRaisesRegex(InstallerError, code):
                    self.runtime.plan(self.request())
                self.assertEqual(self.operations.events, [])
                self.assertEqual(self.fresh.calls, [])

    def test_restore_to_new_preserves_identity_and_delegates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            plan = self.runtime.plan(
                self.request(
                    mode=InstallerMode.RESTORE_TO_NEW,
                    backup_root=Path(directory),
                    restore_protection=RestoreProtectionRequest(
                        RestoreProtectionKind.NONE
                    ),
                )
            )
        result = self.runtime.execute(plan, accepted_plan_digest=plan.plan_digest)

        self.assertEqual(plan.action, InstallAction.RESTORE_TO_NEW)
        self.assertEqual(plan.configuration.instance_id, INSTANCE_ID)
        self.assertEqual(result.instance_id, INSTANCE_ID)
        self.assertEqual(self.restore.prepared, 1)
        self.assertEqual(self.restore.bound, 1)
        self.assertEqual(self.restore.executed, 1)
        self.assertEqual(self.fresh.calls, [])

    def test_failure_after_irreversible_marker_requires_recovery(self) -> None:
        self.fresh.failure = "migration"
        plan = self.runtime.plan(self.request())

        with self.assertRaises(InstallerAdapterError) as caught:
            self.runtime.execute(plan, accepted_plan_digest=plan.plan_digest)

        self.assertTrue(caught.exception.recovery_required)
        failure = next(event for event in self.operations.events if event[0] == "fail")
        self.assertTrue(failure[1]["irreversible_mutation_started"])
        self.assertTrue(failure[1]["recovery_required"])
        self.assertNotIn("cleanup", self.fresh.calls)

    def test_failure_before_irreversible_marker_uses_scoped_cleanup(self) -> None:
        self.fresh.failure = "config"
        plan = self.runtime.plan(self.request())

        with self.assertRaises(InstallerAdapterError) as caught:
            self.runtime.execute(plan, accepted_plan_digest=plan.plan_digest)

        self.assertFalse(caught.exception.recovery_required)
        self.assertEqual(self.fresh.calls[-1], "cleanup")

    def test_direct_listen_warning_remains_in_plan_and_result(self) -> None:
        plan = self.runtime.plan(
            self.request(
                listen=ListenRequest(
                    host="0.0.0.0",
                    port=8088,
                    direct_exposure_accepted=True,
                )
            )
        )
        result = self.runtime.execute(plan, accepted_plan_digest=plan.plan_digest)

        self.assertEqual(plan.warnings, ("DIRECT_LISTEN_EXPOSURE",))
        self.assertEqual(result.warnings, ("DIRECT_LISTEN_EXPOSURE",))


if __name__ == "__main__":
    unittest.main()
