"""Plan-by-default three-profile Candidate VM acceptance harness.

This module owns the closed orchestration contract.  VM operations are behind
one typed provider seam so contract tests cannot accidentally start a VM and a
real provider cannot substitute paths, snapshots, profiles, or shell text.
"""

from __future__ import annotations

import argparse
import base64
import ctypes
import hashlib
import ipaddress
import json
import locale
import ntpath
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from ctypes import wintypes
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from release.candidate import (
    VERIFIED_CANDIDATE_ROOT,
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
    candidate_r2_expected_keys,
    r2_origin_receipt_digest,
    validate_r2_origin_receipt,
    verify_candidate_r2_origin_from_environment,
)

PROFILES = ("FRESH_BASE", "DOCKER_BASE", "RUNTIME_BASE_OFFLINE")
PROFILE_RESULT_KEYS = {
    "FRESH_BASE": "fresh_base",
    "DOCKER_BASE": "docker_base",
    "RUNTIME_BASE_OFFLINE": "runtime_base_offline",
}
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
SSH_KEYGEN = Path("C:/Windows/System32/OpenSSH/ssh-keygen.exe")
SSH_HOST = "192.168.64.10"
SSH_USER = "animemo"
VM_WORK_PARENT = Path("E:/番剧记录/.animemo-vm-work/candidate-acceptance")
OPENSSH_SESSION_ROOT = Path("E:/番剧记录/.animemo-vm-provider-authority")
OPENSSH_IDENTITY = OPENSSH_SESSION_ROOT / "id_ed25519"
SOURCE_VM_HASH_FILES = (
    "Ubuntu 64 位-000001.vmdk",
    "Ubuntu 64 位-000002.vmdk",
    "Ubuntu 64 位-000003.vmdk",
    "Ubuntu 64 位-Snapshot3.vmsn",
    "Ubuntu 64 位-Snapshot4.vmsn",
    "Ubuntu 64 位-Snapshot6.vmsn",
    "Ubuntu 64 位.vmdk",
    "Ubuntu 64 位.vmsd",
    "Ubuntu 64 位.vmx",
)
SNAPSHOT_FILES = {
    "FRESH_BASE": "Ubuntu 64 位-Snapshot3.vmsn",
    "DOCKER_BASE": "Ubuntu 64 位-Snapshot4.vmsn",
    "RUNTIME_BASE_OFFLINE": "Ubuntu 64 位-Snapshot6.vmsn",
}
SNAPSHOT_DISK_FILES = {
    "FRESH_BASE": "Ubuntu 64 位.vmdk",
    "DOCKER_BASE": "Ubuntu 64 位-000003.vmdk",
    "RUNTIME_BASE_OFFLINE": "Ubuntu 64 位-000001.vmdk",
}
PUBLIC_ORIGIN = "https://candidate.invalid"
TARGET_VERSION = "v1.1.0"
REPOSITORY = "yanyuhanyue/AniMemo"
PUBLIC_MIRROR_ORIGIN = "https://download.animemo.cc"
GUEST_CANDIDATE_ROOT = VERIFIED_CANDIDATE_ROOT.as_posix()
GUEST_PROFILE_RUNNER = "/usr/local/lib/animemo-candidate/candidate_profile_runner.py"
GUEST_RECEIPT = (
    "/var/lib/animemo/candidate-acceptance/profile-receipt-draft.json"
)
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
OPENSSH_EXCLUDED_AMBIENT_NAMES = frozenset({"HOME", "USERPROFILE"})
EXPECTED_CANDIDATE_EXTERNAL_STATE = {
    "tag": "ABSENT",
    "github_release": "ABSENT",
    "ghcr": "ABSENT",
    "public_r2": "ABSENT_BY_PUBLIC_READBACK_NON_AUTHORITATIVE",
}
# Compatibility name retained for archived RC14 evidence readers and tests.
EXPECTED_RC14_EXTERNAL_STATE = EXPECTED_CANDIDATE_EXTERNAL_STATE
EXPECTED_SSH_SHA256 = (
    "sha256:6250fd52163fe99a0dc49403ed1b4bbef9b764bdb7bada017a93d057d9376a42"
)
EXPECTED_SCP_SHA256 = (
    "sha256:63b7118d8e1a8a84398cf4ce1584dc6b146606092fe9c68bbaf110bbdcfb480a"
)
EXPECTED_SSH_KEYGEN_SHA256 = (
    "sha256:44c6809b7bbc917f1310ba92857f983e2788e9b0015aa7896fa0362eddb6338b"
)
EXPECTED_OPENSSH_PE_MACHINE = 0x8664
OPENSSH_REQUIRED_OPTIONS = (
    "BatchMode=yes",
    "IdentitiesOnly=yes",
    "IdentityAgent=none",
    "ProxyCommand=none",
    "ProxyJump=none",
    "PermitLocalCommand=no",
    "ClearAllForwardings=yes",
    "ForwardAgent=no",
    "PasswordAuthentication=no",
    "KbdInteractiveAuthentication=no",
    "PreferredAuthentications=publickey",
    "RequestTTY=no",
    "GlobalKnownHostsFile=none",
    f"User={SSH_USER}",
    "HashKnownHosts=no",
)
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_SHA = re.compile(r"[0-9a-f]{40}\Z")
_CANDIDATE_VERSION = re.compile(
    re.escape(TARGET_VERSION) + r"-rc\.[1-9][0-9]*\Z"
)


class CandidateHarnessError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class WindowsControlledFileInspection:
    """Secret-free classification of one controlled Windows file."""

    status: str
    win32_error: int | None = None


_CONTROLLED_FILE_PASS = "PASS"
_CONTROLLED_FILE_ERROR_CODES = {
    "ACL_QUERY_FAILED": "WINDOWS_OPENSSH_ACL_QUERY_FAILED",
    "ACL_UNSAFE": "WINDOWS_OPENSSH_ACL_UNSAFE",
    "OWNER_MISMATCH": "WINDOWS_OPENSSH_OWNER_MISMATCH",
    "ABI_UNSUPPORTED": "WINDOWS_WIN32_ABI_UNSUPPORTED",
    "SECURITY_DESCRIPTOR_INVALID": "WINDOWS_WIN32_SECURITY_DESCRIPTOR_INVALID",
    "RESOURCE_RELEASE_FAILED": "WINDOWS_OPENSSH_ACL_QUERY_FAILED",
}


class WindowsPlatformAuthority(Protocol):
    def resolve_program_data(self) -> str: ...

    def is_directory(self, path: str) -> bool: ...

    def has_reparse_component(self, path: str) -> bool: ...

    def is_fixed_drive(self, path: str) -> bool: ...

    def is_file(self, path: Path) -> bool: ...

    def inspect_binary(self, path: Path) -> WindowsBinaryIdentity: ...

    def inspect_controlled_file(
        self,
        path: Path,
        *,
        root: Path,
        private: bool,
    ) -> WindowsControlledFileInspection: ...


class _WindowsGuid(ctypes.Structure):
    _fields_ = (
        ("data1", wintypes.DWORD),
        ("data2", wintypes.WORD),
        ("data3", wintypes.WORD),
        ("data4", ctypes.c_ubyte * 8),
    )


_FOLDERID_PROGRAM_DATA = _WindowsGuid(
    0x62AB5D82,
    0xFDC1,
    0x4DC3,
    (ctypes.c_ubyte * 8)(0xA9, 0xDD, 0x07, 0x0D, 0x1D, 0x49, 0x5D, 0x97),
)


class _WindowsTrustee(ctypes.Structure):
    _fields_ = (
        ("multiple_trustee", ctypes.c_void_p),
        ("multiple_trustee_operation", ctypes.c_int),
        ("trustee_form", ctypes.c_int),
        ("trustee_type", ctypes.c_int),
        ("name", ctypes.c_void_p),
    )


class _WindowsSidAndAttributes(ctypes.Structure):
    _fields_ = (
        ("sid", ctypes.c_void_p),
        ("attributes", wintypes.DWORD),
    )


class _WindowsTokenUser(ctypes.Structure):
    _fields_ = (("user", _WindowsSidAndAttributes),)


class _WindowsApiAdapter:
    """Single typed Win32 FFI authority for the candidate harness."""

    _ERROR_INSUFFICIENT_BUFFER = 122
    _ERROR_INVALID_SID = 1337
    _ERROR_SUCCESS = 0
    _SE_FILE_OBJECT = 1
    _OWNER_SECURITY_INFORMATION = 0x00000001
    _DACL_SECURITY_INFORMATION = 0x00000004
    _TOKEN_QUERY = 0x0008
    _TOKEN_USER = 1
    _DRIVE_FIXED = 3
    _MAX_SID_SIZE = 68
    _BROAD_SID_TYPES = (1, 17, 27)

    def __init__(
        self,
        *,
        dll_loader: Any | None = None,
        last_error_reader: Callable[[], int] | None = None,
        last_error_writer: Callable[[int], None] | None = None,
    ) -> None:
        if dll_loader is None:
            try:
                dll_loader = ctypes.WinDLL
            except AttributeError as error:
                raise OSError("Windows Win32 ABI is unavailable") from error
        try:
            self._advapi32 = dll_loader("advapi32", use_last_error=True)
            self._kernel32 = dll_loader("kernel32", use_last_error=True)
            self._ole32 = dll_loader("ole32", use_last_error=True)
            self._shell32 = dll_loader("shell32", use_last_error=True)
            self._declare_prototypes()
        except (
            AttributeError,
            ctypes.ArgumentError,
            OSError,
            TypeError,
            ValueError,
        ) as error:
            raise OSError("Windows Win32 ABI is unavailable") from error
        if last_error_reader is None:
            last_error_reader = getattr(ctypes, "get_last_error", lambda: 0)
        if last_error_writer is None:
            last_error_writer = getattr(ctypes, "set_last_error", lambda _value: None)
        self._last_error_reader = last_error_reader
        self._last_error_writer = last_error_writer

    def _declare_prototypes(self) -> None:
        self._advapi32.CreateWellKnownSid.argtypes = (
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.POINTER(wintypes.DWORD),
        )
        self._advapi32.CreateWellKnownSid.restype = wintypes.BOOL
        self._advapi32.EqualSid.argtypes = (ctypes.c_void_p, ctypes.c_void_p)
        self._advapi32.EqualSid.restype = wintypes.BOOL
        self._advapi32.GetEffectiveRightsFromAclW.argtypes = (
            ctypes.c_void_p,
            ctypes.POINTER(_WindowsTrustee),
            ctypes.POINTER(wintypes.DWORD),
        )
        self._advapi32.GetEffectiveRightsFromAclW.restype = wintypes.DWORD
        self._advapi32.GetNamedSecurityInfoW.argtypes = (
            wintypes.LPCWSTR,
            ctypes.c_int,
            wintypes.DWORD,
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_void_p),
        )
        self._advapi32.GetNamedSecurityInfoW.restype = wintypes.DWORD
        self._advapi32.GetTokenInformation.argtypes = (
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        )
        self._advapi32.GetTokenInformation.restype = wintypes.BOOL
        self._advapi32.OpenProcessToken.argtypes = (
            wintypes.HANDLE,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.HANDLE),
        )
        self._advapi32.OpenProcessToken.restype = wintypes.BOOL

        self._kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        self._kernel32.CloseHandle.restype = wintypes.BOOL
        self._kernel32.GetCurrentProcess.argtypes = ()
        self._kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        self._kernel32.GetDriveTypeW.argtypes = (wintypes.LPCWSTR,)
        self._kernel32.GetDriveTypeW.restype = wintypes.UINT
        self._kernel32.LocalFree.argtypes = (ctypes.c_void_p,)
        self._kernel32.LocalFree.restype = ctypes.c_void_p

        self._ole32.CoTaskMemFree.argtypes = (ctypes.c_void_p,)
        self._ole32.CoTaskMemFree.restype = None
        self._shell32.SHGetKnownFolderPath.argtypes = (
            ctypes.POINTER(_WindowsGuid),
            wintypes.DWORD,
            wintypes.HANDLE,
            ctypes.POINTER(ctypes.c_wchar_p),
        )
        self._shell32.SHGetKnownFolderPath.restype = ctypes.c_long

    def _last_error(self) -> int:
        return int(self._last_error_reader())

    def _set_last_error(self, value: int) -> None:
        self._last_error_writer(value)

    def resolve_program_data(self) -> str:
        value = ctypes.c_wchar_p()
        result = self._shell32.SHGetKnownFolderPath(
            ctypes.byref(_FOLDERID_PROGRAM_DATA),
            0,
            None,
            ctypes.byref(value),
        )
        try:
            if result != 0 or value.value is None:
                raise OSError("FOLDERID_ProgramData is unavailable")
            return value.value
        finally:
            if value:
                self._ole32.CoTaskMemFree(ctypes.cast(value, ctypes.c_void_p))

    def is_fixed_drive(self, root: str) -> bool:
        return self._kernel32.GetDriveTypeW(root) == self._DRIVE_FIXED

    def inspect_file_acl(self, path: Path) -> WindowsControlledFileInspection:
        owner = ctypes.c_void_p()
        dacl = ctypes.c_void_p()
        descriptor = ctypes.c_void_p()
        token = wintypes.HANDLE()
        inspection = WindowsControlledFileInspection("ACL_QUERY_FAILED")
        descriptor_acquired = False
        token_acquired = False
        cleanup_failed = False
        cleanup_error: int | None = None
        try:
            result = self._advapi32.GetNamedSecurityInfoW(
                str(path),
                self._SE_FILE_OBJECT,
                self._OWNER_SECURITY_INFORMATION | self._DACL_SECURITY_INFORMATION,
                ctypes.byref(owner),
                None,
                ctypes.byref(dacl),
                None,
                ctypes.byref(descriptor),
            )
            if result != 0:
                inspection = WindowsControlledFileInspection(
                    "ACL_QUERY_FAILED", int(result)
                )
            else:
                descriptor_acquired = bool(descriptor.value)
                if not descriptor_acquired or not owner.value:
                    inspection = WindowsControlledFileInspection(
                        "SECURITY_DESCRIPTOR_INVALID"
                    )
                elif not dacl.value:
                    inspection = WindowsControlledFileInspection("ACL_UNSAFE")
                else:
                    opened = self._advapi32.OpenProcessToken(
                        self._kernel32.GetCurrentProcess(),
                        self._TOKEN_QUERY,
                        ctypes.byref(token),
                    )
                    if not opened:
                        inspection = WindowsControlledFileInspection(
                            "ACL_QUERY_FAILED", self._last_error()
                        )
                    elif not token.value:
                        inspection = WindowsControlledFileInspection(
                            "ACL_QUERY_FAILED"
                        )
                    else:
                        token_acquired = True
                        inspection = self._inspect_acl_with_token(owner, dacl, token)
        except ctypes.ArgumentError:
            inspection = WindowsControlledFileInspection("ABI_UNSUPPORTED")
        except (OSError, TypeError, ValueError):
            inspection = WindowsControlledFileInspection("ACL_QUERY_FAILED")
        finally:
            if token_acquired:
                try:
                    if not self._kernel32.CloseHandle(token):
                        cleanup_failed = True
                        cleanup_error = self._last_error()
                except (ctypes.ArgumentError, OSError, TypeError, ValueError):
                    cleanup_failed = True
            if descriptor_acquired:
                try:
                    released = self._kernel32.LocalFree(descriptor)
                    if released:
                        cleanup_failed = True
                        if cleanup_error is None:
                            cleanup_error = self._last_error()
                except (ctypes.ArgumentError, OSError, TypeError, ValueError):
                    cleanup_failed = True
        if cleanup_failed:
            return WindowsControlledFileInspection(
                "RESOURCE_RELEASE_FAILED", cleanup_error
            )
        return inspection

    def _inspect_acl_with_token(
        self,
        owner: ctypes.c_void_p,
        dacl: ctypes.c_void_p,
        token: wintypes.HANDLE,
    ) -> WindowsControlledFileInspection:
        required = wintypes.DWORD()
        first = self._advapi32.GetTokenInformation(
            token,
            self._TOKEN_USER,
            None,
            0,
            ctypes.byref(required),
        )
        if first:
            return WindowsControlledFileInspection("ACL_QUERY_FAILED")
        size_error = self._last_error()
        if required.value == 0 or size_error != self._ERROR_INSUFFICIENT_BUFFER:
            return WindowsControlledFileInspection("ACL_QUERY_FAILED", size_error)
        token_user_buffer = ctypes.create_string_buffer(required.value)
        if not self._advapi32.GetTokenInformation(
            token,
            self._TOKEN_USER,
            token_user_buffer,
            required.value,
            ctypes.byref(required),
        ):
            return WindowsControlledFileInspection(
                "ACL_QUERY_FAILED", self._last_error()
            )
        token_user = ctypes.cast(
            token_user_buffer, ctypes.POINTER(_WindowsTokenUser)
        ).contents
        current_user_sid = token_user.user.sid
        if not current_user_sid:
            return WindowsControlledFileInspection("SECURITY_DESCRIPTOR_INVALID")
        self._set_last_error(self._ERROR_SUCCESS)
        if not self._advapi32.EqualSid(owner, current_user_sid):
            sid_error = self._last_error()
            if sid_error == self._ERROR_SUCCESS:
                return WindowsControlledFileInspection("OWNER_MISMATCH")
            if sid_error == self._ERROR_INVALID_SID:
                return WindowsControlledFileInspection(
                    "SECURITY_DESCRIPTOR_INVALID", sid_error
                )
            return WindowsControlledFileInspection("ACL_QUERY_FAILED", sid_error)

        for well_known_sid_type in self._BROAD_SID_TYPES:
            sid_size = wintypes.DWORD(self._MAX_SID_SIZE)
            sid = ctypes.create_string_buffer(sid_size.value)
            if not self._advapi32.CreateWellKnownSid(
                well_known_sid_type,
                None,
                sid,
                ctypes.byref(sid_size),
            ):
                return WindowsControlledFileInspection(
                    "ACL_QUERY_FAILED", self._last_error()
                )
            trustee = _WindowsTrustee(
                None,
                0,
                0,
                0,
                ctypes.cast(sid, ctypes.c_void_p),
            )
            effective_access = wintypes.DWORD()
            result = self._advapi32.GetEffectiveRightsFromAclW(
                dacl,
                ctypes.byref(trustee),
                ctypes.byref(effective_access),
            )
            if result != 0:
                return WindowsControlledFileInspection(
                    "ACL_QUERY_FAILED", int(result)
                )
            if effective_access.value != 0:
                return WindowsControlledFileInspection("ACL_UNSAFE")
        return WindowsControlledFileInspection(_CONTROLLED_FILE_PASS)


