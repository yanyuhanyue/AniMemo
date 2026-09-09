"""Bounded page, aggregate, facet and field readers; no snapshot or write side."""
from datetime import datetime
from itertools import groupby
from urllib.parse import urlencode

from django.contrib.auth import get_user_model

from journal.models import JournalEntry, UserSettings, WatchHistoryRecord

from .contracts import (
    DEFAULT_PAGE_SIZE,
    DEFAULT_PART_SIZE,
    FIELDS,
    MAX_PAGE_SIZE,
    MAX_PART_SIZE,
    SCHEMA,
    SUCCESS_BYTES,
    CatalogError,
    PublicQuery,
    bounded_text,
    check_params,
    digest,
    issue_token,
    positive_int,
    read_token,
)
from .query import (
    Scope,
    add_preset_colors,
    authority,
    collation,
    dto,
    filter_expression,
    history_count,
    key_position,
    keyset,
    ordering,
    profile,
    projection,
    reference_position,
    revision,
    rows,
    table,
)

QUERY_PARAMS = {"search", "tag", "tag_ref", "status", "year", "year_ref", "quick", "sort"}


def envelope(scope, **values):
    return {"schema": SCHEMA, "consistency": "live", "scope": scope.wire, **values}


def counts(scope, query):
    source, where, params = authority(scope)
    condition, arguments = filter_expression(scope, query)
    return rows(f"SELECT count(*) AS total, count(*) FILTER (WHERE {condition}) AS matched_count FROM {source} WHERE {where}", [*arguments, *params])[0]


def _stats_select(alias="e"):
    return f"""count(*) AS total,
        count(*) FILTER (WHERE {alias}.watch_status = 'completed') AS completed_count,
        count(*) FILTER (WHERE {alias}.tags ? '剧场版') AS movie_count,
        count(*) FILTER (WHERE {alias}.tags ? 'OVA' OR strpos(upper({alias}.title), 'OVA') > 0) AS ova_count,
        count(*) FILTER (WHERE {alias}.tags ? '泡面番') AS short_count,
        count(*) FILTER (WHERE {alias}.personal_score >= 9.5) AS masterpiece_count,
        count(*) FILTER (WHERE {alias}.watch_status = 'planned' OR {alias}.personal_score IS NULL OR strpos({alias}.airing_period, '待定') > 0 OR {alias}.airing_period = '未定档') AS pending_count,
        count(*) FILTER (WHERE {alias}.personal_score IS NULL OR {alias}.personal_score <= 0) AS unscored_count"""


def _stats(row, average=0):
    names = ("total", "completed_count", "movie_count", "ova_count", "short_count", "masterpiece_count", "pending_count")
    result = {key: row.get(key, 0) for key in names}
    result["average_score"] = average
    return result


def _score_rows(scope, owner_ids=None):
    """Bounded scalar scan in the legacy order, including its float sum order."""
    position = None
    while True:
        source, where, arguments = authority(scope)
        where += " AND e.personal_score IS NOT NULL"
        if owner_ids is not None:
            where += " AND e.user_id IN (" + ",".join("%s" for _ in owner_ids) + ")"
            arguments += owner_ids
        if position is not None:
            where += " AND (e.user_id > %s OR (e.user_id = %s AND (e.updated_at < %s OR (e.updated_at = %s AND e.id < %s))))"
            arguments += [position["user_id"], position["user_id"], position["updated_at"], position["updated_at"], position["id"]]
        batch = rows(f"SELECT e.user_id, e.id, e.updated_at, e.personal_score FROM {source} WHERE {where} ORDER BY e.user_id, e.updated_at DESC, e.id DESC LIMIT 256", arguments)
        if not batch:
            return
        yield from batch
        if len(batch) < 256:
            return
        position = batch[-1]


def _average_score(records):
    count = 0

    def values():
        nonlocal count
        for row in records:
            count += 1
            yield float(row["personal_score"])

    # Keep built-in sum on this runtime, rather than avg(numeric): binary
    # float boundary rounding is part of the existing public stats result.
    score_sum = sum(values())
    return round(score_sum / count, 2)


