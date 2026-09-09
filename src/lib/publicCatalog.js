import { createAuthSessionChangedError } from "./authSession.js";

export const PUBLIC_CATALOG_SCHEMA = "animemo.public-catalog/v1";
export const PUBLIC_EXPORT_SCHEMA = "animemo.public-catalog-export/v1";
export const PUBLIC_PAGE_CACHE_LIMIT = 5;
export const PUBLIC_RESPONSE_BYTES = 524288;
export const PUBLIC_TOKEN_BYTES = 4096;
export const PUBLIC_FIELD_FRAME_BYTES = 65536;
export const PUBLIC_FIELD_CACHE_BYTES = 262144;
const FIELD_CACHE_LIMIT = 3;
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const SORTS = new Set(["date-desc", "date-asc", "score-desc", "score-asc"]);
const FIELD_NAMES = Object.freeze([
  "id", "title", "japanese_title", "airing_period", "studio", "episodes", "description",
  "poster_url", "poster", "baike_url", "tags", "tag_colors", "personal_score", "watch_status",
  "watch_status_display", "review", "visibility", "watch_history_count", "created_at", "updated_at",
]);
const encoder = new TextEncoder();
export const utf8Bytes = (value) => encoder.encode(String(value)).byteLength;
const jsonBytes = (value) => utf8Bytes(JSON.stringify(value));
const codepointLength = (value) => { let size = 0; for (const _character of value) size += 1; return size; };
const nonnegative = (value) => Number.isSafeInteger(value) && value >= 0;

function failure(code, detail) {
  const error = new Error(detail);
  error.code = code;
  return error;
}

export function publicCatalogFailure(error) {
  const status = error?.response?.status ?? error?.status ?? null;
  const code = String(error?.response?.data?.code || error?.code || "public_catalog_unavailable").slice(0, 64);
  let detail = "公开数据暂时不可用，请刷新后重试。";
  if (code === "AUTH_SESSION_CHANGED") detail = "登录状态已变化，请刷新公开页面。";
  else if (code.startsWith("PUBLIC_")) detail = String(error.message || detail).slice(0, 300);
  else if (status === 410) detail = "读取凭证已过期，请刷新或重新选择筛选条件。";
  else if (status === 404) detail = "内容已撤回或当前不可访问。";
  else if (status === 409) detail = "内容已更新，请重新打开详情。";
  else if (typeof error?.response?.data?.detail === "string") detail = error.response.data.detail.slice(0, 300);
  return { code, detail, status };
}

export function publicCatalogBase({ kind = "homepage", publicSlug = "" } = {}) {
  if (kind === "directory") return "public/showcases/";
  if (kind === "homepage") return "public/homepage/";
  if (kind === "showcase" && UUID.test(publicSlug)) return `public/showcase/${publicSlug.toLowerCase()}/`;
  throw failure("PUBLIC_SCOPE_INVALID", "公开手账链接无效。");
}

function token(value) {
  if (value === null || value === undefined || value === "") return null;
  if (typeof value !== "string" || utf8Bytes(value) > PUBLIC_TOKEN_BYTES) throw failure("PUBLIC_TOKEN_INVALID", "读取凭证无效，请刷新后重试。");
  return value;
}

export function publicCatalogQuery(filters = {}) {
  const pageSize = Number(filters.page_size ?? 50);
  if (!Number.isSafeInteger(pageSize) || pageSize < 1 || pageSize > 100) throw failure("PUBLIC_PAGE_SIZE_INVALID", "每页显示数量应为 1 至 100 条。");
  const sort = filters.sort || "date-desc";
  if (!SORTS.has(sort)) throw failure("PUBLIC_SORT_INVALID", "排序方式无效。");
  const query = { page_size: pageSize, sort };
  for (const name of ["search", "tag", "status", "year", "quick"]) {
    const source = String(filters[name] ?? "");
    const value = ["tag", "year"].includes(name) ? source : source.trim();
    if (utf8Bytes(value) > PUBLIC_TOKEN_BYTES) throw failure("PUBLIC_QUERY_INVALID", "筛选条件过长，请缩短搜索词或重新选择筛选值。");
    if (value && (name === "search" || value !== "all")) query[name] = value;
  }
  for (const name of ["tag_ref", "year_ref"]) {
    const value = token(filters[name]);
    if (value) { query[name] = value; delete query[name.replace("_ref", "")]; }
  }
  return query;
}

function facetNeedsReference(item) {
  return !item.complete || item.value === "" || item.value === "all";
}

