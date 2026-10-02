"""Independent-kit anonymous HTTP launcher; worker bytes carry no authority."""
from __future__ import annotations

import hashlib
import math
import os
import queue
import re
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from .http_protocol import (
    CONTROL_BYTES,
    PROGRESS_FRAMES,
    PROTOCOL,
    HttpFailure,
    HttpSelection,
    canonical,
    require,
    strict_json,
)
from .owned_process import ChildProcessError, OwnedProcess
from .egress import select_egress
from .safe_files import (
    KitFileError,
    create_private_directory,
    directory_identity,
    file_digest,
    held_file,
    remove_owned_directory,
)

READY_SECONDS = 30
CLEANUP_SECONDS = 5
_HEADER_KEYS = frozenset({'content-type', 'content-length', 'content-encoding',
    'transfer-encoding', 'link', 'deprecation', 'sunset'})
_FAILURE_CODES = frozenset({'PROTOCOL_INVALID', 'SELECTION_INVALID', 'HEADERS_INVALID', 'LENGTH_INVALID',
    'ENCODING_INVALID', 'STATUS_INVALID', 'DEADLINE', 'TOO_LARGE', 'CONTENT_TYPE_INVALID',
    'TRUNCATED', 'ASSET_BYTES_MISMATCH', 'REDIRECT_INVALID', 'NETWORK_CLOSE_FAILED',
    'NETWORK_FAILED', 'WORKER_FAILED', 'EGRESS_INVALID', 'EGRESS_BINDING_MISMATCH',
    'PROXY_CONNECT_EOF', 'PROXY_CONNECT_FAILED', 'PROXY_CONNECT_INVALID',
    'PROXY_HTTP_401', 'PROXY_HTTP_403', 'PROXY_HTTP_407', 'PROXY_HTTP_429',
    'TLS_IDENTITY_FAILED', 'TLS_HANDSHAKE_FAILED', 'TLS_POLICY_INVALID'})
_RETAINED_HTTP_RESOURCES = {}


class _RetainedHttpResources:
    """Strong ownership only; no retry or output-consumption interface."""
    def __init__(self, *, slot, identity, owned, reader, writer, reasons):
        self._slot, self._identity = slot, identity
        self._owned, self._reader, self._writer = owned, reader, writer
        self._reasons = tuple(reasons)


def retained_http_observations():
    """Safe identities, without private paths or resource-control handles."""
    return tuple({'slot_identity': identity,
        'pid': item._owned.process.pid if item._owned is not None and item._owned.process is not None else None,
        'state': 'PROCESS_OR_IO_CLEANUP_UNCONFIRMED', 'codes': item._reasons}
        for identity, item in _RETAINED_HTTP_RESOURCES.items())


def _command(nonce):
    # This origin is the independently pinned entry.pyz/kit, never a product tar.
    origin = str(Path(__file__).parent.parent)
    program = ('import sys;sys.path.insert(0,sys.argv.pop(1));'
        'from bootstrap_kit.http_worker import main;raise SystemExit(main())')
    return [sys.executable, '-I', '-S', '-B', '-c', program, origin, nonce]


class _ControlReader:
    def __init__(self, stream):
        self.frames = queue.Queue(maxsize=PROGRESS_FRAMES + 3)
        self.failed, self.eof = threading.Event(), threading.Event()
        self.thread = threading.Thread(target=self._read, args=(stream,), name='animemo-http-control')
        self.thread.start()

    def _read(self, stream):
        pending, size = bytearray(), 0
        try:
            while chunk := stream.read1(1024):
                size += len(chunk)
                if size > CONTROL_BYTES:
                    self.failed.set()
                    continue  # Drain until supervisor kills the owned process tree.
                pending.extend(chunk)
                while b'\n' in pending:
                    line, _, rest = pending.partition(b'\n')
                    pending[:] = rest
                    try:
                        self.frames.put_nowait(strict_json(line))
                    except (HttpFailure, queue.Full):
                        self.failed.set()
            if pending:
                self.failed.set()
        except (OSError, ValueError):
            self.failed.set()
        finally:
            self.eof.set()

    def next(self, *, deadline, cancel_event):
        while True:
            if cancel_event is not None and cancel_event.is_set():
                raise HttpFailure('CANCELLED')
            require(not self.failed.is_set(), 'CONTROL_INVALID')
            require(time.monotonic() < deadline, 'DEADLINE')
            try:
                return self.frames.get(timeout=min(0.02, max(0, deadline - time.monotonic())))
            except queue.Empty:
                require(not self.eof.is_set(), 'CONTROL_TRUNCATED')


