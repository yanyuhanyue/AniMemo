from __future__ import annotations

import gzip
import hashlib
import os
import re
import subprocess
import tempfile
import threading
from pathlib import Path

from .errors import CommandExited, CommandFailed, CommandStartFailed, CommandTimedOut
from .redaction import redact
from .state import _absolute, _ensure_private_directory

_ANSI_ESCAPE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")
_AUTH_SCHEME_SECRET = re.compile(r"(?i)\b(Bearer|Basic)\s+\S+")
_MAX_COMMAND_DIAGNOSTIC_CHARACTERS = 4096


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
        output_threads: tuple[threading.Thread, ...] = ()
        try:
            os.chmod(temporary, 0o600)
            with os.fdopen(descriptor, "wb") as raw:
                descriptor = -1
                with gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as compressed:
                    executable = str(argv[0]) if argv else "<unknown>"
                    try:
                        process = subprocess.Popen(
                            list(argv),
                            cwd=cwd,
                            env=env,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE,
                            shell=False,
                        )
                    except OSError as error:
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
                    )
                    stderr_thread = threading.Thread(
                        target=drain_stderr,
                        name="animemo-command-stderr",
                    )
                    stdout_thread.start()
                    stderr_thread.start()
                    output_threads = (stdout_thread, stderr_thread)
                    try:
                        return_code = process.wait(timeout=timeout)
                    except subprocess.TimeoutExpired as error:
                        process.kill()
                        process.wait()
                        stdout_thread.join()
                        stderr_thread.join()
                        stderr = _bounded_diagnostic(b"".join(stderr_chunks))
                        raise CommandTimedOut(
                            executable, timeout, "", stderr
                        ) from error
                    stdout_thread.join()
                    stderr_thread.join()
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
        except Exception:
            if process is not None and process.poll() is None:
                process.kill()
                process.wait()
            for thread in output_threads:
                thread.join()
            raise
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            if process is not None:
                if process.stdout is not None:
                    process.stdout.close()
                if process.stderr is not None:
                    process.stderr.close()
            temporary.unlink(missing_ok=True)
