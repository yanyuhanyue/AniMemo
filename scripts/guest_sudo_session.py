"""One-session Guest sudo controller; no IPC, marker, or environment authority.

The caller owns the canonical provider execution/profile contexts. Bootstrap
rotation and normal verification use different one-use grants on the same SSH
process that produced the final Guest observation. This module does not start
VMs, create release authority, or run Qualification.
"""
from __future__ import annotations

import json
import os
import queue
import re
import shlex
import threading
import time
from contextlib import ExitStack
from dataclasses import dataclass
from typing import Any

from release.formal_windows_pretrust import (
    hold_windows_private_file, hold_windows_private_working_directory,
)
from scripts import candidate_vm_harness as h


class ControllerFailure(RuntimeError):
    pass


def wipe(value: bytearray) -> None:
    value[:] = b"\0" * len(value)
    value.clear()


@dataclass
class _Grant:
    def __reduce__(self):
        raise TypeError("Guest grants cannot be serialized")

    owner: object
    execution: object
    process: object
    role: str
    expires: float
    used: bool = False


# The remote helper emits only public identity before reading any sudo input.
# It then executes a fixed controller operation, discarding all sudo output.
_REMOTE_OBSERVE = (
    'import glob,json,subprocess,sys;'
    'challenge=subprocess.run(["/usr/bin/vmtoolsd","--cmd",'
    '"info-get guestinfo.animemo.connectionChallenge"],check=True,'
    'capture_output=True,text=True,timeout=10).stdout.strip();'
    'print(json.dumps({"machine_id":open("/etc/machine-id").read().strip(),'
    '"boot_id":open("/proc/sys/kernel/random/boot_id").read().strip(),'
    '"mac_addresses":sorted(open(p).read().strip() for p in '
    'glob.glob("/sys/class/net/*/address")),"nonce":challenge}),flush=True);'
    'password=sys.stdin.buffer.readline(4098);'
    'assert 1<len(password)<=4097 and password.endswith(b"\\n");'
)


def _remote_command(role: str, public_key: str = "") -> str:
    if role == "BOOTSTRAP_ROTATION":
        fields = public_key.split()
        if len(fields) != 3 or fields[0] != "ssh-ed25519" or not re.fullmatch(r"[A-Za-z0-9+/]+={0,2}", fields[1]):
            raise ControllerFailure("SESSION_PUBLIC_KEY_INVALID")
        script = (
            "/usr/bin/install -d -o animemo -g animemo -m 0700 /home/animemo/.ssh; "
            "/usr/bin/printf '%s\\n' " + shlex.quote(public_key)
            + " > /home/animemo/.ssh/authorized_keys; "
            "/bin/chown animemo:animemo /home/animemo/.ssh/authorized_keys; "
            "/bin/chmod 0600 /home/animemo/.ssh/authorized_keys; "
            "/bin/rm -f -- /etc/ssh/ssh_host_*_key /etc/ssh/ssh_host_*_key.pub; "
            "/usr/bin/ssh-keygen -A; /usr/bin/systemctl restart ssh"
        )
        argv = ["/usr/bin/sudo", "-S", "-k", "-p", "", "--", "/bin/sh", "-ceu", script]
    elif role == "VERIFIED_SUDO":
        argv = ["/usr/bin/sudo", "-S", "-k", "-p", "", "-v"]
    else:
        raise ControllerFailure("GUEST_ROLE_REJECTED")
    program = _REMOTE_OBSERVE + 'sys.exit(subprocess.run(' + repr(argv) + ',input=password,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode)'
    return "/usr/bin/python3 -I -B -c " + shlex.quote(program)


def _read_observation(process: Any, timeout: float) -> dict:
    result: queue.Queue = queue.Queue(maxsize=1)
    def read() -> None:
        try:
            result.put(process.stdout.readline(4097))
        except Exception:
            result.put(None)
    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    try:
        line = result.get(timeout=max(0.001, timeout))
    except queue.Empty:
        raise ControllerFailure("GUEST_OBSERVATION_TIMEOUT") from None
    if not isinstance(line, bytes) or len(line) > 4096 or not line.endswith(b"\n"):
        raise ControllerFailure("GUEST_OBSERVATION_INVALID")
    try:
        value = json.loads(line, object_pairs_hook=h.reject_duplicate_json_keys)
    except (ValueError, UnicodeError):
        raise ControllerFailure("GUEST_OBSERVATION_INVALID") from None
    if type(value) is not dict or set(value) != {"machine_id", "boot_id", "mac_addresses", "nonce"}:
        raise ControllerFailure("GUEST_OBSERVATION_INVALID")
    if any(type(value[k]) is not str for k in ("machine_id", "boot_id", "nonce")) or type(value["mac_addresses"]) is not list or any(type(x) is not str for x in value["mac_addresses"]):
        raise ControllerFailure("GUEST_OBSERVATION_INVALID")
    return value


