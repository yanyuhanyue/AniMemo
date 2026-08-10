from __future__ import annotations

import inspect
import json
import os
from uuid import uuid4

if __package__:
    from .animemo_bridge.client import AsyncAniMemoClient, BridgeConfig
    from .animemo_bridge.errors import AniMemoBridgeError, PairingResultUnknown
    from .animemo_bridge.events import EventPoller
    from .animemo_bridge.identity import extract_identity
    from .animemo_bridge.routing import RouteStore
    from .animemo_bridge.state import EventState
else:
    from animemo_bridge.client import AsyncAniMemoClient, BridgeConfig
    from animemo_bridge.errors import AniMemoBridgeError, PairingResultUnknown
    from animemo_bridge.events import EventPoller
    from animemo_bridge.identity import extract_identity
    from animemo_bridge.routing import RouteStore
    from animemo_bridge.state import EventState

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.api.web import request as web_request


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
        message = MessageChain().message(str(text))
        result = method(message)
        if inspect.isawaitable(result):
            await result
    return str(text)


def _data_dir():
    return StarTools.get_data_dir("astrbot_plugin_animemo_bridge")


def _config_value(config, key):
    value = config.get(key, DEFAULT_CONFIG[key]) if isinstance(config, dict) else DEFAULT_CONFIG[key]
    if key == "animemo_base_url":
        value = os.getenv("ANIMEMO_BASE_URL", value)
    elif key == "key_id":
        value = os.getenv("ANIMEMO_INTEGRATION_KEY_ID", value)
    elif key == "secret":
        value = os.getenv("ANIMEMO_INTEGRATION_SECRET", value)
    return value


def _config_bool(config, key):
    """Normalize values that may have passed through JSON or environment layers."""
    value = _config_value(config, key)
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value in {0, 1}:
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off", ""}:
            return False
    return bool(DEFAULT_CONFIG[key])


def _config_int(config, key):
    value = _config_value(config, key)
    if isinstance(value, bool):
        raise ValueError(f"{key} 必须是整数。")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    raise ValueError(f"{key} 必须是整数。")


def _validated_timing(config):
    poll_wait = _config_int(config, "poll_wait_seconds")
    request_timeout = _config_int(config, "request_timeout_seconds")
    if not 0 <= poll_wait <= 25:
        raise ValueError("poll_wait_seconds 必须在 0 到 25 之间。")
    if not 5 <= request_timeout <= 120:
        raise ValueError("request_timeout_seconds 必须在 5 到 120 之间。")
    if request_timeout <= poll_wait:
        raise ValueError("request_timeout_seconds 必须大于 poll_wait_seconds。")
    return poll_wait, request_timeout


def _developer_allowed(event):
    is_admin = getattr(event, "is_admin", None)
    return bool(is_admin()) if callable(is_admin) else False


def _watch_action_result(action, result):
    if action in {"find", "search", "entries-search"}:
        entries = result.get("entries", []) if isinstance(result, dict) else []
        if not entries:
            return "没有找到匹配的番剧条目。"
        return "\n".join(
            f"{item.get('entry_id')} · {item.get('title') or item.get('japanese_title') or '未命名'}"
            for item in entries[:20]
            if isinstance(item, dict)
        )
    if action in {"add", "history-add"} and isinstance(result, dict):
        record = result.get("record") if isinstance(result.get("record"), dict) else {}
        episode = record.get("episode_start")
        if episode is not None:
            episode_text = f" · 第{episode}话"
        else:
            episode_text = ""
        return f"观看记录已{'新增' if result.get('created') else '存在'}：{record.get('watched_on') or '日期未填写'}{episode_text}"
    if action in {"get", "history-get"} and isinstance(result, dict):
        records = result.get("records", [])
        title = result.get("title") or f"条目 {result.get('entry_id', 'unknown')}"
        return f"《{title}》共有 {len(records) if isinstance(records, list) else 0} 条观看记录。"
    if isinstance(result, dict):
        detail = result.get("detail")
        if isinstance(detail, str) and detail.strip():
            return detail.strip()[:240]
        return "动作执行完成。"
    return "动作执行完成。"


