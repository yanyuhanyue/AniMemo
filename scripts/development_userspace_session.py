"""One DEV VM owner with at most four separately held, fixed userspace calls."""

from __future__ import annotations

import argparse
from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

from release.materials import read_bounded_release_file
from scripts import candidate_vm_harness as h
from scripts import development_userspace_probe as single
from scripts.development_plan import from_material_plan
from scripts.development_source import acquire_development_source
from scripts.development_userspace_inputs import (
    PURPOSE,
    TEST_IDS,
    EXTRA_TEST_IDS,
    read_inputs,
)
from scripts.guest_batch_scope import authorization_root
from scripts.isolated_guest_validation import _check_checkout

MODES = frozenset({"DIAGNOSE_TRUNCATION", "FINAL_SET"})
MAX_ROUNDS = 4
MAX_LIFETIME = 4 * 60 * 60
MAX_IDLE = 600
ROOT = Path(__file__).resolve().parents[1]
TRUNCATION_REGRESSION_IDS = tuple(
    "updater.tests.test_archive_handoff.ArchiveHandoffBoundaryTests." + name
    for name in (
        "test_posix_truncation_inner_rejection_cannot_be_masked_by_outer",
        "test_posix_truncation_wrong_rejection_type_or_code_cannot_pass",
        "test_posix_truncation_unrelated_cleanup_failure_is_not_expected",
        "test_real_member_api_rejects_outer_archive_and_context_keeps_original_alive",
        "test_second_borrow_and_closed_owner_rejected",
        "test_cross_source_and_same_value_foreign_materials_rejected",
    )
)


def require(value, code="DEVELOPMENT_USERSPACE_SESSION_INVALID"):
    if not value:
        raise h.CandidateHarnessError(code)


def write_new(path, value):
    with Path(path).open("xb") as stream:
        stream.write(h.canonical_json_bytes(value))
        stream.flush()
        os.fsync(stream.fileno())


def controller_files():
    """The already imported controller remains at its initial source revision."""
    files = {}
    for module in tuple(sys.modules.values()):
        name = getattr(module, "__file__", None)
        if type(name) is not str:
            continue
        path = Path(name).resolve()
        if path.suffix == ".py" and path.is_relative_to(ROOT):
            files[path.relative_to(ROOT).as_posix()] = h._hash_regular_file(path)
    require(0 < len(files) <= 512)
    require(
        not {
            "scripts/userspace_platform_probe.py",
            "scripts/userspace_test_result.py",
        }.intersection(files),
        "DEVELOPMENT_USERSPACE_PAYLOAD_IMPORTED_BY_CONTROLLER",
    )
    return files


def check_controller(files):
    require(
        all(
            h._hash_regular_file(ROOT / name) == digest
            for name, digest in files.items()
        ),
        "DEVELOPMENT_USERSPACE_CONTROLLER_CHANGED",
    )


class Limits:
    def __init__(self, expires, root):
        self.root = Path(root)
        self.deadline = time.monotonic() + min(
            MAX_LIFETIME, max(0, (expires - datetime.now(timezone.utc)).total_seconds())
        )
        self.expires = expires
        self.cancelled = threading.Event()
        self.reason = None
        self.rounds = 0

    def check(self):
        if self.root.joinpath("stop.json").exists():
            self.reason = "STOP_REQUEST"
            self.cancelled.set()
        if (
            time.monotonic() >= self.deadline
            or datetime.now(timezone.utc) >= self.expires
        ):
            self.reason = "SESSION_DEADLINE"
            self.cancelled.set()
        require(not self.cancelled.is_set(), "DEVELOPMENT_USERSPACE_SESSION_REVOKED")

    def remaining(self, limit=600):
        self.check()
        remaining = min(
            limit,
            int(self.deadline - time.monotonic()),
            int((self.expires - datetime.now(timezone.utc)).total_seconds()),
        )
        require(remaining > 0, "DEVELOPMENT_USERSPACE_SESSION_REVOKED")
        return remaining

    def reserve_round(self, identity):
        self.check()
        require(self.rounds < MAX_ROUNDS, "DEVELOPMENT_USERSPACE_ROUND_LIMIT")
        ordinal = self.rounds + 1
        write_new(
            self.root / f"round-{ordinal:02d}.started.json",
            {
                "schema": "animemo.userspace-workload-started/v1",
                "ordinal": ordinal,
                "started_utc": datetime.now(timezone.utc).isoformat(),
                **identity,
            },
        )
        self.rounds = ordinal
        return ordinal


