"""PostgreSQL contract and regression checks for bounded public reads."""
import json
import time
from datetime import date, datetime
from datetime import timezone as dt_timezone
from decimal import Decimal
from unittest import SkipTest
from unittest.mock import patch
from urllib.parse import parse_qsl, quote, urlencode, urlsplit

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db import connection
from django.test import SimpleTestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework.response import Response
from rest_framework.test import APIRequestFactory, APITestCase
from site_config.models import InstallationState, SiteSettings, TagDefinition

from .models import JournalEntry, UserSettings, WatchHistoryRecord
from .public_catalog import query as sql_query
from .public_catalog.contracts import (
    FIELDS,
    PUBLIC_FIELDS,
    SUCCESS_BYTES,
    CatalogError,
    PublicQuery,
)
from .public_catalog.query import require_database
from .public_catalog_views import PublicCatalogJSONRenderer, RetiredPublicCatalogView
from .view_helpers import build_public_stats

BASE = "/api/v1/public/homepage/"


def with_query(url, **extra):
    parsed = urlsplit(url)
    query = dict(parse_qsl(parsed.query))
    query.update(extra)
    return parsed.path + "?" + urlencode(query)


class PublicCatalogBoundaryTests(SimpleTestCase):
    def test_actual_renderer_ignores_indent_and_guards_success_and_error_bytes(self):
        renderer = PublicCatalogJSONRenderer()
        response = Response({"value": "汉字"})
        context = {"response": response}
        encoded = renderer.render(response.data, "application/json; indent=100", context)
        self.assertEqual(encoded, '{"value":"汉字"}'.encode())
        response = Response({"value": "🍥" * SUCCESS_BYTES})
        encoded = renderer.render(response.data, renderer_context={"response": response})
        self.assertEqual(response.status_code, 500)
        self.assertLessEqual(len(encoded), 16384)
        self.assertEqual(set(json.loads(encoded)), {"code", "detail", "correlation_id"})
        response = Response({"detail": "secret" * 100000}, status=400)
        encoded = renderer.render(response.data, renderer_context={"response": response})
        self.assertLessEqual(len(encoded), 16384)
        self.assertNotIn(b"secret", encoded)

    def test_legacy_retirement_is_fixed_and_links_to_successor_docs(self):
        response = RetiredPublicCatalogView.as_view()(APIRequestFactory().get("/api/homepage/?limit=999999"))
        response.render()
        self.assertEqual(response.status_code, 410)
        self.assertEqual(response["Link"], '</api/v1/docs/>; rel="successor-version"')
        self.assertEqual(response.data["code"], "public_catalog_retired")
        self.assertEqual(set(response.data), {"code", "detail", "correlation_id"})
        self.assertLessEqual(len(response.content), 16384)

    def test_non_postgresql_backend_fails_explicitly(self):
        with patch.object(connection, "vendor", "sqlite"), self.assertRaises(CatalogError) as raised:
            require_database()
        self.assertEqual((raised.exception.code, raised.exception.status), ("public_catalog_unavailable", 503))

    def test_all_new_route_aliases_fail_closed_without_postgresql(self):
        cache.clear()
        self.addCleanup(cache.clear)
        scopes = ("public/homepage/", "public/showcase/12345678-1234-4234-8234-123456789012/")
        routes = [scope + leaf for scope in scopes for leaf in ("entries/", "summary/", "facets/", "entries/1/", "entries/1/fields/review/")]
        routes.append("public/showcases/")
        with patch.object(connection, "vendor", "sqlite"):
            for prefix in ("/api/", "/api/v1/"):
                for suffix in routes:
                    with self.subTest(path=prefix + suffix):
                        response = self.client.get(prefix + suffix)
                        self.assertEqual(response.status_code, 503)
                        self.assertEqual(response.json()["code"], "public_catalog_unavailable")
                        self.assertEqual(set(response.json()), {"code", "detail", "correlation_id"})
                        self.assertLessEqual(len(response.content), 16384)

    def test_search_trimming_retains_javascript_whitespace_rules(self):
        self.assertEqual(PublicQuery.parse({"search": "\ufeff Test \ufeff"}).search, "Test")
        self.assertEqual(PublicQuery.parse({"search": "\u0085Test\u0085"}).search, "\u0085Test\u0085")


