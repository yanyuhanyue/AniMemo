import hashlib
import json
import tempfile
from io import BytesIO
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings

from plugin_host.installer import PluginInstallError, PluginPackageInstaller
from plugin_host.models import PluginInstallation


def make_plugin(version="1.0.0", *, payload=b"runtime"):
    manifest = {
        "schemaVersion": 2,
        "sdkApi": 2,
        "id": "com.example.installer",
        "slug": "installer-test",
        "name": "Installer test",
        "version": version,
        "description": "installer test",
        "runtimes": ["frontend"],
        "frontend": {"exposure": "public"},
        "extensions": ["frontend.page"],
        "permissions": [],
        "hooks": [],
        "dataPolicy": {
            "storesPersonalData": False,
            "usesExternalNetwork": False,
            "acceptsFileUploads": False,
            "retainsDataOnDisable": True,
        },
    }
    manifest_bytes = json.dumps(manifest, separators=(",", ":")).encode()
    plugin_bytes = bytes(payload)
    files = [
        {"path": "manifest.json", "size": len(manifest_bytes), "sha256": hashlib.sha256(manifest_bytes).hexdigest()},
        {"path": "frontend/plugin.js", "size": len(plugin_bytes), "sha256": hashlib.sha256(plugin_bytes).hexdigest()},
    ]
    index = json.dumps({"packageVersion": 1, "pluginId": manifest["id"], "slug": manifest["slug"], "version": version, "files": files}, separators=(",", ":")).encode()
    output = BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", manifest_bytes)
        archive.writestr("frontend/plugin.js", plugin_bytes)
        archive.writestr("package-index.json", index)
    return output.getvalue()


@override_settings(PLUGIN_MIN_FREE_DISK_MB=0)
class PluginInstallerTests(TestCase):
    def test_upgrade_failure_preserves_current_runtime(self):
        with tempfile.TemporaryDirectory() as directory:
            installer = PluginPackageInstaller(Path(directory))
            first = SimpleUploadedFile("installer-test.ajplugin", make_plugin(), content_type="application/zip")
            installer.install(first)
            installation = PluginInstallation.objects.get(slug="installer-test")
            self.assertEqual(installation.current_version, "1.0.0")
            current = Path(directory) / "runtime" / "installer-test" / "1.0.0" / "frontend" / "plugin.js"
            self.assertEqual(current.read_bytes(), b"runtime")

            broken = SimpleUploadedFile("installer-test.ajplugin", b"not-a-package", content_type="application/zip")
            with self.assertRaises(PluginInstallError):
                installer.install(broken, replace=True)
            installation.refresh_from_db()
            self.assertEqual(installation.current_version, "1.0.0")
            self.assertTrue(current.is_file())
            self.assertEqual(current.read_bytes(), b"runtime")
