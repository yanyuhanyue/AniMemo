from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import unittest
from contextlib import ExitStack
from dataclasses import replace
from unittest import mock

from scripts import candidate_vm_harness as h
from scripts import guest_sudo_session as c
from scripts.tests import test_candidate_vm_harness as fixtures

SENTINEL = b"offline-synthetic-sentinel-64"


class InputSink(io.BytesIO):
    def close(self):
        self.was_closed = True


class Process:
    def __init__(self, observation):
        self.stdin = InputSink()
        self.stdout = io.BytesIO(json.dumps({
            "machine_id": observation.machine_id, "boot_id": observation.boot_id,
            "mac_addresses": list(observation.mac_addresses), "nonce": observation.nonce,
        }).encode() + b"\n")
        self.dead = False
    def poll(self):
        return 0 if self.dead else None


class ExchangeRunner(fixtures.RecordingRunner):
    def __init__(self):
        super().__init__()
        self.processes = []
        self.before_exchange = None
        self.after_exchange = None
        self.failure = False
        self.observation = None
        self.argv = None
        self.environment = None
    def run_guest_exchange(self, argv, *, environment, cwd, exchange, timeout):
        self.argv = tuple(argv)
        self.environment = dict(environment)
        process = Process(self.observation)
        self.processes.append(process)
        if self.before_exchange:
            self.before_exchange(process)
        exchange(process)
        if self.after_exchange:
            self.after_exchange(process, exchange)
        return subprocess.CompletedProcess(argv, 1 if self.failure else 0, b"", b"")