def _averages(scope, owner_ids=None):
    return {owner_id: _average_score(group)
            for owner_id, group in groupby(_score_rows(scope, owner_ids), key=lambda row: row["user_id"])}


def read_summary(scope, params, render):
    check_params(params, QUERY_PARAMS)
    query = PublicQuery.parse(params)
    source, where, arguments = authority(scope)
    condition, parameters = filter_expression(scope, query)
    statistics = rows(f"SELECT {_stats_select()}, count(*) FILTER (WHERE {condition}) AS matched_count FROM {source} WHERE {where}", [*parameters, *arguments])[0]
    average = _averages(scope).get(scope.owner_id, 0)
    payload = envelope(scope, total=statistics["total"], matched_count=statistics["matched_count"], stats=_stats(statistics, average), unscored_count=statistics["unscored_count"], profile=scope.profile)
    _require_budget(payload, render)
    return payload


def _require_budget(payload, render):
    if len(render(payload)) > SUCCESS_BYTES:
        raise CatalogError("public_catalog_budget_exceeded", 500)


def _fit_page(scope, candidates, page_size, context, purpose, position, render, *, totals=None, key="results", extra=None):
    selected = list(candidates[:page_size])
    while True:
        has_more = len(selected) < len(candidates)
        token = issue_token(purpose, context, position(selected[-1])) if has_more and selected else None
        payload = envelope(scope, **(totals or {}), **(extra or {}), **{key: [row["payload"] for row in selected]}, next_cursor=token)
        if key == "results":
            payload["page_count"] = len(selected)
        else:
            payload["complete"] = not has_more
        if len(render(payload)) <= SUCCESS_BYTES:
            return payload
        if len(selected) <= 1:
            raise CatalogError("public_catalog_budget_exceeded", 500)
        selected.pop()


def _entry_rows(scope, query, *, limit, position=None):
    source, where, arguments = authority(scope)
    filters, parameters = filter_expression(scope, query)
    where += " AND (" + filters + ")"
    arguments += parameters
    if position is not None:
        after, parameters = keyset(query.sort, position)
        where += " AND " + after
        arguments += parameters
    order = ", ".join(f"{expression} {'DESC' if descending else 'ASC'}" for expression, descending, _name in ordering(query.sort))
    return rows(f"SELECT {projection()} FROM {source} WHERE {where} ORDER BY {order} LIMIT %s", [*arguments, limit])


def read_entries(scope, params, request, render, collation_version):
    check_params(params, QUERY_PARAMS | {"page_size", "cursor"})
    query = PublicQuery.parse(params)
    size = positive_int(params.get("page_size"), default=DEFAULT_PAGE_SIZE, maximum=MAX_PAGE_SIZE)
    context = {"scope": scope.identity, "query": query.identity, "collation": collation_version}
    after = read_token(params["cursor"], "entries", context) if params.get("cursor") else None
    total = counts(scope, query)
    found = _entry_rows(scope, query, limit=size + 1, position=after)
    candidates = [{"payload": dto(row, scope, request), "position": key_position(row, query.sort)} for row in found]
    add_preset_colors([candidate["payload"] for candidate in candidates])
    return _fit_page(scope, candidates, size, context, "entries", lambda row: row["position"], render, totals=total)


def read_detail(scope, entry_id, params, request, render, collation_version):
    check_params(params, {"revision"})
    source, where, arguments = authority(scope)
    found = rows(f"SELECT {projection()} FROM {source} WHERE {where} AND e.id = %s", [*arguments, entry_id])
    if not found:
        raise CatalogError("not_found", 404)
    entry = dto(found[0], scope, request)
    add_preset_colors([entry])
    if params.get("revision") and bounded_text(params["revision"], 128) != entry["revision"]:
        raise CatalogError("public_catalog_revision_changed", 409)
    export_context = {"scope": scope.identity, "query": PublicQuery(sort="id-asc").identity, "collation": collation_version}
    payload = envelope(scope, entry=entry, next_export_cursor=issue_token("entries", export_context, key_position(found[0], "id-asc")))
    _require_budget(payload, render)
    return payload


def _field_position(params, context):
    if params.get("cursor"):
        value = read_token(params["cursor"], "field", context)
        if set(value) != {"offset"} or type(value["offset"]) is not int or not 0 <= value["offset"] < 2 ** 31:
            raise CatalogError("public_catalog_cursor_invalid")
        return value["offset"]
    return 0


