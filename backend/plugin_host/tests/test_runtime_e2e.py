import hashlib
import json
import tempfile
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from pathlib import Path
from threading import Event
from unittest import skipUnless
from urllib.parse import urljoin
from zipfile import ZIP_DEFLATED, ZipFile

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import close_old_connections, connection
from django.test import TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from rest_framework.test import APIClient

from accounts.models import StaffProfile
from plugin_host.installer import PluginInstallError, PluginPackageInstaller
from plugin_host.models import PluginData, PluginInstallation
from plugin_host.runtime import RuntimeRegistry, RuntimeUnavailable, runtime_registry


User = get_user_model()


def make_runtime_plugin(
    slug,
    version="1.0.0",
    *,
    exposure="public",
    backend=True,
    roles=None,
    broken_backend=False,
):
    runtimes = ["frontend"] + (["backend"] if backend else [])
    extensions = ["frontend.page"] + (["backend.api"] if backend else [])
    permission_roles = roles if roles is not None else (["administrator"] if backend else None)
    permissions = [] if permission_roles is None else [{
        "code": f"{slug}.run",
        "name": "Run test plugin",
        "roles": permission_roles,
    }]
    manifest = {
        "schemaVersion": 2,
        "sdkApi": 2,
        "id": f"com.example.{slug}",
        "slug": slug,
        "name": f"{slug} test plugin",
        "version": version,
        "description": "Runtime E2E fixture",
        "runtimes": runtimes,
        "frontend": {"exposure": exposure},
        "extensions": extensions,
        "permissions": permissions,
        "hooks": [],
        "dataPolicy": {
            "storesPersonalData": backend,
            "usesExternalNetwork": False,
            "acceptsFileUploads": False,
            "retainsDataOnDisable": True,
        },
    }
    if backend:
        manifest["backend"] = {"entry": "backend/plugin.py"}

    files = {
        "manifest.json": json.dumps(manifest, separators=(",", ":"), ensure_ascii=False).encode("utf-8"),
        "frontend/plugin.js": f'export const iconUrl = new URL("./assets/icon.svg", import.meta.url).href; export default {{ version: "{version}" }};\n'.encode("utf-8"),
        "frontend/plugin.css": b":root { --runtime-e2e: 1; } .runtime-e2e { background-image: url(\"./assets/bg.webp\"); }\n",
        "frontend/assets/icon.svg": b'<svg xmlns="http://www.w3.org/2000/svg" width="8" height="8"><rect width="8" height="8"/></svg>',
        "frontend/assets/bg.webp": b"RIFFtestWEBP",
    }
    if backend:
        files["backend/plugin.py"] = (
            b"raise RuntimeError('broken candidate')\n"
            if broken_backend
            else f'''class RuntimePlugin:
    def __init__(self, host):
        self.host = host
        permission = "{slug}.run"
        host.api.get("ping", handler=self.ping, permission=permission)
        host.api.get("data", handler=self.data, permission=permission)
        host.api.post("data", handler=self.data, permission=permission)

    def health_check(self):
        return {{"status": "healthy", "version": self.host.version}}

    def ping(self, request):
        return {{"status": "ok", "version": self.host.version}}

    def data(self, request):
        storage = self.host.storage(user=request.user, namespace="e2e")
        if request.method == "POST":
            storage.set("value", request.data.get("value"))
        return {{"value": storage.get("value")}}


def create_plugin(host):
    return RuntimePlugin(host)
'''.encode("utf-8")
        )

    index_files = [
        {"path": name, "size": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}
        for name, payload in files.items()
    ]
    package_index = {
        "packageVersion": 1,
        "pluginId": manifest["id"],
        "slug": slug,
        "version": version,
        "files": index_files,
    }
    output = BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        for name, payload in files.items():
            archive.writestr(name, payload)
        archive.writestr("package-index.json", json.dumps(package_index, separators=(",", ":")).encode("utf-8"))
    return output.getvalue()


