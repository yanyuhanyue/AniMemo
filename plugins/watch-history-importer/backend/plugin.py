from __future__ import annotations

import json
import re
from datetime import date
from urllib.parse import quote
from uuid import uuid4

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from rest_framework import status
from rest_framework.response import Response

from plugin_host.sdk import HostCapabilityError, PluginStorageLimitError

from .src.animemo_watch_history_importer.bangumi import BangumiError, resolve_group, resolve_subject_id
from .src.animemo_watch_history_importer.parser import build_preview, normalize_title


PLUGIN_SLUG = "watch-history-importer"
PLUGIN_VERSION = "0.4.4"
MAX_FILES = 8
MAX_FILE_BYTES = 2 * 1024 * 1024
INTEGRATION_TEXT_MAX_BYTES = 120 * 1024
DATE_TAG_RE = re.compile(r"^\d{4}年\d{1,2}月(?:\d{1,2}(?:日|号)?)?(?:\s*(?:-|–|—|至)\s*(?:\d{1,2}月)?\d{1,2}(?:日|号)?)?$")
BRUSH_TAG_RE = re.compile(r"^(?:首|二|三|四|五|六|七|八|九|十|\d+|X)刷$", re.IGNORECASE)
YEAR_TAG_RE = re.compile(r"^\d{4}年?$")


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


