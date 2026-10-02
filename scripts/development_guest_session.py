"""Use the fixed Guest grants to execute one independently bound development tree."""
import ast
import hashlib
import json
from pathlib import Path

from installer.development import expected_service_observation
from release.formal_windows_pretrust import hold_windows_private_file
from scripts import candidate_guest_session as c
from scripts import candidate_vm_harness as h
from scripts.development_profile_runner import validate_development_report
from scripts.development_source import require_development_source
from scripts.guest_sudo_session import ControllerFailure


def development_binding(plan):
    value = dict(plan_digest=plan.plan_digest, session_id=plan.session_id,
        workload_mode='PLATFORM_DIAGNOSTIC' if plan.platform_diagnostic else 'CLEAN_PREACCEPTANCE',
        execution_source_sha=plan.execution_source_sha, execution_source_tree=plan.execution_source_tree,
        execution_inventory_digest=plan.execution_inventory_digest,
        verified_candidate_digest=plan.verified_candidate_digest,
        material_source_sha=plan.source_sha, material_source_tree=plan.source_tree,
        qualification_run_id=plan.qualification_run_id)
    if plan.published_subject_digest is not None:
        value.update(workload_mode='PUBLISHED_PLATFORM_PLAN',
                     published_subject_digest=plan.published_subject_digest)
    if plan.runtime_offline_only:
        value['runtime_offline_only'] = True
        value['runtime_trust_selection_digest'] = plan.runtime_trust_selection_digest
        value['candidate_input_digest'] = plan.candidate_input_digest
    return value


def _runtime_workload_binding(provider, plan, *, construction_only=False, _after_workload=False):
    from installer.development_boundary import build_runtime_operation_boundary
    from installer.development_trust import runtime_material_boundary
    from scripts.runtime_development_boundary import require_confirmed_runtime_boundary
    binding = development_binding(plan)
    if construction_only:
        from scripts.runtime_development_boundary import parse_authorization_deadline
        expires = parse_authorization_deadline(plan.runtime_authorization_deadline)
        lifetime = dict(schema='animemo.runtime-boot-deadline/v1',
            **{key: binding[key] for key in ('plan_digest', 'session_id', 'execution_source_sha', 'execution_source_tree')},
            boot_started_utc_seconds=expires - 14400, authorization_expires_utc_seconds=expires,
            effective_expires_utc_seconds=expires, issued_utc_seconds=expires - 14400,
            remaining_seconds=14400.0, stop_reserve_seconds=300)
        baseline = {'baseline_digest': 'sha256:' + '0' * 64,
            'guest_identity': {'machine_id': '0' * 32, 'boot_id': '00000000-0000-0000-0000-000000000000',
                'nonce': '0' * 64, 'clone_identity': 'sha256:' + '0' * 64, 'session_id': plan.session_id}}
        baseline['observation'] = {
            'tools': {name: {'path': path, 'resolved_path': path,
                    'sha256': 'sha256:' + hashlib.sha256(('construction-only:' + path).encode('ascii')).hexdigest()}
                for name, path in [('python', '/usr/bin/python3'), ('docker', '/usr/bin/docker'),
                    ('systemctl', '/usr/bin/systemctl'), ('dpkg', '/usr/bin/dpkg'),
                    ('dpkg-query', '/usr/bin/dpkg-query'), ('pg_dump', '/usr/bin/pg_dump'),
                    ('psql', '/usr/bin/psql'), ('compose', '/usr/libexec/docker/cli-plugins/docker-compose')]},
            'docker_socket': {'state': 'PERMISSION_UNKNOWN', 'path': '/var/run/docker.sock'},
            'docker_daemon': {'state': 'PERMISSION_UNKNOWN', 'identity': None}}
        boundary = build_runtime_operation_boundary(binding=binding, baseline=baseline,
            lifetime=lifetime, material=runtime_material_boundary(provider._runtime_development_material))
        return {**binding, 'runtime_operation_boundary': boundary, 'runtime_lifetime': lifetime}
    confirmed = require_confirmed_runtime_boundary(provider, plan, workload=_after_workload)
    existing = getattr(provider, '_runtime_workload_binding', None)
    if existing is None:
        existing = {**binding, 'runtime_operation_boundary': confirmed.body,
            'runtime_lifetime': confirmed._lifetime.guest_envelope()}
        provider._runtime_workload_binding = json.loads(json.dumps(existing))
    if ({key: value for key, value in existing.items()
            if key not in {'runtime_operation_boundary', 'runtime_lifetime'}} != binding
            or existing['runtime_operation_boundary'] != confirmed.body):
        raise ControllerFailure('RUNTIME_WORKLOAD_BINDING_CHANGED')
    return json.loads(json.dumps(existing))


