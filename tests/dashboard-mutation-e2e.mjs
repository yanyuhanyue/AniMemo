import assert from "node:assert/strict";
import { existsSync } from "node:fs";
import { spawn } from "node:child_process";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";

process.env.PLAYWRIGHT_BROWSERS_PATH ||= resolve(".playwright-browsers");
const { chromium } = await import("@playwright/test");

const host = "127.0.0.1";
const port = Number(process.env.DASHBOARD_MUTATION_E2E_PORT || 4176);
const baseUrl = `http://${host}:${port}`;
const projectRoot = fileURLToPath(new URL("..", import.meta.url));

if (!existsSync(resolve(projectRoot, "dist/client/index.html"))) {
  throw new Error("Production build missing; run npm run build before the dashboard mutation regression.");
}

function wait(ms) {
  return new Promise((resolvePromise) => setTimeout(resolvePromise, ms));
}

async function waitFor(check, timeoutMs = 5000) {
  const started = Date.now();
  while (Date.now() - started < timeoutMs) {
    if (await check()) return;
    await wait(50);
  }
  throw new Error("Timed out waiting for dashboard mutation browser state.");
}

function json(route, body, status = 200) {
  return route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
}

function apiEntry(overrides = {}) {
  return {
    id: 1,
    title: "测试番剧一号",
    japanese_title: "テストアニメ一号",
    airing_period: "2026-01",
    studio: "测试制作",
    episodes: "12",
    personal_score: null,
    watch_status: "planned",
    watch_status_display: "想看",
    tags: ["日常"],
    tag_colors: {},
    visibility: "private",
    description: "",
    review: "",
    updated_at: "2026-08-10T01:00:00Z",
    external_identities: [],
    ...overrides,
  };
}

function patchEntry(item, payload) {
  const fieldMap = {
    japanese_title: "japanese_title",
    airing_period: "airing_period",
    personal_score: "personal_score",
    watch_status: "watch_status",
    tag_colors: "tag_colors",
    poster_url: "poster_url",
    custom_poster_url: "custom_poster_url",
    baike_url: "baike_url",
  };
  for (const [key, value] of Object.entries(payload || {})) {
    item[fieldMap[key] || key] = value;
  }
  item.watch_status_display = {
    completed: "看过",
    watching: "在看",
    planned: "想看",
    on_hold: "搁置",
    dropped: "弃番",
  }[item.watch_status] || item.watch_status;
  item.updated_at = "2026-08-10T02:00:00Z";
  return item;
}

function filteredEntries(items, url) {
  const search = String(url.searchParams.get("search") || "").toLocaleLowerCase("zh-CN");
  const status = url.searchParams.get("status");
  const tag = url.searchParams.get("tag");
  const year = url.searchParams.get("year");
  const quickTags = url.searchParams.getAll("quick_tags");
  const quickKeywords = url.searchParams.getAll("quick_title_keywords");
  const matchAll = url.searchParams.get("quick_match_mode") === "all";
  return items.filter((item) => {
    const title = `${item.title} ${item.japanese_title} ${item.studio} ${item.review}`.toLocaleLowerCase("zh-CN");
    if (search && !title.includes(search)) return false;
    if (status && item.watch_status !== status) return false;
    if (tag && !(item.tags || []).some((value) => String(value).includes(tag))) return false;
    if (year && !String(item.airing_period || "").startsWith(year)) return false;
    const quickMatches = [
      ...quickTags.map((value) => (item.tags || []).some((tagValue) => String(tagValue).includes(value))),
      ...quickKeywords.map((value) => `${item.title} ${item.japanese_title}`.toLocaleLowerCase("zh-CN").includes(value.toLocaleLowerCase("zh-CN"))),
    ];
    if (quickMatches.length && (matchAll ? !quickMatches.every(Boolean) : !quickMatches.some(Boolean))) return false;
    return true;
  });
}

const server = spawn(process.execPath, [resolve(projectRoot, "node_modules/vite/bin/vite.js"), "preview", "--host", host, "--port", String(port)], {
  cwd: projectRoot,
  env: { ...process.env, BROWSER: "none" },
  stdio: "ignore",
  windowsHide: true,
});

const browser = await chromium.launch({ headless: true });
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
await page.emulateMedia({ reducedMotion: "reduce" });

