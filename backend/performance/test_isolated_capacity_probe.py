import importlib
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth.models import AnonymousUser
from django.test import SimpleTestCase, override_settings
from django.urls import clear_url_caches, resolve
from rest_framework.test import APIRequestFactory, force_authenticate

from performance.views import IsolatedProviderLatencyView


class IsolatedCapacityProbeTests(SimpleTestCase):
    def test_probe_route_is_absent_without_explicit_isolated_switch(self):
        from config import urls

        self.assertFalse(
            any("_isolated/capacity" in str(pattern.pattern) for pattern in urls.urlpatterns)
        )

    def test_probe_route_is_mounted_only_with_explicit_isolated_switch(self):
        from config import urls

        try:
            with override_settings(ANIMEMO_ISOLATED_CAPACITY_PROBE=True):
                importlib.reload(urls)
                clear_url_caches()

                match = resolve(
                    "/api/v1/_isolated/capacity/provider-latency/",
                    urlconf=urls,
                )

                self.assertEqual(match.url_name, "isolated-provider-latency")
        finally:
            importlib.reload(urls)
            clear_url_caches()

    def test_probe_requires_authentication(self):
        request = APIRequestFactory().post("/provider-latency/", {}, format="json")
        request.user = AnonymousUser()

        response = IsolatedProviderLatencyView.as_view()(request)

        self.assertEqual(response.status_code, 401)

    @override_settings(
        ANIMEMO_ISOLATED_CAPACITY_PROBE=True,
        ANIMEMO_ISOLATED_PROVIDER_LATENCY_MS=250,
    )
    @patch("performance.views.time.sleep")
    def test_probe_uses_configured_fake_provider_latency(self, sleep):
        request = APIRequestFactory().post("/provider-latency/", {}, format="json")
        force_authenticate(request, user=SimpleNamespace(is_authenticated=True, pk=1))

        response = IsolatedProviderLatencyView.as_view()(request)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.data,
            {
                "provider": "fake-bangumi-provider",
                "network": "disabled",
                "latency_ms": 250,
            },
        )
        sleep.assert_called_once_with(0.25)
