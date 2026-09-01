"""Closed prepublication Candidate input, verification, and receipt contracts.

This module deliberately stops before Release Authority.  It turns one exact
successful Qualification artifact into a fixed local, digest-addressed input
that the disposable-VM harness and candidate-only Installer can consume.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import stat
import tarfile
import tempfile
import unicodedata
import zipfile
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO

from jsonschema import Draft202012Validator, FormatChecker

from durability.canonical import canonical_json_bytes as canonical_identity_bytes
from durability.platform import (
    PlatformQualificationError,
    canonical_platform_qualification_bytes,
    parse_platform_qualification,
)
from updater import __version__ as updater_version
from updater.authority import VerifiedReleaseMaterials
from updater.oci import (
    OCIContractError,
    OCIImageExpectation,
    VerifiedOCIImageSet,
    verify_oci_image,
    verify_oci_image_set,
)

from .contract import (
    POSTGRES_DIGEST,
    POSTGRES_REPOSITORY,
    REDIS_DIGEST,
    REDIS_REPOSITORY,
    deployment_contract_digest,
    validate_deployment_contract,
    validate_manifest,
)
from .materials import (
    CANDIDATE_PRODUCTION_RECEIPT_NAME,
    CANDIDATE_QUALIFICATION_ROOT_FILES,
    MAX_MATERIAL_TOTAL_BYTES,
    AtomicReleaseFile,
    MaterialContractError,
    VerifiedMaterialSet,
    extract_installer_materials,
    reject_duplicate_json_keys,
    validate_candidate_production_receipt,
    validate_material_contract,
    verify_prepublication_material_identity,
)
from .metadata_freshness import validate_qualification_run_metadata
from .notes import render_release_notes, validate_release_notes
from .producer_toolchain import validate_producer_toolchain_receipt

REPOSITORY = "yanyuhanyue/AniMemo"
QUALIFICATION_WORKFLOW_NAME = "Release Producer"
QUALIFICATION_WORKFLOW_PATH = ".github/workflows/release.yml"
QUALIFICATION_WORKFLOW_REF = (
    "yanyuhanyue/AniMemo/.github/workflows/release.yml@refs/heads/main"
)
CANDIDATE_INPUT_SCHEMA = "animemo.prepublication-candidate-input/v1"
VERIFIED_CANDIDATE_SCHEMA = "animemo.verified-prepublication-candidate/v2"
VERIFICATION_EXECUTION_RECEIPT_SCHEMA = (
    "animemo.prepublication-candidate-verification-execution-receipt/v1"
)
PROFILE_RECEIPT_SCHEMA = "animemo.prepublication-candidate-profile-receipt/v1"
AGGREGATE_RECEIPT_SCHEMA = (
    "animemo.prepublication-candidate-acceptance-receipt/v3"
)
VERIFIER_CONTRACT_VERSION = "2"
VERIFIED_CANDIDATE_ROOT = Path("/var/lib/animemo/prepublication-candidates/v2")
CANDIDATE_RUNTIME_ROOT = "candidate-runtime"
OCI_ROLES = ("api", "postgres", "redis", "web")
PROFILE_ROLES = ("FRESH_BASE", "DOCKER_BASE", "RUNTIME_BASE_OFFLINE")
INSTALLER_PROFILES = (
    "ONLINE_FRESH",
    "ONLINE_EXISTING_DOCKER",
    "OFFLINE_VALIDATE_ONLY",
)

MAX_ARCHIVE_BYTES = 16 * 1024 * 1024 * 1024
MAX_ARCHIVE_MEMBER_BYTES = 4 * 1024 * 1024 * 1024
MAX_ARCHIVE_FILE_COUNT = 1100
MAX_RUNTIME_BYTES = 16 * 1024 * 1024 * 1024
MAX_JSON_BYTES = 16 * 1024 * 1024
MAX_RECEIPT_B64URL_BYTES = 512 * 1024
MAX_CONTROLLER_ARCHIVE_BYTES = 4 * 1024 * 1024 * 1024
MAX_CONTROLLER_EXPANDED_BYTES = 6 * 1024 * 1024 * 1024
MIN_CONTROLLER_DISK_RESERVE_BYTES = 2 * 1024 * 1024 * 1024
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_GIT_SHA = re.compile(r"[0-9a-f]{40}\Z")
_RC = re.compile(
    r"(?P<target>v(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\."
    r"(?:0|[1-9][0-9]*))-rc\.(?P<sequence>[1-9][0-9]*)\Z"
)
_WINDOWS_DRIVE = re.compile(r"[A-Za-z]:")
_SINGLE_LINK_COUNT = frozenset({1})
_RECEIPT_PUBLICATION_LINK_COUNTS = frozenset({1, 2})

class CandidateContractError(ValueError):
    """A prepublication Candidate input is not closed or correctly bound."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _reject(code: str) -> None:
    raise CandidateContractError(code)


def canonical_json_bytes(value: object) -> bytes:
    return (
        json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def apt_network_sequence_matches(
    observed_commands: list[tuple[str, int]],
    *,
    expected_digests: list[str],
    retryable_digests: list[str],
) -> bool:
    """Match the exact planned APT sequence, including its one allowed retry."""

    if (
        len(expected_digests) != len(set(expected_digests))
        or len(retryable_digests) != len(set(retryable_digests))
        or not set(retryable_digests).issubset(expected_digests)
    ):
        return False
    observed_index = 0
    retryable = set(retryable_digests)
    for expected_digest in expected_digests:
        if observed_index >= len(observed_commands):
            return False
        observed_digest, return_code = observed_commands[observed_index]
        if observed_digest != expected_digest:
            return False
        observed_index += 1
        if return_code == 124:
            if expected_digest not in retryable or observed_index >= len(
                observed_commands
            ):
                return False
            retry_digest, retry_return_code = observed_commands[observed_index]
            if retry_digest != expected_digest or retry_return_code != 0:
                return False
            observed_index += 1
        elif return_code != 0:
            return False
    return observed_index == len(observed_commands)


_CANDIDATE_COMMAND_OBSERVER_IDENTITY = sha256_bytes(
    canonical_identity_bytes(
        {
            "boundaries": ["PLATFORM", "RUNTIME"],
            "externalPullDispositions": [
                "EXPLICIT_NEVER",
                "FORBIDDEN_DETECTED",
                "NOT_APPLICABLE",
            ],
            "networkClassification": "APT_NETWORK",
            "localClassifications": ["LOCAL_DOCKER_SOCKET", "LOCAL_ONLY"],
            "unknownClassification": "UNKNOWN_NETWORK_CAPABILITY",
            "version": 1,
        }
    )
)


def _file_identity(metadata: os.stat_result) -> tuple[int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
    )


@contextmanager
def _open_regular_file(
    path: Path,
    *,
    maximum: int,
    allowed_link_counts: frozenset[int] = _SINGLE_LINK_COUNT,
) -> Iterator[BinaryIO]:
    try:
        before = path.lstat()
    except OSError as error:
        raise CandidateContractError("CANDIDATE_FILE_UNAVAILABLE") from error
    if (
        path.is_symlink()
        or bool(getattr(path, "is_junction", lambda: False)())
        or not stat.S_ISREG(before.st_mode)
        or before.st_nlink not in allowed_link_counts
        or before.st_size < 0
        or before.st_size > maximum
    ):
        _reject("CANDIDATE_FILE_UNSAFE")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise CandidateContractError("CANDIDATE_FILE_UNREADABLE") from error
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink not in allowed_link_counts
            or opened.st_size < 0
            or opened.st_size > maximum
            or _file_identity(opened) != _file_identity(before)
        ):
            _reject("CANDIDATE_FILE_CHANGED_DURING_OPEN")
        with os.fdopen(descriptor, "rb", closefd=False) as source:
            yield source
            after = os.fstat(descriptor)
            if (
                after.st_nlink not in allowed_link_counts
                or _file_identity(after) != _file_identity(opened)
            ):
                _reject("CANDIDATE_FILE_CHANGED_DURING_READ")
    finally:
        os.close(descriptor)


def _sha256_file(path: Path, *, maximum: int = MAX_ARCHIVE_MEMBER_BYTES) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    try:
        with _open_regular_file(path, maximum=maximum) as source:
            while chunk := source.read(1024 * 1024):
                size += len(chunk)
                if size > maximum:
                    _reject("CANDIDATE_FILE_SIZE_LIMIT")
                digest.update(chunk)
    except OSError as error:
        raise CandidateContractError("CANDIDATE_FILE_UNREADABLE") from error
    return "sha256:" + digest.hexdigest(), size


def _read_regular_file(
    path: Path,
    *,
    maximum: int,
    allowed_link_counts: frozenset[int] = _SINGLE_LINK_COUNT,
) -> bytes:
    try:
        with _open_regular_file(
            path,
            maximum=maximum,
            allowed_link_counts=allowed_link_counts,
        ) as source:
            value = source.read(maximum + 1)
    except OSError as error:
        raise CandidateContractError("CANDIDATE_FILE_UNREADABLE") from error
    if len(value) > maximum:
        _reject("CANDIDATE_FILE_SIZE_LIMIT")
    return value


def _strict_json_bytes(value: bytes, *, code: str) -> dict[str, Any]:
    if not value or len(value) > MAX_JSON_BYTES:
        _reject(code)
    try:
        document = json.loads(value.decode("utf-8"), object_pairs_hook=reject_duplicate_json_keys)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise CandidateContractError(code) from error
    if type(document) is not dict:
        _reject(code)
    return document


def _strict_json_file(path: Path, *, code: str) -> tuple[dict[str, Any], bytes]:
    try:
        value = _read_regular_file(path, maximum=MAX_JSON_BYTES)
    except CandidateContractError as error:
        raise CandidateContractError(code) from error
    return _strict_json_bytes(value, code=code), value


def _schema_path(name: str) -> Path:
    return Path(__file__).resolve().parent / name


def _validate_schema(value: object, schema_name: str, *, code: str) -> dict[str, Any]:
    schema, _ = _strict_json_file(_schema_path(schema_name), code="CANDIDATE_SCHEMA_INVALID")
    try:
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(schema, format_checker=FormatChecker()).validate(value)
    except Exception as error:  # jsonschema normalizes many concrete validation errors
        raise CandidateContractError(code) from error
    if type(value) is not dict:
        _reject(code)
    return dict(value)


def _parse_time(value: object, *, code: str) -> datetime:
    if type(value) is not str or not value:
        _reject(code)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise CandidateContractError(code) from error
    if parsed.tzinfo is None:
        _reject(code)
    return parsed.astimezone(timezone.utc)


