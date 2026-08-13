#!/usr/bin/env python3
"""Fail-closed, selection-aware authority for AniMemo CI workflow jobs."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Mapping, Sequence
from typing import Any

SCHEMA_VERSION = "animemo.ci-risk/v1"
RISK_RANKS = {"LOW": 1, "STANDARD": 2, "HIGH": 3, "CRITICAL": 4}

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
EXECUTION_KEYS = frozenset({"force_full", "reasons"})
SIGNAL_NAMES = (
    "frontend",
    "backend",
    "auth",
    "api_contract",
    "plugin",
    "integration",
    "bridge",
    "migration",
    "dependencies",
    "ci",
    "deployment",
    "shared_contract",
    "first_run",
    "recovery",
    "media_storage",
    "tooling",
)
MIXED_SIGNAL_NAMES = SIGNAL_NAMES[:-1]

GATE_NAMES = (
    "docs_only",
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
    "full_gate",
    "critical_gate",
)
CLASSIFIER_OUTPUT_NAMES = (
    "schema_version",
    "risk_level",
    "risk_rank",
    "execution_force_full",
    "classification_json",
    *GATE_NAMES,
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
    "dr-rehearsal": "run_release_stateful",
}
LEGACY_RELEASE_JOB_GATES: dict[str, str | None] = {
    name: gate for name, gate in RELEASE_JOB_GATES.items() if name != "dr-rehearsal"
}
TRUSTED_PRE_MERGE_WORKFLOW_REF = (
    "yanyuhanyue/AniMemo/.github/workflows/pre-merge-full.yml@refs/heads/main"
)

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
) -> tuple[Mapping[str, Any], str, int, bool, dict[str, bool]]:
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
    force_full = _json_bool(
        _field(execution, "force_full", label="classification_json.execution"),
        label="classification_json.execution.force_full",
    )
    _list(
        _field(execution, "reasons", label="classification_json.execution"),
        label="classification_json.execution.reasons",
    )

    _list(
        _field(document, "paths", label="classification_json"),
        label="classification_json.paths",
    )
    _list(
        _field(document, "unknown_paths", label="classification_json"),
        label="classification_json.unknown_paths",
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

    if gates["docs_only"] and (risk_level != "LOW" or force_full):
        raise GateAuthorityError(
            "classification_json.gates.docs_only must represent intrinsic LOW, "
            "non-forced documentation-only risk"
        )

    expected_mixed = sum(signal_values[name] for name in MIXED_SIGNAL_NAMES) > 1
    if gates["mixed"] != expected_mixed:
        raise GateAuthorityError(
            "classification_json.gates.mixed does not match the primary signals: "
            f"expected {expected_mixed}, found {gates['mixed']}"
        )

    expected_full_gate = force_full or risk_rank >= RISK_RANKS["HIGH"]
    if gates["full_gate"] != expected_full_gate:
        raise GateAuthorityError(
            "classification_json.gates.full_gate violates risk policy: "
            f"expected {expected_full_gate}, found {gates['full_gate']}"
        )
    expected_critical_gate = force_full or risk_level == "CRITICAL"
    if gates["critical_gate"] != expected_critical_gate:
        raise GateAuthorityError(
            "classification_json.gates.critical_gate violates risk policy: "
            f"expected {expected_critical_gate}, found {gates['critical_gate']}"
        )

    release_expectations = {
        "run_release_full": gates["full_gate"],
        "run_release_docker": gates["full_gate"],
        "run_release_stateful": gates["full_gate"],
        "run_release_updater": gates["critical_gate"],
    }
    for name, expected in release_expectations.items():
        if gates[name] != expected:
            raise GateAuthorityError(
                f"classification_json.gates.{name} violates release policy: "
                f"expected {expected}, found {gates[name]}"
            )

    if gates["full_gate"]:
        omitted = [name for name in PRODUCT_GATE_NAMES if not gates[name]]
        if omitted:
            raise GateAuthorityError(
                "classification_json full_gate omitted product gates: "
                + ", ".join(omitted)
            )
        if gates["docs_only"]:
            raise GateAuthorityError(
                "classification_json full_gate cannot also select docs_only"
            )

    _scalar(outputs, "schema_version", SCHEMA_VERSION)
    _scalar(outputs, "risk_level", risk_level)
    _scalar(outputs, "risk_rank", str(risk_rank))
    _scalar(outputs, "execution_force_full", str(force_full).lower())
    for name, value in gates.items():
        _scalar(outputs, name, str(value).lower())

    return document, risk_level, risk_rank, force_full, gates


def _ci_selected_jobs(event_name: str, gates: Mapping[str, bool]) -> list[str]:
    docs_only = gates["docs_only"]
    selected: list[str] = []
    for job, gate in CI_JOB_GATES.items():
        if job == "fast-fail":
            should_run = event_name != "push" and not docs_only
        elif job == "docs-only":
            should_run = docs_only
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


def validate_gate_authority(
    needs: Mapping[str, Any],
    *,
    workflow: str,
    event_name: str,
    workflow_ref: str = "",
) -> dict[str, object]:
    """Validate classifier identity and the exact selected/skipped workflow matrix."""

    if not isinstance(needs, Mapping):
        raise GateAuthorityError("NEEDS_JSON must be an object")
    if workflow not in {"ci", "release"}:
        raise GateAuthorityError(f"unsupported workflow: {workflow!r}")
    if event_name not in SUPPORTED_EVENTS:
        raise GateAuthorityError(f"unsupported event name: {event_name!r}")

    job_set = "current"
    jobs = CI_JOB_GATES if workflow == "ci" else RELEASE_JOB_GATES
    if workflow == "release" and event_name == "workflow_call":
        legacy_keys = frozenset({"classify", *LEGACY_RELEASE_JOB_GATES})
        if (
            frozenset(needs) == legacy_keys
            and workflow_ref == TRUSTED_PRE_MERGE_WORKFLOW_REF
        ):
            jobs = LEGACY_RELEASE_JOB_GATES
            job_set = "legacy-main-without-dr-rehearsal"
    _exact_keys(needs, frozenset({"classify", *jobs}), label="NEEDS_JSON")
    _document, risk_level, risk_rank, force_full, gates = _validate_classification(
        needs
    )
    if event_name in FORCE_FULL_EVENTS and not force_full:
        raise GateAuthorityError(
            f"{event_name} requires classification execution.force_full=true"
        )
    if event_name == "push" and force_full:
        raise GateAuthorityError(
            "push cannot use classification execution.force_full=true"
        )

    if workflow == "ci":
        selected_jobs = _ci_selected_jobs(event_name, gates)
    else:
        selected_jobs = _release_selected_jobs(event_name, gates, jobs=jobs)
    unselected_jobs = _validate_job_matrix(
        needs, jobs=jobs, selected_jobs=selected_jobs
    )

    return {
        "status": "PASS",
        "workflow": workflow,
        "event_name": event_name,
        "schema_version": SCHEMA_VERSION,
        "risk_level": risk_level,
        "risk_rank": risk_rank,
        "execution_force_full": force_full,
        "job_set": job_set,
        "selected_jobs": selected_jobs,
        "unselected_jobs": unselected_jobs,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate selection-aware AniMemo CI or Release Gate authority."
    )
    parser.add_argument("--workflow", required=True)
    parser.add_argument("--event-name", required=True)
    args = parser.parse_args(argv)

    try:
        raw_needs = os.getenv("NEEDS_JSON")
        needs = _object(_load_json(raw_needs, label="NEEDS_JSON"), label="NEEDS_JSON")
        result = validate_gate_authority(
            needs,
            workflow=args.workflow,
            event_name=args.event_name,
            workflow_ref=os.getenv("GITHUB_WORKFLOW_REF", ""),
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
