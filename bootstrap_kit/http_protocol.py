"""Closed, non-authoritative requests shared by the independent HTTP process pair."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlsplit

PROTOCOL = 'animemo.anonymous-http/v1'
CONTROL_BYTES = 16384
PROGRESS_FRAMES = 24
PROGRESS_BYTES = 8192
PROGRESS_STEP = 32 * 1024 * 1024
JSON_BYTES = 8 * 1024 * 1024
ASSET_BYTES = 512 * 1024 * 1024
REPOSITORY_ID = 1327429673
BASE = '/repos/yanyuhanyue/AniMemo'
VERSION = r'v(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)(?:-(?:beta|rc)\.[1-9][0-9]*)?'
KIT_VERSION = r'bootstrap-trust-kit-v[1-9][0-9]*\.[0-9]+\.[0-9]+'
_SHA256 = r'[0-9a-f]{64}'
_TUF_BOOTSTRAP = {
    'github': '/cli/cli/v2.97.0/pkg/cmd/attestation/verification/embed/tuf-repo.github.com/root.json',
    'sigstore': '/sigstore/sigstore-go/v1.2.2/pkg/tuf/repository/root.json',
}
_TUF_HOST = {'github': 'tuf-repo.github.com', 'sigstore': 'tuf-repo-cdn.sigstore.dev'}


class HttpFailure(RuntimeError):
    def __init__(self, code, *, cleanup=(), http_status=None, observations=()):
        self.code = 'BOOTSTRAP_HTTP_' + code
        self.secondary_errors = tuple(cleanup)
        self.http_status = http_status
        self.observations = tuple(observations)
        self.diagnostics = None
        super().__init__(self.code)


HttpError = HttpFailure


def require(value, code='PROTOCOL_INVALID'):
    if not value:
        raise HttpFailure(code)


def canonical(value):
    try:
        return (json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True,
                           allow_nan=False) + '\n').encode('ascii')
    except (ValueError, TypeError, RecursionError):
        raise HttpFailure('PROTOCOL_INVALID') from None


def strict_json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result)
            result[key] = value
        return result
    try:
        return json.loads(raw, object_pairs_hook=pairs,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, UnicodeError, RecursionError):
        raise HttpFailure('PROTOCOL_INVALID') from None


def _positive(value):
    return type(value) is int and 0 < value < 2**63


@dataclass(frozen=True)
class HttpSelection:
    """Untrusted transport selection. Validation supplies no release authority."""
    kind: str
    fields: dict

    @classmethod
    def github_json(cls, path):
        value = cls('GITHUB_JSON', {'path': path})
        value.target()
        return value

    @classmethod
    def github_asset(cls, *, version, release_id, asset_id, name, size, sha256, kind='PRODUCT'):
        value = cls('GITHUB_ASSET', {'version': version, 'release_id': release_id, 'asset_id': asset_id,
            'name': name, 'size': size, 'sha256': sha256.removeprefix('sha256:') if type(sha256) is str else sha256,
            'namespace': kind, 'repository_id': REPOSITORY_ID})
        value.target()
        return value

    @classmethod
    def tuf(cls, track, name):
        value = cls('TUF_METADATA', {'track': track, 'name': name})
        value.target()
        return value

    @classmethod
    def tuf_bootstrap(cls, track):
        value = cls('TUF_BOOTSTRAP', {'track': track})
        value.target()
        return value

    @classmethod
    def actions_bundle(cls, path, query):
        value = cls('ACTIONS_BUNDLE', {'path': path, 'query': query})
        value.target()
        return value

    @property
    def identity(self):
        return hashlib.sha256(canonical(self.record())).hexdigest()

    def record(self):
        self.target()
        return {'kind': self.kind, 'fields': dict(self.fields)}

    @classmethod
    def from_record(cls, value):
        require(type(value) is dict and set(value) == {'kind', 'fields'})
        result = cls(value['kind'], value['fields'])
        result.target()
        return result

    @property
    def maximum_bytes(self):
        self.target()
        if self.kind == 'GITHUB_ASSET':
            return self.fields['size']
        return 16 * 1024 * 1024 if self.kind.startswith('TUF_') else JSON_BYTES

    def target(self):
        require(type(self.kind) is str and type(self.fields) is dict, 'SELECTION_INVALID')
        f = self.fields
        if self.kind == 'GITHUB_JSON':
            require(set(f) == {'path'} and type(f['path']) is str, 'SELECTION_INVALID')
            path = f['path']
            require(len(path) <= 4096 and path.isascii() and all(32 < ord(c) < 127 for c in path), 'SELECTION_INVALID')
            parsed = urlsplit(path)
            require(not parsed.scheme and not parsed.netloc and not parsed.fragment and '\\' not in path,
                    'SELECTION_INVALID')
            suffix = parsed.path.removeprefix(BASE)
            require(parsed.path.startswith(BASE + '/'), 'SELECTION_INVALID')
            patterns = (r'/releases', '/releases/tags/(?:' + VERSION + '|' + KIT_VERSION + ')',
                r'/releases/[1-9][0-9]*', r'/git/ref/tags/' + VERSION, r'/git/tags/[0-9a-f]{40}',
                r'/(?:git/)?commits/[0-9a-f]{40}',
                r'/attestations/(?:sha1:[0-9a-f]{40}|sha256:' + _SHA256 + ')')
            require(any(re.fullmatch(p, suffix) for p in patterns), 'SELECTION_INVALID')
            try:
                pairs = parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=True)
            except ValueError:
                raise HttpFailure('SELECTION_INVALID') from None
            query = dict(pairs)
            require(len(pairs) == len(query) and set(query) <= {'per_page', 'page', 'predicate_type'}, 'SELECTION_INVALID')
            require(not query or suffix == '/releases' or suffix.startswith('/attestations/'), 'SELECTION_INVALID')
            require('per_page' not in query or query['per_page'] == '100', 'SELECTION_INVALID')
            require('page' not in query or re.fullmatch(r'(?:[1-9][0-9]?|100)', query['page']), 'SELECTION_INVALID')
            require('predicate_type' not in query or suffix.startswith('/attestations/sha1:')
                    and query['predicate_type'] == 'release', 'SELECTION_INVALID')
            return 'api.github.com', path, 'application/vnd.github+json'
        if self.kind == 'GITHUB_ASSET':
            require(set(f) == {'version', 'release_id', 'asset_id', 'name', 'size', 'sha256', 'namespace', 'repository_id'}, 'SELECTION_INVALID')
            require(f['repository_id'] == REPOSITORY_ID and type(f['repository_id']) is int
                and all(_positive(f[k]) for k in ('release_id', 'asset_id', 'size'))
                and f['size'] <= ASSET_BYTES and type(f['sha256']) is str
                and re.fullmatch(_SHA256, f['sha256']) and type(f['version']) is str
                and type(f['name']) is str, 'SELECTION_INVALID')
            if f['namespace'] == 'PRODUCT':
                require(re.fullmatch(VERSION, f['version']) and f['name'] in {
                    'checksums.txt', 'deployment-contract.json', 'installer-materials.tar',
                    'release-manifest.json', 'animemo-' + f['version'] + '-portable.tar'}, 'SELECTION_INVALID')
            else:
                require(f['namespace'] == 'BOOTSTRAP_KIT' and re.fullmatch(KIT_VERSION, f['version'])
                    and re.fullmatch(r'(?:entry\.pyz|bootstrap-trust-kit-(?:windows-amd64|linux-amd64)\.(?:tar|manifest\.json))', f['name']), 'SELECTION_INVALID')
            return 'api.github.com', BASE + '/releases/assets/' + str(f['asset_id']), 'application/octet-stream'
        if self.kind in {'TUF_METADATA', 'TUF_BOOTSTRAP'}:
            require(set(f) == ({'track', 'name'} if self.kind == 'TUF_METADATA' else {'track'})
                    and type(f['track']) is str and f['track'] in _TUF_HOST, 'SELECTION_INVALID')
            if self.kind == 'TUF_BOOTSTRAP':
                return 'raw.githubusercontent.com', _TUF_BOOTSTRAP[f['track']], 'application/json'
            name = f['name']
            require(type(name) is str and re.fullmatch(r'(?:timestamp\.json|[1-9][0-9]*\.(?:root|snapshot|targets)\.json|targets/[0-9a-f]{64}\.trusted_root\.json)', name), 'SELECTION_INVALID')
            return _TUF_HOST[f['track']], '/' + name, 'application/json'
        if self.kind == 'ACTIONS_BUNDLE':
            require(set(f) == {'path', 'query'} and all(type(f[k]) is str for k in f), 'SELECTION_INVALID')
            require(re.fullmatch(r'/attestations/1327429673/[0-9]{4}/[0-9]{2}/[0-9]{2}/[1-9][0-9]*\.json\.sn', f['path'])
                and 0 < len(f['query']) <= 4096 and f['query'].isascii()
                and all(32 < ord(c) < 127 and c not in '#\\' for c in f['query']), 'SELECTION_INVALID')
            return 'tmaproduction.blob.core.windows.net', f['path'] + '?' + f['query'], 'application/json'
        raise HttpFailure('SELECTION_INVALID')


def asset_redirect(location):
    require(type(location) is str and len(location) <= 8192 and location.isascii()
        and all(32 < ord(c) < 127 for c in location), 'REDIRECT_INVALID')
    try:
        parsed = urlsplit(location)
    except ValueError:
        raise HttpFailure('REDIRECT_INVALID') from None
    require(parsed.scheme == 'https' and parsed.netloc == 'release-assets.githubusercontent.com'
        and re.fullmatch(r'/github-production-release-asset/1327429673/[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}', parsed.path)
        and parsed.query and not parsed.fragment and '\\' not in location, 'REDIRECT_INVALID')
    return parsed.hostname, parsed.path + '?' + parsed.query
