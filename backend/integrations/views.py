import json
import re
import time

from config.api_errors import public_failure
from django.conf import settings
from django.db import close_old_connections
from django.utils import timezone
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from .authentication import IntegrationHMACAuthentication
from .models import ExternalIdentityBinding, IntegrationConnection, IntegrationEvent
from .pairing import (
    IdentityAlreadyBound,
    PairingCodeInvalid,
    consume_pairing_code,
    create_pairing_code,
)
from .serializers import (
    ExternalIdentityBindingSerializer,
    IntegrationConnectionSerializer,
)
from .services import IntegrationDispatchError, dispatch_integration_action

PLATFORM_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
PUBLIC_ACTION_RE = re.compile(
    r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*\.[a-z][a-z0-9]*(?:-[a-z0-9]+)*$"
)

INTEGRATION_HMAC_PARAMETERS = [
    OpenApiParameter("X-AniMemo-Key-Id", str, OpenApiParameter.HEADER, required=True),
    OpenApiParameter("X-AniMemo-Timestamp", str, OpenApiParameter.HEADER, required=True),
    OpenApiParameter("X-AniMemo-Nonce", str, OpenApiParameter.HEADER, required=True),
    OpenApiParameter("X-AniMemo-Signature", str, OpenApiParameter.HEADER, required=True),
]


def _failure(request, candidate_code, status_code):
    return Response(
        public_failure(request=request, candidate_code=candidate_code, status_code=status_code),
        status=status_code,
    )


def _dispatch_failure(request, error):
    if error.code == "request_too_large":
        return _failure(request, "request_too_large", status.HTTP_413_REQUEST_ENTITY_TOO_LARGE)
    if error.code == "invalid_content_length":
        return _failure(request, "invalid_content_length", status.HTTP_400_BAD_REQUEST)
    if error.code == "invalid_json":
        return _failure(request, "invalid_json", status.HTTP_400_BAD_REQUEST)
    if error.code == "invalid_payload":
        return _failure(request, "invalid_payload", status.HTTP_400_BAD_REQUEST)
    if error.code == "identity_not_bound":
        return _failure(request, "identity_not_bound", status.HTTP_403_FORBIDDEN)
    if error.code == "plugin_not_installed":
        return _failure(request, "permission_denied", status.HTTP_403_FORBIDDEN)
    if error.code == "invalid_action":
        return _failure(request, "invalid_action", status.HTTP_400_BAD_REQUEST)
    if error.code == "invalid_action_response":
        return _failure(request, "invalid_action_response", status.HTTP_500_INTERNAL_SERVER_ERROR)
    if error.code == "action_response_too_large":
        return _failure(request, "integration_action_failed", status.HTTP_502_BAD_GATEWAY)
    if error.code == "request_id_conflict":
        return _failure(request, "request_id_conflict", status.HTTP_409_CONFLICT)
    if error.code == "request_in_progress":
        return _failure(request, "action_in_progress", status.HTTP_409_CONFLICT)
    if error.code == "plugin_unavailable":
        return _failure(request, "plugin_unavailable", status.HTTP_503_SERVICE_UNAVAILABLE)
    if error.code == "action_not_declared":
        return _failure(request, "action_not_declared", status.HTTP_404_NOT_FOUND)
    if error.code == "action_not_registered":
        return _failure(request, "action_not_registered", status.HTTP_503_SERVICE_UNAVAILABLE)
    return _failure(request, "integration_action_failed", status.HTTP_500_INTERNAL_SERVER_ERROR)


def _parse_json_object(request, max_bytes):
    content_length = request.META.get("CONTENT_LENGTH")
    try:
        if content_length and int(content_length) > max_bytes:
            raise IntegrationDispatchError("request_too_large", "请求体过大。", 413)
    except ValueError as error:
        raise IntegrationDispatchError("invalid_content_length", "Content-Length 无效。", 400) from error
    body = request.body
    if len(body) > max_bytes:
        raise IntegrationDispatchError("request_too_large", "请求体过大。", 413)
    try:
        payload = json.loads(body.decode("utf-8")) if body else {}
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise IntegrationDispatchError("invalid_json", "请求体必须是 UTF-8 JSON。", 400) from error
    if not isinstance(payload, dict):
        raise IntegrationDispatchError("invalid_json", "请求体必须是 JSON 对象。", 400)
    return payload


