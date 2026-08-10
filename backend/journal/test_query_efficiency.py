from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from rest_framework.test import APIClient

from accounts.models import StaffProfile, UserSecurityProfile
from journal.analytics import build_user_analytics
from journal.models import Column, ExternalMediaIdentity, JournalEntry, UserSettings
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


class StaffDashboardQueryEfficiencyTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        user_model = get_user_model()
        cls.admin = user_model.objects.create_superuser(
            username="dashboard-query-admin",
            email="dashboard-query-admin@example.com",
            password="StrongPass123!",
        )
        users = []
        for index in range(100):
            user = user_model(
                username=f"dashboard-user-{index:03d}",
                email=f"dashboard-user-{index:03d}@example.com",
                is_staff=index % 4 == 0,
            )
            user.set_unusable_password()
            users.append(user)
        user_model.objects.bulk_create(users)
        cls.users = list(
            user_model.objects.filter(username__startswith="dashboard-user-").order_by("username")
        )
        StaffProfile.objects.bulk_create([
            StaffProfile(user=user, role=StaffProfile.Role.REVIEWER)
            for user in cls.users
            if user.is_staff
        ])
        UserSecurityProfile.objects.bulk_create([
            UserSecurityProfile(
                user=user,
                email_verified=index % 2 == 0,
                two_factor_enabled=index % 5 == 0,
            )
            for index, user in enumerate(cls.users)
            if index % 3 == 0
        ])
        UserSettings.objects.bulk_create([
            UserSettings(
                user=user,
                nickname=f"用户 {index}",
                public_status=(
                    UserSettings.PublicStatus.PENDING
                    if index % 10 == 0
                    else UserSettings.PublicStatus.APPROVED
                ),
            )
            for index, user in enumerate(cls.users)
            if index % 5 == 0
        ])
        JournalEntry.objects.bulk_create([
            JournalEntry(user=user, title=f"用户 {index} 条目 {entry_index}")
            for index, user in enumerate(cls.users)
            for entry_index in range(index % 3)
        ])
        Column.objects.bulk_create([
            Column(author=user, title=f"用户 {index} 专栏 {column_index}", body="正文")
            for index, user in enumerate(cls.users)
            for column_index in range(index % 2)
        ])

    def setUp(self):
        self.client = APIClient()
        self.client.force_authenticate(self.admin)

    def _dashboard_query_count(self):
        with CaptureQueriesContext(connection) as captured:
            response = self.client.get(reverse("staff-dashboard"))
        self.assertEqual(response.status_code, 200)
        return response, len(captured)

    def test_dashboard_query_budget_is_scale_insensitive(self):
        large_response, large_query_count = self._dashboard_query_count()
        self.assertEqual(len(large_response.data["users"]), 100)
        self.assertLessEqual(large_query_count, 30)
        staff_row = next(item for item in large_response.data["users"] if item["id"] == self.users[0].pk)
        self.assertEqual(staff_row["staff_role"], StaffProfile.Role.REVIEWER)

        get_user_model().objects.filter(pk__in=[user.pk for user in self.users[4:]]).delete()
        small_response, small_query_count = self._dashboard_query_count()
        self.assertEqual(len(small_response.data["users"]), 5)
        self.assertLessEqual(large_query_count, small_query_count + 5)

    def test_dashboard_get_does_not_create_missing_security_profile(self):
        missing_profile_user = self.users[1]
        self.assertFalse(UserSecurityProfile.objects.filter(user_id=missing_profile_user.pk).exists())
        profile_count_before = UserSecurityProfile.objects.count()

        response, _query_count = self._dashboard_query_count()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(UserSecurityProfile.objects.count(), profile_count_before)
        self.assertFalse(UserSecurityProfile.objects.filter(user_id=missing_profile_user.pk).exists())
        row = next(item for item in response.data["users"] if item["id"] == missing_profile_user.pk)
        self.assertFalse(row["email_verified"])
        self.assertFalse(row["two_factor_enabled"])
        self.assertEqual(row["entry_count"], 1)
        self.assertEqual(row["column_count"], 1)
