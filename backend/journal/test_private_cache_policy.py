"""Actual API success/error responses keep personal data out of shared caches."""

from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import skipUnless
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.db import OperationalError, connection
from django.middleware.common import CommonMiddleware
from django.test import override_settings
from django.urls import reverse
from plugin_host.models import PluginProject
from plugin_host.runtime import runtime_registry
from plugin_host.services import install_for_user
from rest_framework.exceptions import Throttled
from rest_framework.test import APITestCase
from site_config.models import InstallationState

from .models import JournalEntry, UserSettings


class PrivateApiCachePolicyTests(APITestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("cache-owner")
        self.other = get_user_model().objects.create_user("cache-other")
        self.entry = JournalEntry.objects.create(user=self.user, title="private cache fixture", review="owner-only note")
        self.settings, _ = UserSettings.objects.get_or_create(user=self.user)
        self.settings.public_status = UserSettings.PublicStatus.APPROVED
        self.settings.allow_sharing = True
        self.settings.save()
        state = InstallationState.load()
        state.status = InstallationState.Status.INITIALIZED
        state.save(update_fields=["status"])

    def assert_private(self, response, *, vary_accept=True):
        directives = {part.strip().lower() for part in response.get("Cache-Control", "").split(",")}
        self.assertTrue({"private", "no-store"}.issubset(directives), response.headers)
        self.assertNotIn("public", directives)
        vary = {part.strip().lower() for part in response.get("Vary", "").split(",")}
        expected_vary = {"authorization", "cookie", "accept"} if vary_accept else {"authorization", "cookie"}
        self.assertTrue(expected_vary.issubset(vary), response.headers)

    def test_success_policy_for_personal_route_families_and_both_aliases(self):
        self.client.force_authenticate(self.user)
        paths = [
            reverse("me"), reverse("settings"), reverse("entry-list"),
            reverse("entry-detail", args=[self.entry.pk]), reverse("filter-list"),
            reverse("column-list"), "/api/stats/me/", "/api/export/",
            f"/api/entries/{self.entry.pk}/watch-history/", "/api/external-accounts/",
        ]
        for path in paths:
            for candidate in (path, path.replace("/api/", "/api/v1/", 1)):
                with self.subTest(path=candidate):
                    response = self.client.get(candidate)
                    self.assertEqual(response.status_code, 200, getattr(response, "data", None))
                    self.assert_private(response)

    def test_anonymous_forbidden_missing_invalid_and_server_errors_are_private(self):
        response = self.client.get(reverse("entry-list"))
        self.assertEqual(response.status_code, 401)
        self.assert_private(response)
        self.client.force_authenticate(self.other)
        response = self.client.get(reverse("entry-detail", args=[self.entry.pk]))
        self.assertEqual(response.status_code, 404)
        self.assert_private(response)
        self.client.force_authenticate(self.user)
        response = self.client.patch(reverse("entry-detail", args=[self.entry.pk]), {"title": ""})
        self.assertEqual(response.status_code, 400)
        self.assert_private(response)
        response = self.client.get("/api/stats/me/?start=invalid")
        self.assertEqual(response.status_code, 400)
        self.assert_private(response)
        with patch("journal.entry_views.JournalEntryViewSet.list", side_effect=RuntimeError("synthetic response failure")):
            response = self.client.get(reverse("entry-list"))
        self.assertEqual(response.status_code, 500)
        self.assert_private(response)

    def test_same_url_isolated_between_two_users_and_anonymous(self):
        path = reverse("entry-list")
        for user in (self.user, self.other):
            self.client.force_authenticate(user)
            response = self.client.get(path)
            self.assert_private(response)
            self.assertEqual(response.data["count"], 1 if user == self.user else 0)
        self.client.force_authenticate(None)
        response = self.client.get(path)
        self.assertEqual(response.status_code, 401)
        self.assert_private(response)

    @skipUnless(connection.vendor == "postgresql", "Bounded owner preview requires PostgreSQL")
    def test_owner_preview_is_private_and_public_variant_keeps_identity_vary(self):
        path = reverse("public-showcase-entries", args=[self.settings.public_slug])
        anonymous = self.client.get(path)
        self.assertEqual(anonymous.status_code, 200)
        self.assertEqual(anonymous.data["results"], [])
        self.assertNotIn("no-store", anonymous.get("Cache-Control", ""))
        self.assertTrue({"authorization", "cookie"}.issubset({part.strip().lower() for part in anonymous.get("Vary", "").split(",")}))
        self.client.force_authenticate(self.user)
        owner = self.client.get(path)
        self.assertEqual(owner.status_code, 200)
        self.assertEqual(owner.data["results"][0]["review"], "owner-only note")
        self.assert_private(owner, vary_accept=False)

    def test_public_discovery_keeps_its_public_cache_policy(self):
        for user in (None, self.user):
            self.client.force_authenticate(user)
            for name in ("site-settings", "tag-presets", "featured"):
                with self.subTest(name=name, authenticated=user is not None):
                    response = self.client.get(reverse(name))
                    self.assertEqual(response.status_code, 200)
                    self.assertNotIn("no-store", response.get("Cache-Control", ""))

    @skipUnless(connection.vendor == "postgresql", "Bounded public variants require PostgreSQL")
    def test_bounded_public_identity_variants_keep_private_authenticated_policy(self):
        for user in (None, self.user):
            self.client.force_authenticate(user)
            for name in ("public-homepage-entries", "public-homepage-summary", "public-homepage-facets", "public-showcase-directory"):
                with self.subTest(name=name, authenticated=user is not None):
                    response = self.client.get(reverse(name))
                    self.assertEqual(response.status_code, 200)
                    if user is None:
                        self.assertNotIn("no-store", response.get("Cache-Control", ""))
                        self.assertTrue({"authorization", "cookie"}.issubset({part.strip().lower() for part in response.get("Vary", "").split(",")}))
                    else:
                        self.assert_private(response, vary_accept=False)

    def test_existing_vary_values_are_merged(self):
        from .entry_views import MeView
        original = MeView.get

        def response_with_vary(view, request):
            response = original(view, request)
            response["Vary"] = "Accept-Language, X-Theme"
            response["Cache-Control"] = "public, max-age=3600"
            return response

        self.client.force_authenticate(self.user)
        with patch.object(MeView, "get", response_with_vary):
            response = self.client.get(reverse("me"))
        self.assert_private(response)
        vary = {part.strip().lower() for part in response["Vary"].split(",")}
        self.assertTrue({"accept-language", "x-theme"}.issubset(vary))

    def test_permission_method_and_throttle_errors_keep_private_policy(self):
        self.client.force_authenticate(self.user)
        forbidden = self.client.get(reverse("staff-plugin-list"))
        self.assertEqual(forbidden.status_code, 403)
        self.assert_private(forbidden)
        method = self.client.delete(reverse("entry-list"))
        self.assertEqual(method.status_code, 405)
        self.assert_private(method)
        with patch("journal.entry_views.JournalEntryViewSet.list", side_effect=Throttled(wait=30)):
            throttled = self.client.get(reverse("entry-list"))
        self.assertEqual(throttled.status_code, 429)
        self.assertEqual(throttled["Retry-After"], "30")
        self.assert_private(throttled)

    def test_plugin_discovery_identity_variants_are_private_when_authenticated(self):
        for user in (None, self.user, self.other):
            self.client.force_authenticate(user)
            for name in ("enabled-plugins", "plugin-marketplace"):
                with self.subTest(name=name, authenticated=user is not None):
                    response = self.client.get(reverse(name))
                    self.assertEqual(response.status_code, 200)
                    vary = {part.strip().lower() for part in response.get("Vary", "").split(",")}
                    self.assertTrue({"authorization", "cookie"}.issubset(vary))
                    if user is not None:
                        self.assert_private(response)
                    else:
                        self.assertNotIn("no-store", response.get("Cache-Control", ""))

    @override_settings(DEBUG=False, ALLOWED_HOSTS=["testserver"])
    def test_personal_disallowed_host_errors_before_resolution_are_private(self):
        for prefix in ("/api/", "/api/v1/"):
            response = self.client.get(prefix + "auth/me/", HTTP_HOST="synthetic-invalid-host.example")
            self.assertEqual(response.status_code, 400)
            self.assert_private(response, vary_accept=False)

    @override_settings(DEBUG=False)
    def test_personal_middleware_errors_before_resolution_are_private(self):
        self.client.raise_request_exception = False
        with patch.object(CommonMiddleware, "process_request", side_effect=OperationalError("synthetic middleware failure")):
            for prefix in ("/api/", "/api/v1/"):
                response = self.client.get(prefix + "auth/me/")
                self.assertEqual(response.status_code, 500)
                self.assert_private(response, vary_accept=False)

    def test_setup_credential_validation_errors_are_private(self):
        for prefix in ("/api/", "/api/v1/"):
            response = self.client.post(prefix + "setup/", {}, format="json")
            self.assertEqual(response.status_code, 400)
            self.assert_private(response)

    @override_settings(PLUGIN_MIN_FREE_DISK_MB=0)
    def test_real_plugin_dispatch_is_private_for_success_and_permission_errors(self):
        with TemporaryDirectory(prefix="private-cache-plugin-") as directory, override_settings(PLUGIN_ROOT=Path(directory)):
            try:
                get_user_model().objects.create_superuser("cache-plugin-publisher")
                call_command("sync_official_plugins", verbosity=0, stdout=StringIO())
                plugin = PluginProject.objects.get(slug="watch-history-importer")
                install_for_user(plugin, user=self.user)
                for prefix in ("/api/", "/api/v1/"):
                    for user in (self.user, self.other, None):
                        self.client.force_authenticate(user)
                        response = self.client.get(prefix + "plugins/watch-history-importer/status")
                        self.assertEqual(response.status_code, 200 if user == self.user else 403)
                        self.assert_private(response)
            finally:
                runtime_registry.clear()
