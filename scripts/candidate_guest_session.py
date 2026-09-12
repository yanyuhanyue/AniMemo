"""Candidate's fixed batch capability and workload chain.

Only the canonical Provider calls this module inside its held execution and
Profile lease. Neither commands nor secrets are accepted from a CLI or file.
"""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import queue
import shlex
import threading
import time
import zlib
from dataclasses import dataclass

from release.formal_windows_pretrust import (
    hold_windows_private_file,
)
from scripts import candidate_vm_harness as h
from scripts.guest_sudo_session import SessionSupervisor, ControllerFailure, _Grant, _read_observation
from scripts.isolated_guest_validation import _check_checkout
from scripts import candidate_diagnostics as diagnostics


from scripts.candidate_batch_session import BatchUse, CandidateBatch, AUTHORIZATION as CAPTURE_AUTHORIZATION
MAX_RECEIPT_BYTES = 8 * 1024 * 1024
WORKLOAD_SECONDS = 4 * 60 * 60 + 60 * 60


@dataclass(frozen=True)
class _IssuedConnection:
    execution: object
    plan_digest: str
    lease: object
    verified: h.VerifiedCloneConnection

    def __reduce__(self):
        raise TypeError('Candidate connection authority cannot be serialized')


def _batch(provider, plan):
    batch = provider._candidate_batch
    if type(batch) is not CandidateBatch or batch.provider is not provider or batch.plan is not plan:
        raise ControllerFailure('CANDIDATE_BATCH_REQUIRED')
    return batch


def bootstrap_candidate(provider, plan, profile, lease, disk, snapshot):
    batch = _batch(provider, plan)
    authority = provider._active_profile_authority(profile, plan)
    with batch.operation('BOOTSTRAP', profile):
        provider._verify_bootstrap_connection(authority, profile,
            preboot_disk_graph_digest=disk, preboot_snapshot_identity=snapshot)
        provider._remove_known_hosts(authority)
        batch.capture_after_bootstrap_observation(profile)
        use = batch.issue(profile, lease, ('BOOTSTRAP_ROTATION', 'VERIFIED_SUDO'))
        supervisor = None
        role = 'BOOTSTRAP_ROTATION'
        try:
            supervisor = SessionSupervisor(use, provider=provider, plan=plan, profile=profile,
                lease=lease, preboot_disk_graph_digest=disk, preboot_snapshot_identity=snapshot)
            supervisor.bootstrap_rotation()
            batch.role_result(profile, role, 'PASS')
            role = 'VERIFIED_SUDO'
            supervisor.validate_verified_guest()
            batch.role_result(profile, role, 'PASS')
            issued = _IssuedConnection(provider._execution, plan.plan_digest, lease, supervisor._verified)
            provider._candidate_connections[profile.profile] = issued
            return issued.verified
        except BaseException:
            batch.role_result(profile, role, 'ERROR')
            batch.revoke('CANDIDATE_BOOTSTRAP_OR_AUTHENTICATION_UNCERTAIN')
            raise
        finally:
            if supervisor is not None:
                supervisor.close()


def _continuing_connection(provider, plan, profile, lease, disk, snapshot, observation=None):
    provider._require_active_execution_authority()
    issued = provider._candidate_connections.get(profile.profile)
    if (type(issued) is not _IssuedConnection or issued.execution is not provider._execution
            or issued.plan_digest != plan.plan_digest or issued.lease is not lease
            or h.sha256_bytes(h.canonical_json_bytes(plan.identity_body())) != plan.plan_digest):
        raise ControllerFailure('CANDIDATE_CONTINUATION_AUTHORITY_INVALID')
    lease.require_open()
    authority = provider._active_profile_authority(profile, plan)
    if authority != issued.verified.authority or lease.authority != authority:
        raise ControllerFailure('CANDIDATE_CONTINUATION_AUTHORITY_INVALID')
    expected = os.path.normcase(str(authority.clone_vmx.resolve(strict=False)))
    if provider._running_vmx_paths() != frozenset({expected}):
        raise ControllerFailure('GUEST_VM_NAMESPACE_MISMATCH')
    runtime = provider._read_clone_runtime_identity(authority, profile,
        expected_ip=provider._wait_for_guest_ip(authority.clone_vmx),
        preboot_disk_graph_digest=disk, preboot_snapshot_identity=snapshot)
    host_key = provider._read_known_host_key(authority)
    guest = observation or provider._observe_guest_connection(authority, host_key_digest=host_key)
    if (runtime != issued.verified.runtime or guest != issued.verified.guest
            or host_key != guest.host_key_digest or host_key not in provider._accepted_host_key_digests):
        raise ControllerFailure('CANDIDATE_CONTINUATION_IDENTITY_CHANGED')
    return issued.verified


