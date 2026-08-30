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
RUNTIME_DIRECTORY_MODE = 0o755
RUNTIME_FILE_MODE = 0o644


class PluginFilesystemSecurityError(ValueError):
    """A plugin path cannot be proven safe for mutation or execution."""


def _absolute(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _contained(root: Path, path: Path) -> tuple[Path, Path]:
    boundary = _absolute(Path(root))
    target = _absolute(Path(path))
    try:
        target.relative_to(boundary)
    except ValueError as error:
        raise PluginFilesystemSecurityError("插件存储路径越过受控根目录。") from error
    return boundary, target


def _is_reparse(metadata: os.stat_result) -> bool:
    attributes = int(getattr(metadata, "st_file_attributes", 0))
    reparse = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    return bool(attributes & reparse)


def _metadata(path: Path) -> os.stat_result:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise PluginFilesystemSecurityError("插件存储路径元数据不可验证。") from error
    if path.is_symlink() or _is_reparse(metadata):
        raise PluginFilesystemSecurityError("插件存储路径禁止符号链接或重解析点。")
    return metadata


def _require_owner(metadata: os.stat_result) -> None:
    if os.name != "nt" and hasattr(os, "geteuid") and metadata.st_uid != os.geteuid():
        raise PluginFilesystemSecurityError("插件存储路径所有者不可信。")


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
            raise PluginFilesystemSecurityError("Windows 插件存储当前 SID 不可验证。")
        length = wintypes.DWORD()
        advapi32.GetTokenInformation(token, 1, None, 0, ctypes.byref(length))
        if not length.value:
            raise PluginFilesystemSecurityError("Windows 插件存储当前 SID 不可验证。")
        buffer = ctypes.create_string_buffer(length.value)
        if not advapi32.GetTokenInformation(
            token,
            1,
            buffer,
            length,
            ctypes.byref(length),
        ):
            raise PluginFilesystemSecurityError("Windows 插件存储当前 SID 不可验证。")
        token_user = ctypes.cast(buffer, ctypes.POINTER(_TokenUser)).contents
        if not token_user.user.sid or not advapi32.ConvertSidToStringSidW(
            token_user.user.sid,
            ctypes.byref(sid_text),
        ):
            raise PluginFilesystemSecurityError("Windows 插件存储当前 SID 不可验证。")
        sid = sid_text.value or ""
        if not re.fullmatch(r"S-1-(?:[0-9]+-)*[0-9]+", sid):
            raise PluginFilesystemSecurityError("Windows 插件存储当前 SID 不可验证。")
        return sid
    finally:
        if sid_text:
            kernel32.LocalFree(ctypes.cast(sid_text, ctypes.c_void_p))
        if token:
            kernel32.CloseHandle(token)


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
    advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = (
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.DWORD),
    )
    advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = (ctypes.c_void_p,)
    kernel32.LocalFree.restype = ctypes.c_void_p
    descriptor = ctypes.c_void_p()
    owner = ctypes.c_void_p()
    dacl = ctypes.c_void_p()
    sddl_pointer = ctypes.c_void_p()
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
            raise PluginFilesystemSecurityError("Windows 插件存储 DACL 无法验证。")
        control = ctypes.c_ushort()
        revision = wintypes.DWORD()
        if not advapi32.GetSecurityDescriptorControl(
            descriptor, ctypes.byref(control), ctypes.byref(revision)
        ) or not control.value & 0x1000:
            raise PluginFilesystemSecurityError("Windows 插件存储 DACL 未受保护。")
        length = wintypes.DWORD()
        if not advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW(
            descriptor,
            1,
            security_information,
            ctypes.byref(sddl_pointer),
            ctypes.byref(length),
        ) or not sddl_pointer.value:
            raise PluginFilesystemSecurityError("Windows 插件存储 DACL 无法验证。")
        sddl = ctypes.wstring_at(sddl_pointer.value, length.value).rstrip("\x00")
        owner_match = re.search(r"O:([^:]+?)(?=[GDS]:)", sddl)
        owner_value = owner_match.group(1) if owner_match else ""
        aliases = {"S-1-5-18": "SY", "S-1-5-32-544": "BA"}
        trusted = {sid, "SY", "BA", *aliases}
        if owner_value not in trusted:
            raise PluginFilesystemSecurityError("Windows 插件存储所有者不可信。")
        dacl_text = sddl.split("D:", 1)[1].split("S:", 1)[0] if "D:" in sddl else ""
        entries = re.findall(r"\(([^()]*)\)", dacl_text)
        expected_flags = "OICI" if directory else ""
        observed: set[str] = set()
        for entry in entries:
            fields = entry.split(";")
            if (
                len(fields) != 6
                or fields[0] != "A"
                or fields[1] != expected_flags
                or fields[2] != "FA"
                or fields[3]
                or fields[4]
                or fields[5] not in trusted
            ):
                raise PluginFilesystemSecurityError("Windows 插件存储 DACL 包含非受信 ACE。")
            canonical = aliases.get(fields[5], fields[5])
            if canonical in observed:
                raise PluginFilesystemSecurityError("Windows 插件存储 DACL 包含重复 ACE。")
            observed.add(canonical)
        if observed != {sid, "SY", "BA"}:
            raise PluginFilesystemSecurityError("Windows 插件存储 DACL 主体不完整。")
    except PluginFilesystemSecurityError:
        raise
    except (AttributeError, OSError, ValueError) as error:
        raise PluginFilesystemSecurityError("Windows 插件存储 DACL 无法验证。") from error
    finally:
        if sddl_pointer.value:
            kernel32.LocalFree(sddl_pointer)
        if descriptor.value:
            kernel32.LocalFree(descriptor)


