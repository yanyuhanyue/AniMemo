"""Fixed, zero-secret POSIX diagnostic self-test in the new planning Guest."""
from __future__ import annotations

import base64
import hashlib
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import zlib


def run_guest(operation):
    """Use a real Formal filesystem failure and a dedicated inherited pipe."""
    if sys.platform != 'linux' or os.geteuid() == 0:
        raise ValueError('DEVELOPMENT_DIAGNOSTIC_UNPRIVILEGED_LINUX_REQUIRED')
    root = Path(__file__).resolve().parents[1]
    from installer.offline_python_runtime import install_wheel_runtime
    from scripts import candidate_diagnostics as d
    with tempfile.TemporaryDirectory(prefix='animemo-diagnostic-selftest-') as temporary:
        runtime = Path(temporary) / 'runtime'
        install_wheel_runtime(root / 'wheelhouse', runtime)
        # The authority path deliberately does not exist. No authority is
        # constructed and no production composition can be entered here.
        program = ('import sys;sys.path[:0]=' + repr([str(root), str(runtime)]) + ';'
            'from scripts.formal_profile_runner import main;'
            'from scripts.candidate_diagnostics import inherited_writer;'
            'w=inherited_writer();w.stage("RUNTIME_READY");w.stage("RUNNER_STARTED");'
            'code=main(["--authority-root",' + repr(str(Path(temporary) / 'absent-authority'))
            + ',"--profile","FORMAL_FRESH","--execute"]);'
            'w.exited("RUNTIME_RUNNER",code);raise SystemExit(code)')
        read_fd, write_fd = os.pipe()
        child = None
        try:
            child = subprocess.Popen([sys.executable, '-I', '-B', '-c', program],
                env={'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8',
                     d.FD_ENV: str(write_fd), d.OP_ENV: operation},
                pass_fds=(write_fd,), stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        finally:
            os.close(write_fd)
            if child is None:
                os.close(read_fd)
        reader = d.DiagnosticReader(operation)
        writer = d.DiagnosticWriter(1, operation)
        try:
            with os.fdopen(read_fd, 'rb') as stream:
                while frame := d.read_frame(stream):
                    reader.accept(*frame)
                    writer.frame(*frame)
            if (child.wait(timeout=30) != 2 or reader.finish() is not None
                    or reader.public()['failure_diagnostic']['status'] != 'COMPLETE'):
                raise ValueError('DEVELOPMENT_DIAGNOSTIC_SELFTEST_FAILED')
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=10)


def fixed_command(source, plan):
    """Build only the held self-test after checking the entire copied tree."""
    stage = '/tmp/animemo-diagnostic-selftest-' + plan.session_id
    operation = 'sha256:' + hashlib.sha256(
        ('DIAGNOSTIC_SELFTEST\n' + plan.plan_digest).encode('ascii')).hexdigest()
    inventory = (source.root / 'scripts/closed_runtime_inventory.py').read_text(encoding='utf-8')
    program = ("import sys;from pathlib import Path;scope={'__name__':'_fixed_inventory'};"
        + 'exec(compile(' + repr(inventory) + ",'<fixed-inventory>','exec'),scope);"
        + "assert scope['closed_runtime_inventory_digest'](Path(" + repr(stage) + '))=='
        + repr(source.inventory_digest) + ';sys.path.insert(0,' + repr(stage) + ');'
        + 'from scripts.development_diagnostic_preflight import run_guest;run_guest('
        + repr(operation) + ')')
    encoded = base64.b64encode(zlib.compress(program.encode('utf-8'), 9)).decode('ascii')
    command = '/usr/bin/python3 -I -B -c ' + shlex.quote(
        'import base64,zlib;exec(compile(zlib.decompress(base64.b64decode('
        + repr(encoded) + ")), '<fixed-diagnostic-selftest>', 'exec'))")
    return stage, operation, command


