from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest import mock

from release import r2_plugin_origin as r
from scripts import isolated_guest_validation as entry


def plan():
    return SimpleNamespace(source_sha='a' * 40, source_tree='b' * 40,
        verified_candidate_digest='sha256:' + 'c' * 64, plan_digest='sha256:' + 'd' * 64,
        candidate_version='v2.0.0-rc.1', qualification_run_id=42, session_id='e' * 32)


def response(request):
    base = f'/accounts/{request["account_id"]}/r2/buckets/{request["bucket"]}'
    stamp = r._stamp(r._now())
    return {'schema': r.RESPONSE_SCHEMA, 'producer': r.PRODUCER,
        **{key: request[key] for key in ('request_id', 'request_digest', 'collector_sha256')},
        'started_at': stamp, 'completed_at': stamp, 'failure': None,
        'bucket_read': {'method': 'GET', 'path': base, 'status': 200, 'success': True,
                        'errors': [], 'name': request['bucket'], 'jurisdiction': 'default'},
        'list_reads': [{'method': 'GET', 'path': base + '/objects',
            'query': {'prefix': request['prefix'], 'per_page': 1000},
            'status': 200, 'success': True, 'errors': [], 'objects': [],
            'pagination': None, 'pagination_present': False}],
        'object_reads': [{'method': 'GET', 'path': base + '/objects/' + key, 'key': key,
            'outcome': 'KEY_NOT_FOUND', 'http_status': None, 'cloudflare_error_code': 10007}
            for key in request['expected_keys']]}


