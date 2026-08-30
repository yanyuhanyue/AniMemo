"""Closed offline verifier for the five Formal provenance evidence roles.

This Wave A component emits a non-authoritative preflight receipt only.  It
deliberately exposes no VM provider, clone capability, release authority, or
publication authority.  Wave C owns the production composition that combines
this verified evidence set with exact immutable RC authority before any clone.
"""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from .candidate import (
    canonical_json_bytes,
    reject_duplicate_json_keys,
    sha256_bytes,
)

PREFLIGHT_SCHEMA = "animemo.formal-provenance-preflight/v1"
REQUIRED_EVIDENCE = frozenset(
    {
        "api-image",
        "web-image",
        "release-manifest",
        "deployment-contract",
        "installer-materials",
    }
)
EXPECTED_SUBJECT_BY_EVIDENCE = {
    "api-image": "ghcr.io/yanyuhanyue/animemo-api",
    "web-image": "ghcr.io/yanyuhanyue/animemo-web",
    "release-manifest": "release-manifest.json",
    "deployment-contract": "deployment-contract.json",
    "installer-materials": "installer-materials.tar",
}
EXPECTED_REPOSITORY = {
    "name": "yanyuhanyue/AniMemo",
    "repositoryId": "1327429673",
    "ownerId": "111261350",
}
SHA256_IDENTITY = re.compile(r"^sha256:[0-9a-f]{64}$")
COMMIT_IDENTITY = re.compile(r"^[0-9a-f]{40}$")
MAX_VERIFIER_BYTES = 128 * 1024 * 1024
MAX_INPUT_BYTES = 16 * 1024 * 1024
MAX_CLAIM_BYTES = 1024 * 1024


class ProvenancePreflightError(RuntimeError):
    """Stable fail-closed error for the pre-clone provenance boundary."""


@dataclass(frozen=True)
class FormalProvenanceInput:
    evidence_name: str
    bundle: Path
    trusted_root: Path
    request: Path


@dataclass(frozen=True)
class FormalProvenancePlan:
    verifier: Path
    inputs: tuple[FormalProvenanceInput, ...]


VerifierRunner = Callable[[tuple[str, ...]], bytes]


def _reject(code: str) -> None:
    raise ProvenancePreflightError(code)


def _read_bound_file(
    path: Path, *, maximum: int, executable: bool = False
) -> tuple[Path, bytes]:
    candidate = Path(path)
    try:
        metadata = candidate.lstat()
    except OSError as error:
        raise ProvenancePreflightError("FORMAL_PROVENANCE_INPUT_UNAVAILABLE") from error
    if (
        candidate.is_symlink()
        or bool(getattr(candidate, "is_junction", lambda: False)())
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or metadata.st_size < 1
        or metadata.st_size > maximum
        or (executable and os.name == "posix" and metadata.st_mode & 0o111 == 0)
    ):
        _reject("FORMAL_PROVENANCE_INPUT_UNSAFE")
    try:
        resolved = candidate.resolve(strict=True)
        with candidate.open("rb") as handle:
            opened = os.fstat(handle.fileno())
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_nlink != 1
                or (metadata.st_dev, metadata.st_ino) != (opened.st_dev, opened.st_ino)
            ):
                _reject("FORMAL_PROVENANCE_INPUT_REBOUND")
            value = handle.read(maximum + 1)
            after = os.fstat(handle.fileno())
    except ProvenancePreflightError:
        raise
    except OSError as error:
        raise ProvenancePreflightError("FORMAL_PROVENANCE_INPUT_UNAVAILABLE") from error
    if (
        len(value) < 1
        or len(value) > maximum
        or (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)
        != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    ):
        _reject("FORMAL_PROVENANCE_INPUT_REBOUND")
    return resolved, value


def _write_private_snapshot(
    root: Path,
    name: str,
    value: bytes,
    *,
    executable: bool = False,
) -> Path:
    path = root / name
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    mode = 0o700 if executable else 0o600
    try:
        descriptor = os.open(path, flags, mode)
        with os.fdopen(descriptor, "wb", closefd=True) as output:
            output.write(value)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(path, mode)
        resolved, observed = _read_bound_file(
            path,
            maximum=MAX_VERIFIER_BYTES if executable else MAX_INPUT_BYTES,
            executable=executable,
        )
    except ProvenancePreflightError:
        raise
    except OSError as error:
        raise ProvenancePreflightError(
            "FORMAL_PROVENANCE_SNAPSHOT_UNAVAILABLE"
        ) from error
    if observed != value:
        _reject("FORMAL_PROVENANCE_SNAPSHOT_REBOUND")
    return resolved


