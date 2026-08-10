from __future__ import annotations

import logging
import re


_PAIRING_COMMAND_RE = re.compile(
    r"(?P<prefix>(?<!\S)/?animemo\s+pair\s+)(?P<code>\S+)",
    re.IGNORECASE,
)
_FILTER_MARKER = "_animemo_pairing_log_redactor"


def redact_pairing_command(text: str) -> str:
    """Redact the one-time pairing argument while preserving the command context."""
    return _PAIRING_COMMAND_RE.sub(r"\g<prefix>[REDACTED]", str(text))


class PairingCommandLogFilter(logging.Filter):
    """Prevent one-time pairing codes from reaching AstrBot log handlers."""

    def __init__(self):
        super().__init__()
        setattr(self, _FILTER_MARKER, True)

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            rendered = record.getMessage()
        except Exception:
            return True
        redacted = redact_pairing_command(rendered)
        if redacted != rendered:
            record.msg = redacted
            record.args = ()
        return True


def install_pairing_log_redactor(logger: logging.Logger) -> None:
    """Install the filter once on AstrBot's shared logger."""
    if any(getattr(existing, _FILTER_MARKER, False) for existing in logger.filters):
        return
    logger.addFilter(PairingCommandLogFilter())
