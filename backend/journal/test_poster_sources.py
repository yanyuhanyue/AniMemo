import base64
import tempfile

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from accounts.models import StaffProfile
from site_config.models import SiteSettings
from .models import JournalEntry


User = get_user_model()
PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


class PosterSourceTests(APITestCase):
    def setUp(self):
        self.media = tempfile.TemporaryDirectory()
        self.media_override = override_settings(MEDIA_ROOT=self.media.name)
        self.media_override.enable()
        self.user = User.objects.create_user(username="poster-user", password="test-password")
        self.admin = User.objects.create_user(username="poster-admin", password="test-password", is_staff=True)
        StaffProfile.objects.create(user=self.admin, role=StaffProfile.Role.ADMINISTRATOR)
        self.client.force_authenticate(self.user)

    def tearDown(self):
        self.media_override.disable()
        self.media.cleanup()

    def test_regular_user_cannot_save_an_untrusted_remote_poster(self):
        response = self.client.post(
            reverse("entry-list"),
            {"title": "不受信任封面", "poster_url": "https://example.org/poster.jpg"},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("poster_url", response.data)

    def test_trusted_remote_poster_is_kept_as_the_default_source(self):
        response = self.client.post(
            reverse("entry-list"),
            {
                "title": "Bangumi 封面",
                "poster_url": "https://lain.bgm.tv/pic/cover/l/test.jpg",
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["poster_source"], "default_url")
        self.assertEqual(response.data["poster"], "https://lain.bgm.tv/pic/cover/l/test.jpg")

    def test_user_upload_takes_priority_and_can_be_restored_to_default(self):
        poster = SimpleUploadedFile("poster.png", PNG_1X1, content_type="image/png")
        created = self.client.post(
            reverse("entry-list"),
            {
                "title": "本地上传封面",
                "poster_url": "https://lain.bgm.tv/pic/cover/l/fallback.jpg",
                "poster_file": poster,
            },
            format="multipart",
        )
        self.assertEqual(created.status_code, status.HTTP_201_CREATED)
        self.assertEqual(created.data["poster_source"], "upload")
        self.assertIn("/media/users/", created.data["poster"])

        entry = JournalEntry.objects.get(pk=created.data["id"])
        self.assertTrue(entry.poster_file)

        restored = self.client.patch(
            reverse("entry-detail", kwargs={"pk": entry.pk}),
            {"clear_custom_poster": True},
            format="json",
        )

        self.assertEqual(restored.status_code, status.HTTP_200_OK)
        self.assertEqual(restored.data["poster_source"], "default_url")
        self.assertEqual(restored.data["poster"], "https://lain.bgm.tv/pic/cover/l/fallback.jpg")
        entry.refresh_from_db()
        self.assertFalse(entry.poster_file)
        self.assertEqual(entry.custom_poster_url, "")

    def test_trusted_custom_url_overrides_the_default_without_replacing_it(self):
        entry = JournalEntry.objects.create(
            user=self.user,
            title="自定义网络封面",
            poster_url="https://lain.bgm.tv/pic/cover/l/default.jpg",
        )

        response = self.client.patch(
            reverse("entry-detail", kwargs={"pk": entry.pk}),
            {
                "custom_poster_url": "https://img.re-anime.cc/posters/custom.jpg",
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["poster_source"], "trusted_url")
        self.assertEqual(
            response.data["poster"],
            "https://img.re-anime.cc/posters/custom.jpg",
        )
        entry.refresh_from_db()
        self.assertEqual(entry.poster_url, "https://lain.bgm.tv/pic/cover/l/default.jpg")

    def test_trusted_custom_url_replaces_an_existing_upload(self):
        poster = SimpleUploadedFile("poster.png", PNG_1X1, content_type="image/png")
        created = self.client.post(
            reverse("entry-list"),
            {
                "title": "上传后改用可信 URL",
                "poster_url": "https://lain.bgm.tv/pic/cover/l/default.jpg",
                "poster_file": poster,
            },
            format="multipart",
        )
        self.assertEqual(created.status_code, status.HTTP_201_CREATED)

        response = self.client.patch(
            reverse("entry-detail", kwargs={"pk": created.data["id"]}),
            {"custom_poster_url": "https://img.re-anime.cc/posters/custom.webp"},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["poster_source"], "trusted_url")
        entry = JournalEntry.objects.get(pk=created.data["id"])
        self.assertFalse(entry.poster_file)
        self.assertEqual(entry.poster_url, "https://lain.bgm.tv/pic/cover/l/default.jpg")

    def test_admin_can_manage_the_trusted_poster_host_list(self):
        self.client.force_authenticate(self.admin)
        response = self.client.patch(
            reverse("staff-site-settings"),
            {"trusted_poster_hosts": ["cdn.example.com", "lain.bgm.tv"]},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["trusted_poster_hosts"], ["cdn.example.com", "lain.bgm.tv"])
        self.assertEqual(SiteSettings.load().trusted_poster_hosts, ["cdn.example.com", "lain.bgm.tv"])

    def test_admin_can_save_the_host_list_from_multipart_form_data(self):
        self.client.force_authenticate(self.admin)
        response = self.client.patch(
            reverse("staff-site-settings"),
            {"trusted_poster_hosts": '["cdn.example.com", "lain.bgm.tv"]'},
            format="multipart",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["trusted_poster_hosts"], ["cdn.example.com", "lain.bgm.tv"])

    def test_admin_cannot_add_wildcards_ports_or_ip_addresses(self):
        self.client.force_authenticate(self.admin)
        for host in ["*.example.com", "cdn.example.com:443", "127.0.0.1"]:
            response = self.client.patch(
                reverse("staff-site-settings"),
                {"trusted_poster_hosts": [host]},
                format="json",
            )
            self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST, host)
