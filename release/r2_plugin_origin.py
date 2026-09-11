"""Task-approved Cloudflare plugin observations for the isolated Guest entry.

The connected plugin host is the trusted observer. A local digest binds its
reported reads to a pending request; it is not a Cloudflare signature. This
channel contains public observation data and grants no release authority.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import stat
import time
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

from release.candidate import canonical_json_bytes, sha256_bytes
from release.contract import rc_target_version
from release.formal_windows_pretrust import (
    create_windows_private_directory,
    hold_windows_private_file,
    hold_windows_private_path_chain,
)
from release.r2_prestate import (
    R2_ACCOUNT_ID_SHA256, R2_BUCKET, REPOSITORY,
    candidate_r2_expected_keys, candidate_r2_prefix,
)

ACCOUNT_ID = 'd6a6e23b63921b1ed386645022fa92a7'
REQUEST_SCHEMA = 'animemo.cloudflare-plugin-origin-request/v1'
RESPONSE_SCHEMA = 'animemo.cloudflare-plugin-origin-response/v1'
RECEIPT_SCHEMA = 'animemo.cloudflare-plugin-origin-receipt/v2'
PRODUCER = 'CODEX_CLOUDFLARE_PLUGIN'
WAIT_SECONDS = 300
MAX_RESPONSE_BYTES = 256 * 1024
MAX_LIST_PAGES = 64
COLLECTOR = Path(__file__).with_name('r2_plugin_collect.js')
CHANNEL_PARENT = Path('E:/')
_SHA = re.compile(r'[0-9a-f]{40}\Z')
_DIGEST = re.compile(r'sha256:[0-9a-f]{64}\Z')


class R2PluginOriginError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _require(condition, code='R2_PLUGIN_RESPONSE_INVALID'):
    if not condition:
        raise R2PluginOriginError(code)


def _now():
    return datetime.now(timezone.utc)


def _stamp(value):
    return value.isoformat(timespec='milliseconds').replace('+00:00', 'Z')


def _time(value):
    _require(isinstance(value, str) and value.endswith('Z'))
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        raise R2PluginOriginError('R2_PLUGIN_RESPONSE_INVALID') from None
    _require(parsed.tzinfo is not None)
    return parsed


def _keys(value, expected):
    _require(type(value) is dict and set(value) == set(expected))


def _strict_json(data):
    def pairs(items):
        value = {}
        for key, item in items:
            _require(key not in value)
            value[key] = item
        return value
    try:
        return json.loads(data, object_pairs_hook=pairs,
                          parse_constant=lambda _: (_ for _ in ()).throw(R2PluginOriginError('R2_PLUGIN_RESPONSE_INVALID')))
    except (ValueError, UnicodeError):
        raise R2PluginOriginError('R2_PLUGIN_RESPONSE_INVALID') from None


def _publish_request(path, data):
    staging = path.with_suffix('.tmp')
    _require(not path.exists() and not path.is_symlink(), 'R2_PLUGIN_REQUEST_CHANGED')
    with staging.open('xb') as output:
        output.write(data)
        output.flush()
        os.fsync(output.fileno())
    # Same-directory rename publishes the complete request in one operation.
    staging.rename(path)


def make_request(plan, role):
    _require(role in {'PRESTATE', 'POSTSTATE'}, 'R2_PLUGIN_ROLE_INVALID')
    _require(_SHA.fullmatch(plan.source_sha) and _SHA.fullmatch(plan.source_tree)
             and _DIGEST.fullmatch(plan.verified_candidate_digest)
             and _DIGEST.fullmatch(plan.plan_digest), 'R2_PLUGIN_SCOPE_INVALID')
    _require(sha256_bytes(ACCOUNT_ID.encode('ascii')) == R2_ACCOUNT_ID_SHA256, 'R2_PLUGIN_ACCOUNT_INVALID')
    rc_target_version(plan.candidate_version)
    started = _now()
    request = {
        'schema': REQUEST_SCHEMA, 'request_id': str(uuid4()), 'role': role,
        'repository': REPOSITORY, 'source_sha': plan.source_sha, 'source_tree': plan.source_tree,
        'candidate_version': plan.candidate_version, 'verified_candidate_digest': plan.verified_candidate_digest,
        'qualification_run_id': plan.qualification_run_id, 'plan_digest': plan.plan_digest,
        'session_id': plan.session_id, 'account_id': ACCOUNT_ID, 'bucket': R2_BUCKET,
        'jurisdiction': 'default', 'prefix': candidate_r2_prefix(plan.candidate_version),
        'expected_keys': [candidate_r2_prefix(plan.candidate_version) + name
                          for name in candidate_r2_expected_keys(plan.candidate_version)],
        'created_at': _stamp(started), 'expires_at': _stamp(started + timedelta(seconds=WAIT_SECONDS)),
        'collector_sha256': sha256_bytes(COLLECTOR.read_bytes()),
    }
    request['request_digest'] = sha256_bytes(canonical_json_bytes(request))
    return request


def validate_plugin_request(request):
    _keys(request, ('schema', 'request_id', 'role', 'repository', 'source_sha', 'source_tree',
                    'candidate_version', 'verified_candidate_digest', 'qualification_run_id',
                    'plan_digest', 'session_id', 'account_id', 'bucket', 'jurisdiction',
                    'prefix', 'expected_keys', 'created_at', 'expires_at', 'collector_sha256',
                    'request_digest'))
    _require(request['schema'] == REQUEST_SCHEMA and request['role'] in {'PRESTATE', 'POSTSTATE'})
    try:
        nonce = UUID(request['request_id'])
        _require(nonce.version == 4 and str(nonce) == request['request_id'])
        for key in ('source_sha', 'source_tree'):
            _require(type(request[key]) is str and _SHA.fullmatch(request[key]))
        for key in ('verified_candidate_digest', 'plan_digest', 'collector_sha256'):
            _require(type(request[key]) is str and _DIGEST.fullmatch(request[key]))
        rc_target_version(request['candidate_version'])
    except (ValueError, TypeError, AttributeError):
        raise R2PluginOriginError('R2_PLUGIN_SCOPE_INVALID') from None
    _require(type(request['qualification_run_id']) is int and request['qualification_run_id'] > 0)
    _require(type(request['session_id']) is str and re.fullmatch(r'[0-9a-f]{32}', request['session_id']))
    _require(request['repository'] == REPOSITORY and request['account_id'] == ACCOUNT_ID
             and request['bucket'] == R2_BUCKET and request['jurisdiction'] == 'default')
    prefix = candidate_r2_prefix(request['candidate_version'])
    _require(request['prefix'] == prefix and request['expected_keys'] ==
             [prefix + key for key in candidate_r2_expected_keys(request['candidate_version'])])
    _require(_time(request['expires_at']) - _time(request['created_at']) == timedelta(seconds=300))
    unsigned = dict(request)
    unsigned.pop('request_digest')
    _require(request['request_digest'] == sha256_bytes(canonical_json_bytes(unsigned)), 'R2_PLUGIN_REQUEST_MISMATCH')
    return request


def validate_plugin_response(value, request, *, observed_at=None):
    """Validate a complete, fresh REST observation; never coerce it into S3."""
    validate_plugin_request(request)
    _keys(value, ('schema', 'producer', 'request_id', 'request_digest', 'collector_sha256',
                  'started_at', 'completed_at', 'bucket_read', 'list_reads', 'object_reads', 'failure'))
    _require(value['schema'] == RESPONSE_SCHEMA and value['producer'] == PRODUCER)
    _require(all(value[name] == request[name] for name in ('request_id', 'request_digest', 'collector_sha256')),
             'R2_PLUGIN_REQUEST_MISMATCH')
    _require(str(UUID(value['request_id'])) == request['request_id'])
    start, end, now = _time(value['started_at']), _time(value['completed_at']), observed_at or _now()
    _require(_time(request['created_at']) - timedelta(seconds=5) <= start <= end
             <= now + timedelta(seconds=5) and end <= _time(request['expires_at'])
             and now <= _time(request['expires_at']) and now - end <= timedelta(seconds=120),
             'R2_PLUGIN_OBSERVATION_EXPIRED')
    if value['failure'] is not None:
        codes = {'PREFIX_NON_EMPTY': 'R2_PLUGIN_PREFIX_NON_EMPTY',
                 'PAGINATION_INVALID': 'R2_PLUGIN_PAGINATION_INVALID',
                 'SCOPE_INVALID': 'R2_PLUGIN_SCOPE_INVALID'}
        raise R2PluginOriginError(codes.get(value['failure'], 'R2_PLUGIN_API_READ_FAILED')
                                  if isinstance(value['failure'], str) else 'R2_PLUGIN_API_READ_FAILED')
    base = f'/accounts/{request["account_id"]}/r2/buckets/{request["bucket"]}'
    bucket = value['bucket_read']
    _keys(bucket, ('method', 'path', 'status', 'success', 'errors', 'name', 'jurisdiction'))
    _require(bucket['success'] is True and type(bucket['status']) is int)
    _require(bucket == {'method': 'GET', 'path': base, 'status': 200, 'success': True,
                       'errors': [], 'name': request['bucket'], 'jurisdiction': request['jurisdiction']},
             'R2_PLUGIN_BUCKET_UNVERIFIED')
    pages = value['list_reads']
    _require(type(pages) is list and 1 <= len(pages) <= MAX_LIST_PAGES)
    cursor = None
    seen = set()
    pagination_omitted = False
    for index, page in enumerate(pages):
        _keys(page, ('method', 'path', 'query', 'status', 'success', 'errors', 'objects', 'pagination', 'pagination_present'))
        _require(type(page['pagination_present']) is bool)
        query = {'prefix': request['prefix'], 'per_page': 1000}
        if cursor is not None:
            query['cursor'] = cursor
        _require(page['method'] == 'GET' and page['path'] == base + '/objects' and page['query'] == query)
        _require(type(page['status']) is int and page['status'] == 200
                 and page['success'] is True and page['errors'] == [], 'R2_PLUGIN_API_READ_FAILED')
        _require(type(page['objects']) is list)
        _require(not page['objects'], 'R2_PLUGIN_PREFIX_NON_EMPTY')
        info = page['pagination']
        if info is None:
            pagination_omitted = pagination_omitted or not page['pagination_present']
            # The API's result_info is optional. Preserve its absence; an empty
            # list without grouping/offset plus six missing-key reads may end it.
            _require(index == len(pages) - 1)
            cursor = None
            continue
        _require(type(info) is dict and set(info) <= {'cursor', 'is_truncated', 'delimited', 'per_page'})
        _require(type(info.get('delimited', [])) is list)
        _require(not info.get('delimited', []), 'R2_PLUGIN_PREFIX_NON_EMPTY')
        truncated = info.get('is_truncated', False)
        _require(type(truncated) is bool)
        next_cursor = info.get('cursor')
        if truncated or next_cursor:
            _require(isinstance(next_cursor, str) and 0 < len(next_cursor) <= 4096
                     and next_cursor not in seen and index < len(pages) - 1, 'R2_PLUGIN_PAGINATION_INVALID')
            seen.add(next_cursor)
            cursor = next_cursor
        else:
            _require(index == len(pages) - 1, 'R2_PLUGIN_PAGINATION_INVALID')
            cursor = None
    objects = value['object_reads']
    _require(type(objects) is list and len(objects) == len(request['expected_keys']))
    for item, key in zip(objects, request['expected_keys'], strict=True):
        _keys(item, ('method', 'path', 'key', 'outcome', 'http_status', 'cloudflare_error_code'))
        _require(item['method'] == 'GET' and item['path'] == base + '/objects/' + key and item['key'] == key)
        if item['outcome'] == 'PRESENT':
            raise R2PluginOriginError('R2_PLUGIN_PREFIX_NON_EMPTY')
        _require(item['outcome'] == 'KEY_NOT_FOUND' and item['cloudflare_error_code'] == 10007
                 and item['http_status'] is None, 'R2_PLUGIN_API_READ_FAILED')
    receipt = {
        'schema': RECEIPT_SCHEMA, 'observation_id': request['request_id'], 'observation_role': request['role'],
        'source_sha': request['source_sha'], 'source_tree': request['source_tree'],
        'candidate_version': request['candidate_version'], 'plan_digest': request['plan_digest'],
        'qualification_run_id': request['qualification_run_id'], 'session_id': request['session_id'],
        'verified_candidate_digest': request['verified_candidate_digest'], 'request_digest': request['request_digest'],
        'response_digest': sha256_bytes(canonical_json_bytes(value)), 'account_id': request['account_id'],
        'bucket': request['bucket'], 'prefix': request['prefix'], 'jurisdiction': request['jurisdiction'],
        'endpoint_host': 'api.cloudflare.com', 'auth_method': 'CLOUDFLARE_PLUGIN_REST',
        'observation_producer': PRODUCER, 'bucket_get_count': 1, 'list_get_count': len(pages),
        'object_get_count': len(objects), 'write_request_count': 0, 'pagination_metadata_omitted': pagination_omitted,
        'started_at': value['started_at'], 'completed_at': value['completed_at'], 'result': 'PROVEN_EMPTY',
        'release_authority_granted': False, 'publish_authorized': False,
        'request': request, 'response': value,
    }
    receipt['receipt_digest'] = sha256_bytes(canonical_json_bytes(receipt))
    return receipt


def validate_plugin_receipt(receipt, *, expected_scope, role):
    """Recheck retained evidence at observation time, without renewing its TTL.

    Integrity digests are not signatures. Authenticity comes from the held
    plugin exchange and the canonical Host receipt producer.
    """
    _require(type(receipt) is dict and type(receipt.get('response')) is dict)
    expected = validate_plugin_response(receipt['response'], receipt.get('request'),
        observed_at=_time(receipt['response'].get('completed_at')))
    # Python object equality equates True with 1 and False with 0. Compare
    # canonical bytes so typed fields and the receipt's own digest both bind.
    _require(canonical_json_bytes(receipt) == canonical_json_bytes(expected), 'R2_PLUGIN_RECEIPT_MISMATCH')
    _require(receipt['observation_role'] == role and all(
        receipt[key] == expected_scope[key] for key in
        ('source_sha', 'source_tree', 'candidate_version', 'verified_candidate_digest',
         'qualification_run_id', 'plan_digest', 'session_id')), 'R2_PLUGIN_REQUEST_MISMATCH')
    return receipt


class CloudflarePluginOrigin:
    """One held, bounded PRESTATE/POSTSTATE exchange with the approved host."""
    def __init__(self):
        self.parent = CHANNEL_PARENT.resolve(strict=True)
        self.root = None
        self._hold = None
        self._attempted = set()
        self._prestate = None

    def __enter__(self):
        self.root = create_windows_private_directory(self.parent, prefix='r2-plugin-origin')
        self._hold = hold_windows_private_path_chain(self.root, allow_leaf_child_writes=True)
        self._hold.__enter__()
        return self

    def __exit__(self, *args):
        try:
            return self._hold.__exit__(*args)
        finally:
            self._hold = None

    def observe(self, plan, role):
        _require(self._hold is not None and self.root is not None, 'R2_PLUGIN_CHANNEL_CLOSED')
        _require(role not in self._attempted and (role == 'PRESTATE' or self._prestate is not None),
                 'R2_PLUGIN_OBSERVATION_REPLAY')
        self._attempted.add(role)
        request = make_request(plan, role)
        if role == 'POSTSTATE':
            _require(_time(request['created_at']) >= _time(self._prestate['completed_at']), 'R2_PLUGIN_OBSERVATION_REPLAY')
            _require(all(request[key] == self._prestate[key] for key in
                         ('plan_digest', 'session_id', 'qualification_run_id', 'source_sha', 'source_tree', 'candidate_version', 'verified_candidate_digest')),
                     'R2_PLUGIN_REQUEST_MISMATCH')
        request_path = self.root / (role + '.request.json')
        response_path = self.root / (role + '.response.json')
        data = canonical_json_bytes(request)
        _publish_request(request_path, data)
        deadline = time.monotonic() + WAIT_SECONDS
        with hold_windows_private_file(request_path) as held_request:
            while not response_path.exists():
                _require(time.monotonic() < deadline, 'R2_PLUGIN_RESPONSE_TIMEOUT')
                time.sleep(0.25)
            _require(held_request.read_bytes() == data, 'R2_PLUGIN_REQUEST_CHANGED')
            info = response_path.lstat()
            _require(stat.S_ISREG(info.st_mode) and not response_path.is_symlink()
                     and not getattr(response_path, 'is_junction', lambda: False)()
                     and info.st_nlink == 1 and 0 < info.st_size <= MAX_RESPONSE_BYTES)
            with hold_windows_private_file(response_path) as held_response:
                with held_response.open('rb') as source:
                    body = source.read(MAX_RESPONSE_BYTES + 1)
                _require(0 < len(body) <= MAX_RESPONSE_BYTES)
                receipt = validate_plugin_response(_strict_json(body), request)
        with (self.root / (role + '.receipt.json')).open('xb') as output:
            output.write(canonical_json_bytes(receipt))
        if role == 'PRESTATE':
            self._prestate = receipt
        return receipt
