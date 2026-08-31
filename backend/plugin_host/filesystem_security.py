"""Fail-closed filesystem boundary for plugin package material.

Plugin storage is mutable authority: package bytes become executable runtime
bytes and preview bytes are served to an authenticated browser.  Creation
therefore cannot inherit ambient ``umask`` or an ambient Windows DACL.

The native-Windows contract is deliberately explicit.  AniMemo production
runs the plugin host in Linux containers; a native-Windows process is accepted
only when the Win32 token and security-descriptor APIs can install a protected
DACL for the current user, SYSTEM, and Administrators and this Backend module
can independently read back its exact security descriptor.  An unavailable
API or an unverifiable DACL fails closed.
"""

from __future__ import annotations

import ctypes
import os
import re
import shutil
import stat
from ctypes import wintypes
from functools import lru_cache
from pathlib import Path

PRIVATE_DIRECTORY_MODE = 0o700
PRIVATE_FILE_MODE = 0o600
PLUGIN_ROOT_DIRECTORY_MODE = 0o755
RUNTIME_DIRECTORY_MODE = 0o755
RUNTIME_FILE_MODE = 0o644


_FILESYSTEM_DIAGNOSTIC_CODES = frozenset(
    {
        "unspecified",
        "path_containment",
        "path_metadata",
        "path_reparse",
        "path_owner",
        "token_sid",
        "dacl_read",
        "dacl_protection",
        "dacl_owner",
        "dacl_ace_type",
        "dacl_ace_flags",
        "dacl_ace_rights",
        "dacl_ace_principal",
        "dacl_duplicate",
        "dacl_principals",
        "dacl_api",
        "dacl_build",
        "dacl_extract",
        "dacl_apply",
        "directory_type",
        "directory_mode",
        "directory_create",
        "file_type",
        "file_mode",
        "file_create",
        "tree_enumerate",
        "tree_entry",
        "tree_remove",
    }
)


class PluginFilesystemSecurityError(ValueError):
    """A plugin path cannot be proven safe for mutation or execution."""

    def __init__(self, message: str, *, diagnostic_code: object = "unspecified"):
        super().__init__(message)
        self._diagnostic_code = (
            diagnostic_code
            if type(diagnostic_code) is str
            and diagnostic_code in _FILESYSTEM_DIAGNOSTIC_CODES
            else "unspecified"
        )

    @property
    def diagnostic_code(self) -> str:
        return filesystem_diagnostic_code(self)


def filesystem_diagnostic_code(error: PluginFilesystemSecurityError) -> str:
    """Return only a stable allowlisted value at the public logging boundary."""

    try:
        state = object.__getattribute__(error, "__dict__")
    except (AttributeError, TypeError):
        return "unspecified"
    value = state.get("_diagnostic_code", "unspecified")
    if type(value) is str and value in _FILESYSTEM_DIAGNOSTIC_CODES:
        return value
    return "unspecified"


def _contained(root: Path, path: Path) -> tuple[Path, Path]:
    boundary_text = os.path.abspath(os.fspath(root))
    target_text = os.path.abspath(os.fspath(path))
    normalized_boundary = os.path.normcase(boundary_text)
    normalized_target = os.path.normcase(target_text)
    normalized_prefix = (
        normalized_boundary
        if normalized_boundary.endswith(os.sep)
        else normalized_boundary + os.sep
    )
    boundary = Path(boundary_text)
    if normalized_target == normalized_boundary:
        return boundary, boundary
    if not normalized_target.startswith(normalized_prefix):
        raise PluginFilesystemSecurityError(
            "插件存储路径越过受控根目录。", diagnostic_code="path_containment"
        )
    try:
        common = os.path.commonpath((normalized_boundary, normalized_target))
    except ValueError as error:
        raise PluginFilesystemSecurityError(
            "插件存储路径越过受控根目录。", diagnostic_code="path_containment"
        ) from error
    if os.path.normcase(common) != normalized_boundary:
        raise PluginFilesystemSecurityError(
            "插件存储路径越过受控根目录。", diagnostic_code="path_containment"
        )
    if (
        len(normalized_boundary) != len(boundary_text)
        or os.path.normcase(target_text[: len(boundary_text)])
        != normalized_boundary
    ):
        raise PluginFilesystemSecurityError(
            "插件存储路径越过受控根目录。", diagnostic_code="path_containment"
        )
    suffix = target_text[len(boundary_text) :]
    if not boundary_text.endswith(os.sep) and not suffix.startswith(os.sep):
        raise PluginFilesystemSecurityError(
            "插件存储路径越过受控根目录。", diagnostic_code="path_containment"
        )
    case_aligned_target = boundary_text + suffix
    trusted_prefix = (
        boundary_text if boundary_text.endswith(os.sep) else boundary_text + os.sep
    )
    if not case_aligned_target.startswith(trusted_prefix):
        raise PluginFilesystemSecurityError(
            "插件存储路径越过受控根目录。", diagnostic_code="path_containment"
        )
    return boundary, Path(os.path.normpath(case_aligned_target))


