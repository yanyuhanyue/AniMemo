from __future__ import annotations

import json
import os
import re
import stat
from collections.abc import Mapping, Sequence
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
        "releaseIdentity",
    }
)
_LISTEN_FIELDS = frozenset({"host", "port"})
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


@dataclass(frozen=True)
class ListenIdentity:
    host: str
    port: int

    @property
    def is_loopback(self) -> bool:
        return self.host in {"127.0.0.1", "::1"}


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
    release_identity: Mapping[str, Any]


def _field_is_secret(name: object) -> bool:
    normalized = "".join(character for character in str(name).lower() if character.isalnum())
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
    return f"{parsed.scheme}://{parsed.netloc}"


def parse_instance_locator(payload: Mapping[str, Any]) -> InstanceLocator:
    """Parse the only supported v1.1 canonical locator schema."""

    _require_exact_fields(payload, _REQUIRED_FIELDS)
    if payload["schemaVersion"] != LOCATOR_SCHEMA_VERSION:
        raise LocatorError("LOCATOR_SCHEMA_UNSUPPORTED")
    if payload["deploymentProfile"] != STANDARD_DEPLOYMENT_PROFILE:
        raise LocatorError("LOCATOR_PROFILE_UNSUPPORTED")

    app_root = _canonical_absolute_path(payload["appRoot"], code="LOCATOR_APP_ROOT_INVALID")
    data_root = _canonical_absolute_path(payload["dataRoot"], code="LOCATOR_DATA_ROOT_INVALID")
    if app_root != APP_ROOT or data_root != DATA_ROOT:
        raise LocatorError("LOCATOR_CANONICAL_ROOT_MISMATCH")

    managed_config_path = _canonical_absolute_path(
        payload["managedConfigPath"], code="LOCATOR_CONFIG_PATH_INVALID"
    )
    if managed_config_path == MANAGED_CONFIG_ROOT or MANAGED_CONFIG_ROOT not in managed_config_path.parents:
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
    if not isinstance(host, str) or not host or isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise LocatorError("LOCATOR_LISTEN_INVALID")

    release_identity = payload["releaseIdentity"]
    if not isinstance(release_identity, Mapping) or not release_identity:
        raise LocatorError("LOCATOR_RELEASE_IDENTITY_INVALID")
    normalized_release_identity = _non_secret_identity(release_identity)
    if not isinstance(normalized_release_identity, Mapping):
        raise LocatorError("LOCATOR_RELEASE_IDENTITY_INVALID")

    return InstanceLocator(
        schema_version=LOCATOR_SCHEMA_VERSION,
        instance_id=instance_id,
        app_root=app_root,
        data_root=data_root,
        deployment_profile=STANDARD_DEPLOYMENT_PROFILE,
        listen=ListenIdentity(host=host, port=port),
        public_origin=_parse_public_origin(payload["publicOrigin"]),
        managed_config_path=managed_config_path,
        release_identity=normalized_release_identity,
    )


def load_instance_locator(
    host: ReadOnlyHost | None = None,
    *,
    path: PurePosixPath = INSTANCE_LOCATOR_PATH,
    expected_owner_uid: int | None = None,
) -> InstanceLocator:
    """Read the fixed locator without env, cwd, Compose, or legacy fallback."""

    if path != INSTANCE_LOCATOR_PATH:
        raise LocatorError("LOCATOR_PATH_NOT_CANONICAL")
    reader = host or LocalReadOnlyHost()
    try:
        metadata = reader.lstat(path)
    except FileNotFoundError as error:
        raise LocatorError("LOCATOR_MISSING") from error
    except OSError as error:
        raise LocatorError("LOCATOR_UNREADABLE") from error
    if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise LocatorError("LOCATOR_NOT_REGULAR")
    if getattr(metadata, "st_nlink", 1) != 1:
        raise LocatorError("LOCATOR_LINK_COUNT_INVALID")
    if expected_owner_uid is not None and metadata.st_uid != expected_owner_uid:
        raise LocatorError("LOCATOR_OWNER_INVALID")
    if metadata.st_mode & 0o077:
        raise LocatorError("LOCATOR_PERMISSIONS_INVALID")
    try:
        raw = reader.read_bytes(path, limit=MAX_LOCATOR_BYTES)
        parsed = _strict_json_loads(raw)
    except LocatorError:
        raise
    except (OSError, UnicodeError, ValueError) as error:
        raise LocatorError("LOCATOR_CONTENT_INVALID") from error
    if not isinstance(parsed, Mapping):
        raise LocatorError("LOCATOR_SCHEMA_INVALID")
    return parse_instance_locator(parsed)


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