def _assert_snapshot_bytes(
    path: Path, expected: bytes, *, executable: bool = False
) -> None:
    _, observed = _read_bound_file(
        path,
        maximum=MAX_VERIFIER_BYTES if executable else MAX_INPUT_BYTES,
        executable=executable,
    )
    if observed != expected:
        _reject("FORMAL_PROVENANCE_SNAPSHOT_REBOUND")


def _production_verifier_runner(command: tuple[str, ...]) -> bytes:
    environment = {"PATH": os.defpath, "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"}
    try:
        completed = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=False,
            shell=False,
            timeout=60,
            env=environment,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise ProvenancePreflightError(
            "FORMAL_PROVENANCE_VERIFIER_UNAVAILABLE"
        ) from error
    if completed.returncode != 0:
        _reject("FORMAL_PROVENANCE_VERIFICATION_FAILED")
    if len(completed.stdout) < 2 or len(completed.stdout) > MAX_CLAIM_BYTES:
        _reject("FORMAL_PROVENANCE_CLAIM_INVALID")
    return completed.stdout


def _closed_claim(value: bytes) -> Mapping[str, object]:
    try:
        decoded = value.decode("utf-8")
        claim = json.loads(decoded, object_pairs_hook=reject_duplicate_json_keys)
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as error:
        raise ProvenancePreflightError("FORMAL_PROVENANCE_CLAIM_INVALID") from error
    if (
        not isinstance(claim, dict)
        or claim.get("schemaVersion") != 1
        or canonical_json_bytes(claim) != value
    ):
        _reject("FORMAL_PROVENANCE_CLAIM_INVALID")
    return claim


def _closed_actions_request(value: bytes, evidence_name: str) -> Mapping[str, object]:
    try:
        request = json.loads(
            value.decode("utf-8"), object_pairs_hook=reject_duplicate_json_keys
        )
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as error:
        raise ProvenancePreflightError(
            "FORMAL_PROVENANCE_EVIDENCE_BINDING_INVALID"
        ) from error
    expected_subject = EXPECTED_SUBJECT_BY_EVIDENCE.get(evidence_name)
    if (
        not isinstance(request, dict)
        or set(request)
        != {
            "schemaVersion",
            "mode",
            "evidenceName",
            "subject",
            "workflow",
            "sourceCommit",
        }
        or request.get("schemaVersion") != 1
        or request.get("mode") != "actions-provenance"
        or request.get("evidenceName") != evidence_name
        or request.get("workflow")
        not in {
            ".github/workflows/release.yml",
            ".github/workflows/promote-release.yml",
        }
        or not isinstance(request.get("sourceCommit"), str)
        or not COMMIT_IDENTITY.fullmatch(request["sourceCommit"])
        or not isinstance(request.get("subject"), dict)
        or set(request["subject"]) != {"name", "sha256", "size"}
        or request["subject"].get("name") != expected_subject
        or not isinstance(request["subject"].get("sha256"), str)
        or not SHA256_IDENTITY.fullmatch(request["subject"]["sha256"])
        or type(request["subject"].get("size")) is not int
        or request["subject"]["size"] < 0
    ):
        _reject("FORMAL_PROVENANCE_EVIDENCE_BINDING_INVALID")
    return request


def _bind_actions_claim(
    claim: Mapping[str, object],
    request: Mapping[str, object],
    evidence_name: str,
) -> Mapping[str, object]:
    subject = request["subject"]
    expected_claim_subject = {
        "name": subject["name"],
        "sha256": subject["sha256"],
    }
    source = claim.get("source")
    if (
        request["evidenceName"] != evidence_name
        or claim.get("subject") != expected_claim_subject
        or claim.get("repository") != EXPECTED_REPOSITORY
        or claim.get("workflow") != request["workflow"]
        or not isinstance(source, dict)
        or source.get("commit") != request["sourceCommit"]
        or source.get("ref") != "refs/heads/main"
        or claim.get("signerDigest") != request["sourceCommit"]
    ):
        _reject("FORMAL_PROVENANCE_EVIDENCE_BINDING_INVALID")
    return {
        "evidence_name": evidence_name,
        "subject": expected_claim_subject,
        "workflow": request["workflow"],
        "source_commit": request["sourceCommit"],
    }


