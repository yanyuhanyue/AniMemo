"""Use the fixed Guest grants to execute one independently bound development tree."""
import base64
import zlib
from pathlib import Path

from installer.development import expected_service_observation
from release.formal_windows_pretrust import hold_windows_private_file
from scripts import candidate_guest_session as c
from scripts import candidate_vm_harness as h
from scripts.development_profile_runner import validate_development_report
from scripts.development_source import require_development_source
from scripts.guest_sudo_session import ControllerFailure


def development_binding(plan):
    return dict(plan_digest=plan.plan_digest, session_id=plan.session_id,
        workload_mode='PLATFORM_DIAGNOSTIC' if plan.platform_diagnostic else 'CLEAN_PREACCEPTANCE',
        execution_source_sha=plan.execution_source_sha, execution_source_tree=plan.execution_source_tree,
        execution_inventory_digest=plan.execution_inventory_digest,
        verified_candidate_digest=plan.verified_candidate_digest,
        material_source_sha=plan.source_sha, material_source_tree=plan.source_tree,
        qualification_run_id=plan.qualification_run_id)


def _root_program(provider, plan, profile):
    authority = require_development_source(provider, plan)
    programs = []
    for name in ('candidate_diagnostics.py', 'closed_runtime_inventory.py',
                 'candidate_workload_root.py', 'development_workload_root.py'):
        held = (authority.root / 'scripts' / name).read_bytes()
        if held != (Path(__file__).resolve().parent / name).read_bytes():
            raise ControllerFailure('DEVELOPMENT_ROOT_PROGRAM_SOURCE_MISMATCH')
        programs.append(held.decode('utf-8'))
    args = dict(profile=profile.profile, input_digest=plan.candidate_input_digest,
        material_inventory_digest=provider._candidate_material_authority.tree_inventory_identity,
        execution_inventory_digest=plan.execution_inventory_digest,
        binding=development_binding(plan),
        context=c._profile_context(plan, profile, h._initial_platform_state(profile.profile)))
    operation = c._diagnostic_operation(plan, profile)
    program = ("scope={'__name__':'_animemo_fixed_development_root'}\n"
        + 'exec(compile(' + repr(programs[0]) + ",'<fixed-diagnostic>','exec'),scope)\n"
        + "diagnostic=scope['DiagnosticWriter'](1," + repr(operation) + ')\n'
        + "import os\nif os.geteuid()!=0:\n diagnostic.error('ROOT_INITIALIZATION_FAILED')\n raise SystemExit(2)\n"
        + "diagnostic.stage('ROOT_STARTED')\ntry:\n"
        + ''.join(' exec(compile(' + repr(item) + ",'<fixed-development-root>','exec'),scope)\n" for item in programs[1:])
        + " scope['run_fixed_development'](**" + repr(args) + ',diagnostic=diagnostic)\n'
        + "except BaseException:\n diagnostic.error('ROOT_EXECUTION_FAILED')\n diagnostic.exited('ROOT',2)\n raise SystemExit(2)\n"
        + "diagnostic.exited('ROOT',0)\n")
    encoded = base64.b64encode(zlib.compress(program.encode('utf-8'), 9)).decode('ascii')
    if len(encoded) > 20000:
        raise ControllerFailure('DEVELOPMENT_ROOT_PROGRAM_SIZE_INVALID')
    return 'import base64,zlib;exec(compile(zlib.decompress(base64.b64decode(' + repr(encoded) + ")), '<fixed-development-root>', 'exec'))"


class _DevelopmentWorkloadSupervisor(c._WorkloadSupervisor):
    def _program(self):
        return _root_program(self._provider, self._plan, self._profile)


def execute_development_workload(provider, plan, profile, lease, disk, snapshot,
                                 candidate_root, initial_platform_state):
    batch = c._batch(provider, plan)
    source = require_development_source(provider, plan)
    authority = provider._active_profile_authority(profile, plan)
    with hold_windows_private_file(authority.known_hosts_file):
        with batch.operation('TRANSFER', profile):
            c._continuing_connection(provider, plan, profile, lease, disk, snapshot)
            c._stage_candidate(provider, authority, plan, profile, candidate_root)
            stage = '/tmp/animemo-development-' + plan.session_id + '-' + profile.profile
            provider._ssh_checked(authority, '/usr/bin/test ! -e ' + stage + ' -a ! -L ' + stage,
                code='DEVELOPMENT_STAGE_EXISTS')
            provider._run(provider._scp_argv(authority=authority, source=str(source.root),
                destination=stage, recursive=True), code='DEVELOPMENT_STAGE_FAILED',
                timeout=60 * 60, openssh=True)
            require_development_source(provider, plan)
            c._continuing_connection(provider, plan, profile, lease, disk, snapshot)
        with batch.operation('WORKLOAD', profile):
            use = batch.issue(profile, lease, ('CANDIDATE_WORKLOAD',))
            supervisor = None
            try:
                supervisor = _DevelopmentWorkloadSupervisor(use, provider=provider, plan=plan, profile=profile,
                    lease=lease, preboot_disk_graph_digest=disk, preboot_snapshot_identity=snapshot)
                value = supervisor.execute()
                if plan.platform_diagnostic:
                    from scripts.development_platform_diagnostic import validate_platform_diagnostic_report
                    validate_platform_diagnostic_report(value, loaded=provider._candidate_material_authority.loaded,
                        expected_binding=development_binding(plan),
                        expected_context=c._profile_context(plan, profile, initial_platform_state))
                else:
                    validate_development_report(value, loaded=provider._candidate_material_authority.loaded,
                        expected_binding=development_binding(plan),
                        expected_context=c._profile_context(plan, profile, initial_platform_state),
                        expected_service_source=expected_service_observation(source.root, source.inventory_digest))
                provider._candidate_diagnostics[profile.profile]['host_receipt_parse'] = 'VALIDATED'
                batch.role_result(profile, 'CANDIDATE_WORKLOAD', value['result'])
            except c.WorkloadFailure as error:
                batch.role_result(profile, 'CANDIDATE_WORKLOAD', 'ERROR' if error.revoke_batch else 'FAIL')
                if error.revoke_batch:
                    batch.revoke('CANDIDATE_BATCH_PROFILE_FAILURE')
                raise
            except BaseException:
                provider._candidate_diagnostics.get(profile.profile, {})['host_receipt_parse'] = 'REJECTED'
                batch.role_result(profile, 'CANDIDATE_WORKLOAD', 'ERROR')
                batch.revoke('CANDIDATE_BATCH_RECEIPT_AUTHORITY_INVALID')
                raise ControllerFailure('DEVELOPMENT_WORKLOAD_VALIDATION_FAILED') from None
            finally:
                if supervisor is not None:
                    supervisor.close()
    # The single-Profile diagnostic has no remaining secret use. Finish its
    # bounded operation context first so revocation preserves the valid FAIL
    # report, then wipe the owner before the provider begins VM cleanup.
    if plan.platform_diagnostic and value['result'] == 'FAIL':
        batch.revoke('CANDIDATE_BATCH_PROFILE_FAILURE')
    return value
