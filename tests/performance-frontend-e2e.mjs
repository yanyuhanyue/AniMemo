import assert from "node:assert/strict";
import { spawn, spawnSync } from "node:child_process";
import { existsSync, mkdirSync, readFileSync, statSync, writeFileSync } from "node:fs";
import { basename, dirname, relative, resolve } from "node:path";
import process from "node:process";
import { fileURLToPath } from "node:url";

process.env.PLAYWRIGHT_BROWSERS_PATH ||= resolve(".playwright-browsers");
const { chromium } = await import("@playwright/test");

const projectRoot = fileURLToPath(new URL("..", import.meta.url));
const host = "127.0.0.1";
const port = Number(process.env.FRONTEND_PERF_PORT || 4185);
const baseUrl = `http://${host}:${port}`;
const warmupRuns = 1;
const measuredRuns = 5;
const viewport = { width: 1440, height: 900 };
const distRoot = resolve(projectRoot, "dist/client");
const outputPath = process.env.FRONTEND_PERF_OUTPUT
  ? resolve(process.env.FRONTEND_PERF_OUTPUT)
  : null;

if (!existsSync(resolve(distRoot, "index.html"))) {
  throw new Error("Production build missing; run npm run build before the frontend performance probe.");
}

function wait(ms) {
  return new Promise((resolvePromise) => setTimeout(resolvePromise, ms));
}

async function waitFor(check, timeoutMs = 10_000) {
  const started = Date.now();
  while (Date.now() - started < timeoutMs) {
    if (await check()) return;
    await wait(50);
  }
  throw new Error("Timed out waiting for frontend performance probe state.");
}

function nearestRank(values, percentile) {
  assert.ok(values.length > 0, "nearest-rank requires at least one value");
  const ordered = [...values].map(Number).sort((left, right) => left - right);
  return ordered[Math.max(0, Math.ceil((percentile / 100) * ordered.length) - 1)];
}

function summarize(values, digits = 2) {
  const measured = values.filter((value) => Number.isFinite(Number(value))).map(Number);
  if (!measured.length) return null;
  const round = (value) => Number(value.toFixed(digits));
  const ordered = [...measured].sort((left, right) => left - right);
  const middle = Math.floor(ordered.length / 2);
  const median = ordered.length % 2
    ? ordered[middle]
    : (ordered[middle - 1] + ordered[middle]) / 2;
  return {
    runs: measured.length,
    minimum: round(Math.min(...measured)),
    median: round(median),
    p95: round(nearestRank(measured, 95)),
    maximum: round(Math.max(...measured)),
  };
}

