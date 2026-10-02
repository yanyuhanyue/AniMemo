"""Pure userspace result/projection boundaries; no POSIX execution is claimed."""

import sys

from scripts.tests.userspace_effect_guard import UserspaceEffectGuard

# Install before production imports. Caught attempts remain latched and fail
# tearDown; these tests never request an actual socket or child process.
_IMPORT_GUARD = UserspaceEffectGuard()
sys.addaudithook(_IMPORT_GUARD.audit)

import json
import copy
import unittest
from unittest import mock

from bootstrap_kit.safe_files import KitFileError
from scripts import userspace_platform_probe as probe
from scripts import userspace_test_result as subject
from updater.archive_handoff import ArchiveHandoffError


class UserspaceTestResultTests(unittest.TestCase):
    def tearDown(self):
        _IMPORT_GUARD.require_clean()

    def run_case(self, case):
        result = subject.SafeResult()
        case.run(result)
        return result

    def test_known_exact_error_keeps_fixed_code_without_message(self):
        error = ArchiveHandoffError("HANDLE_CLOSE_FAILED")
        error.args = ("SECRET_EXCEPTION_TEXT",)
        result = subject.project_exception(error)
        self.assertEqual(
            result["chain"],
            [
                {
                    "relation": "PRIMARY",
                    "type": "ArchiveHandoffError",
                    "code": "BOOTSTRAP_ARCHIVE_HANDLE_CLOSE_FAILED",
                    "secondary_codes": [],
                }
            ],
        )
        self.assertNotIn("SECRET_EXCEPTION_TEXT", json.dumps(result))

    def test_kit_fixed_arg_code_is_allowed_only_if_exactly_known(self):
        result = subject.project_exception(KitFileError("BOOTSTRAP_KIT_FILE_CHANGED"))
        self.assertEqual(result["chain"][0]["code"], "BOOTSTRAP_KIT_FILE_CHANGED")
        unknown = subject.project_exception(KitFileError("PRIVATE_PATH_AND_MESSAGE"))
        self.assertEqual(unknown["chain"][0]["code"], "UNKNOWN")
        self.assertNotIn("PRIVATE_PATH_AND_MESSAGE", json.dumps(unknown))

    def test_unknown_subclass_never_invokes_str_repr_or_getattribute(self):
        calls = []

        class Hostile(RuntimeError):
            def __str__(self):
                calls.append("str")
                raise AssertionError("MUST_NOT_FORMAT")

            def __repr__(self):
                calls.append("repr")
                raise AssertionError("MUST_NOT_FORMAT")

            def __getattribute__(self, name):
                calls.append("getattribute")
                raise AssertionError("MUST_NOT_ACCESS_DYNAMIC_ATTRIBUTE")

        value = subject.project_exception(Hostile("SECRET"))
        self.assertEqual(calls, [])
        self.assertEqual(value["chain"][0]["type"], "UNKNOWN")
        self.assertNotIn("SECRET", json.dumps(value))

    def test_unknown_exception_properties_are_not_executed(self):
        calls = []

        class HostileProperty(Exception):
            @property
            def __cause__(self):
                calls.append("cause_property")
                raise AssertionError("MUST_NOT_EXECUTE_PROPERTY")

            @property
            def __context__(self):
                calls.append("context_property")
                raise AssertionError("MUST_NOT_EXECUTE_PROPERTY")

        value = subject.project_exception(HostileProperty("SECRET"))
        self.assertEqual(calls, [])
        self.assertEqual(value["chain"][0]["type"], "UNKNOWN")

    def test_unknown_exception_metaclass_hash_and_equality_are_not_executed(self):
        calls = []

        class HostileMeta(type):
            def __hash__(cls):
                calls.append("hash")
                raise AssertionError("MUST_NOT_HASH_UNKNOWN_TYPE")

            def __eq__(cls, other):
                calls.append("equality")
                raise AssertionError("MUST_NOT_COMPARE_UNKNOWN_TYPE")

        class Hostile(Exception, metaclass=HostileMeta):
            pass

        value = subject.project_exception(Hostile("SECRET"))
        self.assertEqual(calls, [])
        self.assertEqual(value["chain"][0]["type"], "UNKNOWN")

    @staticmethod
    def diagnostic_fixture():
        from scripts.development_userspace_inputs import TEST_IDS

        inner = KitFileError("BOOTSTRAP_KIT_FILE_CHANGED")
        outer = ArchiveHandoffError("HANDLE_CLOSE_FAILED")
        outer.__context__ = inner
        outer.__suppress_context__ = True
        return {
            "tests": [TEST_IDS[-1]],
            "run": 1,
            "errors": 1,
            "failures": 0,
            "skips": [],
            "successful": False,
            "projection_overflow": False,
            "projection_fault": False,
            "fixture_retained_owners": 0,
            "records": [
                {
                    "id": TEST_IDS[-1],
                    "status": "ERROR",
                    "diagnostics": [
                        {
                            "status": "ERROR",
                            "phase": "FIXTURE_EXIT",
                            "exception": subject.project_exception(outer),
                        }
                    ],
                }
            ],
        }

    def test_original_exact_error_is_safe_for_second_bounded_fixture(self):
        # This is a marked schema fixture, not an actual POSIX reproduction.
        value = self.diagnostic_fixture()
        subject.require_original_diagnostic_safe(value)
        self.assertFalse(value["successful"])
        self.assertEqual(value["errors"], 1)

    def test_original_diagnostic_rejects_cleanup_secondary_limited_cancel_unknown(self):
        mutations = (
            lambda r: r["records"][0]["diagnostics"][0].update(phase="FIXTURE_CLEANUP"),
            lambda r: r["records"][0]["diagnostics"][0]["exception"]["chain"][0].update(
                secondary_codes=["UNKNOWN"]
            ),
            lambda r: r["records"][0]["diagnostics"][0]["exception"].update(
                limited=True
            ),
            lambda r: r["records"][0]["diagnostics"][0]["exception"]["chain"][0].update(
                type="KeyboardInterrupt", code="UNKNOWN"
            ),
            lambda r: r["records"][0]["diagnostics"][0]["exception"]["chain"][1].update(
                type="UNKNOWN"
            ),
            lambda r: r.update(fixture_retained_owners=1),
            lambda r: r.update(projection_overflow=True),
            lambda r: r.update(projection_fault=True),
            lambda r: r.update(run=0),
            lambda r: r.update(errors=0),
            lambda r: r.update(failures=1),
            lambda r: r.update(skips=["FIXED_TRUNCATION_ID"]),
        )
        for index, mutate in enumerate(mutations):
            with self.subTest(mutation=index):
                value = self.diagnostic_fixture()
                mutate(value)
                with self.assertRaisesRegex(
                    ValueError, "DEVELOPMENT_USERSPACE_DIAGNOSTIC_UNEXPECTED"
                ):
                    subject.require_original_diagnostic_safe(value)

    def test_original_diagnostic_requires_both_exact_layers_in_order(self):
        for change in ("missing_inner", "missing_outer", "reversed", "wrong_code"):
            with self.subTest(change=change):
                value = self.diagnostic_fixture()
                chain = value["records"][0]["diagnostics"][0]["exception"]["chain"]
                if change == "missing_inner":
                    chain.pop()
                elif change == "missing_outer":
                    chain.pop(0)
                elif change == "reversed":
                    chain.reverse()
                else:
                    chain[0]["code"] = "BOOTSTRAP_ARCHIVE_CLEANUP_FAILED"
                with self.assertRaisesRegex(
                    ValueError, "DEVELOPMENT_USERSPACE_DIAGNOSTIC_UNEXPECTED"
                ):
                    subject.require_original_diagnostic_safe(value)

    def test_exact_projection_rejects_absent_empty_limited_and_secondary(self):
        expected = [("ArchiveHandoffError", "BOOTSTRAP_ARCHIVE_IDENTITY_MISMATCH")]
        good = subject.project_exception(ArchiveHandoffError("IDENTITY_MISMATCH"))
        self.assertTrue(subject.exact_projection(good, expected))
        variants = [None, {"chain": [], "limited": False}]
        for key, value in (("limited", True),):
            item = copy.deepcopy(good)
            item[key] = value
            variants.append(item)
        item = copy.deepcopy(good)
        item["chain"][0]["secondary_codes"] = ["UNKNOWN"]
        variants.append(item)
        for index, item in enumerate(variants):
            with self.subTest(index=index):
                self.assertFalse(subject.exact_projection(item, expected))

    def test_regular_assertion_can_continue_independent_group_but_stays_failed(self):
        class Sample(unittest.TestCase):
            def runTest(self):
                self.fail("FIXED_ASSERTION_FAILURE")

        with (
            mock.patch.object(
                unittest.defaultTestLoader,
                "loadTestsFromName",
                return_value=unittest.TestSuite([Sample()]),
            ),
            mock.patch("updater.archive_handoff._RETAINED", {}),
        ):
            report = subject.run_tests(("FIXED_ASSERTION",))
        self.assertTrue(subject.test_report_safe_to_continue(report))
        self.assertFalse(report["successful"])
        self.assertEqual((report["failures"], report["errors"]), (1, 0))

    def test_continue_rejects_cleanup_secondary_limited_cancellation_unknown(self):
        normal = {
            "projection_overflow": False,
            "projection_fault": False,
            "fixture_retained_owners": 0,
            "records": [
                {
                    "id": "FIXED",
                    "status": "FAIL",
                    "diagnostics": [
                        {
                            "status": "FAIL",
                            "phase": "TEST_EXECUTION",
                            "exception": subject.project_exception(
                                AssertionError("FIXED")
                            ),
                        }
                    ],
                }
            ],
        }
        changes = (
            lambda r: r.update(projection_overflow=True),
            lambda r: r.update(projection_fault=True),
            lambda r: r.update(fixture_retained_owners=1),
            lambda r: r["records"][0]["diagnostics"][0].update(phase="FIXTURE_CLEANUP"),
            lambda r: r["records"][0]["diagnostics"][0].update(phase="BORROW_EXIT"),
            lambda r: r["records"][0]["diagnostics"][0]["exception"].update(
                limited=True
            ),
            lambda r: r["records"][0]["diagnostics"][0]["exception"].update(chain=[]),
            lambda r: r["records"][0]["diagnostics"][0]["exception"]["chain"][0].update(
                secondary_codes=["UNKNOWN"]
            ),
            lambda r: r["records"][0]["diagnostics"][0]["exception"]["chain"][0].update(
                type="KeyboardInterrupt"
            ),
            lambda r: r["records"][0]["diagnostics"][0]["exception"]["chain"][0].update(
                type="UNKNOWN"
            ),
            lambda r: r["records"][0]["diagnostics"][0]["exception"]["chain"][0].update(
                type="OSError"
            ),
            lambda r: r["records"][0]["diagnostics"][0]["exception"]["chain"][0].update(
                code="BOOTSTRAP_KIT_FILE_CHANGED"
            ),
        )
        for index, mutate in enumerate(changes):
            with self.subTest(mutation=index):
                report = copy.deepcopy(normal)
                mutate(report)
                self.assertFalse(subject.test_report_safe_to_continue(report))

    def test_original_diagnostic_rejects_wrong_cause_relation(self):
        value = self.diagnostic_fixture()
        value["records"][0]["diagnostics"][0]["exception"]["chain"][1]["relation"] = (
            "PRIMARY"
        )
        with self.assertRaisesRegex(
            ValueError, "DEVELOPMENT_USERSPACE_DIAGNOSTIC_UNEXPECTED"
        ):
            subject.require_original_diagnostic_safe(value)

    def test_original_diagnostic_rejects_contradictory_pass_record(self):
        value = self.diagnostic_fixture()
        value["successful"] = True
        value["records"][0]["status"] = "PASS"
        with self.assertRaisesRegex(
            ValueError, "DEVELOPMENT_USERSPACE_DIAGNOSTIC_UNEXPECTED"
        ):
            subject.require_original_diagnostic_safe(value)

    def test_secondary_codes_are_bounded_and_unknown_stays_unknown(self):
        class Hostile:
            def __str__(self):
                raise AssertionError("MUST_NOT_FORMAT_SECONDARY")

            def __repr__(self):
                raise AssertionError("MUST_NOT_FORMAT_SECONDARY")

        error = ArchiveHandoffError("HANDLE_CLOSE_FAILED")
        error.secondary_errors = (
            "BOOTSTRAP_KIT_FILE_CHANGED",
            "SECRET_SECONDARY",
            Hostile(),
            7,
            "BOOTSTRAP_ARCHIVE_CLEANUP_FAILED",
        )
        projected = subject.project_exception(error)
        self.assertEqual(
            projected["chain"][0]["secondary_codes"],
            [
                "BOOTSTRAP_KIT_FILE_CHANGED",
                "UNKNOWN",
                "UNKNOWN",
                "UNKNOWN",
                "UNKNOWN",
            ],
        )
        self.assertNotIn("SECRET_SECONDARY", json.dumps(projected))

    def test_non_tuple_secondary_is_unknown_without_iteration(self):
        class HostileIterable:
            def __iter__(self):
                raise AssertionError("MUST_NOT_ITERATE")

            def __getitem__(self, index):
                raise AssertionError("MUST_NOT_INDEX")

        error = ArchiveHandoffError("HANDLE_CLOSE_FAILED")
        error.secondary_errors = HostileIterable()
        self.assertEqual(
            subject.project_exception(error)["chain"][0]["secondary_codes"], ["UNKNOWN"]
        )

    def test_cause_wins_and_context_cycle_is_bounded(self):
        first, second, ignored = ValueError("a"), TypeError("b"), RuntimeError("c")
        first.__cause__, first.__context__ = second, ignored
        second.__context__ = first
        value = subject.project_exception(first)
        self.assertEqual(
            [v["type"] for v in value["chain"]], ["ValueError", "TypeError"]
        )
        self.assertEqual([v["relation"] for v in value["chain"]], ["PRIMARY", "CAUSE"])
        self.assertTrue(value["limited"])

    def test_chain_depth_limit_is_four(self):
        chain = [ValueError(str(i)) for i in range(7)]
        for left, right in zip(chain, chain[1:]):
            left.__cause__ = right
        value = subject.project_exception(chain[0])
        self.assertEqual(len(value["chain"]), 4)
        self.assertTrue(value["limited"])

    def test_from_none_preserves_suppressed_context(self):
        try:
            try:
                raise KitFileError("BOOTSTRAP_KIT_FILE_CHANGED")
            except KitFileError:
                raise ArchiveHandoffError("HANDLE_CLOSE_FAILED") from None
        except ArchiveHandoffError as error:
            value = subject.project_exception(error)
        self.assertEqual(
            [v["relation"] for v in value["chain"]], ["PRIMARY", "SUPPRESSED_CONTEXT"]
        )
        self.assertEqual(value["chain"][1]["code"], "BOOTSTRAP_KIT_FILE_CHANGED")

    def test_test_failure_and_teardown_error_remain_separate(self):
        class Sample(unittest.TestCase):
            def runTest(self):
                self.fail("SECRET_ASSERTION")

            def tearDown(self):
                raise KitFileError("BOOTSTRAP_KIT_HANDLE_CLOSE_FAILED")

        result = self.run_case(Sample())
        self.assertEqual(
            (result.testsRun, len(result.failures), len(result.errors)), (1, 1, 1)
        )
        self.assertEqual(
            [(e["status"], e["phase"]) for e in result.events],
            [
                ("FAIL", "TEST_EXECUTION"),
                ("ERROR", "FIXTURE_CLEANUP"),
            ],
        )
        self.assertNotIn("SECRET_ASSERTION", json.dumps(result.events))
        self.assertEqual(result.failures[0][1], "REDACTED_FIXED_TEST_FAILURE")

    def test_setup_error_is_construction_and_test_does_not_run(self):
        called = []

        class Sample(unittest.TestCase):
            def setUp(self):
                raise ValueError("SECRET_SETUP")

            def runTest(self):
                called.append(True)

        result = self.run_case(Sample())
        self.assertEqual(called, [])
        self.assertEqual(result.events[0]["phase"], "TEST_CONSTRUCTION")
        self.assertEqual((len(result.errors), len(result.failures)), (1, 0))

    def test_cleanup_callback_is_cleanup_not_assertion_failure(self):
        class Sample(unittest.TestCase):
            def runTest(self):
                def callback():
                    raise BrokenPipeError("SECRET_PIPE")

                self.addCleanup(callback)

        result = self.run_case(Sample())
        self.assertEqual(result.events[0]["phase"], "FIXTURE_CLEANUP")
        self.assertEqual(
            result.events[0]["exception"]["chain"][0]["type"], "BrokenPipeError"
        )
        self.assertFalse(result.wasSuccessful())

    def test_subtests_keep_failure_and_error_counts(self):
        class Sample(unittest.TestCase):
            def runTest(self):
                with self.subTest(case="SECRET_ONE"):
                    self.fail("SECRET_SUBTEST")
                with self.subTest(case="SECRET_TWO"):
                    raise ValueError("SECRET_ERROR")

        result = self.run_case(Sample())
        self.assertEqual(
            (result.testsRun, len(result.failures), len(result.errors)), (1, 1, 1)
        )
        self.assertEqual([e["status"] for e in result.events], ["FAIL", "ERROR"])
        self.assertNotIn("SECRET", json.dumps(result.events))

    def test_loader_error_has_requested_id_and_error_status(self):
        name = __name__ + ".THIS_FIXED_CASE_DOES_NOT_EXIST"
        value = subject.run_tests((name,))
        self.assertEqual(value["tests"], [name])
        self.assertEqual((value["run"], value["errors"], value["failures"]), (1, 1, 0))
        self.assertEqual(value["records"][0]["id"], name)
        self.assertEqual(value["records"][0]["status"], "ERROR")
        self.assertFalse(value["successful"])

    def test_projection_overflow_is_latched_without_losing_error_counts(self):
        class Sample(unittest.TestCase):
            def runTest(self):
                for index in range(20):
                    with self.subTest(index=index):
                        raise ValueError("SECRET_OVERFLOW")

        result = self.run_case(Sample())
        self.assertEqual(len(result.events), 16)
        self.assertEqual(len(result.errors), 20)
        self.assertTrue(result.projection_overflow)
        self.assertFalse(result.wasSuccessful())

    def test_projection_exception_preserves_original_error_count_and_fault(self):
        class Sample(unittest.TestCase):
            def runTest(self):
                raise ValueError("SECRET_ORIGINAL_ERROR")

        with mock.patch.object(
            subject,
            "project_exception",
            side_effect=RuntimeError("SECRET_PROJECTION_ERROR"),
        ):
            result = self.run_case(Sample())
        self.assertEqual(
            (result.testsRun, len(result.errors), len(result.failures)), (1, 1, 0)
        )
        self.assertTrue(result.projection_fault)
        self.assertFalse(result.wasSuccessful())
        self.assertEqual(
            result.events,
            [
                {
                    "status": "ERROR",
                    "phase": "UNKNOWN",
                    "exception": {"chain": [], "limited": True},
                }
            ],
        )
        self.assertEqual(result.errors[0][1], "REDACTED_FIXED_TEST_FAILURE")

    def test_traceback_limit_exports_unknown_and_cannot_continue(self):
        def recurse(depth):
            if depth:
                recurse(depth - 1)
            else:
                raise ValueError("SECRET_DEEP_TRACEBACK")

        class Sample(unittest.TestCase):
            def runTest(self):
                recurse(70)

        with (
            mock.patch.object(
                unittest.defaultTestLoader,
                "loadTestsFromName",
                return_value=unittest.TestSuite([Sample()]),
            ),
            mock.patch("updater.archive_handoff._RETAINED", {}),
        ):
            report = subject.run_tests(("FIXED_DEEP_TRACEBACK",))
        self.assertEqual(report["records"][0]["diagnostics"][0]["phase"], "UNKNOWN")
        self.assertFalse(subject.test_report_safe_to_continue(report))
        self.assertEqual(report["errors"], 1)

    def test_run_tests_records_do_not_merge_independent_cases(self):
        class Pass(unittest.TestCase):
            def runTest(self):
                pass

        class Fail(unittest.TestCase):
            def runTest(self):
                self.fail("SECRET_FAIL")

        cases = {"FIXED_PASS": Pass, "FIXED_FAIL": Fail}
        with (
            mock.patch.object(
                unittest.defaultTestLoader,
                "loadTestsFromName",
                side_effect=lambda name: unittest.TestSuite([cases[name]()]),
            ),
            mock.patch("updater.archive_handoff._RETAINED", {}),
        ):
            value = subject.run_tests(tuple(cases))
        self.assertEqual(
            [(r["id"], r["status"]) for r in value["records"]],
            [("FIXED_PASS", "PASS"), ("FIXED_FAIL", "FAIL")],
        )
        self.assertEqual(value["records"][0]["diagnostics"], [])
        self.assertEqual((value["run"], value["failures"], value["errors"]), (2, 1, 0))

    def test_caught_guard_event_still_fails_clean_check(self):
        guard = UserspaceEffectGuard()
        # Exercise its real latch without issuing any socket/process syscall.
        for event in ("socket.connect", "subprocess.Popen"):
            try:
                guard.audit(event, ())
            except RuntimeError:
                pass
        self.assertEqual(
            [v["event"] for v in guard.violations],
            ["socket.connect", "subprocess.Popen"],
        )
        with self.assertRaisesRegex(
            RuntimeError, "USERSPACE_TEST_CAUGHT_EXTERNAL_EFFECT"
        ):
            guard.require_clean()

    def test_run_tests_overflow_is_rejected_by_payload_consumer(self):
        result = {
            "tests": ["FIXED"],
            "records": [{"id": "FIXED", "status": "ERROR", "diagnostics": []}],
            "projection_overflow": True,
            "projection_fault": False,
            "fixture_retained_owners": 0,
            "run": 1,
            "errors": 1,
            "failures": 0,
            "skips": [],
            "successful": False,
        }
        report = {"stages": {}}
        with mock.patch.object(probe, "_run_python", return_value=result):
            with self.assertRaises(ValueError):
                probe._run_tests(
                    report, "FIXED_STAGE", ("FIXED",), root=None, vendor=None, work=None
                )
        self.assertEqual(report["stages"]["FIXED_STAGE"], result)

    def test_no_posix_diagnostic_or_network_is_called_by_fixed_program_builder(self):
        with mock.patch.object(subject, "diagnose_truncation") as diagnose:
            program = probe._test_program(("FIXED_NO_EXECUTION",))
            compile(program, "<fixed-userspace-test-program>", "exec")
        diagnose.assert_not_called()
        self.assertIn("run_tests(ids,guard)", program)

    def test_distinct_context_cannot_hide_behind_explicit_cause(self):
        error = AssertionError()
        error.__cause__ = ValueError()
        error.__context__ = subject.ArchiveHandoffError("CLEANUP_FAILED")
        self.assertTrue(subject.project_exception(error)["limited"])

    def test_cleanup_error_stops_before_next_test(self):
        called = []

        class BrokenCleanup(unittest.TestCase):
            def runTest(self):
                pass

            def tearDown(self):
                raise RuntimeError("private-sentinel")

        class Next(unittest.TestCase):
            def runTest(self):
                called.append(True)

        with mock.patch.object(
            unittest.defaultTestLoader,
            "loadTestsFromName",
            side_effect=[BrokenCleanup(), Next()],
        ):
            value = subject.run_tests(("FIRST", "NEXT"))
        self.assertEqual(called, [])
        self.assertTrue(value["aborted"])
        self.assertEqual(value["records"][1]["status"], "NOT_RUN")

    def test_swallowed_guard_violation_stops_before_next_test(self):
        guard = UserspaceEffectGuard()
        called = []

        class Swallowed(unittest.TestCase):
            def runTest(self):
                try:
                    guard.audit("socket.connect", ())
                except RuntimeError:
                    pass

        class Next(unittest.TestCase):
            def runTest(self):
                called.append(True)

        with mock.patch.object(
            unittest.defaultTestLoader,
            "loadTestsFromName",
            side_effect=[Swallowed(), Next()],
        ):
            value = subject.run_tests(("FIRST", "NEXT"), guard)
        self.assertEqual(called, [])
        self.assertTrue(value["effect_violation"])
        self.assertTrue(value["aborted"])
