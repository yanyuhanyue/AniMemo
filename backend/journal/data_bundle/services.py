from __future__ import annotations

from django.db import transaction
from django.utils import timezone

from journal.models import ExternalMediaIdentity, JournalEntry
from journal.watch_history import WatchHistoryValidationError, replace_history

from .serializers import DataBundleSerializer


DATA_BUNDLE_FORMAT = "animemo-data-bundle"
DATA_BUNDLE_SCHEMA_VERSION = 1


class DataBundleError(ValueError):
    def __init__(self, code, detail, *, errors=None):
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.errors = errors


def _iso(value):
    return value.isoformat() if value is not None else None


def _entry_data(entry):
    return {
        "title": entry.title,
        "japanese_title": entry.japanese_title,
        "airing_period": entry.airing_period,
        "studio": entry.studio,
        "episodes": entry.episodes,
        "description": entry.description,
        "poster_url": entry.poster_url,
        "custom_poster_url": entry.custom_poster_url,
        "baike_url": entry.baike_url,
        "tags": entry.tags,
        "tag_colors": entry.tag_colors,
        "personal_score": str(entry.personal_score) if entry.personal_score is not None else None,
        "watch_status": entry.watch_status,
        "review": entry.review,
        "visibility": entry.visibility,
    }


def _identity_data(identity):
    return {
        "provider": identity.provider,
        "external_id": identity.external_id,
        "canonical_url": identity.canonical_url,
        "metadata": identity.metadata,
        "metadata_schema_version": identity.metadata_schema_version,
        "is_metadata_source": identity.is_metadata_source,
        "metadata_fetched_at": _iso(identity.metadata_fetched_at),
        "provider_updated_at": _iso(identity.provider_updated_at),
    }


def _watch_history_data(record):
    return {
        "watched_on": record.watched_on.isoformat(),
        "watched_label": record.watched_label,
        "brush_number": record.brush_number,
        "brush_label": record.brush_label,
        "episode_start": record.episode_start,
        "episode_end": record.episode_end,
        "notes": record.notes,
        "metadata": record.metadata,
    }


def export_data_bundle(*, user):
    entries = (
        JournalEntry.objects.filter(user=user, deleted_at__isnull=True)
        .prefetch_related("external_identities", "watch_history_records")
        .order_by("created_at", "id")
    )
    return {
        "format": DATA_BUNDLE_FORMAT,
        "schema_version": DATA_BUNDLE_SCHEMA_VERSION,
        "exported_at": timezone.now().isoformat(),
        "entries": [
            {
                "entry": _entry_data(entry),
                "external_identities": [_identity_data(item) for item in entry.external_identities.all()],
                "watch_history": [_watch_history_data(item) for item in entry.watch_history_records.all()],
            }
            for entry in entries
        ],
    }


def _validate_bundle(payload):
    if not isinstance(payload, dict):
        raise DataBundleError("unsupported_import_schema", "仅支持 AniMemo Data Bundle v1。")
    if payload.get("format") != DATA_BUNDLE_FORMAT or payload.get("schema_version") != DATA_BUNDLE_SCHEMA_VERSION:
        raise DataBundleError("unsupported_import_schema", "仅支持 AniMemo Data Bundle v1。")
    serializer = DataBundleSerializer(data=payload)
    if not serializer.is_valid():
        raise DataBundleError("invalid_data_bundle", "Data Bundle 内容无效。", errors=serializer.errors)
    entries = serializer.validated_data["entries"]
    seen_identities = set()
    for item in entries:
        for identity in item["external_identities"]:
            key = (identity["provider"], identity["external_id"])
            if key in seen_identities:
                raise DataBundleError(
                    "invalid_data_bundle",
                    "同一用户不能把同一个外部身份恢复到多个条目。",
                )
            seen_identities.add(key)
    try:
        for item in entries:
            item["watch_history"] = _normalize_history(item["watch_history"])
    except WatchHistoryValidationError as error:
        raise DataBundleError(error.code, error.detail) from error
    return entries


def _normalize_history(records):
    from journal.watch_history import normalize_watch_history_records

    return normalize_watch_history_records(records)


def preview_data_bundle(*, user, payload):
    entries = _validate_bundle(payload)
    journal_empty = not JournalEntry.objects.filter(user=user, deleted_at__isnull=True).exists()
    items = [
        {
            "row": index,
            "title": item["entry"]["title"],
            "status": "ready" if journal_empty else "invalid",
            "reason": "等待恢复" if journal_empty else "Data Bundle 只能恢复到空手账",
        }
        for index, item in enumerate(entries, start=1)
    ]
    return {
        "format": DATA_BUNDLE_FORMAT,
        "schema_version": DATA_BUNDLE_SCHEMA_VERSION,
        "total": len(entries),
        "ready": len(entries) if journal_empty else 0,
        "skipped_duplicates": 0,
        "errors": [] if journal_empty else [{"code": "bundle_import_requires_empty_journal"}],
        "items": items,
    }


def import_data_bundle(*, user, payload):
    entries = _validate_bundle(payload)
    with transaction.atomic():
        from journal.external_media.services import lock_identity_owner

        locked_user = lock_identity_owner(user)
        if JournalEntry.objects.filter(user=locked_user, deleted_at__isnull=True).exists():
            raise DataBundleError(
                "bundle_import_requires_empty_journal",
                "Data Bundle 只能恢复到空手账；已有数据请先使用 CSV 有损导入。",
            )
        created = 0
        for item in entries:
            entry = JournalEntry.objects.create(user=locked_user, **item["entry"])
            ExternalMediaIdentity.objects.bulk_create([
                ExternalMediaIdentity(entry=entry, **identity)
                for identity in item["external_identities"]
            ])
            replace_history(user=user, entry=entry, records=item["watch_history"])
            created += 1
    return {
        "format": DATA_BUNDLE_FORMAT,
        "schema_version": DATA_BUNDLE_SCHEMA_VERSION,
        "created": created,
        "total": len(entries),
        "skipped_duplicates": 0,
        "errors": [],
    }
