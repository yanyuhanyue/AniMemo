from __future__ import annotations

import contextlib
import io
import json
import os
import unittest
from unittest import mock

from scripts.ci_classify import classify_paths
from scripts.ci_gate_authority import (
    CI_JOB_GATES,
    CLASSIFIER_OUTPUT_NAMES,
    CURRENT_RELEASE_GRAPH_CONTRACT,
    GATE_NAMES,
    LEGACY_RELEASE_JOB_GATES,
    LEGACY_RELEASE_GRAPH_CONTRACT,
    PRODUCT_GATE_NAMES,
    RELEASE_JOB_GATES,
    SIGNAL_NAMES,
    TRUSTED_LEGACY_WORKFLOW_SHA,
    TRUSTED_PRE_MERGE_WORKFLOW_REF,
    TRUSTED_REPOSITORY,
    GateAuthorityError,
    main,
    validate_gate_authority,
)

SCHEMA_VERSION = "animemo.ci-risk/v1"


def classification(
    risk: str = "STANDARD",
    *,
    force_full: bool = False,
    gates: dict[str, bool] | None = None,
) -> dict[str, object]:
    rank = {"LOW": 1, "STANDARD": 2, "HIGH": 3, "CRITICAL": 4}[risk]
    gate_values = dict.fromkeys(GATE_NAMES, False)
    full_gate = force_full or rank >= 3
    critical_gate = force_full or risk == "CRITICAL"
    gate_values.update(
        {
            "run_release_full": full_gate,
            "run_release_updater": critical_gate,
            "run_release_docker": full_gate,
            "run_release_stateful": full_gate,
            "full_gate": full_gate,
            "critical_gate": critical_gate,
        }
    )
    if full_gate:
        gate_values.update(dict.fromkeys(PRODUCT_GATE_NAMES, True))
    if gates:
        gate_values.update(gates)
    return {
        "schema_version": SCHEMA_VERSION,
        "risk": {"level": risk, "rank": rank, "reasons": []},
        "execution": {"force_full": force_full, "reasons": []},
        "signals": dict.fromkeys(SIGNAL_NAMES, False),
        "gates": gate_values,
        "paths": [],
        "unknown_paths": [],
    }


def classify_job(document: dict[str, object]) -> dict[str, object]:
    risk = document["risk"]
    execution = document["execution"]
    gates = document["gates"]
    assert isinstance(risk, dict)
    assert isinstance(execution, dict)
    assert isinstance(gates, dict)
    outputs = {
        "schema_version": document["schema_version"],
        "risk_level": risk["level"],
        "risk_rank": str(risk["rank"]),
        "execution_force_full": str(execution["force_full"]).lower(),
        "classification_json": json.dumps(document, separators=(",", ":")),
    }
    outputs.update({name: str(gates[name]).lower() for name in GATE_NAMES})
    return {"result": "success", "outputs": outputs}


def real_classify_job(outputs: dict[str, str]) -> dict[str, object]:
    return {
        "result": "success",
        "outputs": {name: outputs[name] for name in CLASSIFIER_OUTPUT_NAMES},
    }


def real_classification_needs(
    outputs: dict[str, str], *, workflow: str, event_name: str
) -> dict[str, object]:
    document = json.loads(outputs["classification_json"])
    if workflow == "ci":
        needs = ci_needs(document, event_name=event_name)
    else:
        needs = release_needs(document, event_name=event_name)
    needs["classify"] = real_classify_job(outputs)
    return needs


def ci_needs(
    document: dict[str, object],
    *,
    event_name: str = "pull_request",
) -> dict[str, object]:
    gates = document["gates"]
    assert isinstance(gates, dict)
    docs_only = gates["docs_only"]
    needs: dict[str, object] = {"classify": classify_job(document)}
    for job, gate in CI_JOB_GATES.items():
        if job == "fast-fail":
            selected = event_name != "push" and not docs_only
        elif job == "docs-only":
            selected = docs_only
        else:
            selected = event_name != "push" and gates[gate]
        needs[job] = {"result": "success" if selected else "skipped"}
    return needs


def release_needs(
    document: dict[str, object],
    *,
    event_name: str = "pull_request",
) -> dict[str, object]:
    gates = document["gates"]
    assert isinstance(gates, dict)
    needs: dict[str, object] = {"classify": classify_job(document)}
    for job, gate in RELEASE_JOB_GATES.items():
        selected = (
            event_name == "push"
            if gate is None
            else (event_name != "push" and gates[gate])
        )
        needs[job] = {"result": "success" if selected else "skipped"}
    return needs


