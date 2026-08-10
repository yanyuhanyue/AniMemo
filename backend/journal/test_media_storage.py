import tempfile
import io
import errno
import os
import requests
import stat
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from io import StringIO
from pathlib import Path
from collections import namedtuple
from unittest import skipUnless
from unittest.mock import Mock, call, patch

from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.contrib import admin
from django.core.management import call_command
from django.db import IntegrityError, close_old_connections, connection, transaction
from django.test import TestCase, TransactionTestCase, override_settings
from django.test import RequestFactory
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient
from PIL import Image

from accounts.models import StaffProfile
from accounts.models import User
from site_config.media_storage.common import MediaStorageExhausted, MediaStorageOffline, MediaStorageSetupRequired, UnsafeObjectKey
from site_config.media_storage.local import DynamicLocalBackend
from site_config.media_storage.pool import StoragePoolService
from site_config.media_storage.r2 import DynamicR2Backend
from site_config.media_storage.usage import (
    MAX_ANALYTICS_RESPONSE_BYTES,
    CloudflareAnalyticsAuthFailed,
    CloudflareAnalyticsInvalidResponse,
    CloudflareAnalyticsQueryFailed,
    CloudflareAnalyticsTimeout,
    effective_account_usage,
    effective_storage_usage,
    fetch_cloudflare_usage,
    managed_usage_bytes,
    refresh_cloudflare_usage,
)
from site_config.models import CloudflareR2Account, MediaObject, MediaStorageBackend, MediaStoragePoolSettings
from site_config.storage_units import BINARY_GIB_BYTES, DECIMAL_GB_BYTES
from .models import AdminAuditLog, JournalEntry
from .admin import CloudflareR2AccountAdmin, MediaObjectAdmin, MediaStorageBackendAdmin, MediaStoragePoolSettingsAdmin
from .serializers_entries import JournalEntrySerializer
from .serializers_storage import MediaStorageBackendSerializer, StoragePhysicalIdentityLocked


TEST_CREDENTIAL_KEY = "media-storage-test-credential-key"


def r2_backend(slug, priority, *, used=0, warning=8 * DECIMAL_GB_BYTES, limit=9 * DECIMAL_GB_BYTES, account=None):
    account = account or CloudflareR2Account.objects.create(account_id=f"{slug}-account", name=f"{slug} account")
    backend = MediaStorageBackend(
        slug=slug,
        name=slug,
        backend_type=MediaStorageBackend.BackendType.CLOUDFLARE_R2,
        priority=priority,
        warning_bytes=warning,
        write_limit_bytes=limit,
        bucket_name=f"{slug}-bucket",
        endpoint_url="https://account.r2.cloudflarestorage.com",
        public_base_url=f"https://{slug}.example.com",
        usage_payload_bytes=used,
        usage_refreshed_at=timezone.now(),
        cloudflare_account_ref=account,
    )
    backend.set_access_key_id(f"{slug}-access")
    backend.set_secret_access_key(f"{slug}-secret")
    backend.save()
    return backend


