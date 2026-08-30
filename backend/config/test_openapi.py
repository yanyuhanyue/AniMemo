import json
import re

from django.contrib.staticfiles import finders
from django.test import Client, SimpleTestCase, override_settings


class ApiDocumentationTests(SimpleTestCase):
    def setUp(self):
        self.client = Client(HTTP_HOST="localhost")

    def test_schema_is_valid_and_documents_stable_security_contracts(self):
        response = self.client.get("/api/schema/", HTTP_ACCEPT="application/json")

        self.assertEqual(response.status_code, 200)
        schema = json.loads(response.content)
        self.assertTrue(schema["openapi"].startswith("3."))
        paths = schema["paths"]
        for path, method in (
            ("/api/v1/entries/", "get"),
            ("/api/v1/entries/{id}/", "get"),
            ("/api/v1/token/", "post"),
            ("/api/v1/token/refresh/", "post"),
            ("/api/integrations/v1/actions/", "post"),
            ("/api/integrations/v1/events/", "get"),
        ):
            self.assertIn(path, paths)
            self.assertIn(method, paths[path])

        operation_ids = [
            operation.get("operationId")
            for path_item in paths.values()
            for operation in path_item.values()
            if isinstance(operation, dict) and operation.get("operationId")
        ]
        self.assertEqual(len(operation_ids), len(set(operation_ids)))
        self.assertTrue(paths["/api/v1/entries/"]["get"]["operationId"].endswith("_list"))
        self.assertTrue(paths["/api/v1/entries/{id}/"]["get"]["operationId"].endswith("_retrieve"))
        self.assertFalse(any(path.startswith("/api/") and not path.startswith(("/api/v1/", "/api/integrations/v1/")) for path in paths))
        self.assertNotIn("/api/entries/", paths)
        self.assertNotIn("/api/token/", paths)
        self.assertNotIn("/api/v1/plugins/{slug}/", paths)
        self.assertNotIn("/api/v1/plugins/{slug}/{plugin_path}", paths)
        self.assertNotIn("/api/plugins/{slug}/", paths)
        self.assertNotIn("/api/plugins/{slug}/{plugin_path}", paths)
        self.assertFalse(any("plugin-assets" in path or "plugin-previews" in path for path in paths))

        schemes = schema["components"]["securitySchemes"]
        self.assertEqual(schemes["bearerAuth"]["scheme"], "bearer")
        self.assertEqual(schemes["refreshCookie"]["in"], "cookie")
        self.assertEqual(schemes["integrationHmac"]["name"], "X-AniMemo-Key-Id")

        public_failure = schema["components"]["schemas"]["ApiError"]
        self.assertFalse(public_failure["additionalProperties"])
        self.assertEqual(
            set(public_failure["required"]),
            {"code", "detail", "correlation_id"},
        )
        self.assertEqual(
            set(public_failure["properties"]),
            {"code", "detail", "correlation_id"},
        )
        self.assertEqual(
            public_failure["properties"]["correlation_id"]["pattern"],
            "^[0-9a-f]{32}$",
        )
        self.assertNotIn("fields", public_failure["properties"])
        self.assertNotIn("metadata", public_failure["properties"])
        self.assertNotIn("retry_after_seconds", public_failure["properties"])

        refresh = paths["/api/v1/token/refresh/"]["post"]
        self.assertNotIn("requestBody", refresh)
        self.assertEqual(refresh["security"], [{"refreshCookie": []}])
        self.assertIn("X-CSRFToken", {parameter["name"] for parameter in refresh["parameters"]})
        token_properties = paths["/api/v1/token/"]["post"]["requestBody"]["content"]["application/json"]["schema"]["properties"]
        self.assertEqual(set(token_properties["challenge"]["required"]), {"provider", "token"})
        self.assertIn("cf-turnstile-response", token_properties)
        self.assertTrue(token_properties["cf-turnstile-response"]["deprecated"])
        self.assertNotIn("cf_turnstile_response", token_properties)

        for challenge_path in (
            "/api/v1/auth/register/request/",
            "/api/v1/auth/register/complete/",
            "/api/v1/auth/password-reset/",
            "/api/v1/auth/password-reset-confirm/",
        ):
            request_schema = paths[challenge_path]["post"]["requestBody"]["content"]["application/json"]["schema"]
            component_name = request_schema["$ref"].rsplit("/", 1)[-1]
            properties = schema["components"]["schemas"][component_name]["properties"]
            challenge_component = properties["challenge"]["$ref"].rsplit("/", 1)[-1]
            self.assertEqual(
                set(schema["components"]["schemas"][challenge_component]["required"]),
                {"provider", "token"},
            )
            self.assertTrue(properties["cf-turnstile-response"]["deprecated"])
            self.assertNotIn("cf_turnstile_response", properties)

        entries = paths["/api/v1/entries/"]["get"]
        self.assertEqual(
            entries["responses"]["401"]["content"]["application/json"]["schema"]["$ref"],
            "#/components/schemas/ApiError",
        )
        self.assertIn("429", entries["responses"])
        self.assertIn("default", entries["responses"])

        hmac = paths["/api/integrations/v1/actions/"]["post"]
        self.assertEqual(
            {parameter["name"] for parameter in hmac["parameters"] if parameter["in"] == "header"},
            {
                "X-AniMemo-Key-Id",
                "X-AniMemo-Timestamp",
                "X-AniMemo-Nonce",
                "X-AniMemo-Signature",
            },
        )

    @override_settings(
        STORAGES={
            "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
            "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
        }
    )
    def test_swagger_uses_same_origin_sidecar_assets_and_csp(self):
        response = self.client.get("/api/docs/")
        body = response.content.decode("utf-8")

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("cdn.", body.lower())
        self.assertNotRegex(body, r"<script(?![^>]*\bsrc=)[^>]*>")
        self.assertNotRegex(body, r"<style(?:\s|>)")
        self.assertNotIn("unsafe-inline", response["Content-Security-Policy"].split("script-src", 1)[1].split(";", 1)[0])

        asset_urls = re.findall(r"(?:src|href)=\"([^\"]+)\"", body)
        for asset_url in asset_urls:
            if asset_url.startswith("/static/"):
                relative = asset_url.removeprefix("/static/")
                self.assertIsNotNone(finders.find(relative), asset_url)

        script = self.client.get("/api/docs/?script=")
        self.assertEqual(script.status_code, 200)
        self.assertIn("application/javascript", script["Content-Type"])
        self.assertNotIn("unsafe-inline", script.content.decode("utf-8"))