def _stage_candidate(provider, authority, plan, profile, candidate_root):
    material = provider._candidate_material_authority
    if (type(material) is not h.HeldCandidateMaterialAuthority
            or material.loaded.root.resolve(strict=True) != Path(candidate_root).resolve(strict=True)
            or material.loaded.verified['candidate_input_sha256'] != plan.candidate_input_digest
            or h._closed_runtime_inventory_digest(material.loaded.root) != material.tree_inventory_identity):
        raise ControllerFailure('CANDIDATE_MATERIAL_AUTHORITY_INVALID')
    stage = '/tmp/animemo-candidate-' + plan.session_id + '-' + profile.profile
    # scp to a nonexistent, exactly scoped path; never remove an existing tree.
    provider._ssh_checked(authority, '/usr/bin/test ! -e ' + stage + ' -a ! -L ' + stage,
                          code='CANDIDATE_VM_STAGE_EXISTS')
    provider._run(provider._scp_argv(authority=authority, source=str(candidate_root),
        destination=stage, recursive=True), code='CANDIDATE_VM_STAGE_FAILED',
        timeout=60 * 60, openssh=True)


def _root_program(provider, plan, profile, initial_platform_state):
    material = provider._candidate_material_authority
    loaded = material.loaded
    source = Path(__file__).resolve().parent
    programs = []
    for name in ('candidate_diagnostics.py', 'closed_runtime_inventory.py', 'candidate_workload_root.py'):
        reviewed = (source / name).read_bytes().replace(b'\r\n', b'\n')
        trusted = (loaded.root / 'installer-root' / 'scripts' / name).read_bytes()
        if trusted != reviewed:
            raise ControllerFailure('CANDIDATE_ROOT_PROGRAM_SOURCE_MISMATCH')
        programs.append(trusted.decode('utf-8'))
    context = dict(base_vm_identity=plan.source_vm_digest, clone_identity=profile.clone_identity,
        initial_platform_state=dict(initial_platform_state), original_vm_pre_hashes=dict(plan.original_vm_hashes),
        profile=profile.profile, snapshot_disk_graph_identity=profile.snapshot_disk_graph_identity,
        snapshot_identity=profile.snapshot_identity, source_disk_graph_identity=plan.source_disk_graph_identity,
        source_vm_inventory_identity=plan.source_vm_inventory_identity)
    args = dict(session_id=plan.session_id, profile=profile.profile, input_digest=plan.candidate_input_digest,
        verified_digest=plan.verified_candidate_digest, inventory_digest=material.tree_inventory_identity,
        context=context)
    operation = _diagnostic_operation(plan, profile)
    program = ("scope={'__name__':'_animemo_fixed_root'}\n"
        + 'exec(compile(' + repr(programs[0]) + ",'<fixed-diagnostic>','exec'),scope)\n"
        + "diagnostic=scope['DiagnosticWriter'](1," + repr(operation) + ")\n"
        + "import os\nif os.geteuid()!=0:\n diagnostic.error('ROOT_INITIALIZATION_FAILED')\n raise SystemExit(2)\n"
        + "diagnostic.stage('ROOT_STARTED')\ntry:\n"
        + ''.join(' exec(compile(' + repr(item) + ",'<fixed-candidate-root>','exec'),scope)\n" for item in programs[1:])
        + " scope['run_fixed_candidate'](**" + repr(args) + ',diagnostic=diagnostic)\n'
        + "except BaseException:\n diagnostic.error('ROOT_EXECUTION_FAILED')\n diagnostic.exited('ROOT',2)\n raise SystemExit(2)\n"
        + "diagnostic.exited('ROOT',0)\n")
    encoded = base64.b64encode(zlib.compress(program.encode('utf-8'), level=9)).decode('ascii')
    if len(encoded) > 20000:
        raise ControllerFailure('CANDIDATE_ROOT_PROGRAM_SIZE_INVALID')
    return 'import base64,zlib;exec(compile(zlib.decompress(base64.b64decode(' + repr(encoded) + ")), '<fixed-candidate-root>', 'exec'))"


def _diagnostic_operation(plan, profile):
    return h.sha256_bytes(h.canonical_json_bytes(dict(plan_digest=plan.plan_digest,
        source_sha=plan.source_sha, source_tree=plan.source_tree,
        qualification_run_id=plan.qualification_run_id, verified_candidate_digest=plan.verified_candidate_digest,
        profile=profile.profile, session_id=plan.session_id)))


