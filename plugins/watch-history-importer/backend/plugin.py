from __future__ import annotations

import json
import re
from datetime import date
from urllib.parse import quote
from uuid import uuid4

from config.api_errors import public_failure
from django.conf import settings
from django.db import transaction
from django.utils import timezone
from plugin_host.sdk import HostCapabilityError, PluginStorageLimitError
from rest_framework import status
from rest_framework.response import Response

from .src.animemo_watch_history_importer.bangumi import (
    BangumiError,
    resolve_group,
    resolve_subject_id,
)
from .src.animemo_watch_history_importer.parser import build_preview, normalize_title

PLUGIN_SLUG = "watch-history-importer"
PLUGIN_VERSION = "0.4.5"
MAX_FILES = 8
MAX_FILE_BYTES = 2 * 1024 * 1024
INTEGRATION_TEXT_MAX_BYTES = 120 * 1024
DATE_TAG_RE = re.compile(r"^\d{4}年\d{1,2}月(?:\d{1,2}(?:日|号)?)?(?:\s*(?:-|–|—|至)\s*(?:\d{1,2}月)?\d{1,2}(?:日|号)?)?$")
BRUSH_TAG_RE = re.compile(r"^(?:首|二|三|四|五|六|七|八|九|十|\d+|X)刷$", re.IGNORECASE)
YEAR_TAG_RE = re.compile(r"^\d{4}年?$")
_BATCH_ERROR_CODE = "bangumi_lookup_unavailable"
_MAX_PUBLIC_INTEGER = 2_147_483_647
_MAX_PUBLIC_COLLECTION_ITEMS = 10_000
_MAX_SOURCE_NAMES = MAX_FILES
_MISSING = object()
_BATCH_STATUSES = frozenset({"preview", "resolving", "ready", "imported"})
_BATCH_SUMMARY_FIELDS = frozenset(
    {
        "parsed",
        "included_records",
        "anime_groups",
        "excluded",
        "pending",
        "matched",
        "manual_review",
        "imported_entries",
        "imported_history_records",
        "selected_groups",
        "excluded_groups",
        "excluded_group_indices",
    }
)
_RESOLUTION_STATUSES = frozenset(
    {
        "pending",
        "matched",
        "season_mismatch",
        "no_result",
        "low_confidence",
        "ambiguous",
        "episode_mismatch",
        "network_error",
    }
)
_GROUP_TEXT_FIELDS = {
    "source_key": 4096,
    "source_title": 4096,
    "latest_watch_date": 32,
    "latest_watch_date_label": 128,
}
_RECORD_TEXT_FIELDS = {
    "title": 4096,
    "source_title": 4096,
    "watch_date": 32,
    "watch_date_label": 128,
    "brush_label": 32,
    "episode_claim_kind": 32,
    "exclusion_reason": 1000,
    "source_file": 255,
}
_RECORD_NULLABLE_TEXT_FIELDS = frozenset(
    {
        "watch_date",
        "watch_date_label",
        "episode_claim_kind",
        "exclusion_reason",
    }
)
_RESOLUTION_TEXT_FIELDS = {
    "title": 4096,
    "japanese_title": 4096,
    "air_date": 32,
    "studio": 2000,
    "description": 10_000,
    "poster_url": 1000,
    "source_url": 1000,
    "source_title": 4096,
}


