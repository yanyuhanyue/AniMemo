"""Single Runtime DEV baseline and one boot budget; no credentials or install authority.

The clock implementation is stdlib-only so the exact held source can also run
inside the fixed root workload. Public envelopes never carry monotonic values.
"""
from __future__ import annotations

import base64
import hashlib
import inspect
import json
import math
import re
import shlex
import time
import zlib
from datetime import datetime, timezone

MAX_BOOT_SECONDS = 4 * 60 * 60
STOP_RESERVE_SECONDS = 300
VM_STOP_RESERVE_SECONDS = 120
BASELINE_MAX_AGE_SECONDS = 60
_CONFIRM_ISSUER = object()


class RuntimeBoundaryError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _require(condition, code='RUNTIME_DEVELOPMENT_BOUNDARY_INVALID'):
    if not condition:
        raise RuntimeBoundaryError(code)


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False,
                      allow_nan=False).encode('utf-8')


def _digest(value):
    return 'sha256:' + hashlib.sha256(_canonical(value)).hexdigest()


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def parse_authorization_deadline(value):
    _require(type(value) is str, 'RUNTIME_AUTHORIZATION_DEADLINE_REQUIRED')
    try:
        parsed = datetime.strptime(value, '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc)
    except ValueError:
        raise RuntimeBoundaryError('RUNTIME_AUTHORIZATION_DEADLINE_INVALID') from None
    _require(parsed.strftime('%Y-%m-%dT%H:%M:%SZ') == value,
             'RUNTIME_AUTHORIZATION_DEADLINE_INVALID')
    return parsed.timestamp()


class RuntimeDeadline:
    """A non-renewable local monotonic budget, intersected with absolute time."""

    def __init__(self, *, binding, authorization_expires_utc_seconds,
                 wall_clock=time.time, monotonic_clock=time.monotonic,
                 _envelope=None):
        self._wall, self._mono = wall_clock, monotonic_clock
        wall, mono = self._wall(), self._mono()
        _require(_number(wall) and _number(mono), 'RUNTIME_CLOCK_UNTRUSTED')
        self._binding = {name: binding[name] for name in (
            'plan_digest', 'session_id', 'execution_source_sha', 'execution_source_tree')}
        _require(all(type(value) is str and value for value in self._binding.values()))
        _require(_number(authorization_expires_utc_seconds), 'RUNTIME_AUTHORIZATION_DEADLINE_INVALID')
        self.authorization_expires_utc_seconds = authorization_expires_utc_seconds
        self.boot_started_utc_seconds = wall
        self.effective_expires_utc_seconds = min(wall + MAX_BOOT_SECONDS,
                                                 authorization_expires_utc_seconds)
        remaining = self.effective_expires_utc_seconds - wall
        if _envelope is not None:
            expected = {'schema', *self._binding, 'boot_started_utc_seconds',
                        'authorization_expires_utc_seconds', 'effective_expires_utc_seconds',
                        'issued_utc_seconds', 'remaining_seconds', 'stop_reserve_seconds'}
            _require(type(_envelope) is dict and set(_envelope) == expected
                     and _envelope['schema'] == 'animemo.runtime-boot-deadline/v1'
                     and all(_envelope[name] == value for name, value in self._binding.items())
                     and all(_number(_envelope[name]) for name in expected - {'schema', *self._binding})
                     and _envelope['stop_reserve_seconds'] == STOP_RESERVE_SECONDS,
                     'RUNTIME_DEADLINE_ENVELOPE_INVALID')
            started = _envelope['boot_started_utc_seconds']
            issued = _envelope['issued_utc_seconds']
            expires = _envelope['effective_expires_utc_seconds']
            _require(started <= issued < expires <= min(started + MAX_BOOT_SECONDS, authorization_expires_utc_seconds)
                     and 0 < _envelope['remaining_seconds'] <= expires - issued
                     and wall >= issued - 5, 'RUNTIME_CLOCK_UNTRUSTED')
            self.boot_started_utc_seconds = started
            self.effective_expires_utc_seconds = expires
            # Cross-machine delay can only shorten this local monotonic budget.
            remaining = min(expires - wall, _envelope['remaining_seconds'])
        _require(remaining > STOP_RESERVE_SECONDS, 'RUNTIME_BOOT_BUDGET_EXHAUSTED')
        self.monotonic_deadline = mono + remaining
        self._started_monotonic = mono - max(0, wall - self.boot_started_utc_seconds)
        self._cleanup_deadline = self.monotonic_deadline + STOP_RESERVE_SECONDS
        self._last_wall = wall
        self._closed = False
        self._failed = False
        self._events = []
        self._closed_observation = None
        self._cancelled = None

    @property
    def deadline(self):
        return self.monotonic_deadline

    @classmethod
    def from_guest_envelope(cls, envelope, *, binding, **clocks):
        _require(type(envelope) is dict and 'authorization_expires_utc_seconds' in envelope,
                 'RUNTIME_DEADLINE_ENVELOPE_INVALID')
        return cls(binding=binding,
                   authorization_expires_utc_seconds=envelope['authorization_expires_utc_seconds'],
                   _envelope=envelope, **clocks)

    def _remaining(self):
        wall, mono = self._wall(), self._mono()
        if not (_number(wall) and _number(mono)) or wall < self._last_wall - 1:
            self._failed = True
            raise RuntimeBoundaryError('RUNTIME_CLOCK_ROLLBACK')
        self._last_wall = max(self._last_wall, wall)
        return min(self.monotonic_deadline - mono,
                   self.effective_expires_utc_seconds - wall)

    def check(self, stage, *, cleanup=False, minimum_seconds=0):
        _require(type(stage) is str and stage and _number(minimum_seconds)
                 and minimum_seconds >= 0, 'RUNTIME_DEADLINE_CHECK_INVALID')
        if cleanup:
            remaining = min(self._cleanup_deadline - self._mono(),
                self.effective_expires_utc_seconds + STOP_RESERVE_SECONDS - self._wall())
            overrun = max(0, self._mono() - self.monotonic_deadline,
                          self._wall() - self.effective_expires_utc_seconds)
            if overrun:
                self._events.append({'stage': stage, 'result': 'CLEANUP_AFTER_EXPIRY',
                                     'overrun_seconds': overrun})
            _require(remaining > minimum_seconds, 'RUNTIME_CLEANUP_BUDGET_EXHAUSTED')
            return remaining
        _require(not self._closed and not self._failed
                 and (self._cancelled is None or not self._cancelled.is_set()), 'RUNTIME_LIFETIME_CLOSED')
        remaining = self._remaining() - STOP_RESERVE_SECONDS
        if remaining <= minimum_seconds:
            self._failed = True
            self._events.append({'stage': stage, 'result': 'BUDGET_EXHAUSTED'})
            raise RuntimeBoundaryError('RUNTIME_EXECUTION_BUDGET_EXHAUSTED')
        return remaining

    def clip_timeout(self, seconds, *, cleanup=False):
        _require(_number(seconds) and seconds > 0, 'RUNTIME_TIMEOUT_INVALID')
        return min(seconds, self.check('SUBPROCESS', cleanup=cleanup))

    def workload_timeout(self, seconds):
        """Outer process/receipt wait includes instance stop, reserves VM stop."""
        _require(_number(seconds) and seconds > 0, 'RUNTIME_TIMEOUT_INVALID')
        _require(not self._closed and not self._failed
                 and (self._cancelled is None or not self._cancelled.is_set()), 'RUNTIME_LIFETIME_CLOSED')
        remaining = self._remaining() - VM_STOP_RESERVE_SECONDS
        _require(remaining > 0, 'RUNTIME_WORKLOAD_BUDGET_EXHAUSTED')
        return min(seconds, remaining)

    def instance_stop_timeout(self, seconds):
        """The same role may stop its instance after a business-plan refusal."""
        _require(_number(seconds) and seconds > 0, 'RUNTIME_TIMEOUT_INVALID')
        remaining = min(self.monotonic_deadline - self._mono(),
                        self.effective_expires_utc_seconds - self._wall()) - VM_STOP_RESERVE_SECONDS
        _require(remaining > 0, 'RUNTIME_INSTANCE_STOP_BUDGET_EXHAUSTED')
        return min(seconds, remaining)

    def guest_envelope(self):
        self.check('GUEST_DISPATCH')
        issued = self._wall()
        remaining = min(self.monotonic_deadline - self._mono(),
                        self.effective_expires_utc_seconds - issued)
        return {'schema': 'animemo.runtime-boot-deadline/v1', **self._binding,
                'boot_started_utc_seconds': self.boot_started_utc_seconds,
                'authorization_expires_utc_seconds': self.authorization_expires_utc_seconds,
                'effective_expires_utc_seconds': self.effective_expires_utc_seconds,
                'issued_utc_seconds': issued, 'remaining_seconds': remaining,
                'stop_reserve_seconds': STOP_RESERVE_SECONDS}

    def close(self, reason):
        self._closed = True
        self._closed_observation = {'elapsed_boot_budget_seconds': self._mono() - self._started_monotonic,
            'overrun_seconds': max(0, self._mono() - self.monotonic_deadline,
                                   self._wall() - self.effective_expires_utc_seconds)}
        self._events.append({'stage': 'CLOSE', 'result': str(reason)})

    @property
    def record(self):
        return {'schema': 'animemo.runtime-lifetime-observation/v1', **self._binding,
                'boot_started_utc_seconds': self.boot_started_utc_seconds,
                'effective_expires_utc_seconds': self.effective_expires_utc_seconds,
                'maximum_boot_seconds': MAX_BOOT_SECONDS, 'stop_reserve_seconds': STOP_RESERVE_SECONDS,
                'vm_stop_reserve_seconds': VM_STOP_RESERVE_SECONDS,
                'closed': self._closed, 'failed': self._failed,
                'close_observation': self._closed_observation,
                'events': json.loads(json.dumps(self._events))}

    def __reduce__(self):
        raise TypeError('Runtime lifetimes cannot be serialized')


def _single_runtime(plan, profile):
    from scripts.development_plan import is_development_plan
    _require(is_development_plan(plan) and plan.runtime_offline_only
             and len(plan.profiles) == 1 and profile is plan.profiles[0]
             and profile.profile == 'RUNTIME_BASE_OFFLINE'
             and plan.runtime_retention_policy == 'STOP_AND_RETAIN', 'RUNTIME_PLAN_REQUIRED')


def start_runtime_lifetime(provider, plan, profile):
    """Called immediately before the single boot invocation, never on READY."""
    from scripts.development_guest_session import development_binding
    _single_runtime(plan, profile)
    _require(getattr(provider, '_runtime_lifetime', None) is None, 'RUNTIME_BOOT_BUDGET_REPLAY')
    lifetime = RuntimeDeadline(binding=development_binding(plan),
        authorization_expires_utc_seconds=parse_authorization_deadline(plan.runtime_authorization_deadline))
    from scripts.candidate_guest_session import _batch
    batch = _batch(provider, plan)
    batch.require_active()
    owner = batch._development_owner
    _require(owner is not None and getattr(owner, '_preparing_batch', None) is batch,
             'RUNTIME_OWNER_REQUIRED')
    owner._live()
    reservation = batch._development_reservation
    reservation.require_open()
    # These values share this host's monotonic clock. Never serialize them.
    lifetime.monotonic_deadline = min(lifetime.monotonic_deadline, owner._deadline,
                                     reservation.deadline)
    lifetime.effective_expires_utc_seconds = min(lifetime.effective_expires_utc_seconds,
        lifetime._wall() + lifetime.monotonic_deadline - lifetime._mono())
    lifetime._cleanup_deadline = lifetime.monotonic_deadline + STOP_RESERVE_SECONDS
    lifetime._cancelled = batch.cancelled
    lifetime.check('BEFORE_BOOT')
    owner._deadline = min(owner._deadline, lifetime.monotonic_deadline - VM_STOP_RESERVE_SECONDS)
    lifetime._provider, lifetime._plan, lifetime._profile = provider, plan, profile
    provider._runtime_lifetime = lifetime
    provider._profile_operation_results[profile.profile]['runtime_lifetime'] = lifetime.record
    return lifetime


def require_runtime_lifetime(provider, plan, profile):
    _single_runtime(plan, profile)
    value = getattr(provider, '_runtime_lifetime', None)
    _require(type(value) is RuntimeDeadline and value._provider is provider
             and value._plan is plan and value._profile is profile, 'RUNTIME_LIFETIME_REQUIRED')
    return value


def observe_runtime_tool(path):
    """One fixed executable identity, shared by non-root and root readbacks."""
    import hashlib
    import os
    import stat
    from pathlib import Path
    selected = Path(path)
    try:
        resolved = selected.resolve(strict=True)
        if not str(resolved).startswith(('/usr/bin/', '/usr/lib/', '/usr/libexec/', '/usr/share/postgresql-common/')):
            return None
        for parent in (resolved, *resolved.parents):
            info = parent.stat()
            if info.st_uid != 0 or info.st_mode & 0o022:
                return None
        before = resolved.stat()
        if not stat.S_ISREG(before.st_mode) or not before.st_mode & 0o111 or before.st_size > 128 * 1024 * 1024:
            return None
        with resolved.open('rb') as stream:
            held = os.fstat(stream.fileno())
            digest = hashlib.file_digest(stream, 'sha256').hexdigest()
            after = os.fstat(stream.fileno())
        identity = lambda item: (item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns, item.st_ctime_ns)
        if (identity(before) != identity(held) or identity(held) != identity(after)
                or identity(after) != identity(resolved.stat()) or selected.resolve(strict=True) != resolved):
            return None
        return {'path': path, 'resolved_path': str(resolved), 'sha256': 'sha256:' + digest}
    except OSError:
        return None


def observe_runtime_tools():
    from pathlib import Path
    tools = {name: observe_runtime_tool(path) for name, path in (
        ('python', '/usr/bin/python3'), ('docker', '/usr/bin/docker'), ('systemctl', '/usr/bin/systemctl'),
        ('dpkg', '/usr/bin/dpkg'), ('dpkg-query', '/usr/bin/dpkg-query'),
        ('pg_dump', '/usr/bin/pg_dump'), ('psql', '/usr/bin/psql'))}
    compose_paths = ('/usr/libexec/docker/cli-plugins/docker-compose', '/usr/lib/docker/cli-plugins/docker-compose')
    # Docker searches these locations before the distro plugins. Fail rather
    # than accepting a user-context or alternate plugin implementation.
    shadows = ('/usr/local/lib/docker/cli-plugins/docker-compose',
               '/usr/local/libexec/docker/cli-plugins/docker-compose')
    shadow = any(Path(path).exists() or Path(path).is_symlink() for path in shadows)
    compose = [observe_runtime_tool(path) for path in compose_paths if Path(path).exists() or Path(path).is_symlink()]
    tools['compose'] = compose[0] if len(compose) == 1 and not shadow else None
    return tools


def observe_runtime_socket():
    import stat
    from pathlib import Path
    socket = {'state': 'MISSING', 'path': '/var/run/docker.sock'}
    try:
        path = Path('/var/run/docker.sock')
        resolved, info = path.resolve(strict=True), path.stat()
        if (str(resolved) not in ('/run/docker.sock', '/var/run/docker.sock')
                or not stat.S_ISSOCK(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o002
                or info.st_nlink != 1):
            socket['state'] = 'UNTRUSTED'
        else:
            socket.update(state='KNOWN_LOCAL', resolved_path=str(resolved),
                          device=info.st_dev, inode=info.st_ino, mode=info.st_mode,
                          uid=info.st_uid, gid=info.st_gid, ctime_ns=info.st_ctime_ns)
            after = path.stat()
            if (path.resolve(strict=True) != resolved or any(getattr(info, name) != getattr(after, name)
                    for name in ('st_dev', 'st_ino', 'st_mode', 'st_uid', 'st_gid', 'st_ctime_ns'))):
                socket = {'state': 'UNTRUSTED', 'path': '/var/run/docker.sock'}
    except PermissionError:
        socket['state'] = 'PERMISSION_UNKNOWN'
    except OSError:
        pass
    return socket


def runtime_daemon_identity(stdout):
    import hashlib
    import json
    try:
        values = stdout.decode('ascii', errors='strict').splitlines()
        if (len(values) != 2 or any(not value.isdecimal() or not 0 < int(value) < 2**63 for value in values)):
            return None
    except (UnicodeError, ValueError):
        return None
    raw = (json.dumps({'mainPid': int(values[0]), 'started': int(values[1])},
                      sort_keys=True, separators=(',', ':')) + '\n').encode('utf-8')
    return 'sha256:' + hashlib.sha256(raw).hexdigest()


def runtime_tool_version_major(name, text):
    """Read the tool's version token, never a minor or distro/build number."""
    import re
    if type(text) is not str or not 0 < len(text) <= 512:
        return None
    prefix = r'Docker Compose version v?' if name == 'compose' else (
        re.escape(name) + r' \(PostgreSQL\) ' if name in ('pg_dump', 'psql') else None)
    match = re.match(prefix + r'([0-9]{1,4})\.[0-9]+', text) if prefix else None
    return int(match[1]) if match else None


def _collect_guest_baseline(binding, capture_process):
    # Fixed read-only observation after authentication. Socket queries are
    # attempted only if ordinary user permissions allow this local socket.
    import hashlib
    import json
    import os
    import platform
    import sys
    import time
    from pathlib import Path
    missing = []
    if not runtime_transport_codec_ready():
        missing.append('RUNTIME_TRANSPORT_CODEC_UNAVAILABLE')
    tools = observe_runtime_tools()
    if any(value is None for value in tools.values()):
        missing.append('TRUSTED_RUNTIME_TOOL_MISSING')
    if tools['python'] is None or str(Path(sys.executable).resolve()) != tools['python']['resolved_path']:
        missing.append('PYTHON_EXECUTABLE_MISMATCH')
    environment = {'PATH': '/usr/bin:/bin', 'HOME': '/nonexistent', 'LANG': 'C.UTF-8', 'LC_ALL': 'C.UTF-8'}
    command_results = []
    command_deadline = time.monotonic() + 300

    def command(argv):
        left = command_deadline - time.monotonic()
        if left <= 0:
            raise ValueError('RUNTIME_BASELINE_TIMEOUT')
        result = capture_process(argv, timeout=min(30, left), environment=environment)
        if (result.outcome != 'EXITED' or result.secondary_errors
                or any(summary['truncated'] or summary['missing'] for summary in (
                    result.stdout_summary, result.stderr_summary))):
            raise ValueError('RUNTIME_BASELINE_COMMAND_INCOMPLETE')
        # Only fixed package/version/service queries are issued. Daemon stderr
        # is represented by a category; arbitrary daemon configuration is not
        # emitted through the observation protocol.
        command_results.append({'argv': list(argv), 'returncode': result.returncode,
            'started_at': result.started_at, 'ended_at': result.ended_at,
            'stdout_sha256': 'sha256:' + hashlib.sha256(result.stdout).hexdigest(),
            'stderr_sha256': 'sha256:' + hashlib.sha256(result.stderr).hexdigest()})
        return result

    service_active = False
    if tools['systemctl'] is not None:
        completed = command(['/usr/bin/systemctl', 'is-active', '--quiet', 'docker'])
        service_active = completed.returncode == 0
    if not service_active:
        missing.append('DOCKER_SERVICE_INACTIVE')
    socket = observe_runtime_socket()
    if socket['state'] in ('MISSING', 'UNTRUSTED'):
        missing.append('LOCAL_DOCKER_SOCKET_MISSING_OR_UNTRUSTED')
    package_architecture = None
    if tools['dpkg'] is not None:
        result = command(['/usr/bin/dpkg', '--print-architecture'])
        package_architecture = result.stdout.decode('ascii', errors='strict').strip() if result.returncode == 0 else None
    if package_architecture != 'amd64':
        missing.append('PACKAGE_ARCHITECTURE_MISMATCH')
    packages = {}
    if tools['dpkg-query'] is not None:
        for package in ('docker.io', 'docker-compose-v2', 'postgresql-client-16'):
            result = command(['/usr/bin/dpkg-query', '--show',
                '--showformat=${db:Status-Abbrev}\\t${binary:Package}\\t${Version}\\t${Architecture}\\n', package])
            text = result.stdout.decode('utf-8', errors='strict')
            pieces = text.rstrip('\n').split('\t')
            if (result.returncode != 0 or len(pieces) != 4 or pieces[0] != 'ii '
                    or pieces[1].split(':')[0] != package or pieces[3] not in ('amd64', 'all')):
                missing.append('RUNTIME_PACKAGE_MISSING:' + package)
            else:
                packages[package] = {'version': pieces[2], 'architecture': pieces[3]}
    versions = {}
    version_commands = {'docker': ['/usr/bin/docker', '--version'],
        'systemctl': ['/usr/bin/systemctl', '--version'],
        'compose': ['/usr/bin/docker', '--host', 'unix:///var/run/docker.sock', 'compose', 'version'],
        'pg_dump': ['/usr/bin/pg_dump', '--version'], 'psql': ['/usr/bin/psql', '--version']}
    for name, argv in version_commands.items():
        if tools.get(name) is None or (name == 'compose' and tools['docker'] is None):
            continue
        result = command(argv)
        first_line = result.stdout.decode('utf-8', errors='strict').splitlines()
        text = first_line[0] if first_line else ''
        if result.returncode != 0 or not text or len(text) > 512:
            missing.append('RUNTIME_TOOL_VERSION_UNAVAILABLE:' + name)
        else:
            versions[name] = text
            if name in ('pg_dump', 'psql') and runtime_tool_version_major(name, text) != 16:
                missing.append('POSTGRES_CLIENT_MAJOR_MISMATCH:' + name)
            if name == 'compose' and runtime_tool_version_major(name, text) != 2:
                missing.append('COMPOSE_MAJOR_MISMATCH')
    daemon_identity = None
    if tools['systemctl'] is not None:
        result = command(['/usr/bin/systemctl', 'show', '--property=MainPID',
            '--property=ExecMainStartTimestampMonotonic', '--value', 'docker'])
        if result.returncode == 0:
            daemon_identity = runtime_daemon_identity(result.stdout)
            if daemon_identity is None:
                missing.append('DOCKER_DAEMON_IDENTITY_INVALID')
        elif b'permission denied' not in result.stderr.lower() and b'access denied' not in result.stderr.lower():
            missing.append('DOCKER_DAEMON_IDENTITY_UNAVAILABLE')
    daemon = {'state': 'PERMISSION_UNKNOWN', 'identity': daemon_identity}
    if socket['state'] == 'KNOWN_LOCAL' and os.access('/var/run/docker.sock', os.R_OK | os.W_OK) and tools['docker'] is not None:
        daemon = {'state': 'OBSERVED', 'identity': daemon_identity}
        for field, expression in (('server_version', '{{.ServerVersion}}'), ('storage_root', '{{.DockerRootDir}}')):
            result = command(['/usr/bin/docker', '--host', 'unix:///var/run/docker.sock', 'info', '--format', expression])
            if result.returncode != 0:
                if b'permission denied' in result.stderr.lower():
                    daemon = {'state': 'PERMISSION_UNKNOWN', 'identity': daemon_identity}
                    break
                missing.append('DOCKER_DAEMON_UNAVAILABLE')
                daemon = {'state': 'UNAVAILABLE', 'identity': daemon_identity}
                break
            text = result.stdout.decode('utf-8', errors='strict').strip()
            if not text or len(text) > 512 or '\n' in text:
                raise ValueError('RUNTIME_BASELINE_DAEMON_OUTPUT_INVALID')
            daemon[field] = text
    os_release = Path('/etc/os-release').read_text(encoding='utf-8')
    os_values = dict(line.split('=', 1) for line in os_release.splitlines() if '=' in line)
    distribution = {key: os_values.get(key, '').strip('"') for key in ('ID', 'VERSION_ID')}
    if distribution != {'ID': 'ubuntu', 'VERSION_ID': '24.04'}:
        missing.append('RUNTIME_DISTRIBUTION_MISMATCH')
    result = {'schema': 'animemo.runtime-unprivileged-baseline/v1', 'binding': binding,
        'machine_id': Path('/etc/machine-id').read_text(encoding='ascii').strip(),
        'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text(encoding='ascii').strip(),
        'uid': os.getuid(), 'euid': os.geteuid(), 'platform': sys.platform,
        'architecture': platform.machine(), 'distribution': distribution,
        'observed_utc_seconds': time.time(), 'tools': tools, 'docker_service_active': service_active,
        'docker_socket': socket, 'docker_daemon': daemon, 'package_architecture': package_architecture,
        'packages': packages, 'versions': versions, 'command_results': command_results, 'hard_missing': missing,
        'root_observations_pending': ['effective_uid_zero', 'docker_daemon_identity',
            'docker_daemon_access', 'current_full_installer_plan', 'instance_resource_ownership']}
    print(json.dumps(result, sort_keys=True, separators=(',', ':'), ensure_ascii=True))


def baseline_binding(plan, profile, verified):
    return {'plan_digest': plan.plan_digest, 'session_id': plan.session_id,
            'execution_source_sha': plan.execution_source_sha,
            'execution_source_tree': plan.execution_source_tree,
            'execution_inventory_digest': plan.execution_inventory_digest,
            'candidate_input_digest': plan.candidate_input_digest,
            'verified_candidate_digest': plan.verified_candidate_digest,
            'runtime_trust_selection_digest': plan.runtime_trust_selection_digest,
            'profile': profile.profile, 'snapshot_identity': profile.snapshot_identity,
            'clone_identity': profile.clone_identity, 'nonce': verified.guest.nonce,
            'machine_id': verified.guest.machine_id, 'boot_id': verified.guest.boot_id}


def runtime_transport_codec_ready():
    """Check the fixed transport codec before native confirmation or capture."""
    try:
        import base64
        import lzma
        raw = b'animemo-runtime-transport-v1'
        encoded = base64.b85encode(lzma.compress(raw, format=lzma.FORMAT_XZ, preset=6))
        decoder = lzma.LZMADecompressor(format=lzma.FORMAT_XZ, memlimit=67108864)
        decoded = decoder.decompress(base64.b85decode(encoded), max_length=262145)
        return decoded == raw and decoder.eof and not decoder.unused_data
    except (ImportError, ValueError, MemoryError):
        return False
    except lzma.LZMAError:
        return False


def fixed_baseline_command(binding):
    from pathlib import Path
    capture = (Path(__file__).resolve().parents[1] / 'installer' / 'apt_diagnostics.py').read_text(encoding='utf-8')
    source = '\n'.join(inspect.getsource(function) for function in (
        observe_runtime_tool, observe_runtime_tools, observe_runtime_socket,
        runtime_daemon_identity, runtime_transport_codec_ready, runtime_tool_version_major,
        _collect_guest_baseline))
    program = ('capture_scope={"__name__":"__main__"}\nexec(compile(' + repr(capture)
        + ',"<fixed-runtime-capture>","exec"),capture_scope)\ncapture_scope["STREAM_LIMIT"]=65536\n'
        + source + '\n_collect_guest_baseline(' + repr(binding) + ',capture_scope["capture_process"])\n')
    compile(program, '<fixed-runtime-baseline>', 'exec')
    encoded = base64.b64encode(zlib.compress(program.encode('utf-8'), 9)).decode('ascii')
    return '/usr/bin/python3 -I -B -c ' + shlex.quote(
        'import base64,zlib;exec(compile(zlib.decompress(base64.b64decode('
        + repr(encoded) + ")), '<fixed-runtime-baseline>', 'exec'))")


def confirmed_baseline_facts(observation):
    """Only fixed public facts needed for confirmation-to-root drift checks."""
    _require(type(observation) is dict, 'RUNTIME_BASELINE_FACTS_INVALID')
    value = {key: observation.get(key) for key in ('tools', 'docker_socket', 'docker_daemon')}
    tools = value['tools']
    paths = {'python': ('/usr/bin/python3',), 'docker': ('/usr/bin/docker',),
        'systemctl': ('/usr/bin/systemctl',), 'dpkg': ('/usr/bin/dpkg',),
        'dpkg-query': ('/usr/bin/dpkg-query',), 'pg_dump': ('/usr/bin/pg_dump',), 'psql': ('/usr/bin/psql',),
        'compose': ('/usr/libexec/docker/cli-plugins/docker-compose', '/usr/lib/docker/cli-plugins/docker-compose')}
    _require(type(tools) is dict and set(tools) == set(paths), 'RUNTIME_BASELINE_FACTS_INVALID')
    for name, tool in tools.items():
        _require(type(tool) is dict and set(tool) == {'path', 'resolved_path', 'sha256'}
            and tool['path'] in paths[name] and type(tool['resolved_path']) is str
            and tool['resolved_path'].startswith(('/usr/bin/', '/usr/lib/', '/usr/libexec/', '/usr/share/postgresql-common/'))
            and '/..' not in tool['resolved_path'] and type(tool['sha256']) is str
            and re.fullmatch(r'sha256:[0-9a-f]{64}', tool['sha256']), 'RUNTIME_BASELINE_FACTS_INVALID')
    socket = value['docker_socket']
    _require(type(socket) is dict and socket.get('path') == '/var/run/docker.sock'
             and socket.get('state') in ('KNOWN_LOCAL', 'PERMISSION_UNKNOWN'), 'RUNTIME_BASELINE_FACTS_INVALID')
    if socket['state'] == 'PERMISSION_UNKNOWN':
        _require(set(socket) == {'state', 'path'}, 'RUNTIME_BASELINE_FACTS_INVALID')
    else:
        import stat
        fields = ('device', 'inode', 'mode', 'uid', 'gid', 'ctime_ns')
        _require(set(socket) == {'state', 'path', 'resolved_path', *fields}
                 and socket['resolved_path'] in ('/run/docker.sock', '/var/run/docker.sock')
                 and all(type(socket[name]) is int and socket[name] >= 0 for name in fields)
                 and socket['uid'] == 0 and stat.S_ISSOCK(socket['mode']) and not socket['mode'] & 0o002,
                 'RUNTIME_BASELINE_FACTS_INVALID')
    daemon = value['docker_daemon']
    _require(type(daemon) is dict and daemon.get('state') in ('OBSERVED', 'PERMISSION_UNKNOWN')
             and 'identity' in daemon and (daemon['identity'] is None or type(daemon['identity']) is str
                 and re.fullmatch(r'sha256:[0-9a-f]{64}', daemon['identity'])), 'RUNTIME_BASELINE_FACTS_INVALID')
    if daemon['state'] == 'PERMISSION_UNKNOWN':
        _require(set(daemon) == {'state', 'identity'}, 'RUNTIME_BASELINE_FACTS_INVALID')
    else:
        _require(set(daemon) == {'state', 'identity', 'server_version', 'storage_root'}
                 and type(daemon['server_version']) is str and 0 < len(daemon['server_version']) <= 512
                 and daemon['storage_root'] == '/var/lib/docker', 'RUNTIME_BASELINE_FACTS_INVALID')
    return json.loads(_canonical(value))


def compare_runtime_baseline_facts(expected, actual):
    expected = confirmed_baseline_facts(expected)
    actual = confirmed_baseline_facts(actual)
    _require(actual['tools'] == expected['tools'], 'RUNTIME_BASELINE_TOOL_CHANGED')
    _require(actual['docker_socket']['state'] == 'KNOWN_LOCAL', 'RUNTIME_ROOT_SOCKET_REQUIRED')
    if expected['docker_socket']['state'] == 'KNOWN_LOCAL':
        _require(actual['docker_socket'] == expected['docker_socket'], 'RUNTIME_BASELINE_SOCKET_CHANGED')
    _require(actual['docker_daemon']['state'] == 'OBSERVED'
             and actual['docker_daemon']['identity'] is not None, 'RUNTIME_ROOT_DAEMON_REQUIRED')
    if expected['docker_daemon']['identity'] is not None:
        _require(actual['docker_daemon']['identity'] == expected['docker_daemon']['identity'],
                 'RUNTIME_BASELINE_DAEMON_CHANGED')
    if expected['docker_daemon']['state'] == 'OBSERVED':
        _require(all(actual['docker_daemon'][name] == expected['docker_daemon'][name]
                     for name in ('server_version', 'storage_root')), 'RUNTIME_BASELINE_DAEMON_CHANGED')
    return actual


def observe_runtime_baseline_facts(*, expected, runner, timeout):
    """Real root readback; caller supplies its existing bounded local runner."""
    expected = confirmed_baseline_facts(expected)
    tools, socket = observe_runtime_tools(), observe_runtime_socket()
    _require(tools == expected['tools'], 'RUNTIME_BASELINE_TOOL_CHANGED')
    _require(socket['state'] == 'KNOWN_LOCAL', 'RUNTIME_ROOT_SOCKET_REQUIRED')
    if expected['docker_socket']['state'] == 'KNOWN_LOCAL':
        _require(socket == expected['docker_socket'], 'RUNTIME_BASELINE_SOCKET_CHANGED')
    _require(callable(timeout), 'RUNTIME_ROOT_DEADLINE_REQUIRED')

    def command(argv):
        completed = runner.run(argv, timeout=timeout())
        _require(completed.returncode == 0 and not completed.stderr
                 and type(completed.stdout) in (str, bytes), 'RUNTIME_ROOT_DAEMON_REQUIRED')
        try:
            raw = completed.stdout.encode('utf-8', errors='strict') if type(completed.stdout) is str else completed.stdout
        except UnicodeError:
            raise RuntimeBoundaryError('RUNTIME_ROOT_DAEMON_REQUIRED') from None
        _require(len(raw) <= 65536, 'RUNTIME_ROOT_DAEMON_REQUIRED')
        return raw

    identity = runtime_daemon_identity(command(['/usr/bin/systemctl', 'show', '--property=MainPID',
        '--property=ExecMainStartTimestampMonotonic', '--value', 'docker']))
    daemon = {'state': 'OBSERVED', 'identity': identity}
    for field, expression in (('server_version', '{{.ServerVersion}}'), ('storage_root', '{{.DockerRootDir}}')):
        try:
            daemon[field] = command(['/usr/bin/docker', '--host', 'unix:///var/run/docker.sock',
                'info', '--format', expression]).decode('utf-8', errors='strict').strip()
        except UnicodeError:
            raise RuntimeBoundaryError('RUNTIME_ROOT_DAEMON_REQUIRED') from None
    # Re-observe after the commands as well; a replacement during readback is
    # not evidence for the previously confirmed executable or local socket.
    _require(observe_runtime_tools() == tools and observe_runtime_socket() == socket,
             'RUNTIME_BASELINE_CHANGED_DURING_ROOT_OBSERVATION')
    return compare_runtime_baseline_facts(expected, {'tools': tools, 'docker_socket': socket,
                                                    'docker_daemon': daemon})


def runtime_baseline_failure_diagnostic(value):
    """Bounded, rejected observations only; no paths, raw output or authority."""
    value = value if type(value) is dict else {}
    package_names = ('docker.io', 'docker-compose-v2', 'postgresql-client-16')
    version_names = ('docker', 'systemctl', 'compose', 'pg_dump', 'psql')
    tool_names = ('python', 'docker', 'systemctl', 'compose', 'dpkg', 'dpkg-query', 'pg_dump', 'psql')
    codes = {
        'RUNTIME_TRANSPORT_CODEC_UNAVAILABLE', 'TRUSTED_RUNTIME_TOOL_MISSING',
        'PYTHON_EXECUTABLE_MISMATCH', 'DOCKER_SERVICE_INACTIVE',
        'LOCAL_DOCKER_SOCKET_MISSING_OR_UNTRUSTED', 'PACKAGE_ARCHITECTURE_MISMATCH',
        'COMPOSE_MAJOR_MISMATCH', 'DOCKER_DAEMON_IDENTITY_INVALID',
        'DOCKER_DAEMON_IDENTITY_UNAVAILABLE', 'DOCKER_DAEMON_UNAVAILABLE',
        'RUNTIME_DISTRIBUTION_MISMATCH',
        *('RUNTIME_PACKAGE_MISSING:' + name for name in package_names),
        *('RUNTIME_TOOL_VERSION_UNAVAILABLE:' + name for name in version_names),
        'POSTGRES_CLIENT_MAJOR_MISMATCH:pg_dump', 'POSTGRES_CLIENT_MAJOR_MISMATCH:psql',
    }

    def mapping(item):
        return item if type(item) is dict else {}

    def category(item, allowed):
        return item if type(item) is str and item in allowed else (
            'UNAVAILABLE' if item is None else 'UNRECOGNIZED')

    def version(text, name=None):
        valid = type(text) is str and 0 < len(text) <= 512
        match = re.search(r'\b(?:v)?([0-9]{1,4})(?=\.|\b)', text) if valid else None
        required = 16 if name in ('pg_dump', 'psql') else 2 if name == 'compose' else None
        major = runtime_tool_version_major(name, text) if required else int(match[1]) if match else None
        return {'text_valid': valid, 'major': major,
                'required_major_matches': major == required if required else None}

    missing = value.get('hard_missing')
    limited_missing = missing[:32] if type(missing) is list else []
    tools, packages, versions = (mapping(value.get(key)) for key in ('tools', 'packages', 'versions'))
    probes = {
        ('/usr/bin/systemctl', 'is-active', '--quiet', 'docker'): 'docker_service',
        ('/usr/bin/dpkg', '--print-architecture'): 'package_architecture',
        ('/usr/bin/systemctl', 'show', '--property=MainPID',
         '--property=ExecMainStartTimestampMonotonic', '--value', 'docker'): 'docker_daemon_identity',
        ('/usr/bin/docker', '--version'): 'docker_version',
        ('/usr/bin/systemctl', '--version'): 'systemctl_version',
        ('/usr/bin/docker', '--host', 'unix:///var/run/docker.sock', 'compose', 'version'): 'compose_version',
        ('/usr/bin/pg_dump', '--version'): 'pg_dump_version',
        ('/usr/bin/psql', '--version'): 'psql_version',
    }
    probes.update({('/usr/bin/dpkg-query', '--show',
        r'--showformat=${db:Status-Abbrev}\t${binary:Package}\t${Version}\t${Architecture}\n', name):
        'package:' + name for name in package_names})
    probes.update({('/usr/bin/docker', '--host', 'unix:///var/run/docker.sock', 'info', '--format', expression):
        label for expression, label in (('{{.ServerVersion}}', 'docker_server_version'),
                                         ('{{.DockerRootDir}}', 'docker_storage_root'))})
    commands = value.get('command_results')
    returncodes = {label: None for label in probes.values()}
    for item in commands[:32] if type(commands) is list else ():
        item = mapping(item)
        argv = item.get('argv')
        if (type(argv) is list and len(argv) <= 8
                and all(type(arg) is str and len(arg) <= 512 for arg in argv)):
            label, code = probes.get(tuple(argv)), item.get('returncode')
            if label is not None and type(code) is int and -(2**31) <= code < 2**31:
                returncodes[label] = code
    return {
        'schema': 'animemo.runtime-baseline-failure/v1',
        'failure_code': 'RUNTIME_BASELINE_HARD_PREREQUISITE_MISSING',
        'observation_accepted': False,
        'hard_missing': [item for item in limited_missing if type(item) is str and item in codes],
        'hard_missing_unrecognized_or_limited': type(missing) is not list or len(missing) > 32
            or any(type(item) is not str or item not in codes for item in limited_missing),
        'docker_service_active': value.get('docker_service_active')
            if type(value.get('docker_service_active')) is bool else None,
        'docker_socket_state': category(mapping(value.get('docker_socket')).get('state'),
            ('MISSING', 'UNTRUSTED', 'KNOWN_LOCAL', 'PERMISSION_UNKNOWN')),
        'docker_daemon_state': category(mapping(value.get('docker_daemon')).get('state'),
            ('OBSERVED', 'PERMISSION_UNKNOWN', 'UNAVAILABLE')),
        'package_architecture': category(value.get('package_architecture'),
            ('amd64', 'all', 'arm64', 'armhf', 'i386')),
        'tools': {name: 'REPORTED' if type(tools.get(name)) is dict else (
            'UNAVAILABLE' if tools.get(name) is None else 'UNRECOGNIZED') for name in tool_names},
        'package_set_valid': set(packages) == set(package_names),
        'packages': {name: {'fields_valid': type(packages.get(name)) is dict
            and set(packages[name]) == {'version', 'architecture'},
            'version': version(mapping(packages.get(name)).get('version')),
            'architecture': category(mapping(packages.get(name)).get('architecture'),
                ('amd64', 'all', 'arm64', 'armhf', 'i386'))} for name in package_names},
        'version_set_valid': set(versions) == set(version_names),
        'versions': {name: version(versions.get(name), name) for name in version_names},
        'command_results_present': type(commands) is list and bool(commands),
        'command_results_limited': type(commands) is list and len(commands) > 32,
        'probe_returncodes': returncodes,
    }


def validate_runtime_baseline(value, *, expected_binding, now):
    fields = {'schema', 'binding', 'machine_id', 'boot_id', 'uid', 'euid', 'platform',
              'architecture', 'distribution', 'observed_utc_seconds', 'tools',
              'docker_service_active', 'docker_socket', 'docker_daemon', 'package_architecture',
              'packages', 'versions', 'command_results', 'hard_missing', 'root_observations_pending'}
    _require(type(value) is dict and set(value) == fields
             and value['schema'] == 'animemo.runtime-unprivileged-baseline/v1'
             and value['binding'] == expected_binding, 'RUNTIME_BASELINE_BINDING_MISMATCH')
    _require(value['machine_id'] == expected_binding['machine_id']
             and value['boot_id'] == expected_binding['boot_id'], 'RUNTIME_BASELINE_GUEST_CHANGED')
    _require(_number(now) and _number(value['observed_utc_seconds'])
             and -5 <= now - value['observed_utc_seconds'] <= BASELINE_MAX_AGE_SECONDS,
             'RUNTIME_BASELINE_STALE')
    _require(type(value['uid']) is int and type(value['euid']) is int
             and value['uid'] > 0 and value['euid'] > 0
             and value['platform'] == 'linux' and value['architecture'] in ('x86_64', 'amd64')
             and value['distribution'] == {'ID': 'ubuntu', 'VERSION_ID': '24.04'},
             'RUNTIME_BASELINE_UNPRIVILEGED_REQUIRED')
    _require(value['hard_missing'] == [] and value['docker_service_active'] is True,
             'RUNTIME_BASELINE_HARD_PREREQUISITE_MISSING')
    tools = value['tools']
    _require(type(tools) is dict and set(tools) == {'python', 'docker', 'systemctl', 'compose',
                                                 'dpkg', 'dpkg-query', 'pg_dump', 'psql'},
             'RUNTIME_BASELINE_TOOL_UNTRUSTED')
    for name, tool in tools.items():
        expected_paths = ('/usr/libexec/docker/cli-plugins/docker-compose',
            '/usr/lib/docker/cli-plugins/docker-compose') if name == 'compose' else (
                '/usr/bin/python3' if name == 'python' else '/usr/bin/' + name,)
        _require(type(tool) is dict and set(tool) == {'path', 'resolved_path', 'sha256'}
                 and tool['path'] in expected_paths and type(tool['resolved_path']) is str
                 and tool['resolved_path'].startswith(('/usr/bin/', '/usr/lib/', '/usr/libexec/', '/usr/share/postgresql-common/'))
                 and type(tool['sha256']) is str and len(tool['sha256']) == 71
                 and tool['sha256'].startswith('sha256:')
                 and all(char in '0123456789abcdef' for char in tool['sha256'][7:]),
                 'RUNTIME_BASELINE_TOOL_UNTRUSTED')
    _require(value['package_architecture'] == 'amd64' and type(value['packages']) is dict
             and set(value['packages']) == {'docker.io', 'docker-compose-v2', 'postgresql-client-16'}
             and all(type(item) is dict and set(item) == {'version', 'architecture'}
                     and type(item['version']) is str and item['version']
                     and item['architecture'] in ('amd64', 'all') for item in value['packages'].values())
             and type(value['versions']) is dict
             and set(value['versions']) == {'docker', 'systemctl', 'compose', 'pg_dump', 'psql'}
             and type(value['command_results']) is list and value['command_results']
             and type(value['docker_daemon']) is dict
             and value['docker_daemon'].get('state') in ('OBSERVED', 'PERMISSION_UNKNOWN'),
             'RUNTIME_BASELINE_HARD_PREREQUISITE_MISSING')
    _require(all(type(text) is str and 0 < len(text) <= 512 for text in value['versions'].values())
             and all(runtime_tool_version_major(name, value['versions'][name]) == 16 for name in ('pg_dump', 'psql'))
             and runtime_tool_version_major('compose', value['versions']['compose']) == 2,
             'RUNTIME_BASELINE_HARD_PREREQUISITE_MISSING')
    commands = {('/usr/bin/systemctl', 'is-active', '--quiet', 'docker'),
        ('/usr/bin/dpkg', '--print-architecture'), ('/usr/bin/docker', '--version'),
        ('/usr/bin/systemctl', '--version'),
        ('/usr/bin/docker', '--host', 'unix:///var/run/docker.sock', 'compose', 'version'),
        ('/usr/bin/pg_dump', '--version'), ('/usr/bin/psql', '--version')}
    daemon_identity_command = ('/usr/bin/systemctl', 'show', '--property=MainPID',
        '--property=ExecMainStartTimestampMonotonic', '--value', 'docker')
    commands.add(daemon_identity_command)
    commands.update(('/usr/bin/dpkg-query', '--show',
        '--showformat=${db:Status-Abbrev}\\t${binary:Package}\\t${Version}\\t${Architecture}\\n', package)
        for package in ('docker.io', 'docker-compose-v2', 'postgresql-client-16'))
    daemon_commands = {('/usr/bin/docker', '--host', 'unix:///var/run/docker.sock', 'info', '--format', expression)
                       for expression in ('{{.ServerVersion}}', '{{.DockerRootDir}}')}
    actual_commands = []
    for item in value['command_results']:
        _require(type(item) is dict and set(item) == {'argv', 'returncode', 'started_at', 'ended_at',
                'stdout_sha256', 'stderr_sha256'} and type(item['argv']) is list
                and all(type(arg) is str for arg in item['argv']) and type(item['returncode']) is int
                and all(type(item[key]) is str and re.fullmatch(r'sha256:[0-9a-f]{64}', item[key])
                        for key in ('stdout_sha256', 'stderr_sha256')), 'RUNTIME_BASELINE_COMMAND_INVALID')
        argv = tuple(item['argv'])
        _require(argv in commands | daemon_commands and (item['returncode'] == 0 or argv in daemon_commands
                 or argv == daemon_identity_command and value['docker_daemon'].get('identity') is None),
                 'RUNTIME_BASELINE_COMMAND_INVALID')
        actual_commands.append(argv)
    _require(len(actual_commands) == len(set(actual_commands))
             and commands <= set(actual_commands), 'RUNTIME_BASELINE_COMMAND_INVALID')
    daemon = value['docker_daemon']
    if daemon['state'] == 'OBSERVED':
        _require(set(daemon) == {'state', 'identity', 'server_version', 'storage_root'}
                 and daemon['storage_root'] == '/var/lib/docker'
                 and type(daemon['server_version']) is str and daemon['server_version']
                 and daemon_commands <= set(actual_commands), 'RUNTIME_BASELINE_DOCKER_SCOPE_INVALID')
    else:
        _require(set(daemon) == {'state', 'identity'}, 'RUNTIME_BASELINE_DOCKER_SCOPE_INVALID')
    confirmed_baseline_facts(value)
    _require(type(value['docker_socket']) is dict
             and value['docker_socket'].get('state') in ('KNOWN_LOCAL', 'PERMISSION_UNKNOWN')
             and value['docker_socket'].get('path') == '/var/run/docker.sock'
             and value['root_observations_pending'] == ['effective_uid_zero', 'docker_daemon_identity',
                 'docker_daemon_access', 'current_full_installer_plan', 'instance_resource_ownership'],
             'RUNTIME_BASELINE_ROOT_OBSERVATION_REQUIRED')
    return {'baseline_digest': _digest(value), 'guest_identity': {
        name: expected_binding[name] for name in ('machine_id', 'boot_id', 'nonce', 'clone_identity', 'session_id')},
        'observation': json.loads(json.dumps(value))}


class ConfirmedRuntimeBoundary:
    def __init__(self, issuer, *, provider, plan, baseline, body, lifetime):
        _require(issuer is _CONFIRM_ISSUER, 'RUNTIME_BOUNDARY_CONFIRMATION_REQUIRED')
        self._provider, self._plan, self._lifetime = provider, plan, lifetime
        self._baseline, self._body = json.loads(json.dumps(baseline)), json.loads(json.dumps(body))
        self._body_digest = _digest(self._body)

    @property
    def body(self):
        return json.loads(json.dumps(self._body))

    def require_live(self, *, workload=False):
        if workload:
            self._lifetime.workload_timeout(MAX_BOOT_SECONDS)
        else:
            self._lifetime.check('CONFIRMED_BOUNDARY')
        _require(_digest(self._body) == self._body_digest
                 and self._provider._runtime_confirmed_boundary is self,
                 'RUNTIME_BOUNDARY_CHANGED')

    def __reduce__(self):
        raise TypeError('Runtime confirmations cannot be serialized')


def require_confirmed_runtime_boundary(provider, plan, *, workload=False):
    value = getattr(provider, '_runtime_confirmed_boundary', None)
    _require(type(value) is ConfirmedRuntimeBoundary and value._provider is provider
             and value._plan is plan, 'RUNTIME_BOUNDARY_CONFIRMATION_REQUIRED')
    value.require_live(workload=workload)
    return value


def run_before_capture(provider, plan, profile, lease, verified):
    from installer.development_boundary import build_runtime_operation_boundary
    from release.materials import reject_duplicate_json_keys
    from scripts import candidate_guest_session as c
    from scripts import candidate_vm_harness as h
    from scripts.development_guest_session import development_binding
    from scripts.development_source import require_development_source
    from scripts.guest_console_capture import WindowsConsoleCapture
    source = require_development_source(provider, plan)
    from pathlib import Path
    current_root = Path(__file__).resolve().parents[1]
    for relative in ('scripts/runtime_development_boundary.py', 'installer/apt_diagnostics.py'):
        _require((source.root / relative).read_bytes() == (current_root / relative).read_bytes(),
                 'RUNTIME_BASELINE_SOURCE_CHANGED')
    lifetime = require_runtime_lifetime(provider, plan, profile)
    authority = provider._active_profile_authority(profile, plan)
    _require(verified.authority == authority and lease.authority == authority,
             'RUNTIME_BASELINE_TARGET_CHANGED')
    _require(getattr(provider, '_runtime_confirmed_boundary', None) is None,
             'RUNTIME_BOUNDARY_CONFIRMATION_REPLAY')
    binding = baseline_binding(plan, profile, verified)
    command = fixed_baseline_command(binding)
    argv = provider._ssh_argv(authority, command, bootstrap_identity=True)
    c.validate_workload_command_budget((str(provider._tool_path(h.SSH)), *argv[1:]))
    lease.require_open()
    c._batch(provider, plan).require_active()
    lifetime.check('BASELINE', minimum_seconds=420)
    completed = provider._run(argv, code='RUNTIME_BASELINE_FAILED',
                              timeout=lifetime.clip_timeout(300), openssh=True)
    _require(len(completed.stdout) <= 65536 and not completed.stderr, 'RUNTIME_BASELINE_OUTPUT_INVALID')
    try:
        observed = json.loads(completed.stdout, object_pairs_hook=reject_duplicate_json_keys)
    except (ValueError, UnicodeError):
        raise RuntimeBoundaryError('RUNTIME_BASELINE_OUTPUT_INVALID') from None
    def validate():
        try:
            return validate_runtime_baseline(observed, expected_binding=binding, now=time.time())
        except RuntimeBoundaryError as error:
            if error.code == 'RUNTIME_BASELINE_HARD_PREREQUISITE_MISSING':
                provider._profile_operation_results[profile.profile]['runtime_baseline_failure'] = (
                    runtime_baseline_failure_diagnostic(observed))
            raise
    baseline = validate()
    current = provider._observe_guest_connection(authority,
        host_key_digest=provider._read_known_host_key(authority), bootstrap_identity=True)
    _require(current == verified.guest, 'RUNTIME_BASELINE_GUEST_CHANGED')
    if getattr(plan, 'runtime_baseline_only', False) is True:
        lease.require_open()
        c._batch(provider, plan).require_active()
        lifetime.check('BASELINE_DIAGNOSTIC_COMPLETE')
        provider._profile_operation_results[profile.profile]['runtime_baseline_diagnostic'] = {
            'schema': 'animemo.runtime-baseline-diagnostic/v1', 'baseline_validated': True,
            'baseline_digest': baseline['baseline_digest'], 'credential_capture_authorized': False,
            'installer_authorized': False}
        # Deliberately unwind the existing fatal-profile path. It soft-stops,
        # quarantines and retains the clone; it cannot reach capture/Installer.
        raise RuntimeBoundaryError('RUNTIME_BASELINE_DIAGNOSTIC_COMPLETE')
    from installer.development_trust import runtime_material_boundary
    material = runtime_material_boundary(getattr(provider, '_runtime_development_material', None))
    body = build_runtime_operation_boundary(binding=development_binding(plan), baseline=baseline,
                                           lifetime=lifetime.guest_envelope(), material=material)
    lifetime.check('BEFORE_BOUNDARY_CONFIRMATION', minimum_seconds=120)
    WindowsConsoleCapture().confirm_batch('PREPRODUCTION ONLY. Confirm the observed Runtime and exact operation boundary.\n'
        'This step does not read a password. Missing privileged observations remain pending.\n'
        + json.dumps(body, sort_keys=True, ensure_ascii=False, indent=2),
        timeout_seconds=min(60, lifetime.check('NATIVE_CONFIRMATION') - 120),
        cancelled=c._batch(provider, plan).cancelled)
    lifetime.check('BEFORE_CAPTURE', minimum_seconds=120)
    lease.require_open()
    c._batch(provider, plan).require_active()
    validate()
    current = provider._observe_guest_connection(authority,
        host_key_digest=provider._read_known_host_key(authority), bootstrap_identity=True)
    _require(current == verified.guest, 'RUNTIME_BASELINE_GUEST_CHANGED')
    provider._runtime_confirmed_boundary = ConfirmedRuntimeBoundary(_CONFIRM_ISSUER,
        provider=provider, plan=plan, baseline=baseline, body=body, lifetime=lifetime)
    provider._profile_operation_results[profile.profile]['runtime_baseline'] = baseline
    provider._profile_operation_results[profile.profile]['confirmed_runtime_boundary'] = body
    # The actual observed binding can exceed the construction-only fixture.
    # Measure the complete command before bootstrap returns to password capture.
    from scripts.development_guest_session import (
        preflight_development_workload_commands,
    )
    provider._profile_operation_results[profile.profile]['observed_workload_command_preflight'] = (
        preflight_development_workload_commands(provider, plan, _construction_only=False))