@register("astrbot_plugin_animemo_bridge", "AniMemo", "AniMemo Integration Protocol v1 Bridge", "0.1.1")
class AniMemoBridge(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context, config or {})
        self.context = context
        self.config = {**DEFAULT_CONFIG, **(config or {})}
        self.client = None
        data_dir = _data_dir()
        self.routes = RouteStore(data_dir / "routes.json")
        self.state = EventState(data_dir / "state.json")
        self.poller = None
        self.last_ping = "NOT RUN"
        self.configuration_error = ""
        self._web_registered = False

    async def initialize(self):
        if self.client or self.poller:
            await self.terminate()
        register_web_api = getattr(self.context, "register_web_api", None)
        if callable(register_web_api) and not self._web_registered:
            register_web_api(
                "/astrbot_plugin_animemo_bridge/status",
                self._web_status,
                ["GET"],
                "AniMemo Bridge sanitized diagnostics",
            )
            register_web_api(
                "/astrbot_plugin_animemo_bridge/ping",
                self._web_ping,
                ["POST"],
                "AniMemo Bridge connectivity check",
            )
            register_web_api(
                "/astrbot_plugin_animemo_bridge/restart",
                self._web_restart,
                ["POST"],
                "Restart AniMemo Bridge event poller",
            )
            register_web_api(
                "/astrbot_plugin_animemo_bridge/routes/clear",
                self._web_clear_route,
                ["POST"],
                "Clear one masked AniMemo Bridge route",
            )
            self._web_registered = True
        if not _config_bool(self.config, "enabled"):
            return
        try:
            poll_wait, request_timeout = _validated_timing(self.config)
            verify_tls = _config_bool(self.config, "verify_tls")
            if not verify_tls:
                logger.warning("AniMemo Bridge TLS verification is disabled; use this only for local development")
            bridge_config = BridgeConfig.from_values(
                _config_value(self.config, "animemo_base_url"),
                _config_value(self.config, "key_id"),
                _config_value(self.config, "secret"),
                timeout_seconds=request_timeout,
                verify_tls=verify_tls,
            )
        except ValueError as error:
            message = str(error)
            self.configuration_error = "凭证配置不完整" if "secret" in message or "key id" in message else "配置无效"
            logger.warning("AniMemo Bridge disabled until configuration is valid: %s", self.configuration_error)
            return
        self.client = AsyncAniMemoClient(bridge_config)
        if _config_bool(self.config, "poll_events"):
            self.poller = EventPoller(
                client=self.client,
                context=self.context,
                routes=self.routes,
                state=self.state,
                logger=logger,
                wait_seconds=poll_wait,
                developer=_config_bool(self.config, "developer_commands"),
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
            "configured": bool(self.client and self.client.configured),
            "server": _config_value(self.config, "animemo_base_url"),
            "key_id": self._masked_key_id(),
            "poller": self.poller.status if self.poller else "STOPPED",
            "route_count": self.routes.count(),
            "routes": self.routes.masked_routes(),
            "cursor": self.state.cursor,
            "delivered_event_count": self.state.delivered_count,
            "last_successful_poll": self.state.last_successful_poll,
            "last_error": self.state.last_error,
            "last_ping": self.last_ping,
            "configuration_error": self.configuration_error,
        }

    async def _web_ping(self, _request=None):
        try:
            await self._require_client().ping()
            self.last_ping = "OK"
        except AniMemoBridgeError:
            self.last_ping = "FAIL"
        return {"status": self.last_ping}

    async def _web_restart(self, _request=None):
        if not self.client or not _config_bool(self.config, "poll_events"):
            return {"status": "STOPPED"}
        if self.poller:
            await self.poller.stop()
        self.poller = EventPoller(
            client=self.client,
            context=self.context,
            routes=self.routes,
            state=self.state,
            logger=logger,
            wait_seconds=_validated_timing(self.config)[0],
            developer=_config_bool(self.config, "developer_commands"),
        )
        self.poller.start()
        return {"status": self.poller.status}

    async def _web_clear_route(self, request=None):
        payload = request if isinstance(request, dict) else {}
        request_source = request if request is not None else web_request
        reader = getattr(request_source, "json", None)
        if not payload and callable(reader):
            try:
                candidate = reader(default={})
            except TypeError:
                candidate = reader()
            payload = await candidate if inspect.isawaitable(candidate) else candidate
        if not isinstance(payload, dict):
            payload = {}
        platform = str(payload.get("platform") or "").strip().lower()
        external_hash = str(payload.get("external_user_hash") or "").strip()
        if not platform or not external_hash:
            return {"status": "invalid_request"}
        if not self.routes.clear_masked(platform, external_hash):
            return {"status": "not_found"}
        self.routes.save()
        return {"status": "cleared"}

    def _masked_key_id(self):
        key_id = str(_config_value(self.config, "key_id") or "")
        if not key_id:
            return "未配置"
        return f"{key_id[:8]}…{key_id[-4:]}"

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
        if identity.is_private and command != "pair":
            self.routes.save_private(identity)
        if command in {"help", ""}:
            return await _reply(event, "AniMemo Bridge 命令：pair <code>、status、ping、watch get/add/find、unpair-help。")
        if command == "unpair-help":
            return await _reply(event, "请在 AniMemo 绑定管理页面移除该外部身份；Bridge 不会在群聊执行解绑。")
        if command == "pair":
            if not identity.is_private:
                return await _reply(event, "请在私聊中完成 AniMemo 绑定。")
            if len(parts) != 2:
                return await _reply(event, "用法：/animemo pair <code>")
            try:
                result = await self._require_client().pair(parts[1], identity.platform, identity.external_user_id, identity.display_name)
            except PairingResultUnknown:
                return await _reply(
                    event,
                    "配对请求结果未知，请在 AniMemo 绑定页面确认；如未成功请生成新的配对码。",
                )
            except AniMemoBridgeError as error:
                return await _reply(event, f"配对失败：{type(error).__name__}。")
            if result:
                self.routes.save_private(identity)
            return await _reply(event, "AniMemo 绑定成功。" if result else "AniMemo 绑定请求已提交。")
        if not identity.is_private and not _config_bool(self.config, "allow_group_commands"):
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
                return await _reply(event, "用法：/animemo watch get <entry_id>、add <entry_id> <日期> [集数]、find <关键词>")
            action = parts[1].lower()
            payload = {}
            if action in {"get", "history-get"}:
                if len(parts) != 3:
                    return await _reply(event, "用法：/animemo watch get <entry_id>")
                try:
                    payload["entry_id"] = int(parts[2])
                except ValueError:
                    return await _reply(event, "entry_id 必须是数字。")
                action_name = "history-get"
            elif action in {"add", "history-add"}:
                if len(parts) not in {4, 5}:
                    return await _reply(event, "用法：/animemo watch add <entry_id> <日期> [集数]")
                try:
                    payload = {"entry_id": int(parts[2]), "watched_on": parts[3]}
                    if len(parts) == 5:
                        payload["episode_start"] = int(parts[4])
                        payload["episode_end"] = payload["episode_start"]
                except ValueError:
                    return await _reply(event, "entry_id 和集数必须是数字。")
                action_name = "history-add"
            elif action in {"find", "search", "entries-search"}:
                if len(parts) < 3:
                    return await _reply(event, "用法：/animemo watch find <关键词>")
                payload = {"query": " ".join(parts[2:])}
                action_name = "entries-search"
            else:
                return await _reply(event, "支持：get、add、find。")
            try:
                result = await self._require_client().action(
                    str(uuid4()),
                    identity.platform,
                    identity.external_user_id,
                    f"watch-history-importer.{action_name}",
                    payload,
                )
            except AniMemoBridgeError as error:
                return await _reply(event, f"动作失败：{type(error).__name__}。")
            return await _reply(event, _watch_action_result(action, result))
        if command == "action":
            if not _config_bool(self.config, "developer_commands") or not _developer_allowed(event):
                return await _reply(event, "developer action 已关闭。")
            if len(parts) < 3:
                return await _reply(event, "用法：/animemo action <plugin.action> <json>")
            try:
                action_name = parts[1]
                payload = json.loads(" ".join(parts[2:]))
                if not isinstance(payload, dict):
                    raise TypeError("action payload must be an object")
            except (TypeError, json.JSONDecodeError):
                return await _reply(event, "action payload 必须是 JSON 对象。")
            try:
                result = await self._require_client().action(
                    str(uuid4()), identity.platform, identity.external_user_id, action_name, payload
                )
            except AniMemoBridgeError as error:
                return await _reply(event, f"动作失败：{type(error).__name__}。")
            return await _reply(event, _watch_action_result("action", result))
        if command == "debug":
            if not _config_bool(self.config, "developer_commands") or not _developer_allowed(event):
                return await _reply(event, "developer debug 已关闭。")
            return await _reply(event, f"event route count={self.routes.count()} cursor={self.state.cursor}")
        return await _reply(event, "未知命令，请使用 /animemo help。")

    async def _status_text(self):
        poll = self.poller.status if self.poller else "STOPPED"
        return "\n".join((
            "AniMemo Bridge: 已启用" if self.client else "AniMemo Bridge: 未启用",
            f"Server: {_config_value(self.config, 'animemo_base_url')}",
            f"Key ID: {self._masked_key_id()}",
            f"Event poller: {poll}",
            f"Current route: {'已绑定本地投递路由' if self.routes.count() else '无'}",
            f"HMAC connectivity: {self.last_ping}",
            f"Last successful poll: {self.state.last_successful_poll or 'NOT RUN'}",
            f"Last error: {self.state.last_error or 'NONE'}",
            f"Config: {self.configuration_error}" if self.configuration_error else "",
        ))

    @filter.command("animemo")
    async def animemo(self, event: AstrMessageEvent):
        return await self._command(event)


__all__ = ["AniMemoBridge"]
