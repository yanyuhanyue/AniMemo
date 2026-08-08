import re
from datetime import timedelta
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from django.contrib.auth import get_user_model
from django.conf import settings
from django.core.cache import cache
from django.core.management import call_command
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient, APITestCase

from .emails import EmailDeliveryError
from accounts.models import PendingRegistration, UserSecurityProfile
from site_config.models import SiteSettings
from .models import JournalEntry


User = get_user_model()


@override_settings(
    REST_FRAMEWORK={
        **settings.REST_FRAMEWORK,
        "DEFAULT_THROTTLE_RATES": {
            **settings.REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"],
            "register": "1000/min", "register_ip": "1000/min", "register_account": "1000/min", "register_combined": "1000/min",
            "register_verify": "1000/min", "register_verify_ip": "1000/min", "register_verify_account": "1000/min", "register_verify_combined": "1000/min",
            "register_complete": "1000/min", "register_complete_ip": "1000/min", "register_complete_account": "1000/min", "register_complete_combined": "1000/min",
        },
    },
)
class RegistrationFlowSecurityTests(APITestCase):
    password = "VictimPassword123!"

    def setUp(self):
        cache.clear()
        site = SiteSettings.load()
        site.registration_enabled = True
        site.email_delivery_enabled = True
        site.save(update_fields=["registration_enabled", "email_delivery_enabled", "updated_at"])

    def _request_registration(self, email="victim@example.com"):
        sent = {}

        def capture(**kwargs):
            sent.update(kwargs)
            return {"id": "test"}

        with patch("journal.auth_views.send_transactional_email", side_effect=capture):
            with self.captureOnCommitCallbacks(execute=True):
                response = self.client.post(reverse("register-request"), {"email": email}, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        return sent, PendingRegistration.objects.get(email=email)

    @staticmethod
    def _token_from_email(sent):
        url = re.search(r"https?://[^\s]+/register/verify\?token=[^\s<]+", sent["text"]).group(0)
        return parse_qs(urlparse(url).query)["token"][0]

    def test_request_creates_only_pending_state_and_never_accepts_password(self):
        sent, pending = self._request_registration()
        self.assertFalse(User.objects.filter(email="victim@example.com").exists())
        self.assertNotIn("password", {field.name for field in pending._meta.fields})
        raw_token = self._token_from_email(sent)
        self.assertNotEqual(raw_token, pending.token_hash)
        self.assertNotIn(raw_token, pending.token_hash)
        self.assertNotIn(self.password, pending.token_hash)

    def test_repeated_request_invalidates_old_token(self):
        first_sent, _first = self._request_registration()
        old_token = self._token_from_email(first_sent)
        second_sent, _second = self._request_registration()
        new_token = self._token_from_email(second_sent)
        old_response = self.client.post(reverse("register-verify"), {"token": old_token}, format="json")
        self.assertEqual(old_response.status_code, status.HTTP_400_BAD_REQUEST)
        new_response = self.client.post(reverse("register-verify"), {"token": new_token}, format="json")
        self.assertEqual(new_response.status_code, status.HTTP_200_OK)

    def test_verify_then_complete_is_the_only_path_that_creates_user(self):
        sent, pending = self._request_registration()
        raw_token = self._token_from_email(sent)
        verify = self.client.post(reverse("register-verify"), {"token": raw_token}, format="json")
        self.assertEqual(verify.status_code, status.HTTP_200_OK)
        self.assertFalse(User.objects.filter(email=pending.email).exists())
        completion = verify.data["completion_token"]
        complete = self.client.post(reverse("register-complete"), {
            "completion_token": completion,
            "username": "victim-user",
            "password": self.password,
            "password_confirm": self.password,
        }, format="json")
        self.assertEqual(complete.status_code, status.HTTP_201_CREATED)
        user = User.objects.get(email=pending.email)
        self.assertTrue(user.is_active)
        self.assertTrue(user.security_profile.email_verified)
        pending.refresh_from_db()
        self.assertIsNotNone(pending.consumed_at)
        self.assertEqual(
            self.client.post(reverse("register-complete"), {
                "completion_token": completion,
                "username": "victim-user-2",
                "password": self.password,
                "password_confirm": self.password,
            }, format="json").status_code,
            status.HTTP_400_BAD_REQUEST,
        )

    def test_pre_hijacking_regression_attacker_password_is_never_used(self):
        sent, _pending = self._request_registration("pre-hijack@example.com")
        attacker_password = "AttackerPassword123!"
        self.assertFalse(User.objects.filter(email="pre-hijack@example.com").exists())
        verify = self.client.post(reverse("register-verify"), {"token": self._token_from_email(sent)}, format="json")
        completion = verify.data["completion_token"]
        complete = self.client.post(reverse("register-complete"), {
            "completion_token": completion,
            "username": "real-owner",
            "password": self.password,
            "password_confirm": self.password,
        }, format="json")
        self.assertEqual(complete.status_code, status.HTTP_201_CREATED)
        login = APIClient().post(reverse("token_obtain_pair"), {
            "username": "pre-hijack@example.com",
            "password": attacker_password,
        }, format="json")
        self.assertEqual(login.status_code, status.HTTP_401_UNAUTHORIZED)
        owner_login = APIClient().post(reverse("token_obtain_pair"), {
            "username": "real-owner",
            "password": self.password,
        }, format="json")
        self.assertEqual(owner_login.status_code, status.HTTP_200_OK)

    def test_expired_token_and_csrf_protection(self):
        sent, pending = self._request_registration("expired@example.com")
        pending.expires_at = timezone.now() - timedelta(minutes=1)
        pending.save(update_fields=["expires_at"])
        expired = self.client.post(reverse("register-verify"), {"token": self._token_from_email(sent)}, format="json")
        self.assertEqual(expired.status_code, status.HTTP_400_BAD_REQUEST)

        csrf_client = APIClient(enforce_csrf_checks=True)
        no_csrf = csrf_client.post(reverse("register-request"), {"email": "csrf@example.com"}, format="json")
        self.assertEqual(no_csrf.status_code, status.HTTP_403_FORBIDDEN)

    @patch("journal.auth_views.send_transactional_email", side_effect=EmailDeliveryError("provider down"))
    def test_email_failure_does_not_rollback_pending_or_create_user(self, _send):
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(reverse("register-request"), {"email": "mail-failure@example.com"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(PendingRegistration.objects.filter(email="mail-failure@example.com").exists())
        self.assertFalse(User.objects.filter(email="mail-failure@example.com").exists())

    def test_pending_cleanup_command_preserves_valid_rows(self):
        now = timezone.now()
        PendingRegistration.objects.create(email="expired-command@example.com", token_hash="a" * 64, expires_at=now - timedelta(minutes=1))
        PendingRegistration.objects.create(email="valid-command@example.com", token_hash="b" * 64, expires_at=now + timedelta(hours=1))
        consumed = PendingRegistration.objects.create(email="consumed-command@example.com", token_hash="c" * 64, expires_at=now + timedelta(hours=1))
        consumed.consumed_at = now - timedelta(days=31)
        consumed.save(update_fields=["consumed_at"])
        call_command("purge_expired_pending_registrations")
        self.assertFalse(PendingRegistration.objects.filter(email="expired-command@example.com").exists())
        self.assertFalse(PendingRegistration.objects.filter(email="consumed-command@example.com").exists())
        self.assertTrue(PendingRegistration.objects.filter(email="valid-command@example.com").exists())
