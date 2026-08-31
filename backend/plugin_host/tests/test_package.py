import hashlib
import json
import os
import struct
import tempfile
import zlib
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest import mock, skipIf
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from django.test import SimpleTestCase, override_settings

from plugin_host import filesystem_security as filesystem_security_module
from plugin_host import installer as installer_module
from plugin_host import package as package_module
from plugin_host import services as services_module
from plugin_host.filesystem_security import PluginFilesystemSecurityError
from plugin_host.installer import (
    PluginInstallError,
    PluginPackageInstaller,
    _PluginFilesystemLock,
)
from plugin_host.package import (
    LocalPluginPackageStorage,
    PackageHashLock,
    PluginPackageError,
    inspect_package,
)
from plugin_host.services import PluginWorkflowError, static_security_scan


def make_package(*members):
    buffer = BytesIO()
    with ZipFile(buffer, "w", ZIP_DEFLATED) as archive:
        for name, value in members:
            archive.writestr(name, value)
    return buffer.getvalue()


def valid_manifest():
    return {
        "schemaVersion": 2,
        "sdkApi": 2,
        "id": "com.example.demo",
        "slug": "demo",
        "name": "Demo",
        "version": "1.0.0",
        "description": "Demo",
        "author": {"name": "Example"},
        "license": "MIT",
        "installationMode": "user",
        "runtimes": [],
        "extensions": [],
        "permissions": [],
        "hooks": [],
        "dataPolicy": {
            "storesPersonalData": False,
            "usesExternalNetwork": False,
            "acceptsFileUploads": False,
            "retainsDataOnDisable": True,
        },
    }


def indexed_package(*extra_members):
    manifest = json.dumps(valid_manifest(), separators=(",", ":"))
    members = [("manifest.json", manifest), *extra_members]
    files = [
        {"path": name, "size": len(value.encode()), "sha256": hashlib.sha256(value.encode()).hexdigest()}
        for name, value in members
    ]
    index = json.dumps({"packageVersion": 1, "pluginId": "com.example.demo", "slug": "demo", "version": "1.0.0", "files": files})
    return make_package(*members, ("package-index.json", index))


def forge_declared_identity(payload, member_name, declared):
    with ZipFile(BytesIO(payload)) as archive:
        local = archive.getinfo(member_name).header_offset
    encoded = bytearray(payload)
    target = member_name.encode("utf-8")
    central = encoded.find(b"PK\x01\x02")
    while central >= 0:
        name_size = struct.unpack_from("<H", encoded, central + 28)[0]
        extra_size = struct.unpack_from("<H", encoded, central + 30)[0]
        comment_size = struct.unpack_from("<H", encoded, central + 32)[0]
        name = bytes(encoded[central + 46 : central + 46 + name_size])
        if name == target:
            break
        central += 46 + name_size + extra_size + comment_size
        if encoded[central : central + 4] != b"PK\x01\x02":
            central = -1
    if central < 0:
        raise AssertionError("test ZIP central member is absent")
    checksum = zlib.crc32(declared) & 0xFFFFFFFF
    struct.pack_into("<L", encoded, local + 14, checksum)
    struct.pack_into("<L", encoded, local + 22, len(declared))
    struct.pack_into("<L", encoded, central + 16, checksum)
    struct.pack_into("<L", encoded, central + 24, len(declared))
    return bytes(encoded)


