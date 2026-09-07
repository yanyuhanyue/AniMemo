import assert from "node:assert/strict";
import { createServer } from "node:http";
import { fileURLToPath } from "node:url";

import { chromium } from "@playwright/test";
import { createServer as createViteServer } from "vite";

function deferred() {
  let resolve;
  const promise = new Promise((done) => { resolve = done; });
  return { promise, resolve };
}

const root = fileURLToPath(new URL("..", import.meta.url));
const origin = "http://127.0.0.1:55174";
const refreshCookie = (value) => `animemo_refresh=${value}; Path=/api/; HttpOnly; SameSite=Lax`;
const csrfCookie = (value) => `csrftoken=${value}; Path=/; SameSite=Lax`;
const sessionCookie = (value) => `sessionid=${value}; Path=/; HttpOnly; SameSite=Lax`;
const clearRefresh = "animemo_refresh=; Path=/api/; SameSite=Lax; Max-Age=0";
const bCookies = [refreshCookie("synthetic-b"), csrfCookie("synthetic-csrf-b"), sessionCookie("synthetic-session-b")];
const aBody = { access: "synthetic-a", user: { id: 1, username: "A" } };
const bBody = { access: "synthetic-b", user: { id: 2, username: "B" } };
const definitions = [
  { name: "refresh success", action: "refresh", path: "token/refresh/", status: 200, body: aBody, cookies: [refreshCookie("synthetic-a-rotated")] },
  { name: "refresh rejection", action: "refresh", path: "token/refresh/", status: 401, body: { code: "session_expired" }, cookies: [clearRefresh] },
  { name: "logout success", action: "logout", path: "auth/logout/", status: 200, body: {}, cookies: [clearRefresh, "sessionid=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0"] },
  { name: "logout failure", action: "logout", path: "auth/logout/", status: 503, body: {}, cookies: [] },
  { name: "login success", action: "login", path: "token/", status: 200, body: aBody, cookies: [refreshCookie("synthetic-a-rotated"), csrfCookie("synthetic-csrf-a-late")] },
  { name: "login failure", action: "login", path: "token/", status: 401, body: {}, cookies: [] },
  { name: "staff login success", action: "staffLogin", path: "auth/staff-login/", status: 200, body: aBody, cookies: [refreshCookie("synthetic-a-rotated"), csrfCookie("synthetic-csrf-a-late"), sessionCookie("synthetic-session-a-late")] },
  { name: "CSRF success", action: "csrf", path: "auth/csrf/", status: 200, body: { csrf_token: "synthetic-csrf-a-late" }, cookies: [csrfCookie("synthetic-csrf-a-late")] },
  { name: "password change success", action: "changePassword", path: "auth/password-change/", status: 200, body: {}, cookies: [clearRefresh] },
  { name: "account deletion success", action: "deleteAccount", path: "auth/account/", status: 204, body: null, cookies: [clearRefresh, "sessionid=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0"] },
];
const cases = [
  ...definitions.map((definition) => ({ ...definition, phase: "before headers" })),
  ...definitions.filter((definition) => ["refresh success", "refresh rejection", "logout success", "staff login success", "CSRF success"].includes(definition.name))
    .map((definition) => ({ ...definition, phase: "after headers, before body" })),
];

