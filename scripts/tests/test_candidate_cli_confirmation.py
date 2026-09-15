"""Actual CLI/result lifecycle with synthetic provider and native boundaries."""
from contextlib import nullcontext, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from scripts import candidate_vm_harness as h
from scripts.guest_batch_scope import DEVELOPMENT_AUTHORIZATION, RETIRED_FORMAL_AUTHORIZATION
from scripts.guest_sudo_session import ControllerFailure


class CandidateCliConfirmationTests(unittest.TestCase):
    def run_entry(self, *, confirm=True, authorization='ANIMEMO_NEW_CANDIDATE_TEST_SCOPE',
                  confirmation_error=None, batch_error=None, cleanup_error=None, real_confirmation=False):
        temporary=tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        output=Path(temporary.name)/'result.json'
        events=[]
        plan=SimpleNamespace(plan_digest='sha256:'+'a'*64,as_dict=lambda:{'synthetic':True})
        scope=SimpleNamespace(body={'purpose':'CANDIDATE_ACCEPTANCE','synthetic':True},
            close=mock.Mock(side_effect=cleanup_error or (lambda:events.append('scope-close'))))
        batch=mock.Mock(record={'profiles':{}})
        batch.close.side_effect=lambda:events.append('batch-close')
        def confirmation(**kwargs):
            events.append('confirm')
            self.assertEqual(kwargs,dict(authorization_id=authorization,purpose='CANDIDATE_ACCEPTANCE',plan=plan))
            if confirmation_error:
                raise confirmation_error
            return scope
        def batch_factory(*args,**kwargs):
            events.append('batch')
            self.assertIs(args[1],plan)
            self.assertIs(kwargs['local_authorization'],scope)
            if batch_error:
                raise batch_error
            return batch
        def execute(*args,**kwargs):
            events.append('execute')
            return {'status':'PASS','stage':'COMPLETE','synthetic':True}
        stdout=io.StringIO()
        from contextlib import ExitStack
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(h.ClosedVmwareProvider,'execution_authority',return_value=nullcontext()))
            stack.enter_context(mock.patch.object(h,'acquire_candidate_material_authority',return_value=nullcontext(object())))
            stack.enter_context(mock.patch.object(h,'build_harness_plan',return_value=plan))
            executor=stack.enter_context(mock.patch.object(h,'execute_harness_plan',side_effect=execute))
            stack.enter_context(mock.patch('scripts.isolated_guest_validation._check_checkout'))
            preflight=stack.enter_context(mock.patch('scripts.guest_console_capture.WindowsConsoleCapture.preflight'))
            if not real_confirmation:
                stack.enter_context(mock.patch('scripts.guest_batch_scope.confirm_local_batch',side_effect=confirmation))
            factory=stack.enter_context(mock.patch('scripts.candidate_batch_session.CandidateBatch',side_effect=batch_factory))
            stack.enter_context(redirect_stdout(stdout))
            argv=['--verified-candidate-digest','sha256:'+'a'*64,'--expected-qualification-run-id','1234',
                '--expected-source-sha','a'*40,'--expected-source-tree','b'*40,'--execute',
                '--authorization-id',authorization,'--r2-origin-transport','s3','--result',str(output)]
            if confirm:
                argv.append('--confirm-batch')
            code=h.main(argv)
        return code,json.loads(output.read_bytes()),events,scope,factory,executor,preflight

    def test_new_scope_confirmation_binds_exact_plan_and_closes_after_batch(self):
        code,result,events,scope,*_=self.run_entry()
        self.assertEqual(code,0)
        self.assertEqual(events,['confirm','batch','execute','batch-close','scope-close'])
        self.assertEqual(result['batch_confirmation'],scope.body)
        scope.close.assert_called_once_with()

    def test_new_scope_without_native_confirmation_flag_rejects_before_preflight(self):
        code,result,events,scope,factory,executor,preflight=self.run_entry(confirm=False)
        self.assertEqual(code,2)
        self.assertEqual(result['failure_code'],'CANDIDATE_CAPTURE_AUTHORIZATION_INVALID')
        self.assertEqual(events,[])
        factory.assert_not_called();executor.assert_not_called();preflight.assert_not_called()

    def test_native_cancel_never_constructs_batch_or_executes(self):
        code,result,events,scope,factory,executor,_=self.run_entry(
            confirmation_error=ControllerFailure('BATCH_CONFIRMATION_CANCELLED'))
        self.assertEqual(code,2)
        self.assertEqual(events,['confirm'])
        self.assertEqual(result['failure_code'],'BATCH_CONFIRMATION_CANCELLED')
        factory.assert_not_called();executor.assert_not_called();scope.close.assert_not_called()

    def test_batch_construction_failure_still_closes_new_scope(self):
        code,result,events,scope,factory,executor,_=self.run_entry(
            batch_error=ControllerFailure('LOCAL_BATCH_AUTHORIZATION_INVALID'))
        self.assertEqual(code,2)
        self.assertEqual(events,['confirm','batch','scope-close'])
        executor.assert_not_called();scope.close.assert_called_once_with()

    def test_scope_cleanup_failure_prevents_final_pass(self):
        code,result,*_=self.run_entry(cleanup_error=h.CandidateHarnessError('SYNTHETIC_SCOPE_CLEANUP_FAILED'))
        self.assertEqual(code,2)
        self.assertEqual(result['status'],'ERROR')
        self.assertEqual(result['failure_code'],'SYNTHETIC_SCOPE_CLEANUP_FAILED')
        self.assertEqual(result['failure_stage'],'HOST_CLEANUP')

    def test_real_confirmation_api_rejects_dev_and_retired_formal_scope_before_console(self):
        for authorization in (DEVELOPMENT_AUTHORIZATION,RETIRED_FORMAL_AUTHORIZATION):
            with self.subTest(authorization=authorization),mock.patch(
                    'scripts.guest_console_capture.WindowsConsoleCapture.confirm_batch') as console:
                code,result,events,scope,factory,executor,_=self.run_entry(
                    authorization=authorization,real_confirmation=True)
                self.assertEqual(code,2)
                console.assert_not_called();factory.assert_not_called();executor.assert_not_called()


if __name__=='__main__':
    unittest.main()
