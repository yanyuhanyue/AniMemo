"""Plan-by-default three-profile Candidate VM acceptance harness.

This module owns the closed orchestration contract.  VM operations are behind
one typed provider seam so contract tests cannot accidentally start a VM and a
real provider cannot substitute paths, snapshots, profiles, or shell text.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import locale
import os
import re
import shutil
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from release.candidate import (
    CandidateContractError,
    aggregate_receipt_digest,
    canonical_json_bytes,
    load_verified_candidate,
    sha256_bytes,
    validate_aggregate_receipt,
    validate_profile_receipt,
)
from release.materials import reject_duplicate_json_keys
from release.r2_prestate import (
    R2_AUTH_METHOD_ARGUMENT,
    R2_RC14_EXPECTED_KEYS,
    r2_origin_receipt_digest,
    validate_r2_origin_receipt,
    verify_rc14_r2_origin_from_environment,
)

PROFILES = ("FRESH_BASE", "DOCKER_BASE", "RUNTIME_BASE_OFFLINE")
INSTALLER_PROFILES = {
    "FRESH_BASE": "ONLINE_FRESH",
    "DOCKER_BASE": "ONLINE_EXISTING_DOCKER",
    "RUNTIME_BASE_OFFLINE": "OFFLINE_VALIDATE_ONLY",
}
SNAPSHOT_ALLOWLIST = {
    "FRESH_BASE": "Ubuntu 24.04.4 - Fresh Base - Healthy",
    "DOCKER_BASE": "Ubuntu 24.04.4 - Docker Base",
    "RUNTIME_BASE_OFFLINE": "Ubuntu 24.04.4 - AniMemo Runtime Base",
}
SOURCE_VM_IDENTITY = "Ubuntu 64 位"
SOURCE_VM_ROOT = Path("E:/Ubuntu Server")
VMRUN = Path("E:/VMware/vmrun.exe")
ROBOCOPY = Path("C:/Windows/System32/robocopy.exe")
SSH = Path("C:/Windows/System32/OpenSSH/ssh.exe")
SCP = Path("C:/Windows/System32/OpenSSH/scp.exe")
SSH_ALIAS = "animemo-test"
VM_WORK_ROOT = Path("E:/番剧记录/.animemo-vm-work/rc14-candidate-acceptance")
SOURCE_VM_HASH_FILES = (
    "Ubuntu 64 位-000001.vmdk",
    "Ubuntu 64 位-000003.vmdk",
    "Ubuntu 64 位-000004.vmdk",
    "Ubuntu 64 位-Snapshot3.vmsn",
    "Ubuntu 64 位-Snapshot4.vmsn",
    "Ubuntu 64 位-Snapshot5.vmsn",
    "Ubuntu 64 位.vmdk",
    "Ubuntu 64 位.vmsd",
    "Ubuntu 64 位.vmx",
)
SNAPSHOT_FILES = {
    "FRESH_BASE": "Ubuntu 64 位-Snapshot3.vmsn",
    "DOCKER_BASE": "Ubuntu 64 位-Snapshot4.vmsn",
    "RUNTIME_BASE_OFFLINE": "Ubuntu 64 位-Snapshot5.vmsn",
}
PUBLIC_ORIGIN = "https://candidate.rc14.invalid"
RC14_VERSION = "v1.1.0-rc.14"
REPOSITORY = "yanyuhanyue/AniMemo"
PUBLIC_MIRROR_ORIGIN = "https://download.animemo.cc"
GUEST_CANDIDATE_ROOT = "/var/lib/animemo/prepublication-candidates/v1"
GUEST_PROFILE_RUNNER = "/usr/local/lib/animemo-candidate/candidate_profile_runner.py"
GUEST_RECEIPT = "/var/lib/animemo/candidate-acceptance/profile-receipt.json"
GUEST_SUDO_PASSWORD_ENV = "ANIMEMO_CANDIDATE_GUEST_SUDO_PASSWORD"  # noqa: S105
MAX_PUBLIC_RESPONSE_BYTES = 1024 * 1024
MAX_VM_CONFIGURATION_BYTES = 4 * 1024 * 1024
MAX_VM_FILES = 8192
MAX_VM_TOTAL_BYTES = 1024 * 1024 * 1024 * 1024
ALLOWED_VMDK_CREATE_TYPES = frozenset(
    {"twogbmaxextentflat", "twogbmaxextentsparse"}
)
ALLOWED_VMDK_EXTENT_TYPES = frozenset({"FLAT", "SPARSE"})
SAFE_HOST_ENVIRONMENT_NAMES = frozenset(
    {
        "APPDATA",
        "COMSPEC",
        "HOME",
        "HOMEDRIVE",
        "HOMEPATH",
        "LANG",
        "LC_ALL",
        "LOCALAPPDATA",
        "SYSTEMDRIVE",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "USERPROFILE",
        "WINDIR",
    }
)
EXPECTED_RC14_EXTERNAL_STATE = {
    "tag": "ABSENT",
    "github_release": "ABSENT",
    "ghcr": "ABSENT",
    "public_r2": "ABSENT_BY_PUBLIC_READBACK_NON_AUTHORITATIVE",
}
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_SHA = re.compile(r"[0-9a-f]{40}\Z")


class CandidateHarnessError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class HostCommandRunner(Protocol):
    def run(
        self,
        argv: Sequence[str],
        *,
        environment: Mapping[str, str],
        input_bytes: bytes | None = None,
        timeout: int = 300,
    ) -> subprocess.CompletedProcess[bytes]: ...


class SubprocessHostCommandRunner:
    def run(
        self,
        argv: Sequence[str],
        *,
        environment: Mapping[str, str],
        input_bytes: bytes | None = None,
        timeout: int = 300,
    ) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(  # noqa: S603 - callers construct closed argv
            tuple(argv),
            input=input_bytes,
            stdin=subprocess.DEVNULL if input_bytes is None else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=dict(environment),
            shell=False,
            timeout=timeout,
            check=False,
        )


class PublicReadonlyTransport(Protocol):
    def get(self, url: str, headers: Mapping[str, str]) -> tuple[int, bytes]: ...


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


class FixedPublicReadonlyAdapter:
    _HOSTS = frozenset({"api.github.com", "download.animemo.cc", "ghcr.io"})

    def get(self, url: str, headers: Mapping[str, str]) -> tuple[int, bytes]:
        try:
            parsed = urllib.parse.urlsplit(url)
            port = parsed.port
        except ValueError:
            raise CandidateHarnessError("CANDIDATE_EXTERNAL_STATE_UNVERIFIED") from None
        if (
            parsed.scheme != "https"
            or parsed.hostname not in self._HOSTS
            or port is not None
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
            or set(headers) - {"Accept", "Authorization", "Range", "User-Agent"}
        ):
            raise CandidateHarnessError("CANDIDATE_EXTERNAL_STATE_UNVERIFIED")
        request = urllib.request.Request(  # noqa: S310 - fixed HTTPS hosts above
            url,
            headers=dict(headers),
            method="GET",
        )
        try:
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({}),
                _NoRedirectHandler(),
            )
            with opener.open(request, timeout=30) as response:  # noqa: S310 - fixed hosts
                body = response.read(MAX_PUBLIC_RESPONSE_BYTES + 1)
                if len(body) > MAX_PUBLIC_RESPONSE_BYTES:
                    raise CandidateHarnessError(
                        "CANDIDATE_EXTERNAL_STATE_UNVERIFIED"
                    )
                return response.status, body
        except urllib.error.HTTPError as error:
            body = error.read(MAX_PUBLIC_RESPONSE_BYTES + 1)
            if len(body) > MAX_PUBLIC_RESPONSE_BYTES:
                raise CandidateHarnessError("CANDIDATE_EXTERNAL_STATE_UNVERIFIED")
            return error.code, body
        except (OSError, urllib.error.URLError) as error:
            raise CandidateHarnessError(
                "CANDIDATE_EXTERNAL_STATE_UNVERIFIED"
            ) from error


def _file_identity(metadata: os.stat_result) -> tuple[int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
    )


def _hash_original_vm_file(path: Path) -> str:
    try:
        before = path.lstat()
    except OSError as error:
        raise CandidateHarnessError(
            "CANDIDATE_VM_SOURCE_IDENTITY_UNAVAILABLE"
        ) from error
    if (
        path.is_symlink()
        or bool(getattr(path, "is_junction", lambda: False)())
        or not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
        or before.st_size < 1
    ):
        raise CandidateHarnessError("CANDIDATE_VM_SOURCE_IDENTITY_UNAVAILABLE")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise CandidateHarnessError(
            "CANDIDATE_VM_SOURCE_IDENTITY_UNAVAILABLE"
        ) from error
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or _file_identity(opened) != _file_identity(before)
        ):
            raise CandidateHarnessError(
                "CANDIDATE_VM_SOURCE_IDENTITY_UNAVAILABLE"
            )
        digest = hashlib.sha256()
        while chunk := os.read(descriptor, 1024 * 1024):
            digest.update(chunk)
        if _file_identity(os.fstat(descriptor)) != _file_identity(opened):
            raise CandidateHarnessError("CANDIDATE_VM_SOURCE_CHANGED_DURING_HASH")
        return "sha256:" + digest.hexdigest()
    finally:
        os.close(descriptor)


@dataclass(frozen=True)
class SourceVmEvidence:
    vm_identity: str
    snapshot_identities: Mapping[str, str]
    original_hashes: Mapping[str, str]


@dataclass(frozen=True)
class CandidateProfilePlan:
    profile: str
    installer_profile: str
    snapshot_name: str
    snapshot_identity: str
    clone_identity: str

    def as_dict(self) -> dict[str, str]:
        return {
            "profile": self.profile,
            "installerProfile": self.installer_profile,
            "snapshotName": self.snapshot_name,
            "snapshotIdentity": self.snapshot_identity,
            "cloneIdentity": self.clone_identity,
        }


@dataclass(frozen=True)
class CandidateHarnessPlan:
    verified_candidate_digest: str
    candidate_input_digest: str
    qualification_run_id: int
    source_sha: str
    source_tree: str
    candidate_version: str
    source_vm_identity: str
    source_vm_digest: str
    original_vm_hashes: Mapping[str, str]
    profiles: tuple[CandidateProfilePlan, ...]
    plan_digest: str

    def identity_body(self) -> dict[str, object]:
        return {
            "schema": "animemo.prepublication-candidate-vm-plan/v1",
            "version": 1,
            "mode": "PLAN_ONLY",
            "verifiedCandidateDigest": self.verified_candidate_digest,
            "candidateInputDigest": self.candidate_input_digest,
            "qualificationRunId": self.qualification_run_id,
            "sourceSha": self.source_sha,
            "sourceTree": self.source_tree,
            "candidateVersion": self.candidate_version,
            "sourceVmIdentity": self.source_vm_identity,
            "sourceVmDigest": self.source_vm_digest,
            "originalVmHashes": dict(sorted(self.original_vm_hashes.items())),
            "profiles": [profile.as_dict() for profile in self.profiles],
            "r2OriginProofRequiredBeforeClone": True,
            "releaseAuthorityGranted": False,
            "publishAuthorized": False,
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_body(), "planDigest": self.plan_digest}


class CandidateVmProvider(Protocol):
    def inspect_source(self) -> SourceVmEvidence: ...

    def execute_profile(
        self,
        *,
        plan: CandidateProfilePlan,
        harness_plan: CandidateHarnessPlan,
        candidate_root: Path,
        initial_platform_state: Mapping[str, bool],
    ) -> Mapping[str, Any]: ...

    def inspect_original_hashes(self) -> Mapping[str, str]: ...

    def inspect_rc14_external_state(self) -> Mapping[str, str]: ...


class ClosedVmwareProvider:
    """Closed production provider for disposable VMware acceptance clones.

    Every authority is fixed in this module.  The provider never accepts a VM
    path, snapshot, SSH destination, guest command, or publication endpoint
    from CLI input.  The sudo password is read only at execution time and is
    sent over stdin; it is never placed in argv, receipts, or exceptions.
    """

    def __init__(
        self,
        *,
        runner: HostCommandRunner | None = None,
        public_transport: PublicReadonlyTransport | None = None,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        self._runner = runner or SubprocessHostCommandRunner()
        self._public = public_transport or FixedPublicReadonlyAdapter()
        self._environment = environment if environment is not None else os.environ
        self._host_environment = self._sanitized_host_environment(self._environment)

    @staticmethod
    def _sanitized_host_environment(
        source: Mapping[str, str],
    ) -> dict[str, str]:
        result: dict[str, str] = {}
        for name, value in source.items():
            if (
                type(name) is not str
                or type(value) is not str
                or not name
                or "=" in name
                or "\0" in name
                or "\0" in value
            ):
                raise CandidateHarnessError("CANDIDATE_VM_HOST_ENVIRONMENT_INVALID")
            canonical_name = name.upper()
            if canonical_name not in SAFE_HOST_ENVIRONMENT_NAMES:
                continue
            result[canonical_name] = value
        fixed_path = os.pathsep.join(
            dict.fromkeys(
                (
                    str(VMRUN.parent),
                    str(SSH.parent),
                    str(ROBOCOPY.parent),
                    str(Path(result.get("SYSTEMROOT", "C:/Windows")) / "System32"),
                    result.get("SYSTEMROOT", "C:/Windows"),
                )
            )
        )
        result["PATH"] = fixed_path
        return result

    @property
    def _source_vmx(self) -> Path:
        return SOURCE_VM_ROOT / f"{SOURCE_VM_IDENTITY}.vmx"

    @staticmethod
    def _hashes() -> dict[str, str]:
        return {
            name: _hash_original_vm_file(SOURCE_VM_ROOT / name)
            for name in SOURCE_VM_HASH_FILES
        }

    @staticmethod
    def _closed_path(path: Path, *, root: Path, code: str) -> Path:
        try:
            absolute = path.resolve(strict=False)
            boundary = root.resolve(strict=False)
            absolute.relative_to(boundary)
        except (OSError, ValueError) as error:
            raise CandidateHarnessError(code) from error
        if absolute == boundary:
            raise CandidateHarnessError(code)
        return absolute

    def _run(
        self,
        argv: Sequence[str],
        *,
        code: str,
        input_bytes: bytes | None = None,
        timeout: int = 300,
        allowed: frozenset[int] = frozenset({0}),
    ) -> subprocess.CompletedProcess[bytes]:
        try:
            completed = self._runner.run(
                tuple(argv),
                environment=self._host_environment,
                input_bytes=input_bytes,
                timeout=timeout,
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise CandidateHarnessError(code) from error
        if completed.returncode not in allowed:
            raise CandidateHarnessError(code)
        return completed

    @staticmethod
    def _decode_output(value: bytes, *, code: str) -> list[str]:
        if len(value) > MAX_PUBLIC_RESPONSE_BYTES:
            raise CandidateHarnessError(code)
        encodings = (locale.getpreferredencoding(False), "utf-8")
        for encoding in dict.fromkeys(encodings):
            try:
                return value.decode(encoding, errors="strict").splitlines()
            except (LookupError, UnicodeDecodeError):
                continue
        raise CandidateHarnessError(code)

    def _assert_tools(self) -> None:
        for path in (VMRUN, ROBOCOPY, SSH, SCP, self._source_vmx):
            if not path.is_file():
                raise CandidateHarnessError("CANDIDATE_VM_TOOLCHAIN_UNAVAILABLE")
        runner = Path(__file__).resolve().with_name("candidate_profile_runner.py")
        if not runner.is_file():
            raise CandidateHarnessError("CANDIDATE_VM_TOOLCHAIN_UNAVAILABLE")

    def _running_vmx_paths(self) -> frozenset[str]:
        completed = self._run(
            (str(VMRUN), "-T", "ws", "list"),
            code="CANDIDATE_VM_INVENTORY_UNAVAILABLE",
        )
        lines = self._decode_output(
            completed.stdout, code="CANDIDATE_VM_INVENTORY_UNAVAILABLE"
        )
        if not lines or re.fullmatch(r"Total running VMs: [0-9]+", lines[0]) is None:
            raise CandidateHarnessError("CANDIDATE_VM_INVENTORY_INVALID")
        try:
            count = int(lines[0].rsplit(" ", 1)[1])
            paths = {
                os.path.normcase(str(Path(line.strip('"')).resolve(strict=False)))
                for line in lines[1:]
                if line.strip()
            }
        except (OSError, ValueError) as error:
            raise CandidateHarnessError("CANDIDATE_VM_INVENTORY_INVALID") from error
        if len(paths) != count:
            raise CandidateHarnessError("CANDIDATE_VM_INVENTORY_INVALID")
        return frozenset(paths)

    def _is_running(self, vmx: Path) -> bool:
        identity = os.path.normcase(str(vmx.resolve(strict=False)))
        return identity in self._running_vmx_paths()

    def _assert_source_stopped(self) -> None:
        if self._is_running(self._source_vmx):
            raise CandidateHarnessError("CANDIDATE_ORIGINAL_VM_RUNNING")

    def _snapshot_names(self) -> frozenset[str]:
        completed = self._run(
            (
                str(VMRUN),
                "-T",
                "ws",
                "listSnapshots",
                str(self._source_vmx),
            ),
            code="CANDIDATE_VM_SNAPSHOT_INVENTORY_UNAVAILABLE",
        )
        lines = self._decode_output(
            completed.stdout, code="CANDIDATE_VM_SNAPSHOT_INVENTORY_UNAVAILABLE"
        )
        if not lines or re.fullmatch(r"Total snapshots: [0-9]+", lines[0]) is None:
            raise CandidateHarnessError("CANDIDATE_VM_SNAPSHOT_INVENTORY_INVALID")
        names = frozenset(line for line in lines[1:] if line)
        if len(names) != int(lines[0].rsplit(" ", 1)[1]):
            raise CandidateHarnessError("CANDIDATE_VM_SNAPSHOT_INVENTORY_INVALID")
        return names

    @staticmethod
    def _vm_inventory(root: Path) -> dict[str, tuple[int, int, int]]:
        try:
            root_metadata = root.lstat()
        except OSError as error:
            raise CandidateHarnessError("CANDIDATE_VM_COPY_INVENTORY_INVALID") from error
        if (
            root.is_symlink()
            or bool(getattr(root, "is_junction", lambda: False)())
            or not stat.S_ISDIR(root_metadata.st_mode)
        ):
            raise CandidateHarnessError("CANDIDATE_VM_COPY_INVENTORY_INVALID")
        inventory: dict[str, tuple[int, int, int]] = {}
        casefolded_names: set[str] = set()
        file_count = 0
        total_bytes = 0
        try:
            entries = sorted(root.rglob("*"), key=lambda item: item.as_posix())
            for entry in entries:
                metadata = entry.lstat()
                if entry.is_symlink() or bool(
                    getattr(entry, "is_junction", lambda: False)()
                ):
                    raise CandidateHarnessError(
                        "CANDIDATE_VM_COPY_INVENTORY_INVALID"
                    )
                if stat.S_ISDIR(metadata.st_mode):
                    continue
                if not stat.S_ISREG(metadata.st_mode):
                    raise CandidateHarnessError(
                        "CANDIDATE_VM_COPY_INVENTORY_INVALID"
                    )
                file_count += 1
                total_bytes += metadata.st_size
                if file_count > MAX_VM_FILES or total_bytes > MAX_VM_TOTAL_BYTES:
                    raise CandidateHarnessError("CANDIDATE_VM_COPY_LIMIT_EXCEEDED")
                relative = entry.relative_to(root).as_posix()
                folded = relative.casefold()
                if folded in casefolded_names:
                    raise CandidateHarnessError(
                        "CANDIDATE_VM_COPY_INVENTORY_INVALID"
                    )
                casefolded_names.add(folded)
                inventory[relative] = (
                    metadata.st_size,
                    metadata.st_dev,
                    metadata.st_ino,
                )
        except OSError as error:
            raise CandidateHarnessError("CANDIDATE_VM_COPY_INVENTORY_INVALID") from error
        if not inventory:
            raise CandidateHarnessError("CANDIDATE_VM_COPY_INVENTORY_INVALID")
        return inventory

    @staticmethod
    def _configuration_prefix(path: Path, *, complete: bool) -> bytes:
        try:
            before = path.lstat()
        except OSError as error:
            raise CandidateHarnessError("CANDIDATE_VM_DISK_GRAPH_INVALID") from error
        if (
            path.is_symlink()
            or bool(getattr(path, "is_junction", lambda: False)())
            or not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_size < 1
            or (complete and before.st_size > MAX_VM_CONFIGURATION_BYTES)
        ):
            raise CandidateHarnessError("CANDIDATE_VM_DISK_GRAPH_INVALID")
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags)
        except OSError as error:
            raise CandidateHarnessError("CANDIDATE_VM_DISK_GRAPH_INVALID") from error
        try:
            opened = os.fstat(descriptor)
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_nlink != 1
                or _file_identity(opened) != _file_identity(before)
            ):
                raise CandidateHarnessError("CANDIDATE_VM_DISK_GRAPH_INVALID")
            data = os.read(descriptor, MAX_VM_CONFIGURATION_BYTES + 1)
            if complete and len(data) != opened.st_size:
                raise CandidateHarnessError("CANDIDATE_VM_DISK_GRAPH_INVALID")
            if _file_identity(os.fstat(descriptor)) != _file_identity(opened):
                raise CandidateHarnessError("CANDIDATE_VM_DISK_GRAPH_INVALID")
            return data[:MAX_VM_CONFIGURATION_BYTES]
        finally:
            os.close(descriptor)

    @staticmethod
    def _configuration_text(data: bytes) -> str:
        for encoding in dict.fromkeys(
            (locale.getpreferredencoding(False), "utf-8", "latin-1")
        ):
            try:
                return data.decode(encoding, errors="strict")
            except (LookupError, UnicodeDecodeError):
                continue
        raise CandidateHarnessError("CANDIDATE_VM_DISK_GRAPH_INVALID")

    @classmethod
    def _closed_disk_reference(
        cls,
        clone_root: Path,
        value: str,
        *,
        base: Path | None = None,
    ) -> Path:
        normalized = value.replace("\\", "/")
        parts = normalized.split("/")
        if (
            not value
            or len(value) > 1024
            or any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
            or normalized.startswith("/")
            or normalized.startswith("//")
            or re.match(r"^[A-Za-z]:", normalized) is not None
            or any(part in {"", ".", ".."} for part in parts)
        ):
            raise CandidateHarnessError("CANDIDATE_VM_SHARED_DISK_REJECTED")
        target = (base or clone_root).joinpath(*parts)
        try:
            absolute = target.resolve(strict=True)
            absolute.relative_to(clone_root.resolve(strict=True))
            metadata = absolute.lstat()
        except (OSError, ValueError) as error:
            raise CandidateHarnessError("CANDIDATE_VM_SHARED_DISK_REJECTED") from error
        if (
            absolute.is_symlink()
            or bool(getattr(absolute, "is_junction", lambda: False)())
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
        ):
            raise CandidateHarnessError("CANDIDATE_VM_SHARED_DISK_REJECTED")
        return absolute

    @staticmethod
    def _setting_value(line: str, expected_key: str) -> str | None:
        if "=" not in line:
            return None
        key, raw_value = line.split("=", 1)
        if key.strip().casefold() != expected_key.casefold():
            return None
        value = raw_value.strip()
        if len(value) < 2 or value[0] != '"' or value[-1] != '"':
            raise CandidateHarnessError("CANDIDATE_VM_DISK_GRAPH_INVALID")
        result = value[1:-1]
        if '"' in result or "\r" in result or "\n" in result:
            raise CandidateHarnessError("CANDIDATE_VM_DISK_GRAPH_INVALID")
        return result

    @staticmethod
    def _disk_slot_key(key: str) -> bool:
        lowered = key.casefold()
        suffix = ".filename"
        if not lowered.endswith(suffix):
            return False
        slot = lowered[: -len(suffix)]
        if slot.count(":") != 1:
            return False
        bus, unit = slot.split(":", 1)
        if not unit.isdecimal():
            return False
        for prefix in ("scsi", "sata", "ide", "nvme"):
            if bus.startswith(prefix) and bus[len(prefix) :].isdecimal():
                return True
        return False

    @classmethod
    def _validate_clone_disk_graph(cls, clone_root: Path, clone_vmx: Path) -> None:
        vmx_text = cls._configuration_text(
            cls._configuration_prefix(clone_vmx, complete=True)
        )
        disk_references: list[str] = []
        seen_settings: set[str] = set()
        for raw_line in vmx_text.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            raw_key, raw_value = line.split("=", 1)
            key = raw_key.strip()
            setting = key.casefold()
            if not key or setting in seen_settings:
                raise CandidateHarnessError("CANDIDATE_VM_DISK_GRAPH_INVALID")
            seen_settings.add(setting)
            value = raw_value.strip().strip('"').casefold()
            if (
                (setting == "disk.locking" and value == "false")
                or (setting.endswith(".sharing") and value == "multi-writer")
                or (
                    setting.endswith(".sharedbus")
                    and value in {"virtual", "physical"}
                )
                or (
                    setting.endswith(".devicetype")
                    and value in {"rawdisk", "physicaldrive"}
                )
            ):
                raise CandidateHarnessError("CANDIDATE_VM_SHARED_DISK_REJECTED")
            if cls._disk_slot_key(key):
                reference = cls._setting_value(line, key)
                if reference is not None and reference.casefold().endswith(".vmdk"):
                    disk_references.append(reference)
        if not disk_references:
            raise CandidateHarnessError("CANDIDATE_VM_DISK_GRAPH_INVALID")
        descriptor_queue = [
            cls._closed_disk_reference(clone_root, reference)
            for reference in disk_references
        ]
        seen_descriptors: set[Path] = set()
        while descriptor_queue:
            vmdk = descriptor_queue.pop()
            if vmdk in seen_descriptors:
                continue
            seen_descriptors.add(vmdk)
            text = cls._configuration_text(
                cls._configuration_prefix(vmdk, complete=False)
            )
            if not text.lstrip("\ufeff \t\r\n").startswith("# Disk DescriptorFile"):
                raise CandidateHarnessError("CANDIDATE_VM_DISK_GRAPH_INVALID")
            parent_references: list[str] = []
            extent_references: list[str] = []
            parent_ids: list[str] = []
            create_types: list[str] = []
            for raw_line in text.splitlines():
                line = raw_line.strip()
                lowered = line.casefold()
                if lowered.startswith("createtype"):
                    create_type = cls._setting_value(line, "createType")
                    if (
                        create_type is None
                        or create_type.casefold() not in ALLOWED_VMDK_CREATE_TYPES
                    ):
                        raise CandidateHarnessError(
                            "CANDIDATE_VM_SHARED_DISK_REJECTED"
                        )
                    create_types.append(create_type.casefold())
                    continue
                if lowered.startswith("parentfilenamehint"):
                    parent = cls._setting_value(line, "parentFileNameHint")
                    if parent is None:
                        raise CandidateHarnessError(
                            "CANDIDATE_VM_DISK_GRAPH_INVALID"
                        )
                    parent_references.append(parent)
                    continue
                if lowered.startswith("parentcid"):
                    if "=" not in line:
                        raise CandidateHarnessError(
                            "CANDIDATE_VM_DISK_GRAPH_INVALID"
                        )
                    key, value = line.split("=", 1)
                    candidate = value.strip().casefold()
                    if (
                        key.strip().casefold() != "parentcid"
                        or len(candidate) != 8
                        or any(character not in "0123456789abcdef" for character in candidate)
                    ):
                        raise CandidateHarnessError(
                            "CANDIDATE_VM_DISK_GRAPH_INVALID"
                        )
                    parent_ids.append(candidate)
                    continue
                first_field = line.split(None, 1)[0].upper() if line else ""
                if first_field not in {"RW", "RDONLY", "NOACCESS"}:
                    continue
                pieces = line.split('"')
                prefix_fields = pieces[0].split()
                suffix_fields = pieces[2].split() if len(pieces) == 3 else []
                valid_type = (
                    len(prefix_fields) == 3
                    and prefix_fields[2].upper() in ALLOWED_VMDK_EXTENT_TYPES
                )
                if (
                    len(pieces) != 3
                    or not valid_type
                    or not prefix_fields[1].isdecimal()
                    or not pieces[1]
                    or len(suffix_fields) > 1
                    or (suffix_fields and not suffix_fields[0].isdecimal())
                ):
                    raise CandidateHarnessError(
                        "CANDIDATE_VM_SHARED_DISK_REJECTED"
                        if len(prefix_fields) == 3
                        and prefix_fields[2].upper()
                        not in ALLOWED_VMDK_EXTENT_TYPES
                        else "CANDIDATE_VM_DISK_GRAPH_INVALID"
                    )
                extent_references.append(pieces[1])
            if len(create_types) != 1:
                raise CandidateHarnessError("CANDIDATE_VM_DISK_GRAPH_INVALID")
            for reference in parent_references:
                descriptor_queue.append(
                    cls._closed_disk_reference(
                        clone_root,
                        reference,
                        base=vmdk.parent,
                    )
                )
            for reference in extent_references:
                cls._closed_disk_reference(
                    clone_root,
                    reference,
                    base=vmdk.parent,
                )
            if parent_ids and parent_ids[-1].lower() != "ffffffff" and len(parent_references) != 1:
                raise CandidateHarnessError("CANDIDATE_VM_DISK_GRAPH_INVALID")

    def _clone_full(self, plan: CandidateProfilePlan) -> tuple[Path, Path]:
        self._assert_source_stopped()
        suffix = plan.clone_identity.removeprefix("sha256:")
        clone_root = self._closed_path(
            VM_WORK_ROOT / f"{plan.profile.lower()}-{suffix}",
            root=VM_WORK_ROOT,
            code="CANDIDATE_VM_CLONE_PATH_INVALID",
        )
        if clone_root.exists() or clone_root.is_symlink():
            raise CandidateHarnessError("CANDIDATE_VM_CLONE_PATH_EXISTS")
        try:
            source_hashes = self._hashes()
            source_inventory = self._vm_inventory(SOURCE_VM_ROOT)
            clone_root.parent.mkdir(parents=True, exist_ok=True)
            completed = self._run(
                (
                    str(ROBOCOPY),
                    str(SOURCE_VM_ROOT),
                    str(clone_root),
                    "/E",
                    "/COPY:DAT",
                    "/DCOPY:DAT",
                    "/R:0",
                    "/W:0",
                    "/XJ",
                    "/NFL",
                    "/NDL",
                    "/NP",
                    "/NJH",
                    "/NJS",
                ),
                code="CANDIDATE_VM_FULL_COPY_FAILED",
                timeout=4 * 60 * 60,
                allowed=frozenset(range(8)),
            )
            del completed
            clone_inventory = self._vm_inventory(clone_root)
            if {
                name: metadata[0] for name, metadata in source_inventory.items()
            } != {name: metadata[0] for name, metadata in clone_inventory.items()}:
                raise CandidateHarnessError("CANDIDATE_VM_FULL_COPY_MISMATCH")
            for name, (_, source_device, source_inode) in source_inventory.items():
                _, clone_device, clone_inode = clone_inventory[name]
                if (source_device, source_inode) == (clone_device, clone_inode):
                    raise CandidateHarnessError("CANDIDATE_VM_COPY_NOT_INDEPENDENT")
            if self._hashes() != source_hashes:
                raise CandidateHarnessError("CANDIDATE_ORIGINAL_VM_MUTATED")
            clone_vmx = clone_root / f"{SOURCE_VM_IDENTITY}.vmx"
            if not clone_vmx.is_file():
                raise CandidateHarnessError("CANDIDATE_VM_FULL_COPY_MISMATCH")
            self._validate_clone_disk_graph(clone_root, clone_vmx)
            return clone_root, clone_vmx
        except BaseException:
            self._quarantine_clone(clone_root, plan.clone_identity)
            raise

    def _revert_clone(self, clone_vmx: Path, snapshot_name: str) -> None:
        self._run(
            (
                str(VMRUN),
                "-T",
                "ws",
                "revertToSnapshot",
                str(clone_vmx),
                snapshot_name,
            ),
            code="CANDIDATE_VM_CLONE_REVERT_FAILED",
            timeout=600,
        )

    def _start_clone(self, clone_vmx: Path) -> None:
        self._run(
            (str(VMRUN), "-T", "ws", "start", str(clone_vmx), "nogui"),
            code="CANDIDATE_VM_CLONE_START_FAILED",
            timeout=600,
        )

    @staticmethod
    def _ssh_argv(command: str) -> tuple[str, ...]:
        return (
            str(SSH),
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=10",
            "-o",
            "StrictHostKeyChecking=yes",
            "--",
            SSH_ALIAS,
            command,
        )

    def _wait_for_ssh(self) -> None:
        for _ in range(60):
            try:
                completed = self._runner.run(
                    self._ssh_argv("/usr/bin/true"),
                    environment=self._host_environment,
                    timeout=15,
                )
            except (OSError, subprocess.SubprocessError):
                completed = None
            if completed is not None and completed.returncode == 0:
                return
            time.sleep(2)
        raise CandidateHarnessError("CANDIDATE_VM_GUEST_UNREACHABLE")

    def _sudo_password(self) -> bytes:
        value = self._environment.get(GUEST_SUDO_PASSWORD_ENV, "")
        if (
            type(value) is not str
            or not value
            or len(value) > 1024
            or any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
        ):
            raise CandidateHarnessError("CANDIDATE_VM_GUEST_CREDENTIAL_UNAVAILABLE")
        try:
            return value.encode("utf-8") + b"\n"
        except UnicodeEncodeError as error:
            raise CandidateHarnessError(
                "CANDIDATE_VM_GUEST_CREDENTIAL_UNAVAILABLE"
            ) from error

    def _ssh_checked(
        self,
        command: str,
        *,
        code: str,
        sudo_password: bytes | None = None,
        timeout: int = 300,
    ) -> subprocess.CompletedProcess[bytes]:
        return self._run(
            self._ssh_argv(command),
            code=code,
            input_bytes=sudo_password,
            timeout=timeout,
        )

    def _stage_candidate(self, candidate_root: Path, candidate_digest: str) -> str:
        if not _DIGEST.fullmatch(candidate_digest):
            raise CandidateHarnessError("CANDIDATE_VM_STAGE_IDENTITY_INVALID")
        digest_hex = candidate_digest.removeprefix("sha256:")
        guest_root = f"{GUEST_CANDIDATE_ROOT}/{digest_hex}"
        password = self._sudo_password()
        self._ssh_checked(
            "/bin/rm -rf -- /tmp/animemo-candidate-stage "
            "/tmp/animemo-candidate-profile-runner.py",
            code="CANDIDATE_VM_STAGE_FAILED",
        )
        self._run(
            (
                str(SCP),
                "-q",
                "-r",
                "-o",
                "BatchMode=yes",
                "-o",
                "StrictHostKeyChecking=yes",
                "--",
                str(candidate_root),
                f"{SSH_ALIAS}:/tmp/animemo-candidate-stage",
            ),
            code="CANDIDATE_VM_STAGE_FAILED",
            timeout=60 * 60,
        )
        runner_path = Path(__file__).resolve().with_name("candidate_profile_runner.py")
        self._run(
            (
                str(SCP),
                "-q",
                "-o",
                "BatchMode=yes",
                "-o",
                "StrictHostKeyChecking=yes",
                "--",
                str(runner_path),
                f"{SSH_ALIAS}:/tmp/animemo-candidate-profile-runner.py",
            ),
            code="CANDIDATE_VM_STAGE_FAILED",
            timeout=300,
        )
        fixed_commands = (
            f"/usr/bin/test ! -e {guest_root}",
            f"/usr/bin/install -d -m 0700 {GUEST_CANDIDATE_ROOT}",
            f"/bin/mv -- /tmp/animemo-candidate-stage {guest_root}",
            f"/bin/chown -R root:root {guest_root}",
            f"/bin/chmod -R a-w,go-rwx {guest_root}",
            "/usr/bin/install -d -m 0700 /usr/local/lib/animemo-candidate",
            "/usr/bin/install -o root -g root -m 0700 "
            "/tmp/animemo-candidate-profile-runner.py " + GUEST_PROFILE_RUNNER,
            f"/usr/bin/test -r {guest_root}/verified-candidate.json",
            f"/usr/bin/test ! -e {GUEST_RECEIPT}",
        )
        for command in fixed_commands:
            self._ssh_checked(
                "sudo -S -p '' -- " + command,
                code="CANDIDATE_VM_STAGE_FAILED",
                sudo_password=password,
            )
        return guest_root

    def _run_profile_guest(
        self,
        *,
        plan: CandidateProfilePlan,
        harness_plan: CandidateHarnessPlan,
        guest_root: str,
        initial_platform_state: Mapping[str, bool],
    ) -> Mapping[str, Any]:
        context = {
            "base_vm_identity": harness_plan.source_vm_digest,
            "clone_identity": plan.clone_identity,
            "initial_platform_state": dict(initial_platform_state),
            "original_vm_pre_hashes": dict(harness_plan.original_vm_hashes),
            "profile": plan.profile,
            "snapshot_identity": plan.snapshot_identity,
        }
        context_b64url = base64.urlsafe_b64encode(
            canonical_json_bytes(context)
        ).decode("ascii").rstrip("=")
        if re.fullmatch(r"[A-Za-z0-9_-]+", context_b64url) is None:
            raise CandidateHarnessError("CANDIDATE_VM_PROFILE_CONTEXT_INVALID")
        command = (
            "sudo -S -p '' -- /usr/bin/env "
            f"ANIMEMO_CANDIDATE_PROFILE_CONTEXT_B64URL={context_b64url} "
            f"PYTHONPATH={guest_root}/installer-root "
            f"/usr/bin/python3 {GUEST_PROFILE_RUNNER} "
            f"--verified-candidate-digest {harness_plan.verified_candidate_digest} "
            f"--profile {plan.profile} --public-origin {PUBLIC_ORIGIN} --execute"
        )
        password = self._sudo_password()
        self._ssh_checked(
            command,
            code="CANDIDATE_VM_PROFILE_EXECUTION_FAILED",
            sudo_password=password,
            timeout=4 * 60 * 60,
        )
        completed = self._ssh_checked(
            "sudo -S -p '' -- /bin/cat " + GUEST_RECEIPT,
            code="CANDIDATE_VM_PROFILE_RECEIPT_UNAVAILABLE",
            sudo_password=password,
        )
        if not completed.stdout or len(completed.stdout) > 8 * 1024 * 1024:
            raise CandidateHarnessError("CANDIDATE_VM_PROFILE_RECEIPT_INVALID")
        try:
            value = json.loads(
                completed.stdout,
                object_pairs_hook=reject_duplicate_json_keys,
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
            raise CandidateHarnessError(
                "CANDIDATE_VM_PROFILE_RECEIPT_INVALID"
            ) from error
        if type(value) is not dict:
            raise CandidateHarnessError("CANDIDATE_VM_PROFILE_RECEIPT_INVALID")
        return value

    def _stop_clone(self, clone_vmx: Path) -> None:
        self._run(
            (str(VMRUN), "-T", "ws", "stop", str(clone_vmx), "soft"),
            code="CANDIDATE_VM_CLONE_SOFT_SHUTDOWN_FAILED",
            timeout=600,
        )
        for _ in range(60):
            if not self._is_running(clone_vmx):
                return
            time.sleep(2)
        raise CandidateHarnessError("CANDIDATE_VM_CLONE_SOFT_SHUTDOWN_FAILED")

    def _contain_clone(self, clone_vmx: Path) -> None:
        try:
            self._stop_clone(clone_vmx)
            return
        except CandidateHarnessError:
            pass
        for mode in ("soft", "hard"):
            try:
                self._run(
                    (str(VMRUN), "-T", "ws", "suspend", str(clone_vmx), mode),
                    code="CANDIDATE_VM_CLONE_CONTAINMENT_FAILED",
                    timeout=600,
                )
            except CandidateHarnessError:
                if mode == "soft":
                    continue
                raise
            for _ in range(60):
                if not self._is_running(clone_vmx):
                    return
                time.sleep(2)
        raise CandidateHarnessError("CANDIDATE_VM_CLONE_CONTAINMENT_FAILED")

    @classmethod
    def _remove_clone(cls, clone_root: Path) -> None:
        target = cls._closed_path(
            clone_root,
            root=VM_WORK_ROOT,
            code="CANDIDATE_VM_CLONE_PATH_INVALID",
        )
        if target.exists():
            shutil.rmtree(target)

    @classmethod
    def _quarantine_clone(cls, clone_root: Path, identity: str) -> None:
        source = cls._closed_path(
            clone_root,
            root=VM_WORK_ROOT,
            code="CANDIDATE_VM_CLONE_PATH_INVALID",
        )
        if not source.exists():
            return
        quarantine_root = VM_WORK_ROOT / "quarantine"
        quarantine_root.mkdir(parents=True, exist_ok=True)
        target = cls._closed_path(
            quarantine_root
            / (
                identity.removeprefix("sha256:")
                + "-"
                + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            ),
            root=quarantine_root,
            code="CANDIDATE_VM_QUARANTINE_PATH_INVALID",
        )
        if target.exists() or target.is_symlink():
            raise CandidateHarnessError("CANDIDATE_VM_QUARANTINE_PATH_EXISTS")
        shutil.move(str(source), str(target))

    def inspect_source(self) -> SourceVmEvidence:
        self._assert_tools()
        self._assert_source_stopped()
        if self._snapshot_names() != frozenset(SNAPSHOT_ALLOWLIST.values()):
            raise CandidateHarnessError("CANDIDATE_VM_SNAPSHOT_INVENTORY_INVALID")
        hashes = self._hashes()
        return SourceVmEvidence(
            vm_identity=SOURCE_VM_IDENTITY,
            snapshot_identities={
                profile: hashes[SNAPSHOT_FILES[profile]] for profile in PROFILES
            },
            original_hashes=hashes,
        )

    def execute_profile(
        self,
        *,
        plan: CandidateProfilePlan,
        harness_plan: CandidateHarnessPlan,
        candidate_root: Path,
        initial_platform_state: Mapping[str, bool],
    ) -> Mapping[str, Any]:
        if (
            plan not in harness_plan.profiles
            or plan.profile not in PROFILES
            or plan.snapshot_name != SNAPSHOT_ALLOWLIST[plan.profile]
            or dict(initial_platform_state) != _initial_platform_state(plan.profile)
        ):
            raise CandidateHarnessError("CANDIDATE_VM_PROFILE_PLAN_INVALID")
        self._assert_tools()
        before_hashes = self._hashes()
        if before_hashes != dict(harness_plan.original_vm_hashes):
            raise CandidateHarnessError("CANDIDATE_ORIGINAL_VM_MUTATED")
        clone_root: Path | None = None
        clone_vmx: Path | None = None
        start_attempted = False
        try:
            clone_root, clone_vmx = self._clone_full(plan)
            if self._hashes() != before_hashes:
                raise CandidateHarnessError("CANDIDATE_ORIGINAL_VM_MUTATED")
            self._revert_clone(clone_vmx, plan.snapshot_name)
            if self._hashes() != before_hashes:
                raise CandidateHarnessError("CANDIDATE_ORIGINAL_VM_MUTATED")
            self._validate_clone_disk_graph(clone_root, clone_vmx)
            start_attempted = True
            self._start_clone(clone_vmx)
            self._wait_for_ssh()
            guest_root = self._stage_candidate(
                candidate_root, harness_plan.candidate_input_digest
            )
            receipt = self._run_profile_guest(
                plan=plan,
                harness_plan=harness_plan,
                guest_root=guest_root,
                initial_platform_state=initial_platform_state,
            )
            self._stop_clone(clone_vmx)
            if self._hashes() != before_hashes:
                raise CandidateHarnessError("CANDIDATE_ORIGINAL_VM_MUTATED")
            self._remove_clone(clone_root)
            return receipt
        except BaseException:
            if clone_root is not None:
                if start_attempted and clone_vmx is not None:
                    try:
                        running = self._is_running(clone_vmx)
                    except CandidateHarnessError:
                        running = True
                    if running:
                        self._contain_clone(clone_vmx)
                self._quarantine_clone(clone_root, plan.clone_identity)
            raise

    def inspect_original_hashes(self) -> Mapping[str, str]:
        return self._hashes()

    @staticmethod
    def _absent_state(status: int) -> str:
        if status == 404:
            return "ABSENT"
        if status in {200, 206}:
            return "PRESENT"
        raise CandidateHarnessError("CANDIDATE_EXTERNAL_STATE_UNVERIFIED")

    def _github_state(self, path: str) -> str:
        status, _ = self._public.get(
            f"https://api.github.com/repos/{REPOSITORY}/{path}",
            {
                "Accept": "application/vnd.github+json",
                "User-Agent": "AniMemo-Candidate-Acceptance/1",
            },
        )
        return self._absent_state(status)

    def _ghcr_manifest_state(self, role: str) -> str:
        repository = f"yanyuhanyue/animemo-{role}"
        query = urllib.parse.urlencode(
            {
                "scope": f"repository:{repository}:pull",
                "service": "ghcr.io",
            }
        )
        status, body = self._public.get(
            "https://ghcr.io/token?" + query,
            {"User-Agent": "AniMemo-Candidate-Acceptance/1"},
        )
        if status != 200 or not body or len(body) > MAX_PUBLIC_RESPONSE_BYTES:
            raise CandidateHarnessError("CANDIDATE_EXTERNAL_STATE_UNVERIFIED")
        try:
            token_value = json.loads(
                body,
                object_pairs_hook=reject_duplicate_json_keys,
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
            raise CandidateHarnessError(
                "CANDIDATE_EXTERNAL_STATE_UNVERIFIED"
            ) from error
        token = token_value.get("token") if type(token_value) is dict else None
        if (
            type(token) is not str
            or not token
            or len(token) > 8192
            or any(ord(character) < 0x20 or ord(character) == 0x7F for character in token)
        ):
            raise CandidateHarnessError("CANDIDATE_EXTERNAL_STATE_UNVERIFIED")
        status, _ = self._public.get(
            f"https://ghcr.io/v2/{repository}/manifests/{RC14_VERSION}",
            {
                "Accept": (
                    "application/vnd.oci.image.index.v1+json, "
                    "application/vnd.oci.image.manifest.v1+json, "
                    "application/vnd.docker.distribution.manifest.list.v2+json, "
                    "application/vnd.docker.distribution.manifest.v2+json"
                ),
                "Authorization": "Bearer " + token,
                "User-Agent": "AniMemo-Candidate-Acceptance/1",
            },
        )
        return self._absent_state(status)

    def _public_r2_state(self) -> str:
        for key in R2_RC14_EXPECTED_KEYS:
            encoded_key = urllib.parse.quote(key, safe="")
            status, _ = self._public.get(
                f"{PUBLIC_MIRROR_ORIGIN}/{REPOSITORY}/releases/download/"
                f"{RC14_VERSION}/{encoded_key}",
                {
                    "Range": "bytes=0-0",
                    "User-Agent": "AniMemo-Candidate-Acceptance/1",
                },
            )
            if self._absent_state(status) != "ABSENT":
                return "PRESENT_BY_PUBLIC_READBACK_NON_AUTHORITATIVE"
        return "ABSENT_BY_PUBLIC_READBACK_NON_AUTHORITATIVE"

    def inspect_rc14_external_state(self) -> Mapping[str, str]:
        ghcr_states = {
            self._ghcr_manifest_state("api"),
            self._ghcr_manifest_state("web"),
        }
        ghcr = "ABSENT" if ghcr_states == {"ABSENT"} else "PRESENT"
        return {
            "tag": self._github_state(f"git/ref/tags/{RC14_VERSION}"),
            "github_release": self._github_state(
                f"releases/tags/{RC14_VERSION}"
            ),
            "ghcr": ghcr,
            "public_r2": self._public_r2_state(),
        }


def _validate_source_evidence(value: SourceVmEvidence) -> SourceVmEvidence:
    if (
        type(value) is not SourceVmEvidence
        or value.vm_identity != SOURCE_VM_IDENTITY
        or set(value.snapshot_identities) != set(PROFILES)
        or set(value.original_hashes) != set(SOURCE_VM_HASH_FILES)
        or any(
            not _DIGEST.fullmatch(value.snapshot_identities[profile])
            for profile in PROFILES
        )
        or not value.original_hashes
        or any(
            type(name) is not str
            or not name
            or type(digest) is not str
            or not _DIGEST.fullmatch(digest)
            for name, digest in value.original_hashes.items()
        )
    ):
        raise CandidateHarnessError("CANDIDATE_VM_SOURCE_IDENTITY_INVALID")
    return value


def build_harness_plan(
    *,
    verified_candidate_digest: str,
    expected_qualification_run_id: int,
    expected_source_sha: str,
    expected_source_tree: str,
    provider: CandidateVmProvider,
    _state_root: Path | None = None,
) -> CandidateHarnessPlan:
    if (
        isinstance(expected_qualification_run_id, bool)
        or expected_qualification_run_id <= 0
        or not _SHA.fullmatch(expected_source_sha)
        or not _SHA.fullmatch(expected_source_tree)
    ):
        raise CandidateHarnessError("CANDIDATE_HARNESS_EXPECTATION_INVALID")
    try:
        loaded = load_verified_candidate(
            verified_candidate_digest,
            _state_root=_state_root,
        )
    except CandidateContractError as error:
        raise CandidateHarnessError(error.code) from error
    candidate = loaded.candidate_input
    if (
        candidate["qualification_run_id"] != expected_qualification_run_id
        or candidate["qualification_run_attempt"] != 1
        or candidate["source_sha"] != expected_source_sha
        or candidate["source_tree"] != expected_source_tree
        or candidate["candidate_version"] != RC14_VERSION
    ):
        raise CandidateHarnessError("CANDIDATE_HARNESS_AUTHORITY_MISMATCH")
    source = _validate_source_evidence(provider.inspect_source())
    source_vm_digest = sha256_bytes(
        canonical_json_bytes(dict(sorted(source.original_hashes.items())))
    )
    profiles: list[CandidateProfilePlan] = []
    for profile in PROFILES:
        body = {
            "candidateInputDigest": loaded.verified["candidate_input_sha256"],
            "profile": profile,
            "snapshotIdentity": source.snapshot_identities[profile],
            "sourceVmDigest": source_vm_digest,
        }
        profiles.append(
            CandidateProfilePlan(
                profile=profile,
                installer_profile=INSTALLER_PROFILES[profile],
                snapshot_name=SNAPSHOT_ALLOWLIST[profile],
                snapshot_identity=source.snapshot_identities[profile],
                clone_identity=sha256_bytes(canonical_json_bytes(body)),
            )
        )
    provisional = CandidateHarnessPlan(
        verified_candidate_digest=loaded.verified_digest,
        candidate_input_digest=loaded.verified["candidate_input_sha256"],
        qualification_run_id=candidate["qualification_run_id"],
        source_sha=candidate["source_sha"],
        source_tree=candidate["source_tree"],
        candidate_version=candidate["candidate_version"],
        source_vm_identity=source.vm_identity,
        source_vm_digest=source_vm_digest,
        original_vm_hashes=dict(source.original_hashes),
        profiles=tuple(profiles),
        plan_digest="",
    )
    return CandidateHarnessPlan(
        **{
            **provisional.__dict__,
            "plan_digest": sha256_bytes(canonical_json_bytes(provisional.identity_body())),
        }
    )


def _initial_platform_state(profile: str) -> dict[str, bool]:
    return {
        "FRESH_BASE": {
            "docker_present": False,
            "runtime_dependencies_present": False,
            "network_allowed": True,
        },
        "DOCKER_BASE": {
            "docker_present": True,
            "runtime_dependencies_present": False,
            "network_allowed": True,
        },
        "RUNTIME_BASE_OFFLINE": {
            "docker_present": True,
            "runtime_dependencies_present": True,
            "network_allowed": False,
        },
    }[profile]


def _profile_digest(receipt: Mapping[str, Any]) -> str:
    return sha256_bytes(canonical_json_bytes(receipt))


def _read_expected_external_state(
    provider: CandidateVmProvider,
) -> dict[str, str]:
    try:
        state = dict(provider.inspect_rc14_external_state())
    except CandidateHarnessError:
        raise
    except (OSError, TypeError, ValueError) as error:
        raise CandidateHarnessError("CANDIDATE_EXTERNAL_STATE_UNVERIFIED") from error
    if state != EXPECTED_RC14_EXTERNAL_STATE:
        raise CandidateHarnessError("CANDIDATE_RC14_NOT_EMPTY")
    return state


def execute_harness_plan(
    plan: CandidateHarnessPlan,
    *,
    accepted_plan_digest: str,
    provider: CandidateVmProvider,
    environment: Mapping[str, str] | None = None,
    r2_client=None,
    _state_root: Path | None = None,
) -> dict[str, Any]:
    if (
        type(plan) is not CandidateHarnessPlan
        or accepted_plan_digest != plan.plan_digest
        or sha256_bytes(canonical_json_bytes(plan.identity_body()))
        != plan.plan_digest
    ):
        raise CandidateHarnessError("CANDIDATE_HARNESS_PLAN_NOT_ACCEPTED")
    try:
        r2_receipt = verify_rc14_r2_origin_from_environment(
            source_sha=plan.source_sha,
            source_tree=plan.source_tree,
            auth_method=R2_AUTH_METHOD_ARGUMENT,
            environment=environment,
            client=r2_client,
        )
        r2_receipt = validate_r2_origin_receipt(
            r2_receipt,
            expected_source_sha=plan.source_sha,
            expected_source_tree=plan.source_tree,
        )
        loaded = load_verified_candidate(
            plan.verified_candidate_digest,
            _state_root=_state_root,
        )
    except CandidateContractError as error:
        raise CandidateHarnessError(error.code) from error
    rc14_prestate = {
        **_read_expected_external_state(provider),
        "r2_origin": r2_receipt["result"],
    }
    receipts: dict[str, dict[str, Any]] = {}
    for item in plan.profiles:
        expected_initial_state = _initial_platform_state(item.profile)
        value = dict(
            provider.execute_profile(
                plan=item,
                harness_plan=plan,
                candidate_root=loaded.root,
                initial_platform_state=expected_initial_state,
            )
        )
        observed_original_hashes = dict(provider.inspect_original_hashes())
        try:
            receipt = validate_profile_receipt(value)
        except CandidateContractError as error:
            raise CandidateHarnessError(error.code) from error
        if (
            receipt["profile"] != item.profile
            or receipt["candidate_input_digest"] != plan.candidate_input_digest
            or receipt["verified_candidate_digest"]
            != plan.verified_candidate_digest
            or receipt["qualification_run_id"] != plan.qualification_run_id
            or receipt["qualification_run_attempt"] != 1
            or receipt["source_sha"] != plan.source_sha
            or receipt["source_tree"] != plan.source_tree
            or receipt["candidate_version"] != plan.candidate_version
            or receipt["base_vm_identity"] != plan.source_vm_digest
            or receipt["snapshot_identity"] != item.snapshot_identity
            or receipt["clone_identity"] != item.clone_identity
            or receipt["initial_platform_state"] != expected_initial_state
            or receipt["original_vm_pre_hashes"]
            != dict(plan.original_vm_hashes)
            or receipt["original_vm_post_hashes"]
            != observed_original_hashes
            or observed_original_hashes != dict(plan.original_vm_hashes)
            or receipt["result"] != "PASS"
        ):
            raise CandidateHarnessError("CANDIDATE_PROFILE_RECEIPT_BINDING_MISMATCH")
        receipts[item.profile] = receipt
    final_hashes = dict(provider.inspect_original_hashes())
    if final_hashes != dict(plan.original_vm_hashes):
        raise CandidateHarnessError("CANDIDATE_ORIGINAL_VM_MUTATED")
    rc14_poststate = {
        **_read_expected_external_state(provider),
        "r2_origin": r2_receipt["result"],
    }
    if rc14_poststate != rc14_prestate:
        raise CandidateHarnessError("CANDIDATE_RC14_STATE_DRIFT")
    completed = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    aggregate = {
        "schema": "animemo.prepublication-candidate-acceptance-receipt/v1",
        "version": 1,
        "candidate_input_digest": plan.candidate_input_digest,
        "verified_candidate_digest": plan.verified_candidate_digest,
        "qualification_run_id": plan.qualification_run_id,
        "qualification_run_attempt": 1,
        "source_sha": plan.source_sha,
        "source_tree": plan.source_tree,
        "candidate_version": plan.candidate_version,
        "r2_origin_prestate_receipt_digest": r2_origin_receipt_digest(r2_receipt),
        "profile_receipts": {
            "fresh_base": _profile_digest(receipts["FRESH_BASE"]),
            "docker_base": _profile_digest(receipts["DOCKER_BASE"]),
            "runtime_base_offline": _profile_digest(
                receipts["RUNTIME_BASE_OFFLINE"]
            ),
        },
        "all_profiles_pass": True,
        "rc14_prestate": rc14_prestate,
        "rc14_poststate": rc14_poststate,
        "repository_mutation_count": 0,
        "publication_mutation_count": 0,
        "shared_host_connection_count": 0,
        "secret_sweep": 0,
        "placeholder_sweep": 0,
        "release_authority_granted": False,
        "publish_authorized": False,
        "completed_at": completed,
        "result": "PASS",
        "receipt_digest": "",
    }
    unsigned = dict(aggregate)
    unsigned.pop("receipt_digest")
    aggregate["receipt_digest"] = sha256_bytes(canonical_json_bytes(unsigned))
    try:
        validate_aggregate_receipt(aggregate)
    except CandidateContractError as error:
        raise CandidateHarnessError(error.code) from error
    return {
        "status": "PASS",
        "aggregateReceipt": aggregate,
        "aggregateReceiptSha256": aggregate_receipt_digest(aggregate),
        "r2OriginPrestateReceipt": r2_receipt,
        "profileReceipts": receipts,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="AniMemo Candidate VM harness")
    parser.add_argument("--verified-candidate-digest", required=True)
    parser.add_argument("--expected-qualification-run-id", type=int, required=True)
    parser.add_argument("--expected-source-sha", required=True)
    parser.add_argument("--expected-source-tree", required=True)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--accept-plan-digest")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    provider: CandidateVmProvider = ClosedVmwareProvider()
    try:
        plan = build_harness_plan(
            verified_candidate_digest=args.verified_candidate_digest,
            expected_qualification_run_id=args.expected_qualification_run_id,
            expected_source_sha=args.expected_source_sha,
            expected_source_tree=args.expected_source_tree,
            provider=provider,
        )
        if not args.execute:
            print(json.dumps(plan.as_dict(), ensure_ascii=False, sort_keys=True))
            return 0
        if not args.accept_plan_digest:
            raise CandidateHarnessError("CANDIDATE_HARNESS_PLAN_CONFIRMATION_REQUIRED")
        result = execute_harness_plan(
            plan,
            accepted_plan_digest=args.accept_plan_digest,
            provider=provider,
            environment=os.environ,
        )
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except (CandidateHarnessError, CandidateContractError) as error:
        print(json.dumps({"code": getattr(error, "code", str(error))}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
