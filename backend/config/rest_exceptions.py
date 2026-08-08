from rest_framework.response import Response
from rest_framework.views import exception_handler as drf_exception_handler

from site_config.media_storage.common import MediaStorageError, MediaStorageExhausted


def exception_handler(exc, context):
    response = drf_exception_handler(exc, context)
    if response is not None:
        return response
    if isinstance(exc, MediaStorageError):
        status_code = 507 if isinstance(exc, MediaStorageExhausted) else 503
        return Response({"detail": exc.detail, "code": exc.code}, status=status_code)
    return None
