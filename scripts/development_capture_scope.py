"""A fixed local-development budget; each reservation owns one native capture.

This scope cannot authorize Candidate acceptance. The ledger contains public
accounting only. It is never keyed by source, Qualification, plan or session.
"""
from __future__ import annotations

from contextlib import ExitStack
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import time

from release.formal_windows_pretrust import (
    create_windows_private_named_directory,
    hold_windows_private_path_chain,
    hold_windows_private_working_directory,
)

AUTHORIZATION = 'ANIMEMO_V2_LOCAL_INSTALLER_DEVELOPMENT_V1'
LEDGER = Path('E:/') / hashlib.sha256(AUTHORIZATION.encode('ascii')).hexdigest()
MAX_CAPTURES = 6
SCOPE_SECONDS = 12 * 60 * 60
SCHEMA = 'animemo.local-installer-development-capture-budget/v1'
SLOTS = tuple(hashlib.sha256(f'{AUTHORIZATION}:capture:{index}'.encode('ascii')).hexdigest()
              for index in range(1, MAX_CAPTURES + 1))
# The operator confirmed one twelve-hour extension for the three remaining
# captures on 2026-09-13. This exact original scope is the only beneficiary;
# no file, CLI flag, new authorization ID or current clock can renew the grant.
EXTENSION_SCOPE_SHA256 = '1a48a5ad82083ff97f4580c6dee25ec15463c3275ba76211a59a4ac66b420b3f'
EXTENSION_START_UTC = 1789266910.207901
EXTENSION_EXPIRES_UTC = 1789310110.207901
EXTENSION_SPENT_CAPTURES = 3


def _has_confirmed_extension(value):
    raw = (json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False) + '\n').encode('utf-8')
    return hashlib.sha256(raw).hexdigest() == EXTENSION_SCOPE_SHA256


class DevelopmentScopeError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _github_utc_upper_bound():
    """Get fresh public HTTPS time after a clock epoch change, without tokens."""
    from email.utils import parsedate_to_datetime
    import secrets
    from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

    class NoRedirect(HTTPRedirectHandler):
        def redirect_request(self, *_args, **_kwargs):
            return None

    url = 'https://api.github.com/rate_limit?animemo_clock_nonce=' + secrets.token_hex(16)
    request = Request(url, headers={'User-Agent': 'AniMemo-local-development-clock',
        'Cache-Control': 'no-cache, no-store', 'Pragma': 'no-cache', 'Accept': 'application/json'})
    try:
        with build_opener(ProxyHandler({}), NoRedirect()).open(request, timeout=10) as response:
            dates = response.headers.get_all('Date', [])
            ages = response.headers.get_all('Age', [])
            _require(response.status == 200 and response.geturl() == url
                and len(dates) == 1 and ages in ([], ['0']))
            stamp = parsedate_to_datetime(dates[0])
            _require(stamp.tzinfo is not None)
            # Date has whole-second precision. Use its upper bound so this
            # observation can only shorten the original UTC deadline.
            return stamp.timestamp() + 1
    except Exception:
        raise DevelopmentScopeError('DEVELOPMENT_CLOCK_AUTHORITY_UNAVAILABLE') from None


