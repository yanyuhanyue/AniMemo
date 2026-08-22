import hashlib
import json
import tempfile
from io import BytesIO
from pathlib import Path
from unittest import mock
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from django.test import SimpleTestCase, override_settings

from plugin_host import package as package_module
from plugin_host.package import (
    LocalPluginPackageStorage,
    PluginPackageError,
    inspect_package,
)


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


class PackageSecurityTests(SimpleTestCase):
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