def _read_fragment(scope, entry_id, field, expected_revision, expression, expression_params, params, render, *, context_extra=None):
    if not expected_revision:
        raise CatalogError()
    bounded_text(expected_revision, 128)
    context = {"scope": scope.identity, "entry": entry_id, "field": field, "revision": expected_revision, **(context_extra or {})}
    offset = _field_position(params, context)
    size = positive_int(params.get("part_size"), default=DEFAULT_PART_SIZE, maximum=MAX_PART_SIZE)
    source, where, arguments = authority(scope)
    # The client receives only this substring. A JSON scalar can cross frames;
    # offsets are PostgreSQL/Python Unicode codepoints, never UTF-16 positions.
    found = rows(
        f"SELECT e.id, concat(e.xmin::text, ':', e.ctid::text) AS _row_version, {history_count()} AS watch_history_count, substring({expression} FROM %s FOR %s) AS fragment, char_length({expression}) AS total_length FROM {source} WHERE {where} AND e.id = %s",
        [*expression_params, offset + 1, size, *expression_params, *arguments, entry_id],
    )
    if not found:
        raise CatalogError("not_found", 404)
    row = found[0]
    if revision(row) != expected_revision:
        raise CatalogError("public_catalog_revision_changed", 409)
    if row["total_length"] is None or offset > row["total_length"]:
        raise CatalogError("public_catalog_cursor_invalid")
    fragment = row["fragment"] or ""
    next_offset = offset + len(fragment)
    complete = next_offset >= row["total_length"]
    token = None if complete else issue_token("field", context, {"offset": next_offset})
    payload = envelope(scope, entry_id=entry_id, field=field, revision=expected_revision, encoding="json-text", offset=offset, fragment=fragment, total_length=row["total_length"], next_cursor=token, complete=complete)
    _require_budget(payload, render)
    return payload


def read_field(scope, entry_id, field, params, render):
    check_params(params, {"revision", "cursor", "part_size"})
    if field not in FIELDS:
        raise CatalogError("not_found", 404)
    expression = f"e.{field}::text" if field in {"tags", "tag_colors"} else f"to_json(e.{field})::text"
    return _read_fragment(scope, entry_id, field, params.get("revision"), expression, [], params, render)


def _facet_cte(scope, kind):
    source, where, params = authority(scope)
    if kind == "tags":
        candidate = f"SELECT e.id, e.updated_at, concat(e.xmin::text, ':', e.ctid::text) AS _row_version, jt.ordinal, jt.value #>> '{{}}' AS value FROM {source} CROSS JOIN LATERAL jsonb_array_elements(CASE WHEN jsonb_typeof(e.tags) = 'array' THEN e.tags ELSE '[]'::jsonb END) WITH ORDINALITY jt(value, ordinal) WHERE {where} AND jsonb_typeof(jt.value) = 'string'"
    else:
        candidate = f"SELECT e.id, e.updated_at, concat(e.xmin::text, ':', e.ctid::text) AS _row_version, 1::bigint AS ordinal, split_part(COALESCE(NULLIF(e.airing_period, ''), '未定档'), '-', 1) AS value FROM {source} WHERE {where}"
    # Exact JSON strings remain distinct; only presentation uses zh-CN. The
    # first original -updated_at/-id/tag-order occurrence supplies stable ties.
    numbers = "CASE WHEN value ~ '^[0-9]+([.][0-9]+)?$' THEN 0 ELSE 1 END AS numeric_bucket, CASE WHEN value ~ '^[0-9]+([.][0-9]+)?$' THEN value::numeric ELSE 0 END AS numeric_value" if kind == "years" else "0 AS numeric_bucket, 0 AS numeric_value"
    nonempty = "WHERE value <> ''" if kind == "years" else ""
    cte = f"WITH candidates AS ({candidate}), unique_values AS (SELECT DISTINCT ON (value COLLATE \"C\") * FROM candidates {nonempty} ORDER BY value COLLATE \"C\", updated_at DESC, id DESC, ordinal ASC), facets AS (SELECT *, {numbers} FROM unique_values), positions AS (SELECT *, {numbers} FROM candidates)"
    return cte, params


