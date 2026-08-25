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
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO, Protocol

from jsonschema import Draft202012Validator, FormatChecker

from durability.platform import (
    PlatformQualificationError,
    canonical_platform_qualification_bytes,
    parse_platform_qualification,
)
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
    VerifiedMaterialSet,
    extract_installer_materials,
    reject_duplicate_json_keys,
    validate_material_contract,
    verify_prepublication_material_identity,
)
from .metadata_freshness import validate_qualification_run_metadata
from .notes import render_release_notes, validate_release_notes

REPOSITORY = "yanyuhanyue/AniMemo"
QUALIFICATION_WORKFLOW_NAME = "Release Producer"
QUALIFICATION_WORKFLOW_PATH = ".github/workflows/release.yml"
QUALIFICATION_WORKFLOW_REF = (
    "yanyuhanyue/AniMemo/.github/workflows/release.yml@refs/heads/main"
)
CANDIDATE_INPUT_SCHEMA = "animemo.prepublication-candidate-input/v1"
VERIFIED_CANDIDATE_SCHEMA = "animemo.verified-prepublication-candidate/v1"
PROFILE_RECEIPT_SCHEMA = "animemo.prepublication-candidate-profile-receipt/v1"
AGGREGATE_RECEIPT_SCHEMA = (
    "animemo.prepublication-candidate-acceptance-receipt/v1"
)
VERIFIER_VERSION = "1"
VERIFIED_CANDIDATE_ROOT = Path("/var/lib/animemo/prepublication-candidates/v1")
CANDIDATE_RUNTIME_ROOT = "candidate-runtime"
OCI_ROLES = ("api", "postgres", "redis", "web")
PROFILE_ROLES = ("FRESH_BASE", "DOCKER_BASE", "RUNTIME_BASE_OFFLINE")
INSTALLER_PROFILES = (
    "ONLINE_FRESH",
    "ONLINE_EXISTING_DOCKER",
    "OFFLINE_VALIDATE_ONLY",
)

R2_ACCOUNT_ID_SHA256 = (
    "sha256:c5afddc36ea670626be71b625029128a3381d836807378da8eada702bef541e1"
)
R2_BUCKET = "animemo-release-mirror"
R2_RC14_PREFIX = "yanyuhanyue/AniMemo/releases/download/v1.1.0-rc.14/"
R2_RC14_EXPECTED_KEYS = (
    "animemo-v1.1.0-rc.14-portable.tar",
    "checksums.txt",
    "deployment-contract.json",
    "installer-materials.tar",
    "mirror-receipt.json",
    "release-manifest.json",
)

MAX_ARCHIVE_BYTES = 16 * 1024 * 1024 * 1024
MAX_ARCHIVE_MEMBER_BYTES = 4 * 1024 * 1024 * 1024
MAX_ARCHIVE_FILE_COUNT = 1100
MAX_RUNTIME_BYTES = 16 * 1024 * 1024 * 1024
MAX_JSON_BYTES = 16 * 1024 * 1024
MAX_RECEIPT_B64URL_BYTES = 512 * 1024
MAX_R2_RESPONSE_BYTES = 4 * 1024 * 1024
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_GIT_SHA = re.compile(r"[0-9a-f]{40}\Z")
_RC = re.compile(
    r"(?P<target>v(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\."
    r"(?:0|[1-9][0-9]*))-rc\.(?P<sequence>[1-9][0-9]*)\Z"
)
_WINDOWS_DRIVE = re.compile(r"[A-Za-z]:")

_QUALIFICATION_ROOT_FILES = frozenset(
    {
        "candidate-input.json",
        "checksums.txt",
        "deployment-contract.json",
        "installer-materials.tar",
        "platform-qualification.json",
        "prepublication-materials.json",
        "release-manifest.json",
        "release-notes.json",
        "release-notes.md",
    }
)


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


def _file_identity(metadata: os.stat_result) -> tuple[int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
    )


@contextmanager
def _open_regular_file(path: Path, *, maximum: int) -> Iterator[BinaryIO]:
    try:
        before = path.lstat()
    except OSError as error:
        raise CandidateContractError("CANDIDATE_FILE_UNAVAILABLE") from error
    if (
        path.is_symlink()
        or bool(getattr(path, "is_junction", lambda: False)())
        or not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
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
            or opened.st_nlink != 1
            or opened.st_size < 0
            or opened.st_size > maximum
            or _file_identity(opened) != _file_identity(before)
        ):
            _reject("CANDIDATE_FILE_CHANGED_DURING_OPEN")
        with os.fdopen(descriptor, "rb", closefd=False) as source:
            yield source
            after = os.fstat(descriptor)
            if _file_identity(after) != _file_identity(opened):
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


