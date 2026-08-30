from rest_framework.renderers import JSONRenderer

from .api_errors import canonical_error_status, canonicalize_payload


class CanonicalJSONRenderer(JSONRenderer):
    """Apply the strict public failure contract to every DRF error response."""

    def render(self, data, accepted_media_type=None, renderer_context=None):
        context = renderer_context or {}
        response = context.get("response")
        if response is not None and response.status_code >= 400:
            response.status_code = canonical_error_status(response.status_code)
            data = canonicalize_payload(
                data,
                response.status_code,
                request=context.get("request"),
                retry_after=response.headers.get("Retry-After"),
            )
            response.data = data
            response["X-AniMemo-Correlation-ID"] = data["correlation_id"]
        return super().render(data, accepted_media_type, renderer_context)