class OfflineActionsProvenancePreflight:
    """Verify all Formal evidence roles without granting clone authority."""

    def __init__(
        self,
        plan: FormalProvenancePlan,
        *,
        runner: VerifierRunner = _production_verifier_runner,
    ) -> None:
        self._plan = plan
        self._runner = runner

    def verify(self) -> Mapping[str, object]:
        names = [item.evidence_name for item in self._plan.inputs]
        if len(names) != len(REQUIRED_EVIDENCE) or set(names) != REQUIRED_EVIDENCE:
            _reject("FORMAL_PROVENANCE_EVIDENCE_SET_INVALID")
        _, verifier_bytes = _read_bound_file(
            self._plan.verifier, maximum=MAX_VERIFIER_BYTES, executable=True
        )
        claims: list[dict[str, object]] = []
        with tempfile.TemporaryDirectory(
            prefix="animemo-formal-provenance-"
        ) as directory:
            snapshot_root = Path(directory)
            os.chmod(snapshot_root, 0o700)
            verifier_name = "verifier.exe" if os.name == "nt" else "verifier"
            verifier = _write_private_snapshot(
                snapshot_root,
                verifier_name,
                verifier_bytes,
                executable=True,
            )
            bound_inputs = []
            for item in sorted(
                self._plan.inputs, key=lambda value: value.evidence_name
            ):
                _, bundle_bytes = _read_bound_file(item.bundle, maximum=MAX_INPUT_BYTES)
                _, trusted_root_bytes = _read_bound_file(
                    item.trusted_root, maximum=MAX_INPUT_BYTES
                )
                _, request_bytes = _read_bound_file(
                    item.request, maximum=MAX_INPUT_BYTES
                )
                closed_request = _closed_actions_request(
                    request_bytes, item.evidence_name
                )
                bound_inputs.append(
                    {
                        "evidence_name": item.evidence_name,
                        "bundle": _write_private_snapshot(
                            snapshot_root,
                            f"{item.evidence_name}.bundle.json",
                            bundle_bytes,
                        ),
                        "bundle_bytes": bundle_bytes,
                        "trusted_root": _write_private_snapshot(
                            snapshot_root,
                            f"{item.evidence_name}.root.json",
                            trusted_root_bytes,
                        ),
                        "trusted_root_bytes": trusted_root_bytes,
                        "request": _write_private_snapshot(
                            snapshot_root,
                            f"{item.evidence_name}.request.json",
                            request_bytes,
                        ),
                        "request_bytes": request_bytes,
                        "closed_request": closed_request,
                    }
                )
            for bound in bound_inputs:
                command = (
                    str(verifier),
                    "--bundle",
                    str(bound["bundle"]),
                    "--trusted-root",
                    str(bound["trusted_root"]),
                    "--request",
                    str(bound["request"]),
                )
                try:
                    claim = _closed_claim(self._runner(command))
                    _assert_snapshot_bytes(verifier, verifier_bytes, executable=True)
                    _assert_snapshot_bytes(bound["bundle"], bound["bundle_bytes"])
                    _assert_snapshot_bytes(
                        bound["trusted_root"], bound["trusted_root_bytes"]
                    )
                    _assert_snapshot_bytes(bound["request"], bound["request_bytes"])
                    binding = _bind_actions_claim(
                        claim,
                        bound["closed_request"],
                        bound["evidence_name"],
                    )
                except ProvenancePreflightError:
                    raise
                except Exception as error:
                    raise ProvenancePreflightError(
                        "FORMAL_PROVENANCE_VERIFIER_UNAVAILABLE"
                    ) from error
                claims.append(
                    {
                        **binding,
                        "bundle_digest": sha256_bytes(bound["bundle_bytes"]),
                        "trusted_root_digest": sha256_bytes(
                            bound["trusted_root_bytes"]
                        ),
                        "request_digest": sha256_bytes(bound["request_bytes"]),
                        "claim_digest": sha256_bytes(canonical_json_bytes(claim)),
                    }
                )
        unsigned: dict[str, object] = {
            "schema": PREFLIGHT_SCHEMA,
            "verifier_digest": sha256_bytes(verifier_bytes),
            "claims": claims,
            "clone_authorized": False,
            "release_authority_granted": False,
            "publish_authorized": False,
        }
        return {
            **unsigned,
            "preflight_digest": sha256_bytes(canonical_json_bytes(unsigned)),
        }


__all__: Sequence[str] = (
    "FormalProvenanceInput",
    "FormalProvenancePlan",
    "OfflineActionsProvenancePreflight",
    "ProvenancePreflightError",
)
