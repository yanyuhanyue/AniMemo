import assert from "node:assert/strict";
import test from "node:test";

import { createAuthSession } from "../src/lib/authSession.js";
import { createWebApiTransport } from "../src/lib/webApiTransport.js";
import {
  PUBLIC_CATALOG_SCHEMA, PUBLIC_EXPORT_SCHEMA, PUBLIC_FIELD_CACHE_BYTES, PUBLIC_FIELD_FRAME_BYTES,
  PUBLIC_PAGE_CACHE_LIMIT, createPublicCatalogClient, decodeJsonStringFragment, publicCatalogBase, publicCatalogQuery, publicFacetFilters, publicFacetOptionKey, utf8Bytes,
} from "../src/lib/publicCatalog.js";

const slug = "12345678-1234-4234-8234-123456789012";
const afterMicrotasks = () => new Promise((resolve) => setImmediate(resolve));
const deferred = () => { let resolve; let reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return { resolve, reject, promise }; };
const enc = (value) => JSON.stringify(value);
const chars = (value) => Array.from(value);

function fixture({ size = 501, kind = "homepage", actor = null, longText = "尾🪷\\\"汉\n".repeat(18000) } = {}) {
  let currentActor = actor;
  let generation = 5;
  let fault = null;
  const calls = [];
  const snapshots = [];
  const originals = Array.from({ length: size }, (_, index) => ({
    id: index + 1, title: `番剧-${String(index + 1).padStart(4, "0")}`, japanese_title: `作品-${index + 1}`,
    airing_period: index === size - 1 ? "2099-01" : "2026-01", studio: "动画制作", episodes: "12",
    description: index === size - 1 ? longText : `简介-${index + 1}`, review: index === size - 1 ? "首段\n\n末段" : "",
    poster_url: "https://media.animemo.cc/poster.webp", poster: "https://media.animemo.cc/poster.webp", baike_url: "",
    tags: index === size - 1 ? ["末页独有标签", "日常"] : ["日常"], tag_colors: { 日常: "blue" },
    personal_score: index % 2 === 0 ? "0.00" : null, watch_status: "planned", watch_status_display: "想看",
    visibility: "public", watch_history_count: 0, created_at: "2026-01-01T00:00:00Z", updated_at: "2026-01-01T00:00:00Z",
  }));
  const revision = new Map(originals.map((entry) => [entry.id, `revision-${entry.id}`]));
  const strings = new Map();
  function fieldText(entry, name) {
    const key = `${entry.id}:${name}:${revision.get(entry.id)}`;
    if (!strings.has(key)) strings.set(key, chars(enc(entry[name])));
    return strings.get(key);
  }
  function dto(entry) {
    const data = { fields: {}, revision: revision.get(entry.id), detail_url: `${publicCatalogBase({ kind, publicSlug: slug })}entries/${entry.id}/`, preset_colors: { 日常: "blue" } };
    for (const [name, value] of Object.entries(entry)) {
      const text = enc(value);
      const complete = utf8Bytes(text) <= 256;
      data[name] = complete ? value : typeof value === "string" ? chars(value).slice(0, 64).join("") : [];
      data.fields[name] = { complete, kind: typeof value === "string" ? "string" : "json", length: chars(text).length,
        field_url: `${publicCatalogBase({ kind, publicSlug: slug })}entries/${entry.id}/fields/${name}/` };
    }
    return data;
  }
  function scope() { return { kind, owner_id: kind === "directory" ? null : 7, visibility: kind === "showcase" && currentActor === 7 ? "owner" : "public", public_slug: kind === "showcase" ? slug : null }; }
  const envelope = (data) => ({ schema: PUBLIC_CATALOG_SCHEMA, consistency: "live", scope: scope(), ...data });
  const api = { async get(path, config = {}) {
    calls.push({ path, config });
    if (fault) {
      const override = fault(path, config);
      if (override !== undefined) return override;
    }
    const params = config.params || {};
    let data;
    const fieldMatch = path.match(/entries\/(\d+)\/fields\/([^/]+)\/$/);
    const detailMatch = path.match(/entries\/(\d+)\/$/);
    if (fieldMatch) {
      const id = Number(fieldMatch[1]);
      const name = fieldMatch[2];
      assert.equal(params.revision, revision.get(id));
      const entry = originals.find((item) => item.id === id);
      const text = fieldText(entry, name);
      const offset = params.cursor ? Number(params.cursor.replace("field-", "")) : 0;
      const fragment = text.slice(offset, offset + (params.part_size || 4096)).join("");
      const end = offset + chars(fragment).length;
      data = envelope({ entry_id: id, field: name, revision: revision.get(id), encoding: "json-text", offset,
        fragment, total_length: text.length, next_cursor: end < text.length ? `field-${end}` : null, complete: end === text.length });
    } else if (detailMatch) {
      data = envelope({ entry: dto(originals.find((item) => item.id === Number(detailMatch[1]))), next_export_cursor: `page-${detailMatch[1]}` });
    } else if (path.endsWith("facets/")) {
      const values = params.kind === "tags" ? Array.from({ length: 101 }, (_, index) => `标签-${String(index).padStart(3, "0")}`) : ["2099", "2026", "未定档"];
      const filtered = values.filter((value) => value.includes(params.search || ""));
      const offset = params.cursor ? Number(params.cursor.replace("facet-", "")) : 0;
      const rows = filtered.slice(offset, offset + 50);
      const end = offset + rows.length;
      data = envelope({ kind: params.kind, values: rows.map((value) => ({ value, preview: value, complete: true,
        selection_token: `value-${value}`, revision: "facet-rev", field_url: `${publicCatalogBase({ kind, publicSlug: slug })}facets/?kind=${params.kind}&value_token=value-${value}` })),
      next_cursor: end < filtered.length ? `facet-${end}` : null, complete: end === filtered.length });
    } else if (path.endsWith("summary/")) {
      const matched = originals.filter((entry) => !params.search || entry.title.includes(params.search));
      data = envelope({ total: originals.length, matched_count: matched.length, unscored_count: originals.length,
        stats: { total: originals.length, completed_count: 0, average_score: 0, movie_count: 0, ova_count: 0, short_count: 0, masterpiece_count: 0, pending_count: originals.length },
        profile: kind === "showcase" ? { nickname: "公开用户", subtitle: "公开介绍", public_slug: slug } : null });
    } else {
      let rows = originals.filter((entry) => (!params.search || entry.title.includes(params.search))
        && (!params.tag || entry.tags.includes(params.tag)) && (!params.year || entry.airing_period.startsWith(params.year)));
      if (params.sort === "id-asc") rows = rows.toSorted((a, b) => a.id - b.id);
      const offset = params.cursor ? Number(params.cursor.replace("page-", "")) : 0;
      const sliced = rows.slice(offset, offset + Number(params.page_size || 50));
      const end = offset + sliced.length;
      data = envelope({ total: originals.length, matched_count: rows.length, page_count: sliced.length,
        results: sliced.map(dto), next_cursor: end < rows.length ? `page-${end}` : null });
    }
    return { data, config: { ...config, _authGeneration: generation } };
  } };
  const client = createPublicCatalogClient({ api, kind, publicSlug: slug, getActorId: () => currentActor, onChange: (value) => snapshots.push(value) });
  return { client, api, calls, originals, snapshots, dto, envelope, revision, setFault: (value) => { fault = value; },
    changeIdentity: (next, nextGeneration = generation + 1) => { currentActor = next; generation = nextGeneration; return client.authChanged({ user: next ? { id: next } : null, access: next ? "synthetic" : null, generation }); } };
}

