import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from animemo_bridge.identity import MessageIdentity


class PairingTests(unittest.TestCase):
    def test_pairing_identity_is_provider_neutral_and_private(self):
        private = MessageIdentity("telegram", "42", "Alice", "private", "telegram:dm:42")
        group = MessageIdentity("telegram", "42", "Alice", "group", "telegram:group:7")
        self.assertTrue(private.is_private)
        self.assertFalse(group.is_private)
        self.assertEqual(private.platform, "telegram")

    def test_current_astrbot_style_event_fields(self):
        class Event:
            message_type = "FriendMessage"
            unified_msg_origin = "telegram:FriendMessage:42"

            def get_platform_id(self):
                return "telegram-instance-a"

            def get_sender_id(self):
                return "42"

            def get_sender_name(self):
                return "Alice"

        from animemo_bridge.identity import extract_identity

        identity = extract_identity(Event())
        self.assertTrue(identity.is_private)
        self.assertEqual(identity.platform, "telegram-instance-a")
