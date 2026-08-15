from __future__ import annotations

import unittest

from durability.compatibility import (
    EVALUATION_ORDER,
    MATRIX_FORMAT_IDENTITY,
    MATRIX_FORMAT_VERSION,
    MATRIX_IDENTITY,
    ArtifactIdentity,
    CompatibilityDecision,
    CompatibilityEvaluationError,
    CompatibilityOperation,
    CompatibilityOutcome,
    Dimension,
    DimensionAssessment,
    ReasonCode,
    UpgradeAction,
    evaluate_compatibility,
)

DIGEST = "sha256:" + "a" * 64

COMPATIBLE_REASON = {
    Dimension.FORMAT: ReasonCode.FORMAT_SUPPORTED,
    Dimension.INTEGRITY_AUTHENTICATION: ReasonCode.INTEGRITY_AUTHENTICATED,
    Dimension.DEPLOYMENT_CONTRACT: ReasonCode.DEPLOYMENT_CONTRACT_SUPPORTED,
    Dimension.SCHEMA_CONTRACTS: ReasonCode.SCHEMA_CONTRACTS_SUPPORTED,
    Dimension.EXACT_RELEASE_IDENTITY: ReasonCode.RELEASE_IDENTITY_VERIFIED,
    Dimension.PLATFORM_RUNTIME: ReasonCode.PLATFORM_RUNTIME_SUPPORTED,
    Dimension.SUPPORTED_PATH: ReasonCode.DIRECT_PATH_SUPPORTED,
}


def artifact(*, format_version: int = 1) -> ArtifactIdentity:
    return ArtifactIdentity(
        format_identity="animemo-instance-backup",
        format_version=format_version,
        artifact_id="backup-01",
        manifest_digest=DIGEST,
    )


def compatible_dimensions() -> list[DimensionAssessment]:
    return [
        DimensionAssessment(
            name=name,
            outcome=CompatibilityOutcome.COMPATIBLE,
            reason_code=COMPATIBLE_REASON[name],
            source={"identity": f"source-{name.value}"},
            target={"capability": f"target-{name.value}"},
        )
        for name in EVALUATION_ORDER
    ]


def replace_dimension(
    dimensions: list[DimensionAssessment],
    name: Dimension,
    outcome: CompatibilityOutcome,
    reason_code: ReasonCode,
) -> list[DimensionAssessment]:
    result = list(dimensions)
    index = EVALUATION_ORDER.index(name)
    current = result[index]
    result[index] = DimensionAssessment(
        name=name,
        outcome=outcome,
        reason_code=reason_code,
        source=current.source,
        target=current.target,
    )
    return result


def upgrade_action(order: int = 1) -> UpgradeAction:
    return UpgradeAction(
        order=order,
        kind="APPLY_SCHEMA_MIGRATION",
        input_identity={"databaseContract": "animemo-db-v1"},
        output_identity={"databaseContract": "animemo-db-v2"},
        required_release_identity={"manifestDigest": DIGEST},
    )


