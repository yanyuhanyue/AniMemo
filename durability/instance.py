from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import secrets
import stat
import tempfile
import time
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any, Protocol
from urllib.parse import urlsplit
from uuid import UUID

APP_ROOT = PurePosixPath("/opt/animemo")
DATA_ROOT = PurePosixPath("/data/animemo")
UPDATER_APP_ROOT = PurePosixPath("/opt/animemo-updater")
UPDATER_STATE_ROOT = PurePosixPath("/var/lib/animemo-updater")
UPDATER_RUNTIME_ROOT = PurePosixPath("/run/animemo-updater")
INSTANCE_LOCATOR_PATH = UPDATER_STATE_ROOT / "instance.json"
MANAGED_CONFIG_ROOT = DATA_ROOT / "config"
MANAGED_CONFIG_PATH = MANAGED_CONFIG_ROOT / "animemo.json"
BACKUP_ROOT = DATA_ROOT / "backups"
UPDATER_SOCKET_PATH = UPDATER_RUNTIME_ROOT / "updater.sock"

LOCATOR_SCHEMA_VERSION = 1
STANDARD_DEPLOYMENT_PROFILE = "v1.1-standard"
MAX_LOCATOR_BYTES = 1024 * 1024

_REQUIRED_FIELDS = frozenset(
    {
        "schemaVersion",
        "instanceId",
        "appRoot",
        "dataRoot",
        "deploymentProfile",
        "listen",
        "publicOrigin",
        "managedConfigPath",
        "configRevision",
        "releaseIdentity",
    }
)
_LISTEN_FIELDS = frozenset({"host", "port"})
_RELEASE_IDENTITY_FIELDS = frozenset(
    {"version", "channel", "commit", "manifestDigest", "apiDigest", "webDigest"}
)
_RELEASE_VERSION = re.compile(
    r"^v(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)(?:-(?P<channel>beta|rc)\.(?:[1-9][0-9]*))?$"
)
_SHA256_IDENTITY = re.compile(r"^sha256:[0-9a-f]{64}$")
_COMMIT_IDENTITY = re.compile(r"^[0-9a-f]{40}$")
_SECRET_FIELD_PARTS = (
    "authorization",
    "cookie",
    "credential",
    "key",
    "passphrase",
    "password",
    "secret",
    "setupcode",
    "token",
)
_MAX_IDENTITY_DEPTH = 12
_MAX_IDENTITY_MEMBERS = 256
_MAX_IDENTITY_STRING = 4096


