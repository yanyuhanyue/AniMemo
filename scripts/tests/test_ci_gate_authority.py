from __future__ import annotations

import contextlib
import io
import json
import os
import unittest
from copy import deepcopy
from unittest import mock

from scripts.ci_classify import classify_paths
from scripts.ci_gate_authority import (
    CI_JOB_GATES,
    CLASSIFIER_OUTPUT_NAMES,
    CURRENT_RELEASE_GRAPH_CONTRACT,
    PRODUCT_GATE_NAMES,
    RELEASE_JOB_GATES,
    SCHEMA_VERSION,
    GateAuthorityError,
    main,
    validate_gate_authority,
)


def classification(
    paths: list[str] | None = None, *, force_full: bool = False
) -> dict[str, object]:
    outputs = classify_paths(paths or ["src/pages/Journal.jsx"], force_full=force_full)
    return json.loads(outputs["classification_json"])


def classify_job(document: dict[str, object]) -> dict[str, object]:
    risk = document["risk"]
    execution = document["execution"]
    gates = document["gates"]
    assert isinstance(risk, dict)
    assert isinstance(execution, dict)
    assert isinstance(gates, dict)
    outputs: dict[str, object] = {
        "schema_version": document["schema_version"],
        "risk_level": risk["level"],
        "risk_rank": str(risk["rank"]),
        "execution_force_full": str(execution["force_full"]).lower(),
        "classification_json": json.dumps(document, separators=(",", ":")),
    }
    for name in CLASSIFIER_OUTPUT_NAMES:
        if name not in outputs:
            outputs[name] = str(gates[name]).lower()
    return {"result": "success", "outputs": outputs}


