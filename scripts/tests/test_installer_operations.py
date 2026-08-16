from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from installer.operations import (
    PHASE_ORDER,
    FreshInstallOperationError,
    FreshInstallOperationJournal,
    FreshInstallPhase,
    FreshInstallRecoveryRequired,
    FreshInstallStatus,
    create_fresh_install_operation,
    fail_fresh_install,
    mark_irreversible_mutation_started,
    mark_mutation_started,
    parse_fresh_install_operation,
    parse_fresh_install_operation_bytes,
    reconcile_fresh_install,
    succeed_fresh_install,
    transition_phase,
)

DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64
DIGEST_C = "sha256:" + "c" * 64
INSTANCE_ID = "12345678-1234-4678-9234-567812345678"
REVISION = "87654321-4321-4678-9234-567812345678"
OPERATION_ID = "a" * 32


def operation():
    return create_fresh_install_operation(
        operation_id=OPERATION_ID,
        instance_id=INSTANCE_ID,
        plan_digest=DIGEST_A,
        release_identity_digest=DIGEST_B,
        deployment_identity_digest=DIGEST_C,
        config_revision=REVISION,
        at="2026-08-16T00:00:00Z",
    )


def next_time(index: int) -> str:
    return f"2026-08-16T00:00:{index:02d}Z"


class FreshInstallStateMachineTests(unittest.TestCase):
    def test_exact_schema_rejects_unknown_secret_metadata(self) -> None:
        current = operation()
        payload = current.as_dict()
        payload["metadata"] = {"databasePassword": "must-not-appear"}

        with self.assertRaisesRegex(
            FreshInstallOperationError, "FRESH_OPERATION_SCHEMA_INVALID"
        ) as raised:
            parse_fresh_install_operation(payload)
        self.assertNotIn("must-not-appear", str(raised.exception))

    def test_before_mutation_failure_has_no_recovery_barrier(self) -> None:
        failed = fail_fresh_install(
            operation(),
            error_code="PLATFORM_UNSUPPORTED",
            at="2026-08-16T00:00:01Z",
        )
        self.assertEqual(failed.status, FreshInstallStatus.FAILED_NO_MUTATION)
        self.assertFalse(failed.recovery_required)

    def test_reversible_failure_requires_explicit_rollback_proof(self) -> None:
        current = transition_phase(
            operation(), FreshInstallPhase.ROOTS_PREPARING, at=next_time(1)
        )
        current = mark_mutation_started(current, at=next_time(2))
        blocked = fail_fresh_install(
            current, error_code="CONFIG_WRITE_FAILED", at=next_time(3)
        )
        self.assertEqual(blocked.status, FreshInstallStatus.MANUAL_RECOVERY_REQUIRED)

        rolled_back = fail_fresh_install(
            current,
            error_code="CONFIG_WRITE_FAILED",
            at=next_time(3),
            rollback_succeeded=True,
        )
        self.assertEqual(rolled_back.status, FreshInstallStatus.ROLLED_BACK)

    def test_database_boundary_is_durable_and_never_auto_rolls_back(self) -> None:
        current = operation()
        for index, phase in enumerate(
            (
                FreshInstallPhase.ROOTS_PREPARING,
                FreshInstallPhase.CONFIG_STAGING,
                FreshInstallPhase.MATERIAL_STAGING,
                FreshInstallPhase.SERVICES_PREPARING,
                FreshInstallPhase.DATABASE_MIGRATING,
            ),
            start=1,
        ):
            current = transition_phase(current, phase, at=next_time(index))
            if phase is FreshInstallPhase.ROOTS_PREPARING:
                current = mark_mutation_started(current, at=next_time(index + 10))
        current = mark_irreversible_mutation_started(current, at=next_time(20))
        failed = fail_fresh_install(
            current,
            error_code="DATABASE_MIGRATION_FAILED",
            at=next_time(21),
            rollback_succeeded=True,
        )

        self.assertTrue(failed.irreversible_mutation_started)
        self.assertEqual(failed.status, FreshInstallStatus.MANUAL_RECOVERY_REQUIRED)
        self.assertEqual(failed.failed_step, "database_migrating")

    def test_success_requires_every_phase_and_complete_doctor(self) -> None:
        current = operation()
        index = 1
        for phase in PHASE_ORDER[1:-1]:
            current = transition_phase(current, phase, at=next_time(index))
            index += 1
            if phase is FreshInstallPhase.ROOTS_PREPARING:
                current = mark_mutation_started(current, at=next_time(index))
                index += 1
            if phase is FreshInstallPhase.DATABASE_MIGRATING:
                current = mark_irreversible_mutation_started(
                    current, at=next_time(index)
                )
                index += 1
        completed = succeed_fresh_install(current, at=next_time(index))

        self.assertEqual(completed.status, FreshInstallStatus.SUCCEEDED)
        self.assertEqual(completed.phase, FreshInstallPhase.COMPLETE)
        self.assertTrue(completed.target_active)
        self.assertEqual(
            parse_fresh_install_operation_bytes(completed.canonical_bytes()),
            completed,
        )

    def test_out_of_order_transition_and_early_irreversible_mark_fail(self) -> None:
        with self.assertRaisesRegex(
            FreshInstallOperationError, "FRESH_OPERATION_TRANSITION_INVALID"
        ):
            transition_phase(
                operation(), FreshInstallPhase.DATABASE_MIGRATING, at=next_time(1)
            )
        with self.assertRaisesRegex(
            FreshInstallOperationError,
            "FRESH_OPERATION_IRREVERSIBLE_BOUNDARY_INVALID",
        ):
            mark_irreversible_mutation_started(operation(), at=next_time(1))


