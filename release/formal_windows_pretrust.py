from __future__ import annotations

import ctypes
import hashlib
import json
import os
import re
import secrets
import shutil
import stat
import tarfile
import tempfile
from collections.abc import Iterator, Mapping
from contextlib import ExitStack, contextmanager
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from updater.offline import TrustProfile

FORMAL_WINDOWS_PRETRUST_FILES = frozenset(
    {
        "formal-release-verifier.exe",
        "formal-windows-pretrust-manifest.json",
        "formal-windows-trust-profile.json",
        "github-trusted-root.jsonl",
        "github-tuf-root.json",
        "offline-release-verifier",
        "sigstore-trusted-root.jsonl",
        "sigstore-tuf-root.json",
    }
)
FORMAL_WINDOWS_PRETRUST_RUNTIME_FILES = FORMAL_WINDOWS_PRETRUST_FILES - {
    "formal-windows-pretrust-manifest.json"
}
FORMAL_WINDOWS_PRETRUST_PREFIX = (
    "release/release_attestation_verifier/formal-windows-amd64-pretrust-v1"
)

_AUTHORITY_ROLE = "FORMAL_WINDOWS_AMD64_PRETRUST_ONLY"
_RELEASE_AUTHORITY = "GITHUB_IMMUTABLE_RELEASE"
_MAX_FILE_BYTES = 64 * 1024 * 1024
_DIGEST_PREFIX = "sha256:"
_TRUSTED_INSTALLER_SID = (
    "S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464"
)


class FormalWindowsPretrustError(ValueError):
    pass


@dataclass(frozen=True)
class WindowsPrivateToolBundle:
    """Identity of an exact executable bundle held in a private directory."""

    root: Path
    executable: Path
    file_identities: Mapping[str, str]
    aggregate_identity: str


@dataclass(frozen=True)
class WindowsFixedSourceSnapshot:
    """A fixed-drive source inventory held without write/delete sharing."""

    root: Path
    relative_files: tuple[str, ...]


@dataclass(frozen=True)
class WindowsPrivateSourceSnapshot:
    """An exact source VM inventory copied into and held in a private root."""

    root: Path
    file_identities: Mapping[str, str]
    aggregate_identity: str


@dataclass(frozen=True)
class WindowsPrivateTreeSnapshot:
    """An explicit recursive file set copied into a held private tree."""

    root: Path
    file_identities: Mapping[str, str]
    aggregate_identity: str


class _WindowsDirectoryDeleteAccessDenied(FormalWindowsPretrustError):
    pass


@dataclass(frozen=True)
class _HeldWindowsPrivatePathRecord:
    root: Path


_HELD_WINDOWS_PRIVATE_PATHS: dict[object, _HeldWindowsPrivatePathRecord] = {}


class HeldWindowsPrivatePathAuthority:
    """Opaque active full-chain authority reusable by descendant holders."""

    __slots__ = ("__token",)

    def __init__(self) -> None:
        raise TypeError("Windows private path authority不能直接构造")

    def __reduce__(self):
        raise TypeError("Windows private path authority不可序列化")

    def _record(self) -> _HeldWindowsPrivatePathRecord:
        try:
            return _HELD_WINDOWS_PRIVATE_PATHS[self.__token]
        except (AttributeError, KeyError) as error:
            raise FormalWindowsPretrustError(
                "Formal Windows private path authority无效"
            ) from error

    @property
    def root(self) -> Path:
        return self._record().root


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _digest(value: bytes) -> str:
    return _DIGEST_PREFIX + hashlib.sha256(value).hexdigest()


def _is_digest(value: object) -> bool:
    return (
        type(value) is str
        and value.startswith(_DIGEST_PREFIX)
        and len(value) == 71
        and all(character in "0123456789abcdef" for character in value[7:])
    )


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise FormalWindowsPretrustError("Formal Windows pretrust JSON字段重复")
        result[key] = value
    return result


def _json_object(value: bytes, *, label: str) -> dict[str, object]:
    try:
        decoded = json.loads(
            value.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise FormalWindowsPretrustError(f"{label}不是JSON") from error
    if type(decoded) is not dict or _canonical_json_bytes(decoded) != value:
        raise FormalWindowsPretrustError(f"{label}不是canonical JSON对象")
    return decoded


def _is_reparse(metadata: os.stat_result) -> bool:
    attributes = getattr(metadata, "st_file_attributes", 0)
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & reparse)


class _WindowsTrustee(ctypes.Structure):
    _fields_ = (
        ("multiple_trustee", ctypes.c_void_p),
        ("multiple_trustee_operation", ctypes.c_int),
        ("trustee_form", ctypes.c_int),
        ("trustee_type", ctypes.c_int),
        ("name", ctypes.c_void_p),
    )


class _WindowsSidAndAttributes(ctypes.Structure):
    _fields_ = (("sid", ctypes.c_void_p), ("attributes", wintypes.DWORD))


class _WindowsTokenUser(ctypes.Structure):
    _fields_ = (("user", _WindowsSidAndAttributes),)


class _WindowsAclSizeInformation(ctypes.Structure):
    _fields_ = (
        ("ace_count", wintypes.DWORD),
        ("acl_bytes_in_use", wintypes.DWORD),
        ("acl_bytes_free", wintypes.DWORD),
    )


class _WindowsAceHeader(ctypes.Structure):
    _fields_ = (
        ("ace_type", ctypes.c_ubyte),
        ("ace_flags", ctypes.c_ubyte),
        ("ace_size", wintypes.WORD),
    )


class _WindowsFileId128(ctypes.Structure):
    _fields_ = (("identifier", ctypes.c_ubyte * 16),)


class _WindowsFileIdInformation(ctypes.Structure):
    _fields_ = (
        ("volume_serial_number", ctypes.c_ulonglong),
        ("file_id", _WindowsFileId128),
    )


class _WindowsGenericMapping(ctypes.Structure):
    _fields_ = (
        ("generic_read", wintypes.DWORD),
        ("generic_write", wintypes.DWORD),
        ("generic_execute", wintypes.DWORD),
        ("generic_all", wintypes.DWORD),
    )


@dataclass(frozen=True)
class _WindowsAceObservation:
    ace_type: int
    flags: int
    mask: int
    trustee_trusted: bool
    trustee_current: bool = False
    creator_owner: bool = False


_WINDOWS_WRITE_MASK = (
    0x00000002  # FILE_WRITE_DATA / FILE_ADD_FILE
    | 0x00000004  # FILE_APPEND_DATA / FILE_ADD_SUBDIRECTORY
    | 0x00000010  # FILE_WRITE_EA
    | 0x00000040  # FILE_DELETE_CHILD
    | 0x00000100  # FILE_WRITE_ATTRIBUTES
    | 0x00010000  # DELETE
    | 0x00040000  # WRITE_DAC
    | 0x00080000  # WRITE_OWNER
    | 0x10000000  # GENERIC_ALL
    | 0x40000000  # GENERIC_WRITE
)
_WINDOWS_REBIND_MASK = (
    0x00000040  # FILE_DELETE_CHILD
    | 0x00010000  # DELETE
    | 0x00040000  # WRITE_DAC
    | 0x00080000  # WRITE_OWNER
    | 0x10000000  # GENERIC_ALL
)


def _validate_windows_acl_observation(
    *,
    owner_trusted: bool,
    aces: tuple[_WindowsAceObservation, ...],
    mutation_mask: int = _WINDOWS_WRITE_MASK,
) -> None:
    if not owner_trusted:
        raise FormalWindowsPretrustError("Formal Windows authority owner不受信")
    for ace in aces:
        if ace.ace_type in {1, 6}:  # ACCESS_DENIED(_OBJECT)_ACE
            continue
        if ace.ace_type not in {0, 5}:  # allow / object-allow only
            raise FormalWindowsPretrustError("Formal Windows authority ACE类型不受支持")
        if ace.flags & 0x08:  # INHERIT_ONLY_ACE
            continue
        if (
            ace.mask & mutation_mask
            and not (ace.trustee_trusted or ace.creator_owner)
        ):
            raise FormalWindowsPretrustError(
                "Formal Windows authority DACL允许非受信主体写入"
            )


def _assert_no_reparse_components(components: tuple[Path, ...]) -> None:
    try:
        if any(_is_reparse(item.lstat()) for item in components):
            raise FormalWindowsPretrustError(
                "Formal Windows authority父链包含reparse point"
            )
    except OSError as error:
        raise FormalWindowsPretrustError(
            "Formal Windows authority父链不可验证"
        ) from error


