import json

from config.api_errors import public_failure
from config.api_renderers import CanonicalJSONRenderer
from drf_spectacular.utils import extend_schema
from rest_framework.exceptions import ParseError
from rest_framework.parsers import BaseParser
from rest_framework.response import Response
from rest_framework.views import APIView

from .restore import (
    BundleRestoreError,
    cancel_restore,
    commit_restore,
    create_restore,
    current_restore,
    get_restore,
    upload_chunk,
    validate_restore,
)


class RestoreJSONParser(BaseParser):
    media_type = "application/json"

    def parse(self, stream, media_type=None, parser_context=None):
        raw = stream.read(16385)
        if len(raw) > 16384:
            raise ParseError("invalid_request")
        try:
            return json.loads(raw)
        except (ValueError, UnicodeError, RecursionError) as error:
            raise ParseError("invalid_request") from error


class RestoreChunkParser(BaseParser):
    media_type = "application/octet-stream"

    def parse(self, stream, media_type=None, parser_context=None):
        from django.conf import settings

        raw = stream.read(settings.BUNDLE_RESTORE_CHUNK_BYTES + 1)
        if len(raw) > settings.BUNDLE_RESTORE_CHUNK_BYTES:
            raise ParseError("payload_too_large")
        return raw


class RestoreView(APIView):
    parser_classes = (RestoreJSONParser,)
    renderer_classes = (CanonicalJSONRenderer,)

    def call(self, function, request, **kwargs):
        try:
            result = function(user=request.user, **kwargs)
            # Defensive check includes the actual success renderer and envelope.
            if len(self.renderer_classes[0]().render(result)) > 65536:
                raise BundleRestoreError("service_unavailable", 503)
            return Response(result)
        except BundleRestoreError as error:
            return Response(public_failure(request=request, candidate_code=error.code,
                                           status_code=error.status_code), status=error.status_code)

    def generation(self, request):
        if not isinstance(request.data, dict) or set(request.data) != {"generation"}:
            return None
        return request.data["generation"]


class BundleRestoreCreateView(RestoreView):
    @extend_schema(operation_id="bundle_restore_create", tags=["Data Bundle"], request=dict, responses=dict)
    def post(self, request):
        return self.call(create_restore, request, payload=request.data)


class BundleRestoreCurrentView(RestoreView):
    @extend_schema(operation_id="bundle_restore_current", tags=["Data Bundle"], responses=dict)
    def get(self, request):
        return self.call(current_restore, request)


class BundleRestoreStatusView(RestoreView):
    @extend_schema(operation_id="bundle_restore_status", tags=["Data Bundle"], responses=dict)
    def get(self, request, session_id):
        return self.call(get_restore, request, session_id=session_id)


class BundleRestoreChunkView(RestoreView):
    parser_classes = (RestoreChunkParser,)

    @extend_schema(operation_id="bundle_restore_chunk", tags=["Data Bundle"], request=bytes, responses=dict)
    def put(self, request, session_id):
        try:
            offset_raw = request.query_params.get("offset", "")
            generation_raw = request.query_params.get("generation", "")
            if len(offset_raw) > 20 or len(generation_raw) > 10 or not offset_raw.isascii() or not generation_raw.isascii():
                raise ValueError
            offset, generation = int(offset_raw), int(generation_raw)
        except ValueError:
            return self.call(lambda **_kwargs: _invalid(), request)
        return self.call(upload_chunk, request, session_id=session_id, generation=generation, offset=offset, data=request.data)


def _invalid():
    raise BundleRestoreError("invalid_request")


class BundleRestoreValidateView(RestoreView):
    @extend_schema(operation_id="bundle_restore_validate", tags=["Data Bundle"], request=dict, responses=dict)
    def post(self, request, session_id):
        return self.call(validate_restore, request, session_id=session_id, generation=self.generation(request))


class BundleRestoreCommitView(RestoreView):
    @extend_schema(operation_id="bundle_restore_commit", tags=["Data Bundle"], request=dict, responses=dict)
    def post(self, request, session_id):
        return self.call(commit_restore, request, session_id=session_id, generation=self.generation(request))


class BundleRestoreCancelView(RestoreView):
    @extend_schema(operation_id="bundle_restore_cancel", tags=["Data Bundle"], request=dict, responses=dict)
    def post(self, request, session_id):
        return self.call(cancel_restore, request, session_id=session_id, generation=self.generation(request))
