from __future__ import annotations

import copy
import unittest

from release.candidate import (
    CandidateContractError,
    canonical_json_bytes,
    sha256_bytes,
    validate_aggregate_receipt,
)


def _profile(status: str, suffix: str) -> dict[str, object]:
    if status == "PASS":
        return {
            "status": status,
            "failure_code": None,
            "receipt_digest": "sha256:" + suffix * 64,
        }
    return {
        "status": status,
        "failure_code": "CANDIDATE_PROFILE_TEST_FAILURE",
        "receipt_digest": (
            "sha256:" + suffix * 64 if status == "FAIL" else None
        ),
    }


def _receipt() -> dict[str, object]:
    state = {
        "tag": "ABSENT",
        "github_release": "ABSENT",
        "ghcr": "ABSENT",
        "public_r2": "ABSENT_BY_PUBLIC_READBACK_NON_AUTHORITATIVE",
        "r2_origin": "PROVEN_EMPTY",
    }
    original_vm_hashes = {"source.vmx": "sha256:" + "8" * 64}
    value: dict[str, object] = {
        "schema": "animemo.prepublication-candidate-acceptance-receipt/v3",
        "version": 3,
        "candidate_input_digest": "sha256:" + "1" * 64,
        "verified_candidate_digest": "sha256:" + "2" * 64,
        "qualification_run_id": 1234,
        "qualification_run_attempt": 1,
        "source_sha": "a" * 40,
        "source_tree": "b" * 40,
        "candidate_version": "v1.1.0-rc.19",
        "r2_origin_prestate_receipt_digest": "sha256:" + "6" * 64,
        "r2_origin_poststate_receipt_digest": "sha256:" + "7" * 64,
        "r2_origin_prestate_observation_id": (
            "12345678-1234-4678-9234-567812345678"
        ),
        "r2_origin_poststate_observation_id": (
            "87654321-4321-4765-8abc-876543210fed"
        ),
        "base_vm_identity": sha256_bytes(
            canonical_json_bytes(original_vm_hashes)
        ),
        "source_vm_inventory_identity": "sha256:" + "9" * 64,
        "source_disk_graph_identity": "sha256:" + "a" * 64,
        "original_vm_hashes": original_vm_hashes,
        "snapshot_identities": {
            "FRESH_BASE": "sha256:" + "b" * 64,
            "DOCKER_BASE": "sha256:" + "c" * 64,
            "RUNTIME_BASE_OFFLINE": "sha256:" + "d" * 64,
        },
        "snapshot_disk_graph_identities": {
            "FRESH_BASE": "sha256:" + "e" * 64,
            "DOCKER_BASE": "sha256:" + "f" * 64,
            "RUNTIME_BASE_OFFLINE": "sha256:" + "0" * 64,
        },
        "profile_results": {
            "fresh_base": _profile("PASS", "3"),
            "docker_base": _profile("PASS", "4"),
            "runtime_base_offline": _profile("PASS", "5"),
        },
        "all_profiles_pass": True,
        "candidate_prestate": dict(state),
        "candidate_poststate": dict(state),
        "repository_mutation_count": 0,
        "publication_mutation_count": 0,
        "shared_host_connection_count": 0,
        "secret_sweep": 0,
        "placeholder_sweep": 0,
        "release_authority_granted": False,
        "publish_authorized": False,
        "completed_at": "2026-08-30T00:00:00Z",
        "result": "PASS",
        "receipt_digest": "",
    }
    return _seal(value)


def _seal(value: dict[str, object]) -> dict[str, object]:
    unsigned = dict(value)
    unsigned.pop("receipt_digest", None)
    value["receipt_digest"] = sha256_bytes(canonical_json_bytes(unsigned))
    return value


class CandidateAggregateOutcomeTests(unittest.TestCase):
    def test_all_pass_profiles_close_one_v3_aggregate(self):
        receipt = _receipt()
        validated = validate_aggregate_receipt(receipt)
        self.assertTrue(validated["all_profiles_pass"])
        self.assertEqual(validated["result"], "PASS")
        self.assertFalse(validated["release_authority_granted"])
        self.assertFalse(validated["publish_authorized"])

    def test_profile_fail_is_representable_but_keeps_overall_fail_closed(self):
        receipt = _receipt()
        receipt["profile_results"]["fresh_base"] = _profile("FAIL", "8")
        receipt["all_profiles_pass"] = False
        receipt["result"] = "FAIL"
        _seal(receipt)

        validated = validate_aggregate_receipt(receipt)
        self.assertEqual(
            validated["profile_results"]["fresh_base"]["status"], "FAIL"
        )
        self.assertFalse(validated["all_profiles_pass"])
        self.assertEqual(validated["result"], "FAIL")

    def test_error_and_shared_blocker_require_controlled_code_without_digest(self):
        for status in ("ERROR", "NOT_RUN_SHARED_BLOCKER"):
            receipt = _receipt()
            receipt["profile_results"]["docker_base"] = _profile(status, "8")
            receipt["all_profiles_pass"] = False
            receipt["result"] = "FAIL"
            _seal(receipt)
            with self.subTest(status=status):
                self.assertEqual(
                    validate_aggregate_receipt(receipt)["profile_results"][
                        "docker_base"
                    ]["status"],
                    status,
                )

    def test_status_summary_mismatch_and_receipt_reuse_fail_closed(self):
        inconsistent = _receipt()
        inconsistent["profile_results"]["fresh_base"] = _profile("FAIL", "8")
        _seal(inconsistent)
        with self.assertRaisesRegex(
            CandidateContractError, "CANDIDATE_AGGREGATE_RESULT_MISMATCH"
        ):
            validate_aggregate_receipt(inconsistent)

        reused = _receipt()
        reused["profile_results"]["docker_base"]["receipt_digest"] = reused[
            "profile_results"
        ]["fresh_base"]["receipt_digest"]
        _seal(reused)
        with self.assertRaisesRegex(
            CandidateContractError, "CANDIDATE_PROFILE_RECEIPT_REUSE"
        ):
            validate_aggregate_receipt(reused)

    def test_pass_requires_digest_and_error_forbids_one(self):
        mutations = []
        missing_pass = _receipt()
        missing_pass["profile_results"]["fresh_base"]["receipt_digest"] = None
        mutations.append(missing_pass)

        error_with_digest = _receipt()
        error_with_digest["profile_results"]["fresh_base"] = {
            "status": "ERROR",
            "failure_code": "CANDIDATE_PROFILE_EXECUTION_FAILED",
            "receipt_digest": "sha256:" + "8" * 64,
        }
        error_with_digest["all_profiles_pass"] = False
        error_with_digest["result"] = "FAIL"
        mutations.append(error_with_digest)

        for receipt in mutations:
            _seal(receipt)
            with self.assertRaisesRegex(
                CandidateContractError, "CANDIDATE_ACCEPTANCE_RECEIPT_INVALID"
            ):
                validate_aggregate_receipt(receipt)

    def test_unknown_profile_state_is_rejected(self):
        receipt = _receipt()
        receipt["profile_results"]["fresh_base"]["status"] = "SKIPPED"
        _seal(receipt)
        with self.assertRaises(CandidateContractError):
            validate_aggregate_receipt(copy.deepcopy(receipt))


if __name__ == "__main__":
    unittest.main()
