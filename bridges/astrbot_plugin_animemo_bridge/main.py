from __future__ import annotations

import inspect
import os
import secrets
from pathlib import Path

try:
    from .animemo_bridge.client import AsyncAniMemoClient, BridgeConfig
    from .animemo_bridge.errors import AniMemoBridgeError
    from .animemo_bridge.events import EventPoller
    from .animemo_bridge.identity import extract_identity
    from .animemo_bridge.routing import RouteStore
    from .animemo_bridge.state import EventState
except ImportError:  # direct AstrBot loader / static tests
    from animemo_bridge.client import AsyncAniMemoClient, BridgeConfig
    from animemo_bridge.errors import AniMemoBridgeError
    from animemo_bridge.events import EventPoller
    from animemo_bridge.identity import extract_identity
    from animemo_bridge.routing import RouteStore
    from animemo_bridge.state import EventState

try:  # AstrBot is intentionally optional for repository-side unit tests.
    from astrbot.api import logger
    from astrbot.api.event import AstrMessageEvent, filter
    from astrbot.api.star import Context, Star, register
except ImportError:  # pragma: no cover - exercised by packaging/static tests
    class _FallbackLogger:
        def __getattr__(self, _name):
            return lambda *args, **kwargs: None

    logger = _FallbackLogger()

    class Star:
        def __init__(self, context=None, config=None):
            self.context, self.config = context, config or {}

    class Context:
        pass

    class AstrMessageEvent:
        pass

    class _Group:
        def command(self, *_args, **_kwargs):
            return lambda fn: fn

    class _Filter:
        def command(self, *_args, **_kwargs):
            return lambda fn: fn

        def command_group(self, *_args, **_kwargs):
            return _Group()

    filter = _Filter()

    def register(*_args, **_kwargs):
        return lambda cls: cls


DEFAULT_CONFIG = {
    "enabled": True,
    "animemo_base_url": "https://re-anime.cc",
    "key_id": "",
    "secret": "",
    "poll_events": True,
    "poll_wait_seconds": 20,
    "request_timeout_seconds": 35,
    "allow_group_commands": False,
    "developer_commands": False,
    "verify_tls": True,
}


def _message_text(event):
    for name in ("message_str", "get_message_str", "message_text"):
        value = getattr(event, name, None)
        if callable(value):
            value = value()
        if value:
            return str(value).strip()
    return ""


async def _reply(event, text):
    method = getattr(event, "send", None) or getattr(event, "reply", None)
    if callable(method):
        message = str(text)
        try:
            from astrbot.api.event import MessageChain

            message = MessageChain().message(message)
        except (ImportError, AttributeError, TypeError):
            message = str(text)
        result = method(message)
        if inspect.isawaitable(result):
            await result
    return str(text)


def _data_dir(context):
    for name in ("get_plugin_data_dir", "get_data_dir"):
        method = getattr(context, name, None)
        if callable(method):
            try:
                value = method("astrbot_plugin_animemo_bridge")
            except TypeError:
                value = method()
            if value:
                return Path(value)
    for name in ("plugin_data_dir", "data_dir"):
        value = getattr(context, name, None)
        if value:
            return Path(value) / "astrbot_plugin_animemo_bridge"
    return Path("data/plugins/astrbot_plugin_animemo_bridge")


def _config_value(config, key):
    value = config.get(key, DEFAULT_CONFIG[key]) if isinstance(config, dict) else DEFAULT_CONFIG[key]
    if key == "animemo_base_url":
        value = os.getenv("ANIMEMO_BASE_URL", value)
    elif key == "key_id":
        value = os.getenv("ANIMEMO_INTEGRATION_KEY_ID", value)
    elif key == "secret":
        value = os.getenv("ANIMEMO_INTEGRATION_SECRET", value)
    return value


