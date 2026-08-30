from rest_framework.response import Response
from rest_framework.views import exception_handler as drf_exception_handler
from site_config.media_storage.common import MediaStorageError, MediaStorageExhausted

from .api_errors import canonical_error_status, canonicalize_payload, public_failure


def _canonicalize(response, exc, request):
    return canonicalize_payload(
        response.data,
        response.status_code,
        request=request,
        default_code=getattr(exc, "default_code", None),
        retry_after=getattr(exc, "wait", None) or response.headers.get("Retry-After"),
    )


def exception_handler(exc, context):
    request = context.get("request") if context else None
    response = drf_exception_handler(exc, context)
    if response is not None:
        response.status_code = canonical_error_status(response.status_code)
        response.data = _canonicalize(response, exc, request)
        response["X-AniMemo-Correlation-ID"] = response.data["correlation_id"]
        return response
    if isinstance(exc, MediaStorageError):
        status_code = 507 if isinstance(exc, MediaStorageExhausted) else 503
        failure = public_failure(
            request=request,
            candidate_code=exc.code,
            status_code=status_code,
        )
        response = Response(failure, status=status_code)
        response["X-AniMemo-Correlation-ID"] = failure["correlation_id"]
        return response
    failure = public_failure(
        request=request,
        candidate_code="internal_error",
        status_code=500,
    )
    response = Response(failure, status=500)
    response["X-AniMemo-Correlation-ID"] = failure["correlation_id"]
    return response
