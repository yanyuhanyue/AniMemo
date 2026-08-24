from __future__ import annotations

import argparse
import errno
import hashlib
import http.client
import json
import math
import os
import re
import shutil
import socket
import ssl
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from io import BytesIO
from pathlib import Path
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from release.mirror import (
    MAX_MIRROR_RECEIPT_BYTES,
    MIRROR_ORIGIN,
    MIRROR_PATH_PREFIX,
    MIRROR_RECEIPT_NAME,
    MirrorError,
    load_mirror_receipt_bytes,
)

from .commands import CommandRunner
from .errors import (
    CommandExited,
    CommandFailed,
    CommandStartFailed,
    CommandTimedOut,
    RequestRejected,
)


class TransportSourceId(str, Enum):
    GITHUB = "github"
    OFFICIAL_MIRROR = "official-mirror"


class TransportSelectionOrigin(str, Enum):
    EXPLICIT_ADMIN_INPUT = "explicit-admin-input"
    PERSISTED_INSTANCE_POLICY = "persisted-instance-policy"


class TransportRequestKind(str, Enum):
    RELEASE_BUNDLE = "release-bundle"


RELEASE_BUNDLE_OBJECTS = (
    "checksums.txt",
    "deployment-contract.json",
    "installer-materials.tar",
    "release-manifest.json",
)
_EXACT_VERSION = re.compile(
    r"^(?:0|[1-9][0-9]*)\."
    r"(?:0|[1-9][0-9]*)\."
    r"(?:0|[1-9][0-9]*)"
    r"(?:-[0-9A-Za-z]+(?:[.-][0-9A-Za-z]+)*)?$"
)
_HTTP_STATUS = re.compile(r"(?i)\bHTTP(?:\s+status)?[: ]+(\d{3})\b")
GITHUB_RELEASE_ORIGIN = "https://github.com"
OFFICIAL_MIRROR_ENDPOINT_ID = "official-primary"
OFFICIAL_MIRROR_ORIGIN = MIRROR_ORIGIN
_READ_CHUNK_BYTES = 1024 * 1024
_BOUNDED_HTTP_RESPONSE_HEADERS = frozenset(
    {
        "Accept-Ranges",
        "Access-Control-Allow-Origin",
        "Cache-Control",
        "Content-Encoding",
        "Content-Length",
        "Content-Range",
        "Content-Type",
    }
)
_BOUNDED_WORKER_OBJECTS = frozenset((*RELEASE_BUNDLE_OBJECTS, MIRROR_RECEIPT_NAME))
_BOUNDED_WORKSPACE_TOKEN = re.compile(r"^animemo-bounded-http-[A-Za-z0-9_-]{6,64}$")


def _bounded_worker_object_url(exact_version: str, logical_name: str) -> str:
    if (
        not isinstance(exact_version, str)
        or _EXACT_VERSION.fullmatch(exact_version) is None
        or logical_name not in _BOUNDED_WORKER_OBJECTS
    ):
        raise ValueError("invalid bounded worker object identity")
    version = quote(f"v{exact_version}", safe="")
    name = quote(logical_name, safe="")
    return f"{OFFICIAL_MIRROR_ORIGIN}/{MIRROR_PATH_PREFIX}/{version}/{name}"


def _bounded_worker_object_identity(url: str) -> tuple[str, str]:
    parsed = urlsplit(url)
    expected_origin = urlsplit(OFFICIAL_MIRROR_ORIGIN)
    segments = parsed.path.removeprefix("/").split("/")
    prefix_segments = MIRROR_PATH_PREFIX.split("/")
    if (
        parsed.scheme != expected_origin.scheme
        or parsed.netloc != expected_origin.netloc
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or len(segments) != len(prefix_segments) + 2
        or segments[: len(prefix_segments)] != prefix_segments
        or not segments[-2].startswith("v")
    ):
        raise ValueError("bounded worker URL escaped the official mirror")
    exact_version = segments[-2].removeprefix("v")
    logical_name = segments[-1]
    if url != _bounded_worker_object_url(exact_version, logical_name):
        raise ValueError("bounded worker URL is not canonical")
    return exact_version, logical_name


class TransportError(RequestRejected):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        phase: str = "transport",
        transport_id: TransportSourceId | None = None,
        retriable: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.phase = phase
        self.transport_id = transport_id
        self.retriable = retriable


def _canonical_identity(value: dict[str, object]) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, init=False)
class TransferBudgetPolicy:
    policy_version: int = 1
    minimum_object_timeout_seconds: int = 60
    object_timeout_base_seconds: int = 30
    minimum_expected_throughput_bytes_per_second: int = 131072
    maximum_object_timeout_seconds: int = 900
    maximum_attempts_per_object: int = 3
    backoff_seconds: tuple[int, ...] = (10, 30)
    maximum_bundle_elapsed_seconds: int = 1800
    maximum_credential_transitions_per_object: int = 1
    identity: str = field(init=False)

    def __init__(self) -> None:
        values = {
            "backoff_seconds": list(self.backoff_seconds),
            "maximum_attempts_per_object": self.maximum_attempts_per_object,
            "maximum_bundle_elapsed_seconds": self.maximum_bundle_elapsed_seconds,
            "maximum_credential_transitions_per_object": (
                self.maximum_credential_transitions_per_object
            ),
            "maximum_object_timeout_seconds": self.maximum_object_timeout_seconds,
            "minimum_expected_throughput_bytes_per_second": (
                self.minimum_expected_throughput_bytes_per_second
            ),
            "minimum_object_timeout_seconds": self.minimum_object_timeout_seconds,
            "object_timeout_base_seconds": self.object_timeout_base_seconds,
            "policy_version": self.policy_version,
        }
        object.__setattr__(self, "identity", f"sha256:{_canonical_identity(values)}")

    def timeout_for_size(self, expected_size: int) -> int:
        if type(expected_size) is not int or expected_size <= 0:
            raise TransportError(
                "TRANSPORT_REQUEST_INVALID",
                "Transport object size must be a positive integer",
                phase="request",
            )
        throughput = self.minimum_expected_throughput_bytes_per_second
        size_seconds = (expected_size + throughput - 1) // throughput
        return min(
            self.maximum_object_timeout_seconds,
            max(
                self.minimum_object_timeout_seconds,
                self.object_timeout_base_seconds + size_seconds,
            ),
        )


DEFAULT_TRANSFER_BUDGET_POLICY = TransferBudgetPolicy()


@dataclass(frozen=True)
class TransportObjectPlan:
    logical_name: str
    expected_size: int

    def __post_init__(self) -> None:
        if (
            not isinstance(self.logical_name, str)
            or self.logical_name not in RELEASE_BUNDLE_OBJECTS
            or any(character in self.logical_name for character in "*?[]{}")
            or type(self.expected_size) is not int
            or self.expected_size <= 0
        ):
            raise TransportError(
                "TRANSPORT_REQUEST_INVALID",
                "Transport object plan is outside the closed release bundle contract",
                phase="request",
            )


@dataclass(frozen=True)
class TransportObjectDiagnostic:
    logical_name: str
    expected_size: int
    computed_timeout_seconds: int
    attempt_count: int
    credential_transition_count: int
    failure_classes: tuple[str, ...]
    elapsed_milliseconds: int
    result: str


@dataclass(frozen=True)
class TransportAcquisitionDiagnostics:
    objects: tuple[TransportObjectDiagnostic, ...]
    elapsed_milliseconds: int
    result: str


@dataclass(frozen=True)
class ExplicitTransportPolicy:
    source: TransportSourceId
    selection_origin: TransportSelectionOrigin = (
        TransportSelectionOrigin.EXPLICIT_ADMIN_INPUT
    )
    fallback_allowed: bool = field(default=False, init=False)
    identity: str = field(init=False)

    def __post_init__(self) -> None:
        if type(self.source) is not TransportSourceId:
            raise TransportError(
                "TRANSPORT_POLICY_INVALID",
                "Transport source must be an explicit closed value",
                phase="policy",
            )
        if type(self.selection_origin) is not TransportSelectionOrigin:
            raise TransportError(
                "TRANSPORT_POLICY_INVALID",
                "Transport selection origin must be a closed value",
                phase="policy",
            )
        object.__setattr__(
            self,
            "identity",
            _canonical_identity(
                {
                    "fallback": "forbidden",
                    "policy_version": 1,
                    "selection_origin": self.selection_origin.value,
                    "source": self.source.value,
                }
            ),
        )

    @classmethod
    def github(
        cls,
        *,
        selection_origin: TransportSelectionOrigin = (
            TransportSelectionOrigin.EXPLICIT_ADMIN_INPUT
        ),
    ) -> ExplicitTransportPolicy:
        return cls(
            source=TransportSourceId.GITHUB,
            selection_origin=selection_origin,
        )

    @classmethod
    def official_mirror(
        cls,
        *,
        selection_origin: TransportSelectionOrigin = (
            TransportSelectionOrigin.EXPLICIT_ADMIN_INPUT
        ),
    ) -> ExplicitTransportPolicy:
        return cls(
            source=TransportSourceId.OFFICIAL_MIRROR,
            selection_origin=selection_origin,
        )


@dataclass(frozen=True)
class TransportRequest:
    kind: TransportRequestKind
    exact_version: str
    object_plans: tuple[TransportObjectPlan, ...]
    max_object_bytes: int
    max_total_bytes: int
    identity: str = field(init=False)

    @property
    def objects(self) -> tuple[str, ...]:
        return tuple(item.logical_name for item in self.object_plans)

    def __post_init__(self) -> None:
        if (
            type(self.kind) is not TransportRequestKind
            or self.kind is not TransportRequestKind.RELEASE_BUNDLE
            or not isinstance(self.exact_version, str)
            or _EXACT_VERSION.fullmatch(self.exact_version) is None
            or not isinstance(self.object_plans, tuple)
            or any(type(item) is not TransportObjectPlan for item in self.object_plans)
            or self.objects != RELEASE_BUNDLE_OBJECTS
            or type(self.max_object_bytes) is not int
            or self.max_object_bytes <= 0
            or type(self.max_total_bytes) is not int
            or self.max_total_bytes <= 0
            or any(
                item.expected_size > self.max_object_bytes for item in self.object_plans
            )
            or sum(item.expected_size for item in self.object_plans)
            > self.max_total_bytes
        ):
            raise TransportError(
                "TRANSPORT_REQUEST_INVALID",
                "Transport request is outside the closed release bundle contract",
                phase="request",
            )
        object.__setattr__(
            self,
            "identity",
            _canonical_identity(
                {
                    "exact_version": self.exact_version,
                    "kind": self.kind.value,
                    "max_object_bytes": self.max_object_bytes,
                    "max_total_bytes": self.max_total_bytes,
                    "objects": [
                        {
                            "expected_size": item.expected_size,
                            "logical_name": item.logical_name,
                        }
                        for item in self.object_plans
                    ],
                    "request_version": 1,
                }
            ),
        )

    @classmethod
    def release_bundle(
        cls,
        exact_version: str,
        *,
        object_plans: tuple[TransportObjectPlan, ...],
        max_object_bytes: int = 512 * 1024 * 1024,
        max_total_bytes: int = 1024 * 1024 * 1024,
    ) -> TransportRequest:
        return cls(
            kind=TransportRequestKind.RELEASE_BUNDLE,
            exact_version=exact_version,
            object_plans=object_plans,
            max_object_bytes=max_object_bytes,
            max_total_bytes=max_total_bytes,
        )


