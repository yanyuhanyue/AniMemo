"""Bounded, standard-library-only Candidate diagnostics; never a receipt authority."""
from __future__ import annotations

import json
import os
import re
import signal
import struct
import subprocess
import threading
from datetime import datetime

SCHEMA = 'animemo.candidate-operation-diagnostic/v1'
MAX_DIAGNOSTIC_BYTES = 16 * 1024
MAX_EVENTS = 40
MAX_RECEIPT_BYTES = 8 * 1024 * 1024
STAGES = (
    'SSH_OBSERVED', 'SUDO_STARTED', 'ROOT_STARTED', 'MATERIAL_FINALIZING',
    'MATERIAL_VERIFIED', 'RUNTIME_INITIALIZING', 'RUNTIME_READY',
    'RUNNER_STARTING', 'RUNNER_STARTED', 'INSTALLER_STARTING',
    'PLATFORM_PREPARING', 'PLATFORM_PLANNED', 'PLATFORM_READY', 'INSTALLER_RUNNING',
    'INSTALLER_COMPLETED', 'DRAFT_WRITING', 'DRAFT_WRITTEN', 'DRAFT_RETURNED',
    'HOST_PARSED',
)
COMPONENTS = ('SUDO', 'ROOT', 'RUNTIME_RUNNER', 'INSTALLER')
DOCTOR_CHECKS = (
    'instance.locator', 'filesystem.roots', 'filesystem.permissions', 'filesystem.capacity',
    'configuration.required', 'configuration.alignment', 'systemd.allowlist', 'compose.alignment',
    'network.listen', 'identity.public-origin', 'database.postgresql.connectivity',
    'database.schema-compatibility', 'cache.redis.connectivity', 'cache.redis.persistence-contract',
    'service.api.health', 'service.web.health', 'updater.socket', 'updater.state',
    'release.identity', 'release.updater-consistency', 'distribution.transport-policy',
    'distribution.transport-receipt', 'distribution.release-identity', 'distribution.oci-identity',
    'distribution.plan-receipt-drift', 'plugins.integrity', 'media.integrity',
    'backup.readiness', 'compatibility.state',
)
FAULT_MODULES = frozenset({
    'installer.production', 'installer.runtime', 'installer.operations',
    'updater.runtime', 'updater.deployment', 'updater.state', 'updater.commands',
    'updater.server', 'updater.source', 'updater.binding',
    'durability.instance', 'durability.ownership', 'durability.managed_config',
    'durability.private_store',
    'scripts.candidate_profile_runner', 'scripts.development_profile_runner',
    'installer.development', 'installer.platform_bootstrap', 'release.candidate',
})
FAULT_TYPES = frozenset({
    'StateError', 'LocatorError', 'CommandExited', 'CommandTimedOut', 'CommandStartFailed',
    'PermissionError', 'FileNotFoundError', 'OSError', 'ValueError', 'TypeError',
    'KeyError', 'AttributeError', 'RecoveryRequired', 'FreshInstallOperationError',
    'PrivateStoreError', 'ManagedConfigError', 'InstallerAdapterError', 'OTHER',
    'ProfileRunnerError', 'CandidateContractError', 'DevelopmentServiceError',
    'PlatformBootstrapError',
})
INSTALLER_FAILURE_CODES = (
    'INSTALL_ROOT_PREPARATION_FAILED', 'INSTALL_CONFIG_PUBLICATION_FAILED',
    'INSTALL_RELEASE_STAGING_FAILED', 'INSTALL_SERVICE_PREPARATION_FAILED',
    'INSTALL_DATABASE_MIGRATION_FAILED', 'INSTALL_BOOTSTRAP_FAILED',
    'INSTALL_RUNTIME_START_FAILED', 'INSTALL_RUNNING_RELEASE_INVALID',
    'INSTALL_UPDATER_ADOPTION_FAILED', 'INSTALL_DOCTOR_FAILED',
    'INSTALL_DOCTOR_INCOMPLETE', 'INSTALL_CANONICAL_ACCEPTANCE_FAILED',
    'INSTALL_SCOPED_CLEANUP_FAILED', 'INSTALL_RECOVERY_EVIDENCE_FAILED',
    'INSTALL_CANDIDATE_EXECUTION_OBSERVATION_UNAVAILABLE',
    'INSTALL_CANDIDATE_COMMAND_OBSERVATION_FAILED',
    'INSTALL_CANDIDATE_EGRESS_ISOLATION_UNVERIFIED',
    'INSTALL_CANDIDATE_IMAGE_READBACK_FAILED',
    'INSTALL_CANDIDATE_NETWORK_OBSERVATION_FAILED',
    'INSTALL_CANDIDATE_EXTERNAL_PULL_DETECTED',
)
BOOTSTRAP_FAILURE_CODES = (
    'CANDIDATE_BOOTSTRAP_RELEASE_BINDING_MISMATCH',
    'CANDIDATE_BOOTSTRAP_MATERIAL_UNAVAILABLE',
    'CANDIDATE_BOOTSTRAP_MATERIAL_IDENTITY_MISMATCH',
    'CANDIDATE_BOOTSTRAP_RUNTIME_MODULE_MISSING',
    'CANDIDATE_BOOTSTRAP_RUNTIME_MODULE_INVALID',
    'CANDIDATE_BOOTSTRAP_RUNTIME_MODULE_IDENTITY_MISMATCH',
    'CANDIDATE_BOOTSTRAP_DEVELOPMENT_SOURCE_INVALID',
    'CANDIDATE_BOOTSTRAP_TRUST_PROVISIONING_FAILED',
    'CANDIDATE_BOOTSTRAP_TRUST_RECEIPT_INVALID',
)
PLATFORM_FAILURE_CODES = (
    'PLATFORM_BOOTSTRAP_OS_UNSUPPORTED', 'PLATFORM_BOOTSTRAP_ARCH_UNSUPPORTED',
    'PLATFORM_BOOTSTRAP_ROOT_REQUIRED', 'PLATFORM_BOOTSTRAP_PACKAGE_MANAGER_UNAVAILABLE',
    'PLATFORM_BOOTSTRAP_PACKAGE_POLICY_INVALID', 'PLATFORM_BOOTSTRAP_APT_LOCK_TIMEOUT',
    'PLATFORM_BOOTSTRAP_APT_UPDATE_FAILED', 'PLATFORM_BOOTSTRAP_PACKAGE_UNAVAILABLE',
    'PLATFORM_BOOTSTRAP_DOCKER_INSTALL_FAILED', 'PLATFORM_BOOTSTRAP_COMPOSE_INSTALL_FAILED',
    'PLATFORM_BOOTSTRAP_POSTGRES_CLIENT_INSTALL_FAILED', 'PLATFORM_BOOTSTRAP_DOCKER_DAEMON_FAILED',
    'PLATFORM_BOOTSTRAP_HOST_STATE_INCONSISTENT', 'PLATFORM_BOOTSTRAP_OFFLINE_CAPABILITY_MISSING',
    'PLATFORM_BOOTSTRAP_PLAN_NOT_ACCEPTED', 'PLATFORM_BOOTSTRAP_PLAN_CHANGED',
    'PLATFORM_BOOTSTRAP_RECEIPT_INVALID', 'PLATFORM_BOOTSTRAP_POST_QUALIFICATION_FAILED',
    'PLATFORM_BOOTSTRAP_ALREADY_RUNNING',
)
RUNNER_FAILURE_CODES = (
    'CANDIDATE_INSTALLER_RESULT_INVALID', 'CANDIDATE_PROFILE_PLATFORM_STATE_MISMATCH',
    'CANDIDATE_PROFILE_EXECUTION_OBSERVATION_INVALID', 'CANDIDATE_PROFILE_DOCTOR_RECEIPT_MISMATCH',
    'CANDIDATE_PROFILE_DOCTOR_FAILED', 'CANDIDATE_PROFILE_CANONICAL_TEST_MISMATCH',
    'CANDIDATE_PROFILE_COMPLETED_STEPS_MISMATCH', 'CANDIDATE_PROFILE_DOCTOR_EXECUTION_MISMATCH',
    'CANDIDATE_PROFILE_EGRESS_ISOLATION_INVALID', 'CANDIDATE_PROFILE_NETWORK_OBSERVATION_INVALID',
    'CANDIDATE_PROFILE_EXTERNAL_PULL_ACTIVITY', 'CANDIDATE_PROFILE_IMAGE_OBSERVATION_MISMATCH',
    'CANDIDATE_PROFILE_RECEIPT_INVALID', 'CANDIDATE_SCHEMA_INVALID',
    'DEVELOPMENT_PROFILE_REPORT_INVALID', 'DEVELOPMENT_PROFILE_REPORT_DIGEST_INVALID',
    'DEVELOPMENT_PROFILE_BINDING_INVALID', 'DEVELOPMENT_MATERIAL_BINDING_INVALID',
)
ERRORS = (
    'APT_DIAGNOSTIC_WRITE_FAILED',
    'UNKNOWN_BEFORE_ROOT_START', 'ROOT_INITIALIZATION_FAILED',
    'MATERIAL_FINALIZATION_FAILED', 'MATERIAL_INVENTORY_MISMATCH',
    'RUNTIME_INITIALIZATION_FAILED', 'RUNNER_INITIALIZATION_FAILED',
    'RUNNER_EXECUTION_FAILED', 'PLATFORM_PREPARATION_FAILED',
    'INSTALLER_EXECUTION_FAILED', 'INSTALLER_OUTPUT_INVALID',
    'DRAFT_WRITE_FAILED', 'DRAFT_MISSING', 'DRAFT_INVALID',
    'TRANSPORT_PROTOCOL_INVALID', 'TRANSPORT_TRUNCATED',
    'TRANSPORT_LIMIT_EXCEEDED', 'TRANSPORT_INTERRUPTED', 'WORKLOAD_TIMEOUT',
    'ROOT_EXECUTION_FAILED', 'ROOT_PROCESS_FAILED',
    'PRODUCER_TOOLCHAIN_INVALID', 'VERIFIED_CANDIDATE_INVALID',
    'RUNNER_CONTEXT_INVALID', 'PROFILE_RECEIPT_INVALID',
    'PLATFORM_PACKAGE_POLICY_INVALID',
    'DEVELOPMENT_SERVICE_SOURCE_MISMATCH', 'DEVELOPMENT_INSTALLER_ENTRY_FAILED',
) + INSTALLER_FAILURE_CODES + BOOTSTRAP_FAILURE_CODES + PLATFORM_FAILURE_CODES + RUNNER_FAILURE_CODES
FD_ENV = 'ANIMEMO_CANDIDATE_DIAGNOSTIC_FD'
OP_ENV = 'ANIMEMO_CANDIDATE_DIAGNOSTIC_OPERATION'
_OPERATION = re.compile(r'sha256:[0-9a-f]{64}\Z')
APT_OUTCOMES = ('EXITED', 'TIMEOUT', 'CANCELLED', 'LAUNCH_FAILED', 'PROCESS_ERROR',
                'TOOL_VERSION_UNSUPPORTED')