def make_custom_runtime_plugin(slug, version, backend_source, *, permissions, hooks=None, exposure="staff"):
    manifest = {
        "schemaVersion": 2,
        "sdkApi": 2,
        "id": f"com.example.{slug}",
        "slug": slug,
        "name": f"{slug} test plugin",
        "version": version,
        "description": "Custom Runtime E2E fixture",
        "runtimes": ["frontend", "backend"],
        "frontend": {"exposure": exposure},
        "extensions": ["frontend.page", "backend.api", *( ["hooks"] if hooks else [])],
        "backend": {"entry": "backend/plugin.py"},
        "permissions": permissions,
        "hooks": hooks or [],
        "dataPolicy": {
            "storesPersonalData": True,
            "usesExternalNetwork": False,
            "acceptsFileUploads": False,
            "retainsDataOnDisable": True,
        },
    }
    files = {
        "manifest.json": json.dumps(manifest, separators=(",", ":"), ensure_ascii=False).encode("utf-8"),
        "frontend/plugin.js": b'export default function createPlugin() { return { routes: [] }; }\n',
        "backend/plugin.py": backend_source.encode("utf-8"),
    }
    package_index = {
        "packageVersion": 1,
        "pluginId": manifest["id"],
        "slug": slug,
        "version": version,
        "files": [
            {"path": name, "size": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}
            for name, payload in files.items()
        ],
    }
    output = BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        for name, payload in files.items():
            archive.writestr(name, payload)
        archive.writestr("package-index.json", json.dumps(package_index, separators=(",", ":")).encode("utf-8"))
    return output.getvalue()


def make_source_plugin_package(plugin_root):
    root = Path(plugin_root)
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    files = {}
    for path in root.rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(root)
        if relative.name == "package-index.json" or "__pycache__" in relative.parts or "tests" in relative.parts:
            continue
        if relative.parts[0] not in {"frontend", "backend"} and relative.name != "manifest.json":
            continue
        if relative.parts[0] == "frontend" and relative.name not in {"plugin.js", "plugin.css"} and relative.parts[1] != "assets":
            continue
        if path.suffix in {".jsx", ".map", ".pyc"}:
            continue
        files[relative.as_posix()] = path.read_bytes()
    package_index = {
        "packageVersion": 1,
        "pluginId": manifest["id"],
        "slug": manifest["slug"],
        "version": manifest["version"],
        "files": [
            {"path": name, "size": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}
            for name, payload in sorted(files.items())
        ],
    }
    output = BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        for name, payload in files.items():
            archive.writestr(name, payload)
        archive.writestr("package-index.json", json.dumps(package_index, separators=(",", ":")).encode("utf-8"))
    return output.getvalue()


