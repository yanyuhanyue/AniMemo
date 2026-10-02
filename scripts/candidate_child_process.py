"""Own command descendants without extending cancellation to VMware guests.

Windows starts suspended, assigns an unnamed job, then resumes the sole initial
thread. No process can spawn before job assignment. The job has no breakaway
flag and is closed on every exit. POSIX commands use a private process group.
"""
from __future__ import annotations

import ctypes
import os
import signal
import subprocess

from bootstrap_kit.owned_process import ChildProcessError as AccountedChildError


class ChildProcessError(AccountedChildError):
    code = 'CANDIDATE_CHILD_PROCESS_SCOPE_FAILED'

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
            'TerminateJobObject': ([ctypes.c_void_p, ctypes.c_uint32], ctypes.c_int),
            'QueryInformationJobObject': ([ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p], ctypes.c_int),
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

    def terminate(self):
        if not self.api.TerminateJobObject(self.handle, 1):
            raise ChildProcessError()

    def active_processes(self):
        # Reuse the existing Job accounting implementation; do not infer tree
        # completion from the root process exit or a successful CloseHandle.
        from bootstrap_kit.owned_process import _WindowsJob as AccountedJob
        return AccountedJob.active_processes(self)

    def close(self):
        if self.handle is not None:
            if not self.api.CloseHandle(self.handle):
                raise ChildProcessError()
            self.handle = None


class OwnedChild:
    """Private command lifetime; tree=False is reserved for vmrun containment."""
    def __init__(self, argv, *, tree=True, **options):
        self.tree, self.job, self.process = tree, None, None
        self.receipt = None
        self._closed = False
        if os.name == 'nt':
            options['creationflags'] = options.get('creationflags', 0) | 0x08000000
            if tree:
                self.job = _WindowsJob()
                options['creationflags'] |= 0x4  # CREATE_SUSPENDED.
        elif tree:
            options['start_new_session'] = True
        try:
            self.process = subprocess.Popen(argv, **options)
            if self.job is not None:
                self.job.assign_and_resume(self.process)
        except BaseException:
            self.close()
            raise

    def kill(self):
        if self.job is not None:
            self.job.terminate()
        elif self.tree and os.name == 'posix':
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        elif self.process.poll() is None:
            self.process.kill()

    def close(self):
        if self._closed:
            return self.receipt
        if self.tree:
            # This existing supervisor provides independently observed Job/group
            # emptiness and retains a precise failure receipt.
            from bootstrap_kit.owned_process import OwnedProcess
            owner = object.__new__(OwnedProcess)
            owner.process, owner.job = self.process, self.job
            owner.closed, owner.receipt = False, None
            try:
                self.receipt = owner.stop_and_reap()
            finally:
                self.receipt = owner.receipt
                if self.process is not None and self.process.poll() is not None:
                    for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
                        if stream is not None:
                            stream.close()
                    if os.name == 'nt':
                        self.process._handle.Close()
                self._closed = owner.closed
            return self.receipt
        try:
            if self.job is not None:
                self.job.close()
            elif self.process is not None:
                self.kill()
        finally:
            if self.process is not None:
                if self.process.poll() is None:
                    self.process.kill()  # Also handles failed assignment while suspended.
                self.process.wait(timeout=5)
                for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
                    if stream is not None:
                        stream.close()
                self.receipt = {'pid': self.process.pid, 'root_reaped': True,
                    'tree_empty': None, 'job_or_group': 'ROOT_ONLY', 'failures': ()}
                self._closed = True
        return self.receipt

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
