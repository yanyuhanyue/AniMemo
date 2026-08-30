from unittest.mock import patch

from django.test import SimpleTestCase
from rest_framework.exceptions import APIException, Throttled, ValidationError
from rest_framework.renderers import JSONRenderer
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.test import APIRequestFactory
from site_config.media_storage.common import MediaStorageOffline

from .api_errors import correlation_id_for, public_failure
from .api_renderers import CanonicalJSONRenderer
from .rest_exceptions import exception_handler

SERVER_CORRELATION_ID = "a" * 32
SENTINELS = (
    "/srv/private/runtime.sqlite3",
    "SELECT credential FROM secrets",
    "provider-state=DEGRADED",
    "Traceback (most recent call last)",
)
REQUIRED_FIXED_CODE_STATUSES = {
    "session_revoked": (401, 403),
    "registration_policy_rejected": (403,),
    "registration_policy_unavailable": (503,),
    "setup_code_changed": (409,),
    "admin_identity_unavailable": (409,),
    "invalid_import_file": (400,),
    "import_commit_failed": (400,),
    "bangumi_lookup_unavailable": (200, 502),
    "email_delivery_disabled": (409,),
    "email_delivery_not_configured": (400,),
    "email_delivery_failed": (502, 503),
    "invalid_poster_url": (400,),
    "unsafe_storage_path": (400,),
    "storage_root_unavailable": (400,),
    "staff_user_access_denied": (403,),
    "staff_permission_change_denied": (403,),
    "staff_two_factor_required": (403,),
    "staff_reauthentication_failed": (403,),
    "last_superuser_protected": (403,),
    "invalid_tag_definition": (400,),
    "service_check_failed": (503,),
    "updater_unavailable": (503,),
    "updater_request_failed": (400,),
    "incompatible_release": (409,),
    "update_in_progress": (409,),
    "invalid_operation_state": (409,),
    "manual_recovery_required": (409,),
    "provider_not_found": (404,),
    "invalid_analytics_range": (400,),
    "pairing_code_invalid": (400,),
    "identity_already_bound": (409,),
    "integration_action_failed": (400, 409, 500, 502, 503),
    "plugin_operation_failed": (400,),
    "plugin_runtime_unavailable": (503,),
    "plugin_scan_failed": (400,),
    "database_unavailable": (200,),
    "bangumi_unavailable": (200,),
    "plugin_health_unavailable": (200,),
    "plugin_publish_failed": (400, 503),
    "plugin_deployment_update_failed": (400,),
    "plugin_rollback_failed": (400,),
    "plugin_cleanup_failed": (400,),
    "plugin_install_failed": (400,),
    "plugin_project_create_failed": (400,),
    "plugin_project_update_failed": (400,),
    "plugin_project_archive_failed": (400,),
    "plugin_upload_failed": (400,),
    "plugin_preview_failed": (400,),
    "plugin_submission_failed": (400,),
    "plugin_submission_withdraw_failed": (400,),
    "plugin_review_failed": (400,),
    "plugin_revoke_failed": (400,),
    "plugin_manifest_invalid": (400,),
    "plugin_scan_stylesheet_invalid": (400,),
    "plugin_scan_source_invalid": (400,),
    "plugin_service_unavailable": (503,),
}