class _ProgressState:
    """Validated observations only; never an output-consumption capability."""
    def __init__(self, nonce, selection, deadline):
        self.nonce, self.selection, self.deadline = nonce, selection, deadline
        self.cutoff = deadline
        self.sequence, self.latest, self.go = 0, None, None
        self.statuses = []
        self.invalid = False

    def accept(self, frame):
        fields = {'protocol', 'event', 'nonce', 'identity', 'sequence', 'phase',
                  'request_index', 'http_status', 'received_bytes', 'observed_monotonic'}
        require(type(frame) is dict and set(frame) == fields
            and frame['protocol'] == PROTOCOL and frame['event'] == 'PROGRESS'
            and frame['nonce'] == self.nonce and frame['identity'] == self.selection.identity,
            'CONTROL_INVALID')
        phase, index, status = frame['phase'], frame['request_index'], frame['http_status']
        size, observed = frame['received_bytes'], frame['observed_monotonic']
        require(type(frame['sequence']) is int and frame['sequence'] == self.sequence + 1
            and frame['sequence'] <= PROGRESS_FRAMES
            and type(phase) is str and phase in {'REQUEST', 'HEADERS', 'BODY', 'COMPLETE', 'FAILED'}
            and type(index) is int and index in (0, 1)
            and (index == 0 or self.selection.kind == 'GITHUB_ASSET')
            and (status is None or type(status) is int and 100 <= status <= 599)
            and type(size) is int and 0 <= size <= self.selection.maximum_bytes
            and type(observed) in (int, float) and self.go is not None
            and self.go <= observed <= min(self.cutoff, time.monotonic())
            and math.isfinite(observed),
            'CONTROL_INVALID')
        previous = self.latest
        require(previous is None or observed >= previous['observed_monotonic'], 'CONTROL_INVALID')
        if previous is None:
            require(phase == 'REQUEST' and index == 0, 'CONTROL_INVALID')
        elif index != previous['request_index']:
            require(index == 1 and previous['request_index'] == 0
                and previous['phase'] == 'HEADERS' and previous['http_status'] == 302
                and phase == 'REQUEST', 'CONTROL_INVALID')
        else:
            allowed = {'REQUEST': {'HEADERS', 'FAILED'}, 'HEADERS': {'BODY', 'COMPLETE', 'FAILED'},
                       'BODY': {'BODY', 'COMPLETE', 'FAILED'}, 'COMPLETE': {'FAILED'}, 'FAILED': set()}
            require(phase in allowed[previous['phase']]
                and size >= previous['received_bytes']
                and observed >= previous['observed_monotonic']
                and (previous['http_status'] is None or status == previous['http_status']), 'CONTROL_INVALID')
        require((phase != 'REQUEST' or status is None and size == 0)
            and (phase != 'HEADERS' or status is not None and size == 0)
            and (phase not in {'BODY', 'COMPLETE'} or status == 200), 'CONTROL_INVALID')
        self.sequence, self.latest = frame['sequence'], dict(frame)
        if phase == 'HEADERS':
            self.statuses.append({'request_index': index, 'status': status})

    def snapshot(self):
        value = self.latest or {}
        return {'phase': value.get('phase', 'UNKNOWN'), 'request_index': value.get('request_index'),
                'http_status': value.get('http_status'), 'received_bytes': value.get('received_bytes'),
                'received_bytes_kind': 'LAST_OBSERVED_LOWER_BOUND',
                'observed_monotonic': value.get('observed_monotonic'), 'persisted_bytes': None,
                'started_monotonic': self.go, 'deadline_monotonic': self.deadline,
                'http_responses': [dict(item) for item in self.statuses],
                'diagnostic_complete': not self.invalid and value.get('phase') in {'COMPLETE', 'FAILED'}}