function writerFixture({ failAt = null } = {}) {
  const chunks = [];
  let closed = 0;
  let aborted = 0;
  return { chunks, get closed() { return closed; }, get aborted() { return aborted; },
    async write(value) { if (chunks.length === failAt) throw new Error("disk full"); chunks.push(String(value)); },
    async close() { closed += 1; }, async abort() { aborted += 1; },
    value: () => chunks.join(""),
  };
}

test("complete exports respect a rate-limit pause and remain cancellable without closing partial files", async () => {
  const { client, setFault } = fixture({ size: 1 });
  let limited = false;
  setFault((path) => {
    if (path.endsWith("summary/") && !limited) {
      limited = true;
      return Promise.reject({ response: { status: 429, headers: { "retry-after": "0" } } });
    }
    return undefined;
  });
  const writer = writerFixture();
  await client.exportTo(writer);
  assert.equal(writer.closed, 1);
  assert.equal(JSON.parse(writer.value()).records.length, 1);
  setFault(() => Promise.reject({ response: { status: 429, headers: { "retry-after": "60" } } }));
  const cancelledWriter = writerFixture();
  const pending = client.exportTo(cancelledWriter);
  await afterMicrotasks();
  assert.equal(client.getSnapshot().exporting.retryAfter, 60);
  client.cancelExport();
  await assert.rejects(pending);
  assert.equal(cancelledWriter.closed, 0);
  assert.equal(cancelledWriter.aborted, 1);
  assert.equal(client.getSnapshot().exporting.status, "cancelled");
});