def _clean_string(payload, key, *, maximum, pattern=None):
    value = payload.get(key)
    if not isinstance(value, str):
        raise IntegrationDispatchError("invalid_request", f"{key} 必须是字符串。", 400)
    value = value.strip()
    if not value or len(value) > maximum or any(ord(character) < 32 for character in value):
        raise IntegrationDispatchError("invalid_request", f"{key} 无效。", 400)
    if pattern and not pattern.fullmatch(value):
        raise IntegrationDispatchError("invalid_request", f"{key} 无效。", 400)
    return value


class ConnectionsView(APIView):
    def get(self, request):
        rows = IntegrationConnection.objects.filter(enabled=True).order_by("provider", "instance_id")
        return Response({"connections": IntegrationConnectionSerializer(rows, many=True).data})


class PairingCodesView(APIView):
    def post(self, request):
        try:
            payload = _parse_json_object(
                request,
                int(getattr(settings, "INTEGRATION_PAIRING_REQUEST_MAX_BYTES", 8192)),
            )
            connection_id = _clean_string(payload, "connection_id", maximum=64)
            connection = IntegrationConnection.objects.get(pk=connection_id, enabled=True)
        except IntegrationConnection.DoesNotExist:
            return _failure(request, "connection_not_found", status.HTTP_404_NOT_FOUND)
        except (ValueError, IntegrationDispatchError) as error:
            if isinstance(error, IntegrationDispatchError):
                return _dispatch_failure(request, error)
            return _failure(request, "invalid_connection", status.HTTP_400_BAD_REQUEST)
        row, plaintext = create_pairing_code(connection, request.user)
        return Response(
            {
                "code": plaintext,
                "expires_at": row.expires_at,
                "connection": IntegrationConnectionSerializer(connection).data,
            },
            status=status.HTTP_201_CREATED,
        )


class BindingsView(APIView):
    def get(self, request):
        rows = ExternalIdentityBinding.objects.select_related("connection").filter(user=request.user)
        return Response({"bindings": ExternalIdentityBindingSerializer(rows, many=True).data})


class BindingDetailView(APIView):
    def delete(self, request, binding_id):
        deleted, _ = ExternalIdentityBinding.objects.filter(pk=binding_id, user=request.user).delete()
        if not deleted:
            return _failure(request, "binding_not_found", status.HTTP_404_NOT_FOUND)
        return Response(status=status.HTTP_204_NO_CONTENT)


class HMACAPIView(APIView):
    authentication_classes = (IntegrationHMACAuthentication,)
    permission_classes = (permissions.IsAuthenticated,)
    # HMAC nonce replay protection is the instance-authenticated request gate;
    # user-oriented DRF throttles assume request.user has an account pk.
    throttle_classes = ()


class PairConsumeView(HMACAPIView):
    @extend_schema(parameters=INTEGRATION_HMAC_PARAMETERS)
    def post(self, request):
        try:
            payload = _parse_json_object(
                request,
                int(getattr(settings, "INTEGRATION_PAIRING_REQUEST_MAX_BYTES", 8192)),
            )
            unexpected = set(payload) - {"code", "platform", "external_user_id", "display_name"}
            if unexpected:
                raise IntegrationDispatchError("invalid_request", "配对请求包含未知字段。", 400)
            code = _clean_string(payload, "code", maximum=16)
            platform = _clean_string(payload, "platform", maximum=64, pattern=PLATFORM_RE)
            external_user_id = _clean_string(payload, "external_user_id", maximum=255)
            display_name = payload.get("display_name", "")
            if not isinstance(display_name, str) or len(display_name.strip()) > 160:
                raise IntegrationDispatchError("invalid_request", "display_name 无效。", 400)
            binding = consume_pairing_code(
                request.auth,
                code,
                platform,
                external_user_id,
                display_name.strip(),
            )
        except PairingCodeInvalid:
            return _failure(request, "pairing_code_invalid", status.HTTP_400_BAD_REQUEST)
        except IdentityAlreadyBound:
            return _failure(request, "identity_already_bound", status.HTTP_409_CONFLICT)
        except IntegrationDispatchError as error:
            return _dispatch_failure(request, error)
        return Response(
            {
                "binding_id": binding.pk,
                "platform": binding.platform,
                "external_user_id": binding.external_user_id,
                "user": {"id": binding.user_id, "username": binding.user.get_username()},
            },
            status=status.HTTP_201_CREATED,
        )


