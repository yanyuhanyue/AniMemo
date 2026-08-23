from __future__ import annotations

import argparse
import re
import subprocess
import sys
import time
from collections.abc import Callable


MAX_ATTEMPTS = 3
COMMAND_TIMEOUT_SECONDS = 120
BACKOFF_SECONDS = (2, 5)
MAX_DIAGNOSTIC_CHARACTERS = 4096
RETRYABLE_HTTP_STATUSES = frozenset({429, 502, 503, 504})

_TERMINAL_PATTERNS = (
    "access denied",
    "authentication required",
    "invalid reference format",
    "manifest unknown",
    "no matching manifest",
    "pull access denied",
    "repository does not exist",
    "unauthorized",
)

_TRANSIENT_PATTERNS = (
    "bad gateway",
    "connection reset by peer",
    "connection timed out",
    "context deadline exceeded",
    "gateway timeout",
    "i/o timeout",
    "net/http: request canceled",
    "server misbehaving",
    "service unavailable",
    "temporary failure in name resolution",
    "tls handshake timeout",
    "toomanyrequests",
    "unexpected eof",
)

_IMMUTABLE_IMAGE = re.compile(r"^[^@\s]+@sha256:[0-9a-f]{64}$")
_HTTP_STATUS = re.compile(
    r"\b(?:http(?:/[0-9.]+)?(?:\s+(?:status|code))?|status(?:\s+code)?)"
    r"\s*:?\s*([1-5][0-9]{2})\b",
    re.IGNORECASE,
)


def _validate_image(image: str) -> None:
    if not _IMMUTABLE_IMAGE.fullmatch(image):
        raise ValueError("Docker image must be an immutable sha256 digest reference")


def _diagnostic(completed: subprocess.CompletedProcess[str]) -> str:
    combined = "\n".join(part for part in (completed.stdout, completed.stderr) if part)
    return combined[-MAX_DIAGNOSTIC_CHARACTERS:]


def is_retryable_transport_failure(diagnostic: str) -> bool:
    normalized = diagnostic.casefold()
    if any(pattern in normalized for pattern in _TERMINAL_PATTERNS):
        return False
    http_statuses = tuple(int(status) for status in _HTTP_STATUS.findall(normalized))
    if http_statuses:
        return all(status in RETRYABLE_HTTP_STATUSES for status in http_statuses)
    return any(pattern in normalized for pattern in _TRANSIENT_PATTERNS)


def pull_image(
    image: str,
    *,
    run_command: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    sleep: Callable[[float], object] = time.sleep,
) -> bool:
    _validate_image(image)
    command = ["docker", "pull", "--quiet", image]

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            completed = run_command(
                command,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                check=False,
                timeout=COMMAND_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            retryable = True
            diagnostic = f"docker pull exceeded {COMMAND_TIMEOUT_SECONDS} seconds"
        else:
            if completed.returncode == 0:
                print(f"Pulled immutable image on attempt {attempt}/{MAX_ATTEMPTS}: {image}")
                return True
            diagnostic = _diagnostic(completed)
            retryable = is_retryable_transport_failure(diagnostic)

        classification = "retryable transport failure" if retryable else "terminal failure"
        print(
            f"Docker pull attempt {attempt}/{MAX_ATTEMPTS} failed ({classification}): "
            f"{diagnostic}",
            file=sys.stderr,
        )
        if not retryable or attempt == MAX_ATTEMPTS:
            return False
        sleep(BACKOFF_SECONDS[attempt - 1])

    raise AssertionError("bounded pull loop exhausted without returning")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Pull one immutable Docker image with bounded transport retries."
    )
    parser.add_argument("image")
    args = parser.parse_args(argv)
    try:
        succeeded = pull_image(args.image)
    except ValueError as error:
        parser.error(str(error))
    return 0 if succeeded else 1


if __name__ == "__main__":
    raise SystemExit(main())
