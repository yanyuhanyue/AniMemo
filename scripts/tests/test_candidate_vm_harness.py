from __future__ import annotations

import ast
import copy
import ctypes
import hashlib
import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import ExitStack, nullcontext, redirect_stdout
from ctypes import wintypes
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from durability.canonical import canonical_json_bytes as canonical_identity_bytes
from release import candidate as candidate_contract
from release.candidate import (
    VERIFIED_CANDIDATE_ROOT,
    canonical_json_bytes,
    sha256_bytes,
)
from release.formal_windows_pretrust import hold_windows_private_path_authority
from release.r2_prestate import (
    ACCESS_KEY_ENV,
    ACCOUNT_ID_ENV,
    JURISDICTION_ENV,
    R2_AUTH_METHOD_ARGUMENT,
    R2_RC14_EXPECTED_KEYS,
    R2_RC14_PREFIX,
    SECRET_KEY_ENV,
    verify_rc14_r2_origin_from_environment,
)
from scripts import candidate_vm_harness as harness

DIGEST = "sha256:" + "a" * 64
SHA = "b" * 40
TREE = "c" * 40
RUN_ID = 1234
ACCOUNT_ID = "d" * 32


def _loaded(root: Path):
    return SimpleNamespace(
        root=root,
        verified_digest="sha256:" + "1" * 64,
        verified={"candidate_input_sha256": "sha256:" + "2" * 64},
        candidate_input={
            "qualification_run_id": RUN_ID,
            "qualification_run_attempt": 1,
            "source_sha": SHA,
            "source_tree": TREE,
            "candidate_version": "v1.1.0-rc.14",
        },
    )


class FakeProvider:
    def __init__(self):
        self.execute_calls = 0
        self.external_calls = 0
        self.readiness_calls = 0
        self.events = []
        self.readiness_error = None
        self.external_state = dict(harness.EXPECTED_RC14_EXTERNAL_STATE)
        self.external_versions = []
        self.continuation_calls = []
        self.continuation_safe = True
        self.continuation_known_hosts_file_count = 0
        self.profile_errors = {}
        self.profile_results = {}
        self.hashes = {
            name: "sha256:" + "3" * 64
            for name in (
                *harness.SOURCE_VM_HASH_FILES,
                *harness.SOURCE_VM_PRIVATE_ADDITIONAL_FILES,
            )
        }
        self.snapshots = {
            profile: "sha256:" + character * 64
            for profile, character in zip(harness.PROFILES, "456", strict=True)
        }
        self.snapshot_disk_graphs = {
            profile: "sha256:" + character * 64
            for profile, character in zip(harness.PROFILES, "789", strict=True)
        }
        self.source_disk_graph = "sha256:" + "d" * 64

    def inspect_source(self):
        self.events.append("source")
        return harness.SourceVmEvidence(
            vm_identity=harness.SOURCE_VM_IDENTITY,
            snapshot_identities=self.snapshots,
            snapshot_disk_graph_identities=self.snapshot_disk_graphs,
            source_disk_graph_identity=self.source_disk_graph,
            source_vm_inventory_identity="sha256:" + "e" * 64,
            original_hashes=self.hashes,
        )

    def inspect_readiness(self):
        self.readiness_calls += 1
        self.events.append("readiness")
        if self.readiness_error is not None:
            raise self.readiness_error
        return harness.ProviderReadinessReceipt.issue(
            ssh_digest=harness.EXPECTED_SSH_SHA256,
            scp_digest=harness.EXPECTED_SCP_SHA256,
        )

    def execute_profile(
        self, *, plan, harness_plan, candidate_root, initial_platform_state
    ):
        del candidate_root
        self.execute_calls += 1
        self.events.append(f"profile:{plan.profile}")
        error = self.profile_errors.get(plan.profile)
        if error is not None:
            if callable(error):
                raise error(plan, harness_plan)
            raise error
        receipt = {
            "schema": "animemo.prepublication-candidate-profile-receipt-draft/v1",
            "version": 1,
            "candidate_input_digest": harness_plan.candidate_input_digest,
            "verified_candidate_digest": harness_plan.verified_candidate_digest,
            "qualification_run_id": harness_plan.qualification_run_id,
            "qualification_run_attempt": 1,
            "source_sha": harness_plan.source_sha,
            "source_tree": harness_plan.source_tree,
            "candidate_version": harness_plan.candidate_version,
            "profile": plan.profile,
            "base_vm_identity": harness_plan.source_vm_digest,
            "source_vm_inventory_identity": (
                harness_plan.source_vm_inventory_identity
            ),
            "source_disk_graph_identity": harness_plan.source_disk_graph_identity,
            "snapshot_identity": plan.snapshot_identity,
            "snapshot_disk_graph_identity": plan.snapshot_disk_graph_identity,
            "clone_identity": plan.clone_identity,
            "initial_platform_state": dict(initial_platform_state),
            "platform_bootstrap_plan_digest": "sha256:" + "7" * 64,
            "platform_bootstrap_receipt_digest": "sha256:" + "8" * 64,
            "strict_platform_qualification": True,
            "instance_mutation_before_platform_qualification": 0,
            "installer_plan_digest": "sha256:" + "9" * 64,
            "installer_execution_receipt_digest": DIGEST,
            "installer_execution_result": "PASS",
            "api_digest": DIGEST,
            "web_digest": DIGEST,
            "postgres_digest": DIGEST,
            "redis_digest": DIGEST,
            "doctor_execution_identity": DIGEST,
            "doctor_receipt_digest": DIGEST,
            "canonical_acceptance_tests": [
                {"name": name, "result": "PASS", "receiptDigest": DIGEST}
                for name in (
                    "application.journal-crud",
                    "service.api.health",
                    "service.web.health",
                )
            ],
            "completed_steps": ["runtime.validate", "doctor.accept"],
            "network_observation": {
                "authority": "PRODUCTION_EXECUTION_WITH_OS_EGRESS_ISOLATION",
                "completed_command_inventory_digest": sha256_bytes(
                    canonical_json_bytes([])
                ),
                "completed_commands": [],
                "destination_authority": (
                    "NONE"
                    if plan.profile == "RUNTIME_BASE_OFFLINE"
                    else "UBUNTU_ARCHIVE_VERIFIED_APT_SOURCES"
                ),
                "egress_isolation": {
                    "authority": "OS_ENFORCED_CANDIDATE_EGRESS_ISOLATION",
                    "container_network": "animemo_animemo",
                    "container_network_internal": True,
                    "service": "animemo-updater.service",
                    "service_address_families": ["AF_UNIX", "AF_NETLINK"],
                    "receipt_digest": sha256_bytes(
                        canonical_identity_bytes(
                            {
                                "authority": "OS_ENFORCED_CANDIDATE_EGRESS_ISOLATION",
                                "containerNetwork": "animemo_animemo",
                                "containerNetworkInternal": True,
                                "service": "animemo-updater.service",
                                "serviceAddressFamilies": ["AF_UNIX", "AF_NETLINK"],
                            }
                        )
                    ),
                },
                "expected_network_command_digests": [],
                "observer_identities": {
                    "platform": candidate_contract._CANDIDATE_COMMAND_OBSERVER_IDENTITY,
                    "runtime": candidate_contract._CANDIDATE_COMMAND_OBSERVER_IDENTITY,
                },
                "platform_plan_digest": "sha256:" + "7" * 64,
                "policy": (
                    "DENY_ALL"
                    if plan.profile == "RUNTIME_BASE_OFFLINE"
                    else "APT_UBUNTU_ARCHIVE_ONLY"
                ),
                "retryable_network_command_digests": [],
                "result": "PASS",
            },
            "external_pull_observation": {
                "authority": "PRODUCTION_EXECUTION_COMMAND_BOUNDARY",
                "inventory": [],
                "observed_count": 0,
                "observer_identity": candidate_contract._CANDIDATE_COMMAND_OBSERVER_IDENTITY,
                "pull_denied_command_digests": [],
                "result": "PASS",
                "runtime_command_inventory_digest": sha256_bytes(
                    canonical_json_bytes([])
                ),
            },
            "image_acquisition_receipt_digest": DIGEST,
            "image_runtime_readback_receipt_digest": DIGEST,
            "release_authority_granted": False,
            "publish_authorized": False,
            "started_at": "2026-08-25T12:00:00Z",
            "completed_at": "2026-08-25T12:01:00Z",
            "result": "PASS",
        }
        if plan.profile in self.profile_results:
            receipt["installer_execution_result"] = self.profile_results[plan.profile]
            receipt["result"] = self.profile_results[plan.profile]
        return receipt

    def inspect_original_hashes(self):
        return dict(self.hashes)

    def inspect_profile_continuation(self, *, plan, harness_plan):
        self.continuation_calls.append(plan.profile)
        return harness.ProfileContinuationReceipt.issue(
            profile=plan.profile,
            session_id=harness_plan.session_id,
            original_vm_hashes=self.hashes,
            active_profile_root_count=0,
            session_private_key_count=0,
            known_hosts_file_count=self.continuation_known_hosts_file_count,
            running_vm_count=0,
            quarantine_present=False,
            continuation_safe=self.continuation_safe,
        )

    def inspect_candidate_external_state(self, candidate_version):
        self.external_calls += 1
        self.events.append(f"external:{self.external_calls}")
        self.external_versions.append(candidate_version)
        return dict(self.external_state)