def contained_path(root: Path, path: Path) -> Path:
    """Return an absolute lexical path only after a trusted-root guard."""

    _, target = _contained(root, path)
    return target


def resolve_contained_path(root: Path, path: Path) -> Path:
    """Resolve an existing path and reject physical escapes after validation."""

    boundary, candidate = _contained(root, path)
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise PluginFilesystemSecurityError(
            "插件存储路径元数据不可验证。", diagnostic_code="path_metadata"
        ) from error
    _, target = _contained(boundary, resolved)
    return target


def _is_reparse(metadata: os.stat_result) -> bool:
    attributes = int(getattr(metadata, "st_file_attributes", 0))
    reparse = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    return bool(attributes & reparse)


def _metadata(path: Path) -> os.stat_result:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise PluginFilesystemSecurityError(
            "插件存储路径元数据不可验证。", diagnostic_code="path_metadata"
        ) from error
    if path.is_symlink() or _is_reparse(metadata):
        raise PluginFilesystemSecurityError(
            "插件存储路径禁止符号链接或重解析点。", diagnostic_code="path_reparse"
        )
    return metadata


def _require_owner(metadata: os.stat_result) -> None:
    if os.name != "nt" and hasattr(os, "geteuid") and hasattr(os, "getegid") and (
        metadata.st_uid != os.geteuid() or metadata.st_gid != os.getegid()
    ):
        raise PluginFilesystemSecurityError(
            "插件存储路径所有者不可信。", diagnostic_code="path_owner"
        )


@lru_cache(maxsize=1)
def _windows_current_sid() -> str:
    """Read the process token SID without executing an ambient command."""

    class _SidAndAttributes(ctypes.Structure):
        _fields_ = (("sid", ctypes.c_void_p), ("attributes", wintypes.DWORD))

    class _TokenUser(ctypes.Structure):
        _fields_ = (("user", _SidAndAttributes),)

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32.OpenProcessToken.argtypes = (
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    )
    advapi32.OpenProcessToken.restype = wintypes.BOOL
    advapi32.GetTokenInformation.argtypes = (
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    )
    advapi32.GetTokenInformation.restype = wintypes.BOOL
    advapi32.ConvertSidToStringSidW.argtypes = (
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.LPWSTR),
    )
    advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL
    kernel32.GetCurrentProcess.argtypes = ()
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = (ctypes.c_void_p,)
    kernel32.LocalFree.restype = ctypes.c_void_p

    token = wintypes.HANDLE()
    sid_text = wintypes.LPWSTR()
    try:
        if not advapi32.OpenProcessToken(
            kernel32.GetCurrentProcess(),
            0x0008,
            ctypes.byref(token),
        ):
            raise PluginFilesystemSecurityError(
                "Windows 插件存储当前 SID 不可验证。", diagnostic_code="token_sid"
            )
        length = wintypes.DWORD()
        advapi32.GetTokenInformation(token, 1, None, 0, ctypes.byref(length))
        if not length.value:
            raise PluginFilesystemSecurityError(
                "Windows 插件存储当前 SID 不可验证。", diagnostic_code="token_sid"
            )
        buffer = ctypes.create_string_buffer(length.value)
        if not advapi32.GetTokenInformation(
            token,
            1,
            buffer,
            length,
            ctypes.byref(length),
        ):
            raise PluginFilesystemSecurityError(
                "Windows 插件存储当前 SID 不可验证。", diagnostic_code="token_sid"
            )
        token_user = ctypes.cast(buffer, ctypes.POINTER(_TokenUser)).contents
        if not token_user.user.sid or not advapi32.ConvertSidToStringSidW(
            token_user.user.sid,
            ctypes.byref(sid_text),
        ):
            raise PluginFilesystemSecurityError(
                "Windows 插件存储当前 SID 不可验证。", diagnostic_code="token_sid"
            )
        sid = sid_text.value or ""
        if not re.fullmatch(r"S-1-(?:[0-9]+-)*[0-9]+", sid):
            raise PluginFilesystemSecurityError(
                "Windows 插件存储当前 SID 不可验证。", diagnostic_code="token_sid"
            )
        return sid
    finally:
        if sid_text:
            kernel32.LocalFree(ctypes.cast(sid_text, ctypes.c_void_p))
        if token:
            kernel32.CloseHandle(token)


