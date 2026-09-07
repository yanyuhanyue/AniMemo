from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Barrier
from types import SimpleNamespace
from unittest import skipUnless

from django.contrib.auth import get_user_model
from django.db import close_old_connections, connection, connections
from django.test import TestCase, TransactionTestCase
from django.test.utils import CaptureQueriesContext
from plugin_host.errors import HostCapabilityError
from plugin_host.models import PluginProject, UserPluginInstallation
from plugin_host.runtime.capabilities import PluginWatchHistoryCapability
from rest_framework.test import APIClient

from journal.data_bundle import export_data_bundle, import_data_bundle
from journal.data_bundle.services import DataBundleError
from journal.models import JournalEntry
from journal.watch_history import (
    WatchHistoryValidationError,
    add_history,
    merge_history,
    merge_watch_history_records,
    normalize_watch_history_record,
    normalize_watch_history_records,
    replace_history,
)


def memory(**changes):
    return {
        "watched_on": "2026-08-09",
        "watched_label": "2026年8月9日",
        "brush_number": 1,
        "brush_label": "重温",
        "episode_start": 1,
        "episode_end": 12,
        "notes": ["私人记忆：雨夜重温"],
        "metadata": {"place": "私人地点", "nested": {"flag": True}},
        **changes,
    }


class WatchHistoryConflictTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user(username="conflict-owner")
        cls.receiver = get_user_model().objects.create_user(username="conflict-receiver")
        cls.entry = JournalEntry.objects.create(user=cls.user, title="独立 Memory fixture")

    def snapshot(self):
        return list(self.entry.watch_history_records.order_by("sequence").values())

    def assert_private_conflict(self, error):
        self.assertEqual(error.code, "duplicate_watch_history")
        self.assertNotIn("私人记忆", str(error))
        self.assertNotIn("私人地点", str(error))

    def test_add_accepts_only_equivalent_retries_and_preserves_the_stored_memory(self):
        original, created = add_history(user=self.user, entry=self.entry, record=memory())
        self.assertTrue(created)
        before = self.snapshot()
        retry, created = add_history(user=self.user, entry=self.entry, record=memory())
        self.assertFalse(created)
        self.assertEqual(retry.pk, original.pk)
        for changes in (
            {"notes": ["另一份私人记忆"]},
            {"metadata": {"place": "另一处私人地点"}},
            {"metadata": {"place": "私人地点", "nested": {"flag": 1}}},
            {"brush_number": 2},
            {"watched_label": "同日另一种标签"},
        ):
            with self.subTest(changes=changes):
                with self.assertRaises(WatchHistoryValidationError) as caught:
                    add_history(user=self.user, entry=self.entry, record=memory(**changes))
                self.assert_private_conflict(caught.exception)
                self.assertEqual(self.snapshot(), before)

    def test_replace_rejects_conflicting_duplicates_before_deleting_any_old_rows(self):
        add_history(user=self.user, entry=self.entry, record=memory(episode_start=30, episode_end=30))
        before = self.snapshot()
        for changed in (memory(notes=["另一份私人记忆"]), memory(metadata={"different": True}), memory(brush_number=2)):
            for records in ([memory(), changed], [changed, memory()]):
                with self.subTest(records=records), CaptureQueriesContext(connection) as queries:
                    with self.assertRaises(WatchHistoryValidationError) as caught:
                        replace_history(user=self.user, entry=self.entry, records=records)
                    self.assert_private_conflict(caught.exception)
                self.assertFalse(any("DELETE" in item["sql"].upper() for item in queries))
                self.assertEqual(self.snapshot(), before)

    def test_merge_preflights_conflicts_even_after_an_otherwise_new_incoming_record(self):
        add_history(user=self.user, entry=self.entry, record=memory())
        before = self.snapshot()
        for changes in ({"notes": ["另一份记忆"]}, {"metadata": {"different": True}}, {"brush_number": 2}):
            with self.subTest(changes=changes):
                with self.assertRaises(WatchHistoryValidationError) as caught:
                    merge_history(
                        user=self.user, entry=self.entry,
                        records=[memory(episode_start=20, episode_end=21), memory(**changes)],
                    )
                self.assert_private_conflict(caught.exception)
                self.assertEqual(self.snapshot(), before)

    def test_bundle_rejects_non_equivalent_history_duplicates_before_creating_entries(self):
        add_history(user=self.user, entry=self.entry, record=memory())
        payload = export_data_bundle(user=self.user)
        payload["entries"][0]["watch_history"].append(memory(notes=["另一份私人记忆"]))
        before = self.snapshot()
        with self.assertRaises(DataBundleError) as caught:
            import_data_bundle(user=self.receiver, payload=payload)
        self.assert_private_conflict(caught.exception)
        self.assertFalse(JournalEntry.objects.filter(user=self.receiver).exists())
        self.assertEqual(self.snapshot(), before)

    def test_plugin_host_real_add_and_merge_reject_conflicts(self):
        plugin = PluginProject.objects.create(
            plugin_id="com.example.memory-conflict", slug="memory-conflict-test",
            name="Memory conflict fixture", description="isolated capability fixture",
            installation_mode=PluginProject.InstallationMode.USER,
        )
        UserPluginInstallation.objects.create(user=self.user, plugin=plugin, enabled=True)
        history = PluginWatchHistoryCapability(plugin.slug).bind(SimpleNamespace(user=self.user))
        original = history.add_history(self.entry.pk, memory())
        retry = history.add_history(self.entry.pk, deepcopy(memory()))
        self.assertFalse(retry["created"])
        self.assertEqual(retry["record"]["id"], original["record"]["id"])
        before = self.snapshot()
        with self.assertRaises(HostCapabilityError) as caught:
            history.add_history(self.entry.pk, memory(notes=["另一份私人记忆"]))
        self.assert_private_conflict(caught.exception)
        with self.assertRaises(HostCapabilityError) as caught:
            history.merge_history(self.entry.pk, [memory(episode_start=15, episode_end=15), memory(metadata={"changed": True})])
        self.assert_private_conflict(caught.exception)
        self.assertEqual(self.snapshot(), before)

    def test_core_http_conflict_is_explicit_and_contains_no_private_content(self):
        client = APIClient()
        client.force_authenticate(self.user)
        url = f"/api/v1/entries/{self.entry.pk}/watch-history/"
        self.assertEqual(client.post(url, memory(), format="json").status_code, 201)
        before = self.snapshot()
        response = client.post(url, memory(notes=["另一份私人记忆"]), format="json")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["code"], "duplicate_watch_history")
        self.assertNotIn("私人记忆", str(response.data))
        self.assertNotIn("私人地点", str(response.data))
        self.assertEqual(self.snapshot(), before)

    def test_batch_normalization_preserves_only_equivalent_retries(self):
        self.assertEqual(len(normalize_watch_history_records([memory(), deepcopy(memory())])), 1)
        with self.assertRaises(WatchHistoryValidationError) as caught:
            normalize_watch_history_records([memory(), memory(notes=["不同记忆"])])
        self.assert_private_conflict(caught.exception)

    def test_normalized_equivalent_retries_keep_original_row_identity(self):
        original, _ = add_history(user=self.user, entry=self.entry, record=memory())
        before = self.snapshot()
        normalized_retry = memory(
            watched_on=" 2026-08-09 ", brush_number="1", episode_start="1",
            watched_label=" 2026年8月9日 ", notes=[" 私人记忆：雨夜重温 "],
            metadata={"nested": {"flag": True}, "place": "私人地点"},
        )
        retry, created = add_history(user=self.user, entry=self.entry, record=normalized_retry)
        self.assertFalse(created)
        self.assertEqual(retry.pk, original.pk)
        self.assertEqual(self.snapshot(), before)

    def test_identical_replace_and_merge_retries_preserve_ids_sequences_and_timestamps(self):
        first = replace_history(user=self.user, entry=self.entry, records=[memory(), deepcopy(memory())])
        self.assertEqual(len(first), 1)
        before = self.snapshot()
        second = replace_history(user=self.user, entry=self.entry, records=[memory()])
        self.assertEqual(second[0].pk, first[0].pk)
        records, created, skipped = merge_history(user=self.user, entry=self.entry, records=[memory(), deepcopy(memory())])
        self.assertEqual((len(records), created, skipped), (1, 0, 1))
        self.assertEqual(self.snapshot(), before)

    def test_explicit_replace_and_patch_still_edit_the_requested_memory(self):
        original, _ = add_history(user=self.user, entry=self.entry, record=memory())
        changed = replace_history(user=self.user, entry=self.entry, records=[memory(notes=["有意修改后的记录"])])
        self.assertEqual(changed[0].notes, ["有意修改后的记录"])
        client = APIClient()
        client.force_authenticate(self.user)
        response = client.patch(
            f"/api/v1/entries/{self.entry.pk}/watch-history/{changed[0].pk}/",
            {"notes": ["再次明确编辑"]}, format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["notes"], ["再次明确编辑"])
        self.assertFalse(self.entry.watch_history_records.filter(pk=original.pk).exists())

    def test_merge_rejects_non_equivalent_duplicates_inside_incoming_batch(self):
        add_history(user=self.user, entry=self.entry, record=memory(episode_start=30, episode_end=31))
        before = self.snapshot()
        with self.assertRaises(WatchHistoryValidationError) as caught:
            merge_history(user=self.user, entry=self.entry, records=[memory(), memory(metadata={"different": True})])
        self.assert_private_conflict(caught.exception)
        self.assertEqual(self.snapshot(), before)

    def test_standard_brush_number_label_contradictions_are_explicitly_rejected(self):
        for number, label in ((2, "首刷"), (2, "一刷"), (1, "二刷"), (3, "2刷"), (1, "十刷")):
            with self.subTest(number=number, label=label):
                with self.assertRaises(WatchHistoryValidationError) as caught:
                    add_history(user=self.user, entry=self.entry, record=memory(brush_number=number, brush_label=label))
                self.assertEqual(caught.exception.code, "invalid_brush_number")
        self.assertEqual(self.snapshot(), [])
        self.assertIsNone(normalize_watch_history_record(memory(brush_number=None, brush_label="二刷"))["brush_number"])
        self.assertEqual(normalize_watch_history_record(memory(brush_number=3, brush_label="重温"))["brush_number"], 3)

    def test_http_replace_rejects_conflicting_batch_with_409_before_old_collection_changes(self):
        add_history(user=self.user, entry=self.entry, record=memory(episode_start=20, episode_end=20))
        before = self.snapshot()
        client = APIClient()
        client.force_authenticate(self.user)
        response = client.put(
            f"/api/v1/entries/{self.entry.pk}/watch-history/",
            {"records": [memory(), memory(metadata={"different": True})]}, format="json",
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["code"], "duplicate_watch_history")
        self.assertEqual(self.snapshot(), before)

    def test_bundle_equivalent_duplicates_restore_once_without_changing_memory_fields(self):
        add_history(user=self.user, entry=self.entry, record=memory())
        exported = export_data_bundle(user=self.user)
        incoming = deepcopy(exported)
        incoming["entries"][0]["watch_history"].append(deepcopy(memory()))
        result = import_data_bundle(user=self.receiver, payload=incoming)
        self.assertEqual(result["created"], 1)
        self.assertEqual(export_data_bundle(user=self.receiver)["entries"], exported["entries"])

    def test_legacy_merge_helper_rejects_collisions_and_capacity_without_truncating_old_memory(self):
        existing = [memory()]
        before = deepcopy(existing)
        merged, created, skipped = merge_watch_history_records(existing, [memory()])
        self.assertEqual((merged, created, skipped), (before, 0, 1))
        with self.assertRaises(WatchHistoryValidationError) as caught:
            merge_watch_history_records(existing, [memory(notes=["不同记忆"])])
        self.assert_private_conflict(caught.exception)
        self.assertEqual(existing, before)
        full = [memory(episode_start=index, episode_end=index) for index in range(1, 501)]
        full_before = deepcopy(full)
        with self.assertRaises(WatchHistoryValidationError):
            merge_watch_history_records(full, [memory(episode_start=501, episode_end=501)])
        self.assertEqual(full, full_before)


