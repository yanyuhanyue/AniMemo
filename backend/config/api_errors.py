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
    "authentication_failed": "invalid_credentials",
    "no_active_account": "invalid_credentials",
    "token_not_valid": "session_expired",
    "throttled": "rate_limited",
    "invalid": "validation_error",
}


def _jsonable(value):
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def canonicalize_payload(payload, status_code, *, default_code=None, retry_after=None):
    """Normalize DRF JSON errors, including hand-written Response payloads."""
    fields = None
    metadata = None
    compatibility = {}
    detail = None
    code = _CODE_ALIASES.get(
        default_code,
        default_code or _STATUS_CODES.get(status_code, "api_error"),
    )

    if isinstance(payload, dict) and payload.get("code") and "detail" in payload:
        body = _jsonable(payload)
        if status_code == 429 and "retry_after_seconds" not in body and retry_after is not None:
            try:
                body["retry_after_seconds"] = int(float(retry_after))
            except (TypeError, ValueError):
                pass
        return body

    if isinstance(payload, dict):
        raw_code = payload.get("code")
        if raw_code:
            code = str(raw_code)
        detail = payload.get("detail")
        if detail is None:
            fields = _jsonable(payload)
            compatibility.update(fields)
            if status_code == 400:
                code = "validation_error"
                detail = "请求参数无效。"
            else:
                detail = "请求无法完成。"
        else:
            extra_fields = {key: value for key, value in payload.items() if key not in {"detail", "code"}}
            if extra_fields:
                validation_fields = {
                    key: value for key, value in extra_fields.items() if isinstance(value, (list, tuple, dict))
                }
                metadata_fields = {key: value for key, value in extra_fields.items() if key not in validation_fields}
                if validation_fields:
                    fields = _jsonable(validation_fields)
                if metadata_fields:
                    metadata = _jsonable(metadata_fields)
                compatibility.update(_jsonable(extra_fields))
    elif isinstance(payload, list):
        fields = {"non_field_errors": _jsonable(payload)}
        detail = "请求参数无效。"
        code = "validation_error"
    else:
        detail = payload

    if status_code == 400 and not fields and default_code in {"invalid", "validation_error"}:
        fields = _jsonable(payload) if isinstance(payload, dict) else None
        code = "validation_error"

    body = {"code": code, "detail": _jsonable(detail or "请求无法完成。")}
    if fields:
        body["fields"] = fields
    if metadata:
        body["metadata"] = metadata
    body.update(compatibility)
    if status_code == 429 and retry_after is not None:
        try:
            body["retry_after_seconds"] = int(float(retry_after))
        except (TypeError, ValueError):
            pass
    return body
