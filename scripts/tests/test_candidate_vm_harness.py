from __future__ import annotations

import ast
import ctypes
import shutil
import tempfile
import unittest
from ctypes import wintypes
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from release.candidate import canonical_json_bytes, sha256_bytes
from release.r2_prestate import (
    ACCESS_KEY_ENV,
    ACCOUNT_ID_ENV,
    JURISDICTION_ENV,
    R2_AUTH_METHOD_ARGUMENT,
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
        self.hashes = {
            name: "sha256:" + "3" * 64 for name in harness.SOURCE_VM_HASH_FILES
        }
        self.snapshots = {
            profile: "sha256:" + character * 64
            for profile, character in zip(harness.PROFILES, "456", strict=True)
        }

    def inspect_source(self):
        self.events.append("source")
        return harness.SourceVmEvidence(
            vm_identity=harness.SOURCE_VM_IDENTITY,
            snapshot_identities=self.snapshots,
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
        return {
            "schema": "animemo.prepublication-candidate-profile-receipt/v1",
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
            "snapshot_identity": plan.snapshot_identity,
            "clone_identity": plan.clone_identity,
            "initial_platform_state": dict(initial_platform_state),
            "platform_bootstrap_plan_digest": "sha256:" + "7" * 64,
            "platform_bootstrap_receipt_digest": "sha256:" + "8" * 64,
            "strict_platform_qualification": True,
            "instance_mutation_before_platform_qualification": 0,
            "installer_plan_digest": "sha256:" + "9" * 64,
            "installer_execution_result": "PASS",
            "api_digest": DIGEST,
            "web_digest": DIGEST,
            "postgres_digest": DIGEST,
            "redis_digest": DIGEST,
            "doctor_result": "PASS",
            "canonical_test_results": [{"name": "acceptance", "result": "PASS"}],
            "network_request_count": 0,
            "apt_command_count": 0,
            "external_pull_count": 0,
            "original_vm_pre_hashes": dict(self.hashes),
            "original_vm_post_hashes": dict(self.hashes),
            "release_authority_granted": False,
            "publish_authorized": False,
            "started_at": "2026-08-25T12:00:00Z",
            "completed_at": "2026-08-25T12:01:00Z",
            "result": "PASS",
        }

    def inspect_original_hashes(self):
        return dict(self.hashes)

    def inspect_rc14_external_state(self):
        self.external_calls += 1
        return dict(self.external_state)


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

    def run(self, argv, *, environment, input_bytes=None, timeout=300):
        self.calls.append(
            {
                "argv": tuple(argv),
                "environment": dict(environment),
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
        machine: int = 0x8664,
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
        self.machine = machine
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
        digest = self.ssh_digest if path == harness.SSH else self.scp_digest
        return harness.WindowsBinaryIdentity(
            sha256=digest,
            pe_machine=self.machine,
        )

    def is_controlled_file(self, path, *, root, private):
        self.last_controlled_file_check = (path, root, private)
        if path == harness.OPENSSH_IDENTITY:
            return self.identity_safe
        if path == harness.OPENSSH_KNOWN_HOSTS:
            return self.known_hosts_safe
        return False

    def inspect_controlled_file(self, path, *, root, private):
        self.last_controlled_file_check = (path, root, private)
        if path == harness.OPENSSH_IDENTITY:
            status = self.identity_status
        elif path == harness.OPENSSH_KNOWN_HOSTS:
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


def _acl_loader(
    *,
    owner=0x1234567887654321,
    current_user=0x1234567887654321,
    descriptor=0x3456789AA9876543,
    dacl=0x456789ABBA987654,
    named_result=0,
    open_token=True,
    token_probe_success=False,
    token_probe_error=122,
    token_query=True,
    equal_sid_result=True,
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
        if named_result:
            return named_result
        set_void_pointer(args[3], owner)
        set_void_pointer(args[5], dacl)
        set_void_pointer(args[7], descriptor)
        return 0

    def open_process_token(_process, _access, token_pointer):
        if not open_token:
            loader.last_error = 5
            return 0
        ctypes.cast(token_pointer, ctypes.POINTER(wintypes.HANDLE)).contents.value = (
            0x56789ABCCBA98765
        )
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


def _r2_environment():
    return {
        ACCOUNT_ID_ENV: ACCOUNT_ID,
        JURISDICTION_ENV: "default",
        ACCESS_KEY_ENV: "test-only-access-key",
        SECRET_KEY_ENV: "test-only-secret-key",
    }


class CandidateVmHarnessTests(unittest.TestCase):
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

    def _r2_receipt(self):
        with mock.patch(
            "release.r2_prestate.R2_ACCOUNT_ID_SHA256",
            sha256_bytes(ACCOUNT_ID.encode("ascii")),
        ):
            return verify_rc14_r2_origin_from_environment(
                source_sha=SHA,
                source_tree=TREE,
                auth_method=R2_AUTH_METHOD_ARGUMENT,
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
        self.assertRegex(plan.plan_digest, r"^sha256:[0-9a-f]{64}$")

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
                "scripts.candidate_vm_harness.verify_rc14_r2_origin_from_environment",
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
        receipt = self._r2_receipt()
        with mock.patch(
            "scripts.candidate_vm_harness.verify_rc14_r2_origin_from_environment",
            return_value=receipt,
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
        self.assertEqual(verify.call_count, 1)
        self.assertEqual(
            verify.call_args.kwargs["auth_method"], R2_AUTH_METHOD_ARGUMENT
        )

    def test_three_receipts_close_one_aggregate_without_publication_authority(self):
        plan = self._plan()
        environment = _r2_environment()
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
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(len(result["profileReceipts"]), 3)
        aggregate = result["aggregateReceipt"]
        self.assertTrue(aggregate["all_profiles_pass"])
        self.assertFalse(aggregate["release_authority_granted"])
        self.assertFalse(aggregate["publish_authorized"])
        self.assertEqual(self.provider.external_calls, 2)
        self.assertEqual(
            aggregate["rc14_prestate"],
            {**harness.EXPECTED_RC14_EXTERNAL_STATE, "r2_origin": "PROVEN_EMPTY"},
        )
        self.assertEqual(
            aggregate["rc14_poststate"],
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
        unsigned = dict(aggregate)
        receipt_digest = unsigned.pop("receipt_digest")
        self.assertEqual(receipt_digest, sha256_bytes(canonical_json_bytes(unsigned)))

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

    def test_original_vm_hash_drift_stops_before_the_next_profile(self):
        plan = self._plan()
        environment = _r2_environment()
        original_execute = self.provider.execute_profile

        def mutate_source(**kwargs):
            receipt = original_execute(**kwargs)
            self.provider.hashes[harness.SOURCE_VM_HASH_FILES[0]] = (
                "sha256:" + "f" * 64
            )
            receipt["original_vm_post_hashes"] = dict(self.provider.hashes)
            return receipt

        self.provider.execute_profile = mutate_source
        with mock.patch(
            "scripts.candidate_vm_harness.load_verified_candidate",
            return_value=self.loaded,
        ), mock.patch(
            "release.r2_prestate.R2_ACCOUNT_ID_SHA256",
            sha256_bytes(ACCOUNT_ID.encode("ascii")),
        ), self.assertRaisesRegex(
            harness.CandidateHarnessError, "PROFILE_SAFETY_MISMATCH"
        ):
            harness.execute_harness_plan(
                plan,
                accepted_plan_digest=plan.plan_digest,
                provider=self.provider,
                environment=environment,
                r2_client=R2Client(),
            )
        self.assertEqual(self.provider.execute_calls, 1)

    def test_rc14_presence_fails_before_the_first_profile(self):
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
            harness.CandidateHarnessError, "RC14_NOT_EMPTY"
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
            harness.CandidateHarnessError, "CANDIDATE_RC14_NOT_EMPTY"
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
            provider.inspect_rc14_external_state(),
            harness.EXPECTED_RC14_EXTERNAL_STATE,
        )
        urls = [url for url, _ in transport.calls]
        self.assertEqual(sum(url.startswith("https://ghcr.io/token?") for url in urls), 2)
        self.assertEqual(
            sum(
                url.startswith(harness.PUBLIC_MIRROR_ORIGIN + "/")
                for url in urls
            ),
            len(harness.R2_RC14_EXPECTED_KEYS),
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

    def test_closed_provider_has_a_complete_success_lifecycle(self):
        plan = self._plan()
        profile = plan.profiles[0]
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
        clone_root = self.root / "closed-clone"
        clone_vmx = clone_root / "Ubuntu 64 位.vmx"
        with mock.patch.object(provider, "_assert_tools"), mock.patch.object(
            provider, "_hashes", return_value=dict(plan.original_vm_hashes)
        ), mock.patch.object(
            provider, "_clone_full", return_value=(clone_root, clone_vmx)
        ), mock.patch.object(provider, "_revert_clone") as revert, mock.patch.object(
            provider, "_validate_clone_disk_graph"
        ), mock.patch.object(
            provider, "_start_clone"
        ) as start, mock.patch.object(provider, "_wait_for_ssh"), mock.patch.object(
            provider, "_stage_candidate", return_value="/fixed/candidate"
        ), mock.patch.object(
            provider, "_run_profile_guest", return_value=receipt
        ), mock.patch.object(provider, "_stop_clone") as stop, mock.patch.object(
            provider, "_remove_clone"
        ) as remove, mock.patch.object(provider, "_quarantine_clone") as quarantine:
            observed = provider.execute_profile(
                plan=profile,
                harness_plan=plan,
                candidate_root=self.root,
                initial_platform_state=harness._initial_platform_state(profile.profile),
            )
        self.assertEqual(observed, receipt)
        revert.assert_called_once_with(clone_vmx, profile.snapshot_name)
        start.assert_called_once_with(clone_vmx)
        stop.assert_called_once_with(clone_vmx)
        remove.assert_called_once_with(clone_root)
        quarantine.assert_not_called()

    def test_host_commands_receive_only_sanitized_environment_and_stdin_secret(self):
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
        provider = harness.ClosedVmwareProvider(
            windows_platform=FakeWindowsPlatform(),
            environment={
                "HOME": "C:/Users/tester",
                "USERPROFILE": "C:/Users/tester",
                "SSH_AUTH_SOCK": "test-agent-endpoint",
            },
        )
        ssh_argv = provider._ssh_argv("/usr/bin/true")
        scp_argv = provider._scp_argv(
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
            f"UserKnownHostsFile={harness.OPENSSH_KNOWN_HOSTS}",
            f"IdentityFile={harness.OPENSSH_IDENTITY}",
            f"HostKeyAlias={harness.SSH_HOST_KEY_ALIAS}",
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
        provider._ssh_checked("/usr/bin/true", code="TEST")
        provider._run(
            provider._scp_argv(
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
        advapi32 = loader.libraries["advapi32"].functions
        kernel32 = loader.libraries["kernel32"].functions
        shell32 = loader.libraries["shell32"].functions
        ole32 = loader.libraries["ole32"].functions

        pointer_to_void_pointer = ctypes.POINTER(ctypes.c_void_p)
        self.assertEqual(
            advapi32["GetNamedSecurityInfoW"].argtypes[3:],
            (
                pointer_to_void_pointer,
                pointer_to_void_pointer,
                pointer_to_void_pointer,
                pointer_to_void_pointer,
                pointer_to_void_pointer,
            ),
        )
        self.assertEqual(
            advapi32["GetTokenInformation"].argtypes,
            (
                wintypes.HANDLE,
                ctypes.c_int,
                ctypes.c_void_p,
                wintypes.DWORD,
                ctypes.POINTER(wintypes.DWORD),
            ),
        )
        self.assertEqual(
            shell32["SHGetKnownFolderPath"].argtypes[-1],
            ctypes.POINTER(ctypes.c_wchar_p),
        )
        self.assertEqual(kernel32["LocalFree"].restype, ctypes.c_void_p)
        self.assertEqual(ole32["CoTaskMemFree"].restype, None)
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
        loader = RecordingWin32Loader()
        loader.libraries.setdefault("shell32", RecordingWin32Library()).functions[
            "SHGetKnownFolderPath"
        ] = RecordingWin32Function(lambda *_args: -2147467259)
        adapter = harness._WindowsApiAdapter(dll_loader=loader)

        with self.assertRaisesRegex(OSError, "FOLDERID_ProgramData is unavailable"):
            adapter.resolve_program_data()
        self.assertEqual(
            len(loader.libraries["ole32"].functions["CoTaskMemFree"].calls), 0
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
        adapter = harness._WindowsApiAdapter(
            dll_loader=loader, last_error_reader=loader.get_last_error
        )

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
        self.assertFalse(legacy_default_c_int == pointer)

        loader, state = _acl_loader(owner=pointer, current_user=pointer)
        inspection = harness._WindowsApiAdapter(
            dll_loader=loader, last_error_reader=loader.get_last_error
        ).inspect_file_acl(Path("controlled-ssh.exe"))

        self.assertEqual(state["owner_seen"], pointer)
        self.assertEqual(state["current_user_seen"], pointer)
        self.assertEqual(inspection.status, "PASS")

    def test_win32_acl_adapter_rejects_owner_descriptor_and_dacl_anomalies(self):
        cases = {
            "owner-mismatch": (
                {"equal_sid_result": False},
                "OWNER_MISMATCH",
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
                inspection = harness._WindowsApiAdapter(
                    dll_loader=loader, last_error_reader=loader.get_last_error
                ).inspect_file_acl(Path("controlled-ssh.exe"))
                self.assertEqual(inspection.status, expected)

    def test_win32_acl_adapter_rejects_each_broad_principal(self):
        for index, principal in enumerate(
            ("Everyone", "Authenticated Users", "Builtin Users")
        ):
            loader, state = _acl_loader(broad_access_index=index)
            with self.subTest(principal=principal):
                inspection = harness._WindowsApiAdapter(
                    dll_loader=loader, last_error_reader=loader.get_last_error
                ).inspect_file_acl(Path("controlled-ssh.exe"))
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
                inspection = harness._WindowsApiAdapter(
                    dll_loader=loader, last_error_reader=loader.get_last_error
                ).inspect_file_acl(Path("controlled-ssh.exe"))
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
                inspection = harness._WindowsApiAdapter(
                    dll_loader=loader, last_error_reader=loader.get_last_error
                ).inspect_file_acl(Path("controlled-ssh.exe"))
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
            str(harness.OPENSSH_KNOWN_HOSTS),
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
            "host-key-capability": (
                FakeWindowsPlatform(known_hosts_safe=False),
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

    def test_partial_start_failure_is_contained_before_quarantine(self):
        plan = self._plan()
        profile = plan.profiles[0]
        runner = RecordingRunner()
        provider = harness.ClosedVmwareProvider(
            runner=runner,
            windows_platform=FakeWindowsPlatform(),
            environment={},
        )
        clone_root = self.root / "partial-start"
        clone_vmx = clone_root / "Ubuntu 64 位.vmx"
        with mock.patch.object(provider, "_assert_tools"), mock.patch.object(
            provider, "_hashes", return_value=dict(plan.original_vm_hashes)
        ), mock.patch.object(
            provider, "_clone_full", return_value=(clone_root, clone_vmx)
        ), mock.patch.object(provider, "_revert_clone"), mock.patch.object(
            provider, "_validate_clone_disk_graph"
        ), mock.patch.object(
            provider,
            "_start_clone",
            side_effect=harness.CandidateHarnessError("CANDIDATE_VM_CLONE_START_FAILED"),
        ), mock.patch.object(
            provider,
            "_stop_clone",
            side_effect=harness.CandidateHarnessError(
                "CANDIDATE_VM_CLONE_SOFT_SHUTDOWN_FAILED"
            ),
        ), mock.patch.object(
            provider, "_is_running", side_effect=[True] + [True] * 60 + [False]
        ), mock.patch(
            "scripts.candidate_vm_harness.time.sleep"
        ), mock.patch.object(
            provider, "_quarantine_clone"
        ) as quarantine, self.assertRaisesRegex(
            harness.CandidateHarnessError, "CLONE_START_FAILED"
        ):
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
        quarantine.assert_called_once_with(clone_root, profile.clone_identity)

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
