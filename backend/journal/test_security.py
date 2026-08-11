import json
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import parse_qs, unquote, urlparse
from unittest.mock import patch

from PIL import Image
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.tokens import default_token_generator
from django.core.cache import cache
from django.core.files.storage import default_storage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import close_old_connections, connection
from django.db import IntegrityError, transaction
from django.test import RequestFactory, SimpleTestCase, TransactionTestCase, override_settings, skipUnlessDBFeature
from django.urls import reverse
from django.utils import timezone
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode
from rest_framework import status
from rest_framework.exceptions import ParseError
from rest_framework.throttling import SimpleRateThrottle
from rest_framework.test import APIClient, APITestCase
from rest_framework_simplejwt.token_blacklist.models import BlacklistedToken, OutstandingToken
from rest_framework_simplejwt.tokens import RefreshToken

from .account_security import AccountDeletionError, delete_current_account
from .emails import EmailDeliveryError
from .auth_tokens import create_refresh_token
from .csv_security import safe_csv_value
from .import_parsers import LimitedImportJSONParser
from accounts.models import PendingRegistration, RevokedAccessToken, StaffProfile, UserSecurityProfile
from site_config.models import SiteSettings
from .models import AdminAuditLog, Column, JournalEntry, UserSettings
from plugin_host.models import PluginDeployment, PluginPackageBlob, PluginProject, PluginVersion
from plugin_host.permissions import can_access_plugin_backend, plugin_permissions_for_user
from .serializers import ColumnSerializer, JournalEntrySerializer, RegistrationCompleteSerializer, RegistrationRequestSerializer, SiteSettingsSerializer, UserSettingsSerializer
from .network import client_ip
from .security import TOTP_RECOVERY_CODE_COUNT, _totp_at, consume_recovery_code, hash_recovery_codes
from .throttling import AuthThrottleUnavailable, HashedAccountRateThrottle
from .admin_security_middleware import STAFF_2FA_AT_KEY, STAFF_2FA_USER_KEY, STAFF_2FA_VERSION_KEY
from .staff_services import ALL_CAPABILITIES, resolve_staff_role, staff_capabilities


User = get_user_model()
RELAXED_THROTTLE_SETTINGS = {
    **settings.REST_FRAMEWORK,
    "DEFAULT_THROTTLE_RATES": {
        **settings.REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"],
        "login": "1000/min",
        "login_ip": "1000/min",
        "login_account": "1000/min",
        "login_combined": "1000/min",
        "password_reset": "1000/min",
        "password_reset_ip": "1000/min",
        "password_reset_account": "1000/min",
        "password_reset_combined": "1000/min",
        "register_request": "1000/min",
        "register_request_ip": "1000/min",
        "register_request_account": "1000/min",
        "register_request_combined": "1000/min",
        "two_factor": "1000/min",
        "two_factor_ip": "1000/min",
        "two_factor_account": "1000/min",
        "two_factor_combined": "1000/min",
        "external_search": "1000/min",
        "import": "1000/min",
    },
}
REGISTER_THROTTLE_SETTINGS = {
    **RELAXED_THROTTLE_SETTINGS,
    "DEFAULT_THROTTLE_RATES": {
        **RELAXED_THROTTLE_SETTINGS["DEFAULT_THROTTLE_RATES"],
        "register_request": "100/hour",
        "register_request_ip": "2/hour",
        "register_request_account": "2/hour",
        "register_request_combined": "1/hour",
    },
}


class TrustedProxyIpTests(SimpleTestCase):
    def setUp(self):
        self.factory = RequestFactory()

    @override_settings(TRUSTED_PROXY_IPS=[])
    def test_untrusted_client_cannot_spoof_forwarded_for(self):
        request = self.factory.get("/", REMOTE_ADDR="198.51.100.10", HTTP_X_FORWARDED_FOR="203.0.113.9")
        self.assertEqual(client_ip(request), "198.51.100.10")

    @override_settings(TRUSTED_PROXY_IPS=["127.0.0.1/32"])
    def test_configured_proxy_can_supply_client_address(self):
        request = self.factory.get("/", REMOTE_ADDR="127.0.0.1", HTTP_X_FORWARDED_FOR="203.0.113.9, 127.0.0.1")
        self.assertEqual(client_ip(request), "203.0.113.9")

    @override_settings(TRUSTED_PROXY_IPS=["127.0.0.1/32", "2001:db8:1::/64"])
    def test_proxy_chain_supports_ipv6_and_falls_back_on_malformed_xff(self):
        request = self.factory.get(
            "/",
            REMOTE_ADDR="127.0.0.1",
            HTTP_X_FORWARDED_FOR="2001:db8:2::9, 2001:db8:1::10",
        )
        self.assertEqual(client_ip(request), "2001:db8:2::9")
        malformed = self.factory.get("/", REMOTE_ADDR="127.0.0.1", HTTP_X_FORWARDED_FOR="not-an-ip")
        self.assertEqual(client_ip(malformed), "127.0.0.1")


@override_settings(
    ALLOWED_HOSTS=["app.example.com"],
    DEBUG=False,
    SECURE_PROXY_SSL_HEADER=("HTTP_X_FORWARDED_PROTO", "https"),
    SECURE_SSL_REDIRECT=True,
)
class ProductionHealthSecurityTests(SimpleTestCase):
    def test_secure_forwarded_health_request_returns_ok_without_redirect(self):
        response = self.client.get(
            "/health/",
            HTTP_HOST="app.example.com",
            HTTP_X_FORWARDED_PROTO="https",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.json(), {"status": "ok", "service": "anime-journal-api"})

    def test_plain_http_health_request_still_redirects_to_https(self):
        response = self.client.get("/health/", HTTP_HOST="app.example.com")
        self.assertEqual(response.status_code, status.HTTP_301_MOVED_PERMANENTLY)
        self.assertEqual(response["Location"], "https://app.example.com/health/")


