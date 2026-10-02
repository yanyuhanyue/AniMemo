"""Native A/B consent in one live Runtime context; no new Guest authority."""
from __future__ import annotations

import json
import re
import subprocess
import time
from contextlib import ExitStack
from pathlib import Path, PureWindowsPath

from release.formal_windows_pretrust import (
    create_windows_private_named_directory,
    hold_windows_private_path_chain,
)
from scripts import candidate_vm_harness as h
from scripts.guest_console_capture import WindowsConsoleCapture
from scripts.guest_sudo_session import ControllerFailure

# This task-wide name deliberately excludes source/session/permit IDs. Changing
# arguments or restarting cannot recover the one approved preparation attempt.
ENTRY_ID = None  # Supplied only by the reviewed private operator overlay.
ENTRY_ROOT = None
_ISSUER = object()
PROFILE = 'RUNTIME_BASE_OFFLINE'
PREPARATION_EFFECTS = (
    'A ONLY: new private Provider/tool/material/source/work directories under E:\\.',
    ('Read and temporarily copy the EXISTING bootstrap identity '
    f'{h.OPENSSH_IDENTITY}; do not change the original key or ACL.'),
    'Read/check fixed tools, candidate, trust selection and offline media; make private material/source projections.',
    ('Read-only source VM power/snapshot/hash checks AND a private byte snapshot '
    'of the bound source VM files, including disks (historically about 7.5 GB).'),
    'Reserve this fixed one-attempt task entry permanently; cancellation/failure does not restore it.',
    'No clone/revert/boot, Guest SSH role, password capture, sudo, Installer, network or publishing in A.',
    'Clean only newly owned temporary roots/key copies/holders; retain work data and the spent entry.',
)


def _require(condition, code='RUNTIME_TARGET_HANDOFF_INVALID'):
    if not condition:
        raise ControllerFailure(code)


def _write(path, body):
    from scripts.guest_batch_scope import _write as write_exclusive
    write_exclusive(path, body)


def scope_report():
    """Inspect only the public checkout; never instantiate a Provider or UI."""
    root = Path(__file__).resolve().parents[1]
    def git(*args):
        return subprocess.check_output(['git', '-C', str(root), *args], timeout=30).decode('utf-8').strip()
    from scripts.runtime_ui_retry_policy import public_policy
    return {'status': 'SCOPE_ONLY', 'execution_source_sha': git('rev-parse', 'HEAD'),
        'execution_source_tree': git('rev-parse', 'HEAD^{tree}'),
        'checkout_dirty': bool(git('status', '--porcelain=v1')),
        'entry_id': ENTRY_ID, 'entry_root': None if ENTRY_ROOT is None else str(ENTRY_ROOT),
        'operator_binding_configured': ENTRY_ID is not None and ENTRY_ROOT is not None,
        'preparation_effects': list(PREPARATION_EFFECTS),
        'A_approved': False, 'B_approved': False, 'actual_session_id': None,
        'actual_clone_vmx': None, 'private_inputs_verified': False,
        'requires_rebound_selection_for_execution_source': True,
        'proposed_ui_retry_policy': public_policy(),
        'provider_instances': 0, 'credential_reads': 0, 'vm_actions': 0,
        'publish_authorized': False, 'candidate_acceptance_authority_granted': False}


