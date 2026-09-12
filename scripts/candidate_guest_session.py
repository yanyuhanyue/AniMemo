"""Candidate's fixed two-purpose native credential and workload chain.

Only the canonical Provider calls this module inside its held execution and
Profile lease. Neither commands nor secrets are accepted from a CLI or file.
"""
from __future__ import annotations

import hashlib
import base64
import json
import os
from pathlib import Path
import queue
import shlex
import threading
import time
import zlib
from contextlib import contextmanager
from dataclasses import dataclass

from release.formal_windows_pretrust import (
    create_windows_private_named_directory, hold_windows_private_file,
    hold_windows_private_path_chain, hold_windows_private_path_authority,
    hold_windows_private_descendant_path,
)
from scripts import candidate_vm_harness as h
from scripts.guest_console_capture import WindowsConsoleCapture, ConsoleCaptureError
from scripts.guest_sudo_session import SessionSupervisor, ControllerFailure, _Grant, _read_observation, wipe
from scripts.isolated_guest_validation import _check_checkout


CAPTURE_AUTHORIZATION = 'ANIMEMO_V2_EXACT_CANDIDATE_ACCEPTANCE_V1'
CAPTURE_LEDGER = Path('E:/') / hashlib.sha256(CAPTURE_AUTHORIZATION.encode('ascii')).hexdigest()
PURPOSES = ('SESSION_BOOTSTRAP', 'CANDIDATE_WORKLOAD')
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


def _reserve_capture(profile, purpose):
    if profile not in h.PROFILES or purpose not in PURPOSES:
        raise ControllerFailure('CANDIDATE_CAPTURE_SCOPE_INVALID')
    # Parent creation may race; the final fixed purpose directory must be new.
    # A directory alone means attempted, including crashes before any result.
    if not CAPTURE_LEDGER.exists() and not CAPTURE_LEDGER.is_symlink():
        create_windows_private_named_directory(CAPTURE_LEDGER.parent, name=CAPTURE_LEDGER.name)
    with hold_windows_private_path_authority(CAPTURE_LEDGER) as root_authority:
        profile_name = hashlib.sha256(profile.encode('ascii')).hexdigest()
        parent = CAPTURE_LEDGER / profile_name
        if not parent.exists() and not parent.is_symlink():
            create_windows_private_named_directory(CAPTURE_LEDGER, name=profile_name)
        with hold_windows_private_descendant_path(root_authority, parent):
            try:
                return create_windows_private_named_directory(parent,
                    name=hashlib.sha256(purpose.encode('ascii')).hexdigest())
            except Exception:
                raise ControllerFailure('CANDIDATE_CAPTURE_ALREADY_ATTEMPTED') from None


@contextmanager
def _capture(provider, plan, profile, purpose):
    console = WindowsConsoleCapture()
    _check_checkout(plan.source_sha, plan.source_tree)
    console.preflight()
    slot = _reserve_capture(profile.profile, purpose)
    record = dict(capture_attempts=1, capture_completed=0, delivery_attempts={},
                  delivery_completed={}, operation_result='ERROR', secret_cleanup='PENDING')
    record.update(authorization_id=CAPTURE_AUTHORIZATION, profile=profile.profile, purpose=purpose)
    record['binding'] = {key: getattr(plan, key) for key in (
        'plan_digest', 'session_id', 'source_sha', 'source_tree', 'qualification_run_id',
        'candidate_input_digest', 'verified_candidate_digest')}
    roles = ('BOOTSTRAP_ROTATION', 'VERIFIED_SUDO') if purpose == 'SESSION_BOOTSTRAP' else ('CANDIDATE_WORKLOAD',)
    record.update(delivery_attempts={role: 0 for role in roles},
                  delivery_completed={role: 0 for role in roles},
                  operation_results={role: 'NOT_RUN' for role in roles})
    provider._candidate_credential_results.setdefault(profile.profile, {})[purpose] = record
    secret = None
    primary_error = None
    try:
        print(profile.profile + ' / ' + purpose, flush=True)
        secret = console.capture()
        record['capture_completed'] = 1
        _check_checkout(plan.source_sha, plan.source_tree)
        yield secret, record
        outcomes = set(record['operation_results'].values())
        record['operation_result'] = 'PASS' if outcomes == {'PASS'} else ('NOT_RUN' if outcomes == {'NOT_RUN'} else 'FAIL')
    except BaseException as error:
        primary_error = error
        record['failure_code'] = (getattr(error, 'code', str(error))
            if isinstance(error, (ControllerFailure, ConsoleCaptureError, h.CandidateHarnessError))
            else 'CANDIDATE_CAPTURE_INTERRUPTED_OR_UNCLASSIFIED')
        raise
    finally:
        if secret is not None:
            wipe(secret)
        record['secret_cleanup'] = 'BEST_EFFORT_COMPLETED'
        try:
            with hold_windows_private_path_chain(slot, allow_leaf_child_writes=True):
                with (slot / 'result.json').open('xb') as output:
                    output.write(h.canonical_json_bytes(record))
        except BaseException:
            record['record_persistence_failure'] = 'CANDIDATE_CAPTURE_RECORD_WRITE_FAILED'
            if primary_error is None:
                raise ControllerFailure('CANDIDATE_CAPTURE_RECORD_WRITE_FAILED') from None


