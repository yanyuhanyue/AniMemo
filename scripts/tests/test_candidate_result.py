"""Real result files plus synthetic main-entry failures; no Guest or capture."""
from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import contextmanager, nullcontext, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from scripts import candidate_vm_harness as h
from scripts.candidate_batch_session import WIRE_REPAIR_AUTHORIZATION
from scripts.candidate_result import CandidateResultError, CandidateResultFile


class CandidateResultTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name)

    def test_write_failure_keeps_the_prior_complete_snapshot(self):
        writer=CandidateResultFile(self.root/'result.json')
        previous={'status':'RUNNING','profileReceipts':{'FRESH_BASE':{'result':'PASS'}}}
        writer.write(previous)
        with (mock.patch('scripts.candidate_result.os.replace',side_effect=OSError('controlled')),
                self.assertRaisesRegex(CandidateResultError,'CANDIDATE_RESULT_WRITE_FAILED')):
            writer.write({'status':'PASS'})
        self.assertEqual(json.loads(writer.path.read_bytes()),previous)
        self.assertEqual(list(self.root.glob('*.tmp')),[])

    def test_existing_or_replaced_target_is_not_overwritten(self):
        writer=CandidateResultFile(self.root/'result.json')
        writer.write({'status':'RUNNING'})
        with self.assertRaisesRegex(CandidateResultError,'OUTPUT_EXISTS'):
            CandidateResultFile(writer.path)
        other=self.root/'replacement.json'
        other.write_bytes(b'{"preexisting":true}')
        os.replace(other,writer.path)
        with self.assertRaisesRegex(CandidateResultError,'PATH_CHANGED'):
            writer.write({'status':'PASS'})
        self.assertEqual(writer.path.read_bytes(),b'{"preexisting":true}')

    @unittest.skipUnless(os.name=='nt','actual Windows delete-sharing behavior')
    def test_windows_reader_retries_one_atomic_replacement(self):
        writer=CandidateResultFile(self.root/'result.json')
        writer.write({'status':'RUNNING'})
        ordinary_reader=writer.path.open('rb')
        self.addCleanup(ordinary_reader.close)
        with mock.patch('scripts.candidate_result.time.sleep',side_effect=lambda _:ordinary_reader.close()) as wait:
            writer.write({'status':'PASS'})
        wait.assert_called_once_with(0.1)
        self.assertEqual(json.loads(writer.path.read_bytes()),{'status':'PASS'})

    def _run_entry(self, execute, *, cleanup_error=None, final_write_error=False):
        path=self.root/'entry.json'
        plan=SimpleNamespace(plan_digest='sha256:'+'a'*64,as_dict=lambda:{'planDigest':'sha256:'+'a'*64})
        batch=mock.Mock(record={'profiles':{}})
        @contextmanager
        def authority(**_):
            try:
                yield
            finally:
                if cleanup_error:
                    raise h.CandidateHarnessError(cleanup_error)
        original=CandidateResultFile.write
        def write(writer,value):
            if final_write_error and value.get('status')=='PASS':
                raise CandidateResultError('CANDIDATE_RESULT_WRITE_FAILED')
            return original(writer,value)
        stdout=io.StringIO()
        with (mock.patch.object(h.ClosedVmwareProvider,'execution_authority',side_effect=authority),
              mock.patch.object(h,'acquire_candidate_material_authority',return_value=nullcontext(SimpleNamespace())),
              mock.patch.object(h,'build_harness_plan',return_value=plan),
              mock.patch.object(h,'execute_harness_plan',side_effect=execute),
              mock.patch('scripts.isolated_guest_validation._check_checkout'),
              mock.patch('scripts.guest_console_capture.WindowsConsoleCapture.preflight'),
              mock.patch('scripts.candidate_batch_session.CandidateBatch',return_value=batch) as factory,
              mock.patch.object(CandidateResultFile,'write',write),redirect_stdout(stdout)):
            code=h.main(['--verified-candidate-digest','sha256:'+'a'*64,
                '--expected-qualification-run-id','1234','--expected-source-sha','a'*40,
                '--expected-source-tree','b'*40,'--execute','--authorization-id',WIRE_REPAIR_AUTHORIZATION,
                '--r2-origin-transport','s3','--result',str(path)])
        self.assertEqual(factory.call_args.kwargs['authorization_id'],WIRE_REPAIR_AUTHORIZATION)
        return code,json.loads(stdout.getvalue()),json.loads(path.read_bytes())

    def test_main_success_file_and_exit_agree(self):
        code,summary,saved=self._run_entry(lambda *_,**__:{'status':'PASS','stage':'COMPLETE',
            'aggregateReceipt':{'synthetic':True}})
        self.assertEqual(code,0)
        self.assertEqual(summary['controller_exit_code'],0)
        self.assertEqual(saved['status'],'PASS')
        self.assertIn('aggregateReceipt',saved)
        self.assertNotIn('aggregateReceipt',summary)

    def test_main_preserves_wire_primary_and_cleanup_secondary(self):
        def execute(*_,**kwargs):
            kwargs['provider']._candidate_result_checkpoint({'stage':'WIRE',
                'aggregateReceipt':{'synthetic':True},'profileReceipts':{'FRESH_BASE':{'synthetic':True}}})
            raise h.CandidateHarnessError('CANDIDATE_RECEIPT_WIRE_SIZE_LIMIT')
        code,summary,saved=self._run_entry(execute,cleanup_error='CANDIDATE_VM_EXECUTION_AUTHORITY_RELEASE_FAILED')
        self.assertEqual(code,2)
        self.assertEqual(summary['status'],saved['status'])
        self.assertEqual(saved['failure_code'],'CANDIDATE_RECEIPT_WIRE_SIZE_LIMIT')
        self.assertEqual(saved['failure_stage'],'WIRE')
        self.assertIn({'stage':'HOST_CLEANUP','code':'CANDIDATE_VM_EXECUTION_AUTHORITY_RELEASE_FAILED'},saved['secondary_failures'])
        self.assertIn('aggregateReceipt',saved)

    def test_main_final_write_failure_is_nonzero_and_keeps_partial_result(self):
        def execute(*_,**kwargs):
            kwargs['provider']._candidate_result_checkpoint({'stage':'WIRE','aggregateReceipt':{'synthetic':True}})
            return {'status':'PASS','stage':'COMPLETE','aggregateReceipt':{'synthetic':True}}
        code,summary,saved=self._run_entry(execute,final_write_error=True)
        self.assertEqual(code,2)
        self.assertEqual(summary['status'],'ERROR')
        self.assertEqual(summary['output_failure_code'],'CANDIDATE_RESULT_WRITE_FAILED')
        self.assertEqual(saved['status'],'RUNNING')
        self.assertIn('aggregateReceipt',saved)

    def test_poststate_error_does_not_erase_prior_profile_failure(self):
        result={'status':'RUNNING','profileResults':{'fresh_base':{'failure_code':'CANDIDATE_PROFILE_REPORTED_FAILURE'}}}
        h._record_candidate_failure(result,h.CandidateHarnessError('R2_PLUGIN_POSTSTATE_FAILED'),'ORIGIN_POSTSTATE')
        self.assertEqual(result['failure_code'],'CANDIDATE_PROFILE_REPORTED_FAILURE')
        self.assertEqual(result['secondary_failures'],[{'stage':'ORIGIN_POSTSTATE','code':'R2_PLUGIN_POSTSTATE_FAILED'}])


if __name__=='__main__':
    unittest.main()