def ci_needs(
    document: dict[str, object], *, event_name: str = "pull_request"
) -> dict[str, object]:
    execution = document["execution"]
    gates = document["gates"]
    assert isinstance(execution, dict)
    assert isinstance(gates, dict)
    profile = execution["profile"]
    needs: dict[str, object] = {"classify": classify_job(document)}
    for job, gate in CI_JOB_GATES.items():
        if job == "fast-fail":
            selected = event_name != "push" and profile != "DOCS_ONLY"
        elif job == "docs-only":
            selected = profile in {"DOCS_ONLY", "CONTRACT_VALIDATION_ONLY"}
        else:
            assert gate is not None
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
    needs: dict[str, object], *, event_name: str = "pull_request"
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

    def test_classifier_output_surface_stays_old_main_compatible(self):
        self.assertEqual(SCHEMA_VERSION, "animemo.ci-risk/v2")
        self.assertNotIn("execution_profile", CLASSIFIER_OUTPUT_NAMES)
        self.assertNotIn("run_contract_validation", CLASSIFIER_OUTPUT_NAMES)
        self.assertNotIn("run_release_dr", CLASSIFIER_OUTPUT_NAMES)
        self.assertIn("classification_json", CLASSIFIER_OUTPUT_NAMES)

    def test_ci_docs_and_contract_profiles_select_the_shared_lightweight_slot(self):
        docs = classification(["README.md"])
        contract = classification(
            [
                "docs/backup-contract-v1.md",
                "scripts/tests/test_recovery_migration_contracts.py",
            ]
        )

        docs_result = validate_gate_authority(
            ci_needs(docs), workflow="ci", event_name="pull_request"
        )
        contract_result = validate_gate_authority(
            ci_needs(contract), workflow="ci", event_name="pull_request"
        )

        self.assertEqual(docs_result["execution_profile"], "DOCS_ONLY")
        self.assertEqual(docs_result["selected_jobs"], ["docs-only"])
        self.assertEqual(
            contract_result["execution_profile"], "CONTRACT_VALIDATION_ONLY"
        )
        self.assertEqual(
            contract_result["selected_jobs"], ["fast-fail", "docs-only"]
        )

    def test_ci_targeted_and_full_profiles_require_exact_selected_jobs(self):
        frontend = classification(["src/pages/Journal.jsx"])
        full = classification(["README.md"], force_full=True)

        targeted_result = validate_gate_authority(
            ci_needs(frontend), workflow="ci", event_name="pull_request"
        )
        full_result = validate_gate_authority(
            ci_needs(full, event_name="workflow_call"),
            workflow="ci",
            event_name="workflow_call",
        )

        self.assertEqual(targeted_result["selected_jobs"], ["fast-fail", "frontend"])
        self.assertEqual(full_result["execution_profile"], "FULL_AUTHORITY")
        self.assertEqual(
            set(full_result["selected_jobs"]),
            {"fast-fail", *set(CI_JOB_GATES) - {"docs-only"}},
        )

    def test_ci_push_remains_lightweight(self):
        docs = classification(["README.md"])
        product = classification(["backend/journal/services.py"])

        docs_result = validate_gate_authority(
            ci_needs(docs, event_name="push"), workflow="ci", event_name="push"
        )
        product_result = validate_gate_authority(
            ci_needs(product, event_name="push"), workflow="ci", event_name="push"
        )

        self.assertEqual(docs_result["selected_jobs"], ["docs-only"])
        self.assertEqual(product_result["selected_jobs"], [])

    def test_release_targeted_stateful_and_dr_are_independent(self):
        cases = (
            (
                ["backend/journal/migrations/9999_gate_test.py"],
                ["stateful-upgrade"],
            ),
            (["durability/backup.py"], ["dr-rehearsal"]),
            (
                ["updater/agent.py"],
                ["updater-isolated", "stateful-upgrade"],
            ),
            (
                ["deploy/docker-compose.yml"],
                ["docker", "stateful-upgrade"],
            ),
        )
        for paths, selected in cases:
            with self.subTest(paths=paths):
                document = classification(paths)
                result = validate_current_release(release_needs(document))
                self.assertEqual(result["selected_jobs"], selected)

    def test_release_force_full_selects_every_release_job_except_postmerge(self):
        document = classification(["README.md"], force_full=True)
        result = validate_current_release(
            release_needs(document, event_name="workflow_call"),
            event_name="workflow_call",
        )

        self.assertEqual(
            result["selected_jobs"],
            ["updater-isolated", "docker", "stateful-upgrade", "dr-rehearsal"],
        )

    def test_release_push_selects_only_post_merge_sanity(self):
        document = classification(["backend/journal/services.py"])
        result = validate_current_release(
            release_needs(document, event_name="push"), event_name="push"
        )

        self.assertEqual(result["selected_jobs"], ["post-merge-sanity"])

    def test_release_graph_requires_the_single_current_contract(self):
        document = classification(["README.md"], force_full=True)
        current = release_needs(document, event_name="workflow_dispatch")

        current_result = validate_current_release(
            current, event_name="workflow_dispatch"
        )

        self.assertEqual(
            current_result["release_graph_contract"], CURRENT_RELEASE_GRAPH_CONTRACT
        )

        self.assert_rejected(
            "release graph contract",
            lambda: validate_gate_authority(
                current,
                workflow="release",
                event_name="workflow_dispatch",
            ),
        )
        self.assert_rejected(
            "unsupported release graph contract",
            lambda: validate_gate_authority(
                current,
                workflow="release",
                event_name="workflow_dispatch",
                release_graph_contract="animemo.release-gate.jobs/v999",
            ),
        )

    def test_real_classifier_profiles_validate_for_ci_and_release(self):
        cases = (
            (["README.md"], False, "DOCS_ONLY"),
            (
                ["docs/backup-contract-v1.md"],
                False,
                "CONTRACT_VALIDATION_ONLY",
            ),
            (["src/pages/Journal.jsx"], False, "TARGETED"),
            (["updater/agent.py"], False, "TARGETED"),
            (["future/new.bin"], False, "TARGETED"),
            (["README.md"], True, "FULL_AUTHORITY"),
        )
        for paths, force_full, expected_profile in cases:
            event_name = "workflow_call" if force_full else "pull_request"
            document = classification(paths, force_full=force_full)
            with self.subTest(paths=paths, workflow="ci"):
                result = validate_gate_authority(
                    ci_needs(document, event_name=event_name),
                    workflow="ci",
                    event_name=event_name,
                )
                self.assertEqual(result["execution_profile"], expected_profile)
            with self.subTest(paths=paths, workflow="release"):
                result = validate_current_release(
                    release_needs(document, event_name=event_name),
                    event_name=event_name,
                )
                self.assertEqual(result["execution_profile"], expected_profile)

    def test_authority_recomputes_contract_profile_from_exact_paths(self):
        document = classification(["docs/backup-contract-v1.md"])
        path_record = document["paths"][0]
        assert isinstance(path_record, dict)
        path_record["path"] = "docs/not-allowlisted-contract.md"
        needs = ci_needs(document)

        self.assert_rejected(
            "profile violates audited path policy",
            lambda: validate_gate_authority(
                needs, workflow="ci", event_name="pull_request"
            ),
        )

        downgraded = classification(["docs/backup-contract-v1.md"])
        downgraded["risk"] = {"level": "LOW", "rank": 1, "reasons": []}
        downgraded["paths"][0]["risk_level"] = "LOW"
        downgraded["execution"]["profile"] = "DOCS_ONLY"
        downgraded["gates"]["docs_only"] = True
        downgraded["gates"]["run_contract_validation"] = False
        self.assert_rejected(
            "audited contract path.*HIGH risk",
            lambda: validate_gate_authority(
                ci_needs(downgraded), workflow="ci", event_name="pull_request"
            ),
        )

    def test_path_records_and_unknown_paths_are_strict(self):
        cases = []

        duplicate = classification(["src/pages/Journal.jsx"])
        duplicate["paths"].append(deepcopy(duplicate["paths"][0]))
        cases.append(("sorted and contain no duplicates", duplicate))

        empty_path = classification(["src/pages/Journal.jsx"])
        empty_path["paths"][0]["path"] = ""
        cases.append(("path must be non-empty", empty_path))

        unknown_not_subset = classification(["src/pages/Journal.jsx"])
        unknown_not_subset["unknown_paths"] = ["future/new.bin"]
        cases.append(("subset of changed paths", unknown_not_subset))

        for message, document in cases:
            with self.subTest(message=message):
                needs = ci_needs(document)
                self.assert_rejected(
                    message,
                    lambda needs=needs: validate_gate_authority(
                        needs, workflow="ci", event_name="pull_request"
                    ),
                )

    def test_authority_rejects_gate_downgrades_and_escalations(self):
        cases = (
            (
                "run_release_stateful",
                classification(["backend/journal/migrations/9999_gate_test.py"]),
                "run_release_stateful",
                False,
            ),
            (
                "run_release_dr",
                classification(["durability/backup.py"]),
                "run_release_dr",
                False,
            ),
            (
                "run_release_updater",
                classification(["updater/agent.py"]),
                "run_release_updater",
                False,
            ),
            (
                "run_release_full",
                classification(["backend/journal/migrations/9999_gate_test.py"]),
                "run_release_full",
                True,
            ),
            (
                "critical_gate",
                classification(["updater/agent.py"]),
                "critical_gate",
                False,
            ),
        )
        for message, document, gate, value in cases:
            with self.subTest(gate=gate):
                document["gates"][gate] = value
                needs = ci_needs(document)
                self.assert_rejected(
                    message,
                    lambda needs=needs: validate_gate_authority(
                        needs, workflow="ci", event_name="pull_request"
                    ),
                )

    def test_unknown_and_full_profiles_cannot_omit_any_selected_gate(self):
        for paths, force_full in ((["future/new.bin"], False), (["README.md"], True)):
            for gate in (*PRODUCT_GATE_NAMES, "run_release_updater", "run_release_docker", "run_release_stateful", "run_release_dr"):
                with self.subTest(paths=paths, force_full=force_full, gate=gate):
                    document = classification(paths, force_full=force_full)
                    document["gates"][gate] = False
                    event_name = "workflow_call" if force_full else "pull_request"
                    needs = ci_needs(document, event_name=event_name)
                    self.assert_rejected(
                        gate,
                        lambda needs=needs, event_name=event_name: validate_gate_authority(
                            needs, workflow="ci", event_name=event_name
                        ),
                    )

    def test_mixed_gate_must_match_signals(self):
        document = classification(["src/pages/Journal.jsx"])
        document["signals"]["backend"] = True
        needs = ci_needs(document)

        self.assert_rejected(
            "mixed",
            lambda: validate_gate_authority(
                needs, workflow="ci", event_name="pull_request"
            ),
        )

    def test_authority_events_require_explicit_force_full(self):
        for event_name in ("merge_group", "workflow_dispatch", "workflow_call"):
            with self.subTest(event_name=event_name, force_full=False):
                document = classification(["src/pages/Journal.jsx"])
                needs = ci_needs(document, event_name=event_name)
                self.assert_rejected(
                    "requires.*force_full=true",
                    lambda needs=needs, event_name=event_name: validate_gate_authority(
                        needs, workflow="ci", event_name=event_name
                    ),
                )
            with self.subTest(event_name=event_name, force_full=True):
                document = classification(
                    ["src/pages/Journal.jsx"], force_full=True
                )
                result = validate_gate_authority(
                    ci_needs(document, event_name=event_name),
                    workflow="ci",
                    event_name=event_name,
                )
                self.assertEqual(result["status"], "PASS")

        forced_push = classification(["README.md"], force_full=True)
        self.assert_rejected(
            "push cannot.*force_full=true",
            lambda: validate_gate_authority(
                ci_needs(forced_push, event_name="push"),
                workflow="ci",
                event_name="push",
            ),
        )

    def test_selected_unselected_missing_failed_and_cancelled_jobs_fail_closed(self):
        document = classification(["src/pages/Journal.jsx"])
        cases = (
            ("frontend", "frontend", "skipped"),
            ("backend", "backend", "success"),
            ("frontend", "frontend", "failure"),
            ("backend", "backend", "cancelled"),
        )
        for message, job, result in cases:
            with self.subTest(job=job, result=result):
                needs = ci_needs(document)
                needs[job]["result"] = result
                self.assert_rejected(
                    message,
                    lambda needs=needs: validate_gate_authority(
                        needs, workflow="ci", event_name="pull_request"
                    ),
                )

        missing = ci_needs(document)
        missing.pop("frontend")
        self.assert_rejected(
            "missing keys: frontend",
            lambda: validate_gate_authority(
                missing, workflow="ci", event_name="pull_request"
            ),
        )

    def test_classify_and_needs_job_sets_are_fail_closed(self):
        document = classification(["src/pages/Journal.jsx"])

        missing_classify = ci_needs(document)
        missing_classify.pop("classify")
        self.assert_rejected(
            "missing keys: classify",
            lambda: validate_gate_authority(
                missing_classify, workflow="ci", event_name="pull_request"
            ),
        )

        failed_classify = ci_needs(document)
        failed_classify["classify"]["result"] = "failure"
        self.assert_rejected(
            "classify job must be success",
            lambda: validate_gate_authority(
                failed_classify, workflow="ci", event_name="pull_request"
            ),
        )

        unexpected = ci_needs(document)
        unexpected["future-job"] = {"result": "success"}
        self.assert_rejected(
            "unexpected keys: future-job",
            lambda: validate_gate_authority(
                unexpected, workflow="ci", event_name="pull_request"
            ),
        )

    def test_schema_profiles_json_types_and_exact_key_sets_are_strict(self):
        cases = (
            ("schema", lambda doc: doc.__setitem__("schema_version", "future/v3")),
            ("risk level", lambda doc: doc["risk"].__setitem__("level", "SEVERE")),
            ("risk rank", lambda doc: doc["risk"].__setitem__("rank", 4)),
            (
                "execution profile",
                lambda doc: doc["execution"].__setitem__("profile", "FAST"),
            ),
            (
                "execution.force_full",
                lambda doc: doc["execution"].__setitem__("force_full", "false"),
            ),
            (
                "signals.frontend",
                lambda doc: doc["signals"].__setitem__("frontend", "false"),
            ),
            (
                "gates.run_frontend",
                lambda doc: doc["gates"].__setitem__("run_frontend", "true"),
            ),
            ("unexpected keys", lambda doc: doc.__setitem__("future", {})),
            ("missing keys", lambda doc: doc.pop("paths")),
            (
                "execution.*unexpected",
                lambda doc: doc["execution"].__setitem__("future", False),
            ),
            ("gates.*missing", lambda doc: doc["gates"].pop("run_backend")),
            (
                "signals.*unexpected",
                lambda doc: doc["signals"].__setitem__("future", False),
            ),
            ("signals.*missing", lambda doc: doc["signals"].pop("frontend")),
            (
                "gates.*unexpected",
                lambda doc: doc["gates"].__setitem__("future", False),
            ),
        )
        for message, mutate in cases:
            with self.subTest(message=message):
                document = classification(["src/pages/Journal.jsx"])
                mutate(document)
                baseline = classification(["src/pages/Journal.jsx"])
                needs = ci_needs(baseline)
                needs["classify"]["outputs"]["classification_json"] = json.dumps(document)
                self.assert_rejected(
                    message,
                    lambda needs=needs: validate_gate_authority(
                        needs, workflow="ci", event_name="pull_request"
                    ),
                )

    def test_classifier_scalar_outputs_and_output_set_are_strict(self):
        document = classification(["src/pages/Journal.jsx"])
        for output, value in (
            ("schema_version", "wrong/v1"),
            ("risk_level", "HIGH"),
            ("risk_rank", "3"),
            ("execution_force_full", "true"),
            ("mixed", "true"),
            ("run_frontend", "false"),
            ("run_backend", False),
        ):
            with self.subTest(output=output):
                needs = ci_needs(document)
                needs["classify"]["outputs"][output] = value
                self.assert_rejected(
                    output,
                    lambda needs=needs: validate_gate_authority(
                        needs, workflow="ci", event_name="pull_request"
                    ),
                )

        missing = ci_needs(document)
        missing["classify"]["outputs"].pop("mixed")
        self.assert_rejected(
            "outputs.*missing.*mixed",
            lambda: validate_gate_authority(
                missing, workflow="ci", event_name="pull_request"
            ),
        )
        unexpected = ci_needs(document)
        unexpected["classify"]["outputs"]["run_release_dr"] = "false"
        self.assert_rejected(
            "outputs.*unexpected.*run_release_dr",
            lambda: validate_gate_authority(
                unexpected, workflow="ci", event_name="pull_request"
            ),
        )

    def test_malformed_and_duplicate_classification_json_fail_closed(self):
        document = classification(["src/pages/Journal.jsx"])
        malformed = ci_needs(document)
        malformed["classify"]["outputs"]["classification_json"] = "{"
        self.assert_rejected(
            "classification_json",
            lambda: validate_gate_authority(
                malformed, workflow="ci", event_name="pull_request"
            ),
        )

        duplicate = ci_needs(document)
        raw = json.dumps(document, separators=(",", ":"))
        duplicate["classify"]["outputs"]["classification_json"] = (
            raw[:-1] + ',"gates":{}}'
        )
        self.assert_rejected(
            "duplicate.*gates",
            lambda: validate_gate_authority(
                duplicate, workflow="ci", event_name="pull_request"
            ),
        )

    def test_unsupported_workflow_event_or_release_contract_fails_closed(self):
        document = classification(["src/pages/Journal.jsx"])
        needs = ci_needs(document)
        for workflow, event_name in (("deploy", "pull_request"), ("ci", "schedule")):
            with self.subTest(workflow=workflow, event_name=event_name):
                self.assert_rejected(
                    "unsupported",
                    lambda workflow=workflow, event_name=event_name: validate_gate_authority(
                        needs, workflow=workflow, event_name=event_name
                    ),
                )
        self.assert_rejected(
            "not valid for the CI workflow",
            lambda: validate_gate_authority(
                needs,
                workflow="ci",
                event_name="pull_request",
                release_graph_contract=CURRENT_RELEASE_GRAPH_CONTRACT,
            ),
        )

    def test_cli_emits_machine_readable_pass_and_fail(self):
        document = classification(["src/pages/Journal.jsx"])
        environment = {"NEEDS_JSON": json.dumps(ci_needs(document))}
        stdout = io.StringIO()
        with (
            mock.patch.dict(os.environ, environment, clear=True),
            contextlib.redirect_stdout(stdout),
        ):
            code = main(["--workflow", "ci", "--event-name", "pull_request"])

        self.assertEqual(code, 0)
        result = json.loads(stdout.getvalue())
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["execution_profile"], "TARGETED")

        stderr = io.StringIO()
        with (
            mock.patch.dict(os.environ, {}, clear=True),
            contextlib.redirect_stderr(stderr),
        ):
            code = main(["--workflow", "ci", "--event-name", "pull_request"])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(stderr.getvalue())["status"], "FAIL")


if __name__ == "__main__":
    unittest.main()
