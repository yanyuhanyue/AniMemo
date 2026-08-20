from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .commands import CommandRunner
from .errors import CommandFailed, RequestRejected


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
    objects: tuple[str, ...]
    max_object_bytes: int
    max_total_bytes: int
    identity: str = field(init=False)

    def __post_init__(self) -> None:
        if (
            type(self.kind) is not TransportRequestKind
            or self.kind is not TransportRequestKind.RELEASE_BUNDLE
            or not isinstance(self.exact_version, str)
            or _EXACT_VERSION.fullmatch(self.exact_version) is None
            or self.objects != RELEASE_BUNDLE_OBJECTS
            or type(self.max_object_bytes) is not int
            or self.max_object_bytes <= 0
            or type(self.max_total_bytes) is not int
            or self.max_total_bytes <= 0
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
                    "objects": list(self.objects),
                    "request_version": 1,
                }
            ),
        )

    @classmethod
    def release_bundle(
        cls,
        exact_version: str,
        *,
        max_object_bytes: int = 512 * 1024 * 1024,
        max_total_bytes: int = 1024 * 1024 * 1024,
    ) -> TransportRequest:
        return cls(
            kind=TransportRequestKind.RELEASE_BUNDLE,
            exact_version=exact_version,
            objects=RELEASE_BUNDLE_OBJECTS,
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
        if (
            observed_size != matches[0].size
            or observed_sha256 != matches[0].sha256
        ):
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
        try:
            for logical_name in request.objects:
                url = self._object_url(request, logical_name)
                receipt = self._download(
                    url,
                    logical_name=logical_name,
                    destination=pending / logical_name,
                    max_object_bytes=request.max_object_bytes,
                    remaining_total_bytes=request.max_total_bytes - total_bytes,
                )
                receipts.append(receipt)
                total_bytes += receipt.size
            _fsync_directory(pending)
            final = staging / (
                f"acquired-{request.identity[:16]}-{uuid.uuid4().hex}"
            )
            os.replace(pending, final)
            committed = True
            _fsync_directory(staging)
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
            return AcquiredTransportSet(
                root=final,
                objects=objects,
                receipt=receipt,
            )
        finally:
            if not committed and pending.exists():
                shutil.rmtree(pending)

    def _download(
        self,
        url: str,
        *,
        logical_name: str,
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
                final_url = (
                    response.geturl() if hasattr(response, "geturl") else url
                )
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
                            if (
                                size > max_object_bytes
                                or size > remaining_total_bytes
                            ):
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
        opener=None,
        timeout_seconds: int = 60,
    ) -> None:
        self._runner = runner if runner is not None else (
            None if opener is not None else CommandRunner()
        )
        self._credential_provider = credential_provider
        super().__init__(opener=opener, timeout_seconds=timeout_seconds)

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

    def acquire(
        self,
        request: TransportRequest,
        private_staging: Path,
    ) -> AcquiredTransportSet:
        if self._runner is None:
            return super().acquire(request, private_staging)
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
        committed = False
        try:
            environment = self._anonymous_environment(pending / ".runtime")
            command = [
                "/usr/bin/gh",
                "release",
                "download",
                f"v{request.exact_version}",
                "--repo",
                "yanyuhanyue/AniMemo",
            ]
            for logical_name in request.objects:
                command.extend(("--pattern", logical_name))
            command.extend(("--dir", str(pending)))
            try:
                self._runner.run(command, env=environment, timeout=self._timeout_seconds)
            except CommandFailed as anonymous_error:
                token = (
                    self._credential_provider()
                    if callable(self._credential_provider)
                    else None
                )
                if not isinstance(token, str) or not token:
                    raise TransportError(
                        "TRANSPORT_UNAVAILABLE",
                        "GitHub transport is unavailable",
                        transport_id=self.transport_id,
                        retriable=True,
                    ) from anonymous_error
                for logical_name in request.objects:
                    candidate = pending / logical_name
                    if candidate.exists() and not candidate.is_symlink():
                        candidate.unlink()
                authenticated = dict(environment)
                authenticated["GH_TOKEN"] = token
                try:
                    self._runner.run(
                        command,
                        env=authenticated,
                        timeout=self._timeout_seconds,
                    )
                except CommandFailed as error:
                    raise TransportError(
                        "TRANSPORT_UNAVAILABLE",
                        "GitHub transport is unavailable",
                        transport_id=self.transport_id,
                        retriable=True,
                    ) from error

            allowed_runtime = {".runtime", *request.objects}
            if {item.name for item in pending.iterdir()} != allowed_runtime:
                raise TransportError(
                    "TRANSPORT_INCOMPLETE",
                    "GitHub release asset set is incomplete or unexpected",
                    transport_id=self.transport_id,
                )
            receipts: list[TransportObjectReceipt] = []
            total_bytes = 0
            for logical_name in request.objects:
                candidate = pending / logical_name
                _validate_regular_material(candidate, root=pending)
                size, digest = _observed_file_identity(candidate)
                total_bytes += size
                if size > request.max_object_bytes or total_bytes > request.max_total_bytes:
                    raise TransportError(
                        "TRANSPORT_RESPONSE_TOO_LARGE",
                        "GitHub transport exceeded its resource limit",
                        transport_id=self.transport_id,
                    )
                receipts.append(
                    TransportObjectReceipt(
                        logical_name=logical_name,
                        relative_path=logical_name,
                        sha256=digest,
                        size=size,
                    )
                )
            shutil.rmtree(pending / ".runtime")
            _fsync_directory(pending)
            final = staging / f"acquired-{request.identity[:16]}-{uuid.uuid4().hex}"
            os.replace(pending, final)
            committed = True
            _fsync_directory(staging)
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
            return AcquiredTransportSet(final, objects, receipt)
        finally:
            if not committed and pending.exists():
                shutil.rmtree(pending)

    def _object_url(self, request: TransportRequest, logical_name: str) -> str:
        tag = quote(f"v{request.exact_version}", safe="")
        name = quote(logical_name, safe="")
        return (
            f"{self.origin}/yanyuhanyue/AniMemo/releases/download/"
            f"{tag}/{name}"
        )


class OfficialMirrorTransportSource(_HttpsTransportSource):
    transport_id = TransportSourceId.OFFICIAL_MIRROR
    origin = OFFICIAL_MIRROR_ORIGIN

    def __init__(
        self,
        *,
        endpoint_id: str = OFFICIAL_MIRROR_ENDPOINT_ID,
        opener=None,
        timeout_seconds: int = 30,
    ) -> None:
        if endpoint_id != OFFICIAL_MIRROR_ENDPOINT_ID:
            raise TransportError(
                "TRANSPORT_SOURCE_UNSUPPORTED",
                "Official mirror endpoint is not managed by this build",
                transport_id=self.transport_id,
                phase="policy",
            )
        self.endpoint_id = endpoint_id
        super().__init__(opener=opener, timeout_seconds=timeout_seconds)

    def _object_url(self, request: TransportRequest, logical_name: str) -> str:
        version = quote(request.exact_version, safe="")
        name = quote(logical_name, safe="")
        return (
            f"{self.origin}/github/yanyuhanyue/AniMemo/releases/"
            f"v{version}/{name}"
        )
