from __future__ import annotations

import hashlib
import importlib
import json
import os
import re
import stat
import subprocess
import sys
import tarfile
import tempfile
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import MappingProxyType

BOOTSTRAP_AUTHORITY_ROOT = Path("/var/lib/animemo/bootstrap-authority/v1")
_AUTHORIZATION_FILE = "bootstrap-authorization.json"
_MATERIALS_FILE = "installer-materials.tar"
_REPOSITORY = "yanyuhanyue/AniMemo"
_STAGE0_MODEL = "GITHUB_IMMUTABLE_RELEASE_SIGSTORE_TUF_SINGLE_AUTHORITY"
_ONLINE_CARRIER = "GH_2_97_0_EXACT_FROM_OFFICIAL_RELEASE_ASSETS_SHA256_BOUND"
_LEGACY_ONLINE_CARRIER = "GH_2_97_0_EXACT_FROM_OFFICIAL_SIGNED_APT"
_OFFLINE_CARRIER = "OPERATOR_PRETRUSTED_PINNED_LINUX_VERIFIER_AND_TWO_TUF_ROOTS"
_WRITABLE_CARRIERS = frozenset({_ONLINE_CARRIER, _OFFLINE_CARRIER})
_READABLE_CARRIERS = _WRITABLE_CARRIERS | {_LEGACY_ONLINE_CARRIER}
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_COMMIT = re.compile(r"[0-9a-f]{40}\Z")
_TAG = re.compile(r"v[0-9]+\.[0-9]+\.[0-9]+(?:-rc\.[0-9]+)?\Z")
_UTC = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z\Z")
_CAPABILITY_TOKEN = object()
_GH_EXECUTABLE = "/usr/bin/gh"
_GH_VERSION = "2.97.0"
_GH_VERSION_OUTPUT_LIMIT = 4096
_GH_VERSION_LINE = re.compile(
    r"gh version (?P<version>[0-9]+\.[0-9]+\.[0-9]+)(?: (?P<metadata>.*))?\Z"
)
_PROTECTED_RUNTIME_DIRECTORY = "materials"
_REQUIRED_RUNTIME_MODULES = frozenset(
    {
        "installer.bootstrap",
        "installer.cli",
        "installer.production",
        "installer.runtime",
        "updater.offline",
        "updater.trust_lifecycle",
    }
)


