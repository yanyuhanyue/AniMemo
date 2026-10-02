"""Actual Formal failure through supervisor and provider finally, synthetic VM.

The native VM/SSH/material setup and secret are fixtures. The child really runs
Formal main with a missing authority file: no executor returns a PASS or a
verified release. Receipt reading, grant consumption, supervisor and provider
failure/finally logic are not replaced. This does not execute sudo or VMware.
"""
from __future__ import annotations

from contextlib import ExitStack, nullcontext
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from scripts import candidate_guest_session as guests
from scripts import candidate_vm_harness as harness
from scripts import formal_guest_session as formal
from scripts.tests import test_formal_batch_session as formal_fixtures
from scripts.tests.test_guest_sudo_session import SENTINEL

ROOT = Path(__file__).resolve().parents[2]


def _formal_failure_child(root, observation, operation, descriptor):
    """The VM/SSH boundary only; stdin carries a synthetic sentinel."""
    from scripts.candidate_diagnostics import DiagnosticWriter, FD_ENV, OP_ENV
    from scripts.formal_profile_runner import main

    os.write(descriptor, json.dumps(observation).encode() + b'\n')
    synthetic_delivery = bytearray(sys.stdin.buffer.readline())
    try:
        if synthetic_delivery != SENTINEL + b'\n':
            return 97
    finally:
        synthetic_delivery[:] = b'\0' * len(synthetic_delivery)
        synthetic_delivery.clear()
    os.environ[FD_ENV] = str(descriptor)
    os.environ[OP_ENV] = operation
    writer = DiagnosticWriter(descriptor, operation)
    writer.stage('ROOT_STARTED')
    writer.stage('RUNTIME_READY')
    writer.stage('RUNNER_STARTED')
    code = main(['--authority-root', root, '--profile', 'FORMAL_FRESH', '--execute'])
    writer.exited('RUNTIME_RUNNER', code)
    writer.error('ROOT_EXECUTION_FAILED')
    writer.exited('ROOT', code)
    writer.exited('SUDO', code)
    return code


