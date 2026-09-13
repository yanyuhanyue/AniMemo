"""One native capture owned by one frozen Candidate batch, with fixed role grants."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import threading
import time
from pathlib import Path
from types import MappingProxyType

from release.formal_windows_pretrust import create_windows_private_named_directory, hold_windows_private_path_chain
from scripts import candidate_vm_harness as h
from scripts.guest_console_capture import WindowsConsoleCapture
from scripts.guest_sudo_session import ControllerFailure, wipe
from scripts.isolated_guest_validation import _check_checkout

AUTHORIZATION = 'ANIMEMO_V2_CANDIDATE_WORKLOAD_DIAGNOSTICS_SINGLE_CAPTURE_V1'
LEDGER = Path('E:/') / hashlib.sha256(AUTHORIZATION.encode('ascii')).hexdigest()
GATEWAY_REPAIR_AUTHORIZATION = 'ANIMEMO_V2_CANDIDATE_GATEWAY_REPAIR_SINGLE_CAPTURE_V1'
PR247_REVALIDATION_AUTHORIZATION = 'ANIMEMO_V2_CANDIDATE_PR247_REVALIDATION_SINGLE_CAPTURE_V1'
# These are separately authorized, globally single-use scopes. Recognizing a
# scope does not grant it; the operator must obtain its explicit authorization.
CAPTURE_LEDGERS = MappingProxyType({
    AUTHORIZATION: LEDGER,
    GATEWAY_REPAIR_AUTHORIZATION: Path('E:/') / hashlib.sha256(GATEWAY_REPAIR_AUTHORIZATION.encode('ascii')).hexdigest(),
    PR247_REVALIDATION_AUTHORIZATION: Path('E:/') / hashlib.sha256(PR247_REVALIDATION_AUTHORIZATION.encode('ascii')).hexdigest(),
})
ROLES = ('BOOTSTRAP_ROTATION', 'VERIFIED_SUDO', 'CANDIDATE_WORKLOAD')
HARD_SECONDS = 12 * 60 * 60
IDLE_SECONDS = 30 * 60
OPERATION_SECONDS = {'PREPARATION': 2 * 60 * 60, 'BOOTSTRAP': 300,
    'TRANSFER': 60 * 60, 'WORKLOAD': 5 * 60 * 60, 'CLEANUP': 30 * 60}
REVOCATIONS = frozenset(('CANDIDATE_SECRET_USE_SCOPE_CHANGED', 'CANDIDATE_SECRET_ROLE_REJECTED',
    'CANDIDATE_SECRET_USE_FAILED', 'CANDIDATE_BATCH_SCOPE_CHANGED', 'CANDIDATE_BATCH_HARD_EXPIRED',
    'CANDIDATE_BATCH_OPERATION_EXPIRED', 'CANDIDATE_BATCH_IDLE_EXPIRED', 'CANDIDATE_BATCH_CAPTURE_FAILED',
    'CANDIDATE_BATCH_GRANT_REJECTED', 'CANDIDATE_BATCH_ROLE_REPLAY', 'CANDIDATE_BATCH_DELIVERY_UNCERTAIN',
    'CANDIDATE_BOOTSTRAP_OR_AUTHENTICATION_UNCERTAIN', 'CANDIDATE_WORKLOAD_AUTHORITY_OR_DELIVERY_UNCERTAIN',
    'CANDIDATE_BATCH_PROFILE_FAILURE', 'CANDIDATE_PROFILE_CLEANUP_FAILED',
    'CANDIDATE_BATCH_RECEIPT_AUTHORITY_INVALID', 'CANDIDATE_BATCH_EXECUTION_INTERRUPTED',
    'CANDIDATE_BATCH_USE_ORDER_INVALID'))


def capture_ledger(authorization_id):
    if type(authorization_id) is not str or authorization_id not in CAPTURE_LEDGERS:
        raise ControllerFailure('CANDIDATE_CAPTURE_AUTHORIZATION_INVALID')
    return CAPTURE_LEDGERS[authorization_id]


def reserve_capture(authorization_id=None):
    ledger = capture_ledger(authorization_id)
    try:
        return create_windows_private_named_directory(ledger.parent, name=ledger.name)
    except Exception:
        raise ControllerFailure('CANDIDATE_CAPTURE_ALREADY_ATTEMPTED') from None


class BatchUse:
    """A supervisor may deliver to its validated process; it cannot read a secret."""
    __slots__ = ('_batch', '_profile', '_lease', '_roles', '_supervisor', '_closed')

    def __init__(self, *_args, **_kwargs):
        raise TypeError('Batch uses are issued only by the owning batch')

    def __repr__(self):
        return '<CandidateBatchUse>'

    def __reduce__(self):
        raise TypeError('Candidate batch uses cannot be serialized')

    def bind(self, supervisor):
        if self._closed or self._supervisor is not None:
            raise ControllerFailure('CANDIDATE_SECRET_USE_REPLAY')
        self._supervisor = supervisor
        self.check(supervisor)

    def check(self, supervisor):
        batch = self._batch
        batch.require_live()
        if (self._closed or supervisor is not self._supervisor
                or supervisor._provider is not batch.provider
                or supervisor._plan is not batch.plan or supervisor._execution is not batch.execution
                or supervisor._profile is not self._profile or supervisor._lease is not self._lease):
            batch.revoke('CANDIDATE_SECRET_USE_SCOPE_CHANGED')
            raise ControllerFailure('CANDIDATE_SECRET_USE_SCOPE_CHANGED')

    def deliver(self, supervisor, grant, process, role):
        self.check(supervisor)
        if role not in self._roles:
            self._batch.revoke('CANDIDATE_SECRET_ROLE_REJECTED')
            raise ControllerFailure('CANDIDATE_SECRET_ROLE_REJECTED')
        self._batch._deliver(self, supervisor, grant, process, role)

    def close(self, *, failed=False):
        self._closed = True
        if failed:
            self._batch.revoke('CANDIDATE_SECRET_USE_FAILED')


class CandidateBatch:
    def __init__(self, provider, plan, *, authorization_id=None, clock=time.monotonic, development_owner=None):
        from scripts.development_plan import is_development_plan
        self._development = is_development_plan(plan)
        self._development_reservation = None
        self._authorization_id = authorization_id
        self._development_owner = development_owner
        if development_owner is not None:
            from scripts.development_session_owner import DevelopmentSessionOwner
            if not self._development or type(development_owner) is not DevelopmentSessionOwner or development_owner.closed:
                raise ControllerFailure('DEVELOPMENT_SESSION_OWNER_INVALID')
        if self._development:
            from scripts import development_capture_scope as scope
            from scripts.development_source import require_development_source
            if authorization_id != scope.AUTHORIZATION:
                raise ControllerFailure('DEVELOPMENT_CAPTURE_AUTHORIZATION_INVALID')
            require_development_source(provider, plan)
            self._ledger = scope.LEDGER
        else:
            self._ledger = capture_ledger(authorization_id)
        if (type(provider) is not h.ClosedVmwareProvider
                or not (type(plan) is h.CandidateHarnessPlan or self._development)
                or h.sha256_bytes(h.canonical_json_bytes(plan.identity_body())) != plan.plan_digest):
            raise ControllerFailure('CANDIDATE_BATCH_SCOPE_INVALID')
        provider._require_active_execution_authority()
        self.provider, self.plan, self.execution = provider, plan, provider._execution
        self._clock = clock
        self._lock = threading.RLock()
        self._secret = None
        self._slot = None
        self._captured_at = None
        self._last_idle = None
        self._operation = None
        self._uses = []
        self._attempted_roles = set()
        self._completed_roles = set()
        self._closed = False
        self._monitor_done = threading.Event()
        self._monitor_thread = None
        self.cancelled = threading.Event()
        self._record = dict(authorization_id=authorization_id, session_capture_attempts=0,
            session_capture_completed=0, secret_state='NOT_CAPTURED', secret_cleanup='NOT_REQUIRED',
            revocation_code=None, hard_limit_seconds=HARD_SECONDS, idle_limit_seconds=IDLE_SECONDS,
            binding={key: getattr(plan, key) for key in ('plan_digest', 'session_id', 'source_sha',
                'source_tree', 'qualification_run_id', 'candidate_input_digest', 'verified_candidate_digest')},
            profiles={profile: {role: dict(delivery_attempts=0, delivery_completed=0,
                target_verified=False, lease_verified=False, operation_result='NOT_RUN')
                for role in ROLES} for profile in h.PROFILES})
        if self._development:
            self._record['purpose'] = 'LOCAL_INSTALLER_DEVELOPMENT'
            self._record['binding'].update(execution_source_sha=plan.execution_source_sha,
                execution_source_tree=plan.execution_source_tree,
                execution_inventory_digest=plan.execution_inventory_digest)
        if self._development_owner is not None:
            self._record['development_owner_id'] = development_owner.record['owner_id']
            self._record['secret_storage'] = 'PERSISTENT_DEVELOPMENT_MEMORY_OWNER'

    def check_source(self):
        from scripts.development_plan import execution_source
        _check_checkout(*execution_source(self.plan))

    @property
    def record(self):
        import json
        with self._lock:
            return json.loads(h.canonical_json_bytes(self._record))

    def __repr__(self):
        return '<CandidateBatch>'

    def __reduce__(self):
        raise TypeError('Candidate batches cannot be serialized')

    def _scope(self):
        if (self.provider._execution is not self.execution
                or h.sha256_bytes(h.canonical_json_bytes(self.plan.identity_body())) != self.plan.plan_digest):
            self.revoke('CANDIDATE_BATCH_SCOPE_CHANGED')
            raise ControllerFailure('CANDIDATE_BATCH_SCOPE_CHANGED')
        self.provider._require_active_execution_authority()
        if self._development:
            from scripts.development_source import require_development_source
            try:
                require_development_source(self.provider, self.plan)
            except BaseException:
                self.revoke('CANDIDATE_BATCH_SCOPE_CHANGED')
                raise ControllerFailure('DEVELOPMENT_SOURCE_AUTHORITY_INVALID') from None

    def _expiry(self):
        if self._captured_at is None:
            return None
        now = self._clock()
        if (now >= self._captured_at + HARD_SECONDS
                or self._development_reservation is not None and now >= self._development_reservation.deadline):
            return 'CANDIDATE_BATCH_HARD_EXPIRED'
        if self._operation is not None:
            if now >= self._operation[2]:
                return 'CANDIDATE_BATCH_OPERATION_EXPIRED'
        elif now >= self._last_idle + IDLE_SECONDS:
            return 'CANDIDATE_BATCH_IDLE_EXPIRED'
        return None

    def require_active(self):
        with self._lock:
            expired = self._expiry()
            if expired:
                self.revoke(expired)
            if self._closed or self.cancelled.is_set():
                raise ControllerFailure(self._record['revocation_code'] or 'CANDIDATE_BATCH_CLOSED')
            self._scope()

    def require_live(self):
        self.require_active()
        if self._secret is None:
            raise ControllerFailure('CANDIDATE_BATCH_SECRET_UNAVAILABLE')
        if self._development_owner is not None:
            self._secret.require_live(self)

    @contextmanager
    def operation(self, kind, profile):
        if kind not in OPERATION_SECONDS or profile not in self.plan.profiles:
            raise ControllerFailure('CANDIDATE_BATCH_OPERATION_INVALID')
        with self._lock:
            if self._operation is not None:
                raise ControllerFailure('CANDIDATE_BATCH_OPERATION_OVERLAP')
            if self._captured_at is not None and kind != 'CLEANUP':
                self.require_active()
            self._operation = (kind, profile.profile, self._clock() + OPERATION_SECONDS[kind])
        try:
            yield
            if self._captured_at is not None and kind != 'CLEANUP':
                self.require_active()
        finally:
            with self._lock:
                self._operation = None
                self._last_idle = self._clock()

    def cleanup_started(self, profile):
        with self._lock:
            self._operation = ('CLEANUP', profile.profile, self._clock() + OPERATION_SECONDS['CLEANUP'])

    def cleanup_finished(self):
        with self._lock:
            self._operation = None
            self._last_idle = self._clock()

    def capture_after_bootstrap_observation(self, profile):
        with self._lock:
            if self._captured_at is not None:
                self.require_live()
                return
            if (self._closed or self._record['session_capture_attempts'] or profile is not self.plan.profiles[0]
                    or self._operation is None or self._operation[:2] != ('BOOTSTRAP', profile.profile)):
                raise ControllerFailure('CANDIDATE_BATCH_CAPTURE_REJECTED')
            self._scope()
            self.check_source()
            console = WindowsConsoleCapture()
            console.preflight()
            if self._development:
                from scripts import development_capture_scope as scope
                if self._ledger != scope.LEDGER:
                    raise ControllerFailure('DEVELOPMENT_CAPTURE_AUTHORIZATION_INVALID')
                try:
                    material_identity = {key: getattr(self.plan, key) for key in (
                        'verified_candidate_digest', 'candidate_input_digest', 'source_sha',
                        'source_tree', 'qualification_run_id', 'candidate_version')}
                    material_identity['qualification_run_attempt'] = 1
                    options = ({'scope_owner': self._development_owner.scope_owner}
                        if self._development_owner is not None else {})
                    self._development_reservation = scope.reserve_development_capture(
                        self._authorization_id, material_identity=material_identity, **options)
                except scope.DevelopmentScopeError as error:
                    raise ControllerFailure(error.code) from None
                self._slot = self._development_reservation.path
                self._record['development_capture_index'] = self._development_reservation.index
                self._record['development_clock_observation'] = self._development_reservation.time_observation
            else:
                if capture_ledger(self._authorization_id) != self._ledger:
                    raise ControllerFailure('CANDIDATE_CAPTURE_AUTHORIZATION_INVALID')
                self._slot = reserve_capture(self._authorization_id)
            owner_before = self._development_owner.record if self._development_owner is not None else None
            self._record['session_capture_attempts'] = 1 if owner_before is None else 0
            self._record['secret_state'] = 'CAPTURING'
            try:
                if self._development_owner is None:
                    print('Candidate session / one sudo input for this frozen batch', flush=True)
                    self._secret = console.capture()
                    if (type(self._secret) is not bytearray or not 1 <= len(self._secret) <= 4096
                            or any(x < 32 or x == 127 for x in self._secret)):
                        raise ControllerFailure('SUDO_VALUE_INVALID')
                    self._record['session_capture_completed'] = 1
                else:
                    print('Local development / same memory controller across remaining rounds', flush=True)
                    self._secret, captured = self._development_owner.bind_after_verified_guest(
                        self, self._development_reservation, console)
                    self._record['session_capture_attempts'] = captured
                    self._record['session_capture_completed'] = captured
                    self._record['owner_capture_completed'] = 1
                self._captured_at = self._last_idle = self._clock()
                self._operation = ('BOOTSTRAP', profile.profile, self._captured_at + OPERATION_SECONDS['BOOTSTRAP'])
                self.check_source()
                self._scope()
                self._record['secret_state'] = 'ACTIVE'
                self._monitor_thread = threading.Thread(target=self._monitor, daemon=True)
                self._monitor_thread.start()
            except BaseException:
                if owner_before is not None:
                    owner_after = self._development_owner.record
                    self._record['session_capture_attempts'] = owner_after['capture_attempts'] - owner_before['capture_attempts']
                    self._record['session_capture_completed'] = owner_after['capture_completed'] - owner_before['capture_completed']
                self.revoke('CANDIDATE_BATCH_CAPTURE_FAILED')
                raise

    def _monitor(self):
        while not self._monitor_done.wait(0.25):
            with self._lock:
                code = self._expiry()
                if code:
                    self.revoke(code)
                    return

    def issue(self, profile, lease, roles):
        with self._lock:
            self.require_live()
            if (profile not in self.plan.profiles or roles not in (ROLES[:2], ROLES[2:])
                    or self._operation is None or self._operation[:2] !=
                    (('BOOTSTRAP' if roles == ROLES[:2] else 'WORKLOAD'), profile.profile)):
                raise ControllerFailure('CANDIDATE_BATCH_USE_INVALID')
            previous = self.plan.profiles[:self.plan.profiles.index(profile)]
            if (any(self._record['profiles'][item.profile]['CANDIDATE_WORKLOAD']['operation_result']
                    not in {'PASS', 'FAIL'} for item in previous)
                    or (roles == ROLES[2:] and any(self._record['profiles'][profile.profile][role]['operation_result']
                        != 'PASS' for role in ROLES[:2]))):
                self.revoke('CANDIDATE_BATCH_USE_ORDER_INVALID')
                raise ControllerFailure('CANDIDATE_BATCH_USE_ORDER_INVALID')
            lease.require_open()
            use = object.__new__(BatchUse)
            use._batch, use._profile, use._lease, use._roles = self, profile, lease, roles
            use._supervisor, use._closed = None, False
            self._uses.append(use)
            return use

    def _deliver(self, use, supervisor, grant, process, role):
        with self._lock:
            self.require_live()
            if (use not in self._uses or grant.owner is not supervisor or grant.process is not process
                    or grant.execution is not self.execution or grant.role != role or not grant.used
                    or supervisor._clock() >= grant.expires or process.poll() is not None):
                self.revoke('CANDIDATE_BATCH_GRANT_REJECTED')
                raise ControllerFailure('CANDIDATE_BATCH_GRANT_REJECTED')
            use._lease.require_open()
        # Git may block. Let the owner watchdog revoke while this non-secret
        # check runs, then recheck the grant at the actual delivery boundary.
        self.check_source()
        with self._lock:
            use.check(supervisor)
            use._lease.require_open()
            if supervisor._clock() >= grant.expires or process.poll() is not None:
                self.revoke('CANDIDATE_BATCH_GRANT_REJECTED')
                raise ControllerFailure('CANDIDATE_BATCH_GRANT_REJECTED')
            entry = self._record['profiles'][use._profile.profile][role]
            key = (use._profile.profile, role)
            if key in self._attempted_roles:
                self.revoke('CANDIDATE_BATCH_ROLE_REPLAY')
                raise ControllerFailure('CANDIDATE_BATCH_ROLE_REPLAY')
            if any((use._profile.profile, prior) not in self._completed_roles for prior in ROLES[:ROLES.index(role)]):
                self.revoke('CANDIDATE_BATCH_USE_ORDER_INVALID')
                raise ControllerFailure('CANDIDATE_BATCH_USE_ORDER_INVALID')
            self._attempted_roles.add(key)
            entry.update(target_verified=True, lease_verified=True, delivery_attempts=1, operation_result='UNKNOWN')
            value = (self._secret.delivery_value(self) if self._development_owner is not None
                else bytearray(self._secret))
            value.append(10)
        try:
            written = process.stdin.write(value)
            if type(written) is not int or written != len(value):
                raise ControllerFailure('GUEST_STDIN_SHORT_WRITE')
            process.stdin.flush()
            process.stdin.close()
            with self._lock:
                entry['delivery_completed'] = 1
                self._completed_roles.add(key)
                if len(self._completed_roles) == len(h.PROFILES) * len(ROLES):
                    self.release_secret()
        except BaseException:
            self.revoke('CANDIDATE_BATCH_DELIVERY_UNCERTAIN')
            raise
        finally:
            wipe(value)

    def role_result(self, profile, role, result):
        with self._lock:
            if (profile not in self.plan.profiles or role not in ROLES or result not in {'PASS', 'FAIL', 'ERROR', 'UNKNOWN'}
                    or (result in {'PASS', 'FAIL'} and (profile.profile, role) not in self._completed_roles)):
                raise ControllerFailure('CANDIDATE_BATCH_RESULT_INVALID')
            self._record['profiles'][profile.profile][role]['operation_result'] = result

    def revoke(self, code):
        with self._lock:
            if code not in REVOCATIONS:
                code = 'CANDIDATE_BATCH_EXECUTION_INTERRUPTED'
            if self._development_owner is not None:
                self._development_owner.on_revocation(self, code)
            self._record['revocation_code'] = self._record['revocation_code'] or code
            self.cancelled.set()
            self._finish('REVOKED')

    def _finish(self, state):
        self._closed = True
        if type(self._secret) is bytearray:
            wipe(self._secret)
        elif self._secret is not None and self._development_owner is not None:
            self._secret.close()
        self._secret = None
        self._record['secret_state'] = ('ROUND_CAPABILITY_' + state if self._development_owner is not None else state)
        self._record['secret_cleanup'] = 'BEST_EFFORT_COMPLETED' if self._record['session_capture_attempts'] else 'NOT_REQUIRED'
        if self._development_owner is not None:
            self._record['secret_cleanup'] = 'ROUND_CAPABILITY_CLOSED'
        self._monitor_done.set()
        for use in self._uses:
            use._closed = True

    def release_secret(self):
        with self._lock:
            if self._secret is not None:
                if self._development_owner is not None:
                    self._secret.close()
                else:
                    wipe(self._secret)
                self._secret = None
            if not self._closed:
                self._record['secret_state'] = 'RELEASED_NO_FURTHER_USES'
                self._record['secret_cleanup'] = 'BEST_EFFORT_COMPLETED'
                if self._development_owner is not None:
                    self._record['secret_state'] = 'ROUND_CAPABILITY_RELEASED_NO_FURTHER_USES'
                    self._record['secret_cleanup'] = 'ROUND_CAPABILITY_CLOSED'

    def close(self):
        with self._lock:
            self.cancelled.set()
            if not self._closed:
                self._finish('CLOSED')
            try:
                if self._slot is not None:
                    # The development reservation already holds its parent
                    # chain; a second Windows DELETE hold would conflict.
                    if self._development:
                        with (self._slot / 'result.json').open('xb') as output:
                            output.write(h.canonical_json_bytes(self._record))
                    else:
                        with hold_windows_private_path_chain(self._slot, allow_leaf_child_writes=True):
                            with (self._slot / 'result.json').open('xb') as output:
                                output.write(h.canonical_json_bytes(self._record))
                    self._slot = None
            finally:
                if self._development_reservation is not None:
                    self._development_reservation.close()
                    self._development_reservation = None
                if self._development_owner is not None:
                    self._development_owner.close_batch(self)
        if self._monitor_thread is not None and self._monitor_thread is not threading.current_thread():
            self._monitor_thread.join(timeout=2)
            if self._monitor_thread.is_alive():
                raise ControllerFailure('CANDIDATE_BATCH_MONITOR_NOT_CLOSED')