export function publicFacetOptionKey(item) {
  return facetNeedsReference(item) ? item.selection_token : `value:${item.value}`;
}

export function publicFacetFilters(name, item) {
  const reference = item && facetNeedsReference(item);
  return { [name]: item && !reference ? item.value : "all",
    [`${name}_ref`]: reference ? item.selection_token : "",
    [`${name}_label`]: item ? item.preview || "（空值）" : "" };
}

function blankList() {
  return { status: "idle", results: [], total: null, matched_count: null, page_count: 0,
    pageIndex: 0, next_cursor: null, previousAvailable: false, historyEvicted: false, duplicates: 0, error: null };
}
function blankFacet() {
  return { status: "idle", values: [], search: "", pageIndex: 0, next_cursor: null, complete: false, previousAvailable: false, error: null };
}
export function initialPublicCatalogState() {
  return { list: blankList(), summary: { status: "idle", stats: null, profile: null, error: null },
    facets: { tags: blankFacet(), years: blankFacet() }, detail: { status: "idle", entry: null, error: null },
    field: { status: "idle", target: null, frame: null, previousAvailable: false, error: null },
    exporting: { status: "idle", records: 0, bytes: 0, error: null }, scope: null };
}

function scopeKey(scope) { return JSON.stringify([scope.kind, scope.owner_id, scope.visibility, scope.public_slug]); }
function validateEntry(entry) {
  if (!entry || !Number.isSafeInteger(entry.id) || entry.id < 1 || typeof entry.revision !== "string"
    || !entry.revision || entry.revision.length > 256 || !entry.fields || typeof entry.fields !== "object") {
    throw failure("PUBLIC_RESPONSE_INVALID", "公开记录响应不完整，请刷新后重试。");
  }
  for (const [name, field] of Object.entries(entry.fields)) {
    if (!FIELD_NAMES.includes(name) || !field || typeof field.complete !== "boolean"
      || !["string", "json"].includes(field.kind) || !nonnegative(field.length)) {
      throw failure("PUBLIC_RESPONSE_INVALID", "公开字段描述无效。");
    }
  }
  const safe = {};
  for (const name of FIELD_NAMES) if (Object.hasOwn(entry, name)) safe[name] = entry[name];
  return { ...safe, revision: entry.revision, detail_url: entry.detail_url, fields: entry.fields,
    preset_colors: entry.preset_colors && typeof entry.preset_colors === "object" ? entry.preset_colors : {} };
}

/** Decode a JSON string across codepoint fragments without retaining earlier text. */
export function decodeJsonStringFragment(fragment, state = { mode: "start", hex: "", high: "" }, final = false) {
  const next = { ...state };
  let text = "";
  const emit = (character) => {
    const unit = character.charCodeAt(0);
    if (character.length === 1 && unit >= 0xd800 && unit <= 0xdbff) {
      text += next.high;
      next.high = character;
    } else {
      text += next.high + character;
      next.high = "";
    }
  };
  for (const character of fragment) {
    if (next.mode === "start") {
      if (/\s/.test(character)) continue;
      if (character !== '"') throw failure("PUBLIC_FIELD_INVALID", "字段分段无法读取，请重新打开详情。");
      next.mode = "string";
    } else if (next.mode === "string") {
      if (character === "\\") next.mode = "escape";
      else if (character === '"') { text += next.high; next.high = ""; next.mode = "done"; }
      else emit(character);
    } else if (next.mode === "escape") {
      if (character === "u") { next.mode = "unicode"; next.hex = ""; }
      else {
        const escapes = { '"': '"', "\\": "\\", "/": "/", b: "\b", f: "\f", n: "\n", r: "\r", t: "\t" };
        if (!Object.hasOwn(escapes, character)) throw failure("PUBLIC_FIELD_INVALID", "字段转义无效。");
        emit(escapes[character]);
        next.mode = "string";
      }
    } else if (next.mode === "unicode") {
      if (!/^[0-9a-f]$/i.test(character)) throw failure("PUBLIC_FIELD_INVALID", "字段转义无效。");
      next.hex += character;
      if (next.hex.length === 4) { emit(String.fromCharCode(parseInt(next.hex, 16))); next.hex = ""; next.mode = "string"; }
    } else if (!/\s/.test(character)) throw failure("PUBLIC_FIELD_INVALID", "字段分段存在额外内容。");
  }
  if (final && next.mode !== "done") throw failure("PUBLIC_FIELD_INVALID", "字段分段尚不完整。");
  return { text, state: next };
}