def _host_failure_payload(request, error):
    if error.code == "invalid_import_batch":
        candidate_code = "invalid_import_batch"
        status_code = status.HTTP_400_BAD_REQUEST
    elif error.code == "batch_too_large":
        candidate_code = "batch_too_large"
        status_code = status.HTTP_413_REQUEST_ENTITY_TOO_LARGE
    elif error.code == "invalid_excluded_group_indices":
        candidate_code = "invalid_excluded_group_indices"
        status_code = status.HTTP_400_BAD_REQUEST
    elif error.code == "empty_import_selection":
        candidate_code = "empty_import_selection"
        status_code = status.HTTP_400_BAD_REQUEST
    elif error.code == "resolution_pending":
        candidate_code = "resolution_pending"
        status_code = status.HTTP_409_CONFLICT
    elif error.code == "manual_review_required":
        candidate_code = "manual_review_required"
        status_code = status.HTTP_409_CONFLICT
    elif error.code == "batch_missing":
        candidate_code = "batch_missing"
        status_code = status.HTTP_404_NOT_FOUND
    elif error.code == "batch_owner_mismatch":
        candidate_code = "batch_owner_mismatch"
        status_code = status.HTTP_404_NOT_FOUND
    elif error.code == "invalid_watch_history":
        candidate_code = "invalid_watch_history"
        status_code = status.HTTP_400_BAD_REQUEST
    elif error.code == "invalid_watched_on":
        candidate_code = "invalid_watched_on"
        status_code = status.HTTP_400_BAD_REQUEST
    elif error.code == "invalid_brush_number":
        candidate_code = "invalid_brush_number"
        status_code = status.HTTP_400_BAD_REQUEST
    elif error.code == "invalid_episode_start":
        candidate_code = "invalid_episode_start"
        status_code = status.HTTP_400_BAD_REQUEST
    elif error.code == "invalid_episode_end":
        candidate_code = "invalid_episode_end"
        status_code = status.HTTP_400_BAD_REQUEST
    elif error.code == "invalid_episode_range":
        candidate_code = "invalid_episode_range"
        status_code = status.HTTP_400_BAD_REQUEST
    elif error.code == "invalid_notes":
        candidate_code = "invalid_notes"
        status_code = status.HTTP_400_BAD_REQUEST
    elif error.code == "duplicate_watch_history":
        candidate_code = "duplicate_watch_history"
        status_code = status.HTTP_409_CONFLICT
    elif error.code == "invalid_entry":
        candidate_code = "invalid_entry"
        status_code = status.HTTP_400_BAD_REQUEST
    elif error.code == "invalid_limit":
        candidate_code = "invalid_limit"
        status_code = status.HTTP_400_BAD_REQUEST
    elif error.code == "entry_not_found":
        candidate_code = "entry_not_found"
        status_code = status.HTTP_404_NOT_FOUND
    elif error.code == "owner_required":
        candidate_code = "owner_required"
        status_code = status.HTTP_403_FORBIDDEN
    elif error.code == "plugin_context_forbidden":
        candidate_code = "plugin_context_forbidden"
        status_code = status.HTTP_403_FORBIDDEN
    elif error.code == "plugin_disabled":
        candidate_code = "plugin_disabled"
        status_code = status.HTTP_403_FORBIDDEN
    elif error.code == "capability_not_declared":
        candidate_code = "capability_not_declared"
        status_code = status.HTTP_403_FORBIDDEN
    else:
        candidate_code = "plugin_operation_failed"
        status_code = status.HTTP_400_BAD_REQUEST
    return public_failure(
        request=request,
        candidate_code=candidate_code,
        status_code=status_code,
    ), status_code


def _host_failure_response(request, error):
    payload, status_code = _host_failure_payload(request, error)
    return Response(payload, status=status_code)


def _store(host, user, namespace):
    return host.storage(user=user, namespace=namespace)


def _config(host, user):
    return {"resolve_batch_size": 6, "unmatched_policy": "review", **host.user_settings(user)}


def _batch_key(batch_id):
    return str(batch_id)


def _import_history_records(records, history):
    if not isinstance(records, list) or any(not isinstance(record, dict) for record in records):
        raise HostCapabilityError("invalid_watch_history", "导入批次中的观看记录格式无效。")
    return history.normalize([
        {
            "watched_on": record.get("watch_date"),
            "watched_label": record.get("watch_date_label"),
            "brush_number": record.get("brush"),
            "brush_label": record.get("brush_label"),
            "episode_start": (record.get("episode_range") or {}).get("start"),
            "episode_end": (record.get("episode_range") or {}).get("end"),
            "notes": record.get("notes", []),
        }
        for record in records
    ])


def _batch_max_bytes():
    return int(getattr(settings, "WATCH_HISTORY_IMPORT_BATCH_MAX_BYTES", 40 * 1024 * 1024))


def _batch_max_per_user():
    return int(getattr(settings, "WATCH_HISTORY_IMPORT_BATCH_MAX_PER_USER", 4))


def _batch_retention_seconds():
    return int(getattr(settings, "WATCH_HISTORY_IMPORT_BATCH_RETENTION_SECONDS", 604800))


def _total_upload_max_bytes():
    return int(getattr(settings, "WATCH_HISTORY_IMPORT_TOTAL_UPLOAD_MAX_BYTES", 4 * 1024 * 1024))


