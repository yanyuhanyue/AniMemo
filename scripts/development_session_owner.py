"""One native capture retained in memory across serial, separately bound dev rounds."""
from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path

SCHEMA = 'animemo.local-development-memory-owner/v1'
# Application/report failures may retire a round while cleanup is checked.
# Every other revocation still clears the owner immediately.
RETAIN_PENDING_CLEANUP = frozenset({'CANDIDATE_SECRET_USE_FAILED', 'CANDIDATE_BATCH_PROFILE_FAILURE'})


class DevelopmentOwnerError(RuntimeError):
    def __init__(self, code='DEVELOPMENT_SESSION_OWNER_INVALID'):
        self.code = code
        super().__init__(code)


def _require(value, code='DEVELOPMENT_SESSION_OWNER_INVALID'):
    if not value:
        raise DevelopmentOwnerError(code)


def _material(plan):
    value = {key: getattr(plan, key) for key in ('verified_candidate_digest',
        'candidate_input_digest', 'source_sha', 'source_tree', 'qualification_run_id', 'candidate_version')}
    return {**value, 'qualification_run_attempt': 1}


def authenticated_execution_failure(diagnostic):
    from scripts.candidate_diagnostics import (
        INSTALLER_FAILURE_CODES,
        RUNNER_FAILURE_CODES,
    )
    if type(diagnostic) is not dict:
        return False
    codes = diagnostic.get('exit_codes', {})
    allowed_errors = {'PROFILE_RECEIPT_INVALID', 'RUNNER_EXECUTION_FAILED', 'ROOT_EXECUTION_FAILED',
        'INSTALLER_OUTPUT_INVALID', 'INSTALLER_EXECUTION_FAILED',
        *INSTALLER_FAILURE_CODES, *RUNNER_FAILURE_CODES}
    return (diagnostic.get('root_started') is True and diagnostic.get('transport_error') is None
        and diagnostic.get('profile_draft_received') is False
        and diagnostic.get('host_receipt_parse') == 'NOT_REACHED'
        and type(codes) is dict and type(codes.get('INSTALLER')) is int and 0 <= codes['INSTALLER'] <= 255
        and all(type(codes.get(key)) is int and codes[key] == 2 for key in ('RUNTIME_RUNNER', 'ROOT', 'SUDO'))
        and type(diagnostic.get('errors')) is list and bool(diagnostic['errors'])
        and all(type(code) is str and code in allowed_errors for code in diagnostic['errors']))


class DevelopmentSecretUse:
    __slots__ = ('_batch', '_closed', '_owner')

    def __init__(self, *_args, **_kwargs):
        raise TypeError('Development secret uses are issued by the memory owner')

    def __reduce__(self):
        raise TypeError('Development secret uses cannot be serialized')

    def require_live(self, batch):
        _require(not self._closed and batch is self._batch)
        self._owner.require_batch(batch)

    def delivery_value(self, batch):
        self.require_live(batch)
        return self._owner.delivery_value(batch)

    def close(self):
        self._closed = True


