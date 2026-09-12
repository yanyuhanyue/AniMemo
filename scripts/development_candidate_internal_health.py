"""Temporary actual M8 OCI regression. Never creates Candidate receipts."""
from __future__ import annotations
import argparse
from dataclasses import replace
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import socket
import sys
import uuid

sys.path[:0]=[str(Path(__file__).resolve().parents[1]),str(Path(__file__).resolve().parent)]
from development_candidate_health_input import command, download, save, RUN, SHA, TREE, FINAL_ID, FINAL_DIGEST

def probe(root,baseline):
 assert os.name=='posix' and os.geteuid()==0 and os.environ.get('GITHUB_ACTIONS')=='true'
 from release.candidate import load_verified_candidate
 from updater.oci import ImageAcquirer
 from updater.local_bundle import LocalBundleTransportPolicy
 from updater.deployment import HostPaths, ImmutableComposeDeployment, CANDIDATE_NETWORK_OVERRIDE_TEXT
 from installer.production import ProductionFreshInstallPort, ProductionManagedConfigurationPort, LocalDockerCommandRunner, ProductionDoctorAcceptance
 from installer.runtime import ListenRequest
 from durability.instance import instance_namespace
 from durability.managed_config import derive_runtime_environment
 result={'scope':'DEVELOPMENT_ACTUAL_M8_OCI_NOT_CANDIDATE_ACCEPTANCE','status':'ERROR','qualification_run_id':RUN,'source_sha':SHA,'capture_count':0,'vm_start_count':0,'publication_writes':0,'projection':'new host Installer ingress; unchanged actual four OCI and deployment files'}
 phase='verify_actual_q';deployment=None;fresh=None;manifest=None
 environment={'PATH':'/usr/sbin:/usr/bin:/sbin:/bin','LANG':'C.UTF-8','LC_ALL':'C.UTF-8'}
 try:
  verified=json.loads(command([sys.executable,'-B','-m','release.cli','verify-prepublication-candidate',
   '--archive',str(root/'evidence.zip'),'--run-metadata',str(root/'run.json'),
   '--jobs-metadata',str(root/'jobs.json'),'--artifacts-metadata',str(root/'artifacts.json'),
   '--containing-artifact-id',str(FINAL_ID),'--containing-artifact-api-digest',FINAL_DIGEST,
   '--expected-run-id',str(RUN),'--expected-source-sha',SHA,'--expected-source-tree',TREE,
   '--expected-candidate-version','v2.0.0-rc.1','--verified-at',datetime.now(timezone.utc).isoformat().replace('+00:00','Z')],cwd=baseline,environment=environment,timeout=900))
  loaded=load_verified_candidate(verified['verifiedCandidateDigest'])
  phase='import_actual_oci'
  runner=LocalDockerCommandRunner()
  acquired=ImageAcquirer(runner=runner,environment=environment).acquire_local(loaded.materials,loaded.images,LocalBundleTransportPolicy())
  result['actual_oci_roles']=sorted(x.role for x in acquired.images)
  manifest=loaded.materials.manifest
  name='ingress'+str(os.getpid());namespace=instance_namespace(name)
  paths=HostPaths.testing(app=root/'app',data=root/'data',state=root/'state',instance_name=name,instance_id=str(uuid.uuid4()))
  with socket.socket() as available:
   available.bind(('127.0.0.1',0)); port=available.getsockname()[1]
  paths=replace(paths,listen_port=port)
  for path in (paths.app_root/'deploy',paths.state_root,paths.runtime_root,paths.data_root):path.mkdir(parents=True,exist_ok=True)
  for name in ('plugins','media','private','logs','backups'):
   path=paths.data_root/name;path.mkdir();os.chown(path,10001,10001);path.chmod(0o700 if name=='private' else 0o770)
  shutil.copyfile(loaded.materials.material('deploy/docker-compose.yml'),paths.app_root/'deploy/docker-compose.yml')
  overlay=root/'docker-compose.runtime.yml'
  shutil.copyfile(loaded.materials.material('updater/docker-compose.runtime.yml'),overlay)
  override=paths.data_root/'private/candidate-network-isolation.yml'
  override.write_text(CANDIDATE_NETWORK_OVERRIDE_TEXT);override.chmod(0o600)
  config=ProductionManagedConfigurationPort._fresh_config(instance_id=paths.instance_id,revision=str(uuid.uuid4()),public_origin=paths.public_origin,listen=ListenRequest(host=paths.listen_host,port=paths.listen_port),insecure_http_accepted=False)
  def environment_for(config):
   value=dict(derive_runtime_environment(config,namespace=namespace,locator_digest='sha256:'+'a'*64))
   value.update(ANIMEMO_DATA_ROOT=str(paths.data_root),ANIMEMO_MANAGED_ENV_PATH=str(paths.managed_env_path),ANIMEMO_UPDATER_RUNTIME_ROOT=str(paths.runtime_root))
   paths.managed_env_path.write_text(''.join(k+'='+v+'\n' for k,v in value.items()));paths.managed_env_path.chmod(0o600)
   return value
  deployment=ImmutableComposeDeployment(paths,runner=runner,managed_environment=environment_for(config),candidate_network_override=override)
  deployment.updater_compose_file=overlay
  fresh=ProductionFreshInstallPort(releases=object(),configuration=object(),runner=runner,namespace=namespace,candidate_network_isolation=True)
  phase='real_datastores_and_schema'
  deployment.start_datastores(manifest)
  fresh._publish_candidate_edge_proxy(deployment,manifest)
  deployment.migrate(manifest);deployment.bootstrap(manifest)
  phase='real_web_and_exact_django_proxy'
  deployment.start_application(manifest)
  config=replace(config,application=replace(config.application,trusted_proxy_ips=(deployment.exact_web_proxy(manifest),)))
  deployment.refresh_binding(paths,managed_environment=environment_for(config))
  deployment.reconcile_api(manifest)
  fresh._validate_candidate_edge_proxy(deployment,manifest,'web')
  web=deployment._container_id(manifest,'web')
  requested=json.loads(deployment._inspect_container(web,'{{json .HostConfig.PortBindings}}'))
  active=json.loads(deployment._inspect_container(web,'{{json .NetworkSettings.Ports}}'))
  assert requested=={'80/tcp':[{'HostIp':'127.0.0.1','HostPort':str(port)}]}
  assert active in ({},{'80/tcp':None})
  result['requested_loopback_mapping_present']=True;result['actual_docker_published_mapping_absent']=True
  phase='red_actual_host_health'
  try:deployment.verify_health(manifest)
  except Exception as error:
   assert str(error)=='AniMemo HTTP contract failed for /health/'
   result['original_health_gate']='REPRODUCED_INITIAL_HTTP_TRANSPORT_UNAVAILABLE'
   result['original_exception_type']=type(error).__name__
  else:raise AssertionError('red did not fail')
  phase='green_real_health_and_identity'
  fresh._start_candidate_listener(deployment,manifest)
  deployment.verify_health(manifest)
  deployment.probe_postgres(manifest);deployment.probe_redis(manifest)
  deployment.probe_api(manifest);deployment.probe_web(manifest)
  result['stable_http_and_release_identity']='PASS';result['datastores_and_api_web_probes']='PASS'
  phase='real_canonical_crud_and_health'
  actual_api=deployment._container_id(manifest,'api')
  actual_name=deployment._inspect_container(actual_api,'{{.Name}}').lstrip('/')
  assert actual_name==namespace.compose_project+'-api-1'
  assert actual_name!=namespace.compose_project+'-api'
  result['old_synthesized_api_name_matches_actual']=False
  result['owned_compose_api_id_resolved']=True
  doctor=ProductionDoctorAcceptance(releases=object(),compatibility=object(),runner=runner,namespace=namespace)
  observations=doctor._canonical_acceptance(deployment,manifest)
  result['canonical_test_names']=[x['name'] for x in observations]
  fresh._validate_candidate_listener(deployment,manifest)
  phase='egress_readback_and_listener_cleanup'
  network=fresh._candidate_edge_binding['network_id']
  actual=json.loads(runner.run(['/usr/bin/docker','network','inspect',network],timeout=30).stdout)[0]
  assert actual['Internal'] is True
  routes=runner.run(['/usr/bin/docker','exec',web,'/bin/cat','/proc/net/route'],timeout=30).stdout
  assert all(line.split()[1]!='00000000' for line in routes.splitlines()[1:])
  result['internal_network_retained']=True;result['default_route_present']=False
  fresh.close_candidate_listener()
  with socket.socket() as closed:
   closed.settimeout(2);assert closed.connect_ex((paths.listen_host,port))!=0
  result['listener_closed']='PASS';result['status']='PASS'
  phase='actual_posix_lifecycle_and_root_regression'
  command([sys.executable,'-B','-m','unittest','installer.tests.test_candidate_listener','installer.tests.test_candidate_edge_proxy','installer.tests.test_candidate_input','installer.tests.test_candidate_vm_runtime_repairs','-q'],cwd=Path.cwd(),environment=environment,timeout=180)
  result['actual_posix_lifecycle_and_root_regression']='PASS'
 except BaseException as error:
  result.update(failure_stage=phase,failure_type=type(error).__name__)
  if getattr(error,'code',None) in {'INSTALL_CANONICAL_ACCEPTANCE_FAILED','INSTALL_RUNTIME_START_FAILED'}:result['failure_code']=error.code
 finally:
  cleanup=[]
  if fresh is not None:
   try:fresh.close_candidate_listener()
   except BaseException:cleanup.append('LISTENER_CLEANUP_FAILED')
  if deployment is not None and manifest is not None:
   try:deployment._compose(manifest,'down','--volumes',timeout=120)
   except BaseException:cleanup.append('COMPOSE_CLEANUP_FAILED')
  if fresh is not None:
   try:fresh.cleanup_owned_staging(None)
   except BaseException:cleanup.append('CONTROL_FILE_CLEANUP_FAILED')
  result['cleanup_errors']=cleanup
  if cleanup:result['status']='ERROR'
  save(root/'internal-health-result.json',result);print(json.dumps(result),flush=True)
 return 0 if result['status']=='PASS' else 2

if __name__=='__main__':
 parser=argparse.ArgumentParser();parser.add_argument('mode',choices=('download','probe'))
 parser.add_argument('--destination',type=Path,required=True);parser.add_argument('--baseline',type=Path)
 args=parser.parse_args()
 if args.mode=='download':download(args.destination)
 else:raise SystemExit(probe(args.destination,args.baseline))
