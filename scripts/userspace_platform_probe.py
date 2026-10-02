"""Fixed non-root Linux DEV checks under an inherited no-network seccomp filter."""

from __future__ import annotations

import base64
import errno
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import stat
import sys
from datetime import datetime, timezone

from scripts.development_userspace_inputs import (
    COMMIT,
    EXTRA_TEST_IDS,
    ORIGINAL,
    PURPOSE,
    TAG_OBJECT,
    TEST_IDS,
)

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


def require(value, code="DEVELOPMENT_USERSPACE_GUEST_INVALID"):
    if not value:
        raise ValueError(code)


def seccomp_instructions():
    # Linux x86_64 only. Deny every networking syscall, including socketpair;
    # file/pipe/pass_fds tests do not need a loopback network namespace.
    # Deny io_uring network operations and pidfd socket-descriptor import too.
    denied = (
        41,
        42,
        43,
        44,
        45,
        46,
        47,
        48,
        49,
        50,
        51,
        52,
        53,
        54,
        55,
        288,
        299,
        307,
        425,
        426,
        427,
        438,
    )
    instructions = [
        (0x20, 0, 0, 4),
        (0x15, 1, 0, 0xC000003E),
        (0x06, 0, 0, 0x80000000),
        (0x20, 0, 0, 0),
        (0x45, 0, 1, 0x40000000),
        (0x06, 0, 0, 0x00050000 | errno.EPERM),
    ]
    for number in denied:
        instructions.extend(
            ((0x15, 0, 1, number), (0x06, 0, 0, 0x00050000 | errno.EPERM))
        )
    return instructions + [(0x06, 0, 0, 0x7FFF0000)]


def enforce_no_network():
    import ctypes
    import socket

    require(
        sys.platform == "linux" and platform.machine() == "x86_64" and os.geteuid() != 0
    )
    require(
        len(os.listdir("/proc/self/task")) == 1,
        "DEVELOPMENT_USERSPACE_EXISTING_THREADS",
    )
    for name in os.listdir("/proc/self/fd"):
        try:
            metadata = os.fstat(int(name))
        except OSError:
            continue  # The directory iterator's own descriptor may already be closed.
        require(
            not stat.S_ISSOCK(metadata.st_mode),
            "DEVELOPMENT_USERSPACE_INHERITED_SOCKET",
        )

    class Filter(ctypes.Structure):
        _fields_ = [
            ("code", ctypes.c_ushort),
            ("jt", ctypes.c_ubyte),
            ("jf", ctypes.c_ubyte),
            ("k", ctypes.c_uint32),
        ]

    class Program(ctypes.Structure):
        _fields_ = [("length", ctypes.c_ushort), ("filters", ctypes.POINTER(Filter))]

    code = seccomp_instructions()
    filters = (Filter * len(code))(*(Filter(*item) for item in code))
    program = Program(len(code), filters)
    libc = ctypes.CDLL(None, use_errno=True)
    libc.prctl.argtypes = [
        ctypes.c_int,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_ulong,
    ]
    libc.prctl.restype = ctypes.c_int
    require(
        libc.prctl(38, 1, 0, 0, 0) == 0,
        "DEVELOPMENT_USERSPACE_NO_NEW_PRIVS_UNAVAILABLE",
    )
    require(
        libc.prctl(22, 2, ctypes.addressof(program), 0, 0) == 0,
        "DEVELOPMENT_USERSPACE_SECCOMP_UNAVAILABLE",
    )
    try:
        created = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    except OSError as error:
        require(
            error.errno == errno.EPERM, "DEVELOPMENT_USERSPACE_SECCOMP_PROBE_FAILED"
        )
    else:
        created.close()
        require(False, "DEVELOPMENT_USERSPACE_SECCOMP_PROBE_FAILED")
    return {
        "mechanism": "NO_NEW_PRIVS_SECCOMP_FILTER",
        "architecture": "x86_64",
        "inherited_socket_fds": 0,
        "socket_errno": errno.EPERM,
        "scope": "THIS_PAYLOAD_AND_DESCENDANTS",
    }