def _batch_size(batch):
    try:
        return len(
            json.dumps(batch, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        )
    except (TypeError, ValueError) as error:
        raise HostCapabilityError(
            "invalid_import_batch",
            "导入批次包含无法保存的数据。",
        ) from error


def _validate_batch_size(batch):
    maximum = _batch_max_bytes()
    if _batch_size(batch) > maximum:
        raise HostCapabilityError(
            "batch_too_large",
            f"导入批次超过 {maximum // (1024 * 1024)} MiB 上限。",
            413,
        )


def _get_batch(host, user, batch_id):
    batch = _store(host, user, "batches").get(_batch_key(batch_id))
    if not isinstance(batch, dict):
        return None
    return batch


def _save_batch(host, user, batch):
    _validate_batch_size(batch)
    try:
        _store(host, user, "batches").set_bounded(
            batch["id"],
            batch,
            max_value_bytes=_batch_max_bytes(),
            max_rows=_batch_max_per_user(),
            retention_seconds=_batch_retention_seconds(),
        )
    except PluginStorageLimitError as error:
        raise HostCapabilityError(
            "batch_too_large",
            f"导入批次超过 {_batch_max_bytes() // (1024 * 1024)} MiB 上限。",
            413,
        ) from error
    return batch


def _selected_groups(host, user, batch, raw_excluded):
    groups = batch.get("payload", {}).get("groups", [])
    if not isinstance(raw_excluded, list) or any(
        isinstance(value, bool) or not isinstance(value, int)
        for value in raw_excluded
    ):
        raise HostCapabilityError(
            "invalid_excluded_group_indices",
            "排除项必须是番剧分组索引列表。",
        )
    if any(value < 0 or value >= len(groups) for value in raw_excluded):
        raise HostCapabilityError(
            "invalid_excluded_group_indices",
            "排除项包含不存在的番剧分组。",
        )
    excluded = set(raw_excluded)
    selected = [group for index, group in enumerate(groups) if index not in excluded]
    if not selected:
        raise HostCapabilityError(
            "empty_import_selection",
            "至少保留一部番剧后才能正式导入。",
        )
    if any(
        group.get("resolution", {}).get("status") == "pending"
        for group in selected
    ):
        raise HostCapabilityError(
            "resolution_pending",
            "仍有番剧尚未完成 Bangumi 匹配。",
            409,
        )
    unresolved = [
        group
        for group in selected
        if group.get("resolution", {}).get("status") != "matched"
    ]
    if unresolved and _config(host, user).get("unmatched_policy", "review") == "review":
        raise HostCapabilityError(
            "manual_review_required",
            f"仍有 {len(unresolved)} 部番剧需要人工选择 Bangumi 条目。",
            409,
        )
    return selected, excluded


def _stored_import_result(batch):
    result = batch.get("result")
    if isinstance(result, dict):
        return result
    summary = batch.get("summary") or {}
    excluded = int(summary.get("excluded_groups") or 0)
    return {
        "batch_id": batch["id"],
        "imported_entries": int(summary.get("imported_entries") or 0),
        "imported_records": int(summary.get("imported_history_records") or 0),
        "excluded_groups": excluded,
        "created": 0,
        "updated": 0,
        "skipped": excluded,
    }


def _summary(payload):
    groups = payload.get("groups", [])
    statuses = [group.get("resolution", {}).get("status", "pending") for group in groups]
    return {
        **payload.get("summary", {}),
        "pending": statuses.count("pending"),
        "matched": statuses.count("matched"),
        "manual_review": sum(value not in {"pending", "matched"} for value in statuses),
    }


def _public_batch_error(value, request):
    if not value:
        return {}
    candidate = value.get("code") if isinstance(value, dict) else None
    if candidate != _BATCH_ERROR_CODE:
        candidate = _BATCH_ERROR_CODE
    return public_failure(
        request=request,
        candidate_code=candidate,
        status_code=status.HTTP_200_OK,
    )


def _stable_text(value, maximum_length, *, nullable=False):
    if value is None and nullable:
        return None
    if not isinstance(value, str) or len(value) > maximum_length:
        return _MISSING
    return value


def _stable_integer(value, *, minimum=0, maximum=_MAX_PUBLIC_INTEGER, nullable=False):
    if value is None and nullable:
        return None
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < minimum
        or value > maximum
    ):
        return _MISSING
    return value


def _stable_string_list(value, *, maximum_items, maximum_length):
    if not isinstance(value, list) or len(value) > maximum_items:
        return _MISSING
    projected = []
    for item in value:
        stable = _stable_text(item, maximum_length)
        if stable is _MISSING:
            return _MISSING
        projected.append(stable)
    return projected


def _stable_integer_list(
    value,
    *,
    maximum_items,
    minimum=0,
    maximum=_MAX_PUBLIC_INTEGER,
    ordered_unique=False,
):
    if not isinstance(value, list) or len(value) > maximum_items:
        return _MISSING
    projected = []
    for item in value:
        stable = _stable_integer(item, minimum=minimum, maximum=maximum)
        if stable is _MISSING:
            return _MISSING
        projected.append(stable)
    if ordered_unique and projected != sorted(set(projected)):
        return _MISSING
    return projected