let active;
let vite;
let browser;
const observations = [];
const json = (response, status, body, cookies = []) => {
  response.writeHead(status, { "content-type": "application/json", "cache-control": "no-store", ...(cookies.length ? { "set-cookie": cookies } : {}) });
  response.end(body === null ? undefined : JSON.stringify(body));
};
const server = createServer(async (request, response) => {
  try {
    const url = new URL(request.url, origin);
    if (url.pathname === "/") return json(response, 200, {});
    if (!url.pathname.startsWith("/api/")) return vite.middlewares(request, response);
    const path = url.pathname.replace(/^\/api\/v1\//, "");
    let raw = "";
    for await (const chunk of request) raw += chunk;
    const body = raw ? JSON.parse(raw) : {};
    if (!active.held && path === active.path && body.username !== "B") {
      const scenario = active;
      active.held = response;
      active.bodyText = active.body === null ? "" : JSON.stringify(active.body);
      active.closed = false;
      response.on("close", () => { scenario.closed = true; });
      if (active.phase === "after headers, before body") {
        response.writeHead(active.status, { "content-type": "application/json", "cache-control": "no-store", "set-cookie": active.cookies });
        response.write(active.bodyText.slice(0, 1));
      }
      active.started.resolve();
      return;
    }
    if (path === "auth/csrf/") return json(response, 200, { csrf_token: "synthetic-csrf-b" }, [csrfCookie("synthetic-csrf-b")]);
    if (path === "auth/staff-login/" && body.username === "B") return json(response, 200, bBody, bCookies);
    if (path === "token/refresh/") {
      const cookies = request.headers.cookie || "";
      if (cookies.includes("animemo_refresh=synthetic-b")) return json(response, 200, bBody);
      if (cookies.includes("animemo_refresh=synthetic-a-rotated")) return json(response, 200, aBody);
      return json(response, 401, { code: "authentication_required" });
    }
    if (path === "auth/register/request/") return json(response, 200, {});
    throw new Error(`Unexpected synthetic API path: ${path}`);
  } catch (error) {
    active?.errors.push(error.message);
    if (!response.headersSent) json(response, 500, { code: "synthetic_fixture_failure" });
    else response.destroy();
  }
});

try {
  // Serve the exact application modules; the synthetic HTTP server owns every API response.
  vite = await createViteServer({
    root,
    configFile: false,
    optimizeDeps: { entries: ["src/lib/api.js"], include: ["axios"] },
    server: { middlewareMode: true, hmr: false },
    appType: "custom",
  });
  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(55174, "127.0.0.1", resolve);
  });
  process.stdout.write(`Owned native HTTP server PID ${process.pid}, loopback ${origin}\n`);
  browser = await chromium.launch({ headless: true });
  for (const definition of cases) {
    active = { ...definition, started: deferred(), held: null, errors: [] };
    const context = await browser.newContext();
    await context.addCookies([
      { name: "animemo_refresh", value: "synthetic-a", domain: "127.0.0.1", path: "/api/", httpOnly: true, sameSite: "Lax" },
      { name: "csrftoken", value: "synthetic-csrf-a", domain: "127.0.0.1", path: "/", sameSite: "Lax" },
      { name: "sessionid", value: "synthetic-session-a", domain: "127.0.0.1", path: "/", httpOnly: true, sameSite: "Lax" },
    ]);
    const page = await context.newPage();
    await page.route("**/*", (route) => {
      assert.equal(new URL(route.request().url()).origin, origin, "Only owned loopback HTTP may be contacted");
      return route.continue();
    });
    await page.goto(origin);
    await page.evaluate(async ({ baseURL, action }) => {
      const { createAuthSession } = await import("/src/lib/authSession.js");
      const { createWebApiTransport } = await import("/src/lib/webApiTransport.js");
      const { createWebAuthAdapter } = await import("/src/lib/webAuthAdapter.js");
      const session = createAuthSession();
      const transport = createWebApiTransport({ baseURL, session });
      const adapter = createWebAuthAdapter({ session, api: transport.api, cookieClient: transport.cookieClient, browser: window });
      adapter.storeTokens({ access: "synthetic-a", user: { id: 1, username: "A" } });
      window.cookieProbe = { session, adapter };
      let request;
      if (action === "refresh") request = adapter.refreshAccessToken();
      else if (action === "csrf") request = adapter.authApi.registerRequest("synthetic-a@example.test");
      else if (action === "login" || action === "staffLogin") request = adapter.authApi[action]("A", "synthetic-password");
      else request = adapter.authApi[action]({ current_password: "synthetic-password" });
      window.cookieProbe.prior = request.then(() => "resolved", (error) => error.code || "rejected");
    }, { baseURL: `${origin}/api/v1`, action: active.action });
    let dispatchTimeout;
    try {
      await Promise.race([active.started.promise, new Promise((_, reject) => {
        dispatchTimeout = setTimeout(() => reject(new Error("Old request was not dispatched")), 10000);
      })]);
    } finally {
      clearTimeout(dispatchTimeout);
    }
    if (active.phase === "after headers, before body") {
      const relevant = active.cookies[0].split(";")[0].split("=");
      for (let attempt = 0; ; attempt += 1) {
        const actual = (await context.cookies()).find((cookie) => cookie.name === relevant[0]);
        if (actual?.value === relevant[1] || (!relevant[1] && !actual)) break;
        if (attempt >= 100) throw new Error("Old HTTP headers were not processed before the account switch");
        await new Promise((done) => setTimeout(done, 20));
      }
    }
    await page.evaluate(async () => {
      const result = await window.cookieProbe.adapter.authApi.staffLogin("B", "synthetic-password");
      window.cookieProbe.adapter.storeTokens(result.data);
    });
    const before = Object.fromEntries((await context.cookies()).map((cookie) => [cookie.name, cookie.value]));
    assert.equal(before.animemo_refresh, "synthetic-b");
    assert.equal(before.csrftoken, "synthetic-csrf-b");
    assert.equal(before.sessionid, "synthetic-session-b");
    const oldConnectionClosedBeforeResponse = active.closed;
    if (active.phase === "before headers") json(active.held, active.status, active.body, active.cookies);
    else active.held.end(active.bodyText.slice(1));
    const priorOutcome = await page.evaluate(() => window.cookieProbe.prior);
    const after = Object.fromEntries((await context.cookies()).map((cookie) => [cookie.name, cookie.value]));
    await page.evaluate(() => window.cookieProbe.adapter.refreshAccessToken().catch(() => null));
    const userAfterRefresh = await page.evaluate(() => window.cookieProbe.session.getUser()?.id ?? null);
    assert.deepEqual(active.errors, []);
    const result = {
      scenario: active.name,
      phase: active.phase,
      priorOutcome,
      oldConnectionClosedBeforeResponse,
      refreshCookiePreserved: after.animemo_refresh === "synthetic-b",
      csrfCookiePreserved: after.csrftoken === "synthetic-csrf-b",
      sessionCookiePreserved: after.sessionid === "synthetic-session-b",
      userAfterRefresh,
    };
    result.pass = result.refreshCookiePreserved && result.csrfCookiePreserved && result.sessionCookiePreserved && userAfterRefresh === 2 && priorOutcome === "AUTH_SESSION_CHANGED";
    observations.push(result);
    process.stdout.write(JSON.stringify(result) + "\n");
    await context.close();
  }
  process.stdout.write(JSON.stringify({ browserVersion: browser.version(), syntheticOnly: true, actualNativeHttp: true, backendExecuted: false, cases: observations.length }) + "\n");
  assert.ok(observations.every((result) => result.pass), "Old authentication HTTP responses must preserve the new browser cookies and refresh identity");
} finally {
  await browser?.close();
  server.closeAllConnections();
  await new Promise((done) => server.close(done));
  await vite?.close();
  process.stdout.write("Owned browser, HTTP server and Vite middleware closed.\n");
}
