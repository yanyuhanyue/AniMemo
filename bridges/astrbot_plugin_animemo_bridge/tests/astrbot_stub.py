from __future__ import annotations

import logging
import sys
import tempfile
from pathlib import Path
from types import ModuleType


_plugin_data_root = Path(tempfile.gettempdir()) / "animemo-astrbot-unit-plugin-data"


def set_plugin_data_root(path):
    global _plugin_data_root
    _plugin_data_root = Path(path)


class MessageChain:
    def __init__(self):
        self.chain = []

    def message(self, text):
        self.chain.append(str(text))
        return self


class Star:
    def __init__(self, context=None, config=None):
        self.context = context
        self.config = config or {}


class Context:
    pass


class StarTools:
    @classmethod
    def get_data_dir(cls, plugin_name=None):
        path = _plugin_data_root / str(plugin_name or "test-plugin")
        path.mkdir(parents=True, exist_ok=True)
        return path.resolve()


class AstrMessageEvent:
    pass


class PluginRequestProxy:
    async def json(self, default=None):
        return default


def register(*metadata):
    def decorator(cls):
        cls.__astrbot_test_registration__ = metadata
        return cls

    return decorator


def _command(*metadata, **_kwargs):
    def decorator(function):
        function.__astrbot_test_command__ = metadata
        return function

    return decorator


def install_astrbot_stubs():
    astrbot = ModuleType("astrbot")
    astrbot.__path__ = []
    api = ModuleType("astrbot.api")
    api.__path__ = []
    event = ModuleType("astrbot.api.event")
    star = ModuleType("astrbot.api.star")
    web = ModuleType("astrbot.api.web")
    filter_module = ModuleType("astrbot.api.event.filter")

    api.logger = logging.getLogger("astrbot-unit-stub")
    event.AstrMessageEvent = AstrMessageEvent
    event.MessageChain = MessageChain
    event.filter = filter_module
    filter_module.command = _command
    star.Context = Context
    star.Star = Star
    star.StarTools = StarTools
    star.register = register
    web.request = PluginRequestProxy()

    astrbot.api = api
    api.event = event
    api.star = star
    api.web = web
    sys.modules.update(
        {
            "astrbot": astrbot,
            "astrbot.api": api,
            "astrbot.api.event": event,
            "astrbot.api.event.filter": filter_module,
            "astrbot.api.star": star,
            "astrbot.api.web": web,
        }
    )