@register("astrbot_plugin_animemo_bridge", "AniMemo", "AniMemo Integration Protocol v1 Bridge", "0.1.0")
class AniMemoBridge(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context, config or {})
        self.context = context
        self.config = {**DEFAULT_CONFIG, **(config or {})}
        self.client = None
        self.routes = RouteStore(_data_dir(context) / "routes.json")
        self.state = EventState(_data_dir(context) / "state.json")
        self.poller = None
        self.last_ping = "NOT RUN"
        self.configuration_error = ""

    async def initialize(self):
        if not bool(_config_value(self.config, "enabled")):
            return
        try:
            bridge_config = BridgeConfig.from_values(
                _config_value(self.config, "animemo_base_url"),
                _config_value(self.config, "key_id"),
                _config_value(self.config, "secret"),
                timeout_seconds=max(float(_config_value(self.config, "request_timeout_seconds")), float(_config_value(self.config, "poll_wait_seconds")) + 5),
                verify_tls=bool(_config_value(self.config, "verify_tls")),
            )
        except ValueError:
            self.configuration_error = "凭证配置不完整"
            logger.warning("AniMemo Bridge disabled until credentials are configured")
            return
        self.client = AsyncAniMemoClient(bridge_config)
        register_web_api = getattr(self.context, "register_web_api", None)
        if callable(register_web_api):
            register_web_api(
                "/astrbot_plugin_animemo_bridge/status",
                self._web_status,
                ["GET"],
                "AniMemo Bridge sanitized diagnostics",
            )
        if bool(_config_value(self.config, "poll_events")):
            self.poller = EventPoller(
                client=self.client,
                context=self.context,
                routes=self.routes,
                state=self.state,
                logger=logger,
                wait_seconds=int(_config_value(self.config, "poll_wait_seconds")),
                developer=bool(_config_value(self.config, "developer_commands")),
            )
            self.poller.start()

    async def terminate(self):
        if self.poller:
            await self.poller.stop()
            self.poller = None
        if self.client:
            await self.client.aclose()
            self.client = None
        self.routes.save()
        self.state.save()

    async def _web_status(self, _request=None):
        return {
            "enabled": bool(self.client),
            "poller": self.poller.status if self.poller else "STOPPED",
            "route_count": self.routes.count(),
            "routes": self.routes.masked_routes(),
            "cursor": self.state.cursor,
            "last_successful_poll": self.state.last_successful_poll,
            "last_error": self.state.last_error,
        }

    def _require_client(self):
        if not self.client:
            raise AniMemoBridgeError("AniMemo Bridge 尚未启用或凭证未配置。")
        return self.client

    async def _command(self, event):
        text = _message_text(event)
        parts = text.split()
        if parts and parts[0].lower() in {"/animemo", "animemo"}:
            parts = parts[1:]
        command = parts[0].lower() if parts else "help"
        identity = extract_identity(event)
        if identity.is_private:
            self.routes.save_private(identity)
        if command in {"help", ""}:
            return await _reply(event, "AniMemo Bridge 命令：pair <code>、status、ping、watch <action>、unpair-help。")
        if command == "unpair-help":
            return await _reply(event, "请在 AniMemo 绑定管理页面移除该外部身份；Bridge 不会在群聊执行解绑。")
        if command == "pair":
            if not identity.is_private:
                return await _reply(event, "请在私聊中完成 AniMemo 绑定。")
            if len(parts) != 2:
                return await _reply(event, "用法：/animemo pair <code>")
            try:
                result = await self._require_client().pair(parts[1], identity.platform, identity.external_user_id, identity.display_name)
            except AniMemoBridgeError as error:
                return await _reply(event, f"配对失败：{type(error).__name__}。")
            return await _reply(event, "AniMemo 绑定成功。" if result else "AniMemo 绑定请求已提交。")
        if not identity.is_private and not bool(_config_value(self.config, "allow_group_commands")):
            return await _reply(event, "群聊业务命令默认关闭，请在私聊中使用。")
        if command == "status":
            return await _reply(event, await self._status_text())
        if command == "ping":
            try:
                await self._require_client().ping()
                self.last_ping = "OK"
            except AniMemoBridgeError:
                self.last_ping = "FAIL"
            return await _reply(event, f"AniMemo HMAC connectivity: {self.last_ping}")
        if command == "watch":
            if len(parts) < 2:
                return await _reply(event, "用法：/animemo watch <action> [参数]")
            action = parts[1]
            payload = {"args": parts[2:]}
            try:
                result = await self._require_client().action(secrets.token_hex(16), identity.platform, identity.external_user_id, f"watch-history-importer.{action}", payload)
            except AniMemoBridgeError as error:
                return await _reply(event, f"动作失败：{type(error).__name__}。")
            return await _reply(event, result.get("detail") or str(result.get("result") or result))
        if command == "debug" and bool(_config_value(self.config, "developer_commands")):
            return await _reply(event, f"event route count={self.routes.count()} cursor={self.state.cursor}")
        return await _reply(event, "未知命令，请使用 /animemo help。")

    async def _status_text(self):
        poll = self.poller.status if self.poller else "STOPPED"
        return "\n".join((
            "AniMemo Bridge: 已启用" if self.client else "AniMemo Bridge: 未启用",
            f"Server: {_config_value(self.config, 'animemo_base_url')}",
            f"Key ID: {str(_config_value(self.config, 'key_id'))[:8]}…{str(_config_value(self.config, 'key_id'))[-4:] if _config_value(self.config, 'key_id') else ''}",
            f"Event poller: {poll}",
            f"Current route: {'已绑定本地投递路由' if self.routes.count() else '无'}",
            f"Last successful poll: {self.state.last_successful_poll or 'NOT RUN'}",
            f"Config: {self.configuration_error}" if self.configuration_error else "",
        ))

    @filter.command("animemo")
    async def animemo(self, event: AstrMessageEvent):
        return await self._command(event)


__all__ = ["AniMemoBridge"]
