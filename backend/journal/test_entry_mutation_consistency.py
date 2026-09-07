"""Owner mutations use current rows, including when request reads overlap."""

from concurrent.futures import ThreadPoolExecutor
from datetime import date
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import close_old_connections, connections, transaction
from django.test import TransactionTestCase, override_settings, skipUnlessDBFeature
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient, APITestCase
from PIL import Image
from site_config.media_storage.pool import StoragePoolService
from site_config.models import InstallationState, MediaObject, MediaStorageBackend

from .domain_services import JournalEntryService, JournalEntryServiceError
from .entry_views import JournalEntryViewSet
from .models import JournalEntry, WatchHistoryRecord
from .serializers_entries import JournalEntrySerializer


class EntryMutationConsistencyTests(APITestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("mutation-owner")
        self.entry = JournalEntry.objects.create(user=self.user, title="original", review="original review")
        self.service = JournalEntryService(self.user)
        self.client.force_authenticate(self.user)
        state = InstallationState.load()
        state.status = InstallationState.Status.INITIALIZED
        state.save(update_fields=["status"])

    def serializer(self, **fields):
        serializer = JournalEntrySerializer(JournalEntry.objects.get(pk=self.entry.pk), data=fields, partial=True)
        serializer.is_valid(raise_exception=True)
        return serializer

    def test_stale_serializers_preserve_non_overlapping_fields(self):
        first = self.serializer(title="new title")
        second = self.serializer(review="new review")
        self.service.update(first)
        self.service.update(second)
        self.entry.refresh_from_db()
        self.assertEqual((self.entry.title, self.entry.review), ("new title", "new review"))

    def test_stale_serializer_cannot_restore_recycled_entry(self):
        stale = self.serializer(review="must not apply")
        JournalEntry.objects.filter(pk=self.entry.pk).update(deleted_at=timezone.now())
        with self.assertRaises(JournalEntryServiceError) as caught:
            self.service.update(stale)
        self.assertEqual(caught.exception.status_code, 404)
        self.entry.refresh_from_db()
        self.assertIsNotNone(self.entry.deleted_at)
        self.assertEqual(self.entry.review, "original review")

    def test_stale_serializer_cannot_reinsert_deleted_entry(self):
        stale = self.serializer(review="must not reinsert")
        self.service.delete(self.entry.pk)
        with self.assertRaises(JournalEntryServiceError) as caught:
            self.service.update(stale)
        self.assertEqual(caught.exception.status_code, 404)
        self.assertFalse(JournalEntry.objects.filter(pk=self.entry.pk).exists())

    def test_http_revalidates_current_instance_and_keeps_history_projection(self):
        WatchHistoryRecord.objects.create(entry=self.entry, watched_on=date(2026, 1, 2), sequence=1)
        stale = JournalEntry.objects.get(pk=self.entry.pk)
        self.service.update_from_fields(self.entry.pk, {"title": "other request"}, serializer_class=JournalEntrySerializer)
        with patch.object(JournalEntryViewSet, "get_object", return_value=stale):
            response = self.client.patch(reverse("entry-detail", args=[self.entry.pk]), {"review": "current request"})
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["title"], "other request")
        self.assertEqual(response.data["review"], "current request")
        self.assertEqual(response.data["watch_history_count"], 1)
        self.assertEqual(str(response.data["first_watched_on"]), "2026-01-02")

    def test_http_deleted_after_lookup_returns_not_found(self):
        stale = JournalEntry.objects.get(pk=self.entry.pk)
        self.service.delete(self.entry.pk)
        with patch.object(JournalEntryViewSet, "get_object", return_value=stale):
            response = self.client.patch(reverse("entry-detail", args=[self.entry.pk]), {"review": "late"})
        self.assertEqual(response.status_code, 404, response.data)
        self.assertFalse(JournalEntry.objects.filter(pk=self.entry.pk).exists())


