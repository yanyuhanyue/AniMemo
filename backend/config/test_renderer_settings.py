from django.test import SimpleTestCase
from rest_framework.response import Response
from rest_framework.test import APIRequestFactory
from rest_framework.views import APIView

from .api_renderers import CanonicalJSONRenderer
from .settings import DEBUG as CONFIGURED_DEBUG
from .settings import REST_FRAMEWORK, _api_renderer_classes


class _ProductionFailureView(APIView):
    authentication_classes = ()
    permission_classes = ()
    renderer_classes = (CanonicalJSONRenderer,)

    def get(self, _request):
        return Response(
            {"code": "private_code", "detail": "HTML_PRIVATE_CANARY"},
            status=418,
        )


class ProductionRendererSettingsTests(SimpleTestCase):
    def test_browsable_api_renderer_is_debug_only(self):
        self.assertEqual(
            _api_renderer_classes(False),
            ("config.api_renderers.CanonicalJSONRenderer",),
        )
        self.assertEqual(
            REST_FRAMEWORK["DEFAULT_RENDERER_CLASSES"],
            _api_renderer_classes(CONFIGURED_DEBUG),
        )

    def test_html_accept_cannot_turn_production_error_into_html(self):
        request = APIRequestFactory().get("/api/private/", HTTP_ACCEPT="text/html")

        response = _ProductionFailureView.as_view()(request)
        response.render()

        self.assertEqual(response.status_code, 500)
        self.assertEqual(response["Content-Type"], "application/json")
        self.assertEqual(
            set(response.data),
            {"code", "detail", "correlation_id"},
        )
        self.assertNotIn(b"HTML_PRIVATE_CANARY", response.content)
        self.assertNotIn(b"<!DOCTYPE html", response.content)
