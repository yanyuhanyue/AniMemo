"""PostgreSQL authority, bounded SQL projections and stable catalogue keys."""
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from urllib.parse import urlencode

from django.contrib.auth import get_user_model
from django.db import connection
from site_config.models import SiteSettings, TagDefinition

from journal.models import JournalEntry, UserSettings, WatchHistoryRecord
from journal.serializers_entries import JournalEntrySerializer

from .contracts import (
    COLLATION,
    FIELDS,
    JS_WHITESPACE,
    PUBLIC_FIELDS,
    QUICK_TAGS,
    CatalogError,
    read_token,
)

PREVIEW_CHARS = 512
TAG_PREVIEW_COUNT = 8
TAG_PREVIEW_CHARS = 160
JSON_PREVIEW_CHARS = 2048
DATE_KEY = "CASE WHEN e.airing_period ~ '^[0-9]{4}-[0-9]{1,2}$' THEN split_part(e.airing_period, '-', 1)::integer * 100 + split_part(e.airing_period, '-', 2)::integer ELSE 0 END"
PERIOD = "COALESCE(NULLIF(e.airing_period, ''), '未定档')"


def rows(sql, params=()):
    with connection.cursor() as cursor:
        cursor.execute(sql, params)
        names = [column[0] for column in cursor.description]
        return [dict(zip(names, row)) for row in cursor.fetchall()]


def require_database():
    if connection.vendor != "postgresql":
        raise CatalogError("public_catalog_unavailable", 503)
    result = rows(
        "SELECT collprovider, collisdeterministic, colliculocale, collversion, "
        "pg_collation_actual_version(oid) AS actual_version FROM pg_collation "
        "WHERE collname = %s AND collnamespace = current_schema()::regnamespace",
        [COLLATION],
    )
    if len(result) != 1 or (
        result[0]["collprovider"], result[0]["collisdeterministic"], result[0]["colliculocale"]
    ) != ("i", False, "zh-Hans-CN") or result[0]["collversion"] != result[0]["actual_version"]:
        raise CatalogError("public_catalog_unavailable", 503)
    return str(result[0]["collversion"])


def table(model):
    return connection.ops.quote_name(model._meta.db_table)


def collation():
    return connection.ops.quote_name(COLLATION)


@dataclass(frozen=True)
class Scope:
    kind: str
    owner_id: int | None
    visibility: str
    public_slug: str | None
    profile: dict | None = None

    @property
    def wire(self):
        return {"kind": self.kind, "owner_id": self.owner_id, "visibility": self.visibility, "public_slug": self.public_slug}

    @property
    def identity(self):
        return {**self.wire, "actor": self.owner_id if self.visibility == "owner" else None}

    @property
    def base(self):
        return "/api/v1/public/homepage/" if self.kind == "homepage" else f"/api/v1/public/showcase/{self.public_slug}/"


def profile(row, request):
    avatar = ""
    if row.get("avatar"):
        value = UserSettings(avatar=row["avatar"]).avatar.url
        avatar = request.build_absolute_uri(value) if value.startswith("/") else value
    return {
        "nickname": row["nickname"] or row["user__username"], "subtitle": row["showcase_subtitle"],
        "avatar_url": avatar, "accent": row["accent"], "public_slug": str(row["public_slug"]),
    }