@override_settings(CREDENTIAL_ENCRYPTION_KEY=TEST_CREDENTIAL_KEY)
class MediaStorageCredentialApiTests(TestCase):
    def setUp(self):
        self.superuser = User.objects.create_superuser(username="storage-root", password="StrongPass123!")
        self.client = APIClient()
        self.client.force_authenticate(self.superuser)

    def test_credentials_are_encrypted_preserved_replaced_cleared_and_never_returned(self):
        created = self.client.post(reverse("staff-media-storage-list"), {
            "slug": "r2-primary",
            "name": "R2 Primary",
            "backend_type": "cloudflare_r2",
            "priority": 10,
            "warning_bytes": 8 * DECIMAL_GB_BYTES,
            "write_limit_bytes": 9 * DECIMAL_GB_BYTES,
            "bucket_name": "anime-journal-media",
            "endpoint_url": "https://account.r2.cloudflarestorage.com",
            "public_base_url": "https://media.example.com",
            "cloudflare_account_id": "primary-account",
            "access_key_id": "access-plain-value",
            "secret_access_key": "secret-plain-value",
            "analytics_token": "analytics-plain-value",
        }, format="json")
        self.assertEqual(created.status_code, 201, created.data)
        backend = MediaStorageBackend.objects.get(slug="r2-primary")
        self.assertNotIn("access-plain-value", backend.encrypted_access_key_id)
        self.assertNotIn("secret-plain-value", backend.encrypted_secret_access_key)
        self.assertEqual(backend.get_access_key_id(), "access-plain-value")
        self.assertEqual(backend.get_secret_access_key(), "secret-plain-value")
        response_text = str(created.data)
        self.assertNotIn("access-plain-value", response_text)
        self.assertNotIn("secret-plain-value", response_text)
        self.assertNotIn(backend.encrypted_secret_access_key, response_text)
        self.assertTrue(created.data["access_key_configured"])
        self.assertTrue(backend.cloudflare_account_ref.analytics_token_configured)

        preserved = self.client.patch(reverse("staff-media-storage-detail", kwargs={"pk": backend.pk}), {
            "name": "R2 Primary Updated",
            "access_key_id": "",
            "secret_access_key": "",
            "analytics_token": "",
        }, format="json")
        self.assertEqual(preserved.status_code, 200, preserved.data)
        backend.refresh_from_db()
        self.assertEqual(backend.get_secret_access_key(), "secret-plain-value")

        replaced = self.client.patch(reverse("staff-media-storage-detail", kwargs={"pk": backend.pk}), {
            "secret_access_key": "secret-replacement",
        }, format="json")
        self.assertEqual(replaced.status_code, 200, replaced.data)
        backend.refresh_from_db()
        self.assertEqual(backend.get_secret_access_key(), "secret-replacement")

        cleared = self.client.post(reverse("staff-media-storage-action", kwargs={"pk": backend.pk}), {
            "action": "clear-credentials",
            "fields": ["secret_access_key"],
        }, format="json")
        self.assertEqual(cleared.status_code, 200, cleared.data)
        backend.refresh_from_db()
        self.assertFalse(backend.secret_key_configured)

    def test_only_superuser_can_access_storage_admin(self):
        url = reverse("staff-media-storage-list")
        users = [User.objects.create_user(username="storage-normal", password="StrongPass123!")]
        for role in (StaffProfile.Role.REVIEWER, StaffProfile.Role.OPERATOR, StaffProfile.Role.ADMINISTRATOR):
            user = User.objects.create_user(username=f"storage-{role}", password="StrongPass123!", is_staff=True)
            StaffProfile.objects.create(user=user, role=role)
            users.append(user)
        for user in users:
            with self.subTest(user=user.username):
                client = APIClient()
                client.force_authenticate(user)
                self.assertEqual(client.get(url).status_code, 403)
        self.assertEqual(self.client.get(url).status_code, 200)

    def test_duplicate_r2_physical_identity_is_rejected(self):
        payload = {
            "slug": "duplicate-r2-a",
            "name": "Duplicate A",
            "backend_type": "cloudflare_r2",
            "priority": 10,
            "warning_bytes": 8_000_000_000,
            "write_limit_bytes": 9_000_000_000,
            "bucket_name": "same-bucket",
            "endpoint_url": "https://account.r2.cloudflarestorage.com",
            "public_base_url": "https://media-a.example.com",
            "cloudflare_account_id": "duplicate-account",
            "access_key_id": "access-a",
            "secret_access_key": "secret-a",
        }
        self.assertEqual(self.client.post(reverse("staff-media-storage-list"), payload, format="json").status_code, 201)
        duplicate = dict(payload, slug="duplicate-r2-b", name="Duplicate B", public_base_url="https://media-b.example.com")
        response = self.client.post(reverse("staff-media-storage-list"), duplicate, format="json")
        self.assertEqual(response.status_code, 400)
        first = MediaStorageBackend.objects.get(slug="duplicate-r2-a")
        with self.assertRaises(IntegrityError), transaction.atomic():
            MediaStorageBackend.objects.create(
                slug="duplicate-r2-db",
                name="Duplicate DB",
                backend_type=MediaStorageBackend.BackendType.CLOUDFLARE_R2,
                bucket_name=first.bucket_name,
                endpoint_url="https://other.r2.cloudflarestorage.com",
                public_base_url="https://other.example.com",
                cloudflare_account_ref=first.cloudflare_account_ref,
            )

    def test_duplicate_local_resolved_path_is_rejected(self):
        with tempfile.TemporaryDirectory() as root, override_settings(MEDIA_LOCAL_STORAGE_ROOT=root):
            payload = {
                "slug": "duplicate-local-a",
                "name": "Local A",
                "backend_type": "local",
                "priority": 10,
                "warning_bytes": 8_000_000_000,
                "write_limit_bytes": 9_000_000_000,
                "local_root": "same",
                "local_public_base_url": "https://media-a.example.com/local-media",
                "min_free_warning_bytes": 15 * BINARY_GIB_BYTES,
                "min_free_block_bytes": 10 * BINARY_GIB_BYTES,
            }
            self.assertEqual(self.client.post(reverse("staff-media-storage-list"), payload, format="json").status_code, 201)
            response = self.client.post(reverse("staff-media-storage-list"), dict(payload, slug="duplicate-local-b", name="Local B"), format="json")
            self.assertEqual(response.status_code, 400)
            with self.assertRaises(IntegrityError), transaction.atomic():
                MediaStorageBackend.objects.create(
                    slug="duplicate-local-db",
                    name="Local DB",
                    backend_type=MediaStorageBackend.BackendType.LOCAL,
                    local_root="same",
                    local_public_base_url="https://media-db.example.com/local-media",
                )

    def test_r2_physical_identity_is_locked_after_media_exists(self):
        backend = r2_backend("identity-r2", 10)
        MediaObject.objects.create(storage_backend=backend, object_key="identity/object.webp", size_bytes=1)
        url = reverse("staff-media-storage-detail", kwargs={"pk": backend.pk})
        second_account = CloudflareR2Account.objects.create(account_id="identity-second", name="Second")
        for payload in (
            {"bucket_name": "identity-r2-other"},
            {"endpoint_url": "https://other-account.r2.cloudflarestorage.com"},
            {"cloudflare_account_id": second_account.account_id},
            {"backend_type": "local", "local_root": "converted", "local_public_base_url": "https://local.example.com/media"},
        ):
            with self.subTest(payload=payload):
                response = self.client.patch(url, payload, format="json")
                self.assertEqual(response.status_code, 400, response.data)
                self.assertEqual(response.data["code"], "STORAGE_PHYSICAL_IDENTITY_LOCKED")

    def test_local_physical_identity_is_locked_after_media_exists(self):
        with tempfile.TemporaryDirectory() as root, override_settings(MEDIA_LOCAL_STORAGE_ROOT=root):
            backend = MediaStorageBackend.objects.create(
                slug="identity-local",
                name="Identity Local",
                backend_type=MediaStorageBackend.BackendType.LOCAL,
                local_root="first",
                local_public_base_url="https://local.example.com/media",
            )
            MediaObject.objects.create(storage_backend=backend, object_key="identity/local.webp", size_bytes=1)
            response = self.client.patch(
                reverse("staff-media-storage-detail", kwargs={"pk": backend.pk}),
                {"local_root": "second"},
                format="json",
            )
            self.assertEqual(response.status_code, 400, response.data)
            self.assertEqual(response.data["code"], "STORAGE_PHYSICAL_IDENTITY_LOCKED")

    def test_non_physical_settings_remain_editable_with_media(self):
        backend = r2_backend("identity-editable", 10)
        MediaObject.objects.create(storage_backend=backend, object_key="identity/editable.webp", size_bytes=1)
        response = self.client.patch(
            reverse("staff-media-storage-detail", kwargs={"pk": backend.pk}),
            {
                "priority": 77,
                "write_limit_bytes": 10_000_000_000,
                "secret_access_key": "rotated-secret",
                "analytics_token": "rotated-analytics",
                "public_base_url": "https://cdn.example.com",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        backend.refresh_from_db()
        self.assertEqual(backend.priority, 77)
        self.assertEqual(backend.write_limit_bytes, 10_000_000_000)
        self.assertEqual(backend.get_secret_access_key(), "rotated-secret")
        self.assertEqual(backend.cloudflare_account_ref.get_analytics_token(), "rotated-analytics")
        self.assertEqual(backend.public_base_url, "https://cdn.example.com")

    def test_empty_backend_physical_identity_remains_editable(self):
        backend = r2_backend("identity-empty", 10)
        next_account = CloudflareR2Account.objects.create(account_id="identity-empty-next", name="Next")
        response = self.client.patch(
            reverse("staff-media-storage-detail", kwargs={"pk": backend.pk}),
            {
                "bucket_name": "identity-empty-new-bucket",
                "endpoint_url": "https://identity-new.r2.cloudflarestorage.com",
                "cloudflare_account_id": next_account.account_id,
            },
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        backend.refresh_from_db()
        self.assertEqual(backend.bucket_name, "identity-empty-new-bucket")
        self.assertEqual(backend.cloudflare_account_ref_id, next_account.pk)

    def test_config_version_increments_for_each_locked_update(self):
        created = self.client.post(reverse("staff-media-storage-list"), {
            "slug": "versioned-r2",
            "name": "Versioned R2",
            "backend_type": "cloudflare_r2",
            "priority": 10,
            "warning_bytes": 8_000_000_000,
            "write_limit_bytes": 9_000_000_000,
            "bucket_name": "versioned-bucket",
            "endpoint_url": "https://versioned.r2.cloudflarestorage.com",
            "public_base_url": "https://versioned.example.com",
            "cloudflare_account_id": "versioned-account",
            "access_key_id": "access-versioned",
            "secret_access_key": "secret-versioned",
        }, format="json")
        backend_id = created.data["id"]
        first = self.client.patch(reverse("staff-media-storage-detail", kwargs={"pk": backend_id}), {"priority": 20}, format="json")
        second = self.client.patch(reverse("staff-media-storage-detail", kwargs={"pk": backend_id}), {"priority": 30}, format="json")
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.data["config_version"], 3)

    def test_storage_api_separates_health_state_from_roles(self):
        backend = r2_backend("role-warning", 10, used=8_500_000_000, warning=8_000_000_000, limit=9_000_000_000)
        StoragePoolService.set_preferred_backend(backend)
        payload = self.client.get(reverse("staff-media-storage-list")).data
        item = next(row for row in payload["results"] if row["id"] == backend.pk)
        self.assertEqual(item["state"]["status"], "WARNING")
        self.assertTrue(item["is_preferred"])
        self.assertTrue(item["is_effective"])

    @override_settings(STORAGES={
        "default": {"BACKEND": "site_config.media_storage.storage.StoragePoolStorage"},
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    })
    def test_media_upload_without_configured_storage_returns_setup_required(self):
        user = User.objects.create_user(username="storage-upload-user", password="StrongPass123!")
        client = APIClient()
        client.force_authenticate(user)
        output = io.BytesIO()
        Image.new("RGB", (32, 32), "#ff4f8b").save(output, format="PNG")
        upload = SimpleUploadedFile("poster.png", output.getvalue(), content_type="image/png")
        response = client.post(reverse("entry-list"), {"title": "No storage", "poster_file": upload}, format="multipart")
        self.assertEqual(response.status_code, 503, response.data)
        self.assertEqual(response.data["code"], "MEDIA_STORAGE_SETUP_REQUIRED")


@override_settings(CREDENTIAL_ENCRYPTION_KEY=TEST_CREDENTIAL_KEY)
class StoragePoolSelectionTests(TestCase):
    def setUp(self):
        cache.clear()
        DynamicR2Backend.clear_client_cache()

    def test_priority_failover_and_no_automatic_failback(self):
        limit = 1000
        first = r2_backend("r2-a", 10, used=1001, warning=800, limit=limit)
        second = r2_backend("r2-b", 20, used=100, warning=800, limit=limit)
        selected = StoragePoolService.select_write_backend()
        self.assertEqual(selected.pk, second.pk)

        first.usage_payload_bytes = 0
        first.save(update_fields=["usage_payload_bytes", "updated_at"])
        self.assertEqual(StoragePoolService.select_write_backend().pk, second.pk)

        StoragePoolService.set_preferred_backend(first)
        self.assertEqual(StoragePoolService.select_write_backend().pk, first.pk)

    def test_all_blocked_and_no_config_have_distinct_errors(self):
        with self.assertRaises(MediaStorageSetupRequired):
            StoragePoolService.select_write_backend()
        r2_backend("full-r2", 10, used=1000, warning=800, limit=900)
        with self.assertRaises(MediaStorageExhausted):
            StoragePoolService.select_write_backend()

    def test_local_backend_is_used_after_r2_backends_block(self):
        with tempfile.TemporaryDirectory() as root, override_settings(MEDIA_LOCAL_STORAGE_ROOT=root):
            r2_backend("full-a", 10, used=1000, warning=800, limit=900)
            r2_backend("full-b", 20, used=1000, warning=800, limit=900)
            local = MediaStorageBackend.objects.create(
                slug="local-server",
                name="Local Server",
                backend_type=MediaStorageBackend.BackendType.LOCAL,
                priority=100,
                warning_bytes=10_000,
                write_limit_bytes=20_000,
                local_public_base_url="https://local-media.example.com",
                min_free_warning_bytes=2,
                min_free_block_bytes=1,
            )
            self.assertEqual(StoragePoolService.select_write_backend().pk, local.pk)

    def test_incoming_size_skips_r2_that_would_cross_hard_limit(self):
        first = r2_backend("incoming-full", 10, used=895, warning=800, limit=900)
        second = r2_backend("incoming-next", 20, used=0, warning=800, limit=900)
        selected = StoragePoolService.select_write_backend(incoming_size_bytes=10)
        self.assertEqual(selected.pk, second.pk)
        self.assertFalse(StoragePoolService.state_for(first, incoming_size_bytes=10).writable)

    def test_exact_write_limit_boundary_is_allowed(self):
        backend = r2_backend("exact-boundary", 10, used=890, warning=800, limit=900)
        state = StoragePoolService.state_for(backend, incoming_size_bytes=10)
        self.assertTrue(state.writable)
        self.assertEqual(state.status, "WARNING")

    def test_decimal_gb_boundary_and_one_byte_overflow(self):
        backend = r2_backend(
            "decimal-boundary",
            10,
            used=8_900_000_000,
            warning=8_000_000_000,
            limit=9_000_000_000,
        )
        self.assertTrue(StoragePoolService.state_for(backend, incoming_size_bytes=100_000_000).writable)
        self.assertFalse(StoragePoolService.state_for(backend, incoming_size_bytes=100_000_001).writable)

    def test_shared_cloudflare_account_budget_blocks_all_buckets_in_account(self):
        account = CloudflareR2Account.objects.create(
            account_id="shared-account",
            name="Shared Account",
            warning_bytes=8_000_000_000,
            write_limit_bytes=9_000_000_000,
        )
        first = r2_backend("shared-a", 10, used=0, account=account)
        second = r2_backend("shared-b", 20, used=0, account=account)
        MediaObject.objects.create(storage_backend=first, object_key="shared/a", size_bytes=4_000_000_000)
        MediaObject.objects.create(storage_backend=second, object_key="shared/b", size_bytes=4_900_000_000)
        self.assertFalse(StoragePoolService.state_for(first, incoming_size_bytes=200_000_000).writable)
        self.assertFalse(StoragePoolService.state_for(second, incoming_size_bytes=200_000_000).writable)

    def test_different_cloudflare_accounts_fail_over_independently(self):
        account_a = CloudflareR2Account.objects.create(account_id="account-a", name="A", write_limit_bytes=9_000_000_000)
        account_b = CloudflareR2Account.objects.create(account_id="account-b", name="B", write_limit_bytes=9_000_000_000)
        first = r2_backend("account-a-full", 10, account=account_a)
        second = r2_backend("account-b-open", 20, account=account_b)
        MediaObject.objects.create(storage_backend=first, object_key="full", size_bytes=8_999_999_999)
        self.assertEqual(StoragePoolService.select_write_backend(incoming_size_bytes=2).pk, second.pk)

    def test_account_effective_usage_sums_each_bucket_maximum(self):
        account = CloudflareR2Account.objects.create(
            account_id="cross-bucket-account",
            name="Cross Bucket",
            write_limit_bytes=9_000_000_000,
        )
        first = r2_backend("cross-bucket-a", 10, used=1_000_000_000, limit=12_000_000_000, account=account)
        second = r2_backend("cross-bucket-b", 20, used=5_000_000_000, limit=12_000_000_000, account=account)
        MediaObject.objects.create(storage_backend=first, object_key="cross/a", size_bytes=5_000_000_000)
        MediaObject.objects.create(storage_backend=second, object_key="cross/b", size_bytes=1_000_000_000)
        first.accept_new_writes = False
        first.save(update_fields=["accept_new_writes", "updated_at"])
        self.assertEqual(effective_account_usage(account), 10_000_000_000)
        self.assertFalse(StoragePoolService.state_for(first).writable)
        self.assertFalse(StoragePoolService.state_for(second).writable)

    def test_account_incoming_boundary_and_local_fallback(self):
        account = CloudflareR2Account.objects.create(
            account_id="incoming-account",
            name="Incoming",
            write_limit_bytes=9_000_000_000,
        )
        first = r2_backend("incoming-account-a", 10, limit=12_000_000_000, account=account)
        second = r2_backend("incoming-account-b", 20, limit=12_000_000_000, account=account)
        MediaObject.objects.create(storage_backend=first, object_key="incoming/a", size_bytes=8_900_000_000)
        self.assertTrue(StoragePoolService.state_for(first, incoming_size_bytes=100_000_000).writable)
        self.assertFalse(StoragePoolService.state_for(second, incoming_size_bytes=100_000_001).writable)
        with tempfile.TemporaryDirectory() as root, override_settings(MEDIA_LOCAL_STORAGE_ROOT=root):
            local = MediaStorageBackend.objects.create(
                slug="incoming-local-fallback",
                name="Incoming Local",
                backend_type=MediaStorageBackend.BackendType.LOCAL,
                priority=30,
                warning_bytes=20_000_000_000,
                write_limit_bytes=30_000_000_000,
                local_public_base_url="https://local.example.com/media",
                min_free_warning_bytes=2,
                min_free_block_bytes=1,
            )
            self.assertEqual(StoragePoolService.select_write_backend(incoming_size_bytes=100_000_001).pk, local.pk)

    def test_local_reserve_guard_includes_incoming_size(self):
        with tempfile.TemporaryDirectory() as root, override_settings(MEDIA_LOCAL_STORAGE_ROOT=root):
            local = MediaStorageBackend.objects.create(
                slug="local-reserve",
                name="Local Reserve",
                backend_type=MediaStorageBackend.BackendType.LOCAL,
                priority=10,
                warning_bytes=800,
                write_limit_bytes=900,
                local_public_base_url="https://local.example.com/local-media",
                min_free_warning_bytes=101,
                min_free_block_bytes=100,
            )
            fallback = r2_backend("reserve-fallback", 20, used=0, warning=800, limit=900)
            Disk = namedtuple("Disk", "total used free")
            with patch.object(DynamicLocalBackend, "disk_usage", return_value=Disk(1000, 898, 102)):
                selected = StoragePoolService.select_write_backend(incoming_size_bytes=3)
                self.assertFalse(StoragePoolService.state_for(local, incoming_size_bytes=3).writable)
            self.assertEqual(selected.pk, fallback.pk)

    def test_local_enospc_is_converted_and_fails_over(self):
        with tempfile.TemporaryDirectory() as root, override_settings(MEDIA_LOCAL_STORAGE_ROOT=root):
            local = MediaStorageBackend.objects.create(
                slug="local-enospc",
                name="Local ENOSPC",
                backend_type=MediaStorageBackend.BackendType.LOCAL,
                priority=10,
                warning_bytes=800,
                write_limit_bytes=900,
                local_public_base_url="https://local.example.com/local-media",
                min_free_warning_bytes=2,
                min_free_block_bytes=1,
            )
            fallback = r2_backend("enospc-fallback", 20, used=0, warning=800, limit=900)
            r2_adapter = Mock()
            r2_adapter.write.return_value = None
            r2_adapter.delete.return_value = None

            def adapter_for(backend):
                return DynamicLocalBackend(backend) if backend.pk == local.pk else r2_adapter

            with patch.object(StoragePoolService, "adapter_for", side_effect=adapter_for), patch(
                "site_config.media_storage.local.tempfile.mkstemp",
                side_effect=OSError(errno.ENOSPC, "disk full"),
            ):
                media = StoragePoolService.create_media("users/1/enospc.webp", b"payload")
            self.assertEqual(media.storage_backend_id, fallback.pk)
            self.assertEqual(MediaStoragePoolSettings.load().preferred_write_backend_id, fallback.pk)

    def test_managed_media_bytes_are_the_strong_quota_source(self):
        backend = r2_backend("managed-r2", 10, used=880, warning=800, limit=900)
        media = MediaObject.objects.create(storage_backend=backend, object_key="managed/one.webp", size_bytes=910)
        self.assertEqual(managed_usage_bytes(backend), 910)
        self.assertEqual(effective_storage_usage(backend), 910)
        self.assertEqual(StoragePoolService.state_for(backend).status, "WRITE_BLOCKED")
        media.size_bytes = 850
        media.save(update_fields=["size_bytes"])
        backend.usage_payload_bytes = 0
        backend.save(update_fields=["usage_payload_bytes", "updated_at"])
        self.assertEqual(effective_storage_usage(backend), 850)
        self.assertEqual(StoragePoolService.state_for(backend).status, "WARNING")


@override_settings(CREDENTIAL_ENCRYPTION_KEY=TEST_CREDENTIAL_KEY)
class MediaIdentityAndRuntimeTests(TestCase):
    class FakeAdapter:
        def __init__(self, prefix):
            self.prefix = prefix
            self.writes = []
            self.deletes = []

        def write(self, key, content, *, content_type="application/octet-stream"):
            self.writes.append((key, bytes(content), content_type))

        def delete(self, key):
            self.deletes.append(key)

        def url(self, key):
            return f"https://{self.prefix}.example.com/{key}"

        def exists(self, key):
            return True

    def setUp(self):
        cache.clear()
        DynamicR2Backend.clear_client_cache()

    def test_historical_media_and_cross_storage_replace_keep_backend_identity(self):
        first = r2_backend("history-a", 10, used=0, warning=800, limit=900)
        second = r2_backend("history-b", 20, used=0, warning=800, limit=900)
        old = MediaObject.objects.create(storage_backend=first, object_key="users/1/old.webp", size_bytes=4)
        pool = MediaStoragePoolSettings.load()
        pool.preferred_write_backend = second
        pool.save(update_fields=["preferred_write_backend", "updated_at"])
        first_adapter = self.FakeAdapter("history-a")
        second_adapter = self.FakeAdapter("history-b")

        with patch.object(StoragePoolService, "adapter_for", side_effect=lambda backend: first_adapter if backend.pk == first.pk else second_adapter):
            self.assertEqual(StoragePoolService.url_for_reference(old.reference_name), "https://history-a.example.com/users/1/old.webp")
            new = StoragePoolService.create_media("users/1/new.webp", b"new", content_type="image/webp")
            self.assertEqual(new.storage_backend_id, second.pk)
            StoragePoolService.delete_media(old)

        self.assertEqual(second_adapter.writes[0][0], "users/1/new.webp")
        self.assertEqual(first_adapter.deletes, ["users/1/old.webp"])

    def test_write_connection_failure_fails_over_to_next_backend(self):
        first = r2_backend("offline-a", 10)
        second = r2_backend("online-b", 20)
        first_adapter = self.FakeAdapter("offline-a")
        second_adapter = self.FakeAdapter("online-b")

        def write_or_fail(backend):
            adapter = first_adapter if backend.pk == first.pk else second_adapter
            if backend.pk == first.pk:
                adapter.write = Mock(side_effect=MediaStorageOffline("offline"))
            return adapter

        with patch.object(StoragePoolService, "adapter_for", side_effect=write_or_fail):
            media = StoragePoolService.create_media("users/1/failover.webp", b"payload")
        self.assertEqual(media.storage_backend_id, second.pk)
        self.assertEqual(MediaStoragePoolSettings.load().preferred_write_backend_id, second.pk)

    def test_dynamic_r2_client_rebuilds_when_config_version_changes(self):
        backend = r2_backend("runtime-r2", 10)
        clients = [Mock(name="client-v1"), Mock(name="client-v2"), Mock(name="client-v3")]
        with patch("site_config.media_storage.r2.boto3.client", side_effect=clients) as factory:
            self.assertIs(DynamicR2Backend.client_for(backend), clients[0])
            serializer = MediaStorageBackendSerializer(backend, data={"secret_access_key": "runtime-r2-secret-v2"}, partial=True)
            self.assertTrue(serializer.is_valid(), serializer.errors)
            serializer.save()
            self.assertIs(DynamicR2Backend.client_for(backend), clients[1])
            serializer = MediaStorageBackendSerializer(backend, data={"secret_access_key": "runtime-r2-secret-v3"}, partial=True)
            self.assertTrue(serializer.is_valid(), serializer.errors)
            serializer.save()
            self.assertIs(DynamicR2Backend.client_for(backend), clients[2])
        self.assertEqual(factory.call_count, 3)
        self.assertEqual(factory.call_args_list[1].kwargs["aws_secret_access_key"], "runtime-r2-secret-v2")
        self.assertEqual(factory.call_args_list[2].kwargs["aws_secret_access_key"], "runtime-r2-secret-v3")
        client_config = factory.call_args_list[0].kwargs["config"]
        self.assertEqual(client_config.connect_timeout, 8)
        self.assertEqual(client_config.read_timeout, 25)
        self.assertEqual(client_config.retries["max_attempts"], 3)

    def test_backend_delete_is_rejected_while_media_is_referenced(self):
        backend = r2_backend("in-use-r2", 10)
        MediaObject.objects.create(storage_backend=backend, object_key="site/in-use.webp", size_bytes=1)
        root = User.objects.create_superuser(username="delete-storage-root", password="StrongPass123!")
        client = APIClient()
        client.force_authenticate(root)
        response = client.delete(reverse("staff-media-storage-detail", kwargs={"pk": backend.pk}))
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["code"], "STORAGE_IN_USE")

    @override_settings(STORAGES={
        "default": {"BACKEND": "site_config.media_storage.storage.StoragePoolStorage"},
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    })
    def test_model_save_failure_cleans_new_external_object(self):
        with tempfile.TemporaryDirectory() as root, override_settings(MEDIA_LOCAL_STORAGE_ROOT=root):
            local = MediaStorageBackend.objects.create(
                slug="rollback-local",
                name="Rollback Local",
                backend_type=MediaStorageBackend.BackendType.LOCAL,
                priority=10,
                warning_bytes=10_000,
                write_limit_bytes=20_000,
                local_public_base_url="https://local.example.com/local-media",
                min_free_warning_bytes=2,
                min_free_block_bytes=1,
            )
            StoragePoolService.set_preferred_backend(local)
            user = User.objects.create_user(username="rollback-media-user", password="StrongPass123!")
            output = io.BytesIO()
            Image.new("RGB", (32, 32), "#ff4f8b").save(output, format="PNG")
            upload = SimpleUploadedFile("poster.png", output.getvalue(), content_type="image/png")
            request = Mock(user=user)
            serializer = JournalEntrySerializer(data={"title": "Rollback media", "poster_file": upload}, context={"request": request})
            self.assertTrue(serializer.is_valid(), serializer.errors)
            with patch.object(JournalEntry, "_save_table", side_effect=RuntimeError("database write failed")):
                with self.assertRaises(RuntimeError):
                    serializer.save(user=user)
            self.assertFalse(MediaObject.objects.exists())
            self.assertEqual([path for path in Path(root).rglob("*") if path.is_file()], [])


@override_settings(CREDENTIAL_ENCRYPTION_KEY=TEST_CREDENTIAL_KEY)
class CloudflareAnalyticsObservabilityTests(TestCase):
    def setUp(self):
        self.superuser = User.objects.create_superuser(username="analytics-root", password="StrongPass123!")
        self.client = APIClient()
        self.client.force_authenticate(self.superuser)
        self.backend = r2_backend("analytics-observability", 10)
        self.secret = "super-secret-analytics-token"
        self.backend.cloudflare_account_ref.set_analytics_token(self.secret)
        self.backend.cloudflare_account_ref.save(update_fields=["encrypted_analytics_token", "updated_at"])
        self.backend.usage_payload_bytes = 120
        self.backend.usage_metadata_bytes = 30
        self.backend.usage_object_count = 4
        self.backend.usage_refreshed_at = timezone.now() - timedelta(hours=3)
        self.backend.save(update_fields=[
            "usage_payload_bytes", "usage_metadata_bytes", "usage_object_count",
            "usage_refreshed_at", "updated_at",
        ])

    @staticmethod
    def payload(groups):
        return {"data": {"viewer": {"accounts": [{"r2StorageAdaptiveGroups": groups}]}}}

    @staticmethod
    def response(payload=None, *, status_code=200, content=b"{}", json_error=None):
        response = Mock()
        response.status_code = status_code
        response.headers = {}
        response.content = content
        if status_code >= 400:
            response.raise_for_status.side_effect = requests.HTTPError(response=response)
        else:
            response.raise_for_status.return_value = None
        if json_error is not None:
            response.json.side_effect = json_error
        else:
            response.json.return_value = payload
        return response

    def assert_snapshot_unchanged(self):
        self.backend.refresh_from_db()
        self.assertEqual(self.backend.usage_payload_bytes, 120)
        self.assertEqual(self.backend.usage_metadata_bytes, 30)
        self.assertEqual(self.backend.usage_object_count, 4)
        self.assertLess(self.backend.usage_refreshed_at, timezone.now() - timedelta(hours=2))

    def test_exact_zero_metrics_are_valid(self):
        response = self.response(self.payload([{
            "dimensions": {"datetime": "2026-08-10T05:00:00Z"},
            "max": {"payloadSize": 0, "metadataSize": 0, "objectCount": 0},
        }]))
        with patch("site_config.media_storage.usage.requests.post", return_value=response):
            self.assertEqual(fetch_cloudflare_usage(self.backend), {
                "payload_bytes": 0,
                "metadata_bytes": 0,
                "object_count": 0,
            })

    def test_empty_groups_are_successful_no_data(self):
        with patch(
            "site_config.media_storage.usage.requests.post",
            return_value=self.response(self.payload([])),
        ):
            refreshed, metrics = refresh_cloudflare_usage(self.backend.pk)
        self.assertEqual(refreshed.pk, self.backend.pk)
        self.assertIsNone(metrics)
        self.assert_snapshot_unchanged()

    def test_401_and_403_are_classified_as_auth_failures(self):
        for status_code in (401, 403):
            with self.subTest(status_code=status_code):
                with patch(
                    "site_config.media_storage.usage.requests.post",
                    return_value=self.response(status_code=status_code),
                ):
                    with self.assertRaises(CloudflareAnalyticsAuthFailed) as raised:
                        fetch_cloudflare_usage(self.backend)
                self.assertEqual(raised.exception.code, "CLOUDFLARE_ANALYTICS_AUTH_FAILED")
                self.assertNotIn(self.secret, str(raised.exception))

    def test_timeout_is_classified_without_leaking_credentials(self):
        with patch(
            "site_config.media_storage.usage.requests.post",
            side_effect=requests.Timeout("upstream timed out"),
        ):
            with self.assertRaises(CloudflareAnalyticsTimeout) as raised:
                fetch_cloudflare_usage(self.backend)
        self.assertEqual(raised.exception.code, "CLOUDFLARE_ANALYTICS_TIMEOUT")
        self.assertNotIn(self.secret, str(raised.exception))

    def test_graphql_errors_are_query_failures(self):
        response = self.response({"errors": [{"message": f"rejected {self.secret}"}]})
        with patch("site_config.media_storage.usage.requests.post", return_value=response):
            with self.assertRaises(CloudflareAnalyticsQueryFailed) as raised:
                fetch_cloudflare_usage(self.backend)
        self.assertEqual(raised.exception.code, "CLOUDFLARE_ANALYTICS_QUERY_FAILED")
        self.assertNotIn(self.secret, str(raised.exception))

    def test_invalid_responses_share_the_safe_invalid_response_code(self):
        cases = {
            "non-json": self.response(json_error=ValueError("not json")),
            "invalid-shape": self.response({"data": {"viewer": {}}}),
            "missing-metric": self.response(self.payload([{
                "dimensions": {"datetime": "2026-08-10T05:00:00Z"},
                "max": {"payloadSize": 1, "metadataSize": 2},
            }])),
            "wrong-type": self.response(self.payload([{
                "dimensions": {"datetime": "2026-08-10T05:00:00Z"},
                "max": {"payloadSize": None, "metadataSize": 2, "objectCount": 3},
            }])),
            "oversized": self.response(
                self.payload([]),
                content=b"x" * (MAX_ANALYTICS_RESPONSE_BYTES + 1),
            ),
        }
        for name, response in cases.items():
            with self.subTest(name=name):
                with patch("site_config.media_storage.usage.requests.post", return_value=response):
                    with self.assertRaises(CloudflareAnalyticsInvalidResponse) as raised:
                        fetch_cloudflare_usage(self.backend)
                self.assertEqual(raised.exception.code, "CLOUDFLARE_ANALYTICS_INVALID_RESPONSE")
                self.assertNotIn(self.secret, str(raised.exception))

    def test_timeout_and_auth_failure_preserve_last_successful_snapshot(self):
        failures = [
            requests.Timeout("upstream timed out"),
            self.response(status_code=401),
        ]
        for failure in failures:
            with self.subTest(failure=failure.__class__.__name__):
                patcher = patch(
                    "site_config.media_storage.usage.requests.post",
                    side_effect=failure if isinstance(failure, Exception) else None,
                    return_value=None if isinstance(failure, Exception) else failure,
                )
                with patcher:
                    with self.assertRaises((CloudflareAnalyticsTimeout, CloudflareAnalyticsAuthFailed)):
                        refresh_cloudflare_usage(self.backend.pk)
                self.assert_snapshot_unchanged()

    def test_refresh_action_returns_updated_zero_snapshot(self):
        response = self.response(self.payload([{
            "dimensions": {"datetime": "2026-08-10T05:00:00Z"},
            "max": {"payloadSize": 0, "metadataSize": 0, "objectCount": 0},
        }]))
        with patch("site_config.media_storage.usage.requests.post", return_value=response):
            result = self.client.post(
                reverse("staff-media-storage-action", kwargs={"pk": self.backend.pk}),
                {"action": "refresh-usage"},
                format="json",
            )
        self.assertEqual(result.status_code, 200, result.data)
        self.assertEqual(result.data["refresh"], {
            "status": "UPDATED",
            "code": "CLOUDFLARE_ANALYTICS_UPDATED",
        })
        self.assertEqual(result.data["storage"]["usage"]["actual_bytes"], 0)

    def test_refresh_action_reports_no_data_without_erasing_snapshot(self):
        with patch(
            "site_config.media_storage.usage.requests.post",
            return_value=self.response(self.payload([])),
        ):
            result = self.client.post(
                reverse("staff-media-storage-action", kwargs={"pk": self.backend.pk}),
                {"action": "refresh-usage"},
                format="json",
            )
        self.assertEqual(result.status_code, 200, result.data)
        self.assertEqual(result.data["refresh"]["status"], "NO_DATA")
        self.assertEqual(result.data["refresh"]["code"], "CLOUDFLARE_ANALYTICS_NO_DATA")
        self.assertEqual(result.data["storage"]["usage"]["actual_bytes"], 150)
        self.assert_snapshot_unchanged()

    def test_refresh_action_returns_424_code_and_safe_last_known_storage(self):
        with patch(
            "site_config.media_storage.usage.requests.post",
            side_effect=requests.Timeout(f"timed out {self.secret}"),
        ):
            result = self.client.post(
                reverse("staff-media-storage-action", kwargs={"pk": self.backend.pk}),
                {"action": "refresh-usage"},
                format="json",
            )
        self.assertEqual(result.status_code, 424, result.data)
        self.assertEqual(result.data["code"], "CLOUDFLARE_ANALYTICS_TIMEOUT")
        self.assertEqual(result.data["refresh"], {
            "status": "FAILED",
            "code": "CLOUDFLARE_ANALYTICS_TIMEOUT",
        })
        self.assertEqual(result.data["storage"]["usage"]["actual_bytes"], 150)
        audit = AdminAuditLog.objects.filter(action="storage.usage_refreshed").latest("id")
        self.assertEqual(audit.metadata, {
            "ok": False,
            "status": "FAILED",
            "code": "CLOUDFLARE_ANALYTICS_TIMEOUT",
        })
        exposed = f"{result.data} {audit.metadata}"
        self.assertNotIn(self.secret, exposed)
        self.assert_snapshot_unchanged()

    def test_refresh_action_contract_covers_every_dependency_failure_class(self):
        cases = [
            ("401", self.response(status_code=401), None, "CLOUDFLARE_ANALYTICS_AUTH_FAILED"),
            ("403", self.response(status_code=403), None, "CLOUDFLARE_ANALYTICS_AUTH_FAILED"),
            ("timeout", None, requests.Timeout("upstream timeout"), "CLOUDFLARE_ANALYTICS_TIMEOUT"),
            ("graphql", self.response({"errors": [{"message": f"rejected {self.secret}"}]}), None, "CLOUDFLARE_ANALYTICS_QUERY_FAILED"),
            ("non-json", self.response(json_error=ValueError("not json")), None, "CLOUDFLARE_ANALYTICS_INVALID_RESPONSE"),
            ("invalid-shape", self.response({"data": {"viewer": {}}}), None, "CLOUDFLARE_ANALYTICS_INVALID_RESPONSE"),
            ("missing-metric", self.response(self.payload([{
                "dimensions": {"datetime": "2026-08-10T05:00:00Z"},
                "max": {"payloadSize": 1, "metadataSize": 2},
            }])), None, "CLOUDFLARE_ANALYTICS_INVALID_RESPONSE"),
            ("oversized", self.response(self.payload([]), content=b"x" * (MAX_ANALYTICS_RESPONSE_BYTES + 1)), None, "CLOUDFLARE_ANALYTICS_INVALID_RESPONSE"),
        ]
        for name, response, exception, expected_code in cases:
            with self.subTest(name=name):
                patch_kwargs = {"side_effect": exception} if exception else {"return_value": response}
                with patch("site_config.media_storage.usage.requests.post", **patch_kwargs):
                    result = self.client.post(
                        reverse("staff-media-storage-action", kwargs={"pk": self.backend.pk}),
                        {"action": "refresh-usage"},
                        format="json",
                    )
                self.assertEqual(result.status_code, 424, result.data)
                self.assertEqual(result.data["code"], expected_code)
                self.assertEqual(result.data["refresh"], {"status": "FAILED", "code": expected_code})
                self.assertEqual(result.data["storage"]["usage"]["actual_bytes"], 150)
                audit = AdminAuditLog.objects.filter(action="storage.usage_refreshed").latest("id")
                self.assertEqual(audit.metadata["code"], expected_code)
                self.assertNotIn(self.secret, f"{result.data} {audit.metadata}")
                self.assert_snapshot_unchanged()


@override_settings(CREDENTIAL_ENCRYPTION_KEY=TEST_CREDENTIAL_KEY)
class StorageUsageAndLocalSafetyTests(TestCase):
    def test_refresh_usage_command_reports_skipped_without_secrets(self):
        r2_backend("command-skip", 10)
        output = StringIO()
        call_command("refresh_media_storage_usage", stdout=output)
        self.assertIn("success=0 failed=0 skipped=1", output.getvalue())

    def test_cloudflare_usage_parser(self):
        backend = r2_backend("analytics-r2", 10)
        backend.cloudflare_account_ref.set_analytics_token("analytics-token")
        backend.cloudflare_account_ref.save(update_fields=["encrypted_analytics_token", "updated_at"])
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"data": {"viewer": {"accounts": [{"r2StorageAdaptiveGroups": [
            {"dimensions": {"datetime": "2026-08-07T13:00:00Z"}, "max": {"payloadSize": 99, "metadataSize": 9, "objectCount": 8}},
            {"dimensions": {"datetime": "2026-08-07T14:00:00Z"}, "max": {"payloadSize": 12, "metadataSize": 3, "objectCount": 4}},
        ]}]}}}
        with patch("site_config.media_storage.usage.requests.post", return_value=response) as post:
            self.assertEqual(fetch_cloudflare_usage(backend), {"payload_bytes": 12, "metadata_bytes": 3, "object_count": 4})
        request_json = post.call_args.kwargs["json"]
        self.assertIn("r2StorageAdaptiveGroups", request_json["query"])
        self.assertIn("datetime_DESC", request_json["query"])
        self.assertEqual(request_json["variables"]["bucketName"], backend.bucket_name)

    def test_cloudflare_graphql_error_is_safe_and_redacted(self):
        backend = r2_backend("analytics-error", 10)
        backend.cloudflare_account_ref.set_analytics_token("super-secret-analytics-token")
        backend.cloudflare_account_ref.save(update_fields=["encrypted_analytics_token", "updated_at"])
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"errors": [{"message": "schema rejected"}]}
        with patch("site_config.media_storage.usage.requests.post", return_value=response):
            with self.assertRaises(ValueError) as raised:
                fetch_cloudflare_usage(backend)
        self.assertEqual(raised.exception.code, "CLOUDFLARE_ANALYTICS_QUERY_FAILED")
        self.assertNotIn("super-secret-analytics-token", str(raised.exception))

    def test_local_disk_guard_and_traversal_protection(self):
        with tempfile.TemporaryDirectory() as root, override_settings(MEDIA_LOCAL_STORAGE_ROOT=root):
            backend = MediaStorageBackend.objects.create(
                slug="guard-local",
                name="Guard Local",
                backend_type=MediaStorageBackend.BackendType.LOCAL,
                priority=100,
                warning_bytes=1000,
                write_limit_bytes=2000,
                local_public_base_url="https://local.example.com",
                min_free_warning_bytes=15,
                min_free_block_bytes=10,
            )
            Disk = namedtuple("Disk", "total used free")
            with patch.object(DynamicLocalBackend, "disk_usage", return_value=Disk(100, 92, 8)):
                self.assertEqual(StoragePoolService.state_for(backend).status, "WRITE_BLOCKED")
            with self.assertRaises(UnsafeObjectKey):
                DynamicLocalBackend(backend).path_for("../../etc/passwd")

    def test_local_subpath_maps_to_filesystem_and_public_url(self):
        with tempfile.TemporaryDirectory() as root, override_settings(MEDIA_LOCAL_STORAGE_ROOT=root):
            backend = MediaStorageBackend.objects.create(
                slug="mapped-local",
                name="Mapped Local",
                backend_type=MediaStorageBackend.BackendType.LOCAL,
                priority=100,
                warning_bytes=1000,
                write_limit_bytes=2000,
                local_root="secondary",
                local_public_base_url="https://local.example.com/local-media",
                min_free_warning_bytes=15,
                min_free_block_bytes=10,
            )
            adapter = DynamicLocalBackend(backend)
            self.assertEqual(adapter.root(), Path(root).resolve() / "secondary")
            self.assertEqual(adapter.url("users/1/image.webp"), "https://local.example.com/local-media/secondary/users/1/image.webp")
            backend.local_root = "C:/outside"
            with self.assertRaises(UnsafeObjectKey):
                adapter.root()

    def test_local_write_applies_public_file_and_directory_modes(self):
        with tempfile.TemporaryDirectory() as root, override_settings(MEDIA_LOCAL_STORAGE_ROOT=root):
            backend = MediaStorageBackend.objects.create(
                slug="mode-local",
                name="Mode Local",
                backend_type=MediaStorageBackend.BackendType.LOCAL,
                priority=100,
                warning_bytes=10_000,
                write_limit_bytes=20_000,
                local_root="public",
                local_public_base_url="https://local.example.com/local-media",
                min_free_warning_bytes=15,
                min_free_block_bytes=10,
            )
            adapter = DynamicLocalBackend(backend)
            storage_root = Path(root).resolve() / "public"
            target = storage_root / "users" / "1" / "image.webp"

            with patch("site_config.media_storage.local.os.chmod", wraps=os.chmod) as chmod:
                adapter.write("users/1/image.webp", b"payload", content_type="image/webp")

            self.assertEqual(target.read_bytes(), b"payload")
            self.assertIn(call(storage_root, 0o755), chmod.call_args_list)
            self.assertIn(call(target.parent, 0o755), chmod.call_args_list)
            self.assertIn(call(target, 0o644), chmod.call_args_list)

    @skipUnless(os.name == "posix", "Exact POSIX media modes require a POSIX filesystem.")
    def test_local_write_and_replace_have_exact_posix_modes(self):
        with tempfile.TemporaryDirectory() as root, override_settings(MEDIA_LOCAL_STORAGE_ROOT=root):
            os.chmod(root, 0o700)
            backend = MediaStorageBackend.objects.create(
                slug="posix-mode-local",
                name="POSIX Mode Local",
                backend_type=MediaStorageBackend.BackendType.LOCAL,
                priority=100,
                warning_bytes=10_000,
                write_limit_bytes=20_000,
                local_root="public",
                local_public_base_url="https://local.example.com/local-media",
                min_free_warning_bytes=15,
                min_free_block_bytes=10,
            )
            adapter = DynamicLocalBackend(backend)
            adapter.write("users/1/image.webp", b"first", content_type="image/webp")
            adapter.write("users/1/image.webp", b"replacement", content_type="image/webp")

            storage_root = adapter.root()
            target = storage_root / "users" / "1" / "image.webp"
            self.assertEqual(stat.S_IMODE(storage_root.stat().st_mode), 0o755)
            self.assertEqual(stat.S_IMODE(target.parent.stat().st_mode), 0o755)
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o644)
            self.assertEqual(target.read_bytes(), b"replacement")


