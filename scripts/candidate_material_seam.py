"""Development regression against the failed M6 artifact; no Candidate authority."""
from __future__ import annotations

import argparse
import ast
from datetime import datetime, timezone
import hashlib
import json
import os
import io
import re
import signal
import threading
from types import SimpleNamespace
from pathlib import Path
import subprocess
import sys

BASE_SHA = 'bd781acec7b157fdef4c6633df0964ecc612d2af'
BASE_TREE = '4f0e5b40ae8352aef99dc2c0632c5e3dded767bb'
RUN = 34668423966
ARTIFACT = 10290367841
ZIP_DIGEST = '5244257c539f0458434d35daf172c91584e806f004a44d6939599e3963c051db'
VERIFIED = 'sha256:3e8293cf3b015b18f2157678bef3d3905c719eef34640878c4d528be37d3bd15'


def download(destination):
    destination.mkdir(parents=True, exist_ok=False)
    endpoints = {'run': f'actions/runs/{RUN}', 'jobs': f'actions/runs/{RUN}/jobs?per_page=100',
        'artifacts': f'actions/runs/{RUN}/artifacts?per_page=100', 'artifact': f'actions/artifacts/{ARTIFACT}'}
    for name, endpoint in endpoints.items():
        result = subprocess.run(['gh', 'api', 'repos/yanyuhanyue/AniMemo/' + endpoint],
            stdin=subprocess.DEVNULL, capture_output=True, timeout=60)
        if result.returncode:
            raise RuntimeError('DEVELOPMENT_ARTIFACT_METADATA_UNAVAILABLE')
        value = json.loads(result.stdout)
        if name == 'artifact':
            assert value['digest'] == 'sha256:' + ZIP_DIGEST and not value['expired']
            assert value['workflow_run']['id'] == RUN and value['workflow_run']['head_sha'] == BASE_SHA
        if name in {'jobs', 'artifacts'}:
            assert value['total_count'] == len(value[name])
        (destination / (name + '.json')).write_bytes(result.stdout)
    with (destination / 'evidence.zip').open('xb') as output:
        result = subprocess.run(['gh', 'api', f'repos/yanyuhanyue/AniMemo/actions/artifacts/{ARTIFACT}/zip'],
            stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.DEVNULL, timeout=300)
    assert result.returncode == 0
    with (destination / 'evidence.zip').open('rb') as source:
        assert hashlib.file_digest(source, 'sha256').hexdigest() == ZIP_DIGEST


