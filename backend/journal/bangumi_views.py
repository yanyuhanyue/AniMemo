from rest_framework import permissions
from rest_framework.response import Response
from rest_framework.views import APIView

from .external_media.errors import ExternalMediaError
from .external_media.registry import get_provider


class BangumiSearchView(APIView):
    """Public compatibility endpoint backed by the Bangumi provider."""

    permission_classes = [permissions.AllowAny]
    throttle_scope = "external_search"

    def get(self, request):
        query = request.query_params.get("q", "").strip()
        if len(query) < 2:
            return Response({"results": []})
        try:
            results = get_provider("bangumi").search(query)
        except ExternalMediaError as error:
            return Response({"results": [], **error.detail}, status=error.status_code)
        return Response({"results": results})


class BangumiAutofillView(APIView):
    """Public compatibility endpoint for normalized Bangumi subject metadata."""

    permission_classes = [permissions.AllowAny]
    throttle_scope = "external_search"

    def get(self, request):
        provider = get_provider("bangumi")
        try:
            metadata = provider.fetch_subject(request.query_params.get("id", ""))
        except ExternalMediaError as error:
            return Response(error.detail, status=error.status_code)
        return Response(provider.legacy_result(metadata))