@contextmanager
def supervised_commands(provider, limits):
    """Use the existing process owner, without weakening Provider construction."""
    runner = provider._runner
    require(type(runner) is h.SubprocessHostCommandRunner)
    original = runner.run
    finished = threading.Event()

    def watch():
        while not finished.wait(0.25):
            try:
                limits.check()
            except h.CandidateHarnessError:
                return
            except OSError:
                limits.reason = "CONTROL_CHANNEL_UNAVAILABLE"
                limits.cancelled.set()
                return

    def run(argv, **kwargs):
        vmrun = Path(argv[0]).name.casefold() == "vmrun.exe"
        cleanup = vmrun and (
            tuple(argv[1:]) == ("-T", "ws", "list")
            or len(argv) == 6
            and tuple(argv[1:4]) == ("-T", "ws", "stop")
            and argv[-1] == "soft"
        )
        if not cleanup:
            kwargs["timeout"] = limits.remaining(kwargs.get("timeout", 300))
            kwargs["cancel_event"] = limits.cancelled
            # Killing a command Job must never kill vmware-vmx.
            kwargs["cancel_tree"] = not vmrun
            require(
                kwargs.get("input_bytes") is None,
                "DEVELOPMENT_USERSPACE_UNEXPECTED_COMMAND_INPUT",
            )
        return original(argv, **kwargs)

    thread = threading.Thread(target=watch, daemon=True)
    runner.run = run
    thread.start()
    try:
        yield
    finally:
        finished.set()
        thread.join(timeout=2)
        runner.run = original
        require(not thread.is_alive(), "DEVELOPMENT_USERSPACE_WATCHER_UNCLOSED")


def require_lease(lease):
    lease.require_open()
    require(
        lease.path.is_file()
        and not lease.path.is_symlink()
        and h._file_identity(lease.path.lstat()) == lease.file_identity,
        "DEVELOPMENT_USERSPACE_LEASE_CHANGED",
    )


def require_guest(provider, authority, verified, lease):
    require_lease(lease)
    owned = os.path.normcase(str(authority.clone_vmx.resolve(strict=False)))
    require(
        provider._running_vmx_paths() == frozenset({owned}),
        "DEVELOPMENT_USERSPACE_TARGET_CHANGED",
    )
    host_key = provider._read_known_host_key(authority)
    require(
        host_key == verified.guest.host_key_digest,
        "DEVELOPMENT_USERSPACE_TARGET_CHANGED",
    )
    observed = provider._observe_guest_connection(
        authority, host_key_digest=host_key, bootstrap_identity=True
    )
    require(observed == verified.guest, "DEVELOPMENT_USERSPACE_TARGET_CHANGED")


def test_group_passed(group, ids):
    return (
        test_group_closed(group, ids)
        and type(group.get("run")) is int
        and group["run"] == len(ids)
        and type(group.get("errors")) is int
        and group["errors"] == 0
        and type(group.get("failures")) is int
        and group["failures"] == 0
        and group.get("skips") == []
        and group.get("successful") is True
        and type(group.get("records")) is list
        and [
            (item.get("id"), item.get("status"), item.get("diagnostics"))
            for item in group["records"]
        ]
        == [(name, "PASS", []) for name in ids]
    )