class FormalFailureSupervisorTests(unittest.TestCase):
    def _run_failure(self, *, dedicated_descriptor):
        setup = formal_fixtures.FormalBatchSessionTests()
        setup.setUp()
        self.addCleanup(setup.doCleanups)
        fixture = setup.fixture
        profile, runtime = fixture.bootstrap(0)
        provider, plan, batch = fixture.provider, fixture.plan, fixture.batch
        connection = provider._candidate_connections[profile.profile].verified
        fixture.fixture.observe.return_value = connection.guest
        child_results = []
        supervisors = []
        original_init = formal._FormalWorkloadSupervisor.__init__

        def remember_supervisor(supervisor, *args, **kwargs):
            original_init(supervisor, *args, **kwargs)
            supervisors.append(supervisor)

        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            root = Path(directory)
            workload = harness.ClosedFormalProfileWorkload(
                authority_root=root, authority_identity=plan.authority_digest,
                formal_profile='FORMAL_FRESH', runtime_source_tree=plan.source_tree,
                runtime_inventory_digest='sha256:' + '1' * 64,
                runner_path=ROOT / 'scripts' / 'formal_profile_runner.py',
                runner_identity='sha256:' + '2' * 64)

            def exchange_process(argv, *, environment, cwd, exchange, timeout):
                observation = fixture.fixture.runner.observation
                public_guest = {key: getattr(observation, key) for key in
                                ('machine_id', 'boot_id', 'mac_addresses', 'nonce')}
                operation = guests._diagnostic_operation(plan, profile)
                read_descriptor = write_descriptor = None
                if dedicated_descriptor:
                    read_descriptor, write_descriptor = os.pipe()
                descriptor = write_descriptor if dedicated_descriptor else 1
                program = ('import sys;sys.path.insert(0,' + repr(str(ROOT)) + ');'
                    'from scripts.tests.test_formal_failure_supervisor import _formal_failure_child;'
                    'raise SystemExit(_formal_failure_child(' + repr(directory) + ','
                    + repr(public_guest) + ',' + repr(operation) + ',' + str(descriptor) + '))')
                child = subprocess.Popen([sys.executable, '-B', '-c', program], cwd=ROOT,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.DEVNULL if dedicated_descriptor else subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    **({'pass_fds': (write_descriptor,)} if dedicated_descriptor else {}))
                if dedicated_descriptor:
                    os.close(write_descriptor)
                    pipe = os.fdopen(read_descriptor, 'rb')
                    # The actual stream is a dedicated OS pipe passed to the
                    # child; only the process interface maps it onto stdout.
                    transport = SimpleNamespace(stdin=child.stdin, stdout=pipe,
                        poll=child.poll, wait=child.wait, kill=child.kill)
                else:
                    pipe, transport = child.stdout, child
                try:
                    exchange(transport)
                    return subprocess.CompletedProcess(argv, child.wait(timeout=10), b'', b'')
                finally:
                    if child.poll() is None:
                        try:
                            child.wait(timeout=10)
                        except subprocess.TimeoutExpired:
                            child.kill()
                            child.wait(timeout=10)
                    child_results.append(child.returncode)
                    child.stdin.close()
                    pipe.close()

            # Setup/material transfer is synthetic; the actual profile method
            # still dispatches execute_formal_workload and closes in finally.
            patches = [
                mock.patch.object(provider, 'inspect_readiness', return_value=
                    harness.ProviderReadinessReceipt.issue(ssh_digest=harness.EXPECTED_SSH_SHA256,
                                                          scp_digest=harness.EXPECTED_SCP_SHA256)),
                mock.patch.object(provider, '_assert_tools'),
                mock.patch.object(provider, '_hashes', return_value=dict(plan.original_vm_hashes)),
                mock.patch.object(provider, '_validate_formal_workload', return_value=(root, {})),
                mock.patch.object(provider, '_acquire_provider_lease', return_value=fixture.lease),
                mock.patch.object(provider, '_release_provider_lease'),
                mock.patch.object(provider, '_prepare_and_start_profile_clone', return_value=
                    (runtime.disk_graph_digest, runtime.snapshot_identity)),
                mock.patch.object(guests, 'bootstrap_candidate', return_value=connection),
                mock.patch.object(formal, '_root_program', return_value='pass # synthetic SSH boundary'),
                mock.patch.object(guests, 'hold_windows_private_file', side_effect=lambda _: nullcontext()),
                mock.patch.object(formal, 'hold_windows_private_file', side_effect=lambda _: nullcontext()),
                mock.patch.object(fixture.fixture.runner, 'run_guest_exchange', side_effect=exchange_process),
                mock.patch.object(formal._FormalWorkloadSupervisor, '__init__', new=remember_supervisor),
            ]
            for patch in patches:
                stack.enter_context(patch)
            with self.assertRaises(guests.WorkloadFailure) as caught:
                provider.execute_profile(plan=profile, harness_plan=plan,
                    candidate_root=root, initial_platform_state=harness._initial_platform_state(profile.profile),
                    _formal_workload=workload)
            self.assertTrue(caught.exception.revoke_batch)
            self.assertEqual(child_results, [2])
            self.assertEqual(len(supervisors), 1)
            supervisor = supervisors[0]
            self.assertEqual(supervisor.delivery_attempts, {'FORMAL_WORKLOAD': 1})
            self.assertEqual(supervisor.delivery_completed, {'FORMAL_WORKLOAD': 1})
            self.assertIsNone(supervisor._grant)
            self.assertTrue(supervisor._batch_use._closed)
            self.assertEqual(fixture.secret, b'')
            self.assertTrue(batch.cancelled.is_set())
            self.assertNotIn(profile.profile, provider._candidate_connections)
            record = provider._profile_operation_results[profile.profile]
            self.assertEqual(record['result'], 'ERROR')
            self.assertTrue(record['workload_supervisor_closed'])
            self.assertTrue(record['lease_released'])
            public = record['workload_diagnostic']
            self.assertIs(public, provider._candidate_diagnostics[profile.profile])
            self.assertEqual(public['failure_diagnostic']['status'], 'COMPLETE')
            self.assertFalse(public['profile_draft_received'])
            self.assertEqual(public['exit_codes']['RUNTIME_RUNNER'], 2)
            self.assertTrue(any(event['kind'] == 'FAULT' and
                event['module'] == 'scripts.formal_profile_runner' for event in public['events']))
            self.assertNotIn(SENTINEL.decode(), json.dumps(record))
            # Diagnostic projection cannot expose the missing authority path.
            self.assertNotIn(directory, repr(public))

    @unittest.skipUnless(os.name == 'nt', 'Windows stdout PIPE integration')
    def test_windows_formal_failure_supervisor_and_provider_finally(self):
        self._run_failure(dedicated_descriptor=False)

    @unittest.skipUnless(os.name == 'posix', 'POSIX pass_fds integration')
    def test_posix_formal_failure_dedicated_descriptor_and_provider_finally(self):
        self._run_failure(dedicated_descriptor=True)


if __name__ == '__main__':
    unittest.main()