class CompatibilityEngineInvariantTests(unittest.TestCase):
    def test_public_identity_statuses_operations_and_order_are_frozen(self):
        self.assertEqual(MATRIX_IDENTITY, "animemo.compatibility/v1")
        self.assertEqual(MATRIX_FORMAT_IDENTITY, "animemo.compatibility")
        self.assertEqual(MATRIX_FORMAT_VERSION, 1)
        self.assertEqual(
            tuple(outcome.value for outcome in CompatibilityOutcome),
            ("COMPATIBLE", "REQUIRES_UPGRADE", "UNSUPPORTED", "CORRUPT"),
        )
        self.assertEqual(
            tuple(operation.value for operation in CompatibilityOperation),
            ("install", "update", "backup", "restore", "migration", "doctor"),
        )
        self.assertEqual(
            EVALUATION_ORDER,
            (
                Dimension.FORMAT,
                Dimension.INTEGRITY_AUTHENTICATION,
                Dimension.DEPLOYMENT_CONTRACT,
                Dimension.SCHEMA_CONTRACTS,
                Dimension.EXACT_RELEASE_IDENTITY,
                Dimension.PLATFORM_RUNTIME,
                Dimension.SUPPORTED_PATH,
            ),
        )
        forbidden = {"UNKNOWN", "MAYBE", "PARTIAL", "LEGACY"}
        self.assertTrue(forbidden.isdisjoint(outcome.value for outcome in CompatibilityOutcome))

    def test_all_compatible_dimensions_produce_canonical_machine_result(self):
        decision = evaluate_compatibility(
            operation=CompatibilityOperation.RESTORE,
            artifact=artifact(),
            dimensions=compatible_dimensions(),
        )

        self.assertIsInstance(decision, CompatibilityDecision)
        self.assertEqual(decision.outcome, CompatibilityOutcome.COMPATIBLE)
        self.assertEqual(decision.reason_code, ReasonCode.ALL_DIMENSIONS_COMPATIBLE)
        self.assertIsNone(decision.blocking_dimension)
        self.assertEqual(decision.actions, ())
        self.assertEqual(decision.evaluated_dimensions, tuple(decision.evaluated_dimensions))

        result = decision.as_dict()
        self.assertEqual(result["matrixVersion"], MATRIX_IDENTITY)
        self.assertEqual(result["formatIdentity"], MATRIX_FORMAT_IDENTITY)
        self.assertEqual(result["formatVersion"], MATRIX_FORMAT_VERSION)
        self.assertEqual(result["operation"], "restore")
        self.assertEqual(result["overallStatus"], "COMPATIBLE")
        self.assertEqual(result["reasonCode"], "ALL_DIMENSIONS_COMPATIBLE")
        self.assertEqual(result["summary"], "All required compatibility dimensions are compatible.")
        self.assertIsNone(result["blockingDimension"])
        self.assertEqual(result["artifact"], result["evaluatedArtifactIdentity"])
        self.assertEqual([item["name"] for item in result["dimensions"]], [item.value for item in EVALUATION_ORDER])
        self.assertEqual(result["actions"], [])

    def test_result_is_deterministic_and_detached_from_mutable_inputs(self):
        dimensions = compatible_dimensions()
        source = {"z": "last", "a": {"second": 2, "first": 1}}
        dimensions[0] = DimensionAssessment(
            name=Dimension.FORMAT,
            outcome=CompatibilityOutcome.COMPATIBLE,
            reason_code=ReasonCode.FORMAT_SUPPORTED,
            source=source,
            target={"supportedVersions": [2, 1]},
        )
        first = evaluate_compatibility("backup", artifact(), dimensions)
        before = first.canonical_bytes()

        source["z"] = "mutated"
        second_source = {"a": {"first": 1, "second": 2}, "z": "last"}
        dimensions[0] = DimensionAssessment(
            name=Dimension.FORMAT,
            outcome=CompatibilityOutcome.COMPATIBLE,
            reason_code=ReasonCode.FORMAT_SUPPORTED,
            source=second_source,
            target={"supportedVersions": [2, 1]},
        )
        second = evaluate_compatibility("backup", artifact(), dimensions)

        self.assertEqual(first.canonical_bytes(), before)
        self.assertEqual(first.digest(), second.digest())

    def test_artifact_format_version_is_independent_from_matrix_version(self):
        dimensions = replace_dimension(
            compatible_dimensions(),
            Dimension.FORMAT,
            CompatibilityOutcome.UNSUPPORTED,
            ReasonCode.FORMAT_VERSION_UNSUPPORTED,
        )

        result = evaluate_compatibility("restore", artifact(format_version=17), dimensions).as_dict()

        self.assertEqual(result["matrixVersion"], "animemo.compatibility/v1")
        self.assertEqual(result["artifact"]["format"], "animemo-instance-backup")
        self.assertEqual(result["artifact"]["schemaVersion"], 17)
        self.assertEqual(result["overallStatus"], "UNSUPPORTED")

    def test_aggregate_precedence_is_corrupt_then_unsupported_then_upgrade(self):
        dimensions = compatible_dimensions()
        dimensions = replace_dimension(
            dimensions,
            Dimension.SCHEMA_CONTRACTS,
            CompatibilityOutcome.REQUIRES_UPGRADE,
            ReasonCode.SCHEMA_MIGRATION_REQUIRED,
        )
        dimensions = replace_dimension(
            dimensions,
            Dimension.SUPPORTED_PATH,
            CompatibilityOutcome.UNSUPPORTED,
            ReasonCode.SUPPORTED_PATH_UNAVAILABLE,
        )
        dimensions = replace_dimension(
            dimensions,
            Dimension.INTEGRITY_AUTHENTICATION,
            CompatibilityOutcome.CORRUPT,
            ReasonCode.CHECKSUM_MISMATCH,
        )

        decision = evaluate_compatibility("restore", artifact(), dimensions)

        self.assertEqual(decision.outcome, CompatibilityOutcome.CORRUPT)
        self.assertEqual(decision.reason_code, ReasonCode.CHECKSUM_MISMATCH)
        self.assertEqual(decision.blocking_dimension, Dimension.INTEGRITY_AUTHENTICATION)

    def test_unsupported_precedence_suppresses_upgrade_plan(self):
        dimensions = compatible_dimensions()
        dimensions = replace_dimension(
            dimensions,
            Dimension.SCHEMA_CONTRACTS,
            CompatibilityOutcome.REQUIRES_UPGRADE,
            ReasonCode.SCHEMA_MIGRATION_REQUIRED,
        )
        dimensions = replace_dimension(
            dimensions,
            Dimension.SUPPORTED_PATH,
            CompatibilityOutcome.UNSUPPORTED,
            ReasonCode.SUPPORTED_PATH_UNAVAILABLE,
        )

        decision = evaluate_compatibility("restore", artifact(), dimensions)

        self.assertEqual(decision.outcome, CompatibilityOutcome.UNSUPPORTED)
        self.assertEqual(decision.actions, ())
        self.assertEqual(decision.blocking_dimension, Dimension.SUPPORTED_PATH)

    def test_first_dimension_wins_when_highest_precedence_is_tied(self):
        dimensions = compatible_dimensions()
        dimensions = replace_dimension(
            dimensions,
            Dimension.DEPLOYMENT_CONTRACT,
            CompatibilityOutcome.UNSUPPORTED,
            ReasonCode.DEPLOYMENT_CONTRACT_UNSUPPORTED,
        )
        dimensions = replace_dimension(
            dimensions,
            Dimension.EXACT_RELEASE_IDENTITY,
            CompatibilityOutcome.UNSUPPORTED,
            ReasonCode.RELEASE_IDENTITY_UNSUPPORTED,
        )

        decision = evaluate_compatibility("migration", artifact(), dimensions)

        self.assertEqual(decision.outcome, CompatibilityOutcome.UNSUPPORTED)
        self.assertEqual(decision.blocking_dimension, Dimension.DEPLOYMENT_CONTRACT)
        self.assertEqual(decision.reason_code, ReasonCode.DEPLOYMENT_CONTRACT_UNSUPPORTED)

    def test_upgrade_requires_exact_contiguous_actions_and_supported_path(self):
        dimensions = compatible_dimensions()
        dimensions = replace_dimension(
            dimensions,
            Dimension.SCHEMA_CONTRACTS,
            CompatibilityOutcome.REQUIRES_UPGRADE,
            ReasonCode.SCHEMA_MIGRATION_REQUIRED,
        )
        dimensions = replace_dimension(
            dimensions,
            Dimension.SUPPORTED_PATH,
            CompatibilityOutcome.REQUIRES_UPGRADE,
            ReasonCode.ORDERED_PATH_REQUIRED,
        )

        decision = evaluate_compatibility(
            "restore",
            artifact(),
            dimensions,
            actions=[upgrade_action()],
        )

        self.assertEqual(decision.outcome, CompatibilityOutcome.REQUIRES_UPGRADE)
        self.assertEqual(decision.blocking_dimension, Dimension.SCHEMA_CONTRACTS)
        self.assertEqual(decision.reason_code, ReasonCode.SCHEMA_MIGRATION_REQUIRED)
        self.assertEqual(decision.as_dict()["actions"][0]["order"], 1)

    def test_non_upgrade_decisions_cannot_carry_executable_actions(self):
        with self.assertRaisesRegex(CompatibilityEvaluationError, "ACTIONS_FORBIDDEN"):
            evaluate_compatibility(
                "restore",
                artifact(),
                compatible_dimensions(),
                actions=[upgrade_action()],
            )

    def test_upgrade_without_actions_is_an_evaluation_error_not_a_fifth_status(self):
        dimensions = compatible_dimensions()
        dimensions = replace_dimension(
            dimensions,
            Dimension.SCHEMA_CONTRACTS,
            CompatibilityOutcome.REQUIRES_UPGRADE,
            ReasonCode.SCHEMA_MIGRATION_REQUIRED,
        )
        dimensions = replace_dimension(
            dimensions,
            Dimension.SUPPORTED_PATH,
            CompatibilityOutcome.REQUIRES_UPGRADE,
            ReasonCode.ORDERED_PATH_REQUIRED,
        )

        with self.assertRaisesRegex(CompatibilityEvaluationError, "UPGRADE_ACTIONS_REQUIRED") as raised:
            evaluate_compatibility("restore", artifact(), dimensions)

        self.assertEqual(raised.exception.code, "UPGRADE_ACTIONS_REQUIRED")
        self.assertNotIn("UNKNOWN", str(raised.exception))
        self.assertEqual(
            raised.exception.as_dict(),
            {
                "matrixVersion": "animemo.compatibility/v1",
                "error": {"code": "UPGRADE_ACTIONS_REQUIRED"},
            },
        )
        self.assertNotIn("overallStatus", raised.exception.as_dict())

    def test_upgrade_action_order_must_be_contiguous_and_start_at_one(self):
        dimensions = compatible_dimensions()
        dimensions = replace_dimension(
            dimensions,
            Dimension.SUPPORTED_PATH,
            CompatibilityOutcome.REQUIRES_UPGRADE,
            ReasonCode.ORDERED_PATH_REQUIRED,
        )

        with self.assertRaisesRegex(CompatibilityEvaluationError, "ACTION_ORDER_INVALID"):
            evaluate_compatibility(
                "restore",
                artifact(),
                dimensions,
                actions=[upgrade_action(order=2)],
            )

    def test_upgrade_outcome_requires_supported_path_dimension(self):
        dimensions = replace_dimension(
            compatible_dimensions(),
            Dimension.SCHEMA_CONTRACTS,
            CompatibilityOutcome.REQUIRES_UPGRADE,
            ReasonCode.SCHEMA_MIGRATION_REQUIRED,
        )

        with self.assertRaisesRegex(CompatibilityEvaluationError, "SUPPORTED_PATH_REQUIRED"):
            evaluate_compatibility(
                "restore",
                artifact(),
                dimensions,
                actions=[upgrade_action()],
            )


