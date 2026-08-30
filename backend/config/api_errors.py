from __future__ import annotations

import re
import secrets
from dataclasses import dataclass
from types import MappingProxyType
from typing import TypedDict


class PublicFailure(TypedDict):
    """The complete JSON shape permitted at an external failure seam."""

    code: str
    detail: str
    correlation_id: str


@dataclass(frozen=True, slots=True)
class _FailureSpec:
    statuses: frozenset[int]
    detail: str


_CORRELATION_ATTRIBUTE = "_animemo_public_failure_correlation_id"
_CORRELATION_PATTERN = re.compile(r"^[0-9a-f]{32}$")


def _spec(statuses, detail):
    return _FailureSpec(frozenset(statuses), detail)


_BAD_REQUEST = "请求参数无效。"
_UNAUTHORIZED = "认证凭据无效或已过期。"
_FORBIDDEN = "无权执行该操作。"
_NOT_FOUND = "请求的资源不存在。"
_CONFLICT = "请求与资源当前状态冲突。"
_UNAVAILABLE = "服务暂时不可用，请稍后重试。"
_INTERNAL = "请求无法完成，请使用关联编号联系管理员。"


# This map is the sole authority for public error codes and messages. Callers may
# select a code, but they can never supply public prose. New codes require an
# explicit review here; unknown or status-incompatible codes fail closed below.
_PUBLIC_FAILURE_SPECS = MappingProxyType({
    # HTTP/DRF-wide stable codes.
    "invalid_request": _spec({400, 422}, _BAD_REQUEST),
    "validation_error": _spec({400, 422}, _BAD_REQUEST),
    "authentication_required": _spec({401}, "请先登录后再试。"),
    "invalid_credentials": _spec({401}, "用户名、密码或验证码不正确。"),
    "session_expired": _spec({401}, "登录会话已失效，请重新登录。"),
    # DRF rewrites AuthenticationFailed to 403 when a view deliberately has no
    # authentication challenge (for example the staff-login bootstrap seam).
    "session_revoked": _spec({401, 403}, "登录会话已失效，请重新登录。"),
    "two_factor_required": _spec({401, 403}, "需要完成二次验证。"),
    "csrf_failed": _spec({403}, "安全验证已过期，请刷新页面后重试。"),
    "permission_denied": _spec({403}, _FORBIDDEN),
    "not_found": _spec({404, 410}, _NOT_FOUND),
    "method_not_allowed": _spec({405}, "请求方法不受支持。"),
    "request_timeout": _spec({408, 504}, "请求已超时，请稍后重试。"),
    "conflict": _spec({409}, _CONFLICT),
    "payload_too_large": _spec({413}, "请求内容过大。"),
    "rate_limited": _spec({429}, "操作过于频繁，请稍后重试。"),
    "internal_error": _spec({500}, _INTERNAL),
    "service_unavailable": _spec({424, 502, 503, 504}, _UNAVAILABLE),
    "storage_exhausted": _spec({507}, "存储空间不足。"),

    # Authentication, installation and administrative flows.
    "auth_throttle_unavailable": _spec({503}, _UNAVAILABLE),
    "turnstile_failed": _spec({400, 503}, "安全验证失败，请稍后重试。"),
    "registration_policy_rejected": _spec({403}, "当前无法完成注册。"),
    "registration_policy_unavailable": _spec({503}, "注册服务暂时不可用。"),
    "installation_state_unavailable": _spec({503}, _UNAVAILABLE),
    "installation_uninitialized": _spec({403, 409, 503}, "系统尚未完成初始化。"),
    "installation_initialized": _spec({404}, _NOT_FOUND),
    "installation_initializing": _spec({409}, _CONFLICT),
    "setup_code_unavailable": _spec({409}, _CONFLICT),
    "setup_code_changed": _spec({409}, _CONFLICT),
    "setup_code_expired": _spec({410}, _NOT_FOUND),
    "invalid_setup_code": _spec({400}, _BAD_REQUEST),
    "admin_identity_unavailable": _spec({409}, _CONFLICT),
    "invalid_channel": _spec({400}, _BAD_REQUEST),
    "invalid_version": _spec({400}, _BAD_REQUEST),
    "invalid_confirmation": _spec({400}, _BAD_REQUEST),
    "invalid_operation": _spec({400}, _BAD_REQUEST),
    "staff_user_access_denied": _spec({403}, _FORBIDDEN),
    "staff_permission_change_denied": _spec({403}, _FORBIDDEN),
    "staff_two_factor_required": _spec({403}, "需要完成管理员二次验证。"),
    "staff_reauthentication_failed": _spec({403}, "管理员身份验证失败。"),
    "last_superuser_protected": _spec({403}, _FORBIDDEN),
    "invalid_tag_definition": _spec({400}, _BAD_REQUEST),
    "service_check_failed": _spec({503}, _UNAVAILABLE),
    "updater_unavailable": _spec({503}, "系统更新服务暂时不可用，请联系服务器管理员。"),
    "updater_request_failed": _spec({400}, "更新请求未能完成。"),
    "incompatible_release": _spec({409}, _CONFLICT),
    "update_in_progress": _spec({409}, _CONFLICT),
    "invalid_operation_state": _spec({409}, _CONFLICT),
    "manual_recovery_required": _spec({409}, "更新需要管理员手动恢复。"),

    # Import, data bundle, history and entry flows.
    "invalid_import_file": _spec({400}, "导入文件无效。"),
    "import_commit_failed": _spec({400}, "导入未能完成。"),
    "unsupported_import_schema": _spec({400}, "导入文件格式不受支持。"),
    "invalid_data_bundle": _spec({400}, "数据包内容无效。"),
    "bundle_import_requires_empty_journal": _spec({200, 409}, _CONFLICT),
    "invalid_watch_history": _spec({400}, _BAD_REQUEST),
    "invalid_import_batch": _spec({400}, _BAD_REQUEST),
    "batch_too_large": _spec({413}, "导入批次过大。"),
    "invalid_excluded_group_indices": _spec({400}, _BAD_REQUEST),
    "empty_import_selection": _spec({400}, _BAD_REQUEST),
    "resolution_pending": _spec({409}, _CONFLICT),
    "manual_review_required": _spec({409}, _CONFLICT),
    "batch_missing": _spec({400, 404}, "导入批次不可用。"),
    "batch_owner_mismatch": _spec({404}, _NOT_FOUND),
    "invalid_files": _spec({400}, _BAD_REQUEST),
    "files_too_large": _spec({413}, "导入文件过大。"),
    "invalid_selection": _spec({400}, _BAD_REQUEST),
    "invalid_entry_id": _spec({400}, _BAD_REQUEST),
    "invalid_text": _spec({400}, _BAD_REQUEST),
    "invalid_watched_on": _spec({400}, _BAD_REQUEST),
    "invalid_brush_number": _spec({400}, _BAD_REQUEST),
    "invalid_episode_start": _spec({400}, _BAD_REQUEST),
    "invalid_episode_end": _spec({400}, _BAD_REQUEST),
    "invalid_episode_range": _spec({400}, _BAD_REQUEST),
    "invalid_notes": _spec({400}, _BAD_REQUEST),
    "duplicate_watch_history": _spec({409}, _CONFLICT),
    "invalid_entry": _spec({400}, _BAD_REQUEST),
    "invalid_limit": _spec({400}, _BAD_REQUEST),
    "entry_not_found": _spec({404}, _NOT_FOUND),
    "owner_required": _spec({403}, _FORBIDDEN),
    "apply_metadata_required": _spec({400}, _BAD_REQUEST),
    "email_delivery_disabled": _spec({409}, "邮件发送当前不可用。"),
    "email_delivery_not_configured": _spec({400}, "邮件服务尚未配置。"),
    "email_delivery_failed": _spec({502, 503}, "邮件发送失败，请稍后重试。"),

    # External media, account and sync flows.
    "bangumi_lookup_unavailable": _spec({200, 502}, "外部资料服务暂时不可用。"),
    "database_unavailable": _spec({200}, "数据库状态暂时不可用。"),
    "bangumi_unavailable": _spec({200}, "外部资料服务状态暂时不可用。"),
    "plugin_health_unavailable": _spec({200}, "插件服务状态暂时不可用。"),
    "provider_not_found": _spec({404}, _NOT_FOUND),
    "invalid_analytics_range": _spec({400}, _BAD_REQUEST),
    "invalid_external_id": _spec({400}, _BAD_REQUEST),
    "unsupported_provider": _spec({400}, "外部服务提供方不受支持。"),
    "subject_not_found": _spec({404}, _NOT_FOUND),
    "provider_unavailable": _spec({200, 502, 503}, _UNAVAILABLE),
    "provider_timeout": _spec({504}, "外部服务响应超时，请稍后重试。"),
    "provider_invalid_response": _spec({502}, _UNAVAILABLE),
    "identity_already_bound": _spec({409}, _CONFLICT),
    "subject_already_bound": _spec({409}, _CONFLICT),
    "identity_not_found": _spec({404}, _NOT_FOUND),
    "external_identity_changed": _spec({409}, _CONFLICT),
    "external_account_not_configured": _spec({503}, _UNAVAILABLE),
    "external_account_already_connected": _spec({409}, _CONFLICT),
    "external_account_not_connected": _spec({404}, _NOT_FOUND),
    "external_account_token_invalid": _spec({400, 401}, _UNAUTHORIZED),
    "external_account_identity_mismatch": _spec({409}, _CONFLICT),
    "authorization_state_invalid": _spec({400}, _BAD_REQUEST),
    "authorization_state_expired": _spec({400}, _BAD_REQUEST),
    "authorization_exchange_failed": _spec({502}, _UNAVAILABLE),
    "import_preview_expired": _spec({410}, _NOT_FOUND),
    "import_item_invalid": _spec({200, 400}, _BAD_REQUEST),
    "import_conflict": _spec({200, 409}, _CONFLICT),
    "sync_target_not_found": _spec({404}, _NOT_FOUND),
    "sync_context_changed": _spec({409}, _CONFLICT),
    "external_account_needs_reauthorization": _spec({409}, _CONFLICT),
    "sync_value_unsupported": _spec({422}, _BAD_REQUEST),
    "sync_request_invalid": _spec({400}, _BAD_REQUEST),
    "no_sync_action": _spec({400}, _BAD_REQUEST),
    "sync_action_not_allowed": _spec({400}, _BAD_REQUEST),
    "sync_preview_invalid": _spec({400}, _BAD_REQUEST),
    "sync_preview_expired": _spec({400}, _BAD_REQUEST),
    "sync_preview_stale": _spec({409}, _CONFLICT),

    # Integration protocol.
    "pairing_failed": _spec({400}, _BAD_REQUEST),
    "pairing_code_invalid": _spec({400}, "配对码无效。"),
    "integration_action_failed": _spec({400, 409, 500, 502, 503}, "集成动作执行失败。"),
    "request_too_large": _spec({413}, "请求内容过大。"),
    "invalid_content_length": _spec({400}, _BAD_REQUEST),
    "invalid_json": _spec({400}, _BAD_REQUEST),
    "invalid_payload": _spec({400}, _BAD_REQUEST),
    "invalid_query": _spec({400}, _BAD_REQUEST),
    "invalid_connection": _spec({400}, _BAD_REQUEST),
    "connection_not_found": _spec({404}, _NOT_FOUND),
    "binding_not_found": _spec({404}, _NOT_FOUND),
    "identity_not_bound": _spec({403}, _FORBIDDEN),
    "invalid_action": _spec({400}, _BAD_REQUEST),
    "invalid_action_response": _spec({500}, _INTERNAL),
    "plugin_unavailable": _spec({503}, _UNAVAILABLE),
    "action_not_declared": _spec({404}, _NOT_FOUND),
    "action_not_registered": _spec({503}, _UNAVAILABLE),
    "action_failed": _spec({400, 409, 500, 502, 503}, "集成动作执行失败。"),
    "action_response_too_large": _spec({500}, _INTERNAL),
    "request_id_conflict": _spec({409}, _CONFLICT),
    "action_in_progress": _spec({409}, _CONFLICT),
    "stale_runtime": _spec({409}, _CONFLICT),
    "event_not_declared": _spec({400}, _BAD_REQUEST),

    # Plugin host and runtime.
    "plugin_operation_failed": _spec({400}, "插件操作失败。"),
    "plugin_runtime_unavailable": _spec({503}, "插件运行时暂时不可用。"),
    "plugin_scan_failed": _spec({400}, "插件扫描失败。"),
    "plugin_publish_failed": _spec({400, 503}, "插件发布失败。"),
    "plugin_deployment_update_failed": _spec({400}, "插件部署更新失败。"),
    "plugin_rollback_failed": _spec({400}, "插件回退失败。"),
    "plugin_cleanup_failed": _spec({400}, "插件清理失败。"),
    "plugin_install_failed": _spec({400}, "插件安装失败。"),
    "plugin_project_create_failed": _spec({400}, "插件项目创建失败。"),
    "plugin_project_update_failed": _spec({400}, "插件项目更新失败。"),
    "plugin_project_archive_failed": _spec({400}, "插件项目归档失败。"),
    "plugin_upload_failed": _spec({400}, "插件上传失败。"),
    "plugin_preview_failed": _spec({400}, "插件预览失败。"),
    "plugin_submission_failed": _spec({400}, "插件提交失败。"),
    "plugin_submission_withdraw_failed": _spec({400}, "插件撤回失败。"),
    "plugin_review_failed": _spec({400}, "插件审核失败。"),
    "plugin_revoke_failed": _spec({400}, "插件撤销失败。"),
    "plugin_manifest_invalid": _spec({400}, "插件清单无效。"),
    "plugin_scan_stylesheet_invalid": _spec({400}, "插件样式表无效。"),
    "plugin_scan_source_invalid": _spec({400}, "插件源文件无效。"),
    "plugin_service_unavailable": _spec({503}, "插件服务暂时不可用。"),
    "capability_not_declared": _spec({403}, _FORBIDDEN),
    "plugin_context_forbidden": _spec({403}, _FORBIDDEN),
    "plugin_disabled": _spec({403}, _FORBIDDEN),

    # Storage codes are legacy public identifiers and remain exact for v1 clients.
    "invalid_poster_url": _spec({400}, _BAD_REQUEST),
    "unsafe_storage_path": _spec({400}, _BAD_REQUEST),
    "storage_root_unavailable": _spec({400}, _BAD_REQUEST),
    "STORAGE_IN_USE": _spec({409}, _CONFLICT),
    "STORAGE_PHYSICAL_IDENTITY_LOCKED": _spec({400, 409}, _CONFLICT),
    "MEDIA_STORAGE_ERROR": _spec({503}, _UNAVAILABLE),
    "MEDIA_STORAGE_SETUP_REQUIRED": _spec({503}, _UNAVAILABLE),
    "MEDIA_STORAGE_EXHAUSTED": _spec({507}, "存储空间不足。"),
    "MEDIA_STORAGE_OFFLINE": _spec({503}, _UNAVAILABLE),
    "UNSAFE_MEDIA_OBJECT_KEY": _spec({400, 503}, _BAD_REQUEST),
    "CLOUDFLARE_ANALYTICS_AUTH_FAILED": _spec({424}, _UNAVAILABLE),
    "CLOUDFLARE_ANALYTICS_TIMEOUT": _spec({424}, _UNAVAILABLE),
    "CLOUDFLARE_ANALYTICS_QUERY_FAILED": _spec({424}, _UNAVAILABLE),
    "CLOUDFLARE_ANALYTICS_INVALID_RESPONSE": _spec({424}, _UNAVAILABLE),
})