class DevelopmentSessionOwner:
    def __init__(self, *_args, **_kwargs):
        raise TypeError('Development memory owners require the existing fixed scope')

    def __repr__(self):
        return '<DevelopmentSessionOwner>'

    def __reduce__(self):
        raise TypeError('Development memory owners cannot be serialized')

    @property
    def record(self):
        with self._lock:
            return {'schema': SCHEMA, 'owner_id': self._id, 'state': self._state,
                'capture_attempts': self._attempts, 'capture_completed': self._completed,
                'last_reserved_round': self._index, 'capture_limit': self._limit,
                'material_identity': dict(self._material), 'expires_utc_seconds': self._expires_utc,
                'secret_cleanup': self._cleanup, 'close_reason': self._reason}

    @property
    def closed(self):
        with self._lock:
            return self._state == 'CLOSED'

    @property
    def scope_owner(self):
        self._live()
        self._scope_owner.require_open()
        return self._scope_owner

    def _live(self):
        if self._clock() >= self._deadline:
            self.close('DEVELOPMENT_SESSION_EXPIRED')
        _require(self._state != 'CLOSED', 'DEVELOPMENT_SESSION_CLOSED')

    def require_batch(self, batch):
        with self._lock:
            self._live()
            _require(self._batch is batch and self._state == 'ROUND_ACTIVE'
                and type(self._secret) is bytearray and self._completed == 1)

    def bind_after_verified_guest(self, batch, reservation, console):
        from scripts.candidate_batch_session import CandidateBatch
        from scripts.development_capture_scope import (
            LEDGER,
            SLOTS,
            DevelopmentCaptureReservation,
        )
        with self._lock:
            self._live()
            _require(type(batch) is CandidateBatch and batch._development
                and type(reservation) is DevelopmentCaptureReservation)
            reservation.require_open()
            _require(self._state == 'READY' and self._batch is None and self._pending is None
                and _material(batch.plan) == self._material
                and batch.plan.session_id not in self._sessions
                and not ({profile.clone_identity for profile in batch.plan.profiles} & self._clones)
                and reservation.index == self._index + 1 <= self._limit
                and reservation.path == LEDGER / SLOTS[reservation.index - 1]
                and reservation.time_observation['effective_expires_utc_seconds'] == self._expires_utc)
            self._index = reservation.index
            self._sessions.add(batch.plan.session_id)
            self._clones.update(profile.clone_identity for profile in batch.plan.profiles)
            self._deadline = min(self._deadline, reservation.deadline)
            self._batch = batch
            self._state = 'CAPTURING' if self._secret is None else 'ROUND_ACTIVE'
            capture = self._secret is None
            if capture:
                _require(self._attempts == 0)
                self._attempts = 1
        # Do not hold the owner lock while Console input waits. Its independent
        # deadline monitor must remain able to close the owner.
        if capture:
            secret = None
            try:
                secret = console.capture(cancelled=self._done)
                _require(type(secret) is bytearray and 1 <= len(secret) <= 4096
                    and all(32 <= character != 127 for character in secret))
                with self._lock:
                    self._live()
                    _require(self._batch is batch and self._state == 'CAPTURING')
                    self._secret, secret = secret, None
                    self._completed = 1
                    self._state = 'ROUND_ACTIVE'
            except BaseException:
                self.close('DEVELOPMENT_SESSION_CAPTURE_FAILED')
                raise
            finally:
                if type(secret) is bytearray:
                    secret[:] = b'\0' * len(secret)
                    secret.clear()
        with self._lock:
            self.require_batch(batch)
            use = object.__new__(DevelopmentSecretUse)
            use._owner, use._batch, use._closed = self, batch, False
            return use, int(capture)

    def delivery_value(self, batch):
        with self._lock:
            self.require_batch(batch)
            return bytearray(self._secret)

    def on_revocation(self, batch, code):
        with self._lock:
            if batch._development_owner is self and code not in RETAIN_PENDING_CLEANUP:
                self.close(code)

    def close_batch(self, batch):
        with self._lock:
            if self._batch is batch:
                self._pending = batch.plan.session_id
                self._batch = None
                if self._state != 'CLOSED':
                    self._state = 'CLEANUP_PENDING'

    def finish_round(self, report):
        """Only a fully cleaned, authenticated workload failure permits reuse."""
        if self._state == 'CLOSED':
            return
        with self._lock:
            self._live()
            expected = (self._pending, self._index, self._id)
        try:
            session = report.get('credential_session')
            _require(type(session) is dict and self._pending == session['binding']['session_id']
                and session.get('development_owner_id') == self._id
                and session.get('development_capture_index') == self._index
                and self._state == 'CLEANUP_PENDING'
                and report.get('source_preserved') is True and report.get('cleanup_errors') == []
                and report.get('private_material_root_released') is True
                and report.get('private_execution_source_root_released') is True
                and all(not Path(report[key]).exists() for key in ('private_material_root', 'private_execution_source_root')))
            operations = report['profile_operations']
            results = report['profile_results']
            profiles = ('FRESH_BASE', 'DOCKER_BASE', 'RUNTIME_BASE_OFFLINE')
            _require(type(operations) is dict and bool(operations)
                and type(results) is dict and set(results) == set(profiles)
                and set(session['profiles']) == set(profiles))
            statuses = [results[profile]['status'] for profile in profiles]
            if report.get('status') == 'PASS':
                _require(report.get('all_profiles_pass') is True and statuses == ['PASS'] * 3)
            else:
                _require(report.get('status') == 'FAIL' and report.get('all_profiles_pass') is False
                    and statuses.count('ERROR') == 1)
                failed = statuses.index('ERROR')
                _require(statuses == ['PASS'] * failed + ['ERROR'] + ['NOT_RUN_SHARED_BLOCKER'] * (2 - failed))
            _require(set(operations) == {profile for profile in profiles if results[profile]['status'] in {'PASS', 'ERROR'}})
            for profile in profiles:
                if results[profile]['status'] == 'NOT_RUN_SHARED_BLOCKER':
                    _require(all(role['delivery_attempts'] == role['delivery_completed'] == 0
                        and role['operation_result'] == 'NOT_RUN' for role in session['profiles'][profile].values()))
            for profile, operation in operations.items():
                roles = session['profiles'][profile]
                passed = roles['CANDIDATE_WORKLOAD']['operation_result'] == 'PASS'
                _require(passed == (results[profile]['status'] == 'PASS'))
                _require(operation.get('power_state') == 'STOPPED'
                    and operation.get('clone_disposition') == ('REMOVED' if passed else 'QUARANTINED')
                    and (not passed or not Path(operation['clone_vmx']).exists())
                    and operation.get('cleanup_errors') == []
                    and all(operation.get(key) is True for key in ('session_keys_removed', 'known_hosts_removed', 'lease_released')))
                for role in roles.values():
                    _require(role['delivery_attempts'] == role['delivery_completed'] == 1
                        and role['target_verified'] is True and role['lease_verified'] is True)
                _require(all(roles[role]['operation_result'] == 'PASS' for role in ('BOOTSTRAP_ROTATION', 'VERIFIED_SUDO')))
                if roles['CANDIDATE_WORKLOAD']['operation_result'] != 'PASS':
                    diagnostic = report['workload_diagnostics'][profile]
                    _require(authenticated_execution_failure(diagnostic))
            with self._lock:
                self._live()
                _require(expected == (self._pending, self._index, self._id) and self._state == "CLEANUP_PENDING")
                self._pending = None
                if report.get('all_profiles_pass') is True and report.get('status') == 'PASS':
                    _require(set(operations) == {'FRESH_BASE', 'DOCKER_BASE', 'RUNTIME_BASE_OFFLINE'}
                        and all(role['operation_result'] == 'PASS' for profile in session['profiles'].values()
                            for role in profile.values()))
                    self.close('DEVELOPMENT_PREACCEPTANCE_PASSED')
                elif self._index >= self._limit:
                    self.close('DEVELOPMENT_ROUND_BUDGET_EXHAUSTED')
                else:
                    self._state = 'READY'
        except BaseException:
            self.close('DEVELOPMENT_SESSION_CLEANUP_OR_AUTHORITY_UNCERTAIN')
            raise

    def close(self, reason='DEVELOPMENT_SESSION_STOPPED'):
        with self._lock:
            if type(self._secret) is bytearray:
                self._secret[:] = b'\0' * len(self._secret)
                self._secret.clear()
            self._secret = None
            self._cleanup = 'BEST_EFFORT_COMPLETED' if self._attempts else 'NOT_REQUIRED'
            self._state = 'CLOSED'
            self._reason = self._reason or reason
            self._done.set()
            if self._batch is not None:
                self._batch.cancelled.set()

    def dispose(self):
        self.close()
        self._thread.join(timeout=2)
        self._scope_owner.close()

    def _monitor(self):
        while not self._done.wait(0.25):
            with self._lock:
                if self._clock() >= self._deadline:
                    self.close('DEVELOPMENT_SESSION_EXPIRED')
                    return


