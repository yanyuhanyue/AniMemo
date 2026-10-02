"""One canonical Fresh Clone for a fixed non-root DEV platform workload."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import shlex
import subprocess

from scripts import candidate_vm_harness as h
from scripts.development_plan import from_material_plan
from scripts.development_source import acquire_development_source
from scripts.development_userspace_inputs import PURPOSE, read_inputs
from scripts.guest_batch_scope import authorization_root
from scripts.isolated_guest_validation import _check_checkout, _controller_clone


def validate_entry(args):
    if (
        type(args.authorization_id) is not str
        or re.fullmatch(
            r"ANIMEMO_[A-Z0-9_]{1,96}_USERSPACE_(?:PLATFORM_VALIDATION|(?:CLOSURE_)?DELTA)_V[1-9][0-9]*",
            args.authorization_id,
        )
        is None
        or not args.output.is_absolute()
        or args.output.exists()
        or args.output.is_symlink()
        or type(args.execute) is not bool
    ):
        raise h.CandidateHarnessError("DEVELOPMENT_USERSPACE_SCOPE_INVALID")
    try:
        expires = datetime.strptime(
            args.authorization_expires_utc, "%Y-%m-%dT%H:%M:%SZ"
        ).replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        raise h.CandidateHarnessError("DEVELOPMENT_USERSPACE_SCOPE_INVALID") from None
    if datetime.now(timezone.utc) >= expires:
        raise h.CandidateHarnessError("DEVELOPMENT_USERSPACE_AUTHORIZATION_EXPIRED")
    return expires


def reserve_one_clone(args, plan):
    from release.formal_windows_pretrust import create_windows_private_named_directory

    root = authorization_root(args.authorization_id)
    if root.exists() or root.is_symlink():
        raise h.CandidateHarnessError("DEVELOPMENT_USERSPACE_ALREADY_STARTED")
    created = create_windows_private_named_directory(root.parent, name=root.name)
    record = {
        "schema": "animemo.development-userspace-started/v1",
        "purpose": PURPOSE,
        "authorization_id": args.authorization_id,
        "expires_utc": args.authorization_expires_utc,
        "plan_digest": plan.plan_digest,
        "execution_source_sha": plan.execution_source_sha,
        "execution_source_tree": plan.execution_source_tree,
        "inputs_digest": plan.userspace_probe_digest,
        "clone_limit": 1,
        "start_limit": 1,
        "sudo_capture_limit": 0,
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    with (created / "started.json").open("xb") as stream:
        stream.write(h.canonical_json_bytes(record))
        stream.flush()
        import os

        os.fsync(stream.fileno())
    return record


def fixed_guest_command(*, stage, inventory_program, expected_digest, mode=None):
    if re.fullmatch(
        r"/tmp/animemo-userspace-[0-9a-f]{32}", stage
    ) is None or not h._DIGEST.fullmatch(expected_digest):
        raise h.CandidateHarnessError("DEVELOPMENT_USERSPACE_COMMAND_INVALID")
    if mode not in (None, "DIAGNOSE_TRUNCATION", "FINAL_SET"):
        raise h.CandidateHarnessError("DEVELOPMENT_USERSPACE_COMMAND_INVALID")
    program = (
        'import sys;from pathlib import Path;scope={"__name__":"_fixed_userspace_inventory"};'
        + "exec(compile("
        + repr(inventory_program)
        + ',"<fixed-userspace-inventory>","exec"),scope);'
        + 'assert scope["closed_runtime_inventory_digest"](Path('
        + repr(stage)
        + "))=="
        + repr(expected_digest)
        + ";"
        + "sys.path.insert(0,"
        + repr(stage)
        + ");import runpy;runpy.run_path("
        + repr(stage + "/scripts/userspace_platform_probe.py")
        + ',run_name="__main__")'
    )
    if mode is not None:
        program = (
            "import sys;sys.argv="
            + repr(["userspace_platform_probe.py", "--mode", mode])
            + ";"
            + program
        )
    try:
        compile(inventory_program, "<fixed-userspace-inventory>", "exec")
        compile(program, "<fixed-userspace-guest>", "exec")
    except SyntaxError:
        raise h.CandidateHarnessError(
            "DEVELOPMENT_USERSPACE_GENERATION_INVALID"
        ) from None
    command = "/usr/bin/python3 -I -S -B -c " + shlex.quote(program)
    if shlex.split(command, posix=True) != [
        "/usr/bin/python3",
        "-I",
        "-S",
        "-B",
        "-c",
        program,
    ]:
        raise h.CandidateHarnessError("DEVELOPMENT_USERSPACE_COMMAND_INVALID")
    return command


@contextmanager
def soft_stop_clone(provider, plan, result):
    """Narrow this DEV entry to the canonical one soft-stop operation only."""
    expected = provider._active_profile_authority(plan.profiles[0], plan).clone_vmx
    original = provider._contain_clone

    def contain(vmx):
        if vmx != expected:
            raise h.CandidateHarnessError(
                "DEVELOPMENT_USERSPACE_CONTAINMENT_TARGET_INVALID"
            )
        provider._stop_clone(vmx)
        return "STOPPED"

    provider._contain_clone = contain
    try:
        with _controller_clone(provider, plan, result) as opened:
            yield opened
    finally:
        provider._contain_clone = original


@contextmanager
def execution_scope(provider, *, retain_vm, result):
    """Retain VM evidence while separately observing copied bootstrap-key removal."""
    bootstrap_copy = None
    primary = None
    result["controller_cleanup"] = {
        "bootstrap_copy_removed": "UNKNOWN",
        "vm_evidence_retained": retain_vm,
    }
    try:
        with provider.execution_authority(_retain_controller_data=retain_vm):
            bootstrap_copy = provider._execution.bootstrap_identity
            yield
    except BaseException as error:
        primary = error
        raise
    finally:
        if bootstrap_copy is not None:
            try:
                removed = (
                    not bootstrap_copy.exists() and not bootstrap_copy.is_symlink()
                )
            except OSError:
                removed = False
            result["controller_cleanup"]["bootstrap_copy_removed"] = removed
            if not removed and primary is None:
                raise h.CandidateHarnessError(
                    "DEVELOPMENT_USERSPACE_BOOTSTRAP_COPY_RETAINED"
                )


def run(args):
    expires = validate_entry(args)
    checkout = Path(__file__).resolve().parents[1]
    sha = (
        subprocess.check_output(
            ["git", "-C", str(checkout), "rev-parse", "HEAD"], timeout=30
        )
        .decode()
        .strip()
    )
    tree = (
        subprocess.check_output(
            ["git", "-C", str(checkout), "rev-parse", "HEAD^{tree}"], timeout=30
        )
        .decode()
        .strip()
    )
    _check_checkout(sha, tree)
    read_inputs(args.inputs, args.inputs_sha256, source_sha=sha, source_tree=tree)
    result = {
        "schema": "animemo.development-userspace-host-result/v1",
        "purpose": PURPOSE,
        "status": "ERROR",
        "execution_source_sha": sha,
        "execution_source_tree": tree,
        "sudo_capture_attempts": 0,
        "installer_executions": 0,
        "new_clones": 0,
        "start_attempts": 0,
        "formal_authority_granted": False,
        "candidate_acceptance_authority_granted": False,
    }
    provider = h.ClosedVmwareProvider()
    try:
        with execution_scope(provider, retain_vm=args.execute, result=result):
            with h.acquire_candidate_material_authority(
                args.verified_candidate_digest,
                provider=provider,
                _state_root=args.candidate_state,
            ) as material:
                with provider.bind_candidate_material_authority(material):
                    with acquire_development_source(
                        provider,
                        source_sha=sha,
                        source_tree=tree,
                        userspace_inputs={
                            "manifest": args.inputs,
                            "expected_manifest_sha256": args.inputs_sha256,
                        },
                    ) as source:
                        # Original Q binds only its original comparison/extras. DEV
                        # execution and independently selected Linux Kit have separate pins.
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
                            execution_source_sha=sha,
                            execution_source_tree=tree,
                            execution_inventory_digest=source.inventory_digest,
                            userspace_probe_digest=source.userspace_probe_digest,
                        )
                        result["plan"] = plan.as_dict()
                        if not args.execute:
                            result["status"] = "PLAN_ONLY"
                        else:
                            validate_entry(args)
                            result["started"] = reserve_one_clone(args, plan)
                            result.update(
                                clone_slot_consumed=1,
                                new_clones=None,
                                start_attempts=None,
                            )
                            with soft_stop_clone(provider, plan, result) as (
                                profile,
                                lease,
                                disk,
                                snapshot,
                            ):
                                result.update(new_clones=1, start_attempts=1)
                                authority = provider._active_profile_authority(
                                    profile, plan
                                )
                                verified = provider._verify_bootstrap_connection(
                                    authority,
                                    profile,
                                    preboot_disk_graph_digest=disk,
                                    preboot_snapshot_identity=snapshot,
                                )
                                stage = "/tmp/animemo-userspace-" + plan.session_id
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
                                    timeout=600,
                                    openssh=True,
                                )
                                source.require_open()
                                lease.require_open()
                                inventory = (
                                    source.root / "scripts/closed_runtime_inventory.py"
                                ).read_text(encoding="utf-8")

                                def check_guest():
                                    observed = provider._observe_guest_connection(
                                        authority,
                                        host_key_digest=provider._read_known_host_key(
                                            authority
                                        ),
                                        bootstrap_identity=True,
                                    )
                                    if observed != verified.guest:
                                        raise h.CandidateHarnessError(
                                            "DEVELOPMENT_USERSPACE_TARGET_CHANGED"
                                        )

                                check_guest()
                                remaining = int(
                                    (
                                        expires - datetime.now(timezone.utc)
                                    ).total_seconds()
                                )
                                if remaining <= 0:
                                    raise h.CandidateHarnessError(
                                        "DEVELOPMENT_USERSPACE_AUTHORIZATION_EXPIRED"
                                    )
                                completed = provider._ssh_checked(
                                    authority,
                                    fixed_guest_command(
                                        stage=stage,
                                        inventory_program=inventory,
                                        expected_digest=source.inventory_digest,
                                    ),
                                    code="DEVELOPMENT_USERSPACE_GUEST_FAILED",
                                    timeout=min(600, remaining),
                                    bootstrap_identity=True,
                                )
                                if not 0 < len(completed.stdout) <= 1024 * 1024:
                                    raise h.CandidateHarnessError(
                                        "DEVELOPMENT_USERSPACE_OUTPUT_INVALID"
                                    )
                                observed = json.loads(
                                    completed.stdout,
                                    object_pairs_hook=h.reject_duplicate_json_keys,
                                )
                                result["guest"] = observed
                                if (
                                    observed.get("purpose") != PURPOSE
                                    or observed.get("status") != "PASS"
                                    or observed.get("source_sha") != sha
                                    or observed.get("source_tree") != tree
                                    or observed.get("input_manifest_sha256")
                                    != args.inputs_sha256
                                    or observed.get("sudo_capture_attempts") != 0
                                    or observed.get("installer_executions") != 0
                                    or observed.get("formal_authority_granted")
                                    is not False
                                    or observed.get("network_isolation", {}).get(
                                        "child"
                                    )
                                    != {
                                        "status": "PASS",
                                        "seccomp_inherited": True,
                                        "socket_denied": True,
                                    }
                                    or observed.get("network_isolation", {}).get(
                                        "self", {}
                                    )
                                    != {
                                        "status": "PASS",
                                        "mechanism": "NO_NEW_PRIVS_SECCOMP_FILTER",
                                        "architecture": "x86_64",
                                        "inherited_socket_fds": 0,
                                        "socket_errno": 1,
                                        "scope": "THIS_PAYLOAD_AND_DESCENDANTS",
                                    }
                                ):
                                    raise h.CandidateHarnessError(
                                        "DEVELOPMENT_USERSPACE_OUTPUT_INVALID"
                                    )
                                check_guest()
                                lease.require_open()
                                source.require_open()
                                result["status"] = "PASS"
        _check_checkout(sha, tree)
    except BaseException as error:
        result.update(
            status="ERROR",
            failure_code=getattr(error, "code", "DEVELOPMENT_USERSPACE_FAILED"),
        )
        raise
    finally:
        with args.output.open("x", encoding="utf-8") as stream:
            json.dump(result, stream, ensure_ascii=False, sort_keys=True)
            stream.write("\n")
    return result


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
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)
    try:
        report = run(args)
        print(
            json.dumps(
                {
                    "status": report["status"],
                    "purpose": PURPOSE,
                    "sudo_capture_attempts": 0,
                }
            )
        )
        return 0
    except Exception as error:  # noqa: BLE001 - CLI emits fixed failure projection without exception text.
        code = getattr(error, "code", "DEVELOPMENT_USERSPACE_FAILED")
        print(
            json.dumps(
                {"status": "ERROR", "failure_code": code, "sudo_capture_attempts": 0}
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
