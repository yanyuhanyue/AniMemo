from __future__ import annotations

import gzip
import hashlib
import os
import subprocess
from pathlib import Path

from .errors import CommandFailed
from .redaction import redact


class CommandRunner:
    """Run fixed argv vectors without a shell and return redacted failures."""

    def run(self, argv: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None, timeout: int = 300):
        try:
            return subprocess.run(
                list(argv),
                cwd=cwd,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
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
    ) -> dict[str, object]:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        digest = hashlib.sha256()
        uncompressed = 0
        try:
            with temporary.open("wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as compressed:
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
                compressed.flush()
            os.replace(temporary, path)
            os.chmod(path, 0o600)
            return {"sha256": digest.hexdigest(), "uncompressedBytes": uncompressed}
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