def _read_regular_file(path: Path, *, maximum: int) -> bytes:
    try:
        with _open_regular_file(path, maximum=maximum) as source:
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


def normalize_candidate_oci_layout(
    *,
    source_root: Path,
    layout: Path,
    role: str,
    repository: str,
    expected_digest: str,
) -> dict[str, object]:
    """Close a Buildx/OCI exporter root descriptor without touching its DAG."""

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
    if changed:
        descriptor_fd, temporary_name = tempfile.mkstemp(prefix=".candidate-index-", dir=layout)
        temporary = Path(temporary_name)
        try:
            os.chmod(temporary, 0o600)
            with os.fdopen(descriptor_fd, "wb", closefd=True) as output:
                output.write(replacement)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, index_path)
            temporary = None
            try:
                verify_oci_image(layout, expectation)
            except Exception:
                index_path.write_bytes(original)
                raise
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    verified = verify_oci_image(layout, expectation)
    return {
        "changed": changed,
        "configDigest": verified.config_digest,
        "digest": verified.digest,
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
        validate_manifest(manifest, updater_version="1.0.0")
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
        "release_dry_run": f"release-dry-run-{candidate['candidate_version']}",
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
    return result


def _archive_entries(archive: zipfile.ZipFile) -> tuple[list[zipfile.ZipInfo], dict[str, int]]:
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
            or entry.file_size > MAX_ARCHIVE_MEMBER_BYTES
        ):
            _reject("CANDIDATE_ARCHIVE_ENTRY_UNSAFE")
        if name in names:
            _reject("CANDIDATE_ARCHIVE_DUPLICATE_PATH")
        if name.casefold() in folded:
            _reject("CANDIDATE_ARCHIVE_CASE_COLLISION")
        names.append(name)
        folded.add(name.casefold())
        total += entry.file_size
        if total > MAX_ARCHIVE_BYTES:
            _reject("CANDIDATE_ARCHIVE_SIZE_LIMIT")
    return entries, {entry.filename: entry.file_size for entry in entries}