APT_CATEGORIES = ('SIGNATURE', 'CERTIFICATE', 'SOURCE_IDENTITY', 'LOCK', 'RATE_LIMIT',
                  'DNS', 'NETWORK', 'DISK', 'PARTIAL_INDEX', 'FETCH_FAILED', 'NONE', 'UNKNOWN')
APT_HOSTS = ('archive.ubuntu.com', 'security.ubuntu.com', 'ports.ubuntu.com')
APT_INDEXES = ('InRelease', 'Release', 'Packages', 'Translation', 'DEP-11', 'Contents')
APT_SECONDARY_ERRORS = ('PROCESS_CLEANUP_FAILED', 'OUTPUT_DRAIN_FAILED')


class DiagnosticError(ValueError):
    def __init__(self, code='TRANSPORT_PROTOCOL_INVALID'):
        self.code = code if code in ERRORS else 'TRANSPORT_PROTOCOL_INVALID'
        super().__init__(self.code)


def _json(data):
    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                raise DiagnosticError()
            value[key] = item
        return value
    try:
        return json.loads(data, object_pairs_hook=pairs,
            parse_constant=lambda _: (_ for _ in ()).throw(DiagnosticError()))
    except (ValueError, UnicodeError):
        raise DiagnosticError() from None


def validate_apt_observation(value):
    """Closed fields only: raw APT text/URLs/environment are never transportable."""
    def enum_list(items, choices):
        return (type(items) is list and len(items) <= len(choices)
            and all(type(item) is str and item in choices for item in items)
            and len(set(items)) == len(items))

    fields = {'operation_class', 'tool', 'tool_version', 'argv_contract', 'started_at',
              'ended_at', 'returncode', 'outcome', 'categories', 'stdout', 'stderr', 'secondary_errors'}
    if type(value) is not dict or set(value) != fields:
        raise DiagnosticError()
    version, code = value['tool_version'], value['returncode']
    if not (value['operation_class'] in ('UPDATE', 'INSTALL', 'SIMULATE')
            and value['tool'] == '/usr/bin/apt-get'
            and (version is None or type(version) is str and re.fullmatch(
                r'[0-9]+\.[0-9]+(?:\.[0-9]+)?[a-zA-Z0-9.+~:-]{0,32}', version))
            and type(value['argv_contract']) is str and _OPERATION.fullmatch(value['argv_contract'])
            and (code is None or type(code) is int and -255 <= code <= 255)
            and value['outcome'] in APT_OUTCOMES
            and (value['outcome'] != 'EXITED' or code is not None)
            and enum_list(value['categories'], APT_CATEGORIES + APT_OUTCOMES[1:])
            and value['categories']
            and enum_list(value['secondary_errors'], APT_SECONDARY_ERRORS)):
        raise DiagnosticError()
    try:
        stamps = [value[name] for name in ('started_at', 'ended_at')]
        if not all(type(stamp) is str and re.fullmatch(
                r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?Z', stamp)
                for stamp in stamps):
            raise ValueError()
        if datetime.fromisoformat(stamps[0]) > datetime.fromisoformat(stamps[1]):
            raise ValueError()
    except ValueError:
        raise DiagnosticError() from None
    for name in ('stdout', 'stderr'):
        stream = value[name]
        if not (type(stream) is dict and set(stream) == {
                'bytes_seen', 'truncated', 'missing', 'categories', 'hosts', 'indexes'}
                and type(stream['bytes_seen']) is int and 0 <= stream['bytes_seen'] <= 2**63 - 1
                and type(stream['truncated']) is bool and type(stream['missing']) is bool
                and enum_list(stream['categories'], APT_CATEGORIES[:-2])
                and enum_list(stream['hosts'], APT_HOSTS)
                and enum_list(stream['indexes'], APT_INDEXES)):
            raise DiagnosticError()
    categories = set(value['stdout']['categories']) | set(value['stderr']['categories'])
    if value['outcome'] != 'EXITED':
        categories.add(value['outcome'])
    if not categories:
        categories.add('NONE' if code == 0 else 'UNKNOWN')
    if set(value['categories']) != categories:
        raise DiagnosticError()
    return value


def validate_event(value, operation):
    if type(operation) is not str or not _OPERATION.fullmatch(operation):
        raise DiagnosticError()
    common = {'schema', 'operation', 'kind'}
    if type(value) is not dict or value.get('schema') != SCHEMA or value.get('operation') != operation:
        raise DiagnosticError()
    kind = value.get('kind')
    if kind == 'STAGE':
        valid = set(value) == common | {'stage'} and value['stage'] in STAGES
    elif kind == 'EXIT':
        code = value.get('exit_code')
        valid = (set(value) == common | {'component', 'exit_code'}
            and value['component'] in COMPONENTS
            and type(code) is int and -255 <= code <= 255)
    elif kind == 'ERROR':
        valid = set(value) == common | {'code'} and value['code'] in ERRORS
    elif kind == 'DOCTOR':
        checks = value.get('failed_checks')
        valid = (set(value) == common | {'failed_checks'} and type(checks) is list
            and 1 <= len(checks) <= len(DOCTOR_CHECKS)
            and all(type(check) is str and check in DOCTOR_CHECKS for check in checks)
            and len(set(checks)) == len(checks))
    elif kind == 'FAULT':
        valid = (set(value) == common | {'module', 'line', 'category'}
            and type(value['module']) is str and value['module'] in FAULT_MODULES
            and type(value['line']) is int and 1 <= value['line'] <= 100000
            and type(value['category']) is str and value['category'] in FAULT_TYPES)
    elif kind == 'REPORT_COUNTS':
        fields = {'commands', 'pull_denied_commands', 'doctor_checks'}
        valid = (set(value) == common | fields
            and all(type(value[key]) is int and 0 <= value[key] <= MAX_RECEIPT_BYTES for key in fields))
    elif kind == 'APT':
        valid = set(value) == common | {'observation'}
        if valid:
            validate_apt_observation(value['observation'])
    else:
        valid = False
    if not valid:
        raise DiagnosticError()
    return value


class DiagnosticWriter:
    def __init__(self, fd, operation):
        if type(fd) is not int or fd < 0 or type(operation) is not str or not _OPERATION.fullmatch(operation):
            raise DiagnosticError()
        self.fd, self.operation = fd, operation

    def event(self, kind, **fields):
        value = validate_event(dict(schema=SCHEMA, operation=self.operation, kind=kind, **fields), self.operation)
        raw = (json.dumps(value, sort_keys=True, separators=(',', ':')) + '\n').encode()
        self.frame(b'D', raw)

    def stage(self, stage):
        self.event('STAGE', stage=stage)

    def error(self, code):
        self.event('ERROR', code=code)

    def exited(self, component, exit_code):
        self.event('EXIT', component=component, exit_code=exit_code)

    def fault(self, error):
        """Expose six bounded call sites, never exception text or locals."""
        seen = set()
        emitted = set()
        for _ in range(4):
            if error is None or id(error) in seen:
                break
            seen.add(id(error))
            trace, locations = error.__traceback__, []
            for _ in range(80):
                if trace is None:
                    break
                module = trace.tb_frame.f_globals.get('__name__')
                filename = trace.tb_frame.f_code.co_filename.replace('\\', '/')
                if module == '__main__' and filename.endswith('/scripts/development_profile_runner.py'):
                    module = 'scripts.development_profile_runner'
                if (type(module) is str and module in FAULT_MODULES
                        and filename.endswith('/' + module.replace('.', '/') + '.py')
                        and 1 <= trace.tb_lineno <= 100000):
                    locations.append((module, trace.tb_lineno))
                trace = trace.tb_next
            for module, line in reversed(locations[-3:]):
                if len(emitted) >= 6:
                    return
                if (module, line) in emitted:
                    continue
                emitted.add((module, line))
                category = type(error).__name__
                self.event('FAULT', module=module, line=line,
                    category=category if category in FAULT_TYPES else 'OTHER')
            error = error.__cause__ if error.__cause__ is not None else error.__context__

    def frame(self, kind, raw):
        limit = MAX_DIAGNOSTIC_BYTES if kind == b'D' else MAX_RECEIPT_BYTES
        if kind not in (b'D', b'R') or not 0 < len(raw) <= limit:
            raise DiagnosticError('TRANSPORT_LIMIT_EXCEEDED')
        data = memoryview(kind + struct.pack('!I', len(raw)) + raw)
        while data:
            written = os.write(self.fd, data)
            if written <= 0:
                raise DiagnosticError('TRANSPORT_INTERRUPTED')
            data = data[written:]


def inherited_writer():
    """Only a fixed launcher supplies this public descriptor and operation id."""
    fd, operation = os.environ.get(FD_ENV), os.environ.get(OP_ENV)
    if fd is None and operation is None:
        return None
    if fd is None or not re.fullmatch(r'[0-9]{1,6}', fd) or operation is None:
        raise DiagnosticError()
    return DiagnosticWriter(int(fd), operation)


class DiagnosticReader:
    def __init__(self, operation):
        if type(operation) is not str or not _OPERATION.fullmatch(operation):
            raise DiagnosticError()
        self.operation = operation
        self.events = []
        self.diagnostic_bytes = 0
        self.receipt = None
        self.error_code = None
        self._stages = set()
        self._exits = set()
        self._stage_index = -1

    def accept(self, kind, body):
        if kind == b'D':
            self.diagnostic_bytes += len(body)
            if self.diagnostic_bytes > MAX_DIAGNOSTIC_BYTES or len(self.events) >= MAX_EVENTS:
                raise DiagnosticError('TRANSPORT_LIMIT_EXCEEDED')
            event = validate_event(_json(body), self.operation)
            if event['kind'] == 'STAGE':
                index = STAGES.index(event['stage'])
                if event['stage'] == 'HOST_PARSED' or event['stage'] in self._stages or index <= self._stage_index:
                    raise DiagnosticError()
                self._stages.add(event['stage'])
                self._stage_index = index
            elif event['kind'] == 'EXIT':
                if event['component'] in self._exits:
                    raise DiagnosticError()
                self._exits.add(event['component'])
            self.events.append(event)
            return event
        if kind != b'R' or self.receipt is not None:
            raise DiagnosticError()
        value = _json(body)
        if type(value) is not dict:
            raise DiagnosticError('DRAFT_INVALID')
        self.receipt = value
        return None

    def public(self):
        stages = [e['stage'] for e in self.events if e['kind'] == 'STAGE']
        exits = {e['component']: e['exit_code'] for e in self.events if e['kind'] == 'EXIT'}
        errors = [e['code'] for e in self.events if e['kind'] == 'ERROR']
        return dict(schema=SCHEMA, operation=self.operation, events=list(self.events),
            last_stage=stages[-1] if stages else 'NOT_REACHED',
            root_started='ROOT_STARTED' in stages,
            exit_codes={component: exits.get(component) for component in COMPONENTS},
            transport_error=self.error_code, errors=errors,
            profile_draft_received=self.receipt is not None, host_receipt_parse='NOT_REACHED')


def read_frame(stream):
    """Read one bounded frame; never includes rejected bytes in an exception."""
    kind = stream.read(1)
    if kind == b'':
        return None
    if kind not in (b'D', b'R'):
        raise DiagnosticError()
    def exact(size):
        value = bytearray(size)
        offset = 0
        try:
            while offset < size:
                block = stream.read(size - offset)
                if not block:
                    raise DiagnosticError('TRANSPORT_TRUNCATED')
                value[offset:offset + len(block)] = block
                offset += len(block)
            return bytes(value)
        finally:
            value[:] = b'\0' * len(value)
    size = struct.unpack('!I', exact(4))[0]
    if not 0 < size <= (MAX_DIAGNOSTIC_BYTES if kind == b'D' else MAX_RECEIPT_BYTES):
        raise DiagnosticError('TRANSPORT_LIMIT_EXCEEDED')
    return kind, exact(size)


def bounded_process_output(argv, *, environment, timeout, pass_fds=()):
    """Drain only bounded structured stdout; child stderr never enters memory."""
    process = subprocess.Popen(argv, env=environment, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, pass_fds=pass_fds,
        start_new_session=os.name == 'posix')
    expired = threading.Event()
    def kill_group():
        try:
            if os.name == 'posix':
                os.killpg(process.pid, signal.SIGKILL)
            elif process.poll() is None:
                process.kill()
        except ProcessLookupError:
            pass
    def terminate():
        expired.set()
        kill_group()
    timer = threading.Timer(timeout, terminate)
    timer.daemon = True
    timer.start()
    output = bytearray()
    try:
        while True:
            block = process.stdout.read(min(65536, MAX_RECEIPT_BYTES + 1 - len(output)))
            if not block:
                break
            output.extend(block)
            if len(output) > MAX_RECEIPT_BYTES:
                raise DiagnosticError('TRANSPORT_LIMIT_EXCEEDED')
        code = process.wait(timeout=5)
        if expired.is_set():
            raise DiagnosticError('WORKLOAD_TIMEOUT')
        return code, bytes(output), b''
    finally:
        timer.cancel()
        if process.poll() is None:
            kill_group()
        process.wait(timeout=5)
        process.stdout.close()
        output[:] = b'\0' * len(output)
