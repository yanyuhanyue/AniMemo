from rest_framework import permissions
from rest_framework.response import Response
from rest_framework.views import APIView

from .services import preview_collection_sync


class ExternalCollectionSyncPreviewView(APIView):
    permission_classes = [permissions.IsAuthenticated]
    throttle_scope = "external_account"

    def get(self, request, provider, entry_id):
        return Response(
            preview_collection_sync(
                user=request.user,
                provider_slug=provider,
                entry_id=entry_id,
            )
        )
