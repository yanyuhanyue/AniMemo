import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { existsSync } from "node:fs";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";

process.env.PLAYWRIGHT_BROWSERS_PATH ||= resolve(".playwright-browsers");
const { chromium } = await import("@playwright/test");

const host = "127.0.0.1";
const port = Number(process.env.AUTH_FOCUS_PORT || 4174);
const externalBaseUrl = process.env.AUTH_FOCUS_BASE_URL;
const baseUrl = externalBaseUrl || `http://${host}:${port}`;
const projectRoot = fileURLToPath(new URL("..", import.meta.url));
const viewport = {
  width: Number(process.env.AUTH_FOCUS_VIEWPORT_WIDTH || 1440),
  height: Number(process.env.AUTH_FOCUS_VIEWPORT_HEIGHT || 900),
};

if (!externalBaseUrl && !existsSync(resolve(projectRoot, "dist/client/index.html"))) {
  throw new Error("Production build missing; run npm run build before the auth browser regression.");
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
  throw new Error("Timed out waiting for auth browser state.");
}

function json(route, body, status = 200) {
  return route.fulfill({
    status,
    contentType: "application/json",
    body: JSON.stringify(body),
  });
}

async function inspectLeadingIcon(input) {
  const icon = input.locator("xpath=../*[name()='svg'][1]");
  await icon.waitFor({ state: "visible" });
  return icon.evaluate((element) => {
    const rect = element.getBoundingClientRect();
    const hit = document.elementFromPoint(rect.left + rect.width / 2, rect.top + rect.height / 2);
    return {
      hitTag: hit?.tagName || null,
      iconOwnsPoint: hit === element || element.contains(hit),
      opacity: getComputedStyle(element).opacity,
      visibility: getComputedStyle(element).visibility,
    };
  });
}

async function assertIconSurvivesFocus(page, placeholder) {
  const input = page.getByPlaceholder(placeholder);
  await input.waitFor({ state: "visible" });
  const icon = input.locator("xpath=../*[name()='svg'][1]");
  const before = await inspectLeadingIcon(input);
  assert.equal(before.iconOwnsPoint, true, `${placeholder}: icon is covered before focus (${JSON.stringify(before)})`);
  await icon.click();
  await page.waitForTimeout(80);
  assert.equal(await input.evaluate((element) => document.activeElement === element), true, `${placeholder}: icon click did not focus the input`);
  const after = await inspectLeadingIcon(input);
  assert.equal(after.iconOwnsPoint, true, `${placeholder}: icon is covered after focus (${JSON.stringify(after)})`);
}

const server = externalBaseUrl ? null : spawn(
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
  if (server) {
    await waitFor(async () => {
      try {
        return (await fetch(`${baseUrl}/`)).ok;
      } catch {
        return false;
      }
    }, 10000);
  }

  browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport });
  const consoleErrors = [];
  page.on("console", (message) => {
    if (message.type() === "error" && !message.text().startsWith("Failed to load resource:")) consoleErrors.push(message.text());
  });
  page.on("pageerror", (error) => consoleErrors.push(error.message));

  await page.route("**/api/**", async (route) => {
    const path = new URL(route.request().url()).pathname.replace(/^\/api\/v1\//, "");
    if (path === "setup/status/") return json(route, { state: "initialized", accepting_setup: false, expires_at: null });
    if (path === "site-settings/") return json(route, { site_name: "AniMemo", trusted_poster_hosts: [] });
    if (path === "plugins/enabled/") return json(route, { plugins: [], manifests: {} });
    if (path === "auth/csrf/") return json(route, { csrf_token: "browser-test-token" });
    if (path === "token/refresh/" || path === "auth/me/") return json(route, { detail: "unauthenticated" }, 401);
    return json(route, {});
  });

  await page.goto(`${baseUrl}/login`, { waitUntil: "networkidle" });
  await assertIconSurvivesFocus(page, "请输入用户名或注册邮箱");
  await assertIconSurvivesFocus(page, "请输入登录密码");
  await page.getByRole("button", { name: "忘记密码？→" }).click();
  await assertIconSurvivesFocus(page, "请输入注册时使用的邮箱");
  assert.equal(consoleErrors.length, 0, `browser console errors: ${consoleErrors.join(" | ")}`);
  if (process.env.AUTH_FOCUS_SCREENSHOT) {
    await page.screenshot({ path: resolve(process.env.AUTH_FOCUS_SCREENSHOT), fullPage: true });
  }
  process.stdout.write(`auth field leading icons remain visible on focus at ${viewport.width}x${viewport.height}\n`);
} finally {
  await browser?.close();
  server?.kill();
}