class NativeWindowsPlatformAuthority:
    """Read non-secret Windows platform capabilities without ambient overrides."""

    def __init__(self, *, dll_loader: Any | None = None) -> None:
        self._dll_loader = dll_loader
        self._api: _WindowsApiAdapter | None = None

    def _windows_api(self) -> _WindowsApiAdapter:
        if os.name != "nt":
            raise OSError("Windows Win32 ABI is unavailable")
        if self._api is None:
            self._api = _WindowsApiAdapter(dll_loader=self._dll_loader)
        return self._api

    def resolve_program_data(self) -> str:
        if os.name != "nt":
            raise OSError("Windows Known Folder API is unavailable")
        return self._windows_api().resolve_program_data()

    @staticmethod
    def is_directory(path: str) -> bool:
        return Path(path).is_dir()

    @staticmethod
    def has_reparse_component(path: str) -> bool:
        candidate = Path(Path(path).anchor)
        for component in Path(path).parts[1:]:
            candidate /= component
            metadata = candidate.stat(follow_symlinks=False)
            attributes = getattr(metadata, "st_file_attributes", 0)
            if attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                return True
        return False

    def is_fixed_drive(self, path: str) -> bool:
        if os.name != "nt":
            return False
        drive, _ = ntpath.splitdrive(path)
        if re.fullmatch(r"[A-Za-z]:", drive) is None:
            return False
        return self._windows_api().is_fixed_drive(drive + "\\")

    @staticmethod
    def is_file(path: Path) -> bool:
        return path.is_file()

    def inspect_binary(self, path: Path) -> WindowsBinaryIdentity:
        if os.name != "nt" or not path.is_absolute() or not path.is_file():
            raise OSError("binary unavailable")
        if self.has_reparse_component(str(path)):
            raise OSError("binary path is a reparse point")
        try:
            with path.open("rb") as handle:
                header = handle.read(64)
                if len(header) != 64 or header[:2] != b"MZ":
                    raise OSError("invalid PE header")
                pe_offset = int.from_bytes(header[0x3C:0x40], "little")
                handle.seek(pe_offset)
                pe_header = handle.read(6)
                if len(pe_header) != 6 or pe_header[:4] != b"PE\0\0":
                    raise OSError("invalid PE signature")
                machine = int.from_bytes(pe_header[4:6], "little")
            return WindowsBinaryIdentity(
                sha256=_hash_regular_file(path),
                pe_machine=machine,
            )
        except (OSError, ValueError) as error:
            raise OSError("binary identity unavailable") from error

    def inspect_controlled_file(
        self,
        path: Path,
        *,
        root: Path,
        private: bool,
    ) -> WindowsControlledFileInspection:
        if os.name != "nt" or not path.is_absolute() or not root.is_absolute():
            return WindowsControlledFileInspection("ABI_UNSUPPORTED")
        try:
            if self.has_reparse_component(str(root)) or self.has_reparse_component(
                str(path)
            ):
                return WindowsControlledFileInspection("ACL_UNSAFE")
            resolved_root = root.resolve(strict=True)
            resolved = path.resolve(strict=True)
        except (OSError, RuntimeError):
            return WindowsControlledFileInspection("ACL_QUERY_FAILED")
        try:
            resolved.relative_to(resolved_root)
        except ValueError:
            return WindowsControlledFileInspection("ACL_UNSAFE")
        try:
            metadata = resolved.stat()
        except OSError:
            return WindowsControlledFileInspection("ACL_QUERY_FAILED")
        if resolved == resolved_root or not stat.S_ISREG(metadata.st_mode):
            return WindowsControlledFileInspection("ACL_UNSAFE")
        if private:
            try:
                api = self._windows_api()
            except OSError:
                return WindowsControlledFileInspection("ABI_UNSUPPORTED")
            return api.inspect_file_acl(resolved)
        return WindowsControlledFileInspection(_CONTROLLED_FILE_PASS)


@dataclass(frozen=True)
class WindowsProviderEnvironments:
    generic: Mapping[str, str]
    openssh: Mapping[str, str]


@dataclass(frozen=True)
class WindowsBinaryIdentity:
    sha256: str
    pe_machine: int


def _hash_regular_file(path: Path) -> str:
    try:
        before = path.lstat()
    except OSError as error:
        raise CandidateHarnessError(
            "CANDIDATE_VM_FILE_IDENTITY_UNAVAILABLE"
        ) from error
    if (
        path.is_symlink()
        or bool(getattr(path, "is_junction", lambda: False)())
        or not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
        or before.st_size < 1
    ):
        raise CandidateHarnessError("CANDIDATE_VM_FILE_IDENTITY_UNAVAILABLE")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise CandidateHarnessError(
            "CANDIDATE_VM_FILE_IDENTITY_UNAVAILABLE"
        ) from error
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or _file_identity(opened) != _file_identity(before)
        ):
            raise CandidateHarnessError(
                "CANDIDATE_VM_FILE_IDENTITY_UNAVAILABLE"
            )
        digest = hashlib.sha256()
        while chunk := os.read(descriptor, 1024 * 1024):
            digest.update(chunk)
        if _file_identity(os.fstat(descriptor)) != _file_identity(opened):
            raise CandidateHarnessError("CANDIDATE_VM_FILE_CHANGED_DURING_HASH")
        return "sha256:" + digest.hexdigest()
    finally:
        os.close(descriptor)


def _canonical_windows_path(value: str) -> str:
    return ntpath.normcase(ntpath.normpath(value))


def build_windows_provider_environments(
    source: Mapping[str, str],
    *,
    platform: WindowsPlatformAuthority | None = None,
) -> WindowsProviderEnvironments:
    """Build separate generic and OpenSSH environments from fixed authority."""

    logical_values: dict[str, str] = {}
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
        prior = logical_values.get(canonical_name)
        if prior is not None and prior != value:
            raise CandidateHarnessError("WINDOWS_OPENSSH_ENVIRONMENT_CONFLICT")
        logical_values[canonical_name] = value

    generic = {
        name: value
        for name, value in logical_values.items()
        if name in SAFE_HOST_ENVIRONMENT_NAMES
    }
    fixed_path = os.pathsep.join(
        dict.fromkeys(
            (
                str(VMRUN.parent),
                str(SSH.parent),
                str(ROBOCOPY.parent),
                str(Path(generic.get("SYSTEMROOT", "C:/Windows")) / "System32"),
                generic.get("SYSTEMROOT", "C:/Windows"),
            )
        )
    )
    generic["PATH"] = fixed_path

    authority = platform or NativeWindowsPlatformAuthority()
    try:
        program_data = authority.resolve_program_data()
    except (OSError, RuntimeError) as error:
        raise CandidateHarnessError(
            "WINDOWS_OPENSSH_PROGRAMDATA_UNAVAILABLE"
        ) from error
    if (
        type(program_data) is not str
        or not program_data
        or "\0" in program_data
        or program_data.startswith(("\\\\", "//"))
        or re.match(r"^[\\/]{2}[?.][\\/]", program_data) is not None
        or ntpath.isabs(program_data) is False
        or re.fullmatch(r"[A-Za-z]:", ntpath.splitdrive(program_data)[0]) is None
    ):
        raise CandidateHarnessError("WINDOWS_OPENSSH_PROGRAMDATA_INVALID")
    canonical_program_data = ntpath.normpath(program_data)
    try:
        valid_path = (
            authority.is_directory(canonical_program_data)
            and not authority.has_reparse_component(canonical_program_data)
            and authority.is_fixed_drive(canonical_program_data)
        )
    except OSError as error:
        raise CandidateHarnessError("WINDOWS_OPENSSH_PROGRAMDATA_INVALID") from error
    if not valid_path:
        raise CandidateHarnessError("WINDOWS_OPENSSH_PROGRAMDATA_INVALID")
    ambient_program_data = logical_values.get("PROGRAMDATA")
    if (
        ambient_program_data is not None
        and _canonical_windows_path(ambient_program_data)
        != _canonical_windows_path(canonical_program_data)
    ):
        raise CandidateHarnessError("WINDOWS_OPENSSH_PROGRAMDATA_INVALID")

    openssh = {
        name: value
        for name, value in generic.items()
        if name not in OPENSSH_EXCLUDED_AMBIENT_NAMES
    }
    openssh["PROGRAMDATA"] = canonical_program_data
    return WindowsProviderEnvironments(generic=generic, openssh=openssh)


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
    snapshot_disk_graph_identities: Mapping[str, str]
    source_disk_graph_identity: str
    original_hashes: Mapping[str, str]


