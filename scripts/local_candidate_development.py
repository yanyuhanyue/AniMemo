"""Run three clean local development Profiles before pushing or qualifying code."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import os
from pathlib import Path

from scripts import candidate_vm_harness as h
from scripts.candidate_batch_session import CandidateBatch
from scripts.development_capture_scope import AUTHORIZATION, DevelopmentScopeError
from scripts.development_plan import from_material_plan
from scripts.development_source import acquire_development_source, require_material_compatibility
from scripts.guest_console_capture import ConsoleCaptureError, WindowsConsoleCapture
from scripts.guest_sudo_session import ControllerFailure
from scripts.isolated_guest_validation import _check_checkout

SCHEMA = 'animemo.local-installer-development-batch-report/v1'


def _failure(error):
    if isinstance(error, (h.CandidateHarnessError, ControllerFailure, DevelopmentScopeError, ConsoleCaptureError)):
        return getattr(error, 'code', str(error))
    return 'DEVELOPMENT_EXECUTION_INTERRUPTED_OR_UNCLASSIFIED'


def run(args):
    result = {'schema': SCHEMA, 'purpose': 'LOCAL_INSTALLER_DEVELOPMENT', 'status': 'ERROR',
        'started_at': datetime.now(timezone.utc).isoformat(), 'all_profiles_pass': False,
        'candidate_acceptance_authority_granted': False, 'publish_authorized': False,
        'profile_reports': {}, 'profile_results': {}, 'credential_session': None,
        'profile_operations': {}, 'workload_diagnostics': {}, 'host_lifecycle': [],
        'cleanup_errors': [], 'source_preserved': False}
    provider = None
    try:
        if args.authorization_id is not None and (not args.execute or args.authorization_id != AUTHORIZATION):
            raise ControllerFailure('DEVELOPMENT_CAPTURE_AUTHORIZATION_INVALID')
        if args.execute and (args.authorization_id != AUTHORIZATION or args.result is None):
            raise ControllerFailure('DEVELOPMENT_EXECUTION_AUTHORIZATION_REQUIRED')
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
                        if base_plan.candidate_version != 'v2.0.0-rc.1':
                            raise h.CandidateHarnessError('DEVELOPMENT_CANDIDATE_TARGET_INVALID')
                        plan = from_material_plan(base_plan, execution_source_sha=args.execution_source_sha,
                            execution_source_tree=args.execution_source_tree,
                            execution_inventory_digest=source.inventory_digest)
                        result['plan'] = plan.as_dict()
                        result['profile_results'] = {name: {'status': 'NOT_RUN', 'failure_code': None} for name in h.PROFILES}
                        if not args.execute:
                            result['status'] = 'PLAN_ONLY'
                        else:
                            batch = CandidateBatch(provider, plan, authorization_id=args.authorization_id)
                            provider._candidate_batch = batch
                            try:
                                for index, profile in enumerate(plan.profiles):
                                    try:
                                        report = provider.execute_development_profile(plan=profile,
                                            harness_plan=plan, candidate_root=material.loaded.root,
                                            initial_platform_state=h._initial_platform_state(profile.profile))
                                        if report['result'] != 'PASS':
                                            raise h.CandidateHarnessError('DEVELOPMENT_PROFILE_FAILED')
                                        result['profile_reports'][profile.profile] = report
                                        result['profile_results'][profile.profile] = {'status': 'PASS', 'failure_code': None}
                                        if provider.inspect_original_hashes() != dict(plan.original_vm_hashes):
                                            raise h.CandidateHarnessError('CANDIDATE_ORIGINAL_VM_MUTATED')
                                    except BaseException as error:
                                        result['failure_code'] = _failure(error)
                                        result['profile_results'][profile.profile] = {'status': 'ERROR', 'failure_code': _failure(error)}
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
            if (session['session_capture_attempts'] != 1 or session['session_capture_completed'] != 1
                    or session['secret_state'] != 'CLOSED' or len(roles) != 9
                    or any(role['delivery_attempts'] != 1 or role['delivery_completed'] != 1
                           or role['operation_result'] != 'PASS' for role in roles)):
                result['status'] = 'ERROR'
                result['all_profiles_pass'] = False
                result['failure_code'] = 'DEVELOPMENT_CAPTURE_ACCOUNTING_INVALID'
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
    parser.add_argument('--result', type=Path)
    args = parser.parse_args(argv)
    # Reserve only the public result filename before any VM or capture work.
    output = args.result.open('xb') if args.result is not None else None
    try:
        result = run(args)
        raw = h.canonical_json_bytes(result)
        if output is not None:
            output.write(raw)
            output.flush()
            os.fsync(output.fileno())
        print(raw.decode('utf-8'), end='')
        return 0 if result['status'] in {'PASS', 'PLAN_ONLY'} else 2
    finally:
        if output is not None:
            output.close()


if __name__ == '__main__':
    raise SystemExit(main())