class _WindowsAclAuthority:
    _ERROR_INSUFFICIENT_BUFFER = 122
    _OWNER_SECURITY_INFORMATION = 0x00000001
    _GROUP_SECURITY_INFORMATION = 0x00000002
    _DACL_SECURITY_INFORMATION = 0x00000004
    _TOKEN_QUERY = 0x0008
    _TOKEN_DUPLICATE = 0x0002
    _TOKEN_USER = 1
    _SECURITY_IMPERSONATION = 2
    _ACL_SIZE_INFORMATION_CLASS = 2
    _DRIVE_FIXED = 3
    _TRUSTED_SID_TYPES = (22, 26)  # LocalSystem, Builtin Administrators
    _CREATOR_OWNER_SID_TYPE = 3

    def __init__(self, *, dll_loader: object | None = None) -> None:
        if dll_loader is None:
            dll_loader = ctypes.WinDLL
        try:
            self.advapi32 = dll_loader("advapi32", use_last_error=True)  # type: ignore[operator]
            self.kernel32 = dll_loader("kernel32", use_last_error=True)  # type: ignore[operator]
            self._declare()
        except (AttributeError, OSError, TypeError, ValueError) as error:
            raise FormalWindowsPretrustError(
                "Formal Windows security ABI不可用"
            ) from error

    def _declare(self) -> None:
        pointer = ctypes.POINTER(ctypes.c_void_p)
        self.advapi32.CreateWellKnownSid.argtypes = (
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.POINTER(wintypes.DWORD),
        )
        self.advapi32.CreateWellKnownSid.restype = wintypes.BOOL
        self.advapi32.EqualSid.argtypes = (ctypes.c_void_p, ctypes.c_void_p)
        self.advapi32.EqualSid.restype = wintypes.BOOL
        self.advapi32.ConvertSidToStringSidW.argtypes = (
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
        )
        self.advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL
        self.advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = (
            wintypes.LPCWSTR,
            wintypes.DWORD,
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(wintypes.DWORD),
        )
        self.advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = (
            wintypes.BOOL
        )
        self.advapi32.GetAclInformation.argtypes = (
            ctypes.c_void_p,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.c_int,
        )
        self.advapi32.GetAclInformation.restype = wintypes.BOOL
        self.advapi32.GetAce.argtypes = (
            ctypes.c_void_p,
            wintypes.DWORD,
            pointer,
        )
        self.advapi32.GetAce.restype = wintypes.BOOL
        self.advapi32.GetNamedSecurityInfoW.argtypes = (
            wintypes.LPCWSTR,
            ctypes.c_int,
            wintypes.DWORD,
            pointer,
            pointer,
            pointer,
            pointer,
            pointer,
        )
        self.advapi32.GetNamedSecurityInfoW.restype = wintypes.DWORD
        self.advapi32.GetTokenInformation.argtypes = (
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        )
        self.advapi32.GetTokenInformation.restype = wintypes.BOOL
        self.advapi32.OpenProcessToken.argtypes = (
            wintypes.HANDLE,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.HANDLE),
        )
        self.advapi32.OpenProcessToken.restype = wintypes.BOOL
        self.advapi32.DuplicateToken.argtypes = (
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.POINTER(wintypes.HANDLE),
        )
        self.advapi32.DuplicateToken.restype = wintypes.BOOL
        self.advapi32.AccessCheck.argtypes = (
            ctypes.c_void_p,
            wintypes.HANDLE,
            wintypes.DWORD,
            ctypes.POINTER(_WindowsGenericMapping),
            ctypes.c_void_p,
            ctypes.POINTER(wintypes.DWORD),
            ctypes.POINTER(wintypes.DWORD),
            ctypes.POINTER(wintypes.BOOL),
        )
        self.advapi32.AccessCheck.restype = wintypes.BOOL
        self.kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        self.kernel32.CloseHandle.restype = wintypes.BOOL
        self.kernel32.CreateDirectoryW.argtypes = (
            wintypes.LPCWSTR,
            ctypes.c_void_p,
        )
        self.kernel32.CreateDirectoryW.restype = wintypes.BOOL
        self.kernel32.GetCurrentProcess.argtypes = ()
        self.kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        self.kernel32.GetDriveTypeW.argtypes = (wintypes.LPCWSTR,)
        self.kernel32.GetDriveTypeW.restype = wintypes.UINT
        self.kernel32.GetVolumePathNameW.argtypes = (
            wintypes.LPCWSTR,
            wintypes.LPWSTR,
            wintypes.DWORD,
        )
        self.kernel32.GetVolumePathNameW.restype = wintypes.BOOL
        self.kernel32.LocalFree.argtypes = (ctypes.c_void_p,)
        self.kernel32.LocalFree.restype = ctypes.c_void_p

    def _well_known_sid(self, kind: int) -> ctypes.Array[ctypes.c_char]:
        size = wintypes.DWORD(68)
        sid = ctypes.create_string_buffer(size.value)
        if not self.advapi32.CreateWellKnownSid(
            kind, None, sid, ctypes.byref(size)
        ):
            raise FormalWindowsPretrustError(
                "Formal Windows trusted SID不可用"
            )
        return sid

    def _equal_any(
        self, sid: ctypes.c_void_p, candidates: tuple[ctypes.c_void_p, ...]
    ) -> bool:
        return any(self.advapi32.EqualSid(sid, candidate) for candidate in candidates)

    def _sid_string(self, sid: ctypes.c_void_p) -> str:
        value = ctypes.c_void_p()
        if not self.advapi32.ConvertSidToStringSidW(
            sid, ctypes.byref(value)
        ) or not value.value:
            raise FormalWindowsPretrustError(
                "Formal Windows SID不可序列化"
            )
        try:
            return ctypes.wstring_at(value.value)
        finally:
            if self.kernel32.LocalFree(value):
                raise FormalWindowsPretrustError(
                    "Formal Windows SID resource释放失败"
                )

    def assert_fixed_non_reparse_chain(self, path: Path) -> None:
        volume = ctypes.create_unicode_buffer(32768)
        if not self.kernel32.GetVolumePathNameW(
            str(path), volume, len(volume)
        ) or self.kernel32.GetDriveTypeW(volume.value) != self._DRIVE_FIXED:
            raise FormalWindowsPretrustError(
                "Formal Windows authority必须位于fixed drive"
            )
        volume_root = Path(volume.value)
        try:
            relative = path.relative_to(volume_root)
        except ValueError as error:
            raise FormalWindowsPretrustError(
                "Formal Windows authority volume identity无效"
            ) from error
        current = volume_root
        components = [current]
        for part in relative.parts:
            current /= part
            components.append(current)
        _assert_no_reparse_components(tuple(components))

    def _current_user_buffer(
        self, token: wintypes.HANDLE
    ) -> tuple[ctypes.Array[ctypes.c_char], ctypes.c_void_p]:
        required = wintypes.DWORD()
        ctypes.set_last_error(0)
        probe = self.advapi32.GetTokenInformation(
            token, self._TOKEN_USER, None, 0, ctypes.byref(required)
        )
        if (
            probe
            or required.value == 0
            or ctypes.get_last_error() != self._ERROR_INSUFFICIENT_BUFFER
        ):
            raise FormalWindowsPretrustError(
                "Formal Windows current token不可验证"
            )
        buffer = ctypes.create_string_buffer(required.value)
        if not self.advapi32.GetTokenInformation(
            token,
            self._TOKEN_USER,
            buffer,
            required.value,
            ctypes.byref(required),
        ):
            raise FormalWindowsPretrustError(
                "Formal Windows current token不可验证"
            )
        token_user = ctypes.cast(
            buffer, ctypes.POINTER(_WindowsTokenUser)
        ).contents
        if not token_user.user.sid:
            raise FormalWindowsPretrustError(
                "Formal Windows current SID不可验证"
            )
        return buffer, ctypes.c_void_p(token_user.user.sid)

    def _current_token_has_mutation_access(
        self,
        *,
        descriptor: ctypes.c_void_p,
        token: wintypes.HANDLE,
        mutation_mask: int,
    ) -> bool:
        """Ask the Windows access checker about the complete enabled token.

        Comparing an ACE only with TokenUser misses enabled group grants (most
        importantly an elevated Administrators SID) and mishandles deny-only
        groups and ordered deny ACEs.  AccessCheck is the canonical evaluator;
        an impersonation duplicate is mandatory and every ABI/query failure is
        fail-closed.
        """

        # Generic bits belong to the static ACE policy.  AccessCheck maps any
        # generic ACE grant while evaluating each concrete mutation right; do
        # not expand GENERIC_ALL into unrelated rights such as ADD_SUBDIRECTORY.
        requested = mutation_mask
        access_bits = tuple(
            bit
            for bit in (
                0x00000002,  # FILE_WRITE_DATA / FILE_ADD_FILE
                0x00000004,  # FILE_APPEND_DATA / FILE_ADD_SUBDIRECTORY
                0x00000010,  # FILE_WRITE_EA
                0x00000040,  # FILE_DELETE_CHILD
                0x00000100,  # FILE_WRITE_ATTRIBUTES
                0x00010000,  # DELETE
                0x00040000,  # WRITE_DAC
                0x00080000,  # WRITE_OWNER
            )
            if requested & bit
        )
        if not access_bits:
            raise FormalWindowsPretrustError(
                "Formal Windows mutation access mask无效"
            )
        impersonation = wintypes.HANDLE()
        acquired = False
        try:
            if not self.advapi32.DuplicateToken(
                token,
                self._SECURITY_IMPERSONATION,
                ctypes.byref(impersonation),
            ) or not impersonation.value:
                raise FormalWindowsPretrustError(
                    "Formal Windows current token access check不可用"
                )
            acquired = True
            mapping = _WindowsGenericMapping(
                0x00120089,  # FILE_GENERIC_READ
                0x00120116,  # FILE_GENERIC_WRITE
                0x001200A0,  # FILE_GENERIC_EXECUTE
                0x001F01FF,  # FILE_ALL_ACCESS
            )
            for desired_access in access_bits:
                privilege_size = wintypes.DWORD(1024)
                privilege_set = ctypes.create_string_buffer(
                    privilege_size.value
                )
                granted = wintypes.DWORD()
                access_status = wintypes.BOOL()
                ctypes.set_last_error(0)
                checked = self.advapi32.AccessCheck(
                    descriptor,
                    impersonation,
                    desired_access,
                    ctypes.byref(mapping),
                    privilege_set,
                    ctypes.byref(privilege_size),
                    ctypes.byref(granted),
                    ctypes.byref(access_status),
                )
                if checked:
                    if access_status.value:
                        return True
                    continue
                if (
                    ctypes.get_last_error() != self._ERROR_INSUFFICIENT_BUFFER
                    or privilege_size.value <= len(privilege_set)
                    or privilege_size.value > 1024 * 1024
                ):
                    error_code = ctypes.get_last_error()
                    raise FormalWindowsPretrustError(
                        "Formal Windows current token access check失败: "
                        f"winerror={error_code}"
                    )
                privilege_set = ctypes.create_string_buffer(
                    privilege_size.value
                )
                if not self.advapi32.AccessCheck(
                    descriptor,
                    impersonation,
                    desired_access,
                    ctypes.byref(mapping),
                    privilege_set,
                    ctypes.byref(privilege_size),
                    ctypes.byref(granted),
                    ctypes.byref(access_status),
                ):
                    raise FormalWindowsPretrustError(
                        "Formal Windows current token access check失败"
                    )
                if access_status.value:
                    return True
            return False
        finally:
            if acquired and not self.kernel32.CloseHandle(impersonation):
                raise FormalWindowsPretrustError(
                    "Formal Windows current token access resource释放失败"
                )

    def create_private_directory(
        self,
        parent: Path,
        prefix: str,
        *,
        exact_name: str | None = None,
    ) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,48}", prefix):
            raise FormalWindowsPretrustError(
                "Formal Windows private directory prefix无效"
            )
        self.assert_fixed_non_reparse_chain(parent)
        token = wintypes.HANDLE()
        token_acquired = False
        sid_string_pointer = ctypes.c_void_p()
        descriptor = ctypes.c_void_p()
        descriptor_acquired = False
        try:
            if not self.advapi32.OpenProcessToken(
                self.kernel32.GetCurrentProcess(),
                self._TOKEN_QUERY,
                ctypes.byref(token),
            ) or not token.value:
                raise FormalWindowsPretrustError(
                    "Formal Windows current token不可用"
                )
            token_acquired = True
            token_buffer, current_sid = self._current_user_buffer(token)
            if not self.advapi32.ConvertSidToStringSidW(
                current_sid, ctypes.byref(sid_string_pointer)
            ) or not sid_string_pointer.value:
                raise FormalWindowsPretrustError(
                    "Formal Windows current SID不可序列化"
                )
            sid_string = ctypes.wstring_at(sid_string_pointer.value)
            sddl = (
                f"D:P(A;OICI;FA;;;{sid_string})"
                "(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)"
            )
            descriptor_size = wintypes.DWORD()
            if not self.advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
                sddl,
                1,
                ctypes.byref(descriptor),
                ctypes.byref(descriptor_size),
            ) or not descriptor.value:
                raise FormalWindowsPretrustError(
                    "Formal Windows private DACL不可构造"
                )
            descriptor_acquired = True

            class SecurityAttributes(ctypes.Structure):
                _fields_ = (
                    ("length", wintypes.DWORD),
                    ("security_descriptor", ctypes.c_void_p),
                    ("inherit_handle", wintypes.BOOL),
                )

            attributes = SecurityAttributes(
                ctypes.sizeof(SecurityAttributes), descriptor, False
            )
            created: Path | None = None
            attempts = 1 if exact_name is not None else 32
            if exact_name is not None and re.fullmatch(r"[0-9a-f]{64}", exact_name) is None:
                raise FormalWindowsPretrustError(
                    "Formal Windows private directory exact name无效"
                )
            for _attempt in range(attempts):
                candidate = parent / (
                    exact_name
                    if exact_name is not None
                    else f"{prefix}-{secrets.token_hex(16)}"
                )
                if self.kernel32.CreateDirectoryW(
                    str(candidate), ctypes.byref(attributes)
                ):
                    created = candidate
                    break
                if ctypes.get_last_error() != 183:  # ERROR_ALREADY_EXISTS
                    raise FormalWindowsPretrustError(
                        "Formal Windows private directory不可创建"
                    )
            if created is None:
                raise FormalWindowsPretrustError(
                    "Formal Windows private directory名称冲突"
                )
            del token_buffer
            try:
                self.assert_fixed_non_reparse_chain(created)
                self.inspect_acl(created)
            except BaseException:
                try:
                    created.rmdir()
                except OSError:
                    pass
                raise
            return created
        finally:
            cleanup_failed = False
            if descriptor_acquired and self.kernel32.LocalFree(descriptor):
                cleanup_failed = True
            if sid_string_pointer.value and self.kernel32.LocalFree(
                sid_string_pointer
            ):
                cleanup_failed = True
            if token_acquired and not self.kernel32.CloseHandle(token):
                cleanup_failed = True
            if cleanup_failed:
                raise FormalWindowsPretrustError(
                    "Formal Windows private directory resource释放失败"
                )

    def inspect_acl(
        self,
        path: Path,
        *,
        reject_current_mutation: bool = False,
        allow_trusted_installer_owner: bool = False,
        allow_trusted_installer_writer: bool = False,
        mutation_mask: int = _WINDOWS_WRITE_MASK,
    ) -> None:
        owner = ctypes.c_void_p()
        group = ctypes.c_void_p()
        dacl = ctypes.c_void_p()
        descriptor = ctypes.c_void_p()
        token = wintypes.HANDLE()
        descriptor_acquired = False
        token_acquired = False
        try:
            result = self.advapi32.GetNamedSecurityInfoW(
                str(path),
                1,
                self._OWNER_SECURITY_INFORMATION
                | self._GROUP_SECURITY_INFORMATION
                | self._DACL_SECURITY_INFORMATION,
                ctypes.byref(owner),
                ctypes.byref(group),
                ctypes.byref(dacl),
                None,
                ctypes.byref(descriptor),
            )
            if result == 0 and descriptor.value:
                descriptor_acquired = True
            if (
                result != 0
                or not descriptor.value
                or not owner.value
                or not group.value
                or not dacl.value
            ):
                raise FormalWindowsPretrustError(
                    "Formal Windows authority owner/DACL不可验证"
                )
            if not self.advapi32.OpenProcessToken(
                self.kernel32.GetCurrentProcess(),
                self._TOKEN_QUERY
                | (self._TOKEN_DUPLICATE if reject_current_mutation else 0),
                ctypes.byref(token),
            ) or not token.value:
                raise FormalWindowsPretrustError(
                    "Formal Windows current token不可用"
                )
            token_acquired = True
            current_buffer, current_sid = self._current_user_buffer(token)
            system = self._well_known_sid(22)
            administrators = self._well_known_sid(26)
            creator_owner = self._well_known_sid(self._CREATOR_OWNER_SID_TYPE)
            trusted = (
                current_sid,
                ctypes.cast(system, ctypes.c_void_p),
                ctypes.cast(administrators, ctypes.c_void_p),
            )
            owner_trusted = self._equal_any(owner, trusted) or bool(
                allow_trusted_installer_owner
                and self._sid_string(owner) == _TRUSTED_INSTALLER_SID
            )
            info = _WindowsAclSizeInformation()
            if not self.advapi32.GetAclInformation(
                dacl,
                ctypes.byref(info),
                ctypes.sizeof(info),
                self._ACL_SIZE_INFORMATION_CLASS,
            ):
                raise FormalWindowsPretrustError(
                    "Formal Windows authority ACL不可枚举"
                )
            observations: list[_WindowsAceObservation] = []
            for index in range(info.ace_count):
                ace_pointer = ctypes.c_void_p()
                if not self.advapi32.GetAce(
                    dacl, index, ctypes.byref(ace_pointer)
                ) or not ace_pointer.value:
                    raise FormalWindowsPretrustError(
                        "Formal Windows authority ACE不可枚举"
                    )
                header = ctypes.cast(
                    ace_pointer, ctypes.POINTER(_WindowsAceHeader)
                ).contents
                if header.ace_size < 8:
                    raise FormalWindowsPretrustError(
                        "Formal Windows authority ACE大小无效"
                    )
                mask = wintypes.DWORD.from_address(ace_pointer.value + 4).value
                if header.ace_type in {0, 1}:
                    sid_offset = 8
                elif header.ace_type in {5, 6}:
                    if header.ace_size < 12:
                        raise FormalWindowsPretrustError(
                            "Formal Windows authority object ACE大小无效"
                        )
                    object_flags = wintypes.DWORD.from_address(
                        ace_pointer.value + 8
                    ).value
                    sid_offset = 12
                    if object_flags & 0x1:
                        sid_offset += 16
                    if object_flags & 0x2:
                        sid_offset += 16
                else:
                    observations.append(
                        _WindowsAceObservation(
                            header.ace_type,
                            header.ace_flags,
                            mask,
                            False,
                        )
                    )
                    continue
                if sid_offset >= header.ace_size:
                    raise FormalWindowsPretrustError(
                        "Formal Windows authority ACE SID无效"
                    )
                sid = ctypes.c_void_p(ace_pointer.value + sid_offset)
                trustee_trusted = self._equal_any(sid, trusted) or bool(
                    allow_trusted_installer_writer
                    and self._sid_string(sid) == _TRUSTED_INSTALLER_SID
                )
                observations.append(
                    _WindowsAceObservation(
                        ace_type=header.ace_type,
                        flags=header.ace_flags,
                        mask=mask,
                        trustee_trusted=trustee_trusted,
                        trustee_current=bool(
                            self.advapi32.EqualSid(sid, current_sid)
                        ),
                        creator_owner=bool(
                            owner_trusted
                            and self.advapi32.EqualSid(
                                sid, ctypes.cast(creator_owner, ctypes.c_void_p)
                            )
                        ),
                    )
                )
            _validate_windows_acl_observation(
                owner_trusted=owner_trusted,
                aces=tuple(observations),
                mutation_mask=mutation_mask,
            )
            if reject_current_mutation and self._current_token_has_mutation_access(
                descriptor=descriptor,
                token=token,
                mutation_mask=mutation_mask,
            ):
                raise FormalWindowsPretrustError(
                    "Formal Windows fallback ancestor允许current token mutation"
                )
            del current_buffer
        finally:
            cleanup_failed = False
            if token_acquired and not self.kernel32.CloseHandle(token):
                cleanup_failed = True
            if descriptor_acquired and self.kernel32.LocalFree(descriptor):
                cleanup_failed = True
            if cleanup_failed:
                raise FormalWindowsPretrustError(
                    "Formal Windows security resource释放失败"
                )


