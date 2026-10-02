"""Standard-library file boundary used before any kit or product import."""
from __future__ import annotations

import hashlib
import os
import re
import secrets
import stat
from contextlib import contextmanager
from pathlib import Path, PurePosixPath


class KitFileError(ValueError):
    pass


def require(condition, code="BOOTSTRAP_KIT_FILE_UNSAFE"):
    if not condition:
        raise KitFileError(code)


def safe_relative(name):
    require(type(name) is str and 0 < len(name) <= 240)
    require(re.fullmatch(r"[A-Za-z0-9_./+-]+", name) is not None)
    parts = name.split("/")
    require(all(part not in {"", ".", ".."} and not part.endswith(".")
                and part.split(".")[0].upper() not in {
                    "CON", "PRN", "AUX", "NUL", *("COM" + str(i) for i in range(10)),
                    *("LPT" + str(i) for i in range(10))} for part in parts))
    require(not PurePosixPath(name).is_absolute())
    return name


def safe_chain(path):
    path = Path(path).absolute()
    for item in (path, *path.parents):
        info = item.lstat()
        require(not stat.S_ISLNK(info.st_mode)
                and not getattr(info, "st_file_attributes", 0) & 0x400)
    return path


def _state(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_nlink,
            info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _identity(info):
    return (info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode), info.st_nlink,
            info.st_size, info.st_mtime_ns)


def _windows_api():
    import ctypes
    from ctypes import wintypes as w
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel.GetCurrentProcess.restype = w.HANDLE
    kernel.CloseHandle.argtypes = [w.HANDLE]
    kernel.CloseHandle.restype = w.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    kernel.CreateFileW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, ctypes.c_void_p,
                                   w.DWORD, w.DWORD, w.HANDLE]
    kernel.CreateFileW.restype = w.HANDLE
    kernel.CreateDirectoryW.argtypes = [w.LPCWSTR, ctypes.c_void_p]
    kernel.CreateDirectoryW.restype = w.BOOL
    advapi.OpenProcessToken.argtypes = [w.HANDLE, w.DWORD, ctypes.POINTER(w.HANDLE)]
    advapi.OpenProcessToken.restype = w.BOOL
    advapi.GetTokenInformation.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                           w.DWORD, ctypes.POINTER(w.DWORD)]
    advapi.GetTokenInformation.restype = w.BOOL
    advapi.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
    advapi.ConvertSidToStringSidW.restype = w.BOOL
    advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        w.LPCWSTR, w.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(w.DWORD)]
    advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = w.BOOL
    return ctypes, w, kernel, advapi


@contextmanager
def hold_path_chain(path):
    """Reject reparse points; Windows retains directory delete-denying holds."""
    path = safe_chain(path)
    handles = []
    primary = None
    try:
        if os.name == "nt":
            c, _w, kernel, _a = _windows_api()
            for directory in reversed((path, *path.parents)):
                handle = kernel.CreateFileW(str(directory), 0x80 | 0x1, 3, None, 3,
                                             0x02000000 | 0x00200000, None)
                require(handle not in (None, c.c_void_p(-1).value))
                handles.append(handle)
            safe_chain(path)
        yield path
    except BaseException as error:
        primary = error
        raise
    finally:
        failed = False
        for handle in reversed(handles):
            failed = not kernel.CloseHandle(handle) or failed
        if failed:
            if primary is None:
                raise KitFileError("BOOTSTRAP_KIT_HANDLE_CLOSE_FAILED")
            primary.secondary_errors = ("BOOTSTRAP_KIT_HANDLE_CLOSE_FAILED",)


def create_private_directory(parent, prefix="animemo-kit-"):
    """Create only a new task-owned leaf; never alter the parent's DACL."""
    require(type(prefix) is str and re.fullmatch(r"[A-Za-z0-9_-]{1,48}", prefix))
    parent = safe_chain(parent)
    with hold_path_chain(parent):
        if os.name != "nt":
            for _ in range(16):
                target = parent / (prefix + secrets.token_hex(16))
                try:
                    target.mkdir(mode=0o700)
                    return target
                except FileExistsError:
                    continue
            raise KitFileError("BOOTSTRAP_KIT_CREATE_FAILED")
        c, w, kernel, advapi = _windows_api()
        token, sid_text, descriptor = w.HANDLE(), c.c_void_p(), c.c_void_p()
        primary = None
        try:
            require(advapi.OpenProcessToken(kernel.GetCurrentProcess(), 8, c.byref(token)))
            needed = w.DWORD()
            advapi.GetTokenInformation(token, 1, None, 0, c.byref(needed))
            require(0 < needed.value <= 65536)
            buffer = c.create_string_buffer(needed.value)
            require(advapi.GetTokenInformation(token, 1, buffer, needed, c.byref(needed)))
            sid = c.cast(buffer, c.POINTER(c.c_void_p))[0]
            require(advapi.ConvertSidToStringSidW(sid, c.byref(sid_text)))
            sddl = "D:P(A;OICI;FA;;;" + c.wstring_at(sid_text) + ")(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)"
            require(advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(
                sddl, 1, c.byref(descriptor), c.byref(needed)))
            class SecurityAttributes(c.Structure):
                _fields_ = (("length", w.DWORD), ("descriptor", c.c_void_p), ("inherit", w.BOOL))
            attributes = SecurityAttributes(c.sizeof(SecurityAttributes), descriptor, False)
            for _ in range(16):
                target = parent / (prefix + secrets.token_hex(16))
                if kernel.CreateDirectoryW(str(target), c.byref(attributes)):
                    safe_chain(target)
                    return target
                if c.get_last_error() != 183:
                    raise KitFileError("BOOTSTRAP_KIT_CREATE_FAILED")
            raise KitFileError("BOOTSTRAP_KIT_CREATE_FAILED")
        except BaseException as error:
            primary = error
            raise
        finally:
            failed = False
            for pointer in (sid_text, descriptor):
                if pointer.value:
                    failed = bool(kernel.LocalFree(pointer)) or failed
            if token.value:
                failed = not kernel.CloseHandle(token) or failed
            if failed:
                if primary is None:
                    raise KitFileError("BOOTSTRAP_KIT_HANDLE_CLOSE_FAILED")
                primary.secondary_errors = ("BOOTSTRAP_KIT_HANDLE_CLOSE_FAILED",)


