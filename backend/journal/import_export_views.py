import csv
import io
import json
import re
import unicodedata

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from rest_framework import status
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView

from .import_parsers import LimitedImportJSONParser, extract_import_records
from .models import JournalEntry
from .serializers import JournalEntrySerializer


class ExportEntriesView(APIView):
    def get(self, request):
        entries = JournalEntry.objects.filter(
            user=request.user,
            deleted_at__isnull=True,
        ).prefetch_related("external_identities")
        serializer = JournalEntrySerializer(entries, many=True, context={"request": request})
        return Response({"version": 1, "exported_at": timezone.now(), "records": serializer.data})


def _import_identity(value):
    normalized = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return re.sub(r"[\s《》「」『』【】〔〕〈〉·・:：,，。.!！?？'\"“”‘’\-—_]", "", normalized)


def _import_identity_values(record):
    return {key for key in (_import_identity(record.get("title")), _import_identity(record.get("japanese_title"))) if key}


def _normalize_import_record(raw):
    if not isinstance(raw, dict):
        return None
    tags = raw.get("tags", [])
    if isinstance(tags, str):
        tags = re.split(r"[，,]", tags)
    poster_url = raw.get("poster_url", raw.get("posterUrl", "")) or ""
    custom_poster_url = raw.get("custom_poster_url", raw.get("customPosterUrl", "")) or ""
    legacy_poster = raw.get("poster", "") or ""
    if not poster_url and not custom_poster_url and re.match(r"^https?://", str(legacy_poster), re.I):
        poster_url = legacy_poster
    return {
        "title": raw.get("title", ""),
        "japanese_title": raw.get("japanese_title", raw.get("japaneseTitle", "")),
        "airing_period": raw.get("airing_period", raw.get("period", "")),
        "studio": raw.get("studio", ""),
        "episodes": raw.get("episodes", ""),
        "description": raw.get("description", ""),
        "poster_url": poster_url,
        "custom_poster_url": custom_poster_url,
        "baike_url": raw.get("baike_url", raw.get("baikeUrl", "")),
        "tags": tags,
        "tag_colors": raw.get("tag_colors", raw.get("tagColors", {})) or {},
        "personal_score": raw.get("personal_score", raw.get("score")),
        "watch_status": raw.get("watch_status", raw.get("status", "planned")),
        "review": raw.get("review", ""),
        "visibility": raw.get("visibility", "public" if raw.get("shared") else "private"),
    }


def _import_error_text(errors):
    parts = []
    for field, messages in errors.items():
        values = messages if isinstance(messages, list) else [messages]
        parts.append(f"{field}: {'、'.join(str(message) for message in values)}")
    return "；".join(parts) or "记录格式无效"


def _contains_null(value):
    if isinstance(value, str):
        return "\x00" in value
    if isinstance(value, dict):
        return any(_contains_null(key) or _contains_null(item) for key, item in value.items())
    if isinstance(value, list):
        return any(_contains_null(item) for item in value)
    return False


def _import_value_depth(value):
    if isinstance(value, dict):
        return 1 + max((_import_value_depth(item) for item in value.values()), default=0)
    if isinstance(value, list):
        return 1 + max((_import_value_depth(item) for item in value), default=0)
    return 0


def _validate_import_record_limits(record, row_number):
    if not isinstance(record, dict):
        raise ValueError(f"第 {row_number} 行不是对象记录。")
    if len(record) > settings.IMPORT_MAX_COLUMNS:
        raise ValueError(f"第 {row_number} 行字段数量过多。")
    if _contains_null(record):
        raise ValueError(f"第 {row_number} 行包含非法空字节。")
    if _import_value_depth(record) > settings.IMPORT_MAX_NESTING_DEPTH:
        raise ValueError(f"第 {row_number} 行嵌套结构过深。")
    for key, value in record.items():
        if isinstance(value, (str, bytes)) and len(value) > settings.IMPORT_FIELD_MAX_LENGTH:
            raise ValueError(f"第 {row_number} 行字段过长。")
        if isinstance(value, (dict, list)) and len(json.dumps(value, ensure_ascii=False)) > settings.IMPORT_FIELD_MAX_LENGTH:
            raise ValueError(f"第 {row_number} 行嵌套字段过长。")