def _root_support_prefix(source, stopping_name):
    """Embed the exact reviewed stdlib prefix, excluding host/trust issuers."""
    tree = ast.parse(source)
    found = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.ClassDef))
        and node.name == stopping_name]
    if len(found) != 1:
        raise ControllerFailure('DEVELOPMENT_ROOT_PROGRAM_SOURCE_MISMATCH')
    prefix = ''.join(source.splitlines(keepends=True)[:found[0].lineno - 1])
    compile(prefix, '<fixed-runtime-support>', 'exec')
    return prefix


def _root_preparation_source(source):
    """Exact source nodes for preparation only; no business authority issuer."""
    names = {'BOUNDARY_SCHEMA', '_DIGEST', '_BINDING_EXTRAS', 'DevelopmentBoundaryError',
        '_canonical', '_digest', '_base_binding', 'preparation_paths', 'require_root_preparation_binding'}
    parts, selected = [], set()
    for node in ast.parse(source).body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            parts.append(ast.get_source_segment(source, node))
        elif isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name in names:
            selected.add(node.name)
            parts.append(ast.get_source_segment(source, node))
        elif (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name) and node.targets[0].id in names):
            selected.add(node.targets[0].id)
            parts.append(ast.get_source_segment(source, node))
    if selected != names:
        raise ControllerFailure('DEVELOPMENT_ROOT_PROGRAM_SOURCE_MISMATCH')
    result = '\n\n'.join(parts) + '\n'
    compile(result, '<fixed-root-preparation>', 'exec')
    return result


def _root_program(provider, plan, profile, *, _construction_only=False):
    authority = require_development_source(provider, plan)
    programs = []
    for name in ('candidate_diagnostics.py', 'closed_runtime_inventory.py',
                 'candidate_workload_root.py', 'development_workload_root.py'):
        held = (authority.root / 'scripts' / name).read_bytes()
        if held != (Path(__file__).resolve().parent / name).read_bytes():
            raise ControllerFailure('DEVELOPMENT_ROOT_PROGRAM_SOURCE_MISMATCH')
        decoded = held.decode('utf-8')
        if plan.runtime_offline_only and name != 'development_workload_root.py':
            decoded = _root_support_prefix(decoded, {
                'candidate_diagnostics.py': 'DiagnosticReader',
                'closed_runtime_inventory.py': 'closed_runtime_cli_root',
                'candidate_workload_root.py': 'run_fixed_candidate'}[name])
        programs.append(decoded)
    context = c._profile_context(plan, profile, h._initial_platform_state(profile.profile))
    c.validate_workload_context(context)
    binding = (_runtime_workload_binding(provider, plan, construction_only=_construction_only)
        if plan.runtime_offline_only else development_binding(plan))
    args = dict(profile=profile.profile, input_digest=plan.candidate_input_digest,
        material_inventory_digest=provider._candidate_material_authority.tree_inventory_identity,
        execution_inventory_digest=plan.execution_inventory_digest,
        binding=binding,
        context=context)
    runtime_setup = ''
    runtime_arguments = ''
    if plan.runtime_offline_only:
        args['runtime_inputs_inventory_digest'] = provider._runtime_development_material.guest_inventory_digest
        extra = {}
        for relative in ('scripts/runtime_development_boundary.py', 'installer/development_boundary.py'):
            held = (authority.root / relative).read_bytes()
            if held != (Path(__file__).resolve().parents[1] / relative).read_bytes():
                raise ControllerFailure('DEVELOPMENT_ROOT_PROGRAM_SOURCE_MISMATCH')
            extra[relative] = (_root_support_prefix(held.decode('utf-8'), '_single_runtime')
                if relative.startswith('scripts/') else _root_preparation_source(held.decode('utf-8')))
        runtime_setup = (" clock_scope={'__name__':'_animemo_runtime_clock'}\n"
            + ' exec(compile(' + repr(extra['scripts/runtime_development_boundary.py'])
            + ",'<fixed-runtime-clock>','exec'),clock_scope)\n"
            + " operation_scope={'__name__':'_animemo_runtime_boundary'}\n"
            + ' exec(compile(' + repr(extra['installer/development_boundary.py'])
            + ",'<fixed-runtime-boundary>','exec'),operation_scope)\n"
            + ' fixed_arguments=' + repr(args) + '\n'
            + " runtime_deadline=clock_scope['RuntimeDeadline'].from_guest_envelope("
            + "fixed_arguments['binding']['runtime_lifetime'],binding=fixed_arguments['binding'])\n")
        runtime_arguments = ",runtime_deadline=runtime_deadline,preparation_check=operation_scope['require_root_preparation_binding']"
    operation = c._diagnostic_operation(plan, profile)
    program = ("scope={'__name__':'_animemo_fixed_development_root'}\n"
        + 'exec(compile(' + repr(programs[0]) + ",'<fixed-diagnostic>','exec'),scope)\n"
        + "diagnostic=scope['DiagnosticWriter'](1," + repr(operation) + ')\n'
        + "import os\nif os.geteuid()!=0:\n diagnostic.error('ROOT_INITIALIZATION_FAILED')\n raise SystemExit(2)\n"
        + "diagnostic.stage('ROOT_STARTED')\ntry:\n"
        + ''.join(' exec(compile(' + repr(item) + ",'<fixed-development-root>','exec'),scope)\n" for item in programs[1:])
        + runtime_setup
        + " scope['run_fixed_development'](**" + ('fixed_arguments' if plan.runtime_offline_only else repr(args))
        + ',diagnostic=diagnostic' + runtime_arguments + ')\n'
        + "except BaseException:\n diagnostic.error('ROOT_EXECUTION_FAILED')\n diagnostic.exited('ROOT',2)\n raise SystemExit(2)\n"
        + "diagnostic.exited('ROOT',0)\n")
    raw = program.encode('utf-8')
    if len(raw) + 1 > c.MAX_ROOT_PROGRAM_BYTES:
        raise ControllerFailure('DEVELOPMENT_ROOT_PROGRAM_SIZE_INVALID')
    # The fixed SSH transport already compresses its complete public program.
    # An inner base64 layer obscures repetition and can exceed CreateProcessW
    # despite the same reviewed source fitting comfortably when encoded once.
    # Include the NUL in the Linux single-argument bound as well.
    return program


