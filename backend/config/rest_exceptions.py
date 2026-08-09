from rest_framework.response import Response
from rest_framework.views import exception_handler as drf_exception_handler

from site_config.media_storage.common import MediaStorageError, MediaStorageExhausted
from .api_errors import canonicalize_payload


def _canonicalize(response, exc):
    return canonicalize_payload(
        response.data,
        response.status_code,
        default_code=getattr(exc, "default_code", None),
        retry_after=getattr(exc, "wait", None) or response.headers.get("Retry-After"),
    )


def exception_handler(exc, context):
    response = drf_exception_handler(exc, context)
    if response is not None:
        response.data = _canonicalize(response, exc)
        return response
    if isinstance(exc, MediaStorageError):
        status_code = 507 if isinstance(exc, MediaStorageExhausted) else 503
        return Response({"code": exc.code, "detail": str(exc.detail)}, status=status_code)
    return None
