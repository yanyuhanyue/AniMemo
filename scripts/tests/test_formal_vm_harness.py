from __future__ import annotations

import hashlib
import inspect
import io
import json
import os
import pickle
import tarfile
import unittest
from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from release.candidate import canonical_json_bytes
from release.formal_acceptance_test_support import build_test_formal_acceptance
from release.formal_vm_controller import (
    CANDIDATE_PROFILE_RESULT_KEYS,
    FormalAuthorityRequest,
    FormalExecutionContext,
    FormalProducerError,
    FormalProfileObservation,
    FormalProvenanceInput,
    FormalProvenancePlan,
    FormalVmController,
    ProductionFormalAuthorityVerifier,
    ProvenancePreflightError,
    QualifiedCandidateFormalAuthority,
    VerifiedFormalRcAuthority,
    _issue_qualified_candidate_formal_authority,
)
from scripts import candidate_vm_harness as provider_contract
from scripts import closed_runtime_inventory as runtime_inventory_contract
from scripts import formal_vm_harness as harness
from scripts.tests.formal_windows_pretrust_fixture import (
    create_test_formal_windows_pretrust_kit,
    private_windows_test_directory,
)

tempfile = SimpleNamespace(TemporaryDirectory=private_windows_test_directory)


def _candidate_source_evidence() -> dict[str, object]:
    original_vm_hashes = {
        name: "sha256:" + "c" * 64
        for name in (
            *provider_contract.SOURCE_VM_HASH_FILES,
            *provider_contract.SOURCE_VM_PRIVATE_ADDITIONAL_FILES,
        )
    }
    return {
        "candidate_plan_digest": "sha256:" + "a" * 64,
        "candidate_provider_execution_authority_receipt_digest": (
            "sha256:" + "9" * 64
        ),
        "candidate_base_vm_identity": harness.sha256_bytes(
            canonical_json_bytes(original_vm_hashes)
        ),
        "candidate_original_vm_hashes": original_vm_hashes,
        "candidate_snapshot_identities": {
            "FRESH_BASE": "sha256:" + "d" * 64,
            "DOCKER_BASE": "sha256:" + "e" * 64,
            "RUNTIME_BASE_OFFLINE": "sha256:" + "f" * 64,
        },
        "candidate_source_disk_graph_identity": "sha256:" + "1" * 64,
        "candidate_snapshot_disk_graph_identities": {
            "FRESH_BASE": "sha256:" + "2" * 64,
            "DOCKER_BASE": "sha256:" + "3" * 64,
            "RUNTIME_BASE_OFFLINE": "sha256:" + "4" * 64,
        },
        "candidate_source_vm_inventory_identity": "sha256:" + "5" * 64,
    }


def _qualified_source_evidence() -> dict[str, object]:
    evidence = _candidate_source_evidence()
    return {
        "candidate_plan_digest": evidence["candidate_plan_digest"],
        "candidate_provider_execution_authority_receipt_digest": evidence[
            "candidate_provider_execution_authority_receipt_digest"
        ],
        "base_vm_identity": evidence["candidate_base_vm_identity"],
        "original_vm_hashes": evidence["candidate_original_vm_hashes"],
        "snapshot_identities": evidence["candidate_snapshot_identities"],
        "source_disk_graph_identity": evidence[
            "candidate_source_disk_graph_identity"
        ],
        "snapshot_disk_graph_identities": evidence[
            "candidate_snapshot_disk_graph_identities"
        ],
        "source_vm_inventory_identity": evidence[
            "candidate_source_vm_inventory_identity"
        ],
    }


def _formal_observation(
    authority: VerifiedFormalRcAuthority, profile: str, *, result: str = "PASS"
) -> FormalProfileObservation:
    profile_hex = {
        "FORMAL_FRESH": "8",
        "FORMAL_DOCKER": "9",
        "FORMAL_OFFLINE": "a",
    }[profile]
    return FormalProfileObservation(
        profile=profile,
        rc_authority_identity=authority.identity,
        transport_source=(
            "local-bundle" if profile == "FORMAL_OFFLINE" else "github"
        ),
        resolved_version=authority.rc_tag,
        resolved_source_sha=authority.source_sha,
        resolved_manifest_identity=authority.release_manifest_identity,
        resolved_deployment_contract_identity=(
            authority.deployment_contract_identity
        ),
        resolved_installer_materials_identity=(authority.installer_materials_identity),
        resolved_api_digest=authority.api_digest,
        resolved_web_digest=authority.web_digest,
        resolved_publication_identity=authority.publication_identity,
        resolved_workflow_identity=authority.workflow_identity,
        resolved_attestation_claim_identities=authority.attestation_claim_identities,
        base_vm_identity="sha256:" + "0" * 64,
        snapshot_identity="sha256:" + profile_hex * 64,
        clone_identity="sha256:" + profile_hex * 64,
        provider_execution_authority_receipt_digest="sha256:" + "1" * 64,
        publication_execution_receipt_identity=(
            authority.publication_execution_receipt_identity
        ),
        publication_signed_claim_identity=(authority.publication_signed_claim_identity),
        publication_signed_at=authority.publication_signed_at,
        formal_windows_pretrust_kit_identity=(
            authority.formal_windows_pretrust_kit_identity
        ),
        offline_release_trust_profile_identity=(
            authority.offline_release_trust_profile_identity
        ),
        pretrusted_profile_identity=authority.pretrusted_profile_identity,
        provenance_verifier_identity=authority.provenance_verifier_identity,
        github_trusted_root_identity=authority.github_trusted_root_identity,
        sigstore_trusted_root_identity=authority.sigstore_trusted_root_identity,
        platform_plan_digest="sha256:" + "b" * 64,
        platform_receipt_digest="sha256:" + "c" * 64,
        installer_plan_digest="sha256:" + "1" * 64,
        installer_execution_receipt_digest="sha256:" + "2" * 64,
        doctor_receipt_digest="sha256:" + "3" * 64,
        canonical_acceptance_receipt_digests=tuple(
            "sha256:" + character * 64 for character in "456"
        ),
        continuation_receipt_digest="sha256:" + "7" * 64,
        result=result,
    )


