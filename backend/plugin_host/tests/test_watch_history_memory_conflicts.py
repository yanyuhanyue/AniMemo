import tempfile
from copy import deepcopy
from pathlib import Path
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase, override_settings
from integrations.models import IntegrationEvent
from journal.models import JournalEntry
from journal.watch_history import add_history
from rest_framework.test import APIClient

from plugin_host.models import PluginData, PluginProject
from plugin_host.runtime import runtime_registry
from plugin_host.services import install_for_user


class OfficialWatchHistoryMemoryConflictTests(TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="animemo-memory-plugin-")
        self.addCleanup(temporary.cleanup)
        self.enterContext(override_settings(PLUGIN_ROOT=Path(temporary.name), PLUGIN_MIN_FREE_DISK_MB=0))
        self.addCleanup(runtime_registry.clear)
        self.user = get_user_model().objects.create_user(username="official-memory-owner")
        get_user_model().objects.create_superuser(username="official-memory-admin", password="Fixture-admin-Only-123!")
        self.entry = JournalEntry.objects.create(user=self.user, title="Memory 官方插件回归")
        call_command("sync_official_plugins", verbosity=0)
        self.plugin = PluginProject.objects.get(slug="watch-history-importer")
        install_for_user(self.plugin, user=self.user)
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def batch(self, notes):
        batch_id = uuid4().hex
        return PluginData.objects.create(
            plugin=self.plugin, namespace="batches", user=self.user, key=batch_id,
            value={
                "id": batch_id, "status": "ready", "summary": {}, "target_user_id": self.user.pk,
                "payload": {"groups": [{
                    "source_title": self.entry.title,
                    "resolution": {
                        "status": "matched", "bangumi_id": 456, "title": self.entry.title,
                        "japanese_title": "", "tags": [],
                    },
                    "records": [{
                        "watch_date": "2026-08-09", "watch_date_label": "2026年8月9日",
                        "brush": 2, "brush_label": "二刷", "episode_range": {"start": 1, "end": 12},
                        "notes": value,
                    } for value in notes],
                }]},
            },
        )

    def seed(self):
        add_history(user=self.user, entry=self.entry, record={
            "watched_on": "2026-08-09", "watched_label": "2026年8月9日",
            "brush_number": 2, "brush_label": "二刷", "episode_start": 1, "episode_end": 12,
            "notes": ["私人原始 Memory"], "metadata": {},
        })

    def commit(self, batch):
        return self.client.post(
            f"/api/plugins/watch-history-importer/batches/{batch.key}/commit/",
            {"excluded_group_indices": []}, format="json",
        )

    def assert_conflict_preserves_state(self, batch):
        history_before = list(self.entry.watch_history_records.values())
        entry_before = JournalEntry.objects.filter(pk=self.entry.pk).values().get()
        batch_before = deepcopy(batch.value)
        with self.captureOnCommitCallbacks(execute=True):
            response = self.commit(batch)
        self.assertEqual(response.status_code, 409, response.data)
        self.assertEqual(response.data["code"], "duplicate_watch_history")
        self.assertNotIn("私人", str(response.data))
        self.assertEqual(list(self.entry.watch_history_records.values()), history_before)
        self.assertEqual(JournalEntry.objects.filter(pk=self.entry.pk).values().get(), entry_before)
        batch.refresh_from_db()
        self.assertEqual(batch.value, batch_before)
        self.assertFalse(PluginData.objects.filter(plugin=self.plugin, namespace="subjects").exists())
        self.assertFalse(IntegrationEvent.objects.filter(plugin_slug=self.plugin.slug).exists())

    def test_actual_import_commit_conflict_rolls_back_entry_history_subject_and_batch(self):
        self.seed()
        self.assert_conflict_preserves_state(self.batch([["另一份私人 Memory"]]))

    def test_actual_import_batch_rejects_conflicting_duplicates_before_any_mutation(self):
        self.assert_conflict_preserves_state(self.batch([["第一份私人 Memory"], ["第二份私人 Memory"]]))

    def test_actual_import_commit_and_batch_retry_preserve_equivalent_memory(self):
        self.seed()
        before = list(self.entry.watch_history_records.values())
        batch = self.batch([["私人原始 Memory"]])
        with self.captureOnCommitCallbacks(execute=True):
            first = self.commit(batch)
        self.assertEqual(first.status_code, 200, first.data)
        self.assertEqual(first.data["summary"]["imported_history_records"], 0)
        event_count = IntegrationEvent.objects.filter(plugin_slug=self.plugin.slug).count()
        with self.captureOnCommitCallbacks(execute=True):
            second = self.commit(batch)
        self.assertEqual(second.status_code, 200, second.data)
        self.assertEqual(second.data, first.data)
        self.assertEqual(list(self.entry.watch_history_records.values()), before)
        self.assertEqual(IntegrationEvent.objects.filter(plugin_slug=self.plugin.slug).count(), event_count)
