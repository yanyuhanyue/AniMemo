import json
from unittest.mock import patch

from django.test import Client, RequestFactory, SimpleTestCase

SERVER_CORRELATION_ID = "a" * 32


class ApiCsrfFailureTests(SimpleTestCase):
    def test_api_csrf_failure_uses_stable_code(self):
        response = Client(enforce_csrf_checks=True).post("/api/token/refresh/", {})

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["code"], "csrf_failed")

    @patch("config.api_errors.secrets.token_hex", return_value=SERVER_CORRELATION_ID)
    def test_api_csrf_failure_discards_reason_and_client_correlation(self, token_hex):
        from .csrf_failure import csrf_failure

        sentinels = (
            r"C:\private\csrf.py",
            "SELECT secret FROM csrf_tokens",
            "Traceback CSRF_PRIVATE_CANARY",
            "client-selected-correlation",
        )
        request = RequestFactory().post(
            "/api/private/",
            HTTP_X_REQUEST_ID=sentinels[-1],
            HTTP_X_ANIMEMO_CORRELATION_ID=sentinels[-1],
        )

        response = csrf_failure(request, reason=" | ".join(sentinels))

        self.assertEqual(response.status_code, 403)
        self.assertEqual(
            json.loads(response.content),
            {
                "code": "csrf_failed",
                "detail": "安全验证已过期，请刷新页面后重试。",
                "correlation_id": SERVER_CORRELATION_ID,
            },
        )
        self.assertEqual(
            response["X-AniMemo-Correlation-ID"],
            SERVER_CORRELATION_ID,
        )
        serialized = response.content.decode("utf-8") + repr(response.headers)
        for sentinel in sentinels:
            self.assertNotIn(sentinel, serialized)
        token_hex.assert_called_once_with(16)