class StorageAdminReadOnlyTests(TestCase):
    def test_storage_models_cannot_be_mutated_from_django_admin(self):
        request = RequestFactory().get("/")
        admin_pairs = (
            (MediaObjectAdmin, MediaObject),
            (MediaStorageBackendAdmin, MediaStorageBackend),
            (MediaStoragePoolSettingsAdmin, MediaStoragePoolSettings),
            (CloudflareR2AccountAdmin, CloudflareR2Account),
        )
        for admin_class, model in admin_pairs:
            admin_instance = admin_class(model, admin.site)
            with self.subTest(admin_class=admin_class.__name__):
                self.assertFalse(admin_instance.has_add_permission(request))
                self.assertFalse(admin_instance.has_change_permission(request))
                self.assertFalse(admin_instance.has_delete_permission(request))
                self.assertNotIn("delete_selected", admin_instance.get_actions(request))


@skipUnless(connection.vendor == "postgresql", "PostgreSQL is required for row-lock concurrency semantics.")
@override_settings(CREDENTIAL_ENCRYPTION_KEY=TEST_CREDENTIAL_KEY)
class StoragePostgreSQLConcurrencyTests(TransactionTestCase):
    reset_sequences = True

    class BlockingAdapter:
        def __init__(self, backend, write_started, release_write, observed_identity):
            self.backend = backend
            self.write_started = write_started
            self.release_write = release_write
            self.observed_identity = observed_identity

        def write(self, _key, _content, *, content_type="application/octet-stream"):
            self.observed_identity.append(self.backend.physical_identity())
            self.write_started.set()
            if not self.release_write.wait(timeout=10):
                raise TimeoutError("Timed out waiting to finish the storage write.")

        def delete(self, _key):
            return None

    @staticmethod
    def _upload(key):
        close_old_connections()
        try:
            return StoragePoolService.create_media(key, b"payload", content_type="image/webp")
        finally:
            close_old_connections()

    def test_first_r2_upload_serializes_physical_identity_update(self):
        backend = r2_backend("first-upload-r2", 10)
        write_started = threading.Event()
        release_write = threading.Event()
        update_started = threading.Event()
        observed_identity = []

        def update_bucket():
            close_old_connections()
            try:
                instance = MediaStorageBackend.objects.get(pk=backend.pk)
                serializer = MediaStorageBackendSerializer(
                    instance,
                    data={"bucket_name": "changed-during-upload"},
                    partial=True,
                )
                serializer.is_valid(raise_exception=True)
                update_started.set()
                try:
                    serializer.save()
                except StoragePhysicalIdentityLocked:
                    return "locked"
                return "updated"
            finally:
                close_old_connections()

        adapter_factory = lambda candidate: self.BlockingAdapter(candidate, write_started, release_write, observed_identity)
        with patch.object(StoragePoolService, "adapter_for", side_effect=adapter_factory):
            with ThreadPoolExecutor(max_workers=2) as executor:
                upload = executor.submit(self._upload, "race/r2.webp")
                self.assertTrue(write_started.wait(timeout=10))
                update = executor.submit(update_bucket)
                self.assertTrue(update_started.wait(timeout=10))
                time.sleep(0.2)
                self.assertFalse(update.done(), "Backend update did not wait for the upload row lock.")
                release_write.set()
                media = upload.result(timeout=20)
                result = update.result(timeout=20)

        backend.refresh_from_db()
        self.assertEqual(result, "locked")
        self.assertEqual(backend.bucket_name, "first-upload-r2-bucket")
        self.assertEqual(media.storage_backend_id, backend.pk)
        self.assertEqual(observed_identity, [backend.physical_identity()])

    def test_first_local_upload_serializes_root_update(self):
        with tempfile.TemporaryDirectory() as root, override_settings(MEDIA_LOCAL_STORAGE_ROOT=root):
            backend = MediaStorageBackend.objects.create(
                slug="first-upload-local",
                name="First Upload Local",
                backend_type=MediaStorageBackend.BackendType.LOCAL,
                priority=10,
                warning_bytes=10_000,
                write_limit_bytes=20_000,
                local_root="primary",
                local_public_base_url="https://local.example.com/local-media",
                min_free_warning_bytes=15,
                min_free_block_bytes=10,
            )
            write_started = threading.Event()
            release_write = threading.Event()
            update_started = threading.Event()
            observed_identity = []

            def update_root():
                close_old_connections()
                try:
                    instance = MediaStorageBackend.objects.get(pk=backend.pk)
                    serializer = MediaStorageBackendSerializer(instance, data={"local_root": "secondary"}, partial=True)
                    serializer.is_valid(raise_exception=True)
                    update_started.set()
                    try:
                        serializer.save()
                    except StoragePhysicalIdentityLocked:
                        return "locked"
                    return "updated"
                finally:
                    close_old_connections()

            adapter_factory = lambda candidate: self.BlockingAdapter(candidate, write_started, release_write, observed_identity)
            with patch.object(StoragePoolService, "adapter_for", side_effect=adapter_factory):
                with ThreadPoolExecutor(max_workers=2) as executor:
                    upload = executor.submit(self._upload, "race/local.webp")
                    self.assertTrue(write_started.wait(timeout=10))
                    update = executor.submit(update_root)
                    self.assertTrue(update_started.wait(timeout=10))
                    time.sleep(0.2)
                    self.assertFalse(update.done(), "Local root update did not wait for the upload row lock.")
                    release_write.set()
                    media = upload.result(timeout=20)
                    result = update.result(timeout=20)

            backend.refresh_from_db()
            self.assertEqual(result, "locked")
            self.assertEqual(backend.local_root, "primary")
            self.assertEqual(media.storage_backend_id, backend.pk)
            self.assertEqual(observed_identity, [backend.physical_identity()])

    def test_first_upload_serializes_backend_delete(self):
        backend = r2_backend("first-upload-delete", 10)
        superuser = User.objects.create_superuser(username="upload-delete-root", password="StrongPass123!")
        write_started = threading.Event()
        release_write = threading.Event()
        delete_started = threading.Event()
        observed_identity = []

        def delete_backend():
            close_old_connections()
            try:
                client = APIClient()
                client.force_authenticate(User.objects.get(pk=superuser.pk))
                delete_started.set()
                response = client.delete(reverse("staff-media-storage-detail", kwargs={"pk": backend.pk}))
                return response.status_code, response.data.get("code")
            finally:
                close_old_connections()

        adapter_factory = lambda candidate: self.BlockingAdapter(candidate, write_started, release_write, observed_identity)
        with patch.object(StoragePoolService, "adapter_for", side_effect=adapter_factory):
            with ThreadPoolExecutor(max_workers=2) as executor:
                upload = executor.submit(self._upload, "race/delete.webp")
                self.assertTrue(write_started.wait(timeout=10))
                deletion = executor.submit(delete_backend)
                self.assertTrue(delete_started.wait(timeout=10))
                time.sleep(0.2)
                self.assertFalse(deletion.done(), "Backend delete did not wait for the upload row lock.")
                release_write.set()
                media = upload.result(timeout=20)
                delete_status, delete_code = deletion.result(timeout=20)

        self.assertTrue(MediaStorageBackend.objects.filter(pk=backend.pk).exists())
        self.assertEqual(media.storage_backend_id, backend.pk)
        self.assertEqual(delete_status, 409)
        self.assertEqual(delete_code, "STORAGE_IN_USE")

    def test_concurrent_updates_preserve_both_changes_and_refresh_worker_client(self):
        backend = r2_backend("postgres-concurrency", 10)
        first_client = Mock(name="client-v1")
        refreshed_client = Mock(name="client-v3")
        barrier = threading.Barrier(3)

        def update_backend(payload):
            close_old_connections()
            try:
                instance = MediaStorageBackend.objects.get(pk=backend.pk)
                serializer = MediaStorageBackendSerializer(instance, data=payload, partial=True)
                serializer.is_valid(raise_exception=True)
                barrier.wait(timeout=10)
                return serializer.save().config_version
            finally:
                close_old_connections()

        with patch("site_config.media_storage.r2.boto3.client", side_effect=[first_client, refreshed_client]) as factory:
            self.assertIs(DynamicR2Backend.client_for(backend), first_client)
            with ThreadPoolExecutor(max_workers=2) as executor:
                priority_update = executor.submit(update_backend, {"priority": 321})
                warning_update = executor.submit(update_backend, {"warning_bytes": 7_500_000_000})
                barrier.wait(timeout=10)
                versions = sorted((priority_update.result(timeout=20), warning_update.result(timeout=20)))

            backend.refresh_from_db()
            self.assertEqual(versions, [2, 3])
            self.assertEqual(backend.config_version, 3)
            self.assertEqual(backend.priority, 321)
            self.assertEqual(backend.warning_bytes, 7_500_000_000)
            self.assertIs(DynamicR2Backend.client_for(backend), refreshed_client)
            self.assertEqual(factory.call_count, 2)