class FormalVmHarnessTests(unittest.TestCase):
    def _test_lifetime(self, events=None):
        root = Path.cwd().resolve()
        return harness._issue_continuation_evidence_lifetime_authority(
            continuation_root=root,
            evidence_root=root,
            seal_root=root,
            test_enter=(
                (lambda: events.append("lifetime-enter"))
                if events is not None
                else (lambda: None)
            ),
            test_exit=(
                (lambda: events.append("lifetime-exit"))
                if events is not None
                else (lambda: None)
            ),
        )

    def _enter_worker(self, worker):
        worker.__enter__()
        self.addCleanup(worker.fail_closed_cleanup)
        return worker

    def test_evidence_lifetime_factory_holds_closed_roots_and_verifies_seal(self):
        with tempfile.TemporaryDirectory() as directory:
            continuation = harness.create_windows_private_directory(
                Path(directory), prefix="formal-continuation"
            )
            evidence = harness.create_windows_private_directory(
                continuation, prefix="evidence"
            )
            seals = harness.create_windows_private_directory(
                continuation, prefix="seals"
            )
            private_work = harness.create_windows_private_directory(
                continuation, prefix="private-work"
            )
            sums = evidence / "SHA256SUMS"
            payload = evidence / "formal-execution-receipt.json"
            payload_bytes = b'{"result":"PASS"}\n'
            payload.write_bytes(payload_bytes)
            sums_bytes = (
                hashlib.sha256(payload_bytes).hexdigest()
                + "  formal-execution-receipt.json\n"
            ).encode("ascii")
            sums.write_bytes(sums_bytes)
            manifest_identity = (
                "sha256:" + hashlib.sha256(sums_bytes).hexdigest()
            )
            seal = seals / f"{evidence.name}.SHA256SUMS.sha256"
            seal_bytes = (
                f"{manifest_identity.removeprefix('sha256:')}  "
                f"{evidence.name}/SHA256SUMS\n"
            ).encode(
                "ascii"
            )
            seal.write_bytes(seal_bytes)
            authority = harness.acquire_continuation_evidence_lifetime_authority(
                continuation_root=continuation,
                evidence_root=evidence,
                seal_root=seals,
            )
            with self.assertRaises(TypeError):
                pickle.dumps(authority)
            with authority:
                with harness.hold_windows_private_descendant_path(
                    authority.path_authority,
                    private_work,
                    allow_leaf_child_writes=True,
                ):
                    self.assertTrue(private_work.is_dir())
                self.assertEqual(
                    authority.require_contained(
                        evidence / "formal-attempt-1", name="OUTPUT_ROOT"
                    ),
                    evidence / "formal-attempt-1",
                )
                with self.assertRaisesRegex(
                    FormalProducerError, "OUTSIDE_LIFETIME"
                ):
                    authority.require_contained(
                        Path(directory).resolve() / "outside",
                        name="OUTPUT_ROOT",
                    )
                with self.assertRaises(TypeError):
                    harness.ContinuationEvidenceSealSuccess()
                receipt = authority.verify_and_issue_seal_success()
                self.assertEqual(receipt.as_dict()["result"], "PASS")
                payload.write_bytes(b'{"result":"FAIL"}\n')
                with self.assertRaisesRegex(
                    FormalProducerError, "SHA256SUMS_MISMATCH"
                ):
                    authority.verify_and_issue_seal_success()
            with self.assertRaisesRegex(
                FormalProducerError, "FORMAL_EVIDENCE_LIFETIME_INVALID"
            ):
                _ = authority.evidence_root

    def test_parent_worker_requires_concrete_evidence_lifetime(self):
        with self.assertRaises((TypeError, FormalProducerError)):
            harness.ReleaseContinuationParentWorker(
                clear_r2_credentials=lambda: None,
                clear_sudo_credentials=lambda: None,
            )
        with self.assertRaisesRegex(FormalProducerError, "CLEANUP_INVALID"):
            harness.ReleaseContinuationParentWorker(
                clear_r2_credentials=lambda: None,
                clear_sudo_credentials=lambda: None,
                continuation_lifetime_authority=nullcontext(),
            )

    def test_evidence_seal_rejects_noncanonical_or_unbound_manifests(self):
        cases = (
            "missing",
            "extra",
            "duplicate",
            "unsorted",
            "traversal",
            "file-drift",
            "seal-digest",
            "seal-path",
            "seal-multiline",
            "extra-seal-file",
            "hardlink",
        )
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                continuation = harness.create_windows_private_directory(
                    Path(directory), prefix="formal-continuation-negative"
                )
                evidence = harness.create_windows_private_directory(
                    continuation, prefix="evidence"
                )
                seals = harness.create_windows_private_directory(
                    continuation, prefix="seals"
                )
                payloads = {
                    "a.json": b'{"a":1}\n',
                    "z.json": b'{"z":1}\n',
                }
                for name, value in payloads.items():
                    (evidence / name).write_bytes(value)
                entries = [
                    (name, hashlib.sha256(value).hexdigest())
                    for name, value in sorted(payloads.items())
                ]
                if case == "missing":
                    entries.pop()
                elif case == "extra":
                    entries.append(("missing.json", "0" * 64))
                elif case == "duplicate":
                    entries.append(entries[0])
                elif case == "unsorted":
                    entries.reverse()
                elif case == "traversal":
                    entries[0] = ("../a.json", entries[0][1])
                manifest = "".join(
                    f"{digest}  {name}\n" for name, digest in entries
                ).encode("ascii")
                (evidence / "SHA256SUMS").write_bytes(manifest)
                if case == "file-drift":
                    (evidence / "a.json").write_bytes(b'{"a":2}\n')
                if case == "hardlink":
                    os.link(evidence / "a.json", evidence / "linked.json")
                manifest_digest = hashlib.sha256(manifest).hexdigest()
                seal_name = f"{evidence.name}.SHA256SUMS.sha256"
                seal_relative = f"{evidence.name}/SHA256SUMS"
                if case == "seal-digest":
                    manifest_digest = "f" * 64
                if case == "seal-path":
                    seal_relative = "wrong/SHA256SUMS"
                seal_value = f"{manifest_digest}  {seal_relative}\n"
                if case == "seal-multiline":
                    seal_value += "0" * 64 + "  extra\n"
                (seals / seal_name).write_text(seal_value, encoding="ascii")
                if case == "extra-seal-file":
                    (seals / "extra").write_bytes(b"extra")
                authority = (
                    harness.acquire_continuation_evidence_lifetime_authority(
                        continuation_root=continuation,
                        evidence_root=evidence,
                        seal_root=seals,
                    )
                )
                with authority, self.assertRaises(FormalProducerError):
                    authority.verify_and_issue_seal_success()

    def test_parent_worker_keeps_capability_in_memory_and_clears_credentials(self):
        events: list[str] = []
        continuation = object()
        qualified = mock.Mock()
        qualified.candidate_aggregate_receipt_digest = "sha256:" + "5" * 64
        qualified.candidate_profile_receipt_digests = {
            "fresh_base": "sha256:" + "6" * 64,
            "docker_base": "sha256:" + "7" * 64,
            "runtime_base_offline": "sha256:" + "8" * 64,
        }
        qualified.candidate_source_vm_authority_identity = "sha256:" + "9" * 64
        worker = self._enter_worker(harness.ReleaseContinuationParentWorker(
            clear_r2_credentials=lambda: events.append("clear-r2"),
            clear_sudo_credentials=lambda: events.append("clear-sudo"),
            continuation_lifetime_authority=self._test_lifetime(),
        ))
        self.assertEqual(worker.credential_persistence, "NONE")
        with self.assertRaises(TypeError):
            pickle.dumps(worker)

        with (
            mock.patch.object(
                harness,
                "execute_candidate_controller_for_formal",
                side_effect=lambda **_: (
                    events.append("candidate-terminal") or continuation
                ),
            ) as execute_candidate,
            mock.patch.object(
                harness,
                "close_qualified_candidate_for_formal",
                side_effect=lambda *_, **__: (
                    events.append("candidate-close") or qualified
                ),
            ) as close_candidate,
        ):
            observed = worker.run_candidate(
                verified_candidate_digest="sha256:" + "1" * 64,
                expected_qualification_run_id=33293139895,
                expected_source_sha="2" * 40,
                expected_source_tree="3" * 40,
                provider=object(),
                authorize_plan=lambda _: None,
                state_root=Path.cwd() / "private-candidate-state",
                private_material_parent=Path.cwd() / "candidate-materials",
            )
        self.assertEqual(
            observed,
            {
                "status": "PASS",
                "verifiedCandidateDigest": "sha256:" + "1" * 64,
                "candidateAggregateReceiptDigest": "sha256:" + "5" * 64,
                "candidateProfileReceiptDigests": {
                    "fresh_base": "sha256:" + "6" * 64,
                    "docker_base": "sha256:" + "7" * 64,
                    "runtime_base_offline": "sha256:" + "8" * 64,
                },
                "candidateSourceVmAuthorityIdentity": "sha256:" + "9" * 64,
                "credentialPersistence": "NONE",
            },
        )
        self.assertEqual(
            events,
            ["candidate-terminal", "candidate-close", "clear-r2"],
        )
        execute_candidate.assert_called_once()
        self.assertEqual(
            execute_candidate.call_args.kwargs["_state_root"],
            Path.cwd() / "private-candidate-state",
        )
        close_candidate.assert_called_once_with(
            "sha256:" + "1" * 64,
            continuation,
        )
        with self.assertRaisesRegex(
            FormalProducerError, "FORMAL_PARENT_CANDIDATE_ALREADY_USED"
        ):
            worker.run_candidate(
                verified_candidate_digest="sha256:" + "1" * 64,
                expected_qualification_run_id=33293139895,
                expected_source_sha="2" * 40,
                expected_source_tree="3" * 40,
                provider=object(),
                authorize_plan=lambda _: None,
            )

        with mock.patch.object(
            harness,
            "execute_qualified_formal_production",
            side_effect=lambda **kwargs: (
                events.append("formal-terminal")
                or {"status": "PASS", "qualified": kwargs["qualified_candidate"]}
            ),
        ) as execute_formal:
            result = worker.run_formal(
                publication_identity="sha256:" + "4" * 64,
                attestation_claim_identities={},
                provenance_inputs=(),
                publication_input=object(),
                execution=object(),
                publication_root=Path.cwd() / "publication",
                private_work_root=Path.cwd() / "private",
                output_root=Path.cwd() / "output",
            )
        self.assertEqual(result["status"], "PASS")
        self.assertIs(result["qualified"], qualified)
        self.assertEqual(events[-2:], ["formal-terminal", "clear-sudo"])
        execute_formal.assert_called_once()
        qualified.close.assert_called_once_with()
        with self.assertRaisesRegex(
            FormalProducerError, "FORMAL_PARENT_CAPABILITY_UNAVAILABLE"
        ):
            worker.run_formal(
                publication_identity="sha256:" + "4" * 64,
                attestation_claim_identities={},
                provenance_inputs=(),
                publication_input=object(),
                execution=object(),
                publication_root=Path.cwd() / "publication",
                private_work_root=Path.cwd() / "private",
                output_root=Path.cwd() / "output",
            )

    def test_parent_worker_failure_clears_both_credential_scopes_once(self):
        events: list[str] = []
        worker = self._enter_worker(harness.ReleaseContinuationParentWorker(
            clear_r2_credentials=lambda: events.append("clear-r2"),
            clear_sudo_credentials=lambda: events.append("clear-sudo"),
            continuation_lifetime_authority=self._test_lifetime(),
        ))
        with (
            mock.patch.object(
                harness,
                "execute_candidate_controller_for_formal",
                side_effect=FormalProducerError("CANDIDATE_FAILED"),
            ),
            self.assertRaisesRegex(FormalProducerError, "CANDIDATE_FAILED"),
        ):
            worker.run_candidate(
                verified_candidate_digest="sha256:" + "1" * 64,
                expected_qualification_run_id=33293139895,
                expected_source_sha="2" * 40,
                expected_source_tree="3" * 40,
                provider=object(),
                authorize_plan=lambda _: None,
                private_material_parent=Path.cwd() / "candidate-materials",
            )
        worker.fail_closed_cleanup()
        self.assertEqual(events, ["clear-r2", "clear-sudo"])

    def test_parent_cleanup_hooks_retry_and_block_formal_or_seal_after_failure(self):
        calls = {"r2": 0, "sudo": 0}

        def clear_r2():
            calls["r2"] += 1
            if calls["r2"] == 1:
                raise RuntimeError("r2 clear failed")

        def clear_sudo():
            calls["sudo"] += 1
            if calls["sudo"] == 1:
                raise KeyboardInterrupt("sudo clear interrupted")

        qualified = mock.Mock()
        worker = harness.ReleaseContinuationParentWorker(
            clear_r2_credentials=clear_r2,
            clear_sudo_credentials=clear_sudo,
            continuation_lifetime_authority=self._test_lifetime(),
        )
        worker.__enter__()
        with (
            mock.patch.object(
                harness,
                "execute_candidate_controller_for_formal",
                return_value=object(),
            ),
            mock.patch.object(
                harness,
                "close_qualified_candidate_for_formal",
                return_value=qualified,
            ),
            self.assertRaises(harness.FormalCleanupFailure),
        ):
            worker.run_candidate(
                verified_candidate_digest="sha256:" + "1" * 64,
                expected_qualification_run_id=33293139895,
                expected_source_sha="2" * 40,
                expected_source_tree="3" * 40,
                provider=object(),
                authorize_plan=lambda _: None,
                private_material_parent=Path.cwd() / "candidate-materials",
            )
        self.assertEqual(calls, {"r2": 2, "sudo": 1})
        qualified.close.assert_called_once_with()
        with self.assertRaisesRegex(
            FormalProducerError, "FORMAL_PARENT_CAPABILITY_UNAVAILABLE"
        ):
            worker.run_formal(
                publication_identity="sha256:" + "4" * 64,
                attestation_claim_identities={},
                provenance_inputs=(),
                publication_input=object(),
                execution=object(),
                publication_root=Path.cwd() / "publication",
                private_work_root=Path.cwd() / "private",
                output_root=Path.cwd() / "output",
            )
        with self.assertRaisesRegex(
            FormalProducerError, "FORMAL_PARENT_SEAL_LIFETIME_INVALID"
        ):
            worker.seal_and_close(lambda: None)
        worker.fail_closed_cleanup()
        self.assertEqual(calls, {"r2": 2, "sudo": 2})

    def test_terminal_sudo_cleanup_must_retry_before_seal_can_release_hold(self):
        sudo_calls = 0

        def clear_sudo():
            nonlocal sudo_calls
            sudo_calls += 1
            if sudo_calls == 1:
                raise RuntimeError("sudo clear failed")

        qualified = mock.Mock()
        worker = harness.ReleaseContinuationParentWorker(
            clear_r2_credentials=lambda: None,
            clear_sudo_credentials=clear_sudo,
            continuation_lifetime_authority=self._test_lifetime(),
        )
        worker.__enter__()
        with (
            mock.patch.object(
                harness,
                "execute_candidate_controller_for_formal",
                return_value=object(),
            ),
            mock.patch.object(
                harness,
                "close_qualified_candidate_for_formal",
                return_value=qualified,
            ),
        ):
            worker.run_candidate(
                verified_candidate_digest="sha256:" + "1" * 64,
                expected_qualification_run_id=33293139895,
                expected_source_sha="2" * 40,
                expected_source_tree="3" * 40,
                provider=object(),
                authorize_plan=lambda _: None,
                private_material_parent=Path.cwd() / "candidate-materials",
            )
        with (
            mock.patch.object(
                harness,
                "execute_qualified_formal_production",
                return_value={"status": "PASS"},
            ),
            self.assertRaisesRegex(RuntimeError, "sudo clear failed"),
        ):
            worker.run_formal(
                publication_identity="sha256:" + "4" * 64,
                attestation_claim_identities={},
                provenance_inputs=(),
                publication_input=object(),
                execution=object(),
                publication_root=Path.cwd() / "publication",
                private_work_root=Path.cwd() / "private",
                output_root=Path.cwd() / "output",
            )
        with self.assertRaisesRegex(
            FormalProducerError, "FORMAL_PARENT_SEAL_LIFETIME_INVALID"
        ):
            worker.seal_and_close(lambda: None)
        worker.retry_terminal_cleanup_before_seal()
        self.assertEqual(sudo_calls, 2)
        self.assertIsInstance(
            worker.seal_and_close(lambda: None),
            harness.ContinuationEvidenceSealSuccess,
        )

    def test_parent_worker_allows_only_explicit_same_candidate_formal_retries(self):
        events: list[str] = []
        qualified = mock.Mock()
        worker = self._enter_worker(harness.ReleaseContinuationParentWorker(
            clear_r2_credentials=lambda: events.append("clear-r2"),
            clear_sudo_credentials=lambda: events.append("clear-sudo"),
            continuation_lifetime_authority=self._test_lifetime(),
        ))
        with (
            mock.patch.object(
                harness,
                "execute_candidate_controller_for_formal",
                return_value=object(),
            ),
            mock.patch.object(
                harness,
                "close_qualified_candidate_for_formal",
                return_value=qualified,
            ),
        ):
            worker.run_candidate(
                verified_candidate_digest="sha256:" + "1" * 64,
                expected_qualification_run_id=33293139895,
                expected_source_sha="2" * 40,
                expected_source_tree="3" * 40,
                provider=object(),
                authorize_plan=lambda _: None,
                private_material_parent=Path.cwd() / "candidate-materials",
            )
        retryable_failure = {
            "status": "FAIL",
            "executionReceipt": {
                "profile_results": {
                    "formal_fresh": {"status": "ERROR"},
                    "formal_docker": {"status": "NOT_RUN_SHARED_BLOCKER"},
                    "formal_offline": {"status": "NOT_RUN_SHARED_BLOCKER"},
                }
            },
        }
        success = {"status": "PASS"}
        common = {
            "publication_identity": "sha256:" + "4" * 64,
            "attestation_claim_identities": {},
            "provenance_inputs": (),
            "publication_input": object(),
            "execution": object(),
            "publication_root": Path.cwd() / "publication",
            "private_work_root": Path.cwd() / "private",
        }
        with mock.patch.object(
            harness,
            "execute_qualified_formal_production",
            side_effect=(retryable_failure, success),
        ) as execute:
            first = worker.run_formal(
                **common,
                output_root=Path.cwd() / "attempt-1",
            )
            self.assertIs(first, retryable_failure)
            self.assertNotIn("clear-sudo", events)
            qualified.close.assert_not_called()
            with self.assertRaisesRegex(
                FormalProducerError, "FORMAL_PARENT_OUTPUT_ROOT_REUSED"
            ):
                worker.run_formal(
                    **common,
                    output_root=Path.cwd() / "attempt-1",
                )
            second = worker.run_formal(
                **common,
                output_root=Path.cwd() / "attempt-2",
            )
        self.assertIs(second, success)
        self.assertEqual(execute.call_count, 2)
        self.assertIs(
            execute.call_args_list[0].kwargs["qualified_candidate"], qualified
        )
        self.assertIs(
            execute.call_args_list[1].kwargs["qualified_candidate"], qualified
        )
        self.assertEqual(events, ["clear-r2", "clear-sudo"])
        qualified.close.assert_called_once_with()

    def test_parent_worker_closes_candidate_and_sudo_after_fifth_formal_error(self):
        events: list[str] = []
        qualified = mock.Mock()
        worker = self._enter_worker(harness.ReleaseContinuationParentWorker(
            clear_r2_credentials=lambda: events.append("clear-r2"),
            clear_sudo_credentials=lambda: events.append("clear-sudo"),
            continuation_lifetime_authority=self._test_lifetime(),
        ))
        with (
            mock.patch.object(
                harness,
                "execute_candidate_controller_for_formal",
                return_value=object(),
            ),
            mock.patch.object(
                harness,
                "close_qualified_candidate_for_formal",
                return_value=qualified,
            ),
        ):
            worker.run_candidate(
                verified_candidate_digest="sha256:" + "1" * 64,
                expected_qualification_run_id=33293139895,
                expected_source_sha="2" * 40,
                expected_source_tree="3" * 40,
                provider=object(),
                authorize_plan=lambda _: None,
                private_material_parent=Path.cwd() / "candidate-materials",
            )
        common = {
            "publication_identity": "sha256:" + "4" * 64,
            "attestation_claim_identities": {},
            "provenance_inputs": (),
            "publication_input": object(),
            "execution": object(),
            "publication_root": Path.cwd() / "publication",
            "private_work_root": Path.cwd() / "private",
        }
        with mock.patch.object(
            harness,
            "execute_qualified_formal_production",
            side_effect=FormalProducerError("FORMAL_INFRASTRUCTURE_FAILED"),
        ) as execute:
            for attempt in range(1, 6):
                with self.assertRaisesRegex(
                    FormalProducerError, "FORMAL_INFRASTRUCTURE_FAILED"
                ):
                    worker.run_formal(
                        **common,
                        output_root=Path.cwd() / f"attempt-{attempt}",
                    )
                if attempt < 5:
                    qualified.close.assert_not_called()
                    self.assertNotIn("clear-sudo", events)
            with self.assertRaisesRegex(
                FormalProducerError, "FORMAL_PARENT_CAPABILITY_UNAVAILABLE"
            ):
                worker.run_formal(
                    **common,
                    output_root=Path.cwd() / "attempt-6",
                )
        self.assertEqual(execute.call_count, 5)
        qualified.close.assert_called_once_with()
        self.assertEqual(events, ["clear-r2", "clear-sudo"])

    def test_parent_worker_releases_evidence_lifetime_only_after_seal_callback(self):
        events: list[str] = []

        qualified = mock.Mock()
        worker = harness.ReleaseContinuationParentWorker(
            clear_r2_credentials=lambda: events.append("clear-r2"),
            clear_sudo_credentials=lambda: events.append("clear-sudo"),
            continuation_lifetime_authority=self._test_lifetime(events),
        )
        with (
            worker,
            mock.patch.object(
                harness,
                "execute_candidate_controller_for_formal",
                return_value=object(),
            ),
            mock.patch.object(
                harness,
                "close_qualified_candidate_for_formal",
                return_value=qualified,
            ),
            mock.patch.object(
                harness,
                "execute_qualified_formal_production",
                return_value={"status": "PASS"},
            ),
        ):
            worker.run_candidate(
                verified_candidate_digest="sha256:" + "1" * 64,
                expected_qualification_run_id=33293139895,
                expected_source_sha="2" * 40,
                expected_source_tree="3" * 40,
                provider=object(),
                authorize_plan=lambda _: None,
                private_material_parent=Path.cwd() / "candidate-materials",
            )
            worker.run_formal(
                publication_identity="sha256:" + "4" * 64,
                attestation_claim_identities={},
                provenance_inputs=(),
                publication_input=object(),
                execution=object(),
                publication_root=Path.cwd() / "publication",
                private_work_root=Path.cwd() / "private",
                output_root=Path.cwd() / "formal-output",
            )
            self.assertNotIn("lifetime-exit", events)
            with self.assertRaisesRegex(RuntimeError, "seal failed"):
                worker.seal_and_close(
                    lambda: (_ for _ in ()).throw(RuntimeError("seal failed"))
                )
            self.assertNotIn("lifetime-exit", events)
            self.assertEqual(
                worker.seal_and_close(
                    lambda: events.append("seal") or None
                ),
                mock.ANY,
            )
        self.assertLess(events.index("seal"), events.index("lifetime-exit"))
        self.assertEqual(events.count("lifetime-exit"), 1)

    def test_parent_entry_uses_opaque_candidate_then_preflight_before_provider(self):
        request = self.request()
        authority = self.authority()
        qualified = _issue_qualified_candidate_formal_authority(
            loaded=SimpleNamespace(root=Path("candidate-root")),
            candidate_aggregate_receipt_digest="sha256:" + "1" * 64,
            candidate_profile_receipt_digests={
                key: "sha256:" + value * 64
                for key, value in zip(CANDIDATE_PROFILE_RESULT_KEYS, "234", strict=True)
            },
            **_qualified_source_evidence(),
            formal_windows_pretrust_root=Path("candidate-kit"),
        )
        execution = FormalExecutionContext(
            accepted_at="2026-08-30T01:02:03Z",
            observed_at="2026-08-30T01:01:59Z",
            operator_identity="formal-reviewer",
            run_id="formal-run-1",
            run_attempt=1,
            correlation_id="formal-correlation-1",
            current_workflow_commit="e" * 40,
            execution_environment="windows-vmware-private",
            tool_identity="sha256:" + "f" * 64,
        )
        events: list[str] = []
        transaction = mock.Mock(reused=False)
        transaction.commit.side_effect = lambda _value: events.append("commit")
        transaction.cleanup.side_effect = lambda: events.append("output-cleanup")
        executor = mock.Mock()
        executor.cleanup.side_effect = lambda: events.append("runtime-cleanup")
        controller = mock.Mock()
        controller.execute.side_effect = lambda *_args: (
            events.append("profiles") or {"status": "PASS"}
        )
        verifier = mock.Mock()
        verifier.verify.side_effect = lambda _request: (
            events.append("preflight") or authority
        )
        provider = mock.Mock()
        provider.execution_authority.return_value = nullcontext(mock.Mock())
        with (
            mock.patch.object(
                QualifiedCandidateFormalAuthority,
                "issue_request",
                return_value=request,
            ),
            mock.patch.object(
                harness, "ProductionFormalAuthorityVerifier", return_value=verifier
            ),
            mock.patch.object(
                harness, "_FormalOutputTransaction", return_value=transaction
            ),
            mock.patch.object(
                harness, "ClosedFormalVmProfileExecutor", return_value=executor
            ),
            mock.patch.object(harness, "FormalVmController", return_value=controller),
        ):
            result = harness.execute_qualified_formal_production(
                qualified_candidate=qualified,
                publication_identity=request.publication_identity,
                attestation_claim_identities=(request.attestation_claim_identities),
                provenance_inputs=(),
                publication_input=mock.Mock(),
                execution=execution,
                publication_root=Path("publication-root"),
                private_work_root=Path("private-root"),
                output_root=Path("output-root"),
                provider=provider,
            )
        self.assertEqual(result, {"status": "PASS"})
        self.assertEqual(
            events,
            [
                "preflight",
                "profiles",
                "commit",
                "runtime-cleanup",
                "output-cleanup",
            ],
        )

    @staticmethod
    def request(
        *,
        source_sha: str = "1" * 40,
        source_tree: str = "2" * 40,
        installer_materials_identity: str = "sha256:" + "5" * 64,
    ):
        return FormalAuthorityRequest(
            repository="yanyuhanyue/AniMemo",
            rc_tag="v1.1.0-rc.19",
            verified_candidate_digest="sha256:" + "0" * 64,
            source_sha=source_sha,
            source_tree=source_tree,
            release_manifest_identity="sha256:" + "3" * 64,
            deployment_contract_identity="sha256:" + "4" * 64,
            installer_materials_identity=installer_materials_identity,
            formal_windows_pretrust_kit_identity="sha256:" + "0" * 64,
            offline_release_trust_profile_identity="sha256:" + "1" * 64,
            api_digest="sha256:" + "6" * 64,
            web_digest="sha256:" + "7" * 64,
            publication_identity="sha256:" + "8" * 64,
            workflow_identity=(
                "yanyuhanyue/AniMemo/.github/workflows/release.yml@refs/heads/main"
            ),
            attestation_claim_identities={
                "api-image": "sha256:" + "9" * 64,
                "web-image": "sha256:" + "a" * 64,
                "release-manifest": "sha256:" + "b" * 64,
                "deployment-contract": "sha256:" + "c" * 64,
                "installer-materials": "sha256:" + "d" * 64,
            },
        )

    @classmethod
    def authority(cls, **kwargs) -> VerifiedFormalRcAuthority:
        request = cls.request(**kwargs)
        return VerifiedFormalRcAuthority.issue(
            request,
            provenance_preflight_digest="sha256:" + "0" * 64,
            actions_preflight_receipt_digest="sha256:" + "1" * 64,
            provenance_claim_summaries={
                name: {
                    "claim_digest": digest,
                    "bundle_digest": "sha256:" + "2" * 64,
                    "trusted_root_digest": "sha256:" + "3" * 64,
                    "request_digest": "sha256:" + "4" * 64,
                }
                for name, digest in request.attestation_claim_identities.items()
            },
            publication_preflight_summary={
                "verifier_digest": "sha256:" + "5" * 64,
                "bundle_digest": "sha256:" + "6" * 64,
                "trusted_root_digest": "sha256:" + "7" * 64,
                "request_digest": "sha256:" + "8" * 64,
                "claim_digest": "sha256:" + "9" * 64,
            },
            pretrusted_profile_identity="sha256:" + "a" * 64,
            provenance_verifier_identity="sha256:" + "5" * 64,
            github_trusted_root_identity="sha256:" + "7" * 64,
            sigstore_trusted_root_identity="sha256:" + "3" * 64,
            publication_execution_receipt_identity="sha256:" + "b" * 64,
            publication_signed_claim_identity="sha256:" + "9" * 64,
            publication_signed_at="2026-08-29T23:59:59Z",
            candidate_aggregate_receipt_digest="sha256:" + "6" * 64,
            candidate_profile_receipt_digests={
                key: "sha256:" + value * 64
                for key, value in zip(CANDIDATE_PROFILE_RESULT_KEYS, "789", strict=True)
            },
            **_candidate_source_evidence(),
        )

    @staticmethod
    def stage_authority(root: Path, authority: VerifiedFormalRcAuthority) -> None:
        (root / "formal-rc-authority.json").write_bytes(
            canonical_json_bytes(
                {**authority.identity_body(), "identity": authority.identity}
            )
        )

    @staticmethod
    def pretrusted_root(root: Path) -> Path:
        return create_test_formal_windows_pretrust_kit(root)

    @staticmethod
    def production_verifier(plan: FormalProvenancePlan):
        if plan.qualified_candidate is None:
            qualified = _issue_qualified_candidate_formal_authority(
                loaded=SimpleNamespace(
                    root=Path(plan.installer_materials).parent,
                    verified_digest="sha256:" + "0" * 64,
                ),
                formal_windows_pretrust_root=Path(plan.pretrusted_trust_material_root),
                candidate_aggregate_receipt_digest="sha256:" + "6" * 64,
                candidate_profile_receipt_digests={
                    key: "sha256:" + value * 64
                    for key, value in zip(
                        CANDIDATE_PROFILE_RESULT_KEYS, "789", strict=True
                    )
                },
                **_qualified_source_evidence(),
            )
            plan = replace(
                plan,
                pretrusted_trust_material_root=None,
                installer_materials=None,
                qualified_candidate=qualified,
            )
        with (
            mock.patch("release.formal_windows_pretrust.assert_windows_private_acl"),
            mock.patch("release.formal_vm_controller.assert_windows_private_acl"),
        ):
            return ProductionFormalAuthorityVerifier(plan)

    @staticmethod
    def provenance_input(root: Path, name: str, *, trusted_root=None):
        bundle = root / f"{name}.bundle.json"
        request = root / f"{name}.request.json"
        bundle.write_bytes(b"{}")
        request.write_bytes(b"{}")
        return FormalProvenanceInput(name, bundle, trusted_root, request)

    def test_production_verifier_requires_single_pretrusted_material_authority(self):
        with tempfile.TemporaryDirectory() as directory:
            root = harness.create_windows_private_directory(
                Path(directory), prefix="formal-runtime-test"
            )
            material = self.pretrusted_root(root)
            action = self.provenance_input(root, "api-image")
            publication = self.provenance_input(root, "github-release")
            self.production_verifier(
                FormalProvenancePlan(
                    verifier=None,
                    inputs=(action,),
                    publication=publication,
                    pretrusted_trust_material_root=material,
                    installer_materials=root / "installer-materials.tar",
                    private_work_root=root,
                )
            )
            for plan in (
                FormalProvenancePlan(
                    verifier=material / "offline-release-verifier",
                    inputs=(action,),
                    publication=publication,
                    pretrusted_trust_material_root=material,
                    installer_materials=root / "installer-materials.tar",
                    private_work_root=root,
                ),
                FormalProvenancePlan(
                    verifier=None,
                    inputs=(
                        self.provenance_input(
                            root,
                            "web-image",
                            trusted_root=material / "sigstore-trusted-root.jsonl",
                        ),
                    ),
                    publication=publication,
                    pretrusted_trust_material_root=material,
                    installer_materials=root / "installer-materials.tar",
                    private_work_root=root,
                ),
                FormalProvenancePlan(
                    verifier=None,
                    inputs=(action,),
                    publication=FormalProvenanceInput(
                        publication.evidence_name,
                        publication.bundle,
                        material / "github-trusted-root.jsonl",
                        publication.request,
                    ),
                    pretrusted_trust_material_root=material,
                    installer_materials=root / "installer-materials.tar",
                    private_work_root=root,
                ),
            ):
                with self.assertRaisesRegex(
                    FormalProducerError, "FORMAL_PRETRUSTED_MATERIAL_REBOUND"
                ):
                    self.production_verifier(plan)

    def test_pretrusted_material_rejects_fake_verifier_and_root_swap(self):
        for changed in (
            "offline-release-verifier",
            "github-trusted-root.jsonl",
        ):
            with (
                self.subTest(changed=changed),
                tempfile.TemporaryDirectory() as directory,
            ):
                root = Path(directory)
                material = self.pretrusted_root(root)
                if changed == "github-trusted-root.jsonl":
                    github = material / changed
                    sigstore = material / "sigstore-trusted-root.jsonl"
                    github_bytes = github.read_bytes()
                    github.write_bytes(sigstore.read_bytes())
                    sigstore.write_bytes(github_bytes)
                else:
                    (material / changed).write_bytes(b"fake-verifier\n")
                with self.assertRaisesRegex(
                    FormalProducerError, "FORMAL_PRETRUSTED_MATERIAL_INVALID"
                ):
                    self.production_verifier(
                        FormalProvenancePlan(
                            verifier=None,
                            inputs=(self.provenance_input(root, "api-image"),),
                            pretrusted_trust_material_root=material,
                            installer_materials=root / "installer-materials.tar",
                            private_work_root=root,
                        )
                    )

    def test_cli_has_no_operator_authority_or_trust_override(self):
        options = harness._parser()._option_string_actions
        for forbidden in (
            "--authority-root",
            "--pretrusted-trust-material-root",
            "--installer-materials",
            "--provenance-verifier",
            "--github-release-publication-trusted-root",
            "--api-image-trusted-root",
            "--web-image-trusted-root",
        ):
            self.assertNotIn(forbidden, options)

    def test_cli_execute_requires_in_memory_parent_capability(self):
        with mock.patch("sys.stderr", new=io.StringIO()) as error:
            self.assertEqual(harness.main(["--execute"]), 2)
        self.assertEqual(
            json.loads(error.getvalue()),
            {"code": "FORMAL_PARENT_WORKER_CAPABILITY_REQUIRED"},
        )

    def test_closed_runtime_inventory_has_one_20gib_total_byte_contract(self):
        ceiling = 20 * 1024 * 1024 * 1024
        self.assertEqual(runtime_inventory_contract.MAXIMUM_TOTAL_BYTES, ceiling)
        self.assertEqual(
            provider_contract.CLOSED_RUNTIME_MAXIMUM_TOTAL_BYTES,
            ceiling,
        )
        self.assertEqual(
            runtime_inventory_contract.closed_runtime_total_bytes(
                ceiling - 1,
                1,
            ),
            ceiling,
        )
        with self.assertRaises(ValueError):
            runtime_inventory_contract.closed_runtime_total_bytes(ceiling, 1)
        self.assertNotIn(
            "maximum_total_bytes",
            inspect.signature(
                provider_contract._closed_runtime_inventory_digest
            ).parameters,
        )

    def test_closed_runtime_inventory_script_matches_host_canonical_digest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "empty").write_bytes(b"")
            (root / "scripts").mkdir()
            (root / "scripts" / "formal_profile_runner.py").write_bytes(b"pass\n")
            inventory_source = (
                Path(__file__).resolve().parents[2]
                / "scripts"
                / "closed_runtime_inventory.py"
            )
            inventory_program = root / "scripts" / inventory_source.name
            inventory_program.write_bytes(inventory_source.read_bytes())
            host = provider_contract._closed_runtime_inventory_digest(root)
            guest = runtime_inventory_contract.closed_runtime_inventory_digest(
                root
            )
            self.assertEqual(guest, host)
            formal_root = provider_contract.GUEST_FORMAL_ROOT + "/" + "a" * 64
            self.assertEqual(
                provider_contract._guest_runtime_inventory_command(
                    formal_root,
                    material_root=formal_root,
                ),
                "/usr/bin/python3 -P -B "
                + formal_root
                + "/scripts/closed_runtime_inventory.py "
                + formal_root,
            )
            candidate_root = (
                provider_contract.GUEST_CANDIDATE_ROOT + "/" + "b" * 64
            )
            self.assertEqual(
                provider_contract._guest_runtime_inventory_command(
                    candidate_root,
                    material_root=candidate_root + "/installer-root",
                ),
                "/usr/bin/python3 -P -B "
                + candidate_root
                + "/installer-root/scripts/closed_runtime_inventory.py "
                + candidate_root,
            )

    def test_snapshot_uses_exact_attested_installer_not_caller_runtime(self):
        with tempfile.TemporaryDirectory() as directory:
            root = harness.create_windows_private_directory(
                Path(directory), prefix="formal-runtime-test"
            )
            installer_materials = root / "installer-materials.tar"
            inventory_program = (
                Path(__file__).resolve().parents[2]
                / "scripts"
                / "closed_runtime_inventory.py"
            ).read_bytes()
            members = {
                "installer/production.py": b"RESULT = 'production'\n",
                "scripts/closed_runtime_inventory.py": inventory_program,
                "scripts/formal_profile_runner.py": b"TRUSTED_RUNNER = True\n",
            }
            with tarfile.open(
                installer_materials, "w:", format=tarfile.USTAR_FORMAT
            ) as archive:
                for name, value in sorted(members.items()):
                    info = tarfile.TarInfo(name)
                    info.size = len(value)
                    info.mode = 0o644
                    info.mtime = 0
                    info.uid = 0
                    info.gid = 0
                    info.uname = ""
                    info.gname = ""
                    archive.addfile(info, io.BytesIO(value))
            materials_identity = (
                "sha256:" + hashlib.sha256(installer_materials.read_bytes()).hexdigest()
            )
            authority = self.authority(installer_materials_identity=materials_identity)
            authority_root = root / "authority"
            authority_root.mkdir()
            self.stage_authority(authority_root, authority)
            (authority_root / "installer").mkdir()
            (authority_root / "installer" / "production.py").write_bytes(
                b"RESULT = 'forged-pass'\n"
            )
            temporary, snapshot = harness._prepare_runtime_snapshot(
                authority_root,
                authority,
                installer_materials=installer_materials,
                private_work_root=root,
            )
            try:
                self.assertEqual(
                    (snapshot / "installer" / "production.py").read_bytes(),
                    b"RESULT = 'production'\n",
                )
                self.assertEqual(
                    (snapshot / "scripts" / "formal_profile_runner.py").read_bytes(),
                    b"TRUSTED_RUNNER = True\n",
                )
                self.assertEqual(
                    (snapshot / "scripts" / "closed_runtime_inventory.py").read_bytes(),
                    inventory_program,
                )
                self.assertNotIn(
                    b"forged-pass",
                    (snapshot / "installer" / "production.py").read_bytes(),
                )
            finally:
                temporary.cleanup()
            wrong = self.authority(installer_materials_identity="sha256:" + "f" * 64)
            with self.assertRaisesRegex(
                FormalProducerError, "FORMAL_RUNTIME_SOURCE_IDENTITY_MISMATCH"
            ):
                harness._prepare_runtime_snapshot(
                    authority_root,
                    wrong,
                    installer_materials=installer_materials,
                    private_work_root=root,
                )

    def test_snapshot_requires_closed_inventory_from_attested_installer(self):
        with tempfile.TemporaryDirectory() as directory:
            root = harness.create_windows_private_directory(
                Path(directory), prefix="formal-runtime-missing-inventory"
            )
            installer_materials = root / "installer-materials.tar"
            members = {
                "installer/production.py": b"RESULT = 'production'\n",
                "scripts/formal_profile_runner.py": b"TRUSTED_RUNNER = True\n",
            }
            with tarfile.open(
                installer_materials, "w:", format=tarfile.USTAR_FORMAT
            ) as archive:
                for name, value in sorted(members.items()):
                    info = tarfile.TarInfo(name)
                    info.size = len(value)
                    info.mode = 0o644
                    info.mtime = 0
                    info.uid = 0
                    info.gid = 0
                    info.uname = ""
                    info.gname = ""
                    archive.addfile(info, io.BytesIO(value))
            materials_identity = (
                "sha256:" + hashlib.sha256(installer_materials.read_bytes()).hexdigest()
            )
            authority = self.authority(
                installer_materials_identity=materials_identity
            )
            with self.assertRaisesRegex(
                FormalProducerError, "FORMAL_RUNTIME_SOURCE_IDENTITY_MISMATCH"
            ):
                harness._prepare_runtime_snapshot(
                    root,
                    authority,
                    installer_materials=installer_materials,
                    private_work_root=root,
                )

    def test_workload_rejects_runner_replacement_after_issue(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "scripts").mkdir()
            runner = root / "scripts" / "formal_profile_runner.py"
            runner.write_bytes(b"pass\n")
            (root / "scripts" / "closed_runtime_inventory.py").write_bytes(
                (
                    Path(__file__).resolve().parents[2]
                    / "scripts"
                    / "closed_runtime_inventory.py"
                ).read_bytes()
            )
            (root / "formal-rc-authority.json").write_bytes(b"{}\n")
            workload = provider_contract.ClosedFormalProfileWorkload.issue(
                authority_root=root,
                authority_identity="sha256:" + "1" * 64,
                formal_profile="FORMAL_FRESH",
                runtime_source_tree="2" * 40,
            )
            runner.write_bytes(b"raise SystemExit(0)\n")
            with self.assertRaisesRegex(
                provider_contract.CandidateHarnessError,
                "FORMAL_VM_(WORKLOAD_INVALID|RUNTIME_INVENTORY_INVALID)",
            ):
                provider_contract.ClosedVmwareProvider._validate_formal_workload(
                    workload
                )

    def test_provenance_failure_precedes_host_provider_interaction(self):
        events: list[str] = []

        class RejectingVerifier:
            def verify(self, request):
                del request
                events.append("preflight")
                raise ProvenancePreflightError("FORMAL_PROVENANCE_VERIFICATION_FAILED")

        class Provider:
            def __getattr__(self, name):
                events.append("provider:" + name)
                raise AssertionError("provider must remain unreachable")

        executor = harness.ClosedFormalVmProfileExecutor(
            authority_root=Path("unreachable"), provider=Provider()
        )
        with self.assertRaises(ProvenancePreflightError):
            FormalVmController(
                authority_verifier=RejectingVerifier(), profile_executor=executor
            ).execute(
                self.request(),
                FormalExecutionContext(
                    accepted_at="2026-08-30T01:02:03Z",
                    observed_at="2026-08-30T01:01:59Z",
                    operator_identity="formal-reviewer",
                    run_id="formal-run-1",
                    run_attempt=1,
                    correlation_id="formal-correlation-1",
                    current_workflow_commit="e" * 40,
                    execution_environment="windows-vmware-private",
                    tool_identity="sha256:" + "f" * 64,
                ),
            )
        self.assertEqual(events, ["preflight"])

    def test_provider_plan_is_neutral_to_candidate_and_r2(self):
        class Provider:
            def __init__(self):
                self.authority = FormalVmHarnessTests.authority()

            def inspect_readiness(self):
                return provider_contract.ProviderReadinessReceipt.issue(
                    ssh_digest=provider_contract.EXPECTED_SSH_SHA256,
                    scp_digest=provider_contract.EXPECTED_SCP_SHA256,
                )

            def inspect_source(self):
                return provider_contract.SourceVmEvidence(
                    vm_identity=provider_contract.SOURCE_VM_IDENTITY,
                    snapshot_identities={
                        **self.authority.candidate_snapshot_identities
                    },
                    snapshot_disk_graph_identities={
                        **self.authority.candidate_snapshot_disk_graph_identities
                    },
                    source_disk_graph_identity=(
                        self.authority.candidate_source_disk_graph_identity
                    ),
                    source_vm_inventory_identity=(
                        self.authority.candidate_source_vm_inventory_identity
                    ),
                    original_hashes=self.authority.candidate_original_vm_hashes,
                )

        plan = harness._provider_plan(self.authority(), Provider())
        encoded = json.dumps(plan.as_dict(), sort_keys=True).lower()
        self.assertIsInstance(plan, provider_contract.ClosedVmProviderPlan)
        self.assertTrue(
            all(
                isinstance(item, provider_contract.VmProviderProfilePlan)
                for item in plan.profiles
            )
        )
        for forbidden in ("qualification", "r2", "installerprofile", "candidate"):
            self.assertNotIn(forbidden, encoded)

    def test_closed_formal_executor_calls_neutral_provider_and_maps_observation(self):
        authority = self.authority()
        originals = {"source.vmx": "sha256:" + "1" * 64}
        profile_plan = provider_contract.VmProviderProfilePlan(
            profile="FRESH_BASE",
            snapshot_name=provider_contract.SNAPSHOT_ALLOWLIST["FRESH_BASE"],
            snapshot_identity="sha256:" + "2" * 64,
            snapshot_disk_graph_identity="sha256:" + "3" * 64,
            clone_identity="sha256:" + "4" * 64,
            provider_readiness_receipt_digest="sha256:" + "5" * 64,
            session_id="a" * 32,
            connection_nonce="b" * 64,
            ssh_host_key_alias="animemo-formal-fixture",
        )
        plan = provider_contract.ClosedVmProviderPlan(
            purpose="FORMAL_POSTPUBLICATION",
            authority_digest=authority.identity,
            source_sha=authority.source_sha,
            source_tree=authority.source_tree,
            target_version=authority.rc_tag,
            source_vm_identity=provider_contract.SOURCE_VM_IDENTITY,
            source_vm_digest="sha256:" + "6" * 64,
            source_disk_graph_identity="sha256:" + "7" * 64,
            source_vm_inventory_identity="sha256:" + "9" * 64,
            original_vm_hashes=originals,
            profiles=(profile_plan,),
            provider_readiness_receipt_digest="sha256:" + "5" * 64,
            session_id="a" * 32,
            plan_digest="sha256:" + "8" * 64,
        )

        class Provider:
            def __init__(self):
                self.calls: list[tuple[object, object]] = []

            def inspect_execution_authority(self):
                return SimpleNamespace(
                    result="PASS",
                    source_vm_inventory_identity=(
                        plan.source_vm_inventory_identity
                    ),
                    receipt_digest="sha256:" + "1" * 64,
                )

            def execute_formal_profile(
                self, *, plan, harness_plan, workload, initial_platform_state
            ):
                del initial_platform_state
                self.calls.append((plan, harness_plan))
                self.workload = workload
                return {
                    "schema": "animemo.formal-profile-observation-draft/v1",
                    "version": 1,
                    "profile": "FORMAL_FRESH",
                    "rc_authority_identity": authority.identity,
                    "transport_source": "github",
                    "resolved_release": {
                        "version": authority.rc_tag,
                        "source_sha": authority.source_sha,
                        "release_manifest_identity": (
                            authority.release_manifest_identity
                        ),
                        "deployment_contract_identity": (
                            authority.deployment_contract_identity
                        ),
                        "installer_materials_identity": (
                            authority.installer_materials_identity
                        ),
                        "api_digest": authority.api_digest,
                        "web_digest": authority.web_digest,
                        "publication_identity": authority.publication_identity,
                        "workflow_identity": authority.workflow_identity,
                        "attestation_claim_identities": dict(
                            authority.attestation_claim_identities
                        ),
                    },
                    "publication_execution_receipt_identity": (
                        authority.publication_execution_receipt_identity
                    ),
                    "publication_signed_claim_identity": (
                        authority.publication_signed_claim_identity
                    ),
                    "publication_signed_at": authority.publication_signed_at,
                    "formal_windows_pretrust_kit_identity": (
                        authority.formal_windows_pretrust_kit_identity
                    ),
                    "offline_release_trust_profile_identity": (
                        authority.offline_release_trust_profile_identity
                    ),
                    "pretrusted_profile_identity": (
                        authority.pretrusted_profile_identity
                    ),
                    "provenance_verifier_identity": (
                        authority.provenance_verifier_identity
                    ),
                    "github_trusted_root_identity": (
                        authority.github_trusted_root_identity
                    ),
                    "sigstore_trusted_root_identity": (
                        authority.sigstore_trusted_root_identity
                    ),
                    "platform_plan_digest": "sha256:" + "9" * 64,
                    "platform_receipt_digest": "sha256:" + "a" * 64,
                    "installer_plan_digest": "sha256:" + "b" * 64,
                    "installer_execution_receipt_digest": "sha256:" + "c" * 64,
                    "doctor_receipt_digest": "sha256:" + "d" * 64,
                    "canonical_acceptance_receipt_digests": [
                        "sha256:" + character * 64 for character in "ef0"
                    ],
                    "release_authority_granted": False,
                    "publish_authorized": False,
                    "result": "PASS",
                }

            def inspect_original_hashes(self):
                return dict(originals)

            def inspect_profile_continuation(self, *, plan, harness_plan):
                return provider_contract.ProfileContinuationReceipt.issue(
                    profile=plan.profile,
                    session_id=harness_plan.session_id,
                    original_vm_hashes=originals,
                    active_profile_root_count=0,
                    session_private_key_count=0,
                    known_hosts_file_count=0,
                    running_vm_count=0,
                    quarantine_present=False,
                    continuation_safe=True,
                )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "scripts").mkdir()
            (root / "scripts" / "formal_profile_runner.py").write_bytes(b"pass\n")
            self.stage_authority(root, authority)
            provider = Provider()
            executor = harness.ClosedFormalVmProfileExecutor(
                authority_root=root, provider=provider
            )
            executor._plan = plan
            executor._authority_identity = authority.identity
            executor._staging_root = root
            observation = executor.execute(authority=authority, profile="FORMAL_FRESH")
        self.assertEqual(provider.calls, [(profile_plan, plan)])
        self.assertIsInstance(
            provider.workload, provider_contract.ClosedFormalProfileWorkload
        )
        self.assertEqual(observation.platform_plan_digest, "sha256:" + "9" * 64)
        self.assertEqual(observation.platform_receipt_digest, "sha256:" + "a" * 64)

    def test_output_transaction_is_atomic_and_exact_retry_is_idempotent(self):
        request = self.request()
        execution = FormalExecutionContext(
            accepted_at="2026-08-30T01:02:03Z",
            observed_at="2026-08-30T01:01:59Z",
            operator_identity="formal-reviewer",
            run_id="formal-run-1",
            run_attempt=1,
            correlation_id="formal-correlation-1",
            current_workflow_commit="e" * 40,
            execution_environment="windows-vmware-private",
            tool_identity="sha256:" + "f" * 64,
        )
        record = build_test_formal_acceptance(
            rc_tag=request.rc_tag,
            rc_commit=request.source_sha,
            rc_tree=request.source_tree,
            release_manifest_identity=request.release_manifest_identity,
            deployment_contract_identity=request.deployment_contract_identity,
            installer_materials_identity=request.installer_materials_identity,
            api_digest=request.api_digest,
            web_digest=request.web_digest,
            fresh_base_identity="sha256:" + "2" * 64,
            docker_base_identity="sha256:" + "3" * 64,
            runtime_base_identity="sha256:" + "4" * 64,
            accepted_at=execution.accepted_at,
            observed_at=execution.observed_at,
            operator_identity=execution.operator_identity,
            run_id=execution.run_id,
            run_attempt=execution.run_attempt,
            correlation_id=execution.correlation_id,
            current_workflow_commit=execution.current_workflow_commit,
            execution_environment=execution.execution_environment,
            tool_identity=execution.tool_identity,
        )
        evidence = record["formal_evidence"]
        result = {
            "status": "PASS",
            "profileReceipts": evidence["profileReceipts"],
            "aggregateReceipt": evidence["aggregateReceipt"],
            "executionReceipt": evidence["executionReceipt"],
            "rcLiveAcceptanceInput": evidence["rcLiveAcceptanceInput"],
            "rcLiveAcceptanceRecord": record,
        }
        candidate_execution = evidence["executionReceipt"]
        transaction_kwargs = {
            "candidate_aggregate_receipt_digest": candidate_execution[
                "candidate_aggregate_receipt_digest"
            ],
            "candidate_profile_receipt_digests": candidate_execution[
                "candidate_profile_receipt_digests"
            ],
            "candidate_source_vm_authority_identity": candidate_execution[
                "candidate_source_vm_authority_identity"
            ],
            "candidate_material_authority_identity": candidate_execution[
                "candidate_material_authority_identity"
            ],
            "candidate_material_tree_inventory_identity": candidate_execution[
                "candidate_material_tree_inventory_identity"
            ],
        }
        with tempfile.TemporaryDirectory() as directory:
            boundary = harness.create_windows_private_directory(
                Path(directory), prefix="formal-output-test"
            )
            output = boundary / "formal-output"
            transaction = harness._FormalOutputTransaction(
                output, request=request, execution=execution, **transaction_kwargs
            )
            self.assertFalse(output.exists())
            self.assertIsNotNone(transaction.staging)
            transaction.commit(result)
            self.assertTrue(output.is_dir())
            self.assertIsNone(transaction.staging)
            retry = harness._FormalOutputTransaction(
                output, request=request, execution=execution, **transaction_kwargs
            )
            self.assertTrue(retry.reused)
            self.assertEqual(retry.existing_status, "PASS")
            retry.cleanup()
            (output / "formal-execution-receipt.json").write_bytes(b"{}\n")
            with self.assertRaisesRegex(
                FormalProducerError, "FORMAL_OUTPUT_ROOT_INVALID"
            ):
                harness._FormalOutputTransaction(
                    output, request=request, execution=execution, **transaction_kwargs
                )

    def test_output_transaction_rejects_unavailable_parent_before_provider(self):
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "missing" / "output"
            with self.assertRaisesRegex(
                FormalProducerError, "FORMAL_OUTPUT_ROOT_INVALID"
            ):
                harness._FormalOutputTransaction(
                    missing,
                    request=self.request(),
                    execution=FormalExecutionContext(
                        accepted_at="2026-08-30T01:02:03Z",
                        observed_at="2026-08-30T01:01:59Z",
                        operator_identity="formal-reviewer",
                        run_id="formal-run-1",
                        run_attempt=1,
                        correlation_id="formal-correlation-1",
                        current_workflow_commit="e" * 40,
                        execution_environment="windows-vmware-private",
                        tool_identity="sha256:" + "f" * 64,
                    ),
                    candidate_aggregate_receipt_digest="sha256:" + "1" * 64,
                    candidate_profile_receipt_digests={
                        "fresh_base": "sha256:" + "2" * 64,
                        "docker_base": "sha256:" + "3" * 64,
                        "runtime_base_offline": "sha256:" + "4" * 64,
                    },
                    candidate_source_vm_authority_identity=(
                        "sha256:" + "5" * 64
                    ),
                    candidate_material_authority_identity=(
                        "sha256:" + "6" * 64
                    ),
                    candidate_material_tree_inventory_identity=(
                        "sha256:" + "7" * 64
                    ),
                )

    def test_output_transaction_persists_shared_blocker_and_reuses_exact_failure(self):
        request = self.request()
        authority = self.authority()
        execution = FormalExecutionContext(
            accepted_at="2026-08-30T01:02:03Z",
            observed_at="2026-08-30T01:01:59Z",
            operator_identity="formal-reviewer",
            run_id="formal-run-failure",
            run_attempt=1,
            correlation_id="formal-correlation-failure",
            current_workflow_commit="e" * 40,
            execution_environment="windows-vmware-private",
            tool_identity="sha256:" + "f" * 64,
        )

        class Verifier:
            def verify(self, _request):
                return authority

        class Executor:
            def execute(self, **_kwargs):
                raise FormalProducerError("FORMAL_VM_PROVIDER_FAILED")

        result = FormalVmController(
            authority_verifier=Verifier(), profile_executor=Executor()
        ).execute(request, execution)
        self.assertEqual(result["status"], "FAIL")
        self.assertIsNone(result["rcLiveAcceptanceInput"])
        self.assertIsNone(result["rcLiveAcceptanceRecord"])
        transaction_kwargs = {
            "candidate_aggregate_receipt_digest": (
                authority.candidate_aggregate_receipt_digest
            ),
            "candidate_profile_receipt_digests": (
                authority.candidate_profile_receipt_digests
            ),
            "candidate_source_vm_authority_identity": (
                authority.candidate_source_vm_authority_identity
            ),
            "candidate_material_authority_identity": (
                authority.candidate_material_authority_identity
            ),
            "candidate_material_tree_inventory_identity": (
                authority.candidate_material_tree_inventory_identity
            ),
        }
        with tempfile.TemporaryDirectory() as directory:
            boundary = harness.create_windows_private_directory(
                Path(directory), prefix="formal-output-failure-test"
            )
            output = boundary / "formal-output"
            transaction = harness._FormalOutputTransaction(
                output,
                request=request,
                execution=execution,
                **transaction_kwargs,
            )
            transaction.commit(result)
            self.assertEqual(
                {item.name for item in output.iterdir()},
                {
                    "formal-aggregate-receipt.json",
                    "formal-execution-receipt.json",
                    "formal-fresh-receipt.json",
                    "formal-docker-receipt.json",
                    "formal-offline-receipt.json",
                },
            )
            retry = harness._FormalOutputTransaction(
                output,
                request=request,
                execution=execution,
                **transaction_kwargs,
            )
            self.assertTrue(retry.reused)
            self.assertEqual(retry.existing_status, "FAIL")
            retry.cleanup()

    def test_output_transaction_persists_controlled_profile_failure_receipts(self):
        request = self.request()
        authority = self.authority()
        execution = FormalExecutionContext(
            accepted_at="2026-08-30T01:02:03Z",
            observed_at="2026-08-30T01:01:59Z",
            operator_identity="formal-reviewer",
            run_id="formal-run-controlled-failure",
            run_attempt=1,
            correlation_id="formal-correlation-controlled-failure",
            current_workflow_commit="e" * 40,
            execution_environment="windows-vmware-private",
            tool_identity="sha256:" + "f" * 64,
        )

        class Verifier:
            def verify(self, _request):
                return authority

        class Executor:
            def execute(self, *, authority, profile):
                return _formal_observation(
                    authority,
                    profile,
                    result="FAIL" if profile == "FORMAL_DOCKER" else "PASS",
                )

        result = FormalVmController(
            authority_verifier=Verifier(), profile_executor=Executor()
        ).execute(request, execution)
        transaction_kwargs = {
            "candidate_aggregate_receipt_digest": (
                authority.candidate_aggregate_receipt_digest
            ),
            "candidate_profile_receipt_digests": (
                authority.candidate_profile_receipt_digests
            ),
            "candidate_source_vm_authority_identity": (
                authority.candidate_source_vm_authority_identity
            ),
            "candidate_material_authority_identity": (
                authority.candidate_material_authority_identity
            ),
            "candidate_material_tree_inventory_identity": (
                authority.candidate_material_tree_inventory_identity
            ),
        }
        with tempfile.TemporaryDirectory() as directory:
            boundary = harness.create_windows_private_directory(
                Path(directory), prefix="formal-output-controlled-failure-test"
            )
            output = boundary / "formal-output"
            transaction = harness._FormalOutputTransaction(
                output,
                request=request,
                execution=execution,
                **transaction_kwargs,
            )
            transaction.commit(result)
            self.assertEqual(
                {item.name for item in output.iterdir()},
                {
                    "formal-aggregate-receipt.json",
                    "formal-execution-receipt.json",
                    "formal-fresh-receipt.json",
                    "formal-docker-receipt.json",
                    "formal-offline-receipt.json",
                },
            )
            retry = harness._FormalOutputTransaction(
                output,
                request=request,
                execution=execution,
                **transaction_kwargs,
            )
            self.assertTrue(retry.reused)
            retry.cleanup()


if __name__ == "__main__":
    unittest.main()