class SessionSupervisor:
    """In-process owner. Only trusted canonical orchestration may construct it.

    A plan is an input, not a grant. Every secret use runs the actual canonical
    verifier and fresh runtime checks; no VerifiedCloneConnection is accepted
    as an authorization ticket from an external caller.
    """
    def __init__(self, secret: bytearray, *, provider: h.ClosedVmwareProvider,
                 plan: h.CandidateHarnessPlan,
                 profile: h.CandidateProfilePlan,
                 preboot_disk_graph_digest: str, preboot_snapshot_identity: str,
                 lease: h.ProviderSessionLease | None = None,
                 clock=time.monotonic, lifetime: float = 300):
        from scripts.candidate_batch_session import BatchUse
        self._batch_use = secret if type(secret) is BatchUse else None
        if self._batch_use is None and (type(secret) is not bytearray or not 1 <= len(secret) <= 4096 or any(x < 32 or x == 127 for x in secret)):
            raise ControllerFailure("SUDO_VALUE_INVALID")
        if type(provider) is not h.ClosedVmwareProvider or type(plan) is not h.CandidateHarnessPlan or profile not in plan.profiles or profile.session_id != plan.session_id:
            raise ControllerFailure("SESSION_PLAN_INVALID")
        if h.sha256_bytes(h.canonical_json_bytes(plan.identity_body())) != plan.plan_digest:
            raise ControllerFailure("SESSION_PLAN_INVALID")
        if not 0 < lifetime <= 300:
            raise ControllerFailure("SESSION_LIFETIME_INVALID")
        if not h._DIGEST.fullmatch(preboot_disk_graph_digest) or preboot_snapshot_identity != profile.snapshot_identity:
            raise ControllerFailure("PREBOOT_IDENTITY_INVALID")
        provider._require_active_execution_authority()
        self._preboot_disk = preboot_disk_graph_digest
        self._preboot_snapshot = preboot_snapshot_identity
        self._cancelled = threading.Event()
        self._secret = secret if self._batch_use is None else None
        self._provider = provider
        self._plan = plan
        self._profile = profile
        self._authority = provider._active_profile_authority(profile, plan)
        self._execution = provider._execution
        self._lease = lease
        self._holds = ExitStack()
        self._clock = clock
        self._expires = clock() + lifetime
        self._lock = threading.RLock()
        self._state = "NEW"
        self._bootstrap = None
        self._prior_keys = frozenset(provider._accepted_host_key_digests)
        self._grant = None
        self._delivery_attempts = {"BOOTSTRAP_ROTATION": 0, "VERIFIED_SUDO": 0}
        self._delivery_completed = dict(self._delivery_attempts)
        # Completed stdin write/flush/close exchanges, not successful sudo
        # operations. An attempted exchange may have delivered bytes on failure.
        self.injection_count = 0
        try:
            if self._batch_use is not None:
                self._batch_use.bind(self)
            self._check_scope()
            if provider._require_execution_context:
                for directory in (self._authority.session_root, self._authority.profile_root,
                                  self._authority.ssh_root, self._authority.clone_root):
                    self._holds.enter_context(hold_windows_private_working_directory(directory))
                for path in (lease.path, self._authority.identity_file, self._authority.identity_file.with_suffix(".pub")):
                    self._holds.enter_context(hold_windows_private_file(path))
        except BaseException:
            self.close(failed=True)
            raise ControllerFailure("HELD_GUEST_SCOPE_REQUIRED") from None

    @property
    def delivery_attempts(self) -> dict[str, int]:
        """Snapshot of writes attempted by role, including uncertain delivery."""
        with self._lock:
            return dict(self._delivery_attempts)

    @property
    def delivery_completed(self) -> dict[str, int]:
        """Snapshot of completed stdin exchanges; does not imply sudo success."""
        with self._lock:
            return dict(self._delivery_completed)

    def _check_scope(self) -> None:
        provider = self._provider
        if not provider._require_execution_context:
            return  # Existing synthetic runner/platform test boundary only.
        execution = provider._execution
        if execution is None or execution is not self._execution or provider._execution_stack is None:
            raise ControllerFailure("HELD_EXECUTION_REQUIRED")
        lease = self._lease
        if type(lease) is not h.ProviderSessionLease or lease.holder is None:
            raise ControllerFailure("HELD_PROVIDER_LEASE_REQUIRED")
        lease.require_open()
        if lease.authority != self._authority:
            raise ControllerFailure("PROVIDER_LEASE_PROFILE_MISMATCH")
        if lease.path != execution.work_root / ".active-provider-session.lock" or h._file_identity(lease.path.lstat()) != lease.file_identity:
            raise ControllerFailure("PROVIDER_LEASE_CHANGED")
        if execution.private_source is None or dict(execution.private_source.file_identities) != dict(self._plan.original_vm_hashes):
            raise ControllerFailure("SOURCE_AUTHORITY_CHANGED")
        if provider.inspect_readiness().receipt_digest != self._plan.provider_readiness_receipt_digest:
            raise ControllerFailure("PROVIDER_READINESS_CHANGED")
        material = provider._candidate_material_authority
        if type(material) is not h.HeldCandidateMaterialAuthority:
            raise ControllerFailure("HELD_CANDIDATE_REQUIRED")
        loaded = material.loaded  # Canonical property rejects closed authority.
        expected = {"source_sha": self._plan.source_sha, "source_tree": self._plan.source_tree,
                    "candidate_version": self._plan.candidate_version,
                    "qualification_run_id": self._plan.qualification_run_id}
        if (loaded.verified_digest != self._plan.verified_candidate_digest
                or loaded.verified["candidate_input_sha256"] != self._plan.candidate_input_digest
                or any(loaded.candidate_input.get(k) != v for k, v in expected.items())
                or material.identity != execution.candidate_material_authority_identity
                or material.tree_inventory_identity != execution.candidate_material_tree_inventory_identity):
            raise ControllerFailure("CANDIDATE_AUTHORITY_CHANGED")

    def _active(self) -> None:
        if self._batch_use is not None:
            self._batch_use.check(self)
        if self._cancelled.is_set() or self._state in {"FAILED", "CLOSED", "VERIFIED"} or self._clock() >= self._expires or self._provider._execution is not self._execution:
            raise ControllerFailure("SESSION_AUTHORITY_EXPIRED")
        self._provider._require_active_execution_authority()
        self._check_scope()
        if h.sha256_bytes(h.canonical_json_bytes(self._plan.identity_body())) != self._plan.plan_digest or self._profile not in self._plan.profiles or self._provider._active_profile_authority(self._profile, self._plan) != self._authority:
            raise ControllerFailure("SESSION_AUTHORITY_CHANGED")

    def _runtime(self) -> h.CloneRuntimeIdentity:
        self._active()
        expected = os.path.normcase(str(self._authority.clone_vmx.resolve(strict=False)))
        if self._provider._running_vmx_paths() != frozenset({expected}):
            raise ControllerFailure("GUEST_VM_NAMESPACE_MISMATCH")
        return self._provider._read_clone_runtime_identity(
            self._authority, self._profile,
            expected_ip=self._provider._wait_for_guest_ip(self._authority.clone_vmx),
            preboot_disk_graph_digest=self._preboot_disk,
            preboot_snapshot_identity=self._preboot_snapshot,
        )

    def _verify(self, role: str, observation: h.GuestConnectionObservation) -> h.VerifiedCloneConnection:
        runtime = self._runtime()
        if self._bootstrap is None or runtime != self._bootstrap.runtime:
            raise ControllerFailure("GUEST_RUNTIME_CHANGED")
        common = dict(authority=self._authority, plan=self._profile, runtime=runtime,
                      bootstrap=self._bootstrap.guest, known_hosts_was_absent=True,
                      competing_vmx_paths=frozenset())
        if role == "BOOTSTRAP_ROTATION":
            return h.verify_bootstrap_clone_identity(**common, confirmation=observation)
        return h.verify_clone_connection_identity(**common, verified_guest=observation,
                                                  prior_host_key_digests=self._prior_keys | frozenset(self._provider._accepted_host_key_digests))

    def _consume(self, grant: _Grant, process: Any, role: str) -> None:
        self._active()
        if grant is not self._grant or grant.owner is not self or grant.execution is not self._execution or grant.process is not process or grant.role != role or grant.used or self._clock() >= grant.expires or process.poll() is not None or role not in self._delivery_attempts or self._delivery_attempts[role] != 0:
            raise ControllerFailure("GUEST_GRANT_REJECTED")
        grant.used = True
        if self._batch_use is not None:
            self._delivery_attempts[role] += 1
            self._batch_use.deliver(self, grant, process, role)
            self._delivery_completed[role] += 1
            self.injection_count += 1
            return
        value = bytearray(self._secret)
        value.append(10)
        try:
            self._delivery_attempts[role] += 1
            written = process.stdin.write(value)
            if type(written) is not int or written != len(value):
                raise ControllerFailure("GUEST_STDIN_SHORT_WRITE")
            process.stdin.flush()
            process.stdin.close()
            self._delivery_completed[role] += 1
            self.injection_count += 1
        finally:
            wipe(value)

    def _exchange(self, role: str, process: Any) -> None:
        with self._lock:
            self._active()
            if self._grant is not None or self._state != role:
                raise ControllerFailure("GUEST_GRANT_REPLAY")
            value = _read_observation(process, min(30, self._expires - self._clock()))
            observation = h.GuestConnectionObservation(
                machine_id=value["machine_id"], boot_id=value["boot_id"],
                mac_addresses=tuple(value["mac_addresses"]), nonce=value["nonce"],
                host_key_digest=self._provider._read_known_host_key(self._authority))
            verified = self._verify(role, observation)
            if role == "VERIFIED_SUDO":
                self._verified = verified
            grant = _Grant(self, self._execution, process, role, min(self._expires, self._clock()+5))
            self._grant = grant
            self._consume(grant, process, role)

    def _run(self, role: str) -> None:
        public_key = self._provider._session_public_key(self._authority) if role == "BOOTSTRAP_ROTATION" else ""
        with ExitStack() as stack:
            # Production inherits the held private tool/source/profile contexts.
            # Pin this known-hosts file for this connection only, not across rotation.
            if self._provider._require_execution_context:
                stack.enter_context(hold_windows_private_file(self._authority.known_hosts_file))
            self._provider._run(
                self._provider._ssh_argv(self._authority, _remote_command(role, public_key),
                    bootstrap_identity=role == "BOOTSTRAP_ROTATION"),
                code="GUEST_SUDO_OPERATION_FAILED", timeout=120, openssh=True,
                guest_exchange=lambda process: self._exchange(role, process),
            )
        if self._grant is None or not self._grant.used:
            raise ControllerFailure("GUEST_EXCHANGE_NOT_CONSUMED")
        self._grant = None

    def bootstrap_rotation(self) -> None:
        with self._lock:
            try:
                self._active()
                if self._state != "NEW":
                    raise ControllerFailure("BOOTSTRAP_ROTATION_REPLAY")
                self._state = "BOOTSTRAP_ROTATION"
                self._bootstrap = self._provider._verify_bootstrap_connection(
                    self._authority, self._profile,
                    preboot_disk_graph_digest=self._preboot_disk,
                    preboot_snapshot_identity=self._preboot_snapshot,
                )
                self._run("BOOTSTRAP_ROTATION")
                self._state = "ROTATED"
            except BaseException:
                self.close(failed=True)
                raise ControllerFailure("BOOTSTRAP_ROTATION_FAILED") from None

    def validate_verified_guest(self) -> None:
        with self._lock:
            try:
                self._active()
                if self._state != "ROTATED":
                    raise ControllerFailure("VERIFIED_ROLE_NOT_READY")
                self._state = "VERIFIED_SUDO"
                self._provider._remove_known_hosts(self._authority)
                self._provider._wait_for_ssh(self._authority, capture_new_host_key=True)
                self._run("VERIFIED_SUDO")
                self._provider._accepted_host_key_digests.add(self._verified.guest.host_key_digest)
                self._state = "VERIFIED"
                self._clear_secret()
            except BaseException:
                self.close(failed=True)
                raise ControllerFailure("VERIFIED_GUEST_FAILED") from None

    def close(self, *, failed: bool = False) -> None:
        self._cancelled.set()
        with self._lock:
            self._state = "FAILED" if failed else "CLOSED"
            self._grant = None
            self._clear_secret()
            if self._batch_use is not None:
                self._batch_use.close(failed=failed)
            self._holds.close()

    def _clear_secret(self):
        if self._secret is not None:
            wipe(self._secret)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
