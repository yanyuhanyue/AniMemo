from django.test import SimpleTestCase
from rest_framework.exceptions import Throttled, ValidationError
from rest_framework.renderers import JSONRenderer
from rest_framework.response import Response
from rest_framework.test import APIRequestFactory

from .api_renderers import CanonicalJSONRenderer
from .rest_exceptions import exception_handler


class CanonicalApiErrorTests(SimpleTestCase):
    def setUp(self):
        self.request = APIRequestFactory().post("/api/test/", {})

    def test_validation_errors_keep_field_structure(self):
        response = exception_handler(
            ValidationError({"title": ["该字段为必填项。"]}),
            {"request": self.request},
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["code"], "validation_error")
        self.assertEqual(response.data["fields"]["title"], ["该字段为必填项。"])
        self.assertIn("detail", response.data)

    def test_throttled_errors_preserve_retry_after(self):
        response = exception_handler(
            Throttled(wait=7),
            {"request": self.request},
        )

        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.data["code"], "rate_limited")
        self.assertEqual(response.data["retry_after_seconds"], 7)
        self.assertEqual(response["Retry-After"], "7")

    def test_renderer_canonicalizes_hand_written_errors(self):
        response = Response({"detail": "资源状态冲突。", "entry_id": 7}, status=409)
        response.accepted_renderer = CanonicalJSONRenderer()
        response.accepted_media_type = "application/json"
        response.renderer_context = {"response": response}
        response.render()

        self.assertEqual(response.data["code"], "conflict")
        self.assertEqual(response.data["detail"], "资源状态冲突。")
        self.assertEqual(response.data["metadata"], {"entry_id": 7})
        self.assertIsInstance(response.accepted_renderer, JSONRenderer)
