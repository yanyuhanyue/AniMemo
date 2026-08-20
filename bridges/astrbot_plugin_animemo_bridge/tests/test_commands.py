import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import UUID

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from astrbot_stub import install_astrbot_stubs, set_plugin_data_root

install_astrbot_stubs()

from animemo_bridge.errors import BridgeConnectionError, PairingResultUnknown
from animemo_bridge.identity import MessageIdentity
from main import AniMemoBridge, _config_bool, _validated_timing


class CommandsTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _bridge(temp, config, context=None):
        set_plugin_data_root(Path(temp) / "data" / "plugin_data")
        return AniMemoBridge(context or SimpleNamespace(), config)

    def _event(self, text, *, message_type="private", admin=False):
        return SimpleNamespace(
            message_str=text,
            platform="telegram",
            get_sender_id=lambda: "42",
            get_sender_name=lambda: "A",
            unified_msg_origin="telegram:private:42" if message_type == "private" else "telegram:group:1",
            message_type=message_type,
            is_admin=lambda: admin,
            send=lambda value: value,
        )

    def test_runtime_config_parses_bool_strings_and_rejects_invalid_timing(self):
        self.assertFalse(_config_bool({"enabled": "false"}, "enabled"))
        self.assertFalse(_config_bool({"allow_group_commands": 2}, "allow_group_commands"))
        self.assertTrue(_config_bool({"poll_events": "yes"}, "poll_events"))
        self.assertEqual(
            _validated_timing({"poll_wait_seconds": "20", "request_timeout_seconds": "35"}),
            (20, 35),
        )
        for config in (
            {"poll_wait_seconds": -1, "request_timeout_seconds": 35},
            {"poll_wait_seconds": 26, "request_timeout_seconds": 35},
            {"poll_wait_seconds": 20, "request_timeout_seconds": 4},
            {"poll_wait_seconds": 20, "request_timeout_seconds": 121},
            {"poll_wait_seconds": 20, "request_timeout_seconds": 20},
            {"poll_wait_seconds": "NaN", "request_timeout_seconds": 35},
            {"poll_wait_seconds": None, "request_timeout_seconds": 35},
        ):
            with self.subTest(config=config), self.assertRaises(ValueError):
                _validated_timing(config)

    def test_management_schema_exposes_no_tls_verification_bypass(self):
        schema_path = Path(__file__).resolve().parents[1] / "_conf_schema.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        self.assertNotIn("verify_tls", schema)

    async def test_environment_secret_override_stays_out_of_status_and_state(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(
            "os.environ",
            {
                "ANIMEMO_INTEGRATION_KEY_ID": "test-key-id",
                "ANIMEMO_INTEGRATION_SECRET": "environment-only-test-secret",
            },
            clear=False,
        ):
            context = SimpleNamespace(register_web_api=lambda *_args: None)
            bridge = self._bridge(
                temp,
                {
                    "enabled": True,
                    "key_id": "config-key-id",
                    "secret": "config-test-secret",
                    "poll_events": False,
                },
                context,
            )
            await bridge.initialize()
            self.assertEqual(bridge.client.config.key_id, "test-key-id")
            self.assertEqual(bridge.client.config.secret, "environment-only-test-secret")
            serialized = str(await bridge._web_status())
            self.assertNotIn("environment-only-test-secret", serialized)
            self.assertNotIn("config-test-secret", serialized)
            await bridge.terminate()
            for path in (bridge.routes.path, bridge.state.path):
                content = path.read_text(encoding="utf-8")
                self.assertNotIn("environment-only-test-secret", content)
                self.assertNotIn("config-test-secret", content)

    async def test_invalid_service_url_is_never_reflected_in_status(self):
        with tempfile.TemporaryDirectory() as temp:
            sensitive_url = "https://operator:password@animemo.example"
            bridge = self._bridge(
                temp,
                {
                    "enabled": True,
                    "animemo_base_url": sensitive_url,
                    "key_id": "key",
                    "secret": "secret",
                    "poll_events": False,
                },
                SimpleNamespace(register_web_api=lambda *_args: None),
            )
            await bridge.initialize()
            self.assertIsNone(bridge.client)
            web_status = str(await bridge._web_status())
            command_status = await bridge._status_text()
            self.assertNotIn(sensitive_url, web_status)
            self.assertNotIn(sensitive_url, command_status)
            self.assertNotIn("password", web_status)
            self.assertNotIn("password", command_status)
            self.assertIn("配置无效", command_status)

    async def test_group_pair_is_rejected_without_client(self):
        with tempfile.TemporaryDirectory() as temp:
            bridge = self._bridge(temp, {"enabled": False})
            event = SimpleNamespace(message_str="/animemo pair ABC", platform="qq", get_sender_id=lambda: "42", get_sender_name=lambda: "A", unified_msg_origin="qq:group:1", message_type="group", send=lambda text: text)
            result = await bridge._command(event)
            self.assertIn("私聊", result)

    async def test_help_is_available_when_disabled(self):
        with tempfile.TemporaryDirectory() as temp:
            bridge = self._bridge(temp, {"enabled": False})
            event = self._event("/animemo help")
            self.assertIn("pair", await bridge._command(event))

    async def test_user_status_and_ping_copy_are_localized_without_changing_machine_values(self):
        class PingClient:
            async def ping(self):
                return {"status": "ok"}

        with tempfile.TemporaryDirectory() as temp:
            bridge = self._bridge(temp, {"enabled": True})
            bridge.client = PingClient()
            status_text = await bridge._status_text()
            self.assertIn("服务地址：", status_text)
            self.assertIn("事件轮询器：已停止", status_text)
            self.assertIn("HMAC 连通性：未运行", status_text)
            self.assertIn("最近成功轮询（AstrBot 本地时区）：未运行", status_text)
            for legacy_label in ("Server:", "Event poller:", "HMAC connectivity:", "Last error:"):
                self.assertNotIn(legacy_label, status_text)

            ping_text = await bridge._command(self._event("/animemo ping"))
            self.assertEqual(ping_text, "AniMemo HMAC 连通性：正常")
            self.assertEqual(bridge.last_ping, "OK")

    async def test_watch_commands_map_to_reference_actions(self):
        class RecordingClient:
            def __init__(self):
                self.calls = []

            async def action(self, request_id, platform, external_user_id, action, payload):
                self.calls.append((request_id, platform, external_user_id, action, payload))
                if action.endswith("entries-search"):
                    return {"entries": [{"entry_id": 7, "title": "葬送的芙莉莲"}]}
                if action.endswith("history-add"):
                    return {"created": True, "record": {"watched_on": "2026-08-09", "episode_start": 7}}
                return {"entry_id": 7, "records": []}

        with tempfile.TemporaryDirectory() as temp:
            bridge = self._bridge(temp, {"enabled": True})
            bridge.client = RecordingClient()
            for text, expected_action, expected_payload in (
                ("/animemo watch get 7", "watch-history-importer.history-get", {"entry_id": 7}),
                ("/animemo watch add 7 2026-08-09 7", "watch-history-importer.history-add", {"entry_id": 7, "watched_on": "2026-08-09", "episode_start": 7, "episode_end": 7}),
                ("/animemo watch find 芙莉莲", "watch-history-importer.entries-search", {"query": "芙莉莲"}),
            ):
                await bridge._command(self._event(text))
                request_id, platform, external_user_id, action, payload = bridge.client.calls[-1]
                UUID(request_id)
                self.assertEqual((platform, external_user_id, action, payload), ("telegram", "42", expected_action, expected_payload))

    async def test_developer_action_requires_flag_and_admin_when_available(self):
        class RecordingClient:
            def __init__(self):
                self.calls = []

            async def action(self, *args):
                self.calls.append(args)
                return {"secret": "must-not-be-rendered"}

        with tempfile.TemporaryDirectory() as temp:
            bridge = self._bridge(temp, {"enabled": True, "developer_commands": True})
            bridge.client = RecordingClient()
            denied = await bridge._command(self._event('/animemo action watch-history-importer.history-get {"entry_id":1}', admin=False))
            self.assertIn("关闭", denied)
            allowed = await bridge._command(self._event('/animemo action watch-history-importer.history-get {"entry_id":1}', admin=True))
            self.assertEqual(allowed, "动作执行完成。")
            self.assertNotIn("must-not-be-rendered", allowed)
            self.assertEqual(len(bridge.client.calls), 1)

    async def test_developer_debug_requires_flag_and_admin(self):
        with tempfile.TemporaryDirectory() as temp:
            bridge = self._bridge(temp, {"enabled": False, "developer_commands": True})

            denied = await bridge._command(self._event("/animemo debug", admin=False))
            allowed = await bridge._command(self._event("/animemo debug", admin=True))

            self.assertIn("关闭", denied)
            self.assertIn("route count=", allowed)

    async def test_pair_route_is_saved_only_after_confirmed_success(self):
        class PairClient:
            async def pair(self, *args):
                raise BridgeConnectionError("timeout")

        with tempfile.TemporaryDirectory() as temp:
            bridge = self._bridge(temp, {"enabled": True})
            bridge.client = PairClient()
            await bridge._command(self._event("/animemo pair ABC"))
            self.assertEqual(bridge.routes.count(), 0)

        class UnknownPairClient:
            async def pair(self, *args):
                raise PairingResultUnknown("unknown")

        with tempfile.TemporaryDirectory() as temp:
            bridge = self._bridge(temp, {"enabled": True})
            bridge.client = UnknownPairClient()
            result = await bridge._command(self._event("/animemo pair ABC"))
            self.assertIn("结果未知", result)
            self.assertNotIn("ABC", result)
            self.assertEqual(bridge.routes.count(), 0)

        class SuccessClient:
            async def pair(self, *args):
                return {"binding_id": 1}

        with tempfile.TemporaryDirectory() as temp:
            bridge = self._bridge(temp, {"enabled": True})
            bridge.client = SuccessClient()
            await bridge._command(self._event("/animemo pair ABC"))
            self.assertEqual(bridge.routes.count(), 1)

    async def test_diagnostics_register_once_and_clear_only_selected_route(self):
        with tempfile.TemporaryDirectory() as temp:
            registered = []
            context = SimpleNamespace(register_web_api=lambda *args: registered.append(args))
            bridge = self._bridge(temp, {"enabled": True, "key_id": "", "secret": ""}, context)
            await bridge.initialize()
            await bridge.initialize()
            self.assertEqual(len(registered), 4)
            status = await bridge._web_status()
            self.assertFalse(status["configured"])
            self.assertEqual(status["key_id"], "未配置")
            self.assertEqual(status["poller"], "STOPPED")
            self.assertEqual(status["last_ping"], "NOT RUN")

            bridge.routes.save_private(MessageIdentity("qq", "42", "A", "private", "qq:private:42"))
            bridge.routes.save_private(MessageIdentity("telegram", "42", "B", "private", "telegram:private:42"))
            selected = next(item for item in bridge.routes.masked_routes() if item["platform"] == "qq")
            result = await bridge._web_clear_route(
                {"platform": "qq", "external_user_hash": selected["external_user_id"]}
            )
            self.assertEqual(result, {"status": "cleared"})
            self.assertIsNone(bridge.routes.get("qq", "42"))
            self.assertIsNotNone(bridge.routes.get("telegram", "42"))
            serialized = str(await bridge._web_status())
            self.assertNotIn("qq:private:42", serialized)

    async def test_official_plugin_data_path_persists_and_install_dir_is_untouched(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            install_dir = root / "data" / "plugins" / "astrbot_plugin_animemo_bridge"
            install_dir.mkdir(parents=True)
            marker = install_dir / "main.py"
            marker.write_text("plugin code", encoding="utf-8")

            bridge = self._bridge(temp, {"enabled": False})
            expected = root / "data" / "plugin_data" / "astrbot_plugin_animemo_bridge"
            self.assertEqual(bridge.routes.path.parent, expected.resolve())
            self.assertEqual(bridge.state.path.parent, expected.resolve())
            bridge.routes.save_private(MessageIdentity("qq", "42", "A", "private", "qq:FriendMessage:42"))
            bridge.state.mark_delivered(7)
            bridge.state.advance(7)
            await bridge.terminate()

            reloaded = self._bridge(temp, {"enabled": False})
            self.assertIsNotNone(reloaded.routes.get("qq", "42"))
            self.assertTrue(reloaded.state.has_delivered(7))
            self.assertEqual(reloaded.state.cursor, 7)
            self.assertEqual([path.name for path in install_dir.iterdir()], [marker.name])

            reloaded.routes.path.write_text("{broken", encoding="utf-8")
            recovered = self._bridge(temp, {"enabled": False})
            self.assertEqual(recovered.routes.count(), 0)
            self.assertEqual(len(list(expected.glob("routes.json.corrupt-*"))), 1)

    def test_production_main_contains_no_fake_astrbot_runtime(self):
        source = (Path(__file__).resolve().parents[1] / "main.py").read_text(encoding="utf-8")
        for forbidden in ("_FallbackLogger", "class Star:", "class Context:", "astrbot.api.message"):
            self.assertNotIn(forbidden, source)