@dataclass(frozen=True)
class ProviderReadinessReceipt:
    schema: str
    version: int
    environment_policy: str
    program_data_authority: str
    config_authority: str
    ssh_digest: str
    scp_digest: str
    ssh_keygen_digest: str
    architecture: str
    result: str
    receipt_digest: str

    @classmethod
    def issue(
        cls,
        *,
        ssh_digest: str,
        scp_digest: str,
        ssh_keygen_digest: str = EXPECTED_SSH_KEYGEN_SHA256,
    ) -> ProviderReadinessReceipt:
        body = {
            "schema": "animemo.candidate-vm-provider-readiness/v1",
            "version": 1,
            "environmentPolicy": "EXECUTABLE_SCOPED",
            "programDataAuthority": "WINDOWS_KNOWN_FOLDER_FID_PROGRAMDATA",
            "configAuthority": "PROVIDER_OWNED_CLOSED_OPENSSH",
            "sshDigest": ssh_digest,
            "scpDigest": scp_digest,
            "sshKeygenDigest": ssh_keygen_digest,
            "architecture": "AMD64",
            "result": "PASS",
        }
        return cls(
            schema=body["schema"],
            version=body["version"],
            environment_policy=body["environmentPolicy"],
            program_data_authority=body["programDataAuthority"],
            config_authority=body["configAuthority"],
            ssh_digest=body["sshDigest"],
            scp_digest=body["scpDigest"],
            ssh_keygen_digest=body["sshKeygenDigest"],
            architecture=body["architecture"],
            result=body["result"],
            receipt_digest=sha256_bytes(canonical_json_bytes(body)),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "version": self.version,
            "environmentPolicy": self.environment_policy,
            "programDataAuthority": self.program_data_authority,
            "configAuthority": self.config_authority,
            "sshDigest": self.ssh_digest,
            "scpDigest": self.scp_digest,
            "sshKeygenDigest": self.ssh_keygen_digest,
            "architecture": self.architecture,
            "result": self.result,
            "receiptDigest": self.receipt_digest,
        }


@dataclass(frozen=True)
class CandidateProfilePlan:
    profile: str
    installer_profile: str
    snapshot_name: str
    snapshot_identity: str
    snapshot_disk_graph_identity: str
    clone_identity: str
    provider_readiness_receipt_digest: str
    session_id: str
    connection_nonce: str
    ssh_host_key_alias: str

    def as_dict(self) -> dict[str, str]:
        return {
            "profile": self.profile,
            "installerProfile": self.installer_profile,
            "snapshotName": self.snapshot_name,
            "snapshotIdentity": self.snapshot_identity,
            "snapshotDiskGraphIdentity": self.snapshot_disk_graph_identity,
            "cloneIdentity": self.clone_identity,
            "sessionId": self.session_id,
            "connectionNonce": self.connection_nonce,
            "sshHostKeyAlias": self.ssh_host_key_alias,
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
    source_disk_graph_identity: str
    original_vm_hashes: Mapping[str, str]
    profiles: tuple[CandidateProfilePlan, ...]
    provider_readiness_receipt_digest: str
    session_id: str
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
            "sessionId": self.session_id,
            "sourceVmIdentity": self.source_vm_identity,
            "sourceVmDigest": self.source_vm_digest,
            "sourceDiskGraphIdentity": self.source_disk_graph_identity,
            "originalVmHashes": dict(sorted(self.original_vm_hashes.items())),
            "profiles": [profile.as_dict() for profile in self.profiles],
            "r2OriginProofRequiredBeforeClone": True,
            "releaseAuthorityGranted": False,
            "publishAuthorized": False,
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_body(), "planDigest": self.plan_digest}


@dataclass(frozen=True)
class ProfileConnectionAuthority:
    """Plan-derived, single-profile resource and connection authority."""

    session_root: Path
    profile_root: Path
    clone_root: Path
    clone_vmx: Path
    ssh_root: Path
    identity_file: Path
    known_hosts_file: Path
    quarantine_root: Path
    host_key_alias: str
    connection_nonce: str
    clone_identity: str


@dataclass(frozen=True)
class CloneRuntimeIdentity:
    clone_root: Path
    clone_vmx: Path
    vmx_digest: str
    disk_graph_digest: str
    snapshot_name: str
    snapshot_identity: str
    vm_uuid: str
    mac_address: str
    expected_ip: str


@dataclass(frozen=True)
class GuestConnectionObservation:
    machine_id: str
    boot_id: str
    mac_addresses: tuple[str, ...]
    nonce: str
    host_key_digest: str


@dataclass(frozen=True)
class VerifiedCloneConnection:
    authority: ProfileConnectionAuthority
    runtime: CloneRuntimeIdentity
    guest: GuestConnectionObservation


@dataclass(frozen=True)
class ProfileContinuationReceipt:
    profile: str
    session_id: str
    original_vm_hashes: Mapping[str, str]
    active_profile_root_count: int
    session_private_key_count: int
    known_hosts_file_count: int
    running_vm_count: int
    quarantine_present: bool
    continuation_safe: bool
    receipt_digest: str

    def identity_body(self) -> dict[str, object]:
        return {
            "schema": "animemo.candidate-profile-continuation-receipt/v1",
            "version": 1,
            "profile": self.profile,
            "sessionId": self.session_id,
            "originalVmHashes": dict(sorted(self.original_vm_hashes.items())),
            "activeProfileRootCount": self.active_profile_root_count,
            "sessionPrivateKeyCount": self.session_private_key_count,
            "knownHostsFileCount": self.known_hosts_file_count,
            "runningVmCount": self.running_vm_count,
            "quarantinePresent": self.quarantine_present,
            "continuationSafe": self.continuation_safe,
        }

    @classmethod
    def issue(
        cls,
        *,
        profile: str,
        session_id: str,
        original_vm_hashes: Mapping[str, str],
        active_profile_root_count: int,
        session_private_key_count: int,
        known_hosts_file_count: int,
        running_vm_count: int,
        quarantine_present: bool,
        continuation_safe: bool,
    ) -> ProfileContinuationReceipt:
        receipt = cls(
            profile=profile,
            session_id=session_id,
            original_vm_hashes=dict(original_vm_hashes),
            active_profile_root_count=active_profile_root_count,
            session_private_key_count=session_private_key_count,
            known_hosts_file_count=known_hosts_file_count,
            running_vm_count=running_vm_count,
            quarantine_present=quarantine_present,
            continuation_safe=continuation_safe,
            receipt_digest="",
        )
        return cls(
            **{
                **receipt.__dict__,
                "receipt_digest": sha256_bytes(
                    canonical_json_bytes(receipt.identity_body())
                ),
            }
        )


class CandidateProfileExecutionError(CandidateHarnessError):
    """A profile-local failure with provider-proven safe continuation."""

    def __init__(
        self,
        code: str,
        continuation_receipt: ProfileContinuationReceipt,
    ) -> None:
        super().__init__(code)
        self.continuation_receipt = continuation_receipt


@dataclass(frozen=True)
class ProviderSessionLease:
    path: Path
    file_identity: tuple[int, int, int, int]


def connection_challenge(connection_nonce: str, phase: str) -> str:
    if (
        re.fullmatch(r"[0-9a-f]{64}", connection_nonce) is None
        or phase != "guestinfo"
    ):
        raise CandidateHarnessError("CANDIDATE_VM_CONNECTION_CHALLENGE_INVALID")
    return sha256_bytes(f"{connection_nonce}|{phase}".encode("ascii")).removeprefix(
        "sha256:"
    )


def verify_bootstrap_clone_identity(
    *,
    authority: ProfileConnectionAuthority,
    plan: CandidateProfilePlan,
    runtime: CloneRuntimeIdentity,
    bootstrap: GuestConnectionObservation,
    confirmation: GuestConnectionObservation,
    known_hosts_was_absent: bool,
    competing_vmx_paths: frozenset[str],
) -> VerifiedCloneConnection:
    """Read-only gate that must pass before any privileged guest mutation."""

    if competing_vmx_paths:
        raise CandidateHarnessError("CANDIDATE_VM_CONNECTION_NAMESPACE_COLLISION")
    if not known_hosts_was_absent:
        raise CandidateHarnessError("CANDIDATE_VM_KNOWN_HOSTS_RESIDUAL")
    if (
        type(authority) is not ProfileConnectionAuthority
        or type(plan) is not CandidateProfilePlan
        or type(runtime) is not CloneRuntimeIdentity
        or type(bootstrap) is not GuestConnectionObservation
        or type(confirmation) is not GuestConnectionObservation
        or authority.clone_identity != plan.clone_identity
        or authority.clone_root.resolve(strict=False)
        != runtime.clone_root.resolve(strict=False)
        or authority.clone_vmx.resolve(strict=False)
        != runtime.clone_vmx.resolve(strict=False)
        or runtime.snapshot_name != plan.snapshot_name
        or runtime.snapshot_identity != plan.snapshot_identity
        or runtime.expected_ip != SSH_HOST
        or runtime.mac_address not in bootstrap.mac_addresses
        or runtime.mac_address not in confirmation.mac_addresses
        or bootstrap.machine_id != confirmation.machine_id
        or bootstrap.boot_id != confirmation.boot_id
        or bootstrap.mac_addresses != confirmation.mac_addresses
        or bootstrap.host_key_digest != confirmation.host_key_digest
        or bootstrap.nonce
        != connection_challenge(plan.connection_nonce, "guestinfo")
        or confirmation.nonce
        != connection_challenge(plan.connection_nonce, "guestinfo")
        or re.fullmatch(r"[0-9a-f]{32}", bootstrap.machine_id) is None
        or re.fullmatch(
            r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
            bootstrap.boot_id,
        )
        is None
        or not _DIGEST.fullmatch(runtime.vmx_digest)
        or not _DIGEST.fullmatch(runtime.disk_graph_digest)
        or not _DIGEST.fullmatch(bootstrap.host_key_digest)
    ):
        raise CandidateHarnessError("CANDIDATE_VM_CONNECTION_IDENTITY_MISMATCH")
    return VerifiedCloneConnection(
        authority=authority,
        runtime=runtime,
        guest=bootstrap,
    )


def verify_clone_connection_identity(
    *,
    authority: ProfileConnectionAuthority,
    plan: CandidateProfilePlan,
    runtime: CloneRuntimeIdentity,
    bootstrap: GuestConnectionObservation,
    verified_guest: GuestConnectionObservation,
    known_hosts_was_absent: bool,
    competing_vmx_paths: frozenset[str],
    prior_host_key_digests: frozenset[str],
) -> VerifiedCloneConnection:
    """Close the plan-to-guest identity chain before Candidate bytes move."""

    digest_fields = (
        runtime.vmx_digest,
        runtime.disk_graph_digest,
        runtime.snapshot_identity,
        bootstrap.host_key_digest,
        verified_guest.host_key_digest,
    )
    guest_shape_valid = all(
        (
            re.fullmatch(r"[0-9a-f]{32}", observation.machine_id) is not None
            and re.fullmatch(
                r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
                observation.boot_id,
            )
            is not None
            and observation.mac_addresses
            and all(
                re.fullmatch(r"[0-9a-f]{2}(?::[0-9a-f]{2}){5}", value)
                is not None
                for value in observation.mac_addresses
            )
            and re.fullmatch(r"[0-9a-f]{64}", observation.nonce) is not None
        )
        for observation in (bootstrap, verified_guest)
    )
    if (
        type(authority) is not ProfileConnectionAuthority
        or type(plan) is not CandidateProfilePlan
        or type(runtime) is not CloneRuntimeIdentity
        or type(bootstrap) is not GuestConnectionObservation
        or type(verified_guest) is not GuestConnectionObservation
        or type(known_hosts_was_absent) is not bool
        or any(not _DIGEST.fullmatch(value) for value in digest_fields)
        or not guest_shape_valid
        or authority.clone_identity != plan.clone_identity
        or authority.clone_root.resolve(strict=False)
        != runtime.clone_root.resolve(strict=False)
        or authority.clone_vmx.resolve(strict=False)
        != runtime.clone_vmx.resolve(strict=False)
        or runtime.snapshot_name != plan.snapshot_name
        or runtime.snapshot_identity != plan.snapshot_identity
        or re.fullmatch(
            r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
            runtime.vm_uuid,
        )
        is None
        or re.fullmatch(r"[0-9a-f]{2}(?::[0-9a-f]{2}){5}", runtime.mac_address)
        is None
        or runtime.expected_ip != SSH_HOST
        or bootstrap.machine_id != verified_guest.machine_id
        or bootstrap.boot_id != verified_guest.boot_id
        or runtime.mac_address not in bootstrap.mac_addresses
        or runtime.mac_address not in verified_guest.mac_addresses
        or bootstrap.nonce
        != connection_challenge(plan.connection_nonce, "guestinfo")
        or verified_guest.nonce
        != connection_challenge(plan.connection_nonce, "guestinfo")
    ):
        raise CandidateHarnessError("CANDIDATE_VM_CONNECTION_IDENTITY_MISMATCH")
    if competing_vmx_paths:
        raise CandidateHarnessError("CANDIDATE_VM_CONNECTION_NAMESPACE_COLLISION")
    if not known_hosts_was_absent:
        raise CandidateHarnessError("CANDIDATE_VM_KNOWN_HOSTS_RESIDUAL")
    if (
        bootstrap.host_key_digest == verified_guest.host_key_digest
        or verified_guest.host_key_digest in prior_host_key_digests
    ):
        raise CandidateHarnessError("CANDIDATE_VM_HOST_KEY_NOT_FRESH")
    return VerifiedCloneConnection(
        authority=authority,
        runtime=runtime,
        guest=verified_guest,
    )


