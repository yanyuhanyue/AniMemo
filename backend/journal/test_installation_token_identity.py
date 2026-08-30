import secrets
import time
from datetime import timedelta
from io import StringIO

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.sessions.models import Session
from django.core.cache import cache
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient, APITestCase
from rest_framework_simplejwt.tokens import RefreshToken
from site_config.models import InstallationState

from .auth_tokens import issue_token_pair
from .security import _totp_at
from .staff_services import get_security_profile

User = get_user_model()


@override_settings(
    STORAGES={
        "default": {
            "BACKEND": "django.core.files.storage.FileSystemStorage",
        },
        "staticfiles": {
            "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage",
        },
    }
)
class InstallationTokenIdentityTests(APITestCase):
    totp_secret = "JBSWY3DPEHPK3PXP"

    def setUp(self):
        cache.clear()
        self.installation = InstallationState.load()
        self.installation.status = InstallationState.Status.INITIALIZED
        self.installation.initialized_at = timezone.now() - timedelta(days=1)
        self.installation.authentication_epoch = secrets.token_hex(32)
        self.installation.initialized_by = None
        self.installation.save(
            update_fields=[
                "status",
                "initialized_at",
                "initialized_by",
                "authentication_epoch",
                "updated_at",
            ]
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

        self.installation.authentication_epoch = secrets.token_hex(32)
        self.installation.save(
            update_fields=["authentication_epoch", "updated_at"]
        )

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

    def staff_login(self):
        self.user.is_staff = True
        self.user.is_superuser = True
        self.user.save(update_fields=["is_staff", "is_superuser"])
        profile = get_security_profile(self.user)
        profile.set_totp_secret(self.totp_secret)
        profile.two_factor_enabled = True
        profile.save(
            update_fields=[
                "totp_secret_encrypted",
                "two_factor_enabled",
                "updated_at",
            ]
        )
        client = APIClient(enforce_csrf_checks=True)
        csrf = client.get(reverse("csrf-token")).data["csrf_token"]
        response = client.post(
            reverse("staff-login"),
            {
                "username": self.user.username,
                "password": "StrongPass123!",
                "otp": _totp_at(self.totp_secret, time.time()),
            },
            format="json",
            HTTP_X_CSRFTOKEN=csrf,
        )
        return client, response

    def test_uninitialized_staff_login_does_not_leave_an_admin_session(self):
        self.installation.status = InstallationState.Status.UNINITIALIZED
        self.installation.authentication_epoch = ""
        self.installation.initialized_at = None
        self.installation.save(
            update_fields=[
                "status",
                "authentication_epoch",
                "initialized_at",
                "updated_at",
            ]
        )

        client, response = self.staff_login()

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(set(response.data), {"code", "detail", "correlation_id"})
        self.assertEqual(response.data["code"], "session_revoked")
        self.assertEqual(response.data["detail"], "登录会话已失效，请重新登录。")
        self.assertRegex(response.data["correlation_id"], r"^[0-9a-f]{32}$")
        self.assertEqual(
            response["X-AniMemo-Correlation-ID"],
            response.data["correlation_id"],
        )
        self.assertNotIn("_auth_user_id", client.session)
        admin = client.get("/admin/")
        self.assertEqual(admin.status_code, status.HTTP_302_FOUND)
        self.assertIn("/admin-login", admin["Location"])

    def test_tokens_without_an_installation_binding_fail_closed(self):
        legacy_refresh = RefreshToken.for_user(self.user)
        legacy_refresh["sv"] = get_security_profile(self.user).session_version

        rejected_access = self.authenticated_get(legacy_refresh.access_token)
        self.assertEqual(rejected_access.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertEqual(rejected_access.data["code"], "session_revoked")

        rejected_refresh = self.refresh(legacy_refresh)
        self.assertEqual(rejected_refresh.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertEqual(rejected_refresh.data["code"], "session_expired")

    def test_restore_epoch_rotation_invalidates_snapshot_tokens_without_printing_epoch(self):
        old_refresh, old_access = issue_token_pair(self.user)
        previous_epoch = self.installation.authentication_epoch
        admin_client, login_response = self.staff_login()
        self.assertEqual(login_response.status_code, status.HTTP_200_OK)
        self.assertEqual(admin_client.get("/admin/").status_code, status.HTTP_200_OK)
        previous_session_key = admin_client.session.session_key
        self.assertTrue(Session.objects.filter(session_key=previous_session_key).exists())
        primary_profile = get_security_profile(self.user)
        primary_version = primary_profile.session_version
        other_user = User.objects.create_user(
            username="snapshot-user",
            email="snapshot@example.test",
            password="StrongPass456!",
        )
        other_profile = get_security_profile(other_user)
        other_version = other_profile.session_version

        with self.assertRaises(CommandError):
            call_command("rotate_authentication_epoch")

        output = StringIO()
        call_command(
            "rotate_authentication_epoch",
            confirm_restore=True,
            stdout=output,
        )
        self.installation.refresh_from_db()
        primary_profile.refresh_from_db()
        other_profile.refresh_from_db()
        self.assertNotEqual(self.installation.authentication_epoch, previous_epoch)
        self.assertNotIn(self.installation.authentication_epoch, output.getvalue())
        self.assertEqual(primary_profile.session_version, primary_version + 1)
        self.assertEqual(other_profile.session_version, other_version + 1)
        self.assertFalse(Session.objects.filter(session_key=previous_session_key).exists())
        admin = admin_client.get("/admin/")
        self.assertEqual(admin.status_code, status.HTTP_302_FOUND)
        self.assertIn("/admin-login", admin["Location"])

        rejected_access = self.authenticated_get(old_access)
        self.assertEqual(rejected_access.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertEqual(rejected_access.data["code"], "session_revoked")
        rejected_refresh = self.refresh(old_refresh)
        self.assertEqual(rejected_refresh.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertEqual(rejected_refresh.data["code"], "session_expired")
