from __future__ import annotations

import copy
import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from release.notes import CANONICAL_RELEASE_ASSETS
from release.publication import build_publication_plan
from release.publication_transaction import (
    DurablePublicationController,
    GitRemoteAppendOnlyJournal,
    LocalAtomicJournal,
    MutationIntent,
    MutationResponse,
    ObservationClass,
    PublicationTransactionError,
    RemoteObservation,
    _next_snapshot,
    build_initial_ledger,
    validate_ledger,
)


SHA_A = "sha256:" + "a" * 64
SHA_B = "sha256:" + "b" * 64
SHA_C = "sha256:" + "c" * 64
COMMIT_A = "a" * 40
TREE_A = "b" * 40


def _identity(value: object) -> str:
    payload = (
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _candidate_plan() -> dict[str, object]:
    plan: dict[str, object] = {
        "schema": "animemo.publish-candidate-plan/v1",
        "version": 1,
        "repository": "yanyuhanyue/AniMemo",
        "qualification_run_id": 33293139895,
        "qualification_run_attempt": 1,
        "source_sha": COMMIT_A,
        "source_tree": TREE_A,
        "candidate_version": "v1.1.0-rc.19",
        "candidate_input_digest": SHA_A,
        "verified_candidate_digest": SHA_B,
        "candidate_acceptance_receipt_digest": SHA_C,
        "release_manifest_digest": SHA_A,
        "producer_toolchain_receipt_digest": SHA_B,
        "candidate_runtime_inventory_digest": SHA_C,
        "paths": {
            "candidate_input": "candidate-input.json",
            "verified_candidate": "verified-candidate.json",
            "candidate_acceptance_receipt": "candidate-acceptance-receipt.json",
            "release_manifest": "release-manifest.json",
            "producer_toolchain_receipt": "release-producer-toolchain-receipt.json",
            "checksums": "checksums.txt",
            "deployment_contract": "deployment-contract.json",
            "installer_materials": "installer-materials.tar",
            "candidate_runtime": "candidate-runtime",
        },
        "images": {
            "api": {
                "digest": SHA_A,
                "layout_path": "candidate-runtime/oci/api",
                "platform": "linux/amd64",
                "repository": "ghcr.io/yanyuhanyue/animemo-api",
            },
            "web": {
                "digest": SHA_B,
                "layout_path": "candidate-runtime/oci/web",
                "platform": "linux/amd64",
                "repository": "ghcr.io/yanyuhanyue/animemo-web",
            },
        },
        "publish_rebuild_count": 0,
        "manifest_generation_count": 0,
        "mutation_authorized": False,
        "plan_digest": "",
    }
    unsigned = dict(plan)
    unsigned.pop("plan_digest")
    plan["plan_digest"] = _identity(unsigned)
    return plan


def _publication_plan(*, v2: bool) -> dict[str, object]:
    assets = {
        name: {"sha256": SHA_A, "size": 1}
        for name in CANONICAL_RELEASE_ASSETS
    }
    transport = (
        {
            "animemo-v1.1.0-portable.tar": {
                "role": "PORTABLE_RELEASE_BUNDLE",
                "sha256": SHA_C,
                "size": 1,
            }
        }
        if v2
        else None
    )
    return build_publication_plan(
        repository="yanyuhanyue/AniMemo",
        channel="stable",
        tag="v1.1.0",
        commit=COMMIT_A,
        qualification_identity=SHA_A,
        release_notes_identity=SHA_B,
        release_notes_markdown_sha256=SHA_C,
        assets=assets,
        api_digest=SHA_A,
        web_digest=SHA_B,
        transport_assets=transport,
    )


def _intents(count: int = 1) -> list[MutationIntent]:
    return [
        MutationIntent(
            name=f"step-{index}",
            kind="REGISTRY_PUSH",
            remote_key=f"ghcr.io/example/image:{index}",
            expected_identity=SHA_A if index == 1 else SHA_B,
        )
        for index in range(1, count + 1)
    ]


class FakeAdapter:
    def __init__(self, expected: str, *, present: str | None = None) -> None:
        self.expected = expected
        self.present = present
        self.observe_count = 0
        self.mutate_count = 0
        self.response = MutationResponse.acknowledged()
        self.forced_observation: RemoteObservation | None = None
        self.mutate_effect = True
        self.interrupt_after_effect = False

    def observe(self, _intent: MutationIntent) -> RemoteObservation:
        self.observe_count += 1
        if self.forced_observation is not None:
            return self.forced_observation
        if self.present is None:
            return RemoteObservation.absent()
        if self.present == self.expected:
            return RemoteObservation.same(self.present)
        return RemoteObservation.different(self.present)

    def mutate(self, _intent: MutationIntent) -> MutationResponse:
        self.mutate_count += 1
        if self.mutate_effect:
            self.present = self.expected
        if self.interrupt_after_effect:
            raise KeyboardInterrupt()
        return self.response


class PublicationTransactionPlanTests(unittest.TestCase):
    def test_candidate_and_publication_v1_v2_plans_are_closed(self) -> None:
        cases = (
            (_candidate_plan(), "animemo.publish-candidate-plan/v1", 0),
            (_publication_plan(v2=False), "animemo.release-publication-plan/v1", 4),
            (_publication_plan(v2=True), "animemo.release-publication-plan/v2", 4),
        )
        for plan, schema, asset_count in cases:
            with self.subTest(schema=schema):
                ledger = build_initial_ledger(
                    plan, source_tree=TREE_A, intents=_intents()
                )
                self.assertEqual(ledger["planSchema"], schema)
                self.assertEqual(len(ledger["expected"]["assets"]), asset_count)

    def test_candidate_plan_rejects_semantically_invalid_but_redigested_input(self) -> None:
        for mutate in (
            lambda plan: plan["paths"].__setitem__("candidate_runtime", "elsewhere"),
            lambda plan: plan["images"]["api"].__setitem__("platform", "linux/arm64"),
            lambda plan: plan.__setitem__("qualification_run_attempt", True),
        ):
            with self.subTest(mutate=mutate):
                plan = _candidate_plan()
                mutate(plan)
                unsigned = dict(plan)
                unsigned.pop("plan_digest")
                plan["plan_digest"] = _identity(unsigned)
                with self.assertRaisesRegex(
                    PublicationTransactionError, "TRANSACTION_CANDIDATE_PLAN_INVALID"
                ):
                    build_initial_ledger(plan, source_tree=TREE_A, intents=_intents())

    def test_ledger_rejects_wrong_same_identity_even_with_recomputed_digest(self) -> None:
        ledger = build_initial_ledger(
            _candidate_plan(), source_tree=TREE_A, intents=_intents()
        )
        ledger["steps"][0]["preflight"] = {
            "classification": "SAME",
            "identity": SHA_B,
            "diagnosticCode": None,
        }
        ledger.pop("ledgerIdentity")
        ledger["ledgerIdentity"] = _identity(ledger)
        with self.assertRaisesRegex(
            PublicationTransactionError, "TRANSACTION_OBSERVATION_INVALID"
        ):
            validate_ledger(ledger)


class PublicationTransactionControllerTests(unittest.TestCase):
    def _open(
        self,
        root: Path,
        adapters: dict[str, FakeAdapter],
        *,
        plan: dict[str, object] | None = None,
    ) -> DurablePublicationController:
        intents = [
            MutationIntent(name, "REGISTRY_PUSH", f"remote:{name}", adapter.expected)
            for name, adapter in adapters.items()
        ]
        return DurablePublicationController.open(
            plan or _candidate_plan(),
            source_tree=TREE_A,
            intents=intents,
            journal=LocalAtomicJournal(root),
            adapters=adapters,
        )

    def test_preflight_observes_entire_batch_before_global_freeze(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            adapters = {
                "first": FakeAdapter(SHA_A),
                "second": FakeAdapter(SHA_B, present=SHA_C),
            }
            controller = self._open(Path(directory), adapters)
            with self.assertRaisesRegex(
                PublicationTransactionError, "TRANSACTION_GLOBAL_FREEZE"
            ):
                controller.preflight_all()
            self.assertEqual([item.observe_count for item in adapters.values()], [1, 1])
            self.assertEqual([item.mutate_count for item in adapters.values()], [0, 0])
            self.assertEqual(controller.ledger["finalState"], "FROZEN")

    def test_interrupted_mutation_is_read_back_without_replay(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            adapter = FakeAdapter(SHA_A)
            adapter.interrupt_after_effect = True
            controller = self._open(root, {"registry": adapter})
            controller.preflight_all()
            with self.assertRaises(KeyboardInterrupt):
                controller.advance("registry")
            self.assertEqual(adapter.mutate_count, 1)

            resumed = self._open(root, {"registry": adapter})
            resumed.resume_pending()
            resumed.finalize()
            self.assertEqual(adapter.mutate_count, 1)
            self.assertEqual(resumed.ledger["finalState"], "COMPLETE")
            self.assertEqual(
                resumed.ledger["steps"][0]["attempts"][0]["response"][
                    "diagnosticCode"
                ],
                "CONTROLLER_INTERRUPTED",
            )

    def test_external_action_seam_records_intent_then_only_reads_back(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            adapter = FakeAdapter(SHA_A)
            controller = self._open(Path(directory), {"attestation": adapter})
            controller.preflight_all()
            started = controller.begin_external("attestation")
            self.assertEqual(started["steps"][0]["state"], "REQUEST_STARTED")
            self.assertIsNone(started["steps"][0]["attempts"][0]["response"])
            self.assertEqual(adapter.mutate_count, 0)

            # Simulate the workflow action changing only its declared remote key.
            adapter.present = SHA_A
            reconciled = controller.reconcile_external(
                "attestation", response=MutationResponse.acknowledged()
            )
            self.assertEqual(reconciled["steps"][0]["state"], "COMMITTED")
            self.assertEqual(adapter.mutate_count, 0)

    def test_ambiguous_absent_request_can_retry_up_to_exact_remote_commit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            adapter = FakeAdapter(SHA_A)
            adapter.mutate_effect = False
            adapter.response = MutationResponse.ambiguous()
            controller = self._open(Path(directory), {"registry": adapter})
            controller.preflight_all()
            controller.advance("registry")
            self.assertEqual(controller.ledger["steps"][0]["state"], "READY")
            self.assertEqual(len(controller.ledger["steps"][0]["attempts"]), 1)

            adapter.mutate_effect = True
            adapter.response = MutationResponse.acknowledged()
            controller.advance("registry")
            controller.finalize()
            self.assertEqual(adapter.mutate_count, 2)
            self.assertEqual(controller.ledger["finalState"], "COMPLETE")

    def test_terminal_absent_response_is_durably_frozen(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            adapter = FakeAdapter(SHA_A)
            adapter.mutate_effect = False
            adapter.response = MutationResponse.terminal("REMOTE_POLICY_REJECTED")
            controller = self._open(root, {"registry": adapter})
            controller.preflight_all()
            with self.assertRaisesRegex(
                PublicationTransactionError, "TRANSACTION_GLOBAL_FREEZE"
            ):
                controller.advance("registry")
            self.assertEqual(controller.ledger["finalState"], "FROZEN")
            reopened = self._open(root, {"registry": adapter})
            self.assertEqual(reopened.ledger["finalState"], "FROZEN")
            self.assertEqual(adapter.mutate_count, 1)

    def test_invalid_observation_class_never_authorizes_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            adapter = FakeAdapter(SHA_A)
            adapter.forced_observation = RemoteObservation("INVALID")  # type: ignore[arg-type]
            controller = self._open(Path(directory), {"registry": adapter})
            with self.assertRaisesRegex(
                PublicationTransactionError, "TRANSACTION_GLOBAL_FREEZE"
            ):
                controller.preflight_all()
            self.assertEqual(adapter.mutate_count, 0)
            self.assertEqual(
                controller.ledger["steps"][0]["preflight"]["classification"],
                "UNKNOWN",
            )

    def test_steps_cannot_advance_out_of_declared_order(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            adapters = {"first": FakeAdapter(SHA_A), "second": FakeAdapter(SHA_B)}
            controller = self._open(Path(directory), adapters)
            controller.preflight_all()
            with self.assertRaisesRegex(
                PublicationTransactionError, "TRANSACTION_STEP_ORDER_INVALID"
            ):
                controller.advance("second")
            self.assertEqual(adapters["second"].mutate_count, 0)
            controller.advance("first")
            controller.advance("second")
            first_mutations = adapters["first"].mutate_count
            controller.advance("first")
            self.assertEqual(adapters["first"].mutate_count, first_mutations)
            controller.finalize()
            revision = controller.ledger["revision"]
            self.assertEqual(controller.preflight_all()["revision"], revision)

    def test_finalize_freshly_reads_back_all_keys_and_freezes_on_regression(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            adapter = FakeAdapter(SHA_A)
            controller = self._open(Path(directory), {"registry": adapter})
            controller.preflight_all()
            controller.advance("registry")
            observations_before = adapter.observe_count
            adapter.present = None
            with self.assertRaisesRegex(
                PublicationTransactionError, "TRANSACTION_GLOBAL_FREEZE"
            ):
                controller.finalize()
            self.assertGreater(adapter.observe_count, observations_before)
            self.assertEqual(controller.ledger["finalState"], "FROZEN")

    def test_same_operation_key_rejects_changed_plan(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            adapter = FakeAdapter(SHA_A)
            self._open(root, {"registry": adapter})
            changed = _candidate_plan()
            changed["candidate_input_digest"] = SHA_C
            unsigned = dict(changed)
            unsigned.pop("plan_digest")
            changed["plan_digest"] = _identity(unsigned)
            with self.assertRaisesRegex(
                PublicationTransactionError, "TRANSACTION_PLAN_MISMATCH"
            ):
                self._open(root, {"registry": adapter}, plan=changed)


class JournalTests(unittest.TestCase):
    def test_local_journal_rejects_unexpected_entry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            journal = LocalAtomicJournal(root)
            ledger = build_initial_ledger(
                _candidate_plan(), source_tree=TREE_A, intents=_intents()
            )
            journal.append(ledger)
            operation = ledger["operationId"].removeprefix("sha256:")
            (root / operation / "foreign.txt").write_text("x", encoding="utf-8")
            with self.assertRaisesRegex(
                PublicationTransactionError, "TRANSACTION_JOURNAL_CORRUPT"
            ):
                journal.load(ledger["operationId"])

    def test_git_remote_journal_is_linear_fast_forward_and_rejects_stale_append(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository = root / "work"
            remote = root / "remote.git"
            subprocess.run(
                ["git", "init", "--quiet", str(repository)], check=True
            )
            subprocess.run(
                ["git", "init", "--quiet", "--bare", str(remote)], check=True
            )
            subprocess.run(
                ["git", "-C", str(repository), "remote", "add", "origin", str(remote)],
                check=True,
            )
            journal = GitRemoteAppendOnlyJournal(repository)
            initial = build_initial_ledger(
                _candidate_plan(), source_tree=TREE_A, intents=_intents()
            )
            self.assertEqual(journal.append(initial), initial)

            first = copy.deepcopy(initial)
            first["steps"][0]["preflight"] = {
                "classification": ObservationClass.ABSENT.value,
                "identity": None,
                "diagnosticCode": None,
            }
            first["steps"][0]["state"] = "READY"
            first = _next_snapshot(first)
            journal.append(first)
            self.assertEqual(journal.load(initial["operationId"]), first)

            stale = copy.deepcopy(initial)
            stale["steps"][0]["preflight"] = {
                "classification": ObservationClass.SAME.value,
                "identity": SHA_A,
                "diagnosticCode": None,
            }
            stale["steps"][0]["state"] = "COMMITTED"
            stale["steps"][0]["committed"] = True
            stale = _next_snapshot(stale)
            with self.assertRaisesRegex(
                PublicationTransactionError, "TRANSACTION_JOURNAL_APPEND_CONFLICT"
            ):
                journal.append(stale)
            self.assertEqual(journal.load(initial["operationId"]), first)


if __name__ == "__main__":
    unittest.main()
