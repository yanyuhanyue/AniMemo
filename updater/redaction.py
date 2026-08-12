from __future__ import annotations

import re


_HEADER = re.compile(r"(?i)(authorization\s*:\s*(?:bearer|basic)\s+)[^\s,;]+")
_KEY_VALUE = re.compile(
    r"(?i)\b([A-Z0-9_]*(?:PASSWORD|SECRET|TOKEN|API_KEY|ACCESS_KEY|PRIVATE_KEY)[A-Z0-9_]*\s*=\s*)([^\s,;]+)"
)
_URL_CREDENTIALS = re.compile(r"(?P<scheme>https?://)(?P<user>[^/@:\s]+):(?P<password>[^/@\s]+)@")


def redact(value: object) -> str:
    text = str(value)
    text = _HEADER.sub(r"\1[REDACTED]", text)
    text = _KEY_VALUE.sub(r"\1[REDACTED]", text)
    return _URL_CREDENTIALS.sub(r"\g<scheme>\g<user>:[REDACTED]@", text)