def _harden_windows_dacl(path: Path, *, directory: bool) -> None:
    sid = _windows_current_sid()
    ace_flags = "OICI" if directory else ""
    descriptor_text = (
        "D:P"
        f"(A;{ace_flags};FA;;;{sid})"
        f"(A;{ace_flags};FA;;;SY)"
        f"(A;{ace_flags};FA;;;BA)"
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
            raise PluginFilesystemSecurityError("Windows 插件存储 DACL 无法收紧。")
        present = wintypes.BOOL()
        defaulted = wintypes.BOOL()
        dacl = ctypes.c_void_p()
        if not advapi32.GetSecurityDescriptorDacl(
            descriptor,
            ctypes.byref(present),
            ctypes.byref(dacl),
            ctypes.byref(defaulted),
        ) or not present.value or not dacl.value:
            raise PluginFilesystemSecurityError("Windows 插件存储 DACL 无法收紧。")
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
            raise PluginFilesystemSecurityError("Windows 插件存储 DACL 无法收紧。")
    finally:
        if descriptor.value:
            kernel32.LocalFree(descriptor)
    _validate_windows_dacl(path, directory=directory)


def _secure_directory(path: Path, mode: int) -> None:
    metadata = _metadata(path)
    if not stat.S_ISDIR(metadata.st_mode):
        raise PluginFilesystemSecurityError("插件存储目录类型无效。")
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
            raise PluginFilesystemSecurityError("插件存储目录权限无法收紧。") from error
        checked = _metadata(path)
        if stat.S_IMODE(checked.st_mode) != mode:
            raise PluginFilesystemSecurityError("插件存储目录权限不符合合同。")


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
        raise PluginFilesystemSecurityError("插件存储文件必须是单链接普通文件。")
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
            raise PluginFilesystemSecurityError("插件存储文件权限无法收紧。") from error
        checked = _metadata(target)
        if stat.S_IMODE(checked.st_mode) != mode:
            raise PluginFilesystemSecurityError("插件存储文件权限不符合合同。")
    return target


def validate_directory(path: Path) -> Path:
    """Require a real, owned directory without changing its mode."""

    target = _absolute(path)
    metadata = _metadata(target)
    if not stat.S_ISDIR(metadata.st_mode):
        raise PluginFilesystemSecurityError("插件存储目录类型无效。")
    _require_owner(metadata)
    if os.name == "nt":
        _validate_windows_dacl(target, directory=True)
    elif stat.S_IMODE(metadata.st_mode) & 0o022:
        raise PluginFilesystemSecurityError("插件存储目录禁止组或其他用户写入。")
    return target


def validate_directory_chain(root: Path, path: Path) -> Path:
    """Reject links/reparse points in every component below a trusted root."""

    boundary, target = _contained(root, path)
    validate_directory(boundary)
    current = boundary
    for part in target.relative_to(boundary).parts:
        current = current / part
        validate_directory(current)
    return target


def ensure_directory(
    root: Path,
    path: Path,
    *,
    mode: int = PRIVATE_DIRECTORY_MODE,
) -> Path:
    """Create/harden every sensitive component below ``root`` explicitly."""

    boundary, target = _contained(root, path)
    if not boundary.exists():
        try:
            boundary.mkdir(parents=True, mode=mode, exist_ok=False)
        except FileExistsError:
            pass
        except OSError as error:
            raise PluginFilesystemSecurityError("插件存储根目录无法创建。") from error
    _secure_directory(boundary, mode if target == boundary else PRIVATE_DIRECTORY_MODE)
    current = boundary
    for part in target.relative_to(boundary).parts:
        current = current / part
        if not current.exists():
            try:
                os.mkdir(current, mode)
            except FileExistsError:
                pass
            except OSError as error:
                raise PluginFilesystemSecurityError("插件存储目录无法创建。") from error
        _secure_directory(current, mode)
    return target


def ensure_plugin_layout(storage: object) -> None:
    """Install explicit modes/DACLs for every top-level plugin store."""

    root = Path(storage.root)
    packages = Path(storage.packages)
    runtime = Path(storage.runtime)
    previews = Path(storage.previews)
    staging = Path(storage.staging)
    ensure_directory(root, root, mode=PRIVATE_DIRECTORY_MODE)
    ensure_directory(root, packages / "sha256", mode=PRIVATE_DIRECTORY_MODE)
    ensure_directory(root, previews, mode=PRIVATE_DIRECTORY_MODE)
    ensure_directory(root, staging, mode=PRIVATE_DIRECTORY_MODE)
    ensure_directory(root, root / ".locks", mode=PRIVATE_DIRECTORY_MODE)
    ensure_directory(root, runtime, mode=RUNTIME_DIRECTORY_MODE)


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
    try:
        descriptor = os.open(target, flags, file_mode)
        if hasattr(os, "fchmod") and os.name != "nt":
            os.fchmod(descriptor, file_mode)
        view = memoryview(bytes(payload))
        while view:
            written = os.write(descriptor, view)
            if written < 1:
                raise OSError("short plugin storage write")
            view = view[written:]
        os.fsync(descriptor)
    except FileExistsError as error:
        raise PluginFilesystemSecurityError("插件存储文件已存在。") from error
    except OSError as error:
        raise PluginFilesystemSecurityError("插件存储文件无法安全写入。") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    try:
        return secure_file(
            boundary,
            target,
            mode=file_mode,
            directory_mode=directory_mode,
        )
    except Exception:
        target.unlink(missing_ok=True)
        raise


def secure_tree(
    root: Path,
    *,
    directory_mode: int,
    file_mode: int,
) -> Path:
    """Reject links/special files and harden a complete extracted tree."""

    boundary = _absolute(root)
    _secure_directory(boundary, directory_mode)
    directories = [boundary]
    while directories:
        directory = directories.pop()
        try:
            children = list(directory.iterdir())
        except OSError as error:
            raise PluginFilesystemSecurityError("插件存储目录无法枚举。") from error
        for child in children:
            metadata = _metadata(child)
            if stat.S_ISDIR(metadata.st_mode):
                _secure_directory(child, directory_mode)
                directories.append(child)
            elif stat.S_ISREG(metadata.st_mode) and metadata.st_nlink == 1:
                secure_file(
                    boundary,
                    child,
                    mode=file_mode,
                    directory_mode=directory_mode,
                )
            else:
                raise PluginFilesystemSecurityError("插件存储树包含链接或特殊文件。")
    return boundary


def validate_secure_tree(root: Path) -> Path:
    """Validate an existing tree before execution, move, or recursive delete."""

    boundary = validate_directory(root)
    directories = [boundary]
    while directories:
        directory = directories.pop()
        try:
            children = list(directory.iterdir())
        except OSError as error:
            raise PluginFilesystemSecurityError("插件存储目录无法枚举。") from error
        for child in children:
            metadata = _metadata(child)
            _require_owner(metadata)
            if stat.S_ISDIR(metadata.st_mode):
                validate_directory(child)
                directories.append(child)
                continue
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise PluginFilesystemSecurityError("插件存储树包含链接或特殊文件。")
            if os.name == "nt":
                _validate_windows_dacl(child, directory=False)
            elif stat.S_IMODE(metadata.st_mode) & 0o022:
                raise PluginFilesystemSecurityError("插件存储文件禁止组或其他用户写入。")
    return boundary


def remove_secure_tree(root: Path, path: Path) -> None:
    """Remove only a validated real tree contained by ``root``."""

    boundary, target = _contained(root, path)
    if not target.exists() and not target.is_symlink():
        return
    validate_directory_chain(boundary, target.parent)
    validate_secure_tree(target)
    try:
        shutil.rmtree(target)
    except OSError as error:
        raise PluginFilesystemSecurityError("插件存储树无法安全删除。") from error