const state = {
  entries: [
    apiEntry(),
    apiEntry({ id: 2, title: "测试番剧二号", japanese_title: "テストアニメ二号", airing_period: "2025-04" }),
  ],
  settings: {
    nickname: "Browser Test",
    email: "browser@example.test",
    showcase_subtitle: "Mutation QA",
    accent: "#ffe66d",
    avatar_url: "",
    public_status: "private",
    is_public: false,
  },
  filters: [{ id: 9, name: "周末清单", tags: ["日常"], title_keywords: [], match_mode: "any", color: "#ffe66d" }],
  failEntryPatch: false,
  entryPatch401Once: false,
  entryPatchDelay: 0,
  failEntryDelete: false,
  entryDeleteDelay: 0,
  failEntryCreate: false,
  failSettings: false,
  failFilterSave: false,
  failFilterDelete: false,
};

const entryRequests = [];
let entryPatchRequests = 0;
let entryDeleteRequests = 0;
let entryCreateRequests = 0;
let settingsPatchRequests = 0;
let filterPatchRequests = 0;
let filterDeleteRequests = 0;
const consoleErrors = [];
page.on("console", (message) => {
  if (message.type() === "error" && !message.text().startsWith("Failed to load resource:")) consoleErrors.push(message.text());
});
page.on("pageerror", (error) => consoleErrors.push(error.message));

async function openEditor(title) {
  const row = page.locator(".anime-list-row", { hasText: title });
  await row.getByRole("button", { name: /修改/ }).click();
  await page.locator(".anime-edit-modal").waitFor({ state: "visible" });
}