@dataclass(frozen=True)
class TransportObjectReceipt:
    logical_name: str
    relative_path: str
    sha256: str
    size: int


@dataclass(frozen=True)
class TransportReceipt:
    transport_id: TransportSourceId
    request_identity: str
    objects: tuple[TransportObjectReceipt, ...]
    identity: str


@dataclass(frozen=True)
class AcquiredTransportSet:
    root: Path
    objects: tuple[TransportObjectReceipt, ...]
    receipt: TransportReceipt
    diagnostics: TransportAcquisitionDiagnostics | None = None

    def material(self, logical_name: str) -> Path:
        matches = [item for item in self.objects if item.logical_name == logical_name]
        if len(matches) != 1:
            raise TransportError(
                "TRANSPORT_OBJECT_MISSING",
                "Transport object is not present in the acquired set",
                transport_id=self.receipt.transport_id,
            )
        root = _validated_staging(self.root)
        candidate = root / matches[0].relative_path
        _validate_regular_material(candidate, root=root)
        observed_size, observed_sha256 = _observed_file_identity(candidate)
        if observed_size != matches[0].size or observed_sha256 != matches[0].sha256:
            raise TransportError(
                "TRANSPORT_RECEIPT_INVALID",
                "Acquired transport object no longer matches its receipt",
                transport_id=self.receipt.transport_id,
            )
        return candidate


class TransportSource(Protocol):
    @property
    def transport_id(self) -> TransportSourceId: ...

    def acquire(
        self,
        request: TransportRequest,
        private_staging: Path,
    ) -> AcquiredTransportSet: ...


def _validated_staging(value: Path) -> Path:
    path = Path(value)
    if not path.is_absolute():
        raise TransportError(
            "TRANSPORT_PATH_UNSAFE",
            "Private transport staging must be absolute",
        )
    try:
        metadata = path.lstat()
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise TransportError(
            "TRANSPORT_PATH_UNSAFE",
            "Private transport staging is unavailable",
        ) from error
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or path.is_symlink()
        or os.path.normcase(str(path.absolute())) != os.path.normcase(str(resolved))
    ):
        raise TransportError(
            "TRANSPORT_PATH_UNSAFE",
            "Private transport staging is not a direct directory",
        )
    if os.name != "nt" and metadata.st_mode & 0o077:
        raise TransportError(
            "TRANSPORT_PATH_UNSAFE",
            "Private transport staging permissions are too broad",
        )
    return resolved


def _validate_regular_material(path: Path, *, root: Path) -> None:
    try:
        metadata = path.lstat()
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise TransportError(
            "TRANSPORT_PATH_UNSAFE",
            "Acquired transport object is unavailable",
        ) from error
    if (
        not stat.S_ISREG(metadata.st_mode)
        or path.is_symlink()
        or metadata.st_nlink != 1
        or resolved.parent != root
    ):
        raise TransportError(
            "TRANSPORT_PATH_UNSAFE",
            "Acquired transport object is not a private regular file",
        )


def _observed_file_identity(path: Path) -> tuple[int, str]:
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise TransportError(
                    "TRANSPORT_PATH_UNSAFE",
                    "Acquired transport object is not a private regular file",
                )
            digest = hashlib.sha256()
            size = 0
            while True:
                chunk = os.read(descriptor, _READ_CHUNK_BYTES)
                if not chunk:
                    break
                size += len(chunk)
                digest.update(chunk)
        finally:
            os.close(descriptor)
    except TransportError:
        raise
    except OSError as error:
        raise TransportError(
            "TRANSPORT_PATH_UNSAFE",
            "Acquired transport object could not be safely opened",
        ) from error
    return size, digest.hexdigest()


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_file(path: Path) -> None:
    descriptor = os.open(
        path,
        os.O_RDWR | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class _OpenWallClockExpired(TimeoutError):
    pass


def _kill_and_reap_bounded_worker(process: subprocess.Popen) -> None:
    if process.poll() is None:
        try:
            process.kill()
        except OSError as error:
            if process.poll() is None:
                raise TransportError(
                    "TRANSPORT_LOCAL_RESOURCE_FAILED",
                    "Bounded transport worker could not be terminated",
                    phase="resource",
                ) from error
    while True:
        try:
            process.wait()
            return
        except (InterruptedError, KeyboardInterrupt):
            continue
        except OSError as error:
            if process.poll() is not None:
                return
            raise TransportError(
                "TRANSPORT_LOCAL_RESOURCE_FAILED",
                "Bounded transport worker could not be reaped",
                phase="resource",
            ) from error


def _file_identity(metadata: os.stat_result) -> tuple[int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
    )


def _read_bounded_worker_metadata(path: Path) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        before = path.lstat()
        descriptor = os.open(path, flags)
    except OSError as error:
        raise TransportError(
            "TRANSPORT_RECEIPT_INVALID",
            "Bounded transport worker returned invalid metadata",
        ) from error
    try:
        try:
            opened = os.fstat(descriptor)
            if (
                not stat.S_ISREG(before.st_mode)
                or not stat.S_ISREG(opened.st_mode)
                or path.is_symlink()
                or before.st_nlink != 1
                or opened.st_nlink != 1
                or _file_identity(before) != _file_identity(opened)
                or not 1 <= opened.st_size <= 8192
            ):
                raise TransportError(
                    "TRANSPORT_RECEIPT_INVALID",
                    "Bounded transport worker returned invalid metadata",
                )
            raw = os.read(descriptor, 8193)
            after = os.fstat(descriptor)
        except OSError as error:
            raise TransportError(
                "TRANSPORT_LOCAL_RESOURCE_FAILED",
                "Bounded transport metadata could not be read",
                phase="resource",
            ) from error
        if len(raw) != opened.st_size or _file_identity(after) != _file_identity(
            opened
        ):
            raise TransportError(
                "TRANSPORT_RECEIPT_INVALID",
                "Bounded transport worker returned invalid metadata",
            )
        return raw
    finally:
        os.close(descriptor)


class _SpoolResponse:
    def __init__(
        self,
        root: Path,
        *,
        body_metadata: os.stat_result,
        final_url: str,
        headers: dict[str, str],
    ) -> None:
        self._root = root
        body = root / "body.bin"
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor: int | None = None
        try:
            descriptor = os.open(body, flags)
            opened = os.fstat(descriptor)
        except OSError as error:
            if descriptor is not None:
                os.close(descriptor)
            raise TransportError(
                "TRANSPORT_LOCAL_RESOURCE_FAILED",
                "Bounded transport material could not be opened",
                phase="resource",
            ) from error
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or _file_identity(opened) != _file_identity(body_metadata)
        ):
            os.close(descriptor)
            raise TransportError(
                "TRANSPORT_RECEIPT_INVALID",
                "Bounded transport worker returned invalid material",
            )
        try:
            self._stream = os.fdopen(descriptor, "rb")
        except OSError as error:
            os.close(descriptor)
            raise TransportError(
                "TRANSPORT_LOCAL_RESOURCE_FAILED",
                "Bounded transport material could not be opened",
                phase="resource",
            ) from error
        self._final_url = final_url
        self.headers = headers

    def settimeout(self, _seconds: float) -> None:
        return None

    def read1(self, size: int = -1) -> bytes:
        return self._stream.read(size)

    def geturl(self) -> str:
        return self._final_url

    def close(self) -> None:
        if not self._stream.closed:
            self._stream.close()
        if self._root.exists():
            try:
                shutil.rmtree(self._root)
            except OSError as error:
                raise TransportError(
                    "TRANSPORT_LOCAL_RESOURCE_FAILED",
                    "Bounded transport workspace could not be removed",
                    phase="resource",
                ) from error

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()