def _canonical_utc_time(value: object, *, code: str) -> str:
    return _parse_time(value, code=code).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _relative_path(value: object) -> str:
    if type(value) is not str or not value or "\\" in value:
        _reject("CANDIDATE_ARCHIVE_PATH_INVALID")
    if unicodedata.normalize("NFC", value) != value:
        _reject("CANDIDATE_ARCHIVE_PATH_INVALID")
    if any(ord(character) < 32 for character in value):
        _reject("CANDIDATE_ARCHIVE_PATH_INVALID")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or value.startswith("/")
        or _WINDOWS_DRIVE.match(value)
        or any(part in {"", ".", ".."} for part in path.parts)
        or path.as_posix() != value
        or len(value.encode("utf-8")) > 1024
        or len(path.parts) > 12
    ):
        _reject("CANDIDATE_ARCHIVE_PATH_INVALID")
    return value


def _runtime_inventory(root: Path) -> list[dict[str, object]]:
    runtime = root / CANDIDATE_RUNTIME_ROOT
    try:
        root_metadata = runtime.lstat()
    except OSError as error:
        raise CandidateContractError("CANDIDATE_RUNTIME_ROOT_UNAVAILABLE") from error
    if runtime.is_symlink() or not stat.S_ISDIR(root_metadata.st_mode):
        _reject("CANDIDATE_RUNTIME_ROOT_INVALID")
    inventory: list[dict[str, object]] = []
    folded: set[str] = set()
    total = 0
    try:
        paths = sorted(runtime.rglob("*"), key=lambda item: item.relative_to(root).as_posix())
    except OSError as error:
        raise CandidateContractError("CANDIDATE_RUNTIME_ENUMERATION_FAILED") from error
    for path in paths:
        relative = path.relative_to(root).as_posix()
        _relative_path(relative)
        folded_relative = relative.casefold()
        if folded_relative in folded:
            _reject("CANDIDATE_RUNTIME_CASE_COLLISION")
        folded.add(folded_relative)
        metadata = path.lstat()
        if stat.S_ISDIR(metadata.st_mode):
            if path.is_symlink() or bool(getattr(path, "is_junction", lambda: False)()):
                _reject("CANDIDATE_RUNTIME_LINK_FORBIDDEN")
            continue
        digest, size = _sha256_file(path)
        total += size
        if total > MAX_RUNTIME_BYTES:
            _reject("CANDIDATE_RUNTIME_TOTAL_SIZE_LIMIT")
        inventory.append({"path": relative, "sha256": digest, "size": size})
    if len(inventory) > MAX_ARCHIVE_FILE_COUNT:
        _reject("CANDIDATE_RUNTIME_FILE_COUNT_LIMIT")
    return inventory


def _images_from_manifest(manifest: Mapping[str, Any]) -> list[dict[str, object]]:
    images = manifest.get("images")
    if type(images) is not dict or set(images) != set(OCI_ROLES):
        _reject("CANDIDATE_IMAGE_AUTHORITY_INVALID")
    result: list[dict[str, object]] = []
    for role in OCI_ROLES:
        image = images[role]
        if type(image) is not dict:
            _reject("CANDIDATE_IMAGE_AUTHORITY_INVALID")
        result.append(
            {
                "digest": image.get("digest"),
                "layoutPath": f"oci/{role}",
                "platform": image.get("platform"),
                "repository": image.get("repository"),
                "role": role,
            }
        )
    return result


def _verify_runtime(root: Path, manifest: Mapping[str, Any]) -> VerifiedOCIImageSet:
    try:
        return verify_oci_image_set(
            root / CANDIDATE_RUNTIME_ROOT,
            _images_from_manifest(manifest),
        )
    except OCIContractError as error:
        raise CandidateContractError(error.args[0] if error.args else "CANDIDATE_OCI_INVALID") from error


def _remove_empty_buildx_ingest_directory(layout: Path) -> bool:
    """Remove only BuildKit's observed empty directory-export scratch root."""

    ingest = layout / "ingest"
    try:
        metadata = ingest.lstat()
    except FileNotFoundError:
        return False
    except OSError as error:
        raise CandidateContractError("CANDIDATE_OCI_INGEST_UNAVAILABLE") from error
    if (
        stat.S_ISLNK(metadata.st_mode)
        or bool(getattr(ingest, "is_junction", lambda: False)())
        or not stat.S_ISDIR(metadata.st_mode)
    ):
        _reject("CANDIDATE_OCI_INGEST_INVALID")
    try:
        os.rmdir(ingest)
    except OSError as error:
        raise CandidateContractError("CANDIDATE_OCI_INGEST_NOT_EMPTY") from error
    return True


def _restore_empty_buildx_ingest_directory(layout: Path) -> None:
    ingest = layout / "ingest"
    try:
        ingest.mkdir(mode=0o700)
    except OSError as error:
        raise CandidateContractError("CANDIDATE_OCI_ROLLBACK_FAILED") from error


def normalize_candidate_oci_layout(
    *,
    source_root: Path,
    layout: Path,
    role: str,
    repository: str,
    expected_digest: str,
) -> dict[str, object]:
    """Close Buildx's root wrapper without changing the authoritative image DAG."""

    if role not in OCI_ROLES or not _DIGEST.fullmatch(expected_digest):
        _reject("CANDIDATE_OCI_EXPECTATION_INVALID")
    expected_layout = source_root / "oci" / role
    if os.path.normcase(os.path.abspath(layout)) != os.path.normcase(
        os.path.abspath(expected_layout)
    ):
        _reject("CANDIDATE_OCI_LAYOUT_PATH_MISMATCH")
    index_path = layout / "index.json"
    index, original = _strict_json_file(index_path, code="CANDIDATE_OCI_INDEX_INVALID")
    if set(index) != {"manifests", "mediaType", "schemaVersion"}:
        _reject("CANDIDATE_OCI_INDEX_INVALID")
    if (
        index.get("schemaVersion") != 2
        or index.get("mediaType") != "application/vnd.oci.image.index.v1+json"
        or type(index.get("manifests")) is not list
        or len(index["manifests"]) != 1
    ):
        _reject("CANDIDATE_OCI_INDEX_INVALID")
    descriptor = index["manifests"][0]
    if type(descriptor) is not dict:
        _reject("CANDIDATE_OCI_DESCRIPTOR_INVALID")
    canonical_fields = {"digest", "mediaType", "platform", "size"}
    required_fields = {"digest", "mediaType", "size"}
    optional_fields = {"annotations", "artifactType", "platform"}
    before_fields = set(descriptor)
    if before_fields == canonical_fields:
        changed = False
    else:
        if not required_fields.issubset(before_fields) or not before_fields.issubset(
            required_fields | optional_fields
        ):
            _reject("CANDIDATE_OCI_DESCRIPTOR_INVALID")
        platform = descriptor.get("platform")
        if platform is not None and platform != {
            "architecture": "amd64",
            "os": "linux",
        }:
            _reject("CANDIDATE_OCI_DESCRIPTOR_INVALID")
        annotations = descriptor.get("annotations", {})
        if type(annotations) is not dict or any(
            type(key) is not str
            or type(value) is not str
            or not key
            or not value
            or len(key) > 256
            or len(value) > 4096
            for key, value in annotations.items()
        ):
            _reject("CANDIDATE_OCI_DESCRIPTOR_INVALID")
        artifact_type = descriptor.get("artifactType")
        if artifact_type is not None and (
            type(artifact_type) is not str
            or not artifact_type
            or len(artifact_type) > 256
        ):
            _reject("CANDIDATE_OCI_DESCRIPTOR_INVALID")
        descriptor = {
            "digest": descriptor.get("digest"),
            "mediaType": descriptor.get("mediaType"),
            "platform": {"architecture": "amd64", "os": "linux"},
            "size": descriptor.get("size"),
        }
        index["manifests"] = [descriptor]
        changed = True
    if descriptor.get("digest") != expected_digest:
        _reject("OCI_EXPECTED_MANIFEST_DIGEST_MISMATCH")
    expectation = OCIImageExpectation(
        role=role,
        repository=repository,
        digest=expected_digest,
        platform="linux/amd64",
        layout_path=f"oci/{role}",
    )
    replacement = canonical_json_bytes(index)
    temporary: Path | None = None
    index_replaced = False
    ingest_removed = _remove_empty_buildx_ingest_directory(layout)
    try:
        if changed:
            descriptor_fd, temporary_name = tempfile.mkstemp(
                prefix=".candidate-index-", dir=layout
            )
            temporary = Path(temporary_name)
            os.chmod(temporary, 0o600)
            with os.fdopen(descriptor_fd, "wb", closefd=True) as output:
                output.write(replacement)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, index_path)
            temporary = None
            index_replaced = True
        verified = verify_oci_image(layout, expectation)
    except Exception:
        try:
            if index_replaced:
                index_path.write_bytes(original)
            if ingest_removed:
                _restore_empty_buildx_ingest_directory(layout)
        except Exception as rollback_error:
            raise CandidateContractError("CANDIDATE_OCI_ROLLBACK_FAILED") from rollback_error
        raise
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return {
        "changed": changed or ingest_removed,
        "configDigest": verified.config_digest,
        "digest": verified.digest,
        "ingestDirectoryRemoved": ingest_removed,
        "layerCount": len(verified.layer_digests),
        "role": role,
    }


def extract_candidate_oci_archive(*, archive: Path, destination: Path) -> dict[str, object]:
    """Safely expand one registry-fetched OCI layout tar into a new fixed root."""

    if destination.exists() or destination.is_symlink():
        _reject("CANDIDATE_OCI_OUTPUT_EXISTS")
    folded: set[str] = set()
    names: set[str] = set()
    regular_count = 0
    total = 0
    try:
        with (
            _open_regular_file(archive, maximum=MAX_ARCHIVE_BYTES) as source,
            tarfile.open(fileobj=source, mode="r:*") as transport,
        ):
                members = transport.getmembers()
                if not members or len(members) > MAX_ARCHIVE_FILE_COUNT:
                    _reject("CANDIDATE_OCI_ARCHIVE_FILE_COUNT_INVALID")
                for member in members:
                    name = _relative_path(member.name.rstrip("/"))
                    if name in names:
                        _reject("CANDIDATE_OCI_ARCHIVE_DUPLICATE_PATH")
                    if name.casefold() in folded:
                        _reject("CANDIDATE_OCI_ARCHIVE_CASE_COLLISION")
                    names.add(name)
                    folded.add(name.casefold())
                    parts = PurePosixPath(name).parts
                    allowed_directory = name in {"blobs", "blobs/sha256"}
                    allowed_file = name in {"index.json", "oci-layout"} or (
                        len(parts) == 3
                        and parts[:2] == ("blobs", "sha256")
                        and re.fullmatch(r"[0-9a-f]{64}", parts[2]) is not None
                    )
                    if member.isdir():
                        if not allowed_directory:
                            _reject("CANDIDATE_OCI_ARCHIVE_PATH_INVALID")
                        continue
                    if (
                        not member.isfile()
                        or not allowed_file
                        or member.size < 1
                        or member.size > MAX_ARCHIVE_MEMBER_BYTES
                    ):
                        _reject("CANDIDATE_OCI_ARCHIVE_ENTRY_UNSAFE")
                    regular_count += 1
                    total += member.size
                    if total > MAX_RUNTIME_BYTES:
                        _reject("CANDIDATE_OCI_ARCHIVE_SIZE_LIMIT")
                destination.mkdir(parents=True, mode=0o700)
                for member in sorted(members, key=lambda item: item.name):
                    name = member.name.rstrip("/")
                    target = destination.joinpath(*PurePosixPath(name).parts)
                    if member.isdir():
                        target.mkdir(parents=True, exist_ok=True, mode=0o700)
                        continue
                    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    extracted = transport.extractfile(member)
                    if extracted is None:
                        _reject("CANDIDATE_OCI_ARCHIVE_ENTRY_UNREADABLE")
                    written = 0
                    with extracted, target.open("xb") as output:
                        while chunk := extracted.read(1024 * 1024):
                            written += len(chunk)
                            if written > member.size:
                                _reject("CANDIDATE_OCI_ARCHIVE_MEMBER_GREW")
                            output.write(chunk)
                    if written != member.size:
                        _reject("CANDIDATE_OCI_ARCHIVE_MEMBER_SIZE_MISMATCH")
                    os.chmod(target, 0o600)
    except CandidateContractError:
        if destination.exists():
            shutil.rmtree(destination, ignore_errors=True)
        raise
    except (OSError, tarfile.TarError) as error:
        if destination.exists():
            shutil.rmtree(destination, ignore_errors=True)
        raise CandidateContractError("CANDIDATE_OCI_ARCHIVE_INVALID") from error
    return {"fileCount": regular_count, "status": "PASS"}