def run_before_capture(provider, plan, profile, lease, verified):
    from scripts import candidate_guest_session as c
    from scripts import candidate_vm_harness as h
    from scripts.development_source import require_development_source
    source = require_development_source(provider, plan)
    if (plan.published_subject_digest is None or len(plan.profiles) != 1
            or profile.profile != 'FRESH_BASE' or profile not in plan.profiles):
        raise h.CandidateHarnessError('DEVELOPMENT_DIAGNOSTIC_SCOPE_INVALID')
    authority = provider._active_profile_authority(profile, plan)
    if lease.authority != authority or verified.authority != authority:
        raise h.CandidateHarnessError('DEVELOPMENT_DIAGNOSTIC_TARGET_CHANGED')
    stage, operation, command = fixed_command(source, plan)
    argv = provider._ssh_argv(authority, command, bootstrap_identity=True)
    budget = c.validate_workload_command_budget((str(provider._tool_path(h.SSH)), *argv[1:]))
    record = dict(schema='animemo.development-diagnostic-selftest/v1',
        purpose='NON_AUTHORITATIVE_DEVELOPMENT', result='ERROR',
        plan_digest=plan.plan_digest, operation=operation,
        execution_inventory_digest=source.inventory_digest,
        published_subject_digest=plan.published_subject_digest,
        sudo_capture_attempts=0, formal_authority_granted=False, command_budget=budget)
    provider._profile_operation_results[profile.profile]['diagnostic_preflight'] = record
    def check_target():
        lease.require_open()
        c._batch(provider, plan).check_source()
        if provider._running_vmx_paths() != frozenset({os.path.normcase(str(authority.clone_vmx.resolve(strict=False)))}):
            raise h.CandidateHarnessError('DEVELOPMENT_DIAGNOSTIC_TARGET_CHANGED')
        current = provider._observe_guest_connection(authority,
            host_key_digest=provider._read_known_host_key(authority), bootstrap_identity=True)
        if current != verified.guest:
            raise h.CandidateHarnessError('DEVELOPMENT_DIAGNOSTIC_TARGET_CHANGED')
    check_target()
    provider._ssh_checked(authority, '/usr/bin/test ! -e ' + stage + ' -a ! -L ' + stage,
        code='DEVELOPMENT_DIAGNOSTIC_STAGE_EXISTS', bootstrap_identity=True)
    provider._run(provider._scp_argv(authority=authority, source=str(source.root), destination=stage,
        recursive=True, bootstrap_identity=True), code='DEVELOPMENT_DIAGNOSTIC_TRANSFER_FAILED',
        timeout=3600, openssh=True)
    check_target()
    if profile.profile in provider._candidate_diagnostics:
        raise h.CandidateHarnessError('DEVELOPMENT_DIAGNOSTIC_RESIDUAL')
    def exchange(process):
        try:
            c._read_receipt(process, operation=operation, provider=provider, profile=profile,
                batch=c._batch(provider, plan), timeout=90)
        except c.WorkloadFailure as error:
            if error.code != 'CANDIDATE_UNKNOWN_BEFORE_ROOT_START':
                raise
        else:
            raise h.CandidateHarnessError('DEVELOPMENT_DIAGNOSTIC_UNEXPECTED_RECEIPT')
    try:
        provider._run(argv, code='DEVELOPMENT_DIAGNOSTIC_SELFTEST_FAILED', timeout=120,
            openssh=True, guest_exchange=exchange)
    finally:
        record['diagnostic'] = provider._candidate_diagnostics.pop(profile.profile, None)
    observed = record['diagnostic']
    if (observed is None or observed['failure_diagnostic']['status'] != 'COMPLETE'
            or observed['transport_error'] is not None or observed['profile_draft_received']
            or observed['root_started'] or observed['errors'] != ['RUNNER_EXECUTION_FAILED']
            or observed['exit_codes'] != {'INSTALLER': None, 'RUNTIME_RUNNER': 2, 'ROOT': None, 'SUDO': None}
            or not any(item['kind'] == 'FAULT' and item['module'] == 'scripts.formal_profile_runner'
                       for item in observed['events'])):
        raise h.CandidateHarnessError('DEVELOPMENT_DIAGNOSTIC_SELFTEST_INVALID')
    check_target()
    record['result'] = 'PASS'
