#!/usr/bin/env python3
"""Validate AniMemo's machine-readable GitHub Actions release graph."""

from __future__ import annotations

import argparse
import json
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import yaml
from yaml.constructor import ConstructorError

CONTRACT_SCHEMA = "animemo.workflow-dag-contract/v1"
_CONTRACT_FIELDS = {
    "schemaVersion",
    "requiredWorkflows",
    "nonCancellingConcurrency",
    "mutationAuthorities",
    "skipAuthorities",
    "requiredGateReachability",
    "candidateAuthority",
}
_NEEDS_OUTPUT = re.compile(
    r"needs\.([A-Za-z0-9_-]+)\.outputs\.([A-Za-z0-9_-]+)"
)
_STEP_OUTPUT = re.compile(
    r"steps\.([A-Za-z0-9_-]+)\.outputs\.([A-Za-z0-9_-]+)"
)
_ARTIFACT_NEEDS_OUTPUT = re.compile(
    r"\$\{\{\s*needs\.([A-Za-z0-9_-]+)\.outputs\.([A-Za-z0-9_-]+)\s*\}\}"
)
_GITHUB_EXPRESSION = re.compile(r"\$\{\{\s*(.*?)\s*\}\}")


class WorkflowDagError(ValueError):
    """The workflow graph is incomplete, ambiguous, or unsafe."""


class _UniqueKeyLoader(yaml.BaseLoader):
    pass