def _facet_sort(kind, alias="f"):
    common = [(f"{alias}.value COLLATE {collation()}", False), (f"{alias}.updated_at", True), (f"{alias}.id", True), (f"{alias}.ordinal", False)]
    return [(f"{alias}.numeric_bucket", False), (f"{alias}.numeric_value", True), *common] if kind == "years" else common


def _facet_after(kind):
    keys = _facet_sort(kind)
    other = _facet_sort(kind, "last")
    terms = []
    for index, ((expression, descending), (previous, _desc)) in enumerate(zip(keys, other)):
        equal = [f"{left} = {right}" for (left, _), (right, _) in zip(keys[:index], other[:index])]
        equal.append(f"{expression} {'<' if descending else '>'} {previous}")
        terms.append("(" + " AND ".join(equal) + ")")
    return "(" + " OR ".join(terms) + ")"


def read_facets(scope, params, render, collation_version):
    check_params(params, {"kind", "search", "page_size", "cursor", "value_token", "revision", "part_size"})
    kind = params.get("kind", "tags")
    if kind not in {"tags", "years"}:
        raise CatalogError()
    if params.get("value_token"):
        position = reference_position(scope, params["value_token"], kind)
        expression = "to_json(e.tags ->> %s)::text" if kind == "tags" else "to_json(split_part(COALESCE(NULLIF(e.airing_period, ''), '未定档'), '-', 1))::text"
        expression_params = [position["ordinal"] - 1] if kind == "tags" else []
        return _read_fragment(scope, position["id"], "tag" if kind == "tags" else "year", position["revision"], expression, expression_params, params, render, context_extra={"ordinal": position["ordinal"]})
    if params.get("revision") or params.get("part_size"):
        raise CatalogError()
    size = positive_int(params.get("page_size"), default=DEFAULT_PAGE_SIZE, maximum=MAX_PAGE_SIZE)
    search = bounded_text(params.get("search", ""))
    context = {"scope": scope.identity, "kind": kind, "search": digest(search), "collation": collation_version, "order": "facets-v1-numeric-then-zh"}
    last = read_token(params["cursor"], "facets", context) if params.get("cursor") else None
    cte, arguments = _facet_cte(scope, kind)
    where = f"strpos(lower(f.value COLLATE {collation()}) COLLATE \"C\", lower(%s COLLATE {collation()}) COLLATE \"C\") > 0"
    arguments.append(search)
    join = ""
    if last is not None:
        if set(last) != {"value_token"}:
            raise CatalogError("public_catalog_cursor_invalid")
        position = reference_position(scope, last["value_token"], kind)
        join = " CROSS JOIN positions last"
        where += " AND last.id = %s AND last.ordinal = %s AND " + _facet_after(kind)
        arguments += [position["id"], position["ordinal"]]
    order = ", ".join(f"{expression} {'DESC' if descending else 'ASC'}" for expression, descending in _facet_sort(kind))
    source = f"SELECT f.id, f.ordinal, f._row_version, left(f.value, 160) AS preview, char_length(f.value) <= 160 AS complete, f.updated_at, (SELECT count(*) FROM {table(WatchHistoryRecord)} wh WHERE wh.entry_id = f.id) AS watch_history_count FROM facets f{join} WHERE {where} ORDER BY {order} LIMIT %s"
    found = rows(cte + " " + source, [*arguments, size + 1])
    candidates = []
    for row in found:
        rev = revision(row)
        value_token = issue_token("facet-value", {"scope": scope.identity, "kind": kind}, {"id": row["id"], "ordinal": row["ordinal"], "revision": rev})
        value = {"value": row["preview"] if row["complete"] else None, "preview": row["preview"], "complete": bool(row["complete"]), "selection_token": value_token, "revision": rev, "field_url": scope.base + "facets/?" + urlencode({"kind": kind, "value_token": value_token})}
        candidates.append({"payload": value, "position": {"value_token": value_token}})
    return _fit_page(scope, candidates, size, context, "facets", lambda row: row["position"], render, key="values", extra={"kind": kind, "facet_scope": "authorized", "order": "facets-v1-numeric-then-zh"})


