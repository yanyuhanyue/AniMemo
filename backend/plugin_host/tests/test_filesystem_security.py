import ctypes
import os
import stat
import tempfile
from ctypes import wintypes
from pathlib import Path
from types import SimpleNamespace
from unittest import mock, skipIf, skipUnless

from django.test import SimpleTestCase

from plugin_host.filesystem_security import (
    PLUGIN_ROOT_DIRECTORY_MODE,
    PRIVATE_DIRECTORY_MODE,
    PRIVATE_FILE_MODE,
    RUNTIME_DIRECTORY_MODE,
    RUNTIME_FILE_MODE,
    PluginFilesystemSecurityError,
    _contained,
    _require_owner,
    _windows_current_sid,
    ensure_directory,
    ensure_plugin_layout,
    filesystem_diagnostic_code,
    resolve_contained_path,
    secure_file,
    secure_tree,
    validate_secure_tree,
    write_secure_bytes,
)


def _set_windows_dacl(path, descriptor_text):
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


def _grant_windows_world_full_control(path):
    sid = _windows_current_sid()
    _set_windows_dacl(
        path,
        f"D:P(A;;FA;;;{sid})(A;;FA;;;SY)(A;;FA;;;BA)(A;;FA;;;WD)",
    )


def _storage(root):
    return SimpleNamespace(
        root=root,
        packages=root / "packages",
        runtime=root / "runtime",
        previews=root / "previews",
        staging=root / "staging",
    )


