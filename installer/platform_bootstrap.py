"""Verified host-platform preparation before the canonical Installer plan.

The module owns one closed Ubuntu package policy and a small plan/execute
interface.  It may prepare host runtime capabilities, but it never enters an
AniMemo instance namespace and its receipt is not qualification evidence.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import platform as host_platform
import re
import shlex
import signal
import stat
import subprocess
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Protocol
from urllib.parse import urlsplit

from .runtime import InstallTransportSource

try:  # pragma: no cover - imported only on the production Linux host
    import fcntl
except ImportError:  # pragma: no cover - Windows contract tests inject a lock
    fcntl = None


PLAN_SCHEMA = "animemo.platform-bootstrap-plan/v1"
RECEIPT_SCHEMA = "animemo.platform-bootstrap-receipt/v1"
PACKAGE_POLICY_SCHEMA = "animemo.platform-package-policy/v1"
PLATFORM_BOOTSTRAP_LOCK = Path("/run/lock/animemo-platform-bootstrap.lock")
_UBUNTU_ARCHIVE_KEYRING_TEXT = "/usr/share/keyrings/ubuntu-archive-keyring.gpg"
_UBUNTU_ARCHIVE_KEYRING = Path(_UBUNTU_ARCHIVE_KEYRING_TEXT)
_UTC = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?Z\Z"
)
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_APT_COMMAND_TIMEOUT_SECONDS = 900
_APT_LOCK_TIMEOUT_SECONDS = 30
_APT_RETRIES = 2
_APT_INSTALL_TIMEOUT_RETRIES = 1
_COMMAND_TIMEOUT_SECONDS = 120
_SYSTEM_COMPOSE_PLUGIN_PATHS = (
    Path("/usr/libexec/docker/cli-plugins/docker-compose"),
    Path("/usr/lib/docker/cli-plugins/docker-compose"),
)
_SHADOW_COMPOSE_PLUGIN_PATHS = (
    Path("/usr/local/libexec/docker/cli-plugins/docker-compose"),
    Path("/usr/local/lib/docker/cli-plugins/docker-compose"),
)

PLATFORM_BOOTSTRAP_ERROR_CODES = frozenset(
    {
        "PLATFORM_BOOTSTRAP_OS_UNSUPPORTED",
        "PLATFORM_BOOTSTRAP_ARCH_UNSUPPORTED",
        "PLATFORM_BOOTSTRAP_ROOT_REQUIRED",
        "PLATFORM_BOOTSTRAP_PACKAGE_MANAGER_UNAVAILABLE",
        "PLATFORM_BOOTSTRAP_PACKAGE_POLICY_INVALID",
        "PLATFORM_BOOTSTRAP_APT_LOCK_TIMEOUT",
        "PLATFORM_BOOTSTRAP_APT_UPDATE_FAILED",
        "PLATFORM_BOOTSTRAP_PACKAGE_UNAVAILABLE",
        "PLATFORM_BOOTSTRAP_DOCKER_INSTALL_FAILED",
        "PLATFORM_BOOTSTRAP_COMPOSE_INSTALL_FAILED",
        "PLATFORM_BOOTSTRAP_POSTGRES_CLIENT_INSTALL_FAILED",
        "PLATFORM_BOOTSTRAP_DOCKER_DAEMON_FAILED",
        "PLATFORM_BOOTSTRAP_HOST_STATE_INCONSISTENT",
        "PLATFORM_BOOTSTRAP_OFFLINE_CAPABILITY_MISSING",
        "PLATFORM_BOOTSTRAP_PLAN_NOT_ACCEPTED",
        "PLATFORM_BOOTSTRAP_PLAN_CHANGED",
        "PLATFORM_BOOTSTRAP_RECEIPT_INVALID",
        "PLATFORM_BOOTSTRAP_POST_QUALIFICATION_FAILED",
        "PLATFORM_BOOTSTRAP_ALREADY_RUNNING",
    }
)


class PlatformBootstrapError(RuntimeError):
    def __init__(self, code: str) -> None:
        if code not in PLATFORM_BOOTSTRAP_ERROR_CODES:
            code = "PLATFORM_BOOTSTRAP_HOST_STATE_INCONSISTENT"
        super().__init__(code)
        self.code = code


def _reject(code: str) -> None:
    raise PlatformBootstrapError(code)


def _canonical_json_bytes(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def _sha256_identity(value: object) -> str:
    return "sha256:" + hashlib.sha256(_canonical_json_bytes(value)).hexdigest()


@dataclass(frozen=True)
class PlatformPackagePolicy:
    distribution_id: str
    distribution_major: str
    architecture: str
    docker_package: str
    compose_package: str
    postgres_client_package: str
    required_postgres_major: int
    allowed_provisioning_modes: tuple[str, ...]
    daemon_start_policy: str
    policy_version: int

    def body(self) -> dict[str, object]:
        return {
            "schemaVersion": PACKAGE_POLICY_SCHEMA,
            "distributionId": self.distribution_id,
            "distributionMajor": self.distribution_major,
            "architecture": self.architecture,
            "packageNames": [
                self.docker_package,
                self.compose_package,
                self.postgres_client_package,
            ],
            "requiredPostgresMajor": self.required_postgres_major,
            "allowedProvisioningModes": list(self.allowed_provisioning_modes),
            "daemonStartPolicy": self.daemon_start_policy,
            "policyVersion": self.policy_version,
        }

    @property
    def identity(self) -> str:
        return _sha256_identity(self.body())


PLATFORM_PACKAGE_POLICY = PlatformPackagePolicy(
    distribution_id="ubuntu",
    distribution_major="24.04",
    architecture="amd64",
    docker_package="docker.io",
    compose_package="docker-compose-v2",
    postgres_client_package="postgresql-client-16",
    required_postgres_major=16,
    allowed_provisioning_modes=(
        "ONLINE_FRESH",
        "ONLINE_EXISTING_DOCKER",
        "OFFLINE_VALIDATE_ONLY",
    ),
    daemon_start_policy="ENABLE_AND_START_ONLY_WHEN_DOCKER_WAS_ABSENT",
    policy_version=1,
)


class PlatformBootstrapMode(StrEnum):
    ONLINE_FRESH = "ONLINE_FRESH"
    ONLINE_EXISTING_DOCKER = "ONLINE_EXISTING_DOCKER"
    OFFLINE_VALIDATE_ONLY = "OFFLINE_VALIDATE_ONLY"


class PlatformBootstrapActionKind(StrEnum):
    APT_UPDATE = "APT_UPDATE"
    INSTALL_DOCKER = "INSTALL_DOCKER"
    INSTALL_COMPOSE = "INSTALL_COMPOSE"
    INSTALL_POSTGRES_CLIENT = "INSTALL_POSTGRES_CLIENT"
    ENABLE_DOCKER_DAEMON = "ENABLE_DOCKER_DAEMON"
    VALIDATE_ONLY = "VALIDATE_ONLY"


@dataclass(frozen=True)
class BootstrapHostFacts:
    distribution_id: str
    distribution_major: str
    architecture: str
    effective_uid: int
    apt_available: bool
    apt_sources_trusted: bool
    apt_sources_identity: str | None
    systemd_available: bool
    docker_cli_present: bool
    docker_cli_available: bool
    docker_cli_trusted: bool
    docker_cli_identity: str | None
    docker_service_active: bool
    docker_daemon_healthy: bool
    docker_daemon_identity: str | None
    docker_socket_present: bool
    docker_socket_local: bool
    docker_socket_identity: str | None
    compose_v2_present: bool
    compose_v2_available: bool
    compose_v2_identity: str | None
    docker_config_identity: str
    pg_dump_major: int | None
    psql_major: int | None
    installed_policy_packages: tuple[str, ...]

    def __post_init__(self) -> None:
        boolean_fields = (
            self.apt_available,
            self.apt_sources_trusted,
            self.systemd_available,
            self.docker_cli_present,
            self.docker_cli_available,
            self.docker_cli_trusted,
            self.docker_service_active,
            self.docker_daemon_healthy,
            self.docker_socket_present,
            self.docker_socket_local,
            self.compose_v2_present,
            self.compose_v2_available,
        )
        allowed_packages = frozenset(PLATFORM_PACKAGE_POLICY.body()["packageNames"])
        if (
            any(type(value) is not bool for value in boolean_fields)
            or type(self.effective_uid) is not int
            or isinstance(self.effective_uid, bool)
            or not all(
                type(value) is str and value
                for value in (
                    self.distribution_id,
                    self.distribution_major,
                    self.architecture,
                )
            )
            or type(self.installed_policy_packages) is not tuple
            or len(self.installed_policy_packages)
            != len(set(self.installed_policy_packages))
            or not set(self.installed_policy_packages).issubset(allowed_packages)
            or any(
                value is not None
                and (type(value) is not int or isinstance(value, bool) or value < 1)
                for value in (self.pg_dump_major, self.psql_major)
            )
            or (
                self.apt_sources_identity is not None
                and not _DIGEST.fullmatch(self.apt_sources_identity)
            )
            or (
                self.docker_cli_identity is not None
                and not _DIGEST.fullmatch(self.docker_cli_identity)
            )
            or (
                self.docker_daemon_identity is not None
                and not _DIGEST.fullmatch(self.docker_daemon_identity)
            )
            or (
                self.docker_socket_identity is not None
                and not _DIGEST.fullmatch(self.docker_socket_identity)
            )
            or (
                self.compose_v2_identity is not None
                and not _DIGEST.fullmatch(self.compose_v2_identity)
            )
            or self.docker_config_identity != "ABSENT"
            and not _DIGEST.fullmatch(self.docker_config_identity)
        ):
            _reject("PLATFORM_BOOTSTRAP_HOST_STATE_INCONSISTENT")

    def as_dict(self) -> dict[str, object]:
        return {
            "distributionId": self.distribution_id,
            "distributionMajor": self.distribution_major,
            "architecture": self.architecture,
            "effectiveUid": self.effective_uid,
            "aptAvailable": self.apt_available,
            "aptSourcesTrusted": self.apt_sources_trusted,
            "aptSourcesIdentity": self.apt_sources_identity,
            "systemdAvailable": self.systemd_available,
            "dockerCliPresent": self.docker_cli_present,
            "dockerCliAvailable": self.docker_cli_available,
            "dockerCliTrusted": self.docker_cli_trusted,
            "dockerCliIdentity": self.docker_cli_identity,
            "dockerServiceActive": self.docker_service_active,
            "dockerDaemonHealthy": self.docker_daemon_healthy,
            "dockerDaemonIdentity": self.docker_daemon_identity,
            "dockerSocketPresent": self.docker_socket_present,
            "dockerSocketLocal": self.docker_socket_local,
            "dockerSocketIdentity": self.docker_socket_identity,
            "composeV2Present": self.compose_v2_present,
            "composeV2Available": self.compose_v2_available,
            "composeV2Identity": self.compose_v2_identity,
            "dockerConfigIdentity": self.docker_config_identity,
            "pgDumpMajor": self.pg_dump_major,
            "psqlMajor": self.psql_major,
            "installedPolicyPackages": list(self.installed_policy_packages),
        }


@dataclass(frozen=True)
class PlatformBootstrapAction:
    kind: PlatformBootstrapActionKind
    packages: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        expected = {
            PlatformBootstrapActionKind.INSTALL_DOCKER: (
                PLATFORM_PACKAGE_POLICY.docker_package,
            ),
            PlatformBootstrapActionKind.INSTALL_COMPOSE: (
                PLATFORM_PACKAGE_POLICY.compose_package,
            ),
            PlatformBootstrapActionKind.INSTALL_POSTGRES_CLIENT: (
                PLATFORM_PACKAGE_POLICY.postgres_client_package,
            ),
        }.get(self.kind, ())
        if (
            type(self.kind) is not PlatformBootstrapActionKind
            or self.packages != expected
        ):
            _reject("PLATFORM_BOOTSTRAP_PACKAGE_POLICY_INVALID")

    def as_dict(self) -> dict[str, object]:
        return {"kind": self.kind.value, "packages": list(self.packages)}


_EXPECTED_CAPABILITIES = MappingProxyType(
    {
        "dockerCli": True,
        "dockerDaemon": True,
        "composeV2": True,
        "pgDumpMajor": PLATFORM_PACKAGE_POLICY.required_postgres_major,
        "psqlMajor": PLATFORM_PACKAGE_POLICY.required_postgres_major,
        "systemd": True,
    }
)


@dataclass(frozen=True)
class PlatformBootstrapPlan:
    mode: PlatformBootstrapMode
    transport_source: InstallTransportSource
    initial_capabilities: BootstrapHostFacts
    actions: tuple[PlatformBootstrapAction, ...]
    package_policy_identity: str
    expected_capabilities: Mapping[str, object]
    docker_daemon_policy: str
    network_policy: str
    created_at: str
    plan_digest: str

    def identity_body(self) -> dict[str, object]:
        return {
            "schemaVersion": PLAN_SCHEMA,
            "mode": self.mode.value,
            "transportSource": self.transport_source.value,
            "initialCapabilities": self.initial_capabilities.as_dict(),
            "actions": [action.as_dict() for action in self.actions],
            "packagePolicyIdentity": self.package_policy_identity,
            "expectedCapabilities": dict(self.expected_capabilities),
            "dockerDaemonPolicy": self.docker_daemon_policy,
            "networkPolicy": self.network_policy,
        }

    def as_dict(self) -> dict[str, object]:
        return {
            **self.identity_body(),
            "createdAt": self.created_at,
            "planDigest": self.plan_digest,
        }

    def canonical_bytes(self) -> bytes:
        return _canonical_json_bytes(self.as_dict())


@dataclass(frozen=True)
class PlatformBootstrapReceipt:
    plan_digest: str
    mode: PlatformBootstrapMode
    initial_capabilities: BootstrapHostFacts
    installed_packages: tuple[str, ...]
    preserved_packages: tuple[str, ...]
    final_capabilities: BootstrapHostFacts
    docker_daemon_before: str
    docker_daemon_after: str
    docker_daemon_restart_count: int
    package_policy_identity: str
    result: str
    receipt_digest: str

    def identity_body(self) -> dict[str, object]:
        return {
            "schemaVersion": RECEIPT_SCHEMA,
            "planDigest": self.plan_digest,
            "mode": self.mode.value,
            "initialCapabilities": self.initial_capabilities.as_dict(),
            "installedPackages": list(self.installed_packages),
            "preservedPackages": list(self.preserved_packages),
            "finalCapabilities": self.final_capabilities.as_dict(),
            "dockerDaemonBefore": self.docker_daemon_before,
            "dockerDaemonAfter": self.docker_daemon_after,
            "dockerDaemonRestartCount": self.docker_daemon_restart_count,
            "packagePolicyIdentity": self.package_policy_identity,
            "result": self.result,
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_body(), "receiptDigest": self.receipt_digest}

    def canonical_bytes(self) -> bytes:
        return _canonical_json_bytes(self.as_dict())


@dataclass(frozen=True)
class PlatformCommandResult:
    returncode: int
    stdout: bytes = b""
    stderr: bytes = b""


class PlatformCommandRunner(Protocol):
    def run(
        self,
        argv: tuple[str, ...],
        *,
        timeout: int,
        environment: Mapping[str, str],
    ) -> PlatformCommandResult: ...


class SubprocessPlatformCommandRunner:
    def run(
        self,
        argv: tuple[str, ...],
        *,
        timeout: int,
        environment: Mapping[str, str],
    ) -> PlatformCommandResult:
        process: subprocess.Popen[bytes] | None = None
        try:
            process = subprocess.Popen(
                list(argv),
                cwd="/",
                env=dict(environment),
                shell=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=os.name == "posix",
            )
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            if process is not None:
                try:
                    if os.name == "posix":
                        os.killpg(process.pid, signal.SIGTERM)
                    else:  # pragma: no cover - production host is POSIX
                        process.kill()
                    process.communicate(timeout=5)
                except (OSError, subprocess.SubprocessError):
                    pass
                finally:
                    try:
                        if os.name == "posix":
                            # The leader may already be reaped while a descendant
                            # that closed inherited pipes still lives in this PGID.
                            # Always close that residual process group after grace.
                            os.killpg(process.pid, signal.SIGKILL)
                        elif process.poll() is None:  # pragma: no cover - POSIX prod
                            process.kill()
                    except OSError:
                        pass
                    try:
                        process.communicate(timeout=5)
                    except (OSError, subprocess.SubprocessError):
                        pass
            return PlatformCommandResult(returncode=124, stderr=b"command timeout")
        except (OSError, subprocess.SubprocessError):
            if process is not None:
                try:
                    process.kill()
                    process.communicate(timeout=5)
                except (OSError, subprocess.SubprocessError):
                    pass
            return PlatformCommandResult(returncode=126, stderr=b"command failed")
        return PlatformCommandResult(
            returncode=process.returncode,
            stdout=stdout[: 1024 * 1024],
            stderr=stderr[: 1024 * 1024],
        )


_COMMAND_ENVIRONMENT = MappingProxyType(
    {
        "DEBIAN_FRONTEND": "noninteractive",
        "HOME": "/nonexistent",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
    }
)


def _command(
    runner: PlatformCommandRunner,
    argv: Sequence[str],
    *,
    timeout: int = _COMMAND_TIMEOUT_SECONDS,
) -> PlatformCommandResult:
    return runner.run(
        tuple(argv),
        timeout=timeout,
        environment=_COMMAND_ENVIRONMENT,
    )


def _trusted_root_regular(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except OSError:
        return False
    return (
        not path.is_symlink()
        and stat.S_ISREG(metadata.st_mode)
        and metadata.st_nlink == 1
        and (
            os.name != "posix" or metadata.st_uid == 0 and metadata.st_mode & 0o022 == 0
        )
    )


def _trusted_file_identity(path: Path, *, maximum_bytes: int) -> str | None:
    if not path.exists() and not path.is_symlink():
        return None
    if not _trusted_root_regular(path):
        return None
    try:
        before = path.stat()
        raw = path.read_bytes()
        after = path.stat()
    except OSError:
        return None
    if (
        len(raw) > maximum_bytes
        or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        or len(raw) != before.st_size
    ):
        return None
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _ubuntu_archive_uri(uri: str) -> bool:
    try:
        parsed = urlsplit(uri)
    except ValueError:
        return False
    host = (parsed.hostname or "").lower()
    return (
        parsed.scheme in {"http", "https"}
        and parsed.username is None
        and parsed.password is None
        and parsed.port in {None, 80, 443}
        and (
            host in {"archive.ubuntu.com", "security.ubuntu.com"}
            or host.endswith(".archive.ubuntu.com")
        )
        and (parsed.path == "/ubuntu" or parsed.path.startswith("/ubuntu/"))
        and not parsed.query
        and not parsed.fragment
    )


def _read_trusted_text(path: Path, *, maximum_bytes: int = 1024 * 1024) -> str:
    if not _trusted_root_regular(path):
        raise ValueError("untrusted source file")
    try:
        before = path.stat()
        raw = path.read_bytes()
        after = path.stat()
        text = raw.decode("utf-8", errors="strict")
    except (OSError, UnicodeDecodeError):
        raise ValueError("unreadable source file") from None
    if (
        not raw
        or len(raw) > maximum_bytes
        or len(raw) != before.st_size
        or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        or any(
            ord(character) < 0x20 and character not in "\r\n\t" for character in text
        )
    ):
        raise ValueError("invalid source file")
    return text


def _validate_source_options(options: Sequence[str]) -> tuple[str, ...]:
    normalized = tuple(option.casefold() for option in options)
    required = "signed-by=/usr/share/keyrings/ubuntu-archive-keyring.gpg"
    allowed = {"arch=amd64", required}
    if (
        len(normalized) != len(set(normalized))
        or not set(normalized).issubset(allowed)
        or required not in normalized
    ):
        raise ValueError("untrusted apt source options")
    return tuple(sorted(normalized))


def _parse_list_sources(text: str) -> list[tuple[str, ...]]:
    entries: list[tuple[str, ...]] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            tokens = shlex.split(line, comments=True, posix=True)
        except ValueError:
            raise ValueError("invalid apt source") from None
        if not tokens or tokens[0] not in {"deb", "deb-src"}:
            raise ValueError("invalid apt source")
        index = 1
        options: list[str] = []
        if index < len(tokens) and tokens[index].startswith("["):
            while index < len(tokens):
                options.append(tokens[index].strip("[]"))
                closed = tokens[index].endswith("]")
                index += 1
                if closed:
                    break
            if not closed:
                raise ValueError("invalid apt source options")
        if index >= len(tokens) or not _ubuntu_archive_uri(tokens[index]):
            raise ValueError("untrusted apt source")
        normalized_options = _validate_source_options(options)
        if len(tokens) < index + 3:
            raise ValueError("incomplete apt source")
        suite = tokens[index + 1]
        components = tuple(tokens[index + 2 :])
        if (
            suite not in {"noble", "noble-updates", "noble-security", "noble-backports"}
            or not components
            or not set(components).issubset(
                {"main", "restricted", "universe", "multiverse"}
            )
            or len(components) != len(set(components))
        ):
            raise ValueError("untrusted apt suite or component")
        entries.append(
            (
                tokens[0],
                tokens[index],
                suite,
                *sorted(components),
                *normalized_options,
            )
        )
    return entries


def _deb822_paragraphs(text: str) -> list[dict[str, str]]:
    paragraphs: list[dict[str, str]] = []
    current: dict[str, str] = {}
    previous: str | None = None
    for raw_line in [*text.splitlines(), ""]:
        if not raw_line.strip():
            if current:
                paragraphs.append(current)
                current = {}
                previous = None
            continue
        if raw_line.startswith("#"):
            continue
        if raw_line[:1].isspace():
            if previous is None:
                raise ValueError("invalid deb822 continuation")
            current[previous] += " " + raw_line.strip()
            continue
        if ":" not in raw_line:
            raise ValueError("invalid deb822 field")
        key, value = raw_line.split(":", 1)
        key = key.strip().casefold()
        if not key or key in current:
            raise ValueError("duplicate deb822 field")
        current[key] = value.strip()
        previous = key
    return paragraphs


def _parse_deb822_sources(text: str) -> list[tuple[str, ...]]:
    entries: list[tuple[str, ...]] = []
    for paragraph in _deb822_paragraphs(text):
        enabled = paragraph.get("enabled", "yes").casefold()
        if enabled == "no":
            continue
        types = paragraph.get("types", "").split()
        uris = paragraph.get("uris", "").split()
        suites = paragraph.get("suites", "").split()
        components = paragraph.get("components", "").split()
        architectures = paragraph.get("architectures", "amd64").split()
        signed_by = paragraph.get("signed-by")
        allowed_fields = {
            "architectures",
            "components",
            "enabled",
            "signed-by",
            "suites",
            "types",
            "uris",
        }
        if (
            not set(paragraph).issubset(allowed_fields)
            or enabled != "yes"
            or not types
            or not set(types).issubset({"deb", "deb-src"})
            or len(types) != len(set(types))
            or not uris
            or len(uris) != len(set(uris))
            or any(not _ubuntu_archive_uri(uri) for uri in uris)
            or not suites
            or not set(suites).issubset(
                {"noble", "noble-updates", "noble-security", "noble-backports"}
            )
            or len(suites) != len(set(suites))
            or not components
            or not set(components).issubset(
                {"main", "restricted", "universe", "multiverse"}
            )
            or len(components) != len(set(components))
            or architectures != ["amd64"]
            or signed_by != _UBUNTU_ARCHIVE_KEYRING_TEXT
        ):
            raise ValueError("untrusted deb822 source")
        entries.extend(
            (
                source_type,
                uri,
                *sorted(suites),
                *sorted(components),
                "architectures=amd64",
                f"signed-by={signed_by}",
            )
            for source_type in types
            for uri in uris
        )
    return entries


def _apt_sources_evidence(
    list_path: Path = Path("/etc/apt/sources.list"),
    directory: Path = Path("/etc/apt/sources.list.d"),
    keyring_path: Path = _UBUNTU_ARCHIVE_KEYRING,
) -> tuple[bool, str | None]:
    try:
        keyring_identity = _trusted_file_identity(
            keyring_path,
            maximum_bytes=16 * 1024 * 1024,
        )
        if keyring_identity is None:
            raise ValueError("untrusted Ubuntu archive keyring")
        entries: list[tuple[str, ...]] = []
        if list_path.exists() or list_path.is_symlink():
            entries.extend(_parse_list_sources(_read_trusted_text(list_path)))
        metadata = directory.lstat()
        if (
            directory.is_symlink()
            or not stat.S_ISDIR(metadata.st_mode)
            or os.name == "posix"
            and (metadata.st_uid != 0 or metadata.st_mode & 0o022)
        ):
            raise ValueError("untrusted apt source directory")
        for path in sorted(directory.iterdir(), key=lambda candidate: candidate.name):
            if path.suffix not in {".list", ".sources"}:
                continue
            text = _read_trusted_text(path)
            entries.extend(
                _parse_list_sources(text)
                if path.suffix == ".list"
                else _parse_deb822_sources(text)
            )
        if not entries:
            raise ValueError("no active apt sources")
    except (OSError, ValueError):
        return False, None
    normalized = sorted(set(entries))
    return True, _sha256_identity(
        {"entries": normalized, "keyringIdentity": keyring_identity}
    )


def _postgres_major(output: bytes) -> int | None:
    try:
        text = output.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return None
    match = re.search(r"\b([0-9]{1,2})(?:\.[0-9]+)+\b", text)
    return int(match.group(1)) if match else None


def _read_os_release(path: Path = Path("/etc/os-release")) -> tuple[str, str]:
    try:
        raw = path.read_bytes()
        if not raw or len(raw) > 16384:
            raise ValueError
        text = raw.decode("utf-8", errors="strict")
    except (OSError, UnicodeDecodeError, ValueError):
        return "unknown", "unknown"
    values: dict[str, str] = {}
    for line in text.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key in {"ID", "VERSION_ID"}:
            values[key] = value.strip().strip('"')
    return values.get("ID", "unknown").lower(), values.get("VERSION_ID", "unknown")


def _local_docker_socket(path: Path = Path("/var/run/docker.sock")) -> bool:
    try:
        metadata = path.stat()
        resolved = path.resolve(strict=True)
    except OSError:
        return False
    return (
        stat.S_ISSOCK(metadata.st_mode)
        and metadata.st_nlink == 1
        and (os.name != "posix" or metadata.st_uid == 0)
        and metadata.st_mode & 0o002 == 0
        and str(resolved) in {"/run/docker.sock", "/var/run/docker.sock"}
    )


def _docker_socket_identity(
    path: Path = Path("/var/run/docker.sock"),
) -> str | None:
    if not _local_docker_socket(path):
        return None
    try:
        before = path.stat()
        resolved = path.resolve(strict=True)
        after = path.stat()
    except OSError:
        return None
    if (
        before.st_dev,
        before.st_ino,
        before.st_mode,
        before.st_uid,
        before.st_gid,
        before.st_ctime_ns,
    ) != (
        after.st_dev,
        after.st_ino,
        after.st_mode,
        after.st_uid,
        after.st_gid,
        after.st_ctime_ns,
    ):
        return None
    return _sha256_identity(
        {
            "device": before.st_dev,
            "inode": before.st_ino,
            "mode": before.st_mode,
            "uid": before.st_uid,
            "gid": before.st_gid,
            "ctimeNs": before.st_ctime_ns,
            "resolvedPath": str(resolved),
        }
    )


def _compose_plugin_identity() -> tuple[bool, str | None]:
    if any(path.exists() or path.is_symlink() for path in _SHADOW_COMPOSE_PLUGIN_PATHS):
        _reject("PLATFORM_BOOTSTRAP_HOST_STATE_INCONSISTENT")
    compose_files = tuple(
        path
        for path in _SYSTEM_COMPOSE_PLUGIN_PATHS
        if path.exists() or path.is_symlink()
    )
    compose_file_identities = tuple(
        (str(path), _trusted_file_identity(path, maximum_bytes=128 * 1024 * 1024))
        for path in compose_files
    )
    if any(identity is None for _, identity in compose_file_identities):
        _reject("PLATFORM_BOOTSTRAP_HOST_STATE_INCONSISTENT")
    return (
        bool(compose_files),
        (
            _sha256_identity(compose_file_identities)
            if compose_file_identities
            else None
        ),
    )


def _docker_daemon_identity(
    runner: PlatformCommandRunner,
    *,
    service_active: bool,
) -> str | None:
    if not service_active:
        return None
    result = _command(
        runner,
        (
            "/usr/bin/systemctl",
            "show",
            "--property=MainPID",
            "--property=ExecMainStartTimestampMonotonic",
            "--value",
            "docker",
        ),
    )
    try:
        values = result.stdout.decode("ascii", errors="strict").splitlines()
    except UnicodeDecodeError:
        return None
    if (
        result.returncode != 0
        or len(values) != 2
        or any(not value.isdecimal() for value in values)
        or int(values[0]) < 1
        or int(values[1]) < 1
    ):
        return None
    return _sha256_identity({"mainPid": int(values[0]), "started": int(values[1])})


def collect_bootstrap_host_facts(
    runner: PlatformCommandRunner | None = None,
) -> BootstrapHostFacts:
    runner = runner or SubprocessPlatformCommandRunner()
    distribution_id, distribution_major = _read_os_release()
    architecture_result = _command(runner, ("/usr/bin/dpkg", "--print-architecture"))
    if architecture_result.returncode == 0:
        architecture = architecture_result.stdout.decode(
            "ascii", errors="ignore"
        ).strip()
    else:
        machine = host_platform.machine().lower()
        architecture = "amd64" if machine in {"x86_64", "amd64"} else machine
    apt_available = _trusted_root_regular(
        Path("/usr/bin/apt-get")
    ) and _trusted_root_regular(Path("/usr/bin/apt-cache"))
    apt_sources_trusted, apt_sources_identity = _apt_sources_evidence()
    installed: list[str] = []
    for package in PLATFORM_PACKAGE_POLICY.body()["packageNames"]:
        result = _command(
            runner,
            (
                "/usr/bin/dpkg-query",
                "--show",
                "--showformat=${db:Status-Abbrev}",
                str(package),
            ),
        )
        if result.returncode == 0 and result.stdout.startswith(b"ii "):
            installed.append(str(package))
    systemctl_path = Path("/usr/bin/systemctl")
    systemd = (
        _trusted_root_regular(systemctl_path)
        and _command(runner, (str(systemctl_path), "--version")).returncode == 0
    )
    docker_path = Path("/usr/bin/docker")
    docker_present = docker_path.exists() or docker_path.is_symlink()
    docker_identity = _trusted_file_identity(
        docker_path, maximum_bytes=128 * 1024 * 1024
    )
    docker_trusted = not docker_present or docker_identity is not None
    docker_cli = (
        docker_trusted
        and docker_present
        and _command(runner, ("/usr/bin/docker", "--version")).returncode == 0
    )
    socket_path = Path("/var/run/docker.sock")
    socket_present = socket_path.exists() or socket_path.is_symlink()
    socket_local = socket_present and _local_docker_socket(socket_path)
    socket_identity = _docker_socket_identity(socket_path) if socket_present else None
    service_active = (
        systemd
        and _command(
            runner, ("/usr/bin/systemctl", "is-active", "--quiet", "docker")
        ).returncode
        == 0
    )
    daemon_identity = _docker_daemon_identity(
        runner,
        service_active=service_active,
    )
    docker_daemon = (
        docker_cli
        and socket_local
        and service_active
        and daemon_identity is not None
        and _command(
            runner,
            (
                "/usr/bin/docker",
                "--host",
                "unix:///var/run/docker.sock",
                "info",
                "--format",
                "{{.ServerVersion}}",
            ),
        ).returncode
        == 0
    )
    compose_file_present, compose_identity = _compose_plugin_identity()
    compose_present = compose_file_present or (
        PLATFORM_PACKAGE_POLICY.compose_package in installed
    )
    compose = (
        docker_cli
        and _command(
            runner,
            (
                "/usr/bin/docker",
                "--host",
                "unix:///var/run/docker.sock",
                "compose",
                "version",
            ),
        ).returncode
        == 0
    )
    pg_dump_result = _command(runner, ("/usr/bin/pg_dump", "--version"))
    psql_result = _command(runner, ("/usr/bin/psql", "--version"))
    docker_config_path = Path("/etc/docker/daemon.json")
    if docker_config_path.exists() or docker_config_path.is_symlink():
        docker_config_identity = _trusted_file_identity(
            docker_config_path,
            maximum_bytes=1024 * 1024,
        )
        if docker_config_identity is None:
            _reject("PLATFORM_BOOTSTRAP_HOST_STATE_INCONSISTENT")
    else:
        docker_config_identity = "ABSENT"
    return BootstrapHostFacts(
        distribution_id=distribution_id,
        distribution_major=distribution_major,
        architecture=architecture,
        effective_uid=os.geteuid() if hasattr(os, "geteuid") else -1,
        apt_available=apt_available,
        apt_sources_trusted=apt_sources_trusted,
        apt_sources_identity=apt_sources_identity,
        systemd_available=systemd,
        docker_cli_present=docker_present,
        docker_cli_available=docker_cli,
        docker_cli_trusted=docker_trusted,
        docker_cli_identity=docker_identity,
        docker_service_active=service_active,
        docker_daemon_healthy=docker_daemon,
        docker_daemon_identity=daemon_identity,
        docker_socket_present=socket_present,
        docker_socket_local=socket_local,
        docker_socket_identity=socket_identity,
        compose_v2_present=compose_present,
        compose_v2_available=compose,
        compose_v2_identity=compose_identity,
        docker_config_identity=docker_config_identity,
        pg_dump_major=(
            _postgres_major(pg_dump_result.stdout)
            if pg_dump_result.returncode == 0
            else None
        ),
        psql_major=(
            _postgres_major(psql_result.stdout) if psql_result.returncode == 0 else None
        ),
        installed_policy_packages=tuple(installed),
    )


def _validate_common_facts(facts: BootstrapHostFacts) -> None:
    if (
        facts.distribution_id != PLATFORM_PACKAGE_POLICY.distribution_id
        or facts.distribution_major != PLATFORM_PACKAGE_POLICY.distribution_major
    ):
        _reject("PLATFORM_BOOTSTRAP_OS_UNSUPPORTED")
    if facts.architecture != PLATFORM_PACKAGE_POLICY.architecture:
        _reject("PLATFORM_BOOTSTRAP_ARCH_UNSUPPORTED")
    if facts.effective_uid != 0:
        _reject("PLATFORM_BOOTSTRAP_ROOT_REQUIRED")
    if (
        facts.docker_cli_present != facts.docker_cli_available
        or facts.docker_cli_present != (facts.docker_cli_identity is not None)
        or facts.docker_cli_available != facts.docker_daemon_healthy
        or not facts.docker_cli_trusted
        or facts.docker_service_active != facts.docker_daemon_healthy
        or facts.docker_service_active != (facts.docker_daemon_identity is not None)
        or facts.docker_socket_present != facts.docker_socket_local
        or facts.docker_socket_present != facts.docker_daemon_healthy
        or facts.docker_socket_present != (facts.docker_socket_identity is not None)
        or facts.compose_v2_present != facts.compose_v2_available
        or facts.compose_v2_present != (facts.compose_v2_identity is not None)
        or facts.compose_v2_available
        and not facts.docker_cli_available
        or (facts.pg_dump_major is None) != (facts.psql_major is None)
        or (
            facts.pg_dump_major is not None
            and facts.psql_major is not None
            and facts.pg_dump_major != facts.psql_major
        )
    ):
        _reject("PLATFORM_BOOTSTRAP_HOST_STATE_INCONSISTENT")


def _offline_capabilities_complete(facts: BootstrapHostFacts) -> bool:
    required = PLATFORM_PACKAGE_POLICY.required_postgres_major
    return (
        facts.docker_cli_available
        and facts.docker_daemon_healthy
        and facts.compose_v2_available
        and facts.pg_dump_major == required
        and facts.psql_major == required
        and facts.systemd_available
    )


def _build_plan(
    facts: BootstrapHostFacts,
    transport_source: InstallTransportSource,
    created_at: str,
) -> PlatformBootstrapPlan:
    if type(transport_source) is not InstallTransportSource or not _UTC.fullmatch(
        created_at
    ):
        _reject("PLATFORM_BOOTSTRAP_PACKAGE_POLICY_INVALID")
    _validate_common_facts(facts)
    if transport_source in {
        InstallTransportSource.LOCAL_BUNDLE,
        InstallTransportSource.PREPUBLICATION_CANDIDATE,
    }:
        if not _offline_capabilities_complete(facts):
            _reject("PLATFORM_BOOTSTRAP_OFFLINE_CAPABILITY_MISSING")
        mode = PlatformBootstrapMode.OFFLINE_VALIDATE_ONLY
        actions = (PlatformBootstrapAction(PlatformBootstrapActionKind.VALIDATE_ONLY),)
        daemon_policy = "VALIDATE_ONLY"
        network_policy = "DENY_ALL"
    else:
        if not facts.apt_available:
            _reject("PLATFORM_BOOTSTRAP_PACKAGE_MANAGER_UNAVAILABLE")
        if not facts.apt_sources_trusted or facts.apt_sources_identity is None:
            _reject("PLATFORM_BOOTSTRAP_PACKAGE_POLICY_INVALID")
        if not facts.systemd_available:
            _reject("PLATFORM_BOOTSTRAP_HOST_STATE_INCONSISTENT")
        actions_list: list[PlatformBootstrapAction] = []
        if not facts.docker_cli_present:
            mode = PlatformBootstrapMode.ONLINE_FRESH
            actions_list.extend(
                (
                    PlatformBootstrapAction(PlatformBootstrapActionKind.APT_UPDATE),
                    PlatformBootstrapAction(
                        PlatformBootstrapActionKind.INSTALL_DOCKER,
                        (PLATFORM_PACKAGE_POLICY.docker_package,),
                    ),
                    PlatformBootstrapAction(
                        PlatformBootstrapActionKind.INSTALL_COMPOSE,
                        (PLATFORM_PACKAGE_POLICY.compose_package,),
                    ),
                    PlatformBootstrapAction(
                        PlatformBootstrapActionKind.INSTALL_POSTGRES_CLIENT,
                        (PLATFORM_PACKAGE_POLICY.postgres_client_package,),
                    ),
                    PlatformBootstrapAction(
                        PlatformBootstrapActionKind.ENABLE_DOCKER_DAEMON
                    ),
                )
            )
            daemon_policy = "INSTALL_AND_ENABLE_IF_ABSENT"
        else:
            mode = PlatformBootstrapMode.ONLINE_EXISTING_DOCKER
            missing: list[PlatformBootstrapAction] = []
            if not facts.compose_v2_available:
                missing.append(
                    PlatformBootstrapAction(
                        PlatformBootstrapActionKind.INSTALL_COMPOSE,
                        (PLATFORM_PACKAGE_POLICY.compose_package,),
                    )
                )
            if (
                facts.pg_dump_major != PLATFORM_PACKAGE_POLICY.required_postgres_major
                or facts.psql_major != PLATFORM_PACKAGE_POLICY.required_postgres_major
            ):
                missing.append(
                    PlatformBootstrapAction(
                        PlatformBootstrapActionKind.INSTALL_POSTGRES_CLIENT,
                        (PLATFORM_PACKAGE_POLICY.postgres_client_package,),
                    )
                )
            actions_list.extend(
                [
                    PlatformBootstrapAction(PlatformBootstrapActionKind.APT_UPDATE),
                    *missing,
                ]
                if missing
                else [
                    PlatformBootstrapAction(PlatformBootstrapActionKind.VALIDATE_ONLY)
                ]
            )
            daemon_policy = "PRESERVE_NO_RESTART"
        actions = tuple(actions_list)
        network_policy = "APT_UBUNTU_ARCHIVE_ONLY"
    provisional = PlatformBootstrapPlan(
        mode=mode,
        transport_source=transport_source,
        initial_capabilities=facts,
        actions=actions,
        package_policy_identity=PLATFORM_PACKAGE_POLICY.identity,
        expected_capabilities=_EXPECTED_CAPABILITIES,
        docker_daemon_policy=daemon_policy,
        network_policy=network_policy,
        created_at=created_at,
        plan_digest="",
    )
    return PlatformBootstrapPlan(
        **{
            **provisional.__dict__,
            "plan_digest": _sha256_identity(provisional.identity_body()),
        }
    )


@contextmanager
def _platform_lock() -> Iterator[None]:
    if os.name != "posix" or fcntl is None:
        _reject("PLATFORM_BOOTSTRAP_ROOT_REQUIRED")
    parent = PLATFORM_BOOTSTRAP_LOCK.parent
    try:
        parent_metadata = parent.lstat()
        if parent.is_symlink() or not stat.S_ISDIR(parent_metadata.st_mode):
            _reject("PLATFORM_BOOTSTRAP_HOST_STATE_INCONSISTENT")
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(PLATFORM_BOOTSTRAP_LOCK, flags, 0o600)
    except PlatformBootstrapError:
        raise
    except OSError:
        _reject("PLATFORM_BOOTSTRAP_HOST_STATE_INCONSISTENT")
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_uid != 0
            or metadata.st_mode & 0o077 != 0
        ):
            _reject("PLATFORM_BOOTSTRAP_HOST_STATE_INCONSISTENT")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            _reject("PLATFORM_BOOTSTRAP_ALREADY_RUNNING")
        yield
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(descriptor)


_APT_CONFIGURATION_OPTIONS = (
    "-o",
    "Dir::Etc::sourcelist=/etc/apt/sources.list",
    "-o",
    "Dir::Etc::sourceparts=/etc/apt/sources.list.d",
    "-o",
    "Acquire::AllowInsecureRepositories=false",
    "-o",
    "Acquire::AllowDowngradeToInsecureRepositories=false",
)


def _apt_argv(operation: str, packages: tuple[str, ...] = ()) -> tuple[str, ...]:
    prefix = (
        "/usr/bin/apt-get",
        *_APT_CONFIGURATION_OPTIONS,
        "-o",
        f"Dpkg::Lock::Timeout={_APT_LOCK_TIMEOUT_SECONDS}",
        "-o",
        f"Acquire::Retries={_APT_RETRIES}",
        "-o",
        "APT::Get::allow-Downgrades=false",
        "-o",
        "APT::Get::allow-Remove-Essential=false",
        "-o",
        "APT::Get::allow-Change-Held-Packages=false",
    )
    if operation == "update" and not packages:
        return (*prefix, "update")
    if operation in {"install", "simulate"} and packages:
        allowed = frozenset(PLATFORM_PACKAGE_POLICY.body()["packageNames"])
        if not set(packages).issubset(allowed) or len(packages) != len(set(packages)):
            _reject("PLATFORM_BOOTSTRAP_PACKAGE_POLICY_INVALID")
        simulation = ("--simulate",) if operation == "simulate" else ()
        return (
            *prefix,
            *simulation,
            "install",
            "--yes",
            "--no-install-recommends",
            "--no-remove",
            "--no-upgrade",
            *packages,
        )
    _reject("PLATFORM_BOOTSTRAP_PACKAGE_POLICY_INVALID")


def _apt_lock_failed(result: PlatformCommandResult) -> bool:
    output = (result.stdout + result.stderr).lower()
    return (
        b"could not get lock" in output
        or b"unable to acquire the dpkg frontend lock" in output
    )


def _verify_package_available(
    runner: PlatformCommandRunner,
    package: str,
) -> None:
    if package not in PLATFORM_PACKAGE_POLICY.body()["packageNames"]:
        _reject("PLATFORM_BOOTSTRAP_PACKAGE_POLICY_INVALID")
    result = _command(
        runner,
        ("/usr/bin/apt-cache", *_APT_CONFIGURATION_OPTIONS, "policy", package),
    )
    try:
        output = result.stdout.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        _reject("PLATFORM_BOOTSTRAP_PACKAGE_UNAVAILABLE")
    candidate = re.search(r"^\s*Candidate:\s*(\S+)\s*$", output, re.MULTILINE)
    if result.returncode != 0 or candidate is None or candidate.group(1) == "(none)":
        _reject("PLATFORM_BOOTSTRAP_PACKAGE_UNAVAILABLE")
    candidate_version = candidate.group(1)
    lines = output.splitlines()
    version_header = re.compile(r"^\s*(?:\*\*\*\s+)?(\S+)\s+[0-9]+\s*$")
    candidate_block: list[str] | None = None
    for index, line in enumerate(lines):
        header = version_header.match(line)
        if header is None or header.group(1) != candidate_version:
            continue
        candidate_block = []
        for following in lines[index + 1 :]:
            if version_header.match(following):
                break
            candidate_block.append(following)
        break
    trusted_origin = False
    if candidate_block is not None:
        for line in candidate_block:
            repository = re.match(r"^\s*[0-9]+\s+(\S+)", line)
            if repository is None:
                continue
            uri = repository.group(1)
            if uri.startswith(("http://", "https://")):
                if not _ubuntu_archive_uri(uri):
                    _reject("PLATFORM_BOOTSTRAP_PACKAGE_UNAVAILABLE")
                trusted_origin = True
            elif uri != "/var/lib/dpkg/status":
                _reject("PLATFORM_BOOTSTRAP_PACKAGE_UNAVAILABLE")
    if not trusted_origin:
        _reject("PLATFORM_BOOTSTRAP_PACKAGE_UNAVAILABLE")


def _verify_existing_docker_transaction(
    runner: PlatformCommandRunner,
    packages: tuple[str, ...],
) -> None:
    result = _command(runner, _apt_argv("simulate", packages))
    if result.returncode != 0:
        _reject("PLATFORM_BOOTSTRAP_PACKAGE_UNAVAILABLE")
    protected = {
        "containerd",
        "containerd.io",
        "docker-ce",
        "docker-ce-cli",
        "docker.io",
        "moby-engine",
        "runc",
    }
    try:
        output = result.stdout.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        _reject("PLATFORM_BOOTSTRAP_PACKAGE_POLICY_INVALID")
    scheduled = {
        match.group(1).split(":", 1)[0]
        for match in re.finditer(
            r"^(?:Inst|Conf|Remv)\s+(\S+)",
            output,
            re.MULTILINE,
        )
    }
    if scheduled & protected:
        _reject("PLATFORM_BOOTSTRAP_PACKAGE_POLICY_INVALID")


def _final_capabilities_complete(facts: BootstrapHostFacts) -> bool:
    return _offline_capabilities_complete(facts)


def _receipt(
    plan: PlatformBootstrapPlan,
    final_facts: BootstrapHostFacts,
) -> PlatformBootstrapReceipt:
    installed = tuple(
        package
        for action in plan.actions
        for package in action.packages
        if package in final_facts.installed_policy_packages
    )
    preserved = tuple(
        package
        for package in plan.initial_capabilities.installed_policy_packages
        if package not in installed
    )
    provisional = PlatformBootstrapReceipt(
        plan_digest=plan.plan_digest,
        mode=plan.mode,
        initial_capabilities=plan.initial_capabilities,
        installed_packages=installed,
        preserved_packages=preserved,
        final_capabilities=final_facts,
        docker_daemon_before=(
            "HEALTHY" if plan.initial_capabilities.docker_daemon_healthy else "ABSENT"
        ),
        docker_daemon_after="HEALTHY"
        if final_facts.docker_daemon_healthy
        else "ABSENT",
        docker_daemon_restart_count=0,
        package_policy_identity=plan.package_policy_identity,
        result="PASS",
        receipt_digest="",
    )
    return PlatformBootstrapReceipt(
        **{
            **provisional.__dict__,
            "receipt_digest": _sha256_identity(provisional.identity_body()),
        }
    )


class ProductionPlatformBootstrap:
    """Closed host-platform plan/execute composition root."""

    def __init__(
        self,
        *,
        facts_collector: Callable[[], BootstrapHostFacts] | None = None,
        runner: PlatformCommandRunner | None = None,
        clock: Callable[[], str] | None = None,
        lock_factory: Callable[[], object] | None = None,
    ) -> None:
        self._runner = runner or SubprocessPlatformCommandRunner()
        self._facts_collector = facts_collector or (
            lambda: collect_bootstrap_host_facts(self._runner)
        )
        self._clock = clock or _utc_now
        self._lock_factory = lock_factory or _platform_lock

    def _facts(self) -> BootstrapHostFacts:
        facts = self._facts_collector()
        if type(facts) is not BootstrapHostFacts:
            _reject("PLATFORM_BOOTSTRAP_HOST_STATE_INCONSISTENT")
        return facts

    def plan(
        self,
        *,
        transport_source: InstallTransportSource,
    ) -> PlatformBootstrapPlan:
        return _build_plan(self._facts(), transport_source, self._clock())

    def execute(
        self,
        plan: PlatformBootstrapPlan,
        *,
        accepted_plan_digest: str,
    ) -> PlatformBootstrapReceipt:
        if (
            type(plan) is not PlatformBootstrapPlan
            or type(accepted_plan_digest) is not str
            or not hmac.compare_digest(plan.plan_digest, accepted_plan_digest)
            or not _DIGEST.fullmatch(plan.plan_digest)
            or _sha256_identity(plan.identity_body()) != plan.plan_digest
        ):
            _reject("PLATFORM_BOOTSTRAP_PLAN_NOT_ACCEPTED")
        expected_plan = _build_plan(
            plan.initial_capabilities,
            plan.transport_source,
            plan.created_at,
        )
        if expected_plan.as_dict() != plan.as_dict():
            _reject("PLATFORM_BOOTSTRAP_PLAN_CHANGED")
        with self._lock_factory():
            if self._facts().as_dict() != plan.initial_capabilities.as_dict():
                _reject("PLATFORM_BOOTSTRAP_PLAN_CHANGED")
            for action in plan.actions:
                if action.kind is PlatformBootstrapActionKind.VALIDATE_ONLY:
                    continue
                if action.kind is PlatformBootstrapActionKind.APT_UPDATE:
                    result = _command(
                        self._runner,
                        _apt_argv("update"),
                        timeout=_APT_COMMAND_TIMEOUT_SECONDS,
                    )
                    if result.returncode != 0:
                        _reject(
                            "PLATFORM_BOOTSTRAP_APT_LOCK_TIMEOUT"
                            if _apt_lock_failed(result)
                            else "PLATFORM_BOOTSTRAP_APT_UPDATE_FAILED"
                        )
                    continue
                if action.kind in {
                    PlatformBootstrapActionKind.INSTALL_DOCKER,
                    PlatformBootstrapActionKind.INSTALL_COMPOSE,
                    PlatformBootstrapActionKind.INSTALL_POSTGRES_CLIENT,
                }:
                    for package in action.packages:
                        _verify_package_available(self._runner, package)
                    if plan.mode is PlatformBootstrapMode.ONLINE_EXISTING_DOCKER:
                        _verify_existing_docker_transaction(
                            self._runner,
                            action.packages,
                        )
                    result = _command(
                        self._runner,
                        _apt_argv("install", action.packages),
                        timeout=_APT_COMMAND_TIMEOUT_SECONDS,
                    )
                    for _ in range(_APT_INSTALL_TIMEOUT_RETRIES):
                        if result.returncode != 124:
                            break
                        result = _command(
                            self._runner,
                            _apt_argv("install", action.packages),
                            timeout=_APT_COMMAND_TIMEOUT_SECONDS,
                        )
                    if result.returncode != 0:
                        if _apt_lock_failed(result):
                            _reject("PLATFORM_BOOTSTRAP_APT_LOCK_TIMEOUT")
                        _reject(
                            {
                                PlatformBootstrapActionKind.INSTALL_DOCKER: "PLATFORM_BOOTSTRAP_DOCKER_INSTALL_FAILED",
                                PlatformBootstrapActionKind.INSTALL_COMPOSE: "PLATFORM_BOOTSTRAP_COMPOSE_INSTALL_FAILED",
                                PlatformBootstrapActionKind.INSTALL_POSTGRES_CLIENT: "PLATFORM_BOOTSTRAP_POSTGRES_CLIENT_INSTALL_FAILED",
                            }[action.kind]
                        )
                    continue
                if action.kind is PlatformBootstrapActionKind.ENABLE_DOCKER_DAEMON:
                    result = _command(
                        self._runner,
                        ("/usr/bin/systemctl", "enable", "--now", "docker"),
                    )
                    if result.returncode != 0:
                        _reject("PLATFORM_BOOTSTRAP_DOCKER_DAEMON_FAILED")
                    continue
                _reject("PLATFORM_BOOTSTRAP_PACKAGE_POLICY_INVALID")
            final_facts = self._facts()
            if not _final_capabilities_complete(final_facts):
                _reject("PLATFORM_BOOTSTRAP_POST_QUALIFICATION_FAILED")
            if plan.mode is PlatformBootstrapMode.ONLINE_EXISTING_DOCKER and (
                not final_facts.docker_cli_available
                or not final_facts.docker_daemon_healthy
                or final_facts.docker_cli_identity
                != plan.initial_capabilities.docker_cli_identity
                or final_facts.docker_daemon_identity
                != plan.initial_capabilities.docker_daemon_identity
                or final_facts.docker_socket_identity
                != plan.initial_capabilities.docker_socket_identity
                or plan.initial_capabilities.compose_v2_present
                and final_facts.compose_v2_identity
                != plan.initial_capabilities.compose_v2_identity
                or final_facts.docker_config_identity
                != plan.initial_capabilities.docker_config_identity
            ):
                _reject("PLATFORM_BOOTSTRAP_POST_QUALIFICATION_FAILED")
            receipt = _receipt(plan, final_facts)
            validate_platform_bootstrap_receipt(receipt, plan=plan)
            return receipt


def _utc_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def validate_platform_bootstrap_receipt(
    receipt: PlatformBootstrapReceipt,
    *,
    plan: PlatformBootstrapPlan,
) -> None:
    if (
        type(receipt) is not PlatformBootstrapReceipt
        or type(plan) is not PlatformBootstrapPlan
        or type(receipt.initial_capabilities) is not BootstrapHostFacts
        or type(receipt.final_capabilities) is not BootstrapHostFacts
        or type(receipt.plan_digest) is not str
        or type(receipt.installed_packages) is not tuple
        or any(type(value) is not str for value in receipt.installed_packages)
        or type(receipt.preserved_packages) is not tuple
        or any(type(value) is not str for value in receipt.preserved_packages)
        or type(receipt.docker_daemon_before) is not str
        or type(receipt.docker_daemon_after) is not str
        or type(receipt.package_policy_identity) is not str
        or type(receipt.result) is not str
        or type(receipt.receipt_digest) is not str
    ):
        _reject("PLATFORM_BOOTSTRAP_RECEIPT_INVALID")
    planned_packages = tuple(
        package for action in plan.actions for package in action.packages
    )
    expected_preserved = tuple(
        package
        for package in plan.initial_capabilities.installed_policy_packages
        if package not in planned_packages
    )
    expected_daemon_before = (
        "HEALTHY" if plan.initial_capabilities.docker_daemon_healthy else "ABSENT"
    )
    expected_daemon_after = (
        "HEALTHY" if receipt.final_capabilities.docker_daemon_healthy else "ABSENT"
    )
    if (
        receipt.plan_digest != plan.plan_digest
        or receipt.mode is not plan.mode
        or receipt.initial_capabilities.as_dict() != plan.initial_capabilities.as_dict()
        or receipt.package_policy_identity != PLATFORM_PACKAGE_POLICY.identity
        or receipt.installed_packages != planned_packages
        or receipt.preserved_packages != expected_preserved
        or receipt.docker_daemon_before != expected_daemon_before
        or receipt.docker_daemon_after != expected_daemon_after
        or type(receipt.docker_daemon_restart_count) is not int
        or not set(planned_packages).issubset(
            receipt.final_capabilities.installed_policy_packages
        )
        or receipt.docker_daemon_restart_count != 0
        or receipt.result != "PASS"
        or not _DIGEST.fullmatch(receipt.receipt_digest)
        or _sha256_identity(receipt.identity_body()) != receipt.receipt_digest
        or not _final_capabilities_complete(receipt.final_capabilities)
        or plan.mode is PlatformBootstrapMode.ONLINE_EXISTING_DOCKER
        and (
            receipt.final_capabilities.docker_cli_identity
            != plan.initial_capabilities.docker_cli_identity
            or receipt.final_capabilities.docker_daemon_identity
            != plan.initial_capabilities.docker_daemon_identity
            or receipt.final_capabilities.docker_socket_identity
            != plan.initial_capabilities.docker_socket_identity
            or plan.initial_capabilities.compose_v2_present
            and receipt.final_capabilities.compose_v2_identity
            != plan.initial_capabilities.compose_v2_identity
            or receipt.final_capabilities.docker_config_identity
            != plan.initial_capabilities.docker_config_identity
        )
    ):
        _reject("PLATFORM_BOOTSTRAP_RECEIPT_INVALID")


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _facts_from_dict(value: object, *, error_code: str) -> BootstrapHostFacts:
    keys = {
        "distributionId",
        "distributionMajor",
        "architecture",
        "effectiveUid",
        "aptAvailable",
        "aptSourcesTrusted",
        "aptSourcesIdentity",
        "systemdAvailable",
        "dockerCliPresent",
        "dockerCliAvailable",
        "dockerCliTrusted",
        "dockerCliIdentity",
        "dockerServiceActive",
        "dockerDaemonHealthy",
        "dockerDaemonIdentity",
        "dockerSocketPresent",
        "dockerSocketLocal",
        "dockerSocketIdentity",
        "composeV2Present",
        "composeV2Available",
        "composeV2Identity",
        "dockerConfigIdentity",
        "pgDumpMajor",
        "psqlMajor",
        "installedPolicyPackages",
    }
    if (
        type(value) is not dict
        or set(value) != keys
        or type(value["installedPolicyPackages"]) is not list
    ):
        _reject(error_code)
    try:
        return BootstrapHostFacts(
            distribution_id=value["distributionId"],
            distribution_major=value["distributionMajor"],
            architecture=value["architecture"],
            effective_uid=value["effectiveUid"],
            apt_available=value["aptAvailable"],
            apt_sources_trusted=value["aptSourcesTrusted"],
            apt_sources_identity=value["aptSourcesIdentity"],
            systemd_available=value["systemdAvailable"],
            docker_cli_present=value["dockerCliPresent"],
            docker_cli_available=value["dockerCliAvailable"],
            docker_cli_trusted=value["dockerCliTrusted"],
            docker_cli_identity=value["dockerCliIdentity"],
            docker_service_active=value["dockerServiceActive"],
            docker_daemon_healthy=value["dockerDaemonHealthy"],
            docker_daemon_identity=value["dockerDaemonIdentity"],
            docker_socket_present=value["dockerSocketPresent"],
            docker_socket_local=value["dockerSocketLocal"],
            docker_socket_identity=value["dockerSocketIdentity"],
            compose_v2_present=value["composeV2Present"],
            compose_v2_available=value["composeV2Available"],
            compose_v2_identity=value["composeV2Identity"],
            docker_config_identity=value["dockerConfigIdentity"],
            pg_dump_major=value["pgDumpMajor"],
            psql_major=value["psqlMajor"],
            installed_policy_packages=tuple(value["installedPolicyPackages"]),
        )
    except (KeyError, TypeError, PlatformBootstrapError):
        _reject(error_code)


def parse_platform_bootstrap_plan(raw: bytes) -> PlatformBootstrapPlan:
    try:
        value = json.loads(raw, object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        _reject("PLATFORM_BOOTSTRAP_PLAN_CHANGED")
    keys = {
        "schemaVersion",
        "mode",
        "transportSource",
        "initialCapabilities",
        "actions",
        "packagePolicyIdentity",
        "expectedCapabilities",
        "dockerDaemonPolicy",
        "networkPolicy",
        "createdAt",
        "planDigest",
    }
    if (
        type(value) is not dict
        or set(value) != keys
        or raw != _canonical_json_bytes(value)
    ):
        _reject("PLATFORM_BOOTSTRAP_PLAN_CHANGED")
    try:
        facts = _facts_from_dict(
            value["initialCapabilities"],
            error_code="PLATFORM_BOOTSTRAP_PLAN_CHANGED",
        )
        plan = _build_plan(
            facts,
            InstallTransportSource(value["transportSource"]),
            value["createdAt"],
        )
    except (TypeError, ValueError):
        _reject("PLATFORM_BOOTSTRAP_PLAN_CHANGED")
    if value != plan.as_dict():
        _reject("PLATFORM_BOOTSTRAP_PLAN_CHANGED")
    return plan


def parse_platform_bootstrap_receipt(
    raw: bytes,
    *,
    plan: PlatformBootstrapPlan,
) -> PlatformBootstrapReceipt:
    try:
        value = json.loads(raw, object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        _reject("PLATFORM_BOOTSTRAP_RECEIPT_INVALID")
    keys = {
        "schemaVersion",
        "planDigest",
        "mode",
        "initialCapabilities",
        "installedPackages",
        "preservedPackages",
        "finalCapabilities",
        "dockerDaemonBefore",
        "dockerDaemonAfter",
        "dockerDaemonRestartCount",
        "packagePolicyIdentity",
        "result",
        "receiptDigest",
    }
    if (
        type(value) is not dict
        or set(value) != keys
        or raw != _canonical_json_bytes(value)
        or type(value["installedPackages"]) is not list
        or type(value["preservedPackages"]) is not list
    ):
        _reject("PLATFORM_BOOTSTRAP_RECEIPT_INVALID")
    try:
        receipt = PlatformBootstrapReceipt(
            plan_digest=value["planDigest"],
            mode=PlatformBootstrapMode(value["mode"]),
            initial_capabilities=_facts_from_dict(
                value["initialCapabilities"],
                error_code="PLATFORM_BOOTSTRAP_RECEIPT_INVALID",
            ),
            installed_packages=tuple(value["installedPackages"]),
            preserved_packages=tuple(value["preservedPackages"]),
            final_capabilities=_facts_from_dict(
                value["finalCapabilities"],
                error_code="PLATFORM_BOOTSTRAP_RECEIPT_INVALID",
            ),
            docker_daemon_before=value["dockerDaemonBefore"],
            docker_daemon_after=value["dockerDaemonAfter"],
            docker_daemon_restart_count=value["dockerDaemonRestartCount"],
            package_policy_identity=value["packagePolicyIdentity"],
            result=value["result"],
            receipt_digest=value["receiptDigest"],
        )
    except (TypeError, ValueError):
        _reject("PLATFORM_BOOTSTRAP_RECEIPT_INVALID")
    validate_platform_bootstrap_receipt(receipt, plan=plan)
    if value != receipt.as_dict():
        _reject("PLATFORM_BOOTSTRAP_RECEIPT_INVALID")
    return receipt


__all__ = [
    "PACKAGE_POLICY_SCHEMA",
    "PLAN_SCHEMA",
    "PLATFORM_BOOTSTRAP_ERROR_CODES",
    "PLATFORM_BOOTSTRAP_LOCK",
    "PLATFORM_PACKAGE_POLICY",
    "RECEIPT_SCHEMA",
    "BootstrapHostFacts",
    "PlatformBootstrapAction",
    "PlatformBootstrapActionKind",
    "PlatformBootstrapError",
    "PlatformBootstrapMode",
    "PlatformBootstrapPlan",
    "PlatformBootstrapReceipt",
    "PlatformCommandResult",
    "ProductionPlatformBootstrap",
    "SubprocessPlatformCommandRunner",
    "collect_bootstrap_host_facts",
    "parse_platform_bootstrap_plan",
    "parse_platform_bootstrap_receipt",
    "validate_platform_bootstrap_receipt",
]
