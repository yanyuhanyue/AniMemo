import hashlib
import json
import tempfile
from io import BytesIO
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from plugin_host.installer import PluginInstallError, PluginPackageInstaller
from plugin_host.models import PluginDeployment, PluginProject, PluginVersion, UserPluginInstallation
from plugin_host.permissions import can_access_plugin_backend
from plugin_host.services import install_for_user, submit_version, upload_plugin_version, review_submission
from plugin_host.runtime import RuntimeRegistry, RuntimeUnavailable, runtime_registry
from plugin_host.sdk.types import JournalHookContext


def make_package(
    slug="runtime-test",
    version="1.0.0",
    *,
    runtimes=None,
    backend_source="",
    frontend_source="export default {};",
    exposure="authenticated",
    installation_mode="user",
    plugin_id=None,
    hooks=None,
    rollback_floor=None,
):
    runtimes = runtimes or ["frontend", "backend"]
    hooks = hooks or []
    manifest = {
        "schemaVersion": 2, "sdkApi": 2, "id": plugin_id or f"com.example.{slug}", "slug": slug,
        "name": slug, "version": version, "description": "test plugin", "runtimes": runtimes,
        "author": {"name": "Example"}, "license": "MIT", "installationMode": installation_mode,
        "frontend": {"exposure": exposure}, "extensions": ["frontend.page"] + (["backend.api"] if "backend" in runtimes else []) + (["hooks"] if hooks else []),
        "permissions": [], "hooks": hooks, "settings": [],
        "dataPolicy": {"storesPersonalData": False, "usesExternalNetwork": False, "acceptsFileUploads": False, "retainsDataOnDisable": True},
    }
    if "backend" in runtimes:
        manifest["backend"] = {"entry": "backend/plugin.py"}
    if rollback_floor:
        manifest["dataCompatibility"] = {"rollbackFloor": rollback_floor}
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

    def upload_version(self, project, version, source, *, hooks=None, rollback_floor=None):
        payload, _ = make_package(
            project.slug,
            version,
            backend_source=source,
            plugin_id=project.plugin_id,
            installation_mode=project.installation_mode,
            hooks=hooks,
            rollback_floor=rollback_floor,
        )
        uploaded = type(
            "Upload",
            (),
            {"name": f"{project.slug}-{version}.ajplugin", "read": lambda self: payload},
        )()
        version_row, _, _ = upload_plugin_version(project, uploaded, actor=project.owner or self.admin)
        version_row.review_status = PluginVersion.ReviewStatus.APPROVED
        version_row.save(update_fields=["review_status"])
        return version_row

    @staticmethod
    def hook_runtime_source():
        return """class P:
    def __init__(self, host):
        self.host = host
        host.api.get('ping', handler=self.ping, access='user')
        host.register_hook('journal.after_create', self.after_create)
    def ping(self, request):
        return {'version': self.host.version}
    def after_create(self, context):
        return self.host.version
    def health_check(self):
        return True
def create_plugin(host):
    return P(host)
"""

    def hook_project(self, slug):
        return PluginProject.objects.create(
            plugin_id=f"com.example.{slug}",
            slug=slug,
            name=slug,
            description="runtime invariant",
            owner=self.user,
        )

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

    def test_user_plugin_assets_routes_and_hooks_share_the_same_tenant_boundary(self):
        source = """from rest_framework.response import Response
class P:
    def __init__(self, host):
        self.host = host
        host.api.get('ping', handler=self.ping, access='user')
        host.register_hook('journal.after_create', self.after_create)
    def ping(self, request):
        return Response({'user': request.user.username})
    def after_create(self, context):
        return context.user_id
def create_plugin(host):
    return P(host)
"""
        payload, manifest = make_package(
            "tenant-e2e",
            "1.0.0",
            backend_source=source,
            hooks=["journal.after_create"],
        )
        project = PluginProject.objects.create(
            plugin_id=manifest["id"], slug=manifest["slug"], name="Tenant E2E",
            description="test", owner=self.user,
        )
        uploaded = type("Upload", (), {"name": "tenant-e2e.ajplugin", "read": lambda self: payload})()
        version, _, _ = upload_plugin_version(project, uploaded, actor=self.user)
        version.review_status = PluginVersion.ReviewStatus.APPROVED
        version.save(update_fields=["review_status"])
        PluginPackageInstaller().publish(version, actor=self.admin)
        install_for_user(project, user=self.user)
        other = get_user_model().objects.create_user("tenant-other", password="password-123")
        user_client = APIClient()
        user_client.force_authenticate(self.user)
        other_client = APIClient()
        other_client.force_authenticate(other)

        self.assertEqual(user_client.get("/api/plugins/tenant-e2e/ping").status_code, 200)
        self.assertEqual(other_client.get("/api/plugins/tenant-e2e/ping").status_code, 403)
        metadata = user_client.get("/api/plugins/enabled/").data["plugins"]
        asset_url = next(item["frontendEntry"] for item in metadata if item["slug"] == project.slug)
        asset = user_client.get(asset_url)
        self.assertEqual(asset.status_code, 200)
        asset.close()
        self.assertFalse(any(item["slug"] == project.slug for item in other_client.get("/api/plugins/enabled/").data["plugins"]))
        denied_asset = other_client.get(f"/plugin-assets/{project.slug}/1.0.0/plugin.js")
        self.assertEqual(denied_asset.status_code, 404)
        self.assertEqual(
            runtime_registry.hooks.run_hook("journal.after_create", JournalHookContext(self.user.pk, 1, "test")),
            [self.user.pk],
        )
        self.assertEqual(
            runtime_registry.hooks.run_hook("journal.after_create", JournalHookContext(other.pk, 2, "test")),
            [],
        )

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

    def test_publish_upgrade_switches_db_runtime_and_hook_owner(self):
        project = self.hook_project("runtime-upgrade")
        first = self.upload_version(project, "1.0.0", self.hook_runtime_source(), hooks=["journal.after_create"])
        second = self.upload_version(project, "1.1.0", self.hook_runtime_source(), hooks=["journal.after_create"])
        installer = PluginPackageInstaller()
        installer.publish(first, actor=self.admin)

        installer.publish(second, actor=self.admin)

        deployment = PluginDeployment.objects.select_related("current_version", "previous_version").get(plugin=project)
        self.assertEqual(deployment.current_version, second)
        self.assertEqual(deployment.previous_version, first)
        self.assertEqual(runtime_registry.active_version(project.slug), "1.1.0")
        self.assertEqual(
            {item.plugin_version for item in runtime_registry.hooks.registrations_for(project.slug)},
            {"1.1.0"},
        )

    def test_failed_publish_keeps_v1_db_runtime_and_hooks(self):
        project = self.hook_project("runtime-failed-upgrade")
        first = self.upload_version(project, "1.0.0", self.hook_runtime_source(), hooks=["journal.after_create"])
        broken = self.upload_version(project, "1.1.0", "raise RuntimeError('broken candidate')")
        installer = PluginPackageInstaller()
        installer.publish(first, actor=self.admin)

        with self.assertRaises(PluginInstallError):
            installer.publish(broken, actor=self.admin)

        deployment = PluginDeployment.objects.select_related("current_version", "previous_version").get(plugin=project)
        self.assertEqual(deployment.current_version, first)
        self.assertIsNone(deployment.previous_version)
        self.assertEqual(runtime_registry.active_version(project.slug), "1.0.0")
        self.assertEqual(
            {item.plugin_version for item in runtime_registry.hooks.registrations_for(project.slug)},
            {"1.0.0"},
        )

    def test_rollback_switches_db_runtime_and_hook_owner(self):
        project = self.hook_project("runtime-rollback")
        first = self.upload_version(project, "1.0.0", self.hook_runtime_source(), hooks=["journal.after_create"])
        second = self.upload_version(project, "1.1.0", self.hook_runtime_source(), hooks=["journal.after_create"])
        installer = PluginPackageInstaller()
        installer.publish(first, actor=self.admin)
        installer.publish(second, actor=self.admin)

        installer.rollback(project.slug, actor=self.admin)

        deployment = PluginDeployment.objects.select_related("current_version", "previous_version").get(plugin=project)
        self.assertEqual(deployment.current_version, first)
        self.assertEqual(deployment.previous_version, second)
        self.assertEqual(runtime_registry.active_version(project.slug), "1.0.0")
        self.assertEqual(
            {item.plugin_version for item in runtime_registry.hooks.registrations_for(project.slug)},
            {"1.0.0"},
        )

    def test_data_compatibility_floor_denies_incompatible_rollback(self):
        project = self.hook_project("runtime-floor")
        first = self.upload_version(project, "1.0.0", self.hook_runtime_source(), hooks=["journal.after_create"])
        second = self.upload_version(
            project,
            "2.0.0",
            self.hook_runtime_source(),
            hooks=["journal.after_create"],
            rollback_floor="2.0.0",
        )
        installer = PluginPackageInstaller()
        installer.publish(first, actor=self.admin)
        installer.publish(second, actor=self.admin)

        with self.assertRaisesMessage(PluginInstallError, "低于数据兼容下限 2.0.0"):
            installer.rollback(project.slug, actor=self.admin)

        deployment = PluginDeployment.objects.select_related("current_version", "previous_version").get(plugin=project)
        self.assertEqual(deployment.current_version, second)
        self.assertEqual(deployment.previous_version, first)
        self.assertEqual(deployment.rollback_floor, "2.0.0")
        self.assertEqual(runtime_registry.active_version(project.slug), "2.0.0")

    def test_data_compatibility_floor_is_monotonic_across_later_publish(self):
        project = self.hook_project("runtime-floor-forward")
        first = self.upload_version(
            project,
            "2.0.0",
            self.hook_runtime_source(),
            hooks=["journal.after_create"],
            rollback_floor="2.0.0",
        )
        second = self.upload_version(project, "2.1.0", self.hook_runtime_source(), hooks=["journal.after_create"])
        installer = PluginPackageInstaller()
        installer.publish(first, actor=self.admin)
        installer.publish(second, actor=self.admin)

        deployment = PluginDeployment.objects.get(plugin=project)
        self.assertEqual(deployment.current_version, second)
        self.assertEqual(deployment.rollback_floor, "2.0.0")

    def test_disable_and_enable_unload_and_restore_current_runtime(self):
        project = self.hook_project("runtime-toggle")
        version = self.upload_version(project, "1.0.0", self.hook_runtime_source(), hooks=["journal.after_create"])
        installer = PluginPackageInstaller()
        installer.publish(version, actor=self.admin)

        installer.set_enabled(project.slug, False, actor=self.admin)
        self.assertIsNone(runtime_registry.active_version(project.slug))
        self.assertEqual(runtime_registry.hooks.registrations_for(project.slug), ())

        installer.set_enabled(project.slug, True, actor=self.admin)
        self.assertEqual(runtime_registry.active_version(project.slug), "1.0.0")
        self.assertEqual(
            {item.plugin_version for item in runtime_registry.hooks.registrations_for(project.slug)},
            {"1.0.0"},
        )

    def test_second_registry_lazily_reconciles_upgrade_and_disable(self):
        project = self.hook_project("runtime-worker")
        first = self.upload_version(project, "1.0.0", self.hook_runtime_source(), hooks=["journal.after_create"])
        second = self.upload_version(project, "1.1.0", self.hook_runtime_source(), hooks=["journal.after_create"])
        installer = PluginPackageInstaller()
        installer.publish(first, actor=self.admin)
        install_for_user(project, user=self.user)
        worker = RuntimeRegistry()
        try:
            worker.hooks.run_hook("journal.after_create", JournalHookContext(self.user.pk, 1, "worker"))
            self.assertEqual(worker.active_version(project.slug), "1.0.0")

            installer.publish(second, actor=self.admin)
            worker.hooks.run_hook("journal.after_create", JournalHookContext(self.user.pk, 2, "worker"))
            self.assertEqual(worker.active_version(project.slug), "1.1.0")
            self.assertEqual(
                {item.plugin_version for item in worker.hooks.registrations_for(project.slug)},
                {"1.1.0"},
            )

            installer.set_enabled(project.slug, False, actor=self.admin)
            worker.hooks.run_hook("journal.after_create", JournalHookContext(self.user.pk, 3, "worker"))
            self.assertIsNone(worker.active_version(project.slug))
            self.assertEqual(worker.hooks.registrations_for(project.slug), ())
        finally:
            worker.clear()

    def test_stale_deployment_hint_cannot_restore_old_runtime(self):
        project = self.hook_project("runtime-stale-hint")
        first = self.upload_version(project, "1.0.0", self.hook_runtime_source(), hooks=["journal.after_create"])
        second = self.upload_version(project, "1.1.0", self.hook_runtime_source(), hooks=["journal.after_create"])
        installer = PluginPackageInstaller()
        installer.publish(first, actor=self.admin)
        stale = PluginDeployment.objects.select_related("current_version").get(plugin=project)

        installer.publish(second, actor=self.admin)
        self.assertEqual(stale.current_version.version, "1.0.0")
        self.assertEqual(runtime_registry.ensure_current(project.slug).version, "1.1.0")

        installer.rollback(project.slug, actor=self.admin)
        self.assertEqual(runtime_registry.ensure_current(project.slug).version, "1.0.0")

        installer.set_enabled(project.slug, False, actor=self.admin)
        with self.assertRaises(RuntimeUnavailable):
            runtime_registry.ensure_current(project.slug)
        self.assertIsNone(runtime_registry.active_version(project.slug))
