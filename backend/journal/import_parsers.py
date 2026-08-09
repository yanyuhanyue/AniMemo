import json

from django.conf import settings
from rest_framework.exceptions import ParseError
from rest_framework.parsers import JSONParser


def extract_import_records(payload):
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict) and "records" in payload:
        extra_keys = set(payload) - {"records", "preview", "version"}
        if extra_keys:
            raise ValueError("导入 JSON 包含不支持的顶层字段。")
        return payload.get("records")
    raise ValueError("导入内容必须是记录数组或包含 records 数组的对象。")


class LimitedImportJSONParser(JSONParser):
    """JSON parser used only by the journal import endpoint."""

    def parse(self, stream, media_type=None, parser_context=None):
        limit = settings.IMPORT_FILE_MAX_BYTES
        request = (parser_context or {}).get("request")
        declared = None
        if request is not None:
            try:
                declared = int(request.META.get("CONTENT_LENGTH") or 0)
            except (TypeError, ValueError):
                declared = None
        if declared is not None and declared > limit:
            raise ParseError("导入内容不能超过 2 MB。")

        raw = stream.read(limit + 1)
        if len(raw) > limit:
            raise ParseError("导入内容不能超过 2 MB。")
        if b"\x00" in raw:
            raise ParseError("导入内容包含非法空字节。")
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ParseError("导入内容必须使用 UTF-8 编码。") from error
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as error:
            raise ParseError("JSON 格式不正确。") from error
        return payload