class GuestSudoSessionTests(unittest.TestCase):
    def setUp(self):
        fixture = fixtures.CandidateVmHarnessTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        self.plan, self.profile, self.authority, self.runtime, self.bootstrap, self.verified = fixture._connection_fixture()
        self.runner = ExchangeRunner()
        self.provider = h.ClosedVmwareProvider(runner=self.runner, windows_platform=fixtures.FakeWindowsPlatform(), environment={})
        self.clock_value = 1.0
        self.secret = bytearray(SENTINEL)
        self.session = c.SessionSupervisor(self.secret, provider=self.provider, plan=self.plan, profile=self.profile,
            preboot_disk_graph_digest=self.runtime.disk_graph_digest, preboot_snapshot_identity=self.runtime.snapshot_identity,
            clock=lambda: self.clock_value)
        self.addCleanup(self.session.close)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        # Only transport/filesystem observations are substituted. Both canonical
        # verifiers, session grant gates, and provider launch guards execute.
        self.running = self.stack.enter_context(mock.patch.object(self.provider, "_running_vmx_paths",
            return_value=frozenset({os.path.normcase(str(self.authority.clone_vmx.resolve(strict=False)))})))
        self.stack.enter_context(mock.patch.object(self.provider, "_wait_for_guest_ip", return_value=h.SSH_HOST))
        self.runtime_read = self.stack.enter_context(mock.patch.object(self.provider, "_read_clone_runtime_identity", return_value=self.runtime))
        self.stack.enter_context(mock.patch.object(self.provider, "_wait_for_ssh"))
        self.key_read = self.stack.enter_context(mock.patch.object(self.provider, "_read_known_host_key", return_value=self.bootstrap.host_key_digest))
        self.observe = self.stack.enter_context(mock.patch.object(self.provider, "_observe_guest_connection", return_value=self.bootstrap))
        self.stack.enter_context(mock.patch.object(self.provider, "_session_public_key", return_value="ssh-ed25519 YWJj " + self.authority.host_key_alias))
        self.stack.enter_context(mock.patch.object(self.provider, "_remove_known_hosts"))
        self.runner.observation = self.bootstrap

    def test_bootstrap_and_verified_roles_use_canonical_gate_and_stdin_only(self):
        self.session.bootstrap_rotation()
        self.assertEqual(self.session.injection_count, 1)
        self.key_read.return_value = self.verified.host_key_digest
        self.runner.observation = self.verified
        self.session.validate_verified_guest()
        self.assertEqual(self.session.injection_count, 2)
        expected_counts = {"BOOTSTRAP_ROTATION": 1, "VERIFIED_SUDO": 1}
        self.assertEqual(self.session.delivery_attempts, expected_counts)
        self.assertEqual(self.session.delivery_completed, expected_counts)
        self.assertEqual(self.secret, b"")
        for process in self.runner.processes:
            self.assertEqual(process.stdin.getvalue(), SENTINEL + b"\n")
        self.assertNotIn(SENTINEL.decode(), repr(self.runner.argv))
        self.assertNotIn(SENTINEL.decode(), repr(self.runner.environment))
        self.assertNotIn("ANIMEMO_CANDIDATE_GUEST_SUDO_PASSWORD", self.runner.environment)
        with self.assertRaises(c.ControllerFailure):
            self.session.validate_verified_guest()
        self.assertEqual(self.session.injection_count, 2)
        self.assertEqual(self.session.delivery_attempts, expected_counts)

    def test_partial_or_failed_stdin_delivery_is_counted_once_and_never_replayed(self):
        for role in ("BOOTSTRAP_ROTATION", "VERIFIED_SUDO"):
            for failure in ("short_write", "write_error", "flush_error", "close_error"):
                with self.subTest(role=role, failure=failure):
                    secret = bytearray(SENTINEL)
                    session = c.SessionSupervisor(secret, provider=self.provider, plan=self.plan, profile=self.profile,
                        preboot_disk_graph_digest=self.runtime.disk_graph_digest,
                        preboot_snapshot_identity=self.runtime.snapshot_identity)
                    self.addCleanup(session.close)
                    self.runner.before_exchange = None
                    self.runner.observation = self.bootstrap
                    self.key_read.return_value = self.bootstrap.host_key_digest
                    if role == "VERIFIED_SUDO":
                        session.bootstrap_rotation()
                        self.runner.observation = self.verified
                        self.key_read.return_value = self.verified.host_key_digest
                    testcase = self

                    class FailingSink(InputSink):
                        write_calls = 0
                        flush_calls = 0
                        close_calls = 0

                        def write(self, value):
                            self.write_calls += 1
                            testcase.assertEqual(session.delivery_attempts[role], 1)
                            testcase.assertEqual(session.delivery_completed[role], 0)
                            if failure in {"short_write", "write_error"}:
                                written = super().write(value[:3])
                                if failure == "write_error":
                                    raise OSError("synthetic partial write")
                                return written
                            return super().write(value)

                        def flush(self):
                            self.flush_calls += 1
                            if failure == "flush_error":
                                raise OSError("synthetic flush failure")
                            return super().flush()

                        def close(self):
                            self.close_calls += 1
                            if failure == "close_error":
                                raise OSError("synthetic close failure")
                            return super().close()

                    sink = FailingSink()
                    self.runner.before_exchange = lambda process: setattr(process, "stdin", sink)
                    operation = session.bootstrap_rotation if role == "BOOTSTRAP_ROTATION" else session.validate_verified_guest
                    with self.assertRaises(c.ControllerFailure) as caught:
                        operation()
                    self.assertNotIn(SENTINEL.decode(), str(caught.exception))
                    self.assertEqual(secret, b"")
                    self.assertEqual(sink.write_calls, 1)
                    if failure in {"short_write", "write_error"}:
                        self.assertEqual(sink.getvalue(), SENTINEL[:3])
                        self.assertEqual(sink.flush_calls, 0)
                        self.assertEqual(sink.close_calls, 0)
                    else:
                        self.assertEqual(sink.getvalue(), SENTINEL + b"\n")
                    expected_attempts = {"BOOTSTRAP_ROTATION": 1, "VERIFIED_SUDO": int(role == "VERIFIED_SUDO")}
                    expected_completed = {"BOOTSTRAP_ROTATION": int(role == "VERIFIED_SUDO"), "VERIFIED_SUDO": 0}
                    self.assertEqual(session.delivery_attempts, expected_attempts)
                    self.assertEqual(session.delivery_completed, expected_completed)
                    self.assertEqual(session.injection_count, int(role == "VERIFIED_SUDO"))
                    process_count = len(self.runner.processes)
                    for replay in (session.bootstrap_rotation, session.validate_verified_guest):
                        with self.assertRaises(c.ControllerFailure):
                            replay()
                    self.assertEqual(len(self.runner.processes), process_count)
                    self.assertEqual(sink.write_calls, 1)
                    session.close()
                    self.assertEqual(session.delivery_attempts, expected_attempts)
                    self.assertEqual(session.delivery_completed, expected_completed)

    def test_delivery_counter_snapshots_cannot_reset_session_accounting(self):
        self.session.bootstrap_rotation()
        self.session.delivery_attempts["BOOTSTRAP_ROTATION"] = 0
        self.session.delivery_completed.clear()
        self.assertEqual(self.session.delivery_attempts["BOOTSTRAP_ROTATION"], 1)
        self.assertEqual(self.session.delivery_completed["BOOTSTRAP_ROTATION"], 1)

    def test_verified_role_cannot_use_bootstrap_grant(self):
        with self.assertRaises(c.ControllerFailure):
            self.session.validate_verified_guest()
        self.assertEqual(self.runner.processes, [])
        self.assertEqual(self.secret, b"")
        self.assertEqual(self.session.delivery_attempts, {"BOOTSTRAP_ROTATION": 0, "VERIFIED_SUDO": 0})

    def test_wrong_initial_guest_fails_before_exchange(self):
        self.observe.return_value = replace(self.bootstrap, nonce="0" * 64)
        with self.assertRaises(c.ControllerFailure):
            self.session.bootstrap_rotation()
        self.assertEqual(self.runner.processes, [])
        self.assertEqual(self.secret, b"")

    def test_same_connection_wrong_guest_is_rejected_before_stdin(self):
        for field, value in (("machine_id", "2"*32), ("boot_id", "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"),
                             ("nonce", "0"*64), ("mac_addresses", ("00:0c:29:ff:ff:ff",))):
            with self.subTest(field=field):
                self.runner.observation = replace(self.bootstrap, **{field:value})
                # A fresh independent session for each failure; never revive a failed one.
                secret = bytearray(SENTINEL)
                session = c.SessionSupervisor(secret, provider=self.provider, plan=self.plan, profile=self.profile,
                    preboot_disk_graph_digest=self.runtime.disk_graph_digest, preboot_snapshot_identity=self.runtime.snapshot_identity)
                with self.assertRaises(c.ControllerFailure):
                    session.bootstrap_rotation()
                self.assertEqual(self.runner.processes[-1].stdin.getvalue(), b"")
                self.assertEqual(secret, b"")

    def test_competing_vmx_late_at_exchange_rejects_secret(self):
        self.runner.before_exchange = lambda p: setattr(self.running, "return_value", frozenset({"other.vmx"}))
        with self.assertRaises(c.ControllerFailure):
            self.session.bootstrap_rotation()
        self.assertEqual(self.runner.processes[-1].stdin.getvalue(), b"")

    def test_changed_runtime_at_exchange_rejects_secret(self):
        self.runner.before_exchange = lambda p: setattr(self.runtime_read, "return_value", replace(self.runtime, disk_graph_digest="sha256:"+"f"*64))
        with self.assertRaises(c.ControllerFailure):
            self.session.bootstrap_rotation()
        self.assertEqual(self.runner.processes[-1].stdin.getvalue(), b"")

    def test_timeout_and_cancelled_late_requests_fail_closed(self):
        self.runner.before_exchange = lambda p: setattr(self, "clock_value", 9999)
        with self.assertRaises(c.ControllerFailure):
            self.session.bootstrap_rotation()
        self.assertEqual(self.runner.processes[-1].stdin.getvalue(), b"")
        with self.assertRaises(c.ControllerFailure):
            self.session._exchange("BOOTSTRAP_ROTATION", Process(self.bootstrap))
        self.assertEqual(self.session.injection_count, 0)

    def test_cancel_during_read_discards_late_observation(self):
        self.runner.before_exchange = lambda p: self.session._cancelled.set()
        with self.assertRaises(c.ControllerFailure):
            self.session.bootstrap_rotation()
        self.assertEqual(self.runner.processes[-1].stdin.getvalue(), b"")

    def test_process_exit_and_replay_fail_closed(self):
        self.runner.before_exchange = lambda p: setattr(p, "dead", True)
        with self.assertRaises(c.ControllerFailure):
            self.session.bootstrap_rotation()
        self.assertEqual(self.runner.processes[-1].stdin.getvalue(), b"")

    def test_second_exchange_cannot_replay_first_grant(self):
        self.runner.after_exchange = lambda p, exchange: exchange(p)
        with self.assertRaises(c.ControllerFailure):
            self.session.bootstrap_rotation()
        self.assertEqual(self.session.injection_count, 1)
        self.assertEqual(self.secret, b"")

    def test_failed_sudo_revokes_session_and_late_callbacks(self):
        self.runner.failure = True
        with self.assertRaises(c.ControllerFailure):
            self.session.bootstrap_rotation()
        count = len(self.runner.processes)
        with self.assertRaises(c.ControllerFailure):
            self.session.bootstrap_rotation()
        self.assertEqual(len(self.runner.processes), count)
        self.assertEqual(self.secret, b"")

    def test_plain_authority_json_and_wrong_session_are_not_authority(self):
        for plan, profile in (({}, self.profile), (self.plan, replace(self.profile, session_id="different"))):
            with self.assertRaises(c.ControllerFailure):
                c.SessionSupervisor(bytearray(SENTINEL), provider=self.provider, plan=plan, profile=profile,
                    preboot_disk_graph_digest=self.runtime.disk_graph_digest, preboot_snapshot_identity=self.runtime.snapshot_identity)

    def test_production_provider_requires_held_execution_before_secret_use(self):
        provider = h.ClosedVmwareProvider(environment={})
        with self.assertRaisesRegex(h.CandidateHarnessError, "EXECUTION_AUTHORITY_REQUIRED"):
            c.SessionSupervisor(bytearray(SENTINEL), provider=provider, plan=self.plan, profile=self.profile,
                preboot_disk_graph_digest=self.runtime.disk_graph_digest, preboot_snapshot_identity=self.runtime.snapshot_identity)

    def test_production_transport_sends_only_to_owned_stdin_and_reaps(self):
        # A synthetic local helper exercises the actual Popen/cleanup transport.
        # No VM, SSH, sudo, real credential, or verifier bypass is involved.
        observed = []
        program = "import sys; sys.stdout.buffer.write(b'ready\\n'); sys.stdout.flush(); data=sys.stdin.buffer.readline(); sys.exit(0 if data else 2)"
        def exchange(process):
            observed.append(process)
            self.assertEqual(process.stdout.readline(), b"ready\n")
            process.stdin.write(SENTINEL+b"\n")
            process.stdin.close()
        result = h.SubprocessHostCommandRunner().run_guest_exchange(
            (sys.executable, "-I", "-c", program), environment={}, cwd=h.Path(sys.executable).parent,
            exchange=exchange, timeout=5)
        self.assertEqual(result.returncode, 0)
        self.assertIsNotNone(observed[0].poll())
        self.assertTrue(observed[0].stdin.closed)
        self.assertTrue(observed[0].stdout.closed)

    def test_malformed_or_oversized_observation_cannot_request_secret(self):
        self.runner.before_exchange = lambda p: setattr(p, "stdout", io.BytesIO(b"x" * 4097))
        with self.assertRaises(c.ControllerFailure):
            self.session.bootstrap_rotation()
        self.assertEqual(self.runner.processes[-1].stdin.getvalue(), b"")

    def test_closed_stdin_after_observation_is_failure_without_retry(self):
        def fail_write(process):
            process.stdin = io.BytesIO()
            process.stdin.close()
        self.runner.before_exchange = fail_write
        with self.assertRaises(c.ControllerFailure) as caught:
            self.session.bootstrap_rotation()
        self.assertNotIn(SENTINEL.decode(), str(caught.exception))
        self.assertEqual(self.secret, b"")
        self.assertEqual(self.session.delivery_attempts, {"BOOTSTRAP_ROTATION": 1, "VERIFIED_SUDO": 0})
        self.assertEqual(self.session.injection_count, 0)
        with self.assertRaises(c.ControllerFailure):
            self.session.bootstrap_rotation()
        self.assertEqual(len(self.runner.processes), 1)

    def test_source_plan_mutation_is_rejected_at_secret_boundary(self):
        def mutate(process):
            self.plan.original_vm_hashes["unexpected-file"] = "sha256:" + "f" * 64
        self.runner.before_exchange = mutate
        with self.assertRaises(c.ControllerFailure):
            self.session.bootstrap_rotation()
        self.assertEqual(self.runner.processes[-1].stdin.getvalue(), b"")

    def test_rebound_execution_is_rejected_at_secret_boundary(self):
        self.runner.before_exchange = lambda p: setattr(self.provider, "_execution", object())
        with self.assertRaises(c.ControllerFailure):
            self.session.bootstrap_rotation()
        self.assertEqual(self.runner.processes[-1].stdin.getvalue(), b"")
        self.provider._execution = None

    def test_forged_and_cross_process_grant_cannot_write(self):
        def forge(process):
            grant = c._Grant(object(), self.provider._execution, Process(self.bootstrap),
                             "VERIFIED_SUDO", self.clock_value + 5)
            with self.assertRaises(TypeError):
                import pickle
                pickle.dumps(grant)
            self.session._consume(grant, process, "BOOTSTRAP_ROTATION")
        self.runner.before_exchange = forge
        with self.assertRaises(c.ControllerFailure):
            self.session.bootstrap_rotation()
        self.assertEqual(self.runner.processes[-1].stdin.getvalue(), b"")

    def test_transport_reaps_owned_child_if_exchange_rejects(self):
        observed = []
        def reject(process):
            observed.append(process)
            raise c.ControllerFailure("SYNTHETIC_IDENTITY_REJECTED")
        with self.assertRaises(c.ControllerFailure):
            h.SubprocessHostCommandRunner().run_guest_exchange(
                (sys.executable, "-I", "-c", "import sys; sys.stdin.buffer.read()"),
                environment={}, cwd=h.Path(sys.executable).parent, exchange=reject, timeout=5)
        self.assertIsNotNone(observed[0].poll())
        self.assertTrue(observed[0].stdin.closed)
        self.assertTrue(observed[0].stdout.closed)

    def _production_scope(self):
        from pathlib import Path
        from types import SimpleNamespace
        import tempfile
        self.session.close()
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        work = h.create_windows_private_directory(Path(temp.name), prefix="synthetic-guest-scope")
        execution = h._ProviderExecutionAuthorityState(
            root=work, work_root=work, bootstrap_identity=work / "bootstrap-key",
            tool_paths={h.SSH: h.SSH}, vmware_runtime_identity="sha256:"+"1"*64,
            public_source_inventory=tuple(self.plan.original_vm_hashes), private_source_root=work,
            private_source=h.WindowsPrivateSourceSnapshot(work, dict(self.plan.original_vm_hashes), "sha256:"+"2"*64),
            candidate_material_authority_identity="sha256:"+"3"*64,
            candidate_material_tree_inventory_identity="sha256:"+"4"*64)
        self.provider._execution = execution
        self.provider._execution_stack = ExitStack()
        self.provider._require_execution_context = True  # Enable, never bypass, production gate.
        self.provider._readiness = h.ProviderReadinessReceipt.issue(ssh_digest=h.EXPECTED_SSH_SHA256, scp_digest=h.EXPECTED_SCP_SHA256)
        material = object.__new__(h.HeldCandidateMaterialAuthority)
        material._closed = False
        material._identity = execution.candidate_material_authority_identity
        material._tree_inventory_identity = execution.candidate_material_tree_inventory_identity
        material._loaded = SimpleNamespace(verified_digest=self.plan.verified_candidate_digest,
            verified={"candidate_input_sha256": self.plan.candidate_input_digest},
            candidate_input={"source_sha": self.plan.source_sha, "source_tree": self.plan.source_tree,
                             "candidate_version": self.plan.candidate_version, "qualification_run_id": self.plan.qualification_run_id})
        self.provider._candidate_material_authority = material
        authority = self.provider._active_profile_authority(self.profile, self.plan)
        for directory in (authority.ssh_root, authority.clone_root):
            directory.mkdir(parents=True)
        authority.identity_file.write_bytes(b"synthetic key bytes only")
        authority.identity_file.with_suffix(".pub").write_text("ssh-ed25519 YWJj " + authority.host_key_alias)
        lease = self.provider._acquire_provider_lease(authority, work_root=work)
        self.addCleanup(lambda: self.provider._release_provider_lease(lease, work_root=work) if not lease._closed else None)
        self.runtime = replace(self.runtime, clone_root=authority.clone_root, clone_vmx=authority.clone_vmx)
        self.runtime_read.return_value = self.runtime
        self.running.return_value = frozenset({os.path.normcase(str(authority.clone_vmx.resolve(strict=False)))})
        # Fake transport creates only the synthetic known-hosts fixture that the
        # actual Windows private-file guard must hold for the entire exchange.
        def wait_ssh(*args, **kwargs):
            authority.known_hosts_file.write_bytes(b"synthetic public host key")
        self.provider._wait_for_ssh.side_effect = wait_ssh
        self.provider._remove_known_hosts.side_effect = lambda a: a.known_hosts_file.unlink(missing_ok=True)
        secret = bytearray(SENTINEL)
        session = c.SessionSupervisor(secret, provider=self.provider, plan=self.plan, profile=self.profile,
            preboot_disk_graph_digest=self.runtime.disk_graph_digest, preboot_snapshot_identity=self.runtime.snapshot_identity,
            lease=lease)
        self.addCleanup(session.close)
        return session, material, execution, lease

    @unittest.skipUnless(os.name == "nt", "real Windows private-file authority")
    def test_production_held_scope_accepts_actual_file_holds(self):
        session, _, _, _ = self._production_scope()
        session.bootstrap_rotation()
        self.key_read.return_value = self.verified.host_key_digest
        self.runner.observation = self.verified
        session.validate_verified_guest()
        self.assertEqual(session.injection_count, 2)
        self.assertIn(self.verified.host_key_digest, self.provider._accepted_host_key_digests)

    @unittest.skipUnless(os.name == "nt", "real Windows private-file authority")
    def test_production_closed_material_rejects_at_stdin_boundary(self):
        session, material, _, _ = self._production_scope()
        self.runner.before_exchange = lambda process: setattr(material, "_closed", True)
        with self.assertRaises(c.ControllerFailure):
            session.bootstrap_rotation()
        self.assertEqual(self.runner.processes[-1].stdin.getvalue(), b"")

    @unittest.skipUnless(os.name == "nt", "real Windows private-file authority")
    def test_production_source_rebinding_rejects_at_stdin_boundary(self):
        session, material, _, _ = self._production_scope()
        self.runner.before_exchange = lambda process: material._loaded.candidate_input.update(source_sha="0"*40)
        with self.assertRaises(c.ControllerFailure):
            session.bootstrap_rotation()
        self.assertEqual(self.runner.processes[-1].stdin.getvalue(), b"")

    @unittest.skipUnless(os.name == "nt", "real Windows private-file authority")
    def test_production_lease_revocation_rejects_at_stdin_boundary(self):
        session, _, _, lease = self._production_scope()
        self.runner.before_exchange = lambda process: setattr(lease, "_closed", True)
        with self.assertRaises(c.ControllerFailure):
            session.bootstrap_rotation()
        self.assertEqual(self.runner.processes[-1].stdin.getvalue(), b"")
        lease._closed = False  # Fixture cleanup only, never retry this session.

    @unittest.skipUnless(os.name == "nt", "real Windows private-file authority")
    def test_production_cross_profile_lease_rejects_at_stdin_boundary(self):
        session, _, _, lease = self._production_scope()
        original = lease.authority
        self.runner.before_exchange = lambda process: setattr(lease, "authority", replace(original, connection_nonce="0"*64))
        with self.assertRaises(c.ControllerFailure):
            session.bootstrap_rotation()
        self.assertEqual(self.runner.processes[-1].stdin.getvalue(), b"")
        lease.authority = original

    def test_remote_program_is_parseable_and_fixed_operation_only(self):
        import shlex
        for role in ("BOOTSTRAP_ROTATION", "VERIFIED_SUDO"):
            code = shlex.split(c._remote_command(role, "ssh-ed25519 YWJj alias"))[-1]
            compile(code, "<guest-helper>", "exec")
            self.assertIn('password.endswith(b"\\n")', code)
        with self.assertRaises(c.ControllerFailure):
            c._remote_command("ARBITRARY")


if __name__ == "__main__":
    unittest.main()