class ImportEntriesView(APIView):
    parser_classes = [LimitedImportJSONParser, MultiPartParser, FormParser]
    throttle_scope = "import"

    def _read_records(self, request):
        if "file" in request.FILES:
            upload = request.FILES["file"]
            declared_size = getattr(upload, "size", None)
            if declared_size is not None and declared_size > settings.IMPORT_FILE_MAX_BYTES:
                raise ValueError("导入文件不能超过 2 MB。")
            raw = upload.read(settings.IMPORT_FILE_MAX_BYTES + 1)
            if len(raw) > settings.IMPORT_FILE_MAX_BYTES:
                raise ValueError("导入文件不能超过 2 MB。")
            if b"\x00" in raw:
                raise ValueError("导入文件包含不支持的二进制内容。")
            try:
                text = raw.decode("utf-8-sig")
            except UnicodeDecodeError as error:
                raise ValueError("导入文件必须使用 UTF-8 编码。") from error
            if any(len(line) > settings.IMPORT_MAX_LINE_LENGTH for line in text.splitlines()):
                raise ValueError("导入文件单行内容过长。")
            if upload.name.lower().endswith(".csv"):
                csv.field_size_limit(settings.IMPORT_FIELD_MAX_LENGTH)
                try:
                    reader = csv.DictReader(io.StringIO(text, newline=""), strict=True)
                    if not reader.fieldnames or len(reader.fieldnames) > settings.IMPORT_MAX_COLUMNS:
                        raise ValueError("CSV 表头字段数量不合法。")
                    records = []
                    for row_number, row in enumerate(reader, start=2):
                        if row_number - 1 > settings.IMPORT_MAX_RECORDS:
                            raise ValueError("导入文件最多允许 500 条记录。")
                        if None in row:
                            raise ValueError(f"第 {row_number} 行列数不合法。")
                        _validate_import_record_limits(row, row_number)
                        records.append(row)
                except csv.Error as error:
                    raise ValueError("CSV 文件格式不合法。") from error
            elif upload.name.lower().endswith(".json"):
                try:
                    payload = json.loads(text)
                except json.JSONDecodeError as error:
                    raise ValueError("JSON 文件格式不合法。") from error
                records = extract_import_records(payload)
            else:
                raise ValueError("仅支持 .json 或 .csv 导入文件。")
        else:
            records = extract_import_records(request.data)
        if not isinstance(records, list):
            raise ValueError("导入内容必须是记录数组。")
        if len(records) > settings.IMPORT_MAX_RECORDS:
            raise ValueError("导入文件最多允许 500 条记录。")
        for row_number, record in enumerate(records, start=1):
            _validate_import_record_limits(record, row_number)
        return records

    def _prepare(self, request, records):
        existing_keys = set()
        existing = JournalEntry.objects.filter(user=request.user, deleted_at__isnull=True).values_list("title", "japanese_title")
        for title, japanese_title in existing:
            existing_keys.update(_import_identity_values({"title": title, "japanese_title": japanese_title}))

        seen_keys = set()
        ready_rows = []
        items = []
        errors = []
        for index, raw in enumerate(records):
            normalized = _normalize_import_record(raw)
            if normalized is None:
                message = "第 %s 行不是对象记录" % (index + 1)
                errors.append({"row": index + 1, "errors": {"record": [message]}})
                items.append({"row": index + 1, "title": "", "status": "invalid", "reason": message})
                continue
            identity = _import_identity_values(normalized)
            title = str(normalized.get("title") or "")
            if identity & (existing_keys | seen_keys):
                items.append({"row": index + 1, "title": title, "status": "duplicate", "reason": "已存在或在本文件中重复"})
                continue
            serializer = JournalEntrySerializer(data=normalized, context={"request": request})
            if not serializer.is_valid():
                reason = _import_error_text(serializer.errors)
                errors.append({"row": index + 1, "errors": serializer.errors})
                items.append({"row": index + 1, "title": title, "status": "invalid", "reason": reason})
                continue
            ready_rows.append(normalized)
            seen_keys.update(identity)
            items.append({"row": index + 1, "title": title, "status": "ready", "reason": "等待导入"})
        return {
            "total": len(records),
            "ready": len(ready_rows),
            "skipped_duplicates": sum(item["status"] == "duplicate" for item in items),
            "errors": errors,
            "items": items,
            "ready_rows": ready_rows,
        }

    def post(self, request):
        try:
            records = self._read_records(request)
        except (ValueError, UnicodeDecodeError, OSError, json.JSONDecodeError) as error:
            return Response({"detail": str(error) or "导入文件无法读取。"}, status=status.HTTP_400_BAD_REQUEST)

        prepared = self._prepare(request, records)
        preview_value = request.data.get("preview", "") if isinstance(request.data, dict) else ""
        is_preview = str(preview_value).lower() in {"1", "true", "yes"}
        if is_preview:
            return Response({key: value for key, value in prepared.items() if key != "ready_rows"})

        if prepared["errors"]:
            return Response({
                "created": 0,
                "total": prepared["total"],
                "skipped_duplicates": prepared["skipped_duplicates"],
                "errors": prepared["errors"],
            }, status=status.HTTP_400_BAD_REQUEST)

        created = 0
        commit_errors = list(prepared["errors"])
        try:
            with transaction.atomic():
                for index, normalized in enumerate(prepared["ready_rows"], start=1):
                    serializer = JournalEntrySerializer(data=normalized, context={"request": request})
                    if not serializer.is_valid():
                        raise ValueError(json.dumps({"row": index, "errors": serializer.errors}, ensure_ascii=False))
                    serializer.save(user=request.user)
                    created += 1
        except ValueError as error:
            created = 0
            try:
                commit_errors.append(json.loads(str(error)))
            except json.JSONDecodeError:
                commit_errors.append({"row": 0, "errors": {"record": ["导入提交失败。"]}})
        response_data = {
            "created": created,
            "total": prepared["total"],
            "skipped_duplicates": prepared["skipped_duplicates"],
            "errors": commit_errors,
        }
        response_status = status.HTTP_201_CREATED if created else (status.HTTP_400_BAD_REQUEST if commit_errors else status.HTTP_200_OK)
        return Response(response_data, status=response_status)