def acquire_development_session_owner(*, material_identity, clock=time.monotonic):
    from release.materials import reject_duplicate_json_keys
    from scripts import development_capture_scope as scope
    scope_owner = scope.acquire_development_scope_owner()
    try:
        raw = (scope.LEDGER / 'scope.json').read_bytes()
        _require(len(raw) <= 4096)
        value = json.loads(raw, object_pairs_hook=reject_duplicate_json_keys)
        _require(scope._has_additional_capture_grant(value) and value['material_identity'] == material_identity)
        deadline, observation = scope.scope_deadline(value)
        used = [(scope.LEDGER / slot).exists() for slot in scope.SLOTS]
        _require(used == sorted(used, reverse=True) and 8 <= sum(used) < scope.ADDITIONAL_MAX_CAPTURES)
    except BaseException:
        scope_owner.close()
        raise
    owner = object.__new__(DevelopmentSessionOwner)
    owner._id, owner._material = uuid.uuid4().hex, dict(material_identity)
    owner._clock, owner._deadline = clock, deadline
    owner._expires_utc = observation['effective_expires_utc_seconds']
    owner._limit, owner._index = scope.ADDITIONAL_MAX_CAPTURES, sum(used)
    owner._lock, owner._done = threading.RLock(), threading.Event()
    owner._scope_owner = scope_owner
    owner._secret = owner._batch = owner._pending = owner._reason = None
    owner._attempts = owner._completed = 0
    owner._sessions, owner._clones = set(), set()
    owner._state, owner._cleanup = 'READY', 'NOT_REQUIRED'
    owner._thread = threading.Thread(target=owner._monitor, daemon=True)
    owner._thread.start()
    return owner
