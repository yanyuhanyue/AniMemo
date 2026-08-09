from concurrent.futures import ThreadPoolExecutor
from importlib import import_module
from threading import Barrier
from types import SimpleNamespace
from unittest import skipUnless

from django.contrib.auth import get_user_model
from django.db import close_old_connections, connection, connections
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase, TransactionTestCase
from django.urls import reverse
from plugin_host.models import PluginProject, UserPluginInstallation
from rest_framework.test import APIClient

from journal.models import JournalEntry, WatchHistoryRecord
from journal.watch_history import WatchHistoryValidationError, add_history, list_history, merge_history


def record_payload(**overrides):
    return {
        "watched_on": "2026-08-09",
        "watched_label": "2026年8月9日",
        "brush_number": 1,
        "brush_label": "首刷",
        "episode_start": 1,
        "episode_end": 12,
        "notes": ["完整看完"],
        **overrides,
    }


class CoreWatchHistoryTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="history-owner", password="StrongPass123!")
        self.other = get_user_model().objects.create_user(username="history-other", password="StrongPass123!")
        self.entry = JournalEntry.objects.create(user=self.user, title="Core History")
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def collection_url(self, entry=None):
        return reverse("watch-history-collection", kwargs={"entry_id": (entry or self.entry).pk})

    def detail_url(self, record):
        return reverse(
            "watch-history-detail",
            kwargs={"entry_id": self.entry.pk, "record_id": record.pk},
        )

    def test_core_crud_works_without_plugin_installation(self):
        created = self.client.post(self.collection_url(), record_payload(), format="json")
        self.assertEqual(created.status_code, 201)
        record_id = created.data["record"]["id"]

        listed = self.client.get(self.collection_url())
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(listed.data["count"], 1)

        updated = self.client.patch(
            reverse("watch-history-detail", kwargs={"entry_id": self.entry.pk, "record_id": record_id}),
            {"notes": ["更新后的备注"]},
            format="json",
        )
        self.assertEqual(updated.status_code, 200)
        self.assertEqual(updated.data["notes"], ["更新后的备注"])

        deleted = self.client.delete(
            reverse("watch-history-detail", kwargs={"entry_id": self.entry.pk, "record_id": record_id})
        )
        self.assertEqual(deleted.status_code, 204)
        self.assertFalse(WatchHistoryRecord.objects.exists())

    def test_plugin_disable_and_uninstall_do_not_remove_core_history(self):
        record, _ = add_history(user=self.user, entry=self.entry, record=record_payload())
        plugin = PluginProject.objects.create(
            plugin_id="com.example.history-importer",
            slug="history-importer-test",
            name="History Importer",
            description="test",
        )
        installation = UserPluginInstallation.objects.create(user=self.user, plugin=plugin, enabled=False)
        self.assertEqual([item.pk for item in list_history(user=self.user, entry=self.entry)], [record.pk])
        installation.delete()
        self.assertEqual([item.pk for item in list_history(user=self.user, entry=self.entry)], [record.pk])

    def test_cross_user_history_is_hidden(self):
        foreign = JournalEntry.objects.create(user=self.other, title="Foreign")
        self.assertEqual(self.client.get(self.collection_url(foreign)).status_code, 404)
        self.assertEqual(self.client.post(self.collection_url(foreign), record_payload(), format="json").status_code, 404)

    def test_duplicate_semantic_identity_is_deterministic(self):
        first = self.client.post(self.collection_url(), record_payload(notes=["first"]), format="json")
        second = self.client.post(self.collection_url(), record_payload(notes=["second"]), format="json")
        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 200)
        self.assertFalse(second.data["created"])
        self.assertEqual(second.data["record"]["id"], first.data["record"]["id"])
        self.assertEqual(WatchHistoryRecord.objects.count(), 1)

    def test_merge_is_atomic_when_capacity_would_be_exceeded(self):
        add_history(user=self.user, entry=self.entry, record=record_payload(episode_start=600, episode_end=600))
        incoming = [
            record_payload(episode_start=index, episode_end=index)
            for index in range(1, 501)
        ]

        with self.assertRaises(WatchHistoryValidationError):
            merge_history(user=self.user, entry=self.entry, records=incoming)

        self.assertEqual(WatchHistoryRecord.objects.filter(entry=self.entry).count(), 1)

    def test_invalid_date_and_episode_range_are_rejected(self):
        invalid_date = self.client.post(self.collection_url(), record_payload(watched_on="2026-02-30"), format="json")
        invalid_range = self.client.post(
            self.collection_url(),
            record_payload(episode_start=12, episode_end=1),
            format="json",
        )
        self.assertEqual(invalid_date.status_code, 400)
        self.assertEqual(invalid_range.status_code, 400)

    def test_entry_list_contains_summary_not_full_history(self):
        add_history(user=self.user, entry=self.entry, record=record_payload())
        response = self.client.get(reverse("entry-list"), {"page_size": "all"})
        self.assertEqual(response.status_code, 200)
        rows = response.data.get("results", response.data)
        row = rows[0]
        self.assertNotIn("watch_history", row)
        self.assertEqual(row["watch_history_count"], 1)
        self.assertEqual(str(row["last_watched_on"]), "2026-08-09")
        self.assertEqual(str(row["first_watched_on"]), "2026-08-09")
        self.assertEqual(row["latest_episode_start"], 1)
        self.assertEqual(row["latest_episode_end"], 12)

    def test_history_collection_uses_bounded_pagination(self):
        for episode in range(1, 4):
            add_history(
                user=self.user,
                entry=self.entry,
                record=record_payload(episode_start=episode, episode_end=episode),
            )

        first = self.client.get(self.collection_url(), {"page_size": 2})
        second = self.client.get(self.collection_url(), {"page_size": 2, "page": 2})

        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.data["count"], 3)
        self.assertEqual(first.data["next_page"], 2)
        self.assertEqual(len(first.data["results"]), 2)
        self.assertEqual(second.data["count"], 3)
        self.assertIsNone(second.data["next_page"])
        self.assertEqual(len(second.data["results"]), 1)

    def test_entry_detail_does_not_reveal_another_users_private_entry(self):
        foreign = JournalEntry.objects.create(user=self.other, title="Private deep link")

        response = self.client.get(reverse("entry-detail", kwargs={"pk": foreign.pk}))

        self.assertEqual(response.status_code, 404)


