"""Acquire and export opaque GitHub attestation bundles for offline transport.

The exported sidecar is a transport envelope.  It never signs, authorizes, or
normalizes the embedded GitHub/Sigstore bundles; the offline verifier must
cryptographically verify every embedded bundle against pre-existing trust.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
import stat
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from .materials import (
    bound_release_directory_io_available,
    bound_release_directory_matches,
    open_bound_release_directory,
)
from .portable import (
    MAX_PORTABLE_TOTAL_BYTES,
    PORTABLE_STREAM_CHUNK_BYTES,
    canonical_json_bytes,
    portable_release_asset_name,
)

SIDECAR_SCHEMA = "animemo.github-attestation-sidecar/v1"
REQUIRED_ACTIONS_EVIDENCE = frozenset(
    {
        "api-image",
        "web-image",
        "release-manifest",
        "deployment-contract",
        "installer-materials",
    }
)
REQUIRED_EVIDENCE = frozenset(
    {"github-release", "portable-asset", *REQUIRED_ACTIONS_EVIDENCE}
)
MAX_ATTESTATION_SIDECAR_BYTES = 64 * 1024 * 1024
MAX_ATTESTATION_BUNDLE_BYTES = 16 * 1024 * 1024
_COMMIT = re.compile(r"[0-9a-f]{40}")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_WORKFLOWS = frozenset(
    {".github/workflows/release.yml", ".github/workflows/promote-release.yml"}
)


class AttestationAcquisitionError(ValueError):
    """Post-publish evidence acquisition or export is incomplete or ambiguous."""


def release_attestation_sidecar_name(tag: str) -> str:
    portable_release_asset_name(tag)
    return f"animemo-{tag}-release-attestation.json"


def _payload_identity(path: Path) -> dict[str, Any]:
    path = Path(path)
    try:
        metadata = path.lstat()
    except OSError as error:
        raise AttestationAcquisitionError("PAYLOAD_UNAVAILABLE") from error
    if (
        path.is_symlink()
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or metadata.st_size < 1
        or metadata.st_size > MAX_PORTABLE_TOTAL_BYTES
    ):
        raise AttestationAcquisitionError("PAYLOAD_PATH_UNSAFE")
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY
            | getattr(os, "O_BINARY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or opened.st_dev != metadata.st_dev
            or opened.st_ino != metadata.st_ino
            or opened.st_size != metadata.st_size
        ):
            os.close(descriptor)
            raise AttestationAcquisitionError("PAYLOAD_PATH_UNSAFE")
    except AttestationAcquisitionError:
        raise
    except OSError as error:
        raise AttestationAcquisitionError("PAYLOAD_UNAVAILABLE") from error
    hasher = hashlib.sha256()
    consumed = 0
    try:
        with os.fdopen(descriptor, "rb", closefd=True) as stream:
            while chunk := stream.read(PORTABLE_STREAM_CHUNK_BYTES):
                consumed += len(chunk)
                if consumed > MAX_PORTABLE_TOTAL_BYTES:
                    raise AttestationAcquisitionError("PAYLOAD_RESOURCE_LIMIT")
                hasher.update(chunk)
    except OSError as error:
        raise AttestationAcquisitionError("PAYLOAD_UNAVAILABLE") from error
    if consumed != metadata.st_size:
        raise AttestationAcquisitionError("PAYLOAD_CHANGED_DURING_ACQUISITION")
    return {
        "name": path.name,
        "sha256": "sha256:" + hasher.hexdigest(),
        "size": consumed,
    }


def _verified_bundle(value: bytes, *, name: str) -> bytes:
    if not isinstance(value, bytes) or not value or len(value) > MAX_ATTESTATION_BUNDLE_BYTES:
        raise AttestationAcquisitionError(f"ATTESTATION_BUNDLE_INVALID:{name}")
    try:
        parsed = json.loads(value.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise AttestationAcquisitionError(
            f"ATTESTATION_VERIFICATION_OUTPUT_INVALID:{name}"
        ) from error
    entries = [parsed] if isinstance(parsed, Mapping) else parsed
    if (
        not isinstance(entries, list)
        or len(entries) != 1
        or any(
            not isinstance(item, Mapping)
            or not isinstance(item.get("attestation"), Mapping)
            for item in entries
        )
    ):
        raise AttestationAcquisitionError(
            f"ATTESTATION_VERIFICATION_OUTPUT_INVALID:{name}"
        )
    return canonical_json_bytes(
        [{"attestation": item["attestation"]} for item in entries]
    )


def _evidence_record(value: bytes) -> dict[str, Any]:
    return {
        "encoding": "base64",
        "mediaType": "application/vnd.dev.sigstore.bundle-set+json",
        "sha256": "sha256:" + hashlib.sha256(value).hexdigest(),
        "size": len(value),
        "value": base64.b64encode(value).decode("ascii"),
    }


def _directory_identity(metadata: os.stat_result) -> tuple[int, int, int]:
    return metadata.st_dev, metadata.st_ino, metadata.st_mode


def _cleanup_owned_file(parent_descriptor: int, name: str, created: os.stat_result) -> None:
    try:
        current = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
        if _directory_identity(current) == _directory_identity(created):
            os.unlink(name, dir_fd=parent_descriptor)
    except OSError:
        pass


def _write_exclusive(destination: Path, value: bytes) -> None:
    destination = Path(destination)
    if not destination.name or destination.name in {".", ".."}:
        raise AttestationAcquisitionError("SIDECAR_DESTINATION_UNSAFE")
    if bound_release_directory_io_available():
        try:
            parent_descriptor = open_bound_release_directory(destination.parent)
        except AttestationAcquisitionError:
            raise
        except OSError as error:
            raise AttestationAcquisitionError(
                "SIDECAR_DESTINATION_UNAVAILABLE"
            ) from error
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_BINARY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        created: os.stat_result | None = None
        try:
            descriptor = os.open(
                destination.name,
                flags,
                0o600,
                dir_fd=parent_descriptor,
            )
            created = os.fstat(descriptor)
            with os.fdopen(descriptor, "wb", closefd=True) as output:
                output.write(value)
                output.flush()
                os.fsync(output.fileno())
            if not bound_release_directory_matches(
                destination.parent,
                parent_descriptor,
            ):
                raise AttestationAcquisitionError("SIDECAR_DESTINATION_REBOUND")
            os.fsync(parent_descriptor)
            return
        except AttestationAcquisitionError:
            if created is not None:
                _cleanup_owned_file(parent_descriptor, destination.name, created)
            raise
        except OSError as error:
            if created is not None:
                _cleanup_owned_file(parent_descriptor, destination.name, created)
            raise AttestationAcquisitionError(
                "SIDECAR_EXCLUSIVE_EXPORT_FAILED"
            ) from error
        finally:
            os.close(parent_descriptor)

    raise AttestationAcquisitionError("SIDECAR_DESCRIPTOR_RELATIVE_IO_REQUIRED")


def export_attestation_sidecar(
    *,
    repository: str,
    tag: str,
    commit: str,
    workflow: str,
    payload: Path,
    verified_evidence: Mapping[str, bytes],
    destination: Path,
) -> dict[str, Any]:
    """Export verified bundles without making the envelope an authority object."""

    if repository != "yanyuhanyue/AniMemo":
        raise AttestationAcquisitionError("REPOSITORY_AUTHORITY_INVALID")
    expected_payload_name = portable_release_asset_name(tag)
    if not isinstance(commit, str) or not _COMMIT.fullmatch(commit):
        raise AttestationAcquisitionError("COMMIT_IDENTITY_INVALID")
    if workflow not in _WORKFLOWS:
        raise AttestationAcquisitionError("WORKFLOW_IDENTITY_INVALID")
    payload_identity = _payload_identity(payload)
    if payload_identity["name"] != expected_payload_name:
        raise AttestationAcquisitionError("PAYLOAD_NAME_INVALID")
    if not isinstance(verified_evidence, Mapping) or set(verified_evidence) != set(
        REQUIRED_EVIDENCE
    ):
        raise AttestationAcquisitionError("EVIDENCE_SET_INVALID")
    evidence = {
        name: _evidence_record(_verified_bundle(verified_evidence[name], name=name))
        for name in sorted(REQUIRED_EVIDENCE)
    }
    envelope = {
        "schema": SIDECAR_SCHEMA,
        "repository": repository,
        "tag": tag,
        "commit": commit,
        "workflow": workflow,
        "payload": payload_identity,
        "evidence": evidence,
        "authorityRole": "TRANSPORT_ONLY",
        "offlineCryptographicVerificationRequired": True,
        "selectionPolicy": "EXACT_EXPLICIT_TAG_NO_FALLBACK",
        "resigned": False,
    }
    encoded = canonical_json_bytes(envelope)
    if len(encoded) > MAX_ATTESTATION_SIDECAR_BYTES:
        raise AttestationAcquisitionError("SIDECAR_RESOURCE_LIMIT")
    _write_exclusive(destination, encoded)
    return envelope


def validate_attestation_sidecar(
    value: bytes, *, payload: Path | None = None
) -> dict[str, Any]:
    if not isinstance(value, bytes) or not value or len(value) > MAX_ATTESTATION_SIDECAR_BYTES:
        raise AttestationAcquisitionError("SIDECAR_INVALID")
    try:
        envelope = json.loads(value.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise AttestationAcquisitionError("SIDECAR_INVALID") from error
    required = {
        "schema",
        "repository",
        "tag",
        "commit",
        "workflow",
        "payload",
        "evidence",
        "authorityRole",
        "offlineCryptographicVerificationRequired",
        "selectionPolicy",
        "resigned",
    }
    if (
        not isinstance(envelope, dict)
        or set(envelope) != required
        or canonical_json_bytes(envelope) != value
        or envelope["schema"] != SIDECAR_SCHEMA
        or envelope["repository"] != "yanyuhanyue/AniMemo"
        or not isinstance(envelope["commit"], str)
        or not _COMMIT.fullmatch(envelope["commit"])
        or envelope["workflow"] not in _WORKFLOWS
        or envelope["authorityRole"] != "TRANSPORT_ONLY"
        or envelope["offlineCryptographicVerificationRequired"] is not True
        or envelope["selectionPolicy"] != "EXACT_EXPLICIT_TAG_NO_FALLBACK"
        or envelope["resigned"] is not False
    ):
        raise AttestationAcquisitionError("SIDECAR_CONTRACT_INVALID")
    expected_name = portable_release_asset_name(envelope["tag"])
    payload_record = envelope["payload"]
    if (
        not isinstance(payload_record, dict)
        or set(payload_record) != {"name", "sha256", "size"}
        or payload_record["name"] != expected_name
        or not isinstance(payload_record["sha256"], str)
        or not _DIGEST.fullmatch(payload_record["sha256"])
        or isinstance(payload_record["size"], bool)
        or not isinstance(payload_record["size"], int)
        or payload_record["size"] < 1
    ):
        raise AttestationAcquisitionError("SIDECAR_PAYLOAD_BINDING_INVALID")
    evidence = envelope["evidence"]
    if not isinstance(evidence, dict) or set(evidence) != set(REQUIRED_EVIDENCE):
        raise AttestationAcquisitionError("SIDECAR_EVIDENCE_SET_INVALID")
    for name, record in evidence.items():
        if not isinstance(record, dict) or set(record) != {
            "encoding",
            "mediaType",
            "sha256",
            "size",
            "value",
        }:
            raise AttestationAcquisitionError(f"SIDECAR_EVIDENCE_INVALID:{name}")
        try:
            decoded = base64.b64decode(record["value"], validate=True)
        except (TypeError, ValueError, binascii.Error) as error:
            raise AttestationAcquisitionError(
                f"SIDECAR_EVIDENCE_INVALID:{name}"
            ) from error
        if (
            record["encoding"] != "base64"
            or record["mediaType"] != "application/vnd.dev.sigstore.bundle-set+json"
            or record["size"] != len(decoded)
            or record["sha256"] != "sha256:" + hashlib.sha256(decoded).hexdigest()
        ):
            raise AttestationAcquisitionError(f"SIDECAR_EVIDENCE_INVALID:{name}")
        _verified_bundle(decoded, name=name)
    if payload is not None and _payload_identity(payload) != payload_record:
        raise AttestationAcquisitionError("PAYLOAD_IDENTITY_MISMATCH")
    return envelope


class GitHubAttestationAcquirer:
    """Online boundary that acquires exact-tag evidence through official gh commands."""

    def __init__(self, *, runner: Callable[[tuple[str, ...]], bytes]) -> None:
        self._runner = runner

    def acquire_and_export(
        self,
        *,
        repository: str,
        tag: str,
        commit: str,
        workflow: str,
        payload: Path,
        actions_subjects: Mapping[str, str],
        destination: Path,
        actions_workflows: Mapping[str, str] | None = None,
        actions_source_commits: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        if not isinstance(actions_subjects, Mapping) or set(actions_subjects) != set(
            REQUIRED_ACTIONS_EVIDENCE
        ):
            raise AttestationAcquisitionError("ACTIONS_SUBJECT_SET_INVALID")
        portable_release_asset_name(tag)
        if repository != "yanyuhanyue/AniMemo":
            raise AttestationAcquisitionError("REPOSITORY_AUTHORITY_INVALID")
        if workflow not in _WORKFLOWS:
            raise AttestationAcquisitionError("WORKFLOW_IDENTITY_INVALID")
        if not isinstance(commit, str) or not _COMMIT.fullmatch(commit):
            raise AttestationAcquisitionError("COMMIT_IDENTITY_INVALID")
        payload = Path(payload)
        if payload.name != portable_release_asset_name(tag):
            raise AttestationAcquisitionError("PAYLOAD_NAME_INVALID")
        _payload_identity(payload)
        if actions_workflows is None:
            resolved_workflows = {
                name: workflow for name in REQUIRED_ACTIONS_EVIDENCE
            }
        else:
            if (
                not isinstance(actions_workflows, Mapping)
                or not set(actions_workflows).issubset(REQUIRED_ACTIONS_EVIDENCE)
                or any(value not in _WORKFLOWS for value in actions_workflows.values())
            ):
                raise AttestationAcquisitionError("ACTIONS_WORKFLOW_SET_INVALID")
            resolved_workflows = {
                name: actions_workflows.get(name, workflow)
                for name in REQUIRED_ACTIONS_EVIDENCE
            }
        if actions_source_commits is None:
            resolved_source_commits = {
                name: commit for name in REQUIRED_ACTIONS_EVIDENCE
            }
        else:
            if (
                not isinstance(actions_source_commits, Mapping)
                or not set(actions_source_commits).issubset(REQUIRED_ACTIONS_EVIDENCE)
                or any(
                    not isinstance(value, str) or not _COMMIT.fullmatch(value)
                    for value in actions_source_commits.values()
                )
            ):
                raise AttestationAcquisitionError(
                    "ACTIONS_SOURCE_COMMIT_SET_INVALID"
                )
            resolved_source_commits = {
                name: actions_source_commits.get(name, commit)
                for name in REQUIRED_ACTIONS_EVIDENCE
            }

        for name in sorted(REQUIRED_ACTIONS_EVIDENCE):
            subject = actions_subjects[name]
            if not isinstance(subject, str) or not subject:
                raise AttestationAcquisitionError(f"ACTIONS_SUBJECT_INVALID:{name}")

        def acquire(name: str, command: tuple[str, ...]) -> bytes:
            return _verified_bundle(self._runner(command), name=name)

        evidence = {
            "github-release": acquire(
                "github-release",
                ("gh", "release", "verify", tag, "--repo", repository, "--format", "json")
            ),
            "portable-asset": acquire(
                "portable-asset",
                (
                    "gh",
                    "release",
                    "verify-asset",
                    tag,
                    str(payload),
                    "--repo",
                    repository,
                    "--format",
                    "json",
                )
            ),
        }
        for name in sorted(REQUIRED_ACTIONS_EVIDENCE):
            subject = actions_subjects[name]
            evidence[name] = acquire(
                name,
                (
                    "gh",
                    "attestation",
                    "verify",
                    subject,
                    "--repo",
                    repository,
                    "--signer-workflow",
                    f"{repository}/{resolved_workflows[name]}",
                    "--source-digest",
                    resolved_source_commits[name],
                    "--format",
                    "json",
                )
            )
        return export_attestation_sidecar(
            repository=repository,
            tag=tag,
            commit=commit,
            workflow=workflow,
            payload=payload,
            verified_evidence=evidence,
            destination=destination,
        )