export function createPublicCatalogClient({ api, kind = "homepage", publicSlug = "", getActorId = () => null, onChange = () => {} }) {
  const base = publicCatalogBase({ kind, publicSlug });
  let snapshot = initialPublicCatalogState();
  let query = publicCatalogQuery();
  let epoch = 0;
  let disposed = false;
  let authGeneration = null;
  let actorId = getActorId();
  let boundScope = null;
  const requests = new Map();
  const pageCache = new Map();
  const facetCaches = { tags: new Map(), years: new Map() };
  const fieldCache = new Map();
  let fieldTargetKey = "";

  function publish(changes) { if (!disposed) { snapshot = { ...snapshot, ...changes }; onChange(snapshot); } }
  function publishFacet(name, changes) {
    publish({ facets: { ...snapshot.facets, [name]: { ...snapshot.facets[name], ...changes } } });
  }
  function abortAll(reason = failure("PUBLIC_CANCELLED", "读取已取消。")) {
    epoch += 1;
    for (const operation of requests.values()) operation.controller.abort(reason);
    requests.clear();
  }
  function clear() {
    pageCache.clear(); facetCaches.tags.clear(); facetCaches.years.clear(); fieldCache.clear();
    fieldTargetKey = ""; boundScope = null;
    publish(initialPublicCatalogState());
  }
  function context(channel) {
    requests.get(channel)?.controller.abort(failure("PUBLIC_CANCELLED", "读取已由更新请求替代。"));
    const operation = { channel, epoch, controller: new AbortController() };
    requests.set(channel, operation);
    return operation;
  }
  function current(operation) {
    return !disposed && operation.epoch === epoch && requests.get(operation.channel) === operation && !operation.controller.signal.aborted;
  }
  function assertCurrent(operation) {
    if (String(getActorId() ?? "") !== String(actorId ?? "")) throw createAuthSessionChangedError();
    if (!current(operation)) throw failure("PUBLIC_CANCELLED", "读取已取消。");
  }
  function finish(operation) { if (requests.get(operation.channel) === operation) requests.delete(operation.channel); }
  function checkEnvelope(data) {
    if (!data || data.schema !== PUBLIC_CATALOG_SCHEMA || data.consistency !== "live" || jsonBytes(data) > PUBLIC_RESPONSE_BYTES
      || !data.scope || data.scope.kind !== kind || !["public", "owner"].includes(data.scope.visibility)
      || data.scope.owner_id !== null && (!Number.isSafeInteger(data.scope.owner_id) || data.scope.owner_id < 1)
      || kind === "showcase" && String(data.scope.public_slug).toLowerCase() !== publicSlug.toLowerCase()
      || kind !== "showcase" && data.scope.visibility === "owner"
      || data.scope.visibility === "owner" && String(data.scope.owner_id) !== String(actorId)) {
      throw failure("PUBLIC_RESPONSE_INVALID", "公开读取响应无效，请刷新后重试。");
    }
    if (boundScope && scopeKey(data.scope) !== scopeKey(boundScope)) {
      abortAll();
      clear();
      publish({ list: { ...blankList(), status: "error", error: publicCatalogFailure(failure("PUBLIC_SCOPE_CHANGED", "公开范围已更新，请刷新页面。")) } });
      throw failure("PUBLIC_SCOPE_CHANGED", "公开范围已更新，请刷新页面。");
    }
    boundScope = data.scope;
    if (!snapshot.scope) publish({ scope: data.scope });
    return data;
  }
  async function request(operation, path, params = {}) {
    assertCurrent(operation);
    if (!path.startsWith(base) || path.includes("..") || /^https?:/i.test(path)) throw failure("PUBLIC_ROUTE_INVALID", "公开读取路径无效。");
    for (const key of ["cursor", "tag_ref", "year_ref", "value_token"]) if (params[key]) token(params[key]);
    let response;
    for (let attempt = 0; ; attempt += 1) {
      try {
        response = await api.get(path, { params, signal: operation.controller.signal, serverStateInvalidation: false,
          ...(authGeneration === null ? {} : { _authGeneration: authGeneration }) });
        break;
      } catch (error) {
        assertCurrent(operation);
        if (operation.channel !== "export" || error?.response?.status !== 429 || attempt >= 3) throw error;
        const raw = error.response.headers?.get?.("retry-after") ?? error.response.headers?.["retry-after"];
        const seconds = raw === undefined || raw === null ? 60 : Math.ceil(Number(raw));
        if (!Number.isFinite(seconds) || seconds < 0 || seconds >= 900) throw error;
        publish({ exporting: { ...snapshot.exporting, retryAfter: Math.max(1, seconds) } });
        await new Promise((resolve, reject) => {
          const signal = operation.controller.signal;
          const cleanup = () => { clearTimeout(timer); signal.removeEventListener("abort", abort); };
          const abort = () => { cleanup(); reject(signal.reason || failure("PUBLIC_CANCELLED", "读取已取消。")); };
          const timer = setTimeout(() => { cleanup(); resolve(); }, Math.max(1, seconds) * 1000);
          signal.addEventListener("abort", abort, { once: true });
          if (signal.aborted) abort();
        });
        assertCurrent(operation);
        publish({ exporting: { ...snapshot.exporting, retryAfter: null } });
      }
    }
    assertCurrent(operation);
    const generation = response.config?._authGeneration;
    if (generation !== undefined) {
      if (authGeneration !== null && authGeneration !== generation) throw createAuthSessionChangedError();
      authGeneration = generation;
    }
    return checkEnvelope(response.data);
  }
  function listEnvelope(data) {
    if (!Array.isArray(data.results) || data.results.length > 100 || ![data.total, data.matched_count, data.page_count].every(nonnegative)
      || data.page_count !== data.results.length || data.matched_count > data.total) throw failure("PUBLIC_RESPONSE_INVALID", "公开列表计数无效。");
    token(data.next_cursor);
    return data;
  }
  function normalizeRows(rows) {
    if (kind !== "directory") return rows.map(validateEntry);
    return rows.map((row) => {
      if (!UUID.test(row?.public_slug) || !Array.isArray(row.top_picks) || row.top_picks.length > 3) throw failure("PUBLIC_RESPONSE_INVALID", "公开手账目录响应无效。");
      return { nickname: row.nickname, username: row.username, subtitle: row.subtitle, avatar_url: row.avatar_url,
        public_slug: row.public_slug, stats: row.stats, top_picks: row.top_picks.map(validateEntry) };
    });
  }
  async function loadPage(pageIndex, cursor) {
    const operation = context("list");
    publish({ list: { ...snapshot.list, status: "loading", results: [], error: null } });
    try {
      const params = kind === "directory" ? { page_size: query.page_size, ...(query.search ? { search: query.search } : {}) } : query;
      const data = listEnvelope(await request(operation, kind === "directory" ? base : `${base}entries/`, { ...params, ...(cursor ? { cursor } : {}) }));
      const seen = new Set();
      for (const [index, page] of pageCache) if (index < pageIndex) for (const row of page.results) seen.add(kind === "directory" ? row.public_slug : row.id);
      let duplicates = 0;
      const results = normalizeRows(data.results).filter((row) => {
        const id = kind === "directory" ? row.public_slug : row.id;
        if (seen.has(id)) { duplicates += 1; return false; }
        seen.add(id); return true;
      });
      pageCache.delete(pageIndex);
      pageCache.set(pageIndex, { results, requestCursor: cursor, next_cursor: data.next_cursor });
      while (pageCache.size > PUBLIC_PAGE_CACHE_LIMIT) pageCache.delete(pageCache.keys().next().value);
      publish({ list: { status: "ready", results, total: data.total, matched_count: data.matched_count, page_count: data.page_count,
        pageIndex, next_cursor: data.next_cursor, previousAvailable: pageCache.has(pageIndex - 1), historyEvicted: pageIndex > 0 && !pageCache.has(pageIndex - 1), duplicates, error: null } });
      return results;
    } catch (error) {
      if (current(operation)) publish({ list: { ...snapshot.list, status: "error", results: [], error: publicCatalogFailure(error) } });
      throw error;
    } finally { finish(operation); }
  }
  async function loadSummary() {
    if (kind === "directory") return null;
    const operation = context("summary");
    publish({ summary: { status: "loading", stats: null, profile: null, error: null } });
    try {
      const { page_size: _pageSize, ...filters } = query;
      const data = await request(operation, `${base}summary/`, filters);
      if (!nonnegative(data.total) || !nonnegative(data.matched_count) || !data.stats || data.stats.total !== data.total) throw failure("PUBLIC_RESPONSE_INVALID", "公开统计响应无效。");
      publish({ summary: { status: "ready", stats: data.stats, profile: data.profile || null, total: data.total,
        matched_count: data.matched_count, unscored_count: nonnegative(data.unscored_count) ? data.unscored_count : null, error: null } });
      return data;
    } catch (error) {
      if (current(operation)) publish({ summary: { status: "error", stats: null, profile: null, error: publicCatalogFailure(error) } });
      throw error;
    } finally { finish(operation); }
  }
  async function loadFacet(name, { search = snapshot.facets[name]?.search || "", direction = "first" } = {}) {
    if (kind === "directory" || !Object.hasOwn(facetCaches, name)) return null;
    const previous = snapshot.facets[name];
    const cache = facetCaches[name];
    let pageIndex = 0;
    let cursor = null;
    if (direction === "next") { if (!previous.next_cursor) return null; cursor = previous.next_cursor; pageIndex = previous.pageIndex + 1; }
    else if (direction === "previous") {
      const prior = cache.get(previous.pageIndex - 1);
      if (!prior) throw failure("PUBLIC_HISTORY_EVICTED", "较早的筛选选项已移出缓存，请返回首组。");
      cursor = prior.cursor; pageIndex = previous.pageIndex - 1;
    } else cache.clear();
    const operation = context(`facet:${name}`);
    publishFacet(name, { status: "loading", values: [], search, error: null });
    try {
      const data = await request(operation, `${base}facets/`, { kind: name, search, page_size: 50, ...(cursor ? { cursor } : {}) });
      if (data.kind !== name || !Array.isArray(data.values) || data.values.length > 100) throw failure("PUBLIC_RESPONSE_INVALID", "筛选选项响应无效。");
      token(data.next_cursor);
      const values = data.values.map((item) => {
        if (!item || typeof item.preview !== "string" || typeof item.complete !== "boolean" || item.value !== null && typeof item.value !== "string") throw failure("PUBLIC_RESPONSE_INVALID", "筛选选项不完整。");
        token(item.selection_token);
        return { value: item.value, preview: item.preview, complete: item.complete, selection_token: item.selection_token,
          revision: item.revision, field_url: item.field_url };
      });
      cache.delete(pageIndex); cache.set(pageIndex, { cursor });
      while (cache.size > PUBLIC_PAGE_CACHE_LIMIT) cache.delete(cache.keys().next().value);
      publishFacet(name, { status: "ready", values, search, pageIndex, next_cursor: data.next_cursor,
        complete: Boolean(data.complete), previousAvailable: cache.has(pageIndex - 1), error: null });
      return values;
    } catch (error) {
      if (current(operation)) publishFacet(name, { status: "error", values: [], error: publicCatalogFailure(error) });
      throw error;
    } finally { finish(operation); }
  }
  function setQuery(filters, { facets = false } = {}) {
    query = publicCatalogQuery(filters);
    abortAll();
    pageCache.clear(); fieldCache.clear(); fieldTargetKey = "";
    publish({ list: blankList(), summary: { status: "idle", stats: null, profile: null, error: null },
      detail: { status: "idle", entry: null, error: null }, field: { status: "idle", target: null, frame: null, previousAvailable: false, error: null },
      exporting: { status: "idle", records: 0, bytes: 0, error: null } });
    const tasks = [loadPage(0, null), loadSummary()];
    for (const name of ["tags", "years"]) {
      if (facets || snapshot.facets[name].status === "loading") tasks.push(loadFacet(name));
    }
    return Promise.allSettled(tasks);
  }
  function refresh() { boundScope = null; return setQuery(query, { facets: kind !== "directory" }); }
  function nextPage() { return snapshot.list.next_cursor ? loadPage(snapshot.list.pageIndex + 1, snapshot.list.next_cursor) : Promise.resolve([]); }
  function previousPage() {
    const index = snapshot.list.pageIndex - 1;
    if (index < 0) return Promise.resolve([]);
    const page = pageCache.get(index);
    if (!page) return Promise.reject(failure("PUBLIC_HISTORY_EVICTED", "较早页面已移出缓存，请返回首页重新读取。"));
    return loadPage(index, page.requestCursor);
  }
  async function openDetail(id) {
    if (!Number.isSafeInteger(id) || id < 1 || kind === "directory") throw failure("PUBLIC_ENTRY_INVALID", "记录链接无效。");
    closeField();
    const operation = context("detail");
    publish({ detail: { status: "loading", entry: null, error: null } });
    try {
      const data = await request(operation, `${base}entries/${id}/`);
      const entry = validateEntry(data.entry);
      if (entry.id !== id) throw failure("PUBLIC_RESPONSE_INVALID", "详情身份不匹配。");
      publish({ detail: { status: "ready", entry, error: null } });
      return entry;
    } catch (error) {
      if (current(operation)) publish({ detail: { status: "error", entry: null, error: publicCatalogFailure(error) } });
      throw error;
    } finally { finish(operation); }
  }
  function closeField() {
    requests.get("field")?.controller.abort(); requests.delete("field");
    fieldCache.clear(); fieldTargetKey = "";
    publish({ field: { status: "idle", target: null, frame: null, previousAvailable: false, error: null } });
  }
  function closeDetail() {
    requests.get("detail")?.controller.abort(); requests.delete("detail");
    if (snapshot.exporting.kind === "field" && ["choosing", "writing"].includes(snapshot.exporting.status)) cancelExport();
    closeField(); publish({ detail: { status: "idle", entry: null, error: null } });
  }
  function entryTarget(entry, name) {
    if (!entry || !FIELD_NAMES.includes(name) || !entry.fields[name]) throw failure("PUBLIC_FIELD_INVALID", "该字段不可公开读取。");
    return { type: "entry", entryId: entry.id, field: name, revision: entry.revision, kind: entry.fields[name].kind,
      path: `${base}entries/${entry.id}/fields/${name}/`, params: {} };
  }
  function facetTarget(name, item) {
    if (!["tags", "years"].includes(name) || typeof item?.field_url !== "string") throw failure("PUBLIC_FIELD_INVALID", "筛选值读取路径无效。");
    const url = new URL(item.field_url, "https://public-catalog.invalid/");
    const path = url.pathname.replace(/^\/api\/(?:v1\/)?/, "").replace(/^\//, "");
    const valueToken = token(url.searchParams.get("value_token"));
    if (path !== `${base}facets/` || url.searchParams.get("kind") !== name || !valueToken) throw failure("PUBLIC_FIELD_INVALID", "筛选值读取路径无效。");
    return { type: "facet", facet: name, field: name === "tags" ? "tag" : "year", revision: item.revision, kind: "string", path,
      params: { kind: name, value_token: valueToken } };
  }
  function validateField(data, target, offset) {
    if (data.encoding !== "json-text" || data.field !== target.field || data.revision !== target.revision
      || target.type === "entry" && data.entry_id !== target.entryId
      || data.offset !== offset || typeof data.fragment !== "string" || utf8Bytes(data.fragment) > PUBLIC_FIELD_FRAME_BYTES
      || !nonnegative(data.total_length) || typeof data.complete !== "boolean") throw failure("PUBLIC_REVISION_CHANGED", "字段内容已变化，请重新打开详情。");
    const end = offset + codepointLength(data.fragment);
    token(data.next_cursor);
    if (end > data.total_length || data.complete !== (end === data.total_length) || Boolean(data.next_cursor) === data.complete
      || !data.complete && end === offset) throw failure("PUBLIC_FIELD_INVALID", "字段续读位置无效，请重新读取。");
    return end;
  }
  async function readField(target, direction = "first") {
    const key = JSON.stringify([target.type, target.entryId, target.facet, target.field, target.revision, target.params]);
    if (key !== fieldTargetKey) { closeField(); fieldTargetKey = key; }
    let pageIndex = 0;
    let cursor = null;
    let offset = 0;
    let decoder;
    const previous = snapshot.field.frame;
    if (direction === "next") {
      if (!previous?.next_cursor) return null;
      pageIndex = previous.pageIndex + 1; cursor = previous.next_cursor; offset = previous.end; decoder = previous.decoderEnd;
    } else if (direction === "previous") {
      const page = fieldCache.get((previous?.pageIndex || 0) - 1);
      if (!page) throw failure("PUBLIC_HISTORY_EVICTED", "较早字段片段已移出缓存，请从头读取或下载完整字段。");
      ({ pageIndex, cursor, offset, decoderStart: decoder } = page);
    } else fieldCache.clear();
    const operation = context("field");
    publish({ field: { status: "loading", target, frame: null, previousAvailable: false, error: null } });
    try {
      const data = await request(operation, target.path, { ...target.params, revision: target.revision, part_size: 4096, ...(cursor ? { cursor } : {}) });
      const end = validateField(data, target, offset);
      const decoded = target.kind === "string" ? decodeJsonStringFragment(data.fragment, decoder, data.complete) : { text: data.fragment, state: null };
      const frame = { pageIndex, cursor, offset, end, total_length: data.total_length, text: decoded.text,
        decoderStart: decoder, decoderEnd: decoded.state, next_cursor: data.next_cursor, complete: data.complete };
      fieldCache.delete(pageIndex); fieldCache.set(pageIndex, frame);
      while (fieldCache.size > FIELD_CACHE_LIMIT || [...fieldCache.values()].reduce((total, value) => total + jsonBytes(value), 0) > PUBLIC_FIELD_CACHE_BYTES) {
        fieldCache.delete(fieldCache.keys().next().value);
      }
      publish({ field: { status: "ready", target, frame, previousAvailable: fieldCache.has(pageIndex - 1), error: null } });
      return frame;
    } catch (error) {
      if (current(operation)) {
        fieldCache.clear();
        const safeError = publicCatalogFailure(error);
        const changes = { field: { status: "error", target, frame: null, previousAvailable: false, error: safeError } };
        if (target.type === "entry" && ([404, 409, 410].includes(safeError.status) || safeError.code === "PUBLIC_REVISION_CHANGED")) {
          changes.detail = { status: "error", entry: null, error: safeError };
        }
        publish(changes);
      }
      throw error;
    } finally { finish(operation); }
  }
  function readEntryField(name, direction = "first") {
    if (!snapshot.detail.entry) return Promise.reject(failure("PUBLIC_ENTRY_INVALID", "请先重新加载详情。"));
    return readField(entryTarget(snapshot.detail.entry, name), direction);
  }
  function readFacetValue(name, item, direction = "first") { return readField(facetTarget(name, item), direction); }

  async function streamField(operation, target, writer, progress) {
    let cursor = null;
    let offset = 0;
    do {
      const data = await request(operation, target.path, { ...target.params, revision: target.revision, part_size: 4096, ...(cursor ? { cursor } : {}) });
      offset = validateField(data, target, offset);
      await writer.write(data.fragment);
      assertCurrent(operation);
      progress(data.fragment);
      cursor = data.next_cursor;
    } while (cursor);
  }
  async function writeExport(writer, { field = null } = {}) {
    if (!writer || typeof writer.write !== "function" || typeof writer.close !== "function" || typeof writer.abort !== "function") throw failure("PUBLIC_EXPORT_UNAVAILABLE", "当前浏览器无法流式保存文件，请使用支持此功能的桌面浏览器。");
    const operation = context("export");
    let bytes = 0;
    let count = 0;
    const exportKind = field ? "field" : "all";
    const progress = (text) => { bytes += utf8Bytes(text); publish({ exporting: { status: "writing", kind: exportKind, records: count, bytes, error: null } }); };
    const write = async (text) => { assertCurrent(operation); await writer.write(text); assertCurrent(operation); progress(text); };
    publish({ exporting: { status: "writing", kind: exportKind, records: 0, bytes: 0, error: null } });
    try {
      if (field) {
        await streamField(operation, field, writer, progress);
      } else {
        if (kind === "directory") throw failure("PUBLIC_EXPORT_UNAVAILABLE", "请打开个人公开手账后导出。");
        // Export deliberately traverses fresh pages independently of the five-page UI cache.
        const summary = await request(operation, `${base}summary/`);
        await write(`{"schema":${JSON.stringify(PUBLIC_EXPORT_SCHEMA)},"consistency":"live","scope":${JSON.stringify(summary.scope)},"exported_at":${JSON.stringify(new Date().toISOString())},"profile":${JSON.stringify(summary.profile || null)},"records":[`);
        let cursor = null;
        do {
          const page = listEnvelope(await request(operation, `${base}entries/`, { page_size: 50, sort: "id-asc", ...(cursor ? { cursor } : {}) }));
          let lastEntry = null;
          for (const item of page.results) {
            assertCurrent(operation);
            validateEntry(item);
            const result = await request(operation, `${base}entries/${item.id}/`);
            const entry = validateEntry(result.entry);
            if (entry.id !== item.id) throw failure("PUBLIC_RESPONSE_INVALID", "导出详情身份不匹配。");
            await write(`${count ? "," : ""}{`);
            let fieldsWritten = 0;
            for (const name of FIELD_NAMES) {
              if (!Object.hasOwn(entry, name)) continue;
              await write(`${fieldsWritten ? "," : ""}${JSON.stringify(name)}:`);
              const descriptor = entry.fields[name];
              if (descriptor && !descriptor.complete) await streamField(operation, entryTarget(entry, name), writer, progress);
              else await write(JSON.stringify(entry[name]));
              fieldsWritten += 1;
            }
            await write("}");
            count += 1;
            lastEntry = entry;
            progress("");
          }
          cursor = null;
          if (page.next_cursor) {
            // Field reads and rate-limit pauses can outlive the list token.
            // Recheck the final entry after all its fields, then immediately
            // continue with a fresh, scope-bound id-asc token.
            const checkpoint = await request(operation, `${base}entries/${lastEntry.id}/`, { revision: lastEntry.revision });
            const currentEntry = validateEntry(checkpoint.entry);
            if (currentEntry.id !== lastEntry.id || currentEntry.revision !== lastEntry.revision || !checkpoint.next_export_cursor) {
              throw failure("PUBLIC_REVISION_CHANGED", "导出内容已变化，请重新开始下载。");
            }
            cursor = token(checkpoint.next_export_cursor);
          }
        } while (cursor);
        await write("]}");
      }
      assertCurrent(operation);
      publish({ exporting: { status: "finalizing", kind: exportKind, records: count, bytes, error: null } });
      await writer.close();
      assertCurrent(operation);
      publish({ exporting: { status: "complete", kind: exportKind, records: count, bytes, error: null } });
      return { records: count, bytes };
    } catch (error) {
      try { await writer.abort(); } catch { /* Preserve the original read/write failure. */ }
      if (current(operation)) publish({ exporting: { status: "error", kind: exportKind, records: count, bytes, error: publicCatalogFailure(error) } });
      throw error;
    } finally { finish(operation); }
  }
  function cancelExport() {
    if (snapshot.exporting.status === "finalizing") return;
    requests.get("export")?.controller.abort(failure("PUBLIC_EXPORT_CANCELLED", "导出已取消，文件未完成保存。"));
    requests.delete("export");
    publish({ exporting: { status: "cancelled", records: 0, bytes: 0, error: null } });
  }
  function authChanged(value) {
    const nextActor = value?.user?.id ?? null;
    const changed = String(actorId ?? "") !== String(nextActor ?? "")
      || authGeneration !== null && authGeneration !== value.generation
      || authGeneration === null && requests.size > 0;
    actorId = nextActor;
    authGeneration = value?.generation ?? null;
    if (changed) { abortAll(createAuthSessionChangedError()); clear(); }
    return changed;
  }
  function captureIntent() { return { epoch, actorId, authGeneration }; }
  function assertIntent(intent) {
    if (disposed || intent?.epoch !== epoch || intent.actorId !== actorId || intent.authGeneration !== authGeneration) {
      throw failure("PUBLIC_INTENT_CHANGED", "公开页面或登录状态已变化，请重新开始下载。");
    }
  }
  return Object.freeze({
    base, kind, getSnapshot: () => snapshot, getQuery: () => query, setQuery, refresh, nextPage, previousPage,
    loadFacet, openDetail, closeDetail, readEntryField, readFacetValue, closeField, authChanged,
    exportTo: (writer) => writeExport(writer),
    exportEntryFieldTo: (name, writer) => writeExport(writer, { field: entryTarget(snapshot.detail.entry, name) }),
    exportFacetValueTo: (name, item, writer) => writeExport(writer, { field: facetTarget(name, item) }),
    cancelExport,
    captureIntent, assertIntent,
    beginExportSelection() { publish({ exporting: { status: "choosing", records: 0, bytes: 0, error: null } }); },
    exportSelectionError(error, intent) {
      try { assertIntent(intent); } catch { return; }
      publish({ exporting: { status: error?.name === "AbortError" ? "idle" : "error", records: 0, bytes: 0,
        error: error?.name === "AbortError" ? null : publicCatalogFailure(error) } });
    },
    canAutoRefresh: () => !["choosing", "writing", "finalizing"].includes(snapshot.exporting.status)
      && snapshot.detail.status === "idle" && snapshot.field.status === "idle" && snapshot.list.status !== "loading"
      && snapshot.list.pageIndex === 0
      && Object.values(snapshot.facets).every((facet) => facet.pageIndex === 0 && !facet.search && facet.status !== "loading"),
    cacheInfo: () => ({ listPages: pageCache.size, fieldFrames: fieldCache.size,
      fieldBytes: [...fieldCache.values()].reduce((total, value) => total + jsonBytes(value), 0), facetCursors: facetCaches.tags.size + facetCaches.years.size }),
    dispose() { abortAll(); disposed = true; pageCache.clear(); fieldCache.clear(); facetCaches.tags.clear(); facetCaches.years.clear(); },
  });
}