def _stable_episode_range(value):
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {"start", "end"}:
        return _MISSING
    start = _stable_integer(value.get("start"), minimum=1, maximum=32767)
    end = _stable_integer(value.get("end"), minimum=1, maximum=32767)
    if start is _MISSING or end is _MISSING or end < start:
        return _MISSING
    return {"start": start, "end": end}


def _public_text_fields(value, fields, *, nullable_fields=frozenset()):
    if not isinstance(value, dict):
        return {}
    projected = {}
    for key, maximum_length in fields.items():
        if key not in value:
            continue
        stable = _stable_text(
            value[key],
            maximum_length,
            nullable=key in nullable_fields,
        )
        if stable is not _MISSING:
            projected[key] = stable
    return projected


def _public_record(value):
    if not isinstance(value, dict):
        return {}
    projected = _public_text_fields(
        value,
        _RECORD_TEXT_FIELDS,
        nullable_fields=_RECORD_NULLABLE_TEXT_FIELDS,
    )
    for key, minimum, maximum, nullable in (
        ("brush", 1, 32767, True),
        ("claimed_episodes", 1, 32767, True),
        ("source_line", 1, _MAX_PUBLIC_INTEGER, False),
    ):
        if key not in value:
            continue
        stable = _stable_integer(
            value[key],
            minimum=minimum,
            maximum=maximum,
            nullable=nullable,
        )
        if stable is not _MISSING:
            projected[key] = stable
    include = value.get("include", _MISSING)
    if isinstance(include, bool):
        projected["include"] = include
    if "notes" in value:
        notes = _stable_string_list(
            value["notes"],
            maximum_items=256,
            maximum_length=2000,
        )
        if notes is not _MISSING:
            projected["notes"] = notes
    if "episode_range" in value:
        episode_range = _stable_episode_range(value["episode_range"])
        if episode_range is not _MISSING:
            projected["episode_range"] = episode_range
    return projected


def _public_resolution(value, request):
    resolution = value if isinstance(value, dict) else {}
    candidate_status = resolution.get("status")
    resolution_status = (
        candidate_status
        if isinstance(candidate_status, str) and candidate_status in _RESOLUTION_STATUSES
        else "network_error"
    )
    if resolution_status == "network_error":
        return {
            "status": "network_error",
            **_public_batch_error(resolution or True, request),
        }
    projected = _public_text_fields(resolution, _RESOLUTION_TEXT_FIELDS)
    for key, minimum, maximum, nullable in (
        ("bangumi_id", 1, _MAX_PUBLIC_INTEGER, False),
        ("episodes", 0, 32767, True),
    ):
        if key not in resolution:
            continue
        stable = _stable_integer(
            resolution[key],
            minimum=minimum,
            maximum=maximum,
            nullable=nullable,
        )
        if stable is not _MISSING:
            projected[key] = stable
    for key in ("episode_exception", "manual_selection"):
        field = resolution.get(key, _MISSING)
        if isinstance(field, bool):
            projected[key] = field
    confidence = resolution.get("confidence", _MISSING)
    if (
        not isinstance(confidence, bool)
        and isinstance(confidence, (int, float))
        and 0 <= confidence <= 1
    ):
        projected["confidence"] = confidence
    if "tags" in resolution:
        tags = _stable_string_list(
            resolution["tags"],
            maximum_items=8,
            maximum_length=200,
        )
        if tags is not _MISSING:
            projected["tags"] = tags
    if "claimed_episodes" in resolution:
        claimed_episodes = _stable_integer_list(
            resolution["claimed_episodes"],
            maximum_items=512,
            minimum=1,
            maximum=32767,
            ordered_unique=True,
        )
        if claimed_episodes is not _MISSING:
            projected["claimed_episodes"] = claimed_episodes
    projected["status"] = resolution_status
    return projected


def _public_group(value, request):
    group = value if isinstance(value, dict) else {}
    projected = _public_text_fields(group, _GROUP_TEXT_FIELDS)
    records = group.get("records")
    projected["records"] = [
        _public_record(record)
        for record in records
    ] if isinstance(records, list) and len(records) <= _MAX_PUBLIC_COLLECTION_ITEMS else []
    projected["resolution"] = _public_resolution(group.get("resolution"), request)
    return projected


