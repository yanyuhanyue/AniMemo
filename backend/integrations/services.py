import json
import time

from config.api_errors import public_failure
from django.conf import settings
from django.db import IntegrityError, close_old_connections, transaction
from django.utils import timezone
from plugin_host.models import (
    PluginDeployment,
    PluginProject,
    PluginVersion,
    UserPluginInstallation,
)
from plugin_host.runtime import RuntimeLoadError, RuntimeUnavailable, runtime_registry
from rest_framework.response import Response

from .models import (
    ExternalIdentityBinding,
    IntegrationActionReceipt,
    IntegrationEvent,
)
from .plugin_sdk import (
    IntegrationActionContext,
    IntegrationConnectionMetadata,
    validate_integration_name,
)


class IntegrationDispatchError(ValueError):
    def __init__(self, code, detail, status_code):
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.status_code = status_code


def _json_size(payload):
    try:
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise IntegrationDispatchError("invalid_payload", "payload 必须可序列化为 JSON。", 400) from error
    return len(encoded)


def _safe_deployment(slug):
    return PluginDeployment.objects.select_related("plugin", "current_version").filter(
        plugin__slug=slug,
        plugin__status=PluginProject.Status.ACTIVE,
        enabled=True,
        healthy=True,
        status__in=(PluginDeployment.Status.DEPLOYED, PluginDeployment.Status.ENABLED),
        current_version__review_status=PluginVersion.ReviewStatus.APPROVED,
        current_version__published_at__isnull=False,
        current_version__revoked_at__isnull=True,
    ).first()


def _require_user_installation(plugin, user):
    if plugin.installation_mode != PluginProject.InstallationMode.USER:
        return
    if not UserPluginInstallation.objects.filter(plugin=plugin, user=user, enabled=True).exists():
        raise IntegrationDispatchError(
            "plugin_not_installed",
            "当前用户未安装或未启用该插件。",
            403,
        )


def _normalize_handler_result(result):
    if isinstance(result, Response):
        payload, status_code = result.data, result.status_code
    elif isinstance(result, tuple) and len(result) == 2:
        payload, status_code = result
    else:
        payload, status_code = result, 200
    try:
        status_code = int(status_code)
    except (TypeError, ValueError) as error:
        raise IntegrationDispatchError("invalid_action_response", "插件动作返回了无效状态码。", 500) from error
    if not 100 <= status_code <= 599:
        raise IntegrationDispatchError("invalid_action_response", "插件动作返回了无效状态码。", 500)
    if payload is None:
        payload = {}
    if _json_size(payload) > int(
        getattr(settings, "INTEGRATION_ACTION_RESPONSE_MAX_BYTES", 262144)
    ):
        raise IntegrationDispatchError(
            "action_response_too_large",
            "插件动作响应超过允许大小。",
            502,
        )
    return payload, status_code


def _claim_receipt(connection, request_id, action):
    try:
        with transaction.atomic():
            return IntegrationActionReceipt.objects.create(
                connection=connection,
                request_id=request_id,
                action=action,
            ), True
    except IntegrityError:
        receipt = IntegrationActionReceipt.objects.get(connection=connection, request_id=request_id)
        if receipt.action != action:
            raise IntegrationDispatchError(
                "request_id_conflict",
                "request_id 已用于其他动作。",
                409,
            )
        return receipt, False


def _wait_for_receipt(receipt):
    deadline = time.monotonic() + float(getattr(settings, "INTEGRATION_ACTION_RECEIPT_WAIT_SECONDS", 5))
    while receipt.completed_at is None and time.monotonic() < deadline:
        time.sleep(0.05)
        close_old_connections()
        receipt.refresh_from_db()
    return receipt


def _stored_receipt_result(request, receipt):
    if receipt.completed_at is None:
        raise IntegrationDispatchError(
            "request_in_progress",
            "同一 request_id 的动作仍在处理中。",
            409,
        )
    successful_status = isinstance(receipt.response_status, int) and 200 <= receipt.response_status <= 299
    if receipt.status == IntegrationActionReceipt.Status.FAILED or not successful_status:
        safe_payload, safe_status = _handler_failure_payload(
            request,
            receipt.response_payload,
            receipt.response_status,
        )
        return (
            safe_payload,
            safe_status,
            True,
        )
    return receipt.response_payload, receipt.response_status, True


def _complete_receipt(receipt, payload, status_code, *, failed=False):
    receipt.status = (
        IntegrationActionReceipt.Status.FAILED
        if failed
        else IntegrationActionReceipt.Status.COMPLETED
    )
    receipt.response_payload = payload
    receipt.response_status = status_code
    receipt.completed_at = timezone.now()
    receipt.save(
        update_fields=("status", "response_payload", "response_status", "completed_at")
    )


