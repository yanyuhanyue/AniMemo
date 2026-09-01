"""Close one uploaded Candidate-byte transaction without rebuilding its bytes.

GitHub API access is deliberately outside this module.  Callers provide the
bounded Artifact metadata listing and the downloaded archive as one observed
external fact.  The two public functions are the only business-level seam.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import unicodedata
import zipfile
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Any

from release.candidate import (
    build_candidate_input,
    build_prepublication_controller_authority,
    canonical_json_bytes,
    sha256_bytes,
)
from release.materials import (
    reject_duplicate_json_keys,
    validate_candidate_production_receipt,
)
from scripts.release_qualification import (
    REQUIRED_RESULT_JOB_IDS,
    build_qualification_evidence,
    validate_qualification_evidence,
    write_qualification_evidence,
)

__all__ = ["finalize_qualification", "verify_uploaded_qualification"]

_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_SHA = re.compile(r"[0-9a-f]{40}\Z")
_RUN_ID = re.compile(r"[1-9][0-9]*\Z")
_MAX_ARCHIVE_BYTES = 16 * 1024 * 1024 * 1024
_MAX_EXPANDED_BYTES = 16 * 1024 * 1024 * 1024
_MAX_MEMBER_BYTES = 4 * 1024 * 1024 * 1024
_MAX_MEMBER_COUNT = 1200
_MAX_CONTROLLER_ARCHIVE_BYTES = 16 * 1024 * 1024
_MAX_CONTROLLER_MEMBER_BYTES = 8 * 1024 * 1024
_MAX_CONTROLLER_EXPANDED_BYTES = 3 * _MAX_CONTROLLER_MEMBER_BYTES
_REPOSITORY = "yanyuhanyue/AniMemo"
_WORKFLOW_NAME = "Release Producer"
_WORKFLOW_PATH = ".github/workflows/release.yml"
_PRODUCER_JOB = "dry-run"
_FINALIZER_JOB = "qualification-finalizer"
_CONTROLLER_JOB = "controller-authority"
_RECEIPT_NAME = "candidate-production-receipt.json"


def _fail(code: str) -> None:
    raise ValueError(code)


def _strict_object(value: object, *, code: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        _fail(code)
    return value


def _strict_json(path: Path, *, code: str) -> tuple[dict[str, Any], bytes]:
    try:
        encoded = path.read_bytes()
        value = json.loads(
            encoded.decode("utf-8"), object_pairs_hook=reject_duplicate_json_keys
        )
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
        raise ValueError(code) from error
    if not isinstance(value, dict):
        _fail(code)
    return value, encoded


def _identity(request: Mapping[str, Any]) -> dict[str, Any]:
    workflow = _strict_object(request.get("workflow"), code="ReceiptIdentityMismatch")
    run = _strict_object(request.get("run"), code="ReceiptIdentityMismatch")
    if set(workflow) != {"name", "path", "ref", "sha"} or set(run) != {
        "id",
        "attempt",
        "event",
    }:
        _fail("ReceiptIdentityMismatch")
    identity = {
        "repository": request.get("repository"),
        "workflow_ref": workflow.get("ref"),
        "workflow_sha": workflow.get("sha"),
        "run_id": str(run.get("id", "")),
        "run_attempt": run.get("attempt"),
        "event": run.get("event"),
        "candidate_sha": request.get("candidate_sha"),
        "candidate_tree": request.get("candidate_tree"),
        "target_version": request.get("target_version"),
        "release_tag": request.get("release_tag"),
        "channel": request.get("channel"),
    }
    if (
        identity["repository"] != _REPOSITORY
        or workflow.get("name") != _WORKFLOW_NAME
        or workflow.get("path") != _WORKFLOW_PATH
        or not isinstance(identity["workflow_ref"], str)
        or not _SHA.fullmatch(str(identity["workflow_sha"]))
        or not _RUN_ID.fullmatch(identity["run_id"])
        or type(identity["run_attempt"]) is not int
        or identity["run_attempt"] < 1
        or identity["event"] != "workflow_dispatch"
        or not _SHA.fullmatch(str(identity["candidate_sha"]))
        or not _SHA.fullmatch(str(identity["candidate_tree"]))
        or identity["workflow_sha"] != identity["candidate_sha"]
        or identity["channel"] not in {"beta", "rc"}
        or not isinstance(identity["target_version"], str)
        or not isinstance(identity["release_tag"], str)
    ):
        _fail("ReceiptIdentityMismatch")
    return identity


def _completed_results(request: Mapping[str, Any]) -> tuple[str, ...]:
    current_job = request.get("current_job_id")
    required = request.get("required_result_jobs")
    needs = request.get("needs")
    if not isinstance(current_job, str) or not current_job:
        _fail("SelfResultReference")
    if (
        not isinstance(required, list)
        or not required
        or any(not isinstance(item, str) or not item for item in required)
        or len(required) != len(set(required))
    ):
        _fail("IncompleteUpstreamResult")
    if current_job in required:
        _fail("SelfResultReference")
    if not isinstance(needs, Mapping) or set(needs) != set(required):
        _fail("IncompleteUpstreamResult")
    for name in required:
        value = needs.get(name)
        expected = (
            "skipped"
            if name == "performance" and request.get("channel") == "beta"
            else "success"
        )
        if not isinstance(value, Mapping) or value.get("result") != expected:
            _fail("IncompleteUpstreamResult")
    return tuple(required)


def _artifact_listing(observed: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    artifacts = observed.get("artifacts")
    total_count = observed.get("total_count")
    if not isinstance(artifacts, list) or any(
        not isinstance(item, Mapping) for item in artifacts
    ) or (
        type(total_count) is not int
        or total_count != len(artifacts)
        or total_count < 0
        or total_count > 100
    ):
        _fail("ArtifactCardinalityError")
    return list(artifacts)


def _select_artifact(
    request: Mapping[str, Any],
    observed: Mapping[str, Any],
    *,
    role: str,
) -> Mapping[str, Any]:
    identity = _identity(request)
    roles = {
        "provisional": (
            "provisional_artifact",
            f"candidate-materials-{identity['run_id']}",
            _MAX_ARCHIVE_BYTES,
        ),
        "final": (
            "final_artifact",
            f"release-qualification-{identity['run_id']}",
            _MAX_ARCHIVE_BYTES,
        ),
        "controller": (
            "controller_artifact",
            f"controller-authority-{identity['run_id']}",
            _MAX_CONTROLLER_ARCHIVE_BYTES,
        ),
    }
    if role not in roles:
        _fail("ArtifactIdentityMismatch")
    request_key, expected_name, maximum_size = roles[role]
    artifacts = _artifact_listing(observed)
    matches = [item for item in artifacts if item.get("name") == expected_name]
    if len(matches) == 1:
        artifact = matches[0]
    elif len(artifacts) == 1:
        artifact = artifacts[0]
    else:
        _fail("ArtifactCardinalityError")
    workflow_run = artifact.get("workflow_run")
    expected = request.get(request_key)
    if not isinstance(expected, Mapping) or set(expected) != {
        "id",
        "name",
        "api_digest",
    }:
        _fail("ArtifactIdentityMismatch")
    if (
        artifact.get("name") != expected_name
        or type(artifact.get("id")) is not int
        or artifact["id"] < 1
        or artifact.get("expired") is not False
        or type(artifact.get("size_in_bytes")) is not int
        or artifact["size_in_bytes"] < 1
        or artifact["size_in_bytes"] > maximum_size
        or not _DIGEST.fullmatch(str(artifact.get("digest", "")))
        or not isinstance(workflow_run, Mapping)
        or str(workflow_run.get("id", "")) != identity["run_id"]
        or workflow_run.get("head_sha") != identity["candidate_sha"]
    ):
        _fail("ArtifactIdentityMismatch")
    if (
        expected.get("id") != artifact["id"]
        or expected.get("name", expected_name) != artifact["name"]
        or expected.get("api_digest") != artifact["digest"]
    ):
        _fail("ArtifactIdentityMismatch")
    return artifact


def _archive_path(
    observed: Mapping[str, Any], *, key: str = "archive_path", maximum: int = _MAX_ARCHIVE_BYTES
) -> Path:
    raw = observed.get(key)
    if not isinstance(raw, (str, os.PathLike)):
        _fail("ExternalArtifactUnavailable")
    path = Path(raw)
    try:
        metadata = path.lstat()
    except OSError as error:
        raise ValueError("ExternalArtifactUnavailable") from error
    if (
        path.is_symlink()
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or metadata.st_size < 1
        or metadata.st_size > maximum
    ):
        _fail("ExternalArtifactUnavailable")
    return path


def _verify_archive_identity(path: Path, artifact: Mapping[str, Any]) -> str:
    digest = hashlib.sha256()
    size = 0
    try:
        with path.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                size += len(chunk)
                if size > _MAX_ARCHIVE_BYTES:
                    _fail("ExternalArtifactUnavailable")
                digest.update(chunk)
    except OSError as error:
        raise ValueError("ExternalArtifactUnavailable") from error
    value = "sha256:" + digest.hexdigest()
    if size != artifact["size_in_bytes"] or value != artifact["digest"]:
        _fail("ArchiveDigestMismatch")
    return value


def _member_name(name: str) -> str:
    if (
        not isinstance(name, str)
        or not name
        or "\\" in name
        or "\x00" in name
        or name.startswith("/")
        or re.match(r"[A-Za-z]:", name)
        or unicodedata.normalize("NFC", name) != name
    ):
        _fail("UnsafeArchiveMember")
    parsed = PurePosixPath(name)
    if parsed.is_absolute() or any(part in {"", ".", ".."} for part in parsed.parts):
        _fail("UnsafeArchiveMember")
    return parsed.as_posix()


def _extract_archive(
    path: Path,
    destination: Path,
    *,
    max_member_count: int = _MAX_MEMBER_COUNT,
    max_member_bytes: int = _MAX_MEMBER_BYTES,
    max_expanded_bytes: int = _MAX_EXPANDED_BYTES,
) -> dict[str, tuple[int, str]]:
    total = 0
    inventory: dict[str, tuple[int, str]] = {}
    validated_entries: list[tuple[zipfile.ZipInfo, str]] = []
    names: set[str] = set()
    folded: set[str] = set()
    try:
        with zipfile.ZipFile(path, mode="r") as archive:
            entries = archive.infolist()
            if not entries or len(entries) > max_member_count:
                _fail("UnsafeArchiveMember")
            for entry in entries:
                name = _member_name(entry.filename)
                collision = unicodedata.normalize("NFC", name).casefold()
                if name in names or collision in folded:
                    _fail("DuplicateArchiveMember")
                mode = entry.external_attr >> 16
                file_type = stat.S_IFMT(mode)
                if (
                    entry.is_dir()
                    or entry.flag_bits & 0x1
                    or file_type not in {0, stat.S_IFREG}
                    or entry.file_size < 0
                    or entry.file_size > max_member_bytes
                ):
                    _fail("UnsafeArchiveMember")
                total += entry.file_size
                if total > max_expanded_bytes:
                    _fail("UnsafeArchiveMember")
                validated_entries.append((entry, name))
                names.add(name)
                folded.add(collision)

            expanded_written = 0
            for entry, name in validated_entries:
                target = destination.joinpath(*PurePosixPath(name).parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                digest = hashlib.sha256()
                written = 0
                with archive.open(entry, mode="r") as source, target.open("xb") as output:
                    while chunk := source.read(1024 * 1024):
                        written += len(chunk)
                        expanded_written += len(chunk)
                        if (
                            written > entry.file_size
                            or written > max_member_bytes
                            or expanded_written > max_expanded_bytes
                        ):
                            _fail("UnsafeArchiveMember")
                        digest.update(chunk)
                        output.write(chunk)
                if written != entry.file_size:
                    _fail("UnsafeArchiveMember")
                os.chmod(target, 0o600)
                inventory[name] = (written, "sha256:" + digest.hexdigest())
    except ValueError:
        raise
    except (OSError, zipfile.BadZipFile, RuntimeError) as error:
        raise ValueError("UnsafeArchiveMember") from error
    return inventory


def _copy_verified_tree(source: Path, destination: Path) -> int:
    if destination.exists() or destination.is_symlink():
        _fail("ByteSetMismatch")
    destination.mkdir(parents=True, mode=0o700)
    count = 0
    try:
        for path in sorted(item for item in source.rglob("*") if item.is_file()):
            relative = path.relative_to(source)
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            before = path.read_bytes()
            with target.open("xb") as output:
                output.write(before)
                output.flush()
                os.fsync(output.fileno())
            os.chmod(target, 0o600)
            after = target.read_bytes()
            if after != before:
                _fail("ByteSetMismatch")
            count += 1
    except ValueError:
        shutil.rmtree(destination, ignore_errors=True)
        raise
    except OSError as error:
        shutil.rmtree(destination, ignore_errors=True)
        raise ValueError("ByteSetMismatch") from error
    return count


def finalize_qualification(
    request: Mapping[str, Any], observed_archive: Mapping[str, Any]
) -> dict[str, Any]:
    """Verify one provisional transaction and add only finalization metadata."""

    request = _strict_object(request, code="ReceiptIdentityMismatch")
    observed_archive = _strict_object(
        observed_archive, code="ExternalArtifactUnavailable"
    )
    identity = _identity(request)
    required = _completed_results(request)
    if tuple(required) != tuple(REQUIRED_RESULT_JOB_IDS):
        _fail("IncompleteUpstreamResult")
    if request.get("current_job_id") != _FINALIZER_JOB:
        _fail("SelfResultReference")
    artifact = _select_artifact(request, observed_archive, role="provisional")
    archive_path = _archive_path(observed_archive)
    archive_digest = _verify_archive_identity(archive_path, artifact)
    output_raw = request.get("output_directory")
    if not isinstance(output_raw, (str, os.PathLike)):
        _fail("ByteSetMismatch")
    output = Path(output_raw)
    with tempfile.TemporaryDirectory(prefix="animemo-qualification-finalizer-") as temp:
        extracted = Path(temp) / "provisional"
        extracted.mkdir(mode=0o700)
        _extract_archive(archive_path, extracted)
        receipt, receipt_bytes = _strict_json(
            extracted / _RECEIPT_NAME, code="ReceiptSchemaError"
        )
        expected_receipt_sha256 = request.get(
            "candidate_production_receipt_sha256"
        )
        if (
            not _DIGEST.fullmatch(str(expected_receipt_sha256 or ""))
            or expected_receipt_sha256 != sha256_bytes(receipt_bytes)
        ):
            _fail("ReceiptIdentityMismatch")
        try:
            validate_candidate_production_receipt(
                receipt, root=extracted, identity=identity
            )
        except ValueError as error:
            code = str(error)
            if not any(
                marker in code
                for marker in (
                    "ReceiptSchemaError",
                    "ReceiptIdentityMismatch",
                    "ByteSetMismatch",
                )
            ):
                code = "ByteSetMismatch"
            raise ValueError(code) from error
        copied = _copy_verified_tree(extracted, output)

    platform = _strict_object(
        request.get("platform_artifact"), code="ArtifactIdentityMismatch"
    )
    if (
        type(platform.get("id")) is not int
        or platform["id"] < 1
        or not _DIGEST.fullmatch(str(platform.get("api_digest", "")))
    ):
        shutil.rmtree(output, ignore_errors=True)
        _fail("ArtifactIdentityMismatch")
    try:
        release_notes, _ = _strict_json(
            output / "release-notes.json", code="ReceiptIdentityMismatch"
        )
        release_notes_identity = release_notes.get("identity")
        if not _DIGEST.fullmatch(str(release_notes_identity or "")):
            _fail("ReceiptIdentityMismatch")
        release_notes_markdown_sha256 = sha256_bytes(
            (output / "release-notes.md").read_bytes()
        )
        candidate = build_candidate_input(
            root=output,
            qualification_run_id=int(identity["run_id"]),
            qualification_run_attempt=identity["run_attempt"],
            source_sha=identity["candidate_sha"],
            source_tree=identity["candidate_tree"],
            artifact_ids={
                "platform_qualification": platform["id"],
                "release_dry_run": artifact["id"],
            },
            artifact_api_digests={
                "platform_qualification": platform["api_digest"],
                "release_dry_run": artifact["digest"],
            },
            generated_at=str(request.get("created_at", "")),
            output=output / "candidate-input.json",
        )
        receipt_sha256 = sha256_bytes(receipt_bytes)
        evidence = build_qualification_evidence(
            workflow_ref=str(request["workflow"]["ref"]),
            workflow_sha=identity["workflow_sha"],
            run_id=identity["run_id"],
            run_attempt=identity["run_attempt"],
            candidate_sha=identity["candidate_sha"],
            candidate_tree=identity["candidate_tree"],
            upgrade_base_sha=str(request.get("upgrade_base_sha", "")),
            channel=identity["channel"],
            target_version=identity["target_version"],
            release_tag=identity["release_tag"],
            needs=request["needs"],
            current_job_id=_FINALIZER_JOB,
            candidate_production_receipt_sha256=receipt_sha256,
            producer_job_observation={
                "id": _PRODUCER_JOB,
                "result": request["needs"][_PRODUCER_JOB]["result"],
            },
            provisional_artifact={
                "id": artifact["id"],
                "name": artifact["name"],
                "api_digest": artifact["digest"],
                "archive_sha256": archive_digest,
            },
            created_at=str(request.get("created_at", "")),
            event=identity["event"],
            release_notes_identity=str(release_notes_identity),
            release_notes_markdown_sha256=release_notes_markdown_sha256,
        )
        qualification_path = output / f"release-qualification-{identity['run_id']}.json"
        write_qualification_evidence(qualification_path, evidence)
        validate_qualification_evidence(evidence)
    except Exception:
        shutil.rmtree(output, ignore_errors=True)
        raise
    return {
        "schema": "animemo.qualification-finalization-result/v1",
        "status": "PASS",
        "localFinalizationResult": "PASS",
        "provisionalArtifactId": artifact["id"],
        "provisionalArtifactApiDigest": artifact["digest"],
        "provisionalArchiveSha256": archive_digest,
        "candidateProductionReceiptSha256": receipt_sha256,
        "candidateInputSha256": sha256_bytes(canonical_json_bytes(candidate)),
        "qualificationSha256": evidence["qualification_sha256"],
        "copiedProvisionalMemberCount": copied,
        "candidateBytesRebuildCount": 0,
    }


def verify_uploaded_qualification(
    request: Mapping[str, Any], observed_archive: Mapping[str, Any]
) -> dict[str, Any]:
    """Re-read the uploaded final Artifact and emit post-upload authority."""

    request = _strict_object(request, code="ReceiptIdentityMismatch")
    observed_archive = _strict_object(
        observed_archive, code="ExternalArtifactUnavailable"
    )
    identity = _identity(request)
    required = _completed_results(request)
    if request.get("current_job_id") != _CONTROLLER_JOB or tuple(required) != (
        _FINALIZER_JOB,
    ):
        _fail("SelfResultReference")
    artifact = _select_artifact(request, observed_archive, role="final")
    archive_path = _archive_path(observed_archive)
    archive_digest = _verify_archive_identity(archive_path, artifact)
    output_raw = request.get("output_directory")
    if not isinstance(output_raw, (str, os.PathLike)):
        _fail("ArtifactIdentityMismatch")
    output = Path(output_raw)
    if output.exists() or output.is_symlink():
        _fail("ArtifactIdentityMismatch")
    try:
        controller = build_prepublication_controller_authority(
            archive=archive_path,
            containing_artifact_id=artifact["id"],
            containing_artifact_api_digest=artifact["digest"],
            output=output,
        )
        with zipfile.ZipFile(archive_path, mode="r") as archive:
            qualification_name = f"release-qualification-{identity['run_id']}.json"
            names = archive.namelist()
            if names.count(qualification_name) != 1 or names.count(_RECEIPT_NAME) != 1:
                _fail("FinalArtifactSelfReference")
            qualification_bytes = archive.read(qualification_name)
            receipt_bytes = archive.read(_RECEIPT_NAME)
        qualification = json.loads(
            qualification_bytes.decode("utf-8"),
            object_pairs_hook=reject_duplicate_json_keys,
        )
        validated = validate_qualification_evidence(
            qualification,
            expected={
                "repository": identity["repository"],
                "candidate_sha": identity["candidate_sha"],
                "candidate_tree": identity["candidate_tree"],
                "upgrade_base_sha": request.get("upgrade_base_sha"),
                "channel": identity["channel"],
                "target_version": identity["target_version"],
                "release_tag": identity["release_tag"],
                "qualification_run_id": identity["run_id"],
                "qualification_run_attempt": identity["run_attempt"],
                "workflow_ref": identity["workflow_ref"],
                "workflow_sha": identity["workflow_sha"],
                "created_at": request.get("created_at"),
                "candidate_production_receipt_sha256": sha256_bytes(
                    receipt_bytes
                ),
            },
        )
        if validated["candidate_production_receipt_sha256"] != sha256_bytes(
            receipt_bytes
        ):
            _fail("ByteSetMismatch")
        candidate_bytes = (output / "candidate-input.json").read_bytes()
        verified_bytes = (output / "verified-candidate.json").read_bytes()
        authority = {
            "schema": "animemo.qualification-controller-authority/v1",
            "version": 1,
            "authority": "FINAL_ARTIFACT_REMOTE_READBACK_VERIFIED",
            "repository": _REPOSITORY,
            "workflow": {
                "name": _WORKFLOW_NAME,
                "path": _WORKFLOW_PATH,
                "ref": identity["workflow_ref"],
                "sha": identity["workflow_sha"],
            },
            "run": {
                "id": identity["run_id"],
                "attempt": identity["run_attempt"],
                "event": identity["event"],
            },
            "source_sha": identity["candidate_sha"],
            "finalizer_job_observation": {
                "job_id": _FINALIZER_JOB,
                "result": request["needs"][_FINALIZER_JOB]["result"],
            },
            "final_artifact": {
                "id": artifact["id"],
                "name": artifact["name"],
                "size_in_bytes": artifact["size_in_bytes"],
                "api_digest": artifact["digest"],
                "archive_sha256": archive_digest,
                "run_id": identity["run_id"],
                "head_sha": identity["candidate_sha"],
            },
            "qualification_sha256": sha256_bytes(qualification_bytes),
            "candidate_production_receipt_sha256": sha256_bytes(receipt_bytes),
            "candidate_input_sha256": sha256_bytes(candidate_bytes),
            "verified_candidate_sha256": sha256_bytes(verified_bytes),
            "final_run_state_authority": "EXTERNAL_PHASE_B_REQUIRED",
        }
        (output / "controller-authority.json").write_bytes(
            canonical_json_bytes(authority)
        )
    except Exception:
        if output.exists():
            shutil.rmtree(output, ignore_errors=True)
        raise
    return {
        **controller,
        "authorityFileCount": 3,
        "finalArtifactId": artifact["id"],
        "finalArtifactApiDigest": artifact["digest"],
        "finalArtifactArchiveSha256": archive_digest,
        "controllerAuthoritySha256": sha256_bytes(canonical_json_bytes(authority)),
    }


def _bounded_authority_member(directory: Path, name: str) -> bytes:
    if not directory.is_dir() or directory.is_symlink():
        _fail("ControllerAuthorityMismatch")
    target = directory / name
    try:
        metadata = target.lstat()
        if (
            target.is_symlink()
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_size < 1
            or metadata.st_size > _MAX_CONTROLLER_MEMBER_BYTES
        ):
            _fail("ControllerAuthorityMismatch")
        encoded = target.read_bytes()
    except OSError as error:
        raise ValueError("ControllerAuthorityMismatch") from error
    if len(encoded) != metadata.st_size:
        _fail("ControllerAuthorityMismatch")
    return encoded


def _verify_phase_b_controller_authority(
    request: Mapping[str, Any],
    observed_archives: Mapping[str, Any],
    qualification_directory: Path,
    verified_candidate_directory: Path,
) -> dict[str, Any]:
    """Bind a completed Run's controller receipt to both downloaded archives."""

    request = _strict_object(request, code="ReceiptIdentityMismatch")
    observed_archives = _strict_object(
        observed_archives, code="ExternalArtifactUnavailable"
    )
    identity = _identity(request)
    final_artifact = _select_artifact(request, observed_archives, role="final")
    controller_artifact = _select_artifact(
        request, observed_archives, role="controller"
    )
    final_archive = _archive_path(
        observed_archives, key="qualification_archive_path"
    )
    controller_archive = _archive_path(
        observed_archives,
        key="controller_archive_path",
        maximum=_MAX_CONTROLLER_ARCHIVE_BYTES,
    )
    final_archive_digest = _verify_archive_identity(final_archive, final_artifact)
    controller_archive_digest = _verify_archive_identity(
        controller_archive, controller_artifact
    )

    qualification_name = f"release-qualification-{identity['run_id']}.json"
    qualification_bytes = _bounded_authority_member(
        qualification_directory, qualification_name
    )
    receipt_bytes = _bounded_authority_member(
        qualification_directory, _RECEIPT_NAME
    )
    final_candidate_bytes = _bounded_authority_member(
        qualification_directory, "candidate-input.json"
    )
    verified_candidate_input_bytes = _bounded_authority_member(
        verified_candidate_directory, "candidate-input.json"
    )
    verified_candidate_bytes = _bounded_authority_member(
        verified_candidate_directory, "verified-candidate.json"
    )
    if final_candidate_bytes != verified_candidate_input_bytes:
        _fail("ControllerAuthorityMismatch")

    with tempfile.TemporaryDirectory(prefix="animemo-phase-b-controller-") as temp:
        extracted = Path(temp) / "controller"
        extracted.mkdir(mode=0o700)
        inventory = _extract_archive(
            controller_archive,
            extracted,
            max_member_count=3,
            max_member_bytes=_MAX_CONTROLLER_MEMBER_BYTES,
            max_expanded_bytes=_MAX_CONTROLLER_EXPANDED_BYTES,
        )
        if set(inventory) != {
            "candidate-input.json",
            "controller-authority.json",
            "verified-candidate.json",
        }:
            _fail("ControllerAuthorityMismatch")
        authority, authority_bytes = _strict_json(
            extracted / "controller-authority.json",
            code="ControllerAuthorityMismatch",
        )
        controller_candidate_bytes = _bounded_authority_member(
            extracted, "candidate-input.json"
        )
        controller_verified_bytes = _bounded_authority_member(
            extracted, "verified-candidate.json"
        )

    if (
        controller_candidate_bytes != verified_candidate_input_bytes
        or controller_verified_bytes != verified_candidate_bytes
    ):
        _fail("ControllerAuthorityMismatch")
    expected_authority = {
        "schema": "animemo.qualification-controller-authority/v1",
        "version": 1,
        "authority": "FINAL_ARTIFACT_REMOTE_READBACK_VERIFIED",
        "repository": _REPOSITORY,
        "workflow": {
            "name": _WORKFLOW_NAME,
            "path": _WORKFLOW_PATH,
            "ref": identity["workflow_ref"],
            "sha": identity["workflow_sha"],
        },
        "run": {
            "id": identity["run_id"],
            "attempt": identity["run_attempt"],
            "event": identity["event"],
        },
        "source_sha": identity["candidate_sha"],
        "finalizer_job_observation": {
            "job_id": _FINALIZER_JOB,
            "result": "success",
        },
        "final_artifact": {
            "id": final_artifact["id"],
            "name": final_artifact["name"],
            "size_in_bytes": final_artifact["size_in_bytes"],
            "api_digest": final_artifact["digest"],
            "archive_sha256": final_archive_digest,
            "run_id": identity["run_id"],
            "head_sha": identity["candidate_sha"],
        },
        "qualification_sha256": sha256_bytes(qualification_bytes),
        "candidate_production_receipt_sha256": sha256_bytes(receipt_bytes),
        "candidate_input_sha256": sha256_bytes(verified_candidate_input_bytes),
        "verified_candidate_sha256": sha256_bytes(verified_candidate_bytes),
        "final_run_state_authority": "EXTERNAL_PHASE_B_REQUIRED",
    }
    if authority != expected_authority or authority_bytes != canonical_json_bytes(
        expected_authority
    ):
        _fail("ControllerAuthorityMismatch")
    return {
        "schema": "animemo.phase-b-controller-verification/v1",
        "status": "PASS",
        "qualificationRunId": identity["run_id"],
        "finalArtifactId": final_artifact["id"],
        "finalArtifactApiDigest": final_artifact["digest"],
        "controllerArtifactId": controller_artifact["id"],
        "controllerArtifactApiDigest": controller_artifact["digest"],
        "controllerArtifactArchiveSha256": controller_archive_digest,
        "controllerAuthoritySha256": sha256_bytes(authority_bytes),
        "finalRunStateAuthority": "COMPLETED_PRIOR_RUN_API_METADATA",
        "candidateBytesRebuildCount": 0,
    }