class _AclSizeInformation(ctypes.Structure):
    _fields_ = (
        ("ace_count", wintypes.DWORD),
        ("acl_bytes_in_use", wintypes.DWORD),
        ("acl_bytes_free", wintypes.DWORD),
    )


class _AceHeader(ctypes.Structure):
    _fields_ = (
        ("ace_type", ctypes.c_ubyte),
        ("ace_flags", ctypes.c_ubyte),
        ("ace_size", ctypes.c_ushort),
    )


class _AccessAllowedAce(ctypes.Structure):
    _fields_ = (
        ("header", _AceHeader),
        ("mask", wintypes.DWORD),
        ("sid_start", wintypes.DWORD),
    )


def _validate_windows_dacl(path: Path, *, directory: bool) -> None:
    sid = _windows_current_sid()
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32.GetNamedSecurityInfoW.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
    )
    advapi32.GetNamedSecurityInfoW.restype = wintypes.DWORD
    advapi32.GetSecurityDescriptorControl.argtypes = (
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_ushort),
        ctypes.POINTER(wintypes.DWORD),
    )
    advapi32.GetSecurityDescriptorControl.restype = wintypes.BOOL
    advapi32.ConvertStringSidToSidW.argtypes = (
        wintypes.LPCWSTR,
        ctypes.POINTER(ctypes.c_void_p),
    )
    advapi32.ConvertStringSidToSidW.restype = wintypes.BOOL
    advapi32.GetAclInformation.argtypes = (
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.c_int,
    )
    advapi32.GetAclInformation.restype = wintypes.BOOL
    advapi32.IsValidAcl.argtypes = (ctypes.c_void_p,)
    advapi32.IsValidAcl.restype = wintypes.BOOL
    advapi32.GetAce.argtypes = (
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
    )
    advapi32.GetAce.restype = wintypes.BOOL
    advapi32.IsValidSid.argtypes = (ctypes.c_void_p,)
    advapi32.IsValidSid.restype = wintypes.BOOL
    advapi32.GetLengthSid.argtypes = (ctypes.c_void_p,)
    advapi32.GetLengthSid.restype = wintypes.DWORD
    advapi32.EqualSid.argtypes = (ctypes.c_void_p, ctypes.c_void_p)
    advapi32.EqualSid.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = (ctypes.c_void_p,)
    kernel32.LocalFree.restype = ctypes.c_void_p
    descriptor = ctypes.c_void_p()
    owner = ctypes.c_void_p()
    dacl = ctypes.c_void_p()
    principal_pointers: list[tuple[str, ctypes.c_void_p]] = []
    owner_information = 0x00000001
    dacl_information = 0x00000004
    security_information = owner_information | dacl_information
    try:
        result = advapi32.GetNamedSecurityInfoW(
            str(path),
            1,
            security_information,
            ctypes.byref(owner),
            None,
            ctypes.byref(dacl),
            None,
            ctypes.byref(descriptor),
        )
        if result != 0 or not descriptor.value or not owner.value or not dacl.value:
            raise PluginFilesystemSecurityError(
                "Windows 插件存储 DACL 无法验证。", diagnostic_code="dacl_read"
            )
        control = ctypes.c_ushort()
        revision = wintypes.DWORD()
        if not advapi32.GetSecurityDescriptorControl(
            descriptor, ctypes.byref(control), ctypes.byref(revision)
        ) or not control.value & 0x1000:
            raise PluginFilesystemSecurityError(
                "Windows 插件存储 DACL 未受保护。", diagnostic_code="dacl_protection"
            )
        principal_texts = tuple(dict.fromkeys((sid, "S-1-5-18", "S-1-5-32-544")))
        for principal_text in principal_texts:
            principal = ctypes.c_void_p()
            if not advapi32.ConvertStringSidToSidW(
                principal_text, ctypes.byref(principal)
            ) or not principal.value:
                raise PluginFilesystemSecurityError(
                    "Windows 插件存储 DACL 无法验证。", diagnostic_code="dacl_api"
                )
            principal_pointers.append((principal_text, principal))
        if not any(
            advapi32.EqualSid(owner, principal)
            for _, principal in principal_pointers
        ):
            raise PluginFilesystemSecurityError(
                "Windows 插件存储所有者不可信。", diagnostic_code="dacl_owner"
            )
        acl_information = _AclSizeInformation()
        if not advapi32.IsValidAcl(dacl) or not advapi32.GetAclInformation(
            dacl, ctypes.byref(acl_information), ctypes.sizeof(acl_information), 2
        ):
            raise PluginFilesystemSecurityError(
                "Windows 插件存储 DACL 无法验证。", diagnostic_code="dacl_api"
            )
        if acl_information.ace_count != len(principal_pointers):
            raise PluginFilesystemSecurityError(
                "Windows 插件存储 DACL 主体不完整。",
                diagnostic_code="dacl_principals",
            )
        expected_flags = 0x03 if directory else 0x00
        observed: set[str] = set()
        acl_start = dacl.value
        acl_end = acl_start + acl_information.acl_bytes_in_use
        for index in range(acl_information.ace_count):
            ace_pointer = ctypes.c_void_p()
            if not advapi32.GetAce(dacl, index, ctypes.byref(ace_pointer)):
                raise PluginFilesystemSecurityError(
                    "Windows 插件存储 DACL 无法验证。", diagnostic_code="dacl_api"
                )
            ace_start = ace_pointer.value
            if (
                not ace_start
                or ace_start < acl_start
                or ace_start + ctypes.sizeof(_AceHeader) > acl_end
            ):
                raise PluginFilesystemSecurityError(
                    "Windows 插件存储 DACL 包含非受信 ACE。",
                    diagnostic_code="dacl_ace_type",
                )
            header = ctypes.cast(ace_pointer, ctypes.POINTER(_AceHeader)).contents
            sid_offset = _AccessAllowedAce.sid_start.offset
            if (
                header.ace_type != 0
                or header.ace_size < sid_offset + 8
                or ace_start + header.ace_size > acl_end
            ):
                raise PluginFilesystemSecurityError(
                    "Windows 插件存储 DACL 包含非受信 ACE。",
                    diagnostic_code="dacl_ace_type",
                )
            if header.ace_flags != expected_flags:
                raise PluginFilesystemSecurityError(
                    "Windows 插件存储 DACL 包含非受信 ACE。",
                    diagnostic_code="dacl_ace_flags",
                )
            ace = ctypes.cast(
                ace_pointer, ctypes.POINTER(_AccessAllowedAce)
            ).contents
            if ace.mask != 0x001F01FF:
                raise PluginFilesystemSecurityError(
                    "Windows 插件存储 DACL 包含非受信 ACE。",
                    diagnostic_code="dacl_ace_rights",
                )
            ace_sid = ctypes.c_void_p(ace_pointer.value + sid_offset)
            if not advapi32.IsValidSid(ace_sid):
                raise PluginFilesystemSecurityError(
                    "Windows 插件存储 DACL 包含非受信 ACE。",
                    diagnostic_code="dacl_ace_principal",
                )
            sid_length = advapi32.GetLengthSid(ace_sid)
            if not sid_length or header.ace_size != sid_offset + sid_length:
                raise PluginFilesystemSecurityError(
                    "Windows 插件存储 DACL 包含非受信 ACE。",
                    diagnostic_code="dacl_ace_type",
                )
            matches = [
                principal_text
                for principal_text, principal in principal_pointers
                if advapi32.EqualSid(ace_sid, principal)
            ]
            if len(matches) != 1:
                raise PluginFilesystemSecurityError(
                    "Windows 插件存储 DACL 包含非受信 ACE。",
                    diagnostic_code="dacl_ace_principal",
                )
            if matches[0] in observed:
                raise PluginFilesystemSecurityError(
                    "Windows 插件存储 DACL 包含重复 ACE。",
                    diagnostic_code="dacl_duplicate",
                )
            observed.add(matches[0])
        if observed != set(principal_texts):
            raise PluginFilesystemSecurityError(
                "Windows 插件存储 DACL 主体不完整。",
                diagnostic_code="dacl_principals",
            )
    except PluginFilesystemSecurityError:
        raise
    except (
        AttributeError,
        OSError,
        OverflowError,
        TypeError,
        ValueError,
        ctypes.ArgumentError,
    ) as error:
        raise PluginFilesystemSecurityError(
            "Windows 插件存储 DACL 无法验证。", diagnostic_code="dacl_api"
        ) from error
    finally:
        for _, principal in principal_pointers:
            if principal.value:
                kernel32.LocalFree(principal)
        if descriptor.value:
            kernel32.LocalFree(descriptor)


