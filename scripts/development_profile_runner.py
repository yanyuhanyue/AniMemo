"""Run complete Installer checks from frozen development source against Q bytes."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re

from release.candidate import canonical_json_bytes, load_verified_candidate, sha256_bytes
from scripts import candidate_profile_runner as runner
from scripts.candidate_diagnostics import inherited_writer

SCHEMA = 'animemo.local-installer-development-profile-report/v1'
PURPOSE = 'LOCAL_INSTALLER_DEVELOPMENT'
OUTPUT = Path('/var/lib/animemo/local-development/profile-report.json')
_SHA = re.compile(r'[0-9a-f]{40}\Z')
_DIGEST = re.compile(r'sha256:[0-9a-f]{64}\Z')


def validate_development_report(value, *, loaded, expected_binding, expected_context):
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
    # Reuse every production observation check. Its temporary Draft is never
    # emitted as development authority or exposed as a Candidate receipt.
    runner.build_profile_receipt(loaded=loaded, profile=expected_context['profile'],
        context=expected_context, installer_output=value['installer_output'],
        started_at=value['started_at'], completed_at=value['completed_at'])
    return value


def execute_development_profile(*, binding, profile, context_b64url, command_runner=None):
    if (type(binding) is not dict or set(binding) != {'plan_digest', 'session_id',
            'execution_source_sha', 'execution_source_tree', 'execution_inventory_digest',
            'verified_candidate_digest', 'material_source_sha', 'material_source_tree',
            'qualification_run_id'}
            or any(type(binding[name]) is not str or not _SHA.fullmatch(binding[name])
                   for name in ('execution_source_sha', 'execution_source_tree', 'material_source_sha', 'material_source_tree'))
            or any(type(binding[name]) is not str or not _DIGEST.fullmatch(binding[name])
                   for name in ('plan_digest', 'execution_inventory_digest', 'verified_candidate_digest'))
            or type(binding['session_id']) is not str or re.fullmatch('[0-9a-f]{32}', binding['session_id']) is None
            or type(binding['qualification_run_id']) is not int or binding['qualification_run_id'] <= 0):
        raise runner.ProfileRunnerError('DEVELOPMENT_PROFILE_BINDING_INVALID')
    before = load_verified_candidate(binding['verified_candidate_digest']).candidate_input
    if (before['source_sha'] != binding['material_source_sha']
            or before['source_tree'] != binding['material_source_tree']
            or before['qualification_run_id'] != binding['qualification_run_id']):
        raise runner.ProfileRunnerError('DEVELOPMENT_MATERIAL_BINDING_INVALID')
    loaded, context, output, started, completed = runner._execute_profile_workload(
        verified_candidate_digest=binding['verified_candidate_digest'], profile=profile,
        public_origin='https://candidate.invalid', context_b64url=context_b64url,
        runner=command_runner, execution_root=Path(__file__).resolve().parents[1])
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
        expected_binding=binding, expected_context=context)


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
        if len(args.binding) > 8192:
            raise runner.ProfileRunnerError('DEVELOPMENT_PROFILE_BINDING_INVALID')
        binding = json.loads(args.binding, object_pairs_hook=reject_duplicate_json_keys)
        value = execute_development_profile(binding=binding, profile=args.profile,
            context_b64url=os.environ.get(runner.CONTEXT_ENV, ''))
        with OUTPUT.open('xb') as output:
            os.chmod(OUTPUT, 0o600)
            output.write(canonical_json_bytes(value))
        diagnostic.stage('DRAFT_WRITTEN')
        return 0
    except BaseException:
        diagnostic.error('PROFILE_RECEIPT_INVALID')
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