def scope_deadline(value, *, monotonic_now=None, utc_now=None):
    """Preserve immutable deadlines across reboot; never rewrite scope metadata."""
    monotonic_now = time.monotonic() if monotonic_now is None else monotonic_now
    utc_now = time.time() if utc_now is None else utc_now
    started_mono, started_utc = value['created_monotonic'], value['created_utc_seconds']
    _require(all(type(number) in (int, float) and math.isfinite(number)
                 for number in (started_mono, started_utc, monotonic_now, utc_now)),
             'DEVELOPMENT_CAPTURE_SCOPE_EXPIRED')
    if _has_confirmed_extension(value):
        _require(EXTENSION_START_UTC <= utc_now < EXTENSION_EXPIRES_UTC,
                 'DEVELOPMENT_CAPTURE_SCOPE_EXPIRED')
        # Always consult fresh time for this fixed extension. The conversion
        # starts before the request, so network latency only shortens it.
        trusted_utc = _github_utc_upper_bound()
        _require(type(trusted_utc) in (int, float) and math.isfinite(trusted_utc)
                 and abs(trusted_utc - utc_now) <= 30, 'DEVELOPMENT_CLOCK_AUTHORITY_MISMATCH')
        effective_utc = max(utc_now, trusted_utc)
        _require(EXTENSION_START_UTC <= effective_utc < EXTENSION_EXPIRES_UTC,
                 'DEVELOPMENT_CAPTURE_SCOPE_EXPIRED')
        deadline = monotonic_now + EXTENSION_EXPIRES_UTC - effective_utc
        return deadline, {'authority': 'GITHUB_HTTPS_DATE_CONFIRMED_EXTENSION',
            'observed_monotonic': monotonic_now, 'observed_utc_seconds': utc_now,
            'trusted_utc_upper_bound': trusted_utc,
            'original_expires_utc_seconds': started_utc + SCOPE_SECONDS,
            'effective_expires_utc_seconds': EXTENSION_EXPIRES_UTC,
            'extension_scope_sha256': EXTENSION_SCOPE_SHA256,
            'deadline_monotonic': deadline}
    elapsed_utc, elapsed_mono = utc_now - started_utc, monotonic_now - started_mono
    _require(0 <= elapsed_utc < SCOPE_SECONDS, 'DEVELOPMENT_CAPTURE_SCOPE_EXPIRED')
    epoch_changed = elapsed_mono < 0 or abs(elapsed_utc - elapsed_mono) > 60
    trusted_utc = None
    if epoch_changed:
        trusted_utc = _github_utc_upper_bound()
        _require(type(trusted_utc) in (int, float) and math.isfinite(trusted_utc)
                 and abs(trusted_utc - utc_now) <= 30, 'DEVELOPMENT_CLOCK_AUTHORITY_MISMATCH')
        elapsed_utc = max(elapsed_utc, trusted_utc - started_utc)
        _require(0 <= elapsed_utc < SCOPE_SECONDS, 'DEVELOPMENT_CAPTURE_SCOPE_EXPIRED')
        deadline = monotonic_now + SCOPE_SECONDS - elapsed_utc
    else:
        _require(0 <= elapsed_mono < SCOPE_SECONDS, 'DEVELOPMENT_CAPTURE_SCOPE_EXPIRED')
        deadline = min(started_mono + SCOPE_SECONDS, monotonic_now + SCOPE_SECONDS - elapsed_utc)
    return deadline, {'authority': 'GITHUB_HTTPS_DATE' if epoch_changed else 'LOCAL_UTC_AND_MONOTONIC',
        'clock_epoch_changed': epoch_changed, 'observed_monotonic': monotonic_now,
        'observed_utc_seconds': utc_now, 'trusted_utc_upper_bound': trusted_utc,
        'original_expires_utc_seconds': started_utc + SCOPE_SECONDS,
        'deadline_monotonic': deadline}


def _require(value, code='DEVELOPMENT_CAPTURE_SCOPE_INVALID'):
    if not value:
        raise DevelopmentScopeError(code)


def _closed_file(path, *, flags):
    descriptor = os.open(path, flags, 0o600)
    try:
        observed = path.lstat()
        opened = os.fstat(descriptor)
        _require(not path.is_symlink() and not path.is_junction()
                 and stat.S_ISREG(opened.st_mode) and opened.st_nlink == 1
                 and (observed.st_dev, observed.st_ino) == (opened.st_dev, opened.st_ino))
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _lock(descriptor):
    # Windows file locks are released by the OS after a process dies. A capture
    # directory remains spent even when that process never writes its result.
    import msvcrt
    os.lseek(descriptor, 0, os.SEEK_SET)
    try:
        msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
    except OSError:
        raise DevelopmentScopeError('DEVELOPMENT_CAPTURE_SCOPE_BUSY') from None


class DevelopmentCaptureReservation:
    __slots__ = ('path', 'index', 'deadline', 'time_observation', '_holds', '_closed')

    def __init__(self, *_args, **_kwargs):
        raise TypeError('Development capture reservations are issued only by reserve')

    def __reduce__(self):
        raise TypeError('Development capture reservations cannot be serialized')

    def require_open(self):
        _require(not self._closed, 'DEVELOPMENT_CAPTURE_SCOPE_CLOSED')
        _require(time.monotonic() < self.deadline, 'DEVELOPMENT_CAPTURE_SCOPE_EXPIRED')

    def close(self):
        if not self._closed:
            self._closed = True
            self._holds.close()


