import tempfile
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from threading import Barrier, Event
from unittest import skipUnless

from django.contrib.auth import get_user_model
from django.db import close_old_connections, connection, connections
from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from plugin_host.installer import PluginInstallError, PluginPackageInstaller
from plugin_host.models import PluginDeployment, PluginPackageBlob, PluginProject, PluginVersion, UserPluginInstallation
from plugin_host.package import inspect_package
from plugin_host.runtime import RuntimeUnavailable, runtime_registry
from plugin_host.services import garbage_collect_package_blobs, install_for_user, store_package_blob
from plugin_host.tests.test_runtime_e2e import make_package


@skipUnless(connection.vendor == "postgresql", "PostgreSQL concurrency proof requires PostgreSQL")
class PluginPlatformPostgreSQLConcurrencyTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        self.root = tempfile.TemporaryDirectory()
        self.settings = override_settings(PLUGIN_ROOT=Path(self.root.name), PLUGIN_MIN_FREE_DISK_MB=0)
        self.settings.enable()
        self.admin = get_user_model().objects.create_superuser("admin", "admin@example.com", "password-123")
        self.user = get_user_model().objects.create_user("user", "user@example.com", "password-123")

    def tearDown(self):
        runtime_registry.clear()
        self.settings.disable()
        self.root.cleanup()

    @staticmethod
    def _parallel(callables):
        def run(callback):
            try:
                return callback()
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=len(callables)) as pool:
            futures = [pool.submit(run, callback) for callback in callables]
            return [future.result() for future in futures]

    def _version(self, project, version):
        payload, manifest = make_package(project.slug, version, runtimes=["frontend"])
        blob, _, _, _ = store_package_blob(payload)
        return PluginVersion.objects.create(
            plugin=project, version=version, package_blob=blob, manifest_snapshot=manifest,
            runtime_types=["frontend"], review_status=PluginVersion.ReviewStatus.APPROVED,
            created_by=self.admin,
        )

    def _stale_dispatch_result_after(self, project, lifecycle_action):
        started = Event()
        proceed = Event()
        stale_version = PluginDeployment.objects.select_related("current_version").get(plugin=project).current_version.version

        def dispatch():
            close_old_connections()
            try:
                started.set()
                proceed.wait(timeout=10)
                try:
                    return stale_version, runtime_registry.ensure_current(project.slug).version
                except RuntimeUnavailable:
                    return stale_version, None
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(dispatch)
            self.assertTrue(started.wait(timeout=10))
            lifecycle_action()
            proceed.set()
            return future.result(timeout=10)

    def test_same_package_concurrent_upload_creates_one_blob(self):
        payload, _ = make_package("cas-race", "1.0.0", runtimes=["frontend"])
        barrier = Barrier(2)

        def upload():
            close_old_connections()
            barrier.wait()
            result = store_package_blob(payload)[0].sha256
            close_old_connections()
            return result

        hashes = self._parallel([upload, upload])
        self.assertEqual(hashes[0], hashes[1])
        self.assertEqual(len(list((Path(self.root.name) / "packages" / "sha256").rglob("*.ajplugin"))), 1)

    def test_same_user_concurrent_install_creates_one_row(self):
        project = PluginProject.objects.create(plugin_id="com.example.install-race", slug="install-race", name="race", description="test")
        version = self._version(project, "1.0.0")
        version.published_at = timezone.now()
        version.save(update_fields=["published_at"])
        PluginDeployment.objects.create(plugin=project, current_version=version, enabled=True, healthy=True)
        barrier = Barrier(2)

        def install():
            close_old_connections()
            barrier.wait()
            result = install_for_user(PluginProject.objects.get(pk=project.pk), user=get_user_model().objects.get(pk=self.user.pk))[0].pk
            close_old_connections()
            return result

        ids = self._parallel([install, install])
        self.assertEqual(ids[0], ids[1])
        self.assertEqual(UserPluginInstallation.objects.filter(user=self.user, plugin=project).count(), 1)

    def test_gc_vs_same_sha_upload_never_leaves_database_row_without_file(self):
        payload, _ = make_package("gc-upload-race", "1.0.0", runtimes=["frontend"])
        blob, _, _, _ = store_package_blob(payload)
        PluginPackageBlob.objects.filter(pk=blob.pk).update(created_at=timezone.now() - timedelta(days=2))
        barrier = Barrier(2)

        def upload():
            close_old_connections(); barrier.wait()
            try:
                return store_package_blob(payload)[0].sha256
            finally:
                close_old_connections()

        def collect():
            close_old_connections(); barrier.wait()
            try:
                return garbage_collect_package_blobs(root=self.root.name)
            finally:
                close_old_connections()

        self._parallel([upload, collect])
        row = PluginPackageBlob.objects.filter(sha256=blob.sha256).first()
        path = Path(self.root.name) / "packages" / "sha256" / blob.sha256[:2] / f"{blob.sha256}.ajplugin"
        if row is None:
            self.assertFalse(path.exists())
        else:
            self.assertTrue(path.is_file())
            self.assertEqual(inspect_package(path.read_bytes())["sha256"], row.sha256)

    def test_publish_vs_revoke_never_leaves_revoked_runtime_enabled(self):
        project = PluginProject.objects.create(plugin_id="com.example.publish-revoke", slug="publish-revoke", name="race", description="test")
        version = self._version(project, "1.0.0")
        barrier = Barrier(2)

        def publish():
            close_old_connections(); barrier.wait()
            try:
                PluginPackageInstaller().publish(PluginVersion.objects.get(pk=version.pk), actor=get_user_model().objects.get(pk=self.admin.pk))
            except PluginInstallError:
                pass
            finally:
                close_old_connections()

        def revoke():
            close_old_connections(); barrier.wait()
            PluginPackageInstaller().revoke(PluginVersion.objects.get(pk=version.pk), actor=get_user_model().objects.get(pk=self.admin.pk))
            close_old_connections()

        self._parallel([publish, revoke])
        version.refresh_from_db()
        deployment = PluginDeployment.objects.filter(plugin=project).first()
        self.assertIsNotNone(version.revoked_at)
        self.assertTrue(deployment is None or not deployment.enabled)
        self.assertNotEqual(runtime_registry.active_version(project.slug), version.version)

    def test_publish_vs_rollback_keeps_a_known_version(self):
        project = PluginProject.objects.create(plugin_id="com.example.publish-rollback", slug="publish-rollback", name="race", description="test")
        first = self._version(project, "1.0.0")
        second = self._version(project, "1.1.0")
        third = self._version(project, "1.2.0")
        installer = PluginPackageInstaller()
        installer.publish(first, actor=self.admin)
        installer.publish(second, actor=self.admin)
        barrier = Barrier(2)

        def publish():
            close_old_connections(); barrier.wait()
            try:
                PluginPackageInstaller().publish(PluginVersion.objects.get(pk=third.pk), actor=get_user_model().objects.get(pk=self.admin.pk))
            except PluginInstallError:
                pass
            finally:
                close_old_connections()

        def rollback():
            close_old_connections(); barrier.wait()
            try:
                PluginPackageInstaller().rollback(project.slug, actor=get_user_model().objects.get(pk=self.admin.pk))
            except PluginInstallError:
                pass
            finally:
                close_old_connections()

        self._parallel([publish, rollback])
        deployment = PluginDeployment.objects.select_related("current_version").get(plugin=project)
        self.assertTrue((Path(self.root.name) / "runtime" / project.slug / deployment.current_version.version).is_dir())
        self.assertEqual(runtime_registry.ensure_current(project.slug).version, deployment.current_version.version)
        self.assertTrue(all(
            registration.plugin_version == deployment.current_version.version
            for registration in runtime_registry.hooks.registrations_for(project.slug)
        ))

    def test_dispatch_vs_upgrade_uses_database_current_version(self):
        project = PluginProject.objects.create(plugin_id="com.example.dispatch-upgrade", slug="dispatch-upgrade", name="race", description="test")
        first = self._version(project, "1.0.0")
        second = self._version(project, "1.1.0")
        PluginPackageInstaller().publish(first, actor=self.admin)

        stale, active = self._stale_dispatch_result_after(
            project,
            lambda: PluginPackageInstaller().publish(second, actor=self.admin),
        )

        self.assertEqual(stale, "1.0.0")
        self.assertEqual(active, "1.1.0")

    def test_dispatch_vs_rollback_uses_database_current_version(self):
        project = PluginProject.objects.create(plugin_id="com.example.dispatch-rollback", slug="dispatch-rollback", name="race", description="test")
        first = self._version(project, "1.0.0")
        second = self._version(project, "1.1.0")
        installer = PluginPackageInstaller()
        installer.publish(first, actor=self.admin)
        installer.publish(second, actor=self.admin)

        stale, active = self._stale_dispatch_result_after(
            project,
            lambda: PluginPackageInstaller().rollback(project.slug, actor=self.admin),
        )

        self.assertEqual(stale, "1.1.0")
        self.assertEqual(active, "1.0.0")

    def test_dispatch_vs_disable_cannot_resurrect_runtime(self):
        project = PluginProject.objects.create(plugin_id="com.example.dispatch-disable", slug="dispatch-disable", name="race", description="test")
        first = self._version(project, "1.0.0")
        PluginPackageInstaller().publish(first, actor=self.admin)

        stale, active = self._stale_dispatch_result_after(
            project,
            lambda: PluginPackageInstaller().set_enabled(project.slug, False, actor=self.admin),
        )

        self.assertEqual(stale, "1.0.0")
        self.assertIsNone(active)
        self.assertIsNone(runtime_registry.active_version(project.slug))