def _dispatch_failure_payload(request, error):
    if error.code == "invalid_payload":
        status_code = 400
        candidate_code = "invalid_payload"
    elif error.code == "invalid_action_response":
        status_code = 500
        candidate_code = "invalid_action_response"
    elif error.code == "action_response_too_large":
        status_code = 502
        candidate_code = "integration_action_failed"
    elif error.code == "request_id_conflict":
        status_code = 409
        candidate_code = "request_id_conflict"
    elif error.code == "request_in_progress":
        status_code = 409
        candidate_code = "action_in_progress"
    elif error.code == "identity_not_bound":
        status_code = 403
        candidate_code = "identity_not_bound"
    elif error.code == "plugin_not_installed":
        status_code = 403
        candidate_code = "permission_denied"
    elif error.code == "invalid_action":
        status_code = 400
        candidate_code = "invalid_action"
    elif error.code == "plugin_unavailable":
        status_code = 503
        candidate_code = "plugin_unavailable"
    elif error.code == "action_not_declared":
        status_code = 404
        candidate_code = "action_not_declared"
    elif error.code == "action_not_registered":
        status_code = 503
        candidate_code = "action_not_registered"
    else:
        status_code = 500
        candidate_code = "integration_action_failed"
    return public_failure(
        request=request,
        candidate_code=candidate_code,
        status_code=status_code,
    ), status_code


def _handler_failure_payload(request, payload, status_code):
    code = payload.get("code") if isinstance(payload, dict) else None
    if code == "invalid_import_batch" and status_code == 400:
        candidate_code = "invalid_import_batch"
        safe_status = 400
    elif code == "batch_too_large" and status_code == 413:
        candidate_code = "batch_too_large"
        safe_status = 413
    elif code == "invalid_excluded_group_indices" and status_code == 400:
        candidate_code = "invalid_excluded_group_indices"
        safe_status = 400
    elif code == "empty_import_selection" and status_code == 400:
        candidate_code = "empty_import_selection"
        safe_status = 400
    elif code == "resolution_pending" and status_code == 409:
        candidate_code = "resolution_pending"
        safe_status = 409
    elif code == "manual_review_required" and status_code == 409:
        candidate_code = "manual_review_required"
        safe_status = 409
    elif code == "batch_missing" and status_code in {400, 404}:
        candidate_code = "batch_missing"
        safe_status = status_code
    elif code == "batch_owner_mismatch" and status_code == 404:
        candidate_code = "batch_owner_mismatch"
        safe_status = 404
    elif code == "invalid_watch_history" and status_code == 400:
        candidate_code = "invalid_watch_history"
        safe_status = 400
    elif code == "invalid_watched_on" and status_code == 400:
        candidate_code = "invalid_watched_on"
        safe_status = 400
    elif code == "invalid_brush_number" and status_code == 400:
        candidate_code = "invalid_brush_number"
        safe_status = 400
    elif code == "invalid_episode_start" and status_code == 400:
        candidate_code = "invalid_episode_start"
        safe_status = 400
    elif code == "invalid_episode_end" and status_code == 400:
        candidate_code = "invalid_episode_end"
        safe_status = 400
    elif code == "invalid_episode_range" and status_code == 400:
        candidate_code = "invalid_episode_range"
        safe_status = 400
    elif code == "invalid_notes" and status_code == 400:
        candidate_code = "invalid_notes"
        safe_status = 400
    elif code == "duplicate_watch_history" and status_code == 409:
        candidate_code = "duplicate_watch_history"
        safe_status = 409
    elif code == "invalid_entry" and status_code == 400:
        candidate_code = "invalid_entry"
        safe_status = 400
    elif code == "invalid_limit" and status_code == 400:
        candidate_code = "invalid_limit"
        safe_status = 400
    elif code == "invalid_entry_id" and status_code == 400:
        candidate_code = "invalid_entry_id"
        safe_status = 400
    elif code == "invalid_text" and status_code == 400:
        candidate_code = "invalid_text"
        safe_status = 400
    elif code == "entry_not_found" and status_code == 404:
        candidate_code = "entry_not_found"
        safe_status = 404
    elif code == "owner_required" and status_code == 403:
        candidate_code = "owner_required"
        safe_status = 403
    elif code == "plugin_context_forbidden" and status_code == 403:
        candidate_code = "plugin_context_forbidden"
        safe_status = 403
    elif code == "plugin_disabled" and status_code == 403:
        candidate_code = "plugin_disabled"
        safe_status = 403
    elif code == "capability_not_declared" and status_code == 403:
        candidate_code = "capability_not_declared"
        safe_status = 403
    elif code == "plugin_operation_failed" and status_code == 400:
        candidate_code = "plugin_operation_failed"
        safe_status = 400
    else:
        candidate_code = "integration_action_failed"
        safe_status = status_code if status_code in {400, 409, 500, 502, 503} else 500
    return public_failure(
        request=request,
        candidate_code=candidate_code,
        status_code=safe_status,
    ), safe_status