class CatalogDatabaseTestCase(APITestCase):
    @classmethod
    def setUpTestData(cls):
        if connection.vendor != "postgresql":
            raise SkipTest("Public catalogue SQL and collation require PostgreSQL")
        require_database()
        owner_model = get_user_model()
        cls.owner = owner_model.objects.create_user("catalog-owner", is_staff=True)
        cls.other = owner_model.objects.create_user("catalog-other", is_staff=True)
        cls.member = owner_model.objects.create_user("catalog-member")
        state = InstallationState.load()
        state.status = InstallationState.Status.INITIALIZED
        state.save()
        cls.publication, _ = UserSettings.objects.get_or_create(user=cls.owner)
        cls.publication.allow_sharing = True
        cls.publication.public_status = UserSettings.PublicStatus.APPROVED
        cls.publication.save()
        site = SiteSettings.load()
        site.homepage_owner = cls.owner
        site.save()
        cls.showcase = f"/api/v1/public/showcase/{cls.publication.public_slug}/"

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)

    def entry(self, **values):
        return JournalEntry.objects.create(user=self.owner, title="测试番剧", visibility="public", **values)

    def test_all_legacy_route_aliases_are_retired_without_business_reads(self):
        self.entry(review="private payload must not be read")
        for prefix in ("/api/", "/api/v1/"):
            for suffix in ("homepage/", f"showcase/{self.publication.public_slug}/", "showcases/"):
                with self.subTest(path=prefix + suffix), CaptureQueriesContext(connection) as queries:
                    response = self.client.get(prefix + suffix, {"limit": "999999", "compat": "1"}, HTTP_AUTHORIZATION="Bearer invalid")
                self.assert_error(response, 410, "public_catalog_retired")
                self.assertEqual(response["Link"], '</api/v1/docs/>; rel="successor-version"')
                self.assertFalse(any("journal_journalentry" in item["sql"].lower() for item in queries))

    def get_ok(self, path, data=None, **headers):
        response = self.client.get(path, data=data or {}, **headers)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertLessEqual(len(response.content), SUCCESS_BYTES)
        self.assertEqual(response.data["consistency"], "live")
        self.assertEqual(response.data["schema"], "animemo.public-catalog/v1")
        return response

    def assert_error(self, response, status, code=None):
        self.assertEqual(response.status_code, status, response.data)
        self.assertEqual(set(response.data), {"code", "detail", "correlation_id"})
        self.assertLessEqual(len(response.content), 16384)
        if code:
            self.assertEqual(response.data["code"], code)

    def all_pages(self, path, params=None, key="results"):
        params = dict(params or {})
        values, cursors = [], set()
        while True:
            payload = self.get_ok(path, params).data
            values.extend(payload[key])
            cursor = payload["next_cursor"]
            if cursor is None:
                return values
            self.assertLessEqual(len(cursor.encode()), 4096)
            self.assertNotIn(cursor, cursors)
            cursors.add(cursor)
            self.assertLess(len(cursors), 1000)
            params["cursor"] = cursor

    def read_full(self, url):
        fragments = []
        offset = 0
        while True:
            payload = self.get_ok(with_query(url, part_size=16384)).data
            self.assertEqual(payload["offset"], offset)
            self.assertLessEqual(len(payload["fragment"].encode()), 65536)
            fragments.append(payload["fragment"])
            offset += len(payload["fragment"])
            if payload["complete"]:
                self.assertEqual(offset, payload["total_length"])
                return json.loads("".join(fragments))
            self.assertIsNotNone(payload["next_cursor"])
            url = with_query(url, cursor=payload["next_cursor"])


