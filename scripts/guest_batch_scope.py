"""One local confirmation, one capture, bounded rounds of existing VM plans.

This is accounting/consent, not source, Guest, Candidate or Release authority.
The existing provider, verifier and same-connection grant remain mandatory.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from contextlib import ExitStack
from pathlib import Path

from release.formal_windows_pretrust import (
    create_windows_private_named_directory, hold_windows_private_path_chain,
    hold_windows_private_working_directory,
)
from scripts.guest_console_capture import WindowsConsoleCapture
from scripts.guest_sudo_session import ControllerFailure

DEVELOPMENT_AUTHORIZATION = 'ANIMEMO_V2_ATTESTATION_FORMAL_SEAMS_LOCAL_DEV_V1'
RETIRED_FORMAL_AUTHORIZATION = 'ANIMEMO_V2_RC1_FORMAL_SINGLE_CAPTURE_V1'
PURPOSES = {'LOCAL_INSTALLER_DEVELOPMENT': 12, 'CANDIDATE_ACCEPTANCE': 1, 'FORMAL_POSTPUBLICATION': 1}
WINDOW_SECONDS = 24 * 60 * 60
_ISSUER = object()


def _require(value):
    if not value:
        raise ControllerFailure('LOCAL_BATCH_AUTHORIZATION_INVALID')


def _write(path, value):
    from scripts.candidate_vm_harness import canonical_json_bytes
    import os
    with path.open('xb') as stream:
        stream.write(canonical_json_bytes(value))
        stream.flush()
        os.fsync(stream.fileno())


def material_identity(plan):
    return {key: getattr(plan, key) for key in (
        'source_sha', 'source_tree', 'qualification_run_id', 'candidate_version',
        'candidate_input_digest', 'verified_candidate_digest')}


def _require_plan(purpose, plan):
    from scripts.candidate_vm_harness import (
        CandidateHarnessError, CandidateHarnessPlan, PROFILES, canonical_json_bytes, sha256_bytes,
    )
    from scripts.development_plan import confirmed_profile_names, is_development_plan
    from scripts.formal_plan import is_formal_plan
    accepted = (is_development_plan(plan) if purpose == 'LOCAL_INSTALLER_DEVELOPMENT'
        else type(plan) is CandidateHarnessPlan if purpose == 'CANDIDATE_ACCEPTANCE'
        else is_formal_plan(plan) if purpose == 'FORMAL_POSTPUBLICATION' else False)
    _require(accepted)
    try:
        expected_profiles = confirmed_profile_names(plan.identity_body()) if is_development_plan(plan) else PROFILES
    except CandidateHarnessError:
        raise ControllerFailure('LOCAL_BATCH_AUTHORIZATION_INVALID') from None
    _require(sha256_bytes(canonical_json_bytes(plan.identity_body())) == plan.plan_digest
        and tuple(p.profile for p in plan.profiles) == expected_profiles
        and len({p.clone_identity for p in plan.profiles}) == len(expected_profiles))


class LocalRoundReservation:
    def __init__(self, issuer, scope, index, path):
        _require(issuer is _ISSUER)
        self.scope, self.index, self.path = scope, index, path
        self.deadline = scope.deadline
        self.time_observation = {'effective_expires_utc_seconds': scope.expires_utc}
        self._closed = False
        self._holds = ExitStack()
        self._holds.enter_context(hold_windows_private_working_directory(path))

    def require_open(self):
        self.scope.require_open()
        _require(not self._closed and self in self.scope._reservations)

    def close(self):
        if not self._closed:
            self._closed = True
            self._holds.close()

    def __reduce__(self):
        raise TypeError('Round reservations cannot be serialized')


class LocalBatchAuthorization:
    def __init__(self, issuer, *, body, root, holds, monotonic_now, runtime_handoff=None):
        _require(issuer is _ISSUER)
        self._body = json.loads(json.dumps(body))
        self.root, self._holds = root, holds
        self.deadline = monotonic_now + WINDOW_SECONDS
        self.expires_utc = body['confirmed_utc_seconds'] + WINDOW_SECONDS
        initial = body.get('initial_plan', {})
        if initial.get('runtimeBaselineOnly') is True:
            _require(type(body.get('capture_limit')) is int and body['capture_limit'] == 0)
        self._runtime_handoff = runtime_handoff
        if initial.get('runtimeOfflineOnly') is True:
            from scripts.runtime_target_handoff import RuntimeTargetHandoff
            _require(type(runtime_handoff) is RuntimeTargetHandoff)
            from scripts.runtime_development_boundary import (
                parse_authorization_deadline,
            )
            expires = parse_authorization_deadline(initial.get('runtimeAuthorizationDeadline'))
            self.expires_utc = min(self.expires_utc, expires)
            self.deadline = min(self.deadline, monotonic_now + expires - body['confirmed_utc_seconds'])
            target_expires, target_deadline = runtime_handoff.expiry_limits
            self.expires_utc = min(self.expires_utc, target_expires)
            self.deadline = min(self.deadline, target_deadline)
        else:
            _require(runtime_handoff is None)
        self.purpose, self.authorization_id = body['purpose'], body['authorization_id']
        self.round_limit = body['round_limit']
        _require(type(self.round_limit) is int and 1 <= self.round_limit <= PURPOSES[self.purpose])
        self._reservations, self._sessions, self._clones = [], set(), set()
        self._closed, self._capture_consumed = False, False
        self._lock = threading.RLock()

    @property
    def body(self):
        return json.loads(json.dumps(self._body))

    def require_open(self):
        _require(not self._closed and time.monotonic() < self.deadline
            and time.time() < self.expires_utc)
        if self._runtime_handoff is not None:
            self._runtime_handoff.require_consent_open()

    def require_runtime_target(self, provider, plan):
        self.require_open()
        if getattr(plan, 'runtime_offline_only', False):
            _require(self._runtime_handoff is not None)
            self._runtime_handoff.require_active_target(provider, plan)
        else:
            _require(self._runtime_handoff is None)

    def reserve_round(self, plan):
        from scripts.candidate_vm_harness import canonical_json_bytes, sha256_bytes
        with self._lock:
            self.require_open()
            if self._runtime_handoff is not None:
                self._runtime_handoff.require_frozen_target()
            _require_plan(self.purpose, plan)
            if self.purpose == 'LOCAL_INSTALLER_DEVELOPMENT':
                initial, current = self._body['initial_plan'], plan.identity_body()
                _require(current['developmentMode'] == initial['developmentMode']
                    and current.get('runtimeOfflineOnly', False) == initial.get('runtimeOfflineOnly', False)
                    and current.get('runtimeBaselineOnly', False) == initial.get('runtimeBaselineOnly', False)
                    and all(current.get(name) == initial.get(name) for name in (
                        'runtimeAuthorizationDeadline', 'runtimeTrustSelectionDigest', 'runtimeRetentionPolicy'))
                    and (not current.get('runtimeOfflineOnly') or self.round_limit == 1))
            _require(material_identity(plan) == self._body['material_identity']
                and sha256_bytes(canonical_json_bytes(plan.identity_body())) == plan.plan_digest
                and len(self._reservations) < self.round_limit
                and plan.session_id not in self._sessions
                and not ({p.clone_identity for p in plan.profiles} & self._clones))
            if not self._reservations:
                _require(plan.plan_digest == self._body['initial_plan_digest'])
            index = len(self._reservations) + 1
            path = create_windows_private_named_directory(self.root,
                name=hashlib.sha256(f'round-{index:02d}'.encode('ascii')).hexdigest())
            _write(path/'plan.json', plan.as_dict())
            reservation = LocalRoundReservation(_ISSUER, self, index, path)
            self._reservations.append(reservation)
            self._sessions.add(plan.session_id)
            self._clones.update(p.clone_identity for p in plan.profiles)
            return reservation

    def consume_capture(self):
        with self._lock:
            self.require_open()
            _require(self._body.get('capture_limit', 1) == 1
                and self._body.get('initial_plan', {}).get('runtimeBaselineOnly', False) is False)
            if self._runtime_handoff is not None:
                self._runtime_handoff.require_frozen_target()
            _require(not self._capture_consumed and bool(self._reservations))
            _write(self.root/'capture-attempt.json', {'attempt': 1, 'authorization_id': self.authorization_id})
            self._capture_consumed = True

    def close(self):
        with self._lock:
            if not self._closed:
                self._closed = True
                try:
                    for reservation in self._reservations:
                        reservation.close()
                finally:
                    self._holds.close()

    def __reduce__(self):
        raise TypeError('Local batch consent cannot be serialized')


def validate_authorization_id(authorization_id, purpose):
    """Validate a label; only native confirmation can issue runtime consent."""
    _require(type(authorization_id) is str and re.fullmatch('[A-Z][A-Z0-9_]{15,159}', authorization_id)
        and authorization_id != RETIRED_FORMAL_AUTHORIZATION and purpose in PURPOSES)
    _require(authorization_id != DEVELOPMENT_AUTHORIZATION or purpose == 'LOCAL_INSTALLER_DEVELOPMENT')


def authorization_root(authorization_id):
    return Path('E:/')/hashlib.sha256(authorization_id.encode('ascii')).hexdigest()


def confirm_local_batch(*, authorization_id, purpose, plan, round_limit=1, runtime_handoff=None):
    from scripts.candidate_vm_harness import canonical_json_bytes, sha256_bytes
    from release.candidate_failure_policy import FAILURE_POLICY
    validate_authorization_id(authorization_id, purpose)
    _require(type(round_limit) is int and 1 <= round_limit <= PURPOSES[purpose])
    _require_plan(purpose, plan)
    runtime = getattr(plan, 'runtime_offline_only', False)
    baseline = getattr(plan, 'runtime_baseline_only', False)
    if runtime:
        from scripts.runtime_target_handoff import RuntimeTargetHandoff
        _require(purpose == 'LOCAL_INSTALLER_DEVELOPMENT' and type(runtime_handoff) is RuntimeTargetHandoff)
    else:
        _require(runtime_handoff is None)
    if getattr(plan, 'runtime_offline_only', False):
        from scripts.runtime_development_boundary import parse_authorization_deadline
        _require(time.time() < parse_authorization_deadline(plan.runtime_authorization_deadline))
    if purpose == 'LOCAL_INSTALLER_DEVELOPMENT':
        from scripts.development_plan import is_development_plan
        _require(is_development_plan(plan))
        if plan.published_subject_digest is not None or plan.runtime_offline_only:
            _require(round_limit == 1)
    elif purpose == 'CANDIDATE_ACCEPTANCE':
        from scripts.candidate_vm_harness import CandidateHarnessPlan
        _require(type(plan) is CandidateHarnessPlan)
    else:
        from scripts.formal_plan import is_formal_plan
        _require(is_formal_plan(plan))
    root = authorization_root(authorization_id)
    _require(not root.exists())
    body = {'schema': 'animemo.local-batch-confirmation/v1', 'purpose': purpose,
        'authorization_id': authorization_id, 'initial_plan_digest': plan.plan_digest,
        'material_identity': material_identity(plan), 'initial_plan': plan.as_dict(),
        'round_limit': round_limit, 'capture_limit': 0 if baseline else 1, 'window_seconds': WINDOW_SECONDS,
        'failure_policy': FAILURE_POLICY,
        'release_authority_granted': False, 'publish_authorized': False}
    body['execution_source_sha'] = getattr(plan, 'execution_source_sha', plan.source_sha)
    body['execution_source_tree'] = getattr(plan, 'execution_source_tree', plan.source_tree)
    target = None
    confirm_options = {}
    if runtime:
        target, timeout = runtime_handoff.confirmation(plan)
        _require(target['authorization_id'] == authorization_id)
        body['runtime_target_binding'] = target
        body['runtime_target_binding_digest'] = sha256_bytes(canonical_json_bytes(target))
        confirm_options['timeout_seconds'] = timeout
    console = WindowsConsoleCapture()
    prompt = ('B ONLY: approve this exact new clone for one read-only baseline diagnosis.\n'
        'Success and failure stop before password capture, sudo or Installer; retain the stopped clone.\n'
        'PREPRODUCTION ONLY. Zero password captures; cancellation/failure spends this diagnosis.\n'
        if baseline else ('B ONLY: approve this exact new clone and the existing single Runtime batch.\n'
        'A preparation does not grant clone/boot, password capture or Installer execution.\n' if runtime else '')
        + 'PREPRODUCTION ONLY. Confirm this frozen batch before any Clone.\n'
        'One password capture only; cancelling/failing does not restore it.\n'
        'A verified business failure may continue only after complete Profile cleanup; '
        'identity, delivery, timeout or cleanup uncertainty revokes the batch.\n')
    console.confirm_batch(prompt
        + json.dumps({key: value for key, value in body.items() if key != 'initial_plan'}, ensure_ascii=False, indent=2)
        + '\nProfiles: '+json.dumps([p.as_dict() for p in plan.profiles],ensure_ascii=False), **confirm_options)
    if runtime:
        runtime_handoff.accept_confirmation(plan, target)
    holds = ExitStack()
    try:
        # Exclusive task-wide directory prevents process/plan/source changes
        # from rolling the confirmation window or restoring spent captures.
        create_windows_private_named_directory(root.parent, name=root.name)
        holds.enter_context(hold_windows_private_path_chain(root, allow_leaf_child_writes=True))
        now = time.monotonic()
        body['confirmed_utc_seconds'] = time.time()
        _write(root/'scope.json', body)
        authorization = LocalBatchAuthorization(_ISSUER, body=body, root=root, holds=holds,
            monotonic_now=now, runtime_handoff=runtime_handoff)
        authorization._holds = holds.pop_all()
        return authorization
    finally:
        holds.close()
