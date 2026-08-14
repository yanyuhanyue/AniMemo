from datetime import timedelta
from io import StringIO

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from plugin_host.models import PluginData, PluginProject
from plugin_host.storage import PluginStorage, PluginStorageLimitError


class PluginStorageLimitsTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            "storage-limits",
            password="password-123",
        )
        self.project = PluginProject.objects.create(
            plugin_id="com.example.storage-limits",
            slug="storage-limits",
            name="Storage Limits",
            description="test",
        )
        self.storage = PluginStorage(
            self.project,
            user=self.user,
            namespace="batches",
        )

    def test_bounded_set_rejects_large_values_and_prunes_oldest_rows(self):
        with self.assertRaises(PluginStorageLimitError):
            self.storage.set_bounded(
                "too-large",
                {"blob": "x" * 64},
                max_value_bytes=32,
                max_rows=2,
                retention_seconds=60,
            )

        self.storage.set_bounded(
            "one",
            {"value": 1},
            max_value_bytes=1024,
            max_rows=2,
            retention_seconds=60,
        )
        self.storage.set_bounded(
            "two",
            {"value": 2},
            max_value_bytes=1024,
            max_rows=2,
            retention_seconds=60,
        )
        self.storage.set_bounded(
            "three",
            {"value": 3},
            max_value_bytes=1024,
            max_rows=2,
            retention_seconds=60,
        )

        self.assertEqual(
            set(self.storage.collection().values_list("key", flat=True)),
            {"two", "three"},
        )

    def test_bounded_set_prunes_expired_rows_before_saving(self):
        self.storage.set("expired", {"value": "old"})
        PluginData.objects.filter(
            plugin=self.project,
            user=self.user,
            namespace="batches",
            key="expired",
        ).update(updated_at=timezone.now() - timedelta(seconds=61))

        self.storage.set_bounded(
            "current",
            {"value": "new"},
            max_value_bytes=1024,
            max_rows=2,
            retention_seconds=60,
        )

        self.assertEqual(
            list(self.storage.collection().values_list("key", flat=True)),
            ["current"],
        )

    @override_settings(WATCH_HISTORY_IMPORT_BATCH_RETENTION_SECONDS=100)
    def test_maintenance_only_removes_expired_import_batches(self):
        importer = PluginProject.objects.create(
            plugin_id="com.animemo.watch-history-importer",
            slug="watch-history-importer",
            name="Importer",
            description="test",
        )
        other = PluginProject.objects.create(
            plugin_id="com.example.other-storage",
            slug="other-storage",
            name="Other",
            description="test",
        )
        old_batch = PluginData.objects.create(
            plugin=importer,
            user=self.user,
            namespace="batches",
            key="old-batch",
        )
        recent_batch = PluginData.objects.create(
            plugin=importer,
            user=self.user,
            namespace="batches",
            key="recent-batch",
        )
        other_namespace = PluginData.objects.create(
            plugin=importer,
            user=self.user,
            namespace="subjects",
            key="old-subject",
        )
        other_plugin = PluginData.objects.create(
            plugin=other,
            user=self.user,
            namespace="batches",
            key="old-other",
        )
        PluginData.objects.filter(
            pk__in=(old_batch.pk, other_namespace.pk, other_plugin.pk)
        ).update(updated_at=timezone.now() - timedelta(seconds=101))
        PluginData.objects.filter(pk=recent_batch.pk).update(
            updated_at=timezone.now() - timedelta(seconds=99)
        )

        output = StringIO()
        call_command("cleanup_watch_history_import_batches", stdout=output)

        self.assertFalse(PluginData.objects.filter(pk=old_batch.pk).exists())
        self.assertTrue(PluginData.objects.filter(pk=recent_batch.pk).exists())
        self.assertTrue(PluginData.objects.filter(pk=other_namespace.pk).exists())
        self.assertTrue(PluginData.objects.filter(pk=other_plugin.pk).exists())
        self.assertIn("deleted batches=1", output.getvalue())
