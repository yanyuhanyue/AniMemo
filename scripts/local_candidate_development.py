"""Run local DEV profiles; single Runtime uses separate native A/B consent."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
import re
from contextlib import ExitStack
from pathlib import Path

from scripts import candidate_vm_harness as h
from scripts.candidate_batch_session import CandidateBatch
from scripts.development_capture_scope import AUTHORIZATION, DevelopmentScopeError
from scripts.development_plan import from_material_plan
from scripts.development_source import acquire_development_source, require_material_compatibility
from scripts.development_session_owner import DevelopmentOwnerError
from scripts.guest_console_capture import ConsoleCaptureError, WindowsConsoleCapture
from scripts.guest_sudo_session import ControllerFailure
from scripts.isolated_guest_validation import _check_checkout
from release.materials import reject_duplicate_json_keys

SCHEMA = 'animemo.local-installer-development-batch-report/v1'


def result_bytes(result):
    """The next JSON consumer must recover the complete development result."""
    try:
        raw = h.canonical_json_bytes(result)
        decoded = json.loads(raw, object_pairs_hook=reject_duplicate_json_keys)
        if decoded != result or h.canonical_json_bytes(decoded) != raw:
            raise ValueError('result changed on decoding')
        return raw
    except (TypeError, ValueError) as error:
        raise h.CandidateHarnessError('DEVELOPMENT_RESULT_EXPORT_INVALID') from error


def _failure(error):
    from scripts.runtime_development_boundary import RuntimeBoundaryError
    if isinstance(error, (h.CandidateHarnessError, ControllerFailure, DevelopmentScopeError, ConsoleCaptureError, DevelopmentOwnerError, RuntimeBoundaryError)):
        return getattr(error, 'code', str(error))
    return 'DEVELOPMENT_EXECUTION_INTERRUPTED_OR_UNCLASSIFIED'


def _close_owned_session(result, session_owner):
    """Keep primary evidence even if the owner's final validation fails."""
    failures = []
    try:
        session_owner.finish_round(result)
    except BaseException as error:
        failures.append(_failure(error))
    finally:
        try:
            session_owner.dispose()
        except BaseException as error:
            failures.append(_failure(error))
        try:
            result['memory_owner'] = session_owner.record
            if result['memory_owner'].get('state') != 'CLOSED':
                failures.append('DEVELOPMENT_OWNER_NOT_CLOSED')
            if result.get('plan', {}).get('runtimeOfflineOnly') is True and result.get('status') == 'PASS':
                verified = result['memory_owner'].get('runtime_round_verified', {})
                if (verified.get('plan_digest') != result['plan']['planDigest']
                        or verified.get('session_id') != result['plan']['sessionId']
                        or result['memory_owner'].get('close_reason') != 'DEVELOPMENT_PREACCEPTANCE_PASSED'):
                    failures.append('DEVELOPMENT_RUNTIME_OWNER_VALIDATION_MISSING')
        except BaseException:
            failures.append('DEVELOPMENT_OWNER_FINAL_RECORD_FAILED')
    if failures:
        result.update(status='ERROR', all_profiles_pass=False)
        result.setdefault('cleanup_errors', []).extend(dict.fromkeys(failures))
        if 'failure_code' in result:
            result.setdefault('additional_failure_code', failures[0])
        else:
            result['failure_code'] = failures[0]


def _finish_handoff(result, handoff):
    try:
        handoff.close(result)
    except BaseException:
        result.update(status='ERROR', all_profiles_pass=False)
        result['cleanup_errors'].append('RUNTIME_TARGET_HANDOFF_CLEANUP_FAILED')
        result.setdefault('failure_code', 'RUNTIME_TARGET_HANDOFF_CLEANUP_FAILED')
    finally:
        result['runtime_target_handoff'] = handoff.record


def _close_baseline_authorization(result, authorization):
    try:
        authorization.close()
        result['baseline_authorization_closed'] = True
    except BaseException:
        result['baseline_authorization_closed'] = False
        result.update(status='ERROR', all_profiles_pass=False)
        result['cleanup_errors'].append('RUNTIME_BASELINE_ONLY_AUTHORIZATION_CLEANUP_FAILED')