try {
  await waitFor(async () => {
    try {
      return (await fetch(`${baseUrl}/`)).ok;
    } catch {
      return false;
    }
  }, 10000);

  await page.route("**/api/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname.replace(/^\/api\//, "");
    const method = request.method();

    if (path === "auth/csrf/") return json(route, { csrf_token: "browser-test-token" });
    if (path === "token/refresh/") return json(route, { access: "browser-test-access", user: { id: 7, username: "browser-test", is_staff: false } });
    if (path === "auth/me/") return json(route, { id: 7, username: "browser-test", is_staff: false });
    if (path === "site-settings/") return json(route, { site_name: "AniMemo", trusted_poster_hosts: [] });
    if (path === "plugins/enabled/") return json(route, { plugins: [], manifests: {} });
    if (path === "tag-presets/") return json(route, { results: [{ id: 1, name: "日常", color: "rose", sort_order: 10 }] });
    if (path === "stats/me/") return json(route, { summary: { total: state.entries.length }, status_distribution: {}, recent_activity: [] });
    if (path.startsWith("external-media/providers/")) return json(route, { results: [] });

    if (path === "settings/me/" && method === "GET") return json(route, state.settings);
    if (path === "settings/me/" && method === "PATCH") {
      settingsPatchRequests += 1;
      if (state.failSettings) return json(route, { detail: "个人资料保存失败" }, 500);
      Object.assign(state.settings, request.postDataJSON());
      return json(route, state.settings);
    }

    if (path === "filters/" && method === "GET") return json(route, { results: state.filters });
    if (path === "filters/" && method === "POST") {
      if (state.failFilterSave) return json(route, { detail: "筛选保存失败" }, 500);
      const saved = { id: 10, ...request.postDataJSON() };
      state.filters.push(saved);
      return json(route, saved, 201);
    }
    const filterMatch = path.match(/^filters\/(\d+)\/$/);
    if (filterMatch && method === "PATCH") {
      filterPatchRequests += 1;
      if (state.failFilterSave) return json(route, { detail: "筛选保存失败" }, 500);
      const filter = state.filters.find((item) => item.id === Number(filterMatch[1]));
      Object.assign(filter, request.postDataJSON());
      return json(route, filter);
    }
    if (filterMatch && method === "DELETE") {
      filterDeleteRequests += 1;
      if (state.failFilterDelete) return json(route, { detail: "筛选删除失败" }, 500);
      state.filters = state.filters.filter((item) => item.id !== Number(filterMatch[1]));
      return route.fulfill({ status: 204, body: "" });
    }

    if (path === "entries/" && method === "GET") {
      entryRequests.push(url);
      const results = filteredEntries(state.entries, url);
      return json(route, {
        count: results.length,
        next: null,
        results,
        facets: { tags: ["日常"], years: ["2026", "2025"] },
      });
    }
    if (path === "entries/" && method === "POST") {
      entryCreateRequests += 1;
      if (state.failEntryCreate) return json(route, { detail: "创建番剧失败" }, 500);
      const created = patchEntry(apiEntry({ id: Math.max(0, ...state.entries.map((item) => item.id)) + 1 }), request.postDataJSON());
      state.entries.push(created);
      return json(route, created, 201);
    }
    const entryMatch = path.match(/^entries\/(\d+)\/$/);
    if (entryMatch && method === "GET") {
      const item = state.entries.find((entry) => entry.id === Number(entryMatch[1]));
      return item ? json(route, item) : json(route, { detail: "not found" }, 404);
    }
    if (entryMatch && method === "PATCH") {
      entryPatchRequests += 1;
      if (state.entryPatchDelay) await wait(state.entryPatchDelay);
      if (state.entryPatch401Once) {
        state.entryPatch401Once = false;
        return json(route, { detail: "expired" }, 401);
      }
      if (state.failEntryPatch) return json(route, { detail: "记录保存失败" }, 500);
      const item = state.entries.find((entry) => entry.id === Number(entryMatch[1]));
      patchEntry(item, request.postDataJSON());
      return json(route, item);
    }
    if (entryMatch && method === "DELETE") {
      entryDeleteRequests += 1;
      if (state.entryDeleteDelay) await wait(state.entryDeleteDelay);
      if (state.failEntryDelete) return json(route, { detail: "记录删除失败" }, 500);
      state.entries = state.entries.filter((entry) => entry.id !== Number(entryMatch[1]));
      return route.fulfill({ status: 204, body: "" });
    }

    return json(route, { results: [], plugins: [], manifests: {} });
  });

  await page.goto(`${baseUrl}/dashboard`, { waitUntil: "networkidle" });
  await page.getByText("测试番剧一号", { exact: true }).first().waitFor({ state: "visible" });
  assert.equal(entryRequests.length, 1);
  await page.getByText("已载入 2 / 共 2 条", { exact: false }).waitFor({ state: "visible" });

  state.failEntryPatch = true;
  await openEditor("测试番剧一号");
  const titleInput = page.locator("#anime-modal-title");
  await titleInput.fill("测试番剧一号·已修改");
  await page.getByRole("button", { name: "保存全部修改" }).click();
  await page.getByRole("alert").filter({ hasText: "记录保存失败" }).waitFor({ state: "visible" });
  assert.equal(await titleInput.inputValue(), "测试番剧一号·已修改");
  assert.equal(await page.locator(".anime-edit-modal").isVisible(), true);
  assert.equal(entryRequests.length, 1, "failed update must not reload entries");

  state.failEntryPatch = false;
  state.entryPatch401Once = true;
  await page.getByRole("button", { name: "保存全部修改" }).click();
  await page.locator(".anime-edit-modal").waitFor({ state: "detached" });
  await page.getByText("测试番剧一号·已修改", { exact: true }).first().waitFor({ state: "visible" });
  assert.equal(entryPatchRequests, 3, "failed PATCH plus 401 retry must produce exactly three PATCH attempts");
  assert.equal(entryRequests.length, 1, "successful local update must not reload page one");

  state.failEntryDelete = true;
  await openEditor("测试番剧一号·已修改");
  page.once("dialog", (dialog) => dialog.accept());
  await page.getByRole("button", { name: "删除私人记录" }).click();
  await page.getByRole("alert").filter({ hasText: "记录删除失败" }).waitFor({ state: "visible" });
  assert.equal(await page.locator(".anime-edit-modal").isVisible(), true);
  assert.equal(entryRequests.length, 1, "failed delete must not reload or remove the record");

  state.failEntryDelete = false;
  state.entryDeleteDelay = 300;
  page.once("dialog", (dialog) => dialog.accept());
  await page.getByRole("button", { name: "删除私人记录" }).click();
  await waitFor(() => entryDeleteRequests === 2);
  assert.equal(await page.getByRole("button", { name: "处理中..." }).isDisabled(), true, "pending delete must prevent duplicate submission");
  await page.locator(".anime-edit-modal").waitFor({ state: "detached" });
  await page.getByText("测试番剧一号·已修改", { exact: true }).first().waitFor({ state: "detached" });
  await page.getByText("已载入 1 / 共 1 条", { exact: false }).waitFor({ state: "visible" });
  assert.equal(entryRequests.length, 1, "confirmed delete with no next page must stay local");

  state.failEntryCreate = true;
  await page.getByRole("button", { name: /添加番剧/ }).first().click();
  await page.locator(".dashboard-add-modal").waitFor({ state: "visible" });
  await page.locator(".dashboard-add-smart-field input").fill("新增番剧");
  await page.getByLabel("放送季度").fill("2026-08");
  await page.getByRole("button", { name: /创建并加入手账/ }).click();
  await page.getByRole("alert").filter({ hasText: "创建番剧失败" }).waitFor({ state: "visible" });
  assert.equal(await page.locator(".dashboard-add-smart-field input").inputValue(), "新增番剧");
  assert.equal(entryCreateRequests, 1);

  state.failEntryCreate = false;
  await page.getByRole("button", { name: /创建并加入手账/ }).click();
  await page.locator(".dashboard-add-modal").waitFor({ state: "detached" });
  await page.getByText("新增番剧", { exact: true }).first().waitFor({ state: "visible" });
  await page.getByText("已载入 2 / 共 2 条", { exact: false }).waitFor({ state: "visible" });
  assert.equal(entryRequests.length, 1, "confirmed create must reconcile locally");

  state.failSettings = true;
  await page.getByRole("button", { name: "打开账户菜单" }).click();
  await page.getByRole("menuitem", { name: /设置 \/ 修改资料/ }).click();
  const nicknameInput = page.locator(".dashboard-profile-field input").first();
  await nicknameInput.fill("未保存昵称");
  await page.getByRole("button", { name: "保存资料" }).click();
  await page.getByRole("alert").filter({ hasText: "个人资料保存失败" }).waitFor({ state: "visible" });
  assert.equal(await nicknameInput.inputValue(), "未保存昵称");
  assert.equal(entryRequests.length, 1, "settings failure must not reload entries");

  state.failSettings = false;
  await page.getByRole("button", { name: "保存资料" }).click();
  await page.locator(".dashboard-profile-modal").waitFor({ state: "detached" });
  await page.getByRole("status").filter({ hasText: "个人资料已更新" }).waitFor({ state: "visible" });
  assert.equal(settingsPatchRequests, 2);
  assert.equal(entryRequests.length, 1, "settings invalidation must refresh settings only");

  await wait(1100);
  await page.getByLabel("快速修改 测试番剧二号 的观看状态").selectOption("completed");
  await page.getByRole("status").filter({ hasText: "标记为看过" }).waitFor({ state: "visible" });
  await wait(1400);
  assert.equal(await page.getByRole("status").filter({ hasText: "标记为看过" }).isVisible(), true, "the previous flash timer must not clear a newer notice");

  await page.getByRole("button", { name: "周末清单" }).click();
  await waitFor(() => entryRequests.length === 2);
  const quickFilterEntryRequestCount = entryRequests.length;
  await page.getByRole("button", { name: "编辑自定义快速筛选" }).click();
  const filterNameInput = page.getByLabel("筛选名称");
  await filterNameInput.fill("周末清单·改名");
  state.failFilterSave = true;
  await page.getByRole("button", { name: "保存筛选" }).click();
  await page.getByRole("alert").filter({ hasText: "筛选保存失败" }).waitFor({ state: "visible" });
  assert.equal(await filterNameInput.inputValue(), "周末清单·改名");
  assert.equal(entryRequests.length, quickFilterEntryRequestCount);

  state.failFilterSave = false;
  await page.getByRole("button", { name: "保存筛选" }).click();
  await page.locator(".dashboard-filter-editor").waitFor({ state: "detached" });
  assert.equal(filterPatchRequests, 2);
  assert.equal(entryRequests.length, quickFilterEntryRequestCount, "metadata-only filter rename must not reload an identical entries query");

  await page.getByRole("button", { name: "编辑自定义快速筛选" }).click();
  state.failFilterDelete = true;
  await page.getByRole("button", { name: "删除筛选" }).click();
  await page.getByRole("alert").filter({ hasText: "筛选删除失败" }).waitFor({ state: "visible" });
  assert.equal(await page.locator(".dashboard-filter-editor").isVisible(), true);
  assert.equal(entryRequests.length, quickFilterEntryRequestCount, "failed filter delete must preserve active metadata and entries");

  state.failFilterDelete = false;
  await page.getByRole("button", { name: "删除筛选" }).click();
  await page.locator(".dashboard-filter-editor").waitFor({ state: "detached" });
  await waitFor(() => entryRequests.length === quickFilterEntryRequestCount + 1);
  assert.equal(filterDeleteRequests, 2);

  const beforeRaceRequests = entryRequests.length;
  state.entryPatchDelay = 600;
  await page.getByLabel("快速修改 新增番剧 的观看状态").selectOption("watching");
  await page.getByPlaceholder("输入番剧中文或日文名...").fill("新增番剧");
  await waitFor(() => entryRequests.length >= beforeRaceRequests + 2, 5000);
  await page.getByLabel("快速修改 新增番剧 的观看状态").waitFor({ state: "visible" });
  assert.equal(await page.getByLabel("快速修改 新增番剧 的观看状态").inputValue(), "watching", "query refresh after a pending mutation must use the confirmed server value");

  state.entryPatchDelay = 500;
  await page.getByLabel("快速修改 新增番剧 的观看状态").selectOption("completed");
  await page.getByRole("button", { name: /插件中心/ }).click();
  await page.waitForURL("**/plugins");
  await wait(700);
  assert.equal(consoleErrors.length, 0, `browser console errors: ${consoleErrors.join(" | ")}`);

  process.stdout.write("dashboard mutation browser regression: PASS\n");
} finally {
  await browser.close();
  server.kill();
}