def _harden_windows_dacl(path: Path, *, directory: bool) -> None:
    sid = _windows_current_sid()
    ace_flags = "OICI" if directory else ""
    principal_texts = tuple(dict.fromkeys((sid, "S-1-5-18", "S-1-5-32-544")))
    descriptor_text = "D:P" + "".join(
        f"(A;{ace_flags};FA;;;{principal})" for principal in principal_texts
    )
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.DWORD),
    )
    advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
    advapi32.GetSecurityDescriptorDacl.argtypes = (
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.BOOL),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.BOOL),
    )
    advapi32.GetSecurityDescriptorDacl.restype = wintypes.BOOL
    advapi32.SetNamedSecurityInfoW.argtypes = (
        wintypes.LPWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
    )
    advapi32.SetNamedSecurityInfoW.restype = wintypes.DWORD
    kernel32.LocalFree.argtypes = (ctypes.c_void_p,)
    kernel32.LocalFree.restype = ctypes.c_void_p
    descriptor = ctypes.c_void_p()
    try:
        length = wintypes.DWORD()
        if not advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            descriptor_text,
            1,
            ctypes.byref(descriptor),
            ctypes.byref(length),
        ) or not descriptor.value:
            raise PluginFilesystemSecurityError(
                "Windows 插件存储 DACL 无法收紧。", diagnostic_code="dacl_build"
            )
        present = wintypes.BOOL()
        defaulted = wintypes.BOOL()
        dacl = ctypes.c_void_p()
        if not advapi32.GetSecurityDescriptorDacl(
            descriptor,
            ctypes.byref(present),
            ctypes.byref(dacl),
            ctypes.byref(defaulted),
        ) or not present.value or not dacl.value:
            raise PluginFilesystemSecurityError(
                "Windows 插件存储 DACL 无法收紧。", diagnostic_code="dacl_extract"
            )
        target = ctypes.create_unicode_buffer(str(path))
        result = advapi32.SetNamedSecurityInfoW(
            target,
            1,
            0x00000004 | 0x80000000,
            None,
            None,
            dacl,
            None,
        )
        if result != 0:
            raise PluginFilesystemSecurityError(
                "Windows 插件存储 DACL 无法收紧。", diagnostic_code="dacl_apply"
            )
    finally:
        if descriptor.value:
            kernel32.LocalFree(descriptor)
    _validate_windows_dacl(path, directory=directory)


