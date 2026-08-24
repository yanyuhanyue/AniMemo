from __future__ import annotations

from __future__ import annotations

import gzip
import hashlib
import os
import re
import signal
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from .errors import CommandExited, CommandFailed, CommandStartFailed, CommandTimedOut
from .redaction import redact
from .state import _absolute, _ensure_private_directory

_ANSI_ESCAPE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")
_AUTH_SCHEME_SECRET = re.compile(r"(?i)\b(Bearer|Basic)\s+\S+")
_MAX_COMMAND_DIAGNOSTIC_CHARACTERS = 4096
_PROCESS_CLEANUP_SECONDS = 2.0


class _WindowsKillJob:
    def __init__(self, handle: int) -> None:
        self._handle = handle

    @classmethod
    def assign(cls, process: subprocess.Popen[bytes]) -> _WindowsKillJob | None:
        process_handle = getattr(process, "_handle", None)
        if os.name != "nt" or not isinstance(process_handle, int):
            return None
        import ctypes
        from ctypes import wintypes

        class BasicLimitInformation(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class IoCounters(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_ulonglong),
                ("WriteOperationCount", ctypes.c_ulonglong),
                ("OtherOperationCount", ctypes.c_ulonglong),
                ("ReadTransferCount", ctypes.c_ulonglong),
                ("WriteTransferCount", ctypes.c_ulonglong),
                ("OtherTransferCount", ctypes.c_ulonglong),
            ]

        class ExtendedLimitInformation(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", BasicLimitInformation),
                ("IoInfo", IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.SetInformationJobObject.argtypes = (
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        )
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL

        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        information = ExtendedLimitInformation()
        information.BasicLimitInformation.LimitFlags = 0x00002000
        if not kernel32.SetInformationJobObject(
            handle, 9, ctypes.byref(information), ctypes.sizeof(information)
        ):
            error = ctypes.get_last_error()
            kernel32.CloseHandle(handle)
            raise ctypes.WinError(error)
        if not kernel32.AssignProcessToJobObject(handle, process_handle):
            error = ctypes.get_last_error()
            kernel32.CloseHandle(handle)
            raise ctypes.WinError(error)
        return cls(int(handle))

    def terminate(self) -> None:
        if not self._handle:
            return
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)
        kernel32.TerminateJobObject.restype = wintypes.BOOL
        kernel32.TerminateJobObject(self._handle, 1)

    def close(self) -> None:
        if not self._handle:
            return
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.CloseHandle(self._handle)
        self._handle = 0


class _ProcessTree:
    def __init__(self, process: subprocess.Popen[bytes]) -> None:
        self.process = process
        self.windows_job = _WindowsKillJob.assign(process)
        self.closed = False

    def close_descendants(self) -> None:
        if self.closed:
            return
        if self.windows_job is not None:
            self.windows_job.close()
        elif os.name != "nt" and isinstance(getattr(self.process, "pid", None), int):
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        self.closed = True

    def terminate(self) -> None:
        if self.closed:
            return
        if self.windows_job is not None:
            self.windows_job.terminate()
        elif os.name != "nt" and isinstance(getattr(self.process, "pid", None), int):
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        try:
            self.process.kill()
        except (OSError, ProcessLookupError):
            pass
        try:
            self.process.wait(timeout=_PROCESS_CLEANUP_SECONDS)
        except (OSError, subprocess.SubprocessError):
            pass
        if self.windows_job is not None:
            self.windows_job.close()
        self.closed = True


def _join_output_threads(
    threads: tuple[threading.Thread, ...], *, deadline: float
) -> bool:
    for thread in threads:
        thread.join(timeout=max(0.0, deadline - time.monotonic()))
    return not any(thread.is_alive() for thread in threads)


def _bounded_diagnostic(value: object) -> str:
    if isinstance(value, bytes):
        text = value.decode("utf-8", "replace")
    elif value is None:
        text = ""
    else:
        text = str(value)
    cleaned = _ANSI_ESCAPE.sub("", text.replace("\x00", ""))
    cleaned = _AUTH_SCHEME_SECRET.sub(r"\1 [REDACTED]", cleaned)
    return redact(cleaned)[:_MAX_COMMAND_DIAGNOSTIC_CHARACTERS]


def _command_start_failure(executable: str, error: OSError) -> CommandStartFailed:
    if isinstance(error, FileNotFoundError):
        failure_class = "command_not_found"
    elif isinstance(error, PermissionError):
        failure_class = "permission_denied"
    else:
        failure_class = "operating_system_error"
    return CommandStartFailed(executable, failure_class)


class CommandRunner:
    """Run fixed argv vectors without a shell and return redacted failures."""

    def run(
        self,
        argv: list[str],
        *,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
        timeout: int = 300,
    ):
        executable = str(argv[0]) if argv else "<unknown>"
        try:
            return subprocess.run(
                list(argv),
                cwd=cwd,
                env=env,
                text=True,
                capture_output=True,
                timeout=timeout,
                check=True,
                shell=False,
            )
        except subprocess.TimeoutExpired as error:
            stdout = _bounded_diagnostic(error.stdout or error.output)
            stderr = _bounded_diagnostic(error.stderr)
            raise CommandTimedOut(executable, timeout, stdout, stderr) from error
        except subprocess.CalledProcessError as error:
            stdout = _bounded_diagnostic(error.stdout or error.output)
            stderr = _bounded_diagnostic(error.stderr)
            raise CommandExited(
                executable,
                int(error.returncode),
                stdout,
                stderr,
            ) from error
        except OSError as error:
            raise _command_start_failure(executable, error) from error
        except subprocess.SubprocessError as error:
            stdout = _bounded_diagnostic(getattr(error, "stdout", ""))
            stderr = _bounded_diagnostic(getattr(error, "stderr", ""))
            raise CommandFailed(
                f"command failed: {executable}; stdout={stdout}; stderr={stderr}",
                executable=executable,
                stdout=stdout,
                stderr=stderr,
            ) from error

    def write_gzip(
        self,
        argv: list[str],
        path: Path,
        *,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
        timeout: int = 600,
        root: Path | None = None,
    ) -> dict[str, object]:
        path = _absolute(path)
        if root is None:
            path.parent.mkdir(parents=True, exist_ok=True)
        else:
            _ensure_private_directory(root, path.parent)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
        )
        temporary = Path(temporary_name)
        digest = hashlib.sha256()
        uncompressed = 0
        process = None
        process_tree = None
        output_threads: tuple[threading.Thread, ...] = ()
        deadline = time.monotonic() + timeout
        try:
            os.chmod(temporary, 0o600)
            with os.fdopen(descriptor, "wb") as raw:
                descriptor = -1
                with gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as compressed:
                    executable = str(argv[0]) if argv else "<unknown>"
                    try:
                        process_options: dict[str, object] = {}
                        if os.name == "nt":
                            process_options["creationflags"] = (
                                subprocess.CREATE_NEW_PROCESS_GROUP
                            )
                        else:
                            process_options["start_new_session"] = True
                        process = subprocess.Popen(
                            list(argv),
                            cwd=cwd,
                            env=env,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE,
                            shell=False,
                            **process_options,
                        )
                        process_tree = _ProcessTree(process)
                    except OSError as error:
                        if process is not None:
                            try:
                                process.kill()
                                process.wait(timeout=_PROCESS_CLEANUP_SECONDS)
                            except (OSError, subprocess.SubprocessError):
                                pass
                        raise _command_start_failure(executable, error) from error
                    assert process.stdout is not None
                    stdout_errors: list[BaseException] = []
                    stderr_errors: list[BaseException] = []
                    stderr_chunks: list[bytes] = []
                    stderr_bytes = 0

                    def stream_stdout() -> None:
                        nonlocal uncompressed
                        try:
                            for chunk in iter(
                                lambda: process.stdout.read(1024 * 1024), b""
                            ):
                                uncompressed += len(chunk)
                                digest.update(chunk)
                                compressed.write(chunk)
                        except (EOFError, OSError, ValueError) as error:
                            stdout_errors.append(error)

                    def drain_stderr() -> None:
                        nonlocal stderr_bytes
                        if process.stderr is None:
                            return
                        try:
                            for chunk in iter(
                                lambda: process.stderr.read(64 * 1024), b""
                            ):
                                remaining = 16_384 - stderr_bytes
                                if remaining > 0:
                                    stderr_chunks.append(chunk[:remaining])
                                    stderr_bytes += min(len(chunk), remaining)
                        except (EOFError, OSError, ValueError) as error:
                            stderr_errors.append(error)

                    stdout_thread = threading.Thread(
                        target=stream_stdout,
                        name="animemo-command-stdout",
                        daemon=True,
                    )
                    stderr_thread = threading.Thread(
                        target=drain_stderr,
                        name="animemo-command-stderr",
                        daemon=True,
                    )
                    stdout_thread.start()
                    stderr_thread.start()
                    output_threads = (stdout_thread, stderr_thread)
                    try:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise subprocess.TimeoutExpired(list(argv), timeout)
                        return_code = process.wait(timeout=remaining)
                    except subprocess.TimeoutExpired as error:
                        assert process_tree is not None
                        process_tree.terminate()
                        _join_output_threads(
                            output_threads,
                            deadline=time.monotonic() + _PROCESS_CLEANUP_SECONDS,
                        )
                        stderr = _bounded_diagnostic(b"".join(stderr_chunks))
                        raise CommandTimedOut(
                            executable, timeout, "", stderr
                        ) from error
                    assert process_tree is not None
                    process_tree.close_descendants()
                    if not _join_output_threads(output_threads, deadline=deadline):
                        raise CommandTimedOut(executable, timeout, "", "")
                    if stdout_errors:
                        raise stdout_errors[0]
                    if stderr_errors:
                        raise stderr_errors[0]
                    stderr = _bounded_diagnostic(b"".join(stderr_chunks))
                    if return_code != 0:
                        raise CommandExited(executable, return_code, "", stderr)
                raw.flush()
                os.fsync(raw.fileno())
            os.replace(temporary, path)
            os.chmod(path, 0o600)
            if os.name != "nt":
                directory_fd = os.open(path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            return {"sha256": digest.hexdigest(), "uncompressedBytes": uncompressed}
        except BaseException:
            if process_tree is not None:
                process_tree.terminate()
            elif process is not None:
                try:
                    process.kill()
                    process.wait(timeout=_PROCESS_CLEANUP_SECONDS)
                except (OSError, subprocess.SubprocessError):
                    pass
            _join_output_threads(
                output_threads,
                deadline=time.monotonic() + _PROCESS_CLEANUP_SECONDS,
            )
            raise
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            if process is not None:
                if process.stdout is not None:
                    process.stdout.close()
                if process.stderr is not None:
                    process.stderr.close()
            if process_tree is not None:
                process_tree.close_descendants()
            temporary.unlink(missing_ok=True)
