import logging

from ..manifest import SLUG_RE

_EVENT = "plugin_log_event"
_STAGE = "plugin_sdk_log"


class PluginLoggerAdapter(logging.LoggerAdapter):
    def process(self, msg, kwargs):
        return _EVENT, {
            "extra": {
                "animemo_stage": _STAGE,
                "plugin": self.extra["plugin"],
            }
        }

    def log(self, level, msg, *args, **kwargs):
        if self.isEnabledFor(level):
            safe_message, safe_kwargs = self.process(msg, kwargs)
            self.logger.log(level, safe_message, **safe_kwargs)


def get_plugin_logger(plugin_slug):
    slug = plugin_slug if isinstance(plugin_slug, str) else ""
    if SLUG_RE.fullmatch(slug) is None:
        raise ValueError("plugin logger requires a validated plugin slug")
    return PluginLoggerAdapter(logging.getLogger("animemo.plugins"), {"plugin": slug})
