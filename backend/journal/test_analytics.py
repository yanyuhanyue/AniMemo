from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from rest_framework import status
from rest_framework.test import APIClient

from journal.analytics import build_user_analytics
from journal.models import JournalEntry
from journal.watch_history import add_history


User = get_user_model()


@override_settings(TIME_ZONE="Asia/Shanghai")
class AnalyticsCoreTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="analytics-user", password="StrongPass123!")
        self.other = User.objects.create_user(username="analytics-other", password="StrongPass123!")
        self.completed = JournalEntry.objects.create(
            user=self.user,
            title="完成",
            watch_status=JournalEntry.WatchStatus.COMPLETED,
            personal_score="8.50",
            visibility=JournalEntry.Visibility.PUBLIC,
        )
        self.dropped = JournalEntry.objects.create(
            user=self.user,
            title="弃番",
            watch_status=JournalEntry.WatchStatus.DROPPED,
            personal_score="6.00",
        )
        JournalEntry.objects.create(
            user=self.user,
            title="计划",
            watch_status=JournalEntry.WatchStatus.PLANNED,
        )
        other_entry = JournalEntry.objects.create(user=self.other, title="其他用户", personal_score="10.00")
        for watched_on, entry in (
            ("2026-01-01", self.completed),
            ("2026-01-31", self.completed),
            ("2026-01-31", self.dropped),
            ("2026-02-01", self.completed),
            ("2026-01-15", other_entry),
        ):
            add_history(
                user=entry.user,
                entry=entry,
                record={"watched_on": watched_on, "brush_label": entry.title, "notes": []},
            )

    def test_authoritative_metrics_are_tenant_scoped_and_range_is_inclusive(self):
        result = build_user_analytics(user=self.user, start="2026-01-01", end="2026-01-31")

        self.assertEqual(result["summary"], {
            "total": 3,
            "average_score": 7.25,
            "shared": 1,
            "watch_history_count": 3,
            "active_days": 2,
        })
        self.assertEqual(result["status_distribution"]["completed"], 1)
        self.assertEqual(result["status_distribution"]["dropped"], 1)
        self.assertEqual(result["status_distribution"]["planned"], 1)
        self.assertEqual(result["score_distribution"], [
            {"score": "6.00", "count": 1},
            {"score": "8.50", "count": 1},
        ])
        self.assertEqual(result["monthly_activity"], [{"month": "2026-01", "count": 3}])
        self.assertEqual(result["range"]["boundaries"], "inclusive")
        self.assertEqual(result["range"]["timezone"], "Asia/Shanghai")

    def test_api_rejects_inverted_range_with_stable_error(self):
        client = APIClient()
        client.force_authenticate(self.user)

        response = client.get("/api/stats/me/", {"start": "2026-02-01", "end": "2026-01-01"})

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data["code"], "invalid_analytics_range")