def _secure_directory(path: Path, mode: int) -> None:
    metadata = _metadata(path)
    if not stat.S_ISDIR(metadata.st_mode):
        raise PluginFilesystemSecurityError(
            "插件存储目录类型无效。", diagnostic_code="directory_type"
        )
    _require_owner(metadata)
    if os.name == "nt":
        try:
            _validate_windows_dacl(path, directory=True)
        except PluginFilesystemSecurityError:
            _harden_windows_dacl(path, directory=True)
    else:
        try:
            os.chmod(path, mode, follow_symlinks=False)
        except OSError as error:
            raise PluginFilesystemSecurityError(
                "插件存储目录权限无法收紧。", diagnostic_code="directory_mode"
            ) from error
        checked = _metadata(path)
        if stat.S_IMODE(checked.st_mode) != mode:
            raise PluginFilesystemSecurityError(
                "插件存储目录权限不符合合同。", diagnostic_code="directory_mode"
            )


def secure_file(
    root: Path,
    path: Path,
    *,
    mode: int = PRIVATE_FILE_MODE,
    directory_mode: int = PRIVATE_DIRECTORY_MODE,
) -> Path:
    """Validate a single-link regular file and install its exact permissions."""

    boundary, target = _contained(root, path)
    ensure_directory(boundary, target.parent, mode=directory_mode)
    metadata = _metadata(target)
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise PluginFilesystemSecurityError(
            "插件存储文件必须是单链接普通文件。", diagnostic_code="file_type"
        )
    _require_owner(metadata)
    if os.name == "nt":
        try:
            _validate_windows_dacl(target, directory=False)
        except PluginFilesystemSecurityError:
            _harden_windows_dacl(target, directory=False)
    else:
        try:
            os.chmod(target, mode, follow_symlinks=False)
        except OSError as error:
            raise PluginFilesystemSecurityError(
                "插件存储文件权限无法收紧。", diagnostic_code="file_mode"
            ) from error
        checked = _metadata(target)
        if stat.S_IMODE(checked.st_mode) != mode:
            raise PluginFilesystemSecurityError(
                "插件存储文件权限不符合合同。", diagnostic_code="file_mode"
            )
    return target


