from base64 import b64decode
import io
import json
import time
import zipfile
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient, APITestCase
import requests

from accounts.models import StaffProfile, UserSecurityProfile
from site_config.models import SiteSettings, TagDefinition
from .models import AdminAuditLog, Column, JournalEntry, UserSettings
from plugin_host.models import PluginData, PluginProject
from .security import _totp_at


User = get_user_model()


class JournalApiTests(APITestCase):
    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(username="collector", email="collector@example.com", password="StrongPass123!")
        self.other = User.objects.create_user(username="other", email="other@example.com", password="StrongPass123!")
        self.plugin = PluginProject.objects.create(
            plugin_id="com.anime-journal.watch-history-importer",
            slug="watch-history-importer",
            name="忆往昔观看记录导入器",
            description="test fixture",
        )
        self.client.force_authenticate(self.user)

    def test_entry_crud_is_scoped_to_owner(self):
        response = self.client.post(reverse("entry-list"), {
            "title": "葬送的芙莉莲",
            "watch_status": "completed",
            "personal_score": "9.5",
            "tags": ["奇幻", "公路片"],
        }, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        JournalEntry.objects.create(user=self.other, title="不应看到的记录")
        result = self.client.get(reverse("entry-list"))
        self.assertEqual(result.data["count"], 1)

    def test_owner_can_add_manual_watch_history_through_entry_update(self):
        entry = JournalEntry.objects.create(user=self.user, title="手动记录番剧")

        response = self.client.patch(
            reverse("entry-detail", kwargs={"pk": entry.pk}),
            {
                "watch_history": [{
                    "watched_on": "2026-08-06",
                    "watched_label": "2026年8月6日",
                    "brush_number": 1,
                    "brush_label": "首刷",
                    "episode_start": 1,
                    "episode_end": 3,
                    "notes": ["后台手动补录"],
                }],
            },
            format="json",
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["watch_history"][0]["brush_label"], "首刷")
        history = PluginData.objects.get(plugin=self.plugin, namespace="watch_history", user=self.user, key=str(entry.pk))
        self.assertEqual(history.value[0]["brush_label"], "首刷")

    def test_manual_watch_history_accepts_multipart_entry_updates(self):
        entry = JournalEntry.objects.create(user=self.user, title="同时更换封面")
        poster = SimpleUploadedFile(
            "watch-history-poster.png",
            b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="),
            content_type="image/png",
        )

        response = self.client.patch(
            reverse("entry-detail", kwargs={"pk": entry.pk}),
            {
                "poster_file": poster,
                "watch_history": json.dumps([{
                    "watched_on": "2026-08-06",
                    "brush_label": "二刷",
                    "episode_start": 1,
                    "episode_end": 3,
                }]),
            },
            format="multipart",
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["watch_history"][0]["episode_end"], 3)
        history = PluginData.objects.get(plugin=self.plugin, namespace="watch_history", user=self.user, key=str(entry.pk))
        self.assertEqual(history.value[0]["brush_label"], "二刷")

    def test_editing_imported_watch_history_preserves_source_audit_fields(self):
        entry = JournalEntry.objects.create(user=self.user, title="保留导入来源")
        history = PluginData.objects.create(
            plugin=self.plugin, namespace="watch_history", user=self.user, key=str(entry.pk),
            value=[{"watched_on": "2026-08-06", "watched_label": "2026年8月6日", "brush_label": "首刷", "episode_start": 1, "episode_end": 3, "notes": [], "source_file": "2026.txt", "source_line": 18}],
        )

        response = self.client.patch(
            reverse("entry-detail", kwargs={"pk": entry.pk}),
            {"watch_history": [{
                "watched_on": "2026-08-06",
                "brush_label": "首刷",
                "episode_start": 1,
                "episode_end": 3,
                "notes": ["后台补充备注"],
            }]},
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        history.refresh_from_db()
        history.refresh_from_db()
        self.assertEqual(history.value[0]["source_file"], "2026.txt")
        self.assertEqual(history.value[0]["source_line"], 18)
        self.assertEqual(history.value[0]["notes"], ["后台补充备注"])

    def test_manual_watch_history_update_replaces_and_deduplicates_events(self):
        entry = JournalEntry.objects.create(user=self.user, title="可编辑观看记录")
        payload = {
            "watch_history": [
                {
                    "watched_on": "2026-08-06",
                    "brush_label": "首刷",
                    "episode_start": 1,
                    "episode_end": 3,
                },
                {
                    "watched_on": "2026-08-06",
                    "brush_label": "首刷",
                    "episode_start": 1,
                    "episode_end": 3,
                },
            ]
        }

        first = self.client.patch(reverse("entry-detail", kwargs={"pk": entry.pk}), payload, format="json")
        self.assertEqual(first.status_code, 200)
        self.assertEqual(PluginData.objects.filter(plugin=self.plugin, namespace="watch_history", user=self.user, key=str(entry.pk)).count(), 1)

        second = self.client.patch(reverse("entry-detail", kwargs={"pk": entry.pk}), {"watch_history": []}, format="json")
        self.assertEqual(second.status_code, 200)
        self.assertFalse(PluginData.objects.filter(plugin=self.plugin, namespace="watch_history", user=self.user, key=str(entry.pk)).exists())

    def test_manual_watch_history_requires_a_watch_date(self):
        entry = JournalEntry.objects.create(user=self.user, title="缺日期记录")

        response = self.client.patch(
            reverse("entry-detail", kwargs={"pk": entry.pk}),
            {"watch_history": [{"brush_label": "首刷"}]},
            format="json",
        )

        self.assertEqual(response.status_code, 400)

    def test_private_entry_is_not_shareable(self):
        entry = JournalEntry.objects.create(user=self.user, title="私人记录")
        self.client.force_authenticate(user=None)
        response = self.client.get(reverse("shared-entry", kwargs={"share_slug": entry.share_slug}))
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_public_entry_can_be_shared(self):
        entry = JournalEntry.objects.create(user=self.user, title="公开记录", visibility="public")
        settings_obj, _ = UserSettings.objects.get_or_create(user=self.user)
        settings_obj.public_status = UserSettings.PublicStatus.APPROVED
        settings_obj.allow_sharing = True
        settings_obj.save(update_fields=["public_status", "allow_sharing"])
        self.client.force_authenticate(user=None)
        response = self.client.get(reverse("shared-entry", kwargs={"share_slug": entry.share_slug}))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["title"], "公开记录")

    def test_user_can_login_with_email(self):
        self.client.force_authenticate(user=None)
        response = self.client.post(reverse("token_obtain_pair"), {
            "username": "collector@example.com",
            "password": "StrongPass123!",
        }, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("access", response.data)

    def test_regular_user_cannot_use_staff_login(self):
        self.client.force_authenticate(user=None)
        response = self.client.post(reverse("staff-login"), {
            "username": "collector",
            "password": "StrongPass123!",
        }, format="json")
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_staff_login_creates_session_and_tokens(self):
        staff = User.objects.create_user(username="staffer", email="staff@example.com", password="StrongPass123!", is_staff=True)
        self.client.force_authenticate(user=None)
        response = self.client.post(reverse("staff-login"), {
            "username": "staff@example.com",
            "password": "StrongPass123!",
        }, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(int(self.client.session["_auth_user_id"]), staff.id)
        self.assertIn("access", response.data)
        self.assertTrue(response.data["admin_url"].endswith("/admin/"))

    def test_authenticated_user_can_change_password(self):
        response = self.client.post(reverse("password-change"), {
            "current_password": "StrongPass123!",
            "password": "ChangedPass456!",
            "password_confirm": "ChangedPass456!",
        }, format="json")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("ChangedPass456!"))

    def test_account_deletion_requires_current_password(self):
        wrong = self.client.delete(reverse("account"), {
            "current_password": "not-the-password",
        }, format="json")
        self.assertEqual(wrong.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertTrue(User.objects.filter(pk=self.user.pk).exists())

        deleted = self.client.delete(reverse("account"), {
            "current_password": "StrongPass123!",
        }, format="json")
        self.assertEqual(deleted.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(User.objects.filter(pk=self.user.pk).exists())

    def test_public_showcase_stats_are_calculated_from_current_records(self):
        settings_obj, _ = UserSettings.objects.get_or_create(user=self.user)
        settings_obj.public_status = UserSettings.PublicStatus.APPROVED
        settings_obj.allow_sharing = True
        settings_obj.save(update_fields=["public_status", "allow_sharing"])
        JournalEntry.objects.create(
            user=self.user,
            title="高分剧场版",
            visibility="public",
            watch_status="completed",
            personal_score="9.8",
            tags=["剧场版"],
        )
        JournalEntry.objects.create(
            user=self.user,
            title="纪念 OVA",
            visibility="public",
            watch_status="completed",
            personal_score="9.6",
        )
        JournalEntry.objects.create(
            user=self.user,
            title="短篇动画",
            visibility="public",
            watch_status="watching",
            personal_score="8.0",
            tags=["泡面番"],
        )
        JournalEntry.objects.create(
            user=self.user,
            title="尚未开播",
            visibility="public",
            watch_status="planned",
            airing_period="未定档",
        )
        JournalEntry.objects.create(user=self.user, title="私人记录", visibility="private", personal_score="10")

        self.client.force_authenticate(user=None)
        response = self.client.get(
            reverse("showcase", kwargs={"public_slug": settings_obj.public_slug}),
            {"tag": "泡面番"},
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["results"]), 1)
        self.assertEqual(response.data["stats"], {
            "total": 4,
            "completed_count": 2,
            "average_score": 9.13,
            "movie_count": 1,
            "ova_count": 1,
            "short_count": 1,
            "masterpiece_count": 2,
            "pending_count": 1,
        })

    def test_public_showcase_list_uses_live_public_records(self):
        settings_obj, _ = UserSettings.objects.get_or_create(user=self.user)
        settings_obj.nickname = "收藏家"
        settings_obj.public_status = UserSettings.PublicStatus.APPROVED
        settings_obj.allow_sharing = True
        settings_obj.save(update_fields=["nickname", "public_status", "allow_sharing"])
        JournalEntry.objects.create(
            user=self.user,
            title="公开高分剧场版",
            visibility="public",
            watch_status="completed",
            personal_score="9.8",
            tags=["剧场版"],
        )
        JournalEntry.objects.create(
            user=self.user,
            title="公开泡面番",
            visibility="public",
            watch_status="watching",
            personal_score="8.2",
            tags=["泡面番"],
        )
        JournalEntry.objects.create(user=self.user, title="私人满分记录", visibility="private", personal_score="10")

        self.client.force_authenticate(user=None)
        response = self.client.get(reverse("showcase-list"))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 1)
        owner = response.data["results"][0]
        self.assertEqual(owner["nickname"], "收藏家")
        self.assertEqual(owner["stats"]["total"], 2)
        self.assertEqual(owner["stats"]["average_score"], 9.0)
        self.assertEqual(owner["stats"]["movie_count"], 1)
        self.assertEqual(owner["stats"]["short_count"], 1)
        self.assertEqual([entry["title"] for entry in owner["top_picks"]], ["公开高分剧场版", "公开泡面番"])

    def test_owner_preview_includes_private_records_before_public_approval(self):
        settings_obj, _ = UserSettings.objects.get_or_create(user=self.user)
        JournalEntry.objects.create(user=self.user, title="公开记录", visibility="public")
        JournalEntry.objects.create(user=self.user, title="仅自己可见", visibility="private")

        response = self.client.get(reverse("showcase", kwargs={"public_slug": settings_obj.public_slug}))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual({item["title"] for item in response.data["results"]}, {"公开记录", "仅自己可见"})
        self.assertEqual(response.data["stats"]["total"], 2)

    def test_public_journal_application_and_cancellation(self):
        response = self.client.post(reverse("public-journal-status"), {}, format="json")
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        self.assertEqual(response.data["public_status"], UserSettings.PublicStatus.PENDING)
        self.assertFalse(response.data["is_public"])

        duplicate = self.client.post(reverse("public-journal-status"), {}, format="json")
        self.assertEqual(duplicate.status_code, status.HTTP_409_CONFLICT)

        cancelled = self.client.patch(reverse("public-journal-status"), {}, format="json")
        self.assertEqual(cancelled.status_code, status.HTTP_200_OK)
        self.assertEqual(cancelled.data["public_status"], UserSettings.PublicStatus.PRIVATE)
        self.assertFalse(cancelled.data["is_public"])

    def test_staff_public_journal_is_approved_without_review(self):
        staff = User.objects.create_user(
            username="staff-owner",
            email="staff-owner@example.com",
            password="StrongPass123!",
            is_staff=True,
        )
        StaffProfile.objects.create(user=staff, role=StaffProfile.Role.REVIEWER)
        self.client.force_authenticate(staff)

        response = self.client.post(reverse("public-journal-status"), {}, format="json")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["public_status"], UserSettings.PublicStatus.APPROVED)
        self.assertTrue(response.data["is_public"])

    def test_staff_can_approve_public_journal_application(self):
        settings_obj = UserSettings.objects.create(
            user=self.user,
            nickname="收藏家",
            public_status=UserSettings.PublicStatus.PENDING,
        )
        staff = User.objects.create_user(
            username="staff-reviewer",
            email="reviewer@example.com",
            password="StrongPass123!",
            is_staff=True,
        )
        StaffProfile.objects.create(user=staff, role=StaffProfile.Role.REVIEWER)
        self.client.force_authenticate(staff)
        response = self.client.patch(
            reverse("staff-public-journal-review", kwargs={"pk": settings_obj.pk}),
            {"status": UserSettings.PublicStatus.APPROVED},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["public_status"], UserSettings.PublicStatus.APPROVED)
        self.assertTrue(response.data["is_public"])

    def test_public_catalog_supports_latest_page_and_hides_private_fields(self):
        owner = User.objects.create_user(username="primary", email="primary@example.com", password="StrongPass123!", is_staff=True)
        for index in range(12):
            JournalEntry.objects.create(
                user=owner,
                title=f"主目录番剧 {index:02d}",
                japanese_title=f"Catalog {index:02d}",
                airing_period="2026-1",
                personal_score="9.8",
                review="私人评价不应返回",
            )
        self.client.force_authenticate(self.user)
        response = self.client.get(reverse("public-catalog-search"), {"page": 2, "page_size": 10})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 12)
        self.assertEqual(response.data["page"], 2)
        self.assertEqual(response.data["pages"], 2)
        self.assertEqual(len(response.data["results"]), 2)
        self.assertNotIn("personal_score", response.data["results"][0])
        self.assertNotIn("review", response.data["results"][0])

    def test_public_catalog_query_matches_studio_and_period(self):
        owner = User.objects.create_user(username="primary2", email="primary2@example.com", password="StrongPass123!", is_staff=True)
        JournalEntry.objects.create(user=owner, title="春日番剧", studio="京都动画", airing_period="2022-10")
        JournalEntry.objects.create(user=owner, title="夏日番剧", studio="另一家公司", airing_period="2023-7")
        self.client.force_authenticate(self.user)
        response = self.client.get(reverse("public-catalog-search"), {"q": "京都动画"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual([item["title"] for item in response.data["results"]], ["春日番剧"])

    @patch("journal.bangumi_views.requests.get")
    @patch("journal.bangumi_views.requests.post")
    def test_bangumi_search_falls_back_when_v0_endpoint_is_temporarily_unavailable(self, post, get):
        post.side_effect = requests.ConnectionError("temporary upstream failure")
        legacy_response = Mock()
        legacy_response.raise_for_status.return_value = None
        legacy_response.json.return_value = {
            "list": [{
                "id": 1120,
                "name": "地獄少女 三鼎",
                "name_cn": "地狱少女 三鼎",
                "air_date": "2008-10-04",
                "eps": 26,
                "images": {"large": "http://lain.bgm.tv/pic/cover/l/test.jpg"},
                "rating": {"score": 7.1},
            }],
        }
        get.return_value = legacy_response
        self.client.force_authenticate(user=None)

        response = self.client.get(reverse("bangumi-search"), {"q": "地方1"})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["results"][0]["name"], "地狱少女 三鼎")
        self.assertEqual(response.data["results"][0]["poster"], "https://bgm-img-proxy.xhcytus100.workers.dev/pic/cover/l/test.jpg")
        self.assertEqual(response.data["results"][0]["thumbnail"], "https://bgm-img-proxy.xhcytus100.workers.dev/r/100/pic/cover/l/test.jpg")
        post.assert_called_once()
        get.assert_called_once()

    @patch("journal.bangumi_views.requests.post")
    def test_bangumi_search_returns_metadata_used_by_smart_fill(self, post):
        v0_response = Mock()
        v0_response.raise_for_status.return_value = None
        v0_response.json.return_value = {
            "data": [{
                "id": 1424,
                "name": "けいおん！",
                "name_cn": "轻音少女",
                "date": "2009-04-03",
                "total_episodes": 14,
                "summary": "樱丘高中轻音部的故事。",
                "images": {"large": "http://lain.bgm.tv/pic/cover/l/k-on.jpg"},
                "infobox": [{"key": "动画制作", "value": [{"v": "京都アニメーション"}]}],
                "tags": [
                    {"name": "京阿尼"}, {"name": "K-ON!"}, {"name": "校园"}, {"name": "轻音"},
                    {"name": "萌"}, {"name": "治愈"}, {"name": "日常"}, {"name": "2009年4月"},
                    {"name": "社团"},
                ],
                "rating": {"score": 8.2},
            }],
        }
        post.return_value = v0_response
        self.client.force_authenticate(user=None)

        response = self.client.get(reverse("bangumi-search"), {"q": "轻音少女metadata"})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        result = response.data["results"][0]
        self.assertEqual(result["studio"], "京都アニメーション")
        self.assertEqual(result["tags"], ["京阿尼", "K-ON!", "校园", "轻音", "萌", "治愈", "日常", "2009年4月"])
        self.assertEqual(result["poster"], "https://bgm-img-proxy.xhcytus100.workers.dev/pic/cover/l/k-on.jpg")
        self.assertEqual(result["thumbnail"], "https://bgm-img-proxy.xhcytus100.workers.dev/r/100/pic/cover/l/k-on.jpg")

    @patch("journal.bangumi_views.requests.post")
    def test_bangumi_search_prefers_animation_studio_over_production_committee(self, post):
        production_committee = "「無職転生」製作委員会（博報堂DYミュージック&ピクチャーズ、東宝、KADOKAWA、フロンティアワークス、日本BS放送、グリー、EGG FIRM）"
        v0_response = Mock()
        v0_response.raise_for_status.return_value = None
        v0_response.json.return_value = {
            "data": [{
                "id": 277554,
                "name": "無職転生 ～異世界行ったら本気だす～",
                "name_cn": "无职转生～到了异世界就拿出真本事～",
                "infobox": [
                    {"key": "动画制作", "value": "スタジオバインド"},
                    {"key": "製作", "value": production_committee},
                ],
            }],
        }
        post.return_value = v0_response
        self.client.force_authenticate(user=None)

        response = self.client.get(reverse("bangumi-search"), {"q": "无职转生studio-priority"})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["results"][0]["studio"], "スタジオバインド")

    @patch("journal.bangumi_views.requests.get")
    def test_bangumi_autofill_reads_animation_studio_from_persons(self, get):
        subject_response = Mock()
        subject_response.raise_for_status.return_value = None
        subject_response.json.return_value = {
            "id": 277554,
            "name": "無職転生 ～異世界行ったら本気だす～",
            "name_cn": "无职转生～到了异世界就拿出真本事～",
            "date": "2021-01-11",
            "total_episodes": 11,
            "infobox": [{"key": "製作", "value": "「無職転生」製作委員会"}],
        }
        persons_response = Mock()
        persons_response.raise_for_status.return_value = None
        persons_response.json.return_value = [
            {"name": "スタジオバインド", "relation": "动画制作", "type": 2},
            {"name": "EGG FIRM", "relation": "製作", "type": 2},
        ]
        get.side_effect = [subject_response, persons_response]
        self.client.force_authenticate(user=None)

        response = self.client.get(reverse("bangumi-autofill"), {"id": 277554})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["studio"], "スタジオバインド")
        self.assertEqual(response.data["eps"], 11)

    @patch("journal.bangumi_views.requests.get")
    def test_bangumi_autofill_combines_multiple_animation_studios(self, get):
        subject_response = Mock()
        subject_response.raise_for_status.return_value = None
        subject_response.json.return_value = {"id": 1, "name": "测试番剧"}
        persons_response = Mock()
        persons_response.raise_for_status.return_value = None
        persons_response.json.return_value = [
            {"name": "Studio A", "relation": "动画制作", "type": 2},
            {"name": "Studio B", "relation": "アニメーション制作", "type": 2},
            {"name": "Studio A", "relation": "動畫製作", "type": 2},
        ]
        get.side_effect = [subject_response, persons_response]
        self.client.force_authenticate(user=None)

        response = self.client.get(reverse("bangumi-autofill"), {"id": 1})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["studio"], "Studio A / Studio B")

    @patch("journal.bangumi_views.requests.get")
    def test_bangumi_autofill_falls_back_to_infobox_when_persons_fails(self, get):
        subject_response = Mock()
        subject_response.raise_for_status.return_value = None
        subject_response.json.return_value = {
            "id": 2,
            "name": "回退测试",
            "infobox": [
                {"key": "动画制作", "value": "Fallback Animation"},
                {"key": "製作", "value": "Fallback Committee"},
            ],
        }
        get.side_effect = [subject_response, requests.ConnectionError("persons unavailable")]
        self.client.force_authenticate(user=None)

        response = self.client.get(reverse("bangumi-autofill"), {"id": 2})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["studio"], "Fallback Animation")

    @patch("journal.bangumi_views.requests.get")
    def test_bangumi_autofill_never_prefers_committee_over_person_animation_relation(self, get):
        subject_response = Mock()
        subject_response.raise_for_status.return_value = None
        subject_response.json.return_value = {
            "id": 3,
            "name": "优先级测试",
            "infobox": [{"key": "製作", "value": "Very Long Production Committee"}],
        }
        persons_response = Mock()
        persons_response.raise_for_status.return_value = None
        persons_response.json.return_value = [
            {"name": "Correct Animation Studio", "relation": "动画制作", "type": 2},
        ]
        get.side_effect = [subject_response, persons_response]
        self.client.force_authenticate(user=None)

        response = self.client.get(reverse("bangumi-autofill"), {"id": 3})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["studio"], "Correct Animation Studio")

    def test_public_catalog_combines_all_staff_entries(self):
        first_staff = User.objects.create_user(username="staff-one", password="StrongPass123!", is_staff=True)
        second_staff = User.objects.create_user(username="staff-two", password="StrongPass123!", is_staff=True)
        JournalEntry.objects.create(user=first_staff, title="管理员番剧一")
        JournalEntry.objects.create(user=second_staff, title="管理员番剧二")
        JournalEntry.objects.create(user=self.other, title="普通用户私人番剧")

        response = self.client.get(reverse("public-catalog-search"), {"page_size": 10})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 2)
        self.assertEqual(
            {item["title"] for item in response.data["results"]},
            {"管理员番剧一", "管理员番剧二"},
        )

    def test_import_preview_and_commit_skip_existing_and_repeated_records(self):
        JournalEntry.objects.create(user=self.user, title="《已有番剧》", japanese_title="既有番剧")
        payload = {
            "version": 1,
            "records": [
                {"title": "《已有番剧》", "japaneseTitle": "既有番剧", "period": "2025-1", "status": "planned"},
                {"title": "《新番剧》", "japaneseTitle": "新番剧", "period": "2026-1", "status": "watching", "tags": ["日常"]},
                {"title": "《新番剧》", "japaneseTitle": "新番剧", "period": "2026-1", "status": "watching", "tags": ["日常"]},
            ],
        }
        preview_file = SimpleUploadedFile("anime-journal.json", json.dumps(payload).encode("utf-8"), content_type="application/json")

        preview = self.client.post(reverse("import"), {"file": preview_file, "preview": "true"}, format="multipart")

        self.assertEqual(preview.status_code, status.HTTP_200_OK)
        self.assertEqual(preview.data["total"], 3)
        self.assertEqual(preview.data["ready"], 1)
        self.assertEqual(preview.data["skipped_duplicates"], 2)
        self.assertEqual([item["status"] for item in preview.data["items"]], ["duplicate", "ready", "duplicate"])

        commit_file = SimpleUploadedFile("anime-journal.json", json.dumps(payload).encode("utf-8"), content_type="application/json")
        commit = self.client.post(reverse("import"), {"file": commit_file}, format="multipart")

        self.assertEqual(commit.status_code, status.HTTP_201_CREATED)
        self.assertEqual(commit.data["created"], 1)
        self.assertEqual(commit.data["skipped_duplicates"], 2)
        self.assertEqual(JournalEntry.objects.filter(user=self.user).count(), 2)

    def test_import_accepts_csv_templates(self):
        csv_file = SimpleUploadedFile(
            "anime-journal.csv",
            "title,japanese_title,airing_period,watch_status\n《CSV 番剧》,CSV Anime,2026-4,planned\n".encode("utf-8"),
            content_type="text/csv",
        )

        response = self.client.post(reverse("import"), {"file": csv_file}, format="multipart")

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["created"], 1)
        self.assertTrue(JournalEntry.objects.filter(user=self.user, title="《CSV 番剧》").exists())

    def test_public_homepage_uses_live_staff_entries_only(self):
        staff = User.objects.create_user(username="homepage-owner", password="StrongPass123!", is_staff=True)
        inactive_staff = User.objects.create_user(username="inactive-owner", password="StrongPass123!", is_staff=True, is_active=False)
        JournalEntry.objects.create(
            user=staff,
            title="首页真实番剧",
            personal_score="9.7",
            watch_status=JournalEntry.WatchStatus.COMPLETED,
            tags=["剧场版"],
        )
        JournalEntry.objects.create(user=inactive_staff, title="停用账号番剧")
        JournalEntry.objects.create(user=self.other, title="普通用户私人番剧")

        self.client.force_authenticate(user=None)
        response = self.client.get(reverse("homepage"))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual([item["title"] for item in response.data["results"]], ["首页真实番剧"])
        self.assertEqual(response.data["stats"]["total"], 1)
        self.assertEqual(response.data["stats"]["completed_count"], 1)
        self.assertEqual(response.data["stats"]["movie_count"], 1)

    def test_superuser_can_manage_user_permissions(self):
        admin = User.objects.create_superuser(
            username="root-admin",
            email="root-admin@example.com",
            password="StrongPass123!",
        )
        security, _ = UserSecurityProfile.objects.get_or_create(user=admin)
        security.set_totp_secret("JBSWY3DPEHPK3PXP")
        security.two_factor_enabled = True
        security.save(update_fields=["totp_secret_encrypted", "two_factor_enabled", "updated_at"])
        self.client.force_authenticate(admin)

        response = self.client.patch(
            reverse("staff-user-permissions", kwargs={"pk": self.user.pk}),
            {"is_active": False, "is_staff": True, "current_password": "StrongPass123!", "otp": _totp_at("JBSWY3DPEHPK3PXP", time.time())},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.user.refresh_from_db()
        self.assertFalse(self.user.is_active)
        self.assertTrue(self.user.is_staff)

    def test_staff_cannot_grant_administrator_permission(self):
        staff = User.objects.create_user(
            username="limited-staff",
            email="limited-staff@example.com",
            password="StrongPass123!",
            is_staff=True,
        )
        self.client.force_authenticate(staff)

        response = self.client.patch(
            reverse("staff-user-permissions", kwargs={"pk": self.user.pk}),
            {"is_staff": True},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.user.refresh_from_db()
        self.assertFalse(self.user.is_staff)

    def test_public_site_settings_returns_defaults(self):
        self.client.force_authenticate(user=None)

        response = self.client.get(reverse("site-settings"))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["site_name"], "Anime Journal")
        self.assertEqual(response.data["homepage_title"], "XuanHuang 的番剧汇总")
        self.assertTrue(response.data["registration_enabled"])

    def test_administrator_can_update_site_settings(self):
        admin = User.objects.create_user(
            username="site-admin",
            email="site-admin@example.com",
            password="StrongPass123!",
            is_staff=True,
        )
        StaffProfile.objects.create(user=admin, role=StaffProfile.Role.ADMINISTRATOR)
        self.client.force_authenticate(admin)

        response = self.client.patch(reverse("staff-site-settings"), {
            "site_name": "新番剧手账",
            "homepage_title": "测试站点的番剧汇总",
            "registration_enabled": False,
        }, format="json")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        settings_obj = SiteSettings.load()
        self.assertEqual(settings_obj.site_name, "新番剧手账")
        self.assertEqual(settings_obj.homepage_title, "测试站点的番剧汇总")
        self.assertFalse(settings_obj.registration_enabled)

    def test_regular_user_cannot_update_site_settings(self):
        response = self.client.patch(reverse("staff-site-settings"), {
            "site_name": "越权修改",
        }, format="json")

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(SiteSettings.load().site_name, "Anime Journal")

    def test_registration_disabled_prevents_account_creation(self):
        settings_obj = SiteSettings.load()
        settings_obj.registration_enabled = False
        settings_obj.save(update_fields=["registration_enabled", "updated_at"])
        self.client.force_authenticate(user=None)

        response = self.client.post(reverse("register-request"), {
            "email": "closed@example.com",
            "password": "StrongPass123!",
            "password_confirm": "StrongPass123!",
        }, format="json")

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(response.data["detail"], "当前暂未开放注册。")
        self.assertFalse(User.objects.filter(email="closed@example.com").exists())

    def test_site_avatar_upload_returns_public_url(self):
        admin = User.objects.create_user(
            username="avatar-admin",
            email="avatar-admin@example.com",
            password="StrongPass123!",
            is_staff=True,
        )
        StaffProfile.objects.create(user=admin, role=StaffProfile.Role.ADMINISTRATOR)
        self.client.force_authenticate(admin)
        avatar = SimpleUploadedFile(
            "site-avatar.png",
            b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="),
            content_type="image/png",
        )

        response = self.client.patch(reverse("staff-site-settings"), {
            "site_avatar": avatar,
        }, format="multipart")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("/media/site/avatar/", response.data["site_avatar_url"])


class StaffControlRoomTests(APITestCase):
    def setUp(self):
        cache.clear()
        self.admin = User.objects.create_superuser(
            username="root-admin",
            email="root@example.com",
            password="StrongPass123!",
        )
        self.member = User.objects.create_user(
            username="member",
            email="member@example.com",
            password="StrongPass123!",
        )
        UserSettings.objects.create(user=self.member, nickname="成员", public_status=UserSettings.PublicStatus.PENDING)
        self.client.force_authenticate(self.admin)

    def test_staff_resources_use_real_server_side_pagination_and_search(self):
        for index in range(27):
            JournalEntry.objects.create(user=self.member, title=f"分页番剧 {index:02d}")
        response = self.client.get(reverse("staff-resource-list", kwargs={"kind": "entries"}), {"page": 2, "page_size": 10, "q": "分页番剧"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 27)
        self.assertEqual(response.data["page"], 2)
        self.assertEqual(len(response.data["results"]), 10)

    def test_rejection_requires_reason_and_creates_audit_log(self):
        column = Column.objects.create(author=self.member, title="待审专栏", body="正文", status=Column.Status.PENDING)
        missing = self.client.patch(reverse("staff-column-review", kwargs={"pk": column.pk}), {"status": Column.Status.REJECTED}, format="json")
        self.assertEqual(missing.status_code, status.HTTP_400_BAD_REQUEST)

        response = self.client.patch(reverse("staff-column-review", kwargs={"pk": column.pk}), {"status": Column.Status.REJECTED, "reason": "资料来源不足"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        column.refresh_from_db()
        self.assertEqual(column.moderation_reason, "资料来源不足")
        self.assertTrue(AdminAuditLog.objects.filter(action="column.review", target_id=str(column.pk)).exists())

    def test_bulk_recycle_hides_content_and_restore_returns_it(self):
        entry = JournalEntry.objects.create(user=self.member, title="准备回收")
        recycled = self.client.post(reverse("staff-bulk-action", kwargs={"kind": "entries"}), {"ids": [entry.pk], "action": "recycle", "reason": "内容违规"}, format="json")
        self.assertEqual(recycled.status_code, status.HTTP_200_OK)
        entry.refresh_from_db()
        self.assertIsNotNone(entry.deleted_at)
        self.client.force_authenticate(self.member)
        self.assertEqual(self.client.get(reverse("entry-list")).data["count"], 0)

        self.client.force_authenticate(self.admin)
        restored = self.client.post(reverse("staff-bulk-action", kwargs={"kind": "entries"}), {"ids": [entry.pk], "action": "restore"}, format="json")
        self.assertEqual(restored.status_code, status.HTTP_200_OK)
        entry.refresh_from_db()
        self.assertIsNone(entry.deleted_at)

    def test_reviewer_role_cannot_open_user_management(self):
        reviewer = User.objects.create_user(username="reviewer", password="StrongPass123!", is_staff=True)
        StaffProfile.objects.create(user=reviewer, role=StaffProfile.Role.REVIEWER)
        self.client.force_authenticate(reviewer)
        denied = self.client.get(reverse("staff-resource-list", kwargs={"kind": "users"}))
        allowed = self.client.get(reverse("staff-resource-list", kwargs={"kind": "columns"}))
        self.assertEqual(denied.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(allowed.status_code, status.HTTP_200_OK)

    def test_public_tag_presets_only_include_enabled_definitions_in_order(self):
        TagDefinition.objects.all().delete()
        TagDefinition.objects.create(name="后显示", color=TagDefinition.Color.ROSE, is_quick_preset=True, sort_order=20)
        TagDefinition.objects.create(name="不公开", color=TagDefinition.Color.SLATE, is_quick_preset=False, sort_order=5)
        TagDefinition.objects.create(name="先显示", color=TagDefinition.Color.YELLOW, is_quick_preset=True, sort_order=10)

        self.client.force_authenticate(user=None)
        response = self.client.get(reverse("tag-presets"))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual([item["name"] for item in response.data["results"]], ["先显示", "后显示"])
        self.assertEqual(response.data["results"][0]["color"], TagDefinition.Color.YELLOW)

    def test_staff_can_manage_tag_definitions_and_quick_preset_state(self):
        created = self.client.post(reverse("staff-tag-list"), {
            "name": "科幻",
            "color": TagDefinition.Color.CYAN,
            "is_quick_preset": False,
            "sort_order": 25,
        }, format="json")
        self.assertEqual(created.status_code, status.HTTP_201_CREATED)
        self.assertFalse(created.data["is_quick_preset"])
        tag_id = created.data["id"]

        updated = self.client.patch(reverse("staff-tag-detail", kwargs={"pk": tag_id}), {
            "is_quick_preset": True,
            "color": TagDefinition.Color.PURPLE,
        }, format="json")
        self.assertEqual(updated.status_code, status.HTTP_200_OK)
        self.assertTrue(updated.data["is_quick_preset"])
        self.assertEqual(updated.data["color"], TagDefinition.Color.PURPLE)
        self.assertTrue(AdminAuditLog.objects.filter(action="tag.create", target_id=str(tag_id)).exists())
        self.assertTrue(AdminAuditLog.objects.filter(action="tag.update", target_id=str(tag_id)).exists())

    def test_duplicate_tag_names_are_rejected_case_insensitively(self):
        TagDefinition.objects.create(name="SciFi")
        response = self.client.post(reverse("staff-tag-list"), {
            "name": "scifi",
            "color": TagDefinition.Color.SLATE,
            "is_quick_preset": True,
            "sort_order": 1,
        }, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(TagDefinition.objects.filter(name__iexact="scifi").count(), 1)

    def test_reviewer_cannot_manage_tag_definitions(self):
        reviewer = User.objects.create_user(username="tag-reviewer", password="StrongPass123!", is_staff=True)
        StaffProfile.objects.create(user=reviewer, role=StaffProfile.Role.REVIEWER)
        self.client.force_authenticate(reviewer)
        response = self.client.get(reverse("staff-tag-list"))
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_deleting_tag_definition_preserves_existing_entry_tags(self):
        definition = TagDefinition.objects.create(name="保留在记录中", is_quick_preset=True)
        entry = JournalEntry.objects.create(user=self.member, title="标签保留测试", tags=[definition.name])

        response = self.client.delete(reverse("staff-tag-detail", kwargs={"pk": definition.pk}))

        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        entry.refresh_from_db()
        self.assertEqual(entry.tags, ["保留在记录中"])
        self.assertTrue(AdminAuditLog.objects.filter(action="tag.delete", target_id=str(definition.pk)).exists())

    def test_force_logout_revokes_existing_access_tokens(self):
        login_client = APIClient()
        login = login_client.post(reverse("token_obtain_pair"), {"username": "member", "password": "StrongPass123!"}, format="json")
        self.assertEqual(login.status_code, status.HTTP_200_OK)
        authenticated = APIClient()
        authenticated.credentials(HTTP_AUTHORIZATION=f"Bearer {login.data['access']}")
        self.assertEqual(authenticated.get(reverse("me")).status_code, status.HTTP_200_OK)

        response = self.client.post(reverse("staff-user-action", kwargs={"pk": self.member.pk, "action": "force-logout"}), {}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(authenticated.get(reverse("me")).status_code, status.HTTP_401_UNAUTHORIZED)

    def test_user_detail_includes_security_and_login_history(self):
        login_client = APIClient()
        login_client.post(reverse("token_obtain_pair"), {"username": "member", "password": "wrong"}, format="json")
        response = self.client.get(reverse("staff-user-detail", kwargs={"pk": self.member.pk}))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("email_verified", response.data)
        self.assertEqual(len(response.data["login_events"]), 1)

    def test_two_factor_setup_is_enforced_during_staff_login(self):
        begin = self.client.post(reverse("staff-two-factor"), {"action": "begin", "password": "StrongPass123!"}, format="json")
        self.assertEqual(begin.status_code, status.HTTP_200_OK)
        code = _totp_at(begin.data["secret"], time.time())
        confirmed = self.client.post(reverse("staff-two-factor"), {"action": "confirm", "code": code}, format="json")
        self.assertEqual(confirmed.status_code, status.HTTP_200_OK)
        self.assertTrue(UserSecurityProfile.objects.get(user=self.admin).two_factor_enabled)

        login_client = APIClient()
        required = login_client.post(reverse("staff-login"), {"username": "root-admin", "password": "StrongPass123!"}, format="json")
        self.assertEqual(required.status_code, status.HTTP_428_PRECONDITION_REQUIRED)
        accepted = login_client.post(reverse("staff-login"), {"username": "root-admin", "password": "StrongPass123!", "otp": _totp_at(begin.data["secret"], time.time())}, format="json")
        self.assertEqual(accepted.status_code, status.HTTP_200_OK)

    def test_backup_zip_contains_safe_structured_exports(self):
        response = self.client.get(reverse("staff-system-backup"), {"export_format": "zip", "kind": "all"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            names = set(archive.namelist())
            self.assertIn("manifest.json", names)
            self.assertIn("users.json", names)
            users = json.loads(archive.read("users.json"))
            self.assertNotIn("password", users[0])

    @patch("journal.staff_system_views.requests.get")
    def test_system_health_reports_each_dependency(self, get):
        upstream = Mock(status_code=200)
        upstream.raise_for_status.return_value = None
        get.return_value = upstream
        response = self.client.get(reverse("staff-system-health"))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual({item["key"] for item in response.data["services"]}, {"database", "email", "bangumi", "storage", "plugins"})
