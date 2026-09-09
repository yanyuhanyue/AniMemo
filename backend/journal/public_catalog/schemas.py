"""Explicit OpenAPI contracts for every bounded public success shape."""
from drf_spectacular.utils import OpenApiParameter

from .contracts import FIELDS, SCHEMA, SORTS


def obj(properties):
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


TEXT = {"type": "string"}
COUNT = {"type": "integer", "minimum": 0}
BOOL = {"type": "boolean"}
TOKEN = {"type": "string", "maxLength": 4096, "nullable": True}
URL = {"type": "string", "format": "uri-reference"}
SCOPE = obj({
    "kind": {"type": "string", "enum": ["homepage", "showcase", "directory"]},
    "owner_id": {"type": "integer", "nullable": True},
    "visibility": {"type": "string", "enum": ["public", "owner"]},
    "public_slug": {"type": "string", "format": "uuid", "nullable": True},
})
COMMON = {"schema": {"type": "string", "enum": [SCHEMA]}, "consistency": {"type": "string", "enum": ["live"]}, "scope": SCOPE}
FIELD_META = obj({
    "complete": BOOL, "kind": {"type": "string", "enum": ["string", "json"]},
    "length": {**COUNT, "description": "Length of the full JSON text in Unicode codepoints."}, "field_url": URL,
})
ENTRY = obj({
    "id": {"type": "integer", "minimum": 1}, "title": {**TEXT, "maxLength": 200},
    "japanese_title": {**TEXT, "maxLength": 200}, "airing_period": {**TEXT, "maxLength": 50},
    "studio": {**TEXT, "maxLength": 120}, "episodes": {**TEXT, "maxLength": 30},
    "description": {**TEXT, "maxLength": 512}, "review": {**TEXT, "maxLength": 512},
    "poster_url": TEXT, "poster": {**TEXT, "nullable": True}, "baike_url": TEXT,
    "tags": {"type": "array", "maxItems": 8, "items": {**TEXT, "maxLength": 160}},
    "tag_colors": {"description": "Complete original JSON when bounded; otherwise an empty preview. Read fields.tag_colors for completeness and continuation."},
    "personal_score": {"type": "string", "nullable": True}, "watch_status": TEXT,
    "watch_status_display": TEXT, "visibility": TEXT, "watch_history_count": COUNT,
    "created_at": {"type": "string", "format": "date-time"}, "updated_at": {"type": "string", "format": "date-time"},
    "revision": {**TEXT, "maxLength": 64}, "detail_url": URL,
    "fields": obj({name: FIELD_META for name in FIELDS}),
    "preset_colors": {"type": "object", "additionalProperties": TEXT, "description": "Current public preset colors for this entry's preview tags; presentation metadata."},
})
STATS = obj({**{name: COUNT for name in ("total", "completed_count", "movie_count", "ova_count", "short_count", "masterpiece_count", "pending_count")}, "average_score": {"type": "number"}})
PROFILE_PROPERTIES = {"nickname": TEXT, "subtitle": TEXT, "avatar_url": TEXT, "accent": TEXT, "public_slug": {"type": "string", "format": "uuid"}}
PROFILE = obj(PROFILE_PROPERTIES)
PAGE = {**COMMON, "total": COUNT, "matched_count": COUNT, "page_count": COUNT, "next_cursor": TOKEN}
ENTRIES = obj({**PAGE, "results": {"type": "array", "maxItems": 100, "items": ENTRY}})
SUMMARY = obj({**COMMON, "total": COUNT, "matched_count": COUNT, "unscored_count": COUNT, "stats": STATS, "profile": {**PROFILE, "nullable": True}})
DETAIL = obj({**COMMON, "entry": ENTRY, "next_export_cursor": {"type": "string", "maxLength": 4096}})
FIELD = obj({
    **COMMON, "entry_id": {"type": "integer"}, "field": {"type": "string", "enum": [*FIELDS, "tag", "year"]},
    "revision": TEXT, "encoding": {"type": "string", "enum": ["json-text"]},
    "offset": COUNT, "fragment": {**TEXT, "maxLength": 16384}, "total_length": COUNT,
    "next_cursor": TOKEN, "complete": BOOL,
})
FACET_VALUE = obj({"value": {**TEXT, "nullable": True}, "preview": {**TEXT, "maxLength": 160}, "complete": BOOL, "selection_token": {**TEXT, "maxLength": 4096}, "revision": TEXT, "field_url": URL})
FACETS = obj({
    **COMMON, "kind": {"type": "string", "enum": ["tags", "years"]},
    "values": {"type": "array", "maxItems": 100, "items": FACET_VALUE}, "next_cursor": TOKEN,
    "complete": BOOL, "facet_scope": {"type": "string", "enum": ["authorized"]},
    "order": {"type": "string", "enum": ["facets-v1-numeric-then-zh"]},
})
DIRECTORY = obj({**PAGE, "results": {"type": "array", "maxItems": 100, "items": obj({**PROFILE_PROPERTIES, "username": TEXT, "stats": STATS, "top_picks": {"type": "array", "maxItems": 3, "items": ENTRY}})}})
ERROR = {"$ref": "#/components/schemas/ApiError"}

PAGE_PARAMS = [
    OpenApiParameter("page_size", {"type": "integer", "minimum": 1, "maximum": 100, "default": 50}),
    OpenApiParameter("cursor", {"type": "string", "maxLength": 4096}, description="Signed route/scope/query-bound live cursor; expires after 900 seconds. Reuse the original query."),
]
FILTER_PARAMS = [OpenApiParameter(name, str) for name in ("search", "tag", "tag_ref", "status", "year", "year_ref", "quick")] + [OpenApiParameter("sort", str, enum=sorted(SORTS), default="date-desc")]
FIELD_PARAMS = [
    OpenApiParameter("revision", str, required=True),
    OpenApiParameter("cursor", {"type": "string", "maxLength": 4096}),
    OpenApiParameter("part_size", {"type": "integer", "minimum": 1, "maximum": 16384, "default": 4096}),
]
DESCRIPTION = "Live reads recheck current authority. Success is compact uncompressed UTF-8 JSON at most 524288 bytes; errors are at most 16384 bytes. Pages use keyset order and may emit fewer than page_size items to fit the byte budget. Long stored values remain accessible through revision-bound JSON-text field segments."


def responses(success):
    return {200: success, 400: ERROR, 401: ERROR, 403: ERROR, 404: ERROR, 409: ERROR, 410: ERROR, 500: ERROR, 503: ERROR}