_STATUS_FALLBACK_CODES = MappingProxyType({
    400: "invalid_request",
    401: "authentication_required",
    403: "permission_denied",
    404: "not_found",
    405: "method_not_allowed",
    408: "request_timeout",
    409: "conflict",
    410: "not_found",
    413: "payload_too_large",
    422: "invalid_request",
    424: "service_unavailable",
    429: "rate_limited",
    500: "internal_error",
    502: "service_unavailable",
    503: "service_unavailable",
    504: "service_unavailable",
    507: "storage_exhausted",
})


def canonical_error_status(status_code: object) -> int:
    """Return the HTTP status whose generic public failure contract is defined."""

    try:
        normalized = int(status_code)
    except (TypeError, ValueError):
        return 500
    return normalized if normalized in _STATUS_FALLBACK_CODES else 500


_CODE_ALIASES = MappingProxyType({
    "not_authenticated": "authentication_required",
    "authentication_failed": "invalid_credentials",
    "no_active_account": "invalid_credentials",
    "token_not_valid": "session_expired",
    "throttled": "rate_limited",
    "invalid": "validation_error",
    "parse_error": "invalid_request",
    "not_acceptable": "invalid_request",
    "unsupported_media_type": "invalid_request",
})


def _correlation_id(request):
    owner = request
    if request is not None:
        try:
            underlying = getattr(request, "_request", None)
        except Exception:
            underlying = None
        if underlying is not None:
            owner = underlying

    current = getattr(owner, _CORRELATION_ATTRIBUTE, None) if owner is not None else None
    if isinstance(current, str) and _CORRELATION_PATTERN.fullmatch(current):
        return current

    generated = secrets.token_hex(16)
    if owner is not None:
        try:
            setattr(owner, _CORRELATION_ATTRIBUTE, generated)
        except (AttributeError, TypeError):
            pass
    return generated


