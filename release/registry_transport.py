from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from enum import Enum
from typing import NoReturn

from .dependency_images import (
    AUTHORITY,
    DependencyImage,
    DependencyImageAuthority,
    DependencyImageAuthorityError,
    compose_env_lines,
    github_env_lines,
    validate_dependency_image,
)

MAX_ATTEMPTS = 3
BACKOFF_SECONDS = (15, 45)
COMMAND_TIMEOUT_SECONDS = 180
INSPECT_TIMEOUT_SECONDS = 30
MAX_DIAGNOSTIC_CHARACTERS = 4096

_ANSI = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")
_HTTP_STATUS_PATTERNS = (
    re.compile(
        r"(?:http(?:/\d(?:\.\d)?)?\s+|http status(?: code)?\s*[:=]?\s*|"
        r"status(?: code)?\s*[:=]\s*)([0-9]{3})",
        re.IGNORECASE,
    ),
    re.compile(
        r"unexpected status from [^\r\n]*?:\s*([0-9]{3})\b",
        re.IGNORECASE,
    ),
)
_RETRYABLE_HTTP = frozenset((429, 500, 502, 503, 504))
_TERMINAL_PATTERNS = (
    "manifest unknown",
    "name unknown",
    "repository does not exist",
    "pull access denied",
    "authentication required",
    "no matching manifest",
    "invalid reference format",
    "digest mismatch",
    "wrong platform",
    "unsupported media type",
    "certificate signed by unknown authority",
    "x509:",
    "no space left on device",
    "disk full",
    "permission denied",
    "local filesystem error",
    "read-only file system",
)
_RETRYABLE_PATTERNS = (
    "connection reset by peer",
    "unexpected eof",
    "transport eof",
    "i/o timeout",
    "context deadline exceeded",
    "tls handshake timeout",
    "temporary failure in name resolution",
    "temporary dns resolution failure",
    "connection timed out",
)
_CACHE_MISS_PATTERNS = ("no such image", "no such object")


class DiagnosticClassification(str, Enum):
    RETRYABLE = "RETRYABLE"
    TERMINAL = "TERMINAL"


class DependencyImageTransportError(RuntimeError):
    def __init__(self, code: str, diagnostic: str = "") -> None:
        self.code = code
        self.diagnostic = diagnostic
        message = code if not diagnostic else f"{code}: {diagnostic}"
        super().__init__(message)


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: bytes
    stderr: bytes


@dataclass(frozen=True)
class PullReceipt:
    role: str
    reference: str
    platform: str
    source: str
    attempts: int
    repo_digest: str
    os: str
    architecture: str
    authority_identity: str


RunCommand = Callable[[tuple[str, ...], int], CommandResult]
Sleep = Callable[[int], None]


def _error(code: str, diagnostic: str = "") -> NoReturn:
    raise DependencyImageTransportError(code, diagnostic)


def sanitize_diagnostic(raw: bytes) -> str:
    if not isinstance(raw, bytes) or b"\x00" in raw:
        _error("DEPENDENCY_IMAGE_DOCKER_DAEMON_FAILURE")
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise DependencyImageTransportError(
            "DEPENDENCY_IMAGE_DOCKER_DAEMON_FAILURE"
        ) from error
    text = _ANSI.sub("", text)
    text = re.sub(
        r"(?im)^\s*authorization\s*:\s*[^\r\n]+",
        "Authorization: [REDACTED]",
        text,
    )
    text = re.sub(r"(?i)\bbearer\s+[^\s]+", "Bearer [REDACTED]", text)
    text = re.sub(r"(?i)\bbasic\s+[^\s]+", "Basic [REDACTED]", text)
    text = re.sub(
        r"(?i)([a-z][a-z0-9+.-]*://)[^/@\s:]+:[^/@\s]+@",
        r"\1[REDACTED]@",
        text,
    )
    text = re.sub(
        r"(?i)([?&](?:x-amz-signature|x-amz-credential|x-amz-security-token|"
        r"token|access_token|auth|signature)=)[^&\s]+",
        r"\1[REDACTED]",
        text,
    )
    text = "".join(
        character
        for character in text
        if character in "\n\r\t" or ord(character) >= 32
    ).strip()
    if len(text) > MAX_DIAGNOSTIC_CHARACTERS:
        marker = "[TRUNCATED]"
        text = marker + text[-(MAX_DIAGNOSTIC_CHARACTERS - len(marker)) :]
    return text


