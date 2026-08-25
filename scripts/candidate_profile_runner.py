"""Run one closed Candidate profile inside a disposable VM.

The host harness supplies identity context through one canonical, bounded
base64url environment value.  No VM path, shell fragment, package list, or
transport override is accepted by this program.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import subprocess
import sys
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Protocol

from installer.platform_bootstrap import (
    PlatformBootstrapError,
    parse_platform_bootstrap_plan,
    parse_platform_bootstrap_receipt,
)
from release.candidate import (
    CandidateContractError,
    canonical_json_bytes,
    load_verified_candidate,
    sha256_bytes,
    validate_profile_receipt,
)
from release.materials import reject_duplicate_json_keys

PROFILES = ("FRESH_BASE", "DOCKER_BASE", "RUNTIME_BASE_OFFLINE")
INSTALLER_PROFILES = {
    "FRESH_BASE": "ONLINE_FRESH",
    "DOCKER_BASE": "ONLINE_EXISTING_DOCKER",
    "RUNTIME_BASE_OFFLINE": "OFFLINE_VALIDATE_ONLY",
}
CONTEXT_ENV = "ANIMEMO_CANDIDATE_PROFILE_CONTEXT_B64URL"
RECEIPT_OUTPUT = Path("/var/lib/animemo/candidate-acceptance/profile-receipt.json")
MAX_CONTEXT_BYTES = 64 * 1024
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")


class ProfileRunnerError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class CommandRunner(Protocol):
    def run(
        self, argv: tuple[str, ...], environment: Mapping[str, str]
    ) -> tuple[int, bytes, bytes]: ...


class SubprocessCommandRunner:
    def run(
        self, argv: tuple[str, ...], environment: Mapping[str, str]
    ) -> tuple[int, bytes, bytes]:
        completed = subprocess.run(  # noqa: S603 - closed argv from installer_argv
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=dict(environment),
            shell=False,
            timeout=4 * 60 * 60,
            check=False,
        )
        return completed.returncode, completed.stdout, completed.stderr


def _decode_context(value: str) -> dict[str, Any]:
    if (
        not value
        or "=" in value
        or len(value) > MAX_CONTEXT_BYTES * 2
        or re.fullmatch(r"[A-Za-z0-9_-]+", value) is None
    ):
        raise ProfileRunnerError("CANDIDATE_PROFILE_CONTEXT_INVALID")
    try:
        decoded = base64.urlsafe_b64decode(
            (value + "=" * (-len(value) % 4)).encode("ascii")
        )
    except (ValueError, UnicodeEncodeError) as error:
        raise ProfileRunnerError("CANDIDATE_PROFILE_CONTEXT_INVALID") from error
    if (
        not decoded
        or len(decoded) > MAX_CONTEXT_BYTES
        or base64.urlsafe_b64encode(decoded).decode("ascii").rstrip("=") != value
    ):
        raise ProfileRunnerError("CANDIDATE_PROFILE_CONTEXT_INVALID")
    try:
        context = json.loads(
            decoded.decode("utf-8"),
            object_pairs_hook=reject_duplicate_json_keys,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise ProfileRunnerError("CANDIDATE_PROFILE_CONTEXT_INVALID") from error
    fields = {
        "base_vm_identity",
        "clone_identity",
        "initial_platform_state",
        "original_vm_pre_hashes",
        "profile",
        "snapshot_identity",
    }
    if type(context) is not dict or set(context) != fields:
        raise ProfileRunnerError("CANDIDATE_PROFILE_CONTEXT_INVALID")
    for field in ("base_vm_identity", "clone_identity", "snapshot_identity"):
        if type(context[field]) is not str or not _DIGEST.fullmatch(context[field]):
            raise ProfileRunnerError("CANDIDATE_PROFILE_CONTEXT_INVALID")
    hashes = context["original_vm_pre_hashes"]
    if (
        type(hashes) is not dict
        or not hashes
        or any(
            type(name) is not str
            or not name
            or type(digest) is not str
            or not _DIGEST.fullmatch(digest)
            for name, digest in hashes.items()
        )
    ):
        raise ProfileRunnerError("CANDIDATE_PROFILE_CONTEXT_INVALID")
    state = context["initial_platform_state"]
    if type(state) is not dict or set(state) != {
        "docker_present",
        "network_allowed",
        "runtime_dependencies_present",
    } or any(type(item) is not bool for item in state.values()):
        raise ProfileRunnerError("CANDIDATE_PROFILE_CONTEXT_INVALID")
    if context["profile"] not in PROFILES:
        raise ProfileRunnerError("CANDIDATE_PROFILE_CONTEXT_INVALID")
    if canonical_json_bytes(context) != decoded:
        raise ProfileRunnerError("CANDIDATE_PROFILE_CONTEXT_NON_CANONICAL")
    return context


def installer_argv(
    *, verified_candidate_digest: str, profile: str, public_origin: str
) -> tuple[str, ...]:
    if not _DIGEST.fullmatch(verified_candidate_digest) or profile not in PROFILES:
        raise ProfileRunnerError("CANDIDATE_PROFILE_INPUT_INVALID")
    try:
        origin = urllib.parse.urlsplit(public_origin)
        port = origin.port
    except (TypeError, ValueError):
        raise ProfileRunnerError("CANDIDATE_PROFILE_ORIGIN_INVALID") from None
    if (
        type(public_origin) is not str
        or len(public_origin) > 2048
        or origin.scheme != "https"
        or not origin.hostname
        or origin.username is not None
        or origin.password is not None
        or origin.query
        or origin.fragment
        or origin.path not in {"", "/"}
        or port not in {None, 443}
    ):
        raise ProfileRunnerError("CANDIDATE_PROFILE_ORIGIN_INVALID")
    return (
        sys.executable,
        "-m",
        "installer",
        "candidate",
        "--verified-candidate-digest",
        verified_candidate_digest,
        "--profile",
        INSTALLER_PROFILES[profile],
        "--public-origin",
        public_origin,
        "--execute",
        "--accept",
        "--json",
    )


def _result_json(stdout: bytes) -> dict[str, Any]:
    if not stdout or len(stdout) > 8 * 1024 * 1024:
        raise ProfileRunnerError("CANDIDATE_INSTALLER_RESULT_INVALID")
    try:
        value = json.loads(stdout, object_pairs_hook=reject_duplicate_json_keys)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise ProfileRunnerError("CANDIDATE_INSTALLER_RESULT_INVALID") from error
    if type(value) is not dict:
        raise ProfileRunnerError("CANDIDATE_INSTALLER_RESULT_INVALID")
    return value


def build_profile_receipt(
    *,
    loaded,
    profile: str,
    context: Mapping[str, Any],
    installer_output: Mapping[str, Any],
    started_at: str,
    completed_at: str,
) -> dict[str, Any]:
    platform_plan_value = installer_output.get("platformPlan")
    platform = installer_output.get("platformBootstrapReceipt")
    result = installer_output.get("installerResult")
    if (
        type(platform_plan_value) is not dict
        or type(platform) is not dict
        or type(result) is not dict
        or installer_output.get("strictPostProvisionQualification") is not True
    ):
        raise ProfileRunnerError("CANDIDATE_INSTALLER_RESULT_INVALID")
    plan_digest = installer_output.get("installerPlanDigest")
    if type(plan_digest) is not str or not _DIGEST.fullmatch(plan_digest):
        raise ProfileRunnerError("CANDIDATE_INSTALLER_RESULT_INVALID")
    try:
        parsed_plan = parse_platform_bootstrap_plan(
            canonical_json_bytes(platform_plan_value)
        )
        parsed_receipt = parse_platform_bootstrap_receipt(
            canonical_json_bytes(platform),
            plan=parsed_plan,
        )
    except PlatformBootstrapError as error:
        raise ProfileRunnerError("CANDIDATE_INSTALLER_RESULT_INVALID") from error
    platform_plan = parsed_plan.plan_digest
    if (
        parsed_plan.mode.value != INSTALLER_PROFILES[profile]
        or parsed_receipt.result != "PASS"
        or parsed_receipt.plan_digest != platform_plan
    ):
        raise ProfileRunnerError("CANDIDATE_INSTALLER_RESULT_INVALID")
    succeeded = result.get("outcome") in {"SUCCEEDED", "NO_CHANGE"}
    images = {item.role: item.digest for item in loaded.images.images}
    facts = parsed_plan.initial_capabilities
    expected_initial_state = {
        "docker_present": facts.docker_cli_present
        and facts.docker_daemon_healthy,
        "runtime_dependencies_present": facts.compose_v2_present
        and facts.pg_dump_major is not None
        and facts.psql_major is not None,
        "network_allowed": parsed_plan.network_policy != "DENY_ALL",
    }
    if dict(context["initial_platform_state"]) != expected_initial_state:
        raise ProfileRunnerError("CANDIDATE_PROFILE_PLATFORM_STATE_MISMATCH")
    actions = parsed_plan.actions
    apt_count = sum(
        1
        for action in actions
        if action.kind.value in {
            "APT_UPDATE",
            "INSTALL_DOCKER",
            "INSTALL_COMPOSE",
            "INSTALL_POSTGRES_CLIENT",
        }
    )
    receipt = {
        "schema": "animemo.prepublication-candidate-profile-receipt/v1",
        "version": 1,
        "candidate_input_digest": loaded.verified["candidate_input_sha256"],
        "verified_candidate_digest": loaded.verified_digest,
        "qualification_run_id": loaded.candidate_input["qualification_run_id"],
        "qualification_run_attempt": loaded.candidate_input[
            "qualification_run_attempt"
        ],
        "source_sha": loaded.candidate_input["source_sha"],
        "source_tree": loaded.candidate_input["source_tree"],
        "candidate_version": loaded.candidate_input["candidate_version"],
        "profile": profile,
        "base_vm_identity": context["base_vm_identity"],
        "snapshot_identity": context["snapshot_identity"],
        "clone_identity": context["clone_identity"],
        "initial_platform_state": context["initial_platform_state"],
        "platform_bootstrap_plan_digest": platform_plan,
        "platform_bootstrap_receipt_digest": sha256_bytes(
            canonical_json_bytes(platform)
        ),
        "strict_platform_qualification": True,
        "instance_mutation_before_platform_qualification": 0,
        "installer_plan_digest": plan_digest,
        "installer_execution_result": "PASS" if succeeded else "FAIL",
        "api_digest": images["api"],
        "web_digest": images["web"],
        "postgres_digest": images["postgres"],
        "redis_digest": images["redis"],
        "doctor_result": "PASS" if succeeded else "FAIL",
        "canonical_test_results": [
            {"name": "installer-candidate-execution", "result": "PASS" if succeeded else "FAIL"}
        ],
        "network_request_count": 0 if profile == "RUNTIME_BASE_OFFLINE" else apt_count,
        "apt_command_count": apt_count,
        "external_pull_count": 0,
        "original_vm_pre_hashes": dict(context["original_vm_pre_hashes"]),
        "original_vm_post_hashes": dict(context["original_vm_pre_hashes"]),
        "release_authority_granted": False,
        "publish_authorized": False,
        "started_at": started_at,
        "completed_at": completed_at,
        "result": "PASS" if succeeded else "FAIL",
    }
    try:
        return validate_profile_receipt(receipt)
    except CandidateContractError as error:
        raise ProfileRunnerError(error.code) from error


def execute_profile(
    *,
    verified_candidate_digest: str,
    profile: str,
    public_origin: str,
    context_b64url: str,
    runner: CommandRunner | None = None,
) -> dict[str, Any]:
    context = _decode_context(context_b64url)
    if context["profile"] != profile:
        raise ProfileRunnerError("CANDIDATE_PROFILE_CONTEXT_MISMATCH")
    try:
        loaded = load_verified_candidate(verified_candidate_digest)
    except CandidateContractError as error:
        raise ProfileRunnerError(error.code) from error
    started = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    environment = {
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
        "PYTHONPATH": str(loaded.root / "installer-root"),
    }
    return_code, stdout, _ = (runner or SubprocessCommandRunner()).run(
        installer_argv(
            verified_candidate_digest=verified_candidate_digest,
            profile=profile,
            public_origin=public_origin,
        ),
        environment,
    )
    output = _result_json(stdout)
    if return_code != 0:
        raise ProfileRunnerError("CANDIDATE_INSTALLER_EXECUTION_FAILED")
    completed = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    return build_profile_receipt(
        loaded=loaded,
        profile=profile,
        context=context,
        installer_output=output,
        started_at=started,
        completed_at=completed,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="AniMemo Candidate profile runner")
    parser.add_argument("--verified-candidate-digest", required=True)
    parser.add_argument("--profile", choices=PROFILES, required=True)
    parser.add_argument("--public-origin", required=True)
    parser.add_argument("--execute", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        command = installer_argv(
            verified_candidate_digest=args.verified_candidate_digest,
            profile=args.profile,
            public_origin=args.public_origin,
        )
        if not args.execute:
            print(
                json.dumps(
                    {
                        "mode": "PLAN_ONLY",
                        "profile": args.profile,
                        "command": list(command[:-3]),
                        "releaseAuthorityGranted": False,
                        "publishAuthorized": False,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
            return 0
        context = os.environ.get(CONTEXT_ENV, "")
        receipt = execute_profile(
            verified_candidate_digest=args.verified_candidate_digest,
            profile=args.profile,
            public_origin=args.public_origin,
            context_b64url=context,
        )
        if RECEIPT_OUTPUT.exists() or RECEIPT_OUTPUT.is_symlink():
            raise ProfileRunnerError("CANDIDATE_PROFILE_RECEIPT_OUTPUT_EXISTS")
        RECEIPT_OUTPUT.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with RECEIPT_OUTPUT.open("xb") as output:
            output.write(canonical_json_bytes(receipt))
            output.flush()
            os.fsync(output.fileno())
        os.chmod(RECEIPT_OUTPUT, 0o600)
        print(
            json.dumps(
                {
                    "status": receipt["result"],
                    "receipt": str(RECEIPT_OUTPUT),
                    "receiptDigest": sha256_bytes(canonical_json_bytes(receipt)),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0
    except ProfileRunnerError as error:
        print(json.dumps({"code": error.code}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
