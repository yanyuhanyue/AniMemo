from urllib.parse import urlencode

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
    def _redirect(provider, outcome, code=""):
        query = {
            "external_account_provider": provider,
            "external_account_status": outcome,
        }
        if code:
            query["code"] = code
        return HttpResponseRedirect(f"{settings.FRONTEND_URL}/dashboard?{urlencode(query)}")

    def get(self, request, provider):
        try:
            complete_oauth_authorization(
                provider_slug=provider,
                code=request.query_params.get("code"),
                state=request.query_params.get("state"),
            )
        except ExternalAccountError as error:
            return self._redirect(provider, "error", str(error.detail.get("code") or "authorization_exchange_failed"))
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
        return Response(result)
