import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { chromium } from "@playwright/test";

function deferred() {
  let resolvePromise;
  const promise = new Promise((done) => { resolvePromise = done; });
  return { promise, resolve: resolvePromise };
}

const root = fileURLToPath(new URL("..", import.meta.url));
const origin = "http://127.0.0.1:55174";
const server = spawn(process.execPath, [resolve(root, "node_modules/vite/bin/vite.js"), "--host", "127.0.0.1", "--port", "55174", "--strictPort"], {
  cwd: root, env: { ...process.env, BROWSER: "none" }, stdio: ["ignore", "pipe", "pipe"], windowsHide: true,
});
let startupOutput = "";
server.stdout.on("data", (chunk) => { startupOutput += chunk; });
server.stderr.on("data", (chunk) => { startupOutput += chunk; });
let browser;
try {
  for (let attempt = 0; ; attempt += 1) {
    if (server.exitCode !== null) throw new Error("Owned Vite server exited before startup; the port may be occupied.");
    try { if (startupOutput.includes("Local:") && (await fetch(origin)).ok) break; } catch { /* Owned loopback startup. */ }
    if (attempt >= 100) throw new Error("Owned Vite server did not start.");
    await new Promise((done) => setTimeout(done, 50));
  }
  process.stdout.write(`Owned Vite PID ${server.pid}, loopback ${origin}\n`);
  browser = await chromium.launch({ headless: true });
  for (const outcome of ["success", "failure"]) {
    const page = await browser.newPage({ viewport: { width: 1440, height: 900 }, reducedMotion: "reduce" });
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    let delayRefresh = false;
    let refreshCalls = 0;
    const refreshStarted = deferred();
    const logoutStarted = deferred();
    const json = (route, body, status = 200) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
    await page.route("**/*", async (route) => {
      const url = new URL(route.request().url());
      assert.equal(url.origin, origin, "The session regression must not contact an external service");
      if (!url.pathname.startsWith("/api/")) return route.continue();
      const path = url.pathname.replace(/^\/api\/v1\//, "");
      const account = route.request().headers().authorization === "Bearer access-b" ? "B" : "A";
      if (path === "setup/status/") return json(route, { state: "initialized", accepting_setup: false });
      if (path === "auth/csrf/") return json(route, { csrf_token: "synthetic-csrf" });
      if (path === "token/refresh/") {
        refreshCalls += 1;
        if (delayRefresh) { refreshStarted.resolve(route); return; }
        return json(route, { access: "access-a", user: { id: 1, username: "member-a" } });
      }
      if (path === "auth/logout/") { logoutStarted.resolve(route); return; }
      if (path === "auth/me/") return json(route, { id: 1, username: "member-a" });
      if (path === "site-settings/") return json(route, { site_name: "AniMemo", trusted_poster_hosts: [] });
      if (path === "plugins/enabled/") return json(route, { plugins: [], manifests: {} });
      if (path === "settings/me/") return json(route, { nickname: `Synthetic ${account}`, email: `${account.toLowerCase()}@example.test` });
      if (path === "filters/" || path === "tag-presets/") return json(route, { results: [] });
      if (path === "entries/") return json(route, { count: 1, next: null, facets: { tags: [], years: [] }, results: [{
        id: account === "A" ? 1 : 2, title: `PRIVATE_RECORD_${account}`, airing_period: "2026-01", episodes: 12,
        personal_score: null, watch_status: "planned", tags: [], visibility: "private",
      }] });
      return json(route, {});
    });
    await page.goto(`${origin}/dashboard`, { waitUntil: "networkidle" });
    await page.getByText("PRIVATE_RECORD_A", { exact: true }).first().waitFor({ state: "visible" });
    assert.equal(refreshCalls, 1, "React StrictMode must share startup authentication");

    delayRefresh = true;
    await page.evaluate(async () => {
      const auth = await import("/src/lib/api.js");
      window.pendingRefresh = auth.refreshAccessToken().catch(() => null);
    });
    const oldRefresh = await refreshStarted.promise;
    await page.evaluate(async () => {
      const auth = await import("/src/lib/api.js");
      auth.storeTokens({ access: "access-b", user: { id: 2, username: "member-b" } });
    });
    await page.getByText("PRIVATE_RECORD_B", { exact: true }).first().waitFor({ state: "visible", timeout: 5000 });
    assert.equal(await page.getByText("PRIVATE_RECORD_A", { exact: true }).count(), 0);
    await json(oldRefresh, outcome === "success" ? { access: "access-a", user: { id: 1, username: "member-a" } } : { detail: "Old refresh rejected" }, outcome === "success" ? 200 : 401);
    await page.evaluate(() => window.pendingRefresh);
    assert.equal(await page.evaluate(async () => (await import("/src/lib/api.js")).getAuthUser().id), 2);
    assert.equal(await page.getByText("PRIVATE_RECORD_A", { exact: true }).count(), 0);

    await page.getByRole("button", { name: "打开账户菜单" }).click();
    await page.getByRole("menuitem", { name: /退出登录/ }).click();
    const oldLogout = await logoutStarted.promise;
    await page.evaluate(async () => {
      const auth = await import("/src/lib/api.js");
      auth.storeTokens({ access: "access-a", user: { id: 1, username: "member-a" } });
    });
    await page.getByText("PRIVATE_RECORD_A", { exact: true }).first().waitFor({ state: "visible" });
    await json(oldLogout, outcome === "success" ? {} : { detail: "Old logout unavailable" }, outcome === "success" ? 200 : 503);
    await page.waitForLoadState("networkidle");
    assert.equal(await page.evaluate(async () => (await import("/src/lib/api.js")).getAuthUser()?.id), 1);
    assert.equal(new URL(page.url()).pathname, "/dashboard");
    assert.equal(await page.getByText("PRIVATE_RECORD_B", { exact: true }).count(), 0);
    assert.deepEqual(errors, []);
    if (process.env.AUTH_SESSION_SCREENSHOT_DIR) {
      await page.screenshot({ path: resolve(process.env.AUTH_SESSION_SCREENSHOT_DIR, `session-${outcome}.png`), fullPage: true });
    }
    await page.close();
    process.stdout.write(`PASS late ${outcome}: account switches discard prior private UI and isolate old refresh/logout callbacks\n`);
  }
} finally {
  await browser?.close();
  server.kill();
  if (server.exitCode === null) await new Promise((done) => server.once("exit", done));
}
