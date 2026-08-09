import json

from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from journal.data_bundle import export_data_bundle, import_data_bundle
from journal.models import (
    ExternalCollectionSyncState,
    ExternalImportSession,
    ExternalMediaIdentity,
    JournalEntry,
    UserExternalAccountConnection,
)
from journal.watch_history import add_history

User = get_user_model()


class DataBundleV1Tests(APITestCase):
    def setUp(self):
        self.source_user = User.objects.create_user(username="bundle-source", password="StrongPass123!")
        self.target_user = User.objects.create_user(username="bundle-target", password="StrongPass123!")
        self.entry = JournalEntry.objects.create(
            user=self.source_user,
            title="数据包番剧",
            japanese_title="データバンドル",
            airing_period="2026-7",
            studio="AniMemo Studio",
            episodes="12",
            description="完整描述",
            poster_url="https://lain.bgm.tv/pic/cover/l/example.jpg",
            tags=["科幻", "日常"],
            tag_colors={"科幻": "#ff6b6b"},
            personal_score="8.50",
            watch_status=JournalEntry.WatchStatus.DROPPED,
            review="完整短评",
            visibility=JournalEntry.Visibility.UNLISTED,
        )
        ExternalMediaIdentity.objects.create(
            entry=self.entry,
            provider="bangumi",
            external_id="12345",
            canonical_url="https://bgm.tv/subject/12345",
            metadata={"title": "数据包番剧", "score": 7.6},
            metadata_schema_version=1,
            is_metadata_source=True,
            metadata_fetched_at=timezone.now(),
        )
        add_history(
            user=self.source_user,
            entry=self.entry,
            record={
                "watched_on": "2026-07-02",
                "watched_label": "2026年7月2日",
                "brush_number": 2,
                "brush_label": "二刷",
                "episode_start": 1,
                "episode_end": 3,
                "notes": ["重新观看"],
                "metadata": {"origin": "manual"},
            },
        )
        UserExternalAccountConnection.objects.create(
            user=self.source_user,
            provider="bangumi",
            auth_method=UserExternalAccountConnection.AuthMethod.PERSONAL_ACCESS_TOKEN,
            external_user_id="999",
            external_username="secret-user",
            credential_ciphertext="secret-ciphertext",
            connected_at=timezone.now(),
        )
        ExternalImportSession.objects.create(
            user=self.source_user,
            provider="bangumi",
            snapshot=[{"access_token": "never-export-this"}],
            expires_at=timezone.now(),
        )
        ExternalCollectionSyncState.objects.create(
            identity=self.entry.external_identities.get(),
            connection=self.source_user.external_account_connections.get(provider="bangumi"),
            baselines={"watch_status": {"present": True, "value": "dropped"}},
            last_synced_at=timezone.now(),
        )

    @staticmethod
    def semantic(bundle):
        return {key: value for key, value in bundle.items() if key != "exported_at"}

    def test_bundle_roundtrip_preserves_core_semantics_and_excludes_credentials(self):
        first = export_data_bundle(user=self.source_user)
        encoded = json.dumps(first, ensure_ascii=False)

        self.assertNotIn("credential_ciphertext", encoded)
        self.assertNotIn("secret-ciphertext", encoded)
        self.assertNotIn("access_token", encoded)
        self.assertNotIn("ExternalImportSession", encoded)
        self.assertNotIn("ExternalCollectionSyncState", encoded)
        self.assertNotIn("last_synced_at", encoded)

        result = import_data_bundle(user=self.target_user, payload=first)
        second = export_data_bundle(user=self.target_user)

        self.assertEqual(result["created"], 1)
        self.assertEqual(self.semantic(second), self.semantic(first))
        restored = JournalEntry.objects.get(user=self.target_user)
        self.assertEqual(restored.watch_status, JournalEntry.WatchStatus.DROPPED)
        self.assertEqual(restored.external_identities.get().is_metadata_source, True)
        self.assertEqual(restored.watch_history_records.get().notes, ["重新观看"])
        self.assertFalse(ExternalCollectionSyncState.objects.filter(identity__entry=restored).exists())

    def test_export_api_returns_bundle_v1(self):
        self.client.force_authenticate(self.source_user)

        response = self.client.get("/api/export/")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["format"], "animemo-data-bundle")
        self.assertEqual(response.data["schema_version"], 1)

    def test_legacy_json_without_schema_is_rejected_with_stable_code(self):
        self.client.force_authenticate(self.target_user)

        response = self.client.post("/api/import/", {"records": [{"title": "旧格式"}]}, format="json")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data["code"], "unsupported_import_schema")
        self.assertFalse(JournalEntry.objects.filter(user=self.target_user).exists())

    def test_bundle_restore_requires_empty_journal_and_is_atomic(self):
        JournalEntry.objects.create(user=self.target_user, title="已有记录")
        payload = export_data_bundle(user=self.source_user)

        with self.assertRaisesRegex(ValueError, "空手账"):
            import_data_bundle(user=self.target_user, payload=payload)

        self.assertEqual(JournalEntry.objects.filter(user=self.target_user).count(), 1)

    def test_invalid_nested_history_rolls_back_everything(self):
        payload = export_data_bundle(user=self.source_user)
        payload["entries"][0]["watch_history"][0]["episode_start"] = 9
        payload["entries"][0]["watch_history"][0]["episode_end"] = 2

        self.client.force_authenticate(self.target_user)
        response = self.client.post("/api/import/", payload, format="json")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data["code"], "invalid_episode_range")
        self.assertFalse(JournalEntry.objects.filter(user=self.target_user).exists())


class LossyCsvImportTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="csv-user", password="StrongPass123!")
        self.client.force_authenticate(self.user)

    def test_csv_uses_only_canonical_snake_case_fields(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        upload = SimpleUploadedFile(
            "entries.csv",
            b"title,japanese_title,watch_status\nCSV Anime,CSV Original,planned\n",
            content_type="text/csv",
        )
        response = self.client.post("/api/import/", {"file": upload}, format="multipart")

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["created"], 1)
        self.assertEqual(JournalEntry.objects.get(user=self.user).japanese_title, "CSV Original")
