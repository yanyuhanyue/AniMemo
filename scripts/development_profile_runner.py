"""Run complete Installer checks from frozen development source against Q bytes."""
from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path

from installer.development import expected_service_observation
from release.candidate import (
    canonical_json_bytes,
    load_verified_candidate,
    sha256_bytes,
)
from scripts import candidate_profile_runner as runner
from scripts.candidate_diagnostics import RUNNER_FAILURE_CODES, inherited_writer

SCHEMA = 'animemo.local-installer-development-profile-report/v1'
PURPOSE = 'LOCAL_INSTALLER_DEVELOPMENT'
OUTPUT = Path('/var/lib/animemo/local-development/profile-report.json')
_SHA = re.compile(r'[0-9a-f]{40}\Z')
_DIGEST = re.compile(r'sha256:[0-9a-f]{64}\Z')


def validate_development_report(value, *, loaded, expected_binding, expected_context, expected_service_source):
    validate_binding(expected_binding, profile=expected_context.get('profile'))
    if expected_binding['workload_mode'] != 'CLEAN_PREACCEPTANCE':
        raise runner.ProfileRunnerError('DEVELOPMENT_PROFILE_BINDING_INVALID')
    fields = {'schema', 'purpose', 'result', 'binding', 'context', 'installer_output',
              'started_at', 'completed_at', 'candidate_acceptance_authority_granted',
              'publish_authorized', 'report_digest'}
    if (type(value) is not dict or set(value) != fields or value['schema'] != SCHEMA
            or value['purpose'] != PURPOSE or value['result'] != 'PASS'
            or value['binding'] != expected_binding or value['context'] != expected_context
            or value['candidate_acceptance_authority_granted'] is not False
            or value['publish_authorized'] is not False):
        raise runner.ProfileRunnerError('DEVELOPMENT_PROFILE_REPORT_INVALID')
    body = {key: item for key, item in value.items() if key != 'report_digest'}
    if value['report_digest'] != sha256_bytes(canonical_json_bytes(body)):
        raise runner.ProfileRunnerError('DEVELOPMENT_PROFILE_REPORT_DIGEST_INVALID')
    if (type(expected_service_source) is not dict
            or expected_service_source.get('execution_inventory_digest') != expected_binding['execution_inventory_digest']
            or type(value['installer_output']) is not dict
            or value['installer_output'].get('developmentServiceSourceObservation') != expected_service_source):
        raise runner.ProfileRunnerError('DEVELOPMENT_SERVICE_SOURCE_MISMATCH')
    if expected_binding.get('runtime_offline_only'):
        from installer.development_boundary import validate_operation_result
        from installer.development_lifecycle import validate_instance_stop
        validate_operation_result(value['installer_output'].get('developmentOperationPlan'),
            expected_binding, value['installer_output'])
        validate_instance_stop(value['installer_output'].get('developmentInstanceStop'),
            binding=expected_binding,
            installer_plan_digest=value['installer_output'].get('installerPlanDigest'))
    # Runtime DEV uses its confirmed public LocalBundle identity. The original
    # candidate input remains a separate identity and cannot stand in for it.
    try:
        if expected_binding.get('runtime_offline_only'):
            material = expected_binding['runtime_operation_boundary']['material']
            if material['images'] != {item.role: item.digest for item in loaded.images.images}:
                raise runner.ProfileRunnerError('DEVELOPMENT_MATERIAL_BINDING_INVALID')
            from release.candidate import _parse_time
            start = _parse_time(value['started_at'], code='DEVELOPMENT_REPORT_TIME_INVALID').timestamp()
            completed = _parse_time(value['completed_at'], code='DEVELOPMENT_REPORT_TIME_INVALID').timestamp()
            lifetime = expected_binding['runtime_operation_boundary']['lifetime']
            production = value['installer_output'].get('productionExecutionObservation', {})
            resources = expected_binding['runtime_operation_boundary']['resources']
            if (not lifetime['boot_started_utc_seconds'] - 5 <= start <= completed <= lifetime['effective_expires_utc_seconds']
                    or production.get('doctorReport', {}).get('instanceId')
                        != value['installer_output']['installerResult']['instanceId']
                    or production.get('networkObservation', {}).get('egressIsolation', {}).get('service')
                        != resources['service']
                    or production.get('networkObservation', {}).get('egressIsolation', {}).get('containerNetwork')
                        != resources['compose_project'] + '_animemo'):
                raise runner.ProfileRunnerError('DEVELOPMENT_PROFILE_RESULT_SCOPE_MISMATCH')
            runner.validate_profile_execution(profile=expected_context['profile'],
                context=expected_context, installer_output=value['installer_output'],
                expected_material_identity=material['release']['materialIdentityDigest'],
                expected_images=material['images'])
        else:
            runner.build_profile_receipt(loaded=loaded, profile=expected_context['profile'],
                context=expected_context, installer_output=value['installer_output'],
                started_at=value['started_at'], completed_at=value['completed_at'])
    except BaseException:
        diagnostic = inherited_writer()
        observation = value['installer_output'].get('productionExecutionObservation')
        if diagnostic is not None and type(observation) is dict:
            network, pulls, doctor = (observation.get(name) for name in
                ('networkObservation', 'externalPullObservation', 'doctorReport'))
            if all(type(item) is dict for item in (network, pulls, doctor)):
                commands, denied, checks = (network.get('completedCommands'),
                    pulls.get('pullDeniedCommandDigests'), doctor.get('checks'))
                if all(type(item) is list and len(item) <= 8 * 1024 * 1024 for item in (commands, denied, checks)):
                    diagnostic.event('REPORT_COUNTS', commands=len(commands),
                        pull_denied_commands=len(denied), doctor_checks=len(checks))
        raise
    return value


