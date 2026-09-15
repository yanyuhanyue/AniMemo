"""Development-only real Linux gh and parser probe; it never requests sudo."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys


def prepare_probe_inputs(*,loaded,source_sha,execution_root,linux_gh,sidecar):
    from installer.formal_bootstrap import GH_EXE_SHA256
    from release.candidate import canonical_json_bytes, reject_duplicate_json_keys
    from release.materials import read_bounded_release_file
    from updater.offline import _extract_sigstore_bundle
    from release.materials import INITIAL_TRUST_KIT_PREFIX
    root=Path(execution_root)
    probe=root/'development-probe'
    probe.mkdir(mode=0o700)
    gh=read_bounded_release_file(Path(linux_gh),subject='Fixed Linux gh',maximum=64*1024*1024)
    if hashlib.sha256(gh).hexdigest()!=GH_EXE_SHA256 or gh[:4]!=b'\x7fELF':
        raise ValueError('DEVELOPMENT_LINUX_GH_IDENTITY_MISMATCH')
    encoded=read_bounded_release_file(Path(sidecar),subject='Original published sidecar',maximum=16*1024*1024)
    envelope=json.loads(encoded,object_pairs_hook=reject_duplicate_json_keys)
    candidate=loaded.candidate_input
    if envelope.get('tag')!=candidate['candidate_version'] or envelope.get('commit')!=candidate['source_sha']:
        raise ValueError('DEVELOPMENT_PROBE_SUBJECT_MISMATCH')
    manifest=loaded.root.joinpath('release-manifest.json').read_bytes()
    # The Q kit stores pretty-printed JSON objects. gh's JSONL reader needs
    # one complete object per line; derive a separate transport copy without
    # modifying either qualified trust root or changing its JSON value.
    roots=[]
    root_inputs={}
    for name in ('github-trusted-root.jsonl','sigstore-trusted-root.jsonl'):
        raw=(root/INITIAL_TRUST_KIT_PREFIX/name).read_bytes()
        value=json.loads(raw,object_pairs_hook=reject_duplicate_json_keys)
        if type(value) is not dict:
            raise ValueError('DEVELOPMENT_PROBE_TRUST_ROOT_INVALID')
        root_inputs[name]=hashlib.sha256(raw).hexdigest()
        roots.append(canonical_json_bytes(value))
    trusted_roots=b''.join(roots)
    context=dict(schema='animemo.development-linux-attestation-probe/v1',
        execution_source_sha=source_sha,subject_source_sha=candidate['source_sha'],
        subject_version=candidate['candidate_version'],gh_sha256=GH_EXE_SHA256,
        manifest_sha256=hashlib.sha256(manifest).hexdigest(),
        parser_sha256=hashlib.sha256((root/'updater/source.py').read_bytes()).hexdigest(),
        trusted_root_inputs=root_inputs,trusted_root_transport_sha256=hashlib.sha256(trusted_roots).hexdigest())
    values={'gh':gh,'release-manifest.json':manifest,
        'manifest.bundle.json':canonical_json_bytes(_extract_sigstore_bundle(encoded,'release-manifest')),
        'context.json':canonical_json_bytes(context),'trusted-roots.jsonl':trusted_roots}
    for name,data in values.items():
        with (probe/name).open('xb') as stream:stream.write(data)
    return {'development-probe/'+name:'sha256:'+hashlib.sha256(data).hexdigest() for name,data in values.items()}


def run():
    # This executes as the verified bootstrap user in a newly created Guest.
    # The enclosing fixed host command has already checked the entire tree.
    root=Path(__file__).resolve().parents[1]
    probe=root/'development-probe'
    context=json.loads((probe/'context.json').read_bytes())
    if sys.platform!='linux' or os.geteuid()==0:
        raise ValueError('DEVELOPMENT_UNPRIVILEGED_LINUX_REQUIRED')
    for path,key in ((root/'updater/source.py','parser_sha256'),(probe/'gh','gh_sha256'),
        (probe/'release-manifest.json','manifest_sha256'),
        (probe/'trusted-roots.jsonl','trusted_root_transport_sha256')):
        if hashlib.sha256(path.read_bytes()).hexdigest()!=context[key]:
            raise ValueError('DEVELOPMENT_PROBE_INPUT_CHANGED')
    from installer.offline_python_runtime import install_wheel_runtime
    runtime=probe/'python-runtime'
    install_wheel_runtime(root/'wheelhouse',runtime)
    sys.path.insert(1,str(runtime))
    from updater.source import GitHubReleaseSource
    imported=Path(sys.modules['updater.source'].__file__).resolve(strict=True)
    if imported!=root/'updater/source.py':
        raise ValueError('DEVELOPMENT_PROBE_MODULE_SHADOWED')
    gh=probe/'gh'
    gh.chmod(0o700)
    home=probe/'gh-home'
    home.mkdir(mode=0o700)
    environment=dict(PATH='/usr/bin:/bin',HOME=str(home),GH_CONFIG_DIR=str(home),GH_PROMPT_DISABLED='1',
        LANG='C.UTF-8',LC_ALL='C.UTF-8')
    version=subprocess.run([str(gh),'version'],env=environment,stdin=subprocess.DEVNULL,
        capture_output=True,check=True,timeout=15)
    from installer.bootstrap import parse_gh_cli_version_output
    if parse_gh_cli_version_output(version.stdout).semantic_version!='2.97.0':
        raise ValueError('DEVELOPMENT_LINUX_GH_VERSION_MISMATCH')
    sha=context['subject_source_sha']
    command=[str(gh),'attestation','verify',str(probe/'release-manifest.json'),
        '--bundle',str(probe/'manifest.bundle.json'),'--custom-trusted-root',
        str(probe/'trusted-roots.jsonl'),
        '--repo','yanyuhanyue/AniMemo','--cert-identity',
        'https://github.com/yanyuhanyue/AniMemo/.github/workflows/release.yml@refs/heads/main',
        '--cert-oidc-issuer','https://token.actions.githubusercontent.com',
        '--source-ref','refs/heads/main','--source-digest',sha,'--signer-digest',sha,
        '--predicate-type','https://slsa.dev/provenance/v1','--format','json']
    completed=subprocess.run(command,env=environment,stdin=subprocess.DEVNULL,capture_output=True,timeout=90)
    if completed.returncode!=0 or not 0<len(completed.stdout)<=8*1024*1024:
        print(json.dumps({**context,'result':'FAIL','failure_code':'DEVELOPMENT_LINUX_GH_VERIFICATION_FAILED',
            'purpose':'DEVELOPMENT_ONLY','gh_returncode':completed.returncode,
            'public_gh_error':completed.stderr.decode('utf-8',errors='replace')[:4096],
            'sudo_capture_attempts':0,'formal_authority_granted':False},sort_keys=True))
        return
    output=completed.stdout.decode('utf-8')
    parsed=GitHubReleaseSource._verify_attestation_result(output,'release-manifest.json',
        'sha256:'+context['manifest_sha256'],expected_workflow='.github/workflows/release.yml',expected_source_commit=sha)
    if parsed!=(sha,sha):
        raise ValueError('DEVELOPMENT_LINUX_PARSER_MISMATCH')
    report={**context,'result':'PASS','purpose':'DEVELOPMENT_ONLY','platform':'linux/amd64',
        'gh_returncode':completed.returncode,'gh_version_output':version.stdout.decode('utf-8'),
        'gh_output_sha256':hashlib.sha256(completed.stdout).hexdigest(),'gh_output':output,
        'module_path':str(imported),'sudo_capture_attempts':0,'formal_authority_granted':False}
    print(json.dumps(report,sort_keys=True))


if __name__=='__main__':
    try:
        run()
    except Exception as error:
        print(json.dumps({'result':'FAIL','failure_code':'DEVELOPMENT_LINUX_PROBE_EXCEPTION',
            'error_type':type(error).__name__,'purpose':'DEVELOPMENT_ONLY',
            'sudo_capture_attempts':0,'formal_authority_granted':False},sort_keys=True))