def _serialize_batch(batch, include_groups=True):
    payload = batch.get("payload") or {}
    result = {
        "id": batch["id"],
        "status": batch.get("status", "preview"),
        "target_user": {
            "id": batch.get("target_user_id"),
            "username": batch.get("target_username", ""),
            "email": batch.get("target_email", ""),
        },
        "source_names": batch.get("source_names", []),
        "summary": batch.get("summary") or payload.get("summary", {}),
        "error": batch.get("error", ""),
        "created_at": batch.get("created_at"),
        "updated_at": batch.get("updated_at"),
        "imported_at": batch.get("imported_at"),
    }
    if include_groups:
        result["groups"] = payload.get("groups", [])
        result["excluded"] = payload.get("excluded", [])
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
                return {"code": error.code, "detail": error.detail}, error.status_code
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
            return {"code": error.code, "detail": error.detail}, error.status_code

    def integration_history_add(self, context, payload):
        try:
            entry_id = int(payload.get("entry_id"))
        except (TypeError, ValueError):
            return {"code": "invalid_entry_id", "detail": "entry_id 无效。"}, 400
        journal = self.host.journal.bind(context)
        history = self.host.watch_history.bind(context)
        try:
            entry = journal.get_entry(entry_id)
            result = history.add_history(
                entry_id,
                {key: value for key, value in payload.items() if key != "entry_id"},
            )
        except HostCapabilityError as error:
            return {"code": error.code, "detail": error.detail}, error.status_code
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
            return {"code": error.code, "detail": error.detail}, error.status_code
        if query:
            needle = normalize_title(query)
            rows = [row for row in rows if needle in normalize_title(row["title"]) or needle in normalize_title(row["japanese_title"])]
        return {"entries": rows[:20]}

    def integration_import_preview(self, context, payload):
        text = payload.get("text")
        if not isinstance(text, str) or not text.strip() or len(text.encode("utf-8")) > INTEGRATION_TEXT_MAX_BYTES:
            return {"code": "invalid_text", "detail": "text 必须是非空 UTF-8 文本且不超过 120 KiB。"}, 400
        preview = build_preview([(str(payload.get("filename") or "integration.txt")[:120], text)])
        return {"summary": preview.get("summary", {}), "groups": preview.get("groups", [])}

    def integration_import_commit(self, context, payload):
        batch_id = str(payload.get("batch_id") or "").strip()
        if not batch_id:
            return {"code": "batch_missing", "detail": "batch_id 必填。"}, 400
        try:
            result, _batch = self._commit_batch(
                context,
                batch_id,
                payload.get("excluded_group_indices", []),
            )
        except HostCapabilityError as error:
            return {"code": error.code, "detail": error.detail}, error.status_code
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
        return Response({"status": "ok", "plugin": {"id": "com.animemo.watch-history-importer", "slug": PLUGIN_SLUG, "version": PLUGIN_VERSION}, "config": _config(self.host, request.user), "batches": [_serialize_batch(value, include_groups=False) for value in values]})

    def preview(self, request):
        files = request.FILES.getlist("files")
        if not files or len(files) > MAX_FILES:
            return Response({"code": "invalid_files", "detail": f"请选择 1 至 {MAX_FILES} 个 TXT 文件。"}, status=status.HTTP_400_BAD_REQUEST)
        total_bytes = sum(max(0, int(getattr(upload, "size", 0) or 0)) for upload in files)
        if total_bytes > _total_upload_max_bytes():
            return Response(
                {
                    "code": "files_too_large",
                    "detail": f"TXT 文件总大小不能超过 {_total_upload_max_bytes() // (1024 * 1024)} MiB。",
                },
                status=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            )
        try:
            documents = []
            for upload in files:
                if not upload.name.lower().endswith(".txt"):
                    raise ValueError(f"{upload.name} 不是 TXT 文件。")
                documents.append((upload.name, _read_uploaded_text(upload)))
        except (TypeError, ValueError) as error:
            return Response({"code": "invalid_file", "detail": str(error)}, status=status.HTTP_400_BAD_REQUEST)
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
            "error": "",
            "created_at": timezone.now().isoformat(),
            "updated_at": timezone.now().isoformat(),
            "imported_at": None,
        }
        try:
            _save_batch(self.host, request.user, batch)
        except HostCapabilityError as error:
            return Response(
                {"code": error.code, "detail": error.detail},
                status=error.status_code,
            )
        return Response(_serialize_batch(batch), status=status.HTTP_201_CREATED)

    def batch(self, request, batch_id):
        batch = _get_batch(self.host, request.user, batch_id)
        if batch is None:
            return Response({"code": "batch_missing", "detail": "导入批次不存在。"}, status=status.HTTP_404_NOT_FOUND)
        return Response(_serialize_batch(batch))

    def resolve_next(self, request, batch_id):
        batch = _get_batch(self.host, request.user, batch_id)
        if batch is None:
            return Response({"code": "batch_missing", "detail": "导入批次不存在。"}, status=status.HTTP_404_NOT_FOUND)
        payload = batch.get("payload") or {}
        indexes = [index for index, group in enumerate(payload.get("groups", [])) if group.get("resolution", {}).get("status") == "pending"][: max(1, min(int(_config(self.host, request.user).get("resolve_batch_size", 6)), 12))]
        errors = []
        for index in indexes:
            try:
                payload["groups"][index]["resolution"] = resolve_group(payload["groups"][index])
            except BangumiError as error:
                errors.append(str(error))
                payload["groups"][index]["resolution"] = {"status": "network_error", "detail": str(error)}
        batch["payload"] = payload
        batch["summary"] = _summary(payload)
        batch["status"] = "ready" if batch["summary"]["pending"] == 0 else "resolving"
        batch["error"] = "\n".join(errors)
        batch["updated_at"] = timezone.now().isoformat()
        try:
            _save_batch(self.host, request.user, batch)
        except HostCapabilityError as error:
            return Response(
                {"code": error.code, "detail": error.detail},
                status=error.status_code,
            )
        return Response(_serialize_batch(batch))

    def select_subject(self, request, batch_id):
        batch = _get_batch(self.host, request.user, batch_id)
        try:
            index = int(request.data.get("group_index"))
            subject_id = int(request.data.get("bangumi_id"))
            group = batch["payload"]["groups"][index]
        except (TypeError, ValueError, IndexError, KeyError):
            return Response({"code": "invalid_selection", "detail": "请选择有效的导入条目和 Bangumi ID。"}, status=status.HTTP_400_BAD_REQUEST)
        try:
            group["resolution"] = resolve_subject_id(group, subject_id)
        except BangumiError as error:
            return Response({"code": "bangumi_failed", "detail": str(error)}, status=status.HTTP_502_BAD_GATEWAY)
        batch["summary"] = _summary(batch["payload"])
        batch["status"] = "ready" if batch["summary"]["pending"] == 0 else "resolving"
        batch["error"] = ""
        batch["updated_at"] = timezone.now().isoformat()
        try:
            _save_batch(self.host, request.user, batch)
        except HostCapabilityError as error:
            return Response(
                {"code": error.code, "detail": error.detail},
                status=error.status_code,
            )
        return Response(_serialize_batch(batch))

    def commit(self, request, batch_id):
        try:
            _result, batch = self._commit_batch(
                request,
                batch_id,
                request.data.get("excluded_group_indices", []),
            )
        except HostCapabilityError as error:
            return Response({"code": error.code, "detail": error.detail}, status=error.status_code)
        return Response(_serialize_batch(batch))


def create_plugin(host):
    return WatchHistoryPlugin(host)
