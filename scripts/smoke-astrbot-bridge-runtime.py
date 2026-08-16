#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
from contextlib import contextmanager
from pathlib import Path
from queue import Queue
from tempfile import TemporaryDirectory
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
BRIDGE_SOURCE = ROOT / "bridges" / "astrbot_plugin_animemo_bridge"
PLUGIN_NAME = "astrbot_plugin_animemo_bridge"


def require(condition, detail):
    if not condition:
        raise RuntimeError(detail)


class SmokePlatform:
    def __init__(self):
        self.sent = []

    @staticmethod
    def meta():
        return SimpleNamespace(id="animemo-smoke", name="animemo-smoke")

    async def send_by_session(self, session, message_chain):
        self.sent.append((session, message_chain))


class SmokeConfigManager:
    @staticmethod
    def get_conf(_umo):
        return {}


def build_context(Context):
    platform = SmokePlatform()
    context = Context(
        Queue(),
        {},
        SimpleNamespace(),
        SimpleNamespace(),
        SimpleNamespace(platform_insts=[platform]),
        SimpleNamespace(),
        SimpleNamespace(),
        SimpleNamespace(),
        SmokeConfigManager(),
        SimpleNamespace(),
        SimpleNamespace(),
    )
    context.registered_web_apis = []
    return context, platform


@contextmanager
def isolated_astrbot_root(environ=os.environ):
    previous = environ.get("ASTRBOT_ROOT")
    with TemporaryDirectory(prefix="animemo-astrbot-smoke-") as directory:
        root = Path(directory).resolve()
        environ["ASTRBOT_ROOT"] = str(root)
        try:
            yield root
        finally:
            if previous is None:
                environ.pop("ASTRBOT_ROOT", None)
            else:
                environ["ASTRBOT_ROOT"] = previous


async def smoke():
    with isolated_astrbot_root() as astrbot_root:
        await smoke_in_isolated_root(astrbot_root)


async def smoke_in_isolated_root(astrbot_root):
    install_dir = astrbot_root / "data" / "plugins" / PLUGIN_NAME
    shutil.copytree(
        BRIDGE_SOURCE,
        install_dir,
        ignore=shutil.ignore_patterns("tests", "__pycache__", "*.pyc", ".env"),
    )
    sys.path.insert(0, str(install_dir))

    import astrbot
    import yaml
    from astrbot.api.event import MessageChain
    from astrbot.api.star import Context, Star, StarTools
    from astrbot.core.star.filter.command import CommandFilter
    from astrbot.core.star.star import star_map
    from astrbot.core.star.star_handler import star_handlers_registry

    import main as bridge_main
    from animemo_bridge.identity import MessageIdentity
    from packaging.specifiers import SpecifierSet

    require(bridge_main.Star is Star, "Bridge did not import the real AstrBot Star")
    require(bridge_main.Context is Context, "Bridge did not import the real AstrBot Context")
    require(issubclass(bridge_main.AniMemoBridge, Star), "AniMemoBridge is not a real Star subclass")
    require(bridge_main.MessageChain is MessageChain, "Bridge MessageChain import path is not official")
    package_metadata = yaml.safe_load((install_dir / "metadata.yaml").read_text(encoding="utf-8"))
    expected_version = str(package_metadata.get("version", "")).strip()
    require(expected_version, "metadata.yaml does not declare a plugin version")
    require(
        astrbot.__version__ in SpecifierSet(package_metadata.get("astrbot_version", "")),
        "metadata.yaml does not allow the tested AstrBot runtime",
    )

    metadata = star_map.get(bridge_main.AniMemoBridge.__module__)
    require(metadata is not None, "AstrBot register metadata was not created")
    require(metadata.name == PLUGIN_NAME and metadata.version == expected_version, "register metadata is invalid")
    handler = star_handlers_registry.get_handler_by_full_name("main_animemo")
    require(handler is not None, "@filter.command did not register the animemo handler")
    require(
        any(isinstance(item, CommandFilter) and item.command_name == "animemo" for item in handler.event_filters),
        "animemo command filter metadata is invalid",
    )

    chain = MessageChain().message("AniMemo runtime smoke")
    require(chain.chain, "MessageChain.message did not append a message component")
    context, platform = build_context(Context)
    require(
        await context.send_message("animemo-smoke:FriendMessage:user-1", chain),
        "Context.send_message could not resolve the smoke platform",
    )
    require(len(platform.sent) == 1, "smoke platform did not receive the active message")

    data_dir = StarTools.get_data_dir(PLUGIN_NAME)
    expected_data_dir = (astrbot_root / "data" / "plugin_data" / PLUGIN_NAME).resolve()
    require(data_dir == expected_data_dir, "StarTools returned an unexpected plugin data path")
    bridge = bridge_main.AniMemoBridge(context, {"enabled": False})
    await bridge.initialize()
    require(len(context.registered_web_apis) == 4, "Bridge did not register all management Web APIs")
    bridge.routes.save_private(
        MessageIdentity("animemo-smoke", "user-1", "Smoke", "private", "animemo-smoke:FriendMessage:user-1")
    )
    bridge.state.mark_delivered(9)
    bridge.state.advance(9)
    await bridge.terminate()

    reloaded_context, _ = build_context(Context)
    reloaded = bridge_main.AniMemoBridge(reloaded_context, {"enabled": False})
    require(reloaded.routes.get("animemo-smoke", "user-1") is not None, "route did not survive reload")
    require(reloaded.state.has_delivered(9) and reloaded.state.cursor == 9, "event state did not survive reload")
    await reloaded.initialize()
    await reloaded.terminate()
    require(not (install_dir / "routes.json").exists(), "route state was written into the plugin install directory")
    require(not (install_dir / "state.json").exists(), "event state was written into the plugin install directory")

    print(json.dumps({
        "astrbot_version": astrbot.__version__,
        "bridge": PLUGIN_NAME,
        "real_star": True,
        "command_registered": True,
        "web_api_count": len(reloaded_context.registered_web_apis),
        "persistent_data_dir": str(data_dir),
        "send_message": "PASS",
    }, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    asyncio.run(smoke())