class _AbsoluteDeadlineOpener:
    def __init__(self, *, worker_path: Path | None = None) -> None:
        self._worker_path = worker_path

    def open_with_deadline(
        self,
        request: Request,
        *,
        timeout_seconds: int,
        deadline: float,
        maximum_bytes: int,
    ):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise _OpenWallClockExpired("Transport open deadline was exhausted")
        accept = request.get_header("Accept") or "application/octet-stream"
        try:
            exact_version, logical_name = _bounded_worker_object_identity(
                request.full_url
            )
        except (TypeError, ValueError) as error:
            raise TransportError(
                "TRANSPORT_POLICY_INVALID",
                "Bounded transport worker received an unmanaged URL",
                phase="policy",
            ) from error
        if (
            request.get_method() != "GET"
            or accept not in {"application/json", "application/octet-stream"}
            or type(timeout_seconds) is not int
            or timeout_seconds <= 0
            or type(maximum_bytes) is not int
            or maximum_bytes <= 0
        ):
            raise TransportError(
                "TRANSPORT_POLICY_INVALID",
                "Bounded transport worker received an invalid request",
                phase="policy",
            )
        module_worker = self._worker_path is None
        try:
            worker = (
                Path(__file__) if module_worker else self._worker_path
            ).resolve(strict=True)
            worker_metadata = worker.lstat()
        except OSError as error:
            raise TransportError(
                "TRANSPORT_LOCAL_RESOURCE_FAILED",
                "Bounded transport worker is unavailable",
                phase="resource",
            ) from error
        if (
            not stat.S_ISREG(worker_metadata.st_mode)
            or worker.is_symlink()
            or worker_metadata.st_nlink != 1
        ):
            raise TransportError(
                "TRANSPORT_LOCAL_RESOURCE_FAILED",
                "Bounded transport worker is unsafe",
                phase="resource",
            )
        try:
            temporary_root = Path(tempfile.gettempdir()).resolve(strict=True)
            root = Path(
                tempfile.mkdtemp(
                    prefix="animemo-bounded-http-", dir=temporary_root
                )
            ).resolve(strict=True)
            if (
                root.parent != temporary_root
                or _BOUNDED_WORKSPACE_TOKEN.fullmatch(root.name) is None
            ):
                raise OSError("bounded transport workspace escaped its temporary root")
            os.chmod(root, 0o700)
        except OSError as error:
            raise TransportError(
                "TRANSPORT_LOCAL_RESOURCE_FAILED",
                "Bounded transport workspace could not be created",
                phase="resource",
            ) from error
        body = root / "body.bin"
        metadata = root / "metadata.json"
        worker_arguments = (
            "--version",
            exact_version,
            "--logical-name",
            logical_name,
            "--workspace-token",
            root.name,
            "--socket-timeout",
            str(timeout_seconds),
            "--maximum-bytes",
            str(maximum_bytes),
            "--accept",
            accept,
        )
        if module_worker:
            module_root = worker.parent.parent
            command = (
                sys.executable,
                "-P",
                "-B",
                "-m",
                "updater.transport",
                "--animemo-bounded-http-worker",
                *worker_arguments,
            )
            worker_cwd = module_root
        else:
            command = (
                sys.executable,
                "-P",
                "-B",
                str(worker),
                *worker_arguments,
            )
            worker_cwd = worker.parent
        environment = {
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONSAFEPATH": "1",
        }
        if module_worker:
            environment["PYTHONPATH"] = str(module_root)
        if os.name == "nt":
            environment["TEMP"] = str(temporary_root)
            environment["TMP"] = str(temporary_root)
            for name in ("SYSTEMROOT", "WINDIR"):
                if name in os.environ:
                    environment[name] = os.environ[name]
        else:
            environment["TMPDIR"] = str(temporary_root)
        creationflags = (
            getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        )
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                cwd=worker_cwd,
                env=environment,
                close_fds=True,
                creationflags=creationflags,
            )
        except OSError as error:
            try:
                shutil.rmtree(root)
            except OSError as cleanup_error:
                raise TransportError(
                    "TRANSPORT_LOCAL_RESOURCE_FAILED",
                    "Bounded transport workspace could not be removed",
                    phase="resource",
                ) from cleanup_error
            raise TransportError(
                "TRANSPORT_LOCAL_RESOURCE_FAILED",
                "Bounded transport worker could not start",
                phase="resource",
            ) from error
        try:
            try:
                wait_remaining = deadline - time.monotonic()
                if wait_remaining <= 0:
                    raise _OpenWallClockExpired(
                        "Transport open deadline was exhausted"
                    )
                process.wait(timeout=wait_remaining)
            except subprocess.TimeoutExpired as error:
                raise _OpenWallClockExpired(
                    "Transport open deadline was exhausted"
                ) from error
            finally:
                _kill_and_reap_bounded_worker(process)
            if process.returncode != 0:
                raise TransportError(
                    "TRANSPORT_UNAVAILABLE",
                    "Bounded transport worker failed closed",
                )
            raw = _read_bounded_worker_metadata(metadata)
            try:
                value = json.loads(raw)
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise TransportError(
                    "TRANSPORT_RECEIPT_INVALID",
                    "Bounded transport worker returned invalid metadata",
                ) from error
            if not isinstance(value, dict) or not isinstance(value.get("kind"), str):
                raise TransportError(
                    "TRANSPORT_RECEIPT_INVALID",
                    "Bounded transport worker returned invalid metadata",
                )
            kind = value["kind"]
            if kind == "success":
                if set(value) != {"final_url", "headers", "kind", "size"}:
                    raise TransportError(
                        "TRANSPORT_RECEIPT_INVALID",
                        "Bounded transport worker returned invalid metadata",
                    )
                headers = value["headers"]
                size = value["size"]
                final_url = value["final_url"]
                try:
                    body_metadata = body.lstat()
                    metadata_metadata = metadata.lstat()
                    material_names = {path.name for path in root.iterdir()}
                except OSError as error:
                    raise TransportError(
                        "TRANSPORT_LOCAL_RESOURCE_FAILED",
                        "Bounded transport material could not be inspected",
                        phase="resource",
                    ) from error
                if (
                    not isinstance(headers, dict)
                    or not set(headers).issubset(_BOUNDED_HTTP_RESPONSE_HEADERS)
                    or not all(
                        isinstance(name, str)
                        and isinstance(header, str)
                        and len(header) <= 2048
                        and "\r" not in header
                        and "\n" not in header
                        for name, header in headers.items()
                    )
                    or sum(len(name) + len(header) for name, header in headers.items())
                    > 4096
                    or type(size) is not int
                    or not 0 <= size <= maximum_bytes
                    or not isinstance(final_url, str)
                    or material_names != {"body.bin", "metadata.json"}
                    or not stat.S_ISREG(body_metadata.st_mode)
                    or not stat.S_ISREG(metadata_metadata.st_mode)
                    or body.is_symlink()
                    or metadata.is_symlink()
                    or body_metadata.st_nlink != 1
                    or metadata_metadata.st_nlink != 1
                    or body_metadata.st_size != size
                ):
                    raise TransportError(
                        "TRANSPORT_RECEIPT_INVALID",
                        "Bounded transport worker returned invalid metadata",
                    )
                return _SpoolResponse(
                    root,
                    body_metadata=body_metadata,
                    final_url=final_url,
                    headers=headers,
                )
            if set(value) not in ({"kind"}, {"code", "kind"}):
                raise TransportError(
                    "TRANSPORT_RECEIPT_INVALID",
                    "Bounded transport worker returned invalid metadata",
                )
            if (
                kind == "http-error"
                and type(value.get("code")) is int
                and 100 <= value["code"] <= 599
            ):
                raise HTTPError(
                    request.full_url,
                    value["code"],
                    "bounded transport HTTP failure",
                    {},
                    BytesIO(b""),
                )
            if "code" in value:
                raise TransportError(
                    "TRANSPORT_RECEIPT_INVALID",
                    "Bounded transport worker returned invalid metadata",
                )
            if kind == "redirect":
                raise TransportError(
                    "TRANSPORT_REDIRECT_REJECTED",
                    "Transport redirect was rejected",
                )
            if kind == "timeout":
                raise TimeoutError("bounded transport timeout")
            if kind == "temporary-dns":
                raise URLError(socket.gaierror(socket.EAI_AGAIN, "temporary dns"))
            if kind == "connection-reset":
                raise ConnectionResetError("bounded transport connection reset")
            if kind == "eof":
                raise EOFError("bounded transport eof")
            if kind == "tls-certificate":
                raise URLError(
                    ssl.SSLCertVerificationError(1, "certificate verification failed")
                )
            if kind == "response-too-large":
                raise TransportError(
                    "TRANSPORT_RESPONSE_TOO_LARGE",
                    "Transport response exceeded its resource limit",
                    phase="resource",
                )
            if kind == "local-resource":
                raise TransportError(
                    "TRANSPORT_LOCAL_RESOURCE_FAILED",
                    "Bounded transport worker exhausted a local resource",
                    phase="resource",
                )
            if kind == "network-terminal":
                raise URLError("bounded transport terminal network failure")
            raise TransportError(
                "TRANSPORT_RECEIPT_INVALID",
                "Bounded transport worker returned invalid metadata",
            )
        # Caller interruption must not leave the worker or its spool behind.
        except BaseException:
            if root.exists():
                try:
                    shutil.rmtree(root)
                except OSError as error:
                    raise TransportError(
                        "TRANSPORT_LOCAL_RESOURCE_FAILED",
                        "Bounded transport workspace could not be removed",
                        phase="resource",
                    ) from error
            raise


