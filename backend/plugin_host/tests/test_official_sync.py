import tempfile
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase, override_settings

from plugin_host.management.commands.sync_official_plugins import (
    ZIP_TIMESTAMP,
    build_official_package,
)
from plugin_host.models import PluginDeployment, PluginPackageBlob, PluginProject, PluginVersion
from plugin_host.runtime import runtime_registry


class OfficialPluginSyncTests(TestCase):
    def setUp(self):
        self.root = tempfile.TemporaryDirectory()
        self.settings = override_settings(PLUGIN_ROOT=Path(self.root.name), PLUGIN_MIN_FREE_DISK_MB=0)
        self.settings.enable()
        get_user_model().objects.create_superuser(
            username="official-sync-admin",
            email="official-sync@example.com",
            password="temporary-password-123",
        )

    def tearDown(self):
        runtime_registry.clear()
        self.settings.disable()
        self.root.cleanup()

    def test_official_package_is_deterministic(self):
        source = Path(__file__).resolve().parents[3] / "plugins" / "watch-history-importer"
        first = build_official_package(source)
        second = build_official_package(source)

        self.assertEqual(first, second)
        with ZipFile(BytesIO(first)) as archive:
            self.assertTrue(archive.infolist())
            self.assertTrue(all(item.date_time == ZIP_TIMESTAMP for item in archive.infolist()))

    def test_sync_is_idempotent(self):
        call_command("sync_official_plugins")
        deployment = PluginDeployment.objects.get(plugin__slug="watch-history-importer")
        first_version_id = deployment.current_version_id
        first_blob_id = deployment.current_version.package_blob_id

        call_command("sync_official_plugins")
        deployment.refresh_from_db()

        self.assertEqual(deployment.current_version_id, first_version_id)
        self.assertEqual(deployment.current_version.package_blob_id, first_blob_id)
        self.assertEqual(PluginProject.objects.count(), 1)
        self.assertEqual(PluginVersion.objects.count(), 1)
        self.assertEqual(PluginPackageBlob.objects.count(), 1)
