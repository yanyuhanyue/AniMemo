from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient, APITestCase
from rest_framework_simplejwt.tokens import RefreshToken

from site_config.models import InstallationState

from .auth_tokens import issue_token_pair
from .staff_services import get_security_profile


User = get_user_model()


class InstallationTokenIdentityTests(APITestCase):
    def setUp(self):
        cache.clear()
        self.installation = InstallationState.load()
        self.installation.status = InstallationState.Status.INITIALIZED
        self.installation.initialized_at = timezone.now() - timedelta(days=1)
        self.installation.initialized_by = None
        self.installation.save(
            update_fields=["status", "initialized_at", "initialized_by", "updated_at"]
        )
        self.user = User.objects.create_user(
            username="old-installation-user",
            email="old-installation@example.com",
            password="StrongPass123!",
        )

    @staticmethod
    def authenticated_get(access):
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")
        return client.get(reverse("me"))

    @staticmethod
    def refresh(raw_refresh):
        client = APIClient(enforce_csrf_checks=True)
        csrf_response = client.get(reverse("csrf-token"))
        client.cookies[settings.REFRESH_COOKIE_NAME] = str(raw_refresh)
        return client.post(
            reverse("token_refresh"),
            {},
            format="json",
            HTTP_X_CSRFTOKEN=csrf_response.data["csrf_token"],
        )

    def test_tokens_from_previous_installation_cannot_authenticate_reused_user_identity(self):
        old_refresh, old_access = issue_token_pair(self.user)
        old_user_id = self.user.pk
        old_session_version = get_security_profile(self.user).session_version
        self.assertEqual(
            self.authenticated_get(old_access).status_code,
            status.HTTP_200_OK,
        )

        self.installation.initialized_at += timedelta(seconds=1)
        self.installation.save(update_fields=["initialized_at", "updated_at"])

        rejected_refresh = self.refresh(old_refresh)
        self.assertEqual(rejected_refresh.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertEqual(rejected_refresh.data["code"], "session_expired")

        self.user.delete()
        replacement = User.objects.create_user(
            id=old_user_id,
            username="new-installation-user",
            email="new-installation@example.com",
            password="StrongPass456!",
        )
        self.installation.initialized_by = replacement
        self.installation.save(update_fields=["initialized_by", "updated_at"])
        self.assertEqual(replacement.pk, old_user_id)
        self.assertEqual(
            get_security_profile(replacement).session_version,
            old_session_version,
        )

        rejected_access = self.authenticated_get(old_access)
        self.assertEqual(rejected_access.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertEqual(rejected_access.data["code"], "session_revoked")

        new_refresh, new_access = issue_token_pair(replacement)
        accepted_access = self.authenticated_get(new_access)
        self.assertEqual(accepted_access.status_code, status.HTTP_200_OK)
        self.assertEqual(accepted_access.data["username"], replacement.username)

        rotated = self.refresh(new_refresh)
        self.assertEqual(rotated.status_code, status.HTTP_200_OK)
        self.assertIn("access", rotated.data)
        self.assertEqual(rotated.data["user"]["id"], replacement.pk)
        self.assertEqual(
            self.authenticated_get(rotated.data["access"]).status_code,
            status.HTTP_200_OK,
        )

    def test_tokens_without_an_installation_binding_fail_closed(self):
        legacy_refresh = RefreshToken.for_user(self.user)
        legacy_refresh["sv"] = get_security_profile(self.user).session_version

        rejected_access = self.authenticated_get(legacy_refresh.access_token)
        self.assertEqual(rejected_access.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertEqual(rejected_access.data["code"], "session_revoked")

        rejected_refresh = self.refresh(legacy_refresh)
        self.assertEqual(rejected_refresh.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertEqual(rejected_refresh.data["code"], "session_expired")
