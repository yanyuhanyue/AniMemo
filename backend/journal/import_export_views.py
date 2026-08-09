import csv
import io
import json
import re
import unicodedata

from django.conf import settings
from django.db import transaction
from rest_framework import status
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView

from .data_bundle import DataBundleError, export_data_bundle, import_data_bundle, preview_data_bundle
from .import_parsers import LimitedImportJSONParser
from .models import JournalEntry
from .serializers import JournalEntrySerializer


CSV_IMPORT_FIELDS = {
    "title", "japanese_title", "airing_period", "studio", "episodes",
    "description", "poster_url", "custom_poster_url", "baike_url", "tags",
    "tag_colors", "personal_score", "watch_status", "review", "visibility",
}


class ExportEntriesView(APIView):
    def get(self, request):
        return Response(export_data_bundle(user=request.user))


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
    return {
        "title": raw.get("title", ""),
        "japanese_title": raw.get("japanese_title", ""),
        "airing_period": raw.get("airing_period", ""),
        "studio": raw.get("studio", ""),
        "episodes": raw.get("episodes", ""),
        "description": raw.get("description", ""),
        "poster_url": raw.get("poster_url", "") or "",
        "custom_poster_url": raw.get("custom_poster_url", "") or "",
        "baike_url": raw.get("baike_url", ""),
        "tags": tags,
        "tag_colors": raw.get("tag_colors", {}) or {},
        "personal_score": raw.get("personal_score"),
        "watch_status": raw.get("watch_status", "planned"),
        "review": raw.get("review", ""),
        "visibility": raw.get("visibility", "private"),
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

    def _read_payload(self, request):
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
                    unknown_fields = set(reader.fieldnames) - CSV_IMPORT_FIELDS
                    if unknown_fields:
                        raise ValueError(f"CSV 包含不支持的字段：{', '.join(sorted(unknown_fields))}。")
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
                payload_kind = "csv"
                payload = records
            elif upload.name.lower().endswith(".json"):
                try:
                    payload = json.loads(text)
                except json.JSONDecodeError as error:
                    raise ValueError("JSON 文件格式不合法。") from error
                payload_kind = "bundle"
            else:
                raise ValueError("仅支持 .json 或 .csv 导入文件。")
        else:
            payload_kind = "bundle"
            payload = request.data
        if payload_kind == "bundle":
            return payload_kind, payload
        records = payload
        if not isinstance(records, list):
            raise ValueError("导入内容必须是记录数组。")
        if len(records) > settings.IMPORT_MAX_RECORDS:
            raise ValueError("导入文件最多允许 500 条记录。")
        for row_number, record in enumerate(records, start=1):
            _validate_import_record_limits(record, row_number)
        return payload_kind, records

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
            payload_kind, payload = self._read_payload(request)
        except (ValueError, UnicodeDecodeError, OSError, json.JSONDecodeError) as error:
            return Response({"detail": str(error) or "导入文件无法读取。"}, status=status.HTTP_400_BAD_REQUEST)

        preview_value = request.query_params.get("preview", "")
        if not preview_value and hasattr(request.data, "get"):
            preview_value = request.data.get("preview", "")
        is_preview = str(preview_value).lower() in {"1", "true", "yes"}
        if payload_kind == "bundle":
            try:
                result = (
                    preview_data_bundle(user=request.user, payload=payload)
                    if is_preview
                    else import_data_bundle(user=request.user, payload=payload)
                )
            except DataBundleError as error:
                response = {"code": error.code, "detail": error.detail}
                if error.errors is not None:
                    response["errors"] = error.errors
                return Response(response, status=status.HTTP_400_BAD_REQUEST)
            return Response(result, status=status.HTTP_200_OK if is_preview else status.HTTP_201_CREATED)

        prepared = self._prepare(request, payload)
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

