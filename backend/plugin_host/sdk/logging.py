import logging
import re


_SENSITIVE = re.compile(r"(?i)(password|token|secret|cookie|totp|recovery|csrf|authorization|jwt)\s*[=:]\s*[^\s,;]+")


class PluginLoggerAdapter(logging.LoggerAdapter):
    def process(self, msg, kwargs):
        return _SENSITIVE.sub(r"\1=[REDACTED]", str(msg)), {**kwargs, "extra": {**kwargs.get("extra", {}), "plugin": self.extra["plugin"]}}


def get_plugin_logger(plugin_slug):
    return PluginLoggerAdapter(logging.getLogger("anime_journal.plugins"), {"plugin": str(plugin_slug)})