class BootstrapAuthorityError(RuntimeError):
    def __init__(self, code: str, *, reason: str | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.reason = reason


class GhVersionOutputError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class GhCliVersionOutput:
    executable_name: str
    semantic_version: str
    first_line: str
    metadata_present: bool


def parse_gh_cli_version_output(stdout: bytes) -> GhCliVersionOutput:
    if type(stdout) is not bytes or not stdout:
        raise GhVersionOutputError("GH_VERSION_OUTPUT_MALFORMED")
    if len(stdout) > _GH_VERSION_OUTPUT_LIMIT:
        raise GhVersionOutputError("GH_VERSION_OUTPUT_TOO_LARGE")
    try:
        output = stdout.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise GhVersionOutputError("GH_VERSION_OUTPUT_INVALID_UTF8") from error
    if any(
        unicodedata.category(character) == "Cc"
        and character not in {"\r", "\n", "\t"}
        for character in output
    ):
        raise GhVersionOutputError("GH_VERSION_OUTPUT_CONTROL_CHARACTER")
    if "\r" in output.replace("\r\n", ""):
        raise GhVersionOutputError("GH_VERSION_OUTPUT_MALFORMED")
    first_line = output.split("\n", 1)[0].removesuffix("\r")
    match = _GH_VERSION_LINE.fullmatch(first_line)
    if match is None:
        raise GhVersionOutputError("GH_VERSION_OUTPUT_MALFORMED")
    version = match.group("version")
    if version != _GH_VERSION:
        raise GhVersionOutputError("GH_VERSION_MISMATCH")
    return GhCliVersionOutput(
        executable_name="gh",
        semantic_version=version,
        first_line=first_line,
        metadata_present=bool(match.group("metadata")),
    )


def _reject(code: str, *, reason: str | None = None) -> None:
    raise BootstrapAuthorityError(code, reason=reason)


def _reject_gh_version(reason: str) -> None:
    _reject("BOOTSTRAP_STAGE0_GH_VERSION_INVALID", reason=reason)


def _canonical_json_bytes(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def _sha256_identity(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _closed_mapping(
    value: object,
    *,
    keys: frozenset[str],
    code: str,
) -> Mapping[str, object]:
    if type(value) is not dict or frozenset(value) != keys:
        _reject(code)
    return value


def _safe_root() -> Path:
    root = BOOTSTRAP_AUTHORITY_ROOT
    if not root.is_absolute():
        _reject("BOOTSTRAP_AUTHORITY_ROOT_INVALID")
    return root


def _validate_mode_owner(path: Path, metadata: os.stat_result, *, directory: bool) -> None:
    if directory and not stat.S_ISDIR(metadata.st_mode):
        _reject("BOOTSTRAP_AUTHORITY_ROOT_INVALID")
    if not directory and not stat.S_ISREG(metadata.st_mode):
        _reject("BOOTSTRAP_PROTECTED_MATERIAL_INVALID")
    if os.name == "posix" and metadata.st_mode & 0o022:
        _reject("BOOTSTRAP_PATH_WRITABLE_BY_UNTRUSTED_PRINCIPAL")
    if os.name == "posix" and metadata.st_uid != 0:
        _reject("BOOTSTRAP_PATH_NOT_ROOT_OWNED")
    if not directory and metadata.st_nlink != 1:
        _reject("BOOTSTRAP_PROTECTED_MATERIAL_LINK_INVALID")


def _ensure_authority_root() -> Path:
    root = _safe_root()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(root, 0o700)
    except OSError:
        _reject("BOOTSTRAP_AUTHORITY_ROOT_INVALID")
    try:
        metadata = root.lstat()
    except OSError:
        _reject("BOOTSTRAP_AUTHORITY_ROOT_INVALID")
    if root.is_symlink():
        _reject("BOOTSTRAP_AUTHORITY_ROOT_INVALID")
    _validate_mode_owner(root, metadata, directory=True)
    return root


def _protected_material_bytes(path: Path) -> bytes:
    root = _safe_root()
    expected = root / _MATERIALS_FILE
    try:
        if path != expected or path.is_symlink():
            _reject("BOOTSTRAP_PROTECTED_MATERIAL_PATH_INVALID")
        before = path.lstat()
        _validate_mode_owner(path, before, directory=False)
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(path, flags)
    except BootstrapAuthorityError:
        raise
    except OSError:
        _reject("BOOTSTRAP_PROTECTED_MATERIAL_INVALID")
    try:
        opened = os.fstat(descriptor)
        _validate_mode_owner(path, opened, directory=False)
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            _reject("BOOTSTRAP_PROTECTED_MATERIAL_RACE")
        chunks: list[bytes] = []
        size = 0
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > 2 * 1024 * 1024 * 1024:
                _reject("BOOTSTRAP_PROTECTED_MATERIAL_TOO_LARGE")
            chunks.append(chunk)
        after = os.fstat(descriptor)
        if (
            (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            != (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)
            or size != opened.st_size
        ):
            _reject("BOOTSTRAP_PROTECTED_MATERIAL_RACE")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _regular_file_identity(path: Path) -> tuple[int, str]:
    try:
        before = path.lstat()
        if (
            path.is_symlink()
            or not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or (os.name == "posix" and before.st_uid != 0)
            or (os.name == "posix" and before.st_mode & 0o022)
        ):
            _reject("BOOTSTRAP_RUNTIME_SOURCE_INVALID")
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(path, flags)
    except BootstrapAuthorityError:
        raise
    except OSError:
        _reject("BOOTSTRAP_RUNTIME_SOURCE_INVALID")
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
        ):
            _reject("BOOTSTRAP_RUNTIME_SOURCE_RACE")
        digest = hashlib.sha256()
        size = 0
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > 64 * 1024 * 1024:
                _reject("BOOTSTRAP_RUNTIME_SOURCE_INVALID")
            digest.update(chunk)
        after = os.fstat(descriptor)
        if (
            size != opened.st_size
            or (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            != (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)
        ):
            _reject("BOOTSTRAP_RUNTIME_SOURCE_RACE")
        return size, "sha256:" + digest.hexdigest()
    finally:
        os.close(descriptor)


def _archive_python_identities(archive_path: Path) -> dict[str, tuple[int, str]]:
    identities: dict[str, tuple[int, str]] = {}
    try:
        with tarfile.open(archive_path, mode="r:") as archive:
            for member in archive:
                pure = PurePosixPath(member.name)
                if (
                    pure.suffix != ".py"
                    or pure.parts[0] not in {"durability", "installer", "release", "updater"}
                ):
                    continue
                if (
                    pure.is_absolute()
                    or any(part in {"", ".", ".."} for part in pure.parts)
                    or not member.isfile()
                    or member.size < 1
                    or member.size > 64 * 1024 * 1024
                    or member.name in identities
                ):
                    _reject("BOOTSTRAP_RUNTIME_ARCHIVE_INVALID")
                source = archive.extractfile(member)
                if source is None:
                    _reject("BOOTSTRAP_RUNTIME_ARCHIVE_INVALID")
                digest = hashlib.sha256()
                size = 0
                while True:
                    chunk = source.read(1024 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > member.size:
                        _reject("BOOTSTRAP_RUNTIME_ARCHIVE_INVALID")
                    digest.update(chunk)
                if size != member.size:
                    _reject("BOOTSTRAP_RUNTIME_ARCHIVE_INVALID")
                identities[member.name] = (size, "sha256:" + digest.hexdigest())
    except BootstrapAuthorityError:
        raise
    except (OSError, tarfile.TarError):
        _reject("BOOTSTRAP_RUNTIME_ARCHIVE_INVALID")
    return identities


def _validate_protected_runtime_sources(
    authorization: AuthorizedBootstrap,
    *,
    module_files: Mapping[str, Path] | None = None,
) -> None:
    runtime_root = _safe_root() / _PROTECTED_RUNTIME_DIRECTORY
    try:
        root_metadata = runtime_root.lstat()
    except OSError:
        _reject("BOOTSTRAP_RUNTIME_ROOT_INVALID")
    if (
        runtime_root.is_symlink()
        or not stat.S_ISDIR(root_metadata.st_mode)
        or runtime_root.resolve(strict=True) != runtime_root
        or (os.name == "posix" and root_metadata.st_uid != 0)
        or (os.name == "posix" and root_metadata.st_mode & 0o022)
    ):
        _reject("BOOTSTRAP_RUNTIME_ROOT_INVALID")

    if module_files is None:
        observed: dict[str, Path] = {}
        for name in _REQUIRED_RUNTIME_MODULES:
            module = sys.modules.get(name)
            source = getattr(module, "__file__", None)
            if type(source) is not str:
                _reject("BOOTSTRAP_RUNTIME_MODULE_MISSING")
            observed[name] = Path(source)
        module_files = observed
    if frozenset(module_files) != _REQUIRED_RUNTIME_MODULES:
        _reject("BOOTSTRAP_RUNTIME_MODULE_SET_INVALID")

    archive_identities = _archive_python_identities(
        authorization.materials_path
    )
    for name, candidate in module_files.items():
        expected_relative = name.replace(".", "/") + ".py"
        if expected_relative not in archive_identities:
            _reject("BOOTSTRAP_RUNTIME_MODULE_NOT_DECLARED")
        try:
            resolved = Path(candidate).resolve(strict=True)
            relative = resolved.relative_to(runtime_root).as_posix()
        except (OSError, ValueError):
            _reject("BOOTSTRAP_RUNTIME_SOURCE_OUTSIDE_PROTECTED_ROOT")
        if relative != expected_relative or resolved.suffix != ".py":
            _reject("BOOTSTRAP_RUNTIME_SOURCE_OUTSIDE_PROTECTED_ROOT")
        if _regular_file_identity(resolved) != archive_identities[expected_relative]:
            _reject("BOOTSTRAP_RUNTIME_SOURCE_IDENTITY_MISMATCH")


@dataclass(frozen=True)
class BootstrapAuthorization:
    payload: Mapping[str, object]
    identity: str


class AuthorizedBootstrap:
    __slots__ = (
        "authorization_identity",
        "materials_path",
        "materials_sha256",
        "release_commit",
        "version",
    )

    def __init__(
        self,
        token: object,
        *,
        authorization_identity: str,
        materials_path: Path,
        materials_sha256: str,
        release_commit: str,
        version: str,
    ) -> None:
        if token is not _CAPABILITY_TOKEN:
            _reject("BOOTSTRAP_CAPABILITY_FORGERY")
        self.authorization_identity = authorization_identity
        self.materials_path = materials_path
        self.materials_sha256 = materials_sha256
        self.release_commit = release_commit
        self.version = version


def _close_bootstrap_authorization(
    value: object,
    *,
    accepted_carriers: frozenset[str],
) -> BootstrapAuthorization:
    payload = _closed_mapping(
        value,
        keys=frozenset(
            {
                "schemaVersion",
                "state",
                "repository",
                "tag",
                "releaseCommit",
                "releaseAttestationIdentity",
                "installerMaterials",
                "stage0",
                "verifiedAt",
            }
        ),
        code="BOOTSTRAP_AUTHORIZATION_INVALID",
    )
    if (
        payload["schemaVersion"] != 1
        or payload["state"] != "PRIVILEGE_ALLOWED"
        or payload["repository"] != _REPOSITORY
        or type(payload["tag"]) is not str
        or not _TAG.fullmatch(payload["tag"])
        or type(payload["releaseCommit"]) is not str
        or not _COMMIT.fullmatch(payload["releaseCommit"])
        or type(payload["releaseAttestationIdentity"]) is not str
        or not _DIGEST.fullmatch(payload["releaseAttestationIdentity"])
        or type(payload["verifiedAt"]) is not str
        or not _UTC.fullmatch(payload["verifiedAt"])
    ):
        _reject("BOOTSTRAP_AUTHORIZATION_INVALID")

    materials = _closed_mapping(
        payload["installerMaterials"],
        keys=frozenset({"path", "sha256", "size"}),
        code="BOOTSTRAP_MATERIAL_BINDING_INVALID",
    )
    expected_path = str(_safe_root() / _MATERIALS_FILE)
    if (
        materials["path"] != expected_path
        or type(materials["sha256"]) is not str
        or not _DIGEST.fullmatch(materials["sha256"])
        or type(materials["size"]) is not int
        or isinstance(materials["size"], bool)
        or not 1 <= materials["size"] <= 2 * 1024 * 1024 * 1024
    ):
        _reject("BOOTSTRAP_MATERIAL_BINDING_INVALID")

    stage0 = _closed_mapping(
        payload["stage0"],
        keys=frozenset({"model", "carrier", "verifierIdentity"}),
        code="BOOTSTRAP_STAGE0_INVALID",
    )
    if (
        stage0["model"] != _STAGE0_MODEL
        or stage0["carrier"] not in accepted_carriers
        or type(stage0["verifierIdentity"]) is not str
        or not stage0["verifierIdentity"]
        or "TEST" in stage0["verifierIdentity"].upper()
    ):
        _reject("BOOTSTRAP_STAGE0_INVALID")

    canonical = json.loads(_canonical_json_bytes(payload))
    immutable = MappingProxyType(canonical)
    return BootstrapAuthorization(
        payload=immutable,
        identity=_sha256_identity(_canonical_json_bytes(canonical)),
    )


def close_bootstrap_authorization(value: object) -> BootstrapAuthorization:
    return _close_bootstrap_authorization(
        value,
        accepted_carriers=_WRITABLE_CARRIERS,
    )


def _record(authorization: BootstrapAuthorization) -> bytes:
    body = dict(authorization.payload)
    body["authorizationIdentity"] = authorization.identity
    return _canonical_json_bytes(body)


def _load_record(path: Path) -> BootstrapAuthorization:
    try:
        raw = path.read_bytes()
        value = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        _reject("BOOTSTRAP_AUTHORIZATION_INVALID")
    if type(value) is not dict or set(value) != {
        "authorizationIdentity",
        "schemaVersion",
        "state",
        "repository",
        "tag",
        "releaseCommit",
        "releaseAttestationIdentity",
        "installerMaterials",
        "stage0",
        "verifiedAt",
    }:
        _reject("BOOTSTRAP_AUTHORIZATION_INVALID")
    recorded_identity = value.pop("authorizationIdentity")
    authorization = _close_bootstrap_authorization(
        value,
        accepted_carriers=_READABLE_CARRIERS,
    )
    if recorded_identity != authorization.identity or raw != _record(authorization):
        _reject("BOOTSTRAP_AUTHORIZATION_IDENTITY_MISMATCH")
    return authorization


def commit_bootstrap_authorization(value: object) -> BootstrapAuthorization:
    root = _ensure_authority_root()
    authorization = close_bootstrap_authorization(value)
    materials = authorization.payload["installerMaterials"]
    assert isinstance(materials, dict)
    material_bytes = _protected_material_bytes(Path(materials["path"]))
    if (
        len(material_bytes) != materials["size"]
        or _sha256_identity(material_bytes) != materials["sha256"]
    ):
        _reject("BOOTSTRAP_PROTECTED_MATERIAL_MISMATCH")

    final = root / _AUTHORIZATION_FILE
    if final.exists():
        existing = _load_record(final)
        if existing.identity != authorization.identity:
            _reject("BOOTSTRAP_AUTHORIZATION_CONFLICT")
        return existing

    data = _record(authorization)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".bootstrap-", dir=root)
    temporary = Path(temporary_name)
    try:
        os.chmod(temporary, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, final)
        except FileExistsError:
            existing = _load_record(final)
            if existing.identity != authorization.identity:
                _reject("BOOTSTRAP_AUTHORIZATION_CONFLICT")
            return existing
        if os.name == "posix":
            directory = os.open(root, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return authorization


class BootstrapPrivilegeGate:
    def consume(self, *, version: str, release_commit: str) -> AuthorizedBootstrap:
        root = _ensure_authority_root()
        authorization = _load_record(root / _AUTHORIZATION_FILE)
        payload = authorization.payload
        if payload["tag"] != version or payload["releaseCommit"] != release_commit:
            _reject("BOOTSTRAP_RELEASE_BINDING_MISMATCH")
        materials = payload["installerMaterials"]
        assert isinstance(materials, dict)
        material_bytes = _protected_material_bytes(Path(materials["path"]))
        if (
            len(material_bytes) != materials["size"]
            or _sha256_identity(material_bytes) != materials["sha256"]
        ):
            _reject("BOOTSTRAP_PROTECTED_MATERIAL_MISMATCH")
        return AuthorizedBootstrap(
            _CAPABILITY_TOKEN,
            authorization_identity=authorization.identity,
            materials_path=Path(materials["path"]),
            materials_sha256=materials["sha256"],
            release_commit=release_commit,
            version=version,
        )


class ProductionBootstrapPrivilegeGate:
    """生产组合根：先消费 Stage-0 capability，再原子预置离线信任。"""

    def consume(self, *, version: str, release_commit: str) -> AuthorizedBootstrap:
        authorization = BootstrapPrivilegeGate().consume(
            version=version,
            release_commit=release_commit,
        )
        try:
            for module_name in sorted(_REQUIRED_RUNTIME_MODULES):
                importlib.import_module(module_name)
        except Exception:  # noqa: BLE001 - privileged boundary stays redacted
            _reject("BOOTSTRAP_RUNTIME_MODULE_IMPORT_FAILED")
        _validate_protected_runtime_sources(authorization)
        try:
            from updater.trust_lifecycle import (
                ProductionTrustLifecycle,
                TrustCommitReceipt,
            )

            receipt = ProductionTrustLifecycle.production().provision_initial(
                authorization
            )
        except BootstrapAuthorityError:
            raise
        except Exception:  # noqa: BLE001 - privileged boundary stays redacted
            _reject("BOOTSTRAP_TRUST_PROVISIONING_FAILED")
        if (
            type(receipt) is not TrustCommitReceipt
            or receipt.authorization_identity != authorization.authorization_identity
        ):
            _reject("BOOTSTRAP_TRUST_PROVISIONING_RECEIPT_INVALID")
        return authorization


def _run_stage0_gh(arguments: tuple[str, ...]) -> object:
    environment = {
        "GH_PROMPT_DISABLED": "1",
        "HOME": "/root",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
    }
    token = os.environ.get("GH_TOKEN")
    if token is not None:
        if (
            not token
            or len(token) > 4096
            or any(ord(character) < 0x21 or ord(character) > 0x7E for character in token)
        ):
            _reject("BOOTSTRAP_STAGE0_GH_CREDENTIAL_INVALID")
        environment["GH_TOKEN"] = token
    try:
        result = subprocess.run(
            [_GH_EXECUTABLE, *arguments],
            cwd="/",
            env=environment,
            text=False,
            shell=False,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=120,
            check=False,
        )
    except subprocess.TimeoutExpired:
        if arguments == ("version",):
            _reject_gh_version("GH_VERSION_PROCESS_TIMEOUT")
        _reject("BOOTSTRAP_STAGE0_GH_EXECUTION_FAILED")
    except (OSError, subprocess.SubprocessError):
        if arguments == ("version",):
            _reject_gh_version("GH_VERSION_PROCESS_FAILED")
        _reject("BOOTSTRAP_STAGE0_GH_EXECUTION_FAILED")
    if arguments == ("version",):
        if result.returncode != 0:
            _reject_gh_version("GH_VERSION_PROCESS_FAILED")
        try:
            parsed = parse_gh_cli_version_output(result.stdout)
        except GhVersionOutputError as error:
            _reject_gh_version(error.code)
        return {"version": parsed.semantic_version}
    if result.returncode != 0 or len(result.stdout) > 1024 * 1024:
        _reject("BOOTSTRAP_STAGE0_GH_VERIFICATION_FAILED")
    try:
        value = json.loads(result.stdout)
    except (UnicodeDecodeError, json.JSONDecodeError):
        _reject("BOOTSTRAP_STAGE0_GH_OUTPUT_INVALID")
    if type(value) not in {dict, list} or not value:
        _reject("BOOTSTRAP_STAGE0_GH_OUTPUT_INVALID")
    return value


def authorize_online_stage0(
    *,
    tag: str,
    release_commit: str,
    verified_at: str,
) -> BootstrapAuthorization:
    if not _TAG.fullmatch(tag) or not _COMMIT.fullmatch(release_commit):
        _reject("BOOTSTRAP_RELEASE_BINDING_INVALID")
    if not _UTC.fullmatch(verified_at):
        _reject("BOOTSTRAP_VERIFICATION_TIME_INVALID")
    protected = _safe_root() / _MATERIALS_FILE
    before = _protected_material_bytes(protected)
    _run_stage0_gh(("version",))
    release_claim = _run_stage0_gh(
        (
            "release",
            "verify",
            tag,
            "--repo",
            _REPOSITORY,
            "--format",
            "json",
        )
    )
    asset_claim = _run_stage0_gh(
        (
            "release",
            "verify-asset",
            tag,
            str(protected),
            "--repo",
            _REPOSITORY,
            "--format",
            "json",
        )
    )
    after = _protected_material_bytes(protected)
    if before != after:
        _reject("BOOTSTRAP_PROTECTED_MATERIAL_RACE")
    proof_identity = _sha256_identity(
        _canonical_json_bytes(
            {"assetVerification": asset_claim, "releaseVerification": release_claim}
        )
    )
    return commit_bootstrap_authorization(
        {
            "schemaVersion": 1,
            "state": "PRIVILEGE_ALLOWED",
            "repository": _REPOSITORY,
            "tag": tag,
            "releaseCommit": release_commit,
            "releaseAttestationIdentity": proof_identity,
            "installerMaterials": {
                "path": str(protected),
                "sha256": _sha256_identity(after),
                "size": len(after),
            },
            "stage0": {
                "model": _STAGE0_MODEL,
                "carrier": _ONLINE_CARRIER,
                "verifierIdentity": f"gh:{_GH_VERSION}",
            },
            "verifiedAt": verified_at,
        }
    )
