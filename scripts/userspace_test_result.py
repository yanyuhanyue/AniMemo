"""Bounded unittest evidence for the fixed DEV workload; never format exceptions."""

from __future__ import annotations

import errno
import os
import unittest

from bootstrap_kit.safe_files import KitFileError
from updater.archive_handoff import ArchiveHandoffError

TYPES = {
    AssertionError: "AssertionError",
    ValueError: "ValueError",
    TypeError: "TypeError",
    OSError: "OSError",
    RuntimeError: "RuntimeError",
    ImportError: "ImportError",
    ModuleNotFoundError: "ModuleNotFoundError",
    TimeoutError: "TimeoutError",
    BrokenPipeError: "BrokenPipeError",
    KeyboardInterrupt: "KeyboardInterrupt",
    ArchiveHandoffError: "ArchiveHandoffError",
    KitFileError: "KitFileError",
}
CODES = frozenset(
    {
        "BOOTSTRAP_ARCHIVE_IDENTITY_MISMATCH",
        "BOOTSTRAP_ARCHIVE_UNAVAILABLE",
        "BOOTSTRAP_ARCHIVE_ALREADY_BORROWED",
        "BOOTSTRAP_ARCHIVE_TRANSACTION_MISMATCH",
        "BOOTSTRAP_ARCHIVE_HANDLE_CLOSE_FAILED",
        "BOOTSTRAP_ARCHIVE_CLEANUP_FAILED",
        "BOOTSTRAP_ARCHIVE_POSTREAD_FAILED",
        "BOOTSTRAP_KIT_FILE_CHANGED",
        "BOOTSTRAP_KIT_FILE_UNSAFE",
        "BOOTSTRAP_KIT_HANDLE_CLOSE_FAILED",
    }
)


def project_exception(error):
    """Read only base exception slots and allowlisted exact-type dictionary values."""
    chain, seen = [], set()
    forked = False
    current = error
    relation = "PRIMARY"
    for _ in range(4):
        if current is None or id(current) in seen:
            break
        seen.add(id(current))
        kind = type(current)
        code = "UNKNOWN"
        secondary = []
        if kind is ArchiveHandoffError or kind is KitFileError:
            values = BaseException.__getattribute__(current, "__dict__")
            candidate = dict.get(values, "code") if type(values) is dict else None
            if kind is KitFileError:
                args = BaseException.args.__get__(current)
                candidate = args[0] if len(args) == 1 else None
            if type(candidate) is str and candidate in CODES:
                code = candidate
            extras = (
                dict.get(values, "secondary_errors", ())
                if type(values) is dict
                else None
            )
            secondary = (
                [
                    item if type(item) is str and item in CODES else "UNKNOWN"
                    for item in extras[:4]
                ]
                if type(extras) is tuple
                else ["UNKNOWN"]
            )
            if type(extras) is tuple and len(extras) > 4:
                secondary.append("UNKNOWN")
        chain.append(
            {
                "relation": relation,
                "type": next(
                    (label for known, label in TYPES.items() if kind is known),
                    "UNKNOWN",
                ),
                "code": code,
                "secondary_codes": secondary,
            }
        )
        cause = BaseException.__cause__.__get__(current)
        context = BaseException.__context__.__get__(current)
        if cause is not None and context is not None and cause is not context:
            forked = True
        if cause is not None:
            current, relation = cause, "CAUSE"
        elif context is not None:
            relation = (
                "SUPPRESSED_CONTEXT"
                if BaseException.__suppress_context__.__get__(current)
                else "CONTEXT"
            )
            current = context
        else:
            current = None
    return {"chain": chain, "limited": forked or current is not None}


def exact_projection(value, expected):
    return (
        type(value) is dict
        and value.get("limited") is False
        and [(item["type"], item["code"]) for item in value["chain"]] == expected
        and all(item["secondary_codes"] == [] for item in value["chain"])
    )


