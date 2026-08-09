from __future__ import annotations


def _history_updated(payload):
    count = payload.get("count") if isinstance(payload, dict) else None
    return f"AniMemo 观看记录已更新{f'（{count} 条）' if count is not None else ''}。"


def _import_completed(payload):
    count = payload.get("imported_records") if isinstance(payload, dict) else None
    return f"AniMemo 导入已完成{f'（{count} 条观看记录）' if count is not None else ''}。"


RENDERERS = {
    ("watch-history-importer", "history-updated"): _history_updated,
    ("watch-history-importer", "import-completed"): _import_completed,
}


def render_event(event, *, developer=False):
    plugin = str(event.get("plugin_slug") or "unknown")
    name = str(event.get("event_name") or event.get("event") or "notice")
    renderer = RENDERERS.get((plugin, name))
    if renderer:
        return renderer(event.get("payload") or {})
    message = f"AniMemo 有一条来自 {plugin} 的通知：{name}。"
    if developer and isinstance(event.get("payload"), dict):
        keys = ", ".join(sorted(str(key) for key in event["payload"])[:8])
        if keys:
            message += f" 字段：{keys}"
    return message
