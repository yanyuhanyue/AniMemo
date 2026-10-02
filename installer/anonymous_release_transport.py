"""Explicit anonymous acquisition of untrusted GitHub Release proof inputs.

No result from this module is a verification or installation capability. Only
the separate, independently trusted cryptographic verifier may authorize use.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import hashlib
import json
import re
import time
import tempfile
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

HOST = 'api.github.com'
REPOSITORY = 'yanyuhanyue/AniMemo'
REPOSITORY_ID = 1327429673
BASE = '/repos/' + REPOSITORY
PREDICATE = 'https://in-toto.io/attestation/release/v0.2'
PREDICATE_FILTER = 'release'
API_VERSION = '2022-11-28'
MAX_JSON_BYTES = 8 * 1024 * 1024
MAX_TOTAL_BYTES = 32 * 1024 * 1024
MAX_PAGES = 16
MAX_TAG_DEPTH = 8
MAX_HEADERS = 100
MAX_HEADER_BYTES = 32 * 1024
TOTAL_SECONDS = 180
REQUEST_SECONDS = 30
_VERSION = re.compile(r'v(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)(?:-(?:beta|rc)\.[1-9][0-9]*)?\Z')
_SHA1 = re.compile(r'[0-9a-f]{40}\Z')
_SHA256 = re.compile(r'sha256:[0-9a-f]{64}\Z')
_SENSITIVE_HEADERS = frozenset({'content-length', 'content-type', 'content-encoding',
    'transfer-encoding', 'link', 'location', 'retry-after', 'www-authenticate',
    'proxy-authenticate', 'set-cookie', 'authorization', 'deprecation', 'sunset'})
_REQUEST_HEADERS = {
    'Accept': 'application/vnd.github+json',
    'Accept-Encoding': 'identity',
    'User-Agent': 'AniMemo-Anonymous-Stage0/1',
    # Fixed API contract observed with gh 2.97.0's release predicate alias.
    # Newer API versions may omit the inline proof; there is no CDN fallback.
    'X-GitHub-Api-Version': API_VERSION,
    'Connection': 'close',
}


class AnonymousReleaseError(RuntimeError):
    """Fixed public classification; never contains response text or URLs."""
    def __init__(self, code: str, *, http_status: int | None = None):
        self.code = 'BOOTSTRAP_ANONYMOUS_' + code
        self.http_status = http_status
        self.cleanup_failed = False
        super().__init__(self.code)


@dataclass(frozen=True)
class UntrustedReleaseMaterials:
    version: str
    release_metadata: dict
    tag_object: str
    tag_commit: str
    release_bundle: bytes
    observations: tuple[dict, ...]



def _require(condition, code='INPUT_INVALID'):
    if not condition:
        raise AnonymousReleaseError(code)


def _positive(value):
    return type(value) is int and value > 0


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise AnonymousReleaseError('JSON_INVALID')
        result[key] = value
    return result


def _constant(_value):
    raise AnonymousReleaseError('JSON_INVALID')


def _json(raw):
    try:
        return json.loads(raw.decode('utf-8'), object_pairs_hook=_pairs, parse_constant=_constant)
    except (ValueError, UnicodeError, RecursionError):
        raise AnonymousReleaseError('JSON_INVALID') from None


def _canonical(value):
    try:
        return (json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True,
                           separators=(',', ':')) + '\n').encode('utf-8')
    except (ValueError, UnicodeError, RecursionError):
        raise AnonymousReleaseError('JSON_INVALID') from None


def _status(status):
    _require(type(status) is int and 100 <= status <= 599, 'HTTP_STATUS_INVALID')
    if status == 200:
        return
    code = {401: 'AUTHENTICATION_REQUIRED', 403: 'FORBIDDEN', 404: 'NOT_FOUND',
            429: 'RATE_LIMITED'}.get(status)
    if code is None:
        code = 'REDIRECT_REJECTED' if 300 <= status < 400 else 'SERVER_ERROR' if 500 <= status <= 599 else 'HTTP_STATUS_INVALID'
    raise AnonymousReleaseError(code, http_status=status)


def _headers(pairs):
    _require(type(pairs) is list and len(pairs) <= MAX_HEADERS, 'HEADERS_INVALID')
    result, total = {}, 0
    for pair in pairs:
        _require(type(pair) is tuple and len(pair) == 2 and all(type(v) is str for v in pair), 'HEADERS_INVALID')
        name, value = pair
        total += len(name) + len(value)
        _require(total <= MAX_HEADER_BYTES and re.fullmatch(r'[A-Za-z0-9!#$%&\'*+.^_`|~-]+', name)
            and all(character == '\t' or 32 <= ord(character) < 127 or 128 <= ord(character) <= 255
                    for character in value), 'HEADERS_INVALID')
        name = name.lower()
        _require(name not in result or name not in _SENSITIVE_HEADERS, 'HEADERS_INVALID')
        result[name] = value.strip()
    length = result.get('content-length')
    _require(length is None or len(length) <= 20 and re.fullmatch(r'0|[1-9][0-9]*', length), 'LENGTH_INVALID')
    _require(length is None or int(length) <= MAX_JSON_BYTES, 'RESPONSE_TOO_LARGE')
    encoding = result.get('content-encoding')
    _require(encoding is None or encoding.lower() == 'identity', 'ENCODING_REJECTED')
    transfer = result.get('transfer-encoding')
    _require(transfer is None or transfer.lower() == 'chunked' and length is None, 'ENCODING_REJECTED')
    _require(result.get('content-type', '').split(';', 1)[0].strip().lower()
        in {'application/json', 'application/vnd.github+json'}, 'CONTENT_TYPE_INVALID')
    _require('location' not in result and 'set-cookie' not in result and 'authorization' not in result,
             'HEADERS_INVALID')
    return result


def _metadata(value, version):
    _require(type(value) is dict and _positive(value.get('id')) and value.get('tag_name') == version
        and value.get('draft') is False and value.get('immutable') is True
        and value.get('prerelease') is ('-' in version), 'RELEASE_IDENTITY_INVALID')
    published = value.get('published_at')
    _require(type(published) is str and re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z', published),
             'RELEASE_IDENTITY_INVALID')
    try:
        datetime.fromisoformat(published)
    except ValueError:
        raise AnonymousReleaseError('RELEASE_IDENTITY_INVALID') from None
    names = {'checksums.txt', 'deployment-contract.json', 'installer-materials.tar',
             'release-manifest.json', f'animemo-{version}-portable.tar'}
    assets = value.get('assets')
    _require(type(assets) is list and len(assets) == len(names), 'ASSET_INVENTORY_INVALID')
    output, ids = [], set()
    for asset in assets:
        _require(type(asset) is dict and type(asset.get('name')) is str and asset['name'] in names
            and asset.get('state') == 'uploaded' and _positive(asset.get('id')) and asset['id'] not in ids
            and _positive(asset.get('size')) and asset['size'] <= 4 * 1024 * 1024 * 1024
            and type(asset.get('digest')) is str and _SHA256.fullmatch(asset['digest']), 'ASSET_INVENTORY_INVALID')
        names.remove(asset['name'])
        ids.add(asset['id'])
        output.append({key: asset[key] for key in ('id', 'name', 'state', 'size', 'digest')})
    return dict(id=value['id'], tag_name=version, draft=False, immutable=True,
        prerelease=value['prerelease'], published_at=published, assets=output)


def _lifecycle_header(value):
    """Project deprecation metadata without recording arbitrary header text."""
    if value is None:
        return None
    if value.lower() in {'true', 'false'}:
        return {'format': 'BOOLEAN', 'value': value.lower() == 'true'}
    if re.fullmatch(r'@[0-9]{1,18}', value):
        return {'format': 'UNIX_TIMESTAMP', 'value': int(value[1:])}
    try:
        parsed = parsedate_to_datetime(value)
        if parsed.tzinfo is not None:
            return {'format': 'HTTP_DATE', 'utc': parsed.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')}
    except (ValueError, TypeError, OverflowError):
        pass
    return {'format': 'UNRECOGNIZED'}


def _next_page(link, endpoint, page):
    if link is None:
        return None
    _require(type(link) is str and len(link) <= 8192 and link, 'PAGINATION_INVALID')
    relations, seen_relations = {}, set()
    for item in link.split(','):
        match = re.fullmatch(r'\s*<([^<>\s]+)>\s*;\s*rel="(next|prev|first|last|deprecation)"'
                             r'(?:\s*;\s*type="(text/html)")?\s*', item)
        _require(match is not None and match[2] not in seen_relations, 'PAGINATION_INVALID')
        seen_relations.add(match[2])
        try:
            parsed = urlsplit(match[1])
        except ValueError:
            raise AnonymousReleaseError('PAGINATION_INVALID') from None
        if match[2] == 'deprecation':
            # Observed API-version lifecycle documentation is not pagination.
            # Never fetch this URL and never interpret it as another proof path.
            _require(parsed.scheme == 'https' and parsed.netloc == 'docs.github.com'
                and parsed.path == '/en/rest/about-the-rest-api/api-versions'
                and not parsed.query and not parsed.fragment and match[3] == 'text/html', 'PAGINATION_INVALID')
            continue
        _require(match[3] is None, 'PAGINATION_INVALID')
        _require(parsed.scheme == 'https' and parsed.netloc == HOST and parsed.path == endpoint
            and not parsed.fragment, 'PAGINATION_INVALID')
        try:
            pairs = parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=True)
        except ValueError:
            raise AnonymousReleaseError('PAGINATION_INVALID') from None
        query = dict(pairs)
        _require(len(pairs) == len(query) == 3 and set(query) == {'per_page', 'predicate_type', 'page'}
            and query['per_page'] == '100' and query['predicate_type'] == PREDICATE_FILTER
            and re.fullmatch(r'[1-9][0-9]*', query['page']), 'PAGINATION_INVALID')
        number = int(query['page'])
        _require(number <= MAX_PAGES, 'PAGINATION_INVALID')
        relations[match[2]] = number
    next_number = relations.get('next')
    if next_number is not None:
        _require(next_number == page + 1 and relations.get('last', next_number) >= next_number, 'PAGINATION_INVALID')
    else:
        _require(relations.get('last', page) <= page, 'PAGINATION_INCOMPLETE')
    _require(relations.get('first', 1) == 1 and relations.get('prev', page - 1) < page, 'PAGINATION_INVALID')
    return next_number


class AnonymousReleaseReader:
    """Fixed-host GET only. No proxy/environment/credential/config discovery."""
    def __init__(self, http_client=None, *, private_root=None):
        self._client = http_client
        self._private_root = Path(private_root or tempfile.gettempdir())
        self._deadline = 0.0
        self._total = 0
        self._observations = []

    def _get(self, path, category):
        from bootstrap_kit.http_protocol import HttpFailure, HttpSelection
        from bootstrap_kit.http_supervisor import SupervisedAnonymousHttp
        remaining = self._deadline - time.monotonic()
        _require(remaining > 0, 'TIMEOUT')
        client = self._client or SupervisedAnonymousHttp(private_root=self._private_root)
        try:
            with client.fetch(HttpSelection.github_json(path),
                    deadline=min(self._deadline, time.monotonic() + REQUEST_SECONDS)) as result:
                _status(result.status)
                headers = _headers(list(result.headers.items()))
                raw = result.read_bytes(maximum=MAX_JSON_BYTES)
                _require(self._total + len(raw) <= MAX_TOTAL_BYTES, 'RESPONSE_TOO_LARGE')
                self._total += len(raw)
                self._observations.append(dict(endpoint_category=category, http_status=result.status,
                    body_bytes=len(raw), body_sha256='sha256:' + hashlib.sha256(raw).hexdigest(),
                    api_version=API_VERSION, deprecation=_lifecycle_header(headers.get('deprecation')),
                    sunset=_lifecycle_header(headers.get('sunset')),
                    authorization_header_present=False, cookie_header_present=False, credential_helper_called=False))
                return _json(raw), headers
        except HttpFailure as error:
            failure = AnonymousReleaseError(error.code.removeprefix('BOOTSTRAP_HTTP_'),
                                            http_status=error.http_status)
            failure.secondary_errors = error.secondary_errors
            failure.cleanup_failed = bool(error.secondary_errors)
            raise failure from error

    def fetch(self, version):
        _require(type(version) is str and len(version) <= 128 and _VERSION.fullmatch(version), 'VERSION_INVALID')
        self._deadline, self._total, self._observations = time.monotonic() + TOTAL_SECONDS, 0, []
        metadata, _ = self._get(BASE + '/releases/tags/' + version, 'RELEASE_METADATA')
        metadata = _metadata(metadata, version)
        reference, _ = self._get(BASE + '/git/ref/tags/' + version, 'TAG_REFERENCE')
        _require(type(reference) is dict and reference.get('ref') == 'refs/tags/' + version
            and type(reference.get('object')) is dict and reference['object'].get('type') == 'tag', 'TAG_INVALID')
        current = reference['object']
        tag_object, seen = current.get('sha'), set()
        while current.get('type') == 'tag':
            sha = current.get('sha')
            _require(type(sha) is str and _SHA1.fullmatch(sha) and sha not in seen and len(seen) < MAX_TAG_DEPTH
                and current.get('url') == f'https://{HOST}{BASE}/git/tags/{sha}', 'TAG_INVALID')
            seen.add(sha)
            tag, _ = self._get(BASE + '/git/tags/' + sha, 'ANNOTATED_TAG')
            _require(type(tag) is dict and tag.get('sha') == sha and type(tag.get('object')) is dict
                and (len(seen) != 1 or tag.get('tag') == version), 'TAG_INVALID')
            current = tag['object']
        commit = current.get('sha')
        _require(current.get('type') == 'commit' and type(commit) is str and _SHA1.fullmatch(commit)
            and current.get('url') == f'https://{HOST}{BASE}/git/commits/{commit}', 'TAG_INVALID')
        endpoint = BASE + '/attestations/sha1:' + tag_object
        bundles, page = [], 1
        while True:
            suffix = '?per_page=100&predicate_type=' + PREDICATE_FILTER + ('&page=' + str(page) if page != 1 else '')
            payload, headers = self._get(endpoint + suffix, 'RELEASE_PROOF')
            _require(type(payload) is dict and set(payload) == {'attestations'}
                and type(payload['attestations']) is list and len(payload['attestations']) <= 100, 'PROOF_INVALID')
            for item in payload['attestations']:
                _require(type(item) is dict and set(item) <= {'repository_id', 'initiator', 'bundle', 'bundle_url'}
                    and type(item.get('repository_id')) is int and item['repository_id'] == REPOSITORY_ID
                    and item.get('initiator') == 'github',
                    'PROOF_INVALID')
                _require(type(item.get('bundle')) is dict and item['bundle'], 'PROOF_INLINE_UNAVAILABLE')
                # Deliberately never dereference or retain the signed bundle_url.
                bundles.append(_canonical(item['bundle']))
                _require(len(bundles) <= 1, 'PROOF_DUPLICATE')
            next_page = _next_page(headers.get('link'), endpoint, page)
            if next_page is None:
                break
            page = next_page
        _require(len(bundles) == 1, 'PROOF_MISSING')
        return UntrustedReleaseMaterials(version, metadata, tag_object, commit, bundles[0], tuple(self._observations))