def _finish_baseline_diagnostic(result):
    result.update(purpose='RUNTIME_BASELINE_DIAGNOSTIC_ONLY', all_profiles_pass=False)
    complete = result.get('failure_code') == 'RUNTIME_BASELINE_DIAGNOSTIC_COMPLETE'
    if not complete:
        return
    operation = result.get('profile_operations', {}).get('RUNTIME_BASE_OFFLINE', {})
    handoff, credentials = result.get('runtime_target_handoff', {}), result.get('credential_session', {})
    roles = credentials.get('profiles', {}).get('RUNTIME_BASE_OFFLINE', {})
    diagnostic = operation.get('runtime_baseline_diagnostic', {})
    if (result.get('cleanup_errors') or result.get('source_preserved') is not True
            or result.get('candidate_acceptance_authority_granted') is not False
            or result.get('publish_authorized') is not False
            or result.get('profile_reports') != {} or result.get('memory_owner') is not None
            or result.get('batch_confirmation', {}).get('capture_limit') != 0
            or result.get('plan', {}).get('runtimeBaselineOnly') is not True
            or result.get('baseline_authorization_closed') is not True
            or not all(result.get(name) is True for name in ('private_material_root_released',
                'private_execution_source_root_released', 'runtime_trust_inputs_released'))
            or not all(handoff.get(name) is True for name in
                       ('A_approved', 'B_approved', 'entry_spent', 'closed', 'holds_released'))
            or operation.get('power_state') != 'STOPPED' or operation.get('clone_disposition') != 'QUARANTINED'
            or operation.get('cleanup_errors') or operation.get('continuation_authorized') is not False
            or not all(operation.get(name) is True for name in
                       ('lease_released', 'session_keys_removed', 'known_hosts_removed'))
            or operation.get('runtime_lifetime', {}).get('closed') is not True
            or diagnostic.get('baseline_validated') is not True
            or diagnostic.get('credential_capture_authorized') is not False
            or diagnostic.get('installer_authorized') is not False
            or type(diagnostic.get('baseline_digest')) is not str
            or not h._DIGEST.fullmatch(diagnostic['baseline_digest'])
            or credentials.get('session_capture_attempts') != 0 or credentials.get('session_capture_completed') != 0
            or credentials.get('development_capture_index') != 1
            or not roles or any(item.get('delivery_attempts') != 0 or item.get('delivery_completed') != 0
                                for item in roles.values())):
        result.update(status='ERROR', failure_code='RUNTIME_BASELINE_ONLY_COMPLETION_UNVERIFIED')
        result['cleanup_errors'].append('RUNTIME_BASELINE_ONLY_COMPLETION_UNVERIFIED')
        return
    result.pop('failure_code')
    result.update(status='DIAGNOSTIC_COMPLETE', completion_code='RUNTIME_BASELINE_DIAGNOSTIC_COMPLETE',
                  baseline_prerequisites_pass=True)
    result['profile_results']['RUNTIME_BASE_OFFLINE'] = {'status': 'DIAGNOSTIC_COMPLETE', 'failure_code': None}


def run(args, *, session_owner=None, confirmed_owner_sink=None):
    from release.candidate_failure_policy import FAILURE_POLICY
    result = {'schema': SCHEMA, 'purpose': 'LOCAL_INSTALLER_DEVELOPMENT', 'status': 'ERROR',
        'failure_policy': FAILURE_POLICY,
        'started_at': datetime.now(timezone.utc).isoformat(), 'all_profiles_pass': False,
        'candidate_acceptance_authority_granted': False, 'publish_authorized': False,
        'profile_reports': {}, 'profile_results': {}, 'credential_session': None,
        'profile_operations': {}, 'workload_diagnostics': {}, 'host_lifecycle': [],
        'cleanup_errors': [], 'source_preserved': False}
    # Owner is registered later and therefore closes before the handoff. The
    # outer stack runs even if a cleanup diagnostic or result export raises.
    with ExitStack() as cleanup:
        try:
            _run(args, result, cleanup, session_owner=session_owner,
                confirmed_owner_sink=confirmed_owner_sink)
        except BaseException as error:
            result.update(status='ERROR', all_profiles_pass=False)
            result.setdefault('failure_code', _failure(error))
            result['cleanup_errors'].append('DEVELOPMENT_CLEANUP_DIAGNOSTIC_FAILED')
    if getattr(args, 'runtime_baseline_only', False) is True:
        _finish_baseline_diagnostic(result)
    return result


