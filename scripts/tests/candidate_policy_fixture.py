"""Explicit synthetic v5 consumer input; never evidence of Guest execution."""
import copy
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from release import candidate as c, r2_plugin_origin as origin
from release.candidate_failure_policy import FAILURE_POLICY
from scripts.candidate_receipt_regression import load_fixture
from scripts.tests.test_r2_plugin_origin import response


def current_policy_receipt(base):
    value = copy.deepcopy(base)
    source = load_fixture(Path(__file__).parent / 'fixtures/candidate-wire-real-scale.json.xz')
    value.update(schema=c.AGGREGATE_RECEIPT_SCHEMA, version=5, failure_policy=FAILURE_POLICY,
        session_id='1' * 32,
        plan_digest=c.sha256_bytes(b'NON_AUTHORITATIVE_SYNTHETIC_POLICY_PLAN\n' + c.canonical_json_bytes(base)))
    end = datetime.fromisoformat(value['completed_at'].replace('Z', '+00:00'))
    scope = SimpleNamespace(**{key: value[key] for key in ('source_sha', 'source_tree',
        'qualification_run_id', 'candidate_version', 'verified_candidate_digest', 'plan_digest', 'session_id')})
    for role, seconds in (('PRESTATE', 40), ('POSTSTATE', 1)):
        now = end - timedelta(seconds=seconds)
        with mock.patch.object(origin, '_now', return_value=now):
            request = origin.make_request(scope, role)
            receipt = origin.validate_plugin_response(response(request), request, observed_at=now)
        prefix = 'r2_origin_' + role.lower()
        value[prefix + '_receipt'] = receipt
        value[prefix + '_receipt_digest'] = c.sha256_bytes(c.canonical_json_bytes(receipt))
        value[prefix + '_observation_id'] = receipt['observation_id']
    value['profile_receipts'] = {}
    for name, detail in source['profileReceipts'].items():
        for key in ('candidate_input_digest', 'verified_candidate_digest', 'qualification_run_id',
                'qualification_run_attempt', 'source_sha', 'source_tree', 'candidate_version',
                'base_vm_identity', 'source_vm_inventory_identity', 'source_disk_graph_identity',
                'plan_digest', 'session_id'):
            detail[key] = value[key]
        detail.update(snapshot_identity=value['snapshot_identities'][name],
            snapshot_disk_graph_identity=value['snapshot_disk_graph_identities'][name],
            original_vm_pre_hashes=value['original_vm_hashes'], original_vm_post_hashes=value['original_vm_hashes'],
            started_at=(end - timedelta(seconds=30)).isoformat().replace('+00:00', 'Z'),
            completed_at=(end - timedelta(seconds=2)).isoformat().replace('+00:00', 'Z'))
        value['profile_receipts'][name] = c.validate_profile_receipt(detail)
        value['profile_results'][name.lower()]['receipt_digest'] = c.sha256_bytes(c.canonical_json_bytes(detail))
    resign(value)
    return c.validate_aggregate_receipt(value)


def resign(value):
    body = dict(value)
    body.pop('receipt_digest')
    value['receipt_digest'] = c.sha256_bytes(c.canonical_json_bytes(body))
    return value


def fail_profile(value, name='FRESH_BASE'):
    detail = value['profile_receipts'][name]
    detail['installer_execution_result'] = 'FAIL'
    detail['result'] = 'FAIL'
    value['profile_results'][name.lower()] = dict(status='FAIL',
        failure_code='CANDIDATE_PROFILE_REPORTED_FAILURE',
        receipt_digest=c.sha256_bytes(c.canonical_json_bytes(detail)))
    value.update(all_profiles_pass=False, result='FAIL')
    return resign(value)