def validate_directory(root: Path, path: Path) -> Path:
    """Require a real, owned directory without changing its mode."""

    _, target = _contained(root, path)
    metadata = _metadata(target)
    if not stat.S_ISDIR(metadata.st_mode):
        raise PluginFilesystemSecurityError(
            "插件存储目录类型无效。", diagnostic_code="directory_type"
        )
    _require_owner(metadata)
    if os.name == "nt":
        _validate_windows_dacl(target, directory=True)
    elif stat.S_IMODE(metadata.st_mode) & 0o022:
        raise PluginFilesystemSecurityError(
            "插件存储目录禁止组或其他用户写入。", diagnostic_code="directory_mode"
        )
    return target


def validate_directory_chain(root: Path, path: Path) -> Path:
    """Reject links/reparse points in every component below a trusted root."""

    boundary, target = _contained(root, path)
    validate_directory(boundary, boundary)
    current = boundary
    for part in target.relative_to(boundary).parts:
        current = current / part
        validate_directory(boundary, current)
    return target


def ensure_directory(
    root: Path,
    path: Path,
    *,
    mode: int = PRIVATE_DIRECTORY_MODE,
) -> Path:
    """Create/harden every sensitive component below ``root`` explicitly."""

    boundary, target = _contained(root, path)
    boundary_created = False
    if not boundary.exists():
        try:
            boundary.mkdir(parents=True, mode=mode, exist_ok=False)
            boundary_created = True
        except FileExistsError:
            pass
        except OSError as error:
            raise PluginFilesystemSecurityError(
                "插件存储根目录无法创建。", diagnostic_code="directory_create"
            ) from error
    if target == boundary:
        _secure_directory(boundary, mode)
    elif boundary_created:
        _secure_directory(boundary, PRIVATE_DIRECTORY_MODE)
    else:
        validate_directory(boundary, boundary)
    current = boundary
    for part in target.relative_to(boundary).parts:
        current = current / part
        if not current.exists():
            try:
                os.mkdir(current, mode)
            except FileExistsError:
                pass
            except OSError as error:
                raise PluginFilesystemSecurityError(
                    "插件存储目录无法创建。", diagnostic_code="directory_create"
                ) from error
        _secure_directory(current, mode)
    return target