def preflight_development_workload_commands(provider, plan, *, _construction_only=True):
    """Construct each real fixed command with held inputs before confirmation."""
    profiles = {}
    for profile in plan.profiles:
        program_sizes = {}
        root = _root_program(provider, plan, profile,
            _construction_only=plan.runtime_offline_only and _construction_only)
        remote = c._remote_workload_command(root, c._diagnostic_operation(plan, profile),
            runtime_offline_only=plan.runtime_offline_only, _program_sizes=program_sizes)
        program_sizes['remote_command_utf8_bytes'] = len(remote.encode('utf-8'))
        authority = provider._active_profile_authority(profile, plan)
        argv = provider._ssh_argv(authority, remote)
        try:
            budget = c.validate_workload_command_budget((str(provider._tool_path(h.SSH)), *argv[1:]),
                                                        program_sizes=program_sizes)
        except c.WorkloadCommandBudgetFailure as error:
            provider._profile_operation_results.setdefault(profile.profile, {})[
                'workload_command_budget_failure'] = error.command_budget
            raise
        profiles[profile.profile] = dict(**program_sizes, **budget)
        if plan.runtime_offline_only:
            profiles[profile.profile]['construction_only'] = _construction_only
        if plan.published_subject_digest is not None:
            from scripts.development_diagnostic_preflight import fixed_command
            source = require_development_source(provider, plan)
            _, _, command = fixed_command(source, plan)
            probe_argv = provider._ssh_argv(authority, command, bootstrap_identity=True)
            selftest_budget = c.validate_workload_command_budget(
                (str(provider._tool_path(h.SSH)), *probe_argv[1:]))
            profiles[profile.profile]['diagnostic_preflight'] = dict(selftest_budget,
                remote_command_utf8_bytes=len(command.encode('utf-8')))
    return dict(schema='animemo.development-workload-command-preflight/v1',
                profiles=profiles, secret_capture_required=False)


class _DevelopmentWorkloadSupervisor(c._WorkloadSupervisor):
    def _program(self):
        return _root_program(self._provider, self._plan, self._profile)