class _HttpsTransportSource:
    transport_id: TransportSourceId
    origin: str

    def __init__(self, *, opener=None, timeout_seconds: int = 30) -> None:
        if type(timeout_seconds) is not int or timeout_seconds <= 0:
            raise TransportError(
                "TRANSPORT_POLICY_INVALID",
                "Transport timeout must be a positive integer",
                transport_id=self.transport_id,
                phase="policy",
            )
        self._opener = opener or _AbsoluteDeadlineOpener()
        if not callable(getattr(self._opener, "open_with_deadline", None)):
            raise TransportError(
                "TRANSPORT_POLICY_INVALID",
                "Transport opener cannot enforce an absolute connection deadline",
                transport_id=self.transport_id,
                phase="policy",
            )
        self._timeout_seconds = timeout_seconds
        parsed = urlsplit(self.origin)
        if (
            parsed.scheme != "https"
            or not parsed.netloc
            or parsed.path
            or parsed.query
            or parsed.fragment
            or parsed.username
            or parsed.password
        ):
            raise TransportError(
                "TRANSPORT_POLICY_INVALID",
                "Managed transport endpoint is invalid",
                transport_id=self.transport_id,
                phase="policy",
            )

    def _object_url(self, request: TransportRequest, logical_name: str) -> str:
        raise NotImplementedError

    def _preflight(
        self,
        request: TransportRequest,
        *,
        started: float,
    ) -> dict[str, object]:
        del request, started
        return {}

    @staticmethod
    def _ensure_bundle_deadline(started: float) -> None:
        if (
            time.monotonic() - started
            >= DEFAULT_TRANSFER_BUDGET_POLICY.maximum_bundle_elapsed_seconds
        ):
            raise TransportError(
                "TRANSPORT_BUNDLE_DEADLINE_EXHAUSTED",
                "Transport bundle deadline was exhausted",
                phase="deadline",
            )

    def _remaining_transfer_seconds(
        self,
        *,
        attempt_deadline: float,
        bundle_deadline: float,
    ) -> float:
        now = time.monotonic()
        if now >= bundle_deadline:
            raise TransportError(
                "TRANSPORT_BUNDLE_DEADLINE_EXHAUSTED",
                "Transport bundle deadline was exhausted",
                transport_id=self.transport_id,
                phase="deadline",
            )
        if now >= attempt_deadline:
            raise TransportError(
                "TRANSPORT_TIMEOUT",
                "Transport object wall-clock deadline was exhausted",
                transport_id=self.transport_id,
                phase="deadline",
                retriable=True,
            )
        return min(attempt_deadline, bundle_deadline) - now

    def _read_with_deadline(
        self,
        response,
        size: int,
        *,
        attempt_deadline: float,
        bundle_deadline: float,
    ) -> bytes:
        remaining = self._remaining_transfer_seconds(
            attempt_deadline=attempt_deadline,
            bundle_deadline=bundle_deadline,
        )
        timeout_set = False
        candidates = (
            response,
            getattr(response, "fp", None),
            getattr(getattr(response, "fp", None), "raw", None),
            getattr(
                getattr(getattr(response, "fp", None), "raw", None),
                "_sock",
                None,
            ),
        )
        for candidate in candidates:
            setter = getattr(candidate, "settimeout", None)
            if callable(setter):
                setter(max(0.001, remaining))
                timeout_set = True
                break
        reader = getattr(response, "read1", None)
        if not timeout_set or not callable(reader):
            raise TransportError(
                "TRANSPORT_DEADLINE_CONTROL_UNAVAILABLE",
                "Transport response cannot enforce its wall-clock deadline",
                transport_id=self.transport_id,
                phase="deadline",
            )
        chunk = reader(size)
        self._remaining_transfer_seconds(
            attempt_deadline=attempt_deadline,
            bundle_deadline=bundle_deadline,
        )
        return chunk

    def _open_with_deadline(
        self,
        request: Request,
        *,
        timeout_seconds: int,
        attempt_deadline: float,
        bundle_deadline: float,
        maximum_bytes: int,
    ):
        self._remaining_transfer_seconds(
            attempt_deadline=attempt_deadline,
            bundle_deadline=bundle_deadline,
        )
        try:
            value = self._opener.open_with_deadline(
                request,
                timeout_seconds=timeout_seconds,
                deadline=min(attempt_deadline, bundle_deadline),
                maximum_bytes=maximum_bytes,
            )
        except _OpenWallClockExpired as error:
            if bundle_deadline <= attempt_deadline:
                raise TransportError(
                    "TRANSPORT_BUNDLE_DEADLINE_EXHAUSTED",
                    "Transport bundle deadline was exhausted during connection setup",
                    transport_id=self.transport_id,
                    phase="deadline",
                ) from error
            raise TransportError(
                "TRANSPORT_TIMEOUT",
                "Transport object wall-clock deadline was exhausted during connection setup",
                transport_id=self.transport_id,
                phase="deadline",
                retriable=True,
            ) from error
        try:
            self._remaining_transfer_seconds(
                attempt_deadline=attempt_deadline,
                bundle_deadline=bundle_deadline,
            )
        except TransportError:
            closer = getattr(value, "close", None)
            if callable(closer):
                closer()
            raise
        return value

    @staticmethod
    def _network_failure_is_retriable(error: BaseException) -> bool:
        reason = error.reason if isinstance(error, URLError) else error
        if isinstance(reason, TimeoutError):
            return True
        if isinstance(
            reason,
            (
                ConnectionResetError,
                EOFError,
                http.client.IncompleteRead,
                http.client.RemoteDisconnected,
            ),
        ):
            return True
        if isinstance(reason, socket.gaierror):
            return reason.errno == socket.EAI_AGAIN
        if isinstance(reason, ssl.SSLCertVerificationError):
            return False
        return isinstance(reason, OSError) and reason.errno in {
            getattr(socket, "ECONNABORTED", 103),
            getattr(socket, "ECONNRESET", 104),
            getattr(socket, "ETIMEDOUT", 110),
        }

    def acquire(
        self,
        request: TransportRequest,
        private_staging: Path,
    ) -> AcquiredTransportSet:
        if type(request) is not TransportRequest:
            raise TransportError(
                "TRANSPORT_REQUEST_INVALID",
                "Transport request type is invalid",
                transport_id=self.transport_id,
                phase="request",
            )
        staging = _validated_staging(Path(private_staging))
        started = time.monotonic()
        bundle_deadline = (
            started + DEFAULT_TRANSFER_BUDGET_POLICY.maximum_bundle_elapsed_seconds
        )
        preflight = self._preflight(request, started=started)
        expected_sha256 = preflight.get("expected_sha256", {})
        if not isinstance(expected_sha256, dict):
            raise TransportError(
                "TRANSPORT_RECEIPT_INVALID",
                "Transport preflight returned an invalid receipt",
                transport_id=self.transport_id,
            )
        pending = Path(
            tempfile.mkdtemp(prefix=".transport-pending-", dir=staging)
        ).resolve(strict=True)
        os.chmod(pending, 0o700)
        receipts: list[TransportObjectReceipt] = []
        diagnostics: list[TransportObjectDiagnostic] = []
        total_bytes = 0
        committed = False
        final: Path | None = None
        try:
            for plan in request.object_plans:
                logical_name = plan.logical_name
                url = self._object_url(request, logical_name)
                computed_timeout = DEFAULT_TRANSFER_BUDGET_POLICY.timeout_for_size(
                    plan.expected_size
                )
                attempt_started = time.monotonic()
                failures: list[str] = []
                receipt: TransportObjectReceipt | None = None
                attempt_count = 0
                for attempt_count in range(
                    1, DEFAULT_TRANSFER_BUDGET_POLICY.maximum_attempts_per_object + 1
                ):
                    self._ensure_bundle_deadline(started)
                    remaining = (
                        DEFAULT_TRANSFER_BUDGET_POLICY.maximum_bundle_elapsed_seconds
                        - (time.monotonic() - started)
                    )
                    timeout = min(computed_timeout, math.floor(remaining))
                    if timeout <= 0:
                        self._ensure_bundle_deadline(started)
                        raise TransportError(
                            "TRANSPORT_BUNDLE_DEADLINE_EXHAUSTED",
                            "Transport bundle deadline was exhausted",
                            transport_id=self.transport_id,
                            phase="deadline",
                        )
                    try:
                        attempt_deadline = time.monotonic() + computed_timeout
                        receipt = self._download(
                            url,
                            logical_name=logical_name,
                            expected_size=plan.expected_size,
                            expected_sha256=expected_sha256.get(logical_name),
                            destination=pending / logical_name,
                            max_object_bytes=request.max_object_bytes,
                            remaining_total_bytes=request.max_total_bytes - total_bytes,
                            timeout_seconds=timeout,
                            attempt_deadline=attempt_deadline,
                            bundle_deadline=bundle_deadline,
                        )
                        self._ensure_bundle_deadline(started)
                        break
                    except TransportError as error:
                        failures.append(error.code)
                        if (
                            not error.retriable
                            or attempt_count
                            >= DEFAULT_TRANSFER_BUDGET_POLICY.maximum_attempts_per_object
                        ):
                            if error.retriable:
                                raise TransportError(
                                    "TRANSPORT_OBJECT_RETRIES_EXHAUSTED",
                                    "Transport object retries were exhausted",
                                    transport_id=self.transport_id,
                                    retriable=True,
                                ) from error
                            raise
                        backoff = DEFAULT_TRANSFER_BUDGET_POLICY.backoff_seconds[
                            attempt_count - 1
                        ]
                        if time.monotonic() - started + backoff >= (
                            DEFAULT_TRANSFER_BUDGET_POLICY.maximum_bundle_elapsed_seconds
                        ):
                            raise TransportError(
                                "TRANSPORT_BUNDLE_DEADLINE_EXHAUSTED",
                                "Transport bundle deadline was exhausted",
                                transport_id=self.transport_id,
                                phase="deadline",
                            ) from error
                        time.sleep(backoff)
                if receipt is None:
                    raise TransportError(
                        "TRANSPORT_OBJECT_RETRIES_EXHAUSTED",
                        "Transport object retries were exhausted",
                        transport_id=self.transport_id,
                    )
                receipts.append(receipt)
                total_bytes += receipt.size
                diagnostics.append(
                    TransportObjectDiagnostic(
                        logical_name=logical_name,
                        expected_size=plan.expected_size,
                        computed_timeout_seconds=computed_timeout,
                        attempt_count=attempt_count,
                        credential_transition_count=0,
                        failure_classes=tuple(failures),
                        elapsed_milliseconds=round(
                            (time.monotonic() - attempt_started) * 1000
                        ),
                        result="PASS",
                    )
                )
            objects = tuple(receipts)
            receipt_context = {
                key: value
                for key, value in preflight.items()
                if key != "expected_sha256"
            }
            receipt_identity = _canonical_identity(
                {
                    "objects": [
                        {
                            "logical_name": item.logical_name,
                            "relative_path": item.relative_path,
                            "sha256": item.sha256,
                            "size": item.size,
                        }
                        for item in objects
                    ],
                    "receipt_version": 1,
                    "request_identity": request.identity,
                    "source_context": receipt_context,
                    "transport_id": self.transport_id.value,
                }
            )
            receipt = TransportReceipt(
                transport_id=self.transport_id,
                request_identity=request.identity,
                objects=objects,
                identity=receipt_identity,
            )
            self._ensure_bundle_deadline(started)
            _fsync_directory(pending)
            self._ensure_bundle_deadline(started)
            final = staging / (f"acquired-{request.identity[:16]}-{uuid.uuid4().hex}")
            os.replace(pending, final)
            _fsync_directory(staging)
            self._ensure_bundle_deadline(started)
            acquired = AcquiredTransportSet(
                root=final,
                objects=objects,
                receipt=receipt,
                diagnostics=TransportAcquisitionDiagnostics(
                    objects=tuple(diagnostics),
                    elapsed_milliseconds=round((time.monotonic() - started) * 1000),
                    result="PASS",
                ),
            )
            committed = True
            return acquired
        finally:
            if not committed:
                if pending.exists():
                    shutil.rmtree(pending)
                if final is not None and final.exists():
                    shutil.rmtree(final)

    def _download(
        self,
        url: str,
        *,
        logical_name: str,
        expected_size: int,
        expected_sha256: object,
        destination: Path,
        max_object_bytes: int,
        remaining_total_bytes: int,
        timeout_seconds: int,
        attempt_deadline: float,
        bundle_deadline: float,
    ) -> TransportObjectReceipt:
        parsed = urlsplit(url)
        expected = urlsplit(self.origin)
        if (
            parsed.scheme != "https"
            or parsed.netloc != expected.netloc
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise TransportError(
                "TRANSPORT_POLICY_INVALID",
                "Transport object escaped the managed endpoint",
                transport_id=self.transport_id,
                phase="request",
            )
        request = Request(
            url,
            headers={
                "Accept": "application/octet-stream",
                "User-Agent": "AniMemo-Updater",
            },
            method="GET",
        )
        digest = hashlib.sha256()
        size = 0
        try:
            response_context = self._open_with_deadline(
                request,
                timeout_seconds=timeout_seconds,
                attempt_deadline=attempt_deadline,
                bundle_deadline=bundle_deadline,
                maximum_bytes=(
                    min(max_object_bytes, remaining_total_bytes, expected_size) + 1
                ),
            )
            with response_context as response:
                final_url = response.geturl() if hasattr(response, "geturl") else url
                if final_url != url:
                    raise TransportError(
                        "TRANSPORT_REDIRECT_REJECTED",
                        "Transport redirect was rejected",
                        transport_id=self.transport_id,
                    )
                declared_length = response.headers.get("Content-Length")
                declared: int | None = None
                if declared_length is not None:
                    try:
                        declared = int(declared_length)
                    except (TypeError, ValueError) as error:
                        raise TransportError(
                            "TRANSPORT_RECEIPT_INVALID",
                            "Transport response length is invalid",
                            transport_id=self.transport_id,
                        ) from error
                    if (
                        declared < 0
                        or declared > max_object_bytes
                        or declared > remaining_total_bytes
                    ):
                        raise TransportError(
                            "TRANSPORT_RESPONSE_TOO_LARGE",
                            "Transport response exceeded its resource limit",
                            transport_id=self.transport_id,
                        )
                    if declared != expected_size:
                        raise TransportError(
                            "TRANSPORT_OBJECT_SIZE_MISMATCH",
                            "Transport object size differs from exact release metadata",
                            transport_id=self.transport_id,
                        )
                try:
                    descriptor = os.open(
                        destination,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                        0o600,
                    )
                except OSError as error:
                    raise TransportError(
                        "TRANSPORT_LOCAL_RESOURCE_FAILED",
                        "Transport staging could not create a private object",
                        transport_id=self.transport_id,
                        phase="resource",
                    ) from error
                try:
                    with os.fdopen(descriptor, "wb", closefd=True) as output:
                        while True:
                            chunk = self._read_with_deadline(
                                response,
                                _READ_CHUNK_BYTES,
                                attempt_deadline=attempt_deadline,
                                bundle_deadline=bundle_deadline,
                            )
                            if not chunk:
                                break
                            if not isinstance(chunk, bytes):
                                raise TransportError(
                                    "TRANSPORT_RECEIPT_INVALID",
                                    "Transport response returned non-byte content",
                                    transport_id=self.transport_id,
                                )
                            size += len(chunk)
                            if size > max_object_bytes or size > remaining_total_bytes:
                                raise TransportError(
                                    "TRANSPORT_RESPONSE_TOO_LARGE",
                                    "Transport response exceeded its resource limit",
                                    transport_id=self.transport_id,
                                )
                            digest.update(chunk)
                            try:
                                output.write(chunk)
                            except OSError as error:
                                raise TransportError(
                                    "TRANSPORT_LOCAL_RESOURCE_FAILED",
                                    "Transport staging could not persist an object",
                                    transport_id=self.transport_id,
                                    phase="resource",
                                ) from error
                        if declared is not None and declared != size:
                            raise TransportError(
                                "TRANSPORT_INCOMPLETE_RESPONSE",
                                "Transport response ended before its declared length",
                                transport_id=self.transport_id,
                                retriable=True,
                            )
                        try:
                            output.flush()
                            os.fsync(output.fileno())
                        except OSError as error:
                            raise TransportError(
                                "TRANSPORT_LOCAL_RESOURCE_FAILED",
                                "Transport staging could not persist an object",
                                transport_id=self.transport_id,
                                phase="resource",
                            ) from error
                except BaseException:
                    if destination.exists():
                        destination.unlink()
                    raise
        except HTTPError as error:
            if 300 <= error.code < 400:
                code = "TRANSPORT_REDIRECT_REJECTED"
                retriable = False
            elif error.code == 404:
                code = "TRANSPORT_OBJECT_MISSING"
                retriable = False
            elif error.code == 429:
                code = "TRANSPORT_RATE_LIMITED"
                retriable = True
            else:
                code = "TRANSPORT_UNAVAILABLE"
                retriable = error.code in {500, 502, 503, 504}
            raise TransportError(
                code,
                "Transport endpoint did not provide the requested object",
                transport_id=self.transport_id,
                retriable=retriable,
            ) from error
        except TransportError:
            raise
        except TimeoutError as error:
            raise TransportError(
                "TRANSPORT_TIMEOUT",
                "Transport endpoint timed out",
                transport_id=self.transport_id,
                retriable=True,
            ) from error
        except (
            URLError,
            OSError,
            EOFError,
            http.client.IncompleteRead,
            http.client.RemoteDisconnected,
        ) as error:
            raise TransportError(
                "TRANSPORT_UNAVAILABLE",
                "Transport endpoint is unavailable",
                transport_id=self.transport_id,
                retriable=self._network_failure_is_retriable(error),
            ) from error
        _validate_regular_material(destination, root=destination.parent)
        if size != expected_size:
            destination.unlink(missing_ok=True)
            raise TransportError(
                "TRANSPORT_OBJECT_SIZE_MISMATCH",
                "Transport object size differs from exact release metadata",
                transport_id=self.transport_id,
            )
        observed_sha256 = "sha256:" + digest.hexdigest()
        if expected_sha256 is not None and observed_sha256 != expected_sha256:
            destination.unlink(missing_ok=True)
            raise TransportError(
                "TRANSPORT_OBJECT_DIGEST_MISMATCH",
                "Transport object differs from the mirror completeness marker",
                transport_id=self.transport_id,
            )
        return TransportObjectReceipt(
            logical_name=logical_name,
            relative_path=logical_name,
            sha256=digest.hexdigest(),
            size=size,
        )


class GitHubTransportSource(_HttpsTransportSource):
    transport_id = TransportSourceId.GITHUB
    origin = GITHUB_RELEASE_ORIGIN

    def __init__(
        self,
        *,
        runner=None,
        credential_provider=None,
        clock=None,
        sleeper=None,
    ) -> None:
        self._runner = runner if runner is not None else CommandRunner()
        self._credential_provider = credential_provider
        self._clock = clock or time.monotonic
        self._sleeper = sleeper or time.sleep
        self.last_diagnostics: TransportAcquisitionDiagnostics | None = None

    @staticmethod
    def _anonymous_environment(root: Path) -> dict[str, str]:
        root.mkdir(mode=0o700)
        directories = {
            "HOME": root / "home",
            "TMPDIR": root / "tmp",
            "GH_CONFIG_DIR": root / "gh",
            "DOCKER_CONFIG": root / "docker",
        }
        for directory in directories.values():
            directory.mkdir(mode=0o700)
        environment = {name: str(path) for name, path in directories.items()}
        environment.update(
            {
                "GH_PROMPT_DISABLED": "1",
                "LANG": "C.UTF-8",
                "LC_ALL": "C.UTF-8",
            }
        )
        return environment

    @staticmethod
    def _authenticated_environment(
        anonymous: dict[str, str], token: str
    ) -> dict[str, str]:
        authenticated = dict(anonymous)
        authenticated["GH_TOKEN"] = token
        return authenticated

    def acquire(
        self,
        request: TransportRequest,
        private_staging: Path,
    ) -> AcquiredTransportSet:
        if type(request) is not TransportRequest:
            raise TransportError(
                "TRANSPORT_REQUEST_INVALID",
                "Transport request type is invalid",
                transport_id=self.transport_id,
                phase="request",
            )
        staging = _validated_staging(Path(private_staging))
        pending = Path(
            tempfile.mkdtemp(prefix=".transport-pending-", dir=staging)
        ).resolve(strict=True)
        os.chmod(pending, 0o700)
        started = self._clock()
        diagnostics: list[TransportObjectDiagnostic] = []
        receipts: list[TransportObjectReceipt] = []
        total_bytes = 0
        committed = False
        final: Path | None = None
        try:
            for plan in request.object_plans:
                object_started = self._clock()
                computed_timeout = DEFAULT_TRANSFER_BUDGET_POLICY.timeout_for_size(
                    plan.expected_size
                )
                failures: list[str] = []
                attempts = 0
                credential_transitions = 0
                authenticated = False
                token: str | None = None
                object_receipt: TransportObjectReceipt | None = None

                while (
                    attempts
                    < DEFAULT_TRANSFER_BUDGET_POLICY.maximum_attempts_per_object
                ):
                    remaining = self._remaining_bundle_seconds(started)
                    command_timeout = min(computed_timeout, int(remaining))
                    if command_timeout <= 0:
                        self._raise_object_failure(
                            "TRANSPORT_BUNDLE_DEADLINE_EXHAUSTED",
                            "GitHub release bundle deadline was exhausted",
                            plan,
                            computed_timeout,
                            attempts,
                            credential_transitions,
                            failures,
                            object_started,
                            started,
                            diagnostics,
                        )
                    attempts += 1
                    attempt = Path(
                        tempfile.mkdtemp(
                            prefix=f".attempt-{attempts}-{plan.logical_name}-",
                            dir=pending,
                        )
                    ).resolve(strict=True)
                    os.chmod(attempt, 0o700)
                    environment = self._anonymous_environment(attempt / ".runtime")
                    if authenticated:
                        assert token is not None
                        environment = self._authenticated_environment(
                            environment, token
                        )
                    command = [
                        "/usr/bin/gh",
                        "release",
                        "download",
                        f"v{request.exact_version}",
                        "--repo",
                        "yanyuhanyue/AniMemo",
                        "--pattern",
                        plan.logical_name,
                        "--dir",
                        str(attempt),
                    ]
                    try:
                        self._runner.run(
                            command,
                            env=environment,
                            timeout=command_timeout,
                        )
                        if self._remaining_bundle_seconds(started) <= 0:
                            self._raise_transport_error(
                                "TRANSPORT_BUNDLE_DEADLINE_EXHAUSTED",
                                "GitHub release bundle deadline was exhausted",
                            )
                        if {item.name for item in attempt.iterdir()} != {
                            ".runtime",
                            plan.logical_name,
                        }:
                            self._raise_transport_error(
                                "TRANSPORT_OBJECT_SET_INVALID",
                                "GitHub object attempt returned an unexpected file set",
                            )
                        candidate = attempt / plan.logical_name
                        _validate_regular_material(candidate, root=attempt)
                        size, digest = _observed_file_identity(candidate)
                        if size != plan.expected_size:
                            self._raise_transport_error(
                                "TRANSPORT_OBJECT_SIZE_MISMATCH",
                                "GitHub object size differs from exact release metadata",
                            )
                        if (
                            size > request.max_object_bytes
                            or total_bytes + size > request.max_total_bytes
                        ):
                            self._raise_transport_error(
                                "TRANSPORT_RESPONSE_TOO_LARGE",
                                "GitHub transport exceeded its resource limit",
                            )
                        _fsync_file(candidate)
                        os.replace(candidate, pending / plan.logical_name)
                        _fsync_directory(pending)
                        if self._remaining_bundle_seconds(started) <= 0:
                            self._raise_transport_error(
                                "TRANSPORT_BUNDLE_DEADLINE_EXHAUSTED",
                                "GitHub release bundle deadline was exhausted",
                            )
                        total_bytes += size
                        object_receipt = TransportObjectReceipt(
                            logical_name=plan.logical_name,
                            relative_path=plan.logical_name,
                            sha256=digest,
                            size=size,
                        )
                        break
                    except CommandFailed as error:
                        failure_class, code, retriable, transition = (
                            self._classify_command_failure(
                                error,
                                authenticated=authenticated,
                            )
                        )
                        failures.append(failure_class)
                        if transition:
                            if (
                                authenticated
                                or credential_transitions
                                >= DEFAULT_TRANSFER_BUDGET_POLICY.maximum_credential_transitions_per_object
                            ):
                                self._raise_object_failure(
                                    "TRANSPORT_OBJECT_AUTHENTICATION_FAILED",
                                    "GitHub object authentication failed",
                                    plan,
                                    computed_timeout,
                                    attempts,
                                    credential_transitions,
                                    failures,
                                    object_started,
                                    started,
                                    diagnostics,
                                    cause=error,
                                )
                            if (
                                attempts
                                >= DEFAULT_TRANSFER_BUDGET_POLICY.maximum_attempts_per_object
                            ):
                                self._raise_object_failure(
                                    code,
                                    "GitHub object authentication boundary exhausted its attempts",
                                    plan,
                                    computed_timeout,
                                    attempts,
                                    credential_transitions,
                                    failures,
                                    object_started,
                                    started,
                                    diagnostics,
                                    cause=error,
                                )
                            candidate_token = (
                                self._credential_provider()
                                if callable(self._credential_provider)
                                else None
                            )
                            if (
                                not isinstance(candidate_token, str)
                                or not candidate_token
                                or any(
                                    character.isspace() for character in candidate_token
                                )
                            ):
                                self._raise_object_failure(
                                    "TRANSPORT_OBJECT_AUTHENTICATION_REQUIRED",
                                    "GitHub object requires unavailable authentication",
                                    plan,
                                    computed_timeout,
                                    attempts,
                                    credential_transitions,
                                    failures,
                                    object_started,
                                    started,
                                    diagnostics,
                                    cause=error,
                                )
                            token = candidate_token
                            authenticated = True
                            credential_transitions += 1
                            continue
                        if not retriable:
                            self._raise_object_failure(
                                code,
                                "GitHub object acquisition failed closed",
                                plan,
                                computed_timeout,
                                attempts,
                                credential_transitions,
                                failures,
                                object_started,
                                started,
                                diagnostics,
                                cause=error,
                            )
                        if (
                            attempts
                            >= DEFAULT_TRANSFER_BUDGET_POLICY.maximum_attempts_per_object
                        ):
                            terminal_code = (
                                "TRANSPORT_OBJECT_TIMEOUT"
                                if all(item == "COMMAND_TIMEOUT" for item in failures)
                                else "TRANSPORT_OBJECT_TRANSIENT_EXHAUSTED"
                            )
                            self._raise_object_failure(
                                terminal_code,
                                "GitHub object retries were exhausted",
                                plan,
                                computed_timeout,
                                attempts,
                                credential_transitions,
                                failures,
                                object_started,
                                started,
                                diagnostics,
                                cause=error,
                            )
                        delay = DEFAULT_TRANSFER_BUDGET_POLICY.backoff_seconds[
                            attempts - 1
                        ]
                        if self._remaining_bundle_seconds(started) <= delay:
                            self._raise_object_failure(
                                "TRANSPORT_BUNDLE_DEADLINE_EXHAUSTED",
                                "GitHub release bundle deadline was exhausted",
                                plan,
                                computed_timeout,
                                attempts,
                                credential_transitions,
                                failures,
                                object_started,
                                started,
                                diagnostics,
                                cause=error,
                            )
                        self._sleeper(delay)
                    except TransportError as error:
                        failures.append(error.code)
                        self._raise_object_failure(
                            error.code,
                            str(error),
                            plan,
                            computed_timeout,
                            attempts,
                            credential_transitions,
                            failures,
                            object_started,
                            started,
                            diagnostics,
                            cause=error,
                        )
                    except OSError as error:
                        failures.append("LOCAL_OPERATING_SYSTEM_ERROR")
                        self._raise_object_failure(
                            "TRANSPORT_OBJECT_COMMAND_FAILED",
                            "GitHub object acquisition failed in the local operating system",
                            plan,
                            computed_timeout,
                            attempts,
                            credential_transitions,
                            failures,
                            object_started,
                            started,
                            diagnostics,
                            cause=error,
                        )
                    finally:
                        if attempt.exists():
                            shutil.rmtree(attempt)

                if object_receipt is None:
                    self._raise_object_failure(
                        "TRANSPORT_OBJECT_TRANSIENT_EXHAUSTED",
                        "GitHub object attempts were exhausted",
                        plan,
                        computed_timeout,
                        attempts,
                        credential_transitions,
                        failures,
                        object_started,
                        started,
                        diagnostics,
                    )
                receipts.append(object_receipt)
                diagnostics.append(
                    TransportObjectDiagnostic(
                        logical_name=plan.logical_name,
                        expected_size=plan.expected_size,
                        computed_timeout_seconds=computed_timeout,
                        attempt_count=attempts,
                        credential_transition_count=credential_transitions,
                        failure_classes=tuple(failures),
                        elapsed_milliseconds=self._elapsed_milliseconds(object_started),
                        result="PASS",
                    )
                )

            objects = tuple(receipts)
            identity = _canonical_identity(
                {
                    "objects": [
                        {
                            "logical_name": item.logical_name,
                            "relative_path": item.relative_path,
                            "sha256": item.sha256,
                            "size": item.size,
                        }
                        for item in objects
                    ],
                    "receipt_version": 1,
                    "request_identity": request.identity,
                    "transport_id": self.transport_id.value,
                }
            )
            receipt = TransportReceipt(
                transport_id=self.transport_id,
                request_identity=request.identity,
                objects=objects,
                identity=identity,
            )
            acquisition_diagnostics = TransportAcquisitionDiagnostics(
                objects=tuple(diagnostics),
                elapsed_milliseconds=self._elapsed_milliseconds(started),
                result="PASS",
            )
            self._ensure_bundle_commit_deadline(started, diagnostics)
            _fsync_directory(pending)
            self._ensure_bundle_commit_deadline(started, diagnostics)
            final = staging / f"acquired-{request.identity[:16]}-{uuid.uuid4().hex}"
            os.replace(pending, final)
            _fsync_directory(staging)
            self._ensure_bundle_commit_deadline(started, diagnostics)
            self.last_diagnostics = acquisition_diagnostics
            acquired = AcquiredTransportSet(
                final,
                objects,
                receipt,
                acquisition_diagnostics,
            )
            committed = True
            return acquired
        finally:
            if not committed:
                if pending.exists():
                    shutil.rmtree(pending)
                if final is not None and final.exists():
                    shutil.rmtree(final)

    def _remaining_bundle_seconds(self, started: float) -> float:
        return DEFAULT_TRANSFER_BUDGET_POLICY.maximum_bundle_elapsed_seconds - (
            self._clock() - started
        )

    def _elapsed_milliseconds(self, started: float) -> int:
        return max(0, int((self._clock() - started) * 1000))

    def _ensure_bundle_commit_deadline(
        self,
        started: float,
        completed: list[TransportObjectDiagnostic],
    ) -> None:
        if self._remaining_bundle_seconds(started) > 0:
            return
        self.last_diagnostics = TransportAcquisitionDiagnostics(
            objects=tuple(completed),
            elapsed_milliseconds=self._elapsed_milliseconds(started),
            result="FAIL",
        )
        error = TransportError(
            "TRANSPORT_BUNDLE_DEADLINE_EXHAUSTED",
            "GitHub release bundle deadline was exhausted",
            transport_id=self.transport_id,
        )
        error.diagnostics = self.last_diagnostics
        raise error

    def _raise_transport_error(self, code: str, message: str) -> None:
        raise TransportError(code, message, transport_id=self.transport_id)

    def _raise_object_failure(
        self,
        code: str,
        message: str,
        plan: TransportObjectPlan,
        computed_timeout: int,
        attempts: int,
        credential_transitions: int,
        failures: list[str],
        object_started: float,
        bundle_started: float,
        completed: list[TransportObjectDiagnostic],
        *,
        cause: BaseException | None = None,
    ) -> None:
        failed = TransportObjectDiagnostic(
            logical_name=plan.logical_name,
            expected_size=plan.expected_size,
            computed_timeout_seconds=computed_timeout,
            attempt_count=attempts,
            credential_transition_count=credential_transitions,
            failure_classes=tuple(failures),
            elapsed_milliseconds=self._elapsed_milliseconds(object_started),
            result="FAIL",
        )
        self.last_diagnostics = TransportAcquisitionDiagnostics(
            objects=(*completed, failed),
            elapsed_milliseconds=self._elapsed_milliseconds(bundle_started),
            result="FAIL",
        )
        error = TransportError(code, message, transport_id=self.transport_id)
        error.diagnostics = self.last_diagnostics
        if cause is None:
            raise error
        raise error from cause

    @staticmethod
    def _classify_command_failure(
        error: CommandFailed,
        *,
        authenticated: bool,
    ) -> tuple[str, str, bool, bool]:
        if isinstance(error, CommandTimedOut):
            return "COMMAND_TIMEOUT", "TRANSPORT_OBJECT_TIMEOUT", True, False
        if isinstance(error, CommandStartFailed):
            return (
                "COMMAND_START_FAILED",
                "TRANSPORT_OBJECT_COMMAND_FAILED",
                False,
                False,
            )
        if not isinstance(error, CommandExited):
            return (
                "COMMAND_FAILURE_UNKNOWN",
                "TRANSPORT_OBJECT_COMMAND_FAILED",
                False,
                False,
            )
        diagnostic = f"{error.stdout}\n{error.stderr}".lower()
        http_statuses = {
            int(match.group(1)) for match in _HTTP_STATUS.finditer(diagnostic)
        }
        if authenticated and (
            401 in http_statuses
            or 403 in http_statuses
            or "authentication failed" in diagnostic
            or "permission denied" in diagnostic
        ):
            return (
                "AUTHENTICATED_AUTHENTICATION_FAILED",
                "TRANSPORT_OBJECT_AUTHENTICATION_FAILED",
                False,
                False,
            )
        if (
            404 in http_statuses
            or "asset not found" in diagnostic
            or "release not found" in diagnostic
        ):
            return "OBJECT_MISSING", "TRANSPORT_OBJECT_MISSING", False, False
        if not authenticated and 401 in http_statuses:
            return (
                "ANONYMOUS_HTTP_401",
                "TRANSPORT_OBJECT_AUTHENTICATION_REQUIRED",
                False,
                True,
            )
        if (
            not authenticated
            and 403 in http_statuses
            and (
                "authentication" in diagnostic
                or "requires auth" in diagnostic
                or "permission denied" in diagnostic
            )
        ):
            return (
                "ANONYMOUS_HTTP_403_AUTH_REQUIRED",
                "TRANSPORT_OBJECT_AUTHENTICATION_REQUIRED",
                False,
                True,
            )
        rate_limited = 429 in http_statuses or "rate limit exceeded" in diagnostic
        authenticated_rate_limit_benefit = any(
            marker in diagnostic
            for marker in (
                "authenticated requests get a higher rate limit",
                "try authenticating",
                "authentication would increase",
            )
        )
        if not authenticated and rate_limited and authenticated_rate_limit_benefit:
            return (
                "ANONYMOUS_RATE_LIMITED_WITH_AUTH_BENEFIT",
                "TRANSPORT_OBJECT_AUTHENTICATION_REQUIRED",
                True,
                True,
            )
        if rate_limited:
            mode = "AUTHENTICATED" if authenticated else "ANONYMOUS"
            return (
                f"{mode}_HTTP_429",
                "TRANSPORT_OBJECT_TRANSIENT_EXHAUSTED",
                True,
                False,
            )
        retryable_markers = (
            "connection reset",
            "unexpected eof",
            "transport eof",
            "connection timed out",
            "tls handshake timeout",
            "temporary failure in name resolution",
            "temporary dns",
        )
        if http_statuses.intersection({500, 502, 503, 504}) or any(
            marker in diagnostic for marker in retryable_markers
        ):
            return (
                "TRANSIENT_COMMAND_EXIT",
                "TRANSPORT_OBJECT_TRANSIENT_EXHAUSTED",
                True,
                False,
            )
        return (
            "COMMAND_EXIT_TERMINAL",
            "TRANSPORT_OBJECT_COMMAND_FAILED",
            False,
            False,
        )

    def _object_url(self, request: TransportRequest, logical_name: str) -> str:
        tag = quote(f"v{request.exact_version}", safe="")
        name = quote(logical_name, safe="")
        return f"{self.origin}/yanyuhanyue/AniMemo/releases/download/{tag}/{name}"


class OfficialMirrorTransportSource(_HttpsTransportSource):
    transport_id = TransportSourceId.OFFICIAL_MIRROR
    origin = OFFICIAL_MIRROR_ORIGIN

    def __init__(
        self,
        *,
        endpoint_id: str = OFFICIAL_MIRROR_ENDPOINT_ID,
        opener=None,
    ) -> None:
        if endpoint_id != OFFICIAL_MIRROR_ENDPOINT_ID:
            raise TransportError(
                "TRANSPORT_SOURCE_UNSUPPORTED",
                "Official mirror endpoint is not managed by this build",
                transport_id=self.transport_id,
                phase="policy",
            )
        self.endpoint_id = endpoint_id
        super().__init__(opener=opener, timeout_seconds=30)

    def _receipt_url(self, request: TransportRequest) -> str:
        tag = quote(f"v{request.exact_version}", safe="")
        return (
            f"{self.origin}/{MIRROR_PATH_PREFIX}/{tag}/{MIRROR_RECEIPT_NAME}"
        )

    def _read_receipt_once(
        self,
        request: TransportRequest,
        *,
        timeout_seconds: int,
        attempt_deadline: float,
        bundle_deadline: float,
    ) -> dict[str, object]:
        url = self._receipt_url(request)
        parsed = urlsplit(url)
        expected = urlsplit(self.origin)
        if (
            parsed.scheme != "https"
            or parsed.netloc != expected.netloc
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise TransportError(
                "TRANSPORT_POLICY_INVALID",
                "Official mirror marker escaped the managed endpoint",
                transport_id=self.transport_id,
                phase="request",
            )
        http_request = Request(
            url,
            headers={
                "Accept": "application/json",
                "User-Agent": "AniMemo-Updater",
            },
            method="GET",
        )
        try:
            with self._open_with_deadline(
                http_request,
                timeout_seconds=timeout_seconds,
                attempt_deadline=attempt_deadline,
                bundle_deadline=bundle_deadline,
                maximum_bytes=MAX_MIRROR_RECEIPT_BYTES + 1,
            ) as response:
                final_url = response.geturl() if hasattr(response, "geturl") else url
                if final_url != url:
                    raise TransportError(
                        "TRANSPORT_REDIRECT_REJECTED",
                        "Official mirror marker redirect was rejected",
                        transport_id=self.transport_id,
                    )
                length = response.headers.get("Content-Length")
                declared: int | None = None
                if length is not None:
                    try:
                        declared = int(length)
                    except (TypeError, ValueError) as error:
                        raise TransportError(
                            "MIRROR_RECEIPT_INVALID",
                            "Official mirror marker length is invalid",
                            transport_id=self.transport_id,
                        ) from error
                    if not 1 <= declared <= MAX_MIRROR_RECEIPT_BYTES:
                        raise TransportError(
                            "MIRROR_RECEIPT_INVALID",
                            "Official mirror marker length is invalid",
                            transport_id=self.transport_id,
                        )
                chunks: list[bytes] = []
                observed = 0
                while True:
                    chunk = self._read_with_deadline(
                        response,
                        min(_READ_CHUNK_BYTES, MAX_MIRROR_RECEIPT_BYTES + 1 - observed),
                        attempt_deadline=attempt_deadline,
                        bundle_deadline=bundle_deadline,
                    )
                    if not isinstance(chunk, bytes):
                        raise TransportError(
                            "MIRROR_RECEIPT_INVALID",
                            "Official mirror marker returned non-byte content",
                            transport_id=self.transport_id,
                        )
                    if not chunk:
                        break
                    chunks.append(chunk)
                    observed += len(chunk)
                    if observed > MAX_MIRROR_RECEIPT_BYTES:
                        raise TransportError(
                            "MIRROR_RECEIPT_INVALID",
                            "Official mirror marker exceeded its resource limit",
                            transport_id=self.transport_id,
                        )
                data = b"".join(chunks)
                if declared is not None and declared != len(data):
                    raise TransportError(
                        "MIRROR_RECEIPT_INCOMPLETE",
                        "Official mirror marker ended before its declared length",
                        transport_id=self.transport_id,
                        retriable=True,
                    )
        except HTTPError as error:
            if error.code == 404:
                code = "MIRROR_RECEIPT_MISSING"
                retriable = False
            elif error.code == 429 or error.code in {500, 502, 503, 504}:
                code = "MIRROR_RECEIPT_UNAVAILABLE"
                retriable = True
            elif 300 <= error.code < 400:
                code = "TRANSPORT_REDIRECT_REJECTED"
                retriable = False
            else:
                code = "MIRROR_RECEIPT_UNAVAILABLE"
                retriable = False
            raise TransportError(
                code,
                "Official mirror completeness marker is unavailable",
                transport_id=self.transport_id,
                retriable=retriable,
            ) from error
        except TransportError:
            raise
        except TimeoutError as error:
            raise TransportError(
                "MIRROR_RECEIPT_UNAVAILABLE",
                "Official mirror completeness marker is unavailable",
                transport_id=self.transport_id,
                retriable=True,
            ) from error
        except (
            URLError,
            OSError,
            EOFError,
            http.client.IncompleteRead,
            http.client.RemoteDisconnected,
        ) as error:
            raise TransportError(
                "MIRROR_RECEIPT_UNAVAILABLE",
                "Official mirror completeness marker is unavailable",
                transport_id=self.transport_id,
                retriable=self._network_failure_is_retriable(error),
            ) from error
        try:
            receipt = load_mirror_receipt_bytes(data)
        except MirrorError as error:
            raise TransportError(
                "MIRROR_RECEIPT_INVALID",
                "Official mirror completeness marker is invalid",
                transport_id=self.transport_id,
            ) from error
        expected_tag = f"v{request.exact_version}"
        if receipt["releaseTag"] != expected_tag:
            raise TransportError(
                "MIRROR_RECEIPT_INVALID",
                "Official mirror marker tag differs from the exact request",
                transport_id=self.transport_id,
            )
        by_name = {item["name"]: item for item in receipt["assets"]}
        for plan in request.object_plans:
            item = by_name.get(plan.logical_name)
            if item is None or item["size"] != plan.expected_size:
                raise TransportError(
                    "MIRROR_RECEIPT_INVALID",
                    "Official mirror marker differs from GitHub release metadata",
                    transport_id=self.transport_id,
                )
        return {
            "receipt_digest": receipt["receiptDigest"],
            "expected_sha256": {
                plan.logical_name: by_name[plan.logical_name]["sha256"]
                for plan in request.object_plans
            },
        }

    def _preflight(
        self,
        request: TransportRequest,
        *,
        started: float,
    ) -> dict[str, object]:
        timeout = DEFAULT_TRANSFER_BUDGET_POLICY.timeout_for_size(
            MAX_MIRROR_RECEIPT_BYTES
        )
        for attempt in range(
            1, DEFAULT_TRANSFER_BUDGET_POLICY.maximum_attempts_per_object + 1
        ):
            self._ensure_bundle_deadline(started)
            remaining = (
                DEFAULT_TRANSFER_BUDGET_POLICY.maximum_bundle_elapsed_seconds
                - (time.monotonic() - started)
            )
            try:
                attempt_deadline = time.monotonic() + timeout
                result = self._read_receipt_once(
                    request,
                    timeout_seconds=min(timeout, math.floor(remaining)),
                    attempt_deadline=attempt_deadline,
                    bundle_deadline=(
                        started
                        + DEFAULT_TRANSFER_BUDGET_POLICY.maximum_bundle_elapsed_seconds
                    ),
                )
                self._ensure_bundle_deadline(started)
                return result
            except TransportError as error:
                if (
                    not error.retriable
                    or attempt
                    >= DEFAULT_TRANSFER_BUDGET_POLICY.maximum_attempts_per_object
                ):
                    if error.retriable:
                        raise TransportError(
                            "MIRROR_RECEIPT_RETRIES_EXHAUSTED",
                            "Official mirror marker retries were exhausted",
                            transport_id=self.transport_id,
                            retriable=True,
                        ) from error
                    raise
                backoff = DEFAULT_TRANSFER_BUDGET_POLICY.backoff_seconds[attempt - 1]
                if time.monotonic() - started + backoff >= (
                    DEFAULT_TRANSFER_BUDGET_POLICY.maximum_bundle_elapsed_seconds
                ):
                    raise TransportError(
                        "TRANSPORT_BUNDLE_DEADLINE_EXHAUSTED",
                        "Transport bundle deadline was exhausted",
                        transport_id=self.transport_id,
                        phase="deadline",
                    ) from error
                time.sleep(backoff)
        raise TransportError(
            "MIRROR_RECEIPT_RETRIES_EXHAUSTED",
            "Official mirror marker retries were exhausted",
            transport_id=self.transport_id,
        )

    def _object_url(self, request: TransportRequest, logical_name: str) -> str:
        version = quote(f"v{request.exact_version}", safe="")
        name = quote(logical_name, safe="")
        return f"{self.origin}/{MIRROR_PATH_PREFIX}/{version}/{name}"


class _BoundedWorkerResponseTooLarge(Exception):
    pass


class _BoundedWorkerLocalResourceFailure(Exception):
    pass


class _BoundedWorkerRejectRedirects(HTTPRedirectHandler):
    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        del request, file_pointer, code, message, headers, new_url


def _bounded_worker_parse_arguments(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    parser.add_argument("--version", required=True)
    parser.add_argument(
        "--logical-name", required=True, choices=sorted(_BOUNDED_WORKER_OBJECTS)
    )
    parser.add_argument("--workspace-token", required=True)
    parser.add_argument("--socket-timeout", required=True, type=int)
    parser.add_argument("--maximum-bytes", required=True, type=int)
    parser.add_argument(
        "--accept",
        required=True,
        choices=("application/json", "application/octet-stream"),
    )
    arguments = parser.parse_args(argv)
    if _BOUNDED_WORKSPACE_TOKEN.fullmatch(arguments.workspace_token) is None:
        raise SystemExit(2)
    try:
        temporary_root = Path(tempfile.gettempdir()).resolve(strict=True)
        workspace = temporary_root / arguments.workspace_token
        resolved_workspace = workspace.resolve(strict=True)
        workspace_metadata = workspace.lstat()
    except OSError:
        raise SystemExit(2) from None
    if (
        _EXACT_VERSION.fullmatch(arguments.version) is None
        or arguments.socket_timeout <= 0
        or not 0 < arguments.maximum_bytes <= (2**63 - 1)
        or not workspace.is_absolute()
        or workspace.is_symlink()
        or not stat.S_ISDIR(workspace_metadata.st_mode)
        or resolved_workspace.parent != temporary_root
        or not resolved_workspace.name.startswith("animemo-bounded-http-")
        or os.path.normcase(str(workspace.absolute()))
        != os.path.normcase(str(resolved_workspace))
    ):
        raise SystemExit(2)
    if os.name != "nt" and workspace_metadata.st_mode & 0o077:
        raise SystemExit(2)
    arguments.url = _bounded_worker_object_url(
        arguments.version, arguments.logical_name
    )
    arguments.body = resolved_workspace / "body.bin"
    arguments.metadata = resolved_workspace / "metadata.json"
    if arguments.body.exists() or arguments.metadata.exists():
        raise SystemExit(2)
    return arguments


def _bounded_worker_write_exclusive(path: Path, payload: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
        try:
            with os.fdopen(descriptor, "wb", closefd=False) as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
        finally:
            os.close(descriptor)
    except OSError as error:
        raise _BoundedWorkerLocalResourceFailure from error


def _bounded_worker_write_metadata(path: Path, value: dict[str, object]) -> None:
    payload = (
        json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
        + "\n"
    ).encode("ascii")
    if not 1 <= len(payload) <= 8192:
        raise _BoundedWorkerLocalResourceFailure
    _bounded_worker_write_exclusive(path, payload)


def _bounded_worker_response_headers(response) -> dict[str, str]:
    selected: dict[str, str] = {}
    total = 0
    for name in sorted(_BOUNDED_HTTP_RESPONSE_HEADERS):
        value = response.headers.get(name)
        if value is None:
            continue
        if not isinstance(value, str) or "\r" in value or "\n" in value:
            raise URLError("invalid response header")
        total += len(name) + len(value)
        if len(value) > 2048 or total > 4096:
            raise URLError("oversized response headers")
        selected[name] = value
    return selected


def _bounded_worker_download(arguments: argparse.Namespace) -> dict[str, object]:
    request = Request(
        arguments.url,
        headers={
            "Accept": arguments.accept,
            "User-Agent": "AniMemo-Updater",
        },
        method="GET",
    )
    opener = build_opener(_BoundedWorkerRejectRedirects())
    response = opener.open(request, timeout=arguments.socket_timeout)

    try:
        final_url = response.geturl()
        headers = _bounded_worker_response_headers(response)
        declared = headers.get("Content-Length")
        if declared is not None:
            try:
                declared_size = int(declared)
            except ValueError as error:
                raise URLError("invalid content length") from error
            if declared_size < 0 or declared_size > arguments.maximum_bytes:
                raise _BoundedWorkerResponseTooLarge
        reader = getattr(response, "read1", None)
        if not callable(reader):
            raise URLError("bounded response reader unavailable")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        try:
            descriptor = os.open(arguments.body, flags, 0o600)
        except OSError as error:
            raise _BoundedWorkerLocalResourceFailure from error
        size = 0
        try:
            with os.fdopen(descriptor, "wb", closefd=False) as stream:
                while True:
                    chunk = reader(_READ_CHUNK_BYTES)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > arguments.maximum_bytes:
                        raise _BoundedWorkerResponseTooLarge
                    try:
                        stream.write(chunk)
                    except OSError as error:
                        raise _BoundedWorkerLocalResourceFailure from error
                try:
                    stream.flush()
                    os.fsync(stream.fileno())
                except OSError as error:
                    raise _BoundedWorkerLocalResourceFailure from error
        finally:
            os.close(descriptor)
    finally:
        response.close()
    return {
        "final_url": final_url,
        "headers": headers,
        "kind": "success",
        "size": size,
    }


def _bounded_worker_failure_kind(error: BaseException) -> dict[str, object]:
    if isinstance(error, HTTPError):
        if 300 <= error.code <= 399:
            return {"kind": "redirect"}
        return {"code": error.code, "kind": "http-error"}
    reason = error.reason if isinstance(error, URLError) else error
    if isinstance(reason, (TimeoutError, socket.timeout)):
        return {"kind": "timeout"}
    if isinstance(reason, socket.gaierror):
        if reason.errno == socket.EAI_AGAIN:
            return {"kind": "temporary-dns"}
        return {"kind": "network-terminal"}
    if isinstance(reason, ssl.SSLCertVerificationError):
        return {"kind": "tls-certificate"}
    if isinstance(
        reason,
        (ConnectionResetError, http.client.RemoteDisconnected),
    ):
        return {"kind": "connection-reset"}
    if isinstance(reason, (EOFError, http.client.IncompleteRead)):
        return {"kind": "eof"}
    if isinstance(error, _BoundedWorkerResponseTooLarge):
        return {"kind": "response-too-large"}
    if isinstance(error, _BoundedWorkerLocalResourceFailure):
        return {"kind": "local-resource"}
    if isinstance(reason, OSError):
        if reason.errno == errno.ETIMEDOUT:
            return {"kind": "timeout"}
        if reason.errno in {errno.ECONNABORTED, errno.ECONNRESET}:
            return {"kind": "connection-reset"}
    return {"kind": "network-terminal"}


def _bounded_http_worker_main(argv: list[str]) -> int:
    arguments = _bounded_worker_parse_arguments(argv)
    try:
        result = _bounded_worker_download(arguments)
    except Exception as error:  # noqa: BLE001 - serialize only a closed failure kind
        try:
            arguments.body.unlink(missing_ok=True)
            _bounded_worker_write_metadata(
                arguments.metadata,
                _bounded_worker_failure_kind(error),
            )
        except (OSError, _BoundedWorkerLocalResourceFailure):
            return 2
        return 0
    try:
        _bounded_worker_write_metadata(arguments.metadata, result)
    except _BoundedWorkerLocalResourceFailure:
        try:
            arguments.body.unlink(missing_ok=True)
        except OSError:
            pass
        return 2
    return 0


if __name__ == "__main__":
    if sys.argv[1:2] != ["--animemo-bounded-http-worker"]:
        raise SystemExit(2)
    raise SystemExit(_bounded_http_worker_main(sys.argv[2:]))