@contextmanager
def held_file(path, maximum):
    path = Path(path).absolute()
    require(type(maximum) is int and maximum > 0)
    with hold_path_chain(path.parent):
        safe_chain(path)
        before = path.lstat()
        require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1
                and 0 <= before.st_size <= maximum)
        if os.name == "nt":
            import msvcrt
            c, _w, kernel, _a = _windows_api()
            handle = kernel.CreateFileW(str(path), 0x80000000, 1, None, 3, 0x00200000, None)
            require(handle not in (None, c.c_void_p(-1).value))
            try:
                descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
            except BaseException:
                kernel.CloseHandle(handle)
                raise
        else:
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        primary, opened = None, None
        try:
            opened = os.fstat(descriptor)
            require(_identity(before) == _identity(opened), "BOOTSTRAP_KIT_FILE_CHANGED")
            with os.fdopen(descriptor, "rb", closefd=False) as stream:
                yield stream
        except BaseException as error:
            primary = error
            raise
        finally:
            secondary = []
            try:
                require(opened is not None and _state(opened) == _state(os.fstat(descriptor))
                        and _state(before) == _state(path.lstat()), "BOOTSTRAP_KIT_FILE_CHANGED")
            except (OSError, KitFileError):
                secondary.append("BOOTSTRAP_KIT_FILE_CHANGED")
            try:
                os.close(descriptor)
            except OSError:
                secondary.append("BOOTSTRAP_KIT_HANDLE_CLOSE_FAILED")
            if secondary and primary is None:
                primary = KitFileError(secondary[0])
                primary.secondary_errors = tuple(secondary[1:])
                raise primary from None
            if secondary:
                primary.secondary_errors = tuple(secondary)


def read_bounded(path, maximum):
    with held_file(path, maximum) as stream:
        size = os.fstat(stream.fileno()).st_size
        raw = stream.read(size + 1)
        require(len(raw) == size, "BOOTSTRAP_KIT_FILE_CHANGED")
        return raw


def file_digest(stream):
    stream.seek(0)
    size = os.fstat(stream.fileno()).st_size
    digest, count = hashlib.sha256(), 0
    while data := stream.read(min(1048576, size + 1 - count)):
        count += len(data)
        require(count <= size, "BOOTSTRAP_KIT_FILE_CHANGED")
        digest.update(data)
    require(count == size, "BOOTSTRAP_KIT_FILE_CHANGED")
    return "sha256:" + digest.hexdigest(), count


@contextmanager
def exclusive_file(path):
    path = Path(path).absolute()
    with hold_path_chain(path.parent):
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                             | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0), 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            info = os.fstat(stream.fileno())
            require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1)
            yield stream
            stream.flush()
            os.fsync(stream.fileno())


def directory_identity(path):
    info = safe_chain(path).lstat()
    require(stat.S_ISDIR(info.st_mode))
    return info.st_dev, info.st_ino


def remove_owned_directory(path, expected_identity):
    """Single cleanup attempt for a caller-recorded new leaf; never follow links."""
    path = Path(path).absolute()
    require(path.parent != path and directory_identity(path) == tuple(expected_identity),
            "BOOTSTRAP_KIT_CLEANUP_IDENTITY_MISMATCH")
    # Resolve before enumerating/deleting. A raced link is rejected at every
    # subsequent item; each final absolute target must remain below this root.
    root = path.resolve(strict=True)
    files, directories = [], []
    for current, dirs, names in os.walk(root, followlinks=False):
        parent = safe_chain(current)
        require(parent == root or root in parent.parents)
        for name in dirs:
            child = safe_chain(parent / name)
            require(root in child.parents and stat.S_ISDIR(child.lstat().st_mode))
            directories.append((child, directory_identity(child)))
        for name in names:
            child = safe_chain(parent / name)
            info = child.lstat()
            require(root in child.parents and stat.S_ISREG(info.st_mode) and info.st_nlink == 1)
            files.append((child, _state(info)))
    for child, identity in files:
        with hold_path_chain(child.parent):
            safe_chain(child)
            require(_state(child.lstat()) == identity, "BOOTSTRAP_KIT_FILE_CHANGED")
            child.unlink()
    for child, identity in reversed(directories):
        with hold_path_chain(child.parent):
            require(directory_identity(child) == identity, "BOOTSTRAP_KIT_FILE_CHANGED")
            child.rmdir()
    with hold_path_chain(root.parent):
        require(directory_identity(root) == tuple(expected_identity), "BOOTSTRAP_KIT_FILE_CHANGED")
        root.rmdir()
    return True