@override_settings(STORAGES={
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
})
class AdminSecondFactorTests(APITestCase):
    password = "StrongPass123!"
    secret = "JBSWY3DPEHPK3PXP"

    def setUp(self):
        cache.clear()
        self.admin = User.objects.create_superuser(
            username="admin-gateway",
            email="admin-gateway@example.com",
            password=self.password,
        )

    def enable_2fa(self):
        profile, _ = UserSecurityProfile.objects.get_or_create(user=self.admin)
        profile.set_totp_secret(self.secret)
        profile.two_factor_enabled = True
        profile.save(update_fields=["totp_secret_encrypted", "two_factor_enabled", "updated_at"])
        return profile

    def staff_login(self, *, recovery_code=""):
        self.enable_2fa()
        client = APIClient(enforce_csrf_checks=True)
        csrf = client.get(reverse("csrf-token")).data["csrf_token"]
        payload = {"username": self.admin.username, "password": self.password}
        if recovery_code:
            payload["recovery_code"] = recovery_code
        else:
            payload["otp"] = _totp_at(self.secret, time.time())
        response = client.post(reverse("staff-login"), payload, format="json", HTTP_X_CSRFTOKEN=csrf)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return client

    def test_default_admin_login_is_blocked_and_unverified_session_redirects(self):
        client = APIClient()
        self.assertEqual(client.get("/admin/login/").status_code, status.HTTP_302_FOUND)
        client.force_login(self.admin)
        response = client.get("/admin/")
        self.assertEqual(response.status_code, status.HTTP_302_FOUND)
        self.assertIn("/admin-login", response["Location"])

    def test_valid_totp_session_can_enter_admin(self):
        client = self.staff_login()
        response = client.get("/admin/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(client.session[STAFF_2FA_USER_KEY], str(self.admin.pk))

    def test_admin_second_factor_timestamp_must_be_present_valid_and_fresh(self):
        for value in (None, "not-a-date", (timezone.now() - timedelta(hours=9)).isoformat()):
            with self.subTest(value=value):
                client = self.staff_login()
                session = client.session
                if value is None:
                    session.pop(STAFF_2FA_AT_KEY, None)
                else:
                    session[STAFF_2FA_AT_KEY] = value
                session.save()
                response = client.get("/admin/")
                self.assertEqual(response.status_code, status.HTTP_302_FOUND)
                self.assertIn("/admin-login", response["Location"])
                self.assertIsNone(client.session.get("_auth_user_id"))

    @override_settings(ADMIN_2FA_SESSION_MAX_AGE=28800)
    def test_admin_second_factor_timestamp_within_max_age_remains_valid(self):
        client = self.staff_login()
        session = client.session
        session[STAFF_2FA_AT_KEY] = (timezone.now() - timedelta(hours=7, minutes=59)).isoformat()
        session.save()
        self.assertEqual(client.get("/admin/").status_code, status.HTTP_200_OK)

    def test_valid_recovery_session_can_enter_admin(self):
        profile = self.enable_2fa()
        recovery_code = "AAAA-BBBB-CCCC"
        profile.recovery_code_hashes = hash_recovery_codes([recovery_code])
        profile.save(update_fields=["recovery_code_hashes", "updated_at"])
        client = self.staff_login(recovery_code=recovery_code)
        self.assertEqual(client.get("/admin/").status_code, status.HTTP_200_OK)
        profile.refresh_from_db()
        self.assertEqual(profile.recovery_code_hashes, [])

    def test_session_identity_and_version_changes_invalidate_admin(self):
        client = self.staff_login()
        session = client.session
        session[STAFF_2FA_USER_KEY] = "other-user"
        session.save()
        self.assertEqual(client.get("/admin/").status_code, status.HTTP_302_FOUND)

        client = self.staff_login()
        profile = UserSecurityProfile.objects.get(user=self.admin)
        profile.session_version += 1
        profile.save(update_fields=["session_version", "updated_at"])
        self.assertEqual(client.get("/admin/").status_code, status.HTTP_302_FOUND)

    def test_staff_status_and_logout_invalidate_admin(self):
        client = self.staff_login()
        self.admin.is_staff = False
        self.admin.save(update_fields=["is_staff"])
        self.assertEqual(client.get("/admin/").status_code, status.HTTP_302_FOUND)

        self.admin.is_staff = True
        self.admin.save(update_fields=["is_staff"])
        client = self.staff_login()
        csrf = client.get(reverse("csrf-token")).data["csrf_token"]
        self.assertEqual(client.post(reverse("logout"), {}, format="json", HTTP_X_CSRFTOKEN=csrf).status_code, status.HTTP_200_OK)
        self.assertEqual(client.get("/admin/").status_code, status.HTTP_302_FOUND)

    def test_admin_static_path_is_not_redirected_by_second_factor_gate(self):
        client = APIClient()
        response = client.get("/static/admin/css/base.css")
        self.assertNotEqual(response.status_code, status.HTTP_302_FOUND)


class StaffHierarchySecurityTests(APITestCase):
    password = "StrongPass123!"
    secret = "JBSWY3DPEHPK3PXP"

    def setUp(self):
        cache.clear()
        self.manager = User.objects.create_user(username="user-manager", password=self.password, is_staff=True)
        StaffProfile.objects.create(user=self.manager, role=StaffProfile.Role.USER_MANAGER)
        self.normal = User.objects.create_user(username="managed-user", password=self.password)
        self.staff = User.objects.create_user(username="managed-staff", password=self.password, is_staff=True)
        StaffProfile.objects.create(user=self.staff, role=StaffProfile.Role.REVIEWER)

    def test_user_manager_can_manage_normal_user_but_not_staff(self):
        client = APIClient()
        client.force_authenticate(self.manager)
        normal = client.post(reverse("staff-user-action", kwargs={"pk": self.normal.pk, "action": "reset-password"}), {
            "password": "ChangedPass456!",
            "password_confirm": "ChangedPass456!",
        }, format="json")
        self.assertEqual(normal.status_code, status.HTTP_200_OK)
        denied = client.post(reverse("staff-user-action", kwargs={"pk": self.staff.pk, "action": "force-logout"}), {}, format="json")
        self.assertEqual(denied.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(denied.data["detail"], "无权操作该管理员账号。")
        self.assertTrue(AdminAuditLog.objects.filter(action="user.management_denied", target_id=str(self.staff.pk)).exists())

    def test_staff_sensitive_action_requires_reauthentication(self):
        actor = User.objects.create_superuser(username="root-manager", password=self.password)
        profile, _ = UserSecurityProfile.objects.get_or_create(user=actor)
        profile.set_totp_secret(self.secret)
        profile.two_factor_enabled = True
        profile.save(update_fields=["totp_secret_encrypted", "two_factor_enabled", "updated_at"])
        client = APIClient()
        client.force_authenticate(actor)
        denied = client.post(reverse("staff-user-action", kwargs={"pk": self.staff.pk, "action": "force-logout"}), {}, format="json")
        self.assertEqual(denied.status_code, status.HTTP_403_FORBIDDEN)
        accepted = client.post(reverse("staff-user-action", kwargs={"pk": self.staff.pk, "action": "force-logout"}), {
            "current_password": self.password,
            "otp": _totp_at(self.secret, time.time()),
        }, format="json")
        self.assertEqual(accepted.status_code, status.HTTP_200_OK)

    def test_last_active_superuser_cannot_be_disabled(self):
        actor = User.objects.create_superuser(username="root-one", password=self.password)
        target = User.objects.create_superuser(username="root-two", password=self.password)
        profile, _ = UserSecurityProfile.objects.get_or_create(user=actor)
        profile.set_totp_secret(self.secret)
        profile.two_factor_enabled = True
        profile.save(update_fields=["totp_secret_encrypted", "two_factor_enabled", "updated_at"])
        client = APIClient()
        client.force_authenticate(actor)
        payload = {"is_active": False, "current_password": self.password, "otp": _totp_at(self.secret, time.time())}
        self.assertEqual(client.patch(reverse("staff-user-permissions", kwargs={"pk": target.pk}), payload, format="json").status_code, status.HTTP_200_OK)
        actor.refresh_from_db()
        denied = client.patch(reverse("staff-user-permissions", kwargs={"pk": actor.pk}), payload, format="json")
        self.assertEqual(denied.status_code, status.HTTP_403_FORBIDDEN)
        actor.refresh_from_db()
        self.assertTrue(actor.is_active)


@override_settings(REST_FRAMEWORK=RELAXED_THROTTLE_SETTINGS)
class StaffProfileFailClosedTests(APITestCase):
    def test_superuser_keeps_all_capabilities(self):
        user = User.objects.create_superuser(username="cap-root", password="StrongPass123!")
        self.assertEqual(staff_capabilities(user), list(ALL_CAPABILITIES))
        self.assertEqual(resolve_staff_role(user), "superuser")

    def test_explicit_roles_keep_only_their_declared_capabilities(self):
        expected = {
            StaffProfile.Role.REVIEWER: {"view_dashboard", "moderate_content", "view_audit"},
            StaffProfile.Role.USER_MANAGER: {"view_dashboard", "manage_users", "view_audit"},
            StaffProfile.Role.ADMINISTRATOR: set(ALL_CAPABILITIES),
        }
        for index, (role, capabilities) in enumerate(expected.items()):
            user = User.objects.create_user(username=f"role-{index}", password="StrongPass123!", is_staff=True)
            StaffProfile.objects.create(user=user, role=role)
            self.assertEqual(set(staff_capabilities(user)), capabilities)

    def test_missing_profile_returns_no_capabilities_without_creating_one(self):
        user = User.objects.create_user(username="profile-missing", password="StrongPass123!", is_staff=True)
        self.assertIsNone(resolve_staff_role(user))
        self.assertEqual(staff_capabilities(user), [])
        self.assertFalse(StaffProfile.objects.filter(user=user).exists())

        client = APIClient()
        client.force_authenticate(user)
        self.assertEqual(client.get(reverse("staff-system-health")).status_code, status.HTTP_403_FORBIDDEN)
        self.assertFalse(StaffProfile.objects.filter(user=user).exists())

        StaffProfile.objects.create(user=user, role=StaffProfile.Role.OPERATOR)
        self.assertEqual(client.get(reverse("staff-system-health")).status_code, status.HTTP_200_OK)

    def test_regular_user_never_receives_staff_capabilities(self):
        user = User.objects.create_user(username="plain-cap-user", password="StrongPass123!")
        self.assertEqual(staff_capabilities(user), [])

    def test_stale_administrator_profile_cannot_grant_staff_or_plugin_access(self):
        user = User.objects.create_user(username="stale-admin-profile", password="StrongPass123!")
        StaffProfile.objects.create(user=user, role=StaffProfile.Role.ADMINISTRATOR)
        manifest = {
            "permissions": [{"code": "demo.manage", "roles": [StaffProfile.Role.ADMINISTRATOR]}],
            "frontend": {"exposure": "staff"},
        }
        self.assertIsNone(resolve_staff_role(user))
        self.assertEqual(staff_capabilities(user), [])
        self.assertEqual(plugin_permissions_for_user(user), [])
        self.assertFalse(can_access_plugin_backend(user, "demo", manifest, access="staff", permission_code="demo.manage"))

    def test_direct_staff_demotion_removes_profile(self):
        user = User.objects.create_user(username="direct-demotion", password="StrongPass123!", is_staff=True)
        StaffProfile.objects.create(user=user, role=StaffProfile.Role.ADMINISTRATOR)
        user.is_staff = False
        user.save(update_fields=["is_staff"])
        self.assertFalse(StaffProfile.objects.filter(user=user).exists())


class ProductionSecretKeyTests(SimpleTestCase):
    valid_credential_key = "a0DtqkhZwqytmU2lcF-2oUKmjlyqPIrJsU5O_T6d3Io="

    def load_settings(self, secret, *, command="import django; django.setup(); print('ok')", **overrides):
        environment = os.environ.copy()
        environment.update({
            "DEBUG": "false",
            "DATABASE_URL": "postgresql://test:ProdTestPassword2026@127.0.0.1:5432/anime_journal_test",
            "POSTGRES_PASSWORD": "ProdTestPassword2026",
            "TURNSTILE_ENABLED": "false",
            "REDIS_URL": "redis://127.0.0.1:6379/15",
            "CREDENTIAL_ENCRYPTION_KEY": self.valid_credential_key,
            "MEDIA_LOCAL_STORAGE_ROOT": "/tmp/anime-journal-media",
            "ALLOWED_HOSTS": "example.com",
            "FRONTEND_URL": "https://example.com",
            "CORS_ALLOWED_ORIGINS": "https://example.com",
            "CSRF_TRUSTED_ORIGINS": "https://example.com",
            "TRUSTED_PROXY_IPS": "127.0.0.1/32,172.28.0.0/16",
            "SESSION_COOKIE_SECURE": "true",
            "CSRF_COOKIE_SECURE": "true",
            "REFRESH_COOKIE_SECURE": "true",
            "DJANGO_SETTINGS_MODULE": "config.settings",
        })
        if secret is None:
            environment.pop("DJANGO_SECRET_KEY", None)
        else:
            environment["DJANGO_SECRET_KEY"] = secret
        for key, value in overrides.items():
            if value is None:
                environment.pop(key, None)
            else:
                environment[key] = value
        return subprocess.run(
            [sys.executable, "-c", command],
            cwd=settings.BASE_DIR,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_production_rejects_missing_placeholder_and_short_secrets(self):
        for secret in (
            None,
            "change-me",
            "short-secret",
            "local-only-anime-journal-secret-key-change-this-before-production-2026",
            "replace-with-a-random-secret-of-at-least-50-characters",
        ):
            with self.subTest(secret=secret):
                result = self.load_settings(secret)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("DJANGO_SECRET_KEY", result.stderr)

    def test_production_accepts_stable_random_secret(self):
        result = self.load_settings("a9" * 32)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_production_database_ssl_can_be_disabled_for_private_compose_postgres(self):
        command = (
            "from django.conf import settings; "
            "print(settings.DATABASES['default'].get('OPTIONS', {}).get('sslmode', 'disabled'))"
        )
        required = self.load_settings("a9" * 32, command=command, DATABASE_SSL_REQUIRE=None)
        self.assertEqual(required.returncode, 0, required.stderr)
        self.assertEqual(required.stdout.strip(), "require")

        private_compose = self.load_settings(
            "a9" * 32,
            command=command,
            DATABASE_SSL_REQUIRE="false",
        )
        self.assertEqual(private_compose.returncode, 0, private_compose.stderr)
        self.assertEqual(private_compose.stdout.strip(), "disabled")

    def test_production_requires_matching_non_placeholder_postgres_password(self):
        cases = (
            ({"POSTGRES_PASSWORD": None}, "POSTGRES_PASSWORD"),
            ({"POSTGRES_PASSWORD": "change-me"}, "POSTGRES_PASSWORD"),
            ({"POSTGRES_PASSWORD": "REPLACE_WITH_STRONG_RANDOM_PASSWORD"}, "POSTGRES_PASSWORD"),
            ({"POSTGRES_PASSWORD": "DifferentProductionPassword"}, "DATABASE_URL"),
        )
        for overrides, expected in cases:
            with self.subTest(overrides=overrides):
                result = self.load_settings("a9" * 32, **overrides)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(expected, result.stderr)

    def test_production_requires_a_real_fernet_credential_key(self):
        for credential_key in (
            None,
            "1",
            "test-credential-encryption-key",
            "replace-with-a-random-fernet-key-or-long-random-secret",
            "replace-with-a-random-fernet-key",
        ):
            with self.subTest(credential_key=credential_key):
                result = self.load_settings("a9" * 32, CREDENTIAL_ENCRYPTION_KEY=credential_key)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("CREDENTIAL_ENCRYPTION_KEY", result.stderr)

    def test_production_requires_safe_frontend_cors_csrf_and_cookie_settings(self):
        cases = [
            ({"FRONTEND_URL": ""}, "FRONTEND_URL"),
            ({"FRONTEND_URL": "http://example.com"}, "FRONTEND_URL"),
            ({"FRONTEND_URL": "https://localhost"}, "FRONTEND_URL"),
            ({"CORS_ALLOWED_ORIGINS": ""}, "CORS_ALLOWED_ORIGINS"),
            ({"CORS_ALLOWED_ORIGINS": "*"}, "CORS_ALLOWED_ORIGINS"),
            ({"CSRF_TRUSTED_ORIGINS": ""}, "CSRF_TRUSTED_ORIGINS"),
            ({"SESSION_COOKIE_SECURE": "false"}, "SESSION_COOKIE_SECURE"),
            ({"REFRESH_COOKIE_SAMESITE": "None", "REFRESH_COOKIE_SECURE": "false"}, "REFRESH_COOKIE_SAMESITE"),
        ]
        for overrides, expected in cases:
            with self.subTest(overrides=overrides):
                result = self.load_settings("b7" * 32, **overrides)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(expected, result.stderr)

    def test_development_can_use_local_default_secret(self):
        environment = os.environ.copy()
        environment.update({"DEBUG": "true", "DJANGO_SETTINGS_MODULE": "config.settings"})
        environment.pop("DJANGO_SECRET_KEY", None)
        result = subprocess.run(
            [sys.executable, "-c", "import django; django.setup(); print('ok')"],
            cwd=settings.BASE_DIR,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_debug_defaults_false_and_invalid_values_fail_closed(self):
        environment = os.environ.copy()
        environment.update({
            "DJANGO_SETTINGS_MODULE": "config.settings",
            "DJANGO_SECRET_KEY": "ab" * 32,
            "DATABASE_URL": "postgresql://test:ProdTestPassword2026@127.0.0.1:5432/anime_journal_test",
            "POSTGRES_PASSWORD": "ProdTestPassword2026",
            "TURNSTILE_ENABLED": "false",
            "ALLOWED_HOSTS": "example.com",
            "FRONTEND_URL": "https://example.com",
            "CORS_ALLOWED_ORIGINS": "https://example.com",
            "CSRF_TRUSTED_ORIGINS": "https://example.com",
            "TRUSTED_PROXY_IPS": "127.0.0.1/32,172.28.0.0/16",
            "REDIS_URL": "redis://127.0.0.1:6379/15",
            "CREDENTIAL_ENCRYPTION_KEY": self.valid_credential_key,
            "MEDIA_LOCAL_STORAGE_ROOT": "/tmp/anime-journal-media",
            "SESSION_COOKIE_SECURE": "true",
            "CSRF_COOKIE_SECURE": "true",
            "REFRESH_COOKIE_SECURE": "true",
        })
        environment.pop("DEBUG", None)
        # Keep this subprocess focused on the absent-environment default even
        # when a local bootstrap has created the development .env file.
        environment["PYTHON_DOTENV_DISABLED"] = "true"
        result = subprocess.run(
            [sys.executable, "-c", "from django.conf import settings; print(settings.DEBUG); print(settings.CACHES['default']['BACKEND'])"],
            cwd=settings.BASE_DIR,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("False", result.stdout)
        self.assertIn("django_redis.cache.RedisCache", result.stdout)

        environment["DEBUG"] = "abc"
        invalid = subprocess.run(
            [sys.executable, "-c", "import django; django.setup()"],
            cwd=settings.BASE_DIR,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(invalid.returncode, 0)
        self.assertIn("DEBUG", invalid.stderr)

    def test_production_rejects_unsafe_proxy_networks(self):
        environment = os.environ.copy()
        environment.update({
            "DEBUG": "false",
            "DJANGO_SETTINGS_MODULE": "config.settings",
            "DJANGO_SECRET_KEY": "cd" * 32,
            "DATABASE_URL": "postgresql://test:ProdTestPassword2026@127.0.0.1:5432/anime_journal_test",
            "POSTGRES_PASSWORD": "ProdTestPassword2026",
            "TURNSTILE_ENABLED": "false",
            "ALLOWED_HOSTS": "example.com",
            "FRONTEND_URL": "https://example.com",
            "CORS_ALLOWED_ORIGINS": "https://example.com",
            "CSRF_TRUSTED_ORIGINS": "https://example.com",
            "REDIS_URL": "redis://127.0.0.1:6379/15",
            "CREDENTIAL_ENCRYPTION_KEY": self.valid_credential_key,
            "MEDIA_LOCAL_STORAGE_ROOT": "/tmp/anime-journal-media",
            "SESSION_COOKIE_SECURE": "true",
            "CSRF_COOKIE_SECURE": "true",
            "REFRESH_COOKIE_SECURE": "true",
        })
        for proxy in ("not-a-network", "0.0.0.0/0", "::/0", "10.0.0.0/8"):
            with self.subTest(proxy=proxy):
                environment["TRUSTED_PROXY_IPS"] = proxy
                result = subprocess.run(
                    [sys.executable, "-c", "import django; django.setup()"],
                    cwd=settings.BASE_DIR,
                    env=environment,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("TRUSTED_PROXY_IPS", result.stderr)


@override_settings(REST_FRAMEWORK=RELAXED_THROTTLE_SETTINGS)
class CookieJwtSecurityTests(APITestCase):
    password = "StrongPass123!"

    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(
            username="secure-user",
            email="secure@example.com",
            password=self.password,
        )

    def login(self, client=None, password=None):
        client = client or APIClient(enforce_csrf_checks=True)
        csrf = self.csrf_token(client)
        response = client.post(reverse("token_obtain_pair"), {
            "username": self.user.username,
            "password": password or self.password,
        }, format="json", HTTP_X_CSRFTOKEN=csrf)
        return client, response

    def test_login_rejects_missing_wrong_json_and_form_csrf(self):
        client = APIClient(enforce_csrf_checks=True)
        payload = {"username": self.user.username, "password": self.password}
        self.assertEqual(client.post(reverse("token_obtain_pair"), payload, format="json").status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(client.post(reverse("token_obtain_pair"), payload, format="multipart").status_code, status.HTTP_403_FORBIDDEN)
        csrf = self.csrf_token(client)
        self.assertEqual(client.post(reverse("token_obtain_pair"), payload, format="json", HTTP_X_CSRFTOKEN="wrong").status_code, status.HTTP_403_FORBIDDEN)
        accepted = client.post(reverse("token_obtain_pair"), payload, format="json", HTTP_X_CSRFTOKEN=csrf)
        self.assertEqual(accepted.status_code, status.HTTP_200_OK)
        self.assertIn(settings.REFRESH_COOKIE_NAME, accepted.cookies)
        self.assertNotIn("refresh", accepted.data)

    def csrf_token(self, client):
        response = client.get(reverse("csrf-token"))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return response.data["csrf_token"]

    def refresh(self, client, csrf=None):
        headers = {"HTTP_X_CSRFTOKEN": csrf} if csrf else {}
        return client.post(reverse("token_refresh"), {}, format="json", **headers)

    def test_login_uses_httponly_refresh_cookie_and_no_json_refresh(self):
        client, response = self.login()
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("access", response.data)
        self.assertNotIn("refresh", response.data)
        cookie = response.cookies[settings.REFRESH_COOKIE_NAME]
        self.assertTrue(cookie["httponly"])
        self.assertEqual(cookie["path"], settings.REFRESH_COOKIE_PATH)
        self.assertEqual(cookie["samesite"], settings.REFRESH_COOKIE_SAMESITE)
        self.assertTrue(client.cookies[settings.REFRESH_COOKIE_NAME].value)

    @override_settings(REFRESH_COOKIE_SECURE=True)
    def test_production_cookie_configuration_marks_refresh_secure(self):
        _client, response = self.login()
        self.assertTrue(response.cookies[settings.REFRESH_COOKIE_NAME]["secure"])

    def test_refresh_and_logout_require_csrf_and_logout_clears_cookie(self):
        client, login = self.login()
        self.assertEqual(login.status_code, status.HTTP_200_OK)
        token = self.csrf_token(client)
        denied = self.refresh(client)
        self.assertEqual(denied.status_code, status.HTTP_403_FORBIDDEN)
        accepted = self.refresh(client, token)
        self.assertEqual(accepted.status_code, status.HTTP_200_OK)
        self.assertIn("access", accepted.data)
        self.assertNotIn("refresh", accepted.data)
        logout = client.post(reverse("logout"), {}, format="json", HTTP_X_CSRFTOKEN=token)
        self.assertEqual(logout.status_code, status.HTTP_200_OK)
        self.assertEqual(logout.cookies[settings.REFRESH_COOKIE_NAME]["max-age"], 0)

    def test_logout_revokes_only_the_presented_access_token(self):
        client_a, login_a = self.login()
        _client_b, login_b = self.login()
        access_a = login_a.data["access"]
        access_b = login_b.data["access"]
        logout = client_a.post(
            reverse("logout"), {}, format="json",
            HTTP_X_CSRFTOKEN=self.csrf_token(client_a),
            HTTP_AUTHORIZATION=f"Bearer {access_a}",
        )
        self.assertEqual(logout.status_code, status.HTTP_200_OK)
        self.assertEqual(RevokedAccessToken.objects.count(), 1)
        self.assertNotIn(access_a, RevokedAccessToken.objects.values_list("jti", flat=True))

        revoked_client = APIClient()
        revoked_client.credentials(HTTP_AUTHORIZATION=f"Bearer {access_a}")
        self.assertEqual(revoked_client.get(reverse("me")).status_code, status.HTTP_401_UNAUTHORIZED)
        other_client = APIClient()
        other_client.credentials(HTTP_AUTHORIZATION=f"Bearer {access_b}")
        self.assertEqual(other_client.get(reverse("me")).status_code, status.HTTP_200_OK)

        repeated = client_a.post(reverse("logout"), {}, format="json", HTTP_X_CSRFTOKEN=self.csrf_token(client_a))
        self.assertEqual(repeated.status_code, status.HTTP_200_OK)
        self.assertEqual(RevokedAccessToken.objects.count(), 1)

    def test_staff_session_and_csrf_rotate_then_logout_together(self):
        self.user.is_staff = True
        self.user.is_superuser = True
        self.user.save(update_fields=["is_staff", "is_superuser"])
        client = APIClient(enforce_csrf_checks=True)
        old_csrf = self.csrf_token(client)
        old_csrf_cookie = client.cookies["csrftoken"].value
        login = client.post(reverse("staff-login"), {
            "username": self.user.username,
            "password": self.password,
        }, format="json", HTTP_X_CSRFTOKEN=old_csrf)
        self.assertEqual(login.status_code, status.HTTP_200_OK)
        self.assertEqual(str(client.session.get("_auth_user_id")), str(self.user.pk))
        self.assertNotEqual(client.cookies["csrftoken"].value, old_csrf_cookie)

        denied = client.post(reverse("logout"), {}, format="json", HTTP_X_CSRFTOKEN=old_csrf)
        self.assertEqual(denied.status_code, status.HTTP_403_FORBIDDEN)
        new_csrf = self.csrf_token(client)
        accepted = client.post(
            reverse("logout"), {}, format="json",
            HTTP_X_CSRFTOKEN=new_csrf,
            HTTP_AUTHORIZATION=f"Bearer {login.data['access']}",
        )
        self.assertEqual(accepted.status_code, status.HTTP_200_OK)
        self.assertIsNone(client.session.get("_auth_user_id"))

    def test_rotated_refresh_blacklists_the_previous_cookie(self):
        client, login = self.login()
        old_refresh = login.cookies[settings.REFRESH_COOKIE_NAME].value
        csrf = self.csrf_token(client)
        rotated = self.refresh(client, csrf)
        self.assertEqual(rotated.status_code, status.HTTP_200_OK)
        client.cookies[settings.REFRESH_COOKIE_NAME] = old_refresh
        rejected = self.refresh(client, csrf)
        self.assertEqual(rejected.status_code, status.HTTP_401_UNAUTHORIZED)

    @patch("journal.auth_views.record_audit")
    def test_refresh_replay_audit_remains_in_the_web_adapter_flow(self, record_audit):
        client, login = self.login()
        old_refresh = login.cookies[settings.REFRESH_COOKIE_NAME].value
        csrf = self.csrf_token(client)
        self.assertEqual(self.refresh(client, csrf).status_code, status.HTTP_200_OK)
        client.cookies[settings.REFRESH_COOKIE_NAME] = old_refresh

        rejected = self.refresh(client, csrf)

        self.assertEqual(rejected.status_code, status.HTTP_401_UNAUTHORIZED)
        replay_call = next(
            call for call in record_audit.call_args_list
            if call.kwargs.get("action") == "security.refresh_replay_rejected"
        )
        self.assertEqual(replay_call.kwargs["target"], self.user)
        self.assertEqual(len(replay_call.kwargs["metadata"]["token_jti_hash"]), 16)

    def assert_old_session_revoked(self, access, refresh):
        access_client = APIClient()
        access_client.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")
        self.assertEqual(access_client.get(reverse("me")).status_code, status.HTTP_401_UNAUTHORIZED)
        refresh_client = APIClient(enforce_csrf_checks=True)
        csrf = self.csrf_token(refresh_client)
        refresh_client.cookies[settings.REFRESH_COOKIE_NAME] = refresh
        self.assertEqual(self.refresh(refresh_client, csrf).status_code, status.HTTP_401_UNAUTHORIZED)

    def test_password_change_revokes_old_access_and_refresh(self):
        client, login = self.login()
        access = login.data["access"]
        refresh = login.cookies[settings.REFRESH_COOKIE_NAME].value
        authenticated = APIClient()
        authenticated.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")
        changed = authenticated.post(reverse("password-change"), {
            "current_password": self.password,
            "password": "ChangedPass456!",
            "password_confirm": "ChangedPass456!",
        }, format="json")
        self.assertEqual(changed.status_code, status.HTTP_200_OK)
        self.assert_old_session_revoked(access, refresh)
        self.assertEqual(self.login(password=self.password)[1].status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertEqual(self.login(password="ChangedPass456!")[1].status_code, status.HTTP_200_OK)

    def test_password_reset_and_staff_reset_revoke_existing_tokens(self):
        _client, login = self.login()
        access = login.data["access"]
        refresh = login.cookies[settings.REFRESH_COOKIE_NAME].value
        self.user.refresh_from_db()
        uid = urlsafe_base64_encode(force_bytes(self.user.pk))
        token = default_token_generator.make_token(self.user)
        reset = APIClient().post(reverse("password-reset-confirm"), {
            "uid": uid,
            "token": token,
            "password": "ResetPass456!",
            "password_confirm": "ResetPass456!",
        }, format="json")
        self.assertEqual(reset.status_code, status.HTTP_200_OK)
        self.assert_old_session_revoked(access, refresh)

        cache.clear()
        _client, relogin = self.login(password="ResetPass456!")
        second_access = relogin.data["access"]
        second_refresh = relogin.cookies[settings.REFRESH_COOKIE_NAME].value
        admin = User.objects.create_superuser(username="security-admin", email="admin@example.com", password=self.password)
        staff_client = APIClient()
        staff_client.force_authenticate(admin)
        forced = staff_client.post(reverse("staff-user-action", kwargs={"pk": self.user.pk, "action": "reset-password"}), {
            "password": "AdminReset789!",
            "password_confirm": "AdminReset789!",
        }, format="json")
        self.assertEqual(forced.status_code, status.HTTP_200_OK)
        self.assert_old_session_revoked(second_access, second_refresh)


@override_settings(REST_FRAMEWORK=RELAXED_THROTTLE_SETTINGS)
class PostgreSQLRefreshTokenTests(TransactionTestCase):
    def setUp(self):
        if connection.vendor != "postgresql":
            self.skipTest("Refresh row-lock regression requires PostgreSQL")
        cache.clear()
        self.user = User.objects.create_user(
            username="postgres-refresh-user",
            email="postgres-refresh@example.com",
            password="StrongPass123!",
        )

    def test_legacy_refresh_cookie_returns_401_instead_of_lock_error(self):
        client = APIClient(enforce_csrf_checks=True)
        csrf_response = client.get(reverse("csrf-token"))
        self.assertEqual(csrf_response.status_code, status.HTTP_200_OK)

        legacy_refresh = RefreshToken.for_user(self.user)
        client.cookies[settings.REFRESH_COOKIE_NAME] = str(legacy_refresh)
        response = client.post(
            reverse("token_refresh"),
            {},
            format="json",
            HTTP_X_CSRFTOKEN=csrf_response.data["csrf_token"],
        )

        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertEqual(response.data["code"], "session_expired")
        self.assertEqual(response.cookies[settings.REFRESH_COOKIE_NAME]["max-age"], 0)
        self.assertFalse(BlacklistedToken.objects.filter(token__jti=legacy_refresh["jti"]).exists())


@override_settings(REST_FRAMEWORK=RELAXED_THROTTLE_SETTINGS)
class TwoFactorSecurityTests(APITestCase):
    password = "StrongPass123!"

    def setUp(self):
        cache.clear()
        self.admin = User.objects.create_superuser(
            username="two-factor-admin",
            email="two.factor@example.com",
            password=self.password,
        )
        self.client.force_authenticate(self.admin)

    def begin(self, **extra):
        return self.post2fa({
            "action": "begin",
            "password": self.password,
            **extra,
        })

    def post2fa(self, payload):
        cache.clear()
        return self.client.post(reverse("staff-two-factor"), payload, format="json")

    def enable(self):
        begin = self.begin()
        self.assertEqual(begin.status_code, status.HTTP_200_OK)
        confirmed = self.post2fa({
            "action": "confirm",
            "code": _totp_at(begin.data["secret"], time.time()),
        })
        self.assertEqual(confirmed.status_code, status.HTTP_200_OK)
        return begin.data["secret"], confirmed.data["recovery_codes"]

    def test_staff_two_factor_remains_optional_until_enabled(self):
        staff = User.objects.create_user(
            username="optional-two-factor-staff",
            email="optional-two-factor@example.com",
            password=self.password,
            is_staff=True,
        )
        StaffProfile.objects.create(user=staff, role=StaffProfile.Role.REVIEWER)
        client = APIClient(enforce_csrf_checks=True)
        csrf = client.get(reverse("csrf-token")).data["csrf_token"]
        response = client.post(reverse("staff-login"), {
            "username": staff.username,
            "password": self.password,
        }, format="json", HTTP_X_CSRFTOKEN=csrf)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(response.data["admin_access"])
        self.assertIn("access", response.data)

    def test_enabled_two_factor_rejects_password_only_on_both_login_routes(self):
        self.enable()
        token_response = APIClient().post(reverse("token_obtain_pair"), {
            "username": self.admin.username,
            "password": self.password,
        }, format="json")
        self.assertEqual(token_response.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertNotIn("access", token_response.data)

        client = APIClient(enforce_csrf_checks=True)
        csrf = client.get(reverse("csrf-token")).data["csrf_token"]
        staff_response = client.post(reverse("staff-login"), {
            "username": self.admin.username,
            "password": self.password,
        }, format="json", HTTP_X_CSRFTOKEN=csrf)
        self.assertEqual(staff_response.status_code, status.HTTP_428_PRECONDITION_REQUIRED)
        self.assertNotIn("access", staff_response.data)

    def test_first_bind_uses_pending_secret_and_standard_uri(self):
        begin = self.begin()
        self.assertEqual(begin.status_code, status.HTTP_200_OK)
        self.assertEqual(begin["Cache-Control"], "no-store")
        parsed = urlparse(begin.data["otpauth_uri"])
        self.assertEqual(parsed.scheme, "otpauth")
        self.assertEqual(parsed.netloc, "totp")
        self.assertIn("Anime Journal", unquote(parsed.path))
        query = parse_qs(parsed.query)
        self.assertEqual(query["algorithm"], ["SHA1"])
        self.assertEqual(query["digits"], ["6"])
        self.assertEqual(query["period"], ["30"])
        profile = UserSecurityProfile.objects.get(user=self.admin)
        self.assertFalse(profile.two_factor_enabled)
        self.assertEqual(profile.totp_secret_encrypted, "")
        self.assertNotEqual(profile.pending_totp_secret_encrypted, begin.data["secret"])

        wrong = self.post2fa({"action": "confirm", "code": "000000"})
        self.assertEqual(wrong.status_code, status.HTTP_400_BAD_REQUEST)
        confirmed = self.post2fa({
            "action": "confirm",
            "code": _totp_at(begin.data["secret"], time.time()),
        })
        self.assertEqual(confirmed.status_code, status.HTTP_200_OK)
        profile.refresh_from_db()
        self.assertTrue(profile.two_factor_enabled)
        self.assertEqual(profile.get_totp_secret(), begin.data["secret"])
        self.assertFalse(profile.pending_totp_secret_encrypted)
        for recovery_code in confirmed.data["recovery_codes"]:
            self.assertNotIn(recovery_code, profile.recovery_code_hashes)
        self.assertEqual(len(confirmed.data["recovery_codes"]), TOTP_RECOVERY_CODE_COUNT)
        self.assertEqual(len(profile.recovery_code_hashes), TOTP_RECOVERY_CODE_COUNT)

    def test_expired_pending_secret_is_cleared(self):
        begin = self.begin()
        profile = UserSecurityProfile.objects.get(user=self.admin)
        profile.pending_totp_created_at = timezone.now() - timedelta(minutes=11)
        profile.save(update_fields=["pending_totp_created_at", "updated_at"])
        expired = self.post2fa({
            "action": "confirm",
            "code": _totp_at(begin.data["secret"], time.time()),
        })
        self.assertEqual(expired.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(expired.data["detail"], "二维码已过期，请重新生成。")
        profile.refresh_from_db()
        self.assertFalse(profile.pending_totp_secret_encrypted)

    def test_rebind_keeps_old_secret_until_new_code_is_confirmed(self):
        old_secret, _codes = self.enable()
        missing_old_code = self.begin()
        self.assertEqual(missing_old_code.status_code, status.HTTP_400_BAD_REQUEST)
        rebind = self.begin(current_code=_totp_at(old_secret, time.time()))
        self.assertEqual(rebind.status_code, status.HTTP_200_OK)
        profile = UserSecurityProfile.objects.get(user=self.admin)
        self.assertTrue(profile.two_factor_enabled)
        self.assertEqual(profile.get_totp_secret(), old_secret)
        self.assertNotEqual(rebind.data["secret"], old_secret)
        confirmed = self.post2fa({
            "action": "confirm",
            "code": _totp_at(rebind.data["secret"], time.time()),
        })
        self.assertEqual(confirmed.status_code, status.HTTP_200_OK)
        profile.refresh_from_db()
        self.assertEqual(profile.get_totp_secret(), rebind.data["secret"])

    def test_disable_accepts_one_time_recovery_code_and_clears_security_data(self):
        _secret, recovery_codes = self.enable()
        profile = UserSecurityProfile.objects.get(user=self.admin)
        disposable = "AAAA-BBBB-CCCC"
        profile.recovery_code_hashes = hash_recovery_codes([disposable])
        profile.save(update_fields=["recovery_code_hashes", "updated_at"])
        self.assertTrue(consume_recovery_code(profile, disposable))
        profile.refresh_from_db()
        self.assertFalse(consume_recovery_code(profile, disposable))

        profile.recovery_code_hashes = hash_recovery_codes([recovery_codes[0]])
        profile.save(update_fields=["recovery_code_hashes", "updated_at"])
        disabled = self.post2fa({
            "action": "disable",
            "password": self.password,
            "recovery_code": recovery_codes[0],
        })
        self.assertEqual(disabled.status_code, status.HTTP_200_OK)
        profile.refresh_from_db()
        self.assertFalse(profile.two_factor_enabled)
        self.assertEqual(profile.totp_secret_encrypted, "")
        self.assertEqual(profile.recovery_code_hashes, [])

    def test_recovery_code_logs_in_once_and_failed_two_factor_issues_no_token(self):
        _secret, codes = self.enable()
        OutstandingToken.objects.all().delete()
        self.admin.last_login = None
        self.admin.save(update_fields=["last_login"])

        failed = APIClient().post(reverse("token_obtain_pair"), {
            "username": self.admin.username,
            "password": self.password,
            "otp": "000000",
        }, format="json")
        self.assertEqual(failed.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertEqual(OutstandingToken.objects.count(), 0)
        self.admin.refresh_from_db()
        self.assertIsNone(self.admin.last_login)

        recovered = APIClient().post(reverse("token_obtain_pair"), {
            "username": self.admin.username,
            "password": self.password,
            "recovery_code": codes[0].lower(),
        }, format="json")
        self.assertEqual(recovered.status_code, status.HTTP_200_OK)
        self.assertTrue(recovered.data["used_recovery_code"])
        self.assertEqual(recovered.data["remaining_recovery_codes"], 5)
        cache.clear()
        reused = APIClient().post(reverse("token_obtain_pair"), {
            "username": self.admin.username,
            "password": self.password,
            "recovery_code": codes[0],
        }, format="json")
        self.assertEqual(reused.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_staff_login_accepts_recovery_code(self):
        _secret, codes = self.enable()
        client = APIClient(enforce_csrf_checks=True)
        csrf = client.get(reverse("csrf-token")).data["csrf_token"]
        response = client.post(reverse("staff-login"), {
            "username": self.admin.email,
            "password": self.password,
            "recovery_code": f"  {codes[0].lower()}  ",
        }, format="json", HTTP_X_CSRFTOKEN=csrf)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["used_recovery_code"])
        self.assertEqual(response.data["remaining_recovery_codes"], 5)


class PluginRuntimeSecurityTests(APITestCase):
    def setUp(self):
        cache.clear()
        self.admin = User.objects.create_user(username="plugin-security-admin", password="StrongPass123!", is_staff=True)
        self.client.force_authenticate(self.admin)
        project = PluginProject.objects.create(
            plugin_id="com.anime-journal.watch-history-importer", slug="watch-history-importer",
            name="Watch history", description="test", installation_mode=PluginProject.InstallationMode.SYSTEM,
        )
        blob = PluginPackageBlob.objects.create(sha256="0" * 64, size_bytes=0, storage_path="packages/sha256/00/" + "0" * 64 + ".ajplugin")
        version = PluginVersion.objects.create(
            plugin=project, version="0.2.0", package_blob=blob, manifest_snapshot={}, runtime_types=["frontend", "backend"],
            review_status=PluginVersion.ReviewStatus.APPROVED, published_at=timezone.now(),
        )
        self.deployment = PluginDeployment.objects.create(
            plugin=project, current_version=version, enabled=True, healthy=True, updated_by=self.admin,
        )

    def test_enabled_plugin_metadata_only_lists_effective_frontends(self):
        self.client.force_authenticate(user=None)
        response = self.client.get(reverse("enabled-plugins"))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["plugins"], [])

    def test_disabled_plugin_api_returns_404_even_for_admin(self):
        PluginDeployment.objects.filter(pk=self.deployment.pk).update(enabled=False)
        response = self.client.get("/api/plugins/watch-history-importer/status/")
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

class ThrottleSecurityTests(APITestCase):
    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(
            username="throttle-user",
            email="throttle@example.com",
            password="StrongPass123!",
        )
        self.admin = User.objects.create_superuser(
            username="throttle-admin",
            email="throttle-admin@example.com",
            password="StrongPass123!",
        )
        self.rates = {
            "anon": "1000/min",
            "user": "1000/min",
            "login": "2/min",
            "password_reset": "2/hour",
            "two_factor": "2/min",
            "external_search": "2/min",
            "import": "1/hour",
        }

    def test_login_and_two_factor_attempts_return_429_after_limit(self):
        with patch.dict(SimpleRateThrottle.THROTTLE_RATES, self.rates, clear=True):
            client = APIClient()
            responses = [client.post(reverse("token_obtain_pair"), {
                "username": self.user.username,
                "password": "wrong-password",
            }, format="json") for _index in range(3)]
            self.assertEqual(responses[-1].status_code, status.HTTP_429_TOO_MANY_REQUESTS)
            self.assertIn("Retry-After", responses[-1])

            cache.clear()
            client.force_authenticate(self.admin)
            responses = [client.post(reverse("staff-two-factor"), {
                "action": "begin",
                "password": "wrong-password",
            }, format="json") for _index in range(3)]
            self.assertEqual(responses[-1].status_code, status.HTTP_429_TOO_MANY_REQUESTS)

    @patch("journal.auth_views.send_transactional_email")
    def test_password_reset_is_throttled_without_disclosing_account_existence(self, send_email):
        with patch.dict(SimpleRateThrottle.THROTTLE_RATES, self.rates, clear=True):
            client = APIClient()
            existing = client.post(reverse("password-reset"), {"email": self.user.email}, format="json")
            missing = client.post(reverse("password-reset"), {"email": "missing@example.com"}, format="json")
            limited = client.post(reverse("password-reset"), {"email": "third@example.com"}, format="json")
        self.assertEqual(existing.status_code, status.HTTP_200_OK)
        self.assertEqual(missing.status_code, status.HTTP_200_OK)
        self.assertEqual(existing.data, missing.data)
        self.assertEqual(limited.status_code, status.HTTP_429_TOO_MANY_REQUESTS)
        self.assertEqual(send_email.call_count, 1)

    @patch("journal.external_media.views.get_provider")
    def test_external_search_and_import_have_independent_scopes(self, get_provider):
        get_provider.return_value.slug = "bangumi"
        get_provider.return_value.search.return_value = []
        with patch.dict(SimpleRateThrottle.THROTTLE_RATES, self.rates, clear=True):
            client = APIClient()
            search_responses = [client.get(
                reverse("external-media-search", kwargs={"provider": "bangumi"}),
                {"q": f"测试番剧{index}"},
            ) for index in range(3)]
            self.assertEqual(search_responses[-1].status_code, status.HTTP_429_TOO_MANY_REQUESTS)
            self.assertEqual(get_provider.return_value.search.call_count, 2)

            cache.clear()
            client.force_authenticate(self.user)
            payload = {
                "format": "animemo-data-bundle",
                "schema_version": 1,
                "exported_at": "2026-08-09T00:00:00Z",
                "entries": [],
            }
            endpoint = f'{reverse("import")}?preview=true'
            first = client.post(endpoint, payload, format="json")
            second = client.post(endpoint, payload, format="json")
            self.assertEqual(first.status_code, status.HTTP_200_OK)
            self.assertEqual(second.status_code, status.HTTP_429_TOO_MANY_REQUESTS)

    def test_account_keys_are_global_hashed_and_combined(self):
        request = RequestFactory().post("/api/token/", {"username": " Victim@Example.com "})
        request.data = {"username": " Victim@Example.com "}
        request.query_params = {}
        request.user = None
        view = type("LoginView", (), {"account_throttle_scope": "login", "throttle_account_fields": ("username",)})()
        throttle = HashedAccountRateThrottle()
        dimensions = throttle.get_cache_dimensions(request, view)
        self.assertEqual(len(dimensions), 3)
        keys = [key for key, _rate in dimensions]
        self.assertTrue(all("victim@example.com" not in key.lower() for key in keys))
        request.META["REMOTE_ADDR"] = "203.0.113.77"
        changed = throttle.get_cache_dimensions(request, view)
        self.assertEqual(keys[1], changed[1][0])
        self.assertNotEqual(keys[0], changed[0][0])
        self.assertNotEqual(keys[2], changed[2][0])

    @override_settings(REDIS_URL="redis://127.0.0.1:1/0", AUTH_THROTTLE_FAIL_CLOSED=True)
    @patch("journal.throttling.get_redis_connection", side_effect=OSError("redis unavailable"))
    def test_authentication_throttle_fails_closed_when_redis_is_unavailable(self, _connection):
        throttle = HashedAccountRateThrottle()
        with self.assertRaises(AuthThrottleUnavailable):
            throttle._atomic_allow([("test-auth-key", 2, 60)], time.time())


class CsvAndImportSecurityTests(APITestCase):
    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(username="import-user", password="StrongPass123!")
        self.client.force_authenticate(self.user)

    def test_csv_formula_values_are_escaped(self):
        for value in ("=1+1", "+SUM(A1:A2)", "-1+2", "@cmd", " =1+1", "\t=1+1", "\r=1+1", "\n=1+1"):
            with self.subTest(value=value):
                self.assertEqual(safe_csv_value(value), "'" + value)
        self.assertEqual(safe_csv_value("正常标题"), "正常标题")
        self.assertEqual(safe_csv_value(123), "123")

    @override_settings(IMPORT_FILE_MAX_BYTES=64)
    def test_oversized_file_is_rejected_before_parsing(self):
        upload = SimpleUploadedFile("entries.json", b"{" + b" " * 100 + b"}", content_type="application/json")
        response = self.client.post(reverse("import"), {"file": upload, "preview": "true"}, format="multipart")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("不能超过", response.data["detail"])

    @override_settings(IMPORT_FILE_MAX_BYTES=64)
    def test_direct_json_parser_enforces_raw_bytes_without_trusting_content_length(self):
        parser = LimitedImportJSONParser()
        exact = b'{"records":[]}' + b" " * (64 - len(b'{"records":[]}'))
        request = RequestFactory().post("/api/import/", data=b"", content_type="application/json")
        request.META["CONTENT_LENGTH"] = "64"
        self.assertEqual(parser.parse(BytesIO(exact), parser_context={"request": request}), {"records": []})

        oversized = exact + b" "
        request.META["CONTENT_LENGTH"] = "1"
        with self.assertRaises(ParseError):
            parser.parse(BytesIO(oversized), parser_context={"request": request})
        request.META.pop("CONTENT_LENGTH", None)
        with self.assertRaises(ParseError):
            parser.parse(BytesIO(oversized), parser_context={"request": request})

    def test_direct_json_parser_rejects_null_invalid_utf8_and_syntax(self):
        parser = LimitedImportJSONParser()
        request = RequestFactory().post("/api/import/", data=b"", content_type="application/json")
        for raw in (b'{"records":["\x00"]}', b'{"records":["\xff"]}', b'{"records":['):
            with self.subTest(raw=raw), self.assertRaises(ParseError):
                parser.parse(BytesIO(raw), parser_context={"request": request})

    def test_csv_and_json_record_limits_are_rejected(self):
        records = [{"title": f"记录 {index}"} for index in range(settings.IMPORT_MAX_RECORDS + 1)]
        json_response = self.client.post(reverse("import"), {"records": records, "preview": True}, format="json")
        self.assertEqual(json_response.status_code, status.HTTP_400_BAD_REQUEST)

        csv_text = "title\n" + "\n".join(f"记录 {index}" for index in range(settings.IMPORT_MAX_RECORDS + 1))
        upload = SimpleUploadedFile("entries.csv", csv_text.encode("utf-8"), content_type="text/csv")
        csv_response = self.client.post(reverse("import"), {"file": upload, "preview": "true"}, format="multipart")
        self.assertEqual(csv_response.status_code, status.HTTP_400_BAD_REQUEST)

    @override_settings(IMPORT_FIELD_MAX_LENGTH=12)
    def test_overlong_field_is_rejected_without_creating_entries(self):
        response = self.client.post(reverse("import"), {
            "records": [{"title": "这是一条明显超过限制的番剧标题"}],
        }, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(JournalEntry.objects.filter(user=self.user).count(), 0)

    @override_settings(IMPORT_MAX_NESTING_DEPTH=2)
    def test_direct_json_depth_failure_is_all_or_nothing(self):
        response = self.client.post(reverse("import"), {
            "records": [
                {"title": "有效记录"},
                {"title": "无效记录", "tags": [[[["过深"]]]]},
            ],
        }, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(JournalEntry.objects.filter(user=self.user).count(), 0)


@override_settings(REST_FRAMEWORK=RELAXED_THROTTLE_SETTINGS)
class AccountDeletionSecurityTests(APITestCase):
    password = "StrongPass123!"

    def setUp(self):
        cache.clear()

    def make_two_factor(self, user, recovery_codes=None):
        profile, _ = UserSecurityProfile.objects.get_or_create(user=user)
        profile.set_totp_secret("JBSWY3DPEHPK3PXP")
        profile.two_factor_enabled = True
        profile.recovery_code_hashes = hash_recovery_codes(recovery_codes or [])
        profile.save(update_fields=["totp_secret_encrypted", "two_factor_enabled", "recovery_code_hashes", "updated_at"])
        return profile

    def test_staff_password_alone_is_rejected_and_audited(self):
        staff = User.objects.create_user(username="self-staff", password=self.password, is_staff=True)
        self.client.force_authenticate(staff)
        response = self.client.delete(reverse("account"), {"current_password": self.password}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertTrue(User.objects.filter(pk=staff.pk).exists())
        self.assertTrue(AdminAuditLog.objects.filter(action="security.account_deletion_rejected", metadata__reason="two_factor_not_enabled").exists())

    def test_staff_can_delete_with_totp_and_all_sessions_are_revoked(self):
        staff = User.objects.create_user(username="totp-staff", password=self.password, is_staff=True)
        self.make_two_factor(staff)
        self.client.force_authenticate(staff)
        code = _totp_at("JBSWY3DPEHPK3PXP", time.time())
        response = self.client.delete(reverse("account"), {"current_password": self.password, "otp": code}, format="json")
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(User.objects.filter(pk=staff.pk).exists())
        self.assertTrue(AdminAuditLog.objects.filter(action="security.account_deleted", before__username="totp-staff").exists())

    def test_recovery_code_is_consumed_once_for_staff_deletion(self):
        staff = User.objects.create_user(username="recovery-staff", password=self.password, is_staff=True)
        code = "AAAA-BBBB-CCCC"
        self.make_two_factor(staff, [code])
        self.client.force_authenticate(staff)
        response = self.client.delete(reverse("account"), {"current_password": self.password, "recovery_code": code}, format="json")
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(User.objects.filter(pk=staff.pk).exists())

    def test_last_active_superuser_cannot_self_delete(self):
        admin = User.objects.create_superuser(username="last-admin", email="last-admin@example.com", password=self.password)
        self.make_two_factor(admin)
        self.client.force_authenticate(admin)
        code = _totp_at("JBSWY3DPEHPK3PXP", time.time())
        response = self.client.delete(reverse("account"), {"current_password": self.password, "otp": code}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("最后一个有效超级管理员", str(response.data))
        self.assertTrue(User.objects.filter(pk=admin.pk).exists())


class ImageUploadSecurityTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="image-security", password="StrongPass123!")

    @staticmethod
    def image_upload(name="cover.png", fmt="PNG", size=(64, 96)):
        output = BytesIO()
        Image.new("RGBA", size, (255, 0, 128, 220)).save(output, format=fmt)
        return SimpleUploadedFile(name, output.getvalue(), content_type="image/png")

    def test_all_core_image_serializers_reencode_to_static_webp(self):
        site = SiteSettings.load()
        site_result = SiteSettingsSerializer(instance=site, data={"site_avatar": self.image_upload()}, partial=True)
        self.assertTrue(site_result.is_valid(), site_result.errors)
        self.assertEqual(Image.open(site_result.validated_data["site_avatar"]).format, "WEBP")

        settings_obj = UserSettings.objects.get_or_create(user=self.user)[0]
        user_result = UserSettingsSerializer(instance=settings_obj, data={"avatar": self.image_upload("avatar.webp", "WEBP")}, partial=True)
        self.assertTrue(user_result.is_valid(), user_result.errors)
        self.assertEqual(Image.open(user_result.validated_data["avatar"]).format, "WEBP")

        entry = JournalEntry.objects.create(user=self.user, title="图片测试")
        entry_result = JournalEntrySerializer(instance=entry, data={"poster_file": self.image_upload()}, partial=True)
        self.assertTrue(entry_result.is_valid(), entry_result.errors)
        self.assertEqual(Image.open(entry_result.validated_data["poster_file"]).format, "WEBP")

        column = Column.objects.create(author=self.user, title="封面测试", body="内容")
        column_result = ColumnSerializer(instance=column, data={"cover": self.image_upload()}, partial=True)
        self.assertTrue(column_result.is_valid(), column_result.errors)
        self.assertEqual(Image.open(column_result.validated_data["cover"]).format, "WEBP")

    def test_fake_image_and_dynamic_image_are_rejected(self):
        fake = SimpleUploadedFile("fake.png", b"not an image", content_type="image/png")
        settings_obj = UserSettings.objects.get_or_create(user=self.user)[0]
        fake_result = UserSettingsSerializer(instance=settings_obj, data={"avatar": fake}, partial=True)
        self.assertFalse(fake_result.is_valid())

        animated = BytesIO()
        Image.new("RGB", (32, 32), "red").save(animated, format="GIF", save_all=True, append_images=[Image.new("RGB", (32, 32), "blue")], duration=30, loop=0)
        animated_result = UserSettingsSerializer(instance=settings_obj, data={"avatar": SimpleUploadedFile("avatar.png", animated.getvalue(), content_type="image/png")}, partial=True)
        self.assertFalse(animated_result.is_valid())


@override_settings(STORAGES={
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
})
class MediaLifecycleSecurityTests(TransactionTestCase):
    def setUp(self):
        self.media_dir = TemporaryDirectory()
        self.settings_override = override_settings(MEDIA_ROOT=self.media_dir.name)
        self.settings_override.enable()
        self.user = User.objects.create_user(username="media-owner", password="StrongPass123!")

    def tearDown(self):
        self.settings_override.disable()
        self.media_dir.cleanup()
        super().tearDown()

    def _upload(self, name):
        output = BytesIO()
        Image.new("RGB", (48, 72), "#ff4f8b").save(output, format="PNG")
        return SimpleUploadedFile(name, output.getvalue(), content_type="image/png")

    def test_delete_models_removes_owned_files_after_commit(self):
        settings_obj = UserSettings.objects.create(user=self.user)
        settings_obj.avatar = self._upload("avatar.png")
        settings_obj.save()
        entry = JournalEntry.objects.create(user=self.user, title="媒体番剧")
        entry.poster_file = self._upload("poster.png")
        entry.save()
        column = Column.objects.create(author=self.user, title="媒体专栏", body="正文")
        column.cover = self._upload("cover.png")
        column.save()

        names = [settings_obj.avatar.name, entry.poster_file.name, column.cover.name]
        for name in names:
            self.assertTrue(default_storage.exists(name))
        settings_obj.delete()
        entry.delete()
        column.delete()
        for name in names:
            self.assertFalse(default_storage.exists(name))

    def test_user_cascade_cleans_related_files(self):
        settings_obj = UserSettings.objects.create(user=self.user)
        settings_obj.avatar = self._upload("avatar.png")
        settings_obj.save()
        entry = JournalEntry.objects.create(user=self.user, title="级联番剧")
        entry.poster_file = self._upload("poster.png")
        entry.save()
        names = [settings_obj.avatar.name, entry.poster_file.name]
        self.user.delete()
        self.assertFalse(User.objects.filter(pk=self.user.pk).exists())
        for name in names:
            self.assertFalse(default_storage.exists(name))

    def test_rollback_does_not_delete_file(self):
        entry = JournalEntry.objects.create(user=self.user, title="回滚番剧")
        entry.poster_file = self._upload("poster.png")
        entry.save()
        primary_key = entry.pk
        name = entry.poster_file.name
        try:
            with transaction.atomic():
                entry.delete()
                raise RuntimeError("rollback")
        except RuntimeError:
            pass
        self.assertTrue(default_storage.exists(name))
        restored = JournalEntry.objects.get(pk=primary_key)
        self.assertEqual(restored.poster_file.name, name)

    def test_repeated_file_delete_is_idempotent(self):
        entry = JournalEntry.objects.create(user=self.user, title="重复删除")
        entry.poster_file = self._upload("poster.png")
        entry.save()
        primary_key = entry.pk
        entry.delete()
        JournalEntry.objects.filter(pk=primary_key).delete()


class EmailUniquenessSecurityTests(APITestCase):
    def test_email_is_normalized_and_case_insensitive_uniqueness_is_database_enforced(self):
        first = User.objects.create_user(username="email-one", email="  Mixed.Case@Example.COM ", password="StrongPass123!")
        self.assertEqual(first.email, "mixed.case@example.com")
        with self.assertRaises(IntegrityError), transaction.atomic():
            User.objects.create_user(username="email-two", email="MIXED.CASE@example.com", password="StrongPass123!")

    def test_registration_request_normalizes_duplicate_email_without_creating_user(self):
        User.objects.create_user(username="registered", email="registered@example.com", password="StrongPass123!")
        serializer = RegistrationRequestSerializer(data={"email": " Registered@Example.com ", "password": "attacker-password"})
        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertEqual(serializer.validated_data["email"], "registered@example.com")
        self.assertFalse(PendingRegistration.objects.filter(email="registered@example.com").exists())


@override_settings(REST_FRAMEWORK=RELAXED_THROTTLE_SETTINGS)
class UsernameUniquenessSecurityTests(APITestCase):
    password = "StrongPass123!"

    def setUp(self):
        cache.clear()

    def test_username_is_trimmed_and_database_unique_without_case(self):
        first = User.objects.create_user(username="  Alice  ", email="alice-one@example.com", password=self.password)
        self.assertEqual(first.username, "Alice")
        with self.assertRaises(IntegrityError), transaction.atomic():
            User.objects.create_user(username="alice", email="alice-two@example.com", password=self.password)
        with self.assertRaises(IntegrityError), transaction.atomic():
            User.objects.create_user(username="ALICE", email="alice-three@example.com", password=self.password)

    def test_registration_rejects_case_insensitive_username_with_friendly_error(self):
        User.objects.create_user(username="MixedName", email="mixed-one@example.com", password=self.password)
        serializer = RegistrationCompleteSerializer(data={
            "completion_token": "placeholder",
            "username": " mixedname ",
            "password": self.password,
            "password_confirm": self.password,
        })
        self.assertFalse(serializer.is_valid())
        self.assertIn("username", serializer.errors)

    def test_case_insensitive_login_still_resolves_the_canonical_username(self):
        user = User.objects.create_user(username="DisplayCase", email="display@example.com", password=self.password)
        response = APIClient().post(reverse("token_obtain_pair"), {
            "username": "displaycase",
            "password": self.password,
        }, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["user"]["id"], user.pk)

    def test_registration_request_never_accepts_password_as_a_creation_step(self):
        serializer = RegistrationRequestSerializer(data={"email": "unique-name@example.com", "password": self.password})
        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertNotIn("password", serializer.validated_data)


@override_settings(REST_FRAMEWORK=REGISTER_THROTTLE_SETTINGS)
class RegistrationThrottleSecurityTests(APITestCase):
    password = "StrongPass123!"

    def setUp(self):
        cache.clear()
        site = SiteSettings.load()
        site.registration_enabled = True
        site.email_delivery_enabled = True
        site.save(update_fields=["registration_enabled", "email_delivery_enabled", "updated_at"])

    def register(self, email, *, ip):
        return self.client.post(reverse("register-request"), {"email": email}, format="json", REMOTE_ADDR=ip)

    def test_register_ip_limit_is_atomic_and_returns_retry_after(self):
        self.assertEqual(self.register("ip-one@example.com", ip="198.51.100.10").status_code, status.HTTP_201_CREATED)
        self.assertEqual(self.register("ip-two@example.com", ip="198.51.100.10").status_code, status.HTTP_201_CREATED)
        denied = self.register("ip-three@example.com", ip="198.51.100.10")
        self.assertEqual(denied.status_code, status.HTTP_429_TOO_MANY_REQUESTS)
        self.assertIn("Retry-After", denied)

    def test_register_email_limit_is_case_insensitive_across_ips(self):
        first = self.register("Rate.Email@Example.com", ip="198.51.100.11")
        self.assertEqual(first.status_code, status.HTTP_201_CREATED)
        duplicate = self.register("rate.email@example.com", ip="198.51.100.12")
        self.assertEqual(duplicate.status_code, status.HTTP_201_CREATED)
        denied = self.register("RATE.EMAIL@example.com", ip="198.51.100.13")
        self.assertEqual(denied.status_code, status.HTTP_429_TOO_MANY_REQUESTS)

    def test_register_combined_limit_does_not_affect_unrelated_users(self):
        self.assertEqual(self.register("combo@example.com", ip="198.51.100.20").status_code, status.HTTP_201_CREATED)
        denied = self.register("combo@example.com", ip="198.51.100.20")
        self.assertEqual(denied.status_code, status.HTTP_429_TOO_MANY_REQUESTS)
        unrelated = self.register("other@example.com", ip="198.51.100.21")
        self.assertEqual(unrelated.status_code, status.HTTP_201_CREATED)

    @patch("journal.auth_views.send_transactional_email", side_effect=EmailDeliveryError("provider unavailable"))
    def test_email_failure_after_commit_keeps_pending_without_user(self, _send_email):
        with self.captureOnCommitCallbacks(execute=True):
            response = self.register("mail-failure@example.com", ip="198.51.100.30")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertFalse(User.objects.filter(email="mail-failure@example.com").exists())
        self.assertTrue(PendingRegistration.objects.filter(email="mail-failure@example.com").exists())

    def test_throttle_cache_dimensions_do_not_contain_plaintext_email(self):
        request = type("ThrottleRequest", (), {
            "data": {"email": "Secret.Email@Example.com"},
            "query_params": {},
            "user": type("Anonymous", (), {"is_authenticated": False})(),
            "META": {"REMOTE_ADDR": "198.51.100.40"},
        })()
        view = type("RegisterThrottleView", (), {
            "account_throttle_scope": "register_request",
            "secondary_throttle_scope": None,
            "throttle_account_fields": ("email",),
        })()
        dimensions = HashedAccountRateThrottle().get_cache_dimensions(request, view)
        self.assertTrue(dimensions)
        self.assertNotIn("secret.email@example.com", " ".join(key for key, _rate in dimensions).lower())


@skipUnlessDBFeature("has_select_for_update")
@override_settings(REST_FRAMEWORK=RELAXED_THROTTLE_SETTINGS)
class RefreshRotationConcurrencyTests(TransactionTestCase):
    reset_sequences = True

    def test_same_refresh_token_has_only_one_successor_under_real_concurrency(self):
        user = User.objects.create_user(username="refresh-race", password="StrongPass123!")
        raw_refresh = str(create_refresh_token(user))
        barrier = threading.Barrier(3)

        def refresh_once():
            close_old_connections()
            client = APIClient()
            client.cookies[settings.REFRESH_COOKIE_NAME] = raw_refresh
            barrier.wait()
            response = client.post(reverse("token_refresh"), {}, format="json")
            close_old_connections()
            return response.status_code

        with ThreadPoolExecutor(max_workers=3) as pool:
            statuses = list(pool.map(lambda _index: refresh_once(), range(3)))
        self.assertEqual(statuses.count(status.HTTP_200_OK), 1)
        self.assertEqual(statuses.count(status.HTTP_401_UNAUTHORIZED), 2)
        self.assertEqual(BlacklistedToken.objects.count(), 1)
        self.assertEqual(OutstandingToken.objects.filter(user=user).count(), 2)


@skipUnlessDBFeature("has_select_for_update")
class SuperuserDeletionConcurrencyTests(TransactionTestCase):
    reset_sequences = True

    def test_concurrent_self_deletion_keeps_one_active_superuser(self):
        password = "StrongPass123!"
        users = [
            User.objects.create_superuser(username=f"root-{index}", email=f"root-{index}@example.com", password=password)
            for index in range(2)
        ]
        secret = "JBSWY3DPEHPK3PXP"
        for user in users:
            profile = UserSecurityProfile.objects.create(user=user, two_factor_enabled=True)
            profile.set_totp_secret(secret)
            profile.save(update_fields=["totp_secret_encrypted", "updated_at"])
        barrier = threading.Barrier(2)

        def delete_once(user_id):
            close_old_connections()
            user = User.objects.get(pk=user_id)
            barrier.wait()
            try:
                delete_current_account(
                    user=user,
                    current_password=password,
                    otp=_totp_at(secret, time.time()),
                )
                result = "deleted"
            except AccountDeletionError:
                result = "rejected"
            close_old_connections()
            return result

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(delete_once, [user.pk for user in users]))
        self.assertEqual(results.count("deleted"), 1)
        self.assertEqual(results.count("rejected"), 1)
        self.assertEqual(User.objects.filter(is_superuser=True, is_staff=True, is_active=True).count(), 1)
