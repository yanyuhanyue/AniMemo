"""Production host harness for post-publication Formal VM acceptance."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import shutil
import stat
import sys
import tarfile
from collections.abc import Mapping
from contextlib import ExitStack
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import uuid4

from release.acceptance import AcceptanceError, validate_rc_live_acceptance
from release.candidate import canonical_json_bytes, sha256_bytes
from release.formal_vm_controller import (
    FORMAL_PROFILE_RESULT_KEYS,
    FORMAL_PROFILES,
    FormalAuthorityRequest,
    FormalExecutionContext,
    FormalProducerError,
    FormalProfileExecutionError,
    FormalProfileObservation,
    FormalProvenanceInput,
    FormalProvenancePlan,
    FormalVmController,
    ProductionFormalAuthorityVerifier,
    ProvenancePreflightError,
    QualifiedCandidateFormalAuthority,
    VerifiedFormalRcAuthority,
    _candidate_source_vm_authority_identity,
    close_qualified_candidate_for_formal,
    execute_candidate_controller_for_formal,
    validate_formal_acceptance_bundle,
    validate_formal_aggregate_receipt,
    validate_formal_execution_receipt,
    validate_formal_profile_receipt,
    validate_formal_profile_status_receipt,
)
from release.formal_windows_pretrust import (
    FormalWindowsPretrustError,
    HeldWindowsPrivatePathAuthority,
    assert_windows_private_acl,
    commit_windows_private_directory_snapshot,
    create_windows_private_directory,
    hold_windows_private_descendant_path,
    hold_windows_private_path_authority,
    hold_windows_private_path_chain,
    hold_windows_private_snapshot,
)
from release.materials import MaterialContractError, inspect_installer_materials
from scripts.candidate_vm_harness import (
    EXPECTED_SCP_SHA256,
    EXPECTED_SSH_SHA256,
    PROFILES,
    SNAPSHOT_ALLOWLIST,
    CandidateHarnessError,
    CandidateProfileExecutionError,
    ClosedFormalProfileWorkload,
    ClosedVmProviderPlan,
    ClosedVmwareProvider,
    ProviderReadinessReceipt,
    VmProviderProfilePlan,
    _initial_platform_state,
    _validate_continuation_receipt,
    _validate_source_evidence,
)

_FORMAL_TO_PROVIDER = {
    "FORMAL_FRESH": "FRESH_BASE",
    "FORMAL_DOCKER": "DOCKER_BASE",
    "FORMAL_OFFLINE": "RUNTIME_BASE_OFFLINE",
}
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")


class FormalCleanupFailure(BaseException):
    """Fixed-code aggregate retaining every fail-closed cleanup failure."""

    def __init__(self, errors: tuple[BaseException, ...]) -> None:
        super().__init__("FORMAL_FAIL_CLOSED_CLEANUP_FAILED")
        self.errors = errors


class ContinuationEvidenceSealSuccess:
    """Opaque success issued only after internal manifest/seal readback."""

    __slots__ = ("__token",)

    def __init__(self) -> None:
        raise TypeError("Continuation Evidence seal success cannot be constructed")

    def __reduce__(self) -> object:
        raise TypeError("Continuation Evidence seal success cannot be serialized")

    def _record(self) -> Mapping[str, str]:
        try:
            return _CONTINUATION_EVIDENCE_SEAL_SUCCESSES[self.__token]
        except (AttributeError, KeyError) as error:
            raise FormalProducerError("FORMAL_EVIDENCE_SEAL_RESULT_INVALID") from error

    def as_dict(self) -> dict[str, str]:
        return dict(self._record())


_CONTINUATION_EVIDENCE_SEAL_SUCCESSES: dict[object, Mapping[str, str]] = {}


def _issue_continuation_evidence_seal_success(
    body: Mapping[str, str],
) -> ContinuationEvidenceSealSuccess:
    token = object()
    success = object.__new__(ContinuationEvidenceSealSuccess)
    success._ContinuationEvidenceSealSuccess__token = token
    _CONTINUATION_EVIDENCE_SEAL_SUCCESSES[token] = dict(body)
    return success


class _ContinuationEvidenceLifetimeRecord:
    def __init__(
        self,
        *,
        continuation_root: Path,
        evidence_root: Path,
        seal_root: Path,
        test_enter: Any = None,
        test_exit: Any = None,
    ) -> None:
        self.continuation_root = continuation_root
        self.evidence_root = evidence_root
        self.seal_root = seal_root
        self.test_enter = test_enter
        self.test_exit = test_exit
        self.stack: ExitStack | None = None
        self.path_authority: HeldWindowsPrivatePathAuthority | None = None


_CONTINUATION_EVIDENCE_LIFETIMES: dict[
    object, _ContinuationEvidenceLifetimeRecord
] = {}


class ContinuationEvidenceLifetimeAuthority:
    """Opaque full-path-chain authority spanning continuation through seal."""

    __slots__ = ("__token",)

    def __init__(self) -> None:
        raise TypeError(
            "Continuation Evidence lifetime authority cannot be constructed"
        )

    def __reduce__(self) -> object:
        raise TypeError("Continuation Evidence lifetime authority cannot be serialized")

    def _record(self) -> _ContinuationEvidenceLifetimeRecord:
        try:
            return _CONTINUATION_EVIDENCE_LIFETIMES[self.__token]
        except (AttributeError, KeyError) as error:
            raise FormalProducerError("FORMAL_EVIDENCE_LIFETIME_INVALID") from error

    @property
    def continuation_root(self) -> Path:
        return self._record().continuation_root

    @property
    def evidence_root(self) -> Path:
        return self._record().evidence_root

    @property
    def seal_root(self) -> Path:
        return self._record().seal_root

    def __enter__(self) -> ContinuationEvidenceLifetimeAuthority:
        record = self._record()
        if record.stack is not None:
            raise FormalProducerError("FORMAL_EVIDENCE_LIFETIME_INVALID")
        stack = ExitStack()
        try:
            if record.test_enter is not None:
                record.test_enter()
            else:
                _validate_private_lifetime_root(record.continuation_root)
                record.path_authority = stack.enter_context(
                    hold_windows_private_path_authority(
                        record.continuation_root,
                        allow_leaf_child_writes=True,
                    )
                )
                for root in (record.evidence_root, record.seal_root):
                    _validate_contained_path(
                        record.continuation_root, root, require_exists=True
                    )
                    assert_windows_private_acl(root)
        except BaseException:
            stack.close()
            raise
        record.stack = stack
        return self

    def __exit__(self, *_: object) -> None:
        record = self._record()
        stack = record.stack
        if stack is None:
            return
        record.stack = None
        record.path_authority = None
        try:
            stack.close()
        finally:
            try:
                if record.test_exit is not None:
                    record.test_exit()
            finally:
                token = self.__token
                _CONTINUATION_EVIDENCE_LIFETIMES.pop(token, None)
                self.__token = None

    def require_contained(self, path: Path, *, name: str) -> Path:
        record = self._record()
        if record.stack is None:
            raise FormalProducerError("FORMAL_EVIDENCE_LIFETIME_NOT_HELD")
        try:
            return _validate_contained_path(
                record.continuation_root, Path(path), require_exists=False
            )
        except FormalProducerError as error:
            raise FormalProducerError(
                f"FORMAL_PARENT_{name.upper()}_OUTSIDE_LIFETIME"
            ) from error

    @property
    def path_authority(self) -> HeldWindowsPrivatePathAuthority | None:
        record = self._record()
        if record.stack is None:
            raise FormalProducerError("FORMAL_EVIDENCE_LIFETIME_NOT_HELD")
        return record.path_authority

    def verify_and_issue_seal_success(self) -> ContinuationEvidenceSealSuccess:
        record = self._record()
        if record.stack is None:
            raise FormalProducerError("FORMAL_EVIDENCE_SEAL_RESULT_INVALID")
        if record.test_enter is not None:
            return _issue_continuation_evidence_seal_success(
                {
                    "schema": "animemo.continuation-evidence-seal-success/v1",
                    "evidenceRoot": str(record.evidence_root),
                    "sha256sumsIdentity": "sha256:" + "0" * 64,
                    "independentSealIdentity": "sha256:" + "0" * 64,
                    "sealedEvidenceInventoryIdentity": "sha256:" + "0" * 64,
                    "result": "PASS",
                }
            )
        return _verify_and_issue_continuation_evidence_seal(record)


def _validate_contained_path(
    root: Path, path: Path, *, require_exists: bool
) -> Path:
    try:
        if not root.is_absolute() or not path.is_absolute():
            raise FormalProducerError("FORMAL_EVIDENCE_PATH_INVALID")
        closed_root = root.resolve(strict=True)
        closed_path = path.resolve(strict=require_exists)
        closed_path.relative_to(closed_root)
        return closed_path
    except (OSError, ValueError) as error:
        raise FormalProducerError("FORMAL_EVIDENCE_PATH_INVALID") from error


def _validate_private_lifetime_root(root: Path) -> Path:
    closed = _validate_contained_path(root, root, require_exists=True)
    try:
        metadata = closed.lstat()
        if (
            closed.is_symlink()
            or bool(getattr(closed, "is_junction", lambda: False)())
            or not stat.S_ISDIR(metadata.st_mode)
        ):
            raise FormalProducerError("FORMAL_EVIDENCE_LIFETIME_INVALID")
        assert_windows_private_acl(closed)
    except OSError as error:
        raise FormalProducerError("FORMAL_EVIDENCE_LIFETIME_INVALID") from error
    return closed


def acquire_continuation_evidence_lifetime_authority(
    *,
    continuation_root: Path,
    evidence_root: Path,
    seal_root: Path,
) -> ContinuationEvidenceLifetimeAuthority:
    """Bind exact private continuation/Evidence/seal roots before work starts."""

    continuation = _validate_private_lifetime_root(Path(continuation_root))
    evidence = _validate_contained_path(
        continuation, Path(evidence_root), require_exists=True
    )
    seal = _validate_contained_path(
        continuation, Path(seal_root), require_exists=True
    )
    if evidence == seal:
        raise FormalProducerError("FORMAL_EVIDENCE_LIFETIME_INVALID")
    assert_windows_private_acl(evidence)
    assert_windows_private_acl(seal)
    return _issue_continuation_evidence_lifetime_authority(
        continuation_root=continuation,
        evidence_root=evidence,
        seal_root=seal,
    )


def _issue_continuation_evidence_lifetime_authority(
    *,
    continuation_root: Path,
    evidence_root: Path,
    seal_root: Path,
    test_enter: Any = None,
    test_exit: Any = None,
) -> ContinuationEvidenceLifetimeAuthority:
    token = object()
    authority = object.__new__(ContinuationEvidenceLifetimeAuthority)
    authority._ContinuationEvidenceLifetimeAuthority__token = token
    _CONTINUATION_EVIDENCE_LIFETIMES[token] = (
        _ContinuationEvidenceLifetimeRecord(
            continuation_root=Path(continuation_root).resolve(strict=False),
            evidence_root=Path(evidence_root).resolve(strict=False),
            seal_root=Path(seal_root).resolve(strict=False),
            test_enter=test_enter,
            test_exit=test_exit,
        )
    )
    return authority


def _read_closed_asset(path: Path, *, maximum: int) -> bytes:
    try:
        metadata = path.lstat()
        if (
            path.is_symlink()
            or bool(getattr(path, "is_junction", lambda: False)())
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_size < 1
            or metadata.st_size > maximum
        ):
            raise FormalProducerError("FORMAL_RUNTIME_ASSET_INVALID")
        with path.open("rb") as handle:
            opened = os.fstat(handle.fileno())
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_nlink != 1
                or (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino)
            ):
                raise FormalProducerError("FORMAL_RUNTIME_ASSET_REBOUND")
            value = handle.read(maximum + 1)
            after = os.fstat(handle.fileno())
    except FormalProducerError:
        raise
    except OSError as error:
        raise FormalProducerError("FORMAL_RUNTIME_ASSET_INVALID") from error
    if (
        len(value) != metadata.st_size
        or len(value) > maximum
        or (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)
        != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    ):
        raise FormalProducerError("FORMAL_RUNTIME_ASSET_REBOUND")
    return value


def _closed_evidence_file_identity(path: Path) -> tuple[str, int]:
    try:
        before = path.lstat()
        if (
            path.is_symlink()
            or bool(getattr(path, "is_junction", lambda: False)())
            or not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_size < 1
            or before.st_size > 16 * 1024 * 1024 * 1024
        ):
            raise FormalProducerError("FORMAL_EVIDENCE_INVENTORY_INVALID")
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
        after = path.lstat()
        if (
            before.st_size != after.st_size
            or before.st_mtime_ns != after.st_mtime_ns
            or not stat.S_ISREG(after.st_mode)
            or after.st_nlink != 1
        ):
            raise FormalProducerError("FORMAL_EVIDENCE_INVENTORY_REBOUND")
        return "sha256:" + digest.hexdigest(), before.st_size
    except OSError as error:
        raise FormalProducerError("FORMAL_EVIDENCE_INVENTORY_INVALID") from error


def _closed_evidence_inventory(root: Path) -> list[dict[str, object]]:
    inventory: list[dict[str, object]] = []
    try:
        for current_value, directories, files in os.walk(root, followlinks=False):
            current = Path(current_value)
            for name in sorted(directories):
                directory = current / name
                metadata = directory.lstat()
                if (
                    directory.is_symlink()
                    or bool(getattr(directory, "is_junction", lambda: False)())
                    or not stat.S_ISDIR(metadata.st_mode)
                ):
                    raise FormalProducerError("FORMAL_EVIDENCE_INVENTORY_INVALID")
            for name in sorted(files):
                path = current / name
                relative = path.relative_to(root).as_posix()
                if relative == "SHA256SUMS":
                    continue
                digest, size = _closed_evidence_file_identity(path)
                inventory.append(
                    {"path": relative, "sha256": digest, "size": size}
                )
                if len(inventory) > 100_000:
                    raise FormalProducerError("FORMAL_EVIDENCE_INVENTORY_INVALID")
    except OSError as error:
        raise FormalProducerError("FORMAL_EVIDENCE_INVENTORY_INVALID") from error
    inventory.sort(key=lambda item: str(item["path"]))
    if not inventory:
        raise FormalProducerError("FORMAL_EVIDENCE_INVENTORY_INVALID")
    return inventory


def _parse_sha256sums(value: bytes) -> list[tuple[str, str]]:
    try:
        text = value.decode("ascii")
    except UnicodeError as error:
        raise FormalProducerError("FORMAL_EVIDENCE_SHA256SUMS_INVALID") from error
    if not text or not text.endswith("\n"):
        raise FormalProducerError("FORMAL_EVIDENCE_SHA256SUMS_INVALID")
    entries: list[tuple[str, str]] = []
    for line in text.splitlines(keepends=True):
        match = re.fullmatch(r"([0-9a-f]{64})  ([^\r\n]+)\n", line)
        if match is None:
            raise FormalProducerError("FORMAL_EVIDENCE_SHA256SUMS_INVALID")
        relative_value = match.group(2)
        relative = PurePosixPath(relative_value)
        if (
            relative.is_absolute()
            or relative_value == "SHA256SUMS"
            or "\\" in relative_value
            or any(part in {"", ".", ".."} for part in relative.parts)
            or any(ord(character) < 0x20 for character in relative_value)
        ):
            raise FormalProducerError("FORMAL_EVIDENCE_SHA256SUMS_INVALID")
        entries.append((relative_value, "sha256:" + match.group(1)))
    if entries != sorted(entries) or len({path for path, _ in entries}) != len(entries):
        raise FormalProducerError("FORMAL_EVIDENCE_SHA256SUMS_INVALID")
    canonical = "".join(
        f"{digest.removeprefix('sha256:')}  {path}\n"
        for path, digest in entries
    ).encode("ascii")
    if canonical != value:
        raise FormalProducerError("FORMAL_EVIDENCE_SHA256SUMS_INVALID")
    return entries


def _verify_and_issue_continuation_evidence_seal(
    record: _ContinuationEvidenceLifetimeRecord,
) -> ContinuationEvidenceSealSuccess:
    evidence = record.evidence_root
    seal_root = record.seal_root
    manifest_path = evidence / "SHA256SUMS"
    manifest_bytes = _read_closed_asset(manifest_path, maximum=16 * 1024 * 1024)
    entries = _parse_sha256sums(manifest_bytes)
    inventory = _closed_evidence_inventory(evidence)
    expected_entries = [
        (str(item["path"]), str(item["sha256"])) for item in inventory
    ]
    if entries != expected_entries:
        raise FormalProducerError("FORMAL_EVIDENCE_SHA256SUMS_MISMATCH")
    inventory_identity = sha256_bytes(
        canonical_json_bytes(
            {
                "schema": "animemo.continuation-evidence-inventory/v1",
                "files": inventory,
            }
        )
    )
    manifest_identity = sha256_bytes(manifest_bytes)
    seal_name = f"{evidence.name}.SHA256SUMS.sha256"
    try:
        seal_items = list(seal_root.iterdir())
    except OSError as error:
        raise FormalProducerError("FORMAL_EVIDENCE_INDEPENDENT_SEAL_INVALID") from error
    if {item.name for item in seal_items} != {seal_name}:
        raise FormalProducerError("FORMAL_EVIDENCE_INDEPENDENT_SEAL_INVALID")
    seal_path = seal_root / seal_name
    seal_bytes = _read_closed_asset(seal_path, maximum=4096)
    expected_seal_bytes = (
        f"{manifest_identity.removeprefix('sha256:')}  "
        f"{evidence.name}/SHA256SUMS\n"
    ).encode(
        "ascii"
    )
    if seal_bytes != expected_seal_bytes:
        raise FormalProducerError("FORMAL_EVIDENCE_INDEPENDENT_SEAL_INVALID")
    return _issue_continuation_evidence_seal_success(
        {
            "schema": "animemo.continuation-evidence-seal-success/v1",
            "evidenceRoot": str(evidence),
            "sha256sumsIdentity": manifest_identity,
            "independentSealIdentity": sha256_bytes(seal_bytes),
            "sealedEvidenceInventoryIdentity": inventory_identity,
            "result": "PASS",
        }
    )


def _write_closed_asset(path: Path, value: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    try:
        descriptor = os.open(path, flags, 0o600)
        with os.fdopen(descriptor, "wb") as output:
            output.write(value)
            output.flush()
            os.fsync(output.fileno())
    except OSError as error:
        raise FormalProducerError("FORMAL_RUNTIME_ASSET_INVALID") from error


def _copy_closed_asset(source: Path, target: Path, *, maximum: int) -> None:
    source_flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    source_flags |= getattr(os, "O_NOFOLLOW", 0)
    target_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    target_flags |= getattr(os, "O_BINARY", 0)
    source_descriptor = -1
    target_descriptor = -1
    try:
        before = source.lstat()
        if (
            source.is_symlink()
            or bool(getattr(source, "is_junction", lambda: False)())
            or not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_size < 1
            or before.st_size > maximum
        ):
            raise FormalProducerError("FORMAL_RUNTIME_ASSET_INVALID")
        source_descriptor = os.open(source, source_flags)
        opened = os.fstat(source_descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)
            != (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        ):
            raise FormalProducerError("FORMAL_RUNTIME_ASSET_REBOUND")
        target_descriptor = os.open(target, target_flags, 0o600)
        copied = 0
        source_digest = hashlib.sha256()
        while block := os.read(source_descriptor, 1024 * 1024):
            copied += len(block)
            if copied > maximum:
                raise FormalProducerError("FORMAL_RUNTIME_ASSET_INVALID")
            source_digest.update(block)
            view = memoryview(block)
            while view:
                written = os.write(target_descriptor, view)
                if written < 1:
                    raise FormalProducerError("FORMAL_RUNTIME_ASSET_INVALID")
                view = view[written:]
        os.fsync(target_descriptor)
        after = os.fstat(source_descriptor)
        if copied != opened.st_size or (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ) != (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns):
            raise FormalProducerError("FORMAL_RUNTIME_ASSET_REBOUND")
    except FormalProducerError:
        raise
    except OSError as error:
        raise FormalProducerError("FORMAL_RUNTIME_ASSET_INVALID") from error
    finally:
        if source_descriptor >= 0:
            os.close(source_descriptor)
        if target_descriptor >= 0:
            os.close(target_descriptor)
    try:
        rebound = source.lstat()
        target_metadata = target.lstat()
        target_digest = hashlib.sha256()
        with target.open("rb") as handle:
            while block := handle.read(1024 * 1024):
                target_digest.update(block)
    except OSError as error:
        raise FormalProducerError("FORMAL_RUNTIME_ASSET_REBOUND") from error
    if (
        source.is_symlink()
        or target.is_symlink()
        or (rebound.st_dev, rebound.st_ino, rebound.st_size, rebound.st_mtime_ns)
        != (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)
        or not stat.S_ISREG(target_metadata.st_mode)
        or target_metadata.st_nlink != 1
        or target_metadata.st_size != copied
        or target_digest.digest() != source_digest.digest()
    ):
        raise FormalProducerError("FORMAL_RUNTIME_ASSET_REBOUND")


class _FormalRuntimeSnapshot:
    """Held private runtime bytes extracted from the attested installer tar."""

    def __init__(self, root: Path, stack: ExitStack) -> None:
        self.root = root
        self._stack = stack

    def cleanup(self) -> None:
        stack = self._stack
        self._stack = ExitStack()
        try:
            stack.close()
        finally:
            shutil.rmtree(self.root, ignore_errors=True)


def _ensure_private_relative_directory(root: Path, relative: Path) -> Path:
    current = root
    for part in relative.parts:
        if not part or part in {".", ".."} or "/" in part or "\\" in part:
            raise FormalProducerError("FORMAL_RUNTIME_SNAPSHOT_INVALID")
        target = current / part
        if not target.exists():
            created = create_windows_private_directory(
                current, prefix="formal-runtime-dir"
            )
            try:
                os.rename(created, target)
            except OSError:
                shutil.rmtree(created, ignore_errors=True)
                raise
        assert_windows_private_acl(target)
        current = target
    return current


def _prepare_runtime_snapshot(
    publication_root: Path,
    authority: VerifiedFormalRcAuthority,
    *,
    installer_materials: Path,
    private_work_root: Path,
    parent_path_authority: HeldWindowsPrivatePathAuthority | None = None,
) -> tuple[_FormalRuntimeSnapshot, Path]:
    installer_materials = Path(installer_materials)
    try:
        material_identity = inspect_installer_materials(installer_materials)
    except (OSError, MaterialContractError) as error:
        raise FormalProducerError("FORMAL_RUNTIME_SOURCE_UNAVAILABLE") from error
    if material_identity.sha256 != authority.installer_materials_identity:
        raise FormalProducerError("FORMAL_RUNTIME_SOURCE_IDENTITY_MISMATCH")
    material_paths = {item.path for item in material_identity.files}
    if (
        "scripts/formal_profile_runner.py" not in material_paths
        or "scripts/closed_runtime_inventory.py" not in material_paths
        or "installer/production.py" not in material_paths
    ):
        raise FormalProducerError("FORMAL_RUNTIME_SOURCE_IDENTITY_MISMATCH")
    stack = ExitStack()
    try:
        private_work_root = Path(private_work_root).resolve(strict=True)
        assert_windows_private_acl(private_work_root)
        stack.enter_context(
            hold_windows_private_descendant_path(
                parent_path_authority,
                private_work_root,
                allow_leaf_child_writes=True,
            )
            if parent_path_authority is not None
            else hold_windows_private_path_chain(
                private_work_root, allow_leaf_child_writes=True
            )
        )
        snapshot = create_windows_private_directory(
            private_work_root, prefix="animemo-formal-runtime"
        )
        with tarfile.open(installer_materials, mode="r:") as bundle:
            members = bundle.getmembers()
            if len(members) != len(material_identity.files):
                raise FormalProducerError("FORMAL_RUNTIME_SNAPSHOT_INVALID")
            for member, identity in zip(members, material_identity.files, strict=True):
                relative = Path(member.name)
                if (
                    member.name != identity.path
                    or relative.is_absolute()
                    or ".." in relative.parts
                    or not member.name
                    or not member.isfile()
                    or member.size != identity.size
                    or stat.S_IMODE(member.mode) != identity.mode
                    or member.mtime != 0
                    or member.uid != 0
                    or member.gid != 0
                    or member.uname != ""
                    or member.gname != ""
                ):
                    raise FormalProducerError("FORMAL_RUNTIME_SNAPSHOT_INVALID")
                target = snapshot.joinpath(*relative.parts)
                target.resolve(strict=False).relative_to(snapshot.resolve(strict=True))
                _ensure_private_relative_directory(snapshot, relative.parent)
                source = bundle.extractfile(member)
                if source is None:
                    raise FormalProducerError("FORMAL_RUNTIME_SNAPSHOT_INVALID")
                flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
                descriptor = os.open(target, flags, 0o600)
                digest = hashlib.sha256()
                written = 0
                with os.fdopen(descriptor, "wb") as output:
                    while chunk := source.read(1024 * 1024):
                        written += len(chunk)
                        if written > identity.size:
                            raise FormalProducerError("FORMAL_RUNTIME_SNAPSHOT_INVALID")
                        digest.update(chunk)
                        output.write(chunk)
                    output.flush()
                    os.fsync(output.fileno())
                if (
                    written != identity.size
                    or "sha256:" + digest.hexdigest() != identity.sha256
                ):
                    raise FormalProducerError("FORMAL_RUNTIME_SNAPSHOT_INVALID")
                target.chmod(identity.mode)
                assert_windows_private_acl(target)
        if inspect_installer_materials(installer_materials) != material_identity:
            raise FormalProducerError("FORMAL_RUNTIME_ASSET_REBOUND")
        authority_path = snapshot / "formal-rc-authority.json"
        _write_closed_asset(
            authority_path,
            canonical_json_bytes(
                {**authority.identity_body(), "identity": authority.identity}
            ),
        )
        assert_windows_private_acl(authority_path)
        for name in (
            f"animemo-{authority.rc_tag}-portable.tar",
            "release-attestation.sigstore.json",
        ):
            source = publication_root / name
            if not source.exists():
                continue
            maximum = (
                4 * 1024 * 1024 * 1024
                if name.endswith("-portable.tar")
                else 16 * 1024 * 1024
            )
            target = snapshot / name
            if target.exists():
                raise FormalProducerError("FORMAL_RUNTIME_ASSET_COLLISION")
            _copy_closed_asset(source, target, maximum=maximum)
            assert_windows_private_acl(target)
        publication_receipt = {
            "schema": "animemo.formal-publication-preflight/v1",
            "publication_authority_identity": authority.publication_identity,
            "publication_execution_receipt_identity": (
                authority.publication_execution_receipt_identity
            ),
            "publication_signed_claim_identity": (
                authority.publication_signed_claim_identity
            ),
            "publication_signed_at": authority.publication_signed_at,
            "formal_windows_pretrust_kit_identity": (
                authority.formal_windows_pretrust_kit_identity
            ),
            "offline_release_trust_profile_identity": (
                authority.offline_release_trust_profile_identity
            ),
            "pretrusted_profile_identity": authority.pretrusted_profile_identity,
            "provenance_verifier_identity": (authority.provenance_verifier_identity),
            "github_trusted_root_identity": (authority.github_trusted_root_identity),
            "sigstore_trusted_root_identity": (
                authority.sigstore_trusted_root_identity
            ),
            "release_authority_granted": False,
            "publish_authorized": False,
        }
        publication_path = snapshot / "formal-publication-preflight.json"
        if publication_path.exists() or publication_path.is_symlink():
            raise FormalProducerError("FORMAL_RUNTIME_ASSET_COLLISION")
        _write_closed_asset(publication_path, canonical_json_bytes(publication_receipt))
        assert_windows_private_acl(publication_path)
        relative_files = tuple(
            sorted(
                item.relative_to(snapshot).as_posix()
                for item in snapshot.rglob("*")
                if item.is_file()
            )
        )
        stack.enter_context(
            hold_windows_private_snapshot(snapshot, relative_files=relative_files)
        )
        if inspect_installer_materials(installer_materials) != material_identity:
            raise FormalProducerError("FORMAL_RUNTIME_ASSET_REBOUND")
    except (
        OSError,
        tarfile.TarError,
        MaterialContractError,
        FormalWindowsPretrustError,
    ) as error:
        stack.close()
        if "snapshot" in locals():
            shutil.rmtree(snapshot, ignore_errors=True)
        raise FormalProducerError("FORMAL_RUNTIME_SNAPSHOT_INVALID") from error
    except Exception:
        stack.close()
        if "snapshot" in locals():
            shutil.rmtree(snapshot, ignore_errors=True)
        raise
    return _FormalRuntimeSnapshot(snapshot, stack), snapshot


def _provider_plan(
    authority: VerifiedFormalRcAuthority,
    provider: ClosedVmwareProvider,
) -> ClosedVmProviderPlan:
    readiness = provider.inspect_readiness()
    if readiness != ProviderReadinessReceipt.issue(
        ssh_digest=EXPECTED_SSH_SHA256,
        scp_digest=EXPECTED_SCP_SHA256,
    ):
        raise FormalProducerError("FORMAL_VM_PROVIDER_READINESS_FAILED")
    source = _validate_source_evidence(provider.inspect_source())
    source_vm_digest = sha256_bytes(
        canonical_json_bytes(dict(sorted(source.original_hashes.items())))
    )
    observed_source_authority_identity = _candidate_source_vm_authority_identity(
        base_vm_identity=source_vm_digest,
        original_vm_hashes=source.original_hashes,
        snapshot_identities=source.snapshot_identities,
        source_disk_graph_identity=source.source_disk_graph_identity,
        snapshot_disk_graph_identities=source.snapshot_disk_graph_identities,
        source_vm_inventory_identity=source.source_vm_inventory_identity,
    )
    if (
        source_vm_digest != authority.candidate_base_vm_identity
        or dict(source.original_hashes)
        != dict(authority.candidate_original_vm_hashes)
        or dict(source.snapshot_identities)
        != dict(authority.candidate_snapshot_identities)
        or source.source_disk_graph_identity
        != authority.candidate_source_disk_graph_identity
        or dict(source.snapshot_disk_graph_identities)
        != dict(authority.candidate_snapshot_disk_graph_identities)
        or source.source_vm_inventory_identity
        != authority.candidate_source_vm_inventory_identity
        or observed_source_authority_identity
        != authority.candidate_source_vm_authority_identity
    ):
        raise FormalProducerError("FORMAL_VM_SOURCE_AUTHORITY_MISMATCH")
    session_id = uuid4().hex
    profiles: list[VmProviderProfilePlan] = []
    for provider_profile in PROFILES:
        nonce = secrets.token_hex(32)
        body = {
            "formalAuthorityIdentity": authority.identity,
            "profile": provider_profile,
            "snapshotIdentity": source.snapshot_identities[provider_profile],
            "snapshotDiskGraphIdentity": (
                source.snapshot_disk_graph_identities[provider_profile]
            ),
            "sourceDiskGraphIdentity": source.source_disk_graph_identity,
            "sourceVmDigest": source_vm_digest,
            "providerReadinessReceiptDigest": readiness.receipt_digest,
            "sessionId": session_id,
            "connectionNonce": nonce,
        }
        clone_identity = sha256_bytes(canonical_json_bytes(body))
        profiles.append(
            VmProviderProfilePlan(
                profile=provider_profile,
                snapshot_name=SNAPSHOT_ALLOWLIST[provider_profile],
                snapshot_identity=source.snapshot_identities[provider_profile],
                snapshot_disk_graph_identity=(
                    source.snapshot_disk_graph_identities[provider_profile]
                ),
                clone_identity=clone_identity,
                provider_readiness_receipt_digest=readiness.receipt_digest,
                session_id=session_id,
                connection_nonce=nonce,
                ssh_host_key_alias=(
                    "animemo-"
                    + session_id[:12]
                    + "-formal-"
                    + provider_profile.lower().replace("_", "-")
                    + "-"
                    + clone_identity.removeprefix("sha256:")[:12]
                ),
            )
        )
    provisional = ClosedVmProviderPlan(
        purpose="FORMAL_POSTPUBLICATION",
        authority_digest=authority.identity,
        source_sha=authority.source_sha,
        source_tree=authority.source_tree,
        target_version=authority.rc_tag,
        source_vm_identity=source.vm_identity,
        source_vm_digest=source_vm_digest,
        source_disk_graph_identity=source.source_disk_graph_identity,
        source_vm_inventory_identity=source.source_vm_inventory_identity,
        original_vm_hashes=dict(source.original_hashes),
        profiles=tuple(profiles),
        provider_readiness_receipt_digest=readiness.receipt_digest,
        session_id=session_id,
        plan_digest="",
    )
    return ClosedVmProviderPlan(
        **{
            **provisional.__dict__,
            "plan_digest": sha256_bytes(
                canonical_json_bytes(provisional.identity_body())
            ),
        }
    )


class ClosedFormalVmProfileExecutor:
    """Adapter that reuses the exact Candidate VM provider capability."""

    def __init__(
        self,
        *,
        authority_root: Path,
        provider: ClosedVmwareProvider,
        installer_materials: Path | None = None,
        private_work_root: Path | None = None,
        parent_path_authority: HeldWindowsPrivatePathAuthority | None = None,
    ) -> None:
        self._authority_root = Path(authority_root)
        self._installer_materials = (
            self._authority_root / "installer-materials.tar"
            if installer_materials is None
            else Path(installer_materials)
        )
        self._provider = provider
        self._private_work_root = (
            self._authority_root
            if private_work_root is None
            else Path(private_work_root)
        )
        self._parent_path_authority = parent_path_authority
        self._authority_identity: str | None = None
        self._plan: ClosedVmProviderPlan | None = None
        self._snapshot_temporary: _FormalRuntimeSnapshot | None = None
        self._staging_root: Path | None = None

    def cleanup(self) -> None:
        temporary = self._snapshot_temporary
        self._snapshot_temporary = None
        self._staging_root = None
        self._plan = None
        self._authority_identity = None
        if temporary is not None:
            temporary.cleanup()

    def _plan_for(self, authority: VerifiedFormalRcAuthority) -> ClosedVmProviderPlan:
        if self._plan is None:
            self._snapshot_temporary, self._staging_root = _prepare_runtime_snapshot(
                self._authority_root,
                authority,
                installer_materials=self._installer_materials,
                private_work_root=self._private_work_root,
                parent_path_authority=self._parent_path_authority,
            )
            self._plan = _provider_plan(authority, self._provider)
            self._authority_identity = authority.identity
        if self._authority_identity != authority.identity:
            raise FormalProducerError("FORMAL_VM_AUTHORITY_REBOUND")
        return self._plan

    @staticmethod
    def _draft(
        value: object,
        *,
        authority: VerifiedFormalRcAuthority,
        profile: str,
    ) -> dict[str, Any]:
        if (
            type(value) is not dict
            or value.get("schema") != "animemo.formal-profile-observation-draft/v1"
            or value.get("version") != 1
            or value.get("profile") != profile
            or value.get("rc_authority_identity") != authority.identity
            or value.get("result") not in {"PASS", "FAIL"}
            or value.get("release_authority_granted") is not False
            or value.get("publish_authorized") is not False
        ):
            raise FormalProducerError("FORMAL_VM_PROFILE_DRAFT_INVALID")
        return dict(value)

    def execute(
        self,
        *,
        authority: VerifiedFormalRcAuthority,
        profile: str,
    ) -> FormalProfileObservation:
        if profile not in FORMAL_PROFILES:
            raise FormalProducerError("FORMAL_PROFILE_INVALID")
        plan = self._plan_for(authority)
        provider_profile = _FORMAL_TO_PROVIDER[profile]
        item = next(
            value for value in plan.profiles if value.profile == provider_profile
        )
        workload = ClosedFormalProfileWorkload.issue(
            authority_root=self._staging_root,
            authority_identity=authority.identity,
            formal_profile=profile,
            runtime_source_tree=authority.source_tree,
        )
        try:
            value = self._provider.execute_formal_profile(
                plan=item,
                harness_plan=plan,
                workload=workload,
                initial_platform_state=_initial_platform_state(provider_profile),
            )
        except CandidateProfileExecutionError as error:
            raise FormalProfileExecutionError(
                "FORMAL_PROFILE_EXECUTION_FAILED",
                continuation_safe=True,
                continuation_receipt_digest=(error.continuation_receipt.receipt_digest),
            ) from error
        except CandidateHarnessError as error:
            raise FormalProducerError("FORMAL_VM_PROVIDER_FAILED") from error
        draft = self._draft(value, authority=authority, profile=profile)
        try:
            original_hashes = dict(self._provider.inspect_original_hashes())
            continuation = self._provider.inspect_profile_continuation(
                plan=item, harness_plan=plan
            )
            _validate_continuation_receipt(continuation, profile=item, plan=plan)
            provider_execution = self._provider.inspect_execution_authority()
        except CandidateHarnessError as error:
            raise FormalProducerError("FORMAL_VM_CONTINUATION_UNVERIFIED") from error
        if original_hashes != dict(plan.original_vm_hashes):
            raise FormalProducerError("FORMAL_VM_SOURCE_MUTATED")
        if (
            provider_execution.result != "PASS"
            or provider_execution.source_vm_inventory_identity
            != plan.source_vm_inventory_identity
        ):
            raise FormalProducerError("FORMAL_VM_EXECUTION_AUTHORITY_REBOUND")
        release = draft.get("resolved_release")
        canonical = draft.get("canonical_acceptance_receipt_digests")
        if (
            type(release) is not dict
            or type(canonical) is not list
            or len(canonical) > 3
            or (draft["result"] == "PASS" and len(canonical) != 3)
        ):
            raise FormalProducerError("FORMAL_VM_PROFILE_DRAFT_INVALID")
        return FormalProfileObservation(
            profile=profile,
            rc_authority_identity=authority.identity,
            transport_source=draft["transport_source"],
            resolved_version=release["version"],
            resolved_source_sha=release["source_sha"],
            resolved_manifest_identity=release["release_manifest_identity"],
            resolved_deployment_contract_identity=release[
                "deployment_contract_identity"
            ],
            resolved_installer_materials_identity=release[
                "installer_materials_identity"
            ],
            resolved_api_digest=release["api_digest"],
            resolved_web_digest=release["web_digest"],
            resolved_publication_identity=release["publication_identity"],
            resolved_workflow_identity=release["workflow_identity"],
            resolved_attestation_claim_identities=release[
                "attestation_claim_identities"
            ],
            base_vm_identity=plan.source_vm_digest,
            snapshot_identity=item.snapshot_identity,
            clone_identity=item.clone_identity,
            provider_execution_authority_receipt_digest=(
                provider_execution.receipt_digest
            ),
            publication_execution_receipt_identity=draft[
                "publication_execution_receipt_identity"
            ],
            publication_signed_claim_identity=draft[
                "publication_signed_claim_identity"
            ],
            publication_signed_at=draft["publication_signed_at"],
            formal_windows_pretrust_kit_identity=draft[
                "formal_windows_pretrust_kit_identity"
            ],
            offline_release_trust_profile_identity=draft[
                "offline_release_trust_profile_identity"
            ],
            pretrusted_profile_identity=draft["pretrusted_profile_identity"],
            provenance_verifier_identity=draft["provenance_verifier_identity"],
            github_trusted_root_identity=draft["github_trusted_root_identity"],
            sigstore_trusted_root_identity=draft["sigstore_trusted_root_identity"],
            platform_plan_digest=draft["platform_plan_digest"],
            platform_receipt_digest=draft["platform_receipt_digest"],
            installer_plan_digest=draft["installer_plan_digest"],
            installer_execution_receipt_digest=draft[
                "installer_execution_receipt_digest"
            ],
            doctor_receipt_digest=draft["doctor_receipt_digest"],
            canonical_acceptance_receipt_digests=tuple(canonical),
            continuation_receipt_digest=continuation.receipt_digest,
            result=draft["result"],
        )


def _output_files(result: Mapping[str, object]) -> dict[str, object]:
    files = {
        "formal-aggregate-receipt.json": result["aggregateReceipt"],
        "formal-execution-receipt.json": result["executionReceipt"],
    }
    for profile, receipt in result["profileReceipts"].items():
        files[profile.lower().replace("_", "-") + "-receipt.json"] = receipt
    for profile, receipt in result.get("profileStatusReceipts", {}).items():
        name = profile.lower().replace("_", "-") + "-receipt.json"
        if name in files:
            raise FormalProducerError("FORMAL_OUTPUT_PROFILE_RECEIPT_COLLISION")
        files[name] = receipt
    if result.get("rcLiveAcceptanceInput") is not None:
        files["formal-rc-live-acceptance-input.json"] = result["rcLiveAcceptanceInput"]
    if result.get("rcLiveAcceptanceRecord") is not None:
        record = result["rcLiveAcceptanceRecord"]
        files[str(record["rc_tag"]) + ".json"] = record
    return files


class _FormalOutputTransaction:
    def __init__(
        self,
        root: Path,
        *,
        request: FormalAuthorityRequest,
        execution: FormalExecutionContext,
        candidate_aggregate_receipt_digest: str,
        candidate_profile_receipt_digests: Mapping[str, str],
        candidate_source_vm_authority_identity: str,
        candidate_material_authority_identity: str,
        candidate_material_tree_inventory_identity: str,
        parent_path_authority: HeldWindowsPrivatePathAuthority | None = None,
    ) -> None:
        self.root = Path(root)
        self.staging: Path | None = None
        self.reused = False
        self.existing_status: str | None = None
        self.existing_retryable = False
        self._request = request
        self._execution = execution
        self._candidate_aggregate_receipt_digest = (
            candidate_aggregate_receipt_digest
        )
        self._candidate_profile_receipt_digests = dict(
            candidate_profile_receipt_digests
        )
        self._candidate_source_vm_authority_identity = (
            candidate_source_vm_authority_identity
        )
        self._candidate_material_authority_identity = (
            candidate_material_authority_identity
        )
        self._candidate_material_tree_inventory_identity = (
            candidate_material_tree_inventory_identity
        )
        self._parent_path_authority = parent_path_authority
        if (
            _DIGEST.fullmatch(candidate_aggregate_receipt_digest) is None
            or set(self._candidate_profile_receipt_digests)
            != {"fresh_base", "docker_base", "runtime_base_offline"}
            or any(
                _DIGEST.fullmatch(value) is None
                for value in self._candidate_profile_receipt_digests.values()
            )
            or _DIGEST.fullmatch(candidate_source_vm_authority_identity) is None
            or _DIGEST.fullmatch(candidate_material_authority_identity) is None
            or _DIGEST.fullmatch(candidate_material_tree_inventory_identity) is None
        ):
            raise FormalProducerError("FORMAL_OUTPUT_CANDIDATE_AUTHORITY_INVALID")
        self._parent_hold: Any | None = None
        try:
            parent = self.root.parent.resolve(strict=True)
            parent_metadata = parent.lstat()
            if (
                parent.is_symlink()
                or bool(getattr(parent, "is_junction", lambda: False)())
                or not stat.S_ISDIR(parent_metadata.st_mode)
            ):
                raise FormalProducerError("FORMAL_OUTPUT_ROOT_INVALID")
            assert_windows_private_acl(parent)
            self._parent_hold = (
                hold_windows_private_descendant_path(
                    parent_path_authority,
                    parent,
                    allow_leaf_child_writes=True,
                )
                if parent_path_authority is not None
                else hold_windows_private_path_chain(
                    parent, allow_leaf_child_writes=True
                )
            )
            self._parent_hold.__enter__()
            self.root = parent / self.root.name
            if self.root.exists() or self.root.is_symlink():
                self._validate_existing(request=request, execution=execution)
                self.reused = True
                return
            self.staging = create_windows_private_directory(
                parent,
                prefix="animemo-formal-output",
            )
        except FormalProducerError:
            self._release_parent_hold()
            raise
        except (OSError, ValueError, FormalWindowsPretrustError) as error:
            self._release_parent_hold()
            raise FormalProducerError("FORMAL_OUTPUT_ROOT_INVALID") from error

    def _release_parent_hold(self) -> None:
        hold = self._parent_hold
        self._parent_hold = None
        if hold is not None:
            try:
                hold.__exit__(None, None, None)
            except (OSError, FormalWindowsPretrustError) as error:
                raise FormalProducerError("FORMAL_OUTPUT_ROOT_INVALID") from error

    @staticmethod
    def _read_json(path: Path) -> tuple[dict[str, Any], bytes]:
        value = _read_closed_asset(path, maximum=16 * 1024 * 1024)
        try:
            decoded = json.loads(value)
        except (UnicodeError, json.JSONDecodeError) as error:
            raise FormalProducerError("FORMAL_OUTPUT_ROOT_INVALID") from error
        if type(decoded) is not dict or canonical_json_bytes(decoded) != value:
            raise FormalProducerError("FORMAL_OUTPUT_ROOT_INVALID")
        return decoded, value

    @staticmethod
    def _pass_names(rc_tag: str) -> set[str]:
        return {
            "formal-aggregate-receipt.json",
            "formal-execution-receipt.json",
            "formal-fresh-receipt.json",
            "formal-docker-receipt.json",
            "formal-offline-receipt.json",
            "formal-rc-live-acceptance-input.json",
            f"{rc_tag}.json",
        }

    @staticmethod
    def _base_names() -> set[str]:
        return {
            "formal-aggregate-receipt.json",
            "formal-execution-receipt.json",
        }

    @staticmethod
    def _profile_name(profile: str) -> str:
        return profile.lower().replace("_", "-") + "-receipt.json"

    def _validate_existing(
        self,
        *,
        request: FormalAuthorityRequest,
        execution: FormalExecutionContext,
    ) -> None:
        try:
            names = {item.name for item in self.root.iterdir()}
            allowed = self._pass_names(request.rc_tag)
            if (
                not self._base_names().issubset(names)
                or not names.issubset(allowed)
            ):
                raise FormalProducerError("FORMAL_OUTPUT_ROOT_INVALID")
            with hold_windows_private_snapshot(
                self.root,
                relative_files=tuple(sorted(names)),
            ):
                self._validate_existing_held(request=request, execution=execution)
        except FormalProducerError:
            raise
        except (OSError, FormalWindowsPretrustError) as error:
            raise FormalProducerError("FORMAL_OUTPUT_ROOT_INVALID") from error

    def _validate_existing_held(
        self,
        *,
        request: FormalAuthorityRequest,
        execution: FormalExecutionContext,
    ) -> None:
        try:
            metadata = self.root.lstat()
            names = {item.name for item in self.root.iterdir()}
        except OSError as error:
            raise FormalProducerError("FORMAL_OUTPUT_ROOT_INVALID") from error
        if (
            self.root.is_symlink()
            or bool(getattr(self.root, "is_junction", lambda: False)())
            or not stat.S_ISDIR(metadata.st_mode)
            or not self._base_names().issubset(names)
            or not names.issubset(self._pass_names(request.rc_tag))
        ):
            raise FormalProducerError("FORMAL_OUTPUT_ROOT_INVALID")
        profile_values = {
            profile: self._read_json(
                self.root / self._profile_name(profile)
            )[0]
            for profile in FORMAL_PROFILES
            if self._profile_name(profile) in names
        }
        aggregate = self._read_json(
            self.root / "formal-aggregate-receipt.json"
        )[0]
        execution_receipt = self._read_json(
            self.root / "formal-execution-receipt.json"
        )[0]
        bundle: dict[str, Any] | None = None
        record: dict[str, Any] | None = None
        try:
            closed_profiles = {}
            closed_status_receipts = {}
            for profile, profile_value in profile_values.items():
                if profile_value.get("schema") == (
                    "animemo.formal-profile-receipt/v1"
                ):
                    closed_profiles[profile] = validate_formal_profile_receipt(
                        profile_value
                    )
                else:
                    closed_status_receipts[profile] = (
                        validate_formal_profile_status_receipt(profile_value)
                    )
            closed_aggregate = validate_formal_aggregate_receipt(aggregate)
            receipt = validate_formal_execution_receipt(execution_receipt)
            if receipt["result"] == "PASS":
                if names != self._pass_names(request.rc_tag):
                    raise FormalProducerError("FORMAL_OUTPUT_ROOT_INVALID")
                bundle = {
                    "rcLiveAcceptanceInput": self._read_json(
                        self.root / "formal-rc-live-acceptance-input.json"
                    )[0],
                    "profileReceipts": closed_profiles,
                    "aggregateReceipt": closed_aggregate,
                    "executionReceipt": receipt,
                }
                record = self._read_json(self.root / f"{request.rc_tag}.json")[0]
                closed_bundle = validate_formal_acceptance_bundle(bundle)
                closed_record = validate_rc_live_acceptance(record)
            else:
                if (
                    receipt["result"] != "FAIL"
                    or closed_aggregate["result"] != "FAIL"
                    or "formal-rc-live-acceptance-input.json" in names
                    or f"{request.rc_tag}.json" in names
                    or set(profile_values) != set(FORMAL_PROFILES)
                ):
                    raise FormalProducerError("FORMAL_OUTPUT_ROOT_INVALID")
                closed_bundle = None
                closed_record = None
        except (AcceptanceError, FormalProducerError) as error:
            raise FormalProducerError("FORMAL_OUTPUT_ROOT_INVALID") from error
        request_body = request.identity_body()
        acceptance_input = (
            closed_bundle["rcLiveAcceptanceInput"]
            if closed_bundle is not None
            else None
        )
        context_fields = {
            "accepted_at": execution.accepted_at,
            "observed_at": execution.observed_at,
            "operator_identity": execution.operator_identity,
            "run_id": execution.run_id,
            "run_attempt": execution.run_attempt,
            "correlation_id": execution.correlation_id,
            "current_workflow_commit": execution.current_workflow_commit,
            "execution_environment": execution.execution_environment,
            "tool_identity": execution.tool_identity,
        }
        input_fields = {
            "repository": request_body["repository"],
            "rc_tag": request_body["rc_tag"],
            "verified_candidate_digest": request_body[
                "verified_candidate_digest"
            ],
            "source_sha": request_body["source_sha"],
            "source_tree": request_body["source_tree"],
            "release_manifest_identity": request_body["release_manifest_identity"],
            "deployment_contract_identity": request_body[
                "deployment_contract_identity"
            ],
            "installer_materials_identity": request_body[
                "installer_materials_identity"
            ],
            "formal_windows_pretrust_kit_identity": request_body[
                "formal_windows_pretrust_kit_identity"
            ],
            "offline_release_trust_profile_identity": request_body[
                "offline_release_trust_profile_identity"
            ],
            "api_digest": request_body["api_digest"],
            "web_digest": request_body["web_digest"],
            "publication_identity": request_body["publication_identity"],
            "workflow_identity": request_body["workflow_identity"],
            "attestation_claim_identities": request_body[
                "attestation_claim_identities"
            ],
        }
        expected_profile_receipt_digests = {
            FORMAL_PROFILE_RESULT_KEYS[profile]: (
                closed_profiles[profile]["receipt_digest"]
                if profile in closed_profiles
                else None
            )
            for profile in FORMAL_PROFILES
        }
        expected_profile_execution_digests = {
            FORMAL_PROFILE_RESULT_KEYS[profile]: (
                closed_profiles[profile]["execution_receipt_digest"]
                if profile in closed_profiles
                else None
            )
            for profile in FORMAL_PROFILES
        }
        expected_profile_authorities = {
            FORMAL_PROFILE_RESULT_KEYS[profile]: (
                closed_profiles[profile]["profile_authority_identity"]
                if profile in closed_profiles
                else None
            )
            for profile in FORMAL_PROFILES
        }
        outcome_binding_invalid = any(
            (
                closed_aggregate["profile_results"][key]["receipt_digest"]
                != expected_profile_receipt_digests[key]
                or (
                    profile in closed_profiles
                    and closed_aggregate["profile_results"][key]["status"]
                    != closed_profiles[profile]["result"]
                )
                or (
                    profile not in closed_profiles
                    and (
                        profile not in closed_status_receipts
                        or closed_status_receipts[profile]["profile"] != profile
                        or closed_status_receipts[profile]["rc_authority_identity"]
                        != closed_aggregate["rc_authority_identity"]
                        or closed_status_receipts[profile]["status"]
                        != closed_aggregate["profile_results"][key]["status"]
                        or closed_status_receipts[profile]["failure_code"]
                        != closed_aggregate["profile_results"][key]["failure_code"]
                        or closed_status_receipts[profile][
                            "continuation_receipt_digest"
                        ]
                        != closed_aggregate["profile_results"][key][
                            "continuation_receipt_digest"
                        ]
                        or closed_aggregate["profile_results"][key]["status"]
                        not in {"ERROR", "NOT_RUN_SHARED_BLOCKER"}
                    )
                )
            )
            for profile, key in FORMAL_PROFILE_RESULT_KEYS.items()
        )
        if (
            (
                acceptance_input is not None
                and any(
                    acceptance_input.get(key) != value
                    for key, value in input_fields.items()
                )
            )
            or any(receipt.get(key) != value for key, value in context_fields.items())
            or receipt.get("verified_candidate_digest")
            != request_body["verified_candidate_digest"]
            or receipt.get("candidate_aggregate_receipt_digest")
            != self._candidate_aggregate_receipt_digest
            or receipt.get("candidate_profile_receipt_digests")
            != self._candidate_profile_receipt_digests
            or receipt.get("candidate_source_vm_authority_identity")
            != self._candidate_source_vm_authority_identity
            or receipt.get("candidate_material_authority_identity")
            != self._candidate_material_authority_identity
            or receipt.get("candidate_material_tree_inventory_identity")
            != self._candidate_material_tree_inventory_identity
            or closed_aggregate["formal_execution_receipt_digest"]
            != receipt["receipt_digest"]
            or closed_aggregate["profile_authority_identities"]
            != expected_profile_authorities
            or receipt["profile_execution_receipt_digests"]
            != expected_profile_execution_digests
            or receipt["profile_results"] != closed_aggregate["profile_results"]
            or outcome_binding_invalid
            or (
                closed_record is not None
                and closed_record.get("formal_evidence") != closed_bundle
            )
        ):
            raise FormalProducerError("FORMAL_OUTPUT_ROOT_CONFLICT")
        self.existing_status = receipt["result"]
        self.existing_retryable = receipt["result"] == "FAIL" and any(
            value["status"] in {"ERROR", "NOT_RUN_SHARED_BLOCKER"}
            for value in receipt["profile_results"].values()
        )

    def commit(self, result: Mapping[str, object]) -> None:
        if self.reused:
            return
        staging = self.staging
        if staging is None:
            raise FormalProducerError("FORMAL_OUTPUT_TRANSACTION_INVALID")
        files = _output_files(result)
        encoded_files = {
            name: canonical_json_bytes(value) for name, value in files.items()
        }
        expected_file_identities = {
            name: sha256_bytes(value) for name, value in encoded_files.items()
        }
        renamed = False
        try:
            for name, encoded in sorted(encoded_files.items()):
                target = staging / name
                _write_closed_asset(target, encoded)
                assert_windows_private_acl(target)
                if _read_closed_asset(target, maximum=16 * 1024 * 1024) != encoded:
                    raise FormalProducerError("FORMAL_OUTPUT_WRITE_FAILED")
            assert_windows_private_acl(staging)
            with hold_windows_private_snapshot(
                staging, relative_files=tuple(sorted(files))
            ):
                observed_staging = {
                    item.name: _read_closed_asset(item, maximum=16 * 1024 * 1024)
                    for item in staging.iterdir()
                }
                if observed_staging != encoded_files:
                    raise FormalProducerError("FORMAL_OUTPUT_WRITE_FAILED")
            if os.name != "nt":
                descriptor = os.open(staging, os.O_RDONLY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
            if self.root.exists() or self.root.is_symlink():
                observed = {
                    item.name: _read_closed_asset(item, maximum=16 * 1024 * 1024)
                    for item in self.root.iterdir()
                }
                if observed != encoded_files:
                    raise FormalProducerError("FORMAL_OUTPUT_ROOT_CONFLICT")
                self.cleanup()
                return
            # The output parent was held throughout staging.  Its legacy
            # no-share-delete leaf handle would itself veto a child-directory
            # rename on Windows; the commit helper replaces it with the
            # dedicated protected-parent/identity transaction for the atomic
            # rename and immediate held readback.
            self._release_parent_hold()
            with commit_windows_private_directory_snapshot(
                staging,
                self.root,
                relative_files=tuple(sorted(encoded_files)),
                expected_file_identities=expected_file_identities,
            ):
                renamed = True
                self.staging = None
                assert_windows_private_acl(self.root)
                self._validate_existing_held(
                    request=self._request,
                    execution=self._execution,
                )
            self._release_parent_hold()
        except FormalProducerError:
            if renamed:
                shutil.rmtree(self.root, ignore_errors=True)
            self.cleanup()
            raise
        except (OSError, FormalWindowsPretrustError) as error:
            if renamed:
                shutil.rmtree(self.root, ignore_errors=True)
            self.cleanup()
            raise FormalProducerError("FORMAL_OUTPUT_WRITE_FAILED") from error

    def cleanup(self) -> None:
        staging = self.staging
        self.staging = None
        if staging is not None:
            shutil.rmtree(staging, ignore_errors=True)
        self._release_parent_hold()


class _PreverifiedFormalAuthority:
    """One-use verifier seam after the production preflight has completed."""

    def __init__(
        self,
        request: FormalAuthorityRequest,
        authority: VerifiedFormalRcAuthority,
    ) -> None:
        self._request = request
        self._authority = authority
        self._used = False

    def verify(self, request: FormalAuthorityRequest) -> VerifiedFormalRcAuthority:
        if self._used or request != self._request:
            raise FormalProducerError("FORMAL_RC_AUTHORITY_REBOUND")
        self._used = True
        return self._authority


class ReleaseContinuationParentWorker:
    """One-process, non-serializable Candidate→publication→Formal authority.

    Candidate results and the Qualified capability never cross a JSON/file
    boundary.  Credential cleanup hooks are deliberately retained only as
    callables; credential values are never accepted or stored by this object.
    """

    __slots__ = (
        "_candidate_started",
        "_clear_r2_credentials",
        "_clear_sudo_credentials",
        "_formal_attempts",
        "_formal_output_roots",
        "_formal_terminal",
        "_lifetime_authority",
        "_lifetime_closed",
        "_lifetime_entered",
        "_qualified_candidate",
        "_r2_cleared",
        "_sudo_cleared",
    )

    def __init__(
        self,
        *,
        clear_r2_credentials: Any,
        clear_sudo_credentials: Any,
        continuation_lifetime_authority: ContinuationEvidenceLifetimeAuthority,
    ) -> None:
        if not callable(clear_r2_credentials) or not callable(
            clear_sudo_credentials
        ) or type(continuation_lifetime_authority) is not (
            ContinuationEvidenceLifetimeAuthority
        ):
            raise FormalProducerError("FORMAL_PARENT_CLEANUP_INVALID")
        self._clear_r2_credentials = clear_r2_credentials
        self._clear_sudo_credentials = clear_sudo_credentials
        self._lifetime_authority = continuation_lifetime_authority
        self._lifetime_entered = False
        self._lifetime_closed = False
        self._candidate_started = False
        self._formal_attempts = 0
        self._formal_output_roots: set[Path] = set()
        self._formal_terminal = False
        self._qualified_candidate: QualifiedCandidateFormalAuthority | None = None
        self._r2_cleared = False
        self._sudo_cleared = False

    @property
    def credential_persistence(self) -> str:
        return "NONE"

    def __reduce__(self) -> object:
        raise TypeError("ReleaseContinuationParentWorker cannot be serialized")

    def _clear_r2_once(self) -> None:
        if not self._r2_cleared:
            self._clear_r2_credentials()
            self._r2_cleared = True

    def _clear_sudo_once(self) -> None:
        if not self._sudo_cleared:
            self._clear_sudo_credentials()
            self._sudo_cleared = True

    def _release_lifetime_authority(self) -> None:
        if self._lifetime_closed:
            return
        authority = self._lifetime_authority
        if self._lifetime_entered:
            authority.__exit__(None, None, None)
        self._lifetime_closed = True

    def _close_qualified_candidate(self) -> None:
        qualified = self._qualified_candidate
        if qualified is not None:
            qualified.close()
            self._qualified_candidate = None

    @staticmethod
    def _formal_result_retryable(result: Mapping[str, object]) -> bool:
        if result.get("status") != "FAIL":
            return False
        if result.get("retryable") is True:
            return True
        execution = result.get("executionReceipt")
        if type(execution) is not dict:
            return False
        profile_results = execution.get("profile_results")
        return type(profile_results) is dict and any(
            type(value) is dict
            and value.get("status") in {"ERROR", "NOT_RUN_SHARED_BLOCKER"}
            for value in profile_results.values()
        )

    def run_candidate(
        self,
        *,
        verified_candidate_digest: str,
        expected_qualification_run_id: int,
        expected_source_sha: str,
        expected_source_tree: str,
        provider: ClosedVmwareProvider,
        authorize_plan: Any,
        environment: Mapping[str, str] | None = None,
        r2_client: object | None = None,
        state_root: Path | None = None,
        private_material_parent: Path | None = None,
    ) -> Mapping[str, object]:
        if self._candidate_started or self._qualified_candidate is not None:
            raise FormalProducerError("FORMAL_PARENT_CANDIDATE_ALREADY_USED")
        if not self._lifetime_entered:
            raise FormalProducerError("FORMAL_PARENT_LIFETIME_NOT_HELD")
        if private_material_parent is None:
            raise FormalProducerError("FORMAL_PARENT_MATERIAL_ROOT_REQUIRED")
        private_material_parent = self._lifetime_authority.require_contained(
            private_material_parent,
            name="MATERIAL_ROOT",
        )
        if state_root is not None:
            state_root = self._lifetime_authority.require_contained(
                state_root,
                name="CANDIDATE_STATE_ROOT",
            )
        self._candidate_started = True
        try:
            continuation = execute_candidate_controller_for_formal(
                verified_candidate_digest=verified_candidate_digest,
                expected_qualification_run_id=expected_qualification_run_id,
                expected_source_sha=expected_source_sha,
                expected_source_tree=expected_source_tree,
                provider=provider,
                authorize_plan=authorize_plan,
                environment=environment,
                r2_client=r2_client,
                _state_root=state_root,
                _private_material_parent=private_material_parent,
                _parent_path_authority=(
                    self._lifetime_authority.path_authority
                ),
            )
            qualified = close_qualified_candidate_for_formal(
                verified_candidate_digest,
                continuation,
            )
            self._qualified_candidate = qualified
            result = {
                "status": "PASS",
                "verifiedCandidateDigest": verified_candidate_digest,
                "candidateAggregateReceiptDigest": (
                    qualified.candidate_aggregate_receipt_digest
                ),
                "candidateProfileReceiptDigests": (
                    qualified.candidate_profile_receipt_digests
                ),
                "candidateSourceVmAuthorityIdentity": (
                    qualified.candidate_source_vm_authority_identity
                ),
                "credentialPersistence": self.credential_persistence,
            }
        except BaseException as primary_error:
            try:
                self.fail_closed_cleanup()
            except BaseException as cleanup_error:
                raise FormalCleanupFailure(
                    (primary_error, cleanup_error)
                ) from primary_error
            raise
        try:
            self._clear_r2_once()
        except BaseException as r2_error:
            try:
                self.fail_closed_cleanup()
            except BaseException as cleanup_error:
                raise FormalCleanupFailure(
                    (r2_error, cleanup_error)
                ) from r2_error
            raise
        return result

    def run_formal(
        self,
        *,
        publication_identity: str,
        attestation_claim_identities: Mapping[str, str],
        provenance_inputs: tuple[FormalProvenanceInput, ...],
        publication_input: FormalProvenanceInput,
        execution: FormalExecutionContext,
        publication_root: Path,
        private_work_root: Path,
        output_root: Path,
        provider: ClosedVmwareProvider | None = None,
    ) -> dict[str, Any]:
        qualified = self._qualified_candidate
        if (
            self._formal_terminal
            or qualified is None
            or not self._r2_cleared
            or self._sudo_cleared
            or self._formal_attempts >= 5
        ):
            raise FormalProducerError("FORMAL_PARENT_CAPABILITY_UNAVAILABLE")
        publication_root = self._lifetime_authority.require_contained(
            publication_root,
            name="PUBLICATION_ROOT",
        )
        private_work_root = self._lifetime_authority.require_contained(
            private_work_root,
            name="PRIVATE_WORK_ROOT",
        )
        attempt_output_root = self._lifetime_authority.require_contained(
            output_root,
            name="OUTPUT_ROOT",
        )
        if attempt_output_root in self._formal_output_roots:
            raise FormalProducerError("FORMAL_PARENT_OUTPUT_ROOT_REUSED")
        self._formal_output_roots.add(attempt_output_root)
        self._formal_attempts += 1
        try:
            result = execute_qualified_formal_production(
                qualified_candidate=qualified,
                publication_identity=publication_identity,
                attestation_claim_identities=attestation_claim_identities,
                provenance_inputs=provenance_inputs,
                publication_input=publication_input,
                execution=execution,
                publication_root=publication_root,
                private_work_root=private_work_root,
                output_root=attempt_output_root,
                provider=provider,
                _parent_path_authority=(
                    self._lifetime_authority.path_authority
                ),
            )
        except BaseException as error:
            if not isinstance(error, Exception) or self._formal_attempts >= 5:
                self._formal_terminal = True
                try:
                    self._close_qualified_candidate()
                finally:
                    self._clear_sudo_once()
            raise
        if (
            not self._formal_result_retryable(result)
            or self._formal_attempts >= 5
        ):
            self._formal_terminal = True
            try:
                self._close_qualified_candidate()
            finally:
                self._clear_sudo_once()
        return result

    def fail_closed_cleanup(self) -> None:
        self._formal_terminal = True
        errors: list[BaseException] = []
        for action in (
            self._close_qualified_candidate,
            self._clear_r2_once,
            self._clear_sudo_once,
            self._release_lifetime_authority,
        ):
            try:
                action()
            except BaseException as error:
                errors.append(error)
        if len(errors) == 1:
            raise errors[0]
        if errors:
            raise FormalCleanupFailure(tuple(errors))

    def retry_terminal_cleanup_before_seal(self) -> None:
        """Retry terminal in-memory/credential cleanup without releasing holds."""

        if not self._formal_terminal or self._lifetime_closed:
            raise FormalProducerError("FORMAL_PARENT_TERMINAL_CLEANUP_INVALID")
        errors: list[BaseException] = []
        for action in (
            self._close_qualified_candidate,
            self._clear_r2_once,
            self._clear_sudo_once,
        ):
            try:
                action()
            except BaseException as error:
                errors.append(error)
        if len(errors) == 1:
            raise errors[0]
        if errors:
            raise FormalCleanupFailure(tuple(errors))

    def seal_and_close(self, seal_callback: Any) -> object:
        """Run Wave F's sealer before releasing the held Evidence path chain."""

        if (
            not callable(seal_callback)
            or not self._formal_terminal
            or not self._r2_cleared
            or not self._sudo_cleared
            or self._qualified_candidate is not None
            or self._lifetime_closed
        ):
            raise FormalProducerError("FORMAL_PARENT_SEAL_LIFETIME_INVALID")
        # A failed external seal must leave the Evidence path-chain authority
        # held so Wave F can retry the same terminal output without a rebind
        # window.  Only a successfully returned, externally verified seal
        # result authorizes releasing the lifetime hold.
        if seal_callback() is not None:
            raise FormalProducerError("FORMAL_EVIDENCE_SEAL_CALLBACK_INVALID")
        result = self._lifetime_authority.verify_and_issue_seal_success()
        self._release_lifetime_authority()
        return result

    def __enter__(self) -> ReleaseContinuationParentWorker:
        if self._lifetime_closed or self._lifetime_entered:
            raise FormalProducerError("FORMAL_PARENT_LIFETIME_INVALID")
        authority = self._lifetime_authority
        authority.__enter__()
        self._lifetime_entered = True
        return self

    def __exit__(self, *_: object) -> None:
        self.fail_closed_cleanup()