class FreshInstallJournalTests(unittest.TestCase):
    def test_private_atomic_journal_round_trip_and_recovery_barrier(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_root = Path(directory)
            journal = FreshInstallOperationJournal(state_root)
            current = operation()
            journal.create(current)
            entered = transition_phase(
                current, FreshInstallPhase.ROOTS_PREPARING, at=next_time(1)
            )
            journal.persist(current, entered)
            mutating = mark_mutation_started(entered, at=next_time(2))
            journal.persist(entered, mutating)
            blocked = fail_fresh_install(
                mutating,
                error_code="ROOT_PREPARATION_FAILED",
                at=next_time(3),
            )
            journal.persist(mutating, blocked)

            self.assertEqual(journal.load(OPERATION_ID), blocked)
            self.assertEqual(journal.recovery_block(), blocked)
            with self.assertRaises(FreshInstallRecoveryRequired) as raised:
                journal.require_recovery_clear()
            self.assertEqual(raised.exception.operation_id, OPERATION_ID)
            if os.name != "nt":
                mode = (
                    (state_root / "operations" / f"{OPERATION_ID}.json").stat().st_mode
                )
                self.assertEqual(mode & 0o777, 0o600)

            reconciled = reconcile_fresh_install(blocked, at=next_time(4))
            journal.persist(blocked, reconciled)
            self.assertIsNone(journal.recovery_block())

    def test_stale_write_and_duplicate_creation_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            journal = FreshInstallOperationJournal(Path(directory))
            current = operation()
            journal.create(current)
            with self.assertRaises(FreshInstallOperationError):
                journal.create(current)
            next_record = transition_phase(
                current, FreshInstallPhase.ROOTS_PREPARING, at=next_time(1)
            )
            journal.persist(current, next_record)
            with self.assertRaisesRegex(
                FreshInstallOperationError, "FRESH_OPERATION_STALE"
            ):
                journal.persist(current, next_record)

    def test_unknown_update_record_is_not_misclassified_as_fresh(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_root = Path(directory)
            operations = state_root / "operations"
            operations.mkdir(mode=0o700)
            (operations / ("b" * 32 + ".json")).write_text(
                json.dumps(
                    {
                        "id": "b" * 32,
                        "kind": "apply_update",
                        "status": "manual_recovery_required",
                    }
                ),
                encoding="utf-8",
            )
            if os.name != "nt":
                os.chmod(operations / ("b" * 32 + ".json"), 0o600)
            journal = FreshInstallOperationJournal(state_root)
            self.assertIsNone(journal.recovery_block())


if __name__ == "__main__":
    unittest.main()
