from __future__ import annotations

import re

from config.api_errors import public_failure
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_protect
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from .staff_services import StaffCapabilityPermission, record_audit
from .update_agent_client import AgentResponseError, AgentUnavailable, UpdateAgentClient
from .update_public_results import UpdateSuccessResultError, project_update_success
from .web_auth_adapter import no_store

VERSION = re.compile(
    r"^v(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)(?:-(?:beta|rc)\.[1-9][0-9]*)?$"
)
IDENTIFIER = re.compile(r"^[0-9a-f]{32}$")


def _client():
    return UpdateAgentClient()


def _error_response(request, error):
    if isinstance(error, AgentUnavailable):
        return no_store(Response(
            public_failure(request=request, candidate_code="updater_unavailable", status_code=status.HTTP_503_SERVICE_UNAVAILABLE),
            status=status.HTTP_503_SERVICE_UNAVAILABLE,
        ))
    if error.remote_code == "incompatible_release":
        code = "incompatible_release"
    elif error.remote_code == "update_in_progress":
        code = "update_in_progress"
    elif error.remote_code == "invalid_operation_state":
        code = "invalid_operation_state"
    elif error.remote_code == "manual_recovery_required":
        code = "manual_recovery_required"
    else:
        return no_store(Response(
            public_failure(request=request, candidate_code="updater_request_failed", status_code=status.HTTP_400_BAD_REQUEST),
            status=status.HTTP_400_BAD_REQUEST,
        ))
    return no_store(Response(
        public_failure(request=request, candidate_code=code, status_code=status.HTTP_409_CONFLICT),
        status=status.HTTP_409_CONFLICT,
    ))


def _agent(request, operation, params=None):
    request_params = {} if params is None else params
    try:
        result = _client().request(operation, request_params)
        return None, project_update_success(operation, result, request_params)
    except UpdateSuccessResultError:
        return _error_response(
            request,
            AgentUnavailable("AniMemo Update Agent returned an invalid response"),
        ), None
    except (AgentUnavailable, AgentResponseError) as error:
        return _error_response(request, error), None


class StaffUpdateBaseView(APIView):
    permission_classes = (StaffCapabilityPermission,)
    required_capability = "manage_system"
    throttle_scope = "staff_update_check"


class StaffUpdateStatusView(StaffUpdateBaseView):
    def get(self, request):
        error, result = _agent(request, "get_status")
        return error or no_store(Response(result))

class StaffUpdateReleasesView(StaffUpdateBaseView):
    def get(self, request):
        channel = str(request.query_params.get("channel") or "stable").strip().lower()
        if channel not in {"stable", "rc", "beta"}:
            return no_store(Response({"code": "invalid_channel", "detail": "请选择有效的发布通道。"}, status=400))
        if channel != "stable" and not request.user.is_superuser:
            return no_store(Response({"code": "permission_denied", "detail": "只有超级管理员可以查看预发布版本。"}, status=403))
        refresh = str(request.query_params.get("refresh") or "").lower() in {"1", "true", "yes"}
        error, result = _agent(request, "list_releases", {"channel": channel, "refresh": refresh})
        return error or no_store(Response(result))


@method_decorator(csrf_protect, name="dispatch")
class StaffUpdatePlanView(StaffUpdateBaseView):
    throttle_scope = "staff_update_mutation"

    def post(self, request):
        version = str(request.data.get("version") or "").strip()
        if not VERSION.fullmatch(version):
            return no_store(Response({"code": "invalid_version", "detail": "请选择列表中的不可变发布版本。"}, status=400))
        if ("-rc." in version or "-beta." in version) and not request.user.is_superuser:
            return no_store(Response({"code": "permission_denied", "detail": "只有超级管理员可以计划预发布版本。"}, status=403))
        error, result = _agent(request, "plan_update", {"version": version})
        if error:
            return error
        record_audit(request, action="system.update_plan", target_type="release", target_id=version, target_label=version, after=result)
        return no_store(Response(result))


@method_decorator(csrf_protect, name="dispatch")
class StaffUpdateApplyView(StaffUpdateBaseView):
    throttle_scope = "staff_update_mutation"

    def post(self, request):
        plan_id = str(request.data.get("plan_id") or "").strip()
        confirmation = str(request.data.get("confirmation") or "").strip()
        version = confirmation.removeprefix("APPLY ") if confirmation.startswith("APPLY ") else ""
        if not IDENTIFIER.fullmatch(plan_id) or not VERSION.fullmatch(version):
            return no_store(Response({"code": "invalid_confirmation", "detail": "更新确认信息无效，请重新生成计划。"}, status=400))
        error, result = _agent(request, "apply_update", {"planId": plan_id, "confirmation": confirmation})
        if error:
            return error
        record_audit(
            request,
            action="system.update_apply",
            target_type="release",
            target_id=version,
            target_label=version,
            metadata={"operation_id": result.get("operation", {}).get("id", "")},
        )
        return no_store(Response(result, status=status.HTTP_202_ACCEPTED))


@method_decorator(csrf_protect, name="dispatch")
class StaffUpdateRollbackView(StaffUpdateBaseView):
    throttle_scope = "staff_update_mutation"

    def post(self, request):
        confirmation = str(request.data.get("confirmation") or "").strip()
        if confirmation != "ROLLBACK PREVIOUS":
            return no_store(Response({"code": "invalid_confirmation", "detail": "请输入完整回退确认文字。"}, status=400))
        error, result = _agent(request, "rollback_previous", {"confirmation": confirmation})
        if error:
            return error
        record_audit(
            request,
            action="system.update_rollback",
            target_type="release",
            target_label="PREVIOUS",
            metadata={"operation_id": result.get("operation", {}).get("id", "")},
        )
        return no_store(Response(result, status=status.HTTP_202_ACCEPTED))


class StaffUpdateOperationView(StaffUpdateBaseView):
    def get(self, request, operation_id):
        if not IDENTIFIER.fullmatch(operation_id):
            return no_store(Response({"code": "invalid_operation", "detail": "更新操作编号无效。"}, status=400))
        error, result = _agent(request, "get_operation", {"operationId": operation_id})
        return error or no_store(Response(result))


class StaffUpdateLogsView(StaffUpdateBaseView):
    def get(self, request, operation_id):
        if not IDENTIFIER.fullmatch(operation_id):
            return no_store(Response({"code": "invalid_operation", "detail": "更新操作编号无效。"}, status=400))
        try:
            limit = min(max(int(request.query_params.get("limit") or 100), 1), 1000)
        except (TypeError, ValueError):
            limit = 100
        error, result = _agent(request, "get_logs", {"operationId": operation_id, "limit": limit})
        return error or no_store(Response(result))