def probe(destination, baseline):
    assert os.name == 'posix' and os.geteuid() == 0 and os.environ.get('GITHUB_ACTIONS') == 'true'
    head = subprocess.check_output(['git', '-c', 'safe.directory=' + str(baseline), '-C', str(baseline), 'rev-parse', 'HEAD'], text=True).strip()
    assert head == BASE_SHA
    sys.path.insert(0, str(baseline))
    from release import candidate
    from scripts import closed_runtime_inventory as inventory, candidate_workload_root as root_module
    fixed = Path('/var/lib/animemo/prepublication-candidates/v2')
    assert not fixed.exists()
    value = candidate.verify_prepublication_candidate(archive=destination / 'evidence.zip',
        run_metadata=json.loads((destination / 'run.json').read_bytes()),
        jobs_metadata=json.loads((destination / 'jobs.json').read_bytes()),
        artifacts_metadata=json.loads((destination / 'artifacts.json').read_bytes()),
        containing_artifact_id=ARTIFACT, containing_artifact_api_digest='sha256:' + ZIP_DIGEST,
        expected_run_id=RUN, expected_source_sha=BASE_SHA, expected_source_tree=BASE_TREE,
        expected_candidate_version='v2.0.0-rc.1', verified_at=datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
        _state_root=destination / 'verified-base')
    assert value['verifiedCandidateDigest'] == VERIFIED
    loaded = candidate.load_verified_candidate(VERIFIED, _state_root=destination / 'verified-base')
    namespace = {'__name__': '_development_root_seam'}
    for module in (inventory, root_module):
        exec(compile(Path(module.__file__).read_bytes(), '<actual-M6-module>', 'exec'), namespace)
    sealed = destination / 'root-seal-probe'
    sealed.mkdir(mode=0o700)
    source_fd = inventory._open_directory_chain(loaded.root)
    target_fd = inventory._open_directory_chain(sealed)
    try:
        namespace['_copy_stage'](source_fd, target_fd)
    finally:
        os.close(source_fd)
        os.close(target_fd)
    assert inventory.closed_runtime_inventory_digest(sealed) == inventory.closed_runtime_inventory_digest(loaded.root)
    installer = sealed / 'installer-root'
    runtime = destination / 'actual-wheel-runtime'
    # Actual distribution wheels and actual M6 Runner are loaded by a fresh,
    # isolated interpreter. No synthetic Runner replaces the import boundary.
    program = '''import json,runpy,sys
from pathlib import Path
result={'runtime_installed':False,'runner_module_loaded':False,'installer_executed':False,'failure_category':None}
try:
 sys.path.insert(0,sys.argv[1])
 from installer.offline_python_runtime import install_wheel_runtime
 install_wheel_runtime(Path(sys.argv[1])/'wheelhouse',Path(sys.argv[2]))
 sys.path.insert(0,sys.argv[2])
 result['runtime_installed']=True
 runpy.run_path(str(Path(sys.argv[1])/'scripts/candidate_profile_runner.py'),run_name='_development_import_probe')
 result['runner_module_loaded']=True
except ModuleNotFoundError:
 result['failure_category']='MODULE_NOT_FOUND'
except ImportError:
 result['failure_category']='IMPORT_FAILED'
except BaseException:
 result['failure_category']='RUNTIME_OR_RUNNER_INITIALIZATION_FAILED'
print(json.dumps(result,sort_keys=True))
raise SystemExit(0 if result['runner_module_loaded'] else 2)
'''
    completed = subprocess.run(['/usr/bin/python3', '-I', '-S', '-B', '-c', program, str(installer), str(runtime)],
        env={'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'}, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=120)
    assert len(completed.stdout) < 4096
    observation = json.loads(completed.stdout)
    assert set(observation) == {'runtime_installed', 'runner_module_loaded', 'installer_executed', 'failure_category'}
    result = dict(scope='DEVELOPMENT_M6_MATERIAL_SEAM_NOT_PROFILE_ACCEPTANCE',
        artifact_id=ARTIFACT, qualification_run_id=RUN, artifact_sha256='sha256:' + ZIP_DIGEST,
        baseline_sha=BASE_SHA, actual_wheel_count=len(list((installer / 'wheelhouse').glob('*.whl'))),
        actual_root_fd_copy=True, full_inventory_match=True, child_exit_code=completed.returncode,
        import_probe=observation)
    if completed.returncode == 0:
        result['fixed_root_execution'] = exercise_fixed_root(destination, loaded, namespace, inventory)
    (destination / 'material-seam-result.json').write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
    os.chmod(destination / 'material-seam-result.json', 0o644)
    print(json.dumps(result, sort_keys=True))
    return completed.returncode