@skipUnless(connection.vendor == "postgresql", "Requires PostgreSQL row-level locking")
class WatchHistoryPostgreSQLConcurrencyTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        self.user = get_user_model().objects.create_user(username="history-race", password="StrongPass123!")
        self.entry = JournalEntry.objects.create(user=self.user, title="History Race")

    def test_same_semantic_record_is_created_once_under_race(self):
        barrier = Barrier(2)

        def create_record(_index):
            close_old_connections()
            try:
                user = get_user_model().objects.get(pk=self.user.pk)
                entry = JournalEntry.objects.get(pk=self.entry.pk)
                barrier.wait(timeout=10)
                record, created = add_history(user=user, entry=entry, record=record_payload())
                return record.pk, created
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(create_record, range(2)))

        self.assertEqual(sorted(created for _pk, created in results), [False, True])
        self.assertEqual(len({pk for pk, _created in results}), 1)
        self.assertEqual(WatchHistoryRecord.objects.count(), 1)


class WatchHistoryMigrationTests(TransactionTestCase):
    migrate_from = [
        ("plugin_host", "0001_initial"),
        ("journal", "0003_external_account_connections"),
    ]
    migrate_to = [
        ("plugin_host", "0001_initial"),
        ("journal", "0004_core_watch_history_and_metadata_source"),
    ]

    def setUp(self):
        super().setUp()
        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_from)
        old_apps = executor.loader.project_state(self.migrate_from).apps
        User = old_apps.get_model("accounts", "User")
        JournalEntry = old_apps.get_model("journal", "JournalEntry")
        PluginProject = old_apps.get_model("plugin_host", "PluginProject")
        PluginData = old_apps.get_model("plugin_host", "PluginData")
        user = User.objects.create(username="migration-user")
        entry = JournalEntry.objects.create(user=user, title="Migrated History")
        plugin = PluginProject.objects.create(
            plugin_id="com.anime-journal.watch-history-importer",
            slug="watch-history-importer",
            name="Importer",
            description="fixture",
        )
        PluginData.objects.create(
            plugin=plugin,
            namespace="watch_history",
            key=str(entry.pk),
            user=user,
            value=[
                record_payload(source="legacy", notes=["保留备注"]),
                record_payload(source="newer duplicate", notes=["重复项最终值"]),
                record_payload(
                    watched_on="2026-08-10",
                    watched_label="次日",
                    brush_number=2,
                    brush_label="二刷",
                    episode_start=None,
                    episode_end=None,
                    notes=["第二条"],
                ),
            ],
        )
        self.entry_id = entry.pk
        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_to)
        self.apps = executor.loader.project_state(self.migrate_to).apps

    def test_data_migration_preserves_order_metadata_and_deduplicates(self):
        WatchHistoryRecord = self.apps.get_model("journal", "WatchHistoryRecord")
        PluginData = self.apps.get_model("plugin_host", "PluginData")
        rows = list(WatchHistoryRecord.objects.filter(entry_id=self.entry_id).order_by("sequence"))
        self.assertEqual(len(rows), 2)
        self.assertEqual([row.sequence for row in rows], [1, 2])
        self.assertEqual(rows[0].notes, ["重复项最终值"])
        self.assertEqual(rows[0].metadata, {"source": "newer duplicate"})
        self.assertEqual(rows[1].watched_label, "次日")
        self.assertFalse(PluginData.objects.filter(namespace="watch_history").exists())

    def test_migration_normalizer_fails_closed_for_malformed_fixture(self):
        migration = import_module("journal.migrations.0004_core_watch_history_and_metadata_source")
        row = SimpleNamespace(pk=99)
        with self.assertRaisesRegex(RuntimeError, "cannot be migrated safely"):
            migration._normalize({"watched_on": "not-a-date"}, row, 0)
