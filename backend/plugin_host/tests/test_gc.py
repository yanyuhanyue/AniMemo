import os
import tempfile
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from plugin_host.installer import PluginPackageInstaller
from plugin_host.models import PluginPackageBlob
from plugin_host.package import LocalPluginPackageStorage
from plugin_host.services import garbage_collect_package_blobs, store_package_blob
from plugin_host.tests.test_runtime_e2e import make_package


class PluginPackageGarbageCollectionTests(TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.settings = override_settings(
            PLUGIN_ROOT=self.root,
            PLUGIN_MIN_FREE_DISK_MB=0,
            PLUGIN_PACKAGE_GC_GRACE_SECONDS=0,
            PLUGIN_STAGING_GC_GRACE_SECONDS=0,
            PLUGIN_PREVIEW_GC_GRACE_SECONDS=0,
        )
        self.settings.enable()
        self.user = get_user_model().objects.create_user("gc-user", password="password-123")

    def tearDown(self):
        self.settings.disable()
        self.temporary.cleanup()

    def _blob(self, slug="gc-package"):
        payload, _ = make_package(slug, "1.0.0", runtimes=["frontend"])
        blob, _, _, _ = store_package_blob(payload)
        PluginPackageBlob.objects.filter(pk=blob.pk).update(created_at=timezone.now() - timedelta(days=2))
        blob.refresh_from_db()
        return blob, payload

    def test_gc_quarantines_then_removes_unreferenced_blob(self):
        blob, _ = self._blob()
        path = LocalPluginPackageStorage(self.root).package_path(blob.sha256)

        report = garbage_collect_package_blobs(root=self.root)

        self.assertEqual(report["package_blobs_removed"], [blob.sha256])
        self.assertFalse(PluginPackageBlob.objects.filter(pk=blob.pk).exists())
        self.assertFalse(path.exists())

    def test_gc_restores_canonical_file_when_database_delete_rolls_back(self):
        blob, _ = self._blob("gc-rollback")
        path = LocalPluginPackageStorage(self.root).package_path(blob.sha256)

        with patch("plugin_host.models.PluginPackageBlob.delete", side_effect=RuntimeError("database failure")):
            with self.assertRaises(RuntimeError):
                garbage_collect_package_blobs(root=self.root)

        self.assertTrue(PluginPackageBlob.objects.filter(pk=blob.pk).exists())
        self.assertTrue(path.is_file())

    def test_missing_blob_file_is_reported_and_database_row_is_retained(self):
        blob, _ = self._blob("gc-missing")
        LocalPluginPackageStorage(self.root).package_path(blob.sha256).unlink()

        report = garbage_collect_package_blobs(root=self.root)

        self.assertIn(blob.sha256, report["missing_blob_files"])
        self.assertTrue(PluginPackageBlob.objects.filter(pk=blob.pk).exists())

    def test_orphan_physical_package_is_removed_after_grace_period(self):
        payload, _ = make_package("gc-orphan", "1.0.0", runtimes=["frontend"])
        storage = LocalPluginPackageStorage(self.root)
        path = storage.store_package(payload)
        old = (timezone.now() - timedelta(days=2)).timestamp()
        os.utime(path, (old, old))

        report = garbage_collect_package_blobs(root=self.root)

        self.assertEqual(len(report["orphan_files_removed"]), 1)
        self.assertFalse(path.exists())

    def test_cleanup_covers_staging_preview_and_package_gc(self):
        blob, _ = self._blob("gc-cleanup")
        storage = LocalPluginPackageStorage(self.root)
        staging = storage.staging / "stale-upload"
        staging.mkdir(parents=True)
        preview = storage.previews / "deleted-project" / "0.1.0"
        preview.mkdir(parents=True)
        old = (timezone.now() - timedelta(days=2)).timestamp()
        os.utime(staging, (old, old))
        os.utime(preview, (old, old))

        report = PluginPackageInstaller(root=self.root).cleanup()

        self.assertGreaterEqual(report["staging_removed"], 1)
        self.assertEqual(report["preview_removed"], ["deleted-project/0.1.0"])
        self.assertIn(blob.sha256, report["package_blobs_removed"])
        for key in ("runtime_removed", "runtime_retained", "orphan_files_removed", "missing_blob_files"):
            self.assertIn(key, report)

    def test_crash_tombstone_is_restored_when_database_row_still_exists(self):
        blob, _ = self._blob("gc-tombstone")
        storage = LocalPluginPackageStorage(self.root)
        canonical = storage.package_path(blob.sha256)
        tombstone = storage.staging / "gc" / f"{blob.sha256}.crash.tombstone"
        tombstone.parent.mkdir(parents=True)
        os.replace(canonical, tombstone)

        with override_settings(PLUGIN_PACKAGE_GC_GRACE_SECONDS=86400 * 30):
            report = garbage_collect_package_blobs(root=self.root)

        self.assertIn(blob.sha256, report["package_tombstones_restored"])
        self.assertTrue(canonical.is_file())
        self.assertFalse(tombstone.exists())
