from rest_framework.response import Response
from rest_framework.views import exception_handler as drf_exception_handler

from site_config.media_storage.common import MediaStorageError, MediaStorageExhausted


_STATUS_CODES = {
    400: "invalid_request",
    401: "authentication_required",
    403: "permission_denied",
    404: "not_found",
    405: "method_not_allowed",
    408: "request_timeout",
    409: "conflict",
    413: "payload_too_large",
    429: "rate_limited",
    500: "internal_error",
    503: "service_unavailable",
    507: "storage_exhausted",
}

_CODE_ALIASES = {
    "not_authenticated": "authentication_required",
    "authentication_failed": "authentication_failed",
    "throttled": "rate_limited",
    "invalid": "validation_error",
}


def _jsonable(value):
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return str(value)


def _canonicalize(response, exc):
    status_code = response.status_code
    payload = response.data
    fields = None
    detail = None
    code = _CODE_ALIASES.get(
        getattr(exc, "default_code", None),
        getattr(exc, "default_code", None) or _STATUS_CODES.get(status_code, "api_error"),
    )

    if isinstance(payload, dict):
        raw_code = payload.get("code")
        if raw_code:
            code = str(raw_code)
        detail = payload.get("detail")
        if detail is None:
            fields = _jsonable(payload)
            detail = "请求参数无效。" if status_code == 400 else "请求无法完成。"
        elif status_code == 400:
            extra_fields = {key: value for key, value in payload.items() if key not in {"detail", "code"}}
            if extra_fields:
                fields = _jsonable(extra_fields)
    elif isinstance(payload, list):
        fields = {"non_field_errors": _jsonable(payload)}
        detail = "请求参数无效。"
        code = "validation_error"
    else:
        detail = payload

    if status_code == 400 and not fields and getattr(exc, "default_code", "") in {"invalid", "validation_error"}:
        fields = _jsonable(payload) if isinstance(payload, dict) else None
        code = "validation_error"

    body = {"code": code, "detail": _jsonable(detail or "请求无法完成。")}
    if fields:
        body["fields"] = fields
    if status_code == 429:
        retry_after = getattr(exc, "wait", None)
        if retry_after is None:
            retry_after = response.headers.get("Retry-After")
        if retry_after is not None:
            try:
                body["retry_after_seconds"] = int(float(retry_after))
            except (TypeError, ValueError):
                pass
    return body


def exception_handler(exc, context):
    response = drf_exception_handler(exc, context)
    if response is not None:
        response.data = _canonicalize(response, exc)
        return response
    if isinstance(exc, MediaStorageError):
        status_code = 507 if isinstance(exc, MediaStorageExhausted) else 503
        return Response({"code": exc.code, "detail": str(exc.detail)}, status=status_code)
    return None