def resolve_scope(request, kind, public_slug=None):
    if kind == "directory":
        return Scope(kind, None, "public", None)
    query = UserSettings.objects.filter(user__is_active=True)
    if kind == "homepage":
        owner_id = SiteSettings.objects.filter(pk=1).values_list("homepage_owner_id", flat=True).first()
        query = query.filter(user_id=owner_id, user__is_staff=True, allow_sharing=True, public_status=UserSettings.PublicStatus.APPROVED)
    else:
        query = query.filter(public_slug=public_slug)
    record = query.values("user_id", "nickname", "user__username", "showcase_subtitle", "avatar", "accent", "public_slug", "allow_sharing", "public_status").first()
    if record is None:
        if kind == "homepage":
            return Scope(kind, None, "public", None)
        raise CatalogError("not_found", 404)
    preview = kind == "showcase" and request.user.is_authenticated and request.user.pk == record["user_id"]
    if not preview and (not record["allow_sharing"] or record["public_status"] != UserSettings.PublicStatus.APPROVED):
        raise CatalogError("not_found", 404)
    return Scope(kind, record["user_id"], "owner" if preview else "public", str(record["public_slug"]), profile(record, request) if kind == "showcase" else None)


def authority(scope, alias="e"):
    source = f"{table(JournalEntry)} {alias} JOIN {table(get_user_model())} {alias}u ON {alias}u.id = {alias}.user_id JOIN {table(UserSettings)} {alias}s ON {alias}s.user_id = {alias}.user_id"
    clauses, params = [f"{alias}.deleted_at IS NULL", f"{alias}u.is_active"], []
    if scope.kind != "directory":
        clauses.append(f"{alias}.user_id = %s")
        params.append(scope.owner_id)
    if scope.kind == "homepage":
        clauses.append(f"{alias}u.is_staff")
    if scope.visibility == "public":
        clauses.extend([f"{alias}.visibility = %s", f"{alias}s.allow_sharing", f"{alias}s.public_status = %s"])
        params.extend([JournalEntry.Visibility.PUBLIC, UserSettings.PublicStatus.APPROVED])
    return source, " AND ".join(clauses), params


def history_count(alias="e"):
    return f"(SELECT count(*) FROM {table(WatchHistoryRecord)} wh WHERE wh.entry_id = {alias}.id)"


def revision(row):
    return hashlib.sha256(f"{row['id']}:{row['_row_version']}:{row['watch_history_count']}".encode("ascii")).hexdigest()


def reference_position(scope, token, kind):
    position = read_token(token, "facet-value", {"scope": scope.identity, "kind": kind})
    if set(position) != {"id", "ordinal", "revision"} or type(position["id"]) is not int or type(position["ordinal"]) is not int or position["id"] < 1 or position["ordinal"] < 1:
        raise CatalogError("public_catalog_cursor_invalid")
    source, where, params = authority(scope)
    found = rows(f"SELECT e.id, concat(e.xmin::text, ':', e.ctid::text) AS _row_version, {history_count()} AS watch_history_count FROM {source} WHERE {where} AND e.id = %s", [*params, position["id"]])
    if not found:
        raise CatalogError("not_found", 404)
    if position["revision"] != revision(found[0]):
        raise CatalogError("public_catalog_revision_changed", 409)
    return position


def reference_value(scope, position, kind):
    source, where, params = authority(scope, "r")
    if kind == "tags":
        value = "r.tags ->> %s"
        params = [position["ordinal"] - 1, *params, position["id"]]
    else:
        value = "split_part(COALESCE(NULLIF(r.airing_period, ''), '未定档'), '-', 1)"
        params = [*params, position["id"]]
    return f"(SELECT {value} FROM {source} WHERE {where} AND r.id = %s)", params


