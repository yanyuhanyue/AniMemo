from __future__ import annotations

from django.db import IntegrityError, transaction
from django.db.models import Max

from journal.models import JournalEntry, WatchHistoryRecord

from .validation import (
    MAX_WATCH_HISTORY_RECORDS,
    WatchHistoryValidationError,
    normalize_watch_history_record,
    normalize_watch_history_records,
    semantic_digest,
)


def list_history(*, user, entry, offset=0, limit=None, newest_first=False):
    _assert_owner(user, entry)
    queryset = entry.watch_history_records.all()
    if newest_first:
        queryset = queryset.order_by("-watched_on", "-sequence", "-id")
    if offset:
        queryset = queryset[offset:]
    if limit is not None:
        queryset = queryset[:limit]
    return list(queryset)


def add_history(*, user, entry, record):
    normalized = normalize_watch_history_record(record)
    with transaction.atomic():
        locked = _lock_entry(user, entry.pk)
        key = semantic_digest(normalized)
        existing = locked.watch_history_records.filter(semantic_key=key).first()
        if existing is not None:
            return existing, False
        if locked.watch_history_records.count() >= MAX_WATCH_HISTORY_RECORDS:
            raise WatchHistoryValidationError(
                f"单部番剧最多保存 {MAX_WATCH_HISTORY_RECORDS} 条观看记录。"
            )
        sequence = (locked.watch_history_records.aggregate(value=Max("sequence"))["value"] or 0) + 1
        try:
            with transaction.atomic():
                created = WatchHistoryRecord.objects.create(
                    entry=locked,
                    sequence=sequence,
                    semantic_key=key,
                    **_model_values(normalized),
                )
        except IntegrityError:
            existing = locked.watch_history_records.get(semantic_key=key)
            return existing, False
        return created, True


def update_history(*, user, entry, record_id, record):
    normalized = normalize_watch_history_record(record)
    with transaction.atomic():
        locked = _lock_entry(user, entry.pk)
        current = locked.watch_history_records.select_for_update().filter(pk=record_id).first()
        if current is None:
            raise WatchHistoryRecord.DoesNotExist
        for field, value in _model_values(normalized).items():
            setattr(current, field, value)
        current.semantic_key = semantic_digest(normalized)
        try:
            current.save()
        except IntegrityError as error:
            raise WatchHistoryValidationError(
                "相同日期、刷次和话数范围的观看记录已经存在。",
                code="duplicate_watch_history",
            ) from error
        return current


def delete_history(*, user, entry, record_id):
    with transaction.atomic():
        locked = _lock_entry(user, entry.pk)
        deleted, _ = locked.watch_history_records.filter(pk=record_id).delete()
        if not deleted:
            raise WatchHistoryRecord.DoesNotExist


def replace_history(*, user, entry, records):
    normalized = normalize_watch_history_records(records)
    with transaction.atomic():
        locked = _lock_entry(user, entry.pk)
        locked.watch_history_records.all().delete()
        created = [
            WatchHistoryRecord(
                entry=locked,
                sequence=index + 1,
                semantic_key=semantic_digest(record),
                **_model_values(record),
            )
            for index, record in enumerate(normalized)
        ]
        WatchHistoryRecord.objects.bulk_create(created)
        return list(locked.watch_history_records.all())


def merge_history(*, user, entry, records):
    normalized = normalize_watch_history_records(records)
    with transaction.atomic():
        locked = _lock_entry(user, entry.pk)
        existing_records = list(locked.watch_history_records.all())
        existing_by_key = {record.semantic_key: record for record in existing_records}
        incoming = [
            (record, semantic_digest(record))
            for record in normalized
        ]
        new_count = sum(key not in existing_by_key for _record, key in incoming)
        if len(existing_records) + new_count > MAX_WATCH_HISTORY_RECORDS:
            raise WatchHistoryValidationError(
                f"单部番剧最多保存 {MAX_WATCH_HISTORY_RECORDS} 条观看记录。"
            )

        sequence = max((record.sequence for record in existing_records), default=0)
        created = 0
        skipped = 0
        results = []
        for record, key in incoming:
            existing = existing_by_key.get(key)
            if existing is not None:
                results.append(existing)
                skipped += 1
                continue
            sequence += 1
            created_record = WatchHistoryRecord.objects.create(
                entry=locked,
                sequence=sequence,
                semantic_key=key,
                **_model_values(record),
            )
            existing_by_key[key] = created_record
            results.append(created_record)
            created += 1
        return results, created, skipped


def _model_values(record):
    return {
        "watched_on": record["watched_on"],
        "watched_label": record["watched_label"],
        "brush_number": record["brush_number"],
        "brush_label": record["brush_label"],
        "episode_start": record["episode_start"],
        "episode_end": record["episode_end"],
        "notes": record["notes"],
        "metadata": record.get("metadata") or {},
    }


def _assert_owner(user, entry):
    if entry.user_id != getattr(user, "pk", None) or entry.deleted_at is not None:
        raise JournalEntry.DoesNotExist


def _lock_entry(user, entry_id):
    return JournalEntry.objects.select_for_update().get(
        pk=entry_id,
        user=user,
        deleted_at__isnull=True,
    )
