import json
import re

from django.test import Client, SimpleTestCase
from django.urls import resolve, reverse

from config.urls import urlpatterns as root_urlpatterns
from plugin_host.runtime.dispatch import PluginDispatch


UUID_SAMPLE = "12345678-1234-5678-1234-567812345678"


def concrete_path(path):
    values = {
        "provider": "bangumi",
        "slug": "sample-plugin",
        "kind": "entries",
        "action": "force-logout",
        "external_id": "1",
        "plugin_path": "status",
        "preview_session": "preview-session",
        "public_slug": UUID_SAMPLE,
        "share_slug": UUID_SAMPLE,
        "preview_id": UUID_SAMPLE,
    }

    def replace(match):
        name = match.group(1)
        return values.get(name, "1")

    return re.sub(r"\{([^}]+)\}", replace, path)


class ApiVersionContractTests(SimpleTestCase):
    def setUp(self):
        self.client = Client(HTTP_HOST="localhost")

    def schema(self):
        response = self.client.get("/api/schema/", HTTP_ACCEPT="application/json")
        self.assertEqual(response.status_code, 200)
        return json.loads(response.content)

    def test_canonical_routes_have_one_legacy_alias_to_the_same_view(self):
        paths = self.schema()["paths"]
        canonical_paths = [path for path in paths if path.startswith("/api/v1/")]

        self.assertTrue(canonical_paths)
        for canonical_pattern in canonical_paths:
            canonical = concrete_path(canonical_pattern)
            legacy = canonical.replace("/api/v1/", "/api/", 1)
            with self.subTest(canonical=canonical):
                self.assertIs(resolve(canonical).func, resolve(legacy).func)

    def test_v1_namespace_is_canonical_without_changing_legacy_reverse_names(self):
        self.assertEqual(reverse("api-v1:entry-list"), "/api/v1/entries/")
        self.assertEqual(reverse("api-v1:token_refresh"), "/api/v1/token/refresh/")
        self.assertEqual(reverse("entry-list"), "/api/entries/")
        self.assertEqual(reverse("token_refresh"), "/api/token/refresh/")

    def test_legacy_and_v1_return_the_same_auth_contract(self):
        canonical = self.client.get("/api/v1/entries/")
        legacy = self.client.get("/api/entries/")

        self.assertEqual(canonical.status_code, 401)
        self.assertEqual(legacy.status_code, canonical.status_code)
        self.assertEqual(legacy.json(), canonical.json())

    def test_dynamic_plugin_dispatch_keeps_a_v1_and_legacy_entrypoint(self):
        canonical = resolve("/api/v1/plugins/sample-plugin/status/")
        legacy = resolve("/api/plugins/sample-plugin/status/")

        self.assertIs(canonical.func.view_class, PluginDispatch)
        self.assertIs(legacy.func.view_class, PluginDispatch)

    def test_root_urlconf_cannot_gain_an_unversioned_core_endpoint(self):
        api_patterns = {
            str(pattern.pattern)
            for pattern in root_urlpatterns
            if str(pattern.pattern).startswith("api/")
        }

        self.assertEqual(
            api_patterns,
            {
                "api/schema/",
                "api/docs/",
                "api/v1/",
                "api/",
                "api/integrations/v1/",
                "api/v1/plugins/<slug>/<path:plugin_path>",
                "api/v1/plugins/<slug>/",
                "api/plugins/<slug>/<path:plugin_path>",
                "api/plugins/<slug>/",
            },
        )