test("new route and query mapping preserve explicit conditions and never use retired APIs", () => {
  assert.equal(publicCatalogBase(), "public/homepage/");
  assert.equal(publicCatalogBase({ kind: "showcase", publicSlug: slug }), `public/showcase/${slug}/`);
  assert.equal(publicCatalogBase({ kind: "directory" }), "public/showcases/");
  assert.throws(() => publicCatalogBase({ kind: "showcase", publicSlug: "../owner" }));
  assert.deepEqual(publicCatalogQuery({ page_size: 100, tag: "all", tag_ref: "signed-value", year: "2099", quick: "special", status: "on_hold", search: " 中文 ", sort: "score-asc" }),
    { page_size: 100, sort: "score-asc", search: "中文", status: "on_hold", year: "2099", quick: "special", tag_ref: "signed-value" });
  assert.throws(() => publicCatalogQuery({ page_size: 101 }));
  assert.throws(() => publicCatalogQuery({ tag_ref: "汉".repeat(1366) }));
  assert.equal(publicCatalogQuery({ search: " all " }).search, "all");
});

test("searching literal all does not expand into the complete public scope", async () => {
  const { client, calls } = fixture({ size: 65 });
  await client.setQuery({ search: "all" });
  assert.equal(client.getSnapshot().list.matched_count, 0);
  assert.equal(client.getSnapshot().list.results.length, 0);
  assert.equal(calls.find((call) => call.path.endsWith("entries/")).config.params.search, "all");
  assert.equal(calls.find((call) => call.path.endsWith("summary/")).config.params.search, "all");
});

test("empty and reserved facet values retain an exact selection through the UI query mapping", () => {
  for (const name of ["tag", "year"]) {
    for (const value of ["", "all"]) {
      const item = { value, preview: value, complete: true, selection_token: `signed-${name}-${value || "empty"}` };
      const filters = publicFacetFilters(name, item);
      assert.equal(publicFacetOptionKey(item), item.selection_token);
      assert.equal(publicCatalogQuery(filters)[`${name}_ref`], item.selection_token);
      assert.equal(Object.hasOwn(publicCatalogQuery(filters), name), false);
      const cleared = publicCatalogQuery({ ...filters, ...publicFacetFilters(name, null) });
      assert.equal(Object.hasOwn(cleared, `${name}_ref`), false);
    }
    const literal = { value: " 中文 ", preview: " 中文 ", complete: true, selection_token: "signed-literal" };
    assert.equal(publicFacetOptionKey(literal), "value: 中文 ");
    assert.equal(publicCatalogQuery(publicFacetFilters(name, literal))[name], " 中文 ");
  }
});

test("501 static records traverse completely with at most five pages and bounded back history", async () => {
  const { client, calls } = fixture();
  await client.setQuery({ page_size: 50 }, { facets: true });
  const ids = new Set(client.getSnapshot().list.results.map((row) => row.id));
  while (client.getSnapshot().list.next_cursor) {
    await client.nextPage();
    for (const row of client.getSnapshot().list.results) { assert.equal(ids.has(row.id), false); ids.add(row.id); }
    assert.ok(client.cacheInfo().listPages <= PUBLIC_PAGE_CACHE_LIMIT);
  }
  assert.equal(ids.size, 501);
  assert.equal(client.getSnapshot().list.pageIndex, 10);
  for (let page = 9; page >= 6; page -= 1) { await client.previousPage(); assert.equal(client.getSnapshot().list.pageIndex, page); }
  await assert.rejects(client.previousPage(), { code: "PUBLIC_HISTORY_EVICTED" });
  await client.refresh();
  assert.equal(client.getSnapshot().list.pageIndex, 0);
  assert.equal(calls.every((call) => call.path.startsWith("public/")), true);
});

