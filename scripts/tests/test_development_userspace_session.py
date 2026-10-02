"""Fixed userspace session admission and lifecycle; no VM or public network."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest import mock

from scripts import development_userspace_session as session
from scripts import development_userspace_probe as single


class UserspaceSessionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.limits = session.Limits(
            datetime.now(timezone.utc) + timedelta(hours=8), self.root
        )

    def request(self, mode="FINAL_SET"):
        return {
            "source_sha": "a" * 40,
            "source_tree": "b" * 40,
            "inputs_sha256": "sha256:" + "c" * 64,
            "mode": mode,
        }

    def report(self, status="FAIL"):
        request = self.request()
        value = {
            "schema": "animemo.development-userspace-result/v1",
            "purpose": session.PURPOSE,
            "source_sha": request["source_sha"],
            "source_tree": request["source_tree"],
            "input_manifest_sha256": request["inputs_sha256"],
            "status": status,
            "workload_mode": "DIAGNOSE_TRUNCATION"
            if status == "DIAGNOSTIC_COMPLETE"
            else "FINAL_SET",
            "phase": "TRUNCATION_DIAGNOSTIC"
            if status == "DIAGNOSTIC_COMPLETE"
            else "COMPLETE",
            "sudo_capture_attempts": 0,
            "installer_executions": 0,
            "uid": 1000,
            "platform": "linux/amd64",
            "formal_authority_granted": False,
            "candidate_acceptance_authority_granted": False,
            "workload_closed": True,
            "failure_category": "TEST_RESULTS",
            "network_isolation": {
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
        }

        def group(ids):
            return {
                "tests": list(ids),
                "run": len(ids),
                "errors": 0,
                "failures": 0,
                "skips": [],
                "successful": True,
                "projection_overflow": False,
                "projection_fault": False,
                "aborted": False,
                "effect_violation": False,
                "fixture_retained_owners": 0,
                "records": [
                    {"id": name, "status": "PASS", "diagnostics": []} for name in ids
                ],
            }

        value["stages"] = {
            "seven_platform_tests": group(session.TEST_IDS),
            "posix_pass_fds": group(session.EXTRA_TEST_IDS),
            "truncation_regressions": group(session.TRUNCATION_REGRESSION_IDS),
            "product_imports": {"status": "PASS", "origin_bound": True},
            "kit_imports": {"status": "PASS", "origin_bound": True},
            "actions": {"valid_proofs": 5, "tampered_signature": "REJECTED"},
            "platform_release": {
                "status": "VERIFIED",
                "consumer": "TEST_ONLY",
                "current_tuf_update": "NOT_RUN",
            },
        }
        if status == "FAIL":
            failed = value["stages"]["seven_platform_tests"]
            failed.update(successful=False, failures=1)
            failed["records"][0].update(
                status="FAIL",
                diagnostics=[
                    {
                        "status": "FAIL",
                        "phase": "TEST_EXECUTION",
                        "exception": {
                            "limited": False,
                            "chain": [
                                {
                                    "relation": "PRIMARY",
                                    "type": "AssertionError",
                                    "code": "UNKNOWN",
                                    "secondary_codes": [],
                                }
                            ],
                        },
                    }
                ],
            )
        if status == "DIAGNOSTIC_COMPLETE":

            def projection(items):
                return {
                    "limited": False,
                    "chain": [
                        {
                            "relation": relation,
                            "type": kind,
                            "code": code,
                            "secondary_codes": [],
                        }
                        for relation, kind, code in items
                    ],
                }

            inner = projection(
                [
                    (
                        "PRIMARY",
                        "ArchiveHandoffError",
                        "BOOTSTRAP_ARCHIVE_IDENTITY_MISMATCH",
                    )
                ]
            )
            outer = projection(
                [
                    (
                        "PRIMARY",
                        "ArchiveHandoffError",
                        "BOOTSTRAP_ARCHIVE_HANDLE_CLOSE_FAILED",
                    ),
                    (
                        "SUPPRESSED_CONTEXT",
                        "KitFileError",
                        "BOOTSTRAP_KIT_FILE_CHANGED",
                    ),
                ]
            )
            original = group((session.TEST_IDS[-1],))
            original.update(errors=1, successful=False)
            original["records"][0].update(
                status="ERROR",
                diagnostics=[
                    {"status": "ERROR", "phase": "FIXTURE_EXIT", "exception": outer}
                ],
            )
            value["stages"] = {
                "truncation_diagnostic": {
                    "original_test": original,
                    "boundary_observation": {
                        "inner": inner,
                        "outer": outer,
                        "fixture_cleanup": "CLOSED",
                        "fixture_descriptor_closed": True,
                        "fixture_stream_closed": True,
                        "fixture_owner_revoked": True,
                        "retained_owners_after_fixture": 0,
                        "observed_pattern_expected": True,
                    },
                }
            }
        return value

    def test_ordinary_closed_failure_is_valid_but_not_pass(self):
        self.assertEqual(
            session.validate_report(self.report(), self.request()), "CONTINUE"
        )
        self.assertEqual(
            session.validate_report(self.report("PASS"), self.request()), "COMPLETE"
        )
        self.assertEqual(
            session.validate_report(
                self.report("DIAGNOSTIC_COMPLETE"), self.request("DIAGNOSE_TRUNCATION")
            ),
            "CONTINUE",
        )

    def test_identity_isolation_signature_and_cleanup_failures_cannot_continue(self):
        for key, value in (
            ("source_sha", "d" * 40),
            ("workload_closed", False),
            ("network_isolation", {}),
            ("failure_category", "SIGNATURE"),
            ("failure_category", "PROTOCOL_MATERIAL"),
        ):
            report = self.report()
            report[key] = value
            with (
                self.subTest(key=key),
                self.assertRaises(session.h.CandidateHarnessError),
            ):
                session.validate_report(report, self.request())

    def test_missing_or_unsafe_stages_do_not_become_pass_or_continuation(self):
        for status in ("PASS", "FAIL"):
            for mutation in ("missing", "fault", "aborted", "retained", "diagnostics"):
                value = self.report(status)
                group = value["stages"]["seven_platform_tests"]
                if mutation == "missing":
                    value["stages"] = {}
                elif mutation == "fault":
                    group["projection_fault"] = True
                elif mutation == "aborted":
                    group["aborted"] = True
                elif mutation == "retained":
                    group["fixture_retained_owners"] = 1
                else:
                    group["records"][0]["diagnostics"] = [{"phase": "FIXTURE_EXIT"}]
                with (
                    self.subTest(status=status, mutation=mutation),
                    self.assertRaises(session.h.CandidateHarnessError),
                ):
                    session.validate_report(value, self.request())
        value = self.report("DIAGNOSTIC_COMPLETE")
        value["stages"]["truncation_diagnostic"]["boundary_observation"]["inner"][
            "chain"
        ][0]["code"] = "UNKNOWN"
        with self.assertRaisesRegex(
            session.h.CandidateHarnessError, "DIAGNOSTIC_UNSAFE"
        ):
            session.validate_report(value, self.request("DIAGNOSE_TRUNCATION"))

    def test_round_slot_is_exclusive_and_never_more_than_four(self):
        for ordinal in range(1, 5):
            self.assertEqual(self.limits.reserve_round({"mode": "FINAL_SET"}), ordinal)
        with self.assertRaisesRegex(session.h.CandidateHarnessError, "ROUND_LIMIT"):
            self.limits.reserve_round({"mode": "FINAL_SET"})
        self.assertEqual(len(list(self.root.glob("round-*.started.json"))), 4)

    def test_stop_and_absolute_deadline_revoke_without_new_workload(self):
        (self.root / "stop.json").write_bytes(b'{"operation":"STOP"}')
        with self.assertRaisesRegex(session.h.CandidateHarnessError, "REVOKED"):
            self.limits.reserve_round({})
        self.assertEqual(self.limits.rounds, 0)
        self.assertEqual(self.limits.reason, "STOP_REQUEST")
        limited = session.Limits(
            datetime.now(timezone.utc) - timedelta(seconds=1), self.root / "unused"
        )
        with self.assertRaisesRegex(session.h.CandidateHarnessError, "REVOKED"):
            limited.remaining()

    def test_unchanged_retry_and_material_change_rejected_before_execution(self):
        request = {
            "schema": "animemo.userspace-workload-request/v1",
            "ordinal": 2,
            "inputs": str(self.root / "input.json"),
            **self.request(),
        }
        with self.assertRaisesRegex(session.h.CandidateHarnessError, "UNCHANGED_RETRY"):
            session.validate_request(
                request,
                ordinal=2,
                parent=self.root,
                original_material={},
                previous=self.request(),
            )
        request["source_sha"] = "d" * 40
        with mock.patch.object(
            session, "read_inputs", return_value={"files": {}, "kit_source": {}}
        ):
            with self.assertRaisesRegex(
                session.h.CandidateHarnessError, "MATERIAL_REBOUND"
            ):
                session.validate_request(
                    request,
                    ordinal=2,
                    parent=self.root,
                    original_material={"files": {"changed": True}, "kit_source": {}},
                    previous=self.request(),
                )

    def test_lease_file_loss_is_rejected_even_if_memory_flag_is_open(self):
        path = self.root / "lease"
        path.write_bytes(b"")
        lease = SimpleNamespace(
            path=path,
            file_identity=session.h._file_identity(path.lstat()),
            require_open=lambda: None,
        )
        session.require_lease(lease)
        path.unlink()
        with self.assertRaisesRegex(session.h.CandidateHarnessError, "LEASE_CHANGED"):
            session.require_lease(lease)

    def test_fixed_mode_command_roundtrip_never_accepts_shell_selection(self):
        import shlex

        for mode in session.MODES:
            value = single.fixed_guest_command(
                stage="/tmp/animemo-userspace-" + "a" * 32,
                inventory_program="def closed_runtime_inventory_digest(path): return None",
                expected_digest="sha256:" + "a" * 64,
                mode=mode,
            )
            program = shlex.split(value)[-1]
            compile(program, "<fixed-session-command>", "exec")
            self.assertIn(repr(mode), program)
        with self.assertRaises(session.h.CandidateHarnessError):
            single.fixed_guest_command(
                stage="/tmp/animemo-userspace-" + "a" * 32,
                inventory_program="pass",
                expected_digest="sha256:" + "a" * 64,
                mode="sh -c arbitrary",
            )

    def test_mock_provider_cleanup_runs_once_after_ordinary_failure_and_second_result(
        self,
    ):
        from contextlib import contextmanager

        events = []
        expected = self.root / "owned-clone.vmx"
        provider = SimpleNamespace(
            _active_profile_authority=lambda *_: SimpleNamespace(clone_vmx=expected),
            _contain_clone=lambda _: events.append("original-containment"),
            _stop_clone=lambda _: events.append("soft-stop"),
        )

        @contextmanager
        def controller(*_):
            events.append("one-boot")
            try:
                yield ("profile", "lease", "disk", "snapshot")
            finally:
                provider._contain_clone(expected)
                events.append("lease-and-key-cleanup")

        with mock.patch.object(single, "_controller_clone", controller):
            with single.soft_stop_clone(
                provider, SimpleNamespace(profiles=[object()]), {}
            ):
                self.assertEqual(
                    session.validate_report(self.report(), self.request()), "CONTINUE"
                )
                events.append("first-source-closed")
                self.assertEqual(
                    session.validate_report(self.report("PASS"), self.request()),
                    "COMPLETE",
                )
        self.assertEqual(
            events,
            ["one-boot", "first-source-closed", "soft-stop", "lease-and-key-cleanup"],
        )

    def test_mock_provider_cancellation_still_soft_stops(self):
        from contextlib import contextmanager

        events = []
        expected = self.root / "owned-clone.vmx"
        provider = SimpleNamespace(
            _active_profile_authority=lambda *_: SimpleNamespace(clone_vmx=expected),
            _contain_clone=lambda _: None,
            _stop_clone=lambda _: events.append("soft-stop"),
        )

        @contextmanager
        def controller(*_):
            try:
                yield None
            finally:
                provider._contain_clone(expected)

        with mock.patch.object(single, "_controller_clone", controller):
            with self.assertRaises(KeyboardInterrupt):
                with single.soft_stop_clone(
                    provider, SimpleNamespace(profiles=[object()]), {}
                ):
                    raise KeyboardInterrupt()
        self.assertEqual(events, ["soft-stop"])

    def _run_actual_loop_with_fake_provider(
        self, *, cancel_after_first=False, invalid_inventory=False
    ):
        from contextlib import ExitStack, contextmanager, nullcontext

        events, sources = [], []
        source_root = self.root / "source"
        (source_root / "scripts").mkdir(parents=True)
        (source_root / "scripts/closed_runtime_inventory.py").write_text(
            "def broken(:"
            if invalid_inventory
            else "def closed_runtime_inventory_digest(path): return None",
            encoding="utf-8",
        )
        args = SimpleNamespace(
            authorization_id="ANIMEMO_TEST_USERSPACE_CLOSURE_DELTA_V1",
            authorization_expires_utc="2099-01-01T00:00:00Z",
            output=self.root / "final.json",
            execute=True,
            initial_mode="DIAGNOSE_TRUNCATION",
            inputs=self.root / "initial.json",
            inputs_sha256="sha256:" + "c" * 64,
            qualification_run_id=1,
            verified_candidate_digest="sha256:" + "d" * 64,
            candidate_state=self.root,
        )
        provider = SimpleNamespace(
            running=False,
            bind_candidate_material_authority=lambda _: nullcontext(),
            _active_profile_authority=lambda *_: object(),
            _verify_bootstrap_connection=lambda *_, **__: object(),
        )
        material = SimpleNamespace(
            loaded=SimpleNamespace(
                candidate_input={"source_sha": "e" * 40, "source_tree": "f" * 40}
            )
        )
        plan = SimpleNamespace(
            session_id="a" * 32, as_dict=lambda: {}, profiles=[object()]
        )

        @contextmanager
        def source_factory(_provider, *, source_sha, source_tree, userspace_inputs):
            self.assertTrue(all(source.closed for source in sources))
            source = SimpleNamespace(
                source_sha=source_sha,
                source_tree=source_tree,
                inventory_digest="sha256:" + "1" * 64,
                userspace_probe_digest="sha256:" + "2" * 64,
                root=source_root,
                require_open=lambda: self.assertFalse(source.closed),
                closed=False,
            )
            sources.append(source)
            events.append("source-open")
            try:
                yield source
            finally:
                source.closed = True
                events.append("source-close")

        @contextmanager
        def vm_owner(*_):
            events.append("one-boot")
            provider.running = True
            try:
                yield object(), object(), "disk", "snapshot"
            finally:
                provider.running = False
                events.append("one-soft-stop")
                events.append("lease-keys-cleaned")

        def payload(
            _provider,
            authority,
            verified,
            lease,
            source,
            request,
            limits,
            session_id,
            initial_command_digest,
        ):
            self.assertTrue(provider.running)
            self.assertFalse(source.closed)
            ordinal = limits.reserve_round({"mode": request["mode"]})
            _, _, command_digest = session.prepared_payload_command(
                source, request["mode"], session_id, ordinal
            )
            if ordinal == 1:
                self.assertEqual(command_digest, initial_command_digest)
            value = self.report("DIAGNOSTIC_COMPLETE" if ordinal == 1 else "PASS")
            value.update(
                source_sha=request["source_sha"],
                source_tree=request["source_tree"],
                input_manifest_sha256=request["inputs_sha256"],
            )
            decision = session.validate_report(value, request)
            events.append("workload")
            return {"ordinal": ordinal, "decision": decision, "guest": value}

        def following(*_, **__):
            self.assertTrue(provider.running)
            self.assertTrue(sources[-1].closed)
            events.append("wait-after-source-close")
            if cancel_after_first:
                raise KeyboardInterrupt()
            return {
                "mode": "FINAL_SET",
                "source_sha": "3" * 40,
                "source_tree": "4" * 40,
                "inputs": str(self.root / "next.json"),
                "inputs_sha256": "sha256:" + "5" * 64,
            }

        batch = self.root / "batch"

        def reserve(*_):
            batch.mkdir()
            return {"fixture": True}

        with ExitStack() as stack:
            patches = [
                mock.patch.object(
                    session.h, "ClosedVmwareProvider", return_value=provider
                ),
                mock.patch.object(
                    session, "supervised_commands", return_value=nullcontext()
                ),
                mock.patch.object(
                    single, "execution_scope", return_value=nullcontext()
                ),
                mock.patch.object(
                    session.h,
                    "acquire_candidate_material_authority",
                    return_value=nullcontext(material),
                ),
                mock.patch.object(
                    session, "read_inputs", return_value={"files": {}, "kit_source": {}}
                ),
                mock.patch.object(
                    session, "acquire_development_source", side_effect=source_factory
                ),
                mock.patch.object(
                    session.h, "build_harness_plan", return_value=object()
                ),
                mock.patch.object(session, "from_material_plan", return_value=plan),
                mock.patch.object(
                    session, "controller_files", return_value={"fixed": "pin"}
                ),
                mock.patch.object(session, "check_controller"),
                mock.patch.object(session, "authorization_root", return_value=batch),
                mock.patch.object(single, "reserve_one_clone", side_effect=reserve),
                mock.patch.object(single, "soft_stop_clone", side_effect=vm_owner),
                mock.patch.object(session, "require_guest"),
                mock.patch.object(session, "run_payload", side_effect=payload),
                mock.patch.object(session, "next_request", side_effect=following),
                mock.patch.object(
                    session.subprocess,
                    "check_output",
                    side_effect=[b"a" * 40, b"b" * 40],
                ),
            ]
            for patch in patches:
                stack.enter_context(patch)
            if invalid_inventory:
                with self.assertRaisesRegex(
                    session.h.CandidateHarnessError, "GENERATION_INVALID"
                ):
                    session.run(args)
                value = None
            elif cancel_after_first:
                with self.assertRaises(KeyboardInterrupt):
                    session.run(args)
                value = None
            else:
                value = session.run(args)
        self.assertTrue(all(source.closed for source in sources))
        self.assertFalse(provider.running)
        self.assertEqual(events.count("one-boot"), 0 if invalid_inventory else 1)
        self.assertEqual(events.count("one-soft-stop"), 0 if invalid_inventory else 1)
        if invalid_inventory:
            self.assertFalse(batch.exists())
        self.assertTrue(args.output.is_file())
        return value, events

    def test_actual_run_loop_rebinds_two_sources_in_one_vm_context(self):
        value, events = self._run_actual_loop_with_fake_provider()
        self.assertEqual(value["status"], "PASS")
        self.assertEqual(value["workload_invocations"], 2)
        self.assertEqual(
            events,
            [
                "source-open",
                "one-boot",
                "workload",
                "source-close",
                "wait-after-source-close",
                "source-open",
                "workload",
                "source-close",
                "one-soft-stop",
                "lease-keys-cleaned",
            ],
        )

    def test_actual_run_loop_cancellation_after_first_round_closes_vm(self):
        _, events = self._run_actual_loop_with_fake_provider(cancel_after_first=True)
        self.assertEqual(events.count("source-open"), 1)
        self.assertEqual(events[-2:], ["one-soft-stop", "lease-keys-cleaned"])

    def test_actual_run_rejects_invalid_inventory_before_clone_reservation_or_boot(
        self,
    ):
        _, events = self._run_actual_loop_with_fake_provider(invalid_inventory=True)
        self.assertEqual(events, ["source-open", "source-close"])


class UserspaceSessionFixedChildTests(unittest.TestCase):
    """Runner permits only these two exact local Python argv via its audit guard."""

    def test_fixed_real_child_exit(self):
        event = threading.Event()
        result = session.h.SubprocessHostCommandRunner().run(
            (sys.executable, "-I", "-S", "-B", "-c", "print('FIXED_LOCAL_CHILD')"),
            environment={"SYSTEMROOT": "C:/Windows"},
            cwd=Path(sys.executable).parent,
            timeout=10,
            cancel_event=event,
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), b"FIXED_LOCAL_CHILD")

    def test_fixed_real_child_timeout_is_terminal(self):
        with self.assertRaises(subprocess.TimeoutExpired):
            session.h.SubprocessHostCommandRunner().run(
                (sys.executable, "-I", "-S", "-B", "-c", "import time; time.sleep(30)"),
                environment={"SYSTEMROOT": "C:/Windows"},
                cwd=Path(sys.executable).parent,
                timeout=0.2,
                cancel_event=threading.Event(),
            )

    def test_fixed_real_child_cancellation_is_terminal(self):
        event = threading.Event()
        timer = threading.Timer(0.2, event.set)
        timer.start()
        try:
            with self.assertRaisesRegex(session.h.CandidateHarnessError, "REVOKED"):
                session.h.SubprocessHostCommandRunner().run(
                    (
                        sys.executable,
                        "-I",
                        "-S",
                        "-B",
                        "-c",
                        "import time; time.sleep(30)",
                    ),
                    environment={"SYSTEMROOT": "C:/Windows"},
                    cwd=Path(sys.executable).parent,
                    timeout=10,
                    cancel_event=event,
                )
        finally:
            timer.join(timeout=2)
        self.assertFalse(timer.is_alive())


if __name__ == "__main__":
    unittest.main()
