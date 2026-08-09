import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from animemo_bridge.errors import BridgeEventError
from animemo_bridge.events import EventPoller
from animemo_bridge.identity import MessageIdentity
from animemo_bridge.routing import RouteStore
from animemo_bridge.state import EventState


class FakeClient:
    def __init__(self):
        self.acks = []
        self.polled = 0

    async def events(self, **kwargs):
        self.polled += 1
        if self.polled == 1:
            return {"events": [{"event_id": 1, "platform": "qq", "external_user_id": "42", "plugin_slug": "watch-history-importer", "event_name": "history-updated", "payload": {"count": 1}}], "next_cursor": 1}
        await asyncio.sleep(0)
        return {"events": [], "next_cursor": 1}

    async def ack(self, event_ids):
        self.acks.extend(event_ids)
        return {"acked": len(event_ids)}


class FakeContext:
    def __init__(self):
        self.sent = []

    async def send_message(self, umo, message):
        self.sent.append((umo, message))


class MissingPlatformContext:
    async def send_message(self, _umo, _message):
        return False


class EventTests(unittest.IsolatedAsyncioTestCase):
    async def test_delivery_dedup_and_ack(self):
        with tempfile.TemporaryDirectory() as temp:
            routes = RouteStore(Path(temp) / "routes.json")
            routes.save_private(MessageIdentity("qq", "42", "A", "private", "qq:private:42"))
            state = EventState(Path(temp) / "state.json")
            client, context = FakeClient(), FakeContext()
            poller = EventPoller(client=client, context=context, routes=routes, state=state, wait_seconds=0)
            await poller._deliver({"event_id": 1, "platform": "qq", "external_user_id": "42", "plugin_slug": "watch-history-importer", "event_name": "history-updated", "payload": {"count": 1}})
            await poller._deliver({"event_id": 1, "platform": "qq", "external_user_id": "42", "plugin_slug": "watch-history-importer", "event_name": "history-updated", "payload": {"count": 1}})
            self.assertEqual(len(context.sent), 1)
            self.assertEqual(client.acks, [1, 1])

    async def test_missing_route_is_not_acked(self):
        with tempfile.TemporaryDirectory() as temp:
            poller = EventPoller(client=FakeClient(), context=FakeContext(), routes=RouteStore(Path(temp) / "routes.json"), state=EventState(Path(temp) / "state.json"), wait_seconds=0)
            await poller._deliver({"event_id": 3, "platform": "qq", "external_user_id": "no-route", "plugin_slug": "watch-history-importer", "event_name": "history-updated", "payload": {}})
            self.assertEqual(poller.client.acks, [])

    async def test_failed_send_is_not_acked(self):
        with tempfile.TemporaryDirectory() as temp:
            poller = EventPoller(client=FakeClient(), context=MissingPlatformContext(), routes=RouteStore(Path(temp) / "routes.json"), state=EventState(Path(temp) / "state.json"), wait_seconds=0)
            poller.routes.save_private(MessageIdentity("qq", "42", "A", "private", "qq:private:42"))
            with self.assertRaises(BridgeEventError):
                await poller._deliver({"event_id": 4, "platform": "qq", "external_user_id": "42", "plugin_slug": "watch-history-importer", "event_name": "history-updated", "payload": {}})
            self.assertEqual(poller.client.acks, [])

    async def test_stop_cancels_poller(self):
        with tempfile.TemporaryDirectory() as temp:
            poller = EventPoller(client=FakeClient(), context=FakeContext(), routes=RouteStore(Path(temp) / "routes.json"), state=EventState(Path(temp) / "state.json"), wait_seconds=0)
            poller.start()
            await asyncio.sleep(0)
            await poller.stop()
            self.assertIsNone(poller.task)
