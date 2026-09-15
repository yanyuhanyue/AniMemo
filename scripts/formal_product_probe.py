"""Real fixed gh output through the actual published product module, before VM."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

from release.candidate import reject_duplicate_json_keys
from release.formal_vm_controller import FormalProducerError


def probe_published_product(*,loaded,windows_gh,publication_root,output_root):
    from release.formal_windows_pretrust import hold_windows_private_file
    from scripts.published_formal_entry import WINDOWS_GH_SHA256
    if hashlib.sha256(Path(windows_gh).read_bytes()).hexdigest()!=WINDOWS_GH_SHA256:
        raise FormalProducerError('FORMAL_WINDOWS_GH_IDENTITY_MISMATCH')
    product_root=loaded.root/'installer-root'
    module=loaded.materials.material('updater/source.py')
    module_digest=hashlib.sha256(module.read_bytes()).hexdigest()
    candidate=loaded.candidate_input
    inputs=dict(product_root=str(product_root),module_digest=module_digest,
        source_sha=candidate['source_sha'],gh=str(windows_gh),
        manifest=str(Path(publication_root)/'release-manifest.json'),
        bundle=str(Path(publication_root)/'release-manifest.bundle.json'),
        output=str(Path(output_root)/'product-preflight-gh-output.json'))
    program=r'''
import hashlib,inspect,json,os,subprocess,sys
from pathlib import Path
inputs=INPUTS
root=Path(inputs['product_root']).resolve(strict=True)
sys.path.insert(0,str(root))
from updater.source import GitHubReleaseSource
module=Path(sys.modules['updater.source'].__file__).resolve(strict=True)
assert module==root/'updater/source.py'
assert hashlib.sha256(module.read_bytes()).hexdigest()==inputs['module_digest']
sha=inputs['source_sha']
manifest=Path(inputs['manifest'])
digest='sha256:'+hashlib.sha256(manifest.read_bytes()).hexdigest()
command=[inputs['gh'],'attestation','verify',str(manifest),'--bundle',inputs['bundle'],
 '--repo','yanyuhanyue/AniMemo','--cert-identity',
 'https://github.com/yanyuhanyue/AniMemo/.github/workflows/release.yml@refs/heads/main',
 '--cert-oidc-issuer','https://token.actions.githubusercontent.com','--source-ref','refs/heads/main',
 '--source-digest',sha,'--signer-digest',sha,'--predicate-type','https://slsa.dev/provenance/v1','--format','json']
completed=subprocess.run(command,stdin=subprocess.DEVNULL,capture_output=True,timeout=90)
report={'scope':'PUBLISHED_PRODUCT_PREFLIGHT_ONLY','platform':sys.platform,'gh_returncode':completed.returncode,
 'module_sha256':inputs['module_digest'],'source_sha':sha,'parser_result':'NOT_RUN'}
if completed.returncode==0 and 0<len(completed.stdout)<=8*1024*1024:
 with Path(inputs['output']).open('xb') as stream:stream.write(completed.stdout)
 report['gh_output_sha256']=hashlib.sha256(completed.stdout).hexdigest()
 try:
  parse=GitHubReleaseSource._verify_attestation_result
  options=({'expected_workflow':'.github/workflows/release.yml','expected_source_commit':sha}
   if 'expected_workflow' in inspect.signature(parse).parameters else {})
  parsed=parse(completed.stdout.decode('utf-8'),'release-manifest.json',digest,**options)
  assert parsed==(sha,sha)
  report['parser_result']='PASS'
 except Exception:
  report['parser_result']='REJECTED'
assert hashlib.sha256(module.read_bytes()).hexdigest()==inputs['module_digest']
print(json.dumps(report,sort_keys=True))
'''.replace('INPUTS',repr(inputs),1)
    environment={name:os.environ[name] for name in ('SYSTEMROOT','TEMP','TMP') if name in os.environ}
    environment.update({key:value for key,value in os.environ.items()
        if key.lower() in {'http_proxy','https_proxy','all_proxy','no_proxy'}})
    environment.update(PATH=str(Path(windows_gh).parent),LANG='C.UTF-8',LC_ALL='C.UTF-8',
        GH_PROMPT_DISABLED='1',GODEBUG='http2client=0')
    for name in ('HOME','GH_CONFIG_DIR','DOCKER_CONFIG'):
        directory=Path(output_root)/('product-probe-'+name.lower())
        directory.mkdir(mode=0o700)
        environment[name]=str(directory)
    environment['USERPROFILE']=environment['HOME']
    with hold_windows_private_file(Path(windows_gh)):
        if hashlib.sha256(Path(windows_gh).read_bytes()).hexdigest()!=WINDOWS_GH_SHA256:
            raise FormalProducerError('FORMAL_WINDOWS_GH_IDENTITY_MISMATCH')
        completed=subprocess.run([sys.executable,'-I','-B','-c',program],env=environment,cwd=output_root,
            stdin=subprocess.DEVNULL,capture_output=True,timeout=120)
    if completed.returncode!=0 or not 0<len(completed.stdout)<64*1024:
        raise FormalProducerError('FORMAL_PRODUCT_PROBE_EXECUTION_FAILED')
    try:
        report=json.loads(completed.stdout,object_pairs_hook=reject_duplicate_json_keys)
    except (ValueError,UnicodeError) as error:
        raise FormalProducerError('FORMAL_PRODUCT_PROBE_OUTPUT_INVALID') from error
    if (report.get('module_sha256')!=module_digest or report.get('source_sha')!=candidate['source_sha']
            or report.get('scope')!='PUBLISHED_PRODUCT_PREFLIGHT_ONLY'
            or hashlib.sha256(module.read_bytes()).hexdigest()!=module_digest):
        raise FormalProducerError('FORMAL_PRODUCT_PROBE_IDENTITY_CHANGED')
    return report