def _public_summary(value):
    summary = value if isinstance(value, dict) else {}
    projected = {}
    for key in _BATCH_SUMMARY_FIELDS - {"excluded_group_indices"}:
        if key not in summary:
            continue
        stable = _stable_integer(summary[key])
        if stable is not _MISSING:
            projected[key] = stable
    if "excluded_group_indices" in summary:
        indices = _stable_integer_list(
            summary["excluded_group_indices"],
            maximum_items=_MAX_PUBLIC_COLLECTION_ITEMS,
            ordered_unique=True,
        )
        if indices is not _MISSING:
            projected["excluded_group_indices"] = indices
    return projected


def _serialize_batch(batch, request, include_groups=True):
    payload = batch.get("payload")
    if not isinstance(payload, dict):
        payload = {}
    source_names = batch.get("source_names")
    stable_source_names = _stable_string_list(
        source_names,
        maximum_items=_MAX_SOURCE_NAMES,
        maximum_length=255,
    )
    batch_id = _stable_text(batch.get("id"), 64)
    batch_status = batch.get("status")
    if not isinstance(batch_status, str) or batch_status not in _BATCH_STATUSES:
        batch_status = "preview"
    target_user_id = _stable_integer(
        batch.get("target_user_id"),
        minimum=1,
        nullable=True,
    )
    target_username = _stable_text(batch.get("target_username", ""), 150)
    target_email = _stable_text(batch.get("target_email", ""), 320)
    result = {
        "id": "" if batch_id is _MISSING else batch_id,
        "status": batch_status,
        "target_user": {
            "id": None if target_user_id is _MISSING else target_user_id,
            "username": "" if target_username is _MISSING else target_username,
            "email": "" if target_email is _MISSING else target_email,
        },
        "source_names": [] if stable_source_names is _MISSING else stable_source_names,
        "summary": _public_summary(batch.get("summary") or payload.get("summary")),
        "error": _public_batch_error(batch.get("error"), request),
    }
    for key in ("created_at", "updated_at", "imported_at"):
        timestamp = _stable_text(batch.get(key), 64, nullable=True)
        result[key] = None if timestamp is _MISSING else timestamp
    if include_groups:
        groups = payload.get("groups")
        excluded = payload.get("excluded")
        result["groups"] = [
            _public_group(group, request)
            for group in groups
        ] if isinstance(groups, list) and len(groups) <= _MAX_PUBLIC_COLLECTION_ITEMS else []
        result["excluded"] = [
            _public_record(record)
            for record in excluded
        ] if isinstance(excluded, list) and len(excluded) <= _MAX_PUBLIC_COLLECTION_ITEMS else []
    return result


def _read_uploaded_text(upload):
    if getattr(upload, "size", 0) > MAX_FILE_BYTES:
        raise ValueError(f"{upload.name} 超过 2 MB。")
    payload = upload.read()
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return payload.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ValueError(f"{upload.name} 不是可识别的文本编码。")


def _season_label(air_date):
    try:
        aired = date.fromisoformat(air_date)
    except (TypeError, ValueError):
        return ""
    return f"{aired.year}-{aired.month}"


def _metadata_tag(value):
    label = str(value or "").strip()
    return bool(label and (DATE_TAG_RE.match(label) or YEAR_TAG_RE.match(label)))


def _moegirl_url(title):
    normalized = str(title or "").strip()
    return f"https://mzh.moegirl.org.cn/{quote(normalized, safe='')}" if normalized else ""


