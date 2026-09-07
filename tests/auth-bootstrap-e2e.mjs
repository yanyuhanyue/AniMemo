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
  for (const mode of ["getter", "removeItem"]) {
    const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
    await context.addInitScript((storageMode) => {
      if (storageMode === "getter") {
        for (const name of ["localStorage", "sessionStorage"]) {
          Object.defineProperty(window, name, { get() { throw new DOMException("Storage unavailable", "SecurityError"); } });
        }
      } else {
        Storage.prototype.removeItem = () => { throw new DOMException("Storage unavailable", "SecurityError"); };
      }
    }, mode);
    const page = await context.newPage();
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    await page.route("**/*", async (route) => {
      const url = new URL(route.request().url());
      assert.equal(url.origin, origin, "The bootstrap regression must not contact an external service");
      if (!url.pathname.startsWith("/api/")) return route.continue();
      const path = url.pathname.replace(/^\/api\/v1\//, "");
      let status = 200;
      let body = {};
      if (path === "setup/status/") body = { state: "initialized", accepting_setup: false };
      if (path === "auth/csrf/") body = { csrf_token: "synthetic-csrf" };
      if (path === "site-settings/") body = { site_name: "AniMemo", trusted_poster_hosts: [] };
      if (path === "plugins/enabled/") body = { plugins: [], manifests: {} };
      if (path === "token/refresh/" || path === "auth/me/") { status = 401; body = { detail: "Unauthenticated" }; }
      return route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
    });
    await page.goto(`${origin}/login`, { waitUntil: "networkidle" });
    await page.getByPlaceholder("请输入用户名或注册邮箱").waitFor({ state: "visible" });
    await page.waitForFunction(() => getComputedStyle(document.querySelector(".auth-shell")).opacity === "1");
    assert.equal(new URL(page.url()).pathname, "/login");
    assert.deepEqual(errors, []);
    if (process.env.AUTH_BOOTSTRAP_SCREENSHOT_DIR) {
      await page.screenshot({ path: resolve(process.env.AUTH_BOOTSTRAP_SCREENSHOT_DIR, `storage-${mode}.png`), fullPage: true });
    }
    await context.close();
    process.stdout.write(`PASS storage ${mode}: the real login page starts without browser storage\n`);
  }
} finally {
  await browser?.close();
  server.kill();
  if (server.exitCode === null) await new Promise((done) => server.once("exit", done));
}
