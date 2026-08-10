import assert from "node:assert/strict";
import { existsSync } from "node:fs";
import { spawn } from "node:child_process";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";

process.env.PLAYWRIGHT_BROWSERS_PATH ||= resolve(".playwright-browsers");
const { chromium } = await import("@playwright/test");

const host = "127.0.0.1";
const port = Number(process.env.DASHBOARD_E2E_PORT || 4175);
const baseUrl = `http://${host}:${port}`;
const projectRoot = fileURLToPath(new URL("..", import.meta.url));

if (!existsSync(resolve(projectRoot, "dist/client/index.html"))) {
  throw new Error("Production build missing; run npm run build before the dashboard browser regression.");
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
  throw new Error("Timed out waiting for dashboard browser state.");
}

function json(route, body, status = 200) {
  return route.fulfill({
    status,
    contentType: "application/json",
    body: JSON.stringify(body),
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
const entryRequests = [];
const consoleErrors = [];
page.on("console", (message) => {
  if (message.type() === "error") consoleErrors.push(message.text());
});

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
    if (path === "auth/csrf/") return json(route, { csrf_token: "browser-test-token" });
    if (path === "token/refresh/") return json(route, {
      access: "browser-test-access",
      user: { id: 7, username: "browser-test", is_staff: false },
    });
    if (path === "auth/me/") return json(route, { id: 7, username: "browser-test", is_staff: false });
    if (path === "site-settings/") return json(route, { site_name: "AniMemo", trusted_poster_hosts: [] });
    if (path === "plugins/enabled/") return json(route, { plugins: [], manifests: {} });
    if (path === "settings/me/") return json(route, { nickname: "Browser Test", email: "browser@example.test" });
    if (path === "filters/") {
      await wait(120);
      return json(route, { results: [{ id: "all", name: "全部", tags: [] }] });
    }
    if (path === "tag-presets/") {
      await wait(160);
      return json(route, { results: [{ id: 1, name: "日常", color: "blue", sort_order: 10 }] });
    }
    if (path === "stats/me/") {
      await wait(120);
      return json(route, {});
    }
    if (path === "entries/" && request.method() === "GET") {
      entryRequests.push(url);
      return json(route, {
        count: 1,
        next: null,
        results: [{
          id: 1,
          title: "测试番剧",
          japanese_title: "テストアニメ",
          airing_period: "2026-01",
          studio: "测试制作",
          episodes: 12,
          personal_score: null,
          watch_status: "planned",
          watch_status_display: "想看",
          tags: ["日常"],
          tag_colors: {},
          visibility: "private",
        }],
        facets: { tags: ["日常"], years: ["2026"] },
      });
    }
    return route.continue();
  });

  await page.goto(`${baseUrl}/dashboard`, { waitUntil: "networkidle" });
  await page.getByText("测试番剧", { exact: true }).first().waitFor({ state: "visible" });
  await waitFor(() => entryRequests.length === 1, 5000);
  await wait(450);
  assert.equal(entryRequests.length, 1, "metadata and tag preset updates must not repeat the initial entries request");
  assert.equal(entryRequests[0].searchParams.get("page"), "1");
  assert.equal(entryRequests[0].searchParams.get("page_size"), "48");

  const search = page.getByPlaceholder("输入番剧中文或日文名...");
  await search.fill("进击的巨人");
  await waitFor(() => entryRequests.length === 2, 5000);
  await wait(450);
  assert.equal(entryRequests.length, 2, "a changed search query must issue one new page-one request");
  assert.equal(entryRequests[1].searchParams.get("page"), "1");
  assert.equal(entryRequests[1].searchParams.get("search"), "进击的巨人");

  const expectSingleQueryChange = async (action, param, expected) => {
    const before = entryRequests.length;
    await action();
    await waitFor(() => entryRequests.length === before + 1, 5000);
    await wait(250);
    assert.equal(entryRequests.length, before + 1, `${param} must issue exactly one page-one request`);
    const request = entryRequests.at(-1);
    assert.equal(request.searchParams.get("page"), "1");
    assert.equal(request.searchParams.get(param), expected);
  };

  await expectSingleQueryChange(
    () => page.getByLabel("观看状态", { exact: true }).selectOption("completed"),
    "status",
    "completed",
  );
  await expectSingleQueryChange(
    () => page.getByLabel("标签过滤").selectOption("日常"),
    "tag",
    "日常",
  );
  await expectSingleQueryChange(
    () => page.getByLabel("年份区间").selectOption("2026"),
    "year",
    "2026",
  );
  await expectSingleQueryChange(
    () => page.getByLabel("排序规则 (默认)").selectOption("score-desc"),
    "ordering",
    "-personal_score",
  );
  assert.equal(consoleErrors.length, 0, `browser console errors: ${consoleErrors.join(" | ")}`);
  process.stdout.write("dashboard initial request browser regression: PASS\n");
} finally {
  await browser.close();
  server.kill();
}
