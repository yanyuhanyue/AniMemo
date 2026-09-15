"""One isolated Linux tool probe with no sudo capture or Installer execution."""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
import shlex
import subprocess

from scripts import candidate_vm_harness as h
from scripts.development_plan import from_material_plan
from scripts.development_source import acquire_development_source, require_material_compatibility
from scripts.guest_batch_scope import DEVELOPMENT_AUTHORIZATION
from scripts.isolated_guest_validation import _check_checkout, _controller_clone


def run(args):
    published=getattr(args,'published_subject',False)
    valid_scope=(type(args.authorization_id) is str and re.fullmatch(r'ANIMEMO_PUBLISHED_LINUX_PROBE_[A-Z0-9_]{1,96}',args.authorization_id)
        if published else args.authorization_id==DEVELOPMENT_AUTHORIZATION)
    if not valid_scope or args.output.exists() or not args.output.is_absolute():
        raise h.CandidateHarnessError('DEVELOPMENT_LINUX_PROBE_SCOPE_INVALID')
    checkout=Path(__file__).resolve().parents[1]
    sha=subprocess.check_output(['git','-C',str(checkout),'rev-parse','HEAD'],timeout=30).decode().strip()
    tree=subprocess.check_output(['git','-C',str(checkout),'rev-parse','HEAD^{tree}'],timeout=30).decode().strip()
    _check_checkout(sha,tree)
    require_material_compatibility(args.material_source_sha,sha)
    purpose='PUBLISHED_PRODUCT_PREFLIGHT_ONLY' if published else 'DEVELOPMENT_ONLY'
    result={'purpose':purpose,'operation':'LINUX_ATTESTATION_TOOL_PROBE',
        'execution_source_sha':sha,'execution_source_tree':tree,'sudo_capture_attempts':0,
        'installer_executions':0,'candidate_acceptance_authority_granted':False,'formal_authority_granted':False,
        'status':'ERROR'}
    provider=h.ClosedVmwareProvider()
    try:
        with provider.execution_authority(_retain_controller_data=True):
            with h.acquire_candidate_material_authority(args.verified_candidate_digest,provider=provider,
                _state_root=args.candidate_state) as material:
                with provider.bind_candidate_material_authority(material):
                    with acquire_development_source(provider,source_sha=sha,source_tree=tree,
                        attestation_probe_inputs={'linux_gh':args.linux_gh,'sidecar':args.sidecar,'published_subject':published}) as source:
                        base=h.build_harness_plan(verified_candidate_digest=args.verified_candidate_digest,
                            expected_qualification_run_id=args.qualification_run_id,
                            expected_source_sha=args.material_source_sha,expected_source_tree=args.material_source_tree,
                            provider=provider,_candidate_material_authority=material)
                        plan=from_material_plan(base,execution_source_sha=sha,execution_source_tree=tree,
                            execution_inventory_digest=source.inventory_digest)
                        result['plan']=plan.as_dict()
                        if published:
                            from scripts.guest_console_capture import WindowsConsoleCapture
                            WindowsConsoleCapture().confirm_batch(json.dumps(dict(
                                purpose=purpose,authorization_id=args.authorization_id,
                                sudo_capture_attempts=0,installer_executions=0,
                                qualification_run_id=args.qualification_run_id,
                                subject_source_sha=args.material_source_sha,tool_source_sha=sha,
                                plan_digest=plan.plan_digest,profile=plan.profiles[0].as_dict()),sort_keys=True))
                            result['local_tool_confirmation']='CONFIRMED_ZERO_CREDENTIAL_PROBE'
                        with _controller_clone(provider,plan,result) as (profile,lease,disk,snapshot):
                            authority=provider._active_profile_authority(profile,plan)
                            verified=provider._verify_bootstrap_connection(authority,profile,
                                preboot_disk_graph_digest=disk,preboot_snapshot_identity=snapshot)
                            stage='/tmp/animemo-linux-probe-'+plan.session_id
                            provider._ssh_checked(authority,'/usr/bin/test ! -e '+stage+' -a ! -L '+stage,
                                code='DEVELOPMENT_PROBE_STAGE_EXISTS',bootstrap_identity=True)
                            provider._run(provider._scp_argv(authority=authority,source=str(source.root),
                                destination=stage,recursive=True,bootstrap_identity=True),
                                code='DEVELOPMENT_PROBE_TRANSFER_FAILED',timeout=3600,openssh=True)
                            source.require_open()
                            inventory=(source.root/'scripts/closed_runtime_inventory.py').read_text(encoding='utf-8')
                            program=("import sys;from pathlib import Path;scope={'__name__':'_fixed_probe_inventory'};"
                                +'exec(compile('+repr(inventory)+",'<fixed-probe-inventory>','exec'),scope);"
                                +"assert scope['closed_runtime_inventory_digest'](Path("+repr(stage)+'))=='
                                +repr(source.inventory_digest)+';sys.path.insert(0,'+repr(stage)+');'
                                +'import runpy;runpy.run_path('+repr(stage+'/scripts/linux_attestation_probe.py')+",run_name='__main__')")
                            lease.require_open()
                            observed=provider._observe_guest_connection(authority,
                                host_key_digest=provider._read_known_host_key(authority),bootstrap_identity=True)
                            if observed!=verified.guest:
                                raise h.CandidateHarnessError('DEVELOPMENT_PROBE_TARGET_CHANGED')
                            completed=provider._ssh_checked(authority,
                                '/usr/bin/python3 -I -B -c '+shlex.quote(program),
                                code='DEVELOPMENT_LINUX_PROBE_FAILED',timeout=240,bootstrap_identity=True)
                            if not 0<len(completed.stdout)<=8*1024*1024:
                                raise h.CandidateHarnessError('DEVELOPMENT_LINUX_PROBE_OUTPUT_INVALID')
                            report=json.loads(completed.stdout,object_pairs_hook=h.reject_duplicate_json_keys)
                            result['linux_attestation']=report
                            if (report.get('result')!='PASS' or report.get('purpose')!=purpose
                                    or report.get('execution_source_sha')!=sha
                                    or report.get('subject_source_sha')!=args.material_source_sha
                                    or report.get('sudo_capture_attempts')!=0 or report.get('formal_authority_granted') is not False):
                                raise h.CandidateHarnessError('DEVELOPMENT_LINUX_PROBE_OUTPUT_INVALID')
                            observed=provider._observe_guest_connection(authority,
                                host_key_digest=provider._read_known_host_key(authority),bootstrap_identity=True)
                            if observed!=verified.guest:
                                raise h.CandidateHarnessError('DEVELOPMENT_PROBE_TARGET_CHANGED')
                            result['status']='PASS'
        _check_checkout(sha,tree)
    except BaseException as error:
        result.update(status='ERROR',failure_code=getattr(error,'code','DEVELOPMENT_LINUX_PROBE_FAILED'))
        raise
    finally:
        with args.output.open('x',encoding='utf-8') as stream:
            json.dump(result,stream,ensure_ascii=False,sort_keys=True)
            stream.write('\n')
    return result


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--authorization-id',required=True)
    parser.add_argument('--published-subject',action='store_true',
        help='New locally confirmed zero-credential probe importing the exact Q product, before future Formal')
    parser.add_argument('--verified-candidate-digest',required=True)
    parser.add_argument('--qualification-run-id',required=True,type=int)
    parser.add_argument('--material-source-sha',required=True)
    parser.add_argument('--material-source-tree',required=True)
    for name in ('candidate-state','linux-gh','sidecar','output'):
        parser.add_argument('--'+name,type=Path,required=True)
    args=parser.parse_args(argv)
    try:
        result=run(args)
        print(json.dumps({'status':result['status'],'sudo_capture_attempts':0}))
        return 0 if result['status']=='PASS' else 2
    except Exception as error:
        print(json.dumps({'status':'ERROR','failure_code':getattr(error,'code','DEVELOPMENT_LINUX_PROBE_FAILED')}))
        return 2


if __name__=='__main__':
    raise SystemExit(main())
