import hashlib
import json
import os
import struct
import tempfile
import zlib
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from django.test import SimpleTestCase, override_settings

from plugin_host import package as package_module
from plugin_host import services as services_module
from plugin_host.installer import PluginInstallError, PluginPackageInstaller
from plugin_host.package import (
    LocalPluginPackageStorage,
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


class PackageSecurityTests(SimpleTestCase):
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
            destination = Path(directory) / "runtime"
            with self.assertRaises(PluginInstallError):
                PluginPackageInstaller._extract(
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
