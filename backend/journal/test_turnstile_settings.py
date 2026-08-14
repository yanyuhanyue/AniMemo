from django.contrib.auth import get_user_model
from django.urls import reverse
from rest_framework.test import APITestCase

from accounts.models import StaffProfile
from site_config.models import InstallationState, SiteSettings


User = get_user_model()


class StaffTurnstileSettingsTests(APITestCase):
    def setUp(self):
        installation = InstallationState.load()
        installation.status = InstallationState.Status.INITIALIZED
        installation.save(update_fields=["status", "updated_at"])
        self.admin = User.objects.create_user(username="turnstile-admin", password="test-password", is_staff=True)
        StaffProfile.objects.create(user=self.admin, role=StaffProfile.Role.ADMINISTRATOR)
        self.user = User.objects.create_user(username="turnstile-user", password="test-password")

    def test_public_settings_expose_only_runtime_safe_turnstile_config(self):
        site = SiteSettings.load()
        site.turnstile_enabled = True
        site.turnstile_site_key = "public-site-key"
        site.set_turnstile_secret("private-secret")
        site.save()

        response = self.client.get(reverse("site-settings"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["turnstile"], {"enabled": True, "site_key": "public-site-key"})
        self.assertNotIn("turnstile_secret", response.data)
        self.assertNotIn("turnstile_secret_encrypted", response.data)

    def test_staff_update_requires_complete_enabled_configuration(self):
        self.client.force_authenticate(self.admin)

        missing_key = self.client.patch(reverse("staff-site-settings"), {"turnstile_enabled": True}, format="json")
        self.assertEqual(missing_key.status_code, 400)

        missing_secret = self.client.patch(
            reverse("staff-site-settings"),
            {"turnstile_enabled": True, "turnstile_site_key": "public-site-key"},
            format="json",
        )
        self.assertEqual(missing_secret.status_code, 400)

    def test_staff_secret_is_write_only_and_audit_safe(self):
        self.client.force_authenticate(self.admin)

        response = self.client.patch(
            reverse("staff-site-settings"),
            {
                "turnstile_enabled": True,
                "turnstile_site_key": "public-site-key",
                "turnstile_secret": "private-secret-value",
            },
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data["turnstile_secret_configured"])
        self.assertTrue(response.data["turnstile_ready"])
        self.assertNotIn("turnstile_secret", response.data)
        self.assertNotIn("turnstile_secret_encrypted", response.data)
        site = SiteSettings.load()
        self.assertNotEqual(site.turnstile_secret_encrypted, "private-secret-value")
        self.assertNotIn("private-secret-value", str(response.data))

        audit = self.admin.admin_audit_logs.order_by("-id").first()
        self.assertIsNotNone(audit)
        audit_text = str(audit.before) + str(audit.after) + str(audit.metadata)
        self.assertNotIn("private-secret-value", audit_text)
        self.assertNotIn(site.turnstile_secret_encrypted, audit_text)

    def test_blank_secret_preserves_and_clear_requires_disabled_state(self):
        site = SiteSettings.load()
        site.turnstile_enabled = True
        site.turnstile_site_key = "public-site-key"
        site.set_turnstile_secret("private-secret-value")
        site.save()
        ciphertext = site.turnstile_secret_encrypted
        self.client.force_authenticate(self.admin)

        preserve = self.client.patch(
            reverse("staff-site-settings"),
            {"turnstile_enabled": True, "turnstile_site_key": "public-site-key", "turnstile_secret": ""},
            format="json",
        )
        self.assertEqual(preserve.status_code, 200)
        self.assertEqual(SiteSettings.load().turnstile_secret_encrypted, ciphertext)

        rejected = self.client.patch(
            reverse("staff-site-settings"),
            {"turnstile_enabled": True, "clear_turnstile_secret": True},
            format="json",
        )
        self.assertEqual(rejected.status_code, 400)

        cleared = self.client.patch(
            reverse("staff-site-settings"),
            {"turnstile_enabled": False, "clear_turnstile_secret": True},
            format="json",
        )
        self.assertEqual(cleared.status_code, 200)
        self.assertFalse(SiteSettings.load().turnstile_secret_configured)

    def test_regular_user_cannot_manage_turnstile(self):
        self.client.force_authenticate(self.user)
        response = self.client.patch(
            reverse("staff-site-settings"),
            {"turnstile_enabled": True, "turnstile_site_key": "public-site-key", "turnstile_secret": "private"},
            format="json",
        )
        self.assertEqual(response.status_code, 403)