def _verify_checksums(root: Path) -> None:
    checksums = root / "checksums.txt"
    value = _read_regular_file(checksums, maximum=1024 * 1024)
    try:
        lines = value.decode("utf-8").splitlines()
    except UnicodeDecodeError as error:
        raise CandidateContractError("CANDIDATE_CHECKSUMS_INVALID") from error
    if not value or len(lines) != 3:
        _reject("CANDIDATE_CHECKSUMS_INVALID")
    expected_names = {
        "deployment-contract.json",
        "installer-materials.tar",
        "release-manifest.json",
    }
    observed: dict[str, str] = {}
    for line in lines:
        digest, separator, name = line.partition("  ")
        if (
            separator != "  "
            or not re.fullmatch(r"[0-9a-f]{64}", digest)
            or name not in expected_names
            or name in observed
        ):
            _reject("CANDIDATE_CHECKSUMS_INVALID")
        observed[name] = digest
    if set(observed) != expected_names:
        _reject("CANDIDATE_CHECKSUMS_INVALID")
    for name, digest in observed.items():
        actual, _ = _sha256_file(root / name)
        if actual != "sha256:" + digest:
            _reject("CANDIDATE_INTERNAL_SHA256_MISMATCH")


def validate_candidate_input(
    value: object,
    *,
    root: Path | None = None,
) -> dict[str, Any]:
    candidate = _validate_schema(
        value,
        "prepublication-candidate-input.schema.json",
        code="CANDIDATE_INPUT_SCHEMA_INVALID",
    )
    match = _RC.fullmatch(candidate["candidate_version"])
    if (
        match is None
        or match.group("target") != candidate["target_version"]
        or int(match.group("sequence")) != candidate["candidate_sequence"]
        or candidate["qualification_workflow_identity"]["sha"]
        != candidate["source_sha"]
        or len(set(candidate["qualification_artifact_ids"].values())) != 2
    ):
        _reject("CANDIDATE_INPUT_IDENTITY_MISMATCH")
    paths = [item["path"] for item in candidate["candidate_runtime_file_inventory"]]
    if paths != sorted(paths) or len(paths) != len(set(paths)):
        _reject("CANDIDATE_RUNTIME_INVENTORY_INVALID")
    if root is None:
        return candidate

    root = Path(root)
    _verify_checksums(root)
    manifest, manifest_bytes = _strict_json_file(
        root / "release-manifest.json", code="CANDIDATE_MANIFEST_INVALID"
    )
    deployment, deployment_bytes = _strict_json_file(
        root / "deployment-contract.json", code="CANDIDATE_DEPLOYMENT_INVALID"
    )
    notes, notes_bytes = _strict_json_file(
        root / "release-notes.json", code="CANDIDATE_RELEASE_NOTES_INVALID"
    )
    prepublication, _ = _strict_json_file(
        root / "prepublication-materials.json", code="CANDIDATE_MATERIALS_INVALID"
    )
    try:
        validate_manifest(manifest, updater_version=updater_version)
        validated_notes = validate_release_notes(notes)
        validate_deployment_contract(
            deployment,
            installer_materials=root / "installer-materials.tar",
        )
        verify_prepublication_material_identity(
            prepublication,
            installer_materials=root / "installer-materials.tar",
            deployment_contract=root / "deployment-contract.json",
            expected_candidate_sha=candidate["source_sha"],
            expected_candidate_tree_sha=candidate["source_tree"],
        )
    except Exception as error:
        raise CandidateContractError("CANDIDATE_MATERIAL_CONTRACT_INVALID") from error
    markdown = _read_regular_file(
        root / "release-notes.md", maximum=MAX_JSON_BYTES
    )
    if markdown != render_release_notes(validated_notes).encode("utf-8"):
        _reject("CANDIDATE_RELEASE_NOTES_MARKDOWN_MISMATCH")
    material_hashes = {
        "release_notes_json_sha256": sha256_bytes(notes_bytes),
        "release_notes_markdown_sha256": sha256_bytes(markdown),
        "release_manifest_sha256": sha256_bytes(manifest_bytes),
        "deployment_contract_sha256": sha256_bytes(deployment_bytes),
        "installer_materials_sha256": _sha256_file(root / "installer-materials.tar")[0],
        "checksums_sha256": _sha256_file(root / "checksums.txt")[0],
        "producer_toolchain_receipt_sha256": _sha256_file(
            root / "release-producer-toolchain-receipt.json"
        )[0],
    }
    if any(candidate[field] != digest for field, digest in material_hashes.items()):
        _reject("CANDIDATE_MATERIAL_DIGEST_MISMATCH")
    if (
        manifest["release"]["commit"] != candidate["source_sha"]
        or manifest["release"]["version"] != candidate["candidate_version"]
        or deployment_contract_digest(deployment)
        != manifest["deployment"]["contractSha256"]
        or manifest["deployment"]["installerMaterials"]["sha256"]
        != candidate["installer_materials_sha256"]
    ):
        _reject("CANDIDATE_MATERIAL_IDENTITY_MISMATCH")
    try:
        validate_producer_toolchain_receipt(
            root / "release-producer-toolchain-receipt.json",
            expected_candidate_sha=candidate["source_sha"],
        )
    except Exception as error:
        raise CandidateContractError(
            "CANDIDATE_PRODUCER_TOOLCHAIN_INVALID"
        ) from error
    runtime_inventory = _runtime_inventory(root)
    if runtime_inventory != candidate["candidate_runtime_file_inventory"]:
        _reject("CANDIDATE_RUNTIME_INVENTORY_MISMATCH")
    verified = _verify_runtime(root, manifest)
    expected_digests = {
        f"{image.role}_oci_digest": image.digest for image in verified.images
    }
    if any(candidate[name] != digest for name, digest in expected_digests.items()):
        _reject("CANDIDATE_OCI_DIGEST_MISMATCH")
    return candidate


def build_candidate_input(
    *,
    root: Path,
    qualification_run_id: int,
    qualification_run_attempt: int,
    source_sha: str,
    source_tree: str,
    artifact_ids: Mapping[str, int],
    artifact_api_digests: Mapping[str, str],
    generated_at: str,
    output: Path,
) -> dict[str, Any]:
    if output.exists() or output.is_symlink():
        _reject("CANDIDATE_INPUT_OUTPUT_EXISTS")
    manifest, manifest_bytes = _strict_json_file(
        root / "release-manifest.json", code="CANDIDATE_MANIFEST_INVALID"
    )
    notes, notes_bytes = _strict_json_file(
        root / "release-notes.json", code="CANDIDATE_RELEASE_NOTES_INVALID"
    )
    del notes
    candidate_version = manifest.get("release", {}).get("version")
    match = _RC.fullmatch(candidate_version if type(candidate_version) is str else "")
    if match is None:
        _reject("CANDIDATE_VERSION_INVALID")
    runtime = _verify_runtime(root, manifest)
    by_role = {image.role: image for image in runtime.images}
    value = {
        "schema": CANDIDATE_INPUT_SCHEMA,
        "version": 1,
        "purpose": "PREPUBLICATION_CANDIDATE_ACCEPTANCE_ONLY",
        "repository": REPOSITORY,
        "qualification_run_id": qualification_run_id,
        "qualification_run_attempt": qualification_run_attempt,
        "qualification_workflow_identity": {
            "name": QUALIFICATION_WORKFLOW_NAME,
            "path": QUALIFICATION_WORKFLOW_PATH,
            "ref": QUALIFICATION_WORKFLOW_REF,
            "sha": source_sha,
        },
        "qualification_artifact_ids": dict(artifact_ids),
        "qualification_artifact_api_digests": dict(artifact_api_digests),
        "source_sha": source_sha,
        "source_tree": source_tree,
        "target_version": match.group("target"),
        "candidate_version": candidate_version,
        "candidate_sequence": int(match.group("sequence")),
        "release_notes_json_sha256": sha256_bytes(notes_bytes),
        "release_notes_markdown_sha256": _sha256_file(root / "release-notes.md")[0],
        "release_manifest_sha256": sha256_bytes(manifest_bytes),
        "deployment_contract_sha256": _sha256_file(root / "deployment-contract.json")[0],
        "installer_materials_sha256": _sha256_file(root / "installer-materials.tar")[0],
        "checksums_sha256": _sha256_file(root / "checksums.txt")[0],
        "producer_toolchain_receipt_sha256": _sha256_file(
            root / "release-producer-toolchain-receipt.json"
        )[0],
        "api_oci_digest": by_role["api"].digest,
        "web_oci_digest": by_role["web"].digest,
        "postgres_oci_digest": by_role["postgres"].digest,
        "redis_oci_digest": by_role["redis"].digest,
        "candidate_runtime_file_inventory": _runtime_inventory(root),
        "release_authority_granted": False,
        "production_authorized": False,
        "publish_authorized": False,
        "generated_at": generated_at,
    }
    validate_candidate_input(value, root=root)
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        with output.open("xb") as handle:
            handle.write(canonical_json_bytes(value))
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(output, 0o600)
    except OSError as error:
        raise CandidateContractError("CANDIDATE_INPUT_WRITE_FAILED") from error
    return value


