"""Formal-specific scope/role/network plumbing with synthetic I/O only."""
from contextlib import ExitStack, nullcontext
from dataclasses import replace
import io
from pathlib import Path
import shlex
import time
from types import SimpleNamespace
import unittest
from unittest import mock

from scripts import candidate_batch_session as batches, candidate_guest_session as guests
from scripts import formal_guest_session as formal
from scripts import guest_batch_scope as scopes
from scripts.formal_plan import FormalHarnessPlan
from scripts.development_source import HeldDevelopmentSource
from scripts.formal_workload_root import _offline_guest_egress
from scripts.tests import test_candidate_batch_session as batch_fixtures
from scripts.tests.test_candidate_diagnostics import successful_frames
from scripts.tests.formal_windows_pretrust_fixture import private_windows_test_directory


class FormalBatchSessionTests(unittest.TestCase):
    def setUp(self):
        fixture=batch_fixtures.BatchSessionTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.batch.close()
        self.fixture=fixture
        provisional=FormalHarnessPlan(**{**fixture.plan.__dict__,'plan_digest':''},
            authority_digest='sha256:'+'a'*64,execution_source_sha='b'*40,
            execution_source_tree='c'*40,execution_inventory_digest='sha256:'+'d'*64)
        plan=replace(provisional,plan_digest=batches.h.sha256_bytes(batches.h.canonical_json_bytes(provisional.identity_body())))
        fixture.plan=plan
        source=object.__new__(HeldDevelopmentSource)
        source._closed=False
        source._execution=fixture.provider._execution
        source.source_sha,source.source_tree,source.inventory_digest=(plan.execution_source_sha,
            plan.execution_source_tree,plan.execution_inventory_digest)
        fixture.provider._development_source_authority=source
        root=Path(self.enterContext(private_windows_test_directory()))
        body=dict(purpose='FORMAL_POSTPUBLICATION',authorization_id='ANIMEMO_SYNTHETIC_FORMAL_TEST_V1',
            round_limit=1,
            confirmed_utc_seconds=time.time(),material_identity=scopes.material_identity(plan),
            initial_plan_digest=plan.plan_digest, initial_plan=plan.as_dict())
        scope=scopes.LocalBatchAuthorization(scopes._ISSUER,body=body,root=root,
            holds=ExitStack(),monotonic_now=time.monotonic())
        self.addCleanup(scope.close)
        fixture.batch=batches.CandidateBatch(fixture.provider,plan,authorization_id=scope.authorization_id,
            local_authorization=scope)
        self.addCleanup(fixture.batch.close)
        fixture.provider._candidate_batch=fixture.batch
        self.scope=scope

    def test_formal_three_targets_nine_distinct_grants_capture_once_and_wipe(self):
        fixture=self.fixture
        original=bytes(fixture.secret)
        for index in range(3):
            profile,runtime=fixture.bootstrap(index)
            operation=guests._diagnostic_operation(fixture.plan,profile)
            fixture.fixture.runner.before_exchange=lambda process,operation=operation:setattr(process,'stdout',
                io.BytesIO(process.stdout.getvalue()+successful_frames(operation)))
            with fixture.batch.operation('WORKLOAD',profile):
                use=fixture.batch.issue(profile,fixture.lease,('FORMAL_WORKLOAD',))
                supervisor=formal._FormalWorkloadSupervisor(use,provider=fixture.provider,plan=fixture.plan,
                    profile=profile,lease=fixture.lease,preboot_disk_graph_digest=runtime.disk_graph_digest,
                    preboot_snapshot_identity=runtime.snapshot_identity)
                supervisor._workload=SimpleNamespace()
                try:
                    with mock.patch.object(formal,'_root_program',return_value='pass'),mock.patch.object(
                        guests,'hold_windows_private_file',side_effect=lambda _:nullcontext()):
                        self.assertEqual(supervisor.execute()['result'],'PASS')
                    fixture.batch.role_result(profile,'FORMAL_WORKLOAD','PASS')
                finally:
                    supervisor.close()
            fixture.fixture.runner.before_exchange=None
        fixture.console.capture.assert_called_once()
        self.assertEqual(len(fixture.fixture.runner.processes),9)
        for process in fixture.fixture.runner.processes:
            self.assertEqual(process.stdin.getvalue(),original+b'\n')
        self.assertTrue(self.scope._capture_consumed)
        fixture.batch.close()
        self.assertEqual(fixture.secret,b'')

    def test_development_id_is_rejected_for_both_formal_purposes_before_confirmation(self):
        with mock.patch.object(scopes,'WindowsConsoleCapture') as console:
            for purpose,plan in (('FORMAL_POSTPUBLICATION',self.fixture.plan),
                ('CANDIDATE_ACCEPTANCE',self.fixture.fixture.plan)):
                with self.subTest(purpose=purpose),self.assertRaises(scopes.ControllerFailure):
                    scopes.confirm_local_batch(authorization_id=scopes.DEVELOPMENT_AUTHORIZATION,purpose=purpose,plan=plan)
            console.assert_not_called()

    def test_formal_secret_cannot_be_granted_for_candidate_workload(self):
        profile,_=self.fixture.bootstrap(0)
        with self.fixture.batch.operation('WORKLOAD',profile):
            with self.assertRaises(scopes.ControllerFailure):
                self.fixture.batch.issue(profile,self.fixture.lease,('CANDIDATE_WORKLOAD',))

    def test_remote_wrapper_carries_only_public_exact_ssh_flow_to_fixed_root(self):
        command=guests._remote_workload_command('pass','sha256:'+'1'*64,formal_ssh_context=True)
        code=shlex.split(command)[-1]
        compile(code,'<synthetic-formal-remote>','exec')
        self.assertIn("os.environ.get('SSH_CONNECTION','').split()",code)
        self.assertIn('+ ssh_flow',code)
        self.assertNotIn(bytes(self.fixture.secret).decode(),command)


class OfflineEgressTests(unittest.TestCase):
    def test_only_exact_ssh_reply_is_exempt_even_for_preexisting_external_flows(self):
        with mock.patch('scripts.formal_workload_root.subprocess.run') as run:
            receipt=_offline_guest_egress('1'*32,['192.168.64.1','55221','192.168.64.10','22'],'192.168.64.10')
        commands=[call.args[0] for call in run.call_args_list]
        self.assertEqual(commands,receipt['commands'])
        established=[command for command in commands if '--ctstate' in command]
        self.assertEqual(len(established),1)
        rule=established[0]
        for flag,value in (('-s','192.168.64.10'),('-d','192.168.64.1'),('--sport','22'),
            ('--dport','55221'),('--ctstate','ESTABLISHED'),('--ctdir','REPLY')):
            self.assertEqual(rule[rule.index(flag)+1],value)
        self.assertNotIn('RELATED',' '.join(' '.join(command) for command in commands))
        for binary in ('/usr/sbin/iptables','/usr/sbin/ip6tables'):
            self.assertTrue(any(command[0]==binary and command[-2:]==['-j','REJECT'] for command in commands))

    def test_wrong_guest_or_ssh_port_rejects_before_firewall_changes(self):
        with mock.patch('scripts.formal_workload_root.subprocess.run') as run:
            for flow in ([],['192.168.64.1','55221','192.168.64.99','22'],
                         ['192.168.64.1','55221','192.168.64.10','443']):
                with self.assertRaises(ValueError):
                    _offline_guest_egress('1'*32,flow,'192.168.64.10')
            run.assert_not_called()