def reserve_development_capture(authorization_id, *, material_identity):
    """Reserve at most six rounds under one fixed twelve-hour task budget.

    Call only after the exact Guest, source and material checks. Cancelled,
    invalid, partial and crashed captures all consume their reserved round.
    """
    _require(authorization_id == AUTHORIZATION and type(authorization_id) is str,
             'DEVELOPMENT_CAPTURE_AUTHORIZATION_INVALID')
    _require(type(material_identity) is dict and set(material_identity) == {
        'verified_candidate_digest', 'candidate_input_digest', 'source_sha', 'source_tree',
        'qualification_run_id', 'qualification_run_attempt', 'candidate_version'})
    _require(all(type(material_identity[k]) is str and re.fullmatch('sha256:[0-9a-f]{64}', material_identity[k])
                 for k in ('verified_candidate_digest', 'candidate_input_digest')))
    _require(all(type(material_identity[k]) is str and re.fullmatch('[0-9a-f]{40}', material_identity[k])
                 for k in ('source_sha', 'source_tree')))
    _require(type(material_identity['qualification_run_id']) is int and material_identity['qualification_run_id'] > 0
             and type(material_identity['qualification_run_attempt']) is int and material_identity['qualification_run_attempt'] == 1
             and material_identity['candidate_version'] == 'v2.0.0-rc.1')
    _require(os.name == 'nt', 'DEVELOPMENT_NATIVE_WINDOWS_REQUIRED')
    holds = ExitStack()
    try:
        created = False
        if not LEDGER.exists():
            try:
                create_windows_private_named_directory(LEDGER.parent, name=LEDGER.name)
                created = True
            except Exception:
                _require(LEDGER.is_dir(), 'DEVELOPMENT_CAPTURE_SCOPE_UNAVAILABLE')
        holds.enter_context(hold_windows_private_path_chain(LEDGER, allow_leaf_child_writes=True))
        descriptor = _closed_file(LEDGER / 'owner.lock', flags=os.O_RDWR | os.O_CREAT)
        holds.callback(os.close, descriptor)
        _lock(descriptor)
        metadata = LEDGER / 'scope.json'
        monotonic_now, utc_now = time.monotonic(), time.time()
        if created:
            value = {'schema': SCHEMA, 'authorization_id': AUTHORIZATION,
                     'purpose': 'LOCAL_INSTALLER_DEVELOPMENT', 'max_captures': MAX_CAPTURES,
                     'scope_seconds': SCOPE_SECONDS, 'created_monotonic': monotonic_now,
                     'created_utc_seconds': utc_now, 'material_identity': dict(material_identity)}
            with metadata.open('x', encoding='utf-8', newline='\n') as output:
                json.dump(value, output, sort_keys=True, separators=(',', ':'))
                output.write('\n')
                output.flush()
                os.fsync(output.fileno())
        else:
            fd = _closed_file(metadata, flags=os.O_RDONLY)
            with os.fdopen(fd, 'rb') as source:
                raw = source.read(4097)
            _require(len(raw) <= 4096)
            from release.materials import reject_duplicate_json_keys
            value = json.loads(raw, object_pairs_hook=reject_duplicate_json_keys)
        _require(type(value) is dict and set(value) == {
            'schema', 'authorization_id', 'purpose', 'max_captures', 'scope_seconds',
            'created_monotonic', 'created_utc_seconds', 'material_identity'})
        _require(value['schema'] == SCHEMA and value['authorization_id'] == AUTHORIZATION
                 and value['purpose'] == 'LOCAL_INSTALLER_DEVELOPMENT'
                 and type(value['max_captures']) is int and value['max_captures'] == MAX_CAPTURES
                 and type(value['scope_seconds']) is int and value['scope_seconds'] == SCOPE_SECONDS)
        _require(value['material_identity'] == material_identity, 'DEVELOPMENT_CAPTURE_MATERIAL_CHANGED')
        deadline, time_observation = scope_deadline(value, monotonic_now=monotonic_now, utc_now=utc_now)
        children = {p.name for p in LEDGER.iterdir()}
        _require(children <= {'scope.json', 'owner.lock', *SLOTS})
        used = [slot in children for slot in SLOTS]
        _require(used == sorted(used, reverse=True))
        for slot in SLOTS[:sum(used)]:
            path = LEDGER / slot
            _require(path.is_dir() and not path.is_symlink() and not path.is_junction())
        _require(sum(used) < MAX_CAPTURES, 'DEVELOPMENT_CAPTURE_BUDGET_EXHAUSTED')
        if _has_confirmed_extension(value):
            _require(sum(used) >= EXTENSION_SPENT_CAPTURES,
                     'DEVELOPMENT_CAPTURE_SCOPE_INVALID')
        _require(time.monotonic() < deadline, 'DEVELOPMENT_CAPTURE_SCOPE_EXPIRED')
        index = sum(used)
        path = create_windows_private_named_directory(LEDGER, name=SLOTS[index])
        holds.enter_context(hold_windows_private_working_directory(path))
        reservation = object.__new__(DevelopmentCaptureReservation)
        reservation.path, reservation.index = path, index + 1
        reservation.deadline, reservation.time_observation = deadline, time_observation
        reservation._holds, reservation._closed = holds.pop_all(), False
        return reservation
    except DevelopmentScopeError:
        raise
    except Exception:
        raise DevelopmentScopeError('DEVELOPMENT_CAPTURE_SCOPE_UNAVAILABLE') from None
    finally:
        holds.close()
