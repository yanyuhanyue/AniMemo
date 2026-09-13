"""Full historic-scale data is replayed only as explicit local test input."""
from __future__ import annotations

import copy
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from release import candidate
from scripts import candidate_receipt_regression as regression
from scripts import candidate_vm_harness as harness

FIXTURE = Path(__file__).parent / 'fixtures/candidate-wire-real-scale.json.xz'


class RealScaleReceiptTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='candidate-回执-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = regression.load_fixture(FIXTURE)

    def test_real_three_profiles_complete_file_cli_and_freshness_chain(self):
        before = candidate.canonical_json_bytes(self.source)
        summary = regression.exercise_complete_result(self.source, self.root/'complete')
        self.assertEqual(summary['context'], regression.CONTEXT)
        self.assertEqual(summary['status'], 'LOCAL_REGRESSION_PASSED')
        self.assertEqual(summary['wireMetrics']['encoding'], 'xz')
        self.assertGreater(summary['wireMetrics']['canonical_json_bytes'], 360000)
        self.assertLessEqual(summary['wireMetrics']['canonical_json_bytes'], 393216)
        self.assertLessEqual(summary['wireMetrics']['wire_characters'], 49152)
        self.assertTrue(summary['canonical_cli_bytes_identical'])
        self.assertEqual(summary['cli_exit_code'], 0)
        self.assertFalse(summary['wire_in_argv'])
        self.assertLess(summary['cli_argv_characters'], 32767)
        self.assertEqual(summary['freshness_receipt_consumer'], 'PASS')
        self.assertEqual(summary['publish_candidate_consumer']['result'], 'REJECTED_NO_VERIFIED_MATERIAL_CONTEXT')
        self.assertEqual(candidate.canonical_json_bytes(self.source), before)

    def test_actual_cli_failure_preserves_validated_aggregate_and_wire(self):
        with (mock.patch.object(regression.subprocess, 'run', return_value=subprocess.CompletedProcess(
                [], 2, stdout=b'', stderr=b'controlled CLI failure')),
                self.assertRaises(ValueError)):
            regression.exercise_complete_result(self.source, self.root/'cli-failure')
        result = json.loads((self.root/'cli-failure/result.json').read_bytes())
        self.assertEqual(result['status'], 'ERROR')
        self.assertEqual(result['controller_exit_code'], 2)
        self.assertEqual(result['cli_exit_code'], 2)
        candidate.validate_aggregate_receipt(result['aggregateReceipt'])
        self.assertEqual(len(result['profileReceipts']), 3)
        self.assertIn('candidateAcceptanceReceiptB64url', result)

    def test_growth_retains_different_commands_or_reports_supported_size_limit(self):
        source = copy.deepcopy(self.source)
        measured = []
        for growth in (0, 12, 40):
            value = copy.deepcopy(source)
            for name, detail in value['profileReceipts'].items():
                commands = detail['network_observation']['completed_commands']
                commands.extend({'argv_digest':'sha256:'+hashlib.sha256(f'{name}-growth-{i}'.encode()).hexdigest(),
                    'boundary':'RUNTIME','classification':'LOCAL_ONLY','operation':'local',
                    'external_pull_disposition':'NOT_APPLICABLE','return_code':0} for i in range(growth))
                detail['network_observation']['completed_command_inventory_digest'] = candidate.sha256_bytes(candidate.canonical_json_bytes(commands))
                runtime = [item for item in commands if item['boundary']=='RUNTIME']
                detail['external_pull_observation']['runtime_command_inventory_digest'] = candidate.sha256_bytes(candidate.canonical_json_bytes(runtime))
                value['profileResults'][name.lower()]['receipt_digest'] = candidate.sha256_bytes(candidate.canonical_json_bytes(detail))
            scope = regression._scope(value)
            aggregate = harness.build_candidate_aggregate(scope, profile_results=value['profileResults'],
                receipts=value['profileReceipts'],candidate_prestate=value['candidatePrestate'],
                candidate_poststate=value['candidatePrestate'],r2_prestate_receipt=value['r2OriginPrestateReceipt'],
                r2_poststate_receipt=value['r2OriginPoststateReceipt'],plugin_origin=True)
            raw = candidate.canonical_json_bytes(aggregate)
            if len(raw)>393216:
                with self.assertRaisesRegex(harness.CandidateHarnessError,'CANDIDATE_RECEIPT_SIZE_LIMIT'):
                    harness.export_candidate_aggregate(aggregate)
                measured.append('SIZE_LIMIT')
            else:
                exported=harness.export_candidate_aggregate(aggregate)
                self.assertLessEqual(exported['wireMetrics']['wire_characters'],49152)
                self.assertEqual(candidate.decode_aggregate_receipt_b64url(exported['candidateAcceptanceReceiptB64url'])[1],raw)
                measured.append('PASS')
        self.assertEqual(measured,['PASS','PASS','SIZE_LIMIT'])

    def test_actual_cli_rejects_oversized_and_non_ascii_wire_files(self):
        for index, raw in enumerate((b'x'*49153,b'\xff')):
            wire=self.root/f'invalid-{index}.txt'
            output=self.root/f'invalid-{index}.json'
            wire.write_bytes(raw)
            result=subprocess.run([sys.executable,'-X','utf8','-B','-m','release.cli',
                'decode-candidate-acceptance-receipt','--value-file',str(wire),'--output',str(output)],
                cwd=Path(__file__).resolve().parents[2],capture_output=True,timeout=30,check=False)
            self.assertEqual(result.returncode,2)
            self.assertEqual(json.loads(result.stderr)['code'],'CANDIDATE_RECEIPT_WIRE_FILE_INVALID')
            self.assertFalse(output.exists())


if __name__ == '__main__':
    unittest.main()