def exercise_fixed_root(destination, loaded, namespace, inventory):
    """Actual M6 root and actual Runner/Installer, in this disposable CI VM.

    Only the root's process adapter is observed. Commands, program, runtime,
    Runner, and Installer bytes are unchanged. The VM identity context is an
    explicit development fixture and cannot issue a Host acceptance receipt.
    """
    session = '6d6e81b4f0ac4f3c9ced1a5648fc980f'
    profile = 'DOCKER_BASE'
    stage = Path('/tmp') / ('animemo-candidate-' + session + '-' + profile)
    stage.mkdir(mode=0o700)
    source_fd, stage_fd = inventory._open_directory_chain(loaded.root), inventory._open_directory_chain(stage)
    try:
        namespace['_copy_stage'](source_fd, stage_fd)
    finally:
        os.close(source_fd)
        os.close(stage_fd)
    observations = dict(scope='DEVELOPMENT_ACTUAL_M6_FIXED_ROOT_WITH_SYNTHETIC_VM_CONTEXT',
        root_entered=False, root_copy_completed=False, runtime_runner_started=False,
        runtime_runner_exit_code=None, runtime_runner_error_code=None,
        runtime_runner_error_category=None, root_returned=False, root_error_code=None,
        root_error_category=None, draft_returned=False, host_receipt_issued=False,
        child_timeout=False, child_stderr_limit_exceeded=False)
    allowed_codes = set()
    for name in ('scripts/candidate_profile_runner.py', 'scripts/candidate_workload_root.py', 'release/candidate.py'):
        for node in ast.walk(ast.parse((loaded.root / 'installer-root' / name).read_bytes())):
            if isinstance(node, ast.Constant) and type(node.value) is str and re.fullmatch('[A-Z][A-Z0-9_]{1,119}', node.value):
                allowed_codes.add(node.value)
    original_copy = namespace['_copy_stage']
    def copy(src, dst):
        original_copy(src, dst)
        observations['root_copy_completed'] = True
    def observed_run(argv, **options):
        assert argv[0] == '/usr/bin/python3' and argv[1:4] == ['-I', '-B', '-c']
        process = subprocess.Popen(argv, env=options['env'], stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, start_new_session=True)
        observations['runtime_runner_started'] = True
        def stop():
            observations['child_timeout'] = True
            if process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        timer = threading.Timer(600, stop)
        timer.daemon = True
        timer.start()
        body = bytearray()
        try:
            while True:
                block = process.stderr.read(min(4096, 16385 - len(body)))
                if not block:
                    break
                body.extend(block)
                if len(body) > 16384:
                    observations['child_stderr_limit_exceeded'] = True
                    stop()
                    break
            code = process.wait(timeout=10)
            observations['runtime_runner_exit_code'] = code
            if body and not observations['child_stderr_limit_exceeded']:
                try:
                    value = json.loads(body)
                except (ValueError, UnicodeError):
                    value = None
                if type(value) is dict and set(value) == {'code'} and value['code'] in allowed_codes:
                    observations['runtime_runner_error_code'] = value['code']
                else:
                    # No raw line, path, module name, exception text, or traceback
                    # is retained; map only known interpreter exception classes.
                    for category in ('ModuleNotFoundError', 'ImportError', 'FileNotFoundError', 'PermissionError', 'ValueError'):
                        if any(line.startswith(category.encode() + b':') for line in body.splitlines()):
                            observations['runtime_runner_error_category'] = category
                            break
                    if observations['runtime_runner_error_category'] is None:
                        observations['runtime_runner_error_category'] = 'UNCLASSIFIED_NONZERO_OUTPUT'
            return subprocess.CompletedProcess(argv, code, b'', b'')
        finally:
            timer.cancel()
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=10)
            process.stderr.close()
            body[:] = b'\0' * len(body)
    namespace['_copy_stage'] = copy
    namespace['subprocess'] = SimpleNamespace(run=observed_run, DEVNULL=subprocess.DEVNULL)
    draft = io.BytesIO()
    namespace['sys'] = SimpleNamespace(stdout=SimpleNamespace(buffer=draft))
    fixture_digest = 'sha256:' + hashlib.sha256(b'development-only-vm-context').hexdigest()
    context = {name: fixture_digest for name in ('base_vm_identity', 'clone_identity', 'snapshot_identity',
        'source_disk_graph_identity', 'source_vm_inventory_identity', 'snapshot_disk_graph_identity')}
    context.update(profile=profile, original_vm_pre_hashes={'development-only.vmx': fixture_digest},
        initial_platform_state={'docker_present': True, 'runtime_dependencies_present': False, 'network_allowed': True})
    try:
        observations['root_entered'] = os.geteuid() == 0
        namespace['run_fixed_candidate'](session_id=session, profile=profile,
            input_digest=loaded.verified['candidate_input_sha256'], verified_digest=VERIFIED,
            inventory_digest=inventory.closed_runtime_inventory_digest(loaded.root), context=context)
        observations['root_returned'] = True
        observations['draft_returned'] = len(draft.getbuffer()) > 0
    except BaseException as error:
        if type(error).__name__ in {'ValueError', 'PermissionError', 'FileNotFoundError', 'FileExistsError', 'SystemExit'}:
            observations['root_error_category'] = type(error).__name__
        else:
            observations['root_error_category'] = 'OTHER_FIXED_ROOT_FAILURE'
        if str(error) in allowed_codes:
            observations['root_error_code'] = str(error)
    finally:
        draft.close()
    return observations


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('operation', choices=('download', 'probe'))
    parser.add_argument('--destination', type=Path, required=True)
    parser.add_argument('--baseline', type=Path)
    args = parser.parse_args()
    if os.environ.get('GITHUB_ACTIONS') != 'true':
        raise SystemExit('ISOLATED_CI_ONLY')
    if args.operation == 'download':
        download(args.destination)
        return 0
    return probe(args.destination, args.baseline)


if __name__ == '__main__':
    raise SystemExit(main())