class PluginOriginTests(unittest.TestCase):
    def setUp(self):
        self.request = r.make_request(plan(), 'PRESTATE')
        self.value = response(self.request)

    def test_rest_identity_and_exact_get_counts(self):
        receipt = r.validate_plugin_response(self.value, self.request)
        self.assertEqual(receipt['auth_method'], 'CLOUDFLARE_PLUGIN_REST')
        self.assertEqual((receipt['bucket_get_count'], receipt['list_get_count'], receipt['object_get_count']), (1, 1, 6))
        self.assertTrue(receipt['pagination_metadata_omitted'])
        self.assertFalse(receipt['publish_authorized'] or receipt['release_authority_granted'])
        self.assertFalse(any('head_object' in name for name in receipt))

    def test_every_fixed_object_is_required_without_fabricated_http_status(self):
        for mutation in ('missing', 'duplicate', 'http404', 'wrong_error', 'present'):
            with self.subTest(mutation=mutation):
                value = copy.deepcopy(self.value)
                objects = value['object_reads']
                if mutation == 'missing': objects.pop()
                if mutation == 'duplicate': objects[-1] = copy.deepcopy(objects[0])
                if mutation == 'http404': objects[0]['http_status'] = 404
                if mutation == 'wrong_error': objects[0]['cloudflare_error_code'] = 10000
                if mutation == 'present': objects[0]['outcome'] = 'PRESENT'
                with self.assertRaises(r.R2PluginOriginError):
                    r.validate_plugin_response(value, self.request)

    def test_nonempty_prefix_and_common_prefix_stop(self):
        for grouped in (False, True):
            value = copy.deepcopy(self.value)
            if grouped:
                value['list_reads'][0]['pagination'] = {'delimited': [self.request['prefix'] + 'temp/']}
                value['list_reads'][0]['pagination_present'] = True
            else:
                value['list_reads'][0]['objects'] = [{'key': self.request['expected_keys'][0], 'size': 1}]
            with self.assertRaisesRegex(r.R2PluginOriginError, 'PREFIX_NON_EMPTY'):
                r.validate_plugin_response(value, self.request)

    def test_permission_auth_unknown_errors_never_mean_missing(self):
        for status in (401, 403, 404, 500):
            value = copy.deepcopy(self.value)
            value['list_reads'][0]['status'] = status
            with self.assertRaisesRegex(r.R2PluginOriginError, 'API_READ_FAILED'):
                r.validate_plugin_response(value, self.request)
        self.value['failure'] = 'arbitrary untrusted provider text'
        with self.assertRaisesRegex(r.R2PluginOriginError, '^R2_PLUGIN_API_READ_FAILED$'):
            r.validate_plugin_response(self.value, self.request)

    def test_scope_transport_and_method_cannot_be_overridden(self):
        for part, key, val in (('bucket_read', 'name', 'media'), ('bucket_read', 'jurisdiction', 'eu'),
                              ('bucket_read', 'path', '/another/account'), ('bucket_read', 'method', 'PUT')):
            value = copy.deepcopy(self.value)
            value[part][key] = val
            with self.assertRaises(r.R2PluginOriginError):
                r.validate_plugin_response(value, self.request)
        self.value['list_reads'][0]['query']['delimiter'] = '/'
        with self.assertRaises(r.R2PluginOriginError):
            r.validate_plugin_response(self.value, self.request)

    def test_request_source_and_role_binding_rejects_replay(self):
        for field, val in (('source_sha', 'f' * 40), ('source_tree', 'f' * 40), ('candidate_version', 'v2.0.0-rc.2')):
            changed = plan()
            setattr(changed, field, val)
            expected = r.make_request(changed, 'PRESTATE')
            with self.assertRaisesRegex(r.R2PluginOriginError, 'REQUEST_MISMATCH'):
                r.validate_plugin_response(self.value, expected)
        post = r.make_request(plan(), 'POSTSTATE')
        with self.assertRaisesRegex(r.R2PluginOriginError, 'REQUEST_MISMATCH'):
            r.validate_plugin_response(self.value, post)

    def test_expired_and_future_observations_rejected(self):
        for field, stamp in (('started_at', '2000-01-01T00:00:00.000Z'), ('completed_at', '2099-01-01T00:00:00.000Z')):
            value = copy.deepcopy(self.value)
            value[field] = stamp
            with self.assertRaisesRegex(r.R2PluginOriginError, 'EXPIRED'):
                r.validate_plugin_response(value, self.request)
        with mock.patch.object(r, '_now', return_value=r._time(self.request['expires_at']) + r.timedelta(seconds=1)):
            with self.assertRaisesRegex(r.R2PluginOriginError, 'EXPIRED'):
                r.validate_plugin_response(self.value, self.request)

    def test_complete_pagination_and_cursor_loop_rejection(self):
        first = self.value['list_reads'][0]
        first['pagination'] = {'cursor': 'synthetic-next', 'is_truncated': True}
        first['pagination_present'] = True
        second = copy.deepcopy(first)
        second['query']['cursor'] = 'synthetic-next'
        second['pagination'] = {'is_truncated': False}
        self.value['list_reads'].append(second)
        self.assertEqual(r.validate_plugin_response(self.value, self.request)['list_get_count'], 2)
        second['pagination'] = first['pagination']
        with self.assertRaisesRegex(r.R2PluginOriginError, 'PAGINATION_INVALID'):
            r.validate_plugin_response(self.value, self.request)

    def test_incomplete_pagination_rejected(self):
        self.value['list_reads'][0]['pagination'] = {'is_truncated': True, 'cursor': 'more'}
        self.value['list_reads'][0]['pagination_present'] = True
        with self.assertRaisesRegex(r.R2PluginOriginError, 'PAGINATION_INVALID'):
            r.validate_plugin_response(self.value, self.request)

    def test_duplicate_or_extra_json_properties_rejected(self):
        with self.assertRaises(r.R2PluginOriginError):
            r._strict_json(b'{"key":1,"key":2}')
        with self.assertRaises(r.R2PluginOriginError):
            r._strict_json(b'{"key":NaN}')
        self.value['authorization'] = 'untrusted'
        with self.assertRaises(r.R2PluginOriginError):
            r.validate_plugin_response(self.value, self.request)

    def test_request_is_not_visible_until_complete_publication(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / 'PRESTATE.request.json'
            data = r.canonical_json_bytes(self.request)
            original_open = Path.open
            class SlowWriter:
                def __init__(self, file): self.file = file
                def __enter__(self): return self
                def __exit__(self, *args): return self.file.__exit__(*args)
                def __getattr__(self, name): return getattr(self.file, name)
                def write(inner, value):
                    middle = len(value) // 2
                    inner.file.write(value[:middle])
                    inner.file.flush()
                    self.assertFalse(target.exists(), 'Observer must not see a partial final request')
                    inner.file.write(value[middle:])
                    return len(value)
            def open_file(path, *args, **kwargs):
                file = original_open(path, *args, **kwargs)
                return SlowWriter(file) if path == target.with_suffix('.tmp') else file
            with mock.patch.object(Path, 'open', new=open_file):
                r._publish_request(target, data)
            self.assertEqual(target.read_bytes(), data)
            self.assertFalse(target.with_suffix('.tmp').exists())

    def test_explicit_plugin_entry_never_calls_s3(self):
        with tempfile.TemporaryDirectory() as temporary, mock.patch.object(r, 'CHANNEL_PARENT', Path(temporary)):
            channel = r.CloudflarePluginOrigin()
            with mock.patch.object(channel, 'observe', return_value={'observation_id': 'synthetic'}) as observed, mock.patch.object(entry.h, 'verify_candidate_r2_origin_from_environment') as s3:
                value = entry._origin(plan(), 'PRESTATE', plugin_origin=channel)
            self.assertEqual(value['observation_id'], 'synthetic')
            observed.assert_called_once()
            s3.assert_not_called()
        with self.assertRaisesRegex(entry.ControllerFailure, 'CHANNEL_INVALID'):
            entry._origin(plan(), 'PRESTATE', plugin_origin=lambda: None)

    def test_real_private_channel_roundtrip_and_no_role_reuse(self):
        with tempfile.TemporaryDirectory(dir='E:/' if os.name == 'nt' else None) as temporary, mock.patch.object(r, 'CHANNEL_PARENT', Path(temporary)):
            with r.CloudflarePluginOrigin() as channel:
                errors = []
                def observer():
                    try:
                        request_path = channel.root / 'PRESTATE.request.json'
                        deadline = time.monotonic() + 5
                        while not request_path.exists() and time.monotonic() < deadline:
                            time.sleep(0.01)
                        request = json.loads(request_path.read_bytes())
                        staging = channel.root / 'response.tmp'
                        staging.write_bytes(r.canonical_json_bytes(response(request)))
                        staging.rename(channel.root / 'PRESTATE.response.json')
                    except BaseException as error:
                        errors.append(type(error).__name__)
                worker = threading.Thread(target=observer)
                worker.start()
                receipt = channel.observe(plan(), 'PRESTATE')
                worker.join(5)
                self.assertFalse(worker.is_alive() or errors)
                self.assertEqual(receipt['result'], 'PROVEN_EMPTY')
                with self.assertRaisesRegex(r.R2PluginOriginError, 'REPLAY'):
                    channel.observe(plan(), 'PRESTATE')
            with self.assertRaisesRegex(r.R2PluginOriginError, 'CHANNEL_CLOSED'):
                channel.observe(plan(), 'POSTSTATE')

    def test_poststate_requires_prestate_and_times_out_without_new_response(self):
        with tempfile.TemporaryDirectory(dir='E:/' if os.name == 'nt' else None) as temporary, mock.patch.object(r, 'CHANNEL_PARENT', Path(temporary)):
            with r.CloudflarePluginOrigin() as channel:
                with self.assertRaisesRegex(r.R2PluginOriginError, 'REPLAY'):
                    channel.observe(plan(), 'POSTSTATE')
                with mock.patch.object(r, 'WAIT_SECONDS', 0):
                    with self.assertRaisesRegex(r.R2PluginOriginError, 'TIMEOUT'):
                        channel.observe(plan(), 'PRESTATE')

    def test_collector_actual_javascript_uses_only_fixed_gets_and_classifies_missing(self):
        # The scripts-test CI job installs Node. This executes the real collector
        # against a synthetic API; no network or VM operation is available here.
        code = """
const fs = require('fs');
const request = JSON.parse(fs.readFileSync(0, 'utf8'));
const collect = eval('(' + fs.readFileSync(process.argv[1], 'utf8') + ')');
const calls = [];
const cloudflare = {request: async options => {
  calls.push(options);
  if (options.path.endsWith('/objects')) return {success:true,status:200,errors:[],result:[]};
  if (options.path.includes('/objects/')) throw new Error('Cloudflare API error: 10007: The specified key does not exist.');
  return {success:true,status:200,errors:[],result:{name:'animemo-release-mirror',jurisdiction:'default'}};
}};
collect(request, cloudflare, request.account_id).then(result => process.stdout.write(JSON.stringify({result,calls})));
"""
        run = subprocess.run(['node', '-e', code, str(r.COLLECTOR)], input=json.dumps(self.request),
                             capture_output=True, text=True, timeout=20, check=True)
        value = json.loads(run.stdout)
        self.assertEqual(len(value['calls']), 8)
        self.assertTrue(all(call['method'] == 'GET' for call in value['calls']))
        self.assertEqual(r.validate_plugin_response(value['result'], self.request)['result'], 'PROVEN_EMPTY')

    def test_collector_rejects_realistic_error_and_pagination_cases(self):
        code = """
const fs = require('fs');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const collect = eval('(' + fs.readFileSync(process.argv[1], 'utf8') + ')');
(async () => {
  const outcomes = [];
  for (const mode of ['forbidden','nonempty','unknown-key-error','cursor-loop','wrong-scope']) {
    const request = JSON.parse(JSON.stringify(input));
    if (mode === 'wrong-scope') request.bucket = 'media';
    let calls = 0;
    const api = {request: async options => {
      calls++;
      if (options.method !== 'GET') throw new Error('write attempted');
      if (options.path.endsWith('/objects')) {
        if (mode === 'forbidden') throw new Error('Cloudflare API error: 10000: Authentication error');
        if (mode === 'nonempty') return {success:true,status:200,errors:[],result:[{key:request.expected_keys[0],size:1}]};
        if (mode === 'cursor-loop') return {success:true,status:200,errors:[],result:[],result_info:{is_truncated:true,cursor:'same'}};
        return {success:true,status:200,errors:[],result:[]};
      }
      if (options.path.includes('/objects/')) throw new Error('untrusted provider error');
      return {success:true,status:200,errors:[],result:{name:'animemo-release-mirror',jurisdiction:'default'}};
    }};
    const value = await collect(request, api, input.account_id);
    outcomes.push({mode, failure:value.failure, calls, getCount:value.object_reads.length});
  }
  process.stdout.write(JSON.stringify(outcomes));
})();
"""
        run = subprocess.run(['node', '-e', code, str(r.COLLECTOR)], input=json.dumps(self.request),
                             capture_output=True, text=True, timeout=20, check=True)
        values = {item['mode']: item for item in json.loads(run.stdout)}
        self.assertEqual(values['forbidden']['failure'], 'PLUGIN_API_FAILED')
        self.assertEqual(values['nonempty']['failure'], 'PREFIX_NON_EMPTY')
        self.assertEqual(values['nonempty']['getCount'], 0)
        self.assertEqual(values['unknown-key-error']['failure'], 'GET_FAILED')
        self.assertEqual(values['cursor-loop']['failure'], 'PAGINATION_INVALID')
        self.assertEqual(values['wrong-scope']['calls'], 0)


if __name__ == '__main__':
    unittest.main()
