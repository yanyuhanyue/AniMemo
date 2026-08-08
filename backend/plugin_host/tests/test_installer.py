import hashlib
import json
import tempfile
from io import BytesIO
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from types import SimpleNamespace
from unittest.mock import patch

from plugin_host.installer import PluginInstallError, PluginPackageInstaller
from plugin_host.models import PluginProject, PluginVersion
from plugin_host.package import inspect_package
from plugin_host.services import store_package_blob


def make_plugin(version="1.0.0", *, payload=b"runtime"):
    manifest = {
        "schemaVersion": 2, "sdkApi": 2, "id": "com.example.installer", "slug": "installer-test",
        "name": "Installer test", "version": version, "description": "installer test",
        "author": {"name": "Example"}, "license": "MIT", "installationMode": "user",
        "runtimes": ["frontend"], "frontend": {"exposure": "public"}, "extensions": ["frontend.page"],
        "permissions": [], "hooks": [], "settings": [],
        "dataPolicy": {"storesPersonalData": False, "usesExternalNetwork": False, "acceptsFileUploads": False, "retainsDataOnDisable": True},
    }
    manifest_bytes = json.dumps(manifest, separators=(",", ":")).encode()
    files = [
        {"path": "manifest.json", "size": len(manifest_bytes), "sha256": hashlib.sha256(manifest_bytes).hexdigest()},
        {"path": "frontend/plugin.js", "size": len(payload), "sha256": hashlib.sha256(payload).hexdigest()},
    ]
    index = json.dumps({"packageVersion": 1, "pluginId": manifest["id"], "slug": manifest["slug"], "version": version, "files": files}, separators=(",", ":")).encode()
    output = BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", manifest_bytes)
        archive.writestr("frontend/plugin.js", payload)
        archive.writestr("package-index.json", index)
    return output.getvalue()


@override_settings(PLUGIN_MIN_FREE_DISK_MB=0)
class PluginInstallerTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_superuser("admin", "admin@example.com", "password-123")
        self.project = PluginProject.objects.create(
            plugin_id="com.example.installer", slug="installer-test", name="Installer test", description="test", owner=self.user,
        )

    def _version(self, version="1.0.0", payload=b"runtime"):
        blob, inspected, _, _ = store_package_blob(make_plugin(version, payload=payload))
        return PluginVersion.objects.create(
            plugin=self.project, version=version, package_blob=blob, manifest_snapshot=inspected["manifest"], runtime_types=["frontend"],
            review_status=PluginVersion.ReviewStatus.APPROVED, created_by=self.user,
        )

    def test_publish_failure_preserves_current_runtime(self):
        with tempfile.TemporaryDirectory() as directory, override_settings(PLUGIN_ROOT=Path(directory)):
            first = self._version()
            installer = PluginPackageInstaller(Path(directory))
            installer.publish(first, actor=self.user)
            current = Path(directory) / "runtime" / "installer-test" / "1.0.0" / "frontend" / "plugin.js"
            self.assertEqual(current.read_bytes(), b"runtime")
            broken = self._version("1.1.0", payload=b"broken")
            cas_path = Path(directory) / broken.package_blob.storage_path
            cas_path.write_bytes(b"corrupted")
            with self.assertRaises(PluginInstallError):
                installer.publish(broken, actor=self.user)
            self.assertTrue(current.is_file())
            self.assertEqual(current.read_bytes(), b"runtime")

    def test_publish_rejects_staging_and_runtime_peak_growth(self):
        with tempfile.TemporaryDirectory() as directory, override_settings(
            PLUGIN_ROOT=Path(directory), PLUGIN_MIN_FREE_DISK_MB=1,
        ):
            first = self._version(payload=b"first")
            installer = PluginPackageInstaller(Path(directory))
            installer.publish(first, actor=self.user)
            current = Path(directory) / "runtime" / "installer-test" / "1.0.0" / "frontend" / "plugin.js"
            self.assertEqual(current.read_bytes(), b"first")

            second = self._version("1.1.0", payload=b"second")
            inspected_bytes = sum(item["size"] for item in inspect_package(
                (Path(directory) / second.package_blob.storage_path).read_bytes()
            )["files"])
            free = 1024 * 1024 + (inspected_bytes * 2) - 1
            with patch("plugin_host.installer.shutil.disk_usage", return_value=SimpleNamespace(free=free)):
                with self.assertRaises(PluginInstallError):
                    installer.publish(second, actor=self.user)

            deployment = PluginVersion.objects.get(pk=first.pk).plugin.deployment
            self.assertEqual(deployment.current_version_id, first.pk)
            self.assertEqual(current.read_bytes(), b"first")
            self.assertFalse((Path(directory) / "runtime" / "installer-test" / "1.1.0").exists())
