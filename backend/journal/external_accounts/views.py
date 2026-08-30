from urllib.parse import urlencode
from uuid import UUID

from config.api_errors import public_failure
from django.conf import settings
from django.http import HttpResponseRedirect
from drf_spectacular.utils import OpenApiExample, extend_schema
from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from .errors import ExternalAccountError
from .services import (
    apply_import_preview,
    complete_oauth_authorization,
    connect_personal_access_token,
    create_import_preview,
    disconnect_account,
    get_import_preview,
    list_account_providers,
    serialize_connection,
    start_oauth_authorization,
    verify_connection,
)

_IMPORT_RESULT_STATUSES = ("created", "bound", "updated", "skipped", "conflict", "failed")
_IMPORT_FAILURE_CODES = frozenset({"import_item_invalid", "import_conflict", "provider_unavailable"})


def _canonical_preview_id(value):
    try:
        return str(UUID(str(value)))
    except (TypeError, ValueError, AttributeError):
        return ""


def _public_import_result(request, result):
    source_results = result.get("results", []) if isinstance(result, dict) else []
    projected_results = []
    for source in source_results if isinstance(source_results, list) else []:
        if not isinstance(source, dict):
            source = {}
        external_id = source.get("external_id")
        if not isinstance(external_id, str) or len(external_id) > 200:
            external_id = ""
        status_value = source.get("status")
        if status_value not in _IMPORT_RESULT_STATUSES:
            status_value = "failed"
        item = {"external_id": external_id, "status": status_value}
        if status_value in {"conflict", "failed"}:
            candidate_code = source.get("code")
            if candidate_code not in _IMPORT_FAILURE_CODES:
                candidate_code = "import_item_invalid"
            item["error"] = public_failure(
                request=request,
                candidate_code=candidate_code,
                status_code=status.HTTP_200_OK,
            )
        else:
            entry_id = source.get("entry_id")
            if isinstance(entry_id, int) and not isinstance(entry_id, bool) and entry_id > 0:
                item["entry_id"] = entry_id
            updated_fields = source.get("updated_fields")
            if isinstance(updated_fields, list):
                item["updated_fields"] = [
                    field
                    for field in updated_fields
                    if field in {"personal_score", "watch_status", "review"}
                ]
        projected_results.append(item)
    counts = {
        name: sum(1 for item in projected_results if item["status"] == name)
        for name in _IMPORT_RESULT_STATUSES
    }
    return {
        "preview_id": _canonical_preview_id(result.get("preview_id") if isinstance(result, dict) else None),
        "results": projected_results,
        "counts": counts,
    }


def _oauth_callback_failure(request, error):
    return public_failure(
        request=request,
        candidate_code=error.public_code,
        status_code=error.status_code,
    )
from journal.openapi_serializers import (
    ExternalAccountAuthorizeResponseSerializer,
    ExternalAccountConnectRequestSerializer,
    ExternalImportApplyRequestSerializer,
    ExternalImportPreviewRequestSerializer,
)


class ExternalAccountListView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        return Response({"providers": list_account_providers(request.user)})


class ExternalAccountConnectView(APIView):
    permission_classes = [permissions.IsAuthenticated]
    throttle_scope = "external_account"

    @extend_schema(
        request=ExternalAccountConnectRequestSerializer,
        examples=[OpenApiExample("个人访问令牌", value={"access_token": "fake-token"}, request_only=True)],
    )
    def post(self, request, provider):
        connection = connect_personal_access_token(
            user=request.user,
            provider_slug=provider,
            access_token=request.data.get("access_token"),
        )
        return Response(serialize_connection(connection), status=status.HTTP_201_CREATED)


class ExternalAccountDetailView(APIView):
    permission_classes = [permissions.IsAuthenticated]
    throttle_scope = "external_account"

    def delete(self, request, provider):
        disconnect_account(user=request.user, provider_slug=provider)
        return Response(status=status.HTTP_204_NO_CONTENT)


class ExternalAccountVerifyView(APIView):
    permission_classes = [permissions.IsAuthenticated]
    throttle_scope = "external_account"

    def post(self, request, provider):
        return Response(serialize_connection(verify_connection(user=request.user, provider_slug=provider)))


class ExternalAccountAuthorizeView(APIView):
    permission_classes = [permissions.IsAuthenticated]
    throttle_scope = "external_account"

    @extend_schema(responses=ExternalAccountAuthorizeResponseSerializer)
    def post(self, request, provider):
        return Response({
            "provider": provider,
            "authorization_url": start_oauth_authorization(user=request.user, provider_slug=provider),
        })


class ExternalAccountCallbackView(APIView):
    authentication_classes = []
    permission_classes = [permissions.AllowAny]
    throttle_scope = "external_account"

    @staticmethod
    def _redirect(provider, outcome, failure=None):
        query = {
            "external_account_provider": provider,
            "external_account_status": outcome,
        }
        if failure is not None:
            query["code"] = failure["code"]
            query["correlation_id"] = failure["correlation_id"]
        response = HttpResponseRedirect(f"{settings.FRONTEND_URL}/dashboard?{urlencode(query)}")
        if failure is not None:
            response["X-AniMemo-Correlation-ID"] = failure["correlation_id"]
        return response

    def get(self, request, provider):
        try:
            complete_oauth_authorization(
                provider_slug=provider,
                code=request.query_params.get("code"),
                state=request.query_params.get("state"),
            )
        except ExternalAccountError as error:
            failure = _oauth_callback_failure(request, error)
            return self._redirect(provider, "error", failure)
        return self._redirect(provider, "connected")


class ExternalAccountImportPreviewView(APIView):
    permission_classes = [permissions.IsAuthenticated]
    throttle_scope = "external_import_preview"

    @extend_schema(request=ExternalImportPreviewRequestSerializer)
    def post(self, request, provider):
        session = create_import_preview(user=request.user, provider_slug=provider)
        return Response(
            get_import_preview(
                user=request.user,
                provider_slug=provider,
                preview_id=session.pk,
                page=request.data.get("page", 1),
                page_size=request.data.get("page_size", 24),
                filter_value=request.data.get("filter", "all"),
                query=request.data.get("query", ""),
            ),
            status=status.HTTP_201_CREATED,
        )


class ExternalAccountImportPreviewDetailView(APIView):
    permission_classes = [permissions.IsAuthenticated]
    throttle_scope = "external_import_preview"

    def get(self, request, provider, preview_id):
        return Response(get_import_preview(
            user=request.user,
            provider_slug=provider,
            preview_id=preview_id,
            page=request.query_params.get("page", 1),
            page_size=request.query_params.get("page_size", 24),
            filter_value=request.query_params.get("filter", "all"),
            query=request.query_params.get("query", ""),
        ))


class ExternalAccountImportApplyView(APIView):
    permission_classes = [permissions.IsAuthenticated]
    throttle_scope = "external_import_apply"

    @extend_schema(
        request=ExternalImportApplyRequestSerializer,
        examples=[
            OpenApiExample(
                "确认导入条目",
                value={"preview_id": "00000000-0000-4000-8000-000000000000", "items": [{"row": 1, "action": "import"}]},
                request_only=True,
            )
        ],
    )
    def post(self, request, provider):
        result = apply_import_preview(
            user=request.user,
            provider_slug=provider,
            preview_id=request.data.get("preview_id"),
            items=request.data.get("items"),
        )
        return Response(_public_import_result(request, result))
