"""HTTP and final renderer boundary for the bounded public catalogue."""
from config.api_errors import public_failure
from config.api_renderers import CanonicalJSONRenderer
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import permissions
from rest_framework.response import Response
from rest_framework.views import APIView

from . import public_catalog as catalog
from .public_catalog import schemas
from .public_catalog.contracts import ERROR_BYTES, SUCCESS_BYTES


class PublicCatalogJSONRenderer(CanonicalJSONRenderer):
    """Measure the bytes actually returned, independent of Accept indentation."""

    def get_indent(self, accepted_media_type, renderer_context):
        return None

    def encode_payload(self, data, accepted_media_type=None, renderer_context=None):
        return super().render(data, accepted_media_type, renderer_context)

    def render(self, data, accepted_media_type=None, renderer_context=None):
        context = renderer_context or {}
        response = context.get("response")
        encoded = self.encode_payload(data, accepted_media_type, context)
        limit = ERROR_BYTES if response is not None and response.status_code >= 400 else SUCCESS_BYTES
        if len(encoded) <= limit:
            return encoded
        failure = public_failure(request=context.get("request"), candidate_code="public_catalog_budget_exceeded", status_code=500)
        if response is not None:
            response.status_code = 500
            response.data = failure
        return super().render(failure, accepted_media_type, context)


class PublicCatalogView(APIView):
    permission_classes = (permissions.AllowAny,)
    renderer_classes = (PublicCatalogJSONRenderer,)
    scope_kind = "homepage"

    def render_payload(self, payload):
        response = Response(payload)
        return self.request.accepted_renderer.encode_payload(payload, self.request.accepted_media_type, {"request": self.request, "view": self, "response": response})

    def dispatch_read(self, request, reader, *, public_slug=None, **kwargs):
        try:
            version = catalog.require_database()
            scope = catalog.resolve_scope(request, self.scope_kind, public_slug)
            if "entry_id" in kwargs and not 1 <= kwargs["entry_id"] < 2 ** 63:
                raise catalog.CatalogError("not_found", 404)
            if reader in {catalog.read_entries, catalog.read_directory, catalog.read_detail}:
                kwargs.update(request=request, collation_version=version)
            elif reader is catalog.read_facets:
                kwargs["collation_version"] = version
            payload = reader(scope, params=request.query_params, render=self.render_payload, **kwargs)
            return Response(payload)
        except catalog.CatalogError as exc:
            return Response(public_failure(request=request, candidate_code=exc.code, status_code=exc.status), status=exc.status)


class PublicCatalogEntriesView(PublicCatalogView):
    @extend_schema(parameters=schemas.FILTER_PARAMS + schemas.PAGE_PARAMS, responses=schemas.responses(schemas.ENTRIES), description=schemas.DESCRIPTION)
    def get(self, request, public_slug=None):
        return self.dispatch_read(request, catalog.read_entries, public_slug=public_slug)


class PublicCatalogSummaryView(PublicCatalogView):
    @extend_schema(parameters=schemas.FILTER_PARAMS, responses=schemas.responses(schemas.SUMMARY), description=schemas.DESCRIPTION)
    def get(self, request, public_slug=None):
        return self.dispatch_read(request, catalog.read_summary, public_slug=public_slug)


class PublicCatalogFacetsView(PublicCatalogView):
    @extend_schema(parameters=schemas.PAGE_PARAMS + [OpenApiParameter("kind", str, enum=["tags", "years"], default="tags"), OpenApiParameter("search", str), OpenApiParameter("value_token", str), OpenApiParameter("revision", str), OpenApiParameter("part_size", {"type": "integer", "minimum": 1, "maximum": 16384})], responses=schemas.responses({"oneOf": [schemas.FACETS, schemas.FIELD]}), description=schemas.DESCRIPTION)
    def get(self, request, public_slug=None):
        return self.dispatch_read(request, catalog.read_facets, public_slug=public_slug)


class PublicCatalogDetailView(PublicCatalogView):
    @extend_schema(parameters=[OpenApiParameter("revision", str)], responses=schemas.responses(schemas.DETAIL), description=schemas.DESCRIPTION)
    def get(self, request, entry_id, public_slug=None):
        return self.dispatch_read(request, catalog.read_detail, public_slug=public_slug, entry_id=entry_id)


class PublicCatalogFieldView(PublicCatalogView):
    @extend_schema(parameters=schemas.FIELD_PARAMS, responses=schemas.responses(schemas.FIELD), description=schemas.DESCRIPTION)
    def get(self, request, entry_id, field, public_slug=None):
        return self.dispatch_read(request, catalog.read_field, public_slug=public_slug, entry_id=entry_id, field=field)


class PublicCatalogDirectoryView(PublicCatalogView):
    scope_kind = "directory"

    @extend_schema(parameters=schemas.PAGE_PARAMS + [OpenApiParameter("search", str)], responses=schemas.responses(schemas.DIRECTORY), description=schemas.DESCRIPTION)
    def get(self, request):
        return self.dispatch_read(request, catalog.read_directory)


class RetiredPublicCatalogView(PublicCatalogView):
    """Fixed retirement response for the former unbounded read routes."""
    authentication_classes = ()
    throttle_classes = ()

    @extend_schema(responses={410: schemas.ERROR}, deprecated=True)
    def get(self, request, public_slug=None):
        response = Response(public_failure(request=request, candidate_code="public_catalog_retired", status_code=410), status=410)
        response["Link"] = '</api/v1/docs/>; rel="successor-version"'
        return response
