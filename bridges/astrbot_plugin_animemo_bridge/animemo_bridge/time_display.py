from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


DEFAULT_DISPLAY_TIMEZONE = "Asia/Shanghai"


def format_status_timestamp(value, timezone_name=DEFAULT_DISPLAY_TIMEZONE):
    """Render a stored ISO timestamp in the user-facing display timezone."""
    if not value:
        return "NOT RUN"
    raw = str(value).strip()
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        local = parsed.astimezone(ZoneInfo(timezone_name))
        offset = local.strftime("%z")
        offset = f"{offset[:3]}:{offset[3:]}" if len(offset) == 5 else offset
        return f"{local:%Y-%m-%d %H:%M:%S} (UTC{offset})"
    except (TypeError, ValueError, ZoneInfoNotFoundError):
        return raw