def filter_expression(scope, query):
    clauses, params = [], []
    if query.search:
        clauses.append(f"strpos(lower((e.title || ' ' || e.japanese_title) COLLATE {collation()}) COLLATE \"C\", lower(%s COLLATE {collation()}) COLLATE \"C\") > 0")
        params.append(query.search)
    if query.status:
        clauses.append("e.watch_status = %s")
        params.append(query.status)
    if query.tag or query.tag_ref:
        if query.tag_ref:
            value, arguments = reference_value(scope, reference_position(scope, query.tag_ref, "tags"), "tags")
        else:
            value, arguments = "%s::text", [query.tag]
        clauses.append(f"e.tags ? ({value})")
        params.extend(arguments)
    if query.year or query.year_ref:
        if query.year_ref:
            value, arguments = reference_value(scope, reference_position(scope, query.year_ref, "years"), "years")
        else:
            value, arguments = "%s::text", [query.year]
        clauses.append(f"starts_with({PERIOD}, {value})")
        params.extend(arguments)
    quick = []
    for tag in QUICK_TAGS[query.quick]:
        if tag == "OVA":
            quick.append("((e.title || ' ' || e.japanese_title) ~* %s OR EXISTS (SELECT 1 FROM jsonb_array_elements(CASE WHEN jsonb_typeof(e.tags) = 'array' THEN e.tags ELSE '[]'::jsonb END) jt(value) WHERE upper(jt.value #>> '{}') = 'OVA'))")
            params.append(f"(^|[{JS_WHITESPACE}《])OVA($|[{JS_WHITESPACE}》])")
        else:
            quick.append("e.tags ? %s")
            params.append(tag)
    if quick:
        clauses.append("(" + " OR ".join(quick) + ")")
    return " AND ".join(clauses) or "TRUE", params


def ordering(sort):
    if sort == "id-asc":
        return [("e.id", False, "id")]
    if sort.startswith("date-"):
        return [(DATE_KEY, sort == "date-desc", "date"), (f"e.title COLLATE {collation()}", False, "title"), ("e.updated_at", True, "updated"), ("e.id", True, "id")]
    return [("CASE WHEN e.personal_score IS NULL OR e.personal_score = 0 THEN 1 ELSE 0 END", False, "unscored"), ("COALESCE(e.personal_score, 0)", sort == "score-desc", "score"), ("e.updated_at", True, "updated"), ("e.id", True, "id")]


def key_position(row, sort):
    if sort == "id-asc":
        return {"id": row["id"]}
    result = {"updated": row["updated_at"].isoformat(), "id": row["id"]}
    if sort.startswith("date-"):
        result.update(date=row["_date_key"], title=row["title"])
    else:
        result.update(unscored=1 if row["personal_score"] is None or row["personal_score"] == 0 else 0, score=str(row["personal_score"] or 0))
    return result


def keyset(sort, position):
    keys = ordering(sort)
    if set(position) != {name for _expression, _descending, name in keys}:
        raise CatalogError("public_catalog_cursor_invalid")
    if sort == "id-asc":
        if type(position["id"]) is not int or not 1 <= position["id"] < 2 ** 63:
            raise CatalogError("public_catalog_cursor_invalid")
        return "e.id > %s", [position["id"]]
    try:
        values = {**position, "updated": datetime.fromisoformat(position["updated"])}
        if type(values["id"]) is not int or values["id"] < 1 or values["updated"].tzinfo is None:
            raise ValueError
        if sort.startswith("date-"):
            if type(values["date"]) is not int or not isinstance(values["title"], str) or len(values["title"]) > 200:
                raise ValueError
        else:
            values["score"] = Decimal(values["score"])
            if values["unscored"] not in (0, 1) or not values["score"].is_finite():
                raise ValueError
    except (ValueError, TypeError, ArithmeticError):
        raise CatalogError("public_catalog_cursor_invalid") from None
    terms, params = [], []
    for index, (expression, descending, name) in enumerate(keys):
        prefix = []
        for previous, _desc, key in keys[:index]:
            prefix.append(f"{previous} = %s")
            params.append(values[key])
        prefix.append(f"{expression} {'<' if descending else '>'} %s")
        params.append(values[name])
        terms.append("(" + " AND ".join(prefix) + ")")
    return "(" + " OR ".join(terms) + ")", params


