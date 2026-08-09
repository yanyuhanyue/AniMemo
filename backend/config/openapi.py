from drf_spectacular.extensions import OpenApiAuthenticationExtension
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
            "name": "X-Integration-Signature",
            "description": (
                "HMAC integration requests also require the documented timestamp, nonce and key-id headers; "
                "密钥值不会出现在 schema。"
            ),
        }


def exclude_dynamic_plugin_runtime(endpoints):
    """Keep arbitrary plugin runtime paths out of the stable core schema."""

    dynamic_paths = {"/api/plugins/{slug}/", "/api/plugins/{slug}/{plugin_path}"}
    return [endpoint for endpoint in endpoints if endpoint[0] not in dynamic_paths]


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
            suffix = {"get": "retrieve", "post": "create", "put": "update", "patch": "partial_update", "delete": "destroy"}.get(method.lower(), method.lower())
            candidate = f"{stem}_{suffix}"
            if candidate in used:
                candidate = f"{stem}_{method.lower()}"
            used.add(candidate)
            operation["operationId"] = candidate
    return result