test("automatic refresh preserves list and facet navigation until the reader returns to the first page", async () => {
  const { client } = fixture();
  await client.setQuery({ page_size: 50 }, { facets: true });
  assert.equal(client.canAutoRefresh(), true);
  await client.nextPage();
  assert.equal(client.canAutoRefresh(), false);
  await client.previousPage();
  assert.equal(client.canAutoRefresh(), true);
  await client.loadFacet("tags", { direction: "next" });
  assert.equal(client.canAutoRefresh(), false);
  await client.loadFacet("tags", { search: "标签-100" });
  assert.equal(client.canAutoRefresh(), false);
  await client.refresh();
  assert.equal(client.getSnapshot().list.pageIndex, 0);
  assert.equal(client.canAutoRefresh(), false);
  await client.loadFacet("tags", { search: "" });
  assert.equal(client.canAutoRefresh(), true);
});

test("summary stays whole-scope, search/tag/year reach records beyond the first page", async () => {
  const { client } = fixture();
  await client.setQuery({ search: "0501" });
  assert.equal(client.getSnapshot().list.matched_count, 1);
  assert.equal(client.getSnapshot().list.results[0].id, 501);
  assert.equal(client.getSnapshot().summary.stats.total, 501);
  assert.equal(client.getSnapshot().summary.stats.average_score, 0);
  assert.equal(client.getSnapshot().summary.unscored_count, 501);
  await client.setQuery({ tag: "末页独有标签", year: "2099" });
  assert.deepEqual(client.getSnapshot().list.results.map((row) => row.id), [501]);
});

test("facets are separate full-scope paged searches with no all-facet prefetch", async () => {
  const { client, calls } = fixture();
  await client.setQuery({ search: "0501" }, { facets: true });
  assert.equal(client.getSnapshot().facets.tags.values.length, 50);
  assert.equal(calls.filter((call) => call.path.endsWith("facets/") && call.config.params.kind === "tags").length, 1);
  await client.loadFacet("tags", { direction: "next" });
  await client.loadFacet("tags", { direction: "next" });
  assert.deepEqual(client.getSnapshot().facets.tags.values.map((item) => item.value), ["标签-100"]);
  await client.loadFacet("tags", { search: "100" });
  assert.equal(client.getSnapshot().facets.tags.pageIndex, 0);
  assert.equal(client.getSnapshot().facets.tags.values[0].value, "标签-100");
  assert.ok(client.cacheInfo().facetCursors <= 10);
});

test("late same-identity query responses and failures cannot replace a newer query", async () => {
  for (const outcome of ["success", "failure"]) {
    const data = fixture();
    const old = deferred();
    data.setFault((path, config) => path.endsWith("entries/") && config.params.search === "old" ? old.promise : undefined);
    const prior = data.client.setQuery({ search: "old" });
    await afterMicrotasks();
    await data.client.setQuery({ search: "0501" });
    const good = data.client.getSnapshot();
    if (outcome === "success") old.resolve({ data: data.envelope({ total: 501, matched_count: 1, page_count: 1, results: [data.dto(data.originals[0])], next_cursor: null }), config: { _authGeneration: 5 } });
    else old.reject(new Error("old failure"));
    await prior;
    assert.equal(data.client.getSnapshot(), good);
    assert.equal(good.list.results[0].id, 501);
  }
});

