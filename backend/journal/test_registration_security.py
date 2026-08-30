import re
from datetime import timedelta
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from accounts.models import PendingRegistration
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.management import call_command
from django.test import override_settings
from django.test.client import RequestFactory
from django.urls import reverse
from django.utils import timezone
from plugin_host.hooks import RegistrationHookRejected, RegistrationHookUnavailable
from rest_framework import status
from rest_framework.test import APIClient, APITestCase
from site_config.models import InstallationState, SiteSettings

from .emails import EmailDeliveryError
from .registration import request_pending_registration

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
        installation = InstallationState.load()
        installation.status = InstallationState.Status.INITIALIZED
        installation.save(update_fields=["status", "updated_at"])
        site = SiteSettings.load()
        site.registration_enabled = True
        site.email_delivery_enabled = True
        site.save(update_fields=["registration_enabled", "email_delivery_enabled", "updated_at"])

    def _request_registration(self, email="victim@example.com"):
        sent = {}

        def capture(**kwargs):
            sent.update(kwargs)
            return {"id": "test"}

        with (
            patch("journal.auth_views.send_transactional_email", side_effect=capture),
            patch(
                "journal.auth_views._submit_email_task",
                side_effect=lambda delivery: delivery(),
            ),
        ):
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

    def test_registration_hook_failures_never_expose_exception_text(self):
        marker = "REGISTRATION-HOOK-STACK-SENTINEL"
        for exception, expected_status, expected_code in (
            (RegistrationHookRejected(marker), status.HTTP_403_FORBIDDEN, "registration_policy_rejected"),
            (RegistrationHookUnavailable(marker), status.HTTP_503_SERVICE_UNAVAILABLE, "registration_policy_unavailable"),
        ):
            with self.subTest(endpoint="request", code=expected_code), patch(
                "journal.auth_views.request_pending_registration",
                side_effect=exception,
            ):
                response = self.client.post(reverse("register-request"), {"email": "hook@example.com"}, format="json")
                self.assertEqual(response.status_code, expected_status)
                self.assertEqual(response.data["code"], expected_code)
                self.assertIn("correlation_id", response.data)
                self.assertNotIn(marker, str(response.data))

            with self.subTest(endpoint="complete", code=expected_code), patch(
                "journal.auth_views.complete_registration",
                side_effect=exception,
            ):
                response = self.client.post(
                    reverse("register-complete"),
                    {
                        "completion_token": "x" * 64,
                        "username": "hook-user",
                        "password": self.password,
                        "password_confirm": self.password,
                    },
                    format="json",
                )
                self.assertEqual(response.status_code, expected_status)
                self.assertEqual(response.data["code"], expected_code)
                self.assertIn("correlation_id", response.data)
                self.assertNotIn(marker, str(response.data))

    def test_repeated_request_invalidates_old_token(self):
        first_sent, _first = self._request_registration()
        old_token = self._token_from_email(first_sent)
        second_sent, _second = self._request_registration()
        new_token = self._token_from_email(second_sent)
        old_response = self.client.post(reverse("register-verify"), {"token": old_token}, format="json")
        self.assertEqual(old_response.status_code, status.HTTP_400_BAD_REQUEST)
        new_response = self.client.post(reverse("register-verify"), {"token": new_token}, format="json")
        self.assertEqual(new_response.status_code, status.HTTP_200_OK)

    def test_pending_request_uses_locked_get_or_create_for_absent_row_races(self):
        pending = PendingRegistration.objects.create(
            email="race@example.com",
            token_hash="a" * 64,
            expires_at=timezone.now() + timedelta(hours=1),
        )
        locked = patch("journal.registration.PendingRegistration.objects.select_for_update")
        with locked as select_for_update:
            queryset = select_for_update.return_value
            queryset.get_or_create.return_value = (pending, False)
            request = RequestFactory().post("/register", REMOTE_ADDR="198.51.100.50")
            result, raw_token, should_send = request_pending_registration(
                request=request,
                email="race@example.com",
            )
        queryset.get_or_create.assert_called_once()
        self.assertEqual(result.pk, pending.pk)
        self.assertTrue(raw_token)
        self.assertTrue(should_send)

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

    @patch(
        "journal.auth_views._submit_email_task",
        side_effect=lambda delivery: delivery(),
    )
    @patch("journal.auth_views.send_transactional_email", side_effect=EmailDeliveryError("provider down"))
    def test_email_failure_does_not_rollback_pending_or_create_user(
        self, _send, _submit
    ):
        with patch("journal.auth_views.logger.error") as error_log:
            with self.captureOnCommitCallbacks(execute=True):
                response = self.client.post(reverse("register-request"), {"email": "mail-failure@example.com"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(PendingRegistration.objects.filter(email="mail-failure@example.com").exists())
        self.assertFalse(User.objects.filter(email="mail-failure@example.com").exists())
        error_log.assert_called_once()
        self.assertEqual(
            error_log.call_args.args,
            ("registration_email_delivery_failed",),
        )
        self.assertEqual(
            set(error_log.call_args.kwargs["extra"]),
            {
                "animemo_stage",
                "correlation_id",
                "animemo_exception_class",
            },
        )
        self.assertEqual(
            error_log.call_args.kwargs["extra"]["animemo_stage"],
            "registration_email_delivery",
        )
        self.assertEqual(
            error_log.call_args.kwargs["extra"]["animemo_exception_class"],
            "EmailDeliveryError",
        )
        self.assertRegex(
            error_log.call_args.kwargs["extra"]["correlation_id"],
            r"^[0-9a-f]{32}$",
        )
        self.assertNotIn("pending_id", repr(error_log.call_args))
        self.assertNotIn("provider down", repr(error_log.call_args))

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