def test_group_closed(group, ids):
    if not (
        type(group) is dict
        and group.get("tests") == list(ids)
        and all(
            group.get(key) is False
            for key in (
                "projection_overflow",
                "projection_fault",
                "aborted",
                "effect_violation",
            )
        )
        and type(group.get("fixture_retained_owners")) is int
        and group["fixture_retained_owners"] == 0
        and type(group.get("run")) is int
        and group["run"] == len(ids)
        and type(group.get("errors")) is int
        and group["errors"] >= 0
        and type(group.get("failures")) is int
        and group["failures"] >= 0
        and group.get("successful") is (group["errors"] == group["failures"] == 0)
        and type(group.get("records")) is list
        and len(group["records"]) == len(ids)
        and group.get("skips") == []
    ):
        return False
    counted = {"ERROR": 0, "FAIL": 0}
    for record, name in zip(group["records"], ids):
        if (
            type(record) is not dict
            or record.get("id") != name
            or record.get("status") not in {"PASS", "FAIL", "ERROR"}
            or type(record.get("diagnostics")) is not list
        ):
            return False
        if record["status"] == "PASS" and record["diagnostics"] != []:
            return False
        if record["status"] != "PASS" and not record["diagnostics"]:
            return False
        expected = (
            "ERROR"
            if any(
                event.get("status") == "ERROR"
                for event in record["diagnostics"]
                if type(event) is dict
            )
            else "FAIL"
            if record["diagnostics"]
            else "PASS"
        )
        if record["status"] != expected:
            return False
        for event in record["diagnostics"]:
            if (
                type(event) is not dict
                or event.get("status") not in counted
                or event.get("phase") not in {"TEST_EXECUTION", "TEST_CONSTRUCTION"}
            ):
                return False
            counted[event["status"]] += 1
            projection = event.get("exception", {})
            if (
                type(projection) is not dict
                or projection.get("limited") is not False
                or type(projection.get("chain")) is not list
                or not 0 < len(projection["chain"]) <= 4
            ):
                return False
            for item in projection["chain"]:
                if (
                    type(item) is not dict
                    or item.get("type")
                    not in {
                        "AssertionError",
                        "TypeError",
                        "ValueError",
                        "ImportError",
                        "ModuleNotFoundError",
                    }
                    or item.get("code") != "UNKNOWN"
                    or item.get("secondary_codes") != []
                ):
                    return False
    return counted["ERROR"] == group["errors"] and counted["FAIL"] == group["failures"]


def exact_projection(value, expected):
    return (
        type(value) is dict
        and value.get("limited") is False
        and value.get("chain")
        == [
            {"relation": relation, "type": kind, "code": code, "secondary_codes": []}
            for relation, kind, code in expected
        ]
    )