def _run(args, result, cleanup, *, session_owner, confirmed_owner_sink):
    from scripts.guest_batch_scope import DEVELOPMENT_AUTHORIZATION, confirm_local_batch, validate_authorization_id
    from scripts.development_session_owner import acquire_confirmed_development_owner, _material
    if confirmed_owner_sink is not None and (type(confirmed_owner_sink) is not list or confirmed_owner_sink):
        raise ControllerFailure('DEVELOPMENT_OWNER_RECEIVER_INVALID')
    provider = None
    handoff = None
    try:
        if session_owner is not None:
            from scripts.development_session_owner import DevelopmentSessionOwner
            if type(session_owner) is not DevelopmentSessionOwner or session_owner.closed or not args.execute:
                raise ControllerFailure('DEVELOPMENT_SESSION_OWNER_INVALID')
        confirming = getattr(args, 'confirm_batch', False)
        published = getattr(args, 'published_platform_plan', False)
        runtime_only = getattr(args, 'runtime_offline_only', False)
        baseline_only = getattr(args, 'runtime_baseline_only', False)
        target_handoff = getattr(args, 'runtime_target_handoff', False)
        if (type(runtime_only) is not bool
                or type(baseline_only) is not bool or baseline_only and (
                    not runtime_only or getattr(args, 'runtime_ui_retry', False) is not False)
                or type(target_handoff) is not bool or target_handoff and not runtime_only
                or runtime_only and (published or getattr(args, 'platform_diagnostic', False))):
            raise ControllerFailure('DEVELOPMENT_PLAN_BINDING_INVALID')
        if runtime_only and (session_owner is not None or confirmed_owner_sink is not None):
            raise ControllerFailure('RUNTIME_TARGET_EXTERNAL_OWNER_FORBIDDEN')
        if runtime_only and args.execute and not target_handoff:
            raise ControllerFailure('RUNTIME_TARGET_HANDOFF_REQUIRED')
        if runtime_only and args.execute and not confirming and session_owner is None:
            raise ControllerFailure('DEVELOPMENT_LOCAL_CONFIRMATION_REQUIRED')
        if runtime_only:
            from scripts.runtime_development_boundary import (
                parse_authorization_deadline,
            )
            parse_authorization_deadline(getattr(args, 'runtime_authorization_deadline', None))
            if (getattr(args, 'runtime_trust_inputs', None) is None
                    or getattr(args, 'runtime_portable', None) is None
                    or getattr(args, 'runtime_release_attestation', None) is None
                    or type(getattr(args, 'runtime_trust_selection_digest', None)) is not str
                    or not h._DIGEST.fullmatch(args.runtime_trust_selection_digest)):
                raise ControllerFailure('RUNTIME_DEVELOPMENT_MATERIAL_REQUIRED')
        if published and args.execute and confirming is not True:
            raise ControllerFailure('DEVELOPMENT_LOCAL_CONFIRMATION_REQUIRED')
        if args.authorization_id is not None and not args.execute:
            raise ControllerFailure('DEVELOPMENT_CAPTURE_AUTHORIZATION_INVALID')
        if args.execute and (not args.authorization_id or args.result is None):
            raise ControllerFailure('DEVELOPMENT_EXECUTION_AUTHORIZATION_REQUIRED')
        if args.execute:
            validate_authorization_id(args.authorization_id, 'LOCAL_INSTALLER_DEVELOPMENT')
            if not confirming and session_owner is None and args.authorization_id not in {AUTHORIZATION, DEVELOPMENT_AUTHORIZATION}:
                raise ControllerFailure('DEVELOPMENT_LOCAL_CONFIRMATION_REQUIRED')
            if session_owner is not None and getattr(session_owner, '_authorization', None) is not None:
                if session_owner._authorization.authorization_id != args.authorization_id or confirming:
                    raise ControllerFailure('DEVELOPMENT_SESSION_OWNER_INVALID')
        _check_checkout(args.execution_source_sha, args.execution_source_tree)
        require_material_compatibility(args.material_source_sha, args.execution_source_sha)
        if runtime_only and not args.execute:
            from scripts.runtime_target_handoff import PREPARATION_EFFECTS
            result.update(status='SCOPE_ONLY', source_preserved=True,
                execution_source_sha=args.execution_source_sha, execution_source_tree=args.execution_source_tree,
                preparation_effects=list(PREPARATION_EFFECTS), A_approved=False, B_approved=False,
                private_inputs_verified=False, actual_session_id=None, actual_clone_vmx=None)
            return result
        planning_inputs = None
        if published:
            if getattr(args, 'platform_diagnostic', False) or session_owner is not None or confirmed_owner_sink is not None:
                raise ControllerFailure('DEVELOPMENT_PUBLISHED_PLANNING_SCOPE_INVALID')
            planning_inputs = {name: getattr(args, name, None) for name in (
                'asset_root', 'sidecar', 'sidecar_digest', 'release_id', 'windows_gh', 'linux_gh_package')}
            if any(value is None for value in planning_inputs.values()):
                raise ControllerFailure('DEVELOPMENT_PUBLISHED_PLANNING_INPUT_REQUIRED')
        if args.execute:
            WindowsConsoleCapture().preflight()
        if runtime_only:
            from scripts.runtime_target_handoff import begin_preparation
            result['runtime_target_handoff_stage'] = 'A_CONFIRMATION_PENDING'
            handoff = begin_preparation(args)
            cleanup.callback(_finish_handoff, result, handoff)
            result['runtime_target_handoff_stage'] = 'A_APPROVED_ENTRY_SPENT'
            _check_checkout(args.execution_source_sha, args.execution_source_tree)
        provider = h.ClosedVmwareProvider()
        with provider.execution_authority(_retain_controller_data=args.execute):
            result['provider_root'] = str(provider._execution.root)
            result['retained_work_root'] = str(provider._execution.work_root)
            with h.acquire_candidate_material_authority(args.verified_candidate_digest, provider=provider) as material:
                result['private_material_root'] = str(material.loaded.root.parent)
                with provider.bind_candidate_material_authority(material):
                    with acquire_development_source(provider, source_sha=args.execution_source_sha,
                            source_tree=args.execution_source_tree, published_planning_inputs=planning_inputs) as source:
                        result['private_execution_source_root'] = str(source.root.parent)
                        base_plan = h.build_harness_plan(verified_candidate_digest=args.verified_candidate_digest,
                            expected_qualification_run_id=args.qualification_run_id,
                            expected_source_sha=args.material_source_sha,
                            expected_source_tree=args.material_source_tree,
                            provider=provider, _candidate_material_authority=material)
                        if re.fullmatch(r'v2\.\d+\.\d+-rc\.[1-9][0-9]*', base_plan.candidate_version) is None:
                            raise h.CandidateHarnessError('DEVELOPMENT_CANDIDATE_TARGET_INVALID')
                        plan = from_material_plan(base_plan, execution_source_sha=args.execution_source_sha,
                            execution_source_tree=args.execution_source_tree,
                            execution_inventory_digest=source.inventory_digest,
                            platform_diagnostic=published or getattr(args, 'platform_diagnostic', False),
                            published_subject_digest=source.published_subject_digest,
                            runtime_offline_only=runtime_only,
                            runtime_baseline_only=baseline_only,
                            runtime_authorization_deadline=getattr(args, 'runtime_authorization_deadline', None),
                            runtime_trust_selection_digest=getattr(args, 'runtime_trust_selection_digest', None),
                            runtime_retention_policy='STOP_AND_RETAIN' if runtime_only else None)
                        if runtime_only:
                            from installer.development_trust import (
                                read_runtime_trust_selection,
                            )
                            from scripts.development_guest_session import (
                                development_binding,
                            )
                            provider._runtime_development_material = read_runtime_trust_selection(
                                args.runtime_trust_inputs, expected_digest=args.runtime_trust_selection_digest,
                                binding=development_binding(plan), payload=args.runtime_portable,
                                release_attestation=args.runtime_release_attestation)
                        result['plan'] = plan.as_dict()
                        if handoff is not None:
                            handoff.bind(provider, plan)
                            result['runtime_target_handoff_stage'] = 'TARGET_BOUND_B_NOT_APPROVED'
                        result['development_mode'] = plan.identity_body()['developmentMode']
                        result['profile_results'] = {profile.profile: {'status': 'NOT_RUN', 'failure_code': None}
                            for profile in plan.profiles}
                        from scripts.development_guest_session import preflight_development_workload_commands
                        result['workload_command_preflight'] = (
                            {'status': 'BASELINE_ONLY_NO_PRIVILEGED_WORKLOAD', 'installer_workload_constructed': False}
                            if baseline_only else preflight_development_workload_commands(provider, plan))
                        if not args.execute:
                            result['status'] = 'PLAN_ONLY'
                        else:
                            if (confirming or args.authorization_id == DEVELOPMENT_AUTHORIZATION) and session_owner is None:
                                _require_confirmation = getattr(args, 'confirm_batch', False)
                                if not _require_confirmation:
                                    raise ControllerFailure('DEVELOPMENT_LOCAL_CONFIRMATION_REQUIRED')
                                authorization = confirm_local_batch(authorization_id=args.authorization_id,
                                    purpose='LOCAL_INSTALLER_DEVELOPMENT', plan=plan, round_limit=1,
                                    **({'runtime_handoff': handoff} if handoff is not None else {}))
                                result['batch_confirmation'] = authorization.body
                                try:
                                    if handoff is not None:
                                        handoff.require_execution_binding(provider, plan)
                                        _check_checkout(args.execution_source_sha, args.execution_source_tree)
                                        result['runtime_target_handoff_stage'] = 'B_APPROVED_EXISTING_BATCH_SCOPE'
                                    if baseline_only:
                                        cleanup.callback(_close_baseline_authorization, result, authorization)
                                    else:
                                        session_owner = acquire_confirmed_development_owner(
                                            authorization=authorization, material_identity=_material(plan))
                                except BaseException:
                                    authorization.close()
                                    raise
                                if baseline_only:
                                    pass
                                elif confirmed_owner_sink is not None:
                                    confirmed_owner_sink.append(session_owner)
                                else:
                                    cleanup.callback(_close_owned_session, result, session_owner)
                            batch = CandidateBatch(provider, plan, authorization_id=args.authorization_id,
                                development_owner=session_owner,
                                **({'local_authorization': authorization} if baseline_only else {}))
                            provider._candidate_batch = batch
                            try:
                                for index, profile in enumerate(plan.profiles):
                                    try:
                                        report = provider.execute_development_profile(plan=profile,
                                            harness_plan=plan, candidate_root=material.loaded.root,
                                            initial_platform_state=h._initial_platform_state(profile.profile))
                                        result['profile_reports'][profile.profile] = report
                                        if report['result'] != 'PASS':
                                            raise h.CandidateHarnessError('DEVELOPMENT_PROFILE_FAILED')
                                        result['profile_results'][profile.profile] = {'status': 'PASS', 'failure_code': None}
                                        if provider.inspect_original_hashes() != dict(plan.original_vm_hashes):
                                            raise h.CandidateHarnessError('CANDIDATE_ORIGINAL_VM_MUTATED')
                                    except BaseException as error:
                                        result.setdefault('failure_code', _failure(error))
                                        result['profile_results'][profile.profile] = {'status': 'ERROR', 'failure_code': _failure(error)}
                                        if type(error) is h.CandidateProfileExecutionError:
                                            try:
                                                h.validate_business_continuation(provider, plan, profile, error)
                                            except h.CandidateHarnessError as continuation_error:
                                                result.setdefault('additional_failure_code', _failure(continuation_error))
                                            else:
                                                continue
                                        for remaining in plan.profiles[index + 1:]:
                                            result['profile_results'][remaining.profile] = {
                                                'status': 'NOT_RUN_SHARED_BLOCKER', 'failure_code': _failure(error)}
                                        batch.revoke('CANDIDATE_BATCH_PROFILE_FAILURE')
                                        break
                            finally:
                                try:
                                    batch.close()
                                finally:
                                    result['credential_session'] = batch.record
                                    provider._candidate_batch = None
                            result['all_profiles_pass'] = all(v['status'] == 'PASS' for v in result['profile_results'].values())
                            result['status'] = 'PASS' if result['all_profiles_pass'] else 'FAIL'
        _check_checkout(args.execution_source_sha, args.execution_source_tree)
        result['source_preserved'] = True
    except BaseException as error:
        from scripts.candidate_guest_session import WorkloadCommandBudgetFailure
        if isinstance(error, WorkloadCommandBudgetFailure):
            result['workload_command_budget_failure'] = error.command_budget
        if 'failure_code' in result:
            result['additional_failure_code'] = _failure(error)
        result.setdefault('failure_code', _failure(error))
        result['status'] = 'ERROR'
    finally:
        if provider is not None:
            runtime_inputs = getattr(provider, '_runtime_development_material', None)
            if runtime_inputs is not None:
                try:
                    runtime_inputs.close()
                    result['runtime_trust_inputs_released'] = True
                except BaseException:  # noqa: BLE001 - cleanup failure must not replace primary evidence
                    result['runtime_trust_inputs_released'] = False
                    result['status'] = 'ERROR'
                    result['all_profiles_pass'] = False
                    result['cleanup_errors'].append('RUNTIME_TRUST_INPUT_CLEANUP_FAILED')
            result['profile_operations'] = dict(provider._profile_operation_results)
            result['workload_diagnostics'] = dict(provider._candidate_diagnostics)
            result['host_lifecycle'] = list(getattr(provider, '_host_lifecycle_observations', ()))
        result['completed_at'] = datetime.now(timezone.utc).isoformat()
        resources = result['profile_operations'].values()
        if any(item.get('cleanup_errors') for item in resources):
            result['status'] = 'ERROR'
            result['all_profiles_pass'] = False
            result['cleanup_errors'].append('DEVELOPMENT_PROFILE_CLEANUP_FAILED')
        for key in ('private_material_root', 'private_execution_source_root'):
            if key in result:
                result[key + '_released'] = not Path(result[key]).exists()
                if not result[key + '_released']:
                    result['status'] = 'ERROR'
                    result['all_profiles_pass'] = False
                    result['cleanup_errors'].append('DEVELOPMENT_PRIVATE_MATERIAL_CLEANUP_FAILED')
        if result['status'] == 'PASS':
            session = result['credential_session']
            roles = [role for profile in session['profiles'].values() for role in profile.values()]
            capture_valid = (session['session_capture_attempts'] == session['session_capture_completed'] == 1
                if session_owner is None else session.get('development_owner_id') == session_owner.record['owner_id']
                and session.get('owner_capture_completed') == 1
                and session['session_capture_attempts'] == session['session_capture_completed']
                and session['session_capture_attempts'] in (0, 1))
            if (not capture_valid
                    or session['secret_state'] != ('CLOSED' if session_owner is None else 'ROUND_CAPABILITY_CLOSED')
                    or len(roles) != len(result['plan']['profiles']) * 3
                    or any(role['delivery_attempts'] != 1 or role['delivery_completed'] != 1
                           or role['operation_result'] != 'PASS' for role in roles)):
                result['status'] = 'ERROR'
                result['all_profiles_pass'] = False
                result['failure_code'] = 'DEVELOPMENT_CAPTURE_ACCOUNTING_INVALID'
    if result['status'] == 'PASS':
        try:
            result_bytes(result)
        except h.CandidateHarnessError as error:
            result.update(status='ERROR', all_profiles_pass=False, failure_code=error.code)
    return result


