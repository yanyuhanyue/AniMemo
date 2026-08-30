import ctypes
import os
import stat
import tempfile
from ctypes import wintypes
from pathlib import Path
from types import SimpleNamespace
from unittest import skipIf, skipUnless

from django.test import SimpleTestCase

from plugin_host.filesystem_security import (
    PRIVATE_DIRECTORY_MODE,
    PRIVATE_FILE_MODE,
    RUNTIME_DIRECTORY_MODE,
    RUNTIME_FILE_MODE,
    PluginFilesystemSecurityError,
    _validate_windows_ace_entries,
    _windows_current_sid,
    ensure_plugin_layout,
    filesystem_diagnostic_code,
    secure_file,
    secure_tree,
    validate_secure_tree,
    write_secure_bytes,
)


def _grant_windows_world_full_control(path):
    sid = _windows_current_sid()
    descriptor_text = f"D:P(A;;FA;;;{sid})(A;;FA;;;SY)(A;;FA;;;BA)(A;;FA;;;WD)"
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
    dacl = ctypes.c_void_p()
    present = wintypes.BOOL()
    defaulted = wintypes.BOOL()
    length = wintypes.DWORD()
    try:
        if not advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            descriptor_text, 1, ctypes.byref(descriptor), ctypes.byref(length)
        ):
            raise OSError(ctypes.get_last_error(), "cannot create test DACL")
        if not advapi32.GetSecurityDescriptorDacl(
            descriptor, ctypes.byref(present), ctypes.byref(dacl), ctypes.byref(defaulted)
        ) or not present.value:
            raise OSError(ctypes.get_last_error(), "cannot read test DACL")
        target = ctypes.create_unicode_buffer(str(path))
        result = advapi32.SetNamedSecurityInfoW(
            target, 1, 0x00000004 | 0x80000000, None, None, dacl, None
        )
        if result:
            raise OSError(result, "cannot install test DACL")
    finally:
        if descriptor.value:
            kernel32.LocalFree(descriptor)


def _storage(root):
    return SimpleNamespace(
        root=root,
        packages=root / "packages",
        runtime=root / "runtime",
        previews=root / "previews",
        staging=root / "staging",
    )