def _extract_candidate_archive_stream(
    source: BinaryIO,
    destination: Path,
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
            entries, sizes = _archive_entries(archive)
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
            expected = set(_QUALIFICATION_ROOT_FILES)
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
        "verified-prepublication-candidate.schema.json",
        code="VERIFIED_CANDIDATE_SCHEMA_INVALID",
    )
    roles = [item["role"] for item in candidate["verified_artifacts"]]
    oci_roles = [item["role"] for item in candidate["oci_verification"]]
    if roles != sorted(roles) or len(set(roles)) != 2:
        _reject("VERIFIED_CANDIDATE_ARTIFACTS_INVALID")
    if oci_roles != list(OCI_ROLES):
        _reject("VERIFIED_CANDIDATE_OCI_ROLES_INVALID")
    return candidate


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
        or validated["run"]["attempt"] != 1
        or validated["workflow"] != candidate["qualification_workflow_identity"]
    ):
        _reject("CANDIDATE_QUALIFICATION_EVIDENCE_MISMATCH")

    workflow_ref = candidate["qualification_workflow_identity"]["ref"]
    platform_ref = workflow_ref.partition("@")[2]
    if not platform_ref:
        _reject("CANDIDATE_QUALIFICATION_EVIDENCE_MISMATCH")
    platform_bytes = _read_regular_file(
        root / "platform-qualification.json",
        maximum=MAX_JSON_BYTES,
    )
    embedded_platform_bytes = _read_regular_file(
        root / "installer-root" / "release" / "platform-qualification.json",
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
        or platform_bytes != embedded_platform_bytes
        or platform.candidate_sha != candidate["source_sha"]
        or dict(platform.workflow) != {
            "path": QUALIFICATION_WORKFLOW_PATH,
            "ref": platform_ref,
            "sha": candidate["source_sha"],
        }
        or dict(platform.run) != {
            "id": str(candidate["qualification_run_id"]),
            "attempt": 1,
        }
        or dict(platform.image_digests) != {
            "postgres": f"{POSTGRES_REPOSITORY}@{POSTGRES_DIGEST}",
            "redis": f"{REDIS_REPOSITORY}@{REDIS_DIGEST}",
        }
    ):
        _reject("CANDIDATE_PLATFORM_QUALIFICATION_MISMATCH")


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
    _parse_time(verified_at, code="CANDIDATE_VERIFICATION_TIME_INVALID")
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
        artifacts = _validate_bound_artifacts(candidate, artifacts_metadata)
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
        _verify_qualification_intrinsics(staging, candidate)
        verified = {
            "schema": VERIFIED_CANDIDATE_SCHEMA,
            "version": 1,
            "purpose": "VERIFIED_LOCAL_PREPUBLICATION_INPUT",
            "candidate_input_sha256": candidate_digest,
            "repository": REPOSITORY,
            "qualification_run_id": expected_run_id,
            "qualification_run_attempt": 1,
            "qualification_workflow_identity": candidate["qualification_workflow_identity"],
            "source_sha": expected_source_sha,
            "source_tree": expected_source_tree,
            "candidate_version": expected_candidate_version,
            "verified_artifacts": artifacts,
            "containing_artifact": {
                "id": containing_artifact_id,
                "api_digest": containing_artifact_api_digest,
                "archive_sha256": archive_digest,
                "file_count": file_count,
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
            "runtime_file_inventory": candidate["candidate_runtime_file_inventory"],
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
                for image in runtime.images
            ],
            "verifier_version": VERIFIER_VERSION,
            "release_authority_granted": False,
            "production_authorized": False,
            "publish_authorized": False,
            "verified_at": verified_at,
        }
        validate_verified_candidate(verified)
        encoded = canonical_json_bytes(verified)
        verified_digest = sha256_bytes(encoded)
        with (staging / "verified-candidate.json").open("xb") as output:
            output.write(encoded)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(staging / "verified-candidate.json", 0o600)
        target = state_root / candidate_digest.removeprefix("sha256:")
        if target.exists() or target.is_symlink():
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
            shutil.rmtree(staging)
            return {
                "status": "PASS",
                "candidateInputDigest": candidate_digest,
                "verifiedCandidateDigest": verified_digest,
                "existing": True,
            }
        os.replace(staging, target)
        staging = None
        return {
            "status": "PASS",
            "candidateInputDigest": candidate_digest,
            "verifiedCandidateDigest": verified_digest,
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
    candidate, candidate_bytes = _strict_json_file(
        root / "candidate-input.json", code="CANDIDATE_INPUT_INVALID"
    )
    if sha256_bytes(candidate_bytes) != verified["candidate_input_sha256"]:
        _reject("CANDIDATE_INPUT_DIGEST_MISMATCH")
    if root.name != verified["candidate_input_sha256"].removeprefix("sha256:"):
        _reject("CANDIDATE_INPUT_ROOT_IDENTITY_MISMATCH")
    validate_candidate_input(candidate, root=root)
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
    tests_pass = all(item["result"] == "PASS" for item in receipt["canonical_test_results"])
    if completed < started or receipt["original_vm_pre_hashes"] != receipt["original_vm_post_hashes"]:
        _reject("CANDIDATE_PROFILE_SAFETY_MISMATCH")
    if receipt["profile"] == "RUNTIME_BASE_OFFLINE" and any(
        receipt[field] != 0
        for field in ("network_request_count", "apt_command_count", "external_pull_count")
    ):
        _reject("CANDIDATE_OFFLINE_NETWORK_ACTIVITY")
    expected_pass = (
        receipt["installer_execution_result"] == "PASS"
        and receipt["doctor_result"] == "PASS"
        and tests_pass
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
    if receipt["rc14_prestate"] != receipt["rc14_poststate"]:
        _reject("CANDIDATE_PUBLICATION_STATE_CHANGED")
    digests = list(receipt["profile_receipts"].values())
    if len(digests) != len(set(digests)):
        _reject("CANDIDATE_PROFILE_RECEIPT_REUSE")
    expected_pass = receipt["all_profiles_pass"] is True
    if (receipt["result"] == "PASS") is not expected_pass:
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


class R2ReadonlyTransport(Protocol):
    def get(self, url: str, headers: Mapping[str, str]) -> tuple[int, bytes]: ...


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


class CloudflareR2ReadonlyAdapter:
    def get(self, url: str, headers: Mapping[str, str]) -> tuple[int, bytes]:
        try:
            parsed = urllib.parse.urlsplit(url)
            port = parsed.port
        except ValueError:
            _reject("R2_RESPONSE_UNVERIFIED")
        if (
            parsed.scheme != "https"
            or parsed.hostname != "api.cloudflare.com"
            or port is not None
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
            or not parsed.path.startswith("/client/v4/accounts/")
            or "/r2/buckets/" not in parsed.path
            or set(headers) - {"Accept", "Authorization", "Range"}
        ):
            _reject("R2_RESPONSE_UNVERIFIED")
        request = urllib.request.Request(  # noqa: S310 - fixed HTTPS host above
            url, headers=dict(headers), method="GET"
        )
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            _NoRedirectHandler(),
        )
        try:
            with opener.open(request, timeout=30) as response:  # noqa: S310 - fixed HTTPS host above
                body = response.read(MAX_R2_RESPONSE_BYTES + 1)
                if len(body) > MAX_R2_RESPONSE_BYTES:
                    _reject("R2_RESPONSE_UNVERIFIED")
                return response.status, body
        except urllib.error.HTTPError as error:
            body = error.read(MAX_R2_RESPONSE_BYTES + 1)
            if len(body) > MAX_R2_RESPONSE_BYTES:
                _reject("R2_RESPONSE_UNVERIFIED")
            return error.code, body
        except (OSError, urllib.error.URLError) as error:
            raise CandidateContractError("R2_RESPONSE_UNVERIFIED") from error


def verify_r2_origin_empty(
    *,
    account_id: str,
    token: str,
    bucket: str = R2_BUCKET,
    prefix: str = R2_RC14_PREFIX,
    transport: R2ReadonlyTransport | None = None,
) -> dict[str, object]:
    try:
        account_identity = sha256_bytes(account_id.encode("ascii"))
    except UnicodeEncodeError:
        _reject("R2_ACCOUNT_IDENTITY_MISMATCH")
    if not account_id or account_identity != R2_ACCOUNT_ID_SHA256:
        _reject("R2_ACCOUNT_IDENTITY_MISMATCH")
    if bucket != R2_BUCKET:
        _reject("R2_BUCKET_IDENTITY_MISMATCH")
    if prefix != R2_RC14_PREFIX:
        _reject("R2_PREFIX_IDENTITY_MISMATCH")
    if (
        not token
        or len(token) > 4096
        or any(ord(character) < 0x21 or ord(character) > 0x7E for character in token)
    ):
        _reject("R2_READONLY_CREDENTIAL_UNAVAILABLE")
    adapter = transport or CloudflareR2ReadonlyAdapter()
    base = f"https://api.cloudflare.com/client/v4/accounts/{account_id}/r2/buckets/{bucket}/objects"
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {token}",
    }
    query = urllib.parse.urlencode(
        {"per_page": len(R2_RC14_EXPECTED_KEYS) + 1, "prefix": prefix}
    )
    status_code, body = adapter.get(f"{base}?{query}", headers)
    if status_code in {401, 403}:
        _reject("R2_READONLY_CREDENTIAL_UNAVAILABLE")
    if status_code != 200:
        _reject("R2_RESPONSE_UNVERIFIED")
    document = _strict_json_bytes(body, code="R2_RESPONSE_UNVERIFIED")
    objects = document.get("result")
    result_info = document.get("result_info")
    if (
        document.get("success") is not True
        or type(objects) is not list
        or type(result_info) is not dict
        or result_info.get("is_truncated") is not False
    ):
        _reject("R2_RESPONSE_UNVERIFIED")
    if objects:
        _reject("R2_PREFIX_NOT_EMPTY")
    for key in R2_RC14_EXPECTED_KEYS:
        object_url = base + "/" + urllib.parse.quote(prefix + key, safe="/")
        object_status, _ = adapter.get(
            object_url,
            {**headers, "Range": "bytes=0-0"},
        )
        if object_status != 404:
            if object_status in {401, 403}:
                _reject("R2_READONLY_CREDENTIAL_UNAVAILABLE")
            _reject("R2_PREFIX_NOT_EMPTY" if object_status in {200, 206} else "R2_RESPONSE_UNVERIFIED")
    return {
        "status": "PASS",
        "method": "CLOUDFLARE_R2_OBJECTS_REST_API",
        "accountIdentity": R2_ACCOUNT_ID_SHA256,
        "bucket": R2_BUCKET,
        "prefix": R2_RC14_PREFIX,
        "expectedKeyCount": len(R2_RC14_EXPECTED_KEYS),
        "result": "PROVEN_EMPTY",
        "writeMethodCount": 0,
    }


def verify_rc14_r2_origin_from_environment(
    *,
    environment: Mapping[str, str] | None = None,
    transport: R2ReadonlyTransport | None = None,
) -> dict[str, object]:
    values = os.environ if environment is None else environment
    account_id = values.get("ANIMEMO_R2_ACCOUNT_ID", "")
    token = values.get("ANIMEMO_R2_READONLY_API_TOKEN", "")
    bucket = values.get("ANIMEMO_R2_BUCKET", "")
    prefix = values.get("ANIMEMO_R2_EXACT_PREFIX", "")
    if not token:
        _reject("R2_READONLY_CREDENTIAL_UNAVAILABLE")
    return verify_r2_origin_empty(
        account_id=account_id,
        token=token,
        bucket=bucket,
        prefix=prefix,
        transport=transport,
    )


__all__ = [
    "AGGREGATE_RECEIPT_SCHEMA",
    "CANDIDATE_INPUT_SCHEMA",
    "INSTALLER_PROFILES",
    "PROFILE_RECEIPT_SCHEMA",
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
    "validate_verified_candidate",
    "verify_prepublication_candidate",
    "verify_r2_origin_empty",
    "verify_rc14_r2_origin_from_environment",
]