@skipUnlessDBFeature("has_select_for_update")
@override_settings(STORAGES={
    "default": {"BACKEND": "site_config.media_storage.storage.StoragePoolStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
})
class EntryMutationPostgreSQLTests(TransactionTestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("mutation-pg-owner")
        self.entry = JournalEntry.objects.create(user=self.user, title="original", review="original review")
        state, _ = InstallationState.objects.get_or_create(pk=1)
        state.status = InstallationState.Status.INITIALIZED
        state.save(update_fields=["status"])

    def overlapping_patch(self, fields, earlier_mutation, *, request_format="json"):
        """Worker reads the old row while the first connection owns its lock."""
        read_done = Event()
        original_get_object = JournalEntryViewSet.get_object

        def observed_get_object(view):
            obj = original_get_object(view)
            read_done.set()
            return obj

        def worker():
            close_old_connections()
            try:
                client = APIClient()
                client.force_authenticate(get_user_model().objects.get(pk=self.user.pk))
                response = client.patch(reverse("entry-detail", args=[self.entry.pk]), fields, format=request_format)
                return response.status_code, response.data
            finally:
                connections.close_all()

        with patch.object(JournalEntryViewSet, "get_object", observed_get_object), ThreadPoolExecutor(max_workers=1) as pool:
            with transaction.atomic():
                locked = JournalEntry.objects.select_for_update().get(pk=self.entry.pk)
                future = pool.submit(worker)
                self.assertTrue(read_done.wait(10), "second connection did not read the old row")
                earlier_mutation(locked)
            return future.result(timeout=15)

    def test_disjoint_http_patch_preserves_first_committed_title(self):
        def first(entry):
            entry.title = "first title"
            entry.save(update_fields=["title", "updated_at"])
        status_code, body = self.overlapping_patch({"review": "second review"}, first)
        self.assertEqual(status_code, 200, body)
        self.entry.refresh_from_db()
        self.assertEqual((self.entry.title, self.entry.review), ("first title", "second review"))

    def test_same_field_uses_last_lock_holder_value(self):
        def first(entry):
            entry.title = "first title"
            entry.save(update_fields=["title", "updated_at"])
        status_code, body = self.overlapping_patch({"title": "second title"}, first)
        self.assertEqual(status_code, 200, body)
        self.entry.refresh_from_db()
        self.assertEqual(self.entry.title, "second title")

    def test_recycle_before_late_patch_returns_404_and_keeps_tombstone(self):
        def first(entry):
            entry.deleted_at = timezone.now()
            entry.save(update_fields=["deleted_at", "updated_at"])
        status_code, body = self.overlapping_patch({"review": "late review"}, first)
        self.assertEqual(status_code, 404, body)
        self.entry.refresh_from_db()
        self.assertIsNotNone(self.entry.deleted_at)
        self.assertEqual(self.entry.review, "original review")

    def test_delete_before_late_patch_cannot_recreate_row(self):
        status_code, body = self.overlapping_patch({"review": "late review"}, lambda entry: entry.delete())
        self.assertEqual(status_code, 404, body)
        self.assertFalse(JournalEntry.objects.filter(pk=self.entry.pk).exists())

    def test_restore_before_patch_keeps_current_restored_fields(self):
        def first(entry):
            entry.deleted_at = timezone.now()
            entry.save(update_fields=["deleted_at", "updated_at"])
            entry.deleted_at = None
            entry.review = "review present when restored"
            entry.save(update_fields=["deleted_at", "review", "updated_at"])
        status_code, body = self.overlapping_patch({"title": "late title"}, first)
        self.assertEqual(status_code, 200, body)
        self.entry.refresh_from_db()
        self.assertIsNone(self.entry.deleted_at)
        self.assertEqual(self.entry.review, "review present when restored")
        self.assertEqual(self.entry.title, "late title")

    def test_update_event_runs_once_after_commit_and_never_after_rollback(self):
        observed = []

        def event(name, context):
            observed.append((name, context.source, connections["default"].in_atomic_block,
                             JournalEntry.objects.get(pk=context.journal_entry_id).review))

        service = JournalEntryService(self.user)
        with patch("journal.mutation_ports._event_publisher", side_effect=event):
            with self.assertRaisesRegex(RuntimeError, "rollback fixture"), transaction.atomic():
                service.update_from_fields(self.entry.pk, {"review": "rolled back"}, serializer_class=JournalEntrySerializer)
                self.assertEqual(observed, [])
                raise RuntimeError("rollback fixture")
            self.entry.refresh_from_db()
            self.assertEqual(self.entry.review, "original review")
            self.assertEqual(observed, [])
            with transaction.atomic():
                service.update_from_fields(self.entry.pk, {"review": "committed"}, serializer_class=JournalEntrySerializer, source="api")
                self.assertEqual(observed, [])
        self.assertEqual(observed, [("journal.after_update", "api", False, "committed")])

    @staticmethod
    def upload(color):
        data = BytesIO()
        Image.new("RGB", (32, 32), color).save(data, format="PNG")
        return SimpleUploadedFile("poster.png", data.getvalue(), content_type="image/png")

    def configure_media(self):
        backend = MediaStorageBackend.objects.create(
            slug="mutation-local", name="Mutation fixture", backend_type=MediaStorageBackend.BackendType.LOCAL,
            priority=10, warning_bytes=1_000_000, write_limit_bytes=2_000_000,
            local_public_base_url="https://local.example.test/local-media",
            min_free_warning_bytes=2, min_free_block_bytes=1,
        )
        StoragePoolService.set_preferred_backend(backend)

    def replace_poster(self, upload):
        return JournalEntryService(self.user).update_from_fields(
            self.entry.pk, {"poster_file": upload}, serializer_class=JournalEntrySerializer,
            allowed_fields={"poster_file"}, context={"request": SimpleNamespace(user=self.user)},
        )

    def test_concurrent_media_replacements_delete_each_superseded_object(self):
        with TemporaryDirectory() as directory, override_settings(MEDIA_LOCAL_STORAGE_ROOT=directory):
            self.configure_media()
            self.replace_poster(self.upload("red"))
            self.entry.refresh_from_db()
            original_name = self.entry.poster_file.name
            first_names = []

            def first(entry):
                self.replace_poster(self.upload("green"))
                entry.refresh_from_db()
                first_names.append(entry.poster_file.name)

            status_code, body = self.overlapping_patch({"poster_file": self.upload("blue")}, first, request_format="multipart")
            self.assertEqual(status_code, 200, body)
            self.entry.refresh_from_db()
            self.assertNotIn(self.entry.poster_file.name, [original_name, *first_names])
            self.assertEqual(MediaObject.objects.count(), 1)
            self.assertEqual(len([path for path in Path(directory).rglob("*") if path.is_file()]), 1)
            self.assertTrue(self.entry.poster_file.storage.exists(self.entry.poster_file.name))

    def test_media_save_failure_preserves_previous_file_and_rolls_back_new_object(self):
        with TemporaryDirectory() as directory, override_settings(MEDIA_LOCAL_STORAGE_ROOT=directory):
            self.configure_media()
            self.replace_poster(self.upload("red"))
            self.entry.refresh_from_db()
            original_name = self.entry.poster_file.name
            original_files = {path.relative_to(directory): path.read_bytes() for path in Path(directory).rglob("*") if path.is_file()}
            original_save = JournalEntry._save_table

            def fail_after_upload(instance, *args, **kwargs):
                original_save(instance, *args, **kwargs)
                raise RuntimeError("media save rollback fixture")

            with patch.object(JournalEntry, "_save_table", fail_after_upload), self.assertRaisesRegex(RuntimeError, "media save rollback fixture"):
                self.replace_poster(self.upload("blue"))
            self.entry.refresh_from_db()
            self.assertEqual(self.entry.poster_file.name, original_name)
            self.assertEqual(MediaObject.objects.count(), 1)
            files = {path.relative_to(directory): path.read_bytes() for path in Path(directory).rglob("*") if path.is_file()}
            self.assertEqual(files, original_files)