class LocatorError(Exception):
    """A stable, secret-safe canonical locator failure."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)

    def __repr__(self) -> str:
        return f"LocatorError(code={self.code!r})"


class ReadOnlyHost(Protocol):
    def lstat(self, path: PurePosixPath) -> os.stat_result: ...

    def read_bytes(self, path: PurePosixPath, *, limit: int) -> bytes: ...

    def read_secure_bytes(
        self,
        path: PurePosixPath,
        *,
        limit: int,
        expected_owner_uid: int | None = None,
        expected_owner_gid: int | None = None,
        required_mode: int = 0o600,
    ) -> SecureFileSnapshot: ...


@dataclass(frozen=True)
class SecureFileSnapshot:
    payload: bytes
    metadata: os.stat_result


@dataclass(frozen=True)
class InstanceSnapshot:
    locator: InstanceLocator
    digest: str
    storage_digest: str


class LocalReadOnlyHost:
    """OS-backed inspection surface containing no mutation operations."""

    def lstat(self, path: PurePosixPath) -> os.stat_result:
        return Path(str(path)).lstat()

    def read_bytes(self, path: PurePosixPath, *, limit: int) -> bytes:
        with Path(str(path)).open("rb") as handle:
            payload = handle.read(limit + 1)
        if len(payload) > limit:
            raise LocatorError("LOCATOR_TOO_LARGE")
        return payload

    def _physical_path(self, path: PurePosixPath) -> Path:
        return Path(str(path))

    def read_secure_bytes(
        self,
        path: PurePosixPath,
        *,
        limit: int,
        expected_owner_uid: int | None = None,
        expected_owner_gid: int | None = None,
        required_mode: int = 0o600,
    ) -> SecureFileSnapshot:
        physical = self._physical_path(path)
        flags = os.O_RDONLY
        if hasattr(os, "O_BINARY"):
            flags |= os.O_BINARY
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        for attempt in range(20):
            descriptor = os.open(physical, flags)
            try:
                opened = os.fstat(descriptor)
                if not stat.S_ISREG(opened.st_mode):
                    raise LocatorError("LOCATOR_NOT_REGULAR")
                if opened.st_nlink != 1:
                    raise LocatorError("LOCATOR_LINK_COUNT_INVALID")
                if (
                    expected_owner_uid is not None
                    and opened.st_uid != expected_owner_uid
                ):
                    raise LocatorError("LOCATOR_OWNER_INVALID")
                if (
                    expected_owner_gid is not None
                    and opened.st_gid != expected_owner_gid
                ):
                    raise LocatorError("LOCATOR_GROUP_INVALID")
                if stat.S_IMODE(opened.st_mode) != required_mode and not getattr(
                    self, "emulates_private_permissions", False
                ):
                    raise LocatorError("LOCATOR_PERMISSIONS_INVALID")
                chunks = bytearray()
                while len(chunks) <= limit:
                    chunk = os.read(descriptor, min(64 * 1024, limit + 1 - len(chunks)))
                    if not chunk:
                        break
                    chunks.extend(chunk)
                if len(chunks) > limit:
                    raise LocatorError("LOCATOR_TOO_LARGE")
                after = os.fstat(descriptor)
                try:
                    current = physical.lstat()
                except FileNotFoundError:
                    current = None
                stable = (
                    current is not None
                    and after.st_nlink == 1
                    and (opened.st_dev, opened.st_ino, opened.st_size)
                    == (after.st_dev, after.st_ino, after.st_size)
                    and (after.st_dev, after.st_ino) == (current.st_dev, current.st_ino)
                )
                if stable:
                    return SecureFileSnapshot(bytes(chunks), after)
            finally:
                os.close(descriptor)
            if attempt == 19:
                raise LocatorError("LOCATOR_CHANGED_DURING_READ")
            time.sleep(0.005)
        raise LocatorError("LOCATOR_UNREADABLE")


class LocalLocatorStore(LocalReadOnlyHost):
    """Secure local adapter for the fixed locator file."""

    def __init__(self, physical_path: Path | None = None) -> None:
        self._locator_path = physical_path
        self.emulates_private_permissions = (
            physical_path is not None and os.name == "nt"
        )

    @classmethod
    def testing(cls, path: Path) -> LocalLocatorStore:
        path = Path(os.path.abspath(path))
        if not path.is_absolute():
            raise ValueError("Testing locator path must be absolute")
        return cls(path)

    def _physical_path(self, path: PurePosixPath) -> Path:
        if path != INSTANCE_LOCATOR_PATH:
            raise LocatorError("LOCATOR_PATH_NOT_CANONICAL")
        return self._locator_path or Path(str(path))

    @staticmethod
    def _validate_parent(path: Path) -> None:
        parent = path.parent
        if not parent.exists() or not parent.is_dir() or parent.is_symlink():
            raise LocatorError("LOCATOR_PARENT_INVALID")
        resolved = parent.resolve()
        if resolved != parent.absolute():
            raise LocatorError("LOCATOR_PARENT_INVALID")

    @staticmethod
    def _sync_directory(path: Path) -> None:
        if os.name == "nt":
            return
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        descriptor = os.open(path, flags)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _temporary(
        self,
        target: Path,
        payload: bytes,
        *,
        owner_uid: int | None,
        owner_gid: int | None,
    ) -> Path:
        self._validate_parent(target)
        descriptor, name = tempfile.mkstemp(
            prefix=f".{target.name}.{secrets.token_hex(8)}.",
            suffix=".tmp",
            dir=target.parent,
        )
        temporary = Path(name)
        try:
            if hasattr(os, "fchmod"):
                os.fchmod(descriptor, 0o600)
            else:
                os.chmod(temporary, 0o600)
            if os.name != "nt" and owner_uid is not None and owner_gid is not None:
                os.fchown(descriptor, owner_uid, owner_gid)
            view = memoryview(payload)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise OSError("short locator write")
                view = view[written:]
            os.fsync(descriptor)
        except Exception:
            os.close(descriptor)
            temporary.unlink(missing_ok=True)
            raise
        os.close(descriptor)
        return temporary

    @contextmanager
    def _publication_lock(self, target: Path):
        lock = target.parent / ".instance-publication.lock"
        self._validate_parent(target)
        flags = os.O_RDWR | os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(lock, flags, 0o600)
        handle = os.fdopen(descriptor, "r+b")
        locked = False
        try:
            opened = os.fstat(handle.fileno())
            if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
                raise LocatorError("LOCATOR_LOCK_INVALID")
            if os.name == "nt":
                import msvcrt

                if opened.st_size == 0:
                    handle.seek(0)
                    handle.write(b"0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                locked = True
            else:
                import fcntl

                os.fchmod(handle.fileno(), 0o600)
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                locked = True
            yield
        finally:
            if os.name == "nt" and locked:
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            elif os.name != "nt" and locked:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()

    def publish_initial_bytes(
        self,
        path: PurePosixPath,
        payload: bytes,
        *,
        owner_uid: int | None,
        owner_gid: int | None,
    ) -> None:
        target = self._physical_path(path)
        with self._publication_lock(target):
            temporary = self._temporary(
                target, payload, owner_uid=owner_uid, owner_gid=owner_gid
            )
            try:
                try:
                    os.link(temporary, target, follow_symlinks=False)
                except FileExistsError as error:
                    raise LocatorError("LOCATOR_ALREADY_EXISTS") from error
                temporary.unlink()
                self._sync_directory(target.parent)
            finally:
                temporary.unlink(missing_ok=True)

    def replace_bytes(
        self,
        path: PurePosixPath,
        payload: bytes,
        *,
        expected_storage_digest: str,
        owner_uid: int | None,
        owner_gid: int | None,
    ) -> None:
        target = self._physical_path(path)
        with self._publication_lock(target):
            current = self.read_secure_bytes(path, limit=MAX_LOCATOR_BYTES)
            actual = "sha256:" + hashlib.sha256(current.payload).hexdigest()
            if actual != expected_storage_digest:
                raise LocatorError("LOCATOR_CONCURRENT_MODIFICATION")
            temporary = self._temporary(
                target, payload, owner_uid=owner_uid, owner_gid=owner_gid
            )
            try:
                latest = self.read_secure_bytes(path, limit=MAX_LOCATOR_BYTES)
                latest_digest = "sha256:" + hashlib.sha256(latest.payload).hexdigest()
                if latest_digest != expected_storage_digest:
                    raise LocatorError("LOCATOR_CONCURRENT_MODIFICATION")
                os.replace(temporary, target)
                self._sync_directory(target.parent)
            finally:
                temporary.unlink(missing_ok=True)


@dataclass(frozen=True)
class ListenIdentity:
    host: str
    port: int

    @property
    def is_loopback(self) -> bool:
        return ipaddress.ip_address(self.host).is_loopback


@dataclass(frozen=True)
class InstanceLocator:
    schema_version: int
    instance_id: str
    app_root: PurePosixPath
    data_root: PurePosixPath
    deployment_profile: str
    listen: ListenIdentity
    public_origin: str
    managed_config_path: PurePosixPath
    config_revision: str
    release_identity: Mapping[str, Any]


def _field_is_secret(name: object) -> bool:
    normalized = "".join(
        character for character in str(name).lower() if character.isalnum()
    )
    return any(part in normalized for part in _SECRET_FIELD_PARTS)


def _string_is_secret(value: str) -> bool:
    lowered = value.lower()
    return (
        lowered.startswith("bearer ")
        or "-----begin private key-----" in lowered
        or bool(re.search(r"://[^/@\s:]+:[^/@\s]+@", value))
        or bool(re.match(r"^[A-Z][A-Z0-9_]+\s*=", value))
    )


def _non_secret_identity(
    value: object,
    *,
    depth: int = 0,
    counter: list[int] | None = None,
) -> object:
    if counter is None:
        counter = [0]
    counter[0] += 1
    if depth > _MAX_IDENTITY_DEPTH or counter[0] > _MAX_IDENTITY_MEMBERS:
        raise LocatorError("LOCATOR_RELEASE_IDENTITY_INVALID")
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, str):
        if len(value) > _MAX_IDENTITY_STRING:
            raise LocatorError("LOCATOR_RELEASE_IDENTITY_INVALID")
        if _string_is_secret(value):
            raise LocatorError("LOCATOR_SECRET_FIELD_FORBIDDEN")
        return value
    if isinstance(value, Mapping):
        if len(value) > _MAX_IDENTITY_MEMBERS:
            raise LocatorError("LOCATOR_RELEASE_IDENTITY_INVALID")
        normalized: dict[str, object] = {}
        keys = tuple(value)
        if any(not isinstance(key, str) for key in keys):
            raise LocatorError("LOCATOR_RELEASE_IDENTITY_INVALID")
        for key in sorted(keys):
            if not key or _field_is_secret(key):
                raise LocatorError("LOCATOR_SECRET_FIELD_FORBIDDEN")
            normalized[key] = _non_secret_identity(
                value[key], depth=depth + 1, counter=counter
            )
        return MappingProxyType(normalized)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        if len(value) > _MAX_IDENTITY_MEMBERS:
            raise LocatorError("LOCATOR_RELEASE_IDENTITY_INVALID")
        return tuple(
            _non_secret_identity(item, depth=depth + 1, counter=counter)
            for item in value
        )
    raise LocatorError("LOCATOR_RELEASE_IDENTITY_INVALID")


def _require_exact_fields(value: Mapping[str, Any], expected: frozenset[str]) -> None:
    fields = frozenset(value)
    if fields != expected:
        if any(_field_is_secret(field) for field in fields - expected):
            raise LocatorError("LOCATOR_SECRET_FIELD_FORBIDDEN")
        raise LocatorError("LOCATOR_SCHEMA_INVALID")


def _canonical_absolute_path(value: object, *, code: str) -> PurePosixPath:
    if not isinstance(value, str) or not value.startswith("/"):
        raise LocatorError(code)
    path = PurePosixPath(value)
    if str(path) != value or ".." in path.parts:
        raise LocatorError(code)
    return path


def _parse_public_origin(value: object) -> str:
    if not isinstance(value, str):
        raise LocatorError("LOCATOR_PUBLIC_ORIGIN_INVALID")
    parsed = urlsplit(value)
    try:
        port = parsed.port
    except ValueError as error:
        raise LocatorError("LOCATOR_PUBLIC_ORIGIN_INVALID") from error
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise LocatorError("LOCATOR_PUBLIC_ORIGIN_INVALID")
    try:
        normalized_host = ipaddress.ip_address(parsed.hostname).compressed
        rendered_host = (
            f"[{normalized_host}]" if ":" in normalized_host else normalized_host
        )
    except ValueError:
        try:
            normalized_host = parsed.hostname.encode("idna").decode("ascii").lower()
        except UnicodeError as error:
            raise LocatorError("LOCATOR_PUBLIC_ORIGIN_INVALID") from error
        if (
            normalized_host.endswith(".")
            or len(normalized_host) > 253
            or any(
                not label
                or len(label) > 63
                or re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", label) is None
                for label in normalized_host.split(".")
            )
        ):
            raise LocatorError("LOCATOR_PUBLIC_ORIGIN_INVALID")
        rendered_host = normalized_host
    default_port = 80 if parsed.scheme == "http" else 443
    normalized = f"{parsed.scheme}://{rendered_host}"
    if port is not None and port != default_port:
        normalized += f":{port}"
    if value != normalized:
        raise LocatorError("LOCATOR_PUBLIC_ORIGIN_INVALID")
    return normalized


def _parse_release_identity(value: object) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise LocatorError("LOCATOR_RELEASE_IDENTITY_INVALID")
    _non_secret_identity(value)
    _require_exact_fields(value, _RELEASE_IDENTITY_FIELDS)
    version = value["version"]
    channel = value["channel"]
    match = _RELEASE_VERSION.fullmatch(version) if isinstance(version, str) else None
    expected_channel = (
        match.group("channel") if match and match.group("channel") else "stable"
    )
    if match is None or channel != expected_channel:
        raise LocatorError("LOCATOR_RELEASE_IDENTITY_INVALID")
    if not isinstance(value["commit"], str) or not _COMMIT_IDENTITY.fullmatch(
        value["commit"]
    ):
        raise LocatorError("LOCATOR_RELEASE_IDENTITY_INVALID")
    if any(
        not isinstance(value[field], str)
        or not _SHA256_IDENTITY.fullmatch(value[field])
        for field in ("manifestDigest", "apiDigest", "webDigest")
    ):
        raise LocatorError("LOCATOR_RELEASE_IDENTITY_INVALID")
    return MappingProxyType({field: value[field] for field in sorted(value)})


def parse_instance_locator(payload: Mapping[str, Any]) -> InstanceLocator:
    """Parse the only supported v1.1 canonical locator schema."""

    _require_exact_fields(payload, _REQUIRED_FIELDS)
    if (
        type(payload["schemaVersion"]) is not int
        or payload["schemaVersion"] != LOCATOR_SCHEMA_VERSION
    ):
        raise LocatorError("LOCATOR_SCHEMA_UNSUPPORTED")
    if payload["deploymentProfile"] != STANDARD_DEPLOYMENT_PROFILE:
        raise LocatorError("LOCATOR_PROFILE_UNSUPPORTED")

    app_root = _canonical_absolute_path(
        payload["appRoot"], code="LOCATOR_APP_ROOT_INVALID"
    )
    data_root = _canonical_absolute_path(
        payload["dataRoot"], code="LOCATOR_DATA_ROOT_INVALID"
    )
    if app_root != APP_ROOT or data_root != DATA_ROOT:
        raise LocatorError("LOCATOR_CANONICAL_ROOT_MISMATCH")

    managed_config_path = _canonical_absolute_path(
        payload["managedConfigPath"], code="LOCATOR_CONFIG_PATH_INVALID"
    )
    if managed_config_path != MANAGED_CONFIG_PATH:
        raise LocatorError("LOCATOR_CONFIG_PATH_INVALID")

    try:
        instance_id = str(UUID(str(payload["instanceId"])))
    except (ValueError, TypeError, AttributeError) as error:
        raise LocatorError("LOCATOR_INSTANCE_ID_INVALID") from error

    listen = payload["listen"]
    if not isinstance(listen, Mapping):
        raise LocatorError("LOCATOR_LISTEN_INVALID")
    _require_exact_fields(listen, _LISTEN_FIELDS)
    host = listen["host"]
    port = listen["port"]
    try:
        canonical_host = (
            ipaddress.ip_address(host).compressed if isinstance(host, str) else None
        )
    except ValueError:
        canonical_host = None
    if (
        not isinstance(host, str)
        or canonical_host != host
        or isinstance(port, bool)
        or not isinstance(port, int)
        or not 1 <= port <= 65535
    ):
        raise LocatorError("LOCATOR_LISTEN_INVALID")

    if payload["instanceId"] != instance_id:
        raise LocatorError("LOCATOR_INSTANCE_ID_INVALID")

    try:
        config_revision = str(UUID(str(payload["configRevision"])))
    except (ValueError, TypeError, AttributeError) as error:
        raise LocatorError("LOCATOR_CONFIG_REVISION_INVALID") from error
    if payload["configRevision"] != config_revision:
        raise LocatorError("LOCATOR_CONFIG_REVISION_INVALID")

    normalized_release_identity = _parse_release_identity(payload["releaseIdentity"])

    return InstanceLocator(
        schema_version=LOCATOR_SCHEMA_VERSION,
        instance_id=instance_id,
        app_root=app_root,
        data_root=data_root,
        deployment_profile=STANDARD_DEPLOYMENT_PROFILE,
        listen=ListenIdentity(host=host, port=port),
        public_origin=_parse_public_origin(payload["publicOrigin"]),
        managed_config_path=managed_config_path,
        config_revision=config_revision,
        release_identity=normalized_release_identity,
    )


def instance_locator_payload(locator: InstanceLocator) -> dict[str, object]:
    payload = {
        "schemaVersion": locator.schema_version,
        "instanceId": locator.instance_id,
        "appRoot": str(locator.app_root),
        "dataRoot": str(locator.data_root),
        "deploymentProfile": locator.deployment_profile,
        "listen": {"host": locator.listen.host, "port": locator.listen.port},
        "publicOrigin": locator.public_origin,
        "managedConfigPath": str(locator.managed_config_path),
        "configRevision": locator.config_revision,
        "releaseIdentity": dict(locator.release_identity),
    }
    parsed = parse_instance_locator(payload)
    if parsed != locator:
        raise LocatorError("LOCATOR_SCHEMA_INVALID")
    return payload


def _locator_bytes(locator: InstanceLocator) -> bytes:
    return (
        json.dumps(
            instance_locator_payload(locator),
            ensure_ascii=False,
            allow_nan=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def _sha256_identity(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def release_identity_from_manifest(manifest: Mapping[str, Any]) -> Mapping[str, str]:
    """Project one already validated Release Manifest into locator identity."""

    try:
        from release.contract import validate_manifest

        validate_manifest(dict(manifest))
    except Exception as error:
        raise LocatorError("LOCATOR_RELEASE_IDENTITY_INVALID") from error
    try:
        release = manifest["release"]
        images = manifest["images"]
        identity = {
            "version": release["version"],
            "channel": release["channel"],
            "commit": release["commit"],
            "manifestDigest": _sha256_identity(
                json.dumps(
                    manifest,
                    ensure_ascii=False,
                    allow_nan=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode("utf-8")
            ),
            "apiDigest": images["api"]["digest"],
            "webDigest": images["web"]["digest"],
        }
    except (KeyError, TypeError, ValueError) as error:
        raise LocatorError("LOCATOR_RELEASE_IDENTITY_INVALID") from error
    return _parse_release_identity(identity)


def load_instance_snapshot(
    host: ReadOnlyHost | None = None,
    *,
    path: PurePosixPath = INSTANCE_LOCATOR_PATH,
    expected_owner_uid: int | None = None,
    expected_owner_gid: int | None = None,
) -> InstanceSnapshot:
    """Read the fixed locator without env, cwd, Compose, or legacy fallback."""

    if path != INSTANCE_LOCATOR_PATH:
        raise LocatorError("LOCATOR_PATH_NOT_CANONICAL")
    reader = host or LocalReadOnlyHost()
    try:
        secure = reader.read_secure_bytes(
            path,
            limit=MAX_LOCATOR_BYTES,
            expected_owner_uid=expected_owner_uid,
            expected_owner_gid=expected_owner_gid,
            required_mode=0o600,
        )
    except FileNotFoundError as error:
        raise LocatorError("LOCATOR_MISSING") from error
    except LocatorError:
        raise
    except OSError as error:
        raise LocatorError("LOCATOR_UNREADABLE") from error
    metadata = secure.metadata
    if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise LocatorError("LOCATOR_NOT_REGULAR")
    if metadata.st_nlink != 1:
        raise LocatorError("LOCATOR_LINK_COUNT_INVALID")
    if expected_owner_uid is not None and metadata.st_uid != expected_owner_uid:
        raise LocatorError("LOCATOR_OWNER_INVALID")
    if expected_owner_gid is not None and metadata.st_gid != expected_owner_gid:
        raise LocatorError("LOCATOR_GROUP_INVALID")
    if stat.S_IMODE(metadata.st_mode) != 0o600 and not getattr(
        reader, "emulates_private_permissions", False
    ):
        raise LocatorError("LOCATOR_PERMISSIONS_INVALID")
    try:
        parsed = _strict_json_loads(secure.payload)
    except LocatorError:
        raise
    except (OSError, UnicodeError, ValueError) as error:
        raise LocatorError("LOCATOR_CONTENT_INVALID") from error
    if not isinstance(parsed, Mapping):
        raise LocatorError("LOCATOR_SCHEMA_INVALID")
    locator = parse_instance_locator(parsed)
    return InstanceSnapshot(
        locator=locator,
        digest=_sha256_identity(_locator_bytes(locator)),
        storage_digest=_sha256_identity(secure.payload),
    )


def load_instance_locator(
    host: ReadOnlyHost | None = None,
    *,
    path: PurePosixPath = INSTANCE_LOCATOR_PATH,
    expected_owner_uid: int | None = None,
    expected_owner_gid: int | None = None,
) -> InstanceLocator:
    return load_instance_snapshot(
        host,
        path=path,
        expected_owner_uid=expected_owner_uid,
        expected_owner_gid=expected_owner_gid,
    ).locator


def publish_instance_locator(
    locator: InstanceLocator,
    *,
    store: LocalLocatorStore | None = None,
    owner_uid: int | None = None,
    owner_gid: int | None = None,
) -> InstanceSnapshot:
    if store is None and (owner_uid is None or owner_gid is None):
        raise LocatorError("LOCATOR_OWNER_REQUIRED")
    writer = store or LocalLocatorStore()
    payload = _locator_bytes(locator)
    writer.publish_initial_bytes(
        INSTANCE_LOCATOR_PATH,
        payload,
        owner_uid=owner_uid,
        owner_gid=owner_gid,
    )
    snapshot = load_instance_snapshot(
        writer,
        expected_owner_uid=owner_uid,
        expected_owner_gid=owner_gid,
    )
    if snapshot.locator != locator:
        raise LocatorError("LOCATOR_PUBLICATION_MISMATCH")
    return snapshot


def replace_instance_locator(
    locator: InstanceLocator,
    *,
    expected_digest: str,
    store: LocalLocatorStore | None = None,
    owner_uid: int | None = None,
    owner_gid: int | None = None,
) -> InstanceSnapshot:
    if store is None and (owner_uid is None or owner_gid is None):
        raise LocatorError("LOCATOR_OWNER_REQUIRED")
    writer = store or LocalLocatorStore()
    current = load_instance_snapshot(
        writer,
        expected_owner_uid=owner_uid,
        expected_owner_gid=owner_gid,
    )
    if current.digest != expected_digest:
        raise LocatorError("LOCATOR_CONCURRENT_MODIFICATION")
    writer.replace_bytes(
        INSTANCE_LOCATOR_PATH,
        _locator_bytes(locator),
        expected_storage_digest=current.storage_digest,
        owner_uid=owner_uid,
        owner_gid=owner_gid,
    )
    snapshot = load_instance_snapshot(
        writer,
        expected_owner_uid=owner_uid,
        expected_owner_gid=owner_gid,
    )
    if snapshot.locator != locator:
        raise LocatorError("LOCATOR_PUBLICATION_MISMATCH")
    return snapshot


def _strict_json_loads(raw: bytes) -> object:
    def reject_constant(_: str) -> None:
        raise ValueError

    def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError
            result[key] = value
        return result

    return json.loads(
        raw.decode("utf-8"),
        object_pairs_hook=reject_duplicates,
        parse_constant=reject_constant,
    )
