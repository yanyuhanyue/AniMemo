from rest_framework.renderers import JSONRenderer

from .api_errors import canonicalize_payload


class CanonicalJSONRenderer(JSONRenderer):
    """Apply the API error contract to hand-written DRF Responses."""

    def render(self, data, accepted_media_type=None, renderer_context=None):
        context = renderer_context or {}
        response = context.get("response")
        if response is not None and response.status_code >= 400:
            data = canonicalize_payload(
                data,
                response.status_code,
                retry_after=response.headers.get("Retry-After"),
            )
            response.data = data
        return super().render(data, accepted_media_type, renderer_context)