def validate_report(value, request):
    """A valid ordinary failure is not a successful test or an invalid protocol."""
    require(
        type(value) is dict
        and value.get("schema") == "animemo.development-userspace-result/v1"
        and value.get("purpose") == PURPOSE
        and value.get("workload_mode") == request["mode"]
        and value.get("source_sha") == request["source_sha"]
        and value.get("source_tree") == request["source_tree"]
        and value.get("input_manifest_sha256") == request["inputs_sha256"]
        and type(value.get("sudo_capture_attempts")) is int
        and value["sudo_capture_attempts"] == 0
        and type(value.get("installer_executions")) is int
        and value["installer_executions"] == 0
        and type(value.get("uid")) is int
        and value["uid"] > 0
        and value.get("platform") == "linux/amd64"
        and value.get("formal_authority_granted") is False
        and value.get("candidate_acceptance_authority_granted") is False,
        "DEVELOPMENT_USERSPACE_REPORT_IDENTITY_INVALID",
    )
    require(
        value.get("network_isolation")
        == {
            "self": {
                "status": "PASS",
                "mechanism": "NO_NEW_PRIVS_SECCOMP_FILTER",
                "architecture": "x86_64",
                "inherited_socket_fds": 0,
                "socket_errno": 1,
                "scope": "THIS_PAYLOAD_AND_DESCENDANTS",
            },
            "child": {
                "status": "PASS",
                "seccomp_inherited": True,
                "socket_denied": True,
            },
        },
        "DEVELOPMENT_USERSPACE_ISOLATION_UNCONFIRMED",
    )
    require(
        type(value["network_isolation"]["self"]["inherited_socket_fds"]) is int
        and type(value["network_isolation"]["self"]["socket_errno"]) is int
        and value["network_isolation"]["child"]["seccomp_inherited"] is True
        and value["network_isolation"]["child"]["socket_denied"] is True,
        "DEVELOPMENT_USERSPACE_ISOLATION_UNCONFIRMED",
    )
    require(
        value.get("workload_closed") is True, "DEVELOPMENT_USERSPACE_WORKLOAD_UNCLOSED"
    )
    if request["mode"] == "DIAGNOSE_TRUNCATION":
        require(
            value.get("status") == "DIAGNOSTIC_COMPLETE"
            and value.get("phase") == "TRUNCATION_DIAGNOSTIC",
            "DEVELOPMENT_USERSPACE_DIAGNOSTIC_UNSAFE",
        )
        stage = value.get("stages", {}).get("truncation_diagnostic", {})
        original = stage.get("original_test", {})
        boundary = stage.get("boundary_observation", {})
        require(
            type(original.get("run")) is int
            and original["run"] == 1
            and type(original.get("errors")) is int
            and original["errors"] == 1
            and type(original.get("failures")) is int
            and original["failures"] == 0
            and original.get("successful") is False
            and type(original.get("fixture_retained_owners")) is int
            and original["fixture_retained_owners"] == 0
            and original.get("projection_overflow") is False
            and original.get("projection_fault") is False
            and original.get("aborted") is False
            and original.get("effect_violation") is False
            and len(original.get("records", ())) == 1
            and original["records"][0].get("id") == TEST_IDS[-1]
            and original["records"][0].get("status") == "ERROR"
            and boundary.get("fixture_cleanup") == "CLOSED"
            and boundary.get("fixture_descriptor_closed") is True
            and boundary.get("fixture_stream_closed") is True
            and boundary.get("fixture_owner_revoked") is True
            and type(boundary.get("retained_owners_after_fixture")) is int
            and boundary["retained_owners_after_fixture"] == 0
            and boundary.get("observed_pattern_expected") is True,
            "DEVELOPMENT_USERSPACE_DIAGNOSTIC_UNSAFE",
        )
        outer_expected = [
            ("PRIMARY", "ArchiveHandoffError", "BOOTSTRAP_ARCHIVE_HANDLE_CLOSE_FAILED"),
            ("SUPPRESSED_CONTEXT", "KitFileError", "BOOTSTRAP_KIT_FILE_CHANGED"),
        ]
        require(
            exact_projection(
                boundary.get("inner"),
                [
                    (
                        "PRIMARY",
                        "ArchiveHandoffError",
                        "BOOTSTRAP_ARCHIVE_IDENTITY_MISMATCH",
                    )
                ],
            )
            and exact_projection(boundary.get("outer"), outer_expected)
            and original["records"][0].get("diagnostics")
            == [
                {
                    "status": "ERROR",
                    "phase": "FIXTURE_EXIT",
                    "exception": boundary["outer"],
                }
            ],
            "DEVELOPMENT_USERSPACE_DIAGNOSTIC_UNSAFE",
        )
        return "CONTINUE"
    require(
        value.get("status") in {"PASS", "FAIL"}, "DEVELOPMENT_USERSPACE_REPORT_INVALID"
    )
    stages = value.get("stages", {})
    require(
        value.get("phase") == "COMPLETE"
        and type(stages) is dict
        and stages.get("product_imports", {}).get("status") == "PASS"
        and stages.get("product_imports", {}).get("origin_bound") is True
        and stages.get("kit_imports", {}).get("status") == "PASS"
        and stages.get("kit_imports", {}).get("origin_bound") is True
        and stages.get("actions", {}).get("valid_proofs") == 5
        and stages.get("actions", {}).get("tampered_signature") == "REJECTED"
        and stages.get("platform_release", {}).get("status") == "VERIFIED"
        and stages.get("platform_release", {}).get("consumer") == "TEST_ONLY"
        and stages.get("platform_release", {}).get("current_tuf_update") == "NOT_RUN",
        "DEVELOPMENT_USERSPACE_REQUIRED_CONSUMER_UNVERIFIED",
    )
    groups = (
        ("seven_platform_tests", TEST_IDS),
        ("posix_pass_fds", EXTRA_TEST_IDS),
        ("truncation_regressions", TRUNCATION_REGRESSION_IDS),
    )
    require(
        all(test_group_closed(stages.get(name), ids) for name, ids in groups),
        "DEVELOPMENT_USERSPACE_TEST_RESOURCE_UNCONFIRMED",
    )
    all_passed = all(test_group_passed(stages.get(name), ids) for name, ids in groups)
    if value["status"] == "PASS":
        require(all_passed, "DEVELOPMENT_USERSPACE_REQUIRED_TEST_UNVERIFIED")
        return "COMPLETE"
    require(
        not all_passed and value.get("failure_category") == "TEST_RESULTS",
        "DEVELOPMENT_USERSPACE_UNSAFE_FAILURE",
    )
    return "CONTINUE"


