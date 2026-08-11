from drf_spectacular.extensions import OpenApiAuthenticationExtension, OpenApiViewExtension
from drf_spectacular.types import OpenApiTypes
import re


class SessionVersionJWTAuthenticationScheme(OpenApiAuthenticationExtension):
    """Describe AniMemo's in-process JWT authenticator as bearer auth."""

    target_class = "journal.authentication.SessionVersionJWTAuthentication"
    name = "bearerAuth"
    priority = 1

    def get_security_definition(self, auto_schema):
        return {
            "type": "http",
            "scheme": "bearer",
            "bearerFormat": "JWT",
            "description": "使用 access token：Authorization: Bearer <token>",
        }


class IntegrationHMACAuthenticationScheme(OpenApiAuthenticationExtension):
    """Document the stable signature header without exposing any secret."""

    target_class = "integrations.authentication.IntegrationHMACAuthentication"
    name = "integrationHmac"
    priority = 1

    def get_security_definition(self, auto_schema):
        return {
            "type": "apiKey",
            "in": "header",
            "name": "X-AniMemo-Key-Id",
            "description": (
                "HMAC integration requests also require X-AniMemo-Timestamp, X-AniMemo-Nonce and "
                "X-AniMemo-Signature; "
                "密钥值不会出现在 schema。"
            ),
        }


def select_stable_contract_routes(endpoints):
    """Publish canonical Core v1 and the independently frozen Integration v1."""

    dynamic_paths = {
        "/api/v1/plugins/{slug}/",
        "/api/v1/plugins/{slug}/{plugin_path}",
        "/api/plugins/{slug}/",
        "/api/plugins/{slug}/{plugin_path}",
        "/plugin-assets/session/{asset_session}/{slug}/{version}/{asset}",
        "/plugin-assets/{slug}/{version}/{asset}",
        "/plugin-previews/session/{preview_session}/{slug}/{version}/{asset}",
    }
    return [
        endpoint
        for endpoint in endpoints
        if endpoint[0] not in dynamic_paths
        and (
            endpoint[0].startswith("/api/v1/")
            or endpoint[0].startswith("/api/integrations/v1/")
        )
    ]


class BusinessAPIViewSchemaExtension(OpenApiViewExtension):
    """Give hand-written business APIViews an explicit generic JSON contract.

    Core views add precise ``@extend_schema`` metadata where the response shape is
    important. The fallback keeps less critical staff/platform endpoints visible
    without allowing serializer inference failures to silently drop them.
    """

    target_class = "rest_framework.views.APIView"
    match_subclasses = True
    priority = -1

    def view_replacement(self):
        target = self.target
        module = getattr(target, "__module__", "")
        if not module.startswith(("journal.", "integrations.", "plugin_host.")):
            return target
        if any(
            callable(getattr(target, name, None)) or hasattr(target, "serializer_class")
            for name in ("get_serializer", "get_serializer_class")
        ):
            return target
        return type(
            f"{target.__name__}OpenApiSchema",
            (target,),
            {"serializer_class": OpenApiTypes.OBJECT},
        )


def stabilize_operation_ids(result, generator, request, public):
    """Use deterministic path-derived operation IDs instead of collision suffixes."""

    used = set()
    for path in sorted(result.get("paths", {})):
        path_parts = []
        for part in path.strip("/").split("/"):
            if not part:
                continue
            parameter = re.fullmatch(r"\{([^}]+)\}", part)
            path_parts.append(f"by_{parameter.group(1)}" if parameter else re.sub(r"[^a-zA-Z0-9]+", "_", part).strip("_"))
        stem = "_".join(item for item in path_parts if item) or "root"
        for method, operation in result["paths"][path].items():
            if method.lower() not in {"get", "post", "put", "patch", "delete", "head", "options"}:
                continue
            original_operation_id = operation.get("operationId", "")
            get_suffix = "list" if original_operation_id.endswith("_list") else "retrieve"
            suffix = {"get": get_suffix, "post": "create", "put": "update", "patch": "partial_update", "delete": "destroy"}.get(method.lower(), method.lower())
            candidate = f"{stem}_{suffix}"
            if candidate in used:
                candidate = f"{stem}_{method.lower()}"
            used.add(candidate)
            operation["operationId"] = candidate
    return result


def attach_error_contract(result, generator, request, public):
    """Attach the canonical machine-readable error shape to every operation."""

    def error_response(description):
        return {
            "description": description,
            "content": {
                "application/json": {
                    "schema": {"$ref": "#/components/schemas/ApiError"},
                },
            },
        }

    for path, path_item in result.get("paths", {}).items():
        for method, operation in path_item.items():
            if method.lower() not in {"get", "post", "put", "patch", "delete"}:
                continue
            responses = operation.setdefault("responses", {})
            responses.setdefault("429", error_response("请求频率超过限制。"))
            responses.setdefault("default", error_response("符合 AniMemo API Error Contract 的错误响应。"))
            if method.lower() in {"post", "put", "patch", "delete"}:
                responses.setdefault("400", error_response("请求参数或资源状态无效。"))
            if "{" in path:
                responses.setdefault("404", error_response("资源不存在或对当前调用者不可见。"))
            security = operation.get("security")
            if security and {} not in security:
                responses.setdefault("401", error_response("认证凭据缺失、无效或已过期。"))
                responses.setdefault("403", error_response("调用者无权执行该操作。"))
    return result
