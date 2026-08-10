from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def format_status_timestamp(value, timezone_name=None):
    """Render a stored UTC timestamp in the requested or host-local timezone."""
    if not value:
        return "未运行"
    raw = str(value).strip()
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        target_timezone = ZoneInfo(timezone_name) if timezone_name else None
        local = parsed.astimezone(target_timezone)
        offset = local.strftime("%z")
        offset = f"{offset[:3]}:{offset[3:]}" if len(offset) == 5 else offset
        return f"{local:%Y-%m-%d %H:%M:%S} (UTC{offset})"
    except (TypeError, ValueError, ZoneInfoNotFoundError):
        return raw
