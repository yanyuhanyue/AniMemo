"""One proposed retry after the fixed B-cancelled receipt; no quota reset."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from scripts.guest_sudo_session import ControllerFailure

# Historical operator task IDs, deadlines and receipt identities stay in the private overlay.
PREVIOUS_ENTRY_ID = None
RETRY_ENTRY_ID = None
RETRY_ENTRY_ROOT: Path | None = None
RETRY_PERMIT_ID = None
RETRY_DEADLINE = None
PREVIOUS_REPORT: Path | None = None
PREVIOUS_REPORT_SHA256 = None
PREVIOUS_REPORT_BYTES = None


def public_policy(*, deadline=None):
    if any(value is None for value in (PREVIOUS_ENTRY_ID, RETRY_ENTRY_ID, RETRY_ENTRY_ROOT,
            RETRY_PERMIT_ID, RETRY_DEADLINE, PREVIOUS_REPORT, PREVIOUS_REPORT_SHA256, PREVIOUS_REPORT_BYTES)):
        return {'status': 'OPERATOR_BINDING_REQUIRED', 'runtime_execution_available': False,
                'requires_private_operator_overlay': True, 'publish_authorized': False}
    deadline = RETRY_DEADLINE if deadline is None else deadline
    return {'retry_entry_id': RETRY_ENTRY_ID, 'retry_entry_root': str(RETRY_ENTRY_ROOT),
        'retry_permit_id': RETRY_PERMIT_ID, 'authorization_deadline': deadline,
        'new_window_requires_explicit_user_approval': True,
        'deadline_is_declared_scope_not_stored_approval': True,
        'previous_entry_id': PREVIOUS_ENTRY_ID, 'previous_report_sha256': PREVIOUS_REPORT_SHA256,
        'previous_entry_preserved': True, 'automatic_retry': False, 'round_limit': 1,
        'capture_limit': 1, 'requires_separate_user_retry_approval': True,
        'requires_new_source_material_binding': True, 'requires_fresh_native_A_and_B': True,
        'preparation_attempt_remains_spent_after_B_cancel_or_failure': True,
        'retention_policy': 'STOP_AND_RETAIN', 'publish_authorized': False}


def request_retry(args):
    selected = getattr(args, 'runtime_ui_retry', False)
    if type(selected) is not bool:
        raise ControllerFailure('RUNTIME_UI_RETRY_SELECTOR_INVALID')
    if not selected:
        return None
    if public_policy().get('runtime_execution_available') is False:
        raise ControllerFailure('RUNTIME_UI_RETRY_OPERATOR_BINDING_REQUIRED')
    if (args.authorization_id != RETRY_PERMIT_ID
            or getattr(args, 'result_only', False) is not True):
        raise ControllerFailure('RUNTIME_UI_RETRY_FIXED_SCOPE_REQUIRED')
    # The launcher freezes a separately approved clock window. It is shown in
    # fresh native A/B and bound into the plan/live holders. A future window is
    # metadata, not a source change or an extension of any existing approval.
    from scripts.runtime_development_boundary import parse_authorization_deadline
    parse_authorization_deadline(args.runtime_authorization_deadline)
    # Only the public C receipt is checked here, before A. Never inspect the
    # previous E entry, private roots, VM, or credentials to recover permission.
    try:
        with PREVIOUS_REPORT.open('rb') as stream:
            raw = stream.read(PREVIOUS_REPORT_BYTES + 1)
    except OSError:
        raise ControllerFailure('RUNTIME_UI_RETRY_PREVIOUS_RECEIPT_UNAVAILABLE') from None
    if (len(raw) != PREVIOUS_REPORT_BYTES
            or hashlib.sha256(raw).hexdigest() != PREVIOUS_REPORT_SHA256):
        raise ControllerFailure('RUNTIME_UI_RETRY_PREVIOUS_RECEIPT_CHANGED')
    report = json.loads(raw)
    prior = report.get('runtime_target_handoff', {})
    if (report.get('failure_code') != 'BATCH_CONFIRMATION_CANCELLED'
            or report.get('runtime_target_handoff_stage') != 'TARGET_BOUND_B_NOT_APPROVED'
            or report.get('credential_session') is not None
            or report.get('profile_results') != {'RUNTIME_BASE_OFFLINE': {'failure_code': None, 'status': 'NOT_RUN'}}
            or prior.get('entry_id') != PREVIOUS_ENTRY_ID
            or prior.get('A_approved') is not True or prior.get('B_approved') is not False
            or any(prior.get(key) is not True for key in ('entry_spent', 'closed', 'holds_released'))):
        raise ControllerFailure('RUNTIME_UI_RETRY_PREVIOUS_RESULT_NOT_ELIGIBLE')
    return public_policy(deadline=args.runtime_authorization_deadline)
