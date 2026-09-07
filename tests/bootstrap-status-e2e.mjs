import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { existsSync } from "node:fs";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { stripVTControlCharacters } from "node:util";

import { chromium } from "@playwright/test";

const root = fileURLToPath(new URL("..", import.meta.url));
const origin = "http://127.0.0.1:55174";
if (!existsSync(resolve(root, "dist/client/index.html"))) throw new Error("Run npm run build before the bootstrap browser regression.");
const server = spawn(process.execPath, [resolve(root, "node_modules/vite/bin/vite.js"), "preview", "--host", "127.0.0.1", "--port", "55174", "--strictPort"], {
  cwd: root,
  env: { ...process.env, BROWSER: "none" },
  stdio: ["ignore", "pipe", "pipe"],
  windowsHide: true,
});
let startupOutput = "";
server.stdout.on("data", (chunk) => { startupOutput += chunk; });
server.stderr.on("data", (chunk) => { startupOutput += chunk; });
let browser;
try {
  for (let attempt = 0; ; attempt += 1) {
    if (server.exitCode !== null) throw new Error("Owned preview exited before startup; the port may already be occupied.");
    try {
      if (stripVTControlCharacters(startupOutput).includes("Local:") && (await fetch(origin)).ok) break;
    } catch { /* Wait only for this loopback preview. */ }
    if (attempt >= 100) throw new Error("Owned preview did not start.");
    await new Promise((done) => setTimeout(done, 50));
  }
  process.stdout.write(`Owned preview PID ${server.pid}, loopback ${origin}\n`);
  browser = await chromium.launch({ headless: true });
  for (const scenario of ["503", "network", "malformed", "initializing", "missing"]) {
    const page = await browser.newPage({ viewport: { width: scenario === "network" ? 390 : 1440, height: 900 }, reducedMotion: "reduce" });
    const errors = [];
    const restrictedRequests = [];
    page.on("pageerror", (error) => errors.push(error.message));
    let statusCalls = 0;
    let pendingStatus;
    let resolveStatus;
    const statusReceived = new Promise((done) => { resolveStatus = done; });
    await page.route("**/*", async (route) => {
      const url = new URL(route.request().url());
      assert.equal(url.origin, origin, "The bootstrap regression must not contact an external service");
      if (!url.pathname.startsWith("/api/")) return route.continue();
      const path = url.pathname.replace(/^\/api\/v1\//, "");
      const json = (body, status = 200) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
      if (path === "setup/status/") {
        statusCalls += 1;
        if (statusCalls === 1) { pendingStatus = route; resolveStatus(); return; }
        return json({ state: "initialized", accepting_setup: false });
      }
      if (path === "auth/csrf/") return json({ csrf_token: "synthetic-csrf" });
      if (path === "token/refresh/" || path === "auth/me/") return json({ detail: "Unauthenticated" }, 401);
      restrictedRequests.push(path);
      if (path === "plugins/enabled/") return json({ plugins: [], manifests: {} });
      if (path === "site-settings/") return json({ site_name: "AniMemo", trusted_poster_hosts: [] });
      return json({});
    });
    await page.goto(`${origin}/login?next=%2Fdashboard`, { waitUntil: "domcontentloaded" });
    await statusReceived;
    await page.locator(".app-auth-bootstrap").waitFor({ state: "visible" });
    assert.deepEqual(restrictedRequests, [], "Unknown installation state must keep restricted UI and plugin loading closed");
    assert.equal(new URL(page.url()).pathname, "/login");
    if (scenario === "network") await pendingStatus.abort("failed");
    else await pendingStatus.fulfill({
      status: scenario === "503" ? 503 : 200,
      contentType: "application/json",
      body: JSON.stringify(scenario === "missing" ? null : { state: scenario === "initializing" ? "initializing" : "unexpected" }),
    });
    await page.getByRole("heading", { name: "暂时无法确认站点状态" }).waitFor({ state: "visible", timeout: 5000 });
    assert.equal(new URL(page.url()).pathname, "/login");
    assert.equal(new URL(page.url()).searchParams.get("next"), "/dashboard");
    assert.deepEqual(restrictedRequests, [], "Unavailable status must keep product and setup capabilities closed");
    assert.equal(await page.getByRole("heading", { name: "创建首位管理员" }).count(), 0);
    assert.equal(await page.locator("input").count(), 0);
    const retry = page.getByRole("button", { name: "重新检查" });
    await page.keyboard.press("Tab");
    assert.equal(await retry.evaluate((element) => document.activeElement === element), true);
    if (process.env.AUTH_BOOTSTRAP_SCREENSHOT_DIR) {
      await page.screenshot({ path: resolve(process.env.AUTH_BOOTSTRAP_SCREENSHOT_DIR, `status-${scenario}.png`), fullPage: true });
    }
    await retry.press("Enter");
    await page.getByPlaceholder("请输入用户名或注册邮箱").waitFor({ state: "visible" });
    assert.equal(new URL(page.url()).pathname, "/login");
    assert.equal(statusCalls, 2, "A user retry performs one new status request");
    assert.deepEqual(errors, []);
    await page.close();
    process.stdout.write(`PASS status ${scenario}: unknown/unavailable remain closed and keyboard retry restores the requested route\n`);
  }

  for (const acceptingSetup of [true, false]) {
    const page = await browser.newPage({ reducedMotion: "reduce" });
    await page.route("**/*", async (route) => {
      const url = new URL(route.request().url());
      assert.equal(url.origin, origin, "The bootstrap regression must not contact an external service");
      if (!url.pathname.startsWith("/api/")) return route.continue();
      const path = url.pathname.replace(/^\/api\/v1\//, "");
      let body = {};
      let status = 200;
      if (path === "setup/status/") body = { state: "uninitialized", accepting_setup: acceptingSetup };
      if (path === "auth/csrf/") body = { csrf_token: "synthetic-csrf" };
      if (path === "token/refresh/" || path === "auth/me/") status = 401;
      if (path === "site-settings/") body = { site_name: "AniMemo", trusted_poster_hosts: [] };
      return route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
    });
    await page.goto(`${origin}/dashboard`, { waitUntil: "networkidle" });
    await page.getByRole("heading", { name: "创建首位管理员" }).waitFor({ state: "visible" });
    assert.equal(new URL(page.url()).pathname, "/setup");
    assert.equal(await page.getByRole("button", { name: "创建管理员并锁定首装入口" }).isEnabled(), acceptingSetup);
    await page.close();
    process.stdout.write(`PASS explicit uninitialized state: setup accepting=${acceptingSetup}\n`);
  }
} finally {
  await browser?.close();
  server.kill();
  if (server.exitCode === null) await new Promise((done) => server.once("exit", done));
}