test("identity and generation changes synchronously purge lists, summaries, detail, fields and cursors", async () => {
  const data = fixture({ kind: "showcase", actor: 7, size: 2 });
  await data.client.setQuery({}, { facets: true });
  await data.client.openDetail(2);
  await data.client.readEntryField("description");
  assert.equal(data.client.getSnapshot().scope.visibility, "owner");
  const intent = data.client.captureIntent();
  assert.equal(data.changeIdentity(7, 5), false, "Same-generation token updates keep current content");
  assert.equal(data.changeIdentity(8, 6), true);
  assert.equal(data.client.getSnapshot().list.results.length, 0);
  assert.equal(data.client.getSnapshot().summary.stats, null);
  assert.equal(data.client.getSnapshot().detail.entry, null);
  assert.equal(data.client.getSnapshot().field.frame, null);
  assert.deepEqual(data.client.cacheInfo(), { listPages: 0, fieldFrames: 0, fieldBytes: 0, facetCursors: 0 });
  assert.throws(() => data.client.assertIntent(intent), { code: "PUBLIC_INTENT_CHANGED" });
  await data.client.setQuery({});
  assert.equal(data.client.getSnapshot().scope.visibility, "public");
});

test("real #226 transport cancellation prevents a late private body from reaching the catalog", async () => {
  const session = createAuthSession();
  session.store({ access: "synthetic-owner", user: { id: 7 } });
  const { api } = createWebApiTransport({ baseURL: "https://not-contacted.example.test/api/v1", session });
  const data = fixture({ kind: "showcase", actor: 7, size: 1 });
  const replies = [];
  api.defaults.adapter = (config) => new Promise((resolve) => replies.push(() => resolve({ config, status: 200, headers: {}, data: data.envelope({ total: 1, matched_count: 1, page_count: 1, results: [data.dto(data.originals[0])], next_cursor: null }) })));
  const client = createPublicCatalogClient({ api, kind: "showcase", publicSlug: slug, getActorId: () => session.getUser()?.id });
  const unsubscribe = session.subscribe(client.authChanged);
  const pending = client.setQuery({});
  session.store({ access: "synthetic-other", user: { id: 8 } });
  replies.forEach((reply) => reply());
  await pending;
  assert.equal(client.getSnapshot().list.results.length, 0);
  assert.equal(client.getSnapshot().scope, null);
  unsubscribe();
});

test("JSON string decoder handles quotes, slashes, escapes, surrogate pairs and arbitrary splits", () => {
  const text = '中文🪷\n\t"\\结尾';
  for (const encoded of [JSON.stringify(text), '"中文\\ud83e\\udeb7\\n\\t\\"\\\\结尾"']) {
    for (const length of [1, 2, 3, 7]) {
      let state;
      let result = "";
      const points = chars(encoded);
      for (let offset = 0; offset < points.length; offset += length) {
        const part = decodeJsonStringFragment(points.slice(offset, offset + length).join(""), state, offset + length >= points.length);
        result += part.text;
        state = part.state;
      }
      assert.equal(result, text);
    }
  }
  assert.throws(() => decodeJsonStringFragment('"unterminated', undefined, true));
});

test("long fields can be read completely while only three bounded frames are retained", async () => {
  const { client, originals } = fixture({ size: 1 });
  await client.setQuery({});
  await client.openDetail(1);
  let frame = await client.readEntryField("description");
  let value = frame.text;
  while (frame.next_cursor) {
    frame = await client.readEntryField("description", "next");
    value += frame.text;
    assert.ok(client.cacheInfo().fieldFrames <= 3);
    assert.ok(client.cacheInfo().fieldBytes <= PUBLIC_FIELD_CACHE_BYTES);
  }
  assert.equal(value, originals[0].description);
  assert.ok(frame.pageIndex > 5);
  assert.equal(frame.complete, true);
  await client.readEntryField("description", "previous");
  assert.equal(client.getSnapshot().field.frame.pageIndex, frame.pageIndex - 1);
});

test("a changed detail revision clears old text and requires a fresh detail", async () => {
  const data = fixture({ size: 1 });
  await data.client.setQuery({});
  await data.client.openDetail(1);
  await data.client.readEntryField("description");
  data.setFault((path) => {
    if (path.includes("/fields/")) throw Object.assign(new Error("changed"), { response: { status: 409, data: { code: "public_revision_changed" } } });
  });
  await assert.rejects(data.client.readEntryField("description", "next"));
  assert.equal(data.client.getSnapshot().field.frame, null);
  assert.equal(data.client.getSnapshot().detail.entry, null);
  assert.equal(data.client.cacheInfo().fieldFrames, 0);
});

