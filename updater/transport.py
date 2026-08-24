from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

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
OFFICIAL_MIRROR_ORIGIN = "https://download.animemo.app"
_READ_CHUNK_BYTES = 1024 * 1024


class _RejectRedirects(HTTPRedirectHandler):
    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        del request, file_pointer, code, message, headers, new_url


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
        self._opener = opener or build_opener(_RejectRedirects())
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
        receipts: list[TransportObjectReceipt] = []
        total_bytes = 0
        committed = False
        final: Path | None = None
        try:
            for plan in request.object_plans:
                logical_name = plan.logical_name
                url = self._object_url(request, logical_name)
                receipt = self._download(
                    url,
                    logical_name=logical_name,
                    expected_size=plan.expected_size,
                    destination=pending / logical_name,
                    max_object_bytes=request.max_object_bytes,
                    remaining_total_bytes=request.max_total_bytes - total_bytes,
                )
                receipts.append(receipt)
                total_bytes += receipt.size
            objects = tuple(receipts)
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
                    "transport_id": self.transport_id.value,
                }
            )
            receipt = TransportReceipt(
                transport_id=self.transport_id,
                request_identity=request.identity,
                objects=objects,
                identity=receipt_identity,
            )
            _fsync_directory(pending)
            final = staging / (f"acquired-{request.identity[:16]}-{uuid.uuid4().hex}")
            os.replace(pending, final)
            _fsync_directory(staging)
            acquired = AcquiredTransportSet(
                root=final,
                objects=objects,
                receipt=receipt,
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
        destination: Path,
        max_object_bytes: int,
        remaining_total_bytes: int,
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
            response_context = self._opener.open(
                request,
                timeout=self._timeout_seconds,
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
                descriptor = os.open(
                    destination,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                )
                try:
                    with os.fdopen(descriptor, "wb", closefd=True) as output:
                        while True:
                            chunk = response.read(_READ_CHUNK_BYTES)
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
                            output.write(chunk)
                        if declared is not None and declared != size:
                            raise TransportError(
                                "TRANSPORT_RECEIPT_INVALID",
                                "Transport response length did not match its receipt",
                                transport_id=self.transport_id,
                            )
                        output.flush()
                        os.fsync(output.fileno())
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
                retriable = 500 <= error.code < 600
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
        except URLError as error:
            if isinstance(error.reason, TimeoutError):
                raise TransportError(
                    "TRANSPORT_TIMEOUT",
                    "Transport endpoint timed out",
                    transport_id=self.transport_id,
                    retriable=True,
                ) from error
            raise TransportError(
                "TRANSPORT_UNAVAILABLE",
                "Transport endpoint is unavailable",
                transport_id=self.transport_id,
                retriable=True,
            ) from error
        except OSError as error:
            raise TransportError(
                "TRANSPORT_UNAVAILABLE",
                "Transport endpoint is unavailable",
                transport_id=self.transport_id,
                retriable=True,
            ) from error
        _validate_regular_material(destination, root=destination.parent)
        if size != expected_size:
            destination.unlink(missing_ok=True)
            raise TransportError(
                "TRANSPORT_OBJECT_SIZE_MISMATCH",
                "Transport object size differs from exact release metadata",
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

    def _object_url(self, request: TransportRequest, logical_name: str) -> str:
        version = quote(request.exact_version, safe="")
        name = quote(logical_name, safe="")
        return f"{self.origin}/github/yanyuhanyue/AniMemo/releases/v{version}/{name}"