def validate_current_release(
    needs: dict[str, object], *, event_name: str
) -> dict[str, object]:
    return validate_gate_authority(
        needs,
        workflow="release",
        event_name=event_name,
        release_graph_contract=CURRENT_RELEASE_GRAPH_CONTRACT,
    )


class CiGateAuthorityTests(unittest.TestCase):
    def assert_rejected(self, message: str, callback) -> None:
        with self.assertRaisesRegex(GateAuthorityError, message):
            callback()

    def test_ci_docs_only_pull_request_selects_only_docs_job(self):
        document = classification("LOW", gates={"docs_only": True})
        result = validate_gate_authority(
            ci_needs(document), workflow="ci", event_name="pull_request"
        )

        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["selected_jobs"], ["docs-only"])

    def test_ci_standard_change_selects_fast_fail_and_affected_jobs(self):
        document = classification(gates={"run_frontend": True, "run_plugins": True})
        result = validate_gate_authority(
            ci_needs(document), workflow="ci", event_name="pull_request"
        )

        self.assertEqual(result["selected_jobs"], ["fast-fail", "frontend", "plugins"])

    def test_ci_critical_gate_requires_every_product_job(self):
        document = classification(
            "CRITICAL",
            gates={
                "run_frontend": True,
                "run_backend": True,
                "run_bootstrap": True,
                "run_plugins": True,
                "run_bridge": True,
                "run_postgres": True,
                "run_runtime": True,
                "run_release_full": True,
                "run_release_updater": True,
                "run_release_docker": True,
                "run_release_stateful": True,
                "full_gate": True,
                "critical_gate": True,
            },
        )
        result = validate_gate_authority(
            ci_needs(document), workflow="ci", event_name="pull_request"
        )

        self.assertEqual(
            set(result["selected_jobs"]),
            {"fast-fail", *set(CI_JOB_GATES) - {"docs-only"}},
        )

    def test_ci_push_allows_only_intrinsic_docs_job(self):
        document = classification("LOW", gates={"docs_only": True})
        result = validate_gate_authority(
            ci_needs(document, event_name="push"),
            workflow="ci",
            event_name="push",
        )

        self.assertEqual(result["selected_jobs"], ["docs-only"])

    def test_ci_product_push_remains_lightweight_even_when_gates_are_true(self):
        document = classification(
            "HIGH",
            gates={
                "run_frontend": True,
                "run_backend": True,
                "run_bootstrap": True,
                "run_plugins": True,
                "run_bridge": True,
                "run_postgres": True,
                "run_runtime": True,
                "run_release_full": True,
                "run_release_docker": True,
                "run_release_stateful": True,
                "full_gate": True,
            },
        )
        result = validate_gate_authority(
            ci_needs(document, event_name="push"),
            workflow="ci",
            event_name="push",
        )

        self.assertEqual(result["selected_jobs"], [])
        self.assertEqual(result["unselected_jobs"], list(CI_JOB_GATES))

    def test_release_push_selects_only_post_merge_sanity(self):
        document = classification("STANDARD")
        result = validate_current_release(
            release_needs(document, event_name="push"), event_name="push"
        )

        self.assertEqual(result["selected_jobs"], ["post-merge-sanity"])

    def test_release_high_risk_uses_per_job_gates(self):
        document = classification(
            "HIGH",
            gates={
                "run_release_full": True,
                "run_release_docker": True,
                "run_release_stateful": True,
                "full_gate": True,
            },
        )
        result = validate_current_release(
            release_needs(document), event_name="pull_request"
        )

        self.assertEqual(result["selected_jobs"], ["docker", "stateful-upgrade", "dr-rehearsal"])
        self.assertIn("updater-isolated", result["unselected_jobs"])
        self.assertIn("post-merge-sanity", result["unselected_jobs"])

    def test_release_critical_risk_selects_all_release_jobs_except_postmerge(self):
        document = classification(
            "CRITICAL",
            gates={
                "run_release_full": True,
                "run_release_updater": True,
                "run_release_docker": True,
                "run_release_stateful": True,
                "full_gate": True,
                "critical_gate": True,
            },
        )
        result = validate_current_release(
            release_needs(document), event_name="pull_request"
        )

        self.assertEqual(
            result["selected_jobs"],
            ["updater-isolated", "docker", "stateful-upgrade", "dr-rehearsal"],
        )

    def test_event_and_workflow_ref_alone_cannot_select_the_legacy_release_graph(self):
        document = classification("STANDARD", force_full=True)
        needs = release_needs(document, event_name="workflow_call")
        needs.pop("dr-rehearsal")

        self.assert_rejected(
            "release graph contract",
            lambda: validate_gate_authority(
                needs,
                workflow="release",
                event_name="workflow_call",
                workflow_ref=TRUSTED_PRE_MERGE_WORKFLOW_REF,
            ),
        )

    def test_historical_dispatch_accepts_only_authenticated_v1_release_graph(self):
        document = classification("STANDARD", force_full=True)
        needs = release_needs(document, event_name="workflow_dispatch")
        needs.pop("dr-rehearsal")

        result = validate_gate_authority(
            needs,
            workflow="release",
            event_name="workflow_dispatch",
            workflow_ref=TRUSTED_PRE_MERGE_WORKFLOW_REF,
            workflow_sha=TRUSTED_LEGACY_WORKFLOW_SHA,
            repository=TRUSTED_REPOSITORY,
            caller_sha=TRUSTED_LEGACY_WORKFLOW_SHA,
        )

        self.assertEqual(set(needs), {"classify", *LEGACY_RELEASE_JOB_GATES})
        self.assertEqual(result["release_graph_contract"], LEGACY_RELEASE_GRAPH_CONTRACT)
        self.assertNotIn("dr-rehearsal", result["selected_jobs"])

        for field, value in (
            ("workflow_ref", "yanyuhanyue/AniMemo/.github/workflows/release.yml@refs/heads/main"),
            ("workflow_sha", "0" * 40),
            ("repository", "fork/AniMemo"),
            ("caller_sha", "0" * 40),
        ):
            with self.subTest(field=field):
                identity = {
                    "workflow_ref": TRUSTED_PRE_MERGE_WORKFLOW_REF,
                    "workflow_sha": TRUSTED_LEGACY_WORKFLOW_SHA,
                    "repository": TRUSTED_REPOSITORY,
                    "caller_sha": TRUSTED_LEGACY_WORKFLOW_SHA,
                }
                identity[field] = value
                self.assert_rejected(
                    "release graph contract",
                    lambda identity=identity: validate_gate_authority(
                        needs,
                        workflow="release",
                        event_name="workflow_dispatch",
                        **identity,
                    ),
                )

        missing_stateful = dict(needs)
        missing_stateful.pop("stateful-upgrade")
        self.assert_rejected(
            "missing keys: stateful-upgrade",
            lambda: validate_gate_authority(
                missing_stateful,
                workflow="release",
                event_name="workflow_dispatch",
                workflow_ref=TRUSTED_PRE_MERGE_WORKFLOW_REF,
                workflow_sha=TRUSTED_LEGACY_WORKFLOW_SHA,
                repository=TRUSTED_REPOSITORY,
                caller_sha=TRUSTED_LEGACY_WORKFLOW_SHA,
            ),
        )

    def test_release_graph_contract_is_explicit_and_fail_closed(self):
        document = classification("STANDARD", force_full=True)
        current = release_needs(document, event_name="workflow_dispatch")
        legacy = dict(current)
        legacy.pop("dr-rehearsal")

        result = validate_gate_authority(
            current,
            workflow="release",
            event_name="workflow_dispatch",
            release_graph_contract=CURRENT_RELEASE_GRAPH_CONTRACT,
        )
        self.assertEqual(result["release_graph_contract"], CURRENT_RELEASE_GRAPH_CONTRACT)

        self.assert_rejected(
            "unsupported release graph contract",
            lambda: validate_gate_authority(
                current,
                workflow="release",
                event_name="workflow_dispatch",
                release_graph_contract="animemo.release-gate.jobs/v999",
            ),
        )
        self.assert_rejected(
            "unsupported release graph contract",
            lambda: validate_gate_authority(
                legacy,
                workflow="release",
                event_name="workflow_dispatch",
                release_graph_contract=LEGACY_RELEASE_GRAPH_CONTRACT,
            ),
        )
        self.assert_rejected(
            "missing keys: dr-rehearsal",
            lambda: validate_gate_authority(
                legacy,
                workflow="release",
                event_name="workflow_dispatch",
                release_graph_contract=CURRENT_RELEASE_GRAPH_CONTRACT,
            ),
        )

    def test_real_classifier_low_standard_high_critical_and_force_full_documents(self):
        cases = (
            ("LOW", ["README.md"], False, "pull_request"),
            ("STANDARD", ["src/pages/Journal.jsx"], False, "pull_request"),
            (
                "HIGH",
                ["backend/journal/migrations/9999_gate_test.py"],
                False,
                "pull_request",
            ),
            ("CRITICAL", ["updater/agent.py"], False, "pull_request"),
            ("LOW", ["README.md"], True, "workflow_call"),
        )
        for expected_risk, paths, force_full, event_name in cases:
            with self.subTest(
                risk=expected_risk, force_full=force_full, event_name=event_name
            ):
                outputs = classify_paths(paths, force_full=force_full)
                for workflow in ("ci", "release"):
                    with self.subTest(workflow=workflow):
                        result = validate_gate_authority(
                            real_classification_needs(
                                outputs, workflow=workflow, event_name=event_name
                            ),
                            workflow=workflow,
                            event_name=event_name,
                            release_graph_contract=(
                                CURRENT_RELEASE_GRAPH_CONTRACT
                                if workflow == "release"
                                else ""
                            ),
                        )
                        self.assertEqual(result["status"], "PASS")
                        self.assertEqual(result["risk_level"], expected_risk)
                        self.assertEqual(result["execution_force_full"], force_full)

    def test_policy_semantics_reject_gate_downgrades_and_escalations(self):
        cases = (
            (
                "full_gate",
                classification("HIGH"),
                lambda gates: gates.__setitem__("full_gate", False),
            ),
            (
                "critical_gate",
                classification("CRITICAL"),
                lambda gates: gates.__setitem__("critical_gate", False),
            ),
            (
                "run_release_full",
                classification("HIGH"),
                lambda gates: gates.__setitem__("run_release_full", False),
            ),
            (
                "run_release_docker",
                classification("HIGH"),
                lambda gates: gates.__setitem__("run_release_docker", False),
            ),
            (
                "run_release_stateful",
                classification("HIGH"),
                lambda gates: gates.__setitem__("run_release_stateful", False),
            ),
            (
                "run_release_updater",
                classification("HIGH"),
                lambda gates: gates.__setitem__("run_release_updater", True),
            ),
            (
                "run_release_updater",
                classification("CRITICAL"),
                lambda gates: gates.__setitem__("run_release_updater", False),
            ),
            (
                "run_backend",
                classification("HIGH"),
                lambda gates: gates.__setitem__("run_backend", False),
            ),
        )
        for message, document, mutate in cases:
            with self.subTest(message=message):
                mutate(document["gates"])
                needs = ci_needs(document)
                self.assert_rejected(
                    message,
                    lambda needs=needs: validate_gate_authority(
                        needs, workflow="ci", event_name="pull_request"
                    ),
                )

    def test_every_full_gate_requires_every_product_gate(self):
        for gate in PRODUCT_GATE_NAMES:
            with self.subTest(gate=gate):
                document = classification("HIGH")
                document["gates"][gate] = False  # type: ignore[index]
                needs = ci_needs(document)
                self.assert_rejected(
                    gate,
                    lambda needs=needs: validate_gate_authority(
                        needs, workflow="ci", event_name="pull_request"
                    ),
                )

    def test_mixed_gate_must_match_primary_signals(self):
        document = classification()
        document["signals"]["frontend"] = True  # type: ignore[index]
        document["signals"]["backend"] = True  # type: ignore[index]
        needs = ci_needs(document)
        self.assert_rejected(
            "mixed",
            lambda: validate_gate_authority(
                needs, workflow="ci", event_name="pull_request"
            ),
        )

        document["gates"]["mixed"] = True  # type: ignore[index]
        needs = ci_needs(document)
        self.assertEqual(
            validate_gate_authority(needs, workflow="ci", event_name="pull_request")[
                "status"
            ],
            "PASS",
        )

    def test_authority_events_require_force_full_and_pull_request_may_force(self):
        for event_name in ("merge_group", "workflow_dispatch", "workflow_call"):
            with self.subTest(event_name=event_name, force_full=False):
                document = classification("STANDARD")
                needs = ci_needs(document, event_name=event_name)
                self.assert_rejected(
                    "requires.*force_full=true",
                    lambda needs=needs, event_name=event_name: validate_gate_authority(
                        needs, workflow="ci", event_name=event_name
                    ),
                )

            with self.subTest(event_name=event_name, force_full=True):
                document = classification("STANDARD", force_full=True)
                needs = ci_needs(document, event_name=event_name)
                self.assertEqual(
                    validate_gate_authority(
                        needs, workflow="ci", event_name=event_name
                    )["status"],
                    "PASS",
                )

        document = classification("STANDARD", force_full=True)
        needs = ci_needs(document)
        self.assertEqual(
            validate_gate_authority(needs, workflow="ci", event_name="pull_request")[
                "status"
            ],
            "PASS",
        )

    def test_push_rejects_force_full_instead_of_taking_lightweight_path(self):
        document = classification("STANDARD", force_full=True)
        needs = ci_needs(document, event_name="push")
        self.assert_rejected(
            "push cannot.*force_full=true",
            lambda: validate_gate_authority(needs, workflow="ci", event_name="push"),
        )

    def test_classify_must_exist_and_succeed(self):
        document = classification()
        missing = ci_needs(document)
        missing.pop("classify")
        self.assert_rejected(
            "missing.*classify",
            lambda: validate_gate_authority(
                missing, workflow="ci", event_name="pull_request"
            ),
        )

        failed = ci_needs(document)
        failed["classify"]["result"] = "failure"  # type: ignore[index]
        self.assert_rejected(
            "classify.*success.*failure",
            lambda: validate_gate_authority(
                failed, workflow="ci", event_name="pull_request"
            ),
        )

    def test_selected_skipped_and_unselected_success_fail_closed(self):
        document = classification(gates={"run_frontend": True})
        selected_skipped = ci_needs(document)
        selected_skipped["frontend"]["result"] = "skipped"  # type: ignore[index]
        self.assert_rejected(
            "frontend.*success.*skipped",
            lambda: validate_gate_authority(
                selected_skipped, workflow="ci", event_name="pull_request"
            ),
        )

        unselected_success = ci_needs(document)
        unselected_success["backend"]["result"] = "success"  # type: ignore[index]
        self.assert_rejected(
            "backend.*skipped.*success",
            lambda: validate_gate_authority(
                unselected_success, workflow="ci", event_name="pull_request"
            ),
        )

    def test_missing_failed_and_cancelled_jobs_fail_closed(self):
        document = classification(gates={"run_frontend": True})
        cases = (
            ("missing", "frontend", None),
            ("failure", "frontend", "failure"),
            ("cancelled", "backend", "cancelled"),
        )
        for label, job, result in cases:
            with self.subTest(label=label):
                needs = ci_needs(document)
                if result is None:
                    needs.pop(job)
                else:
                    needs[job]["result"] = result  # type: ignore[index]
                self.assert_rejected(
                    job,
                    lambda needs=needs: validate_gate_authority(
                        needs, workflow="ci", event_name="pull_request"
                    ),
                )

    def test_schema_risk_rank_and_execution_are_strict(self):
        mutations = (
            ("schema", lambda doc: doc.__setitem__("schema_version", "future/v2")),
            ("risk level", lambda doc: doc["risk"].__setitem__("level", "SEVERE")),
            ("risk rank", lambda doc: doc["risk"].__setitem__("rank", 3)),
            (
                "execution.force_full",
                lambda doc: doc["execution"].__setitem__("force_full", "false"),
            ),
        )
        for message, mutate in mutations:
            with self.subTest(message=message):
                document = classification()
                mutate(document)
                needs = ci_needs(document)
                self.assert_rejected(
                    message,
                    lambda needs=needs: validate_gate_authority(
                        needs, workflow="ci", event_name="pull_request"
                    ),
                )

    def test_signals_and_gates_require_real_json_booleans(self):
        for field in ("signals", "gates"):
            with self.subTest(field=field):
                document = classification()
                document[field][next(iter(document[field]))] = "false"  # type: ignore[index]
                needs = ci_needs(document)
                self.assert_rejected(
                    field,
                    lambda needs=needs: validate_gate_authority(
                        needs, workflow="ci", event_name="pull_request"
                    ),
                )

    def test_classification_schema_uses_exact_known_key_sets(self):
        cases = (
            (
                "classification_json.*unexpected",
                lambda document: document.__setitem__("future", {}),
            ),
            (
                "classification_json.*missing",
                lambda document: document.pop("paths"),
            ),
            (
                "risk.*unexpected",
                lambda document: document["risk"].__setitem__("future", False),
            ),
            (
                "risk.*missing",
                lambda document: document["risk"].pop("reasons"),
            ),
            (
                "execution.*unexpected",
                lambda document: document["execution"].__setitem__("future", False),
            ),
            (
                "execution.*missing",
                lambda document: document["execution"].pop("reasons"),
            ),
            (
                "signals.*unexpected",
                lambda document: document["signals"].__setitem__("future", False),
            ),
            (
                "signals.*missing",
                lambda document: document["signals"].pop("frontend"),
            ),
        )
        for message, mutate in cases:
            with self.subTest(message=message):
                document = classification()
                mutate(document)
                needs = ci_needs(classification())
                needs["classify"]["outputs"]["classification_json"] = json.dumps(  # type: ignore[index]
                    document
                )
                self.assert_rejected(
                    message,
                    lambda needs=needs: validate_gate_authority(
                        needs, workflow="ci", event_name="pull_request"
                    ),
                )

    def test_gate_set_must_be_exact(self):
        for label, mutate in (
            ("missing", lambda gates: gates.pop("run_backend")),
            ("unexpected", lambda gates: gates.__setitem__("future_gate", False)),
        ):
            with self.subTest(label=label):
                document = classification()
                mutate(document["gates"])
                outputs_document = classification()
                needs = ci_needs(outputs_document)
                needs["classify"]["outputs"]["classification_json"] = json.dumps(  # type: ignore[index]
                    document
                )
                self.assert_rejected(
                    f"gates.*{label}",
                    lambda needs=needs: validate_gate_authority(
                        needs, workflow="ci", event_name="pull_request"
                    ),
                )

    def test_needs_job_set_and_classifier_output_set_must_be_exact(self):
        document = classification()
        cases = []

        missing_job = ci_needs(document)
        missing_job.pop("backend")
        cases.append(("NEEDS_JSON.*missing.*backend", missing_job))

        unexpected_job = ci_needs(document)
        unexpected_job["future-job"] = {"result": "success"}
        cases.append(("NEEDS_JSON.*unexpected.*future-job", unexpected_job))

        missing_output = ci_needs(document)
        missing_output["classify"]["outputs"].pop("mixed")  # type: ignore[index]
        cases.append(("outputs.*missing.*mixed", missing_output))

        unexpected_output = ci_needs(document)
        unexpected_output["classify"]["outputs"]["future"] = "false"  # type: ignore[index]
        cases.append(("outputs.*unexpected.*future", unexpected_output))

        for message, needs in cases:
            with self.subTest(message=message):
                self.assert_rejected(
                    message,
                    lambda needs=needs: validate_gate_authority(
                        needs, workflow="ci", event_name="pull_request"
                    ),
                )

    def test_every_scalar_identity_and_gate_output_is_cross_checked(self):
        document = classification(gates={"run_frontend": True})
        cases = {
            "schema_version": "wrong/v1",
            "risk_level": "HIGH",
            "risk_rank": "3",
            "execution_force_full": "true",
            "mixed": "true",
            "run_frontend": "false",
        }
        for output, value in cases.items():
            with self.subTest(output=output):
                needs = ci_needs(document)
                needs["classify"]["outputs"][output] = value  # type: ignore[index]
                self.assert_rejected(
                    output,
                    lambda needs=needs: validate_gate_authority(
                        needs, workflow="ci", event_name="pull_request"
                    ),
                )

    def test_scalar_boolean_outputs_reject_noncanonical_values(self):
        document = classification()
        for output, value in (
            ("execution_force_full", "False"),
            ("run_backend", False),
            ("run_plugins", "0"),
        ):
            with self.subTest(output=output):
                needs = ci_needs(document)
                needs["classify"]["outputs"][output] = value  # type: ignore[index]
                self.assert_rejected(
                    output,
                    lambda needs=needs: validate_gate_authority(
                        needs, workflow="ci", event_name="pull_request"
                    ),
                )

    def test_intrinsic_docs_only_cannot_be_forced_or_non_low(self):
        for document in (
            classification("STANDARD", gates={"docs_only": True}),
            classification("LOW", force_full=True, gates={"docs_only": True}),
        ):
            with self.subTest(document=document):
                needs = ci_needs(document)
                self.assert_rejected(
                    "docs_only",
                    lambda needs=needs: validate_gate_authority(
                        needs, workflow="ci", event_name="pull_request"
                    ),
                )

    def test_malformed_classification_json_and_duplicate_keys_fail_closed(self):
        document = classification()
        malformed = ci_needs(document)
        malformed["classify"]["outputs"]["classification_json"] = "{"  # type: ignore[index]
        self.assert_rejected(
            "classification_json",
            lambda: validate_gate_authority(
                malformed, workflow="ci", event_name="pull_request"
            ),
        )

        duplicate = ci_needs(document)
        raw = json.dumps(document, separators=(",", ":"))
        duplicate["classify"]["outputs"]["classification_json"] = (  # type: ignore[index]
            raw[:-1] + ',"gates":{}}'
        )
        self.assert_rejected(
            "duplicate.*gates",
            lambda: validate_gate_authority(
                duplicate, workflow="ci", event_name="pull_request"
            ),
        )

    def test_unsupported_workflow_or_event_fails_closed(self):
        document = classification()
        needs = ci_needs(document)
        for workflow, event_name in (("deploy", "pull_request"), ("ci", "schedule")):
            with self.subTest(workflow=workflow, event_name=event_name):
                self.assert_rejected(
                    "unsupported",
                    lambda workflow=workflow, event_name=event_name: (
                        validate_gate_authority(
                            needs, workflow=workflow, event_name=event_name
                        )
                    ),
                )

    def test_cli_reads_needs_json_and_emits_machine_readable_result(self):
        document = classification(gates={"run_frontend": True})
        environment = {"NEEDS_JSON": json.dumps(ci_needs(document))}
        stdout = io.StringIO()
        with (
            mock.patch.dict(os.environ, environment, clear=True),
            contextlib.redirect_stdout(stdout),
        ):
            status = main(["--workflow", "ci", "--event-name", "pull_request"])

        self.assertEqual(status, 0)
        self.assertEqual(json.loads(stdout.getvalue())["status"], "PASS")

    def test_cli_resolves_explicit_current_and_authenticated_legacy_release_graphs(self):
        document = classification("STANDARD", force_full=True)
        current = release_needs(document, event_name="workflow_dispatch")
        legacy = dict(current)
        legacy.pop("dr-rehearsal")
        cases = (
            (
                current,
                [
                    "--workflow",
                    "release",
                    "--event-name",
                    "workflow_dispatch",
                    "--release-graph-contract",
                    CURRENT_RELEASE_GRAPH_CONTRACT,
                ],
                {},
                CURRENT_RELEASE_GRAPH_CONTRACT,
            ),
            (
                legacy,
                ["--workflow", "release", "--event-name", "workflow_dispatch"],
                {
                    "GITHUB_WORKFLOW_REF": TRUSTED_PRE_MERGE_WORKFLOW_REF,
                    "GITHUB_WORKFLOW_SHA": TRUSTED_LEGACY_WORKFLOW_SHA,
                    "GITHUB_REPOSITORY": TRUSTED_REPOSITORY,
                    "GITHUB_SHA": TRUSTED_LEGACY_WORKFLOW_SHA,
                },
                LEGACY_RELEASE_GRAPH_CONTRACT,
            ),
        )
        for needs, argv, identity, expected_contract in cases:
            with self.subTest(contract=expected_contract):
                stdout = io.StringIO()
                environment = {"NEEDS_JSON": json.dumps(needs), **identity}
                with (
                    mock.patch.dict(os.environ, environment, clear=True),
                    contextlib.redirect_stdout(stdout),
                ):
                    status = main(argv)
                self.assertEqual(status, 0)
                self.assertEqual(
                    json.loads(stdout.getvalue())["release_graph_contract"],
                    expected_contract,
                )

    def test_cli_missing_invalid_or_duplicate_needs_json_returns_two(self):
        cases = ("", "{", '{"classify":{},"classify":{}}')
        for raw in cases:
            with self.subTest(raw=raw):
                stderr = io.StringIO()
                with (
                    mock.patch.dict(os.environ, {"NEEDS_JSON": raw}, clear=True),
                    contextlib.redirect_stderr(stderr),
                ):
                    status = main(["--workflow", "ci", "--event-name", "pull_request"])
                self.assertEqual(status, 2)
                self.assertEqual(json.loads(stderr.getvalue())["status"], "FAIL")


if __name__ == "__main__":
    unittest.main()