def bootstrap_candidate(provider, plan, profile, lease, disk, snapshot):
    authority = provider._active_profile_authority(profile, plan)
    # This gate occurs before opening/reserving the input channel. The actual
    # same-process grant repeats it after capture, inside SessionSupervisor.
    provider._verify_bootstrap_connection(authority, profile,
        preboot_disk_graph_digest=disk, preboot_snapshot_identity=snapshot)
    provider._remove_known_hosts(authority)
    with _capture(provider, plan, profile, 'SESSION_BOOTSTRAP') as (secret, record):
        supervisor = None
        try:
            supervisor = SessionSupervisor(secret, provider=provider, plan=plan, profile=profile,
                lease=lease, preboot_disk_graph_digest=disk, preboot_snapshot_identity=snapshot)
            record['operation_results']['BOOTSTRAP_ROTATION'] = 'ERROR'
            supervisor.bootstrap_rotation()
            record['operation_results']['BOOTSTRAP_ROTATION'] = 'PASS'
            record['operation_results']['VERIFIED_SUDO'] = 'ERROR'
            supervisor.validate_verified_guest()
            record['operation_results']['VERIFIED_SUDO'] = 'PASS'
            issued = _IssuedConnection(provider._execution, plan.plan_digest, lease, supervisor._verified)
            provider._candidate_connections[profile.profile] = issued
            return issued.verified
        finally:
            if supervisor is not None:
                record['delivery_attempts'] = supervisor.delivery_attempts
                record['delivery_completed'] = supervisor.delivery_completed
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
    for name in ('closed_runtime_inventory.py', 'candidate_workload_root.py'):
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
    program = ("scope={'__name__':'_animemo_fixed_root'};" +
            ''.join('exec(compile(' + repr(program) + ",'<fixed-candidate-root>','exec'),scope);" for program in programs) +
            "scope['run_fixed_candidate'](**" + repr(args) + ')')
    encoded = base64.b64encode(zlib.compress(program.encode('utf-8'), level=9)).decode('ascii')
    if len(encoded) > 20000:
        raise ControllerFailure('CANDIDATE_ROOT_PROGRAM_SIZE_INVALID')
    return 'import base64,zlib;exec(compile(zlib.decompress(base64.b64decode(' + repr(encoded) + ")), '<fixed-candidate-root>', 'exec'))"


