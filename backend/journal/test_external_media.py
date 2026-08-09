from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db.models import Count, Max
from django.urls import reverse
from plugin_host.models import PluginData, PluginProject, UserPluginInstallation
from rest_framework import status
from rest_framework.test import APITestCase

from .external_media.errors import provider_timeout
from .models import ExternalMediaIdentity, JournalEntry
from .serializers_entries import JournalEntrySerializer

User = get_user_model()


def subject(external_id="1424", **overrides):
    result = {
        "title": "轻音少女",
        "japanese_title": "けいおん！",
        "summary": "轻音部的故事。",
        "episodes": 14,
        "air_date": "2009-04-03",
        "studio": "京都アニメーション",
        "tags": ["校园", "日常"],
        "score": 8.2,
        "poster_url": "https://bgm-img-proxy.xhcytus100.workers.dev/pic/cover/l/k-on.jpg",
        "thumbnail_url": "https://bgm-img-proxy.xhcytus100.workers.dev/r/100/pic/cover/l/k-on.jpg",
        "provider_name": "Bangumi",
        "provider_url": f"https://bgm.tv/subject/{external_id}",
        "external_id": str(external_id),
    }
    result.update(overrides)
    return result


class ExternalMediaIdentityApiTests(APITestCase):
    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(username="identity-owner", password="StrongPass123!")
        self.other = User.objects.create_user(username="identity-other", password="StrongPass123!")
        self.plugin = PluginProject.objects.create(
            plugin_id="com.anime-journal.watch-history-importer.identity",
            slug="watch-history-importer",
            name="观看记录",
            description="test",
        )
        UserPluginInstallation.objects.create(user=self.user, plugin=self.plugin, enabled=True)
        self.client.force_authenticate(self.user)

    def identity_url(self, entry, provider="bangumi"):
        return reverse("entry-external-identity-detail", kwargs={"pk": entry.pk, "provider": provider})

    def refresh_url(self, entry, provider="bangumi"):
        return reverse("entry-refresh-external-identity", kwargs={"pk": entry.pk, "provider": provider})

    def source_url(self, entry, provider="bangumi"):
        return reverse("entry-external-identity-metadata-source", kwargs={"pk": entry.pk, "provider": provider})

    def list_url(self, entry):
        return reverse("entry-external-identities", kwargs={"pk": entry.pk})

    def create_identity(self, entry, external_id="1424", metadata=None):
        return ExternalMediaIdentity.objects.create(
            entry=entry,
            provider="bangumi",
            external_id=external_id,
            canonical_url=f"https://bgm.tv/subject/{external_id}",
            metadata=metadata or subject(external_id),
            is_metadata_source=True,
        )

    def test_create_entry_without_identity_remains_compatible(self):
        response = self.client.post(reverse("entry-list"), {"title": "普通记录"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(response.data["external_identities"], [])

    @patch("journal.external_media.providers.bangumi.BangumiProvider.fetch_subject")
    def test_create_entry_with_identity_persists_server_metadata(self, fetch_subject):
        fetch_subject.return_value = subject()
        response = self.client.post(reverse("entry-list"), {
            "title": "用户确认后的标题",
            "external_identity": {"provider": "BANGUMI", "external_id": "001424"},
        }, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        identity = ExternalMediaIdentity.objects.get(entry_id=response.data["id"])
        self.assertEqual(identity.provider, "bangumi")
        self.assertEqual(identity.external_id, "1424")
        self.assertEqual(identity.metadata["score"], 8.2)
        self.assertTrue(identity.is_metadata_source)
        self.assertEqual(response.data["external_identities"][0]["external_id"], "1424")

    @patch("journal.external_media.providers.bangumi.BangumiProvider.fetch_subject")
    def test_multipart_create_accepts_json_identity(self, fetch_subject):
        fetch_subject.return_value = subject("20")
        response = self.client.post(reverse("entry-list"), {
            "title": "Multipart",
            "external_identity": '{"provider":"bangumi","external_id":"20"}',
        }, format="multipart")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertTrue(ExternalMediaIdentity.objects.filter(entry_id=response.data["id"], external_id="20").exists())

    @patch("journal.external_media.providers.bangumi.BangumiProvider.fetch_subject")
    def test_provider_failure_rolls_back_entry_creation(self, fetch_subject):
        fetch_subject.side_effect = provider_timeout()
        response = self.client.post(reverse("entry-list"), {
            "title": "不得残留",
            "external_identity": {"provider": "bangumi", "external_id": "21"},
        }, format="json")
        self.assertEqual(response.status_code, status.HTTP_504_GATEWAY_TIMEOUT)
        self.assertFalse(JournalEntry.objects.filter(title="不得残留").exists())

    def test_invalid_external_id_is_rejected_without_creating_entry(self):
        response = self.client.post(reverse("entry-list"), {
            "title": "不得残留",
            "external_identity": {"provider": "bangumi", "external_id": "https://attacker.example"},
        }, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data["code"], "invalid_external_id")
        self.assertFalse(JournalEntry.objects.filter(title="不得残留").exists())

    @patch("journal.external_media.providers.bangumi.BangumiProvider.fetch_subject")
    def test_bind_existing_entry(self, fetch_subject):
        entry = JournalEntry.objects.create(user=self.user, title="已有记录")
        fetch_subject.return_value = subject()
        response = self.client.post(self.list_url(entry), {"provider": "bangumi", "external_id": "1424"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(response.data["canonical_url"], "https://bgm.tv/subject/1424")

    @patch("journal.external_media.providers.bangumi.BangumiProvider.fetch_subject")
    def test_duplicate_provider_on_same_entry_requires_explicit_unbind(self, fetch_subject):
        entry = JournalEntry.objects.create(user=self.user, title="已有记录")
        self.create_identity(entry)
        response = self.client.post(self.list_url(entry), {"provider": "bangumi", "external_id": "99"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(response.data["code"], "identity_already_bound")
        fetch_subject.assert_not_called()

    @patch("journal.external_media.providers.bangumi.BangumiProvider.fetch_subject")
    def test_same_user_duplicate_subject_is_rejected_with_entry_id(self, fetch_subject):
        bound_entry = JournalEntry.objects.create(user=self.user, title="已绑定")
        target_entry = JournalEntry.objects.create(user=self.user, title="目标")
        self.create_identity(bound_entry)
        fetch_subject.return_value = subject()
        response = self.client.post(self.list_url(target_entry), {"provider": "bangumi", "external_id": "1424"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(response.data["code"], "subject_already_bound")
        self.assertEqual(response.data["entry_id"], bound_entry.pk)

    @patch("journal.external_media.providers.bangumi.BangumiProvider.fetch_subject")
    def test_different_users_can_bind_the_same_subject(self, fetch_subject):
        first = JournalEntry.objects.create(user=self.user, title="A")
        second = JournalEntry.objects.create(user=self.other, title="B")
        self.create_identity(first)
        fetch_subject.return_value = subject()
        other_client = self.client_class()
        other_client.force_authenticate(self.other)
        response = other_client.post(self.list_url(second), {"provider": "bangumi", "external_id": "1424"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    def test_cross_user_identity_list_and_bind_return_404(self):
        entry = JournalEntry.objects.create(user=self.other, title="别人记录")
        self.assertEqual(self.client.get(self.list_url(entry)).status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(
            self.client.post(self.list_url(entry), {"provider": "bangumi", "external_id": "1"}, format="json").status_code,
            status.HTTP_404_NOT_FOUND,
        )

    def test_cross_user_refresh_and_unbind_return_404(self):
        entry = JournalEntry.objects.create(user=self.other, title="别人记录")
        self.create_identity(entry)
        self.assertEqual(self.client.post(self.refresh_url(entry), {}, format="json").status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(self.client.delete(self.identity_url(entry)).status_code, status.HTTP_404_NOT_FOUND)

    def test_unbind_preserves_entry_and_user_data(self):
        entry = JournalEntry.objects.create(
            user=self.user, title="保留", personal_score="9.50", watch_status="completed", review="个人评价",
        )
        identity = self.create_identity(entry)
        history = PluginData.objects.create(
            plugin=self.plugin, user=self.user, namespace="watch_history", key=str(entry.pk), value=[{"watched_on": "2026-01-01"}],
        )
        response = self.client.delete(self.identity_url(entry))
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(ExternalMediaIdentity.objects.filter(pk=identity.pk).exists())
        entry.refresh_from_db()
        self.assertEqual(entry.review, "个人评价")
        self.assertTrue(PluginData.objects.filter(pk=history.pk).exists())

    def test_entry_delete_cascades_identity(self):
        entry = JournalEntry.objects.create(user=self.user, title="删除")
        identity = self.create_identity(entry)
        entry.delete()
        self.assertFalse(ExternalMediaIdentity.objects.filter(pk=identity.pk).exists())

    @patch("journal.external_media.providers.bangumi.BangumiProvider.refresh")
    def test_refresh_updates_snapshot_and_only_safe_fields(self, refresh):
        entry = JournalEntry.objects.create(
            user=self.user,
            title="用户标题",
            japanese_title="旧日文名",
            airing_period="2008-1",
            studio="旧公司",
            episodes="12",
            description="用户简介",
            tags=["用户标签"],
            personal_score="9.70",
            watch_status="completed",
            review="用户评价",
            visibility="public",
            custom_poster_url="https://lain.bgm.tv/pic/cover/l/custom.jpg",
        )
        identity = self.create_identity(entry)
        history = PluginData.objects.create(
            plugin=self.plugin, user=self.user, namespace="watch_history", key=str(entry.pk), value=[{"watched_on": "2026-01-01"}],
        )
        original_slug = entry.share_slug
        refresh.return_value = subject(score=7.1)
        response = self.client.post(self.refresh_url(entry), {}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(
            response.data["applied_fields"],
            ["japanese_title", "airing_period", "studio", "episodes", "poster_url"],
        )
        identity.refresh_from_db()
        entry.refresh_from_db()
        self.assertEqual(identity.metadata["score"], 7.1)
        self.assertIsNotNone(identity.metadata_fetched_at)
        self.assertEqual(entry.japanese_title, "けいおん！")
        self.assertEqual(entry.airing_period, "2009-4")
        self.assertEqual(entry.studio, "京都アニメーション")
        self.assertEqual(entry.episodes, "14")
        self.assertEqual(entry.title, "用户标题")
        self.assertEqual(entry.description, "用户简介")
        self.assertEqual(entry.tags, ["用户标签"])
        self.assertEqual(str(entry.personal_score), "9.70")
        self.assertEqual(entry.watch_status, "completed")
        self.assertEqual(entry.review, "用户评价")
        self.assertEqual(entry.visibility, "public")
        self.assertEqual(entry.custom_poster_url, "https://lain.bgm.tv/pic/cover/l/custom.jpg")
        self.assertEqual(entry.share_slug, original_slug)
        self.assertTrue(PluginData.objects.filter(pk=history.pk).exists())

    @patch("journal.external_media.providers.bangumi.BangumiProvider.refresh")
    def test_refresh_bounds_provider_values_to_entry_schema(self, refresh):
        entry = JournalEntry.objects.create(user=self.user, title="字段边界")
        self.create_identity(entry)
        refresh.return_value = subject(
            japanese_title="日" * 250,
            studio="S" * 150,
            episodes="1" * 50,
            poster_url=f"https://lain.bgm.tv/{'x' * 1100}",
        )
        response = self.client.post(self.refresh_url(entry), {}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        entry.refresh_from_db()
        self.assertEqual(len(entry.japanese_title), 200)
        self.assertEqual(len(entry.studio), 120)
        self.assertEqual(len(entry.episodes), 30)
        self.assertEqual(entry.poster_url, "")

    @patch("journal.external_media.providers.bangumi.BangumiProvider.refresh")
    def test_provider_score_never_overwrites_personal_score(self, refresh):
        entry = JournalEntry.objects.create(user=self.user, title="评分", personal_score="9.90")
        self.create_identity(entry)
        refresh.return_value = subject(score=1.2)
        self.client.post(self.refresh_url(entry), {}, format="json")
        entry.refresh_from_db()
        self.assertEqual(str(entry.personal_score), "9.90")

    @patch("journal.external_media.providers.bangumi.BangumiProvider.refresh")
    def test_custom_poster_remains_display_priority_after_refresh(self, refresh):
        entry = JournalEntry.objects.create(
            user=self.user,
            title="封面",
            poster_url="https://lain.bgm.tv/pic/cover/l/old.jpg",
            custom_poster_url="https://lain.bgm.tv/pic/cover/l/custom.jpg",
        )
        self.create_identity(entry)
        refresh.return_value = subject()
        self.client.post(self.refresh_url(entry), {}, format="json")
        detail = self.client.get(reverse("entry-detail", kwargs={"pk": entry.pk}))
        self.assertEqual(detail.data["poster"], "https://lain.bgm.tv/pic/cover/l/custom.jpg")
        self.assertEqual(detail.data["poster_source"], "trusted_url")

    def test_missing_identity_refresh_and_unbind_are_404(self):
        entry = JournalEntry.objects.create(user=self.user, title="无绑定")
        refresh_response = self.client.post(self.refresh_url(entry), {}, format="json")
        delete_response = self.client.delete(self.identity_url(entry))
        self.assertEqual(refresh_response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(refresh_response.data["code"], "identity_not_found")
        self.assertEqual(delete_response.status_code, status.HTTP_404_NOT_FOUND)

    @patch("journal.external_media.providers.bangumi.BangumiProvider.refresh")
    def test_refresh_rejects_stale_result_after_unbind_and_rebind(self, refresh):
        entry = JournalEntry.objects.create(user=self.user, title="竞态保护")
        original = self.create_identity(entry, external_id="123", metadata=subject("123"))

        def replace_identity(_identity):
            original.delete()
            self.create_identity(entry, external_id="456", metadata=subject("456", title="新绑定"))
            return subject("123", title="过期结果")

        refresh.side_effect = replace_identity
        response = self.client.post(self.refresh_url(entry), {}, format="json")
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(response.data["code"], "external_identity_changed")
        current = ExternalMediaIdentity.objects.get(entry=entry, provider="bangumi")
        self.assertEqual(current.external_id, "456")
        self.assertEqual(current.metadata["title"], "新绑定")

    def test_existing_identity_is_returned_without_calling_provider(self):
        entry = JournalEntry.objects.create(user=self.user, title="本地快照")
        self.create_identity(entry)
        with patch("journal.external_media.providers.bangumi.BangumiProvider.fetch_subject") as fetch_subject:
            response = self.client.get(reverse("entry-detail", kwargs={"pk": entry.pk}))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["external_identities"][0]["metadata"]["title"], "轻音少女")
        fetch_subject.assert_not_called()

    def test_non_source_refresh_only_updates_snapshot(self):
        entry = JournalEntry.objects.create(user=self.user, title="用户标题", studio="本地公司")
        self.create_identity(entry)
        other = ExternalMediaIdentity.objects.create(
            entry=entry,
            provider="anilist",
            external_id="7",
            canonical_url="https://anilist.co/anime/7",
            metadata=subject("7", studio="旧快照"),
            is_metadata_source=False,
        )
        provider = SimpleNamespace(
            slug="anilist",
            refresh=lambda _identity: subject("7", studio="远端公司"),
            canonical_url=lambda external_id: f"https://anilist.co/anime/{external_id}",
        )
        with patch("journal.external_media.services.get_provider", return_value=provider):
            response = self.client.post(self.refresh_url(entry, "anilist"), {}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data["applied_fields"], [])
        entry.refresh_from_db()
        other.refresh_from_db()
        self.assertEqual(entry.studio, "本地公司")
        self.assertEqual(other.metadata["studio"], "远端公司")

    def test_source_switch_requires_explicit_apply_choice(self):
        entry = JournalEntry.objects.create(user=self.user, title="来源切换", studio="本地公司")
        bangumi = self.create_identity(entry)
        anilist = ExternalMediaIdentity.objects.create(
            entry=entry,
            provider="anilist",
            external_id="8",
            canonical_url="https://anilist.co/anime/8",
            metadata=subject("8", studio="新来源公司"),
            is_metadata_source=False,
        )
        provider = SimpleNamespace(slug="anilist")
        with patch("journal.external_media.services.get_provider", return_value=provider):
            missing = self.client.post(self.source_url(entry, "anilist"), {}, format="json")
            switched = self.client.post(
                self.source_url(entry, "anilist"),
                {"apply_metadata": True},
                format="json",
            )
        self.assertEqual(missing.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(switched.status_code, status.HTTP_200_OK, switched.data)
        bangumi.refresh_from_db()
        anilist.refresh_from_db()
        entry.refresh_from_db()
        self.assertFalse(bangumi.is_metadata_source)
        self.assertTrue(anilist.is_metadata_source)
        self.assertEqual(entry.studio, "新来源公司")

    def test_unbound_entry_get_works_when_provider_is_unavailable(self):
        entry = JournalEntry.objects.create(user=self.user, title="离线可读")
        with patch("journal.external_media.providers.bangumi.BangumiProvider.fetch_subject", side_effect=provider_timeout()):
            response = self.client.get(reverse("entry-detail", kwargs={"pk": entry.pk}))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["external_identities"], [])

    def test_prefetched_identity_serialization_does_not_add_per_entry_queries(self):
        for index in range(3):
            entry = JournalEntry.objects.create(user=self.user, title=f"记录 {index}")
            self.create_identity(entry, external_id=str(100 + index), metadata=subject(str(100 + index)))
        queryset = JournalEntry.objects.filter(user=self.user).annotate(
            watch_history_count=Count("watch_history_records", distinct=True),
            last_watched_on=Max("watch_history_records__watched_on"),
        ).prefetch_related("external_identities")
        with self.assertNumQueries(2):
            data = JournalEntrySerializer(queryset, many=True).data
        self.assertEqual(len(data), 3)