def _request(args):
    _require(ENTRY_ID is not None and ENTRY_ROOT is not None, 'RUNTIME_TARGET_OPERATOR_BINDING_REQUIRED')
    from scripts.guest_batch_scope import validate_authorization_id
    from scripts.runtime_development_boundary import parse_authorization_deadline
    _require(args.runtime_offline_only is True and args.execute is True
        and args.confirm_batch is True and args.runtime_target_handoff is True)
    validate_authorization_id(args.authorization_id, 'LOCAL_INSTALLER_DEVELOPMENT')
    _require(args.authorization_id != ENTRY_ID, 'RUNTIME_TARGET_ENTRY_AND_PERMIT_MUST_DIFFER')
    from scripts.runtime_ui_retry_policy import request_retry
    retry = request_retry(args)
    from scripts.runtime_baseline_only_policy import request_diagnostic
    diagnostic = request_diagnostic(args)
    names = ('execution_source_sha', 'execution_source_tree', 'material_source_sha', 'material_source_tree')
    _require(all(type(getattr(args, name, None)) is str and h._SHA.fullmatch(getattr(args, name)) for name in names))
    digests = ('verified_candidate_digest', 'runtime_trust_selection_digest',
        'runtime_execution_inventory_digest', 'runtime_guest_inventory_digest')
    _require(all(type(getattr(args, name, None)) is str and h._DIGEST.fullmatch(getattr(args, name)) for name in digests))
    expiry = getattr(args, 'runtime_trust_expiry', None)
    _require(type(expiry) is str and re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z', expiry))
    expires = min(parse_authorization_deadline(args.runtime_authorization_deadline),
        parse_authorization_deadline(expiry))
    _require(time.time() < expires, 'RUNTIME_TARGET_HANDOFF_EXPIRED')
    _require(type(args.qualification_run_id) is int and args.qualification_run_id > 0)
    paths = ('runtime_trust_inputs', 'runtime_portable', 'runtime_release_attestation')
    _require(all(getattr(args, name, None) is not None
        and PureWindowsPath(str(getattr(args, name))).is_absolute() for name in paths))
    body = {name: getattr(args, name) for name in (*names, *digests,
        'authorization_id', 'qualification_run_id', 'runtime_authorization_deadline', 'runtime_trust_expiry')}
    body.update({name: str(getattr(args, name)) for name in paths})
    body.update(entry_id=ENTRY_ID, source_vmx=str(PureWindowsPath(str(h.SOURCE_VM_ROOT)) / (h.SOURCE_VM_IDENTITY + '.vmx')),
        profile=PROFILE, snapshot_name=h.SNAPSHOT_ALLOWLIST[PROFILE], preparation_effects=list(PREPARATION_EFFECTS))
    if retry is not None:
        body.update(entry_id=retry['retry_entry_id'], retry_policy=retry)
    if diagnostic is not None:
        _require(retry is None, 'RUNTIME_BASELINE_ONLY_FIXED_SCOPE_REQUIRED')
        body.update(entry_id=diagnostic['entry_id'], baseline_policy=diagnostic, runtime_baseline_only=True)
    return body, expires


def begin_preparation(args):
    """A native YES precedes every private read/copy and the spent entry write."""
    from scripts.guest_batch_scope import authorization_root
    body, expires = _request(args)
    deadline = time.monotonic() + expires - time.time()
    console = WindowsConsoleCapture()
    console.confirm_batch('Confirm A preparation ONLY; B must separately approve the actual target.\n'
        + json.dumps(body, ensure_ascii=False, indent=2),
        timeout_seconds=min(60, expires - time.time()))
    _require(time.time() < expires and time.monotonic() < deadline, 'RUNTIME_TARGET_HANDOFF_EXPIRED')
    holds = ExitStack()
    try:
        # No root inspection or ACL/credential access before native A consent.
        if 'retry_policy' in body:
            from scripts.runtime_ui_retry_policy import RETRY_ENTRY_ROOT
            entry_root = RETRY_ENTRY_ROOT
        elif 'baseline_policy' in body:
            from scripts.runtime_baseline_only_policy import ENTRY_ROOT as diagnostic_root
            entry_root = diagnostic_root
        else:
            entry_root = ENTRY_ROOT
        root = create_windows_private_named_directory(entry_root.parent, name=entry_root.name)
        holds.enter_context(hold_windows_private_path_chain(root, allow_leaf_child_writes=True))
        _write(root / 'attempt.json', {'schema': 'animemo.runtime-target-entry/v1',
            'request': body, 'request_digest': h.sha256_bytes(h.canonical_json_bytes(body)),
            'A_approved': True, 'confirmed_utc_seconds': time.time(), 'attempt': 1})
        _require(not authorization_root(args.authorization_id).exists(), 'RUNTIME_TARGET_PERMIT_ALREADY_SPENT')
        handoff = RuntimeTargetHandoff(_ISSUER, request=body, root=root, holds=holds,
            expires=expires, deadline=deadline)
        handoff._holds = holds.pop_all()
        return handoff
    finally:
        holds.close()


class RuntimeTargetHandoff:
    """Live consent/target accounting only; existing native batch issues B scope."""
    def __init__(self, issuer, *, request, root, holds, expires, deadline):
        _require(issuer is _ISSUER)
        self._request = json.loads(json.dumps(request))
        self._root, self._holds, self._expires = root, holds, expires
        self._deadline = deadline
        self._closed, self._B_confirmed, self._B_attempted, self._holds_released = False, False, False, False
        self._provider = self._execution = self._plan = self._source = self._inputs = None
        self._binding = None

    def _live(self):
        _require(not self._closed and time.time() < self._expires and time.monotonic() < self._deadline,
            'RUNTIME_TARGET_HANDOFF_EXPIRED_OR_CLOSED')

    def _observe(self, provider, plan):
        from installer.development_trust import RuntimeTrustInputs
        from scripts.development_source import require_development_source
        from scripts.guest_batch_scope import _require_plan
        self._live()
        _require(type(provider) is h.ClosedVmwareProvider)
        _require_plan('LOCAL_INSTALLER_DEVELOPMENT', plan)
        _require(plan.runtime_offline_only is True and len(plan.profiles) == 1)
        _require(getattr(plan, 'runtime_baseline_only', False) == self._request.get('runtime_baseline_only', False),
            'RUNTIME_TARGET_REQUEST_CHANGED')
        profile = plan.profiles[0]
        _require(profile.profile == PROFILE and profile.snapshot_name == h.SNAPSHOT_ALLOWLIST[PROFILE])
        provider._require_active_execution_authority()
        source = require_development_source(provider, plan)
        inputs = getattr(provider, '_runtime_development_material', None)
        _require(type(inputs) is RuntimeTrustInputs)
        inputs.verify_current()
        for name, value in (('execution_source_sha', plan.execution_source_sha),
                ('execution_source_tree', plan.execution_source_tree),
                ('material_source_sha', plan.source_sha), ('material_source_tree', plan.source_tree),
                ('verified_candidate_digest', plan.verified_candidate_digest),
                ('qualification_run_id', plan.qualification_run_id),
                ('runtime_execution_inventory_digest', source.inventory_digest),
                ('runtime_trust_selection_digest', inputs.digest),
                ('runtime_guest_inventory_digest', inputs.guest_inventory_digest),
                ('runtime_trust_expiry', inputs.selection['expires_at']),
                ('runtime_authorization_deadline', plan.runtime_authorization_deadline)):
            _require(value == self._request[name], 'RUNTIME_TARGET_REQUEST_CHANGED')
        _require(plan.runtime_trust_selection_digest == inputs.digest)
        _require(re.fullmatch('[0-9a-f]{32}', plan.session_id) is not None)
        authority = provider._active_profile_authority(profile, plan)
        root, work = PureWindowsPath(str(provider._execution.root)), PureWindowsPath(str(provider._execution.work_root))
        _require(root.is_absolute() and root.drive.upper() == 'E:' and root.parent == PureWindowsPath('E:/')
            and re.fullmatch('animemo-provider-execution-[0-9a-f]{32}', root.name) is not None
            and work.parent == root and re.fullmatch('private-work-[0-9a-f]{32}', work.name) is not None)
        clone = work / plan.session_id / PROFILE.lower() / 'vm' / (h.SOURCE_VM_IDENTITY + '.vmx')
        _require(PureWindowsPath(str(authority.clone_vmx)) == clone, 'RUNTIME_TARGET_CLONE_PATH_CHANGED')
        body = {'schema': 'animemo.runtime-target-binding/v1', 'entry_id': self._request['entry_id'],
            'authorization_id': self._request['authorization_id'], 'plan_digest': plan.plan_digest,
            'execution_source_sha': plan.execution_source_sha, 'execution_source_tree': plan.execution_source_tree,
            'execution_inventory_digest': source.inventory_digest,
            'material_source_sha': plan.source_sha, 'material_source_tree': plan.source_tree,
            'verified_candidate_digest': plan.verified_candidate_digest,
            'qualification_run_id': plan.qualification_run_id,
            'source_vmx': self._request['source_vmx'], 'source_vm_inventory_digest':
                h.sha256_bytes(h.canonical_json_bytes(dict(sorted(plan.original_vm_hashes.items())))),
            'profile': profile.profile, 'snapshot_name': profile.snapshot_name,
            'snapshot_identity': profile.snapshot_identity, 'clone_identity': profile.clone_identity,
            'session_id': plan.session_id, 'clone_vmx': str(clone),
            'selection_digest': inputs.digest, 'guest_inventory_digest': inputs.guest_inventory_digest,
            'material_expiry': inputs.selection['expires_at'],
            'authorization_deadline': plan.runtime_authorization_deadline,
            'effective_expiry_utc_seconds': self._expires,
            'round_limit': 1, 'capture_limit': 1, 'retention_policy': 'STOP_AND_RETAIN',
            'publish_authorized': False, 'candidate_acceptance_authority_granted': False}
        if getattr(plan, 'runtime_baseline_only', False):
            body.update(runtime_baseline_only=True, capture_limit=0,
                        credential_capture_authorized=False, installer_authorized=False)
        return body, source, inputs

    def bind(self, provider, plan):
        _require(self._binding is None, 'RUNTIME_TARGET_ALREADY_BOUND')
        body, source, inputs = self._observe(provider, plan)
        _write(self._root / 'target-binding.json', body)
        self._provider, self._execution, self._plan = provider, provider._execution, plan
        self._source, self._inputs, self._binding = source, inputs, body

    def require_binding(self, plan):
        _require(self._binding is not None and plan is self._plan
            and self._provider._execution is self._execution, 'RUNTIME_TARGET_CONTEXT_CHANGED')
        body, source, inputs = self._observe(self._provider, plan)
        _require(source is self._source and inputs is self._inputs and body == self._binding,
            'RUNTIME_TARGET_CONTEXT_CHANGED')
        return json.loads(json.dumps(body))

    def confirmation(self, plan):
        _require(not self._B_attempted, 'RUNTIME_TARGET_CONFIRMATION_ALREADY_CONSUMED')
        body = self.require_binding(plan)
        self._B_attempted = True
        return body, min(60, self._expires - time.time(), self._deadline - time.monotonic())

    def accept_confirmation(self, plan, approved_body):
        _require(self._B_attempted and not self._B_confirmed, 'RUNTIME_TARGET_CONFIRMATION_ALREADY_CONSUMED')
        _require(self.require_binding(plan) == approved_body, 'RUNTIME_TARGET_CONTEXT_CHANGED')
        self._B_confirmed = True

    def require_execution_binding(self, provider, plan):
        _require(self._B_confirmed and provider is self._provider)
        self.require_binding(plan)

    def require_active_target(self, provider, plan):
        """Lock-safe identity check: no subprocess, file hash, IO or new locks."""
        self._live()
        _require(self._B_confirmed and provider is self._provider and plan is self._plan
            and provider._execution is self._execution, 'RUNTIME_TARGET_CONTEXT_CHANGED')
        _require(provider._development_source_authority is self._source
            and not self._source._closed and self._source._execution is self._execution
            and provider._runtime_development_material is self._inputs and not self._inputs._closed,
            'RUNTIME_TARGET_CONTEXT_CHANGED')
        clone = PureWindowsPath(str(self._execution.work_root)) / plan.session_id / PROFILE.lower() / 'vm' / (h.SOURCE_VM_IDENTITY + '.vmx')
        _require(str(clone) == self._binding['clone_vmx']
            and h.sha256_bytes(h.canonical_json_bytes(plan.identity_body())) == self._binding['plan_digest']
            and self._source.inventory_digest == self._binding['execution_inventory_digest']
            and self._inputs.digest == self._binding['selection_digest']
            and self._inputs.selection['expires_at'] == self._binding['material_expiry'],
            'RUNTIME_TARGET_CONTEXT_CHANGED')

    def require_frozen_target(self):
        self.require_active_target(self._provider, self._plan)

    def require_consent_open(self):
        """Read-only owner finalization may follow Provider/inputs release."""
        self._live()
        _require(self._B_confirmed)

    @property
    def expiry_limits(self):
        self.require_frozen_target()
        return self._expires, self._deadline

    @property
    def record(self):
        return {'entry_id': self._request['entry_id'], 'A_approved': True, 'B_approved': self._B_confirmed,
            'B_confirmation_attempted': self._B_attempted, 'entry_spent': True,
            'closed': self._closed, 'holds_released': self._holds_released,
            'target': None if self._binding is None else json.loads(json.dumps(self._binding))}

    def close(self, result):
        if not self._closed:
            self._closed = True
            try:
                _write(self._root / 'completion.json', {'status': result['status'],
                    'phase': 'BEFORE_HOLD_RELEASE', 'final_result': False,
                    'failure_code': result.get('failure_code'), 'handoff': self.record})
            finally:
                self._holds.close()
                self._holds_released = True

    def __reduce__(self):
        raise TypeError('Live Runtime target consent cannot be serialized')