def _assert_windows_acl(
    path: Path,
    *,
    allow_trusted_installer_owner: bool,
    allow_trusted_installer_writer: bool = False,
    allow_hardlinked_file: bool = False,
) -> None:
    path = Path(path)
    if not path.is_absolute():
        raise FormalWindowsPretrustError("Formal Windows authority路径必须为绝对路径")
    try:
        metadata = path.lstat()
    except OSError as error:
        raise FormalWindowsPretrustError("Formal Windows authority路径不可用") from error
    if (
        path.is_symlink()
        or _is_reparse(metadata)
        or not (stat.S_ISDIR(metadata.st_mode) or stat.S_ISREG(metadata.st_mode))
        or (
            stat.S_ISREG(metadata.st_mode)
            and metadata.st_nlink != 1
            and not allow_hardlinked_file
        )
    ):
        raise FormalWindowsPretrustError("Formal Windows authority路径不私有")
    if os.name == "nt":
        authority = _WindowsAclAuthority()
        authority.assert_fixed_non_reparse_chain(path)
        authority.inspect_acl(
            path,
            allow_trusted_installer_owner=allow_trusted_installer_owner,
            allow_trusted_installer_writer=allow_trusted_installer_writer,
        )


def assert_windows_private_acl(path: Path) -> None:
    """Require a non-reparse private file/directory and no broad DACL writer.

    Formal consumers call this for both the private execution directory and
    every snapshotted authority input.  It intentionally has no test or
    non-production bypass.
    """

    _assert_windows_acl(path, allow_trusted_installer_owner=False)


def create_windows_private_directory(parent: Path, *, prefix: str) -> Path:
    """Atomically create a protected Windows directory with a canonical DACL."""

    parent = Path(parent)
    if not parent.is_absolute() or not parent.is_dir() or parent.is_symlink():
        raise FormalWindowsPretrustError(
            "Formal Windows private directory parent无效"
        )
    if os.name != "nt":
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,48}", prefix):
            raise FormalWindowsPretrustError(
                "Formal Windows private directory prefix无效"
            )
        path = Path(tempfile.mkdtemp(prefix=prefix + "-", dir=parent))
        path.chmod(0o700)
        return path
    return _WindowsAclAuthority().create_private_directory(parent, prefix)


def create_windows_private_named_directory(parent: Path, *, name: str) -> Path:
    """Create one canonical private directory at an exact digest leaf name."""

    parent = Path(parent)
    if (
        re.fullmatch(r"[0-9a-f]{64}", name) is None
        or not parent.is_absolute()
        or not parent.is_dir()
        or parent.is_symlink()
        or (parent / name).exists()
        or (parent / name).is_symlink()
    ):
        raise FormalWindowsPretrustError(
            "Formal Windows private named directory参数无效"
        )
    if os.name != "nt":
        path = parent / name
        path.mkdir(mode=0o700)
        return path
    return _WindowsAclAuthority().create_private_directory(
        parent, "private-digest", exact_name=name
    )


def _windows_path_identity(path: Path) -> tuple[int, int]:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise FormalWindowsPretrustError(
            "Formal Windows authority path identity不可用"
        ) from error
    if path.is_symlink() or _is_reparse(metadata):
        raise FormalWindowsPretrustError(
            "Formal Windows authority path identity包含reparse"
        )
    return int(metadata.st_dev), int(metadata.st_ino)


def _declare_windows_handle_identity(kernel32: object) -> None:
    kernel32.GetFileInformationByHandleEx.argtypes = (  # type: ignore[attr-defined]
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    )
    kernel32.GetFileInformationByHandleEx.restype = wintypes.BOOL  # type: ignore[attr-defined]


def _windows_handle_identity(kernel32: object, handle: object) -> tuple[int, int]:
    information = _WindowsFileIdInformation()
    if not kernel32.GetFileInformationByHandleEx(  # type: ignore[attr-defined]
        handle,
        18,  # FileIdInfo
        ctypes.byref(information),
        ctypes.sizeof(information),
    ):
        raise FormalWindowsPretrustError(
            "Formal Windows authority handle identity不可用"
        )
    identifier = bytes(information.file_id.identifier)
    return (
        int(information.volume_serial_number),
        int.from_bytes(identifier, "little"),
    )


@contextmanager
def _hold_windows_directory_component(
    path: Path,
    *,
    allow_child_writes: bool,
    request_delete: bool,
    share_delete: bool = False,
) -> Iterator[Path]:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    _declare_windows_handle_identity(kernel32)
    before_identity = _windows_path_identity(path)
    handle = kernel32.CreateFileW(
        str(path),
        0x00000080 | (0x00010000 if request_delete else 0),
        # FILE_READ_ATTRIBUTES [+ DELETE for rename lock]
        0x00000001
        | (0x00000002 if allow_child_writes else 0)
        | (0x00000004 if share_delete else 0),
        None,
        3,
        0x02200000,
        None,
    )
    invalid = ctypes.c_void_p(-1).value
    if handle in (None, 0, invalid):
        if request_delete and ctypes.get_last_error() == 5:
            raise _WindowsDirectoryDeleteAccessDenied(
                "Formal Windows directory DELETE access被拒绝"
            )
        raise FormalWindowsPretrustError(
            "Formal Windows directory chain handle不可用"
        )
    try:
        handle_identity = _windows_handle_identity(kernel32, handle)
        if not (
            before_identity == handle_identity == _windows_path_identity(path)
        ):
            raise FormalWindowsPretrustError(
                "Formal Windows directory chain handle发生rebound"
            )
        yield path
        if handle_identity != _windows_path_identity(path):
            raise FormalWindowsPretrustError(
                "Formal Windows directory chain path发生rebound"
            )
    finally:
        if not kernel32.CloseHandle(handle):
            raise FormalWindowsPretrustError(
                "Formal Windows directory chain handle释放失败"
            )


@contextmanager
def hold_windows_private_directory(
    root: Path, *, allow_child_writes: bool = False
) -> Iterator[Path]:
    """Hold a private directory without FILE_SHARE_DELETE for an execution.

    This prevents a writer on an ancestor directory from renaming/deleting the
    snapshotted root while the Formal verifier runs.  Use ``allow_child_writes``
    only for the outer private-work boundary while creating a child snapshot;
    hold the completed child snapshot with the default for the complete
    verifier subprocess lifetime.
    """

    root = Path(root)
    assert_windows_private_acl(root)
    if os.name != "nt":
        yield root
        return
    with _hold_windows_directory_component(
        root, allow_child_writes=allow_child_writes, request_delete=True
    ):
        assert_windows_private_acl(root)
        yield root
        assert_windows_private_acl(root)


@contextmanager
def hold_windows_private_path_chain(
    root: Path, *, allow_leaf_child_writes: bool = True
) -> Iterator[Path]:
    """Hold every path component from fixed-volume root through private root."""

    root = Path(root)
    assert_windows_private_acl(root)
    if os.name != "nt":
        yield root
        return
    authority = _WindowsAclAuthority()
    authority.assert_fixed_non_reparse_chain(root)
    current = Path(root.anchor)
    components = [current]
    for part in root.parts[1:]:
        current /= part
        components.append(current)
    with ExitStack() as stack:
        for index, component in enumerate(components):
            leaf = index == len(components) - 1
            volume_root = component == Path(root.anchor)
            try:
                stack.enter_context(
                    _hold_windows_directory_component(
                        component,
                        allow_child_writes=(
                            allow_leaf_child_writes if leaf else True
                        ),
                        request_delete=not volume_root,
                        share_delete=volume_root,
                    )
                )
            except _WindowsDirectoryDeleteAccessDenied:
                if leaf:
                    raise
                if component != Path(root.anchor):
                    authority.inspect_acl(
                        component,
                        reject_current_mutation=True,
                        allow_trusted_installer_owner=True,
                        mutation_mask=_WINDOWS_REBIND_MASK,
                    )
                stack.enter_context(
                    _hold_windows_directory_component(
                        component,
                        allow_child_writes=True,
                        request_delete=False,
                    )
                )
        assert_windows_private_acl(root)
        yield root
        assert_windows_private_acl(root)


@contextmanager
def hold_windows_private_path_authority(
    root: Path, *, allow_leaf_child_writes: bool = True
) -> Iterator[HeldWindowsPrivatePathAuthority]:
    """Issue an opaque capability for one actively held complete path chain."""

    root = Path(root).resolve(strict=True)
    token = object()
    authority = object.__new__(HeldWindowsPrivatePathAuthority)
    authority._HeldWindowsPrivatePathAuthority__token = token
    with hold_windows_private_path_chain(
        root, allow_leaf_child_writes=allow_leaf_child_writes
    ):
        _HELD_WINDOWS_PRIVATE_PATHS[token] = _HeldWindowsPrivatePathRecord(root=root)
        try:
            yield authority
            if authority.root != root:
                raise FormalWindowsPretrustError(
                    "Formal Windows private path authority发生rebound"
                )
        finally:
            _HELD_WINDOWS_PRIVATE_PATHS.pop(token, None)
            authority._HeldWindowsPrivatePathAuthority__token = None


@contextmanager
def hold_windows_private_descendant_path(
    parent_authority: HeldWindowsPrivatePathAuthority,
    descendant: Path,
    *,
    allow_leaf_child_writes: bool = True,
) -> Iterator[Path]:
    """Extend an active parent chain without reopening any held ancestor."""

    if type(parent_authority) is not HeldWindowsPrivatePathAuthority:
        raise FormalWindowsPretrustError(
            "Formal Windows private parent path authority无效"
        )
    parent = parent_authority.root
    descendant = Path(descendant).resolve(strict=True)
    try:
        relative = descendant.relative_to(parent)
    except ValueError as error:
        raise FormalWindowsPretrustError(
            "Formal Windows private descendant越界"
        ) from error
    if not relative.parts:
        raise FormalWindowsPretrustError(
            "Formal Windows private descendant必须是子路径"
        )
    assert_windows_private_acl(descendant)
    if os.name != "nt":
        yield descendant
        return
    authority = _WindowsAclAuthority()
    authority.assert_fixed_non_reparse_chain(descendant)
    current = parent
    components: list[Path] = []
    for part in relative.parts:
        current /= part
        components.append(current)
    with ExitStack() as stack:
        for index, component in enumerate(components):
            assert_windows_private_acl(component)
            stack.enter_context(
                _hold_windows_directory_component(
                    component,
                    allow_child_writes=(
                        allow_leaf_child_writes
                        if index == len(components) - 1
                        else True
                    ),
                    request_delete=True,
                )
            )
        if parent_authority.root != parent:
            raise FormalWindowsPretrustError(
                "Formal Windows private parent path authority发生rebound"
            )
        yield descendant
        if parent_authority.root != parent:
            raise FormalWindowsPretrustError(
                "Formal Windows private parent path authority发生rebound"
            )


@contextmanager
def _hold_windows_file(
    path: Path,
    *,
    allow_trusted_installer_owner: bool,
    allow_trusted_installer_writer: bool = False,
) -> Iterator[Path]:
    path = Path(path)
    _assert_windows_acl(
        path,
        allow_trusted_installer_owner=allow_trusted_installer_owner,
        allow_trusted_installer_writer=allow_trusted_installer_writer,
        allow_hardlinked_file=allow_trusted_installer_owner,
    )
    if not path.is_file() or path.is_symlink():
        raise FormalWindowsPretrustError(
            "Formal Windows private authority input不是普通文件"
        )
    if os.name != "nt":
        yield path
        return
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    _declare_windows_handle_identity(kernel32)
    before_identity = _windows_path_identity(path)
    handle = kernel32.CreateFileW(
        str(path),
        0x00000001 | 0x00000080,  # FILE_READ_DATA | READ_ATTRIBUTES
        0x00000001,  # SHARE_READ only
        None,
        3,
        0x00200000,  # OPEN_REPARSE_POINT
        None,
    )
    invalid = ctypes.c_void_p(-1).value
    if handle in (None, 0, invalid):
        raise FormalWindowsPretrustError(
            "Formal Windows private authority input handle不可用"
        )
    try:
        handle_identity = _windows_handle_identity(kernel32, handle)
        if not (
            before_identity
            == handle_identity
            == _windows_path_identity(path)
        ):
            raise FormalWindowsPretrustError(
                "Formal Windows private authority input handle发生rebound"
            )
        _assert_windows_acl(
            path,
            allow_trusted_installer_owner=allow_trusted_installer_owner,
            allow_trusted_installer_writer=allow_trusted_installer_writer,
            allow_hardlinked_file=allow_trusted_installer_owner,
        )
        yield path
        _assert_windows_acl(
            path,
            allow_trusted_installer_owner=allow_trusted_installer_owner,
            allow_trusted_installer_writer=allow_trusted_installer_writer,
            allow_hardlinked_file=allow_trusted_installer_owner,
        )
        if handle_identity != _windows_path_identity(path):
            raise FormalWindowsPretrustError(
                "Formal Windows private authority input path发生rebound"
            )
    finally:
        if not kernel32.CloseHandle(handle):
            raise FormalWindowsPretrustError(
                "Formal Windows private authority input handle释放失败"
            )


@contextmanager
def hold_windows_private_file(path: Path) -> Iterator[Path]:
    """Hold one completed authority input without WRITE/DELETE sharing."""

    with _hold_windows_file(
        path, allow_trusted_installer_owner=False
    ) as held:
        yield held


@contextmanager
def hold_windows_audited_tool(path: Path) -> Iterator[Path]:
    """Hold an installed Windows tool and its complete path-resolution chain.

    TrustedInstaller may own a system directory or executable, but it is never
    accepted as a writer.  Each ancestor is file-ID bound and held without
    ``FILE_SHARE_DELETE``; the executable is additionally held without write
    or delete sharing while remaining readable/executable by CreateProcess.
    The caller must separately bind the exact expected tool digest.
    """

    path = Path(path)
    _assert_windows_acl(
        path,
        allow_trusted_installer_owner=True,
        allow_hardlinked_file=True,
    )
    if not path.is_file() or path.is_symlink():
        raise FormalWindowsPretrustError(
            "Formal Windows audited tool不是普通文件"
        )
    if os.name != "nt":
        yield path
        return
    authority = _WindowsAclAuthority()
    authority.assert_fixed_non_reparse_chain(path)
    parent = path.parent
    current = Path(parent.anchor)
    components = [current]
    for part in parent.parts[1:]:
        current /= part
        components.append(current)
    with ExitStack() as stack:
        for component in components:
            try:
                stack.enter_context(
                    _hold_windows_directory_component(
                        component,
                        allow_child_writes=True,
                        request_delete=True,
                    )
                )
            except _WindowsDirectoryDeleteAccessDenied:
                if component != Path(path.anchor):
                    authority.inspect_acl(
                        component,
                        reject_current_mutation=True,
                        allow_trusted_installer_owner=True,
                        mutation_mask=_WINDOWS_REBIND_MASK,
                    )
                stack.enter_context(
                    _hold_windows_directory_component(
                        component,
                        allow_child_writes=True,
                        request_delete=False,
                    )
                )
        stack.enter_context(
            _hold_windows_file(
                path, allow_trusted_installer_owner=True
            )
        )
        authority.assert_fixed_non_reparse_chain(path)
        _assert_windows_acl(
            path,
            allow_trusted_installer_owner=True,
            allow_hardlinked_file=True,
        )
        yield path
        authority.assert_fixed_non_reparse_chain(path)
        _assert_windows_acl(
            path,
            allow_trusted_installer_owner=True,
            allow_hardlinked_file=True,
        )