class UntrustedHttpObject:
    """Held downloaded bytes; cryptographic verification remains mandatory."""
    authority = 'UNTRUSTED_HTTP_BYTES'

    def __init__(self, *, slot, slot_identity, selection, record, process_receipt):
        self.body_path, self._slot = slot / 'body.bin', slot
        self._slot_identity = slot_identity
        self._closed = False
        self.headers, self.status = dict(record['headers']), record['status']
        self.size, self.sha256 = record['size'], record['sha256']
        self.observations = tuple(record['observations'])
        self.process_receipt = process_receipt
        self._held = held_file(self.body_path, selection.maximum_bytes)
        self.stream = self._held.__enter__()
        try:
            actual_digest, actual_size = file_digest(self.stream)
            require(actual_size == self.size and actual_digest == 'sha256:' + self.sha256, 'OUTPUT_IDENTITY_MISMATCH')
            if selection.kind == 'GITHUB_ASSET':
                require(actual_size == selection.fields['size'] and self.sha256 == selection.fields['sha256'], 'ASSET_BYTES_MISMATCH')
            require(directory_identity(slot) == slot_identity and {p.name for p in slot.iterdir()} == {'body.bin'}, 'OUTPUT_INVENTORY_INVALID')
            self.stream.seek(0)
        except BaseException:
            self._held.__exit__(*sys.exc_info())
            raise

    def read_bytes(self, maximum=8 * 1024 * 1024):
        require(not self._closed and type(maximum) is int and 0 <= self.size <= maximum, 'TOO_LARGE')
        self.stream.seek(0)
        body = self.stream.read(self.size + 1)
        require(len(body) == self.size and hashlib.sha256(body).hexdigest() == self.sha256, 'OUTPUT_IDENTITY_MISMATCH')
        return body

    def close(self):
        if self._closed:
            return
        self._closed = True  # A rejected delete is never retried.
        error = None
        try:
            self._held.__exit__(None, None, None)
        except (OSError, KitFileError):
            error = HttpFailure('OUTPUT_CLOSE_FAILED')
        try:
            remove_owned_directory(self._slot, self._slot_identity)
        except (OSError, KitFileError):
            if error is None:
                error = HttpFailure('OUTPUT_CLEANUP_FAILED')
            else:
                error.secondary_errors = ('BOOTSTRAP_HTTP_OUTPUT_CLEANUP_FAILED',)
        if error is not None:
            raise error

    def __enter__(self):
        return self

    def __exit__(self, typ, value, traceback):
        try:
            self.close()
        except HttpFailure as error:
            if value is None:
                raise
            value.secondary_errors = tuple(getattr(value, 'secondary_errors', ())) + (error.code,)


def _check_observations(observations, selection, success, egress=None):
    egress = select_egress(egress)
    require(type(observations) is list and (1 if success else 0) <= len(observations) <= 2, 'CONTROL_INVALID')
    expected_host, expected_path, _ = selection.target()
    for index, item in enumerate(observations):
        fields = {'kind', 'host', 'path', 'status', 'authorization', 'cookie'}
        if egress.endpoint is not None:
            fields.add('egress')
        require(type(item) is dict and set(item) == fields
            and item['kind'] == selection.kind and item['authorization'] is False and item['cookie'] is False
            and type(item['status']) is int and 100 <= item['status'] <= 599, 'CONTROL_INVALID')
        if egress.endpoint is not None:
            require(item['egress'] == {**egress.record(), 'target': item['host'], 'connect': 'ESTABLISHED'},
                    'EGRESS_BINDING_MISMATCH')
        if index == 0:
            require(item['host'] == expected_host and item['path'] == expected_path.split('?', 1)[0], 'CONTROL_INVALID')
            if len(observations) == 2 or success:
                require(item['status'] == (302 if len(observations) == 2 else 200), 'CONTROL_INVALID')
        else:
            require(selection.kind == 'GITHUB_ASSET' and item['host'] == 'release-assets.githubusercontent.com'
                and type(item['path']) is str and re.fullmatch(r'/github-production-release-asset/1327429673/[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}', item['path'])
                and (not success or item['status'] == 200), 'CONTROL_INVALID')


