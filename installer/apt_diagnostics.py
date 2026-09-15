"""Bounded process capture and closed, non-secret APT observations.

Raw pipe bytes are transient and separately capped; only fixed classifications,
Ubuntu host classes and index classes are eligible for the diagnostic channel.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import signal
import subprocess
import threading
from dataclasses import dataclass
from datetime import datetime, timezone

STREAM_LIMIT = 1024 * 1024
LINE_LIMIT = 4096
CATEGORIES = {
    'SIGNATURE': (b'no_pubkey', b'not signed', b'invalid signature', b'signatures couldn'),
    'CERTIFICATE': (b'certificate verification failed', b'certificate is not trusted'),
    'SOURCE_IDENTITY': (b'does not have a release file', b'changed its', b'hash sum mismatch'),
    'LOCK': (b'could not get lock', b'unable to acquire the dpkg frontend lock'),
    'RATE_LIMIT': (b'429 too many requests', b'429  too many requests'),
    'DNS': (b'temporary failure resolving', b'could not resolve'),
    'NETWORK': (b'connection failed', b'connection timed out', b'could not connect'),
    'DISK': (b'no space left on device', b'input/output error', b'read-only file system'),
    'PARTIAL_INDEX': (b'some index files failed to download', b'old ones used instead'),
    'FETCH_FAILED': (b'failed to fetch',),
}
HOSTS = ('archive.ubuntu.com', 'security.ubuntu.com', 'ports.ubuntu.com')
INDEXES = ('InRelease', 'Release', 'Packages', 'Translation', 'DEP-11', 'Contents')


def utc_now():
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


class Capture:
    def __init__(self):
        self.raw = bytearray()
        self.pending = bytearray()
        self.discard_line = False
        self.bytes_seen = 0
        self.truncated = False
        self.read_error = False
        self.categories = set()
        self.hosts = set()
        self.indexes = set()

    def _line(self, line):
        lowered = line.lower()
        for category, markers in CATEGORIES.items():
            if any(marker in lowered for marker in markers):
                self.categories.add(category)
        # Never preserve a URL: even userinfo/query attached to an approved host
        # remain private. These enum values only indicate recognized host/index.
        for host in HOSTS:
            if re.search(rb'(?<![a-z0-9.-])' + host.encode() + rb'(?![a-z0-9.-])', lowered):
                self.hosts.add(host)
        for index in INDEXES:
            if re.search(rb'(?<![A-Za-z0-9])' + re.escape(index.encode()) + rb'(?![A-Za-z0-9])', line):
                self.indexes.add(index)

    def feed(self, block):
        self.bytes_seen += len(block)
        remaining = STREAM_LIMIT - len(self.raw)
        self.raw.extend(block[:remaining])
        self.truncated |= len(block) > remaining
        for part in block.splitlines(keepends=True):
            complete = part.endswith((b'\n', b'\r'))
            if not self.discard_line:
                if len(self.pending) + len(part) > LINE_LIMIT:
                    self.pending.clear()
                    self.discard_line = True
                    self.truncated = True
                else:
                    self.pending.extend(part)
            if complete:
                if not self.discard_line:
                    self._line(bytes(self.pending))
                self.pending.clear()
                self.discard_line = False

    def finish(self):
        if not self.discard_line and self.pending:
            self._line(bytes(self.pending))
        self.pending.clear()

    def public(self):
        return dict(bytes_seen=self.bytes_seen, truncated=self.truncated,
                    missing=self.read_error, categories=sorted(self.categories),
                    hosts=sorted(self.hosts), indexes=sorted(self.indexes))


@dataclass(frozen=True)
class CapturedProcess:
    returncode: int | None
    outcome: str
    stdout: bytes
    stderr: bytes
    started_at: str
    ended_at: str
    stdout_summary: dict
    stderr_summary: dict
    secondary_errors: tuple[str, ...]


def capture_process(argv, *, timeout, environment):
    """Drain both pipes concurrently in fixed chunks; never communicate()."""
    started = utc_now()
    captures = [Capture(), Capture()]
    errors = []
    process = None
    outcome = 'EXITED'
    workers = []

    def terminate(sig):
        if process is None:
            return
        try:
            if os.name == 'posix':
                os.killpg(process.pid, sig)
            elif process.poll() is None:
                process.kill()
        except ProcessLookupError:
            pass
        except OSError:
            errors.append('PROCESS_CLEANUP_FAILED')

    def drain(stream, capture):
        try:
            while block := stream.read(8192):
                capture.feed(block)
        except (OSError, ValueError):
            capture.read_error = True
        finally:
            capture.finish()

    try:
        process = subprocess.Popen(list(argv), cwd='/', env=dict(environment),
            shell=False, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, start_new_session=os.name == 'posix')
        for stream, capture in zip((process.stdout, process.stderr), captures):
            worker = threading.Thread(target=drain, args=(stream, capture), daemon=True)
            worker.start()
            workers.append(worker)
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        outcome = 'TIMEOUT'
    except (OSError, subprocess.SubprocessError):
        outcome = 'LAUNCH_FAILED' if process is None else 'PROCESS_ERROR'
    except (KeyboardInterrupt, SystemExit):
        outcome = 'CANCELLED'
    finally:
        if process is not None:
            if outcome != 'EXITED':
                terminate(signal.SIGTERM)
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
            # Close the dedicated group even if the leader exited but a child
            # retains a pipe. No group authority is derived from foreign PIDs.
            terminate(signal.SIGKILL if os.name == 'posix' else signal.SIGTERM)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                errors.append('PROCESS_CLEANUP_FAILED')
            for worker in workers:
                worker.join(timeout=5)
            for stream, capture, worker in zip((process.stdout, process.stderr), captures, workers):
                if worker.is_alive():
                    capture.read_error = True
                    errors.append('OUTPUT_DRAIN_FAILED')
                else:
                    stream.close()
    result = CapturedProcess(process.returncode if process is not None else None,
        outcome, bytes(captures[0].raw), bytes(captures[1].raw), started, utc_now(),
        captures[0].public(), captures[1].public(), tuple(sorted(set(errors))))
    for capture in captures:
        capture.raw[:] = b'\0' * len(capture.raw)
    return result


def apt_observation(argv, result, version):
    operation = 'SIMULATE' if '--simulate' in argv else 'UPDATE' if 'update' in argv else 'INSTALL'
    categories = sorted(set(result.stdout_summary['categories'] + result.stderr_summary['categories']))
    if result.outcome != 'EXITED':
        categories.append(result.outcome)
    if not categories:
        categories = ['NONE' if result.returncode == 0 else 'UNKNOWN']
    return dict(operation_class=operation, tool='/usr/bin/apt-get', tool_version=version,
        argv_contract='sha256:' + hashlib.sha256(json.dumps(list(argv), separators=(',', ':')).encode()).hexdigest(),
        started_at=result.started_at, ended_at=result.ended_at,
        returncode=result.returncode, outcome=result.outcome, categories=categories,
        stdout=result.stdout_summary, stderr=result.stderr_summary,
        secondary_errors=list(result.secondary_errors))