def read_directory(scope, params, request, render, collation_version):
    check_params(params, {"search", "page_size", "cursor"})
    size = positive_int(params.get("page_size"), default=DEFAULT_PAGE_SIZE, maximum=MAX_PAGE_SIZE)
    search = bounded_text(params.get("search", "")).strip()
    context = {"scope": scope.identity, "search": digest(search), "collation": collation_version}
    position = read_token(params["cursor"], "directory", context) if params.get("cursor") else None
    users, settings_table, entries = table(get_user_model()), table(UserSettings), table(JournalEntry)
    source = f"{settings_table} s JOIN {users} u ON u.id = s.user_id"
    where = f"u.is_active AND s.allow_sharing AND s.public_status = 'approved' AND EXISTS (SELECT 1 FROM {entries} e WHERE e.user_id = s.user_id AND e.deleted_at IS NULL AND e.visibility = 'public')"
    condition = "(" + " OR ".join(f"strpos(lower({column} COLLATE {collation()}) COLLATE \"C\", lower(%s COLLATE {collation()}) COLLATE \"C\") > 0" for column in ("s.nickname", "u.username", "s.showcase_subtitle")) + ")"
    total = rows(f"SELECT count(*) AS total, count(*) FILTER (WHERE {condition}) AS matched_count FROM {source} WHERE {where}", [search] * 3)[0]
    arguments = [search] * 3
    where += " AND " + condition
    if position:
        try:
            if set(position) != {"updated", "id"} or type(position["id"]) is not int or position["id"] < 1:
                raise ValueError
            updated = datetime.fromisoformat(position["updated"])
            if updated.tzinfo is None:
                raise ValueError
        except (TypeError, ValueError):
            raise CatalogError("public_catalog_cursor_invalid") from None
        where += " AND (s.updated_at < %s OR (s.updated_at = %s AND s.id < %s))"
        arguments += [updated, updated, position["id"]]
    owners = rows(f'SELECT s.id, s.user_id, s.nickname, u.username AS "user__username", s.showcase_subtitle, s.avatar, s.accent, s.public_slug, s.updated_at FROM {source} WHERE {where} ORDER BY s.updated_at DESC, s.id DESC LIMIT %s', [*arguments, size + 1])
    ids = [owner["user_id"] for owner in owners]
    statistics, picks = {}, {}
    if ids:
        entry_source, entry_where, entry_params = authority(scope)
        placeholders = ",".join("%s" for _ in ids)
        entry_where += f" AND e.user_id IN ({placeholders})"
        arguments = [*entry_params, *ids]
        averages = _averages(scope, ids)
        for row in rows(f"SELECT e.user_id, {_stats_select()} FROM {entry_source} WHERE {entry_where} GROUP BY e.user_id", arguments):
            statistics[row["user_id"]] = _stats(row, averages.get(row["user_id"], 0))
        ranked = f"WITH ranked AS (SELECT e.id, row_number() OVER (PARTITION BY e.user_id ORDER BY e.personal_score DESC, e.updated_at DESC, e.id DESC) AS rank FROM {entry_source} WHERE {entry_where} AND e.personal_score IS NOT NULL) SELECT {projection()} FROM ranked JOIN {entries} e ON e.id = ranked.id WHERE ranked.rank <= 3 ORDER BY e.user_id, ranked.rank"
        owner_map = {owner["user_id"]: owner for owner in owners}
        for row in rows(ranked, arguments):
            owner = owner_map[row["user_id"]]
            item_scope = Scope("showcase", row["user_id"], "public", str(owner["public_slug"]))
            picks.setdefault(row["user_id"], []).append(dto(row, item_scope, request))
        add_preset_colors([entry for owner_picks in picks.values() for entry in owner_picks])
    candidates = []
    for owner in owners:
        item = profile(owner, request)
        item.update(username=owner["user__username"], stats=statistics.get(owner["user_id"], _stats({})), top_picks=picks.get(owner["user_id"], []))
        candidates.append({"payload": item, "position": {"updated": owner["updated_at"].isoformat(), "id": owner["id"]}})
    return _fit_page(scope, candidates, size, context, "directory", lambda row: row["position"], render, totals=total)