def _read_json(path: Path) -> dict[str, Any]:
    value, _ = _strict_json(path, code="ExternalObservationStale")
    return value


def _main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("finalize", "verify", "phase-b"))
    parser.add_argument("--request", required=True, type=Path)
    parser.add_argument("--artifacts-metadata", required=True, type=Path)
    parser.add_argument("--archive", required=True, type=Path)
    parser.add_argument("--controller-archive", type=Path)
    parser.add_argument("--qualification-directory", type=Path)
    parser.add_argument("--verified-candidate-directory", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    arguments = parser.parse_args(argv)
    request = _read_json(arguments.request)
    metadata = _read_json(arguments.artifacts_metadata)
    if arguments.operation == "phase-b":
        if (
            arguments.controller_archive is None
            or arguments.qualification_directory is None
            or arguments.verified_candidate_directory is None
            or arguments.output.exists()
            or arguments.output.is_symlink()
        ):
            _fail("ExternalArtifactUnavailable")
        observed = {
            "total_count": metadata.get("total_count"),
            "artifacts": metadata.get("artifacts"),
            "qualification_archive_path": str(arguments.archive),
            "controller_archive_path": str(arguments.controller_archive),
        }
        result = _verify_phase_b_controller_authority(
            request,
            observed,
            arguments.qualification_directory,
            arguments.verified_candidate_directory,
        )
        arguments.output.write_bytes(canonical_json_bytes(result))
    else:
        request["output_directory"] = str(arguments.output)
        observed = {
            "total_count": metadata.get("total_count"),
            "artifacts": metadata.get("artifacts"),
            "archive_path": str(arguments.archive),
        }
        result = (
            finalize_qualification(request, observed)
            if arguments.operation == "finalize"
            else verify_uploaded_qualification(request, observed)
        )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