def _remote_workload_command(root_program, operation):
    # Only public identity and a bounded receipt reach stdout. The mutable
    # password is wiped immediately after one forwarding write, before wait.
    from scripts.guest_sudo_session import _REMOTE_OBSERVE
    observe = _REMOTE_OBSERVE
    argv = ['/usr/bin/sudo', '-S', '-k', '-p', '', '--', '/usr/bin/python3', '-I', '-B', '-c', root_program]
    diagnostic_source = Path(diagnostics.__file__).read_text(encoding='utf-8')
    encoded = base64.b64encode(zlib.compress(diagnostic_source.encode(), 9)).decode('ascii')
    setup = ("\nimport base64,zlib\nscope={'__name__':'_animemo_diagnostic'}\n"
        + 'exec(compile(zlib.decompress(base64.b64decode(' + repr(encoded)
        + ")), '<fixed-diagnostic>', 'exec'),scope)\n"
        + "diagnostic=scope['DiagnosticWriter'](1," + repr(operation) + ')\n'
        + "reader=scope['DiagnosticReader'](" + repr(operation) + ")\n"
        + "diagnostic.stage('SSH_OBSERVED')\n")
    program = observe + setup + '''password=bytearray()
child=None
try:
    while len(password)<4098:
        one=bytearray(1)
        try:
            count=sys.stdin.buffer.raw.readinto(one)
            if count!=1: raise ValueError('CANDIDATE_SECRET_INPUT_INVALID')
            password.extend(one)
            if one[0]==10: break
        finally:
            one[:]=b'\\0'*len(one)
    if not 1<len(password)<=4097 or password[-1]!=10: raise ValueError('CANDIDATE_SECRET_INPUT_INVALID')
    child=subprocess.Popen(''' + repr(argv) + ''',stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,bufsize=0)
    diagnostic.stage('SUDO_STARTED')
    written=child.stdin.write(password)
    if written!=len(password): raise ValueError('CANDIDATE_SECRET_SHORT_WRITE')
    child.stdin.flush()
    child.stdin.close()
finally:
    password[:]=b'\\0'*len(password)
    password.clear()
try:
    while True:
        frame=scope['read_frame'](child.stdout)
        if frame is None: break
        kind,body=frame
        reader.accept(kind,body)
        diagnostic.frame(kind,body)
except BaseException:
    diagnostic.error('TRANSPORT_PROTOCOL_INVALID')
    if child.poll() is None: child.kill()
    child.wait()
    raise SystemExit(2)
code=child.wait()
if not reader.public()['root_started']:
    diagnostic.error('UNKNOWN_BEFORE_ROOT_START')
diagnostic.exited('SUDO',code)
sys.exit(code)
'''
    return '/usr/bin/python3 -I -B -c ' + shlex.quote(program)


class WorkloadFailure(ControllerFailure):
    def __init__(self, code, *, revoke_batch=True):
        self.code, self.revoke_batch = code, revoke_batch
        super().__init__(code)


def _read_receipt(process, *, operation, provider, profile, batch=None, timeout=WORKLOAD_SECONDS):
    reader = diagnostics.DiagnosticReader(operation)
    results = queue.Queue(maxsize=1)
    def read():
        try:
            while True:
                frame = diagnostics.read_frame(process.stdout)
                if frame is None:
                    break
                reader.accept(*frame)
            results.put(None)
        except diagnostics.DiagnosticError as error:
            results.put(error.code)
        except BaseException:
            results.put('TRANSPORT_INTERRUPTED')
    threading.Thread(target=read, daemon=True).start()
    deadline = time.monotonic() + timeout
    error = None
    while True:
        if batch is not None and batch.cancelled.is_set():
            error = 'TRANSPORT_INTERRUPTED'
            break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            error = 'WORKLOAD_TIMEOUT'
            break
        try:
            error = results.get(timeout=min(0.25, remaining))
            break
        except queue.Empty:
            continue
    reader.error_code = error
    observed = reader.public()
    provider._candidate_diagnostics[profile.profile] = observed
    if error:
        raise WorkloadFailure('CANDIDATE_' + error)
    exits = observed['exit_codes']
    stages = {event['stage'] for event in observed['events'] if event['kind'] == 'STAGE'}
    if not observed['root_started']:
        raise WorkloadFailure('CANDIDATE_UNKNOWN_BEFORE_ROOT_START')
    if reader.receipt is None:
        known_business_failure = (exits['SUDO'] not in (None, 0)
            and exits['ROOT'] not in (None, 0) and exits['RUNTIME_RUNNER'] not in (None, 0)
            and exits['INSTALLER'] not in (None, 0)
            and 'PLATFORM_PREPARING' in stages
            and bool(set(observed['errors']) & {'PLATFORM_PREPARATION_FAILED', 'INSTALLER_EXECUTION_FAILED'}))
        if known_business_failure:
            raise WorkloadFailure('CANDIDATE_INSTALLER_REPORTED_FAILURE', revoke_batch=False)
        raise WorkloadFailure('CANDIDATE_SHARED_WORKLOAD_STARTUP_OR_RECEIPT_FAILURE')
    if (observed['errors'] or any(exits[component] != 0 for component in diagnostics.COMPONENTS)
            or not {'DRAFT_WRITTEN', 'DRAFT_RETURNED', 'RUNNER_STARTED', 'RUNTIME_READY'}.issubset(stages)):
        raise WorkloadFailure('CANDIDATE_WORKLOAD_RECEIPT_DIAGNOSTIC_CONFLICT')
    return reader.receipt


