from rest_framework import permissions
from rest_framework.response import Response
from rest_framework.views import APIView

from .errors import ExternalMediaError
from .registry import get_provider


class ExternalMediaSearchView(APIView):
    permission_classes = [permissions.AllowAny]
    throttle_scope = "external_search"

    def get(self, request, provider):
        query = str(request.query_params.get("q") or "").strip()
        if len(query) < 2:
            return Response({"provider": str(provider), "results": []})
        try:
            adapter = get_provider(provider)
            results = adapter.search(query)
        except ExternalMediaError as error:
            return Response(error.detail, status=error.status_code)
        return Response({"provider": adapter.slug, "results": results})


class ExternalMediaSubjectView(APIView):
    permission_classes = [permissions.AllowAny]
    throttle_scope = "external_search"

    def get(self, request, provider, external_id):
        force = (
            str(request.query_params.get("force") or "").lower() == "true"
            and bool(request.user and request.user.is_authenticated and request.user.is_staff)
        )
        try:
            adapter = get_provider(provider)
            metadata = adapter.fetch_subject(external_id, force=force)
        except ExternalMediaError as error:
            return Response(error.detail, status=error.status_code)
        return Response(metadata)