def _network_isolation_child_program():
    return """import errno
import json
import socket
from pathlib import Path

status = dict(line.split(":", 1) for line in Path("/proc/self/status").read_text().splitlines())
assert status["NoNewPrivs"].strip() == "1"
assert status["Seccomp"].strip() == "2"
try:
    socket.socket(socket.AF_INET, socket.SOCK_STREAM)
except OSError as error:
    assert error.errno == errno.EPERM
else:
    raise AssertionError()
print(json.dumps({"seccomp_inherited": True, "socket_denied": True}))
"""


class ChildFailure(ValueError):
    def __init__(self, code, diagnostic):
        super().__init__(code)
        self.code = code
        self.diagnostic = diagnostic


def child_python_argv(program, *, root, vendor):
    prefix = "import sys;sys.path[:0]=" + repr([str(root), str(vendor)]) + ";"
    complete = prefix + program
    try:
        compile(complete, "<fixed-userspace-child>", "exec")
    except SyntaxError as error:
        raise ChildFailure(
            "DEVELOPMENT_USERSPACE_GENERATION_INVALID",
            {
                "category": "GENERATION_SYNTAX",
                "location": "FIXED_CHILD_PROGRAM",
                "line": error.lineno,
                "column": error.offset,
                "process_started": False,
            },
        ) from None
    return (sys.executable, "-I", "-S", "-B", "-c", complete)


def child_diagnostic(result, category):
    outcomes = {"EXITED", "TIMEOUT", "LAUNCH_FAILED", "PROCESS_ERROR", "CANCELLED"}
    cleanup_codes = {"PROCESS_CLEANUP_FAILED", "OUTPUT_DRAIN_FAILED"}
    return {
        "category": category,
        "outcome": result.outcome if result.outcome in outcomes else "UNKNOWN",
        "returncode": result.returncode
        if type(result.returncode) is int and -255 <= result.returncode <= 255
        else None,
        "stdout_truncated": bool(result.stdout_summary.get("truncated", True)),
        "stderr_truncated": bool(result.stderr_summary.get("truncated", True)),
        "stdout_missing": bool(result.stdout_summary.get("missing", True)),
        "stderr_missing": bool(result.stderr_summary.get("missing", True)),
        "stderr_present": bool(result.stderr),
        "cleanup": "CLOSED" if not result.secondary_errors else "UNCONFIRMED",
        "secondary_codes": sorted(
            {
                value if value in cleanup_codes else "OTHER_CLEANUP_ERROR"
                for value in result.secondary_errors
            }
        ),
    }


def _run_python(program, *, root, vendor, work, timeout=120, test_result=False):
    from installer.apt_diagnostics import capture_process
    from release.materials import reject_duplicate_json_keys

    environment = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(work / "home"),
        "TMPDIR": str(work / "tmp"),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PYTHONPATH": str(vendor),
    }
    argv = child_python_argv(program, root=root, vendor=vendor)
    result = capture_process(
        argv,
        timeout=timeout,
        environment=environment,
    )
    completed = (
        result.outcome == "EXITED"
        and result.returncode in ((0, 2) if test_result else (0,))
        and not result.secondary_errors
        and not result.stdout_summary["truncated"]
        and not result.stderr_summary["truncated"]
        and not result.stdout_summary["missing"]
        and not result.stderr_summary["missing"]
        and not result.stderr
    )
    if not completed:
        category = (
            "PROCESS_START"
            if result.outcome == "LAUNCH_FAILED"
            else "PROCESS_CLEANUP"
            if result.outcome == "EXITED"
            and result.returncode in ((0, 2) if test_result else (0,))
            and result.secondary_errors
            else "CHILD_EXECUTION"
        )
        raise ChildFailure(
            "DEVELOPMENT_USERSPACE_CHILD_FAILED", child_diagnostic(result, category)
        )
    try:
        return json.loads(result.stdout, object_pairs_hook=reject_duplicate_json_keys)
    except (ValueError, UnicodeError):
        raise ChildFailure(
            "DEVELOPMENT_USERSPACE_CHILD_PROTOCOL_INVALID",
            child_diagnostic(result, "PROTOCOL_MATERIAL"),
        ) from None


