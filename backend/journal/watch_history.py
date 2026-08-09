from __future__ import annotations

from datetime import date, datetime


MAX_WATCH_HISTORY_RECORDS = 500
MAX_WATCH_HISTORY_INTEGER = 32767
MAX_WATCH_HISTORY_NOTES = 20
MAX_WATCH_HISTORY_NOTE_LENGTH = 500


class WatchHistoryValidationError(ValueError):
    def __init__(self, detail, *, code="invalid_watch_history"):
        super().__init__(detail)
        self.code = code
        self.detail = detail


def _record_prefix(index):
    return f"第 {index + 1} 条观看记录"


def _normalize_date(value, index):
    if isinstance(value, datetime):
        raise WatchHistoryValidationError(
            f"{_record_prefix(index)}日期无效。",
            code="invalid_watched_on",
        )
    if isinstance(value, date):
        return value
    if not isinstance(value, str) or not value.strip():
        raise WatchHistoryValidationError(
            f"{_record_prefix(index)}缺少观看日期。",
            code="invalid_watched_on",
        )
    try:
        return date.fromisoformat(value.strip())
    except ValueError as error:
        raise WatchHistoryValidationError(
            f"{_record_prefix(index)}日期无效。",
            code="invalid_watched_on",
        ) from error


def _optional_positive_integer(value, label, field, index):
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        normalized = None
    elif isinstance(value, int):
        normalized = value
    elif isinstance(value, str) and value.strip().isdigit():
        normalized = int(value.strip())
    else:
        normalized = None
    if normalized is None:
        raise WatchHistoryValidationError(
            f"{_record_prefix(index)}的{label}必须是正整数。",
            code=f"invalid_{field}",
        )
    if normalized <= 0 or normalized > MAX_WATCH_HISTORY_INTEGER:
        raise WatchHistoryValidationError(
            f"{_record_prefix(index)}的{label}必须是 1 到 {MAX_WATCH_HISTORY_INTEGER}。",
            code=f"invalid_{field}",
        )
    return normalized


def normalize_watch_history_record(raw_record, *, index=0):
    if not isinstance(raw_record, dict):
        raise WatchHistoryValidationError(f"{_record_prefix(index)}格式无效。")

    watched_on = _normalize_date(raw_record.get("watched_on"), index)
    brush_number = _optional_positive_integer(
        raw_record.get("brush_number"), "刷次", "brush_number", index
    )
    episode_start = _optional_positive_integer(
        raw_record.get("episode_start"), "起始话数", "episode_start", index
    )
    episode_end = _optional_positive_integer(
        raw_record.get("episode_end"), "结束话数", "episode_end", index
    )
    if episode_start is not None and episode_end is not None and episode_end < episode_start:
        raise WatchHistoryValidationError(
            f"{_record_prefix(index)}的结束话数不能小于起始话数。",
            code="invalid_episode_range",
        )

    brush_label = str(raw_record.get("brush_label") or "首刷").strip()[:20] or "首刷"
    watched_label = str(raw_record.get("watched_label") or "").strip()[:80]
    if not watched_label:
        watched_label = f"{watched_on.year}年{watched_on.month}月{watched_on.day}日"

    raw_notes = raw_record.get("notes", [])
    if isinstance(raw_notes, str):
        raw_notes = [raw_notes]
    if not isinstance(raw_notes, list) or any(not isinstance(note, str) for note in raw_notes):
        raise WatchHistoryValidationError(
            f"{_record_prefix(index)}的备注必须是字符串数组。",
            code="invalid_notes",
        )
    notes = [
        note.strip()[:MAX_WATCH_HISTORY_NOTE_LENGTH]
        for note in raw_notes
        if note.strip()
    ][:MAX_WATCH_HISTORY_NOTES]

    return {
        "watched_on": watched_on.isoformat(),
        "watched_label": watched_label,
        "brush_number": brush_number,
        "brush_label": brush_label,
        "episode_start": episode_start,
        "episode_end": episode_end,
        "notes": notes,
    }


def watch_history_semantic_key(record):
    watched_on = record.get("watched_on")
    if isinstance(watched_on, date):
        watched_on = watched_on.isoformat()
    return (
        watched_on,
        record.get("brush_label"),
        record.get("episode_start"),
        record.get("episode_end"),
    )


def normalize_watch_history_records(records):
    if not isinstance(records, list):
        raise WatchHistoryValidationError("观看记录必须是数组。")
    if len(records) > MAX_WATCH_HISTORY_RECORDS:
        raise WatchHistoryValidationError(
            f"单部番剧最多保存 {MAX_WATCH_HISTORY_RECORDS} 条观看记录。"
        )
    normalized_by_key = {}
    for index, raw_record in enumerate(records):
        normalized = normalize_watch_history_record(raw_record, index=index)
        normalized_by_key[watch_history_semantic_key(normalized)] = normalized
    return list(normalized_by_key.values())


def preserve_watch_history_metadata(existing_records, normalized_records):
    existing_by_key = {}
    if isinstance(existing_records, list):
        for item in existing_records:
            if not isinstance(item, dict):
                continue
            try:
                existing_by_key[watch_history_semantic_key(item)] = item
            except TypeError:
                continue
    return [
        {**existing_by_key.get(watch_history_semantic_key(record), {}), **record}
        for record in normalized_records
    ]


def merge_watch_history_records(existing_records, incoming_records):
    normalized = normalize_watch_history_records(incoming_records)
    merged = list(existing_records) if isinstance(existing_records, list) else []
    keys = set()
    for item in merged:
        if not isinstance(item, dict):
            continue
        try:
            keys.add(watch_history_semantic_key(item))
        except TypeError:
            continue

    created = 0
    skipped = 0
    for record in normalized:
        key = watch_history_semantic_key(record)
        if key in keys:
            skipped += 1
            continue
        merged.append(record)
        keys.add(key)
        created += 1
    return merged[-MAX_WATCH_HISTORY_RECORDS:], created, skipped