def _require_windows_system32_tool(path: Path) -> None:
    if os.name != "nt":
        raise FormalWindowsPretrustError(
            "Formal audited system tool仅支持Windows"
        )
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetWindowsDirectoryW.argtypes = (
        wintypes.LPWSTR,
        wintypes.UINT,
    )
    kernel32.GetWindowsDirectoryW.restype = wintypes.UINT
    buffer = ctypes.create_unicode_buffer(32768)
    length = kernel32.GetWindowsDirectoryW(buffer, len(buffer))
    if length == 0 or length >= len(buffer):
        raise FormalWindowsPretrustError(
            "Formal Windows system root不可验证"
        )
    system32 = Path(buffer.value) / "System32"
    allowed = {
        system32 / "robocopy.exe",
        system32 / "OpenSSH" / "ssh.exe",
        system32 / "OpenSSH" / "scp.exe",
        system32 / "OpenSSH" / "ssh-keygen.exe",
        system32 / "libcrypto.dll",
    }
    normalized = os.path.normcase(os.path.abspath(path))
    if not path.is_absolute() or normalized not in {
        os.path.normcase(os.path.abspath(candidate)) for candidate in allowed
    }:
        raise FormalWindowsPretrustError(
            "Formal audited system tool路径不在固定allowlist"
        )


def _read_expected_pe(path: Path, *, expected_sha256: str) -> bytes:
    if not _is_digest(expected_sha256):
        raise FormalWindowsPretrustError(
            "Formal audited system tool identity无效"
        )
    try:
        size = path.stat().st_size
        if not 1 <= size <= _MAX_FILE_BYTES:
            raise FormalWindowsPretrustError(
                "Formal audited system tool大小无效"
            )
        value = path.read_bytes()
    except OSError as error:
        raise FormalWindowsPretrustError(
            "Formal audited system tool不可读"
        ) from error
    if len(value) != size or _digest(value) != expected_sha256:
        raise FormalWindowsPretrustError(
            "Formal audited system tool identity不一致"
        )
    _require_pe32_plus_amd64(value)
    return value


@contextmanager
def hold_windows_audited_system_tool_source(
    path: Path, *, expected_sha256: str
) -> Iterator[Path]:
    """Hold one exact System32 source tool while validating pinned PE bytes.

    This is the only ACL mode that accepts TrustedInstaller as a writer.  The
    exception is restricted to robocopy, the three required OpenSSH tools, or
    their pinned System32 libcrypto runtime,
    an exact caller-pinned digest and a held source file with neither write nor
    delete sharing.
    Consumers execute a strict private snapshot, never this source path.
    """

    path = Path(path)
    _require_windows_system32_tool(path)
    _assert_windows_acl(
        path,
        allow_trusted_installer_owner=True,
        allow_trusted_installer_writer=True,
        allow_hardlinked_file=True,
    )
    authority = _WindowsAclAuthority()
    authority.assert_fixed_non_reparse_chain(path)
    current = Path(path.anchor)
    components = [current]
    for part in path.parent.parts[1:]:
        current /= part
        components.append(current)
    with ExitStack() as stack:
        for component in components:
            try:
                stack.enter_context(
                    _hold_windows_directory_component(
                        component,
                        allow_child_writes=True,
                        request_delete=True,
                    )
                )
            except _WindowsDirectoryDeleteAccessDenied:
                if component != Path(path.anchor):
                    authority.inspect_acl(
                        component,
                        reject_current_mutation=True,
                        allow_trusted_installer_owner=True,
                        allow_trusted_installer_writer=True,
                        mutation_mask=_WINDOWS_REBIND_MASK,
                    )
                stack.enter_context(
                    _hold_windows_directory_component(
                        component,
                        allow_child_writes=True,
                        request_delete=False,
                    )
                )
        stack.enter_context(
            _hold_windows_file(
                path,
                allow_trusted_installer_owner=True,
                allow_trusted_installer_writer=True,
            )
        )
        before = _read_expected_pe(path, expected_sha256=expected_sha256)
        yield path
        after = _read_expected_pe(path, expected_sha256=expected_sha256)
        if after != before:
            raise FormalWindowsPretrustError(
                "Formal audited system tool持有期间发生变化"
            )


@contextmanager
def hold_windows_system_tool_private_snapshot(
    source: Path,
    *,
    expected_sha256: str,
    private_root: Path,
    destination_name: str,
    root_already_held: bool = False,
) -> Iterator[Path]:
    """O_EXCL-copy, revalidate and hold a System32 PE in a private snapshot."""

    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}\.exe", destination_name):
        raise FormalWindowsPretrustError(
            "Formal system tool snapshot名称无效"
        )
    private_root = Path(private_root)
    assert_windows_private_acl(private_root)
    destination = private_root / destination_name
    if destination.exists() or destination.is_symlink():
        raise FormalWindowsPretrustError(
            "Formal system tool snapshot目标必须不存在"
        )
    with ExitStack() as stack:
        if not root_already_held:
            stack.enter_context(
                hold_windows_private_path_chain(
                    private_root, allow_leaf_child_writes=True
                )
            )
        else:
            assert_windows_private_acl(private_root)
        source_path = stack.enter_context(
            hold_windows_audited_system_tool_source(
                source, expected_sha256=expected_sha256
            )
        )
        source_value = _read_expected_pe(
            source_path, expected_sha256=expected_sha256
        )
        descriptor = -1
        try:
            descriptor = os.open(
                destination,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_BINARY", 0),
                0o700,
            )
            view = memoryview(source_value)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise OSError("short write")
                view = view[written:]
        except OSError as error:
            raise FormalWindowsPretrustError(
                "Formal system tool snapshot不可创建"
            ) from error
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        assert_windows_private_acl(destination)
        staged_value = _read_closed_file(
            destination, label="Formal system tool snapshot"
        )
        if staged_value != source_value or _digest(staged_value) != expected_sha256:
            raise FormalWindowsPretrustError(
                "Formal system tool snapshot identity不一致"
            )
        _require_pe32_plus_amd64(staged_value)
        stack.enter_context(hold_windows_private_file(destination))
        yield destination
        if (
            _read_closed_file(
                destination, label="Formal system tool snapshot"
            )
            != staged_value
        ):
            raise FormalWindowsPretrustError(
                "Formal system tool snapshot执行期间发生变化"
            )


@contextmanager
def hold_windows_system_tool_private_bundle(
    sources: Mapping[str, tuple[Path, str]],
    *,
    private_root: Path,
) -> Iterator[Mapping[str, Path]]:
    """Stage and hold all fixed System32 provider tools in one private root.

    Public sources are acquired sequentially so their shared System32 path
    chain is never double-opened with incompatible delete sharing.  Each
    private destination is held before its public source handle is released.
    """

    names = _validate_flat_file_inventory(
        tuple(sorted(sources)), label="Formal system tool bundle"
    )
    if any(
        type(source) is not tuple
        or len(source) != 2
        or not _is_digest(source[1])
        for source in sources.values()
    ):
        raise FormalWindowsPretrustError(
            "Formal system tool bundle identity无效"
        )
    private_root = Path(private_root)
    assert_windows_private_acl(private_root)
    if tuple(private_root.iterdir()):
        raise FormalWindowsPretrustError(
            "Formal system tool bundle private root必须为空"
        )
    destinations: dict[str, Path] = {}
    with ExitStack() as stack:
        stack.enter_context(
            hold_windows_private_directory(
                private_root, allow_child_writes=True
            )
        )
        for name in names:
            source, expected_sha256 = sources[name]
            with hold_windows_audited_system_tool_source(
                Path(source), expected_sha256=expected_sha256
            ) as held_source:
                source_value = _read_expected_pe(
                    held_source, expected_sha256=expected_sha256
                )
                destination = private_root / name
                descriptor = -1
                try:
                    descriptor = os.open(
                        destination,
                        os.O_WRONLY
                        | os.O_CREAT
                        | os.O_EXCL
                        | getattr(os, "O_BINARY", 0),
                        0o700,
                    )
                    view = memoryview(source_value)
                    while view:
                        written = os.write(descriptor, view)
                        if written <= 0:
                            raise OSError("short write")
                        view = view[written:]
                except OSError as error:
                    raise FormalWindowsPretrustError(
                        "Formal system tool bundle snapshot不可创建"
                    ) from error
                finally:
                    if descriptor >= 0:
                        os.close(descriptor)
                assert_windows_private_acl(destination)
                observed = _read_closed_file(
                    destination, label="Formal system tool bundle snapshot"
                )
                if observed != source_value or _digest(observed) != expected_sha256:
                    raise FormalWindowsPretrustError(
                        "Formal system tool bundle snapshot identity不一致"
                    )
                stack.enter_context(hold_windows_private_file(destination))
                destinations[name] = destination
        if tuple(sorted(path.name for path in private_root.iterdir())) != names:
            raise FormalWindowsPretrustError(
                "Formal system tool bundle inventory不一致"
            )
        yield dict(destinations)
        if tuple(sorted(path.name for path in private_root.iterdir())) != names:
            raise FormalWindowsPretrustError(
                "Formal system tool bundle execution inventory发生变化"
            )
        for name in names:
            if (
                _digest(
                    _read_closed_file(
                        destinations[name],
                        label="Formal system tool bundle post-execution",
                    )
                )
                != sources[name][1]
            ):
                raise FormalWindowsPretrustError(
                    "Formal system tool bundle execution identity发生变化"
                )


def _validate_flat_file_inventory(
    relative_files: tuple[str, ...], *, label: str
) -> tuple[str, ...]:
    if (
        not relative_files
        or len(relative_files) != len(set(relative_files))
        or tuple(sorted(relative_files)) != relative_files
        or any(
            re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", name) is None
            or name in {".", ".."}
            for name in relative_files
        )
    ):
        raise FormalWindowsPretrustError(f"{label}文件集无效")
    return relative_files


@contextmanager
def _hold_windows_fixed_path_chain(root: Path) -> Iterator[Path]:
    """Hold a mutable fixed-drive source chain by ID without trusting its ACL."""

    root = Path(root)
    if not root.is_absolute() or not root.is_dir() or root.is_symlink():
        raise FormalWindowsPretrustError("Formal Windows fixed source root无效")
    if os.name != "nt":
        yield root
        return
    authority = _WindowsAclAuthority()
    authority.assert_fixed_non_reparse_chain(root)
    current = Path(root.anchor)
    components = [current]
    for part in root.parts[1:]:
        current /= part
        components.append(current)
    with ExitStack() as stack:
        for component in components:
            # A mutable installed/source tree is not accepted through the ACL
            # fallback.  DELETE access is required so every component can be
            # held without FILE_SHARE_DELETE for the complete operation.
            try:
                stack.enter_context(
                    _hold_windows_directory_component(
                        component,
                        allow_child_writes=True,
                        request_delete=True,
                    )
                )
            except _WindowsDirectoryDeleteAccessDenied:
                if component != Path(root.anchor):
                    authority.inspect_acl(
                        component,
                        reject_current_mutation=True,
                        allow_trusted_installer_owner=True,
                        mutation_mask=_WINDOWS_REBIND_MASK,
                    )
                # A mounted fixed-volume root itself cannot be renamed.  A
                # non-root fallback is accepted only when its DACL proves that
                # neither the current token nor any nontrusted SID can rebind
                # it; retain the component's file-ID handle either way.
                stack.enter_context(
                    _hold_windows_directory_component(
                        component,
                        allow_child_writes=True,
                        request_delete=False,
                    )
                )
        authority.assert_fixed_non_reparse_chain(root)
        yield root
        authority.assert_fixed_non_reparse_chain(root)


@contextmanager
def _hold_windows_fixed_source_file(path: Path) -> Iterator[Path]:
    """Hold one exact fixed-drive source file while allowing read consumers."""

    path = Path(path)
    try:
        before = path.lstat()
    except OSError as error:
        raise FormalWindowsPretrustError(
            "Formal Windows fixed source file不可用"
        ) from error
    if (
        not path.is_absolute()
        or path.is_symlink()
        or _is_reparse(before)
        or not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
    ):
        raise FormalWindowsPretrustError(
            "Formal Windows fixed source file不是封闭普通文件"
        )
    if os.name != "nt":
        yield path
        return
    authority = _WindowsAclAuthority()
    authority.assert_fixed_non_reparse_chain(path)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    _declare_windows_handle_identity(kernel32)
    before_identity = _windows_path_identity(path)
    handle = kernel32.CreateFileW(
        str(path),
        0x00000001 | 0x00000080,  # READ_DATA | READ_ATTRIBUTES
        0x00000001,  # SHARE_READ only: block write/delete/rebind
        None,
        3,
        0x00200000,  # OPEN_REPARSE_POINT
        None,
    )
    invalid = ctypes.c_void_p(-1).value
    if handle in (None, 0, invalid):
        raise FormalWindowsPretrustError(
            "Formal Windows fixed source file handle不可用"
        )
    try:
        handle_identity = _windows_handle_identity(kernel32, handle)
        if not (
            before_identity == handle_identity == _windows_path_identity(path)
        ):
            raise FormalWindowsPretrustError(
                "Formal Windows fixed source file发生rebound"
            )
        yield path
        if handle_identity != _windows_path_identity(path):
            raise FormalWindowsPretrustError(
                "Formal Windows fixed source file持有期间发生rebound"
            )
    finally:
        if not kernel32.CloseHandle(handle):
            raise FormalWindowsPretrustError(
                "Formal Windows fixed source file handle释放失败"
            )


def _flat_regular_inventory(root: Path) -> tuple[str, ...]:
    try:
        entries = tuple(root.iterdir())
    except OSError as error:
        raise FormalWindowsPretrustError(
            "Formal Windows fixed source inventory不可用"
        ) from error
    names: list[str] = []
    for entry in entries:
        try:
            metadata = entry.lstat()
        except OSError as error:
            raise FormalWindowsPretrustError(
                "Formal Windows fixed source inventory不可用"
            ) from error
        if (
            entry.is_symlink()
            or _is_reparse(metadata)
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
        ):
            raise FormalWindowsPretrustError(
                "Formal Windows fixed source inventory不封闭"
            )
        names.append(entry.name)
    return tuple(sorted(names))


