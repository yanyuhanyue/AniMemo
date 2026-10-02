"""Independent-kit process ownership, adapted from scripts.candidate_child_process.

Windows starts suspended, assigns an unnamed job, then resumes the sole initial
thread. No process can spawn before job assignment. The job has no breakaway
flag and is closed on every exit. POSIX commands use a private process group.
"""
from __future__ import annotations

import ctypes
import os
import signal
import subprocess
import time


class ChildProcessError(RuntimeError):
    code = 'BOOTSTRAP_HTTP_PROCESS_SCOPE_FAILED'

    def __init__(self):
        super().__init__(self.code)


class _WindowsJob:
    def __init__(self):
        class Basic(ctypes.Structure):
            _fields_ = [('process_time', ctypes.c_int64), ('job_time', ctypes.c_int64),
                ('flags', ctypes.c_uint32), ('minimum', ctypes.c_size_t), ('maximum', ctypes.c_size_t),
                ('active', ctypes.c_uint32), ('affinity', ctypes.c_size_t),
                ('priority', ctypes.c_uint32), ('scheduling', ctypes.c_uint32)]
        class Counters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_uint64) for name in ('read_ops', 'write_ops', 'other_ops',
                'read_bytes', 'write_bytes', 'other_bytes')]
        class Limits(ctypes.Structure):
            _fields_ = [('basic', Basic), ('io', Counters), ('process_memory', ctypes.c_size_t),
                ('job_memory', ctypes.c_size_t), ('peak_process', ctypes.c_size_t), ('peak_job', ctypes.c_size_t)]
        class ThreadEntry(ctypes.Structure):
            _fields_ = [('size', ctypes.c_uint32), ('usage', ctypes.c_uint32), ('thread_id', ctypes.c_uint32),
                ('process_id', ctypes.c_uint32), ('base_priority', ctypes.c_int32),
                ('delta_priority', ctypes.c_int32), ('flags', ctypes.c_uint32)]
        self.thread_entry = ThreadEntry
        self.api = ctypes.WinDLL('kernel32', use_last_error=True)
        signatures = {
            'CreateJobObjectW': ([ctypes.c_void_p, ctypes.c_wchar_p], ctypes.c_void_p),
            'SetInformationJobObject': ([ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32], ctypes.c_int),
            'AssignProcessToJobObject': ([ctypes.c_void_p, ctypes.c_void_p], ctypes.c_int),
            'QueryInformationJobObject': ([ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p], ctypes.c_int),
            'TerminateJobObject': ([ctypes.c_void_p, ctypes.c_uint32], ctypes.c_int),
            'CloseHandle': ([ctypes.c_void_p], ctypes.c_int),
            'CreateToolhelp32Snapshot': ([ctypes.c_uint32, ctypes.c_uint32], ctypes.c_void_p),
            'Thread32First': ([ctypes.c_void_p, ctypes.POINTER(ThreadEntry)], ctypes.c_int),
            'Thread32Next': ([ctypes.c_void_p, ctypes.POINTER(ThreadEntry)], ctypes.c_int),
            'OpenThread': ([ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32], ctypes.c_void_p),
            'GetProcessIdOfThread': ([ctypes.c_void_p], ctypes.c_uint32),
            'ResumeThread': ([ctypes.c_void_p], ctypes.c_uint32),
        }
        for name, (arguments, result) in signatures.items():
            function = getattr(self.api, name)
            function.argtypes, function.restype = arguments, result
        self.handle = self.api.CreateJobObjectW(None, None)
        if not self.handle:
            raise ChildProcessError()
        limits = Limits()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE.
        if not self.api.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            self.close()
            raise ChildProcessError()

    def assign_and_resume(self, process):
        if not self.api.AssignProcessToJobObject(self.handle, int(process._handle)):
            raise ChildProcessError()
        snapshot = self.api.CreateToolhelp32Snapshot(0x4, 0)  # TH32CS_SNAPTHREAD.
        if snapshot in (None, ctypes.c_void_p(-1).value):
            raise ChildProcessError()
        ids = []
        entry = self.thread_entry()
        entry.size = ctypes.sizeof(entry)
        try:
            found = self.api.Thread32First(snapshot, ctypes.byref(entry))
            while found:
                if entry.process_id == process.pid:
                    ids.append(entry.thread_id)
                entry.size = ctypes.sizeof(entry)
                found = self.api.Thread32Next(snapshot, ctypes.byref(entry))
            if ctypes.get_last_error() != 18 or len(ids) != 1:  # ERROR_NO_MORE_FILES.
                raise ChildProcessError()
        finally:
            if not self.api.CloseHandle(snapshot):
                raise ChildProcessError()
        thread = self.api.OpenThread(0x0002 | 0x0800, False, ids[0])
        if not thread:
            raise ChildProcessError()
        try:
            if self.api.GetProcessIdOfThread(thread) != process.pid or self.api.ResumeThread(thread) != 1:
                raise ChildProcessError()
        finally:
            if not self.api.CloseHandle(thread):
                raise ChildProcessError()

    def active_processes(self):
        class Accounting(ctypes.Structure):
            _fields_ = [('user', ctypes.c_int64), ('kernel', ctypes.c_int64),
                ('period_user', ctypes.c_int64), ('period_kernel', ctypes.c_int64),
                ('page_faults', ctypes.c_uint32), ('total', ctypes.c_uint32),
                ('active', ctypes.c_uint32), ('terminated', ctypes.c_uint32)]
        value = Accounting()
        if not self.api.QueryInformationJobObject(self.handle, 1, ctypes.byref(value), ctypes.sizeof(value), None):
            raise ChildProcessError()
        return value.active

    def terminate(self):
        if not self.api.TerminateJobObject(self.handle, 1):
            raise ChildProcessError()

    def close(self):
        if self.handle is not None:
            if not self.api.CloseHandle(self.handle):
                raise ChildProcessError()
            self.handle = None