test("full export ignores the five-page UI window and reconstructs every complete original field", async () => {
  const data = fixture();
  await data.client.setQuery({});
  for (let page = 0; page < 6; page += 1) await data.client.nextPage();
  assert.equal(data.client.cacheInfo().listPages, 5);
  const writer = writerFixture();
  const result = await data.client.exportTo(writer);
  const exported = JSON.parse(writer.value());
  assert.equal(result.records, 501);
  assert.equal(exported.schema, PUBLIC_EXPORT_SCHEMA);
  assert.equal(exported.consistency, "live");
  assert.deepEqual(exported.records, data.originals);
  assert.equal(exported.records.at(-1).tags.includes("末页独有标签"), true);
  assert.equal(writer.closed, 1);
  assert.equal(writer.aborted, 0);
  assert.ok(writer.chunks.every((part) => utf8Bytes(part) <= PUBLIC_FIELD_FRAME_BYTES));
  assert.equal(data.client.cacheInfo().listPages, 5, "Export must not populate an unbounded UI cache");
  const exportLists = data.calls.filter((call) => call.path.endsWith("entries/") && call.config.params.sort === "id-asc");
  assert.equal(exportLists.length, 11);
});

test("stream export aborts on write errors and revoked fields without closing a partial file", async () => {
  for (const reason of ["write", "revoked"]) {
    const data = fixture({ size: 1 });
    await data.client.setQuery({});
    if (reason === "revoked") data.setFault((path) => { if (path.includes("/fields/")) throw Object.assign(new Error("revoked"), { response: { status: 404 } }); });
    const writer = writerFixture({ failAt: reason === "write" ? 3 : null });
    await assert.rejects(data.client.exportTo(writer));
    assert.equal(writer.closed, 0);
    assert.equal(writer.aborted, 1);
    assert.equal(data.client.getSnapshot().exporting.status, "error");
  }
});

test("cancel and identity switches abort an in-flight export without publishing late completion", async () => {
  for (const action of ["cancel", "identity"]) {
    const data = fixture({ size: 1 });
    await data.client.setQuery({});
    const paused = deferred();
    const started = deferred();
    const writer = writerFixture();
    const originalWrite = writer.write;
    let writes = 0;
    writer.write = async (value) => { if (++writes === 3) { started.resolve(); await paused.promise; } return originalWrite(value); };
    const pending = data.client.exportTo(writer);
    await started.promise;
    if (action === "cancel") data.client.cancelExport();
    else data.changeIdentity(9, 6);
    paused.resolve();
    await assert.rejects(pending);
    assert.equal(writer.closed, 0);
    assert.equal(writer.aborted, 1);
    assert.notEqual(data.client.getSnapshot().exporting.status, "complete");
  }
});

test("invalid field offset, oversized frames and unbounded response bodies are rejected", async () => {
  const data = fixture({ size: 1 });
  await data.client.setQuery({});
  await data.client.openDetail(1);
  for (const invalid of ["offset", "frame"]) {
    data.setFault((path, config) => path.includes("/fields/") ? Promise.resolve({ config, data: data.envelope({ entry_id: 1, field: "description", revision: "revision-1", encoding: "json-text",
      offset: invalid === "offset" ? 1 : 0, fragment: "x".repeat(invalid === "frame" ? 65537 : 1), total_length: 999999, complete: false, next_cursor: "next" }) }) : undefined);
    await assert.rejects(data.client.readEntryField("description"));
    data.setFault(null);
    await data.client.openDetail(1);
  }
  data.setFault((path, config) => path.endsWith("entries/") ? Promise.resolve({ config, data: data.envelope({ total: 1, matched_count: 1, page_count: 1,
    results: [{ ...data.dto(data.originals[0]), description: "汉".repeat(180000) }], next_cursor: null }) }) : undefined);
  await data.client.setQuery({});
  assert.equal(data.client.getSnapshot().list.status, "error");
  assert.equal(data.client.getSnapshot().list.results.length, 0);
});

