from unittest.mock import Mock, patch

from accounts.models import StaffProfile
from django.contrib.auth import get_user_model
from django.test import override_settings
from django.urls import reverse
from rest_framework.test import APITestCase
from site_config.models import InstallationState, SiteSettings

from journal.emails import EmailDeliveryError
from journal.models import AdminAuditLog

User = get_user_model()


@override_settings(RESEND_API_KEY="", RESEND_FROM_EMAIL="AniMemo <env@example.com>")
class StaffEmailSettingsTests(APITestCase):
    def setUp(self):
        installation = InstallationState.load()
        installation.status = InstallationState.Status.INITIALIZED
        installation.save(update_fields=["status", "updated_at"])
        self.admin = User.objects.create_user(
            username="email-admin",
            password="test-password",
            is_staff=True,
        )
        StaffProfile.objects.create(user=self.admin, role=StaffProfile.Role.ADMINISTRATOR)
        self.user = User.objects.create_user(
            username="email-user",
            password="test-password",
        )

    def test_admin_can_save_encrypted_resend_configuration(self):
        self.client.force_authenticate(self.admin)

        response = self.client.patch(
            reverse("staff-site-settings"),
            {
                "email_delivery_enabled": True,
                "email_sender_name": "Anime Mail",
                "email_sender_address": "noreply@example.com",
                "resend_api_key": "re_test-secret-value",
            },
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data["resend_api_key_configured"])
        self.assertEqual(response.data["resend_api_key_source"], "database")
        self.assertEqual(response.data["effective_email_from"], "Anime Mail <noreply@example.com>")
        self.assertNotIn("resend_api_key", response.data)
        settings_obj = SiteSettings.load()
        self.assertNotEqual(settings_obj.resend_api_key_encrypted, "re_test-secret-value")
        self.assertEqual(settings_obj.get_resend_api_key(), "re_test-secret-value")

    def test_public_settings_never_expose_email_configuration(self):
        settings_obj = SiteSettings.load()
        settings_obj.set_resend_api_key("re_private")
        settings_obj.save()

        response = self.client.get(reverse("site-settings"))

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("resend_api_key_configured", response.data)
        self.assertNotIn("email_sender_address", response.data)

    @patch("journal.emails.requests.post")
    def test_admin_can_send_test_email(self, post):
        settings_obj = SiteSettings.load()
        settings_obj.email_sender_name = "Anime Mail"
        settings_obj.email_sender_address = "noreply@example.com"
        settings_obj.set_resend_api_key("re_test-secret")
        settings_obj.save()
        provider_response = Mock()
        provider_response.raise_for_status.return_value = None
        provider_response.json.return_value = {"id": "email_123"}
        post.return_value = provider_response
        self.client.force_authenticate(self.admin)

        response = self.client.post(
            reverse("staff-test-email"),
            {"email": "recipient@example.com"},
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["provider_id"], "email_123")
        self.assertEqual(post.call_args.kwargs["json"]["from"], "Anime Mail <noreply@example.com>")
        self.assertEqual(post.call_args.kwargs["json"]["to"], ["recipient@example.com"])
        audit = AdminAuditLog.objects.get(action="settings.test_email")
        self.assertNotIn("recipient@example.com", str(audit.metadata))
        self.assertNotIn("email_123", str(audit.metadata))
        self.assertIn("recipient_hash", audit.metadata)

    @patch("journal.public_views.send_transactional_email")
    def test_test_email_failure_never_exposes_provider_exception(self, send):
        marker = "EMAIL-PROVIDER-STACK-SENTINEL"
        settings_obj = SiteSettings.load()
        settings_obj.email_delivery_enabled = True
        settings_obj.set_resend_api_key("re_test-secret")
        settings_obj.save()
        send.side_effect = EmailDeliveryError(marker)
        self.client.force_authenticate(self.admin)

        response = self.client.post(
            reverse("staff-test-email"),
            {"email": "recipient@example.com"},
            format="json",
        )

        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.data["code"], "email_delivery_failed")
        self.assertIn("correlation_id", response.data)
        self.assertNotIn(marker, str(response.data))
        self.assertFalse(AdminAuditLog.objects.filter(action="settings.test_email").exists())

    @patch("journal.emails.requests.post")
    def test_disabled_email_delivery_blocks_test_send(self, post):
        settings_obj = SiteSettings.load()
        settings_obj.email_delivery_enabled = False
        settings_obj.save()
        self.client.force_authenticate(self.admin)

        response = self.client.post(
            reverse("staff-test-email"),
            {"email": "recipient@example.com"},
            format="json",
        )

        self.assertEqual(response.status_code, 409)
        post.assert_not_called()

    def test_regular_user_cannot_manage_email_settings(self):
        self.client.force_authenticate(self.user)

        response = self.client.post(
            reverse("staff-test-email"),
            {"email": "recipient@example.com"},
            format="json",
        )

        self.assertEqual(response.status_code, 403)

    def test_disabled_email_delivery_blocks_registration_without_creating_user(self):
        settings_obj = SiteSettings.load()
        settings_obj.registration_enabled = True
        settings_obj.email_delivery_enabled = False
        settings_obj.save()

        response = self.client.post(
            reverse("register-request"),
            {
                "email": "new-user@example.com",
                "password": "Strong-password-123!",
                "password_confirm": "Strong-password-123!",
            },
            format="json",
        )

        self.assertEqual(response.status_code, 503)
        self.assertFalse(User.objects.filter(email="new-user@example.com").exists())