class CompatibilityEngineValidationTests(unittest.TestCase):
    def test_missing_duplicate_or_out_of_order_dimensions_fail_closed(self):
        valid = compatible_dimensions()
        invalid_sets = (
            valid[:-1],
            valid[:-1] + [valid[0]],
            [valid[1], valid[0], *valid[2:]],
        )

        for dimensions in invalid_sets:
            with self.subTest(
                names=[item.name.value for item in dimensions]
            ), self.assertRaises(CompatibilityEvaluationError):
                evaluate_compatibility("doctor", artifact(), dimensions)

    def test_unknown_operation_or_outcome_is_rejected_without_a_decision(self):
        with self.assertRaisesRegex(CompatibilityEvaluationError, "OPERATION_INVALID"):
            evaluate_compatibility("repair", artifact(), compatible_dimensions())

        dimensions = compatible_dimensions()
        dimensions[0] = DimensionAssessment(
            name=Dimension.FORMAT,
            outcome="UNKNOWN",  # type: ignore[arg-type]
            reason_code=ReasonCode.FORMAT_SUPPORTED,
            source={},
            target={},
        )
        with self.assertRaisesRegex(CompatibilityEvaluationError, "OUTCOME_INVALID"):
            evaluate_compatibility("doctor", artifact(), dimensions)

    def test_reason_code_must_match_both_dimension_and_outcome(self):
        wrong_dimension = compatible_dimensions()
        wrong_dimension[0] = DimensionAssessment(
            name=Dimension.FORMAT,
            outcome=CompatibilityOutcome.COMPATIBLE,
            reason_code=ReasonCode.RELEASE_IDENTITY_VERIFIED,
            source={},
            target={},
        )
        wrong_outcome = compatible_dimensions()
        wrong_outcome[0] = DimensionAssessment(
            name=Dimension.FORMAT,
            outcome=CompatibilityOutcome.UNSUPPORTED,
            reason_code=ReasonCode.FORMAT_SUPPORTED,
            source={},
            target={},
        )

        for dimensions in (wrong_dimension, wrong_outcome):
            with self.assertRaisesRegex(CompatibilityEvaluationError, "REASON_CODE_INVALID"):
                evaluate_compatibility("doctor", artifact(), dimensions)

    def test_envelope_contract_reason_codes_map_to_canonical_dimensions(self):
        dimensions = replace_dimension(
            compatible_dimensions(),
            Dimension.INTEGRITY_AUTHENTICATION,
            CompatibilityOutcome.CORRUPT,
            ReasonCode.ENVELOPE_AUTHENTICATION_FAILED,
        )

        decision = evaluate_compatibility("migration", artifact(), dimensions)

        self.assertEqual(decision.outcome, CompatibilityOutcome.CORRUPT)
        self.assertEqual(decision.reason_code, ReasonCode.ENVELOPE_AUTHENTICATION_FAILED)
        self.assertEqual(decision.blocking_dimension, Dimension.INTEGRITY_AUTHENTICATION)

    def test_invalid_artifact_identity_fails_closed(self):
        bad_artifacts = (
            ArtifactIdentity("Prototype Backup", 1, "backup-01", DIGEST),
            ArtifactIdentity("animemo-instance-backup", 0, "backup-01", DIGEST),
            ArtifactIdentity("animemo-instance-backup", 1, "", DIGEST),
            ArtifactIdentity("animemo-instance-backup", 1, "backup-01", "not-a-digest"),
        )

        for invalid in bad_artifacts:
            with self.subTest(invalid=invalid), self.assertRaisesRegex(
                CompatibilityEvaluationError, "ARTIFACT_IDENTITY_INVALID"
            ):
                evaluate_compatibility("backup", invalid, compatible_dimensions())

    def test_non_json_or_ambiguous_identity_values_fail_closed(self):
        for source in (
            {"identity": object()},
            {1: "not-a-string-key"},
            {"identity": float("nan")},
            {f"field-{index}": index for index in range(257)},
        ):
            dimensions = compatible_dimensions()
            dimensions[0] = DimensionAssessment(
                name=Dimension.FORMAT,
                outcome=CompatibilityOutcome.COMPATIBLE,
                reason_code=ReasonCode.FORMAT_SUPPORTED,
                source=source,  # type: ignore[arg-type]
                target={},
            )

            with self.subTest(source_type=type(next(iter(source)))), self.assertRaisesRegex(
                CompatibilityEvaluationError, "IDENTITY_EVIDENCE_INVALID"
            ):
                evaluate_compatibility("backup", artifact(), dimensions)

    def test_sensitive_keys_raw_environment_and_secret_like_values_are_rejected(self):
        unsafe_sources = (
            {"apiToken": "must-not-appear"},
            {"environment": {"DATABASE_URL": "must-not-appear"}},
            {"DATABASE_URL": "must-not-appear"},
            {"identity": "Bearer must-not-appear"},
            {"identity": "postgresql://user:must-not-appear@db/animemo"},
            {"identity": "-----BEGIN PRIVATE KEY-----must-not-appear"},
        )

        for unsafe in unsafe_sources:
            dimensions = compatible_dimensions()
            dimensions[0] = DimensionAssessment(
                name=Dimension.FORMAT,
                outcome=CompatibilityOutcome.COMPATIBLE,
                reason_code=ReasonCode.FORMAT_SUPPORTED,
                source=unsafe,
                target={},
            )
            with self.subTest(unsafe=list(unsafe)):
                with self.assertRaisesRegex(CompatibilityEvaluationError, "SENSITIVE_EVIDENCE_FORBIDDEN") as raised:
                    evaluate_compatibility("doctor", artifact(), dimensions)
                self.assertNotIn("must-not-appear", str(raised.exception))

    def test_non_secret_envelope_and_credential_status_metadata_is_allowed(self):
        dimensions = compatible_dimensions()
        dimensions[1] = DimensionAssessment(
            name=Dimension.INTEGRITY_AUTHENTICATION,
            outcome=CompatibilityOutcome.COMPATIBLE,
            reason_code=ReasonCode.INTEGRITY_AUTHENTICATED,
            source={
                "secretEnvelopeFormat": "animemo.migration-secret-envelope/v1",
                "secretSuite": "reviewed-suite-v1",
                "credentialStatus": "EXISTS",
            },
            target={"externalSecretAvailability": "AVAILABLE"},
        )

        result = evaluate_compatibility("migration", artifact(), dimensions).as_dict()

        self.assertEqual(result["overallStatus"], "COMPATIBLE")
        self.assertEqual(
            result["dimensions"][1]["source"]["secretEnvelopeFormat"],
            "animemo.migration-secret-envelope/v1",
        )

    def test_sensitive_upgrade_action_identity_is_rejected_and_never_echoed(self):
        dimensions = replace_dimension(
            compatible_dimensions(),
            Dimension.SUPPORTED_PATH,
            CompatibilityOutcome.REQUIRES_UPGRADE,
            ReasonCode.ORDERED_PATH_REQUIRED,
        )
        unsafe_action = UpgradeAction(
            order=1,
            kind="APPLY_SCHEMA_MIGRATION",
            input_identity={"credential": "must-not-appear"},
            output_identity={"databaseContract": "animemo-db-v2"},
            required_release_identity={"manifestDigest": DIGEST},
        )

        with self.assertRaisesRegex(CompatibilityEvaluationError, "SENSITIVE_EVIDENCE_FORBIDDEN") as raised:
            evaluate_compatibility("restore", artifact(), dimensions, actions=[unsafe_action])

        self.assertNotIn("must-not-appear", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
