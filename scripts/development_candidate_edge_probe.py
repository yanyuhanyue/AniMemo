"""Temporary actual-Q web startup regression; no Candidate execution authority."""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import threading

REPO='yanyuhanyue/AniMemo'
RUN=34677490482
SHA='ce1ce430df7d1eff913f8d0c383c8379f3115561'
TREE='3b96601d74fbf167404edf310b279218871518af'
FINAL_ID=10293840987
FINAL_DIGEST='sha256:50348b91d8c17531a53b3414827ceb92ef41555f784fd10099a51623d6d25a80'

def command(argv, *, cwd=None, environment=None, codes=(0,), timeout=180, merge_stderr=False):
    process=subprocess.Popen(argv,cwd=cwd,env=environment,stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,stderr=subprocess.STDOUT if merge_stderr else subprocess.DEVNULL)
    timer=threading.Timer(timeout,process.kill); timer.daemon=True; timer.start()
    output=bytearray()
    try:
        while chunk:=process.stdout.read1(65536):
            if len(output)+len(chunk)>8*1024*1024: raise RuntimeError('DEVELOPMENT_OUTPUT_LIMIT')
            output.extend(chunk)
        if process.wait(timeout=10) not in codes: raise RuntimeError('DEVELOPMENT_COMMAND_FAILED')
        return bytes(output)
    finally:
        timer.cancel()
        if process.poll() is None: process.kill(); process.wait(timeout=10)
        process.stdout.close(); output[:]=bytes(len(output))

def save(path,value):
    with path.open('x',encoding='utf-8') as f: json.dump(value,f,indent=2)

def download(root):
    root.mkdir()
    def api(endpoint): return json.loads(command(['gh','api',endpoint]))
    run=api(f'repos/{REPO}/actions/runs/{RUN}')
    jobs=api(f'repos/{REPO}/actions/runs/{RUN}/attempts/1/jobs?per_page=100')
    artifacts=api(f'repos/{REPO}/actions/runs/{RUN}/artifacts?per_page=100')
    assert run['head_sha']==SHA and run['run_attempt']==1 and run['status']=='completed' and run['conclusion']=='success'
    for key,value in [('jobs',jobs),('artifacts',artifacts)]:
        assert len(value[key])==value['total_count'] and len({x['id'] for x in value[key]})==value['total_count']
    final=next(x for x in artifacts['artifacts'] if x['id']==FINAL_ID)
    assert final['digest']==FINAL_DIGEST and not final['expired']
    assert final['workflow_run']['id']==RUN and final['workflow_run']['head_sha']==SHA
    for name,value in [('run.json',run),('jobs.json',jobs),('artifacts.json',artifacts)]: save(root/name,value)
    archive=root/'evidence.zip'
    with archive.open('xb') as output:
        subprocess.run(['gh','api',f'repos/{REPO}/actions/artifacts/{FINAL_ID}/zip'],
            stdin=subprocess.DEVNULL,stdout=output,stderr=subprocess.DEVNULL,check=True,timeout=600)
    with archive.open('rb') as f: digest='sha256:'+hashlib.file_digest(f,'sha256').hexdigest()
    assert archive.stat().st_size==final['size_in_bytes'] and digest==FINAL_DIGEST
    print('ACTUAL_Q_ARCHIVE_API_AND_BYTES_VERIFIED')