def dispatch_integration_action(request, connection, envelope):
    platform = envelope["platform"]
    external_user_id = envelope["external_user_id"]
    public_action = envelope["action"]
    request_id = envelope["request_id"]
    payload = envelope["payload"]

    binding = ExternalIdentityBinding.objects.select_related("user").filter(
        connection=connection,
        platform=platform,
        external_user_id=external_user_id,
        enabled=True,
        user__is_active=True,
    ).first()
    if binding is None:
        raise IntegrationDispatchError("identity_not_bound", "外部身份未绑定或已停用。", 403)

    slug, separator, local_action = public_action.partition(".")
    if not separator:
        raise IntegrationDispatchError("invalid_action", "动作名称必须包含插件命名空间。", 400)
    try:
        validate_integration_name(local_action)
    except ValueError as error:
        raise IntegrationDispatchError("invalid_action", "动作名称无效。", 400) from error

    deployment = _safe_deployment(slug)
    if deployment is None:
        raise IntegrationDispatchError("plugin_unavailable", "插件未部署、已停用或当前版本不可用。", 503)
    try:
        candidate = runtime_registry.ensure_current(slug)
    except (RuntimeLoadError, RuntimeUnavailable) as error:
        raise IntegrationDispatchError("plugin_unavailable", "插件 Runtime 不可用。", 503) from error

    declared = {
        item.get("name")
        for item in ((candidate.manifest.get("integrations") or {}).get("actions") or [])
        if isinstance(item, dict)
    }
    if local_action not in declared:
        raise IntegrationDispatchError("action_not_declared", "插件未声明该集成动作。", 404)
    handler = candidate.context.integrations.resolve_action(local_action)
    if handler is None:
        raise IntegrationDispatchError("action_not_registered", "当前 Runtime 未注册该集成动作。", 503)
    _require_user_installation(deployment.plugin, binding.user)

    receipt, created = _claim_receipt(connection, request_id, public_action)
    if not created:
        return _stored_receipt_result(request, _wait_for_receipt(receipt))

    context = IntegrationActionContext(
        user=binding.user,
        connection=IntegrationConnectionMetadata(
            id=str(connection.pk),
            provider=connection.provider,
            instance_id=connection.instance_id,
            name=connection.name,
        ),
        platform=platform,
        external_user_id=external_user_id,
        request_id=request_id,
    )
    try:
        response_payload, response_status = _normalize_handler_result(handler(context, payload))
    except IntegrationDispatchError as error:
        response_payload, response_status = _dispatch_failure_payload(request, error)
        _complete_receipt(receipt, response_payload, response_status, failed=True)
        return response_payload, response_status, False
    except Exception:
        response_payload = public_failure(
            request=request,
            candidate_code="action_failed",
            status_code=500,
        )
        response_status = 500
        _complete_receipt(receipt, response_payload, response_status, failed=True)
        return response_payload, response_status, False
    if not 200 <= response_status <= 299:
        response_payload, response_status = _handler_failure_payload(
            request,
            response_payload,
            response_status,
        )
        _complete_receipt(receipt, response_payload, response_status, failed=True)
        return response_payload, response_status, False
    _complete_receipt(receipt, response_payload, response_status)
    return response_payload, response_status, False


def emit_integration_event(*, plugin_slug, runtime_id, user, event_name, payload):
    if not user or not getattr(user, "is_authenticated", False) or not getattr(user, "is_active", False):
        raise PermissionError("集成事件必须绑定有效 AniMemo 用户。")
    if not isinstance(payload, dict):
        raise IntegrationDispatchError("invalid_payload", "事件 payload 必须是 JSON 对象。", 400)
    if _json_size(payload) > int(getattr(settings, "INTEGRATION_EVENT_PAYLOAD_MAX_BYTES", 65536)):
        raise IntegrationDispatchError("payload_too_large", "事件 payload 过大。", 413)

    deployment = _safe_deployment(plugin_slug)
    if deployment is None:
        raise IntegrationDispatchError("plugin_unavailable", "插件未部署、已停用或当前版本不可用。", 503)
    try:
        candidate = runtime_registry.ensure_current(plugin_slug)
    except (RuntimeLoadError, RuntimeUnavailable) as error:
        raise IntegrationDispatchError("plugin_unavailable", "插件 Runtime 不可用。", 503) from error
    if candidate.context.runtime_id != runtime_id:
        raise IntegrationDispatchError("stale_runtime", "旧 Runtime 不得发送集成事件。", 409)
    declared = {
        item.get("name")
        for item in ((candidate.manifest.get("integrations") or {}).get("events") or [])
        if isinstance(item, dict)
    }
    if event_name not in declared:
        raise IntegrationDispatchError("event_not_declared", "插件未声明该集成事件。", 400)
    _require_user_installation(deployment.plugin, user)

    bindings = list(
        ExternalIdentityBinding.objects.select_related("connection").filter(
            user=user,
            enabled=True,
            connection__enabled=True,
        )
    )
    events = IntegrationEvent.objects.bulk_create(
        [
            IntegrationEvent(
                connection=binding.connection,
                user=user,
                platform=binding.platform,
                external_user_id=binding.external_user_id,
                plugin_slug=plugin_slug,
                event_name=event_name,
                payload=payload,
                route_type=IntegrationEvent.RouteType.PRIVATE,
            )
            for binding in bindings
        ]
    )
    return {"count": len(events), "event_ids": [event.pk for event in events]}