class PluginLockSecurityTests(SimpleTestCase):
    @staticmethod
    def _lock_cases(root):
        return (
            (
                "package",
                PackageHashLock(root / "package", "a" * 64),
                package_module,
                PluginPackageError,
            ),
            (
                "installer",
                _PluginFilesystemLock(root / "installer", "demo"),
                installer_module,
                PluginInstallError,
            ),
        )

    def test_lock_initialization_completes_short_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            for name, lock, _, _ in self._lock_cases(Path(directory)):
                real_write = os.write
                first_write = True

                def partial_write(descriptor, payload):
                    nonlocal first_write
                    if first_write:
                        first_write = False
                        return real_write(descriptor, memoryview(payload)[:1])
                    return real_write(descriptor, payload)

                with self.subTest(lock=name), mock.patch.object(
                    filesystem_security_module.os,
                    "write",
                    side_effect=partial_write,
                ), lock:
                    self.assertEqual(lock.path.read_bytes(), lock.payload)

                self.assertFalse(lock.path.exists())

    def test_plugin_lock_rejects_traversal_before_touching_storage(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "storage"

            with self.assertRaises(PluginInstallError):
                _PluginFilesystemLock(root, "../runtime/evil")

            self.assertFalse(root.exists())

    def test_cleanup_rejects_slug_before_global_storage_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "storage"
            installer = PluginPackageInstaller(root)

            with self.assertRaises(PluginInstallError):
                installer.cleanup("../runtime/evil")

            self.assertFalse(root.exists())

    def test_lock_initialization_failures_close_and_remove_created_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, lock, lock_module, error_type in self._lock_cases(root):
                for stage in ("write", "fsync", "secure_file"):
                    with self.subTest(lock=name, stage=stage):
                        captured_descriptors = []
                        real_open = os.open

                        def capture_open(*args, **kwargs):
                            descriptor = real_open(*args, **kwargs)
                            captured_descriptors.append(descriptor)
                            return descriptor

                        patches = [
                            mock.patch.object(
                                lock_module.os,
                                "open",
                                side_effect=capture_open,
                            )
                        ]
                        if stage == "write":
                            patches.append(
                                mock.patch.object(
                                    filesystem_security_module.os,
                                    "write",
                                    return_value=0,
                                )
                            )
                        elif stage == "fsync":
                            patches.append(
                                mock.patch.object(
                                    lock_module.os,
                                    "fsync",
                                    side_effect=OSError("test fsync failure"),
                                )
                            )
                        else:
                            patches.append(
                                mock.patch.object(
                                    lock_module,
                                    "secure_file",
                                    side_effect=PluginFilesystemSecurityError(
                                        "test secure failure"
                                    ),
                                )
                            )

                        with patches[0], patches[1], self.assertRaises(error_type):
                            lock.__enter__()

                        self.assertEqual(len(captured_descriptors), 1)
                        with self.assertRaises(OSError):
                            os.fstat(captured_descriptors[0])
                        self.assertIsNone(lock.fd)
                        self.assertFalse(lock.path.exists())

    @skipIf(os.name == "nt", "Windows prevents replacing an open lock file")
    def test_lock_exit_preserves_replacement_inode(self):
        with tempfile.TemporaryDirectory() as directory:
            for name, lock, _, _ in self._lock_cases(Path(directory)):
                with self.subTest(lock=name):
                    lock.__enter__()
                    displaced = lock.path.with_name(f"{lock.path.name}.displaced")
                    os.replace(lock.path, displaced)
                    replacement = b"replacement-owner\n"
                    lock.path.write_bytes(replacement)

                    lock.__exit__(None, None, None)

                    self.assertEqual(lock.path.read_bytes(), replacement)

    def test_lock_exit_preserves_changed_owner_payload(self):
        with tempfile.TemporaryDirectory() as directory:
            for name, lock, _, _ in self._lock_cases(Path(directory)):
                with self.subTest(lock=name):
                    lock.__enter__()
                    replacement = b"different-owner\n"
                    lock.path.write_bytes(replacement)

                    lock.__exit__(None, None, None)

                    self.assertEqual(lock.path.read_bytes(), replacement)


class PackageSecurityTests(SimpleTestCase):
    def test_manifest_and_storage_share_the_same_slug_acceptance_boundary(self):
        candidates = (
            "demo",
            "watch-history-importer",
            "a" * 80,
            "con",
            "prn",
            "com1",
            "a" * 81,
            "Demo",
            "d\N{LATIN SMALL LETTER E WITH ACUTE}mo",
        )
        with tempfile.TemporaryDirectory() as directory:
            storage = LocalPluginPackageStorage(Path(directory) / "storage")
            for slug in candidates:
                manifest = valid_manifest()
                manifest["slug"] = slug
                try:
                    package_module.validate_manifest(manifest)
                    manifest_accepts = True
                except package_module.ManifestError:
                    manifest_accepts = False
                try:
                    storage.list_versions(slug)
                    storage_accepts = True
                except PluginPackageError:
                    storage_accepts = False

                with self.subTest(slug=slug):
                    self.assertEqual(storage_accepts, manifest_accepts)

    def test_manifest_and_storage_share_the_same_version_acceptance_boundary(self):
        candidates = (
            "1.0.0",
            "1.0.0-RC.1",
            "1.0.0-x.7.z.92",
            "1.0.0-a-b",
            "1.0.0-0A",
            "1.0.0-rc." + ("1" * 31),
            "1.0.0-rc." + ("1" * 32),
            "1.0.0-rc.",
            "1.0.0-a..b",
            "1.0.0-01",
            "../1.0.0",
            r"1.0.0\child",
            "\N{FULLWIDTH DIGIT ONE}.0.0",
        )
        for version in candidates:
            manifest = valid_manifest()
            manifest["version"] = version
            try:
                package_module.validate_manifest(manifest)
                manifest_accepts = True
            except package_module.ManifestError:
                manifest_accepts = False
            try:
                package_module._canonical_version_segment(version)
                storage_accepts = True
            except PluginPackageError:
                storage_accepts = False

            with self.subTest(version=version):
                self.assertEqual(storage_accepts, manifest_accepts)

    def test_storage_rejects_noncanonical_slug_before_any_path_operation(self):
        invalid_slugs = (
            "",
            ".",
            "..",
            "../escape",
            r"..\escape",
            "/absolute",
            r"C:\absolute",
            "nested/plugin",
            r"nested\plugin",
            "Demo",
            "d\N{LATIN SMALL LETTER E WITH ACUTE}mo",
            "a" * 81,
            "con",
            b"demo",
            Path("demo"),
            None,
        )
        with tempfile.TemporaryDirectory() as directory:
            storage = LocalPluginPackageStorage(Path(directory) / "storage")
            for slug in invalid_slugs:
                with self.subTest(slug=slug), mock.patch.object(
                    package_module,
                    "remove_secure_tree",
                ) as remove:
                    with self.assertRaises(PluginPackageError):
                        storage.delete_plugin(slug)
                    remove.assert_not_called()

    def test_storage_rejects_noncanonical_version_before_reading_cas(self):
        invalid_versions = (
            "",
            ".",
            "..",
            "../1.0.0",
            r"..\1.0.0",
            "/1.0.0",
            r"C:\1.0.0",
            "1.0.0/child",
            r"1.0.0\child",
            "\N{FULLWIDTH DIGIT ONE}.0.0",
            "1.0.0 ",
            "1.0.0-rc.",
            "1.0.0-a..b",
            "1.0.0-01",
            "1.0.0-rc." + ("1" * 32),
            b"1.0.0",
            Path("1.0.0"),
            None,
        )
        with tempfile.TemporaryDirectory() as directory:
            storage = LocalPluginPackageStorage(Path(directory) / "storage")
            for version in invalid_versions:
                with self.subTest(version=version), mock.patch.object(
                    storage,
                    "_read_verified_cas_blob",
                ) as read_blob:
                    with self.assertRaises(PluginPackageError):
                        storage.rollback("demo", version, "a" * 64)
                    read_blob.assert_not_called()

    def test_storage_rejects_invalid_retention_segments_before_enumeration(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = LocalPluginPackageStorage(Path(directory) / "storage")
            for arguments in (
                {"slug": "../escape"},
                {"slug": "demo", "current": "../1.0.0"},
                {"slug": "demo", "previous": r"..\1.0.0"},
            ):
                with self.subTest(arguments=arguments), self.assertRaises(
                    PluginPackageError
                ):
                    storage.retain_versions(**arguments)

    def test_storage_validates_slug_at_every_public_path_entry(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = LocalPluginPackageStorage(Path(directory) / "storage")
            operations = (
                lambda: storage.retain_versions("../escape"),
                lambda: storage.list_versions("../escape"),
                lambda: storage.rollback("../escape", "1.0.0", "a" * 64),
                lambda: storage.delete_plugin("../escape"),
            )
            with mock.patch.object(
                storage,
                "_read_verified_cas_blob",
            ) as read_blob, mock.patch.object(
                package_module,
                "remove_secure_tree",
            ) as remove:
                for operation in operations:
                    with self.subTest(operation=operation), self.assertRaises(
                        PluginPackageError
                    ):
                        operation()
                read_blob.assert_not_called()
                remove.assert_not_called()

    def test_storage_accepts_database_width_canonical_segments(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = LocalPluginPackageStorage(Path(directory) / "storage")
            slug = "a" * 80
            version = "1.0.0-rc." + ("1" * 31)

            retained = storage.retain_versions(slug, current=version, keep=1)

            self.assertEqual(retained, [version])

    def test_storage_sorts_the_complete_shared_semver_prerelease_subset(self):
        versions = (
            "1.0.0-0A",
            "1.0.0-a-b",
            "1.0.0-x.7.z.92",
        )
        with tempfile.TemporaryDirectory() as directory:
            storage = LocalPluginPackageStorage(Path(directory) / "storage")
            for version in versions:
                package_module.ensure_directory(
                    storage.root,
                    storage.runtime / "demo" / version,
                    mode=package_module.RUNTIME_DIRECTORY_MODE,
                )

            self.assertEqual(
                storage.list_versions("demo"),
                list(reversed(versions)),
            )

    def test_full_retention_set_removes_noncanonical_stale_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = LocalPluginPackageStorage(Path(directory) / "storage")
            stale = storage.runtime / "demo" / "legacy+garbage"
            package_module.ensure_directory(storage.root, stale)

            retained = storage.retain_versions(
                "demo",
                current="1.1.0",
                previous="1.0.0",
                keep=2,
            )

            self.assertEqual(retained, ["1.1.0", "1.0.0"])
            self.assertFalse(stale.exists())

    def test_static_scan_reads_nfd_member_through_normalized_zipinfo_plan(self):
        manifest = json.dumps(valid_manifest(), separators=(",", ":"))
        source = "value = 1"
        files = [
            {
                "path": "manifest.json",
                "size": len(manifest.encode()),
                "sha256": hashlib.sha256(manifest.encode()).hexdigest(),
            },
            {
                "path": "backend/caf\u00e9.py",
                "size": len(source.encode()),
                "sha256": hashlib.sha256(source.encode()).hexdigest(),
            },
        ]
        index = json.dumps(
            {
                "packageVersion": 1,
                "pluginId": "com.example.demo",
                "slug": "demo",
                "version": "1.0.0",
                "files": files,
            }
        )
        payload = make_package(
            ("manifest.json", manifest),
            ("backend/cafe\u0301.py", source),
            ("package-index.json", index),
        )
        inspected = inspect_package(payload)

        report = static_security_scan(payload, inspected)

        self.assertEqual(report["dangerous_findings"], [])
        self.assertIn("backend/caf\u00e9.py", {item["path"] for item in inspected["files"]})

    def test_submit_payload_rejects_hardlinked_cas_blob(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "storage"
            storage = LocalPluginPackageStorage(root)
            payload = indexed_package(("frontend/assets/state.txt", "safe"))
            inspected = inspect_package(payload)
            source = storage.store_package(payload, sha256=inspected["sha256"])
            os.link(source, root / "cas-alias.ajplugin")
            version = SimpleNamespace(
                package_blob=SimpleNamespace(sha256=inspected["sha256"]),
                manifest_snapshot=inspected["manifest"],
            )

            with self.settings(PLUGIN_ROOT=root), self.assertRaises(
                PluginWorkflowError
            ):
                services_module._payload_for_version(version)

    def test_submit_payload_rejects_path_replacement_before_open(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "storage"
            storage = LocalPluginPackageStorage(root)
            payload = indexed_package(("frontend/assets/state.txt", "safe"))
            replacement = indexed_package(
                ("frontend/assets/state.txt", "replacement")
            )
            inspected = inspect_package(payload)
            source = storage.store_package(payload, sha256=inspected["sha256"])
            replacement_path = root / "replacement.ajplugin"
            replacement_path.write_bytes(replacement)
            displaced = root / "displaced.ajplugin"
            version = SimpleNamespace(
                package_blob=SimpleNamespace(sha256=inspected["sha256"]),
                manifest_snapshot=inspected["manifest"],
            )
            real_open = package_module.os.open
            replaced = False

            def replace_before_open(path, flags, *args, **kwargs):
                nonlocal replaced
                if not replaced and Path(path) == source:
                    replaced = True
                    source.replace(displaced)
                    replacement_path.replace(source)
                return real_open(path, flags, *args, **kwargs)

            with self.settings(PLUGIN_ROOT=root), mock.patch.object(
                package_module.os,
                "open",
                side_effect=replace_before_open,
            ), self.assertRaises(PluginWorkflowError):
                services_module._payload_for_version(version)

    def test_installer_revalidates_local_header_before_writing_runtime(self):
        payload = indexed_package(("frontend/assets/icon.txt", "ok"))
        inspected = inspect_package(payload)
        encoded = bytearray(payload)
        local = encoded.find(b"PK\x03\x04")
        self.assertGreaterEqual(local, 0)
        struct.pack_into("<L", encoded, local + 14, 0)

        with tempfile.TemporaryDirectory() as directory:
            installer = PluginPackageInstaller(Path(directory) / "storage")
            destination = installer.storage.staging / "runtime"
            with self.assertRaises(PluginInstallError):
                installer._extract(
                    bytes(encoded),
                    inspected,
                    destination,
                )

    def test_rejects_raw_nul_filename_hidden_by_zipinfo_alias(self):
        manifest = json.dumps(valid_manifest(), separators=(",", ":"))
        files = [
            {
                "path": "manifest.json",
                "size": len(manifest.encode()),
                "sha256": hashlib.sha256(manifest.encode()).hexdigest(),
            }
        ]
        index = json.dumps(
            {
                "packageVersion": 1,
                "pluginId": "com.example.demo",
                "slug": "demo",
                "version": "1.0.0",
                "files": files,
            }
        )
        payload = make_package(
            ("manifest.jsonX", manifest),
            ("package-index.json", index),
        ).replace(b"manifest.jsonX", b"manifest.json\x00")

        with self.assertRaises(PluginPackageError):
            inspect_package(payload)

    def test_rejects_forged_size_and_crc_that_hide_deflate_growth(self):
        manifest = json.dumps(valid_manifest(), separators=(",", ":"))
        declared = b"A"
        files = [
            {
                "path": "manifest.json",
                "size": len(manifest.encode()),
                "sha256": hashlib.sha256(manifest.encode()).hexdigest(),
            },
            {
                "path": "frontend/assets/payload.txt",
                "size": len(declared),
                "sha256": hashlib.sha256(declared).hexdigest(),
            },
        ]
        index = json.dumps(
            {
                "packageVersion": 1,
                "pluginId": "com.example.demo",
                "slug": "demo",
                "version": "1.0.0",
                "files": files,
            }
        )
        payload = make_package(
            ("manifest.json", manifest),
            ("frontend/assets/payload.txt", b"A" * (2 * 1024 * 1024)),
            ("package-index.json", index),
        )
        payload = forge_declared_identity(
            payload,
            "frontend/assets/payload.txt",
            declared,
        )

        with self.assertRaises(PluginPackageError):
            inspect_package(payload)

    def test_rollback_extracts_the_same_verified_cas_bytes_after_path_swap(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = LocalPluginPackageStorage(Path(directory) / "storage")
            original = indexed_package(("frontend/assets/state.txt", "original"))
            replacement = indexed_package(("frontend/assets/state.txt", "attacker"))
            digest = hashlib.sha256(original).hexdigest()
            source = storage.store_package(original, sha256=digest)
            real_inspect = package_module.inspect_package

            def replace_path_after_inspection(payload):
                result = real_inspect(payload)
                source.write_bytes(replacement)
                return result

            with mock.patch.object(
                package_module,
                "inspect_package",
                side_effect=replace_path_after_inspection,
            ):
                destination = storage.rollback("demo", "1.0.0", digest)

            self.assertEqual(source.read_bytes(), replacement)
            self.assertEqual(
                (destination / "frontend" / "assets" / "state.txt").read_text(
                    encoding="utf-8"
                ),
                "original",
            )

    def test_rejects_traversal(self):
        with self.assertRaises(PluginPackageError):
            inspect_package(make_package(("../escape", "x")))

    def test_rejects_duplicate_member(self):
        payload = make_package(
            ("manifest.json", '{"schemaVersion":2,"sdkApi":2,"id":"com.example.demo","slug":"demo","name":"Demo","version":"1.0.0","description":"Demo","author":{"name":"Example"},"license":"MIT","installationMode":"user","runtimes":[],"extensions":[],"permissions":[],"hooks":[],"dataPolicy":{"storesPersonalData":false,"usesExternalNetwork":false,"acceptsFileUploads":false,"retainsDataOnDisable":true}}'),
            ("manifest.json", "{}"),
        )
        with self.assertRaises(PluginPackageError):
            inspect_package(payload)

    def test_accepts_integrity_indexed_package(self):
        result = inspect_package(indexed_package(("frontend/assets/icon.txt", "ok")))
        self.assertEqual(result["manifest"]["slug"], "demo")

    def test_rejects_sha_mismatch(self):
        manifest = json.dumps(valid_manifest(), separators=(",", ":"))
        files = [
            {"path": "manifest.json", "size": len(manifest.encode()), "sha256": hashlib.sha256(manifest.encode()).hexdigest()},
            {"path": "frontend/assets/icon.txt", "size": 2, "sha256": "0" * 64},
        ]
        index = json.dumps({"packageVersion": 1, "pluginId": "com.example.demo", "slug": "demo", "version": "1.0.0", "files": files})
        payload = make_package(("manifest.json", manifest), ("frontend/assets/icon.txt", "ok"), ("package-index.json", index))
        with self.assertRaises(PluginPackageError):
            inspect_package(payload)

    def test_rejects_undeclared_extra_file(self):
        base = indexed_package()
        with ZipFile(BytesIO(base)) as source:
            members = [(info.filename, source.read(info)) for info in source.infolist()]
        buffer = BytesIO()
        with ZipFile(buffer, "w", ZIP_DEFLATED) as archive:
            for name, value in members:
                archive.writestr(name, value)
            archive.writestr("frontend/assets/extra.txt", b"extra")
        with self.assertRaises(PluginPackageError):
            inspect_package(buffer.getvalue())

    def test_rejects_symlink_member(self):
        info = ZipInfo("frontend/assets/link")
        info.create_system = 3
        info.external_attr = 0o120777 << 16
        buffer = BytesIO()
        with ZipFile(buffer, "w", ZIP_DEFLATED) as archive:
            archive.writestr(info, "target")
        with self.assertRaises(PluginPackageError):
            inspect_package(buffer.getvalue())

    def test_rejects_case_collision(self):
        payload = indexed_package(("frontend/assets/Icon.txt", "one"), ("frontend/assets/icon.txt", "two"))
        with self.assertRaises(PluginPackageError):
            inspect_package(payload)

    def test_rejects_nfc_casefold_collision(self):
        payload = indexed_package(
            ("frontend/assets/caf\u00e9.txt", "one"),
            ("frontend/assets/cafe\u0301.txt", "two"),
        )
        with self.assertRaises(PluginPackageError):
            inspect_package(payload)

    @override_settings(PLUGIN_MAX_COMPRESSION_RATIO=2)
    def test_rejects_suspicious_compression_ratio(self):
        payload = indexed_package(("frontend/assets/repeated.txt", "A" * 4096))
        with self.assertRaises(PluginPackageError):
            inspect_package(payload)

    def test_rejects_malformed_package_index_entry(self):
        manifest = json.dumps(valid_manifest(), separators=(",", ":"))
        index = json.dumps({
            "packageVersion": 1,
            "pluginId": "com.example.demo",
            "slug": "demo",
            "version": "1.0.0",
            "files": [{"path": "manifest.json", "size": True, "sha256": "0" * 64}],
        })
        payload = make_package(("manifest.json", manifest), ("package-index.json", index))
        with self.assertRaises(PluginPackageError):
            inspect_package(payload)