class OwnedProcess:
    """Internal launcher primitive. Callers expose only a fixed worker command."""
    def __init__(self, argv, **options):
        self.process, self.job = None, None
        self.closed = False
        self.receipt = None
        if os.name == 'nt':
            self.job = _WindowsJob()
            options['creationflags'] = 0x08000000 | 0x4
        elif os.name == 'posix':
            options['start_new_session'] = True
        else:
            raise ChildProcessError()
        try:
            self.process = subprocess.Popen(argv, **options)
            if self.job is not None:
                self.job.assign_and_resume(self.process)
        except BaseException as primary:
            try:
                self.stop_and_reap()
            except (OSError, subprocess.SubprocessError, ChildProcessError):
                primary.cleanup_failed = True
            if self.process is not None and self.process.poll() is not None:
                for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
                    if stream is not None:
                        try:
                            stream.close()
                        except OSError:
                            primary.cleanup_failed = True
                if os.name == 'nt':
                    try:
                        self.process._handle.Close()
                    except OSError:
                        primary.cleanup_failed = True
            if getattr(primary, 'cleanup_failed', False):
                # Preserve precise ownership for a containing seed/supervisor.
                # This is not a retry or output-consumption capability.
                primary._owned_process = self
            raise

    def stop_and_reap(self, seconds=5):
        if self.closed:
            return self.receipt
        deadline = time.monotonic() + seconds
        failures = []
        process = self.process
        try:
            if self.job is not None:
                self.job.terminate()
            elif process is not None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        except (OSError, ChildProcessError):
            failures.append('TERMINATE_FAILED')
        root_reaped, tree_empty = process is None, process is None
        if process is not None:
            try:
                # Covers assignment failure before the process entered its Job.
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=max(0, deadline - time.monotonic()))
                root_reaped = True
            except (OSError, subprocess.SubprocessError):
                failures.append('REAP_FAILED')
        if self.job is not None:
            try:
                while self.job.active_processes():
                    if time.monotonic() >= deadline:
                        raise ChildProcessError()
                    time.sleep(min(0.01, max(0, deadline - time.monotonic())))
                tree_empty = True
            except (OSError, ChildProcessError):
                failures.append('JOB_NOT_EMPTY_OR_QUERY_FAILED')
            try:
                self.job.close()
            except (OSError, ChildProcessError):
                failures.append('JOB_CLOSE_FAILED')
        elif process is not None:
            try:
                # POSIX group zero is independently observed, not inferred from root exit.
                while True:
                    try:
                        os.killpg(process.pid, 0)
                    except ProcessLookupError:
                        tree_empty = True
                        break
                    if time.monotonic() >= deadline:
                        failures.append('GROUP_NOT_EMPTY')
                        break
                    time.sleep(0.01)
            except OSError:
                failures.append('GROUP_QUERY_FAILED')
        self.receipt = {'pid': process.pid if process is not None else None,
            'root_reaped': root_reaped, 'tree_empty': tree_empty,
            'job_or_group': 'WINDOWS_JOB' if self.job is not None else 'POSIX_GROUP',
            'failures': tuple(failures)}
        self.closed = not failures and root_reaped and tree_empty
        if not self.closed:
            error = ChildProcessError()
            error.receipt = self.receipt
            raise error
        return self.receipt
