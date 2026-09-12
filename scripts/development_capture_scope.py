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


class DevelopmentScopeError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


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
    __slots__ = ('path', 'index', 'deadline', '_holds', '_closed')

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
        for name, current in (('created_monotonic', monotonic_now), ('created_utc_seconds', utc_now)):
            started = value[name]
            _require(type(started) in (int, float) and math.isfinite(started)
                     and 0 <= current - started < SCOPE_SECONDS,
                     'DEVELOPMENT_CAPTURE_SCOPE_EXPIRED')
        children = {p.name for p in LEDGER.iterdir()}
        _require(children <= {'scope.json', 'owner.lock', *SLOTS})
        used = [slot in children for slot in SLOTS]
        _require(used == sorted(used, reverse=True))
        for slot in SLOTS[:sum(used)]:
            path = LEDGER / slot
            _require(path.is_dir() and not path.is_symlink() and not path.is_junction())
        _require(sum(used) < MAX_CAPTURES, 'DEVELOPMENT_CAPTURE_BUDGET_EXHAUSTED')
        index = sum(used)
        path = create_windows_private_named_directory(LEDGER, name=SLOTS[index])
        holds.enter_context(hold_windows_private_working_directory(path))
        reservation = object.__new__(DevelopmentCaptureReservation)
        reservation.path, reservation.index = path, index + 1
        reservation.deadline = value['created_monotonic'] + SCOPE_SECONDS
        reservation._holds, reservation._closed = holds.pop_all(), False
        return reservation
    except DevelopmentScopeError:
        raise
    except Exception:
        raise DevelopmentScopeError('DEVELOPMENT_CAPTURE_SCOPE_UNAVAILABLE') from None
    finally:
        holds.close()
