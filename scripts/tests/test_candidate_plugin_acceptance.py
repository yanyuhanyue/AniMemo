"""Complete canonical producer/consumer wiring with explicitly synthetic I/O."""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import zlib
from datetime import datetime, timezone, timedelta
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from release import candidate as contract, cli, metadata_freshness as freshness
from release import r2_plugin_origin as origin
from release.publication_input import build_publish_candidate_plan, PublicationInputError
from release.test_publication_input import _loaded
from scripts import candidate_vm_harness as h
from scripts.tests import test_candidate_vm_harness as fixtures
from scripts.tests.test_r2_plugin_origin import response


def seal(value):
    unsigned = dict(value)
    unsigned.pop('receipt_digest', None)
    value['receipt_digest'] = h.sha256_bytes(h.canonical_json_bytes(unsigned))
    return value


class PluginAcceptanceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.loaded = _loaded()
        self.provider = fixtures.FakeProvider()
        candidate = self.loaded.candidate_input
        with mock.patch.object(h, 'load_verified_candidate', return_value=self.loaded):
            self.plan = h.build_harness_plan(verified_candidate_digest=self.loaded.verified_digest,
                expected_qualification_run_id=candidate['qualification_run_id'],
                expected_source_sha=candidate['source_sha'], expected_source_tree=candidate['source_tree'],
                provider=self.provider)
        with mock.patch.object(origin, 'CHANNEL_PARENT', self.root):
            self.channel = origin.CloudflarePluginOrigin()
        self.roles = []
        def observe(plan, role):
            self.roles.append(role)
            stamp = datetime(2026, 8, 25, 11, 59, tzinfo=timezone.utc)
            if role == 'POSTSTATE': stamp += timedelta(minutes=3)
            with mock.patch.object(origin, '_now', return_value=stamp):
                request = origin.make_request(plan, role)
                return origin.validate_plugin_response(response(request), request)
        with (mock.patch.object(self.channel, 'observe', side_effect=observe), mock.patch.object(
                h, 'verify_candidate_r2_origin_from_environment', side_effect=AssertionError('S3 fallback')),
                mock.patch.object(h, 'load_verified_candidate', return_value=self.loaded)):
            self.result = h.execute_harness_plan(self.plan, accepted_plan_digest=self.plan.plan_digest,
                provider=self.provider, plugin_origin=self.channel)
        self.receipt = self.result['aggregateReceipt']

    def test_three_actual_producer_calls_close_v4_and_every_offline_consumer_accepts(self):
        self.assertEqual(self.roles, ['PRESTATE', 'POSTSTATE'])
        self.assertEqual(self.provider.execute_calls, 3)
        self.assertEqual(self.result['status'], 'PASS')
        self.assertEqual(self.receipt['version'], 4)
        self.assertEqual(len(self.receipt['profile_receipts']), 3)
        for detail in self.receipt['profile_receipts'].values():
            self.assertEqual(detail['session_id'], self.plan.session_id)
            self.assertEqual(detail['plan_digest'], self.plan.plan_digest)
            self.assertEqual(detail['version'], 2)
        # Retained observation evidence does not expire when another Profile
        # takes longer than the short plugin exchange window.
        with mock.patch.object(origin, '_now', return_value=datetime(2030, 1, 1, tzinfo=timezone.utc)):
            contract.validate_aggregate_receipt(self.receipt)
        self.assertFalse(build_publish_candidate_plan(self.loaded, self.receipt)['mutation_authorized'])
        self.consume(self.receipt, 'valid')

    def consume(self, receipt, suffix):
        encoded = h.canonical_json_bytes(receipt)
        b64 = contract.encode_aggregate_receipt_b64url(receipt)
        self.assertLess(len(b64), contract.MAX_RECEIPT_B64URL_BYTES)
        output = self.root / (suffix + '.json')
        result = cli._decode_candidate_acceptance_receipt(SimpleNamespace(value=b64, output=output))
        self.assertEqual(result['status'], 'PASS')
        identity = freshness.FreshnessRunIdentity(workflow_run_id=100, workflow_attempt=1,
            workflow_path='.github/workflows/release-notes-freshness.yml', workflow_sha=receipt['source_sha'],
            candidate_sha=receipt['source_sha'], candidate_tree=receipt['source_tree'],
            qualification_run_id=receipt['qualification_run_id'], qualification_artifact_id=100,
            candidate_acceptance_receipt_sha256=h.sha256_bytes(encoded), candidate_version=receipt['candidate_version'])
        loaded, _ = freshness._load_candidate_acceptance_receipt(output, identity=identity,
            current_time=datetime(2030, 1, 1, tzinfo=timezone.utc))
        self.assertEqual(loaded, receipt)

    def test_wrong_q_session_source_and_role_reject_even_with_new_outer_digest(self):
        for field, value in (('qualification_run_id', 999999), ('session_id', 'f' * 32),
                             ('source_sha', 'f' * 40), ('plan_digest', 'sha256:' + 'f' * 64)):
            receipt = copy.deepcopy(self.receipt)
            receipt[field] = value
            seal(receipt)
            with self.subTest(field=field), self.assertRaises(contract.CandidateContractError):
                contract.validate_aggregate_receipt(receipt)
            with self.assertRaises(PublicationInputError):
                build_publish_candidate_plan(self.loaded, receipt)

    def test_current_v4_fail_and_incomplete_receipts_decode_but_cannot_publish(self):
        for only_one_pass in (False, True):
            receipt = copy.deepcopy(self.receipt)
            for profile in h.PROFILES:
                if only_one_pass and profile == 'FRESH_BASE':
                    continue
                receipt['profile_receipts'].pop(profile)
                receipt['profile_results'][profile.lower()] = {
                    'status': 'ERROR' if profile == 'FRESH_BASE' else 'NOT_RUN_SHARED_BLOCKER',
                    'failure_code': 'CANDIDATE_SHARED_WORKLOAD_STARTUP_OR_RECEIPT_FAILURE',
                    'receipt_digest': None}
            receipt.update(all_profiles_pass=False, result='FAIL')
            seal(receipt)
            self.assertEqual(contract.validate_aggregate_receipt(receipt)['version'], 4)
            wire = contract.encode_aggregate_receipt_b64url(receipt)
            self.assertEqual(contract.decode_aggregate_receipt_b64url(wire)[0], receipt)
            output = self.root / ('failed-' + str(only_one_pass) + '.json')
            cli._decode_candidate_acceptance_receipt(SimpleNamespace(value=wire, output=output))
            self.assertEqual(json.loads(output.read_bytes())['result'], 'FAIL')
            identity = SimpleNamespace(qualification_run_id=receipt['qualification_run_id'],
                candidate_sha=receipt['source_sha'], candidate_tree=receipt['source_tree'],
                candidate_version=receipt['candidate_version'],
                candidate_acceptance_receipt_sha256=h.sha256_bytes(h.canonical_json_bytes(receipt)))
            with self.assertRaises(freshness.MetadataFreshnessError):
                freshness._load_candidate_acceptance_receipt(output, identity=identity,
                    current_time=datetime(2030, 1, 1, tzinfo=timezone.utc))
            with self.assertRaises(PublicationInputError):
                build_publish_candidate_plan(self.loaded, receipt)

    def test_observation_swap_reuse_and_tampered_response_are_rejected(self):
        for mutation in ('swap', 'reuse', 'response', 'digest'):
            receipt = copy.deepcopy(self.receipt)
            pre, post = 'r2_origin_prestate', 'r2_origin_poststate'
            if mutation == 'swap':
                for field in ('_receipt', '_receipt_digest', '_observation_id'):
                    receipt[pre + field], receipt[post + field] = receipt[post + field], receipt[pre + field]
            elif mutation == 'reuse':
                for field in ('_receipt', '_receipt_digest', '_observation_id'):
                    receipt[post + field] = copy.deepcopy(receipt[pre + field])
            else:
                detail = receipt[pre + '_receipt']
                if mutation == 'response': detail['response']['object_reads'][0]['http_status'] = 404
                else: detail['request']['request_digest'] = 'sha256:' + 'a' * 64
                seal(detail)
                receipt[pre + '_receipt_digest'] = h.sha256_bytes(h.canonical_json_bytes(detail))
            seal(receipt)
            with self.subTest(mutation=mutation), self.assertRaises(contract.CandidateContractError):
                contract.validate_aggregate_receipt(receipt)

    def test_profile_scope_tampering_and_missing_draft_reject_at_consumer(self):
        for mutation in ('session', 'source', 'missing'):
            receipt = copy.deepcopy(self.receipt)
            if mutation == 'missing':
                receipt['profile_receipts'].pop('FRESH_BASE')
            else:
                detail = receipt['profile_receipts']['FRESH_BASE']
                detail['session_id' if mutation == 'session' else 'source_sha'] = 'f' * (32 if mutation == 'session' else 40)
                receipt['profile_results']['fresh_base']['receipt_digest'] = h.sha256_bytes(h.canonical_json_bytes(detail))
            seal(receipt)
            with self.subTest(mutation=mutation), self.assertRaises(contract.CandidateContractError):
                contract.validate_aggregate_receipt(receipt)

    def test_cli_freshness_and_publish_each_reject_a_resealed_wrong_session(self):
        receipt = copy.deepcopy(self.receipt)
        receipt['session_id'] = 'f' * 32
        seal(receipt)
        encoded = h.canonical_json_bytes(receipt)
        b64 = base64.urlsafe_b64encode(encoded).decode('ascii').rstrip('=')
        with self.assertRaises(contract.CandidateContractError):
            cli._decode_candidate_acceptance_receipt(SimpleNamespace(value=b64, output=self.root / 'rejected.json'))
        self.assertFalse((self.root / 'rejected.json').exists())
        input_path = self.root / 'untrusted.json'
        input_path.write_bytes(encoded)
        identity = SimpleNamespace(qualification_run_id=receipt['qualification_run_id'],
            candidate_sha=receipt['source_sha'], candidate_tree=receipt['source_tree'],
            candidate_version=receipt['candidate_version'], candidate_acceptance_receipt_sha256=h.sha256_bytes(encoded))
        with self.assertRaises(freshness.MetadataFreshnessError):
            freshness._load_candidate_acceptance_receipt(input_path, identity=identity,
                current_time=datetime(2030, 1, 1, tzinfo=timezone.utc))
        with self.assertRaises(PublicationInputError):
            build_publish_candidate_plan(self.loaded, receipt)

    def test_boolean_cannot_replace_numeric_origin_count_even_with_outer_resealing(self):
        for reseal_inner in (False, True):
            receipt = copy.deepcopy(self.receipt)
            pre = receipt['r2_origin_prestate_receipt']
            pre['bucket_get_count'] = True
            if reseal_inner:
                seal(pre)
            receipt['r2_origin_prestate_receipt_digest'] = h.sha256_bytes(h.canonical_json_bytes(pre))
            seal(receipt)
            with self.subTest(reseal_inner=reseal_inner), self.assertRaises(contract.CandidateContractError):
                contract.validate_aggregate_receipt(receipt)

    def test_large_complete_receipt_fits_dispatch_and_preserves_exact_original_bytes(self):
        receipt = copy.deepcopy(self.receipt)
        hashes = {('snapshot-disk-with-long-name-' + str(index) + '.vmdk'):
            'sha256:' + hashlib.sha256(str(index).encode()).hexdigest() for index in range(63)}
        receipt['original_vm_hashes'] = hashes
        receipt['base_vm_identity'] = h.sha256_bytes(h.canonical_json_bytes(hashes))
        for name, detail in receipt['profile_receipts'].items():
            detail['base_vm_identity'] = receipt['base_vm_identity']
            detail['original_vm_pre_hashes'] = hashes
            detail['original_vm_post_hashes'] = hashes
            receipt['profile_results'][name.lower()]['receipt_digest'] = h.sha256_bytes(h.canonical_json_bytes(detail))
        seal(receipt)
        raw = h.canonical_json_bytes(receipt)
        self.assertGreater(len(base64.urlsafe_b64encode(raw)), 65535)
        wire = contract.encode_aggregate_receipt_b64url(receipt)
        self.assertLessEqual(len(wire), 48 * 1024)
        decoded, encoded = contract.decode_aggregate_receipt_b64url(wire)
        self.assertEqual(decoded, receipt)
        self.assertEqual(encoded, raw)
        self.consume(receipt, 'large-valid')

    def test_compressed_wire_rejects_wrong_digest_size_trailing_stream_and_bomb(self):
        wire = contract.encode_aggregate_receipt_b64url(self.receipt)
        envelope = json.loads(base64.urlsafe_b64decode(wire + '=' * (-len(wire) % 4)))
        for mutation in ('digest', 'size', 'trailing', 'truncated', 'bomb', 'extra', 'boolean'):
            value = copy.deepcopy(envelope)
            compressed = base64.urlsafe_b64decode(value['payload'] + '=' * (-len(value['payload']) % 4))
            if mutation == 'digest': value['receipt_sha256'] = 'sha256:' + 'f' * 64
            elif mutation == 'size': value['receipt_bytes'] -= 1
            elif mutation == 'extra': value['arbitrary'] = True
            elif mutation == 'boolean': value['receipt_bytes'] = True
            else:
                if mutation == 'trailing': compressed += zlib.compress(b'{}')
                elif mutation == 'truncated': compressed = compressed[:-1]
                else:
                    compressed = zlib.compress(b'x' * (contract.MAX_RECEIPT_JSON_BYTES + 1))
                    value['receipt_bytes'] = contract.MAX_RECEIPT_JSON_BYTES
                value['payload'] = base64.urlsafe_b64encode(compressed).decode().rstrip('=')
            bad = base64.urlsafe_b64encode(h.canonical_json_bytes(value)).decode().rstrip('=')
            with self.subTest(mutation=mutation), self.assertRaises(contract.CandidateContractError):
                contract.decode_aggregate_receipt_b64url(bad)


if __name__ == '__main__':
    unittest.main()