class CandidateVmProvider(Protocol):
    def inspect_readiness(self) -> ProviderReadinessReceipt: ...

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

    def inspect_profile_continuation(
        self,
        *,
        plan: CandidateProfilePlan,
        harness_plan: CandidateHarnessPlan,
    ) -> ProfileContinuationReceipt: ...

    def inspect_candidate_external_state(
        self, candidate_version: str
    ) -> Mapping[str, str]: ...


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
        windows_platform: WindowsPlatformAuthority | None = None,
    ) -> None:
        self._runner = runner or SubprocessHostCommandRunner()
        self._public = public_transport or FixedPublicReadonlyAdapter()
        self._environment = environment if environment is not None else os.environ
        self._windows_platform = windows_platform or NativeWindowsPlatformAuthority()
        self._host_environment = self._sanitized_host_environment(self._environment)
        self._openssh_host_environment: Mapping[str, str] | None = None
        self._readiness: ProviderReadinessReceipt | None = None
        self._accepted_host_key_digests: set[str] = set()

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

    @classmethod
    def _hashes(cls) -> dict[str, str]:
        hashes = {
            name: _hash_original_vm_file(SOURCE_VM_ROOT / name)
            for name in SOURCE_VM_HASH_FILES
        }
        source_root = SOURCE_VM_ROOT.resolve(strict=True)
        for path in cls._closed_source_disk_graph_files(SOURCE_VM_ROOT):
            name = path.relative_to(source_root).as_posix()
            digest = _hash_original_vm_file(path)
            if name in hashes and hashes[name] != digest:
                raise CandidateHarnessError("CANDIDATE_VM_SOURCE_IDENTITY_INVALID")
            hashes[name] = digest
        return hashes

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

    @staticmethod
    def _profile_authority(
        plan: CandidateProfilePlan,
        harness_plan: CandidateHarnessPlan,
    ) -> ProfileConnectionAuthority:
        if (
            plan not in harness_plan.profiles
            or plan.session_id != harness_plan.session_id
            or re.fullmatch(r"[0-9a-f]{32}", harness_plan.session_id) is None
            or re.fullmatch(r"[0-9a-f]{64}", plan.connection_nonce) is None
            or re.fullmatch(r"animemo-[a-z0-9-]+", plan.ssh_host_key_alias) is None
            or not _DIGEST.fullmatch(harness_plan.candidate_input_digest)
            or not _DIGEST.fullmatch(plan.clone_identity)
            or _CANDIDATE_VERSION.fullmatch(harness_plan.candidate_version) is None
        ):
            raise CandidateHarnessError("CANDIDATE_VM_PROFILE_NAMESPACE_INVALID")
        session_root = (
            VM_WORK_PARENT
            / harness_plan.candidate_version
            / harness_plan.candidate_input_digest.removeprefix("sha256:")
            / harness_plan.session_id
        )
        profile_root = session_root / "profiles" / plan.profile.lower()
        clone_root = profile_root / "clone" / plan.clone_identity.removeprefix(
            "sha256:"
        )
        ssh_root = profile_root / "ssh"
        return ProfileConnectionAuthority(
            session_root=session_root,
            profile_root=profile_root,
            clone_root=clone_root,
            clone_vmx=clone_root / f"{SOURCE_VM_IDENTITY}.vmx",
            ssh_root=ssh_root,
            identity_file=ssh_root / "id_ed25519",
            known_hosts_file=ssh_root / "known_hosts",
            quarantine_root=session_root / "quarantine" / plan.profile.lower(),
            host_key_alias=plan.ssh_host_key_alias,
            connection_nonce=plan.connection_nonce,
            clone_identity=plan.clone_identity,
        )

    @classmethod
    def _acquire_provider_lease(
        cls, authority: ProfileConnectionAuthority
    ) -> ProviderSessionLease:
        if authority.session_root.parent == authority.session_root:
            raise CandidateHarnessError("CANDIDATE_VM_PROVIDER_SESSION_INVALID")
        try:
            VM_WORK_PARENT.mkdir(parents=True, exist_ok=True)
            lock_path = cls._closed_path(
                VM_WORK_PARENT / ".active-provider-session.lock",
                root=VM_WORK_PARENT,
                code="CANDIDATE_VM_PROVIDER_SESSION_INVALID",
            )
            descriptor = os.open(
                lock_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                stat.S_IRUSR | stat.S_IWUSR,
            )
            os.close(descriptor)
            metadata = lock_path.lstat()
        except FileExistsError as error:
            raise CandidateHarnessError("CANDIDATE_VM_PROVIDER_SESSION_BUSY") from error
        except CandidateHarnessError:
            raise
        except OSError as error:
            raise CandidateHarnessError(
                "CANDIDATE_VM_PROVIDER_SESSION_UNAVAILABLE"
            ) from error
        if lock_path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
            raise CandidateHarnessError("CANDIDATE_VM_PROVIDER_SESSION_INVALID")
        return ProviderSessionLease(
            path=lock_path,
            file_identity=_file_identity(metadata),
        )

    @classmethod
    def _release_provider_lease(cls, lease: ProviderSessionLease) -> None:
        try:
            path = cls._closed_path(
                lease.path,
                root=VM_WORK_PARENT,
                code="CANDIDATE_VM_PROVIDER_SESSION_INVALID",
            )
            metadata = path.lstat()
            if path.is_symlink() or _file_identity(metadata) != lease.file_identity:
                raise CandidateHarnessError(
                    "CANDIDATE_VM_PROVIDER_SESSION_OWNERSHIP_LOST"
                )
            path.unlink()
        except CandidateHarnessError:
            raise
        except OSError as error:
            raise CandidateHarnessError(
                "CANDIDATE_VM_PROVIDER_SESSION_RELEASE_FAILED"
            ) from error

    def _run(
        self,
        argv: Sequence[str],
        *,
        code: str,
        input_bytes: bytes | None = None,
        timeout: int = 300,
        allowed: frozenset[int] = frozenset({0}),
        openssh: bool = False,
    ) -> subprocess.CompletedProcess[bytes]:
        if not argv:
            raise CandidateHarnessError("WINDOWS_OPENSSH_CONFIG_AUTHORITY_UNSAFE")
        executable = _canonical_windows_path(str(argv[0]))
        is_openssh_executable = executable in {
            _canonical_windows_path(str(SSH)),
            _canonical_windows_path(str(SCP)),
        }
        if openssh is not is_openssh_executable:
            raise CandidateHarnessError("WINDOWS_OPENSSH_CONFIG_AUTHORITY_UNSAFE")
        environment = self._host_environment
        if openssh:
            environment = self._openssh_environment()
        try:
            completed = self._runner.run(
                tuple(argv),
                environment=environment,
                input_bytes=input_bytes,
                timeout=timeout,
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise CandidateHarnessError(code) from error
        if completed.returncode not in allowed:
            raise CandidateHarnessError(code)
        return completed

    def _openssh_environment(self) -> Mapping[str, str]:
        if self._openssh_host_environment is None:
            environments = build_windows_provider_environments(
                self._environment,
                platform=self._windows_platform,
            )
            self._openssh_host_environment = dict(environments.openssh)
        return self._openssh_host_environment

    def _bootstrap_identity(self) -> Path:
        try:
            if self._windows_platform.is_file(OPENSSH_IDENTITY):
                return OPENSSH_IDENTITY
        except OSError as error:
            raise CandidateHarnessError("WINDOWS_OPENSSH_ACL_QUERY_FAILED") from error
        raise CandidateHarnessError("WINDOWS_OPENSSH_IDENTITY_UNAVAILABLE")

    def inspect_readiness(self) -> ProviderReadinessReceipt:
        if self._readiness is not None:
            return self._readiness

        runner_path = Path(__file__).resolve().with_name("candidate_profile_runner.py")
        for path in (VMRUN, ROBOCOPY, SSH_KEYGEN, runner_path):
            try:
                available = self._windows_platform.is_file(path)
            except OSError as error:
                raise CandidateHarnessError(
                    "CANDIDATE_VM_TOOLCHAIN_UNAVAILABLE"
                ) from error
            if not available:
                raise CandidateHarnessError("CANDIDATE_VM_TOOLCHAIN_UNAVAILABLE")

        try:
            ssh_identity = self._windows_platform.inspect_binary(SSH)
            scp_identity = self._windows_platform.inspect_binary(SCP)
            ssh_keygen_identity = self._windows_platform.inspect_binary(SSH_KEYGEN)
        except OSError as error:
            raise CandidateHarnessError(
                "WINDOWS_OPENSSH_BINARY_UNAVAILABLE"
            ) from error
        if (
            ssh_identity
            != WindowsBinaryIdentity(
                sha256=EXPECTED_SSH_SHA256,
                pe_machine=EXPECTED_OPENSSH_PE_MACHINE,
            )
            or scp_identity
            != WindowsBinaryIdentity(
                sha256=EXPECTED_SCP_SHA256,
                pe_machine=EXPECTED_OPENSSH_PE_MACHINE,
            )
            or ssh_keygen_identity
            != WindowsBinaryIdentity(
                sha256=EXPECTED_SSH_KEYGEN_SHA256,
                pe_machine=EXPECTED_OPENSSH_PE_MACHINE,
            )
        ):
            raise CandidateHarnessError("WINDOWS_OPENSSH_IDENTITY_MISMATCH")

        bootstrap_identity = self._bootstrap_identity()
        try:
            identity_inspection = self._windows_platform.inspect_controlled_file(
                bootstrap_identity,
                root=bootstrap_identity.parent,
                private=True,
            )
        except OSError as error:
            raise CandidateHarnessError(
                "WINDOWS_OPENSSH_ACL_QUERY_FAILED"
            ) from error
        for inspection in (identity_inspection,):
            if inspection.status == _CONTROLLED_FILE_PASS:
                continue
            code = _CONTROLLED_FILE_ERROR_CODES.get(inspection.status)
            if code is None:
                raise CandidateHarnessError("WINDOWS_WIN32_ABI_UNSUPPORTED")
            raise CandidateHarnessError(code)

        self._openssh_environment()
        if self._openssh_closed_options(None)[1::2] != OPENSSH_REQUIRED_OPTIONS:
            raise CandidateHarnessError("WINDOWS_OPENSSH_CONFIG_AUTHORITY_UNSAFE")
        self._run(
            (str(SSH), "-V"),
            code="WINDOWS_OPENSSH_READINESS_FAILED",
            timeout=15,
            openssh=True,
        )
        self._readiness = ProviderReadinessReceipt.issue(
            ssh_digest=ssh_identity.sha256,
            scp_digest=scp_identity.sha256,
            ssh_keygen_digest=ssh_keygen_identity.sha256,
        )
        return self._readiness

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
        for path in (VMRUN, ROBOCOPY, SSH, SCP, SSH_KEYGEN, self._source_vmx):
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
    def _validate_clone_disk_graph(
        cls, clone_root: Path, clone_vmx: Path
    ) -> tuple[Path, ...]:
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
        graph_files = set(
            cls._validate_closed_disk_descriptors(clone_root, disk_references)
        )
        graph_files.add(clone_vmx.resolve(strict=True))
        return tuple(
            sorted(
                graph_files,
                key=lambda path: path.relative_to(clone_root.resolve(strict=True)).as_posix(),
            )
        )

    @classmethod
    def _validate_closed_disk_descriptors(
        cls,
        clone_root: Path,
        disk_references: list[str] | tuple[str, ...],
    ) -> tuple[Path, ...]:
        if not disk_references:
            raise CandidateHarnessError("CANDIDATE_VM_DISK_GRAPH_INVALID")
        descriptor_queue = [
            cls._closed_disk_reference(clone_root, reference)
            for reference in disk_references
        ]
        graph_files: set[Path] = set()
        seen_descriptors: set[Path] = set()
        while descriptor_queue:
            vmdk = descriptor_queue.pop()
            if vmdk in seen_descriptors:
                continue
            seen_descriptors.add(vmdk)
            graph_files.add(vmdk)
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
                graph_files.add(
                    cls._closed_disk_reference(
                        clone_root,
                        reference,
                        base=vmdk.parent,
                    )
                )
            if parent_ids and parent_ids[-1].lower() != "ffffffff" and len(parent_references) != 1:
                raise CandidateHarnessError("CANDIDATE_VM_DISK_GRAPH_INVALID")
        return tuple(
            sorted(
                graph_files,
                key=lambda path: path.relative_to(clone_root.resolve(strict=True)).as_posix(),
            )
        )

    @classmethod
    def _disk_graph_content_digest(cls, clone_root: Path, clone_vmx: Path) -> str:
        root = clone_root.resolve(strict=True)
        content_hashes = {
            path.relative_to(root).as_posix(): _hash_regular_file(path)
            for path in cls._validate_clone_disk_graph(clone_root, clone_vmx)
            if path.suffix.casefold() != ".vmx"
        }
        return sha256_bytes(canonical_json_bytes(content_hashes))

    @classmethod
    def _descriptor_disk_graph_content_digest(
        cls, clone_root: Path, descriptor: str
    ) -> str:
        root = clone_root.resolve(strict=True)
        content_hashes = {
            path.relative_to(root).as_posix(): _hash_regular_file(path)
            for path in cls._validate_closed_disk_descriptors(
                clone_root, (descriptor,)
            )
        }
        return sha256_bytes(canonical_json_bytes(content_hashes))

    @classmethod
    def _descriptor_disk_graph_digest_from_hashes(
        cls,
        root: Path,
        descriptor: str,
        hashes: Mapping[str, str],
    ) -> str:
        boundary = root.resolve(strict=True)
        names = tuple(
            path.relative_to(boundary).as_posix()
            for path in cls._validate_closed_disk_descriptors(root, (descriptor,))
        )
        if any(name not in hashes or not _DIGEST.fullmatch(hashes[name]) for name in names):
            raise CandidateHarnessError("CANDIDATE_VM_SOURCE_IDENTITY_INVALID")
        return sha256_bytes(
            canonical_json_bytes({name: hashes[name] for name in names})
        )

    @classmethod
    def _closed_source_disk_graph_files(cls, root: Path) -> tuple[Path, ...]:
        files: set[Path] = set()
        source_vmx = root / f"{SOURCE_VM_IDENTITY}.vmx"
        files.update(
            path
            for path in cls._validate_clone_disk_graph(root, source_vmx)
            if path.suffix.casefold() != ".vmx"
        )
        for descriptor in SNAPSHOT_DISK_FILES.values():
            files.update(cls._validate_closed_disk_descriptors(root, (descriptor,)))
        boundary = root.resolve(strict=True)
        return tuple(
            sorted(
                files,
                key=lambda item: item.relative_to(boundary).as_posix(),
            )
        )

    @classmethod
    def _closed_source_disk_graph_content_digest(cls, root: Path) -> str:
        boundary = root.resolve(strict=True)
        content_hashes = {
            path.relative_to(boundary).as_posix(): _hash_regular_file(path)
            for path in cls._closed_source_disk_graph_files(root)
        }
        return sha256_bytes(canonical_json_bytes(content_hashes))

    @classmethod
    def _closed_source_disk_graph_digest_from_hashes(
        cls, root: Path, hashes: Mapping[str, str]
    ) -> str:
        boundary = root.resolve(strict=True)
        names = tuple(
            path.relative_to(boundary).as_posix()
            for path in cls._closed_source_disk_graph_files(root)
        )
        if any(name not in hashes or not _DIGEST.fullmatch(hashes[name]) for name in names):
            raise CandidateHarnessError("CANDIDATE_VM_SOURCE_IDENTITY_INVALID")
        return sha256_bytes(
            canonical_json_bytes({name: hashes[name] for name in names})
        )

    @classmethod
    def _verify_clone_authoritative_hashes(
        cls,
        clone_root: Path,
        *,
        profile: str,
        expected_original_hashes: Mapping[str, str],
        expected_snapshot_identity: str,
        expected_source_disk_graph_identity: str,
    ) -> dict[str, str]:
        expected = dict(expected_original_hashes)
        snapshot_name = SNAPSHOT_FILES.get(profile)
        try:
            boundary = clone_root.resolve(strict=True)
            graph_names = {
                path.relative_to(boundary).as_posix()
                for path in cls._closed_source_disk_graph_files(clone_root)
            }
        except (CandidateHarnessError, OSError, ValueError) as error:
            raise CandidateHarnessError(
                "CANDIDATE_VM_FULL_COPY_IDENTITY_MISMATCH"
            ) from error
        allowed_names = set(SOURCE_VM_HASH_FILES) | graph_names
        if (
            set(expected) != allowed_names
            or len(expected) > 64
            or any(not _DIGEST.fullmatch(value) for value in expected.values())
            or snapshot_name is None
            or expected.get(snapshot_name) != expected_snapshot_identity
            or not _DIGEST.fullmatch(expected_source_disk_graph_identity)
        ):
            raise CandidateHarnessError(
                "CANDIDATE_VM_FULL_COPY_IDENTITY_MISMATCH"
            )
        observed = {
            name: _hash_regular_file(
                cls._closed_disk_reference(clone_root, name)
            )
            for name in sorted(expected)
        }
        if observed != expected:
            raise CandidateHarnessError(
                "CANDIDATE_VM_FULL_COPY_IDENTITY_MISMATCH"
            )
        if (
            cls._closed_source_disk_graph_content_digest(clone_root)
            != expected_source_disk_graph_identity
        ):
            raise CandidateHarnessError(
                "CANDIDATE_VM_FULL_COPY_IDENTITY_MISMATCH"
            )
        return observed

    @classmethod
    def _clone_snapshot_identity(
        cls,
        clone_root: Path,
        *,
        profile: str,
        expected_snapshot_identity: str,
    ) -> str:
        snapshot_name = SNAPSHOT_FILES.get(profile)
        if snapshot_name is None or not _DIGEST.fullmatch(expected_snapshot_identity):
            raise CandidateHarnessError("CANDIDATE_VM_SNAPSHOT_IDENTITY_MISMATCH")
        observed = _hash_regular_file(
            cls._closed_disk_reference(clone_root, snapshot_name)
        )
        if observed != expected_snapshot_identity:
            raise CandidateHarnessError("CANDIDATE_VM_SNAPSHOT_IDENTITY_MISMATCH")
        return observed

    @classmethod
    def _clone_snapshot_disk_graph_identity(
        cls,
        clone_root: Path,
        *,
        profile: str,
        expected_snapshot_disk_graph_identity: str,
    ) -> str:
        descriptor = SNAPSHOT_DISK_FILES.get(profile)
        if descriptor is None or not _DIGEST.fullmatch(
            expected_snapshot_disk_graph_identity
        ):
            raise CandidateHarnessError(
                "CANDIDATE_VM_SNAPSHOT_DISK_GRAPH_MISMATCH"
            )
        observed = cls._descriptor_disk_graph_content_digest(
            clone_root,
            descriptor,
        )
        if observed != expected_snapshot_disk_graph_identity:
            raise CandidateHarnessError(
                "CANDIDATE_VM_SNAPSHOT_DISK_GRAPH_MISMATCH"
            )
        return observed

    def _clone_full(
        self,
        plan: CandidateProfilePlan,
        authority: ProfileConnectionAuthority,
        *,
        expected_original_hashes: Mapping[str, str],
        expected_source_disk_graph_identity: str,
    ) -> tuple[Path, Path]:
        self._assert_source_stopped()
        clone_root = self._closed_path(
            authority.clone_root,
            root=authority.profile_root,
            code="CANDIDATE_VM_CLONE_PATH_INVALID",
        )
        if authority.clone_identity != plan.clone_identity:
            raise CandidateHarnessError("CANDIDATE_VM_PROFILE_NAMESPACE_INVALID")
        if clone_root.exists() or clone_root.is_symlink():
            raise CandidateHarnessError("CANDIDATE_VM_CLONE_PATH_EXISTS")
        try:
            source_hashes = self._hashes()
            if source_hashes != dict(expected_original_hashes):
                raise CandidateHarnessError("CANDIDATE_ORIGINAL_VM_MUTATED")
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
            self._verify_clone_authoritative_hashes(
                clone_root,
                profile=plan.profile,
                expected_original_hashes=expected_original_hashes,
                expected_snapshot_identity=plan.snapshot_identity,
                expected_source_disk_graph_identity=(
                    expected_source_disk_graph_identity
                ),
            )
            self._validate_clone_disk_graph(clone_root, clone_vmx)
            return clone_root, clone_vmx
        except BaseException:
            self._quarantine_clone(authority)
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
    def _inject_guestinfo_challenge(
        authority: ProfileConnectionAuthority,
        plan: CandidateProfilePlan,
    ) -> None:
        if authority.connection_nonce != plan.connection_nonce:
            raise CandidateHarnessError("CANDIDATE_VM_CONNECTION_CHALLENGE_INVALID")
        challenge = connection_challenge(plan.connection_nonce, "guestinfo")
        path = ClosedVmwareProvider._closed_path(
            authority.clone_vmx,
            root=authority.clone_root,
            code="CANDIDATE_VM_CONNECTION_CHALLENGE_INVALID",
        )
        temporary = path.with_name(path.name + ".animemo-guestinfo.tmp")
        try:
            original = ClosedVmwareProvider._configuration_prefix(path, complete=True)
            text = ClosedVmwareProvider._configuration_text(original)
            if re.search(
                r"(?im)^\s*guestinfo\.animemo\.connectionChallenge\s*=", text
            ):
                raise CandidateHarnessError(
                    "CANDIDATE_VM_CONNECTION_CHALLENGE_RESIDUAL"
                )
            payload = original
            if payload and not payload.endswith((b"\n", b"\r")):
                payload += b"\n"
            payload += (
                f'guestinfo.animemo.connectionChallenge = "{challenge}"\n'.encode(
                    "ascii"
                )
            )
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            descriptor = os.open(temporary, flags, stat.S_IRUSR | stat.S_IWUSR)
            try:
                with os.fdopen(descriptor, "wb", closefd=True) as handle:
                    descriptor = -1
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
            finally:
                if descriptor >= 0:
                    os.close(descriptor)
            os.replace(temporary, path)
        except CandidateHarnessError:
            raise
        except OSError as error:
            raise CandidateHarnessError(
                "CANDIDATE_VM_CONNECTION_CHALLENGE_INJECTION_FAILED"
            ) from error
        finally:
            try:
                if temporary.exists() or temporary.is_symlink():
                    temporary.unlink()
            except OSError:
                pass

    @staticmethod
    def _openssh_closed_options(
        authority: ProfileConnectionAuthority | None,
        *,
        capture_new_host_key: bool = False,
        bootstrap_identity: bool = False,
    ) -> tuple[str, ...]:
        values = list(OPENSSH_REQUIRED_OPTIONS)
        if authority is not None:
            values.extend(
                (
                    "StrictHostKeyChecking="
                    + ("accept-new" if capture_new_host_key else "yes"),
                    f"UserKnownHostsFile={authority.known_hosts_file}",
                    "IdentityFile="
                    + str(
                        OPENSSH_IDENTITY
                        if bootstrap_identity
                        else authority.identity_file
                    ),
                    f"HostKeyAlias={authority.host_key_alias}",
                )
            )
        return tuple(item for value in values for item in ("-o", value))

    @classmethod
    def _ssh_argv(
        cls,
        authority: ProfileConnectionAuthority,
        command: str,
        *,
        capture_new_host_key: bool = False,
        bootstrap_identity: bool = False,
    ) -> tuple[str, ...]:
        return (
            str(SSH),
            "-F",
            "none",
            *cls._openssh_closed_options(
                authority,
                capture_new_host_key=capture_new_host_key,
                bootstrap_identity=bootstrap_identity,
            ),
            "-o",
            "ConnectTimeout=10",
            "-o",
            "ConnectionAttempts=1",
            "--",
            SSH_HOST,
            command,
        )

    @classmethod
    def _scp_argv(
        cls,
        *,
        authority: ProfileConnectionAuthority,
        source: str,
        destination: str,
        recursive: bool,
    ) -> tuple[str, ...]:
        recursion = ("-r",) if recursive else ()
        return (
            str(SCP),
            "-F",
            "none",
            "-q",
            *recursion,
            *cls._openssh_closed_options(authority),
            "-o",
            "ConnectTimeout=10",
            "-o",
            "ConnectionAttempts=1",
            "--",
            source,
            f"{SSH_HOST}:{destination}",
        )

    def _wait_for_ssh(
        self,
        authority: ProfileConnectionAuthority,
        *,
        capture_new_host_key: bool,
        bootstrap_identity: bool = False,
    ) -> None:
        for _ in range(60):
            try:
                completed = self._runner.run(
                    self._ssh_argv(
                        authority,
                        "/usr/bin/true",
                        capture_new_host_key=capture_new_host_key,
                        bootstrap_identity=bootstrap_identity,
                    ),
                    environment=self._openssh_environment(),
                    timeout=15,
                )
            except (OSError, subprocess.SubprocessError):
                completed = None
            if completed is not None and completed.returncode == 0:
                return
            time.sleep(2)
        raise CandidateHarnessError("CANDIDATE_VM_GUEST_UNREACHABLE")

    def _prepare_profile_authority(
        self, authority: ProfileConnectionAuthority
    ) -> None:
        self._closed_path(
            authority.profile_root,
            root=VM_WORK_PARENT,
            code="CANDIDATE_VM_PROFILE_NAMESPACE_INVALID",
        )
        if authority.profile_root.exists() or authority.profile_root.is_symlink():
            raise CandidateHarnessError("CANDIDATE_VM_PROFILE_NAMESPACE_EXISTS")
        try:
            authority.ssh_root.mkdir(parents=True, exist_ok=False)
            if (
                authority.known_hosts_file.exists()
                or authority.known_hosts_file.is_symlink()
            ):
                raise CandidateHarnessError("CANDIDATE_VM_KNOWN_HOSTS_RESIDUAL")
            self._run(
                (
                    str(SSH_KEYGEN),
                    "-q",
                    "-t",
                    "ed25519",
                    "-N",
                    "",
                    "-C",
                    authority.host_key_alias,
                    "-f",
                    str(authority.identity_file),
                ),
                code="CANDIDATE_VM_SESSION_KEY_GENERATION_FAILED",
                timeout=30,
            )
            authority.identity_file.chmod(stat.S_IRUSR | stat.S_IWUSR)
            authority.identity_file.with_suffix(".pub").chmod(
                stat.S_IRUSR | stat.S_IWUSR
            )
            inspection = self._windows_platform.inspect_controlled_file(
                authority.identity_file,
                root=authority.ssh_root,
                private=True,
            )
        except CandidateHarnessError:
            raise
        except OSError as error:
            raise CandidateHarnessError(
                "CANDIDATE_VM_PROFILE_NAMESPACE_UNAVAILABLE"
            ) from error
        if inspection.status != _CONTROLLED_FILE_PASS:
            code = _CONTROLLED_FILE_ERROR_CODES.get(
                inspection.status, "WINDOWS_WIN32_ABI_UNSUPPORTED"
            )
            raise CandidateHarnessError(code)

    @staticmethod
    def _read_known_host_key(
        authority: ProfileConnectionAuthority,
    ) -> str:
        try:
            path = ClosedVmwareProvider._closed_path(
                authority.known_hosts_file,
                root=authority.ssh_root,
                code="CANDIDATE_VM_KNOWN_HOSTS_INVALID",
            )
            if (
                path.is_symlink()
                or not path.is_file()
                or path.stat().st_size > 64 * 1024
            ):
                raise CandidateHarnessError("CANDIDATE_VM_KNOWN_HOSTS_INVALID")
            lines = [
                line
                for line in path.read_text(
                    encoding="ascii", errors="strict"
                ).splitlines()
                if line
            ]
        except CandidateHarnessError:
            raise
        except (OSError, UnicodeError) as error:
            raise CandidateHarnessError("CANDIDATE_VM_KNOWN_HOSTS_INVALID") from error
        if len(lines) != 1:
            raise CandidateHarnessError("CANDIDATE_VM_KNOWN_HOSTS_INVALID")
        fields = lines[0].split()
        if (
            len(fields) != 3
            or fields[0] != authority.host_key_alias
            or re.fullmatch(r"(?:ssh-ed25519|ecdsa-sha2-nistp256|ssh-rsa)", fields[1])
            is None
            or re.fullmatch(r"[A-Za-z0-9+/]+={0,2}", fields[2]) is None
        ):
            raise CandidateHarnessError("CANDIDATE_VM_KNOWN_HOSTS_INVALID")
        try:
            base64.b64decode(fields[2], validate=True)
        except (ValueError, TypeError) as error:
            raise CandidateHarnessError("CANDIDATE_VM_KNOWN_HOSTS_INVALID") from error
        return sha256_bytes(f"{fields[1]} {fields[2]}".encode("ascii"))

    @staticmethod
    def _remove_known_hosts(authority: ProfileConnectionAuthority) -> None:
        path = ClosedVmwareProvider._closed_path(
            authority.known_hosts_file,
            root=authority.ssh_root,
            code="CANDIDATE_VM_KNOWN_HOSTS_INVALID",
        )
        try:
            if path.is_symlink() or not path.is_file():
                raise CandidateHarnessError("CANDIDATE_VM_KNOWN_HOSTS_INVALID")
            path.unlink()
        except CandidateHarnessError:
            raise
        except OSError as error:
            raise CandidateHarnessError("CANDIDATE_VM_KNOWN_HOSTS_INVALID") from error

    def _wait_for_guest_ip(self, clone_vmx: Path) -> str:
        for _ in range(60):
            try:
                completed = self._run(
                    (
                        str(VMRUN),
                        "-T",
                        "ws",
                        "getGuestIPAddress",
                        str(clone_vmx),
                        "-wait",
                    ),
                    code="CANDIDATE_VM_GUEST_IP_UNAVAILABLE",
                    timeout=15,
                )
                lines = self._decode_output(
                    completed.stdout,
                    code="CANDIDATE_VM_GUEST_IP_UNAVAILABLE",
                )
                if len(lines) == 1:
                    address = str(ipaddress.ip_address(lines[0]))
                    if address == SSH_HOST:
                        return address
                    raise CandidateHarnessError(
                        "CANDIDATE_VM_CONNECTION_IDENTITY_MISMATCH"
                    )
            except CandidateHarnessError as error:
                if error.code == "CANDIDATE_VM_CONNECTION_IDENTITY_MISMATCH":
                    raise
            time.sleep(2)
        raise CandidateHarnessError("CANDIDATE_VM_GUEST_IP_UNAVAILABLE")

    @staticmethod
    def _vmx_identity_fields(clone_vmx: Path) -> tuple[str, str]:
        text = ClosedVmwareProvider._configuration_text(
            ClosedVmwareProvider._configuration_prefix(clone_vmx, complete=True)
        )
        values: dict[str, str] = {}
        for line in text.splitlines():
            for key in ("uuid.bios", "ethernet0.generatedAddress", "ethernet0.address"):
                value = ClosedVmwareProvider._setting_value(line, key)
                if value is not None:
                    if key in values and values[key] != value:
                        raise CandidateHarnessError(
                            "CANDIDATE_VM_CONNECTION_IDENTITY_MISMATCH"
                        )
                    values[key] = value
        uuid_hex = re.sub(r"[ -]", "", values.get("uuid.bios", "").lower())
        mac_address = values.get(
            "ethernet0.generatedAddress", values.get("ethernet0.address", "")
        ).lower()
        if (
            re.fullmatch(r"[0-9a-f]{32}", uuid_hex) is None
            or re.fullmatch(r"[0-9a-f]{2}(?::[0-9a-f]{2}){5}", mac_address)
            is None
        ):
            raise CandidateHarnessError("CANDIDATE_VM_CONNECTION_IDENTITY_MISMATCH")
        vm_uuid = (
            f"{uuid_hex[:8]}-{uuid_hex[8:12]}-{uuid_hex[12:16]}-"
            f"{uuid_hex[16:20]}-{uuid_hex[20:]}"
        )
        return vm_uuid, mac_address

    def _read_clone_runtime_identity(
        self,
        authority: ProfileConnectionAuthority,
        plan: CandidateProfilePlan,
        *,
        expected_ip: str,
        preboot_disk_graph_digest: str,
        preboot_snapshot_identity: str,
    ) -> CloneRuntimeIdentity:
        self._validate_clone_disk_graph(authority.clone_root, authority.clone_vmx)
        vm_uuid, mac_address = self._vmx_identity_fields(authority.clone_vmx)
        if (
            not _DIGEST.fullmatch(preboot_disk_graph_digest)
            or preboot_snapshot_identity != plan.snapshot_identity
        ):
            raise CandidateHarnessError("CANDIDATE_VM_CONNECTION_IDENTITY_MISMATCH")
        return CloneRuntimeIdentity(
            clone_root=authority.clone_root,
            clone_vmx=authority.clone_vmx,
            vmx_digest=_hash_regular_file(authority.clone_vmx),
            disk_graph_digest=preboot_disk_graph_digest,
            snapshot_name=plan.snapshot_name,
            snapshot_identity=preboot_snapshot_identity,
            vm_uuid=vm_uuid,
            mac_address=mac_address,
            expected_ip=expected_ip,
        )

    def _observe_guest_connection(
        self,
        authority: ProfileConnectionAuthority,
        *,
        host_key_digest: str,
        bootstrap_identity: bool = False,
    ) -> GuestConnectionObservation:
        code = (
            'import glob,json,subprocess;challenge=subprocess.run(['
            '"/usr/bin/vmtoolsd","--cmd",'
            '"info-get guestinfo.animemo.connectionChallenge"],'
            'check=True,capture_output=True,text=True,timeout=10).stdout.strip();'
            'print(json.dumps({'
            '"schema":"animemo.clone-guest-identity/v1",'
            '"nonce":challenge,'
            '"machine_id":open("/etc/machine-id",encoding="ascii").read().strip(),'
            '"boot_id":open("/proc/sys/kernel/random/boot_id",encoding="ascii").read().strip(),'
            '"mac_addresses":sorted(open(value,encoding="ascii").read().strip() '
            'for value in glob.glob("/sys/class/net/*/address"))},'
            'sort_keys=True,separators=(",",":")))'
        )
        completed = self._ssh_checked(
            authority,
            "/usr/bin/python3 -P -B -c '" + code + "'",
            code="CANDIDATE_VM_GUEST_IDENTITY_UNAVAILABLE",
            timeout=30,
            bootstrap_identity=bootstrap_identity,
        )
        try:
            value = json.loads(
                completed.stdout,
                object_pairs_hook=reject_duplicate_json_keys,
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
            raise CandidateHarnessError(
                "CANDIDATE_VM_GUEST_IDENTITY_INVALID"
            ) from error
        if (
            type(value) is not dict
            or set(value)
            != {"schema", "nonce", "machine_id", "boot_id", "mac_addresses"}
            or value["schema"] != "animemo.clone-guest-identity/v1"
            or type(value["mac_addresses"]) is not list
            or any(type(item) is not str for item in value["mac_addresses"])
        ):
            raise CandidateHarnessError("CANDIDATE_VM_GUEST_IDENTITY_INVALID")
        return GuestConnectionObservation(
            machine_id=value["machine_id"],
            boot_id=value["boot_id"],
            mac_addresses=tuple(value["mac_addresses"]),
            nonce=value["nonce"],
            host_key_digest=host_key_digest,
        )

    @staticmethod
    def _session_public_key(authority: ProfileConnectionAuthority) -> str:
        path = authority.identity_file.with_suffix(".pub")
        try:
            value = path.read_text(encoding="ascii", errors="strict").strip()
        except (OSError, UnicodeError) as error:
            raise CandidateHarnessError(
                "CANDIDATE_VM_SESSION_KEY_INVALID"
            ) from error
        fields = value.split()
        if (
            len(fields) != 3
            or fields[0] != "ssh-ed25519"
            or re.fullmatch(r"[A-Za-z0-9+/]+={0,2}", fields[1]) is None
            or fields[2] != authority.host_key_alias
        ):
            raise CandidateHarnessError("CANDIDATE_VM_SESSION_KEY_INVALID")
        try:
            base64.b64decode(fields[1], validate=True)
        except (TypeError, ValueError) as error:
            raise CandidateHarnessError(
                "CANDIDATE_VM_SESSION_KEY_INVALID"
            ) from error
        return value

    def _provision_session_key_and_rotate_host_key(
        self, authority: ProfileConnectionAuthority
    ) -> None:
        public_key = self._session_public_key(authority)
        script = (
            "/usr/bin/install -d -o animemo -g animemo -m 0700 /home/animemo/.ssh; "
            f'/usr/bin/printf "%s\\n" "{public_key}" '
            "> /home/animemo/.ssh/authorized_keys; "
            "/bin/chown animemo:animemo /home/animemo/.ssh/authorized_keys; "
            "/bin/chmod 0600 /home/animemo/.ssh/authorized_keys; "
            "/bin/rm -f -- /etc/ssh/ssh_host_*_key /etc/ssh/ssh_host_*_key.pub; "
            "/usr/bin/ssh-keygen -A; /usr/bin/systemctl restart ssh"
        )
        self._ssh_checked(
            authority,
            "sudo -S -p '' -- /bin/sh -ceu '" + script + "'",
            code="CANDIDATE_VM_HOST_KEY_ROTATION_FAILED",
            sudo_password=self._sudo_password(),
            timeout=120,
            bootstrap_identity=True,
        )

    def _establish_clone_connection(
        self,
        authority: ProfileConnectionAuthority,
        plan: CandidateProfilePlan,
        *,
        preboot_disk_graph_digest: str,
        preboot_snapshot_identity: str,
    ) -> VerifiedCloneConnection:
        running = self._running_vmx_paths()
        expected_vmx = os.path.normcase(str(authority.clone_vmx.resolve(strict=False)))
        if expected_vmx not in running:
            raise CandidateHarnessError("CANDIDATE_VM_CONNECTION_IDENTITY_MISMATCH")
        competitors = frozenset(value for value in running if value != expected_vmx)
        if competitors:
            raise CandidateHarnessError("CANDIDATE_VM_CONNECTION_NAMESPACE_COLLISION")
        expected_ip = self._wait_for_guest_ip(authority.clone_vmx)
        runtime = self._read_clone_runtime_identity(
            authority,
            plan,
            expected_ip=expected_ip,
            preboot_disk_graph_digest=preboot_disk_graph_digest,
            preboot_snapshot_identity=preboot_snapshot_identity,
        )
        known_hosts_was_absent = not (
            authority.known_hosts_file.exists()
            or authority.known_hosts_file.is_symlink()
        )
        if not known_hosts_was_absent:
            raise CandidateHarnessError("CANDIDATE_VM_KNOWN_HOSTS_RESIDUAL")
        self._wait_for_ssh(
            authority,
            capture_new_host_key=True,
            bootstrap_identity=True,
        )
        bootstrap_host_key = self._read_known_host_key(authority)
        bootstrap = self._observe_guest_connection(
            authority,
            host_key_digest=bootstrap_host_key,
            bootstrap_identity=True,
        )
        confirmation = self._observe_guest_connection(
            authority,
            host_key_digest=bootstrap_host_key,
            bootstrap_identity=True,
        )
        verify_bootstrap_clone_identity(
            authority=authority,
            plan=plan,
            runtime=runtime,
            bootstrap=bootstrap,
            confirmation=confirmation,
            known_hosts_was_absent=known_hosts_was_absent,
            competing_vmx_paths=competitors,
        )
        self._provision_session_key_and_rotate_host_key(authority)
        self._remove_known_hosts(authority)
        self._wait_for_ssh(authority, capture_new_host_key=True)
        verified_host_key = self._read_known_host_key(authority)
        verified_guest = self._observe_guest_connection(
            authority,
            host_key_digest=verified_host_key,
        )
        verified = verify_clone_connection_identity(
            authority=authority,
            plan=plan,
            runtime=runtime,
            bootstrap=bootstrap,
            verified_guest=verified_guest,
            known_hosts_was_absent=known_hosts_was_absent,
            competing_vmx_paths=competitors,
            prior_host_key_digests=frozenset(self._accepted_host_key_digests),
        )
        self._accepted_host_key_digests.add(verified.guest.host_key_digest)
        return verified

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
        authority: ProfileConnectionAuthority,
        command: str,
        *,
        code: str,
        sudo_password: bytes | None = None,
        timeout: int = 300,
        bootstrap_identity: bool = False,
    ) -> subprocess.CompletedProcess[bytes]:
        return self._run(
            self._ssh_argv(
                authority,
                command,
                bootstrap_identity=bootstrap_identity,
            ),
            code=code,
            input_bytes=sudo_password,
            timeout=timeout,
            openssh=True,
        )

    def _stage_candidate(
        self,
        authority: ProfileConnectionAuthority,
        candidate_root: Path,
        candidate_digest: str,
    ) -> str:
        if not _DIGEST.fullmatch(candidate_digest):
            raise CandidateHarnessError("CANDIDATE_VM_STAGE_IDENTITY_INVALID")
        digest_hex = candidate_digest.removeprefix("sha256:")
        guest_root = f"{GUEST_CANDIDATE_ROOT}/{digest_hex}"
        password = self._sudo_password()
        self._ssh_checked(
            authority,
            "/bin/rm -rf -- /tmp/animemo-candidate-stage "
            "/tmp/animemo-candidate-profile-runner.py",
            code="CANDIDATE_VM_STAGE_FAILED",
        )
        self._run(
            self._scp_argv(
                authority=authority,
                source=str(candidate_root),
                destination="/tmp/animemo-candidate-stage",
                recursive=True,
            ),
            code="CANDIDATE_VM_STAGE_FAILED",
            timeout=60 * 60,
            openssh=True,
        )
        runner_path = Path(__file__).resolve().with_name("candidate_profile_runner.py")
        self._run(
            self._scp_argv(
                authority=authority,
                source=str(runner_path),
                destination="/tmp/animemo-candidate-profile-runner.py",
                recursive=False,
            ),
            code="CANDIDATE_VM_STAGE_FAILED",
            timeout=300,
            openssh=True,
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
                authority,
                "sudo -S -p '' -- " + command,
                code="CANDIDATE_VM_STAGE_FAILED",
                sudo_password=password,
            )
        return guest_root

    def _run_profile_guest(
        self,
        *,
        authority: ProfileConnectionAuthority,
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
            "PYTHONSAFEPATH=1 "
            f"PYTHONPATH={guest_root}/installer-root "
            f"/usr/bin/python3 -P -B {GUEST_PROFILE_RUNNER} "
            f"--verified-candidate-digest {harness_plan.verified_candidate_digest} "
            f"--profile {plan.profile} --public-origin {PUBLIC_ORIGIN} --execute"
        )
        password = self._sudo_password()
        self._ssh_checked(
            authority,
            command,
            code="CANDIDATE_VM_PROFILE_EXECUTION_FAILED",
            sudo_password=password,
            timeout=4 * 60 * 60,
        )
        completed = self._ssh_checked(
            authority,
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

    @staticmethod
    def _destroy_session_key(authority: ProfileConnectionAuthority) -> None:
        for path in (
            authority.identity_file,
            authority.identity_file.with_suffix(".pub"),
        ):
            try:
                target = ClosedVmwareProvider._closed_path(
                    path,
                    root=authority.ssh_root,
                    code="CANDIDATE_VM_SESSION_KEY_INVALID",
                )
                if target.exists() or target.is_symlink():
                    target.unlink()
            except CandidateHarnessError:
                raise
            except OSError as error:
                raise CandidateHarnessError(
                    "CANDIDATE_VM_SESSION_KEY_DELETION_FAILED"
                ) from error

    @staticmethod
    def _destroy_known_hosts(authority: ProfileConnectionAuthority) -> None:
        try:
            target = ClosedVmwareProvider._closed_path(
                authority.known_hosts_file,
                root=authority.profile_root,
                code="CANDIDATE_VM_KNOWN_HOSTS_INVALID",
            )
            if target.exists() or target.is_symlink():
                target.unlink()
        except CandidateHarnessError:
            raise
        except OSError as error:
            raise CandidateHarnessError(
                "CANDIDATE_VM_KNOWN_HOSTS_DELETION_FAILED"
            ) from error

    @classmethod
    def _remove_clone(cls, verified: VerifiedCloneConnection) -> None:
        if (
            type(verified) is not VerifiedCloneConnection
            or verified.authority.clone_root.resolve(strict=False)
            != verified.runtime.clone_root.resolve(strict=False)
            or verified.authority.clone_vmx.resolve(strict=False)
            != verified.runtime.clone_vmx.resolve(strict=False)
        ):
            raise CandidateHarnessError("CANDIDATE_VM_CLEANUP_AUTHORITY_INVALID")
        target = cls._closed_path(
            verified.authority.profile_root,
            root=verified.authority.session_root,
            code="CANDIDATE_VM_CLONE_PATH_INVALID",
        )
        if target.exists():
            shutil.rmtree(target)

    @classmethod
    def _quarantine_clone(cls, authority: ProfileConnectionAuthority) -> None:
        source = cls._closed_path(
            authority.profile_root,
            root=authority.session_root,
            code="CANDIDATE_VM_CLONE_PATH_INVALID",
        )
        if not source.exists():
            return
        cls._destroy_session_key(authority)
        cls._destroy_known_hosts(authority)
        quarantine_root = authority.quarantine_root
        quarantine_root.mkdir(parents=True, exist_ok=True)
        target = cls._closed_path(
            quarantine_root
            / (
                authority.clone_identity.removeprefix("sha256:")
                + "-"
                + authority.connection_nonce[:16]
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
        self.inspect_readiness()
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
            snapshot_disk_graph_identities={
                profile: self._descriptor_disk_graph_digest_from_hashes(
                    SOURCE_VM_ROOT,
                    SNAPSHOT_DISK_FILES[profile],
                    hashes,
                )
                for profile in PROFILES
            },
            source_disk_graph_identity=(
                self._closed_source_disk_graph_digest_from_hashes(
                    SOURCE_VM_ROOT,
                    hashes,
                )
            ),
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
        readiness = self.inspect_readiness()
        if (
            plan not in harness_plan.profiles
            or plan.profile not in PROFILES
            or plan.snapshot_name != SNAPSHOT_ALLOWLIST[plan.profile]
            or plan.provider_readiness_receipt_digest
            != readiness.receipt_digest
            or harness_plan.provider_readiness_receipt_digest
            != readiness.receipt_digest
            or dict(initial_platform_state) != _initial_platform_state(plan.profile)
        ):
            raise CandidateHarnessError("CANDIDATE_VM_PROFILE_PLAN_INVALID")
        self._assert_tools()
        before_hashes = self._hashes()
        if before_hashes != dict(harness_plan.original_vm_hashes):
            raise CandidateHarnessError("CANDIDATE_ORIGINAL_VM_MUTATED")
        clone_root: Path | None = None
        clone_vmx: Path | None = None
        authority = self._profile_authority(plan, harness_plan)
        lease = self._acquire_provider_lease(authority)
        verified_connection: VerifiedCloneConnection | None = None
        profile_failure: CandidateHarnessError | None = None
        start_attempted = False
        try:
            self._prepare_profile_authority(authority)
            clone_root, clone_vmx = self._clone_full(
                plan,
                authority,
                expected_original_hashes=harness_plan.original_vm_hashes,
                expected_source_disk_graph_identity=(
                    harness_plan.source_disk_graph_identity
                ),
            )
            if (
                clone_root.resolve(strict=False)
                != authority.clone_root.resolve(strict=False)
                or clone_vmx.resolve(strict=False)
                != authority.clone_vmx.resolve(strict=False)
            ):
                raise CandidateHarnessError(
                    "CANDIDATE_VM_CONNECTION_IDENTITY_MISMATCH"
                )
            if self._hashes() != before_hashes:
                raise CandidateHarnessError("CANDIDATE_ORIGINAL_VM_MUTATED")
            self._revert_clone(clone_vmx, plan.snapshot_name)
            if self._hashes() != before_hashes:
                raise CandidateHarnessError("CANDIDATE_ORIGINAL_VM_MUTATED")
            active_graph_files = self._validate_clone_disk_graph(
                clone_root,
                clone_vmx,
            )
            active_disk_names = {
                path.relative_to(clone_root.resolve(strict=True)).as_posix()
                for path in active_graph_files
                if path.suffix.casefold() == ".vmdk"
            }
            if not active_disk_names.issubset(harness_plan.original_vm_hashes):
                raise CandidateHarnessError(
                    "CANDIDATE_VM_REVERT_DISK_GRAPH_UNPLANNED"
                )
            self._clone_snapshot_disk_graph_identity(
                clone_root,
                profile=plan.profile,
                expected_snapshot_disk_graph_identity=(
                    plan.snapshot_disk_graph_identity
                ),
            )
            self._inject_guestinfo_challenge(authority, plan)
            preboot_snapshot_identity = self._clone_snapshot_identity(
                clone_root,
                profile=plan.profile,
                expected_snapshot_identity=plan.snapshot_identity,
            )
            preboot_disk_graph_digest = self._disk_graph_content_digest(
                clone_root, clone_vmx
            )
            start_attempted = True
            self._start_clone(clone_vmx)
            verified_connection = self._establish_clone_connection(
                authority,
                plan,
                preboot_disk_graph_digest=preboot_disk_graph_digest,
                preboot_snapshot_identity=preboot_snapshot_identity,
            )
            try:
                guest_root = self._stage_candidate(
                    authority,
                    candidate_root,
                    harness_plan.candidate_input_digest,
                )
                receipt = self._run_profile_guest(
                    authority=authority,
                    plan=plan,
                    harness_plan=harness_plan,
                    guest_root=guest_root,
                    initial_platform_state=initial_platform_state,
                )
            except CandidateHarnessError as error:
                profile_failure = error
                raise
            self._stop_clone(clone_vmx)
            if self._hashes() != before_hashes:
                raise CandidateHarnessError("CANDIDATE_ORIGINAL_VM_MUTATED")
            self._remove_clone(verified_connection)
            return receipt
        except BaseException as error:
            if authority.profile_root.exists() or authority.profile_root.is_symlink():
                if start_attempted and clone_vmx is not None:
                    try:
                        running = self._is_running(clone_vmx)
                    except CandidateHarnessError:
                        running = True
                    if running:
                        try:
                            self._contain_clone(clone_vmx)
                        except BaseException:
                            self._destroy_session_key(authority)
                            raise
                self._quarantine_clone(authority)
            if profile_failure is error:
                continuation = self.inspect_profile_continuation(
                    plan=plan,
                    harness_plan=harness_plan,
                )
                if continuation.continuation_safe:
                    raise CandidateProfileExecutionError(
                        profile_failure.code,
                        continuation,
                    ) from error
            raise
        finally:
            self._release_provider_lease(lease)

    def inspect_original_hashes(self) -> Mapping[str, str]:
        return self._hashes()

    def inspect_profile_continuation(
        self,
        *,
        plan: CandidateProfilePlan,
        harness_plan: CandidateHarnessPlan,
    ) -> ProfileContinuationReceipt:
        authority = self._profile_authority(plan, harness_plan)
        try:
            original_hashes = self._hashes()
            running_paths = self._running_vmx_paths()
            active_profile_root_count = int(
                authority.profile_root.exists() or authority.profile_root.is_symlink()
            )
            private_keys = (
                tuple(authority.session_root.rglob("id_ed25519"))
                if authority.session_root.exists()
                else ()
            )
            session_private_key_count = sum(
                1 for path in private_keys if path.exists() or path.is_symlink()
            )
            known_hosts_files = (
                tuple(authority.session_root.rglob("known_hosts"))
                if authority.session_root.exists()
                else ()
            )
            known_hosts_file_count = sum(
                1 for path in known_hosts_files if path.exists() or path.is_symlink()
            )
            quarantine_present = (
                authority.quarantine_root.exists()
                and any(authority.quarantine_root.iterdir())
            )
        except (OSError, ValueError) as error:
            raise CandidateHarnessError(
                "CANDIDATE_VM_CONTINUATION_UNVERIFIED"
            ) from error
        continuation_safe = (
            original_hashes == dict(harness_plan.original_vm_hashes)
            and not running_paths
            and active_profile_root_count == 0
            and session_private_key_count == 0
            and known_hosts_file_count == 0
        )
        return ProfileContinuationReceipt.issue(
            profile=plan.profile,
            session_id=harness_plan.session_id,
            original_vm_hashes=original_hashes,
            active_profile_root_count=active_profile_root_count,
            session_private_key_count=session_private_key_count,
            known_hosts_file_count=known_hosts_file_count,
            running_vm_count=len(running_paths),
            quarantine_present=quarantine_present,
            continuation_safe=continuation_safe,
        )

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

    def _ghcr_manifest_state(self, role: str, candidate_version: str) -> str:
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
            f"https://ghcr.io/v2/{repository}/manifests/{candidate_version}",
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

    def _public_r2_state(self, candidate_version: str) -> str:
        for key in candidate_r2_expected_keys(candidate_version):
            encoded_key = urllib.parse.quote(key, safe="")
            status, _ = self._public.get(
                f"{PUBLIC_MIRROR_ORIGIN}/{REPOSITORY}/releases/download/"
                f"{candidate_version}/{encoded_key}",
                {
                    "Range": "bytes=0-0",
                    "User-Agent": "AniMemo-Candidate-Acceptance/1",
                },
            )
            if self._absent_state(status) != "ABSENT":
                return "PRESENT_BY_PUBLIC_READBACK_NON_AUTHORITATIVE"
        return "ABSENT_BY_PUBLIC_READBACK_NON_AUTHORITATIVE"

    def inspect_candidate_external_state(
        self, candidate_version: str
    ) -> Mapping[str, str]:
        if _CANDIDATE_VERSION.fullmatch(candidate_version) is None:
            raise CandidateHarnessError("CANDIDATE_HARNESS_AUTHORITY_MISMATCH")
        ghcr_states = {
            self._ghcr_manifest_state("api", candidate_version),
            self._ghcr_manifest_state("web", candidate_version),
        }
        ghcr = "ABSENT" if ghcr_states == {"ABSENT"} else "PRESENT"
        return {
            "tag": self._github_state(f"git/ref/tags/{candidate_version}"),
            "github_release": self._github_state(
                f"releases/tags/{candidate_version}"
            ),
            "ghcr": ghcr,
            "public_r2": self._public_r2_state(candidate_version),
        }


def _validate_source_evidence(value: SourceVmEvidence) -> SourceVmEvidence:
    if (
        type(value) is not SourceVmEvidence
        or value.vm_identity != SOURCE_VM_IDENTITY
        or set(value.snapshot_identities) != set(PROFILES)
        or set(value.snapshot_disk_graph_identities) != set(PROFILES)
        or not _DIGEST.fullmatch(value.source_disk_graph_identity)
        or not set(SOURCE_VM_HASH_FILES).issubset(value.original_hashes)
        or len(value.original_hashes) > 64
        or any(
            not _DIGEST.fullmatch(value.snapshot_identities[profile])
            for profile in PROFILES
        )
        or any(
            not _DIGEST.fullmatch(value.snapshot_disk_graph_identities[profile])
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
        or _CANDIDATE_VERSION.fullmatch(candidate["candidate_version"]) is None
    ):
        raise CandidateHarnessError("CANDIDATE_HARNESS_AUTHORITY_MISMATCH")
    readiness = provider.inspect_readiness()
    expected_readiness = ProviderReadinessReceipt.issue(
        ssh_digest=EXPECTED_SSH_SHA256,
        scp_digest=EXPECTED_SCP_SHA256,
    )
    if readiness != expected_readiness:
        raise CandidateHarnessError("WINDOWS_OPENSSH_READINESS_FAILED")
    source = _validate_source_evidence(provider.inspect_source())
    source_vm_digest = sha256_bytes(
        canonical_json_bytes(dict(sorted(source.original_hashes.items())))
    )
    session_id = uuid4().hex
    profiles: list[CandidateProfilePlan] = []
    for profile in PROFILES:
        connection_nonce = secrets.token_hex(32)
        body = {
            "candidateInputDigest": loaded.verified["candidate_input_sha256"],
            "profile": profile,
            "snapshotIdentity": source.snapshot_identities[profile],
            "snapshotDiskGraphIdentity": source.snapshot_disk_graph_identities[
                profile
            ],
            "sourceDiskGraphIdentity": source.source_disk_graph_identity,
            "sourceVmDigest": source_vm_digest,
            "providerReadinessReceiptDigest": readiness.receipt_digest,
            "sessionId": session_id,
            "connectionNonce": connection_nonce,
        }
        clone_identity = sha256_bytes(canonical_json_bytes(body))
        profiles.append(
            CandidateProfilePlan(
                profile=profile,
                installer_profile=INSTALLER_PROFILES[profile],
                snapshot_name=SNAPSHOT_ALLOWLIST[profile],
                snapshot_identity=source.snapshot_identities[profile],
                snapshot_disk_graph_identity=(
                    source.snapshot_disk_graph_identities[profile]
                ),
                clone_identity=clone_identity,
                provider_readiness_receipt_digest=readiness.receipt_digest,
                session_id=session_id,
                connection_nonce=connection_nonce,
                ssh_host_key_alias=(
                    "animemo-"
                    + session_id[:12]
                    + "-"
                    + profile.lower().replace("_", "-")
                    + "-"
                    + clone_identity.removeprefix("sha256:")[:12]
                ),
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
        source_disk_graph_identity=source.source_disk_graph_identity,
        original_vm_hashes=dict(source.original_hashes),
        profiles=tuple(profiles),
        provider_readiness_receipt_digest=readiness.receipt_digest,
        session_id=session_id,
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


def _bind_host_profile_receipt(
    value: Mapping[str, Any],
    *,
    plan: CandidateHarnessPlan,
    observed_original_hashes: Mapping[str, str],
) -> dict[str, Any]:
    if (
        value.get("schema")
        != "animemo.prepublication-candidate-profile-receipt-draft/v1"
        or "original_vm_pre_hashes" in value
        or "original_vm_post_hashes" in value
    ):
        raise CandidateHarnessError("CANDIDATE_PROFILE_RECEIPT_DRAFT_INVALID")
    final = {
        **dict(value),
        "schema": "animemo.prepublication-candidate-profile-receipt/v1",
        "original_vm_pre_hashes": dict(plan.original_vm_hashes),
        "original_vm_post_hashes": dict(observed_original_hashes),
    }
    try:
        return validate_profile_receipt(final)
    except CandidateContractError as error:
        raise CandidateHarnessError(error.code) from error


def _read_expected_external_state(
    provider: CandidateVmProvider,
    candidate_version: str,
) -> dict[str, str]:
    try:
        state = dict(provider.inspect_candidate_external_state(candidate_version))
    except CandidateHarnessError:
        raise
    except (OSError, TypeError, ValueError) as error:
        raise CandidateHarnessError("CANDIDATE_EXTERNAL_STATE_UNVERIFIED") from error
    if state != EXPECTED_CANDIDATE_EXTERNAL_STATE:
        raise CandidateHarnessError("CANDIDATE_VERSION_NOT_EMPTY")
    return state


def _validate_continuation_receipt(
    value: object,
    *,
    profile: CandidateProfilePlan,
    plan: CandidateHarnessPlan,
) -> ProfileContinuationReceipt:
    if type(value) is not ProfileContinuationReceipt:
        raise CandidateHarnessError("CANDIDATE_VM_CONTINUATION_UNVERIFIED")
    receipt = value
    if (
        receipt.profile != profile.profile
        or receipt.session_id != plan.session_id
        or dict(receipt.original_vm_hashes) != dict(plan.original_vm_hashes)
        or type(receipt.active_profile_root_count) is not int
        or receipt.active_profile_root_count != 0
        or type(receipt.session_private_key_count) is not int
        or receipt.session_private_key_count != 0
        or type(receipt.known_hosts_file_count) is not int
        or receipt.known_hosts_file_count != 0
        or type(receipt.running_vm_count) is not int
        or receipt.running_vm_count != 0
        or receipt.continuation_safe is not True
        or receipt.receipt_digest
        != sha256_bytes(canonical_json_bytes(receipt.identity_body()))
    ):
        raise CandidateHarnessError("CANDIDATE_VM_CONTINUATION_UNVERIFIED")
    return receipt


def _profile_result(
    status: str,
    *,
    failure_code: str | None = None,
    receipt_digest: str | None = None,
) -> dict[str, str | None]:
    return {
        "status": status,
        "failure_code": failure_code,
        "receipt_digest": receipt_digest,
    }


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
        r2_prestate_receipt = verify_candidate_r2_origin_from_environment(
            target_rc=plan.candidate_version,
            source_sha=plan.source_sha,
            source_tree=plan.source_tree,
            auth_method=R2_AUTH_METHOD_ARGUMENT,
            observation_role="PRESTATE",
            environment=environment,
            client=r2_client,
        )
        r2_prestate_receipt = validate_r2_origin_receipt(
            r2_prestate_receipt,
            expected_source_sha=plan.source_sha,
            expected_source_tree=plan.source_tree,
            expected_target_rc=plan.candidate_version,
            expected_observation_role="PRESTATE",
        )
        loaded = load_verified_candidate(
            plan.verified_candidate_digest,
            _state_root=_state_root,
        )
    except CandidateContractError as error:
        raise CandidateHarnessError(error.code) from error
    candidate_prestate = {
        **_read_expected_external_state(provider, plan.candidate_version),
        "r2_origin": r2_prestate_receipt["result"],
    }
    receipts: dict[str, dict[str, Any]] = {}
    profile_results: dict[str, dict[str, str | None]] = {}
    shared_blocker_code: str | None = None
    for item in plan.profiles:
        result_key = PROFILE_RESULT_KEYS[item.profile]
        if shared_blocker_code is not None:
            profile_results[result_key] = _profile_result(
                "NOT_RUN_SHARED_BLOCKER",
                failure_code=shared_blocker_code,
            )
            continue
        expected_initial_state = _initial_platform_state(item.profile)
        try:
            value = dict(
                provider.execute_profile(
                    plan=item,
                    harness_plan=plan,
                    candidate_root=loaded.root,
                    initial_platform_state=expected_initial_state,
                )
            )
        except CandidateProfileExecutionError as error:
            try:
                _validate_continuation_receipt(
                    error.continuation_receipt,
                    profile=item,
                    plan=plan,
                )
            except CandidateHarnessError as continuation_error:
                shared_blocker_code = continuation_error.code
                profile_results[result_key] = _profile_result(
                    "ERROR",
                    failure_code=shared_blocker_code,
                )
            else:
                profile_results[result_key] = _profile_result(
                    "ERROR",
                    failure_code=error.code,
                )
            continue
        except CandidateHarnessError as error:
            shared_blocker_code = error.code
            profile_results[result_key] = _profile_result(
                "ERROR",
                failure_code=shared_blocker_code,
            )
            continue
        except Exception:
            shared_blocker_code = "CANDIDATE_PROFILE_UNCLASSIFIED_ERROR"
            profile_results[result_key] = _profile_result(
                "ERROR",
                failure_code=shared_blocker_code,
            )
            continue
        try:
            observed_original_hashes = dict(provider.inspect_original_hashes())
        except (CandidateHarnessError, OSError, TypeError, ValueError):
            shared_blocker_code = "CANDIDATE_ORIGINAL_VM_STATE_UNVERIFIED"
            profile_results[result_key] = _profile_result(
                "ERROR",
                failure_code=shared_blocker_code,
            )
            continue
        if observed_original_hashes != dict(plan.original_vm_hashes):
            shared_blocker_code = "CANDIDATE_ORIGINAL_VM_MUTATED"
            profile_results[result_key] = _profile_result(
                "ERROR",
                failure_code=shared_blocker_code,
            )
            continue
        try:
            continuation = provider.inspect_profile_continuation(
                plan=item,
                harness_plan=plan,
            )
            _validate_continuation_receipt(
                continuation,
                profile=item,
                plan=plan,
            )
        except (CandidateHarnessError, OSError, TypeError, ValueError):
            shared_blocker_code = "CANDIDATE_VM_CONTINUATION_UNVERIFIED"
            profile_results[result_key] = _profile_result(
                "ERROR",
                failure_code=shared_blocker_code,
            )
            continue
        try:
            receipt = _bind_host_profile_receipt(
                value,
                plan=plan,
                observed_original_hashes=observed_original_hashes,
            )
        except CandidateHarnessError as error:
            profile_results[result_key] = _profile_result(
                "ERROR",
                failure_code=error.code,
            )
            continue
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
        ):
            profile_results[result_key] = _profile_result(
                "ERROR",
                failure_code="CANDIDATE_PROFILE_RECEIPT_BINDING_MISMATCH",
            )
            continue
        receipts[item.profile] = receipt
        digest = _profile_digest(receipt)
        if receipt["result"] == "PASS":
            profile_results[result_key] = _profile_result(
                "PASS",
                receipt_digest=digest,
            )
        else:
            profile_results[result_key] = _profile_result(
                "FAIL",
                failure_code="CANDIDATE_PROFILE_REPORTED_FAILURE",
                receipt_digest=digest,
            )
    try:
        final_hashes = dict(provider.inspect_original_hashes())
    except (CandidateHarnessError, OSError, TypeError, ValueError) as error:
        raise CandidateHarnessError(
            "CANDIDATE_ORIGINAL_VM_STATE_UNVERIFIED"
        ) from error
    if final_hashes != dict(plan.original_vm_hashes):
        if not any(
            result["failure_code"] == "CANDIDATE_ORIGINAL_VM_MUTATED"
            for result in profile_results.values()
        ):
            receipts.pop("RUNTIME_BASE_OFFLINE", None)
            profile_results["runtime_base_offline"] = _profile_result(
                "ERROR",
                failure_code="CANDIDATE_ORIGINAL_VM_MUTATED",
            )
        for item in plan.profiles:
            key = PROFILE_RESULT_KEYS[item.profile]
            if key not in profile_results:
                profile_results[key] = _profile_result(
                    "NOT_RUN_SHARED_BLOCKER",
                    failure_code="CANDIDATE_ORIGINAL_VM_MUTATED",
                )
    try:
        r2_poststate_receipt = verify_candidate_r2_origin_from_environment(
            target_rc=plan.candidate_version,
            source_sha=plan.source_sha,
            source_tree=plan.source_tree,
            auth_method=R2_AUTH_METHOD_ARGUMENT,
            observation_role="POSTSTATE",
            environment=environment,
            client=r2_client,
        )
        r2_poststate_receipt = validate_r2_origin_receipt(
            r2_poststate_receipt,
            expected_source_sha=plan.source_sha,
            expected_source_tree=plan.source_tree,
            expected_target_rc=plan.candidate_version,
            expected_observation_role="POSTSTATE",
        )
    except CandidateContractError as error:
        raise CandidateHarnessError(error.code) from error
    if (
        r2_prestate_receipt["observation_id"]
        == r2_poststate_receipt["observation_id"]
    ):
        raise CandidateHarnessError("CANDIDATE_R2_OBSERVATION_REUSED")
    candidate_poststate = {
        **_read_expected_external_state(provider, plan.candidate_version),
        "r2_origin": r2_poststate_receipt["result"],
    }
    if candidate_poststate != candidate_prestate:
        raise CandidateHarnessError("CANDIDATE_VERSION_STATE_DRIFT")
    completed = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    all_profiles_pass = all(
        profile_results[PROFILE_RESULT_KEYS[profile]]["status"] == "PASS"
        for profile in PROFILES
    )
    aggregate = {
        "schema": "animemo.prepublication-candidate-acceptance-receipt/v3",
        "version": 3,
        "candidate_input_digest": plan.candidate_input_digest,
        "verified_candidate_digest": plan.verified_candidate_digest,
        "qualification_run_id": plan.qualification_run_id,
        "qualification_run_attempt": 1,
        "source_sha": plan.source_sha,
        "source_tree": plan.source_tree,
        "candidate_version": plan.candidate_version,
        "r2_origin_prestate_receipt_digest": r2_origin_receipt_digest(
            r2_prestate_receipt
        ),
        "r2_origin_poststate_receipt_digest": r2_origin_receipt_digest(
            r2_poststate_receipt
        ),
        "r2_origin_prestate_observation_id": r2_prestate_receipt[
            "observation_id"
        ],
        "r2_origin_poststate_observation_id": r2_poststate_receipt[
            "observation_id"
        ],
        "profile_results": profile_results,
        "all_profiles_pass": all_profiles_pass,
        "candidate_prestate": candidate_prestate,
        "candidate_poststate": candidate_poststate,
        "repository_mutation_count": 0,
        "publication_mutation_count": 0,
        "shared_host_connection_count": 0,
        "secret_sweep": 0,
        "placeholder_sweep": 0,
        "release_authority_granted": False,
        "publish_authorized": False,
        "completed_at": completed,
        "result": "PASS" if all_profiles_pass else "FAIL",
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
        "status": aggregate["result"],
        "aggregateReceipt": aggregate,
        "aggregateReceiptSha256": aggregate_receipt_digest(aggregate),
        "r2OriginPrestateReceipt": r2_prestate_receipt,
        "r2OriginPoststateReceipt": r2_poststate_receipt,
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
        return 0 if result["status"] == "PASS" else 2
    except (CandidateHarnessError, CandidateContractError) as error:
        print(json.dumps({"code": getattr(error, "code", str(error))}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
