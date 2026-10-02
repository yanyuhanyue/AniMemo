"""No Provider execution, credentials, VM, or network: DEV userspace boundaries."""

import copy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from scripts import development_userspace_inputs as inputs
from scripts import development_userspace_probe as host
from scripts import development_source
from scripts import userspace_platform_probe as guest
from scripts.development_plan import from_material_plan
from scripts.tests import test_candidate_vm_harness as fixtures
from scripts.tests.userspace_effect_guard import UserspaceEffectGuard


class UserspaceProbeTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.args = SimpleNamespace(
            authorization_id="ANIMEMO_TEST_USERSPACE_PLATFORM_VALIDATION_V1",
            authorization_expires_utc="2099-01-01T00:00:00Z",
            output=self.root / "result.json",
            execute=False,
        )

    def record(self):
        return {
            "schema": "animemo.development-userspace-inputs/v1",
            "purpose": inputs.PURPOSE,
            "source_sha": "a" * 40,
            "source_tree": "b" * 40,
            "test_ids": list(inputs.TEST_IDS),
            "files": {
                role: {
                    "path": str(self.root / role),
                    "size": 134010880 if role == "original_archive" else 1,
                    "sha256": inputs.ORIGINAL
                    if role == "original_archive"
                    else "sha256:" + "0" * 64,
                }
                for role in inputs.ROLES
            },
        }

    def load(self, value, pin=None):
        path = self.root / "inputs.json"
        raw = inputs.canonical(value)
        path.write_bytes(raw)
        return inputs.read_inputs(
            path, pin or inputs.identity(raw), source_sha="a" * 40, source_tree="b" * 40
        )

    def test_fixed_seven_ids_are_parsed_without_ambient_provider(self):
        with mock.patch.object(host.h, "ClosedVmwareProvider") as provider:
            self.assertEqual(
                self.load(self.record())["test_ids"], list(inputs.TEST_IDS)
            )
        provider.assert_not_called()

    def test_foreign_source_scope_and_arbitrary_test_are_rejected(self):
        for field, value in [
            ("source_sha", "c" * 40),
            ("source_tree", "d" * 40),
            ("purpose", "FORMAL_POSTPUBLICATION"),
            ("test_ids", ["os.system"]),
        ]:
            record = self.record()
            record[field] = value
            with (
                self.subTest(field=field),
                self.assertRaises(host.h.CandidateHarnessError),
            ):
                self.load(record)

    def test_archive_size_digest_or_unlisted_role_never_accepted(self):
        cases = []
        record = self.record()
        record["files"]["original_archive"]["size"] -= 1
        cases.append(record)
        record = self.record()
        record["files"]["original_archive"]["sha256"] = "sha256:" + "f" * 64
        cases.append(record)
        record = self.record()
        record["files"]["command"] = {
            "path": "/bin/sh",
            "size": 1,
            "sha256": "sha256:" + "1" * 64,
        }
        cases.append(record)
        for record in cases:
            with (
                self.subTest(record=record),
                self.assertRaises(host.h.CandidateHarnessError),
            ):
                self.load(record)

    def test_input_pin_and_relative_path_rejected(self):
        with self.assertRaises(host.h.CandidateHarnessError):
            self.load(self.record(), pin="sha256:" + "f" * 64)
        record = self.record()
        record["files"]["kit_entry"]["path"] = "relative.pyz"
        with self.assertRaises(host.h.CandidateHarnessError):
            self.load(record)

    def test_old_scope_and_expiry_fail_before_provider(self):
        for scope, expiry in [
            ("ANIMEMO_V2_RC3_FORMAL_POSTPUBLICATION_V1", "2099-01-01T00:00:00Z"),
            (self.args.authorization_id, "2000-01-01T00:00:00Z"),
        ]:
            args = copy.copy(self.args)
            args.authorization_id = scope
            args.authorization_expires_utc = expiry
            with (
                mock.patch.object(host.h, "ClosedVmwareProvider") as provider,
                self.assertRaises(host.h.CandidateHarnessError),
            ):
                host.run(args)
            provider.assert_not_called()

    def test_existing_output_fails_before_provider(self):
        self.args.output.write_text("preserve")
        with (
            mock.patch.object(host.h, "ClosedVmwareProvider") as provider,
            self.assertRaises(host.h.CandidateHarnessError),
        ):
            host.run(self.args)
        provider.assert_not_called()
        self.assertEqual(self.args.output.read_text(), "preserve")

    def test_input_modes_are_exclusive_before_any_snapshot(self):
        with mock.patch.object(
            development_source, "create_windows_private_directory"
        ) as create:
            with self.assertRaises(host.h.CandidateHarnessError):
                with development_source.acquire_development_source(
                    None,
                    source_sha="a" * 40,
                    source_tree="b" * 40,
                    userspace_inputs={},
                    attestation_probe_inputs={},
                ):
                    self.fail("conflicting factory yielded")
            create.assert_not_called()

    def test_userspace_plan_has_one_fresh_and_distinct_digest_no_capture(self):
        fixture = fixtures.CandidateVmHarnessTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        base = fixture._plan()
        plan = from_material_plan(
            base,
            execution_source_sha="a" * 40,
            execution_source_tree="b" * 40,
            execution_inventory_digest="sha256:" + "c" * 64,
            userspace_probe_digest="sha256:" + "d" * 64,
        )
        body = plan.identity_body()
        self.assertEqual([p.profile for p in plan.profiles], ["FRESH_BASE"])
        self.assertEqual(body["developmentMode"], inputs.PURPOSE)
        self.assertEqual(body["sudoCaptureLimit"], 0)
        self.assertFalse(body["candidateAcceptanceAuthorityGranted"])
        self.assertNotEqual(plan.plan_digest, base.plan_digest)
        with self.assertRaises(host.h.CandidateHarnessError):
            from_material_plan(
                base,
                execution_source_sha="a" * 40,
                execution_source_tree="b" * 40,
                execution_inventory_digest="sha256:" + "c" * 64,
                platform_diagnostic=True,
                userspace_probe_digest="sha256:" + "d" * 64,
            )

    def test_fixed_command_rejects_shell_stage_and_unbound_inventory(self):
        for stage, digest in [
            ("/tmp/x;sudo true", "sha256:" + "1" * 64),
            ("/tmp/animemo-userspace-" + "a" * 32, "unknown"),
        ]:
            with self.assertRaises(host.h.CandidateHarnessError):
                host.fixed_guest_command(
                    stage=stage, inventory_program="pass", expected_digest=digest
                )
        command = host.fixed_guest_command(
            stage="/tmp/animemo-userspace-" + "a" * 32,
            inventory_program="def closed_runtime_inventory_digest(root): return None",
            expected_digest="sha256:" + "1" * 64,
        )
        self.assertTrue(command.startswith("/usr/bin/python3 -I -S -B -c "))
        self.assertIn("userspace_platform_probe.py", command)
        self.assertNotIn("sudo", command)

    def test_seccomp_policy_denies_native_network_and_x32_but_allows_files_and_pipe(
        self,
    ):
        def run(nr, arch=0xC000003E):
            accumulator = 0
            pc = 0
            program = guest.seccomp_instructions()
            for _ in range(100):
                code, jt, jf, k = program[pc]
                if code == 0x20:
                    accumulator = arch if k == 4 else nr
                    pc += 1
                elif code == 0x15:
                    pc += 1 + (jt if accumulator == k else jf)
                elif code == 0x45:
                    pc += 1 + (jt if accumulator & k else jf)
                elif code == 0x06:
                    return k
                else:
                    self.fail("unknown BPF instruction")
            self.fail("unterminated filter")

        for syscall in (41, 42, 44, 46, 53, 288, 307, 425, 426, 427, 438, 0x40000029):
            self.assertEqual(run(syscall), 0x00050001)
        for syscall in (0, 1, 2, 3, 22, 59, 60, 61, 62, 293):
            self.assertEqual(run(syscall), 0x7FFF0000)
        self.assertEqual(run(41, arch=0x40000003), 0x80000000)

    def test_soft_stop_failure_cannot_fall_back_to_suspend_or_other_vm(self):
        from contextlib import contextmanager

        expected = self.root / "new-clone.vmx"
        provider = SimpleNamespace(
            _active_profile_authority=lambda *a: SimpleNamespace(clone_vmx=expected),
            _contain_clone=mock.Mock(),
            _stop_clone=mock.Mock(
                side_effect=host.h.CandidateHarnessError("STOP_FAILED")
            ),
        )
        original = provider._contain_clone

        @contextmanager
        def controller(*_):
            try:
                yield ("fixed-profile", "lease", "disk", "snapshot")
            finally:
                provider._contain_clone(expected)

        with (
            mock.patch.object(host, "_controller_clone", controller),
            self.assertRaisesRegex(host.h.CandidateHarnessError, "STOP_FAILED"),
        ):
            with host.soft_stop_clone(
                provider, SimpleNamespace(profiles=[object()]), {}
            ):
                pass
        provider._stop_clone.assert_called_once_with(expected)
        original.assert_not_called()
        self.assertIs(provider._contain_clone, original)

    def test_failed_fixed_test_preserves_safe_ids_before_rejection(self):
        report = {"stages": {}}
        safe = {
            "tests": list(inputs.EXTRA_TEST_IDS),
            "run": 1,
            "failures": 1,
            "errors": 0,
            "skips": [],
            "successful": False,
            "records": [
                {"id": inputs.EXTRA_TEST_IDS[0], "status": "FAIL", "diagnostics": []}
            ],
            "projection_overflow": False,
            "projection_fault": False,
            "aborted": False,
            "effect_violation": False,
            "fixture_retained_owners": 0,
        }
        with (
            mock.patch.object(guest, "_run_python", return_value=safe),
        ):
            passed = guest._run_tests(
                report,
                "posix_pass_fds",
                inputs.EXTRA_TEST_IDS,
                root=self.root,
                vendor=self.root,
                work=self.root,
            )
        self.assertFalse(passed)
        self.assertEqual(report["stages"]["posix_pass_fds"], safe)
        self.assertEqual(report["phase"], "posix_pass_fds")

    def test_diagnostic_delivery_preserves_phase_without_raw_exception(self):
        def fail(report):
            report["phase"] = "KIT_IMPORTS"
            report["stages"]["seven_platform_tests"] = {"successful": True}
            raise OSError("private-sentinel")

        with mock.patch.object(guest, "run", side_effect=fail):
            report = guest.execute_report()
        self.assertEqual(report["status"], "FAIL")
        self.assertEqual(report["phase"], "KIT_IMPORTS")
        self.assertEqual(report["failure_code"], "DEVELOPMENT_USERSPACE_FAILED")
        self.assertIn("seven_platform_tests", report["stages"])
        self.assertNotIn("private-sentinel", str(report))

    def test_closed_failed_test_child_returns_projection_but_timeout_is_rejected(self):
        import json

        result = SimpleNamespace(
            outcome="EXITED",
            returncode=2,
            secondary_errors=(),
            stdout_summary={"truncated": False, "missing": False},
            stderr_summary={"truncated": False, "missing": False},
            stdout=json.dumps({"successful": False}),
            stderr="",
        )
        with mock.patch(
            "installer.apt_diagnostics.capture_process", return_value=result
        ):
            self.assertEqual(
                guest._run_python(
                    "fixed",
                    root=self.root,
                    vendor=self.root,
                    work=self.root,
                    test_result=True,
                ),
                {"successful": False},
            )
            result.outcome = "TIMEOUT"
            with self.assertRaisesRegex(ValueError, "CHILD_FAILED"):
                guest._run_python(
                    "fixed",
                    root=self.root,
                    vendor=self.root,
                    work=self.root,
                    test_result=True,
                )

    def test_isolation_child_compiles_complete_python_command_without_execution(self):
        result = SimpleNamespace(
            outcome="EXITED",
            returncode=0,
            secondary_errors=(),
            stdout_summary={"truncated": False, "missing": False},
            stderr_summary={"truncated": False, "missing": False},
            stdout=b'{"seccomp_inherited":true,"socket_denied":true}',
            stderr=b"",
        )

        def compile_command(argv, **kwargs):
            self.assertEqual(argv[1:5], ("-I", "-S", "-B", "-c"))
            self.assertTrue(argv[-1].startswith("import sys;sys.path[:0]="))
            compile(argv[-1], "<fixed-isolation-child>", "exec")
            return result

        with mock.patch(
            "installer.apt_diagnostics.capture_process", side_effect=compile_command
        ) as capture:
            guest._run_python(
                guest._network_isolation_child_program(),
                root=self.root,
                vendor=self.root / "vendor",
                work=self.root,
                timeout=15,
            )
        capture.assert_called_once()

    def test_retained_vm_scope_checks_bootstrap_copy_removed_separately(self):
        from contextlib import contextmanager

        key = self.root / "private-copy"
        key.write_bytes(b"test fixture")

        @contextmanager
        def authority(**kwargs):
            self.assertTrue(kwargs["_retain_controller_data"])
            try:
                yield
            finally:
                key.unlink()

        provider = SimpleNamespace(
            execution_authority=authority,
            _execution=SimpleNamespace(bootstrap_identity=key),
        )
        result = {}
        with host.execution_scope(provider, retain_vm=True, result=result):
            pass
        self.assertEqual(
            result["controller_cleanup"],
            {"bootstrap_copy_removed": True, "vm_evidence_retained": True},
        )

    def test_actual_fixed_program_records_subtest_failure_using_requested_id(self):
        import io
        import json
        from contextlib import redirect_stdout

        class FailingSubtest(unittest.TestCase):
            def runTest(self):
                with self.subTest(local="not projected"):
                    self.fail("private-sentinel")

        output = io.StringIO()
        with (
            mock.patch.object(
                guest,
                "_test_guard_program",
                return_value="from scripts.tests.userspace_effect_guard import UserspaceEffectGuard;guard=UserspaceEffectGuard()",
            ),
            mock.patch.object(
                unittest.defaultTestLoader,
                "loadTestsFromName",
                return_value=FailingSubtest(),
            ),
            redirect_stdout(output),
        ):
            with self.assertRaises(SystemExit) as stopped:
                exec(guest._test_program(inputs.EXTRA_TEST_IDS), {})
        self.assertEqual(stopped.exception.code, 2)
        report = json.loads(output.getvalue())
        self.assertEqual(report["records"][0]["id"], inputs.EXTRA_TEST_IDS[0])
        self.assertEqual(report["records"][0]["status"], "FAIL")
        self.assertEqual(
            report["records"][0]["diagnostics"][0]["exception"]["chain"][0]["type"],
            "AssertionError",
        )
        self.assertNotIn("private-sentinel", output.getvalue())

    def test_tests_are_source_constant_not_cli_selected(self):
        program = guest._test_program(inputs.TEST_IDS)
        self.assertEqual(len(inputs.TEST_IDS), 7)
        for name in inputs.TEST_IDS:
            self.assertIn(name, program)
        self.assertIn("not result['skips']", program)

    def test_delta_scope_is_semantic_and_does_not_allow_formal_or_arbitrary_scopes(
        self,
    ):
        self.args.authorization_id = "ANIMEMO_TEST_FIXED_USERSPACE_DELTA_V1"
        host.validate_entry(self.args)
        for scope in ("ANIMEMO_TEST_FORMAL_DELTA_V1", "ANIMEMO_TEST_USERSPACE_EXEC_V1"):
            self.args.authorization_id = scope
            with self.assertRaises(host.h.CandidateHarnessError):
                host.validate_entry(self.args)

    def test_v2_separates_kit_source_and_rejects_missing_extra_or_malformed_identity(
        self,
    ):
        record = self.record()
        record.update(
            schema="animemo.development-userspace-inputs/v2",
            kit_source={"commit": "c" * 40, "tree": "d" * 40},
        )
        self.assertEqual(self.load(record)["kit_source"], record["kit_source"])
        for replacement in (
            None,
            {"commit": "bad", "tree": "d" * 40},
            {"commit": "c" * 40, "tree": "d" * 40, "trusted": True},
        ):
            rejected = copy.deepcopy(record)
            rejected["kit_source"] = replacement
            with self.assertRaises(host.h.CandidateHarnessError):
                self.load(rejected)
        del record["kit_source"]
        with self.assertRaises(host.h.CandidateHarnessError):
            self.load(record)

    def test_kit_compatibility_checks_actual_each_library_byte(self):
        kit_root, current = self.root / "kit", self.root / "current"
        old = kit_root / "library" / "installer" / "example.py"
        new = current / "installer" / "example.py"
        for path in (old, new):
            path.parent.mkdir(parents=True)
            path.write_bytes(b"fixed = 1\n")
        item = {
            "path": "library/installer/example.py",
            "size": old.stat().st_size,
            "sha256": inputs.identity(old.read_bytes()),
        }
        manifest = {"members": [item]}
        checked = inputs.verify_kit_library_compatibility(
            kit_root=kit_root, kit_manifest=manifest, execution_root=current
        )
        self.assertEqual(checked["library_members"], 1)
        new.write_bytes(b"fixed = 2\n")
        with self.assertRaisesRegex(host.h.CandidateHarnessError, "COMPATIBILITY"):
            inputs.verify_kit_library_compatibility(
                kit_root=kit_root, kit_manifest=manifest, execution_root=current
            )
        with self.assertRaisesRegex(host.h.CandidateHarnessError, "COMPATIBILITY"):
            inputs.verify_kit_library_compatibility(
                kit_root=kit_root, kit_manifest={"members": []}, execution_root=current
            )

    def test_actual_ssh_argv_json_shell_transport_compiles_complete_guest_program(self):
        import shlex

        inventory = (
            Path(host.__file__).parent / "closed_runtime_inventory.py"
        ).read_text(encoding="utf-8")
        command = host.fixed_guest_command(
            stage="/tmp/animemo-userspace-" + "b" * 32,
            inventory_program=inventory,
            expected_digest="sha256:" + "1" * 64,
        )
        provider = object.__new__(host.h.ClosedVmwareProvider)
        provider._bootstrap_identity = lambda: self.root / "fixture-not-a-key"
        authority = SimpleNamespace(
            known_hosts_file=self.root / "fixture-known-hosts",
            host_key_alias="animemo-test-alias",
        )
        argv = provider._ssh_argv(authority, command, bootstrap_identity=True)
        transported = json.loads(
            json.dumps(argv, ensure_ascii=False).encode("utf-8").decode("utf-8")
        )
        self.assertEqual(transported, list(argv))
        decoded = shlex.split(transported[-1], posix=True)
        self.assertEqual(decoded[:5], ["/usr/bin/python3", "-I", "-S", "-B", "-c"])
        self.assertEqual(len(decoded), 6)
        compile(decoded[-1], "<actual-ssh-transport-guest>", "exec")
        child = guest.child_python_argv(
            guest._network_isolation_child_program(),
            root=self.root,
            vendor=self.root / "vendor",
        )
        compile(json.loads(json.dumps(child))[-1], "<actual-child-argv>", "exec")
        with self.assertRaisesRegex(host.h.CandidateHarnessError, "GENERATION"):
            host.fixed_guest_command(
                stage="/tmp/animemo-userspace-" + "b" * 32,
                inventory_program="def broken(:",
                expected_digest="sha256:" + "1" * 64,
            )

    def test_generated_syntax_failure_precedes_process_launch_and_hides_source(self):
        with mock.patch("installer.apt_diagnostics.capture_process") as capture:
            with self.assertRaises(guest.ChildFailure) as caught:
                guest._run_python(
                    "exec('private-sentinel\n')",
                    root=self.root,
                    vendor=self.root,
                    work=self.root,
                )
        capture.assert_not_called()
        self.assertEqual(caught.exception.diagnostic["category"], "GENERATION_SYNTAX")
        self.assertFalse(caught.exception.diagnostic["process_started"])
        self.assertNotIn("private-sentinel", str(caught.exception.diagnostic))

    def test_failed_child_projects_start_cleanup_and_protocol_without_raw_text(self):
        for outcome, secondary, stdout, category in (
            ("LAUNCH_FAILED", (), b"", "PROCESS_START"),
            ("EXITED", ("OUTPUT_DRAIN_FAILED",), b"", "PROCESS_CLEANUP"),
            ("EXITED", (), b'{"x":1,"x":2}', "PROTOCOL_MATERIAL"),
        ):
            result = SimpleNamespace(
                outcome=outcome,
                returncode=0,
                secondary_errors=secondary,
                stdout_summary={"truncated": False, "missing": False},
                stderr_summary={"truncated": False, "missing": False},
                stdout=stdout,
                stderr=b"",
            )
            with mock.patch(
                "installer.apt_diagnostics.capture_process", return_value=result
            ):
                with self.assertRaises(guest.ChildFailure) as caught:
                    guest._run_python(
                        "pass", root=self.root, vendor=self.root, work=self.root
                    )
            self.assertEqual(caught.exception.diagnostic["category"], category)
            self.assertNotIn("stdout", caught.exception.diagnostic)
            self.assertNotIn("stderr", caught.exception.diagnostic)

    def test_self_result_remains_exported_when_child_fails_and_child_never_starts_after_self_failure(
        self,
    ):
        report = {}
        failure = guest.ChildFailure(
            "DEVELOPMENT_USERSPACE_CHILD_FAILED",
            {"category": "CHILD_EXECUTION", "returncode": 1},
        )
        with mock.patch.object(
            guest, "enforce_no_network", return_value={"scope": "FIXED"}
        ):
            guest.run_isolation_self(report)
        with mock.patch.object(guest, "_run_python", side_effect=failure):
            with self.assertRaises(guest.ChildFailure):
                guest.run_isolation_child(
                    report, root=self.root, vendor=self.root, work=self.root
                )
        self.assertEqual(report["network_isolation"]["self"]["status"], "PASS")
        self.assertEqual(report["network_isolation"]["child"]["status"], "FAIL")
        with mock.patch.object(
            guest, "enforce_no_network", side_effect=ValueError("FIXED_FAILURE")
        ):
            with self.assertRaises(ValueError):
                guest.run_isolation_self(report)
        with mock.patch.object(guest, "_run_python") as child:
            with self.assertRaises(ValueError):
                guest.run_isolation_child(
                    report, root=self.root, vendor=self.root, work=self.root
                )
        child.assert_not_called()
        self.assertEqual(report["network_isolation"]["self"]["status"], "FAIL")

    def test_caught_effect_guard_violation_remains_failure_after_finally(self):
        for event in ("socket.connect", "subprocess.Popen"):
            guard = UserspaceEffectGuard()
            cleaned = []
            try:
                try:
                    guard.audit(event, ("private-sentinel",))
                except RuntimeError:
                    pass  # Model a production failure conversion that swallows it.
            finally:
                cleaned.append(True)
            self.assertEqual(cleaned, [True])
            with self.assertRaisesRegex(RuntimeError, "CAUGHT_EXTERNAL_EFFECT"):
                guard.require_clean()
            self.assertEqual(guard.violations, [{"event": event}])
            self.assertNotIn("private-sentinel", str(guard.violations))

    def _prepared_kit_fixture(self):
        from bootstrap_kit.tests.fixtures import synthetic_kit, selection
        from bootstrap_kit.safe_files import directory_identity, remove_owned_directory
        from release.formal_windows_pretrust import create_windows_private_directory

        built, _, _ = synthetic_kit(self.root)
        value = json.loads(built["paths"]["manifest"].read_bytes())
        held_root = create_windows_private_directory(
            Path(host.__file__).anchor if __import__("os").name == "nt" else self.root,
            prefix="held-dev-source",
        )
        self.addCleanup(
            remove_owned_directory, held_root, directory_identity(held_root)
        )
        target = held_root / "userspace-probe"
        target.mkdir()
        for name, key in (("kit_manifest", "manifest"), ("kit_archive", "archive")):
            (target / name).write_bytes(built["paths"][key].read_bytes())
        for item in value["members"]:
            if item["path"].startswith("library/"):
                copied = held_root / item["path"].removeprefix("library/")
                copied.parent.mkdir(parents=True, exist_ok=True)
                copied.write_bytes((self.root / "source" / item["path"]).read_bytes())
        return held_root, target, selection(value)

    @unittest.skipUnless(
        __import__("os").name == "nt", "Actual Windows directory share contracts"
    )
    def test_kit_verification_uses_separate_root_while_actual_dev_delete_holds_remain(
        self,
    ):
        from bootstrap_kit import safe_files
        from release.formal_windows_pretrust import hold_windows_private_path_chain

        held_root, target, selection = self._prepared_kit_fixture()
        observed = []
        create = safe_files.create_private_directory

        def remember(*args, **kwargs):
            path = create(*args, **kwargs)
            observed.append(path)
            return path

        with hold_windows_private_path_chain(held_root, allow_leaf_child_writes=True):
            with self.assertRaises(safe_files.KitFileError):
                with safe_files.hold_path_chain(target):
                    self.fail("Conflicting nested hold unexpectedly acquired")
            with mock.patch.object(
                safe_files, "create_private_directory", side_effect=remember
            ):
                result = inputs.verify_prepared_kit_compatibility(
                    target=target, execution_root=held_root, selection=selection
                )
            self.assertEqual(result["library_members"], 3)
            self.assertTrue(observed)
            self.assertTrue(all(not path.exists() for path in observed))
            self.assertTrue(held_root.is_dir())

    def test_kit_verification_cleanup_does_not_replace_primary_pin_failure(self):
        from bootstrap_kit import safe_files, seed

        held_root, target, selection = self._prepared_kit_fixture()
        (target / "kit_archive").write_bytes(b"invalid pinned archive")
        observed = []
        remove = safe_files.remove_owned_directory
        canonical_kit_remove = seed.remove_owned_directory

        def cleanup_failure(path, identity):
            observed.append((path, identity))
            raise OSError("private-cleanup-sentinel")

        try:
            with mock.patch.object(
                safe_files, "remove_owned_directory", side_effect=cleanup_failure
            ):
                self.assertIs(seed.remove_owned_directory, canonical_kit_remove)
                with self.assertRaisesRegex(
                    ValueError, "ARCHIVE_PIN_MISMATCH"
                ) as caught:
                    inputs.verify_prepared_kit_compatibility(
                        target=target, execution_root=held_root, selection=selection
                    )
            self.assertEqual(
                caught.exception.secondary_errors,
                ("DEVELOPMENT_USERSPACE_COMPATIBILITY_CLEANUP_FAILED",),
            )
            self.assertNotIn("private-cleanup-sentinel", str(caught.exception))
        finally:
            for path, identity in observed:
                remove(path, identity)
        self.assertTrue(observed)
        self.assertTrue(all(not path.exists() for path, _ in observed))


if __name__ == "__main__":
    unittest.main()