def correlation_id_for(request: object | None) -> str:
    """Return the server-issued correlation identifier for one request."""

    return _correlation_id(request)


def _normalized_code(candidate_code):
    if not isinstance(candidate_code, str):
        return ""
    return _CODE_ALIASES.get(candidate_code, candidate_code)


def public_failure(
    *,
    request: object | None,
    candidate_code: object | None,
    status_code: int,
) -> PublicFailure:
    """Resolve an untrusted candidate into the strict public failure contract."""

    try:
        normalized_status = int(status_code)
    except (TypeError, ValueError):
        normalized_status = 500

    code = _normalized_code(candidate_code)
    spec = _PUBLIC_FAILURE_SPECS.get(code)
    if spec is None or normalized_status not in spec.statuses:
        code = _STATUS_FALLBACK_CODES.get(normalized_status, "internal_error")
        spec = _PUBLIC_FAILURE_SPECS[code]

    return {
        "code": code,
        "detail": spec.detail,
        "correlation_id": _correlation_id(request),
    }


def canonicalize_payload(
    payload,
    status_code,
    *,
    request=None,
    default_code=None,
    retry_after=None,
) -> PublicFailure:
    """Compatibility adapter for DRF and hand-written Response payloads.

    Only the candidate code crosses into :func:`public_failure`; prose and all
    additional input keys are intentionally discarded.
    """

    del retry_after
    candidate_code = None
    if isinstance(payload, dict):
        candidate_code = payload.get("code")
        if candidate_code is None and status_code == 400 and "detail" not in payload:
            candidate_code = "validation_error"
    elif isinstance(payload, list) and status_code in {400, 422}:
        candidate_code = "validation_error"
    if candidate_code is None:
        candidate_code = default_code
    if candidate_code is None:
        candidate_code = _STATUS_FALLBACK_CODES.get(status_code)
    return public_failure(
        request=request,
        candidate_code=candidate_code,
        status_code=status_code,
    )