@contextmanager
def hold_windows_fixed_source_snapshot(
    root: Path, *, relative_files: tuple[str, ...]
) -> Iterator[WindowsFixedSourceSnapshot]:
    """Hold an exact flat source VM inventory for a complete provider run.

    This mode intentionally does not trust the source DACL.  It requires a
    fixed non-reparse volume and holds every declared file without write/delete
    sharing.  Mutable ancestors are FileId-checked before/after rather than
    trusted; exact private copying later uses only the authoritative file list.
    """

    if (
        not relative_files
        or len(relative_files) != len(set(relative_files))
        or tuple(sorted(relative_files)) != relative_files
        or any(
            type(name) is not str
            or not name
            or len(name) > 255
            or Path(name).name != name
            or name in {".", ".."}
            or "\0" in name
            for name in relative_files
        )
    ):
        raise FormalWindowsPretrustError(
            "Formal Windows fixed source文件集无效"
        )
    root = Path(root)
    if not root.is_absolute() or not root.is_dir() or root.is_symlink():
        raise FormalWindowsPretrustError(
            "Formal Windows fixed source root无效"
        )
    authority = _WindowsAclAuthority() if os.name == "nt" else None
    if authority is not None:
        authority.assert_fixed_non_reparse_chain(root)
    root_identity = _windows_path_identity(root)
    with ExitStack() as stack:
        if _flat_regular_inventory(root) != relative_files:
            raise FormalWindowsPretrustError(
                "Formal Windows fixed source inventory不一致"
            )
        for name in relative_files:
            stack.enter_context(_hold_windows_fixed_source_file(root / name))
        if _flat_regular_inventory(root) != relative_files:
            raise FormalWindowsPretrustError(
                "Formal Windows fixed source inventory发生变化"
            )
        yield WindowsFixedSourceSnapshot(root=root, relative_files=relative_files)
        if authority is not None:
            authority.assert_fixed_non_reparse_chain(root)
        if root_identity != _windows_path_identity(root):
            raise FormalWindowsPretrustError(
                "Formal Windows fixed source root发生rebound"
            )
        if _flat_regular_inventory(root) != relative_files:
            raise FormalWindowsPretrustError(
                "Formal Windows fixed source inventory持有期间发生变化"
            )


def _validate_unicode_flat_names(
    names: tuple[str, ...], *, label: str, maximum: int
) -> tuple[str, ...]:
    if (
        not names
        or len(names) > maximum
        or len(names) != len(set(names))
        or tuple(sorted(names)) != names
        or any(
            type(name) is not str
            or not name
            or len(name) > 255
            or Path(name).name != name
            or name in {".", ".."}
            or "\0" in name
            for name in names
        )
    ):
        raise FormalWindowsPretrustError(f"{label}文件集无效")
    return names


def _hash_stream(path: Path, *, maximum_bytes: int) -> tuple[str, int]:
    digest = hashlib.sha256()
    total = 0
    try:
        with path.open("rb") as stream:
            while True:
                chunk = stream.read(4 * 1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > maximum_bytes:
                    raise FormalWindowsPretrustError(
                        "Formal Windows source snapshot文件超限"
                    )
                digest.update(chunk)
    except OSError as error:
        raise FormalWindowsPretrustError(
            "Formal Windows source snapshot文件不可读"
        ) from error
    if total < 1:
        raise FormalWindowsPretrustError(
            "Formal Windows source snapshot文件为空"
        )
    return _DIGEST_PREFIX + digest.hexdigest(), total


@contextmanager
def hold_windows_private_source_snapshot(
    source_root: Path,
    *,
    source_inventory: tuple[str, ...],
    expected_file_identities: Mapping[str, str],
    private_root: Path,
    maximum_file_bytes: int = 2 * 1024 * 1024 * 1024 * 1024,
    source_already_held: bool = False,
) -> Iterator[WindowsPrivateSourceSnapshot]:
    """Copy only an explicit digest-pinned VM source set into a private root.

    Every public-source file present at qualification time is held, but only
    the explicit expected identity mapping is copied.  A transient child added
    to the broad-writable public source therefore cannot enter the private VM
    authority used by clone operations.
    """

    source_inventory = _validate_unicode_flat_names(
        source_inventory,
        label="Formal Windows public source inventory",
        maximum=8192,
    )
    names = _validate_unicode_flat_names(
        tuple(sorted(expected_file_identities)),
        label="Formal Windows private source",
        maximum=64,
    )
    if (
        not set(names).issubset(source_inventory)
        or any(not _is_digest(expected_file_identities[name]) for name in names)
        or isinstance(maximum_file_bytes, bool)
        or not 1 <= maximum_file_bytes <= 4 * 1024 * 1024 * 1024 * 1024
    ):
        raise FormalWindowsPretrustError(
            "Formal Windows private source identity无效"
        )
    source_root = Path(source_root)
    private_root = Path(private_root)
    assert_windows_private_acl(private_root)
    if tuple(private_root.iterdir()):
        raise FormalWindowsPretrustError(
            "Formal Windows private source root必须为空"
        )
    sizes: dict[str, int] = {}
    with ExitStack() as stack:
        stack.enter_context(
            hold_windows_private_directory(
                private_root, allow_child_writes=True
            )
        )
        if not source_already_held:
            stack.enter_context(_hold_windows_fixed_path_chain(source_root))
        if _flat_regular_inventory(source_root) != source_inventory:
            raise FormalWindowsPretrustError(
                "Formal Windows public source inventory不一致"
            )
        held_sources = (
            {name: source_root / name for name in source_inventory}
            if source_already_held
            else {
                name: stack.enter_context(
                    _hold_windows_fixed_source_file(source_root / name)
                )
                for name in source_inventory
            }
        )
        if _flat_regular_inventory(source_root) != source_inventory:
            raise FormalWindowsPretrustError(
                "Formal Windows public source inventory发生变化"
            )
        for name in names:
            source = held_sources[name]
            destination = private_root / name
            descriptor = -1
            digest = hashlib.sha256()
            total = 0
            try:
                descriptor = os.open(
                    destination,
                    os.O_WRONLY
                    | os.O_CREAT
                    | os.O_EXCL
                    | getattr(os, "O_BINARY", 0),
                    0o600,
                )
                with source.open("rb") as stream:
                    while True:
                        chunk = stream.read(4 * 1024 * 1024)
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > maximum_file_bytes:
                            raise FormalWindowsPretrustError(
                                "Formal Windows private source文件超限"
                            )
                        digest.update(chunk)
                        view = memoryview(chunk)
                        while view:
                            written = os.write(descriptor, view)
                            if written <= 0:
                                raise OSError("short write")
                            view = view[written:]
            except FormalWindowsPretrustError:
                raise
            except OSError as error:
                raise FormalWindowsPretrustError(
                    "Formal Windows private source snapshot不可创建"
                ) from error
            finally:
                if descriptor >= 0:
                    os.close(descriptor)
            observed = _DIGEST_PREFIX + digest.hexdigest()
            if total < 1 or observed != expected_file_identities[name]:
                raise FormalWindowsPretrustError(
                    "Formal Windows public source identity不一致"
                )
            assert_windows_private_acl(destination)
            destination_digest, destination_size = _hash_stream(
                destination, maximum_bytes=maximum_file_bytes
            )
            if destination_digest != observed or destination_size != total:
                raise FormalWindowsPretrustError(
                    "Formal Windows private source copy identity不一致"
                )
            sizes[name] = total
            stack.enter_context(hold_windows_private_file(destination))
        if tuple(sorted(path.name for path in private_root.iterdir())) != names:
            raise FormalWindowsPretrustError(
                "Formal Windows private source inventory不一致"
            )
        aggregate = _digest(
            _canonical_json_bytes(
                {
                    "files": [
                        {
                            "name": name,
                            "sha256": expected_file_identities[name],
                            "size": sizes[name],
                        }
                        for name in names
                    ],
                    "schema": "animemo.windows-private-source-snapshot/v1",
                    "version": 1,
                }
            )
        )
        yield WindowsPrivateSourceSnapshot(
            root=private_root,
            file_identities=dict(expected_file_identities),
            aggregate_identity=aggregate,
        )
        if (
            _flat_regular_inventory(source_root) != source_inventory
            or tuple(sorted(path.name for path in private_root.iterdir())) != names
        ):
            raise FormalWindowsPretrustError(
                "Formal Windows source snapshot持有期间发生变化"
            )
        for name in names:
            observed_digest, observed_size = _hash_stream(
                private_root / name, maximum_bytes=maximum_file_bytes
            )
            if (
                observed_digest != expected_file_identities[name]
                or observed_size != sizes[name]
            ):
                raise FormalWindowsPretrustError(
                    "Formal Windows private source execution identity发生变化"
                )


def _validate_private_tree_file_identities(
    expected_file_identities: Mapping[str, str],
    *,
    maximum_files: int,
) -> tuple[str, ...]:
    if (
        isinstance(maximum_files, bool)
        or not 1 <= maximum_files <= 20000
        or not expected_file_identities
        or len(expected_file_identities) > maximum_files
    ):
        raise FormalWindowsPretrustError(
            "Formal Windows private tree identity无效"
        )
    names = tuple(sorted(expected_file_identities))
    folded: set[str] = set()
    for name in names:
        if type(name) is not str or not _is_digest(expected_file_identities[name]):
            raise FormalWindowsPretrustError(
                "Formal Windows private tree identity无效"
            )
        path = Path(name)
        if (
            path.is_absolute()
            or "\\" in name
            or len(name.encode("utf-8")) > 1024
            or len(path.parts) > 16
            or any(
                not part
                or part in {".", ".."}
                or "\0" in part
                or len(part) > 255
                for part in path.parts
            )
            or path.as_posix() != name
            or name.casefold() in folded
        ):
            raise FormalWindowsPretrustError(
                "Formal Windows private tree path无效"
            )
        folded.add(name.casefold())
    return names


def _private_tree_inventory(root: Path) -> tuple[tuple[str, ...], tuple[str, ...]]:
    try:
        boundary = root.resolve(strict=True)
        paths = tuple(
            sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix())
        )
    except (OSError, ValueError) as error:
        raise FormalWindowsPretrustError(
            "Formal Windows private tree inventory不可用"
        ) from error
    files: list[str] = []
    directories: list[str] = []
    for path in paths:
        try:
            metadata = path.lstat()
            relative = path.relative_to(boundary).as_posix()
        except (OSError, ValueError) as error:
            raise FormalWindowsPretrustError(
                "Formal Windows private tree inventory不可用"
            ) from error
        if path.is_symlink() or _is_reparse(metadata):
            raise FormalWindowsPretrustError(
                "Formal Windows private tree inventory不封闭"
            )
        if stat.S_ISDIR(metadata.st_mode):
            directories.append(relative)
        elif stat.S_ISREG(metadata.st_mode) and metadata.st_nlink == 1:
            files.append(relative)
        else:
            raise FormalWindowsPretrustError(
                "Formal Windows private tree inventory不封闭"
            )
    return tuple(files), tuple(directories)


@contextmanager
def hold_windows_private_tree_snapshot(
    source_root: Path,
    *,
    expected_file_identities: Mapping[str, str],
    private_root: Path,
    maximum_files: int = 4096,
    maximum_file_bytes: int = 4 * 1024 * 1024 * 1024,
    maximum_total_bytes: int = 20 * 1024 * 1024 * 1024,
) -> Iterator[WindowsPrivateTreeSnapshot]:
    """Copy only a caller-pinned recursive file set and hold its private copy.

    Public directory enumeration is never an authority: every copied path is
    supplied by a previously verified closed contract, every source leaf is
    opened without write/delete sharing, and an unexpected public child is
    ignored.  The exact private directory/file inventory is held and checked
    again after the consumer finishes.
    """

    names = _validate_private_tree_file_identities(
        expected_file_identities, maximum_files=maximum_files
    )
    if (
        isinstance(maximum_file_bytes, bool)
        or isinstance(maximum_total_bytes, bool)
        or not 1 <= maximum_file_bytes <= 16 * 1024 * 1024 * 1024
        or not maximum_file_bytes <= maximum_total_bytes <= 64 * 1024 * 1024 * 1024
    ):
        raise FormalWindowsPretrustError(
            "Formal Windows private tree size authority无效"
        )
    source_root = Path(source_root)
    private_root = Path(private_root)
    if (
        not source_root.is_absolute()
        or not source_root.is_dir()
        or source_root.is_symlink()
    ):
        raise FormalWindowsPretrustError(
            "Formal Windows private tree source无效"
        )
    assert_windows_private_acl(private_root)
    if tuple(private_root.iterdir()):
        raise FormalWindowsPretrustError(
            "Formal Windows private tree root必须为空"
        )
    directory_names = tuple(
        sorted(
            {
                Path(*Path(name).parts[:index]).as_posix()
                for name in names
                for index in range(1, len(Path(name).parts))
            },
            key=lambda item: (len(Path(item).parts), item),
        )
    )
    sizes: dict[str, int] = {}
    source_paths: dict[str, Path] = {}
    with ExitStack() as stack:
        stack.enter_context(
            hold_windows_private_directory(private_root, allow_child_writes=True)
        )
        for name in names:
            source_paths[name] = stack.enter_context(
                _hold_windows_fixed_source_file(source_root.joinpath(*Path(name).parts))
            )
        for directory_name in directory_names:
            directory = private_root.joinpath(*Path(directory_name).parts)
            try:
                directory.mkdir()
            except OSError as error:
                raise FormalWindowsPretrustError(
                    "Formal Windows private tree directory不可创建"
                ) from error
            assert_windows_private_acl(directory)
        total_size = 0
        for name in names:
            source = source_paths[name]
            destination = private_root.joinpath(*Path(name).parts)
            descriptor = -1
            digest = hashlib.sha256()
            file_size = 0
            try:
                descriptor = os.open(
                    destination,
                    os.O_WRONLY
                    | os.O_CREAT
                    | os.O_EXCL
                    | getattr(os, "O_BINARY", 0),
                    0o600,
                )
                with source.open("rb") as stream:
                    while True:
                        chunk = stream.read(4 * 1024 * 1024)
                        if not chunk:
                            break
                        file_size += len(chunk)
                        total_size += len(chunk)
                        if (
                            file_size > maximum_file_bytes
                            or total_size > maximum_total_bytes
                        ):
                            raise FormalWindowsPretrustError(
                                "Formal Windows private tree文件超限"
                            )
                        digest.update(chunk)
                        view = memoryview(chunk)
                        while view:
                            written = os.write(descriptor, view)
                            if written <= 0:
                                raise OSError("short write")
                            view = view[written:]
            except FormalWindowsPretrustError:
                raise
            except OSError as error:
                raise FormalWindowsPretrustError(
                    "Formal Windows private tree snapshot不可创建"
                ) from error
            finally:
                if descriptor >= 0:
                    os.close(descriptor)
            observed = _DIGEST_PREFIX + digest.hexdigest()
            if observed != expected_file_identities[name]:
                raise FormalWindowsPretrustError(
                    "Formal Windows private tree source identity不一致"
                )
            sizes[name] = file_size
            assert_windows_private_acl(destination)
            destination_digest, destination_size = _hash_stream_allow_empty(
                destination, maximum_bytes=maximum_file_bytes
            )
            if destination_digest != observed or destination_size != file_size:
                raise FormalWindowsPretrustError(
                    "Formal Windows private tree copy identity不一致"
                )
        observed_files, observed_directories = _private_tree_inventory(private_root)
        if observed_files != names or set(observed_directories) != set(directory_names):
            raise FormalWindowsPretrustError(
                "Formal Windows private tree inventory不一致"
            )
        for directory_name in reversed(directory_names):
            stack.enter_context(
                hold_windows_private_directory(
                    private_root.joinpath(*Path(directory_name).parts),
                    allow_child_writes=True,
                )
            )
        for name in names:
            stack.enter_context(
                hold_windows_private_file(
                    private_root.joinpath(*Path(name).parts)
                )
            )
        aggregate = _digest(
            _canonical_json_bytes(
                {
                    "files": [
                        {
                            "path": name,
                            "sha256": expected_file_identities[name],
                            "size": sizes[name],
                        }
                        for name in names
                    ],
                    "schema": "animemo.windows-private-tree-snapshot/v1",
                    "version": 1,
                }
            )
        )
        yield WindowsPrivateTreeSnapshot(
            root=private_root,
            file_identities=dict(expected_file_identities),
            aggregate_identity=aggregate,
        )
        observed_files, observed_directories = _private_tree_inventory(private_root)
        if observed_files != names or set(observed_directories) != set(directory_names):
            raise FormalWindowsPretrustError(
                "Formal Windows private tree execution inventory发生变化"
            )
        for name in names:
            observed_digest, observed_size = _hash_stream_allow_empty(
                private_root.joinpath(*Path(name).parts),
                maximum_bytes=maximum_file_bytes,
            )
            if (
                observed_digest != expected_file_identities[name]
                or observed_size != sizes[name]
            ):
                raise FormalWindowsPretrustError(
                    "Formal Windows private tree execution identity发生变化"
                )


