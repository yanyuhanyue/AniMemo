from types import SimpleNamespace

from django.contrib.auth import get_user_model
from django.test import TestCase

from journal.models import JournalEntry, WatchHistoryRecord
from plugin_host.models import PluginProject, UserPluginInstallation
from plugin_host.runtime.capabilities import (
    HostCapabilityError,
    PluginAnalyticsCapability,
    PluginJournalCapability,
    PluginWatchHistoryCapability,
)


class PluginCoreCapabilityTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("capability-user", password="password-123")
        self.other = get_user_model().objects.create_user("capability-other", password="password-123")
        self.plugin = PluginProject.objects.create(
            plugin_id="com.example.capability",
            slug="capability-test",
            name="Capability Test",
            description="test",
            installation_mode=PluginProject.InstallationMode.USER,
        )
        self.installation = UserPluginInstallation.objects.create(user=self.user, plugin=self.plugin, enabled=True)
        self.actor = SimpleNamespace(user=self.user)
        self.journal = PluginJournalCapability(self.plugin.slug).bind(self.actor)
        self.history = PluginWatchHistoryCapability(self.plugin.slug).bind(self.actor)
        self.analytics = PluginAnalyticsCapability(self.plugin.slug).bind(self.actor)

    def test_journal_and_history_use_only_bound_user(self):
        foreign = JournalEntry.objects.create(user=self.other, title="Other")
        created = self.journal.create_entry({"title": "Local", "watch_status": "completed"})
        self.assertEqual(created["title"], "Local")
        self.assertEqual(JournalEntry.objects.get(pk=created["entry_id"]).user, self.user)

        added = self.history.add_history(created["entry_id"], {"watched_on": "2026-08-09"})
        self.assertTrue(added["created"])
        self.assertEqual(WatchHistoryRecord.objects.get(entry_id=created["entry_id"]).watched_label, "2026年8月9日")
        with self.assertRaises(HostCapabilityError) as caught:
            self.history.list_history(foreign.pk)
        self.assertEqual(caught.exception.code, "entry_not_found")

    def test_disabled_installation_revokes_existing_bound_capability(self):
        entry = JournalEntry.objects.create(user=self.user, title="Local")
        self.installation.enabled = False
        self.installation.save(update_fields=["enabled"])
        with self.assertRaises(HostCapabilityError) as caught:
            self.journal.get_entry(entry.pk)
        self.assertEqual(caught.exception.code, "plugin_disabled")

    def test_history_normalization_and_merge_are_core_owned(self):
        entry = JournalEntry.objects.create(user=self.user, title="Local")
        incoming = [{
            "watched_on": "2026-08-09",
            "brush_number": "2",
            "brush_label": "  二刷  ",
            "episode_start": "7",
            "episode_end": "8",
            "notes": "  备注  ",
        }]
        normalized = self.history.normalize(incoming)
        self.assertEqual(normalized[0]["notes"], ["备注"])
        first = self.history.merge_history(entry.pk, incoming)
        second = self.history.merge_history(entry.pk, incoming)
        self.assertEqual((first["created"], first["skipped"]), (1, 0))
        self.assertEqual((second["created"], second["skipped"]), (0, 1))

    def test_journal_rejects_non_dto_fields(self):
        with self.assertRaises(HostCapabilityError) as caught:
            self.journal.create_entry({"title": "Local", "user_id": self.other.pk})
        self.assertEqual(caught.exception.code, "invalid_entry")

    def test_analytics_is_read_only_user_scoped_and_revoked_immediately(self):
        JournalEntry.objects.create(user=self.user, title="统计条目", watch_status="dropped")
        JournalEntry.objects.create(user=self.other, title="其他用户条目")

        result = self.analytics.get()

        self.assertEqual(result["summary"]["total"], 1)
        self.assertEqual(result["status_distribution"]["dropped"], 1)
        self.installation.enabled = False
        self.installation.save(update_fields=["enabled"])
        with self.assertRaises(HostCapabilityError) as caught:
            self.analytics.get()
        self.assertEqual(caught.exception.code, "plugin_disabled")