def _test_program(ids):
    # Record only the requested source-constant ID, including loader/subTest
    # errors whose unittest internal IDs need not equal that fixed ID.
    return "\n".join(
        (
            _test_guard_program(ids == EXTRA_TEST_IDS),
            "import json",
            "from scripts.userspace_test_result import run_tests",
            "ids=" + repr(tuple(ids)),
            "result=run_tests(ids,guard)",
            "guard.require_clean()",
            "print(json.dumps(result))",
            "raise SystemExit(0 if result['successful'] and not result['skips'] and result['run']==len(ids) else 2)",
        )
    )


def _test_guard_program(allow_fd_child=False):
    lines = [
        "import sys",
        "from scripts.tests.userspace_effect_guard import UserspaceEffectGuard",
    ]
    if allow_fd_child:
        lines.extend(
            [
                "from pathlib import Path",
                "root=Path(sys.path[0])",
                "program=('import os,sys;sys.path.insert(0,'+repr(str(root))+');'"
                "+'from scripts.candidate_diagnostics import best_effort_fault;'"
                "+'from scripts.tests.test_formal_failure_diagnostics import actual_exception;'"
                "+'raise SystemExit(2 if best_effort_fault(actual_exception())==\"COMPLETE\" else 9)')",
                "guard=UserspaceEffectGuard((sys.executable,'-B','-c',program))",
            ]
        )
    else:
        lines.append("guard=UserspaceEffectGuard()")
    lines.append("sys.addaudithook(guard.audit)")
    return "\n".join(lines)


def _run_tests(report, stage, ids, *, root, vendor, work):
    report["phase"] = stage
    result = _run_python(
        _test_program(ids), root=root, vendor=vendor, work=work, test_result=True
    )
    # Save the safe failed projection before rejecting the stage.
    require(
        type(result) is dict
        and result.get("tests") == list(ids)
        and type(result.get("records")) is list,
        "DEVELOPMENT_USERSPACE_TEST_REPORT_INVALID",
    )
    require(
        all(
            type(item) is dict
            and set(item) == {"id", "status", "diagnostics"}
            and item["id"] in ids
            and item["status"] in {"PASS", "FAIL", "ERROR", "SKIP", "NOT_RUN"}
            and type(item["diagnostics"]) is list
            for item in result["records"]
        ),
        "DEVELOPMENT_USERSPACE_TEST_REPORT_INVALID",
    )
    report["stages"][stage] = result
    from scripts.userspace_test_result import test_report_safe_to_continue

    require(
        result.get("projection_overflow") is False
        and result.get("fixture_retained_owners") == 0
        and result.get("aborted") is False
        and result.get("effect_violation") is False
        and test_report_safe_to_continue(result),
        "DEVELOPMENT_USERSPACE_FIXTURE_CLEANUP_UNCONFIRMED",
    )
    return (
        result.get("successful") is True
        and result.get("run") == len(ids)
        and result.get("failures") == 0
        and result.get("errors") == 0
        and result.get("skips") == []
        and result["records"]
        == [{"id": i, "status": "PASS", "diagnostics": []} for i in ids]
    )


def run_isolation_self(report):
    report["phase"] = "NETWORK_ISOLATION_SELF"
    isolation = {"self": {"status": "FAIL"}, "child": {"status": "NOT_RUN"}}
    report["network_isolation"] = isolation
    isolation["self"] = {"status": "PASS", **enforce_no_network()}


def run_isolation_child(report, *, root, vendor, work):
    report["phase"] = "NETWORK_ISOLATION_CHILD"
    isolation = report["network_isolation"]
    require(
        isolation["self"]["status"] == "PASS",
        "DEVELOPMENT_USERSPACE_SECCOMP_PROBE_FAILED",
    )
    isolation["child"] = {"status": "FAIL"}
    child = _run_python(
        _network_isolation_child_program(),
        root=root,
        vendor=vendor,
        work=work,
        timeout=15,
    )
    require(
        child == {"seccomp_inherited": True, "socket_denied": True},
        "DEVELOPMENT_USERSPACE_SECCOMP_PROBE_FAILED",
    )
    isolation["child"] = {"status": "PASS", **child}


def safe_failure_code(error):
    code = getattr(error, "code", None)
    if type(error) is ValueError and len(error.args) == 1:
        code = error.args[0]
    return (
        code
        if type(code) is str and re.fullmatch(r"[A-Z][A-Z0-9_]{0,95}", code)
        else "DEVELOPMENT_USERSPACE_FAILED"
    )


