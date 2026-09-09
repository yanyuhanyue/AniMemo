"""Public discovery must respect the owner's current publication decision."""

import json
from datetime import date
from unittest import skipUnless

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APITestCase
from site_config.models import InstallationState, SiteSettings

from .models import (
    ExternalMediaIdentity,
    JournalEntry,
    UserSettings,
    WatchHistoryRecord,
)
from .public_catalog_test_support import public_catalog_response


@skipUnless(connection.vendor == "postgresql", "Bounded public visibility oracles require PostgreSQL")
class PublicVisibilityTests(APITestCase):
    @classmethod
    def setUpTestData(cls):
        user_model = get_user_model()
        cls.owner = user_model.objects.create_user("public-owner", is_staff=True)
        cls.staff = user_model.objects.create_user("other-staff", is_staff=True)
        cls.member = user_model.objects.create_user("private-member")
        installation = InstallationState.load()
        installation.status = InstallationState.Status.INITIALIZED
        installation.save()
        cls.publication, _ = UserSettings.objects.get_or_create(user=cls.owner)
        cls.publication.allow_sharing = True
        cls.publication.public_status = UserSettings.PublicStatus.APPROVED
        cls.publication.save()
        cls.site = SiteSettings.load()
        cls.site.homepage_owner = cls.owner
        cls.site.save()
        cls.entries = {}
        for visibility in JournalEntry.Visibility.values:
            cls.entries[visibility] = JournalEntry.objects.create(
                user=cls.owner, title=f"{visibility}-title", review=f"{visibility}-review",
                visibility=visibility, personal_score="8.50", tags=[f"{visibility}-tag"],
            )
        JournalEntry.objects.create(user=cls.owner, title="deleted-title", review="deleted-review",
                                    visibility="public", deleted_at=timezone.now())
        JournalEntry.objects.create(user=cls.staff, title="fallback-title", visibility="public")
        JournalEntry.objects.create(user=cls.member, title="member-title", visibility="private")

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)

    def _assert_only_public(self, response):
        self.assertEqual(response.status_code, 200)
        self.assertEqual(set(response.data), {"stats", "results"})
        self.assertEqual([row["id"] for row in response.data["results"]], [self.entries["public"].pk])
        self.assertEqual(response.data["stats"]["total"], 1)
        self.assertEqual(response.data["stats"]["average_score"], 8.5)
        text = json.dumps(response.data, default=str)
        for marker in ("private-title", "private-review", "unlisted-title", "unlisted-review", "deleted-title", "fallback-title", "member-title"):
            self.assertNotIn(marker, text)

    def test_homepage_is_public_for_every_caller_identity(self):
        for actor in (None, self.member, self.owner, self.staff):
            with self.subTest(actor=actor.pk if actor else "anonymous"):
                self.client.force_authenticate(actor)
                self._assert_only_public(public_catalog_response(self.client))

    def test_homepage_never_falls_back_from_missing_or_invalid_owner(self):
        for owner, active, staff in ((None, True, True), (self.owner, False, True), (self.owner, True, False)):
            with self.subTest(owner=owner.pk if owner else None, active=active, staff=staff):
                get_user_model().objects.filter(pk=self.owner.pk).update(is_active=active, is_staff=staff)
                self.site.homepage_owner = owner
                self.site.save()
                response = public_catalog_response(self.client)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(set(response.data), {"stats", "results"})
                self.assertEqual(response.data["results"], [])
                self.assertEqual(response.data["stats"]["total"], 0)

    def test_homepage_requires_current_sharing_approval(self):
        for state, allow in (("private", True), ("pending", True), ("approved", False)):
            with self.subTest(state=state, allow=allow):
                UserSettings.objects.filter(pk=self.publication.pk).update(public_status=state, allow_sharing=allow)
                response = public_catalog_response(self.client)
                self.assertEqual(response.data["results"], [])
                self.assertEqual(response.data["stats"]["total"], 0)
        self.publication.delete()
        response = public_catalog_response(self.client)
        self.assertEqual(response.data["results"], [])
        self.assertFalse(UserSettings.objects.filter(user=self.owner).exists())

    def test_public_results_do_not_serialize_private_storage_or_identity_metadata(self):
        entry = self.entries["public"]
        ExternalMediaIdentity.objects.create(
            entry=entry, provider="bangumi", external_id="100", canonical_url="https://bgm.tv/subject/100",
            metadata={"title": "public-metadata-title", "private_note": "private-metadata-sentinel"},
        )
        response = public_catalog_response(self.client)
        row = next(row for row in response.data["results"] if row["id"] == entry.pk)
        for field in ("poster_file", "custom_poster_url", "share_slug", "share_url", "external_identities", "user", "email"):
            self.assertNotIn(field, row)
        self.assertNotIn("private-metadata-sentinel", json.dumps(row, default=str))
        self.assertEqual(row["review"], "public-review")

    def test_catalog_search_filters_private_unlisted_deleted_and_unpublished_owners(self):
        # This discovery API retains its existing authentication requirement.
        self.client.force_authenticate(self.member)
        response = self.client.get(reverse("public-catalog-search"), {"q": "title"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 1)
        self.assertEqual([row["id"] for row in response.data["results"]], [self.entries["public"].pk])
        self.assertNotIn("review", response.data["results"][0])
        for query in ("private-title", "unlisted-title", "deleted-title", "fallback-title"):
            with self.subTest(query=query):
                result = self.client.get(reverse("public-catalog-search"), {"q": query})
                self.assertEqual(result.data["count"], 0)
                self.assertEqual(result.data["results"], [])

    def test_publication_revocation_is_immediate_across_public_reads(self):
        self._assert_only_public(public_catalog_response(self.client))
        self.publication.allow_sharing = False
        self.publication.save()
        self.assertEqual(public_catalog_response(self.client).data["results"], [])
        self.assertEqual(self.client.get(reverse("public-showcase-directory")).data["results"], [])
        self.assertEqual(public_catalog_response(self.client, public_slug=self.publication.public_slug).status_code, 404)
        self.assertEqual(self.client.get(reverse("shared-entry", args=[self.entries["public"].share_slug])).status_code, 404)
        self.client.force_authenticate(self.member)
        self.assertEqual(self.client.get(reverse("public-catalog-search")).data["results"], [])

    def test_showcase_owner_preview_and_unlisted_share_keep_separate_visibility(self):
        for actor in (None, self.member, self.staff, self.owner):
            with self.subTest(actor=actor.pk if actor else "anonymous"):
                self.client.force_authenticate(actor)
                response = public_catalog_response(self.client, public_slug=self.publication.public_slug)
                self.assertEqual(response.status_code, 200)
                expected = set(self.entries) if actor == self.owner else {"public"}
                self.assertEqual({row["visibility"] for row in response.data["results"]}, expected)
                self.assertEqual(response.data["stats"]["total"], len(expected))
                for visibility, entry in self.entries.items():
                    shared = self.client.get(reverse("shared-entry", args=[entry.share_slug]))
                    self.assertEqual(shared.status_code, 404 if visibility == "private" else 200)

    def test_disabled_owner_has_no_public_surface(self):
        get_user_model().objects.filter(pk=self.owner.pk).update(is_active=False)
        self.assertEqual(public_catalog_response(self.client).data["results"], [])
        self.assertEqual(self.client.get(reverse("public-showcase-directory")).data["results"], [])
        self.assertEqual(public_catalog_response(self.client, public_slug=self.publication.public_slug).status_code, 404)
        self.assertEqual(self.client.get(reverse("shared-entry", args=[self.entries["public"].share_slug])).status_code, 404)
        self.client.force_authenticate(self.member)
        self.assertEqual(self.client.get(reverse("public-catalog-search")).data["results"], [])


@skipUnless(connection.vendor == "postgresql", "Bounded public SQL budgets require PostgreSQL")
class PublicQueryBudgetTests(APITestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.owner = get_user_model().objects.create_user("budget-owner", is_staff=True)
        UserSettings.objects.create(user=self.owner, allow_sharing=True, public_status="approved")
        site = SiteSettings.load()
        site.homepage_owner = self.owner
        site.save()

    def test_homepage_query_count_does_not_grow_per_entry(self):
        counts, summary_counts = [], []
        for size in (1, 10, 50):
            for index in range(JournalEntry.objects.count(), size):
                entry = JournalEntry.objects.create(user=self.owner, title=f"entry-{index}", visibility="public")
                WatchHistoryRecord.objects.create(entry=entry, watched_on=date(2026, 8, 1),
                                                  watched_label="2026.8.1", brush_label="首刷", sequence=1)
            with CaptureQueriesContext(connection) as captured:
                response = self.client.get(reverse("public-homepage-entries"))
            with CaptureQueriesContext(connection) as summary_queries:
                summary = self.client.get(reverse("public-homepage-summary"))
            self.assertEqual(response.status_code, 200)
            self.assertEqual(len(response.data["results"]), size)
            self.assertEqual(summary.status_code, 200)
            self.assertEqual(summary.data["stats"]["total"], size)
            self.assertEqual(response.data["total"], size)
            self.assertEqual({row["watch_history_count"] for row in response.data["results"]}, {1})
            expected = sorted(JournalEntry.objects.values("pk", "title"), key=lambda row: row["title"])
            self.assertEqual([row["id"] for row in response.data["results"]], [row["pk"] for row in expected])
            counts.append(len(captured))
            summary_counts.append(len(summary_queries))
        self.assertLessEqual(max(counts), min(counts) + 1, counts)
        self.assertLessEqual(max(counts), 6, counts)
        self.assertLessEqual(max(summary_counts), min(summary_counts) + 1, summary_counts)
        # Collation + homepage owner + publication + aggregate + scalar score batch.
        self.assertLessEqual(max(summary_counts), 5, summary_counts)
