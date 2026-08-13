import os
import stat
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth.hashers import check_password
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.management import call_command, CommandError
from django.db import close_old_connections, connection, connections
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase, override_settings, skipUnlessDBFeature
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from .models import InstallationState, SiteSettings
from journal.models import AdminAuditLog, UserSettings
from accounts.models import PendingRegistration
from plugin_host.models import PluginDeployment


User = get_user_model()


@override_settings(TURNSTILE_ENABLED=False)
class FirstRunSetupApiTests(APITestCase):
    def setUp(self):
        cache.clear()
        self.installation = InstallationState.load()
        self.installation.status = InstallationState.Status.UNINITIALIZED
        self.installation.setup_code_hash = ""
        self.installation.setup_code_issued_at = None
        self.installation.setup_code_expires_at = None
        self.installation.failed_attempts = 0
        self.installation.initialized_at = None
        self.installation.initialized_by = None
        self.installation.save()

    def test_uninitialized_installation_exposes_status_without_plaintext_code(self):
        response = self.client.get(reverse("setup-status"))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["state"], InstallationState.Status.UNINITIALIZED)
        self.assertFalse(response.data["accepting_setup"])
        self.assertNotIn("code", response.data)
        self.assertNotIn("code_hash", response.data)

    def test_provision_command_writes_one_private_code_and_activates_setup(self):
        with TemporaryDirectory() as directory:
            private_root = Path(directory) / "private"
            private_root.mkdir(mode=0o700)
            code_path = private_root / "setup-code"
            output = StringIO()

            with override_settings(
                FIRST_RUN_SETUP_CODE_PATH=code_path,
                FIRST_RUN_SETUP_CODE_TTL_SECONDS=3600,
            ):
                call_command("provision_first_run_setup", stdout=output)

            plaintext = code_path.read_text(encoding="utf-8").strip()
            self.installation.refresh_from_db()
            self.assertGreaterEqual(len(plaintext), 32)
            self.assertTrue(check_password(plaintext, self.installation.setup_code_hash))
            self.assertTrue(self.installation.accepting_setup)
            self.assertNotIn(plaintext, output.getvalue())
            if os.name != "nt":
                self.assertEqual(stat.S_IMODE(code_path.stat().st_mode), 0o600)

    def test_application_bootstrap_applies_defaults_before_issuing_setup_code(self):
        SiteSettings.objects.all().delete()
        with TemporaryDirectory() as directory:
            private_root = Path(directory) / "private"
            private_root.mkdir(mode=0o700)
            code_path = private_root / "setup-code"
            with override_settings(FIRST_RUN_SETUP_CODE_PATH=code_path):
                call_command("bootstrap_animemo", verbosity=0)

            self.installation.refresh_from_db()
            self.assertTrue(code_path.is_file())
            self.assertTrue(self.installation.accepting_setup)
            self.assertTrue(SiteSettings.objects.filter(pk=1).exists())

    def test_valid_code_creates_exactly_one_superuser_and_locks_setup(self):
        with TemporaryDirectory() as directory:
            private_root = Path(directory) / "private"
            private_root.mkdir(mode=0o700)
            code_path = private_root / "setup-code"
            with override_settings(FIRST_RUN_SETUP_CODE_PATH=code_path):
                call_command("provision_first_run_setup", verbosity=0)
                code = code_path.read_text(encoding="utf-8").strip()
                csrf = self.client.get(reverse("csrf-token")).data["csrf_token"]
                with self.captureOnCommitCallbacks(execute=True):
                    response = self.client.post(
                        reverse("setup-complete"),
                        {
                            "code": code,
                            "username": "first-admin",
                            "email": "first-admin@example.com",
                            "password": "StrongPass123!RC",
                            "password_confirm": "StrongPass123!RC",
                        },
                        format="json",
                        HTTP_X_CSRFTOKEN=csrf,
                    )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        admin = User.objects.get(username="first-admin")
        self.assertTrue(admin.is_active)
        self.assertTrue(admin.is_staff)
        self.assertTrue(admin.is_superuser)
        self.assertTrue(admin.check_password("StrongPass123!RC"))
        self.assertTrue(UserSettings.objects.filter(user=admin, nickname="first-admin").exists())
        self.installation.refresh_from_db()
        self.assertEqual(self.installation.status, InstallationState.Status.INITIALIZED)
        self.assertEqual(self.installation.initialized_by, admin)
        self.assertEqual(self.installation.setup_code_hash, "")
        self.assertFalse(code_path.exists())
        self.assertTrue(AdminAuditLog.objects.filter(action="installation.initialized", actor=admin).exists())
        self.assertTrue(PluginDeployment.objects.filter(plugin__slug="watch-history-importer", enabled=True).exists())

    def test_plugin_hook_failure_cannot_misreport_a_committed_setup_as_failed(self):
        with TemporaryDirectory() as directory:
            private_root = Path(directory) / "private"
            private_root.mkdir(mode=0o700)
            code_path = private_root / "setup-code"
            with override_settings(FIRST_RUN_SETUP_CODE_PATH=code_path):
                call_command("provision_first_run_setup", verbosity=0)
                code = code_path.read_text(encoding="utf-8").strip()
                csrf = self.client.get(reverse("csrf-token")).data["csrf_token"]
                with patch(
                    "plugin_host.hooks.run_hook",
                    side_effect=RuntimeError("injected plugin hook failure"),
                ):
                    with self.captureOnCommitCallbacks(execute=True):
                        response = self.client.post(
                            reverse("setup-complete"),
                            {
                                "code": code,
                                "username": "hook-failure-admin",
                                "email": "hook-failure-admin@example.com",
                                "password": "StrongPass123!RC",
                                "password_confirm": "StrongPass123!RC",
                            },
                            format="json",
                            HTTP_X_CSRFTOKEN=csrf,
                        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(User.objects.filter(username="hook-failure-admin", is_superuser=True).exists())
        self.assertEqual(InstallationState.load().status, InstallationState.Status.INITIALIZED)
        self.assertFalse(code_path.exists())

    def test_wrong_code_is_rejected_and_consumes_one_persistent_attempt(self):
        with TemporaryDirectory() as directory:
            private_root = Path(directory) / "private"
            private_root.mkdir(mode=0o700)
            code_path = private_root / "setup-code"
            with override_settings(FIRST_RUN_SETUP_CODE_PATH=code_path):
                call_command("provision_first_run_setup", verbosity=0)
                csrf = self.client.get(reverse("csrf-token")).data["csrf_token"]
                response = self.client.post(
                    reverse("setup-complete"),
                    {
                        "code": "definitely-wrong",
                        "username": "first-admin",
                        "email": "first-admin@example.com",
                        "password": "StrongPass123!RC",
                        "password_confirm": "StrongPass123!RC",
                    },
                    format="json",
                    HTTP_X_CSRFTOKEN=csrf,
                )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data["code"], "invalid_setup_code")
        self.installation.refresh_from_db()
        self.assertEqual(self.installation.failed_attempts, 1)
        self.assertTrue(self.installation.accepting_setup)
        self.assertFalse(User.objects.filter(username="first-admin").exists())

    def test_expired_code_is_rejected_invalidated_and_removed(self):
        with TemporaryDirectory() as directory:
            private_root = Path(directory) / "private"
            private_root.mkdir(mode=0o700)
            code_path = private_root / "setup-code"
            with override_settings(FIRST_RUN_SETUP_CODE_PATH=code_path):
                call_command("provision_first_run_setup", verbosity=0)
                code = code_path.read_text(encoding="utf-8").strip()
                self.installation.refresh_from_db()
                self.installation.setup_code_expires_at = timezone.now() - timedelta(seconds=1)
                self.installation.save(update_fields=["setup_code_expires_at", "updated_at"])
                csrf = self.client.get(reverse("csrf-token")).data["csrf_token"]
                response = self.client.post(
                    reverse("setup-complete"),
                    {
                        "code": code,
                        "username": "first-admin",
                        "email": "first-admin@example.com",
                        "password": "StrongPass123!RC",
                        "password_confirm": "StrongPass123!RC",
                    },
                    format="json",
                    HTTP_X_CSRFTOKEN=csrf,
                )

        self.assertEqual(response.status_code, status.HTTP_410_GONE)
        self.assertEqual(response.data["code"], "setup_code_expired")
        self.installation.refresh_from_db()
        self.assertEqual(self.installation.setup_code_hash, "")
        self.assertFalse(code_path.exists())
        self.assertFalse(User.objects.filter(username="first-admin").exists())

    def test_remote_wrong_attempts_cannot_invalidate_valid_setup_code(self):
        with TemporaryDirectory() as directory:
            private_root = Path(directory) / "private"
            private_root.mkdir(mode=0o700)
            code_path = private_root / "setup-code"
            with override_settings(
                FIRST_RUN_SETUP_CODE_PATH=code_path,
                FIRST_RUN_SETUP_MAX_ATTEMPTS=3,
            ):
                call_command("provision_first_run_setup", verbosity=0)
                valid_code = code_path.read_text(encoding="utf-8").strip()
                self.installation.refresh_from_db()
                original_hash = self.installation.setup_code_hash
                original_expiry = self.installation.setup_code_expires_at
                csrf = self.client.get(reverse("csrf-token")).data["csrf_token"]
                responses = [
                    self.client.post(
                        reverse("setup-complete"),
                        {
                            "code": f"wrong-code-{index}",
                            "username": f"remote-attacker-{index}",
                            "email": f"remote-attacker-{index}@example.com",
                            "password": "StrongPass123!RC",
                            "password_confirm": "StrongPass123!RC",
                        },
                        format="json",
                        HTTP_X_CSRFTOKEN=csrf,
                        REMOTE_ADDR="198.51.100.23",
                    )
                    for index in range(3)
                ]

                self.installation.refresh_from_db()
                self.assertEqual([item.status_code for item in responses], [400, 400, 400])
                self.assertTrue(all(item.data["code"] == "invalid_setup_code" for item in responses))
                self.assertEqual(code_path.read_text(encoding="utf-8").strip(), valid_code)
                self.assertEqual(self.installation.setup_code_hash, original_hash)
                self.assertEqual(self.installation.setup_code_expires_at, original_expiry)
                self.assertEqual(self.installation.failed_attempts, 3)
                self.assertTrue(self.installation.accepting_setup)

                with self.captureOnCommitCallbacks(execute=True):
                    completed = self.client.post(
                        reverse("setup-complete"),
                        {
                            "code": valid_code,
                            "username": "first-admin",
                            "email": "first-admin@example.com",
                            "password": "StrongPass123!RC",
                            "password_confirm": "StrongPass123!RC",
                        },
                        format="json",
                        HTTP_X_CSRFTOKEN=csrf,
                        REMOTE_ADDR="203.0.113.44",
                    )

        self.assertEqual(completed.status_code, status.HTTP_201_CREATED)
        self.installation.refresh_from_db()
        self.assertEqual(self.installation.status, InstallationState.Status.INITIALIZED)
        self.assertEqual(self.installation.setup_code_hash, "")
        self.assertFalse(code_path.exists())

    def test_wrong_setup_codes_are_bounded_by_ip_throttle_without_consuming_code(self):
        with TemporaryDirectory() as directory:
            private_root = Path(directory) / "private"
            private_root.mkdir(mode=0o700)
            code_path = private_root / "setup-code"
            with override_settings(FIRST_RUN_SETUP_CODE_PATH=code_path):
                call_command("provision_first_run_setup", verbosity=0)
                valid_code = code_path.read_text(encoding="utf-8").strip()
                self.installation.refresh_from_db()
                original_hash = self.installation.setup_code_hash
                csrf = self.client.get(reverse("csrf-token")).data["csrf_token"]

                responses = [
                    self.client.post(
                        reverse("setup-complete"),
                        {
                            "code": f"wrong-code-{index}",
                            "username": f"rotating-identity-{index}",
                            "email": f"rotating-identity-{index}@example.com",
                            "password": "StrongPass123!RC",
                            "password_confirm": "StrongPass123!RC",
                        },
                        format="json",
                        HTTP_X_CSRFTOKEN=csrf,
                        REMOTE_ADDR="198.51.100.77",
                    )
                    for index in range(11)
                ]

                self.assertEqual([item.status_code for item in responses[:10]], [400] * 10)
                self.assertTrue(
                    all(item.data["code"] == "invalid_setup_code" for item in responses[:10])
                )
                self.assertEqual(responses[10].status_code, status.HTTP_429_TOO_MANY_REQUESTS)
                self.assertEqual(responses[10].data["code"], "rate_limited")
                self.assertIn("Retry-After", responses[10])
                self.installation.refresh_from_db()
                self.assertEqual(self.installation.setup_code_hash, original_hash)
                self.assertEqual(
                    self.installation.failed_attempts,
                    settings.FIRST_RUN_SETUP_MAX_ATTEMPTS,
                )
                self.assertEqual(code_path.read_text(encoding="utf-8").strip(), valid_code)
                self.assertTrue(self.installation.accepting_setup)

    def test_repeated_provisioning_reuses_the_active_code_without_rotation(self):
        with TemporaryDirectory() as directory:
            private_root = Path(directory) / "private"
            private_root.mkdir(mode=0o700)
            code_path = private_root / "setup-code"
            with override_settings(FIRST_RUN_SETUP_CODE_PATH=code_path):
                first_output = StringIO()
                call_command("provision_first_run_setup", stdout=first_output)
                first_code = code_path.read_text(encoding="utf-8")
                self.installation.refresh_from_db()
                first_hash = self.installation.setup_code_hash
                first_expiry = self.installation.setup_code_expires_at

                second_output = StringIO()
                call_command("provision_first_run_setup", stdout=second_output)
                second_code = code_path.read_text(encoding="utf-8")

        self.installation.refresh_from_db()
        self.assertEqual(second_code, first_code)
        self.assertEqual(self.installation.setup_code_hash, first_hash)
        self.assertEqual(self.installation.setup_code_expires_at, first_expiry)
        self.assertIn("Reused private first-run setup code", second_output.getvalue())

    def test_missing_plaintext_for_an_active_hash_is_recovered_with_a_new_code(self):
        with TemporaryDirectory() as directory:
            private_root = Path(directory) / "private"
            private_root.mkdir(mode=0o700)
            code_path = private_root / "setup-code"
            with override_settings(FIRST_RUN_SETUP_CODE_PATH=code_path):
                call_command("provision_first_run_setup", verbosity=0)
                first_code = code_path.read_text(encoding="utf-8").strip()
                code_path.unlink()
                call_command("provision_first_run_setup", verbosity=0)
                second_code = code_path.read_text(encoding="utf-8").strip()

        self.installation.refresh_from_db()
        self.assertNotEqual(second_code, first_code)
        self.assertTrue(check_password(second_code, self.installation.setup_code_hash))
        self.assertTrue(self.installation.accepting_setup)

    def test_successful_code_cannot_be_reused_for_a_second_setup(self):
        with TemporaryDirectory() as directory:
            private_root = Path(directory) / "private"
            private_root.mkdir(mode=0o700)
            code_path = private_root / "setup-code"
            with override_settings(FIRST_RUN_SETUP_CODE_PATH=code_path):
                call_command("provision_first_run_setup", verbosity=0)
                code = code_path.read_text(encoding="utf-8").strip()
                csrf = self.client.get(reverse("csrf-token")).data["csrf_token"]
                payload = {
                    "code": code,
                    "username": "first-admin",
                    "email": "first-admin@example.com",
                    "password": "StrongPass123!RC",
                    "password_confirm": "StrongPass123!RC",
                }
                first = self.client.post(
                    reverse("setup-complete"), payload, format="json", HTTP_X_CSRFTOKEN=csrf,
                )
                second = self.client.post(
                    reverse("setup-complete"), payload, format="json", HTTP_X_CSRFTOKEN=csrf,
                )

        self.assertEqual(first.status_code, status.HTTP_201_CREATED)
        self.assertEqual(second.status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(second.data["code"], "installation_initialized")
        self.assertEqual(User.objects.filter(is_superuser=True).count(), 1)

    def test_explicit_uninitialized_state_remains_authoritative_when_a_regular_user_exists(self):
        existing = User.objects.create_user(
            username="restored-member",
            email="member@example.com",
            password="StrongPass123!Member",
        )
        with TemporaryDirectory() as directory:
            private_root = Path(directory) / "private"
            private_root.mkdir(mode=0o700)
            code_path = private_root / "setup-code"
            with override_settings(FIRST_RUN_SETUP_CODE_PATH=code_path):
                call_command("provision_first_run_setup", verbosity=0)
                code = code_path.read_text(encoding="utf-8").strip()
                csrf = self.client.get(reverse("csrf-token")).data["csrf_token"]
                response = self.client.post(
                    reverse("setup-complete"),
                    {
                        "code": code,
                        "username": "first-admin",
                        "email": "first-admin@example.com",
                        "password": "StrongPass123!RC",
                        "password_confirm": "StrongPass123!RC",
                    },
                    format="json",
                    HTTP_X_CSRFTOKEN=csrf,
                )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(User.objects.filter(pk=existing.pk, is_superuser=False).exists())
        self.assertEqual(User.objects.filter(is_superuser=True).count(), 1)

    def test_existing_matching_user_is_never_reused_or_elevated(self):
        existing = User.objects.create_user(
            username="first-admin",
            email="existing-member@example.com",
            password="ExistingPass123!",
        )
        with TemporaryDirectory() as directory:
            private_root = Path(directory) / "private"
            private_root.mkdir(mode=0o700)
            code_path = private_root / "setup-code"
            with override_settings(FIRST_RUN_SETUP_CODE_PATH=code_path):
                call_command("provision_first_run_setup", verbosity=0)
                code = code_path.read_text(encoding="utf-8").strip()
                csrf = self.client.get(reverse("csrf-token")).data["csrf_token"]
                self.client.raise_request_exception = False
                response = self.client.post(
                    reverse("setup-complete"),
                    {
                        "code": code,
                        "username": "first-admin",
                        "email": "new-admin@example.com",
                        "password": "StrongPass123!RC",
                        "password_confirm": "StrongPass123!RC",
                    },
                    format="json",
                    HTTP_X_CSRFTOKEN=csrf,
                )
                code_file_remains = code_path.exists()

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(response.data["code"], "admin_identity_unavailable")
        existing.refresh_from_db()
        self.assertFalse(existing.is_staff)
        self.assertFalse(existing.is_superuser)
        self.assertTrue(existing.check_password("ExistingPass123!"))
        self.installation.refresh_from_db()
        self.assertEqual(self.installation.status, InstallationState.Status.UNINITIALIZED)
        self.assertTrue(code_file_remains)

    def test_initialized_installation_never_issues_code_and_removes_stale_plaintext(self):
        self.installation.status = InstallationState.Status.INITIALIZED
        self.installation.initialized_at = timezone.now()
        self.installation.save(update_fields=["status", "initialized_at", "updated_at"])
        with TemporaryDirectory() as directory:
            private_root = Path(directory) / "private"
            private_root.mkdir(mode=0o700)
            code_path = private_root / "setup-code"
            code_path.write_text("stale-plaintext\n", encoding="utf-8")
            os.chmod(code_path, 0o600)
            output = StringIO()
            with override_settings(FIRST_RUN_SETUP_CODE_PATH=code_path):
                call_command("provision_first_run_setup", stdout=output)
                file_still_exists = code_path.exists()

        self.installation.refresh_from_db()
        self.assertFalse(file_still_exists)
        self.assertEqual(self.installation.status, InstallationState.Status.INITIALIZED)
        self.assertEqual(self.installation.setup_code_hash, "")
        self.assertIn("already initialized", output.getvalue())

    def test_mid_transaction_failure_rolls_back_admin_and_initializing_state(self):
        with TemporaryDirectory() as directory:
            private_root = Path(directory) / "private"
            private_root.mkdir(mode=0o700)
            code_path = private_root / "setup-code"
            with override_settings(FIRST_RUN_SETUP_CODE_PATH=code_path):
                call_command("provision_first_run_setup", verbosity=0)
                code = code_path.read_text(encoding="utf-8").strip()
                csrf = self.client.get(reverse("csrf-token")).data["csrf_token"]
                self.client.raise_request_exception = False
                with patch("journal.models.AdminAuditLog.objects.create", side_effect=RuntimeError("injected audit failure")):
                    response = self.client.post(
                        reverse("setup-complete"),
                        {
                            "code": code,
                            "username": "first-admin",
                            "email": "first-admin@example.com",
                            "password": "StrongPass123!RC",
                            "password_confirm": "StrongPass123!RC",
                        },
                        format="json",
                        HTTP_X_CSRFTOKEN=csrf,
                    )
                code_file_remains = code_path.exists()

        self.assertEqual(response.status_code, status.HTTP_500_INTERNAL_SERVER_ERROR)
        self.installation.refresh_from_db()
        self.assertEqual(self.installation.status, InstallationState.Status.UNINITIALIZED)
        self.assertTrue(self.installation.accepting_setup)
        self.assertTrue(code_file_remains)
        self.assertFalse(User.objects.filter(username="first-admin").exists())

    def test_unsafe_plaintext_cleanup_rolls_back_initialization(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            private_root = root / "private"
            private_root.mkdir(mode=0o700)
            code_path = private_root / "setup-code"
            with override_settings(FIRST_RUN_SETUP_CODE_PATH=code_path):
                call_command("provision_first_run_setup", verbosity=0)
                code = code_path.read_text(encoding="utf-8").strip()
                os.link(code_path, root / "unexpected-hard-link")
                csrf = self.client.get(reverse("csrf-token")).data["csrf_token"]
                self.client.raise_request_exception = False
                response = self.client.post(
                    reverse("setup-complete"),
                    {
                        "code": code,
                        "username": "first-admin",
                        "email": "first-admin@example.com",
                        "password": "StrongPass123!RC",
                        "password_confirm": "StrongPass123!RC",
                    },
                    format="json",
                    HTTP_X_CSRFTOKEN=csrf,
                )

        self.assertEqual(response.status_code, status.HTTP_500_INTERNAL_SERVER_ERROR)
        self.installation.refresh_from_db()
        self.assertEqual(self.installation.status, InstallationState.Status.UNINITIALIZED)
        self.assertTrue(self.installation.accepting_setup)
        self.assertFalse(User.objects.filter(username="first-admin").exists())

    def test_setup_submission_requires_same_origin_csrf_token(self):
        client = type(self.client)(enforce_csrf_checks=True)
        response = client.post(
            reverse("setup-complete"),
            {
                "code": "not-relevant",
                "username": "first-admin",
                "email": "first-admin@example.com",
                "password": "StrongPass123!RC",
                "password_confirm": "StrongPass123!RC",
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.installation.refresh_from_db()
        self.assertEqual(self.installation.failed_attempts, 0)

    def test_provisioning_rejects_hard_link_target_without_overwriting_it(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            private_root = root / "private"
            private_root.mkdir(mode=0o700)
            outside = root / "outside-secret"
            outside.write_text("do-not-overwrite\n", encoding="utf-8")
            os.chmod(outside, 0o600)
            code_path = private_root / "setup-code"
            os.link(outside, code_path)

            with override_settings(FIRST_RUN_SETUP_CODE_PATH=code_path):
                with self.assertRaises(CommandError):
                    call_command("provision_first_run_setup", verbosity=0)

            self.assertEqual(outside.read_text(encoding="utf-8"), "do-not-overwrite\n")
            self.assertEqual(code_path.read_text(encoding="utf-8"), "do-not-overwrite\n")
        self.installation.refresh_from_db()
        self.assertEqual(self.installation.setup_code_hash, "")

    def test_provisioning_rejects_non_private_existing_file_permissions(self):
        if os.name == "nt":
            self.skipTest("POSIX mode enforcement is verified on Linux runners")
        with TemporaryDirectory() as directory:
            private_root = Path(directory) / "private"
            private_root.mkdir(mode=0o700)
            code_path = private_root / "setup-code"
            code_path.write_text("unsafe\n", encoding="utf-8")
            os.chmod(code_path, 0o644)

            with override_settings(FIRST_RUN_SETUP_CODE_PATH=code_path):
                with self.assertRaises(CommandError):
                    call_command("provision_first_run_setup", verbosity=0)

    def test_missing_installation_state_fails_closed_without_reopening_setup(self):
        InstallationState.objects.filter(pk=1).delete()

        response = self.client.get(reverse("setup-status"))

        self.assertEqual(response.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)
        self.assertEqual(response.data["code"], "installation_state_unavailable")
        self.assertNotIn("accepting_setup", response.data)

    def test_uninitialized_installation_closes_public_registration_surface(self):
        site = SiteSettings.load()
        site.registration_enabled = True
        site.save(update_fields=["registration_enabled", "updated_at"])
        public_settings = self.client.get(reverse("site-settings"))
        csrf = self.client.get(reverse("csrf-token")).data["csrf_token"]
        registration = self.client.post(
            reverse("register-request"),
            {"email": "early-member@example.com"},
            format="json",
            HTTP_X_CSRFTOKEN=csrf,
        )

        self.assertEqual(public_settings.status_code, status.HTTP_200_OK)
        self.assertFalse(public_settings.data["registration_enabled"])
        self.assertEqual(registration.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(registration.data["code"], "installation_uninitialized")
        self.assertFalse(PendingRegistration.objects.filter(email="early-member@example.com").exists())


class InstallationStateMigrationTests(TransactionTestCase):
    migrate_from = [("site", "0002_media_write_reservation")]
    migrate_to = [("site", "0004_installation_authentication_epoch")]

    def setUp(self):
        super().setUp()
        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_from)
        old_apps = executor.loader.project_state(self.migrate_from).apps
        OldUser = old_apps.get_model("accounts", "User")
        OldUser.objects.create(username="existing-installation-owner", is_staff=True, is_superuser=True)
        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_to)
        self.apps = executor.loader.project_state(self.migrate_to).apps

    def test_existing_database_is_migrated_to_initialized_without_active_bootstrap(self):
        MigratedInstallationState = self.apps.get_model("site", "InstallationState")
        installation = MigratedInstallationState.objects.get(pk=1)

        self.assertEqual(installation.status, "initialized")
        self.assertIsNotNone(installation.initialized_at)
        self.assertRegex(installation.authentication_epoch, r"^[0-9a-f]{64}$")
        self.assertEqual(installation.setup_code_hash, "")
        response = self.client.get(reverse("setup-status"))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["state"], "initialized")
        self.assertFalse(response.data["accepting_setup"])


class InstallationStateFreshMigrationTests(TransactionTestCase):
    migrate_from = [("site", "0002_media_write_reservation")]
    migrate_to = [("site", "0004_installation_authentication_epoch")]

    def setUp(self):
        super().setUp()
        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_from)
        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_to)
        self.apps = executor.loader.project_state(self.migrate_to).apps

    def test_empty_database_is_migrated_to_uninitialized_without_a_code(self):
        MigratedInstallationState = self.apps.get_model("site", "InstallationState")
        installation = MigratedInstallationState.objects.get(pk=1)

        self.assertEqual(installation.status, "uninitialized")
        self.assertIsNone(installation.initialized_at)
        self.assertIsNone(installation.initialized_by_id)
        self.assertEqual(installation.authentication_epoch, "")
        self.assertEqual(installation.setup_code_hash, "")


@skipUnlessDBFeature("has_select_for_update")
class FirstRunSetupPostgreSQLConcurrencyTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        super().setUp()
        self.installation, _created = InstallationState.objects.get_or_create(pk=1)
        self.installation.status = InstallationState.Status.UNINITIALIZED
        self.installation.setup_code_hash = ""
        self.installation.setup_code_issued_at = None
        self.installation.setup_code_expires_at = None
        self.installation.failed_attempts = 0
        self.installation.initialized_at = None
        self.installation.initialized_by = None
        self.installation.save()
        self.temporary = TemporaryDirectory()
        private_root = Path(self.temporary.name) / "private"
        private_root.mkdir(mode=0o700)
        self.code_path = private_root / "setup-code"
        self.setting_override = override_settings(FIRST_RUN_SETUP_CODE_PATH=self.code_path)
        self.setting_override.enable()
        call_command("provision_first_run_setup", verbosity=0)
        self.code = self.code_path.read_text(encoding="utf-8").strip()

    def tearDown(self):
        self.setting_override.disable()
        self.temporary.cleanup()
        super().tearDown()

    def test_two_concurrent_valid_submissions_create_exactly_one_first_admin(self):
        barrier = threading.Barrier(2)

        def submit(index):
            close_old_connections()
            try:
                client = type(self.client)()
                payload = {
                    "code": self.code,
                    "username": f"concurrent-admin-{index}",
                    "email": f"concurrent-admin-{index}@example.com",
                    "password": "StrongPass123!RC",
                    "password_confirm": "StrongPass123!RC",
                }
                barrier.wait(timeout=10)
                response = client.post(reverse("setup-complete"), payload, format="json")
                return response.status_code, payload["username"]
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(submit, range(2)))

        self.assertEqual(sorted(result[0] for result in results), [status.HTTP_201_CREATED, status.HTTP_404_NOT_FOUND])
        self.assertEqual(User.objects.filter(username__startswith="concurrent-admin-", is_superuser=True).count(), 1)
        installation = InstallationState.load()
        self.assertEqual(installation.status, InstallationState.Status.INITIALIZED)
        self.assertIn(installation.initialized_by.username, {result[1] for result in results})