def _check_result(record, nonce, selection, egress=None):
    common = {'protocol', 'event', 'nonce', 'identity', 'result'}
    require(type(record) is dict and record.get('protocol') == PROTOCOL
        and record.get('event') == 'RESULT' and record.get('nonce') == nonce
        and record.get('identity') == selection.identity, 'CONTROL_INVALID')
    if record.get('result') == 'FAILED':
        require(set(record) == common | {'code', 'observations'} and type(record['code']) is str
            and (record['code'] in _FAILURE_CODES or re.fullmatch(r'HTTP_[1-5][0-9]{2}', record['code'])), 'CONTROL_INVALID')
        _check_observations(record['observations'], selection, False, egress)
        status = int(record['code'][5:]) if record['code'].startswith('HTTP_') else None
        raise HttpFailure(record['code'], http_status=status, observations=record['observations'])
    require(set(record) == common | {'status', 'size', 'sha256', 'headers', 'observations'}
        and record['result'] == 'BYTES_WRITTEN' and type(record['status']) is int and record['status'] == 200
        and type(record['size']) is int and 0 <= record['size'] <= selection.maximum_bytes
        and type(record['sha256']) is str and re.fullmatch(r'[0-9a-f]{64}', record['sha256']), 'CONTROL_INVALID')
    headers = record['headers']
    require(type(headers) is dict and set(headers) <= _HEADER_KEYS
        and all(type(v) is str and len(v) <= 8192 and '\r' not in v and '\n' not in v and '\x00' not in v
                for v in headers.values()), 'CONTROL_INVALID')
    length, transfer = headers.get('content-length'), headers.get('transfer-encoding')
    require(length is None or re.fullmatch(r'0|[1-9][0-9]{0,19}', length)
        and int(length) == record['size'], 'OUTPUT_IDENTITY_MISMATCH')
    require(headers.get('content-encoding', 'identity').lower() == 'identity'
        and (transfer is None or transfer.lower() == 'chunked' and length is None), 'CONTROL_INVALID')
    allowed = {'application/json', 'application/vnd.github+json'}
    if selection.kind == 'GITHUB_ASSET':
        allowed = {'application/octet-stream', 'application/x-tar', 'application/zip'}
    elif selection.kind.startswith('TUF_'):
        allowed |= {'text/plain', 'application/octet-stream'}
    elif selection.kind == 'ACTIONS_BUNDLE':
        allowed |= {'application/x-snappy'}
    require(headers.get('content-type', '').split(';', 1)[0].strip().lower() in allowed, 'CONTROL_INVALID')
    _check_observations(record['observations'], selection, True, egress)


