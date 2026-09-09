import assert from "node:assert/strict";
import { createHash } from "node:crypto";
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
let importPreviewRequests = 0;
const bundleFile = Buffer.from(JSON.stringify({ format: "animemo-data-bundle", schema_version: 1, entries: [{
  entry: { title: "会话导入回归番剧", japanese_title: "", watch_status: "planned", visibility: "private" },
  watch_history: [], external_identities: [],
}] }));
const bundleDigest = createHash("sha256").update(bundleFile).digest("hex");
const bundleRequests = { create: 0, chunks: 0, validate: 0, cancel: 0 };
let bundleSession = null;
const consoleErrors = [];
page.on("console", (message) => {
  if (message.type() === "error") consoleErrors.push(message.text());
});
page.on("pageerror", (error) => consoleErrors.push(error.message));

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
    const path = url.pathname.replace(/^\/api\/v1\//, "");
    if (path === "setup/status/") return json(route, { state: "initialized", accepting_setup: false, expires_at: null });
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
      return json(route, { results: [{ id: 1, name: "日常", color: "rose", sort_order: 10 }] });
    }
    if (path === "stats/me/") {
      await wait(120);
      return json(route, {});
    }
    if (path === "import/" && request.method() === "POST") {
      importPreviewRequests += 1;
      assert.match(request.headers()["content-type"] || "", /^multipart\/form-data;/);
      return json(route, {
        total: 1,
        ready: 1,
        skipped_duplicates: 0,
        errors: [],
        items: [{ row: 1, title: "导入回归番剧", status: "ready", reason: "等待导入" }],
      });
    }
    if (path === "bundle-restores/current/") return json(route, { session: bundleSession });
    if (path === "bundle-restores/" && request.method() === "POST") {
      bundleRequests.create += 1;
      const payload = request.postDataJSON();
      assert.deepEqual(Object.keys(payload).sort(), ["idempotency_key", "expected_bytes", "sha256", "schema_version"].sort());
      assert.match(payload.idempotency_key, /^[0-9a-f-]{36}$/);
      assert.equal(payload.sha256, bundleDigest);
      assert.equal(payload.expected_bytes, bundleFile.length);
      assert.equal(payload.schema_version, 1);
      bundleSession = {
        id: "07000000-0000-4000-8000-000000000001", generation: 1, state: "receiving",
        expected_bytes: bundleFile.length, received_bytes: 0, sha256: bundleDigest,
        chunk_bytes: 1048576, preview: {}, receipt: {}, error_code: "", cleanup_pending: false,
      };
      return json(route, bundleSession);
    }
    if (bundleSession && path.startsWith(`bundle-restores/${bundleSession.id}/`)) {
      if (path.endsWith("chunks/") && request.method() === "PUT") {
        bundleRequests.chunks += 1;
        assert.equal(request.headers()["content-type"], "application/octet-stream");
        assert.equal(url.searchParams.get("offset"), "0");
        assert.equal(url.searchParams.get("generation"), "1");
        assert.deepEqual(request.postDataBuffer(), bundleFile);
        bundleSession.received_bytes = bundleFile.length;
      } else if (path.endsWith("validate/") && request.method() === "POST") {
        bundleRequests.validate += 1;
        assert.deepEqual(request.postDataJSON(), { generation: 1 });
        assert.equal(bundleSession.received_bytes, bundleFile.length);
        bundleSession.state = "ready";
        bundleSession.preview = { total: 1, ready: 1, skipped_duplicates: 0, errors: [],
          items: [{ row: 1, title: "会话导入回归番剧", status: "ready" }], items_truncated: false };
      } else if (path.endsWith("cancel/") && request.method() === "POST") {
        bundleRequests.cancel += 1;
        assert.deepEqual(request.postDataJSON(), { generation: 1 });
        bundleSession.state = "cancelled";
        bundleSession.generation += 1;
      } else assert.equal(request.method(), "GET");
      return json(route, bundleSession);
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
  const presetTag = page.locator(".tag-chip", { hasText: "日常" }).first();
  assert.match(await presetTag.getAttribute("class"), /\btag-rose\b/, "late tag presets must redecorate loaded entries without refetching");
  assert.equal(entryRequests[0].searchParams.get("page"), "1");
  assert.equal(entryRequests[0].searchParams.get("page_size"), "48");

  const importInput = page.locator('input[type="file"][accept*=".json"]');
  await importInput.setInputFiles({
    name: "critical-import.csv",
    mimeType: "text/csv",
    buffer: Buffer.from("title,watch_status,visibility\n导入回归番剧,planned,private\n"),
  });
  const importDialog = page.getByRole("dialog", { name: "确认导入手账" });
  await importDialog.waitFor({ state: "visible" });
  await importDialog.getByText("critical-import.csv", { exact: true }).waitFor({ state: "visible" });
  await importDialog.getByText("导入回归番剧", { exact: true }).waitFor({ state: "visible" });
  assert.equal(importPreviewRequests, 1, "opening a CSV preview must issue exactly one multipart request");
  assert.equal(entryRequests.length, 1, "import preview must not invalidate or reload journal entries");
  await importDialog.getByRole("button", { name: "取消" }).click();
  await importDialog.waitFor({ state: "detached" });

  await importInput.setInputFiles({ name: "critical-import.json", mimeType: "application/json", buffer: bundleFile });
  const restoreDialog = page.getByRole("dialog", { name: "恢复手账数据包" });
  await restoreDialog.waitFor({ state: "visible" });
  await restoreDialog.getByText("校验完成，等待确认恢复", { exact: true }).waitFor({ state: "visible" });
  await restoreDialog.getByText("critical-import.json", { exact: true }).waitFor({ state: "visible" });
  await restoreDialog.getByText("会话导入回归番剧", { exact: true }).waitFor({ state: "visible" });
  assert.deepEqual(bundleRequests, { create: 1, chunks: 1, validate: 1, cancel: 0 });
  assert.equal(importPreviewRequests, 1, "JSON must use restore sessions without a multipart fallback");
  assert.equal(entryRequests.length, 1, "JSON upload and validation must not invalidate or reload journal entries");
  await restoreDialog.getByRole("button", { name: "取消恢复", exact: true }).click();
  await restoreDialog.getByText("恢复已取消", { exact: true }).waitFor({ state: "visible" });
  assert.equal(bundleRequests.cancel, 1);
  assert.equal(entryRequests.length, 1, "cancelling a preview must not reload journal entries");
  await restoreDialog.getByRole("button", { name: "关闭", exact: true }).last().click();
  await restoreDialog.waitFor({ state: "detached" });

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
