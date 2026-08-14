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
from site_config.models import InstallationState, SiteSettings, TagDefinition
from .models import AdminAuditLog, Column, JournalEntry, UserSettings
from .security import _totp_at


User = get_user_model()


class JournalApiTests(APITestCase):
    def setUp(self):
        cache.clear()
        installation = InstallationState.load()
        installation.status = InstallationState.Status.INITIALIZED
        installation.save(update_fields=["status", "updated_at"])
        self.user = User.objects.create_user(username="collector", email="collector@example.com", password="StrongPass123!")
        self.other = User.objects.create_user(username="other", email="other@example.com", password="StrongPass123!")
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

    @patch("journal.external_media.views.get_provider")
    def test_external_media_search_returns_only_unified_dto(self, get_provider):
        get_provider.return_value.slug = "bangumi"
        get_provider.return_value.search.return_value = [{
            "provider": "bangumi",
            "external_id": "1424",
            "title": "轻音少女",
            "japanese_title": "けいおん！",
            "summary": "樱丘高中轻音部的故事。",
            "episodes": 14,
            "air_date": "2009-04-03",
            "studio": "京都アニメーション",
            "tags": ["校园", "日常"],
            "score": 8.2,
            "poster_url": "https://lain.bgm.tv/pic/cover/l/k-on.jpg",
            "thumbnail_url": "https://lain.bgm.tv/r/100/pic/cover/l/k-on.jpg",
            "canonical_url": "https://bgm.tv/subject/1424",
        }]
        self.client.force_authenticate(user=None)

        response = self.client.get(
            reverse("external-media-search", kwargs={"provider": "bangumi"}),
            {"q": "轻音少女"},
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["provider"], "bangumi")
        result = response.data["results"][0]
        self.assertEqual(result["external_id"], "1424")
        self.assertEqual(result["studio"], "京都アニメーション")
        for old_field in ("id", "name", "japanese_name", "eps", "poster", "thumbnail", "url"):
            self.assertNotIn(old_field, result)

    @patch("journal.external_media.views.get_provider")
    def test_external_media_subject_returns_unified_detail(self, get_provider):
        get_provider.return_value.fetch_subject.return_value = {
            "provider": "bangumi",
            "external_id": "277554",
            "title": "无职转生",
            "japanese_title": "無職転生",
            "summary": "",
            "episodes": 11,
            "air_date": "2021-01-11",
            "studio": "スタジオバインド",
            "tags": [],
            "score": None,
            "poster_url": "",
            "thumbnail_url": "",
            "canonical_url": "https://bgm.tv/subject/277554",
        }
        self.client.force_authenticate(user=None)

        response = self.client.get(reverse(
            "external-media-subject",
            kwargs={"provider": "bangumi", "external_id": "277554"},
        ))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["studio"], "スタジオバインド")
        self.assertEqual(response.data["episodes"], 11)

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

    def test_import_rejects_pre_ga_json_record_aliases(self):
        payload = {
            "version": 1,
            "records": [{"title": "旧格式", "japaneseTitle": "旧字段", "status": "planned"}],
        }
        upload = SimpleUploadedFile(
            "animemo.json",
            json.dumps(payload).encode("utf-8"),
            content_type="application/json",
        )

        response = self.client.post(reverse("import"), {"file": upload, "preview": "true"}, format="multipart")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data["code"], "unsupported_import_schema")
        self.assertFalse(JournalEntry.objects.filter(user=self.user).exists())

    def test_import_accepts_csv_templates(self):
        csv_file = SimpleUploadedFile(
            "animemo.csv",
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
        self.assertEqual(response.data["site_name"], "AniMemo")
        self.assertEqual(response.data["homepage_title"], "AniMemo · 我的动漫记忆库")
        self.assertEqual(
            response.data["homepage_description"],
            "把想看、在看与看完的作品收进同一条记忆轨迹，随时回望每一次与动画相遇的时刻。",
        )
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
        self.assertEqual(SiteSettings.load().site_name, "AniMemo")

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
        installation = InstallationState.load()
        installation.status = InstallationState.Status.INITIALIZED
        installation.save(update_fields=["status", "updated_at"])
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