class _WorkloadSupervisor(SessionSupervisor):
    def __init__(self, use, **kwargs):
        if type(use) is not BatchUse:
            raise ControllerFailure('CANDIDATE_BATCH_USE_REQUIRED')
        super().__init__(use, **kwargs)
        self._delivery_attempts = {'CANDIDATE_WORKLOAD': 0}
        self._delivery_completed = {'CANDIDATE_WORKLOAD': 0}
        self._state = 'CANDIDATE_WORKLOAD'

    def _exchange_workload(self, process):
        try:
            with self._lock:
                self._active()
                if self._grant is not None:
                    raise ControllerFailure('GUEST_GRANT_REPLAY')
                value = _read_observation(process, min(30, self._expires - self._clock()))
                observation = h.GuestConnectionObservation(**{**value,
                    'mac_addresses': tuple(value['mac_addresses']),
                    'host_key_digest': self._provider._read_known_host_key(self._authority)})
                _continuing_connection(self._provider, self._plan, self._profile, self._lease,
                    self._preboot_disk, self._preboot_snapshot, observation)
                _check_checkout(self._plan.source_sha, self._plan.source_tree)
                self._grant = _Grant(self, self._execution, process, 'CANDIDATE_WORKLOAD',
                    min(self._expires, self._clock() + 5))
                self._consume(self._grant, process, 'CANDIDATE_WORKLOAD')
                self._state = 'DELIVERED'
        finally:
            self._clear_secret()
        # No secret or reusable grant is needed while the fixed process runs.
        self.receipt = _read_receipt(process, operation=_diagnostic_operation(self._plan, self._profile),
            provider=self._provider, profile=self._profile,
            batch=self._batch_use._batch,
            timeout=self._workload_deadline - time.monotonic())

    def execute(self):
        command = _remote_workload_command(_root_program(self._provider, self._plan, self._profile,
            h._initial_platform_state(self._profile.profile)), _diagnostic_operation(self._plan, self._profile))
        try:
            self._workload_deadline = time.monotonic() + WORKLOAD_SECONDS
            with hold_windows_private_file(self._authority.known_hosts_file):
                self._provider._run(self._provider._ssh_argv(self._authority, command),
                    code='CANDIDATE_VM_PROFILE_EXECUTION_FAILED', timeout=WORKLOAD_SECONDS,
                    openssh=True, guest_exchange=self._exchange_workload)
            if self._delivery_completed['CANDIDATE_WORKLOAD'] != 1:
                raise ControllerFailure('CANDIDATE_WORKLOAD_NOT_DELIVERED')
            return self.receipt
        except WorkloadFailure as error:
            self.close(failed=error.revoke_batch)
            raise
        except BaseException:
            self.close(failed=True)
            raise


def execute_candidate_workload(provider, plan, profile, lease, disk, snapshot,
                               candidate_root, initial_platform_state):
    batch = _batch(provider, plan)
    authority = provider._active_profile_authority(profile, plan)
    with hold_windows_private_file(authority.known_hosts_file):
        with batch.operation('TRANSFER', profile):
            _continuing_connection(provider, plan, profile, lease, disk, snapshot)
            _stage_candidate(provider, authority, plan, profile, candidate_root)
            _continuing_connection(provider, plan, profile, lease, disk, snapshot)
        with batch.operation('WORKLOAD', profile):
            use = batch.issue(profile, lease, ('CANDIDATE_WORKLOAD',))
            supervisor = None
            try:
                supervisor = _WorkloadSupervisor(use, provider=provider, plan=plan, profile=profile,
                    lease=lease, preboot_disk_graph_digest=disk, preboot_snapshot_identity=snapshot)
                receipt = supervisor.execute()
                batch.role_result(profile, 'CANDIDATE_WORKLOAD', receipt.get('result', 'ERROR'))
                return receipt
            except WorkloadFailure as error:
                batch.role_result(profile, 'CANDIDATE_WORKLOAD', 'FAIL' if not error.revoke_batch else 'ERROR')
                raise
            except BaseException:
                batch.role_result(profile, 'CANDIDATE_WORKLOAD', 'ERROR')
                batch.revoke('CANDIDATE_WORKLOAD_AUTHORITY_OR_DELIVERY_UNCERTAIN')
                raise
            finally:
                if supervisor is not None:
                    supervisor.close()