def classify_diagnostic(raw: bytes) -> DiagnosticClassification:
    diagnostic = sanitize_diagnostic(raw)
    lowered = diagnostic.lower()
    statuses = {
        int(match)
        for pattern in _HTTP_STATUS_PATTERNS
        for match in pattern.findall(diagnostic)
    }
    if any(pattern in lowered for pattern in _TERMINAL_PATTERNS):
        return DiagnosticClassification.TERMINAL
    if statuses and any(status not in _RETRYABLE_HTTP for status in statuses):
        return DiagnosticClassification.TERMINAL
    if statuses & _RETRYABLE_HTTP:
        return DiagnosticClassification.RETRYABLE
    if any(pattern in lowered for pattern in _RETRYABLE_PATTERNS):
        return DiagnosticClassification.RETRYABLE
    return DiagnosticClassification.TERMINAL


def _run_command(argv: tuple[str, ...], timeout_seconds: int) -> CommandResult:
    result = subprocess.run(
        list(argv),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=timeout_seconds,
        check=False,
    )
    return CommandResult(result.returncode, result.stdout, result.stderr)


def _command_diagnostic(result: CommandResult) -> bytes:
    return result.stdout + (b"\n" if result.stdout and result.stderr else b"") + result.stderr


def _validate_reference(image: DependencyImage, *, expected_role: str) -> None:
    try:
        validate_dependency_image(image)
    except DependencyImageAuthorityError as error:
        raise DependencyImageTransportError(
            "DEPENDENCY_IMAGE_REFERENCE_INVALID"
        ) from error
    if image.role != expected_role:
        _error("DEPENDENCY_IMAGE_REFERENCE_INVALID")