def main(argv=None):
    import sys
    argv = sys.argv[1:] if argv is None else argv
    if argv == ['--runtime-target-handoff-scope']:
        from scripts.runtime_target_handoff import scope_report
        try:
            print(result_bytes(scope_report()).decode('utf-8'), end='')
            return 0
        except Exception:
            print(json.dumps({'status': 'ERROR', 'failure_code': 'RUNTIME_TARGET_SCOPE_EXPORT_FAILED'}), file=sys.stderr)
            return 2
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--verified-candidate-digest', required=True)
    parser.add_argument('--qualification-run-id', required=True, type=int)
    parser.add_argument('--material-source-sha', required=True)
    parser.add_argument('--material-source-tree', required=True)
    parser.add_argument('--execution-source-sha', required=True)
    parser.add_argument('--execution-source-tree', required=True)
    parser.add_argument('--authorization-id')
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--confirm-batch', action='store_true')
    parser.add_argument('--runtime-offline-only', action='store_true',
        help='Select only Runtime Offline for a separately confirmed single-round DEV batch')
    parser.add_argument('--runtime-target-handoff', action='store_true',
        help='Required for Runtime --execute: native A approves private preparation/key/VM byte snapshot; native B approves the exact live clone')
    parser.add_argument('--runtime-ui-retry', action='store_true',
        help='Separately approved fixed V2 retry after the unchanged V1 B-cancelled receipt; requires --result-only')
    parser.add_argument('--runtime-baseline-only', action='store_true',
        help='New single-use A/B scope: read-only baseline, always stop before credentials/Installer and retain the clone')
    parser.add_argument('--runtime-execution-inventory-digest')
    parser.add_argument('--runtime-guest-inventory-digest')
    parser.add_argument('--runtime-trust-expiry', help='Exact rebound selection UTC expiry; checked against held inputs')
    parser.add_argument('--runtime-authorization-deadline',
        help='Source-bound single Runtime authorization deadline, UTC YYYY-MM-DDTHH:MM:SSZ')
    parser.add_argument('--runtime-trust-inputs', type=Path,
        help='Independent Linux DEV input directory; never the original Q pretrust')
    parser.add_argument('--runtime-trust-selection-digest')
    parser.add_argument('--runtime-portable', type=Path)
    parser.add_argument('--runtime-release-attestation', type=Path)
    parser.add_argument('--platform-diagnostic', action='store_true',
        help='Run only fixed Fresh platform diagnostics; no application installation or acceptance authority')
    parser.add_argument('--published-platform-plan', action='store_true',
        help='One Fresh DEV observation of original published production planning; stops before platform apply')
    for name in ('asset-root', 'sidecar', 'windows-gh', 'linux-gh-package'):
        parser.add_argument('--' + name, type=Path)
    parser.add_argument('--sidecar-digest')
    parser.add_argument('--release-id', type=int)
    parser.add_argument('--result', type=Path)
    parser.add_argument('--result-only', action='store_true',
        help='Require --result and return after verified report export, without copying JSON to the Console')
    args = parser.parse_args(argv)
    if args.result_only and args.result is None:
        parser.error('--result-only requires --result')
    if args.runtime_ui_retry and not (args.execute and args.confirm_batch
            and args.runtime_offline_only and args.runtime_target_handoff and args.result_only):
        parser.error('--runtime-ui-retry requires the complete single Runtime handoff and --result-only')
    if args.runtime_baseline_only and (args.runtime_ui_retry or not (
            args.execute and args.confirm_batch and args.runtime_offline_only
            and args.runtime_target_handoff and args.result_only)):
        parser.error('--runtime-baseline-only requires a new complete handoff, --result-only and excludes V2')
    # Reserve only the public result filename before any VM or capture work.
    output = args.result.open('xb') if args.result is not None else None
    try:
        result = run(args)
        raw = result_bytes(result)
        if output is not None:
            output.write(raw)
            output.flush()
            os.fsync(output.fileno())
            if args.result.read_bytes() != raw:
                raise h.CandidateHarnessError('DEVELOPMENT_RESULT_READBACK_INVALID')
        if not args.result_only:
            print(raw.decode('utf-8'), end='')
        return 0 if result['status'] in {'PASS', 'PLAN_ONLY', 'SCOPE_ONLY', 'DIAGNOSTIC_COMPLETE'} else 2
    except (OSError, h.CandidateHarnessError):
        import sys
        print(json.dumps({'status':'ERROR','failure_code':'DEVELOPMENT_RESULT_EXPORT_FAILED'}),file=sys.stderr)
        return 2
    finally:
        if output is not None:
            output.close()


if __name__ == '__main__':
    raise SystemExit(main())
