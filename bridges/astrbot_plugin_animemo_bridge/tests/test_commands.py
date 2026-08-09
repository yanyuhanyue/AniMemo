import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from main import AniMemoBridge


class CommandsTests(unittest.IsolatedAsyncioTestCase):
    async def test_group_pair_is_rejected_without_client(self):
        with tempfile.TemporaryDirectory() as temp:
            context = SimpleNamespace(data_dir=temp)
            bridge = AniMemoBridge(context, {"enabled": False})
            event = SimpleNamespace(message_str="/animemo pair ABC", platform="qq", get_sender_id=lambda: "42", get_sender_name=lambda: "A", unified_msg_origin="qq:group:1", message_type="group", send=lambda text: text)
            result = await bridge._command(event)
            self.assertIn("私聊", result)

    async def test_help_is_available_when_disabled(self):
        with tempfile.TemporaryDirectory() as temp:
            bridge = AniMemoBridge(SimpleNamespace(data_dir=temp), {"enabled": False})
            event = SimpleNamespace(message_str="/animemo help", platform="telegram", get_sender_id=lambda: "1", get_sender_name=lambda: "A", unified_msg_origin="telegram:private:1", message_type="private", send=lambda text: text)
            self.assertIn("pair", await bridge._command(event))
