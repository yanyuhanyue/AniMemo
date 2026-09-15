"""Formal workload delivery through the existing fixed, same-connection grants."""
import base64
from pathlib import Path
import zlib

from release.formal_windows_pretrust import hold_windows_private_file
from scripts import candidate_guest_session as c
from scripts import candidate_vm_harness as h
from scripts.development_source import HeldDevelopmentSource
from scripts.formal_plan import is_formal_plan
from scripts.guest_sudo_session import ControllerFailure


def require_formal_tool_source(provider,plan):
    source=getattr(provider,'_development_source_authority',None)
    if (not is_formal_plan(plan) or type(source) is not HeldDevelopmentSource
            or source._execution is not provider._execution
            or (source.source_sha,source.source_tree,source.inventory_digest)!=
                (plan.execution_source_sha,plan.execution_source_tree,plan.execution_inventory_digest)):
        raise ControllerFailure('FORMAL_TOOL_SOURCE_AUTHORITY_INVALID')
    source.require_open()
    return source


def _root_program(provider,plan,profile,workload):
    source=require_formal_tool_source(provider,plan)
    programs=[]
    for name in ('candidate_diagnostics.py','closed_runtime_inventory.py',
                 'candidate_workload_root.py','formal_workload_root.py'):
        held=(source.root/'scripts'/name).read_bytes()
        if held!=(Path(__file__).resolve().parent/name).read_bytes():
            raise ControllerFailure('FORMAL_ROOT_PROGRAM_SOURCE_MISMATCH')
        programs.append(held.decode('utf-8'))
    material=provider._candidate_material_authority
    args=dict(session_id=plan.session_id,profile=workload.formal_profile,
        authority_identity=workload.authority_identity,inventory_digest=workload.runtime_inventory_digest,
        archive_digest=material.loaded.candidate_input['installer_materials_sha256'],
        expected_guest_ip=h.SSH_HOST.rsplit('@',1)[-1])
    operation=c._diagnostic_operation(plan,profile)
    program=("scope={'__name__':'_animemo_fixed_formal_root'}\n"
        +'exec(compile('+repr(programs[0])+",'<fixed-diagnostic>','exec'),scope)\n"
        +"diagnostic=scope['DiagnosticWriter'](1,"+repr(operation)+')\n'
        +"import os,sys\nif os.geteuid()!=0:\n diagnostic.error('ROOT_INITIALIZATION_FAILED')\n raise SystemExit(2)\n"
        +"diagnostic.stage('ROOT_STARTED')\ntry:\n"
        +''.join(' exec(compile('+repr(item)+",'<fixed-formal-root>','exec'),scope)\n" for item in programs[1:])
        +" scope['run_fixed_formal'](ssh_flow=sys.argv[1:],**"+repr(args)+',diagnostic=diagnostic)\n'
        +"except BaseException:\n diagnostic.error('ROOT_EXECUTION_FAILED')\n diagnostic.exited('ROOT',2)\n raise SystemExit(2)\n"
        +"diagnostic.exited('ROOT',0)\n")
    encoded=base64.b64encode(zlib.compress(program.encode('utf-8'),9)).decode('ascii')
    if len(encoded)>20000:
        raise ControllerFailure('FORMAL_ROOT_PROGRAM_SIZE_INVALID')
    return 'import base64,zlib;exec(compile(zlib.decompress(base64.b64decode('+repr(encoded)+")), '<fixed-formal-root>', 'exec'))"


class _FormalWorkloadSupervisor(c._WorkloadSupervisor):
    _role='FORMAL_WORKLOAD'

    def _program(self):
        return _root_program(self._provider,self._plan,self._profile,self._workload)


def execute_formal_workload(provider,plan,profile,lease,disk,snapshot,workload):
    batch=c._batch(provider,plan)
    require_formal_tool_source(provider,plan)
    if not batch._formal:
        raise ControllerFailure('FORMAL_BATCH_REQUIRED')
    authority=provider._active_profile_authority(profile,plan)
    with hold_windows_private_file(authority.known_hosts_file):
        with batch.operation('TRANSFER',profile):
            c._continuing_connection(provider,plan,profile,lease,disk,snapshot)
            root,_=provider._validate_formal_workload(workload)
            stage='/tmp/animemo-formal-'+plan.session_id+'-'+workload.formal_profile
            provider._ssh_checked(authority,'/usr/bin/test ! -e '+stage+' -a ! -L '+stage,
                code='FORMAL_STAGE_EXISTS')
            provider._run(provider._scp_argv(authority=authority,source=str(root),destination=stage,
                recursive=True),code='FORMAL_STAGE_FAILED',timeout=60*60,openssh=True)
            provider._validate_formal_workload(workload)
            c._continuing_connection(provider,plan,profile,lease,disk,snapshot)
        with batch.operation('WORKLOAD',profile):
            use=batch.issue(profile,lease,('FORMAL_WORKLOAD',))
            supervisor=None
            try:
                supervisor=_FormalWorkloadSupervisor(use,provider=provider,plan=plan,profile=profile,
                    lease=lease,preboot_disk_graph_digest=disk,preboot_snapshot_identity=snapshot)
                supervisor._workload=workload
                receipt=supervisor.execute()
                batch.role_result(profile,'FORMAL_WORKLOAD',receipt.get('result','ERROR'))
                return receipt
            except BaseException:
                batch.role_result(profile,'FORMAL_WORKLOAD','ERROR')
                batch.revoke('CANDIDATE_WORKLOAD_AUTHORITY_OR_DELIVERY_UNCERTAIN')
                raise
            finally:
                if supervisor is not None:
                    supervisor.close()
