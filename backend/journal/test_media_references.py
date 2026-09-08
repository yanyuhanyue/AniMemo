import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.db import connection, transaction
from django.test import TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from PIL import Image
from rest_framework.test import APIClient

from site_config.media_storage.local import DynamicLocalBackend
from site_config.media_storage.common import MediaStorageExhausted
from site_config.media_storage.pool import StoragePoolService
from site_config.media_storage.storage import atomic_media_mutation
from site_config.media_storage.usage import managed_usage_bytes
from site_config.models import InstallationState, MediaObject, MediaStorageBackend, MediaWriteReservation, SiteSettings

from .domain_services import JournalEntryService
from .media_inventory import backfill_media_references, inspect_media_references
from .media_references import cleanup_media, poster_usage
from .models import Column, JournalEntry, JournalMediaReference, UserSettings
from .serializers_entries import JournalEntrySerializer


def poster_upload():
    data = io.BytesIO()
    Image.new("RGB", (32, 32), "red").save(data, format="PNG")
    return SimpleUploadedFile("synthetic.png", data.getvalue(), content_type="image/png")


@override_settings(STORAGES={
    "default": {"BACKEND": "site_config.media_storage.storage.StoragePoolStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
})
class MediaReferenceFixture(TransactionTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.media_settings = override_settings(MEDIA_LOCAL_STORAGE_ROOT=self.directory.name)
        self.media_settings.enable()
        self.addCleanup(self.media_settings.disable)
        self.owner = get_user_model().objects.create_user("poster-owner")
        self.other = get_user_model().objects.create_user("poster-other")
        state, _ = InstallationState.objects.get_or_create(pk=1)
        state.status = InstallationState.Status.INITIALIZED
        state.save(update_fields=["status"])
        site = SiteSettings.load()
        site.trusted_poster_hosts = ["media.example.test", "third.example.test"]
        site.save(update_fields=["trusted_poster_hosts"])
        self.backend = MediaStorageBackend.objects.create(
            slug="hold-local", name="Isolated poster media", backend_type="local",
            warning_bytes=1_000_000, write_limit_bytes=2_000_000,
            local_public_base_url="https://media.example.test/local-media",
            min_free_warning_bytes=2, min_free_block_bytes=1,
        )
        StoragePoolService.set_preferred_backend(self.backend)
        self.client = APIClient()
        self.client.force_authenticate(self.owner)

    def upload(self, title="Source"):
        response = self.client.post(reverse("entry-list"), {"title": title, "poster_file": poster_upload()}, format="multipart")
        self.assertEqual(response.status_code, 201, response.data)
        entry = JournalEntry.objects.get(pk=response.data["id"])
        media = StoragePoolService.resolve_reference(entry.poster_file.name)
        return entry, media, response.data["poster"]

    def alias(self, url, expected=201):
        response = self.client.post(reverse("entry-list"), {"title": "Alias", "custom_poster_url": url}, format="json")
        self.assertEqual(response.status_code, expected, response.data)
        return JournalEntry.objects.get(pk=response.data["id"]) if expected == 201 else response

    def clear(self, entry):
        response = self.client.patch(reverse("entry-detail", args=[entry.pk]), {"clear_custom_poster": True}, format="json")
        self.assertEqual(response.status_code, 200, response.data)


class MediaReferenceTests(MediaReferenceFixture):
    def test_explicit_zero_port_does_not_create_a_default_https_holding(self):
        _source, media, url = self.upload()
        normal = self.alias(url.replace("media.example.test", "media.example.test:443"))
        zero_url = url.replace("media.example.test", "media.example.test:0")
        zero = self.alias(zero_url)
        self.assertEqual(zero.custom_poster_url, zero_url)
        self.assertFalse(zero.media_references.exists())
        self.assertEqual(normal.media_references.get().media_id, media.pk)
        self.assertEqual(media.journal_references.count(), 2)
        self.assertEqual(poster_usage(self.owner.pk).known_bytes, 2 * media.size_bytes)
        self.assertEqual(managed_usage_bytes(self.backend), media.size_bytes)
        from site_config.media_storage.identity import url_identity

        for prefix in ("https://@", "https://:@"):
            value = url.replace("https://", prefix, 1)
            self.assertIsNone(url_identity(value))
            response = self.client.post(reverse("entry-list"), {"title": "Empty userinfo", "custom_poster_url": value}, format="json")
            self.assertIn(response.status_code, (201, 400))
            if response.status_code == 201:
                self.assertFalse(JournalMediaReference.objects.filter(entry_id=response.data["id"]).exists())
            self.assertEqual(poster_usage(self.owner.pk).known_bytes, 2 * media.size_bytes)

    def test_remote_direct_inventory_preserves_unverified_health_and_blocks_url_claims(self):
        # A controlled adapter exercises real upload/reference/admission paths.
        # Its acknowledgements are not evidence of actual R2 byte availability.
        from .test_media_storage import r2_backend

        self.backend = r2_backend("hold-remote", 1)
        StoragePoolService.set_preferred_backend(self.backend)
        site = SiteSettings.load()
        site.trusted_poster_hosts = ["hold-remote.example.com"]
        site.save(update_fields=["trusted_poster_hosts"])
        files, deletes = {}, []
        base = self.backend.public_base_url

        class ControlledRemote:
            def write(self, key, content, **kwargs):
                files[key] = bytes(content)

            def url(self, key):
                return base + "/" + key

            def delete(self, key):
                deletes.append(key)
                files.pop(key, None)

            def exists(self, key):
                raise AssertionError("Inventory must not probe remote byte availability")

        with patch.object(StoragePoolService, "adapter_for", return_value=ControlledRemote()):
            source, media, url = self.upload()
            original = dict(files)
            JournalMediaReference.objects.all().delete()
            MediaObject.objects.filter(pk=media.pk).update(reference_inventory_complete=False)
            report = backfill_media_references()
            self.assertEqual(report["status"], "READY", report)
            self.assertEqual(report["objects"][0]["byte_verification"], "UNVERIFIED_REMOTE_BYTES")
            self.assertEqual(media.journal_references.count(), 1)
            self.assertEqual(poster_usage(self.owner.pk).known_bytes, media.size_bytes)
            self.alias(url, 400)
            self.assertEqual(files, original)

            MediaObject.objects.filter(pk=media.pk).update(sha256="")
            report = backfill_media_references()
            self.assertEqual(report["status"], "BLOCKED")
            self.assertIn("UNKNOWN_USAGE", report["objects"][0]["issues"])
            MediaObject.objects.filter(pk=media.pk).update(sha256=media.sha256)

            # Explicit old-schema business fixture: no current URL admission is
            # bypassed or claimed successful by this historical row creation.
            legacy = JournalEntry(user=self.owner, title="Legacy remote URL", custom_poster_url=url)
            JournalEntry.objects.bulk_create([legacy])
            report = backfill_media_references()
            self.assertEqual(report["status"], "BLOCKED")
            self.assertIn("UNVERIFIED_REMOTE_BYTES", report["objects"][0]["issues"])
            self.assertFalse(legacy.media_references.exists())
            self.assertFalse(cleanup_media(media.pk))
            self.assertEqual(files, original)
            legacy.delete()
            self.assertEqual(backfill_media_references()["status"], "READY")
            self.assertEqual(self.client.delete(reverse("entry-detail", args=[source.pk])).status_code, 204)
            self.assertFalse(MediaObject.objects.filter(pk=media.pk).exists())
            self.assertEqual(files, {})
            self.assertEqual(deletes, [media.object_key])
            self.assertEqual(managed_usage_bytes(self.backend), 0)

    def test_process_crash_before_or_after_delete_is_recoverable_without_key_reuse(self):
        if connection.vendor != "postgresql":
            self.skipTest("Requires independently committed PostgreSQL crash boundaries")
        script = """
import json, os, sys, django
from django.conf import settings
settings.DATABASES['default'] = json.loads(os.environ['MEDIA_CRASH_TEST_DATABASE'])
django.setup()
from journal.media_references import cleanup_media
from site_config.media_storage.local import DynamicLocalBackend
original = DynamicLocalBackend.delete
def crash(self, key):
    if sys.argv[1] == 'before':
        os._exit(71)
    original(self, key)
    os._exit(72)
DynamicLocalBackend.delete = crash
cleanup_media(sys.argv[2])
raise SystemExit(73)
"""
        for stage, code in (("before", 71), ("after", 72)):
            with self.subTest(stage=stage):
                source, media, url = self.upload("Crash source")
                with patch("journal.media_references.cleanup_media", return_value=False):
                    source.delete()
                # Preserve the actual test database, authentication and options;
                # rebuilding a URL can lose passwords or use the original DB.
                environment = {**os.environ,
                    "MEDIA_CRASH_TEST_DATABASE": json.dumps(connection.settings_dict),
                    "MEDIA_LOCAL_STORAGE_ROOT": self.directory.name,
                }
                result = subprocess.run([sys.executable, "-B", "-c", script, stage, str(media.pk)],
                                        env=environment, capture_output=True, timeout=30)
                self.assertEqual(result.returncode, code, result.stderr.decode("utf-8", "replace"))
                media.refresh_from_db()
                self.assertEqual(media.lifecycle, MediaObject.Lifecycle.DELETING)
                self.assertEqual(DynamicLocalBackend(self.backend).exists(media.object_key), stage == "before")
                self.assertEqual(managed_usage_bytes(self.backend), media.size_bytes)
                self.alias(url, expected=400)
                with self.assertRaisesRegex(MediaStorageExhausted, "位置已被占用"):
                    StoragePoolService.create_media(media.object_key, b"forbidden-key-reuse")
                output = io.StringIO()
                call_command("reconcile_media_references", retry_cleanup=True, stdout=output)
                report = json.loads(output.getvalue())
                self.assertEqual(report["status"], "READY")
                self.assertEqual(report["cleanup_results"], [{"media_id": str(media.pk), "reclaimed": True}])
                self.assertFalse(MediaObject.objects.filter(pk=media.pk).exists())
                self.assertFalse(DynamicLocalBackend(self.backend).exists(media.object_key))
                self.assertEqual(managed_usage_bytes(self.backend), 0)
                print(json.dumps({"crash_boundary": stage, "actual_child_exit": result.returncode,
                                  "protected_bytes_before_retry": media.size_bytes,
                                  "physical_bytes_after_retry": 0, "result": "PASS"}))

    def test_nonposter_owner_proof_survives_last_reference_cleanup_failure(self):
        profile = UserSettings.objects.create(user=self.owner, avatar=poster_upload())
        media = StoragePoolService.resolve_reference(profile.avatar.name)
        # Model a pre-upgrade avatar with only its actual business association.
        MediaObject.objects.filter(pk=media.pk).update(upload_owner=None)
        alias = self.alias(profile.avatar.url)
        media.refresh_from_db()
        self.assertEqual(media.upload_owner_id, self.owner.pk)
        profile.avatar = None
        profile.save(update_fields=["avatar"])
        with patch.object(DynamicLocalBackend, "delete", side_effect=OSError("isolated failure")):
            self.clear(alias)
        media.refresh_from_db()
        self.assertEqual(media.lifecycle, MediaObject.Lifecycle.DELETE_FAILED)
        second = self.alias(media.public_url_snapshot)
        self.assertEqual(second.media_references.get().media_id, media.pk)
        self.assertFalse(cleanup_media(media.pk))
        self.assertTrue(DynamicLocalBackend(self.backend).exists(media.object_key))
        self.clear(second)
        self.assertFalse(MediaObject.objects.filter(pk=media.pk).exists())

    def test_avatar_save_failure_reclaims_physical_upload(self):
        from django.db.models.signals import post_save
        from .serializers_profile import UserSettingsSerializer

        def reject(sender, **kwargs):
            raise RuntimeError("isolated business save failure")

        post_save.connect(reject, sender=UserSettings, weak=False)
        try:
            serializer = UserSettingsSerializer(data={"avatar": poster_upload()})
            serializer.is_valid(raise_exception=True)
            with self.assertRaisesRegex(RuntimeError, "business save"):
                serializer.save(user=self.owner)
        finally:
            post_save.disconnect(reject, sender=UserSettings)
        self.assertFalse(UserSettings.objects.filter(user=self.owner).exists())
        self.assertFalse(MediaObject.objects.exists())
        self.assertFalse(MediaWriteReservation.objects.exclude(status="abandoned").exists())
        self.assertEqual(managed_usage_bytes(self.backend), 0)
        self.assertFalse([item for item in Path(self.directory.name).rglob("*") if item.is_file()])

    def test_column_related_failure_rolls_back_entire_upload(self):
        from .serializers_profile import ColumnSerializer

        entry = JournalEntry.objects.create(user=self.owner, title="Related")
        serializer = ColumnSerializer(data={"title": "Column", "body": "Body", "cover": poster_upload(), "entries": [entry.pk]})
        serializer.is_valid(raise_exception=True)
        related_manager = Column.entries.related_manager_cls
        with patch.object(related_manager, "set", side_effect=RuntimeError("isolated related failure")):
            with self.assertRaisesRegex(RuntimeError, "related failure"):
                serializer.save(author=self.owner)
        self.assertFalse(Column.objects.exists())
        self.assertFalse(MediaObject.objects.exists())
        self.assertEqual(managed_usage_bytes(self.backend), 0)
        self.assertFalse(MediaWriteReservation.objects.exclude(status="abandoned").exists())

    def test_staff_site_secret_failure_unwinds_outer_upload_transaction(self):
        from .serializers_site import StaffSiteSettingsSerializer

        site = SiteSettings.load()
        serializer = StaffSiteSettingsSerializer(site, data={"site_avatar": poster_upload(), "resend_api_key": "re_isolated_synthetic_key"}, partial=True)
        serializer.is_valid(raise_exception=True)
        with patch.object(SiteSettings, "set_resend_api_key", side_effect=RuntimeError("isolated secret failure")):
            with self.assertRaisesRegex(RuntimeError, "secret failure"):
                serializer.save()
        site.refresh_from_db()
        self.assertFalse(site.site_avatar)
        self.assertFalse(MediaObject.objects.exists())
        self.assertEqual(managed_usage_bytes(self.backend), 0)
        self.assertFalse(MediaWriteReservation.objects.exclude(status="abandoned").exists())

    def test_admin_save_related_failure_unwinds_its_outer_transaction(self):
        import time
        from accounts.models import UserSecurityProfile
        from .admin import JournalEntryAdmin
        from .security import _totp_at

        self.owner.is_staff = self.owner.is_superuser = True
        self.owner.set_password("IsolatedAdminFixture123!")
        self.owner.save(update_fields=["is_staff", "is_superuser", "password"])
        profile, _ = UserSecurityProfile.objects.get_or_create(user=self.owner)
        secret = "JBSWY3DPEHPK3PXP"
        profile.set_totp_secret(secret)
        profile.two_factor_enabled = True
        profile.save(update_fields=["totp_secret_encrypted", "two_factor_enabled", "updated_at"])
        client = APIClient()
        login = client.post(reverse("staff-login"), {
            "username": self.owner.username, "password": "IsolatedAdminFixture123!",
            "otp": _totp_at(secret, time.time()),
        }, format="json")
        self.assertEqual(login.status_code, 200, login.data)
        with patch.object(JournalEntryAdmin, "save_related", side_effect=RuntimeError("isolated admin failure")) as related:
            try:
                response = client.post(reverse("admin:journal_journalentry_add"), {
                    "user": self.owner.pk, "title": "Admin upload", "watch_status": "planned",
                    "visibility": "private", "tags": "[]", "tag_colors": "{}", "poster_file": poster_upload(),
                    "_save": "Save",
                }, format="multipart")
            except RuntimeError as error:
                self.assertIn("admin failure", str(error))
            else:
                form = response.context["adminform"].form if response.context and "adminform" in response.context else None
                self.fail(f"Admin did not reach save_related: status={response.status_code}, url={getattr(response, 'url', '')}, errors={form.errors if form else None}")
            self.assertEqual(related.call_count, 1)
        self.assertFalse(JournalEntry.objects.exists())
        self.assertFalse(MediaObject.objects.exists())
        self.assertEqual(managed_usage_bytes(self.backend), 0)
        self.assertFalse(MediaWriteReservation.objects.exclude(status="abandoned").exists())

    def test_holding_survives_source_clear_and_last_release_reclaims(self):
        source, media, url = self.upload()
        alias = self.alias(url)
        self.assertEqual(poster_usage(self.owner.pk).known_bytes, media.size_bytes * 2)
        self.assertEqual(managed_usage_bytes(self.backend), media.size_bytes)
        self.clear(source)
        media.refresh_from_db()
        self.assertTrue(DynamicLocalBackend(self.backend).exists(media.object_key))
        self.assertEqual(poster_usage(self.owner.pk).known_bytes, media.size_bytes)
        self.clear(alias)
        self.assertFalse(MediaObject.objects.filter(pk=media.pk).exists())
        self.assertFalse(DynamicLocalBackend(self.backend).exists(media.object_key))
        self.assertEqual(managed_usage_bytes(self.backend), 0)

    def test_retry_query_fragment_and_soft_delete_charge_each_business_slot_once(self):
        source, media, url = self.upload()
        decorated = url.replace("media.example.test", "MEDIA.EXAMPLE.TEST:443") + "?v=one#cover"
        alias = self.alias(decorated)
        for _ in range(2):
            response = self.client.patch(reverse("entry-detail", args=[alias.pk]), {"custom_poster_url": decorated}, format="json")
            self.assertEqual(response.status_code, 200)
        alias.deleted_at = timezone.now()
        alias.save(update_fields=["deleted_at"])
        alias.deleted_at = None
        alias.save(update_fields=["deleted_at"])
        alias.refresh_from_db()
        self.assertEqual(alias.custom_poster_url, decorated)
        self.assertEqual(alias.media_references.count(), 1)
        self.assertEqual(poster_usage(self.owner.pk).known_bytes, media.size_bytes * 2)
        independent_slots = JournalEntry.objects.create(user=self.owner, title="Two persisted fields", poster_file=source.poster_file.name, custom_poster_url=url)
        self.assertEqual(independent_slots.media_references.count(), 2)
        self.assertEqual(poster_usage(self.owner.pk).known_bytes, media.size_bytes * 4)

    def test_foreign_and_third_party_urls_never_acquire_holds_or_leak_object_state(self):
        source, media, url = self.upload()
        self.client.force_authenticate(self.other)
        foreign = self.alias(url)
        external = self.alias("https://third.example.test/image.webp")
        self.assertFalse(foreign.media_references.exists())
        self.assertFalse(external.media_references.exists())
        self.assertEqual(poster_usage(self.other.pk).known_bytes, 0)
        self.client.force_authenticate(self.owner)
        self.clear(source)
        self.assertFalse(MediaObject.objects.filter(pk=media.pk).exists())
        self.client.force_authenticate(self.other)
        missing = self.alias(url)
        self.assertFalse(missing.media_references.exists())

    def test_missing_corrupt_unknown_and_deleting_media_reject_new_owned_holds(self):
        source, media, url = self.upload()
        path = DynamicLocalBackend(self.backend).path_for(media.object_key)
        original = path.read_bytes()
        for variant in (b"x" * len(original), original[:-1], None):
            if variant is None:
                path.unlink()
            else:
                path.write_bytes(variant)
            response = self.alias(url, 400)
            self.assertEqual(response.data["code"], "media_hold_unavailable")
            self.assertEqual(set(response.data), {"code", "detail", "correlation_id"})
            self.assertEqual(JournalEntry.objects.count(), 1)
            path.write_bytes(original)
        media.size_bytes = 0
        media.save(update_fields=["size_bytes"])
        self.alias(url, 400)
        self.assertEqual(poster_usage(self.owner.pk).status, "UNKNOWN_USAGE")
        media.size_bytes = len(original)
        media.lifecycle = "deleting"
        media.save(update_fields=["size_bytes", "lifecycle"])
        self.alias(url, 400)
        self.assertEqual(source.media_references.count(), 1)

    def test_failed_cleanup_retains_usage_and_new_holding_blocks_retry(self):
        source, media, url = self.upload()
        with patch.object(DynamicLocalBackend, "delete", side_effect=OSError("synthetic delete failure")):
            self.clear(source)
        media.refresh_from_db()
        self.assertEqual(media.lifecycle, "delete_failed")
        self.assertEqual(managed_usage_bytes(self.backend), media.size_bytes)
        alias = self.alias(url)
        self.assertFalse(cleanup_media(media.pk))
        self.assertTrue(DynamicLocalBackend(self.backend).exists(media.object_key))
        self.clear(alias)
        self.assertFalse(MediaObject.objects.filter(pk=media.pk).exists())

    def test_other_existing_image_roles_protect_bytes_without_poster_charge(self):
        source, media, _url = self.upload()
        avatar = UserSettings.objects.create(user=self.owner, avatar=source.poster_file.name)
        self.clear(source)
        self.assertEqual(poster_usage(self.owner.pk).known_bytes, 0)
        self.assertTrue(DynamicLocalBackend(self.backend).exists(media.object_key))
        avatar.delete()
        self.assertFalse(MediaObject.objects.filter(pk=media.pk).exists())

    def test_explicit_service_owner_without_request_uses_same_admission(self):
        _source, media, url = self.upload()
        with override_settings(POSTER_STORAGE_QUOTA_BYTES=media.size_bytes):
            from .media_references import MediaAdmissionError

            with self.assertRaises(MediaAdmissionError):
                JournalEntryService(self.owner).create_from_fields(
                    {"title": "Service alias", "custom_poster_url": url},
                    serializer_class=JournalEntrySerializer,
                    allowed_fields={"title", "custom_poster_url"},
                )
        self.assertEqual(JournalEntry.objects.count(), 1)

    def test_outer_rollback_reclaims_upload_and_preserves_old_business_state(self):
        source, media, url = self.upload()
        before = DynamicLocalBackend(self.backend).open(media.object_key).read()
        with self.assertRaises(RuntimeError):
            with atomic_media_mutation():
                self.client.patch(reverse("entry-detail", args=[source.pk]), {"poster_file": poster_upload()}, format="multipart")
                raise RuntimeError("synthetic outer rollback")
        source.refresh_from_db()
        self.assertEqual(source.poster_file.name, media.reference_name)
        self.assertEqual(DynamicLocalBackend(self.backend).open(media.object_key).read(), before)
        self.assertEqual(MediaObject.objects.count(), 1)
        self.assertEqual(managed_usage_bytes(self.backend), media.size_bytes)
        self.assertFalse(MediaWriteReservation.objects.filter(status__in=["pending", "cleanup_ready", "cleanup_failed"]).exists())

    def test_rollback_cleanup_failure_is_durable_and_explicit_retry_is_idempotent(self):
        with patch.object(DynamicLocalBackend, "delete_owned_write", side_effect=OSError("synthetic retry needed")):
            with self.assertRaises(RuntimeError):
                with atomic_media_mutation():
                    self.upload()
                    raise RuntimeError("synthetic business rollback")
        receipt = MediaWriteReservation.objects.get()
        self.assertEqual(receipt.status, "cleanup_failed")
        self.assertFalse(MediaObject.objects.exists())
        self.assertEqual(managed_usage_bytes(self.backend), receipt.size_bytes)
        self.assertTrue(DynamicLocalBackend(self.backend).exists(receipt.object_key))
        call_command("reconcile_media_write_reservations", retry_cleanup=True, stdout=io.StringIO())
        call_command("reconcile_media_write_reservations", retry_cleanup=True, stdout=io.StringIO())
        receipt.refresh_from_db()
        self.assertEqual(receipt.status, "abandoned")
        self.assertEqual(managed_usage_bytes(self.backend), 0)
        self.assertFalse(DynamicLocalBackend(self.backend).exists(receipt.object_key))

    def test_failed_local_replace_tracks_its_exact_temporary_file(self):
        if connection.vendor != "postgresql":
            self.skipTest("Partial-write accounting across outer rollback requires independent PostgreSQL commits")
        with patch("site_config.media_storage.local.os.replace", side_effect=OSError("synthetic replace failed")), patch("site_config.media_storage.local.os.unlink", side_effect=OSError("synthetic cleanup failed")):
            response = self.client.post(reverse("entry-list"), {"title": "Failed upload", "poster_file": poster_upload()}, format="multipart")
        self.assertEqual(response.status_code, 507, response.data)
        receipt = MediaWriteReservation.objects.get()
        adapter = DynamicLocalBackend(self.backend)
        temporary = adapter.upload_temporary_path(receipt.object_key, receipt.pk)
        self.assertTrue(temporary.exists())
        self.assertEqual(receipt.status, "cleanup_failed")
        self.assertGreaterEqual(managed_usage_bytes(self.backend), temporary.stat().st_size)
        call_command("reconcile_media_write_reservations", retry_cleanup=True, stdout=io.StringIO())
        self.assertFalse(temporary.exists())
        self.assertEqual(managed_usage_bytes(self.backend), 0)

    def test_legacy_backfill_preserves_urls_soft_delete_and_over_quota_usage(self):
        source, media, url = self.upload()
        alias = self.alias(url)
        # A legacy fixture removes only the new relationship representation.
        with transaction.atomic():
            MediaObject.objects.filter(pk=media.pk).update(reference_inventory_complete=False, upload_owner=None, public_url_snapshot="", public_url_identity="")
            JournalMediaReference.objects.all().delete()
        alias.deleted_at = timezone.now()
        alias.save(update_fields=["deleted_at"])
        before = Path(self.directory.name).rglob("*.webp")
        before_hashes = {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in before}
        with override_settings(POSTER_STORAGE_QUOTA_BYTES=media.size_bytes):
            report = backfill_media_references(limit=1)
            self.assertEqual(report["status"], "INCOMPLETE")
            self.assertFalse(cleanup_media(media.pk))
            report = backfill_media_references(after_entry=report["next_after_entry"], limit=1)
            self.assertEqual(report["status"], "READY", json.dumps(report))
            report = backfill_media_references(limit=100)
            self.assertEqual(report["status"], "READY")
            self.assertEqual(poster_usage(self.owner.pk).known_bytes, media.size_bytes * 2)
            self.alias(url, 400)
            self.clear(source)
        alias.refresh_from_db()
        self.assertEqual(alias.custom_poster_url, url)
        self.assertIsNotNone(alias.deleted_at)
        self.assertEqual(before_hashes, {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in Path(self.directory.name).rglob("*.webp")})

    def test_legacy_origin_without_owner_evidence_is_quarantined(self):
        source, media, url = self.upload()
        alias = self.alias(url)
        with transaction.atomic():
            MediaObject.objects.filter(pk=media.pk).update(reference_inventory_complete=False, upload_owner=None)
            JournalMediaReference.objects.all().delete()
            JournalEntry.objects.filter(pk=source.pk).update(poster_file=None)
        before = alias.custom_poster_url
        report = backfill_media_references()
        self.assertEqual(report["status"], "BLOCKED")
        self.assertIn("OWNER_UNPROVEN", report["objects"][0]["issues"])
        self.assertFalse(alias.media_references.exists())
        media.refresh_from_db()
        self.assertFalse(media.reference_inventory_complete)
        self.assertIsNone(media.upload_owner_id)
        self.assertFalse(cleanup_media(media.pk))
        alias.refresh_from_db()
        self.assertEqual(alias.custom_poster_url, before)
        self.assertEqual(inspect_media_references()["status"], "BLOCKED")
