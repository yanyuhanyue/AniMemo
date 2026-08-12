from __future__ import annotations

import gzip
import hashlib
import os
import subprocess
import tempfile
from pathlib import Path

from .errors import CommandFailed
from .redaction import redact
from .state import _absolute, _ensure_private_directory


class CommandRunner:
    """Run fixed argv vectors without a shell and return redacted failures."""

    def run(self, argv: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None, timeout: int = 300):
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
        except (OSError, subprocess.SubprocessError) as error:
            stdout = getattr(error, "stdout", "") or ""
            stderr = getattr(error, "stderr", "") or ""
            raise CommandFailed(redact(f"command failed: {argv[0]}; stdout={stdout}; stderr={stderr}")) from error

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
        try:
            os.chmod(temporary, 0o600)
            with os.fdopen(descriptor, "wb") as raw:
                descriptor = -1
                with gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as compressed:
                    process = subprocess.Popen(
                        list(argv),
                        cwd=cwd,
                        env=env,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        shell=False,
                    )
                    assert process.stdout is not None
                    for chunk in iter(lambda: process.stdout.read(1024 * 1024), b""):
                        uncompressed += len(chunk)
                        digest.update(chunk)
                        compressed.write(chunk)
                    stderr = process.stderr.read().decode("utf-8", "replace") if process.stderr else ""
                    if process.wait(timeout=timeout) != 0:
                        raise CommandFailed(redact(f"backup command failed: {stderr}"))
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