def validate_binding(binding, *, profile=None):
    published = type(binding) is dict and binding.get('workload_mode') == 'PUBLISHED_PLATFORM_PLAN'
    expected = {'plan_digest', 'session_id',
            'execution_source_sha', 'execution_source_tree', 'execution_inventory_digest',
            'verified_candidate_digest', 'material_source_sha', 'material_source_tree',
            'qualification_run_id', 'workload_mode'}
    if published:
        expected.add('published_subject_digest')
    offline = type(binding) is dict and 'runtime_offline_only' in binding
    if offline:
        expected.update(('runtime_offline_only', 'runtime_trust_selection_digest', 'candidate_input_digest'))
        if type(binding) is dict and ('runtime_operation_boundary' in binding or 'runtime_lifetime' in binding):
            expected.update(('runtime_operation_boundary', 'runtime_lifetime'))
    if (type(binding) is not dict or set(binding) != expected
            or type(binding['workload_mode']) is not str
            or binding['workload_mode'] not in {'CLEAN_PREACCEPTANCE', 'PLATFORM_DIAGNOSTIC', 'PUBLISHED_PLATFORM_PLAN'}
            or offline and (binding['runtime_offline_only'] is not True
                or binding['workload_mode'] != 'CLEAN_PREACCEPTANCE'
                or profile is not None and profile != 'RUNTIME_BASE_OFFLINE')
            or published and (type(binding['published_subject_digest']) is not str
                or not _DIGEST.fullmatch(binding['published_subject_digest']))
            or any(type(binding[name]) is not str or not _SHA.fullmatch(binding[name])
                   for name in ('execution_source_sha', 'execution_source_tree', 'material_source_sha', 'material_source_tree'))
            or any(type(binding[name]) is not str or not _DIGEST.fullmatch(binding[name])
                   for name in ('plan_digest', 'execution_inventory_digest', 'verified_candidate_digest'))
            or type(binding['session_id']) is not str or re.fullmatch('[0-9a-f]{32}', binding['session_id']) is None
            or type(binding['qualification_run_id']) is not int or binding['qualification_run_id'] <= 0):
        raise runner.ProfileRunnerError('DEVELOPMENT_PROFILE_BINDING_INVALID')
    if offline:
        if any(type(binding[name]) is not str or not _DIGEST.fullmatch(binding[name])
               for name in ('runtime_trust_selection_digest', 'candidate_input_digest')):
            raise runner.ProfileRunnerError('DEVELOPMENT_PROFILE_BINDING_INVALID')
        if 'runtime_operation_boundary' in binding:
            from installer.development_boundary import (
                validate_runtime_operation_boundary,
            )
            validate_runtime_operation_boundary(binding['runtime_operation_boundary'], binding)
    return binding