def ensure_plugin_layout(storage: object) -> None:
    """Install explicit modes/DACLs for every top-level plugin store."""

    root = Path(storage.root)
    packages = Path(storage.packages)
    runtime = Path(storage.runtime)
    previews = Path(storage.previews)
    staging = Path(storage.staging)
    ensure_directory(root, root, mode=PLUGIN_ROOT_DIRECTORY_MODE)
    ensure_directory(root, packages / "sha256", mode=PRIVATE_DIRECTORY_MODE)
    ensure_directory(root, previews, mode=PRIVATE_DIRECTORY_MODE)
    ensure_directory(root, staging, mode=PRIVATE_DIRECTORY_MODE)
    ensure_directory(root, root / ".locks", mode=PRIVATE_DIRECTORY_MODE)
    ensure_directory(root, runtime, mode=RUNTIME_DIRECTORY_MODE)


def write_descriptor_all(descriptor: int, payload: bytes) -> None:
    """Write the complete payload or fail instead of accepting a short write."""

    view = memoryview(bytes(payload))
    while view:
        written = os.write(descriptor, view)
        if written < 1:
            raise OSError("short plugin storage write")
        view = view[written:]


def created_file_identity(descriptor: int) -> tuple[int, int]:
    """Capture the identity of a newly created single-link regular file."""

    metadata = os.fstat(descriptor)
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise OSError("new plugin storage file has an invalid identity")
    return int(metadata.st_dev), int(metadata.st_ino)


def _path_has_identity(
    root: Path,
    path: Path,
    identity: tuple[int, int],
) -> bool:
    _, target = _contained(root, path)
    try:
        metadata = target.lstat()
    except OSError:
        return False
    return (
        stat.S_ISREG(metadata.st_mode)
        and metadata.st_nlink == 1
        and (int(metadata.st_dev), int(metadata.st_ino)) == identity
    )


def require_created_file_identity(
    root: Path,
    path: Path,
    identity: tuple[int, int],
) -> None:
    """Fail closed when a just-created path no longer names its opened file."""

    if not _path_has_identity(root, path, identity):
        raise PluginFilesystemSecurityError(
            "插件存储文件身份在创建期间发生变化。", diagnostic_code="file_type"
        )


def close_and_unlink_created_file(
    root: Path,
    path: Path,
    descriptor: int,
    identity: tuple[int, int] | None,
    *,
    expected_payload: bytes | None = None,
) -> None:
    """Close a created file and unlink only the unchanged object we opened."""

    descriptor_matches = False
    try:
        if identity is not None:
            metadata = os.fstat(descriptor)
            descriptor_matches = (
                stat.S_ISREG(metadata.st_mode)
                and metadata.st_nlink == 1
                and (int(metadata.st_dev), int(metadata.st_ino)) == identity
            )
    except OSError:
        descriptor_matches = False
    try:
        os.close(descriptor)
    except OSError:
        return

    if not descriptor_matches or identity is None:
        return
    unlink_created_file(
        root,
        path,
        identity,
        expected_payload=expected_payload,
    )


def unlink_created_file(
    root: Path,
    path: Path,
    identity: tuple[int, int],
    *,
    expected_payload: bytes | None = None,
) -> None:
    """Unlink only an unchanged, closed file with the captured identity."""

    boundary, target = _contained(root, path)
    if not _path_has_identity(boundary, target, identity):
        return
    if expected_payload is not None:
        try:
            if target.read_bytes() != expected_payload:
                return
        except OSError:
            return
        if not _path_has_identity(boundary, target, identity):
            return
    try:
        target.unlink(missing_ok=True)
    except OSError:
        return