def _artifact_by_id(metadata: object, artifact_id: int) -> dict[str, Any]:
    if type(metadata) is not dict or type(metadata.get("artifacts")) is not list:
        _reject("CANDIDATE_ARTIFACT_METADATA_INVALID")
    matches = [
        item
        for item in metadata["artifacts"]
        if type(item) is dict and item.get("id") == artifact_id
    ]
    if len(matches) != 1:
        _reject("CANDIDATE_ARTIFACT_ID_CARDINALITY")
    return matches[0]


def _validate_bound_artifacts(candidate: Mapping[str, Any], metadata: object) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    expected_names = {
        "platform_qualification": f"platform-qualification-{candidate['qualification_run_id']}",
    }
    for role in sorted(expected_names):
        artifact_id = candidate["qualification_artifact_ids"][role]
        item = _artifact_by_id(metadata, artifact_id)
        if (
            item.get("name") != expected_names[role]
            or item.get("expired") is not False
            or item.get("digest")
            != candidate["qualification_artifact_api_digests"][role]
            or item.get("workflow_run", {}).get("id")
            != candidate["qualification_run_id"]
            or item.get("workflow_run", {}).get("head_sha") != candidate["source_sha"]
        ):
            _reject("CANDIDATE_ARTIFACT_BINDING_MISMATCH")
        result.append(
            {
                "role": role,
                "id": artifact_id,
                "api_digest": item["digest"],
            }
        )
    # Candidate Input v1 keeps ``release_dry_run`` only as a compatibility
    # transport role.  Qualification v3 and the production receipt validate
    # that short-lived provisional identity from the final archive itself;
    # Candidate authority must remain verifiable after it expires.
    return result


def _archive_entries(
    archive: zipfile.ZipFile,
    *,
    maximum_total: int = MAX_ARCHIVE_BYTES,
) -> tuple[list[zipfile.ZipInfo], dict[str, int]]:
    entries = archive.infolist()
    if not entries or len(entries) > MAX_ARCHIVE_FILE_COUNT:
        _reject("CANDIDATE_ARCHIVE_FILE_COUNT_INVALID")
    names: list[str] = []
    folded: set[str] = set()
    total = 0
    for entry in entries:
        name = _relative_path(entry.filename)
        unix_mode = entry.external_attr >> 16
        file_type = stat.S_IFMT(unix_mode)
        if (
            entry.is_dir()
            or entry.flag_bits & 0x1
            or file_type not in {0, stat.S_IFREG}
            or entry.file_size <= 0
            or entry.file_size > min(MAX_ARCHIVE_MEMBER_BYTES, maximum_total)
        ):
            _reject("CANDIDATE_ARCHIVE_ENTRY_UNSAFE")
        if name in names:
            _reject("CANDIDATE_ARCHIVE_DUPLICATE_PATH")
        if name.casefold() in folded:
            _reject("CANDIDATE_ARCHIVE_CASE_COLLISION")
        names.append(name)
        folded.add(name.casefold())
        total += entry.file_size
        if total > maximum_total:
            _reject("CANDIDATE_ARCHIVE_SIZE_LIMIT")
    return entries, {entry.filename: entry.file_size for entry in entries}


def _extract_candidate_archive_stream(
    source: BinaryIO,
    destination: Path,
    *,
    maximum_total: int = MAX_ARCHIVE_BYTES,
) -> tuple[dict[str, Any], str, int]:
    try:
        if destination.exists() or destination.is_symlink():
            metadata = destination.lstat()
            if (
                destination.is_symlink()
                or bool(getattr(destination, "is_junction", lambda: False)())
                or not stat.S_ISDIR(metadata.st_mode)
                or any(destination.iterdir())
            ):
                _reject("CANDIDATE_EXTRACTION_ROOT_INVALID")
        else:
            destination.mkdir(parents=True, mode=0o700)
        with zipfile.ZipFile(source, mode="r") as archive:
            entries, sizes = _archive_entries(archive, maximum_total=maximum_total)
            if "candidate-input.json" not in sizes:
                _reject("CANDIDATE_INPUT_MISSING")
            if sizes["candidate-input.json"] > MAX_JSON_BYTES:
                _reject("CANDIDATE_INPUT_SIZE_LIMIT")
            candidate_bytes = archive.read("candidate-input.json")
            candidate = validate_candidate_input(
                _strict_json_bytes(candidate_bytes, code="CANDIDATE_INPUT_INVALID")
            )
            if canonical_json_bytes(candidate) != candidate_bytes:
                _reject("CANDIDATE_INPUT_JSON_NON_CANONICAL")
            expected = set(CANDIDATE_QUALIFICATION_ROOT_FILES)
            expected.add(f"release-qualification-{candidate['qualification_run_id']}.json")
            expected.update(
                item["path"] for item in candidate["candidate_runtime_file_inventory"]
            )
            if set(sizes) != expected:
                _reject("CANDIDATE_ARCHIVE_FILE_SET_MISMATCH")
            for entry in sorted(entries, key=lambda item: item.filename):
                target = destination.joinpath(*PurePosixPath(entry.filename).parts)
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                written = 0
                digest = hashlib.sha256()
                with archive.open(entry, mode="r") as member, target.open("xb") as output:
                    while chunk := member.read(1024 * 1024):
                        written += len(chunk)
                        if written > entry.file_size:
                            _reject("CANDIDATE_ARCHIVE_MEMBER_GREW")
                        digest.update(chunk)
                        output.write(chunk)
                if written != entry.file_size:
                    _reject("CANDIDATE_ARCHIVE_MEMBER_SIZE_MISMATCH")
                os.chmod(target, 0o600)
        _verify_qualification_intrinsics(destination, candidate)
    except CandidateContractError:
        raise
    except (OSError, zipfile.BadZipFile, RuntimeError) as error:
        raise CandidateContractError("CANDIDATE_ARCHIVE_INVALID") from error
    return candidate, sha256_bytes(candidate_bytes), len(entries)


def _extract_candidate_archive(
    archive_path: Path,
    destination: Path,
) -> tuple[dict[str, Any], str, int]:
    with _open_regular_file(archive_path, maximum=MAX_ARCHIVE_BYTES) as source:
        return _extract_candidate_archive_stream(source, destination)


def validate_verified_candidate(value: object) -> dict[str, Any]:
    candidate = _validate_schema(
        value,
        "verified-prepublication-candidate-v2.schema.json",
        code="VERIFIED_CANDIDATE_SCHEMA_INVALID",
    )
    oci_roles = [item["role"] for item in candidate["oci_verification"]]
    if oci_roles != list(OCI_ROLES):
        _reject("VERIFIED_CANDIDATE_OCI_ROLES_INVALID")
    if any(
        item["digest"] != candidate[f"{item['role']}_oci_digest"]
        for item in candidate["oci_verification"]
    ):
        _reject("VERIFIED_CANDIDATE_IDENTITY_BINDING_INVALID")
    inventory = candidate["runtime_file_inventory"]
    if [item["path"] for item in inventory] != sorted(
        item["path"] for item in inventory
    ):
        _reject("VERIFIED_CANDIDATE_RUNTIME_INVENTORY_INVALID")
    if candidate["runtime_inventory_sha256"] != sha256_bytes(
        canonical_json_bytes(inventory)
    ):
        _reject("VERIFIED_CANDIDATE_RUNTIME_INVENTORY_DIGEST_MISMATCH")
    for role in OCI_ROLES:
        role_inventory = [
            item
            for item in inventory
            if item["path"].startswith(f"candidate-runtime/oci/{role}/")
        ]
        if candidate["oci_runtime_inventory_sha256"][role] != sha256_bytes(
            canonical_json_bytes(role_inventory)
        ):
            _reject("VERIFIED_CANDIDATE_OCI_INVENTORY_DIGEST_MISMATCH")
    match = _RC.fullmatch(candidate["candidate_version"])
    if (
        match is None
        or match.group("target") != candidate["target_version"]
        or int(match.group("sequence")) != candidate["candidate_sequence"]
        or candidate["qualification_workflow_identity"]["sha"]
        != candidate["source_sha"]
        or len(set(candidate["qualification_artifact_ids"].values())) != 2
        or len(set(candidate["qualification_artifact_api_digests"].values())) != 2
    ):
        _reject("VERIFIED_CANDIDATE_IDENTITY_BINDING_INVALID")
    return candidate


def validate_verification_execution_receipt(value: object) -> dict[str, Any]:
    receipt = _validate_schema(
        value,
        "prepublication-candidate-verification-execution-receipt.schema.json",
        code="VERIFICATION_EXECUTION_RECEIPT_SCHEMA_INVALID",
    )
    if receipt["verified_at"] != _canonical_utc_time(
        receipt["verified_at"], code="VERIFICATION_EXECUTION_RECEIPT_TIME_INVALID"
    ):
        _reject("VERIFICATION_EXECUTION_RECEIPT_TIME_NON_CANONICAL")
    unsigned = dict(receipt)
    unsigned.pop("receipt_digest")
    if receipt["receipt_digest"] != sha256_bytes(canonical_json_bytes(unsigned)):
        _reject("VERIFICATION_EXECUTION_RECEIPT_DIGEST_MISMATCH")
    return receipt


def _runtime_inventory_digests(
    inventory: list[dict[str, Any]],
) -> tuple[str, dict[str, str]]:
    return (
        sha256_bytes(canonical_json_bytes(inventory)),
        {
            role: sha256_bytes(
                canonical_json_bytes(
                    [
                        item
                        for item in inventory
                        if item["path"].startswith(
                            f"candidate-runtime/oci/{role}/"
                        )
                    ]
                )
            )
            for role in OCI_ROLES
        },
    )


