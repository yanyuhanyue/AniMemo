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
    from scripts.candidate_vm_harness import CandidateHarnessPlan, PROFILES, canonical_json_bytes, sha256_bytes
    from scripts.development_plan import is_development_plan
    from scripts.formal_plan import is_formal_plan
    accepted = (is_development_plan(plan) if purpose == 'LOCAL_INSTALLER_DEVELOPMENT'
        else type(plan) is CandidateHarnessPlan if purpose == 'CANDIDATE_ACCEPTANCE'
        else is_formal_plan(plan) if purpose == 'FORMAL_POSTPUBLICATION' else False)
    _require(accepted)
    _require(sha256_bytes(canonical_json_bytes(plan.identity_body())) == plan.plan_digest
        and tuple(p.profile for p in plan.profiles) == PROFILES
        and len({p.clone_identity for p in plan.profiles}) == len(PROFILES))


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
    def __init__(self, issuer, *, body, root, holds, monotonic_now):
        _require(issuer is _ISSUER)
        self._body = json.loads(json.dumps(body))
        self.root, self._holds = root, holds
        self.deadline = monotonic_now + WINDOW_SECONDS
        self.expires_utc = body['confirmed_utc_seconds'] + WINDOW_SECONDS
        self.purpose, self.authorization_id = body['purpose'], body['authorization_id']
        self.round_limit = PURPOSES[self.purpose]
        self._reservations, self._sessions, self._clones = [], set(), set()
        self._closed, self._capture_consumed = False, False
        self._lock = threading.RLock()

    @property
    def body(self):
        return json.loads(json.dumps(self._body))

    def require_open(self):
        _require(not self._closed and time.monotonic() < self.deadline
            and time.time() < self.expires_utc)

    def reserve_round(self, plan):
        from scripts.candidate_vm_harness import canonical_json_bytes, sha256_bytes
        with self._lock:
            self.require_open()
            _require_plan(self.purpose, plan)
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


def confirm_local_batch(*, authorization_id, purpose, plan):
    from scripts.candidate_vm_harness import canonical_json_bytes, sha256_bytes
    _require(type(authorization_id) is str and re.fullmatch('[A-Z][A-Z0-9_]{15,159}', authorization_id)
        and authorization_id != RETIRED_FORMAL_AUTHORIZATION and purpose in PURPOSES)
    _require(authorization_id != DEVELOPMENT_AUTHORIZATION or purpose == 'LOCAL_INSTALLER_DEVELOPMENT')
    _require_plan(purpose, plan)
    if purpose == 'LOCAL_INSTALLER_DEVELOPMENT':
        from scripts.development_plan import is_development_plan
        _require(is_development_plan(plan) and authorization_id == DEVELOPMENT_AUTHORIZATION)
    elif purpose == 'CANDIDATE_ACCEPTANCE':
        from scripts.candidate_vm_harness import CandidateHarnessPlan
        _require(type(plan) is CandidateHarnessPlan)
    else:
        from scripts.formal_plan import is_formal_plan
        _require(is_formal_plan(plan))
    root = Path('E:/')/hashlib.sha256(authorization_id.encode('ascii')).hexdigest()
    _require(not root.exists())
    body = {'schema': 'animemo.local-batch-confirmation/v1', 'purpose': purpose,
        'authorization_id': authorization_id, 'initial_plan_digest': plan.plan_digest,
        'material_identity': material_identity(plan), 'initial_plan': plan.as_dict(),
        'round_limit': PURPOSES[purpose], 'capture_limit': 1, 'window_seconds': WINDOW_SECONDS,
        'release_authority_granted': False, 'publish_authorized': False}
    body['execution_source_sha'] = getattr(plan, 'execution_source_sha', plan.source_sha)
    body['execution_source_tree'] = getattr(plan, 'execution_source_tree', plan.source_tree)
    console = WindowsConsoleCapture()
    console.confirm_batch('PREPRODUCTION ONLY. Confirm this frozen batch before any Clone.\n'
        'One password capture only; cancelling/failing does not restore it.\n'
        + json.dumps({key: value for key, value in body.items() if key != 'initial_plan'}, ensure_ascii=False, indent=2)
        + '\nProfiles: '+json.dumps([p.as_dict() for p in plan.profiles],ensure_ascii=False))
    holds = ExitStack()
    try:
        # Exclusive task-wide directory prevents process/plan/source changes
        # from rolling the confirmation window or restoring spent captures.
        create_windows_private_named_directory(root.parent, name=root.name)
        holds.enter_context(hold_windows_private_path_chain(root, allow_leaf_child_writes=True))
        now = time.monotonic()
        body['confirmed_utc_seconds'] = time.time()
        _write(root/'scope.json', body)
        return LocalBatchAuthorization(_ISSUER, body=body, root=root, holds=holds.pop_all(), monotonic_now=now)
    finally:
        holds.close()