def validate_request(value, *, ordinal, parent, original_material, previous):
    require(
        type(value) is dict
        and set(value)
        == {
            "schema",
            "ordinal",
            "mode",
            "source_sha",
            "source_tree",
            "inputs",
            "inputs_sha256",
        }
        and value["schema"] == "animemo.userspace-workload-request/v1"
        and type(value["ordinal"]) is int
        and value["ordinal"] == ordinal
        and value["mode"] in MODES
        and all(
            type(value[key]) is str and h._SHA.fullmatch(value[key])
            for key in ("source_sha", "source_tree")
        )
        and type(value["inputs"]) is str
        and Path(value["inputs"]).is_absolute()
        and Path(value["inputs"]).parent == parent
        and type(value["inputs_sha256"]) is str
        and h._DIGEST.fullmatch(value["inputs_sha256"])
    )
    if previous is not None:
        require(
            value["source_sha"] != previous["source_sha"]
            or previous["mode"] == "DIAGNOSE_TRUNCATION"
            and value["mode"] == "FINAL_SET",
            "DEVELOPMENT_USERSPACE_UNCHANGED_RETRY",
        )
    material = read_inputs(
        Path(value["inputs"]),
        value["inputs_sha256"],
        source_sha=value["source_sha"],
        source_tree=value["source_tree"],
    )
    require(
        material["files"] == original_material["files"]
        and material.get("kit_source") == original_material.get("kit_source"),
        "DEVELOPMENT_USERSPACE_MATERIAL_REBOUND",
    )
    _check_checkout(value["source_sha"], value["source_tree"])
    return value


def next_request(limits, *, parent, original_material, previous, controller):
    require(limits.rounds < MAX_ROUNDS, "DEVELOPMENT_USERSPACE_ROUND_LIMIT")
    path = limits.root / f"round-{limits.rounds + 1:02d}.request.json"
    idle_deadline = time.monotonic() + min(MAX_IDLE, limits.remaining(MAX_IDLE))
    while True:
        limits.check()
        check_controller(controller)
        if path.exists():
            raw = read_bounded_release_file(
                path, subject="Fixed DEV workload request", maximum=16384
            )
            return validate_request(
                json.loads(raw, object_pairs_hook=h.reject_duplicate_json_keys),
                ordinal=limits.rounds + 1,
                parent=parent,
                original_material=original_material,
                previous=previous,
            )
        require(time.monotonic() < idle_deadline, "DEVELOPMENT_USERSPACE_IDLE_DEADLINE")
        limits.cancelled.wait(0.5)


def prepared_payload_command(source, mode, session_id, ordinal):
    source.require_open()
    suffix = hashlib.sha256(f"{session_id}:{ordinal}".encode("ascii")).hexdigest()[:32]
    stage = "/tmp/animemo-userspace-" + suffix
    command = single.fixed_guest_command(
        stage=stage,
        inventory_program=(
            source.root / "scripts/closed_runtime_inventory.py"
        ).read_text(encoding="utf-8"),
        expected_digest=source.inventory_digest,
        mode=mode,
    )
    return (
        stage,
        command,
        "sha256:" + hashlib.sha256(command.encode("utf-8")).hexdigest(),
    )