def _build_verified_candidate_identity(
    *,
    candidate: Mapping[str, Any],
    candidate_digest: str,
    containing_artifact_id: int,
    containing_artifact_api_digest: str,
    archive_digest: str,
    archive_file_count: int,
    runtime: VerifiedOCIImageSet,
) -> dict[str, Any]:
    inventory = list(candidate["candidate_runtime_file_inventory"])
    inventory_digest, role_inventory_digests = _runtime_inventory_digests(inventory)
    identity = {
        "schema": VERIFIED_CANDIDATE_SCHEMA,
        "version": 2,
        "purpose": "VERIFIED_LOCAL_PREPUBLICATION_INPUT",
        "candidate_input_sha256": candidate_digest,
        "repository": candidate["repository"],
        "qualification_run_id": candidate["qualification_run_id"],
        "qualification_run_attempt": candidate["qualification_run_attempt"],
        "qualification_workflow_identity": candidate[
            "qualification_workflow_identity"
        ],
        "qualification_artifact_ids": candidate["qualification_artifact_ids"],
        "qualification_artifact_api_digests": candidate[
            "qualification_artifact_api_digests"
        ],
        "source_sha": candidate["source_sha"],
        "source_tree": candidate["source_tree"],
        "target_version": candidate["target_version"],
        "candidate_version": candidate["candidate_version"],
        "candidate_sequence": candidate["candidate_sequence"],
        "release_notes_json_sha256": candidate["release_notes_json_sha256"],
        "release_notes_markdown_sha256": candidate[
            "release_notes_markdown_sha256"
        ],
        "release_manifest_sha256": candidate["release_manifest_sha256"],
        "deployment_contract_sha256": candidate["deployment_contract_sha256"],
        "installer_materials_sha256": candidate["installer_materials_sha256"],
        "checksums_sha256": candidate["checksums_sha256"],
        "api_oci_digest": candidate["api_oci_digest"],
        "web_oci_digest": candidate["web_oci_digest"],
        "postgres_oci_digest": candidate["postgres_oci_digest"],
        "redis_oci_digest": candidate["redis_oci_digest"],
        "containing_artifact": {
            "id": containing_artifact_id,
            "api_digest": containing_artifact_api_digest,
            "archive_sha256": archive_digest,
            "file_count": archive_file_count,
        },
        "safe_extraction": {
            "absolute_path_count": 0,
            "case_collision_count": 0,
            "duplicate_path_count": 0,
            "hardlink_count": 0,
            "parent_escape_count": 0,
            "special_file_count": 0,
            "symlink_count": 0,
        },
        "internal_checksums_result": "PASS",
        "runtime_file_inventory": inventory,
        "runtime_inventory_sha256": inventory_digest,
        "oci_runtime_inventory_sha256": role_inventory_digests,
        "oci_verification": [
            {
                "role": image.role,
                "repository": image.repository,
                "digest": image.digest,
                "platform": image.platform,
                "config_digest": image.config_digest,
                "layer_digests": list(image.layer_digests),
                "result": "PASS",
            }
            for image in sorted(runtime.images, key=lambda item: item.role)
        ],
        "verifier_contract_version": VERIFIER_CONTRACT_VERSION,
        "release_authority_granted": False,
        "production_authorized": False,
        "publish_authorized": False,
    }
    return validate_verified_candidate(identity)


def _build_verification_execution_receipt(
    *,
    identity: Mapping[str, Any],
    identity_digest: str,
    verified_at: str,
) -> dict[str, Any]:
    receipt = {
        "schema": VERIFICATION_EXECUTION_RECEIPT_SCHEMA,
        "version": 1,
        "purpose": "VERIFICATION_EXECUTION_AUDIT_ONLY",
        "candidate_input_sha256": identity["candidate_input_sha256"],
        "verified_candidate_digest": identity_digest,
        "repository": identity["repository"],
        "qualification_run_id": identity["qualification_run_id"],
        "qualification_run_attempt": identity["qualification_run_attempt"],
        "source_sha": identity["source_sha"],
        "source_tree": identity["source_tree"],
        "candidate_version": identity["candidate_version"],
        "verifier_contract_version": identity["verifier_contract_version"],
        "verified_at": _canonical_utc_time(
            verified_at, code="CANDIDATE_VERIFICATION_TIME_INVALID"
        ),
        "check_counts": {
            "qualification_artifact_count": len(
                identity["qualification_artifact_ids"]
            ),
            "containing_artifact_count": 1,
            "oci_image_count": len(identity["oci_verification"]),
            "oci_layer_count": sum(
                len(image["layer_digests"])
                for image in identity["oci_verification"]
            ),
            "runtime_file_count": len(identity["runtime_file_inventory"]),
            "archive_file_count": identity["containing_artifact"]["file_count"],
        },
        "environment_classification": "SANITIZED_LOCAL_VERIFIER",
        "result": "PASS",
        "error_code": None,
        "identity_authority_granted": False,
        "release_authority_granted": False,
        "production_authorized": False,
        "publish_authorized": False,
        "receipt_digest": "",
    }
    unsigned = dict(receipt)
    unsigned.pop("receipt_digest")
    receipt["receipt_digest"] = sha256_bytes(canonical_json_bytes(unsigned))
    return validate_verification_execution_receipt(receipt)


def _ensure_private_directory(path: Path) -> None:
    try:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        metadata = path.lstat()
    except OSError as error:
        raise CandidateContractError(
            "VERIFICATION_EXECUTION_RECEIPT_OUTPUT_UNAVAILABLE"
        ) from error
    if (
        path.is_symlink()
        or bool(getattr(path, "is_junction", lambda: False)())
        or not stat.S_ISDIR(metadata.st_mode)
        or (os.name == "posix" and metadata.st_uid != os.geteuid())
        or (os.name == "posix" and metadata.st_mode & 0o077)
    ):
        _reject("VERIFICATION_EXECUTION_RECEIPT_OUTPUT_INVALID")


def _write_append_only_receipt(root: Path, encoded: bytes, digest: str) -> bool:
    if not _DIGEST.fullmatch(digest) or sha256_bytes(encoded) != digest:
        _reject("VERIFICATION_EXECUTION_RECEIPT_OUTPUT_DIGEST_INVALID")
    receipts_root = root / "verification-receipts"
    receipt_root = receipts_root / digest.removeprefix("sha256:")
    _ensure_private_directory(receipts_root)
    _ensure_private_directory(receipt_root)
    target = receipt_root / "verification-execution-receipt.json"
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".verification-execution-receipt-", dir=receipt_root
        )
    except OSError as error:
        raise CandidateContractError(
            "VERIFICATION_EXECUTION_RECEIPT_OUTPUT_UNAVAILABLE"
        ) from error
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(encoded)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary, 0o600)
        try:
            os.link(temporary, target)
        except FileExistsError:
            try:
                existing = _read_regular_file(
                    target,
                    maximum=MAX_JSON_BYTES,
                    allowed_link_counts=_RECEIPT_PUBLICATION_LINK_COUNTS,
                )
            except CandidateContractError as error:
                raise CandidateContractError(
                    "VERIFICATION_EXECUTION_RECEIPT_OUTPUT_CONFLICT"
                ) from error
            if existing != encoded:
                _reject("VERIFICATION_EXECUTION_RECEIPT_OUTPUT_CONFLICT")
            return True
        return False
    except CandidateContractError:
        raise
    except OSError as error:
        raise CandidateContractError(
            "VERIFICATION_EXECUTION_RECEIPT_OUTPUT_UNAVAILABLE"
        ) from error
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _validate_identity_candidate_binding(
    identity: Mapping[str, Any], candidate: Mapping[str, Any]
) -> None:
    bound_fields = (
        "repository",
        "qualification_run_id",
        "qualification_run_attempt",
        "qualification_workflow_identity",
        "qualification_artifact_ids",
        "qualification_artifact_api_digests",
        "source_sha",
        "source_tree",
        "target_version",
        "candidate_version",
        "candidate_sequence",
        "release_notes_json_sha256",
        "release_notes_markdown_sha256",
        "release_manifest_sha256",
        "deployment_contract_sha256",
        "installer_materials_sha256",
        "checksums_sha256",
        "api_oci_digest",
        "web_oci_digest",
        "postgres_oci_digest",
        "redis_oci_digest",
    )
    if any(identity[field] != candidate[field] for field in bound_fields) or identity[
        "runtime_file_inventory"
    ] != candidate["candidate_runtime_file_inventory"]:
        _reject("VERIFIED_CANDIDATE_INPUT_BINDING_MISMATCH")