class PluginRuntimeE2ETests(TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.plugin_root = Path(self.temporary.name)
        self.settings_override = override_settings(
            PLUGIN_ROOT=self.plugin_root,
            PLUGIN_MIN_FREE_DISK_MB=0,
            PLUGIN_KEEP_VERSIONS=2,
        )
        self.settings_override.enable()
        runtime_registry.clear()
        self.installer = PluginPackageInstaller()
        self.user = User.objects.create_user(username="runtime-user", password="StrongPass123!")
        self.superuser = User.objects.create_superuser(username="runtime-root", password="StrongPass123!")
        self.client = APIClient()

    def tearDown(self):
        runtime_registry.clear()
        self.settings_override.disable()
        self.temporary.cleanup()

    def install(self, slug, version, **kwargs):
        archive = SimpleUploadedFile(
            f"{slug}-{version}.ajplugin",
            make_runtime_plugin(slug, version, **kwargs),
            content_type="application/zip",
        )
        return self.installer.install(
            archive,
            replace=PluginInstallation.objects.filter(slug=slug).exists(),
            actor=self.superuser,
        )

    def test_install_upgrade_failure_rollback_and_reload_use_database_version(self):
        slug = "runtime-test-plugin"
        self.install(slug, "1.0.0")
        self.installer.set_enabled(slug, True, actor=self.superuser)
        self.client.force_authenticate(self.superuser)

        metadata = self.client.get(reverse("enabled-plugins"))
        self.assertEqual(metadata.status_code, 200)
        self.assertEqual(metadata.data["plugins"][0]["version"], "1.0.0")
        ping = self.client.get(f"/api/plugins/{slug}/ping/")
        self.assertEqual(ping.status_code, 200)
        self.assertEqual(ping.data["version"], "1.0.0")

        stored = self.client.post(f"/api/plugins/{slug}/data/", {"value": {"seen": 1}}, format="json")
        self.assertEqual(stored.status_code, 200)
        self.assertEqual(stored.data["value"], {"seen": 1})
        self.assertTrue(PluginData.objects.filter(plugin_slug=slug, namespace="e2e", user=self.superuser).exists())

        self.install(slug, "1.1.0")
        installation = PluginInstallation.objects.get(slug=slug)
        self.assertEqual(installation.current_version, "1.1.0")
        self.assertEqual(installation.previous_version, "1.0.0")
        self.assertTrue(installation.enabled)
        self.assertTrue(installation.healthy)
        self.assertEqual(self.client.get(f"/api/plugins/{slug}/ping/").data["version"], "1.1.0")
        metadata = self.client.get(reverse("enabled-plugins"))
        self.assertEqual(metadata.data["plugins"][0]["version"], "1.1.0")
        self.assertIn(f"/{slug}/1.1.0/", metadata.data["plugins"][0]["frontendEntry"])

        with self.assertRaises(PluginInstallError):
            self.install(slug, "1.2.0", broken_backend=True)
        installation.refresh_from_db()
        self.assertEqual(installation.current_version, "1.1.0")
        self.assertEqual(installation.previous_version, "1.0.0")
        self.assertTrue(installation.healthy)
        self.assertEqual(self.client.get(f"/api/plugins/{slug}/ping/").data["version"], "1.1.0")

        self.client.force_authenticate(self.superuser)
        rollback = self.client.post(reverse("staff-plugin-rollback", kwargs={"slug": slug}))
        self.assertEqual(rollback.status_code, 200)
        installation.refresh_from_db()
        self.assertEqual(installation.current_version, "1.0.0")
        self.assertEqual(installation.previous_version, "1.1.0")

        self.client.force_authenticate(self.superuser)
        self.assertEqual(self.client.get(f"/api/plugins/{slug}/ping/").data["version"], "1.0.0")
        metadata = self.client.get(reverse("enabled-plugins"))
        self.assertEqual(metadata.data["plugins"][0]["version"], "1.0.0")
        self.assertIn(f"/{slug}/1.0.0/", metadata.data["plugins"][0]["frontendEntry"])
        self.assertEqual(self.client.get(f"/api/plugins/{slug}/data/").data["value"], {"seen": 1})

        runtime_versions = {path.name for path in (self.plugin_root / "runtime" / slug).iterdir() if path.is_dir()}
        package_versions = {path.stem for path in (self.plugin_root / "packages" / slug).glob("*.ajplugin")}
        self.assertEqual(runtime_versions, {"1.0.0", "1.1.0"})
        self.assertEqual(package_versions, {"1.0.0", "1.1.0"})

        runtime_registry.clear()
        self.assertEqual(self.client.get(f"/api/plugins/{slug}/ping/").data["version"], "1.0.0")

    def test_cleanup_and_uninstall_remain_available_below_growth_threshold(self):
        slug = "runtime-reclaim-plugin"
        self.install(slug, "1.0.0")
        self.installer.set_enabled(slug, True, actor=self.superuser)
        with override_settings(PLUGIN_MIN_FREE_DISK_MB=10**12):
            cleanup = self.installer.cleanup(slug)
            self.assertIn(slug, cleanup["plugins"])
            snapshot = self.installer.uninstall(slug)
        self.assertEqual(snapshot["slug"], slug)
        self.assertFalse(PluginInstallation.objects.filter(slug=slug).exists())
        self.assertFalse((self.plugin_root / "runtime" / slug).exists())
        self.assertFalse((self.plugin_root / "packages" / slug).exists())


class PluginHandlerPermissionE2ETests(TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.plugin_root = Path(self.temporary.name)
        self.settings_override = override_settings(PLUGIN_ROOT=self.plugin_root, PLUGIN_MIN_FREE_DISK_MB=0)
        self.settings_override.enable()
        runtime_registry.clear()
        self.installer = PluginPackageInstaller()
        self.reviewer = User.objects.create_user(username="permission-reviewer", password="StrongPass123!", is_staff=True)
        self.administrator = User.objects.create_user(username="permission-admin", password="StrongPass123!", is_staff=True)
        self.normal_user = User.objects.create_user(username="permission-user", password="StrongPass123!")
        self.superuser = User.objects.create_superuser(username="permission-root", password="StrongPass123!")
        StaffProfile.objects.create(user=self.reviewer, role=StaffProfile.Role.REVIEWER)
        StaffProfile.objects.create(user=self.administrator, role=StaffProfile.Role.ADMINISTRATOR)
        source = '''class PermissionPlugin:
    def __init__(self, host):
        host.api.get("read", handler=self.read, permission="permission-test.read")
        host.api.delete("manage", handler=self.manage, permission="permission-test.manage")

    def read(self, request):
        return {"allowed": "read"}

    def manage(self, request):
        return {"allowed": "manage"}


def create_plugin(host):
    return PermissionPlugin(host)
'''
        archive = make_custom_runtime_plugin(
            "permission-test",
            "1.0.0",
            source,
            permissions=[
                {"code": "permission-test.read", "name": "Read", "roles": ["reviewer", "administrator"]},
                {"code": "permission-test.manage", "name": "Manage", "roles": ["administrator"]},
            ],
        )
        self.installer.install(SimpleUploadedFile("permission-test.ajplugin", archive), actor=self.superuser)
        self.installer.set_enabled("permission-test", True, actor=self.superuser)
        self.client = APIClient()

    def tearDown(self):
        runtime_registry.clear()
        self.settings_override.disable()
        self.temporary.cleanup()

    def test_permission_is_checked_after_resolving_the_handler(self):
        self.client.force_authenticate(self.reviewer)
        self.assertEqual(self.client.get("/api/plugins/permission-test/read/").status_code, 200)
        self.assertEqual(self.client.delete("/api/plugins/permission-test/manage/").status_code, 403)

        self.client.force_authenticate(self.administrator)
        self.assertEqual(self.client.get("/api/plugins/permission-test/read/").status_code, 200)
        self.assertEqual(self.client.delete("/api/plugins/permission-test/manage/").status_code, 200)

        self.client.force_authenticate(self.normal_user)
        self.assertEqual(self.client.get("/api/plugins/permission-test/read/").status_code, 403)
        self.client.force_authenticate(self.superuser)
        self.assertEqual(self.client.delete("/api/plugins/permission-test/manage/").status_code, 200)

    def test_undeclared_route_permission_fails_runtime_load(self):
        source = '''class InvalidPermissionPlugin:
    def __init__(self, host):
        host.api.get("read", handler=self.read, permission="invalid-permission.manage")

    def read(self, request):
        return {"ok": True}


def create_plugin(host):
    return InvalidPermissionPlugin(host)
'''
        archive = make_custom_runtime_plugin(
            "invalid-permission",
            "1.0.0",
            source,
            permissions=[{"code": "invalid-permission.read", "name": "Read", "roles": ["administrator"]}],
        )
        with self.assertRaises(PluginInstallError):
            self.installer.install(SimpleUploadedFile("invalid-permission.ajplugin", archive), actor=self.superuser)


class PluginRuntimeConsistencyAndHookTests(TestCase):
    slug = "runtime-hook-test-plugin"

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.plugin_root = Path(self.temporary.name)
        self.settings_override = override_settings(PLUGIN_ROOT=self.plugin_root, PLUGIN_MIN_FREE_DISK_MB=0, PLUGIN_KEEP_VERSIONS=2)
        self.settings_override.enable()
        runtime_registry.clear()
        self.installer = PluginPackageInstaller()
        self.superuser = User.objects.create_superuser(username="hook-root", password="StrongPass123!")

    def tearDown(self):
        runtime_registry.clear()
        self.settings_override.disable()
        self.temporary.cleanup()

    def package(self, version, *, declared=True):
        source = f'''class HookPlugin:
    def __init__(self, host):
        self.host = host
        host.api.get("ping", handler=self.ping, permission="{self.slug}.run")
        host.register_hook("journal.after_create", self.after_create)

    def ping(self, request):
        return {{"version": self.host.version}}

    def after_create(self, context):
        storage = self.host.storage(namespace="hook-events")
        events = storage.get("events", []) or []
        storage.set("events", [*events, self.host.version])


def create_plugin(host):
    return HookPlugin(host)
'''
        return make_custom_runtime_plugin(
            self.slug,
            version,
            source,
            permissions=[{"code": f"{self.slug}.run", "name": "Run", "roles": ["administrator"]}],
            hooks=["journal.after_create"] if declared else [],
        )

    def install(self, version):
        archive = SimpleUploadedFile(f"{self.slug}-{version}.ajplugin", self.package(version))
        return self.installer.install(
            archive,
            replace=PluginInstallation.objects.filter(slug=self.slug).exists(),
            actor=self.superuser,
        )

    def events(self):
        row = PluginData.objects.filter(plugin_slug=self.slug, namespace="hook-events", key="events", user=None).first()
        return [] if row is None else row.value

    def test_database_state_reconciles_stale_request_hints_without_version_resurrection(self):
        self.install("1.0.0")
        self.installer.set_enabled(self.slug, True, actor=self.superuser)
        stale = PluginInstallation.objects.get(slug=self.slug)
        self.install("1.1.0")
        self.assertEqual(stale.current_version, "1.0.0")
        self.assertEqual(runtime_registry.ensure_current(self.slug).version, "1.1.0")
        self.assertEqual(runtime_registry.assert_invariant(self.slug), "1.1.0")

        self.installer.rollback(self.slug, actor=self.superuser)
        self.assertEqual(runtime_registry.ensure_current(self.slug).version, "1.0.0")
        self.installer.set_enabled(self.slug, False, actor=self.superuser)
        with self.assertRaises(RuntimeUnavailable):
            runtime_registry.ensure_current(self.slug)
        self.assertIsNone(runtime_registry.active_version(self.slug))

        self.installer.set_enabled(self.slug, True, actor=self.superuser)
        self.installer.uninstall(self.slug)
        with self.assertRaises(RuntimeUnavailable):
            runtime_registry.ensure_current(self.slug)
        self.assertIsNone(runtime_registry.active_version(self.slug))

    def test_upgrade_rollback_and_disable_keep_only_target_version_hooks(self):
        self.install("1.0.0")
        self.installer.set_enabled(self.slug, True, actor=self.superuser)
        runtime_registry.hooks.run_hook("journal.after_create", {"source": "test"})
        self.assertEqual(self.events(), ["1.0.0"])

        self.install("1.1.0")
        runtime_registry.hooks.run_hook("journal.after_create", {"source": "test"})
        self.assertEqual(self.events(), ["1.0.0", "1.1.0"])
        self.assertEqual({item.plugin_version for item in runtime_registry.hooks.registrations_for(self.slug)}, {"1.1.0"})

        self.installer.rollback(self.slug, actor=self.superuser)
        runtime_registry.hooks.run_hook("journal.after_create", {"source": "test"})
        self.assertEqual(self.events(), ["1.0.0", "1.1.0", "1.0.0"])
        self.assertEqual({item.plugin_version for item in runtime_registry.hooks.registrations_for(self.slug)}, {"1.0.0"})

        self.installer.set_enabled(self.slug, False, actor=self.superuser)
        runtime_registry.hooks.run_hook("journal.after_create", {"source": "test"})
        self.assertEqual(self.events(), ["1.0.0", "1.1.0", "1.0.0"])
        self.assertEqual(runtime_registry.hooks.registrations_for(self.slug), ())

    def test_second_worker_lazily_reconciles_upgrade_rollback_and_disable(self):
        self.install("1.0.0")
        self.installer.set_enabled(self.slug, True, actor=self.superuser)
        worker_b = RuntimeRegistry()
        try:
            worker_b.hooks.run_hook("journal.after_create", {"worker": "b"})
            self.assertEqual(worker_b.active_version(self.slug), "1.0.0")

            self.install("1.1.0")
            worker_b.hooks.run_hook("journal.after_create", {"worker": "b"})
            self.assertEqual(worker_b.active_version(self.slug), "1.1.0")
            self.assertEqual(self.events()[-1], "1.1.0")

            self.installer.rollback(self.slug, actor=self.superuser)
            worker_b.hooks.run_hook("journal.after_create", {"worker": "b"})
            self.assertEqual(worker_b.active_version(self.slug), "1.0.0")
            self.assertEqual(self.events()[-1], "1.0.0")

            before_disable = list(self.events())
            self.installer.set_enabled(self.slug, False, actor=self.superuser)
            worker_b.hooks.run_hook("journal.after_create", {"worker": "b"})
            self.assertEqual(self.events(), before_disable)
            self.assertIsNone(worker_b.active_version(self.slug))
        finally:
            worker_b.clear()

    def test_undeclared_hook_rejects_candidate(self):
        archive = SimpleUploadedFile(f"{self.slug}.ajplugin", self.package("1.0.0", declared=False))
        with self.assertRaises(PluginInstallError):
            self.installer.install(archive, actor=self.superuser)
        self.assertFalse(PluginInstallation.objects.filter(slug=self.slug).exists())


@skipUnless(connection.vendor == "postgresql", "PostgreSQL is required for runtime lifecycle concurrency tests")
class PluginRuntimeConcurrencyPostgreSQLTests(TransactionTestCase):
    slug = "runtime-concurrency-plugin"

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.plugin_root = Path(self.temporary.name)
        self.settings_override = override_settings(PLUGIN_ROOT=self.plugin_root, PLUGIN_MIN_FREE_DISK_MB=0, PLUGIN_KEEP_VERSIONS=2)
        self.settings_override.enable()
        runtime_registry.clear()
        self.installer = PluginPackageInstaller()
        self.superuser = User.objects.create_superuser(username=f"concurrency-root-{self._testMethodName}", password="StrongPass123!")
        self.install("1.0.0")
        self.installer.set_enabled(self.slug, True, actor=self.superuser)

    def tearDown(self):
        runtime_registry.clear()
        self.settings_override.disable()
        self.temporary.cleanup()

    def install(self, version):
        archive = SimpleUploadedFile(f"{self.slug}-{version}.ajplugin", make_runtime_plugin(self.slug, version))
        return self.installer.install(
            archive,
            replace=PluginInstallation.objects.filter(slug=self.slug).exists(),
            actor=self.superuser,
        )

    def stale_request_result_after(self, lifecycle_action):
        started = Event()
        proceed = Event()
        stale_installation = PluginInstallation.objects.get(slug=self.slug)

        def request_work():
            close_old_connections()
            try:
                stale_version = stale_installation.current_version
                started.set()
                proceed.wait(timeout=10)
                try:
                    return stale_version, runtime_registry.ensure_current(self.slug).version
                except RuntimeUnavailable:
                    return stale_version, None
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(request_work)
            self.assertTrue(started.wait(timeout=10))
            lifecycle_action()
            proceed.set()
            return future.result(timeout=10)

    def test_dispatch_vs_upgrade_does_not_restore_stale_version(self):
        stale_version, active_version = self.stale_request_result_after(lambda: self.install("1.1.0"))
        self.assertEqual(stale_version, "1.0.0")
        self.assertEqual(active_version, "1.1.0")
        self.assertEqual(runtime_registry.assert_invariant(self.slug), "1.1.0")

    def test_dispatch_vs_disable_does_not_resurrect_runtime(self):
        stale_version, active_version = self.stale_request_result_after(
            lambda: self.installer.set_enabled(self.slug, False, actor=self.superuser)
        )
        self.assertEqual(stale_version, "1.0.0")
        self.assertIsNone(active_version)
        self.assertIsNone(runtime_registry.active_version(self.slug))

    def test_dispatch_vs_rollback_reconciles_to_database_target(self):
        self.install("1.1.0")
        stale_version, active_version = self.stale_request_result_after(
            lambda: self.installer.rollback(self.slug, actor=self.superuser)
        )
        self.assertEqual(stale_version, "1.1.0")
        self.assertEqual(active_version, "1.0.0")
        self.assertEqual(runtime_registry.assert_invariant(self.slug), "1.0.0")

    def test_dispatch_vs_uninstall_does_not_recreate_runtime(self):
        stale_version, active_version = self.stale_request_result_after(lambda: self.installer.uninstall(self.slug))
        self.assertEqual(stale_version, "1.0.0")
        self.assertIsNone(active_version)
        self.assertIsNone(runtime_registry.active_version(self.slug))
        self.assertFalse(PluginInstallation.objects.filter(slug=self.slug).exists())


class WatchHistoryRealPluginE2ETests(TestCase):
    slug = "watch-history-importer"

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.plugin_root = Path(self.temporary.name)
        self.settings_override = override_settings(PLUGIN_ROOT=self.plugin_root, PLUGIN_MIN_FREE_DISK_MB=0)
        self.settings_override.enable()
        runtime_registry.clear()
        self.installer = PluginPackageInstaller()
        self.reviewer = User.objects.create_user(username="watch-reviewer", password="StrongPass123!", is_staff=True)
        self.administrator = User.objects.create_user(username="watch-admin", password="StrongPass123!", is_staff=True)
        self.normal_user = User.objects.create_user(username="watch-user", password="StrongPass123!")
        self.superuser = User.objects.create_superuser(username="watch-root", password="StrongPass123!")
        StaffProfile.objects.create(user=self.reviewer, role=StaffProfile.Role.REVIEWER)
        StaffProfile.objects.create(user=self.administrator, role=StaffProfile.Role.ADMINISTRATOR)
        source_root = Path(__file__).resolve().parents[3] / "plugins" / self.slug
        archive = make_source_plugin_package(source_root)
        self.installer.install(SimpleUploadedFile(f"{self.slug}.ajplugin", archive), actor=self.superuser)
        self.installer.set_enabled(self.slug, True, actor=self.superuser)
        self.client = APIClient()

    def tearDown(self):
        runtime_registry.clear()
        self.settings_override.disable()
        self.temporary.cleanup()

    def metadata_slugs(self):
        return {item["slug"] for item in self.client.get(reverse("enabled-plugins")).data["plugins"]}

    def test_real_plugin_metadata_asset_and_backend_permissions(self):
        self.client.force_authenticate(self.administrator)
        self.assertIn(self.slug, self.metadata_slugs())
        status_response = self.client.get(f"/api/plugins/{self.slug}/status/")
        self.assertEqual(status_response.status_code, 200)
        self.assertEqual(status_response.data["plugin"]["version"], "0.2.0")

        metadata = self.client.get(reverse("enabled-plugins")).data["plugins"]
        plugin = next(item for item in metadata if item["slug"] == self.slug)
        browser_asset = APIClient().get(plugin["frontendEntry"])
        self.assertEqual(browser_asset.status_code, 200)
        browser_asset.close()

        self.client.force_authenticate(self.reviewer)
        self.assertNotIn(self.slug, self.metadata_slugs())
        self.assertEqual(self.client.get(f"/api/plugins/{self.slug}/status/").status_code, 403)

        self.client.force_authenticate(self.normal_user)
        self.assertNotIn(self.slug, self.metadata_slugs())
        self.assertEqual(self.client.get(f"/api/plugins/{self.slug}/status/").status_code, 403)

        self.client.force_authenticate(user=None)
        self.assertNotIn(self.slug, self.metadata_slugs())
        self.assertEqual(self.client.get(f"/api/plugins/{self.slug}/status/").status_code, 403)

        self.client.force_authenticate(self.superuser)
        self.assertEqual(self.client.get(f"/api/plugins/{self.slug}/status/").status_code, 200)


class PluginAssetPermissionE2ETests(TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.plugin_root = Path(self.temporary.name)
        self.settings_override = override_settings(
            PLUGIN_ROOT=self.plugin_root,
            PLUGIN_MIN_FREE_DISK_MB=0,
            PLUGIN_KEEP_VERSIONS=2,
        )
        self.settings_override.enable()
        runtime_registry.clear()
        self.installer = PluginPackageInstaller()
        self.user = User.objects.create_user(username="asset-user", password="StrongPass123!")
        self.reviewer = User.objects.create_user(username="asset-reviewer", password="StrongPass123!", is_staff=True)
        self.administrator = User.objects.create_user(username="asset-admin", password="StrongPass123!", is_staff=True)
        self.superuser = User.objects.create_superuser(username="asset-root", password="StrongPass123!")
        StaffProfile.objects.create(user=self.reviewer, role=StaffProfile.Role.REVIEWER)
        StaffProfile.objects.create(user=self.administrator, role=StaffProfile.Role.ADMINISTRATOR)
        self.client = APIClient()
        for slug, exposure, roles in (
            ("asset-public", "public", None),
            ("asset-authenticated", "authenticated", None),
            ("asset-staff", "staff", ["administrator"]),
        ):
            archive = SimpleUploadedFile(
                f"{slug}.ajplugin",
                make_runtime_plugin(slug, exposure=exposure, backend=False, roles=roles),
                content_type="application/zip",
            )
            self.installer.install(archive, actor=self.superuser)
            self.installer.set_enabled(slug, True, actor=self.superuser)

    def tearDown(self):
        runtime_registry.clear()
        self.settings_override.disable()
        self.temporary.cleanup()

    @staticmethod
    def asset_url(slug):
        return reverse("plugin-asset", kwargs={"slug": slug, "version": "1.0.0", "asset": "plugin.js"})

    def get_asset(self, slug):
        response = self.client.get(self.asset_url(slug))
        response.close()
        return response

    def plugin_slugs(self):
        return {item["slug"] for item in self.client.get(reverse("enabled-plugins")).data["plugins"]}

    def test_metadata_assets_and_cache_follow_exposure_and_permission(self):
        self.assertEqual(self.plugin_slugs(), {"asset-public"})
        public = self.get_asset("asset-public")
        self.assertEqual(public.status_code, 200)
        self.assertEqual(public["Cache-Control"], "public, max-age=31536000, immutable")
        self.assertEqual(self.get_asset("asset-authenticated").status_code, 404)
        self.assertEqual(self.get_asset("asset-staff").status_code, 404)

        self.client.force_authenticate(self.user)
        self.assertEqual(self.plugin_slugs(), {"asset-public", "asset-authenticated"})
        authenticated = self.get_asset("asset-authenticated")
        self.assertEqual(authenticated.status_code, 200)
        self.assertEqual(authenticated["Cache-Control"], "private, no-store")
        self.assertNotIn("public", authenticated["Cache-Control"])
        self.assertEqual(self.get_asset("asset-staff").status_code, 404)

        self.client.force_authenticate(self.reviewer)
        self.assertNotIn("asset-staff", self.plugin_slugs())
        self.assertEqual(self.get_asset("asset-staff").status_code, 404)

        self.client.force_authenticate(self.administrator)
        self.assertIn("asset-staff", self.plugin_slugs())
        staff = self.get_asset("asset-staff")
        self.assertEqual(staff.status_code, 200)
        self.assertEqual(staff["Cache-Control"], "private, no-store")

        self.client.force_authenticate(self.superuser)
        self.assertIn("asset-staff", self.plugin_slugs())
        self.assertEqual(self.get_asset("asset-staff").status_code, 200)

    def test_protected_asset_session_preserves_js_and_css_relative_urls(self):
        self.client.force_authenticate(self.administrator)
        metadata = self.client.get(reverse("enabled-plugins")).data["plugins"]
        plugin = next(item for item in metadata if item["slug"] == "asset-staff")
        self.assertIn("/plugin-assets/session/", plugin["frontendEntry"])

        browser = APIClient()
        module = browser.get(plugin["frontendEntry"])
        self.assertEqual(module.status_code, 200)
        module.close()
        stylesheet = browser.get(plugin["styleEntry"])
        self.assertEqual(stylesheet.status_code, 200)
        stylesheet.close()

        icon = browser.get(urljoin(plugin["frontendEntry"], "./assets/icon.svg"))
        self.assertEqual(icon.status_code, 200)
        icon.close()
        background = browser.get(urljoin(plugin["styleEntry"], "./assets/bg.webp"))
        self.assertEqual(background.status_code, 200)
        background.close()