def probe(root,baseline):
    assert os.name=='posix' and os.geteuid()==0 and os.environ.get('GITHUB_ACTIONS')=='true'
    source=Path.cwd()
    sys.path.insert(0,str(source))
    from release.candidate import load_verified_candidate
    from updater.oci import ImageAcquirer
    from updater.local_bundle import LocalBundleTransportPolicy
    from updater.deployment import HostPaths, ImmutableComposeDeployment, CANDIDATE_NETWORK_OVERRIDE_TEXT
    from installer.production import ProductionFreshInstallPort
    from durability.instance import instance_namespace
    result={'scope':'DEVELOPMENT_ACTUAL_Q_WEB_STARTUP_ONLY_NOT_CANDIDATE_ACCEPTANCE',
        'qualification_run_id':RUN,'source_sha':SHA,'status':'ERROR','vm_start_count':0,'capture_count':0,
        'registry_write_count':0,'projection':['read-only new nginx-entrypoint.sh mount','root-owned public IPv4 file']}
    phase='verify_actual_q'; fresh=None; compose=None; owned=[]; loaded=None
    environment={'PATH':'/usr/sbin:/usr/bin:/sbin:/bin','LANG':'C.UTF-8','LC_ALL':'C.UTF-8'}
    def docker(*args,codes=(0,),timeout=180):
        return command(['/usr/bin/docker','--host','unix:///var/run/docker.sock',*args],
                       environment=environment,codes=codes,timeout=timeout,
                       merge_stderr=bool(args and args[0]=='logs'))
    try:
        verified=json.loads(command([sys.executable,'-B','-m','release.cli','verify-prepublication-candidate',
            '--archive',str(root/'evidence.zip'),'--run-metadata',str(root/'run.json'),
            '--jobs-metadata',str(root/'jobs.json'),'--artifacts-metadata',str(root/'artifacts.json'),
            '--containing-artifact-id',str(FINAL_ID),'--containing-artifact-api-digest',FINAL_DIGEST,
            '--expected-run-id',str(RUN),'--expected-source-sha',SHA,'--expected-source-tree',TREE,
            '--expected-candidate-version','v2.0.0-rc.1','--verified-at',datetime.now(timezone.utc).isoformat().replace('+00:00','Z')],
            cwd=baseline,environment=environment,timeout=900))
        loaded=load_verified_candidate(verified['verifiedCandidateDigest'])
        phase='import_actual_oci'
        acquisition=ImageAcquirer(environment=environment).acquire_local(loaded.materials,loaded.images,LocalBundleTransportPolicy())
        images={x.role:x.canonical_reference for x in acquisition.images}
        result['actual_oci_roles']=sorted(images)
        manifest=loaded.materials.manifest
        name='edge'+str(os.getpid())
        paths=HostPaths.testing(app=root/'app',data=root/'data',state=root/'state',instance_name=name)
        for path in (paths.app_root,paths.data_root/'private',paths.state_root): path.mkdir(parents=True)
        paths.managed_env_path.write_text('ANIMEMO_INSTANCE_NAME='+name+'\n',encoding='ascii')
        override=paths.data_root/'private/candidate-network-isolation.yml'
        override.write_text(CANDIDATE_NETWORK_OVERRIDE_TEXT,encoding='ascii'); override.chmod(0o600)
        labels={'io.animemo.instance-name':name,'io.animemo.instance-id':paths.instance_id,
                'io.animemo.compose-project':paths.compose_project}
        base=root/'compose.json'; empty=root/'empty.json'; green=root/'green.json'
        base.write_text(json.dumps({'services':{
            'postgres':{'image':images['postgres'],'environment':{'POSTGRES_HOST_AUTH_METHOD':'trust'},
                'networks':{'animemo':{'aliases':['api']}},'labels':labels},
            'web':{'image':images['web'],'networks':['animemo'],'labels':labels}},
            'networks':{'animemo':{'driver':'bridge'}}}),encoding='utf-8')
        empty.write_text('{"services":{}}',encoding='ascii')
        projected=root/'nginx-entrypoint.sh'
        shutil.copyfile(source/'deploy/nginx-entrypoint.sh',projected); projected.chmod(0o555)
        result['projected_entrypoint_sha256']='sha256:'+hashlib.sha256(projected.read_bytes()).hexdigest()
        green.write_text(json.dumps({'services':{'web':{'volumes':[{'type':'bind','source':str(projected),
            'target':'/usr/local/bin/animemo-nginx-entrypoint','read_only':True,'bind':{'create_host_path':False}}]}}}),encoding='utf-8')
        compose=['compose','--project-name',paths.compose_project,'--env-file',str(paths.managed_env_path),
                 '-f',str(base),'-f',str(empty),'-f',str(override)]
        phase='create_actual_internal_network'
        docker(*compose,'up','-d','--pull','never','postgres',timeout=180)
        deployment=ImmutableComposeDeployment(paths,candidate_network_override=override,
                                               managed_environment={'ANIMEMO_INSTANCE_NAME':name})
        deployment.compose_file=base; deployment.updater_compose_file=empty
        network=paths.compose_project+'_animemo'
        def run_web(suffix, mounts):
            identifier=docker('run','-d','--pull','never','--network',network,'--name',name+suffix,
                *mounts,images['web']).decode().strip()
            assert re.fullmatch('[0-9a-f]{64}',identifier); owned.append(identifier)
            return identifier
        phase='red_original_q_web'
        red=run_web('red',[])
        assert docker('wait',red,timeout=30).strip()==b'1'
        assert b'AniMemo Web could not determine one exact IPv4 edge proxy.' in docker('logs',red)
        result['original_q_web_without_default_route']='REPRODUCED_EXIT_1_EDGE_PROXY_UNRESOLVED'
        routes=docker('run','--rm','--pull','never','--network',network,'--entrypoint','/bin/cat',images['web'],'/proc/net/route')
        assert all(line.split()[1]!=b'00000000' for line in routes.splitlines()[1:])
        result['container_default_route_present']=False
        phase='bind_real_owned_gateway'
        fresh=ProductionFreshInstallPort(releases=object(),configuration=object(),
                                         namespace=instance_namespace(name),candidate_network_isolation=True)
        fresh._publish_candidate_edge_proxy(deployment,manifest)
        gateway=fresh._candidate_edge_binding['gateway']
        result['binding']=dict(fresh._candidate_edge_binding)
        phase='green_projected_q_web'
        docker(*compose,'-f',str(green),'up','-d','--no-deps','--pull','never','web',timeout=120)
        web=docker(*compose,'ps','-q','web').decode().strip()
        assert re.fullmatch('[0-9a-f]{64}',web)
        fresh._validate_candidate_edge_proxy(deployment,manifest,'web')
        docker('exec',web,'/bin/sh','-c','wget -q -O /dev/null http://127.0.0.1/')
        config=docker('exec',web,'/bin/cat','/etc/nginx/conf.d/default.conf')
        assert b'__ANIMEMO_TRUSTED_EDGE_PROXY_CIDR__' not in config
        assert config.count((gateway+'/32').encode())==2
        assert b'set_real_ip_from 127.0.0.1/32;' in config
        result['projected_q_web_static_http']='PASS'
        result['exact_gateway_32_only']=True
        docker(*compose,'stop','web')
        phase='reject_malformed_bound_files'
        authority=fresh._candidate_edge_path()
        failures=[]
        for index,value in enumerate((b'1.1.1.1\n2.2.2.2\n',b'172.30.0.1/24\n',b'172.30.0.1\0\n')):
            authority.chmod(0o600); authority.write_bytes(value); authority.chmod(0o444)
            item=run_web('invalid'+str(index),['--mount','type=bind,src='+str(projected)+',dst=/usr/local/bin/animemo-nginx-entrypoint,readonly',
                '--mount','type=bind,src='+str(authority)+',dst=/etc/nginx/animemo/candidate-edge-proxy-ipv4,readonly'])
            assert docker('wait',item,timeout=30).strip()==b'1'
            logs=docker('logs',item)
            assert (b'AniMemo Web candidate edge proxy authority is invalid.' in logs
                    or b'AniMemo Web edge proxy identity is invalid.' in logs)
            failures.append('REJECTED_WITHOUT_ROUTE_FALLBACK')
        result['malformed_file_cases']=failures
        phase='root_file_boundary_tests'
        command([sys.executable,'-B','-m','unittest','installer.tests.test_candidate_edge_proxy'],cwd=source,environment=environment)
        result['root_file_and_network_tests']='PASS'
        result['status']='PASS'
    except BaseException as error:
        result.update(failure_stage=phase,failure_type=type(error).__name__)
    finally:
        cleanup=[]
        for identifier in owned:
            try: docker('rm','-f',identifier)
            except BaseException: cleanup.append('OWNED_CONTAINER_CLEANUP_FAILED')
        if compose is not None:
            try: docker(*compose,'down','--volumes',timeout=120)
            except BaseException: cleanup.append('OWNED_COMPOSE_CLEANUP_FAILED')
        if fresh is not None:
            try: fresh.cleanup_owned_staging(None)
            except BaseException: cleanup.append('OWNED_CONTROL_FILE_CLEANUP_FAILED')
        result['cleanup_errors']=cleanup
        if cleanup: result['status']='ERROR'
        save(root/'edge-probe-result.json',result)
        print(json.dumps(result),flush=True)
    return 0 if result['status']=='PASS' else 2

if __name__=='__main__':
    parser=argparse.ArgumentParser(); parser.add_argument('mode',choices=('download','probe'))
    parser.add_argument('--destination',type=Path,required=True); parser.add_argument('--baseline',type=Path)
    args=parser.parse_args()
    if args.mode=='download': download(args.destination)
    else: raise SystemExit(probe(args.destination,args.baseline))