def projection():
    scalar = ["id", "user_id", "title", "japanese_title", "airing_period", "studio", "episodes", "poster_url", "custom_poster_url", "poster_file", "baike_url", "personal_score", "watch_status", "visibility", "created_at", "updated_at"]
    pieces = [*(f"e.{name}" for name in scalar), "concat(e.xmin::text, ':', e.ctid::text) AS _row_version", f"{history_count()} AS watch_history_count", f"{DATE_KEY} AS _date_key"]
    for name in ("description", "review"):
        pieces.extend([f"left(e.{name}, {PREVIEW_CHARS}) AS {name}", f"char_length(e.{name}) <= {PREVIEW_CHARS} AS _{name}_complete", f"char_length(to_json(e.{name})::text) AS _{name}_length"])
    pieces.extend([
        f"COALESCE((SELECT jsonb_agg(COALESCE(left(jt.value #>> '{{}}', {TAG_PREVIEW_CHARS}), '') ORDER BY jt.ordinal) FROM jsonb_array_elements(CASE WHEN jsonb_typeof(e.tags) = 'array' THEN e.tags ELSE '[]'::jsonb END) WITH ORDINALITY jt(value, ordinal) WHERE jt.ordinal <= {TAG_PREVIEW_COUNT}), '[]'::jsonb) AS tags",
        f"jsonb_typeof(e.tags) = 'array' AND jsonb_array_length(CASE WHEN jsonb_typeof(e.tags) = 'array' THEN e.tags ELSE '[]'::jsonb END) <= {TAG_PREVIEW_COUNT} AND NOT EXISTS (SELECT 1 FROM jsonb_array_elements(CASE WHEN jsonb_typeof(e.tags) = 'array' THEN e.tags ELSE '[]'::jsonb END) jt(value) WHERE jsonb_typeof(jt.value) <> 'string' OR char_length(jt.value #>> '{{}}') > {TAG_PREVIEW_CHARS}) AS _tags_complete",
        "char_length(e.tags::text) AS _tags_length",
        f"left(e.tag_colors::text, {JSON_PREVIEW_CHARS}) AS _tag_colors_preview",
        f"char_length(e.tag_colors::text) <= {JSON_PREVIEW_CHARS} AS _tag_colors_complete",
        "char_length(e.tag_colors::text) AS _tag_colors_length",
    ])
    return ", ".join(pieces)


def dto(row, scope, request):
    record = {key: row.get(key) for key in PUBLIC_FIELDS}
    record["tags"] = json.loads(row["tags"]) if isinstance(row["tags"], str) else row["tags"]
    record["tag_colors"] = json.loads(row["_tag_colors_preview"]) if row["_tag_colors_complete"] else {}
    record["personal_score"] = None if row["personal_score"] is None else str(row["personal_score"])
    record["created_at"], record["updated_at"] = row["created_at"].isoformat(), row["updated_at"].isoformat()
    record["watch_status_display"] = dict(JournalEntry.WatchStatus.choices).get(row["watch_status"], row["watch_status"])
    image = JournalEntry(poster_file=row["poster_file"], poster_url=row["poster_url"], custom_poster_url=row["custom_poster_url"])
    record["poster"] = JournalEntrySerializer(context={"request": request}).get_poster(image)
    record["revision"] = revision(row)
    record["detail_url"] = f"{scope.base}entries/{row['id']}/"
    record["fields"] = {
        name: {"complete": bool(row[f"_{name}_complete"]), "kind": "json" if name in {"tags", "tag_colors"} else "string", "length": row[f"_{name}_length"], "field_url": f"{record['detail_url']}fields/{name}/?{urlencode({'revision': record['revision']})}"}
        for name in FIELDS
    }
    return record


def add_preset_colors(records):
    """Fetch only presets used by this bounded batch of preview tags."""
    names = {tag for record in records for tag in record["tags"] if isinstance(tag, str) and len(tag) <= 40}
    colors = dict(TagDefinition.objects.filter(is_quick_preset=True, name__in=names).values_list("name", "color")) if names else {}
    for record in records:
        record["preset_colors"] = {tag: colors[tag] for tag in record["tags"] if tag in colors}
