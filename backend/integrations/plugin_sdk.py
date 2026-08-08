import re
from dataclasses import dataclass


INTEGRATION_NAME_RE = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")


def validate_integration_name(name):
    normalized = str(name or "")
    if not INTEGRATION_NAME_RE.fullmatch(normalized):
        raise ValueError("Integration action/event name must use conservative kebab-case.")
    return normalized


@dataclass(frozen=True)
class IntegrationConnectionMetadata:
    id: str
    provider: str
    instance_id: str
    name: str


@dataclass(frozen=True)
class IntegrationActionContext:
    user: object
    connection: IntegrationConnectionMetadata
    platform: str
    external_user_id: str
    request_id: str


class PluginIntegrations:
    def __init__(self, plugin_slug, manifest, runtime_id):
        self.plugin_slug = plugin_slug
        self.manifest = manifest
        self.runtime_id = runtime_id
        declarations = manifest.get("integrations") or {}
        self._declared_actions = {
            item.get("name") for item in declarations.get("actions", []) if isinstance(item, dict)
        }
        self._declared_events = {
            item.get("name") for item in declarations.get("events", []) if isinstance(item, dict)
        }
        self._actions = {}

    def register_action(self, name, handler):
        local_name = validate_integration_name(name)
        if local_name not in self._declared_actions:
            raise ValueError(f"Integration action is not declared in Manifest: {local_name}")
        if not callable(handler):
            raise ValueError("Integration action handler must be callable.")
        if local_name in self._actions:
            raise ValueError(f"Duplicate integration action: {local_name}")
        self._actions[local_name] = handler
        return handler

    def resolve_action(self, name):
        return self._actions.get(validate_integration_name(name))

    def emit(self, user, event_name, payload):
        local_name = validate_integration_name(event_name)
        if local_name not in self._declared_events:
            raise ValueError(f"Integration event is not declared in Manifest: {local_name}")
        from .services import emit_integration_event

        return emit_integration_event(
            plugin_slug=self.plugin_slug,
            runtime_id=self.runtime_id,
            user=user,
            event_name=local_name,
            payload=payload,
        )

    def __bool__(self):
        return bool(self._actions)
