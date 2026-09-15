"""Fixed Fresh platform preparation; diagnostic output grants no installation authority."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from installer.apt_diagnostics import utc_now
from installer.platform_bootstrap import (
    PLATFORM_BOOTSTRAP_ERROR_CODES, PlatformBootstrapActionKind,
    PlatformBootstrapError, PlatformBootstrapMode, ProductionPlatformBootstrap,
    SubprocessPlatformCommandRunner, _apt_argv, parse_platform_bootstrap_plan,
    parse_platform_bootstrap_receipt,
)
from installer.runtime import InstallTransportSource
from release.candidate import canonical_json_bytes, load_verified_candidate, sha256_bytes
from release.materials import reject_duplicate_json_keys
from scripts import candidate_profile_runner as runner
from scripts.candidate_diagnostics import DiagnosticError, inherited_writer, validate_apt_observation
from scripts.closed_runtime_inventory import closed_runtime_inventory_digest
from scripts.development_profile_runner import OUTPUT, validate_binding

SCHEMA = 'animemo.local-platform-development-diagnostic/v1'
MAX_REPORT_BYTES = 64 * 1024
MAX_APT_OPERATIONS = 4


class _ObservingRunner:
    def __init__(self):
        self.delegate = SubprocessPlatformCommandRunner()
        self.observations = []

    def run(self, argv, *, timeout, environment):
        result = self.delegate.run(argv, timeout=timeout, environment=environment)
        if result.observation is not None:
            if len(self.observations) >= MAX_APT_OPERATIONS:
                raise PlatformBootstrapError('PLATFORM_BOOTSTRAP_HOST_STATE_INCONSISTENT')
            self.observations.append(validate_apt_observation(result.observation))
        return result


def _binding_matches(binding, context, loaded):
    validate_binding(binding)
    if (binding.get('workload_mode') != 'PLATFORM_DIAGNOSTIC'
            or context.get('profile') != 'FRESH_BASE'
            or context.get('initial_platform_state') != dict(docker_present=False,
                network_allowed=True, runtime_dependencies_present=False)):
        raise runner.ProfileRunnerError('DEVELOPMENT_PROFILE_BINDING_INVALID')
    material = loaded.candidate_input
    if loaded.verified_digest != binding['verified_candidate_digest'] or any(material[name] != binding[target] for name, target in (
            ('source_sha', 'material_source_sha'), ('source_tree', 'material_source_tree'),
            ('qualification_run_id', 'qualification_run_id'))):
        raise runner.ProfileRunnerError('DEVELOPMENT_MATERIAL_BINDING_INVALID')


def validate_platform_diagnostic_report(value, *, loaded, expected_binding, expected_context):
    """Validate diagnostic identity/results without producing a Candidate receipt."""
    _binding_matches(expected_binding, expected_context, loaded)
    fields = {'schema', 'purpose', 'result', 'binding', 'context', 'platform_plan',
        'platform_receipt', 'apt_observations', 'error_code', 'started_at', 'completed_at',
        'candidate_acceptance_authority_granted', 'publish_authorized', 'report_digest'}
    if (type(value) is not dict or set(value) != fields or value['schema'] != SCHEMA
            or value['purpose'] != 'DEVELOPMENT_ONLY' or value['result'] not in {'PASS', 'FAIL'}
            or value['binding'] != expected_binding or value['context'] != expected_context
            or value['candidate_acceptance_authority_granted'] is not False
            or value['publish_authorized'] is not False):
        raise runner.ProfileRunnerError('DEVELOPMENT_PROFILE_REPORT_INVALID')
    body = {name: item for name, item in value.items() if name != 'report_digest'}
    if (len(canonical_json_bytes(value)) > MAX_REPORT_BYTES
            or sha256_bytes(canonical_json_bytes(body)) != value['report_digest']):
        raise runner.ProfileRunnerError('DEVELOPMENT_PROFILE_REPORT_DIGEST_INVALID')
    from datetime import datetime
    import re
    try:
        stamps = [value[name] for name in ('started_at', 'completed_at')]
        if not all(type(stamp) is str and re.fullmatch(
                r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?Z', stamp)
                for stamp in stamps):
            raise ValueError()
        if datetime.fromisoformat(stamps[0]) > datetime.fromisoformat(stamps[1]):
            raise ValueError()
    except ValueError:
        raise runner.ProfileRunnerError('DEVELOPMENT_PROFILE_REPORT_INVALID') from None
    plan = None
    if value['platform_plan'] is not None:
        plan = parse_platform_bootstrap_plan(canonical_json_bytes(value['platform_plan']))
        if (plan.mode is not PlatformBootstrapMode.ONLINE_FRESH
                or plan.transport_source is not InstallTransportSource.GITHUB):
            raise runner.ProfileRunnerError('DEVELOPMENT_PROFILE_REPORT_INVALID')
    observations = value['apt_observations']
    if type(observations) is not list or len(observations) > MAX_APT_OPERATIONS:
        raise runner.ProfileRunnerError('DEVELOPMENT_PROFILE_REPORT_INVALID')
    for observation in observations:
        try:
            validate_apt_observation(observation)
        except DiagnosticError:
            raise runner.ProfileRunnerError('DEVELOPMENT_PROFILE_REPORT_INVALID') from None
        if not (datetime.fromisoformat(stamps[0]) <= datetime.fromisoformat(observation['started_at'])
                <= datetime.fromisoformat(observation['ended_at']) <= datetime.fromisoformat(stamps[1])):
            raise runner.ProfileRunnerError('DEVELOPMENT_PROFILE_REPORT_INVALID')
    if value['result'] == 'FAIL':
        if (type(value['error_code']) is not str or value['error_code'] not in PLATFORM_BOOTSTRAP_ERROR_CODES
                or value['platform_receipt'] is not None):
            raise runner.ProfileRunnerError('DEVELOPMENT_PROFILE_REPORT_INVALID')
    else:
        if plan is None or value['error_code'] is not None or type(value['platform_receipt']) is not dict:
            raise runner.ProfileRunnerError('DEVELOPMENT_PROFILE_REPORT_INVALID')
        parse_platform_bootstrap_receipt(canonical_json_bytes(value['platform_receipt']), plan=plan)
        commands = [_apt_argv('update') if action.kind is PlatformBootstrapActionKind.APT_UPDATE
                    else _apt_argv('install', action.packages)
                    for action in plan.actions if action.kind in {
                        PlatformBootstrapActionKind.APT_UPDATE, PlatformBootstrapActionKind.INSTALL_DOCKER,
                        PlatformBootstrapActionKind.INSTALL_COMPOSE, PlatformBootstrapActionKind.INSTALL_POSTGRES_CLIENT}]
        if len(observations) != len(commands):
            raise runner.ProfileRunnerError('DEVELOPMENT_PROFILE_REPORT_INVALID')
        for observation, command in zip(observations, commands):
            expected = sha256_bytes(json.dumps(list(command), separators=(',', ':')).encode())
            if (observation['argv_contract'] != expected or observation['outcome'] != 'EXITED'
                    or observation['returncode'] != 0 or observation['categories'] != ['NONE']
                    or type(observation['tool_version']) is not str
                    or not observation['tool_version'].startswith('2.8.')
                    or observation['secondary_errors']
                    or any(observation[name]['missing'] for name in ('stdout', 'stderr'))):
                raise runner.ProfileRunnerError('DEVELOPMENT_PROFILE_REPORT_INVALID')
    return value


def execute_platform_diagnostic(*, binding, profile, context_b64url):
    context = runner._decode_context(context_b64url)
    loaded = load_verified_candidate(binding['verified_candidate_digest'])
    _binding_matches(binding, context, loaded)
    if profile != 'FRESH_BASE' or closed_runtime_inventory_digest(
            Path(__file__).resolve().parents[1]) != binding['execution_inventory_digest']:
        raise runner.ProfileRunnerError('DEVELOPMENT_PROFILE_BINDING_INVALID')
    diagnostic = inherited_writer()
    if diagnostic is None:
        raise runner.ProfileRunnerError('DEVELOPMENT_PROFILE_REPORT_INVALID')
    commands = _ObservingRunner()
    bootstrap = ProductionPlatformBootstrap(runner=commands)
    started, plan, receipt, error_code = utc_now(), None, None, None
    diagnostic.stage('PLATFORM_PREPARING')
    try:
        plan = bootstrap.plan(transport_source=InstallTransportSource.GITHUB)
        if plan.mode is not PlatformBootstrapMode.ONLINE_FRESH:
            raise PlatformBootstrapError('PLATFORM_BOOTSTRAP_PLAN_CHANGED')
        diagnostic.stage('PLATFORM_PLANNED')
        receipt = bootstrap.execute(plan, accepted_plan_digest=plan.plan_digest)
        diagnostic.stage('PLATFORM_READY')
    except PlatformBootstrapError as error:
        error_code = error.code
        from installer.cli import _diagnose_candidate_platform_failure
        _diagnose_candidate_platform_failure(diagnostic, error)
    value = dict(schema=SCHEMA, purpose='DEVELOPMENT_ONLY', result='FAIL' if error_code else 'PASS',
        binding=binding, context=context, platform_plan=plan.as_dict() if plan else None,
        platform_receipt=receipt.as_dict() if receipt else None,
        apt_observations=commands.observations, error_code=error_code,
        started_at=started, completed_at=utc_now(),
        candidate_acceptance_authority_granted=False, publish_authorized=False)
    value['report_digest'] = sha256_bytes(canonical_json_bytes(value))
    return validate_platform_diagnostic_report(value, loaded=loaded,
        expected_binding=binding, expected_context=context)


def main(argv=None):
    diagnostic = inherited_writer()
    if diagnostic is None:
        return 2
    diagnostic.stage('RUNNER_STARTED')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profile', required=True, choices=('FRESH_BASE',))
    parser.add_argument('--binding', required=True)
    args = parser.parse_args(argv)
    try:
        if len(args.binding) > 8192:
            raise runner.ProfileRunnerError('DEVELOPMENT_PROFILE_BINDING_INVALID')
        binding = json.loads(args.binding, object_pairs_hook=reject_duplicate_json_keys)
        value = execute_platform_diagnostic(binding=binding, profile=args.profile,
            context_b64url=os.environ.get(runner.CONTEXT_ENV, ''))
        diagnostic.stage('DRAFT_WRITING')
        with OUTPUT.open('xb') as output:
            os.chmod(OUTPUT, 0o600)
            output.write(canonical_json_bytes(value))
            output.flush()
            os.fsync(output.fileno())
        diagnostic.stage('DRAFT_WRITTEN')
        # The platform result is explicit in the report. Zero means the fixed
        # diagnostic operation and its report transport completed successfully.
        return 0
    except BaseException as error:
        diagnostic.error('PROFILE_RECEIPT_INVALID')
        diagnostic.fault(error)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