class ActionsView(HMACAPIView):
    @extend_schema(parameters=INTEGRATION_HMAC_PARAMETERS)
    def post(self, request):
        try:
            payload = _parse_json_object(
                request,
                int(getattr(settings, "INTEGRATION_ACTION_REQUEST_MAX_BYTES", 262144)),
            )
            unexpected = set(payload) - {
                "request_id", "platform", "external_user_id", "action", "payload"
            }
            if unexpected:
                raise IntegrationDispatchError("invalid_request", "动作请求包含未知字段。", 400)
            envelope = {
                "request_id": _clean_string(
                    payload, "request_id", maximum=128, pattern=REQUEST_ID_RE
                ),
                "platform": _clean_string(payload, "platform", maximum=64, pattern=PLATFORM_RE),
                "external_user_id": _clean_string(payload, "external_user_id", maximum=255),
                "action": _clean_string(
                    payload, "action", maximum=180, pattern=PUBLIC_ACTION_RE
                ),
                "payload": payload.get("payload", {}),
            }
            if not isinstance(envelope["payload"], dict):
                raise IntegrationDispatchError("invalid_payload", "payload 必须是 JSON 对象。", 400)
            response_payload, response_status, replayed = dispatch_integration_action(request, request.auth, envelope)
        except IntegrationDispatchError as error:
            return _dispatch_failure(request, error)
        response = Response(response_payload, status=response_status)
        response["X-AniMemo-Idempotent-Replay"] = "true" if replayed else "false"
        return response


class EventsView(HMACAPIView):
    @extend_schema(parameters=INTEGRATION_HMAC_PARAMETERS)
    def get(self, request):
        try:
            after = int(request.query_params.get("after", "0"))
            limit = int(request.query_params.get("limit", "50"))
            wait = int(
                request.query_params.get(
                    "wait",
                    str(getattr(settings, "INTEGRATION_EVENT_WAIT_DEFAULT_SECONDS", 1)),
                )
            )
        except (TypeError, ValueError):
            return _failure(request, "invalid_query", status.HTTP_400_BAD_REQUEST)
        maximum_wait = int(getattr(settings, "INTEGRATION_EVENT_WAIT_MAX_SECONDS", 25))
        if after < 0 or not 1 <= limit <= 100 or not 0 <= wait <= maximum_wait:
            return _failure(request, "invalid_query", status.HTTP_400_BAD_REQUEST)

        deadline = time.monotonic() + wait
        rows = []
        while True:
            rows = list(
                IntegrationEvent.objects.filter(
                    connection=request.auth,
                    id__gt=after,
                    acked_at__isnull=True,
                ).order_by("id")[:limit]
            )
            if rows or time.monotonic() >= deadline:
                break
            time.sleep(min(0.25, max(0, deadline - time.monotonic())))
            close_old_connections()
        events = [
            {
                "event_id": row.pk,
                "platform": row.platform,
                "external_user_id": row.external_user_id,
                "plugin_slug": row.plugin_slug,
                "event_name": row.event_name,
                "event": f"{row.plugin_slug}.{row.event_name}",
                "payload": row.payload,
                "route_type": row.route_type,
                "created_at": row.created_at,
            }
            for row in rows
        ]
        next_cursor = rows[-1].pk if rows else after
        return Response({"events": events, "next_cursor": next_cursor})


class EventsAckView(HMACAPIView):
    @extend_schema(parameters=INTEGRATION_HMAC_PARAMETERS)
    def post(self, request):
        try:
            payload = _parse_json_object(
                request,
                int(getattr(settings, "INTEGRATION_ACK_REQUEST_MAX_BYTES", 16384)),
            )
            if set(payload) != {"event_ids"}:
                raise IntegrationDispatchError("invalid_request", "ACK 请求字段无效。", 400)
            event_ids = payload.get("event_ids")
            if (
                not isinstance(event_ids, list)
                or len(event_ids) > 100
                or any(isinstance(value, bool) or not isinstance(value, int) or value <= 0 for value in event_ids)
            ):
                raise IntegrationDispatchError("invalid_request", "event_ids 无效。", 400)
        except IntegrationDispatchError as error:
            return _dispatch_failure(request, error)
        now = timezone.now()
        acked = IntegrationEvent.objects.filter(
            connection=request.auth,
            pk__in=set(event_ids),
            acked_at__isnull=True,
        ).update(acked_at=now)
        return Response({"acked": acked})