@skipUnless(connection.vendor == "postgresql", "Requires PostgreSQL row-level locking")
class WatchHistoryConflictPostgreSQLTests(TransactionTestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="memory-race-owner")
        self.entry = JournalEntry.objects.create(user=self.user, title="Memory race fixture")

    def race(self, payloads):
        barrier = Barrier(2)

        def create(payload):
            close_old_connections()
            try:
                user = get_user_model().objects.get(pk=self.user.pk)
                entry = JournalEntry.objects.get(pk=self.entry.pk)
                barrier.wait(timeout=10)
                try:
                    record, created = add_history(user=user, entry=entry, record=payload)
                    return {"id": record.pk, "created": created, "notes": record.notes}
                except WatchHistoryValidationError as error:
                    return {"error": error.code, "detail": error.detail}
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as executor:
            return list(executor.map(create, payloads))

    def test_concurrent_equivalent_retries_create_once(self):
        results = self.race([memory(), deepcopy(memory())])
        self.assertEqual(sorted(result["created"] for result in results), [False, True])
        self.assertEqual(len({result["id"] for result in results}), 1)
        self.assertEqual(self.entry.watch_history_records.count(), 1)

    def test_concurrent_different_memories_return_one_conflict_without_overwriting_winner(self):
        results = self.race([memory(notes=["第一份私人记忆"]), memory(notes=["第二份私人记忆"])])
        successes = [result for result in results if "created" in result]
        failures = [result for result in results if "error" in result]
        self.assertEqual(len(successes), 1)
        self.assertTrue(successes[0]["created"])
        self.assertEqual([result["error"] for result in failures], ["duplicate_watch_history"])
        self.assertNotIn("私人记忆", failures[0]["detail"])
        stored = self.entry.watch_history_records.get()
        self.assertEqual(stored.pk, successes[0]["id"])
        self.assertEqual(stored.notes, successes[0]["notes"])
