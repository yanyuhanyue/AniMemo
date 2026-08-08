import hashlib
import json
import tempfile
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from zipfile import ZIP_DEFLATED, ZipFile

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from plugin_host.installer import PluginInstallError, PluginPackageInstaller
from plugin_host.models import PluginProject, PluginVersion
from plugin_host.package import inspect_package
from plugin_host.runtime import runtime_registry
from plugin_host.services import store_package_blob


def make_plugin(version="1.0.0", *, payload=b"runtime", backend_source=None, hooks=None):
    hooks = list(hooks or [])
    runtimes = ["frontend"] + (["backend"] if backend_source is not None else [])
    manifest = {
        "schemaVersion": 2, "sdkApi": 2, "id": "com.example.installer", "slug": "installer-test",
        "name": "Installer test", "version": version, "description": "installer test",
        "author": {"name": "Example"}, "license": "MIT", "installationMode": "user",
        "runtimes": runtimes, "frontend": {"exposure": "public"},
        "extensions": ["frontend.page"] + (["backend.api"] if backend_source is not None else []) + (["hooks"] if hooks else []),
        "permissions": [], "hooks": hooks, "settings": [],
        "dataPolicy": {"storesPersonalData": False, "usesExternalNetwork": False, "acceptsFileUploads": False, "retainsDataOnDisable": True},
    }
    if backend_source is not None:
        manifest["backend"] = {"entry": "backend/plugin.py"}
    manifest_bytes = json.dumps(manifest, separators=(",", ":")).encode()
    package_files = {"manifest.json": manifest_bytes, "frontend/plugin.js": payload}
    if backend_source is not None:
        package_files["backend/plugin.py"] = backend_source.encode()
    files = [
        {"path": path, "size": len(content), "sha256": hashlib.sha256(content).hexdigest()}
        for path, content in package_files.items()
    ]
    index = json.dumps({"packageVersion": 1, "pluginId": manifest["id"], "slug": manifest["slug"], "version": version, "files": files}, separators=(",", ":")).encode()
    output = BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        for path, content in package_files.items():
            archive.writestr(path, content)
        archive.writestr("package-index.json", index)
    return output.getvalue()


@override_settings(PLUGIN_MIN_FREE_DISK_MB=0)
class PluginInstallerTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_superuser("admin", "admin@example.com", "password-123")
        self.project = PluginProject.objects.create(
            plugin_id="com.example.installer", slug="installer-test", name="Installer test", description="test", owner=self.user,
        )

    def tearDown(self):
        runtime_registry.clear()

    def _version(self, version="1.0.0", payload=b"runtime", *, backend_source=None, hooks=None):
        blob, inspected, _, _ = store_package_blob(
            make_plugin(version, payload=payload, backend_source=backend_source, hooks=hooks)
        )
        return PluginVersion.objects.create(
            plugin=self.project, version=version, package_blob=blob, manifest_snapshot=inspected["manifest"],
            runtime_types=inspected["manifest"]["runtimes"],
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
            hook_source = """class P:
    def __init__(self, host):
        self.host = host
        host.api.get('ping', handler=self.ping, access='user')
        host.register_hook('journal.after_create', self.after_create)
    def ping(self, request):
        return {'ok': True}
    def after_create(self, context):
        return self.host.version
    def health_check(self):
        return True
def create_plugin(host):
    return P(host)
"""
            first = self._version(
                payload=b"first",
                backend_source=hook_source,
                hooks=["journal.after_create"],
            )
            installer = PluginPackageInstaller(Path(directory))
            installer.publish(first, actor=self.user)
            current = Path(directory) / "runtime" / "installer-test" / "1.0.0" / "frontend" / "plugin.js"
            self.assertEqual(current.read_bytes(), b"first")
            hooks_before = runtime_registry.hooks.registrations_for("installer-test")
            self.assertEqual({item.plugin_version for item in hooks_before}, {"1.0.0"})

            second = self._version(
                "1.1.0",
                payload=b"second",
                backend_source=hook_source,
                hooks=["journal.after_create"],
            )
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
            self.assertEqual(runtime_registry.active_version("installer-test"), "1.0.0")
            self.assertEqual(runtime_registry.hooks.registrations_for("installer-test"), hooks_before)