def write_secure_bytes(
    root: Path,
    path: Path,
    payload: bytes,
    *,
    directory_mode: int = PRIVATE_DIRECTORY_MODE,
    file_mode: int = PRIVATE_FILE_MODE,
) -> Path:
    """Create a new regular file without an ambient-permission interval."""

    boundary, target = _contained(root, path)
    ensure_directory(boundary, target.parent, mode=directory_mode)
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = -1
    identity = None
    try:
        descriptor = os.open(target, flags, file_mode)
        identity = created_file_identity(descriptor)
        if hasattr(os, "fchmod") and os.name != "nt":
            os.fchmod(descriptor, file_mode)
        write_descriptor_all(descriptor, payload)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
    except FileExistsError as error:
        raise PluginFilesystemSecurityError(
            "插件存储文件已存在。", diagnostic_code="file_create"
        ) from error
    except OSError as error:
        if descriptor >= 0:
            close_and_unlink_created_file(
                boundary,
                target,
                descriptor,
                identity,
            )
            descriptor = -1
        raise PluginFilesystemSecurityError(
            "插件存储文件无法安全写入。", diagnostic_code="file_create"
        ) from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    try:
        secured = secure_file(
            boundary,
            target,
            mode=file_mode,
            directory_mode=directory_mode,
        )
        require_created_file_identity(boundary, target, identity)
        return secured
    except BaseException:
        if identity is not None:
            unlink_created_file(
                boundary,
                target,
                identity,
                expected_payload=bytes(payload),
            )
        raise


def secure_tree(
    root: Path,
    path: Path,
    *,
    directory_mode: int,
    file_mode: int,
) -> Path:
    """Reject links/special files and harden a complete extracted tree."""

    boundary, target = _contained(root, path)
    if target != boundary:
        validate_directory_chain(boundary, target.parent)
    _secure_directory(target, directory_mode)
    directories = [target]
    while directories:
        directory = directories.pop()
        try:
            children = list(directory.iterdir())
        except OSError as error:
            raise PluginFilesystemSecurityError(
                "插件存储目录无法枚举。", diagnostic_code="tree_enumerate"
            ) from error
        for child in children:
            metadata = _metadata(child)
            if stat.S_ISDIR(metadata.st_mode):
                _secure_directory(child, directory_mode)
                directories.append(child)
            elif stat.S_ISREG(metadata.st_mode) and metadata.st_nlink == 1:
                secure_file(
                    target,
                    child,
                    mode=file_mode,
                    directory_mode=directory_mode,
                )
            else:
                raise PluginFilesystemSecurityError(
                    "插件存储树包含链接或特殊文件。", diagnostic_code="tree_entry"
                )
    return target


def validate_secure_tree(root: Path, path: Path) -> Path:
    """Validate an existing tree before execution, move, or recursive delete."""

    boundary, target = _contained(root, path)
    validate_directory_chain(boundary, target)
    directories = [target]
    while directories:
        directory = directories.pop()
        try:
            children = list(directory.iterdir())
        except OSError as error:
            raise PluginFilesystemSecurityError(
                "插件存储目录无法枚举。", diagnostic_code="tree_enumerate"
            ) from error
        for child in children:
            metadata = _metadata(child)
            _require_owner(metadata)
            if stat.S_ISDIR(metadata.st_mode):
                validate_directory(target, child)
                directories.append(child)
                continue
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise PluginFilesystemSecurityError(
                    "插件存储树包含链接或特殊文件。", diagnostic_code="tree_entry"
                )
            if os.name == "nt":
                _validate_windows_dacl(child, directory=False)
            elif stat.S_IMODE(metadata.st_mode) & 0o022:
                raise PluginFilesystemSecurityError(
                    "插件存储文件禁止组或其他用户写入。", diagnostic_code="file_mode"
                )
    return target


def remove_secure_tree(root: Path, path: Path) -> None:
    """Remove only a validated real tree contained by ``root``."""

    boundary, target = _contained(root, path)
    if not target.exists() and not target.is_symlink():
        return
    validate_directory_chain(boundary, target.parent)
    validate_secure_tree(boundary, target)
    try:
        shutil.rmtree(target)
    except OSError as error:
        raise PluginFilesystemSecurityError(
            "插件存储树无法安全删除。", diagnostic_code="tree_remove"
        ) from error