def run_payload(
    provider,
    authority,
    verified,
    lease,
    source,
    request,
    limits,
    session_id,
    initial_command_digest,
):
    source.require_open()
    require_guest(provider, authority, verified, lease)
    ordinal = limits.reserve_round(
        {
            "session_id": session_id,
            "boot_id": verified.guest.boot_id,
            "source_sha": source.source_sha,
            "source_tree": source.source_tree,
            "inventory_digest": source.inventory_digest,
            "inputs_digest": source.userspace_probe_digest,
            "mode": request["mode"],
            "input_manifest_sha256": request["inputs_sha256"],
        }
    )
    stage, command, command_digest = prepared_payload_command(
        source, request["mode"], session_id, ordinal
    )
    require(
        ordinal != 1 or command_digest == initial_command_digest,
        "DEVELOPMENT_USERSPACE_PREBOOT_COMMAND_CHANGED",
    )
    provider._ssh_checked(
        authority,
        "/usr/bin/test ! -e " + stage + " -a ! -L " + stage,
        code="DEVELOPMENT_USERSPACE_STAGE_EXISTS",
        bootstrap_identity=True,
    )
    provider._run(
        provider._scp_argv(
            authority=authority,
            source=str(source.root),
            destination=stage,
            recursive=True,
            bootstrap_identity=True,
        ),
        code="DEVELOPMENT_USERSPACE_TRANSFER_FAILED",
        timeout=limits.remaining(),
        openssh=True,
    )
    source.require_open()
    require_guest(provider, authority, verified, lease)
    completed = provider._ssh_checked(
        authority,
        command,
        code="DEVELOPMENT_USERSPACE_GUEST_FAILED",
        timeout=limits.remaining(),
        bootstrap_identity=True,
    )
    require(
        0 < len(completed.stdout) <= 1024 * 1024, "DEVELOPMENT_USERSPACE_REPORT_INVALID"
    )
    value = json.loads(completed.stdout, object_pairs_hook=h.reject_duplicate_json_keys)
    record = {
        "ordinal": ordinal,
        "mode": request["mode"],
        "guest": value,
        "source_sha": source.source_sha,
        "source_tree": source.source_tree,
        "stage": stage,
        "command_sha256": command_digest,
        "status": "RECEIVED_NOT_ACCEPTED",
        "source_closed": False,
    }
    write_new(limits.root / f"round-{ordinal:02d}.received.json", record)
    decision = validate_report(value, request)
    require_guest(provider, authority, verified, lease)
    source.require_open()
    record.update(status="VALID_RESULT", decision=decision)
    return record


