"""Keep the existing DEBUG filesystem mode usable without managed URL authority."""
from pathlib import Path
from unittest import skipUnless
from unittest.mock import patch

from django.core.files.base import ContentFile
from django.db import connection
from django.db.models.signals import post_save
from django.test import override_settings
from django.urls import reverse

from site_config.media_storage.development import DevelopmentFileStorage
from site_config.media_storage.storage import atomic_media_mutation
from site_config.models import MediaObject

from .media_references import poster_usage
from .models import JournalEntry, JournalMediaReference
from .test_media_references import MediaReferenceFixture, poster_upload
from . import test_media_references_postgres as postgres_fixtures


@override_settings(STORAGES={
    "default": {"BACKEND": "site_config.media_storage.development.DevelopmentFileStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
})
class DevelopmentMediaFixture(MediaReferenceFixture):
    def setUp(self):
        super().setUp()
        self.local_root = override_settings(MEDIA_ROOT=self.directory.name)
        self.local_root.enable()
        self.addCleanup(self.local_root.disable)

    def files(self):
        return {str(path.relative_to(self.directory.name)): path.read_bytes() for path in Path(self.directory.name).rglob("*") if path.is_file()}

    def create_file(self):
        response = self.client.post(reverse("entry-list"), {"title": "Development upload", "poster_file": poster_upload()}, format="multipart")
        self.assertEqual(response.status_code, 201, response.data)
        entry = JournalEntry.objects.get(pk=response.data["id"])
        self.assertIsInstance(entry.poster_file.storage, DevelopmentFileStorage)
        return entry


class DevelopmentMediaTests(DevelopmentMediaFixture):
    def test_upload_null_clear_and_quota_use_actual_local_bytes(self):
        entry = self.create_file()
        self.assertEqual(poster_usage(self.owner.pk).known_bytes, 84)
        self.assertFalse(MediaObject.objects.exists())
        self.assertFalse(JournalMediaReference.objects.exists())
        with override_settings(POSTER_STORAGE_QUOTA_BYTES=84):
            response = self.client.post(reverse("entry-list"), {"title": "Quota blocked", "poster_file": poster_upload()}, format="multipart")
        self.assertEqual(response.status_code, 400, response.data)
        self.assertEqual(len(self.files()), 1)
        response = self.client.patch(reverse("entry-detail", args=[entry.pk]), {"poster_file": None}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(poster_usage(self.owner.pk).known_bytes, 0)
        self.assertEqual(self.files(), {})

    def test_outer_rollback_preserves_old_local_file_and_removes_new_upload(self):
        entry = self.create_file()
        before = self.files()
        with self.assertRaisesRegex(RuntimeError, "outer rollback"):
            with atomic_media_mutation():
                response = self.client.patch(reverse("entry-detail", args=[entry.pk]), {"poster_file": poster_upload()}, format="multipart")
                self.assertEqual(response.status_code, 200, response.data)
                self.assertEqual(len(self.files()), 2)
                raise RuntimeError("isolated outer rollback")
        self.assertEqual(self.files(), before)
        entry.refresh_from_db()
        self.assertIn(entry.poster_file.name, {name.replace("\\", "/") for name in before})
        self.assertEqual(poster_usage(self.owner.pk).known_bytes, 84)

    def test_business_save_failure_removes_its_exclusive_file(self):
        def reject(sender, **kwargs):
            raise RuntimeError("isolated business failure")

        post_save.connect(reject, sender=JournalEntry, weak=False)
        try:
            with self.assertRaisesRegex(RuntimeError, "business failure"):
                JournalEntry.objects.create(user=self.owner, title="Failure", poster_file=poster_upload())
        finally:
            post_save.disconnect(reject, sender=JournalEntry)
        self.assertFalse(JournalEntry.objects.exists())
        self.assertEqual(self.files(), {})
        self.assertFalse(MediaObject.objects.exists())

    def test_partial_file_write_failure_preserves_prior_file_and_cleans_claim(self):
        self.create_file()
        before = self.files()

        def broken_chunks(content, chunk_size=None):
            content.seek(0)
            yield content.read(4)
            raise OSError("isolated partial write failure")

        with patch.object(ContentFile, "chunks", broken_chunks):
            response = self.client.post(reverse("entry-list"), {"title": "Partial failure", "poster_file": poster_upload()}, format="multipart")
        self.assertEqual(response.status_code, 500, response.data)
        self.assertEqual(self.files(), before)
        self.assertEqual(JournalEntry.objects.count(), 1)
        self.assertFalse(MediaObject.objects.exists())


@skipUnless(connection.vendor == "postgresql", "Development quota concurrency requires PostgreSQL row locks")
class DevelopmentMediaPostgreSQLTests(DevelopmentMediaFixture):
    run_owner_interleaving = postgres_fixtures.MediaReferencePostgreSQLTests.run_owner_interleaving

    @override_settings(POSTER_STORAGE_QUOTA_BYTES=100)
    def test_two_development_uploads_serialize_owner_admission(self):
        create = lambda client: client.post(reverse("entry-list"), {"title": "Concurrent development", "poster_file": poster_upload()}, format="multipart")
        responses = self.run_owner_interleaving(create, create)
        self.assertEqual([response.status_code for response in responses], [201, 400])
        self.assertEqual(poster_usage(self.owner.pk).known_bytes, 84)
        self.assertEqual(len(self.files()), 1)
        self.assertFalse(MediaObject.objects.exists())
        self.assertFalse(JournalMediaReference.objects.exists())
