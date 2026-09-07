"""Real storage uploads follow the complete entry mutation transaction."""

from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection, transaction
from django.test import TransactionTestCase, override_settings
from django.urls import reverse
from PIL import Image
from rest_framework.test import APIClient
from site_config.media_storage.pool import StoragePoolService
from site_config.models import InstallationState, MediaObject, MediaStorageBackend

from .domain_services import JournalEntryService
from .entry_views import JournalEntryViewSet
from .models import JournalEntry
from .serializers_entries import JournalEntrySerializer


@override_settings(STORAGES={
    "default": {"BACKEND": "site_config.media_storage.storage.StoragePoolStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
})
class EntryMediaTransactionTests(TransactionTestCase):
    def setUp(self):
        self.directory = TemporaryDirectory(prefix="entry-media-transaction-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        settings = override_settings(MEDIA_LOCAL_STORAGE_ROOT=str(self.root))
        settings.enable()
        self.addCleanup(settings.disable)
        self.user = get_user_model().objects.create_user("media-transaction-owner")
        self.entry = JournalEntry.objects.create(user=self.user, title="original")
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        state, _ = InstallationState.objects.get_or_create(pk=1)
        state.status = InstallationState.Status.INITIALIZED
        state.save(update_fields=["status"])
        backend = MediaStorageBackend.objects.create(
            slug="entry-media-transaction", name="Synthetic local transaction storage",
            backend_type=MediaStorageBackend.BackendType.LOCAL,
            priority=10, warning_bytes=1_000_000, write_limit_bytes=2_000_000,
            local_public_base_url="https://local.example.test/local-media",
            min_free_warning_bytes=2, min_free_block_bytes=1,
        )
        StoragePoolService.set_preferred_backend(backend)
        self.replace_poster("red")
        self.entry.refresh_from_db()
        self.old_name = self.entry.poster_file.name
        self.old_files = self.files()
        self.assertEqual(len(self.old_files), 1)

    @staticmethod
    def upload(color):
        output = BytesIO()
        Image.new("RGB", (32, 32), color).save(output, format="PNG")
        return SimpleUploadedFile("synthetic-poster.png", output.getvalue(), content_type="image/png")

    def files(self):
        return {path.relative_to(self.root): path.read_bytes() for path in self.root.rglob("*") if path.is_file()}

    def replace_poster(self, color):
        return JournalEntryService(self.user).update_from_fields(
            self.entry.pk, {"poster_file": self.upload(color)},
            serializer_class=JournalEntrySerializer, allowed_fields={"poster_file"},
            context={"request": SimpleNamespace(user=self.user)},
        )

    def patch_poster(self):
        return self.client.patch(
            reverse("entry-detail", args=[self.entry.pk]),
            {"poster_file": self.upload("blue")}, format="multipart",
        )

    def assert_original_media(self):
        self.entry.refresh_from_db()
        self.assertEqual(self.entry.poster_file.name, self.old_name)
        self.assertEqual(MediaObject.objects.count(), 1)
        self.assertEqual(self.files(), self.old_files)

    def test_http_projection_failure_cleans_upload_after_serializer_returned(self):
        original_query = JournalEntryViewSet.get_base_queryset
        calls = 0

        def fail_projection(view):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("synthetic response projection failure")
            return original_query(view)

        with patch.object(JournalEntryViewSet, "get_base_queryset", fail_projection), patch(
            "journal.mutation_ports._event_publisher",
        ) as events:
            response = self.patch_poster()
        self.assertEqual(response.status_code, 500)
        self.assertEqual(calls, 2)
        events.assert_not_called()
        self.assert_original_media()

    def test_http_serialization_failure_cleans_upload_and_preserves_old_bytes(self):
        with patch.object(
            JournalEntrySerializer, "to_representation",
            side_effect=RuntimeError("synthetic response serialization failure"),
        ), patch("journal.mutation_ports._event_publisher") as events:
            response = self.patch_poster()
        self.assertEqual(response.status_code, 500)
        events.assert_not_called()
        self.assert_original_media()

    def test_service_failure_after_serializer_returned_cleans_upload(self):
        with patch(
            "journal.domain_services.publish_event",
            side_effect=RuntimeError("synthetic event registration failure"),
        ), self.assertRaisesRegex(RuntimeError, "synthetic event registration failure"):
            self.replace_poster("blue")
        self.assert_original_media()

    def test_callback_failure_after_commit_preserves_new_media(self):
        original_get_object = JournalEntryViewSet.get_object
        observed = []

        def fail_after_commit():
            observed.append((connection.in_atomic_block, JournalEntry.objects.get(pk=self.entry.pk).poster_file.name))
            raise RuntimeError("synthetic callback failure after commit")

        def register_earlier_callback(view):
            current = original_get_object(view)
            # This callback precedes upload bookkeeping, so its exception also
            # prevents later on_commit callbacks from running.
            transaction.on_commit(fail_after_commit)
            return current

        with patch.object(JournalEntryViewSet, "get_object", register_earlier_callback):
            response = self.patch_poster()
        self.assertEqual(response.status_code, 500)
        self.entry.refresh_from_db()
        self.assertNotEqual(self.entry.poster_file.name, self.old_name)
        self.assertEqual(observed, [(False, self.entry.poster_file.name)])
        self.assertTrue(self.entry.poster_file.storage.exists(self.entry.poster_file.name))
        self.assertIsNotNone(StoragePoolService.resolve_reference(self.entry.poster_file.name))
        self.assertEqual(len(self.files()), 2)
        self.assertTrue(all(self.files().get(name) == data for name, data in self.old_files.items()))

    def test_success_deletes_previous_media_and_publishes_once_after_commit(self):
        observed = []

        def event(name, context):
            observed.append((name, context.journal_entry_id, connection.in_atomic_block))

        with patch("journal.mutation_ports._event_publisher", side_effect=event):
            response = self.patch_poster()
        self.assertEqual(response.status_code, 200, response.data)
        self.entry.refresh_from_db()
        self.assertNotEqual(self.entry.poster_file.name, self.old_name)
        self.assertTrue(self.entry.poster_file.storage.exists(self.entry.poster_file.name))
        self.assertEqual(MediaObject.objects.count(), 1)
        self.assertEqual(len(self.files()), 1)
        self.assertTrue(set(self.files()).isdisjoint(self.old_files))
        self.assertEqual(observed, [("journal.after_update", self.entry.pk, False)])