def _inspect_image(
    image: DependencyImage,
    *,
    run_command: RunCommand,
    cache_probe: bool,
) -> tuple[tuple[str, ...], str, str] | None:
    try:
        result = run_command(
            ("docker", "image", "inspect", image.reference),
            INSPECT_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as error:
        raise DependencyImageTransportError(
            "DEPENDENCY_IMAGE_DOCKER_DAEMON_FAILURE"
        ) from error
    if result.returncode != 0:
        diagnostic = sanitize_diagnostic(_command_diagnostic(result))
        if cache_probe and any(
            pattern in diagnostic.lower() for pattern in _CACHE_MISS_PATTERNS
        ):
            return None
        _error("DEPENDENCY_IMAGE_DOCKER_DAEMON_FAILURE", diagnostic)
    try:
        payload = json.loads(result.stdout.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise DependencyImageTransportError(
            "DEPENDENCY_IMAGE_DOCKER_DAEMON_FAILURE"
        ) from error
    if not isinstance(payload, list) or len(payload) != 1 or not isinstance(payload[0], dict):
        _error("DEPENDENCY_IMAGE_DOCKER_DAEMON_FAILURE")
    inspected = payload[0]
    repo_digests = inspected.get("RepoDigests")
    os_name = inspected.get("Os")
    architecture = inspected.get("Architecture")
    if (
        not isinstance(repo_digests, list)
        or not all(isinstance(item, str) for item in repo_digests)
        or not isinstance(os_name, str)
        or not isinstance(architecture, str)
    ):
        _error("DEPENDENCY_IMAGE_DOCKER_DAEMON_FAILURE")
    return tuple(repo_digests), os_name, architecture


def _verify_inspection(
    image: DependencyImage,
    inspection: tuple[tuple[str, ...], str, str],
    *,
    cache_probe: bool,
) -> tuple[str, str, str] | None:
    repo_digests, os_name, architecture = inspection
    if image.reference not in repo_digests:
        if cache_probe:
            return None
        _error("DEPENDENCY_IMAGE_LOCAL_DIGEST_MISMATCH")
    if os_name != "linux" or architecture != "amd64":
        if cache_probe:
            return None
        _error("DEPENDENCY_IMAGE_PLATFORM_MISMATCH")
    return image.reference, os_name, architecture


def pull_dependency_image(
    role: str,
    *,
    run_command: RunCommand = _run_command,
    sleep: Sleep = time.sleep,
    authority: DependencyImageAuthority | None = None,
) -> PullReceipt:
    authority = authority or AUTHORITY
    try:
        image = authority.image(role)
    except DependencyImageAuthorityError as error:
        raise DependencyImageTransportError(
            "DEPENDENCY_IMAGE_REFERENCE_INVALID"
        ) from error
    _validate_reference(image, expected_role=role)

    inspection = _inspect_image(image, run_command=run_command, cache_probe=True)
    if inspection is not None:
        verified = _verify_inspection(
            image, inspection, cache_probe=True
        )
        if verified is not None:
            repo_digest, os_name, architecture = verified
            return PullReceipt(
                role=role,
                reference=image.reference,
                platform=image.platform,
                source="CACHE_HIT_VERIFIED",
                attempts=0,
                repo_digest=repo_digest,
                os=os_name,
                architecture=architecture,
                authority_identity=authority.identity,
            )

    pull_argv = (
        "docker",
        "pull",
        "--platform",
        image.platform,
        "--quiet",
        image.reference,
    )
    completed_attempt = 0
    for attempt in range(1, MAX_ATTEMPTS + 1):
        completed_attempt = attempt
        try:
            result = run_command(pull_argv, COMMAND_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired as error:
            if attempt == MAX_ATTEMPTS:
                raise DependencyImageTransportError(
                    "DEPENDENCY_IMAGE_PULL_TIMEOUT"
                ) from error
            sleep(BACKOFF_SECONDS[attempt - 1])
            continue
        if result.returncode == 0:
            break
        raw_diagnostic = _command_diagnostic(result)
        diagnostic = sanitize_diagnostic(raw_diagnostic)
        if classify_diagnostic(raw_diagnostic) is DiagnosticClassification.TERMINAL:
            _error("DEPENDENCY_IMAGE_PULL_TERMINAL", diagnostic)
        if attempt == MAX_ATTEMPTS:
            _error("DEPENDENCY_IMAGE_PULL_TRANSIENT_EXHAUSTED", diagnostic)
        sleep(BACKOFF_SECONDS[attempt - 1])
    else:  # pragma: no cover - the closed loop always returns or raises
        _error("DEPENDENCY_IMAGE_PULL_TRANSIENT_EXHAUSTED")

    inspection = _inspect_image(image, run_command=run_command, cache_probe=False)
    if inspection is None:  # pragma: no cover - non-cache probes never return None
        _error("DEPENDENCY_IMAGE_LOCAL_DIGEST_MISMATCH")
    verified = _verify_inspection(
        image, inspection, cache_probe=False
    )
    if verified is None:  # pragma: no cover - post-pull mismatches raise
        _error("DEPENDENCY_IMAGE_LOCAL_DIGEST_MISMATCH")
    repo_digest, os_name, architecture = verified
    return PullReceipt(
        role=role,
        reference=image.reference,
        platform=image.platform,
        source="NETWORK_PULL_VERIFIED",
        attempts=completed_attempt,
        repo_digest=repo_digest,
        os=os_name,
        architecture=architecture,
        authority_identity=authority.identity,
    )


def pull_all_dependency_images(
    *,
    run_command: RunCommand = _run_command,
    sleep: Sleep = time.sleep,
    authority: DependencyImageAuthority | None = None,
    pull_image: Callable[..., object] | None = None,
) -> tuple[object, object]:
    authority = authority or AUTHORITY
    pull_image = pull_image or pull_dependency_image
    return tuple(
        pull_image(
            role,
            run_command=run_command,
            sleep=sleep,
            authority=authority,
        )
        for role in authority.roles
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Fetch and verify one canonical dependency image."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    pull = subparsers.add_parser("pull")
    pull.add_argument("--role", required=True, choices=("postgres", "redis"))
    pull_all = subparsers.add_parser("pull-all")
    pull_all.add_argument(
        "--projection",
        required=True,
        choices=("github-env", "compose-env"),
    )
    args = parser.parse_args(argv)
    expected_identity = os.environ.get("DEPENDENCY_IMAGE_AUTHORITY_SHA256")
    if expected_identity is not None and expected_identity != AUTHORITY.identity:
        print("DEPENDENCY_IMAGE_AUTHORITY_SNAPSHOT_MISMATCH", file=sys.stderr)
        return 1
    try:
        if args.command == "pull":
            receipt = pull_dependency_image(args.role, authority=AUTHORITY)
        else:
            pull_all_dependency_images(authority=AUTHORITY)
    except DependencyImageTransportError as error:
        print(str(error), file=sys.stderr)
        return 1
    if args.command == "pull":
        print(json.dumps(asdict(receipt), sort_keys=True, separators=(",", ":")))
    elif args.projection == "github-env":
        print("\n".join(github_env_lines(AUTHORITY)))
    else:
        print("\n".join(compose_env_lines(AUTHORITY)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
