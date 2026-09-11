"""One FRESH_BASE controller validation for the explicitly authorized v2 task.

This entry owns the provider/material contexts and never invokes an installer.
Its public result is a dynamic observation, not a Candidate acceptance receipt.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
from contextlib import ExitStack, contextmanager
from pathlib import Path

from release.formal_windows_pretrust import (
    create_windows_private_named_directory,
)
from release.r2_plugin_origin import CloudflarePluginOrigin, R2PluginOriginError
from scripts import candidate_vm_harness as h
from scripts.guest_console_capture import ConsoleCaptureError, WindowsConsoleCapture
from scripts.guest_sudo_session import ControllerFailure, SessionSupervisor, wipe


# Deliberately independent of run/session/source IDs. A failed capture cannot
# acquire another allowance by restarting this entry or changing its arguments.
CAPTURE_AUTHORIZATION = "ANIMEMO_V2_ISOLATED_DYNAMIC_VALIDATION_AND_QUALIFICATION_V1"
CAPTURE_LEDGER = Path("E:/") / hashlib.sha256(CAPTURE_AUTHORIZATION.encode("ascii")).hexdigest()


def _failure_code(error):
    if isinstance(error, (ControllerFailure, ConsoleCaptureError, R2PluginOriginError, h.CandidateHarnessError, h.CandidateContractError)):
        return getattr(error, "code", str(error))
    return "GUEST_VALIDATION_INTERRUPTED_OR_UNCLASSIFIED"


def _record_operation_failure(result, error):
    if "operation_failure_code" not in result:
        result["operation_failure_code"] = _failure_code(error)
        if type(error) in (h.SessionKeyCommandError, h.VmHostCommandError):
            result["operation_failure_diagnostic"] = error.public_diagnostic()


def _reserve_capture() -> None:
    if CAPTURE_LEDGER.exists() or CAPTURE_LEDGER.is_symlink():
        raise ControllerFailure("GUEST_CAPTURE_ALREADY_ATTEMPTED")
    try:
        create_windows_private_named_directory(
            CAPTURE_LEDGER.parent, name=CAPTURE_LEDGER.name,
        )
    except Exception:
        raise ControllerFailure("GUEST_CAPTURE_ALLOWANCE_UNAVAILABLE") from None


def _check_checkout(source_sha: str, source_tree: str) -> None:
    root = Path(__file__).resolve().parents[1]
    def git(*args):
        return subprocess.run(
            ["git", "-C", str(root), *args], check=True,
            capture_output=True, text=True, timeout=30,
        ).stdout.strip()
    try:
        if (git("rev-parse", "HEAD") != source_sha
                or git("rev-parse", "HEAD^{tree}") != source_tree
                or git("status", "--porcelain", "--untracked-files=no")):
            raise ControllerFailure("GUEST_CONTROLLER_SOURCE_CHANGED")
    except (OSError, subprocess.SubprocessError):
        raise ControllerFailure("GUEST_CONTROLLER_SOURCE_UNVERIFIED") from None


def _origin(plan, role, *, plugin_origin=None):
    if plugin_origin is not None:
        if type(plugin_origin) is not CloudflarePluginOrigin:
            raise ControllerFailure("R2_PLUGIN_CHANNEL_INVALID")
        return plugin_origin.observe(plan, role)
    return h.validate_r2_origin_receipt(
        h.verify_candidate_r2_origin_from_environment(
            target_rc=plan.candidate_version, source_sha=plan.source_sha,
            source_tree=plan.source_tree, auth_method=h.R2_AUTH_METHOD_ARGUMENT,
            observation_role=role, environment=os.environ,
        ),
        expected_source_sha=plan.source_sha, expected_source_tree=plan.source_tree,
        expected_target_rc=plan.candidate_version, expected_observation_role=role,
    )


@contextmanager
def _controller_clone(provider, plan, result):
    """Hold one canonical clone through observation, capture and containment."""
    provider._require_active_execution_authority()
    if (type(provider) is not h.ClosedVmwareProvider
            or type(plan) is not h.CandidateHarnessPlan
            or h.sha256_bytes(h.canonical_json_bytes(plan.identity_body())) != plan.plan_digest
            or type(provider._candidate_material_authority) is not h.HeldCandidateMaterialAuthority
            or provider._candidate_material_authority.loaded.verified_digest != plan.verified_candidate_digest
            or provider.inspect_readiness().receipt_digest != plan.provider_readiness_receipt_digest):
        raise ControllerFailure("GUEST_CONTROLLER_AUTHORITY_INVALID")
    profiles = [item for item in plan.profiles if item.profile == "FRESH_BASE"]
    if len(profiles) != 1 or profiles[0].snapshot_name != h.SNAPSHOT_ALLOWLIST["FRESH_BASE"]:
        raise ControllerFailure("GUEST_CONTROLLER_PROFILE_INVALID")
    profile = profiles[0]
    provider._assert_tools()
    before = provider._hashes()
    if before != dict(plan.original_vm_hashes):
        raise ControllerFailure("CANDIDATE_ORIGINAL_VM_MUTATED")
    authority = provider._active_profile_authority(profile, plan)
    work_root = provider._execution.work_root
    lease = provider._acquire_provider_lease(authority, work_root=work_root)
    profile_holds, clone_holds = ExitStack(), ExitStack()
    power_uncertain = False
    result["resource"] = {
        "profile": profile.profile, "snapshot": profile.snapshot_name,
        "session_id": plan.session_id, "clone_identity": profile.clone_identity,
        "clone_vmx": str(authority.clone_vmx), "retained_work_root": str(work_root),
        "power_state": "NOT_STARTED", "session_keys_removed": False,
        "lease_released": False,
    }
    def checked_running():
        nonlocal power_uncertain
        try:
            return provider._running_vmx_paths()
        except h.CandidateHarnessError:
            power_uncertain = True
            raise
    def reject_running(paths, identity):
        nonlocal power_uncertain
        if identity in paths:
            power_uncertain = True
            raise ControllerFailure("CANDIDATE_VM_CLONE_POWER_STATE_INVALID")
    primary_error = None
    try:
        disk, snapshot = provider._prepare_and_start_profile_clone(
            authority=authority, plan=profile, harness_plan=plan, before_hashes=before,
            profile_authority_stack=profile_holds, clone_authority_stack=clone_holds,
            checked_running_vmx_paths=checked_running, reject_running_clone=reject_running,
        )
        owned = os.path.normcase(str(authority.clone_vmx.resolve(strict=False)))
        if checked_running() != frozenset({owned}):
            power_uncertain = True
            raise ControllerFailure("CANDIDATE_VM_CLONE_POWER_STATE_INVALID")
        result["resource"]["running_observed"] = True
        result["resource"]["power_state"] = "RUNNING"
        result["snapshot_started"] = True
        # No password is present here. The Supervisor repeats these canonical
        # observations in its own same-process grant exchange after capture.
        observed = provider._verify_bootstrap_connection(
            authority, profile, preboot_disk_graph_digest=disk,
            preboot_snapshot_identity=snapshot,
        )
        result["bootstrap_observation"] = {
            "machine_id": observed.guest.machine_id, "boot_id": observed.guest.boot_id,
            "vm_uuid": observed.runtime.vm_uuid, "mac_address": observed.runtime.mac_address,
            "host_key_digest": observed.guest.host_key_digest,
            "preboot_disk_graph_digest": disk, "snapshot_identity": snapshot,
        }
        provider._remove_known_hosts(authority)
        yield profile, lease, disk, snapshot
    except BaseException as error:
        primary_error = error
        if (type(error) is h.VmHostCommandError and error.public_diagnostic()["kind"]
                in {"TIMEOUT", "CANCELLED", "PROCESS_START_OR_WAIT_FAILED"}):
            power_uncertain = True
        _record_operation_failure(result, error)
        raise
    finally:
        cleanup_errors = []
        try:
            if authority.clone_vmx.exists():
                try:
                    running = checked_running()
                    owned = os.path.normcase(str(authority.clone_vmx.resolve(strict=False)))
                    needs_containment = power_uncertain or owned in running
                except h.CandidateHarnessError:
                    needs_containment = True
                if needs_containment:
                    result["resource"]["power_state"] = "CONTAINMENT_PENDING"
                    result["resource"]["power_state"] = provider._contain_clone(authority.clone_vmx)
                else:
                    result["resource"]["power_state"] = (
                        "STOPPED" if result.get("snapshot_started") else "NOT_RUNNING_OBSERVED"
                    )
        except BaseException as error:
            cleanup_errors.append({"step": "containment", "code": _failure_code(error)})
        finally:
            # Every independent cleanup is attempted even if an earlier handle
            # close fails. Retained VM data must not silently retain usable keys.
            for step, cleanup in (
                ("clone_holds", clone_holds.close),
                ("profile_holds", profile_holds.close),
                ("session_key", lambda: provider._destroy_session_key(authority)),
                ("known_hosts", lambda: provider._destroy_known_hosts(authority)),
                ("lease", lambda: provider._release_provider_lease(lease, work_root=work_root)),
            ):
                try:
                    if step not in {"session_key", "known_hosts"} or authority.ssh_root.exists():
                        cleanup()
                    if step == "lease":
                        result["resource"]["lease_released"] = True
                except BaseException as error:
                    cleanup_errors.append({"step": step, "code": _failure_code(error)})
            if not any(item["step"] in {"session_key", "known_hosts"} for item in cleanup_errors):
                result["resource"]["session_keys_removed"] = True
            try:
                result["original_vm_unchanged"] = provider._hashes() == before
                if not result["original_vm_unchanged"]:
                    raise ControllerFailure("CANDIDATE_ORIGINAL_VM_MUTATED")
            except BaseException as error:
                cleanup_errors.append({"step": "original_vm", "code": _failure_code(error)})
            result["resource"]["host_lifecycle"] = list(
                getattr(provider, "_host_lifecycle_observations", ())
            )
            if cleanup_errors:
                result["resource"]["cleanup_errors"] = cleanup_errors
                if primary_error is None:
                    raise ControllerFailure("GUEST_CLONE_CLEANUP_FAILED") from None


def _validate(plan, provider, console, result, *, plugin_origin=None):
    def observe(role):
        if plugin_origin is None:
            return _origin(plan, role)
        return _origin(plan, role, plugin_origin=plugin_origin)
    result["r2_prestate"] = observe("PRESTATE")
    result["external_prestate"] = h._read_expected_external_state(provider, plan.candidate_version)
    primary_error = None
    try:
        with _controller_clone(provider, plan, result) as (profile, lease, disk, snapshot):
            _check_checkout(plan.source_sha, plan.source_tree)
            console.preflight()
            _reserve_capture()
            result["capture_attempts"] = 1
            secret = None
            supervisor = None
            try:
                secret = console.capture()
                result["capture_completed"] = 1
                supervisor = SessionSupervisor(
                    secret, provider=provider, plan=plan, profile=profile, lease=lease,
                    preboot_disk_graph_digest=disk, preboot_snapshot_identity=snapshot,
                )
                _check_checkout(plan.source_sha, plan.source_tree)
                supervisor.bootstrap_rotation()
                result["bootstrap_rotation"] = "PASS"
                _check_checkout(plan.source_sha, plan.source_tree)
                supervisor.validate_verified_guest()
                result["verified_guest_validation"] = "PASS"
                result["verified_observation"] = {
                    "machine_id": supervisor._verified.guest.machine_id,
                    "boot_id": supervisor._verified.guest.boot_id,
                    "host_key_digest": supervisor._verified.guest.host_key_digest,
                }
            finally:
                try:
                    if supervisor is not None:
                        result["delivery_attempts"] = supervisor.delivery_attempts
                        result["delivery_completed"] = supervisor.delivery_completed
                        supervisor.close()
                finally:
                    if secret is not None:
                        wipe(secret)
                    result["secret_cleanup"] = "BEST_EFFORT_COMPLETED"
        _check_checkout(plan.source_sha, plan.source_tree)
    except BaseException as error:
        primary_error = error
        _record_operation_failure(result, error)
        raise
    finally:
        try:
            result["r2_poststate"] = observe("POSTSTATE")
            if result["r2_poststate"]["observation_id"] == result["r2_prestate"]["observation_id"]:
                raise ControllerFailure("GUEST_R2_OBSERVATION_REUSED")
            result["external_poststate"] = h._read_expected_external_state(provider, plan.candidate_version)
        except BaseException as error:
            result["poststate_failure_code"] = _failure_code(error)
            if primary_error is None:
                raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verified-candidate-digest", required=True)
    parser.add_argument("--expected-qualification-run-id", type=int, required=True)
    parser.add_argument("--expected-source-sha", required=True)
    parser.add_argument("--expected-source-tree", required=True)
    parser.add_argument("--result", type=Path, required=True)
    parser.add_argument("--r2-origin-transport", choices=("s3", "cloudflare-plugin"), default="s3")
    args = parser.parse_args(argv)
    # Reserve the report before any external action; never overwrite evidence.
    report = args.result.open("x", encoding="utf-8", newline="\n")
    result = {
        "schema": "animemo.isolated-guest-controller-observation/v1",
        "status": "ERROR", "capture_attempts": 0, "capture_completed": 0,
        "delivery_attempts": {"BOOTSTRAP_ROTATION": 0, "VERIFIED_SUDO": 0},
        "delivery_completed": {"BOOTSTRAP_ROTATION": 0, "VERIFIED_SUDO": 0},
        "snapshot_started": False, "r2_origin_transport": args.r2_origin_transport,
    }
    try:
        _check_checkout(args.expected_source_sha, args.expected_source_tree)
        console = WindowsConsoleCapture()
        console.preflight()
        if CAPTURE_LEDGER.exists() or CAPTURE_LEDGER.is_symlink():
            raise ControllerFailure("GUEST_CAPTURE_ALREADY_ATTEMPTED")
        provider = h.ClosedVmwareProvider()
        with ExitStack() as stack:
            plugin_origin = None
            if args.r2_origin_transport == "cloudflare-plugin":
                plugin_origin = stack.enter_context(CloudflarePluginOrigin())
                result["r2_plugin_channel"] = str(plugin_origin.root)
            stack.enter_context(provider.execution_authority(_retain_controller_data=True))
            material = stack.enter_context(h.acquire_candidate_material_authority(
                args.verified_candidate_digest, provider=provider,
            ))
            plan = h.build_harness_plan(
                verified_candidate_digest=args.verified_candidate_digest,
                expected_qualification_run_id=args.expected_qualification_run_id,
                expected_source_sha=args.expected_source_sha,
                expected_source_tree=args.expected_source_tree,
                provider=provider, _candidate_material_authority=material,
            )
            result["plan"] = plan.as_dict()
            with provider.bind_candidate_material_authority(material):
                _validate(plan, provider, console, result, plugin_origin=plugin_origin)
        if result["resource"]["power_state"] != "STOPPED":
            raise ControllerFailure("GUEST_CLONE_SUSPENDED_REQUIRES_RECONCILIATION")
        result["status"] = "DYNAMIC_VALIDATION_PASSED"
        return 0
    except BaseException as error:
        # Unknown exception text and subprocess output can contain sensitive
        # data. Only errors from the closed contract may contribute a code.
        result["failure_code"] = result.get("operation_failure_code", _failure_code(error))
        if result["failure_code"] != _failure_code(error):
            result["outer_failure_code"] = _failure_code(error)
        if "operation_failure_diagnostic" in result:
            result["failure_diagnostic"] = result["operation_failure_diagnostic"]
        elif type(error) in (h.SessionKeyCommandError, h.VmHostCommandError):
            result["failure_diagnostic"] = error.public_diagnostic()
        return 2
    finally:
        with report:
            json.dump(result, report, ensure_ascii=False, sort_keys=True, indent=2)
            report.write("\n")


if __name__ == "__main__":
    raise SystemExit(main())
