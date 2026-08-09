from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from astrbot.api.event import MessageChain

from .errors import (
    BridgeAuthError,
    BridgeConnectionError,
    BridgeEventError,
    BridgeProtocolError,
    BridgeRateLimitError,
)
from .renderers import render_event


async def send_private_message(context, umo, text):
    chain = MessageChain().message(text)
    sender = getattr(context, "send_message", None)
    if not callable(sender):
        raise BridgeEventError("AstrBot context.send_message 不可用。")
    result = sender(umo, chain)
    if hasattr(result, "__await__"):
        result = await result
    if result is False:
        raise BridgeEventError("AstrBot 未找到可用的私聊投递平台。")


class EventPoller:
    def __init__(self, *, client, context, routes, state, logger=None, wait_seconds=20, developer=False, sleep=asyncio.sleep):
        self.client = client
        self.context = context
        self.routes = routes
        self.state = state
        self.logger = logger
        self.wait_seconds = min(int(wait_seconds), 25)
        self.developer = developer
        self.sleep = sleep
        self.task = None
        self.stopping = False
        self.status = "STOPPED"

    def start(self):
        if self.task and not self.task.done():
            return self.task
        self.stopping = False
        self.task = asyncio.create_task(self.run(), name="animemo-bridge-event-poller")
        return self.task

    async def stop(self):
        self.stopping = True
        task, self.task = self.task, None
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self.status = "STOPPED"
        self.state.save()

    async def run(self):
        self.status = "RUNNING"
        backoff = 1.0
        while not self.stopping:
            try:
                result = await self.client.events(after=self.state.cursor, limit=50, wait=self.wait_seconds)
                events = result.get("events", [])
                if not isinstance(events, list):
                    raise BridgeProtocolError("AniMemo events 字段不是数组。")
                for event in events:
                    await self._deliver(event)
                if not events:
                    self.state.advance(result.get("next_cursor", self.state.cursor))
                self.state.last_successful_poll = datetime.now(timezone.utc).isoformat()
                self.state.last_error = ""
                self.state.save()
                backoff = 1.0
            except asyncio.CancelledError:
                raise
            except (BridgeConnectionError, BridgeRateLimitError, BridgeAuthError, BridgeEventError, BridgeProtocolError) as error:
                self.state.last_error = type(error).__name__
                self.state.save()
                if self.logger:
                    self.logger.warning("AniMemo Bridge poll degraded: %s", type(error).__name__)
                await self.sleep(min(backoff, 60.0) + 0.1 * (hash(type(error).__name__) % 3))
                backoff = min(backoff * 2, 60.0)

    async def _deliver(self, event):
        if not isinstance(event, dict):
            raise BridgeEventError("AniMemo 事件结构无效。")
        event_id = event.get("event_id")
        platform = event.get("platform")
        external_user_id = event.get("external_user_id")
        plugin_slug = event.get("plugin_slug")
        event_name = event.get("event_name")
        if not isinstance(event_id, int) or not platform or not external_user_id or not plugin_slug or not event_name:
            raise BridgeEventError("AniMemo 事件结构无效。")
        route = self.routes.get(platform, external_user_id)
        if route is None:
            # Keep the unacknowledged event available for a later route refresh,
            # but avoid hammering the same event in a tight loop.
            self.state.defer(event_id)
            self.state.save()
            await self.sleep(min(max(self.wait_seconds, 1), 5))
            return
        if self.state.has_delivered(event_id):
            await self.client.ack([event_id])
            self.state.resolve_pending(event_id)
            self.state.advance(event_id)
            self.state.save()
            return
        text = render_event(event, developer=self.developer)
        await send_private_message(self.context, route["umo"], text)
        self.state.mark_delivered(event_id)
        self.state.save()
        await self.client.ack([event_id])
        self.state.advance(event_id)
        self.state.save()
