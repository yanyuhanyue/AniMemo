from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIClient

from journal.analytics import build_user_analytics
from journal.models import ExternalMediaIdentity, JournalEntry
from journal.watch_history import add_history


class JournalQueryEfficiencyTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("query-user", password="StrongPass123!")
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.entries = []
        for index in range(20):
            entry = JournalEntry.objects.create(user=self.user, title=f"条目 {index}", personal_score="8.00")
            ExternalMediaIdentity.objects.create(
                entry=entry,
                provider="bangumi",
                external_id=str(1000 + index),
                canonical_url=f"https://bgm.tv/subject/{1000 + index}",
                metadata={"title": f"巨大 snapshot {index}", "detail": "x" * 1000},
                is_metadata_source=True,
            )
            add_history(
                user=self.user,
                entry=entry,
                record={"watched_on": "2026-08-09", "brush_label": f"第 {index + 1} 次"},
            )
            self.entries.append(entry)

    def test_entry_list_has_constant_queries_and_lightweight_summaries(self):
        with CaptureQueriesContext(connection) as captured:
            response = self.client.get("/api/entries/?page_size=all")

        self.assertEqual(response.status_code, 200)
        self.assertLessEqual(len(captured), 3)
        rows = response.data.get("results", response.data)
        self.assertEqual(len(rows), 20)
        self.assertEqual(rows[0]["watch_history_count"], 1)
        self.assertNotIn("watch_history", rows[0])
        self.assertNotIn("metadata", rows[0]["external_identities"][0])

    def test_watch_history_detail_query_count_does_not_scale_with_records(self):
        entry = self.entries[0]
        for index in range(2, 22):
            add_history(
                user=self.user,
                entry=entry,
                record={"watched_on": f"2026-07-{index:02d}", "brush_label": f"补录 {index}"},
            )

        with CaptureQueriesContext(connection) as captured:
            response = self.client.get(f"/api/entries/{entry.pk}/watch-history/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 21)
        self.assertLessEqual(len(captured), 2)

    def test_analytics_uses_bounded_aggregate_queries(self):
        with CaptureQueriesContext(connection) as captured:
            result = build_user_analytics(user=self.user)

        self.assertEqual(result["summary"]["total"], 20)
        self.assertLessEqual(len(captured), 6)