class PublicCatalogPaginationTests(CatalogDatabaseTestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        periods = ["2026-1", "2025-12", "2026-99", "2026-Q1", "待定", "", "2024"]
        scores = [None, Decimal(0), Decimal("9.5"), Decimal("8.75"), Decimal(6)]
        cls.records = JournalEntry.objects.bulk_create([
            JournalEntry(user=cls.owner, title=f"title-{index % 13:02}", japanese_title="日本語" if index % 3 else "OVA",
                         visibility="public", airing_period=periods[index % len(periods)], personal_score=scores[index % len(scores)],
                         watch_status="completed" if index % 2 else "planned", tags=["末页独有" if index == 500 else "日常"])
            for index in range(501)
        ])
        cls.when = datetime(2026, 1, 1, tzinfo=dt_timezone.utc)
        JournalEntry.objects.filter(user=cls.owner).update(updated_at=cls.when)

    def test_501_rows_all_sorts_have_no_page_duplicates_or_omissions(self):
        def date_key(row):
            parts = row.airing_period.split("-")
            return int(parts[0]) * 100 + int(parts[1]) if len(parts) == 2 and len(parts[0]) == 4 and parts[0].isdigit() and parts[1].isdigit() and 1 <= len(parts[1]) <= 2 else 0
        for sort in ("date-desc", "date-asc", "score-desc", "score-asc", "id-asc"):
            with self.subTest(sort=sort):
                actual = self.all_pages(BASE + "entries/", {"sort": sort, "page_size": 100})
                if sort == "id-asc":
                    expected = sorted(self.records, key=lambda row: row.pk)
                elif sort.startswith("date"):
                    expected = sorted(self.records, key=lambda row: (date_key(row) * (-1 if sort == "date-desc" else 1), row.title, -row.pk))
                else:
                    expected = sorted(self.records, key=lambda row: (not bool(row.personal_score), (row.personal_score or 0) * (-1 if sort == "score-desc" else 1), -row.pk))
                self.assertEqual([row["id"] for row in actual], [row.pk for row in expected])
        response = self.get_ok(BASE + "entries/")
        self.assertEqual(response.data["page_count"], 50)
        self.assertEqual(response.data["total"], 501)

    def test_late_filters_and_facets_use_all_authorized_rows(self):
        page = self.get_ok(BASE + "entries/", {"tag": "末页独有"}).data
        self.assertEqual(page["total"], 501)
        self.assertEqual(page["matched_count"], 1)
        self.assertEqual(page["results"][0]["id"], self.records[-1].pk)
        summary = self.get_ok(BASE + "summary/", {"tag": "末页独有"}).data
        self.assertEqual((summary["total"], summary["matched_count"], summary["stats"]["total"]), (501, 1, 501))
        expected_stats = build_public_stats(list(JournalEntry.objects.filter(user=self.owner).values("title", "airing_period", "tags", "personal_score", "watch_status")))
        self.assertEqual(summary["stats"], expected_stats)
        self.assertEqual(summary["unscored_count"], sum(not bool(row.personal_score) for row in self.records))
        facets = self.all_pages(BASE + "facets/", {"kind": "tags", "page_size": 1}, key="values")
        self.assertEqual({row["value"] for row in facets}, {"日常", "末页独有"})

    def test_export_can_recheck_last_entry_after_page_token_expires(self):
        clock = time.time()
        with patch("django.core.signing.time.time", return_value=clock):
            page = self.get_ok(BASE + "entries/", {"sort": "id-asc", "page_size": 50}).data
        last = page["results"][-1]
        with patch("django.core.signing.time.time", return_value=clock + 902):
            self.assert_error(self.client.get(BASE + "entries/", {"sort": "id-asc", "cursor": page["next_cursor"]}), 410, "public_catalog_cursor_expired")
            checkpoint = self.get_ok(BASE + f'entries/{last["id"]}/', {"revision": last["revision"]}).data
            fresh = checkpoint["next_export_cursor"]
            following = self.get_ok(BASE + "entries/", {"sort": "id-asc", "cursor": fresh}).data
            self.assertEqual([entry["id"] for entry in following["results"]], [entry.pk for entry in self.records[50:100]])
            self.assert_error(self.client.get(BASE + "entries/", {"sort": "score-desc", "cursor": fresh}), 400, "public_catalog_cursor_mismatch")
            self.assert_error(self.client.get(self.showcase + "entries/", {"sort": "id-asc", "cursor": fresh}), 400, "public_catalog_cursor_mismatch")
            JournalEntry.objects.filter(pk=last["id"]).update(review="changed")
            self.assert_error(self.client.get(BASE + f'entries/{last["id"]}/', {"revision": last["revision"]}), 409, "public_catalog_revision_changed")
            JournalEntry.objects.filter(pk=last["id"]).update(visibility="private")
            self.assert_error(self.client.get(BASE + f'entries/{last["id"]}/', {"revision": last["revision"]}), 404, "not_found")

    def test_page_and_cursor_limits_scope_sort_filter_and_expiry(self):
        for size in ("0", "101", "-1", "1.0", "50000000000"):
            self.assert_error(self.client.get(BASE + "entries/", {"page_size": size}), 400)
        first = self.get_ok(BASE + "entries/", {"page_size": 1}).data
        cursor = first["next_cursor"]
        for path, extras in ((BASE + "entries/", {"sort": "score-desc"}), (BASE + "entries/", {"tag": "日常"}), (self.showcase + "entries/", {})):
            self.assert_error(self.client.get(path, {"cursor": cursor, **extras}), 400, "public_catalog_cursor_mismatch")
        for token in (cursor[:-1] + ("a" if cursor[-1] != "a" else "b"), "x" * 4097):
            self.assert_error(self.client.get(BASE + "entries/", {"cursor": token}), 400, "public_catalog_cursor_invalid")
        with patch("django.core.signing.time.time", return_value=time.time() + 902):
            self.assert_error(self.client.get(BASE + "entries/", {"cursor": cursor}), 410, "public_catalog_cursor_expired")

    def test_queries_project_only_bounded_rows_and_do_not_grow_per_entry(self):
        captured = []
        original = sql_query.rows
        def inspected(sql, params=()):
            result = original(sql, params)
            captured.append((sql, result))
            return result
        with patch("journal.public_catalog.readers.rows", side_effect=inspected), CaptureQueriesContext(connection) as queries:
            self.get_ok(BASE + "entries/", {"page_size": 100})
        self.assertLessEqual(len(queries), 12)
        for sql, result in captured:
            self.assertNotIn("externalmediaidentity", sql.lower())
            self.assertNotIn("metadata", sql.lower())
            self.assertLessEqual(len(result), 101)
            for row in result:
                if "description" in row:
                    self.assertLessEqual(len(row["description"]), 512)
                    self.assertLessEqual(len(row["review"]), 512)
        with CaptureQueriesContext(connection) as single:
            self.get_ok(BASE + "entries/", {"page_size": 1})
        self.assertEqual(len(queries), len(single))
        captured.clear()
        with patch("journal.public_catalog.readers.rows", side_effect=inspected):
            self.get_ok(BASE + "summary/")
        score_batches = [result for _sql, result in captured if result and set(result[0]) == {"user_id", "id", "updated_at", "personal_score"}]
        self.assertEqual(sum(len(batch) for batch in score_batches), 400)
        self.assertEqual(max(len(batch) for batch in score_batches), 256)
        self.assertTrue(all(len(batch) <= 256 for batch in score_batches))


class PublicCatalogAuthorityTests(CatalogDatabaseTestCase):
    def test_field_routes_reject_unlisted_identifiers_before_business_reads(self):
        entry = self.entry(review="retained public review")
        fields = (
            "poster_file", "Review", "description ", "review::text",
            "review) FROM journal_journalentry; SELECT pg_sleep(1); --",
            "tags->>'private'", 'review" OR TRUE --',
        )
        for base in (BASE, BASE.replace("/v1/", "/"), self.showcase, self.showcase.replace("/v1/", "/")):
            for field in fields:
                path = f"{base}entries/{entry.pk}/fields/{quote(field, safe='')}/"
                with self.subTest(path=path), CaptureQueriesContext(connection) as queries:
                    response = self.client.get(path, {"revision": "unused-for-rejected-field"})
                self.assert_error(response, 404, "not_found")
                self.assertFalse(any("journal_journalentry" in item["sql"].lower() for item in queries))
        entry.refresh_from_db()
        self.assertEqual(entry.review, "retained public review")

    def test_owner_preview_whitelist_homepage_authority_and_aliases(self):
        entries = [JournalEntry.objects.create(user=self.owner, title=visibility, visibility=visibility) for visibility in JournalEntry.Visibility.values]
        deleted = self.entry(deleted_at=timezone.now())
        fallback = JournalEntry.objects.create(user=self.other, title="fallback-secret", visibility="public")
        history = WatchHistoryRecord.objects.create(entry=entries[-1], watched_on=date(2026, 1, 1), watched_label="private-date", notes=["private-history"], metadata={"private-provider": "secret"}, sequence=1)
        del history, deleted, fallback
        for actor in (None, self.member, self.other, self.owner):
            self.client.force_authenticate(actor)
            data = self.get_ok(BASE + "entries/").data
            self.assertEqual([row["visibility"] for row in data["results"]], ["public"])
            public = self.get_ok(self.showcase + "entries/")
            self.assertEqual(public.data["scope"]["visibility"], "owner" if actor == self.owner else "public")
            self.assertEqual(public.data["total"], 3 if actor == self.owner else 1)
            for row in public.data["results"]:
                self.assertEqual(set(row), set(PUBLIC_FIELDS) | {"fields", "revision", "detail_url", "preset_colors"})
            self.assertNotIn("private-history", public.content.decode())
            self.assertNotIn("private-provider", public.content.decode())
            self.assertIn("Authorization", public["Vary"])
            self.assertIn("Cookie", public["Vary"])
            if actor:
                self.assertEqual(public["Cache-Control"], "private, no-store")
        self.client.force_authenticate(None)
        self.assertEqual(self.get_ok(BASE.replace("/v1/", "/") + "entries/").data["total"], 1)
        SiteSettings.objects.filter(pk=1).update(homepage_owner=None)
        self.assertEqual(self.get_ok(BASE + "entries/").data["results"], [])

    def test_revoke_delete_and_private_changes_rechecked_for_detail_and_fields(self):
        entry = self.entry(description="long" * 1000)
        record = self.get_ok(self.showcase + "entries/").data["results"][0]
        field_url = record["fields"]["description"]["field_url"]
        UserSettings.objects.filter(pk=self.publication.pk).update(allow_sharing=False)
        for url in (record["detail_url"], field_url):
            self.assert_error(self.client.get(url), 404)
        self.client.force_authenticate(self.owner)
        self.get_ok(record["detail_url"])
        self.get_ok(field_url)
        JournalEntry.objects.filter(pk=entry.pk).update(deleted_at=timezone.now())
        self.assert_error(self.client.get(record["detail_url"]), 404)

    def test_directory_is_paginated_and_top_picks_use_same_whitelist(self):
        self.entry(personal_score=Decimal("9.5"), tags=["preset-tag"])
        for index in range(6):
            self.entry(personal_score=Decimal("8.0"), tags=["preset-tag"])
        TagDefinition.objects.create(name="preset-tag", color="pink", is_quick_preset=True)
        UserSettings.objects.filter(pk=self.publication.pk).update(nickname="another nickname")
        response = self.get_ok("/api/v1/public/showcases/", {"search": self.owner.username, "page_size": 1})
        self.assertEqual(response.data["total"], 1)
        self.assertEqual(response.data["matched_count"], 1)
        row = response.data["results"][0]
        self.assertEqual(row["stats"]["total"], 7)
        self.assertEqual(len(row["top_picks"]), 3)
        for entry in row["top_picks"]:
            self.assertEqual(set(entry), set(PUBLIC_FIELDS) | {"fields", "revision", "detail_url", "preset_colors"})
            self.assertEqual(entry["preset_colors"], {"preset-tag": "pink"})
        UserSettings.objects.filter(pk=self.publication.pk).update(allow_sharing=False)
        self.assertEqual(self.get_ok("/api/v1/public/showcases/").data["total"], 0)


class PublicCatalogLargeValueTests(CatalogDatabaseTestCase):
    def test_quick_ova_boundaries_use_javascript_whitespace(self):
        entries = JournalEntry.objects.bulk_create([
            JournalEntry(user=self.owner, title="x\ufeffOVA", visibility="public"),
            JournalEntry(user=self.owner, title="x\u0085OVA", visibility="public"),
        ])
        actual = self.get_ok(BASE + "entries/", {"quick": "special"}).data["results"]
        self.assertEqual([row["id"] for row in actual], [entries[0].pk])

    def test_average_preserves_legacy_binary_float_rounding(self):
        for scores, expected in ((("0.01", "0.06"), 0.03), (("0.01", "0.14"), 0.08), (("0.01", "0.20"), 0.11)):
            with self.subTest(scores=scores):
                JournalEntry.objects.filter(user=self.owner).delete()
                for score in scores:
                    self.entry(personal_score=Decimal(score))
                self.assertEqual(self.get_ok(BASE + "summary/").data["stats"]["average_score"], expected)
                self.assertEqual(self.get_ok("/api/v1/public/showcases/").data["results"][0]["stats"]["average_score"], expected)

    def test_complete_original_long_fields_and_facets_can_be_reconstructed(self):
        giant_tag = "大标签🍥\\\"\n" * 5000
        source = {"description": "说明🍥\x01\n\\\"" * 10000, "review": "长评🍥\x02\n\\\"" * 30000,
                  "tags": ["", "preset-tag", giant_tag, *[f"tag-{index}" for index in range(30)]],
                  "tag_colors": [[giant_tag, "raw-original-color"], ["preset-tag", {"legacy": [1, True, None]}]]}
        entry = self.entry(**source)
        TagDefinition.objects.create(name="preset-tag", color="pink", is_quick_preset=True)
        record = self.get_ok(BASE + "entries/", HTTP_ACCEPT="application/json; indent=100").data["results"][0]
        self.assertEqual(record["preset_colors"], {"preset-tag": "pink"})
        self.assertTrue(all(not record["fields"][name]["complete"] for name in FIELDS))
        self.assertEqual(len(record["tags"]), 8)
        for name in FIELDS:
            self.assertEqual(self.read_full(record["fields"][name]["field_url"]), source[name])
        facets = self.all_pages(BASE + "facets/", {"kind": "tags", "page_size": 7}, key="values")
        self.assertEqual(len(facets), len(source["tags"]))
        giant = next(row for row in facets if not row["complete"])
        self.assertIsNone(giant["value"])
        self.assertEqual(self.read_full(giant["field_url"]), giant_tag)
        filtered = self.get_ok(BASE + "entries/", {"tag_ref": giant["selection_token"]}).data
        self.assertEqual([row["id"] for row in filtered["results"]], [entry.pk])
        entry.refresh_from_db()
        for name in FIELDS:
            self.assertEqual(getattr(entry, name), source[name])

    def test_revision_change_rejects_old_detail_field_and_facet_references(self):
        entry = self.entry(description="old description", tags=["old tag"])
        record = self.get_ok(BASE + "entries/").data["results"][0]
        facet = self.get_ok(BASE + "facets/").data["values"][0]
        JournalEntry.objects.filter(pk=entry.pk).update(description="new description", tags=["new tag"])
        for url in (with_query(record["detail_url"], revision=record["revision"]), record["fields"]["description"]["field_url"], facet["field_url"], with_query(BASE + "entries/", tag_ref=facet["selection_token"])):
            self.assert_error(self.client.get(url), 409, "public_catalog_revision_changed")
        fresh = self.get_ok(record["detail_url"]).data["entry"]
        self.assertNotEqual(fresh["revision"], record["revision"])

    def test_byte_fitting_cursor_continues_from_last_emitted_entry(self):
        JournalEntry.objects.bulk_create([JournalEntry(user=self.owner, title=("\x01" * 190) + str(index), visibility="public",
                                                       description="\x02" * 512, review="\x03" * 512,
                                                       tags=[chr(4 + number) * 160 for number in range(8)]) for index in range(100)])
        first = self.get_ok(BASE + "entries/", {"sort": "id-asc", "page_size": 100}, HTTP_ACCEPT="application/json; indent=100")
        self.assertGreater(first.data["page_count"], 0)
        self.assertLess(first.data["page_count"], 100)
        actual = self.all_pages(BASE + "entries/", {"sort": "id-asc", "page_size": 100})
        expected = list(JournalEntry.objects.filter(user=self.owner).order_by("id").values_list("id", flat=True))
        self.assertEqual([row["id"] for row in actual], expected)

    def test_summary_ova_rule_and_quick_filter_keep_their_distinct_legacy_meanings(self):
        rows = [
            JournalEntry(user=self.owner, title="NOVATION", japanese_title="", tags=[], personal_score=0, watch_status="completed", airing_period="2026-Q1", visibility="public"),
            JournalEntry(user=self.owner, title="普通", japanese_title="《OVA》", tags=[], personal_score=None, watch_status="watching", airing_period="待定", visibility="public"),
            JournalEntry(user=self.owner, title="普通2", tags=["ova"], personal_score=Decimal("9.5"), watch_status="watching", airing_period="未定档", visibility="public"),
            JournalEntry(user=self.owner, title="普通3", tags=["OVA", "剧场版", "泡面番"], personal_score=Decimal(8), watch_status="planned", airing_period="2025", visibility="public"),
        ]
        JournalEntry.objects.bulk_create(rows)
        expected = build_public_stats([{key: getattr(row, key) for key in ("title", "airing_period", "tags", "personal_score", "watch_status")} for row in rows])
        self.assertEqual(self.get_ok(BASE + "summary/").data["stats"], expected)
        self.assertEqual(expected["ova_count"], 2)
        quick = self.get_ok(BASE + "entries/", {"quick": "special"}).data
        self.assertEqual({row["id"] for row in quick["results"]}, {row.pk for row in rows[1:]})
        years = self.all_pages(BASE + "facets/", {"kind": "years", "page_size": 1}, key="values")
        self.assertEqual([row["value"] for row in years][:2], ["2026", "2025"])
        self.assertEqual({row["value"] for row in years}, {"2026", "2025", "待定", "未定档"})


class PublicCatalogCollationTests(CatalogDatabaseTestCase):
    def test_zh_cn_golden_including_equivalent_unicode_keys_survives_keyset_pages(self):
        # Pinned independent Intl.Collator('zh-CN') oracle, Node 24.12/ICU 77.1.
        # Original catalogue ties retain -updated_at/-id before stable JS sort.
        titles = ["阿", "啊", "八", "把", "重生", "重庆", "中", "中国", "中文", "中文2", "中文10", "《OVA》", "OVA", "ova", "NOVATION", "劇場版", "剧场版", "あ", "ア", "あい", "アイ", "か", "が", "カ", "ガ", "か\u3099", "ｶﾞ", "A", "a", "Ａ", "1", "01", "10", "2", "é", "e\u0301", "e", "E", "Å", "A\u030a", "Z", "z", "a\u200b", "a\u00ad", "a b", "a-b", "a_b", "a.b", "", " ", "🌙", "😀", "龙", "龍", "吕", "呂", "女", "绿"]
        expected = [48, 49, 11, 50, 51, 31, 30, 32, 33, 0, 1, 2, 3, 5, 16, 15, 52, 53, 54, 55, 57, 56, 6, 7, 8, 10, 9, 4, 43, 42, 28, 27, 29, 39, 38, 44, 46, 45, 47, 35, 34, 36, 37, 14, 13, 12, 41, 40, 17, 18, 19, 20, 21, 23, 25, 22, 24, 26]
        entries = JournalEntry.objects.bulk_create([JournalEntry(user=self.owner, title=title, airing_period="2026-1", visibility="public") for title in titles])
        JournalEntry.objects.filter(user=self.owner).update(updated_at=datetime(2026, 1, 1, tzinfo=dt_timezone.utc))
        for sort in ("date-desc", "date-asc"):
            actual = self.all_pages(BASE + "entries/", {"sort": sort, "page_size": 7})
            self.assertEqual([row["id"] for row in actual], [entries[index].pk for index in expected])
