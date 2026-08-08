from __future__ import annotations

import re
from datetime import date
from urllib.parse import quote
from uuid import uuid4

from django.db import transaction
from django.utils import timezone
from rest_framework import status
from rest_framework.response import Response

from journal.models import JournalEntry
from plugin_host.storage import PluginStorage

from .src.anime_journal_watch_history_importer.bangumi import BangumiError, resolve_group, resolve_subject_id
from .src.anime_journal_watch_history_importer.parser import build_preview, normalize_title


PLUGIN_SLUG = "watch-history-importer"
PLUGIN_VERSION = "0.3.0"
MAX_FILES = 8
MAX_FILE_BYTES = 2 * 1024 * 1024
DATE_TAG_RE = re.compile(r"^\d{4}年\d{1,2}月(?:\d{1,2}(?:日|号)?)?(?:\s*(?:-|–|—|至)\s*(?:\d{1,2}月)?\d{1,2}(?:日|号)?)?$")
BRUSH_TAG_RE = re.compile(r"^(?:首|二|三|四|五|六|七|八|九|十|\d+|X)刷$", re.IGNORECASE)
YEAR_TAG_RE = re.compile(r"^\d{4}年?$")


def _store(user, namespace):
    return PluginStorage(PLUGIN_SLUG, user=user, namespace=namespace)


def _config(user):
    from plugin_host.sdk.settings import get_user_settings

    return {"resolve_batch_size": 6, "unmatched_policy": "review", **get_user_settings(PLUGIN_SLUG, user)}


def _batch_key(batch_id):
    return str(batch_id)


def _get_batch(request, batch_id):
    batch = _store(request.user, "batches").get(_batch_key(batch_id))
    if not isinstance(batch, dict):
        return None
    return batch


