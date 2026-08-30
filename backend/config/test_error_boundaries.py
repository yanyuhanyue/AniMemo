import logging
import sys
from unittest.mock import patch

from django.http import HttpResponse
from django.test import Client, RequestFactory, SimpleTestCase, override_settings
from django.urls import path
from rest_framework.exceptions import ValidationError
from rest_framework.views import APIView

from .closed_logging import ClosedDjangoBoundaryFilter
from .urls import (
    api_bad_request,
    api_page_not_found,
    api_permission_denied,
    api_server_error,
)

SERVER_CORRELATION_ID = "b" * 32
HOSTILE_DIAGNOSTIC = (
    r"C:\private\middleware.py SELECT secret FROM credentials "
    "token=PRIVATE_TOKEN signed=https://private.invalid/?signature=PRIVATE "
    "Traceback username=private-operator"
)


class HostileMiddlewareError(RuntimeError):
    pass


class ExplosiveStatus:
    def __int__(self):
        raise RuntimeError(HOSTILE_DIAGNOSTIC)

    def __str__(self):
        return HOSTILE_DIAGNOSTIC


class HostileExceptionMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        raise HostileMiddlewareError(HOSTILE_DIAGNOSTIC)


def inert_view(_request):
    return HttpResponse("ok")


class DrfValidationFailureView(APIView):
    authentication_classes = []
    permission_classes = []

    def get(self, _request):
        raise ValidationError({"private": [HOSTILE_DIAGNOSTIC]})


urlpatterns = [
    path("api/v1/middleware-crash/", inert_view),
    path("api/v1/drf-validation-failure/", DrfValidationFailureView.as_view()),
]
handler400 = api_bad_request
handler403 = api_permission_denied
handler404 = api_page_not_found
handler500 = api_server_error


