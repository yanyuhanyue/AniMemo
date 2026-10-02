"""Real Formal error translation and bounded host framing; no Guest/authority."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from scripts import candidate_guest_session as session
from scripts import candidate_diagnostics as diagnostic

OPERATION = "sha256:" + "d" * 64
ROOT = Path(__file__).resolve().parents[2]
SENTINEL = "PRIVATE_EXCEPTION_TEXT_MUST_NOT_ESCAPE"


def actual_exception(*, wrapper=False, cycle=False):
    namespace = {"__name__": "installer.bootstrap", "sentinel": SENTINEL}
    exec(
        compile(
            "def fail():\n raise ValueError(sentinel)\n",
            "X:/private-path/installer/bootstrap.py",
            "exec",
        ),
        namespace,
    )
    try:
        namespace["fail"]()
    except ValueError as error:
        if not wrapper:
            return error
        try:
            raise RuntimeError(SENTINEL) from error
        except RuntimeError as wrapped:
            if cycle:
                error.__context__ = wrapped
            return wrapped


def collect(stream):
    stream.seek(0)
    reader = diagnostic.DiagnosticReader(OPERATION)
    while item := diagnostic.read_frame(stream):
        reader.accept(*item)
    reader.finish()
    return reader


class FaultProtocolTests(unittest.TestCase):
    def test_wrapper_cycles_unknown_type_and_secret_redaction(self):
        for wrapper, cycle in ((False, False), (True, False), (True, True)):
            with (
                self.subTest(wrapper=wrapper, cycle=cycle),
                tempfile.TemporaryFile() as stream,
            ):
                writer = diagnostic.DiagnosticWriter(stream.fileno(), OPERATION)
                with mock.patch.object(
                    diagnostic, "inherited_writer", return_value=writer
                ):
                    self.assertEqual(
                        diagnostic.best_effort_fault(
                            actual_exception(wrapper=wrapper, cycle=cycle)
                        ),
                        "COMPLETE",
                    )
                reader = collect(stream)
                self.assertEqual(
                    reader.public()["failure_diagnostic"]["status"], "COMPLETE"
                )
                self.assertLessEqual(
                    reader.public()["failure_diagnostic"]["locations"], 3
                )
                serialized = json.dumps(reader.public())
                for private in (SENTINEL, "private-path", "X:/"):
                    self.assertNotIn(private, serialized)

    def test_absent_writer_and_no_projectable_traceback_are_explicit(self):
        with mock.patch.object(diagnostic, "inherited_writer", return_value=None):
            self.assertEqual(
                diagnostic.best_effort_fault(ValueError(SENTINEL)), "UNAVAILABLE"
            )
        with tempfile.TemporaryFile() as stream:
            writer = diagnostic.DiagnosticWriter(stream.fileno(), OPERATION)
            with mock.patch.object(diagnostic, "inherited_writer", return_value=writer):
                self.assertEqual(
                    diagnostic.best_effort_fault(ValueError(SENTINEL)), "NO_LOCATION"
                )
            reader = collect(stream)
            self.assertEqual(
                reader.public()["failure_diagnostic"]["status"], "INCOMPLETE"
            )
            self.assertEqual(reader.error_code, "FAILURE_DIAGNOSTIC_INCOMPLETE")
            self.assertEqual(reader.public()["errors"], ["RUNNER_EXECUTION_FAILED"])

    def test_short_writes_drain_and_zero_bad_or_broken_fd_never_raise(self):
        original_write = os.write
        with tempfile.TemporaryFile() as stream:
            writer = diagnostic.DiagnosticWriter(stream.fileno(), OPERATION)
            with (
                mock.patch.object(diagnostic, "inherited_writer", return_value=writer),
                mock.patch.object(
                    diagnostic.os,
                    "write",
                    side_effect=lambda fd, data: original_write(fd, data[:7]),
                ),
            ):
                self.assertEqual(
                    diagnostic.best_effort_fault(actual_exception()), "COMPLETE"
                )
            self.assertEqual(
                collect(stream).public()["failure_diagnostic"]["status"], "COMPLETE"
            )
        for failure in (0, OSError(SENTINEL), BrokenPipeError(SENTINEL)):
            with (
                self.subTest(failure=type(failure).__name__),
                mock.patch.object(
                    diagnostic,
                    "inherited_writer",
                    return_value=diagnostic.DiagnosticWriter(999997, OPERATION),
                ),
                mock.patch.object(
                    diagnostic.os,
                    "write",
                    side_effect=failure if isinstance(failure, Exception) else None,
                    return_value=failure,
                ),
            ):
                self.assertEqual(
                    diagnostic.best_effort_fault(actual_exception()), "WRITE_FAILED"
                )

    def test_interrupted_projection_preserves_actual_exit_and_revokes(self):
        with tempfile.TemporaryFile() as stream:
            writer = diagnostic.DiagnosticWriter(stream.fileno(), OPERATION)
            writer.stage("ROOT_STARTED")
            writer.error("RUNNER_EXECUTION_FAILED")
            writer.event("FAULT_BEGIN")
            # A child loses its fd; the fixed parent still observes real exits.
            for component in ("RUNTIME_RUNNER", "ROOT", "SUDO"):
                writer.exited(component, 2)
            stream.seek(0)
            provider = SimpleNamespace(_candidate_diagnostics={})
            with self.assertRaises(session.WorkloadFailure) as caught:
                session._read_receipt(
                    SimpleNamespace(stdout=stream),
                    operation=OPERATION,
                    provider=provider,
                    profile=SimpleNamespace(profile="FRESH_BASE"),
                    timeout=2,
                )
            self.assertTrue(caught.exception.revoke_batch)
            value = provider._candidate_diagnostics["FRESH_BASE"]
            self.assertEqual(value["failure_diagnostic"]["status"], "INCOMPLETE")
            self.assertEqual(value["exit_codes"]["RUNTIME_RUNNER"], 2)
            self.assertIsNone(value["exit_codes"]["INSTALLER"])

    def test_unclosed_projection_at_eof_is_not_a_legacy_failure(self):
        with tempfile.TemporaryFile() as stream:
            writer = diagnostic.DiagnosticWriter(stream.fileno(), OPERATION)
            writer.error("RUNNER_EXECUTION_FAILED")
            writer.event("FAULT_BEGIN")
            reader = collect(stream)
            self.assertEqual(reader.error_code, "FAILURE_DIAGNOSTIC_INCOMPLETE")

    def test_invalid_footer_duplicate_frames_binding_and_limits_reject(self):
        common = dict(schema=diagnostic.SCHEMA, operation=OPERATION)
        fault = dict(
            common,
            kind="FAULT",
            module="installer.bootstrap",
            line=2,
            category="ValueError",
        )
        begin = dict(common, kind="FAULT_BEGIN")
        error = dict(common, kind="ERROR", code="RUNNER_EXECUTION_FAILED")
        end = dict(common, kind="FAULT_END", count=1, status="COMPLETE")
        sequences = (
            [end],
            [error, begin, fault, fault],
            [error, begin, end],
            [error, begin, fault, end, end],
            [error, begin, fault, end, fault],
            [error, begin, {**fault, "operation": "sha256:" + "e" * 64}],
            [error, begin, fault, {**end, "count": True}],
            [error, begin, {**end, "count": 0}],
            [error, begin, {**end, "count": 0, "status": SENTINEL}],
        )
        for items in sequences:
            with (
                self.subTest(kinds=[e["kind"] for e in items]),
                self.assertRaises(diagnostic.DiagnosticError),
            ):
                reader = diagnostic.DiagnosticReader(OPERATION)
                for item in items:
                    reader.accept(b"D", json.dumps(item).encode())
        for value in (0, 7, True, "3"):
            with self.assertRaises(diagnostic.DiagnosticError):
                diagnostic.DiagnosticWriter(0, OPERATION).fault(
                    actual_exception(), maximum_locations=value
                )

    def test_conservative_late_docker_failure_fits_unchanged_budgets(self):
        # 16 stages through DRAFT_WRITING, five APT records, six inner fault
        # sites, a Doctor event, outer three-site report, errors and four exits.
        stream_fields = dict(
            bytes_seen=2**63 - 1,
            truncated=True,
            missing=False,
            categories=list(diagnostic.APT_CATEGORIES[:-2]),
            hosts=list(diagnostic.APT_HOSTS),
            indexes=list(diagnostic.APT_INDEXES),
        )
        apt = dict(
            operation_class="INSTALL",
            tool="/usr/bin/apt-get",
            tool_version="2.8.3",
            argv_contract=OPERATION,
            started_at="2026-09-20T00:00:00Z",
            ended_at="2026-09-20T00:01:00Z",
            returncode=100,
            outcome="EXITED",
            categories=list(diagnostic.APT_CATEGORIES[:-2]),
            stdout=stream_fields,
            stderr=stream_fields,
            secondary_errors=list(diagnostic.APT_SECONDARY_ERRORS),
        )
        with tempfile.TemporaryFile() as stream:
            writer = diagnostic.DiagnosticWriter(stream.fileno(), OPERATION)
            for stage in diagnostic.STAGES[:16]:
                writer.stage(stage)
            for _ in range(5):
                writer.event("APT", observation=apt)
            for line in range(6):
                writer.event(
                    "FAULT",
                    module="installer.production",
                    line=line + 1,
                    category="OTHER",
                )
            writer.event("DOCTOR", failed_checks=list(diagnostic.DOCTOR_CHECKS))
            writer.error("RUNNER_EXECUTION_FAILED")
            writer.event("FAULT_BEGIN")
            for line in range(3):
                writer.event(
                    "FAULT",
                    module="scripts.formal_profile_runner",
                    line=line + 1,
                    category="OTHER",
                )
            writer.event("FAULT_END", count=3, status="COMPLETE")
            writer.error("RUNNER_EXECUTION_FAILED")
            writer.error("ROOT_EXECUTION_FAILED")
            for component in diagnostic.COMPONENTS:
                writer.exited(component, 2)
            reader = collect(stream)
            self.assertEqual(len(reader.events), 40)
            self.assertLessEqual(reader.diagnostic_bytes, 16 * 1024)
            self.assertEqual(
                (diagnostic.MAX_EVENTS, diagnostic.MAX_DIAGNOSTIC_BYTES),
                (40, 16 * 1024),
            )
            with self.assertRaisesRegex(
                diagnostic.DiagnosticError, "TRANSPORT_LIMIT_EXCEEDED"
            ):
                reader.accept(
                    b"D",
                    json.dumps(
                        dict(
                            schema=diagnostic.SCHEMA,
                            operation=OPERATION,
                            kind="ERROR",
                            code="ROOT_EXECUTION_FAILED",
                        )
                    ).encode(),
                )

    @unittest.skipUnless(
        os.name == "posix",
        "Requires real POSIX pass_fds; run on isolated Linux before credential use",
    )
    def test_real_dedicated_inherited_fd_through_child(self):
        read_fd, write_fd = os.pipe()
        program = (
            "import os,sys;sys.path.insert(0," + repr(str(ROOT)) + ");"
            "from scripts.candidate_diagnostics import best_effort_fault;"
            "from scripts.tests.test_formal_failure_diagnostics import actual_exception;"
            'raise SystemExit(2 if best_effort_fault(actual_exception())=="COMPLETE" else 9)'
        )
        environment = {
            **os.environ,
            diagnostic.FD_ENV: str(write_fd),
            diagnostic.OP_ENV: OPERATION,
        }
        process = subprocess.Popen(
            [sys.executable, "-B", "-c", program],
            env=environment,
            pass_fds=(write_fd,),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        os.close(write_fd)
        with os.fdopen(read_fd, "rb") as pipe:
            reader = diagnostic.DiagnosticReader(OPERATION)
            while item := diagnostic.read_frame(pipe):
                reader.accept(*item)
        self.assertEqual(process.wait(timeout=10), 2)
        self.assertEqual(reader.public()["failure_diagnostic"]["status"], "COMPLETE")


class FormalFailureDiagnosticTests(unittest.TestCase):
    def test_actual_formal_file_failure_reaches_host_projection(self):
        with tempfile.TemporaryDirectory() as directory:
            # Missing authority file is injected at the real filesystem seam.
            # No test executor supplies a verified authority or a PASS receipt.
            program = (
                "import os,sys;sys.path.insert(0," + repr(str(ROOT)) + ");"
                "from scripts.candidate_diagnostics import DiagnosticWriter,FD_ENV,OP_ENV;"
                "from scripts.formal_profile_runner import main;"
                'os.environ[FD_ENV]="1";os.environ[OP_ENV]=' + repr(OPERATION) + ";"
                "w=DiagnosticWriter(1," + repr(OPERATION) + ");"
                'w.stage("ROOT_STARTED");w.stage("RUNTIME_READY");w.stage("RUNNER_STARTED");'
                'code=main(["--authority-root",'
                + repr(directory)
                + ',"--profile","FORMAL_FRESH","--execute"]);'
                'w.exited("RUNTIME_RUNNER",code);w.error("RUNNER_EXECUTION_FAILED");'
                'w.error("ROOT_EXECUTION_FAILED");w.exited("ROOT",code);w.exited("SUDO",code);'
                "raise SystemExit(code)"
            )
            process = subprocess.Popen(
                [sys.executable, "-B", "-c", program],
                cwd=ROOT,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
            provider = SimpleNamespace(_candidate_diagnostics={})
            try:
                with self.assertRaises(session.WorkloadFailure) as caught:
                    session._read_receipt(
                        process,
                        operation=OPERATION,
                        provider=provider,
                        profile=SimpleNamespace(profile="FRESH_BASE"),
                        timeout=20,
                    )
                self.assertTrue(caught.exception.revoke_batch)
                self.assertEqual(process.wait(timeout=10), 2)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=10)
                process.stdout.close()
            public = provider._candidate_diagnostics["FRESH_BASE"]
            self.assertEqual(public["failure_diagnostic"]["status"], "COMPLETE")
            self.assertTrue(
                any(
                    e["kind"] == "FAULT"
                    and e["module"] == "scripts.formal_profile_runner"
                    for e in public["events"]
                )
            )
            self.assertIsNone(public["exit_codes"]["INSTALLER"])
            self.assertFalse(public["profile_draft_received"])
            self.assertNotIn(directory, json.dumps(public))


if __name__ == "__main__":
    unittest.main()