class SupervisedAnonymousHttp:
    def __init__(self, *, private_root, egress=None, offline=False):
        self.egress = select_egress(egress, offline=offline)
        self.offline = offline
        self.private_root = Path(private_root)
        self.last_process_receipt = None
        self.last_diagnostics = None

    def fetch(self, selection, *, deadline, cancel_event=None):
        self.last_process_receipt = None
        self.last_diagnostics = None
        egress = select_egress(self.egress, offline=self.offline)
        require(not self.offline, 'OFFLINE_NETWORK_FORBIDDEN')
        require(type(selection) is HttpSelection and type(deadline) in (float, int)
            and math.isfinite(deadline) and 0 < deadline - time.monotonic() <= 900, 'SELECTION_INVALID')
        # Roundtrip detaches mutable caller dictionaries before child creation.
        selection = HttpSelection.from_record(strict_json(canonical(selection.record())))
        if cancel_event is not None and cancel_event.is_set():
            raise HttpFailure('CANCELLED')
        slot = create_private_directory(self.private_root, prefix='animemo-http-')
        slot_identity = directory_identity(slot)
        owned, reader, writer, primary, result = None, None, None, None, None
        safe_to_remove = False
        nonce = uuid.uuid4().hex
        progress = _ProgressState(nonce, selection, deadline)
        times = {'creation_started': time.monotonic(), 'ready': None, 'go': None, 'reaped': None}
        try:
            environment = {key: str(slot) for key in ('HOME', 'USERPROFILE', 'TMP', 'TEMP', 'TMPDIR',
                'GH_CONFIG_DIR', 'XDG_CONFIG_HOME', 'XDG_CACHE_HOME', 'XDG_DATA_HOME')}
            environment.update(LANG='C.UTF-8', LC_ALL='C.UTF-8')
            if os.name == 'nt':
                environment['SystemRoot'] = os.environ.get('SystemRoot', 'C:\\Windows')
            command = _command(nonce)
            if egress.endpoint is not None:
                command += ['--egress-sha256', egress.identity]
            owned = OwnedProcess(command, cwd=slot, env=environment, stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, close_fds=True)
            reader = _ControlReader(owned.process.stdout)
            ready = reader.next(deadline=min(deadline, time.monotonic() + READY_SECONDS), cancel_event=cancel_event)
            expected_ready = {'protocol': PROTOCOL, 'event': 'READY', 'nonce': nonce}
            if egress.endpoint is not None:
                expected_ready['egress_identity'] = egress.identity
            require(ready == expected_ready, 'CONTROL_INVALID')
            times['ready'] = time.monotonic()
            request_seconds = 30
            if selection.kind == 'GITHUB_ASSET':
                request_seconds = min(900, max(60, 30 + (selection.fields['size'] + 131071) // 131072))
            deadline = min(deadline, time.monotonic() + request_seconds)
            progress.deadline = deadline
            progress.cutoff = deadline
            require(time.monotonic() < deadline, 'DEADLINE')
            go_record = {'protocol': PROTOCOL, 'event': 'GO', 'nonce': nonce, 'deadline': deadline,
                'selection': selection.record()}
            if egress.endpoint is not None:
                go_record['egress'] = egress.record()
            go = canonical(go_record)
            require(len(go) <= CONTROL_BYTES, 'PROTOCOL_INVALID')
            write_finished = threading.Event()
            write_errors = []
            def write_go():
                try:
                    owned.process.stdin.write(go)
                    owned.process.stdin.close()
                except (OSError, ValueError):
                    write_errors.append(True)
                finally:
                    write_finished.set()
            writer = threading.Thread(target=write_go, name='animemo-http-go')
            times['go'] = time.monotonic()
            progress.go = times['go']
            writer.start()
            while not write_finished.wait(0.01):
                require(time.monotonic() < deadline, 'DEADLINE')
                require(cancel_event is None or not cancel_event.is_set(), 'CANCELLED')
            require(not write_errors, 'CONTROL_WRITE_FAILED')
            while True:
                result = reader.next(deadline=deadline, cancel_event=cancel_event)
                require(time.monotonic() < deadline, 'DEADLINE')
                require(cancel_event is None or not cancel_event.is_set(), 'CANCELLED')
                if type(result) is dict and result.get('event') == 'PROGRESS':
                    progress.accept(result)
                    result = None
                else:
                    break
            while owned.process.poll() is None:
                require(time.monotonic() < deadline, 'DEADLINE')
                require(cancel_event is None or not cancel_event.is_set(), 'CANCELLED')
                require(not reader.failed.is_set(), 'CONTROL_INVALID')
                time.sleep(0.01)
            _check_result(result, nonce, selection, egress)
            require(owned.process.returncode == 0, 'WORKER_EXIT_FAILED')
        except BaseException as error:  # noqa: BLE001 - revoke output, reap the owned process, then preserve cancellation.
            primary = error
            progress.cutoff = min(progress.cutoff, time.monotonic())
            if isinstance(error, HttpFailure) and error.code == 'BOOTSTRAP_HTTP_CONTROL_INVALID':
                progress.invalid = True
        finally:
            cleanup = []
            cleanup_deadline = time.monotonic() + CLEANUP_SECONDS
            if owned is not None:
                try:
                    self.last_process_receipt = owned.stop_and_reap(max(0, cleanup_deadline - time.monotonic()))
                    times['reaped'] = time.monotonic()
                    self.last_process_receipt['monotonic_boundaries'] = times
                    safe_to_remove = True
                except (ChildProcessError, OSError, subprocess.SubprocessError) as error:
                    self.last_process_receipt = getattr(error, 'receipt', None)
                    cleanup.append('BOOTSTRAP_HTTP_PROCESS_CLEANUP_FAILED')
                if reader is not None:
                    reader.thread.join(timeout=max(0, cleanup_deadline - time.monotonic()))
                    if reader.thread.is_alive():
                        cleanup.append('BOOTSTRAP_HTTP_CONTROL_DRAIN_FAILED')
                        safe_to_remove = False
                    else:
                        while not reader.frames.empty():
                            frame = reader.frames.get_nowait()
                            if type(result) is dict and result.get('event') == 'RESULT':
                                # RESULT terminates the protocol, even if a later
                                # frame would otherwise be a valid diagnostic.
                                progress.invalid = True
                                continue
                            try:
                                progress.accept(frame)
                            except HttpFailure:
                                progress.invalid = True
                        if reader.failed.is_set() or progress.invalid:
                            progress.invalid = True
                            if primary is None:
                                primary = HttpFailure('CONTROL_INVALID')
                            else:
                                primary.secondary_errors = (*getattr(primary, 'secondary_errors', ()),
                                    'BOOTSTRAP_HTTP_CONTROL_INVALID')[:4]
                if writer is not None:
                    writer.join(timeout=max(0, cleanup_deadline - time.monotonic()))
                    if writer.is_alive():
                        cleanup.append('BOOTSTRAP_HTTP_CONTROL_WRITE_CLEANUP_FAILED')
                        safe_to_remove = False
                for stream in (owned.process.stdin, owned.process.stdout):
                    if stream is not None and (reader is None or not reader.thread.is_alive()) and (writer is None or not writer.is_alive()):
                        try:
                            stream.close()
                        except OSError:
                            cleanup.append('BOOTSTRAP_HTTP_PIPE_CLOSE_FAILED')
                if os.name == 'nt' and owned.process.poll() is not None:
                    try:
                        owned.process._handle.Close()
                    except OSError:
                        cleanup.append('BOOTSTRAP_HTTP_PROCESS_HANDLE_CLOSE_FAILED')
            else:
                safe_to_remove = not bool(getattr(primary, 'cleanup_failed', False))
                if not safe_to_remove:
                    cleanup.append('BOOTSTRAP_HTTP_PROCESS_CLEANUP_FAILED')
            if cleanup:
                safe_to_remove = False
                if primary is None:
                    primary = HttpFailure('CLEANUP_FAILED', cleanup=cleanup)
                else:
                    primary.secondary_errors = tuple(getattr(primary, 'secondary_errors', ())) + tuple(cleanup)
                retained = _RetainedHttpResources(slot=slot, identity=slot_identity,
                    owned=owned if owned is not None else getattr(primary, '_owned_process', None),
                    reader=reader, writer=writer, reasons=cleanup)
                _RETAINED_HTTP_RESOURCES[slot_identity] = retained
                primary._retained_http_resources = retained
        # Diagnostics are extracted after containment, without accepting bytes.
        # A terminal RESULT received before the exit wait can outlive its success
        # eligibility; retain only its fully validated observations.
        if type(result) is dict and result.get('event') == 'PROGRESS':
            try:
                progress.accept(result)
            except HttpFailure:
                progress.invalid = True
            result = None
        self.last_diagnostics = progress.snapshot()
        self.last_diagnostics['egress_selection'] = egress.record()
        self.last_diagnostics['ended_monotonic'] = time.monotonic()
        if not safe_to_remove:
            self.last_diagnostics['diagnostic_complete'] = False
        if result is not None:
            try:
                _check_result(result, nonce, selection, egress)
                observed_status, received = result['status'], result['size']
            except HttpFailure as diagnostic_error:
                observations = diagnostic_error.observations
                observed_status = observations[-1]['status'] if observations else None
                received = None
            if observed_status is not None:
                self.last_diagnostics['http_status'] = observed_status
            if received is not None:
                self.last_diagnostics['received_bytes'] = received
        if safe_to_remove and primary is not None:
            try:
                body = slot / 'body.bin'
                if body.exists():
                    with held_file(body, selection.maximum_bytes) as stream:
                        stream.seek(0, 2)
                        self.last_diagnostics['persisted_bytes'] = stream.tell()
            except (OSError, KitFileError):
                self.last_diagnostics['diagnostic_complete'] = False
        if primary is None:
            try:
                output = UntrustedHttpObject(slot=slot, slot_identity=slot_identity, selection=selection,
                    record=result, process_receipt=self.last_process_receipt)
                try:
                    require(time.monotonic() < deadline, 'DEADLINE')
                    require(cancel_event is None or not cancel_event.is_set(), 'CANCELLED')
                except HttpFailure as failure:
                    # Revoke cleanup ownership before the one close attempt:
                    # a rejected removal must not be retried by outer rollback.
                    safe_to_remove = False
                    try:
                        output.close()
                    except (HttpFailure, OSError, KitFileError):
                        failure.secondary_errors = (*failure.secondary_errors,
                            'BOOTSTRAP_HTTP_OUTPUT_CLOSE_FAILED')[:4]
                    raise
                return output
            except BaseException as error:  # noqa: BLE001 - held output must be revoked and cleaned before propagation.
                primary = error
        if safe_to_remove:
            try:
                remove_owned_directory(slot, slot_identity)
            except (OSError, KitFileError):
                primary.secondary_errors = tuple(getattr(primary, 'secondary_errors', ())) + ('BOOTSTRAP_HTTP_OUTPUT_CLEANUP_FAILED',)
        if isinstance(primary, (HttpFailure, KeyboardInterrupt, SystemExit)):
            primary.diagnostics = dict(self.last_diagnostics)
            raise primary
        failure = HttpFailure('LOCAL_FAILED', cleanup=getattr(primary, 'secondary_errors', ()))
        failure.diagnostics = dict(self.last_diagnostics)
        if getattr(primary, '_owned_process', None) is not None:
            failure._owned_process = primary._owned_process
            failure.cleanup_failed = True
        if getattr(primary, '_retained_http_resources', None) is not None:
            failure._retained_http_resources = primary._retained_http_resources
        raise failure from None