class WatchHistoryPlugin:
    def __init__(self, host):
        self.host = host
        host.api.get("status", handler=self.status, access="user")
        host.api.post("preview", handler=self.preview, access="user")
        host.api.get("batches/<batch_id>", handler=self.batch, access="user")
        host.api.post("batches/<batch_id>/resolve-next", handler=self.resolve_next, access="user")
        host.api.post("batches/<batch_id>/select-subject", handler=self.select_subject, access="user")
        host.api.post("batches/<batch_id>/commit", handler=self.commit, access="user")
        host.integrations.register_action("history-get", self.integration_history_get)
        host.integrations.register_action("history-add", self.integration_history_add)
        host.integrations.register_action("entries-search", self.integration_entries_search)
        host.integrations.register_action("import-preview", self.integration_import_preview)
        host.integrations.register_action("import-commit", self.integration_import_commit)

    @staticmethod
    def health_check():
        return True

    def integration_history_get(self, context, payload):
        journal = self.host.journal.bind(context)
        history = self.host.watch_history.bind(context)
        entry_id = payload.get("entry_id")
        if entry_id is not None:
            try:
                entry = journal.get_entry(entry_id)
                records = history.list_history(entry["entry_id"])
            except HostCapabilityError as error:
                return _host_failure_payload(None, error)
            return {"entry_id": entry["entry_id"], "title": entry["title"], "records": records}
        try:
            rows = journal.list_entries(limit=100)
            return {
                "entries": [
                    {**row, "records": history.list_history(row["entry_id"])}
                    for row in rows
                ]
            }
        except HostCapabilityError as error:
            return _host_failure_payload(None, error)

    def integration_history_add(self, context, payload):
        try:
            entry_id = int(payload.get("entry_id"))
        except (TypeError, ValueError):
            return public_failure(
                request=None,
                candidate_code="invalid_entry_id",
                status_code=status.HTTP_400_BAD_REQUEST,
            ), status.HTTP_400_BAD_REQUEST
        journal = self.host.journal.bind(context)
        history = self.host.watch_history.bind(context)
        try:
            entry = journal.get_entry(entry_id)
            result = history.add_history(
                entry_id,
                {key: value for key, value in payload.items() if key != "entry_id"},
            )
        except HostCapabilityError as error:
            return _host_failure_payload(None, error)
        if result["created"]:
            event_payload = {
                "entry_id": entry["entry_id"],
                "title": entry["title"],
                "watched_on": result["record"]["watched_on"],
                "brush_label": result["record"]["brush_label"],
                "episode_start": result["record"]["episode_start"],
                "episode_end": result["record"]["episode_end"],
                "count": 1,
            }
            transaction.on_commit(
                lambda: self.host.integrations.emit(
                    context.user,
                    "history-updated",
                    event_payload,
                ),
                robust=True,
            )
        return {"entry_id": entry["entry_id"], **result}

    def integration_entries_search(self, context, payload):
        query = str(payload.get("query") or "").strip()[:120]
        journal = self.host.journal.bind(context)
        try:
            rows = journal.list_entries(query=query, limit=20)
        except HostCapabilityError as error:
            return _host_failure_payload(None, error)
        if query:
            needle = normalize_title(query)
            rows = [row for row in rows if needle in normalize_title(row["title"]) or needle in normalize_title(row["japanese_title"])]
        return {"entries": rows[:20]}

    def integration_import_preview(self, context, payload):
        text = payload.get("text")
        if not isinstance(text, str) or not text.strip() or len(text.encode("utf-8")) > INTEGRATION_TEXT_MAX_BYTES:
            return public_failure(
                request=None,
                candidate_code="invalid_text",
                status_code=status.HTTP_400_BAD_REQUEST,
            ), status.HTTP_400_BAD_REQUEST
        preview = build_preview([(str(payload.get("filename") or "integration.txt")[:120], text)])
        return {"summary": preview.get("summary", {}), "groups": preview.get("groups", [])}

    def integration_import_commit(self, context, payload):
        batch_id = str(payload.get("batch_id") or "").strip()
        if not batch_id:
            return public_failure(
                request=None,
                candidate_code="batch_missing",
                status_code=status.HTTP_400_BAD_REQUEST,
            ), status.HTTP_400_BAD_REQUEST
        try:
            result, _batch = self._commit_batch(
                context,
                batch_id,
                payload.get("excluded_group_indices", []),
            )
        except HostCapabilityError as error:
            return _host_failure_payload(None, error)
        return result

    def _commit_batch(self, actor, batch_id, raw_excluded):
        journal = self.host.journal.bind(actor)
        history = self.host.watch_history.bind(actor)
        user = actor.user
        with transaction.atomic():
            batch_row = (
                _store(self.host, user, "batches")
                .collection()
                .select_for_update()
                .filter(key=_batch_key(batch_id))
                .first()
            )
            if batch_row is None or not isinstance(batch_row.value, dict):
                raise HostCapabilityError(
                    "batch_missing",
                    "导入批次不存在。",
                    404,
                )
            batch = batch_row.value
            if batch.get("target_user_id") != user.pk:
                raise HostCapabilityError(
                    "batch_owner_mismatch",
                    "导入批次不属于当前账号。",
                    404,
                )
            if batch.get("status") == "imported":
                return _stored_import_result(batch), batch

            selected, excluded = _selected_groups(self.host, user, batch, raw_excluded)
            prepared = [
                (group, _import_history_records(group.get("records", []), history))
                for group in selected
                if group.get("resolution", {}).get("status") == "matched"
                and group.get("resolution", {}).get("bangumi_id")
            ]
            imported_entries = set()
            imported_records = 0
            created_entries = 0
            updated_records = 0
            skipped_records = 0
            entries = journal.list_entries(limit=500)
            by_title = {
                normalize_title(title): entry
                for entry in entries
                for title in (entry["title"], entry["japanese_title"])
                if normalize_title(title)
            }
            subject_store = _store(self.host, actor.user, "subjects")
            for group, incoming_history in prepared:
                resolution = group.get("resolution", {})
                entry = (
                    by_title.get(normalize_title(resolution.get("title")))
                    or by_title.get(normalize_title(resolution.get("japanese_title")))
                )
                records = group.get("records", [])
                if not records:
                    raise HostCapabilityError("invalid_watch_history", "导入分组缺少观看记录。")
                latest = max(records, key=lambda record: record.get("watch_date") or "")
                bangumi_tags = [
                    str(tag).strip()
                    for tag in resolution.get("tags", [])
                    if str(tag).strip() and not _metadata_tag(tag)
                ]
                existing_tags = [] if entry is None else [
                    tag
                    for tag in entry["tags"]
                    if not _metadata_tag(tag) and not BRUSH_TAG_RE.match(str(tag))
                ]
                tags = list(dict.fromkeys([
                    latest.get("watch_date_label"),
                    latest.get("brush_label"),
                    *bangumi_tags,
                    *existing_tags,
                ]))[:30]
                values = {
                    "title": resolution.get("title") or group["source_title"],
                    "japanese_title": resolution.get("japanese_title") or "",
                    "airing_period": _season_label(resolution.get("air_date")),
                    "studio": resolution.get("studio") or "",
                    "episodes": str(resolution.get("episodes") or ""),
                    "description": resolution.get("description") or "",
                    "poster_url": resolution.get("poster_url") or "",
                    "baike_url": _moegirl_url(resolution.get("title") or group["source_title"]),
                    "tags": [tag for tag in tags if tag],
                    "watch_status": "completed",
                    "visibility": "private",
                }
                if entry is None:
                    entry = journal.create_entry(values)
                    created_entries += 1
                else:
                    updates = {
                        field: value
                        for field, value in values.items()
                        if value or field in {"tags", "watch_status", "visibility"}
                    }
                    entry = journal.update_entry(entry["entry_id"], updates)
                by_title[normalize_title(entry["title"])] = entry
                imported_entries.add(entry["entry_id"])
                subject_store.set(
                    str(resolution["bangumi_id"]),
                    {"entry_id": entry["entry_id"], "batch_id": batch["id"]},
                )
                merged = history.merge_history(entry["entry_id"], incoming_history)
                imported_records += merged["created"]
                updated_records += merged["created"]
                skipped_records += merged["skipped"]
            result = {
                "batch_id": batch["id"],
                "imported_entries": len(imported_entries),
                "imported_records": imported_records,
                "excluded_groups": len(excluded),
                "created": created_entries,
                "updated": updated_records,
                "skipped": skipped_records + len(excluded),
            }
            batch["status"] = "imported"
            batch["imported_at"] = timezone.now().isoformat()
            batch["summary"] = {
                **batch.get("summary", {}),
                "imported_entries": len(imported_entries),
                "imported_history_records": imported_records,
                "selected_groups": len(selected),
                "excluded_groups": len(excluded),
                "excluded_group_indices": sorted(excluded),
            }
            batch["result"] = result
            batch["updated_at"] = timezone.now().isoformat()
            _validate_batch_size(batch)
            batch_row.value = batch
            batch_row.save(update_fields=["value", "updated_at"])
            event_payload = dict(result)
            transaction.on_commit(
                lambda: self.host.integrations.emit(
                    user,
                    "import-completed",
                    event_payload,
                ),
                robust=True,
            )
        return result, batch

    def status(self, request):
        batches = list(
            _store(self.host, request.user, "batches")
            .collection()
            .order_by("-updated_at", "-pk")
            .values("value")[: _batch_max_per_user()]
        )
        values = [item["value"] for item in batches]
        return Response({"status": "ok", "plugin": {"id": "com.animemo.watch-history-importer", "slug": PLUGIN_SLUG, "version": PLUGIN_VERSION}, "config": _config(self.host, request.user), "batches": [_serialize_batch(value, request, include_groups=False) for value in values]})

    def preview(self, request):
        files = request.FILES.getlist("files")
        if not files or len(files) > MAX_FILES:
            return Response(
                public_failure(
                    request=request,
                    candidate_code="invalid_files",
                    status_code=status.HTTP_400_BAD_REQUEST,
                ),
                status=status.HTTP_400_BAD_REQUEST,
            )
        total_bytes = sum(max(0, int(getattr(upload, "size", 0) or 0)) for upload in files)
        if total_bytes > _total_upload_max_bytes():
            return Response(
                public_failure(
                    request=request,
                    candidate_code="files_too_large",
                    status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                ),
                status=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            )
        try:
            documents = []
            for upload in files:
                if not upload.name.lower().endswith(".txt"):
                    raise ValueError(f"{upload.name} 不是 TXT 文件。")
                documents.append((upload.name, _read_uploaded_text(upload)))
        except (TypeError, ValueError):
            return Response(
                public_failure(request=request, candidate_code="invalid_import_file", status_code=status.HTTP_400_BAD_REQUEST),
                status=status.HTTP_400_BAD_REQUEST,
            )
        payload = build_preview(documents)
        batch = {
            "id": uuid4().hex,
            "status": "preview",
            "created_by_id": request.user.pk,
            "target_user_id": request.user.pk,
            "target_username": request.user.get_username(),
            "target_email": request.user.email,
            "source_names": [name for name, _content in documents],
            "payload": payload,
            "summary": payload.get("summary", {}),
            "error": {},
            "created_at": timezone.now().isoformat(),
            "updated_at": timezone.now().isoformat(),
            "imported_at": None,
        }
        try:
            _save_batch(self.host, request.user, batch)
        except HostCapabilityError as error:
            return _host_failure_response(request, error)
        return Response(_serialize_batch(batch, request), status=status.HTTP_201_CREATED)

    def batch(self, request, batch_id):
        batch = _get_batch(self.host, request.user, batch_id)
        if batch is None:
            return Response(
                public_failure(
                    request=request,
                    candidate_code="batch_missing",
                    status_code=status.HTTP_404_NOT_FOUND,
                ),
                status=status.HTTP_404_NOT_FOUND,
            )
        return Response(_serialize_batch(batch, request))

    def resolve_next(self, request, batch_id):
        batch = _get_batch(self.host, request.user, batch_id)
        if batch is None:
            return Response(
                public_failure(
                    request=request,
                    candidate_code="batch_missing",
                    status_code=status.HTTP_404_NOT_FOUND,
                ),
                status=status.HTTP_404_NOT_FOUND,
            )
        payload = batch.get("payload") or {}
        indexes = [index for index, group in enumerate(payload.get("groups", [])) if group.get("resolution", {}).get("status") == "pending"][: max(1, min(int(_config(self.host, request.user).get("resolve_batch_size", 6)), 12))]
        failures = []
        for index in indexes:
            try:
                payload["groups"][index]["resolution"] = resolve_group(payload["groups"][index])
            except BangumiError:
                failure = {"code": _BATCH_ERROR_CODE}
                failures.append(failure)
                payload["groups"][index]["resolution"] = {"status": "network_error", **failure}
        batch["payload"] = payload
        batch["summary"] = _summary(payload)
        batch["status"] = "ready" if batch["summary"]["pending"] == 0 else "resolving"
        batch["error"] = failures[0] if failures else {}
        batch["updated_at"] = timezone.now().isoformat()
        try:
            _save_batch(self.host, request.user, batch)
        except HostCapabilityError as error:
            return _host_failure_response(request, error)
        return Response(_serialize_batch(batch, request))

    def select_subject(self, request, batch_id):
        batch = _get_batch(self.host, request.user, batch_id)
        try:
            index = int(request.data.get("group_index"))
            subject_id = int(request.data.get("bangumi_id"))
            group = batch["payload"]["groups"][index]
        except (TypeError, ValueError, IndexError, KeyError):
            return Response(
                public_failure(
                    request=request,
                    candidate_code="invalid_selection",
                    status_code=status.HTTP_400_BAD_REQUEST,
                ),
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            group["resolution"] = resolve_subject_id(group, subject_id)
        except BangumiError:
            return Response(
                public_failure(request=request, candidate_code="bangumi_lookup_unavailable", status_code=status.HTTP_502_BAD_GATEWAY),
                status=status.HTTP_502_BAD_GATEWAY,
            )
        batch["summary"] = _summary(batch["payload"])
        batch["status"] = "ready" if batch["summary"]["pending"] == 0 else "resolving"
        batch["error"] = {}
        batch["updated_at"] = timezone.now().isoformat()
        try:
            _save_batch(self.host, request.user, batch)
        except HostCapabilityError as error:
            return _host_failure_response(request, error)
        return Response(_serialize_batch(batch, request))

    def commit(self, request, batch_id):
        try:
            _result, batch = self._commit_batch(
                request,
                batch_id,
                request.data.get("excluded_group_indices", []),
            )
        except HostCapabilityError as error:
            return _host_failure_response(request, error)
        return Response(_serialize_batch(batch, request))


def create_plugin(host):
    return WatchHistoryPlugin(host)
