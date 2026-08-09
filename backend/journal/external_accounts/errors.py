from rest_framework.exceptions import APIException


class ExternalAccountError(APIException):
    def __init__(self, code, detail, *, status_code, extra=None):
        self.status_code = status_code
        payload = {"code": code, "detail": detail}
        payload.update(extra or {})
        super().__init__(payload, code=code)
        self.detail = payload


def account_error(code, detail, *, status_code=400, extra=None):
    return ExternalAccountError(code, detail, status_code=status_code, extra=extra)


def account_not_configured():
    return account_error("external_account_not_configured", "此外部账号连接方式尚未配置。", status_code=503)


def account_already_connected():
    return account_error("external_account_already_connected", "此外部账号已被连接，请先断开原连接。", status_code=409)


def account_not_connected():
    return account_error("external_account_not_connected", "尚未连接此外部账号。", status_code=404)


def account_token_invalid():
    return account_error("external_account_token_invalid", "外部账号凭据无效或已失效，请重新授权。")


def account_identity_mismatch():
    return account_error(
        "external_account_identity_mismatch",
        "新凭据属于另一个外部账号，请先断开当前连接。",
        status_code=409,
    )


def authorization_state_invalid():
    return account_error("authorization_state_invalid", "授权状态无效或已使用。", status_code=400)


def authorization_state_expired():
    return account_error("authorization_state_expired", "授权状态已过期，请重新发起连接。", status_code=400)


def authorization_exchange_failed():
    return account_error("authorization_exchange_failed", "外部账号授权交换失败，请重新发起连接。", status_code=502)


def import_preview_expired():
    return account_error("import_preview_expired", "导入预览已过期，请重新读取收藏。", status_code=410)


def import_item_invalid(detail="导入项目无效。"):
    return account_error("import_item_invalid", detail)


def import_conflict(detail="导入项目与本地数据冲突。"):
    return account_error("import_conflict", detail, status_code=409)


def provider_unavailable():
    return account_error("provider_unavailable", "外部账号服务暂时不可用，请稍后重试。", status_code=503)


def provider_invalid_response():
    return account_error("provider_invalid_response", "外部账号服务返回了无法识别的数据。", status_code=502)


def unsupported_account_provider(provider):
    return account_error("unsupported_provider", f"暂不支持外部账号提供方：{provider or 'unknown'}。")