def require_original_diagnostic_safe(result):
    records = result["records"]
    if (
        result["run"] != 1
        or result["errors"] != 1
        or result["failures"] != 0
        or result["skips"]
        or result["projection_overflow"]
        or result.get("projection_fault") is not False
        or result.get("effect_violation", False) is not False
        or result.get("aborted", False) is not False
        or result["fixture_retained_owners"] != 0
        or len(records) != 1
        or result["successful"] is not False
        or records[0]["status"] != "ERROR"
        or len(records[0]["diagnostics"]) != 1
    ):
        raise ValueError("DEVELOPMENT_USERSPACE_DIAGNOSTIC_UNEXPECTED")
    event = records[0]["diagnostics"][0]
    if (
        event["status"] != "ERROR"
        or event["phase"] != "FIXTURE_EXIT"
        or [item["relation"] for item in event["exception"]["chain"]]
        != ["PRIMARY", "SUPPRESSED_CONTEXT"]
        or not exact_projection(
            event["exception"],
            [
                ("ArchiveHandoffError", "BOOTSTRAP_ARCHIVE_HANDLE_CLOSE_FAILED"),
                ("KitFileError", "BOOTSTRAP_KIT_FILE_CHANGED"),
            ],
        )
    ):
        raise ValueError("DEVELOPMENT_USERSPACE_DIAGNOSTIC_UNEXPECTED")


def test_report_safe_to_continue(result):
    if (
        result["projection_overflow"]
        or result.get("projection_fault") is not False
        or result["fixture_retained_owners"] != 0
    ):
        return False
    for record in result["records"]:
        for event in record["diagnostics"]:
            if event["phase"] not in {"TEST_EXECUTION", "TEST_CONSTRUCTION"}:
                return False
            projection = event["exception"]
            if projection["limited"] or not projection["chain"]:
                return False
            for item in projection["chain"]:
                if (
                    item["type"]
                    not in {
                        "AssertionError",
                        "TypeError",
                        "ValueError",
                        "ImportError",
                        "ModuleNotFoundError",
                    }
                    or item["code"] != "UNKNOWN"
                    or item["secondary_codes"]
                ):
                    return False
    return True


def exception_phase(tb):
    """Only known Python execution boundaries, not exception text, select a phase."""
    phase = "TEST_EXECUTION"
    for _ in range(64):
        if tb is None:
            return phase
        name = tb.tb_frame.f_code.co_name
        if name in {"_callSetUp", "setUp"}:
            phase = "TEST_CONSTRUCTION"
        elif name in {"_callTearDown", "tearDown", "doCleanups", "_callCleanup"}:
            phase = "FIXTURE_CLEANUP"
        elif name == "release_workspace" and phase != "FIXTURE_CLEANUP":
            phase = "FIXTURE_EXIT"
        elif name == "archive_for" and phase not in {"FIXTURE_CLEANUP", "FIXTURE_EXIT"}:
            phase = "BORROW_EXIT"
        tb = tb.tb_next
    return "UNKNOWN"


class SafeResult(unittest.TestResult):
    def __init__(self):
        super().__init__()
        self.events = []
        self.projection_overflow = False
        self.projection_fault = False

    def _exc_info_to_string(self, err, test):
        return "REDACTED_FIXED_TEST_FAILURE"

    def event(self, status, err):
        if len(self.events) >= 16:
            self.projection_overflow = True
            return
        try:
            event = {
                "status": status,
                "phase": exception_phase(err[2]),
                "exception": project_exception(err[1]),
            }
        except BaseException:  # noqa: BLE001 - Diagnostic failure must preserve the original unittest result.
            self.projection_fault = True
            event = {
                "status": status,
                "phase": "UNKNOWN",
                "exception": {"chain": [], "limited": True},
            }
        self.events.append(event)

    def addError(self, test, err):
        self.event("ERROR", err)
        super().addError(test, err)

    def addFailure(self, test, err):
        self.event("FAIL", err)
        super().addFailure(test, err)

    def addSubTest(self, test, subtest, err):
        if err is not None:
            self.event(
                "FAIL" if issubclass(err[0], test.failureException) else "ERROR", err
            )
        super().addSubTest(test, subtest, err)