class PluginFilesystemSecurityTests(SimpleTestCase):
    def test_containment_accepts_the_exact_normalized_root(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "plugins"

            boundary, target = _contained(root, root / "nested" / "..")

            self.assertEqual(boundary, root.absolute())
            self.assertEqual(target, boundary)
            self.assertIs(target, boundary)

    def test_containment_preserves_guarded_component_case(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "plugins"
            requested = root / "Demo" / "1.0.0-RC.1"

            _, target = _contained(root, requested)

            self.assertEqual(target, Path(os.path.abspath(requested)))
            self.assertEqual(target.parts[-2:], ("Demo", "1.0.0-RC.1"))

    @skipUnless(os.name == "nt", "Windows case-insensitive path contract")
    def test_containment_aligns_only_the_windows_authority_root_case(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "PluginRoot"
            differently_cased_root = Path(str(root).swapcase())
            requested = differently_cased_root / "Demo" / "1.0.0-RC.1"

            boundary, target = _contained(root, requested)

            expected = boundary / "Demo" / "1.0.0-RC.1"
            self.assertEqual(os.fspath(target), os.fspath(expected))

    def test_containment_rejects_a_sibling_with_the_same_textual_prefix(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "plugins"
            sibling = Path(directory) / "plugins-escape" / "payload"

            with self.assertRaises(PluginFilesystemSecurityError) as raised:
                _contained(root, sibling)

            self.assertEqual(raised.exception.diagnostic_code, "path_containment")

    def test_diagnostic_code_rejects_unlisted_values(self):
        malformed = ["dacl_read"]
        error = PluginFilesystemSecurityError("private-path-token", diagnostic_code=malformed)

        self.assertEqual(error.diagnostic_code, "unspecified")

    def test_diagnostic_code_is_revalidated_at_the_logging_boundary(self):
        sentinel = r"C:\private\plugin-store token=FILESYSTEM_DIAGNOSTIC_CANARY"
        error = PluginFilesystemSecurityError("stable", diagnostic_code="dacl_read")
        error._diagnostic_code = sentinel

        self.assertEqual(filesystem_diagnostic_code(error), "unspecified")

    def test_missing_boundary_is_hardened_before_nested_creation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "plugins"
            nested = root / "private" / "nested"

            ensure_directory(root, nested)
            validate_secure_tree(root, root)

            self.assertTrue(nested.is_dir())
            if os.name != "nt":
                self.assertEqual(
                    stat.S_IMODE(root.stat().st_mode),
                    PRIVATE_DIRECTORY_MODE,
                )
                self.assertEqual(
                    stat.S_IMODE(nested.stat().st_mode),
                    PRIVATE_DIRECTORY_MODE,
                )

    @skipIf(os.name == "nt", "POSIX owner contract")
    def test_owner_validation_rejects_uid_or_gid_drift(self):
        current_uid = os.geteuid()
        current_gid = os.getegid()
        for metadata in (
            SimpleNamespace(st_uid=current_uid + 1, st_gid=current_gid),
            SimpleNamespace(st_uid=current_uid, st_gid=current_gid + 1),
        ):
            with self.subTest(uid=metadata.st_uid, gid=metadata.st_gid):
                with self.assertRaises(PluginFilesystemSecurityError) as raised:
                    _require_owner(metadata)
                self.assertEqual(raised.exception.diagnostic_code, "path_owner")

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
                    root,
                    runtime_root,
                    directory_mode=RUNTIME_DIRECTORY_MODE,
                    file_mode=RUNTIME_FILE_MODE,
                )
            finally:
                os.umask(previous)

            self.assertEqual(stat.S_IMODE(private.stat().st_mode), PRIVATE_FILE_MODE)
            self.assertEqual(
                stat.S_IMODE(root.stat().st_mode),
                PLUGIN_ROOT_DIRECTORY_MODE,
            )
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
            root = Path(directory) / "plugins"
            runtime = root / "runtime" / "demo" / "1.0.0"
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
                    root,
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

            self.assertEqual(
                stat.S_IMODE(root.stat().st_mode),
                PLUGIN_ROOT_DIRECTORY_MODE,
            )
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
                validate_secure_tree(storage.root, storage.staging)

    def test_symlinked_authority_root_is_rejected_without_resolution(self):
        with tempfile.TemporaryDirectory() as directory:
            actual = Path(directory) / "actual"
            actual.mkdir()
            link = Path(directory) / "plugin-root"
            try:
                link.symlink_to(actual, target_is_directory=True)
            except OSError:
                self.skipTest("symlink creation unavailable")

            with self.assertRaises(PluginFilesystemSecurityError) as raised:
                validate_secure_tree(link, link)

            self.assertEqual(raised.exception.diagnostic_code, "path_reparse")

    def test_physical_containment_rejects_an_intermediate_parent_swap(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "plugins"
            storage = _storage(root)
            ensure_plugin_layout(storage)
            runtime = storage.runtime / "demo" / "1.0.0"
            backend = runtime / "backend"
            payload = backend / "plugin.py"
            write_secure_bytes(root, payload, b"safe")
            validate_secure_tree(root, runtime)

            displaced = runtime / "backend.displaced"
            backend.rename(displaced)
            outside = Path(directory) / "outside"
            outside.mkdir()
            (outside / "plugin.py").write_bytes(b"outside")

            def assert_physical_escape_rejected():
                with self.assertRaises(PluginFilesystemSecurityError) as raised:
                    resolve_contained_path(runtime, payload)
                self.assertEqual(
                    raised.exception.diagnostic_code,
                    "path_containment",
                )

            try:
                backend.symlink_to(outside, target_is_directory=True)
            except OSError:
                displaced.rename(backend)
                with mock.patch.object(
                    Path,
                    "resolve",
                    return_value=outside / "plugin.py",
                ):
                    assert_physical_escape_rejected()
            else:
                assert_physical_escape_rejected()

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
            validate_secure_tree(storage.root, storage.staging)
            _grant_windows_world_full_control(payload)
            with self.assertRaises(PluginFilesystemSecurityError):
                validate_secure_tree(storage.root, storage.staging)

    @skipUnless(os.name == "nt", "native Windows DACL contract")
    def test_windows_binary_acl_validation_rejects_three_ace_variants(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = _storage(Path(directory))
            ensure_plugin_layout(storage)
            payload = write_secure_bytes(
                storage.staging,
                storage.staging / "payload",
                b"private",
            )
            sid = _windows_current_sid()
            flag_target = storage.staging / "flag-target"
            ensure_directory(storage.staging, flag_target)
            _set_windows_dacl(
                flag_target,
                f"D:P(A;OI;FA;;;{sid})(A;OI;FA;;;SY)(A;OI;FA;;;BA)",
            )
            with self.assertRaises(PluginFilesystemSecurityError) as raised:
                validate_secure_tree(storage.root, storage.staging)
            self.assertEqual(raised.exception.diagnostic_code, "dacl_ace_flags")
            ensure_directory(storage.staging, flag_target)
            cases = (
                (
                    f"D:P(D;;FA;;;{sid})(A;;FA;;;SY)(A;;FA;;;BA)",
                    "dacl_ace_type",
                ),
                (
                    f"D:P(A;;FR;;;{sid})(A;;FA;;;SY)(A;;FA;;;BA)",
                    "dacl_ace_rights",
                ),
                (
                    f"D:P(A;;FA;;;{sid})(A;;FA;;;SY)(A;;FA;;;WD)",
                    "dacl_ace_principal",
                ),
            )
            for descriptor_text, diagnostic_code in cases:
                with self.subTest(diagnostic_code=diagnostic_code):
                    _set_windows_dacl(payload, descriptor_text)
                    with self.assertRaises(PluginFilesystemSecurityError) as raised:
                        validate_secure_tree(storage.root, storage.staging)
                    self.assertEqual(
                        raised.exception.diagnostic_code,
                        diagnostic_code,
                    )
                    secure_file(storage.staging, payload)