def _hash_stream_allow_empty(path: Path, *, maximum_bytes: int) -> tuple[str, int]:
    digest = hashlib.sha256()
    total = 0
    try:
        with path.open("rb") as stream:
            while True:
                chunk = stream.read(4 * 1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > maximum_bytes:
                    raise FormalWindowsPretrustError(
                        "Formal Windows private tree文件超限"
                    )
                digest.update(chunk)
    except OSError as error:
        raise FormalWindowsPretrustError(
            "Formal Windows private tree文件不可读"
        ) from error
    return _DIGEST_PREFIX + digest.hexdigest(), total


@contextmanager
def hold_windows_private_tool_bundle_snapshot(
    source_root: Path,
    *,
    expected_file_identities: Mapping[str, str],
    expected_pe_machines: Mapping[str, int],
    executable_name: str,
    private_root: Path,
    root_already_held: bool = False,
) -> Iterator[WindowsPrivateToolBundle]:
    """Copy and hold a digest-pinned local PE dependency closure.

    The installed source tree may be mutable.  It is never executed: every
    member is held, read, digest/PE checked, O_EXCL-copied into an empty
    private directory, rechecked, then the exact private inventory is held for
    the consumer's complete subprocess lifecycle.
    """

    names = _validate_flat_file_inventory(
        tuple(sorted(expected_file_identities)),
        label="Formal Windows tool bundle",
    )
    if (
        executable_name not in names
        or executable_name not in expected_pe_machines
        or not set(expected_pe_machines).issubset(names)
        or any(
            machine not in {0x014C, 0x8664}
            for machine in expected_pe_machines.values()
        )
        or any(not _is_digest(expected_file_identities[name]) for name in names)
    ):
        raise FormalWindowsPretrustError(
            "Formal Windows tool bundle identity无效"
        )
    source_root = Path(source_root)
    private_root = Path(private_root)
    assert_windows_private_acl(private_root)
    if tuple(private_root.iterdir()):
        raise FormalWindowsPretrustError(
            "Formal Windows tool bundle private root必须为空"
        )
    values: dict[str, bytes] = {}
    with ExitStack() as stack, ExitStack() as source_stack:
        if not root_already_held:
            stack.enter_context(
                hold_windows_private_directory(
                    private_root, allow_child_writes=True
                )
            )
        else:
            assert_windows_private_acl(private_root)
        # The installed runtime root can legitimately be broad-writable.  Its
        # exact allowlisted members are never executed in place: every source
        # file is opened without write/delete sharing, bound by FileId and a
        # caller-pinned digest, then copied to the protected bundle.  Holding
        # the mutable ancestor chain would both add no byte authority and make
        # an Administrators-writable installation impossible to close.
        source_authority = _WindowsAclAuthority() if os.name == "nt" else None
        if source_authority is not None:
            source_authority.assert_fixed_non_reparse_chain(source_root)
        source_root_identity = _windows_path_identity(source_root)
        for name in names:
            source = source_stack.enter_context(
                _hold_windows_fixed_source_file(source_root / name)
            )
            try:
                size = source.stat().st_size
                if not 1 <= size <= _MAX_FILE_BYTES:
                    raise FormalWindowsPretrustError(
                        "Formal Windows tool bundle source大小无效"
                    )
                value = source.read_bytes()
            except OSError as error:
                raise FormalWindowsPretrustError(
                    "Formal Windows tool bundle source不可读"
                ) from error
            if len(value) != size or _digest(value) != expected_file_identities[name]:
                raise FormalWindowsPretrustError(
                    "Formal Windows tool bundle source identity不一致"
                )
            if name in expected_pe_machines:
                _require_pe_machine(
                    value, expected_machine=expected_pe_machines[name]
                )
            values[name] = value
        for name in names:
            destination = private_root / name
            descriptor = -1
            try:
                descriptor = os.open(
                    destination,
                    os.O_WRONLY
                    | os.O_CREAT
                    | os.O_EXCL
                    | getattr(os, "O_BINARY", 0),
                    0o700,
                )
                view = memoryview(values[name])
                while view:
                    written = os.write(descriptor, view)
                    if written <= 0:
                        raise OSError("short write")
                    view = view[written:]
            except OSError as error:
                raise FormalWindowsPretrustError(
                    "Formal Windows tool bundle snapshot不可创建"
                ) from error
            finally:
                if descriptor >= 0:
                    os.close(descriptor)
            assert_windows_private_acl(destination)
            observed = _read_closed_file(
                destination, label="Formal Windows tool bundle snapshot"
            )
            if observed != values[name]:
                raise FormalWindowsPretrustError(
                    "Formal Windows tool bundle snapshot identity不一致"
                )
        if tuple(sorted(path.name for path in private_root.iterdir())) != names:
            raise FormalWindowsPretrustError(
                "Formal Windows tool bundle snapshot inventory不一致"
            )
        stack.enter_context(
            hold_windows_private_snapshot(
                private_root,
                relative_files=names,
                root_already_held=True,
            )
        )
        if source_authority is not None:
            source_authority.assert_fixed_non_reparse_chain(source_root)
        if source_root_identity != _windows_path_identity(source_root):
            raise FormalWindowsPretrustError(
                "Formal Windows tool bundle source root发生rebound"
            )
        source_stack.close()
        aggregate = _digest(
            _canonical_json_bytes(
                {
                    "files": [
                        {
                            "name": name,
                            "sha256": expected_file_identities[name],
                            "size": len(values[name]),
                        }
                        for name in names
                    ],
                    "schema": "animemo.windows-private-tool-bundle/v1",
                    "version": 1,
                }
            )
        )
        yield WindowsPrivateToolBundle(
            root=private_root,
            executable=private_root / executable_name,
            file_identities=dict(expected_file_identities),
            aggregate_identity=aggregate,
        )
        if tuple(sorted(path.name for path in private_root.iterdir())) != names:
            raise FormalWindowsPretrustError(
                "Formal Windows tool bundle execution inventory发生变化"
            )
        for name in names:
            if (
                _digest(
                    _read_closed_file(
                        private_root / name,
                        label="Formal Windows tool bundle post-execution",
                    )
                )
                != expected_file_identities[name]
            ):
                raise FormalWindowsPretrustError(
                    "Formal Windows tool bundle execution identity发生变化"
                )


@contextmanager
def commit_windows_private_directory_snapshot(
    staging: Path,
    destination: Path,
    *,
    relative_files: tuple[str, ...],
    expected_file_identities: Mapping[str, str],
) -> Iterator[Path]:
    """Commit a closed flat snapshot while preserving every Windows file ID.

    Unlike the retired no-share-delete transaction, the child handles grant
    ``FILE_SHARE_DELETE`` but deny write sharing while the
    caller-pinned digests are fixed.  Some supported Windows builds still
    refuse a directory rename with open descendants, so those leaf handles are
    released only for the atomic rename window.  The protected DACL excludes
    nontrusted mutation; the parent worker's own token is the in-process TCB
    because it also owns the authority handles and secrets.  The destination
    is immediately reopened and fully revalidated before the caller observes it.
    """

    staging = Path(staging)
    destination = Path(destination)
    names = _validate_flat_file_inventory(
        relative_files, label="Formal Windows private commit"
    )
    if (
        set(expected_file_identities) != set(names)
        or any(
            re.fullmatch(r"sha256:[0-9a-f]{64}", identity) is None
            for identity in expected_file_identities.values()
        )
    ):
        raise FormalWindowsPretrustError(
            "Formal Windows private commit expected identity无效"
        )
    if (
        not staging.is_absolute()
        or not destination.is_absolute()
        or staging.parent != destination.parent
        or staging == destination
        or destination.exists()
        or destination.is_symlink()
    ):
        raise FormalWindowsPretrustError(
            "Formal Windows private commit参数无效"
        )
    assert_windows_private_acl(staging.parent)
    assert_windows_private_acl(staging)
    try:
        entries = tuple(sorted(staging.iterdir(), key=lambda item: item.name))
    except OSError as error:
        raise FormalWindowsPretrustError(
            "Formal Windows private commit inventory不可用"
        ) from error
    if tuple(item.name for item in entries) != names or any(
        not item.is_file() or item.is_symlink() for item in entries
    ):
        raise FormalWindowsPretrustError(
            "Formal Windows private commit inventory不一致"
        )
    expected: dict[str, tuple[tuple[int, int], str]] = {}
    for item in entries:
        observed_digest = _digest(
            _read_closed_file(item, label="Formal Windows private commit")
        )
        if observed_digest != expected_file_identities[item.name]:
            raise FormalWindowsPretrustError(
                "Formal Windows private commit expected identity不一致"
            )
        expected[item.name] = (_windows_path_identity(item), observed_digest)

    def validate_committed() -> None:
        assert_windows_private_acl(destination)
        try:
            committed = tuple(
                sorted(destination.iterdir(), key=lambda item: item.name)
            )
        except OSError as error:
            raise FormalWindowsPretrustError(
                "Formal Windows private commit readback不可用"
            ) from error
        if tuple(item.name for item in committed) != names or any(
            not item.is_file() or item.is_symlink() for item in committed
        ):
            raise FormalWindowsPretrustError(
                "Formal Windows private commit readback inventory不一致"
            )
        for item in committed:
            identity, digest = expected[item.name]
            if (
                _windows_path_identity(item) != identity
                or _digest(
                    _read_closed_file(
                        item, label="Formal Windows private commit readback"
                    )
                )
                != digest
            ):
                raise FormalWindowsPretrustError(
                    "Formal Windows private commit readback identity不一致"
                )

    if os.name != "nt":
        os.rename(staging, destination)
        validate_committed()
        yield destination
        validate_committed()
        return

    with ExitStack() as stack:
        # A legacy parent handle lacking SHARE_DELETE would itself prevent the
        # child rename.  The dedicated parent handle below grants all shares;
        # protected-DACL validation is the mutation boundary during commit.
        assert_windows_private_acl(staging.parent)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateFileW.argtypes = (
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        )
        kernel32.CreateFileW.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.SetFileInformationByHandle.argtypes = (
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        )
        kernel32.SetFileInformationByHandle.restype = wintypes.BOOL
        _declare_windows_handle_identity(kernel32)
        invalid = ctypes.c_void_p(-1).value
        handles: list[object] = []
        child_handles: list[object] = []

        def open_rename_safe(path: Path, *, directory: bool) -> object:
            handle = kernel32.CreateFileW(
                str(path),
                (0x00010000 | 0x00000080) if directory else 0x80000000,
                0x00000001,  # root/children both deny write/delete sharing
                None,
                3,
                (0x02000000 if directory else 0) | 0x00200000,
                None,
            )
            if handle in (None, 0, invalid):
                raise FormalWindowsPretrustError(
                    "Formal Windows private commit handle不可用"
                )
            handles.append(handle)
            return handle

        try:
            parent_handle = kernel32.CreateFileW(
                str(staging.parent),
                0x00000002 | 0x00000040 | 0x00000080,
                0x00000001 | 0x00000002 | 0x00000004,
                None,
                3,
                0x02000000 | 0x00200000,
                None,
            )
            if parent_handle in (None, 0, invalid):
                raise FormalWindowsPretrustError(
                    "Formal Windows private commit parent handle不可用"
                )
            handles.append(parent_handle)
            directory_handle = open_rename_safe(staging, directory=True)
            directory_identity = _windows_handle_identity(
                kernel32, directory_handle
            )
            if directory_identity != _windows_path_identity(staging):
                raise FormalWindowsPretrustError(
                    "Formal Windows private commit directory rebound"
                )
            for item in entries:
                handle = open_rename_safe(item, directory=False)
                child_handles.append(handle)
                if _windows_handle_identity(kernel32, handle) != expected[item.name][0]:
                    raise FormalWindowsPretrustError(
                        "Formal Windows private commit child rebound"
                    )

            # Windows refuses a directory rename while leaf handles are open
            # on some supported builds even when they grant SHARE_DELETE.  The
            # exact pre-commit digests are already fixed, so close only these
            # leaf handles immediately before the atomic rename and require a
            # full destination reopen/digest match immediately afterwards.
            # This does not claim isolation from the parent worker's own token;
            # that token is the TCB and nontrusted SIDs remain excluded by the
            # canonical protected DACL.
            leaf_release_failed = False
            for handle in reversed(child_handles):
                if not kernel32.CloseHandle(handle):
                    leaf_release_failed = True
                handles.remove(handle)
            child_handles.clear()
            if leaf_release_failed:
                raise FormalWindowsPretrustError(
                    "Formal Windows private commit child handle释放失败"
                )

            class FileRenameInformation(ctypes.Structure):
                _fields_ = (
                    ("replace_if_exists", ctypes.c_ubyte),
                    ("root_directory", wintypes.HANDLE),
                    ("file_name_length", wintypes.DWORD),
                    ("file_name", wintypes.WCHAR * 1),
                )

            encoded_name = str(destination).encode("utf-16-le")
            # Keep an explicit UTF-16 NUL inside the submitted allocation.
            # Some supported Windows builds read the flexible name member as
            # a terminated string despite the authoritative byte length.
            buffer_size = (
                FileRenameInformation.file_name.offset
                + len(encoded_name)
                + ctypes.sizeof(wintypes.WCHAR)
            )
            buffer = ctypes.create_string_buffer(buffer_size)
            rename = ctypes.cast(
                buffer, ctypes.POINTER(FileRenameInformation)
            ).contents
            rename.replace_if_exists = 0
            rename.root_directory = None
            rename.file_name_length = len(encoded_name)
            ctypes.memmove(
                ctypes.addressof(buffer) + FileRenameInformation.file_name.offset,
                encoded_name,
                len(encoded_name),
            )
            renamed = kernel32.SetFileInformationByHandle(
                directory_handle, 3, buffer, buffer_size
            )
            if not renamed:
                # FileRenameInfoEx uses a DWORD flags union in the same
                # naturally aligned prefix.  POSIX semantics permits the
                # directory rename while all descendants remain share-delete
                # pinned by identity.
                wintypes.DWORD.from_buffer(buffer).value = 0x00000002
                renamed = kernel32.SetFileInformationByHandle(
                    directory_handle, 22, buffer, buffer_size
                )
            if not renamed:
                raise FormalWindowsPretrustError(
                    "Formal Windows private commit rename失败: "
                    f"winerror={ctypes.get_last_error()}"
                )
            if staging.exists() or staging.is_symlink():
                raise FormalWindowsPretrustError(
                    "Formal Windows private commit staging仍可见"
                )
            if directory_identity != _windows_path_identity(destination):
                raise FormalWindowsPretrustError(
                    "Formal Windows private commit directory identity不一致"
                )
            release_failed = False
            for handle in reversed(handles):
                if not kernel32.CloseHandle(handle):
                    release_failed = True
            handles.clear()
            if release_failed:
                raise FormalWindowsPretrustError(
                    "Formal Windows private commit rename handle释放失败"
                )
            stack.enter_context(hold_windows_private_directory(destination))
            stack.enter_context(
                hold_windows_private_snapshot(
                    destination,
                    relative_files=names,
                    root_already_held=True,
                )
            )
            validate_committed()
            yield destination
            validate_committed()
        finally:
            cleanup_failed = False
            for handle in reversed(handles):
                if not kernel32.CloseHandle(handle):
                    cleanup_failed = True
            if cleanup_failed:
                raise FormalWindowsPretrustError(
                    "Formal Windows private commit handle释放失败"
                )


@contextmanager
def hold_windows_private_snapshot(
    root: Path,
    *,
    relative_files: tuple[str, ...],
    root_already_held: bool = False,
) -> Iterator[Path]:
    """Hold a completed snapshot root and every declared file for execution."""

    root = Path(root)
    if (
        not relative_files
        or len(relative_files) != len(set(relative_files))
        or tuple(sorted(relative_files)) != relative_files
    ):
        raise FormalWindowsPretrustError(
            "Formal Windows private snapshot file set无效"
        )
    with ExitStack() as stack:
        if root_already_held:
            assert_windows_private_acl(root)
        else:
            stack.enter_context(hold_windows_private_directory(root))
        for relative in relative_files:
            parsed = Path(relative)
            if (
                parsed.is_absolute()
                or "\\" in relative
                or any(part in {"", ".", ".."} for part in parsed.parts)
            ):
                raise FormalWindowsPretrustError(
                    "Formal Windows private snapshot路径无效"
                )
            target = root.joinpath(*parsed.parts)
            if not target.is_relative_to(root):
                raise FormalWindowsPretrustError(
                    "Formal Windows private snapshot路径逃逸"
                )
            stack.enter_context(hold_windows_private_file(target))
        yield root


def _read_closed_file(path: Path, *, label: str) -> bytes:
    try:
        before = path.lstat()
    except OSError as error:
        raise FormalWindowsPretrustError(f"{label}不可用") from error
    if (
        path.is_symlink()
        or _is_reparse(before)
        or not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
        or not 1 <= before.st_size <= _MAX_FILE_BYTES
    ):
        raise FormalWindowsPretrustError(f"{label}不是封闭普通文件")
    assert_windows_private_acl(path.resolve())
    descriptor = -1
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        opened = os.fstat(descriptor)
        if (
            (opened.st_dev, opened.st_ino, opened.st_nlink)
            != (before.st_dev, before.st_ino, before.st_nlink)
            or opened.st_size != before.st_size
        ):
            raise FormalWindowsPretrustError(f"{label}打开期间发生rebound")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(1024 * 1024, _MAX_FILE_BYTES + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > _MAX_FILE_BYTES:
                raise FormalWindowsPretrustError(f"{label}超过大小上限")
        after = os.fstat(descriptor)
        if (
            total != opened.st_size
            or (after.st_dev, after.st_ino, after.st_mode, after.st_nlink, after.st_size)
            != (opened.st_dev, opened.st_ino, opened.st_mode, opened.st_nlink, opened.st_size)
            or after.st_mtime_ns != opened.st_mtime_ns
            or after.st_ctime_ns != opened.st_ctime_ns
        ):
            raise FormalWindowsPretrustError(f"{label}读取期间发生rebound")
        return b"".join(chunks)
    except FormalWindowsPretrustError:
        raise
    except OSError as error:
        raise FormalWindowsPretrustError(f"{label}不可读") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _require_pe_machine(value: bytes, *, expected_machine: int) -> None:
    expected_magic = {0x014C: 0x10B, 0x8664: 0x20B}.get(expected_machine)
    if (
        expected_magic is None
        or value.startswith(b"\x7fELF")
        or len(value) < 0x9A
        or value[:2] != b"MZ"
    ):
        raise FormalWindowsPretrustError("Formal Windows PE machine不一致")
    pe_offset = int.from_bytes(value[0x3C:0x40], "little")
    if (
        pe_offset < 0x40
        or pe_offset + 26 > len(value)
        or value[pe_offset : pe_offset + 4] != b"PE\0\0"
        or int.from_bytes(value[pe_offset + 4 : pe_offset + 6], "little")
        != expected_machine
        or int.from_bytes(value[pe_offset + 6 : pe_offset + 8], "little") < 1
        or int.from_bytes(value[pe_offset + 20 : pe_offset + 22], "little")
        < 0x70
        or int.from_bytes(value[pe_offset + 24 : pe_offset + 26], "little")
        != expected_magic
    ):
        raise FormalWindowsPretrustError("Formal Windows PE machine不一致")


def _require_pe32_plus_amd64(value: bytes) -> None:
    try:
        _require_pe_machine(value, expected_machine=0x8664)
    except FormalWindowsPretrustError as error:
        raise FormalWindowsPretrustError(
            "Formal verifier必须是PE32+ AMD64"
        ) from error


def inspect_windows_pe_imports(path: Path) -> frozenset[str]:
    """Return the closed ASCII import names from one held-compatible PE."""

    value = _read_closed_file(Path(path), label="Formal Windows PE import closure")
    try:
        pe_offset = int.from_bytes(value[0x3C:0x40], "little")
        if value[pe_offset : pe_offset + 4] != b"PE\0\0":
            raise ValueError("signature")
        section_count = int.from_bytes(value[pe_offset + 6 : pe_offset + 8], "little")
        optional_size = int.from_bytes(
            value[pe_offset + 20 : pe_offset + 22], "little"
        )
        optional = pe_offset + 24
        magic = int.from_bytes(value[optional : optional + 2], "little")
        data_directory = optional + ({0x10B: 96, 0x20B: 112}[magic])
        import_rva = int.from_bytes(
            value[data_directory + 8 : data_directory + 12], "little"
        )
        import_size = int.from_bytes(
            value[data_directory + 12 : data_directory + 16], "little"
        )
        if import_rva == 0 and import_size == 0:
            return frozenset()
        if import_rva == 0 or not 20 <= import_size <= len(value):
            raise ValueError("import directory")
        sections: list[tuple[int, int, int]] = []
        section_table = optional + optional_size
        for index in range(section_count):
            offset = section_table + index * 40
            if offset + 40 > len(value):
                raise ValueError("section table")
            virtual_size = int.from_bytes(value[offset + 8 : offset + 12], "little")
            virtual_address = int.from_bytes(
                value[offset + 12 : offset + 16], "little"
            )
            raw_size = int.from_bytes(value[offset + 16 : offset + 20], "little")
            raw_pointer = int.from_bytes(value[offset + 20 : offset + 24], "little")
            sections.append(
                (virtual_address, max(virtual_size, raw_size), raw_pointer)
            )

        def rva_offset(rva: int) -> int:
            for virtual_address, span, raw_pointer in sections:
                if virtual_address <= rva < virtual_address + span:
                    result = raw_pointer + rva - virtual_address
                    if not 0 <= result < len(value):
                        break
                    return result
            raise ValueError("unmapped RVA")

        descriptor = rva_offset(import_rva)
        imports: set[str] = set()
        for _index in range(min(import_size // 20 + 1, 4096)):
            if descriptor + 20 > len(value):
                raise ValueError("import descriptor")
            item = value[descriptor : descriptor + 20]
            if item == bytes(20):
                return frozenset(imports)
            name_rva = int.from_bytes(item[12:16], "little")
            name_offset = rva_offset(name_rva)
            terminator = value.find(b"\0", name_offset, name_offset + 256)
            if terminator < 0:
                raise ValueError("import name")
            name = value[name_offset:terminator].decode("ascii", errors="strict").lower()
            if re.fullmatch(r"[a-z0-9_.-]{1,255}\.dll", name) is None:
                raise ValueError("import name")
            imports.add(name)
            descriptor += 20
    except (KeyError, UnicodeDecodeError, ValueError) as error:
        raise FormalWindowsPretrustError(
            "Formal Windows PE import closure无效"
        ) from error
    raise FormalWindowsPretrustError(
        "Formal Windows PE import closure未终止"
    )


def _require_elf64_amd64(value: bytes) -> None:
    if (
        len(value) < 20
        or value[:4] != b"\x7fELF"
        or value[4] != 2
        or value[5] != 1
        or int.from_bytes(value[18:20], "little") != 0x3E
    ):
        raise FormalWindowsPretrustError("Guest verifier必须是ELF64 AMD64")


def _file_record(name: str, value: bytes) -> dict[str, object]:
    return {
        "mode": "0755"
        if name in {"formal-release-verifier.exe", "offline-release-verifier"}
        else "0644",
        "name": name,
        "sha256": _digest(value),
        "size": len(value),
    }


def _material_record(name: str, value: bytes) -> dict[str, object]:
    return {"name": name, "sha256": _digest(value), "size": len(value)}


@dataclass(frozen=True)
class FormalWindowsTrustProfile:
    identity: str
    source_profile_identity: str
    source_verifier_identity: str
    verifier_identity: str
    linux_guest_verifier_identity: str
    github_trusted_root_sha256: str
    github_tuf_root_sha256: str
    sigstore_trusted_root_sha256: str
    sigstore_tuf_root_sha256: str
    platform: str = "windows/amd64"


@dataclass(frozen=True)
class FormalWindowsPretrustedTrustMaterial:
    root: Path
    profile: FormalWindowsTrustProfile
    identity: str
    verifier_path: Path
    linux_guest_verifier_path: Path
    github_trusted_root_path: Path
    github_tuf_root_path: Path
    sigstore_trusted_root_path: Path
    sigstore_tuf_root_path: Path

    @property
    def kit_identity(self) -> str:
        return self.identity

    @classmethod
    def load(cls, root: Path) -> FormalWindowsPretrustedTrustMaterial:
        root = Path(root)
        if not root.is_absolute():
            raise FormalWindowsPretrustError(
                "Formal Windows pretrust root必须是绝对路径"
            )
        try:
            metadata = root.lstat()
            names = {item.name for item in root.iterdir()}
        except OSError as error:
            raise FormalWindowsPretrustError(
                "Formal Windows pretrust root不可用"
            ) from error
        if (
            root.is_symlink()
            or _is_reparse(metadata)
            or not stat.S_ISDIR(metadata.st_mode)
            or names != FORMAL_WINDOWS_PRETRUST_FILES
        ):
            raise FormalWindowsPretrustError(
                "Formal Windows pretrust root文件集合未关闭"
            )
        assert_windows_private_acl(root)
        files = {
            name: _read_closed_file(root / name, label=f"Formal pretrust/{name}")
            for name in sorted(FORMAL_WINDOWS_PRETRUST_FILES)
        }
        profile, kit_identity = _validate_closed_files(files)
        return cls(
            root=root,
            profile=profile,
            identity=kit_identity,
            verifier_path=root / "formal-release-verifier.exe",
            linux_guest_verifier_path=root / "offline-release-verifier",
            github_trusted_root_path=root / "github-trusted-root.jsonl",
            github_tuf_root_path=root / "github-tuf-root.json",
            sigstore_trusted_root_path=root / "sigstore-trusted-root.jsonl",
            sigstore_tuf_root_path=root / "sigstore-tuf-root.json",
        )


@dataclass(frozen=True)
class FormalWindowsPretrustBinding:
    installer_materials_sha256: str
    kit_identity: str
    profile_identity: str
    source_profile_identity: str
    windows_host_verifier_identity: str
    linux_guest_verifier_identity: str
    github_trusted_root_sha256: str
    github_tuf_root_sha256: str
    sigstore_trusted_root_sha256: str
    sigstore_tuf_root_sha256: str

    @property
    def formal_windows_pretrust_kit_identity(self) -> str:
        return self.kit_identity

    @property
    def offline_release_trust_profile_identity(self) -> str:
        return self.source_profile_identity

    def as_prepublication_record(self) -> dict[str, object]:
        return {
            "formalProfileIdentity": self.profile_identity,
            "githubTrustedRootIdentity": self.github_trusted_root_sha256,
            "githubTufRootIdentity": self.github_tuf_root_sha256,
            "kitIdentity": self.kit_identity,
            "linuxGuestVerifierIdentity": self.linux_guest_verifier_identity,
            "sourceProfileIdentity": self.source_profile_identity,
            "sigstoreTrustedRootIdentity": self.sigstore_trusted_root_sha256,
            "sigstoreTufRootIdentity": self.sigstore_tuf_root_sha256,
            "windowsHostVerifierIdentity": self.windows_host_verifier_identity,
        }


def _validate_identity_record(
    value: object, *, expected: Mapping[str, object], label: str
) -> None:
    if value != expected:
        raise FormalWindowsPretrustError(f"Formal Windows {label}身份不一致")


def _validate_closed_files(
    files: Mapping[str, bytes],
) -> tuple[FormalWindowsTrustProfile, str]:
    if set(files) != FORMAL_WINDOWS_PRETRUST_FILES:
        raise FormalWindowsPretrustError("Formal Windows pretrust文件集合未关闭")
    windows_verifier = files["formal-release-verifier.exe"]
    linux_verifier = files["offline-release-verifier"]
    _require_pe32_plus_amd64(windows_verifier)
    _require_elf64_amd64(linux_verifier)
    if _digest(windows_verifier) == _digest(linux_verifier):
        raise FormalWindowsPretrustError("Host与Guest verifier身份不得复用")

    profile_record = _json_object(
        files["formal-windows-trust-profile.json"],
        label="Formal Windows profile",
    )
    expected_profile_keys = {
        "authorityRole",
        "github",
        "linuxGuestVerifier",
        "platform",
        "profileIdentity",
        "releaseAuthority",
        "schemaVersion",
        "sigstore",
        "sourceProfileIdentity",
        "sourceVerifierIdentity",
        "windowsHostVerifier",
    }
    if set(profile_record) != expected_profile_keys:
        raise FormalWindowsPretrustError("Formal Windows profile字段未关闭")
    profile_without_identity = dict(profile_record)
    profile_identity = profile_without_identity.pop("profileIdentity", None)
    if (
        profile_record["schemaVersion"] != 1
        or profile_record["authorityRole"] != _AUTHORITY_ROLE
        or profile_record["releaseAuthority"] != _RELEASE_AUTHORITY
        or profile_record["platform"]
        != {
            "binaryFormat": "PE32+",
            "goarch": "amd64",
            "goos": "windows",
            "machine": "AMD64",
        }
        or not _is_digest(profile_record["sourceProfileIdentity"])
        or not _is_digest(profile_record["sourceVerifierIdentity"])
        or profile_identity != _digest(_canonical_json_bytes(profile_without_identity))
    ):
        raise FormalWindowsPretrustError("Formal Windows profile合同无效")

    github_trusted = _material_record(
        "github-trusted-root.jsonl", files["github-trusted-root.jsonl"]
    )
    github_tuf = _material_record(
        "github-tuf-root.json", files["github-tuf-root.json"]
    )
    sigstore_trusted = _material_record(
        "sigstore-trusted-root.jsonl", files["sigstore-trusted-root.jsonl"]
    )
    sigstore_tuf = _material_record(
        "sigstore-tuf-root.json", files["sigstore-tuf-root.json"]
    )
    windows_record = {
        **_material_record("formal-release-verifier.exe", windows_verifier),
        "binaryFormat": "PE32+",
        "machine": "AMD64",
    }
    linux_record = {
        **_material_record("offline-release-verifier", linux_verifier),
        "binaryFormat": "ELF64",
        "machine": "AMD64",
    }
    if profile_record["github"] != {
        "trustedRoot": github_trusted,
        "tufRoot": github_tuf,
    }:
        raise FormalWindowsPretrustError("Formal Windows GitHub root角色无效")
    if profile_record["sigstore"] != {
        "trustedRoot": sigstore_trusted,
        "tufRoot": sigstore_tuf,
    }:
        raise FormalWindowsPretrustError("Formal Windows Sigstore root角色无效")
    _validate_identity_record(
        profile_record["windowsHostVerifier"],
        expected=windows_record,
        label="Host verifier",
    )
    _validate_identity_record(
        profile_record["linuxGuestVerifier"],
        expected=linux_record,
        label="Guest verifier",
    )
    if profile_record["sourceVerifierIdentity"] != linux_record["sha256"]:
        raise FormalWindowsPretrustError("Guest verifier未绑定source profile")

    runtime_records = [
        _file_record(name, files[name])
        for name in sorted(FORMAL_WINDOWS_PRETRUST_RUNTIME_FILES)
    ]
    kit_identity = _digest(_canonical_json_bytes(runtime_records))
    manifest = _json_object(
        files["formal-windows-pretrust-manifest.json"],
        label="Formal Windows manifest",
    )
    expected_manifest = {
        "authorityRole": _AUTHORITY_ROLE,
        "files": runtime_records,
        "formalProfileIdentity": profile_identity,
        "kitIdentity": kit_identity,
        "releaseAuthority": _RELEASE_AUTHORITY,
        "schemaVersion": 1,
        "sourceProfileIdentity": profile_record["sourceProfileIdentity"],
    }
    if manifest != expected_manifest:
        raise FormalWindowsPretrustError("Formal Windows manifest绑定无效")
    return (
        FormalWindowsTrustProfile(
            identity=profile_identity,
            source_profile_identity=str(profile_record["sourceProfileIdentity"]),
            source_verifier_identity=str(
                profile_record["sourceVerifierIdentity"]
            ),
            verifier_identity=str(windows_record["sha256"]),
            linux_guest_verifier_identity=str(linux_record["sha256"]),
            github_trusted_root_sha256=str(github_trusted["sha256"]),
            github_tuf_root_sha256=str(github_tuf["sha256"]),
            sigstore_trusted_root_sha256=str(sigstore_trusted["sha256"]),
            sigstore_tuf_root_sha256=str(sigstore_tuf["sha256"]),
        ),
        kit_identity,
    )


def inspect_formal_windows_pretrust_in_installer_materials(
    installer_materials: Path,
) -> FormalWindowsPretrustBinding:
    """Derive the Formal kit binding from exact installer-materials bytes.

    This is the consumer seam used after provenance verification.  No field in
    an operator-authored Formal request is treated as the root of this result.
    A Windows consumer must call it while the exact archive is already inside
    a held private snapshot; this format inspector does not invent filesystem
    authority for a caller-owned path.
    """

    from release.materials import inspect_installer_materials

    installer_materials = Path(installer_materials)
    archive_identity = inspect_installer_materials(installer_materials)
    prefix = FORMAL_WINDOWS_PRETRUST_PREFIX + "/"
    expected_paths = {
        prefix + name for name in FORMAL_WINDOWS_PRETRUST_FILES
    }
    kit_members = {
        item.path: item
        for item in archive_identity.files
        if item.path.startswith(prefix)
    }
    if set(kit_members) != expected_paths:
        raise FormalWindowsPretrustError(
            "Installer materials缺少闭合Formal Windows pretrust"
        )
    for relative, identity in kit_members.items():
        name = relative.removeprefix(prefix)
        expected_mode = (
            0o755
            if name
            in {"formal-release-verifier.exe", "offline-release-verifier"}
            else 0o644
        )
        if identity.mode != expected_mode:
            raise FormalWindowsPretrustError(
                "Installer materials Formal Windows pretrust mode无效"
            )

    try:
        before = installer_materials.lstat()
    except OSError as error:
        raise FormalWindowsPretrustError("Installer materials不可用") from error
    if (
        installer_materials.is_symlink()
        or _is_reparse(before)
        or not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
        or before.st_size != archive_identity.size
    ):
        raise FormalWindowsPretrustError("Installer materials不是封闭普通文件")
    descriptor = -1
    try:
        descriptor = os.open(
            installer_materials,
            os.O_RDONLY
            | getattr(os, "O_BINARY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        with os.fdopen(descriptor, "rb", closefd=True) as stream:
            descriptor = -1
            opened = os.fstat(stream.fileno())
            if (
                (opened.st_dev, opened.st_ino, opened.st_nlink, opened.st_size)
                != (before.st_dev, before.st_ino, before.st_nlink, before.st_size)
            ):
                raise FormalWindowsPretrustError(
                    "Installer materials打开期间发生rebound"
                )
            digest = hashlib.sha256()
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
            if _DIGEST_PREFIX + digest.hexdigest() != archive_identity.sha256:
                raise FormalWindowsPretrustError("Installer materials身份不一致")
            stream.seek(0)
            closed_files: dict[str, bytes] = {}
            with tarfile.open(fileobj=stream, mode="r:") as archive:
                for member in archive.getmembers():
                    if member.name not in expected_paths:
                        continue
                    source = archive.extractfile(member)
                    if source is None:
                        raise FormalWindowsPretrustError(
                            "Formal Windows pretrust archive member不可读"
                        )
                    value = source.read(_MAX_FILE_BYTES + 1)
                    if len(value) != member.size or len(value) > _MAX_FILE_BYTES:
                        raise FormalWindowsPretrustError(
                            "Formal Windows pretrust archive member大小无效"
                        )
                    name = member.name.removeprefix(prefix)
                    identity = kit_members[member.name]
                    if (
                        len(value) != identity.size
                        or _digest(value) != identity.sha256
                    ):
                        raise FormalWindowsPretrustError(
                            "Formal Windows pretrust archive member身份无效"
                        )
                    closed_files[name] = value
            after = os.fstat(stream.fileno())
            if (
                (after.st_dev, after.st_ino, after.st_nlink, after.st_size)
                != (opened.st_dev, opened.st_ino, opened.st_nlink, opened.st_size)
                or after.st_mtime_ns != opened.st_mtime_ns
                or after.st_ctime_ns != opened.st_ctime_ns
            ):
                raise FormalWindowsPretrustError(
                    "Installer materials读取期间发生rebound"
                )
    except FormalWindowsPretrustError:
        raise
    except (OSError, tarfile.TarError) as error:
        raise FormalWindowsPretrustError("Installer materials不可读") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)

    profile, kit_identity = _validate_closed_files(closed_files)
    return FormalWindowsPretrustBinding(
        installer_materials_sha256=archive_identity.sha256,
        kit_identity=kit_identity,
        profile_identity=profile.identity,
        source_profile_identity=profile.source_profile_identity,
        windows_host_verifier_identity=profile.verifier_identity,
        linux_guest_verifier_identity=profile.linux_guest_verifier_identity,
        github_trusted_root_sha256=profile.github_trusted_root_sha256,
        github_tuf_root_sha256=profile.github_tuf_root_sha256,
        sigstore_trusted_root_sha256=profile.sigstore_trusted_root_sha256,
        sigstore_tuf_root_sha256=profile.sigstore_tuf_root_sha256,
    )


def build_formal_windows_pretrust_kit(
    *,
    verifier: Path,
    source_initial_trust_kit: Path,
    output: Path,
) -> dict[str, object]:
    """Derive one dual-platform Formal kit from an already verified root authority."""

    from release.trust_bootstrap import (
        INITIAL_TRUST_KIT_FILES,
        TrustBootstrapError,
        validate_initial_trust_kit,
    )

    verifier = Path(verifier)
    source_initial_trust_kit = Path(source_initial_trust_kit)
    output = Path(output)
    if output.exists() or output.is_symlink():
        raise FormalWindowsPretrustError("Formal Windows pretrust输出必须不存在")
    try:
        source_profile: TrustProfile = validate_initial_trust_kit(
            source_initial_trust_kit
        )
    except (OSError, TrustBootstrapError, ValueError) as error:
        raise FormalWindowsPretrustError("Linux initial pretrust authority无效") from error
    if {item.name for item in source_initial_trust_kit.iterdir()} != set(
        INITIAL_TRUST_KIT_FILES
    ):
        raise FormalWindowsPretrustError("Linux initial pretrust authority未关闭")

    windows_verifier = _read_closed_file(
        verifier, label="Formal Windows verifier"
    )
    _require_pe32_plus_amd64(windows_verifier)
    source_files = {
        name: _read_closed_file(
            source_initial_trust_kit / name,
            label=f"Linux initial pretrust/{name}",
        )
        for name in (
            "github-trusted-root.jsonl",
            "github-tuf-root.json",
            "offline-release-verifier",
            "sigstore-trusted-root.jsonl",
            "sigstore-tuf-root.json",
        )
    }
    _require_elf64_amd64(source_files["offline-release-verifier"])
    if _digest(windows_verifier) == source_profile.verifier_identity:
        raise FormalWindowsPretrustError("Host与Guest verifier身份不得复用")

    profile_body = {
        "authorityRole": _AUTHORITY_ROLE,
        "github": {
            "trustedRoot": _material_record(
                "github-trusted-root.jsonl",
                source_files["github-trusted-root.jsonl"],
            ),
            "tufRoot": _material_record(
                "github-tuf-root.json", source_files["github-tuf-root.json"]
            ),
        },
        "linuxGuestVerifier": {
            **_material_record(
                "offline-release-verifier",
                source_files["offline-release-verifier"],
            ),
            "binaryFormat": "ELF64",
            "machine": "AMD64",
        },
        "platform": {
            "binaryFormat": "PE32+",
            "goarch": "amd64",
            "goos": "windows",
            "machine": "AMD64",
        },
        "releaseAuthority": _RELEASE_AUTHORITY,
        "schemaVersion": 1,
        "sigstore": {
            "trustedRoot": _material_record(
                "sigstore-trusted-root.jsonl",
                source_files["sigstore-trusted-root.jsonl"],
            ),
            "tufRoot": _material_record(
                "sigstore-tuf-root.json", source_files["sigstore-tuf-root.json"]
            ),
        },
        "sourceProfileIdentity": source_profile.identity,
        "sourceVerifierIdentity": source_profile.verifier_identity,
        "windowsHostVerifier": {
            **_material_record("formal-release-verifier.exe", windows_verifier),
            "binaryFormat": "PE32+",
            "machine": "AMD64",
        },
    }
    profile = {
        **profile_body,
        "profileIdentity": _digest(_canonical_json_bytes(profile_body)),
    }
    runtime = {
        **source_files,
        "formal-release-verifier.exe": windows_verifier,
        "formal-windows-trust-profile.json": _canonical_json_bytes(profile),
    }
    runtime_records = [
        _file_record(name, runtime[name]) for name in sorted(runtime)
    ]
    kit_identity = _digest(_canonical_json_bytes(runtime_records))
    manifest = {
        "authorityRole": _AUTHORITY_ROLE,
        "files": runtime_records,
        "formalProfileIdentity": profile["profileIdentity"],
        "kitIdentity": kit_identity,
        "releaseAuthority": _RELEASE_AUTHORITY,
        "schemaVersion": 1,
        "sourceProfileIdentity": source_profile.identity,
    }
    files = {
        **runtime,
        "formal-windows-pretrust-manifest.json": _canonical_json_bytes(manifest),
    }

    output.parent.mkdir(parents=True, exist_ok=True)
    staging = (
        create_windows_private_directory(
            output.parent, prefix=".formal-windows-pretrust"
        )
        if os.name == "nt"
        else Path(
            tempfile.mkdtemp(
                prefix=".formal-windows-pretrust-", dir=output.parent
            )
        )
    )
    try:
        for name, value in sorted(files.items()):
            target = staging / name
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
            descriptor = os.open(target, flags, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(value)
                stream.flush()
                os.fsync(stream.fileno())
            target.chmod(
                0o700
                if name
                in {"formal-release-verifier.exe", "offline-release-verifier"}
                else 0o600
            )
        material = FormalWindowsPretrustedTrustMaterial.load(staging.resolve())
        if material.identity != kit_identity:
            raise FormalWindowsPretrustError("Formal Windows kit identity rebound")
        os.rename(staging, output)
        return {
            "files": len(files),
            "formalProfileIdentity": material.profile.identity,
            "kitIdentity": material.identity,
            "linuxGuestVerifierIdentity": (
                material.profile.linux_guest_verifier_identity
            ),
            "sourceProfileIdentity": material.profile.source_profile_identity,
            "status": "PASS",
            "windowsHostVerifierIdentity": material.profile.verifier_identity,
        }
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
