from rest_framework import permissions
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView
from drf_spectacular.utils import OpenApiExample, extend_schema

from .errors import sync_request_invalid
from .serializers import CollectionSyncApplySerializer
from .services import apply_collection_sync, preview_collection_sync


class ExternalCollectionSyncPreviewView(APIView):
    permission_classes = [permissions.IsAuthenticated]
    throttle_scope = "external_sync_preview"

    def get(self, request, provider, entry_id):
        return Response(
            preview_collection_sync(
                user=request.user,
                provider_slug=provider,
                entry_id=entry_id,
            )
        )


class ExternalCollectionSyncApplyView(APIView):
    permission_classes = [permissions.IsAuthenticated]
    throttle_scope = "external_sync_apply"

    @extend_schema(
        request=CollectionSyncApplySerializer,
        examples=[
            OpenApiExample(
                "确认远程字段",
                value={
                    "preview_token": "preview-token-from-preview-response",
                    "actions": [
                        {"field": "status", "action": "pull_remote"},
                        {"field": "tags", "action": "accept_equal"},
                    ],
                },
                request_only=True,
            )
        ],
    )
    def post(self, request, provider, entry_id):
        serializer = CollectionSyncApplySerializer(data=request.data)
        try:
            serializer.is_valid(raise_exception=True)
        except ValidationError as error:
            raise sync_request_invalid() from error
        return Response(
            apply_collection_sync(
                user=request.user,
                provider_slug=provider,
                entry_id=entry_id,
                **serializer.validated_data,
            )
        )