def _remote_workload_command(root_program):
    # Only public identity and a bounded receipt reach stdout. The mutable
    # password is wiped immediately after one forwarding write, before wait.
    from scripts.guest_sudo_session import _REMOTE_OBSERVE
    observe = _REMOTE_OBSERVE[:_REMOTE_OBSERVE.index('password=')]
    argv = ['/usr/bin/sudo', '-S', '-k', '-p', '', '--', '/usr/bin/python3', '-I', '-B', '-c', root_program]
    program = observe + '\n' + '''password=bytearray()
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
    child=subprocess.Popen(''' + repr(argv) + ''',stdin=subprocess.PIPE,stdout=sys.stdout.buffer,stderr=subprocess.DEVNULL,bufsize=0)
    written=child.stdin.write(password)
    if written!=len(password): raise ValueError('CANDIDATE_SECRET_SHORT_WRITE')
    child.stdin.flush()
    child.stdin.close()
finally:
    password[:]=b'\\0'*len(password)
    password.clear()
sys.exit(child.wait())
'''
    return '/usr/bin/python3 -I -B -c ' + shlex.quote(program)


def _read_receipt(process, *, timeout=WORKLOAD_SECONDS):
    results = queue.Queue(maxsize=1)
    def read():
        try:
            value = process.stdout.read(MAX_RECEIPT_BYTES + 1)
            results.put(value)
        except BaseException:
            results.put(None)
    threading.Thread(target=read, daemon=True).start()
    try:
        body = results.get(timeout=max(0, timeout))
    except queue.Empty:
        raise ControllerFailure('CANDIDATE_WORKLOAD_TIMEOUT') from None
    if type(body) is not bytes or not 0 < len(body) <= MAX_RECEIPT_BYTES:
        raise ControllerFailure('CANDIDATE_VM_PROFILE_RECEIPT_INVALID')
    try:
        value = json.loads(body, object_pairs_hook=h.reject_duplicate_json_keys)
    except (ValueError, UnicodeError):
        raise ControllerFailure('CANDIDATE_VM_PROFILE_RECEIPT_INVALID') from None
    if type(value) is not dict:
        raise ControllerFailure('CANDIDATE_VM_PROFILE_RECEIPT_INVALID')
    return value


class _WorkloadSupervisor(SessionSupervisor):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
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
            wipe(self._secret)
        # No secret or reusable grant is needed while the fixed process runs.
        self.receipt = _read_receipt(process, timeout=self._workload_deadline - time.monotonic())

    def execute(self, command):
        try:
            self._workload_deadline = time.monotonic() + WORKLOAD_SECONDS
            with hold_windows_private_file(self._authority.known_hosts_file):
                self._provider._run(self._provider._ssh_argv(self._authority, command),
                    code='CANDIDATE_VM_PROFILE_EXECUTION_FAILED', timeout=WORKLOAD_SECONDS,
                    openssh=True, guest_exchange=self._exchange_workload)
            if self._delivery_completed['CANDIDATE_WORKLOAD'] != 1:
                raise ControllerFailure('CANDIDATE_WORKLOAD_NOT_DELIVERED')
            return self.receipt
        except BaseException:
            self.close(failed=True)
            raise


def execute_candidate_workload(provider, plan, profile, lease, disk, snapshot,
                               candidate_root, initial_platform_state):
    authority = provider._active_profile_authority(profile, plan)
    with hold_windows_private_file(authority.known_hosts_file):
        _continuing_connection(provider, plan, profile, lease, disk, snapshot)
        _stage_candidate(provider, authority, plan, profile, candidate_root)
        command = _remote_workload_command(_root_program(provider, plan, profile, initial_platform_state))
        # Long SCP/preparation has finished before the second capture exists.
        _continuing_connection(provider, plan, profile, lease, disk, snapshot)
        with _capture(provider, plan, profile, 'CANDIDATE_WORKLOAD') as (secret, record):
            supervisor = None
            try:
                supervisor = _WorkloadSupervisor(secret, provider=provider, plan=plan, profile=profile,
                    lease=lease, preboot_disk_graph_digest=disk, preboot_snapshot_identity=snapshot)
                record['operation_results']['CANDIDATE_WORKLOAD'] = 'ERROR'
                receipt = supervisor.execute(command)
                record['operation_results']['CANDIDATE_WORKLOAD'] = receipt.get('result', 'ERROR')
                return receipt
            finally:
                if supervisor is not None:
                    record['delivery_attempts'] = supervisor.delivery_attempts
                    record['delivery_completed'] = supervisor.delivery_completed
                    supervisor.close()