def _construct_unique_mapping(
    loader: _UniqueKeyLoader, node: yaml.nodes.MappingNode, deep: bool = False
) -> dict[str, Any]:
    mapping: dict[str, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"found duplicate mapping key {key!r}",
                key_node.start_mark,
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


def _load_yaml(path: Path) -> Mapping[str, Any]:
    try:
        value = yaml.load(path.read_text(encoding="utf-8"), Loader=_UniqueKeyLoader)
    except (OSError, UnicodeError) as error:
        raise WorkflowDagError(f"cannot read workflow {path.as_posix()}") from error
    except ConstructorError as error:
        raise WorkflowDagError(
            f"duplicate mapping key in {path.as_posix()}: {error.problem}"
        ) from error
    if not isinstance(value, Mapping):
        raise WorkflowDagError(f"workflow must be a mapping: {path.as_posix()}")
    return value


def _listify(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return list(value)
    if isinstance(value, str):
        return [value]
    raise WorkflowDagError("needs must be a job id or an array of job ids")


def _validate_acyclic(
    workflow_path: str, jobs: Mapping[str, Any]
) -> tuple[int, dict[str, set[str]]]:
    dependencies: dict[str, set[str]] = {}
    for job_id, raw_job in jobs.items():
        assert isinstance(job_id, str) and isinstance(raw_job, Mapping)
        needs = set(_listify(raw_job.get("needs")))
        unknown = sorted(needs - set(jobs))
        if unknown:
            raise WorkflowDagError(
                f"unknown needs in {workflow_path}:{job_id}: {', '.join(unknown)}"
            )
        dependencies[job_id] = needs

    state: dict[str, int] = {}
    stack: list[str] = []

    def visit(job_id: str) -> None:
        state[job_id] = 1
        stack.append(job_id)
        for dependency in sorted(dependencies[job_id]):
            if state.get(dependency, 0) == 0:
                visit(dependency)
            elif state.get(dependency) == 1:
                first = stack.index(dependency)
                cycle = " -> ".join([*stack[first:], dependency])
                raise WorkflowDagError(
                    f"workflow DAG contains cycle in {workflow_path}: {cycle}"
                )
        stack.pop()
        state[job_id] = 2

    for job_id in sorted(jobs):
        if state.get(job_id, 0) == 0:
            visit(job_id)
    return sum(len(items) for items in dependencies.values()), dependencies


def _strings(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, Mapping):
        return [text for item in value.values() for text in _strings(item)]
    if isinstance(value, list):
        return [text for item in value for text in _strings(item)]
    return []


def _validate_output_references(
    workflow_path: str,
    jobs: Mapping[str, Any],
    dependencies: Mapping[str, set[str]],
) -> None:
    output_names: dict[str, set[str]] = {}
    for job_id, raw_job in jobs.items():
        assert isinstance(job_id, str) and isinstance(raw_job, Mapping)
        outputs = raw_job.get("outputs") or {}
        if not isinstance(outputs, Mapping):
            raise WorkflowDagError(
                f"job outputs must be a mapping: {workflow_path}:{job_id}"
            )
        output_names[job_id] = set(outputs)
        steps = raw_job.get("steps") or []
        if not isinstance(steps, list):
            raise WorkflowDagError(
                f"job steps must be an array: {workflow_path}:{job_id}"
            )
        step_ids = {
            step.get("id")
            for step in steps
            if isinstance(step, Mapping) and isinstance(step.get("id"), str)
        }
        for output_name, expression in outputs.items():
            for step_id, step_output in _STEP_OUTPUT.findall(str(expression)):
                if step_id not in step_ids:
                    raise WorkflowDagError(
                        "job output has no step producer: "
                        f"{workflow_path}:{job_id}:{output_name} -> "
                        f"{step_id}.{step_output}"
                    )

    for consumer, raw_job in jobs.items():
        assert isinstance(consumer, str) and isinstance(raw_job, Mapping)
        for text in _strings(raw_job):
            for producer, output in _NEEDS_OUTPUT.findall(text):
                if (
                    producer not in dependencies[consumer]
                    or output not in output_names.get(producer, set())
                ):
                    raise WorkflowDagError(
                        "needs output has no reachable producer: "
                        f"{workflow_path}:{consumer} -> {producer}.{output}"
                    )


def _contract_records(value: object, *, label: str) -> list[Mapping[str, Any]]:
    if not isinstance(value, list) or any(not isinstance(item, Mapping) for item in value):
        raise WorkflowDagError(f"{label} must be an array of objects")
    return list(value)


def _job_from_contract(
    workflows: Mapping[str, Mapping[str, Any]], record: Mapping[str, Any], *, label: str
) -> tuple[str, str, Mapping[str, Any]]:
    workflow = record.get("workflow")
    job_id = record.get("job")
    if not isinstance(workflow, str) or not isinstance(job_id, str):
        raise WorkflowDagError(f"{label} must identify workflow and job")
    document = workflows.get(workflow)
    jobs = document.get("jobs") if isinstance(document, Mapping) else None
    job = jobs.get(job_id) if isinstance(jobs, Mapping) else None
    if not isinstance(job, Mapping):
        raise WorkflowDagError(f"{label} references unknown job: {workflow}:{job_id}")
    return workflow, job_id, job


def _validate_mutation_authorities(
    workflows: Mapping[str, Mapping[str, Any]], contract: Mapping[str, Any]
) -> tuple[int, set[str]]:
    records = _contract_records(
        contract.get("mutationAuthorities"), label="mutationAuthorities"
    )
    domains: set[str] = set()
    authorities: set[tuple[str, str]] = set()
    authority_workflows: set[str] = set()
    for record in records:
        if set(record) != {
            "domain",
            "workflow",
            "job",
            "reconciliationMarker",
        }:
            raise WorkflowDagError("mutation authority has unknown or missing fields")
        domain = record.get("domain")
        marker = record.get("reconciliationMarker")
        if not isinstance(domain, str) or not domain or domain in domains:
            raise WorkflowDagError("mutation authority domain must be unique")
        domains.add(domain)
        workflow, job_id, job = _job_from_contract(
            workflows, record, label="mutation authority"
        )
        authority_key = (workflow, job_id)
        if authority_key in authorities:
            raise WorkflowDagError("mutation authority job must be unique")
        authorities.add(authority_key)
        authority_workflows.add(workflow)
        if "uses" in job:
            raise WorkflowDagError(
                f"mutation authority must be executable: {workflow}:{job_id}"
            )
        steps = job.get("steps")
        if not isinstance(steps, list):
            steps = []
        commands = [
            step.get("run")
            for step in steps
            if isinstance(step, Mapping) and isinstance(step.get("run"), str)
        ]
        if not isinstance(marker, str) or not marker or not any(
            marker in command for command in commands
        ):
            raise WorkflowDagError(
                "mutation authority lacks durable reconciliation: "
                f"{workflow}:{job_id}"
            )
    return len(records), authority_workflows


def _trigger_names(document: Mapping[str, Any]) -> set[str]:
    value = document.get("on")
    if isinstance(value, str):
        return {value}
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return set(value)
    if isinstance(value, Mapping):
        return set(value)
    raise WorkflowDagError("workflow on trigger must be a string, array, or mapping")


def _workflow_calls(
    workflows: Mapping[str, Mapping[str, Any]]
) -> dict[str, set[str]]:
    calls: dict[str, set[str]] = {path: set() for path in workflows}
    for workflow_path, document in workflows.items():
        jobs = document["jobs"]
        assert isinstance(jobs, Mapping)
        for raw_job in jobs.values():
            assert isinstance(raw_job, Mapping)
            reference = raw_job.get("uses")
            if not isinstance(reference, str) or not reference.startswith(
                "./.github/workflows/"
            ):
                continue
            callee = reference.removeprefix("./")
            if callee not in workflows:
                raise WorkflowDagError(
                    f"local reusable workflow is missing: {workflow_path} -> {callee}"
                )
            calls[workflow_path].add(callee)
    return calls


def _validate_workflow_call_graph(calls: Mapping[str, set[str]]) -> None:
    state: dict[str, int] = {}
    stack: list[str] = []

    def visit(workflow: str) -> None:
        state[workflow] = 1
        stack.append(workflow)
        for callee in sorted(calls[workflow]):
            if state.get(callee, 0) == 0:
                visit(callee)
            elif state.get(callee) == 1:
                first = stack.index(callee)
                cycle = " -> ".join([*stack[first:], callee])
                raise WorkflowDagError(f"reusable workflow call graph contains cycle: {cycle}")
        stack.pop()
        state[workflow] = 2

    for workflow in sorted(calls):
        if state.get(workflow, 0) == 0:
            visit(workflow)


def _validate_lane_isolation(
    workflows: Mapping[str, Mapping[str, Any]],
    calls: Mapping[str, set[str]],
    mutation_workflows: set[str],
) -> None:
    pr_events = {"pull_request", "pull_request_target", "merge_group"}
    roots = [
        path
        for path, document in workflows.items()
        if _trigger_names(document) & pr_events
    ]
    for root in roots:
        reached = {root}
        pending = [root]
        while pending:
            workflow = pending.pop()
            for callee in calls[workflow]:
                if callee not in reached:
                    reached.add(callee)
                    pending.append(callee)
        unsafe = sorted(reached & mutation_workflows)
        if unsafe:
            raise WorkflowDagError(
                "pull-request lane reaches mutation authority: "
                f"{root} -> {', '.join(unsafe)}"
            )


def _artifact_operation(step: Mapping[str, Any]) -> str | None:
    action = step.get("uses")
    if not isinstance(action, str):
        return None
    if action.startswith("actions/upload-artifact@"):
        return "upload"
    if action.startswith("actions/download-artifact@"):
        return "download"
    return None


def _canonical_artifact_name(
    workflow_path: str,
    consumer: str,
    name: str,
    jobs: Mapping[str, Any],
) -> str:
    """Resolve a consumer's job-output alias to the producer's step identity."""

    def resolve(match: re.Match[str]) -> str:
        producer_id, output_name = match.groups()
        producer = jobs.get(producer_id)
        outputs = producer.get("outputs") if isinstance(producer, Mapping) else None
        output = outputs.get(output_name) if isinstance(outputs, Mapping) else None
        if not isinstance(output, str) or not output:
            raise WorkflowDagError(
                "artifact name references an unknown job output: "
                f"{workflow_path}:{consumer}:{producer_id}.{output_name}"
            )
        return output

    resolved = _ARTIFACT_NEEDS_OUTPUT.sub(resolve, name)
    return _GITHUB_EXPRESSION.sub(
        lambda match: "${{ " + match.group(1).strip() + " }}", resolved
    )


def _validate_artifact_flows(
    workflows: Mapping[str, Mapping[str, Any]],
    dependencies: Mapping[str, Mapping[str, set[str]]],
) -> tuple[int, int]:
    producers: dict[str, list[tuple[str, str]]] = {
        workflow_path: [] for workflow_path in workflows
    }
    consumers: list[tuple[str, str, str]] = []
    for workflow_path, document in workflows.items():
        jobs = document["jobs"]
        assert isinstance(jobs, Mapping)
        for job_id, raw_job in jobs.items():
            assert isinstance(job_id, str) and isinstance(raw_job, Mapping)
            steps = raw_job.get("steps") or []
            if not isinstance(steps, list):
                continue
            for step in steps:
                if not isinstance(step, Mapping):
                    continue
                operation = _artifact_operation(step)
                if operation is None:
                    continue
                inputs = step.get("with")
                name = inputs.get("name") if isinstance(inputs, Mapping) else None
                if not isinstance(name, str) or not name:
                    raise WorkflowDagError(
                        "artifact action requires an exact name: "
                        f"{workflow_path}:{job_id}"
                    )
                if operation == "upload":
                    producers[workflow_path].append((job_id, name))
                else:
                    consumers.append((workflow_path, job_id, name))

    for workflow_path, consumer, name in consumers:
        document = workflows[workflow_path]
        jobs = document["jobs"]
        assert isinstance(jobs, Mapping)
        resolved_name = _canonical_artifact_name(
            workflow_path, consumer, name, jobs
        )
        matches = [
            producer
            for producer, produced_name in producers[workflow_path]
            if _canonical_artifact_name(
                workflow_path, producer, produced_name, jobs
            )
            == resolved_name
        ]
        if len(matches) != 1:
            raise WorkflowDagError(
                "artifact consumer requires exactly one producer: "
                f"{workflow_path}:{consumer}:{name}"
            )
        producer = matches[0]
        reached: set[str] = set()
        pending = list(dependencies[workflow_path][consumer])
        while pending:
            job_id = pending.pop()
            if job_id in reached:
                continue
            reached.add(job_id)
            pending.extend(dependencies[workflow_path][job_id])
        if producer not in reached:
            raise WorkflowDagError(
                "artifact producer is not reachable through needs: "
                f"{workflow_path}:{producer} -> {consumer}:{name}"
            )
    return sum(len(items) for items in producers.values()), len(consumers)


def _validate_skip_authorities(
    workflows: Mapping[str, Mapping[str, Any]], contract: Mapping[str, Any]
) -> int:
    records = _contract_records(
        contract.get("skipAuthorities"), label="skipAuthorities"
    )
    authorities: set[tuple[str, str]] = set()
    for record in records:
        if set(record) != {
            "workflow",
            "job",
            "covers",
            "allowedSkippedJobs",
            "conditionMarker",
        }:
            raise WorkflowDagError("skip authority has unknown or missing fields")
        workflow, job_id, authority = _job_from_contract(
            workflows, record, label="skip authority"
        )
        authority_key = (workflow, job_id)
        if authority_key in authorities:
            raise WorkflowDagError("skip authority job must be unique")
        authorities.add(authority_key)
        document = workflows[workflow]
        jobs = document["jobs"]
        assert isinstance(jobs, Mapping)
        covers = record.get("covers")
        allowed = record.get("allowedSkippedJobs")
        marker = record.get("conditionMarker")
        if (
            not isinstance(covers, list)
            or not covers
            or any(not isinstance(item, str) for item in covers)
            or len(covers) != len(set(covers))
            or not isinstance(allowed, list)
            or any(not isinstance(item, str) for item in allowed)
            or len(allowed) != len(set(allowed))
            or not set(allowed).issubset(covers)
        ):
            raise WorkflowDagError("skip authority coverage is invalid")
        if not set(covers).issubset(jobs):
            raise WorkflowDagError("skip authority covers unknown job")
        needs = set(_listify(authority.get("needs")))
        if set(covers) != needs:
            raise WorkflowDagError(
                f"skip authority cannot observe every covered job: {workflow}:{job_id}"
            )
        condition = authority.get("if")
        if (
            not isinstance(marker, str)
            or not marker
            or not isinstance(condition, str)
            or marker not in condition
        ):
            raise WorkflowDagError(
                "skip authority must run under its condition marker: "
                f"{workflow}:{job_id}"
            )
        conditionally_skippable = {
            covered
            for covered in covers
            if isinstance(jobs[covered], Mapping) and "if" in jobs[covered]
        }
        if conditionally_skippable != set(allowed):
            raise WorkflowDagError(
                f"skip authority allowed skip set is incomplete: {workflow}:{job_id}"
            )
    return len(records)


def _transitive_dependencies(
    dependencies: Mapping[str, set[str]], job_id: str
) -> set[str]:
    reached: set[str] = set()
    pending = list(dependencies[job_id])
    while pending:
        dependency = pending.pop()
        if dependency in reached:
            continue
        reached.add(dependency)
        pending.extend(dependencies[dependency])
    return reached


def _validate_required_gate_reachability(
    workflows: Mapping[str, Mapping[str, Any]],
    dependencies: Mapping[str, Mapping[str, set[str]]],
    contract: Mapping[str, Any],
) -> int:
    records = _contract_records(
        contract.get("requiredGateReachability"),
        label="requiredGateReachability",
    )
    consumers: set[tuple[str, str]] = set()
    gate_count = 0
    for record in records:
        if set(record) != {"workflow", "job", "requiredGates"}:
            raise WorkflowDagError(
                "required gate reachability has unknown or missing fields"
            )
        workflow, job_id, _ = _job_from_contract(
            workflows, record, label="required gate consumer"
        )
        consumer = (workflow, job_id)
        if consumer in consumers:
            raise WorkflowDagError("required gate consumer must be unique")
        consumers.add(consumer)
        required = record.get("requiredGates")
        jobs = workflows[workflow]["jobs"]
        assert isinstance(jobs, Mapping)
        if (
            not isinstance(required, list)
            or not required
            or any(not isinstance(item, str) or not item for item in required)
            or len(required) != len(set(required))
            or job_id in required
            or not set(required).issubset(jobs)
        ):
            raise WorkflowDagError("required gate inventory is invalid")
        reached = _transitive_dependencies(dependencies[workflow], job_id)
        missing = sorted(set(required) - reached)
        if missing:
            raise WorkflowDagError(
                "required gate is unreachable: "
                f"{workflow}:{job_id} -> {', '.join(missing)}"
            )
        gate_count += len(required)
    return gate_count


def _validate_candidate_authority(
    workflows: Mapping[str, Mapping[str, Any]], contract: Mapping[str, Any]
) -> tuple[str, int]:
    value = contract.get("candidateAuthority")
    if not isinstance(value, Mapping) or set(value) != {
        "producer",
        "consumers",
        "producerMarkers",
        "forbiddenConsumerBuildMarkers",
        "nonAuthoritativeBuilds",
    }:
        raise WorkflowDagError("candidateAuthority has unknown or missing fields")
    producer_record = value["producer"]
    if not isinstance(producer_record, Mapping) or set(producer_record) != {
        "workflow",
        "job",
        "annotation",
    }:
        raise WorkflowDagError("candidate authority producer is invalid")
    producer_workflow, producer_job_id, producer = _job_from_contract(
        workflows, producer_record, label="candidate authority producer"
    )
    annotation = producer_record.get("annotation")
    if not isinstance(annotation, str) or not annotation:
        raise WorkflowDagError("candidate authority annotation is invalid")
    producer_env = producer.get("env")
    if (
        not isinstance(producer_env, Mapping)
        or producer_env.get("ANIMEMO_CANDIDATE_BUILD_AUTHORITY") != annotation
    ):
        raise WorkflowDagError("candidate authority producer annotation is missing")
    markers = value["producerMarkers"]
    forbidden = value["forbiddenConsumerBuildMarkers"]
    if (
        not isinstance(markers, list)
        or not markers
        or any(not isinstance(item, str) or not item for item in markers)
        or not isinstance(forbidden, list)
        or not forbidden
        or any(not isinstance(item, str) or not item for item in forbidden)
    ):
        raise WorkflowDagError("candidate authority marker inventory is invalid")
    producer_text = "\n".join(_strings(producer))
    missing_markers = [marker for marker in markers if marker not in producer_text]
    if missing_markers:
        raise WorkflowDagError("candidate authority producer markers are incomplete")

    annotated: list[str] = []
    for workflow_path, document in workflows.items():
        jobs = document.get("jobs")
        assert isinstance(jobs, Mapping)
        for job_id, raw_job in jobs.items():
            assert isinstance(job_id, str) and isinstance(raw_job, Mapping)
            env = raw_job.get("env")
            if (
                isinstance(env, Mapping)
                and env.get("ANIMEMO_CANDIDATE_BUILD_AUTHORITY") == annotation
            ):
                annotated.append(f"{workflow_path}:{job_id}")
    expected_producer = f"{producer_workflow}:{producer_job_id}"
    if annotated != [expected_producer]:
        raise WorkflowDagError("candidate byte authority must have exactly one producer")

    consumers = _contract_records(value["consumers"], label="candidate consumers")
    consumer_keys: set[tuple[str, str]] = set()
    for record in consumers:
        if set(record) != {"workflow", "job"}:
            raise WorkflowDagError("candidate consumer has unknown or missing fields")
        workflow, job_id, job = _job_from_contract(
            workflows, record, label="candidate consumer"
        )
        consumer_key = (workflow, job_id)
        if consumer_key in consumer_keys:
            raise WorkflowDagError("candidate consumer must be unique")
        consumer_keys.add(consumer_key)
        text = "\n".join(_strings(job))
        present = [marker for marker in forbidden if marker in text]
        if present:
            raise WorkflowDagError(
                "candidate authority consumer cannot build: "
                f"{workflow}:{job_id}:{','.join(present)}"
            )
        env = job.get("env")
        if isinstance(env, Mapping) and env.get(
            "ANIMEMO_CANDIDATE_BUILD_AUTHORITY"
        ) == annotation:
            raise WorkflowDagError("candidate consumer cannot claim byte authority")

    non_authoritative = _contract_records(
        value["nonAuthoritativeBuilds"], label="nonAuthoritativeBuilds"
    )
    non_authoritative_keys: set[tuple[str, str]] = set()
    for record in non_authoritative:
        if set(record) != {"workflow", "job", "annotation"}:
            raise WorkflowDagError(
                "non-authoritative build has unknown or missing fields"
            )
        workflow, job_id, job = _job_from_contract(
            workflows, record, label="non-authoritative build"
        )
        non_authoritative_key = (workflow, job_id)
        if non_authoritative_key in non_authoritative_keys:
            raise WorkflowDagError("non-authoritative build must be unique")
        non_authoritative_keys.add(non_authoritative_key)
        expected_annotation = record.get("annotation")
        env = job.get("env")
        if (
            expected_annotation != "NON_AUTHORITATIVE_ISOLATED_TEST_ONLY"
            or not isinstance(env, Mapping)
            or env.get("ANIMEMO_CANDIDATE_BUILD_AUTHORITY")
            != expected_annotation
        ):
            raise WorkflowDagError(
                f"non-authoritative build annotation is missing: {workflow}:{job_id}"
            )
        for text in _strings(job.get("steps", [])):
            if any(
                marker in text
                for marker in (
                    "release-qualification/",
                    "candidate-runtime/",
                    "installer-materials.tar",
                    "prepublication-materials.json",
                )
            ) and "actions/upload-artifact@" in "\n".join(_strings(job)):
                raise WorkflowDagError(
                    "non-authoritative build can upload candidate authority bytes: "
                    f"{workflow}:{job_id}"
                )
    annotated_non_authoritative: set[tuple[str, str]] = set()
    for workflow_path, document in workflows.items():
        jobs = document.get("jobs")
        assert isinstance(jobs, Mapping)
        for job_id, raw_job in jobs.items():
            assert isinstance(job_id, str) and isinstance(raw_job, Mapping)
            env = raw_job.get("env")
            if (
                isinstance(env, Mapping)
                and env.get("ANIMEMO_CANDIDATE_BUILD_AUTHORITY")
                == "NON_AUTHORITATIVE_ISOLATED_TEST_ONLY"
            ):
                annotated_non_authoritative.add((workflow_path, job_id))
    if annotated_non_authoritative != non_authoritative_keys:
        raise WorkflowDagError(
            "non-authoritative build inventory is incomplete or stale"
        )
    return expected_producer, len(non_authoritative)

def validate_repository(
    root: Path, contract_path: Path | None = None
) -> dict[str, object]:
    """Validate the checked-out repository and return a JSON-safe receipt."""

    root = Path(root).resolve()
    contract_file = contract_path or root / "release" / "workflow-dag-contract.json"
    try:
        contract = json.loads(contract_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise WorkflowDagError("workflow DAG contract is unreadable") from error
    if not isinstance(contract, Mapping):
        raise WorkflowDagError("workflow DAG contract must be an object")
    if set(contract) != _CONTRACT_FIELDS:
        raise WorkflowDagError("workflow DAG contract has unknown or missing fields")
    if contract.get("schemaVersion") != CONTRACT_SCHEMA:
        raise WorkflowDagError("workflow DAG contract schema is unsupported")

    workflow_directory = root / ".github" / "workflows"
    paths = sorted(workflow_directory.glob("*.yml"))
    if not paths:
        raise WorkflowDagError("no workflow files found")
    workflows: dict[str, Mapping[str, Any]] = {}
    for path in paths:
        relative = path.relative_to(root).as_posix()
        workflows[relative] = _load_yaml(path)

    required = contract.get("requiredWorkflows")
    if not isinstance(required, list) or any(
        not isinstance(item, str) or not item for item in required
    ):
        raise WorkflowDagError("requiredWorkflows must be an array of paths")
    if sorted(required) != sorted(workflows):
        raise WorkflowDagError("requiredWorkflows must enumerate every workflow exactly")

    non_cancelling = contract.get("nonCancellingConcurrency")
    if not isinstance(non_cancelling, list) or any(
        not isinstance(item, str) for item in non_cancelling
    ) or len(non_cancelling) != len(set(non_cancelling)):
        raise WorkflowDagError("nonCancellingConcurrency must be an array of paths")
    if not set(non_cancelling).issubset(workflows):
        raise WorkflowDagError("nonCancellingConcurrency references unknown workflow")
    for workflow_path, document in workflows.items():
        concurrency = document.get("concurrency")
        if (
            not isinstance(concurrency, Mapping)
            or not isinstance(concurrency.get("group"), str)
            or not concurrency["group"].strip()
            or "cancel-in-progress" not in concurrency
        ):
            raise WorkflowDagError(
                f"workflow requires closed concurrency: {workflow_path}"
            )
        if (
            workflow_path in non_cancelling
            and str(concurrency.get("cancel-in-progress")).lower() != "false"
        ):
            raise WorkflowDagError(
                f"mutation workflow concurrency cannot cancel: {workflow_path}"
            )

    executable_jobs = 0
    edge_count = 0
    dependency_graphs: dict[str, Mapping[str, set[str]]] = {}
    for workflow_path, document in workflows.items():
        jobs = document.get("jobs")
        if not isinstance(jobs, Mapping) or not jobs:
            raise WorkflowDagError(f"workflow has no jobs: {workflow_path}")
        workflow_edges, dependencies = _validate_acyclic(workflow_path, jobs)
        edge_count += workflow_edges
        dependency_graphs[workflow_path] = dependencies
        _validate_output_references(workflow_path, jobs, dependencies)
        for job_id, raw_job in jobs.items():
            if not isinstance(job_id, str) or not isinstance(raw_job, Mapping):
                raise WorkflowDagError(f"workflow job is invalid: {workflow_path}")
            if "uses" in raw_job:
                continue
            executable_jobs += 1
            timeout = raw_job.get("timeout-minutes")
            if not isinstance(timeout, str) or not re.fullmatch(
                r"[1-9][0-9]*", timeout
            ):
                raise WorkflowDagError(
                    "executable job requires timeout-minutes: "
                    f"{workflow_path}:{job_id}"
                )
    mutation_authority_count, mutation_workflows = _validate_mutation_authorities(
        workflows, contract
    )
    calls = _workflow_calls(workflows)
    _validate_workflow_call_graph(calls)
    _validate_lane_isolation(workflows, calls, mutation_workflows)
    artifact_producer_count, artifact_consumer_count = _validate_artifact_flows(
        workflows, dependency_graphs
    )
    skip_authority_count = _validate_skip_authorities(workflows, contract)
    required_gate_count = _validate_required_gate_reachability(
        workflows, dependency_graphs, contract
    )
    candidate_producer, non_authoritative_build_count = _validate_candidate_authority(
        workflows, contract
    )
    return {
        "schemaVersion": "animemo.workflow-dag-validation-receipt/v1",
        "status": "PASS",
        "workflowCount": len(paths),
        "executableJobCount": executable_jobs,
        "edgeCount": edge_count,
        "cycleCount": 0,
        "unknownNeedCount": 0,
        "jobWithoutExplicitTimeoutCount": 0,
        "mutationAuthorityCount": mutation_authority_count,
        "artifactProducerCount": artifact_producer_count,
        "artifactConsumerCount": artifact_consumer_count,
        "skipAuthorityCount": skip_authority_count,
        "requiredGateCount": required_gate_count,
        "unreachableGateCount": 0,
        "candidateAuthorityProducer": candidate_producer,
        "nonAuthoritativeBuildCount": non_authoritative_build_count,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--contract", type=Path)
    args = parser.parse_args(argv)
    try:
        receipt = validate_repository(args.root, args.contract)
    except WorkflowDagError as error:
        print(
            json.dumps(
                {
                    "schemaVersion": "animemo.workflow-dag-validation-receipt/v1",
                    "status": "FAIL",
                    "detail": str(error),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 2
    print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
