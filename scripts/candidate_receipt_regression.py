"""No-VM, non-authoritative replay of complete Candidate output consumers."""
from __future__ import annotations

import argparse
import json
import lzma
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from release import candidate, metadata_freshness
from release.materials import reject_duplicate_json_keys
from release.publication_input import (
    PublicationInputError,
    build_publish_candidate_plan,
)
from release.r2_plugin_origin import validate_plugin_receipt
from scripts import candidate_vm_harness as harness
from scripts.candidate_result import CandidateResultFile

CONTEXT = 'NON_AUTHORITATIVE_LOCAL_REGRESSION'
MAX_FIXTURE_BYTES = 512 * 1024


def load_fixture(path):
    with Path(path).open('rb') as stream:
        raw = stream.read(MAX_FIXTURE_BYTES + 1)
    if len(raw) > MAX_FIXTURE_BYTES:
        raise ValueError('Regression fixture size limit')
    if Path(path).suffix == '.xz':
        decoder = lzma.LZMADecompressor(format=lzma.FORMAT_XZ, memlimit=8 * 1024 * 1024)
        raw = decoder.decompress(raw, max_length=MAX_FIXTURE_BYTES + 1)
        if len(raw) > MAX_FIXTURE_BYTES or not decoder.eof or decoder.unused_data:
            raise ValueError('Regression fixture stream invalid')
    value = json.loads(raw, object_pairs_hook=reject_duplicate_json_keys)
    if value.get('context') != CONTEXT:
        raise ValueError('Explicit regression context required')
    return value['source_result']


def _scope(value):
    plan = value['plan']
    identity = {key: item for key, item in plan.items() if key != 'planDigest'}
    if candidate.sha256_bytes(candidate.canonical_json_bytes(identity)) != plan['planDigest']:
        raise ValueError('Original plan identity mismatch')
    fields = {'verified_candidate_digest':'verifiedCandidateDigest', 'candidate_input_digest':'candidateInputDigest',
        'qualification_run_id':'qualificationRunId', 'source_sha':'sourceSha', 'source_tree':'sourceTree',
        'candidate_version':'candidateVersion', 'source_vm_digest':'sourceVmDigest',
        'source_vm_inventory_identity':'sourceVmInventoryIdentity', 'source_disk_graph_identity':'sourceDiskGraphIdentity',
        'original_vm_hashes':'originalVmHashes', 'plan_digest':'planDigest', 'session_id':'sessionId'}
    # Content-only builder context, never a CandidateHarnessPlan or capability.
    scope = SimpleNamespace(**{key: plan[source] for key, source in fields.items()})
    scope.profiles = tuple(SimpleNamespace(profile=p['profile'], snapshot_identity=p['snapshotIdentity'],
        snapshot_disk_graph_identity=p['snapshotDiskGraphIdentity']) for p in plan['profiles'])
    return scope