def create_release_continuation_parent_worker(
    *,
    continuation_root: Path,
    evidence_root: Path,
    seal_root: Path,
    clear_r2_credentials: Any,
    clear_sudo_credentials: Any,
) -> ReleaseContinuationParentWorker:
    """Production factory; no worker can omit the held Evidence authority."""

    return ReleaseContinuationParentWorker(
        clear_r2_credentials=clear_r2_credentials,
        clear_sudo_credentials=clear_sudo_credentials,
        continuation_lifetime_authority=(
            acquire_continuation_evidence_lifetime_authority(
                continuation_root=continuation_root,
                evidence_root=evidence_root,
                seal_root=seal_root,
            )
        ),
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="AniMemo Formal VM producer")
    parser.add_argument("--execute", action="store_true")
    return parser


def execute_qualified_formal_production(
    *,
    qualified_candidate: QualifiedCandidateFormalAuthority,
    publication_identity: str,
    attestation_claim_identities: Mapping[str, str],
    provenance_inputs: tuple[FormalProvenanceInput, ...],
    publication_input: FormalProvenanceInput,
    execution: FormalExecutionContext,
    publication_root: Path,
    private_work_root: Path,
    output_root: Path,
    provider: ClosedVmwareProvider | None = None,
    _parent_path_authority: HeldWindowsPrivatePathAuthority | None = None,
) -> dict[str, Any]:
    """One in-memory Candidate→Formal continuation entry for the parent worker.

    The standalone CLI deliberately cannot reconstruct ``qualified_candidate``;
    it is the closed object returned by the Candidate controller continuation.
    """

    request = qualified_candidate.issue_request(
        publication_identity=publication_identity,
        attestation_claim_identities=attestation_claim_identities,
    )
    verifier = ProductionFormalAuthorityVerifier(
        FormalProvenancePlan(
            verifier=None,
            inputs=provenance_inputs,
            publication=publication_input,
            private_work_root=private_work_root,
            qualified_candidate=qualified_candidate,
        ),
        _parent_path_authority=_parent_path_authority,
    )
    authority = verifier.verify(request)
    transaction = _FormalOutputTransaction(
        output_root,
        request=request,
        execution=execution,
        candidate_aggregate_receipt_digest=(
            qualified_candidate.candidate_aggregate_receipt_digest
        ),
        candidate_profile_receipt_digests=(
            qualified_candidate.candidate_profile_receipt_digests
        ),
        candidate_source_vm_authority_identity=(
            qualified_candidate.candidate_source_vm_authority_identity
        ),
        candidate_material_authority_identity=(
            qualified_candidate.candidate_material_authority_identity
        ),
        candidate_material_tree_inventory_identity=(
            qualified_candidate.candidate_material_tree_inventory_identity
        ),
        parent_path_authority=_parent_path_authority,
    )
    try:
        if transaction.reused:
            return {
                "idempotent": True,
                "retryable": transaction.existing_retryable,
                "status": transaction.existing_status,
            }
        active_provider = provider or ClosedVmwareProvider()
        with active_provider.execution_authority():
            executor = ClosedFormalVmProfileExecutor(
                authority_root=publication_root,
                provider=active_provider,
                installer_materials=qualified_candidate.installer_materials,
                private_work_root=private_work_root,
                parent_path_authority=_parent_path_authority,
            )
            try:
                result = FormalVmController(
                    authority_verifier=_PreverifiedFormalAuthority(
                        request, authority
                    ),
                    profile_executor=executor,
                ).execute(request, execution)
                transaction.commit(result)
                return result
            finally:
                executor.cleanup()
    finally:
        transaction.cleanup()


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.execute:
            raise FormalProducerError("FORMAL_PARENT_WORKER_CAPABILITY_REQUIRED")
        print(
            json.dumps(
                {
                    "mode": "PLAN_ONLY",
                    "profiles": list(FORMAL_PROFILES),
                    "parentWorkerCapabilityRequired": True,
                    "provenanceBeforeClone": True,
                    "releaseAuthorityGranted": False,
                    "publishAuthorized": False,
                },
                sort_keys=True,
            )
        )
        return 0
    except FormalProducerError as error:
        print(json.dumps({"code": error.code}, sort_keys=True), file=sys.stderr)
        return 2
    except ProvenancePreflightError:
        print(
            json.dumps(
                {"code": "FORMAL_PROVENANCE_VERIFICATION_FAILED"},
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    except CandidateHarnessError:
        print(
            json.dumps({"code": "FORMAL_VM_PROVIDER_FAILED"}, sort_keys=True),
            file=sys.stderr,
        )
        return 2
    except Exception:  # noqa: BLE001 - top-level secret-free failure boundary
        print(
            json.dumps({"code": "FORMAL_EXTERNAL_FAILURE"}, sort_keys=True),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
