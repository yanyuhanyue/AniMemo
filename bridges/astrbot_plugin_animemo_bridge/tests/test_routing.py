import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from animemo_bridge.identity import MessageIdentity
from animemo_bridge.routing import RouteStore
from animemo_bridge.state import EventState


class RoutingTests(unittest.TestCase):
    def test_private_route_persists_and_group_does_not_overwrite(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "routes.json"
            store = RouteStore(path)
            self.assertTrue(store.save_private(MessageIdentity("qq", "42", "A", "private", "qq:private:42")))
            self.assertFalse(store.save_private(MessageIdentity("qq", "42", "A", "group", "qq:group:7")))
            self.assertEqual(store.get("qq", "42")["umo"], "qq:private:42")
            reloaded = RouteStore(path)
            self.assertEqual(reloaded.count(), 1)
            self.assertNotIn("42", json.dumps(reloaded.masked_routes(), ensure_ascii=False))

    def test_corrupt_state_is_backed_up(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "routes.json"
            path.write_text("{broken", encoding="utf-8")
            store = RouteStore(path)
            self.assertEqual(store.count(), 0)
            self.assertTrue(any(path.parent.glob("routes.json.corrupt-*")))

    def test_platform_and_external_user_are_isolated(self):
        with tempfile.TemporaryDirectory() as temp:
            store = RouteStore(Path(temp) / "routes.json")
            store.save_private(MessageIdentity("qq", "42", "QQ", "private", "qq:private:42"))
            store.save_private(MessageIdentity("telegram", "42", "Telegram", "private", "telegram:private:42"))
            store.save_private(MessageIdentity("qq", "43", "QQ2", "private", "qq:private:43"))
            self.assertEqual(store.count(), 3)
            self.assertEqual(store.get("telegram", "42")["umo"], "telegram:private:42")
            self.assertTrue(store.clear_masked("qq", store.masked_routes()[0]["external_user_id"]))

    def test_event_state_persists_pending_and_delivered_ids(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "state.json"
            state = EventState(path)
            state.defer(1)
            state.mark_delivered(2)
            state.advance(2)
            self.assertEqual(state.cursor, 0)
            state.resolve_pending(1)
            state.advance(2)
            state.save()
            reloaded = EventState(path)
            self.assertEqual(reloaded.cursor, 2)
            self.assertEqual(reloaded.delivered_count, 1)

    def test_pending_events_are_not_evicted_by_delivered_cache_limit(self):
        with tempfile.TemporaryDirectory() as temp:
            state = EventState(Path(temp) / "state.json", max_delivered=500)
            for event_id in range(1, 602):
                state.defer(event_id)
            state.advance(601)
            self.assertEqual(state.cursor, 0)
            self.assertEqual(state.pending_event_ids[0], 1)
            self.assertEqual(len(state.pending_event_ids), 601)
