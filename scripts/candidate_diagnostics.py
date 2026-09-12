"""Bounded, standard-library-only Candidate diagnostics; never a receipt authority."""
from __future__ import annotations

import json
import os
import re
import signal
import struct
import subprocess
import threading

SCHEMA = 'animemo.candidate-operation-diagnostic/v1'
MAX_DIAGNOSTIC_BYTES = 16 * 1024
MAX_EVENTS = 40
MAX_RECEIPT_BYTES = 8 * 1024 * 1024
STAGES = (
    'SSH_OBSERVED', 'SUDO_STARTED', 'ROOT_STARTED', 'MATERIAL_FINALIZING',
    'MATERIAL_VERIFIED', 'RUNTIME_INITIALIZING', 'RUNTIME_READY',
    'RUNNER_STARTING', 'RUNNER_STARTED', 'INSTALLER_STARTING',
    'PLATFORM_PREPARING', 'PLATFORM_READY', 'INSTALLER_RUNNING',
    'INSTALLER_COMPLETED', 'DRAFT_WRITING', 'DRAFT_WRITTEN', 'DRAFT_RETURNED',
    'HOST_PARSED',
)
COMPONENTS = ('SUDO', 'ROOT', 'RUNTIME_RUNNER', 'INSTALLER')
ERRORS = (
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
)
FD_ENV = 'ANIMEMO_CANDIDATE_DIAGNOSTIC_FD'
OP_ENV = 'ANIMEMO_CANDIDATE_DIAGNOSTIC_OPERATION'
_OPERATION = re.compile(r'sha256:[0-9a-f]{64}\Z')


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
