from __future__ import annotations

import hashlib
import json
from datetime import date, datetime


MAX_WATCH_HISTORY_RECORDS = 500
MAX_WATCH_HISTORY_INTEGER = 32767
MAX_WATCH_HISTORY_NOTES = 20
MAX_WATCH_HISTORY_NOTE_LENGTH = 500
MAX_WATCH_HISTORY_METADATA_BYTES = 4096
CORE_FIELDS = {
    "id",
    "watched_on",
    "watched_label",
    "brush_number",
    "brush_label",
    "episode_start",
    "episode_end",
    "notes",
    "metadata",
    "sequence",
    "semantic_key",
    "created_at",
    "updated_at",
}


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
    if normalized is None or not 1 <= normalized <= MAX_WATCH_HISTORY_INTEGER:
        raise WatchHistoryValidationError(
            f"{_record_prefix(index)}的{label}必须是 1 到 {MAX_WATCH_HISTORY_INTEGER}。",
            code=f"invalid_{field}",
        )
    return normalized


def _bounded_text(value, *, maximum, label, index, default=""):
    normalized = str(value or default).strip()
    if not normalized:
        normalized = default
    if len(normalized) > maximum:
        raise WatchHistoryValidationError(
            f"{_record_prefix(index)}的{label}不能超过 {maximum} 个字符。"
        )
    return normalized


def _normalize_metadata(raw_record, index):
    explicit = raw_record.get("metadata") or {}
    if not isinstance(explicit, dict):
        raise WatchHistoryValidationError(f"{_record_prefix(index)}的 metadata 必须是对象。")
    extra = {key: value for key, value in raw_record.items() if key not in CORE_FIELDS}
    metadata = {**extra, **explicit}
    try:
        encoded = json.dumps(metadata, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise WatchHistoryValidationError(f"{_record_prefix(index)}的 metadata 不是有效 JSON。") from error
    if len(encoded) > MAX_WATCH_HISTORY_METADATA_BYTES:
        raise WatchHistoryValidationError(
            f"{_record_prefix(index)}的 metadata 不能超过 {MAX_WATCH_HISTORY_METADATA_BYTES} 字节。"
        )
    return metadata


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

    brush_label = _bounded_text(
        raw_record.get("brush_label"), maximum=20, label="刷次标签", index=index, default="首刷"
    )
    watched_label = _bounded_text(
        raw_record.get("watched_label"), maximum=80, label="观看日期标签", index=index
    )
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
    notes = [note.strip() for note in raw_notes if note.strip()]
    if len(notes) > MAX_WATCH_HISTORY_NOTES or any(
        len(note) > MAX_WATCH_HISTORY_NOTE_LENGTH for note in notes
    ):
        raise WatchHistoryValidationError(
            f"{_record_prefix(index)}最多包含 {MAX_WATCH_HISTORY_NOTES} 条备注，单条不超过 {MAX_WATCH_HISTORY_NOTE_LENGTH} 字。",
            code="invalid_notes",
        )

    return {
        "watched_on": watched_on.isoformat(),
        "watched_label": watched_label,
        "brush_number": brush_number,
        "brush_label": brush_label,
        "episode_start": episode_start,
        "episode_end": episode_end,
        "notes": notes,
        "metadata": _normalize_metadata(raw_record, index),
    }


def semantic_identity(record):
    watched_on = record.get("watched_on")
    if isinstance(watched_on, date):
        watched_on = watched_on.isoformat()
    return (
        str(watched_on or ""),
        str(record.get("brush_label") or ""),
        record.get("episode_start"),
        record.get("episode_end"),
    )


def semantic_digest(record):
    return semantic_digest_from_values(*semantic_identity(record))


def semantic_digest_from_values(watched_on, brush_label, episode_start, episode_end):
    if isinstance(watched_on, date):
        watched_on = watched_on.isoformat()
    canonical = json.dumps(
        [str(watched_on or ""), str(brush_label or ""), episode_start, episode_end],
        ensure_ascii=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


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
        normalized_by_key[semantic_digest(normalized)] = normalized
    return list(normalized_by_key.values())


def watch_history_semantic_key(record):
    return semantic_identity(record)


def preserve_watch_history_metadata(existing_records, normalized_records):
    existing_by_key = {
        semantic_digest(item): item
        for item in existing_records or []
        if isinstance(item, dict)
    }
    return [
        {**existing_by_key.get(semantic_digest(record), {}), **record}
        for record in normalized_records
    ]


def merge_watch_history_records(existing_records, incoming_records):
    normalized = normalize_watch_history_records(incoming_records)
    merged = list(existing_records) if isinstance(existing_records, list) else []
    keys = {semantic_digest(item) for item in merged if isinstance(item, dict)}
    created = 0
    skipped = 0
    for record in normalized:
        key = semantic_digest(record)
        if key in keys:
            skipped += 1
            continue
        merged.append(record)
        keys.add(key)
        created += 1
    return merged[-MAX_WATCH_HISTORY_RECORDS:], created, skipped