def run_tests(ids, guard=None, *, diagnostic_original=False):
    from updater import archive_handoff

    result, records = SafeResult(), []
    aborted, effect_violation = False, False
    for name in ids:
        if aborted:
            records.append({"id": name, "status": "NOT_RUN", "diagnostics": []})
            continue
        before = len(result.failures), len(result.errors), len(result.skipped)
        event_start = len(result.events)
        unittest.defaultTestLoader.loadTestsFromName(name).run(result)
        after = len(result.failures), len(result.errors), len(result.skipped)
        status = (
            "ERROR"
            if after[1] > before[1]
            else "FAIL"
            if after[0] > before[0]
            else "SKIP"
            if after[2] > before[2]
            else "PASS"
        )
        records.append(
            {"id": name, "status": status, "diagnostics": result.events[event_start:]}
        )
        checkpoint = {
            "records": [records[-1]],
            "projection_overflow": result.projection_overflow,
            "projection_fault": result.projection_fault,
            "fixture_retained_owners": len(archive_handoff._RETAINED),
        }
        effect_violation = guard is not None and bool(guard.violations)
        if diagnostic_original:
            if len(ids) != 1:
                raise ValueError("DEVELOPMENT_USERSPACE_DIAGNOSTIC_UNEXPECTED")
            aborted = effect_violation or bool(checkpoint["fixture_retained_owners"])
        else:
            aborted = effect_violation or not test_report_safe_to_continue(checkpoint)
    return {
        "tests": list(ids),
        "run": result.testsRun,
        "failures": len(result.failures),
        "errors": len(result.errors),
        "skips": [r["id"] for r in records if r["status"] == "SKIP"],
        "successful": result.wasSuccessful(),
        "records": records,
        "projection_overflow": result.projection_overflow,
        "projection_fault": result.projection_fault,
        "fixture_retained_owners": len(archive_handoff._RETAINED),
        "aborted": aborted,
        "effect_violation": effect_violation,
    }


def diagnose_truncation(guard=None):
    """Original unchanged case and a second real small-file boundary observation."""
    from scripts.development_userspace_inputs import TEST_IDS
    from updater import archive_handoff
    from updater.tests.test_archive_handoff import ArchiveHandoffBoundaryTests

    original = run_tests((TEST_IDS[-1],), guard, diagnostic_original=True)
    try:
        require_original_diagnostic_safe(original)
    except ValueError:
        return {
            "original_test": original,
            "boundary_observation": {
                "fixture_cleanup": "NOT_RUN",
                "retained_owners_after_fixture": original["fixture_retained_owners"],
                "fixture_descriptor_closed": False,
                "fixture_stream_closed": False,
                "fixture_owner_revoked": False,
                "observed_pattern_expected": False,
            },
        }
    case = ArchiveHandoffBoundaryTests()
    observation = {"inner": None, "outer": None, "fixture_cleanup": "NOT_RUN"}
    owner = None
    descriptor = None
    case.setUp()
    try:
        try:
            with case.bound() as (owner, materials):
                descriptor = owner._stream.fileno()
                try:
                    with owner.archive_for(materials, source=case.source) as path:
                        path.write_bytes(b"x")
                except BaseException as error:  # noqa: BLE001 - Fixed fixture records bounded failure then verifies cleanup.
                    observation["inner"] = project_exception(error)
        except BaseException as error:  # noqa: BLE001 - Never export exception text; parent requires closed fixture.
            observation["outer"] = project_exception(error)
        observation["fixture_owner_retained"] = any(
            item[0] is owner for item in archive_handoff._RETAINED.values()
        )
        observation["fixture_stream_closed"] = (
            owner is not None and owner._stream.closed
        )
        observation["fixture_owner_revoked"] = owner is not None and not owner._active
        try:
            os.fstat(descriptor)
        except OSError as error:
            observation["fixture_descriptor_closed"] = error.errno == errno.EBADF
        else:
            observation["fixture_descriptor_closed"] = False
    finally:
        try:
            case.tearDown()
            observation["fixture_cleanup"] = "CLOSED"
        except BaseException as error:  # noqa: BLE001 - Cleanup failure is explicit and terminates the workload.
            observation["fixture_cleanup"] = "UNCONFIRMED"
            observation["cleanup_error"] = project_exception(error)
    observation["retained_owners_after_fixture"] = len(archive_handoff._RETAINED)
    observation["observed_pattern_expected"] = exact_projection(
        observation["inner"],
        [("ArchiveHandoffError", "BOOTSTRAP_ARCHIVE_IDENTITY_MISMATCH")],
    ) and exact_projection(
        observation["outer"],
        [
            ("ArchiveHandoffError", "BOOTSTRAP_ARCHIVE_HANDLE_CLOSE_FAILED"),
            ("KitFileError", "BOOTSTRAP_KIT_FILE_CHANGED"),
        ],
    )
    return {"original_test": original, "boundary_observation": observation}
