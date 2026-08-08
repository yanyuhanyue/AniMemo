import hashlib
import json
import tempfile
from io import BytesIO
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from plugin_host.installer import PluginPackageInstaller
from plugin_host.models import PluginDeployment, PluginProject, PluginVersion, UserPluginInstallation
from plugin_host.permissions import can_access_plugin_backend
from plugin_host.services import install_for_user, submit_version, upload_plugin_version, review_submission
from plugin_host.runtime import runtime_registry


def make_package(slug="runtime-test", version="1.0.0", *, runtimes=None, backend_source="", frontend_source="export default {};", exposure="authenticated"):
    runtimes = runtimes or ["frontend", "backend"]
    manifest = {
        "schemaVersion": 2, "sdkApi": 2, "id": f"com.example.{slug}", "slug": slug,
        "name": slug, "version": version, "description": "test plugin", "runtimes": runtimes,
        "author": {"name": "Example"}, "license": "MIT", "installationMode": "user",
        "frontend": {"exposure": exposure}, "extensions": ["frontend.page"] + (["backend.api"] if "backend" in runtimes else []),
        "permissions": [], "hooks": [], "settings": [],
        "dataPolicy": {"storesPersonalData": False, "usesExternalNetwork": False, "acceptsFileUploads": False, "retainsDataOnDisable": True},
    }
    if "backend" in runtimes:
        manifest["backend"] = {"entry": "backend/plugin.py"}
    files = {"manifest.json": json.dumps(manifest, separators=(",", ":")).encode(), "frontend/plugin.js": frontend_source.encode()}
    if "backend" in runtimes:
        files["backend/plugin.py"] = backend_source.encode()
    index_files = [{"path": name, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()} for name, data in files.items()]
    index = json.dumps({"packageVersion": 1, "pluginId": manifest["id"], "slug": slug, "version": version, "files": index_files}, separators=(",", ":")).encode()
    output = BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        for name, data in files.items():
            archive.writestr(name, data)
        archive.writestr("package-index.json", index)
    return output.getvalue(), manifest


class PluginRuntimeV3Tests(TestCase):
    def setUp(self):
        self.root = tempfile.TemporaryDirectory()
        self.settings = override_settings(PLUGIN_ROOT=Path(self.root.name), PLUGIN_MIN_FREE_DISK_MB=0)
        self.settings.enable()
        self.user = get_user_model().objects.create_user("user", "user@example.com", "password-123")
        self.admin = get_user_model().objects.create_superuser("admin", "admin@example.com", "password-123")

    def tearDown(self):
        runtime_registry.clear()
        self.settings.disable()
        self.root.cleanup()

    def create_version(self, slug, version, source, *, owner=None, runtimes=None):
        payload, manifest = make_package(slug, version, runtimes=runtimes, backend_source=source)
        project = PluginProject.objects.create(plugin_id=manifest["id"], slug=slug, name=slug, description="test", owner=owner or self.user)
        version_row, _, _ = upload_plugin_version(project, type("Upload", (), {"name": f"{slug}.ajplugin", "read": lambda self: payload})(), actor=owner or self.user)
        return project, version_row

    def test_backend_draft_is_scanned_without_import(self):
        marker = Path(self.root.name) / "imported"
        source = f"from pathlib import Path\nPath(r'{marker}').write_text('executed')\ndef create_plugin(host):\n    raise AssertionError('must not load')\n"
        project, version = self.create_version("draft-safe", "1.0.0", source)
        self.assertFalse(marker.exists())
        self.assertEqual(version.review_status, PluginVersion.ReviewStatus.DRAFT)

    def test_review_publish_and_user_install_share_one_runtime(self):
        source = "from rest_framework.response import Response\nclass P:\n    def __init__(self, host): host.api.get('ping', handler=self.ping, access='user')\n    def ping(self, request): return Response({'user': request.user.username})\ndef create_plugin(host): return P(host)\n"
        project, version = self.create_version("shared-runtime", "1.0.0", source)
        submission = submit_version(version, actor=self.user)
        review_submission(submission, actor=self.admin, approve=True)
        PluginPackageInstaller().publish(version, actor=self.admin)
        install_for_user(project, user=self.user)
        other = get_user_model().objects.create_user("other", "other@example.com", "password-123")
        install_for_user(project, user=other)
        self.assertEqual(UserPluginInstallation.objects.filter(plugin=project).count(), 2)
        self.assertEqual(len(list((Path(self.root.name) / "runtime" / "shared-runtime").iterdir())), 1)
        self.assertEqual(runtime_registry.active_version("shared-runtime"), "1.0.0")

    def test_user_route_requires_enabled_installation(self):
        source = "from rest_framework.response import Response\nclass P:\n    def __init__(self, host): host.api.get('ping', handler=self.ping, access='user')\n    def ping(self, request): return Response({'ok': True})\ndef create_plugin(host): return P(host)\n"
        project, version = self.create_version("user-route", "1.0.0", source)
        version.review_status = PluginVersion.ReviewStatus.APPROVED
        version.save(update_fields=["review_status"])
        PluginPackageInstaller().publish(version, actor=self.admin)
        client = APIClient()
        client.force_authenticate(self.user)
        self.assertEqual(client.get("/api/plugins/user-route/ping").status_code, 403)
        installation, _ = install_for_user(project, user=self.user)
        self.assertEqual(client.get("/api/plugins/user-route/ping").status_code, 200)
        installation.enabled = False
        installation.save(update_fields=["enabled"])
        self.assertEqual(client.get("/api/plugins/user-route/ping").status_code, 403)

    def test_one_hundred_users_share_one_package_and_runtime_tree(self):
        source = "from rest_framework.response import Response\nclass P:\n    def __init__(self, host): host.api.get('ping', handler=self.ping, access='user')\n    def ping(self, request): return Response({'ok': True})\n    @staticmethod\n    def health_check(): return True\ndef create_plugin(host): return P(host)\n"
        project, version = self.create_version("hundred-users", "1.0.0", source)
        version.review_status = PluginVersion.ReviewStatus.APPROVED
        version.save(update_fields=["review_status"])
        PluginPackageInstaller().publish(version, actor=self.admin)
        root = Path(self.root.name)
        before_files = sorted(path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file())
        before_bytes = sum(path.stat().st_size for path in root.rglob("*") if path.is_file())

        users = [get_user_model()(username=f"shared-{index}", email=f"shared-{index}@example.com") for index in range(100)]
        get_user_model().objects.bulk_create(users)
        for user in get_user_model().objects.filter(username__startswith="shared-"):
            install_for_user(project, user=user)

        after_files = sorted(path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file())
        after_bytes = sum(path.stat().st_size for path in root.rglob("*") if path.is_file())
        self.assertEqual(UserPluginInstallation.objects.filter(plugin=project).count(), 100)
        self.assertEqual(before_files, after_files)
        self.assertEqual(before_bytes, after_bytes)
        self.assertEqual(len(list((root / "packages" / "sha256").rglob("*.ajplugin"))), 1)
        self.assertEqual(len(list((root / "runtime" / "hundred-users").iterdir())), 1)