def exercise_complete_result(source, output_directory, *, loaded=None):
    output_directory = Path(output_directory)
    output_directory.mkdir(exist_ok=False)
    state = {'context':CONTEXT, 'status':'RUNNING', 'release_authority_granted':False,
        'publish_authorized':False, 'stage':'VALIDATE_SOURCE'}
    writer = CandidateResultFile(output_directory / 'result.json')
    writer.write(state)
    try:
        scope = _scope(source)
        receipts = source['profileReceipts']
        if set(receipts) != set(harness.PROFILES):
            raise ValueError('Three complete source Profile receipts required')
        for profile, value in receipts.items():
            candidate.validate_profile_receipt(value)
            if source['profileResults'][profile.lower()]['receipt_digest'] != candidate.sha256_bytes(candidate.canonical_json_bytes(value)):
                raise ValueError('Original Profile bytes changed')
        pre, post = source['r2OriginPrestateReceipt'], source['r2OriginPoststateReceipt']
        for role, value in (('PRESTATE',pre),('POSTSTATE',post)):
            validate_plugin_receipt(value, expected_scope=vars(scope), role=role)
        state.update(stage='AGGREGATE', profileReceipts=receipts,
            r2OriginPrestateReceipt=pre, r2OriginPoststateReceipt=post)
        writer.write(state)
        # The original run did not issue this Aggregate. It is constructed only
        # in this explicitly non-authoritative local test context.
        aggregate = harness.build_candidate_aggregate(scope, profile_results=source['profileResults'],
            receipts=receipts, candidate_prestate=source['candidatePrestate'],
            candidate_poststate=source['candidatePrestate'], r2_prestate_receipt=pre,
            r2_poststate_receipt=post, plugin_origin=True)
        state.update(stage='WIRE', aggregateReceipt=aggregate,
            aggregateReceiptSha256=candidate.aggregate_receipt_digest(aggregate))
        writer.write(state)
        exported = harness.export_candidate_aggregate(aggregate)
        state.update(exported, stage='CLI')
        writer.write(state)
        wire_path = output_directory / 'candidate-wire.txt'
        wire = exported['candidateAcceptanceReceiptB64url']
        wire_path.write_bytes(wire.encode('ascii'))
        decoded_path = output_directory / 'decoded-aggregate.json'
        command = [sys.executable, '-X', 'utf8', '-B', '-m', 'release.cli', 'decode-candidate-acceptance-receipt',
            '--value-file', str(wire_path), '--output', str(decoded_path)]
        completed = subprocess.run(command, cwd=Path(__file__).resolve().parents[1],
            stdin=subprocess.DEVNULL, capture_output=True, timeout=60, check=False)
        state['cli_exit_code'] = completed.returncode
        (output_directory / 'cli-result.json').write_bytes(completed.stdout)
        if completed.returncode:
            raise ValueError('Actual CLI process rejected the bounded wire file')
        identity = json.loads(completed.stdout, object_pairs_hook=reject_duplicate_json_keys)
        raw = candidate.canonical_json_bytes(aggregate)
        if decoded_path.read_bytes() != raw or identity['sha256'] != state['aggregateReceiptSha256']:
            raise ValueError('CLI changed original canonical bytes')
        state.update(stage='CONSUMERS', cli_argv_characters=len(subprocess.list2cmdline(command)),
            wire_in_argv=False, canonical_cli_bytes_identical=True)
        writer.write(state)
        expected = SimpleNamespace(qualification_run_id=scope.qualification_run_id,
            candidate_sha=scope.source_sha, candidate_tree=scope.source_tree,
            candidate_version=scope.candidate_version,
            candidate_acceptance_receipt_sha256=state['aggregateReceiptSha256'])
        parsed, parsed_raw = metadata_freshness._load_candidate_acceptance_receipt(decoded_path,
            identity=expected, current_time=datetime.now(timezone.utc))
        if parsed != aggregate or parsed_raw != raw:
            raise ValueError('Freshness changed original canonical bytes')
        state['freshness_receipt_consumer'] = 'PASS'
        try:
            plan = build_publish_candidate_plan(loaded, parsed)
        except PublicationInputError as error:
            if loaded is not None:
                raise
            state['publish_candidate_consumer'] = {'result':'REJECTED_NO_VERIFIED_MATERIAL_CONTEXT', 'code':error.code}
        else:
            if plan['mutation_authorized'] or plan['publish_rebuild_count'] or plan['manifest_generation_count']:
                raise ValueError('Unexpected publication mutation authority')
            state['publish_candidate_consumer'] = {'result':'PASS', 'mutation_authorized':False,
                'plan_digest':plan['plan_digest']}
        inputs = {'qualification_run_id':str(scope.qualification_run_id), 'intended_main_sha':scope.source_sha,
            'candidate_acceptance_receipt_b64url':wire}
        input_characters = sum(len(value) for value in inputs.values())
        if input_characters > 65535:
            raise ValueError('Workflow input group limit exceeded')
        (output_directory / 'freshness-input-assembly.json').write_bytes(candidate.canonical_json_bytes(
            {'context':CONTEXT, 'dispatched':False, 'inputs':inputs}))
        state.update(status='LOCAL_REGRESSION_PASSED', stage='COMPLETE', controller_exit_code=0,
            workflow_input_characters=input_characters, workflow_dispatch_count=0,
            source_profile_bytes={name:len(candidate.canonical_json_bytes(value)) for name,value in receipts.items()})
        writer.write(state)
        return {key:value for key,value in state.items() if key not in {
            'aggregateReceipt','profileReceipts','r2OriginPrestateReceipt','r2OriginPoststateReceipt','candidateAcceptanceReceiptB64url'}}
    except BaseException as error:
        state.update(status='ERROR', controller_exit_code=2,
            failure_code=getattr(error,'code','LOCAL_RECEIPT_REGRESSION_FAILED'))
        writer.write(state)
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--fixture', type=Path, required=True)
    parser.add_argument('--output-directory', type=Path, required=True)
    parser.add_argument('--state-root', type=Path)
    args = parser.parse_args(argv)
    try:
        source = load_fixture(args.fixture)
        loaded = (candidate.load_verified_candidate(source['plan']['verifiedCandidateDigest'],
            _state_root=args.state_root) if args.state_root is not None else None)
        result = exercise_complete_result(source, args.output_directory, loaded=loaded)
    except (OSError, ValueError, harness.CandidateHarnessError):
        print(json.dumps({'context':CONTEXT,'status':'ERROR','controller_exit_code':2}))
        return 2
    print(json.dumps(result, ensure_ascii=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