test("summary and directory send only parameters accepted by their strict HTTP contracts", async () => {
  const data = fixture({ size: 2 });
  data.setFault((path, config) => {
    if (path.endsWith("summary/")) assert.equal(Object.hasOwn(config.params, "page_size"), false);
  });
  await data.client.setQuery({ page_size: 12, search: "0002" });
  assert.equal(data.client.getSnapshot().summary.status, "ready");
  const directory = fixture({ kind: "directory", size: 1 });
  directory.setFault((path, config) => {
    assert.equal(path, "public/showcases/");
    assert.deepEqual(Object.keys(config.params).sort(), ["page_size", "search"]);
    return Promise.resolve({ config, data: directory.envelope({ total: 201, matched_count: 1, page_count: 1, next_cursor: null,
      results: [{ public_slug: slug, nickname: "末页同好", username: "owner", subtitle: "个人手账", stats: { total: 321 }, top_picks: [directory.dto(directory.originals[0])] }] }) });
  });
  await directory.client.setQuery({ page_size: 50, search: "末页" });
  assert.equal(directory.client.getSnapshot().list.status, "ready");
  assert.equal(directory.client.getSnapshot().list.total, 201);
  assert.equal(directory.client.getSnapshot().list.matched_count, 1);
  assert.equal(directory.client.getSnapshot().list.results[0].top_picks[0].id, 1);
  assert.equal(directory.calls.length, 1);
});

test("query changes restart interrupted facet loads and preserve exact literal tags", async () => {
  assert.equal(publicCatalogQuery({ tag: " 有空格 ", year: "未定档" }).tag, " 有空格 ");
  const data = fixture({ size: 2 });
  const old = deferred();
  let held = false;
  data.setFault((path, config) => {
    if (path.endsWith("facets/") && config.params.kind === "tags" && !held) { held = true; return old.promise; }
  });
  const initial = data.client.setQuery({}, { facets: true });
  await afterMicrotasks();
  assert.equal(data.client.getSnapshot().facets.tags.status, "loading");
  await data.client.setQuery({ search: "0002" });
  assert.equal(data.client.getSnapshot().facets.tags.status, "ready");
  old.reject(new Error("outdated facet failure"));
  await initial;
  assert.equal(data.client.getSnapshot().facets.tags.status, "ready");
});

test("long facet values use local signed continuation and can be downloaded in full", async () => {
  const data = fixture({ size: 1 });
  await data.client.setQuery({});
  const original = "超长标签🪷".repeat(6000);
  const encoded = chars(JSON.stringify(original));
  const item = { value: null, preview: "超长标签", complete: false, revision: "facet-long-revision", selection_token: "signed-long-value",
    field_url: "/api/v1/public/homepage/facets/?kind=tags&value_token=signed-long-value" };
  data.setFault((path, config) => {
    if (!config.params.value_token) return undefined;
    assert.equal(path, "public/homepage/facets/");
    assert.equal(config.params.value_token, "signed-long-value");
    const offset = config.params.cursor ? Number(config.params.cursor) : 0;
    const fragment = encoded.slice(offset, offset + 4096).join("");
    const end = offset + chars(fragment).length;
    return Promise.resolve({ config, data: data.envelope({ entry_id: 1, field: "tag", revision: item.revision, encoding: "json-text", offset,
      fragment, total_length: encoded.length, next_cursor: end < encoded.length ? String(end) : null, complete: end === encoded.length }) });
  });
  let part = await data.client.readFacetValue("tags", item);
  let text = part.text;
  while (part.next_cursor) { part = await data.client.readFacetValue("tags", item, "next"); text += part.text; }
  assert.equal(text, original);
  assert.equal(data.client.canAutoRefresh(), false);
  assert.ok(data.client.cacheInfo().fieldFrames <= 3);
  const writer = writerFixture();
  await data.client.exportFacetValueTo("tags", item, writer);
  assert.equal(JSON.parse(writer.value()), original);
  assert.equal(writer.closed, 1);
  data.client.closeDetail();
  assert.equal(data.client.getSnapshot().exporting.status, "complete", "Closing a reader must not relabel a saved file as cancelled");
  assert.throws(() => data.client.readFacetValue("tags", { ...item, field_url: "/api/v1/private/data/?value_token=x" }));
});
