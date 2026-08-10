from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from journal.models import ExternalMediaIdentity, JournalEntry


class DashboardLargeDatasetPaginationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        user_model = get_user_model()
        cls.users = {
            size: user_model.objects.create_user(f"dashboard-{size}", password="StrongPass123!")
            for size in (100, 500, 1001)
        }
        for size, user in cls.users.items():
            entries = []
            for index in range(size):
                is_target = size == 1001 and index == 900
                entries.append(JournalEntry(
                    user=user,
                    title=f"记录-{index:04d}",
                    japanese_title="跨页唯一搜索目标" if is_target else f"作品-{index:04d}",
                    airing_period="2099-01" if is_target else f"{2010 + index % 10}-01",
                    tags=["跨页标签", "共同标签"] if is_target else ["共同标签"],
                    personal_score=None if index % 11 == 0 else str(index % 11),
                    watch_status=JournalEntry.WatchStatus.DROPPED if is_target else JournalEntry.WatchStatus.PLANNED,
                ))
            JournalEntry.objects.bulk_create(entries, batch_size=250)

        cls.target = JournalEntry.objects.get(user=cls.users[1001], japanese_title="跨页唯一搜索目标")
        cls.deleted = JournalEntry.objects.create(
            user=cls.users[1001],
            title="不应出现的软删除记录",
            deleted_at=timezone.now(),
        )
        cls.other_user = user_model.objects.create_user("dashboard-other", password="StrongPass123!")
        cls.other_entry = JournalEntry.objects.create(user=cls.other_user, title="其他用户记录")
        ExternalMediaIdentity.objects.create(
            entry=cls.target,
            provider="bangumi",
            external_id="dashboard-target",
            canonical_url="https://bgm.tv/subject/999999",
        )

    def setUp(self):
        self.client = APIClient()

    def authenticate(self, size):
        self.client.force_authenticate(self.users[size])

    def test_default_pagination_for_100_500_and_1000_plus_records(self):
        for size in (100, 500, 1001):
            with self.subTest(size=size):
                self.authenticate(size)
                first = self.client.get(reverse("entry-list"))
                self.assertEqual(first.status_code, 200)
                self.assertEqual(first.data["count"], size)
                self.assertEqual(len(first.data["results"]), 48)
                self.assertIsNone(first.data["previous"])
                self.assertIsNotNone(first.data["next"])

                last_page = (size + 47) // 48
                last = self.client.get(reverse("entry-list"), {"page": last_page})
                self.assertEqual(last.status_code, 200)
                self.assertEqual(len(last.data["results"]), size - (last_page - 1) * 48)
                self.assertIsNotNone(last.data["previous"])
                self.assertIsNone(last.data["next"])

    def test_search_status_tag_year_and_sort_cover_the_full_dataset(self):
        self.authenticate(1001)
        cases = (
            ({"search": "跨页唯一搜索目标"}, self.target.pk),
            ({"status": JournalEntry.WatchStatus.DROPPED}, self.target.pk),
            ({"tag": "跨页标签"}, self.target.pk),
            ({"year": "2099"}, self.target.pk),
        )
        for params, expected_id in cases:
            with self.subTest(params=params):
                response = self.client.get(reverse("entry-list"), params)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.data["count"], 1)
                self.assertEqual(response.data["results"][0]["id"], expected_id)

        ascending = self.client.get(reverse("entry-list"), {"ordering": "title", "priority": "0"})
        descending = self.client.get(reverse("entry-list"), {"ordering": "-title", "priority": "0"})
        self.assertEqual(ascending.data["results"][0]["title"], "记录-0000")
        self.assertEqual(descending.data["results"][0]["title"], "记录-1000")

        score_desc = self.client.get(reverse("entry-list"), {"ordering": "-personal_score", "priority": "0"})
        self.assertEqual(float(score_desc.data["results"][0]["personal_score"]), 10.0)
        self.assertTrue(all(item["personal_score"] is not None for item in score_desc.data["results"][:48]))

    def test_quick_filter_facets_owner_isolation_and_soft_delete(self):
        self.authenticate(1001)
        quick = self.client.get(reverse("entry-list"), {
            "quick_tags": "跨页标签",
            "quick_title_keywords": "不会匹配",
            "quick_match_mode": "any",
        })
        self.assertEqual(quick.status_code, 200)
        self.assertEqual(quick.data["count"], 1)
        self.assertEqual(quick.data["results"][0]["id"], self.target.pk)

        facets = self.client.get(reverse("entry-list"), {
            "status": JournalEntry.WatchStatus.DROPPED,
            "include_facets": "1",
        })
        self.assertEqual(facets.status_code, 200)
        self.assertIn("共同标签", facets.data["facets"]["tags"])
        self.assertIn("跨页标签", facets.data["facets"]["tags"])
        self.assertIn("2099", facets.data["facets"]["years"])

        all_entries = self.client.get(reverse("entry-list"), {"page_size": 500})
        returned_ids = {item["id"] for item in all_entries.data["results"]}
        self.assertEqual(all_entries.data["count"], 1001)
        self.assertNotIn(self.deleted.pk, returned_ids)
        self.assertNotIn(self.other_entry.pk, returned_ids)

        external = self.client.get(reverse("entry-list"), {"activity": "external-bound"})
        self.assertEqual(external.data["count"], 1)
        self.assertEqual(external.data["results"][0]["id"], self.target.pk)
