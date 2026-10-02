"""A fresh, single-use baseline diagnosis; never restore the consumed V2."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from release.materials import reject_duplicate_json_keys
from scripts.guest_sudo_session import ControllerFailure

# Private operator bindings are intentionally absent from the public checkout.
# Synthetic tests supply in-memory fixtures; production needs the reviewed private overlay.
ENTRY_ID = None
ENTRY_ROOT: Path | None = None
PERMIT_ID = None
PREVIOUS_REPORT: Path | None = None
PREVIOUS_REPORT_SHA256 = None
PREVIOUS_REPORT_BYTES = None


def request_diagnostic(args):
    selected = getattr(args, 'runtime_baseline_only', False)
    if type(selected) is not bool:
        raise ControllerFailure('RUNTIME_BASELINE_ONLY_SELECTOR_INVALID')
    if not selected:
        return None
    if any(value is None for value in (ENTRY_ID, ENTRY_ROOT, PERMIT_ID, PREVIOUS_REPORT,
                                       PREVIOUS_REPORT_SHA256, PREVIOUS_REPORT_BYTES)):
        raise ControllerFailure('RUNTIME_BASELINE_ONLY_OPERATOR_BINDING_REQUIRED')
    if (args.authorization_id != PERMIT_ID
            or getattr(args, 'runtime_ui_retry', False) is not False
            or not all(getattr(args, name, False) is True for name in
                       ('execute', 'confirm_batch', 'runtime_offline_only', 'runtime_target_handoff', 'result_only'))):
        raise ControllerFailure('RUNTIME_BASELINE_ONLY_FIXED_SCOPE_REQUIRED')
    try:
        with PREVIOUS_REPORT.open('rb') as stream:
            raw = stream.read(PREVIOUS_REPORT_BYTES + 1)
    except OSError:
        raise ControllerFailure('RUNTIME_BASELINE_ONLY_PREVIOUS_RECEIPT_UNAVAILABLE') from None
    if len(raw) != PREVIOUS_REPORT_BYTES or hashlib.sha256(raw).hexdigest() != PREVIOUS_REPORT_SHA256:
        raise ControllerFailure('RUNTIME_BASELINE_ONLY_PREVIOUS_RECEIPT_CHANGED')
    report = json.loads(raw, object_pairs_hook=reject_duplicate_json_keys)
    prior = report.get('runtime_target_handoff', {})
    if (report.get('failure_code') != 'RUNTIME_BASELINE_HARD_PREREQUISITE_MISSING'
            or not all(prior.get(name) is True for name in
                       ('A_approved', 'B_approved', 'entry_spent', 'closed', 'holds_released'))
            or report.get('credential_session', {}).get('session_capture_attempts') != 0):
        raise ControllerFailure('RUNTIME_BASELINE_ONLY_PREVIOUS_RECEIPT_SCOPE_INVALID')
    return {'entry_id': ENTRY_ID, 'entry_root': str(ENTRY_ROOT), 'permit_id': PERMIT_ID,
            'previous_report_sha256': PREVIOUS_REPORT_SHA256, 'previous_entry_preserved': True,
            'requires_separate_user_approval': True, 'deadline_is_declared_scope_not_stored_approval': True,
            'requires_new_source_material_binding': True, 'requires_fresh_native_A_and_B': True,
            'round_limit': 1, 'capture_limit': 0, 'installer_authorized': False,
            'success_and_failure_stop_before_capture': True, 'retention_policy': 'STOP_AND_RETAIN',
            'entry_spent_after_cancel_or_failure': True, 'automatic_retry': False, 'publish_authorized': False}
