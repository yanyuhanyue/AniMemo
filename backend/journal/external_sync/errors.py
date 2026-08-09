from rest_framework.exceptions import APIException


class ExternalSyncError(APIException):
    def __init__(self, code, detail, *, status_code):
        self.status_code = status_code
        payload = {"code": code, "detail": detail}
        super().__init__(payload, code=code)
        self.detail = payload


def sync_target_not_found():
    return ExternalSyncError("sync_target_not_found", "未找到可同步的作品绑定。", status_code=404)


def sync_context_changed():
    return ExternalSyncError("sync_context_changed", "同步上下文已变化，请重新预览。", status_code=409)


def external_account_needs_reauthorization():
    return ExternalSyncError(
        "external_account_needs_reauthorization",
        "外部账号需要重新授权后才能读取收藏。",
        status_code=409,
    )


def sync_value_unsupported(detail="当前字段值无法安全参与同步。"):
    return ExternalSyncError("sync_value_unsupported", detail, status_code=422)