class CandidateGuestPathContractTests(unittest.TestCase):
    def test_guest_staging_root_matches_verified_candidate_loader_root(self):
        self.assertEqual(
            Path(harness.GUEST_CANDIDATE_ROOT),
            VERIFIED_CANDIDATE_ROOT,
        )

    def test_candidate_material_manifest_requires_closed_inventory_program(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            candidate = {
                "qualification_run_id": RUN_ID,
                "candidate_runtime_file_inventory": [],
            }
            for name in harness._CANDIDATE_AUTHORITY_ROOT_FILES:
                (root / name).write_bytes(("fixed:" + name).encode("utf-8"))
            (root / f"release-qualification-{RUN_ID}.json").write_bytes(
                b"fixed qualification"
            )
            runner = root / "installer-root" / "scripts" / "candidate_profile_runner.py"
            runner.parent.mkdir(parents=True)
            runner.write_bytes(b"print('fixed candidate runner')\n")
            loaded = SimpleNamespace(
                root=root,
                verified_digest=sha256_bytes(
                    (root / "verified-candidate.json").read_bytes()
                ),
                verified={"candidate_input_sha256": DIGEST},
                candidate_input=candidate,
                materials=SimpleNamespace(
                    verified=SimpleNamespace(
                        files=(
                            SimpleNamespace(
                                path="scripts/candidate_profile_runner.py",
                                sha256=sha256_bytes(runner.read_bytes()),
                            ),
                        )
                    )
                ),
            )
            with self.assertRaisesRegex(
                harness.CandidateHarnessError,
                "CANDIDATE_MATERIAL_AUTHORITY_INVALID",
            ):
                harness._candidate_authoritative_file_identities(loaded)

    def test_closed_inventory_execution_has_no_dynamic_loader_shape(self):
        repository = Path(__file__).resolve().parents[2]
        paths = (
            Path(harness.__file__),
            repository / "scripts" / "formal_vm_harness.py",
            repository / "scripts" / "closed_runtime_inventory.py",
            Path(__file__),
            repository / "scripts" / "tests" / "test_formal_vm_harness.py",
        )
        dynamic_builtins = {"ex" + "ec", "ev" + "al"}
        for path in paths:
            tree = ast.parse(path.read_text(encoding="utf-8"))
            self.assertEqual(
                [
                    node.func.id
                    for node in ast.walk(tree)
                    if isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id in dynamic_builtins
                ],
                [],
                path,
            )
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                for keyword in node.keywords:
                    self.assertFalse(
                        keyword.arg == "shell"
                        and isinstance(keyword.value, ast.Constant)
                        and keyword.value.value is True,
                        path,
                    )
        production_tree = ast.parse(Path(harness.__file__).read_text(encoding="utf-8"))
        inventory_builder = next(
            node
            for node in production_tree.body
            if isinstance(node, ast.FunctionDef)
            and node.name == "_guest_runtime_inventory_command"
        )
        builder_constants = tuple(
            node.value
            for node in ast.walk(inventory_builder)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        )
        self.assertFalse(any(" -" + "c " in value for value in builder_constants))
        self.assertFalse(
            any(
                "base" + "64.b64" + "decode" in value
                for value in builder_constants
            )
        )

    def test_root_only_guest_inventory_calls_use_hidden_sudo_stdin(self):
        production_tree = ast.parse(Path(harness.__file__).read_text(encoding="utf-8"))
        for method_name, error_code in (
            ("_stage_candidate", "CANDIDATE_VM_GUEST_MATERIAL_INVENTORY_UNAVAILABLE"),
            ("_stage_formal_workload", "FORMAL_VM_GUEST_RUNTIME_INVENTORY_UNAVAILABLE"),
        ):
            method = next(
                node
                for node in ast.walk(production_tree)
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == method_name
            )
            call = next(
                node
                for node in ast.walk(method)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "_ssh_checked"
                and any(
                    keyword.arg == "code"
                    and isinstance(keyword.value, ast.Constant)
                    and keyword.value.value == error_code
                    for keyword in node.keywords
                )
            )
            command = call.args[1]
            self.assertIsInstance(command, ast.BinOp)
            self.assertIsInstance(command.op, ast.Add)
            self.assertIsInstance(command.left, ast.Constant)
            self.assertEqual(command.left.value, "sudo -S -p '' -- ")
            sudo_password = next(
                keyword.value
                for keyword in call.keywords
                if keyword.arg == "sudo_password"
            )
            self.assertIsInstance(sudo_password, ast.Name)
            self.assertEqual(sudo_password.id, "password")

    def test_profile_runner_uses_python_safe_path(self):
        provider = harness.ClosedVmwareProvider(
            runner=RecordingRunner(),
            windows_platform=FakeWindowsPlatform(),
            environment={harness.GUEST_SUDO_PASSWORD_ENV: "test-only-password"},
        )
        profile = SimpleNamespace(
            profile="FRESH_BASE",
            clone_identity=DIGEST,
            snapshot_identity=DIGEST,
            snapshot_disk_graph_identity=DIGEST,
        )
        plan = SimpleNamespace(
            source_vm_digest=DIGEST,
            source_vm_inventory_identity=DIGEST,
            source_disk_graph_identity=DIGEST,
            original_vm_hashes={"base.vmx": DIGEST},
            candidate_input_digest=DIGEST,
            verified_candidate_digest=DIGEST,
        )
        completed = SimpleNamespace(stdout=b"{}")
        with mock.patch.object(
            provider,
            "_ssh_checked",
            side_effect=[SimpleNamespace(stdout=b""), completed],
        ) as ssh_checked:
            provider._run_profile_guest(
                authority=mock.sentinel.profile_authority,
                plan=profile,
                harness_plan=plan,
                guest_root="/var/lib/animemo/prepublication-candidates/v2/candidate",
                initial_platform_state={
                    "docker_present": False,
                    "network_allowed": True,
                    "runtime_dependencies_present": False,
                },
            )

        command = ssh_checked.call_args_list[0].args[1]
        self.assertIn("/usr/bin/python3 -P -B ", command)
        self.assertIn("PYTHONSAFEPATH=1", command)


class PublicTransport:
    def __init__(self):
        self.calls = []

    def get(self, url, headers):
        self.calls.append((url, dict(headers)))
        if url.startswith("https://ghcr.io/token?"):
            return 200, b'{"token":"anonymous-read-token"}'
        return 404, b"{}"


class RecordingRunner:
    def __init__(self, *, returncodes=None):
        self.calls = []
        self.returncodes = dict(returncodes or {})

    def run(self, argv, *, environment, cwd, input_bytes=None, timeout=300):
        self.calls.append(
            {
                "argv": tuple(argv),
                "environment": dict(environment),
                "cwd": Path(cwd),
                "input_bytes": input_bytes,
                "timeout": timeout,
            }
        )
        return SimpleNamespace(
            returncode=self.returncodes.get(tuple(argv), 0), stdout=b"", stderr=b""
        )


class FakeWindowsPlatform:
    def __init__(
        self,
        *,
        program_data: str = r"C:\ProgramData",
        directory_exists: bool = True,
        contains_reparse_point: bool = False,
        fixed_drive: bool = True,
        binary_available: bool = True,
        ssh_digest: str | None = None,
        scp_digest: str | None = None,
        vmrun_digest: str | None = None,
        robocopy_digest: str | None = None,
        libcrypto_digest: str | None = None,
        machine: int = 0x8664,
        vm_machine: int = 0x014C,
        robocopy_machine: int = 0x8664,
        identity_safe: bool = True,
        known_hosts_safe: bool = True,
        identity_status: str | None = None,
        known_hosts_status: str | None = None,
    ):
        self.program_data = program_data
        self.directory_exists = directory_exists
        self.contains_reparse_point = contains_reparse_point
        self.fixed_drive = fixed_drive
        self.binary_available = binary_available
        self.ssh_digest = ssh_digest or harness.EXPECTED_SSH_SHA256
        self.scp_digest = scp_digest or harness.EXPECTED_SCP_SHA256
        self.vmrun_digest = vmrun_digest or harness.EXPECTED_VMRUN_SHA256
        self.robocopy_digest = (
            robocopy_digest or harness.EXPECTED_ROBOCOPY_SHA256
        )
        self.libcrypto_digest = (
            libcrypto_digest or harness.EXPECTED_OPENSSH_LIBCRYPTO_SHA256
        )
        self.machine = machine
        self.vm_machine = vm_machine
        self.robocopy_machine = robocopy_machine
        self.identity_safe = identity_safe
        self.known_hosts_safe = known_hosts_safe
        self.identity_status = identity_status or (
            "PASS" if identity_safe else "ACL_UNSAFE"
        )
        self.known_hosts_status = known_hosts_status or (
            "PASS" if known_hosts_safe else "ACL_UNSAFE"
        )
        self.resolve_calls = 0

    def resolve_program_data(self):
        self.resolve_calls += 1
        return self.program_data

    def is_directory(self, path):
        del path
        return self.directory_exists

    def has_reparse_component(self, path):
        del path
        return self.contains_reparse_point

    def is_fixed_drive(self, path):
        del path
        return self.fixed_drive

    def is_file(self, path):
        del path
        return True

    def inspect_binary(self, path):
        if not self.binary_available:
            raise OSError("binary unavailable")
        if path == harness.SSH:
            digest = self.ssh_digest
        elif path == harness.SCP:
            digest = self.scp_digest
        elif path == harness.VMRUN:
            digest = self.vmrun_digest
        elif path == harness.ROBOCOPY:
            digest = self.robocopy_digest
        elif path == harness.OPENSSH_LIBCRYPTO:
            digest = self.libcrypto_digest
        else:
            digest = harness.EXPECTED_SSH_KEYGEN_SHA256
        return harness.WindowsBinaryIdentity(
            sha256=digest,
            pe_machine=(
                self.vm_machine
                if path == harness.VMRUN
                else self.robocopy_machine
                if path == harness.ROBOCOPY
                else self.machine
            ),
        )

    def inspect_controlled_file(self, path, *, root, private):
        self.last_controlled_file_check = (path, root, private)
        if path.name == "id_ed25519":
            status = self.identity_status
        elif path.name == "known_hosts":
            status = self.known_hosts_status
        else:
            status = "ACL_UNSAFE"
        return harness.WindowsControlledFileInspection(status=status)


class RecordingWin32Function:
    def __init__(self, callback=None):
        object.__setattr__(self, "argtypes_assigned", False)
        object.__setattr__(self, "restype_assigned", False)
        object.__setattr__(self, "argtypes", None)
        object.__setattr__(self, "restype", None)
        object.__setattr__(self, "callback", callback)
        object.__setattr__(self, "calls", [])

    def __setattr__(self, name, value):
        if name == "argtypes":
            object.__setattr__(self, "argtypes_assigned", True)
        if name == "restype":
            object.__setattr__(self, "restype_assigned", True)
        object.__setattr__(self, name, value)

    def __call__(self, *args):
        self.calls.append(args)
        if self.callback is not None:
            return self.callback(*args)
        return 0


class RecordingWin32Library:
    def __init__(self):
        self.functions = {}

    def __getattr__(self, name):
        return self.functions.setdefault(name, RecordingWin32Function())


class RecordingWin32Loader:
    def __init__(self):
        self.calls = []
        self.libraries = {}
        self.last_error = 0

    def __call__(self, name, *, use_last_error):
        self.calls.append((name, use_last_error))
        return self.libraries.setdefault(name.lower(), RecordingWin32Library())

    def get_last_error(self):
        return self.last_error

    def set_last_error(self, value):
        self.last_error = value


def _acl_loader(
    *,
    owner=0x1234567887654321,
    current_user=0x1234567887654321,
    descriptor=0x3456789AA9876543,
    dacl=0x456789ABBA987654,
    named_result=0,
    dirty_named_outputs=False,
    open_token=True,
    dirty_token_output=False,
    token_probe_success=False,
    token_probe_error=122,
    token_query=True,
    equal_sid_result=True,
    equal_sid_error=0,
    broad_access_index=None,
    effective_result_index=None,
    local_free_result=None,
    local_free_error=False,
    close_result=True,
):
    loader = RecordingWin32Loader()
    state = {"effective_index": 0, "owner_seen": None, "current_user_seen": None}

    def set_void_pointer(pointer, value):
        ctypes.cast(pointer, ctypes.POINTER(ctypes.c_void_p)).contents.value = value

    def set_dword(pointer, value):
        ctypes.cast(pointer, ctypes.POINTER(wintypes.DWORD)).contents.value = value

    def get_named_security_info(*args):
        if not named_result or dirty_named_outputs:
            set_void_pointer(args[3], owner)
            set_void_pointer(args[5], dacl)
            set_void_pointer(args[7], descriptor)
        return named_result

    def open_process_token(_process, _access, token_pointer):
        if open_token or dirty_token_output:
            ctypes.cast(
                token_pointer, ctypes.POINTER(wintypes.HANDLE)
            ).contents.value = 0x56789ABCCBA98765
        if not open_token:
            loader.last_error = 5
            return 0
        return 1

    def get_token_information(_token, _token_class, buffer, _size, required):
        if buffer is None:
            set_dword(required, ctypes.sizeof(harness._WindowsTokenUser))
            loader.last_error = token_probe_error
            return int(token_probe_success)
        if not token_query:
            loader.last_error = 5
            return 0
        token_user = ctypes.cast(
            buffer, ctypes.POINTER(harness._WindowsTokenUser)
        ).contents
        token_user.user.sid = current_user
        return 1

    def compare_sid(owner_pointer, current_user_pointer):
        state["owner_seen"] = getattr(owner_pointer, "value", owner_pointer)
        state["current_user_seen"] = getattr(
            current_user_pointer, "value", current_user_pointer
        )
        if not equal_sid_result:
            loader.last_error = equal_sid_error
        return int(equal_sid_result)

    def create_well_known_sid(_sid_type, _domain, _sid, _sid_size):
        return 1

    def effective_rights(_dacl, _trustee, access_pointer):
        index = state["effective_index"]
        state["effective_index"] += 1
        if effective_result_index == index:
            return 5
        set_dword(access_pointer, 0x2 if broad_access_index == index else 0)
        return 0

    def local_free(_descriptor):
        if local_free_error:
            raise OSError("simulated LocalFree failure")
        if local_free_result:
            loader.last_error = 6
        return local_free_result

    callbacks = {
        ("advapi32", "GetNamedSecurityInfoW"): get_named_security_info,
        ("advapi32", "OpenProcessToken"): open_process_token,
        ("advapi32", "GetTokenInformation"): get_token_information,
        ("advapi32", "EqualSid"): compare_sid,
        ("advapi32", "CreateWellKnownSid"): create_well_known_sid,
        ("advapi32", "GetEffectiveRightsFromAclW"): effective_rights,
        ("kernel32", "GetCurrentProcess"): lambda: 0x6789ABCDDCBA9876,
        ("kernel32", "CloseHandle"): lambda _token: int(close_result),
        ("kernel32", "LocalFree"): local_free,
    }
    for (library_name, function_name), callback in callbacks.items():
        library = loader.libraries.setdefault(library_name, RecordingWin32Library())
        library.functions[function_name] = RecordingWin32Function(callback)
    return loader, state


def _acl_adapter(loader):
    return harness._WindowsApiAdapter(
        dll_loader=loader,
        last_error_reader=loader.get_last_error,
        last_error_writer=loader.set_last_error,
    )


class R2ClientError(Exception):
    def __init__(self, code="NoSuchKey", status=404):
        self.response = {
            "Error": {"Code": code},
            "ResponseMetadata": {"HTTPStatusCode": status},
        }
        super().__init__(code)


class R2Client:
    def __init__(self, *, non_empty=False):
        self.non_empty = non_empty
        self.operations = []

    def list_objects_v2(self, *, continuation_token=None):
        self.operations.append(
            ("ListObjectsV2", {"continuation_token": continuation_token})
        )
        contents = []
        if self.non_empty:
            contents = [{"Key": R2_RC14_PREFIX + "unexpected", "Size": 1}]
        return {"Contents": contents, "IsTruncated": False}

    def head_object(self, *, key):
        self.operations.append(("HeadObject", {"key": key}))
        raise R2ClientError()

    def get_object(self, *, key):
        self.operations.append(("GetObject", {"key": key}))
        return {}


class R2ClientWithBetweenBoundaryDrift(R2Client):
    def __init__(self):
        super().__init__()
        self.observation_count = 0

    def list_objects_v2(self, *, continuation_token=None):
        self.observation_count += 1
        self.operations.append(
            ("ListObjectsV2", {"continuation_token": continuation_token})
        )
        if self.observation_count == 1:
            return {"Contents": [], "IsTruncated": False}
        return {
            "Contents": [{"Key": R2_RC14_PREFIX + "unexpected", "Size": 1}],
            "IsTruncated": False,
        }


def _r2_environment():
    return {
        ACCOUNT_ID_ENV: ACCOUNT_ID,
        JURISDICTION_ENV: "default",
        ACCESS_KEY_ENV: "test-only-access-key",
        SECRET_KEY_ENV: "test-only-secret-key",
    }


class CandidateVmHarnessTests(unittest.TestCase):
    @staticmethod
    def _write_closed_source_disk_graph(root: Path) -> Path:
        root.mkdir(parents=True, exist_ok=True)
        descriptors = {
            *harness.SNAPSHOT_DISK_FILES.values(),
            "Ubuntu 64 位-000002.vmdk",
        }
        selected_extent: Path | None = None
        for index, descriptor_name in enumerate(sorted(descriptors)):
            extent_name = descriptor_name.removesuffix(".vmdk") + "-s001.vmdk"
            (root / descriptor_name).write_bytes(
                b'# Disk DescriptorFile\ncreateType="twoGbMaxExtentFlat"\n'
                b"parentCID=ffffffff\n"
                + f'RW 100 FLAT "{extent_name}" 0\n'.encode()
            )
            extent = root / extent_name
            extent.write_bytes((f"extent-{index:04d}").encode("ascii"))
            selected_extent = selected_extent or extent
        for name in harness.SOURCE_VM_HASH_FILES:
            path = root / name
            if path.suffix.casefold() == ".vmdk" or path.name == f"{harness.SOURCE_VM_IDENTITY}.vmx":
                continue
            path.write_bytes(("source:" + name).encode("utf-8"))
        (root / f"{harness.SOURCE_VM_IDENTITY}.vmx").write_text(
            'scsi0:0.fileName = "Ubuntu 64 位-000002.vmdk"\n',
            encoding="utf-8",
        )
        for name in harness.SOURCE_VM_PRIVATE_ADDITIONAL_FILES:
            (root / name).write_bytes(("source:" + name).encode("utf-8"))
        assert selected_extent is not None
        return selected_extent

    def test_execute_cli_returns_controlled_nonzero_for_valid_fail_aggregate(self):
        plan = SimpleNamespace(plan_digest=DIGEST)
        result = {"status": "FAIL", "aggregateReceipt": {"result": "FAIL"}}
        output = io.StringIO()
        with mock.patch(
            "scripts.candidate_vm_harness.build_harness_plan",
            return_value=plan,
        ), mock.patch(
            "scripts.candidate_vm_harness.execute_harness_plan",
            return_value=result,
        ), mock.patch.object(
            harness.ClosedVmwareProvider,
            "execution_authority",
            return_value=nullcontext(),
        ), mock.patch(
            "scripts.candidate_vm_harness.acquire_candidate_material_authority",
            return_value=nullcontext(SimpleNamespace()),
        ), redirect_stdout(output):
            code = harness.main(
                [
                    "--verified-candidate-digest",
                    DIGEST,
                    "--expected-qualification-run-id",
                    str(RUN_ID),
                    "--expected-source-sha",
                    SHA,
                    "--expected-source-tree",
                    TREE,
                    "--execute",
                    "--accept-plan-digest",
                    DIGEST,
                ]
            )

        self.assertEqual(code, 2)
        self.assertEqual(json.loads(output.getvalue()), result)

    @unittest.skipUnless(os.name == "nt", "Windows Candidate material authority")
    def test_candidate_material_authority_closes_over_producer_receipt(self):
        self.assertIn(
            "release-producer-toolchain-receipt.json",
            harness._CANDIDATE_AUTHORITY_ROOT_FILES,
        )

    @unittest.skipUnless(os.name == "nt", "Windows Candidate material authority")
    def test_candidate_material_authority_is_private_held_and_transferable(self):
        candidate_digest = "sha256:" + "2" * 64
        candidate_leaf = candidate_digest.removeprefix("sha256:")
        public_state = self.root / "public-state"
        public_root = public_state / candidate_leaf
        public_root.mkdir(parents=True)
        candidate = {
            "qualification_run_id": RUN_ID,
            "candidate_runtime_file_inventory": [],
        }
        for name in harness._CANDIDATE_AUTHORITY_ROOT_FILES:
            (public_root / name).write_bytes(("fixed:" + name).encode("utf-8"))
        qualification = public_root / f"release-qualification-{RUN_ID}.json"
        qualification.write_bytes(b"fixed qualification")
        runner = public_root / "installer-root" / "scripts" / "candidate_profile_runner.py"
        runner.parent.mkdir(parents=True)
        runner.write_bytes(b"print('held candidate runner')\n")
        inventory_program = runner.parent / "closed_runtime_inventory.py"
        inventory_program.write_bytes(
            (Path(__file__).resolve().parents[2] / "scripts" / inventory_program.name)
            .read_bytes()
        )
        verified_digest = sha256_bytes((public_root / "verified-candidate.json").read_bytes())
        materials = (
            SimpleNamespace(
                path="scripts/candidate_profile_runner.py",
                sha256=sha256_bytes(runner.read_bytes()),
            ),
            SimpleNamespace(
                path="scripts/closed_runtime_inventory.py",
                sha256=sha256_bytes(inventory_program.read_bytes()),
            ),
        )
        public_loaded = SimpleNamespace(
            root=public_root,
            verified_digest=verified_digest,
            verified={"candidate_input_sha256": candidate_digest},
            candidate_input=candidate,
            materials=SimpleNamespace(
                verified=SimpleNamespace(files=materials)
            ),
        )
        provider = harness.ClosedVmwareProvider(
            runner=RecordingRunner(),
            windows_platform=FakeWindowsPlatform(),
            environment={},
        )

        def load(digest, *, _state_root=None):
            self.assertEqual(digest, verified_digest)
            if Path(_state_root) == public_state:
                return public_loaded
            return SimpleNamespace(
                **{
                    **public_loaded.__dict__,
                    "root": Path(_state_root) / candidate_leaf,
                }
            )

        outer = harness.create_windows_private_directory(
            Path("E:/"), prefix="candidate-continuation-lifetime"
        )
        material_parent = harness.create_windows_private_directory(
            outer, prefix="candidate-material-parent"
        )
        key_root = harness.create_windows_private_directory(
            self.root, prefix="candidate-bootstrap-key"
        )
        bootstrap_identity = key_root / "id_ed25519"
        bootstrap_identity.write_bytes(b"test-only-bootstrap-identity")
        try:
            with hold_windows_private_path_authority(
                outer
            ) as parent_authority, mock.patch.object(
                harness, "OPENSSH_IDENTITY", bootstrap_identity
            ), provider.execution_authority():
                with mock.patch(
                    "scripts.candidate_vm_harness.load_verified_candidate",
                    side_effect=load,
                ):
                    authority = harness.acquire_candidate_material_authority(
                        verified_digest,
                        provider=provider,
                        _state_root=public_state,
                        private_parent=material_parent,
                        _parent_path_authority=parent_authority,
                    )
                private_root = authority.loaded.root
                self.assertNotEqual(private_root, public_root)
                self.assertEqual(
                    (
                        private_root
                        / "installer-root"
                        / "scripts"
                        / runner.name
                    ).read_bytes(),
                    runner.read_bytes(),
                )
                self.assertEqual(
                    (
                        private_root
                        / "installer-root"
                        / "scripts"
                        / inventory_program.name
                    ).read_bytes(),
                    inventory_program.read_bytes(),
                )
                with self.assertRaises(TypeError):
                    authority.__reduce__()
                with self.assertRaises(OSError):
                    runner.write_bytes(b"rebound")
                authority.close()
                self.assertFalse(private_root.exists())
                with self.assertRaises(harness.CandidateHarnessError):
                    _ = authority.loaded
        finally:
            if outer.exists():
                shutil.rmtree(outer)

    def test_runtime_snapshot_authority_tracks_rebuilt_leaf_state(self):
        rebuilt_state = "Ubuntu 64 位-Snapshot6.vmsn"
        rebuilt_active_disk = "Ubuntu 64 位-000002.vmdk"

        self.assertEqual(
            harness.SNAPSHOT_FILES["RUNTIME_BASE_OFFLINE"], rebuilt_state
        )
        self.assertIn(rebuilt_state, harness.SOURCE_VM_HASH_FILES)
        self.assertNotIn("Ubuntu 64 位-Snapshot5.vmsn", harness.SOURCE_VM_HASH_FILES)
        self.assertIn(rebuilt_active_disk, harness.SOURCE_VM_HASH_FILES)
        self.assertNotIn("Ubuntu 64 位-000004.vmdk", harness.SOURCE_VM_HASH_FILES)

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.loaded = _loaded(self.root)
        self.provider = FakeProvider()

    def tearDown(self):
        self.temporary.cleanup()

    def _plan(self):
        with mock.patch(
            "scripts.candidate_vm_harness.load_verified_candidate",
            return_value=self.loaded,
        ):
            return harness.build_harness_plan(
                verified_candidate_digest=self.loaded.verified_digest,
                expected_qualification_run_id=RUN_ID,
                expected_source_sha=SHA,
                expected_source_tree=TREE,
                provider=self.provider,
            )

    def _connection_fixture(self):
        plan = self._plan()
        profile = plan.profiles[0]
        authority = harness.ClosedVmwareProvider._profile_authority(profile, plan)
        runtime = harness.CloneRuntimeIdentity(
            clone_root=authority.clone_root,
            clone_vmx=authority.clone_vmx,
            vmx_digest="sha256:" + "7" * 64,
            disk_graph_digest="sha256:" + "8" * 64,
            snapshot_name=profile.snapshot_name,
            snapshot_identity=profile.snapshot_identity,
            vm_uuid="564dc3d2-a6b3-0874-ec40-345e9f2e79ad",
            mac_address="00:0c:29:2e:79:ad",
            expected_ip=harness.SSH_HOST,
        )
        bootstrap = harness.GuestConnectionObservation(
            machine_id="1" * 32,
            boot_id="11111111-2222-4333-8444-555555555555",
            mac_addresses=("00:0c:29:2e:79:ad",),
            nonce=harness.connection_challenge(profile.connection_nonce, "guestinfo"),
            host_key_digest="sha256:" + "9" * 64,
        )
        verified_guest = harness.GuestConnectionObservation(
            machine_id=bootstrap.machine_id,
            boot_id=bootstrap.boot_id,
            mac_addresses=bootstrap.mac_addresses,
            nonce=harness.connection_challenge(profile.connection_nonce, "guestinfo"),
            host_key_digest="sha256:" + "a" * 64,
        )
        return plan, profile, authority, runtime, bootstrap, verified_guest

    def test_host_binds_independently_observed_vm_hashes_to_guest_draft(self):
        plan = self._plan()
        profile = plan.profiles[0]
        draft = self.provider.execute_profile(
            plan=profile,
            harness_plan=plan,
            candidate_root=self.root,
            initial_platform_state=harness._initial_platform_state(profile.profile),
        )

        receipt = harness._bind_host_profile_receipt(
            draft,
            plan=plan,
            observed_original_hashes=self.provider.inspect_original_hashes(),
        )

        self.assertEqual(
            receipt["schema"],
            "animemo.prepublication-candidate-profile-receipt/v1",
        )
        self.assertEqual(receipt["original_vm_pre_hashes"], plan.original_vm_hashes)
        self.assertEqual(
            receipt["original_vm_post_hashes"],
            self.provider.inspect_original_hashes(),
        )

    def test_guest_draft_cannot_supply_host_vm_hash_authority(self):
        plan = self._plan()
        profile = plan.profiles[0]
        draft = self.provider.execute_profile(
            plan=profile,
            harness_plan=plan,
            candidate_root=self.root,
            initial_platform_state=harness._initial_platform_state(profile.profile),
        )
        for field in ("original_vm_pre_hashes", "original_vm_post_hashes"):
            with self.subTest(field=field):
                forged = {**draft, field: dict(plan.original_vm_hashes)}
                with self.assertRaisesRegex(
                    harness.CandidateHarnessError,
                    "CANDIDATE_PROFILE_RECEIPT_DRAFT_INVALID",
                ):
                    harness._bind_host_profile_receipt(
                        forged,
                        plan=plan,
                        observed_original_hashes=self.provider.inspect_original_hashes(),
                    )

    def test_host_binding_rejects_observed_original_vm_drift(self):
        plan = self._plan()
        profile = plan.profiles[0]
        draft = self.provider.execute_profile(
            plan=profile,
            harness_plan=plan,
            candidate_root=self.root,
            initial_platform_state=harness._initial_platform_state(profile.profile),
        )
        drifted = self.provider.inspect_original_hashes()
        first = next(iter(drifted))
        drifted[first] = "sha256:" + "f" * 64

        with self.assertRaises(harness.CandidateHarnessError):
            harness._bind_host_profile_receipt(
                draft,
                plan=plan,
                observed_original_hashes=drifted,
            )

    def _temporary_authority(self, authority):
        session_root = self.root / "session"
        profile_root = session_root / "profiles" / "fresh_base"
        clone_root = profile_root / "clone" / authority.clone_identity.removeprefix(
            "sha256:"
        )
        ssh_root = profile_root / "ssh"
        return replace(
            authority,
            session_root=session_root,
            profile_root=profile_root,
            clone_root=clone_root,
            clone_vmx=clone_root / "Ubuntu 64 位.vmx",
            ssh_root=ssh_root,
            identity_file=ssh_root / "id_ed25519",
            known_hosts_file=ssh_root / "known_hosts",
            quarantine_root=session_root / "quarantine" / "fresh_base",
        )

    def _r2_receipt(self, observation_role="PRESTATE"):
        with mock.patch(
            "release.r2_prestate.R2_ACCOUNT_ID_SHA256",
            sha256_bytes(ACCOUNT_ID.encode("ascii")),
        ):
            return verify_rc14_r2_origin_from_environment(
                source_sha=SHA,
                source_tree=TREE,
                auth_method=R2_AUTH_METHOD_ARGUMENT,
                observation_role=observation_role,
                environment=_r2_environment(),
                client=R2Client(),
            )

    def test_default_mode_only_plans_all_three_fixed_profiles(self):
        plan = self._plan()
        self.assertEqual(tuple(item.profile for item in plan.profiles), harness.PROFILES)
        self.assertEqual(
            tuple(item.snapshot_name for item in plan.profiles),
            tuple(harness.SNAPSHOT_ALLOWLIST[item] for item in harness.PROFILES),
        )
        self.assertEqual(self.provider.execute_calls, 0)
        self.assertEqual(
            plan.source_disk_graph_identity,
            self.provider.source_disk_graph,
        )
        self.assertEqual(
            tuple(item.snapshot_disk_graph_identity for item in plan.profiles),
            tuple(
                self.provider.snapshot_disk_graphs[profile]
                for profile in harness.PROFILES
            ),
        )
        self.assertEqual(
            plan.identity_body()["sourceDiskGraphIdentity"],
            self.provider.source_disk_graph,
        )
        self.assertRegex(plan.plan_digest, r"^sha256:[0-9a-f]{64}$")

    def test_plan_accepts_the_next_rc_from_the_verified_candidate_identity(self):
        self.loaded.candidate_input["candidate_version"] = "v1.1.0-rc.15"

        plan = self._plan()

        self.assertEqual(plan.candidate_version, "v1.1.0-rc.15")
        self.assertEqual(self.provider.execute_calls, 0)

    def test_plan_derives_isolated_rc19_session_and_profile_namespaces(self):
        self.loaded.candidate_input["candidate_version"] = "v1.1.0-rc.19"
        first = self._plan()
        second = self._plan()

        self.assertRegex(first.session_id, r"^[0-9a-f]{32}$")
        self.assertNotEqual(first.session_id, second.session_id)
        self.assertEqual(
            len({profile.connection_nonce for profile in first.profiles}),
            len(harness.PROFILES),
        )
        self.assertEqual(
            len({profile.ssh_host_key_alias for profile in first.profiles}),
            len(harness.PROFILES),
        )
        authorities = [
            harness.ClosedVmwareProvider._profile_authority(profile, first)
            for profile in first.profiles
        ]
        self.assertEqual(
            len({authority.profile_root for authority in authorities}),
            len(harness.PROFILES),
        )
        for profile, authority in zip(first.profiles, authorities, strict=True):
            path = authority.profile_root.as_posix()
            self.assertIn(first.candidate_version, path)
            self.assertIn(first.candidate_input_digest.removeprefix("sha256:"), path)
            self.assertIn(first.session_id, path)
            self.assertIn(profile.profile.lower(), path)
            self.assertEqual(authority.host_key_alias, profile.ssh_host_key_alias)
            self.assertEqual(authority.connection_nonce, profile.connection_nonce)
        self.assertNotIn("rc14-candidate-acceptance", str(harness.VM_WORK_PARENT))
        self.assertNotIn("rc14", harness.PUBLIC_ORIGIN.lower())
        production_tree = ast.parse(
            Path(harness.__file__).read_text(encoding="utf-8")
        )
        active_rc14_literals = [
            node.value
            for node in ast.walk(production_tree)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and "rc14" in node.value.lower()
        ]
        self.assertEqual(active_rc14_literals, [])

    def test_exact_clone_connection_chain_is_accepted_before_staging(self):
        _, profile, authority, runtime, bootstrap, verified_guest = (
            self._connection_fixture()
        )

        verified = harness.verify_clone_connection_identity(
            authority=authority,
            plan=profile,
            runtime=runtime,
            bootstrap=bootstrap,
            verified_guest=verified_guest,
            known_hosts_was_absent=True,
            competing_vmx_paths=frozenset(),
            prior_host_key_digests=frozenset(),
        )

        self.assertEqual(verified.authority, authority)
        self.assertEqual(verified.runtime, runtime)
        self.assertEqual(verified.guest, verified_guest)

    def test_wrong_stale_quarantined_and_parallel_guests_fail_closed(self):
        _, profile, authority, runtime, bootstrap, verified_guest = (
            self._connection_fixture()
        )
        identity_cases = {
            "wrong-vmx": {
                "runtime": replace(
                    runtime, clone_vmx=authority.clone_root / "wrong.vmx"
                )
            },
            "wrong-machine-id": {
                "verified_guest": replace(verified_guest, machine_id="2" * 32)
            },
            "wrong-mac": {
                "verified_guest": replace(
                    verified_guest, mac_addresses=("00:0c:29:ff:ff:ff",)
                )
            },
            "wrong-snapshot": {
                "runtime": replace(runtime, snapshot_name="wrong-snapshot")
            },
            "wrong-nonce": {
                "verified_guest": replace(verified_guest, nonce="0" * 64)
            },
            "wrong-ip": {"runtime": replace(runtime, expected_ip="192.0.2.10")},
        }
        for name, changes in identity_cases.items():
            with self.subTest(case=name), self.assertRaisesRegex(
                harness.CandidateHarnessError,
                "CANDIDATE_VM_CONNECTION_IDENTITY_MISMATCH",
            ):
                harness.verify_clone_connection_identity(
                    authority=authority,
                    plan=profile,
                    runtime=changes.get("runtime", runtime),
                    bootstrap=bootstrap,
                    verified_guest=changes.get("verified_guest", verified_guest),
                    known_hosts_was_absent=True,
                    competing_vmx_paths=frozenset(),
                    prior_host_key_digests=frozenset(),
                )

        for name, path in {
            "stale-clone-same-ip": "stale/session/clone.vmx",
            "two-parallel-sessions": "parallel/session/clone.vmx",
            "quarantine-clone-running": "quarantine/stale/clone.vmx",
        }.items():
            with self.subTest(case=name), self.assertRaisesRegex(
                harness.CandidateHarnessError,
                "CANDIDATE_VM_CONNECTION_NAMESPACE_COLLISION",
            ):
                harness.verify_clone_connection_identity(
                    authority=authority,
                    plan=profile,
                    runtime=runtime,
                    bootstrap=bootstrap,
                    verified_guest=verified_guest,
                    known_hosts_was_absent=True,
                    competing_vmx_paths=frozenset({path}),
                    prior_host_key_digests=frozenset(),
                )

        host_key_cases = {
            "inherited-same-host-key": {
                "bootstrap": replace(
                    bootstrap, host_key_digest=verified_guest.host_key_digest
                ),
                "prior": frozenset(),
            },
            "parallel-profile-host-key-reuse": {
                "bootstrap": bootstrap,
                "prior": frozenset({verified_guest.host_key_digest}),
            },
        }
        for name, case in host_key_cases.items():
            with self.subTest(case=name), self.assertRaisesRegex(
                harness.CandidateHarnessError, "CANDIDATE_VM_HOST_KEY_NOT_FRESH"
            ):
                harness.verify_clone_connection_identity(
                    authority=authority,
                    plan=profile,
                    runtime=runtime,
                    bootstrap=case["bootstrap"],
                    verified_guest=verified_guest,
                    known_hosts_was_absent=True,
                    competing_vmx_paths=frozenset(),
                    prior_host_key_digests=case["prior"],
                )

        with self.assertRaisesRegex(
            harness.CandidateHarnessError, "CANDIDATE_VM_KNOWN_HOSTS_RESIDUAL"
        ):
            harness.verify_clone_connection_identity(
                authority=authority,
                plan=profile,
                runtime=runtime,
                bootstrap=bootstrap,
                verified_guest=verified_guest,
                known_hosts_was_absent=False,
                competing_vmx_paths=frozenset(),
                prior_host_key_digests=frozenset(),
            )

    def test_wrong_guest_fails_before_privileged_provisioning_or_staging(self):
        _, profile, authority, runtime, bootstrap, _ = self._connection_fixture()
        authority = self._temporary_authority(authority)
        runtime = replace(
            runtime,
            clone_root=authority.clone_root,
            clone_vmx=authority.clone_vmx,
        )
        wrong_guest = replace(
            bootstrap,
            mac_addresses=("00:0c:29:ff:ff:ff",),
        )
        provider = harness.ClosedVmwareProvider(
            runner=RecordingRunner(),
            windows_platform=FakeWindowsPlatform(),
            environment={},
        )
        exact_vmx = str(authority.clone_vmx.resolve(strict=False)).lower()
        with mock.patch.object(
            provider, "_running_vmx_paths", return_value=frozenset({exact_vmx})
        ), mock.patch.object(
            provider, "_wait_for_guest_ip", return_value=harness.SSH_HOST
        ), mock.patch.object(
            provider, "_read_clone_runtime_identity", return_value=runtime
        ), mock.patch.object(
            provider, "_wait_for_ssh"
        ), mock.patch.object(
            provider, "_read_known_host_key", return_value=bootstrap.host_key_digest
        ), mock.patch.object(
            provider, "_observe_guest_connection", return_value=wrong_guest
        ), mock.patch.object(
            provider, "_provision_session_key_and_rotate_host_key"
        ) as provision, mock.patch.object(
            provider, "_stage_candidate"
        ) as stage, self.assertRaisesRegex(
            harness.CandidateHarnessError,
            "CANDIDATE_VM_CONNECTION_IDENTITY_MISMATCH",
        ):
            provider._establish_clone_connection(
                authority,
                profile,
                preboot_disk_graph_digest=runtime.disk_graph_digest,
                preboot_snapshot_identity=runtime.snapshot_identity,
            )
        provision.assert_not_called()
        stage.assert_not_called()

    def test_machine_id_substitution_fails_during_readonly_bootstrap(self):
        _, profile, authority, runtime, bootstrap, _ = self._connection_fixture()
        authority = self._temporary_authority(authority)
        runtime = replace(
            runtime,
            clone_root=authority.clone_root,
            clone_vmx=authority.clone_vmx,
        )
        substituted = replace(bootstrap, machine_id="2" * 32)
        provider = harness.ClosedVmwareProvider(
            runner=RecordingRunner(),
            windows_platform=FakeWindowsPlatform(),
            environment={},
        )
        exact_vmx = str(authority.clone_vmx.resolve(strict=False)).lower()
        with mock.patch.object(
            provider, "_running_vmx_paths", return_value=frozenset({exact_vmx})
        ), mock.patch.object(
            provider, "_wait_for_guest_ip", return_value=harness.SSH_HOST
        ), mock.patch.object(
            provider, "_read_clone_runtime_identity", return_value=runtime
        ), mock.patch.object(
            provider, "_wait_for_ssh"
        ), mock.patch.object(
            provider, "_read_known_host_key", return_value=bootstrap.host_key_digest
        ), mock.patch.object(
            provider,
            "_observe_guest_connection",
            side_effect=[bootstrap, substituted],
        ), mock.patch.object(
            provider, "_provision_session_key_and_rotate_host_key"
        ) as provision, mock.patch.object(
            provider, "_stage_candidate"
        ) as stage, self.assertRaisesRegex(
            harness.CandidateHarnessError,
            "CANDIDATE_VM_CONNECTION_IDENTITY_MISMATCH",
        ):
            provider._establish_clone_connection(
                authority,
                profile,
                preboot_disk_graph_digest=runtime.disk_graph_digest,
                preboot_snapshot_identity=runtime.snapshot_identity,
            )
        provision.assert_not_called()
        stage.assert_not_called()

    def test_guest_challenge_is_read_from_vmware_guestinfo_not_remote_argv(self):
        _, _, authority, _, bootstrap, _ = self._connection_fixture()
        provider = harness.ClosedVmwareProvider(
            runner=RecordingRunner(),
            windows_platform=FakeWindowsPlatform(),
            environment={},
        )
        payload = {
            "schema": "animemo.clone-guest-identity/v1",
            "nonce": bootstrap.nonce,
            "machine_id": bootstrap.machine_id,
            "boot_id": bootstrap.boot_id,
            "mac_addresses": list(bootstrap.mac_addresses),
        }
        with mock.patch.object(
            provider,
            "_ssh_checked",
            return_value=SimpleNamespace(stdout=json.dumps(payload).encode("utf-8")),
        ) as ssh_checked:
            observed = provider._observe_guest_connection(
                authority,
                host_key_digest=bootstrap.host_key_digest,
                bootstrap_identity=True,
            )
        command = ssh_checked.call_args.args[1]
        self.assertIn("vmtoolsd", command)
        self.assertNotIn(authority.connection_nonce, command)
        self.assertNotIn(bootstrap.nonce, command)
        self.assertEqual(observed, bootstrap)

    def test_session_challenge_is_injected_only_into_exact_clone_vmx_before_boot(self):
        _, profile, authority, _, bootstrap, _ = self._connection_fixture()
        authority = self._temporary_authority(authority)
        authority.clone_root.mkdir(parents=True)
        authority.clone_vmx.write_text(
            'config.version = "8"\n', encoding="utf-8"
        )

        harness.ClosedVmwareProvider._inject_guestinfo_challenge(
            authority, profile
        )

        vmx = authority.clone_vmx.read_text(encoding="utf-8")
        self.assertIn(
            f'guestinfo.animemo.connectionChallenge = "{bootstrap.nonce}"',
            vmx,
        )
        self.assertNotIn(authority.connection_nonce, vmx)
        with self.assertRaisesRegex(
            harness.CandidateHarnessError,
            "CANDIDATE_VM_CONNECTION_CHALLENGE_RESIDUAL",
        ):
            harness.ClosedVmwareProvider._inject_guestinfo_challenge(
                authority, profile
            )

    def test_profile_session_key_is_generated_locally_and_acl_checked(self):
        _, _, authority, _, _, _ = self._connection_fixture()
        authority = self._temporary_authority(authority)
        provider = harness.ClosedVmwareProvider(
            runner=RecordingRunner(),
            windows_platform=FakeWindowsPlatform(),
            environment={},
        )
        generated_argv = []

        def generate(argv, **_kwargs):
            generated_argv.append(tuple(argv))
            authority.identity_file.write_bytes(b"ephemeral-fixture")
            authority.identity_file.with_suffix(".pub").write_text(
                f"ssh-ed25519 QUJD {authority.host_key_alias}\n",
                encoding="ascii",
            )
            return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

        with mock.patch.object(
            harness, "VM_WORK_PARENT", self.root
        ), mock.patch.object(provider, "_run", side_effect=generate):
            provider._prepare_profile_authority(authority)

        self.assertEqual(generated_argv[0][0], str(harness.SSH_KEYGEN))
        self.assertIn(str(authority.identity_file), generated_argv[0])
        self.assertNotIn(str(harness.OPENSSH_IDENTITY), generated_argv[0])
        self.assertTrue(authority.identity_file.is_file())

    def test_bootstrap_is_one_time_then_session_key_owns_all_candidate_calls(self):
        _, _, authority, _, _, _ = self._connection_fixture()
        authority = self._temporary_authority(authority)
        authority.ssh_root.mkdir(parents=True)
        public_key = f"ssh-ed25519 QUJD {authority.host_key_alias}"
        authority.identity_file.with_suffix(".pub").write_text(
            public_key + "\n", encoding="ascii"
        )
        provider = harness.ClosedVmwareProvider(
            runner=RecordingRunner(),
            windows_platform=FakeWindowsPlatform(),
            environment={harness.GUEST_SUDO_PASSWORD_ENV: "test-only-password"},
        )
        with mock.patch.object(provider, "_ssh_checked") as ssh_checked:
            provider._provision_session_key_and_rotate_host_key(authority)
        command = ssh_checked.call_args.args[1]
        self.assertTrue(ssh_checked.call_args.kwargs["bootstrap_identity"])
        self.assertIn(public_key, command)
        self.assertNotIn(authority.connection_nonce, command)

        session_argv = provider._ssh_argv(authority, "/usr/bin/true")
        bootstrap_argv = provider._ssh_argv(
            authority,
            "/usr/bin/true",
            bootstrap_identity=True,
        )
        self.assertIn(f"IdentityFile={authority.identity_file}", session_argv)
        self.assertNotIn(f"IdentityFile={harness.OPENSSH_IDENTITY}", session_argv)
        self.assertIn(f"IdentityFile={harness.OPENSSH_IDENTITY}", bootstrap_argv)

    def test_quarantine_destroys_session_key_before_preserving_diagnostics(self):
        _, _, authority, _, _, _ = self._connection_fixture()
        authority = self._temporary_authority(authority)
        authority.ssh_root.mkdir(parents=True)
        authority.identity_file.write_bytes(b"ephemeral-fixture")
        authority.identity_file.with_suffix(".pub").write_bytes(b"public-fixture")
        authority.known_hosts_file.write_bytes(b"diagnostic-fixture")

        harness.ClosedVmwareProvider._quarantine_clone(authority)

        quarantined = list(authority.quarantine_root.iterdir())
        self.assertEqual(len(quarantined), 1)
        self.assertFalse((quarantined[0] / "ssh" / "id_ed25519").exists())
        self.assertFalse((quarantined[0] / "ssh" / "id_ed25519.pub").exists())
        self.assertFalse((quarantined[0] / "ssh" / "known_hosts").exists())

    def test_global_provider_lease_rejects_parallel_or_stale_sessions(self):
        _, _, authority, _, _, _ = self._connection_fixture()
        authority = self._temporary_authority(authority)
        provider_root = self.root / "provider-lock"
        with mock.patch.object(harness, "VM_WORK_PARENT", provider_root):
            first = harness.ClosedVmwareProvider._acquire_provider_lease(authority)
            with self.assertRaisesRegex(
                harness.CandidateHarnessError,
                "CANDIDATE_VM_PROVIDER_SESSION_BUSY",
            ):
                harness.ClosedVmwareProvider._acquire_provider_lease(authority)
            harness.ClosedVmwareProvider._release_provider_lease(first)
            second = harness.ClosedVmwareProvider._acquire_provider_lease(authority)
            harness.ClosedVmwareProvider._release_provider_lease(second)
        self.assertFalse((provider_root / ".active-provider-session.lock").exists())

    @unittest.skipUnless(os.name == "nt", "Windows provider lease holder")
    def test_private_provider_lease_is_handle_held_until_release(self):
        _, _, authority, _, _, _ = self._connection_fixture()
        authority = self._temporary_authority(authority)
        work_root = harness.create_windows_private_directory(
            self.root, prefix="candidate-provider-lease"
        )
        lease = harness.ClosedVmwareProvider._acquire_provider_lease(
            authority, work_root=work_root
        )
        self.assertIsNotNone(lease.holder)
        with self.assertRaises(OSError):
            lease.path.write_bytes(b"rebound")
        with self.assertRaises(OSError):
            lease.path.unlink()
        harness.ClosedVmwareProvider._release_provider_lease(
            lease, work_root=work_root
        )
        self.assertFalse(lease.path.exists())

    def test_cli_exposes_no_vm_snapshot_shell_or_package_override(self):
        help_text = harness._parser().format_help()
        for forbidden in ("--vm-path", "--snapshot", "--command", "--package"):
            self.assertNotIn(forbidden, help_text)
        self.assertIn("--execute", help_text)
        self.assertIn("--accept-plan-digest", help_text)

    def test_incomplete_original_vm_inventory_is_rejected(self):
        self.provider.hashes.pop(harness.SOURCE_VM_HASH_FILES[0])
        with self.assertRaisesRegex(
            harness.CandidateHarnessError, "SOURCE_IDENTITY_INVALID"
        ):
            self._plan()

    def test_execute_fails_before_clone_when_r2_origin_is_not_proven(self):
        plan = self._plan()
        with mock.patch(
            "scripts.candidate_vm_harness.load_verified_candidate",
            return_value=self.loaded,
        ), self.assertRaisesRegex(
            harness.CandidateHarnessError, "R2_S3_CREDENTIAL_MISSING"
        ):
            harness.execute_harness_plan(
                plan,
                accepted_plan_digest=plan.plan_digest,
                provider=self.provider,
                environment={},
            )
        self.assertEqual(self.provider.execute_calls, 0)

    def test_missing_rest_public_or_tampered_r2_receipt_fails_before_clone(self):
        plan = self._plan()
        valid = self._r2_receipt()
        wrong_prefix = dict(valid)
        wrong_prefix["prefix"] = R2_RC14_PREFIX + "other/"
        wrong_bucket = dict(valid)
        wrong_bucket["bucket"] = "other-bucket"
        write_receipt = dict(valid)
        write_receipt["write_request_count"] = 1
        object_receipt = dict(valid)
        object_receipt["object_count"] = 1
        wrong_source = dict(valid)
        wrong_source["source_sha"] = "e" * 40
        no_auth_method = dict(valid)
        no_auth_method.pop("auth_method")
        cases = {
            "missing": None,
            "rest": {
                "schema": "animemo.r2-rest-prestate-receipt/v1",
                "auth_method": "CLOUDFLARE_REST_BEARER",
            },
            "public-cdn": {
                "schema": "animemo.public-cdn-readback/v1",
                "result": "HTTP_404",
            },
            "wrong-prefix": wrong_prefix,
            "wrong-bucket": wrong_bucket,
            "write": write_receipt,
            "object-count": object_receipt,
            "wrong-source": wrong_source,
            "no-auth-method": no_auth_method,
        }
        for name, receipt in cases.items():
            self.provider.execute_calls = 0
            self.provider.external_calls = 0
            with self.subTest(name=name), mock.patch(
                "scripts.candidate_vm_harness.verify_candidate_r2_origin_from_environment",
                return_value=receipt,
            ), mock.patch(
                "scripts.candidate_vm_harness.load_verified_candidate",
                return_value=self.loaded,
            ), self.assertRaisesRegex(
                harness.CandidateHarnessError, "R2_S3_RECEIPT_INVALID"
            ):
                harness.execute_harness_plan(
                    plan,
                    accepted_plan_digest=plan.plan_digest,
                    provider=self.provider,
                    environment={},
                )
            self.assertEqual(self.provider.execute_calls, 0)
            self.assertEqual(self.provider.external_calls, 0)

    def test_non_empty_s3_prefix_fails_before_public_readback_or_clone(self):
        plan = self._plan()
        with mock.patch(
            "scripts.candidate_vm_harness.load_verified_candidate",
            return_value=self.loaded,
        ), mock.patch(
            "release.r2_prestate.R2_ACCOUNT_ID_SHA256",
            sha256_bytes(ACCOUNT_ID.encode("ascii")),
        ), self.assertRaisesRegex(
            harness.CandidateHarnessError, "R2_S3_PREFIX_NON_EMPTY"
        ):
            harness.execute_harness_plan(
                plan,
                accepted_plan_digest=plan.plan_digest,
                provider=self.provider,
                environment=_r2_environment(),
                r2_client=R2Client(non_empty=True),
            )
        self.assertEqual(self.provider.execute_calls, 0)
        self.assertEqual(self.provider.external_calls, 0)

    def test_harness_selects_only_the_explicit_s3_acceptance_method(self):
        plan = self._plan()
        prestate = self._r2_receipt("PRESTATE")
        poststate = self._r2_receipt("POSTSTATE")
        with mock.patch(
            "scripts.candidate_vm_harness.verify_candidate_r2_origin_from_environment",
            side_effect=[prestate, poststate],
        ) as verify, mock.patch(
            "scripts.candidate_vm_harness.load_verified_candidate",
            return_value=self.loaded,
        ), mock.patch(
            "release.r2_prestate.R2_ACCOUNT_ID_SHA256",
            sha256_bytes(ACCOUNT_ID.encode("ascii")),
        ):
            harness.execute_harness_plan(
                plan,
                accepted_plan_digest=plan.plan_digest,
                provider=self.provider,
                environment={},
            )
        self.assertEqual(verify.call_count, 2)
        self.assertTrue(
            all(
                call.kwargs["auth_method"] == R2_AUTH_METHOD_ARGUMENT
                for call in verify.call_args_list
            )
        )
        self.assertEqual(
            [call.kwargs["observation_role"] for call in verify.call_args_list],
            ["PRESTATE", "POSTSTATE"],
        )

    def test_three_receipts_close_one_aggregate_without_publication_authority(self):
        plan = self._plan()
        environment = _r2_environment()
        client = R2Client()
        with mock.patch(
            "scripts.candidate_vm_harness.load_verified_candidate",
            return_value=self.loaded,
        ), mock.patch(
            "release.r2_prestate.R2_ACCOUNT_ID_SHA256",
            sha256_bytes(ACCOUNT_ID.encode("ascii")),
        ):
            result = harness.execute_harness_plan(
                plan,
                accepted_plan_digest=plan.plan_digest,
                provider=self.provider,
                environment=environment,
                r2_client=client,
            )
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(len(result["profileReceipts"]), 3)
        aggregate = result["aggregateReceipt"]
        self.assertEqual(
            aggregate["schema"],
            "animemo.prepublication-candidate-acceptance-receipt/v3",
        )
        self.assertTrue(aggregate["all_profiles_pass"])
        self.assertEqual(
            {key: value["status"] for key, value in aggregate["profile_results"].items()},
            {
                "fresh_base": "PASS",
                "docker_base": "PASS",
                "runtime_base_offline": "PASS",
            },
        )
        self.assertFalse(aggregate["release_authority_granted"])
        self.assertFalse(aggregate["publish_authorized"])
        self.assertEqual(self.provider.external_calls, 2)
        self.assertEqual(
            aggregate["candidate_prestate"],
            {**harness.EXPECTED_RC14_EXTERNAL_STATE, "r2_origin": "PROVEN_EMPTY"},
        )
        self.assertEqual(
            aggregate["candidate_poststate"],
            {**harness.EXPECTED_RC14_EXTERNAL_STATE, "r2_origin": "PROVEN_EMPTY"},
        )
        self.assertEqual(
            result["r2OriginPrestateReceipt"]["auth_method"],
            "R2_S3_OBJECT_READ_ONLY",
        )
        self.assertEqual(
            aggregate["r2_origin_prestate_receipt_digest"],
            sha256_bytes(
                canonical_json_bytes(result["r2OriginPrestateReceipt"])
            ),
        )
        self.assertEqual(
            aggregate["r2_origin_poststate_receipt_digest"],
            sha256_bytes(
                canonical_json_bytes(result["r2OriginPoststateReceipt"])
            ),
        )
        self.assertEqual(
            aggregate["r2_origin_prestate_observation_id"],
            result["r2OriginPrestateReceipt"]["observation_id"],
        )
        self.assertEqual(
            aggregate["r2_origin_poststate_observation_id"],
            result["r2OriginPoststateReceipt"]["observation_id"],
        )
        self.assertNotEqual(
            result["r2OriginPrestateReceipt"]["observation_id"],
            result["r2OriginPoststateReceipt"]["observation_id"],
        )
        self.assertEqual(
            [name for name, _ in client.operations].count("ListObjectsV2"), 2
        )
        self.assertEqual(
            result["r2OriginPrestateReceipt"]["observation_role"], "PRESTATE"
        )
        self.assertEqual(
            result["r2OriginPoststateReceipt"]["observation_role"], "POSTSTATE"
        )
        self.assertNotIn("rc14_prestate", aggregate)
        self.assertNotIn("rc14_poststate", aggregate)
        unsigned = dict(aggregate)
        receipt_digest = unsigned.pop("receipt_digest")
        self.assertEqual(receipt_digest, sha256_bytes(canonical_json_bytes(unsigned)))

    def test_same_r2_observation_id_with_distinct_roles_fails_closed(self):
        plan = self._plan()
        prestate = self._r2_receipt("PRESTATE")
        poststate = self._r2_receipt("POSTSTATE")
        poststate = copy.deepcopy(poststate)
        poststate["observation_id"] = prestate["observation_id"]
        poststate_unsigned = dict(poststate)
        poststate_unsigned.pop("receipt_digest")
        poststate["receipt_digest"] = sha256_bytes(
            canonical_json_bytes(poststate_unsigned)
        )
        self.assertEqual(prestate["observation_id"], poststate["observation_id"])
        self.assertNotEqual(
            sha256_bytes(canonical_json_bytes(prestate)),
            sha256_bytes(canonical_json_bytes(poststate)),
        )

        with mock.patch(
            "scripts.candidate_vm_harness.verify_candidate_r2_origin_from_environment",
            side_effect=[prestate, poststate],
        ), mock.patch(
            "scripts.candidate_vm_harness.load_verified_candidate",
            return_value=self.loaded,
        ), mock.patch(
            "release.r2_prestate.R2_ACCOUNT_ID_SHA256",
            sha256_bytes(ACCOUNT_ID.encode("ascii")),
        ), self.assertRaisesRegex(
            harness.CandidateHarnessError, "CANDIDATE_R2_OBSERVATION_REUSED"
        ):
            harness.execute_harness_plan(
                plan,
                accepted_plan_digest=plan.plan_digest,
                provider=self.provider,
                environment={},
            )
        self.assertEqual(self.provider.execute_calls, 3)

    def test_between_boundary_r2_drift_fails_closed_after_all_profiles(self):
        plan = self._plan()
        client = R2ClientWithBetweenBoundaryDrift()
        with mock.patch(
            "scripts.candidate_vm_harness.load_verified_candidate",
            return_value=self.loaded,
        ), mock.patch(
            "release.r2_prestate.R2_ACCOUNT_ID_SHA256",
            sha256_bytes(ACCOUNT_ID.encode("ascii")),
        ), self.assertRaisesRegex(
            harness.CandidateHarnessError, "R2_S3_PREFIX_NON_EMPTY"
        ):
            harness.execute_harness_plan(
                plan,
                accepted_plan_digest=plan.plan_digest,
                provider=self.provider,
                environment=_r2_environment(),
                r2_client=client,
            )
        self.assertEqual(self.provider.execute_calls, 3)
        self.assertEqual(client.observation_count, 2)

    def test_next_rc_execution_binds_r2_and_external_reads_to_candidate_version(self):
        candidate_version = "v1.1.0-rc.15"
        self.loaded.candidate_input["candidate_version"] = candidate_version
        plan = self._plan()
        client = R2Client()

        with mock.patch(
            "scripts.candidate_vm_harness.load_verified_candidate",
            return_value=self.loaded,
        ), mock.patch(
            "release.r2_prestate.R2_ACCOUNT_ID_SHA256",
            sha256_bytes(ACCOUNT_ID.encode("ascii")),
        ):
            result = harness.execute_harness_plan(
                plan,
                accepted_plan_digest=plan.plan_digest,
                provider=self.provider,
                environment=_r2_environment(),
                r2_client=client,
            )

        self.assertEqual(
            result["r2OriginPrestateReceipt"]["target_rc"], candidate_version
        )
        head_keys = [
            request["key"]
            for operation, request in client.operations
            if operation == "HeadObject"
        ]
        self.assertEqual(len(head_keys), 2 * len(R2_RC14_EXPECTED_KEYS))
        self.assertTrue(
            all(
                key.startswith(
                    "yanyuhanyue/AniMemo/releases/download/v1.1.0-rc.15/"
                )
                for key in head_keys
            )
        )
        self.assertEqual(
            self.provider.external_versions,
            [candidate_version, candidate_version],
        )

    def test_wrong_plan_digest_never_starts_a_profile(self):
        plan = self._plan()
        with self.assertRaisesRegex(
            harness.CandidateHarnessError, "PLAN_NOT_ACCEPTED"
        ):
            harness.execute_harness_plan(
                plan,
                accepted_plan_digest=DIGEST,
                provider=self.provider,
                environment={},
            )
        self.assertEqual(self.provider.execute_calls, 0)

    def test_original_vm_hash_drift_marks_later_profiles_not_run(self):
        plan = self._plan()
        environment = _r2_environment()
        original_execute = self.provider.execute_profile

        def mutate_source(**kwargs):
            receipt = original_execute(**kwargs)
            self.provider.hashes[harness.SOURCE_VM_HASH_FILES[0]] = (
                "sha256:" + "f" * 64
            )
            return receipt

        self.provider.execute_profile = mutate_source
        with mock.patch(
            "scripts.candidate_vm_harness.load_verified_candidate",
            return_value=self.loaded,
        ), mock.patch(
            "release.r2_prestate.R2_ACCOUNT_ID_SHA256",
            sha256_bytes(ACCOUNT_ID.encode("ascii")),
        ):
            result = harness.execute_harness_plan(
                plan,
                accepted_plan_digest=plan.plan_digest,
                provider=self.provider,
                environment=environment,
                r2_client=R2Client(),
            )
        self.assertEqual(self.provider.execute_calls, 1)
        self.assertEqual(result["status"], "FAIL")
        self.assertEqual(
            {
                key: value["status"]
                for key, value in result["aggregateReceipt"]["profile_results"].items()
            },
            {
                "fresh_base": "ERROR",
                "docker_base": "NOT_RUN_SHARED_BLOCKER",
                "runtime_base_offline": "NOT_RUN_SHARED_BLOCKER",
            },
        )

    def test_profile_local_error_with_valid_containment_continues_series(self):
        plan = self._plan()

        def local_error(profile, harness_plan):
            continuation = harness.ProfileContinuationReceipt.issue(
                profile=profile.profile,
                session_id=harness_plan.session_id,
                original_vm_hashes=self.provider.hashes,
                active_profile_root_count=0,
                session_private_key_count=0,
                known_hosts_file_count=0,
                running_vm_count=0,
                quarantine_present=True,
                continuation_safe=True,
            )
            return harness.CandidateProfileExecutionError(
                "CANDIDATE_PROFILE_LOCAL_FAILURE",
                continuation,
            )

        self.provider.profile_errors["FRESH_BASE"] = local_error
        with mock.patch(
            "scripts.candidate_vm_harness.load_verified_candidate",
            return_value=self.loaded,
        ), mock.patch(
            "release.r2_prestate.R2_ACCOUNT_ID_SHA256",
            sha256_bytes(ACCOUNT_ID.encode("ascii")),
        ):
            result = harness.execute_harness_plan(
                plan,
                accepted_plan_digest=plan.plan_digest,
                provider=self.provider,
                environment=_r2_environment(),
                r2_client=R2Client(),
            )
        self.assertEqual(self.provider.execute_calls, 3)
        self.assertEqual(
            [
                value["status"]
                for value in result["aggregateReceipt"]["profile_results"].values()
            ],
            ["ERROR", "PASS", "PASS"],
        )

    def test_valid_fail_receipt_does_not_skip_remaining_profiles(self):
        plan = self._plan()
        self.provider.profile_results["FRESH_BASE"] = "FAIL"
        with mock.patch(
            "scripts.candidate_vm_harness.load_verified_candidate",
            return_value=self.loaded,
        ), mock.patch(
            "release.r2_prestate.R2_ACCOUNT_ID_SHA256",
            sha256_bytes(ACCOUNT_ID.encode("ascii")),
        ):
            result = harness.execute_harness_plan(
                plan,
                accepted_plan_digest=plan.plan_digest,
                provider=self.provider,
                environment=_r2_environment(),
                r2_client=R2Client(),
            )
        self.assertEqual(self.provider.execute_calls, 3)
        results = result["aggregateReceipt"]["profile_results"]
        self.assertEqual(results["fresh_base"]["status"], "FAIL")
        self.assertIsNotNone(results["fresh_base"]["receipt_digest"])
        self.assertEqual(results["docker_base"]["status"], "PASS")
        self.assertEqual(results["runtime_base_offline"]["status"], "PASS")

    def test_two_independent_local_errors_still_attempt_every_profile(self):
        plan = self._plan()

        def local_error(profile, harness_plan):
            return harness.CandidateProfileExecutionError(
                "CANDIDATE_PROFILE_LOCAL_FAILURE",
                harness.ProfileContinuationReceipt.issue(
                    profile=profile.profile,
                    session_id=harness_plan.session_id,
                    original_vm_hashes=self.provider.hashes,
                    active_profile_root_count=0,
                    session_private_key_count=0,
                    known_hosts_file_count=0,
                    running_vm_count=0,
                    quarantine_present=True,
                    continuation_safe=True,
                ),
            )

        self.provider.profile_errors.update(
            {"FRESH_BASE": local_error, "DOCKER_BASE": local_error}
        )
        with mock.patch(
            "scripts.candidate_vm_harness.load_verified_candidate",
            return_value=self.loaded,
        ), mock.patch(
            "release.r2_prestate.R2_ACCOUNT_ID_SHA256",
            sha256_bytes(ACCOUNT_ID.encode("ascii")),
        ):
            result = harness.execute_harness_plan(
                plan,
                accepted_plan_digest=plan.plan_digest,
                provider=self.provider,
                environment=_r2_environment(),
                r2_client=R2Client(),
            )
        self.assertEqual(self.provider.execute_calls, 3)
        self.assertEqual(
            [value["status"] for value in result["aggregateReceipt"]["profile_results"].values()],
            ["ERROR", "ERROR", "PASS"],
        )
        self.assertEqual(self.provider.external_calls, 2)
        self.assertGreater(
            self.provider.events.index("external:2"),
            self.provider.events.index("profile:RUNTIME_BASE_OFFLINE"),
        )

    def test_receipt_binding_error_is_controlled_and_series_continues(self):
        plan = self._plan()
        original_execute = self.provider.execute_profile

        def wrong_binding(**kwargs):
            receipt = original_execute(**kwargs)
            if kwargs["plan"].profile == "FRESH_BASE":
                receipt["candidate_version"] = "v1.1.0-rc.999"
            return receipt

        self.provider.execute_profile = wrong_binding
        with mock.patch(
            "scripts.candidate_vm_harness.load_verified_candidate",
            return_value=self.loaded,
        ), mock.patch(
            "release.r2_prestate.R2_ACCOUNT_ID_SHA256",
            sha256_bytes(ACCOUNT_ID.encode("ascii")),
        ):
            result = harness.execute_harness_plan(
                plan,
                accepted_plan_digest=plan.plan_digest,
                provider=self.provider,
                environment=_r2_environment(),
                r2_client=R2Client(),
            )
        self.assertEqual(self.provider.execute_calls, 3)
        results = result["aggregateReceipt"]["profile_results"]
        self.assertEqual(results["fresh_base"]["status"], "ERROR")
        self.assertEqual(
            results["fresh_base"]["failure_code"],
            "CANDIDATE_PROFILE_RECEIPT_BINDING_MISMATCH",
        )
        self.assertEqual(results["docker_base"]["status"], "PASS")

    def test_unverified_containment_stops_remaining_profiles(self):
        plan = self._plan()
        self.provider.continuation_safe = False
        with mock.patch(
            "scripts.candidate_vm_harness.load_verified_candidate",
            return_value=self.loaded,
        ), mock.patch(
            "release.r2_prestate.R2_ACCOUNT_ID_SHA256",
            sha256_bytes(ACCOUNT_ID.encode("ascii")),
        ):
            result = harness.execute_harness_plan(
                plan,
                accepted_plan_digest=plan.plan_digest,
                provider=self.provider,
                environment=_r2_environment(),
                r2_client=R2Client(),
            )
        self.assertEqual(self.provider.execute_calls, 1)
        self.assertEqual(
            [
                value["status"]
                for value in result["aggregateReceipt"]["profile_results"].values()
            ],
            ["ERROR", "NOT_RUN_SHARED_BLOCKER", "NOT_RUN_SHARED_BLOCKER"],
        )

    def test_known_hosts_residual_stops_remaining_profiles(self):
        plan = self._plan()
        self.provider.continuation_known_hosts_file_count = 1
        with mock.patch(
            "scripts.candidate_vm_harness.load_verified_candidate",
            return_value=self.loaded,
        ), mock.patch(
            "release.r2_prestate.R2_ACCOUNT_ID_SHA256",
            sha256_bytes(ACCOUNT_ID.encode("ascii")),
        ):
            result = harness.execute_harness_plan(
                plan,
                accepted_plan_digest=plan.plan_digest,
                provider=self.provider,
                environment=_r2_environment(),
                r2_client=R2Client(),
            )
        self.assertEqual(self.provider.execute_calls, 1)
        self.assertEqual(
            [
                value["status"]
                for value in result["aggregateReceipt"]["profile_results"].values()
            ],
            ["ERROR", "NOT_RUN_SHARED_BLOCKER", "NOT_RUN_SHARED_BLOCKER"],
        )
        self.assertEqual(
            result["aggregateReceipt"]["profile_results"]["fresh_base"][
                "failure_code"
            ],
            "CANDIDATE_VM_CONTINUATION_UNVERIFIED",
        )

    def test_continuation_receipt_cannot_authorize_residual_known_hosts(self):
        plan = self._plan()
        profile = plan.profiles[0]
        receipt = harness.ProfileContinuationReceipt.issue(
            profile=profile.profile,
            session_id=plan.session_id,
            original_vm_hashes=plan.original_vm_hashes,
            active_profile_root_count=0,
            session_private_key_count=0,
            known_hosts_file_count=1,
            running_vm_count=0,
            quarantine_present=True,
            continuation_safe=True,
        )

        with self.assertRaisesRegex(
            harness.CandidateHarnessError,
            "CANDIDATE_VM_CONTINUATION_UNVERIFIED",
        ):
            harness._validate_continuation_receipt(
                receipt,
                profile=profile,
                plan=plan,
            )

    def test_unknown_profile_exception_is_a_shared_blocker(self):
        plan = self._plan()
        self.provider.profile_errors["FRESH_BASE"] = RuntimeError(
            "untrusted detail must not escape"
        )
        with mock.patch(
            "scripts.candidate_vm_harness.load_verified_candidate",
            return_value=self.loaded,
        ), mock.patch(
            "release.r2_prestate.R2_ACCOUNT_ID_SHA256",
            sha256_bytes(ACCOUNT_ID.encode("ascii")),
        ):
            result = harness.execute_harness_plan(
                plan,
                accepted_plan_digest=plan.plan_digest,
                provider=self.provider,
                environment=_r2_environment(),
                r2_client=R2Client(),
            )
        self.assertEqual(self.provider.execute_calls, 1)
        results = result["aggregateReceipt"]["profile_results"]
        self.assertEqual(
            results["fresh_base"]["failure_code"],
            "CANDIDATE_PROFILE_UNCLASSIFIED_ERROR",
        )
        self.assertNotIn("untrusted detail", json.dumps(result))

    def test_candidate_version_presence_fails_before_the_first_profile(self):
        plan = self._plan()
        self.provider.external_state["tag"] = "PRESENT"
        environment = _r2_environment()
        with mock.patch(
            "scripts.candidate_vm_harness.load_verified_candidate",
            return_value=self.loaded,
        ), mock.patch(
            "release.r2_prestate.R2_ACCOUNT_ID_SHA256",
            sha256_bytes(ACCOUNT_ID.encode("ascii")),
        ), self.assertRaisesRegex(
            harness.CandidateHarnessError, "VERSION_NOT_EMPTY"
        ):
            harness.execute_harness_plan(
                plan,
                accepted_plan_digest=plan.plan_digest,
                provider=self.provider,
                environment=environment,
                r2_client=R2Client(),
            )
        self.assertEqual(self.provider.external_calls, 1)
        self.assertEqual(self.provider.execute_calls, 0)

    def test_public_cdn_readback_cannot_replace_or_override_s3_receipt(self):
        plan = self._plan()
        self.provider.external_state["public_r2"] = "PRESENT_BY_PUBLIC_READBACK_NON_AUTHORITATIVE"
        with mock.patch(
            "scripts.candidate_vm_harness.load_verified_candidate",
            return_value=self.loaded,
        ), mock.patch(
            "release.r2_prestate.R2_ACCOUNT_ID_SHA256",
            sha256_bytes(ACCOUNT_ID.encode("ascii")),
        ), self.assertRaisesRegex(
            harness.CandidateHarnessError, "CANDIDATE_VERSION_NOT_EMPTY"
        ):
            harness.execute_harness_plan(
                plan,
                accepted_plan_digest=plan.plan_digest,
                provider=self.provider,
                environment=_r2_environment(),
                r2_client=R2Client(),
            )
        self.assertEqual(self.provider.external_calls, 1)
        self.assertEqual(self.provider.execute_calls, 0)

    def test_public_external_readback_uses_only_fixed_get_endpoints(self):
        transport = PublicTransport()
        provider = harness.ClosedVmwareProvider(
            public_transport=transport,
            environment={},
        )
        self.assertEqual(
            provider.inspect_candidate_external_state("v1.1.0-rc.14"),
            harness.EXPECTED_CANDIDATE_EXTERNAL_STATE,
        )
        urls = [url for url, _ in transport.calls]
        self.assertEqual(sum(url.startswith("https://ghcr.io/token?") for url in urls), 2)
        self.assertEqual(
            sum(
                url.startswith(harness.PUBLIC_MIRROR_ORIGIN + "/")
                for url in urls
            ),
            len(R2_RC14_EXPECTED_KEYS),
        )
        self.assertTrue(
            all(
                url.startswith(
                    (
                        "https://api.github.com/",
                        "https://download.animemo.cc/",
                        "https://ghcr.io/",
                    )
                )
                for url in urls
            )
        )

    def test_public_external_readback_uses_the_verified_next_rc(self):
        transport = PublicTransport()
        provider = harness.ClosedVmwareProvider(
            public_transport=transport,
            environment={},
        )

        self.assertEqual(
            provider.inspect_candidate_external_state("v1.1.0-rc.15"),
            harness.EXPECTED_CANDIDATE_EXTERNAL_STATE,
        )

        urls = [url for url, _ in transport.calls]
        self.assertTrue(any("/git/ref/tags/v1.1.0-rc.15" in url for url in urls))
        self.assertTrue(any("/releases/tags/v1.1.0-rc.15" in url for url in urls))
        self.assertEqual(
            sum("/manifests/v1.1.0-rc.15" in url for url in urls),
            2,
        )
        self.assertEqual(
            sum("/releases/download/v1.1.0-rc.15/" in url for url in urls),
            len(R2_RC14_EXPECTED_KEYS),
        )

    def test_closed_provider_has_a_complete_success_lifecycle(self):
        plan = self._plan()
        profile = plan.profiles[0]
        authority = harness.ClosedVmwareProvider._profile_authority(profile, plan)
        _, _, _, runtime, _, verified_guest = self._connection_fixture()
        runtime = replace(
            runtime,
            clone_root=authority.clone_root,
            clone_vmx=authority.clone_vmx,
            snapshot_name=profile.snapshot_name,
            snapshot_identity=profile.snapshot_identity,
        )
        verified = harness.VerifiedCloneConnection(
            authority=authority,
            runtime=runtime,
            guest=verified_guest,
        )
        provider = harness.ClosedVmwareProvider(
            runner=RecordingRunner(),
            windows_platform=FakeWindowsPlatform(),
            environment={harness.GUEST_SUDO_PASSWORD_ENV: "test-only-password"}
        )
        receipt = self.provider.execute_profile(
            plan=profile,
            harness_plan=plan,
            candidate_root=self.root,
            initial_platform_state=harness._initial_platform_state(profile.profile),
        )
        events = []
        unrelated_running_sets = iter(
            frozenset({f"unrelated-{index}"}) for index in range(4)
        )
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(provider, "_assert_tools"))
            stack.enter_context(
                mock.patch.object(
                    provider,
                    "_acquire_provider_lease",
                    return_value=mock.sentinel.lease,
                )
            )
            release_lease = stack.enter_context(
                mock.patch.object(provider, "_release_provider_lease")
            )
            stack.enter_context(
                mock.patch.object(
                    provider, "_hashes", return_value=dict(plan.original_vm_hashes)
                )
            )
            stack.enter_context(
                mock.patch.object(
                    provider,
                    "_prepare_profile_authority",
                    side_effect=lambda value: events.append("prepare"),
                )
            )
            stack.enter_context(
                mock.patch.object(
                    provider,
                    "_clone_full",
                    side_effect=lambda value, profile_authority, **_kwargs: (
                        events.append("clone")
                        or (authority.clone_root, authority.clone_vmx)
                    ),
                )
            )
            stack.enter_context(
                mock.patch.object(
                    provider, "_vm_inventory", return_value={"fixed": (1, 2, 3)}
                )
            )
            stack.enter_context(
                mock.patch.object(
                    provider,
                    "_running_vmx_paths",
                    side_effect=lambda: events.append("power")
                    or next(unrelated_running_sets),
                )
            )
            revert = stack.enter_context(
                mock.patch.object(
                    provider,
                    "_revert_clone",
                    side_effect=lambda *_args: events.append("revert"),
                )
            )
            stack.enter_context(
                mock.patch.object(
                    provider,
                    "_validate_reverted_clone_disk_graph",
                    side_effect=lambda *_args, **_kwargs: events.append("validate")
                    or (),
                )
            )
            stack.enter_context(
                mock.patch.object(
                    provider,
                    "_clone_snapshot_disk_graph_identity",
                    return_value=profile.snapshot_disk_graph_identity,
                )
            )
            stack.enter_context(
                mock.patch.object(
                    provider,
                    "_clone_snapshot_identity",
                    return_value=profile.snapshot_identity,
                )
            )
            stack.enter_context(
                mock.patch.object(
                    provider,
                    "_disk_graph_content_digest",
                    return_value=runtime.disk_graph_digest,
                )
            )
            stack.enter_context(
                mock.patch.object(
                    provider,
                    "_inject_guestinfo_challenge",
                    side_effect=lambda profile_authority, item: events.append("inject"),
                )
            )
            start = stack.enter_context(
                mock.patch.object(
                    provider,
                    "_start_clone",
                    side_effect=lambda _value: events.append("start"),
                )
            )
            stack.enter_context(
                mock.patch.object(
                    provider,
                    "_establish_clone_connection",
                    side_effect=lambda profile_authority, item, **_kwargs: (
                        events.append("identity") or verified
                    ),
                )
            )
            stack.enter_context(
                mock.patch.object(
                    provider,
                    "_stage_candidate",
                    side_effect=lambda profile_authority, candidate_root, digest: (
                        events.append("stage") or "/fixed/candidate"
                    ),
                )
            )
            stack.enter_context(
                mock.patch.object(provider, "_run_profile_guest", return_value=receipt)
            )
            stop = stack.enter_context(mock.patch.object(provider, "_stop_clone"))
            remove = stack.enter_context(mock.patch.object(provider, "_remove_clone"))
            quarantine = stack.enter_context(
                mock.patch.object(provider, "_quarantine_clone")
            )
            observed = provider.execute_profile(
                plan=profile,
                harness_plan=plan,
                candidate_root=self.root,
                initial_platform_state=harness._initial_platform_state(profile.profile),
            )
        self.assertEqual(observed, receipt)
        self.assertEqual(
            events[:9],
            [
                "prepare",
                "clone",
                "power",
                "revert",
                "power",
                "validate",
                "power",
                "inject",
                "power",
            ],
        )
        self.assertLess(events.index("inject"), events.index("start"))
        self.assertLess(events.index("start"), events.index("identity"))
        self.assertLess(events.index("identity"), events.index("stage"))
        revert.assert_called_once_with(authority.clone_vmx, profile.snapshot_name)
        start.assert_called_once_with(authority.clone_vmx)
        stop.assert_called_once_with(authority.clone_vmx)
        remove.assert_called_once_with(verified)
        quarantine.assert_not_called()
        release_lease.assert_called_once_with(
            mock.sentinel.lease, work_root=harness.VM_WORK_PARENT
        )

    def test_host_commands_receive_only_sanitized_environment_and_stdin_secret(self):
        plan = self._plan()
        authority = harness.ClosedVmwareProvider._profile_authority(
            plan.profiles[0], plan
        )
        runner = RecordingRunner()
        provider = harness.ClosedVmwareProvider(
            runner=runner,
            windows_platform=FakeWindowsPlatform(),
            environment={
                "SystemRoot": "C:/Windows",
                "USERPROFILE": "C:/Users/tester",
                "PATH": "C:/attacker-controlled",
                harness.GUEST_SUDO_PASSWORD_ENV: "test-only-password",
                ACCESS_KEY_ENV: "test-only-r2-access",
                SECRET_KEY_ENV: "test-only-r2-secret",
            },
        )
        password = provider._sudo_password()
        provider._ssh_checked(
            authority,
            "sudo -S -p '' -- /usr/bin/true",
            code="TEST",
            sudo_password=password,
        )
        self.assertEqual(len(runner.calls), 1)
        call = runner.calls[0]
        self.assertNotIn("test-only-password", call["argv"])
        self.assertNotIn("test-only-r2-access", call["argv"])
        self.assertNotIn("test-only-r2-secret", call["argv"])
        self.assertEqual(call["input_bytes"], b"test-only-password\n")
        self.assertNotIn(harness.GUEST_SUDO_PASSWORD_ENV, call["environment"])
        self.assertNotIn(
            ACCESS_KEY_ENV, call["environment"]
        )
        self.assertNotIn(SECRET_KEY_ENV, call["environment"])
        self.assertNotIn("test-only-password", call["environment"].values())
        self.assertNotIn("test-only-r2-access", call["environment"].values())
        self.assertNotIn("test-only-r2-secret", call["environment"].values())
        self.assertNotIn("C:/attacker-controlled", call["environment"]["PATH"])
        self.assertEqual(call["environment"]["SYSTEMROOT"], "C:/Windows")

    def test_ssh_and_scp_use_equivalent_closed_configuration_authority(self):
        plan = self._plan()
        authority = harness.ClosedVmwareProvider._profile_authority(
            plan.profiles[0], plan
        )
        provider = harness.ClosedVmwareProvider(
            windows_platform=FakeWindowsPlatform(),
            environment={
                "HOME": "C:/Users/tester",
                "USERPROFILE": "C:/Users/tester",
                "SSH_AUTH_SOCK": "test-agent-endpoint",
            },
        )
        ssh_argv = provider._ssh_argv(authority, "/usr/bin/true")
        scp_argv = provider._scp_argv(
            authority=authority,
            source="C:/safe/source",
            destination="/tmp/safe-destination",
            recursive=True,
        )
        required_options = {
            "BatchMode=yes",
            "IdentitiesOnly=yes",
            "IdentityAgent=none",
            "ProxyCommand=none",
            "ProxyJump=none",
            "PermitLocalCommand=no",
            "ClearAllForwardings=yes",
            "ForwardAgent=no",
            "PasswordAuthentication=no",
            "KbdInteractiveAuthentication=no",
            "PreferredAuthentications=publickey",
            "RequestTTY=no",
            "StrictHostKeyChecking=yes",
            "GlobalKnownHostsFile=none",
            f"UserKnownHostsFile={authority.known_hosts_file}",
            f"IdentityFile={authority.identity_file}",
            f"HostKeyAlias={authority.host_key_alias}",
            f"User={harness.SSH_USER}",
        }
        for argv in (ssh_argv, scp_argv):
            self.assertEqual(argv[1:3], ("-F", "none"))
            self.assertTrue(required_options.issubset(set(argv)))
            self.assertNotIn("test-agent-endpoint", argv)
            self.assertNotIn("C:/Users/tester", argv)
        self.assertEqual(ssh_argv[-2], harness.SSH_HOST)
        self.assertEqual(scp_argv[-1], f"{harness.SSH_HOST}:/tmp/safe-destination")

    def test_openssh_environment_is_not_shared_with_generic_provider_commands(self):
        plan = self._plan()
        authority = harness.ClosedVmwareProvider._profile_authority(
            plan.profiles[0], plan
        )
        runner = RecordingRunner()
        provider = harness.ClosedVmwareProvider(
            runner=runner,
            windows_platform=FakeWindowsPlatform(),
            environment={
                "SystemRoot": "C:/Windows",
                "ProgramData": r"C:\ProgramData",
                "HOME": "C:/Users/tester",
                "USERPROFILE": "C:/Users/tester",
            },
        )
        provider._run((str(harness.VMRUN), "-T", "ws", "list"), code="TEST")
        provider._run((str(harness.ROBOCOPY), "source", "target"), code="TEST")
        provider._ssh_checked(authority, "/usr/bin/true", code="TEST")
        provider._run(
            provider._scp_argv(
                authority=authority,
                source="C:/safe/source",
                destination="/tmp/safe-destination",
                recursive=False,
            ),
            code="TEST",
            openssh=True,
        )
        vmrun_environment, robocopy_environment, ssh_environment, scp_environment = (
            call["environment"] for call in runner.calls
        )
        self.assertNotIn("PROGRAMDATA", vmrun_environment)
        self.assertNotIn("PROGRAMDATA", robocopy_environment)
        for environment in (ssh_environment, scp_environment):
            self.assertEqual(environment["PROGRAMDATA"], r"C:\ProgramData")
            self.assertNotIn("HOME", environment)
            self.assertNotIn("USERPROFILE", environment)

    def test_executable_scope_cannot_be_reassigned_by_an_internal_caller(self):
        runner = RecordingRunner()
        provider = harness.ClosedVmwareProvider(
            runner=runner,
            windows_platform=FakeWindowsPlatform(),
            environment={"ProgramData": r"C:\ProgramData"},
        )
        cases = (
            ((str(harness.VMRUN), "list"), True),
            ((str(harness.ROBOCOPY), "source", "target"), True),
            ((str(harness.SSH), "-V"), False),
            ((str(harness.SCP), "-V"), False),
        )
        for argv, openssh in cases:
            with self.subTest(argv=argv), self.assertRaisesRegex(
                harness.CandidateHarnessError,
                "WINDOWS_OPENSSH_CONFIG_AUTHORITY_UNSAFE",
            ):
                provider._run(argv, code="TEST", openssh=openssh)
        self.assertEqual(runner.calls, [])

    def test_windows_provider_rejects_rooted_paths_without_a_drive(self):
        for executable in (r"\Windows\System32\robocopy.exe", "/Windows/robocopy.exe"):
            with self.subTest(executable=executable):
                runner = RecordingRunner()
                provider = harness.ClosedVmwareProvider(
                    runner=runner,
                    windows_platform=FakeWindowsPlatform(),
                    environment={"ProgramData": r"C:\ProgramData"},
                )
                with mock.patch.object(
                    provider, "_tool_path", return_value=Path(executable)
                ), self.assertRaisesRegex(
                    harness.CandidateHarnessError,
                    "WINDOWS_OPENSSH_CONFIG_AUTHORITY_UNSAFE",
                ):
                    provider._run((str(harness.ROBOCOPY), "source", "target"), code="TEST")
                self.assertEqual(runner.calls, [])

    def test_windows_provider_environments_use_known_folder_authority_per_scope(self):
        platform = FakeWindowsPlatform()
        environments = harness.build_windows_provider_environments(
            {
                "SystemRoot": "C:/Windows",
                "ProgramData": r"C:\ProgramData",
                "HOME": "C:/Users/tester",
                "USERPROFILE": "C:/Users/tester",
                "SSH_AUTH_SOCK": "test-agent-endpoint",
                ACCESS_KEY_ENV: "test-only-r2-access",
                SECRET_KEY_ENV: "test-only-r2-secret",
            },
            platform=platform,
        )
        self.assertEqual(platform.resolve_calls, 1)
        self.assertNotIn("PROGRAMDATA", environments.generic)
        self.assertEqual(environments.openssh["PROGRAMDATA"], r"C:\ProgramData")
        for forbidden in (
            "HOME",
            "USERPROFILE",
            "SSH_AUTH_SOCK",
            ACCESS_KEY_ENV,
            SECRET_KEY_ENV,
        ):
            self.assertNotIn(forbidden, environments.openssh)
        self.assertEqual(
            [name for name in environments.openssh if name.upper() == "PROGRAMDATA"],
            ["PROGRAMDATA"],
        )

    def test_windows_provider_environment_rejects_invalid_program_data_before_use(self):
        cases = {
            "empty": FakeWindowsPlatform(program_data=""),
            "nul": FakeWindowsPlatform(program_data="C:\\Program\0Data"),
            "relative": FakeWindowsPlatform(program_data=r"relative\ProgramData"),
            "unc": FakeWindowsPlatform(program_data=r"\\server\share"),
            "device": FakeWindowsPlatform(program_data=r"\\?\C:\ProgramData"),
            "missing": FakeWindowsPlatform(directory_exists=False),
            "reparse": FakeWindowsPlatform(contains_reparse_point=True),
            "non-fixed": FakeWindowsPlatform(fixed_drive=False),
        }
        for name, platform in cases.items():
            with self.subTest(name=name), self.assertRaisesRegex(
                harness.CandidateHarnessError,
                "WINDOWS_OPENSSH_PROGRAMDATA_INVALID",
            ):
                harness.build_windows_provider_environments({}, platform=platform)

    def test_windows_provider_environment_rejects_unavailable_or_conflicting_authority(self):
        unavailable = FakeWindowsPlatform()
        unavailable.resolve_program_data = mock.Mock(side_effect=OSError("unavailable"))
        with self.assertRaisesRegex(
            harness.CandidateHarnessError,
            "WINDOWS_OPENSSH_PROGRAMDATA_UNAVAILABLE",
        ):
            harness.build_windows_provider_environments({}, platform=unavailable)

        with self.assertRaisesRegex(
            harness.CandidateHarnessError,
            "WINDOWS_OPENSSH_ENVIRONMENT_CONFLICT",
        ):
            harness.build_windows_provider_environments(
                {
                    "ProgramData": r"C:\ProgramData",
                    "PROGRAMDATA": r"D:\Unexpected",
                },
                platform=FakeWindowsPlatform(),
            )

        with self.assertRaisesRegex(
            harness.CandidateHarnessError,
            "WINDOWS_OPENSSH_PROGRAMDATA_INVALID",
        ):
            harness.build_windows_provider_environments(
                {"ProgramData": r"D:\Unexpected"},
                platform=FakeWindowsPlatform(),
            )

    def test_win32_adapter_declares_the_complete_candidate_harness_abi_surface(self):
        loader = RecordingWin32Loader()
        harness._WindowsApiAdapter(dll_loader=loader)

        self.assertEqual(
            loader.calls,
            [
                ("advapi32", True),
                ("kernel32", True),
                ("ole32", True),
                ("shell32", True),
            ],
        )
        expected_functions = {
            "advapi32": {
                "CreateWellKnownSid",
                "EqualSid",
                "GetEffectiveRightsFromAclW",
                "GetNamedSecurityInfoW",
                "GetTokenInformation",
                "OpenProcessToken",
            },
            "kernel32": {"CloseHandle", "GetCurrentProcess", "GetDriveTypeW", "LocalFree"},
            "ole32": {"CoTaskMemFree"},
            "shell32": {"SHGetKnownFolderPath"},
        }
        for library_name, function_names in expected_functions.items():
            library = loader.libraries[library_name]
            self.assertEqual(set(library.functions), function_names)
            for function_name in function_names:
                function = library.functions[function_name]
                self.assertTrue(function.argtypes_assigned, function_name)
                self.assertTrue(function.restype_assigned, function_name)
        self.assertNotIn("ctypes.windll", Path(harness.__file__).read_text(encoding="utf-8"))

        parsed = ast.parse(Path(harness.__file__).read_text(encoding="utf-8"))
        native_call_names = sorted(
            node.func.attr
            for node in ast.walk(parsed)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Attribute)
            and isinstance(node.func.value.value, ast.Name)
            and node.func.value.value.id == "self"
            and node.func.value.attr
            in {"_advapi32", "_kernel32", "_ole32", "_shell32"}
        )
        self.assertEqual(
            native_call_names,
            sorted(
                [
                    "SHGetKnownFolderPath",
                    "CoTaskMemFree",
                    "GetDriveTypeW",
                    "GetNamedSecurityInfoW",
                    "OpenProcessToken",
                    "GetCurrentProcess",
                    "GetTokenInformation",
                    "GetTokenInformation",
                    "EqualSid",
                    "CreateWellKnownSid",
                    "GetEffectiveRightsFromAclW",
                    "CloseHandle",
                    "LocalFree",
                ]
            ),
        )

    def test_win32_adapter_declares_typed_output_pointer_and_release_prototypes(self):
        loader = RecordingWin32Loader()
        harness._WindowsApiAdapter(dll_loader=loader)
        pointer_to_void_pointer = ctypes.POINTER(ctypes.c_void_p)
        prototypes = {
            "advapi32": {
                "CreateWellKnownSid": (
                    (
                        ctypes.c_int,
                        ctypes.c_void_p,
                        ctypes.c_void_p,
                        ctypes.POINTER(wintypes.DWORD),
                    ),
                    wintypes.BOOL,
                ),
                "EqualSid": (
                    (ctypes.c_void_p, ctypes.c_void_p),
                    wintypes.BOOL,
                ),
                "GetEffectiveRightsFromAclW": (
                    (
                        ctypes.c_void_p,
                        ctypes.POINTER(harness._WindowsTrustee),
                        ctypes.POINTER(wintypes.DWORD),
                    ),
                    wintypes.DWORD,
                ),
                "GetNamedSecurityInfoW": (
                    (
                        wintypes.LPCWSTR,
                        ctypes.c_int,
                        wintypes.DWORD,
                        pointer_to_void_pointer,
                        pointer_to_void_pointer,
                        pointer_to_void_pointer,
                        pointer_to_void_pointer,
                        pointer_to_void_pointer,
                    ),
                    wintypes.DWORD,
                ),
                "GetTokenInformation": (
                    (
                        wintypes.HANDLE,
                        ctypes.c_int,
                        ctypes.c_void_p,
                        wintypes.DWORD,
                        ctypes.POINTER(wintypes.DWORD),
                    ),
                    wintypes.BOOL,
                ),
                "OpenProcessToken": (
                    (
                        wintypes.HANDLE,
                        wintypes.DWORD,
                        ctypes.POINTER(wintypes.HANDLE),
                    ),
                    wintypes.BOOL,
                ),
            },
            "kernel32": {
                "CloseHandle": ((wintypes.HANDLE,), wintypes.BOOL),
                "GetCurrentProcess": ((), wintypes.HANDLE),
                "GetDriveTypeW": ((wintypes.LPCWSTR,), wintypes.UINT),
                "LocalFree": ((ctypes.c_void_p,), ctypes.c_void_p),
            },
            "ole32": {
                "CoTaskMemFree": ((ctypes.c_void_p,), None),
            },
            "shell32": {
                "SHGetKnownFolderPath": (
                    (
                        ctypes.POINTER(harness._WindowsGuid),
                        wintypes.DWORD,
                        wintypes.HANDLE,
                        ctypes.POINTER(ctypes.c_wchar_p),
                    ),
                    ctypes.c_long,
                ),
            },
        }
        for library_name, expected_functions in prototypes.items():
            functions = loader.libraries[library_name].functions
            for function_name, (argtypes, restype) in expected_functions.items():
                with self.subTest(library=library_name, function=function_name):
                    self.assertEqual(functions[function_name].argtypes, argtypes)
                    self.assertEqual(functions[function_name].restype, restype)
        self.assertEqual(
            ctypes.sizeof(harness._WindowsSidAndAttributes),
            ctypes.sizeof(ctypes.c_void_p) * 2,
        )

    def test_win32_known_folder_memory_is_released_exactly_once(self):
        loader = RecordingWin32Loader()
        folder = ctypes.create_unicode_buffer(r"C:\ProgramData")

        def known_folder(_folder_id, _flags, _token, output):
            ctypes.cast(output, ctypes.POINTER(ctypes.c_void_p)).contents.value = (
                ctypes.addressof(folder)
            )
            return 0

        loader.libraries.setdefault("shell32", RecordingWin32Library()).functions[
            "SHGetKnownFolderPath"
        ] = RecordingWin32Function(known_folder)
        adapter = harness._WindowsApiAdapter(dll_loader=loader)

        self.assertEqual(adapter.resolve_program_data(), r"C:\ProgramData")
        self.assertEqual(
            len(loader.libraries["ole32"].functions["CoTaskMemFree"].calls), 1
        )

    def test_win32_known_folder_failure_does_not_free_unacquired_memory(self):
        folder = ctypes.create_unicode_buffer(r"C:\dirty-output")
        for dirty_output in (False, True):
            loader = RecordingWin32Loader()

            def known_folder_failure(_folder_id, _flags, _token, output):
                if dirty_output:
                    ctypes.cast(
                        output, ctypes.POINTER(ctypes.c_void_p)
                    ).contents.value = ctypes.addressof(folder)
                return -2147467259

            loader.libraries.setdefault("shell32", RecordingWin32Library()).functions[
                "SHGetKnownFolderPath"
            ] = RecordingWin32Function(known_folder_failure)
            adapter = harness._WindowsApiAdapter(dll_loader=loader)

            with self.subTest(dirty_output=dirty_output), self.assertRaisesRegex(
                OSError, "FOLDERID_ProgramData is unavailable"
            ):
                adapter.resolve_program_data()
            self.assertEqual(
                len(loader.libraries["ole32"].functions["CoTaskMemFree"].calls),
                int(dirty_output),
            )

    def test_win32_adapter_uses_pointer_sized_process_token_and_sid_prototypes(self):
        loader = RecordingWin32Loader()
        harness._WindowsApiAdapter(dll_loader=loader)
        advapi32 = loader.libraries["advapi32"]
        kernel32 = loader.libraries["kernel32"]

        self.assertEqual(
            advapi32.functions["OpenProcessToken"].argtypes,
            (
                wintypes.HANDLE,
                wintypes.DWORD,
                ctypes.POINTER(wintypes.HANDLE),
            ),
        )
        self.assertEqual(
            advapi32.functions["EqualSid"].argtypes,
            (ctypes.c_void_p, ctypes.c_void_p),
        )
        self.assertEqual(
            kernel32.functions["GetCurrentProcess"].restype,
            wintypes.HANDLE,
        )
        self.assertEqual(ctypes.sizeof(wintypes.HANDLE), ctypes.sizeof(ctypes.c_void_p))

    def test_win32_acl_adapter_preserves_64_bit_pointers_and_releases_once(self):
        loader, state = _acl_loader()
        adapter = _acl_adapter(loader)

        inspection = adapter.inspect_file_acl(Path("controlled-ssh.exe"))

        self.assertEqual(inspection.status, "PASS")
        self.assertEqual(state["owner_seen"], 0x1234567887654321)
        self.assertEqual(state["current_user_seen"], 0x1234567887654321)
        self.assertEqual(state["effective_index"], 3)
        self.assertEqual(
            len(loader.libraries["kernel32"].functions["CloseHandle"].calls), 1
        )
        self.assertEqual(
            len(loader.libraries["kernel32"].functions["LocalFree"].calls), 1
        )

    def test_legacy_32_bit_pointer_truncation_fixture_reproduces_false_result(self):
        pointer = 0x1234567887654321
        legacy_default_c_int = ctypes.c_int(pointer).value
        self.assertNotEqual(legacy_default_c_int, pointer)
        legacy_open_process_token = legacy_default_c_int == pointer
        legacy_equal_sid = legacy_default_c_int == pointer
        legacy_private_file = legacy_open_process_token and legacy_equal_sid
        self.assertFalse(legacy_open_process_token)
        self.assertFalse(legacy_equal_sid)
        self.assertFalse(legacy_private_file)

        loader, state = _acl_loader(owner=pointer, current_user=pointer)
        inspection = _acl_adapter(loader).inspect_file_acl(
            Path("controlled-ssh.exe")
        )

        self.assertEqual(state["owner_seen"], pointer)
        self.assertEqual(state["current_user_seen"], pointer)
        self.assertEqual(inspection.status, "PASS")

    def test_legacy_unprototyped_surface_fixture_fails_all_13_callpoints(self):
        callpoints = (
            "SHGetKnownFolderPath",
            "CoTaskMemFree",
            "GetDriveTypeW",
            "GetNamedSecurityInfoW",
            "OpenProcessToken",
            "GetCurrentProcess",
            "GetTokenInformation",
            "GetTokenInformation",
            "EqualSid",
            "CreateWellKnownSid",
            "GetEffectiveRightsFromAclW",
            "CloseHandle",
            "LocalFree",
        )
        legacy_functions = {
            name: RecordingWin32Function() for name in set(callpoints)
        }
        missing = [
            name
            for name in callpoints
            if not legacy_functions[name].argtypes_assigned
            or not legacy_functions[name].restype_assigned
        ]
        self.assertEqual(missing, list(callpoints))

    def test_win32_acl_adapter_rejects_owner_descriptor_and_dacl_anomalies(self):
        cases = {
            "owner-mismatch": (
                {"equal_sid_result": False},
                "OWNER_MISMATCH",
            ),
            "invalid-owner-sid": (
                {"equal_sid_result": False, "equal_sid_error": 1337},
                "SECURITY_DESCRIPTOR_INVALID",
            ),
            "owner-query-failure": (
                {"equal_sid_result": False, "equal_sid_error": 5},
                "ACL_QUERY_FAILED",
            ),
            "null-owner": ({"owner": None}, "SECURITY_DESCRIPTOR_INVALID"),
            "null-descriptor": (
                {"descriptor": None},
                "SECURITY_DESCRIPTOR_INVALID",
            ),
            "null-dacl": ({"dacl": None}, "ACL_UNSAFE"),
        }
        for name, (options, expected) in cases.items():
            loader, _ = _acl_loader(**options)
            with self.subTest(name=name):
                inspection = _acl_adapter(loader).inspect_file_acl(
                    Path("controlled-ssh.exe")
                )
                self.assertEqual(inspection.status, expected)

    def test_win32_acl_adapter_never_releases_dirty_failure_outputs(self):
        cases = {
            "GetNamedSecurityInfoW": {
                "named_result": 5,
                "dirty_named_outputs": True,
            },
            "OpenProcessToken": {
                "open_token": False,
                "dirty_token_output": True,
            },
        }
        for name, options in cases.items():
            loader, _ = _acl_loader(**options)
            with self.subTest(api=name):
                inspection = _acl_adapter(loader).inspect_file_acl(
                    Path("controlled-ssh.exe")
                )
                self.assertEqual(inspection.status, "ACL_QUERY_FAILED")
                if name == "GetNamedSecurityInfoW":
                    self.assertEqual(
                        len(
                            loader.libraries["kernel32"]
                            .functions["LocalFree"]
                            .calls
                        ),
                        0,
                    )
                else:
                    self.assertEqual(
                        len(
                            loader.libraries["kernel32"]
                            .functions["CloseHandle"]
                            .calls
                        ),
                        0,
                    )
                    self.assertEqual(
                        len(
                            loader.libraries["kernel32"]
                            .functions["LocalFree"]
                            .calls
                        ),
                        1,
                    )

    def test_win32_acl_adapter_rejects_each_broad_principal(self):
        for index, principal in enumerate(
            ("Everyone", "Authenticated Users", "Builtin Users")
        ):
            loader, state = _acl_loader(broad_access_index=index)
            with self.subTest(principal=principal):
                inspection = _acl_adapter(loader).inspect_file_acl(
                    Path("controlled-ssh.exe")
                )
                self.assertEqual(inspection.status, "ACL_UNSAFE")
                self.assertEqual(state["effective_index"], index + 1)

    def test_win32_acl_adapter_distinguishes_all_query_failures(self):
        cases = {
            "GetNamedSecurityInfoW": {"named_result": 5},
            "OpenProcessToken": {"open_token": False},
            "GetTokenInformation-unexpected-probe-success": {
                "token_probe_success": True
            },
            "GetTokenInformation-size-probe": {"token_probe_error": 5},
            "GetTokenInformation-query": {"token_query": False},
            "GetEffectiveRightsFromAclW": {"effective_result_index": 1},
        }
        for name, options in cases.items():
            loader, _ = _acl_loader(**options)
            with self.subTest(api=name):
                inspection = _acl_adapter(loader).inspect_file_acl(
                    Path("controlled-ssh.exe")
                )
                self.assertEqual(inspection.status, "ACL_QUERY_FAILED")

    def test_win32_acl_adapter_fails_closed_on_native_release_failure(self):
        cases = {
            "LocalFree-return": {"local_free_result": 0x3456789AA9876543},
            "LocalFree-exception": {"local_free_error": True},
            "CloseHandle": {"close_result": False},
        }
        for name, options in cases.items():
            loader, _ = _acl_loader(**options)
            with self.subTest(release=name):
                inspection = _acl_adapter(loader).inspect_file_acl(
                    Path("controlled-ssh.exe")
                )
                self.assertEqual(inspection.status, "RESOURCE_RELEASE_FAILED")

    def test_native_windows_authority_classifies_adapter_initialization_failure(self):
        controlled = self.root / "controlled"
        controlled.mkdir()
        fixture = controlled / "ssh.exe"
        fixture.write_bytes(b"public fixture bytes")
        authority = harness.NativeWindowsPlatformAuthority(
            dll_loader=mock.Mock(side_effect=OSError("unsupported ABI"))
        )

        inspection = authority.inspect_controlled_file(
            fixture, root=controlled, private=True
        )

        self.assertEqual(inspection.status, "ABI_UNSUPPORTED")

    def test_native_windows_authority_reports_unsupported_nonwindows_abi(self):
        authority = harness.NativeWindowsPlatformAuthority(
            dll_loader=mock.Mock(side_effect=AssertionError("must remain lazy"))
        )
        with mock.patch.object(harness.os, "name", "posix"):
            inspection = authority.inspect_controlled_file(
                self.root / "ssh.exe", root=self.root, private=True
            )
        self.assertEqual(inspection.status, "ABI_UNSUPPORTED")

    @unittest.skipUnless(harness.os.name == "nt", "Windows path authority test")
    def test_native_windows_authority_distinguishes_path_query_and_policy_failure(self):
        controlled = self.root / "controlled-path"
        controlled.mkdir()
        fixture = controlled / "ssh.exe"
        fixture.write_bytes(b"fixture")
        outside = self.root / "outside.exe"
        outside.write_bytes(b"fixture")
        authority = harness.NativeWindowsPlatformAuthority()

        with mock.patch.object(
            authority,
            "has_reparse_component",
            side_effect=OSError("query failed"),
        ):
            query_failure = authority.inspect_controlled_file(
                fixture, root=controlled, private=False
            )
        containment_failure = authority.inspect_controlled_file(
            outside, root=controlled, private=False
        )

        self.assertEqual(query_failure.status, "ACL_QUERY_FAILED")
        self.assertEqual(containment_failure.status, "ACL_UNSAFE")

    @unittest.skipUnless(harness.os.name == "nt", "Windows read-only fixture test")
    def test_real_openssh_bytes_are_private_in_task_owned_readonly_fixtures(self):
        controlled = self.root / "controlled-openssh"
        controlled.mkdir()
        authority = harness.NativeWindowsPlatformAuthority()
        results = []
        for public_binary in (harness.SSH, harness.SCP):
            fixture = controlled / public_binary.name
            shutil.copyfile(public_binary, fixture)
            results.append(
                authority.inspect_controlled_file(
                    fixture, root=controlled, private=True
                ).status
            )
        self.assertEqual(results, ["PASS", "PASS"])

    @unittest.skipUnless(harness.os.name == "nt", "Windows provider authority test")
    def test_production_execution_authority_runs_readiness_from_private_tools(self):
        source = self.root / "source-vm"
        source.mkdir()
        (source / "authority.txt").write_bytes(b"source authority fixture")
        key_root = harness.create_windows_private_directory(
            self.root, prefix="bootstrap-authority"
        )
        bootstrap_identity = key_root / "id_ed25519"
        bootstrap_identity.write_bytes(b"test-only-bootstrap-identity")
        provider = harness.ClosedVmwareProvider()

        with mock.patch.multiple(
            harness,
            PROVIDER_EXECUTION_PARENT=Path("E:/"),
            OPENSSH_IDENTITY=bootstrap_identity,
            SOURCE_VM_ROOT=source,
        ):
            with self.assertRaisesRegex(
                harness.CandidateHarnessError,
                "CANDIDATE_VM_EXECUTION_AUTHORITY_REQUIRED",
            ):
                provider.inspect_readiness()
            with provider.execution_authority() as receipt:
                private_ssh = provider._tool_path(harness.SSH)
                private_libcrypto = provider._tool_path(
                    harness.OPENSSH_LIBCRYPTO
                )
                private_root = provider._execution.root
                readiness = provider.inspect_readiness()
                self.assertTrue(private_ssh.is_relative_to(private_root))
                self.assertEqual(private_libcrypto.parent, private_ssh.parent)
                self.assertIn(
                    "libcrypto.dll",
                    harness.inspect_windows_pe_imports(private_ssh),
                )
                self.assertNotEqual(private_ssh, harness.SSH)
                self.assertEqual(readiness.result, "PASS")
                self.assertEqual(
                    receipt.system_tool_identities["libcrypto.dll"],
                    harness.EXPECTED_OPENSSH_LIBCRYPTO_SHA256,
                )
                self.assertEqual(receipt.source_vm_inventory_identity, None)
            self.assertFalse(private_root.exists())

    def test_provider_readiness_distinguishes_acl_query_policy_and_abi_failures(self):
        cases = {
            "query": ("ACL_QUERY_FAILED", "WINDOWS_OPENSSH_ACL_QUERY_FAILED"),
            "unsafe": ("ACL_UNSAFE", "WINDOWS_OPENSSH_ACL_UNSAFE"),
            "owner": ("OWNER_MISMATCH", "WINDOWS_OPENSSH_OWNER_MISMATCH"),
            "abi": ("ABI_UNSUPPORTED", "WINDOWS_WIN32_ABI_UNSUPPORTED"),
            "descriptor": (
                "SECURITY_DESCRIPTOR_INVALID",
                "WINDOWS_WIN32_SECURITY_DESCRIPTOR_INVALID",
            ),
        }
        for name, (status, expected_code) in cases.items():
            provider = harness.ClosedVmwareProvider(
                runner=RecordingRunner(),
                windows_platform=FakeWindowsPlatform(identity_status=status),
                environment={"ProgramData": r"C:\ProgramData"},
            )
            with self.subTest(name=name), self.assertRaisesRegex(
                harness.CandidateHarnessError, expected_code
            ):
                provider.inspect_readiness()

    def test_win32_adapter_is_not_loaded_during_nonwindows_import_paths(self):
        loader = mock.Mock(side_effect=AssertionError("Win32 loader must stay lazy"))
        authority = harness.NativeWindowsPlatformAuthority(dll_loader=loader)
        with mock.patch.object(harness.os, "name", "posix"), self.assertRaisesRegex(
            OSError, "Windows Known Folder API is unavailable"
        ):
            authority.resolve_program_data()
        loader.assert_not_called()

    def test_provider_readiness_is_secret_free_cached_and_local_only(self):
        runner = RecordingRunner()
        provider = harness.ClosedVmwareProvider(
            runner=runner,
            windows_platform=FakeWindowsPlatform(),
            environment={
                "ProgramData": r"C:\ProgramData",
                "SSH_AUTH_SOCK": "test-agent-endpoint",
                ACCESS_KEY_ENV: "test-only-r2-access",
                SECRET_KEY_ENV: "test-only-r2-secret",
            },
        )
        first = provider.inspect_readiness()
        second = provider.inspect_readiness()
        self.assertIs(first, second)
        self.assertEqual(first.result, "PASS")
        self.assertRegex(first.receipt_digest, r"^sha256:[0-9a-f]{64}$")
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(runner.calls[0]["argv"], (str(harness.SSH), "-V"))
        serialized = str(first.as_dict())
        for forbidden in (
            str(harness.OPENSSH_IDENTITY),
            r"C:\ProgramData",
            "test-agent-endpoint",
            "test-only-r2-access",
            "test-only-r2-secret",
        ):
            self.assertNotIn(forbidden, serialized)

    def test_provider_readiness_fails_closed_with_stable_classification(self):
        cases = {
            "binary-unavailable": (
                FakeWindowsPlatform(binary_available=False),
                RecordingRunner(),
                "WINDOWS_OPENSSH_BINARY_UNAVAILABLE",
            ),
            "ssh-identity": (
                FakeWindowsPlatform(ssh_digest="sha256:" + "0" * 64),
                RecordingRunner(),
                "WINDOWS_OPENSSH_IDENTITY_MISMATCH",
            ),
            "vmrun-identity": (
                FakeWindowsPlatform(vmrun_digest="sha256:" + "0" * 64),
                RecordingRunner(),
                "WINDOWS_VM_TOOL_IDENTITY_MISMATCH",
            ),
            "architecture": (
                FakeWindowsPlatform(machine=0x14C),
                RecordingRunner(),
                "WINDOWS_OPENSSH_IDENTITY_MISMATCH",
            ),
            "identity-capability": (
                FakeWindowsPlatform(identity_safe=False),
                RecordingRunner(),
                "WINDOWS_OPENSSH_ACL_UNSAFE",
            ),
            "version-startup": (
                FakeWindowsPlatform(),
                RecordingRunner(returncodes={(str(harness.SSH), "-V"): 255}),
                "WINDOWS_OPENSSH_READINESS_FAILED",
            ),
        }
        for name, (platform, runner, code) in cases.items():
            provider = harness.ClosedVmwareProvider(
                runner=runner,
                windows_platform=platform,
                environment={"ProgramData": r"C:\ProgramData"},
            )
            with self.subTest(name=name), self.assertRaisesRegex(
                harness.CandidateHarnessError, code
            ):
                provider.inspect_readiness()

    def test_readiness_precedes_source_identity_and_binds_all_profile_plans(self):
        plan = self._plan()
        self.assertEqual(self.provider.events[:2], ["readiness", "source"])
        self.assertEqual(self.provider.readiness_calls, 1)
        self.assertRegex(
            plan.provider_readiness_receipt_digest, r"^sha256:[0-9a-f]{64}$"
        )
        self.assertEqual(
            {
                item.provider_readiness_receipt_digest
                for item in plan.profiles
            },
            {plan.provider_readiness_receipt_digest},
        )

    def test_readiness_failure_stops_before_source_identity_or_clone(self):
        self.provider.readiness_error = harness.CandidateHarnessError(
            "WINDOWS_OPENSSH_READINESS_FAILED"
        )
        with self.assertRaisesRegex(
            harness.CandidateHarnessError, "WINDOWS_OPENSSH_READINESS_FAILED"
        ):
            self._plan()
        self.assertEqual(self.provider.events, ["readiness"])
        self.assertEqual(self.provider.execute_calls, 0)

    def test_every_profile_stops_before_clone_boot_or_network_on_readiness_failure(self):
        plan = self._plan()
        failures = {
            "program-data": FakeWindowsPlatform(directory_exists=False),
            "scp-identity": FakeWindowsPlatform(
                scp_digest="sha256:" + "0" * 64
            ),
            "config-authority": FakeWindowsPlatform(known_hosts_safe=False),
        }
        for profile in plan.profiles:
            for name, platform in failures.items():
                provider = harness.ClosedVmwareProvider(
                    runner=RecordingRunner(),
                    windows_platform=platform,
                    environment={"ProgramData": r"C:\ProgramData"},
                )
                with self.subTest(profile=profile.profile, failure=name), mock.patch.object(
                    provider, "_clone_full"
                ) as clone, mock.patch.object(
                    provider, "_start_clone"
                ) as boot, mock.patch.object(
                    provider, "_wait_for_ssh"
                ) as network, self.assertRaises(harness.CandidateHarnessError):
                    provider.execute_profile(
                        plan=profile,
                        harness_plan=plan,
                        candidate_root=self.root,
                        initial_platform_state=harness._initial_platform_state(
                            profile.profile
                        ),
                    )
                clone.assert_not_called()
                boot.assert_not_called()
                network.assert_not_called()

    def test_documented_windows_provider_contract_matches_closed_implementation(self):
        contract = (
            Path(__file__).resolve().parents[2]
            / "docs"
            / "candidate-acceptance-contract-v1.md"
        ).read_text(encoding="utf-8")
        for required in (
            "FOLDERID_ProgramData",
            "Generic Provider 与 OpenSSH subprocess 环境分开",
            "-F none",
            "IdentitiesOnly=yes",
            "GlobalKnownHostsFile=none",
            "ssh.exe -V",
            "Clone create、VM boot 和真实 SSH/SCP 计数都必须为零",
            "WINDOWS_OPENSSH_CONFIG_AUTHORITY_UNSAFE",
            "WINDOWS_OPENSSH_ACL_QUERY_FAILED",
            "WINDOWS_OPENSSH_ACL_UNSAFE",
            "WINDOWS_OPENSSH_OWNER_MISMATCH",
            "WINDOWS_WIN32_ABI_UNSUPPORTED",
            "WINDOWS_WIN32_SECURITY_DESCRIPTOR_INVALID",
        ):
            self.assertIn(required, contract)

    def test_shared_writable_vmx_and_vmdk_references_are_rejected(self):
        outside = self.root / "shared.vmdk"
        outside.write_bytes(b"shared")
        (self.root / "shared-flat.vmdk").write_bytes(b"shared-flat")
        fixtures = {
            "vmx-parent": (
                'scsi0:0.fileName = "../shared.vmdk"\n',
                b"# Disk DescriptorFile\nparentCID=ffffffff\n",
            ),
            "vmdk-parent": (
                'scsi0:0.fileName = "disk.vmdk"\n',
                b'# Disk DescriptorFile\ncreateType="twoGbMaxExtentSparse"\n'
                b'parentCID=00000001\n'
                b'parentFileNameHint="../shared.vmdk"\n',
            ),
            "vmdk-flat-offset": (
                'scsi0:0.fileName = "disk.vmdk"\n',
                b'# Disk DescriptorFile\ncreateType="twoGbMaxExtentFlat"\n'
                b'parentCID=ffffffff\n'
                b'RW 100 FLAT "../shared-flat.vmdk" 0\n',
            ),
            "vmdk-raw-device-map": (
                'scsi0:0.fileName = "disk.vmdk"\n',
                b'# Disk DescriptorFile\ncreateType="vmfsRawDeviceMap"\n'
                b'parentCID=ffffffff\n'
                b'RW 100 VMFSRDM "inside-rdmp.vmdk"\n',
            ),
        }
        for name, (vmx_text, descriptor) in fixtures.items():
            with self.subTest(name=name):
                clone = self.root / name
                clone.mkdir()
                vmx = clone / "Ubuntu 64 位.vmx"
                vmx.write_text(vmx_text, encoding="utf-8")
                (clone / "disk.vmdk").write_bytes(descriptor)
                (clone / "inside-rdmp.vmdk").write_bytes(b"raw-device-map")
                with self.assertRaisesRegex(
                    harness.CandidateHarnessError, "SHARED_DISK_REJECTED"
                ):
                    harness.ClosedVmwareProvider._validate_clone_disk_graph(
                        clone, vmx
                    )

    def test_clone_local_flat_extent_with_offset_is_accepted(self):
        clone = self.root / "local-flat"
        clone.mkdir()
        vmx = clone / "Ubuntu 64 位.vmx"
        vmx.write_text(
            'scsi0:0.fileName = "disk.vmdk"\n', encoding="utf-8"
        )
        (clone / "disk.vmdk").write_bytes(
            b'# Disk DescriptorFile\ncreateType="twoGbMaxExtentFlat"\n'
            b'parentCID=ffffffff\n'
            b'RW 100 FLAT "disk-flat.vmdk" 0\n'
        )
        (clone / "disk-flat.vmdk").write_bytes(b"local-flat")
        harness.ClosedVmwareProvider._validate_clone_disk_graph(clone, vmx)

    def test_disk_graph_identity_changes_when_extent_bytes_change_at_same_size(self):
        clone = self.root / "content-bound-disk-graph"
        clone.mkdir()
        vmx = clone / "Ubuntu 64 位.vmx"
        vmx.write_text('scsi0:0.fileName = "disk.vmdk"\n', encoding="utf-8")
        (clone / "disk.vmdk").write_bytes(
            b'# Disk DescriptorFile\ncreateType="twoGbMaxExtentFlat"\n'
            b'parentCID=ffffffff\nRW 100 FLAT "disk-flat.vmdk" 0\n'
        )
        extent = clone / "disk-flat.vmdk"
        extent.write_bytes(b"first-bytes")

        first = harness.ClosedVmwareProvider._disk_graph_content_digest(clone, vmx)
        extent.write_bytes(b"other-bytes")
        second = harness.ClosedVmwareProvider._disk_graph_content_digest(clone, vmx)

        self.assertNotEqual(first, second)

    def _reverted_disk_fixture(
        self,
        name: str,
        *,
        new_delta: bool = True,
        delta_preexisting: bool = False,
    ) -> SimpleNamespace:
        clone = self.root / name
        clone.mkdir()
        vmx = clone / f"{harness.SOURCE_VM_IDENTITY}.vmx"
        snapshot_descriptor = harness.SNAPSHOT_DISK_FILES["FRESH_BASE"]
        snapshot_extent = snapshot_descriptor.removesuffix(".vmdk") + "-s001.vmdk"
        (clone / snapshot_descriptor).write_bytes(
            b"# Disk DescriptorFile\n"
            b"version=1\n"
            b"CID=11111111\n"
            b"parentCID=ffffffff\n"
            b'createType="twoGbMaxExtentSparse"\n'
            + f'RW 100 SPARSE "{snapshot_extent}"\n'.encode("utf-8")
        )
        (clone / snapshot_extent).write_bytes(b"sealed-snapshot-extent")
        vmx.write_text(
            f'scsi0:0.fileName = "{snapshot_descriptor}"\n', encoding="utf-8"
        )
        active_descriptor = f"{harness.SOURCE_VM_IDENTITY}-000004.vmdk"
        active_extent = active_descriptor.removesuffix(".vmdk") + "-s001.vmdk"
        fixture = SimpleNamespace(
            clone=clone,
            vmx=vmx,
            snapshot_descriptor=snapshot_descriptor,
            snapshot_extent=snapshot_extent,
            active_descriptor=active_descriptor,
            active_extent=active_extent,
            expected_hashes={
                snapshot_descriptor: harness._hash_regular_file(
                    clone / snapshot_descriptor
                ),
                snapshot_extent: harness._hash_regular_file(clone / snapshot_extent),
            },
        )
        if delta_preexisting:
            self._write_revert_delta_descriptor(fixture)
            (clone / active_extent).write_bytes(b"preexisting-delta-extent")
            vmx.write_text(
                f'scsi0:0.fileName = "{active_descriptor}"\n', encoding="utf-8"
            )
        fixture.pre_revert_inventory = (
            harness.ClosedVmwareProvider._vm_inventory(clone)
        )
        if new_delta and not delta_preexisting:
            self._write_revert_delta_descriptor(fixture)
            (clone / active_extent).write_bytes(b"new-private-delta-extent")
            vmx.write_text(
                f'scsi0:0.fileName = "{active_descriptor}"\n', encoding="utf-8"
            )
        return fixture

    @staticmethod
    def _write_revert_delta_descriptor(
        fixture: SimpleNamespace,
        *,
        parent_hint: str | None = None,
        parent_cid: str = "11111111",
        sectors: int = 100,
        tail: bytes = b"",
    ) -> None:
        parent_hint = parent_hint or fixture.snapshot_descriptor
        (fixture.clone / fixture.active_descriptor).write_bytes(
            b"# Disk DescriptorFile\n"
            b"version=1\n"
            b"CID=22222222\n"
            + f"parentCID={parent_cid}\n".encode("ascii")
            + b'createType="twoGbMaxExtentSparse"\n'
            + f'parentFileNameHint="{parent_hint}"\n'.encode("utf-8")
            + f'RW {sectors} SPARSE "{fixture.active_extent}"\n'.encode("utf-8")
            + tail
        )

    @staticmethod
    def _validate_reverted_disk_fixture(fixture: SimpleNamespace):
        return harness.ClosedVmwareProvider._validate_reverted_clone_disk_graph(
            fixture.clone,
            fixture.vmx,
            profile="FRESH_BASE",
            expected_original_hashes=fixture.expected_hashes,
            pre_revert_inventory=fixture.pre_revert_inventory,
        )

    def test_reverted_clone_accepts_only_a_new_snapshot_bound_delta(self):
        fixture = self._reverted_disk_fixture("reverted-private-delta")

        graph = self._validate_reverted_disk_fixture(fixture)

        self.assertEqual(
            {
                path.relative_to(fixture.clone).as_posix()
                for path in graph
                if path.suffix.casefold() == ".vmdk"
            },
            {
                fixture.snapshot_descriptor,
                fixture.snapshot_extent,
                fixture.active_descriptor,
                fixture.active_extent,
            },
        )

    def test_reverted_clone_rejects_a_preexisting_unplanned_delta(self):
        fixture = self._reverted_disk_fixture(
            "preexisting-revert-delta",
            delta_preexisting=True,
        )

        with self.assertRaisesRegex(
            harness.CandidateHarnessError,
            "CANDIDATE_VM_REVERT_DISK_GRAPH_UNPLANNED",
        ):
            self._validate_reverted_disk_fixture(fixture)

    def test_reverted_clone_rejects_direct_immutable_snapshot_graph(self):
        fixture = self._reverted_disk_fixture(
            "reverted-direct-snapshot",
            new_delta=False,
        )

        with self.assertRaisesRegex(
            harness.CandidateHarnessError,
            "CANDIDATE_VM_REVERT_DISK_GRAPH_UNPLANNED",
        ):
            self._validate_reverted_disk_fixture(fixture)

    def test_reverted_clone_rejects_invalid_new_delta_authority(self):
        cases = {
            "wrong-parent": {"parent_hint": "other.vmdk"},
            "wrong-parent-cid": {"parent_cid": "33333333"},
            "wrong-sector-layout": {"sectors": 101},
            "descriptor-tail-over-limit": {
                "tail": b" " * harness.MAX_VM_CONFIGURATION_BYTES
            },
        }
        for name, values in cases.items():
            with self.subTest(name=name):
                fixture = self._reverted_disk_fixture("invalid-" + name)
                self._write_revert_delta_descriptor(fixture, **values)
                with self.assertRaisesRegex(
                    harness.CandidateHarnessError,
                    "CANDIDATE_VM_REVERT_DISK_GRAPH_UNPLANNED",
                ):
                    self._validate_reverted_disk_fixture(fixture)

    def test_reverted_clone_rejects_swapped_split_extent_order(self):
        clone = self.root / "swapped-split-extent-order"
        clone.mkdir()
        vmx = clone / f"{harness.SOURCE_VM_IDENTITY}.vmx"
        snapshot_descriptor = harness.SNAPSHOT_DISK_FILES["FRESH_BASE"]
        snapshot_stem = snapshot_descriptor.removesuffix(".vmdk")
        snapshot_extents = [
            f"{snapshot_stem}-s001.vmdk",
            f"{snapshot_stem}-s002.vmdk",
        ]
        (clone / snapshot_descriptor).write_bytes(
            b"# Disk DescriptorFile\n"
            b"version=1\n"
            b"CID=11111111\n"
            b"parentCID=ffffffff\n"
            b'createType="twoGbMaxExtentSparse"\n'
            + f'RW 100 SPARSE "{snapshot_extents[0]}"\n'.encode("utf-8")
            + f'RW 200 SPARSE "{snapshot_extents[1]}"\n'.encode("utf-8")
        )
        for name in snapshot_extents:
            (clone / name).write_bytes(("snapshot:" + name).encode("utf-8"))
        vmx.write_text(
            f'scsi0:0.fileName = "{snapshot_descriptor}"\n', encoding="utf-8"
        )
        expected_hashes = {
            name: harness._hash_regular_file(clone / name)
            for name in (snapshot_descriptor, *snapshot_extents)
        }
        pre_revert_inventory = harness.ClosedVmwareProvider._vm_inventory(clone)

        active_descriptor = f"{harness.SOURCE_VM_IDENTITY}-000004.vmdk"
        active_stem = active_descriptor.removesuffix(".vmdk")
        active_extents = [
            f"{active_stem}-s001.vmdk",
            f"{active_stem}-s002.vmdk",
        ]
        (clone / active_descriptor).write_bytes(
            b"# Disk DescriptorFile\n"
            b"version=1\n"
            b"CID=22222222\n"
            b"parentCID=11111111\n"
            b'createType="twoGbMaxExtentSparse"\n'
            + f'parentFileNameHint="{snapshot_descriptor}"\n'.encode("utf-8")
            + f'RW 100 SPARSE "{active_extents[1]}"\n'.encode("utf-8")
            + f'RW 200 SPARSE "{active_extents[0]}"\n'.encode("utf-8")
        )
        for name in active_extents:
            (clone / name).write_bytes(("active:" + name).encode("utf-8"))
        vmx.write_text(
            f'scsi0:0.fileName = "{active_descriptor}"\n', encoding="utf-8"
        )

        with self.assertRaisesRegex(
            harness.CandidateHarnessError,
            "CANDIDATE_VM_REVERT_DISK_GRAPH_UNPLANNED",
        ):
            harness.ClosedVmwareProvider._validate_reverted_clone_disk_graph(
                clone,
                vmx,
                profile="FRESH_BASE",
                expected_original_hashes=expected_hashes,
                pre_revert_inventory=pre_revert_inventory,
            )

    def test_reverted_clone_rejects_duplicate_slot_or_extra_new_file(self):
        duplicate_slot = self._reverted_disk_fixture("duplicate-disk-slot")
        with duplicate_slot.vmx.open("a", encoding="utf-8") as handle:
            handle.write(
                f'scsi0:1.fileName = "{duplicate_slot.active_descriptor}"\n'
            )
        with self.assertRaisesRegex(
            harness.CandidateHarnessError,
            "CANDIDATE_VM_REVERT_DISK_GRAPH_UNPLANNED",
        ):
            self._validate_reverted_disk_fixture(duplicate_slot)

        extra_file = self._reverted_disk_fixture("extra-created-descriptor")
        (extra_file.clone / f"{harness.SOURCE_VM_IDENTITY}-000005.vmdk").write_bytes(
            b"# Disk DescriptorFile\n"
        )
        with self.assertRaisesRegex(
            harness.CandidateHarnessError,
            "CANDIDATE_VM_REVERT_DISK_GRAPH_UNPLANNED",
        ):
            self._validate_reverted_disk_fixture(extra_file)

    def test_reverted_clone_rejects_target_graph_identity_replacement(self):
        fixture = self._reverted_disk_fixture("target-identity-replacement")
        observed = harness.ClosedVmwareProvider._vm_inventory(fixture.clone)
        replaced = dict(observed)
        size, device, inode = replaced[fixture.snapshot_extent]
        replaced[fixture.snapshot_extent] = (size, device, inode + 1)
        with mock.patch.object(
            harness.ClosedVmwareProvider,
            "_vm_inventory",
            side_effect=[replaced, replaced],
        ), self.assertRaisesRegex(
            harness.CandidateHarnessError,
            "CANDIDATE_VM_REVERT_DISK_GRAPH_UNPLANNED",
        ):
            self._validate_reverted_disk_fixture(fixture)

    def test_source_host_hashes_detect_same_size_extent_drift(self):
        source = self.root / "source-extent-drift"
        extent = self._write_closed_source_disk_graph(source)
        provider = harness.ClosedVmwareProvider(
            runner=RecordingRunner(),
            windows_platform=FakeWindowsPlatform(),
            environment={},
        )
        with mock.patch.object(harness, "SOURCE_VM_ROOT", source):
            before = provider._hashes()
            extent.write_bytes(b"X" * extent.stat().st_size)
            after = provider._hashes()

        extent_name = extent.relative_to(source).as_posix()
        self.assertIn(extent_name, before)
        self.assertNotEqual(before[extent_name], after[extent_name])

    def test_clone_authority_rejects_same_size_extent_tamper(self):
        clone = self.root / "clone-extent-tamper"
        extent = self._write_closed_source_disk_graph(clone)
        provider = harness.ClosedVmwareProvider(
            runner=RecordingRunner(),
            windows_platform=FakeWindowsPlatform(),
            environment={},
        )
        with mock.patch.object(harness, "SOURCE_VM_ROOT", clone):
            expected_hashes = provider._hashes()
        expected_graph = provider._closed_source_disk_graph_content_digest(clone)
        self.assertEqual(
            provider._verify_clone_authoritative_hashes(
                clone,
                profile="FRESH_BASE",
                expected_original_hashes=expected_hashes,
                expected_snapshot_identity=expected_hashes[
                    harness.SNAPSHOT_FILES["FRESH_BASE"]
                ],
                expected_source_disk_graph_identity=expected_graph,
            ),
            expected_hashes,
        )
        extent.write_bytes(b"Y" * extent.stat().st_size)

        with self.assertRaisesRegex(
            harness.CandidateHarnessError,
            "CANDIDATE_VM_FULL_COPY_IDENTITY_MISMATCH",
        ):
            provider._verify_clone_authoritative_hashes(
                clone,
                profile="FRESH_BASE",
                expected_original_hashes=expected_hashes,
                expected_snapshot_identity=expected_hashes[
                    harness.SNAPSHOT_FILES["FRESH_BASE"]
                ],
                expected_source_disk_graph_identity=expected_graph,
            )

    def test_preboot_snapshot_graph_rejects_same_size_extent_tamper(self):
        clone = self.root / "preboot-snapshot-extent-tamper"
        self._write_closed_source_disk_graph(clone)
        descriptor = harness.SNAPSHOT_DISK_FILES["FRESH_BASE"]
        expected = harness.ClosedVmwareProvider._descriptor_disk_graph_content_digest(
            clone,
            descriptor,
        )
        extent = clone / (descriptor.removesuffix(".vmdk") + "-s001.vmdk")
        extent.write_bytes(b"Z" * extent.stat().st_size)

        with self.assertRaisesRegex(
            harness.CandidateHarnessError,
            "CANDIDATE_VM_SNAPSHOT_DISK_GRAPH_MISMATCH",
        ):
            harness.ClosedVmwareProvider._clone_snapshot_disk_graph_identity(
                clone,
                profile="FRESH_BASE",
                expected_snapshot_disk_graph_identity=expected,
            )

    def test_clone_authoritative_hashes_reject_same_size_snapshot_tamper(self):
        clone = self.root / "same-size-snapshot-tamper"
        clone.mkdir()
        expected_hashes = {}
        for name in harness.SOURCE_VM_HASH_FILES:
            payload = ("source:" + name).encode("utf-8")
            (clone / name).write_bytes(payload)
            expected_hashes[name] = "sha256:" + hashlib.sha256(payload).hexdigest()
        snapshot_name = harness.SNAPSHOT_FILES["FRESH_BASE"]
        snapshot = clone / snapshot_name
        snapshot.write_bytes(b"X" * snapshot.stat().st_size)

        with self.assertRaisesRegex(
            harness.CandidateHarnessError,
            "CANDIDATE_VM_FULL_COPY_IDENTITY_MISMATCH",
        ):
            harness.ClosedVmwareProvider._verify_clone_authoritative_hashes(
                clone,
                profile="FRESH_BASE",
                expected_original_hashes=expected_hashes,
                expected_snapshot_identity=expected_hashes[snapshot_name],
                expected_source_disk_graph_identity=DIGEST,
            )

    def test_clone_full_rejects_same_size_snapshot_tamper_before_boot(self):
        harness_plan = self._plan()
        original_profile = harness_plan.profiles[0]
        source = self.root / "source-vm"
        source.mkdir()
        expected_hashes = {}
        for name in harness.SOURCE_VM_HASH_FILES:
            payload = ("source:" + name).encode("utf-8")
            (source / name).write_bytes(payload)
            expected_hashes[name] = "sha256:" + hashlib.sha256(payload).hexdigest()
        snapshot_name = harness.SNAPSHOT_FILES[original_profile.profile]
        profile = replace(
            original_profile,
            snapshot_identity=expected_hashes[snapshot_name],
        )
        authority = self._temporary_authority(
            harness.ClosedVmwareProvider._profile_authority(
                original_profile, harness_plan
            )
        )
        provider = harness.ClosedVmwareProvider(
            runner=RecordingRunner(),
            windows_platform=FakeWindowsPlatform(),
            environment={},
        )

        def copy_then_tamper(_argv, **_kwargs):
            shutil.copytree(source, authority.clone_root)
            snapshot = authority.clone_root / snapshot_name
            snapshot.write_bytes(b"X" * snapshot.stat().st_size)
            return SimpleNamespace(returncode=1, stdout=b"", stderr=b"")

        with mock.patch.object(harness, "SOURCE_VM_ROOT", source), mock.patch.object(
            provider, "_assert_source_stopped"
        ), mock.patch.object(
            provider, "_hashes", return_value=expected_hashes
        ), mock.patch.object(provider, "_run", side_effect=copy_then_tamper), mock.patch.object(
            provider, "_validate_clone_disk_graph"
        ), self.assertRaisesRegex(
            harness.CandidateHarnessError,
            "CANDIDATE_VM_FULL_COPY_IDENTITY_MISMATCH",
        ):
            provider._clone_full(
                profile,
                authority,
                expected_original_hashes=expected_hashes,
                expected_source_disk_graph_identity=DIGEST,
            )

    def test_revert_power_failures_are_contained_before_quarantine(self):
        scenarios = {
            "pre-revert-running": {
                "running": lambda identity: [frozenset({identity})],
                "revert_error": None,
                "expected_code": "CANDIDATE_VM_CLONE_POWER_STATE_INVALID",
                "contain": True,
                "revert_called": False,
            },
            "pre-revert-query-failure": {
                "running": lambda _identity: [
                    harness.CandidateHarnessError(
                        "CANDIDATE_VM_INVENTORY_UNAVAILABLE"
                    )
                ],
                "revert_error": None,
                "expected_code": "CANDIDATE_VM_CLONE_POWER_STATE_INVALID",
                "contain": True,
                "revert_called": False,
            },
            "post-revert-running": {
                "running": lambda identity: [
                    frozenset(),
                    frozenset({identity}),
                ],
                "revert_error": None,
                "expected_code": "CANDIDATE_VM_CLONE_POWER_STATE_INVALID",
                "contain": True,
            },
            "post-revert-query-failure": {
                "running": lambda _identity: [
                    frozenset(),
                    harness.CandidateHarnessError(
                        "CANDIDATE_VM_INVENTORY_UNAVAILABLE"
                    ),
                ],
                "revert_error": None,
                "expected_code": "CANDIDATE_VM_CLONE_POWER_STATE_INVALID",
                "contain": True,
            },
            "revert-failure-stopped": {
                "running": lambda _identity: [frozenset(), frozenset()],
                "revert_error": harness.CandidateHarnessError(
                    "CANDIDATE_VM_CLONE_REVERT_FAILED"
                ),
                "expected_code": "CANDIDATE_VM_CLONE_REVERT_FAILED",
                "contain": False,
            },
            "revert-failure-query-unknown": {
                "running": lambda _identity: [
                    frozenset(),
                    harness.CandidateHarnessError(
                        "CANDIDATE_VM_INVENTORY_UNAVAILABLE"
                    ),
                ],
                "revert_error": harness.CandidateHarnessError(
                    "CANDIDATE_VM_CLONE_REVERT_FAILED"
                ),
                "expected_code": "CANDIDATE_VM_CLONE_REVERT_FAILED",
                "contain": True,
            },
            "validator-failure-clone-running": {
                "running": lambda identity: [
                    frozenset(),
                    frozenset(),
                    frozenset({identity}),
                ],
                "revert_error": None,
                "validator_error": harness.CandidateHarnessError(
                    "CANDIDATE_VM_REVERT_DISK_GRAPH_UNPLANNED"
                ),
                "expected_code": "CANDIDATE_VM_REVERT_DISK_GRAPH_UNPLANNED",
                "contain": True,
                "validate_called": True,
            },
            "post-readback-query-failure": {
                "running": lambda _identity: [
                    frozenset(),
                    frozenset(),
                    harness.CandidateHarnessError(
                        "CANDIDATE_VM_INVENTORY_UNAVAILABLE"
                    ),
                ],
                "revert_error": None,
                "expected_code": "CANDIDATE_VM_CLONE_POWER_STATE_INVALID",
                "contain": True,
                "validate_called": True,
            },
            "post-write-running": {
                "running": lambda identity: [
                    frozenset(),
                    frozenset(),
                    frozenset(),
                    frozenset({identity}),
                ],
                "revert_error": None,
                "expected_code": "CANDIDATE_VM_CLONE_POWER_STATE_INVALID",
                "contain": True,
                "validate_called": True,
                "inject_called": True,
            },
        }
        for name, scenario in scenarios.items():
            with self.subTest(name=name):
                plan = self._plan()
                profile = plan.profiles[0]
                provider = harness.ClosedVmwareProvider(
                    runner=RecordingRunner(),
                    windows_platform=FakeWindowsPlatform(),
                    environment={},
                )
                authority = self._temporary_authority(
                    harness.ClosedVmwareProvider._profile_authority(profile, plan)
                )
                authority.profile_root.mkdir(parents=True)
                clone_identity = os.path.normcase(
                    str(authority.clone_vmx.resolve(strict=False))
                )
                events = []

                def revert(*_args):
                    events.append("revert")
                    if scenario["revert_error"] is not None:
                        raise scenario["revert_error"]

                with ExitStack() as stack:
                    stack.enter_context(mock.patch.object(provider, "_assert_tools"))
                    stack.enter_context(
                        mock.patch.object(
                            provider,
                            "_acquire_provider_lease",
                            return_value=mock.sentinel.lease,
                        )
                    )
                    stack.enter_context(
                        mock.patch.object(provider, "_release_provider_lease")
                    )
                    stack.enter_context(
                        mock.patch.object(
                            provider,
                            "_hashes",
                            return_value=dict(plan.original_vm_hashes),
                        )
                    )
                    stack.enter_context(
                        mock.patch.object(
                            provider, "_profile_authority", return_value=authority
                        )
                    )
                    stack.enter_context(
                        mock.patch.object(provider, "_prepare_profile_authority")
                    )
                    stack.enter_context(
                        mock.patch.object(
                            provider,
                            "_clone_full",
                            return_value=(authority.clone_root, authority.clone_vmx),
                        )
                    )
                    stack.enter_context(
                        mock.patch.object(
                            provider,
                            "_vm_inventory",
                            return_value={"fixed": (1, 2, 3)},
                        )
                    )
                    stack.enter_context(
                        mock.patch.object(
                            provider,
                            "_running_vmx_paths",
                            side_effect=scenario["running"](clone_identity),
                        )
                    )
                    stack.enter_context(
                        mock.patch.object(provider, "_revert_clone", side_effect=revert)
                    )
                    validate = stack.enter_context(
                        mock.patch.object(
                            provider,
                            "_validate_reverted_clone_disk_graph",
                            side_effect=scenario.get("validator_error"),
                        )
                    )
                    stack.enter_context(
                        mock.patch.object(
                            provider,
                            "_clone_snapshot_disk_graph_identity",
                            return_value=profile.snapshot_disk_graph_identity,
                        )
                    )
                    stack.enter_context(
                        mock.patch.object(
                            provider,
                            "_clone_snapshot_identity",
                            return_value=profile.snapshot_identity,
                        )
                    )
                    stack.enter_context(
                        mock.patch.object(
                            provider,
                            "_disk_graph_content_digest",
                            return_value="sha256:" + "a" * 64,
                        )
                    )
                    inject = stack.enter_context(
                        mock.patch.object(provider, "_inject_guestinfo_challenge")
                    )
                    start = stack.enter_context(
                        mock.patch.object(provider, "_start_clone")
                    )
                    contain = stack.enter_context(
                        mock.patch.object(
                            provider,
                            "_contain_clone",
                            side_effect=lambda _vmx: events.append("contain"),
                        )
                    )
                    stack.enter_context(
                        mock.patch.object(
                            provider,
                            "_quarantine_clone",
                            side_effect=lambda _authority: events.append("quarantine"),
                        )
                    )
                    stack.enter_context(
                        self.assertRaisesRegex(
                            harness.CandidateHarnessError,
                            scenario["expected_code"],
                        )
                    )
                    provider.execute_profile(
                        plan=profile,
                        harness_plan=plan,
                        candidate_root=self.root,
                        initial_platform_state=harness._initial_platform_state(
                            profile.profile
                        ),
                    )

                expected_events = []
                if scenario.get("revert_called", True):
                    expected_events.append("revert")
                if scenario["contain"]:
                    expected_events.append("contain")
                    contain.assert_called_once_with(authority.clone_vmx)
                else:
                    contain.assert_not_called()
                expected_events.append("quarantine")
                self.assertEqual(events, expected_events)
                if scenario.get("validate_called", False):
                    validate.assert_called_once()
                else:
                    validate.assert_not_called()
                if scenario.get("inject_called", False):
                    inject.assert_called_once()
                else:
                    inject.assert_not_called()
                start.assert_not_called()
                shutil.rmtree(authority.session_root, ignore_errors=True)

    def test_partial_start_failure_is_contained_before_quarantine(self):
        plan = self._plan()
        profile = plan.profiles[0]
        runner = RecordingRunner()
        provider = harness.ClosedVmwareProvider(
            runner=runner,
            windows_platform=FakeWindowsPlatform(),
            environment={},
        )
        authority = self._temporary_authority(
            harness.ClosedVmwareProvider._profile_authority(profile, plan)
        )
        authority.profile_root.mkdir(parents=True)
        clone_root = authority.clone_root
        clone_vmx = authority.clone_vmx
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(provider, "_assert_tools"))
            stack.enter_context(
                mock.patch.object(
                    provider,
                    "_acquire_provider_lease",
                    return_value=mock.sentinel.lease,
                )
            )
            stack.enter_context(
                mock.patch.object(provider, "_release_provider_lease")
            )
            stack.enter_context(
                mock.patch.object(
                    provider, "_hashes", return_value=dict(plan.original_vm_hashes)
                )
            )
            stack.enter_context(
                mock.patch.object(provider, "_profile_authority", return_value=authority)
            )
            stack.enter_context(
                mock.patch.object(provider, "_prepare_profile_authority")
            )
            stack.enter_context(
                mock.patch.object(
                    provider, "_clone_full", return_value=(clone_root, clone_vmx)
                )
            )
            stack.enter_context(
                mock.patch.object(
                    provider, "_vm_inventory", return_value={"fixed": (1, 2, 3)}
                )
            )
            stack.enter_context(
                mock.patch.object(
                    provider, "_running_vmx_paths", return_value=frozenset()
                )
            )
            stack.enter_context(mock.patch.object(provider, "_revert_clone"))
            stack.enter_context(
                mock.patch.object(provider, "_validate_reverted_clone_disk_graph")
            )
            stack.enter_context(
                mock.patch.object(
                    provider,
                    "_clone_snapshot_disk_graph_identity",
                    return_value=profile.snapshot_disk_graph_identity,
                )
            )
            stack.enter_context(
                mock.patch.object(
                    provider,
                    "_clone_snapshot_identity",
                    return_value=profile.snapshot_identity,
                )
            )
            stack.enter_context(
                mock.patch.object(
                    provider,
                    "_disk_graph_content_digest",
                    return_value="sha256:" + "a" * 64,
                )
            )
            stack.enter_context(
                mock.patch.object(provider, "_inject_guestinfo_challenge")
            )
            stack.enter_context(
                mock.patch.object(
                    provider,
                    "_start_clone",
                    side_effect=harness.CandidateHarnessError(
                        "CANDIDATE_VM_CLONE_START_FAILED"
                    ),
                )
            )
            stack.enter_context(
                mock.patch.object(
                    provider,
                    "_stop_clone",
                    side_effect=harness.CandidateHarnessError(
                        "CANDIDATE_VM_CLONE_SOFT_SHUTDOWN_FAILED"
                    ),
                )
            )
            stack.enter_context(
                mock.patch.object(
                    provider,
                    "_is_running",
                    side_effect=[True] + [True] * 60 + [False],
                )
            )
            stack.enter_context(mock.patch("scripts.candidate_vm_harness.time.sleep"))
            quarantine = stack.enter_context(
                mock.patch.object(provider, "_quarantine_clone")
            )
            stack.enter_context(
                self.assertRaisesRegex(
                    harness.CandidateHarnessError, "CLONE_START_FAILED"
                )
            )
            provider.execute_profile(
                plan=profile,
                harness_plan=plan,
                candidate_root=self.root,
                initial_platform_state=harness._initial_platform_state(profile.profile),
            )
        suspend_modes = [
            call["argv"][-1]
            for call in runner.calls
            if "suspend" in call["argv"]
        ]
        self.assertEqual(suspend_modes, ["soft", "hard"])
        quarantine.assert_called_once_with(authority)

    def test_soft_shutdown_failure_uses_soft_suspend(self):
        runner = RecordingRunner()
        provider = harness.ClosedVmwareProvider(runner=runner, environment={})
        clone_vmx = self.root / "clone" / "Ubuntu 64 位.vmx"
        with mock.patch.object(
            provider,
            "_stop_clone",
            side_effect=harness.CandidateHarnessError(
                "CANDIDATE_VM_CLONE_SOFT_SHUTDOWN_FAILED"
            ),
        ), mock.patch.object(provider, "_is_running", return_value=False):
            provider._contain_clone(clone_vmx)
        self.assertEqual(runner.calls[0]["argv"][3], "suspend")
        self.assertEqual(runner.calls[0]["argv"][-1], "soft")


if __name__ == "__main__":
    unittest.main()