def execute_development_workload(provider, plan, profile, lease, disk, snapshot,
                                 candidate_root, initial_platform_state):
    batch = c._batch(provider, plan)
    source = require_development_source(provider, plan)
    authority = provider._active_profile_authority(profile, plan)
    lifetime = None
    if plan.runtime_offline_only:
        from scripts.runtime_development_boundary import require_runtime_lifetime
        lifetime = require_runtime_lifetime(provider, plan, profile)
    with hold_windows_private_file(authority.known_hosts_file):
        with batch.operation('TRANSFER', profile):
            c._continuing_connection(provider, plan, profile, lease, disk, snapshot)
            c._stage_candidate(provider, authority, plan, profile, candidate_root)
            stage = '/tmp/animemo-development-' + plan.session_id + '-' + profile.profile
            provider._ssh_checked(authority, '/usr/bin/test ! -e ' + stage + ' -a ! -L ' + stage,
                code='DEVELOPMENT_STAGE_EXISTS')
            provider._run(provider._scp_argv(authority=authority, source=str(source.root),
                destination=stage, recursive=True), code='DEVELOPMENT_STAGE_FAILED',
                timeout=lifetime.clip_timeout(60 * 60) if lifetime else 60 * 60, openssh=True)
            if plan.runtime_offline_only:
                import shlex
                material = provider._runtime_development_material
                material.verify_current()
                files = material.staging_files()
                trust_stage = '/tmp/animemo-runtime-inputs-' + plan.session_id
                provider._ssh_checked(authority, '/usr/bin/test ! -e ' + trust_stage + ' -a ! -L ' + trust_stage,
                    code='DEVELOPMENT_RUNTIME_INPUT_STAGE_EXISTS')
                directories = sorted({str(Path(name).parent).replace('\\', '/') for name in files
                    if str(Path(name).parent) != '.'})
                provider._ssh_checked(authority, '/usr/bin/mkdir -m 700 -p -- ' +
                    ' '.join(shlex.quote(trust_stage + '/' + item) for item in directories),
                    code='DEVELOPMENT_RUNTIME_INPUT_STAGE_FAILED')
                for relative, record in sorted(files.items()):
                    lifetime.check('RUNTIME_TRUST_TRANSFER')
                    provider._run(provider._scp_argv(authority=authority, source=str(record['source']),
                        destination=trust_stage + '/' + relative, recursive=False),
                        code='DEVELOPMENT_RUNTIME_INPUT_STAGE_FAILED',
                        timeout=lifetime.clip_timeout(60 * 60), openssh=True)
                material.verify_current()
            require_development_source(provider, plan)
            c._continuing_connection(provider, plan, profile, lease, disk, snapshot)
        with batch.operation('WORKLOAD', profile):
            use = batch.issue(profile, lease, ('CANDIDATE_WORKLOAD',))
            supervisor = None
            try:
                supervisor = _DevelopmentWorkloadSupervisor(use, provider=provider, plan=plan, profile=profile,
                    lease=lease, preboot_disk_graph_digest=disk, preboot_snapshot_identity=snapshot)
                value = supervisor.execute()
                if plan.published_subject_digest is not None:
                    from scripts.development_published_planning import validate_published_planning_report
                    validate_published_planning_report(value, loaded=provider._candidate_material_authority.loaded,
                        expected_binding=(_runtime_workload_binding(provider, plan, _after_workload=True)
                            if plan.runtime_offline_only else development_binding(plan)),
                        expected_context=c._profile_context(plan, profile, initial_platform_state))
                elif plan.platform_diagnostic:
                    from scripts.development_platform_diagnostic import validate_platform_diagnostic_report
                    validate_platform_diagnostic_report(value, loaded=provider._candidate_material_authority.loaded,
                        expected_binding=development_binding(plan),
                        expected_context=c._profile_context(plan, profile, initial_platform_state))
                else:
                    validate_development_report(value, loaded=provider._candidate_material_authority.loaded,
                        expected_binding=(_runtime_workload_binding(provider, plan, _after_workload=True)
                            if plan.runtime_offline_only else development_binding(plan)),
                        expected_context=c._profile_context(plan, profile, initial_platform_state),
                        expected_service_source=expected_service_observation(source.root, source.inventory_digest))
                provider._candidate_diagnostics[profile.profile]['host_receipt_parse'] = 'VALIDATED'
                batch.role_result(profile, 'CANDIDATE_WORKLOAD', value['result'])
            except c.WorkloadFailure as error:
                batch.role_result(profile, 'CANDIDATE_WORKLOAD', 'ERROR' if error.revoke_batch else 'FAIL')
                if error.revoke_batch:
                    batch.revoke('CANDIDATE_BATCH_PROFILE_FAILURE')
                raise
            except BaseException as error:
                provider._candidate_diagnostics.get(profile.profile, {})['host_receipt_parse'] = 'REJECTED'
                batch.role_result(profile, 'CANDIDATE_WORKLOAD', 'ERROR')
                batch.revoke('CANDIDATE_BATCH_RECEIPT_AUTHORITY_INVALID')
                code = str(error) if isinstance(error, ControllerFailure) else None
                if code in c.WORKLOAD_CONSTRUCTION_FAILURE_CODES:
                    raise ControllerFailure(code) from None
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