class PluginFilesystemSecurityTests(SimpleTestCase):
    def test_diagnostic_code_rejects_unlisted_values(self):
        malformed = ["dacl_read"]
        error = PluginFilesystemSecurityError("private-path-token", diagnostic_code=malformed)

        self.assertEqual(error.diagnostic_code, "unspecified")

    def test_diagnostic_code_is_revalidated_at_the_logging_boundary(self):
        sentinel = r"C:\private\plugin-store token=FILESYSTEM_DIAGNOSTIC_CANARY"
        error = PluginFilesystemSecurityError("stable", diagnostic_code="dacl_read")
        error._diagnostic_code = sentinel

        self.assertEqual(filesystem_diagnostic_code(error), "unspecified")

    def test_windows_ace_parser_accepts_equivalent_sddl_text_forms(self):
        sid = "S-1-5-21-1000"
        for flags in ("OICI", "CIOI"):
            with self.subTest(flags=flags):
                _validate_windows_ace_entries(
                    f"P(A;{flags};FA;;;{sid})"
                    f"(A;{flags};0x001f01ff;;;SY)"
                    f"(A;{flags};FA;;;S-1-5-32-544)",
                    sid=sid,
                    directory=True,
                )

    def test_windows_ace_parser_rejects_inherited_or_broader_entries(self):
        sid = "S-1-5-21-1000"
        cases = (
            (f"P(A;OICIID;FA;;;{sid})(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)", "dacl_ace_flags"),
            (f"P(A;OICI;GA;;;{sid})(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)", "dacl_ace_rights"),
            (f"P(A;OICI;FA;;;{sid})(A;OICI;FA;;;SY)(A;OICI;FA;;;WD)", "dacl_ace_principal"),
        )
        for dacl_text, diagnostic_code in cases:
            with self.subTest(diagnostic_code=diagnostic_code):
                with self.assertRaises(PluginFilesystemSecurityError) as raised:
                    _validate_windows_ace_entries(
                        dacl_text,
                        sid=sid,
                        directory=True,
                    )
                self.assertEqual(raised.exception.diagnostic_code, diagnostic_code)

    @skipIf(os.name == "nt", "POSIX mode contract")
    def test_permissive_umask_cannot_create_group_or_world_writable_material(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "plugins"
            previous = os.umask(0o000)
            try:
                storage = _storage(root)
                ensure_plugin_layout(storage)
                private = write_secure_bytes(
                    storage.staging,
                    storage.staging / "work" / "payload",
                    b"private",
                )
                runtime_root = storage.runtime / "demo" / "1.0.0"
                write_secure_bytes(
                    runtime_root,
                    runtime_root / "plugin.py",
                    b"runtime",
                    directory_mode=RUNTIME_DIRECTORY_MODE,
                    file_mode=RUNTIME_FILE_MODE,
                )
                secure_tree(
                    runtime_root,
                    directory_mode=RUNTIME_DIRECTORY_MODE,
                    file_mode=RUNTIME_FILE_MODE,
                )
            finally:
                os.umask(previous)

            self.assertEqual(stat.S_IMODE(private.stat().st_mode), PRIVATE_FILE_MODE)
            self.assertEqual(
                stat.S_IMODE(private.parent.stat().st_mode),
                PRIVATE_DIRECTORY_MODE,
            )
            self.assertEqual(
                stat.S_IMODE(runtime_root.stat().st_mode),
                RUNTIME_DIRECTORY_MODE,
            )
            self.assertEqual(
                stat.S_IMODE((runtime_root / "plugin.py").stat().st_mode),
                RUNTIME_FILE_MODE,
            )

    @skipIf(os.name == "nt", "POSIX mode contract")
    def test_restrictive_umask_does_not_narrow_formal_runtime_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory) / "plugins" / "runtime" / "demo" / "1.0.0"
            previous = os.umask(0o077)
            try:
                payload = write_secure_bytes(
                    runtime,
                    runtime / "plugin.py",
                    b"runtime",
                    directory_mode=RUNTIME_DIRECTORY_MODE,
                    file_mode=RUNTIME_FILE_MODE,
                )
                secure_tree(
                    runtime,
                    directory_mode=RUNTIME_DIRECTORY_MODE,
                    file_mode=RUNTIME_FILE_MODE,
                )
            finally:
                os.umask(previous)

            self.assertEqual(stat.S_IMODE(runtime.stat().st_mode), RUNTIME_DIRECTORY_MODE)
            self.assertEqual(stat.S_IMODE(payload.stat().st_mode), RUNTIME_FILE_MODE)

    @skipIf(os.name == "nt", "POSIX mode contract")
    def test_preexisting_writable_paths_are_explicitly_corrected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "plugins"
            runtime = root / "runtime"
            runtime.mkdir(parents=True, mode=0o777)
            payload = runtime / "payload"
            payload.write_bytes(b"runtime")
            os.chmod(root, 0o777)
            os.chmod(runtime, 0o777)
            os.chmod(payload, 0o666)

            storage = _storage(root)
            ensure_plugin_layout(storage)
            secure_file(
                runtime,
                payload,
                mode=RUNTIME_FILE_MODE,
                directory_mode=RUNTIME_DIRECTORY_MODE,
            )

            self.assertEqual(stat.S_IMODE(root.stat().st_mode), PRIVATE_DIRECTORY_MODE)
            self.assertEqual(stat.S_IMODE(runtime.stat().st_mode), RUNTIME_DIRECTORY_MODE)
            self.assertEqual(stat.S_IMODE(payload.stat().st_mode), RUNTIME_FILE_MODE)

    def test_hardlinked_file_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "plugins"
            storage = _storage(root)
            ensure_plugin_layout(storage)
            payload = write_secure_bytes(
                storage.staging,
                storage.staging / "payload",
                b"runtime",
            )
            alias = storage.staging / "alias"
            os.link(payload, alias)

            with self.assertRaises(PluginFilesystemSecurityError):
                secure_file(storage.staging, payload)
            alias.unlink()

    def test_symlinked_tree_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "plugins"
            storage = _storage(root)
            ensure_plugin_layout(storage)
            outside = Path(directory) / "outside"
            outside.mkdir()
            link = storage.staging / "linked"
            try:
                link.symlink_to(outside, target_is_directory=True)
            except OSError:
                self.skipTest("symlink creation unavailable")

            with self.assertRaises(PluginFilesystemSecurityError):
                validate_secure_tree(storage.staging)

    @skipUnless(os.name == "nt", "native Windows DACL contract")
    def test_windows_layout_rejects_an_extra_broad_writer_ace(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = _storage(Path(directory))
            ensure_plugin_layout(storage)
            payload = write_secure_bytes(
                storage.staging,
                storage.staging / "work" / "payload",
                b"private",
            )
            validate_secure_tree(storage.staging)
            _grant_windows_world_full_control(payload)
            with self.assertRaises(PluginFilesystemSecurityError):
                validate_secure_tree(storage.staging)
