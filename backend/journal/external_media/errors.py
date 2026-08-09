from rest_framework.exceptions import APIException


class ExternalMediaError(APIException):
    def __init__(self, code, detail, *, status_code, extra=None):
        self.status_code = status_code
        payload = {"code": code, "detail": detail}
        payload.update(extra or {})
        super().__init__(payload, code=code)
        self.detail = payload


def invalid_external_id(detail="外部作品 ID 无效。"):
    return ExternalMediaError("invalid_external_id", detail, status_code=400)


def unsupported_provider(provider):
    return ExternalMediaError(
        "unsupported_provider",
        f"暂不支持外部资料提供方：{provider or 'unknown'}。",
        status_code=400,
    )


def subject_not_found():
    return ExternalMediaError("subject_not_found", "没有找到对应的外部作品。", status_code=404)


def provider_unavailable():
    return ExternalMediaError("provider_unavailable", "外部资料服务暂时不可用，请稍后重试。", status_code=503)


def provider_timeout():
    return ExternalMediaError("provider_timeout", "外部资料服务响应超时，请稍后重试。", status_code=504)


def provider_invalid_response():
    return ExternalMediaError("provider_invalid_response", "外部资料服务返回了无法识别的数据。", status_code=502)


def identity_already_bound():
    return ExternalMediaError(
        "identity_already_bound",
        "该记录已绑定此资料提供方，请先解除原绑定。",
        status_code=409,
    )


def subject_already_bound(entry_id):
    return ExternalMediaError(
        "subject_already_bound",
        "该外部作品已绑定到你的另一条记录。",
        status_code=409,
        extra={"entry_id": entry_id},
    )


def identity_not_found():
    return ExternalMediaError("identity_not_found", "该记录尚未绑定此资料提供方。", status_code=404)