def run(args):
    expires = single.validate_entry(args)
    require(args.initial_mode in MODES)
    source_sha = (
        subprocess.check_output(
            ["git", "-C", str(ROOT), "rev-parse", "HEAD"], timeout=30
        )
        .decode()
        .strip()
    )
    source_tree = (
        subprocess.check_output(
            ["git", "-C", str(ROOT), "rev-parse", "HEAD^{tree}"], timeout=30
        )
        .decode()
        .strip()
    )
    original_material = read_inputs(
        args.inputs, args.inputs_sha256, source_sha=source_sha, source_tree=source_tree
    )
    request = {
        "schema": "animemo.userspace-workload-request/v1",
        "ordinal": 1,
        "mode": args.initial_mode,
        "source_sha": source_sha,
        "source_tree": source_tree,
        "inputs": str(args.inputs),
        "inputs_sha256": args.inputs_sha256,
    }
    limits = Limits(expires, authorization_root(args.authorization_id))
    provider = h.ClosedVmwareProvider()
    report = {
        "schema": "animemo.userspace-session-result/v1",
        "purpose": PURPOSE,
        "status": "ERROR",
        "controller_source_sha": source_sha,
        "controller_source_tree": source_tree,
        "rounds": [],
        "new_clones": 0,
        "start_attempts": 0,
        "sudo_capture_attempts": 0,
        "installer_executions": 0,
        "formal_authority_granted": False,
        "candidate_acceptance_authority_granted": False,
    }
    try:
        with (
            supervised_commands(provider, limits),
            single.execution_scope(provider, retain_vm=args.execute, result=report),
        ):
            with h.acquire_candidate_material_authority(
                args.verified_candidate_digest,
                provider=provider,
                _state_root=args.candidate_state,
            ) as material:
                with (
                    provider.bind_candidate_material_authority(material),
                    ExitStack() as first_source,
                ):
                    source = first_source.enter_context(
                        acquire_development_source(
                            provider,
                            source_sha=source_sha,
                            source_tree=source_tree,
                            userspace_inputs={
                                "manifest": args.inputs,
                                "expected_manifest_sha256": args.inputs_sha256,
                            },
                        )
                    )
                    original = material.loaded.candidate_input
                    base = h.build_harness_plan(
                        verified_candidate_digest=args.verified_candidate_digest,
                        expected_qualification_run_id=args.qualification_run_id,
                        expected_source_sha=original["source_sha"],
                        expected_source_tree=original["source_tree"],
                        provider=provider,
                        _candidate_material_authority=material,
                    )
                    plan = from_material_plan(
                        base,
                        execution_source_sha=source_sha,
                        execution_source_tree=source_tree,
                        execution_inventory_digest=source.inventory_digest,
                        userspace_probe_digest=source.userspace_probe_digest,
                    )
                    report["initial_vm_plan"] = plan.as_dict()
                    _, _, initial_command_digest = prepared_payload_command(
                        source, request["mode"], plan.session_id, 1
                    )
                    report["preboot_command_sha256"] = initial_command_digest
                    controller = controller_files()
                    report["controller_files"] = controller
                    if not args.execute:
                        report["status"] = "PLAN_ONLY"
                    else:
                        limits.check()
                        report["started"] = single.reserve_one_clone(args, plan)
                        report.update(
                            clone_slot_consumed=1, new_clones=None, start_attempts=None
                        )
                        write_new(
                            limits.root / "session-limits.json",
                            {
                                "max_workloads": MAX_ROUNDS,
                                "max_seconds": MAX_LIFETIME,
                                "idle_seconds": MAX_IDLE,
                                "expires_utc": args.authorization_expires_utc,
                                "controller_source_sha": source_sha,
                                "controller_source_tree": source_tree,
                                "controller_files": controller,
                            },
                        )
                        with single.soft_stop_clone(provider, plan, report) as (
                            profile,
                            lease,
                            disk,
                            snapshot,
                        ):
                            report.update(new_clones=1, start_attempts=1)
                            authority = provider._active_profile_authority(
                                profile, plan
                            )
                            verified = provider._verify_bootstrap_connection(
                                authority,
                                profile,
                                preboot_disk_graph_digest=disk,
                                preboot_snapshot_identity=snapshot,
                            )
                            while True:
                                check_controller(controller)
                                record = run_payload(
                                    provider,
                                    authority,
                                    verified,
                                    lease,
                                    source,
                                    request,
                                    limits,
                                    plan.session_id,
                                    initial_command_digest,
                                )
                                first_source.close()
                                source = None
                                record["source_closed"] = True
                                write_new(
                                    limits.root
                                    / f"round-{record['ordinal']:02d}.result.json",
                                    record,
                                )
                                report["rounds"].append(record)
                                if record["decision"] == "COMPLETE":
                                    report["status"] = "PASS"
                                    break
                                if limits.rounds >= MAX_ROUNDS:
                                    report["status"] = "INCOMPLETE_ROUND_LIMIT"
                                    break
                                request = next_request(
                                    limits,
                                    parent=args.inputs.parent,
                                    original_material=original_material,
                                    previous=request,
                                    controller=controller,
                                )
                                require_guest(provider, authority, verified, lease)
                                source = first_source.enter_context(
                                    acquire_development_source(
                                        provider,
                                        source_sha=request["source_sha"],
                                        source_tree=request["source_tree"],
                                        userspace_inputs={
                                            "manifest": Path(request["inputs"]),
                                            "expected_manifest_sha256": request[
                                                "inputs_sha256"
                                            ],
                                        },
                                    )
                                )
        check_controller(controller)
    except BaseException as error:
        if isinstance(error, (KeyboardInterrupt, SystemExit)):
            limits.reason = "OPERATOR_CANCELLED"
            limits.cancelled.set()
        report["failure_code"] = getattr(
            error, "code", "DEVELOPMENT_USERSPACE_SESSION_FAILED"
        )
        raise
    finally:
        report["workload_invocations"] = limits.rounds
        report["cancel_reason"] = limits.reason
        report["round_record_paths"] = [
            str(limits.root / f"round-{i:02d}.received.json")
            for i in range(1, limits.rounds + 1)
            if (limits.root / f"round-{i:02d}.received.json").is_file()
        ]
        write_new(args.output, report)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in (
        "authorization-id",
        "authorization-expires-utc",
        "verified-candidate-digest",
        "inputs-sha256",
    ):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--qualification-run-id", type=int, required=True)
    for name in ("candidate-state", "inputs", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument(
        "--initial-mode", choices=sorted(MODES), default="DIAGNOSE_TRUNCATION"
    )
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)
    try:
        value = run(args)
        print(
            json.dumps(
                {
                    "status": value["status"],
                    "workload_invocations": value["workload_invocations"],
                }
            )
        )
        return 0 if value["status"] in {"PASS", "PLAN_ONLY"} else 2
    except (Exception, KeyboardInterrupt) as error:  # noqa: BLE001 - only fixed codes leave the controller.
        print(
            json.dumps(
                {
                    "status": "ERROR",
                    "code": getattr(
                        error, "code", "DEVELOPMENT_USERSPACE_SESSION_FAILED"
                    ),
                }
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