def execute_report(mode="FINAL_SET"):
    report = {
        "schema": "animemo.development-userspace-result/v1",
        "purpose": PURPOSE,
        "status": "FAIL",
        "phase": "INPUT_VALIDATION",
        "stages": {},
        "sudo_capture_attempts": 0,
        "installer_executions": 0,
        "formal_authority_granted": False,
        "candidate_acceptance_authority_granted": False,
        "workload_mode": mode,
        "workload_closed": False,
    }
    try:
        run(report)
    except BaseException as error:  # noqa: BLE001 - Preserve safe phase/results even on cancellation, never exception text.
        report.update(status="FAIL", failure_code=safe_failure_code(error))
        if type(error) is ChildFailure:
            report["child_failure"] = error.diagnostic
        report["failure_category"] = (
            error.diagnostic["category"]
            if type(error) is ChildFailure
            and error.diagnostic["category"] != "CHILD_EXECUTION"
            else "ISOLATION_SELFTEST"
            if report["phase"].startswith("NETWORK_ISOLATION")
            else "TEST_ASSERTION"
            if report["failure_code"] == "DEVELOPMENT_USERSPACE_TEST_FAILED"
            else "PROTOCOL_MATERIAL"
        )
    return report


def run(report):
    require(report.get("workload_mode") in {"DIAGNOSE_TRUNCATION", "FINAL_SET"})
    root = Path(__file__).resolve().parents[1]
    inputs = root / "userspace-probe"
    context = json.loads((inputs / "context.json").read_bytes())
    require(
        context["purpose"] == PURPOSE
        and context["test_ids"] == list(TEST_IDS)
        and context["extra_test_ids"] == list(EXTRA_TEST_IDS)
        and context["formal_authority_granted"] is False
        and context["sudo_capture_attempts"] == 0
    )
    require(
        sys.platform == "linux"
        and platform.machine() == "x86_64"
        and sys.version_info[:2] == (3, 12)
        and os.geteuid() != 0,
        "DEVELOPMENT_USERSPACE_PLATFORM_REQUIRED",
    )
    for role, item in context["roles"].items():
        path = inputs / role
        require(path.is_file() and not path.is_symlink() and path.stat().st_nlink == 1)
        with path.open("rb") as stream:
            observed = "sha256:" + hashlib.file_digest(stream, "sha256").hexdigest()
        require(path.stat().st_size == item["size"] and observed == item["sha256"])
    report.update(
        source_sha=context["source_sha"],
        source_tree=context["source_tree"],
        input_manifest_sha256=context["input_manifest_sha256"],
        phase="NETWORK_ISOLATION_SELF",
    )
    run_isolation_self(report)
    report["phase"] = "PRIVATE_DEPENDENCIES"
    work = inputs / "run"
    work.mkdir(mode=0o700)
    for name in ("home", "tmp", "scratch", "kit-parent"):
        (work / name).mkdir(mode=0o700)
    from installer.offline_python_runtime import install_wheel_runtime

    vendor = work / "python-runtime"
    install_wheel_runtime(root / "wheelhouse", vendor)
    sys.path.insert(1, str(vendor))
    run_isolation_child(report, root=root, vendor=vendor, work=work)
    report.update(
        platform="linux/amd64",
        python=platform.python_version(),
        uid=os.geteuid(),
    )
    if report["workload_mode"] == "DIAGNOSE_TRUNCATION":
        report["phase"] = "TRUNCATION_DIAGNOSTIC"
        diagnostic = _run_python(
            _test_guard_program()
            + "\nimport json\nfrom scripts.userspace_test_result import diagnose_truncation\nresult=diagnose_truncation(guard)\nguard.require_clean()\nprint(json.dumps(result))",
            root=root,
            vendor=vendor,
            work=work,
        )
        report["stages"]["truncation_diagnostic"] = diagnostic
        boundary = diagnostic["boundary_observation"]
        require(
            diagnostic["original_test"]["fixture_retained_owners"] == 0
            and diagnostic["original_test"]["projection_overflow"] is False
            and diagnostic["original_test"]["projection_fault"] is False
            and boundary["fixture_cleanup"] == "CLOSED"
            and boundary["retained_owners_after_fixture"] == 0
            and boundary["fixture_descriptor_closed"] is True
            and boundary["fixture_stream_closed"] is True
            and boundary["fixture_owner_revoked"] is True
            and boundary["observed_pattern_expected"] is True,
            "DEVELOPMENT_USERSPACE_FIXTURE_CLEANUP_UNCONFIRMED",
        )
        report.update(status="DIAGNOSTIC_COMPLETE", workload_closed=True)
        return
    seven_pass = _run_tests(
        report, "seven_platform_tests", TEST_IDS, root=root, vendor=vendor, work=work
    )
    truncation_pass = _run_tests(
        report,
        "truncation_regressions",
        TRUNCATION_REGRESSION_IDS,
        root=root,
        vendor=vendor,
        work=work,
    )
    fd_pass = _run_tests(
        report, "posix_pass_fds", EXTRA_TEST_IDS, root=root, vendor=vendor, work=work
    )
    report["phase"] = "PRODUCT_IMPORTS"
    product = (
        "import importlib,json;names=('installer.production','updater.source','cryptography','_cffi_backend');"
        "loaded={n:importlib.import_module(n).__file__ for n in names};"
        "from pathlib import Path;allowed=" + repr([str(root), str(vendor)]) + ";"
        "assert all(any(Path(p).is_relative_to(Path(a)) for a in allowed) for p in loaded.values());"
        "print(json.dumps({'modules':list(loaded),'status':'PASS','origin_bound':True,'execution':'IMPORT_ONLY'}))"
    )
    report["stages"]["product_imports"] = _run_python(
        product, root=root, vendor=vendor, work=work
    )
    report["phase"] = "KIT_VALIDATION"
    from bootstrap_kit import seed, manifest

    selection = manifest.OperatorSelection(
        **json.loads((inputs / "kit_selection").read_bytes())
    )
    with seed.verify_local(
        manifest_path=inputs / "kit_manifest",
        archive_path=inputs / "kit_archive",
        selection=selection,
        destination_parent=work / "kit-parent",
        now=datetime.now(timezone.utc),
        production=False,
    ) as kit:
        from scripts.development_userspace_inputs import (
            verify_kit_library_compatibility,
        )

        require(
            {key: kit.manifest["source"][key] for key in ("commit", "tree")}
            == context["kit_source"],
            "DEVELOPMENT_USERSPACE_KIT_COMPATIBILITY_INVALID",
        )
        compatibility = verify_kit_library_compatibility(
            kit_root=kit.root,
            kit_manifest=kit.manifest,
            execution_root=root,
        )
        require(
            compatibility == context["kit_compatibility"],
            "DEVELOPMENT_USERSPACE_KIT_COMPATIBILITY_INVALID",
        )
        report["kit_source"] = context["kit_source"]
        report["kit_compatibility"] = compatibility
        report["phase"] = "KIT_IMPORTS"
        kit_imports = (
            "import importlib,json;names=('bootstrap_kit.runtime','bootstrap_kit.trust',"
            "'installer.bootstrap','installer.tokenless_stage0','updater.source','jsonschema','cramjam','rpds');"
            "loaded={n:importlib.import_module(n).__file__ for n in names};"
            "from pathlib import Path;k=Path(" + repr(str(kit.root)) + ");"
            "assert all(Path(p).is_relative_to(k) for p in loaded.values());"
            "print(json.dumps({'modules':list(loaded),'status':'PASS','origin_bound':True,'execution':'IMPORT_ONLY'}))"
        )
        report["stages"]["kit_imports"] = _run_python(
            kit_imports,
            root=kit.root / "library",
            vendor=kit.root / "vendor",
            work=work,
        )
        from bootstrap_kit.trust import load_local_trust

        material = load_local_trust(kit.root / "trust")
        from installer.tokenless_stage0 import (
            TokenlessActionsVerifier,
            TokenlessStage0Error,
        )
        from installer.anonymous_release_transport import UntrustedReleaseMaterials
        from installer.bootstrap import authorize_online_stage0_test_only

        release = json.loads((inputs / "release_manifest").read_bytes())
        expected = [
            ("ghcr.io/yanyuhanyue/animemo-api", release["images"]["api"]["digest"]),
            ("ghcr.io/yanyuhanyue/animemo-web", release["images"]["web"]["digest"]),
            ("release-manifest.json", context["roles"]["release_manifest"]["sha256"]),
            (
                "deployment-contract.json",
                context["roles"]["deployment_contract"]["sha256"],
            ),
            ("installer-materials.tar", ORIGINAL),
        ]
        report["phase"] = "ACTIONS_PROOFS"
        verifier = TokenlessActionsVerifier(material, scratch_parent=work / "scratch")
        for i, (name, digest) in enumerate(expected):
            verified = verifier.verify(
                bundle=(inputs / f"actions_{i}").read_bytes(),
                evidence_name=(
                    "api-image",
                    "web-image",
                    "release-manifest",
                    "deployment-contract",
                    "installer-materials",
                )[i],
                subject_name=name,
                subject_sha256=digest,
                workflow=".github/workflows/release.yml",
                source_commit=COMMIT,
            )
            require(
                verified.source_commit == COMMIT and verified.signer_digest == COMMIT
            )
        damaged = json.loads((inputs / "actions_0").read_bytes())
        original = base64.b64decode(
            damaged["dsseEnvelope"]["signatures"][0]["sig"], validate=True
        )
        damaged["dsseEnvelope"]["signatures"][0]["sig"] = base64.b64encode(
            bytes([original[0] ^ 1]) + original[1:]
        ).decode()
        try:
            verifier.verify(
                bundle=json.dumps(damaged).encode(),
                evidence_name="api-image",
                subject_name=expected[0][0],
                subject_sha256=expected[0][1],
                workflow=".github/workflows/release.yml",
                source_commit=COMMIT,
            )
        except TokenlessStage0Error as error:
            require(
                error.code == "BOOTSTRAP_TOKENLESS_SIGNATURE_REJECTED",
                "DEVELOPMENT_USERSPACE_WRONG_NEGATIVE",
            )
        else:
            require(False, "DEVELOPMENT_USERSPACE_SIGNATURE_ACCEPTED")
        report["stages"]["actions"] = {
            "valid_proofs": 5,
            "tampered_signature": "REJECTED",
            "time_policy": "REAL_CURRENT_CLOCK_RETAINED_TRUST_MATERIALS",
        }
        # Keep the exact original outer asset; do not confuse the role name with
        # the production consumer's required basename.
        report["phase"] = "PLATFORM_RELEASE"
        archive = work / "installer-materials.tar"
        with (
            (inputs / "original_archive").open("rb") as source,
            archive.open("xb") as target,
        ):
            while chunk := source.read(1024 * 1024):
                target.write(chunk)
        with archive.open("rb") as stream:
            require(
                "sha256:" + hashlib.file_digest(stream, "sha256").hexdigest()
                == ORIGINAL
            )
        materials = UntrustedReleaseMaterials(
            "v2.0.0-rc.3",
            json.loads((inputs / "release_metadata").read_bytes()),
            TAG_OBJECT,
            COMMIT,
            (inputs / "release_bundle").read_bytes(),
            (),
        )
        consumed = authorize_online_stage0_test_only(
            tag="v2.0.0-rc.3",
            release_commit=COMMIT,
            archive=archive,
            verified_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            material=material,
            inputs=materials,
            scratch_parent=work / "scratch",
        )
        require(
            consumed["state"] == "TEST_ONLY"
            and consumed["production_authority_granted"] is False
        )
        report["stages"]["platform_release"] = {
            "status": "VERIFIED",
            "consumer": "TEST_ONLY",
            "current_tuf_update": "NOT_RUN",
        }
    report.update(
        status="PASS" if seven_pass and truncation_pass and fd_pass else "FAIL",
        phase="COMPLETE",
        workload_closed=True,
    )
    if not seven_pass or not truncation_pass or not fd_pass:
        report.update(
            failure_category="TEST_RESULTS",
            failure_code="DEVELOPMENT_USERSPACE_TEST_FAILED",
        )


if __name__ == "__main__":
    # Exit zero means only that bounded diagnostic delivery completed. The host
    # independently requires status PASS, all identities and isolation evidence.
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode", choices=("DIAGNOSE_TRUNCATION", "FINAL_SET"), default="FINAL_SET"
    )
    print(json.dumps(execute_report(parser.parse_args().mode), sort_keys=True))
