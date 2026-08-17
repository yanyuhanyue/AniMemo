#!/usr/bin/env python3
"""Fail-closed, selection-aware authority for AniMemo CI workflow jobs."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Mapping, Sequence
from typing import Any

SCHEMA_VERSION = "animemo.ci-risk/v2"
RISK_RANKS = {"LOW": 1, "STANDARD": 2, "HIGH": 3, "CRITICAL": 4}
EXECUTION_PROFILES = frozenset(
    {"DOCS_ONLY", "CONTRACT_VALIDATION_ONLY", "TARGETED", "FULL_AUTHORITY"}
)

AUDITED_CONTRACT_PRIMARY_DOCUMENTS = frozenset(
    {
        "docs/backup-contract-v1.md",
        "docs/compatibility-matrix-v1.md",
        "docs/doctor-basic-contract-v1.md",
        "docs/migration-bundle-v1.md",
        "docs/migration-secret-envelope-v1.md",
        "docs/restore-contract-v1.md",
    }
)
AUDITED_CONTRACT_CHANGE_PATHS = frozenset(
    {
        *AUDITED_CONTRACT_PRIMARY_DOCUMENTS,
        "CONTEXT.md",
        "README.md",
        "docs/data-bundle-v1.md",
        "scripts/tests/test_recovery_migration_contracts.py",
    }
)
AUDITED_CONTRACT_VALIDATION_TESTS = frozenset(
    {"scripts/tests/test_recovery_migration_contracts.py"}
)

TOP_LEVEL_KEYS = frozenset(
    {
        "schema_version",
        "risk",
        "execution",
        "paths",
        "unknown_paths",
        "signals",
        "gates",
    }
)
RISK_KEYS = frozenset({"level", "rank", "reasons"})
EXECUTION_KEYS = frozenset({"profile", "force_full", "reasons"})
SIGNAL_NAMES = (
    "frontend",
    "backend",
    "auth",
    "api_contract",
    "plugin",
    "integration",
    "bridge",
    "migration",
    "database",
    "dependencies",
    "ci",
    "deployment",
    "release",
    "updater",
    "shared_contract",
    "first_run",
    "recovery",
    "media_storage",
    "tooling",
)
MIXED_SIGNAL_NAMES = SIGNAL_NAMES[:-1]

GATE_NAMES = (
    "docs_only",
    "run_contract_validation",
    "mixed",
    "run_frontend",
    "run_backend",
    "run_bootstrap",
    "run_plugins",
    "run_bridge",
    "run_postgres",
    "run_runtime",
    "run_release_full",
    "run_release_updater",
    "run_release_docker",
    "run_release_stateful",
    "run_release_dr",
    "full_gate",
    "critical_gate",
)
SCALAR_GATE_NAMES = tuple(
    name
    for name in GATE_NAMES
    if name not in {"run_contract_validation", "run_release_dr"}
)
CLASSIFIER_OUTPUT_NAMES = (
    "schema_version",
    "risk_level",
    "risk_rank",
    "execution_force_full",
    "classification_json",
    *SCALAR_GATE_NAMES,
)
PRODUCT_GATE_NAMES = (
    "run_frontend",
    "run_backend",
    "run_bootstrap",
    "run_plugins",
    "run_bridge",
    "run_postgres",
    "run_runtime",
)

# None marks a job selected directly by event policy instead of a classifier gate.
CI_JOB_GATES: dict[str, str | None] = {
    "fast-fail": None,
    "docs-only": "docs_only",
    "frontend": "run_frontend",
    "backend": "run_backend",
    "bootstrap-smoke": "run_bootstrap",
    "postgres": "run_postgres",
    "plugins": "run_plugins",
    "astrbot-bridge": "run_bridge",
    "astrbot-runtime": "run_runtime",
}

RELEASE_JOB_GATES: dict[str, str | None] = {
    "post-merge-sanity": None,
    "updater-isolated": "run_release_updater",
    "docker": "run_release_docker",
    "stateful-upgrade": "run_release_stateful",
    "dr-rehearsal": "run_release_dr",
}
CURRENT_RELEASE_GRAPH_CONTRACT = "animemo.release-gate.jobs/v2"

SUPPORTED_EVENTS = frozenset(
    {"push", "pull_request", "merge_group", "workflow_call", "workflow_dispatch"}
)
FORCE_FULL_EVENTS = frozenset({"merge_group", "workflow_call", "workflow_dispatch"})


class GateAuthorityError(ValueError):
    """The workflow evidence cannot establish selection-aware authority."""


class _DuplicateJsonKey(ValueError):
    def __init__(self, key: str):
        super().__init__(key)
        self.key = key


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKey(key)
        result[key] = value
    return result


def _load_json(raw: object, *, label: str) -> Any:
    if not isinstance(raw, str) or not raw:
        raise GateAuthorityError(f"{label} must be a non-empty JSON string")
    try:
        return json.loads(raw, object_pairs_hook=_reject_duplicate_keys)
    except _DuplicateJsonKey as error:
        raise GateAuthorityError(
            f"{label} contains duplicate object key {error.key!r}"
        ) from error
    except json.JSONDecodeError as error:
        raise GateAuthorityError(f"{label} is invalid JSON: {error.msg}") from error


def _object(value: object, *, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise GateAuthorityError(f"{label} must be an object")
    return value


def _field(source: Mapping[str, Any], name: str, *, label: str) -> Any:
    if name not in source:
        raise GateAuthorityError(f"{label}.{name} is missing")
    return source[name]


def _exact_keys(
    source: Mapping[str, Any], expected: Sequence[str] | frozenset[str], *, label: str
) -> None:
    missing = sorted(set(expected) - set(source))
    unexpected = sorted(set(source) - set(expected))
    if missing:
        raise GateAuthorityError(f"{label} has missing keys: {', '.join(missing)}")
    if unexpected:
        raise GateAuthorityError(
            f"{label} has unexpected keys: {', '.join(unexpected)}"
        )


def _list(value: object, *, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise GateAuthorityError(f"{label} must be an array")
    return value


def _json_bool(value: object, *, label: str) -> bool:
    if type(value) is not bool:
        raise GateAuthorityError(f"{label} must be a JSON boolean")
    return value


def _scalar(outputs: Mapping[str, Any], name: str, expected: str) -> None:
    value = _field(outputs, name, label="classify.outputs")
    if not isinstance(value, str):
        raise GateAuthorityError(f"classify.outputs.{name} must be a string")
    if value != expected:
        raise GateAuthorityError(
            f"classify.outputs.{name} does not match classification_json: "
            f"expected {expected!r}, found {value!r}"
        )


def _job(needs: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    if name not in needs:
        raise GateAuthorityError(f"{name} job is missing from NEEDS_JSON")
    return _object(needs[name], label=f"needs.{name}")


def _job_result(needs: Mapping[str, Any], name: str) -> str:
    job = _job(needs, name)
    result = _field(job, "result", label=f"needs.{name}")
    if not isinstance(result, str) or not result:
        raise GateAuthorityError(f"{name} job result must be a non-empty string")
    return result


def _validate_classification(
    needs: Mapping[str, Any],
) -> tuple[Mapping[str, Any], str, int, str, bool, dict[str, bool]]:
    classify = _job(needs, "classify")
    classify_result = _field(classify, "result", label="needs.classify")
    if classify_result != "success":
        rendered = classify_result if isinstance(classify_result, str) else "invalid"
        raise GateAuthorityError(f"classify job must be success; found {rendered!r}")

    outputs = _object(
        _field(classify, "outputs", label="needs.classify"),
        label="needs.classify.outputs",
    )
    _exact_keys(outputs, CLASSIFIER_OUTPUT_NAMES, label="needs.classify.outputs")
    raw_classification = _field(
        outputs, "classification_json", label="classify.outputs"
    )
    document = _object(
        _load_json(raw_classification, label="classification_json"),
        label="classification_json",
    )
    _exact_keys(document, TOP_LEVEL_KEYS, label="classification_json")

    schema_version = _field(document, "schema_version", label="classification_json")
    if schema_version != SCHEMA_VERSION:
        raise GateAuthorityError(
            f"classification schema must be {SCHEMA_VERSION!r}; "
            f"found {schema_version!r}"
        )

    risk = _object(
        _field(document, "risk", label="classification_json"),
        label="classification_json.risk",
    )
    _exact_keys(risk, RISK_KEYS, label="classification_json.risk")
    risk_level = _field(risk, "level", label="classification_json.risk")
    if not isinstance(risk_level, str) or risk_level not in RISK_RANKS:
        raise GateAuthorityError(
            f"classification risk level is unsupported: {risk_level!r}"
        )
    risk_rank = _field(risk, "rank", label="classification_json.risk")
    if type(risk_rank) is not int:
        raise GateAuthorityError("classification risk rank must be an integer")
    expected_rank = RISK_RANKS[risk_level]
    if risk_rank != expected_rank:
        raise GateAuthorityError(
            f"classification risk rank does not match {risk_level}: "
            f"expected {expected_rank}, found {risk_rank}"
        )
    _list(
        _field(risk, "reasons", label="classification_json.risk"),
        label="classification_json.risk.reasons",
    )

    execution = _object(
        _field(document, "execution", label="classification_json"),
        label="classification_json.execution",
    )
    _exact_keys(execution, EXECUTION_KEYS, label="classification_json.execution")
    execution_profile = _field(
        execution, "profile", label="classification_json.execution"
    )
    if (
        not isinstance(execution_profile, str)
        or execution_profile not in EXECUTION_PROFILES
    ):
        raise GateAuthorityError(
            f"classification execution profile is unsupported: {execution_profile!r}"
        )
    force_full = _json_bool(
        _field(execution, "force_full", label="classification_json.execution"),
        label="classification_json.execution.force_full",
    )
    _list(
        _field(execution, "reasons", label="classification_json.execution"),
        label="classification_json.execution.reasons",
    )

    path_records = _list(
        _field(document, "paths", label="classification_json"),
        label="classification_json.paths",
    )
    changed_paths: list[str] = []
    path_risks: dict[str, str] = {}
    for index, raw_record in enumerate(path_records):
        record = _object(raw_record, label=f"classification_json.paths[{index}]")
        _exact_keys(
            record,
            frozenset({"path", "risk_level", "rules"}),
            label=f"classification_json.paths[{index}]",
        )
        path = _field(record, "path", label=f"classification_json.paths[{index}]")
        if not isinstance(path, str) or not path:
            raise GateAuthorityError(
                f"classification_json.paths[{index}].path must be non-empty"
            )
        changed_paths.append(path)
        path_risk = _field(
            record, "risk_level", label=f"classification_json.paths[{index}]"
        )
        if path_risk not in RISK_RANKS:
            raise GateAuthorityError(
                f"classification_json.paths[{index}].risk_level is unsupported"
            )
        path_risks[path] = path_risk
        _list(
            _field(record, "rules", label=f"classification_json.paths[{index}]"),
            label=f"classification_json.paths[{index}].rules",
        )

    unknown_paths = _list(
        _field(document, "unknown_paths", label="classification_json"),
        label="classification_json.unknown_paths",
    )
    if any(not isinstance(path, str) or not path for path in unknown_paths):
        raise GateAuthorityError(
            "classification_json.unknown_paths must contain non-empty strings"
        )
    if not set(unknown_paths).issubset(changed_paths):
        raise GateAuthorityError(
            "classification_json.unknown_paths must be a subset of changed paths"
        )
    if unknown_paths != sorted(set(unknown_paths)):
        raise GateAuthorityError(
            "classification_json.unknown_paths must be sorted and contain no duplicates"
        )

    signals = _object(
        _field(document, "signals", label="classification_json"),
        label="classification_json.signals",
    )
    _exact_keys(signals, SIGNAL_NAMES, label="classification_json.signals")
    signal_values = {
        name: _json_bool(signals[name], label=f"classification_json.signals.{name}")
        for name in SIGNAL_NAMES
    }

    raw_gates = _object(
        _field(document, "gates", label="classification_json"),
        label="classification_json.gates",
    )
    _exact_keys(raw_gates, GATE_NAMES, label="classification_json.gates")
    gates = {
        name: _json_bool(raw_gates[name], label=f"classification_json.gates.{name}")
        for name in GATE_NAMES
    }

    if changed_paths != sorted(set(changed_paths)):
        raise GateAuthorityError(
            "classification_json.paths must be sorted and contain no duplicates"
        )
    if changed_paths:
        expected_risk = max(path_risks.values(), key=RISK_RANKS.__getitem__)
        if risk_level != expected_risk:
            raise GateAuthorityError(
                "classification_json.risk.level must match the highest path risk: "
                f"expected {expected_risk}, found {risk_level}"
            )
    elif risk_level != "CRITICAL":
        raise GateAuthorityError(
            "an empty changed-path set must retain fail-closed CRITICAL risk"
        )

    audited_sensitive_paths = (
        AUDITED_CONTRACT_PRIMARY_DOCUMENTS | AUDITED_CONTRACT_VALIDATION_TESTS
    )
    for path in sorted(set(changed_paths) & audited_sensitive_paths):
        if path_risks[path] != "HIGH":
            raise GateAuthorityError(
                f"audited contract path {path!r} must retain inherent HIGH risk"
            )

    root_docs = {"LICENSE", "NOTICE", "THIRD_PARTY_NOTICES", "TRADEMARKS"}
    authority_docs_only = bool(changed_paths) and risk_level == "LOW" and all(
        path in root_docs
        or path.lower().startswith("docs/")
        or path.lower().endswith((".md", ".mdx", ".rst"))
        for path in changed_paths
    )
    audited_contract_change = (
        bool(changed_paths)
        and any(path in AUDITED_CONTRACT_PRIMARY_DOCUMENTS for path in changed_paths)
        and all(path in AUDITED_CONTRACT_CHANGE_PATHS for path in changed_paths)
    )
    if force_full:
        expected_profile = "FULL_AUTHORITY"
    elif audited_contract_change:
        expected_profile = "CONTRACT_VALIDATION_ONLY"
    elif authority_docs_only:
        expected_profile = "DOCS_ONLY"
    else:
        expected_profile = "TARGETED"
    if execution_profile != expected_profile:
        raise GateAuthorityError(
            "classification_json.execution.profile violates audited path policy: "
            f"expected {expected_profile}, found {execution_profile}"
        )
    if execution_profile == "CONTRACT_VALIDATION_ONLY" and risk_level != "HIGH":
        raise GateAuthorityError(
            "CONTRACT_VALIDATION_ONLY must retain inherent HIGH risk"
        )

    expected_mixed = sum(signal_values[name] for name in MIXED_SIGNAL_NAMES) > 1
    targeted = execution_profile == "TARGETED"
    conservative_broad = targeted and (bool(unknown_paths) or not changed_paths)

    def selected(*names: str) -> bool:
        return targeted and any(signal_values[name] for name in names)

    expected_gates = {
        "docs_only": execution_profile == "DOCS_ONLY",
        "run_contract_validation": execution_profile
        == "CONTRACT_VALIDATION_ONLY",
        "mixed": expected_mixed,
        "run_frontend": force_full
        or conservative_broad
        or selected("frontend"),
        "run_backend": force_full
        or conservative_broad
        or selected(
            "backend",
            "auth",
            "api_contract",
            "migration",
            "database",
            "integration",
            "shared_contract",
            "first_run",
            "media_storage",
        ),
        "run_bootstrap": force_full
        or conservative_broad
        or selected("ci", "deployment", "first_run", "updater"),
        "run_plugins": force_full
        or conservative_broad
        or selected(
            "plugin",
            "integration",
            "shared_contract",
            "migration",
            "recovery",
            "release",
        ),
        "run_bridge": force_full
        or conservative_broad
        or selected("bridge", "integration", "shared_contract"),
        "run_postgres": force_full
        or conservative_broad
        or selected(
            "auth",
            "api_contract",
            "plugin",
            "migration",
            "database",
            "integration",
            "shared_contract",
            "media_storage",
            "first_run",
            "recovery",
        ),
        "run_runtime": force_full
        or conservative_broad
        or selected("bridge", "integration", "shared_contract"),
        "run_release_full": force_full,
        "run_release_updater": force_full
        or conservative_broad
        or selected("updater", "release"),
        "run_release_docker": force_full
        or conservative_broad
        or selected("deployment", "release", "first_run"),
        "run_release_stateful": force_full
        or conservative_broad
        or selected("database", "deployment", "release", "updater", "first_run"),
        "run_release_dr": force_full
        or conservative_broad
        or selected("recovery", "migration"),
        "full_gate": force_full,
        "critical_gate": risk_level == "CRITICAL",
    }
    for name, expected in expected_gates.items():
        if gates[name] != expected:
            raise GateAuthorityError(
                f"classification_json.gates.{name} violates execution policy: "
                f"expected {expected}, found {gates[name]}"
            )

    _scalar(outputs, "schema_version", SCHEMA_VERSION)
    _scalar(outputs, "risk_level", risk_level)
    _scalar(outputs, "risk_rank", str(risk_rank))
    _scalar(outputs, "execution_force_full", str(force_full).lower())
    for name in SCALAR_GATE_NAMES:
        value = gates[name]
        _scalar(outputs, name, str(value).lower())

    return document, risk_level, risk_rank, execution_profile, force_full, gates


def _ci_selected_jobs(
    event_name: str, execution_profile: str, gates: Mapping[str, bool]
) -> list[str]:
    selected: list[str] = []
    for job, gate in CI_JOB_GATES.items():
        if job == "fast-fail":
            should_run = event_name != "push" and execution_profile != "DOCS_ONLY"
        elif job == "docs-only":
            should_run = execution_profile in {
                "DOCS_ONLY",
                "CONTRACT_VALIDATION_ONLY",
            }
        else:
            assert gate is not None
            should_run = event_name != "push" and gates[gate]
        if should_run:
            selected.append(job)
    return selected


def _release_selected_jobs(
    event_name: str,
    gates: Mapping[str, bool],
    *,
    jobs: Mapping[str, str | None] = RELEASE_JOB_GATES,
) -> list[str]:
    selected: list[str] = []
    for job, gate in jobs.items():
        should_run = (
            event_name == "push"
            if gate is None
            else (event_name != "push" and gates[gate])
        )
        if should_run:
            selected.append(job)
    return selected


def _validate_job_matrix(
    needs: Mapping[str, Any],
    *,
    jobs: Mapping[str, str | None],
    selected_jobs: Sequence[str],
) -> list[str]:
    selected = set(selected_jobs)
    errors: list[str] = []
    unselected_jobs: list[str] = []
    for name in jobs:
        expected = "success" if name in selected else "skipped"
        if expected == "skipped":
            unselected_jobs.append(name)
        try:
            actual = _job_result(needs, name)
        except GateAuthorityError as error:
            errors.append(str(error))
            continue
        if actual != expected:
            errors.append(
                f"{name} job must be {expected} by selection policy; found {actual}"
            )
    if errors:
        raise GateAuthorityError("job authority mismatch: " + "; ".join(errors))
    return unselected_jobs


def _release_graph_jobs(
    release_graph_contract: str,
) -> tuple[str, Mapping[str, str | None]]:
    if release_graph_contract == CURRENT_RELEASE_GRAPH_CONTRACT:
        return CURRENT_RELEASE_GRAPH_CONTRACT, RELEASE_JOB_GATES
    if not release_graph_contract:
        raise GateAuthorityError("release graph contract is required")
    raise GateAuthorityError(
        f"unsupported release graph contract: {release_graph_contract!r}"
    )


def validate_gate_authority(
    needs: Mapping[str, Any],
    *,
    workflow: str,
    event_name: str,
    release_graph_contract: str = "",
) -> dict[str, object]:
    """Validate classifier identity and the exact selected/skipped workflow matrix."""

    if not isinstance(needs, Mapping):
        raise GateAuthorityError("NEEDS_JSON must be an object")
    if workflow not in {"ci", "release"}:
        raise GateAuthorityError(f"unsupported workflow: {workflow!r}")
    if event_name not in SUPPORTED_EVENTS:
        raise GateAuthorityError(f"unsupported event name: {event_name!r}")

    resolved_release_graph_contract = ""
    if workflow == "ci":
        if release_graph_contract:
            raise GateAuthorityError(
                "release graph contract is not valid for the CI workflow"
            )
        jobs = CI_JOB_GATES
    else:
        resolved_release_graph_contract, jobs = _release_graph_jobs(
            release_graph_contract
        )
    _exact_keys(needs, frozenset({"classify", *jobs}), label="NEEDS_JSON")
    (
        _document,
        risk_level,
        risk_rank,
        execution_profile,
        force_full,
        gates,
    ) = _validate_classification(needs)
    if event_name in FORCE_FULL_EVENTS and not force_full:
        raise GateAuthorityError(
            f"{event_name} requires classification execution.force_full=true"
        )
    if event_name == "push" and force_full:
        raise GateAuthorityError(
            "push cannot use classification execution.force_full=true"
        )

    if workflow == "ci":
        selected_jobs = _ci_selected_jobs(event_name, execution_profile, gates)
    else:
        selected_jobs = _release_selected_jobs(event_name, gates, jobs=jobs)
    unselected_jobs = _validate_job_matrix(
        needs, jobs=jobs, selected_jobs=selected_jobs
    )

    result: dict[str, object] = {
        "status": "PASS",
        "workflow": workflow,
        "event_name": event_name,
        "schema_version": SCHEMA_VERSION,
        "risk_level": risk_level,
        "risk_rank": risk_rank,
        "execution_profile": execution_profile,
        "execution_force_full": force_full,
        "selected_jobs": selected_jobs,
        "unselected_jobs": unselected_jobs,
    }
    if workflow == "release":
        result["release_graph_contract"] = resolved_release_graph_contract
    return result


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate selection-aware AniMemo CI or Release Gate authority."
    )
    parser.add_argument("--workflow", required=True)
    parser.add_argument("--event-name", required=True)
    parser.add_argument("--release-graph-contract", default="")
    args = parser.parse_args(argv)

    try:
        raw_needs = os.getenv("NEEDS_JSON")
        needs = _object(_load_json(raw_needs, label="NEEDS_JSON"), label="NEEDS_JSON")
        result = validate_gate_authority(
            needs,
            workflow=args.workflow,
            event_name=args.event_name,
            release_graph_contract=args.release_graph_contract,
        )
    except GateAuthorityError as error:
        print(
            json.dumps(
                {
                    "status": "FAIL",
                    "code": "ci_gate_authority_failed",
                    "detail": str(error),
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2

    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