function emittedAssetReferences(path) {
  const source = readFileSync(path, "utf8");
  const references = new Set();
  const patterns = [
    /(?:src|href)=["']([^"']+)["']/g,
    /["'](\/[^"']+\.(?:css|js|woff2?|png|webp|svg))["']/g,
    /(?:import\(|from\s*)["']([^"']+)["']/g,
    /url\(["']?([^)'"?]+)["']?\)/g,
  ];
  for (const pattern of patterns) {
    for (const match of source.matchAll(pattern)) {
      const reference = match[1];
      if (!reference || /^(?:data:|https?:|#)/.test(reference)) continue;
      const candidate = reference.startsWith("/")
        ? resolve(distRoot, reference.slice(1))
        : resolve(dirname(path), reference);
      if (candidate.startsWith(distRoot) && existsSync(candidate) && statSync(candidate).isFile()) references.add(candidate);
    }
  }
  return [...references];
}

function buildInventory() {
  const pending = [resolve(distRoot, "index.html")];
  const visited = new Set();
  while (pending.length) {
    const path = pending.pop();
    if (visited.has(path)) continue;
    visited.add(path);
    if (/\.(?:html|css|js)$/.test(path)) pending.push(...emittedAssetReferences(path));
  }
  const files = [...visited].map((path) => ({
    path: relative(distRoot, path).replaceAll("\\", "/"),
    bytes: statSync(path).size,
  }));
  const javascript = files.filter((item) => item.path.endsWith(".js"));
  const css = files.filter((item) => item.path.endsWith(".css"));
  return {
    total_files: files.length,
    total_bytes: files.reduce((total, item) => total + item.bytes, 0),
    javascript_bytes: javascript.reduce((total, item) => total + item.bytes, 0),
    css_bytes: css.reduce((total, item) => total + item.bytes, 0),
    javascript: javascript.sort((left, right) => right.bytes - left.bytes),
    css: css.sort((left, right) => right.bytes - left.bytes),
  };
}

function json(route, body, status = 200, headers = {}) {
  return route.fulfill({
    status,
    contentType: "application/json",
    headers,
    body: JSON.stringify(body),
  });
}

function apiEntry(overrides = {}) {
  return {
    id: 1,
    title: "性能基线番剧",
    japanese_title: "パフォーマンス基準アニメ",
    airing_period: "2026-07",
    studio: "Baseline Studio",
    episodes: "12",
    personal_score: 8.8,
    watch_status: "watching",
    watch_status_display: "在看",
    tags: ["日常", "性能基线"],
    tag_colors: { 日常: "rose", 性能基线: "teal" },
    visibility: "private",
    description: "确定性浏览器性能基线记录。",
    review: "只用于隔离环境测量。",
    watch_history_count: 4,
    first_watched_on: "2026-07-01",
    last_watched_on: "2026-08-10",
    latest_episode_start: 5,
    latest_episode_end: 6,
    poster_url: "/assets/posters/poster-01.webp",
    poster_source: "default_url",
    updated_at: "2026-08-10T03:00:00Z",
    external_identities: [],
    ...overrides,
  };
}

function watchRecord(id, watchedOn, start, end, brush = "首刷") {
  return {
    id,
    watched_on: watchedOn,
    watched_label: watchedOn,
    brush_number: brush === "首刷" ? 1 : 2,
    brush_label: brush,
    episode_start: start,
    episode_end: end,
    notes: [`基线记录 ${id}`],
    sequence: id,
  };
}

function createMockState({ authenticated = true, staff = false } = {}) {
  return {
    authenticated,
    staff,
    staffDashboardDelayMs: 35,
    dashboardLastPage: 1,
    dashboardPageEntries: 1,
    dashboardTotalCount: 1,
    dashboardRequests: [],
    updateOperationDelayMs: 0,
    updateOperationRequests: 0,
    updateOperationInFlight: 0,
    updateOperationMaxInFlight: 0,
    counters: new Map(),
    watchHistory: [
      watchRecord(4, "2026-08-10", 5, 6),
      watchRecord(3, "2026-08-03", 3, 4),
      watchRecord(2, "2026-07-20", 1, 2),
      watchRecord(1, "2026-07-01", 1, 1),
    ],
  };
}

function incrementCounter(state, key) {
  const next = (state.counters.get(key) || 0) + 1;
  state.counters.set(key, next);
  return next;
}

async function handleApi(route, state) {
  const request = route.request();
  const url = new URL(request.url());
  const path = url.pathname.replace(/^\/api\/v1\//, "");
  const method = request.method();
  incrementCounter(state, `${method} ${path}`);

  if (path === "auth/csrf/") return json(route, { csrf_token: "frontend-perf-csrf" });
  if (path === "token/refresh/") {
    if (!state.authenticated) return json(route, { detail: "unauthenticated" }, 401);
    return json(route, {
      access: "frontend-perf-access",
      user: {
        id: 7,
        username: state.staff ? "perf-staff" : "perf-user",
        nickname: state.staff ? "Performance Staff" : "Performance User",
        is_staff: state.staff,
        is_superuser: state.staff,
        pluginPermissions: [],
      },
    });
  }
  if (path === "auth/me/") {
    if (!state.authenticated) return json(route, { detail: "unauthenticated" }, 401);
    return json(route, {
      id: 7,
      username: state.staff ? "perf-staff" : "perf-user",
      nickname: state.staff ? "Performance Staff" : "Performance User",
      is_staff: state.staff,
      is_superuser: state.staff,
      pluginPermissions: [],
    });
  }
  if (path === "site-settings/") return json(route, {
    site_name: "AniMemo Performance Baseline",
    homepage_title: "AniMemo",
    registration_enabled: true,
    trusted_poster_hosts: ["lain.bgm.tv"],
  });
  if (path === "plugins/enabled/") return json(route, { plugins: [], manifests: {} });

  if (path === "settings/me/" && method === "GET") return json(route, {
    nickname: "Performance User",
    email: "perf@example.test",
    showcase_subtitle: "Deterministic frontend baseline",
    accent: "#ffe66d",
    avatar_url: "/assets/avatar.png",
    public_status: "private",
    is_public: false,
  });
  if (path === "filters/" && method === "GET") return json(route, {
    results: [{ id: "all", name: "全部", tags: [], title_keywords: [], match_mode: "any" }],
  });
  if (path === "tag-presets/" && method === "GET") return json(route, {
    results: [
      { id: 1, name: "日常", color: "rose", sort_order: 10 },
      { id: 2, name: "性能基线", color: "teal", sort_order: 20 },
    ],
  });
  if (path === "stats/me/" && method === "GET") return json(route, {
    summary: { total: 1, watch_history_count: state.watchHistory.length, active_days: 4 },
    status_distribution: { watching: 1, completed: 0, planned: 0, on_hold: 0, dropped: 0 },
    activity_summary: { today: 0, last_7_days: 1, current_month: 2 },
    recent_activity: [],
  });
  if (path === "entries/" && method === "GET") {
    const page = Number(url.searchParams.get("page") || 1);
    const search = url.searchParams.get("search");
    state.dashboardRequests.push(`${url.pathname}${url.search}`);
    const results = Array.from({ length: state.dashboardPageEntries }, (_, index) => {
      const id = (page - 1) * state.dashboardPageEntries + index + 1;
      return apiEntry({
        id,
        title: search && page === 1 && index === 0
          ? "搜索性能结果"
          : state.dashboardLastPage > 1 ? `性能基线番剧 ${id}` : "性能基线番剧",
      });
    });
    return json(route, {
      count: state.dashboardTotalCount,
      next: page < state.dashboardLastPage
        ? `/api/v1/entries/?page=${page + 1}&page_size=48`
        : null,
      results,
      facets: { tags: ["日常", "性能基线"], years: ["2026"] },
    });
  }
  if (path === "entries/1/" && method === "GET") return json(route, apiEntry());
  if (path === "entries/1/watch-history/" && method === "GET") {
    const page = Number(url.searchParams.get("page") || 1);
    const results = page === 1 ? state.watchHistory.slice(0, 2) : state.watchHistory.slice(2);
    return json(route, {
      count: state.watchHistory.length,
      next_page: page === 1 ? 2 : null,
      results,
    });
  }
  if (path === "entries/1/watch-history/" && method === "POST") {
    const payload = request.postDataJSON();
    const record = {
      id: 5,
      watched_label: payload.watched_on,
      sequence: 5,
      ...payload,
    };
    state.watchHistory.unshift(record);
    return json(route, { record }, 201);
  }

  if (path === "plugins/marketplace/" && method === "GET") return json(route, {
    plugins: [{
      slug: "official-baseline",
      name: "Official Baseline Plugin",
      description: "Deterministic plugin platform fixture.",
      current_version: "1.0.0",
      runtime_types: ["frontend"],
      owner: "AniMemo",
      published: true,
      available: true,
    }],
  });
  if (path === "plugins/installed/" && method === "GET") return json(route, {
    plugins: [{
      slug: "official-baseline",
      name: "Official Baseline Plugin",
      description: "Deterministic installed plugin fixture.",
      current_version: "1.0.0",
      runtime_types: ["frontend"],
      owner: "AniMemo",
      published: true,
      available: true,
      installation: { enabled: true, config: {} },
      settings: [],
    }],
  });
  if (path === "plugins/my/" && method === "GET") return json(route, {
    projects: [],
    policy: {
      package: { max_package_bytes: 5 * 1024 * 1024, max_files: 100 },
      draft_limit: 10,
      uploads_per_hour: 20,
    },
  });

  if (path === "staff/dashboard/" && method === "GET") {
    await wait(state.staffDashboardDelayMs);
    return json(route, {
      stats: {
        users: 50,
        active_users: 42,
        entries: 1000,
        columns: 8,
        pending_columns: 1,
        published_columns: 6,
        removal_requests: 0,
        pending_journals: 1,
      },
      pending_columns: [],
      recent_columns: [],
      recent_entries: [],
      journal_requests: [],
      users: [],
      viewer: {
        id: 7,
        is_superuser: true,
        role: "superuser",
        capabilities: ["moderate_content", "manage_users", "view_audit", "manage_system", "backup_data"],
      },
    });
  }
  if (path === "staff/site-settings/" && method === "GET") return json(route, {
    site_name: "AniMemo Performance Baseline",
    homepage_title: "AniMemo",
    homepage_owner_options: [],
    registration_enabled: true,
    email_delivery_enabled: false,
    trusted_poster_hosts: ["lain.bgm.tv"],
  });
  if (path === "staff/plugins/" && method === "GET") return json(route, {
    plugins: [],
    available_plugins: [],
    runtimes: [],
  });
  if (path === "staff/plugins/review/" && method === "GET") return json(route, {
    submissions: [],
    approved_versions: [],
    deployments: [],
    marketplace_versions: [],
  });
  if (path === "staff/system/updates/status/" && method === "GET") return json(route, {
    current: { version: "1.0.0-rc.1", channel: "rc", commit: "frontendperf", apiDigest: "sha256:api", webDigest: "sha256:web" },
    previous: null,
    previousCompatibility: null,
    updaterVersion: "frontend-perf",
    runtime: { databaseContract: "v1", enabledPluginApis: [2] },
    history: [],
    operation: {
      id: "frontend-perf-operation",
      status: "verifying_health",
      events: [{ at: "2026-08-12T12:00:00Z", status: "verifying_health", detail: "deterministic frontend polling probe" }],
    },
  });
  if (path === "staff/system/updates/releases/" && method === "GET") return json(route, { releases: [] });
  if (path === "staff/system/updates/operations/frontend-perf-operation/" && method === "GET") {
    state.updateOperationRequests += 1;
    state.updateOperationInFlight += 1;
    state.updateOperationMaxInFlight = Math.max(state.updateOperationMaxInFlight, state.updateOperationInFlight);
    await wait(state.updateOperationDelayMs);
    state.updateOperationInFlight -= 1;
    return json(route, {
      id: "frontend-perf-operation",
      status: "verifying_health",
      events: [{ at: "2026-08-12T12:00:00Z", status: "verifying_health", detail: "deterministic frontend polling probe" }],
    });
  }

  return json(route, {});
}

async function installObservers(page, { controllableVisibility = false } = {}) {
  await page.addInitScript(({ forceVisibility }) => {
    globalThis.__frontendPerf = {
      lcp: 0,
      cls: 0,
      longTasks: [],
      layoutShifts: [],
    };
    if (forceVisibility) {
      let visibilityState = "visible";
      Object.defineProperty(document, "visibilityState", {
        configurable: true,
        get: () => visibilityState,
      });
      globalThis.__setFrontendPerfVisibility = (value) => {
        visibilityState = value;
        document.dispatchEvent(new Event("visibilitychange"));
      };
    }
    try {
      new PerformanceObserver((list) => {
        const entries = list.getEntries();
        const latest = entries.at(-1);
        if (latest) globalThis.__frontendPerf.lcp = latest.startTime;
      }).observe({ type: "largest-contentful-paint", buffered: true });
    } catch {
      // Chromium builds without this entry type still provide the remaining probe evidence.
    }
    try {
      new PerformanceObserver((list) => {
        for (const entry of list.getEntries()) {
          if (!entry.hadRecentInput) {
            globalThis.__frontendPerf.cls += entry.value;
            globalThis.__frontendPerf.layoutShifts.push({ startTime: entry.startTime, value: entry.value });
          }
        }
      }).observe({ type: "layout-shift", buffered: true });
    } catch {
      // Chromium builds without this entry type still provide the remaining probe evidence.
    }
    try {
      new PerformanceObserver((list) => {
        for (const entry of list.getEntries()) {
          globalThis.__frontendPerf.longTasks.push({ startTime: entry.startTime, duration: entry.duration });
        }
      }).observe({ type: "longtask", buffered: true });
    } catch {
      // Chromium builds without this entry type still provide the remaining probe evidence.
    }
  }, { forceVisibility: controllableVisibility });
}

function canonicalRequest(request) {
  const url = new URL(request.url());
  return {
    method: request.method(),
    url: `${url.pathname}${url.search}`,
    resource_type: request.resourceType(),
  };
}

function duplicateGroups(requests) {
  const groups = new Map();
  for (const request of requests) {
    const key = `${request.method} ${request.url}`;
    const existing = groups.get(key) || [];
    existing.push(request);
    groups.set(key, existing);
  }
  return [...groups.entries()]
    .filter(([, entries]) => entries.length > 1)
    .map(([key, entries]) => ({
      key,
      count: entries.length,
      phases: [...new Set(entries.map((entry) => entry.phase))],
      classification: entries.some((entry) => entry.phase !== entries[0].phase)
        ? "cross-phase user-triggered or freshness request"
        : "unexplained exact duplicate",
    }));
}

async function collectPageMetrics(page, requestLog, routeReadyMs, interaction) {
  await page.waitForTimeout(250);
  const browserMetrics = await page.evaluate(() => {
    const navigation = performance.getEntriesByType("navigation")[0];
    const resources = performance.getEntriesByType("resource").map((entry) => ({
      name: entry.name,
      initiator_type: entry.initiatorType,
      duration_ms: entry.duration,
      transfer_bytes: entry.transferSize,
      encoded_bytes: entry.encodedBodySize,
      decoded_bytes: entry.decodedBodySize,
    }));
    return {
      navigation: navigation ? {
        dom_content_loaded_ms: navigation.domContentLoadedEventEnd,
        load_event_ms: navigation.loadEventEnd,
        response_end_ms: navigation.responseEnd,
        transferred_bytes: navigation.transferSize,
        encoded_bytes: navigation.encodedBodySize,
        decoded_bytes: navigation.decodedBodySize,
      } : null,
      lcp_ms: globalThis.__frontendPerf?.lcp || null,
      cls: globalThis.__frontendPerf?.cls || 0,
      long_tasks: globalThis.__frontendPerf?.longTasks || [],
      layout_shifts: globalThis.__frontendPerf?.layoutShifts || [],
      resources,
    };
  });
  const jsResources = browserMetrics.resources.filter((resource) => new URL(resource.name).pathname.endsWith(".js"));
  const allResources = browserMetrics.resources;
  const longTasks = browserMetrics.long_tasks;
  return {
    route_ready_ms: Number(routeReadyMs.toFixed(2)),
    navigation: browserMetrics.navigation,
    lcp_ms: browserMetrics.lcp_ms === null ? null : Number(browserMetrics.lcp_ms.toFixed(2)),
    cls: Number(browserMetrics.cls.toFixed(6)),
    long_task_count: longTasks.length,
    long_task_total_ms: Number(longTasks.reduce((total, task) => total + task.duration, 0).toFixed(2)),
    longest_task_ms: Number(Math.max(0, ...longTasks.map((task) => task.duration)).toFixed(2)),
    layout_shift_count: browserMetrics.layout_shifts.length,
    interaction,
    requests: requestLog,
    request_count: requestLog.length,
    api_request_count: requestLog.filter((request) => request.url.startsWith("/api/")).length,
    duplicates: duplicateGroups(requestLog),
    resource_count: allResources.length,
    resource_transfer_bytes: allResources.reduce((total, resource) => total + resource.transfer_bytes, 0),
    resource_decoded_bytes: allResources.reduce((total, resource) => total + resource.decoded_bytes, 0),
    javascript_transfer_bytes: jsResources.reduce((total, resource) => total + resource.transfer_bytes, 0),
    javascript_decoded_bytes: jsResources.reduce((total, resource) => total + resource.decoded_bytes, 0),
    route_chunks: jsResources.map((resource) => ({
      file: basename(new URL(resource.name).pathname),
      transfer_bytes: resource.transfer_bytes,
      decoded_bytes: resource.decoded_bytes,
      duration_ms: Number(resource.duration_ms.toFixed(2)),
    })),
  };
}

async function sampleAnimationFrames(page, durationMs = 1500) {
  return page.evaluate((duration) => new Promise((resolvePromise) => {
    const deltas = [];
    let previous = performance.now();
    const started = previous;
    const frame = (now) => {
      deltas.push(now - previous);
      previous = now;
      if (now - started < duration) requestAnimationFrame(frame);
      else {
        const ordered = [...deltas].sort((left, right) => left - right);
        const percentile = ordered[Math.max(0, Math.ceil(ordered.length * 0.95) - 1)] || 0;
        resolvePromise({
          duration_ms: now - started,
          frames: deltas.length,
          p95_frame_delta_ms: percentile,
          over_50ms_frames: deltas.filter((value) => value > 50).length,
        });
      }
    };
    requestAnimationFrame(frame);
  }), durationMs);
}

const journeys = {
  login: {
    auth: { authenticated: false, staff: false },
    path: "/login",
    async ready(page) {
      await page.getByRole("heading", { name: "登录手账房" }).waitFor({ state: "visible" });
    },
    async interact(page, setPhase) {
      const animation = await sampleAnimationFrames(page);
      setPhase("login-mode-transition");
      const started = await page.evaluate(() => performance.now());
      await page.getByRole("button", { name: "忘记密码？→" }).click();
      await page.getByPlaceholder("请输入注册时使用的邮箱").waitFor({ state: "visible" });
      const finished = await page.evaluate(() => performance.now());
      return {
        name: "login-to-password-reset-mode",
        duration_ms: Number((finished - started).toFixed(2)),
        animation_frame_proxy: {
          duration_ms: Number(animation.duration_ms.toFixed(2)),
          frames: animation.frames,
          p95_frame_delta_ms: Number(animation.p95_frame_delta_ms.toFixed(2)),
          over_50ms_frames: animation.over_50ms_frames,
        },
      };
    },
  },
  dashboard: {
    auth: { authenticated: true, staff: false },
    path: "/dashboard",
    async ready(page) {
      await page.getByText("性能基线番剧", { exact: true }).first().waitFor({ state: "visible" });
      await page.getByRole("heading", { name: "手账统计与最近动态" }).waitFor({ state: "visible" });
    },
    async interact(page, setPhase) {
      setPhase("dashboard-search");
      const started = await page.evaluate(() => performance.now());
      await page.getByPlaceholder("输入番剧中文或日文名...").fill("性能搜索");
      await page.getByText("搜索性能结果", { exact: true }).first().waitFor({ state: "visible" });
      const searchFinished = await page.evaluate(() => performance.now());

      setPhase("dashboard-status-filter");
      const filterStarted = searchFinished;
      await Promise.all([
        page.waitForResponse((response) => {
          const url = new URL(response.url());
          return url.pathname === "/api/v1/entries/" && url.searchParams.get("status") === "watching";
        }),
        page.getByLabel("观看状态", { exact: true }).selectOption("watching"),
      ]);
      const filterFinished = await page.evaluate(() => performance.now());
      return {
        name: "dashboard-search-and-filter",
        search_to_render_ms: Number((searchFinished - started).toFixed(2)),
        status_filter_request_ms: Number((filterFinished - filterStarted).toFixed(2)),
      };
    },
  },
  watch_history: {
    auth: { authenticated: true, staff: false },
    path: "/dashboard",
    async ready(page) {
      await page.getByText("性能基线番剧", { exact: true }).first().waitFor({ state: "visible" });
    },
    async interact(page, setPhase) {
      setPhase("watch-history-initial");
      const openStarted = await page.evaluate(() => performance.now());
      await page.getByRole("button", { name: "编辑 性能基线番剧" }).click();
      await page.getByRole("tab", { name: "观看记录" }).click();
      await page.getByRole("tabpanel", { name: "观看记录" }).waitFor({ state: "visible" });
      await page.getByText("基线记录 4", { exact: true }).waitFor({ state: "visible" });
      const openFinished = await page.evaluate(() => performance.now());

      setPhase("watch-history-pagination");
      const pageStarted = await page.evaluate(() => performance.now());
      await page.getByRole("button", { name: /加载更早记录/ }).click();
      await page.getByText("基线记录 1", { exact: true }).waitFor({ state: "visible" });
      const pageFinished = await page.evaluate(() => performance.now());

      setPhase("watch-history-append");
      const appendStarted = await page.evaluate(() => performance.now());
      await page.getByPlaceholder("例如：和朋友一起补完").fill("性能基线新增记录");
      await page.getByRole("tabpanel", { name: "观看记录" }).getByRole("button", { name: "记录观看" }).click();
      await page.getByRole("status").filter({ hasText: "观看记录已保存" }).waitFor({ state: "visible" });
      const appendFinished = await page.evaluate(() => performance.now());
      return {
        name: "watch-history-interactions",
        initial_load_ms: Number((openFinished - openStarted).toFixed(2)),
        pagination_ms: Number((pageFinished - pageStarted).toFixed(2)),
        append_ms: Number((appendFinished - appendStarted).toFixed(2)),
      };
    },
  },
  staff: {
    auth: { authenticated: true, staff: true },
    path: "/admin-control",
    async ready(page) {
      await page.getByRole("heading", { name: "管理控制室" }).waitFor({ state: "visible" });
      await page.getByText("注册用户", { exact: true }).waitFor({ state: "visible" });
    },
    async interact(page, setPhase) {
      setPhase("staff-tab-transition");
      const started = await page.evaluate(() => performance.now());
      await page.getByRole("button", { name: "插件中心" }).click();
      await page.getByRole("heading", { name: "插件生命周期" }).waitFor({ state: "visible" });
      const finished = await page.evaluate(() => performance.now());
      return { name: "staff-overview-to-plugin-tab", duration_ms: Number((finished - started).toFixed(2)) };
    },
  },
  plugin_platform: {
    auth: { authenticated: true, staff: false },
    path: "/plugins",
    async ready(page) {
      await page.getByRole("heading", { name: "插件中心" }).waitFor({ state: "visible" });
      await page.getByText("Official Baseline Plugin", { exact: true }).waitFor({ state: "visible" });
    },
    async interact(page, setPhase) {
      setPhase("plugin-installed-tab");
      const started = await page.evaluate(() => performance.now());
      await page.getByRole("button", { name: "已安装" }).click();
      await page.getByRole("region", { name: "已安装插件" }).waitFor({ state: "visible" });
      const finished = await page.evaluate(() => performance.now());
      return { name: "plugin-marketplace-to-installed", duration_ms: Number((finished - started).toFixed(2)) };
    },
  },
};

async function runJourney(browser, name, definition, runIndex, measured) {
  const context = await browser.newContext({ viewport, serviceWorkers: "block" });
  const page = await context.newPage();
  await installObservers(page);
  await page.emulateMedia({ reducedMotion: "no-preference" });
  const devtools = await context.newCDPSession(page);
  await devtools.send("Network.enable");
  await devtools.send("Network.setCacheDisabled", { cacheDisabled: true });
  const mockState = createMockState(definition.auth);
  const requestLog = [];
  const failures = [];
  const consoleErrors = [];
  let phase = "initial";
  const requestStarted = new Map();

  page.on("request", (request) => {
    requestStarted.set(request, Date.now());
    requestLog.push({
      ...canonicalRequest(request),
      phase,
      started_offset_ms: null,
      status: null,
      duration_ms: null,
    });
  });
  page.on("response", (response) => {
    const request = response.request();
    const canonical = canonicalRequest(request);
    const record = [...requestLog].reverse().find((item) => item.method === canonical.method
      && item.url === canonical.url && item.status === null);
    if (record) {
      record.status = response.status();
      record.duration_ms = Date.now() - (requestStarted.get(request) || Date.now());
    }
  });
  page.on("requestfailed", (request) => {
    const canonical = canonicalRequest(request);
    failures.push({ ...canonical, error: request.failure()?.errorText || "request failed" });
  });
  page.on("console", (message) => {
    if (message.type() === "error" && !message.text().startsWith("Failed to load resource:")) {
      consoleErrors.push(message.text());
    }
  });
  page.on("pageerror", (error) => consoleErrors.push(error.message));

  await page.route("**/api/**", (route) => handleApi(route, mockState));
  const navigationStarted = Date.now();
  await page.goto(`${baseUrl}${definition.path}`, { waitUntil: "domcontentloaded" });
  await definition.ready(page);
  const routeReadyMs = Date.now() - navigationStarted;
  const interaction = await definition.interact(page, (nextPhase) => { phase = nextPhase; });
  const metrics = await collectPageMetrics(page, requestLog, routeReadyMs, interaction);
  metrics.run = runIndex;
  metrics.measured = measured;
  metrics.failures = failures;
  metrics.console_errors = consoleErrors;

  assert.equal(failures.length, 0, `${name}: network failures: ${JSON.stringify(failures)}`);
  assert.equal(consoleErrors.length, 0, `${name}: console errors: ${consoleErrors.join(" | ")}`);
  assert.equal(requestLog.some((request) => Number(request.status) >= 500), false, `${name}: HTTP 5xx observed`);
  const unexplained = metrics.duplicates.filter((duplicate) => duplicate.classification === "unexplained exact duplicate"
    && duplicate.key.includes(" /api/"));
  assert.deepEqual(unexplained, [], `${name}: unexplained exact duplicate API requests`);
  await context.close();
  return metrics;
}

function summarizeJourney(runs) {
  const measured = runs.filter((run) => run.measured);
  const interactionKeys = [...new Set(measured.flatMap((run) => Object.keys(run.interaction || {})))]
    .filter((key) => key.endsWith("_ms") && key !== "name");
  const topology = measured.map((run) => run.requests
    .filter((request) => request.url.startsWith("/api/"))
    .map((request) => `${request.method} ${request.url}`));
  return {
    route_ready_ms: summarize(measured.map((run) => run.route_ready_ms)),
    lcp_ms: summarize(measured.map((run) => run.lcp_ms)),
    cls: summarize(measured.map((run) => run.cls), 6),
    long_task_count: summarize(measured.map((run) => run.long_task_count)),
    long_task_total_ms: summarize(measured.map((run) => run.long_task_total_ms)),
    longest_task_ms: summarize(measured.map((run) => run.longest_task_ms)),
    request_count: summarize(measured.map((run) => run.request_count)),
    api_request_count: summarize(measured.map((run) => run.api_request_count)),
    resource_transfer_bytes: summarize(measured.map((run) => run.resource_transfer_bytes), 0),
    resource_decoded_bytes: summarize(measured.map((run) => run.resource_decoded_bytes), 0),
    javascript_transfer_bytes: summarize(measured.map((run) => run.javascript_transfer_bytes), 0),
    javascript_decoded_bytes: summarize(measured.map((run) => run.javascript_decoded_bytes), 0),
    interactions: Object.fromEntries(interactionKeys.map((key) => [key, summarize(measured.map((run) => run.interaction?.[key]))])),
    animation_frame_proxy: measured[0]?.interaction?.animation_frame_proxy ? {
      p95_frame_delta_ms: summarize(measured.map((run) => run.interaction.animation_frame_proxy.p95_frame_delta_ms)),
      over_50ms_frames: summarize(measured.map((run) => run.interaction.animation_frame_proxy.over_50ms_frames)),
    } : null,
    exact_duplicate_groups: measured.map((run) => run.duplicates),
    api_topology_consistent: topology.every((value) => JSON.stringify(value) === JSON.stringify(topology[0])),
    api_topology: topology[0] || [],
    route_chunks: measured[0]?.route_chunks || [],
  };
}

async function auditStaffPolling(browser) {
  const context = await browser.newContext({ viewport, serviceWorkers: "block" });
  const page = await context.newPage();
  await installObservers(page, { controllableVisibility: true });
  await page.emulateMedia({ reducedMotion: "reduce" });
  const state = createMockState({ authenticated: true, staff: true });
  state.staffDashboardDelayMs = 300;
  await page.route("**/api/**", (route) => handleApi(route, state));
  await page.goto(`${baseUrl}/admin-control`, { waitUntil: "domcontentloaded" });
  await page.getByText("注册用户", { exact: true }).waitFor({ state: "visible" });
  const key = "GET staff/dashboard/";
  assert.equal(state.counters.get(key), 1, "Staff initial load must issue one dashboard request");

  await page.evaluate(() => {
    window.dispatchEvent(new Event("focus"));
    window.dispatchEvent(new Event("focus"));
  });
  await waitFor(() => state.counters.get(key) === 2);
  await wait(400);
  const overlapCount = state.counters.get(key);
  assert.equal(overlapCount, 2, "Concurrent focus triggers must coalesce into one in-flight refresh");

  await page.evaluate(() => {
    globalThis.__setFrontendPerfVisibility("hidden");
    window.dispatchEvent(new Event("focus"));
  });
  await wait(150);
  const hiddenCount = state.counters.get(key);
  assert.equal(hiddenCount, 2, "Hidden staff tabs must not refresh");

  await page.evaluate(() => {
    globalThis.__setFrontendPerfVisibility("visible");
    globalThis.__setFrontendPerfVisibility("visible");
  });
  await waitFor(() => state.counters.get(key) === 3);
  await wait(400);
  const visibleCount = state.counters.get(key);
  assert.equal(visibleCount, 3, "Returning visible must refresh once without overlap");
  await context.close();
  return {
    configured_interval_ms: 20_000,
    initial_requests: 1,
    requests_after_two_overlapping_focus_events: overlapCount,
    requests_after_hidden_visibility_and_focus: hiddenCount,
    requests_after_two_visible_events: visibleCount,
    hidden_tab_suppression: "PASS",
    overlap_coalescing: "PASS",
    visible_return_refresh: "PASS",
  };
}

async function auditDashboardPage48(browser) {
  const context = await browser.newContext({ viewport, serviceWorkers: "block" });
  const page = await context.newPage();
  await installObservers(page);
  await page.emulateMedia({ reducedMotion: "reduce" });
  const state = createMockState({ authenticated: true, staff: false });
  state.dashboardLastPage = 48;
  state.dashboardPageEntries = 48;
  state.dashboardTotalCount = 10_000;
  await page.route("**/api/**", (route) => handleApi(route, state));
  const started = Date.now();
  await page.goto(`${baseUrl}/dashboard`, { waitUntil: "domcontentloaded" });
  await page.getByRole("button", { name: "编辑 性能基线番剧 1", exact: true }).waitFor({ state: "visible" });

  for (let expectedPage = 2; expectedPage <= 48; expectedPage += 1) {
    await page.locator(".dashboard-infinite-sentinel").scrollIntoViewIfNeeded();
    await waitFor(() => state.dashboardRequests.length >= expectedPage, 15_000);
  }

  const requestedPages = state.dashboardRequests.map((requestUrl) => Number(new URL(requestUrl, baseUrl).searchParams.get("page")));
  assert.deepEqual(requestedPages, Array.from({ length: 48 }, (_, index) => index + 1), "Dashboard deep pagination must request pages 1 through 48 exactly once");
  assert.equal(state.dashboardRequests.filter((requestUrl) => requestUrl.includes("include_facets=1")).length, 1, "Only dashboard page 1 may request facets");
  const renderedEntries = await page.locator('[aria-label^="编辑 性能基线番剧 "]').count();
  assert.equal(renderedEntries, 48 * 48, "Dashboard must append every page without dropping entries");
  const result = {
    dataset: "LARGE-shaped deterministic browser mock (10,000 total; 48 entries returned per page)",
    authority: "AUXILIARY — browser/request-topology evidence only; mocked API on Windows, not PostgreSQL latency evidence",
    repetitions: "1 high-cost deep-pagination run; p95 NOT REPORTED",
    requested_pages: requestedPages,
    page_48_requests: requestedPages.filter((value) => value === 48).length,
    total_entries_rendered_after_page_48: renderedEntries,
    elapsed_ms: Date.now() - started,
    first_page_facets_requests: state.dashboardRequests.filter((requestUrl) => requestUrl.includes("include_facets=1")).length,
    exact_duplicate_requests: [],
    request_topology: state.dashboardRequests,
  };
  await context.close();
  return result;
}

async function openActiveUpdatePanel(page) {
  await page.getByRole("button", { name: "系统更新" }).click();
  await page.getByRole("heading", { name: "真实操作进度" }).waitFor({ state: "visible" });
  await page.getByText("verifying_health", { exact: true }).waitFor({ state: "visible" });
}

async function auditUpdateOperationPolling(browser) {
  const visibleContext = await browser.newContext({ viewport, serviceWorkers: "block" });
  const visiblePage = await visibleContext.newPage();
  const visibleState = createMockState({ authenticated: true, staff: true });
  visibleState.updateOperationDelayMs = 3_000;
  await visiblePage.route("**/api/**", (route) => handleApi(route, visibleState));
  await visiblePage.goto(`${baseUrl}/admin-control`, { waitUntil: "domcontentloaded" });
  await visiblePage.getByText("注册用户", { exact: true }).waitFor({ state: "visible" });
  await openActiveUpdatePanel(visiblePage);
  await waitFor(() => visibleState.updateOperationRequests >= 1, 4_000);
  await wait(3_250);
  const overlapEvidence = {
    response_delay_ms: visibleState.updateOperationDelayMs,
    requests_started: visibleState.updateOperationRequests,
    maximum_in_flight: visibleState.updateOperationMaxInFlight,
  };
  await visibleContext.close();

  const hiddenContext = await browser.newContext({ viewport, serviceWorkers: "block" });
  const hiddenPage = await hiddenContext.newPage();
  await installObservers(hiddenPage, { controllableVisibility: true });
  const hiddenState = createMockState({ authenticated: true, staff: true });
  await hiddenPage.route("**/api/**", (route) => handleApi(route, hiddenState));
  await hiddenPage.goto(`${baseUrl}/admin-control`, { waitUntil: "domcontentloaded" });
  await hiddenPage.getByText("注册用户", { exact: true }).waitFor({ state: "visible" });
  await openActiveUpdatePanel(hiddenPage);
  await hiddenPage.evaluate(() => globalThis.__setFrontendPerfVisibility("hidden"));
  await wait(3_250);
  const hiddenEvidence = {
    visibility: "hidden",
    requests_after_one_interval: hiddenState.updateOperationRequests,
  };
  await hiddenContext.close();

  assert.equal(overlapEvidence.maximum_in_flight, 1, "Slow Update Operation responses must not overlap across the 2.5 second interval");
  assert.equal(hiddenEvidence.requests_after_one_interval, 0, "Hidden Update Operation tabs must not continue polling");
  return {
    configured_interval_ms: 2_500,
    authority: "AUXILIARY — deterministic browser mock; no real Update Agent operation was invoked",
    hidden_tab_suppression: "PASS",
    overlap_coalescing: "PASS",
    visible_slow_response: overlapEvidence,
    hidden_tab: hiddenEvidence,
  };
}

const server = spawn(
  process.execPath,
  [resolve(projectRoot, "node_modules/vite/bin/vite.js"), "preview", "--host", host, "--port", String(port)],
  {
    cwd: projectRoot,
    env: { ...process.env, BROWSER: "none" },
    stdio: "ignore",
    windowsHide: true,
  },
);

let browser;
try {
  await waitFor(async () => {
    try {
      return (await fetch(`${baseUrl}/`)).ok;
    } catch {
      return false;
    }
  });
  browser = await chromium.launch({ headless: true });
  const browserVersion = browser.version();
  const results = {};
  for (const [name, definition] of Object.entries(journeys)) {
    const runs = [];
    for (let index = 0; index < warmupRuns + measuredRuns; index += 1) {
      const measured = index >= warmupRuns;
      process.stderr.write(`[frontend-perf] ${name} ${measured ? "measured" : "warm-up"} run ${measured ? index : 1}/${measured ? measuredRuns : warmupRuns}\n`);
      runs.push(await runJourney(browser, name, definition, index + 1, measured));
    }
    results[name] = { runs, summary: summarizeJourney(runs) };
  }
  const staffPolling = await auditStaffPolling(browser);
  const dashboardPage48 = await auditDashboardPage48(browser);
  const updateOperationPolling = await auditUpdateOperationPolling(browser);
  const report = {
    schema: "animemo.frontend-performance.v1",
    generated_at: new Date().toISOString(),
    environment: {
      commit: process.env.FRONTEND_PERF_COMMIT || null,
      platform: process.platform,
      architecture: process.arch,
      node: process.version,
      chromium: browserVersion,
      viewport,
      base_url: baseUrl,
      build: "vite production preview",
      api: "deterministic Playwright route mock",
      cache: "disabled; fresh browser context per run",
      motion: "no-preference",
    },
    repetition: { warmup_runs: warmupRuns, measured_runs: measuredRuns, percentile: "nearest-rank p95" },
    build_inventory: buildInventory(),
    journeys: results,
    staff_polling: staffPolling,
    dashboard_page_48: dashboardPage48,
    update_operation_polling: updateOperationPolling,
    unavailable: {
      inp: "NOT RUN — five synthetic interactions are insufficient for field INP; deterministic wall-clock interaction proxies are reported instead.",
      javascript_parsed_size: "NOT RUN — resource decoded bytes and emitted build bytes are reported; V8 parsed/heap attribution was not stable enough for this probe.",
      turnstile_integration_impact: "NOT RUN unless the production build was supplied VITE_TURNSTILE_SITE_KEY; this probe does not call Cloudflare.",
      watch_history_import_preview_apply: "NOT RUN — this probe covers canonical Watch History initial load, pagination, and append; no production-like import was executed.",
    },
  };
  const serialized = `${JSON.stringify(report, null, 2)}\n`;
  if (outputPath) {
    mkdirSync(dirname(outputPath), { recursive: true });
    writeFileSync(outputPath, serialized, "utf8");
  }
  process.stdout.write(serialized);
} finally {
  await browser?.close();
  if (server.pid && process.platform === "win32") {
    spawnSync("taskkill", ["/pid", String(server.pid), "/t", "/f"], { stdio: "ignore" });
  } else {
    server.kill();
  }
}