def _verify_qualification_intrinsics(
    root: Path,
    candidate: Mapping[str, Any],
) -> None:
    qualification_path = root / (
        f"release-qualification-{candidate['qualification_run_id']}.json"
    )
    qualification, qualification_bytes = _strict_json_file(
        qualification_path,
        code="CANDIDATE_QUALIFICATION_EVIDENCE_INVALID",
    )
    production_receipt, production_receipt_bytes = _strict_json_file(
        root / CANDIDATE_PRODUCTION_RECEIPT_NAME,
        code="CANDIDATE_PRODUCTION_RECEIPT_INVALID",
    )
    notes, _ = _strict_json_file(
        root / "release-notes.json",
        code="CANDIDATE_RELEASE_NOTES_INVALID",
    )
    try:
        from scripts.release_qualification import (
            QualificationError,
            validate_qualification_evidence,
        )
    except ImportError as error:
        raise CandidateContractError(
            "CANDIDATE_QUALIFICATION_VERIFIER_UNAVAILABLE"
        ) from error
    try:
        validated = validate_qualification_evidence(
            qualification,
            expected={
                "repository": REPOSITORY,
                "qualification_run_id": candidate["qualification_run_id"],
                "candidate_sha": candidate["source_sha"],
                "candidate_tree": candidate["source_tree"],
                "channel": "rc",
                "target_version": candidate["target_version"],
                "release_tag": candidate["candidate_version"],
                "workflow_ref": candidate["qualification_workflow_identity"]["ref"],
                "workflow_sha": candidate["source_sha"],
                "release_notes_identity": notes["identity"],
                "release_notes_markdown_sha256": candidate[
                    "release_notes_markdown_sha256"
                ],
            },
        )
    except (KeyError, QualificationError) as error:
        raise CandidateContractError(
            "CANDIDATE_QUALIFICATION_EVIDENCE_INVALID"
        ) from error
    canonical_qualification = (
        json.dumps(
            validated,
            ensure_ascii=False,
            allow_nan=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
    if (
        qualification_bytes != canonical_qualification
        or validated["run"]["attempt"]
        != candidate["qualification_run_attempt"]
        or validated["workflow"] != candidate["qualification_workflow_identity"]
    ):
        _reject("CANDIDATE_QUALIFICATION_EVIDENCE_MISMATCH")
    provisional = validated["provisional_artifact"]
    if (
        provisional["id"]
        != candidate["qualification_artifact_ids"]["release_dry_run"]
        or provisional["api_digest"]
        != candidate["qualification_artifact_api_digests"]["release_dry_run"]
        or provisional["archive_sha256"] != provisional["api_digest"]
        or sha256_bytes(production_receipt_bytes)
        != validated["candidate_production_receipt_sha256"]
    ):
        _reject("CANDIDATE_PRODUCTION_RECEIPT_BINDING_MISMATCH")
    receipt_identity = {
        "repository": validated["repository"],
        "workflow_ref": validated["workflow"]["ref"],
        "workflow_sha": validated["workflow"]["sha"],
        "run_id": validated["run"]["id"],
        "run_attempt": validated["run"]["attempt"],
        "event": validated["run"]["event"],
        "candidate_sha": validated["candidate_sha"],
        "candidate_tree": validated["candidate_tree"],
        "target_version": validated["target_version"],
        "release_tag": validated["release_tag"],
        "channel": validated["channel"],
    }
    try:
        validate_candidate_production_receipt(
            production_receipt,
            root=root,
            identity=receipt_identity,
        )
    except MaterialContractError as error:
        raise CandidateContractError(
            "CANDIDATE_PRODUCTION_RECEIPT_INVALID"
        ) from error

    workflow_ref = candidate["qualification_workflow_identity"]["ref"]
    platform_ref = workflow_ref.partition("@")[2]
    if not platform_ref:
        _reject("CANDIDATE_QUALIFICATION_EVIDENCE_MISMATCH")
    platform_bytes = _read_regular_file(
        root / "platform-qualification.json",
        maximum=MAX_JSON_BYTES,
    )
    try:
        platform = parse_platform_qualification(platform_bytes)
    except PlatformQualificationError as error:
        raise CandidateContractError(
            "CANDIDATE_PLATFORM_QUALIFICATION_INVALID"
        ) from error
    if (
        platform_bytes != canonical_platform_qualification_bytes(platform)
        or platform.candidate_sha != candidate["source_sha"]
        or dict(platform.workflow) != {
            "path": QUALIFICATION_WORKFLOW_PATH,
            "ref": platform_ref,
            "sha": candidate["source_sha"],
        }
        or dict(platform.run) != {
            "id": str(candidate["qualification_run_id"]),
            "attempt": candidate["qualification_run_attempt"],
        }
        or dict(platform.image_digests) != {
            "postgres": f"{POSTGRES_REPOSITORY}@{POSTGRES_DIGEST}",
            "redis": f"{REDIS_REPOSITORY}@{REDIS_DIGEST}",
        }
    ):
        _reject("CANDIDATE_PLATFORM_QUALIFICATION_MISMATCH")


def _verify_embedded_platform_qualification(root: Path) -> None:
    platform_bytes = _read_regular_file(
        root / "platform-qualification.json",
        maximum=MAX_JSON_BYTES,
    )
    embedded_platform_bytes = _read_regular_file(
        root / "installer-root" / "release" / "platform-qualification.json",
        maximum=MAX_JSON_BYTES,
    )
    if platform_bytes != embedded_platform_bytes:
        _reject("CANDIDATE_PLATFORM_QUALIFICATION_MISMATCH")


def build_prepublication_controller_authority(
    *,
    archive: Path,
    containing_artifact_id: int,
    containing_artifact_api_digest: str,
    output: Path,
    _maximum_archive_bytes: int = MAX_ARCHIVE_BYTES,
    _maximum_expanded_bytes: int = MAX_ARCHIVE_BYTES,
) -> dict[str, Any]:
    if (
        type(containing_artifact_id) is not int
        or containing_artifact_id < 1
        or _DIGEST.fullmatch(containing_artifact_api_digest) is None
    ):
        _reject("CONTROLLER_AUTHORITY_ARTIFACT_BINDING_INVALID")
    archive = Path(archive)
    output = Path(output)
    if (
        output.is_absolute()
        or output.drive
        or len(output.parts) != 1
        or output.name in {"", ".", ".."}
    ):
        _reject("CONTROLLER_AUTHORITY_OUTPUT_INVALID")
    trusted_root = os.path.abspath(os.getcwd())
    normalized_output = os.path.abspath(
        os.path.join(trusted_root, os.fspath(output))
    )
    if (
        not normalized_output.startswith(trusted_root + os.sep)
        or os.path.dirname(normalized_output) != trusted_root
    ):
        _reject("CONTROLLER_AUTHORITY_OUTPUT_INVALID")
    with tempfile.TemporaryDirectory(
        prefix="animemo-controller-authority-"
    ) as temporary:
        root = Path(temporary) / "qualification"
        with _open_regular_file(archive, maximum=_maximum_archive_bytes) as source:
            artifact_hash = hashlib.sha256()
            artifact_size = 0
            while chunk := source.read(1024 * 1024):
                artifact_size += len(chunk)
                if artifact_size > _maximum_archive_bytes:
                    _reject("CANDIDATE_ARCHIVE_SIZE_LIMIT")
                artifact_hash.update(chunk)
            archive_digest = "sha256:" + artifact_hash.hexdigest()
            if archive_digest != containing_artifact_api_digest:
                _reject("CANDIDATE_ARTIFACT_API_DIGEST_MISMATCH")
            source.seek(0)
            candidate, candidate_digest, file_count = (
                _extract_candidate_archive_stream(
                    source,
                    root,
                    maximum_total=_maximum_expanded_bytes,
                )
            )
        candidate_bytes = _read_regular_file(
            root / "candidate-input.json", maximum=MAX_JSON_BYTES
        )
        if sha256_bytes(candidate_bytes) != candidate_digest:
            _reject("CANDIDATE_INPUT_DIGEST_MISMATCH")
        candidate = validate_candidate_input(candidate, root=root)
        manifest = _strict_json_file(
            root / "release-manifest.json", code="CANDIDATE_MANIFEST_INVALID"
        )[0]
        runtime = _verify_runtime(root, manifest)
        deployment = _strict_json_file(
            root / "deployment-contract.json", code="CANDIDATE_DEPLOYMENT_INVALID"
        )[0]
        material_contract = {
            "schemaVersion": deployment["schemaVersion"],
            "profile": deployment["profile"],
            "platform": deployment["platform"],
            "archive": deployment["archive"],
            "materials": deployment["materials"],
        }
        extract_installer_materials(
            root / "installer-materials.tar",
            material_contract,
            root / "installer-root",
        )
        _verify_embedded_platform_qualification(root)
        verified = _build_verified_candidate_identity(
            candidate=candidate,
            candidate_digest=candidate_digest,
            containing_artifact_id=containing_artifact_id,
            containing_artifact_api_digest=containing_artifact_api_digest,
            archive_digest=archive_digest,
            archive_file_count=file_count,
            runtime=runtime,
        )
    verified_bytes = canonical_json_bytes(verified)
    output = Path(normalized_output)
    if output.exists() or output.is_symlink():
        _reject("CONTROLLER_AUTHORITY_OUTPUT_EXISTS")
    try:
        output.mkdir(mode=0o700)
        created = output.lstat()
        if (
            output.is_symlink()
            or bool(getattr(output, "is_junction", lambda: False)())
            or not stat.S_ISDIR(created.st_mode)
            or (os.name == "posix" and created.st_uid != os.geteuid())
        ):
            _reject("CONTROLLER_AUTHORITY_OUTPUT_INVALID")
        for name, encoded in (
            ("candidate-input.json", candidate_bytes),
            ("verified-candidate.json", verified_bytes),
        ):
            with AtomicReleaseFile(output / name) as atomic:
                assert atomic.created is not None
                with atomic.open_stream() as stream:
                    stream.write(encoded)
                    stream.flush()
                    os.fsync(stream.fileno())
                    completed = os.fstat(stream.fileno())
                    if (
                        completed.st_nlink != 1
                        or (
                            completed.st_dev,
                            completed.st_ino,
                            completed.st_mode,
                        )
                        != (
                            atomic.created.st_dev,
                            atomic.created.st_ino,
                            atomic.created.st_mode,
                        )
                    ):
                        _reject("CONTROLLER_AUTHORITY_OUTPUT_CHANGED")
                    atomic.publish(stream, completed)
        final = output.lstat()
        if (
            final.st_dev != created.st_dev
            or final.st_ino != created.st_ino
            or final.st_mode != created.st_mode
            or {item.name for item in output.iterdir()}
            != {"candidate-input.json", "verified-candidate.json"}
        ):
            _reject("CONTROLLER_AUTHORITY_OUTPUT_CHANGED")
        for name, encoded in (
            ("candidate-input.json", candidate_bytes),
            ("verified-candidate.json", verified_bytes),
        ):
            if _read_regular_file(output / name, maximum=MAX_JSON_BYTES) != encoded:
                _reject("CONTROLLER_AUTHORITY_OUTPUT_CHANGED")
    except FileExistsError as error:
        raise CandidateContractError("CONTROLLER_AUTHORITY_OUTPUT_EXISTS") from error
    except MaterialContractError as error:
        raise CandidateContractError("CONTROLLER_AUTHORITY_OUTPUT_CHANGED") from error
    except OSError as error:
        raise CandidateContractError("CONTROLLER_AUTHORITY_OUTPUT_INVALID") from error
    return {
        "status": "PASS",
        "containingArtifactId": containing_artifact_id,
        "containingArtifactApiDigest": containing_artifact_api_digest,
        "candidateInputDigest": candidate_digest,
        "verifiedCandidateDigest": sha256_bytes(verified_bytes),
        "archiveFileCount": file_count,
        "authorityFileCount": 2,
    }


def build_prepublication_controller_authority_from_stream(
    *,
    source: BinaryIO,
    expected_archive_size: int,
    containing_artifact_id: int,
    containing_artifact_api_digest: str,
    output: Path,
) -> dict[str, Any]:
    if (
        type(expected_archive_size) is not int
        or expected_archive_size < 1
        or expected_archive_size > MAX_CONTROLLER_ARCHIVE_BYTES
    ):
        _reject("CONTROLLER_AUTHORITY_ARCHIVE_SIZE_INVALID")
    with tempfile.TemporaryDirectory(
        prefix="animemo-controller-download-"
    ) as temporary:
        temporary_root = Path(temporary)
        free_bytes = shutil.disk_usage(temporary_root).free
        expanded_budget = min(
            MAX_CONTROLLER_EXPANDED_BYTES,
            free_bytes
            - expected_archive_size
            - MAX_MATERIAL_TOTAL_BYTES
            - MIN_CONTROLLER_DISK_RESERVE_BYTES,
        )
        if expanded_budget < 1:
            _reject("CONTROLLER_AUTHORITY_DISK_BUDGET_INSUFFICIENT")
        archive = temporary_root / "qualification.zip"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_BINARY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = -1
        try:
            descriptor = os.open(archive, flags, 0o600)
            written = 0
            digest = hashlib.sha256()
            with os.fdopen(descriptor, "wb") as destination:
                descriptor = -1
                while chunk := source.read(1024 * 1024):
                    written += len(chunk)
                    if written > expected_archive_size:
                        _reject("CONTROLLER_AUTHORITY_ARCHIVE_STREAM_GREW")
                    digest.update(chunk)
                    destination.write(chunk)
                destination.flush()
                os.fsync(destination.fileno())
            if written != expected_archive_size:
                _reject("CONTROLLER_AUTHORITY_ARCHIVE_STREAM_TRUNCATED")
            if "sha256:" + digest.hexdigest() != containing_artifact_api_digest:
                _reject("CANDIDATE_ARTIFACT_API_DIGEST_MISMATCH")
        except CandidateContractError:
            raise
        except OSError as error:
            raise CandidateContractError(
                "CONTROLLER_AUTHORITY_ARCHIVE_STREAM_INVALID"
            ) from error
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        return build_prepublication_controller_authority(
            archive=archive,
            containing_artifact_id=containing_artifact_id,
            containing_artifact_api_digest=containing_artifact_api_digest,
            output=output,
            _maximum_archive_bytes=MAX_CONTROLLER_ARCHIVE_BYTES,
            _maximum_expanded_bytes=expanded_budget,
        )


def verify_prepublication_candidate(
    *,
    archive: Path,
    run_metadata: Mapping[str, Any],
    jobs_metadata: Mapping[str, Any],
    artifacts_metadata: Mapping[str, Any],
    containing_artifact_id: int,
    containing_artifact_api_digest: str,
    expected_run_id: int,
    expected_source_sha: str,
    expected_source_tree: str,
    expected_candidate_version: str,
    verified_at: str,
    _state_root: Path | None = None,
) -> dict[str, Any]:
    if not _DIGEST.fullmatch(containing_artifact_api_digest):
        _reject("CANDIDATE_ARTIFACT_DIGEST_INVALID")
    _canonical_utc_time(verified_at, code="CANDIDATE_VERIFICATION_TIME_INVALID")
    selected = validate_qualification_run_metadata(
        run_metadata=run_metadata,
        jobs_metadata=jobs_metadata,
        artifacts_metadata=artifacts_metadata,
        expected_run_id=expected_run_id,
        expected_sha=expected_source_sha,
    )
    if (
        selected.get("artifactId") != containing_artifact_id
        or selected.get("digest") != containing_artifact_api_digest
    ):
        _reject("CANDIDATE_CONTAINING_ARTIFACT_MISMATCH")
    state_root = Path(_state_root or VERIFIED_CANDIDATE_ROOT)
    state_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    root_metadata = state_root.lstat()
    if (
        state_root.is_symlink()
        or bool(getattr(state_root, "is_junction", lambda: False)())
        or not stat.S_ISDIR(root_metadata.st_mode)
        or (os.name == "posix" and root_metadata.st_uid != os.geteuid())
        or (os.name == "posix" and root_metadata.st_mode & 0o077)
    ):
        _reject("VERIFIED_CANDIDATE_ROOT_INVALID")
    staging: Path | None = Path(
        tempfile.mkdtemp(prefix=".candidate-", dir=state_root)
    )
    try:
        with _open_regular_file(archive, maximum=MAX_ARCHIVE_BYTES) as source:
            archive_hash = hashlib.sha256()
            archive_size = 0
            while chunk := source.read(1024 * 1024):
                archive_size += len(chunk)
                if archive_size > MAX_ARCHIVE_BYTES:
                    _reject("CANDIDATE_ARCHIVE_SIZE_LIMIT")
                archive_hash.update(chunk)
            archive_digest = "sha256:" + archive_hash.hexdigest()
            if archive_digest != containing_artifact_api_digest:
                _reject("CANDIDATE_ARTIFACT_API_DIGEST_MISMATCH")
            source.seek(0)
            candidate, candidate_digest, file_count = _extract_candidate_archive_stream(
                source, staging
            )
        if (
            candidate["repository"] != REPOSITORY
            or candidate["qualification_run_id"] != expected_run_id
            or candidate["qualification_run_attempt"] != 1
            or candidate["source_sha"] != expected_source_sha
            or candidate["source_tree"] != expected_source_tree
            or candidate["candidate_version"] != expected_candidate_version
        ):
            _reject("CANDIDATE_EXPECTATION_MISMATCH")
        validate_candidate_input(candidate, root=staging)
        _validate_bound_artifacts(candidate, artifacts_metadata)
        runtime = _verify_runtime(staging, _strict_json_file(
            staging / "release-manifest.json", code="CANDIDATE_MANIFEST_INVALID"
        )[0])
        deployment = _strict_json_file(
            staging / "deployment-contract.json", code="CANDIDATE_DEPLOYMENT_INVALID"
        )[0]
        material_contract = {
            "schemaVersion": deployment["schemaVersion"],
            "profile": deployment["profile"],
            "platform": deployment["platform"],
            "archive": deployment["archive"],
            "materials": deployment["materials"],
        }
        extract_installer_materials(
            staging / "installer-materials.tar",
            material_contract,
            staging / "installer-root",
        )
        _verify_embedded_platform_qualification(staging)
        verified = _build_verified_candidate_identity(
            candidate=candidate,
            candidate_digest=candidate_digest,
            containing_artifact_id=containing_artifact_id,
            containing_artifact_api_digest=containing_artifact_api_digest,
            archive_digest=archive_digest,
            archive_file_count=file_count,
            runtime=runtime,
        )
        encoded = canonical_json_bytes(verified)
        verified_digest = sha256_bytes(encoded)
        receipt = _build_verification_execution_receipt(
            identity=verified,
            identity_digest=verified_digest,
            verified_at=verified_at,
        )
        receipt_encoded = canonical_json_bytes(receipt)
        receipt_digest = sha256_bytes(receipt_encoded)
        with (staging / "verified-candidate.json").open("xb") as output:
            output.write(encoded)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(staging / "verified-candidate.json", 0o600)
        target = state_root / candidate_digest.removeprefix("sha256:")

        def accept_existing_target() -> dict[str, Any]:
            try:
                existing = load_verified_candidate(
                    verified_digest,
                    _state_root=state_root,
                )
            except CandidateContractError as error:
                raise CandidateContractError(
                    "VERIFIED_CANDIDATE_OUTPUT_CONFLICT"
                ) from error
            if existing.verified["candidate_input_sha256"] != candidate_digest:
                _reject("VERIFIED_CANDIDATE_OUTPUT_CONFLICT")
            receipt_existing = _write_append_only_receipt(
                target, receipt_encoded, receipt_digest
            )
            return {
                "status": "PASS",
                "candidateInputDigest": candidate_digest,
                "verifiedCandidateDigest": verified_digest,
                "verifiedCandidateIdentityDigest": verified_digest,
                "verificationExecutionReceiptDigest": receipt_digest,
                "verificationExecutionReceiptContentDigest": receipt[
                    "receipt_digest"
                ],
                "verificationExecutionReceiptExisting": receipt_existing,
                "existing": True,
            }

        if target.exists() or target.is_symlink():
            shutil.rmtree(staging)
            staging = None
            return accept_existing_target()
        _write_append_only_receipt(staging, receipt_encoded, receipt_digest)
        try:
            os.replace(staging, target)
        except OSError as error:
            if not target.exists() and not target.is_symlink():
                raise CandidateContractError(
                    "VERIFIED_CANDIDATE_OUTPUT_UNAVAILABLE"
                ) from error
            shutil.rmtree(staging)
            staging = None
            return accept_existing_target()
        staging = None
        return {
            "status": "PASS",
            "candidateInputDigest": candidate_digest,
            "verifiedCandidateDigest": verified_digest,
            "verifiedCandidateIdentityDigest": verified_digest,
            "verificationExecutionReceiptDigest": receipt_digest,
            "verificationExecutionReceiptContentDigest": receipt["receipt_digest"],
            "verificationExecutionReceiptExisting": False,
            "existing": False,
        }
    except BaseException:
        if staging is not None and staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        raise


@dataclass(frozen=True)
class LoadedVerifiedCandidate:
    root: Path
    verified_digest: str
    verified: Mapping[str, Any]
    candidate_input: Mapping[str, Any]
    manifest: Mapping[str, Any]
    deployment_contract: Mapping[str, Any]
    materials: VerifiedReleaseMaterials
    images: VerifiedOCIImageSet


def _locate_verified_candidate_root(state_root: Path, digest: str) -> Path:
    try:
        metadata = state_root.lstat()
        entries = list(state_root.iterdir())
    except OSError as error:
        raise CandidateContractError("VERIFIED_CANDIDATE_ROOT_UNAVAILABLE") from error
    if (
        state_root.is_symlink()
        or bool(getattr(state_root, "is_junction", lambda: False)())
        or not stat.S_ISDIR(metadata.st_mode)
        or len(entries) > 256
        or (os.name == "posix" and metadata.st_uid != os.geteuid())
        or (os.name == "posix" and metadata.st_mode & 0o077)
    ):
        _reject("VERIFIED_CANDIDATE_ROOT_INVALID")
    matches: list[Path] = []
    for entry in sorted(entries, key=lambda item: item.name):
        if entry.name.startswith(".candidate-"):
            continue
        if re.fullmatch(r"[0-9a-f]{64}", entry.name) is None:
            _reject("VERIFIED_CANDIDATE_ROOT_INVALID")
        try:
            entry_metadata = entry.lstat()
        except OSError as error:
            raise CandidateContractError("VERIFIED_CANDIDATE_ROOT_INVALID") from error
        if (
            entry.is_symlink()
            or bool(getattr(entry, "is_junction", lambda: False)())
            or not stat.S_ISDIR(entry_metadata.st_mode)
            or (os.name == "posix" and entry_metadata.st_uid != os.geteuid())
            or (os.name == "posix" and entry_metadata.st_mode & 0o077)
        ):
            _reject("VERIFIED_CANDIDATE_ROOT_INVALID")
        try:
            observed, _ = _sha256_file(
                entry / "verified-candidate.json",
                maximum=MAX_JSON_BYTES,
            )
        except CandidateContractError as error:
            raise CandidateContractError("VERIFIED_CANDIDATE_ROOT_INVALID") from error
        if observed == digest:
            matches.append(entry)
    if len(matches) != 1:
        _reject("VERIFIED_CANDIDATE_ROOT_UNAVAILABLE")
    return matches[0]


def load_verified_candidate(
    digest: str,
    *,
    _state_root: Path | None = None,
) -> LoadedVerifiedCandidate:
    if not _DIGEST.fullmatch(digest):
        _reject("VERIFIED_CANDIDATE_DIGEST_INVALID")
    state_root = Path(_state_root or VERIFIED_CANDIDATE_ROOT)
    root = _locate_verified_candidate_root(state_root, digest)
    verified, verified_bytes = _strict_json_file(
        root / "verified-candidate.json", code="VERIFIED_CANDIDATE_INVALID"
    )
    if sha256_bytes(verified_bytes) != digest:
        _reject("VERIFIED_CANDIDATE_DIGEST_MISMATCH")
    validate_verified_candidate(verified)
    if verified_bytes != canonical_json_bytes(verified):
        _reject("VERIFIED_CANDIDATE_JSON_NON_CANONICAL")
    candidate, candidate_bytes = _strict_json_file(
        root / "candidate-input.json", code="CANDIDATE_INPUT_INVALID"
    )
    if sha256_bytes(candidate_bytes) != verified["candidate_input_sha256"]:
        _reject("CANDIDATE_INPUT_DIGEST_MISMATCH")
    if root.name != verified["candidate_input_sha256"].removeprefix("sha256:"):
        _reject("CANDIDATE_INPUT_ROOT_IDENTITY_MISMATCH")
    validate_candidate_input(candidate, root=root)
    _validate_identity_candidate_binding(verified, candidate)
    manifest = _strict_json_file(
        root / "release-manifest.json", code="CANDIDATE_MANIFEST_INVALID"
    )[0]
    deployment = _strict_json_file(
        root / "deployment-contract.json", code="CANDIDATE_DEPLOYMENT_INVALID"
    )[0]
    material_contract = {
        "schemaVersion": deployment["schemaVersion"],
        "profile": deployment["profile"],
        "platform": deployment["platform"],
        "archive": deployment["archive"],
        "materials": deployment["materials"],
    }
    archive_identity, material_files = validate_material_contract(material_contract)
    verified_materials = VerifiedMaterialSet(
        root=root / "installer-root",
        archive_sha256=archive_identity["sha256"],
        files=material_files,
    )
    for identity in material_files:
        verified_materials.material(identity.path)
    images = _verify_runtime(root, manifest)
    observed_oci = [
        {
            "role": image.role,
            "repository": image.repository,
            "digest": image.digest,
            "platform": image.platform,
            "config_digest": image.config_digest,
            "layer_digests": list(image.layer_digests),
            "result": "PASS",
        }
        for image in sorted(images.images, key=lambda item: item.role)
    ]
    if observed_oci != verified["oci_verification"]:
        _reject("VERIFIED_CANDIDATE_OCI_BINDING_MISMATCH")
    materials = VerifiedReleaseMaterials(
        manifest=dict(manifest),
        deployment_contract=dict(deployment),
        verified=verified_materials,
        identity_digest=verified["candidate_input_sha256"],
    )
    return LoadedVerifiedCandidate(
        root=root,
        verified_digest=digest,
        verified=verified,
        candidate_input=candidate,
        manifest=manifest,
        deployment_contract=deployment,
        materials=materials,
        images=images,
    )


def validate_profile_receipt(value: object) -> dict[str, Any]:
    receipt = _validate_schema(
        value,
        "prepublication-candidate-profile-receipt.schema.json",
        code="CANDIDATE_PROFILE_RECEIPT_INVALID",
    )
    started = _parse_time(receipt["started_at"], code="CANDIDATE_RECEIPT_TIME_INVALID")
    completed = _parse_time(receipt["completed_at"], code="CANDIDATE_RECEIPT_TIME_INVALID")
    tests = receipt["canonical_acceptance_tests"]
    test_names = [item["name"] for item in tests]
    steps = receipt["completed_steps"]
    pulls = receipt["external_pull_observation"]
    if completed < started or receipt["original_vm_pre_hashes"] != receipt["original_vm_post_hashes"]:
        _reject("CANDIDATE_PROFILE_SAFETY_MISMATCH")
    if len(test_names) != len(set(test_names)):
        _reject("CANDIDATE_PROFILE_CANONICAL_TEST_DUPLICATE")
    if test_names != [
        "application.journal-crud",
        "service.api.health",
        "service.web.health",
    ]:
        _reject("CANDIDATE_PROFILE_CANONICAL_TEST_INVENTORY_MISMATCH")
    if not steps or steps[-1] != "doctor.accept":
        _reject("CANDIDATE_PROFILE_COMPLETED_STEPS_MISMATCH")
    if pulls["observed_count"] != len(pulls["inventory"]) or pulls["result"] != "PASS":
        _reject("CANDIDATE_PROFILE_EXTERNAL_PULL_OBSERVATION_MISMATCH")
    if pulls["inventory"]:
        _reject("CANDIDATE_PROFILE_EXTERNAL_PULL_ACTIVITY")
    network = receipt["network_observation"]
    egress = network["egress_isolation"]
    egress_body = {
        "authority": egress["authority"],
        "containerNetwork": egress["container_network"],
        "containerNetworkInternal": egress["container_network_internal"],
        "service": egress["service"],
        "serviceAddressFamilies": egress["service_address_families"],
    }
    expected_policy = (
        "DENY_ALL"
        if receipt["profile"] == "RUNTIME_BASE_OFFLINE"
        else "APT_UBUNTU_ARCHIVE_ONLY"
    )
    completed_commands = network["completed_commands"]
    runtime_commands = [
        item for item in completed_commands if item["boundary"] == "RUNTIME"
    ]
    pull_denied_digests = sorted(
        item["argv_digest"]
        for item in runtime_commands
        if item["external_pull_disposition"] == "EXPLICIT_NEVER"
    )
    observed_network_commands = [
        (item["argv_digest"], item["return_code"])
        for item in completed_commands
        if item["classification"] == "APT_NETWORK"
    ]
    if (
        network["authority"]
        != "PRODUCTION_EXECUTION_WITH_OS_EGRESS_ISOLATION"
        or egress["authority"] != "OS_ENFORCED_CANDIDATE_EGRESS_ISOLATION"
        or egress["container_network_internal"] is not True
        or egress["service_address_families"] != ["AF_UNIX", "AF_NETLINK"]
        or egress["receipt_digest"]
        != sha256_bytes(canonical_identity_bytes(egress_body))
        or network["policy"] != expected_policy
        or network["destination_authority"]
        != (
            "NONE"
            if expected_policy == "DENY_ALL"
            else "UBUNTU_ARCHIVE_VERIFIED_APT_SOURCES"
        )
        or network["platform_plan_digest"]
        != receipt["platform_bootstrap_plan_digest"]
        or network["result"] != "PASS"
        or network["completed_command_inventory_digest"]
        != sha256_bytes(canonical_json_bytes(completed_commands))
        or network["observer_identities"]
        != {
            "platform": _CANDIDATE_COMMAND_OBSERVER_IDENTITY,
            "runtime": _CANDIDATE_COMMAND_OBSERVER_IDENTITY,
        }
        or not apt_network_sequence_matches(
            observed_network_commands,
            expected_digests=network["expected_network_command_digests"],
            retryable_digests=network["retryable_network_command_digests"],
        )
        or expected_policy == "DENY_ALL"
        and observed_network_commands
        or any(
            item["operation"]
            in {"docker-compose-run", "docker-compose-up", "docker-run"}
            and item["external_pull_disposition"] != "EXPLICIT_NEVER"
            or item["operation"]
            not in {"docker-compose-run", "docker-compose-up", "docker-run"}
            and item["external_pull_disposition"] != "NOT_APPLICABLE"
            for item in completed_commands
        )
        or pulls["observer_identity"] != _CANDIDATE_COMMAND_OBSERVER_IDENTITY
        or pulls["pull_denied_command_digests"] != pull_denied_digests
        or pulls["runtime_command_inventory_digest"]
        != sha256_bytes(canonical_json_bytes(runtime_commands))
    ):
        _reject("CANDIDATE_PROFILE_NETWORK_OBSERVATION_INVALID")
    expected_pass = (
        receipt["installer_execution_result"] == "PASS"
        and all(item["result"] == "PASS" for item in tests)
    )
    if (receipt["result"] == "PASS") is not expected_pass:
        _reject("CANDIDATE_PROFILE_RESULT_MISMATCH")
    return receipt


def validate_aggregate_receipt(value: object) -> dict[str, Any]:
    receipt = _validate_schema(
        value,
        "prepublication-candidate-acceptance-receipt.schema.json",
        code="CANDIDATE_ACCEPTANCE_RECEIPT_INVALID",
    )
    _parse_time(receipt["completed_at"], code="CANDIDATE_RECEIPT_TIME_INVALID")
    if receipt["candidate_prestate"] != receipt["candidate_poststate"]:
        _reject("CANDIDATE_PUBLICATION_STATE_CHANGED")
    if (
        receipt["r2_origin_prestate_receipt_digest"]
        == receipt["r2_origin_poststate_receipt_digest"]
        or receipt["r2_origin_prestate_observation_id"]
        == receipt["r2_origin_poststate_observation_id"]
    ):
        _reject("CANDIDATE_R2_OBSERVATION_REUSED")
    profile_results = receipt["profile_results"]
    original_vm_hashes = receipt["original_vm_hashes"]
    if receipt["base_vm_identity"] != sha256_bytes(
        canonical_json_bytes(dict(sorted(original_vm_hashes.items())))
    ):
        _reject("CANDIDATE_SOURCE_VM_AUTHORITY_MISMATCH")
    digests = [
        result["receipt_digest"]
        for result in profile_results.values()
        if result["receipt_digest"] is not None
    ]
    if len(digests) != len(set(digests)):
        _reject("CANDIDATE_PROFILE_RECEIPT_REUSE")
    expected_pass = all(
        result["status"] == "PASS" for result in profile_results.values()
    )
    if (
        receipt["all_profiles_pass"] is not expected_pass
        or (receipt["result"] == "PASS") is not expected_pass
    ):
        _reject("CANDIDATE_AGGREGATE_RESULT_MISMATCH")
    unsigned = dict(receipt)
    unsigned.pop("receipt_digest")
    if receipt["receipt_digest"] != sha256_bytes(canonical_json_bytes(unsigned)):
        _reject("CANDIDATE_ACCEPTANCE_RECEIPT_DIGEST_MISMATCH")
    return receipt


def aggregate_receipt_digest(value: object) -> str:
    return sha256_bytes(canonical_json_bytes(validate_aggregate_receipt(value)))


def decode_aggregate_receipt_b64url(value: str) -> tuple[dict[str, Any], bytes]:
    if (
        type(value) is not str
        or not value
        or len(value) > MAX_RECEIPT_B64URL_BYTES
        or "=" in value
        or re.fullmatch(r"[A-Za-z0-9_-]+", value) is None
    ):
        _reject("CANDIDATE_RECEIPT_B64URL_INVALID")
    padding = "=" * (-len(value) % 4)
    try:
        decoded = base64.urlsafe_b64decode((value + padding).encode("ascii"))
    except (ValueError, UnicodeEncodeError) as error:
        raise CandidateContractError("CANDIDATE_RECEIPT_B64URL_INVALID") from error
    if len(decoded) > MAX_JSON_BYTES:
        _reject("CANDIDATE_RECEIPT_SIZE_LIMIT")
    canonical = base64.urlsafe_b64encode(decoded).decode("ascii").rstrip("=")
    if canonical != value:
        _reject("CANDIDATE_RECEIPT_B64URL_NON_CANONICAL")
    receipt = validate_aggregate_receipt(
        _strict_json_bytes(decoded, code="CANDIDATE_ACCEPTANCE_RECEIPT_INVALID")
    )
    encoded = canonical_json_bytes(receipt)
    if encoded != decoded:
        _reject("CANDIDATE_RECEIPT_JSON_NON_CANONICAL")
    return receipt, encoded


__all__ = [
    "AGGREGATE_RECEIPT_SCHEMA",
    "CANDIDATE_INPUT_SCHEMA",
    "INSTALLER_PROFILES",
    "PROFILE_RECEIPT_SCHEMA",
    "VERIFICATION_EXECUTION_RECEIPT_SCHEMA",
    "VERIFIED_CANDIDATE_ROOT",
    "VERIFIED_CANDIDATE_SCHEMA",
    "CandidateContractError",
    "LoadedVerifiedCandidate",
    "aggregate_receipt_digest",
    "build_candidate_input",
    "canonical_json_bytes",
    "decode_aggregate_receipt_b64url",
    "extract_candidate_oci_archive",
    "load_verified_candidate",
    "normalize_candidate_oci_layout",
    "sha256_bytes",
    "validate_aggregate_receipt",
    "validate_candidate_input",
    "validate_profile_receipt",
    "validate_verification_execution_receipt",
    "validate_verified_candidate",
    "verify_prepublication_candidate",
]
