"""First-run credentials must not become traceback or POST diagnostics."""

import secrets
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.management import call_command
from django.test import override_settings
from django.urls import reverse
from django.views.debug import ExceptionReporter
from rest_framework.settings import api_settings
from rest_framework.test import APIRequestFactory, APITestCase

from .models import InstallationState
from .serializers import FirstRunSetupSerializer
from .views import InstallationSetupView, InstallationStatusView


@override_settings(TURNSTILE_ENABLED=False)
class FirstRunExceptionReportingTests(APITestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.installation = InstallationState.load()
        self.installation.status = InstallationState.Status.UNINITIALIZED
        self.installation.setup_code_hash = ""
        self.installation.setup_code_issued_at = None
        self.installation.setup_code_expires_at = None
        self.installation.failed_attempts = 0
        self.installation.initialized_at = None
        self.installation.initialized_by = None
        self.installation.save()
        private_root = Path(self.enterContext(TemporaryDirectory())) / "private"
        private_root.mkdir(mode=0o700)
        self.code_path = private_root / "setup-code"
        self.enterContext(override_settings(FIRST_RUN_SETUP_CODE_PATH=self.code_path))
        call_command("provision_first_run_setup", stdout=StringIO(), verbosity=0)
        self.code = self.code_path.read_text(encoding="utf-8").strip()
        self.password = "Aa1!" + secrets.token_urlsafe(24)
        self.confirmation = "Bb2@" + secrets.token_urlsafe(24)

    def _payload(self):
        return {
            "code": self.code,
            "username": "reporter-admin",
            "email": "reporter-admin@example.com",
            "password": self.password,
            "password_confirm": self.confirmation,
        }

    def _assert_no_credentials(self, report):
        for value in (self.code, self.password, self.confirmation):
            # Do not include the report or a credential in assertion output.
            self.assertFalse(value in report, "first-run credential appeared in diagnostics")

    def _render_failure(self, request):
        # Observe a real exception at the existing DRF handler boundary without
        # changing its public-error behavior or weakening application CSRF.
        request._dont_enforce_csrf_checks = True
        reports = []
        handler = api_settings.EXCEPTION_HANDLER

        def observe_exception(exc, context):
            reporter = ExceptionReporter(
                context["request"]._request, type(exc), exc, exc.__traceback__
            )
            reports.extend((reporter.get_traceback_text(), reporter.get_traceback_html()))
            return handler(exc, context)

        with patch.object(
            InstallationSetupView, "get_exception_handler", return_value=observe_exception
        ):
            response = InstallationSetupView.as_view()(request)
        self.assertEqual(response.status_code, 500)
        self.assertEqual(len(reports), 2)
        return reports

    def _assert_rolled_back(self):
        self.installation.refresh_from_db()
        self.assertEqual(self.installation.status, InstallationState.Status.UNINITIALIZED)
        self.assertTrue(self.installation.accepting_setup)
        self.assertTrue(self.code_path.exists())
        self.assertFalse(get_user_model().objects.filter(username="reporter-admin").exists())

    def test_service_failure_redacts_json_tracebacks_in_debug_and_production(self):
        for debug in (False, True):
            with self.subTest(debug=debug), override_settings(DEBUG=debug):
                request = APIRequestFactory().post(
                    reverse("setup-complete"), self._payload(), format="json"
                )
                with (
                    patch.object(
                        FirstRunSetupSerializer, "validate", autospec=True,
                        side_effect=lambda _self, attrs: attrs,
                    ),
                    patch(
                        "journal.models.AdminAuditLog.objects.create",
                        side_effect=RuntimeError("injected audit failure"),
                    ),
                ):
                    reports = self._render_failure(request)
                for report in reports:
                    self._assert_no_credentials(report)
                self._assert_rolled_back()

    def test_nested_serializer_aliases_are_redacted(self):
        def fail_validation(_serializer, attrs):
            # These names deliberately do not match code/password/payload.
            credential_alias = dict(attrs)
            assert credential_alias
            raise RuntimeError("injected serializer failure")

        for debug in (False, True):
            with self.subTest(debug=debug), override_settings(DEBUG=debug):
                request = APIRequestFactory().post(
                    reverse("setup-complete"), self._payload(), format="json"
                )
                with patch.object(
                    FirstRunSetupSerializer, "validate", autospec=True,
                    side_effect=fail_validation,
                ):
                    reports = self._render_failure(request)
                for report in reports:
                    self._assert_no_credentials(report)
                self._assert_rolled_back()

    def test_form_post_values_are_redacted(self):
        request = APIRequestFactory().post(
            reverse("setup-complete"), self._payload(), format="multipart"
        )
        with (
            override_settings(DEBUG=True),
            patch.object(
                FirstRunSetupSerializer, "validate", autospec=True,
                side_effect=lambda _self, attrs: attrs,
            ),
            patch(
                "journal.models.AdminAuditLog.objects.create",
                side_effect=RuntimeError("injected form failure"),
            ),
        ):
            reports = self._render_failure(request)
        for report in reports:
            self._assert_no_credentials(report)
        self._assert_rolled_back()

    def test_before_post_throttle_failure_is_redacted(self):
        def fail_throttles(_view, request):
            credential_alias = dict(request.data)
            assert credential_alias
            raise RuntimeError("injected throttle failure")

        request = APIRequestFactory().post(
            reverse("setup-complete"), self._payload(), format="json"
        )
        with (
            override_settings(DEBUG=True),
            patch.object(
                InstallationSetupView, "check_throttles", autospec=True,
                side_effect=fail_throttles,
            ),
        ):
            reports = self._render_failure(request)
        for report in reports:
            self._assert_no_credentials(report)
        self._assert_rolled_back()

    @override_settings(DEBUG=True)
    def test_real_500_response_keeps_closed_error_contract(self):
        self.client.raise_request_exception = False
        with (
            patch.object(
                FirstRunSetupSerializer, "validate", autospec=True,
                side_effect=lambda _self, attrs: attrs,
            ),
            patch(
                "journal.models.AdminAuditLog.objects.create",
                side_effect=RuntimeError("injected technical 500 failure"),
            ),
        ):
            response = self.client.post(reverse("setup-complete"), self._payload(), format="json")
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.data["code"], "internal_error")
        self.assertRegex(response.data["correlation_id"], r"^[0-9a-f]{32}$")
        self._assert_no_credentials(response.content.decode("utf-8", errors="replace"))
        self._assert_rolled_back()

    def test_status_request_keeps_default_reporter(self):
        request = APIRequestFactory().get(reverse("setup-status"))
        original_filter = getattr(request, "exception_reporter_filter", None)
        response = InstallationStatusView.as_view()(request)
        self.assertEqual(response.status_code, 200)
        self.assertIs(getattr(request, "exception_reporter_filter", None), original_filter)