class CanonicalApiErrorTests(SimpleTestCase):
    def setUp(self):
        self.request = APIRequestFactory().post(
            "/api/test/",
            {},
            HTTP_X_ANIMEMO_CORRELATION_ID="client-controlled-correlation",
        )

    @patch("config.api_errors.secrets.token_hex", return_value=SERVER_CORRELATION_ID)
    def test_drf_wrapper_and_django_request_share_one_server_correlation(self, token_hex):
        django_request = APIRequestFactory().post(
            "/api/test/",
            {},
            HTTP_X_ANIMEMO_CORRELATION_ID="client-controlled-correlation",
        )
        drf_request = Request(django_request)

        failure = public_failure(
            request=drf_request,
            candidate_code="validation_error",
            status_code=400,
        )

        self.assertEqual(failure["correlation_id"], SERVER_CORRELATION_ID)
        self.assertEqual(correlation_id_for(django_request), SERVER_CORRELATION_ID)
        self.assertEqual(correlation_id_for(drf_request), SERVER_CORRELATION_ID)
        token_hex.assert_called_once_with(16)

    @patch("config.api_errors.secrets.token_hex", return_value=SERVER_CORRELATION_ID)
    def test_unknown_http_status_is_reframed_as_matching_internal_500(self, token_hex):
        class TeapotFailure(APIException):
            status_code = 418
            default_code = "private_teapot_failure"
            default_detail = "SELECT private FROM teapot Traceback STATUS_CANARY"

        handled = exception_handler(TeapotFailure(), {"request": self.request})
        handled.accepted_renderer = CanonicalJSONRenderer()
        handled.accepted_media_type = "application/json"
        handled.renderer_context = {"request": self.request, "response": handled}
        handled.render()

        self.assertEqual(handled.status_code, 500)
        self.assertEqual(
            handled.data,
            {
                "code": "internal_error",
                "detail": "请求无法完成，请使用关联编号联系管理员。",
                "correlation_id": SERVER_CORRELATION_ID,
            },
        )
        self.assertEqual(
            handled["X-AniMemo-Correlation-ID"],
            SERVER_CORRELATION_ID,
        )
        self.assertNotIn("STATUS_CANARY", str(handled.data))
        token_hex.assert_called_once_with(16)

    @patch("config.api_errors.secrets.token_hex", return_value=SERVER_CORRELATION_ID)
    def test_renderer_alone_reframes_unknown_status_to_internal_500(self, token_hex):
        response = Response(
            {"code": "private_teapot_failure", "detail": "STATUS_CANARY"},
            status=418,
        )
        response.accepted_renderer = CanonicalJSONRenderer()
        response.accepted_media_type = "application/json"
        response.renderer_context = {"request": self.request, "response": response}

        response.render()

        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.data["code"], "internal_error")
        self.assertEqual(set(response.data), {"code", "detail", "correlation_id"})
        self.assertEqual(
            response["X-AniMemo-Correlation-ID"],
            SERVER_CORRELATION_ID,
        )
        self.assertNotIn("STATUS_CANARY", str(response.data))
        token_hex.assert_called_once_with(16)

    @patch("config.api_errors.secrets.token_hex", return_value=SERVER_CORRELATION_ID)
    def test_validation_errors_use_strict_three_field_contract(self, token_hex):
        response = exception_handler(
            ValidationError({"title": [SENTINELS[0]]}),
            {"request": self.request},
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.data,
            {
                "code": "validation_error",
                "detail": "请求参数无效。",
                "correlation_id": SERVER_CORRELATION_ID,
            },
        )
        self.assertEqual(response["X-AniMemo-Correlation-ID"], SERVER_CORRELATION_ID)
        token_hex.assert_called_once_with(16)

    @patch("config.api_errors.secrets.token_hex", return_value=SERVER_CORRELATION_ID)
    def test_throttled_errors_keep_retry_header_outside_body(self, token_hex):
        response = exception_handler(
            Throttled(wait=7),
            {"request": self.request},
        )

        self.assertEqual(response.status_code, 429)
        self.assertEqual(
            response.data,
            {
                "code": "rate_limited",
                "detail": "操作过于频繁，请稍后重试。",
                "correlation_id": SERVER_CORRELATION_ID,
            },
        )
        self.assertEqual(response["Retry-After"], "7")
        token_hex.assert_called_once_with(16)

    @patch("config.api_errors.secrets.token_hex", return_value=SERVER_CORRELATION_ID)
    def test_renderer_discards_detail_metadata_fields_and_client_correlation(self, token_hex):
        response = Response(
            {
                "code": "conflict",
                "detail": SENTINELS[1],
                "metadata": {"provider": SENTINELS[2]},
                "fields": {"trace": [SENTINELS[3]]},
                "correlation_id": "client-controlled-correlation",
            },
            status=409,
        )
        response.accepted_renderer = CanonicalJSONRenderer()
        response.accepted_media_type = "application/json"
        response.renderer_context = {"request": self.request, "response": response}
        response.render()

        self.assertEqual(
            response.data,
            {
                "code": "conflict",
                "detail": "请求与资源当前状态冲突。",
                "correlation_id": SERVER_CORRELATION_ID,
            },
        )
        self.assertEqual(response["X-AniMemo-Correlation-ID"], SERVER_CORRELATION_ID)
        self.assertIsInstance(response.accepted_renderer, JSONRenderer)
        token_hex.assert_called_once_with(16)

    @patch("config.api_errors.secrets.token_hex", return_value=SERVER_CORRELATION_ID)
    def test_handler_and_renderer_reuse_one_server_correlation(self, token_hex):
        response = exception_handler(
            ValidationError({"title": ["无效"]}),
            {"request": self.request},
        )
        response.accepted_renderer = CanonicalJSONRenderer()
        response.accepted_media_type = "application/json"
        response.renderer_context = {"request": self.request, "response": response}

        response.render()

        self.assertEqual(response.data["correlation_id"], SERVER_CORRELATION_ID)
        self.assertEqual(response["X-AniMemo-Correlation-ID"], SERVER_CORRELATION_ID)
        token_hex.assert_called_once_with(16)

    @patch("config.api_errors.secrets.token_hex", return_value=SERVER_CORRELATION_ID)
    def test_unknown_code_and_status_mismatch_fail_closed(self, token_hex):
        unknown = public_failure(
            request=self.request,
            candidate_code="unknown_exception_type",
            status_code=409,
        )
        mismatched = public_failure(
            request=self.request,
            candidate_code="permission_denied",
            status_code=400,
        )

        self.assertEqual(unknown["code"], "conflict")
        self.assertEqual(unknown["detail"], "请求与资源当前状态冲突。")
        self.assertEqual(mismatched["code"], "invalid_request")
        self.assertEqual(mismatched["detail"], "请求参数无效。")
        self.assertEqual(unknown["correlation_id"], SERVER_CORRELATION_ID)
        self.assertEqual(mismatched["correlation_id"], SERVER_CORRELATION_ID)
        token_hex.assert_called_once_with(16)

    @patch("config.api_errors.secrets.token_hex", return_value=SERVER_CORRELATION_ID)
    def test_required_fixed_codes_are_explicitly_status_bound(self, token_hex):
        for code, statuses in REQUIRED_FIXED_CODE_STATUSES.items():
            for status_code in statuses:
                with self.subTest(code=code, status_code=status_code):
                    failure = public_failure(
                        request=self.request,
                        candidate_code=code,
                        status_code=status_code,
                    )
                    self.assertEqual(failure["code"], code)
                    self.assertEqual(
                        set(failure),
                        {"code", "detail", "correlation_id"},
                    )
                    self.assertEqual(
                        failure["correlation_id"],
                        SERVER_CORRELATION_ID,
                    )
        token_hex.assert_called_once_with(16)

    @patch("config.api_errors.secrets.token_hex", return_value=SERVER_CORRELATION_ID)
    def test_code_matching_is_exact_and_never_trims_input(self, token_hex):
        failure = public_failure(
            request=self.request,
            candidate_code=" staff_user_access_denied ",
            status_code=403,
        )

        self.assertEqual(failure["code"], "permission_denied")
        self.assertEqual(failure["detail"], "无权执行该操作。")
        self.assertEqual(failure["correlation_id"], SERVER_CORRELATION_ID)
        token_hex.assert_called_once_with(16)

    @patch("config.api_errors.secrets.token_hex", return_value=SERVER_CORRELATION_ID)
    def test_unrecognized_exception_returns_strict_internal_failure(self, token_hex):
        response = exception_handler(
            RuntimeError(" ".join(SENTINELS)),
            {"request": self.request},
        )

        self.assertEqual(response.status_code, 500)
        self.assertEqual(
            response.data,
            {
                "code": "internal_error",
                "detail": "请求无法完成，请使用关联编号联系管理员。",
                "correlation_id": SERVER_CORRELATION_ID,
            },
        )
        self.assertEqual(response["X-AniMemo-Correlation-ID"], SERVER_CORRELATION_ID)
        serialized = str(response.data)
        for sentinel in SENTINELS:
            self.assertNotIn(sentinel, serialized)
        token_hex.assert_called_once_with(16)

    @patch("config.api_errors.secrets.token_hex", return_value=SERVER_CORRELATION_ID)
    def test_media_storage_exception_never_exposes_internal_detail(self, token_hex):
        response = exception_handler(
            MediaStorageOffline(" ".join(SENTINELS)),
            {"request": self.request},
        )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(
            response.data,
            {
                "code": "MEDIA_STORAGE_OFFLINE",
                "detail": "服务暂时不可用，请稍后重试。",
                "correlation_id": SERVER_CORRELATION_ID,
            },
        )
        serialized = str(response.data)
        for sentinel in SENTINELS:
            self.assertNotIn(sentinel, serialized)
        token_hex.assert_called_once_with(16)