def execute_development_profile(*, binding, profile, context_b64url, command_runner=None):
    validate_binding(binding, profile=profile)
    if binding['workload_mode'] != 'CLEAN_PREACCEPTANCE':
        raise runner.ProfileRunnerError('DEVELOPMENT_PROFILE_BINDING_INVALID')
    if binding.get('runtime_offline_only'):
        from scripts.candidate_diagnostics import FD_ENV, bounded_process_output
        from scripts.runtime_development_boundary import RuntimeDeadline
        lifetime = RuntimeDeadline.from_guest_envelope(binding['runtime_lifetime'], binding=binding)
        class RuntimeCommandRunner:
            def run(self, argv, environment):
                return bounded_process_output(argv, environment=dict(environment),
                    timeout=lifetime.workload_timeout(4 * 60 * 60),
                    pass_fds=((int(environment[FD_ENV]),) if FD_ENV in environment else ()))
        command_runner = command_runner or RuntimeCommandRunner()
    before = load_verified_candidate(binding['verified_candidate_digest']).candidate_input
    if (before['source_sha'] != binding['material_source_sha']
            or before['source_tree'] != binding['material_source_tree']
            or before['qualification_run_id'] != binding['qualification_run_id']):
        raise runner.ProfileRunnerError('DEVELOPMENT_MATERIAL_BINDING_INVALID')
    loaded, context, output, started, completed = runner._execute_profile_workload(
        verified_candidate_digest=binding['verified_candidate_digest'], profile=profile,
        public_origin='https://candidate.invalid', context_b64url=context_b64url,
        runner=command_runner, execution_root=Path(__file__).resolve().parents[1], development_binding=binding)
    material = loaded.candidate_input
    if (material['source_sha'] != binding['material_source_sha']
            or material['source_tree'] != binding['material_source_tree']
            or material['qualification_run_id'] != binding['qualification_run_id']):
        raise runner.ProfileRunnerError('DEVELOPMENT_MATERIAL_BINDING_INVALID')
    value = {'schema': SCHEMA, 'purpose': PURPOSE, 'result': 'PASS', 'binding': binding,
             'context': context, 'installer_output': output,
             'started_at': started, 'completed_at': completed,
             'candidate_acceptance_authority_granted': False, 'publish_authorized': False}
    value['report_digest'] = sha256_bytes(canonical_json_bytes(value))
    return validate_development_report(value, loaded=loaded,
        expected_binding=binding, expected_context=context,
        expected_service_source=expected_service_observation(Path(__file__).resolve().parents[1], binding['execution_inventory_digest']))


def main(argv=None):
    diagnostic = inherited_writer()
    if diagnostic is None:
        return 2
    diagnostic.stage('RUNNER_STARTED')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profile', required=True, choices=runner.PROFILES)
    parser.add_argument('--binding', required=True)
    args = parser.parse_args(argv)
    try:
        from release.materials import reject_duplicate_json_keys
        if len(args.binding.encode('utf-8')) > 128 * 1024:
            raise runner.ProfileRunnerError('DEVELOPMENT_PROFILE_BINDING_INVALID')
        binding = json.loads(args.binding, object_pairs_hook=reject_duplicate_json_keys)
        value = execute_development_profile(binding=binding, profile=args.profile,
            context_b64url=os.environ.get(runner.CONTEXT_ENV, ''))
        diagnostic.stage('DRAFT_WRITING')
        with OUTPUT.open('xb') as output:
            os.chmod(OUTPUT, 0o600)
            output.write(canonical_json_bytes(value))
        diagnostic.stage('DRAFT_WRITTEN')
        return 0
    except BaseException as error:  # noqa: BLE001 - fixed entry emits only bounded diagnostics
        diagnostic.error('PROFILE_RECEIPT_INVALID')
        if isinstance(error, runner.ProfileRunnerError) and error.code in RUNNER_FAILURE_CODES:
            diagnostic.error(error.code)
        diagnostic.fault(error)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
