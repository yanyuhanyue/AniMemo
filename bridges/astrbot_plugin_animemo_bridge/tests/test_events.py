import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from animemo_bridge.errors import BridgeAuthError, BridgeEventError
from animemo_bridge.events import EventPoller
from animemo_bridge.identity import MessageIdentity
from animemo_bridge.renderers import render_event
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


class FlakyAckClient(FakeClient):
    def __init__(self):
        super().__init__()
        self.fail_ack_once = True

    async def ack(self, event_ids):
        self.acks.extend(event_ids)
        if self.fail_ack_once:
            self.fail_ack_once = False
            raise BridgeEventError("ack failed")
        return {"acked": len(event_ids)}


class AuthBackoffClient(FakeClient):
    async def events(self, **kwargs):
        self.polled += 1
        if self.polled == 1:
            raise BridgeAuthError("auth failed")
        return {"events": [], "next_cursor": kwargs.get("after", 0)}


class InvalidShapeClient(FakeClient):
    async def events(self, **kwargs):
        self.polled += 1
        return {"events": {"event_id": 1}, "next_cursor": kwargs.get("after", 0)}


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
            self.assertEqual(EventState(Path(temp) / "state.json").cursor, 1)

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

    async def test_missing_earlier_route_does_not_let_cursor_skip_later_event(self):
        with tempfile.TemporaryDirectory() as temp:
            routes = RouteStore(Path(temp) / "routes.json")
            routes.save_private(MessageIdentity("qq", "later", "B", "private", "qq:private:later"))
            state = EventState(Path(temp) / "state.json")
            client, context = FakeClient(), FakeContext()
            poller = EventPoller(client=client, context=context, routes=routes, state=state, wait_seconds=0)
            await poller._deliver({"event_id": 1, "platform": "qq", "external_user_id": "missing", "plugin_slug": "watch-history-importer", "event_name": "history-updated", "payload": {}})
            await poller._deliver({"event_id": 2, "platform": "qq", "external_user_id": "later", "plugin_slug": "watch-history-importer", "event_name": "history-updated", "payload": {}})
            self.assertEqual(state.cursor, 0)
            self.assertIn(1, state.pending_event_ids)

    async def test_ack_failure_replay_does_not_send_duplicate(self):
        with tempfile.TemporaryDirectory() as temp:
            routes = RouteStore(Path(temp) / "routes.json")
            routes.save_private(MessageIdentity("qq", "42", "A", "private", "qq:private:42"))
            state = EventState(Path(temp) / "state.json")
            client, context = FlakyAckClient(), FakeContext()
            poller = EventPoller(client=client, context=context, routes=routes, state=state, wait_seconds=0)
            event = {"event_id": 6, "platform": "qq", "external_user_id": "42", "plugin_slug": "watch-history-importer", "event_name": "history-updated", "payload": {}}
            with self.assertRaises(BridgeEventError):
                await poller._deliver(event)
            await poller._deliver(event)
            self.assertEqual(len(context.sent), 1)
            self.assertEqual(client.acks, [6, 6])

    async def test_unknown_event_uses_safe_renderer(self):
        text = render_event({"plugin_slug": "unknown-plugin", "event_name": "secret-event", "payload": {"secret": "never"}})
        self.assertEqual(text, "AniMemo 有一条来自 unknown-plugin 的通知：secret-event。")
        self.assertNotIn("never", text)

    async def test_auth_failure_enters_backoff_and_recovers(self):
        sleeps = []

        async def sleep(delay):
            sleeps.append(delay)
            poller.stopping = True

        with tempfile.TemporaryDirectory() as temp:
            poller = EventPoller(
                client=AuthBackoffClient(),
                context=FakeContext(),
                routes=RouteStore(Path(temp) / "routes.json"),
                state=EventState(Path(temp) / "state.json"),
                wait_seconds=0,
                sleep=sleep,
            )
            await poller.run()
            self.assertTrue(sleeps)
            self.assertEqual(poller.state.last_error, "BridgeAuthError")

    async def test_invalid_event_list_enters_protocol_backoff(self):
        sleeps = []

        async def sleep(delay):
            sleeps.append(delay)
            poller.stopping = True

        with tempfile.TemporaryDirectory() as temp:
            poller = EventPoller(
                client=InvalidShapeClient(),
                context=FakeContext(),
                routes=RouteStore(Path(temp) / "routes.json"),
                state=EventState(Path(temp) / "state.json"),
                wait_seconds=0,
                sleep=sleep,
            )
            await poller.run()
            self.assertTrue(sleeps)
            self.assertEqual(poller.state.last_error, "BridgeProtocolError")

    async def test_start_is_idempotent(self):
        with tempfile.TemporaryDirectory() as temp:
            poller = EventPoller(
                client=FakeClient(),
                context=FakeContext(),
                routes=RouteStore(Path(temp) / "routes.json"),
                state=EventState(Path(temp) / "state.json"),
                wait_seconds=0,
            )
            first = poller.start()
            second = poller.start()
            self.assertIs(first, second)
            await poller.stop()
