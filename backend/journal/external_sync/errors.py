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


def sync_request_invalid():
    return ExternalSyncError("sync_request_invalid", "同步确认请求格式无效。", status_code=400)


def no_sync_action():
    return ExternalSyncError("no_sync_action", "请至少选择一个可执行的同步操作。", status_code=400)


def sync_action_not_allowed():
    return ExternalSyncError("sync_action_not_allowed", "当前状态不允许所选同步操作。", status_code=400)


def sync_preview_invalid():
    return ExternalSyncError("sync_preview_invalid", "同步预览确认已失效，请重新预览。", status_code=400)


def sync_preview_expired():
    return ExternalSyncError("sync_preview_expired", "同步预览确认已过期，请重新预览。", status_code=400)


def sync_preview_stale():
    return ExternalSyncError("sync_preview_stale", "数据已发生变化，请重新确认。", status_code=409)
