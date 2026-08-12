from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_protect
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from journal.web_auth_adapter import no_store

from .first_run import SetupCompletionError, complete_first_run_setup
from .models import InstallationState
from .serializers import (
    FirstRunSetupSerializer,
    InstallationErrorResponseSerializer,
    InstallationSetupResponseSerializer,
    InstallationStatusSerializer,
)


def installation_state_unavailable_response():
    return no_store(Response(
        {
            "code": "installation_state_unavailable",
            "detail": "安装状态不可用，已拒绝初始化请求。",
        },
        status=status.HTTP_503_SERVICE_UNAVAILABLE,
    ))


class InstallationStatusView(APIView):
    authentication_classes = []
    permission_classes = [permissions.AllowAny]

    @extend_schema(
        responses={200: InstallationStatusSerializer, 503: InstallationErrorResponseSerializer},
        auth=[],
    )
    def get(self, _request):
        try:
            installation = InstallationState.load()
        except InstallationState.DoesNotExist:
            return installation_state_unavailable_response()
        return no_store(Response({
            "state": installation.status,
            "accepting_setup": installation.accepting_setup,
            "expires_at": installation.setup_code_expires_at if installation.accepting_setup else None,
        }))


@method_decorator(csrf_protect, name="dispatch")
class InstallationSetupView(APIView):
    authentication_classes = []
    permission_classes = [permissions.AllowAny]
    throttle_scope = "first_run_setup"
    account_throttle_scope = "first_run_setup"
    throttle_account_fields = ("username", "email")

    @extend_schema(
        request=FirstRunSetupSerializer,
        responses={
            201: InstallationSetupResponseSerializer,
            400: InstallationErrorResponseSerializer,
            404: InstallationErrorResponseSerializer,
            409: InstallationErrorResponseSerializer,
            410: InstallationErrorResponseSerializer,
            429: InstallationErrorResponseSerializer,
            503: InstallationErrorResponseSerializer,
        },
        auth=[],
        parameters=[
            OpenApiParameter(
                "X-CSRFToken",
                str,
                OpenApiParameter.HEADER,
                required=True,
            )
        ],
    )
    def post(self, request):
        serializer = FirstRunSetupSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        payload = serializer.validated_data
        try:
            complete_first_run_setup(
                code=payload["code"],
                username=payload["username"],
                email=payload["email"],
                password=payload["password"],
                request=request,
            )
        except InstallationState.DoesNotExist:
            return installation_state_unavailable_response()
        except SetupCompletionError as error:
            return no_store(Response(
                {"code": error.code, "detail": error.detail},
                status=error.status_code,
            ))
        return no_store(Response(
            {"state": InstallationState.Status.INITIALIZED, "detail": "初始化完成，请登录管理员账号。"},
            status=status.HTTP_201_CREATED,
        ))