class DjangoApiErrorBoundaryTests(SimpleTestCase):
    @patch("config.closed_logging.correlation_id_for", return_value=SERVER_CORRELATION_ID)
    def test_repeated_prepare_filters_preserve_out_of_band_authority(self, correlation_id_for):
        request = RequestFactory().get("/api/v1/private-path/?signature=PRIVATE")
        try:
            raise HostileMiddlewareError(HOSTILE_DIAGNOSTIC)
        except HostileMiddlewareError:
            exc_info = sys.exc_info()
        record = logging.getLogger("django.request").makeRecord(
            "django.request",
            logging.ERROR,
            __file__,
            1,
            HOSTILE_DIAGNOSTIC,
            (),
            exc_info,
            extra={"request": request, "status_code": 500},
        )

        self.assertTrue(ClosedDjangoBoundaryFilter().filter(record))
        self.assertTrue(ClosedDjangoBoundaryFilter().filter(record))
        self.assertTrue(ClosedDjangoBoundaryFilter(finalize=True).filter(record))

        self.assertEqual(record.correlation_id, SERVER_CORRELATION_ID)
        self.assertEqual(record.exception_class, "HostileMiddlewareError")
        self.assertEqual(record.getMessage(), "django_boundary_event")
        self.assertNotIn(HOSTILE_DIAGNOSTIC, repr(record.__dict__))
        correlation_id_for.assert_called_once_with(request)

    @override_settings(DEBUG=False, ROOT_URLCONF=__name__)
    @patch("config.api_errors.secrets.token_hex", return_value=SERVER_CORRELATION_ID)
    def test_drf_response_and_django_log_share_one_correlation(self, token_hex):
        with self.assertLogs("django.request", level="WARNING") as captured:
            response = Client().get("/api/v1/drf-validation-failure/")

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["correlation_id"], SERVER_CORRELATION_ID)
        self.assertEqual(response["X-AniMemo-Correlation-ID"], SERVER_CORRELATION_ID)
        self.assertEqual(len(captured.records), 1)
        self.assertEqual(captured.records[0].correlation_id, SERVER_CORRELATION_ID)
        self.assertNotIn(HOSTILE_DIAGNOSTIC, repr(captured.records[0].__dict__))
        token_hex.assert_called_once_with(16)

    @patch("config.closed_logging.correlation_id_for", return_value=SERVER_CORRELATION_ID)
    def test_security_logger_drops_hostile_exception_and_request(self, correlation_id_for):
        request = RequestFactory().get(
            "/api/v1/private-path/?signature=PRIVATE",
            HTTP_HOST="private.internal",
            HTTP_AUTHORIZATION="Bearer PRIVATE_TOKEN",
        )
        logger = logging.getLogger("django.security.SuspiciousOperation")

        with self.assertLogs(logger, level="ERROR") as captured:
            try:
                raise HostileMiddlewareError(HOSTILE_DIAGNOSTIC)
            except HostileMiddlewareError:
                logger.error(
                    HOSTILE_DIAGNOSTIC,
                    exc_info=True,
                    extra={
                        "request": request,
                        "status_code": 400,
                        "raw_path": request.get_full_path(),
                        "correlation_id": "a" * 32,
                        "exception_class": "INJECTED_PRIVATE_VALUE",
                    },
                )

        self.assertEqual(len(captured.records), 1)
        record = captured.records[0]
        self.assertEqual(record.getMessage(), "django_boundary_event")
        self.assertEqual(record.args, ())
        self.assertIsNone(record.exc_info)
        self.assertIsNone(record.exc_text)
        self.assertIsNone(getattr(record, "request", None))
        self.assertFalse(hasattr(record, "raw_path"))
        self.assertEqual(record.event, "django_security_boundary")
        self.assertEqual(record.correlation_id, SERVER_CORRELATION_ID)
        self.assertEqual(record.exception_class, "HostileMiddlewareError")
        log_value = repr(record.__dict__) + "\n".join(captured.output)
        for forbidden in (
            HOSTILE_DIAGNOSTIC,
            "PRIVATE_TOKEN",
            "private.internal",
            "signature=PRIVATE",
            "private-path",
            "Traceback",
            "INJECTED_PRIVATE_VALUE",
        ):
            self.assertNotIn(forbidden, log_value)
        correlation_id_for.assert_called_once_with(request)

    @patch("config.closed_logging.correlation_id_for", return_value=SERVER_CORRELATION_ID)
    def test_server_logger_fails_closed_for_hostile_status_conversion(self, correlation_id_for):
        request = RequestFactory().get("/api/v1/private-path/?signature=PRIVATE")
        logger = logging.getLogger("django.server")

        with self.assertLogs(logger, level="ERROR") as captured:
            logger.error(
                HOSTILE_DIAGNOSTIC,
                extra={
                    "request": request,
                    "status_code": ExplosiveStatus(),
                    "correlation_id": "a" * 32,
                    "exception_class": "INJECTED_PRIVATE_VALUE",
                },
            )

        record = captured.records[0]
        self.assertEqual(record.getMessage(), "django_boundary_event")
        self.assertEqual(record.status_code, 500)
        self.assertEqual(record.correlation_id, SERVER_CORRELATION_ID)
        self.assertEqual(record.exception_class, "RequestBoundaryError")
        self.assertEqual(record.thread, 0)
        self.assertEqual(record.threadName, "")
        self.assertEqual(record.process, 0)
        self.assertEqual(record.processName, "")
        self.assertEqual(record.taskName, "")
        log_value = repr(record.__dict__) + "\n".join(captured.output)
        for forbidden in (HOSTILE_DIAGNOSTIC, "INJECTED_PRIVATE_VALUE", "signature=PRIVATE", "private-path"):
            self.assertNotIn(forbidden, log_value)
        correlation_id_for.assert_called_once_with(request)

    @override_settings(DEBUG=False, ROOT_URLCONF="config.urls")
    @patch("config.api_errors.secrets.token_hex", return_value=SERVER_CORRELATION_ID)
    def test_unknown_api_route_uses_strict_public_failure(self, token_hex):
        response = Client().get(
            "/api/v1/definitely-not-a-private-route/",
            HTTP_X_REQUEST_ID=HOSTILE_DIAGNOSTIC,
            HTTP_X_ANIMEMO_CORRELATION_ID=HOSTILE_DIAGNOSTIC,
        )

        self.assertEqual(response.status_code, 404)
        self.assertEqual(
            response.json(),
            {
                "code": "not_found",
                "detail": "请求的资源不存在。",
                "correlation_id": SERVER_CORRELATION_ID,
            },
        )
        self.assertEqual(
            response["X-AniMemo-Correlation-ID"],
            SERVER_CORRELATION_ID,
        )
        self.assertNotIn(HOSTILE_DIAGNOSTIC, response.content.decode("utf-8"))
        token_hex.assert_called_once_with(16)

    @override_settings(
        DEBUG=False,
        ROOT_URLCONF=__name__,
        MIDDLEWARE=(f"{__name__}.HostileExceptionMiddleware",),
        ALLOWED_HOSTS=("private.internal",),
    )
    @patch("config.api_errors.secrets.token_hex", return_value=SERVER_CORRELATION_ID)
    def test_unhandled_middleware_exception_uses_strict_public_failure(self, token_hex):
        with self.assertLogs("django.request", level="ERROR") as captured:
            response = Client(raise_request_exception=False).get(
                "/api/v1/middleware-crash/?signature=PRIVATE",
                HTTP_HOST="private.internal",
                HTTP_X_REQUEST_ID=HOSTILE_DIAGNOSTIC,
                HTTP_X_ANIMEMO_CORRELATION_ID=HOSTILE_DIAGNOSTIC,
            )

        self.assertEqual(response.status_code, 500)
        self.assertEqual(
            response.json(),
            {
                "code": "internal_error",
                "detail": "请求无法完成，请使用关联编号联系管理员。",
                "correlation_id": SERVER_CORRELATION_ID,
            },
        )
        self.assertEqual(
            response["X-AniMemo-Correlation-ID"],
            SERVER_CORRELATION_ID,
        )
        serialized = response.content.decode("utf-8") + repr(response.headers)
        self.assertNotIn(HOSTILE_DIAGNOSTIC, serialized)
        self.assertEqual(len(captured.records), 1)
        record = captured.records[0]
        self.assertEqual(record.getMessage(), "django_boundary_event")
        self.assertEqual(record.args, ())
        self.assertIsNone(record.exc_info)
        self.assertIsNone(record.exc_text)
        self.assertIsNone(getattr(record, "request", None))
        self.assertEqual(record.event, "django_request_boundary")
        self.assertEqual(record.stage, "http_request_boundary")
        self.assertEqual(record.status_code, 500)
        self.assertEqual(record.correlation_id, SERVER_CORRELATION_ID)
        self.assertEqual(record.exception_class, "HostileMiddlewareError")
        log_value = repr(record.__dict__) + "\n".join(captured.output)
        for forbidden in (
            HOSTILE_DIAGNOSTIC,
            "PRIVATE_TOKEN",
            "private.internal",
            "signature=PRIVATE",
            "middleware-crash",
            "Traceback",
        ):
            self.assertNotIn(forbidden, log_value)
        token_hex.assert_called_once_with(16)

    @override_settings(DEBUG=False, ROOT_URLCONF="config.urls")
    def test_non_api_unknown_route_keeps_default_html_boundary(self):
        response = Client().get("/definitely-not-an-api-route/")

        self.assertEqual(response.status_code, 404)
        self.assertTrue(response["Content-Type"].startswith("text/html"))