def _save_batch(request, batch):
    _store(request.user, "batches").set(batch["id"], batch)
    return batch


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
        host.api.get("astrbot/schema", handler=self.astrbot_schema, access="user")

    @staticmethod
    def health_check():
        return True

    def astrbot_schema(self, request):
        return Response({"interface": "anime-journal.watch-history-import", "version": "2.0", "implemented": False, "workflow": ["preview", "resolve", "review", "commit"]})

    def status(self, request):
        batches = list(_store(request.user, "batches").collection().values("value")[:8])
        values = [item["value"] for item in batches]
        return Response({"status": "ok", "plugin": {"id": "com.anime-journal.watch-history-importer", "slug": PLUGIN_SLUG, "version": PLUGIN_VERSION}, "config": _config(request.user), "batches": [_serialize_batch(value, include_groups=False) for value in values]})

    def preview(self, request):
        files = request.FILES.getlist("files")
        if not files or len(files) > MAX_FILES:
            return Response({"code": "invalid_files", "detail": f"请选择 1 至 {MAX_FILES} 个 TXT 文件。"}, status=status.HTTP_400_BAD_REQUEST)
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
        _save_batch(request, batch)
        return Response(_serialize_batch(batch), status=status.HTTP_201_CREATED)

    def batch(self, request, batch_id):
        batch = _get_batch(request, batch_id)
        if batch is None:
            return Response({"code": "batch_missing", "detail": "导入批次不存在。"}, status=status.HTTP_404_NOT_FOUND)
        return Response(_serialize_batch(batch))

    def resolve_next(self, request, batch_id):
        batch = _get_batch(request, batch_id)
        if batch is None:
            return Response({"code": "batch_missing", "detail": "导入批次不存在。"}, status=status.HTTP_404_NOT_FOUND)
        payload = batch.get("payload") or {}
        indexes = [index for index, group in enumerate(payload.get("groups", [])) if group.get("resolution", {}).get("status") == "pending"][: max(1, min(int(_config(request.user).get("resolve_batch_size", 6)), 12))]
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
        _save_batch(request, batch)
        return Response(_serialize_batch(batch))

    def select_subject(self, request, batch_id):
        batch = _get_batch(request, batch_id)
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
        _save_batch(request, batch)
        return Response(_serialize_batch(batch))

    def commit(self, request, batch_id):
        batch = _get_batch(request, batch_id)
        if batch is None:
            return Response({"code": "batch_missing", "detail": "导入批次不存在。"}, status=status.HTTP_404_NOT_FOUND)
        groups = batch.get("payload", {}).get("groups", [])
        raw_excluded = request.data.get("excluded_group_indices", [])
        if not isinstance(raw_excluded, list) or any(isinstance(value, bool) or not isinstance(value, int) for value in raw_excluded):
            return Response({"code": "invalid_excluded_group_indices", "detail": "排除项必须是番剧分组索引列表。"}, status=status.HTTP_400_BAD_REQUEST)
        excluded = set(raw_excluded)
        selected = [group for index, group in enumerate(groups) if index not in excluded]
        if not selected:
            return Response({"code": "empty_import_selection", "detail": "至少保留一部番剧后才能正式导入。"}, status=status.HTTP_400_BAD_REQUEST)
        if any(group.get("resolution", {}).get("status") == "pending" for group in selected):
            return Response({"code": "resolution_pending", "detail": "仍有番剧尚未完成 Bangumi 匹配。"}, status=status.HTTP_409_CONFLICT)
        unresolved = [group for group in selected if group.get("resolution", {}).get("status") != "matched"]
        if unresolved and _config(request.user).get("unmatched_policy", "review") == "review":
            return Response({"code": "manual_review_required", "detail": f"仍有 {len(unresolved)} 部番剧需要人工选择 Bangumi 条目。"}, status=status.HTTP_409_CONFLICT)
        if batch.get("target_user_id") != request.user.pk:
            return Response({"code": "batch_owner_mismatch", "detail": "导入批次不属于当前账号。"}, status=status.HTTP_404_NOT_FOUND)
        target_user = request.user
        imported_entries = set()
        imported_records = 0
        with transaction.atomic():
            entries = list(JournalEntry.objects.select_for_update().filter(user=target_user, deleted_at__isnull=True))
            by_title = {normalize_title(title): entry for entry in entries for title in (entry.title, entry.japanese_title) if normalize_title(title)}
            history_store = _store(target_user, "watch_history")
            subject_store = _store(target_user, "subjects")
            for group in selected:
                resolution = group.get("resolution", {})
                if resolution.get("status") != "matched" or not resolution.get("bangumi_id"):
                    continue
                entry = by_title.get(normalize_title(resolution.get("title"))) or by_title.get(normalize_title(resolution.get("japanese_title")))
                latest = max(group.get("records", []), key=lambda record: record.get("watch_date") or "")
                bangumi_tags = [str(tag).strip() for tag in resolution.get("tags", []) if str(tag).strip() and not _metadata_tag(tag)]
                existing_tags = [] if entry is None else [tag for tag in entry.tags if not _metadata_tag(tag) and not BRUSH_TAG_RE.match(str(tag))]
                tags = list(dict.fromkeys([latest.get("watch_date_label"), latest.get("brush_label"), *bangumi_tags, *existing_tags]))[:30]
                defaults = {
                    "title": resolution.get("title") or group["source_title"], "japanese_title": resolution.get("japanese_title") or "",
                    "airing_period": _season_label(resolution.get("air_date")), "studio": resolution.get("studio") or "",
                    "episodes": str(resolution.get("episodes") or ""), "description": resolution.get("description") or "",
                    "poster_url": resolution.get("poster_url") or "", "baike_url": _moegirl_url(resolution.get("title") or group["source_title"]),
                    "tags": [tag for tag in tags if tag], "watch_status": JournalEntry.WatchStatus.COMPLETED,
                    "visibility": JournalEntry.Visibility.PRIVATE,
                }
                if entry is None:
                    entry = JournalEntry.objects.create(user=target_user, **defaults)
                else:
                    for field, value in defaults.items():
                        if value or field in {"tags", "watch_status", "visibility"}:
                            setattr(entry, field, value)
                    entry.save()
                by_title[normalize_title(entry.title)] = entry
                imported_entries.add(entry.pk)
                subject_store.set(f"{target_user.pk}:{resolution['bangumi_id']}", {"entry_id": entry.pk, "batch_id": batch["id"]})
                history = history_store.get(str(entry.pk), []) or []
                keys = {(item.get("watched_on"), item.get("brush_label"), item.get("episode_start"), item.get("episode_end")) for item in history}
                for record in group.get("records", []):
                    episode = record.get("episode_range") or {}
                    normalized = {"watched_on": record["watch_date"], "watched_label": record["watch_date_label"], "brush_number": record.get("brush"), "brush_label": record["brush_label"], "episode_start": episode.get("start"), "episode_end": episode.get("end"), "notes": record.get("notes", [])}
                    key = (normalized["watched_on"], normalized["brush_label"], normalized["episode_start"], normalized["episode_end"])
                    if key not in keys:
                        history.append(normalized)
                        keys.add(key)
                        imported_records += 1
                history_store.set(str(entry.pk), history)
        batch["status"] = "imported"
        batch["imported_at"] = timezone.now().isoformat()
        batch["summary"] = {**batch.get("summary", {}), "imported_entries": len(imported_entries), "imported_history_records": imported_records, "selected_groups": len(selected), "excluded_groups": len(excluded), "excluded_group_indices": sorted(excluded)}
        batch["updated_at"] = timezone.now().isoformat()
        _save_batch(request, batch)
        return Response(_serialize_batch(batch))


def create_plugin(host):
    return WatchHistoryPlugin(host)
