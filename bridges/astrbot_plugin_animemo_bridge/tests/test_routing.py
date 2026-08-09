import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from animemo_bridge.identity import MessageIdentity
from animemo_bridge.routing import RouteStore


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
