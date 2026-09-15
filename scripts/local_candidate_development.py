"""Run three clean local development Profiles before pushing or qualifying code."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
import re
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
    if isinstance(error, (h.CandidateHarnessError, ControllerFailure, DevelopmentScopeError, ConsoleCaptureError, DevelopmentOwnerError)):
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
        except BaseException:
            failures.append('DEVELOPMENT_OWNER_FINAL_RECORD_FAILED')
    if failures:
        result.update(status='ERROR', all_profiles_pass=False)
        result.setdefault('cleanup_errors', []).extend(dict.fromkeys(failures))
        if 'failure_code' in result:
            result.setdefault('additional_failure_code', failures[0])
        else:
            result['failure_code'] = failures[0]


def run(args, *, session_owner=None, confirmed_owner_sink=None):
    from scripts.guest_batch_scope import DEVELOPMENT_AUTHORIZATION, confirm_local_batch, validate_authorization_id
    from release.candidate_failure_policy import FAILURE_POLICY
    from scripts.development_session_owner import acquire_confirmed_development_owner, _material
    owns_session = False
    if confirmed_owner_sink is not None and (type(confirmed_owner_sink) is not list or confirmed_owner_sink):
        raise ControllerFailure('DEVELOPMENT_OWNER_RECEIVER_INVALID')
    result = {'schema': SCHEMA, 'purpose': 'LOCAL_INSTALLER_DEVELOPMENT', 'status': 'ERROR',
        'failure_policy': FAILURE_POLICY,
        'started_at': datetime.now(timezone.utc).isoformat(), 'all_profiles_pass': False,
        'candidate_acceptance_authority_granted': False, 'publish_authorized': False,
        'profile_reports': {}, 'profile_results': {}, 'credential_session': None,
        'profile_operations': {}, 'workload_diagnostics': {}, 'host_lifecycle': [],
        'cleanup_errors': [], 'source_preserved': False}
    provider = None
    try:
        if session_owner is not None:
            from scripts.development_session_owner import DevelopmentSessionOwner
            if type(session_owner) is not DevelopmentSessionOwner or session_owner.closed or not args.execute:
                raise ControllerFailure('DEVELOPMENT_SESSION_OWNER_INVALID')
        confirming = getattr(args, 'confirm_batch', False)
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
        if args.execute:
            WindowsConsoleCapture().preflight()
        provider = h.ClosedVmwareProvider()
        with provider.execution_authority(_retain_controller_data=args.execute):
            result['provider_root'] = str(provider._execution.root)
            result['retained_work_root'] = str(provider._execution.work_root)
            with h.acquire_candidate_material_authority(args.verified_candidate_digest, provider=provider) as material:
                result['private_material_root'] = str(material.loaded.root.parent)
                with provider.bind_candidate_material_authority(material):
                    with acquire_development_source(provider, source_sha=args.execution_source_sha,
                            source_tree=args.execution_source_tree) as source:
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
                            platform_diagnostic=getattr(args, 'platform_diagnostic', False))
                        result['plan'] = plan.as_dict()
                        result['development_mode'] = plan.identity_body()['developmentMode']
                        result['profile_results'] = {profile.profile: {'status': 'NOT_RUN', 'failure_code': None}
                            for profile in plan.profiles}
                        from scripts.development_guest_session import preflight_development_workload_commands
                        result['workload_command_preflight'] = preflight_development_workload_commands(provider, plan)
                        if not args.execute:
                            result['status'] = 'PLAN_ONLY'
                        else:
                            if (confirming or args.authorization_id == DEVELOPMENT_AUTHORIZATION) and session_owner is None:
                                _require_confirmation = getattr(args, 'confirm_batch', False)
                                if not _require_confirmation:
                                    raise ControllerFailure('DEVELOPMENT_LOCAL_CONFIRMATION_REQUIRED')
                                authorization = confirm_local_batch(authorization_id=args.authorization_id,
                                    purpose='LOCAL_INSTALLER_DEVELOPMENT', plan=plan, round_limit=1)
                                result['batch_confirmation'] = authorization.body
                                try:
                                    session_owner = acquire_confirmed_development_owner(
                                        authorization=authorization, material_identity=_material(plan))
                                except BaseException:
                                    authorization.close()
                                    raise
                                if confirmed_owner_sink is not None:
                                    confirmed_owner_sink.append(session_owner)
                                else:
                                    owns_session = True
                            batch = CandidateBatch(provider, plan, authorization_id=args.authorization_id,
                                development_owner=session_owner)
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
        if 'failure_code' in result:
            result['additional_failure_code'] = _failure(error)
        result.setdefault('failure_code', _failure(error))
        result['status'] = 'ERROR'
    finally:
        if provider is not None:
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
    if owns_session:
        _close_owned_session(result, session_owner)
    return result


def main(argv=None):
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
    parser.add_argument('--platform-diagnostic', action='store_true',
        help='Run only fixed Fresh platform diagnostics; no application installation or acceptance authority')
    parser.add_argument('--result', type=Path)
    args = parser.parse_args(argv)
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
        print(raw.decode('utf-8'), end='')
        return 0 if result['status'] in {'PASS', 'PLAN_ONLY'} else 2
    except (OSError, h.CandidateHarnessError):
        import sys
        print(json.dumps({'status':'ERROR','failure_code':'DEVELOPMENT_RESULT_EXPORT_FAILED'}),file=sys.stderr)
        return 2
    finally:
        if output is not None:
            output.close()


if __name__ == '__main__':
    raise SystemExit(main())
