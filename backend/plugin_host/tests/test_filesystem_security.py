import os
import stat
import subprocess
import tempfile
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
    ensure_plugin_layout,
    secure_file,
    secure_tree,
    validate_secure_tree,
    write_secure_bytes,
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
            secure_file(runtime, payload, mode=RUNTIME_FILE_MODE)

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
            icacls = Path(os.environ["SystemRoot"]) / "System32" / "icacls.exe"
            completed = subprocess.run(
                [str(icacls), str(payload), "/grant", "*S-1-1-0:(F)"],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                check=False,
                creationflags=int(getattr(subprocess, "CREATE_NO_WINDOW", 0)),
            )
            self.assertEqual(completed.returncode, 0)
            with self.assertRaises(PluginFilesystemSecurityError):
                validate_secure_tree(storage.staging)
